"""绘图测试（FR-09-13）—— T-09-14，以及 F-M3-3 的回归护栏。

**这个文件本身曾是缺陷的一部分**（2026-10-11 实测）：修复前它的 ``_write_csv`` 夹具用
``heldout_win_rate`` / ``heldout_ci_lo`` / ``heldout_ci_hi`` / ``train_win_rate`` 四个
**生产台账里不存在的列名**写数据 ⇒ 全部用例绿，而真实 ``runs/*/metrics/generations.csv``
（``win_rate`` / ``ci_lo`` / ``ci_hi``，train 是**另一行**）喂进去只能画出**空白图**，
5 张 PNG 各 20–27 KB、文件都在、打开什么都没有。

因此本文件的第一条纪律是：**夹具必须用 MOD-08 的权威列名建行**，
否则这里测的是一个不存在的数据形状。第二条纪律见 ``test_plot_requires_data_points``：
「文件存在」不等于「有图」。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dice_ratchet.infra import plotting
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.plotting import GENERATIONS_CSV_COLUMNS, plot_generations


def _write_csv(path: Path, rows: int = 4, *, train: bool = True,
               split: str = "heldout") -> None:
    """写一个**真实形状**的台账：一行 heldout（+ 每代一行 train）。

    列名取自 ``GENERATIONS_CSV_COLUMNS``（= MOD-08 的 ``GENERATIONS_COLUMNS``），
    ``split`` 是列、train 是**另一行**——与生产一致。
    """
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        for g in range(rows):
            writer.writerow({
                "generation": g, "harness_version": g, "n": 40 + g,
                "split": split, "basket": "H1",
                "win_rate": round(0.40 + 0.03 * g, 4),
                "ci_lo": round(0.30 + 0.03 * g, 4),
                "ci_hi": round(0.50 + 0.03 * g, 4),
                "mean_stages_cleared": round(3.5 + 0.2 * g, 4),
                "mean_hearts_left": round(0.8 + 0.1 * g, 4),
                "mean_battles_won": round(9.0 + 0.5 * g, 4),
                "entropy_mean_norm": round(0.72 - 0.01 * g, 4),
                "entropy_slope": round(-0.004 - 0.001 * g, 4),
                "win_rate_gap": round(0.05 + 0.01 * g, 4),
                "verdict": "null",
            })
            if train:
                writer.writerow({
                    "generation": g, "harness_version": g, "n": 30,
                    "split": "train", "basket": "train_window",
                    "win_rate": round(0.45 + 0.04 * g, 4),
                    "mean_stages_cleared": round(4.0 + 0.2 * g, 4),
                    "mean_hearts_left": round(0.9 + 0.1 * g, 4),
                    "mean_battles_won": round(9.5 + 0.5 * g, 4),
                    "entropy_mean_norm": round(0.70 - 0.01 * g, 4),
                })


# =====================================================================
# 列清单口径：**必须**与 MOD-08 同源（Spec B-06）
# =====================================================================

def test_column_list_is_derived_from_mod08():
    """F-M3-3 的根因护栏：绘图**不得**自己维护一份列清单。

    修复前 ``plotting.GENERATIONS_CSV_COLUMNS`` 与 ``harness.GENERATIONS_COLUMNS``
    有 4 个列名不同（``heldout_win_rate`` 等），而 ``_g()`` 缺列时静默返回空
    ⇒ 5 张空白 PNG 被「文件存在」的判据放行。本用例让这两份清单**结构上不可能分叉**。
    """
    from dice_ratchet.evals.harness import GENERATIONS_COLUMNS

    assert tuple(GENERATIONS_CSV_COLUMNS) == tuple(GENERATIONS_COLUMNS)
    for phantom in ("heldout_win_rate", "heldout_ci_lo", "heldout_ci_hi",
                    "train_win_rate"):
        assert phantom not in GENERATIONS_CSV_COLUMNS, (
            f"{phantom} 不是台账的列（train 是另一**行**，不是另一列）——F-M3-3 的原始缺陷")


# =====================================================================
# 基本产出
# =====================================================================

def test_plot_generations_produces_five_pngs(tmp_path):
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "generations.csv"
    _write_csv(csv_path)
    out = plot_generations(csv_path, tmp_path / "plots")
    names = sorted(p.name for p in out)
    assert names == sorted(plotting.PLOT_FILES)
    for p in out:
        assert p.is_file()
        assert p.stat().st_size > 0


def test_plot_generations_missing_csv_raises(tmp_path):
    with pytest.raises(InfraError) as exc:
        plot_generations(tmp_path / "nope.csv", tmp_path / "plots")
    assert exc.value.code == E.E_IO_STATE_MISSING


def test_plot_generations_empty_csv_raises(tmp_path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("generation\n", encoding="utf-8")
    with pytest.raises(InfraError):
        plot_generations(csv_path, tmp_path / "plots")


def test_plot_generations_degrades_without_matplotlib(tmp_path, monkeypatch):
    # T-09-14 ②：缺 matplotlib 时返回 [] 且不抛异常
    import builtins
    real_import = builtins.__import__

    def _fake_import(name: str, *args, **kw):
        if name == "matplotlib":
            raise ImportError("no matplotlib")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)
    csv_path = tmp_path / "generations.csv"
    _write_csv(csv_path)
    out = plot_generations(csv_path, tmp_path / "plots")
    assert out == []
    assert not (tmp_path / "plots").exists()


# =====================================================================
# F-M3-3：不许用空白图冒充产物
# =====================================================================

def test_plot_requires_data_points(tmp_path):
    """**核心回归**：有数据行但指标列全空 ⇒ 必须抛 ``E_IO_PLOT_NO_DATA``。

    修复前：``_g()`` 返回空列表，matplotlib 对空序列**不报错**，于是 5 张空白 PNG 被
    当成「实测产出 5 张 PNG」的验收证据。本用例把「文件存在」升级为「**有数据点**」。
    """
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "nogen.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        # 有 generation/split/n，但没有 win_rate / entropy / gap
        writer.writerow({"generation": 1, "split": "heldout", "n": 40})
    with pytest.raises(InfraError) as exc:
        plot_generations(csv_path, tmp_path / "plots")
    assert exc.value.code == E.E_IO_PLOT_NO_DATA
    assert "win_rate.png" in exc.value.message


def test_plot_accepts_partial_columns_when_points_exist(tmp_path):
    """容错读取仍在：**只要每张图有数据点**，列不全也能画。"""
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "partial.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        for g in range(3):
            writer.writerow({"generation": g, "split": "heldout", "n": 40,
                             "win_rate": 0.5, "mean_stages_cleared": 3.0,
                             "mean_hearts_left": 1.0, "entropy_mean_norm": 0.7,
                             "win_rate_gap": 0.05})
    out = plot_generations(csv_path, tmp_path / "plots")
    assert len(out) == 5


# =====================================================================
# F-M3-3：split 是**行**维度；重复代去重
# =====================================================================

def test_train_row_is_read_as_a_row_not_a_column(tmp_path):
    """train 曲线来自 ``split="train"`` 的行；旧代码按列名 ``train_win_rate`` 找 ⇒ 永远空。

    这里直接验「喂了 train 行就不会发 ``infra.plot_missing_split``，没喂就会发」——
    即 train 一侧的存在性是可观测的，不是「反正图都生成了」。
    """
    pytest.importorskip("matplotlib")
    from dice_ratchet.infra import events

    seen: list[tuple[str, dict]] = []
    real_emit = events.emit

    def _capture(ev, **fields):
        seen.append((ev, fields))

    # 有 train 行 → 不发 missing_split
    csv_path = tmp_path / "with_train.csv"
    _write_csv(csv_path, train=True)
    import dice_ratchet.infra.plotting as plotting_mod
    plotting_mod.events.emit = _capture
    try:
        plot_generations(csv_path, tmp_path / "plots_a")
    finally:
        plotting_mod.events.emit = real_emit
    assert [e for e, _ in seen if e == "infra.plot_missing_split"] == []

    # 无 train 行 → 发 missing_split（gap 图仍可画：gap 是 heldout 行上的一个数）
    seen.clear()
    csv_path2 = tmp_path / "no_train.csv"
    _write_csv(csv_path2, train=False)
    plotting_mod.events.emit = _capture
    try:
        plot_generations(csv_path2, tmp_path / "plots_b")
    finally:
        plotting_mod.events.emit = real_emit
    missing = [f for e, f in seen if e == "infra.plot_missing_split"]
    assert len(missing) == 1 and missing[0]["split"] == "train"


def test_duplicate_generation_rows_are_deduped(tmp_path):
    """同代重复行去重取末条。

    实测来源：``runs/g6-eval-smoke`` 的台账里同一代同值出现 **5 行**（单键 upsert 在
    空 ``split`` 上反复追加导致）。不去重会让曲线出现重影——看着像多点，实则一个代。
    """
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "dup.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        for _ in range(5):
            writer.writerow({"generation": 0, "split": "heldout", "n": 40,
                             "win_rate": 0.45, "mean_stages_cleared": 3.0,
                             "mean_hearts_left": 1.0, "entropy_mean_norm": 0.7,
                             "win_rate_gap": 0.05})
        writer.writerow({"generation": 1, "split": "heldout", "n": 41,
                         "win_rate": 0.50, "mean_stages_cleared": 3.2,
                         "mean_hearts_left": 1.1, "entropy_mean_norm": 0.71,
                         "win_rate_gap": 0.04})
    out = plot_generations(csv_path, tmp_path / "plots")
    assert len(out) == 5
    # 去重后只剩 2 个代（画出来的点数无法从文件断言，直接验内部去重函数）
    rows = plotting._read_csv(csv_path)
    assert len(plotting._dedup_by_generation(plotting._split_rows(rows, "heldout"))) == 2


def test_legacy_rows_without_split_are_treated_as_heldout(tmp_path):
    """兼容 F-M3-3 之前的台账（``split`` 列为空）——否则那些 run 一张图都画不出来。"""
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "legacy.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        for g in range(2):
            writer.writerow({"generation": g, "n": 40, "win_rate": 0.5,
                             "mean_stages_cleared": 3.0, "mean_hearts_left": 1.0,
                             "entropy_mean_norm": 0.7, "win_rate_gap": 0.05})
    out = plot_generations(csv_path, tmp_path / "plots")
    assert len(out) == 5


def test_f57_main_curve_skips_candidate_arm_rows(tmp_path):
    """F57：代际主曲线只画**起点臂**（``arm="parent"``），候选行不得混进来。

    修复前 ``(generation, split)`` 双键 ⇒ 候选行**覆盖** parent 行，图上那一点到底是
    谁的数看不出来。现在两行共存，绘图必须**显式分层**取 parent：否则同一代会出现两个点
    或候选数冒充代际进展。
    """
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "f57.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        writer.writerow({"generation": 1, "split": "heldout", "arm": "parent", "n": 20,
                         "win_rate": 0.10, "mean_stages_cleared": 4.35,
                         "mean_hearts_left": 0.25, "entropy_mean_norm": 0.3788,
                         "win_rate_gap": 0.15})
        writer.writerow({"generation": 1, "split": "heldout", "arm": "cand-1708efeb010a",
                         "n": 20, "win_rate": 0.35, "mean_stages_cleared": 4.75,
                         "mean_hearts_left": 0.40, "entropy_mean_norm": 0.3848,
                         "win_rate_gap": 0.05})
    figures: list = []
    plot_generations(csv_path, tmp_path / "plots", figures=figures)
    heldout_rows = plotting._dedup_by_generation(
        plotting._split_rows(plotting._read_csv(csv_path), "heldout", parents_only=True))
    assert [r["arm"] for r in heldout_rows] == ["parent"], "主曲线只认起点臂"
    assert [float(r["win_rate"]) for r in heldout_rows] == [0.10]
    win = dict(figures)["win_rate.png"]
    ax = win.axes[0]
    ys = [y for _x, y in ax.lines[0].get_xydata()]
    assert ys == [0.10], f"win_rate.png 的主曲线必须是 parent 的 0.10，实际 {ys}"
