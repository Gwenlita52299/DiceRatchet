"""``kernel/state.js`` 的逐位移植 —— GameState 辅助函数与不变式断言（GAME-01 §2）。

注意：本模块的函数**原地修改 draft state**（与 JS 同；``reduce`` 在调用前已深拷贝）。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

from .counter import SCHOOLS
from .error import KernelError
from .risk import compute_risk_tier, face_has_self_corruption
from .rng import STREAM_NAMES

__all__ = [
    "STATUS_KINDS", "empty_statuses", "status_list", "pad", "thaw",
    "die_def", "die_faces", "current_face", "die_risk_tier",
    "new_die", "add_die_to_deck", "remove_die_from_deck", "refresh_corrupt",
    "deck_school_info", "is_mono", "assert_invariants",
]

STATUS_KINDS: Final[tuple[str, ...]] = ("poison", "burn", "weak", "strength", "regen")


def empty_statuses() -> dict[str, int]:
    return {"poison": 0, "burn": 0, "weak": 0, "strength": 0, "regen": 0}


def status_list(st: Mapping) -> list[dict[str, Any]]:
    return [{"kind": k, "stacks": st[k]} for k in STATUS_KINDS if st[k] > 0]


def pad(n: int, w: int) -> str:
    return str(n).zfill(w)


def thaw(o: Any) -> Any:
    """把配表的冻结视图（``MappingProxyType`` / ``tuple``）递归解冻为可变的 dict / list。

    任何把配表数据**写入 state** 的地方都必须先解冻：state 要能被 ``copy.deepcopy``
    （JS 侧 ``structuredClone``）且必须是纯 JSON 结构（P-14/K-1）。只读访问无需解冻。
    """
    if isinstance(o, MappingProxyType):
        return {k: thaw(v) for k, v in o.items()}
    if isinstance(o, (tuple, list)):
        return [thaw(v) for v in o]
    return o


# ----------------------------------------------------------------------
# 骰子
# ----------------------------------------------------------------------

def die_def(tables: Mapping, def_id: str) -> Mapping:
    d = tables["allDice"].get(def_id)
    if d is None:
        raise KernelError("E_KERNEL_UNKNOWN_DIE", def_id)
    return d


def die_faces(state: Mapping, tables: Mapping, die_id: str) -> list[Mapping]:
    die = state["player"]["dice"][die_id]
    return die["faces"] if die["faces"] else die_def(tables, die["defId"])["faces"]


def current_face(state: Mapping, tables: Mapping, die_id: str) -> Mapping:
    die = state["player"]["dice"][die_id]
    return die_faces(state, tables, die_id)[die["faceIndex"]]


def die_risk_tier(state: Mapping, tables: Mapping, die_id: str) -> str:
    die = state["player"]["dice"][die_id]
    def_ = die_def(tables, die["defId"])
    return compute_risk_tier({"category": def_["category"], "faces": die_faces(state, tables, die_id)})


def new_die(state: dict, def_id: str) -> str:
    state["dieSeq"] += 1
    die_id = f"d-{pad(state['dieSeq'], 4)}"
    state["player"]["dice"][die_id] = {
        "defId": def_id, "faceIndex": 0, "isCorrupt": False, "faces": None, "forged": False,
    }
    return die_id


def add_die_to_deck(state: dict, def_id: str, temporary: bool = False) -> str:
    """加入骰池（永久）。``inBattle`` 时同时放入弃骰堆。"""
    die_id = new_die(state, def_id)
    if temporary:
        state["player"]["dice"][die_id]["temporary"] = True
    state["player"]["deck"].append(die_id)
    if state.get("node") and state["node"].get("battle"):
        state["player"]["discardPile"].append(die_id)
    return die_id


def remove_die_from_deck(state: dict, die_id: str) -> None:
    p = state["player"]
    p["deck"] = [d for d in p["deck"] if d != die_id]
    p["drawPile"] = [d for d in p["drawPile"] if d != die_id]
    p["discardPile"] = [d for d in p["discardPile"] if d != die_id]
    p["hand"] = [d for d in p["hand"] if d != die_id]
    del p["dice"][die_id]


def refresh_corrupt(state: dict, tables: Mapping, die_id: str) -> None:
    die = state["player"]["dice"][die_id]
    die["isCorrupt"] = face_has_self_corruption(current_face(state, tables, die_id))


# ----------------------------------------------------------------------
# 学派
# ----------------------------------------------------------------------

def deck_school_info(state: Mapping, tables: Mapping) -> dict[str, Any]:
    """deckSchool：骰池中学派技能骰计数最大者；平局取注册顺序靠前者；无学派骰 → neutral（D-01）。"""
    counts: dict[str, int] = {"blade": 0, "arcana": 0, "verdant": 0}
    for die_id in state["player"]["deck"]:
        def_ = die_def(tables, state["player"]["dice"][die_id]["defId"])
        if def_["category"] == "skill" and def_["school"] in counts:
            counts[def_["school"]] += 1
    best = "neutral"
    best_n = 0
    for s in SCHOOLS:
        if counts[s] > best_n:
            best = s
            best_n = counts[s]
    total = counts["blade"] + counts["arcana"] + counts["verdant"]
    concentration_permille = (best_n * 1000) // total if total else 0
    return {"school": best, "counts": counts, "total": total,
            "concentrationPermille": concentration_permille}


def is_mono(state: Mapping, tables: Mapping) -> bool:
    info = deck_school_info(state, tables)
    c = state["config"]
    return info["total"] >= c["monoMinDice"] and info["concentrationPermille"] >= c["monoThresholdPermille"]


# ----------------------------------------------------------------------
# 不变式（debug 模式开启）
# ----------------------------------------------------------------------

def _scan_float(v: Any, path: str) -> None:
    if isinstance(v, float):
        raise KernelError("E_KERNEL_INVARIANT", f"float at {path}")
    if v is not None and isinstance(v, (dict, list)):
        if isinstance(v, dict):
            for k in v:
                _scan_float(v[k], f"{path}.{k}")
        else:
            for i, item in enumerate(v):
                _scan_float(item, f"{path}.{i}")


def assert_invariants(state: Mapping) -> None:
    p = state["player"]
    if len(p["hand"]) > state["config"]["handMax"]:
        raise KernelError("E_KERNEL_INVARIANT", "hand > handMax")
    all_ = list(p["drawPile"]) + list(p["discardPile"]) + list(p["hand"])
    if len(set(all_)) != len(all_):
        raise KernelError("E_KERNEL_INVARIANT", "duplicate die in piles")
    for die_id in all_:
        if die_id not in p["deck"]:
            raise KernelError("E_KERNEL_INVARIANT", f"die {die_id} not in deck")
    if state.get("node") and state["node"].get("battle") and len(all_) != len(p["deck"]):
        raise KernelError("E_KERNEL_INVARIANT", "die lost")
    for die_id in p["deck"]:
        d = p["dice"].get(die_id)
        if d is None or d["faceIndex"] < 0 or d["faceIndex"] > 5:
            raise KernelError("E_KERNEL_INVARIANT", f"bad die {die_id}")
    names = [r["name"] for r in state["rng"]]
    if names != list(STREAM_NAMES):
        raise KernelError("E_KERNEL_INVARIANT", "rng streams")
    _scan_float(state, "state")
