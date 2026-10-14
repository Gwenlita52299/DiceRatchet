"""MOD-08 指标口径（PRD §6/§7/§13，Spec v1.4 INV-7 / §4.5）。

设计要点（每一条都对应 PRD 的一个可测需求）：

- **INV-7 结构不可绕过**：``EpisodeResult`` **没有** shaped 分数字段，主指标只从
  ``MAIN_METRIC_SOURCES`` 白名单（``info.outcome`` / ``info.truncation_reason`` /
  ``info.stages_cleared`` / ``info.hearts_left`` / ``info.battles_won``）来。
  本模块**不出现** 调试分数字段名（``tests/unit/evals/test_metrics_inv7.py`` 用 AST 扫描守）。
- **整数累加后单次相除**（FR-08-16）：所有均值/比率先累加整数计数，最后一次除法。
- **截断一律计 lose**（Spec §4.5 / §8.6）。
- **dropped 计入 n 且按 lose 计**（Spec v1.3 §8.5，防幸存者偏差）；``degraded`` 从 n 中
  剔除并单列 ``degraded_n``（Spec §8.5）。
- **数值精度**：报告侧统一 ``round(x, 4)``（Spec v1.4 B-34）；本模块只负责计算，
  落盘精度由 ``infra.io.write_json_atomic`` 与 ``render_report_sections`` 保证。

数据模型字段与 PRD §7 逐条对应；两个**超出 §7 速写的扩展字段**（``decision_kinds`` /
``slot_counts`` 的键约定）是 FR-08-15/20 的直接要求：

- ``decision_kinds``：决策点序号 → KIND_SPACE 下标，``action_entropy_curve()`` 的横轴依据；
- ``slot_counts`` 的键为 ``"<slotKind>×<school>"``，供 ``play_face`` 的 ``jsd_slot`` 口径。

函数清单与 Spec §4.7 / PRD §6 的签名一致。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from dice_ratchet.infra.errors import E, InfraError

__all__ = [
    "Split", "BasketId", "Outcome", "KIND_SPACE", "KIND_INDEX", "MAIN_METRIC_SOURCES",
    "EpisodeResult", "SplitMetrics", "GainVerdict", "CollapseVerdict", "PairedTest",
    "wilson_ci", "normalized_entropy", "action_entropy_curve", "bootstrap_mean_ci",
    "paired_bootstrap_gap", "mcnemar_exact_p", "aggregate", "render_report_sections",
    "judge_gain", "verdict_rules", "bh_fdr", "holm_bonferroni", "power_note",
    "bootstrap_seed", "mean_win_rate", "win_rate", "mean_stages_cleared",
    "mean_hearts_left", "mean_battles_won", "mean_decisions", "illegal_rate",
    "fallback_rate", "truncation_rate", "metric_value", "MAIN_METRICS",
]

Split = Literal["train", "heldout", "test"]
BasketId = Literal["H1", "H2", "H3", "test_full", "train_window"]
Outcome = Literal["win", "lose"]

#: Spec §4.3 agent 可用 kind，顺序即 ``kind_counts`` / ``decision_kinds`` 的下标（FR-08-15）。
KIND_SPACE: tuple[str, ...] = (
    "play_face", "discard_die", "purify", "use_ability", "end_turn", "map_choose",
    "shop_buy", "shop_refresh", "shop_remove", "shop_leave", "event_choose",
    "treasure_pick", "forge_pick", "shrine_choose", "rest_choose", "relic_swap",
)
KIND_INDEX: Mapping[str, int] = {name: i for i, name in enumerate(KIND_SPACE)}

#: INV-7 白名单：主指标只允许来自这些环境原生来源（Spec §4.5 / §3 MOD-08）。
MAIN_METRIC_SOURCES: tuple[str, ...] = (
    "info.outcome", "info.truncation_reason", "info.stages_cleared",
    "info.hearts_left", "info.battles_won",
)

#: MOD-07 C2 允许用于"显著提升"判定的指标（MOD-07 PRD §8.2）。
MAIN_METRICS: tuple[str, ...] = ("win_rate", "mean_stages_cleared", "mean_hearts_left")

#: 报告判定用的 z（95% 双侧正态分位；与 PRD T-01 的手算值一致）。
Z95: float = 1.959964


# =====================================================================
# 数据模型（PRD §7）
# =====================================================================

@dataclass(frozen=True)
class EpisodeResult:
    """一局的评估结果（INV-7：结构上不含 shaped 分数字段，主指标无法误用）。

    ``decision_kinds``：每个决策点选的 kind 在 ``KIND_SPACE`` 中的下标（FR-08-15
    的熵曲线横轴 = 决策点序号）。``slot_counts``：``"<slotKind>×<school>"`` → 次数
    （FR-08-20 的 ``jsd_slot`` 口径）。``seq_hash``：动作序列 sha256，供
    ``distinct_sequence_ratio``。
    """

    seed: int
    split: Split
    generation: int
    harness_version: int
    outcome: Outcome
    truncation_reason: str | None
    stages_cleared: int
    hearts_left: int
    battles_won: int
    decisions: int
    steps: int
    illegal_attempts: int
    fallback_decisions: int
    kind_counts: tuple[int, ...]      # 长度 16，对齐 KIND_SPACE
    slot_counts: Mapping[str, int]
    seq_hash: str
    dropped: bool = False
    degraded: bool = False
    decision_kinds: tuple[int, ...] = ()
    arm: str = ""
    error_code: str | None = None
    decisions_llm: int = 0

    def __post_init__(self) -> None:
        if len(self.kind_counts) != len(KIND_SPACE):
            raise InfraError(
                E.E_EVAL_SCHEMA,
                f"kind_counts 长度必须为 {len(KIND_SPACE)}，实际 {len(self.kind_counts)}",
                hint="对齐 KIND_SPACE（Spec §4.3）")

    @property
    def win(self) -> bool:
        """截断局一律 lose（Spec §4.5 / §8.6）。"""
        return self.outcome == "win" and not self.dropped

    def as_dict(self) -> dict[str, Any]:
        """白名单视图（写 ``episode_end`` 摘要行用）；**不含** shaped 分数字段。

        ``decision_kinds`` 是本模块对 Spec §4.5 ``episode_end`` 行的**附加**字段：
        没有它就无法重建 ``action_entropy_curve``（FR-08-15/20），因此随摘要一起落盘。
        """
        return {
            "seed": self.seed, "split": self.split, "generation": self.generation,
            "harness_version": self.harness_version, "outcome": self.outcome,
            "truncation_reason": self.truncation_reason,
            "stages_cleared": self.stages_cleared, "hearts_left": self.hearts_left,
            "battles_won": self.battles_won, "decisions": self.decisions,
            "steps": self.steps, "illegal_attempts": self.illegal_attempts,
            "fallback_decisions": self.fallback_decisions,
            "kind_counts": list(self.kind_counts), "decision_kinds": list(self.decision_kinds),
            "slot_counts": dict(self.slot_counts), "seq_hash": self.seq_hash,
            "dropped": self.dropped, "degraded": self.degraded,
            "arm": self.arm, "error_code": self.error_code,
            "decisions_llm": self.decisions_llm,
        }


@dataclass(frozen=True)
class SplitMetrics:
    """一个 (split, basket) 的聚合指标（PRD §7）。"""

    split: Split
    basket: BasketId
    n: int
    n_dropped: int
    low_confidence: bool
    win_rate: float
    win_rate_ci95: tuple[float, float]
    mean_stages_cleared: float
    mean_hearts_left: float
    mean_battles_won: float
    mean_decisions: float
    illegal_rate: float
    fallback_rate: float
    truncation_rate: float
    degraded_n: int
    entropy_mean_norm: float
    action_entropy_curve: tuple[float | None, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "basket": self.basket, "n": self.n, "n_dropped": self.n_dropped,
            "low_confidence": self.low_confidence, "win_rate": self.win_rate,
            "win_rate_ci95": list(self.win_rate_ci95),
            "mean_stages_cleared": self.mean_stages_cleared,
            "mean_hearts_left": self.mean_hearts_left,
            "mean_battles_won": self.mean_battles_won,
            "mean_decisions": self.mean_decisions,
            "illegal_rate": self.illegal_rate, "fallback_rate": self.fallback_rate,
            "truncation_rate": self.truncation_rate, "degraded_n": self.degraded_n,
            "entropy_mean_norm": self.entropy_mean_norm,
            "action_entropy_curve": [None if v is None else v
                                     for v in self.action_entropy_curve],
        }


@dataclass(frozen=True)
class GainVerdict:
    """增益真实性（PRD §7 / §8.2）。"""

    win_rate_gap: float
    gap_threshold: float
    verdict: str
    collapse_veto: bool
    mcnemar_p: float | None = None
    bootstrap_gap_ci95: tuple[float, float] = (0.0, 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "win_rate_gap": self.win_rate_gap,
            "gap_threshold": self.gap_threshold,
            "verdict": self.verdict,
            "collapse_veto": self.collapse_veto,
            "mcnemar_p": self.mcnemar_p,
            "bootstrap_gap_ci95": list(self.bootstrap_gap_ci95),
        }


@dataclass(frozen=True)
class CollapseVerdict:
    """坍缩判定（PRD §7）；``collapse_reasons`` 落 ext 文件、不进 report.json。"""

    entropy_slope: float
    collapse_flag: bool
    jsd_kind: float | None = None
    collapse_reasons: tuple[str, ...] = ()
    jsd_slot: float | None = None
    distinct_sequence_ratio: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entropy_slope": self.entropy_slope,
            "collapse_flag": self.collapse_flag,
            "jsd_kind": self.jsd_kind,
        }


@dataclass(frozen=True)
class PairedTest:
    """配对检验结果（Spec §4.7；MOD-07 不得自行实现统计检验）。"""

    method: str
    metric: str
    p: float
    delta: float
    significant: bool
    direction: Literal["up", "down", "flat"]
    n_pairs: int

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "metric": self.metric, "p": self.p,
                "delta": self.delta, "significant": self.significant,
                "direction": self.direction, "n_pairs": self.n_pairs}


# =====================================================================
# 基础统计（FR-08-15 / T-01 / T-02）
# =====================================================================

def wilson_ci(wins: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval（95% 默认）。``n == 0`` → ``E_EVAL_INSUFFICIENT_N``。

    手算校验（PRD T-01）：``wilson_ci(3, 5) == (0.2307, 0.8824)``；
    ``wilson_ci(0, 40) == (0.0, 0.0876)``。
    """
    if n <= 0:
        raise InfraError(E.E_EVAL_INSUFFICIENT_N, "wilson_ci 需要 n >= 1",
                         hint="空样本没有可报告的比例；先检查 dropped/degraded 剔除逻辑")
    wins = max(0, min(int(wins), int(n)))
    p_hat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p_hat + z2 / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def normalized_entropy(counts: Mapping[str, int] | Sequence[int], *,
                       kind_space: int = len(KIND_SPACE), base: float = 2.0) -> float:
    """归一化香农熵：``H / log_base(kind_space)``（FR-08-15 / T-02）。

    手算校验：``{play_face: 6, end_turn: 2}``、``kind_space=16``、``base=2``
    → ``0.811278 / 4 = 0.202820``（容差 1e-6）。空计数或单类别 → ``0.0``。
    """
    values = list(counts.values()) if isinstance(counts, Mapping) else list(counts)
    total = sum(int(v) for v in values)
    if total <= 0 or kind_space <= 1 or base <= 1.0:
        return 0.0
    entropy = 0.0
    for value in values:
        v = int(value)
        if v <= 0:
            continue
        p = v / total
        entropy -= p * (math.log(p) / math.log(base))
    return entropy / (math.log(kind_space) / math.log(base))


