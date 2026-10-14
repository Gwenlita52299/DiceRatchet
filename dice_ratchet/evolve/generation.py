"""MOD-07 单代编排（PRD §8.1；FR-07-14/15）。

``run_one_generation(state, run_ctx, runner)`` 是 MOD-06 ``evolve_fn`` 的实现，也是
PRD §8.1 伪代码的落地：**一次调用 = 一个演化周期**。

时序（与 §8.1 逐条对应）::

    parent  = versioning.load_version(current_version())
    bundle  = build_evidence_bundle(...)            # FR-07-2/3（INV-5 硬闸在 propose 内）
    prop    = propose(bundle, cfg, rng_seed)        # FR-07-1
    smoke   = seeds.take("train", generation=g)[-smoke_n:]   # FR-07-6
    parent_ho / parent_smoke = evaluate_arm(...)    # FR-07-7：parent 每代重算
                                                    #   （上一代 accept 过则**继承**测量）
    for cand in order_candidates(prop.candidates):
        staging = stage_candidate(...)              # 落盘 + 应用 diff
        validate_candidate → guard.gate → smoke → heldout → judge
    finish(): gate.await_human → accept/rollback → report.json

**绝不静默退化**（FR-07-15）：空候选、全淘汰、扫描失败都落 ``report.json``
（``decision="reject"`` + 对应错误码）。

**F57（2026-10-12）——parent 的测量跨代继承**：上一代**被接受**的候选就是本代的
parent（``accept()`` 写 ``harness/versions/v{N}/`` 并移动 ``version.json`` 指针，本模块
第 0 步的 ``load_version(current_version())`` 因此自动取到它），而它**已经**被完整测过
一次——那批局正是 accept 决策的依据。再测一遍等于为同一份 harness 再买一次同样的局
（≈20 局 / ≈¥17/代），且在「API 臂不可复现」的前提下还会得到**略微不同**的数字
（实测同一 harness 同一批 seed 两次测量：`0.15` vs `0.10`，5pp 就是本 run 的噪声底）。
继承时本代**不新增 heldout 台账行**（沿用 F48 的先例：没有真实测量的代不写行），
只在 ``report.json`` 的 ``heldout.source`` 记 ``"inherited:gen-00X"``。
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
from . import checkpoint as checkpoint_mod
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
    #: F57（2026-10-12）：本代 ``heldout`` 段的来源。``"measured"`` = 本代真测；
    #: ``"inherited:gen-00X"`` = 跨代继承第 X 代同一臂的测量（**0 次 evaluate_arm**）。
    heldout_source: str = "measured"
    #: D-M3-7（2026-10-14）：本代**起点** harness 的 play 面指纹（accept 会改写在役工件，
    #: 所以必须在**任何写入之前**取好）。
    parent_play_hash: str = ""
    #: accept 之后重新算的在役 play 面指纹（未 accept / 算不出 → 空）。
    accepted_play_hash: str = ""
    #: D2=A（2026-10-14）：拒绝时**轻量推进**后的版本号（``v_N+1``，无快照、无 tag）。
    #: 非 ``None`` 时它就是本代「结束时在役」的版本号（``harness_version``）。
    retained_version: int | None = None
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
    def retained_play_hash(self) -> str:
        """**结束时在役** harness 的 play 面指纹（accept 后用新指纹，否则仍是起点的）。"""
        return str(self.accepted_play_hash or self.parent_play_hash)

    @property
    def play_plane_changed(self) -> bool:
        """本代接受的补丁是否**真的改变了打法**（F66 的第二半）。

        ``False`` 且 ``accepted`` 非空 = 只改了 ``prompts/reflection.md`` 之类的**学习侧**
        工件：在役 play 面未变 ⇒ 那份 heldout 测量**没有新东西可测**，台账沿用继承来的数。
        """
        if not self.accepted_play_hash or not self.parent_play_hash:
            return False
        return self.accepted_play_hash != self.parent_play_hash

    @property
    def harness_version(self) -> int:
        if self.retained_version is not None:
            return int(self.retained_version)
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
    # F42-B：提案在**评估之前**落检查点，resume 时指纹一致则复用同一 candidate id 与 diff。
    # 理由：API 提案不可复现（`api_supports_seed=false`），不落检查点则候选臂归档必然 miss
    # （归档路径含 `cand-<id>`），中断后只能省 parent 侧。
    #
    # D-M3-7（2026-10-14）：**先**取起点 play 面指纹——accept 会改写 `harness/` 在役工件，
    # 之后再也算不出「起点长什么样」了。
    try:
        result.parent_play_hash = versioning_mod.play_hash(harness)
    except Exception as exc: # noqa: BLE001 - 指纹算不出不阻断本代（继承会退回重测）
        events.emit("evolve.play_hash", generation=int(gen), status="failed",
                    detail=f"{type(exc).__name__}: {exc}"[:200])
    bundle = proposer_mod.build_evidence_bundle(
        run_id=run_id, n_traj=int(getattr(cfg, "n_traj", 20)), cfg=cfg, memory=memory,
        run_dir=run_root, harness=harness, generation=gen)
    rng_seed = propose_seed(run_id, gen, seed=seed)
    inputs = proposer_mod.proposal_inputs(bundle, cfg)
    fingerprint = checkpoint_mod.proposal_fingerprint(
        run_id=run_id, generation=gen, propose_seed=rng_seed,
        harness_version=int(getattr(parent, "version", 0) or 0),
        memory_version=int(memory_version or 0), inputs=inputs)
    prop = checkpoint_mod.load_proposal_checkpoint(run_root, gen, fingerprint=fingerprint)
    if prop is None:
        prop = proposer_mod.propose(bundle, cfg=cfg, rng_seed=rng_seed, client=client,
                                    run_id=run_id, generation=gen, protocol=protocol,
                                    runs_root=ctx.root.parent, app_config=app_cfg,
                                    inputs=inputs)
        try: # 检查点是加速产物：落盘失败不得阻断本代
            checkpoint_mod.save_proposal_checkpoint(run_root, generation=gen,
                                                    fingerprint=fingerprint, proposal=prop,
                                                    inputs=inputs)
        except Exception as exc: # noqa: BLE001
            events.emit("evolve.proposal_checkpoint", gen=gen, status="failed",
                        detail=str(exc)[:200])
    if not prop.candidates:
        result.error_code = prop.error_code or str(E.E_EVOLVE_NO_CANDIDATE)
        result.reason = prop.reason or "no_candidate"
        _finish_report(result=result, run_ctx=run_ctx, cfg=cfg, ctx=ctx,
                       run_id=run_id, protocol=protocol, now=now)
        return result

    # ---- 2) 冒烟 seed：本代训练块末尾（FR-07-6）-------------------------------------
    smoke_n = int(getattr(cfg, "smoke_n", 2))
    smoke_seeds = tuple(take("train", generation=gen, protocol=protocol)[-max(1, smoke_n):])

    # ---- 3) parent 的 heldout（优先**继承**同 play 面的测量）+ 本代冒烟 ---------------
    # D-M3-7（2026-10-14 用户架构）：每代**只测候选**。起点的 heldout 只要 play 面没变
    # 就直接复用已有测量（0 次 evaluate_arm）——「这里的继承不需要重跑 heldout，直接复用
    # 数据即可」。找不到可复用测量时才退回实测（fail-open 到「花钱但正确」）。
    # 冒烟仍每代重测：它取的是**本代训练块末尾**的 seed，候选的冒烟也落在同一批 seed 上
    # （FR-07-7 的「同预算配对」前提），省它 ≈¥1.7 却会破坏快速淘汰的配对，不划算。
    ev = _build_evaluator(cfg=cfg, app_cfg=app_cfg, evaluator=evaluator, runner=runner,
                          protocol=protocol, memory_version=memory_version,
                          run_id=run_id, gen=gen, smoke_seeds=smoke_seeds,
                          episode_config=episode_config, seed=seed, run_dir=run_root)
    if not result.pending_human:
        inherited, source = _inherited_parent_heldout(ev, run_root=run_root, gen=gen,
                                                     parent=parent, harness=harness,
                                                     play_hash=str(result.parent_play_hash))
        result.heldout_source = source
        parent_heldout, parent_smoke = _parent_arms(
            ev, result, parent=parent, harness=harness, inherited=inherited)
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
                     seed: int | None = None, run_dir: Any = None) -> Any:
    if evaluator is not None:
        return evaluator
    # F54：held-out 篮子**必须**与 gen-000 基线同源（`evolve.heldout_basket`），否则
    # parent 与 candidate 的对比会跨越两套 seed（C0 的「同预算」前提直接失效）。缺配置时
    # 不传该字段，回落到 `CandidateEvaluator` 的默认（H1）——不静默换成别的篮子。
    heldout_basket = getattr(getattr(app_cfg, "evolve", None), "heldout_basket", None)
    extra = {} if heldout_basket is None else {"heldout_basket": str(heldout_basket)}
    return judge_mod.CandidateEvaluator(
        protocol=protocol, config=app_cfg, memory_version=int(memory_version),
        run_id=run_id, generation=int(gen), smoke_seeds=tuple(smoke_seeds),
        runner=runner, episode_config=episode_config, run_dir=run_dir, **extra)


def _parent_arms(ev: Any, result: GenerationResult, *, parent: HarnessRef,
                 harness: Path, inherited: Any = None) -> tuple[Any, Any]:
    """parent 的 heldout + 冒烟；MOD-08 异常 → 保守 reject（NFR-07-7）。

    ``inherited``（F57）：上一代接受过的同一份 harness 的**已还原**测量。非 ``None``
    时 heldout 直接复用（0 次 ``evaluate_arm``）。

    F42-B：``KeyboardInterrupt`` / ``SystemExit`` **必须穿透**（``except Exception`` 本来
    就不会捕获它们，这里显式写明契约）。一代 84 局 ≈ 3.75 h，Ctrl-C 是最常见的中断；
    把它折成「保守 reject」会写出一个 ``report.json``，让 ``loop`` 把该代记为**已完成**，
    从而丢掉整代的已评估结果（正是 F42 要消灭的浪费）。
    """
    parent_dir = getattr(parent, "dir", None) or harness
    try:
        return ev.parent_arms(parent_dir, parent_version=int(parent.version),
                              inherited=inherited)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc: # noqa: BLE001 - 评审链路整体不可用 → 空报告
        events.emit("evolve.evaluate", candidate_id="parent",
                    error_code=str(getattr(exc, "code", E.E_EVOLVE_EVAL_FAILED)))
        return None, None


# =====================================================================
# parent 测量的跨代继承（F57 → D-M3-7）
# =====================================================================

def _inherited_parent_heldout(ev: Any, *, run_root: Path, gen: int, parent: HarnessRef,
                              harness: Path | str | None = None,
                              play_hash: str = "") -> tuple[Any, str]:
    """找**同一 play 面**的已有 heldout 测量，还原成 parent 的 ``ArmResult``。

    返回 ``(arm_or_None, source)``；``source`` 写进 ``report.json`` 的 ``heldout.source``：
    命中继承时 ``"inherited:gen-00X"``，否则 ``"measured"``（调用方据此实测）。

    **资格条件（全部满足才继承，任一不满足即重测）**：

    1. 某个更早代的 ``report.json`` 记录了**相同的** ``harness_play_hash``
       （D-M3-7：play 面逐字节相同才是「同一份起点」；版本号在拒绝时也推进，不可用）；
    2. 那个代**真的测过**它当时在役的 harness：
       ``gen-000`` 的基线臂，或某代 ``decision="accept"`` 的候选臂（reject / inherited
       的代没有新测量可继承，继续往前找）；
    3. 归档 ``metrics/arms/gen{prev:03d}-heldout-*-{arm}.json`` 能被
       ``restore_arm_from_archive`` 还原，且逐局 seed **覆盖本代 heldout 篮子**
       （篮子变了就重测，fail-closed）。

    **旧 run 的回落**（``report.json`` 里没有 ``harness_play_hash``）：沿用 F57 的判据
    ——「上一代 ``decision == accept`` 且 ``harness_version == parent.version``」。
    这样 2026-10-14 之前落盘的 run 仍按原口径工作，不会因为新字段缺失而一律重测。

    第 1 条扫的是**全部更早的代**（``gen-1 … 0``）：中断/续跑下「最近一个同 play 面且
    真有测量的代」才是正确的来源，不假定它就是 ``gen-1``。
    """
    from dice_ratchet.evals import harness as eval_harness

    parent_version = int(getattr(parent, "version", 0) or 0)
    arms_dir = eval_harness.arms_dir(run_root)
    current = str(play_hash or "")
    if not current and harness is not None:
        try:
            current = versioning_mod.play_hash(harness)
        except Exception: # noqa: BLE001 - 算不出指纹 ⇒ 不继承（退回实测）
            current = ""
    required_seeds = tuple(_heldout_seeds_of(ev))

    for prev in range(int(gen) - 1, -1, -1):
        report = _prior_report(run_root, prev)
        if report is None:
            continue
        recorded = str(report.get("harness_play_hash") or "")
        if recorded and current:
            if recorded != current:
                continue
        else:
            # 旧 run（无 play_hash 字段）：F57 的判据——同版本 + accept。
            if str(report.get("decision") or "") != "accept":
                continue
            if _int_or_none(report.get("harness_version")) != parent_version:
                continue
        archive_path = _measured_heldout_archive(arms_dir, prev, report)
        if archive_path is None:
            continue
        try:
            limits = (ev.eval_limits() if hasattr(ev, "eval_limits")
                      else {"t_max": 120, "min_support": 3, "drop_rate_max": 0.10})
            restored = eval_harness.restore_arm_from_archive(
                eval_harness.load_arm_archive(archive_path),
                generation=int(gen), harness_version=parent_version, arm="parent",
                required_seeds=required_seeds, **limits)
        except Exception as exc: # noqa: BLE001 - 继承失败必须重测，不得中断本代
            events.emit("evolve.parent_inherit", generation=int(gen), status="failed",
                        source=f"gen-{prev:03d}",
                        detail=f"{type(exc).__name__}: {exc}"[:200])
            continue
        if restored is None:
            continue
        events.emit("evolve.parent_inherit", generation=int(gen), status="ok",
                    source=f"gen-{prev:03d}", harness_version=parent_version,
                    play_hash=current[:19],
                    n=int(getattr(getattr(restored, "metrics", None), "n", 0) or 0))
        return restored, f"inherited:gen-{prev:03d}"
    return None, "measured"


def _measured_heldout_archive(arms_dir: Path, generation: int,
                              report: Mapping[str, Any]) -> Path | None:
    """该代**真实测得**在役 harness 的 heldout 臂归档；没有真实测量 → ``None``。

    - ``gen-000``：基线臂 ``gen000-heldout-*-parent.json``（``write_gen000_baseline`` 落的）；
    - ``gen ≥ 1``：只有 ``decision="accept"`` 的代才有新测量——那一代被接受的候选臂
      （``gen{NNN}-heldout-*-cand-{id}.json``）。reject / inherited 的**没有**，返回
      ``None`` 让调用方继续往前找。
    """
    if int(generation) == 0:
        matches = sorted(Path(arms_dir).glob("gen000-heldout-*-parent.json"))
        return matches[0] if matches else None
    if str(report.get("decision") or "") != "accept":
        return None
    for cand in _accepted_candidates(report):
        path = _heldout_archive_for(arms_dir, generation, str(cand.get("candidate_id") or ""))
        if path is not None:
            return path
    return None


def _int_or_none(value: Any) -> int | None:
    """字段 → ``int``；缺失/非数字 → ``None``（**不得**把 ``0`` 当缺失，见调用点注释）。"""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _heldout_seeds_of(ev: Any) -> Sequence[int]:
    """该代 held-out 篮子的 seed（继承资格检查用）；取不到时返回空 ⇒ **不继承**（fail-closed）。"""
    getter = getattr(ev, "heldout_seeds", None)
    if not callable(getter):
        return ()
    try:
        return tuple(int(s) for s in getter())
    except Exception: # noqa: BLE001 - 篮子解析失败 ⇒ 退回重测（绝不猜）
        return ()


def _prior_report(run_root: Path, generation: int) -> Mapping[str, Any] | None:
    """读某一代已落盘的 ``report.json``；缺失/损坏 → ``None``（按「无历史」处理）。"""
    from dice_ratchet.infra.io import read_json

    path = GenPaths.of(run_root, generation).report
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except InfraError: # pragma: no cover - 损坏报告按「推不出」处理
        return None
    return data if isinstance(data, Mapping) else None


def _accepted_candidates(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """报告里 ``decision="accept"`` 的候选（F57 继承的来源）；无则空列表。"""
    out: list[Mapping[str, Any]] = []
    for item in report.get("candidates") or ():
        if isinstance(item, Mapping) and str(item.get("decision") or "") == "accept":
            out.append(item)
    return out


def _heldout_archive_for(arms_dir: Path, generation: int, candidate_id: str) -> Path | None:
    """该代该候选的 heldout 臂归档路径（``gen{NNN}-heldout-{basket}-cand-{id}.json``）。

    文件名里的 ``basket`` 由**当年**的配置决定，可能与本代不同，所以这里通配
    ``gen{NNN}-heldout-*-cand-{id}.json``；basket 是否可用由
    ``restore_arm_from_archive`` 的 seed 覆盖检查兜住（fail-closed）。
    结果排序后取第一个，保证**可复现**（同一 run 内不会同时存在两个同名臂）。
    """
    if not candidate_id or not Path(arms_dir).is_dir():
        return None
    matches = sorted(Path(arms_dir).glob(
        f"gen{int(generation):03d}-heldout-*-cand-{candidate_id}.json"))
    return matches[0] if matches else None


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
                # D-M3-7：accept 已经改写 `harness/` 在役工件 ⇒ 重新算 play 面指纹，
                # 用它判断「这一代是否真的改变了打法」（F66 的第二半）。
                try:
                    result.accepted_play_hash = versioning_mod.play_hash(harness)
                except Exception as exc: # noqa: BLE001 - 算不出只影响台账口径，不阻断裁决
                    events.emit("evolve.play_hash", generation=gen, status="failed",
                                detail=f"{type(exc).__name__}: {exc}"[:200])
                if result.parent_play_hash and result.accepted_play_hash \
                        and result.accepted_play_hash == result.parent_play_hash:
                    events.emit("evolve.play_hash", generation=gen, status="unchanged",
                                play_hash=result.accepted_play_hash[:19],
                                detail="接受的补丁没有改变 play 面（只改了学习侧工件）")
            except InfraError as exc:
                # F47-D：**基础设施失败 ≠ 裁判拒绝**。原先这里写 `decision="reject"`，
                # 于是一次 `E_EVOLVE_GIT_FAILED`（如脏工作区）在报告与台账里长得跟
                # 「候选不显著、合法拒绝」一模一样，且退出码仍为 0 ⇒ 按 §3.2 判据算合格。
                # 沿用 ``loop._call_evolve`` 已确立的词汇：保守拒绝、不更新 harness。
                result.error_code = str(exc.code)
                result.reason = str(exc)
                result.decision = "error_reject"
        elif gate_state.status in ("rejected", "timeout"):
            reason = gate_state.reason or ("hitl_timeout" if gate_state.status == "timeout"
                                           else "human_rollback")
            try:
                versioning_mod.rollback(parent_version, run_id=run_id, reason=reason,
                                        decided_by=gate_state.resolved_by or "human",
                                        cfg=ctx, harness=harness, generation=gen,
                                        now=now)
                result.decision = "rollback"
            except InfraError as exc: # pragma: no cover - 回滚失败：保守记 error_reject
                result.error_code = str(exc.code)
                result.reason = str(exc)
                result.decision = "error_reject" # 回滚没做成：不得记成 "rollback"
        else:
            result.decision = "pending"

    # ---- 1.5) 拒绝：写死路记录 + **轻量推进版本号**（D1=A / D2=A，2026-10-14）--------
    # 顺序不可颠倒：死路记录 **先**落盘，`record_rejection` 的 commit 才把它带进
    # 版本血缘——下一代的提案方读到的就是它（用户的步骤 3：`v1 = v0 + 仅 P1 不可行的提示词`）。
    #
    # **只在真的评估过候选时推进**（``result.reports`` 非空且至少一条构成死路）：
    # 空候选 / 提案失败 / 候选在 propose 阶段即被丢弃，都属于「**没有补丁**」——没有死路
    # 可记，按 FR-07-15 的「空候选 → reject，不更新 harness」保持 harness 原样
    # （实测用例：``test_invalid_candidate_is_rejected_without_touching_harness``
    # 与 ``test_mod08_failure_conservatively_rejects`` 都逐字节比对 version.json）。
    if str(result.decision) == "reject" and any(_is_dead_end_report(r)
                                                for r in result.reports):
        _record_dead_ends(result=result, harness=harness, gen=gen)
        try:
            ref = versioning_mod.record_rejection(
                run_id=run_id, generation=gen, reason=_rejection_summary(result),
                cfg=ctx, harness=harness, now=now)
            result.retained_version = int(ref.version)
        except InfraError as exc:
            # 基础设施失败 ≠ 裁判拒绝（F47-D 同一条纪律）：版本没落成，就不能声称
            # 「v_{N+1} 已存在」。保守记 error_reject（不更新 harness）。
            result.error_code = str(exc.code)
            result.reason = str(exc)
            result.decision = "error_reject"

    # ---- 2) 指标段（MOD-08）+ 决策段（MOD-07）--------------------------------------
    report = _build_report(result=result, app_cfg=ctx_app_config(run_ctx, cfg),
                           cfg=cfg, run_id=run_id, run_root=run_root,
                           protocol=protocol, gen=gen, parent_version=parent_version)
    try:
        from dice_ratchet.evals import (enforce_report_determinism,
                                        write_generation_report)
        from dice_ratchet.evals.harness import ledger_and_plots

        app_cfg = ctx_app_config(run_ctx, cfg)
        # G3 的**生产执行点**（2026-10-11 D-M3-3 裁决 A，Spec v2.9 / B-65）。
        # 现场：`check_report_determinism` 从 M1 起没有任何生产调用点 ⇒「同参数重跑 sha256
        # 必须一致」这条承诺在真实 run 里从未被执行过。这里把它接上，但**按臂分口径**：
        # 臂内含 `provider=api` 的角色（当前主力臂 `C1`）时，端到端逐字节可复现在物理上
        # 不成立（`api_supports_seed=false`）⇒ 记 `api_nondeterministic` + 发
        # `eval.nondeterministic`，不抛 `E_EVAL_NONDETERMINISTIC`；本地臂仍硬执行。
        enforce_report_determinism(run_root, gen, report, app_cfg=app_cfg)
        path, _sha = write_generation_report(run_root, gen, report)
        result.report_path = str(path)
        # F43：本代行由**写报告的一方**追写 `metrics/generations.csv`（B-06 固定列）。
        # 原先只有 `eval report` 会写它，而那条路径会把重算结果写进**同一个**
        # `generations/gen-NNN/report.json`（decision 变 "pending"）⇒ 判据②（csv 可复算）
        # 只能靠破坏判据①（本模块刚写下的决策记录）来满足。
        # 台账是附属产物：失败**不阻塞**裁决落盘，但必须可观测（`evolve.ledger`）。
        # F-M3-3（2026-10-11）：改为写**全部** split 行（heldout + train）。原先只写 heldout
        # ⇒ `generations.csv` 里 train 一侧永远缺席（实测 8 个归档 run 的 train 行数全为 0），
        # 而 `train_heldout_gap.png` 与 `win_rate.png` 的 train 对照曲线都要它。
        # F56（2026-10-12）：台账与绘图收敛到 `harness.ledger_and_plots` —— 两处各自
        # 写一半正是「`loop run` 的 run 没有 5 张 PNG」的成因（绘图原先只挂在 `eval report`）。
        ledger_and_plots(run_root, gen, report, cfg=app_cfg)
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


#: 「补丁本身不行」的否决码（D2=A 的轻量版本推进只认这些 + 判定表拒绝）。
#:
#: **刻意不含** ``E_EVAL_*`` / ``E_EVOLVE_JUDGE_FAILED`` / ``E_EVOLVE_EVAL_FAILED``：
#: 那些是**基础设施**故障（MOD-08 超时、裁判链路异常），不是「这条路走不通」。
#: 把它们记成死路会让下一代的提案方读到「某某改法被否」而其实什么都没测出来；
#: 推进版本号更会伪造一条不存在的演化历史（F47-D 同一条纪律：基础设施失败 ≠ 裁判拒绝）。
_PATCH_LEVEL_REJECTION_CODES: frozenset[str] = frozenset({
    str(E.E_EVOLVE_PATCH_INVALID), str(E.E_EVOLVE_SCAN_FAILED),
    str(E.E_EVOLVE_LOAD_FAILED), str(E.E_EVOLVE_SMOKE_FAILED),
    str(E.E_EVOLVE_NO_GAIN), str(E.E_EVOLVE_OVERFIT_SUSPECT),
    str(E.E_EVOLVE_COLLAPSE),
})


def _is_dead_end_report(report: CandidateReport) -> bool:
    """这条候选报告是否构成一条**死路**（值得推进版本号 + 写给下一代看）。"""
    if report.verdict is not None and report.verdict.decision == "accept":
        return False
    if report.verdict is not None:      # 判定表拒绝：C0…C5 里挂在哪一条都算
        return True
    return str(report.error_code or "") in _PATCH_LEVEL_REJECTION_CODES


def _record_dead_ends(*, result: GenerationResult, harness: Path, gen: int) -> None:
    """把本代**每一条被否决的候选**写进 ``harness/dead_ends.md``（D1=A）。

    覆盖补丁级否决面（都值得让下一代的提案方知道）：判定表拒绝（``C0…C5``）、
    patch 无效 / 扫描失败 / 受限加载失败 / 冒烟快速淘汰。**基础设施故障不算死路**
    （见 ``_PATCH_LEVEL_REJECTION_CODES``）。写失败**不阻断**裁决（死路记录是提案质量的
    辅助，不是判据产物），但必须发事件（``evolve.dead_end`` 的失败态）。
    """
    for report in result.reports:
        if not _is_dead_end_report(report):
            continue
        reason = (report.verdict.reason if report.verdict is not None else "") \
            or report.reason or str(report.error_code or "rejected")
        metrics = {"paired": report.paired, "heldout": report.heldout}
        try:
            versioning_mod.write_dead_end(harness, generation=gen, candidate=report.candidate,
                                          reason=reason, error_code=str(report.error_code or ""),
                                          metrics=metrics)
        except Exception as exc: # noqa: BLE001 - 辅助产物失败不阻塞裁决
            events.emit("evolve.dead_end", generation=gen,
                        candidate_id=report.candidate_id, status="failed",
                        detail=f"{type(exc).__name__}: {exc}"[:200])


def _rejection_summary(result: GenerationResult) -> str:
    """``version.json`` 的 ``reason``：``rejected:<candidate>:<首个失败条件>``。"""
    winner_report = result.reports[0] if result.reports else None
    for report in result.reports:
        if report.verdict is not None:
            winner_report = report
            break
    if winner_report is None:
        return "rejected:no_candidate"
    reason = (winner_report.verdict.reason if winner_report.verdict is not None else "") \
        or winner_report.reason or str(winner_report.error_code or "rejected")
    return f"rejected:{winner_report.candidate_id}:{' '.join(str(reason).split())[:160]}"


def ctx_app_config(run_ctx: Any, cfg: Any) -> Any:
    """取回 ``AppConfig``（MOD-08 ``evaluate_generation`` 需要 ``evals.*`` 段）。"""
    app_cfg = getattr(run_ctx, "app_config", None)
    if app_cfg is not None and hasattr(app_cfg, "evals"):
        return app_cfg
    return cfg


def _build_report(*, result: GenerationResult, app_cfg: Any, cfg: Any, run_id: str,
                  run_root: Path, protocol: Any, gen: int,
                  parent_version: int) -> dict[str, Any]:
    """MOD-08 指标段 + MOD-07 决策段（§6.3：本模块只填四段）。

    **``heldout`` 段取谁**（D-M3-7，2026-10-14 用户架构）：取**本代结束时在役**那份
    harness 的测量——被接受 ⇒ 取那个候选自己的臂（``retained_arm``）；被拒 ⇒ 起点那份
    （``result.parent_heldout``，可能是继承来的）。这样「接受了一代」才在台账里留下新数字，
    判据 1 的「held-out 单调不降」才有可比较的两个起点的数。
    """
    parent = result.parent
    metrics: dict[str, Any] = {}
    if result.parent_heldout is not None:
        try:
            from dice_ratchet.evals import evaluate_generation

            play_plane_changed = bool(
                result.accepted is not None
                and result.accepted_play_hash
                and result.parent_play_hash
                and result.accepted_play_hash != result.parent_play_hash)
            source = str(result.heldout_source or "measured")
            retained_arm = None
            if play_plane_changed and result.winner is not None \
                    and result.winner.heldout is not None:
                # 本代真测了**新** play 面 ⇒ 台账里的这一行就是它的测量。
                retained_arm = _arm_like(result.winner,
                                         harness_version=int(result.accepted.version))
                source = "measured"
            winner_arm = None
            if result.winner is not None and result.winner.heldout is not None:
                winner_arm = _arm_like(result.winner, harness_version=parent_version)
            metrics = dict(evaluate_generation(
                gen, result.parent_heldout, winner_arm, run_id=run_id, run_dir=run_root,
                config=app_cfg, protocol=protocol,
                heldout_source=source,
                retained_arm=retained_arm,
                retained_version=int(result.harness_version),
                play_hash=str(result.retained_play_hash or result.parent_play_hash)))
        except Exception as exc: # noqa: BLE001 - 指标段失败：用最小骨架（保守 reject）
            events.emit("evolve.decision", gen=gen, candidate_id="", decision="reject",
                        decided_by="auto", reason=f"metrics_unavailable:{type(exc).__name__}")
            metrics = {}
    sections = versioning_mod.build_decision_sections(gen, result.reports, result.winner,
                                                      result.gate_state)
    # F47-D：``build_decision_sections`` 只看 gate 状态，而 accept/rollback **失败**时
    # gate 仍是 "approved"/"rejected" ⇒ 它会写出 ``decision="accept"`` 而磁盘上
    # ``harness/versions/`` 里没有新版本（**幻影 accept**），与 ``result.decision``
    # （``error_reject``）以及 ``evolve.decision`` 事件自相矛盾。以 result 为准。
    if str(result.decision) == "error_reject":
        sections["decision"] = "error_reject"
        sections["reason"] = str(result.reason or result.error_code or "")
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
    zero = {"basket": "H1", "source": "unavailable", "n": 0, "n_dropped": 0,
            "low_confidence": True,
            "win_rate": 0.0, "win_rate_ci95": [0.0, 0.0], "mean_stages_cleared": 0.0,
            "mean_hearts_left": 0.0, "mean_battles_won": 0.0, "mean_decisions": 0.0,
            "illegal_rate": 0.0, "fallback_rate": 0.0, "truncation_rate": 0.0,
            "degraded_n": 0, "action_entropy_curve": []}
    return {"generation": int(gen),
            "harness_version": int(result.harness_version or parent_version),
            "parent_version": int(parent_version),
            "harness_play_hash": str(result.retained_play_hash or ""),
            "episode_config_hash": None,
            "heldout": dict(zero), "train": {"window": 0, "win_rate": 0.0, "n": 0,
                                             "n_dropped": 0, "low_confidence": True,
                                             "source": "unavailable"},
            "gain_realism": {"win_rate_gap": 0.0,
                             "gap_threshold": float(getattr(result, "gap_threshold", 0.15)),
                             "verdict": "insufficient", "collapse_veto": False,
                             "mcnemar_p": None, "bootstrap_gap_ci95": [0.0, 0.0]},
            "diversity": {"entropy_slope": 0.0, "collapse_flag": False, "jsd_kind": None},
            "ablation": None, "power_note": "",
            **dict(sections)}


#: ``report.json`` 的 ``heldout`` 段允许出现的键（= ``schemas/generation_report.schema.json``
#: 的 ``split_metrics`` 属性集减去 ``source``——那个由 ``render_report_sections`` 填）。
#: 必须显式过筛：候选报告里的 heldout dict 还带 ``arm`` / ``seeds`` / ``harness_version`` /
#: ``memory_version``，而 schema 对 ``split_metrics`` 是 ``additionalProperties: false``
#: ⇒ 原样塞进去会让 ``write_generation_report`` 以 ``E_EVAL_SCHEMA`` 落 ``report.invalid.json``。
_SPLIT_METRIC_KEYS: tuple[str, ...] = (
    "basket", "n", "n_dropped", "low_confidence", "win_rate", "win_rate_ci95",
    "mean_stages_cleared", "mean_hearts_left", "mean_battles_won", "mean_decisions",
    "illegal_rate", "fallback_rate", "truncation_rate", "degraded_n",
    "entropy_mean_norm", "action_entropy_curve")


def _arm_like(report: CandidateReport, *, harness_version: int) -> Any:
    """把候选报告的最小面包装成 ``evaluate_generation`` 需要的 ``ArmResult`` 形状。"""
    from types import SimpleNamespace

    heldout = dict(report.heldout or {})
    clean = {key: heldout[key] for key in _SPLIT_METRIC_KEYS if key in heldout}
    clean.setdefault("basket", str(heldout.get("basket") or "H1"))
    metrics = SimpleNamespace(**clean, split="heldout", as_dict=lambda: dict(clean))
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
