"""MOD-02 契约漂移集成测试（Spec §16 B-24；MOD-01 × MOD-02 的字段清单对拍）。

两个方向：

1. ``OBSERVATION_SCHEMA`` 的每个 ``source_path`` 必须真的存在于 MOD-01
   ``observe(state,"full")`` 的输出里——否则上线即 ``E_ENV_PROJECTION_MISSING``。
2. ``GameState`` 顶层字段清单快照：MOD-01 增删字段必须让本测试红，强制同步
   ``DELETED_SOURCE_PATHS`` 与 MOD-02 PRD（§14 风险表：字段改名未同步注册表）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, load_tables
from gamer_rsi.sandbox import projection as P

#: ``GameState`` 顶层字段快照（GAME-01 §2 / MOD-01 v1.0）。改动 → 本测试红。
GAMESTATE_TOP_LEVEL = frozenset({
    "stateVersion", "runId", "configHash", "config", "stage", "chapter", "turn",
    "steps", "phase", "player", "enemies", "map", "node", "rng", "dieSeq", "shopSeq",
    "slotSeq", "seenEvents", "pendingRelic", "stats", "illegalCount",
    "consecutiveIllegal", "outcome", "outcomeReason", "truncationReason",
})

#: GAME ``observe(state,"full")`` 顶层键快照（GAME-00 §9.4 obs-1.0.0）。
GAME_OBS_TOP_LEVEL = frozenset({
    "schemaVersion", "runId", "phase", "stage", "chapter", "chapterName", "turn",
    "difficulty", "player", "hand", "piles", "deck", "slots", "enemies", "battleKind",
    "battleHints", "map", "stages", "shop", "event", "treasure", "forge", "shrine",
    "rest", "pendingRelic", "toggles", "config", "relics", "stats", "legalActionKinds",
    "outcome", "outcomeReason", "truncationReason", "seed",
})


@pytest.fixture(scope="module")
def state():
    lines = [json.loads(x) for x in
             (Path(__file__).resolve().parents[2]
              / "src/game/tests/golden/g5_hard_six_stage.jsonl")
             .read_text(encoding="utf-8").splitlines() if x.strip()]
    engine = GameEngine(load_tables())
    cur = engine.create(lines[0]["config"])
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        cur = engine.reduce(cur, line["action"])["state"]
        if cur["phase"] == "player_turn":
            return engine, cur
    raise AssertionError("g5 未出现 player_turn")


def test_gamestate_field_snapshot(state):
    _, cur = state
    assert set(cur) == GAMESTATE_TOP_LEVEL, (
        "GameState 顶层字段漂移：MOD-01 增删字段必须同步 MOD-02 注册表 / "
        "DELETED_SOURCE_PATHS（Spec §16 B-24）")


def test_game_obs_field_snapshot(state):
    engine, cur = state
    obs = engine.observe(cur, "full")
    assert set(obs) == GAME_OBS_TOP_LEVEL, "GAME observe 顶层键漂移"


def test_registry_source_paths_exist_in_game_obs(state):
    engine, cur = state
    obs = engine.observe(cur, "full")
    missing: list[str] = []
    for spec in P.OBSERVATION_SCHEMA:
        path = spec.source_path
        if path.startswith("@"):
            continue
        node: object = obs
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(f"{spec.name} -> {path}")
                break
            node = node[part]
    assert missing == [], f"注册表 source_path 与 GAME obs 漂移：{missing}"


def test_deleted_source_paths_exist_in_game_obs(state):
    """Spec §4.2「删除」清单必须仍然存在于 GAME obs（否则应改为 optional/删除）。"""
    engine, cur = state
    obs = engine.observe(cur, "full")
    for path in P.DELETED_SOURCE_PATHS:
        assert path in obs, f"DELETED_SOURCE_PATHS 里的 {path} 已不在 GAME obs 中"


def test_registry_keeps_only_spec_allowed_sources(state):
    """注册表不得偷偷保留 Spec §4.2「删除」的字段（防越权可见性）。"""
    engine, cur = state
    obs = engine.observe(cur, "full")
    from gamer_rsi.sandbox.api import EpisodeConfig, SandboxEnv

    env = SandboxEnv(engine=engine, episode_config=EpisodeConfig())
    view = env.reset(int(cur["config"]["seed"]), episode_id="e-000001-g000-train")
    for path in P.DELETED_SOURCE_PATHS:
        root = path.split(".")[0]
        assert root not in view, f"{root} 不得出现在 Observation（Spec §4.2）"
    assert view["schemaVersion"] == P.OBSERVATION_SCHEMA_VERSION
    env.close()
