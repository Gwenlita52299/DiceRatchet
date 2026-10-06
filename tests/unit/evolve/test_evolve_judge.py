"""FR-07-6 / FR-07-8：冒烟快速淘汰与判定表 C0–C5（PRD §11 单测 5）。

「给定-当-则」逐条对应 PRD §11-5（表驱动 12 例）：

- C1 降 0.01 → 但 ``tol=0.05`` 故**通过**；降 0.06 → reject(``E_EVOLVE_NO_GAIN``)；
- C2 ``significant=False`` → reject；
- C3 ``gap=0.19>0.15`` → reject(``E_EVOLVE_OVERFIT_SUSPECT``)；
- C4 ``collapse_flag=True`` → reject(``E_EVOLVE_COLLAPSE``)；
- C5 ``illegal_rate=0.11`` → reject(``E_EVOLVE_ILLEGAL_RATE``)；
- 六项全真 → accept；并覆盖短路顺序。
"""

from __future__ import annotations

import pytest

# 注意：``gamer_rsi.evolve.__init__`` 会把子模块名 ``judge`` 重绑到同名函数上，
# 因此 ``import gamer_rsi.evolve.judge as J`` 拿到的是函数而不是模块（Python 的
# ``import ... as`` 会走父包属性）。这里显式从子模块按名导入。
from gamer_rsi.evolve.judge import (CONDITION_IDS, better, first_failure, judge,
                                    pick_winner, smoke_fast_reject, smoke_seeds_for,
                                    superseded_verdict)
from gamer_rsi.infra.errors import E

from evolve_support import (FakeArm, heldout_dict, make_candidate, make_config,
                            make_report, paired_dict)

# =====================================================================
# 基线与辅助
# =====================================================================

PARENT = FakeArm(arm="parent", win_rate=0.30, n=40)


def _verdict(*, heldout=None, paired=None, train=None, diversity=None, parent=PARENT,
             cfg=None, candidate=None):
    report = make_report(candidate=candidate, heldout=heldout, paired=paired,
                         train=train, diversity=diversity)
    return judge(report, parent, cfg=cfg or make_config(), generation=1)


def _trace_of(verdict):
    return {entry["condition"]: entry for entry in verdict.condition_trace}


# =====================================================================
# 六项全真 → accept
# =====================================================================

def test_all_six_pass_accepts():
    verdict = _verdict()
    assert verdict.decision == "accept"
    assert verdict.error_code is None
    assert verdict.reason == ""
    assert [e["condition"] for e in verdict.condition_trace] == list(CONDITION_IDS)
    assert all(e["passed"] for e in verdict.condition_trace)


# =====================================================================
# C0 前置完备
# =====================================================================

def test_c0_rejects_insufficient_n():
    verdict = _verdict(heldout=heldout_dict(n=5))
    assert verdict.decision == "reject"
    assert verdict.error_code == str(E.E_EVOLVE_JUDGE_FAILED)
    assert verdict.reason.startswith("C0:incomplete_report")


def test_c0_rejects_missing_key_field():
    heldout = heldout_dict()
    heldout["illegal_rate"] = None
    verdict = _verdict(heldout=heldout)
    assert verdict.decision == "reject"
    assert "C0" in verdict.reason


def test_c0_rejects_when_parent_lacks_win_rate():
    verdict = _verdict(parent=object())
    assert verdict.decision == "reject"
    assert verdict.error_code == str(E.E_EVOLVE_JUDGE_FAILED)


def test_c0_short_circuits_before_c1():
    verdict = _verdict(heldout=heldout_dict(n=0, win_rate=0.0))
    assert [e["condition"] for e in verdict.condition_trace] == ["C0"]


# =====================================================================
# C1 主指标不降（tol=0.05）
# =====================================================================

@pytest.mark.parametrize("win,expected", [
    (0.30, True),   # 持平
    (0.29, True),   # 降 0.01 ＜ tol=0.05
    (0.25, True),   # 恰好等于 parent - tol（边界取等号通过）
    (0.24, False),  # 降 0.06 ＞ tol
    (0.35, True),   # 提升
])
def test_c1_win_rate_tolerance(win, expected):
    verdict = _verdict(heldout=heldout_dict(win_rate=win))
    c1 = _trace_of(verdict)["C1"]["passed"]
    assert c1 is expected, verdict.reason


def test_c1_failure_reason_and_code():
    verdict = _verdict(heldout=heldout_dict(win_rate=0.20))
    assert verdict.decision == "reject"
    assert verdict.error_code == str(E.E_EVOLVE_NO_GAIN)
    assert verdict.reason.startswith("C1:win_rate")


def test_c1_short_circuits_before_c2():
    verdict = _verdict(heldout=heldout_dict(win_rate=0.10),
                       paired=paired_dict(significant=False))
    assert [e["condition"] for e in verdict.condition_trace] == ["C0", "C1"]


