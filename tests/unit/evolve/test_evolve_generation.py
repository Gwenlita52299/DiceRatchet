"""§8.1 / FR-07-12 / FR-07-14 / FR-07-15：单代编排与报告落盘（PRD §11 集成 ① 的单测形态）。

覆盖：空候选、全淘汰、扫描失败、冒烟淘汰、winner → accept、HITL 挂起、
MOD-08 异常 → 保守 reject（NFR-07-7），以及「每次接受/拒绝都有 report.json」。

MOD-08 与 MOD-02 都打桩：
- ``evaluate_candidate`` 的 MOD-08 侧用 ``ScriptedEvaluator``（不跑局、不调模型）；
- ``guard.gate`` / ``load_harness_tools`` 用 stub（真实扫描在
  ``test_malicious_candidates.py`` 里跑，那里是本模块的安全用例主场）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dice_ratchet.evolve import generation as GEN
from dice_ratchet.evolve.paths import GenPaths
from dice_ratchet.evolve.schemas import Candidate
from dice_ratchet.infra.errors import E

from evolve_support import (FakeArm, FakeRunContext, FakeState, StubLLM,
                            make_app_config, make_candidate, make_config, prompt_diff,
                            proposal_payload, write_harness)

RUN_ID = "1111111-abcdef12-20260930T120000Z"


# =====================================================================
# 夹具
# =====================================================================

class ScriptedEvaluator:
    """``judge.CandidateEvaluator`` 的替身（不跑局、不调模型）。"""

    def __init__(self, *, parent_heldout=None, parent_smoke=None,
                 smoke=None, heldout=None, paired=None, error=None) -> None:
        self.parent_heldout = parent_heldout or FakeArm(arm="parent", win_rate=0.30)
        self.parent_smoke = parent_smoke or FakeArm(arm="parent-smoke", win_rate=0.40, n=2)
        self.smoke = smoke or FakeArm(arm="smoke", win_rate=0.40, n=2)
        self.heldout = heldout or FakeArm(arm="cand", win_rate=0.35)
        self.paired = paired
        self.error = error
        self.calls: list[str] = []

    def parent_arms(self, parent_dir, *, parent_version):
        self.calls.append("parent")
        if self.error is not None:
            raise self.error
        return self.parent_heldout, self.parent_smoke

    def candidate_arms(self, staging_dir, *, candidate, harness_version):
        self.calls.append(f"cand-{candidate.candidate_id}")
        if self.error is not None:
            raise self.error
        return self.smoke, self.heldout, {}


@pytest.fixture(autouse=True)
def _stub_mod02(monkeypatch):
    """MOD-02 打桩：静态扫描通过、受限加载返回哑对象。"""

    class _Worker:
        def close(self) -> None:
            pass

    monkeypatch.setattr(GEN, "guard_gate", lambda staging, run_dir=None: _OkReport())
    monkeypatch.setattr(GEN, "load_harness_tools_checked", lambda staging: _Worker())


@pytest.fixture
def stub_pairing(monkeypatch):
    """C2 的配对检验打桩（真实 ``compare_paired`` 要求真 ``ArmResult``）。"""
    import importlib

    judge_mod = importlib.import_module("dice_ratchet.evolve.judge")

    def _install(**values):
        result = _paired(**values)
        monkeypatch.setattr(judge_mod, "_compare_paired",
                            lambda candidate_arm, parent_arm: result)
        return result

    return _install


class _OkReport:
    ok = True
    findings: tuple = ()
    scanned_files: tuple = ()


def _setup(tmp_path, *, hitl: bool = False, n_candidates: int = 1, **evolve_overrides):
    harness = write_harness(tmp_path)
    evolve = make_config(hitl=hitl, n_candidates=n_candidates, **evolve_overrides)
    app_cfg = replace(make_app_config(), evolve=evolve)
    run_root = tmp_path / "runs" / RUN_ID
    run_ctx = FakeRunContext(run_root, run_id=RUN_ID, app_config=app_cfg,
                             harness=harness, memory=None, memory_version=1)
    state = FakeState(generation=0, run_id=RUN_ID)
    return harness, evolve, app_cfg, run_root, run_ctx, state


def _run(run_ctx, state, *, evaluator=None, client=None, harness=None, run_root=None,
         seed=7):
    return GEN.run_one_generation(
        state=state, run_ctx=run_ctx, runner=None, config=run_ctx.app_config,
        evaluator=evaluator, client=client,
        harness=harness or run_ctx.harness_dir, run_dir=run_root or run_ctx.paths.root,
        seed=seed)


def _one_candidate_llm(diff: str | None = None) -> StubLLM:
    return StubLLM([{"candidates": [proposal_payload(diff=diff)]}])


def _read_report(run_root: Path, generation: int) -> dict:
    path = GenPaths.of(run_root, generation).report
    assert path.is_file(), f"缺少 report.json：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


# =====================================================================
# 正常路径：winner → accept
# =====================================================================

def test_accept_path_writes_report_and_version(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                           paired=_paired(delta=0.10))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())

    assert outcome.decision == "accept"
    assert outcome.generation == 1
    assert outcome.pending_human is False
    assert int(outcome.harness_version) == 1
    assert (harness / "versions" / "v1" / "diff.patch").is_file()

    report = _read_report(run_root, 1)
    assert report["decision"] == "accept"
    assert report["decided_by"] == "auto"
    assert report["generation"] == 1
    assert report["harness_version"] == 1
    assert report["parent_version"] == 0
    assert len(report["candidates"]) == 1
    # 候选明细落盘（FR-07-12）
    assert len(GenPaths.of(run_root, 1).candidate_ids()) == 1


def test_accept_records_condition_trace_in_candidate_file(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    cid = GenPaths.of(run_root, 1).candidate_ids()[0]
    payload = json.loads(GenPaths.of(run_root, 1).candidate(cid).read_text(
        encoding="utf-8"))
    assert payload["decision"] == "accept"
    assert payload["verdict"]["condition_trace"]
    assert [e["condition"] for e in payload["verdict"]["condition_trace"]] == [
        "C0", "C1", "C2", "C3", "C4", "C5"]
    assert payload["report"]["smoke"]["n"] == 2


# =====================================================================
# 空候选 / 全淘汰：也必须有 report.json（FR-07-15）
# =====================================================================

def test_no_candidate_still_writes_report(tmp_path):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    outcome = _run(run_ctx, state, evaluator=ScriptedEvaluator(),
                   client=StubLLM([{"candidates": []}]))
    assert outcome.decision == "reject"
    assert outcome.error_code == str(E.E_EVOLVE_NO_CANDIDATE)
    report = _read_report(run_root, 1)
    assert report["decision"] == "reject"
    assert report["candidates"] == []
    assert report["reason"] == "no_candidate"


def test_propose_failure_still_writes_report(tmp_path):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    outcome = _run(run_ctx, state, evaluator=ScriptedEvaluator(),
                   client=StubLLM([RuntimeError("llm down"), RuntimeError("llm down"),
                                   RuntimeError("llm down")]))
    assert outcome.error_code == str(E.E_EVOLVE_PROPOSE_FAILED)
    report = _read_report(run_root, 1)
    assert report["decision"] == "reject"


def test_rejected_candidate_is_recorded(tmp_path, stub_pairing):
    """C2 不显著 → reject，候选明细仍落盘。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(significant=False)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.35),
                           paired=_paired(significant=False))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "reject"
    ids = GenPaths.of(run_root, 1).candidate_ids()
    assert len(ids) == 1
    payload = json.loads(GenPaths.of(run_root, 1).candidate(*ids).read_text(
        encoding="utf-8"))
    assert payload["decision"] == "reject"
    assert payload["error_code"] == str(E.E_EVOLVE_NO_GAIN)
    assert payload["verdict"]["reason"].startswith("C2:")


