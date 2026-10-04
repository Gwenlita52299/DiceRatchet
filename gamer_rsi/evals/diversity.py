"""MOD-08 多样性 / 坍缩监控（PRD §6/§8.3，FR-08-19/20）。

对齐调研 §4 的失败模式 2（多样性/模型坍缩）。四个条件（PRD §8.3）：

===== ================================================================ ==========
#     条件                                                                默认阈值
===== ================================================================ ==========
C1    ``entropy_slope <= -0.010``（动作分布随回合快速收敛）                -0.010
C2    ``entropy_mean_norm(g) < 0.60 × entropy_mean_norm(gen_0)``         0.60
C3    ``jsd_kind(g, g-1) < 0.010`` 且 ``win_rate`` 提升 ≥0.05             0.010/0.05
C4    ``distinct_sequence_ratio < 0.50``（轨迹高度重复）                  0.50
===== ================================================================ ==========

``collapse_flag = C4 OR (C1/C2/C3 任一在连续 2 代触发)``（两点确认，降误报）；
``collapse_reasons`` 逐条记录被触发的条件 ID（落 ``metrics/gen-XXX.ext.json``，
**不进** ``report.json``——PRD §7）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.infra.events import emit as _emit

from .metrics import KIND_SPACE, CollapseVerdict, EpisodeResult, _effective

__all__ = [
    "CollapseConfig", "js_divergence", "entropy_slope", "kind_distribution",
    "slot_distribution", "distinct_sequence_ratio", "collapse_check",
    "entropy_mean_norm", "divergence_series",
]


def entropy_slope(curve: Sequence[float | None]) -> float:
    """``curve`` 在**非 null 点**上的 OLS 斜率（FR-08-19 / T-04：横轴 = 决策点序号）。

    样本点 < 2 → ``0.0``（不足以谈趋势）。
    """
    points = [(float(i), float(v)) for i, v in enumerate(curve) if v is not None]
    if len(points) < 2:
        return 0.0
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in points)
    if var_x <= 0.0:
        return 0.0
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return float(cov / var_x)


@dataclass(frozen=True)
class CollapseConfig:
    """坍缩阈值（PRD §9 ``eval.collapse``）。"""

    c1_slope_threshold: float = -0.010
    c2_ratio_threshold: float = 0.60
    c3_jsd_freeze: float = 0.010
    c3_winrate_jump: float = 0.05
    c4_seq_ratio: float = 0.50
    confirm_generations: int = 2

    @classmethod
    def from_eval_config(cls, cfg: Any) -> "CollapseConfig":
        """从 ``infra.config.EvalConfig.Collapse``（或任意同名对象）构造。"""
        collapse = getattr(cfg, "collapse", cfg)
        return cls(
            c1_slope_threshold=float(getattr(collapse, "c1_slope_threshold", -0.010)),
            c2_ratio_threshold=float(getattr(collapse, "c2_ratio_threshold", 0.60)),
            c3_jsd_freeze=float(getattr(collapse, "c3_jsd_freeze", 0.010)),
            c3_winrate_jump=float(getattr(collapse, "c3_winrate_jump", 0.05)),
            c4_seq_ratio=float(getattr(collapse, "c4_seq_ratio", 0.50)),
            confirm_generations=int(getattr(collapse, "confirm_generations", 2)),
        )


# =====================================================================
# 基础量（FR-08-20）
# =====================================================================

def js_divergence(p: Mapping[str, float], q: Mapping[str, float], *,
                  base: float = 2.0) -> float:
    """Jensen–Shannon 散度（对称、有界，零概率项不产生 NaN）。

    手算校验（PRD T-03）：``P=(1,0), Q=(0.8,0.2)`` → ``0.108031``；``P=Q`` → ``0.0``。
    """
    if base <= 1.0:
        raise InfraError(E.E_EVAL_SCHEMA, f"base 必须 > 1，实际 {base}")
    keys = sorted(set(p) | set(q))
    if not keys:
        return 0.0
    p_sum = sum(float(p.get(k, 0.0)) for k in keys)
    q_sum = sum(float(q.get(k, 0.0)) for k in keys)
    p_vec = {k: (float(p.get(k, 0.0)) / p_sum if p_sum > 0 else 0.0) for k in keys}
    q_vec = {k: (float(q.get(k, 0.0)) / q_sum if q_sum > 0 else 0.0) for k in keys}
    log = math.log
    jsd = 0.0
    for k in keys:
        m = 0.5 * (p_vec[k] + q_vec[k])
        if p_vec[k] > 0.0 and m > 0.0:
            jsd += 0.5 * p_vec[k] * (log(p_vec[k] / m) / log(base))
        if q_vec[k] > 0.0 and m > 0.0:
            jsd += 0.5 * q_vec[k] * (log(q_vec[k] / m) / log(base))
    return float(max(0.0, jsd))


def kind_distribution(results: Sequence[EpisodeResult]) -> dict[str, float]:
    """``action.kind`` 的行为分布（比例，和为 1；总量 0 → 空 dict）。"""
    totals = [0] * len(KIND_SPACE)
    for result in _effective(results):
        for i, value in enumerate(result.kind_counts[: len(KIND_SPACE)]):
            totals[i] += int(value)
    total = sum(totals)
    if total <= 0:
        return {}
    return {name: totals[i] / total for i, name in enumerate(KIND_SPACE) if totals[i] > 0}


def slot_distribution(results: Sequence[EpisodeResult]) -> dict[str, float]:
    """``play_face`` 的 ``slotKind×school`` 分布（比例；键缺失即该组合未出现）。"""
    totals: dict[str, int] = {}
    for result in _effective(results):
        for key, value in dict(result.slot_counts or {}).items():
            totals[str(key)] = totals.get(str(key), 0) + int(value)
    total = sum(totals.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in sorted(totals.items()) if v > 0}


def distinct_sequence_ratio(results: Sequence[EpisodeResult]) -> float:
    """不同动作序列（``seq_hash``）占比 = ``distinct / n``（C4）。

    单局也算 1 条不同序列；空样本 → ``1.0``（不误报坍缩）。
    """
    rows = _effective(results)
    if not rows:
        return 1.0
    return len({r.seq_hash for r in rows}) / len(rows)


def entropy_mean_norm(curve: Sequence[float | None]) -> float:
    """曲线非 ``null`` 点的均值（C2 的分子；空曲线 → ``0.0``）。"""
    values = [float(v) for v in curve if v is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


def divergence_series(distributions: Sequence[Mapping[str, float]], *,
                      base: float = 2.0) -> list[float | None]:
    """相邻两代分布的 JSD 序列；``distributions[0]`` 没有前代 → ``None``。"""
    out: list[float | None] = [None]
    for prev, cur in zip(distributions, distributions[1:]):
        out.append(js_divergence(prev, cur, base=base))
    return out


# =====================================================================
# 两点确认的坍缩判定（FR-08-19 / §8.3）
# =====================================================================

def _confirmed(flags: Sequence[bool], *, confirm: int) -> bool:
    """末尾连续 ``confirm`` 代为 True（两点确认；``confirm<=1`` 时单代即可）。"""
    if confirm <= 1:
        return bool(flags and flags[-1])
    if len(flags) < confirm:
        return False
    return all(flags[-confirm:])


def collapse_check(*, curves: Sequence[Sequence[float | None]],
                   jsd_kind: Sequence[float | None],
                   win_rates: Sequence[float],
                   cfg: CollapseConfig,
                   jsd_slot: Sequence[float | None] | None = None,
                   seq_ratios: Sequence[float] | None = None,
                   generation: int | None = None) -> CollapseVerdict:
    """坍缩判定（FR-08-19）。

    ``curves`` / ``jsd_kind`` / ``win_rates`` 按**代数升序**排列，最后一个元素是当前代；
    ``seq_ratios``（可缺省）为各代 ``distinct_sequence_ratio``，用于 C4。
    """
    n_gen = len(curves)
    if n_gen == 0:
        return CollapseVerdict(entropy_slope=0.0, collapse_flag=False)
    slopes = [entropy_slope(curve) for curve in curves]
    means = [entropy_mean_norm(curve) for curve in curves]
    jsds = list(jsd_kind) + [None] * (n_gen - len(jsd_kind))
    wins = list(win_rates) + [0.0] * (n_gen - len(win_rates))

    c1_flags: list[bool] = [slopes[i] <= cfg.c1_slope_threshold for i in range(n_gen)]
    baseline = means[0]
    c2_flags: list[bool] = [
        (baseline > 0.0 and means[i] < cfg.c2_ratio_threshold * baseline)
        for i in range(n_gen)]
    c3_flags: list[bool] = [False] * n_gen
    for i in range(1, n_gen):
        frozen = jsds[i] is not None and float(jsds[i]) < cfg.c3_jsd_freeze  # type: ignore[arg-type]
        jumped = (wins[i] - wins[i - 1]) >= cfg.c3_winrate_jump
        c3_flags[i] = bool(frozen and jumped)
    c4_flags: list[bool] = [False] * n_gen
    if seq_ratios:
        seqs = list(seq_ratios) + [1.0] * (n_gen - len(seq_ratios))
        c4_flags = [float(seqs[i]) < cfg.c4_seq_ratio for i in range(n_gen)]

    confirm = max(1, int(cfg.confirm_generations))
    reasons: list[str] = []
    if _confirmed(c4_flags, confirm=1): # C4 单代即触发（坍缩最直接表征）
        reasons.append("C4")
    if _confirmed(c1_flags, confirm=confirm):
        reasons.append("C1")
    if _confirmed(c2_flags, confirm=confirm):
        reasons.append("C2")
    if _confirmed(c3_flags, confirm=confirm):
        reasons.append("C3")

    last = n_gen - 1
    verdict = CollapseVerdict(
        entropy_slope=float(slopes[last]), collapse_flag=bool(reasons),
        jsd_kind=(None if jsds[last] is None else float(jsds[last])),  # type: ignore[arg-type]
        collapse_reasons=tuple(reasons),
        jsd_slot=(None if not jsd_slot else
                  (None if jsd_slot[-1] is None else float(jsd_slot[-1]))),
        distinct_sequence_ratio=(None if not seq_ratios else float(seq_ratios[-1])),
    )
    if verdict.collapse_flag:
        try:
            _emit("eval.collapse", generation=int(generation if generation is not None else last),
                  flag=True, reasons=list(reasons))
        except Exception: # 埋点不得改变判定
            pass
    return verdict
