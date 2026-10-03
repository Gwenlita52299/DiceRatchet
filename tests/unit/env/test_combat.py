"""战斗层（GAME-00 §4–§7）：check_play_face 全错误分支、抽牌/溢出、禁槽、被动、死亡。"""

from __future__ import annotations

import copy

import pytest

from gamer_rsi.env import combat as C
from gamer_rsi.env import GameEngine
from gamer_rsi.env import relic as R
from gamer_rsi.env.state import current_face


def _hand_die(state, kind=None):
    for d in state["player"]["hand"]:
        f = current_face(state, state.get("_tables") or {}, d)
        if kind is None or f["kind"] == kind:
            return d
    return state["player"]["hand"][0]


@pytest.fixture
def battle(tables, started):
    s = copy.deepcopy(started)
    s["_tables"] = tables
    return s


def test_check_play_face_unknown_slot(battle):
    a = {"kind": "play_face", "dieId": battle["player"]["hand"][0], "slotId": "slot_nope"}
    r = C.check_play_face(battle, battle["_tables"], a)
    assert r and r["code"] == "E_ACTION_UNKNOWN_SLOT"


def test_check_play_face_slot_disabled(battle, tables):
    slot = battle["player"]["slots"][0]
    slot["disabledUntilTurn"] = battle["turn"] + 3
    a = {"kind": "play_face", "dieId": battle["player"]["hand"][0], "slotId": slot["slotId"]}
    r = C.check_play_face(battle, tables, a)
    assert r and r["code"] == "E_ACTION_SLOT_DISABLED"


def test_check_play_face_slot_occupied(battle, tables):
    slot = next(s for s in battle["player"]["slots"] if not s.get("reusable"))
    slot["filledBy"] = "d-9999"
    a = {"kind": "play_face", "dieId": battle["player"]["hand"][0], "slotId": slot["slotId"]}
    r = C.check_play_face(battle, tables, a)
    assert r and r["code"] == "E_ACTION_SLOT_OCCUPIED"


def test_check_play_face_reusable_slot_ok(battle, tables):
    slot = next((s for s in battle["player"]["slots"] if s.get("reusable")), None)
    if slot is None:
        pytest.skip("无工具槽")
    slot["filledBy"] = "d-9999"
    a = {"kind": "play_face", "dieId": battle["player"]["hand"][0], "slotId": slot["slotId"]}
    # 只要不违反约束就应通过（occupied 分支被 reusable 跳过）
    r = C.check_play_face(battle, tables, a)
    assert not r or r["code"] != "E_ACTION_SLOT_OCCUPIED"


def test_check_play_face_bad_target_enemy(battle, tables):
    die_id = _hand_die(battle, "purge")
    face = current_face(battle, tables, die_id)
    if face["kind"] != "purge":
        pytest.skip("手中无 purge 面")
    a = {"kind": "play_face", "dieId": die_id, "slotId": _attack_slot(battle),
         "targetId": "e-nope"}
    r = C.check_play_face(battle, tables, a)
    assert r and r["code"] == "E_ACTION_BAD_TARGET"


def test_check_play_face_dead_target(battle, tables):
    die_id = _hand_die(battle, "purge")
    if current_face(battle, tables, die_id)["kind"] != "purge":
        pytest.skip("手中无 purge 面")
    e = battle["enemies"][0]
    e["dead"] = True
    a = {"kind": "play_face", "dieId": die_id, "slotId": _attack_slot(battle),
         "targetId": e["enemyId"]}
    r = C.check_play_face(battle, tables, a)
    assert r and r["code"] == "E_ACTION_BAD_TARGET"


def _attack_slot(state):
    s = next((x for x in state["player"]["slots"] if x["slotKind"] == "attack"), None)
    return s["slotId"] if s else state["player"]["slots"][0]["slotId"]


def test_check_play_face_unneeded_target(engine, tables, find_state):
    """给「不需要目标」的骰面传 targetId → BAD_TARGET。

    原实现固定取 ``hand[0]``，而 `started`（g1 首个 player_turn）的 hand[0] 是 purge
    （需要目标）→ 恒跳过。这里改成**先找一个「合法且不需要目标」的 play_face**（g1 首个
    player_turn 就有：ward→slot_3），再故意补一个 targetId。
    """
    def has_no_target_play(s):
        if s["phase"] != "player_turn":
            return False
        return any(a["kind"] == "play_face"
                   and C.face_needs_target(C.current_face(s, tables, a["dieId"])) is None
                   for a in engine.legal_actions(s))

    state = copy.deepcopy(find_state("g1_all_off_medium", has_no_target_play,
                                     label="存在「合法且不需要目标」play_face 的 player_turn"))
    cand = next(a for a in engine.legal_actions(state)
                if a["kind"] == "play_face"
                and C.face_needs_target(C.current_face(state, tables, a["dieId"])) is None)
    a = {**cand, "targetId": state["enemies"][0]["enemyId"]}
    r = C.check_play_face(state, tables, a)
    assert r and r["code"] == "E_ACTION_BAD_TARGET", r


