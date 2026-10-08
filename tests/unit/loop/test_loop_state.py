"""MOD-06 run 状态机、断点续跑、计分幂等与 schema round-trip。

覆盖 PRD §11 的 T-06-7 / T-06-8 / T-06-13。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakes import FakeEnv, FakePolicy
from dice_ratchet.infra.io import validate
from dice_ratchet.loop.state import RunState, load_state, save_state, scoreboard_path
from loop_support import (RUN_ID, build_paths, build_runner, end_row, reconfigure,
                          scoreboard_rows, step_rows)


def _episode_path(root: Path, split: str, key: str) -> Path:
    return Path(root) / RUN_ID / "episodes" / split / f"{key}.jsonl"


# =====================================================================
# T-06-7 断点续跑不重复计分
# =====================================================================

def test_t06_7_resume_does_not_rescore(tmp_path, mk_orchestrator, mk_memory):
    """给定 ``k=3`` 跑到第 4 局后中断（状态已落盘）；当 ``--resume``；

    则 ``completed_train_seeds`` 无重复、``scoreboard.episodes`` 的 ``episode_key``
    全局唯一、总局数等于期望值。
    """
    seeds = list(range(1000, 1006))
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                seeds=tuple(seeds), evolve_fn=_noop_evolve)
    reconfigure(orch, ctx, k=3, reflect_enabled=False, max_generations=5)

    real = orch.runner.run_episode
    calls = {"n": 0}

    def flaky(seed, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("模拟进程被杀（第 4 局中断）")
        return real(seed, **kwargs)

    orch.runner.run_episode = flaky
    with pytest.raises(RuntimeError):
        orch.run(seeds=seeds)

    assert calls["n"] == 4
    assert ctx.state.completed_train_seeds == [1000, 1001, 1002]
    assert ctx.state.pending_queue == [1003, 1004, 1005]
    # 状态已落盘（可被 --resume 独立重建）
    reloaded = load_state(ctx.paths)
    assert reloaded.completed_train_seeds == [1000, 1001, 1002]
    assert reloaded.phase == "playing"

    # ---- 恢复 ---------------------------------------------------------
    runner2, ctx2 = build_runner(tmp_path, policy=FakePolicy(),
                                 env=FakeEnv(steps_to_terminate=5), memory=mk_memory(),
                                 loop_cfg=orch.config, state=reloaded)
    from dice_ratchet.loop.generation import LoopOrchestrator
    from dice_ratchet.loop.state import Scoreboard

    orch2 = LoopOrchestrator(run_ctx=ctx2, config=orch.config, runner=runner2,
                             evolve_fn=_noop_evolve,
                             scoreboard=Scoreboard(scoreboard_path(ctx2.paths)))
    summary = orch2.run(None, resume=True)

    assert summary.exit_code == 0
    assert summary.phase == "done"
    assert ctx2.state.completed_train_seeds == seeds
    assert len(set(ctx2.state.completed_train_seeds)) == len(seeds)
    rows = scoreboard_rows(ctx2)
    keys = [r["episode_key"] for r in rows]
    assert len(keys) == len(set(keys)) == 6, f"计分幂等被破坏：{keys}"
    # 前 3 局在 gen-000；第 3 局收尾触发周期边界后 generation=1，其余 3 局属 gen-001
    # （§7.3：同一 seed 在不同代各计一条记录）
    assert set(keys) == ({f"e-{s:06d}-g000-train" for s in seeds[:3]}
                         | {f"e-{s:06d}-g001-train" for s in seeds[3:]})


def _noop_evolve(state, run_ctx):
    from dice_ratchet.loop.generation import EvolveOutcome

    return EvolveOutcome(decision="reject", generation=int(state.generation) + 1,
                         harness_version=int(state.harness_version))


def test_t06_7_scoreboard_duplicate_key_is_skipped(tmp_path, mk_orchestrator, mk_memory):
    """同一 ``episode_key`` 重复计分只保留一条（FR-06-10）。"""
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                seeds=(1000,), evolve_fn=_noop_evolve)
    reconfigure(orch, ctx, k=99, reflect_enabled=False)
    orch.run(seeds=[1000])
    assert len(scoreboard_rows(ctx)) == 1

    # 人为把该 episode 放回队列并重跑：计分必须幂等
    ctx.state.pending_queue = [1000]
    ctx.state.completed_train_seeds = []
    orch.run(None, resume=True)
    rows = scoreboard_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["superseded_by"] is None


def test_t06_7_force_rerun_supersedes_not_deletes(tmp_path, mk_orchestrator, mk_memory):
    """``--force-rerun`` 时旧记录标 ``superseded_by`` 而非删除（FR-06-10）。"""
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                seeds=(1000,), evolve_fn=_noop_evolve)
    reconfigure(orch, ctx, k=99, reflect_enabled=False)
    orch.run(seeds=[1000])
    ctx.state.pending_queue = [1000]
    ctx.state.completed_train_seeds = []
    orch.run(None, resume=True, force_rerun=True)
    rows = scoreboard_rows(ctx)
    assert len(rows) == 2
    assert rows[0]["superseded_by"] == "e-001000-g000-train"
    assert rows[1]["superseded_by"] is None


# =====================================================================
# T-06-8 中断粒度
# =====================================================================

def test_t06_8_interrupt_mid_episode_then_resume(tmp_path, mk_orchestrator, mk_memory):
    """给定第 7 局第 3 步后抛 ``KeyboardInterrupt``；当恢复；

    则前 2 步已在 JSONL、该 seed 从 ``pending_queue`` 头部重跑且不产生重复
    ``episode_key``。
    """
    seeds = list(range(1000, 1007))
    key = "e-001006-g000-train"

    class InterruptingPolicy(FakePolicy):
        def __init__(self) -> None:
            super().__init__()
            self.per_episode: dict[str, int] = {}

        def play_decision(self, env, obs, *, ctx):
            done = self.per_episode.get(ctx.episode, 0)
            if ctx.seed == 1006 and done >= 2:
                raise KeyboardInterrupt
            self.per_episode[ctx.episode] = done + 1
            return super().play_decision(env, obs, ctx=ctx)

    policy = InterruptingPolicy()
    orch, ctx = mk_orchestrator(tmp_path, policy=policy,
                                env=FakeEnv(steps_to_terminate=5), memory=mk_memory(),
                                seeds=tuple(seeds), evolve_fn=_noop_evolve)
    reconfigure(orch, ctx, k=99, reflect_enabled=False)

    summary = orch.run(seeds=seeds)

    assert summary.exit_code == 130
    assert ctx.state.interrupted_at is not None
    path = _episode_path(tmp_path, "train", key)
    rows = step_rows(path)
    assert len(rows) == 2, f"中断时磁盘上应恰好留 2 步，实际 {len(rows)}"
    assert not any(r.get("type") == "episode_end" for r in step_rows(path))
    assert ctx.state.pending_queue[0] == 1006
    assert ctx.state.completed_train_seeds == seeds[:6]
    assert load_state(ctx.paths).pending_queue[0] == 1006

    # ---- 恢复 ---------------------------------------------------------
    reloaded = load_state(ctx.paths)
    runner2, ctx2 = build_runner(tmp_path, policy=FakePolicy(),
                                 env=FakeEnv(steps_to_terminate=5), memory=mk_memory(),
                                 loop_cfg=orch.config, state=reloaded)
    from dice_ratchet.loop.generation import LoopOrchestrator
    from dice_ratchet.loop.state import Scoreboard

    orch2 = LoopOrchestrator(run_ctx=ctx2, config=orch.config, runner=runner2,
                             evolve_fn=_noop_evolve,
                             scoreboard=Scoreboard(scoreboard_path(ctx2.paths)))
    summary2 = orch2.run(None, resume=True)

    assert summary2.exit_code == 0
    assert ctx2.state.completed_train_seeds == seeds
    rows = step_rows(_episode_path(tmp_path, "train", key))
    # 重跑后该局文件被整体重写：5 步完整序列，中断时那 2 行孤儿不再存在
    assert len(rows) == 5
    assert [r["step"] for r in rows] == [0, 1, 2, 3, 4]
    assert end_row(_episode_path(tmp_path, "train", key))["outcome"] in ("win", "lose")
    keys = [r["episode_key"] for r in scoreboard_rows(ctx2)]
    assert len(keys) == len(set(keys)) == 7


def test_t06_8_interrupt_before_any_step(tmp_path, mk_orchestrator, mk_memory):
    """在第一局第一个决策前中断：state.json 仍可被 --resume 独立重建。"""
    class ImmediateInterrupt(FakePolicy):
        def play_decision(self, env, obs, *, ctx):
            raise KeyboardInterrupt

    orch, ctx = mk_orchestrator(tmp_path, policy=ImmediateInterrupt(),
                                env=FakeEnv(steps_to_terminate=2), memory=mk_memory(),
                                seeds=(1000, 1001), evolve_fn=_noop_evolve)
    reconfigure(orch, ctx, k=99, reflect_enabled=False)
    summary = orch.run(seeds=[1000, 1001])
    assert summary.exit_code == 130
    state = load_state(ctx.paths)
    assert state.pending_queue == [1000, 1001]
    assert state.completed_train_seeds == []
    assert state.tick == 0


# =====================================================================
# T-06-13 Schema round-trip（G5）
# =====================================================================

def test_t06_13_state_roundtrip_and_schema(tmp_path):
    """``RunState → JSON → RunState`` 完全相等，且通过 §6 JSON Schema 校验。"""
    state = RunState(run_id="r-round", phase="playing", generation=3, harness_version=7,
                     k=30, tick=97, completed_train_seeds=[1000, 1001, 1002],
                     pending_queue=[1003, 1004], last_completed_cycle=1,
                     evolving_cycle=None, memory_version=2, resume_count=1,
                     hitl_pending={"cycle": 1, "generation": 3, "candidate_dir": "g",
                                   "report": "r"},
                     interrupted_at="2026-10-06T00:00:00Z",
                     updated_at="2026-10-06T00:00:00Z")
    payload = state.to_dict()
    validate("run_state", payload)
    again = RunState.from_dict(payload)
    assert again.to_dict() == payload
    assert again == state


def test_t06_13_save_load_roundtrip(tmp_path):
    """``save_state`` → ``load_state`` 等价，且落盘文件过 schema（G5）。"""
    paths = build_paths(tmp_path)
    state = RunState(run_id=RUN_ID, phase="reflecting", generation=1, k=3, tick=4,
                     completed_train_seeds=[1000], pending_queue=[1001, 1002],
                     last_completed_cycle=0)
    save_state(paths, state)
    raw = json.loads(Path(paths.state).read_text(encoding="utf-8"))
    validate("run_state", raw)
    assert load_state(paths) == state


def test_t06_13_corrupt_state_is_rejected(tmp_path):
    """损坏的 state.json 立即失败（``E_IO_RUN_STATE``），不能带着半截状态续跑。"""
    from dice_ratchet.infra.errors import E, InfraError

    paths = build_paths(tmp_path)
    Path(paths.state).write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    with pytest.raises(InfraError) as excinfo:
        load_state(paths)
    assert excinfo.value.code is E.E_IO_RUN_STATE


def test_t06_13_schema_files_registered(tmp_path):
    """G5：§6 的三个 JSON Schema 落在 ``schemas/`` 并已登记错误码。"""
    from dice_ratchet.infra.io import _SCHEMA_ERROR_CODES

    for name in ("run_state", "scoreboard", "loop_reflection"):
        assert name in _SCHEMA_ERROR_CODES
        validate(name, _sample_payload(name))


def _sample_payload(name: str) -> dict:
    if name == "run_state":
        return RunState(run_id="r", phase="pending", updated_at="2026-10-06T00:00:00Z").to_dict()
    if name == "scoreboard":
        return {"episodes": [{
            "episode_key": "e-001000-g000-train", "seed": 1000, "split": "train",
            "generation": 0, "harness_version": 0, "outcome": "win",
            "truncation_reason": None, "steps": 3, "decisions": 3, "cum_reward": 0.0,
            "stages_cleared": 6, "hearts_left": 2, "battles_won": 9,
            "illegal_attempts": 0, "fallback_decisions": 0, "superseded_by": None}]}
    return {"reflection_id": None, "episode_key": "e-001000-g000-train", "seed": 1000,
            "generation": 0, "harness_version": 0, "tick": 0, "outcome": "lose",
            "truncation_reason": None, "status": "skipped", "reason": "E_LLM_TIMEOUT",
            "entry": None, "llm": None, "template_sha256": "sha256:" + "0" * 64}
