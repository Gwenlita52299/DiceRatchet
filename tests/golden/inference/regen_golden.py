#!/usr/bin/env python
"""重新生成 MOD-03 golden（G2）；改 schema / 编译规则 / 修复 prompt 后运行并审阅 diff。

    python tests/golden/inference/regen_golden.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dice_ratchet.inference.schema import (DECISION_SCHEMA, compile_grammar,  # noqa: E402
                                        repair_prompt_text, validate)

BAD_OUTPUT = '{"reasoning": "ok", "plan": [{"kind": "teleport"}]}'


def main() -> int:
    out = Path(__file__).resolve().parent
    schema_text = json.dumps(DECISION_SCHEMA, ensure_ascii=False, indent=2, sort_keys=True)
    (out / "decision_schema.json").write_text(schema_text + "\n", encoding="utf-8")

    grammar = compile_grammar(DECISION_SCHEMA)
    assert grammar is not None, "DECISION_SCHEMA 必须能编译出 GBNF"
    (out / "decision.gbnf").write_text(grammar, encoding="utf-8")

    try:
        validate(json.loads(BAD_OUTPUT), DECISION_SCHEMA)
        raise AssertionError("BAD_OUTPUT 竟然通过了 schema")
    except Exception as exc: # noqa: BLE001 - 快照的就是这个错误
        prompt = repair_prompt_text(BAD_OUTPUT, exc, DECISION_SCHEMA)
    (out / "repair_prompt.txt").write_text(prompt + "\n", encoding="utf-8")

    print(f"golden 已更新：{out}")
    return 0


if __name__ == "__main__": # pragma: no cover
    raise SystemExit(main())
