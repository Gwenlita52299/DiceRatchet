"""FR-08-21 / §8.4：消融归因与 Holm–Bonferroni（单测；重活注入 fake）。"""

from __future__ import annotations

import json

import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.evals.ablation import (ARM_MECHANISM, attribution, combine_attribution,
                                      run_ablation)
from dice_ratchet.evals.harness import ArmResult
from dice_ratchet.evals.metrics import EpisodeResult, aggregate, holm_bonferroni


def _arm(name: str, wins: int, n: int = 40) -> ArmResult:
    results = []
    for i in range(n):
        counts = [0] * 16
        counts[0] = 3
        results.append(EpisodeResult(
            seed=2040 + i, split="heldout", generation=3, harness_version=3,
            outcome="win" if i < wins else "lose", truncation_reason=None,
            stages_cleared=5, hearts_left=1, battles_won=10, decisions=90, steps=200,
            illegal_attempts=0, fallback_decisions=0, kind_counts=tuple(counts),
            slot_counts={}, seq_hash=f"s{i}", decision_kinds=(0, 1)))
    return ArmResult(arm=name, split="heldout", basket="H2", harness_version=3,
                     memory_version=0, run_id="r", results=tuple(results),
                     metrics=aggregate(results, basket="H2"))


# ---- attribution / combine ---------------------------------------------------------

def test_attribution_returns_mechanism_name():
    full = [True] * 20 + [False] * 20
    worse = [True] * 6 + [False] * 34 # 14 对不一致 → 显著
    assert attribution(full, worse, arm_name="no_reflection") == "reflection"
    assert attribution(full, full, arm_name="no_reflection") == "none"
    assert attribution(full, worse, arm_name="no_self_edit") == "self_edit"


def test_attribution_without_arm_name_uses_significant_label():
    full = [True] * 20 + [False] * 20
    worse = [True] * 6 + [False] * 34
    assert attribution(full, worse) == "significant"
    assert attribution(full, full) == "none"


def test_attribution_length_mismatch_raises():
    with pytest.raises(InfraError) as exc:
        attribution([True], [True, False])
    assert exc.value.code == E.E_EVAL_SCHEMA


def test_combine_attribution_all_labels():
    assert combine_attribution(True, True) == "both"
    assert combine_attribution(True, False) == "reflection"
    assert combine_attribution(False, True) == "self_edit"
    assert combine_attribution(False, False) == "none"


def test_holm_bonferroni_thresholds_and_step_down():
    result = holm_bonferroni([0.001, 0.06], alpha=0.05)
    assert result[0] == (0, 0.025, True) # 最小 p 与 alpha/2 比
    assert result[1][2] is False
    # step-down：排在前面的 p=0.03 不显著后，p=0.04 即使 <= alpha 也不再判显著
    blocked = holm_bonferroni([0.001, 0.03, 0.04], alpha=0.05)
    assert blocked[0][2] is True
    assert blocked[1][2] is False
    assert blocked[2][2] is False


# ---- run_ablation ------------------------------------------------------------------

def test_run_ablation_default_evaluator_is_used(tmp_path, monkeypatch):
    import dice_ratchet.evals.ablation as mod

    calls: list[str] = []

    def fake_evaluate_arm(harness_dir, **kw):
        calls.append(kw["arm"])
        wins = {"ablation-full": 28, "ablation-no_reflection": 12,
                "ablation-no_self_edit": 26}[kw["arm"]]
        return _arm(kw["arm"], wins)

    monkeypatch.setattr(mod, "evaluate_arm", fake_evaluate_arm)
    report = run_ablation(3, arms=["full", "no_reflection", "no_self_edit"], run_id="r",
                          split_basket="H2", run_dir=tmp_path, write=True)
    assert calls == ["ablation-full", "ablation-no_reflection", "ablation-no_self_edit"]
    payload = json.loads((tmp_path / "ablation" / "gen-003.json").read_text(encoding="utf-8"))
    assert payload["attribution"] == "reflection"
    assert payload["delta_full"]["no_reflection"] == pytest.approx(report.delta_full["no_reflection"])
    assert set(report.holm) == {"no_reflection", "no_self_edit"}


def test_run_ablation_requires_full_arm():
    with pytest.raises(InfraError) as exc:
        run_ablation(3, arms=["no_reflection"], run_id="r", write=False)
    assert exc.value.code == E.E_EVAL_ARM_NOT_ALLOWED


def test_arm_mechanism_mapping():
    assert ARM_MECHANISM["no_reflection"] == "reflection"
    assert ARM_MECHANISM["no_self_edit"] == "self_edit"
    assert ARM_MECHANISM["full"] == "none"
