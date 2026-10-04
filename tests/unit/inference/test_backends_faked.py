"""用**假 mlx_lm / llama_cpp 模块**与本地 HTTP 桩覆盖真实后端的代码路径。

CI 上没有 mlx-lm / 权重 / llama-server 模型，但 ``_load_sync`` / ``_generate_sync`` /
HTTP 客户端 / 子进程收尾这些**我方代码**必须被测到。做法：往 ``sys.modules`` 注入最小
替身，或用 ``http.server`` + 短命子进程做真实 I/O。
"""

from __future__ import annotations

import asyncio
import http.server
import json
import subprocess
import sys
import threading
import types
import urllib.error
from dataclasses import replace

import pytest

from gamer_rsi.infra.config import InferenceConfig
from gamer_rsi.infra.errors import E
from gamer_rsi.inference import llama_backend as llama_mod
from gamer_rsi.inference.backend import Message, Usage, make_result
from gamer_rsi.inference.errors import InferenceError
from gamer_rsi.inference.llama_backend import (LlamaCppBackend, LlamaServerBackend, _get_ok,
                                               _post_json)
from gamer_rsi.inference.mlx_backend import MLXBackend
from gamer_rsi.inference.registry import ModelSpec

MESSAGES = [Message(role="system", content="s"), Message(role="user", content="u")]


def _spec(**overrides) -> ModelSpec:
    base = dict(model_id="m", backend="mlx", weights_path="", weights_sha256="",
                quantization="4bit", tokenizer_path=None, context_length=1024,
                kv_cache_bits=8, resident_gb_est=1.0, kv_gb_per_1k_tokens=0.1,
                max_inflight=1, supports_grammar=False, supports_json_mode=True)
    base.update(overrides)
    return ModelSpec(**base)


# =====================================================================
# 假 mlx_lm
# =====================================================================

class _FakeResponse:
    def __init__(self, text: str, tokens: int = 1) -> None:
        self.text = text
        self.token_ids = list(range(tokens))


class _FakeTokenizer:
    def __init__(self) -> None:
        self.templated: list[list[dict]] = []

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        self.templated.append(list(messages))
        return "TEMPLATED:" + "|".join(m["content"] for m in messages)

    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


@pytest.fixture
def fake_mlx(monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("mlx_lm")
    sample = types.ModuleType("mlx_lm.sample_utils")
    calls: dict[str, object] = {"stream": [], "sampler": None}

    def load(path, tokenizer_path=None):
        calls["load"] = (path, tokenizer_path)
        return "MODEL", _FakeTokenizer()

    def stream_generate(model, tokenizer, prompt=None, max_tokens=8, sampler=None, seed=None):
        calls["stream"].append({"prompt": prompt, "max_tokens": max_tokens, "seed": seed})
        yield _FakeResponse('{"kind"', 2)
        yield _FakeResponse(': "end_turn"}', 3)

    module.load = load
    module.stream_generate = stream_generate
    sample.make_sampler = lambda **kwargs: calls.__setitem__("sampler", kwargs) or "SAMPLER"
    monkeypatch.setitem(sys.modules, "mlx_lm", module)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample)

    # 假 ``mlx.core``：让常驻显存（NFR-03-03）与显式 seed（INV-6）在**装没装真 mlx**
    # 的环境下都确定。2026-10-04（M1 验收 F14）：真 mlx 一旦安装，``_resident_gb()``
    # 会走真实 ``mx.get_active_memory()``，本用例原先只因「mlx 未安装」才拿到 1.0；
    # 且 mlx-lm ≥0.2x 的 ``generate_step`` 已无 ``seed`` 形参，显式 seed 必须走
    # ``mx.random.seed``，故断言点也从「转发 seed= 给 stream_generate」改为
    # 「seed 真的设进了 MLX 全局 RNG」。
    core = types.ModuleType("mlx.core")
    core.get_active_memory = lambda: 1_000_000_000
    core.clear_cache = lambda: None
    core_rng = types.SimpleNamespace(seed=lambda value: calls.__setitem__("mx_seed", int(value)))
    core.random = core_rng
    mlx_pkg = types.ModuleType("mlx")
    mlx_pkg.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx_pkg)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return calls


