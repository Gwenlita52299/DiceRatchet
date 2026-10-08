"""MOD-04 启发式策略：GAME ``src/game/scripts/bots.js::greedy_counter`` 的纯 obs 移植。

用途（PRD FR-04-14）：

- 模型不可用 / 超时 / 重试耗尽 / 泄漏阻断时的 fallback；
- MOD-08 ``baseline_heuristic`` 基线（同一份实现，禁止另写一份）。

约束：

- **只用公开信息**：``obs``（``obs-agent-1.0.0`` 投影）、``legal``（``env.legal_actions()``）
  与 ``hints``（``env.hints()``）；不读环境内部状态、不调 LLM、无随机、无墙钟。
- **只返回合法动作**：候选经 ``legal`` 精确匹配后返回其副本；匹配不到则退化为
  ``legal[0]``（确定性顺序由 MOD-02 保证），``legal`` 为空时返回 ``{}``。
- 战斗内打分与 JS ``greedy_counter`` 逐步对拍（§11 U9）；战斗外的分支在观测缺字段
  （如投影不含 ``deck``）时按等价信息退化，行为仍确定。
"""

from __future__ import annotations

import json
from typing import Any, Final, Mapping, Sequence

__all__ = ["heuristic_policy", "heuristic_policy_explained", "score_play", "pick_die_def",
           "node_score"]

_FALLBACK_WEIGHTS: Final[Mapping[str, float]] = {
    "poison": 2.5, "burn": 2.0, "weak": 2.0, "strength": 4.0, "regen": 2.5,
}
_SHRINE_PREF: Final[tuple[str, ...]] = ("heart", "relic", "vigor", "gold")
_END_TURN: Final[dict[str, Any]] = {"kind": "end_turn"}
_SHOP_LEAVE: Final[dict[str, Any]] = {"kind": "shop_leave"}


