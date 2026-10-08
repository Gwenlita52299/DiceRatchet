"""``kernel/event.js`` 的逐位移植 —— 叙事事件（GAME-00 §8.4 / GAME-02 §6），消费 event 流。"""

from __future__ import annotations

from typing import Any, Mapping

from . import relic as R
from .counter import apply_mul, counter_of
from .forge import apply_forged_face, enter_forge
from .rng import choice, randint, weighted
from .state import add_die_to_deck, die_def, remove_die_from_deck

__all__ = ["event_pool", "enter_event", "requirement_met", "requirement_text", "event_choose"]

Ctx = dict[str, Any]


def event_pool(state: Mapping, tables: Mapping) -> list[str]:
    if not state["config"]["toggles"]["events"]:
        return []
    return sorted(
        ev["id"] for ev in tables["events"].values()
        if (not ev.get("relicsOnly") or state["config"]["toggles"]["relics"])
        and ev["stageRange"][0] <= state["stage"] <= ev["stageRange"][1]
        and ev["id"] not in state["seenEvents"]
    )


def enter_event(ctx: Ctx) -> bool:
    state, tables = ctx["state"], ctx["tables"]
    pool = event_pool(state, tables)
    if not pool:
        return False
    ev_id = weighted(state, "event", [{"key": k, "weight": tables["events"][k]["weight"]} for k in pool])
    state["seenEvents"].append(ev_id)
    state["node"]["event"] = {"eventId": ev_id, "resolved": False, "resultText": None, "chosen": None}
    state["phase"] = "event"
    ctx["events"].append({"type": "node_event", "eventId": ev_id})
    return True


def requirement_met(state: Mapping, req: Mapping | None) -> bool:
    if not req:
        return True
    if isinstance(req.get("gold"), int) and state["player"]["starShards"] < req["gold"]:
        return False
    if isinstance(req.get("hearts"), int) and state["player"]["hearts"] < req["hearts"]:
        return False
    return True


def requirement_text(req: Mapping | None) -> str | None:
    if not req:
        return None
    parts = []
    if isinstance(req.get("gold"), int):
        parts.append(f"需要 {req['gold']} 星屑")
    if isinstance(req.get("hearts"), int):
        parts.append(f"需要 ≥{req['hearts']} 心")
    return "，".join(parts)


def pick_random_skill(state: Mapping, tables: Mapping, school: str | None) -> str:
    pool = sorted(sid for sid in tables["skills"] if school is None or tables["skills"][sid]["school"] == school)
    return choice(state, "event", pool)


def non_cursed_dice(state: Mapping, tables: Mapping) -> list[str]:
    return [die_id for die_id in state["player"]["deck"]
            if die_def(tables, state["player"]["dice"][die_id]["defId"])["category"] != "cursed"]


def chapter_boss_school(state: Mapping, tables: Mapping) -> str:
    boss_stage = next(s for s in tables["stages"]["stages"]
                      if s["chapter"] == state["chapter"] and s["boss"])
    def_ = tables["monsters"][boss_stage["bossId"]]
    return def_["phases"][0]["school"] if def_.get("phases") else def_["school"]


