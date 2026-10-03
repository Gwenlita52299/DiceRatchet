"""``kernel/forge.js`` 的逐位移植 —— 锻造：5 选 1 替换骰面（GAME-00 §8.5 / GAME-02 §1.3），消费 forge 流。"""

from __future__ import annotations

from typing import Any, Mapping

from .counter import apply_mul
from .rng import randint, weighted
from .state import die_def, die_faces, refresh_corrupt, thaw

__all__ = ["enter_forge", "forgeable_dice", "apply_forged_face", "forge_pick"]

Ctx = dict[str, Any]


def enter_forge(ctx: Ctx, prepaid: bool) -> None:
    state, tables = ctx["state"], ctx["tables"]
    items = [{"key": f["id"], "weight": f["weight"]} for f in tables["forgeFaces"].values()]
    candidates: list[str] = []
    n = tables["shops"]["forge"]["candidates"]
    while len(candidates) < n and items:
        k = weighted(state, "forge", items)
        candidates.append(k)
        items.pop(next(i for i, it in enumerate(items) if it["key"] == k))
    cfg = tables["shops"]["forge"]
    price = apply_mul(randint(state, "forge", cfg["priceMin"], cfg["priceMax"]),
                      state["config"]["shopPriceMulPermille"])
    state["node"]["forge"] = {"candidates": candidates, "price": 0 if prepaid else price, "done": False}
    state["phase"] = "forge"


def forgeable_dice(state: Mapping, tables: Mapping) -> list[str]:
    return [die_id for die_id in state["player"]["deck"]
            if die_def(tables, state["player"]["dice"][die_id]["defId"])["category"] != "cursed"]


def apply_forged_face(state: dict, tables: Mapping, die_id: str, face_index: int,
                      forge_face_id: str) -> None:
    die = state["player"]["dice"][die_id]
    faces = [thaw(f) for f in die_faces(state, tables, die_id)]
    faces[face_index] = {**thaw(tables["forgeFaces"][forge_face_id]["face"]),
                         "faceIndex": face_index, "forged": True}
    die["faces"] = faces
    die["forged"] = True
    refresh_corrupt(state, tables, die_id)


def forge_pick(ctx: Ctx, action: Mapping) -> None:
    state, tables, events = ctx["state"], ctx["tables"], ctx["events"]
    f = state["node"]["forge"]
    state["player"]["starShards"] -= f["price"]
    apply_forged_face(state, tables, action["dieId"], action["faceIndex"], action["candidateId"])
    f["done"] = True
    events.append({"type": "die_forged", "dieId": action["dieId"],
                   "faceIndex": action["faceIndex"], "faceId": action["candidateId"]})
