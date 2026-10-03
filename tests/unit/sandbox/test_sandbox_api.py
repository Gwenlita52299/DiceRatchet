"""MOD-02 ``SandboxEnv`` 门面单测（PRD §11；FR-02-7/8/8a/8b/8c/24，INV-1）。"""

from __future__ import annotations

import pytest

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.sandbox.api import (ALL_ON_TOGGLES, EpisodeConfig, SandboxEnv, StepResult,
                                   make_default_env)
from gamer_rsi.sandbox.projection import obs_digest, validate_observation


# ---- FR-02-7 门面：不暴露 FullState -----------------------------------------------

@pytest.mark.parametrize("attr", ["engine", "state", "full_state", "fullState",
                                  "rng", "rng_state"])
def test_no_full_state_export(sandbox, attr):
    with pytest.raises(AttributeError):
        getattr(sandbox, attr)


def test_public_members_never_return_state(sandbox):
    """反射检查：任何公开成员的结果都不能是 GameState（含 ``stateVersion`` 键）。"""
    for name in dir(sandbox):
        if name.startswith("_"):
            continue
        member = getattr(sandbox, name)
        if not callable(member):
            continue
        try:
            value = member()
        except (InfraError, TypeError):
            continue
        assert not (isinstance(value, dict) and "stateVersion" in value), name
        assert not (isinstance(value, dict) and "rng" in value), name


def test_repr_hides_state(sandbox):
    text = repr(sandbox)
    assert "stateVersion" not in text and "rng" not in text
    assert "SandboxEnv" in text


# ---- FR-02-8 非法动作：不抛异常、reward -0.05、观测不变 ----------------------------

def test_illegal_action_is_shaped_not_raised(sandbox):
    before = sandbox.observe()
    result = sandbox.step({"kind": "play_face", "dieId": "d-nope", "slotId": "slot_1"})
    assert isinstance(result, StepResult)
    assert result.info["illegal"] is True
    assert result.reward == pytest.approx(-0.05)
    assert result.terminated is False
    after = result.observation
    # 除 lastResult 外观测不变（FR-02-8a）
    assert {k: v for k, v in after.items() if k != "lastResult"} == \
        {k: v for k, v in before.items() if k != "lastResult"}
    assert after["lastResult"]["illegal"] is True
    assert after["lastResult"]["reason"]
    assert result.info["obs_digest"] == sandbox.last_obs_digest()


def test_legal_step_advances_and_digest_changes(sandbox):
    before = sandbox.last_obs_digest()
    result = sandbox.step(sandbox.legal_actions()[0])
    assert result.info["illegal"] is False
    assert result.reward == 0.0
    assert result.info["obs_digest"] == sandbox.last_obs_digest() != before
    assert "state_hash" in result.info and result.info["state_hash"].startswith("sha256:")


# ---- FR-02-8a/8b reject 与唯一判负 -------------------------------------------------

def test_reject_does_not_change_state(sandbox):
    before = sandbox.observe()
    result = sandbox.reject(stage="syntax", reason="JSON 残缺", raw="{", final=False)
    assert result.reward == pytest.approx(-0.05)
    assert result.truncated is False
    after = result.observation
    assert {k: v for k, v in after.items() if k != "lastResult"} == \
        {k: v for k, v in before.items() if k != "lastResult"}
    assert after["lastResult"]["stage"] == "syntax"
    assert result.info["reject_stage"] == "syntax"


def test_three_final_rejects_trigger_policy_failure(sandbox):
    for i in range(2):
        result = sandbox.reject(stage="semantic", reason="非法动作", raw=None, final=True)
        assert result.truncated is False, i
    third = sandbox.reject(stage="semantic", reason="非法动作", raw=None, final=True)
    assert third.truncated is True
    assert third.info["truncation_reason"] == "policy_failure"
    assert third.info["illegal_count"] == 3


def test_successful_step_resets_final_streak(sandbox):
    sandbox.reject(stage="semantic", reason="x", raw=None, final=True)
    sandbox.reject(stage="semantic", reason="x", raw=None, final=True)
    sandbox.step(sandbox.legal_actions()[0]) # 成功决策清零
    result = sandbox.reject(stage="semantic", reason="x", raw=None, final=True)
    assert result.truncated is False


def test_non_final_rejects_do_not_trip_policy_failure(sandbox):
    for _ in range(5):
        assert sandbox.reject(stage="syntax", reason="x", raw=None,
                              final=False).truncated is False


def test_reject_stage_must_be_string(sandbox):
    with pytest.raises(InfraError) as exc:
        sandbox.reject(stage="", reason="x", raw=None, final=True)
    assert exc.value.code == E.E_ENV_SCHEMA


# ---- FR-02-8c legal_actions / hints -----------------------------------------------

def test_legal_actions_matches_mod01_without_abandon(sandbox, engine):
    ours = sandbox.legal_actions()
    theirs = [a for a in engine.legal_actions(sandbox._state) if a["kind"] != "abandon_run"]
    assert ours == theirs
    assert all(a["kind"] != "abandon_run" for a in ours)


