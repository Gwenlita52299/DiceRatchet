"""MOD-04 决策编排单测（PRD §11 U6/U10 + FR-04-9..16）。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from policy_support import ScriptedClient, plan

from gamer_rsi.agent import DecisionResult, EpisodeContext, PolicyAgent
from gamer_rsi.agent.heuristic import heuristic_policy
from gamer_rsi.infra.config import PolicyConfig
from gamer_rsi.infra.errors import E
from gamer_rsi.inference import InferenceError
from gamer_rsi.sandbox.api import StepResult

BAD_JSON = "这不是 JSON"
GOOD_JSON = plan({"kind": "end_turn"}, reasoning="收工")


def _agent(cfg, client, harness_dir):
    agent = PolicyAgent(cfg, client, harness_dir=harness_dir)
    return agent


def _ctx(seed: int = 1000) -> EpisodeContext:
    return EpisodeContext(run="r-test", episode=f"e-{seed:06d}-g000-train", seed=seed,
                          split="train", mode="train", tick=1)


# ----------------------------------------------------------------------
# 基本信息
# ----------------------------------------------------------------------

def test_decision_result_fields_match_contract():
    names = {f.name for f in dataclasses.fields(DecisionResult)}
    assert {"steps", "source", "attempts", "llm", "memory_ids", "adopted_memory_ids",
            "prompt_hash", "tool_hash", "reasoning", "last", "error_codes"} <= names


def test_happy_path_fields(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([plan(action, reasoning="按计划行动")])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs, ctx=_ctx())
    assert decision.source == "llm"
    assert decision.attempts == 1
    assert decision.error_codes == []
    assert decision.reasoning == "按计划行动"
    assert decision.prompt_hash.startswith("sha256:")
    assert decision.template_hash.startswith("sha256:")
    assert decision.mode == "train"
    assert decision.llm is not None and decision.llm.raw
    assert decision.llm.prompt_tokens == 10 and decision.llm.completion_tokens == 5
    assert decision.steps and decision.steps[0].action == action
    assert decision.steps[0].source == "llm"
    assert decision.last is decision.steps[-1].result
    assert client.calls[0]["sampling"]["temperature"] == 0.7
    env.close()


# ----------------------------------------------------------------------
# 重试与轻罚（FR-04-9）
# ----------------------------------------------------------------------

def test_retry_records_reject_and_swaps_seed(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([BAD_JSON, plan(action)])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.source == "llm"
    assert decision.attempts == 2
    assert client.n_calls == 2
    assert decision.steps[0].illegal is True
    assert decision.steps[0].result.reward == pytest.approx(-0.05)
    assert E.E_ACTION_PARSE in decision.error_codes
    assert client.calls[0]["seed"] != client.calls[1]["seed"]
    # 重试 prompt 的 {{constraints}} 带上次错误
    retry_text = client.calls[1]["messages"][0].content
    assert "E_ACTION_PARSE" in retry_text
    env.close()


def test_retry_exhausted_final_reject_then_heuristic_fallback(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    client = ScriptedClient([BAD_JSON])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.source == "fallback"
    assert decision.attempts == 3
    assert client.n_calls == 3
    # 3 次尝试各一行 reject + 1 行 final reject + 1 行启发式动作
    assert len(decision.steps) == 5
    assert decision.steps[-1].action
    assert decision.steps[-1].source == "fallback"
    assert E.E_ACTION_FALLBACK in decision.error_codes
    assert sum(1 for s in decision.steps if s.illegal) == 4
    env.close()


def test_llm_schema_invalid_counts_as_failed_attempt(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    action = dict(env.legal_actions()[0])
    error = InferenceError(E.E_LLM_SCHEMA_INVALID, "三级修复耗尽")
    client = ScriptedClient([error, plan(action)])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.source == "llm"
    assert decision.attempts == 2
    assert E.E_LLM_SCHEMA_INVALID in decision.error_codes
    assert decision.steps[0].illegal is True
    env.close()


def test_heuristic_fallback_disabled_ends_decision(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    client = ScriptedClient([BAD_JSON])
    agent = _agent(PolicyConfig(heuristic_fallback=False), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.steps[-1].action is None
    assert all(s.illegal for s in decision.steps)
    env.close()


# ----------------------------------------------------------------------
# 模型不可用 / 降级（§10）
# ----------------------------------------------------------------------

def test_llm_unavailable_degrades_immediately(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    client = ScriptedClient([InferenceError(E.E_LLM_UNAVAILABLE, "服务未启动")])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.degraded is True
    assert decision.source == "fallback"
    assert client.n_calls == 1
    assert E.E_LLM_UNAVAILABLE in decision.error_codes
    assert decision.steps[-1].action # 启发式仍出招
    env.close()


def test_llm_timeout_degrades(make_env, harness_dir):
    env = make_env(seed=1000)
    client = ScriptedClient([InferenceError(E.E_LLM_TIMEOUT, "超时")])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, env.observe())
    assert decision.degraded is True and E.E_LLM_TIMEOUT in decision.error_codes
    env.close()


# ----------------------------------------------------------------------
# 泄漏阻断 / 预算（FR-04-6 / §10）
# ----------------------------------------------------------------------

def test_leak_block_never_calls_llm(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    client = ScriptedClient([GOOD_JSON])

    class Finding:
        leak_class = "seed_leak"
        surface = "prompt"
        token_class = "seed"
        evidence = "2013"

    agent = PolicyAgent(PolicyConfig(), client, harness_dir=harness_dir,
                        scanner=lambda text, surface: [Finding()])
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert client.n_calls == 0
    assert E.E_ACTION_LEAK_BLOCK in decision.error_codes
    assert decision.source == "fallback"
    env.close()


def test_budget_overflow_bypasses_llm(make_env, harness_dir):
    env = make_env(seed=1000)
    client = ScriptedClient([GOOD_JSON])
    agent = _agent(PolicyConfig(prompt_token_budget=256), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, env.observe())
    assert client.n_calls == 0
    assert E.E_ACTION_BUDGET in decision.error_codes
    assert decision.source == "fallback"
    env.close()


# ----------------------------------------------------------------------
# ② 语义 / ③ 执行（FR-04-7/8）
# ----------------------------------------------------------------------

def test_semantic_illegal_uses_fresh_legal_actions(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    legal = env.legal_actions()
    bogus = {"kind": "map_choose", "nodeId": "n-99-9-9"}
    client = ScriptedClient([plan(bogus), plan(dict(legal[0]))])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.source == "llm"
    assert E.E_ACTION_ILLEGAL in decision.error_codes
    assert decision.steps[0].illegal is True
    assert decision.steps[0].action is None # 非法动作不执行
    env.close()


def test_exec_illegal_is_consumed_not_overridden(make_env, harness_dir, monkeypatch):
    env = make_env(seed=1000)
    obs = env.observe()
    action = dict(env.legal_actions()[0])
    real_step = env.step

    def fake_step(a):
        result = real_step(a)
        return StepResult(observation=result.observation, reward=-0.05, terminated=False,
                          truncated=False,
                          info={**dict(result.info), "illegal": True,
                                "illegal_reason": "内核拒绝"})

    monkeypatch.setattr(env, "step", fake_step)
    client = ScriptedClient([plan(action), plan(action)])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert E.E_ACTION_EXEC in decision.error_codes
    assert decision.steps[0].illegal is True
    assert "内核拒绝" in str(decision.steps[0].illegal_reason)
    env.close()


def test_leaving_player_turn_discards_remaining_actions(make_env, harness_dir, monkeypatch):
    """FR-04-7：离开 player_turn 后剩余动作直接丢弃，不计非法。"""
    env = make_env(seed=1000)
    obs = env.observe()
    for _ in range(80):
        if obs.get("phase") == "player_turn":
            break
        env.step(heuristic_policy(obs, env.legal_actions(), env.hints()))
        obs = env.observe()
    legal = env.legal_actions()
    first = next((a for a in legal if a["kind"] == "end_turn"), None) or dict(legal[0])
    real_step = env.step

    def phase_changing_step(action):
        result = real_step(action)
        # 模拟"执行第一个动作后离开玩家回合"（战斗结束 / 进入敌方回合）
        return StepResult(observation={**dict(result.observation), "phase": "map"},
                          reward=result.reward, terminated=result.terminated,
                          truncated=result.truncated, info=result.info)

    monkeypatch.setattr(env, "step", phase_changing_step)
    client = ScriptedClient([plan(first, {"kind": "map_choose", "nodeId": "nope"})])
    agent = _agent(PolicyConfig(), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert decision.source == "llm"
    assert client.n_calls == 1 # 未因第二个动作非法而重试
    assert E.E_ACTION_ILLEGAL not in decision.error_codes
    assert len([s for s in decision.steps if s.action]) == 1
    env.close()


# ----------------------------------------------------------------------
# U6：连续非法判负（判负方是 MOD-02）
# ----------------------------------------------------------------------

def test_u6_illegal_streak_is_judged_by_sandbox(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    client = ScriptedClient([BAD_JSON])
    agent = _agent(PolicyConfig(heuristic_fallback=False), client, harness_dir)
    agent.reset(_ctx())
    calls_before = 0
    last = None
    for _ in range(5):
        decision = agent.play_decision(env, obs)
        obs = decision.last.observation
        assert client.n_calls - calls_before <= 3 # 每个决策最多 3 次 LLM
        calls_before = client.n_calls
        last = decision
        if decision.last.truncated or decision.last.terminated:
            break
    assert last is not None and last.last.truncated is True
    assert last.last.info["truncation_reason"] == "policy_failure"
    # Spec §4.3：truncated → 主指标计为 lose；MOD-04 只读 info 并补记错误码
    assert (last.last.info.get("outcome") or "lose") == "lose"
    assert E.E_ACTION_ILLEGAL_STREAK in last.error_codes
    env.close()


# ----------------------------------------------------------------------
# U10：模式与采样（FR-04-15）
# ----------------------------------------------------------------------

def test_u10_eval_mode_temperature_zero_and_explicit_seed(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([plan(action), plan(action)])
    agent = _agent(PolicyConfig(mode="eval", temperature=1.2), client, harness_dir)
    ctx = EpisodeContext(run="r", episode="e-002000-g000-parent", seed=2000,
                         split="heldout", mode="eval")
    agent.reset(ctx)
    agent.play_decision(env, obs, ctx=ctx)
    call = client.calls[0]
    assert call["mode"] == "eval"
    assert call["sampling"]["temperature"] == 0.0
    assert call["sampling"]["top_p"] == 1.0
    assert isinstance(call["seed"], int) and call["seed"] > 0
    env.close()


def test_u10_retry_seed_changes_with_attempt(make_env, harness_dir):
    env = make_env(seed=1000)
    client = ScriptedClient([BAD_JSON, BAD_JSON, BAD_JSON])
    agent = _agent(PolicyConfig(mode="eval"), client, harness_dir)
    agent.reset(_ctx())
    agent.play_decision(env, env.observe())
    seeds = [c["seed"] for c in client.calls]
    assert len(seeds) == len(set(seeds)) == 3
    env.close()


@pytest.mark.parametrize("configured,expected", [(0.1, 0.7), (0.7, 0.7), (1.0, 1.0), (1.2, 1.2)])
def test_play_mode_temperature_clamped(make_env, harness_dir, configured, expected):
    env = make_env(seed=1000)
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([plan(action)])
    agent = _agent(PolicyConfig(temperature=configured), client, harness_dir)
    agent.reset(_ctx())
    agent.play_decision(env, env.observe())
    assert client.calls[0]["sampling"]["temperature"] == pytest.approx(expected)
    assert client.calls[0]["mode"] == "train"
    env.close()


def test_baseline_fixed_prompt_is_eval_like(make_env, harness_dir):
    env = make_env(seed=1000)
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([plan(action)])
    agent = _agent(PolicyConfig(mode="baseline_fixed_prompt"), client, harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, env.observe())
    assert client.calls[0]["mode"] == "eval"
    assert client.calls[0]["sampling"]["temperature"] == 0.0
    assert decision.template_hash
    env.close()


# ----------------------------------------------------------------------
# adopted_memory_ids（MOD-05 §6.4）
# ----------------------------------------------------------------------

class _Hit:
    def __init__(self, entry_id, hint):
        self.entry_id = entry_id
        self.text = "lesson"
        self.utility = 0.5
        self.harness_version = 0
        self.action_hint = hint


class _Result:
    def __init__(self, entries):
        self.entries = entries
        self.text = "rendered"


class _Memory:
    def __init__(self, entries):
        self.entries = entries
        self.calls = 0

    def retrieve(self, obs, *, top_k, token_budget, tick):
        self.calls += 1
        return _Result(self.entries)


def test_adopted_memory_ids_tracks_action_hint(make_env, harness_dir):
    env = make_env(seed=1000)
    obs = env.observe()
    action = dict(env.legal_actions()[0])
    memory = _Memory([_Hit("m-hit", {"kind": action["kind"]}),
                      _Hit("m-miss", {"kind": "forge_pick"})])
    client = ScriptedClient([plan(action)])
    agent = PolicyAgent(PolicyConfig(), client, memory, harness_dir=harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, obs)
    assert memory.calls == 1
    assert decision.memory_ids == ["m-hit", "m-miss"]
    assert decision.adopted_memory_ids == ["m-hit"]
    env.close()


def test_memory_reader_failure_does_not_block_decision(make_env, harness_dir):
    class Broken:
        def retrieve(self, obs, *, top_k, token_budget, tick):
            raise RuntimeError("memory down")

    env = make_env(seed=1000)
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([plan(action)])
    agent = PolicyAgent(PolicyConfig(), client, Broken(), harness_dir=harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, env.observe())
    assert decision.source == "llm"
    assert decision.memory_ids == []
    env.close()


# ----------------------------------------------------------------------
# 工具失败不穿透
# ----------------------------------------------------------------------

def test_tool_failure_does_not_break_decision(make_env, make_harness_dir):
    env = make_env(seed=1000)
    action = dict(env.legal_actions()[0])
    client = ScriptedClient([plan(action)])
    agent = _agent(PolicyConfig(), client, make_harness_dir("boom", {
        "boom.py": "@harness_tool\ndef rank_actions(obs, candidates, memory):\n"
                  "    return 1 // 0\n"}))
    agent.reset(_ctx())
    decision = agent.play_decision(env, env.observe())
    assert decision.source == "llm"
    assert E.E_ACTION_TOOL_FAIL in decision.error_codes
    agent.close()
    env.close()


def test_no_llm_configured_falls_back(make_env, harness_dir):
    env = make_env(seed=1000)
    agent = PolicyAgent(PolicyConfig(), None, harness_dir=harness_dir)
    agent.reset(_ctx())
    decision = agent.play_decision(env, env.observe())
    assert decision.degraded is True
    assert E.E_LLM_UNAVAILABLE in decision.error_codes
    assert decision.steps[-1].action
    env.close()
