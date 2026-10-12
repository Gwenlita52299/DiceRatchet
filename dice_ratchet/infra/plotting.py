"""代际绘图（FR-09-13）。

读取 ``metrics/generations.csv``（**列定义归 MOD-08**，本模块只冻结文件路径与绘图接口），
输出到 ``runs/<run_id>/plots/``：

- ``win_rate.png``：heldout 胜率 + Wilson 95% CI 误差棒 + train 胜率对照
- ``stages_cleared.png`` / ``hearts_left.png``：次级指标
- ``entropy.png``：归一化动作熵与斜率标注
- ``train_heldout_gap.png``：INV-7 增益真实性口径的 gap 柱

每图含 ``n`` 标注、``ci95`` 误差棒与数据点标注。无 matplotlib 时降级为
``E_IO_PLOT_UNAVAILABLE`` 警告而不失败（返回 ``[]``）。

**2026-10-11（F-M3-3 裁决 A）修掉的三件事**——修复前这里产出的是**空白图**：

1. **列名口径**：本模块原先自己维护一份 ``GENERATIONS_CSV_COLUMNS``，其中
   ``heldout_win_rate`` / ``heldout_ci_lo`` / ``heldout_ci_hi`` / ``train_win_rate``
   四个名字**在真实台账里根本不存在**（MOD-08 写的是 ``win_rate`` / ``ci_lo`` / ``ci_hi``，
   且 train 是**另一行**而不是另一列）。``_g()`` 用 ``r.get(key)``，缺列时**返回空列表、
   不报错** ⇒ 5 张 PNG 全部"生成成功"、每张 20–27 KB、**打开是空白图**。现在这一份清单是
   **唯一权威**（``GENERATIONS_CSV_COLUMNS``），MOD-08 的 ``GENERATIONS_COLUMNS`` 直接
   从它取 ⇒ 两份清单在结构上不可能再分叉。

   **为什么权威定义放在 ``infra`` 而不是 ``evals``**：分层门禁禁止
   ``infra → evals``（NFR-09-5「依赖单向」，实测过：把派生写成 ``infra`` 导入 ``evals``
   会被 ``E_IO_GUARD_LAYER`` 拦下），而 ``evals → infra`` 是允许的。台账是 **IO 工件**，
   它的 schema 落在 IO 层既符合方向，也让下游（``harness``）能引用同一个事实来源。
2. **train 从哪来**：``split`` 是**行维度**。旧代码把 train 当列名找，永远找不到。
   现在按 ``split`` 取行（heldout 行画曲线 + CI，train 行画对照曲线）。
3. **"文件存在"≠"有图"**：任何一张图**没有数据点**时抛 ``E_IO_PLOT_NO_DATA``，
   不再允许空白图冒充 M3 的「曲线图」交付物。
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Any, Final, Sequence

from . import events
from .errors import E, InfraError

__all__ = ["plot_generations", "PLOT_FILES", "GENERATIONS_CSV_COLUMNS"]

PLOT_FILES: Final[tuple[str, ...]] = (
    "win_rate.png", "stages_cleared.png", "hearts_left.png",
    "entropy.png", "train_heldout_gap.png",
)

#: ``metrics/generations.csv`` 的**唯一权威列清单**（口径归 MOD-08，定义留在此处：
#: 见模块 docstring 第 1 条的分层理由）。``evals.harness.GENERATIONS_COLUMNS``
#: **直接取本常量**，因此「改列名忘改绘图」这类漂移在结构上不可能发生。
GENERATIONS_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "generation", "harness_version", "parent_version", "split", "basket", "n",
    "win_rate", "ci_lo", "ci_hi",
    "mean_stages_cleared", "mean_hearts_left", "mean_battles_won", "mean_decisions",
    "illegal_rate", "fallback_rate", "truncation_rate",
    "entropy_mean_norm", "entropy_slope", "jsd_kind", "collapse_flag",
    "win_rate_gap", "verdict", "decision",
    "bootstrap_seed", "config_hash", "git_commit", "ts",
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
    """取某列的非空值。**缺列返回空列表**（容错），但调用方必须再查 ``_require_points``。

    注意：这个「静默返回空」正是 F-M3-3 修复前产出空白图的原因（列名对不上时 5 张图
    全部"成功"生成）。保留容错语义是有意的——**判据由 ``_require_points`` 承担**，
    而不是靠这里抛错，这样「列缺失」与「合法地没有该点」能被区分开。
    """
    return [float(r[key]) for r in rows if r.get(key) is not None]


def _annotate(ax: Any, xs: Sequence[float], ys: Sequence[float],
              ns: Sequence[int]) -> None:
    """在每个数据点上标 ``值`` 与 ``n``（FR-09-13 要求「每图含 n 与数据点标注」）。"""
    for x, y, n in zip(xs, ys, ns):
        ax.annotate(f"{y:.3f}\n(n={n})", (x, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8)


def _split_rows(rows: Sequence[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    """取某个 split 的行。

    **兼容空 ``split``**：F-M3-3 之前的台账没有 train 行，且早期归档里有 ``split`` 列
    为空的行（实测 `runs/g6-eval-smoke`）。空值按 ``heldout`` 处理——否则那些 run 会
    一张图都画不出来。
    """
    want = "heldout" if split == "heldout" else "train"
    return [r for r in rows if str(r.get("split") or "").strip() in ("", want)]


def _dedup_by_generation(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 ``generation`` 去重**取末条**，并按代排序。

    为什么必须做：台账在 F43 之前会堆积同代重复行（实测 `runs/g6-eval-smoke` 同代同值
    出现 5 行）；不去重会让曲线出现**重影**——看着像多点，实则只有一个代。
    """
    by_gen: dict[float, dict[str, Any]] = {}
    for row in rows:
        gen = row.get("generation")
        if gen is None:
            continue
        by_gen[float(gen)] = row # 后写覆盖先写 ⇒ 取末条
    return [by_gen[g] for g in sorted(by_gen)]


