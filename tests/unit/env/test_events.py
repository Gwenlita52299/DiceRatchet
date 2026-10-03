"""叙事事件（GAME-00 §8.4）：事件池、要求、效果执行（apply_ops 全 op）、结算。"""

from __future__ import annotations

import copy

import pytest

from gamer_rsi.env import GameEngine, load_tables
from gamer_rsi.env import event as EV


@pytest.fixture
def event_state(phases):
    """event phase 的真实 state（g4 全开关 hard）。"""
    return copy.deepcopy(phases("event"))


@pytest.fixture
def ctx(event_state, tables):
    return {"state": event_state, "tables": tables, "events": []}


def test_event_pool_respects_toggle(tables):
    state = {"config": {"toggles": {"events": False, "relics": True}}, "stage": 1, "seenEvents": []}
    assert EV.event_pool(state, tables) == []


def test_event_pool_filters_by_stage_and_seen(tables, event_state):
    on = dict(event_state["config"]["toggles"], events=True)
    state = {"config": {"toggles": on}, "stage": 1, "seenEvents": []}
    pool = EV.event_pool(state, tables)
    assert pool == sorted(pool)
    assert pool, "stage 1 应有可用事件"
    # 全程范围内，见过的事件不再出现
    state2 = {"config": {"toggles": on}, "stage": 1, "seenEvents": list(pool)}
    assert EV.event_pool(state2, tables) == []


def test_event_pool_relics_only(tables, event_state):
    on = dict(event_state["config"]["toggles"], events=True)
    state = {"config": {"toggles": on}, "stage": 1, "seenEvents": []}
    with_relics = EV.event_pool(state, tables)
    state["config"]["toggles"]["relics"] = False
    without = EV.event_pool(state, tables)
    assert set(without) <= set(with_relics)
    dropped = set(with_relics) - set(without)
    for eid in dropped:
        assert tables["events"][eid].get("relicsOnly")


@pytest.mark.parametrize("req,want", [
    (None, True),
    ({}, True),
    ({"gold": 10}, True),
    ({"gold": 10000}, False),
    ({"hearts": 1}, True),
    ({"hearts": 99}, False),
    ({"gold": 10, "hearts": 99}, False),
])
def test_requirement_met(req, want):
    state = {"player": {"starShards": 50, "hearts": 3}}
    assert EV.requirement_met(state, req) is want


@pytest.mark.parametrize("req,want", [
    (None, None),
    ({"gold": 10}, "需要 10 星屑"),
    ({"hearts": 2}, "需要 ≥2 心"),
    ({"gold": 10, "hearts": 2}, "需要 10 星屑，需要 ≥2 心"),
])
def test_requirement_text(req, want):
    assert EV.requirement_text(req) == want


def test_chapter_boss_school(event_state, tables):
    school = EV.chapter_boss_school(event_state, tables)
    assert school in ("blade", "arcana", "verdant")


def test_non_cursed_dice(event_state, tables):
    dice = EV.non_cursed_dice(event_state, tables)
    assert dice
    for did in dice:
        assert tables["allDice"][event_state["player"]["dice"][did]["defId"]]["category"] != "cursed"


def test_pick_random_skill_is_in_pool(event_state, tables):
    for _ in range(12):
        sid = EV.pick_random_skill(event_state, tables, "blade")
        assert tables["skills"][sid]["school"] == "blade"
    sid = EV.pick_random_skill(event_state, tables, None)
    assert sid in tables["skills"]


def _op(ctx, **kw):
    ctx["events"].clear()
    before = ctx["state"]["rng"][-1]["counter"]
    nxt = EV.apply_ops(ctx, [{"op": kw.pop("op"), **kw}])
    return nxt, ctx["events"], before


def test_op_gain_gold(ctx):
    nxt, ev, _ = _op(ctx, op="gain_gold", value=10)
    assert nxt is None
    assert ctx["state"]["player"]["starShards"] >= 10
    assert ev[0]["type"] == "gold"


def test_op_lose_gold(ctx):
    ctx["state"]["player"]["starShards"] = 5
    nxt, ev, _ = _op(ctx, op="lose_gold", value=100)
    assert ctx["state"]["player"]["starShards"] == 0
    assert ev[0] == {"type": "gold", "value": -100}


