"""``kernel/shop.js`` 的逐位移植 —— 商店、宝箱、神殿、休息（GAME-00 §8.5），消费 shop / loot 流。"""

from __future__ import annotations

from typing import Any, Mapping

from . import relic as R
from .counter import SCHOOLS, apply_mul, counter_of
from .map import chapter_def, stage_def
from .rng import choice, randint, sample
from .state import (add_die_to_deck, deck_school_info, die_def, pad, remove_die_from_deck)

__all__ = ["stage_theme", "enter_shop", "remove_price", "refresh_price", "shop_buy",
           "shop_refresh", "shop_remove", "removable_dice", "enter_treasure", "treasure_pick",
           "enter_shrine", "shrine_choose", "enter_rest", "rest_choose"]

Ctx = dict[str, Any]


def stage_theme(state: Mapping, tables: Mapping) -> str:
    return chapter_def(tables, state["chapter"])["theme"]


def skills_of(tables: Mapping, school: str | None, safe_only: bool) -> list[str]:
    return sorted(
        sid for sid, d in tables["skills"].items()
        if d["school"] == school and (not safe_only or d["riskTier"] == "safe")
    )


def price(state: Mapping, lo: int, hi: int, stream: str) -> int:
    return apply_mul(randint(state, stream, lo, hi), state["config"]["shopPriceMulPermille"])


def gen_shop_items(ctx: Ctx) -> list[dict[str, Any]]:
    state, tables = ctx["state"], ctx["tables"]
    cfg = tables["shops"]
    counter = counter_of(stage_theme(state, tables))
    all_ids = sorted(tables["skills"].keys())
    items: list[dict[str, Any]] = []
    picked: set[str] = set()
    first = choice(state, "shop", skills_of(tables, counter, False))
    picked.add(first)
    items.append({"defId": first, "kind": "die"})
    bias = tables["difficulty"]["common"]["biasStrengthPermille"]
    for _ in range(1, cfg["shop_dice"]["count"]):
        biased = randint(state, "shop", 1, 1000) <= bias
        base = skills_of(tables, counter, False) if biased else all_ids
        pool = [sid for sid in base if sid not in picked]
        die_id = choice(state, "shop", pool if pool else [x for x in all_ids if x not in picked])
        picked.add(die_id)
        items.append({"defId": die_id, "kind": "die"})
    for it in items:
        it["price"] = price(state, cfg["shop_dice"]["priceMin"], cfg["shop_dice"]["priceMax"], "shop")
    if state["config"]["toggles"]["relics"]:
        pool = R.relic_pool(state, tables, ["common", "rare"])
        for rid in sample(state, "shop", pool, cfg["shop_relic"]["count"]):
            items.append({"defId": rid, "kind": "relic",
                          "price": price(state, cfg["shop_relic"]["priceMin"],
                                         cfg["shop_relic"]["priceMax"], "shop")})
    state["shopSeq"] += 1
    return [{**it, "itemId": f"s-{pad(state['shopSeq'], 3)}-{i}", "sold": False}
            for i, it in enumerate(items)]


def enter_shop(ctx: Ctx) -> None:
    state, tables = ctx["state"], ctx["tables"]
    state["node"]["shop"] = {"items": [], "refreshLeft": tables["shops"]["shop_dice"]["refreshMax"],
                             "freeRemoveUsed": False}
    state["node"]["shop"]["items"] = gen_shop_items(ctx)
    state["phase"] = "shop"


def remove_price(state: Mapping, tables: Mapping) -> int:
    if R.free_remove_available(state):
        return 0
    c = tables["shops"]["shop_remove"]
    return apply_mul(c["base"] + c["step"] * state["player"]["removeCount"],
                     state["config"]["shopPriceMulPermille"])


def refresh_price(state: Mapping, tables: Mapping) -> int:
    return apply_mul(tables["shops"]["shop_dice"]["refreshCost"],
                     state["config"]["shopPriceMulPermille"])


def shop_buy(ctx: Ctx, item_id: str) -> None:
    state, events = ctx["state"], ctx["events"]
    it = next(x for x in state["node"]["shop"]["items"] if x["itemId"] == item_id)
    state["player"]["starShards"] -= it["price"]
    it["sold"] = True
    if it["kind"] == "die":
        die_id = add_die_to_deck(state, it["defId"])
        events.append({"type": "die_gained", "dieId": die_id, "defId": it["defId"]})
    else:
        R.grant_relic(ctx, it["defId"], "shop")


def shop_refresh(ctx: Ctx) -> None:
    state, tables = ctx["state"], ctx["tables"]
    state["player"]["starShards"] -= refresh_price(state, tables)
    state["node"]["shop"]["refreshLeft"] -= 1
    state["node"]["shop"]["items"] = gen_shop_items(ctx)
    ctx["events"].append({"type": "shop_refresh"})