def _legal_match(action: Mapping[str, Any] | None,
                 legal: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """在 ``legal`` 中找与 ``action`` 等价的条目并返回其副本（去掉显式 null 后比对）。"""

    def sig(item: Mapping[str, Any]) -> str:
        return json.dumps({str(k): v for k, v in item.items() if v is not None},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    if action is None:
        return None
    target = sig(action)
    for item in legal:
        if isinstance(item, Mapping) and sig(item) == target:
            return dict(item)
    return None


def _first_kind(legal: Sequence[Mapping[str, Any]], *kinds: str) -> dict[str, Any] | None:
    for item in legal:
        if isinstance(item, Mapping) and item.get("kind") in kinds:
            return dict(item)
    return None


def _incoming_damage(obs: Mapping[str, Any]) -> int:
    total = 0
    for enemy in obs.get("enemies") or ():
        if not isinstance(enemy, Mapping):
            continue
        for intent in enemy.get("intent") or ():
            if isinstance(intent, Mapping) and intent.get("kind") == "purge":
                total += int(intent.get("value") or 0) * int(intent.get("hits") or 1)
    return total


def _die(obs: Mapping[str, Any], die_id: Any) -> Mapping[str, Any] | None:
    for item in obs.get("hand") or ():
        if isinstance(item, Mapping) and item.get("dieId") == die_id:
            return item
    return None


def _preview(hint: Mapping[str, Any], action: Mapping[str, Any]) -> Mapping[str, Any] | None:
    previews = hint.get("previews") or []
    if not previews:
        return None
    for item in previews:
        if isinstance(item, Mapping) and item.get("enemyId") == action.get("targetId"):
            return item
    return previews[0] if isinstance(previews[0], Mapping) else None


def score_play(obs: Mapping[str, Any], hints: Mapping[str, Any],
               action: Mapping[str, Any]) -> float:
    """单张 ``play_face`` 的启发式得分（逐行移植 JS ``scorePlay``，strategy=greedy_counter）。"""
    die = _die(obs, action.get("dieId"))
    hint = (hints.get(str(action.get("dieId"))) or {}).get(str(action.get("slotId"))) or {}
    player = obs.get("player") or {}
    score = 0.0
    corruption = int((die or {}).get("corruptionValue") or 0)
    kind = str((die or {}).get("kind") or "")
    if kind == "purge":
        preview = _preview(hint, action)
        enemy = next((e for e in obs.get("enemies") or ()
                      if isinstance(e, Mapping) and e.get("enemyId") == action.get("targetId")),
                     None)
        if preview and enemy:
            hp_ward = int(enemy.get("hp") or 0) + int(enemy.get("ward") or 0)
            score = min(int(preview.get("value") or 0), hp_ward) * 3
            if int(preview.get("value") or 0) >= hp_ward:
                score += 8
            if preview.get("arrow") == "up":
                score += 6
        else:
            score = 3
        if enemy:
            score += max(0, 10 - int(enemy.get("hp") or 0)) * 0.3
    elif kind == "ward":
        need = max(0, _incoming_damage(obs) - int(player.get("ward") or 0))
        previews = hint.get("previews") or []
        value = int(previews[0].get("value") or 0) if previews and isinstance(
            previews[0], Mapping) else int((die or {}).get("value") or 0)
        score = min(value, need) * 2.5
    elif kind == "mend":
        previews = hint.get("previews") or []
        value = int(previews[0].get("value") or 0) if previews and isinstance(
            previews[0], Mapping) else int((die or {}).get("value") or 0)
        score = min(value, int(player.get("virtueMax") or 0) - int(player.get("virtue") or 0)) * 2.2
    elif kind == "status":
        weight = _FALLBACK_WEIGHTS.get(str((die or {}).get("statusKind") or ""), 1.0)
        score = int((die or {}).get("value") or 0) * weight
        if (die or {}).get("statusKind") == "weak" and not any(
                (e.get("intent") or ()) and any(i.get("kind") == "purge"
                                                for i in (e.get("intent") or ()))
                for e in obs.get("enemies") or () if isinstance(e, Mapping)):
            score *= 0.3
    elif kind == "draw":
        score = 7 * int((die or {}).get("value") or 0)
    elif kind == "reroll":
        target = _die(obs, action.get("targetId"))
        score = 9.0 if target and target.get("isCorrupt") else 1.5
    elif kind == "modify":
        target = _die(obs, action.get("targetId"))
        score = 2.0 if target and int(target.get("pip") or 0) < 6 else 0.0
    elif kind == "duplicate_face":
        score = 5.0
    elif kind == "gold":
        score = 1.5
    elif kind == "selfCorruption":
        score = -corruption * 3
    if corruption > 0 and kind != "selfCorruption":
        score -= corruption * 2.5
    slot = next((s for s in obs.get("slots") or ()
                 if isinstance(s, Mapping) and s.get("slotId") == action.get("slotId")), None)
    if slot and slot.get("slotKind") == "utility" and kind in ("purge", "ward", "mend"):
        score -= 1.5
    if slot:
        score += float(slot.get("bonus") or 0) * 0.5
    return score


def _battle_action(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
                   hints: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    plays = [a for a in legal if isinstance(a, Mapping) and a.get("kind") == "play_face"]
    best: Mapping[str, Any] | None = None
    best_score = 0.5
    best_detail = ""
    for action in plays:
        value = score_play(obs, hints, action)
        if value > best_score:
            best, best_score = action, value
            best_detail = (f"counter play_face {action.get('dieId')}"
                           f"\u2192{action.get('slotId')} {action.get('targetId')}")
    if best is not None:
        chosen = _legal_match(best, legal) or dict(best)
        return chosen, f"heuristic:{best_detail} score={round(best_score, 3)}"

    corrupt = sorted((d for d in obs.get("hand") or ()
                      if isinstance(d, Mapping) and d.get("isCorrupt")),
                     key=lambda d: -(int(d.get("corruptionValue") or 0)))
    if corrupt:
        die = corrupt[0]
        die_id = die.get("dieId")
        purify = _legal_match({"kind": "purify", "dieId": die_id}, legal)
        if purify is not None:
            return purify, f"heuristic:purify {die_id}"
        reroll = next((dict(a) for a in legal
                       if isinstance(a, Mapping) and a.get("kind") == "use_ability"
                       and a.get("abilityId") == "warrior_reroll"), None)
        if reroll is not None and len(corrupt) >= 2:
            return reroll, f"heuristic:reroll corrupted({len(corrupt)})"
        discard = _legal_match({"kind": "discard_die", "dieId": die_id}, legal)
        if discard is not None:
            return discard, f"heuristic:discard_die {die_id}"
    end_turn = _legal_match(_END_TURN, legal)
    if end_turn is not None:
        return end_turn, "heuristic:end_turn"
    return _fallback(legal), "heuristic:fallback(no end_turn)"


def node_score(obs: Mapping[str, Any], node: Mapping[str, Any]) -> float:
    """地图节点打分（移植 JS ``nodeScore``，strategy=greedy_counter）。"""
    player = obs.get("player") or {}
    low_hp = int(player.get("hearts") or 0) <= 1
    kind = str(node.get("kind") or "")
    base = {
        "battle": 5.0,
        "elite": -5.0 if low_hp else 3.0,
        "boss": 0.0,
        "treasure": 7.0,
        "shop": 8.0 if int(player.get("starShards") or 0) >= 50 else 2.0,
        "forge": 5.0 if int(player.get("starShards") or 0) >= 40 else 1.0,
        "event": 4.0,
        "shrine": 6.0,
        "rest": 9.0 if int(player.get("hearts") or 0) < int(player.get("heartsMax") or 0) else 3.0,
    }.get(kind, 0.0)
    preview = node.get("preview") or {}
    if isinstance(preview, Mapping):
        if preview.get("arrow") == "down":
            base += 2
        if preview.get("arrow") == "up":
            base -= 1
    return base


def pick_die_def(offers: Sequence[Mapping[str, Any]],
                 obs: Mapping[str, Any] | None = None) -> Mapping[str, Any] | None:
    """骰子 def 选择（移植 JS ``pickDieDef``，strategy=greedy_counter）。"""

    def score(die: Mapping[str, Any]) -> float:
        total = 0.0
        for face in die.get("faces") or ():
            if not isinstance(face, Mapping):
                continue
            kind = face.get("kind")
            if kind == "purge":
                total += float(face.get("value") or 0)
            elif kind in ("ward", "mend"):
                total += float(face.get("value") or 0) * 0.7
            elif kind == "status":
                total += float(face.get("value") or 0) * 0.9
            elif kind == "draw":
                total += 3
            total -= float(face.get("selfCorruption") or 0) * 1.2
        if die.get("counter"):
            total += 8
        if obs is not None:
            deck_school = (obs.get("player") or {}).get("deckSchool")
            if deck_school and die.get("school") == deck_school:
                total += 2
        return total

    candidates = [d for d in offers if isinstance(d, Mapping)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda d: (-score(d), str(d.get("defId"))))[0]


def _map_action(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]
                ) -> tuple[dict[str, Any], str]:
    choices = [a for a in legal if isinstance(a, Mapping) and a.get("kind") == "map_choose"]
    if not choices:
        return _fallback(legal), "heuristic:fallback(no map_choose)"
    nodes: list[Mapping[str, Any]] = []
    for row in (obs.get("map") or {}).get("rows") or ():
        if not isinstance(row, list):
            continue
        for node in row:
            if isinstance(node, Mapping) and node.get("reachable") and node.get("nodeId"):
                nodes.append(node)
    reachable = [n for n in nodes
                 if any(a.get("nodeId") == n.get("nodeId") for a in choices)]
    if not reachable:
        return dict(choices[0]), "heuristic:map_choose(first legal)"
    reachable.sort(key=lambda n: -node_score(obs, n))
    best = reachable[0]
    return (dict(next(a for a in choices if a.get("nodeId") == best.get("nodeId"))),
            f"heuristic:map_choose {best.get('nodeId')} score={round(node_score(obs, best), 3)}")


def _treasure_action(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]
                     ) -> tuple[dict[str, Any], str]:
    picks = [a for a in legal if isinstance(a, Mapping) and a.get("kind") == "treasure_pick"]
    if not picks:
        return _fallback(legal), "heuristic:fallback(no treasure_pick)"
    deck = obs.get("deck")
    if isinstance(deck, (list, tuple)) and len(deck) >= 20:
        skip = _legal_match({"kind": "treasure_pick", "defId": "skip"}, legal)
        if skip is not None:
            return skip, "heuristic:treasure_pick skip(deck full)"
    offers = [o for o in (obs.get("treasure") or {}).get("offers") or ()
              if isinstance(o, Mapping)
              and any(a.get("defId") == o.get("defId") for a in picks)]
    chosen = pick_die_def(offers, obs)
    if chosen is None:
        return dict(picks[0]), "heuristic:treasure_pick(first legal)"
    match = next(a for a in picks if a.get("defId") == chosen.get("defId"))
    return dict(match), f"heuristic:treasure_pick {chosen.get('defId')}"


def _shop_action(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]
                 ) -> tuple[dict[str, Any], str]:
    shop = obs.get("shop") or {}
    items = [it for it in shop.get("items") or ()
             if isinstance(it, Mapping) and not it.get("sold") and it.get("affordable")]
    relic = next((it for it in items if it.get("kind") == "relic"), None)
    if relic is not None:
        match = _legal_match({"kind": "shop_buy", "itemId": relic.get("itemId")}, legal)
        if match is not None:
            return match, f"heuristic:shop_buy relic {relic.get('itemId')}"

    # 牌库规模：优先用原始 obs 的 deck；投影观测（obs-agent）不含 deck，但 MOD-02 的
    # ``shop.removable`` 恰好是**全牌库**的 dieId 列表（len 与 deck 一致），可作等价代理。
    deck = obs.get("deck")
    removable = [str(d) for d in shop.get("removable") or ()]
    if isinstance(deck, (list, tuple)):
        deck_len: int | None = len(deck)
    elif shop.get("removable") is not None:
        deck_len = len(removable)
    else: # pragma: no cover - 两个信号都没有时才发生
        deck_len = None

    dice = [it for it in items if it.get("kind") == "die"]
    if dice and (deck_len is None or deck_len < 14):
        counter_school = shop.get("counterSchool")
        enriched = [dict(it, counter=it.get("school") == counter_school) for it in dice]
        chosen = pick_die_def(enriched, obs)
        if chosen is not None:
            match = _legal_match({"kind": "shop_buy", "itemId": chosen.get("itemId")}, legal)
            if match is not None:
                return match, f"heuristic:shop_buy die {chosen.get('itemId')}"

    shards = int((obs.get("player") or {}).get("starShards") or 0)
    remove_price = int(shop.get("removePrice") or 0)
    if shards >= remove_price + 20 and isinstance(deck, (list, tuple)):
        # JS：只移除 deck 中第一颗 defId=="die_utility" 且可移除的骰子；没有就 leave。
        allowed = set(removable)
        target = next((str(d.get("dieId")) for d in deck
                       if isinstance(d, Mapping) and d.get("defId") == "die_utility"
                       and str(d.get("dieId")) in allowed), None)
        if target is not None:
            match = _legal_match({"kind": "shop_remove", "dieId": target}, legal)
            if match is not None:
                return match, f"heuristic:shop_remove {target}"
    leave = _legal_match(_SHOP_LEAVE, legal)
    if leave is not None:
        return leave, "heuristic:shop_leave"
    return _fallback(legal), "heuristic:fallback(no shop_leave)"


