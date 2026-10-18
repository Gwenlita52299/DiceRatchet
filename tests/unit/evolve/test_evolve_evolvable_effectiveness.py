"""F-M3-9：可演化键的**取值域**（A）、**模式裁剪**（C）、**逐臂生效**（D）、
**生效指纹**（B）。

背景（mvp1 现场）：gen-002 以 ``{"agent.policy.temperature": 0.4}`` 被 accept，但

* 该值在 eval 模式恒 0.0、play 模式被 clamp 到 0.7 —— 越界值不会生效（A 要拦住）；
* 候选臂的 ``harness/config.json`` **从未被读过**（进程启动时才读 run 级那份）——
  ``config_patch`` 的候选字节变了、行为没变（D 要修）；
* C2 只看 ``play_hash``（文件字节），于是「改了个不生效的字节」也判成打法变了（B 要修）。

本文件把这些约束钉在**可执行**的判据上。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dice_ratchet.evolve import proposer as P
from dice_ratchet.evolve import versioning as V
from dice_ratchet.infra.config import (POLICY_TEMPERATURE_RANGE, PolicyConfig,
                                       apply_policy_overrides,
                                       effective_policy_values,
                                       harness_policy_overrides)

from evolve_support import context_for, make_candidate, make_config, write_harness


def _config_patch(flat: dict) -> object:
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            f'@@ -1 +1 @@\n-{{}}\n+{json.dumps(flat)}\n')
    return make_candidate(kind="config_patch", diff=diff, touched=["harness/config.json"])


# =====================================================================
# A：取值域（越界值在花钱之前拒绝）
# =====================================================================

def test_temperature_below_clamp_window_rejected():
    """0.4 是 mvp1 被 accept 的那个值：play 下 clamp 到 0.7、eval 下恒 0.0 ⇒ 必须拒。"""
    result = P.validate_candidate(
        _config_patch({"agent.policy.temperature": 0.4}),
        cfg=make_config(evolvable_keys=("sampling.temperature",)))
    assert result.status == "fail"
    assert any("config_value_out_of_range" in v for v in result.violations)


def test_temperature_inside_window_passes():
    result = P.validate_candidate(
        _config_patch({"agent.policy.temperature": 0.9}),
        cfg=make_config(evolvable_keys=("sampling.temperature",)))
    assert result.status == "pass", result.violations


def test_memory_top_k_bounds():
    cfg = make_config(evolvable_keys=("sampling.memory_top_k",))
    ok = P.validate_candidate(_config_patch({"agent.policy.memory_top_k": 3}), cfg=cfg)
    assert ok.status == "pass", ok.violations
    bad = P.validate_candidate(_config_patch({"agent.policy.memory_top_k": 99}), cfg=cfg)
    assert bad.status == "fail"
    assert any("config_value_out_of_range" in v for v in bad.violations)


# =====================================================================
# C：按 mode 裁剪（eval 下 temperature 不生效 ⇒ 提案直接作废）
# =====================================================================

def test_temperature_rejected_in_eval_mode():
    result = P.validate_candidate(
        _config_patch({"agent.policy.temperature": 0.9}),
        cfg=make_config(evolvable_keys=("sampling.temperature",)),
        mode="eval")
    assert result.status == "fail"
    assert any("config_key_inert_in_mode" in v for v in result.violations)


def test_only_inert_keys_left_gives_actionable_reason():
    """白名单非空、但当前模式下全都不生效：给出「请改 prompt/tool 面」而不是空指针。"""
    cfg = make_config(evolvable_keys=("sampling.temperature",))
    result = P.validate_candidate(_config_patch({"agent.policy.memory_top_k": 3}),
                                  cfg=cfg, mode="eval")
    assert result.status == "fail"
    assert any("config_key_inert_in_mode" in v for v in result.violations)


# =====================================================================
# D：候选臂的 config.json 必须逐臂生效
# =====================================================================

def test_harness_policy_overrides_reads_only_agent_policy(tmp_path):
    harness = write_harness(tmp_path)
    (harness / "config.json").write_text(
        json.dumps({"agent.policy.memory_top_k": 3,
                    "memory.retrieve.top_k": 8,
                    "agent.tools.enabled": False}), encoding="utf-8")
    overrides = harness_policy_overrides(harness)
    assert overrides == {"memory_top_k": 3}, overrides


def test_harness_policy_overrides_rejects_unknown_field(tmp_path):
    harness = write_harness(tmp_path)
    (harness / "config.json").write_text(
        json.dumps({"agent.policy.show_intent": True}), encoding="utf-8")
    with pytest.raises(Exception) as exc:
        harness_policy_overrides(harness)
    assert "未知 PolicyConfig 字段" in str(exc.value)


def test_apply_policy_overrides_changes_effective_values():
    base = PolicyConfig()
    patched = apply_policy_overrides(
        base, {"memory_top_k": 2, "temperature": 1.0})
    assert patched.memory_top_k == 2 and patched.temperature == 1.0
    # eval 下温度恒 0.0，但 memory_top_k 依然生效
    assert effective_policy_values(patched, mode="eval") == {
        "temperature": 0.0, "top_p": 1.0, "memory_top_k": 2}
    assert effective_policy_values(patched, mode="train")["temperature"] == 1.0


def test_loop_policy_factory_applies_arm_config(tmp_path, monkeypatch):
    """``loop.cli._policy_factory`` 必须把候选臂的 config.json 套到 PolicyConfig 上。"""
    from dice_ratchet.loop import cli as loop_cli

    harness = write_harness(tmp_path)
    (harness / "config.json").write_text(
        json.dumps({"agent.policy.memory_top_k": 2}), encoding="utf-8")
    app_cfg = type("C", (), {"agent": type("A", (), {"policy": PolicyConfig()})()})()
    factory = loop_cli._policy_factory(app_cfg, None, "llm", harness)
    policy = factory(1000, "e-1000-g001-train", harness)
    assert policy.cfg.memory_top_k == 2


# =====================================================================
# B：生效指纹（字节变了但生效值没变 ⇒ C2 不通过）
# =====================================================================

def test_effective_hash_ignores_inert_temperature_change(tmp_path):
    """温度从 0.7 改到 0.4：eval 下都不生效 ⇒ 生效指纹**不变**，字节指纹变了。"""
    base = write_harness(tmp_path / "base")
    cand = write_harness(tmp_path / "cand")
    (cand / "config.json").write_text(
        json.dumps({"agent.policy.temperature": 0.4}), encoding="utf-8")

    policy = PolicyConfig()
    assert V.play_hash(base) != V.play_hash(cand), "字节指纹应当变化"
    assert (V.effective_play_hash(base, policy_base=policy, mode="eval")
            == V.effective_play_hash(cand, policy_base=policy, mode="eval"))


def test_effective_hash_sees_memory_top_k_change(tmp_path):
    base = write_harness(tmp_path / "base")
    cand = write_harness(tmp_path / "cand")
    (cand / "config.json").write_text(
        json.dumps({"agent.policy.memory_top_k": 2}), encoding="utf-8")

    policy = PolicyConfig()
    assert (V.effective_play_hash(base, policy_base=policy, mode="eval")
            != V.effective_play_hash(cand, policy_base=policy, mode="eval"))


def test_judge_rejects_when_only_inert_bytes_changed(tmp_path):
    """C2 的最终判据：两个指纹都变才算「打法变了」。"""
    from dice_ratchet.evolve.judge import judge
    from dice_ratchet.evolve.schemas import CandidateReport

    from evolve_support import FakeArm

    cand = _config_patch({"agent.policy.temperature": 0.4})
    heldout = {"n": 40, "win_rate": 0.5, "mean_stages_cleared": 5.0,
               "illegal_rate": 0.01, "fallback_rate": 0.0, "truncation_rate": 0.0}
    report = CandidateReport(candidate=cand, heldout=heldout,
                             smoke={"n": 2, "win_rate": 0.5})
    parent = FakeArm(win_rate=0.5, n=40, illegal_rate=0.01,
                     fallback_rate=0.0, truncation_rate=0.0,
                     mean_stages_cleared=5.0)
    verdict = judge(report, parent, cfg=make_config(), generation=1,
                    play_hash="sha256:aaa", parent_play_hash="sha256:bbb",
                    effective_hash="sha256:same", parent_effective_hash="sha256:same")
    assert verdict.decision == "reject"
    assert "no_effective_change" in verdict.reason
