"""§8.1 / FR-07-12 / FR-07-14 / FR-07-15：单代编排与报告落盘（PRD §11 集成 ① 的单测形态）。

覆盖：空候选、全淘汰、扫描失败、冒烟淘汰、winner → accept、HITL 挂起、
MOD-08 异常 → 保守 reject（NFR-07-7），以及「每次接受/拒绝都有 report.json」。

MOD-08 与 MOD-02 都打桩：
- ``evaluate_candidate`` 的 MOD-08 侧用 ``ScriptedEvaluator``（不跑局、不调模型）；
- ``guard.gate`` / ``load_harness_tools`` 用 stub（真实扫描在
  ``test_malicious_candidates.py`` 里跑，那里是本模块的安全用例主场）。
"""

from __future__ import annotations

import csv
import importlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dice_ratchet.evolve import checkpoint as CHECKPOINT
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
    """``judge.CandidateEvaluator`` 的替身（不跑局、不调模型）。

    ``**kwargs`` 是**有意**的（F57）：``parent_arms`` 新增了 ``inherited=``
    （上一代已测测量的跨代继承）。替身用 ``**kwargs`` 接收未识别的参数，否则新增
    可选参数会被测试替身**静默**挡掉——实测教训：不接受新参数的 ``parent_arms`` 抛
    ``TypeError``，而 ``_parent_arms`` 把它折成「parent 评估不可用」
    （报告里出现 ``heldout.source="unavailable"``），看起来像环境问题而不是契约漂移。
    """

    def __init__(self, *, parent_heldout=None, parent_smoke=None,
                 smoke=None, heldout=None, paired=None, error=None) -> None:
        self.parent_heldout = parent_heldout or FakeArm(arm="parent", win_rate=0.30)
        self.parent_smoke = parent_smoke or FakeArm(arm="parent-smoke", win_rate=0.40, n=2)
        self.smoke = smoke or FakeArm(arm="smoke", win_rate=0.40, n=2)
        self.heldout = heldout or FakeArm(arm="cand", win_rate=0.35)
        self.paired = paired
        self.error = error
        self.calls: list[str] = []
        self.inherited_seen: list = []
        self.seeds: tuple[int, ...] = (5000, 5001)

    def parent_arms(self, parent_dir, *, parent_version, **kwargs):
        self.calls.append("parent")
        inherited = kwargs.get("inherited")
        self.inherited_seen.append(inherited)
        if self.error is not None:
            raise self.error
        if inherited is not None:  # F57：heldout 复用；冒烟按本代 seed 重测
            return inherited, self.parent_smoke
        return self.parent_heldout, self.parent_smoke

    def candidate_arms(self, staging_dir, *, candidate, harness_version, **kwargs):
        self.calls.append(f"cand-{candidate.candidate_id}")
        if self.error is not None:
            raise self.error
        return self.smoke, self.heldout, {}

    # -- F57：跨代继承的资格检查（真实 ``CandidateEvaluator`` 由 evals.seeds 解析篮子）--

    def heldout_seeds(self, generation: int | None = None):
        return tuple(self.seeds)

    def eval_limits(self) -> dict:
        return {"t_max": 120, "min_support": 3, "drop_rate_max": 0.10}


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


