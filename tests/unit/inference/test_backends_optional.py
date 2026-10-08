"""真实后端（MLX / llama.cpp）在本机依赖缺失时的**可测路径** + Mock/Manager 边角。

真实矩阵乘/子进程推理路径在 CI 上不可执行（无 mlx-lm / llama-server），本文件覆盖
加载前置校验、生命周期、错误码映射与共享 ``generate_json`` 骨架；真机跑批由
``inference probe/bench/robustness`` 承担（G6）。
"""

from __future__ import annotations

import asyncio

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.inference.backend import (BackendManager, BackendHealth, Message, MockBackend,
                                         NullInfra, estimate_prompt_tokens)
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.llama_backend import (LlamaCppBackend, LlamaServerBackend, _free_port,
                                               _server_args, llama_server_available)
from dice_ratchet.inference.mlx_backend import JSON_INSTRUCTION, MLXBackend
from dice_ratchet.inference.schema import DECISION_SCHEMA

from support import GOOD, BAD_JSON, build, inference_config

MESSAGES = [Message(role="user", content="phase=player_turn")]


def _spec(**overrides):
    from dice_ratchet.inference.registry import ModelSpec

    base = dict(model_id="m", backend="mlx", weights_path="", weights_sha256="",
                quantization="4bit", tokenizer_path=None, context_length=1024,
                kv_cache_bits=8, resident_gb_est=1.0, kv_gb_per_1k_tokens=0.1, max_inflight=1,
                supports_grammar=False, supports_json_mode=True)
    base.update(overrides)
    return ModelSpec(**base)


# =====================================================================
# MLXBackend
# =====================================================================

def test_mlx_backend_lifecycle_without_weights():
    backend = MLXBackend()
    assert backend.name == "mlx" and backend._supports_grammar is False

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.load(_spec(weights_path=str(__file__) + ".missing"))
        assert excinfo.value.code == E.E_LLM_UNAVAILABLE
        await backend.unload() # 未加载也能卸载（幂等）
        health = await backend.health()
        assert health.state == "unloaded" and health.resident_gb == 0.0

    asyncio.run(run())


def test_mlx_backend_rejects_wrong_backend_and_preload_generate():
    backend = MLXBackend()

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.load(_spec(backend="mock", weights_path=str(__file__)))
        assert excinfo.value.code == E.E_LLM_UNAVAILABLE
        with pytest.raises(InferenceError) as excinfo2:
            await backend.generate(MESSAGES)
        assert excinfo2.value.code == E.E_LLM_LOAD_FAILED

    asyncio.run(run())


def test_mlx_backend_rejects_grammar_explicitly():
    """MLX 无 GBNF：显式报错而非静默忽略（Spec P-09）。"""
    backend = MLXBackend()
    backend._state = "ready"
    backend._model = object() # 冒充已加载，走到 grammar 分支

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.generate(MESSAGES, grammar="root ::= \"x\"")
        assert excinfo.value.code == E.E_LLM_BACKEND_ERROR
        assert excinfo.value.retryable is False

    asyncio.run(run())


def test_mlx_backend_helpers_without_tokenizer():
    backend = MLXBackend()
    assert backend.count_tokens(MESSAGES) == estimate_prompt_tokens(MESSAGES)
    assert JSON_INSTRUCTION in backend._apply_chat_template(MESSAGES, json_mode=True)
    assert backend._count_prompt_tokens("abcdef") == 2
    assert backend._resident_gb() == 0.0

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.embed(["x"])
        assert excinfo.value.code == E.E_LLM_UNSUPPORTED

    asyncio.run(run())


# =====================================================================
# LlamaCppBackend / LlamaServerBackend
# =====================================================================

def test_llama_cpp_backend_preload_and_missing_weights(tmp_path):
    backend = LlamaCppBackend()
    assert backend.name == "llama_cpp" and backend._supports_grammar is True

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.load(_spec(backend="llama_cpp", weights_path=str(tmp_path / "x.gguf")))
        assert excinfo.value.code == E.E_LLM_UNAVAILABLE
        with pytest.raises(InferenceError) as excinfo2:
            await backend.generate(MESSAGES)
        assert excinfo2.value.code == E.E_LLM_LOAD_FAILED
        await backend.unload()
        with pytest.raises(InferenceError) as excinfo3:
            await backend.embed(["x"])
        assert excinfo3.value.code == E.E_LLM_UNSUPPORTED

    asyncio.run(run())


