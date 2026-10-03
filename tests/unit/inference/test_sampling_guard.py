"""采样白名单与 eval 收紧（FR-03-07 / B-14；PRD §11 ``test_sampling_guard.py``）。"""

from __future__ import annotations

import asyncio

import pytest

from gamer_rsi.infra.errors import E
from gamer_rsi.inference.backend import Message, validate_sampling
from gamer_rsi.inference.errors import InferenceError
from gamer_rsi.inference.schema import DECISION_SCHEMA

MESSAGES = [Message(role="user", content="phase=player_turn")]


def _run(manager, **kwargs):
    return asyncio.run(manager.generate_json("mock-a", MESSAGES, DECISION_SCHEMA, **kwargs))


@pytest.mark.parametrize("temperature", [0.3, 0.9, 1.4])
def test_eval_rejects_nonzero_temperature(manager_factory, temperature):
    manager, records, backends = manager_factory(mode="eval")
    with pytest.raises(InferenceError) as excinfo:
        _run(manager, temperature=temperature)
    assert excinfo.value.code == E.E_LLM_BACKEND_ERROR
    assert excinfo.value.retryable is False
    # 零埋点、零提交（FR-03-07 / PRD §11）
    assert records == []
    assert backends == {}


def test_eval_requires_explicit_seed(manager_factory):
    from support import inference_config

    manager, records, backends = manager_factory(
        mode="eval", config=inference_config(mode="eval", seed=None))
    with pytest.raises(InferenceError) as excinfo:
        _run(manager, temperature=0.0)
    assert excinfo.value.code == E.E_LLM_BACKEND_ERROR
    assert "seed" in str(excinfo.value.detail)
    assert records == [] and backends == {}


def test_eval_forces_top_p_and_top_k(manager_factory):
    manager, records, _ = manager_factory(mode="eval")
    result = _run(manager, temperature=0.0, top_p=0.9, top_k=40)
    record = result.calls[-1]
    assert record.temperature == 0.0 and record.top_p == 1.0 and record.top_k == 0
    assert record.sampling_seed == 1042 and record.mode == "eval"


@pytest.mark.parametrize("sampling", [
    {"temperature": -0.1}, {"temperature": 1.6}, {"top_p": 0.0}, {"top_p": 1.1},
    {"top_k": -1}, {"top_k": 201}, {"repetition_penalty": 0.99},
    {"repetition_penalty": 1.6}, {"temperature": "hot"}, {"top_k": 1.5},
])
def test_train_whitelist_ranges(manager_factory, sampling):
    manager, records, backends = manager_factory()
    with pytest.raises(InferenceError) as excinfo:
        _run(manager, **sampling)
    assert excinfo.value.code == E.E_LLM_BACKEND_ERROR
    assert records == [] and backends == {}


def test_train_accepts_boundary_values(manager_factory):
    manager, _, _ = manager_factory()
    result = _run(manager, temperature=1.5, top_p=1.0, top_k=200, repetition_penalty=1.5,
                  seed=7)
    record = result.calls[-1]
    assert record.temperature == 1.5 and record.top_k == 200 and record.sampling_seed == 7


def test_validate_sampling_defaults_and_seed_fallback():
    assert validate_sampling({}, mode="train") == {
        "temperature": 1.0, "top_p": 0.95, "top_k": 0, "repetition_penalty": 1.0, "seed": None}
    forced = validate_sampling({"temperature": 0.0, "top_p": 0.3, "top_k": 9, "seed": 5},
                               mode="eval")
    assert forced["top_p"] == 1.0 and forced["top_k"] == 0 and forced["seed"] == 5


def test_manager_falls_back_to_config_seed(manager_factory, monkeypatch):
    manager, _, _ = manager_factory(mode="eval")
    result = _run(manager, temperature=0.0)
    assert result.calls[-1].sampling_seed == 1042


def test_out_of_range_message_is_actionable(manager_factory):
    manager, _, _ = manager_factory()
    with pytest.raises(InferenceError) as excinfo:
        _run(manager, temperature=9.9)
    assert excinfo.value.detail["param"] == "temperature"
    assert "越界" in excinfo.value.message