def _forge_action(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]
                  ) -> tuple[dict[str, Any], str]:
    forge = obs.get("forge") or {}
    picks = [a for a in legal if isinstance(a, Mapping) and a.get("kind") == "forge_pick"]
    if picks and not forge.get("done") and forge.get("affordable"):
        # 与 JS ``greedyCounter`` 逐字一致：候选分 = purge? value : 1，取最大（稳定序）。
        by_id = {c.get("candidateId"): c for c in forge.get("candidates") or ()
                 if isinstance(c, Mapping)}

        def candidate_score(candidate_id: Any) -> float:
            candidate = by_id.get(candidate_id) or {}
            return float(candidate.get("value") or 0) \
                if candidate.get("kind") == "purge" else 1.0

        best_candidate = sorted(
            (c for c in forge.get("candidates") or () if isinstance(c, Mapping)),
            key=lambda c: -candidate_score(c.get("candidateId")))
        ordered_ids = [c.get("candidateId") for c in best_candidate]
        # 目标骰：JS 取 deck 里 defId=="die_strike" 的第一颗。obs-agent 投影不含 deck，
        # 因此有 deck 时逐字复刻 JS；没有 deck 时按 forgeable 顺序取第一个合法组合
        # （确定性且必然合法）。
        forgeable = [str(d) for d in forge.get("forgeable") or ()]
        deck = obs.get("deck")
        preferred: list[str] = []
        if isinstance(deck, (list, tuple)):
            allowed = set(forgeable)
            preferred = [str(d.get("dieId")) for d in deck
                         if isinstance(d, Mapping) and d.get("defId") == "die_strike"
                         and str(d.get("dieId")) in allowed]
        rest = sorted({str(a.get("dieId")) for a in picks if a.get("dieId")} - set(preferred),
                      key=lambda d: (forgeable.index(d) if d in forgeable else 1 << 30, d))
        ordered_dies = preferred + rest
        for candidate_id in ordered_ids:
            for die_id in ordered_dies:
                match = _legal_match({"kind": "forge_pick", "dieId": die_id, "faceIndex": 0,
                                      "candidateId": candidate_id}, picks)
                if match is None:
                    match = next((dict(a) for a in picks
                                  if a.get("candidateId") == candidate_id
                                  and a.get("dieId") == die_id), None)
                if match is not None:
                    return match, (f"heuristic:forge_pick {match.get('dieId')}"
                                   f"#{match.get('faceIndex')} {candidate_id}")
    leave = _legal_match(_SHOP_LEAVE, legal)
    if leave is not None:
        return leave, "heuristic:forge_leave"
    return _fallback(legal), "heuristic:fallback(no forge action)"


