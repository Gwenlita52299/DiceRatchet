"""重试与退避（FR-03-04/05；PRD §11 ``test_retry_backoff.py``）。"""

from __future__ import annotations

import asyncio

import pytest

from gamer_rsi.infra.errors import E
from gamer_rsi.inference.backend import Message
from gamer_rsi.inference.errors import InferenceError
from gamer_rsi.inference.schema import DECISION_SCHEMA

GOOD = '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}'
BAD_JSON = '{"reasoning": "ok", "plan": ['
BAD_KIND = '{"reasoning": "ok", "plan": [{"kind": "teleport"}]}'
MESSAGES = [Message(role="user", content="phase=player_turn")]


class FakeClock:
    """假时钟：``sleep`` 立即推进 ``now`` 并记录时长（观测 200/400 ms 退避）。"""

    def __init__(self, start_ms: float = 1_000.0) -> None:
        self.now_ms = float(start_ms)
        self.sleeps: list[int] = []

    def now(self) -> float:
        return self.now_ms

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds * 1000))
        self.now_ms += seconds * 1000.0


def _generate(manager, **kwargs):
    return asyncio.run(manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA, **kwargs))


def test_third_attempt_succeeds_with_doubling_backoff(manager_factory):
    clock = FakeClock()
    manager, records, _ = manager_factory([BAD_JSON, BAD_KIND, GOOD], clock=clock)
    result = _generate(manager, total_timeout_ms=8_000)
    assert result.attempts == 3
    assert result.retry_reasons == ["JSONDecodeError:$", "ValidationError:$.plan[0].kind"]
    assert clock.sleeps == [200, 400]
    assert result.parsed == {"reasoning": "ok", "plan": [{"kind": "end_turn"}]}
    assert result.outcome_kind == "action"
    assert result.raw_texts == [BAD_JSON, BAD_KIND, GOOD]
    # FR-03-06：每次尝试一条记录；attempts 记真实尝试次数
    assert [r.attempt for r in records] == [1, 2, 3]
    assert {r.attempts for r in records} == {3}
    assert [r.outcome_kind for r in records] == ["error", "error", "action"]
    assert records[0].error_code == E.E_LLM_SCHEMA_INVALID
    assert records[-1].error_code is None
    assert records[0].retry_reasons == ("JSONDecodeError:$",)
    assert records[2].retry_reasons == ("JSONDecodeError:$", "ValidationError:$.plan[0].kind")


def test_exhaustion_raises_schema_invalid(manager_factory):
    clock = FakeClock()
    manager, records, _ = manager_factory([BAD_KIND, BAD_KIND, BAD_KIND], clock=clock)
    with pytest.raises(InferenceError) as excinfo:
        _generate(manager, total_timeout_ms=8_000)
    error = excinfo.value
    assert error.code == E.E_LLM_SCHEMA_INVALID
    assert error.attempts == 3 and error.retryable is False
    assert error.detail["raw_texts"] == [BAD_KIND] * 3
    # 最后一步不再退避（尝试已耗尽）
    assert clock.sleeps == [200, 400]
    assert [r.outcome_kind for r in records] == ["error"] * 3
    assert {r.attempts for r in records} == {3}


def test_max_attempts_one_means_no_repair(manager_factory):
    clock = FakeClock()
    manager, records, _ = manager_factory([BAD_JSON, GOOD], clock=clock)
    with pytest.raises(InferenceError) as excinfo:
        _generate(manager, max_attempts=1, total_timeout_ms=8_000)
    assert excinfo.value.attempts == 1
    assert clock.sleeps == []
    assert len(records) == 1 and records[0].attempts == 1


def test_retry_backoff_ms_is_overridable(manager_factory):
    clock = FakeClock()
    manager, _, _ = manager_factory([BAD_JSON, BAD_JSON, GOOD], clock=clock)
    result = _generate(manager, retry_backoff_ms=10, total_timeout_ms=8_000)
    assert result.attempts == 3 and clock.sleeps == [10, 20]


def test_repair_conversation_is_appended_not_mutated(manager_factory):
    clock = FakeClock()
    manager, _, _ = manager_factory([BAD_JSON, GOOD], clock=clock)
    messages = [Message(role="system", content="s"), Message(role="user", content="u")]
    result = asyncio.run(manager.generate_json("mock-a", messages, DECISION_SCHEMA,
                                               total_timeout_ms=8_000))
    assert result.attempts == 2
    assert messages == [Message(role="system", content="s"), Message(role="user", content="u")]
    assert len(messages) == 2 # 原始入参不可变（repair 走副本）
