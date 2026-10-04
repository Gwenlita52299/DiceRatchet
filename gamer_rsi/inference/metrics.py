"""埋点聚合与格式鲁棒性报告（PRD §13 M1 交付物）。

输入是每次尝试一条的 ``InferenceCallRecord``；输出 ``metrics/inference_agg.json`` /
``metrics/format_robustness.json`` 的内容（落盘动作在 ``cli.py``，对象构造在这里）。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Final, Mapping, Sequence

from ..infra.errors import E
from .records import InferenceCallRecord

__all__ = ["percentile", "aggregate", "robustness_report", "SCHEMA_VERSION"]

SCHEMA_VERSION: Final[str] = "inference-agg-1.0.0"

#: M1 验收阈值（PRD §1 / §11）。
FIRST_PASS_MIN: Final[float] = 0.90
SCHEMA_FAIL_MAX: Final[float] = 0.01

#: §13 口径：``outcome_kind="error"`` 视为失败尝试。
_VALID_OUTCOMES: Final[frozenset[str]] = frozenset({"action", "degraded"})


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（``q ∈ [0,1]``）；空序列返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _as_dict(record: InferenceCallRecord | Mapping[str, Any]) -> dict[str, Any]:
    return record.as_dict() if isinstance(record, InferenceCallRecord) else dict(record)


def _group_calls(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按 ``call_id`` 分组（一次调用的多次尝试聚在一起）。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[str(row.get("call_id") or f"__row{index}")].append(row)
    return dict(groups)


def _call_summary(attempts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """一次调用的最终结论（取最后一次尝试的 outcome/error_code）。"""
    last = attempts[-1]
    attempts_count = int(last.get("attempts") or len(attempts))
    return {
        "model_id": str(last.get("model_id")),
        "purpose": str(last.get("purpose")),
        "episode_id": last.get("episode_id"),
        "attempts": attempts_count,
        "outcome_kind": str(last.get("outcome_kind")),
        "error_code": last.get("error_code"),
        "degraded_reason": last.get("degraded_reason"),
        "latency_ms": sum(int(a.get("latency_ms") or 0) for a in attempts),
        "prompt_tokens": sum(int(a.get("prompt_tokens") or 0) for a in attempts),
        "completion_tokens": sum(int(a.get("completion_tokens") or 0) for a in attempts),
        "decode_ms": sum(int(a.get("decode_ms") or 0) for a in attempts),
    }


def aggregate(records: Sequence[InferenceCallRecord | Mapping[str, Any]]) -> dict[str, Any]:
    """§13 聚合指标：延迟分位、tok/s、首答通过率、重试直方图、错误率。"""
    rows = [_as_dict(r) for r in records]
    groups = _group_calls(rows)
    calls = [_call_summary(attempts) for attempts in groups.values()]

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        by_key[(call["model_id"], call["purpose"])].append(call)

    def _metrics(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        latencies = [c["latency_ms"] for c in subset]
        valid_first = [c for c in subset
                       if c["attempts"] == 1 and c["outcome_kind"] in _VALID_OUTCOMES]
        action_first = [c for c in subset if c["attempts"] == 1 and c["outcome_kind"] == "action"]
        schema_fail = [c for c in subset if c["error_code"] == str(E.E_LLM_SCHEMA_INVALID)]
        degraded = [c for c in subset if c["outcome_kind"] == "degraded"]
        errors = Counter(str(c["error_code"]) for c in subset if c["error_code"])
        retry_histogram = Counter(int(c["attempts"]) for c in subset)
        decode_ms = sum(c["decode_ms"] for c in subset)
        completion = sum(c["completion_tokens"] for c in subset)
        total = len(subset)
        return {
            "n_calls": total,
            "latency_ms": {"p50": round(percentile(latencies, 0.50), 4),
                           "p95": round(percentile(latencies, 0.95), 4),
                           "max": round(max(latencies), 4) if latencies else 0.0},
            "tok_per_s": round(completion / decode_ms * 1000.0, 4) if decode_ms else None,
            "first_pass_valid_rate": round(len(valid_first) / total, 4) if total else 0.0,
            # §13 逐字口径（仅 action）；MLX 阶段一恒 degraded，故同时给出上一条
            "first_pass_action_rate": round(len(action_first) / total, 4) if total else 0.0,
            "schema_fail_rate": round(len(schema_fail) / total, 4) if total else 0.0,
            "degraded_call_rate": round(len(degraded) / total, 4) if total else 0.0,
            "retry_histogram": {str(k): v for k, v in sorted(retry_histogram.items())},
            "error_rate_by_code": {k: round(v / total, 4) for k, v in sorted(errors.items())},
        }

    overall = _metrics(calls)
    episodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        if call["episode_id"]:
            episodes[str(call["episode_id"])].append(call)
    if episodes:
        overall["n_episodes"] = len(episodes)
        overall["calls_per_episode"] = round(len(calls) / len(episodes), 4)
        overall["tokens_per_episode"] = round(
            sum(c["prompt_tokens"] + c["completion_tokens"] for c in calls)
            / len(episodes), 4)

    return {
        "schema_version": SCHEMA_VERSION,
        "n_attempt_records": len(rows),
        "overall": overall,
        "by_model_purpose": {
            f"{model_id}|{purpose}": _metrics(subset)
            for (model_id, purpose), subset in sorted(by_key.items())
        },
    }


def robustness_report(records: Sequence[InferenceCallRecord | Mapping[str, Any]], *,
                      model_id: str, backend: str, mode: str, n_prompts: int,
                      prompt_source: str, thresholds: Mapping[str, float] | None = None,
                      errors: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """M1 交付物 ``metrics/format_robustness.json``（PRD §1 目标①②）。"""
    agg = aggregate(records)
    overall = agg["overall"]
    limits = {"first_pass_valid_rate_min": FIRST_PASS_MIN,
              "schema_fail_rate_max": SCHEMA_FAIL_MAX}
    if thresholds:
        limits.update({k: float(v) for k, v in thresholds.items()})
    passed = (overall["first_pass_valid_rate"] >= limits["first_pass_valid_rate_min"]
              and overall["schema_fail_rate"] <= limits["schema_fail_rate_max"])
    # 溯源：M1 交付物要求的是**真实模型 + 真实固定 prompt**（PRD §11/§12）。这里显式
    # 标注「这份报告能不能当 M1 证据用」，避免 mock / 合成 prompt 的产物被误读成真实测量
    # （M1 验收 F2）。
    real_model = str(backend) not in ("mock", "fake")
    real_prompts = str(prompt_source).startswith("file:") and "synthetic" not in str(prompt_source)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "format_robustness",
        "model_id": model_id,
        "backend": backend,
        "mode": mode,
        "n_prompts": int(n_prompts),
        "prompt_source": prompt_source,
        "thresholds": limits,
        "passed": bool(passed),
        "real_model": real_model,
        "real_prompts": real_prompts,
        #: 只有「真实后端 + 真实 prompt 快照 + 阈值通过」才能作为 M1 验收证据入档。
        "m1_evidence": bool(real_model and real_prompts and passed),
        "overall": overall,
        "by_model_purpose": agg["by_model_purpose"],
        "errors": [dict(e) for e in errors],
    }
