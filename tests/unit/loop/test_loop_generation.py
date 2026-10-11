"""MOD-06 周期调度、成本闸门与并发守卫（PRD §11 的 T-06-6 / T-06-10 / T-06-11）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fakes import FakeEnv, FakePolicy
from loop_support import reconfigure
from dice_ratchet.infra.config import LoopConfig
from dice_ratchet.infra.errors import EXIT_CONFIG, E, InfraError, error_code_of
from dice_ratchet.loop import episodes_per_generation
from dice_ratchet.loop.generation import EvolveOutcome, estimated_hours, maybe_run_generation


class RecordingEvolve:
    """记录调用次数的假 ``evolve_fn``。"""

    def __init__(self, outcome: EvolveOutcome | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.outcome = outcome

    def __call__(self, state, run_ctx):
        self.calls.append((int(state.generation), int(run_ctx.state.generation)))
        base = self.outcome or EvolveOutcome(decision="reject")
        return replace(base, generation=int(state.generation) + 1,
                       harness_version=int(state.harness_version))


# =====================================================================
# T-06-6 周期触发与幂等
# =====================================================================

def test_t06_6_cycle_triggers_once_per_k(tmp_path, mk_orchestrator, mk_memory):
    """给定 ``k=3`` 与记录调用的假 ``evolve_fn``；当跑 6 局；

    则 ``evolve_fn`` 恰被调 2 次、``generation==2``；当再对同一 state 调
    ``maybe_run_generation()``；则调用次数不增。

    注：``last_completed_cycle`` 按 PRD §7.3 的 ``cycle_index = len(completed)//k``
    取 1/2（PRD §11 的 T-06-6 文本写作 1，与 §8 的伪代码不自洽；本模块以 §7.3/§8 为准，
    见 README「与 PRD 的已知偏差」）。
    """
    evolve = RecordingEvolve()
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=evolve, seeds=tuple(range(1000, 1006)))
    reconfigure(orch, ctx, k=3, reflect_enabled=False, max_generations=5)
    summary = orch.run(seeds=list(range(1000, 1006)))

    assert summary.phase == "done"
    assert len(evolve.calls) == 2, f"每 K 局恰好触发一次，实际 {evolve.calls}"
    assert ctx.state.generation == 2
    assert ctx.state.last_completed_cycle == 2
    assert ctx.state.evolving_cycle is None

    before = len(evolve.calls)
    maybe_run_generation(state=ctx.state, run_ctx=ctx, evolve_fn=evolve)
    assert len(evolve.calls) == before, "同一 cycle_index 至多触发一次（FR-06-6）"


def test_t06_6_cycle_not_triggered_before_k(tmp_path, mk_orchestrator, mk_memory):
    """不足 K 局不触发（FR-06-5）。"""
    evolve = RecordingEvolve()
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=evolve, seeds=(1000, 1001))
    reconfigure(orch, ctx, k=3, reflect_enabled=False)
    orch.run(seeds=[1000, 1001])
    assert evolve.calls == []
    assert ctx.state.generation == 0


def test_t06_6_generation_capped_by_max_generations(tmp_path, mk_orchestrator, mk_memory):
    """``generation >= max_generations`` 后不再触发（FR-06-5 的第三个条件）。"""
    evolve = RecordingEvolve()
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=evolve, seeds=tuple(range(1000, 1006)))
    reconfigure(orch, ctx, k=2, reflect_enabled=False, max_generations=1)
    orch.run(seeds=list(range(1000, 1006)))
    assert len(evolve.calls) == 1
    assert ctx.state.generation == 1


def test_t06_6_evolve_error_is_conservative_reject(tmp_path, mk_orchestrator, mk_memory):
    """MOD-07 抛 ``E_EVOLVE_*`` → 保守拒绝、``generation += 1``、回 ``playing``（退出码 0）。"""
    def boom(_state, _run_ctx):
        raise InfraError(E.E_EVOLVE_JUDGE_FAILED, "裁判挂了")

    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=boom, seeds=(1000, 1001))
    reconfigure(orch, ctx, k=2, reflect_enabled=False)
    summary = orch.run(seeds=[1000, 1001])
    assert summary.exit_code == 0
    assert ctx.state.generation == 1
    assert ctx.state.phase == "done"


# =====================================================================
# T-06-10 成本闸门
# =====================================================================

@pytest.mark.parametrize("k,heldout,smoke,candidates,expected", [
    (30, 40, 5, 3, 210),
    (30, 40, 5, 2, 165),
    (30, 40, 2, 1, 114),      # 2026-10-06 M2 开工 D-1 默认档
    (30, 50, 2, 1, 134),
])
def test_t06_10_e_gen_formula(k, heldout, smoke, candidates, expected):
    """``E_gen = K + (H + S) + C·(S + H)``（Spec §8.6）。"""
    cfg = LoopConfig(k=k, heldout_n=heldout, smoke_n=smoke, n_candidates=candidates)
    assert episodes_per_generation(cfg) == expected


def _state_at_cycle(k: int) -> "object":
    from dice_ratchet.loop.state import RunState

    return RunState(run_id="r-budget", phase="playing", k=k,
                    completed_train_seeds=list(range(1000, 1000 + k)))


def test_t06_10_budget_gate_rejects_expensive_generation(tmp_path, mk_runner):
    """给定 ``K=30, H=40, S=5, C=3``（E_gen=210）与 cap=170；当到达周期边界；

    则不调用 MOD-07、发 ``loop.budget_exhausted``、``phase="failed"``（退出码 1）；
    把 C 改成 2（E_gen=165）后正常触发。
    """
    cfg = LoopConfig(k=30, heldout_n=40, smoke_n=5, n_candidates=3,
                     max_episodes_per_generation=170, max_generations=3)
    runner, run_ctx = mk_runner(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), loop_cfg=cfg)
    run_ctx.state = _state_at_cycle(30)
    evolve = RecordingEvolve()

    maybe_run_generation(state=run_ctx.state, run_ctx=run_ctx, evolve_fn=evolve)
    assert evolve.calls == []
    assert run_ctx.state.phase == "failed"

    cheap = LoopConfig(k=30, heldout_n=40, smoke_n=5, n_candidates=2,
                       max_episodes_per_generation=170, max_generations=3)
    run_ctx.config = cheap
    run_ctx.state = _state_at_cycle(30)
    maybe_run_generation(state=run_ctx.state, run_ctx=run_ctx, evolve_fn=evolve)
    assert len(evolve.calls) == 1
    assert run_ctx.state.phase == "playing"


def test_t06_10_default_tier_114_le_120(tmp_path, mk_runner):
    """D-1 默认档：``K=30,H=40,S=2,C=1`` → 114 ≤ 120，**不**触发闸门；

    把 H 改成 50（E_gen=134）后则发 ``loop.budget_exhausted{e_gen:134,cap:120}``。
    （同时锁住「H 每 +10 在 C=1 下是 +20 局」的算式。）
    """
    default = LoopConfig(k=30, heldout_n=40, smoke_n=2, n_candidates=1,
                         max_episodes_per_generation=120, max_generations=3)
    assert episodes_per_generation(default) == 114
    runner, run_ctx = mk_runner(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), loop_cfg=default)
    evolve = RecordingEvolve()
    run_ctx.state = _state_at_cycle(30)
    maybe_run_generation(state=run_ctx.state, run_ctx=run_ctx, evolve_fn=evolve)
    assert len(evolve.calls) == 1, "114 ≤ 120 必须正常触发 MOD-07"
    assert run_ctx.state.phase == "playing"

    tight = LoopConfig(k=30, heldout_n=50, smoke_n=2, n_candidates=1,
                       max_episodes_per_generation=120, max_generations=3)
    assert episodes_per_generation(tight) == 134
    run_ctx.config = tight
    run_ctx.state = _state_at_cycle(30)
    maybe_run_generation(state=run_ctx.state, run_ctx=run_ctx, evolve_fn=evolve)
    assert len(evolve.calls) == 1, "134 > 120 必须拒绝启动该代"
    assert run_ctx.state.phase == "failed"


def test_t06_10_budget_event_payload(tmp_path, mk_runner, monkeypatch):
    """``loop.budget_exhausted{e_gen, cap}`` 的载荷必须可被 MOD-09 校验。"""
    seen: list[dict] = []

    from dice_ratchet.infra import events

    real_emit = events.emit

    def spy(event, **fields):
        if event == "loop.budget_exhausted":
            seen.append({"event": event, **fields})
        return real_emit(event, **fields)

    monkeypatch.setattr(events, "emit", spy)
    cfg = LoopConfig(k=30, heldout_n=50, smoke_n=2, n_candidates=1,
                     max_episodes_per_generation=120)
    runner, run_ctx = mk_runner(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), loop_cfg=cfg)
    run_ctx.state = _state_at_cycle(30)
    maybe_run_generation(state=run_ctx.state, run_ctx=run_ctx, evolve_fn=RecordingEvolve())
    assert seen == [{"event": "loop.budget_exhausted", "e_gen": 134, "cap": 120}]


def test_t06_10_cycle_start_event_carries_est_hours(tmp_path, mk_runner, monkeypatch):
    """每代开始写 ``loop.cycle_start{e_gen, est_hours}``（FR-06-13 第三条）。"""
    seen: list[dict] = []
    from dice_ratchet.infra import events

    real_emit = events.emit

    def spy(event, **fields):
        if event == "loop.cycle_start":
            seen.append({"event": event, **fields})
        return real_emit(event, **fields)

    monkeypatch.setattr(events, "emit", spy)
    cfg = LoopConfig(k=30, heldout_n=40, smoke_n=2, n_candidates=1,
                     max_episodes_per_generation=120)
    runner, run_ctx = mk_runner(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), loop_cfg=cfg)
    run_ctx.state = _state_at_cycle(30)
    maybe_run_generation(state=run_ctx.state, run_ctx=run_ctx, evolve_fn=RecordingEvolve())
    assert seen and seen[0]["e_gen"] == 114
    # Spec §8.6：114 局 ≈ 37 h/代（按 n=10 实测 19.6 min/局）
    assert seen[0]["est_hours"] == pytest.approx(estimated_hours(114), rel=0.01)
    assert 30.0 < seen[0]["est_hours"] < 45.0


def test_t06_10_budget_gate_exit_code_is_1(tmp_path, mk_orchestrator, mk_memory):
    """预算闸门 → ``run()`` 退出码 1（FR-06-13）。"""
    evolve = RecordingEvolve()
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=evolve, seeds=(1000, 1001))
    reconfigure(orch, ctx, k=2, reflect_enabled=False, heldout_n=40, smoke_n=2,
                n_candidates=1, max_episodes_per_generation=4)
    summary = orch.run(seeds=[1000, 1001])
    assert summary.exit_code == 1
    assert summary.phase == "failed"
    assert ctx.state.generation == 0


# =====================================================================
# T-06-11 训练并发守卫
# =====================================================================

def test_t06_11_train_concurrency_guard_config_layer():
    """给定 ``train_concurrency=2``；当校验配置；则 ``E_IO_CONCURRENCY``（退出码 2）。"""
    cfg = LoopConfig(train_concurrency=2)
    with pytest.raises(InfraError) as excinfo:
        cfg.validate()
    assert excinfo.value.code is E.E_IO_CONCURRENCY
    assert error_code_of(excinfo.value.code) == EXIT_CONFIG


def test_t06_11_train_concurrency_guard_cli_layer():
    """CLI 侧同样拒绝（即使绕过了 dataclass 校验）。"""
    from dice_ratchet.loop.cli import _guard_concurrency

    class _Cfg:
        loop = LoopConfig(train_concurrency=2)

    with pytest.raises(InfraError) as excinfo:
        _guard_concurrency(_Cfg())
    assert excinfo.value.code is E.E_IO_CONCURRENCY
    assert error_code_of(excinfo.value.code) == 2


def test_t06_11_eval_concurrency_allowed():
    """``eval_concurrency`` 是局级并发（FR-06-14），1..8 合法。"""
    LoopConfig(eval_concurrency=2).validate()
    LoopConfig(eval_concurrency=8).validate()


# =====================================================================
# F34：``--resume`` 落在「队列已空、周期边界未完成」时必须重入边界
# =====================================================================

def _crash_at_boundary(ctx) -> None:
    """伪造 2026-10-09 实跑崩溃后的现场（状态取自 ``runs/m2-s32b-smoke-c1-medium6``）。

    ``completed`` 恰好是 k 的整数倍、``pending_queue`` 空、``phase="evolving"``、
    ``evolving_cycle`` 悬着、``generation`` 未推进 —— ``last_completed_cycle`` 尚未写，
    因为 ``maybe_run_generation`` 是在 ``_call_evolve`` **返回之后**才写它（崩溃在之前）。
    """
    state = ctx.state
    state.completed_train_seeds = [1000, 1001, 1002]
    state.pending_queue = []
    state.phase = "evolving"
    state.evolving_cycle = 1
    state.last_completed_cycle = None
    state.generation = 0


def test_f34_resume_reenters_boundary_when_queue_empty(tmp_path, mk_orchestrator,
                                                       mk_memory):
    """F34：空队列下的 ``resume`` 必须重入周期边界，而不是静默 ``phase="done"``。

    修复前 ``_loop`` 是 ``while state.pending_queue:``，空队列一次都不进 ⇒ 直接落到
    ``phase="done"``、**退出码 0**，而 ``evolve_fn`` 一次都没被调用 ⇒ 对外是「成功」，
    实际 ``generation`` 一步没走、``harness/versions/`` 一个新版本都没有。这正是 §3.1
    要防的静默成功（与 F28 同类）。
    """
    evolve = RecordingEvolve()
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=evolve, seeds=(1000, 1001, 1002))
    reconfigure(orch, ctx, k=3, reflect_enabled=False, max_generations=1)
    _crash_at_boundary(ctx)

    summary = orch.run(resume=True)

    assert len(evolve.calls) == 1, f"空队列下必须重入边界恰好一次，实际 {evolve.calls}"
    assert ctx.state.generation == 1, "generation 必须推进（修复前恒为 0）"
    assert ctx.state.last_completed_cycle == 1
    assert ctx.state.evolving_cycle is None, "崩溃遗留的 evolving_cycle 必须清掉"
    assert summary.phase == "done"

    # 幂等：再 resume 一次不得重复触发（FR-06-6 的 last_completed_cycle / generation 闸）
    orch.run(resume=True)
    assert len(evolve.calls) == 1, "同一 cycle 至多触发一次"
