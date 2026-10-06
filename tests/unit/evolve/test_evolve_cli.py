"""``evolve approve|rollback`` 的 CLI 单测（Spec §4.6；PRD FR-07-11）。

命令面（G6 可直接复制执行）::

    python -m gamer_rsi evolve approve  --run-id <run_id> --generation 1
    python -m gamer_rsi evolve rollback --run-id <run_id> --generation 1 --reason "..."

退出码：``0`` 成功 / ``2`` 配置错误（缺 ``--run-id``、run 目录不存在、无 ``gate.json``）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from gamer_rsi.evolve import cli as EV
from gamer_rsi.evolve.paths import GenPaths
from gamer_rsi.infra.errors import E, InfraError

from evolve_support import make_candidate, make_config


def _args(run_id: str, *, generation: int = 1, reason: str = "",
          config_dir: str = "configs") -> argparse.Namespace:
    return argparse.Namespace(run_id=run_id, generation=generation, reason=reason,
                              config_dir=config_dir, k=None, generations=None,
                              hitl=None, model=None, mode=None, seed=None,
                              timeout_ms=None, log_level=None)


def _pending_gate(tmp_path, *, run_id: str, generation: int = 1) -> Path:
    """在 ``runs/<run_id>/generations/gen-{N}/gate.json`` 造一个 pending gate。"""
    from gamer_rsi.evolve import gate as G

    root = tmp_path / "runs" / run_id
    cand = make_candidate()
    state = G.await_human(generation=generation, run_id=run_id,
                          diff_path=root / "x.patch",
                          report_path=GenPaths.of(root, generation).report,
                          cfg=make_config(hitl=True), run_dir=root, winner=cand,
                          parent_version=0, harness_version=0)
    assert state.status == "pending"
    return GenPaths.of(root, generation).gate


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    """把 ``infra.runs_root`` 指向 tmp（默认 ``runs`` 相对 cwd）。"""
    from gamer_rsi.infra import config as cfg_mod

    real_load = cfg_mod.load_config

    def _load(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        return cfg

    return tmp_path


def _patch_runs_root(monkeypatch, tmp_path):
    """让 ``evolve.cli`` 读到的 ``cfg.infra.runs_root`` 指向 ``tmp_path/runs``。"""
    from dataclasses import replace

    from gamer_rsi.infra import config as cfg_mod

    real_load = cfg_mod.load_config

    def _load(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        infra = replace(cfg.infra, runs_root=str(tmp_path / "runs"))
        return replace(cfg, infra=infra)

    monkeypatch.setattr(EV.infra_cli, "load_config", _load)


# =====================================================================
# 成功路径
# =====================================================================

def test_approve_marks_gate_approved(tmp_path, monkeypatch, capsys):
    _patch_runs_root(monkeypatch, tmp_path)
    gate = _pending_gate(tmp_path, run_id="run-1", generation=1)
    code = EV.cmd_evolve_approve(_args("run-1", generation=1, reason="人工确认"))
    assert code == 0
    payload = json.loads(gate.read_text(encoding="utf-8"))
    assert payload["status"] == "approved"
    assert payload["resolved_by"] == "human"
    assert payload["reason"] == "人工确认"
    printed = json.loads(capsys.readouterr().out)
    assert printed["action"] == "approve"
    assert printed["gate"]["status"] == "approved"


def test_rollback_marks_gate_rejected(tmp_path, monkeypatch):
    _patch_runs_root(monkeypatch, tmp_path)
    gate = _pending_gate(tmp_path, run_id="run-2", generation=2)
    code = EV.cmd_evolve_rollback(_args("run-2", generation=2, reason="效果可疑"))
    assert code == 0
    payload = json.loads(gate.read_text(encoding="utf-8"))
    assert payload["status"] == "rejected"
    assert payload["reason"] == "效果可疑"


def test_handlers_are_registered():
    assert set(EV.HANDLERS) == {("evolve", "approve"), ("evolve", "rollback")}


def test_resolution_is_idempotent(tmp_path, monkeypatch):
    _patch_runs_root(monkeypatch, tmp_path)
    _pending_gate(tmp_path, run_id="run-3", generation=1)
    EV.cmd_evolve_approve(_args("run-3", generation=1))
    EV.cmd_evolve_rollback(_args("run-3", generation=1, reason="事后反悔"))
    gate = GenPaths.of(tmp_path / "runs" / "run-3", 1).gate
    assert json.loads(gate.read_text(encoding="utf-8"))["status"] == "approved"


# =====================================================================
# 错误路径
# =====================================================================

def test_missing_run_id_is_config_error(tmp_path, monkeypatch):
    _patch_runs_root(monkeypatch, tmp_path)
    with pytest.raises(InfraError) as excinfo:
        EV.cmd_evolve_approve(_args("", generation=1))
    assert str(excinfo.value.code) == str(E.E_IO_CONFIG_INVALID)


def test_missing_run_dir_is_config_error(tmp_path, monkeypatch):
    _patch_runs_root(monkeypatch, tmp_path)
    with pytest.raises(InfraError) as excinfo:
        EV.cmd_evolve_approve(_args("nope", generation=1))
    assert "run 目录不存在" in str(excinfo.value)


def test_missing_gate_file_is_rejected(tmp_path, monkeypatch):
    _patch_runs_root(monkeypatch, tmp_path)
    (tmp_path / "runs" / "run-4" / "generations" / "gen-001").mkdir(parents=True)
    with pytest.raises(InfraError) as excinfo:
        EV.cmd_evolve_approve(_args("run-4", generation=1))
    assert "gate.json" in str(excinfo.value)


def test_legacy_gate_without_generation_dir_is_rejected(tmp_path, monkeypatch):
    _patch_runs_root(monkeypatch, tmp_path)
    (tmp_path / "runs" / "run-5").mkdir(parents=True)
    with pytest.raises(InfraError):
        EV.cmd_evolve_rollback(_args("run-5", generation=9))
