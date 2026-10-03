"""并发排队语义（FR-03-08 / R-5 / P-10；PRD §11 ``test_concurrency.py``）。

`max_inflight=1` + 4 个并发 `generate_json` → 观测最大 inflight == 1、全部成功、
总墙钟 ≈ 4 × 单次延迟（局级并发靠排队，不做模型级批处理）。
"""

from __future__ import annotations

import asyncio
import time

from gamer_rsi.inference.backend import Message
from gamer_rsi.inference.schema import DECISION_SCHEMA

from inference_support import GOOD, MODEL_ID, mock_factory, model_entry, registry_of

MESSAGES = [Message(role="user", content="phase=player_turn")]
DELAY_MS = 60


def _gather(manager, n: int):
    async def run():
        started = time.monotonic()
        results = await asyncio.gather(*[
            manager.generate_json(MODEL_ID, MESSAGES, DECISION_SCHEMA,
                                  temperature=0.0, seed=index)
            for index in range(n)])
        wall_ms = (time.monotonic() - started) * 1000
        await manager.unload_all()
        return results, wall_ms

    return asyncio.run(run())


def test_single_flight_serializes_four_calls(manager_builder):
    factory, created = mock_factory([GOOD], delay_ms=DELAY_MS)
    manager = manager_builder(registry_of(model_entry(max_inflight=1)), factory=factory)
    results, wall_ms = _gather(manager, 4)

    assert all(r.ok for r in results)
    assert manager.max_inflight_observed == 1 # 观测最大 inflight == 1
    assert created[MODEL_ID].max_inflight == 1
    assert wall_ms >= 4 * DELAY_MS * 0.9, f"应排队 4 次：{wall_ms:.0f}ms"


def test_parallelism_respects_registry_limit(manager_builder):
    factory, created = mock_factory([GOOD], delay_ms=DELAY_MS)
    manager = manager_builder(registry_of(model_entry(max_inflight=3)), factory=factory)
    results, wall_ms = _gather(manager, 3)

    assert all(r.ok for r in results)
    assert 1 <= manager.max_inflight_observed <= 3
    assert manager.max_inflight_observed >= 2, "limit=3 时应出现真并发"
    assert wall_ms < 3 * DELAY_MS, f"3 路并发应快于串行：{wall_ms:.0f}ms"


def test_eval_mode_forces_inflight_one(manager_builder):
    factory, _ = mock_factory([GOOD], delay_ms=DELAY_MS)
    manager = manager_builder(registry_of(model_entry(max_inflight=4)), factory=factory,
                              mode="eval")
    results, wall_ms = _gather(manager, 3)
    assert all(r.ok for r in results)
    assert manager.max_inflight_observed == 1
    assert wall_ms >= 3 * DELAY_MS * 0.9


def test_health_reports_inflight_after_completion(manager_builder):
    factory, _ = mock_factory([GOOD])
    manager = manager_builder(registry_of(), factory=factory)
    _gather(manager, 1)
    health = asyncio.run(manager.health())
    assert health[MODEL_ID].inflight == 0
