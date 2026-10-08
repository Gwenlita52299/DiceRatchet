"""MOD-03 单测共享常量与构造器（独立模块名，避免 ``conftest`` 同名冲突）。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping, Sequence

from dice_ratchet.infra.config import InferenceConfig
from dice_ratchet.inference.backend import MockBackend
from dice_ratchet.inference.records import InferenceCallRecord
from dice_ratchet.inference.registry import ModelRegistry, build_registry

# ---- 固定脚本文本（合法/非法 DECISION_SCHEMA）--------------------------------------
GOOD = '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}'
GOOD2 = '{"reasoning": "ok2", "plan": [{"kind": "play_face", "dieId": "d1", "slotId": "s1"}]}'
BAD_JSON = '{"reasoning": "ok", "plan": ['
BAD_KIND = '{"reasoning": "ok", "plan": [{"kind": "teleport"}]}'
BAD_EMPTY_PLAN = '{"reasoning": "ok", "plan": []}'
BAD_EXTRA = '{"reasoning": "ok", "plan": [{"kind": "end_turn", "nope": 1}]}'
BAD_RANGE = '{"reasoning": "ok", "plan": [{"kind": "play_face", "faceIndex": 99}]}'

DEFAULT_SAMPLING: Mapping[str, Any] = {
    "temperature": 0.0, "top_p": 1.0, "top_k": 0, "repetition_penalty": 1.0,
}


class FakeClock:
    """假时钟：``sleep`` 立即推进 ``now`` 并记录退避时长（观测 [200, 400] ms）。"""

    def __init__(self, start_ms: float = 1_000.0) -> None:
        self.now_ms = float(start_ms)
        self.sleeps: list[int] = []

    def now(self) -> float:
        return self.now_ms

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(float(seconds) * 1000))
        self.now_ms += float(seconds) * 1000.0


def inference_config(**overrides: Any) -> InferenceConfig:
    return replace(InferenceConfig(), **overrides)


def registry_data(*, model_id: str = "mock-a", backend: str = "mock",
                  supports_grammar: bool = True, context_length: int = 4096,
                  max_inflight: int = 1, resident_gb_est: float = 0.0,
                  kv_gb_per_1k_tokens: float = 0.0, ram_budget_gb: float = 20.0,
                  fallback_backend: str | None = "mock",
                  extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造满足 ``schemas/models.schema.json`` 的单模型注册表。"""
    entry: dict[str, Any] = {
        "backend": backend, "weights_path": None, "quantization": "none",
        "context_length": context_length, "kv_cache_bits": 8, "max_inflight": max_inflight,
        "supports_grammar": supports_grammar, "supports_json_mode": True,
        "resident_gb_est": resident_gb_est, "kv_gb_per_1k_tokens": kv_gb_per_1k_tokens,
        "default_sampling": dict(DEFAULT_SAMPLING),
    }
    if extra:
        entry.update(extra)
    return {"registry_version": 1, "ram_budget_gb": ram_budget_gb,
            "fallback_backend": fallback_backend, "models": {model_id: entry}}


def build(**kwargs: Any) -> ModelRegistry:
    return build_registry(registry_data(**kwargs))


def make_mock_manager(registry: ModelRegistry, script: Sequence[Any], *, mode: str = "train",
                      config: InferenceConfig | None = None, delay_ms: int = 0,
                      clock: FakeClock | None = None,
                      records: list[InferenceCallRecord] | None = None):
    """``(manager, records, created)`` —— 供不依赖 fixture 的用例直接构造。"""
    from dice_ratchet.inference.backend import BackendManager

    sink: list[InferenceCallRecord] = records if records is not None else []
    created: dict[str, MockBackend] = {}
    texts = [script] if isinstance(script, str) else list(script)

    def factory(spec):
        backend = MockBackend(texts, delay_ms=delay_ms, model_id=spec.model_id)
        created[spec.model_id] = backend
        return backend

    cfg = config if config is not None else inference_config(
        mode=mode, seed=(1042 if mode == "eval" else None))
    manager = BackendManager(registry=registry, mode=mode, config=cfg, backend_factory=factory,
                             clock=clock.now if clock else None,
                             sleep=clock.sleep if clock else None, record_sink=sink.append)
    return manager, sink, created


def decision_json(kind: str = "end_turn", **fields: Any) -> str:
    action = {"kind": kind, **fields}
    return json.dumps({"reasoning": "ok", "plan": [action]}, ensure_ascii=False)