# =====================================================================
# C2 至少一指标显著提升
# =====================================================================

@pytest.mark.parametrize("paired,ok", [
    ({"significant": True, "direction": "up", "metric": "win_rate"}, True),
    ({"significant": True, "direction": "up", "metric": "mean_stages_cleared"}, True),
    ({"significant": True, "direction": "up", "metric": "mean_hearts_left"}, True),
    ({"significant": False, "direction": "up", "metric": "win_rate"}, False),
    ({"significant": True, "direction": "down", "metric": "win_rate"}, False),
    ({"significant": True, "direction": "flat", "metric": "win_rate"}, False),
    ({"significant": True, "direction": "up", "metric": "mean_decisions"}, False),
])
def test_c2_significance_matrix(paired, ok):
    verdict = _verdict(paired=paired_dict(**paired))
    assert _trace_of(verdict)["C2"]["passed"] is ok, verdict.reason


def test_c2_failure_code():
    verdict = _verdict(paired=paired_dict(significant=False))
    assert verdict.error_code == str(E.E_EVOLVE_NO_GAIN)
    assert verdict.reason.startswith("C2:no_significant_gain")


def test_c2_short_circuits_before_c3():
    verdict = _verdict(paired=paired_dict(significant=False),
                       train={"win_rate": 0.9})
    assert [e["condition"] for e in verdict.condition_trace] == ["C0", "C1", "C2"]


# =====================================================================
# C3 增益真实性
# =====================================================================

@pytest.mark.parametrize("train_win,gap,ok", [
    (0.40, 0.05, True),
    (0.45, 0.10, True),
    (0.49, 0.14, True),
    (0.54, 0.19, False), # PRD §11-5 的示例
])
def test_c3_gap_threshold(train_win, gap, ok):
    verdict = _verdict(train={"win_rate": train_win})
    entry = _trace_of(verdict)["C3"]
    assert entry["passed"] is ok, verdict.reason
    assert abs(float(entry["value"]) - gap) < 1e-9


def test_c3_boundary_is_inclusive():
    verdict = _verdict(train={"win_rate": 0.50}) # gap 恰好 0.15
    assert _trace_of(verdict)["C3"]["passed"] is True


def test_c3_failure_code_is_overfit_suspect():
    verdict = _verdict(train={"win_rate": 0.60})
    assert verdict.error_code == str(E.E_EVOLVE_OVERFIT_SUSPECT)
    assert verdict.reason.startswith("C3:gap")


def test_c3_short_circuits_before_c4():
    verdict = _verdict(train={"win_rate": 0.60},
                       diversity={"collapse_flag": True, "entropy_slope": -1.0})
    assert [e["condition"] for e in verdict.condition_trace] == ["C0", "C1", "C2", "C3"]


# =====================================================================
# C4 无坍缩
# =====================================================================

@pytest.mark.parametrize("diversity,ok", [
    ({"collapse_flag": False, "entropy_slope": 0.0}, True),
    ({"collapse_flag": False, "entropy_slope": -0.01}, True),   # 恰好等于下限
    ({"collapse_flag": False, "entropy_slope": -0.004}, True),  # Spec 示例：通过区间
    ({"collapse_flag": False, "entropy_slope": -0.02}, False),
    ({"collapse_flag": True, "entropy_slope": 0.1}, False),
    ({"collapse_flag": False, "entropy_slope": None}, False),
])
def test_c4_collapse_and_slope(diversity, ok):
    verdict = _verdict(diversity=diversity)
    assert _trace_of(verdict)["C4"]["passed"] is ok, verdict.reason


def test_c4_failure_code():
    verdict = _verdict(diversity={"collapse_flag": True, "entropy_slope": -0.0})
    assert verdict.error_code == str(E.E_EVOLVE_COLLAPSE)
    assert verdict.reason.startswith("C4:collapse")


def test_c4_short_circuits_before_c5():
    verdict = _verdict(diversity={"collapse_flag": True, "entropy_slope": 0.0},
                       heldout=heldout_dict(illegal_rate=0.9))
    assert [e["condition"] for e in verdict.condition_trace] == [
        "C0", "C1", "C2", "C3", "C4"]


# =====================================================================
# C5 合法性与健壮性
# =====================================================================

@pytest.mark.parametrize("field,value,ok", [
    ("illegal_rate", 0.05, True),      # 恰好等于上限
    ("illegal_rate", 0.11, False),     # PRD §11-5 的示例
    ("fallback_rate", 0.10, True),
    ("fallback_rate", 0.11, False),
    ("truncation_rate", 0.05, True),
    ("truncation_rate", 0.06, False),
])
def test_c5_thresholds(field, value, ok):
    verdict = _verdict(heldout=heldout_dict(**{field: value}))
    assert _trace_of(verdict)["C5"]["passed"] is ok, verdict.reason


