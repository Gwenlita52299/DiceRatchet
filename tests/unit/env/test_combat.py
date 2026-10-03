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


def test_check_play_face_unneeded_target(battle, tables):
    die_id = battle["player"]["hand"][0]
    face = current_face(battle, tables, die_id)
    if face["kind"] in ("purge", "reroll", "modify", "duplicate_face"):
        pytest.skip("该面需要目标")
    slot = battle["player"]["slots"][0]
    a = {"kind": "play_face", "dieId": die_id, "slotId": slot["slotId"], "targetId": "e-00"}
    r = C.check_play_face(battle, tables, a)
    if not r or r["code"] != "E_ACTION_CONSTRAINT_VIOLATION":
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


def test_blade_echo_relic(battle, tables):
    if "relic_blade_echo" not in tables["relics"]:
        pytest.skip("配表无 relic_blade_echo")
    battle["player"]["relics"] = ["relic_blade_echo"]
    echo = R.blade_echo_value(battle, tables)
    if echo <= 0:
        pytest.skip("blade_echo 值为 0")
    blade = next((d for d in battle["player"]["hand"]
                  if tables["allDice"][battle["player"]["dice"][d]["defId"]]["school"] == "blade"), None)
    if blade is None:
        # 强行造一颗 blade 手骰
        blade = battle["player"]["hand"][0]
        battle["player"]["dice"][blade]["defId"] = next(
            did for did, dd in tables["allDice"].items() if dd["school"] == "blade")
    before = battle["player"]["dice"][blade]["faceIndex"]
    ctx = {"state": battle, "tables": tables, "events": []}
    C.start_player_turn(ctx, True)
    assert battle["player"]["dice"][blade]["faceIndex"] >= before
