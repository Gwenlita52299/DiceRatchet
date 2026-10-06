"""run 级状态机与计分台账（MOD-06 PRD §6/§7.1/§7.3；FR-06-8/9/10）。

本模块是 ``runs/<run_id>/state.json`` 的**语义所有者**：字段定义、状态转移、原子落盘、
计分幂等去重。目录与原子写原语归 MOD-09（``infra.run.RunPaths`` /
``infra.run.write_state`` / ``infra.io.write_json_atomic``），本模块不另建文件格式。

三条不变量（PRD §7.3 / 验收标准「计分正确性」）：

- ``state.json`` 的 ``completed_train_seeds`` 无重复，且是计分台账的**超集约束**：
  只有真正完整跑完（含截断收尾）的 episode 才会进 ``scoreboard``；
- ``scoreboard.json`` 的 ``episode_key`` 全局唯一（同 seed 重跑走 ``superseded_by``，
  不新增第二条有效记录）；
- 任一时刻 ``state.json`` 都能独立重建「下一步做什么」（NFR-06-2），不依赖内存态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence

from ..infra import events
from ..infra.errors import E, InfraError
from ..infra.io import read_json, validate, write_json_atomic
from ..infra.run import RunPaths, read_state, write_state

__all__ = [
    "Phase", "RunCounters", "RunBudget", "RunState", "Scoreboard", "QueueLog",
    "COUNTED_PHASES", "init_state", "load_state", "save_state", "utc_now",
    "scoreboard_path", "queue_path", "locks_dir", "STATE_SCHEMA",
]

#: PRD §7.2 的状态集合。``interrupted`` 是临时态：恢复后回 ``playing``，不落盘为终态。
Phase = Literal["pending", "playing", "reflecting", "evolving", "hitl_wait", "done",
                "failed"]
COUNTED_PHASES: Final[frozenset[str]] = frozenset(
    {"pending", "playing", "reflecting", "evolving", "hitl_wait", "done", "failed"})

STATE_SCHEMA: Final[str] = "run_state"
SCOREBOARD_SCHEMA: Final[str] = "scoreboard"


def utc_now() -> str:
    """ISO-8601 UTC 时间戳（``2026-09-30T12:00:00Z``）。

    仅用于审计字段（``updated_at`` / ``interrupted_at`` / 轨迹 ``ts``），**不**参与任何
    决策或采样（NFR-06-1：随机与排序不许用墙钟）。
    """
    return (datetime.now(tz=timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


# =====================================================================
# 子结构
# =====================================================================

@dataclass
class RunCounters:
    """PRD §13 的派生计数原料（本模块只计数，不做统计推断）。"""

    episodes_total: int = 0
    episodes_truncated: int = 0
    reflections_ok: int = 0
    reflections_skipped: int = 0
    illegal_actions: int = 0
    env_errors: int = 0
    llm_timeouts: int = 0
    io_retries: int = 0

    def to_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RunCounters":
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: int(v) for k, v in data.items() if k in known})


@dataclass
class RunBudget:
    """评估局成本账（FR-06-13：全系统唯一成本上限）。"""

    eval_episodes_used: int = 0
    eval_episodes_cap: int = 0
    candidates_used: int = 0

    def to_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RunBudget":
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: int(v) for k, v in data.items() if k in known})


# =====================================================================
# RunState
# =====================================================================

@dataclass
class RunState:
    """``state.json`` 的内存视图（PRD §6 JSON Schema 的权威定义处）。"""

    run_id: str
    phase: str = "pending"
    generation: int = 0
    harness_version: int = 0
    k: int = 30
    tick: int = 0
    completed_train_seeds: list[int] = field(default_factory=list)
    pending_queue: list[int] = field(default_factory=list)
    hitl_pending: dict[str, Any] | None = None
    last_completed_cycle: int | None = None
    evolving_cycle: int | None = None
    memory_version: int = 0
    resume_count: int = 0
    counters: RunCounters = field(default_factory=RunCounters)
    budget: RunBudget = field(default_factory=RunBudget)
    interrupted_at: str | None = None
    updated_at: str = field(default_factory=utc_now)

    # ---- 便捷视图 ---------------------------------------------------------
    @property
    def completed(self) -> frozenset[int]:
        return frozenset(int(s) for s in self.completed_train_seeds)

    @property
    def next_cycle_index(self) -> int:
        """``cycle_index = len(completed_train_seeds) // k``（PRD §7.3 周期幂等键）。"""
        return len(self.completed_train_seeds) // max(1, int(self.k))

    def cycle_due(self, *, max_generations: int) -> int | None:
        """返回需要触发的 ``cycle_index``；无需触发时返回 ``None``。

        唯一触发条件（FR-06-5 / §7.3）::

            len(completed_train_seeds) > 0
            and len(completed_train_seeds) % k == 0        # 恰好跑满 K 局
            and cycle_index > last_completed_cycle         # 同一 cycle 只触发一次
            and generation < max_generations

        其中 ``cycle_index = len(completed_train_seeds) // k``（PRD §7.3 的周期幂等键）。
        ``% k == 0`` 这一条**不可省**：PRD §8 的伪代码把 ``prev`` 初始化为 ``-1``，
        那会让第 1 局之后（``cycle_index == 0 > -1``）就触发一次演化——与 FR-06-5
        「每完成 K 局训练局触发一次」直接冲突。此处以 FR-06-5 为准。
        """
        completed = len(self.completed_train_seeds)
        if completed <= 0 or completed % max(1, int(self.k)) != 0:
            return None
        cycle = completed // max(1, int(self.k))
        prev = -1 if self.last_completed_cycle is None else int(self.last_completed_cycle)
        if cycle > prev and int(self.generation) < int(max_generations):
            return cycle
        return None

    # ---- 序列化 -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "phase": str(self.phase),
            "generation": int(self.generation),
            "harness_version": int(self.harness_version),
            "k": int(self.k),
            "tick": int(self.tick),
            "completed_train_seeds": [int(s) for s in self.completed_train_seeds],
            "pending_queue": [int(s) for s in self.pending_queue],
            "hitl_pending": (None if self.hitl_pending is None
                             else dict(self.hitl_pending)),
            "last_completed_cycle": (None if self.last_completed_cycle is None
                                     else int(self.last_completed_cycle)),
            "evolving_cycle": (None if self.evolving_cycle is None
                               else int(self.evolving_cycle)),
            "memory_version": int(self.memory_version),
            "resume_count": int(self.resume_count),
            "counters": self.counters.to_dict(),
            "budget": self.budget.to_dict(),
            "interrupted_at": self.interrupted_at,
            "updated_at": str(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunState":
        """从 ``state.json`` 还原；缺字段用默认值（向前兼容旧 run）。"""
        raw = dict(data)
        # 未知字段不阻断续跑（旧版本写的 state 必须仍可读；NFR-06-2 的向前兼容）
        return cls(
            run_id=str(raw.get("run_id", "")),
            phase=str(raw.get("phase", "pending")),
            generation=int(raw.get("generation", 0)),
            harness_version=int(raw.get("harness_version", 0)),
            k=int(raw.get("k", 30)),
            tick=int(raw.get("tick", 0)),
            completed_train_seeds=[int(s) for s in raw.get("completed_train_seeds", ())],
            pending_queue=[int(s) for s in raw.get("pending_queue", ())],
            hitl_pending=(None if raw.get("hitl_pending") is None
                          else dict(raw["hitl_pending"])),
            last_completed_cycle=_opt_int(raw.get("last_completed_cycle")),
            evolving_cycle=_opt_int(raw.get("evolving_cycle")),
            memory_version=int(raw.get("memory_version", 0)),
            resume_count=int(raw.get("resume_count", 0)),
            counters=RunCounters.from_dict(raw.get("counters")),
            budget=RunBudget.from_dict(raw.get("budget")),
            interrupted_at=raw.get("interrupted_at"),
            updated_at=str(raw.get("updated_at") or utc_now()),
        )


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


# =====================================================================
# 路径助手（Spec §5 / PRD §7.1 的目录树）
# =====================================================================

def scoreboard_path(paths: RunPaths) -> Path:
    return Path(paths.metrics) / "scoreboard.json"


def queue_path(paths: RunPaths) -> Path:
    return Path(paths.root) / "queue.jsonl"


def locks_dir(paths: RunPaths) -> Path:
    return Path(paths.root) / "locks"


# =====================================================================
# 状态生命周期
# =====================================================================

def init_state(run_id: str, *, k: int, seeds: Sequence[int] = (),
               harness_version: int = 0, budget_cap: int = 0,
               memory_version: int = 0) -> RunState:
    """新建 run 状态：``pending_queue`` 去重保序，``phase="pending"``（FR-06-9）。"""
    queue = _dedup_keep_order(int(s) for s in seeds)
    return RunState(run_id=str(run_id), phase="pending", generation=0,
                    harness_version=int(harness_version), k=int(k),
                    pending_queue=queue, memory_version=int(memory_version),
                    budget=RunBudget(eval_episodes_cap=int(budget_cap)))


def load_state(paths: RunPaths) -> RunState:
    """读 ``state.json``；缺失抛 ``E_IO_STATE_MISSING``（由 ``infra.run.read_state`` 决定）。

    同时按 §6 JSON Schema 校验结构——损坏的 state 必须**立即**失败，不能带着半截状态续跑
    （``E_IO_RUN_STATE``，NFR-06-5）。
    """
    raw = read_state(paths)
    validate(STATE_SCHEMA, raw)
    return RunState.from_dict(raw)


def save_state(paths: RunPaths, state: RunState, *, fsync: bool = True,
               validate_schema: bool = True) -> None:
    """原子替换 ``state.json``（tmp + ``os.replace``，NFR-06-5）。

    替换失败 → ``E_IO_RUN_STATE``（``write_json_atomic`` 的 ``E_IO_WRITE`` 由调用方
    按 PRD §10 升格为「状态不可信 → 立即 ``os._exit(5)``」）。
    """
    state.updated_at = utc_now()
    payload = state.to_dict()
    if validate_schema:
        validate(STATE_SCHEMA, payload)
    try:
        write_state(paths, payload, fsync=fsync)
    except InfraError as exc:
        if exc.code is E.E_IO_RUN_STATE:
            raise
        raise InfraError(E.E_IO_RUN_STATE,
                         f"state.json 原子写失败：{paths.state}\n{exc.message}",
                         hint="状态不可信，续跑前请检查磁盘；E_IO_RUN_STATE 应立即中止") from exc


def _dedup_keep_order(values: Sequence[int] | Any) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        seed = int(value)
        if seed not in seen:
            seen.add(seed)
            out.append(seed)
    return out


# =====================================================================
# 计分台账（FR-06-10）
# =====================================================================

class Scoreboard:
    """``metrics/scoreboard.json`` 的幂等追加门面。

    计分只发生在 episode 正常收尾；写入前检查 ``episode_key`` 是否已存在
    （FR-06-10）。同 seed 因 ``--force-rerun`` 重跑时，旧记录标 ``superseded_by``
    而非删除——台账的「有效记录集合」始终是「实际完整跑完的 episode 集合」。
    """

    def __init__(self, path: Path, *, fsync: bool = True, fsync_every: int = 1) -> None:
        self.path = Path(path)
        self.fsync = bool(fsync)
        self.fsync_every = max(1, int(fsync_every))
        self._rows: list[dict[str, Any]] = []
        self._index: dict[str, int] = {}
        self._dirty = 0
        self._loaded = False

    # ---- 读 ---------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.is_file():
            return
        data = read_json(self.path)
        if not isinstance(data, dict) or not isinstance(data.get("episodes"), list):
            raise InfraError(E.E_IO_RUN_STATE,
                             f"scoreboard.json 结构非法：{self.path}",
                             hint="顶层必须是 {\"episodes\": [...]}")
        self._rows = [dict(row) for row in data["episodes"]]
        self._reindex()

    def _reindex(self) -> None:
        self._index = {str(row.get("episode_key")): i
                       for i, row in enumerate(self._rows)}

    @property
    def rows(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return [dict(row) for row in self._rows]

    def keys(self) -> list[str]:
        self._ensure_loaded()
        return [str(row.get("episode_key")) for row in self._rows]

    def has(self, episode_key: str) -> bool:
        self._ensure_loaded()
        return str(episode_key) in self._index

    def active_keys(self) -> set[str]:
        """未被 ``superseded_by`` 覆盖的 ``episode_key`` 集合（计分正确性的判据）。"""
        self._ensure_loaded()
        return {str(row.get("episode_key")) for row in self._rows
                if not row.get("superseded_by")}

    # ---- 写 ---------------------------------------------------------------
    def add_once(self, row: Mapping[str, Any], *, force: bool = False) -> bool:
        """幂等追加一行；返回 ``True`` 表示本次真的写入了。

        - ``episode_key`` 已存在且 ``force=False`` → 跳过（不重复计分）；
        - ``force=True``（``--force-rerun``）→ 追加新行并把旧行标 ``superseded_by``。
        """
        self._ensure_loaded()
        key = str(row.get("episode_key") or "")
        if not key:
            raise InfraError(E.E_IO_RUN_STATE, "计分记录缺少 episode_key")
        existing = self._index.get(key)
        payload = {**dict(row), "superseded_by": row.get("superseded_by")}
        if existing is not None and not force:
            events.emit("loop.episode_end", episode_key=key, outcome=str(row.get("outcome")),
                        scored=False, reason="duplicate_episode_key")
            return False
        if existing is not None:
            self._rows[existing] = {**self._rows[existing], "superseded_by": key}
        self._rows.append(payload)
        self._index[key] = len(self._rows) - 1
        self._flush()
        return True

    def supersede(self, episode_key: str, *, by: str) -> None:
        """把既有记录标 ``superseded_by=by``（保留审计，不删）。"""
        self._ensure_loaded()
        index = self._index.get(str(episode_key))
        if index is None:
            return
        self._rows[index] = {**self._rows[index], "superseded_by": str(by)}
        self._flush()

    def _flush(self) -> None:
        self._dirty += 1
        do_fsync = self.fsync and (self._dirty % self.fsync_every == 0)
        payload = {"episodes": self._rows}
        validate(SCOREBOARD_SCHEMA, payload)
        write_json_atomic(self.path, payload, fsync=do_fsync)


# =====================================================================
# queue.jsonl（PRD §7.1：add/done/skip 追加式事件日志）
# =====================================================================

class QueueLog:
    """``queue.jsonl``：待做队列的追加式审计流，供恢复与审计（append + flush）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, action: str, seed: int, **extra: Any) -> None:
        if action not in ("add", "done", "skip"):
            raise InfraError(E.E_IO_RUN_STATE, f"queue 事件类型非法：{action!r}",
                             hint="只允许 add / done / skip")
        row = {"ts": utc_now(), "action": str(action), "seed": int(seed), **extra}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
