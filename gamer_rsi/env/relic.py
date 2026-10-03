"""``kernel/relic.js`` 的逐位移植 —— 遗物效果查询与获取（GAME-00 §8.3 / GAME-02 §7）。

遗物效果只读 state，不消费 RNG（遗物不得修改 RNG 消费顺序）。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "has_relic", "attack_flat_bonus", "deck_thin_bonus", "counter_bonus_permille",
    "relic_mul_permille", "draw_delta", "mono_penalty_disabled", "gold_mul",
    "battle_start_virtue_loss", "battle_start_ward", "purify_bonus", "swift_hand_active",
    "verdant_mend", "extra_heart_loss", "blade_echo_value", "free_remove_available",
    "grant_relic", "swap_relic", "relic_pool",
]

CTX = Mapping[str, Any]  # {state, tables, events}


def has_relic(state: Mapping, relic_id: str) -> bool:
    return bool(state["config"]["toggles"]["relics"]) and relic_id in state["player"]["relics"]


def relic_op(tables: Mapping, relic_id: str) -> Mapping | None:
    r = tables["relics"].get(relic_id)
    return r["effect"] if r else None


def attack_flat_bonus(state: Mapping, tables: Mapping, school: str) -> int:
    b = 0
    if school == "blade" and has_relic(state, "relic_blade_mark"):
        b += relic_op(tables, "relic_blade_mark")["value"]  # type: ignore[index]
    if has_relic(state, "relic_last_stand") and state["player"]["virtue"] <= relic_op(tables, "relic_last_stand")["threshold"]:  # type: ignore[index]
        b += relic_op(tables, "relic_last_stand")["value"]  # type: ignore[index]
    b += deck_thin_bonus(state, tables)
    return b


def deck_thin_bonus(state: Mapping, tables: Mapping) -> int:
    if not has_relic(state, "relic_deck_thin"):
        return 0
    op = relic_op(tables, "relic_deck_thin")
    return op["value"] if len(state["player"]["deck"]) <= op["threshold"] else 0  # type: ignore[index]


def counter_bonus_permille(state: Mapping, tables: Mapping) -> int:
    return relic_op(tables, "relic_school_prism")["value"] if has_relic(state, "relic_school_prism") else 0  # type: ignore[index]


def relic_mul_permille(state: Mapping, tables: Mapping, enemy: Mapping) -> int:
    if has_relic(state, "relic_boss_trophy") and enemy["tier"] in ("elite", "boss"):
        return relic_op(tables, "relic_boss_trophy")["mulPermille"]  # type: ignore[index]
    return 1000


def draw_delta(state: Mapping, tables: Mapping) -> int:
    d = 0
    if has_relic(state, "relic_arcana_loop") and state["player"]["arcanaLastTurn"] >= relic_op(tables, "relic_arcana_loop")["need"]:  # type: ignore[index]
        d += 1
    if has_relic(state, "relic_stubborn_mix"):
        d -= relic_op(tables, "relic_stubborn_mix")["drawPenalty"]  # type: ignore[index]
    return d


def mono_penalty_disabled(state: Mapping) -> bool:
    return has_relic(state, "relic_stubborn_mix")


def gold_mul(state: Mapping, tables: Mapping) -> int:
    m = state["config"]["goldMulPermille"]
    if has_relic(state, "relic_greedy_eye"):
        m = (m * relic_op(tables, "relic_greedy_eye")["goldPermille"]) // 1000  # type: ignore[index]
    return m


def battle_start_virtue_loss(state: Mapping, tables: Mapping) -> int:
    return relic_op(tables, "relic_greedy_eye")["virtueLoss"] if has_relic(state, "relic_greedy_eye") else 0  # type: ignore[index]


def battle_start_ward(state: Mapping, tables: Mapping) -> int:
    return relic_op(tables, "relic_iron_bark")["value"] if has_relic(state, "relic_iron_bark") else 0  # type: ignore[index]


def purify_bonus(state: Mapping, tables: Mapping) -> int:
    return relic_op(tables, "relic_purifier")["value"] if has_relic(state, "relic_purifier") else 0  # type: ignore[index]


def swift_hand_active(state: Mapping) -> bool:
    return has_relic(state, "relic_swift_hand") and not state["player"]["firstPlayDone"]


def verdant_mend(state: Mapping, tables: Mapping) -> int:
    return relic_op(tables, "relic_verdant_root")["value"] if has_relic(state, "relic_verdant_root") else 0  # type: ignore[index]


def extra_heart_loss(state: Mapping) -> int:
    return 1 if has_relic(state, "relic_glass_heart") else 0


def blade_echo_value(state: Mapping, tables: Mapping) -> int:
    return relic_op(tables, "relic_blade_echo")["value"] if has_relic(state, "relic_blade_echo") else 0  # type: ignore[index]


def free_remove_available(state: Mapping) -> bool:
    return (has_relic(state, "relic_deck_thin")
            and state.get("node") is not None
            and state["node"].get("shop") is not None
            and not state["node"]["shop"]["freeRemoveUsed"])


def _on_gain(state: dict, tables: Mapping, relic_id: str) -> None:
    if relic_id == "relic_glass_heart":
        state["player"]["virtueMax"] += relic_op(tables, relic_id)["virtueMax"]  # type: ignore[index]


def _on_lose(state: dict, tables: Mapping, relic_id: str) -> None:
    if relic_id == "relic_glass_heart":
        v = relic_op(tables, relic_id)["virtueMax"]  # type: ignore[index]
        p = state["player"]
        p["virtueMax"] -= v
        p["virtue"] = min(p["virtue"], p["virtueMax"])


def grant_relic(ctx: CTX, relic_id: str, return_phase: str | None) -> bool:
    """获得遗物；栏位满时进入 ``relic_swap`` 阶段。返回 True 表示已直接获得。"""
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    if not state["config"]["toggles"]["relics"] or relic_id not in tables["relics"]:
        return True
    if relic_id in state["player"]["relics"]:
        return True
    if len(state["player"]["relics"]) < state["config"]["relicSlots"]:
        state["player"]["relics"].append(relic_id)
        _on_gain(state, tables, relic_id)
        events.append({"type": "relic_gained", "relicId": relic_id,
                       "name": tables["relics"][relic_id]["name"]})
        return True
    state["pendingRelic"] = {"relicId": relic_id, "returnPhase": return_phase}
    state["phase"] = "relic_swap"
    events.append({"type": "relic_swap_required", "relicId": relic_id})
    return False


def swap_relic(ctx: CTX, drop_id: str) -> str | None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    pending = state["pendingRelic"]
    if drop_id != "skip":
        state["player"]["relics"] = [r for r in state["player"]["relics"] if r != drop_id]
        _on_lose(state, tables, drop_id)
        events.append({"type": "relic_lost", "relicId": drop_id})
        state["player"]["relics"].append(pending["relicId"])
        _on_gain(state, tables, pending["relicId"])
        events.append({"type": "relic_gained", "relicId": pending["relicId"],
                       "name": tables["relics"][pending["relicId"]]["name"]})
    state["pendingRelic"] = None
    return pending["returnPhase"]


def relic_pool(state: Mapping, tables: Mapping, rarities: list[str]) -> list[str]:
    """随机遗物候选（不含已拥有；rarity 过滤）。"""
    return sorted(
        rid for rid, r in tables["relics"].items()
        if r["rarity"] in rarities and rid not in state["player"]["relics"]
    )
