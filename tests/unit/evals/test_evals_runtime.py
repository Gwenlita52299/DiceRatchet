"""执行器 / IO / reward 的运行期单测（补齐 G1 覆盖率，均为确定性轻量用例）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.infra.config import load_config
from dice_ratchet.evals import seeds as S

from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol
_H1_START = validate_protocol(coerce_protocol(None)).splits["heldout"].baskets["H1"].start
from dice_ratchet.evals.baselines import run_baseline
from dice_ratchet.evals.harness import (append_generations_csv, episode_result_from_rows,
                                     evaluate_arm, evaluate_generation,
                                     load_episode_results, write_ext_metrics,
                                     write_plots_data)
from dice_ratchet.evals.metrics import EpisodeResult, aggregate
from dice_ratchet.evals import reward

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
    row = {"type": "episode_end", "seed": _H1_START, "outcome": "win",
           "truncation_reason": None, "stages_cleared": 6, "hearts_left": 2,
           "battles_won": 15, "decisions": 90, "steps": 210, "illegal_attempts": 1,
           "fallback_decisions": 0, "kind_counts": [0] * 16, "decision_kinds": [0, 1],
           "slot_counts": {"attack×blade": 3}, "seq_hash": "h", "degraded": False}
    result = episode_result_from_rows([row], seed=_H1_START, split="heldout", generation=3,
                                      harness_version=2)
    assert result is not None and result.decision_kinds == (0, 1)
    assert episode_result_from_rows([{"type": "x"}], seed=1, split="heldout",
                                    generation=0, harness_version=0) is None
    split_dir = tmp_path / "episodes" / "heldout"
    split_dir.mkdir(parents=True)
    (split_dir / f"e-{_H1_START:06d}-g003-parent.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8")
    loaded = load_episode_results(tmp_path, "heldout", generation=3)
    assert len(loaded) == 1 and loaded[0].seed == _H1_START
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
    arm = run_baseline("random", split="heldout", basket=[_H1_START, _H1_START + 1], run_id="r-run",
                       run_dir=tmp_path, config=_CFG, protocol=_PROTO, n_workers=1)
    assert arm.metrics.n == 2
    assert (tmp_path / "baselines" / "random-heldout.json").is_file()
    assert (tmp_path / "baselines" / "registry.csv").is_file()
    assert all(r.steps > 0 for r in arm.results)
    assert all(0 <= k < 16 for r in arm.results for k in r.decision_kinds)


def test_evaluate_arm_default_runner_policy_loop(tmp_path):
    # 无 MOD-03 客户端 → SandboxRunner 检测 play_decision，走 PolicyAgent 降级循环
    arm = evaluate_arm(Path("harness"), split="heldout", basket=[_H1_START], memory_version=0,
                       arm="parent", run_id="r-policy", protocol=_PROTO, generation=1,
                       harness_version=1, config=_CFG, n_workers=1)
    assert arm.metrics.n == 1
    assert arm.results[0].decisions >= 0


def test_evaluate_generation_reads_train_window(tmp_path):
    for seed in (1000, 1001):
        # F64（2026-10-14）：训练段现在按 ``arm="train"`` **真过滤**（同代的冒烟局用的是
        # 本代训练块的末尾 seed，不过滤会被混进训练段），因此夹具必须像真实写入方那样
        # 写上 ``arm`` / ``generation`` / ``harness_version`` / ``split``。
        row = {"type": "episode_end", "seed": seed, "outcome": "win",
               "arm": "train", "split": "train", "generation": 1, "harness_version": 1,
               "truncation_reason": None, "stages_cleared": 6, "hearts_left": 2,
               "battles_won": 12, "decisions": 90, "steps": 200, "illegal_attempts": 0,
               "fallback_decisions": 0, "kind_counts": [0] * 16, "decision_kinds": [0],
               "slot_counts": {}, "seq_hash": f"h{seed}", "degraded": False}
        split_dir = tmp_path / "episodes" / "train"
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / f"e-{seed:06d}-g001-train.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
    runner_results = tuple(
        EpisodeResult(seed=_H1_START + i, split="heldout", generation=1, harness_version=1,
                      outcome="win" if i < 4 else "lose", truncation_reason=None,
                      stages_cleared=5, hearts_left=1, battles_won=10, decisions=90,
                      steps=200, illegal_attempts=0, fallback_decisions=0,
                      kind_counts=tuple([1] * 16), slot_counts={}, seq_hash=f"s{i}",
                      decision_kinds=(0, 1, 2))
        for i in range(8))
    from dice_ratchet.evals.harness import ArmResult

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


# ---- env.episode 必须真的进 env（F26 回归）-----------------------------------------

class _StopBeforeEnv(Exception):
    """哨兵：在 ``make_default_env`` 处中断，只取它收到的 ``episode_config``。"""


def _capture_make_default_env(monkeypatch, seen):
    def _fake_make_default_env(**kwargs):
        seen.update(kwargs)
        raise _StopBeforeEnv

    monkeypatch.setattr("dice_ratchet.sandbox.api.make_default_env", _fake_make_default_env)


def test_sandbox_runner_honours_config_episode(monkeypatch):
    """``config.env.episode`` 必须传进 ``make_default_env``（F26 回归）。

    回归背景（2026-10-07，M2 补 medium × 6 基线时暴露）：``SandboxRunner`` 在
    ``episode_config=None`` 时曾直接落到 ``EpisodeConfig()`` 的**代码默认**（hard × 6、
    toggles 全开），而 ``baseline`` / ``play`` 这两个 MOD-08 入口都不显式传该参数 ——
    于是 ``env.episode.*``（difficulty / stage_count / toggles / character）对它们**完全失效**：
    带 ``DICERATCHET_ENV__EPISODE__DIFFICULTY=medium`` 的局与 hard × 6 逐位相同。
    """
    from types import SimpleNamespace

    from dice_ratchet.evals import harness as H

    seen: dict = {}
    _capture_make_default_env(monkeypatch, seen)
    config = SimpleNamespace(env=SimpleNamespace(
        episode=SimpleNamespace(difficulty="medium", stage_count=6)))
    runner = H.SandboxRunner(agent_factory=lambda seed, arm: None, run_id="t-f26",
                             config=config)
    with pytest.raises(_StopBeforeEnv):
        runner.run_episode(_H1_START, split="heldout", arm="baseline-heuristic",
                           generation=0, harness_version=0)
    cfg = seen["episode_config"]
    assert cfg.difficulty == "medium", "config.env.episode.difficulty 没进 env（F26 复发）"
    assert cfg.stage_count == 6


def test_sandbox_runner_explicit_episode_wins(monkeypatch):
    """显式传入的 ``episode_config`` 优先于 ``config.env.episode``（Spec §5 的优先级）。"""
    from types import SimpleNamespace

    from dice_ratchet.evals import harness as H

    seen: dict = {}
    _capture_make_default_env(monkeypatch, seen)
    config = SimpleNamespace(env=SimpleNamespace(
        episode=SimpleNamespace(difficulty="medium", stage_count=6)))
    runner = H.SandboxRunner(agent_factory=lambda seed, arm: None, run_id="t-f26",
                             config=config)
    explicit = SimpleNamespace(difficulty="easy", stage_count=3)
    with pytest.raises(_StopBeforeEnv):
        runner.run_episode(_H1_START, split="heldout", arm="baseline-heuristic",
                           generation=0, harness_version=0, episode_config=explicit)
    assert seen["episode_config"].difficulty == "easy"
    assert seen["episode_config"].stage_count == 3


def test_c34_evaluate_generation_persists_kind_distribution(tmp_path):
    """**§3.4 的 c3 修复之三**：`evaluate_generation` 必须落该代的 kind 分布。

    它是**唯一**能同时拿到 parent 与 winner 逐局结果的生产路径，所以 g−1 的分布
    只能在这里落盘——`eval report` 的重算路径没有 g−1 的结果，只能读回。
    修复前 `jsds` 是硬编码/错位的，且分布**从不落盘** ⇒ g+1 代无从计算 `jsd_kind`。
    """
    from dice_ratchet.evals.harness import (ArmResult, evaluate_generation,
                                            kind_distribution_path,
                                            read_kind_distribution)
    from dice_ratchet.evals.metrics import aggregate

    def _res(seed: int, kind_index: int) -> EpisodeResult:
        counts = [0] * 16
        counts[kind_index] = 10
        return EpisodeResult(seed=seed, split="heldout", generation=1, harness_version=1,
                             outcome="win", truncation_reason=None, stages_cleared=5,
                             hearts_left=1, battles_won=10, decisions=90, steps=200,
                             illegal_attempts=0, fallback_decisions=0,
                             kind_counts=tuple(counts), slot_counts={},
                             seq_hash=f"s{seed}", decision_kinds=(0, 1, 2))

    parent_results = tuple(_res(_H1_START + i, 0) for i in range(8))
    parent = ArmResult(arm="parent", split="heldout", basket="H1", harness_version=1,
                       memory_version=0, run_id="r-c34", results=parent_results,
                       metrics=aggregate(parent_results, basket="H1"))
    evaluate_generation(1, parent, None, run_id="r-c34", run_dir=tmp_path,
                        config=_CFG, protocol=_PROTO)
    dist = read_kind_distribution(tmp_path, 1)
    assert dist is not None, "g=1 的 kind 分布必须落盘"
    assert kind_distribution_path(tmp_path, 1).is_file()
    assert abs(sum(dist.values()) - 1.0) < 1e-6, "分布应归一化"
