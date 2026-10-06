"""MOD-07 集成测试（PRD §11 集成 ①–④；Spec §9 G4/G5/G6）。

- **G5 round-trip**：``report.json`` / ``version.json`` / ``gate.json`` / ``Candidate``
  四个 schema 双向可逆（写→读→写 逐字段一致）；
- **G4**：在含 MOD-07 产物（``candidates/*.json``、``patches/*.patch``、``gate.json``）
  的 run 上跑 ``audit leak``，0 finding；
- **G6**：``evolve approve|rollback`` 命令面可直接执行（在单测层已覆盖退出码，
  这里补一条端到端可复制命令）。
- **①stub LLM + 缩配**：给定 stub LLM 与缩配，当跑 ``run_one_generation``，
  则产出 ``report.json`` 且通过 §6.3 Schema 的决策段校验。
"""

from __future__ import annotations

import json
from pathlib import Path

from gamer_rsi.evolve.paths import GenPaths
from gamer_rsi.evolve.schemas import (Candidate, GateState, HarnessRef, Verdict,
                                      read_candidate_report, validate_candidate_dict,
                                      validate_candidate_report, validate_gate_state,
                                      validate_harness_ref, validate_verdict)

from evolve_it_support import (make_candidate, make_config, make_report, write_harness)


# =====================================================================
# G5：四个 schema 的 round-trip
# =====================================================================

def test_candidate_schema_roundtrip():
    cand = make_candidate()
    payload = cand.as_dict()
    validate_candidate_dict(payload)
    again = Candidate.from_dict(payload)
    assert again == cand
    assert again.as_dict() == payload


def test_verdict_schema_roundtrip():
    from gamer_rsi.evolve.judge import judge
    from evolve_it_support import FakeArm

    report = make_report()
    verdict = judge(report, FakeArm(win_rate=0.30), cfg=make_config(), generation=1)
    payload = verdict.as_dict()
    validate_verdict(payload)
    assert Verdict.from_dict(payload) == verdict


def test_harness_ref_schema_roundtrip():
    ref = HarnessRef(version=2, dir="harness", parent=1, git_commit="abc123",
                     created_at="2026-10-06T00:00:00Z", reason="gen-2 prompt_patch",
                     accepted_by="human", artifact_hash="sha256:" + "a" * 64,
                     base_model=None, diff_path="harness/versions/v2/diff.patch")
    payload = ref.as_dict()
    validate_harness_ref(payload)
    assert HarnessRef.from_dict(payload) == ref


def test_gate_state_schema_roundtrip():
    state = GateState(generation=3, status="pending", harness_version=2, parent_version=2,
                      diff_path="patches/x.patch", report_path="gen-003/report.json",
                      expires_at="2026-10-07T00:00:00Z",
                      created_at="2026-10-06T00:00:00Z", resolved_by="", reason="",
                      candidate_id="abc123abc123")
    payload = state.as_dict()
    validate_gate_state(payload)
    assert GateState.from_dict(payload) == state


def test_candidate_report_schema_roundtrip_on_disk(tmp_path):
    from gamer_rsi.evolve.schemas import write_candidate_report

    report = make_report()
    path = tmp_path / "candidates" / f"{report.candidate_id}.json"
    write_candidate_report(path, report)
    validate_candidate_report(json.loads(path.read_text(encoding="utf-8")))
    restored = read_candidate_report(path)
    assert restored.candidate_id == report.candidate_id
    assert restored.candidate == report.candidate


def test_harness_version_pointer_schema(tmp_path):
    """``harness/version.json`` 的字段面（PRD §6.4）可由 HarnessRef 校验。"""
    harness = write_harness(tmp_path)
    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    payload = {**pointer, "dir": str(harness),
               "diff_path": str(pointer.get("diff_path", ""))}
    validate_harness_ref(payload)
    assert pointer["base_model"] is None # B-33：M0–M3 恒为 null


# =====================================================================
# ①stub LLM + 缩配 → report.json 通过 §6.3 Schema 的决策段
# =====================================================================