def test_llama_server_backend_server_args_are_loopback_and_deduped():
    spec = _spec(backend="llama_server", weights_path="m.gguf", kv_cache_bits=4,
                 server_args=["--parallel", "1", "--host", "0.0.0.0", "--port", "1234"])
    args = _server_args(spec, 8080, 8192)
    assert args[0] == "llama-server" and "-m" in args
    assert args[args.index("--host") + 1] == "127.0.0.1" # 只监听 loopback（NFR-03-06）
    assert args[args.index("--port") + 1] == "8080"
    assert "0.0.0.0" not in args and "1234" not in args # 注册表里的 host/port 被替换
    assert "--parallel" in args and "1" in args
    assert "--cache-type-k" in args and "q4_0" in args # kv_cache_bits=4（Q-4）


def test_free_port_is_usable():
    port = _free_port()
    assert 1024 < port < 65536


def test_llama_server_availability_is_boolean():
    assert isinstance(llama_server_available(), bool)


def test_llama_server_backend_without_binary(tmp_path, monkeypatch):
    from dice_ratchet.inference import llama_backend as llama_mod

    monkeypatch.setattr(llama_mod, "llama_server_available", lambda: False)
    backend = LlamaServerBackend()
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"x")
    spec = _spec(backend="llama_server", weights_path=str(weights))

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.load(spec)
        assert excinfo.value.code == E.E_LLM_LOAD_FAILED
        await backend.unload()
        health = await backend.health()
        assert health.state == "unloaded"
        with pytest.raises(InferenceError) as excinfo2:
            await backend.generate(MESSAGES)
        assert excinfo2.value.code == E.E_LLM_LOAD_FAILED
        with pytest.raises(InferenceError) as excinfo3:
            await backend.embed(["x"])
        assert excinfo3.value.code == E.E_LLM_UNSUPPORTED

    asyncio.run(run())


# =====================================================================
# MockBackend 行为与共享 generate_json 骨架
# =====================================================================

def test_mock_backend_maps_injected_exceptions():
    backend = MockBackend([MemoryError(), TimeoutError(), OSError("boom")])
    backend._state = "ready"
    backend._supports_grammar = True

    async def run():
        for expected in (E.E_LLM_OOM, E.E_LLM_TIMEOUT, E.E_LLM_BACKEND_ERROR):
            with pytest.raises(InferenceError) as excinfo:
                await backend.generate(MESSAGES)
            assert excinfo.value.code == expected
        health = await backend.health()
        assert health.last_error_code == str(E.E_LLM_BACKEND_ERROR)

    asyncio.run(run())


def test_mock_backend_sticky_script_and_delay():
    backend = MockBackend([GOOD], delay_ms=1)
    backend._state = "ready"

    async def run():
        first = await backend.generate(MESSAGES)
        second = await backend.generate(MESSAGES)
        assert first.text == second.text == GOOD
        assert backend.calls == 2 and backend.max_inflight == 1
        assert first.usage.latency_ms == 1 and first.usage.ttft_ms == 1

    asyncio.run(run())


def test_mock_backend_generate_json_direct_use():
    """后端自带的 generate_json 骨架（不经 manager）也要能跑三级策略。"""
    backend = MockBackend([BAD_JSON, GOOD])
    backend._state = "ready"
    backend._supports_grammar = True

    async def run():
        result = await backend.generate_json(MESSAGES, DECISION_SCHEMA, total_timeout_ms=5_000)
        assert result.attempts == 2 and result.parsed is not None
        assert result.retry_reasons == ["JSONDecodeError:$"]

    asyncio.run(run())


def test_mock_backend_requires_load_and_nonempty_script():
    backend = MockBackend([GOOD])
    with pytest.raises(ValueError):
        MockBackend([])

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await backend.generate(MESSAGES)
        assert excinfo.value.code == E.E_LLM_LOAD_FAILED

    asyncio.run(run())