def shop_remove(ctx: Ctx, die_id: str) -> None:
    state, tables = ctx["state"], ctx["tables"]
    cost = remove_price(state, tables)
    if cost == 0 and R.free_remove_available(state):
        state["node"]["shop"]["freeRemoveUsed"] = True
    else:
        state["player"]["removeCount"] += 1
    state["player"]["starShards"] -= cost
    def_id = state["player"]["dice"][die_id]["defId"]
    remove_die_from_deck(state, die_id)
    ctx["events"].append({"type": "die_removed", "dieId": die_id, "defId": def_id})


def removable_dice(state: Mapping, tables: Mapping) -> list[str]:
    if len(state["player"]["deck"]) <= 5:
        return []
    return [die_id for die_id in state["player"]["deck"]
            if die_def(tables, state["player"]["dice"][die_id]["defId"])["category"] != "cursed"]


# ---------------- 宝箱：两同学派 + 一克制当前敌学派 ----------------

def enter_treasure(ctx: Ctx) -> None:
    state, tables = ctx["state"], ctx["tables"]
    safe_only = bool(stage_def(tables, state["stage"]).get("tutorial"))
    counter = counter_of(stage_theme(state, tables))
    build = deck_school_info(state, tables)["school"]
    if build == "neutral" or build == counter:
        build = choice(state, "loot", [s for s in SCHOOLS if s != counter])
    offers = [*sample(state, "loot", skills_of(tables, build, safe_only), 2),
              *sample(state, "loot", skills_of(tables, counter, safe_only), 1)]
    if len(offers) < 3:
        # 教学关安全骰不足时，从其余安全技能骰（含通用）补齐
        rest = sorted(sid for sid in tables["skills"]
                      if sid not in offers and (not safe_only or tables["skills"][sid]["riskTier"] == "safe"))
        offers.extend(sample(state, "loot", rest, 3 - len(offers)))
    state["node"]["treasure"] = {"offers": offers, "counterSchool": counter, "buildSchool": build}
    state["phase"] = "treasure"


def treasure_pick(ctx: Ctx, def_id: str) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    if def_id == "skip":
        g = apply_mul(tables["shops"]["treasure"]["skipGold"], R.gold_mul(state, tables))
        state["player"]["starShards"] += g
        events.append({"type": "gold", "value": g})
    else:
        die_id = add_die_to_deck(state, def_id)
        events.append({"type": "die_gained", "dieId": die_id, "defId": def_id})
    state["node"]["treasure"] = None


# ---------------- 神殿 / 休息 ----------------

def enter_shrine(ctx: Ctx) -> None:
    state, tables = ctx["state"], ctx["tables"]
    options = [
        {"optionId": "heart", "label": "祈祷：回复 1 心",
         "available": state["player"]["hearts"] < state["player"]["heartsMax"]},
        {"optionId": "gold", "label": f"求财：得 {apply_mul(60, R.gold_mul(state, tables))} 星屑",
         "available": True},
    ]
    if state["config"]["toggles"]["relics"]:
        pool = R.relic_pool(state, tables, ["rare"])
        if pool:
            rid = choice(state, "loot", pool)
            options.append({"optionId": "relic",
                            "label": f"供奉：获得遗物「{tables['relics'][rid]['name']}」",
                            "available": True, "relicId": rid})
    state["node"]["shrine"] = {"options": options}
    state["phase"] = "shrine"


def shrine_choose(ctx: Ctx, option_id: str) -> bool:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    opt = next(o for o in state["node"]["shrine"]["options"] if o["optionId"] == option_id)
    state["node"]["shrine"] = None
    if option_id == "heart":
        state["player"]["hearts"] = min(state["player"]["heartsMax"], state["player"]["hearts"] + 1)
        events.append({"type": "heart_gained", "hearts": state["player"]["hearts"]})
    elif option_id == "gold":
        g = apply_mul(60, R.gold_mul(state, tables))
        state["player"]["starShards"] += g
        events.append({"type": "gold", "value": g})
    elif option_id == "relic":
        return R.grant_relic(ctx, opt["relicId"], None)
    return True


def enter_rest(ctx: Ctx) -> None:
    state = ctx["state"]
    state["node"]["rest"] = {
        "options": [
            {"optionId": "heart", "label": "休憩：回复 1 心",
             "available": state["player"]["hearts"] < state["player"]["heartsMax"]},
            {"optionId": "vigor", "label": "冥想：净化上限永久 +1", "available": True},
        ]
    }
    state["phase"] = "rest"


def rest_choose(ctx: Ctx, option_id: str) -> None:
    state, events = ctx["state"], ctx["events"]
    state["node"]["rest"] = None
    if option_id == "heart":
        state["player"]["hearts"] = min(state["player"]["heartsMax"], state["player"]["hearts"] + 1)
        events.append({"type": "heart_gained", "hearts": state["player"]["hearts"]})
    else:
        state["player"]["virtueMax"] += 1
        events.append({"type": "virtue_max_up", "virtueMax": state["player"]["virtueMax"]})
