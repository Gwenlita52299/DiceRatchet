"""run 目录树与状态原语（FR-09-4 / FR-09-5 / FR-09-12 / FR-09-19）。

目录树对齐 Spec §5::

    runs/<run_id>/
      config.json state.json events.jsonl
      episodes/<split>/ reflections/ generations/ metrics/ plots/ audit/
      prompts/ memory/

``state.json`` 的**语义**归 MOD-06；本模块只提供原子读写原语。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (AppConfig, config_drift, config_hash, env_applied_keys, full_config,
                     harness_overridden_keys)
from .errors import E, InfraError
from .io import read_json, write_json_atomic

__all__ = ["RunPaths", "make_run_id", "init_run_dir", "write_state", "read_state",
           "git_short_sha", "utc_timestamp", "resolve_run_dir", "snapshot_config",
           "describe_config_drift"]

#: 冲突提示里每类最多列出的键数（其余折成「另 N 个」，避免提示刷屏）
_MAX_DRIFT_LISTED: int = 8


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

def snapshot_config(cfg: AppConfig, run_id: str, git_sha: str,
                    content_hash: str | None = None) -> dict[str, Any]:
    """落盘到 ``config.json`` 的快照（Spec §7 / FR-09-5）。

    含：完整 AppConfig（``full_config``，非 hash 裁剪视图）、``config_hash``、
    ``content_hash``（MOD-01 配表摘要，可缺省）、git sha、生效的 ``DICERATCHET_*`` 白名单，
    以及被 harness 层覆盖的键（B-19：这些键不计入 config_hash）。
    """
    return {
        "run_id": run_id,
        "git_sha": git_sha,
        "config_hash": config_hash(cfg),
        "content_hash": content_hash,
        "harness_config_path": "harness/config.json",
        "harness_overridden_keys": list(harness_overridden_keys(cfg)),
        "env_whitelist": list(env_applied_keys(cfg)),
        "config": full_config(cfg),
    }


def describe_config_drift(prev_snap: Any, cfg: AppConfig) -> str:
    """把 ``config_hash`` 冲突的成因说清（B-90 / F79）；无 ``config`` 快照时返回空串。

    现场：任何键的增删（升版本、改 ``configs/``、删死键）都会让历史 run 无法 ``--resume``
    ——这是 FR-09-3 的既定 fail-closed 行为，**不打算放开**（放开即等于允许两套配置写同一个
    run，见 F79 选项②被否）。此前的问题只是提示语不区分「值变了」与「结构变了」：前者是
    误改配置，后者是正常的配置演进，排查方向相反却给出同一句话。
    """
    if not isinstance(prev_snap, dict):
        return ""
    prev = prev_snap.get("config")
    if not isinstance(prev, dict):
        return ""
    keys = list(prev_snap.get("harness_overridden_keys") or ())
    keys += list(harness_overridden_keys(cfg))
    drift = config_drift(prev, cfg, harness_keys=keys)
    added, removed, changed = drift["added"], drift["removed"], drift["changed"]
    if not (added or removed or changed):
        return ("配置差异不在扁平键层面（键集相同、值也相同）⇒ 成因是 harness 基线或键的嵌套"
                "结构变化，不是「某个值被改坏」")
    lines = [f"配置漂移（config_hash 口径）：新增 {len(added)} 键 / 删除 {len(removed)} 键 / "
             f"值变化 {len(changed)} 键"]
    for label, items in (("新增", added), ("删除", removed), ("值变化", changed)):
        if items:
            head = items[:_MAX_DRIFT_LISTED]
            tail = f" …（另 {len(items) - len(head)} 个）" if len(items) > len(head) else ""
            lines.append(f"  {label}：{', '.join(head)}{tail}")
    if added or removed:
        lines.append("  提示：键的增删属**配置结构**变化（改过 configs/ 或升过代码版本），"
                     "不是把某个值改坏了；历史 run 不能 --resume 是 FR-09-3 的既定行为")
    return "\n".join(lines)


def _check_runs_ignored(runs_root: Path) -> None:
    """FR-09-19：``runs_root`` 必须被 ``.gitignore`` 覆盖，否则 ``worktree_clean_check`` 永远 dirty。

    ``git check-ignore`` 的退出码语义：``0`` 已忽略 / ``1`` 未忽略 / ``>=2`` 出错误
    （不是 git 仓库、git 太旧等）。出错误时按"可观测性缺失 → 降级"处理（PRD §10），
    只有明确的"未忽略"才阻断——否则在非 git 目录里连一次冒烟 run 都建不起来。
    """
    runs_root = Path(runs_root)
    base = runs_root.parent if str(runs_root.parent) else "."
    # 传相对路径：绝对路径在 macOS 上会因 /tmp -> /private/tmp 的符号链接前缀不匹配
    rel = runs_root.name or "."
    try:
        out = subprocess.run(
            ["git", "-C", str(base), "check-ignore", rel],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return # git 不可用时降级（E_IO_GIT_UNAVAILABLE 已在 run_id 记录）
    if out.returncode == 0:
        return # 已忽略
    if out.returncode != 1:
        return # >=2：git 不可用/非仓库 → 降级，不阻断
    raise InfraError(E.E_IO_RUNS_NOT_IGNORED,
                     f"{runs_root} 未被 .gitignore 覆盖",
                     hint=f"在 .gitignore 加一行：{runs_root.name}/")


def init_run_dir(run_id: str, cfg: AppConfig,
                 runs_root: Path = Path("runs"),
                 *, git_check: bool = True,
                 content_hash: str | None = None) -> RunPaths:
    """幂等创建 Spec §5 目录树（FR-09-5）。

    创建 ``config.json``、``events.jsonl``、``episodes/<split>/``（四个 split）、
    ``reflections/``、``generations/``、``metrics/``、``plots/``、``audit/``、
    ``prompts/``、``memory/``。目录已存在且 ``config.json`` 的 ``config_hash``
    不一致 → ``E_IO_RUN_CONFLICT``（禁止两套配置写同一 run）。

    ``content_hash`` 由调用方（组合根经 ``cli.register_content_hash_provider``）传入，
    MOD-01 的配表摘要不该让 infra 反向依赖 env（NFR-09-5）。
    """
    runs_root = Path(runs_root)
    root = runs_root / run_id
    paths = RunPaths(root)
    existed = root.is_dir()
    root.mkdir(parents=True, exist_ok=True)
    for d in paths.all_dirs():
        d.mkdir(parents=True, exist_ok=True)
    # FR-09-5：episodes/<split>/ 四类 split 必须一次建全（下游按 Spec §5 假设存在）
    for split in paths.splits:
        paths.episodes_split(split)
    # events.jsonl 先落空文件，保证「run 目录即完整可读」
    paths.events.touch(exist_ok=True)
    # 目录存在后再查 .gitignore：带尾斜杠的模式只匹配已存在的目录（FR-09-19）
    if git_check:
        _check_runs_ignored(runs_root)

    snap = snapshot_config(cfg, run_id, git_short_sha(runs_root.parent),
                           content_hash=content_hash)
    if existed and paths.config.is_file():
        prev_snap = read_json(paths.config)
        prev_hash = prev_snap.get("config_hash")
        if prev_hash != snap["config_hash"]:
            drift = describe_config_drift(prev_snap, cfg)
            raise InfraError(E.E_IO_RUN_CONFLICT,
                             f"run 目录 {root} 已存在且 config_hash 不一致："
                             f"{prev_hash} != {snap['config_hash']}"
                             + (f"\n{drift}" if drift else ""),
                             hint="改用 --run-id 指定新目录，或删除旧 run 后重跑")
        return paths
    write_json_atomic(paths.config, snap)
    return paths


def resolve_run_dir(run_id: str, cfg: AppConfig,
                    runs_root: Path = Path("runs"),
                    *, resume: bool = False,
                    strict_hash: bool = True) -> RunPaths:
    """``--resume`` / 只读命令的定位与一致性校验（FR-09-12）。

    - 校验 ``config_hash`` 一致（``config_hash`` 不含 harness，故演化过的 run 也能续跑）。
    - ``state.json`` 缺失且 ``resume=True`` → ``E_IO_STATE_MISSING``。
    - ``strict_hash=False``（F55，2026-10-12）：**只读命令**用——定位已有 run 但**不**校验
      ``config_hash``。理由：「两套配置不许写同一个 run」是**写侧**的不变量；只读命令看的是
      已落盘产物，而它们的 CLI 面本来就不含 run 的创建参数（如 `--generations`），
      硬校验只会把「合法的只读」误判成冲突（实测：`rehearsal check` 读不了
      `loop run --generations 2` 的 run）。**写侧调用方不得传 False。**
    """
    root = Path(runs_root) / run_id
    if not root.is_dir():
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"run 目录不存在：{root}",
                         hint=f"检查 --run-id {run_id!r} 是否正确")
    paths = RunPaths(root)
    if strict_hash and paths.config.is_file():
        prev_snap = read_json(paths.config)
        prev_hash = prev_snap.get("config_hash")
        want = config_hash(cfg)
        if prev_hash != want:
            drift = describe_config_drift(prev_snap, cfg)
            raise InfraError(E.E_IO_RUN_CONFLICT,
                             f"--resume 的 config_hash 不一致：{prev_hash} != {want}"
                             + (f"\n{drift}" if drift else ""),
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
