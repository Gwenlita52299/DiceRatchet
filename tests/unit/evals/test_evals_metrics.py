"""T-01 / T-02 / T-05 / T-10：指标手算校验与 §8.2 判定规则。"""

from __future__ import annotations

import math

import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.evals.metrics import (EpisodeResult, aggregate, illegal_rate,
                                     judge_gain, mcnemar_exact_p, mean_hearts_left,
                                     mean_stages_cleared, normalized_entropy,
                                     verdict_rules, wilson_ci, win_rate)

from evals_support import make_result


# ---- T-01 Wilson CI 手算 -----------------------------------------------------------

def test_wilson_ci_hand_calculated():
    lo, hi = wilson_ci(3, 5)
    assert lo == pytest.approx(0.2307, abs=1e-4)
    assert hi == pytest.approx(0.8824, abs=1e-4)


def test_wilson_ci_zero_wins_boundary():
    lo, hi = wilson_ci(0, 40)
    assert lo == pytest.approx(0.0, abs=1e-4)
    assert hi == pytest.approx(0.0876, abs=1e-4)


def test_wilson_ci_n_zero_raises():
    with pytest.raises(InfraError) as exc:
        wilson_ci(0, 0)
    assert exc.value.code == E.E_EVAL_INSUFFICIENT_N


# ---- T-02 归一化熵手算 -------------------------------------------------------------

def test_normalized_entropy_hand_calculated():
    # 原始熵 0.811278 bits；除以 log2(16)=4 → 0.202820
    value = normalized_entropy({"play_face": 6, "end_turn": 2}, kind_space=16, base=2.0)
    assert value == pytest.approx(0.202820, abs=1e-6)


def test_normalized_entropy_degenerate():
    assert normalized_entropy({}, kind_space=16) == 0.0
    assert normalized_entropy({"play_face": 5}, kind_space=16) == 0.0


# ---- T-05 主指标与非法率公式 -------------------------------------------------------

def _three():
    return [make_result(1, outcome="win", stages_cleared=6, hearts_left=1),
            make_result(2, outcome="lose", stages_cleared=3, hearts_left=0),
            make_result(3, outcome="win", stages_cleared=6, hearts_left=2)]


def test_main_metrics_from_integer_accumulators():
    rows = _three()
    assert win_rate(rows) == pytest.approx(0.6667, abs=1e-4)
    assert mean_stages_cleared(rows) == pytest.approx(5.0)
    assert mean_hearts_left(rows) == pytest.approx(1.0)


def test_truncated_episode_counts_as_lose():
    lose = make_result(9, outcome="lose", truncation_reason="policy_failure")
    metrics = aggregate([lose], basket="H1")
    assert metrics.n == 1
    assert metrics.win_rate == 0.0
    assert metrics.truncation_rate == 1.0


def test_illegal_rate_step_level():
    rows = [make_result(i, steps=29, illegal_attempts=0) for i in range(10)]
    rows.append(make_result(99, steps=7, illegal_attempts=3))
    total_steps = sum(r.steps for r in rows) + sum(r.illegal_attempts for r in rows)
    total_illegal = sum(r.illegal_attempts for r in rows)
    assert total_illegal == 3 and total_steps == 300
    assert illegal_rate(rows) == pytest.approx(0.01)


# ---- T-10 verdict + McNemar 手算 ---------------------------------------------------

@pytest.mark.parametrize("gap,p_improve,p_regress,collapse,expected", [
    (0.05, 0.20, 0.001, False, "regress"),          # 显著退步优先
    (0.30, 0.001, 0.50, False, "overfit_suspect"),  # gap 超阈值
    (0.02, 0.001, 0.50, False, "real_gain"),        # 显著提升 + gap 合格 + 无坍缩
    (0.02, 0.20, 0.50, False, "null"),              # 证据不足
])
def test_verdict_ordered_rules(gap, p_improve, p_regress, collapse, expected):
    verdict, veto = verdict_rules(gap=gap, gap_threshold=0.15, p_improve=p_improve,
                                  p_regress=p_regress, alpha=0.05,
                                  collapse_flag=collapse)
    assert verdict == expected
    assert veto is False


def test_collapse_never_real_gain():
    verdict, veto = verdict_rules(gap=0.01, gap_threshold=0.15, p_improve=0.001,
                                  p_regress=0.5, alpha=0.05, collapse_flag=True)
    assert verdict == "null"
    assert veto is True


def test_low_confidence_never_real_gain():
    verdict, _ = verdict_rules(gap=0.0, gap_threshold=0.15, p_improve=0.0,
                               p_regress=0.5, alpha=0.05, collapse_flag=False,
                               low_confidence=True)
    assert verdict == "insufficient"


def test_mcnemar_exact_hand_calculated():
    cand = [True] * 6 + [False] * 0
    parent = [False] * 6
    assert mcnemar_exact_p(cand, parent, "two-sided") == pytest.approx(0.03125)
    assert mcnemar_exact_p(cand, parent, "greater") == pytest.approx(0.015625)
    cand5 = [True] * 5
    parent5 = [False] * 5
    assert mcnemar_exact_p(cand5, parent5, "two-sided") == pytest.approx(0.0625)


def test_aggregate_drops_degraded_and_keeps_dropped():
    rows = [make_result(1, outcome="win"),
            make_result(2, outcome="win", degraded=True),      # 剔出 n
            make_result(3, outcome="lose", dropped=True)]      # 计入 n，按 lose
    metrics = aggregate(rows, basket="H1")
    assert metrics.n == 2 and metrics.degraded_n == 1 and metrics.n_dropped == 1
    assert metrics.win_rate == pytest.approx(0.5)
    assert metrics.low_confidence is True


def test_aggregate_low_confidence_when_drop_rate_exceeded():
    rows = [make_result(i, outcome="win") for i in range(8)]
    rows += [make_result(100 + i, outcome="lose", dropped=True) for i in range(2)]
    metrics = aggregate(rows, basket="H1", drop_rate_max=0.10)
    assert metrics.n == 10 and metrics.n_dropped == 2
    assert metrics.low_confidence is True


def test_judge_gain_gap_from_train_minus_heldout():
    train = aggregate([make_result(i, outcome="win") for i in range(10)],
                      split="train", basket="train_window")
    heldout = aggregate([make_result(2000 + i, outcome="win" if i < 5 else "lose")
                         for i in range(10)], basket="H1")
    gain = judge_gain(train=train, heldout=heldout, gap_threshold=0.15)
    assert gain.win_rate_gap == pytest.approx(0.5)
    assert gain.verdict == "overfit_suspect"
