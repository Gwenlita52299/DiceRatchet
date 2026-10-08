"""MOD-03 埋点记录与调用上下文（PRD §13 / NFR-03-05）。

一次调用 = 每次尝试一条 ``InferenceCallRecord``；本模块**只产出对象**，
``runs/<run_id>/metrics/llm_calls.jsonl`` 的落盘归 MOD-09（PRD §2.2）。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

__all__ = ["InferenceCallRecord", "CallContext", "prompt_digest", "make_call_record",
           "utc_now_iso", "CALL_EVENT", "SCHEMA_ID_DECISION"]

#: 埋点事件名（登记在 ``infra.events.EVENT_REGISTRY``）。
CALL_EVENT: Final[str] = "llm_call"

#: DECISION_SCHEMA 在埋点中的 schema_id。
SCHEMA_ID_DECISION: Final[str] = "decision.v1"

#: ``prompt_digest`` 的分隔符（单元分隔符 US，PRD §13 口径）。
_SEP: Final[str] = "\x1f"


def utc_now_iso() -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ``（UTC，与 infra.events 口径一致）。"""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def prompt_digest(messages: Sequence[Any]) -> str:
    """``"sha256:" + sha256(␟ 连接各 f"{m.role}\\x1f{m.content}")``（可离线复算，PRD §13）。

    接受任何带 ``.role`` / ``.content`` 的对象（``Message`` 或鸭子类型），
    便于在恢复/审计路径上不依赖本模块的数据类。
    """
    parts = [f"{getattr(m, 'role', '')}{_SEP}{getattr(m, 'content', '')}" for m in messages]
    payload = _SEP.join(parts).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CallContext:
    """调用方上下文（MOD-04/06/08 传入，用于把埋点对齐到 run/episode/step）。

    全部字段可选：独立使用（probe/bench/robustness 或单测）时不传即可。
    """

    run_id: str | None = None
    episode_id: str | None = None
    generation: int | None = None
    harness_version: int | None = None
    seed: int | None = None
    step: int | None = None
    purpose: str = "decision"
    schema_id: str | None = SCHEMA_ID_DECISION
    caller: str = "mod-04"