def action_entropy_curve(results: Sequence[EpisodeResult], *, t_max: int = 120,
                         min_support: int = 3) -> list[float | None]:
    """每个**决策点序号**上的归一化动作熵（FR-08-15，横轴 = decision_index）。

    长度恒为 ``t_max``；样本数 < ``min_support`` 的位置写 ``None``。
    """
    if t_max < 0:
        raise InfraError(E.E_EVAL_SCHEMA, f"t_max 必须 >= 0，实际 {t_max}")
    curve: list[float | None] = []
    for index in range(t_max):
        counts = [0] * len(KIND_SPACE)
        support = 0
        for result in results:
            if result.degraded or result.dropped:
                continue
            if index < len(result.decision_kinds):
                kind = int(result.decision_kinds[index])
                if 0 <= kind < len(KIND_SPACE):
                    counts[kind] += 1
                support += 1
        if support < max(1, int(min_support)):
            curve.append(None)
        else:
            curve.append(normalized_entropy(counts, kind_space=len(KIND_SPACE)))
    return curve


def bootstrap_mean_ci(values: Sequence[float], *, B: int = 10000,
                      rng: np.random.Generator) -> tuple[float, float]:
    """配对/单样本 bootstrap 均值 95% CI（NFR-08-01：随机性只来自显式 rng）。"""
    array = np.asarray(list(values), dtype=float)
    if array.size == 0:
        raise InfraError(E.E_EVAL_INSUFFICIENT_N, "bootstrap_mean_ci 需要非空样本")
    if B <= 0:
        raise InfraError(E.E_EVAL_SCHEMA, f"B 必须 >= 1，实际 {B}")
    idx = rng.integers(0, array.size, size=(int(B), array.size))
    means = array[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi))


