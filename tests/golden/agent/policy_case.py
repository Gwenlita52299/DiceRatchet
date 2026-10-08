"""MOD-04 prompt golden 的**固定输入**（regen 与测试共用，唯一事实来源）。

输入全部显式写死（不读墙钟、不读随机、不读 run 目录），因此在任何机器上都应得到同一
``prompt_hash``。观测取自 g5 golden 的第一个 ``player_turn`` 投影（经 MOD-02
``render_compact``），保证 golden 覆盖真实的战斗观测形态。
"""

from __future__ import annotations

import json
from pathlib import Path

from dice_ratchet.agent.prompts.assembler import (DEFAULT_TEMPLATE_PATH, MemoryItem,
                                               PromptContext, PromptSpec, ReflectionItem,
                                               load_task_text, load_template)
from dice_ratchet.env import GameEngine, load_tables
from dice_ratchet.sandbox.api import EpisodeConfig, SandboxEnv
from dice_ratchet.sandbox.projection import render_compact

__all__ = ["REPO", "HERE", "golden_spec", "golden_context", "golden_observation",
           "golden_filename"]

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
G5 = REPO / "src/game/tests/golden/g5_hard_six_stage.jsonl"

TASK = load_task_text()
TOOLS = "- rank_actions(obs, candidates, memory) -> list[dict]"
LEGAL = "\n".join([
    '{"dieId":"d-0001","kind":"play_face","slotId":"slot_1","targetId":"e-00"}',
    '{"dieId":"d-0002","kind":"discard_die"}',
    '{"kind":"end_turn"}',
])
CONSTRAINTS = "- error_code: E_ACTION_ILLEGAL\n- error_detail: 上一次动作不在合法集合内"
MEMORY = (
    MemoryItem("m-0002", "低德性时优先净化，避免自魔化。", score=0.71, version=3,
               action_hint={"kind": "purify"}),
    MemoryItem("m-0007", "克制箭头 up 的攻击骰收益更高。", score=0.64, version=3,
               action_hint={"kind": "play_face", "slotKind": "attack", "school": "blade"}),
    MemoryItem("m-0011", "打空手骰后立即 end_turn，避免浪费回合。", score=0.55, version=3),
)
REFLECTIONS = (
    ReflectionItem("r-0009", "本局第 2 关因护盾不足掉 2 心。", generation=3),
    ReflectionItem("r-0004", "第 1 关商店买了遗物，收益一般。", generation=2),
    ReflectionItem("r-0001", "开局地图选了精英节点，风险偏高。", generation=1),
)


def golden_observation() -> dict:
    """重放 g5 golden，取第一个 ``player_turn`` 的 agent 侧观测。"""
    lines = [json.loads(line) for line in G5.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    cfg = lines[0]["config"]
    env = SandboxEnv(engine=GameEngine(load_tables()),
                     episode_config=EpisodeConfig(
                         difficulty=cfg["difficulty"], stage_count=cfg["stageCount"],
                         toggles=cfg["toggles"], character=cfg.get("character", "warrior")),
                     episode_id="e-003100-g000-train")
    obs = env.reset(cfg["seed"])
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        obs = env.observe()
        if obs.get("phase") == "player_turn":
            env.close()
            return dict(obs)
        result = env.step(line["action"])
        if result.terminated or result.truncated:
            break
    env.close()
    raise AssertionError("g5 未出现 player_turn") # pragma: no cover


def golden_spec() -> PromptSpec:
    return load_template(DEFAULT_TEMPLATE_PATH)


def golden_context() -> PromptContext:
    return PromptContext(
        observation=render_compact(golden_observation()),
        task=TASK,
        harness_version=7,
        tools=TOOLS,
        legal_actions=LEGAL,
        reflections=REFLECTIONS,
        memory=MEMORY,
        constraints=CONSTRAINTS,
        obs_digest="sha256:" + "0" * 64,
        token_budget=3072,
        token_estimator="chars_div_3",
        reflection_max=3,
        memory_top_k=5,
        mode="play",
    )


def golden_filename(prompt_hash: str) -> str:
    """NFR-04-3：``policy_prompt_<hash>.txt``（取 hex 前 16 位，文件名避免过长）。"""
    return f"policy_prompt_{prompt_hash.split(':', 1)[1][:16]}.txt"
