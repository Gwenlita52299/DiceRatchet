"""MOD-04 启发式单测（PRD §11 U9 + FR-04-14）。

JS 基准：``src/game/tests/golden/g5_hard_six_stage.jsonl`` 的逐步动作由 JS
``greedy_counter`` 产生（``tests/golden/test_bot_match.py`` 守卫），因此"逐步对拍"
= 用 Python 启发式在同一投影观测上复选同一动作。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from gamer_rsi.agent import EpisodeContext, PolicyAgent, semantic_match
from gamer_rsi.agent.heuristic import heuristic_policy, heuristic_policy_explained
from gamer_rsi.env import GameEngine, load_tables
from gamer_rsi.infra.config import PolicyConfig
from gamer_rsi.sandbox.api import EpisodeConfig, SandboxEnv

REPO = Path(__file__).resolve().parents[3]
G5 = REPO / "src/game/tests/golden/g5_hard_six_stage.jsonl"
#: 默认局的 seed 数与 JS 参考通关率（Spec §8.6 唯一登记处，n=200、seed 1–200）
HEURISTIC_N = 200
SPEC_REFERENCE_WIN_RATE = 0.455


def _g5_lines() -> list[dict]:
    return [json.loads(line) for line in G5.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _g5_env() -> SandboxEnv:
    lines = _g5_lines()
    cfg = lines[0]["config"]
    env = SandboxEnv(engine=GameEngine(load_tables()),
                     episode_config=EpisodeConfig(
                         difficulty=cfg["difficulty"], stage_count=cfg["stageCount"],
                         toggles=cfg["toggles"], character=cfg.get("character", "warrior")),
                     episode_id="e-003100-g000-train")
    env.reset(cfg["seed"])
    return env


def test_u9_heuristic_matches_js_greedy_counter_on_golden_battles():
    """U9：战斗观测上 Python 启发式与 JS ``greedy_counter`` 逐步一致。"""
    env = _g5_env()
    lines = _g5_lines()
    compared = 0
    mismatches: list[tuple[dict, dict]] = []
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        obs = env.observe()
        if obs.get("phase") == "player_turn":
            expected = line["action"]
            chosen = heuristic_policy(obs, env.legal_actions(), env.hints())
            compared += 1
            if json.dumps(chosen, sort_keys=True) != json.dumps(expected, sort_keys=True):
                mismatches.append((expected, chosen))
        result = env.step(line["action"])
        if result.terminated or result.truncated:
            break
    env.close()
    assert compared > 100, f"g5 战斗步数异常：{compared}"
    assert not mismatches, f"与 JS greedy_counter 分歧 {len(mismatches)} 处：{mismatches[:3]}"


def test_heuristic_is_line_by_line_port_when_obs_has_deck(engine):
    """FR-04-14「逐行移植」的最强证明：观测带 ``deck`` 时**全 phase** 与参考实现零分歧。

    投影观测 ``obs-agent-1.0.0`` 不含 ``deck``（MOD-02 契约），此时本模块用
    ``shop.removable``（等价于全牌库 dieId 列表）等公开代理近似；本用例用含 deck 的
    raw obs 验证"移植本体"没有任何逻辑偏差。
    """
    import sys

    sys.path.insert(0, str(REPO / "tests" / "golden"))
    from greedy_bot import bot_rng, choose_action

    mismatches: list[tuple] = []
    decisions = 0
    for seed in (1, 2, 7, 10):
        state = engine.create({"difficulty": "hard", "seed": seed, "toggles": {},
                               "stageCount": 6, "runId": f"r-port-{seed}"})
        rnd = bot_rng(seed + 7)
        steps = 0
        while state["phase"] != "settle" and steps < 1500:
            expected = choose_action(state, engine, "greedy_counter", rnd)
            obs = engine.observe(state, "full")
            legal = [a for a in engine.legal_actions(state) if a.get("kind") != "abandon_run"]
            got = heuristic_policy(obs, legal, engine.play_hints(state))
            decisions += 1
            if got != expected:
                mismatches.append((seed, state["phase"], expected, got))
            state = engine.reduce(state, expected)["state"]
            steps += 1
    assert decisions > 800
    assert not mismatches, f"移植本体与参考实现分歧：{mismatches[:3]}"


def test_heuristic_always_returns_legal_action_across_phases(make_env):
    """FR-04-14：只返回合法动作；整局跑通且零非法。"""
    phases_seen: set[str] = set()
    for seed in (1000, 1001, 1002):
        env = make_env(seed=seed)
        steps = 0
        while steps < 400:
            obs = env.observe()
            phases_seen.add(str(obs.get("phase")))
            legal = env.legal_actions()
            action = heuristic_policy(obs, legal, env.hints())
            if not legal:
                break
            ok, detail, _ = semantic_match(action, legal)
            assert ok, f"seed={seed} 非法启发式动作 {action}: {detail}"
            result = env.step(action)
            steps += 1
            if result.terminated or result.truncated:
                break
        env.close()
    assert {"map", "player_turn"} <= phases_seen


def test_heuristic_is_deterministic_and_explainable(make_env):
    env = make_env(seed=1000)
    obs = env.observe()
    legal = env.legal_actions()
    first, reason = heuristic_policy_explained(obs, legal, env.hints())
    second = heuristic_policy(obs, legal, env.hints())
    assert first == second
    assert reason.startswith("heuristic:")
    assert heuristic_policy(obs, [], {}) == {}
    env.close()


def test_heuristic_reasoning_format_for_battle(make_env):
    env = make_env(seed=1000)
    obs = env.observe()
    for _ in range(80):
        if obs.get("phase") == "player_turn":
            break
        env.step(heuristic_policy(obs, env.legal_actions(), env.hints()))
        obs = env.observe()
    action, reason = heuristic_policy_explained(obs, env.legal_actions(), env.hints())
    assert action.get("kind") in {"play_face", "purify", "discard_die", "use_ability", "end_turn"}
    assert reason.startswith("heuristic:")
    # 对拍格式示例："heuristic:counter play_face d-0007→slot_1 e-00 score=12.5"
    if action.get("kind") == "play_face":
        assert "play_face" in reason and "\u2192" in reason
    env.close()


def test_baseline_heuristic_mode_never_calls_llm(make_env):
    class ExplodingClient:
        def generate_json(self, *args, **kwargs): # pragma: no cover - 不应被调用
            raise AssertionError("baseline_heuristic 不得调用 LLM")

    env = make_env(seed=1000)
    agent = PolicyAgent(PolicyConfig(mode="baseline_heuristic"), ExplodingClient())
    ctx = EpisodeContext(run="r", episode="e-001000-g000-train", seed=1000,
                         split="heldout", mode="eval")
    agent.reset(ctx)
    decision = agent.play_decision(env, env.observe(), ctx=ctx)
    assert decision.source == "heuristic"
    assert decision.llm is None
    assert decision.prompt_hash == ""
    assert decision.steps and decision.steps[0].action
    assert not decision.steps[0].illegal
    env.close()


def test_heuristic_full_episode_matches_known_outcome(make_env):
    """seed=1000 默认局：启发式整局可判定胜负（回归用，不锁死具体结局）。"""
    env = make_env(seed=1000)
    agent = PolicyAgent(PolicyConfig(mode="baseline_heuristic"))
    ctx = EpisodeContext(run="r", episode="e-001000-g000-train", seed=1000)
    agent.reset(ctx)
    decision = agent.play_decision(env, env.observe(), ctx=ctx)
    guard = 0
    while not (decision.last.terminated or decision.last.truncated) and guard < 400:
        decision = agent.play_decision(env, decision.last.observation, ctx=ctx)
        guard += 1
    assert decision.last.terminated or decision.last.truncated
    assert guard < 400
    env.close()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_heuristic_never_illegal_multiple_seeds(make_env, seed):
    env = make_env(seed=seed)
    steps = 0
    while steps < 500:
        legal = env.legal_actions()
        if not legal:
            break
        action = heuristic_policy(env.observe(), legal, env.hints())
        result = env.step(action)
        assert not result.info.get("illegal"), f"seed={seed} 产生非法动作 {action}"
        steps += 1
        if result.terminated or result.truncated:
            break
    env.close()


def _play_projected_episode(engine: GameEngine, seed: int) -> str | None:
    """用 MOD-04 启发式在**投影观测**上跑完默认局，返回 ``outcome``。"""
    env = SandboxEnv(engine=engine, episode_config=EpisodeConfig(difficulty="hard",
                                                                 stage_count=6))
    obs = env.reset(seed)
    steps = 0
    result = None
    while steps < 1000:
        legal = env.legal_actions()
        if not legal:
            break
        result = env.step(heuristic_policy(obs, legal, env.hints()))
        obs = result.observation
        steps += 1
        if result.terminated or result.truncated:
            break
    env.close()
    return None if result is None else result.info.get("outcome")


@pytest.mark.slow
def test_heuristic_win_rate_within_3pp_of_js_reference_on_default_run(engine):
    """FR-04-14 / Spec §8.6：默认局（hard × 6 关，seed 1..200）通关率与 JS 参考差距 ≤ 3pp。

    参考值 ``0.455`` 是 **Spec §8.6 登记**的 JS ``greedy_counter`` 通关率
    （n=200，seed 1–200，复现命令 ``node src/game/tests/bot-match.js 200 6 hard 1 --stats``），
    由 ``tests/golden/test_bot_match.py`` 持续守卫，因此本用例只跑 MOD-04 一侧。
    """
    wins = 0
    for index in range(HEURISTIC_N):
        wins += 1 if _play_projected_episode(engine, 1 + index) == "win" else 0
    rate = wins / HEURISTIC_N
    gap_pp = abs(rate - SPEC_REFERENCE_WIN_RATE) * 100.0
    assert gap_pp <= 3.0, (
        f"默认局通关率 {rate:.3f} 与 JS 参考 {SPEC_REFERENCE_WIN_RATE:.3f} "
        f"相差 {gap_pp:.1f}pp > 3pp（n={HEURISTIC_N}）；"
        "obs-agent 投影不含 deck，treasure/shop 分支只能用公开代理近似")