def paired_bootstrap_gap(a: Sequence[float], b: Sequence[float], *, B: int = 10000,
                         rng: np.random.Generator) -> tuple[float, tuple[float, float]]:
    """配对 bootstrap 的均值差 ``mean(a) - mean(b)`` 点估计 + 95% CI（NFR-08-02）。

    ``a`` / ``b`` 必须同序对齐（调用方按 seed 配对后再传入）。
    """
    if len(a) != len(b):
        raise InfraError(E.E_EVAL_SCHEMA,
                         f"配对样本长度不一致：{len(a)} != {len(b)}",
                         hint="配对检验要求两个 ArmResult 使用同一 seed 集合")
    if len(a) == 0:
        raise InfraError(E.E_EVAL_INSUFFICIENT_N, "paired_bootstrap_gap 需要非空配对样本")
    diff = np.asarray(list(a), dtype=float) - np.asarray(list(b), dtype=float)
    idx = rng.integers(0, diff.size, size=(int(B), diff.size))
    means = diff[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(diff.mean()), (float(lo), float(hi)))


def _binom_tail(n: int, k: int, *, greater: bool) -> float:
    """``P(X >= k)``（greater）或 ``P(X <= k)``（less），``X ~ Binomial(n, 1/2)``。"""
    if n <= 0:
        return 1.0
    k = max(0, min(int(k), n))
    if greater:
        terms = sum(math.comb(n, i) for i in range(k, n + 1))
    else:
        terms = sum(math.comb(n, i) for i in range(0, k + 1))
    return min(1.0, terms / (2.0 ** n))


