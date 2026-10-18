"""MOD-03 后端契约、数据模型、``MockBackend`` 与 ``BackendManager``（PRD §6/§8）。

模块内分层：

- **数据模型**：``Message`` / ``Usage`` / ``BackendHealth`` / ``GenerationResult``；
  ``ModelSpec`` / ``BackendName`` 定义在 ``registry.py``（注册表类型化），此处 re-export，
  避免 ``registry ↔ backend`` 循环 import。
- **契约**：``Backend`` Protocol（5 个方法 + ``name``/``model_id``，NFR-03-07）。
- **基础设施**：``InfraFacade`` 协议 + ``NullInfra`` / ``InfraBridge``（松耦合，PRD §14）。
- **门面**：``BackendManager``（single-flight 加载、并发限流、内存预算前置、三级 JSON 重试）。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (Any, AsyncIterator, Callable, Final, Literal, Mapping, Protocol,
                    Sequence, runtime_checkable)

from ..infra.errors import E
from .errors import InferenceError
from .records import CallContext, InferenceCallRecord, make_call_record, prompt_digest
from .registry import (BackendName, DEFAULT_REGISTRY_PATH, ModelRegistry, ModelSpec,
                       estimate_required_gb, load_registry)
from .schema import compile_grammar, repair_messages, retry_reason, validate

__all__ = [
    "Message", "Usage", "BackendHealth", "GenerationResult", "Backend", "LocalBackend",
    "MockBackend", "BackendManager", "InfraFacade", "NullInfra", "InfraBridge",
    "ModelSpec", "ModelRegistry", "BackendName", "OutcomeKind", "validate_sampling",
    "estimate_prompt_tokens", "prompt_digest", "make_result", "execute_json_call",
    "SAMPLING_KEYS", "CHARS_PER_TOKEN", "DECISION_MOCK_TEXT",
]

OutcomeKind = Literal["action", "degraded", "error"]
FinishReason = Literal["stop", "length", "timeout", "error"]

#: token 估算的字符比（与 MOD-04 ``policy.token_estimator=chars_div_3`` 对齐）。
CHARS_PER_TOKEN: Final[float] = 3.0

#: 采样白名单（FR-03-07）。
SAMPLING_KEYS: Final[tuple[str, ...]] = (
    "temperature", "top_p", "top_k", "repetition_penalty", "seed",
)

#: 采样白名单区间；``top_p`` 下界为开区间（PRD §6）。
SAMPLING_RANGES: Final[Mapping[str, tuple[float, float]]] = {
    "temperature": (0.0, 1.5),
    "top_p": (0.0, 1.0),
    "top_k": (0, 200),
    "repetition_penalty": (1.0, 1.5),
}

DEFAULT_SAMPLING: Final[Mapping[str, Any]] = {
    "temperature": 1.0, "top_p": 0.95, "top_k": 0, "repetition_penalty": 1.0, "seed": None,
}

#: ``mock`` 注册表项的默认输出（合法 DECISION_SCHEMA，可离线冒烟，G6）。
DECISION_MOCK_TEXT: Final[str] = '{"reasoning": "mock", "plan": [{"kind": "end_turn"}]}'

#: 预算比较容差（浮点边界：20.0 与 20.0 不应判超）。
_BUDGET_EPS: Final[float] = 1e-9


# =====================================================================
# 数据模型（PRD §6）
# =====================================================================

@dataclass(frozen=True)
class Message:
    """一轮对话消息（``role`` 逐字取自 Spec §4.5 的三值）。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    """token 与延迟埋点（FR-03-03：前四个字段必须有）。

    2026-10-17（缓存修复）：新增**前缀缓存**指标。服务方（DeepSeek）在
    ``usage.prompt_cache_hit_tokens`` / ``prompt_tokens_details.cached_tokens`` 里返回
    命中的输入 token；本地后端没有前缀缓存，``cache_hit_tokens`` 保持 0
    （``cache_miss_tokens`` 由属性按 ``prompt_tokens`` 派生）。
    """

    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    ttft_ms: int | None = None
    prefill_ms: int | None = None
    decode_ms: int | None = None
    #: 命中服务方前缀缓存的输入 token（0 = 未命中/后端不支持）
    cache_hit_tokens: int = 0

    @property
    def cache_miss_tokens(self) -> int:
        """未命中的输入 token（= ``prompt_tokens - cache_hit_tokens``）。"""
        return max(0, self.prompt_tokens - self.cache_hit_tokens)

    @property
    def cache_hit_rate(self) -> float:
        """``cache_hit_tokens / prompt_tokens``（0..1；prompt 为 0 时返回 0.0）。"""
        if self.prompt_tokens <= 0:
            return 0.0
        return round(self.cache_hit_tokens / self.prompt_tokens, 6)


@dataclass(frozen=True)
class BackendHealth:
    """无副作用探针结果（FR-03-02：``unload`` 后 ``state == "unloaded"``）。"""

    state: Literal["unloaded", "loading", "ready", "degraded", "error"]
    backend: BackendName
    model_id: str | None
    resident_gb: float | None
    inflight: int
    last_error_code: str | None


@dataclass(frozen=True)
class GenerationResult:
    """一次生成调用的结果（PRD §6 字段 + 3 个 FR 必需的扩展字段）。"""

    text: str
    parsed: dict[str, Any] | None
    usage: Usage
    attempts: int # 真实尝试次数 ∈ 1..max_attempts（FR-03-04）
    retry_reasons: list[str] # ["ValidationError:$.plan[0].kind"]
    finish_reason: FinishReason
    outcome_kind: OutcomeKind
    model_id: str
    backend: BackendName
    prompt_digest: str # "sha256:<hex>"
    raw_texts: list[str] = field(default_factory=list)
    # 扩展字段（PRD 正文要求但接口清单未列；均为只读附加信息）
    truncated: bool = False # FR-03-10：policy="truncate" 且发生截断
    degraded_reason: str | None = None # §10②：degraded 的原因
    calls: tuple[InferenceCallRecord, ...] = () # FR-03-06：每次尝试一条埋点

    @property
    def ok(self) -> bool:
        return self.outcome_kind in ("action", "degraded")


