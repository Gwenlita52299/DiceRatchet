"""MOD-05 golden 场景（G2）：固定反思 + 固定 tick 下的检索输出与快照 sha256。

被 ``test_memory_golden.py`` 与 ``regen_memory_golden.py`` 共用。**任何**改变检索打分、
渲染、去重/合并或快照字节的改动都会让 golden 变红——必须显式重跑 regen 并在 PR 说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dice_ratchet.infra.config import MemoryConfig
from dice_ratchet.memory import (MemoryEntry, MemoryStore, Provenance, ReflectionEntry,
                              RetrieveResult)
from dice_ratchet.memory.snapshot import entries_sha256, read_snapshot, snapshot_path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

RETRIEVE_GOLDEN = HERE / "retrieve_seed1042_g0.json"
SHA256_GOLDEN = HERE / "memory_v0_sha256.txt"

RUN_ID = "golden-memory-1042"
FIXED_AT = "2026-10-02T09:14:11Z"
TICK = 12

#: 固定观测（seed 1042 的第 3 关精英战：净化值 4/20、敌方 purge/克制）
OBS: dict[str, Any] = {
    "schemaVersion": "obs-agent-1.0.0",
    "episodeId": "e-001042-g000-train",
    "decisionIndex": 7,
    "phase": "player_turn",
    "stage": 3,
    "stageCount": 6,
    "chapter": 1,
    "turn": 2,
    "difficulty": "hard",
    "player": {"hearts": 1, "heartsMax": 6, "virtue": 4, "virtueMax": 20, "ward": 0,
               "starShards": 12, "statuses": [], "deckSchool": "blade", "purifyLeft": 1,
               "abilities": []},
    "hand": [{"dieId": "d1", "defId": "blade_1", "school": "blade", "faceIndex": 0,
              "value": 3, "label": "斩", "isCorrupt": True},
             {"dieId": "d2", "defId": "neutral_1", "school": "neutral", "faceIndex": 1,
              "value": 2, "label": "光盾", "isCorrupt": False}],
    "slots": [{"slotId": "s1", "slotKind": "attack", "constraint": "any"},
              {"slotId": "s2", "slotKind": "defense", "constraint": "any"}],
    "enemies": [{"enemyId": "en-1", "defId": "blade_purge", "school": "blade",
                 "hp": 9, "hpMax": 14, "ward": 0, "statuses": [],
                 "intent": [{"kind": "purge", "value": 9, "school": "blade",
                             "arrow": "down"}]}],
    "toggles": {"schoolCounter": True},
    "relics": [],
    "legalActionKinds": ["play_face", "purify", "end_turn"],
    "lastResult": None,
}

_REFLECTIONS: tuple[dict[str, Any], ...] = (
    {
        "reflection_id": "refl-88ac1d0b2fe4",
        "episode_id": "e-001042-g000-train",
        "seed": 1042,
        "tick": 6,
        "text": ("净化值只剩 4 时我还在打输出，结果被打死。"
                 "应该先把光盾骰放进防御槽，再考虑输出。"),
        "outcome": "lose",
        "stages_cleared": 2,
        "hearts_left": 0,
        "failure_cause": "virtue_management",
    },
    {
        "reflection_id": "refl-9a0b1c2d3e4f",
        "episode_id": "e-001043-g000-train",
        "seed": 1043,
        "tick": 7,
        "text": "地图路线优先走左侧商店，避免精英战。",
        "outcome": "win",
        "stages_cleared": 3,
        "hearts_left": 2,
        "failure_cause": "none",
    },
)


@dataclass(frozen=True)
class GoldenCase:
    """一次 golden 场景的全部可比对产物。"""

    retrieve: dict[str, Any]
    snapshot_sha256: str
    snapshot_entries: tuple[MemoryEntry, ...]
    store: MemoryStore
    result: RetrieveResult


def _reflection(spec: dict[str, Any]) -> ReflectionEntry:
    return ReflectionEntry(
        reflection_id=spec["reflection_id"], schema_version="1.1", created_at=FIXED_AT,
        run_id=RUN_ID, episode_id=spec["episode_id"], seed=int(spec["seed"]),
        split="train", generation=0, harness_version=1, tick=int(spec["tick"]),
        text=spec["text"], outcome=spec["outcome"], truncation_reason=None,
        stages_cleared=int(spec["stages_cleared"]), hearts_left=int(spec["hearts_left"]),
        battles_won=1, decisions=20, illegal_attempts=0, fallback_decisions=0,
        failure_cause=spec["failure_cause"], tags=(), extracted_memory_ids=(),
        scan={"status": "pass", "hits": []}, llm_latency_ms=120, prompt_tokens=900,
        completion_tokens=80)


def _provenance(spec: dict[str, Any]) -> Provenance:
    return Provenance(run_id=RUN_ID, episode_id=spec["episode_id"], split="train",
                      seed=int(spec["seed"]), generation=0, harness_version=1)


def golden_view(result: RetrieveResult, *, memory_version: int) -> dict[str, Any]:
    """去掉非确定字段（``elapsed_ms``）后的可比对视图。"""
    return {
        "memory_version": int(memory_version),
        "obs_digest": result.obs_digest,
        "entry_ids": [item.entry_id for item in result.entries],
        "scores": [round(score, 6) for score in result.scores],
        "tokens": int(result.tokens),
        "dropped_reasons": result.dropped_reasons(),
        "conflict_groups": list(result.conflict_groups),
        "text": result.text,
        "entries": [item.to_dict() for item in result.entries],
    }


def build(run_dir: Path) -> GoldenCase:
    """在 ``run_dir`` 上重放 golden 场景；返回可比对产物。"""
    store = MemoryStore(Path(run_dir), MemoryConfig(), config_dir=CONFIG_DIR,
                        run_id=RUN_ID)
    for spec in _REFLECTIONS:
        store.add_reflection(_reflection(spec), provenance=_provenance(spec))

    # 固定一次效用回填（环境原生胜负 + adopted 三元组判定结果）
    first = store.entries()[0]
    store.record_usage("e-001042-g000-train", [first.entry_id], "win",
                       adopted_ids=[first.entry_id], tick=11)

    store.snapshot(reason="gen-000", memory_version=0)
    result = store.retrieve(OBS, top_k=8, token_budget=900, tick=TICK)
    data = read_snapshot(snapshot_path(store.memory_dir, 0), require_version=0, verify=True)
    assert data.computed_sha256 == entries_sha256(data.entry_lines)
    return GoldenCase(retrieve=golden_view(result, memory_version=0),
                      snapshot_sha256=data.computed_sha256,
                      snapshot_entries=data.entries, store=store, result=result)
