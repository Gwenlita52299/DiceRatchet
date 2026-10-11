"""JSONL 落地、轨迹 schema 校验与 redaction（FR-09-6 / FR-09-8 / FR-09-9）。

- ``JsonlWriter``：追加写、逐行 flush、崩溃安全（先构造整行再单次 write）、轮转。
- ``validate_trajectory_line()``：按 ``schemas/trajectory_line.schema.json`` 校验，
  局末 ``episode_end`` 行走同一文件的 ``$defs/EpisodeEnd`` 分支（Spec §4.5）。
- ``Redactor``：只作用于 agent 可见的三个出口（``prompts/``、``memory/``、
  ``reflections/``）；轨迹 / events.jsonl / generations/ 属审计侧，不做 redaction（B-19）。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Final, Sequence

from .errors import E, InfraError
from .io import read_jsonl, validate

__all__ = ["JsonlWriter", "Redactor", "validate_trajectory_line", "read_trajectory",
           "EPISODE_END_TYPE", "REDACTION_PREFIX"]

EPISODE_END_TYPE: Final[str] = "episode_end"
REDACTION_PREFIX: Final[str] = "<REDACTED:"
TRAJECTORY_SCHEMA: Final[str] = "trajectory_line.schema.json"

# redaction 的三个合法出口（FR-09-9）
REDACTED_SURFACES: Final[tuple[str, ...]] = ("prompts", "memory", "reflections")


# =====================================================================
# JsonlWriter（FR-09-6）
# =====================================================================

class JsonlWriter:
    """追加写 JSONL；每行 ``json.dumps(ensure_ascii=False) + "\\n"``，写后 flush。

    崩溃安全：整行字符串构造完成后**单次** ``write``，保证「一行要么完整要么不存在」。
    ``fsync_every=N>0`` 时每 N 行 ``os.fsync`` 一次（默认 0 = 不 fsync，NFR-09-2）。
    """

    def __init__(self, path: Path, *, fsync_every: int = 0,
                 rotate_max_mb: int = 512,
                 redactor: Redactor | None = None,
                 schema: str | None = None,
                 on_redaction_hit: Callable[[int, dict[str, Any]], None] | None = None,
                 append: bool = True) -> None:
        self.path = Path(path)
        self.fsync_every = max(0, int(fsync_every))
        self.rotate_max_mb = max(1, int(rotate_max_mb))
        self.redactor = redactor
        self.schema = schema
        self.on_redaction_hit = on_redaction_hit
        self._n_since_fsync = 0
        self._n_written = 0
        self._closed = False
        self._latency: list[float] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        self._fh = open(self.path, mode, encoding="utf-8", newline="\n")

    # ---- 生命周期 ---------------------------------------------------------------
    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.flush()
            if self.fsync_every > 0:
                os.fsync(self._fh.fileno())
        except OSError as exc:
            raise InfraError(E.E_IO_DISK_FULL if exc.errno == 28 else E.E_IO_WRITE,
                             f"close 落盘失败：{self.path}") from exc
        finally:
            self._fh.close()

    @property
    def n_written(self) -> int:
        return self._n_written

    def drain_latency_ms(self) -> list[float]:
        out, self._latency = self._latency, []
        return out

    # ---- 写入 -------------------------------------------------------------------
    def write(self, event: dict[str, Any]) -> None:
        """写一行；先 schema 校验 + redaction，再单次写入整行（FR-09-6/8/9）。"""
        if self._closed:
            raise InfraError(E.E_IO_WRITE, f"writer 已关闭：{self.path}")
        t0 = time.perf_counter()
        obj = dict(event)
        if self.schema:
            self._validate(obj)
        if self.redactor is not None:
            obj, hits = self.redactor.redact(obj)
            if hits > 0 and self.on_redaction_hit is not None:
                self.on_redaction_hit(hits, obj)
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        if "\n" in line or "\r" in line: # pragma: no cover - 防御：JSON 不会产生换行
            raise InfraError(E.E_IO_WRITE, "轨迹行内含换行符，拒绝写入")
        payload = (line + "\n").encode("utf-8")
        self._maybe_rotate()
        try:
            self._fh.write(payload.decode("utf-8"))
            self._fh.flush()
            self._n_written += 1
            self._n_since_fsync += 1
            if self.fsync_every > 0 and self._n_since_fsync >= self.fsync_every:
                os.fsync(self._fh.fileno())
                self._n_since_fsync = 0
        except OSError as exc:
            raise InfraError(E.E_IO_DISK_FULL if exc.errno == 28 else E.E_IO_WRITE,
                             f"写入失败：{self.path}") from exc
        self._latency.append((time.perf_counter() - t0) * 1000.0)

    def write_lines(self, events: Sequence[dict[str, Any]]) -> None:
        for ev in events:
            self.write(ev)

    def _validate(self, obj: dict[str, Any]) -> None:
        """轨迹行 schema 校验；episode_end 行走 EpisodeEnd 分支。"""
        if obj.get("type") == EPISODE_END_TYPE:
            validate(TRAJECTORY_SCHEMA, obj) # $defs/EpisodeEnd 通过 allOf 分支生效
        else:
            validate(TRAJECTORY_SCHEMA, obj)

    def _maybe_rotate(self) -> None:
        if self.rotate_max_mb <= 0:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.rotate_max_mb * (1 << 20):
            return
        self._fh.close()
        rotated = self.path.with_suffix(self.path.suffix + ".1")
        try:
            os.replace(self.path, rotated)
        except OSError: # pragma: no cover
            pass
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")


def read_trajectory(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读取轨迹文件；返回 ``(行, 截断行数)``（末行截断容忍，NFR-09-1）。"""
    rows, truncated = read_jsonl(path)
    return rows, len(truncated)