# =====================================================================
# 后端契约（NFR-03-07）
# =====================================================================

@runtime_checkable
class Backend(Protocol):
    """MOD-03 唯一后端契约。实现：MLX / LlamaCpp / LlamaServer / Mock 四者。

    新增后端只需实现 5 个方法 + ``name`` / ``model_id`` 属性；同一套一致性测试套件
    覆盖全部实现（``tests/unit/inference/test_backend_protocol.py``）。
    """

    name: BackendName
    model_id: str

    async def load(self, spec: ModelSpec) -> None:
        """加载权重与 tokenizer；幂等。失败抛 ``E_LLM_LOAD_FAILED`` / ``E_LLM_OOM``。"""

    async def unload(self) -> None:
        """释放权重、KV 缓存、子进程；幂等。"""

    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        """自由文本生成；每次尝试均记埋点。grammar/json_mode 由 generate_json 注入。"""

    async def generate_json(self, messages: Sequence[Message], schema: Mapping[str, Any], *,
                            max_attempts: int = 3, retry_backoff_ms: int = 200,
                            total_timeout_ms: int = 8_000, **sampling: Any) -> GenerationResult:
        """约束解码 → schema 校验 → 修复重试。全部失败抛 ``E_LLM_SCHEMA_INVALID``。"""

    async def health(self) -> BackendHealth:
        """无副作用探针（不触发推理）。"""


# =====================================================================
# 基础设施门面（PRD §14：经 InfraFacade 松耦合，测试注入 NullInfra）
# =====================================================================

@runtime_checkable
class InfraFacade(Protocol):
    """MOD-03 需要的 MOD-09 能力子集（事件、时钟、原文倾倒）。"""

    def emit(self, event: str, **fields: Any) -> None: ...

    def now_ms(self) -> float: ...

    def run_id(self) -> str | None: ...

    def dump_raw(self, call_id: str, text: str) -> str | None: ...


class NullInfra:
    """零依赖实现（单测与非 run 场景）：丢弃事件、用单调钟计时。"""

    def emit(self, event: str, **fields: Any) -> None:
        return None

    def now_ms(self) -> float:
        return time.monotonic() * 1000.0

    def run_id(self) -> str | None:
        return None

    def dump_raw(self, call_id: str, text: str) -> str | None:
        return None


class InfraBridge:
    """默认实现：事件走 ``infra.events``；``debug_dump_raw`` 落 ``runs/<run_id>/raw/llm/``。"""

    def __init__(self, *, run_id: str | None = None, runs_root: Path | str = "runs",
                 debug_dump_raw: bool = False) -> None:
        self._run_id = run_id
        self._runs_root = Path(runs_root)
        self._debug_dump_raw = bool(debug_dump_raw)

    def emit(self, event: str, **fields: Any) -> None:
        from ..infra import events # 延迟 import：不把 infra 拖进纯单测路径

        try:
            events.emit(event, **fields)
        except Exception: # pragma: no cover - 埋点失败不得拖垮推理
            return None

    def now_ms(self) -> float:
        return time.monotonic() * 1000.0

    def run_id(self) -> str | None:
        return self._run_id

    def dump_raw(self, call_id: str, text: str) -> str | None:
        if not self._debug_dump_raw or self._run_id is None:
            return None
        path = self._runs_root / self._run_id / "raw" / "llm" / f"{call_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)


# =====================================================================
# 采样与上下文工具
# =====================================================================

def _sampling_out_of_range(key: str, value: Any, low: float, high: float,
                           *, exclusive: bool = False) -> InferenceError:
    left = "(" if exclusive else "["
    return InferenceError(
        E.E_LLM_BACKEND_ERROR,
        f"采样参数 {key}={value!r} 越界（合法区间 {left}{low}, {high}]）",
        retryable=False, detail={"param": key, "value": value, "low": low, "high": high})


def validate_sampling(sampling: Mapping[str, Any], *, mode: str = "train") -> dict[str, Any]:
    """采样白名单校验（FR-03-07），越界抛 ``E_LLM_BACKEND_ERROR``（绝不静默裁剪）。

    ``mode="eval"`` 额外要求 ``temperature == 0.0`` 且 ``seed is not None``（B-14），并把
    ``top_p``/``top_k`` 归一为评估口径 ``1.0`` / ``0``（NFR-03-01，harness 不可覆盖）。
    """
    out: dict[str, Any] = dict(DEFAULT_SAMPLING)
    for key in SAMPLING_KEYS:
        if key in sampling and sampling[key] is not None:
            out[key] = sampling[key]
    for key, (low, high) in SAMPLING_RANGES.items():
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InferenceError(E.E_LLM_BACKEND_ERROR,
                                 f"采样参数 {key}={value!r} 类型非法", retryable=False,
                                 detail={"param": key, "value": value})
        if key == "top_k" and not isinstance(value, int):
            raise InferenceError(E.E_LLM_BACKEND_ERROR,
                                 f"采样参数 top_k={value!r} 必须是整数", retryable=False,
                                 detail={"param": key, "value": value})
        if key == "top_p":
            if not (low < float(value) <= high):
                raise _sampling_out_of_range(key, value, low, high, exclusive=True)
        elif not (low <= float(value) <= high):
            raise _sampling_out_of_range(key, value, low, high)
    out["temperature"] = float(out["temperature"])
    out["top_p"] = float(out["top_p"])
    out["top_k"] = int(out["top_k"])
    out["repetition_penalty"] = float(out["repetition_penalty"])
    if out["seed"] is not None:
        out["seed"] = int(out["seed"])
    if mode == "eval":
        if out["temperature"] != 0.0:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                f"mode=eval 要求 temperature=0.0，收到 {out['temperature']!r}（B-14，不可覆盖）",
                retryable=False, detail={"param": "temperature", "mode": "eval"})
        if out["seed"] is None:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                "mode=eval 要求显式 seed（INV-6/B-14）；请由 MOD-09 seed 注册表传入",
                retryable=False, detail={"param": "seed", "mode": "eval"})
        out["top_p"] = 1.0
        out["top_k"] = 0
    return out


