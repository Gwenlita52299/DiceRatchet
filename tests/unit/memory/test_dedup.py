"""FR-05-3 / GWT-05-03：精确去重、近似去重（阈值 + 同情境）、跨情境不合并。"""

from __future__ import annotations

import json
from pathlib import Path

from gamer_rsi.memory import SituationKey, similarity

LESSON = "净化值低于 25% 时先把光盾骰放进防御槽，再考虑输出。"


def _lines(path: Path) -> list[str]:
    if not Path(path).is_file():
        return []
    return [line for line in Path(path).read_text("utf-8").splitlines() if line.strip()]


def test_exact_duplicate_merges_without_new_row(store_factory, make_candidate,
                                               make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    first = make_candidate(lesson=LESSON)
    assert store.add_memory(first, provenance=prov).added == 1
    rows_before = len(_lines(store.active_path))
    merged_from_before = len(store.entries()[0].merged_from)

    result = store.add_memory(make_candidate(lesson=LESSON, reflection_id="refl-2"),
                              provenance=prov)
    assert result.merged == 1 and result.added == 0
    assert len(_lines(store.active_path)) == rows_before
    entry = store.entries()[0]
    assert len(entry.merged_from) == merged_from_before + 1
    # add_memory 没有局末 outcome：只累加 n_ref（不臆造胜负计数）
    assert entry.n_ref == 1 and entry.n_not_adopted == 0
    assert entry.merged_from[0] != entry.entry_id


def test_near_duplicate_merges_above_threshold(store_factory, make_candidate,
                                               make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    near = LESSON[:-1] + "！"  # 只差一个句末标点
    assert similarity(make_candidate(lesson=LESSON), make_candidate(lesson=near)) >= 0.85
    store.add_memory(make_candidate(lesson=LESSON), provenance=prov)
    rows_before = len(_lines(store.active_path))
    result = store.add_memory(make_candidate(lesson=near, reflection_id="refl-near"),
                              provenance=prov)
    assert result.merged == 1
    assert len(_lines(store.active_path)) == rows_before
    entry = store.entries()[0]
    assert entry.n_ref == 1 and len(entry.merged_from) == 1
    assert entry.confidence == 0.7


def test_dissimilar_same_situation_adds_row(store_factory, make_candidate,
                                            make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSON), provenance=prov)
    result = store.add_memory(
        make_candidate(lesson="不要让魔化骰进入攻击槽，先净化再输出。",
                       tags=("combat", "corruption"), reflection_id="refl-other"),
        provenance=prov)
    assert result.added == 1
    assert len(_lines(store.active_path)) == 2


def test_same_lesson_different_situation_adds_row(store_factory, make_candidate,
                                                  make_prov) -> None:
    """GWT-05-03：同 lesson 但 ``phase``/``virtue_bucket`` 不同 → 新增一行。"""
    store = store_factory()
    prov = make_prov()
    base = make_candidate(lesson=LESSON)
    other = make_candidate(
        lesson=LESSON,
        situation=SituationKey(phase="shop", stage_bucket="5-6", virtue_bucket="high",
                               hearts_left=3, hand_count=0))
    assert base.situation_key != other.situation_key
    store.add_memory(base, provenance=prov)
    result = store.add_memory(other, provenance=prov)
    assert result.added == 1
    assert len(_lines(store.active_path)) == 2


def test_merge_keeps_survivor_with_higher_utility(store_factory, make_candidate,
                                                  make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSON), provenance=prov)
    entry_id = store.entries()[0].entry_id
    store.record_usage("ep-1", [entry_id], "win", adopted_ids=[entry_id], tick=12)
    survivor = store.entries()[0]
    assert survivor.utility > 0.2

    store.add_memory(make_candidate(lesson=LESSON, reflection_id="refl-2"),
                     provenance=prov)
    assert len(store.entries()) == 1
    merged = store.entries()[0]
    assert merged.entry_id == entry_id                 # survivor = utility 高者
    assert merged.n_ref == survivor.n_ref + 1          # 引用计数累加
    assert merged.n_adopted == survivor.n_adopted      # 采纳证据只由 record_usage 回填
    assert -1.0 <= merged.utility <= 1.0


def test_merge_persists_to_disk(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSON), provenance=prov)
    store.add_memory(make_candidate(lesson=LESSON, reflection_id="refl-2"), provenance=prov)
    # 合并改写的是**已有行**（不新增行），落盘只能整档重写，因此按 DIRTY_ROWS_PER_COMPACT
    # 攒批；close()/snapshot() 是显式收口，这里用 close() 把攒批的改写落盘。
    store.close()
    rows = [json.loads(line) for line in _lines(store.active_path)]
    assert len(rows) == 1
    assert rows[0]["n_ref"] == 1 and len(rows[0]["merged_from"]) == 1


def test_dedup_rate_in_stats(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    for index in range(4):
        store.add_memory(make_candidate(lesson=LESSON, reflection_id=f"refl-{index}"),
                         provenance=prov)
    stats = store.stats()
    assert stats.candidate_count == 4
    assert stats.merged_count == 3
    assert stats.dedup_rate == 0.75
