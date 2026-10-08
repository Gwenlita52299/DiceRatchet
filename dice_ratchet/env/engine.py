"""``kernel/index.js`` 的逐位移植 —— 规则内核对外 API（GAME-01 §1）。

对外门面 ``GameEngine``（Spec §4.7 第 1 行）；模块级函数保留 JS 的 ``tables`` 显式参数
形式，便于逐行对照与单测。全部函数纯：不持有模块级可变状态（K-1）。
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from . import combat as C
from . import event as E
from . import forge as F
from . import map as M
from . import relic as R
from . import shop as S
from .counter import SCHOOLS, apply_mul, arrow_of, counter_of, school_mul_permille
from .digest import config_hash, digest
from .error import KernelError
from .loader import GAME_CONTENT_DIR, load_tables
from .risk import self_corruption_value
from .rng import choice, create_rng_state, randint
from .slots import SLOT_KIND_NAMES, describe_constraint
from .state import (assert_invariants, current_face, deck_school_info, die_def, die_faces,
                    die_risk_tier, empty_statuses, is_mono, new_die, remove_die_from_deck,
                    status_list)

__all__ = ["GameEngine", "KERNEL_VERSION", "OBS_SCHEMA", "ACTION_KINDS", "TOGGLE_KEYS",
           "apply_difficulty", "allowed_stage_counts", "validate_config", "digest", "config_hash",
           "state_hash", "map_error_code", "EnvError", "GAME_CONTENT_DIR"]

KERNEL_VERSION: Final[str] = "kernel-1.0.0"
OBS_SCHEMA: Final[str] = "obs-1.0.0"
TOGGLE_KEYS: Final[tuple[str, ...]] = ("mapBranching", "relics", "events", "shops", "schoolCounter")
ACTION_KINDS: Final[tuple[str, ...]] = (
    "map_choose", "play_face", "discard_die", "purify", "use_ability", "end_turn",
    "shop_buy", "shop_refresh", "shop_remove", "shop_leave", "event_choose", "treasure_pick",
    "forge_pick", "shrine_choose", "rest_choose", "relic_swap", "abandon_run",
)
_U32_MAX: Final[int] = 4294967296
_OPT_RE: Final[re.Pattern[str]] = re.compile(r"^opt_(\d+)$")

Ctx = dict[str, Any]


def _is_int(x: Any) -> bool:
    """JS ``Number.isInteger``：布尔不是整数。"""
    return isinstance(x, int) and not isinstance(x, bool)


class EnvError(KernelError):
    """对外错误（已按 FR-01-10 映射到 ``E_ENV_*``）；``detail`` 保留原 JS 内核码。"""

    def __init__(self, code: str, message: str, detail: str | None = None) -> None:
        super().__init__(code, message)
        self.detail = detail


def map_error_code(code: str) -> tuple[str, str | None]:
    """JS 内核码 → Python 对外码（FR-01-10）；返回 ``(py_code, detail)``。"""
    if code.startswith("E_KERNEL_RNG"):
        return "E_ENV_RNG", code
    if code in ("E_KERNEL_INVARIANT", "E_KERNEL_EXCEPTION", "E_KERNEL_DISPATCH"):
        return "E_ENV_INTERNAL", code
    if code.startswith("E_CONTENT_"):
        return "E_ENV_CONTENT_INVALID", code
    if code == "E_KERNEL_BAD_CONFIG":
        return "E_ENV_CONFIG_INVALID", None
    return code, None


# ======================================================================
# 配置
# ======================================================================

def apply_difficulty(base: Mapping, difficulty: str, tables: Mapping) -> dict[str, Any]:
    lv = tables["difficulty"]["levels"].get(difficulty)
    if not lv:
        raise KernelError("E_KERNEL_BAD_CONFIG", f"unknown difficulty {difficulty}")
    common = tables["difficulty"]["common"]
    base_toggles = base.get("toggles") or {}
    toggles = {}
    for k in TOGGLE_KEYS:
        v = base_toggles.get(k)
        toggles[k] = bool(v) if isinstance(v, bool) else True
    base_debug = base.get("debug")
    base_debug = base_debug if isinstance(base_debug, dict) else {}
    seed = base.get("seed")
    return {
        "configVersion": "cfg-1",
        "difficulty": difficulty,
        "seed": (seed & 0xFFFFFFFF) if _is_int(seed) else 0,
        "character": base.get("character") or "warrior",
        "toggles": toggles,
        "debug": {
            "showHiddenInfo": bool(base_debug.get("showHiddenInfo")),
            "logActions": base_debug.get("logActions") is not False,
        },
        "heartsMax": lv["heartsMax"], "virtueMax": lv["virtueMax"],
        "drawPerTurn": lv["drawPerTurn"], "handMax": lv["handMax"],
        "enemyHpMulPermille": lv["enemyHpMulPermille"], "enemyDmgMulPermille": lv["enemyDmgMulPermille"],
        "enemyExtraDice": lv["enemyExtraDice"],
        "counterIntensity": lv["counterIntensity"],
        "counterMulPermille": {"advantage": lv["counterMulPermille"]["advantage"],
                               "disadvantage": lv["counterMulPermille"]["disadvantage"]},
        "monoPenaltyPermille": lv["monoPenaltyPermille"],
        "monoThresholdPermille": common["monoThresholdPermille"],
        "monoMinDice": common["monoMinDice"],
        "shopPriceMulPermille": lv["shopPriceMulPermille"], "goldMulPermille": lv["goldMulPermille"],
        "relicSlots": lv["relicSlots"], "purifyPerStage": lv["purifyPerStage"],
        "eliteStages": list(lv["eliteStages"]),
        "bossPhaseHpBonusPermille": lv["bossPhaseHpBonusPermille"], "reviveOnce": lv["reviveOnce"],
        "stageCount": common["stageCount"] if base.get("stageCount") is None else base["stageCount"],
        "maxTurnsPerBattle": common["maxTurnsPerBattle"],
    }


def allowed_stage_counts(tables: Mapping) -> list[int]:
    """允许的局长（D-34）：只能在章末 Boss 关截断。"""
    return sorted(s["stage"] for s in tables["stages"]["stages"] if s.get("boss"))


def validate_config(config: Mapping, tables: Mapping) -> dict[str, Any]:
    errors: list[str] = []
    if config["difficulty"] not in tables["difficulty"]["levels"]:
        errors.append("difficulty")
    if not _is_int(config["seed"]) or config["seed"] < 0 or config["seed"] >= _U32_MAX:
        errors.append("seed")
    if config["character"] not in tables["characters"]:
        errors.append("character")
    for k in TOGGLE_KEYS:
        if not isinstance(config["toggles"].get(k), bool):
            errors.append(f"toggles.{k}")
    if config["stageCount"] not in allowed_stage_counts(tables):
        errors.append("stageCount")
    return {"ok": not errors, "errors": errors}


# ======================================================================
# createGame
# ======================================================================

def create_game(input_: Mapping, tables: Mapping) -> dict[str, Any]:
    config = input_ if input_.get("configVersion") else apply_difficulty(
        input_, input_.get("difficulty") or "medium", tables)
    v = validate_config(config, tables)
    if not v["ok"]:
        raise KernelError("E_KERNEL_BAD_CONFIG", ",".join(v["errors"]))
    ch = tables["characters"][config["character"]]
    if not len(ch["startingDeck"]):
        raise KernelError("E_KERNEL_EMPTY_DECK")
    state: dict[str, Any] = {
        "stateVersion": "gs-1",
        "runId": input_.get("runId") or f"r-{config_hash(config)[7:19]}",
        "configHash": config_hash(config),
        "config": config,
        "stage": 1, "chapter": 1, "turn": 0, "steps": 0,
        "phase": "map",
        "player": {
            "hearts": config["heartsMax"] + ch["heartsBonus"],
            "heartsMax": config["heartsMax"] + ch["heartsBonus"],
            "virtue": config["virtueMax"] + ch["virtueBonus"],
            "virtueMax": config["virtueMax"] + ch["virtueBonus"],
            "ward": 0, "starShards": 0, "statuses": empty_statuses(), "charge": 0,
            "purifyLeft": config["purifyPerStage"], "abilities": [],
            "deck": [], "drawPile": [], "discardPile": [], "hand": [], "dice": {},
            "slots": [{**s, "reusable": bool(s.get("reusable")), "filledBy": None,
                       "disabledUntilTurn": None} for s in ch["startingSlots"]],
            "relics": [], "removeCount": 0,
            "pendingVirtue": 0, "pendingStatuses": {}, "pendingDraw": 0,
            "firstPlayDone": False, "bladeAttackedThisTurn": False,
            "arcanaLastTurn": 0, "arcanaThisTurn": 0, "drainNext": 0, "reviveUsed": False,
        },
        "enemies": [],
        "map": None,
        "node": None,
        "rng": create_rng_state(config["seed"]),
        "dieSeq": 0, "shopSeq": 0, "slotSeq": 0,
        "seenEvents": [],
        "pendingRelic": None,
        "stats": {"turns": 0, "damageDealt": 0, "kills": 0, "counterHits": 0,
                  "skillFaceUses": 0, "battlesWon": 0, "battlesLost": 0, "stagesCleared": 0},
        "illegalCount": 0, "consecutiveIllegal": 0,
        "outcome": None, "outcomeReason": None, "truncationReason": None,
    }
    for def_id in ch["startingDeck"]:
        state["player"]["deck"].append(new_die(state, def_id))
    state["map"] = M.generate_maps(state, tables)
    return state


# ======================================================================
# 节点流程
# ======================================================================

def _enter_node(ctx: Ctx, node: Mapping) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    state["node"] = {"nodeId": node["nodeId"], "kind": node["kind"], "queue": ["complete"]}
    events.append({"type": "node_enter", "nodeId": node["nodeId"], "kind": node["kind"]})
    kind = node["kind"]
    if kind in ("battle", "elite", "boss"):
        C.start_battle(ctx, M.group_members(tables, node["enemyGroupId"]), kind)
    elif kind == "shop":
        S.enter_shop(ctx)
    elif kind == "forge":
        F.enter_forge(ctx, False)
    elif kind == "event":
        if not E.enter_event(ctx):
            state["node"]["kind"] = "treasure"
            S.enter_treasure(ctx)
    elif kind == "treasure":
        S.enter_treasure(ctx)
    elif kind == "shrine":
        S.enter_shrine(ctx)
    elif kind == "rest":
        S.enter_rest(ctx)
    else:
        raise KernelError("E_KERNEL_NODE_KIND", kind)


def _complete_node(ctx: Ctx) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    events.append({"type": "node_leave",
                   "nodeId": state["node"]["nodeId"] if state.get("node") else None})
    state["node"] = None
    state["enemies"] = []
    sm = M.current_stage_map(state)
    if state["map"]["currentRow"] >= len(sm["rows"]) - 1:
        state["stats"]["stagesCleared"] += 1
        if state["stage"] >= state["config"]["stageCount"]:
            state["outcome"] = "win"
            state["phase"] = "settle"
            return
        state["stage"] += 1
        state["chapter"] = M.stage_def(tables, state["stage"])["chapter"]
        state["map"]["currentRow"] = -1
        state["map"]["currentNodeId"] = None
        events.append({"type": "stage_enter", "stage": state["stage"], "chapter": state["chapter"]})
    state["phase"] = "map"


def _run_queue(ctx: Ctx) -> None:
    state = ctx["state"]
    while state.get("node") and state["node"]["queue"]:
        step = state["node"]["queue"].pop(0)
        if step == "complete":
            _complete_node(ctx)
            return
        if step == "treasure":
            S.enter_treasure(ctx)
            return
        if step.startswith("relic:"):
            if not R.grant_relic(ctx, step[len("relic:"):], None):
                return


def _reset_battle_state(state: dict[str, Any]) -> None:
    p = state["player"]
    for die_id in list(p["deck"]):
        if p["dice"][die_id].get("temporary"):
            remove_die_from_deck(state, die_id)
    p["drawPile"] = []
    p["discardPile"] = []
    p["hand"] = []
    for die_id in p["deck"]:
        p["dice"][die_id]["isCorrupt"] = False
    p["ward"] = 0
    p["statuses"] = empty_statuses()
    p["charge"] = 0
    for s in p["slots"]:
        s["filledBy"] = None
        s["disabledUntilTurn"] = None
    p["virtue"] = p["virtueMax"]
    state["turn"] = 0


def _finish_battle(ctx: Ctx) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    b = state["node"]["battle"]
    result = b["result"]
    kind = b["kind"]
    enemies = state["enemies"]
    _reset_battle_state(state)
    state["node"]["battle"] = None
    if result == "dead":
        events.append({"type": "battle_end", "result": "lose"})
        state["outcome"] = "lose"
        state["outcomeReason"] = "hearts"
        state["phase"] = "settle"
        state["stats"]["battlesLost"] += 1
        return
    if result in ("lost", "truncated"):
        state["stats"]["battlesLost"] += 1
        events.append({"type": "battle_end", "result": result,
                       "heartsLeft": state["player"]["hearts"]})
        state["node"]["queue"] = ["complete"]
        _run_queue(ctx)
        return
    state["stats"]["battlesWon"] += 1
    # 胜利掉落（loot 流）
    gold = 0
    for e in enemies:
        lo, hi = tables["shops"]["loot"][e["tier"]]
        gold += randint(state, "loot", lo, hi)
    gold = apply_mul(gold, R.gold_mul(state, tables))
    state["player"]["starShards"] += gold
    events.append({"type": "battle_end", "result": "win", "drops": {"starShards": gold}})
    if kind == "boss" and state["stage"] >= state["config"]["stageCount"]:
        state["stats"]["stagesCleared"] += 1  # D-35：最终 Boss 胜利也计入通过关数
        state["outcome"] = "win"
        state["phase"] = "settle"
        state["node"] = None
        return
    queue: list[str] = []
    if kind in ("elite", "boss") and state["config"]["toggles"]["relics"]:
        pool = R.relic_pool(state, tables, ["boss", "rare"] if kind == "boss" else ["common", "rare"])
        if pool:
            queue.append(f"relic:{choice(state, 'loot', pool)}")
    queue.append("treasure")  # 战斗胜利：骰子 3 选 1（loot 流）
    queue.append("complete")
    state["node"]["queue"] = queue
    state["phase"] = "map"
    _run_queue(ctx)


# ======================================================================
# 合法性
# ======================================================================

def _illegal(code: str, reason: str) -> dict[str, Any]:
    return {"legal": False, "code": code, "reason": reason}


_OK: Final[dict[str, Any]] = {"legal": True, "code": None, "reason": None}


def normalize_action(state: Mapping, action: Mapping, tables: Mapping) -> dict[str, Any]:
    """规范化动作：补全默认目标（便于 UI/自动化客户端）。"""
    if not action or not isinstance(action, Mapping):
        return action  # type: ignore[return-value]
    a = dict(action)
    if (a.get("kind") == "play_face" and state["phase"] == "player_turn"
            and a.get("dieId") in state["player"]["hand"]):
        need = C.face_needs_target(current_face(state, tables, a["dieId"]))
        if a.get("targetId") is None:
            if need == "enemy":
                alive = C.alive_enemies(state)
                a["targetId"] = alive[0]["enemyId"] if alive else None
            elif need == "die":
                a["targetId"] = next((d for d in state["player"]["hand"] if d != a["dieId"]), None)
            else:
                a["targetId"] = None
    return a


def is_legal(state: Mapping, action: Mapping, tables: Mapping) -> dict[str, Any]:
    if not action or not isinstance(action, Mapping) or action.get("kind") not in ACTION_KINDS:
        return _illegal("E_ACTION_SYNTAX", "unknown action kind")
    if state["phase"] == "settle":
        return _illegal("E_ACTION_PHASE", "对局已结束")
    a = normalize_action(state, action, tables)
    p = state["player"]
    phase = state["phase"]

    def phase_is(*ph: str) -> bool:
        return phase in ph

    kind = a["kind"]
    if kind == "abandon_run":
        return _OK
    if kind == "map_choose":
        if not phase_is("map"):
            return _illegal("E_ACTION_PHASE", "不在地图阶段")
        if a.get("nodeId") not in M.reachable_node_ids(state):
            return _illegal("E_ACTION_NODE_UNREACHABLE", "节点不可达")
        return _OK
    if kind == "play_face":
        if not phase_is("player_turn"):
            return _illegal("E_ACTION_PHASE", "不在玩家回合")
        r = C.check_play_face(state, tables, a)
        return _illegal(r["code"], r["reason"]) if r else _OK
    if kind == "discard_die":
        if not phase_is("player_turn"):
            return _illegal("E_ACTION_PHASE", "不在玩家回合")
        if a.get("dieId") not in p["hand"]:
            return _illegal("E_ACTION_DIE_NOT_IN_HAND", "骰子不在手中")
        return _OK
    if kind == "purify":
        if not phase_is("player_turn"):
            return _illegal("E_ACTION_PHASE", "不在玩家回合")
        if a.get("dieId") not in p["hand"]:
            return _illegal("E_ACTION_DIE_NOT_IN_HAND", "骰子不在手中")
        if not p["dice"][a["dieId"]]["isCorrupt"]:
            return _illegal("E_ACTION_NOT_CORRUPT", "不是魔化骰")
        if p["purifyLeft"] <= 0:
            return _illegal("E_ACTION_NO_PURIFY", "净化次数已用完")
        return _OK
    if kind == "use_ability":
        if not phase_is("player_turn"):
            return _illegal("E_ACTION_PHASE", "不在玩家回合")
        ab = next((x for x in p["abilities"] if x["id"] == a.get("abilityId")), None)
        if ab is None:
            return _illegal("E_ACTION_UNKNOWN_ABILITY", "未知能力")
        if ab["usesLeft"] <= 0:
            return _illegal("E_ACTION_ABILITY_EXHAUSTED", "能力次数已用完")
        if a["abilityId"] == "warrior_reroll" and not p["hand"]:
            return _illegal("E_ACTION_ABILITY_NO_TARGET", "手中没有骰子")
        if a["abilityId"] == "warrior_focus":
            if a.get("dieId") not in p["hand"]:
                return _illegal("E_ACTION_DIE_NOT_IN_HAND", "骰子不在手中")
            if not _is_int(a.get("value")) or a["value"] < 1 or a["value"] > 6:
                return _illegal("E_ACTION_SYNTAX", "value 1..6")
            if p["dice"][a["dieId"]]["faceIndex"] == a["value"] - 1:
                return _illegal("E_ACTION_NOOP", "点数未改变")
        return _OK
    if kind == "end_turn":
        if not phase_is("player_turn"):
            return _illegal("E_ACTION_PHASE", "不在玩家回合")
        left = C.corrupt_remaining(state)
        if left:
            return _illegal("E_ACTION_CORRUPT_DIE_REMAINS",
                            f"corrupt_die_remains: 还有 {len(left)} 颗魔化骰未处理")
        return _OK
    if kind == "shop_buy":
        if not phase_is("shop"):
            return _illegal("E_ACTION_PHASE", "不在商店")
        it = next((x for x in state["node"]["shop"]["items"] if x["itemId"] == a.get("itemId")), None)
        if it is None or it["sold"]:
            return _illegal("E_ACTION_UNKNOWN_ITEM", "商品不存在")
        if p["starShards"] < it["price"]:
            return _illegal("E_ACTION_NOT_AFFORDABLE", "星屑不足")
        return _OK
    if kind == "shop_refresh":
        if not phase_is("shop"):
            return _illegal("E_ACTION_PHASE", "不在商店")
        if state["node"]["shop"]["refreshLeft"] <= 0:
            return _illegal("E_ACTION_NO_REFRESH", "刷新次数已用完")
        if p["starShards"] < S.refresh_price(state, tables):
            return _illegal("E_ACTION_NOT_AFFORDABLE", "星屑不足")
        return _OK
    if kind == "shop_remove":
        if not phase_is("shop"):
            return _illegal("E_ACTION_PHASE", "不在商店")
        if a.get("dieId") not in S.removable_dice(state, tables):
            return _illegal("E_ACTION_NOT_REMOVABLE", "该骰子不可删除")
        if p["starShards"] < S.remove_price(state, tables):
            return _illegal("E_ACTION_NOT_AFFORDABLE", "星屑不足")
        return _OK
    if kind == "shop_leave":
        if phase_is("shop", "forge"):
            return _OK
        if phase_is("event") and state["node"]["event"]["resolved"]:
            return _OK
        return _illegal("E_ACTION_PHASE", "当前不能离开")
    if kind == "event_choose":
        if not phase_is("event") or state["node"]["event"]["resolved"]:
            return _illegal("E_ACTION_PHASE", "不在事件中")
        ev = tables["events"][state["node"]["event"]["eventId"]]
        m = _OPT_RE.match(str(a.get("optionId") or ""))
        if not m:
            return _illegal("E_ACTION_UNKNOWN_OPTION", "未知选项")
        idx = int(m.group(1))
        opt = ev["options"][idx] if idx < len(ev["options"]) else None
        if opt is None:
            return _illegal("E_ACTION_UNKNOWN_OPTION", "未知选项")
        if not E.requirement_met(state, opt.get("requirement")):
            return _illegal("E_ACTION_REQUIREMENT", "不满足选项要求")
        return _OK
    if kind == "treasure_pick":
        if not phase_is("treasure"):
            return _illegal("E_ACTION_PHASE", "不在宝箱")
        if a.get("defId") != "skip" and a.get("defId") not in state["node"]["treasure"]["offers"]:
            return _illegal("E_ACTION_UNKNOWN_ITEM", "无此选项")
        return _OK
    if kind == "forge_pick":
        if not phase_is("forge"):
            return _illegal("E_ACTION_PHASE", "不在锻造")
        f = state["node"]["forge"]
        if f["done"]:
            return _illegal("E_ACTION_FORGE_DONE", "本次已锻造")
        if a.get("dieId") not in F.forgeable_dice(state, tables):
            return _illegal("E_ACTION_NOT_FORGEABLE", "该骰子不可锻造")
        if not _is_int(a.get("faceIndex")) or a["faceIndex"] < 0 or a["faceIndex"] > 5:
            return _illegal("E_ACTION_SYNTAX", "faceIndex")
        if a.get("candidateId") not in f["candidates"]:
            return _illegal("E_ACTION_UNKNOWN_ITEM", "无此候选面")
        if p["starShards"] < f["price"]:
            return _illegal("E_ACTION_NOT_AFFORDABLE", "星屑不足")
        return _OK
    if kind == "shrine_choose":
        if not phase_is("shrine"):
            return _illegal("E_ACTION_PHASE", "不在神殿")
        o = next((x for x in state["node"]["shrine"]["options"]
                  if x["optionId"] == a.get("optionId")), None)
        if o is None or not o["available"]:
            return _illegal("E_ACTION_UNKNOWN_OPTION", "选项不可用")
        return _OK
    if kind == "rest_choose":
        if not phase_is("rest"):
            return _illegal("E_ACTION_PHASE", "不在休息点")
        o = next((x for x in state["node"]["rest"]["options"]
                  if x["optionId"] == a.get("optionId")), None)
        if o is None or not o["available"]:
            return _illegal("E_ACTION_UNKNOWN_OPTION", "选项不可用")
        return _OK
    if kind == "relic_swap":
        if not phase_is("relic_swap"):
            return _illegal("E_ACTION_PHASE", "无需替换遗物")
        if a.get("relicId") != "skip" and a.get("relicId") not in p["relics"]:
            return _illegal("E_ACTION_UNKNOWN_ITEM", "未拥有该遗物")
        return _OK
    return _illegal("E_ACTION_SYNTAX", "unknown")


def legal_actions(state: Mapping, tables: Mapping) -> list[dict[str, Any]]:
    """枚举全部合法动作（候选 → isLegal 过滤，保证与校验同源，顺序与 JS 一致）。"""
    cands: list[dict[str, Any]] = []
    p = state["player"]
    phase = state["phase"]
    if phase == "map":
        for node_id in M.reachable_node_ids(state):
            cands.append({"kind": "map_choose", "nodeId": node_id})
    elif phase == "player_turn":
        enemies = [e["enemyId"] for e in C.alive_enemies(state)]
        for die_id in p["hand"]:
            need = C.face_needs_target(current_face(state, tables, die_id))
            if need == "enemy":
                targets: list[Any] = enemies
            elif need == "die":
                others = [d for d in p["hand"] if d != die_id]
                targets = others if others else [None]
            else:
                targets = [None]
            for s in p["slots"]:
                for t in targets:
                    cands.append({"kind": "play_face", "dieId": die_id, "slotId": s["slotId"],
                                  "targetId": t})
            cands.append({"kind": "discard_die", "dieId": die_id})
            cands.append({"kind": "purify", "dieId": die_id})
        cands.append({"kind": "use_ability", "abilityId": "warrior_reroll"})
        for die_id in p["hand"]:
            for v in range(1, 7):
                cands.append({"kind": "use_ability", "abilityId": "warrior_focus",
                              "dieId": die_id, "value": v})
        cands.append({"kind": "end_turn"})
    elif phase == "shop":
        for it in state["node"]["shop"]["items"]:
            cands.append({"kind": "shop_buy", "itemId": it["itemId"]})
        cands.append({"kind": "shop_refresh"})
        for die_id in p["deck"]:
            cands.append({"kind": "shop_remove", "dieId": die_id})
        cands.append({"kind": "shop_leave"})
    elif phase == "forge":
        if not state["node"]["forge"]["done"]:
            for die_id in F.forgeable_dice(state, tables):
                for fi in range(6):
                    for cand in state["node"]["forge"]["candidates"]:
                        cands.append({"kind": "forge_pick", "dieId": die_id, "faceIndex": fi,
                                      "candidateId": cand})
        cands.append({"kind": "shop_leave"})
    elif phase == "event":
        ev = tables["events"][state["node"]["event"]["eventId"]]
        for i in range(len(ev["options"])):
            cands.append({"kind": "event_choose", "optionId": f"opt_{i}"})
        cands.append({"kind": "shop_leave"})
    elif phase == "treasure":
        for d in state["node"]["treasure"]["offers"]:
            cands.append({"kind": "treasure_pick", "defId": d})
        cands.append({"kind": "treasure_pick", "defId": "skip"})
    elif phase == "shrine":
        for o in state["node"]["shrine"]["options"]:
            cands.append({"kind": "shrine_choose", "optionId": o["optionId"]})
    elif phase == "rest":
        for o in state["node"]["rest"]["options"]:
            cands.append({"kind": "rest_choose", "optionId": o["optionId"]})
    elif phase == "relic_swap":
        for r in p["relics"]:
            cands.append({"kind": "relic_swap", "relicId": r})
        cands.append({"kind": "relic_swap", "relicId": "skip"})

    out = [a for a in cands if is_legal(state, a, tables)["legal"]]
    if phase != "settle":
        out.append({"kind": "abandon_run"})
    return out


# ======================================================================
# reduce
# ======================================================================

def _dispatch(ctx: Ctx, a: Mapping) -> None:
    state = ctx["state"]
    kind = a["kind"]
    if kind == "abandon_run":
        state["outcome"] = "lose"
        state["outcomeReason"] = "abandon"
        state["phase"] = "settle"
        ctx["events"].append({"type": "run_abandoned"})
    elif kind == "map_choose":
        node = M.find_node(state, a["nodeId"])
        node["visited"] = True
        state["map"]["currentRow"] = node["row"]
        state["map"]["currentNodeId"] = node["nodeId"]
        state["map"]["path"].append(node["nodeId"])
        _enter_node(ctx, node)
    elif kind == "play_face":
        C.play_face(ctx, a)
    elif kind == "discard_die":
        C.discard_die(ctx, a["dieId"])
    elif kind == "purify":
        C.purify_die(ctx, a["dieId"])
    elif kind == "use_ability":
        C.use_ability(ctx, a)
    elif kind == "end_turn":
        C.end_turn(ctx)
    elif kind == "shop_buy":
        S.shop_buy(ctx, a["itemId"])
    elif kind == "shop_refresh":
        S.shop_refresh(ctx)
    elif kind == "shop_remove":
        S.shop_remove(ctx, a["dieId"])
    elif kind == "shop_leave":
        _run_queue(ctx)
    elif kind == "event_choose":
        E.event_choose(ctx, a["optionId"])
    elif kind == "treasure_pick":
        S.treasure_pick(ctx, a["defId"])
        _run_queue(ctx)
    elif kind == "forge_pick":
        F.forge_pick(ctx, a)
    elif kind == "shrine_choose":
        if S.shrine_choose(ctx, a["optionId"]):
            _run_queue(ctx)
    elif kind == "rest_choose":
        S.rest_choose(ctx, a["optionId"])
        _run_queue(ctx)
    elif kind == "relic_swap":
        ret = R.swap_relic(ctx, a["relicId"])
        if ret:
            state["phase"] = ret
        else:
            _run_queue(ctx)
    else:
        raise KernelError("E_KERNEL_DISPATCH", kind)


def reduce_state(state: Mapping, action: Mapping, tables: Mapping) -> dict[str, Any]:
    """``reduce(state, action, tables) -> {state, events, illegal, code, reason, error}``。

    不合法时返回原 state 与 ``illegal=True``，不抛异常；内核异常时 ``error`` 有值且 state 不变。
    """
    verdict = is_legal(state, action, tables)
    if not verdict["legal"]:
        return {
            "state": state,
            "events": [{"type": "illegal", "code": verdict["code"], "reason": verdict["reason"]}],
            "illegal": True, "code": verdict["code"], "reason": verdict["reason"],
        }
    try:
        draft = copy.deepcopy(state)
        ctx: Ctx = {"state": draft, "tables": tables, "events": []}
        a = normalize_action(draft, action, tables)
        draft["steps"] += 1
        _dispatch(ctx, a)
        if draft.get("node") and draft["node"].get("battle") and draft["node"]["battle"].get("result"):
            _finish_battle(ctx)
        if draft["config"]["debug"]["showHiddenInfo"]:
            assert_invariants(draft)
        return {"state": draft, "events": ctx["events"], "illegal": False}
    except KernelError as err:
        code = err.code if err.code.startswith("E_KERNEL") else "E_KERNEL_EXCEPTION"
        py_code, detail = map_error_code(code)
        return {
            "state": state,
            "events": [{"type": "error", "code": code, "reason": str(err)}],
            "illegal": False,
            "error": {"code": py_code, "message": str(err), "detail": detail},
        }
    except Exception as err:  # noqa: BLE001 - 内核异常统一捕获（与 JS 的 catch 同语义）
        py_code, detail = map_error_code("E_KERNEL_EXCEPTION")
        return {
            "state": state,
            "events": [{"type": "error", "code": "E_KERNEL_EXCEPTION", "reason": str(err)}],
            "illegal": False,
            "error": {"code": py_code, "message": str(err), "detail": detail},
        }


def debug_start_battle(state: Mapping, member_ids: list[str], tables: Mapping,
                       kind: str = "battle") -> dict[str, Any]:
    """测试/平衡脚本用：在当前 state 上直接开启一场指定编队的战斗（不经地图）。"""
    draft = copy.deepcopy(state)
    ctx: Ctx = {"state": draft, "tables": tables, "events": []}
    draft["node"] = {"nodeId": "n-debug", "kind": kind, "queue": ["complete"]}
    C.start_battle(ctx, member_ids, kind)
    return draft


def replay(config: Mapping, actions: Sequence[Mapping], tables: Mapping) -> dict[str, Any]:
    state = create_game(config, tables)
    steps = [{"obsDigest": digest(observe(state, "full", tables)), "events": []}]
    for a in actions:
        r = reduce_state(state, a, tables)
        state = r["state"]
        steps.append({"obsDigest": digest(observe(state, "full", tables)), "events": r["events"]})
    return {"steps": steps, "digests": [s["obsDigest"] for s in steps],
            "outcome": state["outcome"], "state": state}


# ======================================================================
# observe（白名单显式构造，GAME-01 §5）
# ======================================================================

def face_view(f: Mapping) -> dict[str, Any]:
    return {
        "faceIndex": f["faceIndex"], "label": f["label"], "kind": f["kind"],
        "value": f["value"], "school": f["school"], "target": f["target"],
        "statusKind": f.get("statusKind") or None,
        "selfCorruption": self_corruption_value(f),
        "tags": list(f.get("tags") or []),
        "forged": bool(f.get("forged")),
    }


def die_summary(state: Mapping, tables: Mapping, die_id: str) -> dict[str, Any]:
    die = state["player"]["dice"][die_id]
    def_ = die_def(tables, die["defId"])
    return {
        "dieId": die_id, "defId": die["defId"], "name": def_["name"],
        "category": def_["category"], "school": def_["school"],
        "riskTier": die_risk_tier(state, tables, die_id), "forged": bool(die.get("forged")),
        "faces": [face_view(f) for f in die_faces(state, tables, die_id)],
    }


def hand_die_view(state: Mapping, tables: Mapping, die_id: str) -> dict[str, Any]:
    die = state["player"]["dice"][die_id]
    f = current_face(state, tables, die_id)
    def_ = die_def(tables, die["defId"])
    return {
        "dieId": die_id, "defId": die["defId"], "name": def_["name"],
        "category": def_["category"], "school": f["school"], "dieSchool": def_["school"],
        "riskTier": die_risk_tier(state, tables, die_id),
        "faceIndex": die["faceIndex"], "pip": die["faceIndex"] + 1,
        "label": f["label"], "kind": f["kind"], "value": f["value"],
        "statusKind": f.get("statusKind") or None, "target": f["target"],
        "isCorrupt": die["isCorrupt"],
        "corruptionValue": self_corruption_value(f),
        "needsTarget": C.face_needs_target(f),
        "faces": [face_view(x) for x in die_faces(state, tables, die_id)],
    }


def enemy_intent_view(state: Mapping, tables: Mapping, e: Mapping) -> list[dict[str, Any]]:
    out = []
    for it in e["intent"]:
        base = {"slotId": it["slotId"], "pip": it["pip"], "kind": it["kind"],
                "school": e["school"], "hits": it["hits"]}
        if it["kind"] == "purge":
            r = C.enemy_attack_value(state, tables, e, it["value"])
            out.append({**base, "value": r["value"], "raw": r["raw"],
                        "schoolMul": r["mulPermille"], "monoPermille": r["monoPermille"],
                        "arrow": r["arrow"] if state["config"]["toggles"]["schoolCounter"] else "eq"})
        else:
            out.append({**base, "value": it["value"],
                        "statusKind": it.get("statusKind") or None,
                        "target": it.get("target") or None, "arrow": "eq"})
    return out


def map_view(state: Mapping, tables: Mapping) -> dict[str, Any]:
    sm = M.current_stage_map(state)
    reach = M.reachable_node_ids(state)
    deck = deck_school_info(state, tables)["school"]
    rows: list[Any] = []
    for r, row in enumerate(sm["rows"]):
        if r > max(state["map"]["currentRow"], 0) + 1:
            rows.append({"count": len(row), "reachable": False})
            continue
        out_row = []
        for n in row:
            view = {
                "nodeId": n["nodeId"], "kind": n["kind"], "visited": n["visited"],
                "reachable": state["phase"] == "map" and n["nodeId"] in reach,
                "edges": list(n["edges"]), "preview": None, "school": None,
            }
            if n["enemyGroupId"]:
                members = [tables["monsters"][i] for i in M.group_members(tables, n["enemyGroupId"])]
                school = members[0]["phases"][0]["school"] if members[0].get("phases") else members[0]["school"]
                mul_in = school_mul_permille(state["config"], school, deck)
                view["school"] = school
                view["preview"] = {
                    "enemyNames": [m["name"] for m in members],
                    "schools": [m["phases"][0]["school"] if m.get("phases") else m["school"] for m in members],
                    "arrow": arrow_of(mul_in) if state["config"]["toggles"]["schoolCounter"] else "eq",
                }
            out_row.append(view)
        rows.append(out_row)
    return {"stage": sm["stage"], "chapter": sm["chapter"], "rows": rows,
            "currentRow": state["map"]["currentRow"],
            "currentNodeId": state["map"]["currentNodeId"],
            "path": list(state["map"]["path"])}


def battle_hints(state: Mapping, tables: Mapping) -> list[str]:
    hints: list[str] = []
    if not state["config"]["toggles"]["schoolCounter"] or not state["enemies"]:
        return hints
    if M.stage_def(tables, state["stage"]).get("tutorial"):
        return hints  # 教学关不提示学派盲区
    owned: set[str] = set()
    for die_id in state["player"]["deck"]:
        for f in die_faces(state, tables, die_id):
            owned.add(f["school"])
    names = tables["schools"]["schools"]
    seen: set[str] = set()
    for e in C.alive_enemies(state):
        c = counter_of(e["school"])
        if c and c not in owned and e["school"] not in seen:
            seen.add(e["school"])
            hints.append(f"你的骰池没有能克制{names[e['school']]['name']}的骰子")
    if (is_mono(state, tables) and state["config"]["monoPenaltyPermille"] > 0
            and not R.mono_penalty_disabled(state)):
        hints.append(f"骰池学派过于集中：敌人伤害 +{state['config']['monoPenaltyPermille'] // 10}%")
    return hints


def node_views(state: Mapping, tables: Mapping) -> dict[str, Any]:
    out: dict[str, Any] = {"shop": None, "event": None, "treasure": None,
                           "forge": None, "shrine": None, "rest": None}
    n = state.get("node")
    if not n:
        return out
    p = state["player"]
    if n.get("shop") and state["phase"] == "shop":
        items = []
        for it in n["shop"]["items"]:
            base = {"itemId": it["itemId"], "kind": it["kind"], "defId": it["defId"],
                    "price": it["price"], "sold": it["sold"],
                    "affordable": p["starShards"] >= it["price"]}
            if it["kind"] == "die":
                d = tables["skills"][it["defId"]]
                items.append({**base, "name": d["name"], "school": d["school"],
                              "riskTier": d["riskTier"], "faces": [face_view(f) for f in d["faces"]]})
            else:
                r = tables["relics"][it["defId"]]
                items.append({**base, "name": r["name"], "desc": r["desc"],
                              "rarity": r["rarity"], "school": None})
        out["shop"] = {
            "items": items,
            "refreshLeft": n["shop"]["refreshLeft"],
            "refreshPrice": S.refresh_price(state, tables),
            "removePrice": S.remove_price(state, tables),
            "removable": S.removable_dice(state, tables),
            "counterSchool": counter_of(S.stage_theme(state, tables)),
        }
    if n.get("event") and state["phase"] in ("event", "relic_swap"):
        ev = tables["events"][n["event"]["eventId"]]
        out["event"] = {
            "eventId": ev["id"], "title": ev["title"], "text": ev["text"], "school": ev["school"],
            "resolved": n["event"]["resolved"], "resultText": n["event"]["resultText"],
            "chosen": n["event"]["chosen"],
            "options": [
                {"optionId": f"opt_{i}", "label": o["label"],
                 "requirement": E.requirement_text(o.get("requirement")),
                 "affordable": E.requirement_met(state, o.get("requirement")),
                 "risk": o["risk"]}
                for i, o in enumerate(ev["options"])
            ],
        }
    if n.get("treasure") and state["phase"] == "treasure":
        out["treasure"] = {
            "offers": [
                {"defId": did, "name": tables["skills"][did]["name"],
                 "school": tables["skills"][did]["school"],
                 "riskTier": tables["skills"][did]["riskTier"],
                 "faces": [face_view(f) for f in tables["skills"][did]["faces"]],
                 "counter": tables["skills"][did]["school"] == n["treasure"]["counterSchool"]}
                for did in n["treasure"]["offers"]
            ],
            "counterSchool": n["treasure"]["counterSchool"],
            "skipGold": apply_mul(tables["shops"]["treasure"]["skipGold"], R.gold_mul(state, tables)),
        }
    if n.get("forge") and state["phase"] == "forge":
        out["forge"] = {
            "price": n["forge"]["price"], "done": n["forge"]["done"],
            "affordable": p["starShards"] >= n["forge"]["price"],
            "candidates": [
                {"candidateId": cid, **face_view({**tables["forgeFaces"][cid]["face"], "faceIndex": 0})}
                for cid in n["forge"]["candidates"]
            ],
            "forgeable": F.forgeable_dice(state, tables),
        }
    if n.get("shrine") and state["phase"] == "shrine":
        out["shrine"] = {"options": [{"optionId": o["optionId"], "label": o["label"],
                                      "available": o["available"]} for o in n["shrine"]["options"]]}
    if n.get("rest") and state["phase"] == "rest":
        out["rest"] = {"options": [{**o} for o in n["rest"]["options"]]}
    return out


def observe(state: Mapping, view: str, tables: Mapping) -> dict[str, Any]:
    p = state["player"]
    c = state["config"]
    ds = deck_school_info(state, tables)
    in_battle = state["phase"] in ("player_turn", "enemy_turn")
    nodes = node_views(state, tables)
    obs: dict[str, Any] = {
        "schemaVersion": OBS_SCHEMA,
        "runId": state["runId"],
        "phase": state["phase"],
        "stage": state["stage"],
        "chapter": state["chapter"],
        "chapterName": M.chapter_def(tables, state["chapter"])["name"],
        "turn": state["turn"],
        "difficulty": c["difficulty"],
        "player": {
            "hearts": p["hearts"], "heartsMax": p["heartsMax"], "virtue": p["virtue"],
            "virtueMax": p["virtueMax"], "ward": p["ward"], "starShards": p["starShards"],
            "statuses": status_list(p["statuses"]), "charge": p["charge"],
            "deckSchool": ds["school"], "deckSchoolCounts": {**ds["counts"]},
            "deckConcentrationPermille": ds["concentrationPermille"],
            "purifyLeft": p["purifyLeft"],
            "abilities": [
                *[{"id": a["id"], "usesLeft": a["usesLeft"] if in_battle else 0} for a in p["abilities"]],
                {"id": "warrior_purify", "usesLeft": p["purifyLeft"] if in_battle else 0},
            ],
            "pendingVirtue": p["pendingVirtue"], "pendingDraw": p["pendingDraw"],
            "pendingStatuses": status_list({**empty_statuses(), **p["pendingStatuses"]}),
        },
        "hand": ([hand_die_view(state, tables, die_id) for die_id in p["hand"]]
                 if in_battle else []),
        "piles": {"draw": len(p["drawPile"]), "discard": len(p["discardPile"]), "deck": len(p["deck"])},
        "deck": [die_summary(state, tables, die_id) for die_id in p["deck"]],
        "slots": [{
            "slotId": s["slotId"], "slotKind": s["slotKind"],
            "slotKindName": SLOT_KIND_NAMES[s["slotKind"]], "constraint": s["constraint"],
            "constraintText": describe_constraint(s["constraint"]), "bonus": s["bonus"],
            "reusable": s.get("reusable"),
            "filledBy": ({"dieId": s["filledBy"],
                          "label": current_face(state, tables, s["filledBy"])["label"]}
                         if s.get("filledBy") and s["filledBy"] in p["dice"] else None),
            "effectKind": {"attack": "purge", "defense": "ward", "heal": "mend",
                           "utility": "any", "cursed": "selfCorruption"}[s["slotKind"]],
            "disabled": in_battle and C.slot_disabled(state, s),
        } for s in p["slots"]],
        "enemies": [{
            "enemyId": e["enemyId"], "defId": e["defId"], "name": e["name"], "tier": e["tier"],
            "school": e["school"], "hp": e["hp"], "hpMax": e["hpMax"], "ward": e["ward"],
            "statuses": status_list(e["statuses"]), "intent": enemy_intent_view(state, tables, e),
            "phase": e["phase"],
            "phaseCount": (len(tables["monsters"][e["defId"]]["phases"])
                           if tables["monsters"][e["defId"]].get("phases") else 1),
            "taunt": e["taunt"], "passives": [x["kind"] for x in e["passives"]],
            "arrowVsYou": (arrow_of(school_mul_permille(c, e["school"], ds["school"]))
                           if c["toggles"]["schoolCounter"] else "eq"),
        } for e in C.alive_enemies(state)],
        "battleKind": state["node"]["battle"]["kind"] if state.get("node") and state["node"].get("battle") else None,
        "battleHints": battle_hints(state, tables) if in_battle else [],
        "map": map_view(state, tables) if state.get("map") else None,
        "stages": [{"stage": s["stage"], "chapter": s["chapter"], "boss": s.get("boss"),
                    "elite": c["eliteStages"].count(s["stage"]) > 0}
                   for s in tables["stages"]["stages"]],
        "shop": nodes["shop"], "event": nodes["event"], "treasure": nodes["treasure"],
        "forge": nodes["forge"], "shrine": nodes["shrine"], "rest": nodes["rest"],
        "pendingRelic": ({"relicId": state["pendingRelic"]["relicId"],
                          "name": tables["relics"][state["pendingRelic"]["relicId"]]["name"],
                          "desc": tables["relics"][state["pendingRelic"]["relicId"]]["desc"]}
                         if state.get("pendingRelic") else None),
        "toggles": {**c["toggles"]},
        "config": {
            "heartsMax": c["heartsMax"], "virtueMax": c["virtueMax"],
            "drawPerTurn": c["drawPerTurn"], "handMax": c["handMax"],
            "enemyHpMulPermille": c["enemyHpMulPermille"], "enemyDmgMulPermille": c["enemyDmgMulPermille"],
            "enemyExtraDice": c["enemyExtraDice"],
            "counterIntensity": c["counterIntensity"],
            "counterMulPermille": {**c["counterMulPermille"]},
            "monoPenaltyPermille": c["monoPenaltyPermille"],
            "relicSlots": c["relicSlots"], "purifyPerStage": c["purifyPerStage"],
            "eliteStages": list(c["eliteStages"]), "reviveOnce": c["reviveOnce"],
        },
        "relics": ([{"relicId": rid, "name": tables["relics"][rid]["name"],
                     "desc": tables["relics"][rid]["desc"], "rarity": tables["relics"][rid]["rarity"]}
                    for rid in p["relics"]] if c["toggles"]["relics"] else []),
        "stats": {k: state["stats"][k] for k in
                  ("turns", "damageDealt", "kills", "counterHits", "skillFaceUses",
                   "battlesWon", "battlesLost", "stagesCleared")},
        "legalActionKinds": sorted({a["kind"] for a in legal_actions(state, tables)}),
        "outcome": state["outcome"],
        "outcomeReason": state["outcomeReason"],
        "truncationReason": state["truncationReason"],
        "seed": None,
    }
    if view == "visual":
        return {
            "schemaVersion": OBS_SCHEMA, "view": "visual", "phase": obs["phase"],
            "dice": [{"slot": i, "text": f"{h['pip']}·{h['label']}", "corrupt": h["isCorrupt"]}
                     for i, h in enumerate(obs["hand"])],
            "enemies": [{"slot": i, "name": e["name"],
                         "hpRatioPermille": (e["hp"] * 1000) // max(1, e["hpMax"])}
                        for i, e in enumerate(obs["enemies"])],
            "slots": [{"slotId": s["slotId"], "occupied": bool(s["filledBy"])} for s in obs["slots"]],
            "particles": [],
        }
    return obs


# ======================================================================
# UI 提示（伤害预览/非法原因）——规则仍在内核计算，前端只展示
# ======================================================================

def play_hints(state: Mapping, tables: Mapping) -> dict[str, Any] | None:
    if state["phase"] != "player_turn":
        return None
    p = state["player"]
    out: dict[str, Any] = {}
    enemies = C.alive_enemies(state)
    for die_id in p["hand"]:
        face = current_face(state, tables, die_id)
        need = C.face_needs_target(face)
        out[die_id] = {}
        for s in p["slots"]:
            probe = normalize_action(state, {"kind": "play_face", "dieId": die_id,
                                             "slotId": s["slotId"]}, tables)
            r = C.check_play_face(state, tables, probe)
            hint: dict[str, Any] = {
                "legal": not r, "code": r["code"] if r else None,
                "reason": (describe_constraint(s["constraint"])
                           if r and r["code"] == "E_ACTION_CONSTRAINT_VIOLATION"
                           else (r["reason"] if r else None)),
                "needsTarget": need, "previews": None,
            }
            if not r and face["kind"] == "purge":
                school = face["school"]
                value_bonus = 0
                if R.swift_hand_active(state):
                    d = deck_school_info(state, tables)["school"]
                    if d != "neutral":
                        school = d
                    value_bonus = 1
                hint["previews"] = [
                    {**C.player_attack_value(state, tables, face,
                                             {"school": school, "slotBonus": s["bonus"],
                                              "valueBonus": value_bonus}, e),
                     "enemyId": e["enemyId"]}
                    for e in enemies
                ]
            elif not r and face["kind"] in ("ward", "mend"):
                sv = C.support_value(state, tables, face, {"slotBonus": s["bonus"]})
                hint["previews"] = [{"enemyId": None, "raw": sv, "mulPermille": 1000,
                                     "value": sv, "arrow": "eq"}]
            out[die_id][s["slotId"]] = hint
        out[die_id]["__discardCost"] = ((self_corruption_value(face) + 1) // 2
                                        if p["dice"][die_id]["isCorrupt"] else 0)
    return out


def native_result(state: Mapping) -> dict[str, Any]:
    """FR-01-11：供 MOD-02 填写 ``StepResult.info``（INV-7）。"""
    return {
        "outcome": state["outcome"],
        "truncationReason": state["truncationReason"],
        "stagesCleared": state["stats"]["stagesCleared"],
        "heartsLeft": state["player"]["hearts"],
        "battlesWon": state["stats"]["battlesWon"],
        "turns": state["stats"]["turns"],
        "kills": state["stats"]["kills"],
    }


def state_hash(state: Mapping) -> str:
    return digest(state)


# ======================================================================
# 对外门面（Spec §4.7）
# ======================================================================

class GameEngine:
    """``GameEngine(tables)``：GAME-01 §1 的 Python 等价门面。"""

    def __init__(self, tables: Mapping) -> None:
        self._tables = tables

    @classmethod
    def with_default_tables(cls) -> "GameEngine":
        return cls(load_tables_checked())

    def create(self, config_input: Mapping) -> dict[str, Any]:
        try:
            return create_game(config_input, self._tables)
        except KernelError as err:
            py_code, detail = map_error_code(err.code)
            raise EnvError(py_code, str(err), detail=detail) from err

    def reduce(self, state: Mapping, action: Mapping) -> dict[str, Any]:
        return reduce_state(state, action, self._tables)

    def legal_actions(self, state: Mapping) -> list[dict[str, Any]]:
        return legal_actions(state, self._tables)

    def is_legal(self, state: Mapping, action: Mapping) -> dict[str, Any]:
        return is_legal(state, action, self._tables)

    def observe(self, state: Mapping, view: str = "full") -> dict[str, Any]:
        return observe(state, view, self._tables)

    def play_hints(self, state: Mapping) -> dict[str, Any] | None:
        return play_hints(state, self._tables)

    def native_result(self, state: Mapping) -> dict[str, Any]:
        return native_result(state)

    def replay(self, config: Mapping, actions: Sequence[Mapping]) -> dict[str, Any]:
        return replay(config, actions, self._tables)


def load_tables_checked(content_dir: Path | None = None) -> dict[str, Any]:
    """``load_tables`` 的错误映射版本：``E_CONTENT_*`` → ``E_ENV_CONTENT_INVALID``。"""
    try:
        return load_tables(Path(content_dir) if content_dir else GAME_CONTENT_DIR)
    except KernelError as err:
        py_code, detail = map_error_code(err.code)
        raise EnvError(py_code, str(err), detail=detail) from err


def config_schema(tables: Mapping) -> dict[str, Any]:
    return {
        "difficulties": [{**lv, "id": did} for did, lv in tables["difficulty"]["levels"].items()],
        "difficultyKeys": tables["difficulty"]["keys"],
        "toggles": [
            {"id": "mapBranching", "default": True, "label": "地图分支",
             "off": "每关单一线性节点，自动推进", "on": "逐层分支选择，展示路径与预览"},
            {"id": "relics", "default": True, "label": "遗物系统",
             "off": "无遗物系统，相关 UI 隐藏", "on": "遗物栏、被动加成，可在商店/精英/事件获得"},
            {"id": "events", "default": True, "label": "随机事件",
             "off": "事件节点改为宝箱", "on": "随机叙事事件（正面/负面/赌博）"},
            {"id": "shops", "default": True, "label": "商店与锻造",
             "off": "商店/锻造节点改为宝箱", "on": "商店买骰/遗物/删骰；锻造改造骰面"},
            {"id": "schoolCounter", "default": True, "label": "学派克制",
             "off": "克制倍率恒为 ×1.00", "on": "锋刃 → 自然 → 秘法 → 锋刃 克制方阵生效"},
        ],
        "stageCounts": allowed_stage_counts(tables),
        "characters": [{**ch, "id": cid} for cid, ch in tables["characters"].items()],
        "enums": {
            "schools": [*SCHOOLS, "neutral"],
            "faceKinds": ["purge", "ward", "mend", "status", "selfCorruption", "draw", "reroll",
                          "modify", "gold", "nothing", "duplicate_face"],
            "slotKinds": ["attack", "defense", "heal", "utility", "cursed"],
            "riskTiers": ["safe", "balanced", "risky", "cursed"],
            "statusKinds": ["poison", "burn", "weak", "strength", "regen"],
            "actionKinds": list(ACTION_KINDS),
            "phases": ["map", "player_turn", "enemy_turn", "shop", "event", "treasure",
                       "shrine", "forge", "rest", "relic_swap", "settle"],
        },
        "schools": tables["schools"]["schools"],
        "defaults": {
            "difficulty": "medium", "character": "warrior",
            "stageCount": tables["stages"]["stageCount"],
            "toggles": {"mapBranching": True, "relics": True, "events": True, "shops": True,
                        "schoolCounter": True},
        },
    }
