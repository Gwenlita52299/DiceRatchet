"""T-12 + FR-08-11…14：三基线复用、确定性与不可覆盖归档。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.agent.heuristic import heuristic_policy
from gamer_rsi.evals.baselines import (HeuristicAgent, RandomAgent, archive_baseline,
                                       baseline_arm, build_agent,
                                       fixed_prompt_policy_config)
from gamer_rsi.evals.metrics import aggregate
from gamer_rsi.infra.errors import E, InfraError

from evals_support import make_result

_OBS = {"phase": "rest", "player": {"hearts": 2}}
_LEGAL = [{"kind": "rest_choose", "optionId": "heart"},
          {"kind": "rest_choose", "optionId": "gold"}]


class _StubClient:
    """最小 MOD-03 客户端替身（只用于构造/装配断言，不发真实调用）。"""

    model_id = "stub-1"

    def generate_json(self, messages, schema, *, mode, sampling, seed):  # noqa: D102
        raise AssertionError("本用例不应真的调用模型")


def test_heuristic_agent_delegates_to_mod04():
    # T-12：同一 obs/legal 上必须与 MOD-04 heuristic_policy 选同一动作（防逻辑分叉）
    agent = HeuristicAgent(seed=7)
    assert agent.act(_OBS, _LEGAL) == heuristic_policy(_OBS, _LEGAL, {})


def test_random_agent_is_seed_deterministic_and_legal():
    a = RandomAgent(seed=2000)
    b = RandomAgent(seed=2000)
    picks_a = [a.act(_OBS, _LEGAL) for _ in range(20)]
    picks_b = [b.act(_OBS, _LEGAL) for _ in range(20)]
    assert picks_a == picks_b
    assert all(p in _LEGAL for p in picks_a)


def test_fixed_prompt_without_client_fails_closed():
    """F1/F22：拿不到 MOD-03 客户端时必须 fail-closed，不得降级成启发式副本。"""
    with pytest.raises(InfraError) as exc:
        build_agent("fixed_prompt", seed=1, client=None)
    assert exc.value.code == E.E_LLM_UNAVAILABLE


def test_build_agent_kinds():
    assert isinstance(build_agent("random", seed=1), RandomAgent)
    assert isinstance(build_agent("heuristic", seed=1), HeuristicAgent)
    with pytest.raises(InfraError):
        build_agent("nope", seed=1)


def test_fixed_prompt_agent_is_the_mod04_policy_agent():
    """F22：``fixed_prompt`` 走 MOD-04 ``PolicyAgent(mode="baseline_fixed_prompt")``。

    修复前 MOD-08 另有一份逐 env 步的 ``FixedPromptAgent``：一次推理只推进一个 env 步，
    实测比决策点口径多花 **3.1×** 调用（M1 验收报告 F22）。
    """
    from gamer_rsi.agent.policy import PolicyAgent

    agent = build_agent("fixed_prompt", seed=1, client=_StubClient(), harness_version=9)
    assert isinstance(agent, PolicyAgent)
    assert hasattr(agent, "play_decision") # SandboxRunner 据此走决策点循环
    assert agent.cfg.mode == "baseline_fixed_prompt"
    assert agent.cfg.temperature == 0.0
    assert agent.cfg.tools_enabled is False # R-6：harness 工具演化不得改变基线 prompt
    assert agent.cfg.memory_top_k == 0 and agent.cfg.reflection_max == 0
    assert agent.harness_version == 0 # R-6：harness_version 一律归 0，不受传入值影响


def test_fixed_prompt_policy_config_keeps_the_rest_of_the_config():
    class _Inner:
        from gamer_rsi.infra.config import PolicyConfig

        policy = PolicyConfig(retry_max=2, max_plan_len=8,
                              fixed_prompt_path="custom/frozen.md")

    class _Cfg:
        agent = _Inner()

    frozen = fixed_prompt_policy_config(_Cfg())
    assert frozen.mode == "baseline_fixed_prompt"
    assert frozen.retry_max == 2 and frozen.max_plan_len == 8
    assert frozen.fixed_prompt_path == "custom/frozen.md" # 冻结模板路径仍由配置决定


def test_archive_baseline_records_provenance(tmp_path):
    """F6：归档必须记真实 commit / seeds_hash / 后端，而不是空串与配置里的 model_id。"""
    metrics = aggregate([make_result(2000, outcome="win")], basket="H1")
    path = archive_baseline("heuristic", "heldout", metrics, run_id="r-1",
                            model_hash="none", git_commit="abc123", run_dir=tmp_path,
                            ts="20260101T000000Z", seeds=[2000], seeds_hash="sha256:dead",
                            protocol_hash="sha256:beef", model_backend="none",
                            seed_timeout_s=1800, n_workers=1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["git_commit"] == "abc123"
    assert payload["seeds_hash"] == "sha256:dead"
    assert payload["protocol_hash"] == "sha256:beef"
    assert payload["model_hash"] == "none" and payload["model_backend"] == "none"
    assert payload["seeds"] == [2000] and payload["seed_timeout_s"] == 1800
    header = (tmp_path / "baselines" / "registry.csv").read_text(encoding="utf-8")
    assert "model_backend" in header.splitlines()[0]
    assert "seeds_hash" in header.splitlines()[0]


def test_baseline_arm_labels_are_the_f5_filter_keys():
    assert [baseline_arm(k) for k in ("random", "heuristic", "fixed_prompt")] == [
        "baseline-random", "baseline-heuristic", "baseline-fixed_prompt"]


def test_archive_baseline_is_not_overwritable(tmp_path):
    metrics = aggregate([make_result(2000, outcome="win")], basket="H1")
    first = archive_baseline("heuristic", "heldout", metrics, run_id="r-1",
                             model_hash="m1", git_commit="abc", run_dir=tmp_path,
                             ts="20260101T000000Z")
    second = archive_baseline("heuristic", "heldout", metrics, run_id="r-1",
                              model_hash="m1", git_commit="abc", run_dir=tmp_path,
                              ts="20260102T000000Z")
    assert first.name == "heuristic-heldout.json"
    assert second.name == "heuristic-heldout-20260102T000000Z.json"
    assert json.loads(first.read_text(encoding="utf-8"))["kind"] == "heuristic"
    registry = (tmp_path / "baselines" / "registry.csv").read_text(encoding="utf-8")
    assert len(registry.strip().splitlines()) == 3 # 表头 + 两行
