"""``schema.py`` 边角：加载错误、编译子集边界、GBNF 自检与私有编译器辅助。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.inference import schema as schema_mod
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.schema import (DECISION_SCHEMA, _fixed_width, _gbnf_literal,
                                        _int_range_expr, compile_grammar,
                                        decision_schema_path, grammar_problems,
                                        load_decision_schema, render_error,
                                        validate_grammar_syntax)


# ---- 加载与定位 ---------------------------------------------------------------------

def test_decision_schema_path_prefers_explicit_root(tmp_path: Path):
    target = tmp_path / "schemas" / "decision.schema.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"type": "object"}', encoding="utf-8")
    assert decision_schema_path(tmp_path) == target
    assert load_decision_schema(target) == {"type": "object"}


def test_decision_schema_missing_raises(tmp_path: Path):
    with pytest.raises(InferenceError) as excinfo:
        load_decision_schema(tmp_path / "nope.json")
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "读取失败" in excinfo.value.message


def test_decision_schema_bad_json_raises(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(InferenceError):
        load_decision_schema(bad)


def test_decision_schema_must_be_object(tmp_path: Path):
    path = tmp_path / "arr.json"
    path.write_text('["nope"]', encoding="utf-8")
    with pytest.raises(InferenceError) as excinfo:
        load_decision_schema(path)
    assert "顶层必须是 object" in excinfo.value.message


def test_decision_schema_module_getattr_and_cache():
    first = schema_mod.decision_schema()
    assert first is schema_mod.decision_schema() # 缓存
    assert schema_mod.__getattr__("DECISION_SCHEMA") is first
    with pytest.raises(AttributeError):
        schema_mod.__getattr__("NOPE")


# ---- 关键字白名单边界 ----------------------------------------------------------------

@pytest.mark.parametrize("schema", [
    {"type": "object", "additionalProperties": False, "properties": {"a": "not-a-schema"}},
    {"type": "object", "additionalProperties": False, "properties": 5},
    {"type": "object", "additionalProperties": False, "properties": {"a": {"type": None}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": ["string", "integer"]}}}, # 多类型不支持
    {"type": "object", "additionalProperties": False, "properties": {"a": {"enum": []}}},
    {"type": "object", "additionalProperties": False, "properties": {"a": {"enum": 5}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"enum": [1.5]}}}, # 非标量枚举
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "array", "items": 5}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "array", "items": {"type": "string"},
                          "minItems": "x"}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "array", "items": {"type": "string"},
                          "minItems": 3, "maxItems": 2}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "string", "maxLength": -1}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "integer", "minimum": True}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "integer", "minimum": 1.5}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "integer", "minimum": -5, "maximum": 5}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "number", "minimum": 0.5, "maximum": 5}}},
    {"type": "object", "additionalProperties": False,
     "properties": {"a": {"type": "number", "minimum": True, "maximum": 5}}},
])
def test_unsupported_edges_return_none(schema):
    assert compile_grammar(schema) is None


def test_supported_edges_compile():
    schema = {
        "type": "object", "additionalProperties": False, "required": [], "properties": {
            "empty_obj": {"type": "object", "additionalProperties": False},
            "single_type": {"type": ["string"]},
            "bounded_number": {"type": "number", "minimum": 0, "maximum": 9},
            "negative_int": {"type": "integer", "minimum": -20, "maximum": -10},
            "fixed_int": {"type": "integer", "minimum": 7, "maximum": 7},
            "unbounded_int": {"type": "integer"},
            "optional_array": {"type": "array", "items": {"type": "string"},
                               "maxItems": 3},
            "enum_mixed": {"enum": ["a", 1, True, None]},
        },
    }
    grammar = compile_grammar(schema)
    assert grammar is not None
    assert grammar_problems(grammar) == []
    assert '"-" ( "1"' in grammar # 负数区间
    assert '"7"' in grammar
    assert "j-number" in grammar and "j-int" in grammar # 无界退回词法规则
    assert '"true"' in grammar and '"null"' in grammar


def test_unsupported_keywords_walks_nested_properties():
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"a": {"type": "object", "additionalProperties": False,
                                   "properties": {"b": {"type": "string",
                                                        "pattern": "x"}}}}}
    found = schema_mod.unsupported_keywords(schema)
    assert "pattern" in found
    # properties 非 dict 时 walk 不炸
    assert schema_mod.unsupported_keywords({"type": "object", "properties": 5}) == []


# ---- GBNF 自检 ----------------------------------------------------------------------

def test_grammar_problems_reports_structural_issues():
    assert grammar_problems("") == ["缺少 root 规则"]
    assert any("不是合法的规则定义" in p for p in grammar_problems("root ::= ok\n::::"))
    assert any("重复定义" in p for p in grammar_problems("root ::= a\na ::= \"x\"\na ::= \"y\""))
    assert any("括号不配平" in p for p in grammar_problems('root ::= ( "a"'))
    assert any("字符类不配平" in p for p in grammar_problems('root ::= [ab'))
    assert any("未定义规则" in p for p in grammar_problems("root ::= ghost\n"))
    # 空行与注释不算问题
    assert grammar_problems('# 注释\n\nroot ::= "x"\n') == []


def test_validate_grammar_syntax_raises_value_error():
    with pytest.raises(ValueError):
        validate_grammar_syntax("root ::= ghost\n")
    validate_grammar_syntax('root ::= "x" j-ws\nj-ws ::= ([ \\t\\n] j-ws)?\n')


# ---- 私有编译器辅助（边界与转义）-------------------------------------------------------

def test_int_range_expr_boundaries():
    assert _int_range_expr(5, 5) == '"5"'
    assert _int_range_expr(0, 9).startswith("(")
    assert _int_range_expr(-20, -10).startswith('"-" ')
    assert _int_range_expr(95, 105).count("|") >= 1
    with pytest.raises(schema_mod._Unsupported):
        _int_range_expr(-5, 5) # 混合符号：明确不支持


def test_fixed_width_direct_calls():
    assert _fixed_width(0, 0, 0) == '""' # 递归基准
    assert _fixed_width(5, 5, 1) == '"5"'
    assert _fixed_width(10, 12, 2).startswith('"1"')


def test_gbnf_literal_escapes_specials():
    assert _gbnf_literal('a"b') == '"a\\"b"'
    assert _gbnf_literal("a\\b") == '"a\\\\b"'
    assert _gbnf_literal("a\nb\tc\rd") == '"a\\nb\\tc\\rd"'


def test_render_error_fallback_for_plain_exception():
    assert render_error(ValueError("boom")) == "boom"
    assert render_error("already text") == "already text"


def test_decision_schema_is_cacheable_for_mod04():
    """schema 与 phase 无关，可缓存编译（PRD §6）。"""
    assert compile_grammar(DECISION_SCHEMA) == compile_grammar(DECISION_SCHEMA)