def test_mlx_load_and_generate_with_fake_runtime(tmp_path, fake_mlx):
    weights = tmp_path / "model"
    weights.mkdir()
    (weights / "model.safetensors").write_bytes(b"w")
    backend = MLXBackend()
    spec = _spec(weights_path=str(weights))

    async def run():
        await backend.load(spec)
        health = await backend.health()
        result = await backend.generate(MESSAGES, max_tokens=8, seed=7)
        tokens = backend.count_tokens(MESSAGES)
        templated = backend._apply_chat_template(MESSAGES)
        await backend.unload()
        return health, result, tokens, templated

    health, result, tokens, templated = asyncio.run(run())
    assert health.state == "ready" and health.resident_gb == 1.0
    assert result.text == '{"kind": "end_turn"}'
    assert result.usage.prompt_tokens > 0 and result.usage.completion_tokens == 5
    assert result.usage.ttft_ms is not None and result.usage.decode_ms is not None
    # 显式 seed 必须真的设进 MLX 全局 RNG（mlx-lm ≥0.2x 的 generate_step 无 seed 形参）
    assert fake_mlx.get("mx_seed") == 7
    assert fake_mlx["load"][0] == str(weights)
    assert tokens > 0
    assert "TEMPLATED" in templated


def test_mlx_json_mode_prepends_instruction(tmp_path, fake_mlx):
    backend = MLXBackend()
    backend._tokenizer = _FakeTokenizer()
    prompt = backend._apply_chat_template(MESSAGES, json_mode=True)
    assert prompt.startswith("TEMPLATED:")
    assert backend._tokenizer.templated[0][0]["role"] == "system"
    assert "JSON" in backend._tokenizer.templated[0][0]["content"]
    # 重复调用不重复注入
    backend._apply_chat_template(MESSAGES, json_mode=True)
    assert sum(1 for m in backend._tokenizer.templated[1] if "JSON" in m["content"]) == 1


def test_mlx_generate_timeout_and_oom_mapping(monkeypatch):
    backend = MLXBackend()
    backend._state = "ready"
    backend._model = object()

    def slow(*args, **kwargs):
        import time

        time.sleep(0.2)
        return make_result(text="late", usage=Usage(1, 1, 1), backend="mlx", model_id="m",
                           messages=MESSAGES)

    monkeypatch.setattr(backend, "_generate_sync", slow)
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(backend.generate(MESSAGES, timeout_ms=20))
    assert excinfo.value.code == E.E_LLM_TIMEOUT

    def oom(*args, **kwargs):
        raise MemoryError("metal")

    monkeypatch.setattr(backend, "_generate_sync", oom)
    with pytest.raises(InferenceError) as excinfo2:
        asyncio.run(backend.generate(MESSAGES))
    assert excinfo2.value.code == E.E_LLM_OOM

    def boom(*args, **kwargs):
        raise RuntimeError("mlx bug")

    monkeypatch.setattr(backend, "_generate_sync", boom)
    with pytest.raises(InferenceError) as excinfo3:
        asyncio.run(backend.generate(MESSAGES))
    assert excinfo3.value.code == E.E_LLM_BACKEND_ERROR and excinfo3.value.retryable is True


def test_mlx_missing_dependency_is_unavailable(tmp_path, monkeypatch):
    weights = tmp_path / "m.bin"
    weights.write_bytes(b"w")
    monkeypatch.setitem(sys.modules, "mlx_lm", None) # 模拟未安装
    backend = MLXBackend()
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(backend.load(_spec(weights_path=str(weights))))
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "mlx-lm" in excinfo.value.message

    async def health():
        return await backend.health()

    assert asyncio.run(health()).state == "error"


# =====================================================================
# 假 llama_cpp
# =====================================================================