# =====================================================================
# 轨迹行校验（FR-09-8）
# =====================================================================

#: ``episode_key = e-{seed:06d}-g{gen:03d}-{arm}``（Spec §4.5 B-16）。
#:
#: **F41（2026-10-09 实跑登记）**：臂位原先只有
#: ``train|parent|cand-<id>|baseline-*|ablation-*``，漏了 MOD-07 FR-07-6 的**冒烟臂**标签
#: （``parent-smoke`` / ``cand-<id>-smoke``）。于是冒烟局每一行轨迹都校验失败 → 该局
#: dropped（轨迹文件 0 行）→ ``n_dropped/n = 1.0 > drop_rate_max`` → ``low_confidence``
#: → ``error_codes`` 带 ``E_EVAL_INSUFFICIENT_N`` → ``judge.smoke_fast_reject`` 返回
#: ``smoke_error:…`` ⇒ **每个候选都在冒烟关被淘汰**，``harness/versions/`` 永远不可能有
#: 新版本（闭环形式上「跑通」、实则零产出）。现允许任意臂位带 ``-smoke`` 后缀。
#:
#: ⚠️ 同一模式在 ``schemas/trajectory_line.schema.json`` 里有一份**副本**（两层都要过）；
#: ``tests/unit/infra/test_logging.py`` 的用例钉住二者逐字一致，防再次漂移。
_EPISODE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"^e-[0-9]{6}-g[0-9]{3}-(train|parent|cand-[a-z0-9]+|baseline-[a-z_]+|ablation-[a-z_]+)"
    r"(-smoke)?$"
)
#: 与 ``schemas/trajectory_line.schema.json`` 的 ``episode_key.pattern`` 必须逐字相同。
EPISODE_KEY_PATTERN: Final[str] = _EPISODE_KEY_RE.pattern
_OBS_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_LINE_KEYS: Final[frozenset[str]] = frozenset({
    "episode_key", "generation", "harness_version", "arm", "seed", "split", "step",
    "decision_index", "plan_index", "attempt", "source", "obs_digest", "action",
    "reward", "cum_reward", "illegal", "mode", "ts",
})
_REQUIRED_END_KEYS: Final[frozenset[str]] = frozenset({
    "type", "episode_key", "generation", "harness_version", "arm", "seed", "split",
    "outcome", "stages_cleared", "hearts_left", "battles_won", "decisions",
    "degraded", "ts",
})


def validate_trajectory_line(line: dict[str, Any]) -> None:
    """轨迹行校验（FR-09-8）：失败抛 ``E_IO_TRAJECTORY_SCHEMA``。

    先做 registry 级快速检查（键路径精确报错），再走 JSON Schema（G5 round-trip）。
    """
    if not isinstance(line, dict):
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                         f"轨迹行必须是对象，实际 {type(line).__name__}")
    if line.get("type") == EPISODE_END_TYPE:
        _validate_episode_end(line)
    else:
        _validate_step(line)
    validate(TRAJECTORY_SCHEMA, line)


def _missing(path: str, keys: frozenset[str], got: set[str]) -> InfraError:
    missing = sorted(keys - got)
    return InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                      f"轨迹行缺必填字段：{missing}" if path == "line"
                      else f"{path} 缺必填字段：{missing}",
                      hint="字段定义见 Spec §4.5；schema 见 schemas/trajectory_line.schema.json")