def mcnemar_exact_p(a: Sequence[bool], b: Sequence[bool],
                    alternative: Literal["two-sided", "greater", "less"] = "two-sided") -> float:
    """McNemar 精确检验（PRD §8.2 / T-10）。

    ``a`` = 候选逐局胜负，``b`` = parent 逐局胜负，按 seed 对齐。
    ``greater`` 表示"候选显著更好"。手算校验：``b=6, c=0`` → 双尾 ``0.03125``、
    单尾 ``0.015625``；``b=5, c=0`` → 双尾 ``0.0625``（不显著）。
    """
    if len(a) != len(b):
        raise InfraError(E.E_EVAL_SCHEMA,
                         f"McNemar 需要等长配对样本：{len(a)} != {len(b)}")
    b_disc = sum(1 for x, y in zip(a, b) if x and not y)
    c_disc = sum(1 for x, y in zip(a, b) if y and not x)
    n = b_disc + c_disc
    if n == 0:
        return 1.0
    if alternative == "greater":
        return _binom_tail(n, b_disc, greater=True)
    if alternative == "less":
        return _binom_tail(n, b_disc, greater=False)
    if alternative != "two-sided":
        raise InfraError(E.E_EVAL_SCHEMA,
                         f"未知 alternative={alternative!r}",
                         hint="合法值：two-sided | greater | less")
    return min(1.0, 2.0 * _binom_tail(n, min(b_disc, c_disc), greater=False))


# =====================================================================
# 聚合（FR-08-16：整数累加后单次相除）
# =====================================================================

def _effective(results: Sequence[EpisodeResult]) -> list[EpisodeResult]:
    """degraded 局从 n 中剔除（Spec §8.5）；dropped 局保留并计 lose。"""
    return [r for r in results if not r.degraded]


def mean_win_rate(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.win) / len(rows)


#: FR-08-16 的便捷访问器：签名同形 ``(results) -> float``。
win_rate = mean_win_rate


