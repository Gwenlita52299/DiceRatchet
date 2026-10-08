"""全测试共享夹具：合成 run 目录（轨迹/记忆/prompt）与真实 MOD-01 引擎。

MOD-02 的审计与 golden 都要一个**可重放**的 run 目录：轨迹里的 seed/动作必须能被
``GameEngine.reduce`` 原样重放，否则 ``audit`` 会把重放降级记进 ``warnings``。
本文件的 ``build_sandbox_run`` 用 SandboxEnv 真跑一局（确定性脚本策略），再落盘
Spec §4.5 的目录布局。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from dice_ratchet.env import GameEngine, load_tables
from dice_ratchet.sandbox.api import EpisodeConfig, SandboxEnv

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXED_TS = "2026-09-30T12:00:00Z"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def game_tables() -> Mapping[str, Any]:
    return load_tables()


@pytest.fixture(scope="session")
def game_engine(game_tables: Mapping[str, Any]) -> GameEngine:
    return GameEngine(game_tables)


@dataclass(frozen=True)
class BuiltRun:
    """``build_sandbox_run`` 的产物（路径 + 关键事实）。"""

    root: Path
    run_id: str
    episode_key: str
    episode_path: Path
    prompt_path: Path
    memory_path: Path
    digests: tuple[str, ...]
    steps: int
    phase_seq: tuple[str, ...]
    first_player_turn: Mapping[str, Any] | None


def _trajectory_row(*, episode_key: str, generation: int, harness_version: int, arm: str,
                    seed: int, split: str, index: int, decision_index: int,
                    obs_digest: str, action: Mapping[str, Any], reward: float,
                    cum_reward: float) -> dict[str, Any]:
    return {
        "episode_key": episode_key, "generation": generation,
        "harness_version": harness_version, "arm": arm, "seed": seed, "split": split,
        "step": index, "decision_index": decision_index, "plan_index": 0, "attempt": 1,
        "source": "scripted", "obs_digest": obs_digest, "action": dict(action),
        "reward": round(reward, 4), "cum_reward": round(cum_reward, 4), "illegal": False,
        "illegal_reason": None, "llm_raw": None, "llm_latency_ms": None,
        "prompt_tokens": None, "completion_tokens": None,
        "prompt_hash": None, "memory_ids": None, "tool_hash": None,
        "mode": "train", "ts": FIXED_TS,
    }


def build_sandbox_run(
    root: Path, *, run_id: str = "1111111-abcdef12-20260930T120000Z",
    difficulty: str = "hard", stage_count: int = 6, seed: int = 1000,
    max_steps: int = 40, split: str = "train", generation: int = 0,
    harness_version: int = 1, arm: str = "train",
    prompt_text: str | None = None, memory_text: str | None = None,
    engine: GameEngine | None = None, corrupt_log: bool = False,
    honeypot: bool = True, x_obs_keys: bool = True,
) -> BuiltRun:
    """在 ``root`` 下合成一个可重放的 run 目录。"""
    from dice_ratchet.sandbox import obs_field_paths

    engine = engine or GameEngine(load_tables())
    episode_key = f"e-{seed:06d}-g{generation:03d}-{arm}"
    env = SandboxEnv(engine=engine, honeypot=honeypot,
                     episode_config=EpisodeConfig(difficulty=difficulty,
                                                  stage_count=stage_count))
    env.reset(seed, episode_id=episode_key)
    obs = env.observe()
    rows: list[dict[str, Any]] = []
    digests: list[str] = []
    phases: list[str] = []
    first_player_turn: Mapping[str, Any] | None = None
    cum = 0.0
    for index in range(max_steps):
        actions = env.legal_actions()
        if not actions:
            break
        result = env.step(actions[0])
        cum += result.reward
        obs = result.observation
        if first_player_turn is None and obs.get("phase") == "player_turn":
            first_player_turn = obs
        row = _trajectory_row(
            episode_key=episode_key, generation=generation,
            harness_version=harness_version, arm=arm, seed=seed, split=split,
            index=index, decision_index=int(obs.get("decisionIndex", 0)),
            obs_digest=env.last_obs_digest(), action=actions[0], reward=result.reward,
            cum_reward=cum)
        if x_obs_keys and index == 0:
            row["x_obs_keys"] = list(obs_field_paths())
        rows.append(row)
        digests.append(env.last_obs_digest())
        phases.append(str(obs.get("phase")))
        if result.terminated or result.truncated:
            break
    if first_player_turn is None:
        # 地图阶段也能取到观测，退化为首个观测即可（golden 需要非空）
        first_player_turn = obs
    env.close()

    run_root = Path(root) / run_id
    (run_root / "episodes" / split).mkdir(parents=True, exist_ok=True)
    (run_root / "prompts").mkdir(parents=True, exist_ok=True)
    (run_root / "memory").mkdir(parents=True, exist_ok=True)
    (run_root / "audit").mkdir(parents=True, exist_ok=True)
    (run_root / "config.json").write_text(json.dumps({
        "run_id": run_id, "git_sha": "1111111",
        "config_hash": "sha256:0000000000000000",
        "config": {"env": {"episode": {"difficulty": difficulty,
                                       "stage_count": stage_count,
                                       "character": "warrior",
                                       "max_env_steps": 1000, "max_decisions": 300}},
                   "infra": {"runs_root": "runs"}},
        "harness_config_path": "harness/config.json"}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    end_row = {
        "type": "episode_end", "episode_key": episode_key, "generation": generation,
        "harness_version": harness_version, "arm": arm, "seed": seed, "split": split,
        "outcome": "lose" if obs.get("outcome") in (None, "lose") else "win",
        "truncation_reason": obs.get("truncationReason"), "stages_cleared": 1,
        "hearts_left": 0, "battles_won": 1, "decisions": len(rows), "degraded": False,
        "exclude_reason": None, "ts": FIXED_TS,
    }
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in [*rows, end_row])
    if corrupt_log:
        body = body.replace('"step":', '"step"::', 1)
    episode_path = run_root / "episodes" / split / f"{episode_key}.jsonl"
    episode_path.write_text(body + "\n", encoding="utf-8")

    prompt_path = run_root / "prompts" / f"{episode_key}.txt"
    prompt_path.write_text(
        prompt_text if prompt_text is not None
        else f"# decision prompt\nepisode={episode_key}\n请根据观测选择动作。\n",
        encoding="utf-8")
    memory_path = run_root / "memory" / f"m-{episode_key}.jsonl"
    memory_path.write_text(json.dumps(
        {"id": "m-0001", "episode_key": episode_key,
         "text": memory_text if memory_text is not None else "本局教训：优先处理护卫。"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return BuiltRun(root=run_root, run_id=run_id, episode_key=episode_key,
                    episode_path=episode_path, prompt_path=prompt_path,
                    memory_path=memory_path, digests=tuple(digests), steps=len(rows),
                    phase_seq=tuple(phases), first_player_turn=first_player_turn)


@pytest.fixture
def build_run() -> Callable[..., BuiltRun]:
    """返回合成 run 的工厂（``tmp_path`` 由调用方给出或默认临时目录）。"""
    return build_sandbox_run


@pytest.fixture
def g5_lines(repo_root: Path) -> list[dict[str, Any]]:
    """``g5_hard_six_stage`` 的 JS golden 行（MOD-01 对拍基准，本模块只读）。"""
    path = repo_root / "src/game/tests/golden/g5_hard_six_stage.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
