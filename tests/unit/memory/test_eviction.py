"""FR-05-7 / GWT-05-06：容量淘汰（免疫条件）与 TTL（逻辑时钟）。"""

from __future__ import annotations

import json
from dataclasses import replace

LESSONS = (
    "净化值低时应该先放光盾，再考虑输出。",
    "地图路线优先走左侧商店，避免精英战。",
    "不要让魔化骰进入攻击槽，先净化再输出。",
    "锻造把低面骰重铸成防御面更划算。",
)


def _lines(path) -> list[str]:
    return [line for line in open(path, encoding="utf-8").read().splitlines() if line.strip()]


def _statuses(store) -> dict[str, str]:
    return {entry.entry_id: entry.status for entry in store.entries()}


def _evicted_rows(store) -> list[dict]:
    return [json.loads(line) for line in _lines(store.evicted_path)]


def _events(store, name: str) -> list[dict]:
    return [json.loads(line) for line in _lines(store.metrics_path)
            if json.loads(line)["event"] == name]


def test_capacity_eviction_respects_immunity(store_factory, make_candidate,
                                             make_prov, cfg) -> None:
    """GWT-05-06：max_entries=3、min_age_ticks=30；受保护条目仍在 active。"""
    small = replace(cfg, max_entries=3, min_age_ticks=30)
    store = store_factory(small)
    prov = make_prov()

    old = make_candidate(lesson=LESSONS[0], created_tick=10, reflection_id="refl-old")
    young = make_candidate(lesson=LESSONS[1], created_tick=99, reflection_id="refl-young")
    referenced = make_candidate(lesson=LESSONS[2], created_tick=10,
                                reflection_id="refl-ref")
    for candidate in (old, young, referenced):
        store.add_memory(candidate, provenance=prov)
    # 让 referenced 拿到 n_ref ≥ 3（免疫）
    ref_id = next(e.entry_id for e in store.entries() if e.source_reflection_id == "refl-ref")
    old_id = next(e.entry_id for e in store.entries() if e.source_reflection_id == "refl-old")
    for tick in (11, 12, 13):
        store.record_usage("ep", [ref_id], "lose", adopted_ids=[], tick=tick)

    newest = make_candidate(lesson=LESSONS[3], created_tick=100, reflection_id="refl-new")
    result = store.add_memory(newest, provenance=prov)
    assert result.evicted == 1

    statuses = _statuses(store)
    assert old_id not in statuses  # 已移出 active 库
    assert statuses[ref_id] == "active"
    assert sum(1 for s in statuses.values() if s == "active") == 3
    assert _evicted_rows(store)[0]["entry_id"] == old_id
    assert _events(store, "memory.evict")[0]["evict_reason"] == "capacity"


def test_capacity_no_op_when_within_limit(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    for index, lesson in enumerate(LESSONS):
        store.add_memory(make_candidate(lesson=lesson, reflection_id=f"refl-{index}"),
                         provenance=prov)
    assert store.stats().evicted_count == 0
    assert store.stats().active_count == 4


def test_ttl_sweep_evicts_and_archives(store_factory, make_candidate, make_prov, cfg) -> None:
    """GWT-05-06：``ttl_ticks=1``、``tick = created_tick + 2`` → 过期即淘汰并归档。"""
    short = replace(cfg, ttl_ticks=1)
    store = store_factory(short)
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSONS[0], created_tick=10,
                                    reflection_id="refl-ttl"), provenance=prov)
    entry_id = store.entries()[0].entry_id
    assert store.entries()[0].expires_tick == 11

    report = store.verify(12)
    assert report.evicted_ttl == 1
    assert entry_id not in _statuses(store)
    rows = _evicted_rows(store)
    assert rows and rows[0]["entry_id"] == entry_id and rows[0]["status"] == "evicted"
    assert _events(store, "memory.evict")[0]["evict_reason"] == "ttl"


def test_ttl_sweep_is_idempotent(store_factory, make_candidate, make_prov, cfg) -> None:
    short = replace(cfg, ttl_ticks=1)
    store = store_factory(short)
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSONS[0], created_tick=10,
                                    reflection_id="refl-ttl"), provenance=prov)
    assert store.sweep_ttl(50) == 1
    assert store.sweep_ttl(50) == 0
    assert len(_evicted_rows(store)) == 1


def test_expired_entry_is_not_retrieved(store_factory, make_candidate, make_prov, cfg,
                                        obs_player_turn) -> None:
    short = replace(cfg, ttl_ticks=5)
    store = store_factory(short)
    store.add_memory(make_candidate(lesson=LESSONS[0], created_tick=10),
                     provenance=make_prov())
    assert len(store.retrieve(obs_player_turn, tick=12).entries) == 1
    assert len(store.retrieve(obs_player_turn, tick=100).entries) == 0
    reasons = [d.reason for d in store.retrieve(obs_player_turn, tick=100).dropped]
    assert "expired" in reasons


def test_capacity_uses_utility_then_last_ref_then_id(store_factory, make_candidate,
                                                     make_prov, cfg) -> None:
    small = replace(cfg, max_entries=2, min_age_ticks=0)
    store = store_factory(small)
    prov = make_prov()
    for index, lesson in enumerate(LESSONS[:3]):
        store.add_memory(make_candidate(lesson=lesson, created_tick=10,
                                        reflection_id=f"refl-{index}"), provenance=prov)
    ids = sorted(e.entry_id for e in store.entries())
    # 给 ids[0] 更强的效用证据 → 它必须留下；并列时按 (last_ref_tick, entry_id) 升序淘汰
    store.record_usage("ep", [ids[0]], "win", adopted_ids=[ids[0]], tick=11)
    store.add_memory(make_candidate(lesson=LESSONS[3], created_tick=11,
                                    reflection_id="refl-3"), provenance=prov)
    remaining = {e.entry_id for e in store.entries()}
    assert ids[0] in remaining
    assert len(remaining) == 2
