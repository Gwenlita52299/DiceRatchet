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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping

__all__ = ["AxisSensitivity", "ComparisonDenominators", "TolSensitivity", "axis_sensitivity",
           "denominators_from_run", "tolerance_sensitivity"]

#: 臂归档文件名 ``gen{NNN}-{split}-{basket}-{arm}.json`` 里的代数
_GEN_FILE_RE: Final[re.Pattern[str]] = re.compile(r"^gen(\d{3})-")

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
        "分母来源按优先级：调用方传入的**本 run 实测值** > 缺省值。实测值取自 `metrics/arms/`"
        "的**同一代两臂**（`denominators_from_run`，B-91 / F52：修复前它把整 run 的两个 split、"
        "全部代与臂逐局累加 ⇒ 分母随 run 长度漂移、se 被低估 ≈2.2×）。缺省值是 `C1` 在 "
        "`runs/m2-s32b-smoke-c1-medium6` gen-1 上**一代两臂 80 局**的实测口径"
        "（illegal 27,836 尝试 / fallback 9,021 决策 / truncation 单臂 40 局）。",
        "`tol` 的实测标定（按同代内抖动）**未做**：需要 ≥2 代配对实测。"
        "见 Spec §8.6 与 M3 检查单 §3.3。",
    )
    return TolSensitivity(axes=axes, notes=notes)


@dataclass(frozen=True)
class ComparisonDenominators:
    """**一次 C5 比较**的实测分母与比例（B-91 / F52）。

    「一次比较」= 同一代、同一 split 的 `parent` 与 `cand-*` 两臂（`gen-000` 只有 `parent`
    时是单臂）。字段是给报告用的**口径标签**：读者必须能看出这些数字属于哪一代、哪几臂，
    否则「tol 相当于几局」又可以随 run 长度漂移（这正是 F52 的现场）。
    """

    generation: int
    split: str
    arms: tuple[str, ...]
    episodes_per_arm: int
    denominators: dict[str, float]
    rates: dict[str, float]
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {"generation": int(self.generation), "split": str(self.split),
                "arms": list(self.arms), "episodes_per_arm": int(self.episodes_per_arm),
                "denominators": {k: round(float(v), 3) for k, v in self.denominators.items()},
                "rates": {k: round(float(v), 6) for k, v in self.rates.items()},
                "source": self.source}


