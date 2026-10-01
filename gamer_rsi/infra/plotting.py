"""代际绘图（FR-09-13）。

读取 ``metrics/generations.csv``（**列定义归 MOD-08**，本模块只冻结文件路径与绘图接口），
输出到 ``runs/<run_id>/plots/``：

- ``win_rate.png``：heldout 胜率 + Wilson 95% CI 误差棒 + train 胜率对照
- ``stages_cleared.png`` / ``hearts_left.png``：次级指标
- ``entropy.png``：归一化动作熵与斜率标注
- ``train_heldout_gap.png``：INV-7 增益真实性口径的 gap 柱

每图含 ``n`` 标注、``ci95`` 误差棒与数据点标注。无 matplotlib 时降级为
``E_IO_PLOT_UNAVAILABLE`` 警告而不失败（返回 ``[]``）。
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Any, Final, Sequence

from .errors import E, InfraError

__all__ = ["plot_generations", "PLOT_FILES", "GENERATIONS_CSV_COLUMNS"]

PLOT_FILES: Final[tuple[str, ...]] = (
    "win_rate.png", "stages_cleared.png", "hearts_left.png",
    "entropy.png", "train_heldout_gap.png",
)

# 读取约定（列定义与口径归 MOD-08；本模块只做容错读取）
GENERATIONS_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "generation", "harness_version", "n",
    "heldout_win_rate", "heldout_ci_lo", "heldout_ci_hi",
    "train_win_rate",
    "mean_stages_cleared", "mean_hearts_left", "mean_battles_won",
    "entropy_mean_norm", "entropy_slope", "win_rate_gap", "verdict",
)


def _import_matplotlib() -> Any:
    """延迟导入 matplotlib；home 不可写时把缓存目录指到临时目录。"""
    if not os.access(os.path.expanduser("~"), os.W_OK):
        os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplcfg-"))
    import matplotlib
    matplotlib.use("Agg") # 无头模式，避免后端警告
    import matplotlib.pyplot as plt
    return plt


def _read_csv(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.is_file():
        raise InfraError(E.E_IO_STATE_MISSING,
                         f"绘图输入不存在：{csv_path}",
                         hint="先跑 MOD-08 的 evaluate_generation 生成 metrics/generations.csv")
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise InfraError(E.E_IO_STATE_MISSING, f"{csv_path} 没有任何数据行")
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            item[key] = _num(value)
        out.append(item)
    return out


def _num(value: str | None) -> Any:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _g(rows: Sequence[dict[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


def _annotate(ax: Any, xs: Sequence[float], ys: Sequence[float], ns: Sequence[int]) -> None:
    for x, y, n in zip(xs, ys, ns):
        ax.annotate(f"{y:.3f}\n(n={n})", (x, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8)


def plot_generations(csv_path: Path, out_dir: Path) -> list[Path]:
    """绘图；返回生成的 png 列表（matplotlib 不可用时返回 ``[]`` 并告警）。"""
    try:
        plt = _import_matplotlib()
    except ImportError:
        events.emit("infra.plot_unavailable", code=E.E_IO_PLOT_UNAVAILABLE,
                    path=str(csv_path))
        return []
    rows = _read_csv(Path(csv_path))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gens = [float(r["generation"]) for r in rows]
    ns = [int(r.get("n") or 0) for r in rows]
    written: list[Path] = []

    def _save(fig: Any, name: str) -> None:
        fig.tight_layout()
        fig.savefig(out_dir / name, dpi=120)
        plt.close(fig)
        written.append(out_dir / name)

    # ---- win_rate.png -------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    hw = _g(rows, "heldout_win_rate")
    ax.plot(gens[: len(hw)], hw, "o-", label="heldout win_rate", color="#1f77b4")
    lo, hi = _g(rows, "heldout_ci_lo"), _g(rows, "heldout_ci_hi")
    if len(lo) == len(hw) == len(hi):
        ax.errorbar(gens[: len(hw)], hw, yerr=[hw[i] - lo[i] for i in range(len(hw))],
                    fmt="none", ecolor="#1f77b4", alpha=0.4, capsize=4, label="ci95")
        ax.errorbar(gens[: len(hw)], hw, yerr=[hi[i] - hw[i] for i in range(len(hw))],
                    fmt="none", ecolor="#1f77b4", alpha=0.4, capsize=4)
    tw = _g(rows, "train_win_rate")
    if tw:
        ax.plot(gens[: len(tw)], tw, "s--", label="train win_rate", color="#ff7f0e")
    ax.set_xlabel("generation")
    ax.set_ylabel("win_rate")
    ax.set_title("Held-out win rate (Wilson 95% CI)")
    ax.legend(loc="best")
    ax.set_ylim(-0.05, 1.05)
    _annotate(ax, gens[: len(hw)], hw, ns[: len(hw)])
    _save(fig, "win_rate.png")

    # ---- stages_cleared.png / hearts_left.png --------------------------------------
    for name, key, title, color in [
        ("stages_cleared.png", "mean_stages_cleared", "Mean stages cleared", "#2ca02c"),
        ("hearts_left.png", "mean_hearts_left", "Mean hearts left", "#d62728"),
    ]:
        fig, ax = plt.subplots(figsize=(7, 4.2))
        ys = _g(rows, key)
        ax.plot(gens[: len(ys)], ys, "o-", color=color)
        ax.set_xlabel("generation")
        ax.set_ylabel(key)
        ax.set_title(title)
        _annotate(ax, gens[: len(ys)], ys, ns[: len(ys)])
        _save(fig, name)

    # ---- entropy.png ---------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    en = _g(rows, "entropy_mean_norm")
    ax.plot(gens[: len(en)], en, "o-", color="#9467bd", label="entropy_mean_norm")
    slope = rows[-1].get("entropy_slope")
    if isinstance(slope, float):
        ax.text(0.02, 0.97, f"entropy_slope={slope:.4f}", transform=ax.transAxes,
                va="top", ha="left", fontsize=9,
                bbox={"facecolor": "wheat", "alpha": 0.7, "pad": 4})
    ax.set_xlabel("generation")
    ax.set_ylabel("normalized action entropy")
    ax.set_title("Action entropy (collapse watch, INV-7)")
    ax.set_ylim(-0.05, 1.05)
    _annotate(ax, gens[: len(en)], en, ns[: len(en)])
    _save(fig, "entropy.png")

    # ---- train_heldout_gap.png ------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    gap = _g(rows, "win_rate_gap")
    if gap:
        colors = ["#2ca02c" if g <= 0.15 else "#d62728" for g in gap]
        ax.bar(gens[: len(gap)], gap, color=colors, width=0.6)
        ax.axhline(0.15, ls="--", color="#d62728", label="gap_threshold=0.15 (INV-7)")
        ax.legend(loc="best")
    ax.set_xlabel("generation")
    ax.set_ylabel("win_rate_gap (train - heldout)")
    ax.set_title("Train/held-out gap (gain realism)")
    _save(fig, "train_heldout_gap.png")

    return written