def test_invalid_candidate_is_rejected_without_touching_harness(tmp_path, stub_pairing):
    """``propose`` 阶段就丢弃非法候选（路径不在白名单）→ 显式 ``NO_CANDIDATE``。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    before = (harness / "version.json").read_text(encoding="utf-8")
    bad = proposal_payload(diff=prompt_diff(path="harness/loop.py"))
    outcome = _run(run_ctx, state, evaluator=ScriptedEvaluator(),
                   client=StubLLM([{"candidates": [bad]}]))
    assert outcome.decision == "reject"
    assert outcome.error_code == str(E.E_EVOLVE_NO_CANDIDATE)
    assert (harness / "version.json").read_text(encoding="utf-8") == before
    assert GenPaths.of(run_root, 1).candidate_ids() == ()
    report = _read_report(run_root, 1)
    assert report["decision"] == "reject"


def test_validate_stage_rejection_is_recorded_per_candidate(tmp_path, stub_pairing):
    """候选进入评估链后才被 ``validate_candidate`` 拒绝 → 逐候选落盘（FR-07-12）。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    bad = make_candidate(kind="prompt_patch", diff=prompt_diff(),
                         touched=["harness/prompts/reflection.md"]) # 声明与 diff 不符
    report = GEN._evaluate_one(
        cand=bad, cfg=evolve, ev=ScriptedEvaluator(),
        parent=GEN.versioning_mod.load_version(0, harness=harness),
        parent_heldout=None, parent_smoke=None, smoke_seeds=(1,), run_id=RUN_ID, gen=1,
        ctx=GEN.EvolveContext.of(app_cfg, run_id=RUN_ID, runs_root=run_root.parent,
                                 harness=harness))
    assert report.verdict is None
    assert report.error_code == str(E.E_EVOLVE_PATCH_INVALID)
    assert "declared_files_mismatch" in report.reason


