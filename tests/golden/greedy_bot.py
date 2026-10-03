"""``src/game/scripts/bots.js`` 的 ``greedy_counter`` 移植（MOD-01 成功度量③）。

这是**测试基础设施**，不属于 ``gamer_rsi/env``（env 只逐文件镜像 JS kernel，
FR-01-1）；bot 策略在 JS 侧位于 ``scripts/bots.js``。因为内核已由 golden/差分对拍
证明逐位等价，同一策略 + 同一 seed 的 Python 结局必须与 JS 逐局相同。
"""

from __future__ import annotations

from typing import Any, Sequence

from gamer_rsi.env import GameEngine

__all__ = ["run_game", "choose_action", "bot_rng"]

_STRATEGIES = ("random", "greedy_damage", "greedy_counter", "school_blade",
               "school_arcana", "school_verdant")
_BEATS = {"blade": "verdant", "arcana": "blade", "verdant": "arcana"}

_FALLBACK_WEIGHTS = {"poison": 2.5, "burn": 2.0, "weak": 2.0, "strength": 4.0, "regen": 2.5}


def bot_rng(seed: int):
    """JS ``botRng``：xorshift32（仅 ``random`` 策略的决策用，与内核 RNG 无关）。"""
    x = (seed * 2654435761) & 0xFFFFFFFF or 1

    def rnd() -> float:
        nonlocal x
        x ^= (x << 13) & 0xFFFFFFFF
        x &= 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        x &= 0xFFFFFFFF
        return x / 4294967296

    return rnd


def _pref_school(strategy: str) -> str | None:
    return strategy[7:] if strategy.startswith("school_") else None


def _counter_of(school: str) -> str | None:
    return next((k for k, v in _BEATS.items() if v == school), None)


def _incoming_damage(obs: Mapping) -> int:
    total = 0
    for e in obs["enemies"]:
        for it in e["intent"]:
            if it["kind"] == "purge":
                total += it["value"] * (it.get("hits") or 1)
    return total


def score_play(obs: Mapping, hints: Mapping, a: Mapping, strategy: str) -> float:
    die = next((d for d in obs["hand"] if d["dieId"] == a["dieId"]), None)
    hint = hints[a["dieId"]][a["slotId"]]
    pref = _pref_school(strategy)
    p = obs["player"]
    s = 0.0
    sc = die.get("corruptionValue") or 0 if die else 0
    kind = die["kind"] if die else ""
    if kind == "purge":
        pv = hint["previews"] and next(
            (x for x in hint["previews"] if x["enemyId"] == a.get("targetId")),
            hint["previews"][0] if hint["previews"] else None)
        enemy = next((e for e in obs["enemies"] if e["enemyId"] == a.get("targetId")), None)
        if pv and enemy:
            s = min(pv["value"], enemy["hp"] + enemy["ward"]) * 3
            if pv["value"] >= enemy["hp"] + enemy["ward"]:
                s += 8
        else:
            s = 3
        if strategy == "greedy_counter" and pv and pv.get("arrow") == "up":
            s += 6
        if enemy:
            s += max(0, 10 - enemy["hp"]) * 0.3
    elif kind == "ward":
        need = max(0, _incoming_damage(obs) - p["ward"])
        v = hint["previews"][0]["value"] if hint["previews"] else (die["value"] if die else 0)
        s = min(v, need) * 2.5
    elif kind == "mend":
        v = hint["previews"][0]["value"] if hint["previews"] else (die["value"] if die else 0)
        s = min(v, p["virtueMax"] - p["virtue"]) * 2.2
    elif kind == "status":
        w = _FALLBACK_WEIGHTS.get(die["statusKind"], 1) if die else 1
        s = (die["value"] if die else 0) * w
        if die and die.get("statusKind") == "weak" and not any(
                e["intent"] and any(i["kind"] == "purge" for i in e["intent"])
                for e in obs["enemies"]):
            s *= 0.3
    elif kind == "draw":
        s = 7 * (die["value"] if die else 0)
    elif kind == "reroll":
        s = 9 if next((d for d in obs["hand"]
                       if d["dieId"] == a.get("targetId") and d.get("isCorrupt")), None) else 1.5
    elif kind == "modify":
        s = 2 if next((d for d in obs["hand"]
                       if d["dieId"] == a.get("targetId") and d["pip"] < 6), None) else 0
    elif kind == "duplicate_face":
        s = 5
    elif kind == "gold":
        s = 1.5
    elif kind == "selfCorruption":
        s = -sc * 3
    if sc > 0 and kind != "selfCorruption":
        s -= sc * 2.5
    if pref and die and die.get("school") == pref:
        s *= 1.4
    slot = next((x for x in obs["slots"] if x["slotId"] == a["slotId"]), None)
    if slot and slot["slotKind"] == "utility" and kind in ("purge", "ward", "mend"):
        s -= 1.5
    if slot:
        s += slot["bonus"] * 0.5
    return s


