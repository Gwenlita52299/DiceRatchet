"""``kernel/slots.js`` 的逐位移植 —— 槽约束 DSL 与槽/骰面兼容表（GAME-00 §4.3）。"""

from __future__ import annotations

import re
from typing import Final

from .error import KernelError

__all__ = ["parse_constraint", "check_constraint", "describe_constraint",
           "SLOT_ACCEPTS", "SLOT_KIND_NAMES", "slot_accepts"]


def _syntax_error(text: str) -> KernelError:
    return KernelError("E_KERNEL_CONSTRAINT_SYNTAX", f"bad constraint: {text}")


_GE_RE: Final[re.Pattern[str]] = re.compile(r"^(ge|gt|le|lt|eq):(-?\d+)$")
_IN_RE: Final[re.Pattern[str]] = re.compile(r"^in:(\d+(?:,\d+)*)$")


def parse_constraint(text: str) -> dict:
    """``any | odd | even | (ge|gt|le|lt|eq):int | in:int,int,...``。"""
    if not isinstance(text, str):
        raise _syntax_error(text)
    if text in ("any", "odd", "even"):
        return {"op": text}
    m = _GE_RE.match(text)
    if m:
        return {"op": m.group(1), "n": int(m.group(2))}
    m2 = _IN_RE.match(text)
    if m2:
        return {"op": "in", "set": [int(x) for x in m2.group(1).split(",")]}
    raise _syntax_error(text)


def check_constraint(text: str, pip: int) -> bool:
    c = parse_constraint(text)
    op = c["op"]
    if op == "any":
        return True
    if op == "odd":
        return pip % 2 == 1
    if op == "even":
        return pip % 2 == 0
    if op == "ge":
        return pip >= c["n"]
    if op == "gt":
        return pip > c["n"]
    if op == "le":
        return pip <= c["n"]
    if op == "lt":
        return pip < c["n"]
    if op == "eq":
        return pip == c["n"]
    if op == "in":
        return pip in c["set"]
    raise _syntax_error(text)


def describe_constraint(text: str) -> str:
    c = parse_constraint(text)
    op = c["op"]
    if op == "any":
        return "任意点数"
    if op == "odd":
        return "需要奇数"
    if op == "even":
        return "需要偶数"
    if op == "ge":
        return f"需要 ≥{c['n']}"
    if op == "gt":
        return f"需要 >{c['n']}"
    if op == "le":
        return f"需要 ≤{c['n']}"
    if op == "lt":
        return f"需要 <{c['n']}"
    if op == "eq":
        return f"需要 ={c['n']}"
    if op == "in":
        return "需要 " + "/".join(str(x) for x in c["set"])
    return text


# slotKind → 可接受的 face.kind；None 表示任意
SLOT_ACCEPTS: Final[dict[str, list[str] | None]] = {
    "attack": ["purge"],
    "defense": ["ward"],
    "heal": ["mend"],
    "utility": None,
    "cursed": ["selfCorruption"],
}

SLOT_KIND_NAMES: Final[dict[str, str]] = {
    "attack": "攻击", "defense": "防御", "heal": "恢复", "utility": "工具", "cursed": "厄咒",
}


def slot_accepts(slot_kind: str | None, face_kind: str) -> bool:
    if slot_kind not in SLOT_ACCEPTS:
        return False
    acc = SLOT_ACCEPTS[slot_kind]
    return acc is None or face_kind in acc