def _event_action(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]
                  ) -> tuple[dict[str, Any], str]:
    event = obs.get("event") or {}
    if event.get("resolved"):
        leave = _legal_match(_SHOP_LEAVE, legal)
        if leave is not None:
            return leave, "heuristic:event resolved → shop_leave"
    options = [o for o in event.get("options") or ()
               if isinstance(o, Mapping) and o.get("affordable")]
    ordered = [o for o in options if o.get("risk") == "positive"] or options[-1:]
    for option in reversed(ordered):
        match = _legal_match({"kind": "event_choose", "optionId": option.get("optionId")}, legal)
        if match is not None:
            return match, f"heuristic:event_choose {option.get('optionId')}"
    fallback = _first_kind(legal, "event_choose")
    if fallback is not None:
        return fallback, "heuristic:event_choose(first legal)"
    return _fallback(legal), "heuristic:fallback(no event_choose)"


def _choose_phase(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]
                  ) -> tuple[dict[str, Any], str]:
    phase = str(obs.get("phase") or "")
    if phase == "shrine":
        acts = [a for a in legal if isinstance(a, Mapping) and a.get("kind") == "shrine_choose"]
        if acts:
            acts.sort(key=lambda a: _SHRINE_PREF.index(str(a.get("optionId")))
                      if str(a.get("optionId")) in _SHRINE_PREF else 99)
            return dict(acts[0]), f"heuristic:shrine_choose {acts[0].get('optionId')}"
    if phase == "rest":
        acts = [a for a in legal if isinstance(a, Mapping) and a.get("kind") == "rest_choose"]
        if acts:
            acts.sort(key=lambda a: _SHRINE_PREF.index(str(a.get("optionId")))
                      if str(a.get("optionId")) in _SHRINE_PREF else 99)
            return dict(acts[0]), f"heuristic:rest_choose {acts[0].get('optionId')}"
    if phase == "relic_swap":
        skip = _legal_match({"kind": "relic_swap", "relicId": "skip"}, legal)
        if skip is not None:
            return skip, "heuristic:relic_swap skip"
    # 未单独建模的 phase（含 settle 边界）：取 legal 首项，绝不空转（PRD §1 目标 4）。
    return _fallback(legal), f"heuristic:fallback(phase={phase or 'unknown'})"


def _fallback(legal: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """最终兜底：``legal[0]``（MOD-02 保证同状态下顺序确定）。"""
    for item in legal:
        if isinstance(item, Mapping):
            return dict(item)
    return {}


def heuristic_policy_explained(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
                               hints: Mapping[str, Any] | None = None
                               ) -> tuple[dict[str, Any], str]:
    """``heuristic_policy`` 的可解释版本：返回 ``(action, reasoning)``。"""
    hints = hints or {}
    if not legal:
        return {}, "heuristic:no-legal-action"
    phase = str(obs.get("phase") or "")
    if phase == "player_turn":
        return _battle_action(obs, legal, hints)
    if phase == "map":
        return _map_action(obs, legal)
    if phase == "treasure":
        return _treasure_action(obs, legal)
    if phase == "shop":
        return _shop_action(obs, legal)
    if phase == "forge":
        return _forge_action(obs, legal)
    if phase == "event":
        return _event_action(obs, legal)
    return _choose_phase(obs, legal)


def heuristic_policy(obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
                     hints: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """启发式策略（Spec §4.7 唯一签名）：返回一个**合法** GAME 动作（dict，camelCase）。"""
    action, _ = heuristic_policy_explained(obs, legal, hints)
    return action