def battle_action(state: Mapping, engine: GameEngine, strategy: str, rnd) -> Mapping:
    legal = engine.legal_actions(state)
    hints = engine.play_hints(state)
    plays = [a for a in legal if a["kind"] == "play_face"]
    best, best_s = None, 0.5
    obs = engine.observe(state)
    for a in plays:
        s = score_play(obs, hints, a, strategy)
        if s > best_s:
            best, best_s = a, s
    if best is not None:
        return best
    corrupt = sorted((d for d in obs["hand"] if d.get("isCorrupt")),
                     key=lambda d: -(d.get("corruptionValue") or 0))
    if corrupt:
        d = corrupt[0]
        if any(a["kind"] == "purify" and a["dieId"] == d["dieId"] for a in legal):
            return {"kind": "purify", "dieId": d["dieId"]}
        reroll = next((a for a in legal
                       if a["kind"] == "use_ability" and a["abilityId"] == "warrior_reroll"), None)
        if reroll and len(corrupt) >= 2:
            return reroll
        return {"kind": "discard_die", "dieId": d["dieId"]}
    return {"kind": "end_turn"}


def node_score(obs: Mapping, node: Mapping, strategy: str) -> float:
    p = obs["player"]
    low_hp = p["hearts"] <= 1
    k = node["kind"]
    base = {"battle": 5, "elite": -5 if low_hp else 3, "boss": 0, "treasure": 7,
            "shop": 8 if p["starShards"] >= 50 else 2,
            "forge": 5 if p["starShards"] >= 40 else 1,
            "event": 4, "shrine": 6,
            "rest": 9 if p["hearts"] < p["heartsMax"] else 3}.get(k, 0)
    if strategy == "greedy_counter" and node.get("preview") and node["preview"].get("arrow") == "down":
        base += 2
    if node.get("preview") and node["preview"].get("arrow") == "up":
        base -= 1
    return base


def pick_die_def(offers: Sequence[Mapping], strategy: str, obs: Mapping | None) -> Mapping:
    pref = _pref_school(strategy)

    def score(d: Mapping) -> float:
        s = 0.0
        for f in d["faces"]:
            if f["kind"] == "purge":
                s += f["value"]
            elif f["kind"] in ("ward", "mend"):
                s += f["value"] * 0.7
            elif f["kind"] == "status":
                s += f["value"] * 0.9
            elif f["kind"] == "draw":
                s += 3
            s -= (f.get("selfCorruption") or 0) * 1.2
        if pref and d.get("school") == pref:
            s += 12
        if strategy == "greedy_counter" and d.get("counter"):
            s += 8
        if not pref and obs and d.get("school") == obs["player"].get("deckSchool"):
            s += 2
        return s

    return sorted(offers, key=score, reverse=True)[0]


