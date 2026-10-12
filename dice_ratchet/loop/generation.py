"""周期调度与 run 编排（MOD-06 PRD FR-06-5/6/7/9/11/13/14/15）。

本模块把「跑一局」「反思一局」「每 K 局演化一次」三层拼成一条可中断、可恢复、
成本可控的闭环：

- ``episodes_per_generation``：``E_gen`` 的唯一计算公式（FR-06-13，Spec §8.6）；
- ``maybe_run_generation``：周期边界**幂等**触发 MOD-07（FR-06-6）；
- ``LoopOrchestrator``：``run/resume/status`` 的主循环与状态机（FR-06-8/9/10/11/15）。

MOD-07 通过 ``evolve_fn(state, run_ctx) -> EvolveOutcome`` 注入（PRD §14 风险表：
"MOD-07 接口在 M2 前未就绪 → ``evolve_fn`` 依赖注入 + ``--no-evolve`` 开关，
用 stub 跑通 M2 前半段"）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from ..infra import events
from ..infra.errors import E, InfraError, error_code_of
from ..infra.io import read_json, read_jsonl
from ..infra.run import RunPaths
from . import reflect as reflect_mod
from .runner import EpisodeRunner, RecordedEpisode, RunContext
from .state import (QueueLog, RunState, Scoreboard, init_state, load_state, locks_dir,
                    save_state, scoreboard_path, queue_path, utc_now)

__all__ = ["EvolveOutcome", "LoopOrchestrator", "RunSummary", "SignalGuard",
           "ensure_evolution_worktree_clean", "episodes_per_generation",
           "load_or_init_state", "maybe_run_generation", "write_gen000_baseline"]

#: MOD-03 实测 ``t_d`` p50（Spec §8.6 n=10 实测：7.46 s；p95 10.13 s）。
_TD_P50_S: float = 7.46
#: 默认局每局决策数（Spec §8.6 n=10 实测均值 97.1）。
_D_MEAN: float = 97.1
#: 每决策点的后端调用数（Spec §8.6 实测 1.6–1.8，取中值 1.62 → 19.6 min/局）。
_CALLS_PER_DECISION: float = 1.62


# =====================================================================
# 成本闸门（FR-06-13，Spec §8.6）
# =====================================================================

def episodes_per_generation(cfg: Any) -> int:
    """``E_gen = K + (H + S) + C·(S + H)``（等价于 ``K + (1+C)(S+H)``）。

    默认档（2026-10-06 M2 开工 D-1）：``K=30, C=1, S=2, H=40`` → ``30 + 2×42 = 114``。
    这是全系统**唯一**的成本上限来源，MOD-07 不另设（Spec §8.6 硬约束）。
    """
    return (int(cfg.k) + (int(cfg.heldout_n) + int(cfg.smoke_n))
            + int(cfg.n_candidates) * (int(cfg.smoke_n) + int(cfg.heldout_n)))


def estimated_hours(e_gen: int, *, t_d_s: float = _TD_P50_S, decisions: float = _D_MEAN,
                    calls_per_decision: float = _CALLS_PER_DECISION) -> float:
    """``est_hours = E_gen × D × t_d``（含重试的调用放大系数，Spec §8.6）。"""
    return float(e_gen) * float(decisions) * float(t_d_s) * float(calls_per_decision) / 3600.0


# =====================================================================
# MOD-07 的注入面
# =====================================================================

@dataclass
class EvolveOutcome:
    """MOD-07 一个演化周期的结果（本模块只消费这几个字段）。"""

    decision: str = "reject"          # accept | reject | baseline | error_reject
    generation: int = 0
    harness_version: int = 0
    pending_human: bool = False       # FR-06-11：HITL 挂起
    candidate_dir: str = ""
    report: str = ""
    reason: str = ""
    error_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "generation": int(self.generation),
                "harness_version": int(self.harness_version),
                "pending_human": bool(self.pending_human),
                "candidate_dir": self.candidate_dir, "report": self.report,
                "reason": self.reason, "error_code": self.error_code}


@dataclass
class RunSummary:
    """``LoopOrchestrator.run`` 的返回值（CLI 打印与测试断言用）。"""

    run_id: str
    phase: str
    generation: int
    tick: int
    episodes_total: int
    completed: int
    pending: int
    exit_code: int = 0
    hitl_pending: dict[str, Any] | None = None
    error_code: str | None = None
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "phase": self.phase,
                "generation": int(self.generation), "tick": int(self.tick),
                "episodes_total": int(self.episodes_total),
                "completed": int(self.completed), "pending": int(self.pending),
                "exit_code": int(self.exit_code), "hitl_pending": self.hitl_pending,
                "error_code": self.error_code, "duration_s": round(self.duration_s, 4)}


# =====================================================================
# 信号处理（FR-06-15）
# =====================================================================

class SignalGuard:
    """``SIGINT``/``SIGTERM`` → ``KeyboardInterrupt``；第二次信号立即 ``os._exit(130)``。

    第一次信号：完成当前 step 落盘 → 写 ``state.json``（附 ``interrupted_at``）→
    flush → 退出码 130（由主循环抛出 ``KeyboardInterrupt``，CLI 收尾）。
    第二次信号：``os._exit(130)``，不再给任何清理机会。
    """

    def __init__(self, state: RunState, paths: RunPaths, *, fsync: bool = True) -> None:
        self.state = state
        self.paths = paths
        self.fsync = bool(fsync)
        self.interrupted = False
        self.count = 0
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> "SignalGuard":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handler)
            except (ValueError, OSError): # pragma: no cover - 非主线程/受限平台
                pass
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, previous in self._previous.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError): # pragma: no cover
                pass

    def _handler(self, signum: int, frame: Any) -> None:
        self.count += 1
        if self.count > 1:
            os._exit(error_code_of(E.E_IO_INTERRUPTED))
        self.interrupted = True
        self.state.interrupted_at = utc_now()
        try:
            save_state(self.paths, self.state, fsync=self.fsync)
            events.emit("loop.interrupted", phase=str(self.state.phase),
                        completed=len(self.state.completed_train_seeds),
                        pending=len(self.state.pending_queue), tick=int(self.state.tick))
        except Exception: # noqa: BLE001 - 信号路径不得再抛
            pass
        raise KeyboardInterrupt


# =====================================================================
# 周期边界（FR-06-5/6）
# =====================================================================

def maybe_run_generation(*, state: RunState, run_ctx: RunContext,
                         evolve_fn: Callable[[RunState, RunContext], Any] | None = None,
                         force: bool = False) -> RunState:
    """周期边界**幂等**触发 MOD-07（FR-06-6）。

    幂等三重校验：``last_completed_cycle`` + ``evolving_cycle`` + MOD-07 产物存在性。

    - 已裁决 → 写 ``last_completed_cycle``、``generation += 1``、回 ``playing``
      （接受或拒绝均 +1，FR-06-7）；
    - 进程在 ``evolving`` 阶段被杀 → ``--resume`` **不重跑** MOD-07，检查
      ``generations/gen-XXX/{report,decision}.json``：存在则标该 cycle 完成，
      否则重置 ``phase="playing"`` 后重新触发；
    - ``E_EVOLVE_*`` → 保守拒绝（不更新 harness），``decision="error_reject"``，
      ``generation += 1``，回 ``playing``（退出码 0）。
    """
    config = run_ctx.config
    paths = run_ctx.paths
    cycle = state.cycle_due(max_generations=int(getattr(config, "max_generations", 3)))
    if cycle is None:
        if not force:
            return state
        cycle = state.next_cycle_index

    # ---- 幂等：``evolving_cycle`` 表明上一次在该 cycle 中被杀 ----------------------
    if state.evolving_cycle is not None and int(state.evolving_cycle) == int(cycle):
        if _cycle_artifacts_exist(paths, cycle):
            state.last_completed_cycle = int(cycle)
            state.evolving_cycle = None
            state.generation += 1
            state.phase = "playing"
            _save(run_ctx, state)
            return state
        # F42-B：一代内的**已完成臂**不重跑。这里只做可观测性——真正的复用由 MOD-07
        # 经 `metrics/arms/`（按臂检查点）与 `generations/gen-NNN/proposals/`（提案检查点）
        # 完成；`_cycle_artifacts_exist` 继续负责「整代已裁决」这唯一终态。**不清**
        # `metrics/arms/`：它正是这次重入能省下 3.75 h / ≈¥47 的依据。
        events.emit("loop.resume", phase="evolving", cycle=int(cycle),
                    completed_arms=_completed_arm_count(paths, cycle),
                    reuse="metrics/arms+proposals")
        state.evolving_cycle = None
        state.phase = "playing"
        _save(run_ctx, state)
    if state.last_completed_cycle is not None and int(state.last_completed_cycle) >= int(cycle):
        return state

    if evolve_fn is None:
        # ``--no-evolve``：只验编排（M2 前半段的 stub 路径），不推进 generation
        state.last_completed_cycle = int(cycle)
        state.phase = "playing"
        _save(run_ctx, state)
        return state

    e_gen = episodes_per_generation(config)
    cap = int(getattr(config, "max_episodes_per_generation", 120))
    if e_gen > cap:
        # FR-06-13：全系统唯一成本上限被突破 → 拒绝启动该代，退出码 1
        events.emit("loop.budget_exhausted", e_gen=int(e_gen), cap=int(cap))
        state.phase = "failed"
        state.evolving_cycle = None
        _save(run_ctx, state)
        return state

    events.emit("loop.cycle_start", cycle=int(cycle), generation=int(state.generation),
                e_gen=int(e_gen), est_hours=round(estimated_hours(e_gen), 4))
    state.phase = "evolving"
    state.evolving_cycle = int(cycle)
    state.budget.candidates_used += int(getattr(config, "n_candidates", 0))
    state.budget.eval_episodes_used += max(0, e_gen - int(getattr(config, "k", 0)))
    # FR-06-14：评估局使用冻结的 memory.frozen(v) 只读视图 → 边界先出快照
    version = _snapshot_memory(run_ctx, state)
    _save(run_ctx, state)                     # 先落盘再调 MOD-07（FR-06-6）

    outcome = _call_evolve(evolve_fn, state, run_ctx)
    state.last_completed_cycle = int(cycle)
    state.evolving_cycle = None
    state.generation += 1                     # 接受或拒绝均 +1（FR-06-7）
    if version:
        state.memory_version = max(int(state.memory_version), int(version))
    if outcome.pending_human and bool(getattr(config, "hitl", False)):
        # FR-06-11：写 hitl_pending、置 phase=hitl_wait、退出码 0（正常挂起非错误）
        state.hitl_pending = {"cycle": int(cycle), "generation": int(state.generation),
                              "candidate_dir": str(outcome.candidate_dir),
                              "report": str(outcome.report)}
        state.phase = "hitl_wait"
        _save(run_ctx, state)
        return state
    state.hitl_pending = None
    state.phase = "playing"
    _save(run_ctx, state)
    return state


def _save(run_ctx: RunContext, state: RunState) -> None:
    save_state(run_ctx.paths, state, fsync=_fsync(run_ctx.config))


def _cycle_artifacts_exist(paths: RunPaths, cycle: int) -> bool:
    """MOD-07 是否已为该 cycle 产出**已裁决**的记录（``report.json`` / ``decision.json``）。

    HITL 挂起时 MOD-07 已经写了 ``report.json``（``decision="pending"``），但裁决还没发生：
    此时 ``gate.json`` 仍是 ``pending``，必须让本轮**继续**触发 MOD-07（它认得已存在的
    gate 文件并据此 accept/rollback），否则 ``--resume`` 会空转。
    """
    base = Path(paths.generations) / f"gen-{int(cycle):03d}"
    if not any((base / name).is_file() for name in ("report.json", "decision.json")):
        return False
    return not _hitl_gate_pending(base)


def _completed_arm_count(paths: RunPaths, cycle: int) -> int:
    """该 cycle 已归档的**臂**数量（``metrics/arms/gen{cycle:03d}-*.json``；F42-B）。

    只用于 resume 的可观测性：resume 之所以不再白花一整代，是因为 MOD-07 会按这些归档
    （以及 ``generations/gen-NNN/proposals/proposal.json``）复用已完成的局与提案。
    """
    arms = Path(paths.metrics) / "arms"
    if not arms.is_dir():
        return 0
    return len(list(arms.glob(f"gen{int(cycle):03d}-*.json")))


def _hitl_gate_pending(base: Path) -> bool:
    """``gate.json`` 存在且仍为 ``pending``（人工尚未裁决）。

    F33（2026-10-09 复检登记）：这里原先用的 ``read_json`` **没有**模块级导入（模块级
    只导了 ``read_jsonl``，另一个函数在体内局部导入 ``read_json`` ——那是另一个作用域，
    帮不到这里）。于是只要走到「resume 且该 cycle 的 ``report.json``/``decision.json``
    已存在」，就会 ``NameError``：正是 **HITL 挂起 → ``evolve approve`` →
    ``loop run --resume``** 这条流程。修法是把它提到模块级导入。
    """
    gate = base / "gate.json"
    if not gate.is_file():
        return False
    try:
        data = read_json(gate)
    except InfraError:
        return False
    return isinstance(data, Mapping) and str(data.get("status", "")) == "pending"


def _call_evolve(evolve_fn: Callable[[RunState, RunContext], Any], state: RunState,
                 run_ctx: RunContext) -> EvolveOutcome:
    """调用 MOD-07；``E_EVOLVE_*`` → 保守拒绝（Spec §8.5「裁判失败 → 保守拒绝」）。"""
    try:
        raw = evolve_fn(state, run_ctx)
    except InfraError as exc:
        if not str(exc.code).startswith("E_EVOLVE_"):
            raise
        events.emit("evolve.decision", gen=int(state.generation), decision="error_reject",
                    decided_by="auto", reason=str(exc.code))
        return EvolveOutcome(decision="error_reject", generation=int(state.generation) + 1,
                             harness_version=int(state.harness_version),
                             reason=str(exc.code), error_code=str(exc.code))
    return _coerce_outcome(raw, state)


def _coerce_outcome(raw: Any, state: RunState) -> EvolveOutcome:
    if isinstance(raw, EvolveOutcome):
        return raw
    default = EvolveOutcome(decision="reject", generation=int(state.generation) + 1,
                            harness_version=int(state.harness_version))
    if raw is None:
        return default
    if isinstance(raw, Mapping):
        known = {f for f in EvolveOutcome.__dataclass_fields__}  # type: ignore[attr-defined]
        return EvolveOutcome(**{k: v for k, v in raw.items() if k in known})
    return replace(
        default, decision=str(getattr(raw, "decision", default.decision)),
        generation=int(getattr(raw, "generation", default.generation)),
        harness_version=int(getattr(raw, "harness_version", default.harness_version)),
        pending_human=bool(getattr(raw, "pending_human", False)),
        candidate_dir=str(getattr(raw, "candidate_dir", "")),
        report=str(getattr(raw, "report", "")),
        reason=str(getattr(raw, "reason", "")))


def _snapshot_memory(run_ctx: RunContext, state: RunState) -> int | None:
    """代际边界的记忆快照（Spec §4.7 ``MOD-06 → MOD-05 snapshot(version)``）。

    失败只降级（``E_IO_MEMORY_WRITE_FAILED``，记忆是增强项），不阻塞演化周期。
    """
    memory = getattr(run_ctx, "memory", None)
    if memory is None:
        return None
    # MOD-05 的 ``snapshot_every_generation`` 是 property（返回 bool）；假实现可能是方法，
    # 两种都接受（NFR-06-6：依赖可注入）。
    flag = getattr(memory, "snapshot_every_generation", True)
    if callable(flag):
        flag = flag()
    if not flag:
        return None
    version = int(state.generation) + 1
    try:
        memory.snapshot(version)
    except Exception: # noqa: BLE001 - 快照失败不阻塞
        events.emit("loop.reflect", episode_key="", status="skipped",
                    reason=str(E.E_IO_MEMORY_WRITE_FAILED), detail="memory_snapshot")
        return None
    return version


# =====================================================================
# gen-000 基线（FR-06-13a）
# =====================================================================

def write_gen000_baseline(run_ctx: RunContext, *, harness_version: int = 0,
                          basket: Any = None, runner: Any = None) -> Path | None:
    """对 harness v0 在 ``basket``（缺省取 ``evolve.heldout_basket``，默认 H1）上跑
    MOD-08 ``evaluate_arm`` + ``evaluate_generation``。

    产出 ``generations/gen-000/report.json``（``decision="baseline"``），后续各代与它对比。
    ``runner`` 非空时用 MOD-06 的 ``EpisodeRunner``（经 ``EvalRunnerAdapter``）执行评估局，
    轨迹落到 ``episodes/heldout/``；为空时退回 MOD-08 的默认执行器。
    MOD-08 / 模型不可用时返回 ``None`` 并降级——基线缺失不阻塞主循环。

    **F54（2026-10-11）**：`basket` 原先的默认值是**写死的字符串 `"H1"`**，且生产调用方
    从不传它 ⇒ 「彩排只用 H3」在代码层不可达：`scripts/run_m3_rehearsal.sh` 实测跑在 H1 上
    （一次性资源被消耗）。现在缺省从 ``evolve.heldout_basket`` 取，**默认值仍是 H1**，
    于是「基线篮子」与「候选评估篮子」（`evolve.judge.CandidateEvaluator`）由**同一个键**
    决定，不可能再分叉。
    """
    paths = run_ctx.paths
    report_path = Path(paths.generations) / "gen-000" / "report.json"
    if report_path.is_file():
        return report_path
    cfg = run_ctx.app_config
    if cfg is None:
        return None
    if basket is None:
        basket = str(getattr(getattr(cfg, "evolve", None), "heldout_basket", None)
                     or "H1")
    try:
        from ..evals import evaluate_arm, evaluate_generation, write_generation_report
        from ..evals.seeds import coerce_protocol, validate_protocol
    except Exception: # noqa: BLE001 - MOD-08 未就绪（M0 过渡期）
        return None
    proto = validate_protocol(coerce_protocol(None))
    executor = None
    if runner is not None:
        from .runner import EvalRunnerAdapter

        executor = EvalRunnerAdapter(runner)
    try:
        parent = evaluate_arm(
            Path(run_ctx.harness_dir), split="heldout", basket=basket,
            memory_version=int(run_ctx.memory_version), arm="parent",
            run_id=run_ctx.run_id, protocol=proto, config=cfg, generation=0,
            harness_version=int(harness_version), runner=executor,
            n_workers=int(getattr(cfg.evals, "n_workers", 1)),
            n_rep=int(getattr(cfg.evals.determinism, "n_rep", 1)),
            episode_config=run_ctx.episode_config)
        # **必须传 run_dir**（2026-10-11 修）：MOD-08 从 F48/F49 起会在评估/报告路径上
        # 落**按代**的产物（`metrics/kind_distribution/gen-XXX.json`，`c3` 判据要读它），
        # 而 `evaluate_generation` 的 `run_dir=None` 会回落到**硬编码的 `Path("runs")`**
        # ⇒ 那些文件落到真实 `runs/<run_id>/` 而不是 `run_ctx.paths.root`。
        # 后果不止「位置错」：真实 `runs/<run_id>/` 一旦被建出来，F42-A 的**臂归档**
        # （`_arm_archive_dir` 按 `runs_root/run_id` 推导，且要求目录已存在）就会开始
        # 生效 ⇒ 第二次评估**复用归档、不再执行任何局**。MOD-07 的两处调用点都已传
        # `run_dir`，这里原先漏了。
        report = dict(evaluate_generation(0, parent, None, run_id=run_ctx.run_id,
                                          run_dir=paths.root))
    except InfraError as exc:
        # 基线拿不到（模型/权重缺失）→ 降级，不阻塞 run
        events.emit("loop.resume", phase=str(run_ctx.state.phase), baseline="missing",
                    error_code=str(exc.code))
        return None
    report["candidates"] = []
    report["decision"] = "baseline"
    report["decided_by"] = "auto"
    report["reason"] = "harness v0 基线（gen-000，FR-06-13a）"
    write_generation_report(paths.root, 0, report)
    # F56（2026-10-12，M3 彩排实测）：**基线也要进台账 + 出图**。
    # 现场：`write_gen000_baseline` 原先只写 `report.json` ⇒ `metrics/generations.csv` 从
    # gen-001 开始（缺 gen-000 行），于是判据 1 判定器手上没有「相邻两代」⇒
    # `comparable=0` ⇒ 彩排的判据「判定器能给出结论」**必然不合格**；
    # 同时 `plots/` 全空（两条写入路径都没接绘图，见 harness.ledger_and_plots 的 docstring）。
    try:
        from ..evals.harness import ledger_and_plots

        ledger_and_plots(paths.root, 0, report, cfg=cfg)
    except Exception as exc: # noqa: BLE001 - 附属产物失败不阻塞基线
        from ..infra import events as _events

        _events.emit("loop.resume", phase="baseline", baseline="ledger_failed",
                     error_code=str(getattr(exc, "code", "E_IO_WRITE")))
    return report_path


# =====================================================================
# 演化起跑前的 git 门禁（F47-A）
# =====================================================================

def ensure_evolution_worktree_clean(run_ctx: Any) -> bool:
    """演化**起跑前**拒绝脏工作区（FR-09-14「演化前门禁」的真正落点，F47-A）。

    为什么必须在这里：MOD-07 的同一判据原先只长在 ``commit_harness`` 内部，即
    **整代跑完、即将落版本时**——脏工作区会白花一整代（实测 3.75 h / ≈¥47），
    失败还被 ``_finish_report`` 记成 ``decision="reject"``，看起来像「候选不达标」。
    本函数把它前移到**任何推理调用之前**，之后由 MOD-07 的提交前检查兜底。

    **只由 CLI 边界调用**（``cmd_loop_run``，含 ``--resume``）且仅在 ``evolve_fn`` 非空
    时——保护的是「版本落盘」这件事。不放进 ``LoopOrchestrator.run()`` 的内部：那会让
    程序化嵌入（含测试与本地带未提交改动的诊断）一律被拦；``--no-evolve``/``loop
    smoke`` 不产版本，也不拦。

    仓库根取 ``harness_dir`` 的父目录（与 MOD-07 ``_repo_root`` 同规则；``git status
    --porcelain`` 的路径是仓库根相对的，故子目录调用结果一致）。
    返回是否真的检查过：``False`` = 不在 git worktree 内 → 降级跳过（Spec §8.5），
    否则脏工作区抛 ``E_IO_GIT_DIRTY``（退出码 5）。
    """
    from ..infra import git_ops

    harness = Path(getattr(run_ctx, "harness_dir", "harness"))
    return bool(git_ops.preflight_worktree_clean(harness.resolve().parent))


# =====================================================================
# LoopOrchestrator
# =====================================================================

class LoopOrchestrator:
    """run 级主循环（FR-06-8/9/10/11/15）。"""

    #: 环境内核错误累计上限：同类异常 ≥3 → 中止 run（退出码 4，PRD §10）
    ENV_ERROR_LIMIT: int = 3

    def __init__(self, *, run_ctx: RunContext, config: Any, runner: EpisodeRunner,
                 evolve_fn: Callable[[RunState, RunContext], Any] | None = None,
                 scoreboard: Scoreboard | None = None) -> None:
        self.run_ctx = run_ctx
        self.config = config
        self.runner = runner
        self.evolve_fn = evolve_fn
        self.paths = run_ctx.paths
        self.state = run_ctx.state
        self.scoreboard = scoreboard or Scoreboard(
            scoreboard_path(self.paths), fsync=bool(getattr(config, "state_fsync", True)))
        self.queue = QueueLog(queue_path(self.paths))
        self._started = time.perf_counter()

    # ---- 查询 -------------------------------------------------------------
    def status(self) -> RunState:
        return self.state

    def summary(self, *, exit_code: int = 0, error_code: str | None = None) -> RunSummary:
        return RunSummary(
            run_id=self.state.run_id, phase=self.state.phase,
            generation=int(self.state.generation), tick=int(self.state.tick),
            episodes_total=int(self.state.counters.episodes_total),
            completed=len(self.state.completed_train_seeds),
            pending=len(self.state.pending_queue), exit_code=int(exit_code),
            hitl_pending=(dict(self.state.hitl_pending)
                          if self.state.hitl_pending else None),
            error_code=error_code, duration_s=time.perf_counter() - self._started)

    # ---- 主循环 -----------------------------------------------------------
    def run(self, seeds: Sequence[int] | None = None, *, resume: bool = False,
            force_rerun: bool = False) -> RunSummary:
        """跑完整条闭环；``resume=True`` 时从 ``state.json`` 续跑（FR-06-9）。"""
        state = self.state
        if resume:
            state.resume_count += 1
            events.emit("loop.resume", phase=str(state.phase),
                        completed=len(state.completed_train_seeds),
                        pending=len(state.pending_queue), tick=int(state.tick))
        else:
            state.phase = "pending"
        if seeds is not None:
            wanted = _dedup(seeds)
            _verify_train_splits(wanted)
            done = state.completed
            state.pending_queue = [s for s in wanted if s not in done]   # 不重复做
        else:
            _verify_train_splits(state.pending_queue)
        self._queue_add_missing()
        _save(self.run_ctx, state)

        if resume and state.hitl_pending is not None:
            self._clear_hitl(state)
        if resume:
            self.retry_skipped_reflections()

        with SignalGuard(state, self.paths, fsync=_fsync(self.config)) as guard:
            try:
                self._loop(guard, force_rerun=force_rerun)
            except KeyboardInterrupt:
                state.interrupted_at = state.interrupted_at or utc_now()
                _save(self.run_ctx, state)
                return self.summary(exit_code=error_code_of(E.E_IO_INTERRUPTED),
                                    error_code=str(E.E_IO_INTERRUPTED))
            except InfraError as exc:
                state.phase = "failed"
                _save(self.run_ctx, state)
                raise
        if state.phase == "failed":
            return self.summary(exit_code=1,
                                error_code=str(E.E_EVAL_BUDGET_EXCEEDED))
        if state.hitl_pending is not None:
            return self.summary(exit_code=0)      # HITL 挂起是正常结束（退出码 0）
        self._check_max_run_bytes()
        return self.summary(exit_code=0)

    # ---- 主循环内部 -------------------------------------------------------
    def _loop(self, guard: SignalGuard, *, force_rerun: bool) -> None:
        state = self.state
        reflect_enabled = bool(getattr(self.config, "reflect_enabled", True))
        while state.pending_queue:
            if guard.interrupted:
                raise KeyboardInterrupt
            seed = int(state.pending_queue[0])
            state.phase = "playing"
            state.harness_version = self.run_ctx.harness_version()   # 每局重读（FR-06-7）
            _save(self.run_ctx, state)

            record = self._run_training_episode(seed)
            if record is None:
                continue                       # 该 seed 已被跳过（env 配置非法 / 内核错误）
            self._score_once(record, force_rerun=force_rerun)
            if seed not in state.completed_train_seeds:
                state.completed_train_seeds.append(seed)
            state.pending_queue.pop(0)
            state.counters.episodes_total += 1
            state.counters.episodes_truncated += 1 if record.truncation_reason else 0
            state.tick += 1                                       # 逻辑时钟（FR-06-3a）
            self.queue.append("done", seed, episode_key=record.episode_key)
            state.phase = "reflecting"
            _save(self.run_ctx, state)

            self._reflect_one(record, enabled=reflect_enabled)

            state.phase = "playing"
            _save(self.run_ctx, state)
            maybe_run_generation(state=state, run_ctx=self.run_ctx,
                                 evolve_fn=self.evolve_fn)
            if state.phase in ("hitl_wait", "failed"):
                return                     # FR-06-11 挂起 / FR-06-13 预算闸门

        # F34（2026-10-09 登记）：队列排空 ≠ 本轮无事可做。
        #
        # ``--resume`` 时 ``run()`` 会把 ``pending_queue`` 重算成「``--seeds`` 里尚未
        # 完成的 seed」，因此**崩溃在周期边界内**的 run（``phase="evolving"``、
        # ``evolving_cycle`` 挂着、``completed`` 恰好是 k 的整数倍）会拿到空队列：
        # 上面的 ``while`` 一次都不进，边界永不重入，函数直接落到 ``phase="done"``
        # ——对外是**退出码 0 的成功**，而 ``generation`` 一步没走、
        # ``harness/versions/`` 一个新版本都没有。实测（零成本探针）：这样的 resume
        # 返回 ``{"exit_code": 0, "phase": "done", "generation": 0}``，同时
        # ``evolving_cycle`` 仍悬着 —— 正是 §3.1 要防的静默成功（同 F28 的教训）。
        #
        # ``maybe_run_generation`` 本就为此设计（见其 docstring：进程在 ``evolving``
        # 阶段被杀时，「产物存在则标该 cycle 完成，否则重置 ``phase="playing"`` 后
        # 重新触发」），只是原先没有任何路径在空队列下调用它。这里补上这一次调用。
        #
        # **只需调用一次**：``cycle_due`` 的双重幂等键（``last_completed_cycle`` +
        # ``evolving_cycle`` + MOD-07 产物存在性）保证重复调用不会重复触发；而空队列
        # 下 ``cycle_index = len(completed)//k`` 不再前进，故至多一个 cycle 可能到期。
        maybe_run_generation(state=state, run_ctx=self.run_ctx,
                             evolve_fn=self.evolve_fn)
        if state.phase in ("hitl_wait", "failed"):
            return
        state.phase = "done"
        _save(self.run_ctx, state)

    def _run_training_episode(self, seed: int) -> RecordedEpisode | None:
        """跑一局并落实 PRD §10 的失败恢复矩阵；``None`` = 该 seed 被跳过。"""
        state = self.state
        try:
            return self.runner.run_episode(
                seed, split="train", arm="train",
                episode_config=self.run_ctx.episode_config,
                harness_version=int(state.harness_version))
        except InfraError as exc:
            code = str(exc.code)
            if code == str(E.E_ENV_CONFIG_INVALID):
                # 环境 reset 失败/配置非法 → 跳过该 seed，run 继续
                self.queue.append("skip", seed, reason=code)
                state.pending_queue.pop(0)
                _save(self.run_ctx, state)
                return None
            if code.startswith("E_ENV_"):
                # 内核错误：不重试（不掩盖 bug）；同类累计 ≥3 → 中止 run（退出码 4）
                state.counters.env_errors += 1
                self._write_env_error_audit(seed, exc)
                self.queue.append("skip", seed, reason=code)
                state.pending_queue.pop(0)
                if state.counters.env_errors >= self.ENV_ERROR_LIMIT:
                    raise InfraError(
                        E.E_ENV_INTERNAL,
                        f"同类环境异常累计 {state.counters.env_errors} 次：{code}",
                        hint="内核 bug 不重试、不掩盖（MOD-06 PRD §10）；"
                             "修好后 --resume 继续") from exc
                _save(self.run_ctx, state)
                return None
            raise
        except KeyboardInterrupt:
            raise

    def _write_env_error_audit(self, seed: int, exc: InfraError) -> None:
        """``audit/env_error_<seed>.json``（含 ``state_hash`` 与入参，PRD §10）。"""
        try:
            from ..infra.io import write_json_atomic

            path = Path(self.paths.audit) / f"env_error_{int(seed)}.json"
            write_json_atomic(path, {
                "seed": int(seed), "error_code": str(exc.code), "message": exc.message,
                "hint": exc.hint, "episode_config": _config_dict(self.run_ctx.episode_config),
                "harness_version": int(self.state.harness_version),
                "generation": int(self.state.generation), "ts": utc_now(),
            })
        except Exception: # noqa: BLE001 - 审计落盘失败不改变主循环行为
            pass

    # ---- 计分（FR-06-10）--------------------------------------------------
    def _score_once(self, record: RecordedEpisode, *, force_rerun: bool) -> bool:
        row = {
            "episode_key": record.episode_key, "seed": int(record.seed),
            "split": record.split, "generation": int(record.generation),
            "harness_version": int(record.harness_version), "outcome": record.outcome,
            "truncation_reason": record.truncation_reason, "steps": int(record.steps),
            "decisions": int(record.decisions), "cum_reward": round(record.cum_reward, 4),
            "stages_cleared": int(record.stages_cleared),
            "hearts_left": int(record.hearts_left),
            "battles_won": int(record.battles_won),
            "illegal_attempts": int(record.illegal_attempts),
            "fallback_decisions": int(record.fallback_decisions),
            "superseded_by": None,
        }
        return self.scoreboard.add_once(row, force=force_rerun)

    # ---- 反思（FR-06-3/4）-------------------------------------------------
    def _reflect_one(self, record: RecordedEpisode, *, enabled: bool) -> str | None:
        state = self.state
        template = Path(self.run_ctx.harness_dir) / "prompts" / "reflection.md"
        template_sha = reflect_mod.template_sha256(template)
        started = time.perf_counter()
        if not enabled:
            reflect_mod.write_reflection_record(
                self.paths, record=reflect_mod.skipped_record(
                    record=record, reason="reflect_disabled",
                    template_sha256=template_sha))
            state.counters.reflections_skipped += 1
            events.emit("loop.reflect", episode_key=record.episode_key, status="skipped",
                        reason="reflect_disabled", latency_ms=0,
                        template_sha256=template_sha)
            return None
        try:
            summary = reflect_mod.summarize_episode(record, window=self._recent())
            # Issue #1：反思走 **reflection** 角色（可能不是 policy 的模型）。
            reflection_client = self._role_client("reflection")
            entry = reflect_mod.reflect(summary, client=reflection_client, template=template,
                                        timeout_s=float(getattr(self.config,
                                                                "reflect_timeout_s", 60.0)))
            if entry is None:
                raise InfraError(E.E_LLM_SCHEMA_INVALID, "反思模型返回空文本")
            self._add_reflection(entry, record)
            latency_ms = int(round((time.perf_counter() - started) * 1000.0))
            reflect_mod.write_reflection_record(
                self.paths, record=reflect_mod.ok_record(
                    record=record, entry=entry,
                    llm={"model": getattr(reflection_client, "model_id", None),
                         "latency_ms": latency_ms},
                    template_sha256=template_sha))
            state.counters.reflections_ok += 1
            events.emit("loop.reflect", episode_key=record.episode_key, status="ok",
                        reason=None, latency_ms=latency_ms, template_sha256=template_sha)
            return str(getattr(entry, "reflection_id", ""))
        except Exception as exc: # noqa: BLE001 - FR-06-4：反思失败只降级，绝不阻塞 run
            reason = reflect_mod.reason_of(exc)
            if reason == str(E.E_LLM_TIMEOUT):
                state.counters.llm_timeouts += 1
            state.counters.reflections_skipped += 1
            try:
                reflect_mod.write_reflection_record(
                    self.paths, record=reflect_mod.skipped_record(
                        record=record, reason=reason, template_sha256=template_sha))
            except Exception: # noqa: BLE001 - 连 skipped 标注都写不下去也不能阻塞 run
                pass
            events.emit("loop.reflect", episode_key=record.episode_key, status="skipped",
                        reason=reason,
                        latency_ms=int(round((time.perf_counter() - started) * 1000.0)),
                        template_sha256=template_sha)
            return None

    def _add_reflection(self, entry: Any, record: RecordedEpisode) -> None:
        """MOD-05 写入（持 ``locks/memory.lock``，FR-06-14）。"""
        memory = self.runner.memory
        if memory is None:
            return
        from ..memory import Provenance

        provenance = Provenance(run_id=str(self.run_ctx.run_id),
                                episode_id=record.episode_key, split="train",
                                seed=int(record.seed), generation=int(record.generation),
                                harness_version=int(record.harness_version))
        with _FileLock(locks_dir(self.paths) / "memory.lock"):
            memory.add_reflection(entry, provenance=provenance)

    def _client(self) -> Any:
        """注入的 MOD-03 客户端或其**角色提供者**（由组合根注入到 runner 上；缺失则降级）。"""
        return (getattr(self.runner, "client", None)
                or getattr(self.runner, "llm", None)
                or getattr(self.run_ctx, "client", None))

    def _role_client(self, role: str) -> Any:
        """按角色取客户端（Issue #1）：注入角色提供者时按角色解析，否则原样返回。

        鸭子类型（``client_for``）而非 import 类型：MOD-06 不需要知道 MOD-03 的提供者类。
        """
        from ..inference.client import client_for_role

        return client_for_role(self._client(), role)

    def _recent(self) -> list[RecordedEpisode]:
        """最近 ``reflect_context_window`` 局的摘要（NFR-06-7：不累积轨迹）。

        数据来源是 ``metrics/scoreboard.json`` 的训练局台账——它是 run 的权威记录，
        因此续跑后窗口依然正确。
        """
        window = int(getattr(self.config, "reflect_context_window", 5))
        rows = [r for r in self.scoreboard.rows
                if str(r.get("split")) == "train" and not r.get("superseded_by")]
        return [self._record_from_row(row) for row in rows[-window:]]

    def _record_from_row(self, row: Mapping[str, Any]) -> RecordedEpisode:
        """从计分台账的一行还原 ``RecordedEpisode``（反思只读它需要的字段）。"""
        return RecordedEpisode(
            episode_key=str(row.get("episode_key")), seed=int(row.get("seed", 0)),
            split=str(row.get("split", "train")), arm=str(row.get("arm") or "train"),
            generation=int(row.get("generation", 0)),
            harness_version=int(row.get("harness_version", 0)),
            steps=int(row.get("steps", 0)), decisions=int(row.get("decisions", 0)),
            cum_reward=float(row.get("cum_reward", 0.0)),
            outcome=str(row.get("outcome", "lose")),
            truncation_reason=row.get("truncation_reason"),
            stages_cleared=int(row.get("stages_cleared", 0)),
            hearts_left=int(row.get("hearts_left", 0)),
            battles_won=int(row.get("battles_won", 0)),
            illegal_attempts=int(row.get("illegal_attempts", 0)),
            fallback_decisions=int(row.get("fallback_decisions", 0)),
            degraded=False, exclude_reason=None, jsonl_path="", reflection_id=None,
            wall_ms=0, tick=int(self.state.tick), run_id=str(self.run_ctx.run_id))

    # ---- skipped 反思的补偿（配置键 loop.retry_reflections_on_resume / --retry-reflections）----
    def retry_skipped_reflections(self) -> int:
        """重跑此前 ``status="skipped"`` 的反思（PRD §16 开放问题 7 的显式开关）。

        默认关闭（``loop.retry_reflections_on_resume=false``）：补跑会改变记忆内容，
        进而影响后续局与确定性，因此必须是**显式**选择。只对已完成（有计分记录）的局
        补跑，降级路径与正常反思完全一致（失败仍写回 skipped）。
        """
        if not bool(getattr(self.config, "retry_reflections_on_resume", False)):
            return 0
        # F33：``read_json`` 已提到模块级导入（原先只有这里的局部导入，导致
        # ``_hitl_gate_pending`` 以为它在全局作用域可见）。

        directory = Path(self.paths.reflections)
        if not directory.is_dir():
            return 0
        by_key = {str(r.get("episode_key")): r for r in self.scoreboard.rows}
        retried = 0
        for path in sorted(directory.glob("*.json")):
            try:
                record = read_json(path)
            except InfraError: # pragma: no cover - 损坏的 skipped 标注：跳过
                continue
            if not isinstance(record, Mapping) or str(record.get("status")) != "skipped":
                continue
            row = by_key.get(str(record.get("episode_key")))
            if row is None or str(row.get("split")) != "train":
                continue
            self._reflect_one(self._record_from_row(row), enabled=True)
            retried += 1
        return retried

    # ---- HITL（FR-06-11）--------------------------------------------------
    def _clear_hitl(self, state: RunState) -> None:
        """``--resume`` 读 ``harness/version.json`` 与 ``hitl_pending``，清空标记并继续。"""
        state.harness_version = self.run_ctx.harness_version()
        state.hitl_pending = None
        if state.phase == "hitl_wait":
            state.phase = "playing"
        _save(self.run_ctx, state)

    # ---- queue.jsonl ------------------------------------------------------
    def _queue_add_missing(self) -> None:
        known = {int(r.get("seed")) for r in _read_queue(self.paths)
                 if r.get("action") == "add"}
        for seed in self.state.pending_queue:
            if int(seed) not in known:
                self.queue.append("add", int(seed))
                known.add(int(seed))

    # ---- 磁盘配额告警（loop.max_run_bytes，不中断）--------------------------
    def _check_max_run_bytes(self) -> None:
        limit = int(getattr(self.config, "max_run_bytes", 0))
        if limit <= 0:
            return
        try:
            total = sum(p.stat().st_size
                        for p in Path(self.paths.episodes).rglob("*.jsonl"))
        except OSError: # pragma: no cover
            return
        if total > limit:
            print(f"[{E.E_IO_DISK_FULL}] 本次 run 轨迹 {total / (1 << 30):.2f} GiB 超过 "
                  f"loop.max_run_bytes（仅告警，不中断）", file=sys.stderr)


# =====================================================================
# 小工具
# =====================================================================

def _dedup(values: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        seed = int(value)
        if seed not in seen:
            seen.add(seed)
            out.append(seed)
    return out


def _verify_train_splits(seeds: Sequence[int]) -> None:
    """INV-5：训练局只能消费 ``train`` split 的 seed（Spec v1.4 B-36）。

    MOD-06 **不**自行切分 seed（那是 MOD-08 ``evals.seeds.take`` 的唯一出口），只校验
    传入 seed 的 split 标签与 MOD-08 注册表一致；不一致立即抛 ``E_EVAL_SPLIT_VIOLATION``。
    """
    pending = [int(s) for s in seeds]
    if not pending:
        return
    from ..evals.seeds import coerce_protocol, split_of_seed, validate_protocol

    proto = validate_protocol(coerce_protocol(None))
    for seed in pending:
        split = split_of_seed(seed, protocol=proto)
        if split != "train":
            raise InfraError(
                E.E_EVAL_SPLIT_VIOLATION,
                f"seed {seed} 的 split={split!r}，不能作为训练局（INV-5）",
                hint="训练 seed 只能经 MOD-08 evals.seeds.take('train') 取用；"
                     "held-out/test seed 进入训练口即 P0 违规")


def _read_queue(paths: RunPaths) -> list[dict[str, Any]]:
    path = queue_path(paths)
    if not path.is_file():
        return []
    rows, _truncated = read_jsonl(path)
    return rows


def _config_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return {"repr": repr(value)}


def _fsync(config: Any) -> bool:
    return bool(getattr(config, "state_fsync", True))


class _FileLock:
    """``runs/<run_id>/locks/memory.lock`` 文件锁（FR-06-14）。

    训练局串行（``train_concurrency`` 固定 1）+ 跨进程文件锁，双保险防止共享记忆写入
    竞态。``fcntl`` 不可用的平台降级为「仅进程内占位」——训练局本来就不并发。
    """

    def __init__(self, path: Path, *, timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout_s = float(timeout_s)
        self._fh: Any = None
        self._fcntl: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl

            self._fcntl = fcntl
        except ImportError: # pragma: no cover - Windows 降级
            return self
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._fcntl.flock(self._fh.fileno(),
                                  self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    raise InfraError(
                        E.E_IO_MEMORY_LOCKED,
                        f"memory.lock 被占用超过 {self.timeout_s}s：{self.path}",
                        hint="并发写共享记忆会污染记忆库（FR-06-14）；检查残留进程")
                time.sleep(0.02)

    def __exit__(self, *exc: object) -> None:
        if self._fh is None:
            return
        try:
            if self._fcntl is not None:
                self._fcntl.flock(self._fh.fileno(), self._fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def load_or_init_state(run_ctx: RunContext, *, seeds: Sequence[int] = (),
                       resume: bool = False) -> RunState:
    """``--resume`` → ``load_state``（缺失即 ``E_IO_STATE_MISSING``）；否则新建。"""
    if resume:
        state = load_state(run_ctx.paths)
        state.k = int(getattr(run_ctx.config, "k", state.k))
        return state
    return init_state(
        run_ctx.run_id, k=int(getattr(run_ctx.config, "k", 30)), seeds=seeds,
        harness_version=run_ctx.harness_version(),
        budget_cap=int(getattr(run_ctx.config, "max_episodes_per_generation", 0)))
