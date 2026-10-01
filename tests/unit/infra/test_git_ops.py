"""git 封装测试（FR-09-14）—— T-09-11。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.infra.git_ops import (commit_harness, git_available, rollback_to,
                                     tag_version, worktree_clean_check)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, check=True)
    return out.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"],
                ["config", "user.name", "Tester"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def harness(repo: Path) -> Path:
    h = repo / "harness"
    h.mkdir()
    (h / "prompts").mkdir()
    (h / "prompts" / "decision.md").write_text("v0\n", encoding="utf-8")
    return h


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_worktree_clean_passes_when_clean(repo: Path):
    worktree_clean_check(repo) # 不抛即通过


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_worktree_clean_rejects_dirty(repo: Path):
    (repo / "harness").mkdir()
    (repo / "harness" / "x.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        worktree_clean_check(repo)
    assert exc.value.code == E.E_IO_GIT_DIRTY
    assert exc.value.exit_code == 4


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_commit_harness_message_and_sha(repo: Path, harness: Path):
    # T-09-11：commit 信息以 harness(v3): 开头，返回 40 位 sha
    sha = commit_harness(repo, 3, "add heuristic")
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
    msg = _git(repo, "log", "-1", "--format=%s")
    assert msg == "harness(v3): add heuristic"


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_tag_version_and_skip_when_exists(repo: Path, harness: Path):
    commit_harness(repo, 1, "first")
    assert tag_version(repo, 1) == "harness-v1"
    # 已存在则跳过（幂等）
    assert tag_version(repo, 1) == "harness-v1"
    tags = _git(repo, "tag", "-l", "harness-v*")
    assert tags == "harness-v1"


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_tag_does_not_create_ambiguous_branch(repo: Path, harness: Path):
    commit_harness(repo, 2, "second")
    tag_version(repo, 2)
    # rev-parse 必须无歧义地解析到 tag
    sha = _git(repo, "rev-parse", "harness-v2")
    assert len(sha) == 40


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_rollback_to_restores_harness(repo: Path, harness: Path):
    # 顺序：先写内容再提交，使每个 tag 指向对应版本的内容
    (harness / "prompts" / "decision.md").write_text("v1\n", encoding="utf-8")
    commit_harness(repo, 1, "v1")
    tag_version(repo, 1)
    (harness / "prompts" / "decision.md").write_text("v2\n", encoding="utf-8")
    commit_harness(repo, 2, "v2")
    tag_version(repo, 2)
    (harness / "prompts" / "decision.md").write_text("v3\n", encoding="utf-8")
    commit_harness(repo, 3, "v3")

    rollback_to(repo, 1)
    content = (harness / "prompts" / "decision.md").read_text(encoding="utf-8")
    assert content == "v1\n"


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_rollback_to_missing_tag_rejected(repo: Path, harness: Path):
    commit_harness(repo, 1, "v1")
    with pytest.raises(InfraError) as exc:
        rollback_to(repo, 99)
    assert exc.value.code == E.E_IO_SNAPSHOT_MISSING


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_dirty_repo_blocks_commit(repo: Path, harness: Path):
    # harness/ 外的未提交改动（如源码）必须阻断 harness commit
    commit_harness(repo, 1, "v1")
    (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        commit_harness(repo, 2, "v2")
    assert exc.value.code == E.E_IO_GIT_DIRTY


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_harness_changes_do_not_block_own_commit(repo: Path, harness: Path):
    # harness/ 内的新改动是 commit_harness 的目标本身，不应被门禁拦下
    commit_harness(repo, 1, "v1")
    (harness / "prompts" / "decision.md").write_text("changed\n", encoding="utf-8")
    sha = commit_harness(repo, 2, "v2")
    assert len(sha) == 40


@pytest.mark.skipif(not git_available(), reason="git 不可用")
def test_commit_harness_requires_dir(tmp_path: Path):
    with pytest.raises(InfraError) as exc:
        commit_harness(tmp_path, 1, "x")
    assert exc.value.code == E.E_IO_CONFIG_INVALID
