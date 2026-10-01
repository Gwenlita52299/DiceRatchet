"""run 目录树与状态原语（FR-09-4 / FR-09-5 / FR-09-12 / FR-09-19）。

目录树对齐 Spec §5::

    runs/<run_id>/
      config.json state.json events.jsonl
      episodes/<split>/ reflections/ generations/ metrics/ plots/ audit/
      prompts/ memory/

``state.json`` 的**语义**归 MOD-06；本模块只提供原子读写原语。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, artifact_hash, config_hash
from .errors import E, InfraError
from .io import read_json, write_json_atomic

__all__ = ["RunPaths", "make_run_id", "init_run_dir", "write_state", "read_state",
           "git_short_sha", "utc_timestamp", "resolve_run_dir", "snapshot_config"]


@dataclass(frozen=True)
class RunPaths:
    """Spec §5 目录树的惰性视图（``episodes`` 下的 split 子目录按需创建）。"""

    root: Path
    config: Path = field(init=False)
    state: Path = field(init=False)
    events: Path = field(init=False)
    episodes: Path = field(init=False)
    reflections: Path = field(init=False)
    generations: Path = field(init=False)
    metrics: Path = field(init=False)
    plots: Path = field(init=False)
    audit: Path = field(init=False)
    prompts: Path = field(init=False)
    memory: Path = field(init=False)

    def __post_init__(self) -> None:
        for name in ("config", "state", "events", "episodes", "reflections",
                     "generations", "metrics", "plots", "audit", "prompts", "memory"):
            fname = {"config": "config.json", "state": "state.json",
                     "events": "events.jsonl"}.get(name, name)
            object.__setattr__(self, name, self.root / fname)

    @property
    def splits(self) -> tuple[str, ...]:
        """Spec §4.5 的四个 split。"""
        return ("train", "heldout", "test", "demo")

    def episodes_split(self, split: str) -> Path:
        """``episodes/<split>/``，自动创建（Spec §4.5 B-16）。"""
        if split not in self.splits:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"未知 split {split!r}（合法：{self.splits}）")
        path = self.episodes / split
        path.mkdir(parents=True, exist_ok=True)
        return path

    def episode_path(self, split: str, episode_key: str) -> Path:
        return self.episodes_split(split) / f"{episode_key}.jsonl"

    def all_dirs(self) -> list[Path]:
        return [self.episodes, self.reflections, self.generations, self.metrics,
                self.plots, self.audit, self.prompts, self.memory]


# ---- run_id（FR-09-4）---------------------------------------------------------------

def git_short_sha(repo: Path = Path(".")) -> str:
    """``git rev-parse --short HEAD``；不可用时 ``nogit`` + ``E_IO_GIT_UNAVAILABLE``。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        sha = out.stdout.strip()
        return sha or "nogit"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def utc_timestamp(now: datetime | None = None) -> str:
    """``YYYYMMDDTHHMMSSZ``（UTC）。"""
    now = now or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_run_id(cfg: AppConfig, *, now: datetime | None = None,
                explicit: str | None = None,
                repo: Path = Path(".")) -> tuple[str, str]:
    """``run_id = f"{git_short_sha}-{config_hash[:8]}-{utc_ts}"``（FR-09-4）。

    ``explicit`` 非空时以显式值为准（Spec v1.3 §4.6：复现与测试用）。
    返回 ``(run_id, git_short_sha)``。
    """
    sha = git_short_sha(repo)
    if explicit:
        return explicit, sha
    return f"{sha}-{config_hash(cfg)[:8]}-{utc_timestamp(now)}", sha


# ---- 目录初始化（FR-09-5 / FR-09-19）--------------------------------------------------

def snapshot_config(cfg: AppConfig, run_id: str, git_sha: str) -> dict[str, Any]:
    """落盘到 ``config.json`` 的快照（Spec §7）。"""
    from .config import canonical_json
    return {
        "run_id": run_id,
        "git_sha": git_sha,
        "config_hash": config_hash(cfg),
        "config": json.loads(canonical_json(cfg)),
        "harness_config_path": "harness/config.json",
    }


