"""G6 可复现命令（PRD §12 G6 / §11 集成）。

``python -m dice_ratchet inference probe|bench|robustness`` 必须退出码 0 且报告落盘；
错误路径必须给出退出码（3 模型不可用 / 2 配置错误 / 1 未达 M1 阈值）。
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from dice_ratchet import app
from dice_ratchet.infra import events


@pytest.fixture(autouse=True)
def _isolated_run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DICERATCHET_INFRA__RUNS_ROOT", str(tmp_path / "runs"))
    events.unbind()
    yield
    events.unbind()


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = app.main(argv, reexec=False)
    return code, out.getvalue(), err.getvalue()


def test_probe_mock_exits_zero_and_writes_report(tmp_path: Path):
    report_path = tmp_path / "probe.json"
    code, _, stderr = _run(["inference", "probe", "--model", "mock", "--run-id", "it-probe",
                            "--mode", "eval", "--seed", "7", "--out", str(report_path)])
    assert code == 0, stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["model_id"] == "mock" and report["parsed_ok"] is True
    assert report["outcome_kind"] == "action"
    assert report["health_before"]["state"] == "ready"
    assert report["usage"]["prompt_tokens"] > 0


def test_bench_mock_exits_zero_and_writes_report(tmp_path: Path):
    report_path = tmp_path / "bench.json"
    code, _, stderr = _run(["inference", "bench", "--model", "mock", "--run-id", "it-bench",
                            "--mode", "eval", "--seed", "7", "--n", "5",
                            "--out", str(report_path)])
    assert code == 0, stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "bench" and report["n"] == 5 and report["n_ok"] == 5
    assert report["budget_ok"] is True
    assert report["aggregate"]["overall"]["n_calls"] == 5


def test_robustness_mock_meets_thresholds_and_writes_m1_deliverable(tmp_path: Path):
    report_path = tmp_path / "format_robustness.json"
    code, _, stderr = _run(["inference", "robustness", "--model", "mock",
                            "--run-id", "it-robust", "--mode", "eval", "--seed", "7",
                            "--n", "25", "--out", str(report_path)])
    assert code == 0, stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "format_robustness" and report["passed"] is True
    assert report["overall"]["first_pass_valid_rate"] >= 0.90
    assert report["overall"]["schema_fail_rate"] <= 0.01
    assert report["overall"]["n_calls"] == 25


def test_reports_default_to_run_metrics_dir(tmp_path: Path):
    code, _, stderr = _run(["inference", "probe", "--model", "mock",
                            "--run-id", "it-defaults", "--mode", "eval", "--seed", "7"])
    assert code == 0, stderr
    metrics = tmp_path / "runs" / "it-defaults" / "metrics" / "inference_probe.json"
    assert metrics.is_file()


def test_raw_prompt_is_not_dumped_by_default(tmp_path: Path):
    """NFR-03-05：默认只存 prompt_digest，原文不落 raw/。"""
    code, _, stderr = _run(["inference", "probe", "--model", "mock",
                            "--run-id", "it-noraw", "--mode", "eval", "--seed", "7"])
    assert code == 0, stderr
    assert not (tmp_path / "runs" / "it-noraw" / "raw").exists()


def test_debug_dump_raw_writes_under_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DICERATCHET_INFERENCE__DEBUG_DUMP_RAW", "true")
    code, _, stderr = _run(["inference", "probe", "--model", "mock",
                            "--run-id", "it-raw", "--mode", "eval", "--seed", "7"])
    assert code == 0, stderr
    raw_dir = tmp_path / "runs" / "it-raw" / "raw" / "llm"
    assert raw_dir.is_dir() and list(raw_dir.glob("c-*.txt"))


def test_bench_report_carries_measured_resident_gb(tmp_path: Path):
    """NFR-03-03：实测常驻内存作为回填证据出现在报告里。"""
    report_path = tmp_path / "bench.json"
    code, _, stderr = _run(["inference", "bench", "--model", "mock", "--run-id", "it-res",
                            "--mode", "eval", "--seed", "7", "--n", "3",
                            "--out", str(report_path)])
    assert code == 0, stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["resident_gb_measured"] == 0.0 # mock 注册表 resident_gb_est=0.0
    assert report["ram_budget_gb"] == 20.0


def test_unknown_model_exits_model_unavailable():
    code, _, stderr = _run(["inference", "probe", "--model", "nope", "--run-id", "it-bad"])
    assert code == 3 # EXIT_MODEL（E_LLM_UNAVAILABLE）
    assert "E_LLM_UNAVAILABLE" in stderr


def test_eval_without_seed_is_a_config_error():
    code, _, stderr = _run(["inference", "probe", "--model", "mock", "--mode", "eval",
                            "--run-id", "it-eval-no-seed"])
    assert code == 2
    assert "E_IO_CONFIG_INVALID" in stderr or "seed" in stderr


def test_dry_run_does_not_touch_runs_dir(tmp_path: Path):
    code, stdout, _ = _run(["inference", "probe", "--model", "mock", "--dry-run"])
    assert code == 0
    plan = json.loads(stdout)
    assert plan["dry_run"] is True
    assert plan["config"]["inference"]["model_id"] == "mock"
    assert not (tmp_path / "runs").exists()


def test_parser_exposes_g6_flags():
    from dice_ratchet.infra.cli import build_parser

    for name in ("probe", "bench", "robustness"):
        args = build_parser().parse_args(["inference", name, "--model", "mock", "--n", "3",
                                          "--seed", "1", "--mode", "eval"])
        assert args.model == "mock" and args.n == 3 and args.seed == 1 and args.mode == "eval"
