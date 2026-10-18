"""F-M3-6：accept / reject / rollback 之后**工作区必须干净**，且 ``git_commit`` 自洽。

m（2026-10-18，mvp1 现场）实测的两个缺陷，用**真 git 仓库**回归（其余 versioning 用例
把 git 打桩，覆盖不到这两条）：

1. **备份目录进了版本历史**：accept 先把 ``harness/`` 备份到 ``harness/.backup-v{parent}/``，
   再 ``git add harness`` ⇒ 备份文件被提交；随后 ``_restore`` 又删掉它们 ⇒ 工作区永久脏，
   下一次 ``loop run`` 被 F47-A（``E_IO_GIT_DIRTY``）拦死。
2. **``git_commit`` 是提交后才回写的**：写进文件却没再提交 ⇒ HEAD 的 ``version.json``
   记的是**上一次**提交的 sha（把 v6 标成 v5）。

修法：备份移出仓库（临时目录），``commit_harness`` 额外排除 ``.backup-*``；
版本化提交拆成「快照提交」+「sha 回填提交」，tag 打在回填提交上。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dice_ratchet.evolve import versioning as V
from dice_ratchet.evolve.schemas import Verdict
from dice_ratchet.infra.git_ops import git_available

from evolve_support import context_for, make_candidate, prompt_diff, write_harness


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, check=True)
    return out.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """真 git 仓库 + 已提交的 ``harness/``（v0），``runs/`` 进 .gitignore。"""
    if not git_available():
        pytest.skip("git 不可用")
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"],
                ["config", "user.name", "Tester"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    harness = write_harness(repo)
    for path in sorted((repo / "harness").rglob("*")):
        assert path.exists()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init harness v0")
    assert harness.is_dir()
    return repo


def test_accept_leaves_clean_worktree_and_self_consistent_sha(repo: Path):
    harness = repo / "harness"
    ctx = context_for(repo, harness=harness)
    cand = make_candidate(diff=prompt_diff(added="新增约束：先清护卫"))
    verdict = Verdict(decision="accept", decided_by="auto",
                      candidate_id=cand.candidate_id, generation=1)

    ref = V.accept(cand, verdict, run_id=ctx.run_id, cfg=ctx, harness=harness,
                   generation=1)

    # ① 工作区干净（修复前：version.json 与 versions/v1/version.json 留在改动里）
    assert _git(repo, "status", "--porcelain") == ""
    # ① 备份目录没有进版本历史（修复前：.backup-v0/* 被提交）
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not [p for p in tracked if ".backup-" in p], tracked
    # ② git_commit == 快照提交的 sha，且该提交确实包含版本快照
    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["git_commit"] == ref.git_commit
    files = _git(repo, "ls-tree", "-r", "--name-only", ref.git_commit).splitlines()
    assert "harness/versions/v1/version.json" in files
    # ② tag 打在回填提交上：checkout 出来的 version.json 自洽（tag 内容 == 工作区内容）
    assert _git(repo, "rev-parse", "harness-v1") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "status", "--porcelain") == ""
    # 快照里也记录同一个 sha（load_version 读它）
    snap_pointer = json.loads(
        (harness / "versions" / "v1" / "version.json").read_text(encoding="utf-8"))
    assert snap_pointer["git_commit"] == pointer["git_commit"]


def test_record_rejection_leaves_clean_worktree(repo: Path):
    """拒绝也推进版本号（D2=A）：同样必须干净 + 记录**自己的**提交。"""
    harness = repo / "harness"
    ctx = context_for(repo, harness=harness)
    (harness / "dead_ends.md").write_text("## gen-001 · 已否决\n", encoding="utf-8")

    ref = V.record_rejection(run_id=ctx.run_id, generation=1, reason="C1:win_rate",
                             cfg=ctx, harness=harness)

    assert _git(repo, "status", "--porcelain") == ""
    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 1 and pointer["git_commit"] == ref.git_commit
    # 拒绝不建快照、不打 tag（D2=A）
    assert not (harness / "versions" / "v1").exists()
    assert _git(repo, "tag", "-l", "harness-v1") == ""
