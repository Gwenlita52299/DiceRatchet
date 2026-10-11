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

from dataclasses import replace

import pytest

# 注意：``dice_ratchet.evolve.__init__`` 会把子模块名 ``judge`` 重绑到同名函数上，
# 因此 ``import dice_ratchet.evolve.judge as J`` 拿到的是函数而不是模块（Python 的
# ``import ... as`` 会走父包属性）。这里显式从子模块按名导入。
from dice_ratchet.evolve.judge import (CONDITION_IDS, better, first_failure, judge,
                                    pick_winner, smoke_fast_reject, smoke_seeds_for,
                                    superseded_verdict)
from dice_ratchet.infra.config import AppConfig
from dice_ratchet.infra.errors import E

from evolve_support import (FakeArm, heldout_dict, make_app_config, make_candidate,
                            make_config, make_report, paired_dict)

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

#: C5 的边界口径用**显式 cfg** 钉住，不与 `configs/default.yaml` 的默认值耦合。
#: **2026-10-11（D-M3-2 裁决 E，Spec v2.8 / B-64）**：C5 三轴**全部**为相对门
#: `candidate ≤ min(parent + <axis>_tol, <axis>_backstop)`；`max_illegal_rate` /
#: `max_fallback_rate` 由「主门」降为 **backstop**（语义变了，键仍在）。
#: `PARENT` 的三项实测值都是 0.0（见 `evolve_support.FakeArm`），故相对门的门限
#: = `min(0.0 + tol, backstop)`。
_C5_BOUNDS = {"illegal_tol": 0.05, "fallback_tol": 0.05, "truncation_tol": 0.05}

@pytest.mark.parametrize("field,value,ok", [
    # illegal（步级）：parent=0.0 ⇒ 门 = min(0+0.05, 0.35) = 0.05
    ("illegal_rate", 0.05, True),      # 恰好等于 parent + tol
    ("illegal_rate", 0.051, False),    # 超出 tol 一档（旧绝对门 0.35 会**放行**它）
    # fallback（步级）：parent=0.0 ⇒ 门 = min(0+0.05, 0.25) = 0.05
    ("fallback_rate", 0.05, True),
    ("fallback_rate", 0.051, False),
    # truncation（局级，粒度 1/40 = 0.025）：parent=0.0 ⇒ 门 = min(0+0.05, 0.60) = 0.05
    ("truncation_rate", 0.05, True),   # 恰好等于 parent + tol
    ("truncation_rate", 0.075, False),
])
def test_c5_thresholds(field, value, ok):
    verdict = _verdict(heldout=heldout_dict(**{field: value}),
                       cfg=make_config(**_C5_BOUNDS))
    assert _trace_of(verdict)["C5"]["passed"] is ok, verdict.reason


def test_c5_failure_code():
    verdict = _verdict(heldout=heldout_dict(illegal_rate=0.11),
                       cfg=make_config(**_C5_BOUNDS))
    assert verdict.error_code == str(E.E_EVOLVE_ILLEGAL_RATE)
    assert verdict.reason.startswith("C5:illegal/fallback/truncation")


def test_c5_thresholds_are_configurable():
    """`illegal_tol` 可配：把它放宽到 0.30 后 0.11 就在门内（backstop 0.35 仍容许）。"""
    verdict = _verdict(heldout=heldout_dict(illegal_rate=0.11),
                       cfg=make_config(illegal_tol=0.30))
    assert _trace_of(verdict)["C5"]["passed"] is True


# =====================================================================
# C5 的 M3 收紧：三轴相对门 + 绝对 backstop（D-M3-2 裁决 E）
# =====================================================================

def test_c5_illegal_is_relative_not_absolute():
    """**核心回归**：parent 非法率很低（0.03）时候选跳到 0.40 必须 **reject**。

    修复前（绝对门 0.35）0.40 会被放行吗？不会——但 0.30 会。真正的病是：parent=0.03
    时候选 0.30 意味着**非法率涨了 10×**，绝对门却只看「有没有超过 0.35」。
    本用例同时钉住两侧：**相对门**（0.04 作为 parent+0 的对照通过、0.30 被拦）
    与**旧绝对门的漏洞**（0.30 < 0.35 修复前必然通过）。
    """
    parent = FakeArm(arm="parent", win_rate=0.30, n=40, illegal_rate=0.03,
                     fallback_rate=0.0, truncation_rate=0.0)
    cfg = make_config()  # 默认：illegal_tol=0.05, max_illegal_rate(backstop)=0.35

    # 候选把非法率从 0.03 推到 0.30（涨 10×）——修复前 0.30 ≤ 0.35 会**通过**
    bad = _verdict(parent=parent, heldout=heldout_dict(illegal_rate=0.30), cfg=cfg)
    assert _trace_of(bad)["C5"]["passed"] is False, bad.reason
    assert bad.decision == "reject"

    # 同 parent 下 +0.05 以内仍然放行（不把门做成「一票否决」）
    ok = _verdict(parent=parent, heldout=heldout_dict(illegal_rate=0.08), cfg=cfg)
    assert _trace_of(ok)["C5"]["passed"] is True, ok.reason


