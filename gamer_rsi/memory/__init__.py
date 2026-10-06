"""MOD-05 memory：L1 策略记忆与反思库（PRD ``docs/prd/MOD-05-memory-PRD.md``）。

职责（Spec §3 MOD-05）：把 MOD-06 局末产出的反思文本转成可检索、可度量、可淘汰、可回滚
的结构化记忆，供 MOD-04 组装 prompt 时按当前 ``Observation`` 取用。**不**生成反思文本
（MOD-06）、**不**判定记忆价值（MOD-08/07）、**不**做 prompt 拼装（MOD-04）。

模块布局（PRD §6.1）::

    schemas.py    MemoryEntry / ReflectionEntry / Provenance 等类型 + JSON Schema + round-trip
    store.py      MemoryStore：写入管线、去重/冲突、容量/TTL、检索、快照/回滚、自检
    retriever.py  纯函数：situation_key / extract_candidates / similarity / utility / BM25
    snapshot.py   memory_v{N}.jsonl 读写、content_sha256、回滚文件原语（B-35）
    cli.py        memory stats|snapshot|rollback|verify|retrieve（B-08）

确定性（G3）：打分、排序、淘汰只用调用方传入的逻辑时钟 ``tick``；不 import ``random``；
``created_at`` 只作审计展示且不计入 ``content_sha256``。INV-5 的四拦截点见 ``store.py`` 顶部。
"""

from __future__ import annotations

from .errors import (MEMORY_ERROR_CODES, MemoryCorruptError, MemoryIsolationError,
                     MemoryLockedError, MemoryQuotaError, MemoryWriteFailedError,
                     SnapshotMissingError)
from .retriever import (bm25_score, estimate_tokens, extract_candidates, obs_tag_weights,
                        render_entries, render_entry, similarity, situation_key,
                        situation_of, tokenize, utility_score)
from .schemas import (ADD_RESULT_SCHEMA, ENTRY_SCHEMA_VERSION, MEMORY_ENTRY_SCHEMA,
                      MEMORY_SCHEMA_VERSION, MEMORY_TAGS, REFLECTION_ENTRY_SCHEMA,
                      RETRIEVE_RESULT_SCHEMA, STORE_SCHEMA_VERSION, AddResult, MemoryCandidate,
                      MemoryEntry, Provenance, ReflectionEntry, RetrieveResult,
                      RetrievedMemory, RollbackReport, SituationKey, StoreStats,
                      VerifyReport, DroppedEntry, validate_memory_entry,
                      validate_reflection_entry)
from .snapshot import build_header, read_snapshot, snapshot_path, write_snapshot
from .store import LOCK_TIMEOUT_S, MemoryStore, MemoryView

__all__ = [
    # 类型与 schema
    "MemoryEntry", "ReflectionEntry", "Provenance", "MemoryCandidate", "SituationKey",
    "AddResult", "RetrieveResult", "RetrievedMemory", "DroppedEntry", "StoreStats",
    "VerifyReport", "RollbackReport",
    "MEMORY_ENTRY_SCHEMA", "REFLECTION_ENTRY_SCHEMA", "RETRIEVE_RESULT_SCHEMA",
    "validate_memory_entry", "validate_reflection_entry",
    "ADD_RESULT_SCHEMA", "MEMORY_TAGS", "ENTRY_SCHEMA_VERSION", "STORE_SCHEMA_VERSION",
    "MEMORY_SCHEMA_VERSION",
    # 存储与检索
    "MemoryStore", "MemoryView", "LOCK_TIMEOUT_S",
    "situation_key", "situation_of", "obs_tag_weights", "extract_candidates", "similarity",
    "utility_score", "bm25_score", "tokenize", "estimate_tokens", "render_entry",
    "render_entries",
    # 快照
    "snapshot_path", "build_header", "read_snapshot", "write_snapshot",
    # 错误
    "MemoryIsolationError", "MemoryCorruptError", "MemoryLockedError", "MemoryQuotaError",
    "SnapshotMissingError", "MemoryWriteFailedError", "MEMORY_ERROR_CODES",
]
