"""G2 Golden：``DECISION_SCHEMA`` / GBNF 产物 / 修复 prompt 快照（PRD §12 G2）。

快照变更必须显式重新生成并在 PR 说明：

    python tests/golden/inference/regen_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.inference.schema import (DECISION_SCHEMA, compile_grammar,
                                        load_decision_schema, repair_prompt_text, validate)

GOLDEN = Path(__file__).resolve().parent
REPO = GOLDEN.parents[2]
BAD_OUTPUT = '{"reasoning": "ok", "plan": [{"kind": "teleport"}]}'


def _read(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def test_decision_schema_golden_matches_runtime():
    golden = json.loads(_read("decision_schema.json"))
    assert golden == DECISION_SCHEMA, "DECISION_SCHEMA 漂移：运行 regen_golden.py 并审阅 diff"
    canonical = json.dumps(DECISION_SCHEMA, ensure_ascii=False, indent=2, sort_keys=True)
    assert _read("decision_schema.json") == canonical + "\n"


def test_decision_schema_file_is_the_single_source_of_truth():
    """golden 与 ``schemas/decision.schema.json`` 必须同源（Spec §4.3 / B-28）。"""
    on_disk = json.loads((REPO / "schemas" / "decision.schema.json").read_text(encoding="utf-8"))
    assert on_disk == load_decision_schema()
    assert on_disk == json.loads(_read("decision_schema.json"))


def test_gbnf_golden_matches_runtime():
    grammar = compile_grammar(DECISION_SCHEMA)
    assert grammar is not None
    assert _read("decision.gbnf") == grammar, "GBNF 漂移：运行 regen_golden.py 并审阅 diff"


def test_repair_prompt_golden_matches_runtime():
    with pytest.raises(Exception) as excinfo:
        validate(json.loads(BAD_OUTPUT), DECISION_SCHEMA)
    prompt = repair_prompt_text(BAD_OUTPUT, excinfo.value, DECISION_SCHEMA)
    assert _read("repair_prompt.txt") == prompt + "\n", \
        "修复 prompt 漂移：运行 regen_golden.py 并审阅 diff"


def test_golden_exposed_by_schema_module_matches_decision_schema():
    assert DECISION_SCHEMA["properties"]["plan"]["maxItems"] == 12
    assert DECISION_SCHEMA["properties"]["reasoning"]["maxLength"] == 200