def test_hints_forwarded(sandbox, engine, player_turn_state):
    env = SandboxEnv(engine=engine)
    env.reset(1000)
    # 进入战斗后 hints 与 MOD-01 play_hints 一致
    while env.observe()["phase"] != "player_turn":
        env.step(env.legal_actions()[0])
    assert env.hints() == engine.play_hints(env._state)
    assert env.hints()


def test_hints_empty_outside_player_turn(sandbox):
    assert sandbox.hints() == {}


# ---- 决策点序号与上限（B-12 / Spec §8.6）-----------------------------------------

def test_decision_index_is_per_turn_not_per_step(engine):
    """决策点 = player_turn 每回合一次；其余 phase 每步一次（Spec §4.3/B-12）。"""
    env = SandboxEnv(engine=engine)
    env.reset(1000)
    moves = 0
    while moves < 25:
        obs = env.observe()
        key = (obs["phase"], obs["turn"])
        actions = env.legal_actions()
        if not actions:
            break
        env.step(actions[0])
        moves += 1
        after = env.observe()
        if obs["phase"] == "player_turn" and (after["phase"], after["turn"]) == key:
            assert after["decisionIndex"] == obs["decisionIndex"] # 同一决策点
        else:
            assert after["decisionIndex"] == obs["decisionIndex"] + 1


def test_step_limit_truncates(engine):
    env = SandboxEnv(engine=engine, max_env_steps=3, max_decisions=100)
    env.reset(1000)
    for _ in range(3):
        result = env.step(env.legal_actions()[0])
    assert result.truncated is True
    assert result.info["truncation_reason"] == "step_limit"


def test_decision_limit_truncates(engine):
    env = SandboxEnv(engine=engine, max_decisions=1, max_env_steps=500)
    env.reset(1000)
    result = env.step(env.legal_actions()[0]) # map 阶段每步一个决策点
    assert result.truncated is True
    assert result.info["truncation_reason"] == "decision_limit"


# ---- 观测内容与生命周期 ------------------------------------------------------------

def test_observation_fields_and_validity(sandbox):
    obs = sandbox.observe()
    assert validate_observation(obs) == []
    assert obs["schemaVersion"] == "obs-agent-1.0.0"
    assert obs["episodeId"] == "e-001000-g000-train"
    assert obs["stageCount"] == 6 and obs["decisionIndex"] == 0
    assert obs["lastResult"] is None


def test_reset_accepts_episode_config_and_episode_id(engine):
    env = SandboxEnv(engine=engine)
    obs = env.reset(1234, episode_config=EpisodeConfig(difficulty="medium", stage_count=3,
                                                       toggles=dict(ALL_ON_TOGGLES)),
                    episode_id="e-001234-g001-cand-a")
    assert obs["episodeId"] == "e-001234-g001-cand-a"
    assert obs["stageCount"] == 3 and obs["difficulty"] == "medium"


def test_episode_config_from_mapping_and_mod09_style_object():
    cfg = EpisodeConfig.from_any({"difficulty": "easy", "stage_count": 3})
    assert cfg.difficulty == "easy" and cfg.stage_count == 3
    assert cfg.toggles == ALL_ON_TOGGLES

    class Mod09Style:
        difficulty = "hard"
        stage_count = 6
        character = "warrior"
        max_env_steps = 1000
        max_decisions = 300

    assert EpisodeConfig.from_any(Mod09Style()).stage_count == 6


def test_close_then_use_raises(engine):
    env = SandboxEnv(engine=engine)
    env.reset(1000)
    env.close()
    with pytest.raises(InfraError) as exc:
        env.observe()
    assert exc.value.code == E.E_ENV_SCHEMA


def test_step_before_reset_raises(engine):
    env = SandboxEnv(engine=engine)
    with pytest.raises(InfraError) as exc:
        env.observe()
    assert exc.value.code == E.E_ENV_SCHEMA


def test_context_manager_closes(engine):
    with SandboxEnv(engine=engine) as env:
        env.reset(1000)
        assert env.observe()["phase"] == "map"
    with pytest.raises(InfraError):
        env.observe()


def test_projection_stat_has_counts_only(engine):
    env = SandboxEnv(engine=engine)
    env.reset(1000)
    stat = env.projection_stat()
    assert set(stat) == {"kept", "dropped_hidden", "missing_optional"}
    assert all(isinstance(v, int) for v in stat.values())


# ---- 蜜罐开关不改变合法观测 --------------------------------------------------------

def test_honeypot_does_not_change_observation(engine):
    with_hp = SandboxEnv(engine=engine, honeypot=True)
    without = SandboxEnv(engine=engine, honeypot=False)
    obs_a = with_hp.reset(1000, episode_id="e-1")
    obs_b = without.reset(1000, episode_id="e-1")
    assert obs_digest(obs_a) == obs_digest(obs_b)


def test_assert_projection_mode_reports_violations(engine):
    env = SandboxEnv(engine=engine, projection_mode="assert")
    obs = env.reset(1000, episode_id="e-1")
    assert validate_observation(obs) == []


def test_render_compact_from_env(sandbox):
    text = sandbox.render_compact()
    assert "phase=map" in text and text.endswith("\n")


def test_make_default_env_runs():
    env = make_default_env()
    obs = env.reset(1, episode_id="e-000001-g000-train")
    assert obs["phase"] == "map"
    env.close()