def test_no_candidate_inherits_parent_heldout_without_measuring(tmp_path, monkeypatch):
    """F-M3-4：``no_candidate`` 也要把 parent 的已有测量继承进 report/台账（0 局）。

    m3r4 cycle 1 的现场：提案在校验阶段全灭后直接落骨架报告，台账 parent 行变成
    ``n=0``——「本代在役 harness 的 heldout」凭空消失。修复只做**继承**，绝不实测
    （没有候选可比较，补跑 40 局只是白花钱）。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    ev = ScriptedEvaluator()
    parent_arm = FakeArm(arm="parent", win_rate=0.175, n=40)
    monkeypatch.setattr(GEN, "_inherited_parent_heldout",
                        lambda *a, **k: (parent_arm, "inherited:gen-000"))
    outcome = _run(run_ctx, state, evaluator=ev, client=StubLLM([{"candidates": []}]))
    assert outcome.decision == "reject"
    assert outcome.error_code == str(E.E_EVOLVE_NO_CANDIDATE)
    # 一局都不跑：继承路径不得调用 parent_arms / candidate_arms。
    assert ev.calls == []

    report = _read_report(run_root, 1)
    assert report["heldout"]["n"] == 40
    assert report["heldout"]["win_rate"] == pytest.approx(0.175)
    assert report["heldout"]["source"] == "inherited:gen-000"

    # F57：``source=inherited:*`` 不写台账行（本代没有真实测量，写行会在图上多一个
    # 与上一代重合的点）。这里锁住「没有 n=0 的假行」，而不是要求一行 n=40。
    ledger = run_root / "metrics" / "generations.csv"
    if ledger.is_file():
        with ledger.open(encoding="utf-8") as handle:
            rows = [row for row in csv.DictReader(handle) if row["generation"] == "1"]
        assert rows == [], rows


def test_propose_failure_still_writes_report(tmp_path):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    outcome = _run(run_ctx, state, evaluator=ScriptedEvaluator(),
                   client=StubLLM([RuntimeError("llm down"), RuntimeError("llm down"),
                                   RuntimeError("llm down")]))
    assert outcome.error_code == str(E.E_EVOLVE_PROPOSE_FAILED)
    report = _read_report(run_root, 1)
    assert report["decision"] == "reject"


def test_rejected_candidate_is_recorded(tmp_path, stub_pairing):
    """**F62（2026-10-14）**：play 面未变（只改学习侧 ``reflection.md``）⇒ C2 拒绝，
    候选明细仍落盘。

    原先这条用例用「统计不显著」制造拒绝；改判之后那是**通过**条件（见下一条用例）。
    换成「只改学习侧」的补丁：它是一条**合法**补丁（路径在白名单内、diff 可应用），
    但没有改变 play 面 ⇒ C2 以 ``no_play_change`` 拒绝。这同时钉住了新语义的边界：
    D-M3-7 的继承资格全靠 play 指纹，因此「只改学习侧」的补丁在本机制下无法成为棘轮的
    一步（要改学习侧就必须连带一次 play 面改动）。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(significant=False)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.35),
                           paired=_paired(significant=False))
    learn_only = prompt_diff(path="harness/prompts/reflection.md", added="只在反思里加一行")
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm(learn_only))
    assert outcome.decision == "reject"
    ids = GenPaths.of(run_root, 1).candidate_ids()
    assert len(ids) == 1
    payload = json.loads(GenPaths.of(run_root, 1).candidate(*ids).read_text(
        encoding="utf-8"))
    assert payload["decision"] == "reject"
    assert payload["error_code"] == str(E.E_EVOLVE_NO_GAIN)
    assert payload["verdict"]["reason"].startswith("C2:no_play_change")
    # 新语义下「统计不显著」**不再**是拒绝理由（它只落 trace 与 paired）
    trace = {e["condition"]: e for e in payload["verdict"]["condition_trace"]}
    assert trace["C2"]["value"]["significant"] is False
    assert trace["C2"]["value"]["candidate_play_hash"] == \
        trace["C2"]["value"]["parent_play_hash"]


def test_f62_flat_but_changed_candidate_is_accepted(tmp_path, stub_pairing):
    """**F62 的核心护栏**：统计完全不显著（delta=0、flat）但 play 面变了 ⇒ **accept**。

    这就是「取消 C2 的统计门」在生成链路上的行为：M3 正式实验里三代候选的实测形态
    （+2.5/+7.5/+5.0 pp，p=1.0/0.55/0.75）在这一版之后应当能过 C2，
    再由 C1/C3/C4/C5 决定去留。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.0, significant=False, direction="flat")
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.30),
                           paired=_paired(delta=0.0, significant=False, direction="flat"))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "accept", (outcome.decision, outcome.reason)
    ids = GenPaths.of(run_root, 1).candidate_ids()
    payload = json.loads(GenPaths.of(run_root, 1).candidate(*ids).read_text(
        encoding="utf-8"))
    trace = {e["condition"]: e for e in payload["verdict"]["condition_trace"]}
    assert trace["C2"]["passed"] is True
    assert trace["C2"]["value"]["candidate_play_hash"] != \
        trace["C2"]["value"]["parent_play_hash"]


def test_f62_paired_by_metric_lands_in_the_candidate_file(tmp_path, stub_pairing,
                                                          monkeypatch):
    """F62 的第二半：逐指标统计（含连续轴）必须真的落进候选产物（判据 2 的证据）。

    打桩 ``_paired_by_metric``（真实实现要真 ``ArmResult`` 的逐局结果）——这里钉的是
    **接线**：它算出来的东西要出现在 ``candidates/{id}.json`` 的 ``report.paired`` 里。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    # 注意：``dice_ratchet.evolve`` 把子模块名 ``judge`` 重绑到了同名函数上，
    # ``import dice_ratchet.evolve.judge as m`` 拿到的是**函数**（与 test_evolve_judge
    # 头部那条注记同一个坑）⇒ 必须走 importlib。
    import importlib

    judge_mod = importlib.import_module("dice_ratchet.evolve.judge")

    monkeypatch.setattr(judge_mod, "_paired_by_metric", lambda *a, **k: {
        "mean_stages_cleared": {"delta": 0.30, "method": "paired_bootstrap",
                                "p": 0.0, "significant": True, "direction": "up"}})
    _run(run_ctx, state, evaluator=ScriptedEvaluator(), client=_one_candidate_llm())
    ids = GenPaths.of(run_root, 1).candidate_ids()
    payload = json.loads(GenPaths.of(run_root, 1).candidate(*ids).read_text(
        encoding="utf-8"))
    by_metric = payload["report"]["paired"]["by_metric"]
    assert by_metric["mean_stages_cleared"]["delta"] == 0.30
    assert by_metric["mean_stages_cleared"]["method"] == "paired_bootstrap"


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
    # F67（2026-10-14）：胜率轴已从冒烟门删除（n=2 分辨率 0.5，无诊断力）⇒ 用**非法率**
    # 制造冒烟淘汰（步级统计，2 局也有上千次尝试）。
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing()
    ev = ScriptedEvaluator(smoke=FakeArm(win_rate=0.0, illegal_rate=0.5, n=2))
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


