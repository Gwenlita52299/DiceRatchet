"""绘图测试（FR-09-13）—— T-09-14。"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from gamer_rsi.infra import plotting
from gamer_rsi.infra.plotting import GENERATIONS_CSV_COLUMNS, plot_generations

# 列定义归 MOD-08；本模块只冻结读取


def _write_csv(path: Path, rows: int = 4) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_CSV_COLUMNS))
        writer.writeheader()
        for g in range(rows):
            writer.writerow({
                "generation": g, "harness_version": g, "n": 40 + g,
                "heldout_win_rate": round(0.40 + 0.03 * g, 4),
                "heldout_ci_lo": round(0.30 + 0.03 * g, 4),
                "heldout_ci_hi": round(0.50 + 0.03 * g, 4),
                "train_win_rate": round(0.45 + 0.04 * g, 4),
                "mean_stages_cleared": round(3.5 + 0.2 * g, 4),
                "mean_hearts_left": round(0.8 + 0.1 * g, 4),
                "mean_battles_won": round(9.0 + 0.5 * g, 4),
                "entropy_mean_norm": round(0.72 - 0.01 * g, 4),
                "entropy_slope": round(-0.004 - 0.001 * g, 4),
                "win_rate_gap": round(0.05 + 0.01 * g, 4),
                "verdict": "null",
            })


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
    from gamer_rsi.infra.errors import E, InfraError
    with pytest.raises(InfraError) as exc:
        plot_generations(tmp_path / "nope.csv", tmp_path / "plots")
    assert exc.value.code == E.E_IO_STATE_MISSING


def test_plot_generations_empty_csv_raises(tmp_path):
    from gamer_rsi.infra.errors import InfraError
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


def test_plot_generations_partial_columns(tmp_path):
    # 只有部分列也能绘出 5 张图（容错读取）
    pytest.importorskip("matplotlib")
    csv_path = tmp_path / "partial.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["generation", "n", "heldout_win_rate"])
        writer.writeheader()
        writer.writerow({"generation": 0, "n": 40, "heldout_win_rate": 0.5})
    out = plot_generations(csv_path, tmp_path / "plots")
    assert len(out) == 5
