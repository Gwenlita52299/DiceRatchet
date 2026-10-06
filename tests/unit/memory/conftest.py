"""MOD-05 单测共享夹具：run 目录 / MemoryConfig / ReflectionEntry / MemoryCandidate 工厂。

PRD §11 的 8 个单测文件共用这里的工厂，保证「同一条反思 → 同一候选 → 同一 entry_id」
在各处一致（G3 确定性的前提）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

from gamer_rsi.infra.config import MemoryConfig
from gamer_rsi.memory import (MemoryCandidate, MemoryEntry, MemoryStore, Provenance,
                              ReflectionEntry, SituationKey)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs"

FIXED_AT = "2026-10-02T09:14:11Z"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture
def cfg() -> MemoryConfig:
    """默认 ``memory.*`` 配置（与 ``configs/default.yaml`` 一致）。"""
    return MemoryConfig()


@pytest.fixture
def make_prov() -> Callable[..., Provenance]:
    def factory(*, seed: int = 1042, split: str = "train", episode_id: str | None = None,
                generation: int = 0, harness_version: int = 1,
                run_id: str = "r-test") -> Provenance:
        return Provenance(run_id=run_id,
                          episode_id=episode_id or f"e-{seed:06d}-g{generation:03d}-{split}",
                          split=split, seed=seed, generation=generation,
                          harness_version=harness_version)
    return factory


@pytest.fixture
def make_reflection() -> Callable[..., ReflectionEntry]:
    def factory(*, text: str = "净化值只剩 3 时我还在打输出，结果被打死。",
                seed: int = 1042, reflection_id: str = "refl-88ac1d0b2fe4",
                outcome: str = "lose", tick: int = 5, harness_version: int = 1,
                generation: int = 0, stages_cleared: int = 2, hearts_left: int = 0,
                split: str = "train", run_id: str = "r-test", **extra: Any
                ) -> ReflectionEntry:
        kwargs: dict[str, Any] = dict(
            reflection_id=reflection_id, schema_version="1.1", created_at=FIXED_AT,
            run_id=run_id, episode_id=f"e-{seed:06d}-g{generation:03d}-{split}",
            seed=seed, split=split, generation=generation,
            harness_version=harness_version, tick=tick, text=text, outcome=outcome,
            truncation_reason=None, stages_cleared=stages_cleared,
            hearts_left=hearts_left, battles_won=1, decisions=20, illegal_attempts=1,
            fallback_decisions=0, failure_cause="virtue_management", tags=(),
            extracted_memory_ids=(), scan={"status": "pass", "hits": []},
            llm_latency_ms=120, prompt_tokens=800, completion_tokens=90)
        kwargs.update(extra)
        return ReflectionEntry(**kwargs)
    return factory


@pytest.fixture
def make_candidate() -> Callable[..., MemoryCandidate]:
    def factory(*, lesson: str = "净化值低于 25% 时先把光盾骰放进防御槽。",
                situation: SituationKey | None = None,
                tags: tuple[str, ...] = ("combat", "low_virtue", "defense"),
                polarity: str = "do",
                action_hint: dict[str, Any] | None = None,
                created_tick: int = 10, reflection_id: str = "refl-88ac1d0b2fe4",
                episode_id: str = "e-001042-g000-train", ttl_ticks: int | None = None,
                confidence: float = 0.7) -> MemoryCandidate:
        return MemoryCandidate(
            situation=situation or SituationKey(
                phase="player_turn", stage_bucket="3-4", node_kind="elite",
                virtue_bucket="critical", hearts_left=1, enemy_school="blade",
                intent_kind="purge", counter_arrow="down", hand_count=4,
                corrupt_count=1, slot_kinds=("attack", "defense", "heal", "utility")),
            tags=tags, lesson=lesson, polarity=polarity, confidence=confidence,
            action_hint=(action_hint if action_hint is not None else
                         {"kind": "play_face", "slotKind": "defense", "school": "neutral"}),
            source_episode_id=episode_id, source_reflection_id=reflection_id,
            created_at=FIXED_AT, created_tick=created_tick, run_id="r-test",
            generation=0, harness_version=1, ttl_ticks=ttl_ticks)
    return factory


@pytest.fixture
def store_factory(tmp_path: Path, config_dir: Path
                  ) -> Callable[..., MemoryStore]:
    def factory(cfg: MemoryConfig | None = None, *, name: str = "run-a",
                run_id: str = "r-test", on_embed: Any = None,
                on_scan: Any = None) -> MemoryStore:
        run_dir = tmp_path / name
        run_dir.mkdir(parents=True, exist_ok=True)
        return MemoryStore(run_dir, cfg or MemoryConfig(), config_dir=config_dir,
                           run_id=run_id, on_embed=on_embed, on_scan=on_scan)
    return factory


@pytest.fixture
def obs_player_turn() -> dict[str, Any]:
    """与 ``make_candidate`` 默认情境对齐的观测（用于检索/加成用例）。"""
    return {
        "schemaVersion": "obs-agent-1.0.0",
        "episodeId": "e-001042-g000-train",
        "decisionIndex": 7,
        "phase": "player_turn",
        "stage": 3,
        "stageCount": 6,
        "player": {"hearts": 1, "heartsMax": 6, "virtue": 4, "virtueMax": 20,
                   "deckSchool": "blade"},
        "hand": [{"dieId": "d1", "isCorrupt": True}, {"dieId": "d2", "isCorrupt": False}],
        "slots": [{"slotKind": "attack"}, {"slotKind": "defense"}],
        "enemies": [{"enemyId": "en-1", "school": "blade",
                     "intent": [{"kind": "purge", "arrow": "down", "value": 9}]}],
    }


@pytest.fixture
def mem_entry() -> Callable[..., MemoryEntry]:
    """构造一条合法 ``MemoryEntry``（字段顺序与 PRD §6.6 的示例一致）。"""
    def factory(**changes: Any) -> MemoryEntry:
        situation = SituationKey(phase="player_turn", stage_bucket="3-4",
                                 node_kind="elite", virtue_bucket="critical",
                                 hearts_left=1, enemy_school="blade",
                                 intent_kind="purge", counter_arrow="down",
                                 hand_count=4, corrupt_count=1,
                                 slot_kinds=("attack", "defense", "heal", "utility"))
        base = MemoryEntry(
            entry_id="mem-3f1c9ab24d07", schema_version="1.1", created_at=FIXED_AT,
            created_tick=37, run_id="a1b2c3d4", generation=2, harness_version=1,
            text_hash="9c1f" + "0" * 60, content_sha256="",
            source_episode_id="e-001037-g002-train",
            source_reflection_id="refl-88ac1d0b2fe4",
            situation=situation,
            situation_key=situation.key(),
            tags=("combat", "low_virtue", "defense", "school_counter"),
            lesson="净化值低于 25% 且敌方意图伤害 ≥ 当前净化值时，先把光盾骰放进防御槽，再考虑输出。",
            action_hint={"kind": "play_face", "slotKind": "defense", "school": "neutral"},
            polarity="do", confidence=0.7, last_ref_tick=52, n_ref=4, n_adopted=3,
            n_win_adopted=2, n_not_adopted=1, n_win_not_adopted=0, utility=0.42,
            ttl_ticks=300, expires_tick=337, status="active")
        base = replace(base, **changes)
        return replace(base, content_sha256=base.compute_content_sha256())
    return factory
