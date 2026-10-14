"""MOD-07 候选评估编排与接受判定表（PRD FR-07-6 … FR-07-8；§8.2）。

两件事，职责**严格分开**：

``evaluate_candidate()`` —— 只编排，不做统计
    冒烟（``split="train"``，本代训练块末尾 ``smoke_n`` 个 seed）→ 快速淘汰 →
    heldout（``evolve.heldout_seeds``，默认 40 局）→ ``compare_paired``。
    指标全部委托 MOD-08 ``evaluate_arm`` / ``compare_paired``（NFR-07-6）。
    parent 与候选必须使用**同一个冻结的记忆快照** ``memory_version``（FR-07-7）。

``judge()`` —— 判定表（§8.2 六项，短路求值）
    ``accept ⟺ C0∧C1∧C2∧C3∧C4∧C5``；否则 ``reject`` 且 ``reason`` 含**首个失败条件 id**
    （如 ``C2:heldout_win_rate 0.28<0.32``）。逐条写 ``condition_trace``。

纯函数边界：``judge`` / ``smoke_fast_reject`` / ``gap_of`` 不落盘、不调模型、不 import
网络——它们是 G1 覆盖率 ≥80% 的主力（PRD §12）。
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping, Sequence
from pathlib import Path

from dice_ratchet.infra import events
from dice_ratchet.infra.errors import E, InfraError

from .errors import JudgeFailedError, SmokeFailedError
from .schemas import Candidate, CandidateReport, Verdict, _plain

__all__ = [
    "CONDITION_IDS", "DEFAULT_SMOKE_BASKET", "HOLDOUT_BASKET", "PRIMARY_METRIC",
    "CandidateEvaluator", "better", "condition_trace_entry", "evaluate_candidate",
    "first_failure", "gap_of", "judge", "metric_of", "pick_winner",
    "smoke_failure_reason", "smoke_fast_reject", "smoke_seeds_for",
    "superseded_verdict", "verdict_for_winner",
]

#: 判定表条件 id（§8.2；顺序即短路顺序）。
CONDITION_IDS: tuple[str, ...] = ("C0", "C1", "C2", "C3", "C4", "C5")

#: C2 允许的"显著提升"指标集（方法归 MOD-08 ``compare_paired``）。
_PRIMARY_CANDIDATES: tuple[str, ...] = ("win_rate", "mean_stages_cleared",
                                        "mean_hearts_left")
PRIMARY_METRIC: str = "win_rate"

#: heldout 篮子（Spec B-07：H1=代际裁判）。
HOLDOUT_BASKET: str = "H1"
DEFAULT_SMOKE_BASKET: str = "train"

#: 冒烟快速淘汰阈值（FR-07-6）。
_SMOKE_WIN_RATE_MARGIN: float = 0.2
_SMOKE_MAX_ILLEGAL_RATE: float = 0.30
_SMOKE_MAX_FALLBACK_RATE: float = 0.30

#: C5 阈值（Spec v1.3 P-25 结案：不再用回合数）。
#:
#: **2026-10-09（Spec v2.0 B-44，用户裁决 ②+③）**：C5 由「三项绝对门」改为
#: 「``illegal`` / ``fallback`` 绝对门 + ``truncation`` **相对门**」。
#: 理由：``truncation_rate`` 是**局型 × 策略强度**的合成属性（同一 run 里 heuristic 0.00、
#: DeepSeek hard×6 0.30），而 ``illegal``/``fallback`` 是 agent 的能力/遵从属性。
#: 把三者一起按一个臂标定，换臂（8B → DeepSeek）后 truncation 绝对门会 reject 每个候选，
#: 把闭环静默锁死（缺陷 F28，见 docs/tasks/2026-10-06-M2开工检查单.md §8.2）。
_C5_ILLEGAL: float = 0.05
_C5_FALLBACK: float = 0.10
#: ``truncation`` 相对门容差：``candidate ≤ parent + tol``。
#: 0.05 = n=40 下的 2 局（粒度 1/40 = 0.025），与 ``evolve.win_rate_tol`` 同依据（D-3）。
_C5_TRUNCATION_TOL: float = 0.05
#: **2026-10-11（D-M3-2，用户裁决 E）**：三轴统一为「相对门 + 绝对 backstop」。
#: 现场：``illegal``/``fallback`` 的绝对门（0.35 / 0.25）对 ``C1`` 实测（0.0318 / 0.0017）
#: 松 11× / 147× ⇒ 实际非约束门 ⇒「候选把非法率推高」的退化候选被放行、
#: **判据 2 的归因结论不成立**。改为与 truncation 同构后才有诊断力。
#: ``backstop`` 只用于堵相对门的已知放宽（parent 已很烂 ⇒ 跟着烂），不替代相对门。
_C5_ILLEGAL_TOL: float = 0.05
_C5_FALLBACK_TOL: float = 0.05
_C5_TRUNCATION_BACKSTOP: float = 0.60
#: 相对门的通用兜底（`max_illegal_rate` / `max_fallback_rate` 缺省时的 backstop）。
_C5_BACKSTOP: float = 0.35


# =====================================================================
# ArmResult 的鸭子类型取值（MOD-07 不 import MOD-08 实现细节）
# =====================================================================

def _metric_bag(result: Any) -> Mapping[str, Any]:
    """取 ``ArmResult.metrics``（``SplitMetrics``）或其字典形态。"""
    metrics = getattr(result, "metrics", None)
    if metrics is None and isinstance(result, Mapping):
        metrics = result.get("metrics", result)
    if metrics is None:
        raise JudgeFailedError("ArmResult 缺少 metrics 段",
                               hint="MOD-08 evaluate_arm 必须返回带 metrics 的结果")
    if isinstance(metrics, Mapping):
        return metrics
    as_dict = getattr(metrics, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    raise JudgeFailedError(f"metrics 形态不可识别：{type(metrics).__name__}")


def metric_of(result: Any, name: str, default: float | None = None) -> float | None:
    """从 ``ArmResult`` 取一个指标（缺失/None → ``default``）。"""
    if result is None:
        return default
    try:
        bag = _metric_bag(result)
    except JudgeFailedError:
        return default
    value = bag.get(name, None)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def gap_of(candidate_train: Any, candidate_heldout: Any) -> float:
    """``win_rate_gap = train.win_rate - heldout.win_rate``（C3 口径）。

    按 Spec §4.5 B-34 把结果 round 到 4 位：``0.50 - 0.35`` 的浮点差是
    ``0.15000000000000002``，不 round 会让"恰好等于阈值"的候选被误判为超限。
    """
    train = metric_of(candidate_train, "win_rate", 0.0) or 0.0
    heldout = metric_of(candidate_heldout, "win_rate", 0.0) or 0.0
    return round(float(train) - float(heldout), 4)


def smoke_seeds_for(generation: int, *, smoke_n: int, protocol: Any = None) -> tuple[int, ...]:
    """冒烟 seed = **本代训练块的末 ``smoke_n`` 个**（FR-07-6；不再是固定的 1000–1004）。"""
    from dice_ratchet.evals.seeds import take

    block = take("train", generation=max(1, int(generation)), protocol=protocol)
    count = max(1, int(smoke_n))
    return tuple(block[-count:]) if block else ()


# =====================================================================
# 冒烟快速淘汰（FR-07-6；纯函数）
# =====================================================================

def smoke_fast_reject(smoke: Any, *, baseline: Any, cfg: Any) -> str | None:
    """冒烟快速淘汰（FR-07-6）：任一命中即返回原因串（调用方写 ``E_EVOLVE_SMOKE_FAILED``）。

    - 冒烟胜率 < ``parent_smoke`` 胜率 − 0.2；
    - 非法率 > 0.30；
    - fallback 率 > 0.30；
    - harness 运行期异常（``ArmResult.error_codes`` 非空）。
    """
    if smoke is None:
        return "smoke_missing"
    errors = tuple(getattr(smoke, "error_codes", ()) or ())
    if not errors and isinstance(smoke, Mapping):
        errors = tuple(smoke.get("error_codes") or ())
    if errors:
        return f"smoke_error:{errors[0]}"

    win = metric_of(smoke, "win_rate", 0.0) or 0.0
    parent_win = metric_of(baseline, "win_rate", 0.0) or 0.0
    if win < parent_win - _SMOKE_WIN_RATE_MARGIN:
        return f"win_rate {win:.4f}<{parent_win:.4f}-{_SMOKE_WIN_RATE_MARGIN:.2f}"

    illegal = metric_of(smoke, "illegal_rate", 0.0) or 0.0
    if illegal > _SMOKE_MAX_ILLEGAL_RATE:
        return f"illegal_rate {illegal:.4f}>{_SMOKE_MAX_ILLEGAL_RATE:.2f}"

    fallback = metric_of(smoke, "fallback_rate", 0.0) or 0.0
    if fallback > _SMOKE_MAX_FALLBACK_RATE:
        return f"fallback_rate {fallback:.4f}>{_SMOKE_MAX_FALLBACK_RATE:.2f}"
    return None


# =====================================================================
# 判定表（§8.2；纯函数）
# =====================================================================

def condition_trace_entry(cond: str, passed: bool, detail: str, *,
                          value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"condition": str(cond), "passed": bool(passed), "detail": str(detail),
            "value": _plain(value), "threshold": _plain(threshold)}


def _trace(entries: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(e) for e in entries)


def first_failure(trace: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for entry in trace:
        if not entry.get("passed"):
            return entry
    return None


def _fail(cond: str, detail: str, *, code: str, cand: CandidateReport,
          generation: int, trace: list[dict[str, Any]], extra: Mapping[str, Any] | None = None
          ) -> Verdict:
    return Verdict(decision="reject", decided_by="auto",
                   reason=f"{cond}:{detail}", error_code=code,
                   condition_trace=_trace(trace), candidate_id=cand.candidate_id,
                   generation=int(generation))


def _incomplete(value: Any) -> bool:
    return value is None


def judge(cand_report: CandidateReport, parent_report: Any, *, cfg: Any,
          generation: int = 0) -> Verdict:
    """§8.2 判定表：短路求值，六项全 True → ``accept``，否则 ``reject``。

    ``parent_report`` 是 parent 的 heldout ``ArmResult``（含 ``metrics``）；
    ``cand_report.train`` 是候选在 train 侧的同口径指标（C3 用）。
    """
    cand = cand_report.candidate
    trace: list[dict[str, Any]] = []
    heldout = cand_report.heldout or {}
    smoke = cand_report.smoke or {}

    # ---- C0 前置完备 ----------------------------------------------------------------
    n = int(heldout.get("n", 0) or 0)
    min_n = int(getattr(cfg, "min_heldout_n", 20))
    key_fields = ("win_rate", "mean_stages_cleared", "illegal_rate", "fallback_rate",
                  "truncation_rate")
    missing = [f for f in key_fields if _incomplete(heldout.get(f))]
    c0_ok = n >= min_n and not missing
    trace.append(condition_trace_entry(
        "C0", c0_ok,
        f"heldout.n={n} min_n={min_n}" + (f" missing={missing}" if missing else ""),
        value=n, threshold=min_n))
    if not c0_ok:
        return _fail("C0", "incomplete_report", code=str(E.E_EVOLVE_JUDGE_FAILED),
                     cand=cand_report, generation=generation, trace=trace)

    parent_win = metric_of(parent_report, "win_rate", None)
    if parent_win is None:
        trace[-1] = condition_trace_entry("C0", False, "parent heldout 缺 win_rate")
        return _fail("C0", "incomplete_report", code=str(E.E_EVOLVE_JUDGE_FAILED),
                     cand=cand_report, generation=generation, trace=trace)

    # C5 的 truncation 是**相对门**（2026-10-09 B-44），因此 parent 的 truncation_rate
    # 与 win_rate 一样是**前置完备性**的一部分：缺它就无法求值 → fail-closed 记 C0，
    # 而不是静默跳过门（跳过会让 C5 对 truncation 失去诊断力）。
    parent_truncation = metric_of(parent_report, "truncation_rate", None)
    if parent_truncation is None:
        trace[-1] = condition_trace_entry("C0", False, "parent heldout 缺 truncation_rate")
        return _fail("C0", "incomplete_report", code=str(E.E_EVOLVE_JUDGE_FAILED),
                     cand=cand_report, generation=generation, trace=trace)

    # C5 自 2026-10-11（D-M3-2 裁决 E）起**三轴全为相对门**，因此 parent 的 illegal/fallback
    # 与 truncation 一样是**前置完备性**的一部分：缺任一项就无法求值 → fail-closed 记 C0。
    # 不得静默跳过该轴（跳过 = C5 对那一轴失去诊断力，正是本次要修的病）。
    parent_illegal = metric_of(parent_report, "illegal_rate", None)
    if parent_illegal is None:
        trace[-1] = condition_trace_entry("C0", False, "parent heldout 缺 illegal_rate")
        return _fail("C0", "incomplete_report", code=str(E.E_EVOLVE_JUDGE_FAILED),
                     cand=cand_report, generation=generation, trace=trace)

    parent_fallback = metric_of(parent_report, "fallback_rate", None)
    if parent_fallback is None:
        trace[-1] = condition_trace_entry("C0", False, "parent heldout 缺 fallback_rate")
        return _fail("C0", "incomplete_report", code=str(E.E_EVOLVE_JUDGE_FAILED),
                     cand=cand_report, generation=generation, trace=trace)

    # ---- C1 主指标不降（tol=0.05，D-3）---------------------------------------------
    win = float(heldout.get("win_rate") or 0.0)
    tol = float(getattr(cfg, "win_rate_tol", 0.05))
    c1_ok = win >= parent_win - tol
    trace.append(condition_trace_entry(
        "C1", c1_ok, f"win_rate {win:.4f} vs parent {parent_win:.4f} (tol={tol})",
        value=win, threshold=parent_win - tol))
    if not c1_ok:
        return _fail("C1", f"win_rate {win:.4f}<{parent_win - tol:.4f}",
                     code=str(E.E_EVOLVE_NO_GAIN), cand=cand_report,
                     generation=generation, trace=trace)

    # ---- C2 至少一指标显著提升 ------------------------------------------------------
    paired = cand_report.paired or {}
    metric = str(paired.get("metric", PRIMARY_METRIC))
    significant = bool(paired.get("significant", False))
    direction = str(paired.get("direction", "flat"))
    c2_ok = significant and direction == "up" and metric in _PRIMARY_CANDIDATES
    trace.append(condition_trace_entry(
        "C2", c2_ok,
        f"{metric} significant={significant} direction={direction} "
        f"delta={paired.get('delta')} p={paired.get('p')}",
        value=paired.get("delta"), threshold=str(getattr(cfg, "alpha", 0.05))))
    if not c2_ok:
        return _fail("C2", "no_significant_gain", code=str(E.E_EVOLVE_NO_GAIN),
                     cand=cand_report, generation=generation, trace=trace)

    # ---- C3 增益真实性 --------------------------------------------------------------
    gap = gap_of(cand_report.train, cand_report.heldout)
    gap_threshold = float(getattr(cfg, "gap_threshold", 0.15))
    c3_ok = gap <= gap_threshold
    trace.append(condition_trace_entry(
        "C3", c3_ok, f"win_rate_gap {gap:.4f} vs {gap_threshold}",
        value=gap, threshold=gap_threshold))
    if not c3_ok:
        return _fail("C3", f"gap {gap:.4f}>{gap_threshold:.4f}",
                     code=str(E.E_EVOLVE_OVERFIT_SUSPECT), cand=cand_report,
                     generation=generation, trace=trace)

    # ---- C4 无坍缩 ------------------------------------------------------------------
    diversity = cand_report.diversity or {}
    collapse = bool(diversity.get("collapse_flag", False))
    slope = diversity.get("entropy_slope", None)
    min_slope = float(getattr(cfg, "min_entropy_slope", -0.01))
    slope_ok = slope is not None and float(slope) >= min_slope
    c4_ok = (not collapse) and slope_ok
    trace.append(condition_trace_entry(
        "C4", c4_ok, f"collapse_flag={collapse} entropy_slope={slope} min={min_slope}",
        value=slope, threshold=min_slope))
    if not c4_ok:
        return _fail("C4", "collapse", code=str(E.E_EVOLVE_COLLAPSE), cand=cand_report,
                     generation=generation, trace=trace)

    # ---- C5 合法性与健壮性 ----------------------------------------------------------
    # 2026-10-11（D-M3-2 裁决 E，用户）：三轴统一为 **相对门 + 绝对 backstop**，判定式
    #   `candidate ≤ min(parent + tol, backstop)`。
    # 为什么要改：M2 出于「先松开」把这它留成绝对门（0.35 / 0.25），而 `C1` 实测
    #   illegal 0.0318 / fallback 0.0017 ⇒ 松 11× / 147×，**等于非约束门**。后果不是
    #   「少抓一个坏候选」，而是**判据 2 的归因结论不成立**（候选完全可以靠推高非法率
    #   换 win_rate 而照样过闸）。truncation 早在 2026-10-09（B-44/F28）就是相对门了。
    # 为什么保留 backstop：相对门单独用时有个已知放宽——parent 自身 0.90 时，候选 0.60
    #   也能过（§8.2.2）。backstop 只用于堵这个洞，不替代相对门。
    # tol 语义按轴分列：illegal/fallback 是**步级**（分母 ≈12k / ≈3.7k），
    #   truncation 是**局级**（粒度 1/40 = 0.025）。见 `infra/config.py` 的字段注释。
    illegal = float(heldout.get("illegal_rate") or 0.0)
    fallback = float(heldout.get("fallback_rate") or 0.0)
    truncation = float(heldout.get("truncation_rate") or 0.0)
    # 兜底值沿用 M2 标定的绝对门（`max_illegal_rate`/`max_fallback_rate`）——它们**仍在
    # config 里**，但语义从「主门」降为「backstop」，因此 `hasattr` 检查保持不变。
    illegal_backstop = float(getattr(cfg, "max_illegal_rate", _C5_BACKSTOP))
    fallback_backstop = float(getattr(cfg, "max_fallback_rate", _C5_BACKSTOP))
    illegal_tol = float(getattr(cfg, "illegal_tol", _C5_ILLEGAL_TOL))
    fallback_tol = float(getattr(cfg, "fallback_tol", _C5_FALLBACK_TOL))
    truncation_tol = float(getattr(cfg, "truncation_tol", _C5_TRUNCATION_TOL))
    truncation_backstop = float(
        getattr(cfg, "truncation_backstop", _C5_TRUNCATION_BACKSTOP))
    illegal_max = min(float(parent_illegal) + illegal_tol, illegal_backstop)
    fallback_max = min(float(parent_fallback) + fallback_tol, fallback_backstop)
    truncation_max = min(float(parent_truncation) + truncation_tol, truncation_backstop)
    illegal_ok = illegal <= illegal_max
    fallback_ok = fallback <= fallback_max
    truncation_ok = truncation <= truncation_max
    c5_ok = illegal_ok and fallback_ok and truncation_ok
    trace.append(condition_trace_entry(
        "C5", c5_ok,
        f"illegal={illegal:.4f}≤min(parent {float(parent_illegal):.4f}+tol {illegal_tol}, "
        f"backstop {illegal_backstop}) "
        f"fallback={fallback:.4f}≤min(parent {float(parent_fallback):.4f}+tol "
        f"{fallback_tol}, backstop {fallback_backstop}) "
        f"truncation={truncation:.4f}≤min(parent {float(parent_truncation):.4f}+tol "
        f"{truncation_tol}, backstop {truncation_backstop})",
        value={"illegal_rate": illegal, "fallback_rate": fallback,
               "truncation_rate": truncation,
               "parent_illegal_rate": float(parent_illegal),
               "parent_fallback_rate": float(parent_fallback),
               "parent_truncation_rate": float(parent_truncation)},
        threshold={"max_illegal_rate": illegal_max,
                   "max_fallback_rate": fallback_max,
                   "max_truncation_rate": truncation_max,
                   "illegal_tol": illegal_tol, "fallback_tol": fallback_tol,
                   "truncation_tol": truncation_tol,
                   "illegal_backstop": illegal_backstop,
                   "fallback_backstop": fallback_backstop,
                   "truncation_backstop": truncation_backstop}))
    if not c5_ok:
        return _fail("C5", "illegal/fallback/truncation",
                     code=str(E.E_EVOLVE_ILLEGAL_RATE), cand=cand_report,
                     generation=generation, trace=trace)

    return Verdict(decision="accept", decided_by="auto", reason="",
                   error_code=None, condition_trace=_trace(trace),
                   candidate_id=cand_report.candidate_id, generation=int(generation))


def better(a: CandidateReport, b: CandidateReport) -> bool:
    """§8.2：C2 ``delta`` 大者优先；并列时 ``candidate_id`` 小者优先（确定性）。"""
    delta_a = float((a.paired or {}).get("delta", 0.0) or 0.0)
    delta_b = float((b.paired or {}).get("delta", 0.0) or 0.0)
    if delta_a != delta_b:
        return delta_a > delta_b
    return a.candidate_id < b.candidate_id


def pick_winner(reports: Sequence[CandidateReport]) -> CandidateReport | None:
    """多候选全通过时取 ``(C2 delta 最大, candidate_id 最小)``（§8.2）。"""
    accepted = [r for r in reports
                if r.verdict is not None and r.verdict.decision == "accept"]
    if not accepted:
        return None
    winner = accepted[0]
    for cand in accepted[1:]:
        if better(cand, winner):
            winner = cand
    return winner


def verdict_for_winner(report: CandidateReport, *, generation: int) -> Verdict:
    """``accept`` 判词（已有 verdict 时原样返回，只补 generation）。"""
    if report.verdict is not None:
        return report.verdict
    return Verdict(decision="accept", decided_by="auto", reason="",
                   candidate_id=report.candidate_id, generation=int(generation))


def superseded_verdict(report: CandidateReport, winner: CandidateReport, *,
                       generation: int) -> Verdict:
    """多候选同时通过时的落选判词（``reject/reason="superseded"``，不计失败、无错误码）。"""
    return Verdict(decision="reject", decided_by="auto", reason="superseded",
                   error_code=None,
                   condition_trace=(condition_trace_entry(
                       "W", False,
                       f"superseded by {winner.candidate_id}", value=None,
                       threshold=None),),
                   candidate_id=report.candidate_id, generation=int(generation))


# =====================================================================
# 评估编排（FR-07-6 / FR-07-7；只编排 MOD-08）
# =====================================================================

@dataclass(frozen=True)
class CandidateEvaluator:
    """MOD-08 的注入面（默认实现走 ``dice_ratchet.evals``）。

    ``protocol`` / ``config`` / ``memory_version`` / ``run_id`` / ``generation`` /
    ``smoke_seeds`` / ``runner`` 都由编排层固定，保证 parent 与候选**同预算**（FR-07-7）。
    """

    protocol: Any = None
    config: Any = None
    memory_version: int = 0
    run_id: str = ""
    generation: int = 0
    smoke_seeds: tuple[int, ...] = ()
    heldout_basket: str = HOLDOUT_BASKET
    runner: Any = None
    episode_config: Any = None
    harness_version_of: Callable[[Any], int] | None = None
    #: F42-A/B：run 根目录（臂归档 / 检查点落点）。为空时由 ``evaluate_arm`` 按
    #: ``config.infra.runs_root / run_id`` 推导。
    run_dir: Any = None

    def heldout_seeds(self, generation: int | None = None) -> tuple[int, ...]:
        """本代 held-out 篮子的 seed 列表（F57 继承资格检查用；**不跑任何局**）。

        与 ``_evaluate_arm`` 走的解析链一致（``evals.seeds.resolve_basket``），
        所以「继承来的局是否覆盖本代篮子」这个判断问的是**同一个篮子**。
        """
        from dice_ratchet.evals import seeds as seeds_mod

        proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(self.protocol))
        gen = int(self.generation if generation is None else generation)
        return tuple(int(s) for s in
                     seeds_mod.resolve_basket(self.heldout_basket, generation=gen,
                                              protocol=proto))

    def eval_limits(self) -> dict[str, Any]:
        """还原历史 ``ArmResult`` 需要的聚合参数（``t_max`` / ``min_support`` / ``drop_rate_max``）。

        F57：继承来的逐局结果必须用**同一套聚合口径**重算，否则「继承」会悄悄改变测量
        结果（与 ``_evaluate_arm`` 里对齐 ``n_workers``/``n_rep`` 同一条纪律）。
        """
        eval_cfg = getattr(self.config, "evals", None)
        return {"t_max": int(getattr(eval_cfg, "t_max", 120) or 120),
                "min_support": int(getattr(eval_cfg, "min_support", 3) or 3),
                "drop_rate_max": float(getattr(eval_cfg, "drop_rate_max", 0.10) or 0.10)}

    def _evaluate_arm(self, arm_dir: Any, *, split: str, basket: Any, arm: str,
                      harness_version: int) -> Any:
        from dice_ratchet.evals import evaluate_arm

        kwargs: dict[str, Any] = {}
        if self.runner is not None:
            # F66（2026-10-14）：**把本臂的 harness 目录绑到 runner 上**。
            # 原先只传 ``runner``，而 ``evaluate_arm`` 的
            # ``executor = runner or _default_runner(harness_dir=…)`` 会在注入 runner 时
            # **完全忽略 harness_dir** ⇒ 候选臂跑的是 run 级 ``harness/``（未打补丁），
            # 三次候选评估实测与 parent 的 ``prompt_hash`` 逐字节相同（40/40）。
            # 只有**候选臂**需要显式绑目录：parent / parent-smoke 用的就是 run 级
            # harness（组合根的策略工厂默认值），绑它只会给不支持该参数的 runner
            # （测试替身等）添麻烦，语义上也多余。
            if str(arm).startswith("cand-"):
                kwargs["runner"] = _wrap_runner(self.runner, harness_dir=arm_dir)
                self._assert_patch_reaches_prompt(arm=arm, arm_dir=Path(arm_dir))
            else:
                kwargs["runner"] = _wrap_runner(self.runner)
        # F42-C：原先**没传** ``n_workers``/``n_rep`` ⇒ 落到 ``evaluate_arm`` 的默认
        # ``n_workers=1``，即一代 84 局**串行**跑；而 gen-000 基线路径传的是
        # ``cfg.evals.n_workers=2``（``loop/generation.py``）⇒ 同一份配置下两条路径的
        # 并发度不同，候选/parent 臂的墙钟与 gen-000 不可比。这里按配置对齐；
        # **不改任何配置值**，所以 ``config_hash`` 不变。
        eval_cfg = getattr(self.config, "evals", None)
        kwargs["n_workers"] = int(getattr(eval_cfg, "n_workers", 1) or 1)
        kwargs["n_rep"] = int(getattr(getattr(eval_cfg, "determinism", None), "n_rep", 1) or 1)
        if self.run_dir is not None: # F42-A：臂归档落在真实 run 目录
            kwargs["run_dir"] = self.run_dir
        return evaluate_arm(
            arm_dir, split=split, basket=basket, memory_version=int(self.memory_version),
            arm=arm, run_id=self.run_id, protocol=self.protocol, config=self.config,
            generation=int(self.generation), harness_version=int(harness_version),
            episode_config=self.episode_config, **kwargs)

    def _assert_patch_reaches_prompt(self, *, arm: str, arm_dir: Path) -> None:
        """F66 护栏（候选臂专用）：**play 模板必须真的从本臂目录解析出来**。

        判据（不满足即 fail-closed ``E_EVOLVE_PATCH_INVALID``，**不花钱**）：
        ``resolve_path(cfg.agent.policy.template_path, harness_dir=arm_dir)`` 的落点
        必须在 ``arm_dir`` 之内。修复前它会落到 CWD 的仓库模板（F66 现场：候选臂
        与 parent 的首个决策点 ``prompt_hash`` 逐字节相同、40/40）。

        **诚实边界**：本条只证明「加载器会读本臂的模板」，不能证明「模型真的看到了
        补丁」——后者由两处覆盖：① ``tests/unit/evolve/test_f66_*`` 的回归用例（假
        LLM 断言收到的 messages 含补丁行）；② 后续把 ``play_hash`` 作为继承判据时
        的「候选 play_hash ≠ parent play_hash」断言（F63/F66 的第二半）。
        """
        from dice_ratchet.agent.prompts.assembler import resolve_path

        policy_cfg = getattr(getattr(self.config, "agent", None), "policy", None)
        template_path = str(getattr(policy_cfg, "template_path", "") or "")
        if not template_path:
            return
        try:
            resolved = resolve_path(template_path, harness_dir=arm_dir).resolve()
            inside = resolved.is_relative_to(Path(arm_dir).resolve())
        except Exception as exc: # noqa: BLE001 - 解析失败一律按「补丁没生效」处理
            raise InfraError(E.E_EVOLVE_PATCH_INVALID,
                             f"{arm}: 无法解析 play 模板（{template_path}）：{exc}",
                             hint="检查 staging 布局（<staging>/<id>/prompts/decision.md）"
                             ) from exc
        if not inside:
            raise InfraError(
                E.E_EVOLVE_PATCH_INVALID,
                f"{arm}: play 模板落点不在本臂目录内（{resolved} 不属于 {Path(arm_dir)}）"
                " —— 候选会用 run 级模板，补丁不会生效（F66）",
                hint="resolve_path 必须让 harness_dir 优先于 CWD；"
                     "并确认 staging 布局镜像 harness 根")

    def parent_arms(self, parent_dir: Any, *, parent_version: int,
                    inherited: Any = None) -> tuple[Any, Any]:
        """parent 的 heldout + 冒烟。

        **F57（2026-10-12）——``inherited``：跨代继承。** 原先这里**每代都重测**
        parent（``parent 每代都要重新评估``）：一代 20 局 heldout + 2 局冒烟 ≈ ¥18.7，
        而**上一代被接受的那个候选就是本代的 parent**（accept ⇒ ``versions/v{N}``）——
        它刚刚**已经**被完整测过一次（那批局就是 accept 决策的依据）。重测同一份
        harness 只是再买一次同一批局（API 臂不可复现 ⇒ 还会得到略微不同的数字）。

        ``inherited`` 为**已还原的 parent ``ArmResult``**（见
        ``harness.restore_arm_from_archive``）时，heldout **一次 ``evaluate_arm`` 都不调**。

        **冒烟为什么仍然重测**：冒烟 seed 取的是**本代训练块末尾**（FR-07-6），
        逐代不同；而候选的冒烟跑在本代的 seed 上 ⇒ 「parent 与候选同预算」（FR-07-7）
        要求 parent 的冒烟也落在**同一批 seed** 上。冒烟只有 ``smoke_n``（默认 2）局，
        省它的收益 ≈¥1.7 而会破坏快速淘汰的配对前提，不划算。
        """
        if inherited is not None:
            heldout = inherited
        else:
            heldout = self._evaluate_arm(parent_dir, split="heldout",
                                        basket=self.heldout_basket, arm="parent",
                                        harness_version=int(parent_version))
        smoke = self._evaluate_arm(parent_dir, split="train", basket=self.smoke_seeds,
                                   arm="parent-smoke", harness_version=int(parent_version))
        return heldout, smoke

    def candidate_arms(self, staging_dir: Any, *, candidate: Candidate,
                       harness_version: int) -> tuple[Any, Any, dict[str, Any]]:
        """候选的冒烟 + heldout + 配对检验（FR-07-6/7/8）。"""
        from dice_ratchet.evals import compare_paired

        arm = f"cand-{candidate.candidate_id}"
        smoke = self._evaluate_arm(staging_dir, split="train", basket=self.smoke_seeds,
                                   arm=f"{arm}-smoke", harness_version=int(harness_version))
        heldout = self._evaluate_arm(staging_dir, split="heldout",
                                     basket=self.heldout_basket, arm=arm,
                                     harness_version=int(harness_version))
        return smoke, heldout, {}


def _wrap_runner(runner: Any, *, harness_dir: Any = None) -> Any:
    """MOD-06 ``EpisodeRunner`` → MOD-08 ``EpisodeRunner`` 协议适配器（懒惰 import）。

    ``harness_dir``（F66）：本臂要用的 harness 目录（候选臂 = staging 拷贝）。
    适配器会在每局把它透传给 ``EpisodeRunner``，从而让 MOD-04 用**该臂自己的**模板
    装配 prompt —— 不传就等于让候选臂用 run 级（未打补丁）模板。
    """
    from dice_ratchet.loop.runner import EvalRunnerAdapter

    return EvalRunnerAdapter(runner, harness_dir=harness_dir)


def evaluate_candidate(candidate: Candidate, parent: Any, *, cfg: Any,
                       staging_dir: Any, evaluator: CandidateEvaluator | None = None,
                       parent_heldout: Any = None, parent_smoke: Any = None,
                       harness_version: int = 0, generation: int = 0,
                       smoke_seeds: Sequence[int] = ()) -> CandidateReport:
    """冒烟 → 快速淘汰 → heldout → 配对检验（FR-07-6/7）。

    ``parent`` 是 parent 的 ``HarnessRef``（取 ``.version`` 与 ``.dir``）；
    ``parent_heldout`` / ``parent_smoke`` 由编排层**每代重算一次**并复用（NFR-07-5 成本）。
    """
    started = time.perf_counter()
    ev = evaluator or CandidateEvaluator(config=cfg, generation=int(generation))
    version = int(harness_version)
    seeds = tuple(int(s) for s in (smoke_seeds or ev.smoke_seeds))

    try:
        if parent_smoke is None or parent_heldout is None:
            parent_heldout, parent_smoke = _resolve_parent(
                ev, parent, parent_heldout=parent_heldout, parent_smoke=parent_smoke)
        smoke, heldout, _unused = ev.candidate_arms(staging_dir, candidate=candidate,
                                                    harness_version=version)
    except InfraError as exc:
        events.emit("evolve.evaluate", candidate_id=candidate.candidate_id,
                    error_code=str(exc.code))
        return CandidateReport(candidate=candidate, error_code=str(exc.code),
                               reason=str(exc.code),
                               elapsed_s=time.perf_counter() - started)
    except Exception as exc: # noqa: BLE001 - NFR-07-7：裁判链路异常一律保守 reject
        events.emit("evolve.evaluate", candidate_id=candidate.candidate_id,
                    error_code=str(E.E_EVOLVE_JUDGE_FAILED))
        return CandidateReport(candidate=candidate,
                               error_code=str(E.E_EVOLVE_JUDGE_FAILED),
                               reason=f"{type(exc).__name__}: {exc}",
                               elapsed_s=time.perf_counter() - started)

    smoke_dict = _metrics_dict(smoke)
    events.emit("evolve.smoke", candidate_id=candidate.candidate_id,
                win_rate=float(smoke_dict.get("win_rate", 0.0) or 0.0),
                n=int(smoke_dict.get("n", 0) or 0),
                illegal_rate=float(smoke_dict.get("illegal_rate", 0.0) or 0.0),
                elapsed_s=round(time.perf_counter() - started, 4))

    fast_reject = smoke_fast_reject(smoke, baseline=parent_smoke, cfg=cfg)
    if fast_reject:
        events.emit("evolve.smoke", candidate_id=candidate.candidate_id,
                    win_rate=float(smoke_dict.get("win_rate", 0.0) or 0.0),
                    fast_reject_reason=fast_reject)
        return CandidateReport(
            candidate=candidate, smoke=smoke_dict, heldout=None,
            error_code=str(E.E_EVOLVE_SMOKE_FAILED),
            reason=f"{E.E_EVOLVE_SMOKE_FAILED}:{fast_reject}",
            elapsed_s=time.perf_counter() - started)

    try:
        paired = _paired_dict(_compare_paired(heldout, parent_heldout),
                              alpha=float(getattr(cfg, "alpha", 0.05)))
    except InfraError as exc:
        events.emit("evolve.evaluate", candidate_id=candidate.candidate_id,
                    error_code=str(exc.code))
        return CandidateReport(candidate=candidate, smoke=smoke_dict,
                               heldout=_metrics_dict(heldout),
                               error_code=str(exc.code), reason=str(exc.code),
                               elapsed_s=time.perf_counter() - started)
    report = CandidateReport(candidate=candidate, smoke=smoke_dict,
                             heldout=_metrics_dict(heldout), paired=paired,
                             train=smoke_dict,
                             diversity=_diversity_dict(heldout),
                             elapsed_s=time.perf_counter() - started)
    verdict = judge(report, parent_heldout, cfg=cfg, generation=int(generation))
    report = CandidateReport(candidate=report.candidate, smoke=report.smoke,
                             heldout=report.heldout, paired=report.paired,
                             train=report.train, diversity=report.diversity,
                             verdict=verdict, elapsed_s=report.elapsed_s,
                             error_code=verdict.error_code,
                             reason=verdict.reason)
    events.emit("evolve.evaluate", candidate_id=candidate.candidate_id,
                win_rate=float((report.heldout or {}).get("win_rate", 0.0) or 0.0),
                delta=paired.get("delta"), p_value=paired.get("p"),
                episodes_spent=int((report.heldout or {}).get("n", 0) or 0)
                + int(smoke_dict.get("n", 0) or 0))
    return report


def _resolve_parent(ev: CandidateEvaluator, parent: Any, *, parent_heldout: Any,
                    parent_smoke: Any) -> tuple[Any, Any]:
    if parent_heldout is not None and parent_smoke is not None:
        return parent_heldout, parent_smoke
    parent_dir = getattr(parent, "dir", None) or parent
    parent_version = int(getattr(parent, "version", 0) or 0)
    return ev.parent_arms(parent_dir, parent_version=parent_version)


def _compare_paired(candidate_arm: Any, parent_arm: Any) -> Any:
    """C2 的配对检验**唯一**来源（FR-07-7：MOD-07 不做统计）。"""
    from dice_ratchet.evals import compare_paired

    return compare_paired(candidate_arm, parent_arm)


def _metrics_dict(result: Any) -> dict[str, Any]:
    """``ArmResult`` → ``candidates/{id}.json`` 的 ``report`` 段（MOD-08 字段面）。"""
    if result is None:
        return {}
    bag = dict(_metric_bag(result))
    bag.setdefault("seeds", list(getattr(result, "seeds", ()) or ()))
    bag.setdefault("arm", getattr(result, "arm", ""))
    bag.setdefault("harness_version", int(getattr(result, "harness_version", 0) or 0))
    bag.setdefault("memory_version", int(getattr(result, "memory_version", 0) or 0))
    return bag


def _paired_dict(paired: Any, *, alpha: float) -> dict[str, Any]:
    if paired is None:
        return {}
    if isinstance(paired, Mapping):
        out = dict(paired)
    else:
        as_dict = getattr(paired, "as_dict", None)
        out = dict(as_dict()) if callable(as_dict) else {"value": str(paired)}
    out.setdefault("alpha", float(alpha))
    return out


def _diversity_dict(result: Any) -> dict[str, Any]:
    """候选的坍缩相关指标（C4 用）。

    MOD-08 的 ``SplitMetrics`` 只带 ``action_entropy_curve``（坡度与 ``collapse_flag``
    由 ``collapse_check`` 在**代际**层面产出）。候选评估没有历史曲线，因此这里取
    MOD-08 的 ``entropy_slope`` 纯函数直接算坡度；``entropy_slope`` / ``collapse_flag``
    若由上游显式给出（例如测试替身或未来的 MOD-08 扩展）则优先采用。
    """
    if result is None:
        return {}
    bag = _metric_bag(result)
    slope = bag.get("entropy_slope", None)
    if slope is None:
        curve = bag.get("action_entropy_curve") or ()
        slope = _slope_of(curve)
    return {"entropy_slope": slope,
            "collapse_flag": bool(bag.get("collapse_flag", False)),
            "jsd_kind": bag.get("jsd_kind", None),
            "entropy_mean_norm": bag.get("entropy_mean_norm", None)}


def _slope_of(curve: Sequence[Any]) -> float | None:
    """调用 MOD-08 的 ``entropy_slope``（MOD-07 不自己实现统计口径）。"""
    if not curve:
        return None
    try:
        from dice_ratchet.evals.diversity import entropy_slope

        return float(entropy_slope(list(curve)))
    except Exception: # noqa: BLE001 - 曲线缺失/形态异常：C4 按"不可判"处理
        return None


def smoke_failure_reason(report: CandidateReport) -> str:
    """从候选报告里取冒烟淘汰原因（若有）。"""
    if report.error_code == str(E.E_EVOLVE_SMOKE_FAILED):
        return report.reason or str(E.E_EVOLVE_SMOKE_FAILED)
    raise SmokeFailedError("该候选未被冒烟淘汰", hint="调用方应先判断 error_code")
