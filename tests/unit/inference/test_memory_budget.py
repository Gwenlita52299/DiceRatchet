"""内存预算前置拒绝（FR-03-08 / PRD §7 公式 / §11 ``test_memory_budget.py``）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Mapping

import pytest

from dice_ratchet.infra.config import InferenceConfig
from dice_ratchet.infra.errors import E
from dice_ratchet.inference.backend import BackendManager, Message
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.registry import build_registry
from dice_ratchet.inference.schema import DECISION_SCHEMA

GOOD = '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}'
MESSAGES = [Message(role="user", content="phase=player_turn")]

#: 30B-A3B 的注册表参数（PRD §7：32768/4 必超预算）。
BIG_30B = dict(model_id="big-30b", resident_gb_est=18.8, kv_gb_per_1k_tokens=0.05,
               context_length=8192, max_inflight=1)


def inference_config(**overrides: Any) -> InferenceConfig:
    return replace(InferenceConfig(), **overrides)


def _registry_data(**overrides: Any) -> dict[str, Any]:
    params = {**BIG_30B, **overrides}
    model_id = str(params.pop("model_id"))
    ram_budget_gb = float(params.pop("ram_budget_gb", 20.0))
    entry: Mapping[str, Any] = {
        "backend": "mock", "weights_path": None, "quantization": "none",
        "weights_sha256": None, "kv_cache_bits": 8, "supports_grammar": True,
        "supports_json_mode": True, "default_sampling": {"temperature": 0.0, "top_p": 1.0},
        **params,
    }
    return {"registry_version": 1, "ram_budget_gb": ram_budget_gb,
            "models": {model_id: entry}}


def _registry(**overrides: Any):
    return build_registry(_registry_data(**overrides))


def test_estimate_required_gb_matches_prd_formula():
    manager = BackendManager(registry=_registry(), config=inference_config())
    required = manager.estimate_required_gb("big-30b", max_inflight=4, context_length=32768)
    assert required == pytest.approx(18.8 + 0.05 * 32.768 * 4 + 1.0)
    assert required > 20.0
    assert manager.estimate_required_gb("big-30b") == pytest.approx(
        18.8 + 0.05 * 8.192 * 1 + 1.0)


def test_generate_json_rejects_before_any_backend_work():
    created: list[str] = []

    def factory(spec): # pragma: no cover - 不应被调用
        created.append(spec.model_id)
        raise AssertionError("预算拒绝后不得构造/加载后端（零提交）")

    manager = BackendManager(registry=_registry(), config=inference_config(),
                             backend_factory=factory)
    manager.ram_budget_gb = 20.0 # 固定预算，避免配置默认差异
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(manager.generate_json("big-30b", MESSAGES, DECISION_SCHEMA,
                                          max_inflight=1))
    assert created == []
    # 默认 max_inflight=1 且 ctx=8192 → 20.21 > 20.0：临界超限（PRD §7 注）
    assert excinfo.value.code == E.E_LLM_OOM
    assert excinfo.value.detail["required_gb"] > 20.0
    assert excinfo.value.retryable is True


def test_load_also_rejects_over_budget():
    manager = BackendManager(registry=_registry(), config=inference_config(ram_budget_gb=10.0))
    with pytest.raises(InferenceError) as excinfo:
        asyncio.run(manager.load("big-30b"))
    assert excinfo.value.code == E.E_LLM_OOM
    assert excinfo.value.detail["ram_budget_gb"] == 10.0


def test_registry_budget_used_without_explicit_config():
    manager = BackendManager(registry=_registry(ram_budget_gb=5.0))
    assert manager.ram_budget_gb == 5.0
    with pytest.raises(InferenceError):
        asyncio.run(manager.load("big-30b"))


def test_budget_boundary_is_inclusive(manager_factory):
    """恰好等于预算（浮点边界）不应被拒。"""
    manager, _, _ = manager_factory([GOOD], resident_gb_est=0.0, kv_gb_per_1k_tokens=0.0,
                                    context_length=1000)
    manager.ram_budget_gb = 1.0
    required = manager.estimate_required_gb("mock-a")
    assert required == pytest.approx(1.0)
    asyncio.run(manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA))


def test_budget_rejection_emits_llm_oom_event():
    events: list[tuple[str, dict]] = []

    class Recorder:
        def emit(self, event, **fields):
            events.append((event, fields))

        def now_ms(self):
            return 0.0

        def run_id(self):
            return None

        def dump_raw(self, call_id, text):
            return None

    manager = BackendManager(registry=_registry(ram_budget_gb=5.0),
                             config=inference_config(), infra=Recorder())
    with pytest.raises(InferenceError):
        asyncio.run(manager.load("big-30b"))
    assert any(name == "llm.oom" and fields.get("code") == E.E_LLM_OOM
               for name, fields in events)
