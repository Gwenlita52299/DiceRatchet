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
import shutil

import pytest

from dice_ratchet.evolve import versioning as V
from dice_ratchet.evolve.errors import VersionConflictError
from dice_ratchet.evolve.schemas import Candidate, Verdict
from dice_ratchet.infra.errors import E, InfraError

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
# F47-C2：失败自清（不留快照 ⇒ 不毒化版本号）
# =====================================================================

def _git_failure(monkeypatch, *, code=E.E_IO_GIT_DIRTY):
    """让 ``commit_harness`` 以「脏工作区」失败（``commit_harness_version`` 会包成
    ``GitFailedError``）；``tag_version`` 一并打桩，避免 tmp 目录不是 git 仓库。"""
    from dice_ratchet.infra import git_ops

    def _boom(repo, version, message):
        raise InfraError(code, "git worktree 不干净（1 项），拒绝演化")

    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): True)
    monkeypatch.setattr(git_ops, "commit_harness", _boom)
    monkeypatch.setattr(git_ops, "tag_version", lambda repo, version: f"harness-v{version}")


def _git_success(monkeypatch):
    from dice_ratchet.infra import git_ops

    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): True)
    monkeypatch.setattr(git_ops, "commit_harness", lambda repo, version, message: "a" * 40)
    monkeypatch.setattr(git_ops, "tag_version", lambda repo, version: f"harness-v{version}")


def test_accept_git_failure_leaves_no_snapshot_and_can_retry(tmp_path, monkeypatch):
    """F47-C2：accept 在建完快照**之后**才失败 ⇒ 必须删掉那一个快照。

    残留快照的**实际**危害（见反例自检，2026-10-10 实测口径）：``next_version`` =
    ``max(指针, 现有目录) + 1``，所以残留目录不会引发 ``VersionConflictError``，而是
    ①**顶掉一个版本号**（版本序列出现永远填不上的空洞）；
    ②成为一个**能被 ``load_version`` / ``rollback`` 取用的幻影版本**——它没有账本记录、
    ``git_commit`` 为空，是对「不可变已采纳版本」这一语义的伪造。
    """
    from dice_ratchet.evolve.errors import GitFailedError

    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    cand = make_candidate(diff=prompt_diff(added="新增约束：先清护卫"))
    before = (harness / "prompts" / "decision.md").read_text(encoding="utf-8")

    _git_failure(monkeypatch)
    with pytest.raises(GitFailedError):
        _accept(cand, harness, ctx, generation=1)

    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0"], \
        "失败不得留下本次新建的快照"
    assert json.loads((harness / "version.json").read_text(encoding="utf-8"))["version"] == 0
    assert (harness / "prompts" / "decision.md").read_text(encoding="utf-8") == before, \
        "失败必须恢复生效工件（PRD §10 失败保守）"

    # ---- 反例自检：残留快照不是无害垃圾（证明上面那条清理断言是承重的）------------
    stray = harness / "versions" / "v1"
    shutil.copytree(harness / "versions" / "v0", stray)
    payload = json.loads((stray / "version.json").read_text(encoding="utf-8"))
    payload["version"] = 1
    (stray / "version.json").write_text(json.dumps(payload), encoding="utf-8")
    assert V.next_version(harness) == 2, "残留快照会顶掉版本号 v1（序列空洞）"
    phantom = V.load_version(1, harness=harness)
    assert phantom.version == 1 and not phantom.git_commit, "幻影版本可被 load 到（伪造记录）"
    shutil.rmtree(stray)

    # ---- 正例：清理干净后同一代重试成功，版本号从 v1 起连续 ----------------------
    _git_success(monkeypatch)
    ref = _accept(cand, harness, ctx, generation=1)
    assert ref.version == 1 and ref.parent == 0
    assert sorted(p.name for p in (harness / "versions").iterdir()) == ["v0", "v1"]


