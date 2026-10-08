"""``kernel/risk.js`` 的逐位移植 —— 骰面风险判定（纯函数，loader 与内核共用）。"""

from __future__ import annotations

from typing import Mapping

__all__ = ["face_has_self_corruption", "self_corruption_value", "compute_risk_tier"]


def face_has_self_corruption(face: Mapping) -> bool:
    return face["kind"] == "selfCorruption" or int(face.get("selfCorruption") or 0) > 0


def self_corruption_value(face: Mapping) -> int:
    if face["kind"] == "selfCorruption":
        return int(face["value"])
    return int(face.get("selfCorruption") or 0)


def compute_risk_tier(def_: Mapping) -> str:
    """风险等级自动判定（决策 D-09）：不利面 = selfCorruption 或 nothing；
    0 → safe，1–2 → balanced，≥3 → risky；cursed 类恒为 cursed。"""
    if def_["category"] == "cursed":
        return "cursed"
    bad = sum(1 for f in def_["faces"] if face_has_self_corruption(f) or f["kind"] == "nothing")
    if bad == 0:
        return "safe"
    if bad <= 2:
        return "balanced"
    return "risky"
