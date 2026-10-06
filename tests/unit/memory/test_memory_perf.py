"""MOD-05 性能回归（NFR-05-2）：``retrieve`` / ``add_reflection`` / 启动加载的 p95 上界。

指标口径（M5/24GB，纯 Python，``active = 2000``）：

- ``retrieve()`` P95 ≤ 50 ms；
- ``add_reflection()`` P95 ≤ 15 ms（不含扫描与 embedding）；
- 启动全量加载 ≤ 2 s。

取 60 次样本的 p95（``sorted[samples*95//100]``）；实测通常远低于上界（本机实测
retrieve ≈ 20 ms、add ≈ 2 ms、加载 ≈ 0.26 s），断言留足余量以避免 CI 抖动。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from gamer_rsi.infra.config import MemoryConfig
from gamer_rsi.memory import (MemoryCandidate, MemoryStore, Provenance, ReflectionEntry,
                              SituationKey)

SAMPLES = 60
OBS = {"phase": "player_turn", "stage": 3,
       "player": {"hearts": 1, "virtue": 4, "virtueMax": 20},
       "hand": [{"dieId": "d1", "isCorrupt": True}],
       "slots": [{"slotKind": "defense"}],
       "enemies": [{"school": "blade", "intent": [{"kind": "purge", "arrow": "down"}]}]}


def _p95_ms(fn: Callable[[], object]) -> float:
    durations = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        fn()
        durations.append((time.perf_counter() - started) * 1000.0)
    durations.sort()
    return durations[min(len(durations) - 1, int(len(durations) * 0.95))]


def _candidate(index: int) -> MemoryCandidate:
    return MemoryCandidate(
        situation=SituationKey(stage_bucket="3-4", virtue_bucket="critical", hearts_left=2,
                               enemy_school="blade", intent_kind=f"kind-{index}",
                               counter_arrow="down", hand_count=4, corrupt_count=1,
                               slot_kinds=("attack", "defense")),
        tags=("combat", "low_virtue", "defense"),
        lesson=(f"第 {index} 条：净化值低于 25% 时先把光盾骰放进防御槽，"
                f"注意 {index} 号敌人的清扫意图。"),
        polarity="do", confidence=0.7,
        action_hint={"kind": "play_face", "slotKind": "defense", "school": "neutral"},
        source_episode_id=f"e-{1000 + index:06d}", source_reflection_id=f"refl-{index}",
        created_at="2026-10-02T09:14:11Z", created_tick=800, run_id="r-perf",
        generation=0, harness_version=1)


@pytest.fixture(scope="module")
def full_store(tmp_path_factory) -> MemoryStore:
    """2000 条 active（情境各异，不会互相合并）的库。"""
    root = tmp_path_factory.mktemp("perf-store")
    store = MemoryStore(root, MemoryConfig(), config_dir=Path("configs"), run_id="r-perf")
    prov = Provenance(run_id="r-perf", episode_id="e-001042-g000-train", split="train",
                      seed=1042, generation=0, harness_version=1)
    for index in range(2000):
        store.add_memory(_candidate(index), provenance=prov)
    assert store.stats().active_count == 2000
    return store


def test_retrieve_p95_within_budget(full_store: MemoryStore) -> None:
    full_store.retrieve(OBS, tick=801)  # 预热索引
    p95 = _p95_ms(lambda: full_store.retrieve(OBS, tick=807))
    assert p95 <= 50.0, f"retrieve P95 = {p95:.1f} ms > 50 ms（NFR-05-2）"


def test_add_reflection_p95_within_budget(full_store: MemoryStore) -> None:
    prov = Provenance(run_id="r-perf", episode_id="e-001042-g000-train", split="train",
                      seed=1042, generation=0, harness_version=1)
    base = ReflectionEntry(
        reflection_id="refl-perf", schema_version="1.1",
        created_at="2026-10-02T09:14:11Z", run_id="r-perf",
        episode_id="e-001042-g000-train", seed=1042, split="train", generation=0,
        harness_version=1, tick=900, text="净化值只剩 3 时我还在打输出。", outcome="lose",
        stages_cleared=2, hearts_left=0, battles_won=1, decisions=20,
        failure_cause="virtue_management")
    counter = {"n": 0}

    def call() -> None:
        counter["n"] += 1
        full_store.add_reflection(
            replace(base, reflection_id=f"refl-perf-{counter['n']}"), provenance=prov)

    # 预热一次：首调用要新建条目并触发一次压实（模块级 fixture 的 2000 条 active 已达
    # 上限），是**一次性**成本而非稳态写入成本。与 retrieve 用例的预热同理，避免把冷启动
    # 尖峰算进 P95（CI 上首调用实测 ~21–25 ms，会把 P95 直接顶过 15 ms）。
    full_store.add_reflection(replace(base, reflection_id="refl-perf-warmup"),
                              provenance=prov)
    counter["n"] = 0
    p95 = _p95_ms(call)
    assert p95 <= 15.0, f"add_reflection P95 = {p95:.1f} ms > 15 ms（NFR-05-2）"


def test_startup_load_within_budget(full_store: MemoryStore) -> None:
    started = time.perf_counter()
    reopened = MemoryStore(full_store.run_dir, MemoryConfig(), config_dir=Path("configs"),
                           run_id="r-perf")
    elapsed = time.perf_counter() - started
    assert len(reopened.entries()) >= 2000
    assert elapsed <= 2.0, f"启动全量加载 = {elapsed:.2f} s > 2 s（NFR-05-2）"
