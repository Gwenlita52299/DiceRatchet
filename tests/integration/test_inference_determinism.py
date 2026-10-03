"""确定性集成（G3 / INV-6 / FR-03-12；PRD §11 ``test_determinism.py``）。

评估口径：``mode="eval", temperature=0, seed=1042`` → 同 prompt 连跑 3 次文本逐字节一致，
``prompt_digest`` 一致；换 seed 后**允许**不同（不承诺 L2 跨后端文本一致）。
"""

from __future__ import annotations

import asyncio

from gamer_rsi.inference.backend import Message
from gamer_rsi.inference.records import prompt_digest
from gamer_rsi.inference.schema import DECISION_SCHEMA

from inference_support import GOOD, GOOD2, MODEL_ID, mock_factory, registry_of

MESSAGES = [Message(role="system", content="s"), Message(role="user", content="u")]


def _run_three(manager, messages):
    async def run():
        out = []
        for _ in range(3):
            out.append(await manager.generate_json(MODEL_ID, messages, DECISION_SCHEMA,
                                                   temperature=0.0, seed=1042))
        await manager.unload_all()
        return out

    return asyncio.run(run())


def test_eval_mode_is_byte_identical_across_three_runs(manager_builder):
    factory, created = mock_factory([GOOD, GOOD, GOOD])
    records = []
    manager = manager_builder(registry_of(), factory=factory, mode="eval",
                              sink=records.append)
    results = _run_three(manager, MESSAGES)

    texts = {r.text for r in results}
    digests = {r.prompt_digest for r in results}
    assert len(texts) == 1, f"评估模式文本必须逐字节一致：{texts}"
    assert len(digests) == 1
    assert all(r.attempts == 1 and r.parsed is not None for r in results)
    # 采样口径固定（B-14）
    for record in records:
        assert record.mode == "eval" and record.temperature == 0.0
        assert record.top_p == 1.0 and record.top_k == 0
        assert record.sampling_seed == 1042


def test_same_prompt_digest_recomputes_offline(manager_builder):
    factory, _ = mock_factory([GOOD])
    manager = manager_builder(registry_of(), factory=factory, mode="eval")
    result = _run_three(manager, MESSAGES)[0]
    assert result.prompt_digest == prompt_digest(MESSAGES)


def test_different_seed_may_change_text(manager_builder):
    """换 seed 允许不同（mock 的脚本不同 ⇒ 文本不同），但都必须是合法 JSON。"""
    factory, _ = mock_factory([GOOD, GOOD2])
    manager = manager_builder(registry_of(), factory=factory, mode="eval")

    async def run():
        first = await manager.generate_json(MODEL_ID, MESSAGES, DECISION_SCHEMA,
                                            temperature=0.0, seed=1)
        second = await manager.generate_json(MODEL_ID, MESSAGES, DECISION_SCHEMA,
                                             temperature=0.0, seed=2)
        await manager.unload_all()
        return first, second

    first, second = asyncio.run(run())
    assert first.text != second.text
    assert first.prompt_digest == second.prompt_digest # prompt 未变
    for result in (first, second):
        assert result.parsed is not None
