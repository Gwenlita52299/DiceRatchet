"""T-09 / T-14 / T-15 + FR-08-07…10/23：执行器、确定性、报告 schema round-trip。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import validate
from dice_ratchet.evals import seeds as S
from dice_ratchet.evals.harness import (ArmResult, EvalSpec, append_generations_csv,
                                     bootstrap_seed, check_report_determinism,
                                     compare_paired, evaluate_arm, evaluate_generation,
                                     generation_report_path, run_eval,
                                     write_generation_report)
from dice_ratchet.evals.metrics import (aggregate, bootstrap_mean_ci, judge_gain,
                                     paired_bootstrap_gap, render_report_sections)
from dice_ratchet.evals.diversity import CollapseConfig, collapse_check

from evals_support import FakeRunner, make_result

_PROTO = S.default_protocol()
# F45-2：heldout H1 已是六位端口空间（731000-731039），从协议取起点
_H1_START = S.default_protocol().splits["heldout"].baskets["H1"].start
_SEEDS = tuple(range(_H1_START, _H1_START + 12))


def _spec(**kw):
    base = dict(run_id="r-1", generation=1, harness_version=1, split="heldout",
                basket="H1", seeds=_SEEDS, n_workers=1)
    base.update(kw)
    return EvalSpec(**base)  # type: ignore[arg-type]


# ---- FR-08-07/08：并行 = 串行（T-15）-----------------------------------------------

def test_parallel_equals_serial_byte_for_byte():
    outcomes = {seed: ("win" if seed % 3 else "lose") for seed in _SEEDS}
    serial = run_eval(_spec(n_workers=1), runner=FakeRunner(outcomes), protocol=_PROTO)
    parallel = run_eval(_spec(n_workers=4), runner=FakeRunner(outcomes), protocol=_PROTO)
    assert serial.metrics.as_dict() == parallel.metrics.as_dict()
    assert [r.as_dict() for r in serial.results] == [r.as_dict() for r in parallel.results]


def test_results_sorted_by_seed():
    spec = _spec(seeds=tuple(reversed(_SEEDS)))
    result = run_eval(spec, runner=FakeRunner(), protocol=_PROTO)
    assert [r.seed for r in result.results] == sorted(_SEEDS)


# ---- FR-08-10：dropped / degraded 语义 ---------------------------------------------

def test_dropped_counted_as_lose_and_flagged_low_confidence():
    outcomes = {seed: "win" for seed in _SEEDS}
    runner = FakeRunner(outcomes, fail_seeds=[_H1_START, _H1_START + 1])
    result = run_eval(_spec(), runner=runner, protocol=_PROTO)
    assert result.metrics.n == len(_SEEDS)
    assert result.metrics.n_dropped == 2
    assert result.metrics.win_rate == pytest.approx(10 / 12)
    assert str(E.E_EVAL_INSUFFICIENT_N) in result.error_codes
    with pytest.raises(InfraError) as exc:
        result.raise_if_low_confidence()
    assert exc.value.code == E.E_EVAL_INSUFFICIENT_N


def test_degraded_excluded_from_n():
    runner = FakeRunner(default="win", degraded_seeds=[_H1_START, _H1_START + 1])
    result = run_eval(_spec(), runner=runner, protocol=_PROTO)
    assert result.metrics.n == len(_SEEDS) - 2
    assert result.metrics.degraded_n == 2
    assert result.metrics.n_dropped == 0


def test_budget_marks_remaining_dropped():
    runner = FakeRunner(default="win")
    result = run_eval(_spec(budget_seconds=0), runner=runner, protocol=_PROTO)
    assert result.budget_exceeded is True
    assert str(E.E_EVAL_BUDGET_EXCEEDED) in result.error_codes
    assert result.metrics.n_dropped == len(_SEEDS)


# ---- FR-08-01/05：执行器也走 INV-5 隔离检查 -----------------------------------------

def test_run_eval_rejects_cross_split_seeds():
    with pytest.raises(InfraError) as exc:
        run_eval(_spec(seeds=(1000, _H1_START)), runner=FakeRunner(), protocol=_PROTO)
    assert exc.value.code == E.E_EVAL_SEED_OVERLAP


# ---- T-09 确定性 -------------------------------------------------------------------

def test_bootstrap_seed_formula():
    import hashlib

    expected = int.from_bytes(hashlib.sha256(b"r-1:3:H1").digest()[:8], "big")
    assert bootstrap_seed("r-1", 3, "H1") == expected


def test_bootstrap_same_rng_seed_is_bit_identical():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    a = bootstrap_mean_ci(values, B=200, rng=np.random.default_rng(42))
    b = bootstrap_mean_ci(values, B=200, rng=np.random.default_rng(42))
    assert a == b
    gap_a = paired_bootstrap_gap(values, [0.0] * 5, B=200, rng=np.random.default_rng(7))
    gap_b = paired_bootstrap_gap(values, [0.0] * 5, B=200, rng=np.random.default_rng(7))
    assert gap_a == gap_b


def test_report_sha_is_stable_and_mismatch_raises(tmp_path):
    rows = [make_result(_H1_START + i, outcome="win" if i % 2 else "lose")
            for i in range(10)]
    heldout = aggregate(rows, basket="H1")
    train = aggregate(rows, split="train", basket="train_window")
    gain = judge_gain(train=train, heldout=heldout)
    diversity = collapse_check(curves=[list(heldout.action_entropy_curve)],
                               jsd_kind=[None], win_rates=[heldout.win_rate],
                               cfg=CollapseConfig())
    report = render_report_sections(generation=1, harness_version=1, parent_version=0,
                                    heldout=heldout, train=train, gain=gain,
                                    diversity=diversity, ablation=None,
                                    decision="pending", decided_by="auto", reason="")
    _path, sha = write_generation_report(tmp_path, 1, report)
    assert check_report_determinism(tmp_path, 1, report) == sha
    tampered = dict(report)
    tampered["generation"] = 2
    with pytest.raises(InfraError) as exc:
        check_report_determinism(tmp_path, 1, tampered)
    assert exc.value.code == E.E_EVAL_NONDETERMINISTIC


# ---- T-14 报告 schema round-trip ---------------------------------------------------

def test_report_schema_round_trip(tmp_path):
    rows = [make_result(_H1_START + i, outcome="win" if i % 2 else "lose")
            for i in range(40)]
    heldout = aggregate(rows, basket="H1")
    train = aggregate(rows, split="train", basket="train_window")
    diversity = collapse_check(curves=[list(heldout.action_entropy_curve)],
                               jsd_kind=[None], win_rates=[heldout.win_rate],
                               cfg=CollapseConfig())
    report = render_report_sections(generation=3, harness_version=7, parent_version=6,
                                    heldout=heldout, train=train,
                                    gain=judge_gain(train=train, heldout=heldout),
                                    diversity=diversity, ablation=None,
                                    decision="reject", decided_by="auto", reason="C2 未显著",
                                    episode_config_hash="sha256:abc",
                                    candidates=[{"id": "b", "kind": "tool_patch"}])
    validate("generation_report", report)
    assert json.loads(json.dumps(report)) == report
    path = generation_report_path(tmp_path, 3)
    assert path.name == "report.json" and path.parent.name == "gen-003"


# ---- FR-08-25：MOD-07 门面 ---------------------------------------------------------

def test_evaluate_arm_with_injected_runner():
    runner = FakeRunner({seed: ("win" if seed % 2 else "lose") for seed in _SEEDS})
    arm = evaluate_arm(Path("harness"), split="heldout", basket=_SEEDS, memory_version=3,
                       arm="parent", run_id="r-1", protocol=_PROTO, runner=runner,
                       generation=1, harness_version=1)
    assert isinstance(arm, ArmResult)
    assert arm.metrics.n == len(_SEEDS)
    assert arm.by_seed()[_H1_START].seed == _H1_START
    assert arm.seeds == _SEEDS


def test_evaluate_arm_default_basket_is_full_h1():
    arm = evaluate_arm(Path("harness"), split="heldout", basket="H1", memory_version=0,
                       arm="parent", run_id="r-1", protocol=_PROTO,
                       runner=FakeRunner(), generation=1, harness_version=1)
    assert arm.metrics.n == 40 and arm.seeds[0] == _H1_START


def test_compare_paired_mcnemar_and_bootstrap():
    parent_runner = FakeRunner({seed: "lose" for seed in _SEEDS})
    cand_runner = FakeRunner({seed: "win" for seed in _SEEDS})
    parent = evaluate_arm(Path("harness"), split="heldout", basket=_SEEDS,
                          memory_version=0, arm="parent", run_id="r-1", protocol=_PROTO,
                          runner=parent_runner, generation=1, harness_version=1)
    cand = evaluate_arm(Path("harness"), split="heldout", basket=_SEEDS,
                        memory_version=0, arm="cand-a", run_id="r-1", protocol=_PROTO,
                        runner=cand_runner, generation=1, harness_version=1)
    test = compare_paired(cand, parent)
    assert test.method == "mcnemar" and test.metric == "win_rate"
    assert test.delta == pytest.approx(1.0)
    assert test.significant is True and test.direction == "up"
    # 全胜 vs 全负：b=12, c=0 → 单尾 ~0.000244
    assert test.p < 0.01
    continuous = compare_paired(cand, parent, "mean_stages_cleared",
                                rng=np.random.default_rng(0), B=200)
    assert continuous.method == "paired_bootstrap"


def test_compare_paired_requires_shared_seeds():
    a = evaluate_arm(Path("harness"), split="heldout", basket=[_H1_START, _H1_START + 1],
                     memory_version=0, arm="a", run_id="r", protocol=_PROTO,
                     runner=FakeRunner(), generation=1)
    b = evaluate_arm(Path("harness"), split="heldout", basket=[_H1_START + 10, _H1_START + 11],
                     memory_version=0, arm="b", run_id="r", protocol=_PROTO,
                     runner=FakeRunner(), generation=1)
    with pytest.raises(InfraError) as exc:
        compare_paired(a, b)
    assert exc.value.code == E.E_EVAL_INSUFFICIENT_N


def test_evaluate_generation_sections_validate(tmp_path):
    runner = FakeRunner({seed: ("win" if seed % 2 else "lose") for seed in _SEEDS})
    parent = evaluate_arm(Path("harness"), split="heldout", basket="H1", memory_version=0,
                          arm="parent", run_id="r-1", protocol=_PROTO, runner=runner,
                          generation=1, harness_version=1)
    report = evaluate_generation(1, parent, None, run_id="r-1", run_dir=tmp_path,
                                 protocol=_PROTO)
    validate("generation_report", report)
    assert report["generation"] == 1
    assert report["heldout"]["basket"] == "H1"
    assert "power_note" in report


def test_generations_csv_columns_are_fixed(tmp_path):
    append_generations_csv(tmp_path, {"generation": 1, "win_rate": 0.5, "verdict": "null"})
    text = (tmp_path / "metrics" / "generations.csv").read_text(encoding="utf-8")
    header = text.splitlines()[0].split(",")
    from dice_ratchet.evals.harness import GENERATIONS_COLUMNS

    assert tuple(header) == GENERATIONS_COLUMNS

# =====================================================================
# F43：台账每代一行（upsert）、重算不覆盖已裁决的代
# =====================================================================

def _row(generation: int, *, decision: str, n: int, win: float) -> dict:
    from dice_ratchet.evals.harness import generations_row_from_report

    return generations_row_from_report({
        "generation": generation, "harness_version": generation, "parent_version": 0,
        "decision": decision,
        "heldout": {"basket": "H1", "n": n, "win_rate": win, "win_rate_ci95": [0.5, 0.7]},
        "diversity": {"entropy_slope": 0.0, "collapse_flag": False},
        "gain_realism": {"win_rate_gap": 0.0, "verdict": "null"},
    })


def test_f43_generations_csv_upserts_one_row_per_generation(tmp_path):
    """F43：同代重复写入只留**一行**（两个写入方：MOD-07 与 ``eval report``）。"""
    from dice_ratchet.evals.harness import append_generations_csv

    append_generations_csv(tmp_path, _row(1, decision="reject", n=40, win=0.6))
    append_generations_csv(tmp_path, _row(1, decision="pending", n=117, win=0.65))
    append_generations_csv(tmp_path, _row(2, decision="reject", n=40, win=0.7))

    import csv

    rows = list(csv.DictReader(
        (tmp_path / "metrics" / "generations.csv").open(encoding="utf-8")))
    assert [r["generation"] for r in rows] == ["1", "2"], "每代恰好一行且按代排序"
    gen1 = rows[0]
    assert gen1["decision"] == "reject", "已裁决的 decision 不得被重算的 pending 覆盖"
    assert gen1["n"] == "117", "指标列由后写入方（重算）刷新"


def test_fm33_ledger_keeps_train_and_heldout_rows(tmp_path):
    """**F-M3-3 的台账回归**：同代的 train 与 heldout 必须**共存**，不得互相覆盖。

    修复前 `append_generations_csv` 按 `generation` **单键** upsert——那时台账只有 heldout
    行，单键恰好够用。按 F-M3-3 补上 train 行后，同一代的 train/heldout 会互相吃掉，
    台账里永远只剩最后写的那个 split。
    """
    from dice_ratchet.evals.harness import (append_generations_csv,
                                            generations_rows_from_report)

    report = {
        "generation": 1, "harness_version": 1, "parent_version": 0, "decision": "reject",
        "heldout": {"basket": "H1", "n": 40, "win_rate": 0.6, "win_rate_ci95": [0.5, 0.7]},
        "train": {"basket": "train_window", "n": 30, "win_rate": 0.7,
                  "win_rate_ci95": [0.6, 0.8]},
        "diversity": {"entropy_slope": 0.0, "collapse_flag": False},
        "gain_realism": {"win_rate_gap": 0.1, "verdict": "null"},
    }
    rows = generations_rows_from_report(report)
    assert [r["split"] for r in rows] == ["heldout", "train"], "heldout 必须在前"
    # 反例自检：**顺序反过来写也不得覆盖**（单键实现下会只剩 train）
    for row in reversed(rows):
        append_generations_csv(tmp_path, row)

    import csv

    got = list(csv.DictReader(
        (tmp_path / "metrics" / "generations.csv").open(encoding="utf-8")))
    assert len(got) == 2, f"train/heldout 必须共存，实际 {[r['split'] for r in got]}"
    assert {r["split"] for r in got} == {"heldout", "train"}
    by_split = {r["split"]: r for r in got}
    assert by_split["heldout"]["n"] == "40" and by_split["train"]["n"] == "30"
    # 代级属性只挂 heldout 行（避免两个互相矛盾的坍缩判定）
    assert by_split["heldout"]["collapse_flag"] == "False"
    assert by_split["train"]["collapse_flag"] in ("", None)
    assert by_split["train"]["win_rate_gap"] in ("", None)
    # 判决只挂 heldout 行
    assert by_split["heldout"]["decision"] == "reject"
    assert by_split["train"]["decision"] in ("", None)


def test_fm33_train_row_skipped_when_train_section_empty(tmp_path):
    """`train.n == 0`（该代没有已落盘训练局）⇒ **不写 train 行**。

    写一行 n=0 会被绘图与统计当成真数据点（退化成一个"零点"）。
    """
    from dice_ratchet.evals.harness import generations_rows_from_report

    for empty in ({}, {"n": 0, "win_rate": 0.0, "low_confidence": True}):
        # 注意第二种形状：引擎在无训练局时写的**不是**空 dict，而是 ``n=0`` 的完整段
        # （实测 `gen-000` 基线的 `report.json`）。用 `if train:` 判断会把它当有数据，
        # 写出一行 `win_rate=0` 的**假零点**。
        rows = generations_rows_from_report({
            "generation": 2, "harness_version": 2, "parent_version": 1,
            "heldout": {"basket": "H1", "n": 40, "win_rate": 0.6,
                        "win_rate_ci95": [0.5, 0.7]},
            "train": empty,
            "diversity": {"entropy_slope": 0.0, "collapse_flag": False},
            "gain_realism": {"win_rate_gap": 0.1, "verdict": "null"},
        })
        assert [r["split"] for r in rows] == ["heldout"], f"train={empty} 时不得写 train 行"


def test_fm33_legacy_rows_with_empty_split_are_replaced_not_appended(tmp_path):
    """历史台账里 `split` 为空的行必须被**替换**而不是继续追加。

    实测来源：`runs/g6-eval-smoke/metrics/generations.csv` 同一代同值出现 **5 行**
    ——单键 upsert 在空 split 上把 `str(prev.get("split")) != generation` 之外的比较
    全部走空，于是每次都 `existing.append`。
    """
    import csv

    from dice_ratchet.evals.harness import append_generations_csv

    csv_path = tmp_path / "metrics" / "generations.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("generation,n,win_rate\n0,40,0.45\n0,40,0.45\n0,40,0.45\n")
    append_generations_csv(tmp_path, {"generation": 0, "split": "heldout",
                                      "n": 40, "win_rate": 0.5})
    got = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(got) == 1, f"空 split 的旧行必须被替换，实际 {len(got)} 行"
    assert got[0]["win_rate"] == "0.5"


def test_f43_recompute_report_goes_to_sidecar(tmp_path):
    """F43：``eval report`` 不得覆盖 MOD-07 已裁决的 ``report.json``。"""
    import json

    from dice_ratchet.evals.cli import _write_report_preserving_verdict
    from dice_ratchet.evals.harness import generation_report_path

    target = generation_report_path(tmp_path, 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"decision": "reject", "candidates": [{"x": 1}]}),
                      encoding="utf-8")

    path, _sha = _write_report_preserving_verdict(
        tmp_path, 1, {"decision": "pending", "generation": 1}, existing_decided="reject")

    assert path.name == "report.eval.json", "已裁决的代 → sidecar"
    assert json.loads(target.read_text(encoding="utf-8"))["decision"] == "reject"

    # 未裁决（或缺失）时行为不变：仍走正式写入路径（桩掉写入，只验分支选择）
    import dice_ratchet.evals.cli as cli
    from dice_ratchet.evals.harness import generation_report_path as grp

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cli, "write_generation_report",
                        lambda root, gen, rep, **kw: (grp(root, gen), "sha256:x"))
    try:
        path2, _ = _write_report_preserving_verdict(
            tmp_path, 2, {"decision": "pending", "generation": 2}, existing_decided=None)
    finally:
        monkeypatch.undo()
    assert path2.name == "report.json"


def test_f43_row_builder_matches_fixed_columns():
    """行构造器产出的键集必须与 ``GENERATIONS_COLUMNS`` 逐字一致（防两处行定义漂移）。"""
    from dice_ratchet.evals.harness import GENERATIONS_COLUMNS

    assert tuple(_row(1, decision="reject", n=1, win=0.0).keys()) == GENERATIONS_COLUMNS


def test_f47_error_reject_is_a_verdict_and_survives_recompute(tmp_path):
    """F47-D：``error_reject``（基础设施失败）也是 MOD-07 的真实判决。

    它是「阈值内但落版本失败」的唯一可见信号，因此 ``eval report`` 的重算（``pending``）
    不得把它盖掉——否则 §3.2 的「机制性失败为零」判据就没有依据可查。
    """
    import csv

    from dice_ratchet.evals.harness import VERDICT_DECISIONS, append_generations_csv

    assert "error_reject" in VERDICT_DECISIONS
    append_generations_csv(tmp_path, _row(1, decision="error_reject", n=0, win=0.0))
    append_generations_csv(tmp_path, _row(1, decision="pending", n=117, win=0.65))

    rows = list(csv.DictReader(
        (tmp_path / "metrics" / "generations.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["decision"] == "error_reject", "重算不得覆盖基础设施失败判决"
    assert rows[0]["n"] == "117", "指标列仍由后写入方刷新"


# =====================================================================
# §3.4：`c3` 的三处缺陷（kind 分布没落盘 / 重算路径硬编码 null / pair 错位）
# =====================================================================

def test_c34_kind_distribution_round_trip(tmp_path):
    """kind 分布必须能落盘并读回（`c3` 要 g−1 的分布，而 `eval report` 手上没有）。"""
    from dice_ratchet.evals.harness import (kind_distribution_path, read_kind_distribution,
                                            write_kind_distribution)

    dist = {"map_choose": 0.3, "play_face": 0.6, "end_turn": 0.1}
    path = write_kind_distribution(tmp_path, 1, dist)
    assert path == kind_distribution_path(tmp_path, 1)
    assert read_kind_distribution(tmp_path, 1) == dist
    # 缺失 → None（**不是**空 dict、更不是 0.0）
    assert read_kind_distribution(tmp_path, 7) is None


def test_c34_aligned_jsd_kinds_length_matches_generations(tmp_path):
    """`jsd_kind` 必须与 `curves` **等长**（`collapse_check` 按代索引取 `jsds[i]`）。"""
    from dice_ratchet.evals.harness import aligned_jsd_kinds, write_kind_distribution

    for gen in (0, 1, 2):
        write_kind_distribution(tmp_path, gen, {"map_choose": 0.5, "play_face": 0.5})
    jsds = aligned_jsd_kinds(tmp_path, [0, 1, 2])
    assert len(jsds) == 3, "长度必须等于曲线数"
    assert jsds[0] is None, "首位没有前驱 ⇒ None"
    assert jsds[1] == 0.0 and jsds[2] == 0.0, "同分布 ⇒ jsd 0.0"


def test_c34_missing_distribution_gives_none_not_zero(tmp_path):
    """**反例自检**：缺分布 ⇒ `None`。写成 0.0 会被 `c3` 读成「分布完全没变」⇒ 误报冻结。"""
    from dice_ratchet.evals.harness import aligned_jsd_kinds, write_kind_distribution

    write_kind_distribution(tmp_path, 1, {"map_choose": 1.0})   # 只有 g=1，没有 g=0
    jsds = aligned_jsd_kinds(tmp_path, [0, 1])
    assert jsds[1] is None, "缺前驱分布必须是 None"
