"""DeepSeek API 后端（Issue #1 §「DeepSeek API 适配要求」）。

形态选择：**新增 ``backend: deepseek``，复用 ``BackendManager`` 的全套语义**（三级 JSON
策略、schema 校验、修复重试、上下文守卫、内存预算、埋点）。适配器类名按 Issue 要求叫
``DeepSeekAPIAdapter``，实现 MOD-03 唯一的 ``Backend`` 契约（5 个方法 + name/model_id），
因此「API 后端」与「本地后端」在调用方眼里完全同形 —— 这是「解耦模型角色」的机制本身。

本文件独占的职责（下游 MOD-04/06/07/08 看不到任何 HTTP 细节）：

- 认证：key 只从 ``spec.api_key_env`` 指向的**环境变量**读，绝不进 harness/、run JSON、
  Git 或 prompt；错误信息里出现 key 会被就地打码。
- HTTP：默认用标准库 ``urllib``（不引入 openai/requests 依赖）；transport 可注入
  （单测与 CI 用假 transport 或 ``api_mode=replay``，完全不触网）。
- 错误映射：401/403 → ``E_LLM_UNAVAILABLE``（不可重试）；400/404/422 → 不可重试；
  408/429/5xx/连接失败 → 可重试（适配器内部做**有限**重试，退避封顶）。
- 可复现性：记 provider/model_id/实际 model_revision/request hash/prompt hash/
  sampling/response hash/usage/成本；同一 request hash 而 response hash 变化时，
  **不覆盖旧证据**，另存变体并记 ``llm.nondeterministic``（``E_LLM_NONDETERMINISTIC``）。
- ``replay``：只从历史响应目录读，缺记录即 fail-closed；replay 结果单独记档，
  不得冒充新的独立实验样本。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from ..infra.errors import E
from .backend import (BackendHealth, GenerationResult, InfraFacade, LocalBackend, Message,
                      NullInfra, Usage, estimate_prompt_tokens, make_result)
from .errors import InferenceError
from .records import prompt_digest
from .registry import ModelSpec
from .secrets import env_file_path, lookup_api_key, permissions_warning

__all__ = [
    "DeepSeekAPIAdapter", "ApiRequest", "ApiResponse", "TransportError",
    "API_JSON_INSTRUCTION", "DEFAULT_API_BASE", "build_request_payload",
    "canonical_request_hash", "make_urllib_transport",
]

#: OpenAI 兼容 base URL 的兜底值（注册表 ``api_base`` 优先）。
DEFAULT_API_BASE: Final[str] = "https://api.deepseek.com/v1"

#: JSON mode 的附加 system 指令（DeepSeek 要求 prompt 里出现 "json" 字样）。
API_JSON_INSTRUCTION: Final[str] = (
    "只输出一个 json 对象：不要解释，不要 markdown 代码块，不要多余前后缀。"
)

#: 传输层有限重试的退避基数（秒）；``Retry-After`` 只作为提示，仍受此上限约束。
_BACKOFF_BASE_S: Final[float] = 0.5
_BACKOFF_CAP_S: Final[float] = 4.0

#: 错误体回显的最大字符数（避免把大段响应塞进事件）。
_ERROR_BODY_CHARS: Final[int] = 240


# =====================================================================
# 传输层契约（可注入；默认 urllib）
# =====================================================================

@dataclass(frozen=True)
class ApiRequest:
    """一次 HTTP 请求（不含 key：认证头由 transport 装配，见 ``make_urllib_transport``）。"""

    url: str
    payload: dict[str, Any]
    timeout_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    api_key: str = field(default="", repr=False) # 只在内存里传递，绝不落盘

    def with_key_redacted(self) -> dict[str, Any]:
        """可落盘的请求视图（**没有** key）。"""
        return {"url": self.url, "payload": self.payload,
                "timeout_ms": int(self.timeout_ms),
                "headers": {k: v for k, v in self.headers.items()
                            if k.lower() != "authorization"}}


@dataclass(frozen=True)
class ApiResponse:
    """一次 HTTP 响应（状态码 + 文本体；非 2xx 也走这里，便于统一映射）。"""

    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


class TransportError(Exception):
    """传输层失败（连接/超时）；由适配器映射为 ``E_LLM_*``。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind # "connect" | "timeout"


