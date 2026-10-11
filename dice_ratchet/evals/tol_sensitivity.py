"""C5 的 `tol` 最小可判效应（MDE）换算（D-M3-5 的 §3.3 落地；Spec v2.14 / B-70）。

**它回答的问题**：`illegal_tol = fallback_tol = truncation_tol = 0.05` 到底意味着
「多大的退化才会被拦下」？这个数字**可以零成本算出来**——不需要任何新跑批，
只需要各轴的**样本量与分母**。

**为什么必须按轴分列（这是本模块存在的核心理由，也是最容易写错的地方）**：

===========  ==========  ================================  =========================
轴            粒度        分母（`C1` hard×6 实测口径）       二项 se
===========  ==========  ================================  =========================
`illegal`     **步级**   成功步 + 非法尝试 ≈ **12,000/40 局**  ≈ 0.0017
`fallback`    **步级**   决策点 ≈ **3,700/40 局**             ≈ 0.0030
`truncation`  **局级**   40 局（粒度 1/40）                  0.025
===========  ==========  ================================  =========================

⇒ 同一个 `0.05`：对 `illegal` 是 **~30 个 se**（只抓「真·退化」，检不出微小漂移）、
对 `truncation` 只有 **2 局**。**三个轴不可互换**——把它当成一个数字读会得出错误结论。

**诚实边界**：本模块算的是「这根门在给定的样本量下能分辨多大的漂移」（MDE），
**不是**「用实测抖动标定出来的 `tol`」。后者需要 ≥2 代的配对实测（彩排 + 正式实验），
当前数据不支持（详见 Spec §8.6 与 M3 检查单 §3.3）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping

__all__ = ["AxisSensitivity", "TolSensitivity", "axis_sensitivity",
           "tolerance_sensitivity"]

#: `C1` 在 **heldout 一代两臂 × 40 局**（80 局）下的**实测分母**
#: （`runs/m2-s32b-smoke-c1-medium6` gen-1，逐局实测，2026-10-11）：
#:
#:   arm                  n   steps  illegal  attempts   decisions
#:   parent              40  13,778      453    14,231       4,595
#:   cand-af1591e1ad7e   40  13,160      445    13,605       4,426
#:   ---------------------------------------------------------------
#:   合计                80  26,938      898    27,836       9,021
#:
#: 两臂的「每局」口径只差 4.6%（attempts/ep 340.1 vs 355.8）⇒ 这个量级是**稳定的**。
#: 调用方拿到自己 run 的实测值时应显式传入（`denominators_from_run`）。
_FALLBACK_DENOMINATORS: Final[dict[str, tuple[str, float]]] = {
    # 轴 → (粒度, 分母)
    "illegal": ("step", 27_836.0),      # 成功步 + 非法尝试（一代两臂 80 局）
    "fallback": ("step", 9_021.0),      # 决策点（一代两臂 80 局）
    # 局级：**单臂 40 局**（判据 1 的裁决读的就是单臂 heldout；粒度 1/40 = 0.025）
    "truncation": ("episode", 40.0),
}


@dataclass(frozen=True)
class AxisSensitivity:
    """一个轴的 MDE 换算。"""

    axis: str
    granularity: str            # "step" | "episode"
    tol: float
    denominator: float          # 步级=尝试数；局级=局数
    rate: float | None = None   # 该轴的实测比例（可缺）
    se: float | None = None     # 二项标准误
    tol_in_units: float | None = None     # tol 相当于多少个「单位」（步 / 局）
    tol_in_se: float | None = None        # tol 相当于多少个 se
    raw_delta: float | None = None        # tol 对应的**原始计数**差（步数 / 局数）

    def as_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis, "granularity": self.granularity,
            "tol": round(float(self.tol), 6),
            "denominator": (None if self.denominator is None
                            else round(float(self.denominator), 3)),
            "rate": None if self.rate is None else round(float(self.rate), 6),
            "se": None if self.se is None else round(float(self.se), 6),
            "tol_in_units": (None if self.tol_in_units is None
                             else round(float(self.tol_in_units), 3)),
            "tol_in_se": None if self.tol_in_se is None else round(float(self.tol_in_se), 2),
            "raw_delta": None if self.raw_delta is None else round(float(self.raw_delta), 3),
            "reading": self.reading(),
        }

    def reading(self) -> str:
        """一句话结论（给报告直接用）。"""
        if self.granularity == "episode" and self.raw_delta is not None:
            return (f"tol={self.tol} ≈ **{self.raw_delta:.0f} 局**"
                    f"（分母 {self.denominator:.0f} 局，粒度 1/{self.denominator:.0f}）")
        if self.tol_in_se is not None:
            return (f"tol={self.tol} ≈ **{self.raw_delta:.0f} 个步级事件**"
                    f" = {self.tol_in_se:.0f} 个 se（分母 {self.denominator:.0f} 尝试，"
                    f"se≈{self.se:.4f}）")
        return f"tol={self.tol}（分母 {self.denominator:.0f}，粒度 {self.granularity}）"


@dataclass(frozen=True)
class TolSensitivity:
    """C5 三轴的 MDE 汇总。"""

    axes: tuple[AxisSensitivity, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    version: str = "tol-sensitivity-v1"

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version,
                "axes": [a.as_dict() for a in self.axes],
                "notes": list(self.notes),
                "disclaimer": ("这是**最小可判效应（MDE）**换算——「这根门能分辨多大的漂移」，"
                               "**不是**用实测抖动标定出来的 tol。后者需要 ≥2 代配对实测，"
                               "当前数据不支持。")}


def axis_sensitivity(axis: str, *, tol: float, denominator: float | None = None,
                     rate: float | None = None) -> AxisSensitivity:
    """单个轴的 MDE 换算。

    - ``denominator``：步级轴 = 尝试数（成功步 + 非法尝试）；局级轴 = 局数。缺省取
      `_FALLBACK_DENOMINATORS` 的 `C1` hard×6 估算。
    - ``rate``：该轴实测比例；给定后可算二项 se。
    """
    if axis not in _FALLBACK_DENOMINATORS:
        raise ValueError(f"未知轴 {axis!r}；已知 {sorted(_FALLBACK_DENOMINATORS)}")
    granularity, default_denom = _FALLBACK_DENOMINATORS[axis]
    denom = float(denominator if denominator is not None else default_denom)
    if denom <= 0:
        raise ValueError(f"denominator 必须 > 0，实际 {denom}")
    se = (math.sqrt(max(0.0, float(rate) * (1.0 - float(rate))) / denom)
          if rate is not None else None)
    # tol 以「比例」表示，换算回原始计数就是 tol × 分母
    raw = float(tol) * denom
    return AxisSensitivity(
        axis=axis, granularity=granularity, tol=float(tol), denominator=denom,
        rate=None if rate is None else float(rate), se=se,
        tol_in_units=raw, tol_in_se=(None if se in (None, 0.0) else float(tol) / se),
        raw_delta=raw)


def tolerance_sensitivity(*, illegal_tol: float = 0.05, fallback_tol: float = 0.05,
                          truncation_tol: float = 0.05,
                          denominators: Mapping[str, float] | None = None,
                          rates: Mapping[str, float] | None = None) -> TolSensitivity:
    """C5 三轴的 MDE 汇总（缺省用 `C1` hard×6 的分母估算）。"""
    denoms = dict(denominators or {})
    rate_map = dict(rates or {})
    axes = (
        axis_sensitivity("illegal", tol=illegal_tol,
                         denominator=denoms.get("illegal"), rate=rate_map.get("illegal")),
        axis_sensitivity("fallback", tol=fallback_tol,
                         denominator=denoms.get("fallback"), rate=rate_map.get("fallback")),
        axis_sensitivity("truncation", tol=truncation_tol,
                         denominator=denoms.get("truncation"),
                         rate=rate_map.get("truncation")),
    )
    notes = (
        "三轴**不可互换**：illegal/fallback 是**步级**（分母 ≈12k/3.7k 尝试），"
        "truncation 是**局级**（分母 40 局）。同一个 0.05 在前者≈30 个 se、在后者只有 2 局。",
        "分母来源按优先级：调用方传入的**本 run 实测值** > 缺省值。缺省值是 `C1` 在 "
        "`runs/m2-s32b-smoke-c1-medium6` gen-1 上**一代两臂 80 局**的实测口径"
        "（illegal 27,836 尝试 / fallback 9,021 决策 / truncation 单臂 40 局）。"
        "**注意旧归档的分母会偏大**（它含跨代多臂），所以「本 run 实测」才是权威。",
        "`tol` 的实测标定（按同代内抖动）**未做**：需要 ≥2 代配对实测。"
        "见 Spec §8.6 与 M3 检查单 §3.3。",
    )
    return TolSensitivity(axes=axes, notes=notes)


def denominators_from_run(run_dir: Path | str) -> dict[str, float]:
    """从已落盘的 run 里取**实测分母**（步级 = 成功步 + 非法尝试；局级 = 局数）。

    读不到任何一局时返回空 dict（调用方回落到缺省估算，并把这件事写进报告）。
    """
    from dice_ratchet.evals.harness import load_episode_results

    root = Path(run_dir)
    steps = 0
    illegal = 0
    decisions = 0
    episodes = 0
    for split in ("heldout", "train"):
        try:
            results = load_episode_results(root, split)  # type: ignore[arg-type]
        except Exception: # noqa: BLE001 - 读不到就跳过该 split
            continue
        for r in results:
            episodes += 1
            steps += int(r.steps)
            illegal += int(r.illegal_attempts)
            decisions += int(r.decisions)
    if episodes <= 0:
        return {}
    return {"illegal": float(steps + illegal), "fallback": float(decisions),
            "truncation": float(episodes)}