def test_rollback_git_failure_leaves_no_snapshot(tmp_path, monkeypatch):
    """F47-C2（回滚侧）：``rollback()`` 新建的快照同样必须在失败时删掉。

    注意区分两个名字：``snap`` 是**回滚目标**（只读，绝不能删），``new_snap`` 才是本次新建。
    """
    from dice_ratchet.evolve.errors import GitFailedError

    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    _accept(make_candidate(diff=prompt_diff(added="v1 改动")), harness, ctx, generation=1)
    before = sorted(p.name for p in (harness / "versions").iterdir()) # ["v0", "v1"]
    assert before == ["v0", "v1"]

    _git_failure(monkeypatch)
    with pytest.raises(GitFailedError):
        V.rollback(0, run_id=ctx.run_id, reason="人工回滚", decided_by="human",
                   cfg=ctx, harness=harness, generation=2)

    assert sorted(p.name for p in (harness / "versions").iterdir()) == before, \
        "回滚失败不得留下本次新建的快照（也不得误删回滚目标）"
    assert json.loads((harness / "version.json").read_text(encoding="utf-8"))["version"] == 1


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
    # F62（2026-10-14）：C2 改判「play 面确实变了」⇒ 必须把两个指纹传进判定器。
    verdict = judge(report, FakeArm(win_rate=0.30), cfg=make_config(), generation=1,
                    play_hash="sha256:cand", parent_play_hash="sha256:parent")
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


# =====================================================================
# D2=A（2026-10-14）：拒绝的**轻量版本推进** —— 推进指针 + commit，但
# **不建全量快照、不打 tag**（用户裁决：`v1 = v0 + 仅 P1 不可行的提示词`）
# =====================================================================

def test_record_rejection_bumps_pointer_without_snapshot_or_tag(tmp_path, monkeypatch):
    """拒绝 ⇒ ``version.json`` 进到 v1，但 ``versions/v1/`` **不存在**、**不打 tag**。

    为什么这是用户口径而不是实现细节：play 工件逐字节没变（唯一变化是
    ``dead_ends.md``），复制一份与 v0 完全相同的快照会让「版本 = 一份不同的 harness」
    这个不变量失真；而 ``harness-v{N}`` 是「可回滚的版本」的标记，这里没有可回滚的内容。
    """
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    from dice_ratchet.infra import git_ops

    tags: list[int] = []
    messages: list[str] = []
    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): True)
    monkeypatch.setattr(git_ops, "commit_harness",
                        lambda repo, version, message: (
                            messages.append(message), "c0ffee")[1])
    monkeypatch.setattr(git_ops, "tag_version",
                        lambda repo, version: tags.append(int(version)) or "t")

    ref = V.record_rejection(run_id=ctx.run_id, generation=1, reason="C2:no_significant_gain",
                             cfg=ctx, harness=harness)
    assert ref.version == 1 and ref.parent == 0
    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 1 and pointer["git_commit"] == "c0ffee"
    assert pointer["reason"] == "C2:no_significant_gain"
    assert not (harness / "versions" / "v1").exists(), "拒绝不得复制全量快照"
    assert tags == [], "拒绝不得打 tag（没有可回滚的内容）"
    assert messages and messages[0].startswith("gen-1 reject ")
    # 幂等：同代重复调用返回首次结果，不产生第二个版本号
    again = V.record_rejection(run_id=ctx.run_id, generation=1, reason="C2:no_significant_gain",
                               cfg=ctx, harness=harness)
    assert again.version == 1
    assert json.loads((harness / "version.json").read_text(encoding="utf-8"))["version"] == 1


