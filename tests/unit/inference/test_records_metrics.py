"""埋点记录与聚合指标（FR-03-06 / NFR-03-05 / PRD §13；G5 round-trip）。"""

from __future__ import annotations

import hashlib

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.infra.io import validate as validate_schema
from dice_ratchet.inference.backend import Message
from dice_ratchet.inference.metrics import aggregate, percentile
from dice_ratchet.inference.records import (CallContext, InferenceCallRecord, make_call_record,
                                         prompt_digest, read_call_records)
from dice_ratchet.inference.schema import DECISION_SCHEMA

MESSAGES = [Message(role="system", content="sys"), Message(role="user", content="usr")]


def _record(*, call_id="c-000001", attempt=1, attempts=1, outcome="action",
            error_code=None, latency=100, decode=50, completion=10, purpose="decision",
            episode_id=None, backend="mock", model_id="mock-a", mode="train",
            retry_reasons=(), degraded_reason=None):
    return make_call_record(
        call_id=call_id, model_id=model_id, backend=backend, mode=mode,
        ctx=CallContext(run_id="r-1", episode_id=episode_id, purpose=purpose, seed=7),
        digest=prompt_digest(MESSAGES), prompt_tokens=20, completion_tokens=completion,
        max_tokens=256, temperature=0.0, top_p=1.0, top_k=0, repetition_penalty=1.0,
        sampling_seed=7, grammar_used=True, json_mode=True, latency_ms=latency,
        attempt=attempt, attempts=attempts, retry_reasons=retry_reasons,
        outcome_kind=outcome, finish_reason="stop", decode_ms=decode, ttft_ms=1,
        prefill_ms=1, error_code=error_code, degraded_reason=degraded_reason,
        resident_gb=5.6, inflight=1)


# ---- prompt_digest 口径 --------------------------------------------------------------

def test_prompt_digest_matches_documented_formula():
    expected = "sha256:" + hashlib.sha256(
        "system\x1fsys\x1fuser\x1fusr".encode("utf-8")).hexdigest()
    assert prompt_digest(MESSAGES) == expected
    assert prompt_digest(MESSAGES) == prompt_digest(list(MESSAGES)) # 稳定
    assert prompt_digest(MESSAGES) != prompt_digest([MESSAGES[0]])
    assert prompt_digest([Message(role="user", content="sys"),
                          Message(role="system", content="usr")]) != expected # role 参与


def test_prompt_digest_does_not_collide_on_separator():
    a = [Message(role="ab", content="c")]
    b = [Message(role="a", content="bc")]
    assert prompt_digest(a) != prompt_digest(b)


# ---- 记录结构（G5）-------------------------------------------------------------------

def test_call_record_round_trips_against_schema():
    record = _record()
    assert isinstance(record, InferenceCallRecord)
    payload = record.as_dict()
    validate_schema("inference_call", payload)
    assert payload["event"] == "llm_call"
    assert payload["prompt_digest"].startswith("sha256:")
    assert payload["tok_per_s"] == pytest.approx(200.0) # 10 / 50ms × 1000


def test_call_record_event_payload_has_required_fields():
    event = _record().to_event()
    for key in ("call_id", "model_id", "backend", "latency_ms", "prompt_tokens",
                "completion_tokens", "attempt", "outcome_kind"):
        assert key in event


def test_call_record_step_fields_are_trajectory_safe():
    """FR-03-06：投到轨迹步行的四个可选字段必须都在 MOD-09 的登记集合内。"""
    from dice_ratchet.infra.logging import _OPTIONAL_LINE_KEYS

    record = _record(latency=812)
    fields = record.step_fields(text="{\"reasoning\": \"ok\"}")
    assert fields == {"llm_latency_ms": 812, "prompt_tokens": 20, "completion_tokens": 10,
                      "llm_raw": "{\"reasoning\": \"ok\"}"}
    assert set(fields) <= _OPTIONAL_LINE_KEYS
    # 不传 text 时不写 llm_raw（默认只存 digest，NFR-03-05）
    assert "llm_raw" not in record.step_fields()
    # 不外泄审计侧字段（seed 不进步记录）
    assert "seed" not in fields and "sampling_seed" not in fields


