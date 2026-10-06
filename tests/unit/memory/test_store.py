"""FR-05-2/4/8/11/12：写入管线、原子落盘、效用回填、损坏行隔离、自检、确定性（G3）。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.memory import (MemoryEntry, MemoryStore, MemoryWriteFailedError,
                              ReflectionEntry, extract_candidates, utility_score)

LESSON = "净化值低于 25% 时先把光盾骰放进防御槽，再考虑输出。"
OTHER = "地图路线优先走左侧商店，避免精英战。"


def _lines(path) -> list[str]:
    text = Path(path).read_text("utf-8") if Path(path).is_file() else ""
    return [line for line in text.splitlines() if line.strip()]


def _events(store, name: str) -> list[dict]:
    return [json.loads(line) for line in _lines(store.metrics_path)
            if json.loads(line)["event"] == name]


# ---- 写入管线（FR-05-2 / §8.1）--------------------------------------------------------

def test_add_reflection_writes_reflection_and_candidates(store_factory, make_reflection,
                                                         make_prov) -> None:
    store = store_factory()
    reflection = make_reflection(text="净化值只剩 3 时我还在打输出。")
    result = store.add_reflection(reflection, provenance=make_prov())
    assert result.quarantined == 0
    assert result.added == len(result.entry_ids) >= 1
    assert len(_lines(store.reflections_path)) == 1
    recorded = json.loads(_lines(store.reflections_path)[0])
    assert recorded["reflection_id"] == reflection.reflection_id
    assert recorded["scan"]["status"] == "pass"
    assert recorded["extracted_memory_ids"] == list(result.entry_ids)


def test_add_reflection_without_candidate_only_stores_reflection(store_factory,
                                                                make_reflection,
                                                                make_prov) -> None:
    store = store_factory()
    result = store.add_reflection(make_reflection(text="今天天气不错。"),
                                  provenance=make_prov())
    assert (result.added, result.merged, result.quarantined) == (0, 0, 0)
    assert len(_lines(store.reflections_path)) == 1
    assert len(store.entries()) == 0


def test_add_reflection_is_idempotent_for_same_reflection(store_factory, make_reflection,
                                                          make_prov) -> None:
    store = store_factory()
    reflection = make_reflection(text="净化值只剩 3 时我还在打输出。")
    first = store.add_reflection(reflection, provenance=make_prov())
    second = store.add_reflection(reflection, provenance=make_prov())
    assert first.entry_ids == second.entry_ids
    assert len(store.entries()) == len(first.entry_ids)
    assert second.merged == len(second.entry_ids)


def test_entry_fields_come_from_provenance(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    prov = make_prov(seed=1042, generation=3, harness_version=7)
    store.add_memory(make_candidate(), provenance=prov)
    entry = store.entries()[0]
    assert entry.run_id == prov.run_id
    assert entry.generation == 3
    assert entry.harness_version == 7
    assert entry.schema_version == "1.1"
    assert entry.status == "active"
    assert entry.expires_tick == entry.created_tick + entry.ttl_ticks
    assert entry.content_sha256 == entry.compute_content_sha256()


def test_active_file_lines_have_snapshot_shape(store_factory, make_candidate,
                                               make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    row = json.loads(_lines(store.active_path)[0])
    assert row["_type"] == "memory_entry"

    # 追加路径：重开 store 后条目与索引一致（半行/坏行不会污染加载）
    reopened = MemoryStore(store.run_dir, store.cfg, config_dir=store.config_dir,
                           run_id="r-test")
    assert len(reopened.entries()) == 1
    assert reopened.entries()[0].entry_id == store.entries()[0].entry_id


def test_write_is_atomic_no_half_line(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(lesson=LESSON), provenance=make_prov())
    store.add_memory(make_candidate(lesson=OTHER, action_hint=None,
                                    tags=("map_route",), reflection_id="refl-2"),
                     provenance=make_prov())
    text = Path(store.active_path).read_text("utf-8")
    assert text.endswith("\n")
    for line in text.splitlines():
        json.loads(line)


# ---- 效用回填（FR-05-8 / GWT-05-07）---------------------------------------------------

def test_record_usage_updates_counters_and_utility(store_factory, make_candidate,
                                                   make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    entry_id = store.entries()[0].entry_id
    before = store.entries()[0].utility
    store.record_usage("ep-1", [entry_id], "win", adopted_ids=[entry_id], tick=42)
    entry = store.entries()[0]
    assert (entry.n_ref, entry.n_adopted, entry.n_win_adopted) == (1, 1, 1)
    assert entry.last_ref_tick == 42
    assert entry.utility > before
    assert entry.utility == utility_score(entry, tick=42)
    assert store.stats().retrieve_hit_rate == 1.0


def test_record_usage_not_adopted_counts_base_rate(store_factory, make_candidate,
                                                   make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    entry_id = store.entries()[0].entry_id
    store.record_usage("ep-1", [entry_id], "lose", adopted_ids=[], tick=42)
    entry = store.entries()[0]
    assert (entry.n_ref, entry.n_adopted, entry.n_not_adopted) == (1, 0, 1)
    assert entry.n_win_not_adopted == 0
    assert store.stats().retrieve_hit_rate == 0.0


def test_record_usage_ignores_unknown_and_duplicate_ids(store_factory, make_candidate,
                                                        make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    entry_id = store.entries()[0].entry_id
    store.record_usage("ep", [entry_id, entry_id, "mem-does-not-exist"], "win",
                       adopted_ids=[entry_id], tick=5)
    assert store.entries()[0].n_ref == 1


# ---- 损坏行隔离（GWT-05-09）----------------------------------------------------------

def test_corrupt_middle_line_is_quarantined(store_factory, make_candidate,
                                            make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSON), provenance=prov)
    store.add_memory(make_candidate(lesson=OTHER, action_hint=None,
                                    reflection_id="refl-2"), provenance=prov)
    lines = _lines(store.active_path)
    assert len(lines) == 2
    lines.insert(1, '{"entry_id": "mem-broken", "schema_version"')
    Path(store.active_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = MemoryStore(store.run_dir, store.cfg, config_dir=store.config_dir,
                           run_id="r-test")
    assert reopened.stats().entry_count == 2
    quarantine = [json.loads(line) for line in _lines(reopened.quarantine_path)]
    assert len(quarantine) == 1
    assert quarantine[0]["reason"] == "corrupt"
    assert quarantine[0]["code"] == str(E.E_IO_MEMORY_CORRUPT)
    events = _events(reopened, "memory.quarantine")
    assert events and str(events[0]["code"]) in (str(E.E_IO_MEMORY_CORRUPT),
                                                 "E_IO_MEMORY_CORRUPT")


def test_truncated_tail_line_is_quarantined(store_factory, make_candidate,
                                            make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    text = Path(store.active_path).read_text("utf-8")
    Path(store.active_path).write_text(text + '{"entry_id": "mem-half', encoding="utf-8")
    reopened = MemoryStore(store.run_dir, store.cfg, config_dir=store.config_dir,
                           run_id="r-test")
    assert reopened.stats().entry_count == 1
    assert len(_lines(reopened.quarantine_path)) == 1


def test_duplicate_entry_id_is_quarantined(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    line = _lines(store.active_path)[0]
    Path(store.active_path).write_text(line + "\n" + line + "\n", encoding="utf-8")
    reopened = MemoryStore(store.run_dir, store.cfg, config_dir=store.config_dir,
                           run_id="r-test")
    assert reopened.stats().entry_count == 1
    assert any("重复" in json.loads(row)["message"]
               for row in _lines(reopened.quarantine_path))


# ---- verify（FR-05-11）---------------------------------------------------------------

def test_verify_detects_content_sha256_mismatch(store_factory, make_candidate,
                                                make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    row = json.loads(_lines(store.active_path)[0])
    row["lesson"] = "被手工改过的教训。"
    Path(store.active_path).write_text(json.dumps(row, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    report = store.verify(1, full=True)
    assert report.sha256_mismatch == 1
    assert not report.healthy
    assert str(E.E_IO_MEMORY_CORRUPT) in report.codes


def test_verify_detects_situation_key_mismatch(store_factory, make_candidate,
                                               make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    row = json.loads(_lines(store.active_path)[0])
    row["situation_key"] = "sk-00000000"
    row["content_sha256"] = MemoryEntry.from_json(
        json.dumps({k: v for k, v in row.items() if k != "_type"})).with_updates(
        situation_key="sk-00000000").content_sha256
    Path(store.active_path).write_text(json.dumps(row, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    report = store.verify(1, full=True)
    assert report.situation_key_mismatch == 1


def test_verify_healthy_on_fresh_store(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    report = store.verify(1, full=True)
    assert report.healthy and report.checked == 1 and report.ok == 1


# ---- stats（FR-05-11 / §13）-----------------------------------------------------------

def test_stats_reports_versions_and_rates(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    prov = make_prov()
    store.add_memory(make_candidate(lesson=LESSON), provenance=prov)
    store.add_memory(make_candidate(lesson=LESSON, reflection_id="refl-2"), provenance=prov)
    stats = store.stats().to_dict()
    assert stats["entry_count"] == 1
    assert stats["active_count"] == 1
    assert stats["candidate_count"] == 2
    assert stats["merged_count"] == 1
    assert stats["dedup_rate"] == 0.5
    assert stats["seed_leak_blocks"] == 0
    json.dumps(stats)  # 可 JSON 序列化（CLI/报告）


# ---- 确定性（G3）---------------------------------------------------------------------

def test_same_inputs_produce_identical_active_file(tmp_path, config_dir, make_candidate,
                                                   make_prov) -> None:
    from gamer_rsi.infra.config import MemoryConfig

    def build(name: str) -> str:
        run_dir = tmp_path / name
        run_dir.mkdir()
        store = MemoryStore(run_dir, MemoryConfig(), config_dir=config_dir,
                            run_id="r-test")
        for index, lesson in enumerate((LESSON, OTHER)):
            store.add_memory(make_candidate(lesson=lesson, reflection_id=f"refl-{index}"),
                             provenance=make_prov())
        store.close()
        return Path(store.active_path).read_text("utf-8")

    assert build("a") == build("b")


def test_retrieve_is_byte_identical_across_calls(store_factory, make_candidate,
                                                 make_prov, obs_player_turn) -> None:
    store = store_factory()
    store.add_memory(make_candidate(), provenance=make_prov())
    first = store.retrieve(obs_player_turn, tick=11)
    second = store.retrieve(obs_player_turn, tick=11)
    assert first.text == second.text
    assert first.scores == second.scores
    assert [e.entry_id for e in first.entries] == [e.entry_id for e in second.entries]
    assert first.dropped == second.dropped


# ---- 关闭开关 / 陈旧血缘（FR-05-12 / §9）---------------------------------------------

def test_disabled_memory_is_noop(store_factory, make_candidate, make_prov, cfg,
                                 obs_player_turn) -> None:
    store = store_factory(replace(cfg, enabled=False))
    result = store.add_memory(make_candidate(), provenance=make_prov())
    assert result.added == 0 and result.reasons == ("memory_disabled",)
    assert store.retrieve(obs_player_turn, tick=1).entries == ()
    assert not Path(store.active_path).exists()


def test_stale_harness_entries_are_excluded(store_factory, make_candidate, make_prov,
                                            obs_player_turn) -> None:
    """FR-05-12：``harness_version > current`` 的条目默认不参与检索。"""
    store = store_factory()
    store.add_memory(make_candidate(lesson=LESSON), provenance=make_prov(harness_version=7))
    assert len(store.retrieve(obs_player_turn, tick=12).entries) == 1
    store.set_harness_version(6)
    result = store.retrieve(obs_player_turn, tick=12)
    assert result.entries == ()
    assert [d.reason for d in result.dropped] == ["stale_harness"]
    assert len(store.retrieve(obs_player_turn, tick=12,
                              exclude_stale_harness=False).entries) == 1


# ---- 写失败（PRD §10 E_IO_MEMORY_WRITE_FAILED）--------------------------------------

def test_write_failure_disables_writes_after_three(store_factory, make_candidate,
                                                   make_prov, monkeypatch) -> None:
    store = store_factory()

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    for _ in range(3):
        with pytest.raises(MemoryWriteFailedError) as exc:
            store.add_memory(make_candidate(), provenance=make_prov())
        assert exc.value.code == E.E_IO_MEMORY_WRITE_FAILED
    assert store._write_disabled is True
    result = store.add_memory(make_candidate(), provenance=make_prov())
    assert result.added == 0  # 停写后 no-op（读不受影响）


def test_write_failure_is_reported_as_event(store_factory, make_candidate, make_prov,
                                            monkeypatch) -> None:
    store = store_factory()
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(MemoryWriteFailedError):
        store.add_memory(make_candidate(), provenance=make_prov())
    assert store.stats().write_failures == 1


# ---- 向量检索（可选，Spec B-01）与陈锁接管 -------------------------------------------

def _with_embedding(cfg, *, enabled: bool = True, dim: int = 4, vector_weight: float = 1.0):
    return replace(
        cfg,
        embedding=replace(cfg.embedding, enabled=enabled, dim=dim),
        retrieve=replace(cfg.retrieve,
                         weights={**cfg.retrieve.weights, "vector": vector_weight}))


def test_embedding_hook_attaches_vector(store_factory, make_candidate, make_prov, cfg) -> None:
    store = store_factory(_with_embedding(cfg),
                          on_embed=lambda text: [1.0, 0.0, 0.0, 0.0])
    store.add_memory(make_candidate(), provenance=make_prov())
    assert store.entries()[0].embedding == (1.0, 0.0, 0.0, 0.0)


def test_embedding_disabled_by_default(store_factory, make_candidate, make_prov, cfg) -> None:
    store = store_factory(cfg, on_embed=lambda text: [1.0, 0.0, 0.0, 0.0])
    store.add_memory(make_candidate(), provenance=make_prov())
    assert store.entries()[0].embedding is None


def test_embedding_dim_mismatch_degrades(store_factory, make_candidate, make_prov,
                                        cfg) -> None:
    store = store_factory(_with_embedding(cfg, dim=8),
                          on_embed=lambda text: [1.0, 0.0, 0.0, 0.0])
    store.add_memory(make_candidate(), provenance=make_prov())
    assert store.entries()[0].embedding is None          # 降级为 BM25
    events = _events(store, "memory.write_failed")
    assert any(str(e.get("code")) == str(E.E_LLM_UNSUPPORTED) for e in events)


def test_embedding_failure_degrades(store_factory, make_candidate, make_prov, cfg) -> None:
    def boom(_text: str):
        raise RuntimeError("E_LLM_UNSUPPORTED")

    store = store_factory(_with_embedding(cfg), on_embed=boom)
    store.add_memory(make_candidate(), provenance=make_prov())
    assert store.entries()[0].embedding is None


def test_vector_weight_uses_query_embedding(store_factory, make_candidate, make_prov,
                                            cfg, obs_player_turn) -> None:
    """向量权重开启且两向量一致 → 恰好贡献 ``w_vec * 1.0``（cos+1)/2 = 1）。"""
    vector = [1.0, 0.0, 0.0, 0.0]
    on = store_factory(_with_embedding(cfg, vector_weight=1.0), name="run-vec",
                       on_embed=lambda text: vector)
    on.add_memory(make_candidate(), provenance=make_prov())
    with_vector = on.retrieve(obs_player_turn, tick=12).entries[0].score

    off = store_factory(_with_embedding(cfg, vector_weight=0.0), name="run-novec",
                        on_embed=lambda text: vector)
    off.add_memory(make_candidate(), provenance=make_prov())
    without_vector = off.retrieve(obs_player_turn, tick=12).entries[0].score
    assert with_vector - without_vector == pytest.approx(1.0, abs=2e-6)


def test_embedding_provider_not_wired_is_noop(store_factory, make_candidate, make_prov,
                                              cfg) -> None:
    store = store_factory(_with_embedding(cfg))
    store.add_memory(make_candidate(), provenance=make_prov())
    assert store.entries()[0].embedding is None


# ---- 可选 LLM 抽取（§9 memory.extract.use_llm + §10 E_LLM_EXTRACT_FAILED）------------

def _llm_cfg(cfg):
    return replace(cfg, extract=replace(cfg.extract, use_llm=True))


def test_llm_extract_used_when_enabled(tmp_path, config_dir, make_reflection,
                                       make_prov, cfg) -> None:
    from gamer_rsi.memory import MemoryStore

    seen: dict[str, object] = {}

    def on_extract(reflection):
        seen["called"] = reflection.reflection_id
        return [{"lesson": "敌方清扫前先放光盾，别急着输出。", "tags": ["combat", "defense"],
                 "polarity": "do", "confidence": 0.9,
                 "action_hint": {"kind": "play_face", "slotKind": "defense",
                                 "school": "neutral"}}]

    store = MemoryStore(tmp_path / "llm", _llm_cfg(cfg), config_dir=config_dir,
                        run_id="r-test", on_extract=on_extract)
    result = store.add_reflection(make_reflection(text="任意文本。"), provenance=make_prov())
    assert result.added == 1 and seen["called"] == "refl-88ac1d0b2fe4"
    entry = store.entries()[0]
    assert entry.lesson.startswith("敌方清扫前先放光盾")
    assert entry.confidence == 0.9
    assert set(entry.tags) == {"combat", "defense"}


def test_llm_extract_failure_falls_back_to_rules(tmp_path, config_dir, make_reflection,
                                                 make_prov, cfg) -> None:
    from gamer_rsi.memory import MemoryStore

    def boom(_reflection):
        raise TimeoutError("E_LLM_TIMEOUT")

    store = MemoryStore(tmp_path / "llm-fail", _llm_cfg(cfg), config_dir=config_dir,
                        run_id="r-test", on_extract=boom)
    result = store.add_reflection(make_reflection(text="净化值只剩 3 时我还在打输出。"),
                                  provenance=make_prov())
    assert result.added >= 1                      # 规则抽取兜住了
    events = _events(store, "memory.write_failed")
    assert any(str(e.get("code")) == str(E.E_LLM_EXTRACT_FAILED) for e in events)


def test_llm_extract_garbage_falls_back_to_rules(tmp_path, config_dir, make_reflection,
                                                 make_prov, cfg) -> None:
    from gamer_rsi.memory import MemoryStore

    store = MemoryStore(tmp_path / "llm-garbage", _llm_cfg(cfg), config_dir=config_dir,
                        run_id="r-test", on_extract=lambda _r: [{"nonsense": 1}])
    result = store.add_reflection(make_reflection(text="净化值只剩 3 时我还在打输出。"),
                                  provenance=make_prov())
    assert result.added >= 1
    assert any(str(e.get("code")) == str(E.E_LLM_EXTRACT_FAILED)
               for e in _events(store, "memory.write_failed"))


def test_llm_extract_not_used_when_disabled(tmp_path, config_dir, make_reflection,
                                            make_prov, cfg) -> None:
    from gamer_rsi.memory import MemoryStore

    calls = {"n": 0}

    def on_extract(_reflection):
        calls["n"] += 1
        return []

    store = MemoryStore(tmp_path / "llm-off", cfg, config_dir=config_dir,
                        run_id="r-test", on_extract=on_extract)
    store.add_reflection(make_reflection(text="净化值只剩 3 时我还在打输出。"),
                         provenance=make_prov())
    assert calls["n"] == 0


def test_config_switches_are_exposed(store_factory, cfg) -> None:
    """§9 的两个开关必须真的被消费（而不是只躺在 configs 里）。"""
    from dataclasses import replace as _replace

    store = store_factory(cfg)
    assert store.enabled is True
    assert store.snapshot_every_generation is True
    off = store_factory(_replace(cfg, snapshot=_replace(cfg.snapshot,
                                                        every_generation=False)),
                        name="run-off")
    assert off.snapshot_every_generation is False