def test_op_lose_virtue(ctx):
    nxt, ev, _ = _op(ctx, op="lose_virtue", value=2)
    assert ctx["state"]["player"]["pendingVirtue"] == -2
    assert ev[0] == {"type": "virtue_penalty", "value": 2}


def test_op_heal_virtue(ctx):
    ctx["state"]["player"]["pendingVirtue"] = -3
    nxt, ev, _ = _op(ctx, op="heal_virtue", value=4)
    # JS 语义：先 max(0, ...) 清掉负向扣减，再加
    assert ctx["state"]["player"]["pendingVirtue"] == 4
    assert ev[0] == {"type": "virtue_bonus", "value": 4}


def test_op_gain_die_explicit(ctx):
    n = len(ctx["state"]["player"]["deck"])
    nxt, ev, _ = _op(ctx, op="gain_die", defId="skill_blade_slash")
    assert len(ctx["state"]["player"]["deck"]) == n + 1
    assert ev[0]["defId"] == "skill_blade_slash"


def test_op_gain_die_count(ctx):
    n = len(ctx["state"]["player"]["deck"])
    _op(ctx, op="gain_die", defId="skill_blade_slash", count=3)
    assert len(ctx["state"]["player"]["deck"]) == n + 3


def test_op_gain_die_random_school(ctx):
    nxt, ev, _ = _op(ctx, op="gain_die", school="arcana")
    assert tables_ref(ctx)["skills"][ev[0]["defId"]]["school"] == "arcana"


def tables_ref(ctx):
    return ctx["tables"]


def test_op_gain_die_counter_boss(ctx):
    nxt, ev, _ = _op(ctx, op="gain_die_counter_boss")
    from gamer_rsi.env.counter import counter_of
    assert tables_ref(ctx)["skills"][ev[0]["defId"]]["school"] == \
        counter_of(EV.chapter_boss_school(ctx["state"], ctx["tables"]))


def test_op_gain_die_random(ctx):
    nxt, ev, _ = _op(ctx, op="gain_die_random")
    assert ev[0]["defId"] in tables_ref(ctx)["skills"]


def test_op_remove_die(ctx):
    deck = ctx["state"]["player"]["deck"]
    _op(ctx, op="remove_die")
    # 不做强断言（依赖 deck 长度 > 5 的内部条件），主要覆盖分支


def test_op_remove_die_blocked_when_small(ctx):
    p = ctx["state"]["player"]
    keep = [d for d in p["deck"] if
            tables_ref(ctx)["allDice"][p["dice"][d]["defId"]]["category"] != "cursed"]
    for d in list(p["deck"]):
        if d not in keep[:5]:
            p["deck"].remove(d)
    n = len(p["deck"])
    _op(ctx, op="remove_die")
    assert len(p["deck"]) == n  # deck <= 5 时不删


def test_op_gain_relic(ctx):
    ctx["state"]["config"]["toggles"]["relics"] = True
    nxt, ev, _ = _op(ctx, op="gain_relic")
    assert nxt in (None, "relic_swap")
    if nxt == "relic_swap":
        assert ctx["state"]["phase"] == "relic_swap"


def test_op_gain_relic_disabled(ctx):
    ctx["state"]["config"]["toggles"]["relics"] = False
    _op(ctx, op="gain_relic")
    assert not any(e["type"] == "relic_gained" for e in ctx["events"])


def test_op_gain_heart(ctx):
    ctx["state"]["player"]["hearts"] = 1
    nxt, ev, _ = _op(ctx, op="gain_heart", value=5)
    assert ctx["state"]["player"]["hearts"] == min(5, ctx["state"]["player"]["heartsMax"])
    assert ev[0]["type"] == "heart_gained"


def test_op_lose_heart(ctx):
    h = ctx["state"]["player"]["hearts"]
    nxt, ev, _ = _op(ctx, op="lose_heart", value=1)
    assert ctx["state"]["player"]["hearts"] == h - 1
    assert ev[0]["type"] == "player_lost_heart"


