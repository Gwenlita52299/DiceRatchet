"""MOD-02 golden（G2/G3/NFR-02-3）：g5 投影摘要、紧凑渲染、clean 审计报告。

golden 由 ``regen_sandbox_golden.py`` 显式生成；本文件只读比对。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, load_tables
from gamer_rsi.sandbox.api import EpisodeConfig, SandboxEnv
from gamer_rsi.sandbox.audit import run_audit
from gamer_rsi.sandbox.projection import render_compact

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FIXED_TS = "2026-09-30T12:00:00Z"


def _g5() -> list[dict]:
    path = REPO / "src/game/tests/golden/g5_hard_six_stage.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _replay() -> tuple[list[str], str]:
    lines = _g5()
    cfg = lines[0]["config"]
    env = SandboxEnv(engine=GameEngine(load_tables()),
                     episode_config=EpisodeConfig(
                         difficulty=cfg["difficulty"], stage_count=cfg["stageCount"],
                         toggles=cfg["toggles"], character=cfg.get("character", "warrior")),
                     episode_id="e-000001-g000-train")
    env.reset(cfg["seed"])
    digests = [env.last_obs_digest()]
    render = ""
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        env.step(line["action"])
        digests.append(env.last_obs_digest())
        if not render and env.observe()["phase"] == "player_turn":
            render = render_compact(env.observe())
    env.close()
    return digests, render


def test_g5_obs_digest_sequence_matches_golden():
    digests, _ = _replay()
    golden = (HERE / "g5_obs_agent.txt").read_text(encoding="utf-8").splitlines()
    assert len(digests) == len(golden), "步数变化：MOD-01 内核或投影规则被改动"
    for i, (got, want) in enumerate(zip(digests, golden)):
        assert got == want, f"第 {i} 步 obs_digest 分歧：{got} != {want}"


def test_g5_compact_render_matches_golden():
    _, render = _replay()
    want = (HERE / "g5_render.txt").read_text(encoding="utf-8")
    assert render == want


def test_g5_obs_digest_is_deterministic_across_runs():
    """G3：同 seed 双跑逐字节相同。"""
    first, render_a = _replay()
    second, render_b = _replay()
    assert first == second
    assert render_a == render_b


def test_clean_leak_report_matches_golden(tmp_path, build_run):
    """NFR-02-3：干净自测 run 的审计报告（除 generated_at）与 golden 相同。"""
    golden = json.loads((HERE / "leak-report-clean.json").read_text(encoding="utf-8"))
    built = build_run(tmp_path, run_id=golden["run_id"])
    result = run_audit(built.root, generated_at=FIXED_TS).to_dict()
    assert result == golden


@pytest.mark.parametrize("name", ["g5_obs_agent.txt", "g5_render.txt",
                                  "leak-report-clean.json"])
def test_golden_files_exist(name):
    assert (HERE / name).is_file()
