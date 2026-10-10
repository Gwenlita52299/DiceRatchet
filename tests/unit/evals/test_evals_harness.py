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
_SEEDS = tuple(range(2000, 2012))


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
    runner = FakeRunner(outcomes, fail_seeds=[2000, 2001])
    result = run_eval(_spec(), runner=runner, protocol=_PROTO)
    assert result.metrics.n == len(_SEEDS)
    assert result.metrics.n_dropped == 2
    assert result.metrics.win_rate == pytest.approx(10 / 12)
    assert str(E.E_EVAL_INSUFFICIENT_N) in result.error_codes
    with pytest.raises(InfraError) as exc:
        result.raise_if_low_confidence()
    assert exc.value.code == E.E_EVAL_INSUFFICIENT_N


def test_degraded_excluded_from_n():
    runner = FakeRunner(default="win", degraded_seeds=[2000, 2001])
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
        run_eval(_spec(seeds=(1000, 2000)), runner=FakeRunner(), protocol=_PROTO)
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
    rows = [make_result(2000 + i, outcome="win" if i % 2 else "lose") for i in range(10)]
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
    rows = [make_result(2000 + i, outcome="win" if i % 2 else "lose") for i in range(40)]
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
    assert arm.by_seed()[2000].seed == 2000
    assert arm.seeds == _SEEDS


def test_evaluate_arm_default_basket_is_full_h1():
    arm = evaluate_arm(Path("harness"), split="heldout", basket="H1", memory_version=0,
                       arm="parent", run_id="r-1", protocol=_PROTO,
                       runner=FakeRunner(), generation=1, harness_version=1)
    assert arm.metrics.n == 40 and arm.seeds[0] == 2000


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
    a = evaluate_arm(Path("harness"), split="heldout", basket=[2000, 2001],
                     memory_version=0, arm="a", run_id="r", protocol=_PROTO,
                     runner=FakeRunner(), generation=1)
    b = evaluate_arm(Path("harness"), split="heldout", basket=[2010, 2011],
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
