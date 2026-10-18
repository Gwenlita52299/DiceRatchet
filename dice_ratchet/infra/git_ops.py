"""git 操作封装（FR-09-14）。

- ``worktree_clean_check()``：工作区干净性判据，脏 → ``E_IO_GIT_DIRTY``。
- ``preflight_worktree_clean()``：**起跑前**门禁（F47-A）——MOD-06 在演化开始前调用，
  使脏工作区在**任何推理调用之前**失败（原先唯一的调用点在 ``commit_harness`` 内部，
  即整代跑完之后，白花一整代且被记成「候选被拒」）。
- ``commit_harness(version, message)``：提交 ``harness/``，信息格式 ``harness(v{N}): {msg}``。
  **先门禁后暂存**（F47-C1）：失败时不留 staged 残留。
- ``tag_version(N)``：打轻量 tag ``harness-v{N}``（已存在则跳过；不建同名分支，避免
  ``refname is ambiguous``）。
- ``rollback_to(N)``：``git checkout harness-v{N} -- harness/`` 恢复工件目录。

git 不可用时统一降级为 ``E_IO_GIT_UNAVAILABLE``（Spec §8.5：可观测性缺失降级+告警）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from . import events
from .errors import E, InfraError

__all__ = ["git_available", "worktree_clean_check", "preflight_worktree_clean",
           "commit_harness", "tag_version", "rollback_to", "harness_tag", "git_run",
           "HARNESS_DIR"]

HARNESS_DIR: str = "harness"


def git_run(args: list[str], repo: Path, *, check: bool = True,
            capture: bool = True) -> subprocess.CompletedProcess[str]:
    """运行 git 子命令。

    区分两类失败（PRD §10「可复现性缺失 → 立即失败，绝不静默继续」）：

    - git **不可用**（找不到可执行文件 / 超时）→ ``E_IO_GIT_UNAVAILABLE``（退出码 0 的降级码）；
    - git 可用但**命令失败**（如未配置 user.name、hook 拒绝、tag 冲突）→
      ``E_IO_GIT_FAILED``（退出码 2）——绝不伪装成"不可用"悄悄成功。
    """
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=capture, text=True, check=check, timeout=30)
    except FileNotFoundError as exc:
        raise InfraError(E.E_IO_GIT_UNAVAILABLE, "git 可执行文件未找到") from exc
    except subprocess.TimeoutExpired as exc:
        raise InfraError(E.E_IO_GIT_UNAVAILABLE, f"git 命令超时：{args[:2]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise InfraError(E.E_IO_GIT_FAILED,
                         f"git {' '.join(args[:2])} 失败（exit {exc.returncode}）"
                         + (f"：{detail.splitlines()[-1]}" if detail else ""),
                         hint="检查 git 身份配置（user.name/user.email）与仓库状态；"
                              "该失败不可降级，run 不应继续") from exc
    except subprocess.SubprocessError as exc: # pragma: no cover
        raise InfraError(E.E_IO_GIT_UNAVAILABLE, f"git 调用失败：{exc}") from exc


def git_available(repo: Path = Path(".")) -> bool:
    try:
        git_run(["rev-parse", "--is-inside-work-tree"], repo)
        return True
    except InfraError:
        return False


def harness_tag(version: int) -> str:
    return f"harness-v{version}"


def worktree_clean_check(repo: Path = Path("."), *,
                         allow: Sequence[str] = ()) -> None:
    """工作区必须干净（演化前门禁）。脏 → ``E_IO_GUARD_DIRTY``。

    ``allow`` 前缀下的改动被忽略：``commit_harness()`` 用它放行即将提交的 ``harness/``，
    但仍要求工作区其余部分干净（否则演化会夹带未提交的源码改动）。
    """
    status = git_run(["status", "--porcelain"], repo)
    dirty = [ln for ln in status.stdout.splitlines()
             if ln.strip() and not _is_skipped(ln, tuple(allow))]
    if dirty:
        raise InfraError(E.E_IO_GIT_DIRTY,
                         f"git worktree 不干净（{len(dirty)} 项），拒绝演化",
                         hint="先提交或丢弃改动；runs/ 等产物应已在 .gitignore 内（FR-09-19）")


def _is_skipped(line: str, allow: tuple[str, ...]) -> bool:
    path = line[3:].strip().strip('"')
    if path.startswith("runs/") or path.startswith("logs/"):
        return True
    return any(path == a or path.startswith(a.rstrip("/") + "/") for a in allow)


def _unstage(repo: Path) -> None:
    """把 ``harness/`` 从索引里撤出（**只动索引**，不碰工作区）。

    用于 ``commit_harness`` 的失败路径（F47-C1）：门禁拒绝时不得留下 staged 残留。
    ``git reset -- <pathspec>`` 在无 HEAD（空仓库）时会失败，故 ``check=False``；
    撤索引失败不覆盖原始异常（调用方正在 except 里）。
    """
    try:
        git_run(["reset", "--quiet", "--", HARNESS_DIR], repo, check=False)
    except InfraError: # pragma: no cover - git 已不可用：原始异常更重要
        pass


def preflight_worktree_clean(repo: Path = Path("."), *,
                             allow: Sequence[str] = ()) -> bool:
    """**跑批/演化起跑前**的工作区门禁（F47-A：FR-09-14 的「演化前门禁」）。

    与 ``commit_harness`` 内部的同名检查是**同一判据、不同时机**：那份在整代跑完、
    即将落版本时才触发，脏工作区会白白花掉一整代（实测 3.75 h / ≈¥47）并在报告里
    表现为「候选被拒」。本函数把它前移到**任何推理调用之前**。

    返回是否真的检查过：``False`` = 该路径不在 git worktree 内（git 不可用/临时目录），
    按 Spec §8.5「可观测性缺失 → 降级」跳过，不阻断 run。

    ``allow`` 语义同 ``worktree_clean_check``（前缀白名单）。
    """
    if not git_available(repo):
        return False
    worktree_clean_check(repo, allow=allow)
    return True


def commit_harness(repo: Path, version: int, message: str) -> str:
    """提交 ``harness/``，返回 40 位 commit sha。"""
    repo = Path(repo)
    harness = repo / HARNESS_DIR
    if not harness.is_dir():
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"harness 目录不存在：{harness}",
                         hint="先初始化 harness/（MOD-07 versioning 负责）")
    full_msg = f"harness(v{version}): {message}"
    # 门禁**先于**暂存（F47-C1）：``git add`` 会把 harness/ 写进索引，而门禁失败时
    # 原先只在 accept 侧恢复**工作区**文件，索引里的 staged 条目无人清理 ⇒ 一次脏工作区
    # 会留下 staged 残留（实测 ``MM harness/decision.md``）。
    # 以 allow=harness/ 放行即将提交的本目录改动，但工作区其余任何改动仍阻断演化。
    try:
        worktree_clean_check(repo, allow=(HARNESS_DIR,))
        # F-M3-6（2026-10-18）：``.backup-*`` 是 accept/rollback 的回滚备份目录。备份本体
        # 已移到仓库外的临时目录，这里再兜一层——崩溃遗留的旧备份绝不能进版本历史
        # （mvp1 现场：accept 的 commit 里混进 6 个备份文件，随后又被删掉 ⇒ 工作区永久脏）。
        git_run(["add", "--all", "--", HARNESS_DIR,
                 f":(exclude){HARNESS_DIR}/.backup-*"], repo)
        # 空提交保护：无变化时 git commit --allow-empty 保留版本动作
        result = git_run(["diff", "--cached", "--quiet"], repo, check=False)
        args = ["commit", "-m", full_msg] if result.returncode != 0 else \
            ["commit", "--allow-empty", "-m", full_msg]
        git_run(args, repo)
    except InfraError:
        # 本函数不得留下索引残留（否则门禁失败会污染调用方的后续操作）
        _unstage(repo)
        raise
    sha = git_run(["rev-parse", "HEAD"], repo).stdout.strip()
    events.emit("evolve.version", from_version=version - 1, to_version=version,
                git_commit=sha, action="commit")
    return sha


def tag_version(repo: Path, version: int) -> str:
    """打轻量 tag ``harness-v{N}``；已存在则跳过。

    **不建同名分支**（避免 ``refname is ambiguous``），也**不删除**任何已存在的分支——
    PRD FR-09-14 只要求前者；销毁用户数据不属于本函数职责（2026-10-03 审计 P1-11）。
    rollback/校验一律走全限定 ``refs/tags/harness-v{N}``，因此与同名分支并存无歧义风险。
    """
    tag = harness_tag(version)
    exists = git_run(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
                     repo, check=False)
    if exists.returncode == 0:
        return tag
    git_run(["tag", tag], repo)
    return tag


def rollback_to(repo: Path, version: int) -> str:
    """以 ``git checkout harness-v{N} -- harness/`` 恢复工件目录，返回新 commit sha。"""
    repo = Path(repo)
    tag = harness_tag(version)
    verify = git_run(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
                     repo, check=False)
    if verify.returncode != 0:
        raise InfraError(E.E_IO_SNAPSHOT_MISSING,
                         f"回滚目标 tag 不存在：{tag}",
                         hint=f"可用 tag：{git_run(['tag', '-l', 'harness-v*'], repo).stdout.split()}")
    git_run(["checkout", tag, "--", HARNESS_DIR], repo)
    git_run(["add", HARNESS_DIR], repo)
    result = git_run(["diff", "--cached", "--quiet"], repo, check=False)
    if result.returncode != 0:
        git_run(["commit", "-m", f"harness(rollback): revert to {tag}"], repo)
    sha = git_run(["rev-parse", "HEAD"], repo).stdout.strip()
    events.emit("evolve.rollback", to_version=version, git_commit=sha)
    return sha