def apply_ops(ctx: Ctx, ops: list[Mapping]) -> str | None:
    """执行效果列表；返回 'forge' / 'relic_swap' / None。"""
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    p = state["player"]
    next_phase: str | None = None
    for op in ops:
        kind = op["op"]
        if kind == "gain_gold":
            g = apply_mul(op["value"], R.gold_mul(state, tables))
            p["starShards"] += g
            events.append({"type": "gold", "value": g})
        elif kind == "lose_gold":
            p["starShards"] = max(0, p["starShards"] - op["value"])
            events.append({"type": "gold", "value": -op["value"]})
        elif kind == "lose_virtue":
            p["pendingVirtue"] -= op["value"]
            events.append({"type": "virtue_penalty", "value": op["value"]})
        elif kind == "heal_virtue":
            p["pendingVirtue"] = max(0, p["pendingVirtue"]) + op["value"]
            events.append({"type": "virtue_bonus", "value": op["value"]})
        elif kind == "gain_die":
            n = op.get("count") or 1
            for _ in range(n):
                def_id = op.get("defId") or pick_random_skill(state, tables, op.get("school"))
                die_id = add_die_to_deck(state, def_id)
                events.append({"type": "die_gained", "dieId": die_id, "defId": def_id})
        elif kind == "gain_die_counter_boss":
            def_id = pick_random_skill(state, tables, counter_of(chapter_boss_school(state, tables)))
            events.append({"type": "die_gained", "dieId": add_die_to_deck(state, def_id), "defId": def_id})
        elif kind == "gain_die_random":
            def_id = pick_random_skill(state, tables, None)
            events.append({"type": "die_gained", "dieId": add_die_to_deck(state, def_id), "defId": def_id})
        elif kind == "remove_die":
            pool = non_cursed_dice(state, tables)
            if len(p["deck"]) > 5 and pool:
                die_id = choice(state, "event", pool)
                def_id = p["dice"][die_id]["defId"]
                remove_die_from_deck(state, die_id)
                events.append({"type": "die_removed", "dieId": die_id, "defId": def_id})
        elif kind == "gain_relic":
            if state["config"]["toggles"]["relics"]:
                pool = R.relic_pool(state, tables, ["common", "rare"])
                if pool and not R.grant_relic(ctx, choice(state, "event", pool), "event"):
                    next_phase = "relic_swap"
        elif kind == "gain_heart":
            p["hearts"] = min(p["heartsMax"], p["hearts"] + op["value"])
            events.append({"type": "heart_gained", "hearts": p["hearts"]})
        elif kind == "lose_heart":
            p["hearts"] = max(0, p["hearts"] - op["value"])
            events.append({"type": "player_lost_heart", "heartsLeft": p["hearts"]})
        elif kind == "add_curse":
            for _ in range(op.get("count") or 1):
                die_id = add_die_to_deck(state, op["defId"])
                events.append({"type": "curse_added", "dieId": die_id, "defId": op["defId"]})
        elif kind == "remove_curses":
            for die_id in list(p["deck"]):
                if die_def(tables, p["dice"][die_id]["defId"])["category"] == "cursed":
                    events.append({"type": "die_removed", "dieId": die_id, "defId": p["dice"][die_id]["defId"]})
                    remove_die_from_deck(state, die_id)
        elif kind == "modify_face":
            pool = non_cursed_dice(state, tables)
            if pool:
                die_id = choice(state, "event", pool)
                fi = randint(state, "event", 0, 5)
                apply_forged_face(state, tables, die_id, fi, op["faceId"])
                events.append({"type": "die_forged", "dieId": die_id, "faceIndex": fi,
                               "faceId": op["faceId"]})
        elif kind == "duplicate_die":
            pool = non_cursed_dice(state, tables)
            if pool:
                src = p["dice"][choice(state, "event", pool)]
                die_id = add_die_to_deck(state, src["defId"])
                if src.get("faces"):
                    p["dice"][die_id]["faces"] = [dict(f) for f in src["faces"]]
                    p["dice"][die_id]["forged"] = True
                events.append({"type": "die_gained", "dieId": die_id, "defId": src["defId"]})
        elif kind == "gain_status":
            key = op["statusKind"]
            p["pendingStatuses"][key] = p["pendingStatuses"].get(key, 0) + op["value"]
            events.append({"type": "pending_status", "statusKind": key, "stacks": op["value"]})
        elif kind == "extra_draw":
            p["pendingDraw"] += op["value"]
            events.append({"type": "pending_draw", "value": op["value"]})
        elif kind == "add_slot":
            state["slotSeq"] += 1
            p["slots"].append({"slotId": f"slot_x{state['slotSeq']}", "slotKind": "attack",
                               "constraint": "any", "bonus": 0, "filledBy": None,
                               "disabledUntilTurn": None, "reusable": False})
            events.append({"type": "slot_added"})
        elif kind == "goto_forge":
            next_phase = "forge"
    return next_phase


def event_choose(ctx: Ctx, option_id: str) -> None:
    state, tables = ctx["state"], ctx["tables"]
    ev = tables["events"][state["node"]["event"]["eventId"]]
    idx = int(option_id[len("opt_"):])
    opt = ev["options"][idx]
    out_idx = 0
    if len(opt["outcomes"]) > 1:
        out_idx = int(weighted(state, "event", [{"key": str(i), "weight": o["weight"]}
                                                for i, o in enumerate(opt["outcomes"])]))
    out = opt["outcomes"][out_idx]
    state["node"]["event"]["resolved"] = True
    state["node"]["event"]["chosen"] = option_id
    state["node"]["event"]["resultText"] = out["text"]
    ctx["events"].append({"type": "event_result", "eventId": ev["id"], "optionId": option_id,
                          "text": out["text"]})
    next_phase = apply_ops(ctx, out["effect"])
    if state["player"]["hearts"] <= 0:
        state["outcome"] = "lose"
        state["phase"] = "settle"
        return
    if next_phase == "forge":
        enter_forge(ctx, True)
