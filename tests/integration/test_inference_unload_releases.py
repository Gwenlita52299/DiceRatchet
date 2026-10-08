"""卸载释放与 session 生命周期（FR-03-02 / R-4；PRD §11 ``test_unload_releases.py``）。"""

from __future__ import annotations

import asyncio

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.inference.backend import Message
from dice_ratchet.inference.errors import InferenceError

from inference_support import GOOD, MODEL_ID, mock_factory, model_entry, registry_of

MESSAGES = [Message(role="user", content="phase=player_turn")]


def test_unload_releases_and_reports_unloaded(manager_builder):
    factory, created = mock_factory([GOOD], delay_ms=0)
    manager = manager_builder(registry_of(model_entry(resident_gb_est=5.6)),
                              factory=factory)

    async def run():
        await manager.load(MODEL_ID)
        ready = (await manager.health())[MODEL_ID]
        await manager.unload(MODEL_ID)
        unloaded = (await manager.health())[MODEL_ID]
        return ready, unloaded

    ready, unloaded = asyncio.run(run())
    assert ready.state == "ready" and ready.resident_gb == pytest.approx(5.6)
    assert unloaded.state == "unloaded"
    assert (unloaded.resident_gb or 0.0) < 1.0
    assert created[MODEL_ID]._spec is None # 权重引用已释放


def test_unload_is_idempotent_even_when_never_loaded(manager_builder):
    factory, _ = mock_factory([GOOD])
    manager = manager_builder(registry_of(), factory=factory)

    async def run():
        await manager.unload(MODEL_ID) # 未加载：不报错（FR-03-02）
        await manager.load(MODEL_ID)
        await manager.unload(MODEL_ID)
        await manager.unload(MODEL_ID)
        return (await manager.health())[MODEL_ID]

    health = asyncio.run(run())
    assert health.state == "unloaded"


def test_get_before_load_raises_unavailable(manager_builder):
    factory, _ = mock_factory([GOOD])
    manager = manager_builder(registry_of(), factory=factory)
    with pytest.raises(InferenceError) as excinfo:
        manager.get(MODEL_ID)
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    with pytest.raises(InferenceError):
        manager.get("unknown-model")


def test_session_context_manager_auto_unloads(manager_builder):
    factory, _ = mock_factory([GOOD])
    manager = manager_builder(registry_of(), factory=factory)

    async def run():
        async with manager.session(MODEL_ID) as backend:
            inside = await backend.health()
        return inside, (await manager.health())[MODEL_ID]

    inside, after = asyncio.run(run())
    assert inside.state == "ready"
    assert after.state == "unloaded"


def test_unload_all_releases_every_model():
    from dice_ratchet.inference.backend import BackendManager
    from dice_ratchet.inference.registry import build_registry

    entries = {
        "m1": model_entry(),
        "m2": model_entry(max_inflight=2),
    }
    registry = build_registry({"registry_version": 1, "ram_budget_gb": 20.0, "models": entries})
    created: dict[str, object] = {}

    def factory(spec):
        from dice_ratchet.inference.backend import MockBackend

        backend = MockBackend([GOOD], model_id=spec.model_id)
        created[spec.model_id] = backend
        return backend

    manager = BackendManager(registry=registry, backend_factory=factory)

    async def run():
        await manager.load("m1")
        await manager.load("m2")
        await manager.unload_all()
        return await manager.health()

    health = asyncio.run(run())
    assert {state.state for state in health.values()} == {"unloaded"}
    assert all(backend._spec is None for backend in created.values())
