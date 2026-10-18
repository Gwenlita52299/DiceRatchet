"""F-M3-5：`evolve.evolvable_keys` 的每个概念键都必须落到真实配置字段上。

背景：`prompt.show_intent` 在概念白名单里挂了很久，但 `PolicyConfig` 从来没有这个
字段、也没有任何运行期读取点（意图/推理已由 `decision.md` 强制 `reasoning` 恒开）。
提案一旦写它，前缀白名单与「可演化键」两道校验都放行，直到 `load_config` 构造
`PolicyConfig` 才 `E_IO_CONFIG_INVALID`——那时训练已经跑完，这一代白跑。

2026-10-18 已把它从白名单删除（`configs/default.yaml` + `EvolveConfig` 默认值 +
MOD-07 PRD 三处同步）。这个文件是**棘轮**：白名单里不许再出现加载器不认识的键。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.evolve import proposer as P
from dice_ratchet.infra.config import load_config

CONFIGS = Path(__file__).resolve().parents[3] / "configs"

#: 现行白名单（概念名）。新增键时必须同时能在 `harness/config.json` 里写出来。
EXPECTED_EVOLVABLE_KEYS = (
    "sampling.temperature", "sampling.memory_top_k",          # 训练侧
    "plan.max_plan_len", "prompt.token_budget", "policy.retry_max",  # eval 侧（F-M3-10）
)


def _walk(cfg, dotted: str):
    """按点分路径取 `AppConfig` 上的当前值；路径不存在即断言失败。"""
    node = cfg
    for part in dotted.split("."):
        assert hasattr(node, part), f"{dotted} 在 AppConfig 上不存在（段 {part}）"
        node = getattr(node, part)
    return node


def test_default_evolvable_keys_are_the_two_real_ones():
    """默认白名单只剩两个真键；`prompt.show_intent` 已下线。"""
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.evolve.evolvable_keys == EXPECTED_EVOLVABLE_KEYS
    assert "prompt.show_intent" not in cfg.evolve.evolvable_keys
    # F-M3-9：`retrieval.top_k` 被 policy.memory_top_k 遮蔽（永不生效）⇒ 已移出白名单
    assert "retrieval.top_k" not in cfg.evolve.evolvable_keys


def test_every_evolvable_key_maps_to_a_loadable_harness_key(tmp_path):
    """白名单里的每个键：翻译成实际扁平键 → 写进 harness/config.json → 加载必须成功。

    这条测试比「校验器放行」更强：它校验的是**配置加载器**这一端，也就是旧实现
    真正漏掉的那一端。
    """
    cfg = load_config(config_dir=CONFIGS)
    assert cfg.evolve.evolvable_keys, "白名单为空时本测试无意义"
    for key in cfg.evolve.evolvable_keys:
        actual = P._actual_config_key(key)
        assert P._is_harness_config_key(actual), f"{key} → {actual} 不在加载器前缀白名单内"
        value = _walk(cfg, actual)
        harness = tmp_path / "config.json"
        harness.write_text(json.dumps({actual: value}), encoding="utf-8")
        load_config(config_dir=CONFIGS, harness_config=harness)  # 不抛即通过


def test_every_declared_key_has_a_spec():
    """白名单里的每一项都必须在规格表里（否则读值域/生效 mode 时无据可依）。"""
    cfg = load_config(config_dir=CONFIGS)
    for concept in cfg.evolve.evolvable_keys:
        assert concept in P.EVOLVABLE_SPEC_BY_CONCEPT, concept
        spec = P.EVOLVABLE_SPEC_BY_CONCEPT[concept]
        assert spec.actual.startswith("agent.policy."), spec.actual


def test_mode_filters_inert_keys():
    """F-M3-9/10：eval 下 `sampling.*` 不生效 ⇒ 只剩三个 eval 侧键。"""
    cfg = load_config(config_dir=CONFIGS).evolve
    assert P.effective_evolvable_keys(cfg, "eval") == (
        "plan.max_plan_len", "prompt.token_budget", "policy.retry_max")
    assert P.effective_evolvable_keys(cfg, "train") == EXPECTED_EVOLVABLE_KEYS


def test_show_intent_is_not_a_known_key():
    """`prompt.show_intent` 不是规格表里的键（别名存在也不代表可演化）。"""
    assert "prompt.show_intent" not in P.EVOLVABLE_SPEC_BY_CONCEPT
    assert P._actual_config_key("prompt.show_intent") == "agent.policy.show_intent"


def test_show_intent_direct_patch_is_rejected():
    """即使有人手动写这个键，也在花钱之前被拒（通用路径，不是特判分支）。"""
    from evolve_support import make_candidate

    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"agent.policy.show_intent": true}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    cfg = load_config(config_dir=CONFIGS)
    result = P.validate_candidate(cand, cfg=cfg.evolve)
    assert result.status == "fail"
    # 它不在白名单里（默认白名单已无该键）⇒ 通用「不可演化」拒绝
    assert any("config_key_not_evolvable" in v for v in result.violations)