def denominators_from_run(run_dir: Path | str, *, generation: int | None = None,
                          split: str = "heldout") -> "ComparisonDenominators | None":
    """从**臂归档**读「**一次 C5 比较**」的实测分母与比例（B-91 / F52 修正）。

    **修复前**（F52）：本函数对 ``episodes/heldout`` + ``episodes/train`` 的**全部**
    ``EpisodeResult`` 逐局累加——不分代、不分臂，连两个免费基线臂与 train/冒烟都算进去。
    于是同一个 run、同一条命令，分母随「跑了多久/有没有补测基线」而变（实测正式实验日志里
    是 **382**，事后重算是 **462**），而 C5 实际比的是**同一代内 candidate vs parent**
    （每臂 40 局）。后果不只是偏大：``se`` 被低估 ≈2.2×、``tol_in_se`` 由 0.69 抬到 2.13
    （把噪声主导的 truncation 轴读成「超过 2 个 se」），而读出的「粒度 1/462」**根本不存在**
    ——没有任何一个被测比例背后有 462 局。

    **现在**：读 ``metrics/arms/gen{NNN}-{split}-*.json``（C5 真正读的那批 `EpisodeMetrics`
    的逐局明细），按下面的口径算：

    - **代**：``generation`` 显式给定，否则取「有非基线臂归档的**最大代**」；
    - **臂**：该代的全部非 ``baseline-*`` 臂（``gen-000`` 只有 ``parent`` 时就是单臂；
      ``gen ≥ 1`` 是 ``parent`` + ``cand-*`` 两臂）——这正是「一次比较」的参与者；
    - **degraded 局剔除**（与 ``evals.metrics`` 的 ``_effective`` 同口径），dropped 保留；
    - **步级分母 = 各臂合计**（尝试 / 决策点，与缺省常量 27,836 / 9,021 同口径，
      对应「两个比例的差」的样本量）；**局级分母 = 每臂最少局数**（与缺省常量 40 同口径，
      对应「单臂比例的分辨率 1/n」）；
    - ``rates`` 是按同一批局算出的三轴比例，供 ``se`` 使用（修复前 CLI 把 truncation 的
      比例**写死 0.30**，另两轴干脆不传 ⇒ 只有一根轴的 se 有值）。

    读不到任何可用臂归档时返回 ``None``（调用方回落缺省估算**并写明**）。
    """
    from dice_ratchet.evals.harness import arms_dir, load_arm_archive

    root = Path(run_dir)
    adir = arms_dir(root)
    if not adir.is_dir():
        return None
    by_gen: dict[int, list[tuple[str, Mapping[str, Any]]]] = {}
    for path in sorted(adir.glob(f"gen???-{split}-*.json")):
        match = _GEN_FILE_RE.match(path.name)
        if match is None:
            continue
        data = load_arm_archive(path)
        if not data:
            continue
        arm = str(data.get("arm") or "")
        if not arm or arm.endswith("-smoke"):
            continue # 冒烟不属于 C5 的比较（且它在 train split）
        by_gen.setdefault(int(match.group(1)), []).append((arm, data))
    if not by_gen:
        return None
    # 优先「有非基线臂」的最大代；全代都只有基线臂时退回「有归档的最大代」
    preferred = generation if generation is not None else max(by_gen)
    order = [int(preferred)] if generation is not None else sorted(by_gen, reverse=True)
    picked: list[tuple[str, Mapping[str, Any]]] = []
    picked_gen = int(preferred)
    for gen in order:
        items = by_gen.get(int(gen)) or []
        real = [(a, d) for a, d in items if not a.startswith("baseline")]
        if real or (generation is not None and items):
            picked, picked_gen = (real or items), int(gen)
            break
    if not picked:
        return None

    rows: list[dict[str, float]] = []
    arms: list[str] = []
    for arm, data in picked:
        episodes = [e for e in (data.get("episodes") or ()) if not bool(e.get("degraded"))]
        if not episodes:
            continue
        attempts = sum(int(e.get("steps") or 0) + int(e.get("illegal_attempts") or 0)
                       for e in episodes)
        illegal = sum(int(e.get("illegal_attempts") or 0) for e in episodes)
        decisions = sum(int(e.get("decisions") or 0) for e in episodes)
        fallback = sum(int(e.get("fallback_decisions") or 0) for e in episodes)
        truncated = sum(1 for e in episodes if e.get("truncation_reason"))
        rows.append({"n": float(len(episodes)), "attempts": float(attempts),
                     "illegal": float(illegal), "decisions": float(decisions),
                     "fallback": float(fallback), "truncated": float(truncated)})
        arms.append(arm)
    if not rows:
        return None

    attempts = sum(r["attempts"] for r in rows)
    decisions = sum(r["decisions"] for r in rows)
    per_arm_n = min(r["n"] for r in rows)
    denominators = {
        "illegal": attempts,
        "fallback": decisions,
        # 局级：单臂分辨率（与 _FALLBACK_DENOMINATORS 的 40 同口径）
        "truncation": per_arm_n,
    }
    rates = {
        "illegal": (sum(r["illegal"] for r in rows) / attempts) if attempts > 0 else 0.0,
        "fallback": (sum(r["fallback"] for r in rows) / decisions) if decisions > 0 else 0.0,
        "truncation": (sum(r["truncated"] for r in rows)
                       / sum(r["n"] for r in rows)) if rows else 0.0,
    }
    source = (f"本 run 实测（{split}）：gen-{picked_gen:03d} 的 {len(arms)} 臂"
              f"（{', '.join(arms)}），每臂最少 {per_arm_n:.0f} 局；"
              f"步级分母 = 各臂合计尝试/决策（{attempts:.0f} / {decisions:.0f}）")
    return ComparisonDenominators(generation=picked_gen, split=str(split),
                                  arms=tuple(arms), episodes_per_arm=int(per_arm_n),
                                  denominators=denominators, rates=rates, source=source)