def test_scan_failure_is_rejected_before_smoke(tmp_path, monkeypatch, stub_pairing):
    """FR-07-13：扫描失败 → 直接 reject，0 局 heldout 消耗。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    ev = ScriptedEvaluator()

    class _BadReport:
        ok = False
        findings = ({"code": "E_ENV_AST_DENIED", "file": "tools/x.py", "lineno": 3,
                     "col": 0, "detail": "禁用调用 subprocess"},)
        scanned_files = ()

    monkeypatch.setattr(GEN, "guard_gate", lambda staging, run_dir=None: _BadReport())
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "reject"
    # parent 的 heldout 会被预评估（FR-07-7），但候选臂 0 消耗 heldout 预算
    assert ev.calls == ["parent"]
    payload = json.loads(GenPaths.of(run_root, 1).candidate(
        *GenPaths.of(run_root, 1).candidate_ids()).read_text(encoding="utf-8"))
    assert payload["error_code"] == str(E.E_EVOLVE_SCAN_FAILED)
    assert "subprocess" in payload["reason"] or "detail" in payload["reason"]


def test_smoke_failure_skips_heldout(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    ev = ScriptedEvaluator(smoke=FakeArm(win_rate=0.0, n=2))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "reject"
    payload = json.loads(GenPaths.of(run_root, 1).candidate(
        *GenPaths.of(run_root, 1).candidate_ids()).read_text(encoding="utf-8"))
    assert payload["error_code"] == str(E.E_EVOLVE_SMOKE_FAILED)
    assert payload["report"]["heldout"] is None


def test_load_failure_is_rejected(tmp_path, monkeypatch, stub_pairing):
    from dice_ratchet.evolve.errors import LoadFailedError

    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()

    def _boom(staging):
        raise LoadFailedError("worker 启动失败")

    monkeypatch.setattr(GEN, "load_harness_tools_checked", _boom)
    outcome = _run(run_ctx, state, evaluator=ScriptedEvaluator(),
                   client=_one_candidate_llm())
    assert outcome.decision == "reject"
    payload = json.loads(GenPaths.of(run_root, 1).candidate(
        *GenPaths.of(run_root, 1).candidate_ids()).read_text(encoding="utf-8"))
    assert payload["error_code"] == str(E.E_EVOLVE_LOAD_FAILED)


# =====================================================================
# NFR-07-7：MOD-08 异常 → 保守 reject，harness 不变
# =====================================================================

def test_mod08_failure_conservatively_rejects(tmp_path, stub_pairing):
    from dice_ratchet.infra.errors import E as _E, InfraError

    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    before = (harness / "version.json").read_text(encoding="utf-8")
    ev = ScriptedEvaluator(error=InfraError(_E.E_EVAL_TIMEOUT, "MOD-08 超时"))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "reject"
    payload = json.loads(GenPaths.of(run_root, 1).candidate(
        *GenPaths.of(run_root, 1).candidate_ids()).read_text(encoding="utf-8"))
    # 保留 MOD-08 的原始错误码（PRD §10：E_EVOLVE_EVAL_FAILED 用于"MOD-08 超时/异常"，
    # 但保留上游码对排查更有信息量，报告 reason 里两码俱在）
    assert payload["error_code"] == str(_E.E_EVAL_TIMEOUT)
    assert (harness / "version.json").read_text(encoding="utf-8") == before
    assert json.loads((harness / "version.json").read_text(
        encoding="utf-8"))["version"] == 0


def test_parent_arm_failure_does_not_crash_generation(tmp_path, stub_pairing):
    from dice_ratchet.infra.errors import E as _E, InfraError

    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    ev = ScriptedEvaluator(error=InfraError(_E.E_EVAL_TIMEOUT, "parent 评估失败"))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "reject"
    assert _read_report(run_root, 1)["decision"] == "reject"


# =====================================================================
# HITL：挂起退出（exit 0 可续跑）
# =====================================================================

def test_hitl_pending_parks_generation(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path, hitl=True)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.pending_human is True
    assert outcome.decision == "pending"
    gate = GenPaths.of(run_root, 1).gate
    assert gate.is_file()
    assert json.loads(gate.read_text(encoding="utf-8"))["status"] == "pending"
    report = _read_report(run_root, 1)
    assert report["decision"] == "pending"
    # 尚未 accept
    assert not (harness / "versions" / "v1").exists()
    assert int(outcome.harness_version) == 0


def test_hitl_approve_then_resume_accepts(tmp_path, stub_pairing):
    from dice_ratchet.evolve import gate as G

    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path, hitl=True)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    first = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert first.pending_human is True

    G.resolve_gate(generation=1, run_id=RUN_ID, action="approve", reason="人工确认",
                   run_dir=run_root)
    second = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert second.decision == "accept"
    assert second.pending_human is False
    assert (harness / "versions" / "v1").is_dir()
    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 1 and pointer["accepted_by"] == "human"
    assert _read_report(run_root, 1)["decision"] == "accept"


def test_hitl_rollback_then_resume_rolls_back(tmp_path, stub_pairing):
    from dice_ratchet.evolve import gate as G

    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path, hitl=True)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    G.resolve_gate(generation=1, run_id=RUN_ID, action="rollback", reason="人工拒绝",
                   run_dir=run_root)
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "rollback"
    report = _read_report(run_root, 1)
    assert report["decision"] == "rollback"
    assert report["decided_by"] == "human"


def test_hitl_disabled_never_writes_gate(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path, hitl=False)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert not GenPaths.of(run_root, 1).gate.exists()
    assert _read_report(run_root, 1)["decided_by"] == "auto"


# =====================================================================
# 多候选：winner = (delta 最大, candidate_id 最小)，其余 superseded
# =====================================================================

def test_multi_candidate_picks_best_and_marks_superseded(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path, n_candidates=2)
    stub_pairing(delta=0.10)
    llm = StubLLM([{"candidates": [
        proposal_payload(diff=prompt_diff(added="候选 A"), rationale="依据 traj:train#0"),
        proposal_payload(diff=prompt_diff(added="候选 B"), rationale="依据 traj:train#1"),
    ]}])
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    outcome = _run(run_ctx, state, evaluator=ev, client=llm)
    assert outcome.decision == "accept"
    ids = GenPaths.of(run_root, 1).candidate_ids()
    assert len(ids) == 2
    decisions = {}
    for cid in ids:
        payload = json.loads(GenPaths.of(run_root, 1).candidate(cid).read_text(
            encoding="utf-8"))
        decisions[cid] = (payload["decision"], payload["reason"])
    # 一个 accept、一个 superseded（§8.2：落选不计失败、无错误码）
    assert sorted(d for d, _ in decisions.values()) == ["accept", "reject"]
    assert "superseded" in [reason for _, reason in decisions.values()]
    report = _read_report(run_root, 1)
    assert len(report["candidates"]) == 2


# =====================================================================
# 成本事件与 seed 派生
# =====================================================================

def test_propose_seed_uses_run_id_and_generation():
    a = GEN.propose_seed(RUN_ID, 1)
    b = GEN.propose_seed(RUN_ID, 2)
    assert a != b
    assert GEN.propose_seed(RUN_ID, 1, seed=5) == 5


def _paired(**values):
    """C2 配对检验结果的轻量替身（``paired`` 段只需要 ``as_dict()``）。"""

    class _P:
        def __init__(self) -> None:
            self._v = {"method": "mcnemar", "metric": "win_rate", "p": 0.01,
                       "delta": 0.05, "significant": True, "direction": "up",
                       "n_pairs": 40}
            self._v.update(values)

        def as_dict(self):
            return dict(self._v)

    return _P()


# =====================================================================
# F32：``harness=`` 缺省时不得 ``NameError``（生产装配走的就是这一支）
# =====================================================================

def test_f32_harness_default_does_not_name_error(tmp_path):
    """F32：省略 ``harness=`` 必须解析出 ``run_ctx.harness_dir``，而不是抛 ``NameError``。

    原写法 ``getattr(run_ctx, "harness_dir", default_harness_dir())`` 会**无条件**求值
    默认参数，而 ``default_harness_dir`` 这个函数在仓库里从不存在 ⇒ 生产装配
    （``app._evolve_fn_provider`` 的 ``run_one_generation(harness=None)``）必然崩溃。
    本模块的单测**全部**显式传 ``harness=``，所以 1800+ 用例全绿也没拦住它——2026-10-09
    的 §3.2 实跑因此在做完任何事之前崩掉，白烧 ¥34.62 / 92 min。
    """
    ctx = FakeRunContext(tmp_path, harness=tmp_path / "h")
    assert GEN._resolve_harness_path(None, ctx) == tmp_path / "h"           # 默认取 run_ctx
    assert GEN._resolve_harness_path(tmp_path / "x", ctx) == tmp_path / "x"  # 显式优先
    # 鸭子类型替身（缺 ``harness_dir``）退到 infra 常量，而不是 ``NameError``
    assert GEN._resolve_harness_path(None, object()) == Path("harness")


def test_f32_full_generation_without_harness_kwarg(tmp_path, stub_pairing):
    """F32 的行为面：**完全不传** ``harness=`` 时整代仍能跑完（= 生产装配口径）。"""
    _harness, _evolve, _app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    outcome = GEN.run_one_generation(
        state=state, run_ctx=run_ctx, runner=None, config=run_ctx.app_config,
        evaluator=ev, client=_one_candidate_llm(), run_dir=run_root, seed=7)

    assert outcome is not None
    assert _read_report(run_root, 1)["decision"] in ("accept", "reject")
