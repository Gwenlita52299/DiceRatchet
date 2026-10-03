"""MOD-02 单测夹具：真实 MOD-01 引擎与各 phase 的真实 state（从 golden 重放）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, load_tables
from gamer_rsi.sandbox.api import EpisodeConfig, SandboxEnv

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN = _REPO_ROOT / "src/game/tests/golden"


@pytest.fixture(scope="module")
def tables():
    return load_tables()


@pytest.fixture(scope="module")
def engine(tables):
    return GameEngine(tables)


@pytest.fixture(scope="module")
def golden_config():
    lines = (_GOLDEN / "g5_hard_six_stage.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["config"]


@pytest.fixture(scope="module")
def player_turn_state(engine, golden_config):
    """从 g5 默认局重放，取一个真实 ``player_turn`` 的 GameState（只读用）。"""
    lines = [json.loads(x) for x in
             (_GOLDEN / "g5_hard_six_stage.jsonl").read_text(encoding="utf-8").splitlines()
             if x.strip()]
    state = engine.create(lines[0]["config"])
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        state = engine.reduce(state, line["action"])["state"]
        if state["phase"] == "player_turn":
            return state
    raise AssertionError("g5 未出现 player_turn")


@pytest.fixture(scope="module")
def shop_state(engine):
    """从 g4 重放，取一个真实 ``shop`` 的 GameState（蜜罐/审计用）。"""
    path = _GOLDEN / "g4_hard_corrupt_path.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    state = engine.create(lines[0]["config"])
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        state = engine.reduce(state, line["action"])["state"]
        if state["phase"] == "shop":
            return state
    raise AssertionError("g4 未出现 shop")


@pytest.fixture
def full_obs(engine, player_turn_state):
    """真实 GAME ``observe(state,"full")`` 输出。"""
    return engine.observe(player_turn_state, "full")


@pytest.fixture
def sandbox(engine):
    """默认局的 SandboxEnv（已 reset，位于 map 阶段）。"""
    env = SandboxEnv(engine=engine, episode_config=EpisodeConfig(difficulty="hard",
                                                                 stage_count=6))
    env.reset(1000, episode_id="e-001000-g000-train")
    return env


@pytest.fixture
def state_copy(player_turn_state):
    return copy.deepcopy(dict(player_turn_state))