@pytest.fixture
def fake_llama_cpp(monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("llama_cpp")
    calls: dict[str, object] = {"payload": None}

    class LlamaGrammar:
        @staticmethod
        def from_string(text):
            return {"grammar": text}

    class Llama:
        def __init__(self, **kwargs):
            calls["init"] = kwargs
            self.raise_exc: BaseException | None = None

        def create_chat_completion(self, **payload):
            calls["payload"] = payload
            if self.raise_exc is not None:
                raise self.raise_exc
            return {"choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5},
                    "timings": {"prompt_ms": 3.0, "predicted_ms": 7.0}}

    module.Llama = Llama
    module.LlamaGrammar = LlamaGrammar
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    return calls


def test_llama_cpp_load_and_generate_with_fake_runtime(tmp_path, fake_llama_cpp):
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"w")
    backend = LlamaCppBackend()
    spec = _spec(backend="llama_cpp", weights_path=str(weights), supports_grammar=True)

    async def run():
        await backend.load(spec)
        result = await backend.generate(MESSAGES, max_tokens=16, seed=3,
                                        grammar='root ::= "{}"')
        health = await backend.health()
        await backend.unload()
        return result, health

    result, health = asyncio.run(run())
    assert result.text == '{"ok": true}' and result.model_id == "m"
    assert result.usage.prompt_tokens == 11 and result.usage.prefill_ms == 3
    assert health.state == "ready" and fake_llama_cpp["init"]["n_ctx"] == 1024
    assert fake_llama_cpp["payload"]["grammar"] == {"grammar": 'root ::= "{}"'}


def test_llama_cpp_json_mode_and_grammar_timeout_oom(tmp_path, fake_llama_cpp, monkeypatch):
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"w")
    backend = LlamaCppBackend()
    spec = _spec(backend="llama_cpp", weights_path=str(weights))

    async def run():
        await backend.load(spec)
        await backend.generate(MESSAGES, json_mode=True)
        assert fake_llama_cpp["payload"]["response_format"] == {"type": "json_object"}

        backend._llm.raise_exc = MemoryError("oom")
        with pytest.raises(InferenceError) as excinfo:
            await backend.generate(MESSAGES)
        assert excinfo.value.code == E.E_LLM_OOM

        backend._llm.raise_exc = None
        slow = backend._llm.create_chat_completion

        def hang(**payload):
            import time

            time.sleep(0.2)
            return slow(**payload)

        monkeypatch.setattr(backend._llm, "create_chat_completion", hang)
        with pytest.raises(InferenceError) as excinfo2:
            await backend.generate(MESSAGES, timeout_ms=20)
        assert excinfo2.value.code == E.E_LLM_TIMEOUT

    asyncio.run(run())


def test_llama_cpp_missing_dependency_is_unavailable(tmp_path, monkeypatch):
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"w")
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    backend = LlamaCppBackend()
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(backend.load(_spec(backend="llama_cpp", weights_path=str(weights))))
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "llama-cpp-python" in excinfo.value.message


# =====================================================================
# llama-server：HTTP 客户端（本地桩）+ 子进程收尾 + 错误映射
# =====================================================================

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): # pragma: no cover - 静音
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        body = json.dumps({"echo": payload}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def http_stub():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_helpers_against_local_stub(http_stub):
    assert _get_ok(http_stub + "/health") is True
    assert _post_json(http_stub + "/v1/chat/completions", {"a": 1}, 5.0) == {"echo": {"a": 1}}
    assert _get_ok("http://127.0.0.1:1/nope") is False # 连接失败 → False，不抛


def _dummy_proc() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_llama_server_generate_with_fake_http(monkeypatch):
    backend = LlamaServerBackend()
    backend._state = "ready"
    backend._proc = _dummy_proc()
    backend._port = 8080
    backend.model_id = "m"
    captured: dict[str, object] = {}

    def fake_post(url, payload, timeout_s):
        captured["url"] = url
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
                "timings": {"prompt_ms": 1.0, "predicted_ms": 2.0}}

    monkeypatch.setattr(llama_mod, "_post_json", fake_post)
    try:
        result = asyncio.run(backend.generate(MESSAGES, max_tokens=16, seed=5,
                                              grammar='root ::= "{}"',
                                              stop=["END"]))
    finally:
        backend._terminate_proc(backend._proc)
        backend._proc = None

    assert result.text == '{"ok": true}' and result.usage.prompt_tokens == 9
    assert captured["url"].endswith("/v1/chat/completions")
    payload = captured["payload"]
    assert payload["grammar"] == 'root ::= "{}"' # GBNF 走请求体
    assert payload["seed"] == 5 and payload["stop"] == ["END"]


@pytest.mark.parametrize("status,retryable", [(500, True), (503, True), (400, False)])
def test_llama_server_http_error_mapping(monkeypatch, status, retryable):
    backend = LlamaServerBackend()
    backend._state = "ready"
    backend._proc = _dummy_proc()
    backend._port = 8080

    def fake_post(url, payload, timeout_s):
        raise urllib.error.HTTPError(url, status, "err", {}, None)

    monkeypatch.setattr(llama_mod, "_post_json", fake_post)
    try:
        with pytest.raises(InferenceError) as excinfo:
            asyncio.run(backend.generate(MESSAGES))
    finally:
        backend._terminate_proc(backend._proc)
        backend._proc = None
    assert excinfo.value.code == E.E_LLM_BACKEND_ERROR
    assert excinfo.value.retryable is retryable