def make_urllib_transport() -> Callable[[ApiRequest], ApiResponse]:
    """标准库 transport（同步阻塞；调用方经 ``asyncio.to_thread`` 执行）。"""

    def transport(request: ApiRequest) -> ApiResponse:
        headers = {"Content-Type": "application/json", **request.headers}
        if request.api_key:
            headers["Authorization"] = f"Bearer {request.api_key}"
        data = json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(request.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(request.timeout_ms, 1) / 1000.0) as resp:
                return ApiResponse(status=int(resp.status),
                                   text=resp.read().decode("utf-8", errors="replace"),
                                   headers=dict(resp.headers or {}))
        except urllib.error.HTTPError as exc: # 非 2xx：把状态码交回上层统一映射
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            return ApiResponse(status=int(exc.code), text=body,
                               headers=dict(getattr(exc, "headers", {}) or {}))
        except TimeoutError as exc:
            raise TransportError("timeout", f"连接/读取超时：{exc}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise TransportError("timeout", f"连接/读取超时：{reason}") from exc
            raise TransportError("connect", f"连接失败：{reason}") from exc

    return transport


# =====================================================================
# 请求构造与哈希
# =====================================================================

def build_request_payload(spec: ModelSpec, messages: Sequence[Message], *,
                          max_tokens: int, sampling: Mapping[str, Any],
                          json_mode: bool,
                          stop: Sequence[str] | None = None) -> dict[str, Any]:
    """构造 OpenAI 兼容的 chat/completions 请求体（不含任何认证信息）。

    - 只发送 DeepSeek 真正接受的采样键（``temperature`` / ``top_p``）；``top_k`` 与
      ``repetition_penalty`` 不接受但**照记进审计**（审计要看得见请求口径）。
    - ``seed`` 仅在注册表声明 ``api_supports_seed`` 时发送：API 不承诺跨时间严格确定性，
      所以默认不发、也不假装发了（Issue #1「可复现性」）。
    - ``json_mode`` 且注册表支持时加 ``response_format={"type": "json_object"}``，
      并把含 "json" 字样的指令并入 system 消息（服务方的硬要求：prompt 里必须出现 json）。
    """
    msgs: list[dict[str, str]] = [{"role": m.role, "content": m.content} for m in messages]
    if json_mode and spec.supports_json_mode:
        if msgs and msgs[0]["role"] == "system":
            msgs[0] = {"role": "system",
                       "content": f"{msgs[0]['content']}\n{API_JSON_INSTRUCTION}"}
        else:
            msgs.insert(0, {"role": "system", "content": API_JSON_INSTRUCTION})
    payload: dict[str, Any] = {
        "model": spec.api_model or spec.model_id,
        "messages": msgs,
        "max_tokens": int(max_tokens),
        "stream": False,
        "temperature": float(sampling.get("temperature", 1.0)),
        "top_p": float(sampling.get("top_p", 0.95)),
    }
    if sampling.get("seed") is not None and spec.api_supports_seed:
        payload["seed"] = int(sampling["seed"])
    if json_mode and spec.supports_json_mode:
        payload["response_format"] = {"type": "json_object"}
    if stop:
        payload["stop"] = [str(s) for s in stop][:16] # OpenAI 兼容上限 16 条
    return payload


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    """请求体的规范哈希（排序 + 紧凑 + 保非 ASCII）；同口径请求必得同一 hash。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _response_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# =====================================================================
# 适配器
# =====================================================================

class DeepSeekAPIAdapter(LocalBackend):
    """``backend: deepseek`` 的 API 后端（Issue #1 验收标准 B）。

    构造参数全部可注入：``transport``（假传输）、``sleep``（假退避）、``api_key``
    （测试直给，生产走环境变量）、``infra``（事件与 run_id）。因此单测与 CI 完全不触网。
    """

    name = "deepseek"
    _supports_grammar = False # 无 GBNF：走 json_object + jsonschema 校验（与 MLX 同口径）
    _mode = "train"

    def __init__(self, *, infra: InfraFacade | None = None, config: Any = None,
                 transport: Callable[[ApiRequest], ApiResponse] | None = None,
                 sleep: Callable[[float], Any] | None = None,
                 api_key: str | None = None,
                 api_key_file: Path | str | None = None,
                 replay_dir: Path | str | None = None,
                 runs_root: Path | str | None = None) -> None:
        self._infra: InfraFacade = infra or NullInfra()
        self._config = config
        self._transport = transport
        self._sleep = sleep or asyncio.sleep
        self._api_key_override = api_key
        #: 显式传入优先；``None`` 时按 ``inference.api_key_file`` 解析（默认仓库根 .env）
        self._api_key_file = Path(api_key_file) if api_key_file is not None else None
        self._replay_dir = Path(replay_dir) if replay_dir else None
        self._runs_root = Path(runs_root) if runs_root else None
        self.model_id: str = ""
        self._spec: ModelSpec | None = None
        self._api_key: str = ""
        self._state: str = "unloaded"
        self._last_error_code: str | None = None
        self._supports_json_mode = True
        self._api_mode: str = "live"
        #: key 的来源（environment / env_file / explicit）——只记来源，不记 key 本身
        self._api_key_source: str = "missing"

    # ---- 生命周期 ---------------------------------------------------------------
    async def load(self, spec: ModelSpec) -> None:
        if spec.backend != "deepseek":
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 f"DeepSeekAPIAdapter 只服务 backend=deepseek，收到 {spec.backend!r}",
                                 retryable=False)
        self._spec = spec
        self._api_mode = str(getattr(self._config, "api_mode", "live") or "live")
        self._supports_json_mode = bool(spec.supports_json_mode)
        if self._api_mode == "replay":
            directory = self._replay_dir
            if directory is None:
                raw = str(getattr(self._config, "api_replay_dir", "") or "")
                directory = Path(raw) if raw else None
            if directory is None or not directory.is_dir():
                raise InferenceError(
                    E.E_LLM_UNAVAILABLE,
                    f"api_mode=replay 需要存在的历史响应目录（{directory}）",
                    hint="设置 inference.api_replay_dir 指向含 <request_hash>.json 的目录",
                    retryable=False)
            self._replay_dir = directory
            self._api_key = ""
        else:
            key = self._api_key_override
            if key is not None:
                self._api_key_source = "explicit"
            else:
                env_name = spec.api_key_env
                if not env_name:
                    raise InferenceError(
                        E.E_LLM_UNAVAILABLE,
                        f"模型 {spec.model_id!r} 未登记 api_key_env（provider=api）",
                        hint="在 configs/models.yaml 该模型下补 api_key_env，例如 DEEPSEEK_API_KEY",
                        retryable=False)
                file_path = (self._api_key_file if self._api_key_file is not None
                             else env_file_path(self._config))
                ref = lookup_api_key(env_name, env_file=file_path)
                key, self._api_key_source = ref.value, ref.source
                if ref.present:
                    self._warn_loose_permissions(ref)
            if not key:
                file_hint = (f"；也可以把 key 写进本地密钥文件 {self._api_key_file or env_file_path(self._config)}"
                             f"（已在 .gitignore 内，cp .env.example .env 后 chmod 600）")
                raise InferenceError(
                    E.E_LLM_UNAVAILABLE,
                    f"缺少 API key（环境变量 {spec.api_key_env or '<未登记>'} 为空，"
                    f"本地密钥文件也没有该名字）",
                    hint=f"export {spec.api_key_env or 'DEEPSEEK_API_KEY'}=<key>{file_hint}；"
                         "key 只从环境变量或该本地文件读，绝不写入 harness/、run JSON 或 Git",
                    retryable=False)
            self._api_key = key
        self.model_id = spec.model_id
        self._state = "ready"
        self._last_error_code = None

    async def unload(self) -> None:
        self._api_key = ""
        self._api_key_source = "missing"
        self._spec = None
        self._state = "unloaded"

    # ---- 生成 -------------------------------------------------------------------
    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        spec = self._spec
        if self._state != "ready" or spec is None:
            raise InferenceError(E.E_LLM_LOAD_FAILED, "DeepSeekAPIAdapter 尚未 load()",
                                 hint="先 await manager.load(model_id)")
        if grammar is not None:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                "DeepSeekAPIAdapter 不支持 GBNF grammar（supports_grammar=false）",
                retryable=False,
                hint="注册表应声明 supports_grammar=false，走 response_format=json_object 降级")
        sampling = {"temperature": float(temperature), "top_p": float(top_p),
                    "top_k": int(top_k), "repetition_penalty": float(repetition_penalty),
                    "seed": (None if seed is None else int(seed))}
        payload = build_request_payload(spec, messages, max_tokens=max_tokens,
                                        sampling=sampling, json_mode=bool(json_mode),
                                        stop=stop)
        request_hash = canonical_request_hash(payload)
        self._last_attempts = 1
        started = time.perf_counter()
        if self._api_mode == "replay":
            response, replay = self._replay_response(request_hash), True
        else:
            response, replay = await self._post(payload, timeout_ms=int(timeout_ms)), False
        parsed = self._parse_response(response.text)
        latency_ms = int(round((time.perf_counter() - started) * 1000.0))
        usage = self._usage_of(parsed["usage"], messages, latency_ms=latency_ms)
        audit = {
            "schema_version": 1,
            "provider": "api",
            "backend": self.name,
            "model_id": spec.model_id,
            "api_model": spec.api_model or spec.model_id,
            "model_revision": parsed["model_revision"] or spec.model_revision,
            "online": True,
            "replay": bool(replay),
            "mode": str(getattr(self._config, "mode", self._mode)),
            "request_hash": request_hash,
            "prompt_hash": prompt_digest(list(messages)),
            "response_hash": _response_hash(response.text),
            "sampling": sampling,
            "max_tokens": int(max_tokens),
            "json_mode": bool(json_mode),
            "usage": {"prompt_tokens": usage.prompt_tokens,
                      "completion_tokens": usage.completion_tokens},
            "cost_usd": spec.cost_usd(prompt_tokens=usage.prompt_tokens,
                                      completion_tokens=usage.completion_tokens),
            "latency_ms": latency_ms,
            "attempts": int(parsed["attempts"]),
            "status": int(response.status),
            "finish_reason": parsed["finish_reason"],
            "api_key_env": spec.api_key_env,
            "api_key_source": self._api_key_source,
        }
        self._record(audit, request_hash=request_hash, response_hash=audit["response_hash"],
                     replay=replay)
        self._infra.emit("llm.api.call", **{k: v for k, v in audit.items()
                                           if k not in ("sampling", "usage")},
                         temperature=sampling["temperature"], top_p=sampling["top_p"])
        result = make_result(text=parsed["text"], usage=usage, backend="deepseek",
                             model_id=spec.model_id, messages=messages,
                             finish_reason=parsed["finish_reason"],
                             outcome_kind="action")
        if getattr(self._config, "debug_dump_raw", False):
            self._infra.dump_raw(f"api-{request_hash[-12:]}", parsed["text"])
        return result

    async def health(self) -> BackendHealth:
        ready = self._state == "ready"
        return BackendHealth(state=self._state, # type: ignore[arg-type]
                             backend=self.name, model_id=self.model_id or None,
                             resident_gb=0.0, inflight=0,
                             last_error_code=self._last_error_code)

    def count_tokens(self, messages: Sequence[Message]) -> int:
        """无本地 tokenizer（API 侧计费）：用与 MOD-04 对齐的字符估算。"""
        return estimate_prompt_tokens(messages)

    # ---- HTTP / 重试 ---------------------------------------------------------------
    async def _post(self, payload: Mapping[str, Any], *, timeout_ms: int) -> ApiResponse:
        spec = self._spec
        assert spec is not None # load() 已保证
        base = (spec.api_base or DEFAULT_API_BASE).rstrip("/")
        request = ApiRequest(url=f"{base}/chat/completions", payload=dict(payload),
                            timeout_ms=self._per_call_timeout_ms(timeout_ms),
                            headers={"Accept": "application/json"},
                            api_key=self._api_key)
        transport = self._transport or make_urllib_transport()
        retries = max(0, int(getattr(self._config, "api_max_retries", 2)))
        deadline = time.monotonic() + max(timeout_ms, 1) / 1000.0
        last: InferenceError | None = None
        for attempt in range(1, retries + 2):
            try:
                response = await asyncio.to_thread(transport, request)
            except TransportError as exc:
                last = self._transport_error(exc)
            except InferenceError:
                raise
            except Exception as exc: # noqa: BLE001 - 传输实现的意外异常收敛为后端错误
                last = InferenceError(E.E_LLM_BACKEND_ERROR,
                                      f"API 传输异常：{self._redact(repr(exc))}",
                                      retryable=True, attempts=attempt)
            else:
                mapped = self._status_error(response, attempts=attempt)
                if mapped is None:
                    if attempt > 1:
                        # 重试成功的次数要如实记进埋点（parsed.attempts 取它）
                        self._last_attempts = attempt
                    return response
                last = mapped
            if last is None or not last.retryable or attempt > retries:
                self._last_error_code = str(last.code) if last is not None else None
                raise last if last is not None else InferenceError(
                    E.E_LLM_BACKEND_ERROR, "API 调用失败且无错误详情", retryable=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._last_error_code = str(E.E_LLM_TIMEOUT)
                raise InferenceError(
                    E.E_LLM_TIMEOUT,
                    f"API 重试预算耗尽（total_timeout 内仅完成 {attempt} 次尝试）",
                    retryable=True, attempts=attempt,
                    detail={"cause": str(last.code)}) from last
            await self._sleep(min(self._backoff_s(attempt), max(remaining, 0.0)))
        # 循环必然在内部 return/raise；这里的兜底只为让类型检查收敛
        raise last if last is not None else InferenceError(  # pragma: no cover
            E.E_LLM_BACKEND_ERROR, "API 调用未产生结果", retryable=False)

    def _per_call_timeout_ms(self, timeout_ms: int) -> int:
        configured = int(getattr(self._config, "api_connect_timeout_ms", 10_000))
        return max(1, min(int(timeout_ms), configured))

    def _backoff_s(self, attempt: int) -> float:
        return min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _BACKOFF_CAP_S)

    def _transport_error(self, exc: TransportError) -> InferenceError:
        if exc.kind == "timeout":
            return InferenceError(E.E_LLM_TIMEOUT, f"API 超时：{exc}", retryable=True)
        return InferenceError(E.E_LLM_UNAVAILABLE, f"API 连接失败：{exc}",
                              retryable=True,
                              hint="检查网络与 api_base；本地主线不受影响（Qwen 可离线跑）")

    def _status_error(self, response: ApiResponse, *, attempts: int) -> InferenceError | None:
        """非 2xx → 映射为 InferenceError；2xx 返回 None。"""
        spec = self._spec
        where_file = (self._api_key_file if self._api_key_file is not None
                      else env_file_path(self._config))
        status = int(response.status)
        if 200 <= status < 300:
            return None
        snippet = self._redact(response.text.strip())[:_ERROR_BODY_CHARS]
        retry_after = next((v for k, v in response.headers.items()
                            if k.lower() == "retry-after"), None)
        detail = {"status": status, "body": snippet, "retry_after": retry_after}
        if status in (401, 403):
            # 真跑时的第一现场：把「key 从哪来、该改哪」直接说清楚（key 本身仍然打码）。
            source = {"environment": "环境变量", "env_file": "本地密钥文件",
                      "explicit": "显式注入"}.get(self._api_key_source, "未知来源")
            where = (f"{spec.api_key_env or 'DEEPSEEK_API_KEY'}（{source}"
                     f"{'：' + str(where_file) if where_file else ''}）")
            return InferenceError(E.E_LLM_UNAVAILABLE,
                                  f"API 认证/权限失败（HTTP {status}）：{snippet}",
                                  retryable=False, attempts=attempts, detail=detail,
                                  hint=f"检查 {where} 是否有效、是否已开通该模型")
        if status == 404:
            return InferenceError(E.E_LLM_UNAVAILABLE,
                                  f"API 端点/模型不存在（HTTP 404）：{snippet}",
                                  retryable=False, attempts=attempts, detail=detail,
                                  hint="检查注册表里的 api_base 与 api_model")
        if status == 408 or status == 429 or 500 <= status < 600:
            return InferenceError(E.E_LLM_BACKEND_ERROR,
                                  f"API 暂时不可用（HTTP {status}）：{snippet}",
                                  retryable=True, attempts=attempts, detail=detail)
        return InferenceError(E.E_LLM_BACKEND_ERROR,
                              f"API 拒绝请求（HTTP {status}）：{snippet}",
                              retryable=False, attempts=attempts, detail=detail)

    def _warn_loose_permissions(self, ref: Any) -> None:
        """本地密钥文件对同组/其他用户可读 → 记一条告警事件（不阻断，Spec §8.5）。"""
        if ref.source != "env_file":
            return
        message = permissions_warning(ref.env_file)
        if message:
            self._infra.emit("llm.api.key_file_permissions", code=str(E.E_IO_CONFIG_UNKNOWN),
                             path=str(ref.env_file), reason=message)

    def _redact(self, text: str) -> str:
        """把 key 就地打码（错误体/异常串可能回显请求头）。"""
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "***")
        return text

    # ---- 响应解析 -----------------------------------------------------------------
    def _parse_response(self, text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                f"API 响应不是合法 JSON：{self._redact(text)[:120]!r}（{exc}）",
                retryable=True) from exc
        if not isinstance(data, Mapping):
            raise InferenceError(E.E_LLM_BACKEND_ERROR, "API 响应顶层不是对象",
                                 retryable=True)
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            error = data.get("error") or {}
            message = str(error.get("message") if isinstance(error, Mapping) else error)
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                f"API 响应没有 choices：{self._redact(message)[:120]!r}",
                retryable=True)
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message") or {}
        content = message.get("content") if isinstance(message, Mapping) else None
        if content is None:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                "API 响应的 message.content 为空（reasoner 类模型可能只回 reasoning_content）",
                retryable=True)
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        return {
            "text": str(content),
            "usage": dict(usage or {}),
            "model_revision": str(data.get("model") or ""),
            "finish_reason": {"length": "length"}.get(str(first.get("finish_reason")), "stop"),
            "attempts": int(getattr(self, "_last_attempts", 1)),
        }

    def _usage_of(self, usage: Mapping[str, Any], messages: Sequence[Message],
                  *, latency_ms: int) -> Usage:
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        prompt = int(prompt_tokens) if isinstance(prompt_tokens, int) else \
            estimate_prompt_tokens(messages)
        completion = int(completion_tokens) if isinstance(completion_tokens, int) else 0
        return Usage(prompt_tokens=prompt, completion_tokens=completion, latency_ms=latency_ms)

    # ---- replay / 审计落盘 ---------------------------------------------------------
    def _replay_response(self, request_hash: str) -> ApiResponse:
        assert self._replay_dir is not None # load() 已保证
        path = self._replay_dir / f"{request_hash.replace(':', '_')}.json"
        if not path.is_file():
            raise InferenceError(
                E.E_LLM_UNAVAILABLE,
                f"replay 缺少请求 {request_hash} 的历史响应（{path.name}）",
                retryable=False,
                hint="replay 只能复现已录制的请求；新请求请用 api_mode=live"
                     "（replay 结果不得冒充新的独立实验样本）")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 f"replay 记录不可读：{path.name}（{exc}）",
                                 retryable=False) from exc
        raw = record.get("response") if isinstance(record, Mapping) else None
        text = json.dumps(raw, ensure_ascii=False) if raw is not None else \
            json.dumps(record, ensure_ascii=False)
        return ApiResponse(status=200, text=text)

    def _record(self, audit: Mapping[str, Any], *, request_hash: str, response_hash: str,
                replay: bool) -> None:
        if not bool(getattr(self._config, "api_record_enabled", True)):
            return
        directory = self._record_dir(replay=replay)
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        payload = dict(audit)
        base = directory / f"{request_hash.replace(':', '_')}.json"
        try:
            if not base.exists():
                base.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                return
            previous = json.loads(base.read_text(encoding="utf-8"))
            if str(previous.get("response_hash")) == response_hash:
                return # 同口径同响应：幂等，不重复写
            # API 不承诺跨时间严格确定性：旧证据保留，另存变体（序号确定、不用墙钟），
            # 并显式标记。
            seq = len(list(directory.glob(f"{request_hash.replace(':', '_')}.*.json"))) + 2
            variant = directory / f"{request_hash.replace(':', '_')}.v{seq}.json"
            variant.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            self._infra.emit("llm.nondeterministic",
                             code=str(E.E_LLM_NONDETERMINISTIC),
                             request_hash=request_hash,
                             previous_response_hash=str(previous.get("response_hash")),
                             response_hash=response_hash,
                             model_id=str(audit.get("model_id")),
                             variant_path=str(variant))
        except OSError as exc: # 审计落盘失败不阻断推理，但必须留事件
            self._infra.emit("llm.api.record_failed", code=str(E.E_IO_WRITE),
                             path=str(base), reason=str(exc)[:200])

    def _record_dir(self, *, replay: bool) -> Path | None:
        run_id = self._infra.run_id()
        if not run_id:
            return None
        root = self._runs_root if self._runs_root is not None else Path("runs")
        name = str(getattr(self._config, "api_record_dir", "llm_api") or "llm_api")
        return root / run_id / (f"{name}_replay" if replay else name)
