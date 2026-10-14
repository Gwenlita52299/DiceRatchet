"""判据 1 判定器（F-M3-9，Spec v2.13 / B-69）的回归护栏。

**为什么有这个文件**：MOD-08 §12 判定表第 1 行要求 `monotonic_ok=true`，但
``grep -rn monotonic_ok dice_ratchet/ schemas/ scripts/`` 在 2026-10-11 之前是 **0 命中**
（只在文档里出现 3 次）⇒ M3 跑完 3 代后「判据 1 通过/不通过」无法由代码产出。

本文件同时钉住三处**容易写错**的口径：

1. **`g−1` = 上一个已存在的 heldout 代**，不是字面 `g−1`，且**第一代不参与判定**
   （它没有可比对象）——但「一对可比都没有」必须判失败（只有基线的 run 不能算通过）。
2. **`P(gap>0)` ≠ `GainVerdict.win_rate_gap`**：后者是 `train − heldout`，
   `P(train > heldout)` 恰恰是判据 1 **不**想要的。真定义写在本模块的
   `_paired_p_gap` 上：相邻两代 heldout 的 `mean_stages_cleared` 的配对 bootstrap
   （在**共有 seed** 上配对）。
3. **台账必须按 `split="heldout"` 过滤**（F48 之后每代每 split 一行），
   否则 train 行会被当成第二个数据点。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from dice_ratchet.evals.acceptance import (CRITERION_TOL, P_GAP_THRESHOLD,
                                           evaluate_acceptance)
from dice_ratchet.evals.harness import GENERATIONS_COLUMNS

#: 测试用的小样本（真值来自 `evolve.min_heldout_n=20`）；用例显式传入以避免
#: 「测试依赖配置默认值」这类脆弱性。
N = 6


def _write_report(run_dir: Path, *, generation: int, decision: str) -> None:
    """写该代 ``report.json``（**接受记录的唯一权威来源**，F43 口径）。

    判定器**不**从台账读判决：台账的 `decision` 在「重算晚于裁决」时会被保留成旧值
    （F43 的设计），而 `report.json` 是 MOD-07 写下的判决记录。
    """
    d = run_dir / "generations" / f"gen-{generation:03d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({
        "generation": generation, "harness_version": generation,
        "parent_version": max(0, generation - 1), "decision": decision,
        "decided_by": "auto", "reason": "", "heldout": {}, "train": {},
    }, ensure_ascii=False), encoding="utf-8")


def _write_ledger(run_dir: Path, gens: list[dict]) -> None:
    """写 ``metrics/generations.csv``。

    每个元素 ``{"generation": g, "win_rate": .., "ci_lo": .., "decision": ..}``
    → 写**heldout + train 两行**（复现 F48 之后的真实形状；判定器必须只读 heldout）。
    """
    path = run_dir / "metrics" / "generations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_COLUMNS))
        writer.writeheader()
        for g in gens:
            if g.get("decision"):
                _write_report(run_dir, generation=int(g["generation"]),
                              decision=str(g["decision"]))
            writer.writerow({"generation": g["generation"], "split": "heldout",
                             "n": g.get("n", N), "win_rate": g["win_rate"],
                             "ci_lo": g["ci_lo"], "ci_hi": g.get("ci_hi", 0.9),
                             "harness_version": g.get("harness_version", ""),
                             "decision": g.get("decision", "")})
            writer.writerow({"generation": g["generation"], "split": "train",
                             "n": 30, "win_rate": 0.99, "ci_lo": 0.9, "ci_hi": 0.99})


def _write_episode(run_dir: Path, *, generation: int, seed: int,
                   stages_cleared: int, arm: str = "parent") -> None:
    """写一局的 ``episode_end``（判定器读 ``stages_cleared`` 做配对 bootstrap）。"""
    d = run_dir / "episodes" / "heldout"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"e-{seed:06d}-g{generation:03d}-{arm}.jsonl"
    path.write_text(json.dumps({
        "type": "episode_end", "seed": seed, "split": "heldout",
        "generation": generation, "arm": arm, "outcome": "win",
        "stages_cleared": stages_cleared, "hearts_left": 1, "battles_won": 5,
        "decisions": 80, "steps": 200, "illegal_attempts": 0,
        "fallback_decisions": 0, "kind_counts": [0] * 16,
        "decision_kinds": [0], "slot_counts": {}, "seq_hash": f"s{seed}",
    }, ensure_ascii=False) + "\n", encoding="utf-8")


def _episodes(run_dir: Path, *, generation: int, stages: list[int],
              seed0: int = 5000) -> None:
    for i, st in enumerate(stages):
        _write_episode(run_dir, generation=generation, seed=seed0 + i,
                       stages_cleared=st)


def _eval(run_dir: Path, **kw):
    kw.setdefault("min_heldout_n", N)
    kw.setdefault("bootstrap_B", 400)   # 小 B：这是判定器的结构用例，不是统计标定
    return evaluate_acceptance(run_dir, run_id="r-acc-test", **kw)


# =====================================================================
# 「一对可比都没有」≠「通过」
# =====================================================================

def test_single_generation_has_nothing_to_compare(tmp_path):
    """**反例自检的起点**：只有一代时 `monotonic_ok` 必须是 False。

    修法上很容易写成「第一代默认 passed=True」或「第一代默认 False 参与 any()」——
    前者让只有基线的 run 也算通过，后者让**永远**判失败（连 3 代全对也过不了）。
    正确语义：第一代**不参与**判定，但**至少要有一对可比**。
    """
    _write_ledger(tmp_path, [{"generation": 0, "win_rate": 0.5, "ci_lo": 0.4,
                              "decision": "baseline"}])
    rep = _eval(tmp_path)
    assert len(rep.comparable) == 0
    assert rep.monotonic_ok is False
    assert rep.ok is False
    assert rep.generations[0].prev_generation is None
    assert rep.generations[0].pass_ci is None, "无可比对象时不得编一个 pass 出来"


def test_three_generations_non_decreasing_passes(tmp_path):
    """3 代单调不降 + 1 次 accept ⇒ 判据 1 通过。"""
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.55, "ci_lo": 0.53, "decision": "accept"},
        {"generation": 2, "win_rate": 0.60, "ci_lo": 0.54, "decision": "reject"},
    ])
    for g, base in ((0, 2), (1, 4), (2, 6)):
        _episodes(tmp_path, generation=g, stages=[base, base, base, base, base, base])
    rep = _eval(tmp_path)
    assert rep.accepts == 1
    assert rep.decisions == {"baseline": 1, "accept": 1, "reject": 1}
    assert rep.monotonic_ok is True, rep.reasons
    assert rep.ok is True, rep.reasons
    assert rep.reasons == ()
    assert len(rep.comparable) == 2, "g1 相对 g0、g2 相对 g1，共两对"


def test_f70_selected_baseline_is_annotated(tmp_path):
    """**F70（2026-10-14，用户裁决「只做 ② 标注」）**：上一代 accept ⇒ 本代基线是
    **选择后的**测量（winner's curse），判据 1 的产物必须显式标注。

    现场口径：M3 正式实验 0 accept ⇒ 这条标注一直是空的；一旦棘轮真的转起来，
    「held-out 单调不降」的参考点就是那个候选**自己**的测量，不标注会被当成无偏参考。
    这里的期望值刻意写死为「只有 gen-002←gen-001 那一对被标注」（基线是 accept）
    —— gen-001←gen-000 的基线是 `baseline`，不是选择后的。
    """
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.55, "ci_lo": 0.53, "decision": "accept"},
        {"generation": 2, "win_rate": 0.56, "ci_lo": 0.54, "decision": "reject"},
    ])
    for g, base in ((0, 2), (1, 4), (2, 6)):
        _episodes(tmp_path, generation=g, stages=[base] * N)
    rep = _eval(tmp_path)
    selected = rep.selected_baseline_pairs
    assert [g.generation for g in selected] == [2], [g.generation for g in rep.comparable]
    assert selected[0].prev_generation == 1
    assert selected[0].baseline_selected is True
    assert rep.generations[1].baseline_selected is False, "基线是 baseline 代，不是选择后的"
    payload = rep.as_dict()
    assert payload["baseline_selection"]["selected_pairs"] == 1
    note = payload["baseline_selection"]["note"]
    assert "F70" in note and "gen-002←gen-001" in note and "winner's curse" in note


def test_f70_note_is_empty_without_any_accept(tmp_path):
    """没有 accept（正是 M3 正式实验的形态）时不得凭空写一条 F70 边界。"""
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.50, "ci_lo": 0.50, "decision": "reject"},
    ])
    for g in (0, 1):
        _episodes(tmp_path, generation=g, stages=[3] * N)
    rep = _eval(tmp_path)
    assert rep.selected_baseline_pairs == ()
    assert rep.baseline_selection_note == ""
    assert rep.as_dict()["baseline_selection"] == {"selected_pairs": 0, "note": ""}


def test_all_reject_is_a_degenerate_pass_and_must_fail(tmp_path):
    """**退化通过护栏**：每代都 reject 也能满足单调不降 ⇒ 判据 1 必须判失败。

    没有这条，「3 代都没动」会被记成验收通过（MOD-07 §12 的「≥1 次 accept」正是为它写的）。
    """
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.50, "ci_lo": 0.50, "decision": "reject"},
        {"generation": 2, "win_rate": 0.50, "ci_lo": 0.50, "decision": "reject"},
    ])
    for g in (0, 1, 2):
        _episodes(tmp_path, generation=g, stages=[3] * N)
    rep = _eval(tmp_path)
    assert rep.monotonic_ok is True, "单调性本身是满足的"
    assert rep.accepts == 0
    assert rep.ok is False
    assert any("退化通过" in r for r in rep.reasons)


# =====================================================================
# 两条通过路径
# =====================================================================

def test_ci_lo_path_passes(tmp_path):
    """路径 ①：`ci_lo(g) ≥ win_rate(g−1) − 0.02`。"""
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline"},
        # 恰好等于 prev − CRITERION_TOL ⇒ 边界取等号通过
        {"generation": 1, "win_rate": 0.45, "ci_lo": 0.50 - CRITERION_TOL,
         "decision": "accept"},
    ])
    _episodes(tmp_path, generation=0, stages=[1] * N)
    _episodes(tmp_path, generation=1, stages=[1] * N)   # 连续指标不变 ⇒ P(gap>0)=0
    rep = _eval(tmp_path)
    entry = rep.generations[1]
    assert entry.pass_ci is True and entry.pass_p_gap is False
    assert entry.passed is True
    assert entry.threshold == pytest.approx(0.50 - CRITERION_TOL)
    assert rep.monotonic_ok is True


def test_ci_drop_fails_when_both_paths_fail(tmp_path):
    """降得超过容差且连续指标没改善 ⇒ 判失败，且 reason 里要能读到两个数。"""
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.80, "ci_lo": 0.70, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.40, "ci_lo": 0.30, "decision": "accept"},
    ])
    _episodes(tmp_path, generation=0, stages=[5] * N)
    _episodes(tmp_path, generation=1, stages=[2] * N)   # 明显退步
    rep = _eval(tmp_path)
    entry = rep.generations[1]
    assert entry.pass_ci is False
    assert entry.p_gap_gt_0 == 0.0, "全退步时 P(gap>0) 必须是 0.0，不是 None"
    assert entry.p_gap_n_pairs == N
    assert entry.passed is False
    assert rep.monotonic_ok is False
    assert any("单调性不成立" in r for r in rep.reasons)


def test_p_gap_path_rescues_a_ci_dip(tmp_path):
    """路径 ②：`P(gap>0) ≥ 0.90` 可以把「CI 下降」救回来。

    win_rate 掉到 prev − 0.20（远超 0.02 容差）⇒ `pass_ci=False`；但 6 局里 5 局
    的 `stages_cleared` 上升 ⇒ 配对 bootstrap 的 `P(Δ>0)` 高 ⇒ 整代仍判通过。
    **这正是 `P(gap>0)` 存在的意义**，也是它能被判据 1 引用的唯一合理定义
    （`GainVerdict.win_rate_gap` 是 `train − heldout`，语义完全不同）。
    """
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.80, "ci_lo": 0.70, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.60, "ci_lo": 0.30, "decision": "accept"},
    ])
    _episodes(tmp_path, generation=0, stages=[3, 3, 3, 3, 3, 3])
    _episodes(tmp_path, generation=1, stages=[4, 4, 4, 4, 4, 3])   # 5/6 上升
    rep = _eval(tmp_path)
    entry = rep.generations[1]
    assert entry.pass_ci is False, "CI 掉出容差"
    assert entry.p_gap_metric == "mean_stages_cleared"
    assert entry.p_gap_gt_0 is not None and entry.p_gap_gt_0 >= P_GAP_THRESHOLD, entry.p_gap_gt_0
    assert entry.pass_p_gap is True
    assert entry.passed is True
    assert rep.monotonic_ok is True
    assert rep.ok is True


# =====================================================================
# fail-closed 与「读不到 ≠ 0」
# =====================================================================

def test_insufficient_n_is_fail_closed(tmp_path):
    """n < min_heldout_n ⇒ 明确记 reason，不静默跳过该代。"""
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.5, "ci_lo": 0.4, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.5, "ci_lo": 0.4, "decision": "accept",
         "n": N - 1},
    ])
    for g in (0, 1):
        _episodes(tmp_path, generation=g, stages=[3] * N)
    rep = _eval(tmp_path)
    assert rep.ok is False
    assert any("min_heldout_n" in r for r in rep.reasons)


def test_unreadable_episodes_give_none_not_zero(tmp_path):
    """**「无法计算」与「算出 0」必须分开**：没有逐局数据时 `p_gap_gt_0` 是 None。

    写成 0.0 会被读成「显著退步」，从而把一代错判为失败（P(gap>0)=0 < 0.90）。
    """
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.5, "ci_lo": 0.4, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.5, "ci_lo": 0.49, "decision": "accept"},
    ])
    # 故意不写任何 episodes/
    rep = _eval(tmp_path)
    entry = rep.generations[1]
    assert entry.p_gap_gt_0 is None
    assert entry.p_gap_n_pairs == 0
    assert entry.pass_p_gap is None
    assert entry.pass_ci is True, "CI 路径仍可用 ⇒ 整代通过"
    assert entry.passed is True


def test_paired_p_gap_is_reproducible_and_seed_sensitive():
    """判定器必须**可复算**（NFR-08-01）：同 seed 逐位相同，换 seed 会动。

    用混合场景（一半上升一半下降）让结果**真的依赖重抽** —— 全上升/全下降两种极端下
    `P(Δ>0)` 恒为 1.0/0.0，换 seed 也不会变，那样这个用例证明不了任何东西。
    """
    from dice_ratchet.evals.acceptance import _paired_p_gap

    a = {i: 3 for i in range(10)}
    b = {i: (3 + (1 if i % 2 == 0 else -1)) for i in range(10)}   # 5 升 5 降
    same_a = _paired_p_gap(a, b, seed=99, B=800)
    same_b = _paired_p_gap(a, b, seed=99, B=800)
    assert same_a == same_b, "同 seed 必须逐位相同（可复算）"
    assert 0.0 < same_a[0] < 1.0, f"混合场景的 P(Δ>0) 应落在开区间，实际 {same_a[0]}"
    others = {_paired_p_gap(a, b, seed=s, B=800)[0] for s in (1, 2, 3, 4)}
    assert len(others) > 1, "换 seed 必须影响重抽结果（否则 RNG 没被用上）"


def test_paired_p_gap_only_uses_shared_seeds():
    """配对 bootstrap **只在共有 seed 上**做（配对检验的前提）。"""
    from dice_ratchet.evals.acceptance import _paired_p_gap

    a = {1: 3, 2: 3, 3: 3}
    b = {1: 4, 2: 4}                       # seed 3 只有 a 有
    p, n = _paired_p_gap(a, b, seed=7, B=200)
    assert n == 2, "只应配对共有的 seed"
    assert p == 1.0
    assert _paired_p_gap({1: 1}, {2: 2}, seed=7, B=50) == (None, 0), "无共有 seed ⇒ None"


# =====================================================================
# 台账读取口径（F48 之后）
# =====================================================================

def test_train_rows_are_ignored(tmp_path):
    """台账里 train 行的 `win_rate=0.99` 不得被当成数据点。

    F48 之后台账是「每代每 split 一行」。不过滤 `split` 会让 train 行混进来，
    判据 1 的数字立刻失去物理含义。
    """
    _write_ledger(tmp_path, [
        {"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline"},
        {"generation": 1, "win_rate": 0.55, "ci_lo": 0.53, "decision": "accept"},
    ])
    for g in (0, 1):
        _episodes(tmp_path, generation=g, stages=[3] * N)
    rep = _eval(tmp_path)
    assert [g.generation for g in rep.generations] == [0, 1]
    assert rep.generations[1].win_rate == 0.55, "不是 train 行的 0.99"


def test_duplicate_generation_rows_take_last(tmp_path):
    """同代重复行取末条（历史台账实测有过 5 行同值）。"""
    path = tmp_path / "metrics" / "generations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(GENERATIONS_COLUMNS))
        writer.writeheader()
        for win in (0.10, 0.20, 0.90):
            writer.writerow({"generation": 0, "split": "heldout", "n": N,
                             "win_rate": win, "ci_lo": win, "decision": "baseline"})
        writer.writerow({"generation": 1, "split": "heldout", "n": N,
                         "win_rate": 0.95, "ci_lo": 0.90, "decision": "accept"})
    _episodes(tmp_path, generation=0, stages=[3] * N)
    _episodes(tmp_path, generation=1, stages=[3] * N)
    rep = _eval(tmp_path)
    assert rep.generations[0].win_rate == 0.90, "重复行取末条"


def test_missing_ledger_raises(tmp_path):
    from dice_ratchet.infra.errors import E, InfraError

    with pytest.raises(InfraError) as exc:
        _eval(tmp_path)
    assert exc.value.code == E.E_IO_STATE_MISSING


# =====================================================================
# §3.3：C5 的 `tol` 的最小可判效应（MDE）换算 —— 零成本
# =====================================================================

def test_tol_sensitivity_is_axis_specific_not_one_number():
    """**核心口径**：同一个 `0.05` 在三个轴上是完全不同的量。

    返回三轴各自的「相当于多少个原始单位 / 多少个 se」。若实现把三轴当同一个数
    （比如只返回一个 `0.05`），本用例会失败——而那个误读会让「0.05 到底能拦什么」
    这个问题得到错误答案。
    """
    from dice_ratchet.evals.tol_sensitivity import tolerance_sensitivity

    s = tolerance_sensitivity(rates={"illegal": 0.0318, "fallback": 0.0017,
                                     "truncation": 0.30})
    by = {a.axis: a for a in s.axes}
    assert set(by) == {"illegal", "fallback", "truncation"}

    # 粒度必须分列
    assert by["illegal"].granularity == "step"
    assert by["fallback"].granularity == "step"
    assert by["truncation"].granularity == "episode"

    # 步级轴：tol 换算成几百到上千个「步级事件」，且远大于 1 个 se
    for axis in ("illegal", "fallback"):
        assert by[axis].raw_delta > 100, by[axis].raw_delta
        assert by[axis].tol_in_se > 10, by[axis].tol_in_se
    # 局级轴：tol 只相当于几局
    assert 1 <= by["truncation"].raw_delta <= 4, by["truncation"].raw_delta
    assert by["truncation"].tol_in_se < 3

    # reading 必须自解释（报告直接引用它）
    assert "步级事件" in by["illegal"].reading()
    assert "局" in by["truncation"].reading()


def test_tol_sensitivity_disclaims_calibration():
    """**诚实边界必须在产物里**：这是 MDE 不是标定值。"""
    from dice_ratchet.evals.tol_sensitivity import tolerance_sensitivity

    payload = tolerance_sensitivity().as_dict()
    assert "最小可判效应" in payload["disclaimer"]
    assert "不是" in payload["disclaimer"]
    assert any("未做" in n for n in payload["notes"])


def test_tol_sensitivity_uses_measured_denominators_when_given():
    """传入实测分母时必须**覆盖**缺省估算，并在产物里看得出差别。"""
    from dice_ratchet.evals.tol_sensitivity import tolerance_sensitivity

    default = {a.axis: a for a in tolerance_sensitivity().axes}
    measured = {a.axis: a for a in tolerance_sensitivity(
        denominators={"illegal": 1000.0, "fallback": 500.0, "truncation": 10.0}).axes}
    assert measured["illegal"].raw_delta == 50.0          # 0.05 × 1000
    assert measured["truncation"].raw_delta == 0.5
    assert measured["illegal"].raw_delta != default["illegal"].raw_delta


def test_unknown_axis_is_rejected():
    """未知轴必须显式报错（否则会静默算出一个没有意义的数）。"""
    from dice_ratchet.evals.tol_sensitivity import axis_sensitivity

    with pytest.raises(ValueError):
        axis_sensitivity("made_up_axis", tol=0.05)


def test_denominators_from_run_reads_real_episodes(tmp_path):
    """实测分母：步级 = 成功步 + 非法尝试；局级 = 局数。"""
    from dice_ratchet.evals.tol_sensitivity import denominators_from_run

    d = tmp_path / "r"
    ep = d / "episodes" / "heldout"
    ep.mkdir(parents=True)
    for i in range(3):
        (ep / f"e-{731000+i}-g000-parent.jsonl").write_text(json.dumps({
            "type": "episode_end", "seed": 731000 + i, "split": "heldout",
            "generation": 0, "arm": "parent", "outcome": "win", "stages_cleared": 3,
            "hearts_left": 1, "battles_won": 5, "decisions": 10, "steps": 100,
            "illegal_attempts": 4, "fallback_decisions": 1, "kind_counts": [0] * 16,
            "decision_kinds": [0], "slot_counts": {}, "seq_hash": "s",
        }) + "\n", encoding="utf-8")
    got = denominators_from_run(d)
    assert got["illegal"] == 3 * (100 + 4), "步级分母 = 成功步 + 非法尝试"
    assert got["fallback"] == 3 * 10, "fallback 分母 = 决策点"
    assert got["truncation"] == 3.0, "局级分母 = 局数"
    assert denominators_from_run(tmp_path / "nope") == {}


# =====================================================================
# F57（2026-10-12）：判据 1 读**起点臂**行；起点未变的相邻代**豁免**
# =====================================================================

def _write_ledger_rows(run_dir: Path, rows: list[dict]) -> None:
    """写台账的**显式行**（可逐行指定 ``arm`` / ``harness_version``）。"""
    import csv as _csv

    path = run_dir / "metrics" / "generations.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = _csv.DictWriter(fh, fieldnames=list(GENERATIONS_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_f57_judge_reads_parent_rows_and_ignores_candidate_rows(tmp_path):
    """F57 的核心：同代有 parent 与候选两行时，判据 1 必须读 **parent**。

    现场（`runs/_sweep/m3-rehearsal` gen-001）：parent 行 `0.10` 被判「单调性不成立」，
    20 分钟后候选行 `0.35` 覆盖同一 ``(generation, split)`` 键 ⇒ 同一批产物给出
    `monotonic_ok=true`。三键之后两行共存，判定器**显式**只读 parent。
    """
    _write_ledger_rows(tmp_path, [
        {"generation": 0, "split": "heldout", "arm": "parent", "harness_version": 0,
         "n": N, "win_rate": 0.15, "ci_lo": 0.05, "ci_hi": 0.36, "decision": "baseline"},
        {"generation": 1, "split": "heldout", "arm": "parent", "harness_version": 0,
         "n": N, "win_rate": 0.10, "ci_lo": 0.03, "ci_hi": 0.30},
        {"generation": 1, "split": "heldout", "arm": "cand-abc", "harness_version": 1,
         "n": N, "win_rate": 0.90, "ci_lo": 0.80, "ci_hi": 0.99},
    ])
    _write_report(tmp_path, generation=0, decision="baseline")
    _write_report(tmp_path, generation=1, decision="reject")
    report = _eval(tmp_path)
    assert [g.generation for g in report.generations] == [0, 1], (
        "候选行不得当成第二个数据点")
    assert report.generations[1].win_rate == 0.10, "读的必须是 parent 行"
    # 两代同 harness_version ⇒ 起点没变 ⇒ 无处可判（不是失败，也不是通过）
    assert report.generations[1].not_applicable is True
    assert len(report.comparable) == 0


def test_f57_same_harness_version_pair_is_not_applicable(tmp_path):
    """F57 豁免：相邻两代 ``harness_version`` 相同 ⇒ ``not_applicable``。

    「held-out 不降」在同一份 harness 的两次重测之间是恒真命题。判成失败会把**合法拒绝**
    误记成「单调性破坏」（现场 gen-001：`ci_lo=0.0279 < 0.10−0.02`——而 0.15 与 0.10
    都来自 v0，5pp 就是本 run 的噪声底）；判成通过则虚增可比对数（退化通过）。
    """
    _write_ledger_rows(tmp_path, [
        {"generation": 0, "split": "heldout", "arm": "parent", "harness_version": 0,
         "n": N, "win_rate": 0.15, "ci_lo": 0.05, "ci_hi": 0.36, "decision": "baseline"},
        {"generation": 1, "split": "heldout", "arm": "parent", "harness_version": 0,
         "n": N, "win_rate": 0.10, "ci_lo": 0.03, "ci_hi": 0.30, "decision": "reject"},
        {"generation": 2, "split": "heldout", "arm": "parent", "harness_version": 1,
         "n": N, "win_rate": 0.40, "ci_lo": 0.20, "ci_hi": 0.60, "decision": "accept"},
    ])
    _write_report(tmp_path, generation=0, decision="baseline")
    _write_report(tmp_path, generation=1, decision="reject")
    _write_report(tmp_path, generation=2, decision="accept")
    report = _eval(tmp_path)
    by_gen = {g.generation: g for g in report.generations}
    assert by_gen[1].not_applicable is True, "v0 → v0：起点没变"
    assert by_gen[1].harness_version == 0 and by_gen[1].prev_harness_version == 0
    assert by_gen[2].not_applicable is False, "v0 → v1：起点变了，必须判"
    assert [g.generation for g in report.comparable] == [2], (
        "豁免的代不占「可比」名额")
    assert report.accepts == 1


def test_f57_missing_harness_version_does_not_exempt(tmp_path):
    """fail-closed：``harness_version`` 读不到时**不做**豁免（宁可照旧判）。

    把缺列当 0 会把任意两代都当成「同一起点」而全部豁免——那等于关掉判据 1。
    """
    _write_ledger_rows(tmp_path, [
        {"generation": 0, "split": "heldout", "arm": "parent",
         "n": N, "win_rate": 0.60, "ci_lo": 0.40, "ci_hi": 0.80, "decision": "baseline"},
        {"generation": 1, "split": "heldout", "arm": "parent",
         "n": N, "win_rate": 0.10, "ci_lo": 0.03, "ci_hi": 0.30, "decision": "accept"},
    ])
    _write_report(tmp_path, generation=0, decision="baseline")
    _write_report(tmp_path, generation=1, decision="accept")
    report = _eval(tmp_path)
    assert report.generations[1].not_applicable is False
    assert report.generations[1].harness_version is None
    assert len(report.comparable) == 1, "缺列 ⇒ 照旧判（这一对明显不成立）"
    assert report.monotonic_ok is False


# =====================================================================
# D-M3-7（2026-10-14）：同起点豁免改判 **play 面指纹**（版本号不再可信）
# =====================================================================

def _write_report_full(run_dir: Path, *, generation: int, decision: str,
                       play_hash: str, harness_version: int,
                       heldout_source: str = "measured") -> None:
    d = run_dir / "generations" / f"gen-{generation:03d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({
        "generation": generation, "harness_version": harness_version,
        "parent_version": max(0, harness_version - 1), "decision": decision,
        "harness_play_hash": play_hash,
        "decided_by": "auto", "reason": "",
        "heldout": {"source": heldout_source}, "train": {},
    }, ensure_ascii=False), encoding="utf-8")


def test_dm35_same_play_hash_is_not_applicable_even_when_versions_differ(tmp_path):
    """相邻两代 **play_hash 相同**（同一份起点）⇒ ``not_applicable``，**即便版本号不同**。

    为什么这是必需的：拒绝时版本号也会推进（D2=A：``v_{N+1} = v_N + 死路记录``），
    于是「版本号相同」不再是「起点逐字节相同」的充分条件。若仍按版本号豁免，
    同一份 harness 的两次重测会被当成两个真实起点去判单调性（F57 要修的正是这件事）。
    """
    ph = "sha256:" + "a" * 64
    _write_ledger(tmp_path, [{"generation": 0, "win_rate": 0.50, "ci_lo": 0.40,
                              "decision": "baseline"},
                             {"generation": 1, "win_rate": 0.10, "ci_lo": 0.02,
                              "decision": "reject"}])
    _write_report_full(tmp_path, generation=0, decision="baseline", play_hash=ph,
                       harness_version=0)
    _write_report_full(tmp_path, generation=1, decision="reject", play_hash=ph,
                       harness_version=1, heldout_source="inherited:gen-000")
    rep = _eval(tmp_path)
    assert rep.generations[1].not_applicable is True
    assert rep.generations[1].play_hash == ph
    assert len(rep.comparable) == 0 and len(rep.not_applicable_pairs) == 1


def test_dm35_different_play_hash_is_compared_even_when_versions_match(tmp_path):
    """play_hash 不同 ⇒ **必须判**（即便台账里的 ``harness_version`` 恰好相同）。

    反例：补丁被接受后版本号推进、但两人工件的 ``harness_version`` 列被写坏成同一个值
    （F60 现场的同类污染）。旧判据（版本号相同 ⇒ 豁免）会**静默跳过**一代真实比较。
    """
    _write_ledger(tmp_path, [{"generation": 0, "win_rate": 0.50, "ci_lo": 0.40,
                              "decision": "baseline", "harness_version": 0},
                             {"generation": 1, "win_rate": 0.10, "ci_lo": 0.02,
                              "decision": "reject", "harness_version": 0}])
    _write_report_full(tmp_path, generation=0, decision="baseline",
                       play_hash="sha256:" + "a" * 64, harness_version=0)
    _write_report_full(tmp_path, generation=1, decision="reject",
                       play_hash="sha256:" + "b" * 64, harness_version=0)
    _episodes(tmp_path, generation=0, stages=[6, 6, 6, 6, 6, 6])
    _episodes(tmp_path, generation=1, stages=[1, 1, 1, 1, 1, 1])
    rep = _eval(tmp_path)
    assert rep.generations[1].not_applicable is False
    assert len(rep.comparable) == 1
    assert rep.generations[1].pass_ci is False, "0.02 < 0.50 − 0.02 ⇒ 单调性不成立"


def test_dm35_missing_play_hash_falls_back_to_version_equality(tmp_path):
    """旧 run 的 ``report.json`` **没有** play_hash ⇒ 回落「版本号相同」判据（不静默放宽）。"""
    _write_ledger(tmp_path, [{"generation": 0, "win_rate": 0.50, "ci_lo": 0.40,
                              "decision": "baseline"},
                             {"generation": 1, "win_rate": 0.10, "ci_lo": 0.02,
                              "decision": "reject"}])
    # 只改台账里的 harness_version：report.json 里没有 harness_play_hash
    rows = [{"generation": 0, "win_rate": 0.50, "ci_lo": 0.40, "decision": "baseline",
             "harness_version": 3},
            {"generation": 1, "win_rate": 0.10, "ci_lo": 0.02, "decision": "reject",
             "harness_version": 3}]
    _write_ledger(tmp_path, rows)
    for gen in (0, 1):                   # 两代台账版本号相同 ⇒ 旧判据豁免
        _write_report_full(tmp_path, generation=gen,
                           decision="baseline" if gen == 0 else "reject",
                           play_hash="", harness_version=3)
    rep = _eval(tmp_path)
    assert rep.generations[1].not_applicable is True
    assert rep.generations[1].play_hash == ""


def test_dm35_episode_stages_follows_the_inheritance_chain(tmp_path):
    """继承来的 heldout 段**本代没有局**：``P(gap>0)`` 必须去**来源代**取逐局数据。

    否则 ``_episode_stages(g)`` 拿到空集 ⇒ ``p_gap=None`` ⇒ 判据 1 只剩 ``pass_ci``
    一条路（第二判定路径被静默废掉）。
    """
    from dice_ratchet.evals.acceptance import _episode_stages, _inherited_origin

    _write_report_full(tmp_path, generation=1, decision="reject",
                       play_hash="sha256:" + "a" * 64, harness_version=1)
    _write_report_full(tmp_path, generation=2, decision="reject",
                       play_hash="sha256:" + "a" * 64, harness_version=2,
                       heldout_source="inherited:gen-001")
    _write_report_full(tmp_path, generation=3, decision="reject",
                       play_hash="sha256:" + "a" * 64, harness_version=3,
                       heldout_source="inherited:gen-002")
    _episodes(tmp_path, generation=1, stages=[6, 6, 6, 6, 6, 6])

    assert _inherited_origin(tmp_path, 3) == 1, "链要一路跟随到真正测过的那一代"
    assert _episode_stages(tmp_path, 2) == _episode_stages(tmp_path, 1)
    assert len(_episode_stages(tmp_path, 3)) == 6, "必须能读到来源代的逐局数据"
