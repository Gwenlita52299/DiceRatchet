"""GBNF 编译用例（PRD §11 ``test_schema_compile.py`` / §6 支持子集）。"""

from __future__ import annotations

import pytest

from dice_ratchet.inference.schema import (DECISION_SCHEMA, compile_grammar, grammar_problems,
                                        unsupported_keywords, validate_grammar_syntax)

DECISION_PROPERTIES = ("reasoning", "plan", "kind", "dieId", "slotId", "faceIndex",
                       "targetId", "abilityId", "value", "nodeId", "itemId", "optionId",
                       "defId", "candidateId", "relicId")


def _small(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": required or list(properties), "properties": properties}


# ---- DECISION_SCHEMA 必编译 ---------------------------------------------------------

def test_decision_schema_compiles_to_valid_gbnf():
    grammar = compile_grammar(DECISION_SCHEMA)
    assert grammar is not None, "DECISION_SCHEMA 必须能编译出 grammar（PRD §11）"
    assert grammar_problems(grammar) == []
    validate_grammar_syntax(grammar)
    assert grammar.startswith("root ::= ")
    for name in DECISION_PROPERTIES:
        assert f'\\"{name}\\"' in grammar, f"grammar 缺少字段 {name}"
    for kind in ("play_face", "discard_die", "purify", "use_ability", "end_turn",
                 "map_choose", "shop_buy", "shop_refresh", "shop_remove", "shop_leave",
                 "event_choose", "treasure_pick", "forge_pick", "shrine_choose",
                 "rest_choose", "relic_swap"):
        assert f'\\"{kind}\\"' in grammar


def test_decision_grammar_parses_with_llama_cpp():
    """若装了 llama-cpp-python，则 GBNF 必须被其解析器接受（PRD §11）。"""
    llama_cpp = pytest.importorskip("llama_cpp")
    grammar = compile_grammar(DECISION_SCHEMA)
    parsed = llama_cpp.LlamaGrammar.from_string(grammar)
    assert parsed is not None


# ---- 不支持关键字：返回 None 而不是抛错、也不静默忽略（Spec P-09）---------------------

@pytest.mark.parametrize("schema", [
    {"type": "object", "allOf": [{"type": "object"}], "additionalProperties": False},
    {"type": "object", "oneOf": [{"type": "object"}], "additionalProperties": False},
    {"type": "object", "$ref": "#/$defs/x", "additionalProperties": False},
    {"type": "object", "if": {"type": "object"}, "additionalProperties": False},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "string", "pattern": "^x"}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "string", "minLength": 2}}},
    {"type": "object", "properties": {"a": {"type": "string"}}}, # 未声明 additionalProperties:false
    {"type": "object", "additionalProperties": True, "properties": {"a": {"type": "string"}}},
    {"type": "object", "additionalProperties": False, "required": ["missing"],
     "properties": {"a": {"type": "string"}}}, # required 指向未声明字段
    {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "null"}}},
])
def test_unsupported_schemas_return_none(schema):
    assert compile_grammar(schema) is None


def test_unsupported_keywords_are_reported_not_ignored():
    schema = {"type": "object", "additionalProperties": False, "allOf": [], "pattern": "x",
              "properties": {"a": {"type": "string", "format": "date-time"}}}
    found = unsupported_keywords(schema)
    assert "allOf" in found and "pattern" in found and "format" in found
    assert compile_grammar(schema) is None


def test_compile_grammar_tolerates_non_mappings():
    assert compile_grammar(None) is None
    assert compile_grammar("not-a-schema") is None # type: ignore[arg-type]


# ---- 各关键字落到 grammar 上 ---------------------------------------------------------

def test_max_length_becomes_repetition():
    grammar = compile_grammar(_small({"a": {"type": "string", "maxLength": 16}}))
    assert grammar is not None and "j-char{0,16}" in grammar


def test_integer_range_is_enforced():
    grammar = compile_grammar(_small({"n": {"type": "integer", "minimum": 0, "maximum": 11}}))
    assert grammar is not None
    assert '"1" ( "0" | "1" )' in grammar # 10..11 分支
    assert "::= j-int" not in grammar # 有界整数不退回无界规则


def test_array_root_is_supported():
    grammar = compile_grammar({"type": "array", "items": {"type": "string"}})
    assert grammar is not None and grammar_problems(grammar) == []


def test_min_items_and_max_items_are_enforced():
    grammar = compile_grammar({"type": "object", "additionalProperties": False,
                               "required": ["plan"],
                               "properties": {"plan": {
                                   "type": "array", "minItems": 1, "maxItems": 12,
                                   "items": {"type": "string"}}}})
    assert grammar is not None and "{0,11}" in grammar


def test_exact_items_count_is_exact():
    grammar = compile_grammar({"type": "object", "additionalProperties": False,
                               "required": ["t"], "properties": {"t": {
                                   "type": "array", "minItems": 2, "maxItems": 2,
                                   "items": {"type": "boolean"}}}})
    assert grammar is not None and "{1,1}" in grammar


def test_boolean_and_number_types():
    grammar = compile_grammar(_small({"b": {"type": "boolean"}, "n": {"type": "number"}}))
    assert grammar is not None
    assert '("true" | "false")' in grammar and "j-number" in grammar


def test_optional_properties_allow_empty_object():
    grammar = compile_grammar({"type": "object", "additionalProperties": False,
                               "properties": {"a": {"type": "string"}}})
    assert grammar is not None
    assert ")? " in grammar # 全可选 ⇒ 允许 {}


def test_annotations_do_not_block_compilation():
    grammar = compile_grammar({"$schema": "https://json-schema.org/draft/2020-12/schema",
                               "$id": "x", "title": "T", "description": "d",
                               "type": "object", "additionalProperties": False,
                               "required": [], "properties": {"a": {"type": "string"}}})
    assert grammar is not None


def test_grammar_problems_detects_undefined_reference():
    assert grammar_problems("root ::= missing\n") != []