# =====================================================================
# F47-D：基础设施失败 ≠ 裁判拒绝
# =====================================================================

def test_f47_accept_infra_failure_is_recorded_as_error_reject(tmp_path, stub_pairing,
                                                              monkeypatch):
    """accept 抛 ``InfraError``（如 ``E_EVOLVE_GIT_FAILED``：脏工作区）时必须记为
    ``decision="error_reject"``，不得写成 ``"reject"``，更不得写成 ``"accept"``。

    原先有两个自相矛盾的口径：
    - ``_finish_report`` 写 ``decision="reject"``（长得跟「候选不显著」一模一样）；
    - ``build_decision_sections`` 只看 gate 状态，而此时 gate 是 ``approved``
      ⇒ **report.json 写 ``decision="accept"``**，而磁盘上 ``harness/versions/``
      里根本没有新版本（幻影 accept）。事件流与报告因此互相打脸。

    本用例同时钉住两点：报告口径 = ``error_reject``；``evolve.decision`` 事件一致。
    """
    from dice_ratchet.infra import events
    from dice_ratchet.infra.errors import InfraError

    harness, _evolve, _app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    versions_before = sorted(p.name for p in (harness / "versions").iterdir())
    calls: list[int] = []

    def _boom(*args, **kwargs):
        calls.append(1)
        raise InfraError(E.E_EVOLVE_GIT_FAILED, "脏工作区：拒绝提交 harness")

    monkeypatch.setattr(GEN.versioning_mod, "accept", _boom)

    seen_events = tmp_path / "events.jsonl"
    events.bind(seen_events, str(run_ctx.run_id))
    try:
        outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    finally:
        events.unbind()
    seen = [json.loads(line) for line in seen_events.read_text(encoding="utf-8").splitlines()
            if line.strip()]

    # 前置：候选真的过了六道门 —— ``accept`` 只在 ``gate.status == "approved"`` 时被调用
    assert calls, "accept 必须被调用过（否则本用例考的不是这条路径）"

    # 反例自检：只看 gate 状态（= 修复前的口径）确实会推出 "accept"（幻影 accept 的来源）
    phantom = GEN.versioning_mod.build_decision_sections(
        1, [], None,
        GEN.versioning_mod.gate_state_for(generation=1, status="approved",
                                          harness_version=0, parent_version=0))
    assert phantom["decision"] == "accept", "反例：gate=approved 会被推成 accept"

    assert outcome.decision == "error_reject"
    assert str(outcome.error_code) == str(E.E_EVOLVE_GIT_FAILED)

    report = _read_report(run_root, 1)
    assert report["decision"] == "error_reject", "基础设施失败不得写成 accept/reject"
    assert sorted(p.name for p in (harness / "versions").iterdir()) == versions_before, \
        "失败时不得留下版本目录"

    decisions = [row for row in seen if row["event"] == "evolve.decision"]
    assert decisions and decisions[-1]["decision"] == "error_reject", \
        "事件流与报告必须同口径"


# =====================================================================
# F42-B：提案检查点（评估之前落盘；resume 时指纹一致则复用同一候选）
# =====================================================================
#
# 现场：API 提案**不可复现**（注册表标注 `api_supports_seed=false`，实测「字节相同的请求
# 9 次返回 9 个不同响应」）⇒ 不落检查点的话，resume 时候选 id 必然变，
# `metrics/arms/genNNN-*-cand-<id>.json` 归档必然 miss，能省的只有 parent 侧。
# 机制：评估**之前**把 ProposalResult 落 `generations/gen-NNN/proposals/proposal.json`，
# 连同输入指纹（run_id / generation / propose_seed / harness_version / memory_version /
# prompt_digest）；指纹一致 → 复用同一 candidate id 与 diff，**0 次 proposer 调用**。

