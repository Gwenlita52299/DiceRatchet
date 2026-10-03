"""超时不留半截结果（FR-03-05 / NFR / PRD §11 ``test_timeout.py``）。"""

from __future__ import annotations

import asyncio

import pytest

from gamer_rsi.infra.errors import E
from gamer_rsi.inference.backend import Message
from gamer_rsi.inference.errors import InferenceError
from gamer_rsi.inference.schema import DECISION_SCHEMA

BAD_JSON = '{"reasoning": "ok", "plan": ['
MESSAGES = [Message(role="user", content="phase=player_turn")]


def test_total_timeout_raises_and_keeps_no_partial_output(manager_factory):
    # 每次 generate 睡 800ms + 退避 200ms → 1000ms 预算在第 2 次尝试前耗尽
    manager, records, backends = manager_factory([BAD_JSON], delay_ms=800)
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA,
                                          total_timeout_ms=1_000, retry_backoff_ms=200))
    error = excinfo.value
    assert error.code == E.E_LLM_TIMEOUT and error.retryable is True
    raw_texts = error.detail["raw_texts"]
    # raw_texts 只含**完整**返回；没有半截输出
    assert all(text == BAD_JSON for text in raw_texts)
    assert len(raw_texts) <= 1
    assert error.attempts == len(raw_texts)
    assert backends["mock-a"].calls == len(raw_texts)


def test_single_generate_timeout_discards_inflight_text(manager_factory):
    # 单次 generate 就超过预算：wait_for 取消，raw_texts 必为空（不留半截）
    manager, records, _ = manager_factory(['{"reasoning": "late"}'], delay_ms=500)
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA,
                                          total_timeout_ms=100))
    assert excinfo.value.code == E.E_LLM_TIMEOUT
    assert excinfo.value.detail["raw_texts"] == []
    assert records == []


def test_timeout_detail_carries_retry_reasons(manager_factory):
    manager, _, _ = manager_factory([BAD_JSON], delay_ms=800)
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA,
                                          total_timeout_ms=1_000, retry_backoff_ms=200))
    reasons = excinfo.value.detail["retry_reasons"]
    assert reasons and reasons[0].startswith("JSONDecodeError")


def test_generate_timeout_param_is_forwarded(manager_factory):
    """``generate_timeout_ms`` 由 manager.generate（自由文本）透传。"""
    manager, _, _ = manager_factory(['{"a": 1}'], delay_ms=300)
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(manager.generate("mock-a", MESSAGES, timeout_ms=50))
    assert excinfo.value.code == E.E_LLM_TIMEOUT