def test_op_add_curse(ctx):
    n = len(ctx["state"]["player"]["deck"])
    nxt, ev, _ = _op(ctx, op="add_curse", defId="die_curse_static")
    assert len(ctx["state"]["player"]["deck"]) == n + 1
    assert ev[0]["type"] == "curse_added"


def test_op_remove_curses(ctx):
    from gamer_rsi.env.state import add_die_to_deck
    add_die_to_deck(ctx["state"], "die_curse_static")
    _op(ctx, op="remove_curses")
    for did in ctx["state"]["player"]["deck"]:
        assert tables_ref(ctx)["allDice"][ctx["state"]["player"]["dice"][did]["defId"]]["category"] != "cursed"


def test_op_modify_face(ctx):
    _op(ctx, op="modify_face", faceId=next(iter(ctx["tables"]["forgeFaces"])))
    assert any(e["type"] == "die_forged" for e in ctx["events"])


def test_op_duplicate_die(ctx):
    n = len(ctx["state"]["player"]["deck"])
    _op(ctx, op="duplicate_die")
    assert len(ctx["state"]["player"]["deck"]) == n + 1


def test_op_gain_status(ctx):
    _op(ctx, op="gain_status", statusKind="strength", value=2)
    assert ctx["state"]["player"]["pendingStatuses"]["strength"] == 2


def test_op_extra_draw(ctx):
    _op(ctx, op="extra_draw", value=1)
    assert ctx["state"]["player"]["pendingDraw"] >= 1


def test_op_add_slot(ctx):
    n = len(ctx["state"]["player"]["slots"])
    _op(ctx, op="add_slot")
    assert len(ctx["state"]["player"]["slots"]) == n + 1
    assert ctx["state"]["player"]["slots"][-1]["slotKind"] == "attack"


def test_op_goto_forge(ctx):
    nxt, ev, _ = _op(ctx, op="goto_forge")
    assert nxt == "forge"


def test_apply_ops_consumes_no_rng_when_deterministic(ctx):
    before = [r["counter"] for r in ctx["state"]["rng"]]
    EV.apply_ops(ctx, [{"op": "gain_gold", "value": 5}, {"op": "lose_virtue", "value": 1}])
    assert [r["counter"] for r in ctx["state"]["rng"]] == before


def test_event_choose_resolves(ctx):
    ev_state = ctx["state"]
    ev_id = ev_state["node"]["event"]["eventId"]
    options = ctx["tables"]["events"][ev_id]["options"]
    opt_id = next(f"opt_{i}" for i, o in enumerate(options)
                  if EV.requirement_met(ev_state, o.get("requirement")))
    EV.event_choose(ctx, opt_id)
    assert ev_state["node"]["event"]["resolved"] is True
    assert ev_state["node"]["event"]["chosen"] == opt_id
    assert any(e["type"] == "event_result" for e in ctx["events"])


def test_event_choose_lose_ends_run(ctx, tables):
    """事件里丢掉最后一颗心 → 立刻判负（`outcome="lose"`、`phase="settle"`）。

    原实现在夹具事件（`ev_corrupt_spring`）的选项里找扣血 outcome —— 该事件只有
    add_curse/gain_heart/lose_gold/remove_curses，所以恒跳过。这里显式挑一个
    **单个 outcome 且含 lose_heart** 的选项：`event_choose` 只按 `state.node.event.eventId`
    查配表（options 不缓存），而单 outcome 时不会走 weighted 抽签 → 结果确定。
    """
    ctx["state"]["player"]["hearts"] = 1
    pick = next(((ev_id, i) for ev_id, ev in sorted(tables["events"].items())
                 for i, o in enumerate(ev["options"])
                 if len(o["outcomes"]) == 1
                 and any(e["op"] == "lose_heart" and e.get("value", 0) >= 1
                         for e in o["outcomes"][0]["effect"])), None)
    assert pick is not None, "配表里没有「单 outcome 且含 lose_heart」的事件选项（契约变更？）"
    ev_id, idx = pick
    ctx["state"]["node"]["event"] = {"eventId": ev_id, "resolved": False,
                                    "resultText": None, "chosen": None}
    EV.event_choose(ctx, f"opt_{idx}")
    assert ctx["state"]["outcome"] == "lose"
    assert ctx["state"]["phase"] == "settle"
