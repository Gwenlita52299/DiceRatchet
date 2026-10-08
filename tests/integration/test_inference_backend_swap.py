"""后端可换（NFR-03-07 L2；PRD §11 ``test_backend_swap.py``）。

同一份 ``DECISION_SCHEMA`` 打到两种后端行为（有 GBNF vs 只有 JSON mode）：两者 ``parsed``
都必须通过 schema。**不**断言文本相同（NFR-03-01 L2：kernel/量化不同）。
"""

from __future__ import annotations

import asyncio

from dice_ratchet.inference.schema import DECISION_SCHEMA, validate

from inference_support import GOOD, GOOD2, MODEL_ID, mock_factory, model_entry, registry_of

MESSAGES_USER = "phase=player_turn；给出计划"


def _generate(manager):
    from dice_ratchet.inference.backend import Message

    async def run():
        result = await manager.generate_json(
            MODEL_ID, [Message(role="user", content=MESSAGES_USER)], DECISION_SCHEMA,
            temperature=0.0, seed=3)
        await manager.unload_all()
        return result

    return asyncio.run(run())


def test_grammar_and_json_mode_backends_both_validate(manager_builder):
    grammar_factory, _ = mock_factory([GOOD]) # 类比 llama.cpp（GBNF 可用）
    json_factory, _ = mock_factory([GOOD2]) # 类比 MLX（supports_grammar=false）

    grammar_manager = manager_builder(
        registry_of(model_entry(supports_grammar=True)), factory=grammar_factory)
    json_mode_manager = manager_builder(
        registry_of(model_entry(supports_grammar=False)), factory=json_factory)

    grammar_result = _generate(grammar_manager)
    json_result = _generate(json_mode_manager)

    validate(grammar_result.parsed, DECISION_SCHEMA)
    validate(json_result.parsed, DECISION_SCHEMA)
    assert grammar_result.parsed != json_result.parsed # 文本/取值可以不同（L2 不承诺）
    assert grammar_result.outcome_kind == "action"
    assert json_result.outcome_kind == "degraded" # 约束解码不可用但校验通过（§10②）
    assert json_result.degraded_reason == "grammar_unavailable"


def test_json_mode_still_repairs_invalid_output(manager_builder):
    """无 GBNF 时三级修复仍然工作（R-2 的对策）。"""
    factory, _ = mock_factory(['{"reasoning": "ok", "plan": [{"kind": "nope"}]}', GOOD2])
    manager = manager_builder(registry_of(model_entry(supports_grammar=False)),
                              factory=factory)
    result = _generate(manager)
    assert result.attempts == 2
    assert result.outcome_kind == "degraded"
    assert result.retry_reasons == ["ValidationError:$.plan[0].kind"]


def test_grammar_unavailable_and_no_json_mode_errors(manager_builder):
    """grammar 编译失败 + 未声明 JSON mode → E_LLM_BACKEND_ERROR（Spec P-09）。"""
    from dice_ratchet.infra.errors import E
    from dice_ratchet.inference.backend import Message
    from dice_ratchet.inference.errors import InferenceError

    factory, _ = mock_factory([GOOD])
    manager = manager_builder(
        registry_of(model_entry(supports_grammar=True, supports_json_mode=False)),
        factory=factory)
    unsupported = {"type": "object", "allOf": [{"type": "object"}]}

    async def run():
        await manager.generate_json(MODEL_ID, [Message(role="user", content="x")],
                                    unsupported, temperature=0.0, seed=1)

    try:
        asyncio.run(run())
    except InferenceError as exc:
        assert exc.code == E.E_LLM_BACKEND_ERROR and exc.retryable is False
    else: # pragma: no cover - 不应走到
        raise AssertionError("应抛 E_LLM_BACKEND_ERROR")