def test_proposal_checkpoint_is_written_before_evaluation(tmp_path, stub_pairing):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    _run(run_ctx, state, evaluator=ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                                                     paired=_paired(delta=0.10)),
         client=_one_candidate_llm())
    path = CHECKPOINT.proposal_checkpoint_path(run_root, 1)
    assert path.is_file(), "提案没有在评估之前落检查点（F42-B）"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == CHECKPOINT.PROPOSAL_CHECKPOINT_VERSION
    assert data["fingerprint"].startswith("sha256:")
    assert len(data["proposal"]["candidates"]) == 1
    cand = data["proposal"]["candidates"][0]
    for key in ("candidate_id", "diff", "rationale", "expected_effect"):
        assert key in cand, f"检查点缺 {key}"


def test_resume_reuses_the_proposal_with_zero_proposer_calls(tmp_path, stub_pairing):
    """跑到一半被杀 → 检查点命中：同一 candidate id、**0 次 proposer 调用**。

    真实中断点：Ctrl-C（``loop`` 的 SIGINT 处理器抛 ``KeyboardInterrupt``，正是 F42 描述的
    「一代 3.75 h 中途被打断」）。``KeyboardInterrupt`` 不是 ``Exception`` ⇒ 不会被
    ``evaluate_candidate`` / ``_parent_arms`` 的保守化捕获吞掉，直接穿透 MOD-07，
    ``report.json`` 不落盘 ⇒ 与现场「``loop`` 把 phase 重置为 playing 重跑整代」完全同形。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)

    class _Interrupted(ScriptedEvaluator):
        """第一次进入 parent 臂时模拟 Ctrl-C（此后照常）。"""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.boom = True

        def parent_arms(self, parent_dir, *, parent_version, **kwargs):
            if self.boom:
                self.boom = False
                raise KeyboardInterrupt("模拟：一代评估中途被 Ctrl-C")
            return super().parent_arms(parent_dir, parent_version=parent_version, **kwargs)

    first_client = _one_candidate_llm()
    with pytest.raises(KeyboardInterrupt):
        _run(run_ctx, state, evaluator=_Interrupted(), client=first_client)
    assert len(first_client.calls) == 1, "第一轮应当真实调用过 proposer 并落检查点"
    checkpoint = CHECKPOINT.proposal_checkpoint_path(run_root, 1)
    assert checkpoint.is_file(), "提案没有在评估之前落检查点（F42-B）"
    assert GenPaths.of(run_root, 1).report.is_file() is False, \
        "本代不应有 report.json（被杀在中途）"
    first_id = json.loads(checkpoint.read_text(encoding="utf-8"))[
        "proposal"]["candidates"][0]["candidate_id"]

    # resume：客户端**没有** payload 了（真被调用就会 AssertionError）
    second_client = StubLLM([])
    outcome = _run(run_ctx, state,
                   evaluator=ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                                               paired=_paired(delta=0.10)),
                   client=second_client)
    assert second_client.calls == [], "提案被复用后仍然调了 proposer（每次调用都是钱）"
    assert GenPaths.of(run_root, 1).candidate_ids() == (first_id,)
    assert outcome.decision == "accept"


def test_proposal_checkpoint_mismatch_falls_back_to_the_client(tmp_path, stub_pairing):
    """指纹不一致（换了 harness/记忆/模板/seed）⇒ 不复用，照常重新提案（fail-closed）。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    _run(run_ctx, state, evaluator=ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                                                     paired=_paired(delta=0.10)),
         client=_one_candidate_llm())
    path = CHECKPOINT.proposal_checkpoint_path(run_root, 1)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fingerprint"] = "sha256:0000"
    path.write_text(json.dumps(data), encoding="utf-8")

    client = _one_candidate_llm()
    _run(run_ctx, state, evaluator=ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                                                     paired=_paired(delta=0.10)),
         client=client)
    assert len(client.calls) == 1, "指纹不一致却复用了检查点"


