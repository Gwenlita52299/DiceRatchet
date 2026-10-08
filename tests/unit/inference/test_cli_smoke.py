"""MOD-03 CLI 单测冒烟（G6 命令面 + 未达阈值退出码）。

与 ``tests/integration/test_inference_cli.py`` 的分工：

- 集成套件跑 **真实** 的 ``probe`` / ``bench`` / ``robustness``（``--model mock``）端到端；
- 本文件只补集成套件**观测不到**的确定性分支：``robustness`` 未达 M1 阈值时的
  **退出码 1**（``cmd_robustness`` 的 ``return 0 if report["passed"] else 1``）、报告落盘到
  run 默认 ``metrics/`` 目录，以及 ``probe`` 的错误路径退出码。

这两条路径此前只存在于 ``--model qwen3-8b-q4`` 的真实跑批里（集成用例的 mock 恒达阈值），
所以单测覆盖率命令 ``pytest tests/unit/inference --cov=dice_ratchet.inference`` 里
``cli.py`` 长期显示 0%——补上本文件后该口径不再有"命令面无人守"的假象。

注意：``mock`` 模型的 ``weights_path`` 为 ``None``，真实路径会命中
``if spec.weights_path is None`` 的空测量分支（0 次调用）而**依然返回 0**；
因此未达阈值这条分支用 monkeypatch 直接替换 ``_robustness`` 的返回值来确定性地驱动。
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from dice_ratchet import app
from dice_ratchet.infra import events
from dice_ratchet.inference import cli as inference_cli


@pytest.fixture(autouse=True)
def _isolated_run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``runs/`` 落到 tmp，避免污染仓库；并解绑事件（与集成套件同款隔离）。"""
    monkeypatch.setenv("DICERATCHET_INFRA__RUNS_ROOT", str(tmp_path / "runs"))
    events.unbind()
    yield
    events.unbind()


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = app.main(argv, reexec=False)
    return code, out.getvalue(), err.getvalue()


def test_robustness_below_threshold_exits_one_and_still_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """未达 M1 阈值 → 退出码 1（领域失败，而非 2/3 的配置/模型错误），报告照旧落盘。"""

    async def _failing(*_args, **_kwargs):
        return {"kind": "format_robustness", "passed": False, "m1_evidence": False,
                "overall": {"first_pass_valid_rate": 0.5, "schema_fail_rate": 0.2}}

    monkeypatch.setattr(inference_cli, "_robustness", _failing)
    code, stdout, stderr = _run(["inference", "robustness", "--model", "mock",
                                 "--run-id", "u-rob-fail", "--mode", "eval", "--seed", "7",
                                 "--n", "5"])
    assert code == 1, (stdout, stderr)
    report = json.loads((tmp_path / "runs" / "u-rob-fail" / "metrics"
                         / "format_robustness.json").read_text(encoding="utf-8"))
    assert report["passed"] is False


def test_robustness_meeting_threshold_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """达阈值 → 退出码 0，且 ``--out`` 覆盖默认落点。"""
    out_path = tmp_path / "rob.json"

    async def _passing(*_args, **_kwargs):
        return {"kind": "format_robustness", "passed": True, "m1_evidence": False,
                "overall": {"first_pass_valid_rate": 1.0, "schema_fail_rate": 0.0}}

    monkeypatch.setattr(inference_cli, "_robustness", _passing)
    code, stdout, stderr = _run(["inference", "robustness", "--model", "mock",
                                 "--run-id", "u-rob-ok", "--mode", "eval", "--seed", "7",
                                 "--n", "5", "--out", str(out_path)])
    assert code == 0, (stdout, stderr)
    assert json.loads(out_path.read_text(encoding="utf-8"))["passed"] is True


def test_probe_unknown_model_exits_three_and_writes_no_report(tmp_path: Path):
    """未知模型 → 退出码 3（E_LLM_UNAVAILABLE），且不产出报告。

    ``init_run_dir`` 会**预先**建好 ``metrics/`` 目录，所以这里断言的是报告文件不存在，
    而不是目录不存在（目录存在是正常的，别把它写成"失败时不落任何目录"）。
    """
    code, _, stderr = _run(["inference", "probe", "--model", "does-not-exist",
                            "--run-id", "u-probe-bad", "--mode", "eval", "--seed", "7"])
    assert code == 3, stderr
    assert "E_LLM_UNAVAILABLE" in stderr
    metrics = tmp_path / "runs" / "u-probe-bad" / "metrics"
    assert not (metrics / "inference_probe.json").exists()
    assert list(metrics.glob("*.json")) == []