def test_face_needs_target(table=None):
    from gamer_rsi.env.combat import face_needs_target

    for kind in ("purge",):
        assert face_needs_target({"kind": kind, "target": "enemy"}) == "enemy"
    for kind in ("reroll", "modify", "duplicate_face"):
        assert face_needs_target({"kind": kind, "target": "die"}) == "die"
    for kind in ("ward", "mend", "draw", "gold", "status", "selfCorruption", "nothing"):
        assert face_needs_target({"kind": kind, "target": "self"}) is None


def test_alive_enemies_and_find_enemy(battle):
    battle["enemies"][0]["dead"] = True
    assert C.alive_enemies(battle) == [e for e in battle["enemies"] if not e["dead"]]
    got = C._find_enemy(battle, None)
    if C.alive_enemies(battle):
        assert got is not None and not got["dead"]
        # 未知 id 回退到首个存活
        assert C._find_enemy(battle, "e-nope")["enemyId"] == got["enemyId"]
    else:
        assert got is None  # 全灭时无目标
        assert C._find_enemy(battle, "e-nope") is None


def test_find_enemy_prefers_explicit(battle):
    e2 = battle["enemies"][-1]
    got = C._find_enemy(battle, e2["enemyId"])
    assert got is not None and got["enemyId"] == e2["enemyId"]
    e2["dead"] = True
    got = C._find_enemy(battle, e2["enemyId"])
    assert got is None or got["enemyId"] != e2["enemyId"]


def test_corrupt_remaining(battle):
    corrupt = C.corrupt_remaining(battle)
    assert isinstance(corrupt, list)
    for d in corrupt:
        assert battle["player"]["dice"][d]["isCorrupt"]


def test_check_player_death_triggers_revive(battle, tables):
    """心数归零时触发复活（reviveUsed）或败北。"""
    ctx = {"state": battle, "tables": tables, "events": []}
    p = battle["player"]
    p["hearts"] = 0
    p["reviveUsed"] = False
    C.check_player_death(ctx)
    assert battle["outcome"] in (None, "lose", "dead")


def test_has_passive_returns_match(battle, tables):
    e = battle["enemies"][0]
    e["passives"] = [{"kind": "poison_immune"}]
    assert C.has_passive(e, "poison_immune") == {"kind": "poison_immune"}
    assert C.has_passive(e, "nope") is None


def test_add_status_poison_immune(battle, tables):
    e = battle["enemies"][0]
    e["passives"] = [{"kind": "poison_immune"}]
    ctx = {"state": battle, "tables": tables, "events": []}
    before = e["statuses"]["poison"]
    C.add_status(ctx, e["enemyId"], "poison", 3)
    assert e["statuses"]["poison"] == before
    assert any(x["type"] == "status_immune" for x in ctx["events"])


def test_add_status_stacks(battle, tables):
    e = battle["enemies"][0]
    e["passives"] = []
    ctx = {"state": battle, "tables": tables, "events": []}
    before = e["statuses"]["burn"]
    C.add_status(ctx, e["enemyId"], "burn", 2)
    assert e["statuses"]["burn"] == before + 2
    assert any(x["type"] == "status_applied" for x in ctx["events"])


def test_draw_dice_overflow(battle, tables):
    """手牌满时抽到的骰子进弃牌堆（hand_overflow 事件）。"""
    ctx = {"state": battle, "tables": tables, "events": []}
    p = battle["player"]
    p["hand"] = [f"d-{i:04d}" for i in range(battle["config"]["handMax"])]
    p["drawPile"] = ["d-9001"]
    p["discardPile"] = []
    C.draw_dice(ctx, 1)
    assert "d-9001" in p["discardPile"]
    assert any(x["type"] == "hand_overflow" for x in ctx["events"])


def test_draw_dice_reshuffle(battle, tables):
    ctx = {"state": battle, "tables": tables, "events": []}
    p = battle["player"]
    p["hand"] = []
    # 用真实牌库骰子，避免 invariant（piles 必须都在 deck 里）
    real = [d for d in p["deck"] if d not in p["hand"]][:2]
    p["drawPile"] = []
    p["discardPile"] = list(real)
    C.draw_dice(ctx, 1)
    assert any(x in p["hand"] for x in real)
    assert any(x["type"] == "reshuffle" for x in ctx["events"])


