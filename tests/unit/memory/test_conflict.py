"""FR-05-3 / GWT-05-04：冲突检测（同情境、相反极性、同 hint kind）与保守共存/压制。"""

from __future__ import annotations

import json
from dataclasses import replace

from dice_ratchet.memory import SituationKey, similarity
from dice_ratchet.memory.store import _evidence_ok, _wilson_interval

DO_LESSON = "净化值低时应该先放光盾，再考虑输出。"
AVOID_LESSON = "不要让魔化骰进入攻击槽，否则会被反打。"


def _hint() -> dict:
    return {"kind": "play_face", "slotKind": "defense", "school": "neutral"}


def _lines(path) -> list[str]:
    return [line for line in open(path, encoding="utf-8").read().splitlines() if line.strip()]


def test_conflict_keeps_both_active_with_shared_group(store_factory, make_candidate,
                                                      make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    first = make_candidate(lesson=DO_LESSON, polarity="do", action_hint=_hint())
    second = make_candidate(lesson=AVOID_LESSON, polarity="avoid", action_hint=_hint(),
                            reflection_id="refl-2")
    assert first.situation_key == second.situation_key
    assert similarity(first, second) < 0.85
    assert store.add_memory(first, provenance=prov).added == 1
    result = store.add_memory(second, provenance=prov)
    assert result.conflicted == 1 and result.added == 0

    entries = store.entries()
    assert len(entries) == 2
    assert all(entry.status == "active" for entry in entries)
    groups = {entry.conflict_group for entry in entries}
    assert len(groups) == 1 and None not in groups
    assert len(_lines(store.active_path)) == 2


def test_conflict_group_is_deterministic(store_factory, make_candidate, make_prov) -> None:
    def build() -> str:
        store = store_factory(name=f"run-{id(object())}")
        prov = make_prov()
        store.add_memory(make_candidate(lesson=DO_LESSON, action_hint=_hint()),
                         provenance=prov)
        store.add_memory(make_candidate(lesson=AVOID_LESSON, polarity="avoid",
                                        action_hint=_hint(), reflection_id="refl-2"),
                         provenance=prov)
        return store.entries()[0].conflict_group
    assert build() == build()


def test_retrieve_suppresses_lower_scored_conflict(store_factory, make_candidate,
                                                   make_prov, obs_player_turn) -> None:
    """GWT-05-04：检索只返回组内高分者 + ``dropped.reason="conflict_suppressed"``。"""
    store = store_factory()
    prov = make_prov()
    strong = make_candidate(lesson=DO_LESSON, action_hint=_hint(),
                            tags=("combat", "low_virtue", "defense"))
    weak = make_candidate(lesson=AVOID_LESSON, polarity="avoid", action_hint=_hint(),
                          tags=("corruption",), reflection_id="refl-2")
    store.add_memory(strong, provenance=prov)
    store.add_memory(weak, provenance=prov)
    result = store.retrieve(obs_player_turn, tick=12)
    assert len(result.entries) == 1
    assert result.entries[0].conflict_group is not None
    assert result.conflict_groups == (result.entries[0].conflict_group,)
    reasons = [d.reason for d in result.dropped]
    assert "conflict_suppressed" in reasons
    suppressed = [d for d in result.dropped if d.reason == "conflict_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0].entry_id != result.entries[0].entry_id


def test_different_polarity_same_hint_kind_is_required(store_factory, make_candidate,
                                                       make_prov) -> None:
    """hint kind 不同 → 不算冲突（FR-05-3 的与条件）。"""
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=DO_LESSON, polarity="do", action_hint=_hint()),
                     provenance=prov)
    result = store.add_memory(
        make_candidate(lesson=AVOID_LESSON, polarity="avoid", reflection_id="refl-2",
                       action_hint={"kind": "purify", "slotKind": None, "school": None}),
        provenance=prov)
    assert result.added == 1 and result.conflicted == 0
    assert all(entry.conflict_group is None for entry in store.entries())


def test_conflict_event_is_emitted(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=DO_LESSON, action_hint=_hint()), provenance=prov)
    store.add_memory(make_candidate(lesson=AVOID_LESSON, polarity="avoid",
                                    action_hint=_hint(), reflection_id="refl-2"),
                     provenance=prov)
    events = [json.loads(line) for line in _lines(store.metrics_path)]
    conflicts = [e for e in events if e["event"] == "memory.conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["rule"] in ("auto", "suppress")
    assert len(conflicts[0]["entry_ids"]) == 2


def test_auto_supersede_off_by_default_but_rule_recorded(store_factory, make_candidate,
                                                         make_prov, cfg) -> None:
    auto = replace(cfg, conflict=replace(cfg.conflict, auto_supersede=True))
    store = store_factory(auto)
    prov = make_prov()
    store.add_memory(make_candidate(lesson=DO_LESSON, action_hint=_hint()), provenance=prov)
    result = store.add_memory(make_candidate(lesson=AVOID_LESSON, polarity="avoid",
                                             action_hint=_hint(), reflection_id="refl-2"),
                              provenance=prov)
    assert result.conflicted == 1
    # 证据不足（n_ref < min_refs）→ 仍然保守共存
    assert all(e.status == "active" for e in store.entries())
    events = [json.loads(line) for line in _lines(store.metrics_path)]
    assert [e for e in events if e["event"] == "memory.conflict"][0]["rule"] == "suppress"


def test_supersede_when_evidence_is_sufficient(mem_entry) -> None:
    """证据门槛：n_ref ≥ min_refs 且 Wilson 95% CI 不重叠 + 胜率差 ≥ 0.15。"""
    from dice_ratchet.infra.config import MemoryConfig

    cfg = MemoryConfig()
    new = mem_entry(n_ref=8, n_adopted=8, n_win_adopted=8, n_not_adopted=0,
                    n_win_not_adopted=0)
    old = mem_entry(n_ref=8, n_adopted=8, n_win_adopted=0, n_not_adopted=8,
                    n_win_not_adopted=0)
    assert _evidence_ok(new, [old], cfg) is True
    weak = mem_entry(n_ref=1, n_adopted=1, n_win_adopted=1, n_not_adopted=0,
                     n_win_not_adopted=0)
    assert _evidence_ok(weak, [old], cfg) is False
    low, high = _wilson_interval(8, 8)
    assert 0.0 <= low <= high <= 1.0
