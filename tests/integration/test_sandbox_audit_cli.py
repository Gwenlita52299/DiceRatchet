"""MOD-02 `audit leak` 端到端集成（PRD §11 集成用例；G4/G6）。

真跑 ``python -m gamer_rsi audit leak --run-id <id>``（子进程 + 临时 cwd），验证
退出码 0/2、报告落盘、E_ENV_AUDIT_IO 判 critical。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli_workspace(tmp_path, build_run):
    """临时工作区：含 configs/（含 seeds.yaml）与 runs/<id>/。"""
    shutil.copytree(REPO / "configs", tmp_path / "configs")

    def make(*, run_id: str, **kwargs):
        built = build_run(tmp_path / "runs" / "_staging", run_id=run_id, **kwargs)
        target = tmp_path / "runs" / run_id
        target.parent.mkdir(parents=True, exist_ok=True)
        built.root.rename(target)
        return target

    return tmp_path, make


def _run_cli(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-m", "gamer_rsi", *argv],
                          cwd=str(cwd), env=env, capture_output=True, text=True,
                          timeout=300)


def test_cli_audit_leak_clean_run_exits_0(cli_workspace):
    workspace, make = cli_workspace
    make(run_id="it-clean")
    proc = _run_cli(workspace, "audit", "leak", "--run-id", "it-clean")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "verdict=clean" in proc.stdout and "0 finding" in proc.stdout
    report = workspace / "runs" / "it-clean" / "audit" / "leak-leak.json"
    assert report.is_file()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["verdict"] == "clean" and payload["findings"] == []
    assert payload["honeypot"]["caught_rate"] == 1.0 # G4
    assert oct(report.stat().st_mode)[-3:] == "600"


def test_cli_audit_leak_canary_run_exits_2(cli_workspace):
    workspace, make = cli_workspace
    from gamer_rsi.sandbox.honeypot import HONEYPOT_CANARY

    root = make(run_id="it-canary")
    episode = next((root / "episodes" / "train").glob("*.jsonl"))
    episode.write_text(episode.read_text(encoding="utf-8").replace(
        '"llm_raw": null', f'"llm_raw": "泄漏 {HONEYPOT_CANARY}"', 1), encoding="utf-8")
    proc = _run_cli(workspace, "audit", "leak", "--run-id", "it-canary")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    payload = json.loads((root / "audit" / "leak-leak.json").read_text(encoding="utf-8"))
    assert payload["verdict"] == "critical"
    assert payload["leak_counts"].get("canary_leak", 0) >= 1


def test_cli_audit_leak_seed_leak_exits_2(cli_workspace):
    workspace, make = cli_workspace
    make(run_id="it-seed", prompt_text="heldout seed=2013 的经验\n")
    proc = _run_cli(workspace, "audit", "leak", "--run-id", "it-seed")
    assert proc.returncode == 2
    assert "seed_leak=1" in proc.stdout


def test_cli_audit_leak_corrupt_jsonl_is_audit_io(cli_workspace):
    workspace, make = cli_workspace
    make(run_id="it-broken", corrupt_log=True)
    proc = _run_cli(workspace, "audit", "leak", "--run-id", "it-broken")
    assert proc.returncode == 2
    assert "E_ENV_AUDIT_IO" in proc.stdout + proc.stderr


def test_cli_audit_leak_missing_run_id_argument(cli_workspace):
    workspace, _ = cli_workspace
    proc = _run_cli(workspace, "audit", "leak")
    assert proc.returncode == 2
    assert "run-id" in (proc.stdout + proc.stderr)


def test_cli_audit_leak_ghost_run_is_audit_io(cli_workspace):
    workspace, _ = cli_workspace
    proc = _run_cli(workspace, "audit", "leak", "--run-id", "ghost")
    assert proc.returncode != 0
    assert "E_ENV_AUDIT_IO" in proc.stdout + proc.stderr


def test_cli_audit_dry_run_does_not_write(cli_workspace):
    workspace, make = cli_workspace
    root = make(run_id="it-dry")
    proc = _run_cli(workspace, "audit", "leak", "--run-id", "it-dry", "--dry-run")
    assert proc.returncode == 0
    assert not (root / "audit" / "leak-leak.json").exists()


def test_cli_audit_machinery_failure_exits_4(cli_workspace, monkeypatch, capsys):
    """蜜罐失效（审计器自身守卫违例）→ 退出码 4；报告仍记 verdict=critical（PRD §18.2）。"""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from gamer_rsi.app import main # 组合根：注册 MOD-02 命令实现后才是完整 CLI
    from gamer_rsi.sandbox import audit as A
    from gamer_rsi.sandbox.honeypot import HoneypotSelfTestResult

    workspace, make = cli_workspace
    root = make(run_id="it-machinery")
    bad = HoneypotSelfTestResult(cases=12, caught=0, missed=())
    monkeypatch.setattr(A.HP, "self_test", lambda: bad)
    monkeypatch.chdir(workspace)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["audit", "leak", "--run-id", "it-machinery"], reexec=False)
    text = out.getvalue() + err.getvalue()
    assert code == 4, text
    assert "E_ENV_HONEYPOT" in text and "exit=4" in text
    payload = json.loads((root / "audit" / "leak-leak.json").read_text(encoding="utf-8"))
    assert payload["verdict"] == "critical"
    assert payload["machinery_errors"] == ["E_ENV_HONEYPOT"]