def _validate_step(line: dict[str, Any]) -> None:
    got = set(line.keys())
    if not _REQUIRED_LINE_KEYS <= got:
        raise _missing("line", _REQUIRED_LINE_KEYS, got)
    for key in sorted(got):
        if not key.startswith("x_") and key not in _OPTIONAL_LINE_KEYS \
                and key not in _REQUIRED_LINE_KEYS:
            raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                             f"轨迹行含未登记字段 {key!r}（仅允许 x_ 前缀扩展）")
    if not _EPISODE_KEY_RE.match(str(line["episode_key"])):
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                         f"episode_key={line['episode_key']!r} 不匹配 "
                         f"e-{{seed:06d}}-g{{gen:03d}}-{{arm}}")
    if not _OBS_DIGEST_RE.match(str(line["obs_digest"])):
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                         f"obs_digest={line['obs_digest']!r} 必须为 sha256:<64hex>")
    if str(line["ts"])[-1] != "Z":
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                         f"ts={line['ts']!r} 必须为 UTC ISO-8601 且以 Z 结尾")


_OPTIONAL_LINE_KEYS: Final[frozenset[str]] = frozenset({
    "illegal_reason", "llm_raw", "llm_latency_ms", "prompt_tokens",
    "completion_tokens", "prompt_hash", "memory_ids", "tool_hash", "type",
})


def _validate_episode_end(line: dict[str, Any]) -> None:
    got = set(line.keys())
    if not _REQUIRED_END_KEYS <= got:
        raise _missing("episode_end", _REQUIRED_END_KEYS, got)
    if str(line["type"]) != EPISODE_END_TYPE:
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                         f"type={line['type']!r} 只能是 {EPISODE_END_TYPE!r}")
    if not _EPISODE_KEY_RE.match(str(line["episode_key"])):
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA,
                         f"episode_end 的 episode_key={line['episode_key']!r} 非法")
    if str(line["ts"])[-1] != "Z":
        raise InfraError(E.E_IO_TRAJECTORY_SCHEMA, "episode_end 的 ts 必须以 Z 结尾")


# =====================================================================
# Redactor（FR-09-9，作用域收窄 B-19）
# =====================================================================

class Redactor:
    """敏感值注册表 + 值级替换。

    由 MOD-02 审计器注册：heldout/test seed、canary 串、``GameState`` 专有键名。
    **只作用于** ``prompts/``、``memory/``、``reflections/`` 三个出口；轨迹、
    ``events.jsonl``、``generations/`` 属审计侧，不做 redaction（MOD-08 配对检验需要原始 seed）。
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {} # 规范化值 -> label
        self._patterns: list[tuple[re.Pattern[str], str]] = []
        self._raw: dict[str, str] = {}

    def register(self, value: str | int, label: str) -> None:
        """注册一个敏感值；``label`` 出现在替换结果 ``<REDACTED:{label}>`` 中。"""
        key = str(value)
        if not key:
            return
        self._values.setdefault(key, label)
        self._raw[key] = label
        # 带词边界的精确值匹配（避免误伤普通数字，风险节）
        self._patterns.append((re.compile(rf"(?<![\w.]){re.escape(key)}(?![\w])"), label))

    def register_many(self, values: Sequence[str | int], label: str) -> None:
        for v in values:
            self.register(v, label)

    @property
    def registered(self) -> dict[str, str]:
        return dict(self._raw)

    def redact(self, obj: Any) -> tuple[Any, int]:
        """深度遍历并做值级替换；返回 ``(擦除后对象, 命中数)``。"""
        if not self._values:
            return obj, 0
        return self._redact(obj, 0)

    def _redact(self, obj: Any, hits: int) -> tuple[Any, int]:
        if isinstance(obj, str):
            return self._redact_str(obj, hits)
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str):
                    nk, hits = self._redact_str(k, hits)
                else:
                    nk = k
                nv, hits = self._redact(v, hits)
                out[nk] = nv
            return out, hits
        if isinstance(obj, (list, tuple)):
            items: list[Any] = []
            for v in obj:
                nv, hits = self._redact(v, hits)
                items.append(nv)
            return (tuple(items) if isinstance(obj, tuple) else items), hits
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            # 数字值精确匹配（seed 是整数；不套正则，避免 2042 误伤 20420）
            key = repr(obj) if isinstance(obj, int) else str(obj)
            if key in self._values:
                return f"{REDACTION_PREFIX}{self._values[key]}>", hits + 1
        return obj, hits

    def _redact_str(self, text: str, hits: int) -> tuple[str, int]:
        result = text
        total = 0
        for pattern, label in self._patterns:
            result, n = pattern.subn(f"{REDACTION_PREFIX}{label}>", result)
            total += n
        if total == 0 and text in self._values: # 精确整体匹配（短串兜底）
            return f"{REDACTION_PREFIX}{self._values[text]}>", hits + 1
        return result, hits + total