def _require_points(name: str, xs: Sequence[Any], ys: Sequence[Any],
                    csv_path: Path) -> None:
    """**F-M3-3 的核心判据**：一张图没有数据点 ⇒ 拒绝产出空白图。

    修复前 matplotlib 对空序列不报错 ⇒ 5 张 20–27 KB 的空白 PNG 会被「文件存在」的
    判据当成 M3 交付物。M3 的验收语是「实测产出 5 张 PNG」，这里把它变成
    「**有数据点的** 5 张 PNG」。
    """
    if ys and len(ys) == len(xs):
        return
    raise InfraError(
        E.E_IO_PLOT_NO_DATA,
        f"{name} 没有可用数据点（x={len(xs)} y={len(ys)}）：{csv_path}",
        hint="检查 (a) 台账是否有该 split 的数据行——F-M3-3 之前只写 heldout 行，"
             "train 曲线与 gap 图必然为空；(b) 列名是否与 MOD-08 的 GENERATIONS_COLUMNS 一致"
             "（绘图列清单必须从 MOD-08 派生，不得另写一份）")


def plot_generations(csv_path: Path, out_dir: Path, *, figures: list[Any] | None = None,
                     close_after: bool = False) -> list[Path]:
    """绘图；返回生成的 png 列表。

    - matplotlib 不可用 → ``E_IO_PLOT_UNAVAILABLE``（降级，返回 ``[]``，不失败）；
    - 输入存在但**某张图没有数据点** → ``E_IO_PLOT_NO_DATA``（**失败**，不产出空白图）。

    ``figures``（可选）：传入一个 list 时，把每张图的 ``(名字, figure)`` 追加进去，
    供调用方**检查真实画出来的对象**。M3 彩排的「5 张 PNG 有数据点」判据走这条路——
    只查「文件存在」或「文件大小」会被空白图通过（F48 的教训：空白图 20–27 KB，
    非白像素比例 3.43% vs 有数据图 3.60%，两者分不开）。

    ``close_after``（F56，2026-10-12）：画完**立刻**关掉每张 figure。生产路径每代都会调
    本函数（`harness.ledger_and_plots`），不关会在长 run / 测试里撞上 matplotlib 的
    "More than 20 figures" 上限（实测 RuntimeWarning）。**默认 False 不变**：彩排判据要
    在返回后读 `Line2D.get_xydata()`，那时 figure 必须还活着。
    """
    try:
        plt = _import_matplotlib()
    except ImportError:
        events.emit("infra.plot_unavailable", code=E.E_IO_PLOT_UNAVAILABLE,
                    path=str(csv_path))
        return []
    csv_path = Path(csv_path)
    rows = _read_csv(csv_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    heldout = _dedup_by_generation(_split_rows(rows, "heldout"))
    train = _dedup_by_generation(_split_rows(rows, "train"))
    hgen = [float(r["generation"]) for r in heldout]
    tgen = [float(r["generation"]) for r in train]
    hns = [int(r.get("n") or 0) for r in heldout]
    tns = [int(r.get("n") or 0) for r in train]
    written: list[Path] = []

    def _save(fig: Any, name: str) -> None:
        fig.tight_layout()
        fig.savefig(out_dir / name, dpi=120)
        if figures is not None:
            figures.append((name, fig))
        if figures is None or close_after:
            plt.close(fig)
        written.append(out_dir / name)

    # ---- win_rate.png -------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    hw = _g(heldout, "win_rate")
    _require_points("win_rate.png", hgen, hw, csv_path)
    ax.plot(hgen, hw, "o-", label="heldout win_rate", color="#1f77b4")
    lo, hi = _g(heldout, "ci_lo"), _g(heldout, "ci_hi")
    if len(lo) == len(hw) == len(hi):
        ax.errorbar(hgen, hw, yerr=[hw[i] - lo[i] for i in range(len(hw))],
                    fmt="none", ecolor="#1f77b4", alpha=0.4, capsize=4, label="ci95")
        ax.errorbar(hgen, hw, yerr=[hi[i] - hw[i] for i in range(len(hw))],
                    fmt="none", ecolor="#1f77b4", alpha=0.4, capsize=4)
    # train 是**另一行**（`split="train"`），不是另一个列名——修复前按列名找，永远找不到。
    tw = _g(train, "win_rate")
    if tw:
        ax.plot(tgen[: len(tw)], tw, "s--", label="train win_rate", color="#ff7f0e")
    ax.set_xlabel("generation")
    ax.set_ylabel("win_rate")
    ax.set_title("Held-out win rate (Wilson 95% CI)")
    ax.legend(loc="best")
    ax.set_ylim(-0.05, 1.05)
    _annotate(ax, hgen, hw, hns)
    _save(fig, "win_rate.png")

    # ---- stages_cleared.png / hearts_left.png --------------------------------------
    for name, key, title, color in [
        ("stages_cleared.png", "mean_stages_cleared", "Mean stages cleared", "#2ca02c"),
        ("hearts_left.png", "mean_hearts_left", "Mean hearts left", "#d62728"),
    ]:
        fig, ax = plt.subplots(figsize=(7, 4.2))
        ys = _g(heldout, key)
        _require_points(name, hgen, ys, csv_path)
        ax.plot(hgen, ys, "o-", color=color)
        ax.set_xlabel("generation")
        ax.set_ylabel(key)
        ax.set_title(title)
        _annotate(ax, hgen, ys, hns)
        _save(fig, name)

    # ---- entropy.png ---------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    en = _g(heldout, "entropy_mean_norm")
    _require_points("entropy.png", hgen, en, csv_path)
    ax.plot(hgen, en, "o-", color="#9467bd", label="entropy_mean_norm")
    slope = heldout[-1].get("entropy_slope")
    if isinstance(slope, (int, float)):
        ax.text(0.02, 0.97, f"entropy_slope={float(slope):.4f}", transform=ax.transAxes,
                va="top", ha="left", fontsize=9,
                bbox={"facecolor": "wheat", "alpha": 0.7, "pad": 4})
    ax.set_xlabel("generation")
    ax.set_ylabel("normalized action entropy")
    ax.set_title("Action entropy (collapse watch, INV-7)")
    ax.set_ylim(-0.05, 1.05)
    _annotate(ax, hgen, en, hns)
    _save(fig, "entropy.png")

    # ---- train_heldout_gap.png ------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    gap = _g(heldout, "win_rate_gap")
    _require_points("train_heldout_gap.png", hgen, gap, csv_path)
    colors = ["#2ca02c" if g <= 0.15 else "#d62728" for g in gap]
    ax.bar(hgen, gap, color=colors, width=0.6)
    ax.axhline(0.15, ls="--", color="#d62728", label="gap_threshold=0.15 (INV-7)")
    ax.legend(loc="best")
    ax.set_xlabel("generation")
    ax.set_ylabel("win_rate_gap (train - heldout)")
    ax.set_title("Train/held-out gap (gain realism)")
    _save(fig, "train_heldout_gap.png")

    if not train:
        # gap 图有值但**没有 train 行**：win_rate_gap 是 heldout 行上的一个数（图仍可画），
        # 但 train 对照曲线会缺席——必须可观测，不能让「5 张图都在」掩盖这个缺口。
        events.emit("infra.plot_missing_split", split="train", path=str(csv_path))
    # 注意：`figures is not None` 时**不**在此关闭 figure —— 调用方（M3 彩排）要读
    # `Line2D.get_xydata()` 来判「真的有数据点」，关了就读不到了。调用方负责关。
    return written
