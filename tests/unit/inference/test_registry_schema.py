"""注册表 schema 与类型化（G5 / NFR-03-04 / PRD §7）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.infra.io import validate as validate_schema
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.registry import (DEFAULT_REGISTRY_PATH, ModelSpec, build_registry,
                                          load_registry, registry_to_json)
from dice_ratchet.inference.registry import estimate_required_gb

REPO = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO / DEFAULT_REGISTRY_PATH


def test_registry_yaml_round_trips_against_schema():
    """configs/models.yaml 必须满足 schemas/models.schema.json（G5）。"""
    import yaml

    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    validate_schema("models", raw)


def test_shipped_registry_is_typed():
    registry = load_registry(REGISTRY_PATH)
    assert registry.registry_version == 1
    assert registry.ram_budget_gb == 20.0
    assert registry.fallback_backend == "mock"
    # Issue #1：新增 DeepSeek API 模型（外部 proposer / 全量对照臂 B0/B1/C0/C1 用）。
    # 2026-10-08 实测 GET /v1/models：该账号只有 deepseek-flash 与 deepseek-v4-pro。
    assert set(registry.ids()) == {"qwen3-8b-q4", "qwen3-30b-a3b-q4", "mock",
                                   "deepseek-flash", "deepseek-v4-pro"}
    flash = registry.spec("deepseek-flash")
    assert flash.backend == "deepseek" and flash.provider == "api" and flash.online is True
    assert flash.api_model == "deepseek-flash" and flash.api_key_env == "DEEPSEEK_API_KEY"
    assert flash.context_length == 1_048_576 and flash.supports_json_mode is True
    # 成本口径：人民币 + 高峰档上界（官方定价页），金额与币种分开记
    assert flash.cost_registered is True and flash.cost_currency == "CNY"
    cost = flash.estimate_cost(prompt_tokens=1000, completion_tokens=1000)
    assert cost == {"amount": 0.01, "currency": "CNY", "basis": "peak"}
    # 未登记单价的模型 → None（不拿 0 冒充免费）
    assert registry.spec("mock").estimate_cost(prompt_tokens=1, completion_tokens=1) is None

    eight = registry.spec("qwen3-8b-q4")
    assert isinstance(eight, ModelSpec)
    assert eight.backend == "mlx" and eight.supports_grammar is False
    assert eight.supports_json_mode is True and eight.max_inflight == 1
    # Q-6 已关闭（2026-10-04 实测）：权重落地后写入目录级 sha256
    assert eight.weights_sha256 == (
        "20303070eb19e8a47a04f828d9239e9c1697392f183408535b5ab51fe9fe4b49")
    # 2026-10-04 实测回填：原预算 1500/3000/25 未达成，按实测 + 余量重设
    assert eight.default_sampling["temperature"] == 0.7
    assert eight.perf_budget["p95_latency_ms"] == 6000.0
    assert eight.perf_budget["p50_latency_ms"] == 5000.0
    assert eight.perf_budget["min_tok_per_s"] == 10.0

    big = registry.spec("qwen3-30b-a3b-q4")
    assert big.backend == "llama_server" and big.supports_grammar is True
    assert "--parallel" in big.server_args and "--host" in big.server_args

    mock = registry.spec("mock")
    assert mock.backend == "mock" and mock.weights_path == ""


def test_registry_budget_formula_and_30b_ceiling():
    registry = load_registry(REGISTRY_PATH)
    eight = registry.spec("qwen3-8b-q4")
    # resident_gb_est 已由实测回填 5.6 → 4.6077（M1 验收 §12），预算仍远低于 20 GB
    assert eight.resident_gb_est == pytest.approx(4.6077)
    assert estimate_required_gb(eight, max_inflight=1, context_length=8192) == pytest.approx(
        4.6077 + 0.15 * 8.192 + 1.0)
    big = registry.spec("qwen3-30b-a3b-q4")
    # PRD §7：8192/1 临界（20.21 > 20.0）；32768/4 明确拒绝
    assert estimate_required_gb(big, max_inflight=1, context_length=8192) > 20.0
    assert estimate_required_gb(big, max_inflight=4, context_length=32768) > 25.0


def test_unknown_model_id_raises_unavailable():
    registry = load_registry(REGISTRY_PATH)
    with pytest.raises(InferenceError) as excinfo:
        registry.spec("nope")
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "qwen3-8b-q4" in (excinfo.value.hint or "")


@pytest.mark.parametrize("bad", [
    {"registry_version": 1, "models": {"m": {"backend": "unknown", "context_length": 1024}}},
    {"registry_version": 1, "models": {"m": {"backend": "mock"}}}, # 缺 context_length
    {"registry_version": 1, "models": {"m": {"backend": "mock", "context_length": 1024,
                                             "unexpected": 1}}},
    {"registry_version": 2, "models": {"m": {"backend": "mock", "context_length": 1024}}},
    {"registry_version": 1, "models": {}},
    {"registry_version": 1, "models": {"m": {"backend": "mock", "context_length": 1024,
                                             "kv_cache_bits": 3}}},
])
def test_schema_violations_raise_unavailable(bad):
    with pytest.raises(InferenceError) as excinfo:
        build_registry(bad)
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "schema" in excinfo.value.message


def test_defaults_are_inherited_by_models():
    registry = build_registry({
        "registry_version": 1,
        "defaults": {"context_length": 2048, "kv_cache_bits": 4, "max_inflight": 2,
                     "quantization": "4bit", "supports_grammar": True,
                     "supports_json_mode": True, "kv_gb_per_1k_tokens": 0.1},
        "models": {"m": {"backend": "mock", "context_length": 2048}},
    })
    spec = registry.spec("m")
    assert spec.context_length == 2048 and spec.kv_cache_bits == 4
    assert spec.max_inflight == 2 and spec.quantization == "4bit"
    assert spec.supports_grammar is True and spec.kv_gb_per_1k_tokens == 0.1


def test_missing_registry_file_raises_unavailable(tmp_path):
    with pytest.raises(InferenceError) as excinfo:
        load_registry(tmp_path / "nope.yaml")
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "不存在" in excinfo.value.message


def test_registry_to_json_is_stable():
    registry = load_registry(REGISTRY_PATH)
    assert registry_to_json(registry) == registry_to_json(load_registry(REGISTRY_PATH))
