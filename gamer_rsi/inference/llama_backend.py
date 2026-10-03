"""llama.cpp 两条路线（PRD §6 差异表）。

- ``LlamaCppBackend``：``llama-cpp-python`` **进程内**，GBNF 走
  ``LlamaGrammar.from_string(gbnf)``；``unload`` 置 ``None`` + ``gc``。
- ``LlamaServerBackend``：``llama-server`` **子进程** + ``HTTP``（只监听 ``127.0.0.1``，
  NFR-03-06），GBNF 走请求体 ``grammar`` 字段；``unload`` 发 ``SIGTERM`` 并 ``wait()``
  （R-7：``session()`` + ``atexit`` 双保险，随机空闲端口）。

两者对上层完全等价（同一份 ``DECISION_SCHEMA``、同一组 ``E_LLM_*``）。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Final, Sequence

from ..infra.errors import E
from .backend import (BackendHealth, GenerationResult, InfraFacade, LocalBackend, Message,
                      NullInfra, Usage, estimate_prompt_tokens, make_result)
from .errors import InferenceError
from .registry import ModelSpec
from .weights import skip_hash_check_enabled, verify_weights

__all__ = ["LlamaCppBackend", "LlamaServerBackend", "llama_server_available"]

_LOOPBACK: Final[str] = "127.0.0.1"
_HEALTH_POLL_S: Final[float] = 0.2


def llama_server_available() -> bool:
    """``llama-server`` 是否在 ``PATH`` 上（探针/CLI 用于给出可读提示）。"""
    from shutil import which

    return which("llama-server") is not None


def _prompt_of(messages: Sequence[Message]) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in messages) + "\nassistant:"


def _usage_from(payload: dict[str, Any], latency_ms: int, prompt: str,
                text: str) -> Usage:
    usage = payload.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or max(1, len(prompt) // 3))
    completion_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
    timings = payload.get("timings") or {}
    prefill_ms = int(timings.get("prompt_ms", 0) or 0) or None
    decode_ms = int(timings.get("predicted_ms", 0) or 0) or None
    return Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                 latency_ms=latency_ms, ttft_ms=prefill_ms, prefill_ms=prefill_ms,
                 decode_ms=decode_ms)


# =====================================================================
# 进程内：llama-cpp-python
# =====================================================================

class LlamaCppBackend(LocalBackend):
    """``llama-cpp-python`` 进程内后端（GBNF 稳定，阶段二可选路线）。"""

    name = "llama_cpp"

    def __init__(self, *, infra: InfraFacade | None = None, config: Any = None) -> None:
        self._infra: InfraFacade = infra or NullInfra()
        self._config = config
        self.model_id: str = ""
        self._llm: Any = None
        self._spec: ModelSpec | None = None
        self._state: str = "unloaded"
        self._last_error_code: str | None = None
        self._supports_grammar = True
        self._supports_json_mode = True

    async def load(self, spec: ModelSpec) -> None:
        if self._state == "ready" and self._llm is not None:
            return
        self._spec = spec
        self._state = "loading"
        verify_weights(spec, skip_hash_check=skip_hash_check_enabled(self._config))
        try:
            self._llm = await asyncio.to_thread(self._load_sync, spec)
        except InferenceError as exc:
            self._state, self._last_error_code = "error", str(exc.code)
            raise
        except MemoryError as exc:
            self._state, self._last_error_code = "error", str(E.E_LLM_OOM)
            raise InferenceError(E.E_LLM_OOM, f"llama.cpp 加载 OOM：{spec.model_id}",
                                 retryable=True) from exc
        except Exception as exc:
            self._state, self._last_error_code = "error", str(E.E_LLM_LOAD_FAILED)
            raise InferenceError(E.E_LLM_LOAD_FAILED,
                                 f"llama.cpp 加载失败（{spec.model_id}）：{exc!r}",
                                 retryable=True) from exc
        self.model_id = spec.model_id
        self._state = "ready"
        self._last_error_code = None

    async def unload(self) -> None:
        self._llm = None
        self._spec = None
        self._state = "unloaded"
        import gc

        gc.collect()

    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        if self._state != "ready" or self._llm is None:
            raise InferenceError(E.E_LLM_LOAD_FAILED, "LlamaCppBackend 尚未 load()")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, list(messages), max_tokens, temperature,
                                  top_p, top_k, repetition_penalty, seed, list(stop or []),
                                  grammar, json_mode),
                timeout=max(timeout_ms, 1) / 1000.0)
        except asyncio.TimeoutError as exc:
            raise InferenceError(E.E_LLM_TIMEOUT, f"llama.cpp 生成超时（{timeout_ms}ms）",
                                 retryable=True) from exc
        except MemoryError as exc:
            raise InferenceError(E.E_LLM_OOM, "llama.cpp 生成 OOM", retryable=True) from exc

    async def health(self) -> BackendHealth:
        ready = self._state == "ready"
        return BackendHealth(state=self._state, # type: ignore[arg-type]
                             backend=self.name, model_id=self.model_id or None,
                             resident_gb=(float(self._spec.resident_gb_est)
                                          if ready and self._spec else 0.0),
                             inflight=0, last_error_code=self._last_error_code)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: # pragma: no cover
        if self._llm is None or not hasattr(self._llm, "create_embedding"):
            raise InferenceError(E.E_LLM_UNSUPPORTED, "llama.cpp 后端未启用 embedding")
        out = await asyncio.to_thread(
            self._llm.create_embedding, list(texts)) # type: ignore[attr-defined]
        return [item["embedding"] for item in out.get("data", [])]

    # ---- 内部 -------------------------------------------------------------------
    def _load_sync(self, spec: ModelSpec) -> Any:
        try:
            from llama_cpp import Llama
        except ImportError as exc: # pragma: no cover - 依赖缺失路径
            raise InferenceError(E.E_LLM_UNAVAILABLE, "未安装 llama-cpp-python",
                                 retryable=False,
                                 hint="pip install 'gamer-rsi[llama]' 或 pip install llama-cpp-python"
                                 ) from exc
        return Llama(model_path=spec.weights_path, n_ctx=spec.context_length,
                     n_gpu_layers=-1, logits_all=False, verbose=False)

    def _generate_sync(self, messages: list[Message], max_tokens: int, temperature: float,
                       top_p: float, top_k: int, repetition_penalty: float,
                       seed: int | None, stop: list[str], grammar: str | None,
                       json_mode: bool) -> GenerationResult:
        grammar_obj = None
        if grammar is not None:
            from llama_cpp import LlamaGrammar

            grammar_obj = LlamaGrammar.from_string(grammar)
        payload: dict[str, Any] = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens, "temperature": temperature, "top_p": top_p,
            "top_k": top_k, "repeat_penalty": repetition_penalty,
        }
        if seed is not None:
            payload["seed"] = int(seed)
        if stop:
            payload["stop"] = stop
        if grammar_obj is not None:
            payload["grammar"] = grammar_obj
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        out = self._llm.create_chat_completion(**payload)
        latency_ms = int((time.monotonic() - started) * 1000)
        text = (out.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        prompt = _prompt_of(messages)
        return make_result(text=text, usage=_usage_from(out, latency_ms, prompt, text),
                           backend=self.name, model_id=self.model_id, messages=messages)


# =====================================================================
# 子进程：llama-server + HTTP
# =====================================================================

class LlamaServerBackend(LocalBackend):
    """``llama-server`` 子进程后端（真并发 + GBNF 稳定；阶段二默认）。"""

    name = "llama_server"

    def __init__(self, *, infra: InfraFacade | None = None, config: Any = None) -> None:
        self._infra: InfraFacade = infra or NullInfra()
        self._config = config
        self.model_id: str = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int = 0
        self._spec: ModelSpec | None = None
        self._state: str = "unloaded"
        self._last_error_code: str | None = None
        self._supports_grammar = True
        self._supports_json_mode = True
        self._atexit_registered = False

    # ---- 生命周期 ---------------------------------------------------------------
    async def load(self, spec: ModelSpec) -> None:
        if self._state == "ready" and self._proc is not None and self._proc.poll() is None:
            return
        self._spec = spec
        self._state = "loading"
        verify_weights(spec, skip_hash_check=skip_hash_check_enabled(self._config))
        if not llama_server_available():
            self._state, self._last_error_code = "error", str(E.E_LLM_LOAD_FAILED)
            raise InferenceError(E.E_LLM_LOAD_FAILED, "PATH 上找不到 llama-server",
                                 retryable=True,
                                 hint="brew install llama.cpp 或从源码构建")
        port = int(getattr(self._config, "llama_server_port", 0) or 0) or _free_port()
        args = _server_args(spec, port, spec.context_length)
        try:
            self._proc = subprocess.Popen( # noqa: S603 - 参数全部来自静态注册表
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            self._state, self._last_error_code = "error", str(E.E_LLM_LOAD_FAILED)
            raise InferenceError(E.E_LLM_LOAD_FAILED,
                                 f"llama-server 启动失败（端口 {port} 可能被占用）：{exc!r}",
                                 retryable=True) from exc
        if not self._atexit_registered:
            atexit.register(self._terminate_sync) # R-7：子进程泄漏兜底
            self._atexit_registered = True
        self._port = port
        timeout_ms = int(getattr(self._config, "llama_server_startup_timeout_ms", 60_000))
        try:
            await self._wait_ready(timeout_ms)
        except InferenceError:
            self._terminate_proc(self._proc)
            self._proc = None
            raise
        self.model_id = spec.model_id
        self._state = "ready"
        self._last_error_code = None

    async def unload(self) -> None:
        proc, self._proc = self._proc, None
        self._spec = None
        self._state = "unloaded"
        self._terminate_proc(proc)

    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        if self._state != "ready" or self._proc is None:
            raise InferenceError(E.E_LLM_LOAD_FAILED, "llama-server 尚未 ready")
        payload: dict[str, Any] = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens, "temperature": temperature, "top_p": top_p,
            "top_k": top_k, "repeat_penalty": repetition_penalty,
            "cache_prompt": bool(getattr(self._config, "cache_prompt", True)),
        }
        if seed is not None:
            payload["seed"] = int(seed)
        if stop:
            payload["stop"] = list(stop)
        if grammar is not None:
            payload["grammar"] = grammar
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"http://{_LOOPBACK}:{self._port}/v1/chat/completions"
        started = time.monotonic()
        try:
            out = await asyncio.wait_for(asyncio.to_thread(_post_json, url, payload,
                                                           max(timeout_ms, 1) / 1000.0),
                                         timeout=max(timeout_ms, 1) / 1000.0)
        except asyncio.TimeoutError as exc:
            raise InferenceError(E.E_LLM_TIMEOUT, f"llama-server 请求超时（{timeout_ms}ms）",
                                 retryable=True) from exc
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500
            raise InferenceError(E.E_LLM_BACKEND_ERROR,
                                 f"llama-server HTTP {exc.code}", retryable=retryable,
                                 detail={"status": exc.code}) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InferenceError(E.E_LLM_BACKEND_ERROR, f"llama-server 连接失败：{exc!r}",
                                 retryable=True) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        text = (out.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return make_result(text=text, usage=_usage_from(out, latency_ms, _prompt_of(messages),
                                                        text),
                           backend=self.name, model_id=self.model_id, messages=messages)

    async def health(self) -> BackendHealth:
        state = self._state
        if self._proc is not None and self._proc.poll() is not None and state == "ready":
            state, self._last_error_code = "error", str(E.E_LLM_BACKEND_ERROR)
        ready = state == "ready"
        return BackendHealth(state=state, # type: ignore[arg-type]
                             backend=self.name, model_id=self.model_id or None,
                             resident_gb=(float(self._spec.resident_gb_est)
                                          if ready and self._spec else 0.0),
                             inflight=0, last_error_code=self._last_error_code)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._state != "ready":
            raise InferenceError(E.E_LLM_UNSUPPORTED, "llama-server 未就绪")
        url = f"http://{_LOOPBACK}:{self._port}/v1/embeddings"
        out = await asyncio.to_thread(_post_json, url, {"input": list(texts)}, 60.0)
        return [item["embedding"] for item in out.get("data", [])]

    # ---- 内部 -------------------------------------------------------------------
    async def _wait_ready(self, timeout_ms: int) -> None:
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000.0
        url = f"http://{_LOOPBACK}:{self._port}/health"
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                self._state, self._last_error_code = "error", str(E.E_LLM_LOAD_FAILED)
                raise InferenceError(E.E_LLM_LOAD_FAILED,
                                     f"llama-server 提前退出（exit={self._proc.returncode if self._proc else 'n/a'}）",
                                     retryable=True)
            try:
                if await asyncio.to_thread(_get_ok, url):
                    return
            except Exception: # pragma: no cover - 就绪前连接失败是正常路径
                pass
            await asyncio.sleep(_HEALTH_POLL_S)
        self._state, self._last_error_code = "error", str(E.E_LLM_LOAD_FAILED)
        raise InferenceError(E.E_LLM_LOAD_FAILED,
                             f"llama-server 在 {timeout_ms}ms 内未就绪（端口 {self._port}）",
                             retryable=True)

    def _terminate_sync(self) -> None:
        """``atexit`` 兜底：进程退出时回收子进程（R-7）。"""
        self._terminate_proc(self._proc)
        self._proc = None

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired: # pragma: no cover - 僵死进程
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception: # pragma: no cover - 收尾不得抛
            pass


def _post_json(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response: # noqa: S310 - 仅 loopback
        return json.loads(response.read().decode("utf-8"))


def _get_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response: # noqa: S310 - 仅 loopback
            return 200 <= response.status < 300
    except Exception: # pragma: no cover - 就绪探测失败即未就绪
        return False


def _free_port() -> int:
    """OS 分配的空闲端口（避免 ``--port`` 冲突；PRD §9 ``llama_server_port=0``）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOOPBACK, 0))
        return int(sock.getsockname()[1])


def _server_args(spec: ModelSpec, port: int, context_length: int) -> list[str]:
    """拼装 ``llama-server`` 参数：只监听 127.0.0.1（NFR-03-06），端口由 manager 分配。"""
    extra: list[str] = []
    skip_next = False
    for index, arg in enumerate(spec.server_args):
        if skip_next:
            skip_next = False
            continue
        if arg in ("--port", "--host", "-c", "--ctx-size"):
            skip_next = True
            continue
        extra.append(arg)
    args = ["llama-server", "-m", spec.weights_path, "-c", str(context_length),
            "--host", _LOOPBACK, "--port", str(port)]
    if spec.kv_cache_bits == 4:
        args += ["--cache-type-k", "q4_0", "--cache-type-v", "q4_0"]
    return args + extra