def test_c5_fallback_is_relative_too():
    """fallback 与 illegal 同构：parent=0.0017（`C1` 实测值）时候选 0.20 必须被拦。"""
    parent = FakeArm(arm="parent", win_rate=0.30, n=40, fallback_rate=0.0017)
    cfg = make_config()
    bad = _verdict(parent=parent, heldout=heldout_dict(fallback_rate=0.20), cfg=cfg)
    assert _trace_of(bad)["C5"]["passed"] is False, bad.reason
    # 修复前：0.20 ≤ max_fallback_rate 0.25 ⇒ 通过（正是「非约束门」）
    assert 0.20 <= cfg.max_fallback_rate, "反例自检：该值必须落在旧绝对门之内"


@pytest.mark.parametrize("axis,parent_value,backstop", [
    ("illegal_rate", 0.50, 0.35),    # min(0.50+0.05, 0.35) = 0.35
    ("fallback_rate", 0.50, 0.25),   # min(0.50+0.05, 0.25) = 0.25
])
def test_c5_backstop_caps_when_parent_is_already_bad(axis, parent_value, backstop):
    """**反例自检的对称面**：相对门单独用会「跟着烂」——parent 0.50 时容差到 0.55。

    backstop 必须把它压回绝对上限，否则 §8.2.2 记录的已知放宽就会在 M3 重演。
    """
    parent = FakeArm(arm="parent", win_rate=0.30, n=40,
                     illegal_rate=parent_value if axis == "illegal_rate" else 0.0,
                     fallback_rate=parent_value if axis == "fallback_rate" else 0.0)
    cfg = make_config()
    # 恰好等于 backstop → 通过
    at = _verdict(parent=parent, heldout=heldout_dict(**{axis: backstop}), cfg=cfg)
    assert _trace_of(at)["C5"]["passed"] is True, at.reason
    # 超出 backstop 一档 → 拦（若没有 backstop，0.55 会在 parent+0.05 内被放行）
    over = _verdict(parent=parent, heldout=heldout_dict(**{axis: backstop + 0.05}), cfg=cfg)
    assert _trace_of(over)["C5"]["passed"] is False, over.reason


def test_c5_truncation_backstop_closes_the_known_relaxation():
    """F28/§8.2.2 的已知放宽：parent=0.90 时候选 0.60 修复前会**通过**相对门。

    `truncation_backstop=0.60` 落地后：0.60 恰好等于 backstop ⇒ 通过；
    0.601 ⇒ 拦。这**不再**是「无界相对门」——这就是本裁决要堵的洞。
    """
    parent = FakeArm(arm="parent", win_rate=0.30, n=40, truncation_rate=0.90)
    cfg = make_config(truncation_backstop=0.60)
    at = _verdict(parent=parent, heldout=heldout_dict(truncation_rate=0.60), cfg=cfg)
    assert _trace_of(at)["C5"]["passed"] is True, at.reason
    over = _verdict(parent=parent, heldout=heldout_dict(truncation_rate=0.601), cfg=cfg)
    assert _trace_of(over)["C5"]["passed"] is False, over.reason
    # 反例自检：相对门单独用（无 backstop）时 0.601 其实在 0.90+0.05 之内
    assert 0.601 <= 0.90 + cfg.truncation_tol


def test_c5_truncation_is_relative_to_parent_not_absolute():
    """**F28 回归护栏**：parent 自身 truncation 高（DeepSeek hard×6 实测 0.30）时不得全拒。

    旧的绝对门 `truncation_rate ≤ 0.15` 会把 parent 与每个候选一起 reject，
    使闭环静默锁死（`harness/versions/` 永不出现新版本）。
    D-M3-2 加 backstop（0.60）后本场景仍必须通过——backstop 是 0.60，不是 0.15。
    """
    parent = FakeArm(arm="parent", win_rate=0.30, n=40, truncation_rate=0.30)
    verdict = _verdict(parent=parent, heldout=heldout_dict(truncation_rate=0.30))
    assert verdict.decision == "accept", verdict.reason