def test_llama_server_connection_error_is_retryable(monkeypatch):
    backend = LlamaServerBackend()
    backend._state = "ready"
    backend._proc = _dummy_proc()
    backend._port = 8080
    monkeypatch.setattr(llama_mod, "_post_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    try:
        with pytest.raises(InferenceError) as excinfo:
            asyncio.run(backend.generate(MESSAGES))
    finally:
        backend._terminate_proc(backend._proc)
        backend._proc = None
    assert excinfo.value.code == E.E_LLM_BACKEND_ERROR and excinfo.value.retryable is True


def test_llama_server_generate_timeout(monkeypatch):
    backend = LlamaServerBackend()
    backend._state = "ready"
    backend._proc = _dummy_proc()
    backend._port = 8080

    def hang(url, payload, timeout_s):
        import time

        time.sleep(0.3)
        return {}

    monkeypatch.setattr(llama_mod, "_post_json", hang)
    try:
        with pytest.raises(InferenceError) as excinfo:
            asyncio.run(backend.generate(MESSAGES, timeout_ms=20))
    finally:
        backend._terminate_proc(backend._proc)
        backend._proc = None
    assert excinfo.value.code == E.E_LLM_TIMEOUT


def test_llama_server_wait_ready_paths(monkeypatch):
    backend = LlamaServerBackend()
    backend._proc = _dummy_proc()
    backend._port = 8080
    monkeypatch.setattr(llama_mod, "_get_ok", lambda url: True)
    asyncio.run(backend._wait_ready(1_000)) # 立即就绪

    monkeypatch.setattr(llama_mod, "_get_ok", lambda url: False)
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(backend._wait_ready(60))
    assert excinfo.value.code == E.E_LLM_LOAD_FAILED
    backend._terminate_proc(backend._proc)
    backend._proc = None

    # 子进程提前退出
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    backend._proc = dead
    with pytest.raises(InferenceError) as excinfo2:
        asyncio.run(backend._wait_ready(100))
    assert excinfo2.value.code == E.E_LLM_LOAD_FAILED


def test_llama_server_load_without_binary(tmp_path, monkeypatch):
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"w")
    monkeypatch.setattr(llama_mod, "llama_server_available", lambda: False)
    backend = LlamaServerBackend()
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(backend.load(_spec(backend="llama_server", weights_path=str(weights))))
    assert excinfo.value.code == E.E_LLM_LOAD_FAILED
    assert not backend._atexit_registered # 未启动进程就不注册 atexit


def test_llama_server_health_detects_dead_process():
    backend = LlamaServerBackend()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    backend._proc = dead
    backend._state = "ready"
    backend.model_id = "m"
    health = asyncio.run(backend.health())
    assert health.state == "error"
    assert health.last_error_code == str(E.E_LLM_BACKEND_ERROR)
    backend._proc = None


def test_llama_server_unload_terminates_process():
    backend = LlamaServerBackend()
    proc = _dummy_proc()
    backend._proc = proc
    backend._state = "ready"
    asyncio.run(backend.unload())
    assert proc.poll() is not None # 已回收（R-7：不残留子进程）
    assert backend._state == "unloaded"
    asyncio.run(backend.unload()) # 幂等


def test_llama_server_embed_requires_ready():
    backend = LlamaServerBackend()
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(backend.embed(["x"]))
    assert excinfo.value.code == E.E_LLM_UNSUPPORTED


def test_backend_config_knobs_are_read_from_inference_config(tmp_path, fake_llama_cpp):
    """``skip_hash_check`` / ``cache_prompt`` 等来自 harness 配置（PRD §9）。"""
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"w")
    config = replace(InferenceConfig(), skip_hash_check=True, cache_prompt=False)
    backend = LlamaCppBackend(config=config)
    spec = _spec(backend="llama_cpp", weights_path=str(weights), weights_sha256="b" * 64)
    asyncio.run(backend.load(spec)) # skip_hash_check=true → 不做 sha256 校验
    assert backend._state == "ready"
