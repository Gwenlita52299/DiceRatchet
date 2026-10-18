"""MOD-07 候选评估编排与接受判定表（PRD FR-07-6 … FR-07-8；§8.2）。

两件事，职责**严格分开**：

``evaluate_candidate()`` —— 只编排，不做统计
    冒烟（``split="train"``，本代训练块末尾 ``smoke_n`` 个 seed）→ 快速淘汰 →
    heldout（``evolve.heldout_seeds``，默认 40 局）→ ``compare_paired``。
    指标全部委托 MOD-08 ``evaluate_arm`` / ``compare_paired``（NFR-07-6）。
    parent 与候选必须使用**同一个冻结的记忆快照** ``memory_version``（FR-07-7）。

``judge()`` —— 判定表（§8.2 六项，短路求值）
    ``accept ⟺ C0∧C1∧C2∧C3∧C4∧C5``；否则 ``reject`` 且 ``reason`` 含**首个失败条件 id**
    （如 ``C1:win_rate 0.20<0.25``、``C2:no_play_change``）。逐条写 ``condition_trace``。

    **C2 自 2026-10-14（F62，用户裁决 B）起改判**：由「至少一指标**显著提升**」改为
    「候选的 **play 面指纹确实变了**」（确定性、¥0）。统计量（显著/不显著/方向）**不再
    阻断**，只落 ``condition_trace`` 与 ``paired``，供判据 2 复核。为什么改：M3 正式
    实验实测三代全部止步 C2（``no_significant_gain``），而 n=40 上 C2 的等效门槛是
    「净 +15 pp / 0.45 关」，实际效应只有 +2.5–7.5 pp / +0.05–0.325 关 ⇒ **门没有功效**
    （不是候选不够好）。为什么必须补一道门而不是删掉 C2：零改动候选若被 accept，它在
    判据 1 里因 ``play_hash`` 未变而落回 ``not_applicable`` ⇒「≥1 次 accept」第二次
    变成**退化通过**（B-66/D-M3-4 立这条就是为了防这个）。

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
from .versioning import PLAY_CONFIG_FILE, PLAY_PROMPT_FILE

__all__ = [
    "CONDITION_IDS", "DEFAULT_SMOKE_BASKET", "HOLDOUT_BASKET", "PRIMARY_METRIC",
    "CandidateEvaluator", "better", "condition_trace_entry", "evaluate_candidate",
    "first_failure", "gap_of", "judge", "metric_of", "pick_winner",
    "smoke_failure_reason", "smoke_fast_reject", "smoke_seeds_for",
    "superseded_verdict", "verdict_for_winner",
]

#: 判定表条件 id（§8.2；顺序即短路顺序）。
CONDITION_IDS: tuple[str, ...] = ("C0", "C1", "C2", "C3", "C4", "C5")

#: **报告项**指标集（方法归 MOD-08 ``compare_paired``）。
#:
#: 2026-10-14（F62）：C2 取消统计门后，这组指标**不再参与裁决**，只作为判据 2 的证据
#: 逐项落在 ``paired["by_metric"]`` 里（``compare_all_metrics`` 全跑一遍）。
_REPORTED_METRICS: tuple[str, ...] = ("win_rate", "mean_stages_cleared",
                                      "mean_hearts_left")
PRIMARY_METRIC: str = "win_rate"
#: 多候选排序的指标优先级（F62）：**连续轴优先**——D-3/B-39 把 4–5 pp 量级的信号
#: 放在 ``mean_stages_cleared`` 上，而 ``win_rate`` 在 n=40 上只能判 ≥15 pp。
RANKING_METRICS: tuple[str, ...] = ("mean_stages_cleared", "win_rate")

#: heldout 篮子（Spec B-07：H1=代际裁判）。
HOLDOUT_BASKET: str = "H1"
DEFAULT_SMOKE_BASKET: str = "train"

#: 冒烟快速淘汰阈值（FR-07-6）。
#:
#: **F67（2026-10-14，用户裁决）**：原先还有一条 ``_SMOKE_WIN_RATE_MARGIN = 0.2`` 的
#: 胜率轴，**已删除**。理由：冒烟 ``smoke_n=2`` ⇒ 胜率分辨率是 0.5，而 0.2 的 margin
#: **结构上不可达**（差值只能是 0 / 0.5 / 1.0）——它实际等价于一条没写清楚的硬条件
#: （「parent 全胜且候选至多赢 1 局」），并附带 2–3% 的等价候选误杀。冒烟的定位是
#: **triage 而不是证据**（花 ≈¥1.7 换掉 ≈¥21 的 40 局），因此只保留**步级**统计的
#: 安全硬门：非法率/fallback 的分母是上千次尝试，2 局也有诊断力；局级胜率的诊断力
#: 归 C1（40 局）。
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

    - harness 运行期异常（``ArmResult.error_codes`` 非空）；
    - 非法率 > 0.30；
    - fallback 率 > 0.30。

    **F67（2026-10-14）**：胜率轴已删除（n=2 分辨率 0.5 > margin 0.2，无诊断力）；
    ``baseline`` 仍作为参数保留（契约不变，且未来若加回步级轴仍需要它）。
    """
    if smoke is None:
        return "smoke_missing"
    errors = tuple(getattr(smoke, "error_codes", ()) or ())
    if not errors and isinstance(smoke, Mapping):
        errors = tuple(smoke.get("error_codes") or ())
    if errors:
        return f"smoke_error:{errors[0]}"

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
          generation: int = 0, play_hash: str = "", parent_play_hash: str = "",
          effective_hash: str = "", parent_effective_hash: str = "") -> Verdict:
    """§8.2 判定表：短路求值，六项全 True → ``accept``，否则 ``reject``。

    ``parent_report`` 是 parent 的 heldout ``ArmResult``（含 ``metrics``）；
    ``cand_report.train`` 是候选在 train 侧的同口径指标（C3 用）。

    ``play_hash`` / ``parent_play_hash``（F62，2026-10-14）：候选 staging 目录与起点
    harness 的 **play 面指纹**（``versioning.play_hash``）。C2 的判据是「两者不同」。
    **fail-closed**：任一为空 ⇒ C2 不通过（``C2:play_hash_unavailable``）——「算不出
    指纹」绝不能静默变成「打法变了」。
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

    # ---- C2 play 面确实变了（F62，2026-10-14；取代「至少一指标显著提升」）--------------
    # 统计量仍在算、仍在记（`paired` / `condition_trace`），但**不再阻断**：
    # 「显著提升」这条门在 n=40 上没有功效（实测三代全灭，等效门槛 +15 pp / 0.45 关），
    # 而「补丁是否真的改变了打法」是**确定性**的、不需要任何额外预算，且正是判据 1
    # 能继续可判的前提（零改动的 accept 会让下一代落回 `not_applicable`）。
    paired = cand_report.paired or {}
    metric = str(paired.get("metric", PRIMARY_METRIC))
    significant = bool(paired.get("significant", False))
    direction = str(paired.get("direction", "flat"))
    stats = (f"{metric} significant={significant} direction={direction} "
             f"delta={paired.get('delta')} p={paired.get('p')}")
    ph = str(play_hash or "")
    prev_ph = str(parent_play_hash or "")
    eff = str(effective_hash or "")
    prev_eff = str(parent_effective_hash or "")
    if not ph or not prev_ph:
        c2_ok = False
        c2_reason = "play_hash_unavailable"
        c2_detail = (f"play 面指纹不可用（候选={ph or '<missing>'} 起点={prev_ph or '<missing>'}）；"
                     f"{stats}")
    elif ph == prev_ph:
        c2_ok = False
        c2_reason = "no_play_change"
        c2_detail = (f"play 面未变（{ph[:19]}）：补丁没有改变打法；{stats}")
    elif eff and prev_eff and eff == prev_eff:
        # F-M3-9（方案 B）：字节变了、**生效值**没变 ⇒ 不是真的改变打法。
        c2_ok = False
        c2_reason = "no_effective_change"
        c2_detail = (f"play 面字节已变（{prev_ph[:19]} → {ph[:19]}）但生效采样值未变"
                     f"（{prev_eff[:19]}）：改的键在当前 mode 下不生效；{stats}")
    else:
        c2_ok = True
        c2_reason = ""
        c2_detail = f"play 面已变：{prev_ph[:19]} → {ph[:19]}；{stats}"
    trace.append(condition_trace_entry(
        "C2", c2_ok, c2_detail,
        value={"candidate_play_hash": ph, "parent_play_hash": prev_ph,
               "candidate_effective_hash": eff, "parent_effective_hash": prev_eff,
               "metric": metric, "significant": significant, "direction": direction,
               "delta": paired.get("delta"), "p": paired.get("p")},
        threshold="play_hash != parent_play_hash 且 effective_play_hash 变化"))
    if not c2_ok:
        return _fail("C2", c2_reason, code=str(E.E_EVOLVE_NO_GAIN),
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


def _delta_of(report: CandidateReport, metric: str) -> float:
    """候选相对起点的某指标 delta（**优先** ``paired["by_metric"]``，回落主口径）。"""
    paired = report.paired or {}
    bag = paired.get("by_metric")
    if isinstance(bag, Mapping):
        item = bag.get(metric)
        if isinstance(item, Mapping) and item.get("delta") is not None:
            try:
                return float(item["delta"])
            except (TypeError, ValueError): # pragma: no cover - 形态异常按 0 处理
                return 0.0
    if str(paired.get("metric") or "") == metric and paired.get("delta") is not None:
        try:
            return float(paired["delta"])
        except (TypeError, ValueError): # pragma: no cover
            return 0.0
    return 0.0


def better(a: CandidateReport, b: CandidateReport) -> bool:
    """§8.2 的候选排序（**F62 改：连续轴优先**）。

    原先按「C2 的 ``win_rate`` delta 最大者优先」。C2 取消统计门之后仍需要一个确定性
    排序（多候选全部通过时选谁进 HITL/accept），主轴换成**分辨率更高的连续轴**：
    ``mean_stages_cleared`` delta → ``win_rate`` delta → ``candidate_id`` 小者优先。
    """
    for metric in RANKING_METRICS:
        delta_a, delta_b = _delta_of(a, metric), _delta_of(b, metric)
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
                      harness_version: int, play_hash: str = "") -> Any:
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
            episode_config=self.episode_config, play_hash=str(play_hash or ""), **kwargs)

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
                    inherited: Any = None, play_hash: str = "") -> tuple[Any, Any]:
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

        ``play_hash``（B-92 / F63）：起点 harness 的 play 面指纹，写进臂归档 provenance
        ——它是「这份测量测的是哪份 play 面」的唯一凭据（版本号在候选臂上是 accept 之前的
        号，不可用）。由编排层算好后传入，本类不碰文件系统。
        """
        if inherited is not None:
            heldout = inherited
        else:
            heldout = self._evaluate_arm(parent_dir, split="heldout",
                                        basket=self.heldout_basket, arm="parent",
                                        harness_version=int(parent_version),
                                        play_hash=play_hash)
        smoke = self._evaluate_arm(parent_dir, split="train", basket=self.smoke_seeds,
                                   arm="parent-smoke", harness_version=int(parent_version),
                                   play_hash=play_hash)
        return heldout, smoke

    def candidate_arms(self, staging_dir: Any, *, candidate: Candidate,
                       harness_version: int,
                       play_hash: str = "") -> tuple[Any, Any, dict[str, Any]]:
        """候选的冒烟 + heldout + 配对检验（FR-07-6/7/8）。

        ``play_hash``（B-92 / F63）：**候选 staging 拷贝**的 play 面指纹——它才是这份
        测量的身份（``harness_version`` 记的是 accept 之前的在役版本号）。
        """
        from dice_ratchet.evals import compare_paired

        arm = f"cand-{candidate.candidate_id}"
        smoke = self._evaluate_arm(staging_dir, split="train", basket=self.smoke_seeds,
                                   arm=f"{arm}-smoke", harness_version=int(harness_version),
                                   play_hash=play_hash)
        heldout = self._evaluate_arm(staging_dir, split="heldout",
                                     basket=self.heldout_basket, arm=arm,
                                     harness_version=int(harness_version),
                                     play_hash=play_hash)
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
                       smoke_seeds: Sequence[int] = (),
                       play_hash: str = "", parent_play_hash: str = "",
                       effective_hash: str = "",
                       parent_effective_hash: str = "") -> CandidateReport:
    """冒烟 → 快速淘汰 → heldout → 配对检验（FR-07-6/7）。

    ``parent`` 是 parent 的 ``HarnessRef``（取 ``.version`` 与 ``.dir``）；
    ``parent_heldout`` / ``parent_smoke`` 由编排层**每代重算一次**并复用（NFR-07-5 成本）。

    ``play_hash`` / ``parent_play_hash``（F62）：C2 的判据（见 ``judge``）；由编排层从
    ``staging_dir`` 与起点 harness 各算一次传进来——本函数不碰文件系统，保持纯编排。
    """
    started = time.perf_counter()
    ev = evaluator or CandidateEvaluator(config=cfg, generation=int(generation))
    version = int(harness_version)
    seeds = tuple(int(s) for s in (smoke_seeds or ev.smoke_seeds))

    try:
        _assert_play_face_actually_changed(candidate, play_hash=play_hash,
                                          parent_play_hash=parent_play_hash)
        if parent_smoke is None or parent_heldout is None:
            parent_heldout, parent_smoke = _resolve_parent(
                ev, parent, parent_heldout=parent_heldout, parent_smoke=parent_smoke,
                play_hash=str(parent_play_hash or ""))
        smoke, heldout, _unused = ev.candidate_arms(staging_dir, candidate=candidate,
                                                    harness_version=version,
                                                    play_hash=str(play_hash or ""))
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
    # F62（2026-10-14）：统计量降级为**报告项**（不再阻断），但必须**逐指标**留证据——
    # 原先生产路径只算 win_rate/McNemar，而 D-3/B-39 要的 4–5 pp 量级在连续轴上。
    by_metric = _paired_by_metric(heldout, parent_heldout,
                                  alpha=float(getattr(cfg, "alpha", 0.05)))
    if by_metric:
        paired["by_metric"] = by_metric
    report = CandidateReport(candidate=candidate, smoke=smoke_dict,
                             heldout=_metrics_dict(heldout), paired=paired,
                             train=smoke_dict,
                             diversity=_diversity_dict(heldout),
                             elapsed_s=time.perf_counter() - started)
    verdict = judge(report, parent_heldout, cfg=cfg, generation=int(generation),
                    play_hash=str(play_hash or ""),
                    parent_play_hash=str(parent_play_hash or ""),
                    effective_hash=str(effective_hash or ""),
                    parent_effective_hash=str(parent_effective_hash or ""))
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