@pytest.mark.parametrize("cand,parent,ok", [
    (0.30, 0.30, True),    # 与 parent 持平（F28 的核心场景）
    (0.35, 0.30, True),    # 恰好等于 parent + tol
    (0.36, 0.30, False),   # 超出 tol 一档
    (0.10, 0.00, False),   # parent 无截断时，候选 0.10 仍超 0.00 + 0.05
    (0.05, 0.00, True),    # 边界
    (0.60, 0.90, True),    # parent 更差时相对门放行，但恰好等于 backstop
    (0.61, 0.90, False),   # **D-M3-2 新增**：backstop 把已知放宽堵死（修复前 0.61 会过）
])
def test_c5_truncation_relative_boundary(cand, parent, ok):
    verdict = _verdict(parent=FakeArm(arm="parent", win_rate=0.30, n=40,
                                      truncation_rate=parent),
                       heldout=heldout_dict(truncation_rate=cand))
    assert _trace_of(verdict)["C5"]["passed"] is ok, verdict.reason


def test_c5_defaults_match_calibration():
    """C5 默认值（2026-10-11 D-M3-2 裁决 E 之后的形态）。

    改这些默认值必须同步 Spec §8.6 / MOD-07 PRD §8.2+§9 / `configs/default.yaml`
    （后者另有 `tests/unit/infra/test_config.py` 的默认值守门）。

    历史：2026-10-07（B-40）三个绝对门 0.35/0.25/0.15「先松开」；2026-10-09（B-44）
    第 3 项改相对门 `truncation_tol=0.05`；**2026-10-11（B-64）前两项也改相对门**，
    `max_illegal_rate`/`max_fallback_rate` 保留但语义降为 **backstop**。
    """
    cfg = make_config()
    assert (cfg.max_illegal_rate, cfg.max_fallback_rate) == (0.35, 0.25)
    assert (cfg.illegal_tol, cfg.fallback_tol, cfg.truncation_tol) == (0.05, 0.05, 0.05)
    assert cfg.truncation_backstop == 0.60
    assert not hasattr(cfg, "max_truncation_rate"), "C5 的 truncation 绝对门已废弃（F28）"


def test_c5_trace_is_auditable_and_serializes():
    """**E 的另一半**：`condition_trace` 必须带 `value`/`threshold`/`passed` 且可落盘。

    M3 的结论要能审计「C5 当时是过还是没过、门限是多少」。verdict 经
    `CandidateReport.as_dict()` 序列化（= 落 `candidates/{id}.json.verdict`，
    见 `evolve/schemas.py`），因此这里走**真实的序列化往返**，而不是只看内存对象。
    """
    from dice_ratchet.evolve.schemas import CandidateReport

    parent = FakeArm(arm="parent", win_rate=0.30, n=40, illegal_rate=0.03,
                     fallback_rate=0.0017, truncation_rate=0.0)
    verdict = _verdict(parent=parent, heldout=heldout_dict(illegal_rate=0.30),
                       cfg=make_config())
    entry = _trace_of(verdict)["C5"]
    assert entry["passed"] is False
    assert set(entry) == {"condition", "passed", "detail", "value", "threshold"}
    assert entry["value"]["illegal_rate"] == 0.30
    assert entry["value"]["parent_illegal_rate"] == 0.03
    # 门限必须**可读地**写明相对门与 backstop 两者（审计时不能只看到一个数）
    assert entry["threshold"]["illegal_tol"] == 0.05
    assert entry["threshold"]["illegal_backstop"] == 0.35
    assert entry["threshold"]["max_illegal_rate"] == 0.08  # min(0.03+0.05, 0.35)

    report = CandidateReport(candidate=make_candidate(), smoke={"n": 2, "win_rate": 0.5},
                             heldout=heldout_dict(illegal_rate=0.30),
                             paired=paired_dict(), train={"win_rate": 0.35},
                             diversity={"entropy_slope": 0.0, "collapse_flag": False},
                             verdict=verdict)
    raw = report.as_dict()
    restored = CandidateReport.from_dict(raw)
    restored_entry = {e["condition"]: e for e in restored.verdict.condition_trace}["C5"]
    assert restored_entry == entry, "C5 trace 经 JSON 往返后必须逐字段不变"


@pytest.mark.parametrize("field", ["illegal_rate", "fallback_rate"])
def test_c0_rejects_when_parent_lacks_relative_gate_metric(field):
    """相对门的输入缺失必须 fail-closed 记 C0——不得静默跳过该轴。

    这是 D-M3-2 的**反例自检**：如果实现里写的是 `getattr(parent, ...) or 0.0` 之类的
    兜底，缺 parent 值会被当成 0.0，门限静默变成 `tol` 且永不报错。
    """

    class _PartialParent:
        class _Metrics:
            def as_dict(self):  # noqa: D102
                base = {"n": 40, "win_rate": 0.30, "illegal_rate": 0.0,
                        "fallback_rate": 0.0, "truncation_rate": 0.0}
                base.pop(field)
                return base

        metrics = _Metrics()

    verdict = _verdict(parent=_PartialParent())
    assert verdict.decision == "reject"
    assert verdict.error_code == str(E.E_EVOLVE_JUDGE_FAILED)
    assert verdict.reason.startswith("C0:incomplete_report")
    assert field in _trace_of(verdict)["C0"]["detail"]


