"""配置体系测试（FR-09-1..4 / NFR-09-3）—— 对应 PRD §11 的 T-09-1..T-09-5。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.infra import config as cfg_mod
from dice_ratchet.infra.config import (AppConfig, artifact_hash, canonical_json,
                                    config_hash, derive_seed, env_applied_keys,
                                    harness_overridden_keys, load_config, load_seeds)
from dice_ratchet.infra.errors import E, InfraError

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs"


@pytest.fixture(autouse=True)
def _repo_configs():
    return CONFIGS


# ---- T-09-1 环境变量覆盖默认值 ------------------------------------------------------

def test_env_var_overrides_default(tmp_path, monkeypatch):
    # 给定：default.yaml 的 loop.k=30，环境变量 DICERATCHET_LOOP__K=5
    monkeypatch.setenv("DICERATCHET_LOOP__K", "5")
    # 当：load_config()
    cfg = load_config(config_dir=CONFIGS)
    # 则：cfg.loop.k == 5
    assert cfg.loop.k == 5


def test_cli_beats_env_var(monkeypatch):
    # T-09-2：同上且 CLI 传 --k 7 → CLI 胜出
    monkeypatch.setenv("DICERATCHET_LOOP__K", "5")
    cfg = load_config(cli_overrides={"loop.k": 7}, config_dir=CONFIGS)
    assert cfg.loop.k == 7


def test_env_var_nested_key(monkeypatch):
    # B-30：DICERATCHET_MEMORY__RETRIEVE__TOP_K=8 → memory.retrieve.top_k
    monkeypatch.setenv("DICERATCHET_MEMORY__RETRIEVE__TOP_K", "8")
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.memory.retrieve.top_k == 8


def test_env_var_bool_and_int_inference(monkeypatch):
    monkeypatch.setenv("DICERATCHET_INFRA__UTC", "false")
    monkeypatch.setenv("DICERATCHET_LOOP__MAX_GENERATIONS", "9")
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.infra.utc is False
    assert cfg.loop.max_generations == 9


def test_unknown_env_key_ignored(monkeypatch):
    # FR-09-1：未知键 → 记 E_IO_CONFIG_UNKNOWN 并忽略（不中断）
    monkeypatch.setenv("DICERATCHET_LOOP__NOPE", "1")
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.loop.k == 30
    assert "loop.nope" in load_config.unknown_keys


def test_harness_config_whitelist_rejects_static_keys(tmp_path):
    # FR-09-1：harness/config.json 含非白名单键 → E_IO_CONFIG_INVALID
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "config.json").write_text('{"loop.k": 5}', encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        load_config(config_dir=CONFIGS, harness_config=harness / "config.json")
    assert exc.value.code == E.E_IO_CONFIG_INVALID


def test_harness_config_whitelist_allows_policy(tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "config.json").write_text('{"agent.policy.temperature": 0.9}',
                                         encoding="utf-8")
    cfg = load_config(config_dir=CONFIGS, harness_config=harness / "config.json")
    assert cfg.agent.policy.temperature == 0.9


# ---- T-09-3 / T-09-4 哈希稳定性与作用域 ----------------------------------------------

def test_config_hash_stable_across_runs():
    # T-09-3：同一 AppConfig，两次不同 run_id/时间 → config_hash 相同
    a = config_hash(load_config(config_dir=CONFIGS))
    b = config_hash(load_config(config_dir=CONFIGS))
    assert a == b
    assert len(a) == 16


def test_config_hash_excludes_volatile_fields():
    # FR-09-3：runs_root / log_level / threads 不参与 hash
    base = load_config(config_dir=CONFIGS)
    other = load_config(cli_overrides={"infra.runs_root": "/tmp/other",
                                       "infra.log_level": "DEBUG",
                                       "infra.threads": 4}, config_dir=CONFIGS)
    assert config_hash(base) == config_hash(other)


def test_config_hash_excludes_absolute_paths(tmp_path):
    # FR-09-3：绝对路径不参与 hash —— 两个不同的绝对路径 → hash 相同
    a = load_config(cli_overrides={"env.content_dir": "/abs/one/content"},
                    config_dir=CONFIGS)
    b = load_config(cli_overrides={"env.content_dir": "/abs/two/content"},
                    config_dir=CONFIGS)
    assert config_hash(a) == config_hash(b)


def test_config_hash_keeps_relative_paths():
    # 相对路径参与 hash（是可复现的仓库内路径）
    base = load_config(config_dir=CONFIGS)
    other = load_config(cli_overrides={"env.content_dir": "other/content"},
                        config_dir=CONFIGS)
    assert config_hash(base) != config_hash(other)


def test_config_hash_changes_on_real_change():
    base = load_config(config_dir=CONFIGS)
    changed = load_config(cli_overrides={"loop.k": 31}, config_dir=CONFIGS)
    assert config_hash(base) != config_hash(changed)


def test_config_hash_excludes_harness_version(tmp_path):
    # T-09-4 / B-19：仅 harness 不同 → config_hash 相同，artifact_hash 不同
    base = load_config(config_dir=CONFIGS)
    # harness_version 不是配置键（在 report/version.json 里），此处用 artifact_hash 对比
    h1 = tmp_path / "h1"
    h2 = tmp_path / "h2"
    for d, marker in ((h1, "a"), (h2, "b")):
        d.mkdir()
        (d / "tools.py").write_text(f"# {marker}\n", encoding="utf-8")
    assert artifact_hash(h1) != artifact_hash(h2)
    assert config_hash(base) == config_hash(load_config(config_dir=CONFIGS))


# ---- 回归（2026-10-03 审计 P0-1）：harness 层不得进入 config_hash（B-19）----------------

def _harness(tmp_path, payload: dict) -> Path:
    harness = tmp_path / "harness"
    harness.mkdir(exist_ok=True)
    (harness / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return harness / "config.json"


def test_harness_layer_does_not_change_config_hash(tmp_path):
    """T-09-4 的真实版本：harness/config.json 演化后 config_hash 必须不变。"""
    base = load_config(config_dir=CONFIGS)
    hcfg = _harness(tmp_path, {"agent.policy.temperature": 0.9,
                               "memory.retrieve.top_k": 3,
                               "agent.tools.rank_actions": "tools/rank.py"})
    evolved = load_config(config_dir=CONFIGS, harness_config=hcfg)
    # 生效视图确实变了……
    assert evolved.agent.policy.temperature == 0.9
    assert evolved.memory.retrieve.top_k == 3
    assert evolved.agent.tools == {"rank_actions": "tools/rank.py"}
    # ……但 run 身份（config_hash）不变，harness 变化由 artifact_hash 表达
    assert config_hash(evolved) == config_hash(base)
    assert harness_overridden_keys(evolved) == (
        "agent.policy.temperature", "agent.tools.rank_actions", "memory.retrieve.top_k")
    harness_dir = hcfg.parent
    assert artifact_hash(harness_dir) != artifact_hash(tmp_path / "empty-harness")


def test_harness_tools_key_absent_in_baseline_is_dropped(tmp_path):
    """harness 新增的键（静态基线不存在）在 hash 视图里整键删除，但快照保留生效值。"""
    base = load_config(config_dir=CONFIGS)
    hcfg = _harness(tmp_path, {"agent.tools.only_in_harness": {"a": 1}})
    evolved = load_config(config_dir=CONFIGS, harness_config=hcfg)
    assert config_hash(evolved) == config_hash(base)
    # canonical_json 是**生效视图**（供 config.json 快照与 dry-run），harness 值可见
    assert "only_in_harness" in canonical_json(evolved)
    assert evolved.agent.tools == {"only_in_harness": {"a": 1}}


def test_static_policy_change_still_changes_config_hash(tmp_path):
    """config_hash 仍覆盖静态配置：default.yaml/CLI 改 policy.* 必须换 hash。"""
    base = load_config(config_dir=CONFIGS)
    changed = load_config(cli_overrides={"agent.policy.temperature": 0.71}, config_dir=CONFIGS)
    assert config_hash(base) != config_hash(changed)


def test_hash_volatile_paths_excluded():
    """FR-09-3：路径/日志级别/线程数不参与 config_hash（与代码注释一致）。"""
    base = load_config(config_dir=CONFIGS)
    other = load_config(cli_overrides={"infra.config_dir": "/tmp/cfg",
                                       "infra.schemas_dir": "/tmp/schemas"},
                        config_dir=CONFIGS)
    assert config_hash(base) == config_hash(other)


def test_harness_cannot_set_eval_temperature(tmp_path):
    """FR-09-1：harness 层不得覆盖评估温度（Spec §8.1 B-14）。"""
    hcfg = _harness(tmp_path, {"evals.determinism.eval_temperature": 0.3})
    with pytest.raises(InfraError) as exc:
        load_config(config_dir=CONFIGS, harness_config=hcfg)
    assert exc.value.code == E.E_IO_CONFIG_INVALID


def test_harness_cannot_set_static_keys(tmp_path):
    """FR-09-1：harness 层只接受白名单前缀。"""
    hcfg = _harness(tmp_path, {"loop.k": 5})
    with pytest.raises(InfraError) as exc:
        load_config(config_dir=CONFIGS, harness_config=hcfg)
    assert exc.value.code == E.E_IO_CONFIG_INVALID


def test_harness_short_prefix_is_normalized(tmp_path):
    """harness 可用文档短前缀 ``policy.*``（归一为 ``agent.policy.*``）。"""
    hcfg = _harness(tmp_path, {"policy.memory_top_k": 7})
    cfg = load_config(config_dir=CONFIGS, harness_config=hcfg)
    assert cfg.agent.policy.memory_top_k == 7
    assert harness_overridden_keys(cfg) == ("agent.policy.memory_top_k",)


def test_canonical_json_is_sorted_and_compact():
    text = canonical_json(load_config(config_dir=CONFIGS))
    # 紧凑：无缩进/空格；排序：每一层键序确定（不依赖 PYTHONHASHSEED）
    assert "\n" not in text and ": " not in text and ", " not in text
    obj = json.loads(text)
    assert list(obj) == sorted(obj)
    assert list(obj["loop"]) == sorted(obj["loop"])
    # 同一配置两次序列化逐字节一致
    assert text == canonical_json(load_config(config_dir=CONFIGS))


# ---- T-09-5 derive_seed 跨 PYTHONHASHSEED 稳定 ---------------------------------------

def test_derive_seed_stable_in_process():
    assert derive_seed("a", 1, 2) == derive_seed("a", 1, 2)
    assert derive_seed("a", 1, 2) != derive_seed("a", 1, 3)


def test_derive_seed_is_64bit_unsigned():
    v = derive_seed("anything")
    assert 0 <= v < 2**64


def test_derive_seed_no_collision_boundary():
    # 分隔符防撞哈希："ab"+"c" ≠ "a"+"bc"
    assert derive_seed("ab", "c") != derive_seed("a", "bc")


def test_derive_seed_types():
    assert derive_seed(1, 1.0, True, None, [1, 2]) == derive_seed(1, 1.0, True, None, [1, 2])
    assert derive_seed(1) != derive_seed(1.0) # 类型标签区分


# ---- FR-09-2 schema 校验 -------------------------------------------------------------

def test_invalid_type_reports_key_path():
    with pytest.raises(InfraError) as exc:
        load_config(cli_overrides={"loop.k": "not-a-number"}, config_dir=CONFIGS)
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    assert "loop.k" in str(exc.value)


def test_out_of_range_reports_bounds():
    with pytest.raises(InfraError) as exc:
        load_config(cli_overrides={"loop.max_candidates_per_generation": 9},
                    config_dir=CONFIGS)
    assert "max_candidates_per_generation" in str(exc.value)


def test_unknown_flat_key_ignored_and_recorded():
    # FR-09-1：未知键（含未知段）→ 记 E_IO_CONFIG_UNKNOWN 并忽略，不中断
    cfg = load_config(cli_overrides={"nonexistent.key": 1}, config_dir=CONFIGS)
    assert cfg.loop.k == 30
    assert "nonexistent.key" in load_config.unknown_keys


# ---- 回归（2026-10-03 审计 P1-4/P1-6）：未知段记录 + 注解级类型校验 --------------------

def test_unknown_env_section_recorded(monkeypatch):
    """未知段（如 Spec §5 的 DICERATCHET_MEMORY_RETRIEVE__TOP_K 写法）必须被记录而非静默丢弃。"""
    monkeypatch.setenv("DICERATCHET_FOO__BAR", "1")
    load_config(config_dir=CONFIGS)
    assert "foo.bar" in load_config.unknown_keys
    assert "DICERATCHET_FOO__BAR" in load_config.unknown_keys


def test_env_applied_keys_lists_only_accepted(monkeypatch):
    monkeypatch.setenv("DICERATCHET_LOOP__K", "9")
    monkeypatch.setenv("DICERATCHET_LOOP__NOPE", "1")
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.loop.k == 9
    assert env_applied_keys(cfg) == ("DICERATCHET_LOOP__K",)


@pytest.mark.parametrize("key,value", [
    ("infra.fsync_every", "not-an-int"),
    ("loop.io_retry", "oops"),
    ("infra.utc", "maybe"),
    ("agent.policy.tools_enabled", "yes"),
    ("evals.n_workers", "x"),
    ("env.episode.toggles", "relics"),
])
def test_annotation_type_validation_rejects_wrong_types(key, value):
    """FR-09-2：121 个此前无手写校验的键改由 dataclass 注解统一校验。"""
    with pytest.raises(InfraError) as exc:
        load_config(cli_overrides={key: value}, config_dir=CONFIGS)
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    assert key in str(exc.value)


def test_int_is_accepted_for_float_annotation():
    cfg = load_config(cli_overrides={"agent.policy.temperature": 1}, config_dir=CONFIGS)
    assert cfg.agent.policy.temperature == 1


def test_yaml_lists_are_coerced_to_tuples():
    cfg = load_config(config_dir=CONFIGS)
    assert isinstance(cfg.env.episode.toggles, tuple)
    assert isinstance(cfg.evolve.evolvable_keys, tuple)
    assert isinstance(cfg.evals.baseline.kinds, tuple)


def test_unknown_key_in_yaml_file_is_rejected(tmp_path):
    # YAML 文件内的未知子键是作者错误 → 硬失败（区别于 env/CLI 的宽松策略）
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "default.yaml").write_text("loop:\n  nope: 1\n", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        load_config(config_dir=cfg_dir)
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    assert "loop.nope" in str(exc.value)


def test_eval_temperature_locked():
    # Spec §8.1 B-14：评估温度固定 0.0
    with pytest.raises(InfraError):
        load_config(cli_overrides={"eval.determinism.eval_temperature": 0.3},
                    config_dir=CONFIGS)


def test_cross_section_consistency():
    # loop.n_candidates 必须 == evolve.n_candidates；loop.heldout_n == eval.n_heldout
    with pytest.raises(InfraError) as exc:
        load_config(cli_overrides={"evolve.n_candidates": 3, "loop.n_candidates": 2},
                    config_dir=CONFIGS)
    assert "n_candidates" in str(exc.value)


def test_leak_scan_must_be_true():
    with pytest.raises(InfraError):
        load_config(cli_overrides={"agent.policy.leak_scan": False}, config_dir=CONFIGS)


def test_eval_mode_requires_seed():
    with pytest.raises(InfraError) as exc:
        load_config(cli_overrides={"inference.mode": "eval"}, config_dir=CONFIGS)
    assert "seed" in str(exc.value)


# ---- seeds.yaml 加载（MOD-08 §9 协议，加载归 MOD-09）----------------------------------

def test_load_seeds_protocol():
    proto = load_seeds(CONFIGS)
    assert set(proto.splits) == {"train", "heldout", "test"}
    assert proto.splits["heldout"]["baskets"]["H1"]["start"] == 2000


def test_seeds_overlap_rejected(tmp_path):
    bad = tmp_path / "seeds.yaml"
    bad.write_text(
        "version: 1\nhash_algo: sha256\nsplits:\n"
        "  train: {start: 1000, end: 2050}\n" # 与 H1 重叠
        "  heldout: {baskets: {H1: {start: 2000, end: 2039}}}\n"
        "  test: {start: 9000, end: 9099}\n", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        load_seeds(tmp_path)
    assert exc.value.code == E.E_EVAL_SEED_OVERLAP


def test_seeds_missing_returns_defaults(tmp_path):
    proto = load_seeds(tmp_path)
    assert proto.splits == {}


# ---- harness/ 目录（Spec §4.4）-------------------------------------------------------

def test_repo_harness_dir_exists_and_is_hashable():
    """仓库根的 ``harness/`` 必须存在且可被 ``artifact_hash`` 指纹化。

    MOD-04（M1）从这里读 prompt / 采样参数覆盖，MOD-07（M2）在这里写 ``version.json``
    与 ``versions/v{N}/``。此前该目录不存在（2026-10-03 审计「未覆盖」项），
    2026-10-03 由 M0 验收后续创建，本用例把它钉住。
    """
    harness = REPO / "harness"
    assert harness.is_dir(), "harness/ 缺失（Spec §4.4 的 harness 工件根）"
    assert (harness / "config.json").is_file(), "harness/config.json 缺失（Spec §4.4）"
    for sub in ("prompts", "tools", "versions"):
        assert (harness / sub).is_dir(), f"harness/{sub}/ 缺失（Spec §4.4 / B-29）"
    digest = artifact_hash(harness)
    assert digest.startswith("sha256:") and digest != "sha256:" + "0" * 64, (
        "artifact_hash 落到空目录兜底值 → harness/ 实际为空或不可读")


def test_repo_harness_layer_only_whitelisted_and_isolated_from_config_hash(tmp_path):
    """v1 的 harness 层：只含白名单键，且**不计入** ``config_hash``（B-19）。

    ``harness/config.json`` 当前为 ``{}``（尚未演化）。用例不要求它永远为空，但要求
    「harness 层存在与否不影响 config_hash」这条 B-19 结论成立，否则 ``--resume``
    会在第一代被接受后必然冲突（P0-1 的回归面）。
    """
    with_h = load_config(harness_config=REPO / "harness" / "config.json")
    without_h = load_config(harness_config=REPO / "harness" / "__absent__.json")
    assert config_hash(with_h) == config_hash(without_h), "harness 层不得进入 config_hash（B-19）"
    # 白名单校验：非白名单键必须报错而不是被静默接受（用 tmp 文件，不污染真实 harness/）
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"episode.max_env_steps": 5}), encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        load_config(harness_config=bad)
    assert exc.value.code == E.E_IO_CONFIG_INVALID


# ---- M1 验收 F12：配置里的路径必须指向真实文件（死配置回归） -------------------------

def test_policy_prompt_paths_point_to_real_files():
    """F12：``policy.{fixed_prompt_path,task_path}`` 的默认值必须真实存在。

    修复前它们指向 ``harness/prompts/{fixed_v1,task}.md``（**不存在**），``policy.py``
    靠包内回落兜住并静默——于是「改了配置也不影响实际 prompt」，与 M0 的 F1（死配置）同类。
    两个文件都是随包冻结件（R-6：基线模板不随 harness 演化），因此指向包内路径。
    """
    cfg = load_config(config_dir=CONFIGS)
    for key, value in (("fixed_prompt_path", cfg.agent.policy.fixed_prompt_path),
                       ("task_path", cfg.agent.policy.task_path),
                       ("template_path", cfg.agent.policy.template_path)):
        assert (REPO / value).is_file(), f"agent.policy.{key}={value!r} 指向不存在的文件"


def test_policy_template_paths_are_consumed_by_the_agent(tmp_path):
    """F12 的另一半：改配置必须**真的**改变 agent 读到的模板（不再被包内回落静默覆盖）。"""
    from dataclasses import replace

    from dice_ratchet.agent.policy import PolicyAgent
    from dice_ratchet.agent.prompts.assembler import FIXED_TEMPLATE_PATH

    frozen = tmp_path / "my_fixed.md"
    frozen.write_text("# 自定义冻结模板\n\n观测:\n{{observation}}\n\n记忆:\n{{memory}}\n",
                      encoding="utf-8")
    cfg = load_config(config_dir=CONFIGS)
    agent = PolicyAgent(replace(cfg.agent.policy, mode="baseline_fixed_prompt",
                                fixed_prompt_path=str(frozen)),
                        harness_dir=REPO / "harness")
    assert agent.template_spec.path == str(frozen)
    assert "自定义冻结模板" in agent.template_spec.text
    # 反向：路径不可用时回落包内冻结件（仍然是固定的那一份，不会退化成兜底模板）
    missing = PolicyAgent(replace(cfg.agent.policy, mode="baseline_fixed_prompt",
                                  fixed_prompt_path=str(tmp_path / "nope.md")),
                          harness_dir=REPO / "harness")
    assert missing.template_spec.path == str(FIXED_TEMPLATE_PATH)
    assert missing.template_spec.fallback_used is False


def test_schema_ids_are_uniform():
    """F12②：所有 ``schemas/*.json`` 都要有 ``$id``，且都用 ``diceratchet.local`` 前缀。"""
    ids = {}
    for path in sorted((REPO / "schemas").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "$id" in data, f"{path.name} 缺 $id"
        ids[path.name] = data["$id"]
    # 逐名登记（不用 len==N）：新增 schema 必须显式出现在这里，同时登记
    # infra/io.py::_SCHEMA_ERROR_CODES 的错误码归属（test_logging 守门）。
    expected = {
        "audit_report.schema.json", "decision.schema.json",
        "generation_report.schema.json", "harness_gate.schema.json",
        "inference_call.schema.json", "models.schema.json",
        "observation.schema.json", "trajectory_line.schema.json",
        # 2026-10-06 MOD-05 memory 落地：两个 entry + 两个跨模块返回值（G5）
        "memory_entry.schema.json", "reflection_entry.schema.json",
        "retrieve_result.schema.json", "add_result.schema.json",
        # 2026-10-06 MOD-06 loop 落地：run 状态机 + 计分台账 + 反思记录外壳（G5）
        "run_state.schema.json", "scoreboard.schema.json", "loop_reflection.schema.json",
        # 2026-10-06 MOD-07 evolve 落地：候选改动 + 候选评估记录（G5）
        "evolve_candidate.schema.json", "evolve_candidate_report.schema.json",
    }
    assert set(ids) == expected, sorted(set(ids) ^ expected)
    assert all(value.startswith("https://diceratchet.local/schemas/") for value in ids.values()), ids


# ---- 推理/评估关键默认值的三层一致性（M1 验收复核发现的默认值漂移） ----------------

@pytest.mark.parametrize("key,expected", [
    ("total_timeout_ms", 120000),
    ("generate_timeout_ms", 30000),
    ("max_attempts", 3),
    ("max_tokens", 256),
    ("retry_backoff_ms", 200),
])
def test_inference_defaults_match_yaml(key, expected):
    """dataclass 默认必须与 ``configs/default.yaml`` 一致。

    背景（2026-10-06 M1 验收复核）：``total_timeout_ms`` 的 YAML 值是实测回填的 120000，
    但 dataclass 默认长期停留在 8000。两者不一致时，凡「不读 default.yaml 直接构造
    ``InferenceConfig()``」的路径（库式调用/测试夹具）都会拿到把正常决策判成
    ``E_LLM_TIMEOUT`` 的旧值；且 ``_default_dict()`` 取自 dataclass，会让 YAML 缺失该键时静默回落。
    """
    from dice_ratchet.infra.config import InferenceConfig

    assert getattr(InferenceConfig(), key) == expected
    assert getattr(load_config(config_dir=CONFIGS, harness_config=REPO / "harness/config.json").inference,
                   key) == expected


def test_seed_timeout_defaults_agree_across_layers():
    """``EvalConfig`` / ``EvalSpec`` / YAML 的 ``seed_timeout_s`` 必须同为 14400（F24 实测标定值）。"""
    from dice_ratchet.evals.harness import EvalSpec
    from dice_ratchet.infra.config import EvalConfig

    yaml_value = load_config(config_dir=CONFIGS,
                             harness_config=REPO / "harness/config.json").evals.seed_timeout_s
    spec_value = EvalSpec(run_id="r", generation=0, harness_version=0, split="train",
                          basket="train_window", seeds=[1], agent_kind="heuristic").seed_timeout_s
    assert yaml_value == 14400
    assert EvalConfig().seed_timeout_s == yaml_value
    assert spec_value == yaml_value


def test_numeric_getattr_fallbacks_for_timeouts_are_canonical():
    """超时的 ``getattr(cfg, key, <字面量>)`` 兜底必须与规范值一致。

    这类兜底只在属性缺失时才生效，因此很容易在改默认值时被漏掉——M1 验收复核时一次查到
    三处漂移（``app.py`` 300、``evals/cli.py`` 1800、``inference/backend.py`` 8000）。
    这里直接读源码文本，保证以后再改默认值时这三处不会再次分叉。
    """
    import inspect

    from dice_ratchet import app as app_mod
    from dice_ratchet.evals import cli as evals_cli
    from dice_ratchet.inference import backend as backend_mod

    cases = [
        (app_mod, "seed_timeout_s", "14400"),
        (evals_cli, "seed_timeout_s", "14400"),
        (backend_mod, "total_timeout_ms", "120_000"),
    ]
    for module, key, expected in cases:
        source = inspect.getsource(module)
        hits = [line.strip() for line in source.splitlines()
                if "getattr(" in line and f'"{key}"' in line]
        assert hits, f"{module.__name__} 里找不到 {key} 的 getattr 兜底"
        for hit in hits:
            compact = hit.replace(" ", "").replace("_", "")
            assert expected.replace("_", "") in compact, \
                f"{module.__name__} 的 {key} 兜底不是 {expected}：{hit}"