# =====================================================================
# Manager 边角：自由文本 / count_tokens / embed / 截断 / debug dump
# =====================================================================

def test_manager_generate_free_text_and_optional_helpers():
    registry = build()
    manager, records, _ = _make_manager(registry, [GOOD])

    async def run():
        result = await manager.generate("mock-a", MESSAGES, max_tokens=32)
        assert result.text == GOOD and result.calls
        assert records[-1].purpose == "free_text" and records[-1].schema_id is None
        assert manager.count_tokens("mock-a", MESSAGES) > 0
        with pytest.raises(InferenceError) as excinfo:
            await manager.embed("mock-a", ["x"])
        assert excinfo.value.code == E.E_LLM_UNSUPPORTED
        await manager.unload_all()

    asyncio.run(run())


def test_manager_context_overflow_reject_and_truncate():
    registry = build(context_length=256)
    manager, _, _ = _make_manager(registry, [GOOD])
    long_messages = [Message(role="system", content="s" * 100),
                     Message(role="user", content="u" * 900)]

    async def run():
        with pytest.raises(InferenceError) as excinfo:
            await manager.generate_json("mock-a", long_messages, DECISION_SCHEMA,
                                        max_tokens=64, temperature=0.0, seed=1)
        assert excinfo.value.code == E.E_LLM_CONTEXT_OVERFLOW
        await manager.unload_all()

    asyncio.run(run())

    truncating = _make_manager(
        build(context_length=256),
        [GOOD],
        config=inference_config(context_overflow_policy="truncate"))[0]

    async def run_truncate():
        result = await truncating.generate_json("mock-a", long_messages, DECISION_SCHEMA,
                                                max_tokens=64, temperature=0.0, seed=1)
        assert result.truncated is True and result.parsed is not None
        await truncating.unload_all()

    asyncio.run(run_truncate())


def test_manager_reports_unloaded_health_for_registry_models():
    manager, _, _ = _make_manager(build(), [GOOD])

    async def run():
        health = await manager.health()
        assert (await manager.health())["mock-a"].state == "unloaded"
        await manager.load("mock-a")
        assert (await manager.health())["mock-a"].state == "ready"
        await manager.unload_all()
        return health

    health = asyncio.run(run())
    assert isinstance(health["mock-a"], BackendHealth)


def test_manager_debug_dump_raw_writes_attempt_text(tmp_path):
    from dice_ratchet.inference.backend import InfraBridge

    bridge = InfraBridge(run_id="r-1", runs_root=tmp_path, debug_dump_raw=True)
    manager = BackendManager(registry=build(), config=inference_config(debug_dump_raw=True),
                             infra=bridge,
                             backend_factory=lambda spec: MockBackend([GOOD],
                                                                      model_id=spec.model_id))

    async def run():
        result = await manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA,
                                             temperature=0.0, seed=1)
        await manager.unload_all()
        return result

    result = asyncio.run(run())
    dumped = tmp_path / "r-1" / "raw" / "llm" / f"{result.calls[0].call_id}.txt"
    assert dumped.is_file() and dumped.read_text(encoding="utf-8") == GOOD
    assert bridge.run_id() == "r-1"
    assert NullInfra().dump_raw("c-1", "x") is None


def test_manager_records_failed_calls_through_sink():
    """失败调用（三次耗尽）也必须落埋点（FR-03-06）。"""
    manager, records, _ = _make_manager(build(), [BAD_JSON])

    async def run():
        with pytest.raises(InferenceError):
            await manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA,
                                        temperature=0.0, seed=1, total_timeout_ms=5_000)
        await manager.unload_all()

    asyncio.run(run())
    assert len(records) == 3
    assert {r.outcome_kind for r in records} == {"error"}
    assert {r.error_code for r in records} == {str(E.E_LLM_SCHEMA_INVALID)}


def test_manager_uses_registry_fallback_backend_field():
    registry = build(extra={"fallback_backend": "mock"})
    assert registry.spec("mock-a").fallback_backend == "mock"
    assert registry.fallback_backend == "mock"


def _make_manager(registry, script, **kwargs):
    from support import make_mock_manager

    return make_mock_manager(registry, script, **kwargs)