def test_c0_rejects_when_parent_lacks_truncation_rate():
    """truncation 相对门需要 parent 的 truncation_rate；缺它必须 fail-closed 记 C0，
    不能静默跳过门（跳过 = C5 对 truncation 失去诊断力）。"""

    class _NoTruncationParent:
        class _Metrics:
            def as_dict(self):  # noqa: D102
                return {"n": 40, "win_rate": 0.30, "illegal_rate": 0.0,
                        "fallback_rate": 0.0}

        metrics = _Metrics()

    verdict = _verdict(parent=_NoTruncationParent())
    assert verdict.decision == "reject"
    assert verdict.error_code == str(E.E_EVOLVE_JUDGE_FAILED)
    assert verdict.reason.startswith("C0:incomplete_report")
    assert "truncation_rate" in _trace_of(verdict)["C0"]["detail"]


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


# =====================================================================
# F42-C：MOD-07 的每代臂评估必须按配置传 n_workers / n_rep
# =====================================================================
#
# 现场（``runs/m2-s32b-smoke-c1-medium6``，2026-10-10）：``CandidateEvaluator._evaluate_arm``
# 只传 ``runner``，**不传** ``n_workers``/``n_rep`` ⇒ 落到 ``evaluate_arm`` 的默认
# ``n_workers=1``，即一代 84 局**串行**跑；而 gen-000 基线走 ``loop/generation.py``
# 时传的是 ``cfg.evals.n_workers=2``。同一份配置下两条路径的并发度不同。
#
# 修复前签名里没有这两个 kwarg（`evaluate_arm` 也收不到）⇒ 下面的 spy 断言会红。

def _spy_evaluate_arm(monkeypatch):
    """截 ``evaluate_arm`` 的 kwargs（返回 (captured, evaluator)）。"""
    import sys

    import dice_ratchet.evals as evals_pkg
    # 父包把 ``evolve.judge`` 重绑到同名函数上：显式从 ``sys.modules`` 取子模块。
    judge_mod = sys.modules["dice_ratchet.evolve.judge"]

    captured: list[dict] = []

    def fake_evaluate_arm(harness_dir, **kwargs):
        captured.append({"harness_dir": harness_dir, **kwargs})
        return FakeArm(arm=kwargs.get("arm", ""), win_rate=0.5, n=2)

    monkeypatch.setattr(evals_pkg, "evaluate_arm", fake_evaluate_arm)
    cfg = make_app_config(evals=replace(AppConfig().evals,
                                        n_workers=2,
                                        determinism=replace(AppConfig().evals.determinism,
                                                            n_rep=3)))
    ev = judge_mod.CandidateEvaluator(config=cfg, run_id="r-f42c", generation=1)
    return captured, ev


def test_candidate_evaluator_forwards_n_workers_and_n_rep(monkeypatch):
    """F42-C：``_evaluate_arm`` 必须把配置里的 ``n_workers``/``n_rep`` 转发给 ``evaluate_arm``。

    反例自检：同一 spy 下断言捕获到的 kwargs **确实**没有落在默认值 1/1 上
    （``cfg.evals.n_workers=2``、``determinism.n_rep=3``）——若实现只是「恰好等于默认」
    这条用例也会红，因为它在配置里显式设了非默认值。
    """
    captured, ev = _spy_evaluate_arm(monkeypatch)
    ev._evaluate_arm("harness", split="heldout", basket="H1", arm="parent",
                     harness_version=0)
    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["n_workers"] == 2, (
        "MOD-07 的每代臂评估没按配置传 n_workers：84 局会串行跑（F42-C）"
        f"（实际 kwargs={sorted(kwargs)}）")
    assert kwargs["n_rep"] == 3
    # 其余调用面不得被顺手改掉（回归护栏）
    assert kwargs["arm"] == "parent" and kwargs["generation"] == 1
    assert kwargs["memory_version"] == 0 and kwargs["run_id"] == "r-f42c"


def test_candidate_evaluator_defaults_to_serial_when_config_lacks_n_workers(monkeypatch):
    """配置里没有 ``n_workers``/``determinism`` 时保守回落到串行 1/1，而不是抛错。"""
    import sys

    import dice_ratchet.evals as evals_pkg
    judge_mod = sys.modules["dice_ratchet.evolve.judge"]

    captured: list[dict] = []
    monkeypatch.setattr(evals_pkg, "evaluate_arm",
                        lambda harness_dir, **kw: captured.append(kw) or None)
    ev = judge_mod.CandidateEvaluator(config=make_config(), run_id="r-f42c", generation=0)
    ev._evaluate_arm("harness", split="heldout", basket="H1", arm="parent",
                     harness_version=0)
    assert captured[0]["n_workers"] == 1 and captured[0]["n_rep"] == 1