def test_corrupt_proposal_checkpoint_is_ignored(tmp_path, stub_pairing):
    """检查点损坏 ⇒ 照常重新提案（不得抛错、不得静默放弃本代）。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    _run(run_ctx, state, evaluator=ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                                                     paired=_paired(delta=0.10)),
         client=_one_candidate_llm())
    CHECKPOINT.proposal_checkpoint_path(run_root, 1).write_text("{not json",
                                                               encoding="utf-8")
    client = _one_candidate_llm()
    _run(run_ctx, state, evaluator=ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                                                     paired=_paired(delta=0.10)),
         client=client)
    assert len(client.calls) == 1


def test_proposal_fingerprint_is_stable_and_input_sensitive():
    """指纹：同输入恒定；``propose_seed`` / ``harness_version`` / prompt 任一变化即变。"""
    inputs = {"system": "S", "user": "U", "schema": {"type": "object"}}
    base = dict(run_id="r1", generation=1, propose_seed=7, harness_version=0,
                memory_version=0, inputs=inputs)
    one = CHECKPOINT.proposal_fingerprint(**base)
    assert one == CHECKPOINT.proposal_fingerprint(**base)
    assert one != CHECKPOINT.proposal_fingerprint(**{**base, "propose_seed": 8})
    assert one != CHECKPOINT.proposal_fingerprint(**{**base, "harness_version": 1})
    assert one != CHECKPOINT.proposal_fingerprint(**{**base, "memory_version": 1})
    assert one != CHECKPOINT.proposal_fingerprint(
        **{**base, "inputs": {**inputs, "user": "U2"}})
    assert one != CHECKPOINT.proposal_fingerprint(**{**base, "run_id": "r2"})


# =====================================================================
# F54：候选评估的 held-out 篮子必须来自 `evolve.heldout_basket`
# =====================================================================

def test_f54_evaluator_uses_configured_heldout_basket(tmp_path, monkeypatch):
    """给定 ``evolve.heldout_basket="H3"``；当编排装配默认评估器；

    则 ``CandidateEvaluator.heldout_basket == "H3"``。

    **为什么这是护栏而不是实现细节**：修复前该字段只在 ``judge.py`` 有类级默认
    ``HOLDOUT_BASKET = "H1"``，而``loop.generation.write_gen000_baseline`` 也写死 H1
    ⇒ 「彩排只用 H3」在代码层不可达，实测跑到 H1 上（一次性资源被消耗）。
    这条用例把「候选评估篮子」钉在配置键上；「基线篮子」由
    ``tests/integration/test_loop_closure.py::test_f54_gen000_baseline_uses_configured_basket``
    从**真实产物文件名**上钉住。两者同源 ⇒ parent/candidate 不可能跨越两套 seed。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(
        tmp_path, heldout_basket="H3")
    captured: dict[str, object] = {}
    judge_mod = importlib.import_module("dice_ratchet.evolve.judge")
    real = judge_mod.CandidateEvaluator

    def _spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(judge_mod, "CandidateEvaluator", _spy)
    GEN._build_evaluator(cfg=evolve, app_cfg=app_cfg, evaluator=None, runner=None,
                         protocol=None, memory_version=1, run_id=RUN_ID, gen=0,
                         smoke_seeds=(1000, 1001), episode_config=None, run_dir=run_root)
    assert captured.get("heldout_basket") == "H3", (
        f"`evolve.heldout_basket=H3` 必须转发给 CandidateEvaluator；实际 {captured!r}")


def test_f54_evaluator_falls_back_to_class_default_when_config_lacks_key(tmp_path):
    """配置对象缺 ``evolve.heldout_basket``（旧配置/替身）时**不静默换篮子**：不传该字段，
    由 ``CandidateEvaluator`` 的类级默认（H1）兜底。"""
    from evolve_support import make_config

    class _NoBasket:
        pass

    class _AppCfg:
        evolve = _NoBasket()

    judge_mod = importlib.import_module("dice_ratchet.evolve.judge")
    ev = GEN._build_evaluator(cfg=make_config(), app_cfg=_AppCfg(), evaluator=None,
                              runner=None, protocol=None, memory_version=0,
                              run_id=RUN_ID, gen=0, smoke_seeds=(), episode_config=None,
                              run_dir=None)
    assert isinstance(ev, judge_mod.CandidateEvaluator)
    assert ev.heldout_basket == judge_mod.HOLDOUT_BASKET == "H1"


# =====================================================================
# F57（2026-10-12）：parent 的测量**跨代继承**（上一代 accept ⇒ 本代不重测）
# =====================================================================

