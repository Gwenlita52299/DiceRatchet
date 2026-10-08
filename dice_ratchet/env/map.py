"""``kernel/map.js`` 的逐位移植 —— 章节/关卡/地图生成（GAME-00 §4.4 / GAME-02 §4），只消费 map 流。"""

from __future__ import annotations

from typing import Any, Mapping

from .rng import choice
from .state import pad

__all__ = ["stage_def", "chapter_def", "generate_maps", "current_stage_map",
           "find_node", "reachable_node_ids", "group_members"]

NON_BATTLE = ["shop", "forge", "event", "treasure", "shrine", "rest"]


def stage_def(tables: Mapping, stage: int) -> Mapping:
    return next(s for s in tables["stages"]["stages"] if s["stage"] == stage)


def chapter_def(tables: Mapping, chapter: int) -> Mapping:
    return next(c for c in tables["stages"]["chapters"] if c["chapter"] == chapter)


def substitute(tables: Mapping, toggles: Mapping, kind: str) -> str:
    fb = tables["stages"]["toggleFallback"].get(kind)
    if fb and not toggles[fb["toggle"]]:
        return substitute(tables, toggles, fb["fallback"])
    return kind


def edges_for(i: int, n: int, m: int) -> list[int]:
    """行 r 的第 i 个节点（共 n 个）连接到行 r+1（共 m 个）的区间。"""
    lo = (i * m) // n
    hi = max(lo, ((i + 1) * m - 1) // n)
    return list(range(lo, hi + 1))


def generate_stage(state: Mapping, tables: Mapping, sd: Mapping) -> dict[str, Any]:
    c = state["config"]
    linear = not c["toggles"]["mapBranching"]
    elite = c["eliteStages"].count(sd["stage"]) > 0
    rows: list[list[dict[str, Any]]] = []
    used_kinds: set[str] = set()
    pool = sorted({substitute(tables, c["toggles"], k) for k in sd["kinds"]})
    for r in range(sd["rows"]):
        last = r == sd["rows"] - 1
        count = 1 if linear else sd["nodeBudget"][r]
        if last and sd["boss"]:
            count = 1
        row: list[dict[str, Any]] = []
        for i in range(count):
            if r == 0:
                kind = "battle"
            elif last:
                kind = "boss" if sd["boss"] else ("elite" if elite else "battle")
            else:
                avail = [k for k in pool if not (k in NON_BATTLE and k in used_kinds)]
                kind = choice(state, "map", avail if avail else ["battle"])
                if kind in NON_BATTLE:
                    used_kinds.add(kind)
            row.append({
                "nodeId": f"n-{pad(sd['stage'], 2)}-{r}-{i}", "kind": kind,
                "row": r, "index": i, "edges": [], "enemyGroupId": None, "visited": False,
            })
        rows.append(row)
    # 精英：Boss 关的倒数第二行首个战斗节点升格为精英（无战斗节点则首节点）
    if elite and sd["boss"] and sd["rows"] >= 3:
        row = rows[sd["rows"] - 2]
        target = next((n for n in row if n["kind"] == "battle"), row[0])
        if target["kind"] in NON_BATTLE:
            used_kinds.discard(target["kind"])
        target["kind"] = "elite"
    # 编队
    groups = list(tables["groups"].values())
    normal_pool = sorted(g["id"] for g in groups
                         if not g.get("elite") and g["stages"][0] <= sd["stage"] <= g["stages"][1])
    elite_pool = sorted(g["id"] for g in groups if g.get("elite"))
    for row in rows:
        for n in row:
            if n["kind"] == "battle":
                n["enemyGroupId"] = choice(state, "map", normal_pool)
            elif n["kind"] == "elite":
                n["enemyGroupId"] = choice(state, "map", elite_pool)
            elif n["kind"] == "boss":
                n["enemyGroupId"] = f"boss:{sd['bossId']}"
    # 边
    for r in range(len(rows) - 1):
        n, m = len(rows[r]), len(rows[r + 1])
        for i, node in enumerate(rows[r]):
            node["edges"] = [rows[r + 1][j]["nodeId"] for j in edges_for(i, n, m)]
    return {"stage": sd["stage"], "chapter": sd["chapter"], "rows": rows}


def generate_maps(state: Mapping, tables: Mapping) -> dict[str, Any]:
    stages = [generate_stage(state, tables, stage_def(tables, s))
              for s in range(1, state["config"]["stageCount"] + 1)]
    return {"stages": stages, "currentRow": -1, "currentNodeId": None, "path": []}


def current_stage_map(state: Mapping) -> Mapping:
    return state["map"]["stages"][state["stage"] - 1]


def find_node(state: Mapping, node_id: str) -> dict[str, Any] | None:
    sm = current_stage_map(state)
    for row in sm["rows"]:
        for n in row:
            if n["nodeId"] == node_id:
                return n
    return None


def reachable_node_ids(state: Mapping) -> list[str]:
    sm = current_stage_map(state)
    if state["map"]["currentRow"] < 0:
        return [n["nodeId"] for n in sm["rows"][0]]
    cur = find_node(state, state["map"]["currentNodeId"])
    return list(cur["edges"]) if cur else []


def group_members(tables: Mapping, group_id: str) -> list[str]:
    if group_id.startswith("boss:"):
        return [group_id[len("boss:"):]]
    return list(tables["groups"][group_id]["members"])
