"""执行器 / IO / reward 的运行期单测（补齐 G1 覆盖率，均为确定性轻量用例）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.infra.config import load_config
from gamer_rsi.evals import seeds as S
from gamer_rsi.evals.baselines import run_baseline
from gamer_rsi.evals.harness import (append_generations_csv, episode_result_from_rows,
                                     evaluate_arm, evaluate_generation,
                                     load_episode_results, write_ext_metrics,
                                     write_plots_data)
from gamer_rsi.evals.metrics import EpisodeResult, aggregate
from gamer_rsi.evals import reward

_PROTO = S.default_protocol()
_CFG = load_config()


# ---- reward.py（只供调试的 shaped reward）------------------------------------------

def test_shaped_reward_illegal_and_win():
    assert reward.shaped_reward(illegal=True) == reward.ILLEGAL_PENALTY
    win = reward.shaped_reward(outcome="win", stages_cleared=6, battles_won=10,
                               hearts_left=2, decisions=90)
    assert win == pytest.approx(1.0 + 0.6 + 0.1 + 0.04 - 0.09)
    truncated = reward.shaped_reward(outcome="lose", truncated=True)
    assert truncated == pytest.approx(reward.TRUNCATION_PENALTY)


def test_shaped_reward_delta_form_and_cumulative():
    prev = {"stages_cleared": 1, "battles_won": 2, "hearts_left": 1, "decisions": 10}
    cur = {"stages_cleared": 2, "battles_won": 3, "hearts_left": 1, "decisions": 11}
    delta = reward.shaped_reward(previous=prev, current=cur)
    assert delta == pytest.approx(reward.STAGE_BONUS + reward.BATTLE_BONUS
                                  - reward.DECISION_COST)
    assert reward.cumulative_reward([0.1, 0.2]) == pytest.approx(0.3)
    assert reward.mean_cum_reward_debug([[1.0], [3.0]]) == pytest.approx(2.0)
    assert reward.mean_cum_reward_debug([]) == 0.0


# ---- 轨迹 → EpisodeResult / 报告 IO -------------------------------------------------

def test_episode_result_from_rows_and_load(tmp_path):
    row = {"type": "episode_end", "seed": 2000, "outcome": "win",
           "truncation_reason": None, "stages_cleared": 6, "hearts_left": 2,
           "battles_won": 15, "decisions": 90, "steps": 210, "illegal_attempts": 1,
           "fallback_decisions": 0, "kind_counts": [0] * 16, "decision_kinds": [0, 1],
           "slot_counts": {"attack×blade": 3}, "seq_hash": "h", "degraded": False}
    result = episode_result_from_rows([row], seed=2000, split="heldout", generation=3,
                                      harness_version=2)
    assert result is not None and result.decision_kinds == (0, 1)
    assert episode_result_from_rows([{"type": "x"}], seed=1, split="heldout",
                                    generation=0, harness_version=0) is None
    split_dir = tmp_path / "episodes" / "heldout"
    split_dir.mkdir(parents=True)
    (split_dir / "e-002000-g003-parent.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")
    loaded = load_episode_results(tmp_path, "heldout", generation=3)
    assert len(loaded) == 1 and loaded[0].seed == 2000
    assert load_episode_results(tmp_path, "test") == []
    assert load_episode_results(tmp_path, "heldout", seeds=[9999]) == []


def test_report_io_helpers(tmp_path):
    ext = write_ext_metrics(tmp_path, 3, {"jsd_kind": 0.01})
    assert json.loads(ext.read_text(encoding="utf-8"))["jsd_kind"] == 0.01
    plots = write_plots_data(tmp_path, 3, [0.5, None], basket="H1", win_rate=0.4, n=40)
    data = json.loads(plots.read_text(encoding="utf-8"))
    assert data["buckets"][0]["h_norm"] == 0.5
    assert data["buckets"][1]["h_norm"] is None
    csv_path = append_generations_csv(tmp_path, {"generation": 3, "win_rate": 0.4})
    assert csv_path.is_file()
    append_generations_csv(tmp_path, {"generation": 4, "win_rate": 0.5})
    assert len(csv_path.read_text(encoding="utf-8").strip().splitlines()) == 3


# ---- SandboxRunner（真实 MOD-01/02 门面，INV-1）-------------------------------------

def test_run_baseline_random_real_env(tmp_path):
    arm = run_baseline("random", split="heldout", basket=[2000, 2001], run_id="r-run",
                       run_dir=tmp_path, config=_CFG, protocol=_PROTO, n_workers=1)
    assert arm.metrics.n == 2
    assert (tmp_path / "baselines" / "random-heldout.json").is_file()
    assert (tmp_path / "baselines" / "registry.csv").is_file()
    assert all(r.steps > 0 for r in arm.results)
    assert all(0 <= k < 16 for r in arm.results for k in r.decision_kinds)


def test_evaluate_arm_default_runner_policy_loop(tmp_path):
    # 无 MOD-03 客户端 → SandboxRunner 检测 play_decision，走 PolicyAgent 降级循环
    arm = evaluate_arm(Path("harness"), split="heldout", basket=[2000], memory_version=0,
                       arm="parent", run_id="r-policy", protocol=_PROTO, generation=1,
                       harness_version=1, config=_CFG, n_workers=1)
    assert arm.metrics.n == 1
    assert arm.results[0].decisions >= 0


def test_evaluate_generation_reads_train_window(tmp_path):
    for seed in (1000, 1001):
        row = {"type": "episode_end", "seed": seed, "outcome": "win",
               "truncation_reason": None, "stages_cleared": 6, "hearts_left": 2,
               "battles_won": 12, "decisions": 90, "steps": 200, "illegal_attempts": 0,
               "fallback_decisions": 0, "kind_counts": [0] * 16, "decision_kinds": [0],
               "slot_counts": {}, "seq_hash": f"h{seed}", "degraded": False}
        split_dir = tmp_path / "episodes" / "train"
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / f"e-{seed:06d}-g001-train.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
    runner_results = tuple(
        EpisodeResult(seed=2000 + i, split="heldout", generation=1, harness_version=1,
                      outcome="win" if i < 4 else "lose", truncation_reason=None,
                      stages_cleared=5, hearts_left=1, battles_won=10, decisions=90,
                      steps=200, illegal_attempts=0, fallback_decisions=0,
                      kind_counts=tuple([1] * 16), slot_counts={}, seq_hash=f"s{i}",
                      decision_kinds=(0, 1, 2))
        for i in range(8))
    from gamer_rsi.evals.harness import ArmResult

    parent = ArmResult(arm="parent", split="heldout", basket="H1", harness_version=1,
                       memory_version=0, run_id="r-gen", results=runner_results,
                       metrics=aggregate(runner_results, basket="H1"))
    report = evaluate_generation(1, parent, None, run_id="r-gen", run_dir=tmp_path,
                                 config=_CFG, protocol=_PROTO)
    assert report["train"]["window"] == 2
    assert report["heldout"]["n"] == 8
    assert report["gain_realism"]["verdict"] in {"real_gain", "null", "overfit_suspect",
                                                 "regress", "insufficient"}
    assert report["power_note"]
