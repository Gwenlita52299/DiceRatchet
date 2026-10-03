"""商店/锻造/神殿/休息点（GAME-00 §8.1/§8.2）：价格、刷新、删除、选项结算。

注意 ``reduce`` 是写时拷贝：断言必须落在返回的新 state 上。
"""

from __future__ import annotations

import copy

import pytest

from gamer_rsi.env import GameEngine
from gamer_rsi.env import relic as R
from gamer_rsi.env import shop as S
from gamer_rsi.env import forge as F
from gamer_rsi.env.state import add_die_to_deck, remove_die_from_deck


@pytest.fixture
def shop_state(phases):
    return copy.deepcopy(phases("shop"))


def test_shop_items_have_prices(shop_state, tables):
    items = shop_state["node"]["shop"]["items"]
    assert items
    for it in items:
        assert it["price"] >= 0
        assert it["kind"] in ("die", "relic")


def test_refresh_price_pure(shop_state, tables):
    p = S.refresh_price(shop_state, tables)
    assert p >= 0
    assert S.refresh_price(shop_state, tables) == p


def test_remove_price_scales_with_count(shop_state, tables):
    base = S.remove_price(shop_state, tables)
    shop_state["player"]["removeCount"] += 1
    assert S.remove_price(shop_state, tables) >= base


def test_removable_dice_rules(shop_state, tables):
    removable = S.removable_dice(shop_state, tables)
    deck = shop_state["player"]["deck"]
    if len(deck) <= 5:
        assert removable == []
    else:
        assert removable
        for did in removable:
            assert did in deck


def test_shop_refresh_consumes(engine, tables, shop_state):
    state = shop_state
    shards = state["player"]["starShards"]
    left = state["node"]["shop"]["refreshLeft"]
    if left <= 0 or shards < S.refresh_price(state, tables):
        pytest.skip("无刷新次数或星屑不足")
    r = engine.reduce(state, {"kind": "shop_refresh"})
    assert r["illegal"] is not True
    s2 = r["state"]
    assert s2["node"]["shop"]["refreshLeft"] == left - 1
    assert s2["player"]["starShards"] < shards
    assert any(e["type"] == "shop_refresh" for e in r["events"])


def test_shop_buy_and_remove(engine, tables, shop_state):
    state = shop_state
    items = state["node"]["shop"]["items"]
    die_item = next((it for it in items if it["kind"] == "die" and not it["sold"]
                     and state["player"]["starShards"] >= it["price"]), None)
    if die_item is None:
        pytest.skip("买得起的骰子商品")
    r = engine.reduce(state, {"kind": "shop_buy", "itemId": die_item["itemId"]})
    assert r["illegal"] is not True, r.get("code")
    assert any(e["type"] == "die_gained" for e in r["events"])

    s2 = r["state"]
    removable = S.removable_dice(s2, tables)
    if removable and s2["player"]["starShards"] >= S.remove_price(s2, tables):
        r2 = engine.reduce(s2, {"kind": "shop_remove", "dieId": removable[0]})
        assert r2["illegal"] is not True, r2.get("code")
        assert removable[0] not in r2["state"]["player"]["deck"]


def test_shop_leave(engine, shop_state):
    r = engine.reduce(shop_state, {"kind": "shop_leave"})
    assert r["illegal"] is not True
    assert r["state"]["phase"] == "map"
    assert r["state"]["node"] is None


def test_shrine_options(engine, tables, phases):
    state = copy.deepcopy(phases("shrine", "g3_easy_full_run"))
    options = state["node"]["shrine"]["options"]
    assert {o["optionId"] for o in options} >= {"heart", "gold"}
    heart = next(o for o in options if o["optionId"] == "heart")
    assert heart["available"] == (state["player"]["hearts"] < state["player"]["heartsMax"])

    for o in options:
        if not o["available"]:
            r = engine.reduce(state, {"kind": "shrine_choose", "optionId": o["optionId"]})
            assert r["illegal"] is True
            continue
        r = engine.reduce(state, {"kind": "shrine_choose", "optionId": o["optionId"]})
        assert r["illegal"] is not True, f"{o['optionId']}：{r.get('code')}"
        node = r["state"]["node"]
        assert node is None or node["shrine"] is None
        assert r["state"]["phase"] != "shrine"
        return
    pytest.skip("所有神殿选项均不可用")