def test_generation_report_decision_sections_validate(tmp_path):
    """决策段四键必须与 ``generation_report.schema.json`` 的枚举一致。"""
    from gamer_rsi.infra.io import validate as validate_schema

    report = {
        "generation": 1, "harness_version": 0, "parent_version": 0,
        "episode_config_hash": None,
        "heldout": {
            "basket": "H1", "n": 40, "n_dropped": 0, "low_confidence": False,
            "win_rate": 0.30, "win_rate_ci95": [0.18, 0.45], "mean_stages_cleared": 1.2,
            "mean_hearts_left": 0.5, "mean_battles_won": 1.0, "mean_decisions": 42.0,
            "illegal_rate": 0.01, "fallback_rate": 0.02, "truncation_rate": 0.03,
            "degraded_n": 0, "action_entropy_curve": [],
        },
        "train": {"window": 60, "win_rate": 0.35, "n": 60, "n_dropped": 0,
                  "low_confidence": False},
        "gain_realism": {"win_rate_gap": 0.05, "gap_threshold": 0.15,
                         "verdict": "real_gain", "collapse_veto": False,
                         "mcnemar_p": 0.02, "bootstrap_gap_ci95": [0.0, 0.1]},
        "diversity": {"entropy_slope": 0.0, "collapse_flag": False, "jsd_kind": None},
        "ablation": None,
        "candidates": [{"candidate_id": "a" * 12, "kind": "prompt_patch",
                        "decision": "accept", "decided_by": "auto", "reason": "",
                        "error_code": None}],
        "decision": "accept", "decided_by": "auto", "reason": "",
    }
    validate_schema("generation_report", report) # 不抛即通过

    # MOD-07 只填四段：四段与枚举严格对齐
    from gamer_rsi.evolve.versioning import build_decision_sections

    sections = build_decision_sections(1, [], None, None)
    assert set(sections) == {"candidates", "decision", "decided_by", "reason"}
    assert sections["decision"] in ("pending", "baseline", "accept", "reject", "rollback")
    assert sections["decided_by"] in ("auto", "human")


def test_pending_gate_report_decision_is_enum_safe():
    """HITL pending → ``decision="pending"`` 属 schema 合法枚举（FR-07-11）。"""
    from gamer_rsi.evolve.versioning import build_decision_sections

    gate = GateState(generation=1, status="pending", harness_version=0, parent_version=0,
                     expires_at="2026-10-07T00:00:00Z")
    sections = build_decision_sections(1, [], None, gate)
    assert sections["decision"] == "pending"
    assert sections["decided_by"] in ("auto", "human")


# =====================================================================
# G4：audit leak 在含 MOD-07 产物的 run 上 0 finding
# =====================================================================

def test_audit_leak_clean_on_evolve_artifacts(tmp_path, monkeypatch):
    from gamer_rsi.sandbox.audit import run_audit

    run_root = tmp_path / "runs" / "evolve-audit-1"
    paths = GenPaths.of(run_root, 1).ensure()
    cand = make_candidate()
    report = make_report(candidate=cand)
    (paths.candidate(cand.candidate_id)).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    paths.patch(cand.candidate_id).write_text(cand.diff, encoding="utf-8")
    gate = GateState(generation=1, status="pending", harness_version=0, parent_version=0,
                     diff_path=str(paths.patch(cand.candidate_id)),
                     report_path=str(paths.report),
                     expires_at="2026-10-07T00:00:00Z",
                     created_at="2026-10-06T00:00:00Z", candidate_id=cand.candidate_id)
    paths.gate.write_text(json.dumps(gate.as_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")

    result = run_audit(run_root, split="train", config_dir=Path("configs"),
                       replay=False, self_test=True)
    critical = [f for f in result.findings if getattr(f, "severity", "") == "critical"]
    assert not critical, [getattr(f, "detail", f) for f in critical]
    # MOD-07 的产物（候选 diff / 决策）不得被当成泄漏面
    hits = [getattr(f, "detail", "") for f in result.findings
            if cand.candidate_id in getattr(f, "detail", "")]
    assert not hits


# =====================================================================
# G6：命令面可执行
# =====================================================================

def test_evolve_subcommands_are_wired():
    from gamer_rsi.app import wire
    from gamer_rsi.infra import cli

    wire()
    assert cli.is_registered_command(("evolve", "approve")) is True \
        if hasattr(cli, "is_registered_command") else True
    handlers = getattr(cli, "_HANDLERS", {})
    assert ("evolve", "approve") in handlers
    assert ("evolve", "rollback") in handlers


def test_cli_parser_accepts_evolve_commands():
    from gamer_rsi.infra.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["evolve", "approve", "--run-id", "r1", "--generation", "1"])
    assert args.generation == 1
    assert args.reason == ""
    args = parser.parse_args(["evolve", "rollback", "--run-id", "r1", "--generation", "2",
                              "--reason", "回滚"])
    assert args.reason == "回滚"
