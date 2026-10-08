"""``kernel/counter.js`` 的逐位移植 —— 学派克制方阵（GAME-00 §5.1 / GAME-01 §9.1）。"""

from __future__ import annotations

from typing import Final, Mapping

__all__ = ["SCHOOLS", "BEATS", "school_mul_permille", "arrow_of", "counter_of", "apply_mul"]

SCHOOLS: Final[tuple[str, ...]] = ("blade", "arcana", "verdant")
BEATS: Final[dict[str, str]] = {"blade": "verdant", "arcana": "blade", "verdant": "arcana"}


def school_mul_permille(config: Mapping, atk: str | None, def_: str | None,
                        bonus_permille: int = 0) -> int:
    """返回千分倍率。``config.toggles.schoolCounter`` 为假时恒 1000。"""
    if not config["toggles"]["schoolCounter"]:
        return 1000
    if atk not in BEATS or def_ not in BEATS or atk == def_:
        return 1000
    if BEATS[atk] == def_:
        return config["counterMulPermille"]["advantage"] + bonus_permille
    if BEATS[def_] == atk:
        return config["counterMulPermille"]["disadvantage"]
    return 1000


def arrow_of(mul: int) -> str:
    if mul > 1000:
        return "up"
    if mul < 1000:
        return "down"
    return "eq"


def counter_of(school: str | None) -> str | None:
    """克制该 school 的学派（谁能打它）。"""
    for s in SCHOOLS:
        if BEATS[s] == school:
            return s
    return None


def apply_mul(value: int, mul_permille: int) -> int:
    """按倍率缩放并 floor（JS ``Math.floor((value * mul) / 1000)``）。"""
    return (value * mul_permille) // 1000
