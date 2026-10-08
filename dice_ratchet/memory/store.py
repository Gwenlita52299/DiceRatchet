"""MOD-05 ``MemoryStore``：写入管线、去重/冲突、容量/TTL、检索、快照/回滚、自检（PRD §6.2/§8）。

**时间只来自调用方传入的 ``tick``**（NFR-05-1）：``__init__`` 不注入墙钟，``created_at``
只是审计展示（取 ``ReflectionEntry.created_at``，不计入 ``content_sha256``）。

**INV-5 四拦截点**（PRD §6.5，缺一不可）::

    I1 源头  add_reflection / add_memory 入口：provenance.split == "train" 且 seed ∈ train
    I2 文本  落盘前逐字段扫 text/lesson/tags/action_hint（MOD-02 scan_text + seed 正则）
    I3 兜底  snapshot() / verify(full=True) 复扫全库
    I4 出口  retrieve() 返回前对入选条目做廉价字面量复检

I3/I4 是本模块兜底，**不替代** MOD-02 的扫描（PRD §6.5 末段）。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import write_bytes_atomic
from dice_ratchet.sandbox import audit as sandbox_audit
from dice_ratchet.sandbox import honeypot as sandbox_honeypot

from .errors import (MemoryIsolationError, MemoryLockedError, MemoryWriteFailedError,
                     SnapshotMissingError)
from .retriever import (doc_tokens, extract_candidates, obs_tag_weights, render_entries,
                        render_entry, score_entry, select_with_budget, situation_key,
                        similarity, stage_bucket, utility_score)
from .schemas import (ENTRY_SCHEMA_VERSION, MEMORY_TAGS, POLARITIES, STORE_SCHEMA_VERSION,
                      AddResult, DroppedEntry, MemoryCandidate, MemoryEntry, Provenance,
                      ReflectionEntry, RetrieveResult, RollbackReport, SituationKey,
                      StoreStats, VerifyReport, canonical_json, content_sha256_of, round6,
                      stable_id, validate_reflection_entry)
from .snapshot import (build_header, entries_sha256, entry_line, find_snapshots,
                       read_snapshot, snapshot_path, write_snapshot)

__all__ = ["MemoryStore", "MemoryView", "LOCK_TIMEOUT_S", "MAX_ACTIVE_FILE_BYTES"]

#: 压实/快照锁的等待上限（PRD §10：> 5 s 视为被占用 → 跳过本次压实）。
LOCK_TIMEOUT_S: Final[float] = 5.0
#: 陈锁接管阈值：崩溃残留的 ``memory.lock`` 超过该秒数即被接管（避免永久锁死）。
LOCK_STALE_S: Final[float] = 60.0
#: 单文件上限（NFR-05-4：≤ 16 MB）。
MAX_ACTIVE_FILE_BYTES: Final[int] = 16 * 1024 * 1024
#: 连续写失败达到该次数后本 run 停写（PRD §10 E_IO_MEMORY_WRITE_FAILED）。
MAX_WRITE_FAILURES: Final[int] = 3
#: 改写/删除（``_replace``/``_archive``）攒到多少行才整档重写一次 ``active`` 文件。
#: 改行只能靠整档重写落盘，而合并是高频事件；攒批把每次写入的 2.4 MB 重写摊薄到
#: 1/32，代价是崩溃时最多丢这 32 行的元数据更新（``snapshot()``/``close()`` 仍强制压实）。
DIRTY_ROWS_PER_COMPACT: Final[int] = 32

_NUM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z_])[0-9]{3,7}(?![0-9A-Za-z_])")
_SEED_HINT_RE: Final[re.Pattern[str]] = re.compile(r"seed\s*[:=#]?\s*[0-9]{1,7}", re.IGNORECASE)
_SEED_PATTERN: Final[str] = r"\bseed\b\s*[:=#]?\s*\d{1,7}"

_DEFAULT_COUNTERS: Final[dict[str, int]] = {
    "candidate_count": 0, "added": 0, "merged": 0, "conflicted": 0, "quarantined": 0,
    "evicted": 0, "appends": 0, "snapshot_count": 0, "rollback_count": 0,
    "retrieve_calls": 0, "ref_total": 0, "ref_adopted": 0, "seed_leak_blocks": 0,
    "write_failures": 0, "conflict_unresolved": 0, "compact_count": 0,
}


# =====================================================================
# MemoryStore
# =====================================================================

class MemoryStore:
    """``runs/<run_id>/memory/`` 的单文件 JSONL 记忆库 + 全量内存索引（PRD §7.1 阶段一）。"""

    def __init__(self, run_dir: Path, cfg: Any, *,
                 config_dir: Path = Path("configs"),
                 run_id: str | None = None,
                 on_scan: Callable[[str, str], Sequence[Any]] | None = None,
                 on_embed: Callable[[str], Sequence[float]] | None = None,
                 on_extract: Callable[[ReflectionEntry], Sequence[Mapping[str, Any]]]
                 | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.cfg = cfg
        self.config_dir = Path(config_dir)
        self.run_id = run_id or self.run_dir.name
        self._on_scan = on_scan
        self._on_embed = on_embed
        self._on_extract = on_extract

        self.memory_dir = self.run_dir / "memory"
        self.reflections_dir = self.run_dir / "reflections"
        self.audit_dir = self.run_dir / "audit"
        self.metrics_dir = self.run_dir / "metrics"
        for directory in (self.memory_dir, self.reflections_dir, self.audit_dir,
                          self.metrics_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.active_path = self.memory_dir / "memory_active.jsonl"
        self.evicted_path = self.memory_dir / "memory_evicted.jsonl"
        self.quarantine_path = self.memory_dir / "quarantine.jsonl"
        self.index_path = self.memory_dir / "index.json"
        self.lock_path = self.memory_dir / "memory.lock"
        self.reflections_path = self.reflections_dir / "reflections.jsonl"
        self.leak_quarantine_path = self.reflections_dir / "leak_quarantine.jsonl"
        self.leak_events_path = self.audit_dir / "leak_events.jsonl"
        self.metrics_path = self.metrics_dir / "memory.jsonl"

        self._entries: dict[str, MemoryEntry] = {}
        self._evicted: dict[str, MemoryEntry] = {}
        self._df: dict[str, int] = {}
        self._doc_len: dict[str, int] = {}
        self._doc_tokens_cache: dict[str, list[str]] = {}
        self._doc_freq_cache: dict[str, dict[str, int]] = {}
        self._line_cache: dict[str, str] = {}
        self._total_len = 0
        self._appends = 0
        self._write_failures = 0
        self._write_disabled = False
        self._emitting = False
        self._last_tick = 0
        # 自上次压实以来「行内容被改写（``_replace``）或行被删除（``_archive``）」的行数。
        # 攒到 ``DIRTY_ROWS_PER_COMPACT`` 才整档重写一次。
        #
        # **为什么必须攒**：合并（重复反思不断累加 ``n_ref``/``merged_from``）是高频事件，
        # 而改行落盘只能整档重写——每次 ``add_reflection()`` 都要写掉整个 ``active`` 文件
        # （2000 条约 2.4 MB）。实测该路径 84% 的时间在盘上（``_compact`` 3.8 ms +
        # ``_save_index`` 1.6 ms / 次，带分支覆盖率）；CI 共享 runner 的盘更慢，P95 直接
        # 冲到 125 ms，击穿 NFR-05-2 的 15 ms 预算（折算后 48 ms）。攒批把写盘量按 32 倍
        # 摊薄，同时把崩溃时的未落盘窗口限制在这 32 行以内（``snapshot()``/``close()``
        # 都会强制压实，见 ``_after_write`` 与 ``close``）。
        self._dirty_rows = 0

        self._forbidden_seeds: dict[str, str] = {}
        self._forbidden_tokens: dict[str, str] = {}
        self._train_seeds: set[int] = set()
        self._index_state: dict[str, Any] = {}
        self._counters: dict[str, int] = dict(_DEFAULT_COUNTERS)
        self._evicted_count = 0
        self._load_seed_protocol()

        self._index_state = self._load_index()
        self._counters.update(
            {k: int(v) for k, v in (self._index_state.get("counters") or {}).items()
             if isinstance(v, (int, float))})
        self._evicted_count = int(self._index_state.get("evicted_count", 0))
        self._load_active()
        self._rebuild_index()
        self._last_tick = max([self._last_tick, int(self._index_state.get("last_tick", 0))])

    # ---- 基本属性 ---------------------------------------------------------------
    @property
    def memory_version(self) -> int:
        return int(self._index_state.get("memory_version", 0))

    @property
    def parent_memory_version(self) -> int:
        return int(self._index_state.get("parent_memory_version", 0))

    @property
    def harness_version(self) -> int:
        return int(self._index_state.get("harness_version", 0))

    @property
    def generation(self) -> int:
        return int(self._index_state.get("generation", 0))

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    @property
    def snapshot_every_generation(self) -> bool:
        """``memory.snapshot.every_generation``：MOD-06 在代际边界据此决定是否快照。"""
        snapshot_cfg = getattr(self.cfg, "snapshot", None)
        return bool(getattr(snapshot_cfg, "every_generation", True))

    def entries(self) -> tuple[MemoryEntry, ...]:
        """当前 live 条目（按 ``entry_id`` 升序；确定性视图）。"""
        return tuple(self._entries[k] for k in sorted(self._entries))

    def get(self, entry_id: str) -> MemoryEntry | None:
        return self._entries.get(str(entry_id))

    # =================================================================
    # 种子协议与 INV-5 扫描（I1/I2/I3/I4）
    # =================================================================
    def _load_seed_protocol(self) -> None:
        """读 ``configs/seeds.yaml``：train 全集（I1）与 heldout/test 全集（I2/I3/I4）。"""
        try:
            self._forbidden_seeds = dict(
                sandbox_audit.load_forbidden_seeds(self.config_dir))
        except Exception:  # noqa: BLE001 - 缺 seeds.yaml 时降级为空集（不阻断训练）
            self._forbidden_seeds = {}
        tokens: dict[str, str] = {}
        for token in sandbox_honeypot.sentinel_tokens():
            tokens[str(token)] = "canary"
        self._forbidden_tokens = tokens
        train: set[int] = set()
        try:
            from dice_ratchet.infra.config import load_seeds

            proto = load_seeds(self.config_dir)
            spec = (proto.splits or {}).get("train") or {}
            if isinstance(spec, Mapping):
                if "start" in spec and "end" in spec:
                    train.update(range(int(spec["start"]), int(spec["end"]) + 1))
                for bspec in (spec.get("baskets") or {}).values():
                    if isinstance(bspec, Mapping) and "start" in bspec and "end" in bspec:
                        train.update(range(int(bspec["start"]), int(bspec["end"]) + 1))
        except Exception:  # noqa: BLE001 - 缺协议时 I1 退化为「只信 provenance.split」
            train = set()
        self._train_seeds = train

    def _forbidden_map(self) -> dict[str, str]:
        merged = dict(self._forbidden_seeds)
        for token, klass in self._forbidden_tokens.items():
            merged.setdefault(token, klass)
        return merged

    #: I1：仅 train 且 seed 属于 train 窗口（PRD §6.5）
    def check_admission(self, reflection: ReflectionEntry | None,
                        provenance: Provenance) -> None:
        if str(provenance.split) != "train":
            raise MemoryIsolationError(
                f"I1：split={provenance.split!r} 的局禁止写记忆（仅 train 可写）",
                hint="评估局使用 memory.frozen(v) 只读视图（Spec §8.4 B-19）")
        if self._train_seeds and int(provenance.seed) not in self._train_seeds:
            raise MemoryIsolationError(
                f"I1：provenance.seed={provenance.seed} 不属于 seeds.yaml 的 train 窗口",
                hint=f"train 窗口大小={len(self._train_seeds)}；seed 只能经 MOD-08 seeds.take 取用")
        if reflection is not None:
            if str(reflection.split) != "train":
                raise MemoryIsolationError(
                    f"I1：ReflectionEntry.split={reflection.split!r} 必须为 'train'")
            if int(reflection.seed) != int(provenance.seed):
                raise MemoryIsolationError(
                    f"I1：reflection.seed={reflection.seed} != provenance.seed={provenance.seed}",
                    hint="provenance 是唯一的 seed 入口；两者不一致说明调用点越权")

    def _scan_text(self, text: str, *, field_name: str, episode_id: str,
                   step: int, line_ref: str = "") -> list[dict[str, Any]]:
        """I2/I3/I4 共用的字面量扫描（先扫 forbidden，再调 MOD-02 交叉校验）。"""
        if not isinstance(text, str) or not text:
            return []
        hits: list[dict[str, Any]] = []
        for match in _NUM_TOKEN_RE.finditer(text):
            token = match.group(0)
            if token not in self._forbidden_seeds:
                continue
            window = text[max(0, match.start() - 24): match.end() + 8]
            pattern = _SEED_PATTERN if _SEED_HINT_RE.search(window) else token
            hits.append({"pattern": pattern, "span": [match.start(), match.end()],
                         "token": token, "field": field_name, "leak_class": "seed_leak"})
        for token, klass in self._forbidden_tokens.items():
            start = 0
            while True:
                idx = text.find(token, start)
                if idx < 0:
                    break
                hits.append({"pattern": re.escape(token),
                             "span": [idx, idx + len(token)], "token": token,
                             "field": field_name, "leak_class": klass})
                start = idx + len(token)
        # MOD-02 交叉校验（PRD §6.5 I2 要求必须调用 scan_text）
        try:
            findings = self._on_scan(text, "memory") if self._on_scan is not None else \
                sandbox_audit.scan_text(text, forbidden=self._forbidden_map(),
                                        surface="memory", episode_id=episode_id,
                                        step=step, line_ref=line_ref)
        except Exception:  # noqa: BLE001 - 交叉校验失败不得吞掉本地命中的命中
            findings = []
        for finding in findings or ():
            token_class = str(getattr(finding, "token_class", "seed_leak"))
            if any(h["pattern"] == token_class for h in hits):
                continue
            hits.append({"pattern": token_class, "span": [0, 0],
                         "token": token_class, "field": field_name,
                         "leak_class": token_class,
                         "evidence": str(getattr(finding, "evidence", ""))})
        return hits

    def _scan_reflection(self, reflection: ReflectionEntry) -> list[dict[str, Any]]:
        """I2：逐字段扫 ``text/tags/failure_cause/truncation_reason``。"""
        fields: list[tuple[str, Any]] = [
            ("text", reflection.text),
            ("tags", " ".join(str(t) for t in reflection.tags)),
            ("failure_cause", reflection.failure_cause),
            ("truncation_reason", reflection.truncation_reason or ""),
        ]
        hits: list[dict[str, Any]] = []
        for name, value in fields:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            hits.extend(self._scan_text(text, field_name=name,
                                        episode_id=reflection.episode_id,
                                        step=int(reflection.tick)))
        return hits

    def _scan_entry(self, entry: MemoryEntry, *, field_name: str = "entry",
                    point: str = "I3") -> list[dict[str, Any]]:
        """I3/I4：扫条目 ``lesson/tags/action_hint``（廉价字面量复检）。"""
        fields: list[tuple[str, Any]] = [
            ("lesson", entry.lesson),
            ("tags", " ".join(str(t) for t in entry.tags)),
            ("action_hint", json.dumps(entry.action_hint or {}, ensure_ascii=False)),
        ]
        hits: list[dict[str, Any]] = []
        for name, value in fields:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for hit in self._scan_text(text, field_name=name,
                                       episode_id=entry.source_episode_id,
                                       step=int(entry.created_tick)):
                hit["point"] = point
                hit["entry_id"] = entry.entry_id
                hits.append(hit)
        return hits

    # =================================================================
    # 落盘原语
    # =================================================================
    def _append_line(self, path: Path, line: str, *, fsync: bool = True,
                     count_failure: bool = True) -> None:
        """追加一行（自带 ``\\n``）；磁盘/权限失败 → ``E_IO_MEMORY_WRITE_FAILED``。

        ``count_failure=False``：埋点/隔离等旁路写入失败不累加「本 run 停写」计数
        （PRD §10 的第 3 次失败指的是**记忆数据**写入）。
        """
        if self._write_disabled:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                if fsync:
                    os.fsync(handle.fileno())
        except OSError as exc:
            if count_failure:
                self._write_failures += 1
                self._counters["write_failures"] = self._write_failures
                self._emit("memory.write_failed", code=E.E_IO_MEMORY_WRITE_FAILED,
                           message=f"{type(exc).__name__}: {exc}", path=str(path))
                if self._write_failures >= MAX_WRITE_FAILURES:
                    self._write_disabled = True
                    self._emit("memory.write_failed", code=E.E_IO_MEMORY_WRITE_FAILED,
                               message=f"连续 {self._write_failures} 次写失败：本 run 停写",
                               disabled=True)
            raise MemoryWriteFailedError(f"记忆写入失败：{path}: {exc}",
                                         hint="记忆是增强项，本局继续；连续 3 次失败后停写")

    def _try_lock(self, timeout_s: float = LOCK_TIMEOUT_S) -> bool:
        """尝试拿 ``memory/memory.lock``；占用超过 ``timeout_s`` 返回 ``False``（跳过压实）。

        陈锁（> ``LOCK_STALE_S``，通常是上一个进程崩溃残留）直接接管，避免压实/快照
        被永久锁死；判定用的是文件 mtime 与墙钟差，只影响互斥，不参与打分/哈希。
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()}\n")
                return True
            except FileExistsError:
                if self._lock_is_stale():
                    try:
                        self.lock_path.unlink(missing_ok=True)
                    except OSError:  # pragma: no cover
                        pass
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
            except OSError:  # pragma: no cover - 极端文件系统错误：退化为不加锁
                return True

    def _lock_is_stale(self) -> bool:
        try:
            age = self._wall_now() - self.lock_path.stat().st_mtime
        except OSError:
            return False
        return age > LOCK_STALE_S

    @staticmethod
    def _wall_now() -> float:
        from datetime import datetime, timezone  # noqa: PLC0415 - 见 NFR-05-1 的说明

        return datetime.now(tz=timezone.utc).timestamp()

    def _unlock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 删除失败不影响正确性
            pass

    def _compact(self) -> bool:
        """压实：tmp + ``fsync`` + ``os.replace``（FR-05-4）；拿不到锁则跳过。"""
        if not self._try_lock():
            self._emit("memory.write_failed", code=E.E_IO_MEMORY_LOCKED,
                       message="memory.lock 被占用 > 5 s：跳过本次压实，写入仍走追加")
            return False
        try:
            self._compact_locked()
            return True
        finally:
            self._unlock()

    def _compact_locked(self) -> None:
        """已持锁的压实实现（``snapshot()`` 复用，避免嵌套取锁）。"""
        lines = [self._line_of(e) for e in self._file_order_entries()]
        body = "".join(line + "\n" for line in lines)
        write_bytes_atomic(self.active_path, body.encode("utf-8"), fsync=True)
        self._appends = 0
        self._counters["appends"] = 0
        self._counters["compact_count"] = int(self._counters.get("compact_count", 0)) + 1
        self._dirty_rows = 0
        self._save_index()

    def _file_order_entries(self) -> list[MemoryEntry]:
        """active 库的落盘顺序（= 写入顺序；``_compact`` 与 ``snapshot`` 共用）。

        **不排序**：``dict`` 保持插入顺序、且对已有键赋值不改变位置，因此
        「active 文件顺序 == 快照条目顺序」，``rollback`` 可逐字节恢复（GWT-05-08）。
        公开的 ``entries()`` 仍按 ``entry_id`` 排序，作为确定性 API 视图。
        """
        return list(self._entries.values())

    def _check_quota(self) -> None:
        """E_IO_MEMORY_QUOTA：单文件 > 16 MB 或 ``active > 2*max_entries``（PRD §10）。

        先强制淘汰，仍超限则停写并告警（**读不受影响**）。
        """
        max_entries = int(getattr(self.cfg, "max_entries", 2000))
        if not self._over_quota(max_entries):
            return
        self.evict_capacity(self._last_tick)
        self._compact()
        if self._over_quota(max_entries):
            self._write_disabled = True
            self._emit("memory.write_failed", code=E.E_IO_MEMORY_QUOTA,
                       message="强制淘汰+压实后仍超限：本 run 停写（读不受影响）",
                       disabled=True)

    def _over_quota(self, max_entries: int) -> bool:
        active = sum(1 for e in self._entries.values() if e.status == "active")
        try:
            size = self.active_path.stat().st_size
        except OSError:
            size = 0
        return size > MAX_ACTIVE_FILE_BYTES or active > 2 * max_entries

    # =================================================================
    # 索引
    # =================================================================
    def _rebuild_index(self) -> None:
        self._df = {}
        self._doc_len = {}
        self._doc_tokens_cache = {}
        self._doc_freq_cache = {}
        self._line_cache = {}
        self._total_len = 0
        for entry in self._entries.values():
            tokens = doc_tokens(entry)
            self._doc_tokens_cache[entry.entry_id] = tokens
            self._doc_freq_cache[entry.entry_id] = Counter(tokens)
            self._line_cache[entry.entry_id] = entry_line(entry)
            self._doc_len[entry.entry_id] = len(tokens)
            self._total_len += len(tokens)
            for token in set(tokens):
                self._df[token] = self._df.get(token, 0) + 1

    def _line_of(self, entry: MemoryEntry) -> str:
        """条目行（缓存）：2000 条压实若每次现算 ``json.dumps`` 要 ~14 ms，超过
        ``add_reflection`` 的 P95 预算（NFR-05-2）；条目不可变，缓存安全。"""
        line = self._line_cache.get(entry.entry_id)
        if line is None:
            line = entry_line(entry)
            self._line_cache[entry.entry_id] = line
        return line

    def _index_add(self, entry: MemoryEntry) -> None:
        tokens = doc_tokens(entry)
        self._doc_tokens_cache[entry.entry_id] = tokens
        self._doc_freq_cache[entry.entry_id] = Counter(tokens)
        self._doc_len[entry.entry_id] = len(tokens)
        self._total_len += len(tokens)
        for token in set(tokens):
            self._df[token] = self._df.get(token, 0) + 1

    def _index_remove(self, entry: MemoryEntry) -> None:
        self._line_cache.pop(entry.entry_id, None)
        self._doc_freq_cache.pop(entry.entry_id, None)
        tokens = self._doc_tokens_cache.pop(entry.entry_id, None)
        length = self._doc_len.pop(entry.entry_id, 0)
        self._total_len = max(0, self._total_len - length)
        for token in set(tokens if tokens is not None else doc_tokens(entry)):
            self._df[token] = max(0, self._df.get(token, 0) - 1)

    def _stats_for_ranking(self) -> tuple[dict[str, int], int, float]:
        n_docs = len(self._entries)
        avgdl = (self._total_len / n_docs) if n_docs else 0.0
        return self._df, n_docs, avgdl

    def _tokens_map_for(self, entries: Mapping[str, MemoryEntry]
                        ) -> dict[str, list[str]]:
        """条目的文档 token（命中缓存；``MemoryView`` 的独立条目集现算一次）。

        NFR-05-2：2000 条 active 下 ``retrieve()`` P95 ≤ 50 ms——分词是热点，必须缓存。
        """
        if entries is self._entries:
            return self._doc_tokens_cache
        return {entry_id: doc_tokens(entry) for entry_id, entry in entries.items()}

    # =================================================================
    # index.json
    # =================================================================
    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._quarantine_raw(reason="corrupt", raw="", path=str(self.index_path),
                                 code=E.E_IO_MEMORY_CORRUPT)
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    def _save_index(self) -> None:
        active = sum(1 for e in self._entries.values() if e.status == "active")
        payload = {
            "store_schema_version": STORE_SCHEMA_VERSION,
            "entry_schema_version": ENTRY_SCHEMA_VERSION,
            "memory_version": self.memory_version,
            "parent_memory_version": self.parent_memory_version,
            "harness_version": self.harness_version,
            "generation": self.generation,
            "run_id": self.run_id,
            "config_hash": self._config_hash(),
            "entry_count": len(self._entries),
            "active_count": active,
            "evicted_count": self._evicted_count,
            "last_tick": self._last_tick,
            "counters": dict(self._counters),
            "lineage": list(self._index_state.get("lineage", [])),
        }
        try:
            write_bytes_atomic(self.index_path,
                               (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")) + "\n").encode("utf-8"),
                               fsync=True)
        except InfraError:  # pragma: no cover - 索引写失败不阻断主流程
            pass
        self._index_state = payload

    def _config_hash(self) -> str:
        try:
            payload = asdict(self.cfg) if hasattr(self.cfg, "__dataclass_fields__") \
                else dict(self.cfg)
        except Exception:  # noqa: BLE001 - 非 dataclass 配置：退化为 repr 摘要
            payload = {"repr": repr(self.cfg)}
        return content_sha256_of({"memory": payload})[:16]

    def _record_lineage(self, memory_version: int) -> None:
        lineage = list(self._index_state.get("lineage", []))
        lineage.append({"generation": self.generation,
                        "harness_version": self.harness_version,
                        "memory_version": int(memory_version)})
        self._index_state["lineage"] = lineage[-64:]

    # =================================================================
    # 加载与隔离
    # =================================================================
    def _load_active(self) -> None:
        """加载 ``memory_active.jsonl``：坏行/重复 id/半行 → quarantine，其余照常可用。"""
        if not self.active_path.is_file():
            return
        try:
            text = self.active_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            self._quarantine_raw(reason="corrupt", raw="", path=str(self.active_path),
                                 code=E.E_IO_MEMORY_CORRUPT)
            return
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            last = index == len(lines) - 1
            if last and not text.endswith("\n"):
                # 半行（崩溃残留）：按 E_IO_MEMORY_CORRUPT 截断到最后完整 \n
                self._quarantine_raw(reason="corrupt", raw=line,
                                     path=str(self.active_path),
                                     code=E.E_IO_MEMORY_CORRUPT)
                continue
            try:
                entry = MemoryEntry.from_json(line)
            except InfraError as exc:
                self._quarantine_raw(reason="corrupt", raw=line,
                                     path=str(self.active_path),
                                     code=E.E_IO_MEMORY_CORRUPT,
                                     message=getattr(exc, "message", str(exc)))
                continue
            if entry.entry_id in self._entries:
                self._quarantine_raw(reason="corrupt", raw=line,
                                     path=str(self.active_path),
                                     code=E.E_IO_MEMORY_CORRUPT,
                                     message=f"entry_id 重复：{entry.entry_id}")
                continue
            if entry.status == "evicted":
                self._evicted[entry.entry_id] = entry
                self._evicted_count = max(self._evicted_count, len(self._evicted))
                continue
            self._entries[entry.entry_id] = entry
            self._last_tick = max(self._last_tick, int(entry.created_tick),
                                  int(entry.last_ref_tick or 0))

    def _quarantine_raw(self, *, reason: str, raw: str, path: str, code: E,
                        message: str = "", entry_id: str | None = None,
                        reflection_id: str | None = None,
                        hits: Sequence[Mapping[str, Any]] = ()) -> None:
        row = {"_type": "memory_quarantine", "reason": reason, "path": path,
               "raw": raw[:2000], "message": message, "code": str(code),
               "ts": self._now_iso(), "run_id": self.run_id}
        if entry_id:
            row["entry_id"] = entry_id
        if reflection_id:
            row["reflection_id"] = reflection_id
        if hits:
            row["hits"] = [dict(h) for h in hits]
        try:
            self._append_line(self.quarantine_path, json.dumps(row, ensure_ascii=False,
                                                               separators=(",", ":")))
        except MemoryWriteFailedError:  # pragma: no cover - 隔离写失败不阻断加载
            pass
        self._counters["quarantined"] = int(self._counters.get("quarantined", 0)) + 1
        self._emit("memory.quarantine", reason=reason, code=code, path=path,
                   entry_id=entry_id, reflection_id=reflection_id,
                   matched_pattern=(hits[0]["pattern"] if hits else None))

    def _quarantine_entry(self, entry: MemoryEntry, *, reason: str,
                          hits: Sequence[Mapping[str, Any]] = (),
                          point: str = "I3") -> MemoryEntry:
        updated = entry.with_updates(status="quarantined")
        self._replace(updated)
        self._counters["quarantined"] = int(self._counters.get("quarantined", 0)) + 1
        self._counters["seed_leak_blocks"] = int(
            self._counters.get("seed_leak_blocks", 0)) + 1
        row = {"_type": "memory_quarantine", "reason": reason, "entry_id": entry.entry_id,
               "entry": updated.to_dict(), "hits": [dict(h) for h in hits],
               "ts": self._now_iso(), "run_id": self.run_id}
        try:
            self._append_line(self.quarantine_path,
                              json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        except MemoryWriteFailedError:  # pragma: no cover
            pass
        self._emit("memory.quarantine", reason=reason, entry_id=entry.entry_id,
                   interception_point=point,
                   matched_pattern=(hits[0]["pattern"] if hits else None))
        return updated

    def _replace(self, entry: MemoryEntry) -> None:
        old = self._entries.get(entry.entry_id)
        if old is not None:
            self._index_remove(old)
        self._entries[entry.entry_id] = entry
        self._index_add(entry)
        self._line_cache[entry.entry_id] = entry_line(entry)
        # 行内容变了：只能靠整档重写落盘（``_load_active`` 视重复 entry_id 为损坏）。
        # 攒够 ``DIRTY_ROWS_PER_COMPACT`` 行再重写，避免每次合并都写掉整个 active 文件。
        self._dirty_rows += 1

    # =================================================================
    # 事件与埋点
    # =================================================================
    def _now_iso(self) -> str:
        # 审计字段：不参与打分、不入 content_sha256（NFR-05-1）
        from datetime import datetime, timezone  # noqa: PLC0415 - 仅此处需要

        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _emit(self, event: str, **fields: Any) -> None:
        from dice_ratchet.infra import events  # noqa: PLC0415 - 延迟导入避免循环

        row = {"ts": self._now_iso(), "run_id": self.run_id,
               "memory_version": self.memory_version,
               "harness_version": self.harness_version,
               "generation": self.generation, **fields}
        try:
            events.emit(event, **row)
        except Exception:  # noqa: BLE001 - 事件系统异常不得阻断记忆写入/检索
            pass
        if self._emitting:
            # 重入保护：埋点落盘失败会再触发 _emit，此处置位避免无限递归
            return
        line = json.dumps({"event": event, **row}, ensure_ascii=False, separators=(",", ":"))
        self._emitting = True
        try:
            self._append_line(self.metrics_path, line, fsync=False, count_failure=False)
        except MemoryWriteFailedError:  # pragma: no cover - 埋点失败不阻断
            pass
        finally:
            self._emitting = False

    def _leak_event(self, *, point: str, hits: Sequence[Mapping[str, Any]],
                    field_name: str, episode_id: str = "", entry_id: str | None = None,
                    reflection_id: str | None = None) -> None:
        """I1–I4 命中事件 → ``audit/leak_events.jsonl``（MOD-02 审计器读）。"""
        row = {"ts": self._now_iso(), "run_id": self.run_id,
               "event": "memory.seed_leak_blocked", "interception_point": point,
               "field": field_name, "episode_id": episode_id,
               "entry_id": entry_id, "reflection_id": reflection_id,
               "matched_pattern": (hits[0]["pattern"] if hits else None),
               "hits": [dict(h) for h in hits]}
        try:
            self._append_line(self.leak_events_path,
                              json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        except MemoryWriteFailedError:  # pragma: no cover
            pass
        self._counters["seed_leak_blocks"] = int(
            self._counters.get("seed_leak_blocks", 0)) + 1
        self._emit("memory.seed_leak_blocked", interception_point=point,
                   field=field_name, matched_pattern=row["matched_pattern"],
                   source_profile="A" if self._write_disabled else "B")

    # =================================================================
    # 写入管线（FR-05-2/3/4/7/8）
    # =================================================================
    def add_reflection(self, reflection: ReflectionEntry, *,
                       provenance: Provenance) -> AddResult:
        """MOD-06 局末反思入站：I1 → I2 → 抽取 → 去重/冲突 → 落盘 → 淘汰/压实。"""
        if not self.enabled:
            return AddResult(memory_version=self.memory_version,
                             reasons=("memory_disabled",))
        if self._write_disabled:
            return AddResult(memory_version=self.memory_version,
                             reasons=("write_disabled",))
        self.check_admission(reflection, provenance)
        validate_reflection_entry(reflection.to_dict())

        scan_on_write = bool(getattr(getattr(self.cfg, "scan", None), "on_write", True))
        hits = self._scan_reflection(reflection) if scan_on_write else []
        if hits:
            self._quarantine_reflection(reflection, hits)
            return AddResult(quarantined=1, memory_version=self.memory_version,
                             reasons=("scan_quarantined",))

        candidates = self._extract(reflection)
        self._counters["candidate_count"] = int(
            self._counters.get("candidate_count", 0)) + len(candidates)

        recorded = replace(reflection, tags=tuple(reflection.tags),
                           scan={"status": "pass", "hits": []},
                           extracted_memory_ids=())
        self._append_reflection(recorded)

        added = merged = conflicted = 0
        entry_ids: list[str] = []
        reasons: list[str] = []
        for candidate in candidates:
            outcome = str(reflection.outcome)
            kind_name, entry_id = self._store_candidate(candidate, provenance, outcome)
            entry_ids.append(entry_id)
            reasons.append(kind_name)
            if kind_name == "added":
                added += 1
            elif kind_name == "merged":
                merged += 1
            else:
                conflicted += 1

        if entry_ids:
            updated = replace(recorded, extracted_memory_ids=tuple(entry_ids))
            # 修正 reflections.jsonl 的最后一行（重写该行，保持幂等可读）
            self._rewrite_last_reflection(updated)

        self._last_tick = max(self._last_tick, int(reflection.tick))
        evicted = self.sweep_ttl(int(reflection.tick))
        evicted += self.evict_capacity(int(reflection.tick))
        self._after_write()
        return AddResult(added=added, merged=merged, conflicted=conflicted,
                         quarantined=0, evicted=evicted,
                         memory_version=self.memory_version,
                         entry_ids=tuple(entry_ids), reasons=tuple(reasons))

    def add_memory(self, candidate: MemoryCandidate, *,
                   provenance: Provenance) -> AddResult:
        """直接写入一个已抽取的候选（不经过反思文本）。"""
        if not self.enabled:
            return AddResult(memory_version=self.memory_version,
                             reasons=("memory_disabled",))
        if self._write_disabled:
            return AddResult(memory_version=self.memory_version,
                             reasons=("write_disabled",))
        self.check_admission(None, provenance)
        hits = self._scan_entry_fields(candidate)
        if hits:
            self._emit("memory.seed_leak_blocked", interception_point="I2",
                       field=hits[0].get("field", "lesson"),
                       matched_pattern=hits[0]["pattern"])
            self._leak_event(point="I2", hits=hits,
                             field_name=str(hits[0].get("field", "lesson")),
                             episode_id=candidate.source_episode_id,
                             reflection_id=candidate.source_reflection_id)
            return AddResult(quarantined=1, memory_version=self.memory_version,
                             reasons=("scan_quarantined",))
        self._counters["candidate_count"] = int(
            self._counters.get("candidate_count", 0)) + 1
        # 直接写入的候选没有局末结果（``outcome`` 未知）：只累加 n_ref，不臆造胜负
        kind_name, entry_id = self._store_candidate(candidate, provenance, "")
        self._last_tick = max(self._last_tick, int(candidate.created_tick))
        evicted = self.sweep_ttl(int(candidate.created_tick))
        evicted += self.evict_capacity(int(candidate.created_tick))
        self._after_write()
        return AddResult(added=int(kind_name == "added"),
                         merged=int(kind_name == "merged"),
                         conflicted=int(kind_name == "conflicted"),
                         evicted=evicted, memory_version=self.memory_version,
                         entry_ids=(entry_id,), reasons=(kind_name,))

    def _extract(self, reflection: ReflectionEntry) -> list[MemoryCandidate]:
        """候选抽取：默认走确定性规则（FR-05-2）。

        ``memory.extract.use_llm=true`` 且组合根注入了 MOD-03 的 ``generate_json``
        适配器（``on_extract``）时先用 LLM 抽取；**任何**失败（超时/JSON 非法/字段不合法）
        都记 ``E_LLM_EXTRACT_FAILED`` 并降级为规则抽取（§9/§10，反思仍入库）。
        """
        limit = int(getattr(getattr(self.cfg, "extract", None),
                            "max_candidates_per_reflection", 3))
        use_llm = bool(getattr(getattr(self.cfg, "extract", None), "use_llm", False))
        if use_llm and self._on_extract is not None and limit > 0:
            try:
                raw = list(self._on_extract(reflection) or [])
                parsed = [c for c in (_llm_candidate(reflection, item, self.cfg)
                                      for item in raw) if c is not None]
                if parsed:
                    return parsed[:limit]
                raise ValueError("LLM 抽取未产出可用候选")
            except Exception as exc:  # noqa: BLE001 - 降级必须覆盖一切失败
                self._emit("memory.write_failed", code=E.E_LLM_EXTRACT_FAILED,
                           message=f"LLM 抽取失败，降级为规则抽取：{type(exc).__name__}: {exc}")
        return extract_candidates(reflection, self.cfg)

    def _scan_entry_fields(self, candidate: MemoryCandidate) -> list[dict[str, Any]]:
        fields = [("lesson", candidate.lesson),
                  ("tags", " ".join(str(t) for t in candidate.tags)),
                  ("action_hint", json.dumps(candidate.action_hint or {},
                                             ensure_ascii=False))]
        hits: list[dict[str, Any]] = []
        for name, value in fields:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for hit in self._scan_text(text, field_name=name,
                                       episode_id=candidate.source_episode_id,
                                       step=int(candidate.created_tick)):
                hit["point"] = "I2"
                hits.append(hit)
        return hits

    def _append_reflection(self, reflection: ReflectionEntry) -> None:
        self._append_line(self.reflections_path, reflection.to_json())

    def _rewrite_last_reflection(self, reflection: ReflectionEntry) -> None:
        """把 ``reflections.jsonl`` 末行替换为带 ``extracted_memory_ids`` 的版本。"""
        if not self.reflections_path.is_file():
            return
        try:
            lines = self.reflections_path.read_text(encoding="utf-8").split("\n")
        except OSError:  # pragma: no cover
            return
        while lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return
        lines[-1] = reflection.to_json()
        try:
            write_bytes_atomic(self.reflections_path,
                               ("\n".join(lines) + "\n").encode("utf-8"), fsync=True)
        except InfraError:  # pragma: no cover
            pass

    def _quarantine_reflection(self, reflection: ReflectionEntry,
                               hits: Sequence[Mapping[str, Any]]) -> None:
        payload = {"_type": "memory_quarantine", "reason": "scan",
                   "reflection": reflection.to_dict(), "hits": [dict(h) for h in hits],
                   "ts": self._now_iso(), "run_id": self.run_id}
        for path in (self.leak_quarantine_path, self.quarantine_path):
            try:
                self._append_line(path, json.dumps(payload, ensure_ascii=False,
                                                   separators=(",", ":")))
            except MemoryWriteFailedError:  # pragma: no cover
                pass
        self._counters["quarantined"] = int(self._counters.get("quarantined", 0)) + 1
        self._leak_event(point="I2", hits=hits,
                         field_name=str(hits[0].get("field", "text")),
                         episode_id=reflection.episode_id,
                         reflection_id=reflection.reflection_id)
        self._emit("memory.quarantine", reason="scan",
                   reflection_id=reflection.reflection_id,
                   matched_pattern=hits[0]["pattern"])

    # ---- 单条候选的落库 ----------------------------------------------------------
    def _store_candidate(self, candidate: MemoryCandidate, provenance: Provenance,
                         outcome: str) -> tuple[str, str]:
        key = candidate.situation_key
        same = [e for e in self._entries.values()
                if e.situation_key == key and e.status in ("active", "superseded")]
        exact = [e for e in same if e.text_hash == candidate.text_hash]
        if exact:
            survivor = self._merge_survivor(exact)
            self._merge_into(survivor, candidate, outcome)
            return "merged", survivor.entry_id

        threshold = float(getattr(getattr(self.cfg, "dedup", None), "sim_threshold", 0.85))
        weights = getattr(self.cfg, "dedup", None)
        near = [e for e in same
                if similarity(candidate, e,
                              jaccard_weight=float(getattr(weights, "jaccard_weight", 0.5)),
                              char3_weight=float(getattr(weights, "char3_weight", 0.5)))
                >= threshold]
        if near:
            survivor = self._merge_survivor(near)
            self._merge_into(survivor, candidate, outcome)
            return "merged", survivor.entry_id

        conflicts = [e for e in same
                     if e.polarity != candidate.polarity
                     and _same_hint_kind(e, candidate)]
        entry = self._build_entry(candidate, provenance)
        conflict_group: str | None = None
        if conflicts:
            conflict_group = content_sha256_of(
                {"situation_key": key, "kind": _hint_kind(candidate)})[:12]
            entry = entry.with_updates(conflict_group=conflict_group)
            auto = bool(getattr(getattr(self.cfg, "conflict", None), "auto_supersede", False))
            winner = max(conflicts + [entry],
                         key=lambda e: (e.utility, -e.created_tick, e.entry_id))
            superseded = False
            if auto and winner.entry_id == entry.entry_id and _evidence_ok(entry, conflicts,
                                                                           self.cfg):
                old = conflicts[0]
                self._replace(old.with_updates(status="superseded",
                                               superseded_by=entry.entry_id))
                entry = entry.with_updates(supersedes=old.entry_id,
                                           conflict_group=conflict_group)
                superseded = True
            for other in conflicts:
                if other.conflict_group != conflict_group:
                    self._replace(other.with_updates(conflict_group=conflict_group))
            self._counters["conflicted"] = int(self._counters.get("conflicted", 0)) + 1
            if not superseded:
                self._counters["conflict_unresolved"] = int(
                    self._counters.get("conflict_unresolved", 0)) + 1
            self._emit("memory.conflict", conflict_group=conflict_group,
                       entry_ids=sorted([entry.entry_id] + [c.entry_id for c in conflicts]),
                       winner_id=winner.entry_id,
                       rule=("auto" if superseded else "suppress"))
            self._persist(entry)
            return "conflicted", entry.entry_id

        self._persist(entry)
        return "added", entry.entry_id

    def _build_entry(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryEntry:
        ttl = int(candidate.ttl_ticks or getattr(self.cfg, "ttl_ticks", 300))
        entry_id = stable_id("mem", candidate.situation_key, candidate.text_hash,
                             candidate.source_reflection_id or provenance.episode_id)
        entry = MemoryEntry(
            entry_id=entry_id,
            schema_version=ENTRY_SCHEMA_VERSION,
            created_at=str(candidate.created_at),
            created_tick=int(candidate.created_tick),
            run_id=str(provenance.run_id or self.run_id),
            generation=int(provenance.generation),
            harness_version=int(provenance.harness_version),
            text_hash=candidate.text_hash,
            content_sha256="",
            source_episode_id=str(candidate.source_episode_id or provenance.episode_id),
            source_reflection_id=str(candidate.source_reflection_id),
            situation=candidate.situation,
            situation_key=candidate.situation_key,
            tags=tuple(candidate.tags),
            lesson=candidate.lesson,
            action_hint=(None if candidate.action_hint is None else dict(candidate.action_hint)),
            polarity=str(candidate.polarity),
            confidence=round6(candidate.confidence),
            last_ref_tick=None,
            n_ref=0, n_adopted=0, n_win_adopted=0, n_not_adopted=0, n_win_not_adopted=0,
            utility=0.0,
            ttl_ticks=ttl,
            expires_tick=int(candidate.created_tick) + ttl,
            status="active",
            merged_from=(),
            embedding=self._embedding_for(candidate),
        )
        return entry.with_updates(utility=utility_score(entry, tick=entry.created_tick))

    def _embedding_for(self, candidate: MemoryCandidate) -> tuple[float, ...] | None:
        """向量检索（可选，Spec B-01）：``memory.embedding.enabled`` 且注入了 MOD-03
        ``embed()`` 时才计算；失败/维度不符一律降级为 ``None``（阶段一 ``w_vec=0``）。"""
        if candidate.embedding is not None:
            return tuple(candidate.embedding)
        embedding_cfg = getattr(self.cfg, "embedding", None)
        if not embedding_cfg or not getattr(embedding_cfg, "enabled", False):
            return None
        if self._on_embed is None:
            return None
        try:
            vector = tuple(float(x) for x in self._on_embed(candidate.lesson))
        except Exception:  # noqa: BLE001 - E_LLM_UNSUPPORTED / 后端失败 → 降级为 BM25
            return None
        dim = int(getattr(embedding_cfg, "dim", 0) or 0)
        if dim and len(vector) != dim:
            self._emit("memory.write_failed", code=E.E_LLM_UNSUPPORTED,
                       message=f"embedding 维度 {len(vector)} != memory.embedding.dim {dim}")
            return None
        return vector

    def _merge_survivor(self, candidates: Sequence[MemoryEntry]) -> MemoryEntry:
        """survivor = utility 高者 → created_tick 小者 → entry_id 小者（§8.1）。"""
        return sorted(candidates,
                      key=lambda e: (-e.utility, e.created_tick, e.entry_id))[0]

    def _merge_into(self, survivor: MemoryEntry, candidate: MemoryCandidate,
                    outcome: str) -> None:
        """精确/近似合并：**不新增行**，只累加 ``n_ref`` 与 ``merged_from``（FR-05-3）。

        候选在写入时没有 adopted 信息（判定归 MOD-04，局末由 ``record_usage`` 按 INV-7
        回填），因此合并**不臆造**采纳/胜负计数——只有 ``add_reflection`` 带环境原生
        ``outcome`` 时才把这次重复观测记进 ``n_not_adopted`` / ``n_win_not_adopted``
        （即「又见到一次、且未被采纳」，保守口径，避免自证式膨胀）。
        """
        merged_id = stable_id("mem", candidate.situation_key, candidate.text_hash,
                              candidate.source_reflection_id)
        win = 1 if outcome == "win" else 0
        known_outcome = outcome in ("win", "lose")
        merged_from = tuple(sorted(set(survivor.merged_from) | {merged_id}))
        if merged_id == survivor.entry_id:
            # 同一来源的重复写入（幂等重放）：只累加引用，不写自环 provenance
            merged_from = survivor.merged_from
        changes: dict[str, Any] = {
            "n_ref": survivor.n_ref + 1,
            "confidence": round6(max(survivor.confidence, candidate.confidence)),
            "merged_from": merged_from,
        }
        if known_outcome:
            changes["n_not_adopted"] = survivor.n_not_adopted + 1
            changes["n_win_not_adopted"] = survivor.n_win_not_adopted + win
        updated = survivor.with_updates(**changes)
        updated = updated.with_updates(utility=utility_score(updated,
                                                             tick=max(self._last_tick,
                                                                      candidate.created_tick)))
        self._replace(updated)
        self._counters["merged"] = int(self._counters.get("merged", 0)) + 1
        self._emit("memory.merge", survivor_id=survivor.entry_id, merged_id=merged_id,
                   sim=similarity(candidate, survivor), reason="exact_or_near")

    def _persist(self, entry: MemoryEntry) -> None:
        self._append_line(self.active_path, self._line_of(entry))
        self._entries[entry.entry_id] = entry
        self._index_add(entry)
        self._appends += 1
        self._counters["appends"] = int(self._counters.get("appends", 0)) + 1
        self._counters["added"] = int(self._counters.get("added", 0)) + 1
        self._touch_context(entry.harness_version, entry.generation)
        self._emit("memory.add", entry_id=entry.entry_id,
                   source_reflection_id=entry.source_reflection_id,
                   tags=list(entry.tags), polarity=entry.polarity, result="added")
        self._last_tick = max(self._last_tick, int(entry.created_tick))
        self._save_index()

    def _touch_context(self, harness_version: int, generation: int) -> None:
        """记忆库的「当前 harness 代际」= 已写入的最高 harness_version（FR-05-12 的基准）。

        MOD-06 每局重读 ``harness/version.json``；MOD-07 回滚 harness 后应显式调用
        ``set_harness_version(h')``，此后 ``harness_version > h'`` 的条目默认不参与检索。
        """
        self._index_state["harness_version"] = max(self.harness_version,
                                                   int(harness_version))
        self._index_state["generation"] = max(self.generation, int(generation))

    def set_harness_version(self, harness_version: int, *, generation: int | None = None
                            ) -> None:
        """显式声明当前 harness 代际（MOD-07 回滚/换代后调用；不写记忆条目）。"""
        self._index_state["harness_version"] = int(harness_version)
        if generation is not None:
            self._index_state["generation"] = int(generation)
        self._save_index()

    def set_generation(self, generation: int) -> None:
        self._index_state["generation"] = int(generation)
        self._save_index()

    def _after_write(self) -> None:
        """写入收尾：需要时压实（FR-05-4），随后查配额并落索引。

        ``_appends`` 与 ``_dirty_rows`` 都由 ``_compact_locked`` 清零，因此 ``>=`` 即
        「距上次压实已追加 / 已改写这么多行」。压实因此只在三种情形发生：

        1. 追加到 ``compact_every``（FR-05-4 的「每 ``compact_every`` 次」）；
        2. 改写/删除攒到 ``DIRTY_ROWS_PER_COMPACT`` 行；
        3. 配额兜底（``_check_quota`` 超限时强制压实）。

        改写落盘只能整档重写：库满（``active == max_entries``）时每次新增都会淘汰一条，
        合并又会改写 survivor 行——若每次写入都压实，就等于「每个 ``add_reflection()``
        重写 2000 行 / 2.4 MB」，CI 慢盘上 P95 实测 125 ms，击穿 NFR-05-2 折算后的 48 ms。
        """
        compact_every = int(getattr(self.cfg, "compact_every", 200))
        appended_enough = compact_every > 0 and self._appends >= compact_every
        if appended_enough or self._dirty_rows >= DIRTY_ROWS_PER_COMPACT:
            self._compact()
        self._check_quota()
        self._save_index()

    # =================================================================
    # 效用回填（FR-05-8）
    # =================================================================
    def record_usage(self, episode_key: str, memory_ids: Sequence[str],
                     outcome: str, *, adopted_ids: Sequence[str],
                     tick: int) -> None:
        """局末一次：更新引用/采纳/胜负计数与 ``last_ref_tick``，重算 ``utility``。"""
        if not self.enabled:
            return
        adopted = {str(m) for m in adopted_ids}
        seen: set[str] = set()
        touched = False
        for raw_id in memory_ids:
            entry_id = str(raw_id)
            if entry_id in seen:
                continue
            seen.add(entry_id)
            entry = self._entries.get(entry_id)
            if entry is None or entry.status not in ("active", "superseded"):
                continue
            win = 1 if str(outcome) == "win" else 0
            if entry_id in adopted:
                changes = {"n_adopted": entry.n_adopted + 1,
                           "n_win_adopted": entry.n_win_adopted + win}
            else:
                changes = {"n_not_adopted": entry.n_not_adopted + 1,
                           "n_win_not_adopted": entry.n_win_not_adopted + win}
            updated = entry.with_updates(n_ref=entry.n_ref + 1,
                                         last_ref_tick=int(tick), **changes)
            updated = updated.with_updates(utility=utility_score(updated, tick=int(tick)))
            self._replace(updated)
            self._counters["ref_total"] = int(self._counters.get("ref_total", 0)) + 1
            if entry_id in adopted:
                self._counters["ref_adopted"] = int(
                    self._counters.get("ref_adopted", 0)) + 1
            touched = True
        self._last_tick = max(self._last_tick, int(tick))
        if touched:
            self._compact()
        self._save_index()

    # =================================================================
    # 容量、TTL 与淘汰（FR-05-7 / §8.2）
    # =================================================================
    def evict_capacity(self, tick: int) -> int:
        """按 ``(utility, last_ref_tick, entry_id)`` 升序淘汰到 ``max_entries``。

        免疫条件：``n_ref >= 3`` 或 ``age_ticks < min_age_ticks``（默认 30 = 1 代）。
        """
        max_entries = int(getattr(self.cfg, "max_entries", 2000))
        active = [e for e in self._entries.values() if e.status == "active"]
        overflow = len(active) - max_entries
        if overflow <= 0:
            return 0
        min_age = int(getattr(self.cfg, "min_age_ticks", 30))
        immune_ids = {e.entry_id for e in active
                      if e.n_ref >= 3 or (int(tick) - e.created_tick) < min_age}
        pool = [e for e in active if e.entry_id not in immune_ids]
        pool.sort(key=lambda e: (e.utility,
                                 -1 if e.last_ref_tick is None else e.last_ref_tick,
                                 e.entry_id))
        evicted = 0
        for entry in pool[:overflow]:
            self._archive(entry, reason="capacity")
            evicted += 1
        return evicted

    def sweep_ttl(self, tick: int) -> int:
        """``expires_tick <= tick`` 即淘汰（``evict_reason="ttl"``）。"""
        evicted = 0
        for entry in [e for e in self._entries.values() if e.status == "active"]:
            if entry.expires_tick <= int(tick):
                self._archive(entry, reason="ttl")
                evicted += 1
        return evicted

    def _archive(self, entry: MemoryEntry, *, reason: str) -> None:
        """归档：从 active 库移出 → 追加 ``memory_evicted.jsonl`` → 事件 ``memory.evict``。

        淘汰只**删行**、不改行内容，所以只计入 ``_dirty_rows``（与 ``_replace`` 的改写
        一起攒批），不单独触发整档重写：``active`` 文件里多出的那一行在下次压实
        （攒批阈值 / ``snapshot()`` / ``close()``）或下次加载时被忽略。库满
        （``active == max_entries``）时每个新增都会淘汰一条，若每次淘汰都压实，就等于
        「每次写入全量重写 2000 行」（NFR-05-2 的预算即被击穿，CI 实测 125 ms）。
        """
        archived = entry.with_updates(status="evicted")
        self._entries.pop(entry.entry_id, None)
        self._line_cache.pop(entry.entry_id, None)
        self._evicted[entry.entry_id] = archived
        self._index_remove(entry)
        self._dirty_rows += 1
        self._evicted_count += 1
        self._counters["evicted"] = int(self._counters.get("evicted", 0)) + 1
        try:
            self._append_line(self.evicted_path, entry_line(archived))
        except MemoryWriteFailedError:  # pragma: no cover
            pass
        self._emit("memory.evict", entry_id=entry.entry_id, evict_reason=reason,
                   utility=entry.utility, n_ref=entry.n_ref)

    def evicted_entries(self) -> tuple[MemoryEntry, ...]:
        return tuple(self._evicted[k] for k in sorted(self._evicted))

    # =================================================================
    # 检索（FR-05-5/6/12，§6.3）
    # =================================================================
    def retrieve(self, obs: Mapping[str, Any], *, top_k: int | None = None,
                 token_budget: int | None = None, tick: int = 0,
                 tags_hint: Sequence[str] = (),
                 exclude_stale_harness: bool = True) -> RetrieveResult:
        """检索入口：任何内部异常都降级为空记忆 + ``memory.retrieve_failed``（§10）。"""
        if not self.enabled:
            return RetrieveResult()
        try:
            result = self._retrieve_over(self._entries, obs=obs, top_k=top_k,
                                         token_budget=token_budget, tick=tick,
                                         tags_hint=tags_hint,
                                         exclude_stale_harness=exclude_stale_harness)
        except Exception as exc:  # noqa: BLE001 - 记忆是增强而非依赖（INV-3 允许空）
            self._emit("memory.retrieve_failed", code=E.E_IO_MEMORY_CORRUPT,
                       message=f"{type(exc).__name__}: {exc}")
            return RetrieveResult()
        self._last_tick = max(self._last_tick, int(tick))
        return result

    def _retrieve_over(self, entries: Mapping[str, MemoryEntry], *,
                       obs: Mapping[str, Any], top_k: int | None,
                       token_budget: int | None, tick: int,
                       tags_hint: Sequence[str],
                       exclude_stale_harness: bool,
                       current_harness: int | None = None) -> RetrieveResult:
        started = time.perf_counter()
        retrieve_cfg = getattr(self.cfg, "retrieve", None)
        top_k = int(top_k if top_k is not None
                    else getattr(retrieve_cfg, "top_k", 8))
        token_budget = int(token_budget if token_budget is not None
                           else getattr(retrieve_cfg, "token_budget", 900))
        min_score = float(getattr(retrieve_cfg, "min_score", 0.15))
        bonus = float(getattr(retrieve_cfg, "same_situation_bonus", 0.25))
        weights = dict(getattr(retrieve_cfg, "weights",
                              {"tag": 1.0, "bm25": 0.6, "vector": 0.0,
                               "utility": 0.3, "recency": 0.1}))
        bm25_cfg = getattr(retrieve_cfg, "bm25", {"k1": 1.2, "b": 0.75})
        recency_lambda = float(getattr(retrieve_cfg, "recency_lambda", 0.01))

        key, _situation, obs_tags_tuple = situation_key(obs, tags_hint)
        tag_weights = obs_tag_weights(obs, tags_hint)
        query_tokens = list(dict.fromkeys([*obs_tags_tuple, *map(str, tags_hint)]))
        # 查询词去重排序在 2000 条打分里是纯重复劳动：这里算一次，循环内直接复用。
        query_set = tuple(sorted(set(query_tokens)))
        entry_tags = {e.entry_id: set(e.tags) for e in entries.values()}
        query_embedding = self._query_embedding(
            " ".join([*obs_tags_tuple, *map(str, tags_hint)]), weights)

        live = [e for e in entries.values()]
        tokens_map = self._tokens_map_for(entries)
        # BM25 的 (df, n_docs, avgdl)：对 ``self._entries`` 直接复用增量维护的索引
        # （``_index_add``/``_index_remove``）；否则（``MemoryView`` 的独立条目集）现算一次。
        if entries is self._entries:
            df, n_docs, avgdl = self._stats_for_ranking()
        else:
            df, n_docs, avgdl = _stats_from_tokens(tokens_map)

        dropped: list[DroppedEntry] = []
        scored: list[tuple[MemoryEntry, float]] = []
        harness_now = int(self.harness_version if current_harness is None
                          else current_harness)
        live_sorted = sorted(live, key=lambda e: e.entry_id)
        # 增量维护的 df/doc_len 与 ``live`` 同源时，逐条取缓存；否则退化为现算。
        indexed = df is self._df and tokens_map is self._doc_tokens_cache
        for entry in live_sorted:
            reason = self._hard_filter(entry, tick=tick,
                                       exclude_stale=exclude_stale_harness,
                                       harness_now=harness_now)
            if reason:
                dropped.append(DroppedEntry(entry_id=entry.entry_id, reason=reason))
                continue
            doc = tokens_map.get(entry.entry_id)
            score = score_entry(entry, obs_key=key, obs_tags=tag_weights,
                                query_tokens=query_tokens, df=df, n_docs=n_docs,
                                avgdl=avgdl, weights=weights, same_situation_bonus=bonus,
                                recency_lambda=recency_lambda, tick=int(tick),
                                query_embedding=query_embedding,
                                doc=doc,
                                doc_freqs=(self._doc_freq_cache.get(entry.entry_id)
                                           if indexed else None),
                                query_set=query_set, entry_tags=entry_tags.get(
                                    entry.entry_id),
                                bm25_k1=float(bm25_cfg.get("k1", 1.2)),
                                bm25_b=float(bm25_cfg.get("b", 0.75)))
            scored.append((entry, score))

        scored.sort(key=lambda item: (-item[1], item[0].entry_id))
        kept, suppressed = _suppress_conflicts(scored)
        dropped.extend(DroppedEntry(entry_id=e.entry_id, reason="conflict_suppressed",
                                    score=s) for e, s in suppressed)

        above = [(e, s) for e, s in kept if s >= min_score]
        dropped.extend(DroppedEntry(entry_id=e.entry_id, reason="below_min_score", score=s)
                       for e, s in kept if s < min_score)

        selected, budget_dropped, used = select_with_budget(
            above, top_k=top_k, token_budget=token_budget, line_of=render_entry)
        dropped.extend(DroppedEntry(entry_id=e.entry_id, reason=reason, score=s)
                       for e, reason, s in budget_dropped)

        # I4：出口兜底复检（覆盖旧快照载入的条目）
        clean: list[RetrievedMemory] = []
        for item in selected:
            hits = self._scan_entry(item.entry, point="I4")
            if hits:
                self._leak_event(point="I4", hits=hits,
                                 field_name=str(hits[0].get("field", "lesson")),
                                 episode_id=item.entry.source_episode_id,
                                 entry_id=item.entry.entry_id,
                                 reflection_id=item.entry.source_reflection_id)
                dropped.append(DroppedEntry(entry_id=item.entry_id, reason="inv5_leak",
                                            score=item.score))
                continue
            clean.append(item)
        clean = [replace(item, rank=index) for index, item in enumerate(clean)]
        used = sum(item.tokens for item in clean)

        conflict_groups = tuple(sorted({item.entry.conflict_group for item in clean
                                        if item.entry.conflict_group}))
        elapsed_ms = round6((time.perf_counter() - started) * 1000.0)
        result = RetrieveResult(
            entries=tuple(clean),
            scores=tuple(item.score for item in clean),
            tokens=int(used),
            dropped=tuple(dropped),
            conflict_groups=conflict_groups,
            elapsed_ms=elapsed_ms,
            text=render_entries([item.entry for item in clean]),
            obs_digest=_obs_digest(obs),
            n_candidates=len(live),
        )
        self._counters["retrieve_calls"] = int(self._counters.get("retrieve_calls", 0)) + 1
        self._emit("memory.retrieve", obs_digest=result.obs_digest,
                   n_candidates=result.n_candidates,
                   returned_ids=[item.entry_id for item in clean],
                   dropped_reasons=result.dropped_reasons(), tokens=result.tokens,
                   elapsed_ms=result.elapsed_ms)
        return result

    def _query_embedding(self, text: str, weights: Mapping[str, float]
                         ) -> tuple[float, ...] | None:
        """向量检索的查询向量（可选）：``w_vec>0`` 且注入了 MOD-03 ``embed()`` 时计算。"""
        if float(weights.get("vector", 0.0)) <= 0.0 or self._on_embed is None:
            return None
        try:
            return tuple(float(x) for x in self._on_embed(text))
        except Exception:  # noqa: BLE001 - 降级为纯 BM25
            return None

    def _hard_filter(self, entry: MemoryEntry, *, tick: int, exclude_stale: bool,
                     harness_now: int | None = None) -> str | None:
        if entry.status != "active":
            return "not_active"
        if entry.expires_tick <= int(tick):
            return "expired"
        baseline = self.harness_version if harness_now is None else int(harness_now)
        if exclude_stale and entry.harness_version > baseline:
            return "stale_harness"
        return None

    # =================================================================
    # 只读冻结视图（评估局，Spec §4.7）
    # =================================================================
    def frozen(self, memory_version: int) -> "MemoryView":
        """评估局只读视图：从 ``memory_v{N}.jsonl`` 载入，禁止写库。"""
        path = snapshot_path(self.memory_dir, int(memory_version))
        if not path.is_file():
            raise SnapshotMissingError(
                f"冻结视图需要快照 memory_v{memory_version}.jsonl，但 {path} 不存在",
                hint="MOD-06 在演化前调用 snapshot()；评估局只能读已冻结版本")
        data = read_snapshot(path, require_version=int(memory_version), verify=True)
        return MemoryView(self, data)

    # =================================================================
    # 快照 / 回滚（FR-05-9，§8.3）
    # =================================================================
    def snapshot(self, reason: str | int = "manual", *,
                 memory_version: int | None = None) -> Path:
        """产出 ``memory_v{N}.jsonl``；I3 复扫全库，命中即拒出快照。

        ``reason`` 允许传 int（MOD-06 PRD 的历史写法 ``snapshot(version)``）：
        此时按显式版本号处理，``reason`` 记为 ``explicit``。
        """
        version = memory_version
        if isinstance(reason, int) and not isinstance(reason, bool):
            version = int(reason) if version is None else version
            reason_text = "explicit"
        else:
            reason_text = str(reason)

        hits = self._seed_guard_all()
        if hits:
            raise MemoryIsolationError(
                f"I3：active 库命中 heldout/test 泄漏 {len(hits)} 处，拒绝出快照",
                hint="命中的条目已标 quarantined；事件见 audit/leak_events.jsonl")

        if not self._try_lock():
            raise MemoryLockedError("memory.lock 被占用 > 5 s，快照中止")
        try:
            # 先压实 active：保证「active 文件 == 快照条目段」（rollback 逐字节恢复）
            self._compact_locked()
            current = self.memory_version
            target = int(version) if version is not None else current + 1
            live = [e for e in self._file_order_entries() if e.status != "quarantined"]
            lines = [self._line_of(e) for e in live]
            digest = entries_sha256(lines)
            header = build_header(
                memory_version=target, parent_memory_version=current,
                harness_version=self.harness_version, generation=self.generation,
                run_id=self.run_id, created_at=self._now_iso(),
                config_hash=self._config_hash(), entry_count=len(live),
                active_count=sum(1 for e in live if e.status == "active"),
                reason=reason_text, content_sha256=digest)
            path = snapshot_path(self.memory_dir, target)
            write_snapshot(path, header, live)
            self._index_state["parent_memory_version"] = (current if target != current
                                                          else self.parent_memory_version)
            self._index_state["memory_version"] = target
            self._record_lineage(target)
            self._counters["snapshot_count"] = int(
                self._counters.get("snapshot_count", 0)) + 1
            self._save_index()
            self._emit("memory.snapshot", from_version=current, to_version=target,
                       entry_count=len(live), content_sha256=digest,
                       reason=reason_text)
            return path
        finally:
            self._unlock()

    def rollback(self, target_version: int, *, dry_run: bool = False) -> RollbackReport:
        """校验 sha256 → 原子替换 active 库 → 新版本号（不回退编号，Spec §8.4）。"""
        target_version = int(target_version)
        path = snapshot_path(self.memory_dir, target_version)
        if not path.is_file():
            raise SnapshotMissingError(
                f"回滚目标快照不存在：{path}",
                hint="见 memory snapshot 产生的 memory_v{N}.jsonl；CLI 退出码 2")
        data = read_snapshot(path, require_version=target_version, verify=True)
        current = self.memory_version
        new_version = current + 1
        current_ids = {e.entry_id for e in self._entries.values()}
        target_ids = {e.entry_id for e in data.entries}
        lost = tuple(sorted(current_ids - target_ids))
        report = RollbackReport(
            from_version=current, to_version=new_version, target_version=target_version,
            dry_run=bool(dry_run), entries=len(data.entries),
            active=sum(1 for e in data.entries if e.status == "active"),
            entries_delta=len(target_ids) - len(current_ids),
            active_delta=(sum(1 for e in data.entries if e.status == "active")
                          - sum(1 for e in self._entries.values() if e.status == "active")),
            lost_entry_ids=lost, content_sha256=data.computed_sha256,
            harness_version=self.harness_version, snapshot_path=str(path))
        if dry_run:
            return report

        if not self._try_lock():
            raise MemoryLockedError("memory.lock 被占用 > 5 s，回滚中止")
        try:
            body = "".join(line + "\n" for line in data.entry_lines)
            write_bytes_atomic(self.active_path, body.encode("utf-8"), fsync=True)
            self._entries = {e.entry_id: e for e in data.entries}
            self._rebuild_index()
            self._dirty_rows = 0
            self._appends = 0
            self._index_state["memory_version"] = new_version
            self._index_state["parent_memory_version"] = target_version
            self._record_lineage(new_version)
            self._counters["rollback_count"] = int(
                self._counters.get("rollback_count", 0)) + 1
            self._save_index()
            # 历史不可重写：为 rollback 结果再落一个新版本快照（parent=target）
            entries = [e for e in data.entries if e.status != "quarantined"]
            lines = [entry_line(e) for e in entries]
            digest = entries_sha256(lines)
            header = build_header(
                memory_version=new_version, parent_memory_version=target_version,
                harness_version=self.harness_version, generation=self.generation,
                run_id=self.run_id, created_at=self._now_iso(),
                config_hash=self._config_hash(), entry_count=len(entries),
                active_count=sum(1 for e in entries if e.status == "active"),
                reason=f"rollback_to_v{target_version}", content_sha256=digest)
            write_snapshot(snapshot_path(self.memory_dir, new_version), header, entries)
            self._emit("memory.rollback", from_version=current, to_version=new_version,
                       entry_count=len(entries), content_sha256=digest,
                       restored_from=target_version)
            return replace(report, content_sha256=digest)
        finally:
            self._unlock()

    def _seed_guard_all(self) -> list[dict[str, Any]]:
        """I3 兜底：复扫全库；命中条目标 ``quarantined`` 并落 leak 事件。"""
        hits: list[dict[str, Any]] = []
        for entry in list(self._entries.values()):
            if entry.status == "quarantined":
                continue
            found = self._scan_entry(entry, point="I3")
            if not found:
                continue
            hits.extend(found)
            self._quarantine_entry(entry, reason="scan", hits=found, point="I3")
            self._leak_event(point="I3", hits=found,
                             field_name=str(found[0].get("field", "lesson")),
                             episode_id=entry.source_episode_id,
                             entry_id=entry.entry_id,
                             reflection_id=entry.source_reflection_id)
        return hits

    # =================================================================
    # stats / verify（FR-05-11）
    # =================================================================
    def stats(self) -> StoreStats:
        active = sum(1 for e in self._entries.values() if e.status == "active")
        quarantined = sum(1 for e in self._entries.values() if e.status == "quarantined")
        candidates = int(self._counters.get("candidate_count", 0))
        merged = int(self._counters.get("merged", 0))
        conflicts = int(self._counters.get("conflicted", 0))
        ref_total = int(self._counters.get("ref_total", 0))
        ref_adopted = int(self._counters.get("ref_adopted", 0))
        return StoreStats(
            memory_version=self.memory_version,
            parent_memory_version=self.parent_memory_version,
            harness_version=self.harness_version,
            generation=self.generation,
            entry_count=len(self._entries),
            active_count=active,
            evicted_count=self._evicted_count,
            quarantined_count=quarantined,
            merged_count=merged,
            conflicted_count=conflicts,
            candidate_count=candidates,
            dedup_rate=round6(merged / candidates) if candidates else 0.0,
            conflict_rate=round6(conflicts / candidates) if candidates else 0.0,
            retrieve_hit_rate=round6(ref_adopted / ref_total) if ref_total else 0.0,
            seed_leak_blocks=int(self._counters.get("seed_leak_blocks", 0)),
            write_failures=self._write_failures,
            snapshot_count=int(self._counters.get("snapshot_count", 0)),
        )

    def verify(self, tick: int | None = None, *, full: bool = False) -> VerifyReport:
        """行完整性 / entry_id 唯一 / （full）``content_sha256`` 与 ``situation_key`` 可复算。"""
        tick_value = int(self._last_tick if tick is None else tick)
        checked = ok = bad = duplicates = sha_bad = key_bad = 0
        codes: list[str] = []
        seen: set[str] = set()
        if self.active_path.is_file():
            try:
                text = self.active_path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover
                text = ""
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                checked += 1
                try:
                    entry = MemoryEntry.from_json(line)
                except InfraError:
                    bad += 1
                    codes.append(str(E.E_IO_MEMORY_CORRUPT))
                    self._quarantine_raw(reason="corrupt", raw=line,
                                         path=str(self.active_path),
                                         code=E.E_IO_MEMORY_CORRUPT)
                    continue
                if entry.entry_id in seen:
                    duplicates += 1
                    codes.append(str(E.E_IO_MEMORY_CORRUPT))
                    self._quarantine_raw(reason="corrupt", raw=line,
                                         path=str(self.active_path),
                                         code=E.E_IO_MEMORY_CORRUPT,
                                         message=f"entry_id 重复：{entry.entry_id}")
                    continue
                seen.add(entry.entry_id)
                if full:
                    if entry.compute_content_sha256() != entry.content_sha256:
                        sha_bad += 1
                        codes.append(str(E.E_IO_MEMORY_CORRUPT))
                        self._quarantine_entry(entry, reason="corrupt")
                        continue
                    if entry.situation.key() != entry.situation_key:
                        key_bad += 1
                        codes.append(str(E.E_IO_MEMORY_CORRUPT))
                        self._quarantine_entry(entry, reason="corrupt")
                        continue
                ok += 1

        evicted_ttl = self.sweep_ttl(tick_value)
        leak_hits = 0
        if full:
            hits = self._seed_guard_all()
            leak_hits = len(hits)
            if hits:
                codes.append(str(E.E_EVAL_INV5_LEAK))
                self._save_index()
                raise MemoryIsolationError(
                    f"I3：verify(full=True) 命中 {len(hits)} 处泄漏，拒绝出快照",
                    hint="命中条目已标 quarantined 并写入 audit/leak_events.jsonl")
        if bad or duplicates or sha_bad or key_bad or evicted_ttl:
            self._compact()
        self._save_index()
        return VerifyReport(checked=checked, ok=ok, bad_lines=bad, quarantined=bad,
                            duplicates=duplicates, sha256_mismatch=sha_bad,
                            situation_key_mismatch=key_bad, evicted_ttl=evicted_ttl,
                            leak_hits=leak_hits, codes=tuple(dict.fromkeys(codes)))

    # =================================================================
    # 快照列举与元数据
    # =================================================================
    def snapshots(self) -> list[tuple[int, Path]]:
        return find_snapshots(self.memory_dir)

    def index(self) -> dict[str, Any]:
        return dict(self._index_state)

    def close(self) -> None:
        """收尾：把内存态与 ``active`` 文件对齐（需要时压实）并落 index.json（幂等）。

        压实条件：本段有未落盘的改写或淘汰（``_dirty_rows``，它们只能靠整档重写落盘）。
        ``snapshot()`` 也走压实，所以两个显式收口都会把攒批的改动落盘；只是追加且未到阈值
        时无需重写——那些行本来就已经追加落盘了。
        """
        if self._dirty_rows:
            self._compact()
        self._save_index()


# =====================================================================
# MemoryView：评估局只读视图
# =====================================================================

@dataclass
class MemoryView:
    """``memory_v{N}.jsonl`` 的只读视图（Spec §4.7：评估局禁止写记忆库）。"""

    store: MemoryStore
    data: Any
    _entries: dict[str, MemoryEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._entries = {e.entry_id: e for e in self.data.entries}

    @property
    def memory_version(self) -> int:
        return int(self.data.header.get("memory_version", 0))

    @property
    def harness_version(self) -> int:
        return int(self.data.header.get("harness_version", 0))

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def entries(self) -> tuple[MemoryEntry, ...]:
        return tuple(self._entries[k] for k in sorted(self._entries))

    def get(self, entry_id: str) -> MemoryEntry | None:
        return self._entries.get(str(entry_id))

    def stats(self) -> StoreStats:
        base = self.store.stats()
        return replace(base, memory_version=self.memory_version,
                       entry_count=len(self._entries),
                       active_count=sum(1 for e in self._entries.values()
                                        if e.status == "active"))

    def retrieve(self, obs: Mapping[str, Any], *, top_k: int | None = None,
                 token_budget: int | None = None, tick: int = 0,
                 tags_hint: Sequence[str] = (),
                 exclude_stale_harness: bool = True) -> RetrieveResult:
        try:
            return self.store._retrieve_over(  # noqa: SLF001 - 同一模块内的共享实现
                self._entries, obs=obs, top_k=top_k, token_budget=token_budget,
                tick=tick, tags_hint=tags_hint,
                exclude_stale_harness=exclude_stale_harness,
                current_harness=int(self.data.header.get("harness_version",
                                                         self.store.harness_version)))
        except Exception as exc:  # noqa: BLE001
            self.store._emit("memory.retrieve_failed", code=E.E_IO_MEMORY_CORRUPT,  # noqa: SLF001
                             message=f"{type(exc).__name__}: {exc}")
            return RetrieveResult()

    # ---- 写操作一律拒绝（评估局只读）----------------------------------------------
    def _readonly(self, *_: Any, **__: Any) -> Any:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         "frozen 视图是只读的：评估局禁止写记忆库（Spec §8.4 B-19）",
                         hint="train 局使用 MemoryStore；评估局只调用 retrieve()")

    add_reflection = _readonly
    add_memory = _readonly
    record_usage = _readonly
    snapshot = _readonly
    rollback = _readonly

    def verify(self, *_: Any, **__: Any) -> VerifyReport:  # 只读自检
        return self.store.verify()


# =====================================================================
# 模块级纯函数
# =====================================================================

def _llm_candidate(reflection: ReflectionEntry, item: Any, cfg: Any
                   ) -> MemoryCandidate | None:
    """把 LLM 抽取产物规整为 ``MemoryCandidate``（字段不合法 → ``None``，由调用方降级）。

    MOD-05 **不做语义判断**（§2.2）：这里只做受控词表与长度校验，``situation`` 缺省时
    回落到规则抽取器的默认情境，绝不猜测内容含义。
    """
    if not isinstance(item, Mapping):
        return None
    lesson = " ".join(str(item.get("lesson") or "").split())
    if not lesson:
        return None
    lesson = lesson[:240]
    tags = tuple(str(t) for t in (item.get("tags") or ()) if str(t) in MEMORY_TAGS)[:8]
    polarity = str(item.get("polarity") or "")
    polarity = polarity if polarity in POLARITIES else ("do" if reflection.outcome == "win"
                                                        else "avoid")
    hint = item.get("action_hint")
    hint = dict(hint) if isinstance(hint, Mapping) else None
    raw_situation = item.get("situation")
    situation = (SituationKey.from_dict(raw_situation) if isinstance(raw_situation, Mapping)
                 else _reflection_situation(reflection))
    try:
        confidence = min(1.0, max(0.0, float(item.get("confidence", 0.6))))
    except (TypeError, ValueError):
        confidence = 0.6
    return MemoryCandidate(
        situation=situation, tags=tags, lesson=lesson, polarity=polarity,
        confidence=round6(confidence), action_hint=hint,
        source_episode_id=str(reflection.episode_id),
        source_reflection_id=str(reflection.reflection_id),
        created_at=str(reflection.created_at), created_tick=int(reflection.tick),
        run_id=str(reflection.run_id), generation=int(reflection.generation),
        harness_version=int(reflection.harness_version))


def _reflection_situation(reflection: ReflectionEntry) -> SituationKey:
    """反思自身的保守情境（只有 stage/hearts 是可信字段，其余取缺省）。"""
    # 复用 retriever 的单一档位口径（不在别处再写一份分档规则）
    return SituationKey(stage_bucket=stage_bucket(int(reflection.stages_cleared) + 1),
                        hearts_left=int(reflection.hearts_left))


def _stats_from_tokens(tokens_map: Mapping[str, Sequence[str]]
                       ) -> tuple[dict[str, int], int, float]:
    """``(df, n_docs, avgdl)``：BM25 的文档频率、文档数与平均长度（确定性）。"""
    df: dict[str, int] = {}
    total = 0
    for tokens in tokens_map.values():
        total += len(tokens)
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    n_docs = len(tokens_map)
    return df, n_docs, (total / n_docs if n_docs else 0.0)


def _hint_kind(item: MemoryCandidate | MemoryEntry) -> str:
    hint = getattr(item, "action_hint", None) or {}
    if isinstance(hint, Mapping) and hint.get("kind"):
        return str(hint["kind"])
    return "-"


def _same_hint_kind(a: MemoryEntry, b: MemoryCandidate) -> bool:
    """hint kind 相同，或两者均无 hint（PRD FR-05-3）。"""
    kind_a = _hint_kind(a)
    kind_b = _hint_kind(b)
    return kind_a == kind_b


def _suppress_conflicts(scored: Sequence[tuple[MemoryEntry, float]]
                        ) -> tuple[list[tuple[MemoryEntry, float]],
                                   list[tuple[MemoryEntry, float]]]:
    """冲突组内只保留 ``S`` 最高者（并列取 ``entry_id`` 小者）。"""
    groups: dict[str, list[tuple[MemoryEntry, float]]] = {}
    kept: list[tuple[MemoryEntry, float]] = []
    for entry, score in scored:
        if entry.conflict_group:
            groups.setdefault(entry.conflict_group, []).append((entry, score))
        else:
            kept.append((entry, score))
    suppressed: list[tuple[MemoryEntry, float]] = []
    for entries in groups.values():
        ordered = sorted(entries, key=lambda item: (-item[1], item[0].entry_id))
        kept.append(ordered[0])
        suppressed.extend(ordered[1:])
    kept.sort(key=lambda item: (-item[1], item[0].entry_id))
    return kept, suppressed


def _obs_digest(obs: Mapping[str, Any]) -> str:
    """单向摘要（不含 seed；事件字段约束见 §13）。"""
    payload = {k: v for k, v in (obs or {}).items() if k != "episodeId"}
    return "sha256:" + content_sha256_of(payload)[:16]


def _wilson_interval(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 置信区间（``auto_supersede`` 证据门槛用，§9）。"""
    if total <= 0:
        return 0.0, 1.0
    phat = wins / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (z * ((phat * (1 - phat) / total + z * z / (4 * total * total)) ** 0.5)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _evidence_ok(new: MemoryEntry, olds: Sequence[MemoryEntry], cfg: Any) -> bool:
    """证据充分才允许自动替代（默认 ``auto_supersede=false``，§9/§14）。"""
    conflict_cfg = getattr(cfg, "conflict", None)
    min_refs = int(getattr(conflict_cfg, "min_refs", 5))
    delta = float(getattr(conflict_cfg, "winrate_delta", 0.15))
    if new.n_ref < min_refs:
        return False
    low_new, high_new = _wilson_interval(new.n_win_adopted, new.n_adopted)
    for old in olds:
        low_old, high_old = _wilson_interval(old.n_win_adopted, old.n_adopted)
        if high_new < low_old or high_old < low_new:
            if abs(_winrate(new) - _winrate(old)) >= delta:
                return True
    return False


def _winrate(entry: MemoryEntry) -> float:
    return (entry.n_win_adopted / entry.n_adopted) if entry.n_adopted else 0.0
