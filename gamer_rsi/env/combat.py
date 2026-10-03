"""``kernel/combat.js`` 的逐位移植 —— 战斗：骰面结算、伤害顺序、状态、敌人意图与回合推进。

GAME-00 §4.2/§5.4、GAME-01 §4。``ctx`` 是 ``{"state", "tables", "events"}`` 的 dict，
与 JS 的 ctx 对象一一对应；所有函数原地操作 ``ctx["state"]``（reduce 已深拷贝）。
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any, Mapping

from . import relic as R
from .counter import apply_mul, arrow_of, school_mul_permille
from .risk import face_has_self_corruption, self_corruption_value
from .rng import choice, randint, shuffle
from .slots import check_constraint
from .state import (add_die_to_deck, current_face, deck_school_info, die_def, empty_statuses,
                    is_mono, refresh_corrupt, thaw)

__all__ = [
    "MAX_CHARGE", "spawn_enemies", "alive_enemies", "plan_intent", "commit_intent_for",
    "commit_intents", "enemy_attack_value", "player_attack_value", "support_value",
    "apply_self_corruption", "roll_die", "draw_dice", "start_battle", "end_turn",
    "face_needs_target", "play_face", "discard_die", "purify_die", "use_ability",
    "slot_disabled", "check_play_face", "corrupt_remaining", "face_has_self_corruption",
]

MAX_CHARGE = 5

Ctx = dict[str, Any]


# ======================================================================
# 敌人
# ======================================================================

def enemy_slots_for(def_: Mapping, phase: int) -> list[Mapping]:
    if def_.get("phases"):
        return def_["phases"][phase - 1]["slots"]
    return def_["slots"]


def enemy_school_for(def_: Mapping, phase: int) -> str:
    if def_.get("phases"):
        return def_["phases"][phase - 1]["school"]
    return def_["school"]


def spawn_enemies(ctx: Ctx, member_ids: list[str]) -> list[dict[str, Any]]:
    state, tables = ctx["state"], ctx["tables"]
    c = state["config"]
    out = []
    for i, def_id in enumerate(member_ids):
        def_ = tables["monsters"][def_id]
        hp = apply_mul(def_["maxCorruption"], c["enemyHpMulPermille"])
        if def_["tier"] == "boss":
            hp = apply_mul(hp, 1000 + c["bossPhaseHpBonusPermille"])
        hp = max(1, hp)
        out.append({
            "enemyId": f"e-{i:02d}", "defId": def_id, "name": def_["name"], "tier": def_["tier"],
            "hp": hp, "hpMax": hp, "ward": 0, "school": enemy_school_for(def_, 1), "phase": 1,
            "statuses": empty_statuses(),
            "slots": [thaw(s) for s in enemy_slots_for(def_, 1)],
            "intent": [], "passives": thaw(def_["passives"]), "dead": False, "taunt": def_["taunt"],
        })
    return out


def alive_enemies(state: Mapping) -> list[dict[str, Any]]:
    return sorted((e for e in state["enemies"] if not e["dead"]), key=lambda e: e["enemyId"])


def has_passive(enemy: Mapping, kind: str) -> Mapping | None:
    for p in enemy["passives"]:
        if p["kind"] == kind:
            return p
    return None


def slot_order(enemy: Mapping, ai: str | None) -> list[Mapping]:
    """按 ai 决定的槽优先级排序（确定性贪心）。比较器与 JS 逐字一致。"""
    idx = list(enumerate(enemy["slots"]))

    def pri(s: Mapping) -> int:
        if ai == "greedy_damage":
            return 0 if s["effect"]["kind"] == "purge" else 1
        if ai == "greedy_block":
            return 0 if s["effect"]["kind"] == "ward" else 1
        return 0

    def comparator(a: tuple[int, Mapping], b: tuple[int, Mapping]) -> int:
        d = pri(a[1]) - pri(b[1])
        if d:
            return d
        if ai == "greedy_damage" and pri(a[1]) == 0:
            d = b[1]["effect"]["value"] - a[1]["effect"]["value"]
            if d:
                return d
        return a[0] - b[0]

    idx.sort(key=cmp_to_key(comparator))
    return [s for _, s in idx]


def plan_intent(enemy: Mapping, ai: str | None, pips: list[int]) -> list[dict[str, Any]]:
    """由意图骰分配敌槽（确定性贪心，D-13）。"""
    order = slot_order(enemy, ai)
    # 降序 pip、升序原序兜底（JS: (a,b) => b.p - a.p || a.i - b.i）
    indexed = sorted(((p, i) for i, p in enumerate(pips)), key=lambda t: (-t[0], t[1]))
    used = [False] * len(indexed)
    intent: list[dict[str, Any]] = []

    def take(slot: Mapping) -> int:
        for k in range(len(indexed)):
            if not used[k] and check_constraint(slot["constraint"], indexed[k][0]):
                used[k] = True
                return indexed[k][0]
        return 0

    for slot in order:
        pip = take(slot)
        if pip:
            intent.append({"slotId": slot["slotId"], "pip": pip, **slot["effect"]})
    intent.sort(key=lambda it: it["slotId"])
    return [{**it, "seq": k, "hits": it.get("hits") or 1, "committed": True}
            for k, it in enumerate(intent)]


def commit_intent_for(ctx: Ctx, enemy: dict[str, Any]) -> None:
    state, tables = ctx["state"], ctx["tables"]
    def_ = tables["monsters"][enemy["defId"]]
    n = def_["diceCount"] + state["config"]["enemyExtraDice"]
    pips = [randint(state, "dice.enemy", 1, def_["diceSides"]) for _ in range(n)]
    enemy["intent"] = plan_intent(enemy, def_["ai"], pips)


def commit_intents(ctx: Ctx) -> None:
    for e in alive_enemies(ctx["state"]):
        commit_intent_for(ctx, e)
    ctx["events"].append({
        "type": "intent_committed",
        "enemyIds": [e["enemyId"] for e in alive_enemies(ctx["state"])],
    })


def enemy_attack_value(state: Mapping, tables: Mapping, enemy: Mapping, value: int) -> dict[str, Any]:
    """敌人攻击对玩家的最终值（raw → 难度倍率 → 学派 → 单系惩罚，逐步 floor，最小 1）。"""
    raw = max(0, value + enemy["statuses"]["strength"] - enemy["statuses"]["weak"])
    deck = deck_school_info(state, tables)["school"]
    mul = school_mul_permille(state["config"], enemy["school"], deck)
    v = apply_mul(raw, state["config"]["enemyDmgMulPermille"])
    v = apply_mul(v, mul)
    mono = 0
    if is_mono(state, tables) and not R.mono_penalty_disabled(state) and state["config"]["monoPenaltyPermille"] > 0:
        mono = state["config"]["monoPenaltyPermille"]
        v = apply_mul(v, 1000 + mono)
    return {"raw": raw, "mulPermille": mul, "monoPermille": mono,
            "value": max(1, v), "arrow": arrow_of(mul)}


# ======================================================================
# 玩家伤害 / 效果
# ======================================================================

def player_attack_value(state: Mapping, tables: Mapping, face: Mapping, opts: Mapping,
                        enemy: Mapping) -> dict[str, Any]:
    """玩家攻击面的最终伤害（GAME-01 §9.2：先学派后遗物，各自 floor，攻击向最小 1）。"""
    p = state["player"]
    school = opts["school"]
    raw = (face["value"] + (opts.get("slotBonus") or 0) + (opts.get("valueBonus") or 0)
           + p["statuses"]["strength"] - p["statuses"]["weak"])
    if school == "blade":
        raw += p["charge"]
    raw += R.attack_flat_bonus(state, tables, school)
    execute = False
    if "execute" in (face.get("tags") or []) and enemy["hp"] * 2 <= enemy["hpMax"]:
        raw *= 2
        execute = True
    raw = max(0, raw)
    mul = school_mul_permille(state["config"], school, enemy["school"],
                              R.counter_bonus_permille(state, tables))
    step1 = apply_mul(raw, mul)
    relic_mul = R.relic_mul_permille(state, tables, enemy)
    final = max(1, apply_mul(step1, relic_mul))
    return {"raw": raw, "mulPermille": mul, "relicMulPermille": relic_mul,
            "value": final, "arrow": arrow_of(mul), "execute": execute}


def support_value(state: Mapping, tables: Mapping, face: Mapping, opts: Mapping) -> int:
    return max(0, face["value"] + (opts.get("slotBonus") or 0) + (opts.get("valueBonus") or 0)
               + R.deck_thin_bonus(state, tables))


def _battle(state: Mapping) -> Mapping | None:
    node = state.get("node")
    return node["battle"] if node else None


def battle_over(state: Mapping) -> bool:
    b = _battle(state)
    return b is None or bool(b["result"])


def check_player_death(ctx: Ctx) -> None:
    """失心流程（GAME-01 §4.3）。"""
    state, events = ctx["state"], ctx["events"]
    p = state["player"]
    if p["virtue"] > 0 or battle_over(state):
        return
    if state["config"]["reviveOnce"] and not p["reviveUsed"]:
        p["reviveUsed"] = True
        p["virtue"] = p["virtueMax"] // 2
        events.append({"type": "revive", "virtue": p["virtue"]})
        return
    p["hearts"] -= 1 + R.extra_heart_loss(state)
    if p["hearts"] < 0:
        p["hearts"] = 0
    events.append({"type": "player_lost_heart", "heartsLeft": p["hearts"]})
    _battle(state)["result"] = "lost" if p["hearts"] > 0 else "dead"


def apply_self_corruption(ctx: Ctx, value: int, source: str) -> None:
    ctx["state"]["player"]["virtue"] -= value
    ctx["events"].append({"type": "self_corruption", "target": "player", "value": value,
                          "source": source})
    check_player_death(ctx)


def damage_player(ctx: Ctx, dmg: int, info: Mapping) -> None:
    p = ctx["state"]["player"]
    absorbed = min(p["ward"], dmg)
    p["ward"] -= absorbed
    if absorbed > 0:
        ctx["events"].append({"type": "ward_absorb", "target": "player", "value": absorbed})
    p["virtue"] -= dmg - absorbed
    ctx["events"].append({"type": "damage", "target": "player", **info,
                          "value": dmg, "dealt": dmg - absorbed})
    check_player_death(ctx)


def check_boss_phase(ctx: Ctx, enemy: dict[str, Any]) -> None:
    def_ = ctx["tables"]["monsters"][enemy["defId"]]
    if not def_.get("phases") or enemy["dead"]:
        return
    changed = False
    while (enemy["phase"] < len(def_["phases"])
           and enemy["hp"] * 1000 <= def_["phases"][enemy["phase"] - 1]["endsAtPermille"] * enemy["hpMax"]):
        enemy["phase"] += 1
        changed = True
    if changed:
        enemy["school"] = enemy_school_for(def_, enemy["phase"])
        enemy["slots"] = [thaw(s) for s in enemy_slots_for(def_, enemy["phase"])]
        ctx["events"].append({"type": "boss_phase", "enemyId": enemy["enemyId"],
                              "phase": enemy["phase"], "school": enemy["school"]})
        commit_intent_for(ctx, enemy)  # 阶段切换：意图刷新并公开（T-11）


def on_enemy_killed(ctx: Ctx, enemy: dict[str, Any]) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    enemy["dead"] = True
    enemy["intent"] = []
    state["stats"]["kills"] += 1
    events.append({"type": "enemy_died", "enemyId": enemy["enemyId"], "name": enemy["name"]})
    echo = R.blade_echo_value(state, tables)
    if echo and state["phase"] == "player_turn":
        target = next((i for i in state["player"]["hand"]
                       if die_def(tables, state["player"]["dice"][i]["defId"])["school"] == "blade"), None)
        if target is not None:
            die = state["player"]["dice"][target]
            die["faceIndex"] = min(5, die["faceIndex"] + echo)
            refresh_corrupt(state, tables, target)
            events.append({"type": "die_modified", "dieId": target, "pip": die["faceIndex"] + 1,
                           "source": "relic_blade_echo"})
    if not alive_enemies(state):
        _battle(state)["result"] = "won"


def damage_enemy(ctx: Ctx, enemy: dict[str, Any], dmg: int, info: Mapping,
                 from_player_attack: bool) -> None:
    absorbed = min(enemy["ward"], dmg)
    enemy["ward"] -= absorbed
    if absorbed > 0:
        ctx["events"].append({"type": "ward_absorb", "target": enemy["enemyId"], "value": absorbed})
    enemy["hp"] -= dmg - absorbed
    ctx["state"]["stats"]["damageDealt"] += dmg - absorbed
    ctx["events"].append({"type": "damage", "target": enemy["enemyId"], **info,
                          "value": dmg, "dealt": dmg - absorbed})
    if enemy["hp"] <= 0:
        enemy["hp"] = 0
        on_enemy_killed(ctx, enemy)
        return
    check_boss_phase(ctx, enemy)
    thorns = has_passive(enemy, "thorns")
    if from_player_attack and thorns:
        apply_self_corruption(ctx, thorns["value"], f"thorns:{enemy['enemyId']}")


def add_status(ctx: Ctx, target_id: str, status_kind: str, stacks: int) -> None:
    state, events = ctx["state"], ctx["events"]
    if target_id == "player":
        state["player"]["statuses"][status_kind] += stacks
    else:
        e = next((x for x in state["enemies"] if x["enemyId"] == target_id), None)
        if e is None or e["dead"]:
            return
        if status_kind == "poison" and has_passive(e, "poison_immune"):
            events.append({"type": "status_immune", "target": target_id, "statusKind": status_kind})
            return
        e["statuses"][status_kind] += stacks
    events.append({"type": "status_applied", "target": target_id,
                   "statusKind": status_kind, "stacks": stacks})


def heal_player(ctx: Ctx, v: int, source: str) -> None:
    p = ctx["state"]["player"]
    before = p["virtue"]
    p["virtue"] = max(p["virtue"], min(p["virtueMax"], p["virtue"] + v))
    ctx["events"].append({"type": "mend", "target": "player", "value": p["virtue"] - before,
                          "source": source})


# ---------- 骰子抽/掷 ----------

def roll_die(ctx: Ctx, die_id: str) -> int:
    state, tables = ctx["state"], ctx["tables"]
    pip = randint(state, "dice.player", 1, 6)
    state["player"]["dice"][die_id]["faceIndex"] = pip - 1
    refresh_corrupt(state, tables, die_id)
    return pip


def draw_dice(ctx: Ctx, n: int) -> None:
    state = ctx["state"]
    p = state["player"]
    drawn: list[str] = []
    pips: list[int] = []
    for _ in range(n):
        if not p["drawPile"]:
            if not p["discardPile"]:
                break
            p["drawPile"] = shuffle(state, "dice.player", p["discardPile"])
            p["discardPile"] = []
            ctx["events"].append({"type": "reshuffle", "count": len(p["drawPile"])})
        die_id = p["drawPile"].pop(0)
        if len(p["hand"]) >= state["config"]["handMax"]:
            p["discardPile"].append(die_id)
            ctx["events"].append({"type": "hand_overflow", "dieId": die_id})
            continue
        p["hand"].append(die_id)
        drawn.append(die_id)
        pips.append(roll_die(ctx, die_id))
    if drawn:
        ctx["events"].append({"type": "roll", "dieIds": drawn, "pips": pips})


# ======================================================================
# 战斗开始 / 回合
# ======================================================================

def start_battle(ctx: Ctx, member_ids: list[str], kind: str) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    p = state["player"]
    state["enemies"] = spawn_enemies(ctx, member_ids)
    state["node"]["battle"] = {"kind": kind, "result": None, "groupMembers": list(member_ids)}
    state["turn"] = 1
    p["virtue"] = max(1, p["virtueMax"] - R.battle_start_virtue_loss(state, tables) + p["pendingVirtue"])
    p["pendingVirtue"] = 0
    p["ward"] = 0
    p["statuses"] = empty_statuses()
    for k, v in p["pendingStatuses"].items():
        p["statuses"][k] += v
    p["pendingStatuses"] = {}
    p["charge"] = 0
    p["reviveUsed"] = False
    p["purifyLeft"] = state["config"]["purifyPerStage"] + R.purify_bonus(state, tables)
    ch = tables["characters"][state["config"]["character"]]
    p["abilities"] = [{"id": a["id"], "usesLeft": a["usesPerBattle"]}
                      for a in ch["abilities"] if a["usesPerBattle"] is not None]
    p["hand"] = []
    p["discardPile"] = []
    p["drawPile"] = shuffle(state, "dice.player", p["deck"])
    for die_id in p["deck"]:
        p["dice"][die_id]["isCorrupt"] = False
    for s in p["slots"]:
        s["filledBy"] = None
        s["disabledUntilTurn"] = None
    p["arcanaLastTurn"] = 0
    p["arcanaThisTurn"] = 0
    p["drainNext"] = 0
    state["phase"] = "player_turn"
    events.append({"type": "battle_start", "kind": kind,
                   "enemies": [{"enemyId": e["enemyId"], "name": e["name"], "school": e["school"]}
                               for e in state["enemies"]]})
    ward = R.battle_start_ward(state, tables)
    if ward:
        p["ward"] += ward
        events.append({"type": "ward_gain", "target": "player", "value": ward,
                       "source": "relic_iron_bark"})
    start_player_turn(ctx, True)


def start_player_turn(ctx: Ctx, first: bool) -> None:
    state, tables = ctx["state"], ctx["tables"]
    p = state["player"]
    for s in p["slots"]:
        s["filledBy"] = None
    p["firstPlayDone"] = False
    p["bladeAttackedThisTurn"] = False
    if not first:
        p["arcanaLastTurn"] = p["arcanaThisTurn"]
    p["arcanaThisTurn"] = 0
    n = state["config"]["drawPerTurn"] + R.draw_delta(state, tables) - p["drainNext"]
    if first:
        n += p["pendingDraw"]
        p["pendingDraw"] = 0
    p["drainNext"] = 0
    draw_dice(ctx, max(1, n))
    commit_intents(ctx)
    ctx["events"].append({"type": "turn_start", "turn": state["turn"]})


def player_upkeep(ctx: Ctx) -> None:
    """玩家 upkeep（E8）：光盾清零；中毒/灼烧非致死至少留 1；再生；虚弱减半（D-12）。"""
    p = ctx["state"]["player"]
    p["ward"] = 0
    st = p["statuses"]
    if st["poison"] > 0:
        d = min(st["poison"], max(0, p["virtue"] - 1))
        p["virtue"] -= d
        ctx["events"].append({"type": "status_tick", "target": "player",
                              "statusKind": "poison", "value": d})
        st["poison"] -= 1
    if st["burn"] > 0:
        d = min(st["burn"], max(0, p["virtue"] - 1))
        p["virtue"] -= d
        ctx["events"].append({"type": "status_tick", "target": "player",
                              "statusKind": "burn", "value": d})
        st["burn"] //= 2
    if st["regen"] > 0:
        heal_player(ctx, st["regen"], "regen")
        st["regen"] -= 1
    if st["weak"] > 0:
        st["weak"] //= 2


def enemy_turn_start(ctx: Ctx, e: dict[str, Any]) -> None:
    """敌人回合开始：光盾清零、中毒/灼烧结算（可致死，D-30）、虚弱减半、被动。"""
    e["ward"] = 0
    st = e["statuses"]
    if st["poison"] > 0:
        d = st["poison"]
        st["poison"] -= 1
        ctx["events"].append({"type": "status_tick", "target": e["enemyId"],
                              "statusKind": "poison", "value": d})
        damage_enemy(ctx, e, d, {"source": "poison", "raw": d, "mulPermille": 1000}, False)
    if not e["dead"] and st["burn"] > 0:
        d = st["burn"]
        st["burn"] //= 2
        ctx["events"].append({"type": "status_tick", "target": e["enemyId"],
                              "statusKind": "burn", "value": d})
        damage_enemy(ctx, e, d, {"source": "burn", "raw": d, "mulPermille": 1000}, False)
    if e["dead"]:
        return
    if st["weak"] > 0:
        st["weak"] //= 2
    regen = has_passive(e, "regen")
    if regen:
        before = e["hp"]
        e["hp"] = min(e["hpMax"], e["hp"] + regen["value"])
        ctx["events"].append({"type": "mend", "target": e["enemyId"],
                              "value": e["hp"] - before, "source": "passive"})
    ramp = has_passive(e, "ramp_strength")
    if ramp:
        add_status(ctx, e["enemyId"], "strength", ramp["value"])


def resolve_enemy_intent(ctx: Ctx, e: Mapping, it: Mapping) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    p = state["player"]
    kind = it["kind"]
    if kind == "purge":
        for _ in range(it["hits"]):
            if battle_over(state):
                break
            r = enemy_attack_value(state, tables, e, it["value"])
            damage_player(ctx, r["value"], {
                "source": e["enemyId"], "raw": r["raw"], "mulPermille": r["mulPermille"],
                "school": e["school"], "monoPermille": r["monoPermille"],
            })
    elif kind == "ward":
        e["ward"] += it["value"]
        events.append({"type": "ward_gain", "target": e["enemyId"], "value": it["value"]})
    elif kind == "status":
        add_status(ctx, e["enemyId"] if it.get("target") == "self" else "player",
                   it["statusKind"], it["value"])
    elif kind == "heal":
        before = e["hp"]
        e["hp"] = min(e["hpMax"], e["hp"] + it["value"])
        events.append({"type": "mend", "target": e["enemyId"], "value": e["hp"] - before})
    elif kind == "add_curse":
        for _ in range(it["value"]):
            die_id = add_die_to_deck(state, it["dieId"], True)  # 厄咒骰仅本场有效（D-14）
            events.append({"type": "curse_added", "dieId": die_id, "defId": it["dieId"]})
    elif kind == "disable_slot":
        open_ = [s for s in p["slots"]
                 if not (s["disabledUntilTurn"] is not None and state["turn"] < s["disabledUntilTurn"])]
        if open_:
            s = open_[it["pip"] % len(open_)]
            s["disabledUntilTurn"] = state["turn"] + 1 + it["value"]
            events.append({"type": "slot_disabled", "slotId": s["slotId"],
                           "untilTurn": s["disabledUntilTurn"]})
    elif kind == "drain_hand":
        p["drainNext"] += it["value"]
        events.append({"type": "drain", "value": it["value"]})


def end_turn(ctx: Ctx) -> None:
    """end_turn：E1..E13（GAME-01 §4.2）。E1 的魔化骰校验在 isLegal 中完成。"""
    state, events = ctx["state"], ctx["events"]
    p = state["player"]
    # E2
    p["discardPile"].extend(p["hand"])
    for die_id in p["hand"]:
        p["dice"][die_id]["isCorrupt"] = False
    p["hand"] = []
    if not p["bladeAttackedThisTurn"]:
        p["charge"] //= 2
    events.append({"type": "turn_end", "turn": state["turn"]})
    # E3/E4
    state["phase"] = "enemy_turn"
    for e in alive_enemies(state):
        if battle_over(state):
            break
        enemy_turn_start(ctx, e)
        if e["dead"] or battle_over(state):
            continue
        for it in e["intent"]:
            if battle_over(state):
                break
            resolve_enemy_intent(ctx, e, it)
        e["intent"] = []
    if battle_over(state):
        state["phase"] = "player_turn"
        return
    # E7
    state["turn"] += 1
    state["stats"]["turns"] += 1
    state["phase"] = "player_turn"
    if state["turn"] > state["config"]["maxTurnsPerBattle"]:
        state["truncationReason"] = "E_KERNEL_TURN_LIMIT"
        _battle(state)["result"] = "truncated"
        return
    # E8/E9
    player_upkeep(ctx)
    check_player_death(ctx)
    if battle_over(state):
        return
    # E10–E12
    start_player_turn(ctx, False)


# ======================================================================
# 玩家动作
# ======================================================================

def face_needs_target(face: Mapping) -> str | None:
    if face["kind"] == "purge" or (face["kind"] == "status" and face.get("target") == "enemy"):
        return "enemy"
    if face["kind"] in ("reroll", "modify", "duplicate_face"):
        return "die"
    return None


def _find_enemy(state: Mapping, target_id: str | None) -> dict[str, Any] | None:
    if target_id is not None:
        e = next((x for x in state["enemies"] if x["enemyId"] == target_id and not x["dead"]), None)
        if e is not None:
            return e
    alive = alive_enemies(state)
    return alive[0] if alive else None


def apply_primary(ctx: Ctx, face: Mapping, opts: Mapping) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    p = state["player"]
    kind = face["kind"]
    if kind == "purge":
        enemy = _find_enemy(state, opts.get("targetId"))
        if enemy is None:
            return
        r = player_attack_value(state, tables, face, opts, enemy)
        if r["mulPermille"] > 1000:
            state["stats"]["counterHits"] += 1
        damage_enemy(ctx, enemy, r["value"], {
            "source": "play_face", "raw": r["raw"], "mulPermille": r["mulPermille"],
            "relicMulPermille": r["relicMulPermille"], "school": opts["school"],
            "label": face["label"], "dieId": opts["dieId"], "slotId": opts["slotId"],
            "execute": r["execute"],
        }, True)
        if opts["school"] == "blade":
            p["bladeAttackedThisTurn"] = True
            p["charge"] = min(MAX_CHARGE, p["charge"] + 1)
    elif kind == "ward":
        v = support_value(state, tables, face, opts)
        p["ward"] += v
        events.append({"type": "ward_gain", "target": "player", "value": v})
    elif kind == "mend":
        heal_player(ctx, support_value(state, tables, face, opts), "play_face")
    elif kind == "status":
        if face.get("target") == "enemy":
            enemy = _find_enemy(state, opts.get("targetId"))
            target = enemy["enemyId"] if enemy is not None else None
        else:
            target = "player"
        if target is not None:
            add_status(ctx, target, face["statusKind"], face["value"])
    elif kind == "draw":
        draw_dice(ctx, face["value"])
    elif kind == "reroll":
        if opts.get("targetId") is not None and opts["targetId"] in p["hand"]:
            pip = roll_die(ctx, opts["targetId"])
            events.append({"type": "roll", "dieIds": [opts["targetId"]], "pips": [pip]})
    elif kind == "modify":
        if opts.get("targetId") is not None and opts["targetId"] in p["hand"]:
            die = p["dice"][opts["targetId"]]
            die["faceIndex"] = min(5, die["faceIndex"] + face["value"])
            refresh_corrupt(state, tables, opts["targetId"])
            events.append({"type": "die_modified", "dieId": opts["targetId"], "pip": die["faceIndex"] + 1})
    elif kind == "duplicate_face":
        if opts.get("targetId") is not None and opts["targetId"] in p["hand"]:
            tf = current_face(state, tables, opts["targetId"])
            events.append({"type": "duplicate", "dieId": opts["targetId"], "label": tf["label"]})
            if tf["kind"] not in ("duplicate_face", "selfCorruption"):
                apply_primary(ctx, tf, {"school": tf["school"], "slotBonus": 0, "valueBonus": 0,
                                        "targetId": None, "dieId": opts["targetId"]})
    elif kind == "gold":
        g = apply_mul(face["value"], R.gold_mul(state, tables))
        p["starShards"] += g
        events.append({"type": "gold", "value": g})


def play_face(ctx: Ctx, action: Mapping) -> None:
    """play_face：负面先结算（M6），再正向分量。"""
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    p = state["player"]
    die_id, slot_id, target_id = action["dieId"], action["slotId"], action.get("targetId")
    face = current_face(state, tables, die_id)
    slot = next((s for s in p["slots"] if s["slotId"] == slot_id), None)
    def_ = die_def(tables, p["dice"][die_id]["defId"])
    p["hand"] = [d for d in p["hand"] if d != die_id]
    p["discardPile"].append(die_id)
    p["dice"][die_id]["isCorrupt"] = False
    if not slot.get("reusable"):
        slot["filledBy"] = die_id
    school = face["school"]
    value_bonus = 0
    if R.swift_hand_active(state):
        ds = deck_school_info(state, tables)["school"]
        if ds != "neutral":
            school = ds
        value_bonus += 1
    p["firstPlayDone"] = True
    events.append({"type": "play_face", "dieId": die_id, "slotId": slot_id,
                   "label": face["label"], "kind": face["kind"], "school": school})
    if def_["category"] == "skill":
        state["stats"]["skillFaceUses"] += 1
    if school == "arcana":
        p["arcanaThisTurn"] += 1

    sc = self_corruption_value(face)
    if sc > 0:
        apply_self_corruption(ctx, sc, die_id)
        if battle_over(state):
            return
    apply_primary(ctx, face, {"school": school, "slotBonus": slot["bonus"], "valueBonus": value_bonus,
                              "targetId": target_id, "dieId": die_id, "slotId": slot_id})
    if not battle_over(state) and school == "verdant":
        m = R.verdant_mend(state, tables)
        if m:
            heal_player(ctx, m, "relic_verdant_root")


def discard_die(ctx: Ctx, die_id: str) -> None:
    state, tables = ctx["state"], ctx["tables"]
    p = state["player"]
    die = p["dice"][die_id]
    face = current_face(state, tables, die_id)
    was_corrupt = die["isCorrupt"]
    p["hand"] = [d for d in p["hand"] if d != die_id]
    p["discardPile"].append(die_id)
    die["isCorrupt"] = False
    ctx["events"].append({"type": "discard", "dieId": die_id, "corrupt": was_corrupt})
    if was_corrupt:
        v = self_corruption_value(face)
        apply_self_corruption(ctx, (v + 1) // 2, f"discard:{die_id}")


def purify_die(ctx: Ctx, die_id: str) -> None:
    p = ctx["state"]["player"]
    p["purifyLeft"] -= 1
    p["hand"] = [d for d in p["hand"] if d != die_id]
    p["discardPile"].append(die_id)
    p["dice"][die_id]["isCorrupt"] = False
    ctx["events"].append({"type": "purify", "dieId": die_id, "purifyLeft": p["purifyLeft"]})


def use_ability(ctx: Ctx, action: Mapping) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    p = state["player"]
    ab = next((a for a in p["abilities"] if a["id"] == action["abilityId"]), None)
    ab["usesLeft"] -= 1
    if action["abilityId"] == "warrior_reroll":
        pips = [roll_die(ctx, die_id) for die_id in p["hand"]]
        events.append({"type": "ability", "abilityId": action["abilityId"]})
        events.append({"type": "roll", "dieIds": list(p["hand"]), "pips": pips})
    elif action["abilityId"] == "warrior_focus":
        die = p["dice"][action["dieId"]]
        die["faceIndex"] = action["value"] - 1
        refresh_corrupt(state, tables, action["dieId"])
        events.append({"type": "ability", "abilityId": action["abilityId"]})
        events.append({"type": "die_modified", "dieId": action["dieId"], "pip": action["value"]})


# ======================================================================
# 合法性（战斗内）
# ======================================================================

def slot_disabled(state: Mapping, slot: Mapping) -> bool:
    return slot["disabledUntilTurn"] is not None and state["turn"] < slot["disabledUntilTurn"]


_SLOT_ACCEPTS_INLINE = {
    "attack": ["purge"], "defense": ["ward"], "heal": ["mend"],
    "utility": None, "cursed": ["selfCorruption"],
}


def check_play_face(state: Mapping, tables: Mapping, a: Mapping) -> dict[str, str] | None:
    """返回 None 表示合法，否则 ``{code, reason}``。"""
    p = state["player"]
    if a["dieId"] not in p["hand"]:
        return {"code": "E_ACTION_DIE_NOT_IN_HAND", "reason": "骰子不在手中"}
    die = p["dice"][a["dieId"]]
    if a.get("faceIndex") is not None and a["faceIndex"] != die["faceIndex"]:
        return {"code": "E_ACTION_FACE_MISMATCH", "reason": "骰面不匹配"}
    slot = next((s for s in p["slots"] if s["slotId"] == a["slotId"]), None)
    if slot is None:
        return {"code": "E_ACTION_UNKNOWN_SLOT", "reason": "未知技能槽"}
    if slot_disabled(state, slot):
        return {"code": "E_ACTION_SLOT_DISABLED", "reason": "技能槽被禁用"}
    if slot.get("filledBy") and not slot.get("reusable"):
        return {"code": "E_ACTION_SLOT_OCCUPIED", "reason": "技能槽已被占用"}
    face = current_face(state, tables, a["dieId"])
    slot_kind = slot["slotKind"]
    if slot_kind not in _SLOT_ACCEPTS_INLINE:  # JS：undefined → 视为不兼容
        return {"code": "E_ACTION_KIND_MISMATCH", "reason": f"该槽不接受「{face['label']}」"}
    accepts = _SLOT_ACCEPTS_INLINE[slot_kind]
    if accepts is not None and face["kind"] not in accepts:
        return {"code": "E_ACTION_KIND_MISMATCH", "reason": f"该槽不接受「{face['label']}」"}
    if not check_constraint(slot["constraint"], die["faceIndex"] + 1):
        return {"code": "E_ACTION_CONSTRAINT_VIOLATION", "reason": slot["constraint"]}
    need = face_needs_target(face)
    if need == "enemy":
        t = next((e for e in state["enemies"] if e["enemyId"] == a.get("targetId")), None)
        if t is None or t["dead"]:
            return {"code": "E_ACTION_BAD_TARGET", "reason": "目标无效"}
    elif need == "die":
        others = [d for d in p["hand"] if d != a["dieId"]]
        if others:
            if a.get("targetId") not in others:
                return {"code": "E_ACTION_BAD_TARGET", "reason": "需要选择另一颗手骰"}
        elif a.get("targetId") is not None:
            return {"code": "E_ACTION_BAD_TARGET", "reason": "无可选目标"}
    elif a.get("targetId") is not None:
        return {"code": "E_ACTION_BAD_TARGET", "reason": "该骰面不需要目标"}
    return None


def corrupt_remaining(state: Mapping) -> list[str]:
    p = state["player"]
    return [die_id for die_id in p["hand"] if p["dice"][die_id]["isCorrupt"]]
