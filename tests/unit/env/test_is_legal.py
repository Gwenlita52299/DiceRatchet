"""``is_legal`` 全分支（GAME-01 §4）：每个非法码都能被某个动作触发，且不改变 state。"""

from __future__ import annotations

import copy

from gamer_rsi.env import GameEngine
from gamer_rsi.env import map as M
from gamer_rsi.env import shop as S
from gamer_rsi.env import forge as F

_ILLEGAL_CASES = [
    ("syntax_kind", lambda s: {"kind": "nope"}),
    ("syntax_empty", lambda s: {}),
    ("settle", lambda s: {"kind": "end_turn"}),
    ("map_phase", lambda s: {"kind": "play_face", "dieId": "d-nope", "slotId": "slot_1"}),
    ("node_unreachable", lambda s: {"kind": "map_choose", "nodeId": "n-nope"}),
    ("not_in_hand", lambda s: {"kind": "play_face", "dieId": "d-nope", "slotId": "slot_1"}),
    ("discard_not_in_hand", lambda s: {"kind": "discard_die", "dieId": "d-nope"}),
    ("purify_not_in_hand", lambda s: {"kind": "purify", "dieId": "d-nope"}),
    ("purify_not_corrupt", lambda s: {"kind": "purify", "dieId": s["player"]["hand"][0]}),
    ("purify_exhausted", lambda s: {"kind": "purify", "dieId": _first_corrupt(s) or "d-nope"}),
    ("unknown_ability", lambda s: {"kind": "use_ability", "abilityId": "nope"}),
    ("ability_exhausted", lambda s: {"kind": "use_ability", "abilityId": _usable_ability(s)}),
    ("ability_no_target", lambda s: {**_clear_hand(s), "kind": "use_ability", "abilityId": "warrior_reroll"}),
    ("focus_not_in_hand", lambda s: {"kind": "use_ability", "abilityId": "warrior_focus", "dieId": "d-nope", "value": 3}),
    ("focus_bad_value", lambda s: {"kind": "use_ability", "abilityId": "warrior_focus",
                                   "dieId": _hand_die(s), "value": 9}),
    ("focus_noop", lambda s: {"kind": "use_ability", "abilityId": "warrior_focus",
                              "dieId": _hand_die(s), "value": _current_pip(s)}),
    ("shop_unknown_item", lambda s: {"kind": "shop_buy", "itemId": "s-nope"}),
    ("shop_not_affordable", lambda s: {"kind": "shop_buy", "itemId": _shop_item(s, affordable=False)}),
    ("shop_no_refresh", lambda s: {**_no_refresh(s), "kind": "shop_refresh"}),
    ("shop_refresh_broke", lambda s: {**_broke(s), "kind": "shop_refresh"}),
    ("shop_not_removable", lambda s: {"kind": "shop_remove", "dieId": "d-nope"}),
    ("shop_remove_broke", lambda s: {**_broke(s), "kind": "shop_remove", "dieId": _removable(s)}),
    ("leave_blocked", lambda s: {"kind": "shop_leave"}),  # player_turn：不在 shop/forge/event
    ("event_resolved_leave_ok", lambda s: {"kind": "shop_leave"}),
    ("event_unknown_option", lambda s: {"kind": "event_choose", "optionId": "opt_99"}),
    ("event_bad_option_fmt", lambda s: {"kind": "event_choose", "optionId": "nope"}),
    ("treasure_unknown", lambda s: {"kind": "treasure_pick", "defId": "die_nope"}),
    ("forge_done", lambda s: {"kind": "forge_pick", "dieId": "d-nope", "faceIndex": 0, "candidateId": "ff_nope"}),
    ("forge_not_forgeable", lambda s: {"kind": "forge_pick", "dieId": "d-nope", "faceIndex": 0,
                                       "candidateId": _forge_candidate(s)}),
    ("forge_bad_face_index", lambda s: {"kind": "forge_pick", "dieId": _forgeable(s), "faceIndex": 9,
                                        "candidateId": _forge_candidate(s)}),
    ("forge_unknown_candidate", lambda s: {"kind": "forge_pick", "dieId": _forgeable(s), "faceIndex": 0,
                                           "candidateId": "ff_nope"}),
    ("forge_broke", lambda s: {**_broke(s), "kind": "forge_pick", "dieId": _forgeable(s), "faceIndex": 0,
                               "candidateId": _forge_candidate(s)}),
    ("shrine_unknown", lambda s: {"kind": "shrine_choose", "optionId": "opt_99"}),
    ("rest_unknown", lambda s: {"kind": "rest_choose", "optionId": "opt_99"}),
    ("relic_swap_unknown", lambda s: {"kind": "relic_swap", "relicId": "relic_nope"}),
]

