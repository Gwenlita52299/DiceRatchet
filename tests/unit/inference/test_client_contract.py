"""统一同步客户端契约与跨模块接线（Issue #1 §2；附带修复两处真实接线缺陷）。

本文件的两组用例都是**回归护栏**，对应本轮发现的两个既有缺陷：

1. ``BackendManager`` 曾把 ``mode`` 写死在自己的构造参数上：组合根一律 ``mode="eval"``，
   于是 MOD-04 在**训练局**（探索温度 0.7–1.2）调用模型时被 ``validate_sampling(mode="eval")``
   判为越界 → 每个决策点都抛 ``E_LLM_BACKEND_ERROR`` → PolicyAgent 静默降级为启发式
   （``degraded=True``，该局不计入 n）。修法：``mode`` 可按调用覆盖，
   ``BackendInferenceClient`` 把调用方声明的 train/eval 口径带进 MOD-03。

2. MOD-07 的 ``_call_generate_json`` 用的是 ``generate_json(messages, schema, seed=…,
   temperature=0.0)``，而真实的 ``BackendInferenceClient`` 只接受
   ``(messages, schema, *, mode, sampling, seed)`` → 提案路径在真实客户端下必然
   ``TypeError``（单测的宽松 ``**kwargs`` 假件掩盖了它）。修法：按契约调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dice_ratchet.infra.config import InferenceConfig
from dice_ratchet.infra.errors import E
from dice_ratchet.inference.backend import BackendManager, Message
from dice_ratchet.inference.client import BackendInferenceClient, coerce_client
from dice_ratchet.inference.errors import InferenceError
from support import GOOD, decision_json, inference_config, registry_data

from dice_ratchet.inference.registry import build_registry

MESSAGES = [Message(role="system", content="s"), Message(role="user", content="u")]
SCHEMA: dict[str, Any] = {"type": "object", "required": ["reasoning", "plan"]}


def _manager(*texts: str, mode: str = "eval", **config_overrides: Any) -> BackendManager:
    from dice_ratchet.inference.backend import MockBackend

    registry = build_registry(registry_data(model_id="mock-a"))
    cfg = inference_config(mode=mode, seed=(1042 if mode == "eval" else None),
                          **config_overrides)
    script = list(texts) or [GOOD]
    return BackendManager(registry=registry, mode=mode, config=cfg,
                          backend_factory=lambda spec: MockBackend(script,
                                                                  model_id=spec.model_id))


def test_sync_client_forwards_per_call_mode() -> None:
    # 同一个 eval-mode manager：调用方声明 train + 0.7 → 放行（训练探索温度）
    client = BackendInferenceClient(_manager(GOOD), model_id="mock-a")
    result = client.generate_json(MESSAGES, SCHEMA, mode="train",
                                  sampling={"temperature": 0.7, "max_tokens": 32}, seed=7)
    assert result.text == GOOD


def test_sync_client_keeps_eval_hardening() -> None:
    # 调用方声明 eval 却没把温度压到 0 → 仍然拒绝（B-14 不因重构而放松）
    client = BackendInferenceClient(_manager(GOOD), model_id="mock-a")
    with pytest.raises(InferenceError) as exc:
        client.generate_json(MESSAGES, SCHEMA, mode="eval",
                             sampling={"temperature": 0.7}, seed=7)
    assert exc.value.code == E.E_LLM_BACKEND_ERROR
    assert "temperature" in exc.value.message


def test_manager_default_mode_still_applies_without_explicit_mode() -> None:
    # 不传 mode 的旧调用口径不变：manager.mode=eval 仍要求 temperature=0.0
    manager = _manager(GOOD, mode="eval")
    import asyncio

    with pytest.raises(InferenceError) as exc:
        asyncio.run(manager.generate_json("mock-a", MESSAGES, SCHEMA, temperature=0.7))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR


def test_train_mode_manager_accepts_exploration_temperature() -> None:
    # 反向用例：train manager + train 口径 → 0.7 可用（训练局不再整体降级）
    manager = _manager(decision_json("end_turn"), mode="train")
    client = BackendInferenceClient(manager, model_id="mock-a")
    result = client.generate_json(MESSAGES, SCHEMA, mode="train",
                                  sampling={"temperature": 0.9}, seed=11)
    assert result.parsed is not None


def test_proposer_calls_unified_contract_with_real_client() -> None:
    """MOD-07 的提案路径必须能用真实（非宽松假件）的同步客户端跑通。"""
    from dice_ratchet.evolve.proposer import _call_generate_json

    client = BackendInferenceClient(
        _manager(decision_json("end_turn")), model_id="mock-a")
    # 真实客户端：按契约调用成功；旧的 loose 形状会 TypeError
    result = _call_generate_json(client, system="s", user="u",
                                 schema={"type": "object"}, seed=3)
    assert result["reasoning"] == "ok" # _extract_json 已取到 parsed payload
    assert result["plan"] == [{"kind": "end_turn"}]


def test_loose_stub_clients_still_work() -> None:
    """接受 ``**kwargs`` 的假件（测试与自研后端）仍能按契约被调用。"""
    from dice_ratchet.evolve.proposer import _call_generate_json

    class Stub:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        def generate_json(self, messages: Any, schema: Any, **kwargs: Any) -> Any:
            self.kwargs = dict(kwargs)
            return '{"ok": true}'

    stub = Stub()
    assert _call_generate_json(stub, system="s", user="u", schema={}, seed=5) == '{"ok": true}'
    assert stub.kwargs["mode"] == "eval"
    assert stub.kwargs["sampling"] == {"temperature": 0.0}
    assert stub.kwargs["seed"] == 5


def test_coerce_client_is_role_aware_and_backward_compatible() -> None:
    from dice_ratchet.inference.roles import RoleModelProvider

    cfg = __import__("dice_ratchet.infra.config", fromlist=["load_config"]).load_config(
        config_dir=Path(__file__).resolve().parents[3] / "configs")
    provider = RoleModelProvider(cfg, run_id="r-contract", runs_root="/tmp/dr-contract")
    assert coerce_client(provider, role="reflection").model_id == "qwen3-8b-q4"

    class Plain:
        model_id = "plain-1"

    assert coerce_client(Plain()) is not None # 非角色对象原样返回
    assert coerce_client(None) is None


def test_inference_config_mode_is_not_hardcoded_in_root() -> None:
    """组合根的 model 装配不再把 "eval" 写死成唯一口径（防回归的文本级断言）。"""
    import inspect

    from dice_ratchet import app as app_mod

    source = inspect.getsource(app_mod._role_provider)
    # 组合根仍以 eval 为**默认**（基线/play 都是测量场景），但 per-call mode 由调用方决定
    assert 'mode="eval"' in source
    assert "kwargs[\"mode\"]" in Path(
        Path(app_mod.__file__).parent / "inference" / "client.py").read_text(encoding="utf-8")
