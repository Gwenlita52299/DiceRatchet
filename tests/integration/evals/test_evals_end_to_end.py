"""MOD-08 集成测试：T-13 golden、T-16 蜜罐+消融、T-17 可复现命令（G2/G4/G6）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from gamer_rsi.infra import events
from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.evals import seeds as S
from gamer_rsi.evals.ablation import run_ablation
from gamer_rsi.evals.harness import ArmResult, load_episode_results
from gamer_rsi.evals.metrics import EpisodeResult, aggregate

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "evals"
_GOLDEN = _REPO / "tests" / "golden" / "evals" / "split_metrics.json"


# ---- T-13 Golden 指标快照（G2）------------------------------------------------------

def test_golden_split_metrics_snapshot():
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    results = load_episode_results(_FIXTURES, "heldout", harness_version=0)
    metrics = aggregate(results, split="heldout", basket="H1", t_max=120, min_support=3)
    assert len(results) == golden["n_fixture_episodes"]
    assert metrics.as_dict() == golden["metrics"], (
        "指标口径变化：审阅 diff 后运行 tests/golden/evals/regen_metrics_golden.py（G2）")


# ---- T-16 蜜罐注入必被抓到 + 消融端到端（G4）-----------------------------------------

def test_guard_violation_is_recorded_as_event(tmp_path):
    events.set_dev_mode(True)
    sink_path = tmp_path / "events.jsonl"
    events.bind(sink_path, "r-leak")
    try:
        with pytest.raises(InfraError) as exc:
            S.guard_prompt_context(split="heldout", seed=2043,
                                   text="honeypot: seed 2043 leaked",
                                   destination="prompt")
        assert exc.value.code == E.E_EVAL_INV5_LEAK
    finally:
        events.unbind()
        events.set_dev_mode(None)
    rows = [json.loads(line) for line in sink_path.read_text(encoding="utf-8").splitlines() if line]
    hits = [r for r in rows if r.get("event") == "eval.guard_violation"]
    assert len(hits) == 1
    assert hits[0]["error_code"] == "E_EVAL_INV5_LEAK"
    assert hits[0]["matched_token"] == "2043"


def _result(seed: int, outcome: str, *, harness_version: int = 3) -> EpisodeResult:
    counts = [0] * 16
    counts[0] = 4
    return EpisodeResult(
        seed=seed, split="heldout", generation=3, harness_version=harness_version,
        outcome=outcome, truncation_reason=None, stages_cleared=5, hearts_left=1,
        battles_won=10, decisions=90, steps=210, illegal_attempts=0,
        fallback_decisions=0, kind_counts=tuple(counts),
        slot_counts={"attack×blade": 4}, seq_hash=f"seq-{seed}",
        decision_kinds=(0, 1, 2, 3))


def _arm(name: str, *, wins: int, n: int = 40, harness_version: int = 3) -> ArmResult:
    results = tuple(_result(2040 + i, "win" if i < wins else "lose", harness_version=harness_version)
                    for i in range(n))
    return ArmResult(arm=name, split="heldout", basket="H2", harness_version=harness_version,
                     memory_version=0, run_id="r-ablation", results=results,
                     metrics=aggregate(results, basket="H2"))


def test_ablation_end_to_end_writes_report(tmp_path):
    # full 明显更好：两臂各少赢 12 局（12 对不一致 → 单尾 p≈0.00024，Holm 阈值 0.025 下显著）
    def evaluator(arm, generation, basket):
        if arm == "full":
            return _arm("ablation-full", wins=28)
        if arm == "no_reflection":
            return _arm("ablation-no_reflection", wins=16)
        return _arm("ablation-no_self_edit", wins=16)

    report = run_ablation(3, arms=["full", "no_reflection", "no_self_edit"],
                          run_id="r-ablation", split_basket="H2", evaluator=evaluator,
                          run_dir=tmp_path, write=True)
    payload = json.loads((tmp_path / "ablation" / "gen-003.json").read_text(encoding="utf-8"))
    assert payload["generation"] == 3
    assert set(payload["delta_full"]) == {"no_reflection", "no_self_edit"}
    assert set(payload["mcnemar_p"]) == {"no_reflection", "no_self_edit"}
    assert payload["attribution"] == "both"
    assert report.attribution == "both"


def test_ablation_single_mechanism_attribution(tmp_path):
    def evaluator(arm, generation, basket):
        if arm == "full":
            return _arm("ablation-full", wins=28)
        if arm == "no_reflection":
            return _arm("ablation-no_reflection", wins=14)
        return _arm("ablation-no_self_edit", wins=27) # 无实质影响

    report = run_ablation(3, arms=["full", "no_reflection", "no_self_edit"],
                          run_id="r-ablation", split_basket="H2", evaluator=evaluator,
                          run_dir=tmp_path, write=False)
    assert report.attribution == "reflection"


def test_ablation_unknown_arm_rejected(tmp_path):
    with pytest.raises(InfraError) as exc:
        run_ablation(3, arms=["full", "no_bogus"], run_id="r", write=False)
    assert exc.value.code == E.E_EVAL_ARM_NOT_ALLOWED


# ---- T-17 可复现命令（G6）----------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["baseline", "--kind", "random", "--split", "heldout", "--basket", "H1"],
    ["eval", "report", "--split", "heldout", "--baseline", "all"],
    ["eval", "diversity"],
    ["eval", "ablation", "--arms", "no_reflection,no_self_edit"],
    ["eval", "test", "--unlock-once"],
])
def test_g6_commands_parse_and_dry_run(argv):
    proc = subprocess.run([sys.executable, "-m", "gamer_rsi", *argv, "--dry-run"],
                          cwd=_REPO, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["dry_run"] is True


@pytest.mark.slow
def test_g6_baseline_and_report_actually_run(tmp_path):
    # 独占 run_id：旧版写死 "g6-eval-smoke"，一旦仓库里留下同名 run 目录（且 config_hash
    # 因默认值回填而变化）就会以 E_IO_RUN_CONFLICT 失败（2026-10-04 实测回填后触发）。
    # 用例改为自建唯一 run 并在结束时清理，保证可重复执行。
    run_id = f"g6-eval-smoke-{uuid4().hex[:8]}"
    run_dir = _REPO / "runs" / run_id
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "gamer_rsi", "baseline", "--kind", "heuristic",
             "--split", "heldout", "--basket", "H1", "--run-id", run_id],
            cwd=_REPO, capture_output=True, text=True, timeout=900)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (run_dir / "baselines" / "heuristic-heldout.json").is_file()
        proc2 = subprocess.run(
            [sys.executable, "-m", "gamer_rsi", "eval", "report", "--split", "heldout",
             "--baseline", "all", "--run-id", run_id],
            cwd=_REPO, capture_output=True, text=True, timeout=900)
        assert proc2.returncode == 0, proc2.stdout + proc2.stderr
        assert (run_dir / "generations" / "gen-000" / "report.sha256").is_file()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
