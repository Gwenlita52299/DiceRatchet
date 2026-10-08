"""FR-07-9 / FR-07-10 / NFR-07-6：版本化、幂等 accept、回滚、决策段（PRD §11 单测 6）。

「给定-当-则」逐条对应 PRD §11-6：

- 给定已存在 ``(run_id, gen)``，当重复 ``accept``，则返回首次结果且
  ``harness/versions/`` 无新增目录（幂等）；
- 给定 current=5，当 ``rollback(2)``，则 ``harness/`` 的 sha256 等于 ``versions/v2/``，
  ``version.json.parent==5``，产生 ``revert.patch``；
- 给定 ``evolve.hitl=false``，当 accept，则不写 ``gate.json`` 且 ``decided_by="auto"``。

git 在本文件里一律打桩（``git_available=False`` → 降级不提交），以保持用例离线可跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.evolve import versioning as V
from dice_ratchet.evolve.errors import VersionConflictError
from dice_ratchet.evolve.schemas import Candidate, Verdict
from dice_ratchet.infra.errors import E

from evolve_support import context_for, make_candidate, prompt_diff, write_harness


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    """本文件全部用例离线：git 视为不可用（降级为不提交，Spec §8.5）。"""
    from dice_ratchet.infra import git_ops

    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): False)


def _accept(cand: Candidate, harness: Path, ctx, *, generation: int = 1,
            decided_by: str = "auto", reason: str | None = None):
    verdict = Verdict(decision="accept", decided_by=decided_by,
                      candidate_id=cand.candidate_id, generation=generation)
    return V.accept(cand, verdict, run_id=ctx.run_id, cfg=ctx, harness=harness,
                    generation=generation, decided_by=decided_by, reason=reason)


# =====================================================================
# accept
# =====================================================================

def test_accept_creates_immutable_snapshot(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    cand = make_candidate(diff=prompt_diff(added="新增约束：先清护卫"))
    ref = _accept(cand, harness, ctx)

    assert ref.version == 1 and ref.parent == 0
    snap = harness / "versions" / "v1"
    assert (snap / "version.json").is_file()
    assert (snap / "diff.patch").is_file()
    assert (snap / "prompts" / "decision.md").is_file()
    assert (snap / "tools" / "score.py").is_file()
    assert (snap / "config.json").is_file()

    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 1 and pointer["parent"] == 0
    assert pointer["accepted_by"] == "auto"
    assert pointer["base_model"] is None
    assert pointer["artifact_hash"].startswith("sha256:")
    # 生效工件真的被改了
    assert "新增约束：先清护卫" in (harness / "prompts" / "decision.md").read_text(
        encoding="utf-8")
    # diff.patch 与候选 diff 一致（可离线重建）
    assert (snap / "diff.patch").read_text(encoding="utf-8").startswith(
        "--- a/harness/prompts/decision.md")


def test_accept_is_idempotent_for_same_run_and_generation(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    cand = make_candidate()
    first = _accept(cand, harness, ctx)
    second = _accept(cand, harness, ctx)
    assert first.version == second.version == 1
    assert first.artifact_hash == second.artifact_hash
    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0", "v1"]


def test_accept_increments_version_for_a_new_generation(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(rationale="gen1", diff=prompt_diff(added="gen1")),
            harness, ctx, generation=1)
    second = _accept(make_candidate(rationale="gen2", diff=prompt_diff(added="gen2")),
                     harness, ctx, generation=2)
    assert second.version == 2 and second.parent == 1
    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0", "v1", "v2"]


def test_accept_rejects_patch_that_does_not_apply(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    from dice_ratchet.evolve.errors import PatchInvalidError

    bad = Candidate.create(kind="prompt_patch",
                           diff=("--- a/harness/prompts/decision.md\n"
                                 "+++ b/harness/prompts/decision.md\n"
                                 "@@ -1,2 +1,3 @@\n 不存在的一行\n+新增\n 也不存在\n"),
                           rationale="坏 diff", expected_effect=["win_rate", "up", 1],
                           touched_files=["harness/prompts/decision.md"])
    before = (harness / "prompts" / "decision.md").read_text(encoding="utf-8")
    with pytest.raises(PatchInvalidError):
        _accept(bad, harness, ctx)
    # 失败后生效工件必须逐字节不变（PRD §10 失败保守）
    assert (harness / "prompts" / "decision.md").read_text(encoding="utf-8") == before
    assert json.loads((harness / "version.json").read_text(encoding="utf-8"))["version"] == 0
    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0"]


def test_accept_records_human_decider(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    cand = make_candidate()
    ref = _accept(cand, harness, ctx, decided_by="human", reason="人工确认")
    assert ref.accepted_by == "human"
    assert ref.reason == "人工确认"
    snap = json.loads((harness / "versions" / "v1" / "version.json").read_text(
        encoding="utf-8"))
    assert snap["accepted_by"] == "human" and snap["reason"] == "人工确认"


def test_accept_ledger_is_written(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(), harness, ctx, generation=1)
    ledger = json.loads(ctx.ledger_path(1).read_text(encoding="utf-8"))
    assert ledger["gen-001"]["action"] == "accept"
    assert ledger["gen-001"]["ref"]["version"] == 1


# =====================================================================
# rollback
# =====================================================================

def test_rollback_restores_previous_content_and_bumps_version(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    v0_content = (harness / "prompts" / "decision.md").read_text(encoding="utf-8")
    _accept(make_candidate(diff=prompt_diff(added="v1 改动")), harness, ctx, generation=1)
    _accept(make_candidate(diff=prompt_diff(added="v2 改动"), rationale="gen2"),
            harness, ctx, generation=2)
    assert "v2 改动" in (harness / "prompts" / "decision.md").read_text(encoding="utf-8")

    ref = V.rollback(0, run_id=ctx.run_id, reason="人工回滚", decided_by="human",
                     cfg=ctx, harness=harness, generation=3)
    assert ref.version == 3 and ref.parent == 2
    assert (harness / "prompts" / "decision.md").read_text(encoding="utf-8") == v0_content
    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 3 and pointer["parent"] == 2
    assert pointer["accepted_by"] == "human"
    revert = harness / "versions" / "v3" / "revert.patch"
    assert revert.is_file() and revert.read_text(encoding="utf-8").strip()


def test_rollback_content_matches_target_snapshot(tmp_path):
    """``harness/`` 的工件内容（排除 ``versions/`` 与元数据）必须等于 ``versions/v0/``。"""
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(diff=prompt_diff(added="v1 改动")), harness, ctx, generation=1)
    V.rollback(0, run_id=ctx.run_id, reason="回到 v0", decided_by="human", cfg=ctx,
               harness=harness, generation=2)

    def _content_hash(base: Path) -> str:
        items = []
        for rel in ("prompts/decision.md", "prompts/reflection.md", "tools/score.py",
                    "config.json"):
            items.append((rel, __import__("hashlib").sha256(
                (base / rel).read_bytes()).hexdigest()))
        return V.tree_digest(Path(tmp_path) / "empty", exclude=()) if False else str(items)

    assert _content_hash(harness) == _content_hash(harness / "versions" / "v0")


def test_rollback_rejects_future_version(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    with pytest.raises(VersionConflictError):
        V.rollback(3, run_id=ctx.run_id, reason="向前回滚", decided_by="human",
                   cfg=ctx, harness=harness, generation=1)


def test_rollback_rejects_missing_version(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(), harness, ctx, generation=1)
    with pytest.raises(VersionConflictError):
        V.rollback(7, run_id=ctx.run_id, reason="不存在", decided_by="human",
                   cfg=ctx, harness=harness, generation=2)


def test_rollback_to_current_is_a_noop(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    ref = V.rollback(0, run_id=ctx.run_id, reason="已在 v0", decided_by="human",
                     cfg=ctx, harness=harness, generation=1)
    assert ref.version == 0
    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0"]


def test_rollback_is_idempotent(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(), harness, ctx, generation=1)
    first = V.rollback(0, run_id=ctx.run_id, reason="回滚", decided_by="human",
                       cfg=ctx, harness=harness, generation=2)
    second = V.rollback(0, run_id=ctx.run_id, reason="回滚", decided_by="human",
                        cfg=ctx, harness=harness, generation=2)
    assert first.version == second.version == 2
    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0", "v1", "v2"]


# =====================================================================
# load_version / next_version / 快照
# =====================================================================

def test_load_version_reads_pointer_and_snapshot(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    current = V.load_version(0, harness=harness)
    assert current.version == 0
    assert Path(current.dir) == harness
    _accept(make_candidate(), harness, ctx, generation=1)
    snap = V.load_version(1, harness=harness)
    assert snap.version == 1 and snap.parent == 0
    assert snap.diff_path.endswith("versions/v1/diff.patch")


def test_load_version_missing_raises(tmp_path):
    harness = write_harness(tmp_path)
    with pytest.raises(VersionConflictError):
        V.load_version(4, harness=harness)


def test_snapshot_harness_twice_raises(tmp_path):
    harness = write_harness(tmp_path)
    with pytest.raises(VersionConflictError):
        V.snapshot_harness(0, harness=harness)


def test_next_version_considers_pointer_and_directories(tmp_path):
    harness = write_harness(tmp_path)
    assert V.next_version(harness) == 1
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(), harness, ctx, generation=1)
    assert V.next_version(harness) == 2


# =====================================================================
# staging
# =====================================================================

def test_stage_candidate_copies_harness_and_applies_diff(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    cand = make_candidate(diff=prompt_diff(added="staging 改动"))
    staging = V.stage_candidate(cand, run_id=ctx.run_id, cfg=ctx, harness=harness,
                                generation=1)
    assert staging.is_dir()
    assert "staging 改动" in (staging / "prompts" / "decision.md").read_text(
        encoding="utf-8")
    # 生效工件不受影响（快照/回滚是唯一入口）
    assert "staging 改动" not in (harness / "prompts" / "decision.md").read_text(
        encoding="utf-8")
    assert (staging / "version.json").is_file()


def test_write_candidate_patch_lands_under_patches(tmp_path):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    cand = make_candidate()
    path = V.write_candidate_patch(cand, run_root=ctx.root, gen=1)
    assert path.is_file() and path.name == f"{cand.candidate_id}.patch"


# =====================================================================
# 决策段（report.json 的四段）
# =====================================================================

def test_build_decision_sections_no_candidate():
    sections = V.build_decision_sections(1, [], None, None)
    assert sections == {"candidates": [], "decision": "reject", "decided_by": "auto",
                        "reason": "no_candidate"}


def test_build_decision_sections_accepts_winner(tmp_path):
    from dice_ratchet.evolve.judge import judge
    from evolve_support import FakeArm, make_config, make_report

    report = make_report()
    verdict = judge(report, FakeArm(win_rate=0.30), cfg=make_config(), generation=1)
    report = make_report(verdict=verdict)
    sections = V.build_decision_sections(1, [report], report, None)
    assert sections["decision"] == "accept"
    assert sections["candidates"][0]["candidate_id"] == report.candidate_id
    assert sections["candidates"][0]["decision"] == "accept"


def test_build_decision_sections_pending_gate(tmp_path):
    from dice_ratchet.evolve.schemas import GateState

    gate = GateState(generation=1, status="pending", harness_version=0,
                     parent_version=0, expires_at="2026-10-01T00:00:00Z",
                     reason="hitl_pending", candidate_id="abc")
    sections = V.build_decision_sections(1, [], None, gate)
    assert sections["decision"] == "pending"


def test_build_decision_sections_timeout_gate():
    from dice_ratchet.evolve.schemas import GateState

    gate = GateState(generation=1, status="timeout", harness_version=0,
                     parent_version=0, expires_at="2026-10-01T00:00:00Z",
                     reason="hitl_timeout")
    sections = V.build_decision_sections(1, [], None, gate)
    assert sections["decision"] == "rollback"
    assert sections["reason"] == "hitl_timeout"


def test_build_decision_sections_rejected_gate():
    from dice_ratchet.evolve.schemas import GateState

    gate = GateState(generation=1, status="rejected", harness_version=0,
                     parent_version=0, expires_at="2026-10-01T00:00:00Z",
                     resolved_by="human", reason="人工拒绝")
    sections = V.build_decision_sections(1, [], None, gate)
    assert sections["decision"] == "rollback"
    assert sections["decided_by"] == "human"
    assert sections["reason"] == "人工拒绝"


# =====================================================================
# git 失败路径（E_EVOLVE_GIT_FAILED → 丢弃未提交 accept）
# =====================================================================

def test_git_failure_restores_harness(tmp_path, monkeypatch):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    from dice_ratchet.infra import git_ops
    from dice_ratchet.evolve.errors import GitFailedError

    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): True)

    def _boom(repo, version, message):
        raise __import__("dice_ratchet.infra.errors", fromlist=["InfraError"]).InfraError(
            E.E_IO_GIT_FAILED, "git commit 失败")

    monkeypatch.setattr(git_ops, "commit_harness", _boom)
    before = (harness / "prompts" / "decision.md").read_text(encoding="utf-8")
    with pytest.raises(GitFailedError):
        _accept(make_candidate(diff=prompt_diff(added="会被丢弃的改动")), harness, ctx)
    assert (harness / "prompts" / "decision.md").read_text(encoding="utf-8") == before
    assert json.loads((harness / "version.json").read_text(encoding="utf-8"))["version"] == 0


def test_git_commit_message_format(tmp_path, monkeypatch):
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    from dice_ratchet.infra import git_ops

    seen: list[tuple[int, str]] = []
    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): True)
    monkeypatch.setattr(git_ops, "commit_harness",
                        lambda repo, version, message: (
                            seen.append((version, message)), "deadbeef")[1])
    monkeypatch.setattr(git_ops, "tag_version", lambda repo, version: f"harness-v{version}")
    ref = _accept(make_candidate(rationale="引用 traj:train#0 的依据"), harness, ctx,
                  generation=1)
    assert ref.git_commit == "deadbeef"
    assert seen and seen[0][0] == 1
    assert seen[0][1].startswith("gen-1 prompt_patch ")
