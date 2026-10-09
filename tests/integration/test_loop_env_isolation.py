"""F30 回归：MOD-06 的评估局并发不得让两局共享环境状态（FR-06-14）。

现场（``runs/m2-s32-smoke-c1-medium6/``，2026-10-09）：``loop run`` 的 gen-000 基线经
``evaluate_arm(n_workers=2)`` 驱动**同一个** ``EpisodeRunner``，而 runner 只持有一个
``SandboxEnv`` 实例，且 ``close_env=False`` 关掉了按局关闭。两局的 ``reset``/``step``
交错 ⇒ 观测串局 ⇒ 模型按**别的局**的相位出动作 ⇒ 语义非法 ⇒ 重试耗尽 ⇒ fallback。
同一臂、同一模型、同一难度、同 seed 的 ``play`` 路径（MOD-08 ``SandboxRunner`` 每局
新建 env）实测干净（``fallback_rate`` 0.000 / ``illegal_rate`` 0.032），而 ``loop run``
的 parent 臂是 0.290 / 0.2853 —— 差在代码路径，不在模型。

本文件从**组合根**（``loop.cli._build_orchestrator``）装配，断言的是**不变量**而非修法：
无论将来用「每局新建」「锁串行化」还是别的隔离手段，只要
「评估局之间环境状态互不污染」与「并发度不改变逐局结果」被破坏就会红。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dice_ratchet.evals import evaluate_arm
from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol
from dice_ratchet.infra import cli as infra_cli
from dice_ratchet.loop import cli as loop_cli
from dice_ratchet.loop.runner import EvalRunnerAdapter
from loop_support import REPO_ROOT

#: 6 局 = 3 轮「2 个 worker 同时在飞」，足以暴露交错（现场 40 局里 13 局被污染）。
SEEDS = (2000, 2001, 2002, 2003, 2004, 2005)

#: 逐局一致性要比对的字段。刻意不只看胜负：6 局尺度上，污染常常只表现为
#: 非法动作／步数塌缩／``decisionIndex`` 取自另一个环境状态，胜负反而可能不变。
_RESULT_FIELDS = ("steps", "decisions", "illegal_attempts", "fallback_decisions",
                  "outcome", "stages_cleared", "battles_won", "truncation_reason",
                  "seq_hash")


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """按生产路径装配：真 parser → ``cli_init_run`` → ``_build_orchestrator``。

    ``--agent heuristic`` + ``--no-evolve --no-reflect``：免模型、免网络，但**装配路径
    与 ``loop run --agent llm`` 完全同一条**——这正是 F30 能逃走的原因：冒烟沿用 llm
    臂，而 llm 臂每局墙钟很长，串局只表现为高非法率与高 fallback，既不报错也不退出非 0。
    """
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("DICERATCHET_INFRA__RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("DICERATCHET_ENV__EPISODE__DIFFICULTY", "medium")
    monkeypatch.setenv("DICERATCHET_ENV__EPISODE__STAGE_COUNT", "6")
    args = infra_cli.build_parser().parse_args(
        ["loop", "run", "--agent", "heuristic", "--no-evolve", "--no-reflect",
         "--seeds", "train:1000", "--k", "1", "--generations", "1",
         "--run-id", "r-f30", "--config-dir", str(REPO_ROOT / "configs")])
    paths, cfg, run_id = infra_cli.cli_init_run(args)
    return loop_cli._build_orchestrator(paths, cfg, run_id, args)


def _eval(run_ctx: Any, orch: Any, *, n_workers: int, generation: int) -> dict[int, Any]:
    """在 ``H1`` 的显式 seed 子集上跑一臂，返回 ``{seed: EpisodeResult}``。"""
    proto = validate_protocol(coerce_protocol(None))
    arm = evaluate_arm(
        run_ctx.harness_dir, split="heldout", basket=SEEDS, memory_version=0,
        arm="parent", run_id=run_ctx.run_id, protocol=proto,
        config=run_ctx.app_config, generation=generation, harness_version=0,
        runner=EvalRunnerAdapter(orch.runner), n_workers=n_workers,
        episode_config=run_ctx.episode_config)
    return {int(r.seed): r for r in arm.results}


def test_runner_does_not_hold_one_shared_env(tmp_path, monkeypatch):
    """评估局并发时 runner 不得持有一个跨局复用的 ``SandboxEnv``（F30 的结构面）。"""
    orch, _run_ctx = _build(tmp_path, monkeypatch)
    runner = orch.runner
    assert runner.env is None, (
        "EpisodeRunner 持有了单个 SandboxEnv 实例：evaluate_arm 会拿它跑全部评估局，"
        "两局的 reset/step 将交错并串局（F30）")
    assert runner.env_factory is not None, (
        "EpisodeRunner 既没有共享 env 也没有 env_factory：每局环境来源未定义")


def test_concurrency_does_not_change_per_episode_results(tmp_path, monkeypatch):
    """``n_workers=2`` 与 ``n_workers=1`` 的逐局结果必须逐字段一致（NFR-06-1 / F30）。

    串局会同时污染两件事：动作合法性（模型读到别的局的观测）与观测自身
    （``decisionIndex``/``episodeId`` 来自另一个环境状态，进而改变 ``seq_hash``）。
    故比对整条结果而不只看胜率。
    """
    orch, run_ctx = _build(tmp_path, monkeypatch)
    serial = _eval(run_ctx, orch, n_workers=1, generation=1)
    concurrent = _eval(run_ctx, orch, n_workers=2, generation=2)

    assert set(serial) == set(SEEDS) == set(concurrent)
    diffs = {seed: {name: (getattr(serial[seed], name), getattr(concurrent[seed], name))
                    for name in _RESULT_FIELDS
                    if getattr(serial[seed], name) != getattr(concurrent[seed], name)}
             for seed in SEEDS}
    diffs = {seed: d for seed, d in diffs.items() if d}
    assert not diffs, f"并发改变了逐局结果（环境状态被另一局改写，F30）：{diffs}"

    dirty = {seed: r.illegal_attempts for seed, r in concurrent.items()
             if r.illegal_attempts}
    assert not dirty, f"并发评估出现非法动作（读到别的局的观测，F30）：{dirty}"
