"""MLX 后端（阶段一默认，PRD §6 差异表 / R-1）。

只用 ``mlx_lm.load`` + 高层 ``stream_generate``；MLX 的 API 变动全部收敛在本文件
（R-1）。**无 GBNF**（``supports_grammar: false``）：结构化输出走 JSON mode + 运行时
``jsonschema`` 校验 + 修复重试，结果记为 ``outcome_kind="degraded"``（PRD §10②）。
阻塞的 MLX 调用统一经 ``asyncio.to_thread`` 执行，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import gc
import time
from pathlib import Path
from typing import Any, Final, Sequence

from ..infra.errors import E
from .backend import (BackendHealth, GenerationResult, InfraFacade, LocalBackend, Message,
                      NullInfra, Usage, estimate_prompt_tokens, make_result)
from .errors import InferenceError
from .registry import ModelSpec
from .weights import skip_hash_check_enabled, verify_weights

__all__ = ["MLXBackend", "JSON_INSTRUCTION"]

#: JSON mode 的附加 system 指令（无 GBNF 时的约束提示；R-2）。
JSON_INSTRUCTION: Final[str] = (
    "你只输出一个 JSON 对象，不要解释，不要 markdown 代码块，不要多余前后缀。"
)

#: JSON 结构的合法首字符（logits 白名单掩码用；R-2 的弱约束兜底）。
_JSON_CHARS: Final[str] = '{}[]",:0123456789-.eEtruefalsn \t\n\\'


class MLXBackend(LocalBackend):
    """``mlx-lm`` 进程内后端（Apple Silicon 阶段一主力）。"""

    name = "mlx"

    def __init__(self, *, infra: InfraFacade | None = None, config: Any = None) -> None:
        self._infra: InfraFacade = infra or NullInfra()
        self._config = config
        self.model_id: str = ""
        self._model: Any = None
        self._tokenizer: Any = None
        self._spec: ModelSpec | None = None
        self._state: str = "unloaded"
        self._last_error_code: str | None = None
        self._supports_grammar = False # MLX 无 GBNF（PRD §6）
        self._supports_json_mode = True
        self._json_mask_active = False

    # ---- 生命周期 ---------------------------------------------------------------
    async def load(self, spec: ModelSpec) -> None:
        """加载权重与 tokenizer；幂等。失败抛 ``E_LLM_LOAD_FAILED`` / ``E_LLM_OOM``。"""
        if self._state == "ready" and self._model is not None:
            return
        if spec.backend != "mlx":
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 f"MLXBackend 只服务 backend=mlx，收到 {spec.backend!r}",
                                 retryable=False)
        self._spec = spec
        self._state = "loading"
        self._verify_weights(spec)
        try:
            self._model, self._tokenizer = await asyncio.to_thread(self._load_sync, spec)
        except InferenceError as exc:
            self._state = "error"
            self._last_error_code = str(exc.code)
            raise
        except MemoryError as exc:
            self._state = "error"
            self._last_error_code = str(E.E_LLM_OOM)
            raise InferenceError(E.E_LLM_OOM, f"MLX 加载触发 Metal OOM：{spec.model_id}",
                                 retryable=True) from exc
        except Exception as exc:
            self._state = "error"
            self._last_error_code = str(E.E_LLM_LOAD_FAILED)
            raise InferenceError(E.E_LLM_LOAD_FAILED,
                                 f"mlx_lm.load 失败（{spec.model_id}）：{exc!r}",
                                 retryable=True,
                                 hint="确认已安装 mlx-lm ≥ 0.20 且权重路径可读") from exc
        self.model_id = spec.model_id
        self._state = "ready"
        self._last_error_code = None

    async def unload(self) -> None:
        """释放引用 + ``mx.clear_cache()``（FR-03-02 / R-4），幂等。"""
        self._model = None
        self._tokenizer = None
        self._spec = None
        self._state = "unloaded"
        gc.collect()
        try:
            import mlx.core as mx

            mx.clear_cache()
            metal = getattr(mx, "metal", None)
            if metal is not None and hasattr(metal, "clear_cache"):
                metal.clear_cache()
        except Exception: # pragma: no cover - mlx 未安装/版本差异：释放引用已足够
            pass

    # ---- 生成 -------------------------------------------------------------------
    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        if self._state != "ready" or self._model is None:
            raise InferenceError(E.E_LLM_LOAD_FAILED, "MLXBackend 尚未 load()",
                                 hint="先 await manager.load(model_id)")
        if grammar is not None:
            # MLX 无 GBNF：显式报错而非静默忽略（Spec P-09）
            raise InferenceError(E.E_LLM_BACKEND_ERROR,
                                 "MLXBackend 不支持 GBNF grammar（supports_grammar=false）",
                                 retryable=False,
                                 hint="注册表应声明 supports_grammar=false，走 JSON mode 降级")
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, list(messages), max_tokens, temperature,
                                  top_p, top_k, repetition_penalty, seed, list(stop or []),
                                  json_mode),
                timeout=max(timeout_ms, 1) / 1000.0)
        except asyncio.TimeoutError as exc:
            raise InferenceError(E.E_LLM_TIMEOUT, f"MLX 生成超时（{timeout_ms}ms）",
                                 retryable=True) from exc
        except MemoryError as exc:
            raise InferenceError(E.E_LLM_OOM, "MLX 生成触发 Metal OOM", retryable=True) from exc
        except InferenceError:
            raise
        except Exception as exc:
            raise InferenceError(E.E_LLM_BACKEND_ERROR, f"mlx_lm 生成失败：{exc!r}",
                                 retryable=True) from exc
        return out

    async def health(self) -> BackendHealth:
        ready = self._state == "ready"
        return BackendHealth(state=self._state, # type: ignore[arg-type]
                             backend=self.name, model_id=self.model_id or None,
                             resident_gb=self._resident_gb() if ready else 0.0, inflight=0,
                             last_error_code=self._last_error_code)

    # ---- 可选辅助接口（B-01）------------------------------------------------------
    def count_tokens(self, messages: Sequence[Message]) -> int:
        """有 tokenizer 用真值，否则退回字符估算（MOD-04 token 预算用）。"""
        if self._tokenizer is None:
            return estimate_prompt_tokens(messages)
        text = self._apply_chat_template(list(messages))
        try:
            return len(self._tokenizer.encode(text))
        except Exception: # pragma: no cover - tokenizer 版本差异
            return estimate_prompt_tokens(messages)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: # pragma: no cover
        raise InferenceError(E.E_LLM_UNSUPPORTED, "MLXBackend 未实现 embed()（B-01）")

    # ---- 内部 -------------------------------------------------------------------
    def _load_sync(self, spec: ModelSpec) -> tuple[Any, Any]:
        try:
            from mlx_lm import load
        except ImportError as exc: # pragma: no cover - 依赖缺失路径
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 "未安装 mlx-lm（阶段一后端依赖）",
                                 hint="pip install 'gamer-rsi[mlx]' 或 pip install mlx-lm>=0.20",
                                 retryable=False) from exc
        path = spec.weights_path
        if not Path(path).exists():
            raise InferenceError(E.E_LLM_UNAVAILABLE, f"权重路径不存在：{path}",
                                 hint="离线下载权重后重试；M1 不联网（NFR-03-06）")
        model, tokenizer = load(path, tokenizer_path=spec.tokenizer_path or path)
        return model, tokenizer

    def _verify_weights(self, spec: ModelSpec) -> None:
        """NFR-03-06：加载前校验 ``sha256``，不匹配抛 ``E_LLM_UNAVAILABLE``。"""
        verify_weights(spec, skip_hash_check=skip_hash_check_enabled(self._config))

    def _generate_sync(self, messages: list[Message], max_tokens: int, temperature: float,
                       top_p: float, top_k: int, repetition_penalty: float,
                       seed: int | None, stop: list[str], json_mode: bool) -> GenerationResult:
        try:
            from mlx_lm import stream_generate
            from mlx_lm.sample_utils import make_sampler
        except ImportError as exc: # pragma: no cover
            raise InferenceError(E.E_LLM_UNAVAILABLE, "mlx-lm 生成入口不可用",
                                 retryable=False) from exc
        prompt = self._apply_chat_template(messages, json_mode=json_mode)
        sampler = make_sampler(temp=temperature, top_p=top_p, top_k=top_k)
        started = time.monotonic()
        ttft_ms: int | None = None
        prefill_ms: int | None = None
        pieces: list[str] = []
        generated = 0
        self._maybe_build_json_mask(json_mode)
        kwargs: dict[str, Any] = {"max_tokens": max_tokens, "sampler": sampler}
        if seed is not None:
            kwargs["seed"] = int(seed)
        for response in stream_generate(self._model, self._tokenizer, prompt=prompt, **kwargs):
            if ttft_ms is None:
                ttft_ms = int((time.monotonic() - started) * 1000)
                prefill_ms = ttft_ms
            text = getattr(response, "text", "") or ""
            pieces.append(text)
            generated += len(getattr(response, "token_ids", []) or [])
            if stop and any("".join(pieces).endswith(s) for s in stop):
                break
        text = "".join(pieces)
        latency_ms = int((time.monotonic() - started) * 1000)
        decode_ms = max(0, latency_ms - (prefill_ms or 0))
        usage = Usage(prompt_tokens=self._count_prompt_tokens(prompt),
                      completion_tokens=generated or max(1, len(text) // 4),
                      latency_ms=latency_ms, ttft_ms=ttft_ms, prefill_ms=prefill_ms,
                      decode_ms=decode_ms)
        return make_result(text=text, usage=usage, backend=self.name,
                           model_id=self.model_id, messages=messages)

    def _apply_chat_template(self, messages: list[Message], *, json_mode: bool = False) -> str:
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        if json_mode and not any(m["content"].startswith("你只输出一个 JSON")
                                 for m in msgs if m["role"] == "system"):
            msgs = [{"role": "system", "content": JSON_INSTRUCTION}] + msgs
        tokenizer = self._tokenizer
        if tokenizer is not None and hasattr(tokenizer, "apply_chat_template"):
            try:
                return tokenizer.apply_chat_template(msgs, tokenize=False,
                                                     add_generation_prompt=True)
            except Exception: # pragma: no cover - 模板缺失时退回朴素拼接
                pass
        return "\n".join(f"{m['role']}: {m['content']}" for m in msgs) + "\nassistant:"

    def _count_prompt_tokens(self, prompt: str) -> int:
        tokenizer = self._tokenizer
        if tokenizer is not None:
            try:
                return len(tokenizer.encode(prompt))
            except Exception: # pragma: no cover
                pass
        return max(1, len(prompt) // 3)

    def _maybe_build_json_mask(self, json_mode: bool) -> None:
        """JSON mode 的 logits 白名单掩码（弱约束，R-2）。

        MLX 无 GBNF；这里只登记可用性供 ``health``/埋点区分降级程度。真正的掩码需要
        ``mlx_lm`` 的 logits processor 接口，版本间不稳定（R-1），故本实现不改写采样，
        由「prompt 指令 + 运行时 jsonschema 校验 + 修复重试」兜住（PRD §10②）。
        """
        self._json_mask_active = False

    def _resident_gb(self) -> float:
        try: # pragma: no cover - 依赖 mlx 运行时
            import mlx.core as mx

            metal = getattr(mx, "metal", None)
            if metal is not None and hasattr(metal, "get_active_memory"):
                return round(metal.get_active_memory() / 1e9, 4)
        except Exception: # pragma: no cover
            pass
        return float(self._spec.resident_gb_est) if self._spec is not None else 0.0
