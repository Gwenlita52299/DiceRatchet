"""M3 彩排判据（D-M3-5 裁决 A；Spec v2.14 / B-70）的回归护栏。

**这个文件存在的理由**：彩排判据如果靠人眼读文档，彩排本身就会给出假阳性。
`F48` 刚证明过——「5 张 PNG 存在」这条判据**会被空白图通过**
（实测空白图 20–27 KB、非白像素比例 3.43%，有数据图 3.60%，**分不开**）。

本文件逐条钉住 6 项判据，重点在两处**反例自检**：

- ``test_plots_check_rejects_blank_plots``：**有文件但没有数据点**必须判不合格。
  这条如果写成「检查文件存在」，F48 那一类缺陷会再次溜过彩排。
- ``test_one_shot_check_rejects_h1_and_test_seeds``：H1/H2/test 的 seed 一旦出现必须判不合格。
  D-M3-5 的核诉求就是「不碰一次性资源」，这里把它变成代码断言而不是文档纪律。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from dice_ratchet.evals.harness import GENERATIONS_COLUMNS
from dice_ratchet.evals.rehearsal import (REJECT_WHITELIST, PLOT_FILES_REQUIRED,
                                          run_rehearsal_checks)

H3_SEED = 731080          # 彩排允许
H1_SEED = 731000          # 一次性（代际裁判）
H2_SEED = 731040          # 一次性（消融）
TEST_SEED = 947000        # 一次性（终检，locked）


# =====================================================================
# 构造一个「合格」的彩排 run
# =====================================================================

def _make_run(tmp_path: Path, *, gens=(0, 1), plots: bool = True,
              with_train_row: bool = True, heldout_seeds=(H3_SEED,),
              arms: bool = True, decisions=("baseline", "accept")) -> Path:
    run = tmp_path / "m3-rehearsal"
    (run / "metrics").mkdir(parents=True, exist_ok=True)

    # 台账：每代 heldout 一行（+ train 一行）
    with (run / "metrics" / "generations.csv").open("w", encoding="utf-8",
                                                   newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(GENERATIONS_COLUMNS))
        w.writeheader()
        for gen, dec in zip(gens, decisions):
            w.writerow({"generation": gen, "split": "heldout", "basket": "H3",
                        "n": 20, "win_rate": 0.50 + 0.05 * gen, "ci_lo": 0.50,
                        "ci_hi": 0.80, "mean_stages_cleared": 3.0 + gen,
                        "mean_hearts_left": 1.0, "entropy_mean_norm": 0.7,
                        "win_rate_gap": 0.05, "decision": dec})
            if with_train_row:
                w.writerow({"generation": gen, "split": "train", "n": 30,
                            "win_rate": 0.6, "ci_lo": 0.5, "ci_hi": 0.7,
                            "mean_stages_cleared": 3.5, "mean_hearts_left": 1.1,
                            "entropy_mean_norm": 0.68})

    # 判决记录（含 candidates，供机制性失败检查）
    for gen, dec in zip(gens, decisions):
        d = run / "generations" / f"gen-{gen:03d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.json").write_text(json.dumps({
            "generation": gen, "harness_version": gen, "decision": dec,
            "error_code": None,
            "candidates": [{"candidate_id": "c1", "decision": "reject",
                            "error_code": "E_EVOLVE_NO_GAIN"}],
        }, ensure_ascii=False), encoding="utf-8")

    # harness 版本目录：`decision="accept"` 的代必须有对应 `versions/vN`
    # （否则 `mechanical_failures_zero` 会（正确地）报幻影 accept）
    for gen, dec in zip(gens, decisions):
        if dec == "accept":
            (tmp_path / "harness" / "versions" / f"v{gen}").mkdir(parents=True,
                                                                 exist_ok=True)

    # 逐局（heldout 只用 H3）
    ep = run / "episodes" / "heldout"
    ep.mkdir(parents=True, exist_ok=True)
    for gen in gens:
        for i, seed in enumerate(heldout_seeds):
            (ep / f"e-{seed:06d}-g{gen:03d}-parent.jsonl").write_text(
                json.dumps({"type": "episode_end", "seed": seed, "generation": gen,
                            "split": "heldout", "arm": "parent", "outcome": "win",
                            "stages_cleared": 3 + gen, "hearts_left": 1,
                            "battles_won": 5, "decisions": 80, "steps": 200,
                            "illegal_attempts": 0, "fallback_decisions": 0,
                            "kind_counts": [0] * 16, "decision_kinds": [0],
                            "slot_counts": {}, "seq_hash": "s"}) + "\n",
                encoding="utf-8")

    # 判据 1 判定器的产物
    (run / "metrics" / "acceptance.json").write_text(json.dumps({"ok": True}),
                                                     encoding="utf-8")

    # 臂归档
    if arms:
        (run / "metrics" / "arms").mkdir(parents=True, exist_ok=True)
        for gen in gens:
            (run / "metrics" / "arms" / f"gen{gen:03d}-heldout-H3-parent.json").write_text(
                "{}", encoding="utf-8")

    # 绘图（真实渲染；plots=False 时故意不画）
    if plots:
        from dice_ratchet.infra.plotting import plot_generations

        plot_generations(run / "metrics" / "generations.csv", run / "plots")
    return run


def _check(run: Path, name: str):
    rep = run_rehearsal_checks(run, run_id="m3-rehearsal")
    return {c.name: c for c in rep.checks}[name], rep


# =====================================================================
# 合格路径
# =====================================================================

def test_fully_valid_rehearsal_passes(tmp_path):
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path)
    rep = run_rehearsal_checks(run, run_id="m3-rehearsal")
    assert rep.ok is True, [c.detail for c in rep.failed]
    assert {c.name for c in rep.checks} == {
        "mechanical_failures_zero", "plots_have_data_points", "ledger_has_both_splits",
        "acceptance_judge_runs", "arm_archive_present", "one_shot_resources_untouched"}


# =====================================================================
# 判据 ②：**空白图必须判不合格**（F48 的核心教训）
# =====================================================================

def test_plots_check_rejects_missing_plots(tmp_path):
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path, plots=False)
    check, rep = _check(run, "plots_have_data_points")
    assert check.ok is False
    assert rep.ok is False
    assert all(name in check.detail for name in PLOT_FILES_REQUIRED[:2])


def test_plots_check_rejects_blank_plots(tmp_path):
    """**反例自检**：文件存在、大小正常，但图里**没有数据点** ⇒ 不合格。

    构造方式：删掉台账里的指标列（其余列保留）后重新画——这正是 F48 的现场
    （列名对不上 ⇒ `_g()` 静默返回空 ⇒ 5 张空白 PNG「生成成功」）。
    若判据写成「文件存在」，本用例会**通过**（假阳性）。
    """
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path, plots=False)
    # 只留 generation/split/n，指标列全空 → 画出来的是空白图
    with (run / "metrics" / "generations.csv").open("w", encoding="utf-8",
                                                   newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(GENERATIONS_COLUMNS))
        w.writeheader()
        for gen in (0, 1):
            w.writerow({"generation": gen, "split": "heldout", "n": 20})
    # 用「旧行为」造 5 个文件：直接调 plot_generations 会被 E_IO_PLOT_NO_DATA 拦下，
    # 所以这里手工造同尺寸的假 PNG（代表「文件在、但里面没有数据」）。
    plots = run / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    for name in PLOT_FILES_REQUIRED:
        (plots / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20000)
    check, rep = _check(run, "plots_have_data_points")
    assert check.ok is False, "空白图/占位图必须判不合格"
    assert "E_IO_PLOT_NO_DATA" in check.detail or "没有数据点" in check.detail
    assert rep.ok is False


def test_plots_check_counts_real_points(tmp_path):
    """对照面：真实的图必须**数得出**点数（否则判据太松，什么都拦不住）。"""
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path)
    check, _rep = _check(run, "plots_have_data_points")
    assert check.ok is True
    assert set(check.evidence["data_points"]) == set(PLOT_FILES_REQUIRED)
    assert all(v > 0 for v in check.evidence["data_points"].values()), \
        check.evidence["data_points"]


# =====================================================================
# 判据 ③：台账两个 split 都要有（F48）
# =====================================================================

def test_ledger_without_train_rows_fails(tmp_path):
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path, with_train_row=False)
    check, rep = _check(run, "ledger_has_both_splits")
    assert check.ok is False
    assert "train" in check.detail
    assert rep.ok is False


# =====================================================================
# 判据 ①：机制性失败为零（M2 §3.2 的 5 条 + 正向白名单）
# =====================================================================

@pytest.mark.parametrize("decision,code,needle", [
    ("error_reject", None, "error_reject"),
    ("reject", "E_EVOLVE_PATCH_INVALID", "不在裁判拒绝白名单"),
    ("reject", "E_IO_GIT_DIRTY", "E_IO_*"),
])
def test_mechanical_failures_are_caught(tmp_path, decision, code, needle):
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path)
    # 改写 gen-001 的判决记录
    p = run / "generations" / "gen-001" / "report.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["decision"] = decision
    raw["error_code"] = code
    raw["candidates"] = []
    p.write_text(json.dumps(raw), encoding="utf-8")
    check, rep = _check(run, "mechanical_failures_zero")
    assert check.ok is False, f"{decision}/{code} 应算机制性失败"
    assert any(needle in prob for prob in check.evidence["problems"])
    assert rep.ok is False


def test_reject_whitelist_is_honoured(tmp_path):
    """**反例自检的对称面**：白名单内的裁判拒绝**不算**机制性失败。

    「候选被合法拒绝」是科学结论，不是缺陷（M2 §3.2 的改写正是为了这个）。
    若把 `E_EVOLVE_NO_GAIN` 也算成失败，彩排会在最正常的情况下判不合格。
    """
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path)
    p = run / "generations" / "gen-001" / "report.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["decision"] = "reject"
    raw["error_code"] = "E_EVOLVE_NO_GAIN"
    p.write_text(json.dumps(raw), encoding="utf-8")
    check, _rep = _check(run, "mechanical_failures_zero")
    assert check.ok is True, check.detail
    assert "E_EVOLVE_NO_GAIN" in REJECT_WHITELIST


def test_phantom_accept_is_caught(tmp_path):
    """`decision=accept` 但 `harness/versions/vN` 不存在（幻影 accept，F47-D）。

    夹具只给接受的代建了 `versions/v{gen}`，所以这里把 `harness_version` 指到一个
    **没有**目录的版本号上，模拟「报告说接受了、磁盘上没有那个版本」。
    **反例自检**：把 `harness_version` 改回 1（夹具建过 `v1`）时本判据必须放过——
    否则它会把每个合法的 accept 都当成幻影。
    """
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path)
    p = run / "generations" / "gen-001" / "report.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["decision"] = "accept"

    raw["harness_version"] = 1        # 夹具建过 v1 ⇒ 合法
    p.write_text(json.dumps(raw), encoding="utf-8")
    ok_check, _ = _check(run, "mechanical_failures_zero")
    assert ok_check.ok is True, ok_check.detail

    raw["harness_version"] = 7        # 没有 v7 ⇒ 幻影
    p.write_text(json.dumps(raw), encoding="utf-8")
    check, _rep = _check(run, "mechanical_failures_zero")
    assert check.ok is False
    assert any("幻影" in prob for prob in check.evidence["problems"])


# =====================================================================
# 判据 ④：判据 1 判定器要能给出结论
# =====================================================================

def test_single_generation_fails_the_judge_check(tmp_path):
    """只跑 1 代 ⇒ 判定器 `comparable=0`，给不出「单调性」结论 ⇒ 彩排不合格。

    这条是「彩排至少要 2 代」的代码表达（D-M3-5 的规模裁决）。
    """
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path, gens=(0,), decisions=("baseline",))
    check, _rep = _check(run, "acceptance_judge_runs")
    assert check.ok is False
    assert "comparable" in check.detail or "可比较" in check.detail


# =====================================================================
# 判据 ⑥：**一次性资源**（本文件的重点）
# =====================================================================

@pytest.mark.parametrize("seed,zone", [
    (H1_SEED, "H1"), (H2_SEED, "H2"), (TEST_SEED, "test"),
])
def test_one_shot_check_rejects_forbidden_seeds(tmp_path, seed, zone):
    """H1 / H2 / test 的 seed 一旦出现在彩排的 heldout 里 ⇒ **必须判不合格**。

    D-M3-5 的核诉求就是把「不碰一次性资源」从文档纪律变成**代码断言**。
    （`TEST_SEED=947000` 走的是「声明之外的 seed」分支——test 与 heldout 是不同 split，
    真跑时根本不会落进 `episodes/heldout/`，这里同时覆盖「手工塞进去」的情形。）
    """
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path, heldout_seeds=(H3_SEED, seed))
    check, rep = _check(run, "one_shot_resources_untouched")
    assert check.ok is False, f"{zone} 的 seed {seed} 必须被判违规"
    assert rep.ok is False
    assert any(zone in v or str(seed) in v for v in check.evidence["violations"])


def test_one_shot_check_rejects_test_attestation(tmp_path):
    """存在 `test_attestation.json` ⇒ 一次性资源已被消耗 ⇒ 不合格。"""
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path)
    d = run / "generations" / "gen-001"
    (d / "test_attestation.json").write_text("{}", encoding="utf-8")
    check, _rep = _check(run, "one_shot_resources_untouched")
    assert check.ok is False
    assert any("test_attestation" in v for v in check.evidence["violations"])


def test_one_shot_check_accepts_h3_only(tmp_path):
    """对照面：heldout 全在 H3 内 ⇒ 通过（否则判据太严，彩排根本没法做）。"""
    pytest.importorskip("matplotlib")
    run = _make_run(tmp_path, heldout_seeds=(H3_SEED, H3_SEED + 1, H3_SEED + 19))
    check, rep = _check(run, "one_shot_resources_untouched")
    assert check.ok is True, check.detail
    assert rep.ok is True, [c.name for c in rep.failed]


def test_rehearsal_script_never_mentions_forbidden_zones():
    """**脚本级护栏**：彩排脚本里不得出现 H1/H2/test 的区间字面量。

    人是会改脚本的。这条把「不碰一次性资源」钉在脚本文本上——与
    `test_one_shot_check_rejects_forbidden_seeds` 一内一外。
    """
    script = (Path(__file__).resolve().parents[3] / "scripts"
              / "run_m3_rehearsal.sh").read_text(encoding="utf-8")
    # 只看**可执行行**：注释里出现这些数字是为了说明纪律（「一律不出现」），不是使用它们。
    code = [ln for ln in script.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    executable = "\n".join(code)
    for forbidden in ("731000", "731039", "731040", "731079", "947000", "947099"):
        assert forbidden not in executable, f"彩排脚本的可执行行里出现了禁用区间 {forbidden}"
    assert "731080" in script, "彩排脚本必须显式声明用 H3"
