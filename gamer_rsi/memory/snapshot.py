"""MOD-05 快照读写、sha256 校验与回滚文件原语（FR-05-9 / §7.1 / §8.3，B-35 新增）。

文件格式（``runs/<run_id>/memory/memory_v{N}.jsonl``）::

    {"_type":"memory_snapshot_header","memory_version":5,"parent_memory_version":4,
     "harness_version":7,"generation":3,"run_id":"...","created_at":"...",
     "config_hash":"...","store_schema_version":"1.0","entry_count":812,
     "active_count":640,"reason":"generation_3","content_sha256":"..."}
    {"_type":"memory_entry", ...MemoryEntry 的全部字段...}
    ...

``content_sha256`` = 其后所有 ``_type:"memory_entry"`` 行（各自带 ``\\n``）按序拼接后的
sha256；**不含 header 自身**，因此跨机可复核（NFR-05-3）。写入一律
``tmp + fsync + os.replace``（FR-05-4），崩溃不留半行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.infra.io import write_bytes_atomic

from .errors import MemoryCorruptError, SnapshotMissingError
from .schemas import STORE_SCHEMA_VERSION, MemoryEntry, canonical_json

__all__ = [
    "HEADER_TYPE", "ENTRY_TYPE", "snapshot_name", "snapshot_path", "find_snapshots",
    "entry_line", "entries_sha256", "build_header", "write_snapshot", "read_snapshot",
    "verify_snapshot", "SnapshotData", "parse_entries",
]

HEADER_TYPE: Final[str] = "memory_snapshot_header"
ENTRY_TYPE: Final[str] = "memory_entry"


def snapshot_name(version: int) -> str:
    return f"memory_v{int(version)}.jsonl"


def snapshot_path(memory_dir: Path, version: int) -> Path:
    return Path(memory_dir) / snapshot_name(version)


def find_snapshots(memory_dir: Path) -> list[tuple[int, Path]]:
    """列出 ``memory_v{N}.jsonl``（按 N 升序；忽略不可解析的编号）。"""
    out: list[tuple[int, Path]] = []
    for path in sorted(Path(memory_dir).glob("memory_v*.jsonl")):
        stem = path.name[len("memory_v"):-len(".jsonl")]
        try:
            out.append((int(stem), path))
        except ValueError:
            continue
    return sorted(out, key=lambda item: item[0])


def entry_line(entry: MemoryEntry) -> str:
    """快照里的条目行（``_type`` 前置 + 条目规范字段序）。"""
    payload = {"_type": ENTRY_TYPE, **entry.to_dict()}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def entries_sha256(lines: Iterable[str]) -> str:
    """``content_sha256`` 的唯一口径：条目行（各带 ``\\n``）拼接后的 sha256。"""
    import hashlib

    hasher = hashlib.sha256()
    for line in lines:
        hasher.update((line + "\n").encode("utf-8"))
    return hasher.hexdigest()


def build_header(*, memory_version: int, parent_memory_version: int, harness_version: int,
                 generation: int, run_id: str, created_at: str, config_hash: str,
                 entry_count: int, active_count: int, reason: str,
                 content_sha256: str) -> dict[str, Any]:
    """快照 header（首行）；字段与 PRD §7.1 逐字对齐。"""
    return {
        "_type": HEADER_TYPE,
        "memory_version": int(memory_version),
        "parent_memory_version": int(parent_memory_version),
        "harness_version": int(harness_version),
        "generation": int(generation),
        "run_id": str(run_id),
        "created_at": str(created_at),
        "config_hash": str(config_hash),
        "store_schema_version": STORE_SCHEMA_VERSION,
        "entry_count": int(entry_count),
        "active_count": int(active_count),
        "reason": str(reason),
        "content_sha256": str(content_sha256),
    }


@dataclass(frozen=True)
class SnapshotData:
    """一个快照的内容与自校验结果。"""

    path: Path
    header: Mapping[str, Any]
    entries: tuple[MemoryEntry, ...]
    entry_lines: tuple[str, ...]
    content_sha256: str
    computed_sha256: str

    @property
    def memory_version(self) -> int:
        return int(self.header.get("memory_version", 0))

    @property
    def parent_memory_version(self) -> int:
        return int(self.header.get("parent_memory_version", 0))

    @property
    def harness_version(self) -> int:
        return int(self.header.get("harness_version", 0))

    @property
    def sha256_ok(self) -> bool:
        return self.content_sha256 == self.computed_sha256


def write_snapshot(path: Path, header: Mapping[str, Any],
                   entries: Sequence[MemoryEntry]) -> str:
    """原子写快照（tmp + ``fsync`` + ``os.replace``），返回 ``content_sha256``。

    调用方负责填 header 的 ``content_sha256``；本函数原样写入并返回同一值，
    便于调用方与 ``index.json`` 对齐。
    """
    lines = [entry_line(e) for e in entries]
    body = "".join(line + "\n" for line in lines)
    payload = (json.dumps(dict(header), ensure_ascii=False, separators=(",", ":"))
               + "\n" + body)
    write_bytes_atomic(Path(path), payload.encode("utf-8"), fsync=True)
    return str(header.get("content_sha256", ""))


def _iter_lines(path: Path) -> list[str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InfraError(E.E_IO_WRITE, f"快照不可读：{path}: {exc}") from exc
    return [line for line in text.split("\n") if line.strip()]


def parse_entries(lines: Iterable[str], *, origin: str = "") -> list[MemoryEntry]:
    """解析快照条目行（跳过并忽略 header 行）；格式错 → ``E_IO_MEMORY_CORRUPT``。"""
    entries: list[MemoryEntry] = []
    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryCorruptError(f"{origin} 快照条目行不是合法 JSON：{exc}") from exc
        if not isinstance(raw, Mapping):
            raise MemoryCorruptError(f"{origin} 快照条目行必须是 JSON 对象")
        if raw.get("_type") == HEADER_TYPE:
            continue
        if raw.get("_type") != ENTRY_TYPE:
            raise MemoryCorruptError(
                f"{origin} 快照行缺少 _type={ENTRY_TYPE}：{str(raw.get('_type'))!r}")
        payload = {k: v for k, v in raw.items() if k != "_type"}
        entries.append(MemoryEntry.from_json(canonical_json(payload)))
    return entries


def read_snapshot(path: Path, *, require_version: int | None = None,
                  verify: bool = True) -> SnapshotData:
    """读快照：header + 条目 + 复算 sha256；缺失 → ``E_IO_SNAPSHOT_MISSING``。"""
    path = Path(path)
    if not path.is_file():
        raise SnapshotMissingError(f"快照不存在：{path}")
    lines = _iter_lines(path)
    if not lines:
        raise MemoryCorruptError(f"快照为空：{path}")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise MemoryCorruptError(f"快照 header 不是合法 JSON：{path}: {exc}") from exc
    if not isinstance(header, Mapping) or header.get("_type") != HEADER_TYPE:
        raise MemoryCorruptError(f"快照首行必须是 _type={HEADER_TYPE}：{path}")
    if require_version is not None and int(header.get("memory_version", -1)) != int(require_version):
        raise MemoryCorruptError(
            f"快照版本号不符：{path} 的 memory_version="
            f"{header.get('memory_version')!r} != {require_version}")
    entry_lines = tuple(lines[1:])
    computed = entries_sha256(entry_lines)
    declared = str(header.get("content_sha256", ""))
    if verify and declare_mismatch(declared, computed):
        raise MemoryCorruptError(
            f"快照 content_sha256 不符：{path} declared={declared[:12]}… "
            f"computed={computed[:12]}…", hint="快照被改动或写入中断；请从上一版重建")
    entries = tuple(parse_entries(entry_lines, origin=str(path)))
    return SnapshotData(path=path, header=header, entries=entries,
                        entry_lines=entry_lines, content_sha256=declared,
                        computed_sha256=computed)


def declare_mismatch(declared: str, computed: str) -> bool:
    """sha256 声明与复算是否不符（空声明按不符处理）。"""
    return (not declared) or (declared != computed)


def verify_snapshot(path: Path) -> SnapshotData:
    """``rollback`` / ``verify`` 用：读快照并强制校验 sha256。"""
    return read_snapshot(path, verify=True)