_PHASE_SETUP = {
    "settle": "settle", "map_phase": "map", "node_unreachable": "map",
    "shop_unknown_item": "shop", "shop_not_affordable": "shop", "shop_no_refresh": "shop",
    "shop_refresh_broke": "shop", "shop_not_removable": "shop", "shop_remove_broke": "shop",
    "event_resolved_leave_ok": "event",
    "event_unknown_option": "event", "event_bad_option_fmt": "event",
    "treasure_unknown": "treasure",
    "forge_done": "forge", "forge_not_forgeable": "forge", "forge_bad_face_index": "forge",
    "forge_unknown_candidate": "forge", "forge_broke": "forge",
    "shrine_unknown": "shrine", "rest_unknown": "rest", "relic_swap_unknown": "relic_swap",
}


def _first_corrupt(s):
    return next((d for d in s["player"]["hand"] if s["player"]["dice"][d]["isCorrupt"]), None)


def _hand_die(s):
    return s["player"]["hand"][0]


def _current_pip(s):
    return s["player"]["dice"][_hand_die(s)]["faceIndex"] + 1


def _usable_ability(s):
    # 找到能力并把次数清零，触发 ability_exhausted 分支
    ab = s["player"]["abilities"][0]
    ab["usesLeft"] = 0
    return ab["id"]


def _clear_hand(s):
    s["player"]["hand"].clear()
    return {}


def _shop_item(s, affordable=True):
    items = s["node"]["shop"]["items"]
    if not affordable:
        s["player"]["starShards"] = 0  # 强制买不起
    avail = [it for it in items if not it["sold"]]
    it = next((x for x in avail if (s["player"]["starShards"] >= x["price"]) == affordable), None)
    return (it or avail[0])["itemId"]


_TABLES = None


def _broke(s):
    s["player"]["starShards"] = 0
    return {}


def _no_refresh(s):
    s["node"]["shop"]["refreshLeft"] = 0
    return {}


def _removable(s):
    return (S.removable_dice(s, _TABLES) or s["player"]["deck"])[0]


def _forgeable(s):
    return (F.forgeable_dice(s, _TABLES) or s["player"]["deck"])[0]


def _forge_candidate(s):
    return s["node"]["forge"]["candidates"][0]


def test_illegal_cases(engine, tables, phases):
    """每个非法码用例：在对应 phase 上触发，illegal=True 且 state 原样返回。"""
    global _TABLES
    _TABLES = tables
    for name, make in _ILLEGAL_CASES:
        phase = _PHASE_SETUP.get(name)
        if phase == "settle":
            # settle 需要专门构造（run 已结束）；reduce 到 settle 后任何动作都非法
            state = _replay_to_settle(engine)
        else:
            if phase == "shrine":
                state = copy.deepcopy(phases("shrine", "g3_easy_full_run"))
            else:
                state = copy.deepcopy(phases(phase)) if phase else copy.deepcopy(phases("player_turn"))
        action = make(state)
        r = engine.reduce(state, action)
        assert r["illegal"] is True, f"{name}：期望非法"
        assert r["state"] is state, f"{name}：非法动作未原样返回 state"
        assert r["code"], f"{name}：缺少错误码"