@dataclass(frozen=True)
class InferenceCallRecord:
    """``runs/<run_id>/metrics/llm_calls.jsonl`` 的一行（PRD §13 逐字段）。

    冻结数据类：由 ``make_call_record()`` 构造，禁止事后修改（埋点不可篡改）。
    """

    call_id: str
    ts: str
    event: str
    run_id: str | None
    episode_id: str | None
    generation: int | None
    harness_version: int | None
    seed: int | None
    step: int | None
    model_id: str
    backend: str
    mode: str
    purpose: str
    schema_id: str | None
    caller: str
    prompt_digest: str
    prompt_tokens: int
    completion_tokens: int
    max_tokens: int
    temperature: float
    top_p: float
    top_k: int
    repetition_penalty: float
    sampling_seed: int | None
    grammar_used: bool
    json_mode: bool
    latency_ms: int
    ttft_ms: int | None
    prefill_ms: int | None
    decode_ms: int | None
    tok_per_s: float | None
    attempt: int
    attempts: int
    retry_reasons: tuple[str, ...]
    outcome_kind: str
    finish_reason: str
    degraded_reason: str | None = None
    error_code: str | None = None
    resident_gb: float | None = None
    inflight: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON 友好形态（tuple → list；浮点 round(4)，对齐 B-34）。"""
        data = asdict(self)
        data["retry_reasons"] = list(self.retry_reasons)
        for key, value in list(data.items()):
            if isinstance(value, float):
                data[key] = round(value, 4)
        return data

    def to_event(self) -> dict[str, Any]:
        """``llm.call`` 事件体（infra.events 会自动补 ``ts``/``run_id``/``module``）。"""
        data = self.as_dict()
        return {"call_id": data["call_id"], "model_id": data["model_id"],
                "backend": data["backend"], "latency_ms": data["latency_ms"],
                "prompt_tokens": data["prompt_tokens"],
                "completion_tokens": data["completion_tokens"],
                "attempt": data["attempt"], "outcome_kind": data["outcome_kind"],
                "prompt_digest": data["prompt_digest"], "error_code": data["error_code"],
                "grammar_used": data["grammar_used"], "json_mode": data["json_mode"]}

    def step_fields(self, *, text: str | None = None) -> dict[str, Any]:
        """投影到轨迹步记录的**可选字段**（FR-03-06 / Spec §4.5）。

        MOD-04/09 落 ``episodes/<split>/<episode_key>.jsonl`` 时把这四个键并进步行：
        ``llm_latency_ms`` / ``prompt_tokens`` / ``completion_tokens`` / ``llm_raw``
        （``llm_raw`` 只在给了 ``text`` 时写入；不外泄 seed 等审计侧字段）。
        """
        fields: dict[str, Any] = {"llm_latency_ms": self.latency_ms,
                                  "prompt_tokens": self.prompt_tokens,
                                  "completion_tokens": self.completion_tokens}
        if text is not None:
            fields["llm_raw"] = text
        return fields


def make_call_record(*, call_id: str, model_id: str, backend: str, mode: str, ctx: CallContext,
                     digest: str, prompt_tokens: int, completion_tokens: int, max_tokens: int,
                     temperature: float, top_p: float, top_k: int, repetition_penalty: float,
                     sampling_seed: int | None, grammar_used: bool, json_mode: bool,
                     latency_ms: int, attempt: int, attempts: int,
                     retry_reasons: Sequence[str] = (), outcome_kind: str = "action",
                     finish_reason: str = "stop", ttft_ms: int | None = None,
                     prefill_ms: int | None = None, decode_ms: int | None = None,
                     degraded_reason: str | None = None, error_code: str | None = None,
                     resident_gb: float | None = None, inflight: int | None = None,
                     ts: str | None = None, seed: int | None = None) -> InferenceCallRecord:
    """构造一条埋点记录；``tok_per_s = completion_tokens / decode_ms × 1000``（PRD §13）。"""
    tok_per_s: float | None = None
    if decode_ms and decode_ms > 0 and completion_tokens > 0:
        tok_per_s = completion_tokens / decode_ms * 1000.0
    return InferenceCallRecord(
        call_id=call_id, ts=ts or utc_now_iso(), event=CALL_EVENT,
        run_id=ctx.run_id, episode_id=ctx.episode_id, generation=ctx.generation,
        harness_version=ctx.harness_version,
        seed=ctx.seed if ctx.seed is not None else seed, step=ctx.step,
        model_id=model_id, backend=backend, mode=mode, purpose=ctx.purpose,
        schema_id=ctx.schema_id, caller=ctx.caller, prompt_digest=digest,
        prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens),
        max_tokens=int(max_tokens), temperature=float(temperature), top_p=float(top_p),
        top_k=int(top_k), repetition_penalty=float(repetition_penalty),
        sampling_seed=sampling_seed, grammar_used=bool(grammar_used), json_mode=bool(json_mode),
        latency_ms=int(latency_ms), ttft_ms=ttft_ms, prefill_ms=prefill_ms, decode_ms=decode_ms,
        tok_per_s=tok_per_s, attempt=int(attempt), attempts=int(attempts),
        retry_reasons=tuple(retry_reasons), outcome_kind=outcome_kind,
        finish_reason=finish_reason, degraded_reason=degraded_reason, error_code=error_code,
        resident_gb=resident_gb, inflight=inflight)


def read_call_records(path: Path) -> list[dict[str, Any]]:
    """读取 ``llm_calls.jsonl``（容错末行截断，供聚合与测试）。"""
    import json

    rows: list[dict[str, Any]] = []
    if not Path(path).is_file():
        return rows
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError: # 末行截断：丢弃，不中断（NFR-09-1）
            continue
    return rows


# 便于调用方 ``from .records import RECORD_FIELDS`` 做 schema round-trip
RECORD_FIELDS: Final[tuple[str, ...]] = tuple(f for f in InferenceCallRecord.__dataclass_fields__)
