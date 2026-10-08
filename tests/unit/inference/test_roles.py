"""模型角色解析与角色提供者（Issue #1 §2 / 验收标准 A）。

覆盖：

- 角色绑定优先级：``experiment.roles`` > ``experiment.arms[arm]`` > ``models`` >
  ``inference.model_id``（``--model`` 的既有语义因此不破）；
- 六个实验臂 A0/A1/B0/B1/C0/C1 各自解析出的三角色 model_id（与 Issue #1 §1 的表一致）；
- 换臂 → 换 ``config_hash``（run-id 族隔离，F10）；
- fail-closed：未知臂 / 未知角色 / 空 model_id / 注册表里没有的模型 / API 模型没登记
  ``api_key_env``；
- run provenance：三角色各自的 provider/backend/model_revision/online 可还原，
  且**绝不**出现 key 本体（只有变量名与「是否已设置」）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dice_ratchet.infra.config import (EXPERIMENT_ARMS, MODEL_ROLES, AppConfig,
                                      ExperimentConfig, ModelRolesConfig, config_hash,
                                      load_config)
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.roles import (PROVENANCE_FILENAME, ROLE_NAMES, RoleModelProvider,
                                         resolve_role_bindings, role_model_ids,
                                         run_provenance)
from support import registry_data

from dice_ratchet.inference.registry import build_registry

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs"

#: Issue #1 §1 的六臂表（决策模型 / proposer 模型）。
EXPECTED_ARMS: dict[str, tuple[str, str, str]] = {
    "A0": ("qwen3-8b-q4", "qwen3-8b-q4", "qwen3-8b-q4"),
    "A1": ("qwen3-8b-q4", "qwen3-8b-q4", "qwen3-8b-q4"),
    "B0": ("qwen3-8b-q4", "qwen3-8b-q4", "deepseek-chat"),
    "B1": ("qwen3-8b-q4", "qwen3-8b-q4", "deepseek-chat"),
    "C0": ("deepseek-chat", "deepseek-chat", "deepseek-chat"),
    "C1": ("deepseek-chat", "deepseek-chat", "deepseek-chat"),
}


@pytest.fixture
def cfg():
    return load_config(config_dir=CONFIGS)


def _with_arm(cfg, arm: str):
    return replace(cfg, experiment=replace(cfg.experiment, arm=arm))


# ---- 角色绑定与优先级 ----------------------------------------------------------------

def test_role_names_and_config_defaults_agree():
    # 角色名只有三个，且配置段与代码常量一一对应（防止两处漂移）
    assert ROLE_NAMES == MODEL_ROLES == ("policy", "reflection", "proposer")
    roles = ModelRolesConfig()
    for role in MODEL_ROLES:
        assert getattr(roles, role) == "" # 空 = 继承 inference.model_id


def test_default_yaml_arms_match_code_defaults(cfg):
    # configs/default.yaml 的 experiment.arms 必须与 dataclass 默认值一致（防漂移）
    assert cfg.experiment.arms == ExperimentConfig().arms
    assert cfg.experiment.arm == "A1"
    assert set(cfg.experiment.arms) == set(EXPERIMENT_ARMS)


@pytest.mark.parametrize("arm", sorted(EXPECTED_ARMS))
def test_six_arms_resolve_to_issue_table(cfg, arm):
    ids = role_model_ids(_with_arm(cfg, arm))
    assert (ids["policy"], ids["reflection"], ids["proposer"]) == EXPECTED_ARMS[arm]


def test_each_arm_has_distinct_config_hash(cfg):
    # 换臂 = 换 run-id 族（F10）：六个臂 + "不选臂" 的 hash 两两不同
    hashes = {arm: config_hash(_with_arm(cfg, arm)) for arm in (*EXPERIMENT_ARMS, "")}
    assert len(set(hashes.values())) == len(hashes)


def test_roles_override_beats_arm_and_models(cfg):
    # experiment.roles 优先级最高（Issue #1 的第二种配置形态）
    cfg2 = replace(cfg, experiment=replace(cfg.experiment, arm="A0",
                                           roles={"proposer": "mock"}))
    ids = role_model_ids(cfg2)
    assert ids["proposer"] == "mock"
    assert ids["policy"] == "qwen3-8b-q4" # 其余角色不受影响


def test_models_block_beats_inference_model_id(cfg):
    # 显式角色绑定优先于 inference.model_id（--model 写的就是后者）
    cfg2 = replace(cfg, models=ModelRolesConfig(proposer="mock"))
    assert role_model_ids(cfg2)["proposer"] == "mock"
    # policy 未显式绑定 → 仍继承 inference.model_id
    assert role_model_ids(cfg2)["policy"] == cfg.inference.model_id


def test_cli_model_semantics_preserved(cfg):
    # `--model mock`（= inference.model_id 覆盖）在 A 臂下仍一次覆盖全部角色
    cfg2 = replace(cfg, inference=replace(cfg.inference, model_id="mock"))
    ids = role_model_ids(_with_arm(cfg2, "A1"))
    assert set(ids.values()) == {"mock"}


def test_binding_source_is_traceable(cfg):
    bindings = resolve_role_bindings(_with_arm(cfg, "B1"))
    assert bindings["policy"].source == "inference.model_id"
    assert bindings["proposer"].source == "experiment.arms.B1"
    bindings2 = resolve_role_bindings(
        replace(cfg, experiment=replace(cfg.experiment, arm="A0",
                                        roles={"policy": "mock"})))
    assert bindings2["policy"].source == "experiment.roles"


# ---- fail-closed ---------------------------------------------------------------------

def test_unknown_arm_fails_closed(cfg):
    with pytest.raises(InfraError) as exc:
        role_model_ids(_with_arm(cfg, "Z9"))
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    assert "Z9" in exc.value.message


def test_unknown_model_id_fails_closed(cfg):
    cfg2 = replace(cfg, models=ModelRolesConfig(policy="no-such-model"))
    with pytest.raises(InferenceError) as exc:
        resolve_role_bindings(cfg2)
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert "no-such-model" in exc.value.message


def test_empty_role_binding_fails_closed(cfg):
    cfg2 = replace(cfg, inference=replace(cfg.inference, model_id=""))
    with pytest.raises(InferenceError) as exc:
        resolve_role_bindings(cfg2)
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert "model_id" in exc.value.message


def test_api_model_without_key_env_fails_closed(cfg):
    # provider=api 但注册表没登记 api_key_env → 装配期就报错，不允许"以后再说"
    registry = build_registry({
        "registry_version": 1, "ram_budget_gb": 20.0, "fallback_backend": "mock",
        "models": {"api-x": dict(
            registry_data(model_id="api-x", backend="mock")["models"]["api-x"],
            backend="deepseek", provider="api", online=True, api_key_env="",
            api_base="https://example.invalid/v1", api_model="api-x")}})
    cfg2 = replace(cfg, models=ModelRolesConfig(policy="api-x", reflection="api-x",
                                               proposer="api-x"))
    with pytest.raises(InferenceError) as exc:
        resolve_role_bindings(cfg2, registry=registry)
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert "api_key_env" in exc.value.message


def test_provider_unknown_role_fails_closed(cfg):
    provider = RoleModelProvider(cfg, run_id="r-test", runs_root="/tmp/dr-roles")
    with pytest.raises(InferenceError) as exc:
        provider.client_for("judge")
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert "judge" in exc.value.message


def test_bare_appconfig_defaults_are_valid():
    # 不读 YAML 也能用：代码默认值自成合法配置（arm=A1 的臂表在 dataclass 默认里）
    AppConfig().validate()
    assert set(AppConfig().experiment.arms) == set(EXPERIMENT_ARMS)


# ---- provenance ----------------------------------------------------------------------

def test_provenance_restores_three_roles(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    provider = RoleModelProvider(_with_arm(cfg, "B1"), run_id="r-prov",
                                 runs_root=tmp_path)
    path = provider.write_provenance()
    assert path == tmp_path / "r-prov" / PROVENANCE_FILENAME
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["arm"] == "B1" and payload["harness_mode"] == "evolve"
    assert payload["config_hash"] == config_hash(_with_arm(cfg, "B1"))
    roles = payload["roles"]
    assert set(roles) == set(ROLE_NAMES)
    assert roles["policy"]["model_id"] == "qwen3-8b-q4"
    assert roles["policy"]["provider"] == "local" and roles["policy"]["online"] is False
    assert roles["proposer"]["model_id"] == "deepseek-chat"
    assert roles["proposer"]["provider"] == "api"
    assert roles["proposer"]["backend"] == "deepseek"
    assert roles["proposer"]["online"] is True
    assert roles["proposer"]["api_model"] == "deepseek-chat"
    assert roles["proposer"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert roles["proposer"]["api_key_present"] is True
    # 绝不落盘 key 本体
    assert "sk-test-not-a-real-key" not in Path(path).read_text(encoding="utf-8")


def test_provenance_records_api_key_absent(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    payload = run_provenance(_with_arm(cfg, "C1"), resolve_role_bindings(_with_arm(cfg, "C1")),
                             run_id="r-prov2")
    assert payload["roles"]["policy"]["api_key_present"] is False
    assert payload["api_mode"] == "live"


def test_harness_mode_reflects_arm(cfg):
    assert _with_arm(cfg, "A1").experiment.harness_mode() == "evolve"
    assert _with_arm(cfg, "A0").experiment.harness_mode() == "fixed"
    assert _with_arm(cfg, "B0").experiment.harness_mode() == "fixed"
    assert _with_arm(cfg, "C1").experiment.harness_mode() == "evolve"


def test_provider_shares_one_manager_across_roles(cfg):
    provider = RoleModelProvider(cfg, run_id="r-1", runs_root="/tmp/dr-roles")
    policy = provider.client_for("policy")
    reflection = provider.client_for("reflection")
    proposer = provider.client_for("proposer")
    assert policy is not reflection
    assert all(c.backend is provider.manager for c in (policy, reflection, proposer))
    assert [c.role for c in (policy, reflection, proposer)] == list(ROLE_NAMES)
    assert provider.client_for("policy") is policy # 同角色缓存


def test_provider_client_role_aware_via_coerce(cfg):
    from dice_ratchet.inference.client import coerce_client, role_model_id

    provider = RoleModelProvider(_with_arm(cfg, "B1"), run_id="r-2", runs_root="/tmp/dr-roles")
    assert coerce_client(provider, role="proposer").model_id == "deepseek-chat"
    assert coerce_client(provider).model_id == "qwen3-8b-q4" # 缺省 policy
    assert role_model_id(provider, "proposer") == "deepseek-chat"