def mean_stages_cleared(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    return (sum(int(r.stages_cleared) for r in rows) / len(rows)) if rows else 0.0


def mean_hearts_left(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    return (sum(int(r.hearts_left) for r in rows) / len(rows)) if rows else 0.0


def mean_battles_won(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    return (sum(int(r.battles_won) for r in rows) / len(rows)) if rows else 0.0


def mean_decisions(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    return (sum(int(r.decisions) for r in rows) / len(rows)) if rows else 0.0


def _illegal_rate(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    # 分母为全部步级尝试（成功步 + 非法尝试），与 T-05 的「步级非法 3 / 总步 300」同口径
    attempts = sum(int(r.steps) + int(r.illegal_attempts) for r in rows)
    if attempts <= 0:
        return 0.0
    return sum(int(r.illegal_attempts) for r in rows) / attempts


def illegal_rate(results: Sequence[EpisodeResult]) -> float:
    return _illegal_rate(results)


def fallback_rate(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    decisions = sum(int(r.decisions) for r in rows)
    if decisions <= 0:
        return 0.0
    return sum(int(r.fallback_decisions) for r in rows) / decisions


def truncation_rate(results: Sequence[EpisodeResult]) -> float:
    rows = _effective(results)
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.truncation_reason is not None) / len(rows)


def metric_value(results: Sequence[EpisodeResult], metric: str) -> float:
    """按指标名取值（``compare_paired`` 的连续量分支用）。"""
    table = {
        "win_rate": mean_win_rate,
        "mean_stages_cleared": mean_stages_cleared,
        "mean_hearts_left": mean_hearts_left,
        "mean_battles_won": mean_battles_won,
        "mean_decisions": mean_decisions,
        "illegal_rate": _illegal_rate,
    }
    if metric not in table:
        raise InfraError(E.E_EVAL_SCHEMA, f"未知指标 {metric!r}",
                         hint=f"合法指标：{sorted(table)}")
    return float(table[metric](results))


def aggregate(results: Sequence[EpisodeResult], *, split: Split | None = None,
              basket: BasketId = "H1", t_max: int = 120, min_support: int = 3,
              drop_rate_max: float = 0.10) -> SplitMetrics:
    """聚合为 ``SplitMetrics``（FR-08-16）。

    - ``degraded`` 局剔除出 n，并单列 ``degraded_n``；
    - ``dropped`` 局保留在 n 内且按 lose 计（防幸存者偏差），单列 ``n_dropped``；
    - ``n_dropped / n > drop_rate_max``（或 n == 0）→ ``low_confidence=True``。
    """
    rows = list(results)
    effective = _effective(rows)
    n = len(effective)
    n_dropped = sum(1 for r in effective if r.dropped)
    degraded_n = sum(1 for r in rows if r.degraded)
    resolved_split: Split = split or (effective[0].split if effective else "heldout")
    if n == 0:
        ci = (0.0, 0.0)
        low_confidence = True
    else:
        wins = sum(1 for r in effective if r.win)
        ci = wilson_ci(wins, n)
        low_confidence = n_dropped / n > drop_rate_max
    counts = [0] * len(KIND_SPACE)
    for r in effective:
        for i, value in enumerate(r.kind_counts[: len(KIND_SPACE)]):
            counts[i] += int(value)
    curve = action_entropy_curve(effective, t_max=t_max, min_support=min_support)
    return SplitMetrics(
        split=resolved_split, basket=basket, n=n, n_dropped=n_dropped,
        low_confidence=low_confidence, win_rate=mean_win_rate(effective),
        win_rate_ci95=ci, mean_stages_cleared=mean_stages_cleared(effective),
        mean_hearts_left=mean_hearts_left(effective),
        mean_battles_won=mean_battles_won(effective),
        mean_decisions=mean_decisions(effective), illegal_rate=_illegal_rate(effective),
        fallback_rate=fallback_rate(effective),
        truncation_rate=truncation_rate(effective), degraded_n=degraded_n,
        entropy_mean_norm=normalized_entropy(counts, kind_space=len(KIND_SPACE)),
        action_entropy_curve=tuple(curve),
    )


# =====================================================================
# 增益真实性（FR-08-18 / §8.2）
# =====================================================================

def verdict_rules(*, gap: float, gap_threshold: float, p_improve: float,
                  p_regress: float | None, alpha: float, collapse_flag: bool,
                  low_confidence: bool = False) -> tuple[str, bool]:
    """§8.2 的**有序规则（短路求值）**，返回 ``(verdict, collapse_veto)``。

    与 PRD 逐字一致，额外插入两条保守规则（Spec §8.5 / PRD §10）：

    - ``low_confidence`` → 不出 ``real_gain``（最高为 ``insufficient``）；
    - ``collapse_flag`` → 永不出 ``real_gain``（``collapse_veto=True``）。
    """
    if p_regress is not None and p_regress < alpha:
        return "regress", False
    if gap > gap_threshold:
        return "overfit_suspect", False
    if collapse_flag:
        return "null", True
    if low_confidence:
        return "insufficient", False
    if p_improve < alpha and gap <= gap_threshold:
        return "real_gain", False
    return "null", False


def judge_gain(*, train: SplitMetrics, heldout: SplitMetrics, gap_threshold: float = 0.15,
               alpha: float = 0.05, collapse_flag: bool = False,
               candidate: Sequence[EpisodeResult] | None = None,
               parent: Sequence[EpisodeResult] | None = None,
               low_confidence: bool = False,
               rng: np.random.Generator | None = None, B: int = 10000) -> GainVerdict:
    """``win_rate_gap = train.win_rate - heldout.win_rate`` + §8.2 判定（FR-08-18）。

    ``candidate`` / ``parent`` 为同一 H1 篮子上的逐局胜负（按 seed 配对）→ McNemar；
    缺失时只按 gap 与 collapse 保守判定（``mcnemar_p=None``）。
    """
    gap = float(train.win_rate) - float(heldout.win_rate)
    p_improve = 1.0
    p_regress: float | None = None
    ci: tuple[float, float] = (0.0, 0.0)
    if candidate is not None and parent is not None and len(candidate) == len(parent):
        cand_bools = [r.win for r in candidate]
        parent_bools = [r.win for r in parent]
        p_improve = mcnemar_exact_p(cand_bools, parent_bools, "greater")
        p_regress = mcnemar_exact_p(cand_bools, parent_bools, "less")
        if rng is not None and candidate:
            _, ci = paired_bootstrap_gap(
                [1.0 if r.win else 0.0 for r in candidate],
                [1.0 if r.win else 0.0 for r in parent], B=B, rng=rng)
    low = low_confidence or heldout.low_confidence or train.low_confidence
    verdict, veto = verdict_rules(gap=gap, gap_threshold=gap_threshold,
                                  p_improve=p_improve, p_regress=p_regress,
                                  alpha=alpha, collapse_flag=collapse_flag,
                                  low_confidence=low)
    return GainVerdict(win_rate_gap=gap, gap_threshold=float(gap_threshold),
                       verdict=verdict, collapse_veto=veto, mcnemar_p=p_improve,
                       bootstrap_gap_ci95=ci)


# =====================================================================
# 多重比较（NFR-08-02）
# =====================================================================

def holm_bonferroni(pvalues: Sequence[float], *, alpha: float = 0.05
                    ) -> list[tuple[int, float, bool]]:
    """Holm–Bonferroni 校正（消融用，PRD §8.4）。

    返回 ``[(原下标, 校正后阈值, 是否显著)]``，按原顺序排列。
    第 ``k`` 小的 p 与 ``alpha / (m - k)`` 比较，并做前缀"全部显著"短路。
    """
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    out: list[tuple[int, float, bool]] = [(i, alpha, False) for i in range(m)]
    still = True
    for rank, index in enumerate(order):
        threshold = alpha / (m - rank)
        significant = still and pvalues[index] <= threshold
        if not significant:
            still = False
        out[index] = (index, threshold, significant)
    return out


def bh_fdr(pvalues: Sequence[float], *, q: float = 0.10) -> list[tuple[int, float, bool]]:
    """Benjamini–Hochberg FDR（次级指标，NFR-08-02）。"""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    out: list[tuple[int, float, bool]] = [(i, q, False) for i in range(m)]
    threshold = 1.0
    for rank in range(m - 1, -1, -1):
        index = order[rank]
        threshold = min(threshold, pvalues[index] * m / (rank + 1))
        out[index] = (index, threshold, pvalues[index] <= q)
    return out


# =====================================================================
# 报告渲染（FR-08-17 / §4.5）
# =====================================================================

#: n=40 下的功效说明必须写入报告 power_note（PRD §8.2）。
POWER_NOTE_N40: str = (
    "n=40 时 McNemar 精确检验最小可判显著的不一致对数 k=6（双尾 p=0.031，"
    "k=5 时 p=0.0625 不显著）；verdict 实际由 gap 主导、显著性为辅，"
    "未判 real_gain 只代表证据不足，不代表无提升。")

#: n=100（test）下的功效说明。
POWER_NOTE_N100: str = (
    "n=100 时 McNemar 精确检验的判定粒度更细（k=10 双尾 p≈0.0017）；"
    "test 结果只作终检复核，不进入任何代际决策路径（FR-08-04）。")


def power_note(n: int) -> str:
    """按样本量给出功效说明（R-1 对策：报告强制写 power_note）。"""
    if n <= 40:
        return POWER_NOTE_N40
    return POWER_NOTE_N100


def bootstrap_seed(run_id: str, generation: int, basket: str) -> int:
    """``rng_seed = int.from_bytes(sha256(f"{run_id}:{generation}:{basket}").digest()[:8])``.

    逐字实现 NFR-08-01（禁止 ``random.*``；bootstrap 只用显式 rng）。
    """
    payload = f"{run_id}:{int(generation)}:{basket}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], byteorder="big", signed=False)


def _round4(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _round4(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round4(v) for v in value]
    return value


def render_report_sections(*, generation: int, harness_version: int, parent_version: int,
                           heldout: SplitMetrics, train: SplitMetrics, gain: GainVerdict,
                           diversity: CollapseVerdict, ablation: Mapping[str, float] | None,
                           decision: str, decided_by: Literal["auto", "human"],
                           reason: str, episode_config_hash: str | None = None,
                           candidates: Sequence[Mapping[str, Any]] | None = None,
                           heldout_source: str = "measured",
                           play_hash: str = "", train_source: str = "measured",
                           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """产出 ``report.json`` 的指标段（Spec §4.5；decision 段由 MOD-07 填）。

    与 ``schemas/generation_report.schema.json`` 一一对应；``ablation`` 可为 ``null``。

    ``heldout_source``（F57，2026-10-12）：该代 ``heldout`` 段的**来源**。代际口径定为
    **本代结束时在役 harness 的测量**（D-M3-7，2026-10-14 修订；见 ``evolve/generation.py``）；
    该测量可能**跨代继承**：继承时记 ``"inherited:gen-00X"``，本代真测时记 ``"measured"``。
    没有这个字段，「这个 0.10 是本代测的，还是复用的上一代」在产物里**看不出来**。

    ``play_hash``（D-M3-7，2026-10-14）：该代**结束时在役** harness 的 play 面指纹。
    版本号在拒绝时也会推进（D2=A），所以「版本相同 ⇔ 起点相同」不再成立；代际继承与
    判据 1 的同起点豁免都以本字段为准（``versioning.play_hash``）。

    ``train_source``：``train`` 段取到了哪个 seed 块的训练局（``"measured"`` /
    ``"insufficient"``，F64）。该字段存在的唯一目的是让「训练段只拿到 4 局冒烟局」这类
    退化**在产物里可见**——实测 gen-001 的 ``gain_realism.win_rate_gap`` 就是建在 n=4 的
    垃圾训练段上而 ``low_confidence=False``。
    """
    if generation < 0:
        raise InfraError(E.E_EVAL_SCHEMA, f"generation 必须 >= 0，实际 {generation}")
    train_section: dict[str, Any] = {
        "window": train.n, "win_rate": train.win_rate,
        "n": train.n, "n_dropped": train.n_dropped,
        "low_confidence": train.low_confidence,
        "source": str(train_source or "measured"),
    }
    heldout_section = dict(heldout.as_dict())
    heldout_section["source"] = str(heldout_source or "measured")
    report: dict[str, Any] = {
        "generation": int(generation),
        "harness_version": int(harness_version),
        "parent_version": int(parent_version),
        "harness_play_hash": str(play_hash or ""),
        "episode_config_hash": episode_config_hash,
        "heldout": heldout_section,
        "train": train_section,
        "gain_realism": gain.as_dict(),
        "diversity": diversity.as_dict(),
        "ablation": dict(ablation) if ablation is not None else None,
        "candidates": [dict(c) for c in (candidates or ())],
        "decision": decision,
        "decided_by": decided_by,
        "reason": reason,
    }
    if extra:
        report.update(dict(extra))
    return _round4(report)


def best_metric(results: Sequence[EpisodeResult], metric: str) -> float:
    """便捷取值（``metric_value`` 的别名，语义更贴近调用点）。"""
    return metric_value(results, metric)
