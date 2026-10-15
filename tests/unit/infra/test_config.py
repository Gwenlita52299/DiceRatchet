"""配置体系测试（FR-09-1..4 / NFR-09-3）—— 对应 PRD §11 的 T-09-1..T-09-5。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.infra import config as cfg_mod
from dice_ratchet.infra.config import (AppConfig, artifact_hash, canonical_json,
                                    config_drift, config_hash, derive_seed, env_applied_keys,
                                    full_config, harness_overridden_keys, load_config,
                                    load_seeds)
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
    # （2026-10-14 B-88：原来用的是 env.content_dir，该键已删除；改用仍在的
    #  agent.policy.template_path —— 判据本身与具体键无关，这里只是换一个路径型键）
    a = load_config(cli_overrides={"agent.policy.template_path": "/abs/one/decision.md"},
                    config_dir=CONFIGS)
    b = load_config(cli_overrides={"agent.policy.template_path": "/abs/two/decision.md"},
                    config_dir=CONFIGS)
    assert config_hash(a) == config_hash(b)


def test_config_hash_keeps_relative_paths():
    # 相对路径参与 hash（是可复现的仓库内路径）
    base = load_config(config_dir=CONFIGS)
    other = load_config(cli_overrides={"agent.policy.template_path": "harness/prompts/other.md"},
                        config_dir=CONFIGS)
    assert config_hash(base) != config_hash(other)


def test_config_hash_changes_on_real_change():
    base = load_config(config_dir=CONFIGS)
    changed = load_config(cli_overrides={"loop.k": 31}, config_dir=CONFIGS)
    assert config_hash(base) != config_hash(changed)


# ---- B-90（F79）：config_hash 口径的键级差异（冲突提示的依据）---------------------------

def test_b90_config_drift_ignores_noise_outside_the_hash_basis():
    """易变键 / 绝对路径不进差异表——它们本来就不参与 config_hash，报出来只是噪声。"""
    base = load_config(config_dir=CONFIGS)
    snap = full_config(base)
    other = load_config(cli_overrides={"infra.runs_root": "/tmp/other",
                                       "infra.log_level": "DEBUG",
                                       "loop.k": 31}, config_dir=CONFIGS)
    drift = config_drift(snap, other, harness_keys=harness_overridden_keys(other))
    assert drift["added"] == [] and drift["removed"] == []
    assert drift["changed"] == ["loop.k"]


def test_b90_config_drift_excludes_harness_subtree():
    """harness 覆盖过的键整棵子树从两侧剔除（B-19：config_hash 对它 neutral）。"""
    base = load_config(config_dir=CONFIGS)
    snap = full_config(base)          # 快照存的是 harness 生效值
    snap["agent"]["policy"]["temperature"] = 0.99
    other = load_config(cli_overrides={"agent.policy.temperature": 0.11}, config_dir=CONFIGS)
    # 不剔除 → 假报「值变化」
    assert config_drift(snap, other)["changed"] == ["agent.policy.temperature"]
    # 剔除（真实调用方的做法）→ 干净
    drift = config_drift(snap, other, harness_keys=("agent.policy.temperature",))
    assert drift == {"added": [], "removed": [], "changed": []}


def test_b90_config_drift_reports_added_and_removed_keys():
    """键增删（配置演进）必须与「值变化」分开报案。"""
    base = load_config(config_dir=CONFIGS)
    snap = full_config(base)
    del snap["loop"]["k"]
    snap["loop"]["retired_key"] = 1
    drift = config_drift(snap, base)
    assert drift["added"] == ["loop.k"]
    assert drift["removed"] == ["loop.retired_key"]
    assert drift["changed"] == []


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
    assert proto.splits["heldout"]["baskets"]["H1"]["start"] == 731000


def test_seeds_overlap_rejected(tmp_path):
    bad = tmp_path / "seeds.yaml"
    bad.write_text(
        "version: 1\nhash_algo: sha256\nsplits:\n"
        "  train: {start: 1000, end: 731050}\n" # 与 H1 重叠
        "  heldout: {baskets: {H1: {start: 731000, end: 731039}}}\n"
        "  test: {start: 947000, end: 947099}\n", encoding="utf-8")
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



# ---- Issue #1：模型角色（models.*）与实验臂（experiment.*）--------------------------

def test_models_and_experiment_env_overrides(monkeypatch):
    # 角色绑定与臂都能只用配置切换（--model 之外的两条路：环境变量 / YAML）
    monkeypatch.setenv("DICERATCHET_MODELS__PROPOSER", "deepseek-flash")
    monkeypatch.setenv("DICERATCHET_EXPERIMENT__ARM", "B1")
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.models.proposer == "deepseek-flash"
    assert cfg.experiment.arm == "B1"
    # 角色/臂都计入 config_hash（换臂 = 换 run-id 族）
    assert cfg.models.proposer in json.dumps(cfg_mod.full_config(cfg), ensure_ascii=False)


def test_experiment_arm_must_exist():
    cfg = AppConfig()
    with pytest.raises(InfraError) as exc:
        cfg_mod.AppConfig(
            experiment=cfg_mod.ExperimentConfig(arm="Z9", arms=cfg.experiment.arms)).validate()
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    assert "Z9" in exc.value.message


def test_experiment_arms_reject_unknown_keys_and_harness():
    arms = dict(AppConfig().experiment.arms)
    arms["A0"] = {**arms["A0"], "judge": "x"}
    with pytest.raises(InfraError) as exc:
        AppConfig(experiment=cfg_mod.ExperimentConfig(arm="A0", arms=arms)).validate()
    assert exc.value.code == E.E_IO_CONFIG_INVALID and "judge" in exc.value.message

    arms = dict(AppConfig().experiment.arms)
    arms["A0"] = {**arms["A0"], "harness": "sometimes"}
    with pytest.raises(InfraError) as exc:
        AppConfig(experiment=cfg_mod.ExperimentConfig(arm="A0", arms=arms)).validate()
    assert exc.value.code == E.E_IO_CONFIG_INVALID and "harness" in exc.value.message


def test_experiment_role_overrides_reject_unknown_role():
    with pytest.raises(InfraError) as exc:
        AppConfig(experiment=cfg_mod.ExperimentConfig(
            arm="A1", roles={"judge": "qwen3-8b-q4"})).validate()
    assert exc.value.code == E.E_IO_CONFIG_INVALID and "judge" in exc.value.message


def test_harness_config_cannot_override_experiment(tmp_path):
    # 臂/角色绑定是静态实验决定：harness/config.json 白名单不接受 experiment.* / models.*
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "config.json").write_text('{"experiment.arm": "C1"}', encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        load_config(config_dir=CONFIGS, harness_config=harness / "config.json")
    assert exc.value.code == E.E_IO_CONFIG_INVALID


def test_inference_api_mode_config():
    # api_mode=replay 必须给 replay 目录（fail-closed）
    with pytest.raises(InfraError) as exc:
        cfg_mod.InferenceConfig(api_mode="replay").validate()
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    with pytest.raises(InfraError):
        cfg_mod.InferenceConfig(api_mode="sometimes").validate()
    cfg_mod.InferenceConfig(api_mode="replay", api_replay_dir="runs/x/llm_api").validate()


# ---- F54：held-out 篮子可配置（彩排只用 H3 的前提）--------------------------------

def test_heldout_basket_defaults_to_h1_and_is_overridable(monkeypatch):
    """给定配置体系；当不设/设置 ``DICERATCHET_EVOLVE__HELDOUT_BASKET``；

    则默认是 H1（M3 判据 1 的代际裁判篮子）、可被覆盖成 H3（reserve）。

    **为什么需要这个键**：修复前 held-out 篮子**两处写死**——``evolve.judge.HOLDOUT_BASKET``
    与 ``loop.generation.write_gen000_baseline(basket="H1")``——且没有配置项 ⇒
    ``scripts/run_m3_rehearsal.sh``（声明只用 H3 731080–731099）实测跑在 **H1** 上
    （2026-10-11 事故：已落盘 ``e-731000…731004-g000-parent.jsonl``，¥5.1 API + 5 个一次性 seed）。
    """
    assert cfg_mod.EvolveConfig().heldout_basket == "H1"
    assert load_config(config_dir=CONFIGS).evolve.heldout_basket == "H1"

    monkeypatch.setenv("DICERATCHET_EVOLVE__HELDOUT_BASKET", "H3")
    assert load_config(config_dir=CONFIGS).evolve.heldout_basket == "H3"


@pytest.mark.parametrize("bad", ["H4", "test_full", "h1", "", "train_window"])
def test_heldout_basket_rejects_unknown_names_fail_closed(bad):
    """拼错的篮子名必须在**配置装配**时就被拒。

    修复前：只有跑到评估那一刻才由 ``resolve_basket`` 抛 ``E_EVAL_SEED_OVERLAP``
    —— 那时代价是「gen-000 基线已经花了钱才发现」。
    """
    with pytest.raises(InfraError) as exc:
        cfg_mod.EvolveConfig(heldout_basket=bad).validate()
    assert exc.value.code == E.E_IO_CONFIG_INVALID
    assert "heldout_basket" in str(exc.value)


# ---- B-87/B-88（F75/F77）：env.* 只留真的会被读的键 ---------------------------------
#
# 病根（F73/F75/F77 同一族）：配置表按 PRD 写全、接线按需 ⇒ 「配置声称 ≠ 代码执行」。
# 本节的第一个用例是**系统性护栏**：任何新增的 env.* 键都必须在这里登记消费者，
# 否则 CI 红——把「写完配置就以为接上了」这条路的成本抬到与接线同高。
# B-88 把范围从 `env.sandbox.*` 扩到**整个 `env.*`**（F77 正是漏在这个范围外被发现的）。

#: 键 → 声明的消费者（模块相对仓库根）。新增键必须同时补这一行。
ENV_KEY_CONSUMERS = {
    # MOD-01 局配置：组合根显式传 cfg.env.episode（loop/cli.py），
    # evals/harness.py 另有「配置回落」路径（F-M1 的漏传修复）。
    "env.episode.difficulty": "dice_ratchet/loop/cli.py",
    "env.episode.stage_count": "dice_ratchet/loop/cli.py",
    "env.episode.character": "dice_ratchet/loop/cli.py",
    "env.episode.toggles": "dice_ratchet/loop/cli.py",
    "env.episode.max_env_steps": "dice_ratchet/loop/cli.py",
    "env.episode.max_decisions": "dice_ratchet/loop/cli.py",
    # MOD-02 沙箱：只有这两个键有消费者（见 B-87）。
    "env.sandbox.policy_failure_streak": "dice_ratchet/evals/baselines.py",
    "env.sandbox.audit.ruleset_pin": "dice_ratchet/app.py",
}


def test_b88_every_env_key_declares_a_consumer():
    """``env.*`` 的键集必须与 ``ENV_KEY_CONSUMERS`` **逐键相等**。

    修复前（B-88 之前）本前缀有 11 个键、3 个没有消费者（`content_dir`/`golden_dir`/
    `check_invariants`）⇒ 期望集与实际键集不等 ⇒ 红。**诚实边界**：本用例证明「有人为这个键
    声明了消费者、且该文件提到了这个键名」，不是数据流证明——真正的数据流证据由各键自己的
    用例承担（如 `test_b86_audit_leak_cli_consumes_the_pin`、
    `test_b88_audit_identifies_tables_by_content_hash`）。
    """
    keys = {k for k in cfg_mod._legal_flat_keys() if k.startswith("env.")}
    assert keys == set(ENV_KEY_CONSUMERS), (
        f"未登记消费者的键：{sorted(keys - set(ENV_KEY_CONSUMERS))}；"
        f"已失效的登记：{sorted(set(ENV_KEY_CONSUMERS) - keys)}")
    for key, rel in ENV_KEY_CONSUMERS.items():
        src = (REPO / rel).read_text(encoding="utf-8")
        # 允许两种交接方式：**逐键读**（文件里出现叶名）或 **整对象交接**
        # （组合根把 `cfg.env.episode` 整体交给 EpisodeConfig.from_any ⇒ 只出现父段名）。
        segments = key.split(".")
        needles = {segments[-1], segments[-2]}
        assert needles & {n for n in needles if n in src}, (
            f"{key} 声明的消费者 {rel} 里既找不到叶名 {segments[-1]!r} 也找不到父段 "
            f"{segments[-2]!r}")


def test_b88_dead_env_keys_are_gone():
    """**修复前失败**：`content_dir` / `golden_dir` / `check_invariants` 三键删除（F77）。

    逐键理由见 `EnvConfig` 的类注释；要点：`content_dir` 的权威是 MOD-01 的
    `loader.GAME_CONTENT_DIR`（import 时解析，**从不读 yaml**），配置与快照各说一套正是
    「审计拿错配表」的来源；`check_invariants` 声称默认开启逐步校验，**实际生产局从不执行**
    （被游戏侧 `debug.showHiddenInfo` 门着，本仓库无调用方传 debug）⇒ 已登记 F78。
    """
    env_cfg = cfg_mod.EnvConfig()
    for gone in ("content_dir", "golden_dir", "check_invariants"):
        assert not hasattr(env_cfg, gone), f"{gone} 是 0 消费者键，应已删除"
    keys = cfg_mod._legal_flat_keys()
    for dead in ("env.content_dir", "env.golden_dir", "env.check_invariants"):
        assert dead not in keys, f"{dead} 不应再是合法键"
    import yaml
    raw = yaml.safe_load((CONFIGS / "default.yaml").read_text(encoding="utf-8"))
    assert set(raw["env"]) == {"episode", "sandbox"}, sorted(raw["env"])


def test_b87_dead_sandbox_subtrees_are_gone():
    """**修复前失败**：guard / honeypot / projection / render / exec 五个子树整体删除。

    逐键理由见 ``SandboxConfig`` 的类注释（四个是**不变式开关**——接线即削弱 INV-4/G4；
    其余是无接线路径或权威另有其处的键）。
    """
    box = cfg_mod.SandboxConfig()
    for gone in ("guard", "honeypot", "projection", "render", "exec"):
        assert not hasattr(box, gone), f"{gone} 子树已删除（0 消费者），不应再存在"
        assert not hasattr(cfg_mod.SandboxConfig, gone.capitalize())
    assert box.policy_failure_streak == 3
    assert box.audit.ruleset_pin
    keys = cfg_mod._legal_flat_keys()
    for dead in ("env.sandbox.guard.enforce", "env.sandbox.guard.scan_roots",
                 "env.sandbox.guard.process_boundary", "env.sandbox.honeypot.enabled",
                 "env.sandbox.honeypot.plan_path", "env.sandbox.honeypot.self_test",
                 "env.sandbox.projection.mode", "env.sandbox.projection.schema_version",
                 "env.sandbox.render.max_tokens_p95", "env.sandbox.exec.timeout_s",
                 "env.sandbox.exec.mem_limit_mb", "env.sandbox.exec.cpu_limit_s",
                 "env.sandbox.exec.max_tools"):
        assert dead not in keys, f"{dead} 是 0 消费者键，不应再是合法键"


def test_b87_yaml_sandbox_block_matches_dataclass():
    """yaml 的 ``env.sandbox`` 段不得残留已删键（否则会走「未知键忽略」的静默路径）。"""
    import yaml
    raw = yaml.safe_load((CONFIGS / "default.yaml").read_text(encoding="utf-8"))
    block = raw["env"]["sandbox"]
    assert set(block) == {"policy_failure_streak", "audit"}, sorted(block)
    assert set(block["audit"]) == {"ruleset_pin"}, sorted(block["audit"])


def test_b87_tool_worker_limits_are_code_constants():
    """``exec.*`` 的四个值现在是**代码常量**——必须与 PRD §9 的历史默认值一致。

    containment 参数（RLIMIT_AS / RLIMIT_CPU / 墙钟 / 工具数）不做成配置：让配置或可演化的
    harness 抬高自己的沙箱上限，正是 INV-4 要防的事。改这四个数必须有意识（本用例会提醒）。
    """
    import inspect

    from dice_ratchet.sandbox.guard import ToolWorker
    params = inspect.signature(ToolWorker.__init__).parameters
    assert params["timeout_s"].default == 2.0
    assert params["mem_limit_mb"].default == 256
    assert params["cpu_limit_s"].default == 2
    assert params["max_tools"].default == 32


def test_b87_capabilities_behind_deleted_keys_still_exist():
    """反向护墙：删的是**配置键**，不是能力——测试/golden 仍可直接传参。

    * ``SandboxEnv(projection_mode="assert")``（golden 与 §11 用例用）
    * ``SandboxEnv(honeypot=False)``（对照用例用）
    * ``honeypot.load_plan(configs/honeypot.yaml)``（契约夹具校验，4 个注入点）
    * ``guard.run_agent_tick()``（payload 子进程边界；保留为备用能力，见 F76）
    """
    from dice_ratchet.sandbox.api import SandboxEnv
    from dice_ratchet.sandbox.guard import run_agent_tick
    from dice_ratchet.sandbox.honeypot import INJECTION_POINTS, load_plan
    names = SandboxEnv.__init__.__code__.co_varnames
    assert "projection_mode" in names and "honeypot" in names
    assert callable(run_agent_tick)
    plan = load_plan(CONFIGS / "honeypot.yaml")
    assert set(plan["points"]) == set(INJECTION_POINTS)
