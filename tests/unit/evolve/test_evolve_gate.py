"""FR-07-11：HITL 门（pending 落盘 / 人工裁决 / 超时保守回滚）——PRD §11 单测 6 的后半。

「给定-当-则」逐条对应 PRD §11-6：

- 给定 ``evolve.hitl=false``，当 accept，则**不写** ``gate.json`` 且 ``decided_by="auto"``；
- 给定 ``hitl=true`` 且 gate 未响应至超时，则 ``decision="rollback"``、``reason="hitl_timeout"``。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gamer_rsi.evolve import gate as G
from gamer_rsi.evolve.paths import GenPaths
from gamer_rsi.infra.errors import E, InfraError

from evolve_support import make_candidate, make_config


def _await(tmp_path, cfg, *, generation: int = 1, winner=None, now=None):
    cand = winner or make_candidate()
    return G.await_human(generation=generation, run_id="run-1",
                         diff_path=tmp_path / "patches" / f"{cand.candidate_id}.patch",
                         report_path=GenPaths.of(tmp_path, generation).report,
                         cfg=cfg, run_dir=tmp_path, winner=cand, parent_version=0,
                         harness_version=0, now=now)


# =====================================================================
# hitl=false：不落 gate.json，直接 approved
# =====================================================================

def test_hitl_disabled_does_not_write_gate_file(tmp_path):
    state = _await(tmp_path, make_config(hitl=False))
    assert state.status == "approved"
    assert state.resolved_by == "auto"
    assert not GenPaths.of(tmp_path, 1).gate.exists()


def test_hitl_disabled_ignores_existing_pending_gate(tmp_path):
    cfg = make_config(hitl=True)
    _await(tmp_path, cfg)
    assert GenPaths.of(tmp_path, 1).gate.is_file()
    state = _await(tmp_path, make_config(hitl=False))
    assert state.status == "approved"


# =====================================================================
# hitl=true：pending 落盘
# =====================================================================

def test_hitl_enabled_writes_pending_gate_file(tmp_path):
    state = _await(tmp_path, make_config(hitl=True, gate_timeout_hours=24))
    assert state.status == "pending"
    path = GenPaths.of(tmp_path, 1).gate
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("generation", "harness_version", "parent_version", "diff_path",
                "report_path", "status", "expires_at"):
        assert key in payload, key
    assert payload["status"] == "pending"
    assert payload["expires_at"] > payload["created_at"]


def test_pending_gate_is_idempotent(tmp_path):
    cfg = make_config(hitl=True)
    first = _await(tmp_path, cfg)
    second = _await(tmp_path, cfg)
    assert first.status == second.status == "pending"
    assert first.expires_at == second.expires_at


def test_expires_at_honours_timeout_hours(tmp_path):
    cfg = make_config(hitl=True, gate_timeout_hours=1)
    state = _await(tmp_path, cfg)
    created = datetime.strptime(state.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    expires = datetime.strptime(state.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    assert expires - created == timedelta(hours=1)


# =====================================================================
# 人工裁决（resolve_gate）
# =====================================================================

@pytest.mark.parametrize("action,expected", [("approve", "approved"),
                                             ("rollback", "rejected")])
def test_resolve_gate_transitions(tmp_path, action, expected):
    _await(tmp_path, make_config(hitl=True))
    state = G.resolve_gate(generation=1, run_id="run-1", action=action,
                           reason="人工理由", run_dir=tmp_path)
    assert state.status == expected
    assert state.resolved_by == "human"
    assert state.reason == "人工理由"
    on_disk = json.loads(GenPaths.of(tmp_path, 1).gate.read_text(encoding="utf-8"))
    assert on_disk["status"] == expected


def test_resolve_gate_requires_gate_file(tmp_path):
    with pytest.raises(InfraError) as excinfo:
        G.resolve_gate(generation=3, run_id="run-1", action="approve", run_dir=tmp_path)
    assert "gate.json" in str(excinfo.value)


def test_resolve_gate_is_idempotent_after_decision(tmp_path):
    _await(tmp_path, make_config(hitl=True))
    first = G.resolve_gate(generation=1, run_id="run-1", action="approve",
                           run_dir=tmp_path)
    second = G.resolve_gate(generation=1, run_id="run-1", action="rollback",
                            run_dir=tmp_path)
    assert first.status == second.status == "approved" # 只前进一步，不可回退


def test_resolve_gate_rejects_unknown_action(tmp_path):
    _await(tmp_path, make_config(hitl=True))
    with pytest.raises(InfraError):
        G.resolve_gate(generation=1, run_id="run-1", action="explode", run_dir=tmp_path)


def test_await_returns_resolved_gate_on_resume(tmp_path):
    cfg = make_config(hitl=True)
    _await(tmp_path, cfg)
    G.resolve_gate(generation=1, run_id="run-1", action="approve", run_dir=tmp_path)
    state = _await(tmp_path, cfg)
    assert state.status == "approved"
    assert state.resolved_by == "human"


# =====================================================================
# 超时 → 保守回滚
# =====================================================================

def test_expired_pending_becomes_timeout(tmp_path):
    state = _await(tmp_path, make_config(hitl=True, gate_timeout_hours=1))
    future = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    expired = G.expire_if_needed(state, run_dir=tmp_path, now=future)
    assert expired.status == "timeout"
    assert expired.reason == "hitl_timeout"
    assert expired.error_code == str(E.E_EVOLVE_GATE_TIMEOUT)
    on_disk = json.loads(GenPaths.of(tmp_path, 1).gate.read_text(encoding="utf-8"))
    assert on_disk["status"] == "timeout"


def test_gate_expired_detects_deadline(tmp_path):
    state = _await(tmp_path, make_config(hitl=True, gate_timeout_hours=1))
    now = datetime.now(tz=timezone.utc)
    assert G.gate_expired(state, now=now) is False
    assert G.gate_expired(state, now=now + timedelta(hours=3)) is True


def test_gate_expired_ignores_resolved_states(tmp_path):
    state = _await(tmp_path, make_config(hitl=True, gate_timeout_hours=1))
    resolved = G.resolve_gate(generation=1, run_id="run-1", action="approve",
                              run_dir=tmp_path)
    future = datetime.now(tz=timezone.utc) + timedelta(days=10)
    assert G.gate_expired(resolved, now=future) is False


def test_timeout_verdict_is_rollback(tmp_path):
    state = _await(tmp_path, make_config(hitl=True, gate_timeout_hours=1))
    future = datetime.now(tz=timezone.utc) + timedelta(hours=5)
    expired = G.expire_if_needed(state, run_dir=tmp_path, now=future)
    verdict = G.verdict_from_gate(expired)
    assert verdict.decision == "rollback"
    assert verdict.reason == "hitl_timeout"
    assert verdict.error_code == str(E.E_EVOLVE_GATE_TIMEOUT)


def test_raise_for_timeout_raises_gate_timeout(tmp_path):
    from gamer_rsi.evolve.errors import GateTimeoutError

    state = _await(tmp_path, make_config(hitl=True, gate_timeout_hours=1))
    future = datetime.now(tz=timezone.utc) + timedelta(hours=5)
    expired = G.expire_if_needed(state, run_dir=tmp_path, now=future)
    with pytest.raises(GateTimeoutError):
        G.raise_for_timeout(expired)


def test_verdict_from_approved_gate():
    from gamer_rsi.evolve.schemas import GateState

    state = GateState(generation=2, status="approved", harness_version=0,
                      parent_version=0, expires_at="", resolved_by="human",
                      reason="人工确认")
    verdict = G.verdict_from_gate(state)
    assert verdict.decision == "accept" and verdict.decided_by == "human"


def test_write_gate_validates_schema(tmp_path):
    from gamer_rsi.evolve.schemas import GateState

    bad = GateState(generation=0, status="pending", harness_version=0, parent_version=0,
                    expires_at="")
    with pytest.raises(InfraError):
        G.write_gate(tmp_path / "gate.json", bad)


def test_read_gate_returns_none_when_missing(tmp_path):
    assert G.read_gate(tmp_path / "nope.json") is None