def _write_winner_archive(run_root: Path, *, generation: int, candidate_id: str,
                          harness_version: int, seeds: tuple[int, ...],
                          win_rate_result: str = "win") -> Path:
    """写一份「上一代胜出候选的 heldout 臂归档」（形状与 ``write_arm_archive`` 一致）。"""
    arms = run_root / "metrics" / "arms"
    arms.mkdir(parents=True, exist_ok=True)
    path = arms / f"gen{generation:03d}-heldout-H1-cand-{candidate_id}.json"
    path.write_text(json.dumps({
        "arm": f"cand-{candidate_id}", "split": "heldout", "basket": "H1",
        "harness_version": int(harness_version), "memory_version": 1,
        "run_id": RUN_ID, "provenance": {"basket": "H1",
                                         "harness_version": int(harness_version)},
        "episodes": [{"arm": f"cand-{candidate_id}", "seed": int(s),
                      "outcome": win_rate_result, "stages_cleared": 5,
                      "hearts_left": 1, "battles_won": 5, "decisions": 90,
                      "decisions_llm": 90, "steps": 200, "illegal_attempts": 0,
                      "fallback_decisions": 0, "kind_counts": [0] * 16,
                      "slot_counts": {}, "seq_hash": f"s{s}", "dropped": False,
                      "degraded": False, "decision_kinds": [0], "error_code": None,
                      "truncation_reason": None} for s in seeds],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_f57_parent_measurement_is_inherited_after_accept(tmp_path, stub_pairing):
    """上一代 **accept** ⇒ 本代 parent 的 heldout **继承**那批局（0 次重测），出处写进报告。

    这正是用户口径：``gen-001 = v0+P1 被接受`` ⇒ ``v1`` 自动成为下一代的起点，
    ``gen-002`` 直接在 ``v1`` 上跑 ``P2``，**不重跑 v1 的评估**。
    「不重跑」在这里是硬断言：``ev2.inherited_seen`` 拿到了非 ``None`` 的继承结果，
    且报告的 ``heldout.source == "inherited:gen-001"``。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    ev1 = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40), paired=_paired(delta=0.10))
    out1 = _run(run_ctx, state, evaluator=ev1, client=_one_candidate_llm())
    assert out1.decision == "accept" and int(out1.harness_version) == 1
    cid = GenPaths.of(run_root, 1).candidate_ids()[0]

    # gen-001 的 winner 归档（真实运行时由 evaluate_arm 落；这里只造那份工件）
    _write_winner_archive(run_root, generation=1, candidate_id=cid, harness_version=1,
                          seeds=ev1.seeds)

    ev2 = ScriptedEvaluator()
    state2 = FakeState(generation=1, run_id=RUN_ID)
    out2 = _run(run_ctx, state2, evaluator=ev2, client=_one_candidate_llm())
    assert ev2.inherited_seen, "parent_arms 必须被调用"
    inherited = ev2.inherited_seen[-1]
    assert inherited is not None, "上一代 accept 且版本匹配 ⇒ 必须继承（不是重测）"
    assert inherited.arm == "parent"
    assert inherited.metrics.n == len(ev1.seeds)
    report2 = _read_report(run_root, 2)
    assert report2["heldout"]["source"] == "inherited:gen-001"
    assert report2["heldout"]["win_rate"] == 1.0, "继承的是那份归档的数字（全胜）"


def test_f57_rejected_generation_is_not_an_inheritance_source(tmp_path):
    """上一代 **reject** ⇒ 不是继承来源：即便同名归档躺在盘上也不许用。

    用户裁决「补丁被拒不重跑同一 ``harness_version``」说的是**不给被拒的候选再花一次
    钱**，不是「拿旧数字当新测量」。旧 run（``report.json`` 里**没有**
    ``harness_play_hash``）沿用这条判据——本用例把它当**回落分支**逐条钉住。

    用**手工构造**的上一代报告（而不是跑一代）：这里测的是
    ``_inherited_parent_heldout`` 的资格判断，跑一代只会引入无关的夹具依赖。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    ev = ScriptedEvaluator()
    cid = "deadbeef0001"
    _write_winner_archive(run_root, generation=1, candidate_id=cid, harness_version=0,
                          seeds=ev.seeds)
    parent = GEN.versioning_mod.load_version(0, harness=harness)
    assert int(parent.version) == 0

    def _write_prev_report(decision: str, harness_version: int) -> None:
        path = GenPaths.of(run_root, 1).report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"generation": 1, "decision": decision,
                                    "harness_version": harness_version,
                                    "parent_version": 0,
                                    "candidates": [{"candidate_id": cid,
                                                    "decision": decision}]},
                                   ensure_ascii=False), encoding="utf-8")

    # ① reject：归档再全也不能用
    _write_prev_report("reject", 0)
    inherited, source = GEN._inherited_parent_heldout(ev, run_root=run_root, gen=2,
                                                      parent=parent)
    assert inherited is None and source == "measured", "reject 不是继承来源"

    # ② accept 但版本不匹配（例如报告来自别的 harness）：同样不能用
    _write_prev_report("accept", 7)
    inherited, source = GEN._inherited_parent_heldout(ev, run_root=run_root, gen=2,
                                                      parent=parent)
    assert inherited is None and source == "measured", "版本不同不得继承"

    # ③ 正例：accept + 版本匹配 ⇒ 继承（证明 ①② 的 None 不是「函数永远返回 None」）
    _write_prev_report("accept", 0)
    inherited, source = GEN._inherited_parent_heldout(ev, run_root=run_root, gen=2,
                                                      parent=parent)
    assert inherited is not None and source == "inherited:gen-001"
    assert inherited.arm == "parent" and inherited.metrics.n == len(ev.seeds)

    # ④ 归档不在 ⇒ 退回重测（fail-closed）
    for path in (run_root / "metrics" / "arms").glob("*.json"):
        path.unlink()
    inherited, source = GEN._inherited_parent_heldout(ev, run_root=run_root, gen=2,
                                                      parent=parent)
    assert inherited is None and source == "measured"