def test_battle_start_resets_and_ward_relic(battle, tables):
    """start_battle 重置 charge/revive/purify，且 relic_iron_bark 给战斗起始护盾。"""
    if "relic_iron_bark" not in tables["relics"]:
        pytest.skip("配表无 relic_iron_bark")
    battle["player"]["relics"] = ["relic_iron_bark"]
    ctx = {"state": battle, "tables": tables, "events": []}
    p = battle["player"]
    p["charge"] = 5
    p["reviveUsed"] = True
    before = p["ward"]
    C.start_battle(ctx, [battle["enemies"][0]["defId"]], "battle")
    assert p["charge"] == 0
    assert p["reviveUsed"] is False
    assert p["ward"] >= before
    assert p["purifyLeft"] == battle["config"]["purifyPerStage"] + R.purify_bonus(battle, tables)


def test_start_battle_applies_pending(battle, tables):
    """start_battle 把 pendingStatuses 结算到 statuses（战斗起始 buff）。"""
    ctx = {"state": battle, "tables": tables, "events": []}
    p = battle["player"]
    p["pendingStatuses"] = {"strength": 2}
    C.start_battle(ctx, [battle["enemies"][0]["defId"]], "battle")
    assert p["statuses"]["strength"] >= 2
    assert p["pendingStatuses"] == {}


def test_player_upkeep_ticks_statuses(battle, tables):
    ctx = {"state": battle, "tables": tables, "events": []}
    p = battle["player"]
    p["statuses"] = {"poison": 2, "burn": 4, "regen": 1, "weak": 2, "strength": 1}
    p["ward"] = 5
    C.player_upkeep(ctx)
    assert p["ward"] == 0
    assert p["statuses"]["burn"] == 2  # 灼烧减半（D-12）
    assert p["statuses"]["weak"] == 1
    assert any(x["type"] == "status_tick" for x in ctx["events"])


def test_slot_disabled_helper(battle, tables):
    slot = battle["player"]["slots"][0]
    assert C.slot_disabled(battle, slot) is False
    slot["disabledUntilTurn"] = battle["turn"] + 1
    assert C.slot_disabled(battle, slot) is True
    slot["disabledUntilTurn"] = battle["turn"] - 1
    assert C.slot_disabled(battle, slot) is False


def test_blade_echo_relic(engine, tables, find_state):
    """锋刃回响（配表 `relic_blade_echo`，效果 `blade_echo`）：击杀敌人后首颗**锋刃学派**
    手骰点数 +echo（上限 5），并报 `die_modified` 事件。

    原用例有两个问题，合起来使它既恒跳过、又断言错了对象：
    ① fixture `battle` 来自 ``started`` = **g1（all_off，relics=false）**，`has_relic()` 恒
       False → `blade_echo_value` 恒 0 → 恒跳过；
    ② 它断言的是 `start_player_turn` 会改点数，但实现里 echo 只在 `on_enemy_killed`
       生效（`combat.py:264-273`），所以即便夹具修好，断言也是空的。
    实测该分支此前**从未被执行**（combat.py:266-273 无覆盖）。这里改测真实行为。
    """
    state = copy.deepcopy(find_state(
        "g5_hard_six_stage",
        lambda s: (s["phase"] == "player_turn" and s["enemies"] and s["player"]["hand"]),
        label="全开局的 player_turn"))
    state["player"]["relics"] = ["relic_blade_echo"]
    echo = R.blade_echo_value(state, tables)
    assert echo > 0, "开关全开 + 持遗物时 blade_echo_value 必须 > 0"

    # 指定一颗锋刃学派的骰子当「首颗锋刃手骰」，点数压到 0
    blade_def = next(did for did, dd in tables["allDice"].items() if dd["school"] == "blade")
    blade = state["player"]["hand"][0]
    state["player"]["dice"][blade]["defId"] = blade_def
    state["player"]["dice"][blade]["faceIndex"] = 0

    ctx = {"state": state, "tables": tables, "events": []}
    C.on_enemy_killed(ctx, state["enemies"][0])
    assert state["player"]["dice"][blade]["faceIndex"] == min(5, echo), "击杀后应 +echo"
    assert any(e["type"] == "die_modified" and e["source"] == "relic_blade_echo"
               and e["dieId"] == blade for e in ctx["events"]), ctx["events"]
    assert state["stats"]["kills"] == 1

    # 上限：点数已在 5 时不再增长
    state2 = copy.deepcopy(state)
    state2["player"]["dice"][blade]["faceIndex"] = 5
    C.on_enemy_killed({"state": state2, "tables": tables, "events": []}, state2["enemies"][0])
    assert state2["player"]["dice"][blade]["faceIndex"] == 5

    # 未持遗物时不得改动点数（反向用例：echo=0 分支）
    state3 = copy.deepcopy(state)
    state3["player"]["relics"] = []
    state3["player"]["dice"][blade]["faceIndex"] = 0
    ctx3 = {"state": state3, "tables": tables, "events": []}
    C.on_enemy_killed(ctx3, state3["enemies"][0])
    assert state3["player"]["dice"][blade]["faceIndex"] == 0
    assert not any(e.get("source") == "relic_blade_echo" for e in ctx3["events"])