def choose_action(state: Mapping, engine: GameEngine, strategy: str, rnd) -> Mapping:
    legal = engine.legal_actions(state)
    phase = state["phase"]
    if phase == "player_turn":
        return battle_action(state, engine, strategy, rnd)
    if phase == "map":
        if strategy == "random":
            mc = [a for a in legal if a["kind"] == "map_choose"]
            return mc[int(rnd() * len(mc))]
        obs = engine.observe(state)
        nodes = [n for row in obs["map"]["rows"] if isinstance(row, list)
                 for n in row if n and n.get("reachable")]
        nodes.sort(key=lambda n: -node_score(obs, n, strategy))
        return {"kind": "map_choose", "nodeId": nodes[0]["nodeId"]}
    if phase == "treasure":
        obs = engine.observe(state)
        if strategy == "random":
            return {"kind": "treasure_pick",
                    "defId": obs["treasure"]["offers"][int(rnd() * len(obs["treasure"]["offers"]))]["defId"]}
        if len(obs["deck"]) >= 20:
            return {"kind": "treasure_pick", "defId": "skip"}
        return {"kind": "treasure_pick", "defId": pick_die_def(obs["treasure"]["offers"], strategy, obs)["defId"]}
    if phase == "shop":
        obs = engine.observe(state)
        buyable = [it for it in obs["shop"]["items"] if not it["sold"] and it["affordable"]]
        relic = next((it for it in buyable if it["kind"] == "relic"), None)
        if relic and strategy != "random":
            return {"kind": "shop_buy", "itemId": relic["itemId"]}
        dice = [it for it in buyable if it["kind"] == "die"]
        if dice and len(obs["deck"]) < 14:
            if strategy == "random":
                return {"kind": "shop_buy", "itemId": dice[0]["itemId"]}
            scored = pick_die_def([{**x, "counter": x["school"] == obs["shop"].get("counterSchool")}
                                   for x in dice], strategy, obs)
            return {"kind": "shop_buy", "itemId": scored["itemId"]}
        if strategy != "random" and obs["player"]["starShards"] >= obs["shop"]["removePrice"] + 20:
            for w in ("die_utility",):
                d = next((x for x in obs["deck"]
                          if x["defId"] == w and x["dieId"] in obs["shop"]["removable"]), None)
                if d:
                    return {"kind": "shop_remove", "dieId": d["dieId"]}
        return {"kind": "shop_leave"}
    if phase == "forge":
        obs = engine.observe(state)
        f = obs["forge"]
        if not f["done"] and f["affordable"]:
            cands = sorted(f["candidates"],
                           key=lambda c: -((c["value"] if c["kind"] == "purge" else 1)))
            cand = cands[0] if cands else None
            target = next((d for d in obs["deck"]
                           if d["defId"] == "die_strike" and d["dieId"] in f["forgeable"]), None)
            if target and cand:
                return {"kind": "forge_pick", "dieId": target["dieId"], "faceIndex": 0,
                        "candidateId": cand["candidateId"]}
        return {"kind": "shop_leave"}
    if phase == "event":
        obs = engine.observe(state)
        if obs["event"]["resolved"]:
            return {"kind": "shop_leave"}
        opts = [x for x in obs["event"]["options"] if x["affordable"]]
        if strategy == "random":
            return {"kind": "event_choose", "optionId": opts[int(rnd() * len(opts))]["optionId"]}
        pos = next((x for x in opts if x["risk"] == "positive"), None)
        return {"kind": "event_choose", "optionId": (pos or opts[-1])["optionId"]}
    if phase in ("shrine", "rest"):
        acts = [a for a in legal if a["kind"] in ("shrine_choose", "rest_choose")]
        pref = ["heart", "relic", "vigor", "gold"]
        acts.sort(key=lambda a: pref.index(a["optionId"]) if a["optionId"] in pref else 99)
        return acts[0]
    if phase == "relic_swap":
        return {"kind": "relic_swap", "relicId": "skip"}
    return legal[0]


def run_game(engine: GameEngine, *, difficulty: str = "medium", seed: int = 1,
             strategy: str = "greedy_counter", toggles: Mapping | None = None,
             max_steps: int = 20000, stage_count: int | None = None,
             record: bool = False) -> dict[str, Any]:
    rnd = bot_rng(seed + 7)
    cfg: dict[str, Any] = {"difficulty": difficulty, "seed": seed,
                           "toggles": dict(toggles or {}), "runId": f"r-bot{seed}"}
    if stage_count is not None:
        cfg["stageCount"] = stage_count
    state = engine.create(cfg)
    actions: list[Mapping] = []
    steps = 0
    while state["phase"] != "settle" and steps < max_steps:
        a = choose_action(state, engine, strategy, rnd)
        r = engine.reduce(state, a)
        if r.get("error"):
            raise RuntimeError(f"kernel error {r['error']} @ {a}")
        if r["illegal"]:
            raise RuntimeError(f"bot illegal {r['code']} {r['reason']} @ {a} phase={state['phase']}")
        if record:
            actions.append(a)
        state = r["state"]
        steps += 1
    return {"state": state, "outcome": state["outcome"], "stage": state["stage"],
            "steps": steps, "actions": actions, "stats": state["stats"]}