def _play_plane_name(raw: Any) -> str:
    """把候选声明的文件路径归一成「相对 harness 根」的 play 面名字（B-92）。

    **实测口径**（`runs/m3-formal/generations/gen-00{1,2,3}/candidates/*.json`）：真实候选的
    ``touched_files`` 是 **`harness/prompts/decision.md`**（带 `harness/` 前缀），diff 头是
    ``--- a/harness/prompts/decision.md``。若只按文档里那种裸 ``prompts/decision.md`` 比，
    这个断言会**一次都不触发**——那正是 F73 家族「写了个永不生效的判据」的翻版，故此处显式归一。
    """
    name = str(raw).strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    for prefix in ("a/", "b/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.startswith("harness/"):
        name = name[len("harness/"):]
    return name


def _declared_play_plane_change(candidate: Candidate) -> bool:
    """候选是否**声明**改了 play 面（B-92 / F63 的窄口径断言用）。

    只看提案方给的 ``touched_files``（先按 ``_play_plane_name`` 归一）：

    - 命中 play 面文件（``prompts/decision.md`` / ``config.json`` / ``tools/*.py``）⇒ ``True``；
    - 只改学习侧（``reflection.md`` / ``dead_ends.md`` 等**不在** ``play_hash`` 内）⇒ ``False``
      ——那种提案由 C2 按规则合法拒绝（B-83 的诚实边界），不该被当成机器故障；
    - ``touched_files`` 为空（提案方没填）⇒ ``False``：宁可漏报，也不能误杀合法候选。
    """
    names = {_play_plane_name(f) for f in (candidate.touched_files or ()) if str(f).strip()}
    if not names:
        return False
    if PLAY_PROMPT_FILE in names or PLAY_CONFIG_FILE in names:
        return True
    return any(n.startswith("tools/") and n.endswith(".py") for n in names)


def _assert_play_face_actually_changed(candidate: Candidate, *, play_hash: str,
                                       parent_play_hash: str) -> None:
    """F63/F66 第二半（B-92）：声明改了 play 面、实测却与起点逐字节相同 ⇒ fail-closed。

    **为什么是硬件故障而不是规则拒绝**：``touched_files`` 里写着 ``prompts/decision.md``
    之类的 play 面文件，而实测 ``play_hash`` 与起点相同 ⇒ 补丁**没有生效**（F66 的现场就是
    这样：候选臂跑的是 run 级未打补丁的模板）。这属于机器故障，必须响亮失败，不能被当作
    「候选不好」；而且它**在花钱之前**判（0 局），一次脏提案省 ≈¥21。
    """
    if not (_declared_play_plane_change(candidate) and play_hash and parent_play_hash):
        return
    if str(play_hash) != str(parent_play_hash):
        return
    raise InfraError(
        E.E_EVOLVE_PATCH_INVALID,
        f"{candidate.candidate_id}: 补丁声明改了 play 面"
        f"（touched_files={list(candidate.touched_files)}），但实测 play_hash 与起点逐字节相同"
        f"（{str(play_hash)[:19]}…）—— 补丁没有生效（F66/F63 家族）",
        hint="检查 staging 拷贝与模板解析（resolve_path 必须优先 harness_dir）；"
             "本断言在花钱之前判，0 局消耗")


def _resolve_parent(ev: CandidateEvaluator, parent: Any, *, parent_heldout: Any,
                    parent_smoke: Any, play_hash: str = "") -> tuple[Any, Any]:
    if parent_heldout is not None and parent_smoke is not None:
        return parent_heldout, parent_smoke
    parent_dir = getattr(parent, "dir", None) or parent
    parent_version = int(getattr(parent, "version", 0) or 0)
    return ev.parent_arms(parent_dir, parent_version=parent_version, play_hash=play_hash)


def _compare_paired(candidate_arm: Any, parent_arm: Any) -> Any:
    """C2 的配对检验**唯一**来源（FR-07-7：MOD-07 不做统计）。"""
    from dice_ratchet.evals import compare_paired

    return compare_paired(candidate_arm, parent_arm)


def _paired_by_metric(candidate_arm: Any, parent_arm: Any, *, alpha: float) -> dict[str, Any]:
    """逐指标配对检验（F62 的**报告项**；方法仍全部归 MOD-08）。

    ``compare_all_metrics`` 早就存在于 MOD-08（文档写着「MOD-07 C2 逐指标取用」），
    但**没有任何生产调用点** ⇒ D-3/B-39 要求的连续轴检验在生产路径上从未执行过。
    现在它每次候选评估都跑一遍（数据都在内存里，**0 局新成本**），结果落
    ``paired["by_metric"]``，供判据 2 与人工复核。

    异常一律吞掉并返回 ``{}``：这是**证据**不是门；门的完整性由 ``_compare_paired``
    的 fail-closed 路径保证（两者在同一批内存数据上，前者失败通常意味着后者已抛）。
    """
    from dice_ratchet.evals import compare_all_metrics

    try:
        tests = compare_all_metrics(candidate_arm, parent_arm, alpha=float(alpha))
    except Exception: # noqa: BLE001 - 报告项失败不阻断裁决
        return {}
    out: dict[str, Any] = {}
    for name, test in dict(tests or {}).items():
        if str(name) not in _REPORTED_METRICS:
            # MOD-08 若将来扩了指标集，这里只收**文档登记过**的那几项：多出来的会
            # 悄悄改变 `paired["by_metric"]` 的形态，而它是判据 2 的复核入口。
            continue
        as_dict = getattr(test, "as_dict", None)
        out[str(name)] = dict(as_dict()) if callable(as_dict) else {"value": str(test)}
    return out


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
