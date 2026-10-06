"""MOD-06 HITL 挂起/恢复与 INV-5 split 越权（PRD §11 的 T-06-9 / T-06-12）。"""

from __future__ import annotations

import pytest

from fakes import FakeEnv, FakePolicy
from gamer_rsi.infra.errors import E, InfraError, error_code_of
from gamer_rsi.loop.generation import EvolveOutcome
from gamer_rsi.loop.state import load_state, scoreboard_path, Scoreboard
from loop_support import REPO_ROOT, build_runner, reconfigure, write_harness


def _pending_evolve(candidate_dir: str = "generations/gen-001", report: str = "report.json"):
    def evolve(state, run_ctx):
        return EvolveOutcome(decision="accept", generation=int(state.generation) + 1,
                             harness_version=int(state.harness_version) + 1,
                             pending_human=True, candidate_dir=candidate_dir,
                             report=report, reason="等人工裁决")

    return evolve


# =====================================================================
# T-06-9 HITL 挂起/恢复
# =====================================================================

def test_t06_9_hitl_suspends_then_resumes(tmp_path, mk_orchestrator, mk_memory):
    """给定 ``hitl=true``、假 ``evolve_fn`` 返回 ``pending_human=True``；当 ``run()``；

    则 ``phase=="hitl_wait"``、``hitl_pending.cycle`` 正确、退出码 0；
    当写好新 harness 后 ``--resume``；则 ``hitl_pending is None`` 且
    ``harness_version`` 为新值。
    """
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=_pending_evolve(), seeds=(1000, 1001),
                                harness_version=0)
    reconfigure(orch, ctx, k=2, reflect_enabled=False, hitl=True)

    summary = orch.run(seeds=[1000, 1001])

    assert summary.exit_code == 0, "HITL 挂起是正常结束、非错误"
    assert summary.phase == "hitl_wait"
    assert ctx.state.hitl_pending is not None
    assert ctx.state.hitl_pending["cycle"] == 1
    assert ctx.state.hitl_pending["generation"] == 1
    assert ctx.state.hitl_pending["candidate_dir"] == "generations/gen-001"
    assert ctx.state.hitl_pending["report"] == "report.json"
    assert ctx.state.generation == 1
    assert ctx.state.phase == "hitl_wait"
    # 状态已落盘，可跨进程续跑
    assert load_state(ctx.paths).hitl_pending == ctx.state.hitl_pending

    # ---- 人工执行 evolve approve 后切换 harness，再 --resume -----------------
    write_harness(tmp_path, version=1)
    reloaded = load_state(ctx.paths)
    runner2, ctx2 = build_runner(tmp_path, policy=FakePolicy(),
                                 env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                 loop_cfg=orch.config, state=reloaded, harness_version=1)
    from gamer_rsi.loop.generation import LoopOrchestrator

    orch2 = LoopOrchestrator(run_ctx=ctx2, config=orch.config, runner=runner2,
                             evolve_fn=_pending_evolve(),
                             scoreboard=Scoreboard(scoreboard_path(ctx2.paths)))
    summary2 = orch2.run(None, resume=True)

    assert summary2.exit_code == 0
    assert ctx2.state.hitl_pending is None
    assert ctx2.state.harness_version == 1, "resume 必须重读 harness/version.json（FR-06-7）"
    assert ctx2.state.phase == "done"


def test_t06_9_hitl_off_does_not_suspend(tmp_path, mk_orchestrator, mk_memory):
    """``loop.hitl=false`` 时同一个 ``pending_human`` 结果不挂起（直接继续）。"""
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                evolve_fn=_pending_evolve(), seeds=(1000, 1001))
    reconfigure(orch, ctx, k=2, reflect_enabled=False, hitl=False)
    summary = orch.run(seeds=[1000, 1001])
    assert summary.exit_code == 0
    assert ctx.state.hitl_pending is None
    assert ctx.state.phase == "done"


