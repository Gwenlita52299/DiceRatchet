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

from gamer_rsi.infra import events
from gamer_rsi.infra.errors import E, InfraError

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
_C5_ILLEGAL: float = 0.05
_C5_FALLBACK: float = 0.10
_C5_TRUNCATION: float = 0.05


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
    from gamer_rsi.evals.seeds import take

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
    illegal = float(heldout.get("illegal_rate") or 0.0)
    fallback = float(heldout.get("fallback_rate") or 0.0)
    truncation = float(heldout.get("truncation_rate") or 0.0)
    max_illegal = float(getattr(cfg, "max_illegal_rate", _C5_ILLEGAL))
    max_fallback = float(getattr(cfg, "max_fallback_rate", _C5_FALLBACK))
    max_truncation = float(getattr(cfg, "max_truncation_rate", _C5_TRUNCATION))
    c5_ok = (illegal <= max_illegal and fallback <= max_fallback
             and truncation <= max_truncation)
    trace.append(condition_trace_entry(
        "C5", c5_ok,
        f"illegal={illegal:.4f}≤{max_illegal} fallback={fallback:.4f}≤{max_fallback} "
        f"truncation={truncation:.4f}≤{max_truncation}",
        value={"illegal_rate": illegal, "fallback_rate": fallback,
               "truncation_rate": truncation},
        threshold={"max_illegal_rate": max_illegal,
                   "max_fallback_rate": max_fallback,
                   "max_truncation_rate": max_truncation}))
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
    """MOD-08 的注入面（默认实现走 ``gamer_rsi.evals``）。

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

    def _evaluate_arm(self, arm_dir: Any, *, split: str, basket: Any, arm: str,
                      harness_version: int) -> Any:
        from gamer_rsi.evals import evaluate_arm

        kwargs: dict[str, Any] = {}
        if self.runner is not None:
            kwargs["runner"] = _wrap_runner(self.runner)
        return evaluate_arm(
            arm_dir, split=split, basket=basket, memory_version=int(self.memory_version),
            arm=arm, run_id=self.run_id, protocol=self.protocol, config=self.config,
            generation=int(self.generation), harness_version=int(harness_version),
            episode_config=self.episode_config, **kwargs)

    def parent_arms(self, parent_dir: Any, *, parent_version: int
                    ) -> tuple[Any, Any]:
        """parent 的 heldout + 冒烟（FR-07-7：parent 每代都要重新评估）。"""
        heldout = self._evaluate_arm(parent_dir, split="heldout",
                                     basket=self.heldout_basket, arm="parent",
                                     harness_version=int(parent_version))
        smoke = self._evaluate_arm(parent_dir, split="train", basket=self.smoke_seeds,
                                   arm="parent-smoke", harness_version=int(parent_version))
        return heldout, smoke

    def candidate_arms(self, staging_dir: Any, *, candidate: Candidate,
                       harness_version: int) -> tuple[Any, Any, dict[str, Any]]:
        """候选的冒烟 + heldout + 配对检验（FR-07-6/7/8）。"""
        from gamer_rsi.evals import compare_paired

        arm = f"cand-{candidate.candidate_id}"
        smoke = self._evaluate_arm(staging_dir, split="train", basket=self.smoke_seeds,
                                   arm=f"{arm}-smoke", harness_version=int(harness_version))
        heldout = self._evaluate_arm(staging_dir, split="heldout",
                                     basket=self.heldout_basket, arm=arm,
                                     harness_version=int(harness_version))
        return smoke, heldout, {}


def _wrap_runner(runner: Any) -> Any:
    """MOD-06 ``EpisodeRunner`` → MOD-08 ``EpisodeRunner`` 协议适配器（懒惰 import）。"""
    from gamer_rsi.loop.runner import EvalRunnerAdapter

    return EvalRunnerAdapter(runner)


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
    from gamer_rsi.evals import compare_paired

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
        from gamer_rsi.evals.diversity import entropy_slope

        return float(entropy_slope(list(curve)))
    except Exception: # noqa: BLE001 - 曲线缺失/形态异常：C4 按"不可判"处理
        return None


def smoke_failure_reason(report: CandidateReport) -> str:
    """从候选报告里取冒烟淘汰原因（若有）。"""
    if report.error_code == str(E.E_EVOLVE_SMOKE_FAILED):
        return report.reason or str(E.E_EVOLVE_SMOKE_FAILED)
    raise SmokeFailedError("该候选未被冒烟淘汰", hint="调用方应先判断 error_code")