def test_record_rejection_ledger_has_no_versions_key(tmp_path):
    """拒绝的账本条目**不带** ``versions`` 键。

    为什么必须这样：``accept()`` 的幂等分支按 ``prior["versions"]`` 判「这一代已产生过
    版本号」，命中就 ``load_version(max(existing))``。拒绝推进的版本号**没有快照**，
    带上它会让一次「拒绝后重跑该代并接受」以 ``VersionConflictError`` 炸掉整代。
    """
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    V.record_rejection(run_id=ctx.run_id, generation=2, reason="C1:no_gain",
                       cfg=ctx, harness=harness)
    ledger = V.read_ledger(ctx.ledger_path(2))
    entry = ledger["gen-002"]
    assert entry["action"] == "reject" and entry["bumped_version"] == 1
    assert "versions" not in entry


def test_record_rejection_git_failure_rolls_back_pointer(tmp_path, monkeypatch):
    """git 提交失败 ⇒ **指针回滚**（不留「幻影版本号」）+ 抛 ``E_EVOLVE_GIT_FAILED``。

    现场纪律（PRD §10 失败保守 / F47-D）：版本没落成，就不能声称 ``v_{N+1}`` 已存在，
    否则下一次 accept 会在「历史里有一个不存在的版本」之上继续编号。
    """
    harness = write_harness(tmp_path)
    ctx = context_for(tmp_path, harness=harness)
    from dice_ratchet.infra import git_ops
    from dice_ratchet.evolve.errors import GitFailedError

    monkeypatch.setattr(git_ops, "git_available", lambda repo=Path("."): True)

    def _boom(repo, version, message):
        raise InfraError(E.E_IO_GIT_FAILED, "git commit 失败")

    monkeypatch.setattr(git_ops, "commit_harness", _boom)
    with pytest.raises(GitFailedError):
        V.record_rejection(run_id=ctx.run_id, generation=1, reason="C2", cfg=ctx,
                           harness=harness)
    assert json.loads((harness / "version.json").read_text(encoding="utf-8"))["version"] == 0


# =====================================================================
# play 面指纹（D-M3-7）：版本号在拒绝时也推进 ⇒ 「同版本 ⇔ 同起点」不再成立
# =====================================================================

def test_play_hash_ignores_learning_side_and_dead_ends(tmp_path):
    """``play_hash`` 只看 play 面：``reflection.md`` 与 ``dead_ends.md`` 都不算。

    - ``reflection.md`` 影响的是「反思 → 记忆 → 下一轮 prompt 的内容」（学习侧输入）；
    - ``dead_ends.md`` 是**只给提案方看**的死路记录（D1=A）。
    两者都不改变「同一局面下怎么打」。这条是用户的步骤 3 在代码上的落点。
    """
    harness = write_harness(tmp_path)
    before = V.play_hash(harness)
    (harness / "prompts" / "reflection.md").write_text("# 换了反思模板\n", encoding="utf-8")
    (harness / "dead_ends.md").write_text("## gen-001 · x\n- 结局：reject\n",
                                          encoding="utf-8")
    assert V.play_hash(harness) == before
    assert V.play_plane_files(harness) == ("config.json", "prompts/decision.md",
                                           "tools/score.py")


def test_play_hash_tracks_play_plane_and_deletions(tmp_path):
    """play 面的三个来源（decision.md / config.json / tools/*.py）任一变化都必须换指纹。

    删掉一个 play 文件也要换（``"<missing>"`` 占位）——否则「删掉工具」会被当成
    「什么都没变」而继承一份不再适用的 heldout 测量。
    """
    harness = write_harness(tmp_path)
    base = V.play_hash(harness)
    (harness / "prompts" / "decision.md").write_text("# 改了打法\n", encoding="utf-8")
    changed = V.play_hash(harness)
    assert changed != base
    (harness / "prompts" / "decision.md").write_text(
        (Path(harness) / "versions" / "v0" / "prompts" / "decision.md").read_text(
            encoding="utf-8"), encoding="utf-8")
    assert V.play_hash(harness) == base
    (harness / "config.json").write_text('{"sampling": {"temperature": 0.9}}\n',
                                         encoding="utf-8")
    assert V.play_hash(harness) != base
    (harness / "tools" / "score.py").unlink()
    assert V.play_hash(harness) != base