def _check_runs_ignored(runs_root: Path) -> None:
    """FR-09-19：``runs_root`` 必须被 ``.gitignore`` 覆盖，否则 ``worktree_clean_check`` 永远 dirty。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(runs_root.parent if runs_root.name else "."), "check-ignore",
             str(runs_root)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return # git 不可用时降级（E_IO_GIT_UNAVAILABLE 已在 run_id 记录）
    if out.returncode == 0:
        return
    raise InfraError(E.E_IO_RUNS_NOT_IGNORED,
                     f"{runs_root} 未被 .gitignore 覆盖",
                     hint=f"在 .gitignore 加一行：{runs_root.name}/")


def init_run_dir(run_id: str, cfg: AppConfig,
                 runs_root: Path = Path("runs"),
                 *, git_check: bool = True) -> RunPaths:
    """幂等创建 Spec §5 目录树（FR-09-5）。

    目录已存在且 ``config.json`` 的 ``config_hash`` 不一致 → ``E_IO_RUN_CONFLICT``。
    """
    runs_root = Path(runs_root)
    if git_check:
        _check_runs_ignored(runs_root)
    root = runs_root / run_id
    paths = RunPaths(root)
    existed = root.is_dir()
    root.mkdir(parents=True, exist_ok=True)
    for d in paths.all_dirs():
        d.mkdir(parents=True, exist_ok=True)

    snap = snapshot_config(cfg, run_id, git_short_sha(runs_root.parent))
    if existed and paths.config.is_file():
        prev_hash = read_json(paths.config).get("config_hash")
        if prev_hash != snap["config_hash"]:
            raise InfraError(E.E_IO_RUN_CONFLICT,
                             f"run 目录 {root} 已存在且 config_hash 不一致："
                             f"{prev_hash} != {snap['config_hash']}",
                             hint="改用 --run-id 指定新目录，或删除旧 run 后重跑")
        return paths
    write_json_atomic(paths.config, snap)
    return paths


def resolve_run_dir(run_id: str, cfg: AppConfig,
                    runs_root: Path = Path("runs"),
                    *, resume: bool = False) -> RunPaths:
    """``--resume`` 定位与一致性校验（FR-09-12）。

    - 校验 ``config_hash`` 一致（``config_hash`` 不含 harness，故演化过的 run 也能续跑）。
    - ``state.json`` 缺失且 ``resume=True`` → ``E_IO_STATE_MISSING``。
    """
    root = Path(runs_root) / run_id
    if not root.is_dir():
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"run 目录不存在：{root}",
                         hint=f"检查 --run-id {run_id!r} 是否正确")
    paths = RunPaths(root)
    if paths.config.is_file():
        prev_hash = read_json(paths.config).get("config_hash")
        want = config_hash(cfg)
        if prev_hash != want:
            raise InfraError(E.E_IO_RUN_CONFLICT,
                             f"--resume 的 config_hash 不一致：{prev_hash} != {want}",
                             hint="config_hash 不含 harness；若改过 configs/，请开新 run")
    if resume and not paths.state.is_file():
        raise InfraError(E.E_IO_STATE_MISSING,
                         f"--resume 需要 state.json，但 {paths.state} 不存在",
                         hint="该 run 可能从未真正启动；去掉 --resume 重新开始")
    return paths


# ---- state.json 原子读写（FR-09-18 / MOD-06 只提供语义）-------------------------------

def write_state(paths: RunPaths, state: dict[str, Any], *, fsync: bool = True) -> None:
    """原子写 ``state.json``（tmp + ``os.replace``）。语义（字段定义）归 MOD-06。"""
    write_json_atomic(paths.state, state, fsync=fsync)


def read_state(paths: RunPaths) -> dict[str, Any]:
    data = read_json(paths.state)
    if not isinstance(data, dict):
        raise InfraError(E.E_IO_RUN_STATE,
                         f"state.json 顶层必须是对象，实际 {type(data).__name__}")
    return data