def test_c5_failure_code():
    verdict = _verdict(heldout=heldout_dict(illegal_rate=0.11))
    assert verdict.error_code == str(E.E_EVOLVE_ILLEGAL_RATE)
    assert verdict.reason.startswith("C5:illegal/fallback/truncation")


def test_c5_thresholds_are_configurable():
    verdict = _verdict(heldout=heldout_dict(illegal_rate=0.11),
                       cfg=make_config(max_illegal_rate=0.35))
    assert _trace_of(verdict)["C5"]["passed"] is True


# =====================================================================
# 判定表不变量
# =====================================================================

def test_short_circuit_order_is_exactly_the_documented_order():
    assert CONDITION_IDS == ("C0", "C1", "C2", "C3", "C4", "C5")
    verdict = _verdict()
    assert [e["condition"] for e in verdict.condition_trace] == list(CONDITION_IDS)


def test_first_failure_returns_first_false_entry():
    verdict = _verdict(paired=paired_dict(significant=False))
    first = first_failure(verdict.condition_trace)
    assert first is not None and first["condition"] == "C2"


def test_judge_records_candidate_id_and_generation():
    cand = make_candidate()
    verdict = _verdict(candidate=cand)
    assert verdict.candidate_id == cand.candidate_id
    assert verdict.generation == 1


# =====================================================================
# 冒烟快速淘汰（FR-07-6）
# =====================================================================

@pytest.mark.parametrize("smoke,parent,reason", [
    (dict(win_rate=0.10), dict(win_rate=0.50), "win_rate"),        # 0.10 < 0.50-0.2
    (dict(win_rate=0.29), dict(win_rate=0.50), "win_rate"),        # 边界下方
    (dict(win_rate=0.31), dict(win_rate=0.50), None),              # 边界上方：通过
    (dict(win_rate=0.50, illegal_rate=0.31), dict(win_rate=0.50), "illegal_rate"),
    (dict(win_rate=0.50, fallback_rate=0.31), dict(win_rate=0.50), "fallback_rate"),
])
def test_smoke_fast_reject(smoke, parent, reason):
    result = smoke_fast_reject(FakeArm(n=2, **smoke), baseline=FakeArm(n=2, **parent),
                                 cfg=make_config())
    if reason is None:
        assert result is None
    else:
        assert result is not None and reason in result


def test_smoke_fast_reject_on_runtime_error():
    smoke = FakeArm(error_codes=("E_ENV_INTERNAL",))
    result = smoke_fast_reject(smoke, baseline=FakeArm(), cfg=make_config())
    assert result is not None and "smoke_error" in result


def test_smoke_fast_reject_missing_smoke():
    assert smoke_fast_reject(None, baseline=FakeArm(), cfg=make_config()) == "smoke_missing"


def test_smoke_seeds_are_last_of_the_generation_block():
    """FR-07-6：冒烟 seed = 本代训练块的**末** N 个（不再是固定的 1000–1004）。"""
    seeds = smoke_seeds_for(1, smoke_n=2)
    assert seeds == (1028, 1029)
    seeds_gen2 = smoke_seeds_for(2, smoke_n=2)
    assert seeds_gen2 == (1058, 1059)
    assert seeds != seeds_gen2


# =====================================================================
# winner 选择（§8.2）
# =====================================================================

def _judged(**kwargs):
    """先构造报告，再用判定表把 ``verdict`` 一起冻结进 ``CandidateReport``。"""
    report = make_report(**kwargs)
    return make_report(**kwargs,
                       verdict=judge(report, PARENT, cfg=make_config(), generation=1))


def test_pick_winner_takes_largest_delta():
    a = _judged(candidate=make_candidate(rationale="a"), paired=paired_dict(delta=0.10))
    b = _judged(candidate=make_candidate(rationale="b"), paired=paired_dict(delta=0.20))
    assert pick_winner([a, b]) is b


def test_pick_winner_tie_breaks_on_smaller_candidate_id():
    report_a = _judged(candidate=make_candidate(rationale="同一 delta 甲"),
                       paired=paired_dict(delta=0.10))
    report_b = _judged(candidate=make_candidate(rationale="同一 delta 乙"),
                       paired=paired_dict(delta=0.10))
    winner = pick_winner([report_a, report_b])
    assert winner.candidate_id == min(report_a.candidate_id, report_b.candidate_id)
    assert better(report_a, report_b) == (report_a.candidate_id < report_b.candidate_id)


def test_pick_winner_returns_none_when_all_rejected():
    report = _judged(paired=paired_dict(significant=False))
    assert pick_winner([report]) is None


def test_superseded_verdict_has_no_error_code():
    winner = make_report(candidate=make_candidate(rationale="winner"))
    loser = make_report(candidate=make_candidate(rationale="loser"))
    verdict = superseded_verdict(loser, winner, generation=1)
    assert verdict.decision == "reject"
    assert verdict.reason == "superseded"
    assert verdict.error_code is None
