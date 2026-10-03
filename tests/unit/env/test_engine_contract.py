"""MOD-01 单元测试：错误映射 / observe 白名单 / 合法性 / 纯函数 / 类型一致性。

- T-08：``map_error_code``（FR-01-10）
- T-10：observe 白名单（K-5：obs 不得泄漏 state 私有字段）
- 合法性边界：非法动作不改变 state（GAME-01 §4）
- K-1：reduce 不修改入参（冻结入参断言）
- G5：types.py 与 GameState 实际字段一致性
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, digest, load_tables, map_error_code
from gamer_rsi.env.engine import EnvError, native_result, observe
from gamer_rsi.env.error import KernelError

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def tables():
    return load_tables()


@pytest.fixture(scope="module")
def engine(tables):
    return GameEngine(tables)


@pytest.fixture(scope="module")
def started(engine):
    """推进一步到第一场战斗的 state（player_turn）。"""
    lines = [json.loads(x) for x in
             (_REPO_ROOT / "src/game/tests/golden/g1_all_off_medium.jsonl").read_text().splitlines() if x.strip()]
    state = engine.create(lines[0]["config"])
    state = engine.reduce(state, {"kind": "map_choose", "nodeId": "n-01-0-0"})["state"]
    assert state["phase"] == "player_turn"
    return state


# ======================================================================
# T-08 错误码映射
# ======================================================================

@pytest.mark.parametrize("js_code,expect", [
    ("E_KERNEL_RNG_STREAM_MISSING", ("E_ENV_RNG", "E_KERNEL_RNG_STREAM_MISSING")),
    ("E_KERNEL_RNG_RANGE", ("E_ENV_RNG", "E_KERNEL_RNG_RANGE")),
    ("E_KERNEL_INVARIANT", ("E_ENV_INTERNAL", "E_KERNEL_INVARIANT")),
    ("E_KERNEL_EXCEPTION", ("E_ENV_INTERNAL", "E_KERNEL_EXCEPTION")),
    ("E_KERNEL_DISPATCH", ("E_ENV_INTERNAL", "E_KERNEL_DISPATCH")),
    ("E_CONTENT_FACES", ("E_ENV_CONTENT_INVALID", "E_CONTENT_FACES")),
    ("E_CONTENT_SCHEMA", ("E_ENV_CONTENT_INVALID", "E_CONTENT_SCHEMA")),
    ("E_KERNEL_BAD_CONFIG", ("E_ENV_CONFIG_INVALID", None)),
    ("E_ACTION_ILLEGAL", ("E_ACTION_ILLEGAL", None)),
    ("E_ACTION_BAD_TARGET", ("E_ACTION_BAD_TARGET", None)),
])
def test_map_error_code(js_code, expect):
    assert map_error_code(js_code) == expect


def test_engine_create_maps_config_error(engine):
    with pytest.raises(EnvError) as excinfo:
        engine.create({"difficulty": "medium", "seed": 1, "character": "rogue"})
    assert excinfo.value.code == "E_ENV_CONFIG_INVALID"


def test_illegal_action_keeps_state_and_code(engine, started):
    """非法动作返回原 state 与 illegal=True（GAME-01 §4），不抛异常。"""
    illegal = {"kind": "play_face", "dieId": "d-nope", "slotId": "slot_1"}
    r = engine.reduce(started, illegal)
    assert r["illegal"] is True
    assert r["state"] is started  # 同一对象，未拷贝未修改
    assert r["code"] == "E_ACTION_DIE_NOT_IN_HAND"
    assert r["reason"] == "骰子不在手中"
    assert any(e["type"] == "illegal" for e in r["events"])


def test_end_turn_blocked_by_corrupt_die(engine):
    """g4 覆盖的路径：手中有魔化骰时 end_turn 被拒绝（状态不变）。"""
    raw = (_REPO_ROOT / "src/game/tests/golden/g4_hard_corrupt_path.jsonl").read_text().splitlines()
    lines = [json.loads(x) for x in raw if x.strip()]
    state = engine.create(lines[0]["config"])
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        r = engine.reduce(state, line["action"])
        state = r["state"]
        if state["phase"] == "player_turn" and any(
                state["player"]["dice"][d]["isCorrupt"] for d in state["player"]["hand"]):
            break
    else:
        pytest.skip("该 golden 中未出现带魔化骰的回合")
    assert engine.is_legal(state, {"kind": "end_turn"})["legal"] is False
    r = engine.reduce(state, {"kind": "end_turn"})
    assert r["illegal"] is True
    assert r["code"] == "E_ACTION_CORRUPT_DIE_REMAINS"


# ======================================================================
# T-10 observe 白名单
# ======================================================================

_HIDDEN_KEYS = {"rng", "dieSeq", "shopSeq", "slotSeq", "seenEvents",
                "illegalCount", "consecutiveIllegal", "configHash", "steps"}


def test_observe_hides_private_state(engine, started):
    obs = engine.observe(started)
    assert set(obs.keys()).isdisjoint(_HIDDEN_KEYS)
    # 玩家手牌白名单字段（不得泄漏 deck 之外的骰池细节如 drawPile 顺序）
    for h in obs["hand"]:
        assert set(h.keys()) == {"dieId", "defId", "name", "category", "school", "dieSchool",
                                 "riskTier", "faceIndex", "pip", "label", "kind", "value",
                                 "statusKind", "target", "isCorrupt", "corruptionValue",
                                 "needsTarget", "faces"}


def test_observe_is_pure(engine, started):
    obs = engine.observe(started)
    assert engine.observe(started) == obs
    assert digest(obs) == digest(engine.observe(started))


def test_legal_actions_subset_of_is_legal(engine, started):
    legal = engine.legal_actions(started)
    assert legal, "至少应有 abandon_run"
    for a in legal:
        assert engine.is_legal(started, a)["legal"] is True
    assert "abandon_run" in [a["kind"] for a in legal]


def test_native_result_fields(engine, started):
    r = native_result(started)
    assert set(r.keys()) == {"outcome", "truncationReason", "stagesCleared", "heartsLeft",
                             "battlesWon", "turns", "kills"}


# ======================================================================
# K-1 纯函数：reduce 不修改入参
# ======================================================================

def test_reduce_does_not_mutate_input(engine, started):
    before = copy.deepcopy(started)
    legal = next(a for a in engine.legal_actions(started) if a["kind"] == "play_face")
    engine.reduce(started, legal)
    assert started == before, "reduce 修改了入参 state（违反 K-1/P-14）"


def test_tables_are_read_only(tables):
    """load_tables 返回深度冻结视图：修改应失败。"""
    import types
    assert isinstance(tables["monsters"], types.MappingProxyType)
    with pytest.raises(TypeError):
        tables["monsters"]["mon_verdant_slime"] = {}


# ======================================================================
# G5：types.py 与实际 GameState 字段一致性
# ======================================================================

def test_types_cover_real_state_fields(engine):
    import typing
    from gamer_rsi.env import types as env_types
    hints = typing.get_type_hints(env_types.GameState)
    real = set(engine.create({"difficulty": "medium", "seed": 1}).keys())
    missing = real - set(hints)
    assert not missing, f"GameState TypedDict 缺少字段：{missing}"


def test_types_cover_real_obs_fields(engine, started):
    import typing
    from gamer_rsi.env import types as env_types
    hints = typing.get_type_hints(env_types.Observation)
    real = set(observe(started, "full", engine._tables).keys())
    missing = real - set(hints)
    assert not missing, f"Observation TypedDict 缺少字段：{missing}"


# ======================================================================
# 结构化克隆等价：state 必须是纯 JSON（可 deepcopy / pickle）
# ======================================================================

def test_state_is_json_serializable(engine, started):
    """state 不得残留冻结视图（MappingProxy），否则 deepcopy/pickle 失败（FR-01-7）。"""
    copy.deepcopy(started)
    json.dumps(started, ensure_ascii=False)


def test_kernel_error_round_trip():
    err = KernelError("E_KERNEL_INVARIANT", "boom")
    assert err.code == "E_KERNEL_INVARIANT"
    assert str(err) == "boom"
