"""FR-05-9 / GWT-05-08：快照 header、``content_sha256`` 跨机复核、回滚与血缘。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E, error_code_of
from dice_ratchet.memory import MemoryLockedError, MemoryStore, SnapshotMissingError
from dice_ratchet.memory.snapshot import (entries_sha256, entry_line, find_snapshots,
                                       read_snapshot, snapshot_path, verify_snapshot)

LESSON = "净化值低于 25% 时先把光盾骰放进防御槽，再考虑输出。"
OTHER = "地图路线优先走左侧商店，避免精英战。"


def _lines(path) -> list[str]:
    text = Path(path).read_text("utf-8") if Path(path).is_file() else ""
    return [line for line in text.splitlines() if line.strip()]


def _events(store, name: str) -> list[dict]:
    return [json.loads(line) for line in _lines(store.metrics_path)
            if json.loads(line)["event"] == name]


def _seed_three(store, make_candidate, make_prov) -> tuple[str, ...]:
    prov = make_prov()
    for index, lesson in enumerate((LESSON, OTHER, "不要让魔化骰进入攻击槽。")):
        store.add_memory(make_candidate(lesson=lesson, reflection_id=f"refl-{index}"),
                         provenance=prov)
    return tuple(e.entry_id for e in store.entries())


def test_snapshot_header_and_sha256(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    path = store.snapshot(reason="gen-000")
    assert path.name == "memory_v1.jsonl"
    data = read_snapshot(path, require_version=1, verify=True)
    header = data.header
    assert header["_type"] == "memory_snapshot_header"
    assert header["memory_version"] == 1
    assert header["parent_memory_version"] == 0
    assert header["store_schema_version"] == "1.0"
    assert header["entry_count"] == 3 and header["active_count"] == 3
    assert header["reason"] == "gen-000"
    assert header["config_hash"]
    assert header["harness_version"] == store.harness_version
    assert data.sha256_ok
    assert len(data.entries) == 3
    assert store.memory_version == 1


def test_snapshot_sha256_recomputable_from_file_bytes(store_factory, make_candidate,
                                                      make_prov) -> None:
    """NFR-05-3：``content_sha256`` 可跨机复核（条目行按序拼接的 sha256）。"""
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    path = store.snapshot(reason="gen-000")
    raw = Path(path).read_text("utf-8").split("\n")
    header = json.loads(raw[0])
    body_lines = [line for line in raw[1:] if line.strip()]
    assert entries_sha256(body_lines) == header["content_sha256"]
    assert verify_snapshot(path).computed_sha256 == header["content_sha256"]


def test_snapshot_versions_increase_monotonically(store_factory, make_candidate,
                                                  make_prov) -> None:
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    assert store.snapshot(reason="gen-000").name == "memory_v1.jsonl"
    assert store.snapshot(reason="gen-001").name == "memory_v2.jsonl"
    assert store.snapshot(reason="explicit", memory_version=7).name == "memory_v7.jsonl"
    assert [v for v, _ in find_snapshots(store.memory_dir)] == [1, 2, 7]


def test_rollback_restores_identical_bytes(store_factory, make_candidate,
                                           make_prov) -> None:
    """GWT-05-08：``memory_active.jsonl`` 与 v3 的条目段逐字节相同。"""
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    store.snapshot(reason="gen-000")          # v1
    store.snapshot(reason="gen-001")          # v2
    store.snapshot(reason="gen-002")          # v3
    baseline = Path(store.active_path).read_bytes()
    v3 = read_snapshot(snapshot_path(store.memory_dir, 3))

    prov = make_prov(seed=1043)
    for index in range(5):
        store.add_memory(make_candidate(lesson=f"新增教训 {index}，路线和商店都要注意。",
                                        reflection_id=f"refl-new-{index}",
                                        action_hint=None, tags=("map_route",)),
                         provenance=prov)
    assert store.stats().entry_count == 8

    report = store.rollback(3)
    assert report.dry_run is False
    assert report.from_version == 3
    assert report.to_version == 4          # 不回退编号（Spec §8.4）
    assert report.target_version == 3
    assert Path(store.active_path).read_bytes() == baseline
    assert entries_sha256(_lines(store.active_path)) == v3.computed_sha256
    assert store.memory_version == 4
    assert store.parent_memory_version == 3
    events = _events(store, "memory.rollback")
    assert len(events) == 1 and events[0]["from_version"] == 3 and events[0]["to_version"] == 4
    # 历史不可重写：rollback 结果自身也落一个新快照（parent = 目标版本）
    new_snapshot = read_snapshot(snapshot_path(store.memory_dir, 4))
    assert new_snapshot.parent_memory_version == 3
    assert new_snapshot.header["reason"] == "rollback_to_v3"


def test_rollback_dry_run_does_not_write(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    store.snapshot(reason="gen-000")
    before = Path(store.active_path).read_bytes()
    report = store.rollback(1, dry_run=True)
    assert report.dry_run is True
    assert report.entries_delta == 0 and report.active_delta == 0
    assert report.lost_entry_ids == ()
    assert Path(store.active_path).read_bytes() == before
    assert store.memory_version == 1


def test_rollback_reports_lost_entries(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    first = _seed_three(store, make_candidate, make_prov)
    store.snapshot(reason="gen-000")
    store.add_memory(make_candidate(lesson="额外的教训：先处理护卫。", action_hint=None,
                                    reflection_id="refl-extra"), provenance=make_prov())
    report = store.rollback(1, dry_run=True)
    assert report.entries_delta == -1
    assert len(report.lost_entry_ids) == 1
    assert report.lost_entry_ids[0] not in first


def test_rollback_missing_target_exit_code_2(store_factory) -> None:
    store = store_factory()
    with pytest.raises(SnapshotMissingError) as exc:
        store.rollback(42)
    assert exc.value.code == E.E_IO_SNAPSHOT_MISSING
    assert error_code_of(exc.value.code) == 2


def test_rollback_detects_sha256_mismatch(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    path = store.snapshot(reason="gen-000")
    lines = Path(path).read_text("utf-8").splitlines()
    row = json.loads(lines[1])
    row["lesson"] = "被篡改的教训。"
    lines[1] = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        store.rollback(1)
    assert str(E.E_IO_MEMORY_CORRUPT) in str(exc.value)


def test_snapshot_rejects_unknown_version_file(store_factory, make_candidate,
                                               make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    path = store.snapshot(reason="gen-000")
    with pytest.raises(Exception):
        read_snapshot(path, require_version=99, verify=True)


def test_frozen_view_reads_snapshot_and_is_readonly(store_factory, make_candidate,
                                                    make_prov, make_reflection,
                                                    obs_player_turn) -> None:
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    store.snapshot(reason="gen-000")
    view = store.frozen(1)
    assert view.memory_version == 1
    assert view.entry_count == 3
    assert len(view.retrieve(obs_player_turn, tick=20).entries) >= 1
    assert view.stats().memory_version == 1
    for call in ("add_reflection", "add_memory", "record_usage", "snapshot", "rollback"):
        with pytest.raises(Exception):
            getattr(view, call)(None)


def test_frozen_view_requires_existing_snapshot(store_factory) -> None:
    store = store_factory()
    with pytest.raises(SnapshotMissingError):
        store.frozen(3)


def test_snapshot_entry_lines_equal_active_lines(store_factory, make_candidate,
                                                 make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    store.snapshot(reason="gen-000")
    path = snapshot_path(store.memory_dir, 1)
    body = [line for line in Path(path).read_text("utf-8").splitlines()[1:] if line.strip()]
    assert body == _lines(store.active_path)


def test_compaction_keeps_snapshot_shape(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(lesson=LESSON), provenance=make_prov())
    assert store._compact() is True
    assert _lines(store.active_path) == [entry_line(store.entries()[0])]


def test_locked_store_blocks_snapshot(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    store.lock_path.write_text("99999\n", encoding="utf-8")
    with pytest.raises(MemoryLockedError):
        store.snapshot(reason="gen-000")
    store.lock_path.unlink()


def test_index_records_lineage(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    _seed_three(store, make_candidate, make_prov)
    store.snapshot(reason="gen-000")
    store.snapshot(reason="gen-001")
    lineage = store.index()["lineage"]
    assert [row["memory_version"] for row in lineage] == [1, 2]
    assert all({"generation", "harness_version", "memory_version"} <= set(row)
               for row in lineage)


def test_stale_lock_is_taken_over(store_factory, make_candidate, make_prov) -> None:
    """崩溃残留的 ``memory.lock``（> 60 s）必须被接管，否则快照被永久锁死。"""
    import os
    import time

    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    store.lock_path.write_text("99999\n", encoding="utf-8")
    old = time.time() - 120
    os.utime(store.lock_path, (old, old))
    path = store.snapshot(reason="gen-000")
    assert path.is_file()
    assert not store.lock_path.exists()