def test_rest_options(engine, phases):
    state = copy.deepcopy(phases("rest"))
    options = state["node"]["rest"]["options"]
    assert options
    for o in options:
        r = engine.reduce(state, {"kind": "rest_choose", "optionId": o["optionId"]})
        if o["available"]:
            assert r["illegal"] is not True, f"{o['optionId']}：{r.get('code')}"
            assert r["state"]["phase"] != "rest"
            return
    pytest.skip("所有休息选项均不可用")


def test_forge_flow(engine, tables, phases):
    state = copy.deepcopy(phases("forge"))
    f = state["node"]["forge"]
    assert f["candidates"]
    die_id = F.forgeable_dice(state, tables)[0]
    if state["player"]["starShards"] < f["price"]:
        state["player"]["starShards"] = f["price"] + 10
    before = state["player"]["starShards"]
    r = engine.reduce(state, {"kind": "forge_pick", "dieId": die_id, "faceIndex": 0,
                              "candidateId": f["candidates"][0]})
    assert r["illegal"] is not True, r.get("code")
    s2 = r["state"]
    assert s2["node"]["forge"]["done"] is True
    assert s2["player"]["starShards"] == before - f["price"]
    assert any(e["type"] == "die_forged" for e in r["events"])


def test_forge_done_blocks_second(engine, tables, phases):
    state = copy.deepcopy(phases("forge"))
    state["node"]["forge"]["price"] = 0
    die_id = F.forgeable_dice(state, tables)[0]
    a = {"kind": "forge_pick", "dieId": die_id, "faceIndex": 0,
         "candidateId": state["node"]["forge"]["candidates"][0]}
    r1 = engine.reduce(state, a)
    assert r1["illegal"] is not True
    r2 = engine.reduce(r1["state"], a)
    assert r2["illegal"] is True
    assert r2["code"] == "E_ACTION_FORGE_DONE"


def test_forge_leave_via_shop_leave(engine, phases):
    state = copy.deepcopy(phases("forge"))
    r = engine.reduce(state, {"kind": "shop_leave"})
    assert r["illegal"] is not True
    assert r["state"]["phase"] == "map"


def test_treasure_pick(engine, phases):
    state = copy.deepcopy(phases("treasure"))
    offers = state["node"]["treasure"]["offers"]
    r = engine.reduce(state, {"kind": "treasure_pick", "defId": offers[0]})
    assert r["illegal"] is not True, r.get("code")
    assert any(e["type"] == "die_gained" for e in r["events"])


def test_treasure_skip(engine, phases):
    state = copy.deepcopy(phases("treasure"))
    r = engine.reduce(state, {"kind": "treasure_pick", "defId": "skip"})
    assert r["illegal"] is not True
    assert not any(e["type"] == "die_gained" for e in r["events"])


def test_add_die_to_deck_and_remove(tables, started):
    state = copy.deepcopy(started)
    n = len(state["player"]["deck"])
    did = add_die_to_deck(state, "skill_blade_slash")
    assert len(state["player"]["deck"]) == n + 1
    assert did in state["player"]["dice"]
    assert did not in state["player"]["hand"]  # 新骰子进牌库不入手
    remove_die_from_deck(state, did)
    assert len(state["player"]["deck"]) == n
    assert did not in state["player"]["dice"]


def test_add_die_generates_unique_ids(tables, started):
    state = copy.deepcopy(started)
    ids = [add_die_to_deck(state, "skill_blade_slash") for _ in range(5)]
    assert len(set(ids)) == 5
    assert all(i.startswith("d-") and len(i) == len("d-0000") for i in ids)
