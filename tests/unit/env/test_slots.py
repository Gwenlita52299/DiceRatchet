"""槽约束 DSL（GAME-00 §4.3）穷举测试 —— 对应 JS ``slots.test.js``。"""

from __future__ import annotations

import pytest

from dice_ratchet.env.error import KernelError
from dice_ratchet.env.slots import (SLOT_ACCEPTS, SLOT_KIND_NAMES, check_constraint,
                                describe_constraint, parse_constraint, slot_accepts)


@pytest.mark.parametrize("text,want", [
    ("any", {"op": "any"}),
    ("odd", {"op": "odd"}),
    ("even", {"op": "even"}),
    ("ge:3", {"op": "ge", "n": 3}),
    ("gt:2", {"op": "gt", "n": 2}),
    ("le:5", {"op": "le", "n": 5}),
    ("lt:4", {"op": "lt", "n": 4}),
    ("eq:6", {"op": "eq", "n": 6}),
    ("eq:-1", {"op": "eq", "n": -1}),  # 负数被正则接受（与 JS 一致）
    ("in:1,3,5", {"op": "in", "set": [1, 3, 5]}),
])
def test_parse_constraint(text, want):
    assert parse_constraint(text) == want


@pytest.mark.parametrize("bad", ["", "ANY", "ge", "ge:x", "in:", "in:1,", "foo:1", "odd:1", 3, None])
def test_parse_constraint_syntax_error(bad):
    with pytest.raises(KernelError) as excinfo:
        parse_constraint(bad)
    assert excinfo.value.code == "E_KERNEL_CONSTRAINT_SYNTAX"


@pytest.mark.parametrize("text,pip,want", [
    ("any", 1, True), ("any", 6, True),
    ("odd", 3, True), ("odd", 4, False),
    ("even", 4, True), ("even", 3, False),
    ("ge:4", 4, True), ("ge:4", 3, False),
    ("gt:4", 5, True), ("gt:4", 4, False),
    ("le:2", 2, True), ("le:2", 3, False),
    ("lt:2", 1, True), ("lt:2", 2, False),
    ("eq:5", 5, True), ("eq:5", 4, False),
    ("in:2,4,6", 4, True), ("in:2,4,6", 3, False),
])
def test_check_constraint(text, pip, want):
    assert check_constraint(text, pip) is want


@pytest.mark.parametrize("text,want", [
    ("any", "任意点数"), ("odd", "需要奇数"), ("even", "需要偶数"),
    ("ge:3", "需要 ≥3"), ("gt:2", "需要 >2"), ("le:5", "需要 ≤5"),
    ("lt:4", "需要 <4"), ("eq:6", "需要 =6"), ("in:1,3,5", "需要 1/3/5"),
])
def test_describe_constraint(text, want):
    assert describe_constraint(text) == want


def test_slot_accepts_table():
    assert SLOT_ACCEPTS["attack"] == ["purge"]
    assert SLOT_ACCEPTS["defense"] == ["ward"]
    assert SLOT_ACCEPTS["heal"] == ["mend"]
    assert SLOT_ACCEPTS["utility"] is None
    assert SLOT_ACCEPTS["cursed"] == ["selfCorruption"]
    assert set(SLOT_KIND_NAMES) == set(SLOT_ACCEPTS)


@pytest.mark.parametrize("slot_kind,face_kind,want", [
    ("attack", "purge", True), ("attack", "ward", False),
    ("defense", "ward", True), ("heal", "mend", True),
    ("utility", "purge", True), ("utility", "selfCorruption", True),
    ("cursed", "selfCorruption", True), ("cursed", "purge", False),
    (None, "purge", False), ("nope", "purge", False),
])
def test_slot_accepts(slot_kind, face_kind, want):
    assert slot_accepts(slot_kind, face_kind) is want