def test_read_call_records_tolerates_truncated_tail(tmp_path):
    path = tmp_path / "llm_calls.jsonl"
    path.write_text('{"call_id": "c-000001"}\n{"call_id": "c-00', encoding="utf-8")
    rows = read_call_records(path)
    assert rows == [{"call_id": "c-000001"}]
    assert read_call_records(tmp_path / "missing.jsonl") == []


# ---- 聚合指标 ------------------------------------------------------------------------

def test_percentile_interpolates():
    assert percentile([], 0.5) == 0.0
    assert percentile([10], 0.95) == 10.0
    assert percentile([0, 100], 0.5) == 50.0
    assert percentile([1, 2, 3, 4], 1.0) == 4.0


def test_aggregate_reports_first_pass_retry_and_error_rates():
    records = [
        _record(call_id="c-000001", latency=100, decode=50, completion=10),
        _record(call_id="c-000002", attempt=1, attempts=2, outcome="error",
                error_code=E.E_LLM_SCHEMA_INVALID, latency=100, decode=0, completion=0,
                retry_reasons=("JSONDecodeError:$",)),
        _record(call_id="c-000002", attempt=2, attempts=2, outcome="action",
                latency=200, decode=100, completion=20),
        _record(call_id="c-000003", attempt=1, attempts=3, outcome="error",
                error_code=E.E_LLM_SCHEMA_INVALID, latency=100, decode=0, completion=0),
        _record(call_id="c-000003", attempt=2, attempts=3, outcome="error",
                error_code=E.E_LLM_SCHEMA_INVALID, latency=100, decode=0, completion=0),
        _record(call_id="c-000003", attempt=3, attempts=3, outcome="error",
                error_code=E.E_LLM_SCHEMA_INVALID, latency=100, decode=0, completion=0),
    ]
    agg = aggregate(records)
    overall = agg["overall"]
    assert agg["n_attempt_records"] == 6
    assert overall["n_calls"] == 3
    assert overall["first_pass_valid_rate"] == round(1 / 3, 4)
    assert overall["retry_histogram"] == {"1": 1, "2": 1, "3": 1}
    # 只有 c-000003 是「修复耗尽」的失败调用；c-000002 重试后成功
    assert overall["schema_fail_rate"] == round(1 / 3, 4)
    assert overall["error_rate_by_code"] == {str(E.E_LLM_SCHEMA_INVALID): round(1 / 3, 4)}
    assert overall["latency_ms"]["p50"] > 0
    assert "mock-a|decision" in agg["by_model_purpose"]


def test_aggregate_counts_degraded_as_valid_but_not_action():
    records = [_record(outcome="degraded", degraded_reason="grammar_unavailable",
                       backend="mlx")]
    overall = aggregate(records)["overall"]
    assert overall["first_pass_valid_rate"] == 1.0 # MLX JSON mode 首答通过（§13 口径修正）
    assert overall["first_pass_action_rate"] == 0.0
    assert overall["degraded_call_rate"] == 1.0


def test_aggregate_per_episode_costs():
    records = [
        _record(call_id="c-000001", episode_id="e-1"),
        _record(call_id="c-000002", episode_id="e-1"),
        _record(call_id="c-000003", episode_id="e-2"),
    ]
    overall = aggregate(records)["overall"]
    assert overall["n_episodes"] == 2
    assert overall["calls_per_episode"] == pytest.approx(1.5)
    # 3 次调用 × prompt 20 tok + completion 30 tok = 90，两个 episode → 45
    assert overall["tokens_per_episode"] == pytest.approx(45.0)


def test_aggregate_accepts_dicts_and_is_empty_safe():
    assert aggregate([])["overall"]["n_calls"] == 0
    agg = aggregate([_record().as_dict()])
    assert agg["overall"]["n_calls"] == 1


def test_decision_schema_used_by_records_is_the_single_source():
    assert DECISION_SCHEMA["required"] == ["reasoning", "plan"]
