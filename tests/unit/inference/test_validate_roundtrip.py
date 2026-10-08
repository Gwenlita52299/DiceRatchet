"""``DECISION_SCHEMA`` round-trip：12 正例 / 9+ 反例 + ``json_path`` 口径（G5 / PRD §11）。"""

from __future__ import annotations

import pytest
from jsonschema import SchemaError, ValidationError

from dice_ratchet.inference.schema import (DECISION_SCHEMA, json_path_of, load_decision_schema,
                                        render_error, retry_reason, validate)


def _plan(*actions: dict, reasoning: str = "ok") -> dict:
    return {"reasoning": reasoning, "plan": list(actions)}


POSITIVES = [
    _plan({"kind": "end_turn"}),
    _plan({"kind": "play_face", "dieId": "d1", "slotId": "s1", "faceIndex": 0}),
    _plan({"kind": "play_face", "dieId": "d2", "slotId": "s2", "faceIndex": 11}),
    _plan({"kind": "discard_die", "dieId": "d3"}),
    _plan({"kind": "purify", "dieId": "d4", "value": 0}),
    _plan({"kind": "use_ability", "abilityId": "a" * 32, "targetId": "e1"}),
    _plan({"kind": "map_choose", "nodeId": "n1"}),
    _plan({"kind": "shop_buy", "itemId": "i1"}, {"kind": "shop_leave"}),
    _plan({"kind": "shop_refresh"}, {"kind": "shop_remove", "itemId": "i1"},
          {"kind": "shop_leave"}),
    _plan({"kind": "event_choose", "optionId": "o1"}),
    _plan({"kind": "treasure_pick", "relicId": "r1"}),
    _plan(*[{"kind": "purify", "dieId": f"d{i}", "value": 99} for i in range(12)]),
]

NEGATIVES = [
    ({"plan": [{"kind": "end_turn"}]}, "$"), # 缺 reasoning
    ({"reasoning": "ok"}, "$"), # 缺 plan
    (_plan(), "$.plan"), # minItems=1
    (_plan(*[{"kind": "end_turn"} for _ in range(13)]), "$.plan"), # maxItems=12
    (_plan({"kind": "teleport"}), "$.plan[0].kind"), # enum
    (_plan({"kind": "play_face", "faceIndex": 12}), "$.plan[0].faceIndex"), # maximum
    (_plan({"kind": "play_face", "faceIndex": -1}), "$.plan[0].faceIndex"), # minimum
    (_plan({"kind": "purify", "value": 100}), "$.plan[0].value"),
    (_plan({"kind": "end_turn", "nope": 1}), "$.plan[0]"), # additionalProperties
    (dict(_plan({"kind": "end_turn"}), extra=True), "$"), # 根级 additionalProperties
    (_plan({"kind": "use_ability", "abilityId": "x" * 33}), "$.plan[0].abilityId"),
    ({"reasoning": 5, "plan": [{"kind": "end_turn"}]}, "$.reasoning"), # 类型
]


@pytest.mark.parametrize("payload", POSITIVES)
def test_positive_examples_pass(payload):
    validate(payload, DECISION_SCHEMA)


@pytest.mark.parametrize("payload", [case[0] for case in NEGATIVES])
def test_negative_examples_fail(payload):
    with pytest.raises(ValidationError):
        validate(payload, DECISION_SCHEMA)


@pytest.mark.parametrize("payload,expected_path", NEGATIVES)
def test_negative_examples_report_expected_json_path(payload, expected_path):
    with pytest.raises(ValidationError) as excinfo:
        validate(payload, DECISION_SCHEMA)
    assert json_path_of(excinfo.value) == expected_path


def test_retry_reason_and_render_error_shapes():
    with pytest.raises(ValidationError) as excinfo:
        validate(_plan({"kind": "teleport"}), DECISION_SCHEMA)
    error = excinfo.value
    assert retry_reason(error) == "ValidationError:$.plan[0].kind"
    assert "validator=enum" in render_error(error)
    assert json_path_of(error) == "$.plan[0].kind"


def test_validate_rejects_invalid_schema():
    with pytest.raises(SchemaError):
        validate({"reasoning": "ok"}, {"type": "nope"})


def test_decision_schema_is_loaded_from_the_single_file():
    """唯一事实来源是 ``schemas/decision.schema.json``（Spec §4.3 / B-28）。"""
    loaded = load_decision_schema()
    assert loaded == DECISION_SCHEMA
    assert loaded["required"] == ["reasoning", "plan"]
    assert loaded["additionalProperties"] is False
    kinds = loaded["properties"]["plan"]["items"]["properties"]["kind"]["enum"]
    assert len(kinds) == 16 and kinds[0] == "play_face"