def estimate_prompt_tokens(messages: Sequence[Message],
                           *, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """字符数估算 prompt token 数（确定性、无 tokenizer 依赖；口径与 MOD-04 对齐）。"""
    chars = sum(len(m.content) for m in messages)
    return int(math.ceil(chars / max(chars_per_token, 0.1))) + 4 * len(messages)


def make_result(*, text: str, usage: Usage, backend: BackendName, model_id: str,
                messages: Sequence[Message], finish_reason: FinishReason = "stop",
                outcome_kind: OutcomeKind = "action",
                parsed: Mapping[str, Any] | None = None) -> GenerationResult:
    """后端 ``generate()`` 的统一返回构造（所有实现共用，保证字段口径一致）。"""
    return GenerationResult(
        text=text, parsed=dict(parsed) if parsed is not None else None, usage=usage,
        attempts=1, retry_reasons=[], finish_reason=finish_reason, outcome_kind=outcome_kind,
        model_id=model_id, backend=backend, prompt_digest=prompt_digest(messages))


# =====================================================================
# 三级 JSON 调用（backend 与 manager 共用）
# =====================================================================

@dataclass
class _Attempt:
    """一次尝试的中间结果（用于最终构造埋点记录）。"""

    text: str
    usage: Usage
    finish_reason: str
    digest: str
    reasons: tuple[str, ...]


async def execute_json_call(*, backend: Backend, messages: Sequence[Message],
                            schema: Mapping[str, Any], grammar: str | None,
                            sampling: Mapping[str, Any], max_tokens: int, max_attempts: int,
                            backoff_ms: int, deadline: float,
                            now_ms: Callable[[], float], sleep: Callable[[float], Any],
                            ctx: CallContext, mode: str, call_id: str,
                            record_sink: Callable[[InferenceCallRecord], None] | None = None,
                            emit: Callable[..., None] | None = None,
                            json_mode: bool = True, truncated: bool = False,
                            stop: Sequence[str] | None = None,
                            resident_gb: float | None = None,
                            inflight: int | None = None) -> GenerationResult:
    """三级策略：① 约束解码 → ② ``jsonschema.validate`` → ③ 修复重试（PRD §8）。

    任一级成功即返回，``attempts`` 记录真实尝试次数（1..``max_attempts``）。失败一律抛
    ``E_LLM_SCHEMA_INVALID``（尝试耗尽）或 ``E_LLM_TIMEOUT``（墙上预算耗尽），且**不留半截
    结果**（``raw_texts`` 只含完整返回）。每次尝试产出一条埋点记录（FR-03-06）。
    """
    convo: list[Message] = list(messages)
    attempts: list[_Attempt] = []
    retry_reasons: list[str] = []
    outcome: OutcomeKind | None = None
    final_text, final_parsed = "", None
    final_usage: Usage | None = None
    final_finish: FinishReason = "stop"
    degraded_reason: str | None = None

    def emit_records(total: int, result: str, finish: str, code: str | None,
                     final_degraded: str | None = None) -> list[InferenceCallRecord]:
        return _emit_records(
            attempts=attempts, total=total, outcome=result, finish=finish, error_code=code,
            ctx=ctx, mode=mode, call_id=call_id, backend=backend, sampling=sampling,
            max_tokens=max_tokens, grammar_used=grammar is not None, json_mode=json_mode,
            record_sink=record_sink, emit=emit, resident_gb=resident_gb, inflight=inflight,
            final_degraded=final_degraded)

    def timeout_error(message: str, cause: BaseException | None = None) -> InferenceError:
        err = InferenceError(E.E_LLM_TIMEOUT, message, attempts=len(attempts), retryable=True,
                             detail={"raw_texts": [a.text for a in attempts],
                                     "retry_reasons": list(retry_reasons)})
        return err

    for index in range(1, max_attempts + 1):
        remaining = deadline - now_ms()
        if remaining <= 0:
            emit_records(max_attempts, "error", "timeout", E.E_LLM_TIMEOUT)
            raise timeout_error(f"total_timeout_ms 耗尽（已完成 {len(attempts)} 次尝试）")
        try:
            out = await asyncio.wait_for(
                backend.generate(convo, max_tokens=max_tokens, grammar=grammar,
                                 json_mode=json_mode, stop=stop,
                                 timeout_ms=int(remaining), **sampling),
                timeout=max(remaining, 1) / 1000.0)
        except asyncio.TimeoutError as exc:
            emit_records(max_attempts, "error", "timeout", E.E_LLM_TIMEOUT)
            raise timeout_error(
                f"单次 generate 超时（已完成 {len(attempts)} 次尝试）", exc) from exc
        except InferenceError:
            raise
        except Exception as exc:
            emit_records(max_attempts, "error", "error", E.E_LLM_BACKEND_ERROR)
            raise InferenceError(E.E_LLM_BACKEND_ERROR, f"后端返回异常：{exc!r}",
                                 attempts=len(attempts), retryable=True,
                                 detail={"raw_texts": [a.text for a in attempts]}) from exc

        try:
            parsed = json.loads(out.text)
            validate(parsed, schema)
        except Exception as exc:
            if type(exc).__name__ == "SchemaError":
                raise InferenceError(E.E_LLM_BACKEND_ERROR,
                                     f"调用方提供的 JSON Schema 非法：{exc}",
                                     retryable=False,
                                     detail={"schema_error": str(exc)}) from exc
            if not isinstance(exc, json.JSONDecodeError) and \
                    type(exc).__name__ != "ValidationError":
                raise
            reason = retry_reason(exc)
            retry_reasons.append(reason)
            attempts.append(_Attempt(out.text, out.usage, out.finish_reason, out.prompt_digest,
                                     tuple(retry_reasons)))
            if index == max_attempts:
                break
            await sleep(backoff_ms * 2 ** (index - 1) / 1000.0)
            convo = repair_messages(messages, bad=out.text, error=exc, schema=schema)
            continue

        if grammar is not None:
            outcome, degraded_reason = "action", None
        else:
            outcome = "degraded"
            degraded_reason = ("grammar_unavailable"
                               if not getattr(backend, "_supports_grammar", True)
                               else "schema_not_compilable")
        attempts.append(_Attempt(out.text, out.usage, out.finish_reason, out.prompt_digest,
                                 tuple(retry_reasons)))
        final_text, final_usage = out.text, out.usage
        final_finish, final_parsed = out.finish_reason, dict(parsed)
        break

    if outcome is None:
        emit_records(max_attempts, "error", "error", E.E_LLM_SCHEMA_INVALID)
        raise InferenceError(
            E.E_LLM_SCHEMA_INVALID, f"{max_attempts} 次尝试后仍不满足 JSON Schema",
            attempts=max_attempts, retryable=False,
            detail={"retry_reasons": list(retry_reasons),
                    "raw_texts": [a.text for a in attempts]})

    records = emit_records(len(attempts), outcome, final_finish, E.E_LLM_SCHEMA_INVALID,
                           final_degraded=degraded_reason)
    assert final_usage is not None # 成功路径必有 usage
    return GenerationResult(
        text=final_text, parsed=final_parsed, usage=final_usage, attempts=len(attempts),
        retry_reasons=list(retry_reasons), finish_reason=final_finish, outcome_kind=outcome,
        model_id=backend.model_id, backend=backend.name, prompt_digest=prompt_digest(messages),
        raw_texts=[a.text for a in attempts], truncated=truncated,
        degraded_reason=degraded_reason, calls=tuple(records))


def _emit_records(*, attempts: Sequence[_Attempt], total: int, outcome: str, finish: str,
                  error_code: str | None, ctx: CallContext, mode: str, call_id: str,
                  backend: Backend, sampling: Mapping[str, Any], max_tokens: int,
                  grammar_used: bool, json_mode: bool,
                  record_sink: Callable[[InferenceCallRecord], None] | None,
                  emit: Callable[..., None] | None, resident_gb: float | None,
                  inflight: int | None,
                  final_degraded: str | None = None) -> list[InferenceCallRecord]:
    """为每次尝试产出一条 ``InferenceCallRecord`` 并埋点（FR-03-06 / NFR-03-05）。"""
    records: list[InferenceCallRecord] = []
    last = len(attempts)
    for index, attempt in enumerate(attempts, start=1):
        success = index == last and outcome != "error"
        record = make_call_record(
            call_id=call_id, model_id=backend.model_id, backend=backend.name, mode=mode,
            ctx=ctx, digest=attempt.digest, prompt_tokens=attempt.usage.prompt_tokens,
            completion_tokens=attempt.usage.completion_tokens, max_tokens=max_tokens,
            temperature=float(sampling.get("temperature", 1.0)),
            top_p=float(sampling.get("top_p", 0.95)), top_k=int(sampling.get("top_k", 0)),
            repetition_penalty=float(sampling.get("repetition_penalty", 1.0)),
            sampling_seed=sampling.get("seed"), grammar_used=grammar_used, json_mode=json_mode,
            latency_ms=attempt.usage.latency_ms, attempt=index, attempts=total,
            retry_reasons=attempt.reasons,
            outcome_kind=("error" if not success else outcome),
            finish_reason=(finish if success else (attempt.finish_reason or "error")),
            ttft_ms=attempt.usage.ttft_ms, prefill_ms=attempt.usage.prefill_ms,
            decode_ms=attempt.usage.decode_ms, error_code=(None if success else error_code),
            degraded_reason=(final_degraded if success else None),
            cache_hit_tokens=attempt.usage.cache_hit_tokens,
            resident_gb=resident_gb, inflight=inflight)
        records.append(record)
        if record_sink is not None:
            record_sink(record)
        if emit is not None:
            emit("llm.call", **record.to_event())
    return records


# =====================================================================
# 共享后端基类：三级 JSON 调用（供各实现直接使用）
# =====================================================================

class LocalBackend:
    """``Backend`` 的通用实现骨架：``generate_json`` 复用共享三级策略。

    子类只需实现 ``load`` / ``unload`` / ``generate`` / ``health``，并设置 ``name`` /
    ``model_id`` / ``_supports_grammar`` / ``_supports_json_mode``。
    """

    name: BackendName = "mock"
    model_id: str = ""
    _supports_grammar: bool = True
    _supports_json_mode: bool = True
    _mode: str = "train"

    async def generate_json(self, messages: Sequence[Message], schema: Mapping[str, Any], *,
                            max_attempts: int = 3, retry_backoff_ms: int = 200,
                            total_timeout_ms: int = 8_000, **sampling: Any) -> GenerationResult:
        """直接调用后端时的三级策略（``BackendManager`` 会做同样的校验与预算门禁）。"""
        raw = {key: value for key, value in sampling.items() if key in SAMPLING_KEYS}
        validated = validate_sampling(raw, mode=self._mode)
        grammar = compile_grammar(schema) if self._supports_grammar else None
        if grammar is None and not self._supports_json_mode:
            raise InferenceError(E.E_LLM_BACKEND_ERROR,
                                 "grammar 不可用且后端未声明 JSON mode", retryable=False)
        self._call_seq = int(getattr(self, "_call_seq", 0)) + 1
        return await execute_json_call(
            backend=self, messages=list(messages), schema=schema, grammar=grammar,
            sampling=validated, max_tokens=int(sampling.get("max_tokens", 256)),
            max_attempts=max_attempts, backoff_ms=retry_backoff_ms,
            stop=sampling.get("stop"),
            deadline=time.monotonic() * 1000.0 + total_timeout_ms,
            now_ms=lambda: time.monotonic() * 1000.0, sleep=asyncio.sleep,
            ctx=CallContext(seed=validated.get("seed")), mode=self._mode,
            call_id=f"c-{self._call_seq:06d}")


# =====================================================================
# MockBackend（测试基石，零依赖）
# =====================================================================

class MockBackend(LocalBackend):
    """按脚本逐次返回预设输出；支持注入延迟 / 异常 / OOM（PRD §11）。

    ``script`` 中的 ``str`` 为返回文本；``BaseException`` 实例为注入异常
    （``MemoryError`` → ``E_LLM_OOM``，``TimeoutError`` → ``E_LLM_TIMEOUT``）。脚本耗尽后
    粘住最后一项（便于 200 条 robustness 用单条脚本跑批）。
    """

    def __init__(self, script: Sequence[str | BaseException], *, delay_ms: int = 0,
                 name: BackendName = "mock", model_id: str = "mock-1",
                 resident_gb: float = 0.0) -> None:
        if not script:
            raise ValueError("MockBackend 的 script 不能为空")
        self._script: list[str | BaseException] = list(script)
        self.delay_ms = int(delay_ms)
        self.name = name
        self.model_id = model_id
        self.resident_gb = float(resident_gb)
        # 可观测计数（单测断言「零提交」「最大 inflight」）
        self.calls = 0
        self.inflight = 0
        self.max_inflight = 0
        self._index = 0
        self._spec: ModelSpec | None = None
        self._state: str = "unloaded"
        self._last_error_code: str | None = None

    # ---- 生命周期 ---------------------------------------------------------------
    async def load(self, spec: ModelSpec) -> None:
        self._spec = spec
        self.model_id = spec.model_id
        self.resident_gb = spec.resident_gb_est
        self._supports_grammar = bool(spec.supports_grammar)
        self._supports_json_mode = bool(spec.supports_json_mode)
        self._index = 0
        self._state = "ready"
        self._last_error_code = None

    async def unload(self) -> None:
        self._spec = None
        self._state = "unloaded"

    # ---- 生成 -------------------------------------------------------------------
    async def generate(self, messages: Sequence[Message], *, max_tokens: int = 256,
                       temperature: float = 1.0, top_p: float = 0.95, top_k: int = 0,
                       repetition_penalty: float = 1.0, seed: int | None = None,
                       stop: Sequence[str] | None = None, timeout_ms: int = 30_000,
                       grammar: str | None = None, json_mode: bool = False) -> GenerationResult:
        if self._state != "ready":
            raise InferenceError(E.E_LLM_LOAD_FAILED, "MockBackend 尚未 load()",
                                 hint="先 await manager.load(model_id)")
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay_ms:
                await asyncio.sleep(self.delay_ms / 1000.0)
            item = self._script[min(self._index, len(self._script) - 1)]
            self._index += 1
            self.calls += 1
            if isinstance(item, BaseException):
                raise self._map_error(item)
            latency = self.delay_ms
            prefill = latency // 2
            usage = Usage(prompt_tokens=estimate_prompt_tokens(messages),
                          completion_tokens=max(1, len(item) // 4), latency_ms=latency,
                          ttft_ms=(0 if latency == 0 else min(1, latency)),
                          prefill_ms=prefill, decode_ms=latency - prefill)
            return make_result(text=item, usage=usage, backend=self.name,
                               model_id=self.model_id, messages=messages)
        finally:
            self.inflight -= 1

    async def health(self) -> BackendHealth:
        ready = self._state == "ready"
        return BackendHealth(state=self._state, # type: ignore[arg-type]
                             backend=self.name, model_id=self.model_id or None,
                             resident_gb=(self.resident_gb if ready else 0.0),
                             inflight=self.inflight,
                             last_error_code=self._last_error_code)

    # ---- 内部 -------------------------------------------------------------------
    def _map_error(self, exc: BaseException) -> InferenceError:
        if isinstance(exc, InferenceError):
            self._last_error_code = str(exc.code)
            return exc
        if isinstance(exc, MemoryError):
            code: E | str = E.E_LLM_OOM
        elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            code = E.E_LLM_TIMEOUT
        else:
            code = E.E_LLM_BACKEND_ERROR
        self._last_error_code = str(code)
        return InferenceError(code, f"MockBackend 注入异常：{exc!r}",
                              detail={"injected": type(exc).__name__})


# =====================================================================
# BackendManager（单例门面，FR-03-01/02/08）
# =====================================================================

class BackendManager:
    """模型常驻门面：single-flight 加载、并发限流、内存预算前置、三级 JSON 重试。

    ``clock`` / ``sleep`` / ``backend_factory`` / ``record_sink`` 为可注入点：
    单测用假时钟观测退避、用 ``MockBackend`` 注入脚本、用 ``record_sink`` 收集埋点。

    ``ram_budget_gb`` 解析优先级：显式参数 > ``config.ram_budget_gb`` > 注册表
    ``ram_budget_gb``（PRD §9：配置可覆盖注册表；两者缺省一致为 20.0）。
    """

    def __init__(self, registry_path: str = DEFAULT_REGISTRY_PATH, *,
                 mode: Literal["train", "eval"] = "train",
                 ram_budget_gb: float | None = None,
                 infra: InfraFacade | None = None,
                 config: Any = None,
                 registry: ModelRegistry | None = None,
                 backend_factory: Callable[[ModelSpec], Backend] | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Any] | None = None,
                 record_sink: Callable[[InferenceCallRecord], None] | None = None,
                 chars_per_token: float = CHARS_PER_TOKEN,
                 runs_root: Path | str | None = None) -> None:
        self.registry = registry if registry is not None else load_registry(registry_path)
        self.mode = mode
        self.config = config
        self.infra: InfraFacade = infra if infra is not None else NullInfra()
        if ram_budget_gb is not None:
            self.ram_budget_gb = float(ram_budget_gb)
        elif config is not None and getattr(config, "ram_budget_gb", None):
            self.ram_budget_gb = float(config.ram_budget_gb)
        else:
            self.ram_budget_gb = float(self.registry.ram_budget_gb)
        self._factory = backend_factory
        self._clock = clock or (lambda: time.monotonic() * 1000.0)
        self._sleep = sleep or asyncio.sleep
        self._record_sink = record_sink
        self._chars_per_token = float(chars_per_token)
        #: API 后端的审计工件落点（``runs/<run_id>/<api_record_dir>``）。不传则由适配器
        #: 回落到 ``Path("runs")`` 并记 ``llm.api.record_dir_fallback`` 事件 —— 曾经因为
        #: 这里没往下传，审计记录落到了仓库根的 runs/ 而不是 run 目录（真跑时发现）。
        self._runs_root = Path(runs_root) if runs_root else None
        self._backends: dict[str, Backend] = {}
        self._states: dict[str, str] = {}
        self._last_errors: dict[str, str | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._inflight = 0
        self.max_inflight_observed = 0
        self._call_seq = 0

    # ---- 生命周期（FR-03-01 / FR-03-02）-------------------------------------------
    def get(self, model_id: str) -> Backend:
        """已加载则直接返回；否则抛 ``E_LLM_UNAVAILABLE``（FR-03-01）。"""
        self.registry.spec(model_id) # 未知 id 先抛
        backend = self._backends.get(model_id)
        if backend is None:
            raise InferenceError(E.E_LLM_UNAVAILABLE, f"模型 {model_id!r} 尚未加载",
                                 hint=f"先 await load({model_id!r}) 或用 manager.session(...)")
        return backend

    async def load(self, model_id: str) -> Backend:
        """幂等 + single-flight 加载；未知 id 抛 ``E_LLM_UNAVAILABLE``（FR-03-01）。"""
        spec = self.registry.spec(model_id)
        backend = self._backends.get(model_id)
        if backend is not None:
            return backend
        lock = self._locks.setdefault(model_id, asyncio.Lock())
        async with lock:
            backend = self._backends.get(model_id)
            if backend is not None:
                return backend
            self.check_budget(spec)
            self._states[model_id] = "loading"
            backend = self._make_backend(spec)
            try:
                await backend.load(spec)
            except InferenceError as exc:
                self._states[model_id] = "error"
                self._last_errors[model_id] = str(exc.code)
                if exc.code == E.E_LLM_OOM:
                    self.infra.emit("llm.oom", code=str(exc.code), model_id=model_id)
                raise
            except Exception as exc: # 后端实现的意外异常统一包装（R-1：API 变动隔离在一层）
                self._states[model_id] = "error"
                self._last_errors[model_id] = str(E.E_LLM_LOAD_FAILED)
                raise InferenceError(E.E_LLM_LOAD_FAILED,
                                     f"加载 {model_id!r} 失败：{exc!r}",
                                     retryable=True) from exc
            self._backends[model_id] = backend
            self._states[model_id] = "ready"
            self._last_errors[model_id] = None
            return backend

    async def unload(self, model_id: str) -> None:
        """释放权重与 KV；未加载时静默返回（FR-03-02，幂等）。"""
        self.registry.spec(model_id)
        backend = self._backends.pop(model_id, None)
        if backend is None:
            self._states[model_id] = "unloaded"
            return
        await backend.unload()
        self._states[model_id] = "unloaded"

    async def unload_all(self) -> None:
        for model_id in list(self._backends):
            await self.unload(model_id)

    @asynccontextmanager
    async def session(self, model_id: str) -> AsyncIterator[Backend]:
        """``async with manager.session(id)`` 退出自动卸载（FR-03-02）。"""
        backend = await self.load(model_id)
        try:
            yield backend
        finally:
            await self.unload(model_id)

    # ---- 预算与上下文（FR-03-08 / FR-03-10）---------------------------------------
    def effective_max_inflight(self, spec: ModelSpec) -> int:
        """评估模式强制 1（R-5）；否则取注册表与配置的较小值（保守）。"""
        if self.mode == "eval":
            return 1
        cfg_limit = int(getattr(self.config, "max_inflight", 0) or 0)
        return max(1, min(spec.max_inflight, cfg_limit) if cfg_limit else spec.max_inflight)

    def estimate_required_gb(self, model_id: str, *, max_inflight: int | None = None,
                             context_length: int | None = None) -> float:
        """内存预算估算（PRD §7 公式）；默认取生效并发与注册表上下文长度。"""
        spec = self.registry.spec(model_id)
        return estimate_required_gb(
            spec,
            max_inflight=self.effective_max_inflight(spec) if max_inflight is None
            else int(max_inflight),
            context_length=spec.context_length if context_length is None
            else int(context_length))

    def check_budget(self, spec: ModelSpec, *, max_inflight: int | None = None,
                     context_length: int | None = None) -> float:
        """acquire 之前的预算前置检查；超预算抛 ``E_LLM_OOM`` 且**不提交**推理（FR-03-08）。"""
        limit = self.effective_max_inflight(spec) if max_inflight is None else int(max_inflight)
        context = spec.context_length if context_length is None else int(context_length)
        required = estimate_required_gb(spec, max_inflight=limit, context_length=context)
        if required > self.ram_budget_gb + _BUDGET_EPS:
            self.infra.emit("llm.oom", code=str(E.E_LLM_OOM), model_id=spec.model_id,
                            required_gb=round(required, 4), ram_budget_gb=self.ram_budget_gb,
                            max_inflight=limit, context_length=context)
            raise InferenceError(
                E.E_LLM_OOM,
                f"内存预算不足：required={required:.2f}GB > ram_budget={self.ram_budget_gb:.2f}GB"
                f"（max_inflight={limit}, context_length={context}）",
                retryable=True,
                detail={"required_gb": round(required, 4),
                        "ram_budget_gb": self.ram_budget_gb, "max_inflight": limit,
                        "context_length": context, "model_id": spec.model_id})
        return required

    def guard_context(self, spec: ModelSpec, messages: Sequence[Message],
                      max_tokens: int) -> tuple[list[Message], bool]:
        """上下文守卫（FR-03-10）：``reject`` 抛码；``truncate`` 从最早非 system 截断。"""
        policy = str(getattr(self.config, "context_overflow_policy", "reject") or "reject")
        context_length = int(spec.context_length)
        estimate = self._estimate_tokens(messages)
        if estimate + max_tokens <= context_length:
            return list(messages), False
        detail = {"prompt_tokens_est": estimate, "max_tokens": max_tokens,
                  "context_length": context_length, "policy": policy}
        if policy != "truncate":
            raise InferenceError(
                E.E_LLM_CONTEXT_OVERFLOW,
                f"上下文溢出：est_prompt({estimate}) + max_tokens({max_tokens})"
                f" > context_length({context_length})", retryable=False, detail=detail)
        msgs = list(messages)
        dropped = 0
        while self._estimate_tokens(msgs) + max_tokens > context_length:
            index = next((i for i, m in enumerate(msgs) if m.role != "system"), None)
            if index is None:
                raise InferenceError(
                    E.E_LLM_CONTEXT_OVERFLOW,
                    "truncate 后仍溢出：只剩 system 消息，无法继续截断",
                    retryable=False, detail=detail)
            msgs.pop(index)
            dropped += 1
        return msgs, dropped > 0

    # ---- 生成（FR-03-04）----------------------------------------------------------
    async def generate_json(self, model_id: str, messages: Sequence[Message],
                            schema: Mapping[str, Any], **kwargs: Any) -> GenerationResult:
        """门面级三级 JSON 生成（PRD §8 伪代码的真实控制流）。

        ``mode`` 可按调用覆盖（缺省 ``self.mode``）：MOD-04 的同一个 agent 在训练局用
        ``train`` 口径、在评估局用 ``eval`` 口径（B-14 的收紧只在评估口生效），
        同步门面 ``BackendInferenceClient`` 会把调用方声明的 mode 原样带进来。
        """
        spec = self.registry.spec(model_id)
        config = self.config
        mode = str(kwargs.get("mode") or self.mode)
        raw_sampling = {key: kwargs[key] for key in SAMPLING_KEYS if key in kwargs}
        if raw_sampling.get("seed") is None and getattr(config, "seed", None) is not None:
            raw_sampling["seed"] = config.seed
        sampling = validate_sampling(raw_sampling, mode=mode) # 越界即抛，零埋点
        max_tokens = int(kwargs.get("max_tokens", getattr(config, "max_tokens", 256)))
        max_attempts = int(kwargs.get("max_attempts", getattr(config, "max_attempts", 3)))
        backoff_ms = int(kwargs.get("retry_backoff_ms", getattr(config, "retry_backoff_ms", 200)))
        total_timeout_ms = int(kwargs.get("total_timeout_ms",
                                          getattr(config, "total_timeout_ms", 120_000)))
        ctx = kwargs.get("call_context") or CallContext(run_id=self.infra.run_id())
        if ctx.seed is None and sampling.get("seed") is not None:
            ctx = replace(ctx, seed=sampling["seed"])
        grammar = compile_grammar(schema) if spec.supports_grammar else None
        if grammar is None and not spec.supports_json_mode:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                "grammar 不可用且注册表声明不支持 JSON mode（Spec P-09 降级路径不可用）",
                retryable=False, detail={"schema_compilable": False, "model_id": model_id})
        messages_used, truncated = self.guard_context(spec, messages, max_tokens)
        limit = self.effective_max_inflight(spec)
        self.check_budget(spec, max_inflight=limit) # acquire 之前（FR-03-08）
        backend = await self.load(model_id)
        deadline = self._now_ms() + total_timeout_ms
        async with self._semaphore(limit):
            self._inflight += 1
            self.max_inflight_observed = max(self.max_inflight_observed, self._inflight)
            try:
                result = await execute_json_call(
                    backend=backend, messages=messages_used, schema=schema, grammar=grammar,
                    sampling=sampling, max_tokens=max_tokens, max_attempts=max_attempts,
                    backoff_ms=backoff_ms, deadline=deadline, now_ms=self._now_ms,
                    sleep=self._sleep, ctx=ctx, mode=mode,
                    call_id=self._next_call_id(), record_sink=self._record_sink,
                    emit=self.infra.emit, json_mode=True, truncated=truncated,
                    stop=kwargs.get("stop"), resident_gb=spec.resident_gb_est,
                    inflight=self._inflight)
            finally:
                self._inflight -= 1
        if result.outcome_kind == "degraded":
            self.infra.emit("llm.degrade", code=str(E.E_LLM_BACKEND_ERROR),
                            reason=result.degraded_reason, model_id=model_id)
        if getattr(config, "debug_dump_raw", False):
            for record, text in zip(result.calls, result.raw_texts):
                self.infra.dump_raw(record.call_id, text)
        return result

    async def generate(self, model_id: str, messages: Sequence[Message],
                       **kwargs: Any) -> GenerationResult:
        """自由文本生成（反思 / 记忆 / 候选改进，PRD §3）：一次尝试、无 schema。"""
        spec = self.registry.spec(model_id)
        config = self.config
        mode = str(kwargs.get("mode") or self.mode)
        raw_sampling = {key: kwargs[key] for key in SAMPLING_KEYS if key in kwargs}
        if raw_sampling.get("seed") is None and getattr(config, "seed", None) is not None:
            raw_sampling["seed"] = config.seed
        sampling = validate_sampling(raw_sampling, mode=mode)
        max_tokens = int(kwargs.get("max_tokens", getattr(config, "max_tokens", 256)))
        messages_used, _ = self.guard_context(spec, messages, max_tokens)
        limit = self.effective_max_inflight(spec)
        self.check_budget(spec, max_inflight=limit)
        backend = await self.load(model_id)
        ctx = kwargs.get("call_context") or CallContext(
            run_id=self.infra.run_id(), purpose="free_text",
            schema_id=None, caller="mod-04")
        async with self._semaphore(limit):
            self._inflight += 1
            self.max_inflight_observed = max(self.max_inflight_observed, self._inflight)
            timeout_ms = int(kwargs.get("timeout_ms",
                                        getattr(config, "generate_timeout_ms", 30_000)))
            try:
                out = await asyncio.wait_for(
                    backend.generate(messages_used, max_tokens=max_tokens,
                                     stop=kwargs.get("stop"), timeout_ms=timeout_ms,
                                     **sampling),
                    timeout=max(timeout_ms, 1) / 1000.0)
            except asyncio.TimeoutError as exc:
                raise InferenceError(E.E_LLM_TIMEOUT,
                                     f"自由文本生成超时（{timeout_ms}ms）",
                                     retryable=True) from exc
            finally:
                self._inflight -= 1
        record = make_call_record(
            call_id=self._next_call_id(), model_id=backend.model_id, backend=backend.name,
            mode=mode, ctx=ctx, digest=out.prompt_digest,
            prompt_tokens=out.usage.prompt_tokens,
            completion_tokens=out.usage.completion_tokens, max_tokens=max_tokens,
            temperature=sampling["temperature"], top_p=sampling["top_p"],
            top_k=sampling["top_k"], repetition_penalty=sampling["repetition_penalty"],
            sampling_seed=sampling["seed"], grammar_used=False, json_mode=False,
            latency_ms=out.usage.latency_ms, attempt=1, attempts=1,
            outcome_kind=out.outcome_kind, finish_reason=out.finish_reason,
            cache_hit_tokens=out.usage.cache_hit_tokens,
            resident_gb=spec.resident_gb_est, inflight=self._inflight)
        if self._record_sink is not None:
            self._record_sink(record)
        self.infra.emit("llm.call", **record.to_event())
        return replace(out, calls=(record,))

    # ---- 探针与可选辅助接口 -------------------------------------------------------
    async def health(self) -> dict[str, BackendHealth]:
        """全部注册模型的状态（未加载者 ``state="unloaded"``，FR-03-02）。"""
        result: dict[str, BackendHealth] = {}
        for model_id, spec in self.registry.models.items():
            backend = self._backends.get(model_id)
            if backend is None:
                result[model_id] = BackendHealth(
                    state=self._states.get(model_id, "unloaded"), # type: ignore[arg-type]
                    backend=spec.backend, model_id=model_id, resident_gb=0.0, inflight=0,
                    last_error_code=self._last_errors.get(model_id))
            else:
                result[model_id] = await backend.health()
        return result

    def count_tokens(self, model_id: str, messages: Sequence[Message]) -> int:
        """可选辅助接口（B-01）：优先用后端 tokenizer，缺失时降级字符估算。"""
        backend = self._backends.get(model_id)
        counter = getattr(backend, "count_tokens", None)
        if callable(counter):
            return int(counter(messages))
        return estimate_prompt_tokens(messages, chars_per_token=self._chars_per_token)

    async def embed(self, model_id: str, texts: Sequence[str]) -> list[list[float]]:
        """可选辅助接口（B-01）：阶段一未实现 → ``E_LLM_UNSUPPORTED``（MOD-05 降级 BM25）。"""
        backend = self._backends.get(model_id)
        embedder = getattr(backend, "embed", None)
        if callable(embedder):
            return await embedder(texts)
        raise InferenceError(E.E_LLM_UNSUPPORTED,
                             "embed() 未实现（阶段一默认关闭，B-01）",
                             hint="调用方应降级为 BM25（MOD-05）")

    # ---- 内部 -------------------------------------------------------------------
    def _make_backend(self, spec: ModelSpec) -> Backend:
        if self._factory is not None:
            return self._factory(spec)
        if spec.backend == "mock":
            return MockBackend([DECISION_MOCK_TEXT], model_id=spec.model_id,
                               name="mock", resident_gb=spec.resident_gb_est)
        if spec.backend == "mlx":
            from .mlx_backend import MLXBackend
            return MLXBackend(infra=self.infra, config=self.config)
        if spec.backend in ("llama_cpp", "llama_server"):
            from .llama_backend import LlamaCppBackend, LlamaServerBackend
            if spec.backend == "llama_server":
                return LlamaServerBackend(infra=self.infra, config=self.config)
            return LlamaCppBackend(infra=self.infra, config=self.config)
        if spec.backend == "deepseek":
            # Issue #1：API 后端与本地后端在 BackendManager 眼里完全同形 —— 三级 JSON
            # 策略、schema 校验、修复重试、预算门禁、埋点全部复用，不另起一套。
            from .deepseek_backend import DeepSeekAPIAdapter
            return DeepSeekAPIAdapter(infra=self.infra, config=self.config,
                                      runs_root=self._runs_root)
        raise InferenceError(E.E_LLM_UNAVAILABLE,
                             f"未知 backend={spec.backend!r}"
                             "（合法：mlx/llama_cpp/llama_server/mock/deepseek）")

    def _estimate_tokens(self, messages: Sequence[Message]) -> int:
        return estimate_prompt_tokens(messages, chars_per_token=self._chars_per_token)

    def _now_ms(self) -> float:
        return float(self._clock())

    def _semaphore(self, limit: int) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(limit)
        if semaphore is None:
            semaphore = self._semaphores[limit] = asyncio.Semaphore(limit)
        return semaphore

    def _next_call_id(self) -> str:
        self._call_seq += 1
        return f"c-{self._call_seq:06d}"