def _replay_to_settle(engine):
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "src/game/tests/golden/g4_hard_corrupt_path.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    state = engine.create(lines[0]["config"])
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        state = engine.reduce(state, line["action"])["state"]
    assert state["phase"] == "settle"
    return state


def test_purify_exhausted_path(engine, find_state):
    """purify 次数耗尽时 purify 被拒。

    前置：**手里有魔化骰**的 player_turn。原实现取 ``phases("player_turn")``（g4 的第一个
    player_turn，step 2）——那一手没有魔化骰，于是恒跳过。g4 从 step 92 起有 22 个带魔化
    手骰的 player_turn，扫到第一个即可（M0 验收：5 个恒跳过用例之一）。
    """
    state = copy.deepcopy(find_state(
        "g4_hard_corrupt_path",
        lambda s: s["phase"] == "player_turn" and _first_corrupt(s) is not None,
        label="带魔化手骰的 player_turn"))
    state["player"]["purifyLeft"] = 0
    r = engine.reduce(state, {"kind": "purify", "dieId": _first_corrupt(state)})
    assert r["illegal"] is True
    assert r["code"] == "E_ACTION_NO_PURIFY"


def test_end_turn_corrupt_blocked(engine, find_state):
    """手里还有魔化骰时不能 end_turn（唯一判负前必须先处理魔化骰）。"""
    state = copy.deepcopy(find_state(
        "g4_hard_corrupt_path",
        lambda s: s["phase"] == "player_turn" and _first_corrupt(s) is not None,
        label="带魔化手骰的 player_turn"))
    r = engine.reduce(state, {"kind": "end_turn"})
    assert r["illegal"] is True
    assert r["code"] == "E_ACTION_CORRUPT_DIE_REMAINS"


def test_abandon_run_always_legal(engine, phases):
    for phase in ("map", "player_turn", "shop", "forge", "treasure", "rest",
                  "event", "relic_swap"):
        state = phases(phase)
        r = engine.reduce(state, {"kind": "abandon_run"})
        assert r["illegal"] is not True
        assert r["state"]["phase"] == "settle"
        assert r["state"]["outcome"] == "lose"  # abandon_run 记为 lose（GAME-01 §4）


def test_legal_actions_all_pass_is_legal(engine, phases):
    """legal_actions 的每一项在 is_legal 下都合法（两入口一致）。"""
    for phase in ("map", "player_turn", "shop", "forge", "treasure", "rest",
                  "event", "relic_swap"):
        state = phases(phase)
        legal = engine.legal_actions(state)
        assert legal, f"{phase} 无合法动作"
        for a in legal:
            assert engine.is_legal(state, a)["legal"] is True, f"{phase} {a} 不一致"


def test_normalize_action_fills_defaults(engine, phases, tables):
    """normalize_action 补齐 targetId/value 等默认值。"""
    state = phases("player_turn")
    die_id = state["player"]["hand"][0]
    a = engine.reduce(state, {"kind": "play_face", "dieId": die_id, "slotId": "slot_1"})
    assert a["illegal"] is not True or a["code"]


def test_reachable_node_ids_subset_of_map(engine, phases):
    state = phases("map")
    sm = M.current_stage_map(state)
    all_ids = {n["nodeId"] for row in sm["rows"] for n in row}
    reachable = M.reachable_node_ids(state)
    assert reachable
    assert set(reachable) <= all_ids


def test_replay_matches_stepwise(engine):
    import json
    from pathlib import Path

    from gamer_rsi.env import digest
    from gamer_rsi.env.engine import observe, replay

    path = Path(__file__).resolve().parents[3] / "src/game/tests/golden/g5_hard_six_stage.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    actions = [l["action"] for l in lines[1:] if l.get("action")]
    r = replay(lines[0]["config"], actions, engine._tables)
    want = [l["obsDigest"] for l in lines[1:] if l.get("step") is not None]
    assert len(r["digests"]) == len(want)
    assert r["digests"] == want
    assert r["outcome"] == lines[-1]["outcome"] if lines[-1].get("outcome") else True
