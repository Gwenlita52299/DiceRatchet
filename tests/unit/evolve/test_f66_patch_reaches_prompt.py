"""F66 回归：候选补丁必须真的进入决策 prompt（2026-10-14，正式实验主体发现）。

**现场**：`evaluate_arm` 的 `executor = runner or _default_runner(harness_dir=…)`
在**注入 runner 时完全忽略 `harness_dir`**，而 MOD-07 给每次候选评估都注入
`EvalRunnerAdapter(loop 的 EpisodeRunner)`（其策略工厂绑在 run 级 `harness/`）⇒
三次候选评估的 `prompt_hash` 与 parent 逐字节相同（40/40），判据 2 无数据。

本文件的用例在**修复前**全部失败（详见每条 docstring 的失败签名）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dice_ratchet.agent.prompts.assembler import resolve_path, sha256_of
from dice_ratchet.infra.errors import E, InfraError

REPO_ROOT = Path(__file__).resolve().parents[3]


def _staged_harness(root: Path, *, marker: str | None = None) -> Path:
    """造一个 `harness 根` 级布局的 staging 目录：`<root>/prompts/decision.md`。"""
    staged = root / "gen-001" / "staging" / "cand-abc123"
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    src = (REPO_ROOT / "harness" / "prompts" / "decision.md").read_text(encoding="utf-8")
    if marker:
        src += f"\n10. {marker}\n"
    (staged / "prompts" / "decision.md").write_text(src, encoding="utf-8")
    return staged


def test_f66_resolve_path_prefers_passed_harness_root(tmp_path):
    """给定 staging 目录（harness 根布局），当 ``resolve_path(…, harness_dir=staged)``，
    则返回 **staging 里那份**。

    修复前失败签名：返回 `<repo>/harness/prompts/decision.md`（CWD 回落）⇒
    ``assert staged/prompts/decision.md == <repo>/harness/…`` 失败。
    """
    staged = _staged_harness(tmp_path, marker="STAGED-MARKER")
    got = resolve_path("harness/prompts/decision.md", harness_dir=staged)
    assert Path(got) == staged / "prompts" / "decision.md"


def test_f66_resolve_path_keeps_legacy_repo_relative_file(tmp_path):
    """反向护墙：**不在 harness/ 下**的随包冻结件（``fixed_prompt_path``）仍能解析。

    给定 ``harness_dir=<repo>/harness``，当解析 ``dice_ratchet/agent/prompts/fixed_v1.md``，
    则落到仓库内那份（走 ``harness_dir.parent`` 这条旧语义，不能被本次修复破坏）。
    """
    got = resolve_path("dice_ratchet/agent/prompts/fixed_v1.md",
                       harness_dir=REPO_ROOT / "harness")
    assert Path(got) == REPO_ROOT / "dice_ratchet" / "agent" / "prompts" / "fixed_v1.md"


def test_f66_staged_template_is_byte_different(tmp_path):
    """staging 模板与 run 级模板**不得**逐字节相同（补丁确实落在文件里）。"""
    staged = _staged_harness(tmp_path, marker="STAGED-MARKER")
    staged_text = (staged / "prompts" / "decision.md").read_text(encoding="utf-8")
    run_text = (REPO_ROOT / "harness" / "prompts" / "decision.md").read_text(encoding="utf-8")
    assert sha256_of(staged_text) != sha256_of(run_text)
    assert "STAGED-MARKER" in staged_text


def test_f66_candidate_evaluator_rejects_template_outside_arm_dir(tmp_path):
    """护栏：候选臂的 play 模板落点不在本臂目录内 ⇒ fail-closed
    ``E_EVOLVE_PATCH_INVALID``（**修复前必失败**：那时落点就是 CWD）。

    给定一个**空** staging 目录（布局不含 prompts/），当调
    ``CandidateEvaluator._assert_patch_reaches_prompt``，则抛错。
    """
    from dice_ratchet.evolve.judge import CandidateEvaluator
    from dice_ratchet.infra.config import AppConfig

    empty = tmp_path / "staging" / "cand-empty"
    empty.mkdir(parents=True, exist_ok=True)
    evaluator = CandidateEvaluator(config=AppConfig())
    with pytest.raises(InfraError) as exc:
        evaluator._assert_patch_reaches_prompt(arm="cand-empty", arm_dir=empty)
    assert str(exc.value.code) == str(E.E_EVOLVE_PATCH_INVALID)


def test_f66_candidate_evaluator_accepts_staged_template(tmp_path):
    """正例：staging 布局正确（harness 根 + prompts/decision.md）⇒ 护栏通过。"""
    from dice_ratchet.evolve.judge import CandidateEvaluator
    from dice_ratchet.infra.config import AppConfig

    staged = _staged_harness(tmp_path, marker="STAGED-MARKER")
    evaluator = CandidateEvaluator(config=AppConfig())
    evaluator._assert_patch_reaches_prompt(arm="cand-abc123", arm_dir=staged)  # 不抛


def test_f66_evaluate_arm_binds_arm_dir_to_runner(tmp_path, monkeypatch):
    """接线护栏：``CandidateEvaluator._evaluate_arm`` 必须把**本臂目录**绑到 runner 上。

    给定一个 staging 目录，当调 ``_evaluate_arm``，则传给 ``evaluate_arm`` 的 runner
    是携带 ``harness_dir=<本臂目录>`` 的适配器。

    修复前失败签名：``_wrap_runner() got an unexpected keyword argument 'harness_dir'``
    / 适配器没有 ``harness_dir`` 属性。
    """
    from dice_ratchet import evals as evals_pkg
    from dice_ratchet.evolve.judge import CandidateEvaluator
    from dice_ratchet.infra.config import AppConfig

    captured: dict = {}

    def fake_evaluate_arm(harness_dir, **kwargs):
        captured["harness_dir"] = harness_dir
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(evals_pkg, "evaluate_arm", fake_evaluate_arm)
    staged = _staged_harness(tmp_path, marker="STAGED-MARKER")
    evaluator = CandidateEvaluator(config=AppConfig(), runner=object(), run_id="r-f66",
                                   generation=1, smoke_seeds=(1000,))
    evaluator._evaluate_arm(staged, split="heldout", basket="H1", arm="cand-abc123",
                            harness_version=0)
    assert Path(captured["harness_dir"]) == staged
    bound = captured["kwargs"]["runner"]
    assert Path(bound.harness_dir) == staged, "runner 未绑定本臂目录 ⇒ 候选会用 run 级模板"
