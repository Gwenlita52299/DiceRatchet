"""T-03 / T-04 / T-11：JSD、熵曲线与斜率、坍缩两点确认。"""

from __future__ import annotations

import pytest

from dice_ratchet.evals.diversity import (CollapseConfig, collapse_check,
                                       distinct_sequence_ratio, entropy_mean_norm,
                                       entropy_slope, js_divergence, kind_distribution,
                                       slot_distribution)
from dice_ratchet.evals.metrics import action_entropy_curve

from evals_support import make_result


# ---- T-03 JSD 手算 -----------------------------------------------------------------

def test_js_divergence_hand_calculated():
    value = js_divergence({"a": 1.0}, {"a": 0.8, "b": 0.2})
    assert value == pytest.approx(0.108031, abs=1e-6)


def test_js_divergence_identical_is_zero():
    assert js_divergence({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) == pytest.approx(0.0)


def test_js_divergence_no_nan_with_zeros():
    value = js_divergence({"a": 0.0, "b": 1.0}, {"a": 1.0, "b": 0.0})
    assert value == pytest.approx(1.0, abs=1e-6)
    assert js_divergence({}, {}) == 0.0


# ---- T-04 熵曲线与斜率 -------------------------------------------------------------

def test_entropy_curve_length_and_null_support():
    rows = [make_result(2000 + i, decision_kinds=(0, 5, 9)) for i in range(3)]
    curve = action_entropy_curve(rows, t_max=48, min_support=3)
    assert len(curve) == 48
    assert curve[0] is not None and curve[2] is not None
    assert all(value is None for value in curve[3:]) # 无支撑回合为 null


def test_entropy_curve_min_support():
    rows = [make_result(2000 + i, decision_kinds=(0,)) for i in range(2)]
    curve = action_entropy_curve(rows, t_max=4, min_support=3)
    assert curve[0] is None


def test_entropy_slope_monotone_decreasing():
    curve = [1.0, 0.8, 0.6, 0.4, 0.2]
    assert entropy_slope(curve) < 0.0
    assert entropy_slope([0.2, 0.4, 0.6]) > 0.0
    assert entropy_slope([None, None]) == 0.0


def test_entropy_mean_norm_ignores_nulls():
    assert entropy_mean_norm([0.5, None, 1.0]) == pytest.approx(0.75)
    assert entropy_mean_norm([None]) == 0.0


# ---- T-11 坍缩两点确认 -------------------------------------------------------------

def _config(**kw):
    base = dict(c1_slope_threshold=-0.010, c2_ratio_threshold=0.60,
                c3_jsd_freeze=0.010, c3_winrate_jump=0.05, c4_seq_ratio=0.50,
                confirm_generations=2)
    base.update(kw)
    return CollapseConfig(**base)


def test_c1_single_generation_is_not_confirmed():
    curve = [1.0 - 0.1 * i for i in range(10)] # 斜率 -0.1 <= -0.010
    verdict = collapse_check(curves=[curve], jsd_kind=[None], win_rates=[0.3],
                             cfg=_config())
    assert verdict.collapse_flag is False


def test_c1_two_consecutive_generations_confirms():
    curve = [1.0 - 0.1 * i for i in range(10)]
    verdict = collapse_check(curves=[curve, curve], jsd_kind=[None, None],
                             win_rates=[0.3, 0.3], cfg=_config())
    assert verdict.collapse_flag is True
    assert "C1" in verdict.collapse_reasons


def test_c4_triggers_single_generation():
    verdict = collapse_check(curves=[[0.5] * 5], jsd_kind=[None], win_rates=[0.3],
                             cfg=_config(), seq_ratios=[0.2])
    assert verdict.collapse_flag is True
    assert "C4" in verdict.collapse_reasons


def test_c3_needs_frozen_jsd_and_winrate_jump():
    """C3 = 「分布冻结（jsd < freeze）**且** 胜率跳变 ≥ jump」。

    **2026-10-11 修订（D-M3-5 的 c3 修复）**：C3 由「连续 `confirm=2` 代」改为
    **单代即触发**（与 C4 同类——分布冻结 + 胜率跳变本身就是直接证据，不是趋势推断）。
    原因：`c3_flags` 只在 `i ≥ 1` 上有定义（比较相邻两代）⇒ 要「末尾连续 2 代为 True」
    需要 **≥3 代里连续两次**「冻结+跳变」；M3 只有 3 代 ⇒ 结构上几乎不可能触发。
    这是 c3 从未触发的**第三个**原因（前两个：`eval report` 硬编码 null、
    `evolve_generation` 的 pair 错位）。
    """
    flat = [0.5] * 5
    # 两代即可：jsd 冻结 + 胜率跳升 +0.10 ≥ 0.05
    verdict = collapse_check(curves=[flat, flat], jsd_kind=[None, 0.001],
                             win_rates=[0.50, 0.60], cfg=_config())
    assert verdict.collapse_flag is True
    assert "C3" in verdict.collapse_reasons

    # 反例自检 ①：胜率没跳 ⇒ 不报
    no_jump = collapse_check(curves=[flat, flat], jsd_kind=[None, 0.001],
                             win_rates=[0.40, 0.44], cfg=_config())
    assert "C3" not in no_jump.collapse_reasons, "跳变 < 0.05 不得报 C3"

    # 反例自检 ②：分布**变了**（jsd 不冻结）⇒ 不报（免得把「策略变了」误判成「冻结」）
    changed = collapse_check(curves=[flat, flat], jsd_kind=[None, 0.30],
                             win_rates=[0.50, 0.60], cfg=_config())
    assert "C3" not in changed.collapse_reasons, "分布明显变化时不得报 C3"

    # 反例自检 ③：jsd 缺测（None）⇒ 不报。**None 不等于 0.0** ——
    # 把缺失当 0.0 会读成「分布完全没变」从而误报冻结。
    missing = collapse_check(curves=[flat, flat], jsd_kind=[None, None],
                             win_rates=[0.50, 0.60], cfg=_config())
    assert "C3" not in missing.collapse_reasons, "jsd 缺测不得当成 0.0 报冻结"


# ---- 分布与重复度 ------------------------------------------------------------------

def test_kind_and_slot_distributions():
    counts = [0] * 16
    counts[0], counts[1] = 3, 1
    rows = [make_result(1, kind_counts=counts, slot_counts={"attack×blade": 3}),
            make_result(2, kind_counts=counts, slot_counts={"attack×blade": 1})]
    kind = kind_distribution(rows)
    assert kind["play_face"] == pytest.approx(0.75)
    assert kind["discard_die"] == pytest.approx(0.25)
    assert slot_distribution(rows)["attack×blade"] == pytest.approx(1.0)


def test_distinct_sequence_ratio():
    rows = [make_result(1, seq="a"), make_result(2, seq="a"), make_result(3, seq="b")]
    assert distinct_sequence_ratio(rows) == pytest.approx(2 / 3)
    assert distinct_sequence_ratio([]) == 1.0
