"""MOD-06 单局时序与逐步落盘（PRD §11 的 T-06-1 / T-06-2 / T-06-3）。

给定-当-则全部用 ``tests/fakes.py`` 的假依赖：不调模型、不写真实 ``runs/``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeEnv, FakePolicy
from dice_ratchet.sandbox.api import EpisodeConfig


# =====================================================================
# T-06-1 单局时序
# =====================================================================

def test_t06_1_episode_timeline_and_jsonl(tmp_path, mk_runner):
    """给定假 policy（每决策 1 步）+ 假 env（3 步后 terminated）。

    当 ``run_episode(seed=1, split="train", arm="train")``；
    则 JSONL 恰好 3 行步骤 + 1 行 ``episode_end``，``outcome`` 取自 ``info["outcome"]``，
    每行 ``obs_digest`` 以 ``sha256:`` 开头，路径为
    ``episodes/train/e-000001-g000-train.jsonl``。
    """
    env = FakeEnv(steps_to_terminate=3)
    policy = FakePolicy()
    runner, ctx = mk_runner(tmp_path, policy=policy, env=env)
    policy.reset_calls = 0

    rec = runner.run_episode(1, split="train", arm="train",
                             episode_config=EpisodeConfig(), harness_version=0)

    path = Path(tmp_path) / "r-loop-test" / "episodes" / "train" / "e-000001-g000-train.jsonl"
    assert Path(rec.jsonl_path) == path
    assert path.is_file()
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    import json

    parsed = [json.loads(line) for line in rows]
    steps = [r for r in parsed if r.get("type") != "episode_end"]
    ends = [r for r in parsed if r.get("type") == "episode_end"]
    assert len(steps) == 3, f"应有 3 行步骤，实际 {len(steps)}"
    assert len(ends) == 1
    assert ends[0]["outcome"] == "win"
    assert all(r["obs_digest"].startswith("sha256:") for r in steps)
    assert [r["step"] for r in steps] == [0, 1, 2]
    assert rec.steps == 3 and rec.outcome == "win"
    assert rec.episode_key == "e-000001-g000-train"
    # FR-06-1：policy.reset 在 env.reset 之前被调用一次
    assert len(policy.resets) == 1


def test_t06_1_reset_precedes_env_reset(tmp_path, mk_runner):
    """FR-06-1 的顺序：``policy.reset(ctx)`` 必须在 ``env.reset(seed)`` 之前。"""
    order: list[str] = []

    class OrderedEnv(FakeEnv):
        def reset(self, seed, **kw):
            order.append("env")
            return super().reset(seed, **kw)

    class OrderedPolicy(FakePolicy):
        def reset(self, ctx):
            order.append("policy")
            return super().reset(ctx)

    runner, _ = mk_runner(tmp_path, policy=OrderedPolicy(), env=OrderedEnv())
    runner.run_episode(1, split="train", arm="train", harness_version=0)
    assert order[0] == "policy"
    assert "env" in order


# =====================================================================
# T-06-2 截断与计负
# =====================================================================

@pytest.mark.parametrize("reason", ["step_limit", "policy_failure"])
def test_t06_2_truncated_counts_as_lose(tmp_path, mk_runner, reason):
    """给定假 env 在第 5 步返回 ``truncated=True``；当跑一局；

    则 ``outcome=="lose"``、``truncation_reason`` 原样记录，且本模块自身不做任何步数计数
    （5 步就停在 5 步，与 ``episode.max_env_steps`` 无关）。
    """
    env = FakeEnv(steps_to_terminate=99, truncate_at=5, truncation_reason=reason)
    runner, _ = mk_runner(tmp_path, policy=FakePolicy(), env=env)
    rec = runner.run_episode(1, split="train", arm="train",
                             episode_config=EpisodeConfig(max_env_steps=1000))
    assert rec.outcome == "lose"
    assert rec.truncation_reason == reason
    assert len(env.steps) == 5
    assert rec.steps == 5
    # 截断由 MOD-02 判定；MOD-06 只读 info，不自行判负
    assert rec.dropped is False


def test_t06_2_truncated_counter_via_orchestrator(tmp_path, mk_orchestrator, mk_memory):
    """截断局计入 ``counters.episodes_truncated``（PRD §13 的派生计数原料）。"""
    env = FakeEnv(steps_to_terminate=99, truncate_at=4, truncation_reason="step_limit")
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(), env=env,
                                memory=mk_memory(), seeds=(1000,))
    orch.config = _with(orch.config, reflect_enabled=False)
    orch.runner.config = orch.config
    summary = orch.run(seeds=[1000])
    assert summary.exit_code == 0
    assert ctx.state.counters.episodes_truncated == 1
    assert ctx.state.counters.episodes_total == 1


def _with(cfg, **kwargs):
    from dataclasses import replace

    return replace(cfg, **kwargs)


# =====================================================================
# T-06-3 计划内多步
# =====================================================================

def test_t06_3_multi_step_plan_with_reject_row(tmp_path, mk_runner):
    """给定一个决策返回 4 个 ``StepRecord``（第 2 个为 reject 行）；当落盘；

    则 4 行都写出、只有第 1 个执行行带 LLM 字段、``cum_reward`` 逐行累加。
    """
    env = FakeEnv(steps_to_terminate=3, reward=0.5)
    policy = FakePolicy(scripts=[[
        {"action": {"kind": "play_face", "dieId": "d-0001", "slotId": "slot_1"}},
        {"reject": {"stage": "semantic", "reason": "slot occupied",
                    "action": {"kind": "play_face", "dieId": "d-0002", "slotId": "slot_1"}}},
        {"action": {"kind": "play_face", "dieId": "d-0003", "slotId": "slot_2"}},
        {"action": {"kind": "end_turn"}},
    ]])
    runner, _ = mk_runner(tmp_path, policy=policy, env=env)
    rec = runner.run_episode(7, split="train", arm="train", harness_version=0)

    path = Path(rec.jsonl_path)
    import json

    parsed = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    steps = [r for r in parsed if r.get("type") != "episode_end"]
    assert len(steps) == 4, f"4 个 StepRecord 必须全部落盘，实际 {len(steps)}"
    assert [r["illegal"] for r in steps] == [False, True, False, False]
    assert steps[1]["illegal_reason"] == "slot occupied"
    # LLM 字段只挂在决策内第一个**执行**动作行上（Spec §4.5）
    assert steps[0]["llm_raw"] is not None
    assert steps[0]["prompt_hash"] is not None
    assert steps[0]["llm_latency_ms"] == 7
    for row in steps[1:]:
        assert row["llm_raw"] is None
        assert row["prompt_hash"] is None
        assert row["llm_latency_ms"] is None
    # cum_reward 逐行累加（reject 行也带 shaped -0.05）
    assert [r["cum_reward"] for r in steps] == [0.5, 0.45, 0.95, 1.45]
    assert rec.cum_reward == pytest.approx(1.45)
    assert rec.illegal_attempts == 1


def test_t06_3_step_rows_satisfy_trajectory_schema(tmp_path, mk_runner):
    """落盘的每一行都过 MOD-09 的 ``trajectory_line`` schema（G5 的实际用途）。"""
    from dice_ratchet.infra.logging import validate_trajectory_line

    runner, _ = mk_runner(tmp_path, policy=FakePolicy(), env=FakeEnv(steps_to_terminate=2))
    rec = runner.run_episode(1, split="train", arm="train", harness_version=0)
    import json

    for line in Path(rec.jsonl_path).read_text(encoding="utf-8").splitlines():
        validate_trajectory_line(json.loads(line))
