"""后端一致性套件（NFR-03-07 / PRD §11 ``test_backend_protocol.py``）。

同一套「load → health → generate → generate_json → unload」用例覆盖全部实现：
``MockBackend``（零依赖）、本文件内的 ``StubBackend``（证明套件不绑定 Mock），以及
MLX / llama.cpp 三个真实实现的**契约面**检查（依赖缺失时只验 isinstance 与方法齐备）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import pytest

from gamer_rsi.inference.backend import (Backend, BackendHealth, GenerationResult,
                                         LocalBackend, Message, MockBackend, Usage,
                                         make_result, estimate_prompt_tokens)
from gamer_rsi.inference.schema import DECISION_SCHEMA

GOOD = '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}'

SYSTEM = Message(role="system", content="只输出 JSON")
USER = Message(role="user", content="phase=player_turn；给出计划")


class StubBackend(LocalBackend):
    """最小可用的第二种实现（非 Mock），用于证明一致性套件与实现解耦。"""

    name = "mock"
    model_id = "stub-1"

    def __init__(self, text: str = GOOD) -> None:
        self.text = text
        self.model_id = "stub-1"
        self._state = "unloaded"
        self._supports_grammar = True
        self._supports_json_mode = True

    async def load(self, spec) -> None:
        self.model_id = spec.model_id
        self._state = "ready"

    async def unload(self) -> None:
        self._state = "unloaded"

    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        text = self.text
        usage = Usage(prompt_tokens=estimate_prompt_tokens(messages),
                      completion_tokens=len(text) // 4, latency_ms=1, ttft_ms=0,
                      prefill_ms=0, decode_ms=1)
        return make_result(text=text, usage=usage, backend=self.name, model_id=self.model_id,
                           messages=messages)

    async def health(self) -> BackendHealth:
        return BackendHealth(state=self._state, # type: ignore[arg-type]
                             backend=self.name, model_id=self.model_id, resident_gb=0.0,
                             inflight=0, last_error_code=None)


def _assert_contract_surface(backend: Any) -> None:
    assert isinstance(backend, Backend), f"{type(backend).__name__} 不满足 Backend 契约"
    for attr in ("name", "model_id"):
        assert hasattr(backend, attr), f"缺少属性 {attr}"
    for method in ("load", "unload", "generate", "generate_json", "health"):
        assert callable(getattr(backend, method, None)), f"缺少方法 {method}"


async def _run_conformance(backend: Any, spec) -> None:
    _assert_contract_surface(backend)
    assert (await backend.health()).state == "unloaded"
    await backend.load(spec)
    assert (await backend.health()).state == "ready"
    await backend.load(spec) # 幂等
    result = await backend.generate([SYSTEM, USER], max_tokens=64)
    assert isinstance(result, GenerationResult)
    assert result.text and result.model_id == spec.model_id
    assert result.usage.prompt_tokens > 0 and result.usage.latency_ms >= 0
    parsed = await backend.generate_json([SYSTEM, USER], DECISION_SCHEMA)
    assert parsed.parsed is not None and parsed.attempts >= 1
    assert parsed.retry_reasons == [] and parsed.prompt_digest.startswith("sha256:")
    await backend.unload()
    health = await backend.health()
    assert health.state == "unloaded" and (health.resident_gb or 0.0) < 1.0
    await backend.unload() # 幂等


@pytest.mark.parametrize("factory", [
    pytest.param(lambda spec: MockBackend([GOOD], model_id=spec.model_id), id="mock"),
    pytest.param(lambda spec: StubBackend(), id="stub"),
])
def test_backend_conformance_suite(factory, registry_factory):
    spec = registry_factory().spec("mock-a")
    asyncio.run(_run_conformance(factory(spec), spec))


def test_protocol_rejects_incomplete_implementation():
    class NotABackend:
        name = "mock"

    assert not isinstance(NotABackend(), Backend)


@pytest.mark.parametrize("module,name", [
    ("gamer_rsi.inference.mlx_backend", "MLXBackend"),
    ("gamer_rsi.inference.llama_backend", "LlamaCppBackend"),
    ("gamer_rsi.inference.llama_backend", "LlamaServerBackend"),
])
def test_real_backends_satisfy_contract_surface(module: str, name: str):
    """真实后端只需依赖缺失时不 ImportError 即可验契约面（懒加载设计）。"""
    imported = pytest.importorskip(module)
    backend = getattr(imported, name)()
    _assert_contract_surface(backend)
    assert backend.name in ("mlx", "llama_cpp", "llama_server")


def test_conformance_requires_five_methods_and_two_attributes():
    """NFR-03-07：新增后端 = 5 个方法 + name/model_id。"""
    documented = {"load", "unload", "generate", "generate_json", "health"}
    for attr in documented:
        assert hasattr(Backend, attr)
    assert "name" in getattr(Backend, "__annotations__", {})
    assert "model_id" in getattr(Backend, "__annotations__", {})
