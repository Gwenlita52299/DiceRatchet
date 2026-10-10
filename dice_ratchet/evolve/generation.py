"""MOD-07 单代编排（PRD §8.1；FR-07-14/15）。

``run_one_generation(state, run_ctx, runner)`` 是 MOD-06 ``evolve_fn`` 的实现，也是
PRD §8.1 伪代码的落地：**一次调用 = 一个演化周期**。

时序（与 §8.1 逐条对应）::

    parent  = versioning.load_version(current_version())
    bundle  = build_evidence_bundle(...)            # FR-07-2/3（INV-5 硬闸在 propose 内）
    prop    = propose(bundle, cfg, rng_seed)        # FR-07-1
    smoke   = seeds.take("train", generation=g)[-smoke_n:]   # FR-07-6
    parent_ho / parent_smoke = evaluate_arm(...)    # FR-07-7：parent 每代重算
    for cand in order_candidates(prop.candidates):
        staging = stage_candidate(...)              # 落盘 + 应用 diff
        validate_candidate → guard.gate → smoke → heldout → judge
    finish(): gate.await_human → accept/rollback → report.json

**绝不静默退化**（FR-07-15）：空候选、全淘汰、扫描失败都落 ``report.json``
（``decision="reject"`` + 对应错误码）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from dice_ratchet.infra import events
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import write_json_atomic

from . import gate as gate_mod
from . import judge as judge_mod
from . import proposer as proposer_mod
from . import versioning as versioning_mod
from .errors import (HoldoutLeakError, LoadFailedError, ScanFailedError,
                     VersionConflictError)
from .paths import EvolveContext, GenPaths, harness_dir
from .schemas import (Candidate, CandidateReport, GateState, HarnessRef, Verdict,
                      write_candidate_report)

__all__ = [
    "GenerationResult", "guard_gate", "load_harness_tools_checked", "propose_seed",
    "report_metrics_builder", "run_one_generation",
]


#: proposer 的确定性子 seed 域（NFR-07-1：同 ``(run_id, gen, seed)`` 必得同结果）。
_PROPOSE_SEED_DOMAIN: str = "propose"


# =====================================================================
# seed 派生（MOD-09 derive_seed 是唯一入口）
# =====================================================================

def propose_seed(run_id: str, gen: int, *, seed: int | None = None) -> int:
    """``evolve.seed`` 由 MOD-09 从 ``run_id`` 派生（NFR-07-1）。

    ``seed`` 显式给出时直接使用（``--freeze-seed`` / 测试）。
    """
    if seed is not None:
        return int(seed)
    from dice_ratchet.infra.config import derive_seed

    return int(derive_seed(str(run_id), int(gen), _PROPOSE_SEED_DOMAIN))


# =====================================================================
# MOD-02 门禁与受限加载（FR-07-13）
# =====================================================================

def guard_gate(staging: Path, *, run_dir: Path | None = None) -> Any:
    """MOD-02 ``guard.gate(harness_dir) -> GateReport``（INV-4 静态扫描）。"""
    from dice_ratchet.sandbox.guard import gate as sandbox_gate

    try:
        return sandbox_gate(Path(staging), run_dir=run_dir)
    except InfraError:
        raise
    except Exception as exc: # noqa: BLE001 - 扫描器自身异常：fail-closed
        raise ScanFailedError(f"guard.gate 异常：{type(exc).__name__}: {exc}",
                              hint="静态扫描不可用时按 fail-closed 处理（不得跳过扫描）"
                              ) from exc


def load_harness_tools_checked(staging: Path) -> Any:
    """MOD-02 ``guard.load_harness_tools()`` 受限加载（子进程 worker，Spec B-20）。

    失败抛 ``E_EVOLVE_LOAD_FAILED``：已 accept 则立即回滚 ``parent_version``。
    """
    from dice_ratchet.sandbox.guard import load_harness_tools

    try:
        return load_harness_tools(Path(staging))
    except InfraError as exc:
        raise LoadFailedError(f"受限加载失败：{exc}", hint=str(getattr(exc, "hint", ""))) \
            from exc
    except Exception as exc: # noqa: BLE001
        raise LoadFailedError(f"受限加载异常：{type(exc).__name__}: {exc}") from exc


def _gate_status(report: Any) -> str:
    """MOD-02 ``GuardReport`` → PRD §6.2 的 ``status: "pass"|"fail"``。

    ``GuardReport`` 用 ``ok: bool`` 表达结论（``violations`` 是格式化字符串列表），
    这里做一次归一；两种形态（属性 / 字典）都接受。
    """
    if isinstance(report, Mapping):
        if "status" in report:
            return str(report["status"])
        ok = report.get("ok")
    else:
        status = getattr(report, "status", None)
        if status is not None:
            return str(status)
        ok = getattr(report, "ok", None)
    if ok is None:
        return "fail" # fail-closed：结论不可读即拒绝
    return "pass" if bool(ok) else "fail"


def _gate_violations(report: Any) -> list[dict[str, Any]]:
    """门禁发现（``GuardReport.findings`` → ``{code,file,line,symbol}``）。"""
    raw = getattr(report, "findings", None)
    if raw is None and isinstance(report, Mapping):
        raw = report.get("findings")
    if raw is None:
        # 退化形态：只有格式化字符串列表
        strings = (report.get("violations") if isinstance(report, Mapping)
                   else getattr(report, "violations", None))
        return [{"detail": str(item)} for item in list(strings or ())]
    out: list[dict[str, Any]] = []
    for item in list(raw or ()):
        payload = getattr(item, "to_dict", None)
        if callable(payload):
            out.append(dict(payload()))
        elif isinstance(item, Mapping):
            out.append(dict(item))
        else:
            out.append({"detail": str(item)})
    return out


# =====================================================================
# 结果的内部聚合
# =====================================================================

@dataclass
class GenerationResult:
    """``run_one_generation`` 的结构化结果（``EvolveOutcome`` 由此产出）。"""

    generation: int
    decision: str = "reject"
    parent: HarnessRef | None = None
    accepted: HarnessRef | None = None
    parent_heldout: Any = None
    reports: list[CandidateReport] = field(default_factory=list)
    winner: CandidateReport | None = None
    gate_state: GateState | None = None
    report_path: str = ""
    candidate_dir: str = ""
    error_code: str | None = None
    reason: str = ""

    @property
    def pending_human(self) -> bool:
        return bool(self.gate_state is not None and self.gate_state.status == "pending")

    @property
    def harness_version(self) -> int:
        if self.accepted is not None:
            return int(self.accepted.version)
        if self.parent is not None:
            return int(self.parent.version)
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {"generation": int(self.generation), "decision": self.decision,
                "harness_version": self.harness_version, "pending_human": self.pending_human,
                "report": self.report_path, "candidate_dir": self.candidate_dir,
                "error_code": self.error_code, "reason": self.reason,
                "n_candidates": len(self.reports)}


# =====================================================================
# 主入口
# =====================================================================

def _resolve_harness_path(harness: Path | str | None, run_ctx: Any) -> Path:
    """``harness=`` 显式优先 → ``run_ctx.harness_dir`` → ``paths.harness_dir()``。

    F32（2026-10-09 实跑登记）：这里**不能**写成
    ``getattr(run_ctx, "harness_dir", default_harness_dir())`` —— Python 会先**无条件
    求值**默认参数，而 ``default_harness_dir`` 这个名字在本模块里从不存在（它是
    ``proposer.py`` 顶部 ``from .paths import harness_dir as default_harness_dir`` 的
    别名；本行显然是从那里抄过来却漏了导入）⇒ 即便 ``run_ctx`` 有 ``harness_dir``
    也会 ``NameError``。生产装配（``app._evolve_fn_provider`` →
    ``run_one_generation(state=…, run_ctx=…, runner=…)``，即 ``harness=None``）必然走
    这一支，而单测都显式传 ``harness=`` ⇒ 1800+ 用例全绿掩护了一条**完全死掉的闭环
    路径**，直到 §3.2 实跑才炸（崩在该代做任何事之前，白烧 ¥34.62 / 92 min）。
    """
    if harness is not None:
        return Path(harness)
    value = getattr(run_ctx, "harness_dir", None)
    return Path(value) if value is not None else harness_dir()


def run_one_generation(*, state: Any, run_ctx: Any, runner: Any = None,
                       config: Any = None, memory: Any = None,
                       evaluator: Any = None, client: Any = None,
                       harness: Path | str | None = None,
                       run_dir: Path | str | None = None,
                       seed: int | None = None, now: Any = None) -> Any:
    """跑一代演化并返回 ``EvolveOutcome``（MOD-06 的 ``evolve_fn`` 实现）。

    所有外部依赖都可注入（NFR-07-6）：``memory`` / ``client`` / ``evaluator`` /
    ``harness`` / ``run_dir`` / ``seed``。默认值取自 ``run_ctx``。
    """
    app_cfg = config if config is not None else getattr(run_ctx, "app_config", None)
    evol_cfg = getattr(app_cfg, "evolve", app_cfg)
    gen = int(getattr(state, "generation", 0) or 0) + 1
    run_id = str(getattr(run_ctx, "run_id", ""))
    root = (Path(run_dir) if run_dir is not None
            else Path(getattr(run_ctx, "paths").root))
    # F32：见 ``_resolve_harness_path`` 的注释——默认值必须延迟求值。
    harness_path = _resolve_harness_path(harness, run_ctx)
    memory = memory if memory is not None else getattr(run_ctx, "memory", None)
    protocol = getattr(run_ctx, "protocol", None)
    memory_version = int(getattr(run_ctx, "memory_version", 0) or 0)
    episode_config = getattr(run_ctx, "episode_config", None)
    started = time.perf_counter()

    # 用真实的 run 根（而不是按 runs_root/run_id 反推）：MOD-09 允许显式 run-id 与
    # 自定义 runs_root，反推在多一层目录时会漂移。
    ctx = EvolveContext(run_id=run_id, root=root, harness=harness_path,
                        runs_root=root.parent)
    try:
        result = _run_generation(
            state=state, run_ctx=run_ctx, runner=runner, cfg=evol_cfg,
            app_cfg=app_cfg, gen=gen, run_id=run_id, ctx=ctx,
            memory=memory, evaluator=evaluator, client=client,
            protocol=protocol, memory_version=memory_version,
            episode_config=episode_config, seed=seed, now=now,
            elapsed=lambda: time.perf_counter() - started)
    except HoldoutLeakError:
        raise
    except VersionConflictError:
        raise
    except InfraError as exc:
        _emit_cost(state, evol_cfg, spent=0)
        return _outcome_from_error(state=state, gen=gen, harness=harness_path,
                                   run_root=ctx.root, error_code=str(exc.code),
                                   reason=str(exc))
    _emit_cost(state, evol_cfg,
               spent=sum(int((r.heldout or {}).get("n", 0) or 0)
                         + int((r.smoke or {}).get("n", 0) or 0) for r in result.reports))
    return _as_outcome(result)


def _run_generation(*, state: Any, run_ctx: Any, runner: Any, cfg: Any, app_cfg: Any,
                    gen: int, run_id: str, ctx: EvolveContext, memory: Any,
                    evaluator: Any, client: Any, protocol: Any, memory_version: int,
                    episode_config: Any, seed: int | None, now: Any,
                    elapsed: Any) -> GenerationResult:
    from dice_ratchet.evals.seeds import take

    run_root, harness = ctx.root, ctx.harness

    # ---- 0) parent 与冻结记忆快照（FR-07-7：parent 与候选共用同一个 memory_version）--
    parent = versioning_mod.load_version(versioning_mod.current_version(harness),
                                         harness=harness)
    result = GenerationResult(generation=gen, parent=parent,
                              candidate_dir=str(ctx.gen(gen).candidates))

    # ---- 1) 证据束 + 候选生成（INV-5 硬闸在 propose 内）-----------------------------
    bundle = proposer_mod.build_evidence_bundle(
        run_id=run_id, n_traj=int(getattr(cfg, "n_traj", 20)), cfg=cfg, memory=memory,
        run_dir=run_root, harness=harness, generation=gen)
    rng_seed = propose_seed(run_id, gen, seed=seed)
    prop = proposer_mod.propose(bundle, cfg=cfg, rng_seed=rng_seed, client=client,
                                run_id=run_id, generation=gen, protocol=protocol,
                                runs_root=ctx.root.parent, app_config=app_cfg)
    if not prop.candidates:
        result.error_code = prop.error_code or str(E.E_EVOLVE_NO_CANDIDATE)
        result.reason = prop.reason or "no_candidate"
        _finish_report(result=result, run_ctx=run_ctx, cfg=cfg, ctx=ctx,
                       run_id=run_id, protocol=protocol, now=now)
        return result

    # ---- 2) 冒烟 seed：本代训练块末尾（FR-07-6）-------------------------------------
    smoke_n = int(getattr(cfg, "smoke_n", 2))
    smoke_seeds = tuple(take("train", generation=gen, protocol=protocol)[-max(1, smoke_n):])

    # ---- 3) parent 的 heldout + 冒烟（每代重算；NFR-07-5 成本）-----------------------
    ev = _build_evaluator(cfg=cfg, app_cfg=app_cfg, evaluator=evaluator, runner=runner,
                          protocol=protocol, memory_version=memory_version,
                          run_id=run_id, gen=gen, smoke_seeds=smoke_seeds,
                          episode_config=episode_config, seed=seed)
    if not result.pending_human:
        parent_heldout, parent_smoke = _parent_arms(
            ev, result, parent=parent, harness=harness)
    else: # pragma: no cover - 首轮不会走到
        parent_heldout, parent_smoke = None, None
    result.parent_heldout = parent_heldout

    # ---- 4) 逐候选：validate → scan → smoke → heldout → judge（FR-07-4/6/7/8/13）----
    winner: CandidateReport | None = None
    for cand in prop.candidates:
        report = _evaluate_one(cand=cand, cfg=cfg, ev=ev, parent=parent,
                               parent_heldout=parent_heldout, parent_smoke=parent_smoke,
                               smoke_seeds=smoke_seeds, run_id=run_id, gen=gen, ctx=ctx)
        result.reports.append(report)
        _write_candidate(result.reports[-1], run_root=run_root, gen=gen)
        if report.verdict is not None and report.verdict.decision == "accept":
            if winner is None or judge_mod.better(report, winner):
                winner = report
    result.winner = winner

    # ---- 5) 落选者的 superseded 判词（§8.2：不计失败、无错误码）---------------------
    if winner is not None:
        for index, report in enumerate(result.reports):
            if report is winner or report.verdict is None:
                continue
            if report.verdict.decision == "accept":
                result.reports[index] = _replace_verdict(
                    report, judge_mod.superseded_verdict(report, winner, generation=gen))

    # ---- 6) HITL / 自动裁决 → accept / rollback -------------------------------------
    _finish_report(result=result, run_ctx=run_ctx, cfg=cfg, ctx=ctx, run_id=run_id,
                   protocol=protocol, now=now)
    return result


def _build_evaluator(*, cfg: Any, app_cfg: Any, evaluator: Any, runner: Any,
                     protocol: Any, memory_version: int, run_id: str, gen: int,
                     smoke_seeds: Sequence[int], episode_config: Any,
                     seed: int | None = None) -> Any:
    if evaluator is not None:
        return evaluator
    return judge_mod.CandidateEvaluator(
        protocol=protocol, config=app_cfg, memory_version=int(memory_version),
        run_id=run_id, generation=int(gen), smoke_seeds=tuple(smoke_seeds),
        runner=runner, episode_config=episode_config)


def _parent_arms(ev: Any, result: GenerationResult, *, parent: HarnessRef,
                 harness: Path) -> tuple[Any, Any]:
    """parent 的 heldout + 冒烟；MOD-08 异常 → 保守 reject（NFR-07-7）。"""
    parent_dir = getattr(parent, "dir", None) or harness
    try:
        return ev.parent_arms(parent_dir, parent_version=int(parent.version))
    except Exception as exc: # noqa: BLE001 - 评审链路整体不可用 → 空报告
        events.emit("evolve.evaluate", candidate_id="parent",
                    error_code=str(getattr(exc, "code", E.E_EVOLVE_EVAL_FAILED)))
        return None, None


def _evaluate_one(*, cand: Candidate, cfg: Any, ev: Any, parent: HarnessRef,
                  parent_heldout: Any, parent_smoke: Any, smoke_seeds: Sequence[int],
                  run_id: str, gen: int, ctx: EvolveContext) -> CandidateReport:
    """单候选的完整链路（FR-07-4 → FR-07-13 → FR-07-6 → FR-07-7 → FR-07-8）。"""
    run_root, harness = ctx.root, ctx.harness
    # ---- ① 改动面硬约束（纯函数，先于任何落盘）-------------------------------------
    validation = proposer_mod.validate_candidate(cand, cfg=cfg)
    events.emit("evolve.validate", candidate_id=cand.candidate_id, status=validation.status,
                violations=list(validation.violations), diff_lines=validation.diff_lines,
                touched_files=list(validation.touched_files))
    if not validation.ok:
        return CandidateReport(candidate=cand, error_code=str(E.E_EVOLVE_PATCH_INVALID),
                               reason=validation.reason)

    # ---- ② staging（落盘 + 应用 diff）----------------------------------------------
    try:
        staging = versioning_mod.stage_candidate(cand, run_id=run_id, cfg=cfg,
                                                 harness=harness, generation=gen)
    except Exception as exc: # noqa: BLE001 - diff 不可应用：该候选 reject
        return CandidateReport(candidate=cand, error_code=str(E.E_EVOLVE_PATCH_INVALID),
                               reason=f"{E.E_EVOLVE_PATCH_INVALID}:{exc}")
    versioning_mod.write_candidate_patch(cand, run_root=run_root, gen=gen)

    # ---- ③ MOD-02 静态扫描（FR-07-13；失败 0 局 heldout 消耗）----------------------
    try:
        gate_report = guard_gate(staging, run_dir=run_root)
    except ScanFailedError as exc:
        return CandidateReport(candidate=cand, error_code=str(E.E_EVOLVE_SCAN_FAILED),
                               reason=f"{E.E_EVOLVE_SCAN_FAILED}:{exc}")
    status = _gate_status(gate_report)
    violations = _gate_violations(gate_report)
    events.emit("evolve.scan", candidate_id=cand.candidate_id, status=status,
                ast_digest=str(getattr(gate_report, "ast_digest", "")),
                third_party_imports=sorted({str(v.get("code", "")) for v in violations
                                            if "IMPORT" in str(v.get("code", "")).upper()
                                            or "import" in str(v.get("detail", ""))}),
                disallowed_calls=sorted({str(v.get("code", "")) for v in violations
                                         if "CALL" in str(v.get("code", "")).upper()
                                         or "call" in str(v.get("detail", ""))}))
    if status != "pass":
        report = CandidateReport(candidate=cand,
                                 error_code=str(E.E_EVOLVE_SCAN_FAILED),
                                 reason=f"{E.E_EVOLVE_SCAN_FAILED}:{_first_violation(violations)}")
        return report

    # ---- ④ 受限加载（FR-07-13：MOD-02 在子进程 worker 中加载）----------------------
    try:
        worker = load_harness_tools_checked(staging)
        close = getattr(worker, "close", None)
        if callable(close):
            close()
    except LoadFailedError as exc:
        return CandidateReport(candidate=cand, error_code=str(E.E_EVOLVE_LOAD_FAILED),
                               reason=f"{E.E_EVOLVE_LOAD_FAILED}:{exc}")

    # ---- ⑤ 冒烟 → heldout → 配对 → 判定表 -----------------------------------------
    report = judge_mod.evaluate_candidate(
        cand, parent, cfg=cfg, staging_dir=staging, evaluator=ev,
        parent_heldout=parent_heldout, parent_smoke=parent_smoke,
        harness_version=int(getattr(parent, "version", 0) or 0), generation=gen,
        smoke_seeds=smoke_seeds)
    if report.error_code == str(E.E_EVOLVE_LOAD_FAILED): # pragma: no cover - 防御
        return report
    return report


def _first_violation(violations: Sequence[Mapping[str, Any]]) -> str:
    """门禁第一条发现的**可读**摘要（优先 ``detail``，退回 ``code``）。"""
    if not violations:
        return "unknown"
    item = violations[0]
    return str(item.get("detail") or item.get("code") or item)


def _replace_verdict(report: CandidateReport, verdict: Verdict) -> CandidateReport:
    return CandidateReport(candidate=report.candidate, smoke=report.smoke,
                           heldout=report.heldout, paired=report.paired,
                           train=report.train, diversity=report.diversity,
                           verdict=verdict, elapsed_s=report.elapsed_s,
                           error_code=verdict.error_code, reason=verdict.reason)


# =====================================================================
# 收尾：gate → accept/rollback → report.json（FR-07-11/14/15）
# =====================================================================

def _finish_report(*, result: GenerationResult, run_ctx: Any, cfg: Any,
                   ctx: EvolveContext, run_id: str,
                   protocol: Any, now: Any = None) -> None:
    gen = int(result.generation)
    paths = ctx.gen(gen).ensure()
    run_root, harness = ctx.root, ctx.harness
    parent = result.parent
    parent_version = int(getattr(parent, "version", 0) or 0)

    # ---- 0) 终态候选明细（superseded 判词在上面的收尾阶段才落定）-------------------
    for report in result.reports:
        _write_candidate(report, run_root=run_root, gen=gen)

    # ---- 1) HITL 门（只在有 winner 时才挂起）---------------------------------------
    gate_state: GateState | None = None
    if result.winner is not None:
        diff_path = paths.patch(result.winner.candidate_id)
        gate_state = gate_mod.await_human(
            generation=gen, run_id=run_id, diff_path=diff_path,
            report_path=paths.report, cfg=cfg, run_dir=run_root,
            winner=result.winner.candidate, parent_version=parent_version,
            harness_version=parent_version, now=now)
        gate_state = gate_mod.expire_if_needed(gate_state, run_dir=run_root)
        result.gate_state = gate_state
        if gate_state.status == "approved":
            try:
                accepted = versioning_mod.accept(
                    result.winner.candidate,
                    Verdict(decision="accept", decided_by=gate_state.resolved_by or "auto",
                            reason=gate_state.reason, candidate_id=result.winner.candidate_id,
                            generation=gen),
                    run_id=run_id, cfg=ctx, harness=harness, generation=gen,
                    decided_by=gate_state.resolved_by or None, now=now)
                result.accepted = accepted
                result.decision = "accept"
            except InfraError as exc:
                result.error_code = str(exc.code)
                result.reason = str(exc)
                result.decision = "reject"
        elif gate_state.status in ("rejected", "timeout"):
            reason = gate_state.reason or ("hitl_timeout" if gate_state.status == "timeout"
                                           else "human_rollback")
            try:
                versioning_mod.rollback(parent_version, run_id=run_id, reason=reason,
                                        decided_by=gate_state.resolved_by or "human",
                                        cfg=ctx, harness=harness, generation=gen,
                                        now=now)
            except InfraError as exc: # pragma: no cover - 回滚失败：保守记 reject
                result.error_code = str(exc.code)
                result.reason = str(exc)
            result.decision = "rollback"
        else:
            result.decision = "pending"

    # ---- 2) 指标段（MOD-08）+ 决策段（MOD-07）--------------------------------------
    report = _build_report(result=result, app_cfg=ctx_app_config(run_ctx, cfg),
                           cfg=cfg, run_id=run_id, run_root=run_root,
                           protocol=protocol, gen=gen, parent_version=parent_version)
    try:
        from dice_ratchet.evals import write_generation_report

        path, _sha = write_generation_report(run_root, gen, report)
        result.report_path = str(path)
    except InfraError as exc:
        # MOD-08 报告段不可用：降级为 MOD-07 自产的最小合法报告（不静默丢弃）
        events.emit("evolve.decision", gen=gen, candidate_id="", decision="reject",
                    decided_by="auto", reason=str(exc.code))
        result.report_path = _write_minimal_report(result=result, run_root=run_root, gen=gen,
                                                   report=report)
    events.emit("evolve.decision", gen=gen,
                candidate_id=(result.winner.candidate_id if result.winner else ""),
                decision=str(result.decision), decided_by=str(
                    (result.gate_state.resolved_by if result.gate_state else "auto") or "auto"),
                reason=str(result.reason)[:200], error_code=result.error_code)


def ctx_app_config(run_ctx: Any, cfg: Any) -> Any:
    """取回 ``AppConfig``（MOD-08 ``evaluate_generation`` 需要 ``evals.*`` 段）。"""
    app_cfg = getattr(run_ctx, "app_config", None)
    if app_cfg is not None and hasattr(app_cfg, "evals"):
        return app_cfg
    return cfg


def _build_report(*, result: GenerationResult, app_cfg: Any, cfg: Any, run_id: str,
                  run_root: Path, protocol: Any, gen: int,
                  parent_version: int) -> dict[str, Any]:
    """MOD-08 指标段 + MOD-07 决策段（§6.3：本模块只填四段）。"""
    parent = result.parent
    metrics: dict[str, Any] = {}
    if result.parent_heldout is not None:
        try:
            from dice_ratchet.evals import evaluate_generation

            winner_arm = None
            if result.winner is not None and result.winner.heldout is not None:
                winner_arm = _arm_like(result.winner, harness_version=parent_version)
            metrics = dict(evaluate_generation(
                gen, result.parent_heldout, winner_arm, run_id=run_id, run_dir=run_root,
                config=app_cfg, protocol=protocol))
        except Exception as exc: # noqa: BLE001 - 指标段失败：用最小骨架（保守 reject）
            events.emit("evolve.decision", gen=gen, candidate_id="", decision="reject",
                        decided_by="auto", reason=f"metrics_unavailable:{type(exc).__name__}")
            metrics = {}
    sections = versioning_mod.build_decision_sections(gen, result.reports, result.winner,
                                                      result.gate_state)
    if metrics:
        report = {**metrics, **sections}
    else:
        report = _skeleton_report(result=result, gen=gen, parent_version=parent_version,
                                  sections=sections)
    # 空候选/全淘汰也必须带错误码（FR-07-15）
    if result.error_code and not report.get("reason"):
        report["reason"] = str(result.error_code)
    return report


def _skeleton_report(*, result: GenerationResult, gen: int, parent_version: int,
                     sections: Mapping[str, Any]) -> dict[str, Any]:
    """MOD-08 指标段不可用时的最小合法 report（全部缺失字段用 ``null``；FR-07-14）。"""
    zero = {"basket": "H1", "n": 0, "n_dropped": 0, "low_confidence": True,
            "win_rate": 0.0, "win_rate_ci95": [0.0, 0.0], "mean_stages_cleared": 0.0,
            "mean_hearts_left": 0.0, "mean_battles_won": 0.0, "mean_decisions": 0.0,
            "illegal_rate": 0.0, "fallback_rate": 0.0, "truncation_rate": 0.0,
            "degraded_n": 0, "action_entropy_curve": []}
    return {"generation": int(gen),
            "harness_version": int(result.harness_version or parent_version),
            "parent_version": int(parent_version), "episode_config_hash": None,
            "heldout": dict(zero), "train": {"window": 0, "win_rate": 0.0, "n": 0},
            "gain_realism": {"win_rate_gap": 0.0,
                             "gap_threshold": float(getattr(result, "gap_threshold", 0.15)),
                             "verdict": "insufficient", "collapse_veto": False,
                             "mcnemar_p": None, "bootstrap_gap_ci95": [0.0, 0.0]},
            "diversity": {"entropy_slope": 0.0, "collapse_flag": False, "jsd_kind": None},
            "ablation": None, "power_note": "",
            **dict(sections)}


def _arm_like(report: CandidateReport, *, harness_version: int) -> Any:
    """把候选报告的最小面包装成 ``evaluate_generation`` 需要的 ``ArmResult`` 形状。"""
    from types import SimpleNamespace

    heldout = dict(report.heldout or {})
    metrics = SimpleNamespace(**{k: v for k, v in heldout.items()
                                 if k not in ("arm", "seeds", "harness_version",
                                              "memory_version")},
                             as_dict=lambda: dict(heldout))
    return SimpleNamespace(metrics=metrics, harness_version=int(harness_version),
                           results=(), seeds=tuple(heldout.get("seeds") or ()))


def _write_minimal_report(*, result: GenerationResult, run_root: Path, gen: int,
                          report: Mapping[str, Any]) -> str:
    """schema 校验失败时的兜底落盘（``report.invalid.json`` 语义之外的保底）。"""
    path = GenPaths.of(run_root, gen).report
    write_json_atomic(path, dict(report))
    return str(path)


# =====================================================================
# 候选明细落盘（FR-07-12）
# =====================================================================

def _write_candidate(report: CandidateReport, *, run_root: Path, gen: int) -> Path:
    path = GenPaths.of(run_root, gen).candidate(report.candidate_id)
    write_candidate_report(path, report)
    return path


# =====================================================================
# EvolveOutcome 桥接与错误路径
# =====================================================================

def _as_outcome(result: GenerationResult) -> Any:
    from dice_ratchet.loop.generation import EvolveOutcome

    return EvolveOutcome(
        decision=str(result.decision), generation=int(result.generation),
        harness_version=int(result.harness_version),
        pending_human=bool(result.pending_human), candidate_dir=str(result.candidate_dir),
        report=str(result.report_path), reason=str(result.reason),
        error_code=result.error_code)


def _outcome_from_error(*, state: Any, gen: int, harness: Path, run_root: Path,
                        error_code: str, reason: str) -> Any:
    from dice_ratchet.loop.generation import EvolveOutcome

    return EvolveOutcome(decision="error_reject", generation=int(gen),
                         harness_version=int(versioning_mod.current_version(harness)),
                         pending_human=False,
                         candidate_dir=str(GenPaths.of(run_root, gen).candidates),
                         report="", reason=str(reason), error_code=str(error_code))


def _emit_cost(state: Any, cfg: Any, *, spent: int) -> None:
    cap = int(getattr(getattr(state, "budget", None), "eval_episodes_used", 0) or 0)
    events.emit("evolve.cost", episodes_spent=int(spent), episodes_cap=int(cap),
                candidates_evaluated=int(getattr(cfg, "n_candidates", 0) or 0))


def report_metrics_builder(*, app_cfg: Any, run_id: str, run_root: Path,
                           protocol: Any) -> Any: # pragma: no cover - 供外部编排复用
    """返回一个 ``(gen, parent, winner) -> dict`` 的指标段构造器（MOD-08 门面）。"""
    from dice_ratchet.evals import evaluate_generation

    def build(gen: int, parent: Any, winner: Any) -> dict[str, Any]:
        return dict(evaluate_generation(gen, parent, winner, run_id=run_id,
                                        run_dir=run_root, config=app_cfg,
                                        protocol=protocol))

    return build
