"""修复 prompt（PRD §8 模板 / Spec v1.4 B-28 独占拥有；§11 ``test_repair_prompt.py``）。"""

from __future__ import annotations

import json

import pytest
from jsonschema import ValidationError

from dice_ratchet.inference.backend import Message
from dice_ratchet.inference.schema import (DECISION_SCHEMA, REPAIR_SYSTEM, json_path_of,
                                        render_error, repair_messages, repair_prompt_text,
                                        validate)

BAD = '{"reasoning": "ok", "plan": [{"kind": "teleport"}]}'
MESSAGES = [Message(role="system", content="你是决策器"),
            Message(role="user", content="phase=player_turn")]


def _validation_error(payload: str) -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        validate(json.loads(payload), DECISION_SCHEMA)
    return excinfo.value


def test_repair_messages_appends_system_and_user_without_mutating_input():
    error = _validation_error(BAD)
    original = list(MESSAGES)
    repaired = repair_messages(MESSAGES, bad=BAD, error=error, schema=DECISION_SCHEMA)

    assert MESSAGES == original, "repair_messages 不得修改入参（不可变契约）"
    assert isinstance(repaired, list) and len(repaired) == len(MESSAGES) + 2
    assert repaired[0] is MESSAGES[0] and repaired[1] is MESSAGES[1]
    assert repaired[-2].role == "system" and repaired[-2].content == REPAIR_SYSTEM
    assert repaired[-1].role == "user"
    assert REPAIR_SYSTEM == ("你是 JSON 修复器。只输出一个符合给定 JSON Schema 的 JSON 对象，"
                             "不要解释，不要 markdown 代码块。")


def test_repair_user_message_contains_output_error_path_and_schema():
    error = _validation_error(BAD)
    text = repair_messages(MESSAGES, bad=BAD, error=error,
                           schema=DECISION_SCHEMA)[-1].content
    assert BAD in text
    assert "--- 原始输出 ---" in text and "--- 校验错误 ---" in text
    assert "--- 目标 Schema ---" in text and text.endswith("请输出修正后的 JSON。")
    assert "$.plan[0].kind" in text
    assert render_error(error) in text
    assert json.dumps('kind', ensure_ascii=False) in text # schema JSON 里出现字段名
    assert "maxItems" in text


def test_repair_prompt_handles_bad_json():
    with pytest.raises(json.JSONDecodeError) as excinfo:
        json.loads('{"reasoning": ')
    error = excinfo.value
    text = repair_prompt_text('{"reasoning": ', error, DECISION_SCHEMA)
    assert "JSONDecodeError" in text and "(line 1" in text
    assert json_path_of(error) == "$"


def test_repair_prompt_accepts_error_string():
    text = repair_prompt_text("bad", "some error", DECISION_SCHEMA)
    assert "some error @ $" in text


def test_repaired_conversation_is_what_the_next_attempt_sees(manager_factory):
    """修复轮次的 convo = 原 messages + system + user（PRD §8 时序）。"""
    import asyncio

    manager, _, _ = manager_factory(
        [BAD, '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}'])
    seen: list[list[Message]] = []

    async def run():
        backend = await manager.load("mock-a")
        original = backend.generate

        async def spy(messages, **kwargs):
            seen.append(list(messages))
            return await original(messages, **kwargs)

        backend.generate = spy # type: ignore[method-assign]
        return await manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA,
                                            total_timeout_ms=8_000)

    asyncio.run(run())
    assert seen[0] == MESSAGES
    assert [m.role for m in seen[1]] == ["system", "user", "system", "user"]
    assert "JSON 修复器" in seen[1][2].content
    assert BAD in seen[1][3].content
