"""MOD-03 集成测试共享常量与构造器（独立模块名，避免 ``conftest`` 同名冲突）。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from dice_ratchet.infra.config import InferenceConfig
from dice_ratchet.inference.backend import Backend, BackendManager, MockBackend
from dice_ratchet.inference.records import InferenceCallRecord
from dice_ratchet.inference.registry import ModelRegistry, build_registry

GOOD = '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}'
GOOD2 = '{"reasoning": "alt", "plan": [{"kind": "play_face", "dieId": "d1", "slotId": "s1"}]}'
BAD_JSON = '{"reasoning": "ok", "plan": ['
BAD_KIND = '{"reasoning": "ok", "plan": [{"kind": "teleport"}]}'

MODEL_ID = "it-model"


def model_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "backend": "mock", "weights_path": None, "quantization": "none",
        "context_length": 4096, "kv_cache_bits": 8, "max_inflight": 1,
        "supports_grammar": True, "supports_json_mode": True,
        "resident_gb_est": 0.0, "kv_gb_per_1k_tokens": 0.0,
        "default_sampling": {"temperature": 0.0, "top_p": 1.0, "top_k": 0,
                             "repetition_penalty": 1.0},
    }
    entry.update(overrides)
    return entry


def registry_of(entry: Mapping[str, Any] | None = None, *,
                ram_budget_gb: float = 20.0) -> ModelRegistry:
    return build_registry({"registry_version": 1, "ram_budget_gb": ram_budget_gb,
                           "models": {MODEL_ID: dict(entry or model_entry())}})


def inference_config(**overrides: Any) -> InferenceConfig:
    return replace(InferenceConfig(), **overrides)


class FakeClock:
    def __init__(self, start_ms: float = 1_000.0) -> None:
        self.now_ms = float(start_ms)
        self.sleeps: list[int] = []

    def now(self) -> float:
        return self.now_ms

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds * 1000))
        self.now_ms += seconds * 1000.0


def mock_factory(script: Sequence[Any], *, delay_ms: int = 0):
    """返回 ``(factory, created)`` —— 便于断言后端调用次数与并发峰值。"""
    created: dict[str, MockBackend] = {}
    texts = [script] if isinstance(script, str) else list(script)

    def factory(spec) -> Backend:
        backend = MockBackend(texts, delay_ms=delay_ms, model_id=spec.model_id)
        created[spec.model_id] = backend
        return backend

    return factory, created


def build_manager(registry: ModelRegistry, *, factory: Callable[[Any], Backend] | None = None,
                  mode: str = "train", config: InferenceConfig | None = None,
                  clock: FakeClock | None = None,
                  sink: Callable[[InferenceCallRecord], None] | None = None) -> BackendManager:
    cfg = config if config is not None else inference_config(
        mode=mode, seed=(1042 if mode == "eval" else None))
    return BackendManager(registry=registry, mode=mode, config=cfg, backend_factory=factory,
                          clock=clock.now if clock else None,
                          sleep=clock.sleep if clock else None, record_sink=sink)
