"""轨迹落盘（MOD-06 PRD FR-06-2；Spec §4.5）。

一次决策 = 一个或多个 ``StepRecord``；**每个** ``StepRecord``（含 ``reject`` 行）立即
追加到 ``episodes/<split>/<episode_key>.jsonl`` 并 ``flush``，崩溃时最多丢当前 step。
局末追加一行 ``episode_end``。

- 行 schema 校验走 MOD-09 的 ``infra.logging.validate_trajectory_line``（FR-09-8）；
- 原子性与重试走 MOD-09 的 ``JsonlWriter``（单次 ``write`` 整行 + ``flush``）与
  ``loop.io_retry`` / ``loop.io_backoff_ms``（指数退避 200/400/800 ms）；
- **不做 redaction**：轨迹属审计侧，redaction 只作用于 prompt/memory/reflection（B-19）。

``episode_end`` 行严格按 ``schemas/trajectory_line.schema.json`` 的 ``EpisodeEnd``
分支（``additionalProperties:false``）；MOD-08 复算多样性所需的逐局汇总字段以 ``x_``
前缀随行写入（schema 显式保留的扩展位），不污染 Spec §4.5 的字段面。
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..infra.errors import E, InfraError
from ..infra.logging import JsonlWriter, validate_trajectory_line

__all__ = ["EpisodeFile", "TrajectoryWriter", "TRAJECTORY_SCHEMA"]

TRAJECTORY_SCHEMA = "trajectory_line"

#: MOD-04 的 ``source`` → Spec §4.5 的 ``source`` 枚举。
#: ``fixed_prompt`` 是「冻结模板的真实模型调用」，语义上仍是一次 LLM 决策。
_SOURCE_MAP: dict[str, str] = {
    "llm": "llm",
    "fallback": "fallback",
    "heuristic": "heuristic",
    "random": "random",
    "scripted": "scripted",
    "fixed_prompt": "llm",
}

_RETRYABLE = frozenset({E.E_IO_WRITE, E.E_IO_DISK_FULL})


def _source_of(raw: str) -> str:
    return _SOURCE_MAP.get(str(raw), "scripted")


def _opt_str(value: Any) -> str | None:
    text = None if value in (None, "") else str(value)
    return text


def _opt_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _clamp_attempt(value: Any) -> int:
    try:
        attempt = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(3, attempt))


@dataclass(frozen=True)
class StepMeta:
    """决策内**第一个执行动作行**才携带的 LLM 字段（Spec §4.5）。"""

    llm_raw: str | None = None
    llm_latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_hash: str | None = None
    memory_ids: Sequence[str] | None = None
    tool_hash: str | None = None

    @classmethod
    def of_decision(cls, decision: Any) -> "StepMeta":
        llm = getattr(decision, "llm", None)
        return cls(
            llm_raw=_opt_str(getattr(llm, "raw", None)) if llm is not None else None,
            llm_latency_ms=_opt_int(getattr(llm, "latency_ms", None)) if llm is not None else None,
            prompt_tokens=_opt_int(getattr(llm, "prompt_tokens", None)) if llm is not None else None,
            completion_tokens=(_opt_int(getattr(llm, "completion_tokens", None))
                               if llm is not None else None),
            prompt_hash=_opt_str(getattr(decision, "prompt_hash", None)),
            memory_ids=list(getattr(decision, "memory_ids", ()) or ()) or None,
            tool_hash=_opt_str(getattr(decision, "tool_hash", None)),
        )


def first_executed_index(steps: Sequence[Any]) -> int:
    """决策内第一个**执行**（非 reject）动作行的下标；没有则 ``-1``。"""
    for index, step in enumerate(steps):
        if not bool(getattr(step, "illegal", False)):
            return index
    return -1


class EpisodeFile:
    """单个 ``episodes/<split>/<episode_key>.jsonl`` 的写入句柄。"""

    def __init__(self, writer: JsonlWriter, *, episode_key: str, split: str, seed: int,
                 arm: str, generation: int, harness_version: int, mode: str,
                 io_retry: int = 3, io_backoff_ms: int = 200,
                 on_retry: Any = None) -> None:
        self._writer = writer
        self.episode_key = episode_key
        self.split = split
        self.seed = int(seed)
        self.arm = arm
        self.generation = int(generation)
        self.harness_version = int(harness_version)
        self.mode = mode
        self.io_retry = max(0, int(io_retry))
        self.io_backoff_ms = max(1, int(io_backoff_ms))
        self.on_retry = on_retry
        self._step = 0
        self._n_rows = 0
        self._last_step: dict[str, Any] | None = None

    @property
    def n_rows(self) -> int:
        return self._n_rows

    @property
    def path(self) -> Path:
        return self._writer.path

    # ---- 写入 -------------------------------------------------------------
    def _write(self, row: dict[str, Any]) -> None:
        validate_trajectory_line(row)
        attempt = 0
        while True:
            try:
                self._writer.write(row)
                self._n_rows += 1
                return
            except InfraError as exc:
                if exc.code not in _RETRYABLE or attempt >= self.io_retry:
                    raise
                if self.on_retry is not None:
                    self.on_retry(exc)
                time.sleep(self.io_backoff_ms * (2 ** attempt) / 1000.0)
                attempt += 1

    def write_step(self, record: Any, *, cum_reward: float,
                   meta: StepMeta | None = None) -> dict[str, Any]:
        """写一行 env 步（含 reject 行）；返回落盘的行（供测试断言）。"""
        result = record.result
        info = dict(getattr(result, "info", {}) or {})
        llm = meta or StepMeta()
        row: dict[str, Any] = {
            "episode_key": self.episode_key,
            "generation": self.generation,
            "harness_version": self.harness_version,
            "arm": self.arm,
            "seed": self.seed,
            "split": self.split,
            "step": self._step,
            "decision_index": max(0, int(info.get("decision_index") or 0)),
            "plan_index": max(0, int(getattr(record, "plan_index", 0) or 0)),
            "attempt": _clamp_attempt(getattr(record, "attempt", 1)),
            "source": _source_of(getattr(record, "source", "")),
            "obs_digest": str(info.get("obs_digest") or ""),
            "action": (None if getattr(record, "action", None) is None
                       else dict(record.action)),
            "reward": float(getattr(result, "reward", 0.0)),
            "cum_reward": float(cum_reward),
            "illegal": bool(getattr(record, "illegal", False)),
            "illegal_reason": _opt_str(getattr(record, "illegal_reason", None)),
            "llm_raw": llm.llm_raw,
            "llm_latency_ms": llm.llm_latency_ms,
            "prompt_tokens": llm.prompt_tokens,
            "completion_tokens": llm.completion_tokens,
            "prompt_hash": llm.prompt_hash,
            "memory_ids": (None if llm.memory_ids is None else [str(m) for m in llm.memory_ids]),
            "tool_hash": llm.tool_hash,
            "mode": self.mode,
            "ts": _utc_stamp(),
        }
        if row["action"] is None:
            # reject(final=True) 的兜底行可能没有动作对象；schema 允许 null。
            row["action"] = None
        self._write(row)
        self._step += 1
        self._last_step = row
        return row

    def write_end(self, end: Mapping[str, Any]) -> dict[str, Any]:
        """写局末 ``episode_end`` 行（Spec §4.5 / ``$defs/EpisodeEnd``）。

        ``trajectory_line`` schema 的**顶层** ``required`` 对 ``episode_end`` 行同样生效
        （``if/then`` 只追加约束、不豁免顶层必填），所以 ``decision_index`` /
        ``plan_index`` / ``attempt`` / ``source`` / ``obs_digest`` / ``action`` 也必须给出。
        取本局最后一步的值（无步时给中性默认），口径与 ``tests/unit/infra/test_logging.py``
        的 ``end_line()`` 一致。
        """
        payload = dict(end)
        last = self._last_step or {}
        payload.setdefault("reward", 0.0)
        payload.setdefault("cum_reward", 0.0)
        payload.setdefault("illegal", False)
        row: dict[str, Any] = {
            "type": "episode_end",
            "episode_key": self.episode_key,
            "generation": self.generation,
            "harness_version": self.harness_version,
            "arm": self.arm,
            "seed": self.seed,
            "split": self.split,
            "step": self._step,
            "decision_index": int(last.get("decision_index") or 0),
            "plan_index": int(last.get("plan_index") or 0),
            "attempt": int(last.get("attempt") or 1),
            "source": str(last.get("source") or "heuristic"),
            "obs_digest": str(last.get("obs_digest") or "sha256:" + "0" * 64),
            "action": last.get("action"),
            "outcome": payload.get("outcome"),
            "truncation_reason": payload.get("truncation_reason"),
            "stages_cleared": int(payload.get("stages_cleared") or 0),
            "hearts_left": int(payload.get("hearts_left") or 0),
            "battles_won": int(payload.get("battles_won") or 0),
            "decisions": int(payload.get("decisions") or 0),
            "degraded": bool(payload.get("degraded", False)),
            "exclude_reason": payload.get("exclude_reason"),
            "reward": float(payload.get("reward") or 0.0),
            "cum_reward": float(payload.get("cum_reward") or 0.0),
            "illegal": bool(payload.get("illegal", False)),
            "mode": self.mode,
            "ts": _utc_stamp(),
        }
        # MOD-08 复算多样性/序列哈希所需的逐局汇总：schema 显式保留的 x_ 扩展位
        # （``patternProperties: {"^x_": {}}``），不进入 Spec §4.5 的字段面。
        row["x_steps"] = int(payload.get("x_steps") or self._step)
        row["x_illegal_attempts"] = int(payload.get("x_illegal_attempts") or 0)
        row["x_fallback_decisions"] = int(payload.get("x_fallback_decisions") or 0)
        row["x_decisions_llm"] = int(payload.get("x_decisions_llm") or 0)
        row["x_dropped"] = bool(payload.get("x_dropped", False))
        row["x_kind_counts"] = list(payload.get("x_kind_counts") or [])
        row["x_decision_kinds"] = list(payload.get("x_decision_kinds") or [])
        row["x_slot_counts"] = dict(payload.get("x_slot_counts") or {})
        row["x_seq_hash"] = str(payload.get("x_seq_hash") or "")
        self._write(row)
        return row


class TrajectoryWriter:
    """``episodes/<split>/<episode_key>.jsonl`` 的工厂（``with writer.episode(...)``）。"""

    def __init__(self, paths: Any, *, io_retry: int = 3, io_backoff_ms: int = 200,
                 on_retry: Any = None, max_run_bytes: int = 0,
                 warn: Any = None) -> None:
        self.paths = paths
        self.io_retry = int(io_retry)
        self.io_backoff_ms = int(io_backoff_ms)
        self.on_retry = on_retry
        self.max_run_bytes = int(max_run_bytes)
        self._warn = warn or (lambda message: print(message, file=sys.stderr))
        self._bytes_quota_warned = False

    def path_for(self, split: str, episode_key: str) -> Path:
        return Path(self.paths.episode_path(split, episode_key))

    @contextmanager
    def episode(self, *, split: str, episode_key: str, seed: int, arm: str,
                generation: int, harness_version: int, mode: str,
                append: bool = False) -> Iterator[EpisodeFile]:
        """打开一集的轨迹文件。

        ``append=False``（默认）：每次**尝试**重写该 ``episode_key`` 的文件。理由：
        ``episode_key`` 唯一标识「同一代 + 同一臂 + 同一 seed 的一次完整执行」，中断后
        重跑是同一次执行的重来——若把崩溃残留的半局行追加在新局之前，文件里会出现两段
        ``step`` 从 0 开始的孤儿行，MOD-02 的轨迹重放（``audit leak``）会把它判成
        parity mismatch。崩溃安全仍由「每步 append+flush」保证（FR-06-2）：
        中断时磁盘上仍留着已完成的那些行（T-06-8 的观测点），只是重跑会换掉它们。
        """
        path = self.path_for(split, episode_key)
        writer = JsonlWriter(path, append=append)
        file = EpisodeFile(writer, episode_key=episode_key, split=split, seed=seed,
                           arm=arm, generation=generation,
                           harness_version=harness_version, mode=mode,
                           io_retry=self.io_retry, io_backoff_ms=self.io_backoff_ms,
                           on_retry=self._note_retry)
        try:
            yield file
        finally:
            writer.close()
            self._check_quota(path)

    def _note_retry(self, exc: InfraError) -> None:
        if self.on_retry is not None:
            self.on_retry(exc)

    def _check_quota(self, path: Path) -> None:
        """``loop.max_run_bytes`` 配额告警（**不中断**，PRD §9）。"""
        if self.max_run_bytes <= 0 or self._bytes_quota_warned:
            return
        try:
            total = sum(p.stat().st_size for p in Path(self.paths.episodes).rglob("*.jsonl"))
        except OSError: # pragma: no cover - 统计失败不影响主循环
            return
        if total > self.max_run_bytes:
            self._bytes_quota_warned = True
            self._warn(f"[{E.E_IO_DISK_FULL}] runs 轨迹已达 "
                       f"{total / (1 << 30):.2f} GiB，超过 loop.max_run_bytes "
                       f"({self.max_run_bytes / (1 << 30):.2f} GiB)；仅告警，不中断")


def _utc_stamp() -> str:
    """轨迹行的 ``ts``（UTC ISO-8601，``Z`` 结尾；仅审计用，不参与决策）。"""
    from .state import utc_now

    return utc_now()


def seq_hash(actions: Sequence[Mapping[str, Any]]) -> str:
    """动作序列 sha256（MOD-08 ``distinct_sequence_ratio`` 的输入，口径与 MOD-08 一致）。"""
    payload = json.dumps([dict(a) for a in actions], sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