def test_t06_9_evolving_cycle_artifacts_short_circuit(tmp_path, mk_runner, mk_memory):
    """进程在 ``evolving`` 阶段被杀：``--resume`` 检查 MOD-07 产物存在性，

    存在 → 不重跑 MOD-07，标该 cycle 完成并 ``generation += 1``；
    不存在 → 重置 ``phase="playing"`` 后重新触发（FR-06-6 的三重校验）。
    """
    from gamer_rsi.loop.generation import maybe_run_generation
    from gamer_rsi.loop.state import RunState

    # ---- 产物存在 -------------------------------------------------------
    runner, run_ctx = mk_runner(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory())
    from gamer_rsi.infra.config import LoopConfig

    run_ctx.config = LoopConfig(k=2, heldout_n=4, smoke_n=2, n_candidates=1)
    (tmp_path / "r-loop-test" / "generations" / "gen-001").mkdir(parents=True,
                                                                 exist_ok=True)
    (tmp_path / "r-loop-test" / "generations" / "gen-001" / "report.json").write_text(
        "{}", encoding="utf-8")
    state = RunState(run_id="r-loop-test", phase="evolving", k=2, generation=0,
                     evolving_cycle=1, completed_train_seeds=[1000, 1001])
    calls: list[int] = []

    def evolve(_state, _ctx):
        calls.append(1)
        return EvolveOutcome()

    maybe_run_generation(state=state, run_ctx=run_ctx, evolve_fn=evolve)
    assert calls == [], "产物已存在 → 绝不重跑 MOD-07"
    assert state.generation == 1
    assert state.last_completed_cycle == 1
    assert state.evolving_cycle is None
    assert state.phase == "playing"

    # ---- 产物不存在 -----------------------------------------------------
    (tmp_path / "r-loop-test" / "generations" / "gen-001" / "report.json").unlink()
    state2 = RunState(run_id="r-loop-test", phase="evolving", k=2, generation=0,
                      evolving_cycle=1, completed_train_seeds=[1000, 1001])
    maybe_run_generation(state=state2, run_ctx=run_ctx, evolve_fn=evolve)
    assert calls == [1], "产物缺失 → 重新触发一次 MOD-07"
    assert state2.generation == 1
    assert state2.last_completed_cycle == 1


# =====================================================================
# T-06-12 split 越权（INV-5 回归）
# =====================================================================

@pytest.mark.parametrize("seed", [2000, 9000])
def test_t06_12_heldout_seed_in_train_position_raises(tmp_path, mk_orchestrator,
                                                      mk_memory, monkeypatch, seed):
    """给定 seed 标 ``heldout``/``test`` 却被当训练局传入；当 run；

    则抛 ``E_EVAL_SPLIT_VIOLATION``（INV-5 违反，P0）。
    """
    monkeypatch.chdir(REPO_ROOT)
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                seeds=())
    reconfigure(orch, ctx, k=99, reflect_enabled=False)
    with pytest.raises(InfraError) as excinfo:
        orch.run(seeds=[seed])
    assert excinfo.value.code is E.E_EVAL_SPLIT_VIOLATION
    assert error_code_of(excinfo.value.code) == 4, "守卫违例 → 退出码 4"
    # P0：立即中止，不得启动任何一局
    assert ctx.state.completed_train_seeds == []


def test_t06_12_cli_seeds_prefix_rejected(tmp_path, monkeypatch):
    """``--seeds heldout:2000-2001`` 在 CLI 解析层即被拒（INV-5）。"""
    import argparse

    from gamer_rsi.loop.cli import _resolve_seeds

    monkeypatch.chdir(REPO_ROOT)
    args = argparse.Namespace(seeds="heldout:2000-2001")
    with pytest.raises(InfraError) as excinfo:
        _resolve_seeds(args, object(), resume=False)
    assert excinfo.value.code is E.E_EVAL_SPLIT_VIOLATION


def test_t06_12_train_seeds_accepted(tmp_path, monkeypatch):
    """train 前缀 / 无前缀的 train seed 正常接受。"""
    import argparse

    from gamer_rsi.loop.cli import _resolve_seeds

    monkeypatch.chdir(REPO_ROOT)
    args = argparse.Namespace(seeds="train:1000-1002")
    assert _resolve_seeds(args, object(), resume=False) == [1000, 1001, 1002]