def test_dm35_inheritance_is_keyed_on_play_hash_not_version(tmp_path):
    """D-M3-7：继承的资格是 **play 面指纹相同**，不是「上一代 accept」。

    用户的架构（步骤 3/5）：``v1 = v0 + （仅 P1 不可行的提示词）``——补丁被拒时 play 面
    逐字节不变，所以 cycle 1 的起点 heldout 应当**直接复用** cycle 0 的起点那份测量
    （它最终来自 gen-000 基线那 40 局），不必重跑。修复前的资格判据要求
    ``decision == "accept"``，于是**每一代都白跑一遍 parent 的 heldout**（实测 m3-formal：
    每代 40 局 ≈¥21，共 3 代）。

    本用例覆盖三条：
    ① 中间代全是 reject，但 play_hash 与 gen-000 相同 ⇒ 仍能从 **gen-000** 继承；
    ② play_hash 变了 ⇒ 一律不继承（fail-closed）；
    ③ 报告里有 play_hash 时**不看** ``decision``/``harness_version``（拒绝也照样继承）。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    ev = ScriptedEvaluator()
    arms = run_root / "metrics" / "arms"
    arms.mkdir(parents=True, exist_ok=True)

    # gen-000 的基线臂（真实运行时由 `write_gen000_baseline` 的 evaluate_arm 落）
    _write_winner_archive(run_root, generation=0, candidate_id="baseline0001",
                          harness_version=0, seeds=ev.seeds)
    baseline_arm = arms / "gen000-heldout-H1-parent.json"
    (arms / "gen000-heldout-H1-cand-baseline0001.json").rename(baseline_arm)

    def _write_report(gen: int, *, decision: str, play_hash: str,
                      harness_version: int = 0) -> None:
        path = GenPaths.of(run_root, gen).report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"generation": gen, "decision": decision,
                                    "harness_version": harness_version,
                                    "parent_version": 0,
                                    "harness_play_hash": play_hash,
                                    "candidates": []}, ensure_ascii=False),
                        encoding="utf-8")

    parent = GEN.versioning_mod.load_version(0, harness=harness)
    same = "sha256:" + "a" * 64
    _write_report(0, decision="baseline", play_hash=same)

    # ① 中间代 reject，但 play 面相同 ⇒ 继续往前找到 gen-000 的基线臂
    _write_report(1, decision="reject", play_hash=same, harness_version=1)
    inherited, source = GEN._inherited_parent_heldout(ev, run_root=run_root, gen=2,
                                                      parent=parent, play_hash=same)
    assert inherited is not None, "同 play 面就必须能继承（拒绝不是障碍）"
    assert source == "inherited:gen-000", source

    # ② play 面不同 ⇒ 不得继承
    inherited, source = GEN._inherited_parent_heldout(
        ev, run_root=run_root, gen=2, parent=parent, play_hash="sha256:" + "b" * 64)
    assert inherited is None and source == "measured", "play 面变了就必须重测"

    # ③ 有 play_hash 时不看 decision：把 gen-001 改成 reject + 版本乱写，仍从 gen-000 继承
    _write_report(1, decision="reject", play_hash=same, harness_version=99)
    inherited, source = GEN._inherited_parent_heldout(ev, run_root=run_root, gen=2,
                                                      parent=parent, play_hash=same)
    assert inherited is not None and source == "inherited:gen-000"


def test_dm35_dead_end_bump_does_not_change_play_hash(tmp_path):
    """``v1 = v0 + dead_ends.md`` 必须与 ``v0`` **同 play 面**（用户的步骤 3 成立）。

    现场：``dead_ends.md`` 一旦被算进 play 面，补丁被拒就会「改变起点」，于是
    ① 继承失效（每代重测 40 局），② 判据 1 把「同一个 harness 的两次重测」当成两代比较
    （F57 要修的正是这件事）。本用例把这条写成可执行的断言。
    """
    harness, _evolve, _cfg, run_root, _ctx, _state = _setup(tmp_path)
    from dice_ratchet.evolve.versioning import play_hash, play_plane_files, write_dead_end
    from dice_ratchet.evolve.schemas import Candidate

    before = play_hash(harness)
    assert "dead_ends.md" not in play_plane_files(harness), "死路记录不在 play 面内"
    write_dead_end(harness, generation=1,
                   candidate=Candidate.create(
                       kind="prompt_patch",
                       diff="--- a/harness/prompts/decision.md\n"
                            "+++ b/harness/prompts/decision.md\n@@ -1 +1 @@\n-a\n+b\n",
                       rationale="试过了", expected_effect=("win_rate", "up", 2),
                       touched_files=["harness/prompts/decision.md"]),
                   reason="C2:no_significant_gain", error_code="E_EVOLVE_NO_GAIN")
    assert play_hash(harness) == before, "写死路记录不得改变 play 面指纹"
    assert "dead_ends.md" in [p.name for p in harness.iterdir()]


# =====================================================================
# D-M3-7（2026-10-14）：`heldout` 段 = **本代结束时在役**那份 harness 的测量
# =====================================================================

def test_dm35_accepted_generation_records_the_new_harness_measurement(tmp_path, stub_pairing):
    """接受一代 ⇒ 该代 ``heldout`` 段必须是**新** harness 的测量（不是起点的）。

    用户的架构（步骤 3）：门禁通过则 ``v1 = v0 + P1``，**后续比较的 heldout 就是步骤 3
    的 heldout**。若 ``heldout`` 恒取起点臂（F57 原口径），「接受了一代」在台账里
    **永远不可见**（新测量没有落点）⇒ 判据 1 的「held-out 单调不降」退化成拿起点与
    起点比的恒真命题。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.10)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.40),
                           paired=_paired(delta=0.10))
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm())
    assert outcome.decision == "accept"

    report = _read_report(run_root, 1)
    assert report["heldout"]["win_rate"] == 0.40, "接受的代必须记**新** harness 的数"
    assert report["heldout"]["source"] == "measured"
    assert report["harness_version"] == 1 and report["parent_version"] == 0
    assert report["harness_play_hash"], "报告必须带在役 harness 的 play 面指纹"
    # 起点（v0）的指纹在 accept **之后**已不可得 ⇒ 实现必须在任何写入前取好它
    assert report["harness_play_hash"] != GEN.versioning_mod.play_hash(
        harness / "versions" / "v0")


def test_dm35_rejected_generation_keeps_start_measurement_and_bumps_lightweight(
        tmp_path, stub_pairing):
    """拒绝 ⇒ ① 台账数字仍是起点那份（``measured``/``inherited`` 如实标注）；
    ② 版本号**轻量推进**到 v1（无快照、无 tag）；③ 死路记录落盘（D1=A/D2=A）。

    F62（2026-10-14）：拒绝理由换成 **C2 ``no_play_change``**（只改学习侧的补丁）——
    「统计不显著」自本版起不再是拒绝理由。
    """
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    stub_pairing(delta=0.01, significant=False)
    ev = ScriptedEvaluator(heldout=FakeArm(win_rate=0.31),
                           paired=_paired(delta=0.01, significant=False))
    learn_only = prompt_diff(path="harness/prompts/reflection.md", added="只在反思里加一行")
    outcome = _run(run_ctx, state, evaluator=ev, client=_one_candidate_llm(learn_only))
    assert outcome.decision == "reject"
    assert int(outcome.harness_version) == 1, "拒绝也推进版本号（D2=A）"

    report = _read_report(run_root, 1)
    assert report["heldout"]["win_rate"] == 0.30, "拒绝 ⇒ 在役 harness 还是起点那份"
    assert report["harness_version"] == 1 and report["parent_version"] == 0

    pointer = json.loads((harness / "version.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 1, "version.json 必须指到 v1"
    assert pointer["reason"].startswith("rejected:"), pointer["reason"]
    assert not (harness / "versions" / "v1").exists(), "拒绝不得复制全量快照"

    dead = (harness / "dead_ends.md").read_text(encoding="utf-8")
    cid = GenPaths.of(run_root, 1).candidate_ids()[0]
    assert cid in dead and "gen-001" in dead, dead
    assert "C2" in dead, "死路记录必须写明挂在哪个判定条件上"
    assert "no_play_change" in dead, dead


def test_dm35_play_plane_unchanged_after_rejection(tmp_path, stub_pairing):
    """拒绝之后 ``play_hash`` 必须**逐字节不变**——继承的资格全靠它（用户的步骤 3）。"""
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    before = GEN.versioning_mod.play_hash(harness)
    stub_pairing(delta=0.01, significant=False)
    learn_only = prompt_diff(path="harness/prompts/reflection.md", added="只在反思里加一行")
    _run(run_ctx, state, evaluator=ScriptedEvaluator(paired=_paired(delta=0.01,
                                                                  significant=False)),
         client=_one_candidate_llm(learn_only))
    assert GEN.versioning_mod.play_hash(harness) == before
    assert _read_report(run_root, 1)["harness_play_hash"] == before
