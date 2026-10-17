"""``loop run|status|smoke`` 的实现（Spec §4.6；经组合根注册进 MOD-09 命令面）。

可复现命令（MOD-06 PRD §3，G6 必须可直接复制执行）::

    python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl
    python -m dice_ratchet loop status --run-id <run_id>
    python -m dice_ratchet loop run --run-id <run_id> --resume
    python -m dice_ratchet loop smoke --seeds train:1000-1002 --k 3 --no-reflect

退出码（Spec §8.3 / PRD §10）：``0`` 成功（含 HITL 挂起）/ ``1`` 预算闸门等领域失败 /
``2`` 配置错误 / ``3`` 模型不可用 / ``4`` 沙箱守卫违例 / ``5`` IO 落盘失败 / ``130`` Ctrl-C。

NFR-09-5：``infra`` 不 import 任何子包；本文件是 MOD-06 侧的命令实现，由
``dice_ratchet/app.py`` 的 ``wire()`` 注册。MOD-03 客户端与 MOD-07 演化函数同样由组合根
经 ``set_inference_client_provider`` / ``set_evolve_provider`` 注入，避免反向依赖。

INV-1：本文件只经 ``dice_ratchet.sandbox.api`` 触达环境，**不** import ``dice_ratchet.env``。
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any, Callable

from ..infra import cli as infra_cli
from ..infra.errors import E, InfraError
from ..infra.run import RunPaths, read_state
from .generation import (LoopOrchestrator, ensure_evolution_worktree_clean,
                         write_gen000_baseline)
from .runner import EpisodeRunner, RunContext, read_harness_version
from .state import init_state, load_state

__all__ = ["HANDLERS", "cmd_loop_run", "cmd_loop_smoke", "cmd_loop_status",
           "get_evolve_provider", "set_evolve_provider", "set_inference_client_provider"]


# =====================================================================
# 组合根注入点（与 memory/evals 的 provider 模式一致）
# =====================================================================

_CLIENT_PROVIDER: Callable[..., Any] | None = None
_EVOLVE_PROVIDER: Callable[..., Any] | None = None


def set_inference_client_provider(provider: Callable[..., Any] | None) -> None:
    """注册 MOD-03 客户端工厂（签名 ``(cfg, *, run_id, runs_root) -> client``）。"""
    global _CLIENT_PROVIDER
    _CLIENT_PROVIDER = provider


def set_evolve_provider(provider: Callable[..., Any] | None) -> None:
    """注册 MOD-07 的 ``evolve_fn`` 工厂（签名 ``(cfg, runner, run_ctx) -> callable``）。"""
    global _EVOLVE_PROVIDER
    _EVOLVE_PROVIDER = provider


def get_evolve_provider() -> Callable[..., Any] | None:
    """当前注册的 MOD-07 工厂（``None`` = 未注册）。组合根用它避免覆盖显式注册。"""
    return _EVOLVE_PROVIDER


# =====================================================================
# 命令实现
# =====================================================================

def cmd_loop_run(args: argparse.Namespace) -> int:
    """``loop run``：跑（或续跑）N 代闭环。"""
    paths, cfg, run_id = infra_cli.cli_init_run(args)
    _guard_concurrency(cfg)
    resume = bool(getattr(args, "resume", False))
    orch, run_ctx = _build_orchestrator(paths, cfg, run_id, args)
    # F47-A：演化起跑前的 git 门禁——必须早于 gen-000 基线（那份也要花钱），
    # 否则「脏工作区」要等整代跑完、MOD-07 提交前才被拒（实测白花 3.75 h / ≈¥47）。
    if orch.evolve_fn is not None:
        ensure_evolution_worktree_clean(run_ctx)
    seeds = _resolve_seeds(args, orch.config, resume=resume)
    run_baseline = getattr(args, "baseline", None)
    if run_baseline is None:
        run_baseline = orch.evolve_fn is not None
    if not resume and run_baseline:
        # gen-000 基线（FR-06-13a）：只在真的会演化时默认执行；stub/--no-baseline 跳过
        write_gen000_baseline(run_ctx, harness_version=int(run_ctx.state.harness_version),
                              runner=orch.runner)
    summary = orch.run(seeds, resume=resume,
                       force_rerun=bool(getattr(args, "force_rerun", False)))
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if summary.phase == "hitl_wait":
        print("已挂起等人工裁决（退出码 0）："
              f"python -m dice_ratchet evolve approve|rollback --run-id {run_id} "
              f"--generation {summary.generation}；随后 "
              f"python -m dice_ratchet loop run --run-id {run_id} --resume", file=sys.stderr)
    return int(summary.exit_code)


def cmd_loop_baseline(args: argparse.Namespace) -> int:
    """``loop baseline``：对**给定 harness 目录**跑一次 held-out 测量（不训练、不演化）。

    为什么需要它（2026-10-17，协议口径纠正）：D-M3-7 里「候选补丁单独测 held-out」是一个
    独立动作——P1 没过门禁时，下一轮要测的是 ``v0+P2``（不是 ``v0+P1+P2``）。原先只有
    ``loop run`` 会在开跑时顺带测 gen-000 基线，且会被后续训练/演化绑在一起；本命令把
    「测一个 harness」抽出来：落 ``generations/gen-000/report.json`` + 臂归档，然后**停下**。
    幂等：报告已存在则直接返回（不重复花钱）。
    """
    paths, cfg, run_id = infra_cli.cli_init_run(args)
    _guard_concurrency(cfg)
    orch, run_ctx = _build_orchestrator(paths, cfg, run_id, args)
    basket = getattr(args, "basket", None)
    report_path = write_gen000_baseline(
        run_ctx, harness_version=int(run_ctx.state.harness_version),
        basket=basket, runner=orch.runner)
    if report_path is None:
        print(json.dumps({"run_id": run_id, "status": "unavailable",
                          "hint": "MOD-08/模型不可用：基线降级，不阻塞"}, ensure_ascii=False))
        return 0
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    heldout = report.get("heldout") or {}
    print(json.dumps({
        "run_id": run_id,
        "report": str(report_path),
        "decision": report.get("decision"),
        "harness_version": report.get("harness_version"),
        "harness_play_hash": report.get("harness_play_hash"),
        "heldout": {key: heldout.get(key) for key in
                    ("n", "win_rate", "win_rate_ci95", "mean_stages_cleared",
                     "mean_hearts_left", "truncation_rate", "illegal_rate", "source")},
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_loop_status(args: argparse.Namespace) -> int:
    """``loop status``：打印 ``state.json``（只读；不校验 config_hash、不写盘）。"""
    cfg = infra_cli.load_config(infra_cli.config_overrides(args),
                                config_dir=Path(args.config_dir))
    run_id = getattr(args, "run_id", None)
    if not run_id:
        raise InfraError(E.E_IO_CONFIG_INVALID, "loop status 必须指定 --run-id",
                         hint="用法：python -m dice_ratchet loop status --run-id <run_id>")
    root = Path(cfg.infra.runs_root) / str(run_id)
    if not root.is_dir():
        raise InfraError(E.E_IO_CONFIG_INVALID, f"run 目录不存在：{root}",
                         hint="检查 --run-id 是否正确")
    paths = RunPaths(root)
    if not paths.state.is_file():
        raise InfraError(E.E_IO_STATE_MISSING, f"state.json 不存在：{paths.state}",
                         hint="该 run 尚未启动（loop run 会先写 state.json）")
    state = load_state(paths)
    payload = read_state(paths)
    payload["completed"] = len(state.completed_train_seeds)
    payload["pending"] = len(state.pending_queue)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_loop_smoke(args: argparse.Namespace) -> int:
    """``loop smoke``：只验编排，不触网不调模型（``--no-reflect`` 免模型路径，NFR-06-6）。"""
    if getattr(args, "agent", None) in (None, "llm"):
        args.agent = "heuristic"
    args.no_reflect = True
    args.no_evolve = True
    if getattr(args, "generations", None) in (None, 3):
        args.generations = 1
    return cmd_loop_run(args)


HANDLERS: dict[tuple[str, ...], Callable[[argparse.Namespace], int]] = {
    ("loop", "run"): cmd_loop_run,
    ("loop", "status"): cmd_loop_status,
    ("loop", "baseline"): cmd_loop_baseline,
    ("loop", "smoke"): cmd_loop_smoke,
}


# =====================================================================
# 装配
# =====================================================================

def _loop_options(args: argparse.Namespace, cfg: Any) -> dict[str, Any]:
    """CLI → ``LoopConfig`` 的运行期覆盖（配置优先级：CLI > env > yaml）。"""
    overrides: dict[str, Any] = {}
    if getattr(args, "no_reflect", False):
        overrides["reflect_enabled"] = False
    if getattr(args, "retry_reflections", False):
        overrides["retry_reflections_on_resume"] = True
    if getattr(args, "hitl", None) is not None:
        overrides["hitl"] = bool(args.hitl)
    if getattr(args, "k", None) is not None:
        overrides["k"] = int(args.k)
    if getattr(args, "generations", None) is not None:
        overrides["max_generations"] = int(args.generations)
    return overrides


def _guard_concurrency(cfg: Any) -> None:
    """FR-06-14：``train_concurrency>1`` 启动即拒绝（``E_IO_CONCURRENCY``，退出码 2）。"""
    value = int(getattr(cfg.loop, "train_concurrency", 1))
    if value != 1:
        raise InfraError(
            E.E_IO_CONCURRENCY,
            f"loop.train_concurrency={value} 非法：训练局必须串行（固定 1）",
            hint="共享记忆写入无法在训练局之间并发（Spec §8.6 P-10 / FR-06-14）；"
                 "评估局的并发由 loop.eval_concurrency 单独控制")


def _resolve_seeds(args: argparse.Namespace, loop_cfg: Any, *,
                   resume: bool) -> list[int] | None:
    """``--seeds`` 解析；省略时按 MOD-08 ``seeds.take`` 取本 run 各代的 train 块。"""
    raw = getattr(args, "seeds", None)
    if raw:
        parsed = infra_cli.parse_seeds_spec(str(raw))
        declared = next((name for name, values in parsed.items()
                         if name != "default" and values), "")
        seeds = sorted({int(s) for values in parsed.values() for s in values})
        if declared and declared != "train":
            raise InfraError(
                E.E_EVAL_SPLIT_VIOLATION,
                f"--seeds 的 split 前缀是 {declared!r}，训练局只接受 train seed（INV-5）",
                hint="loop run 只消费 train 池；held-out/test 由 MOD-07/MOD-08 在评估口取用")
        return seeds
    if resume:
        return None                      # 续跑：以 state.json 的 pending_queue 为准
    from ..evals.seeds import coerce_protocol, take, validate_protocol

    proto = validate_protocol(coerce_protocol(None))
    seeds: list[int] = []
    for generation in range(1, int(getattr(loop_cfg, "max_generations", 3)) + 1):
        seeds.extend(take("train", generation=generation, protocol=proto))
    return seeds


def _build_orchestrator(paths: RunPaths, cfg: Any, run_id: str,
                        args: argparse.Namespace) -> tuple[LoopOrchestrator, RunContext]:
    options = _loop_options(args, cfg)
    loop_cfg = replace(cfg.loop, **options) if options else cfg.loop
    loop_cfg.validate()

    harness_dir = Path(getattr(args, "harness_dir", "harness"))
    memory = _build_memory(paths, cfg, run_id, args)
    agent_kind = str(getattr(args, "agent", "llm") or "llm")
    client = _build_client(cfg, run_id, paths, args, agent_kind=agent_kind)
    if agent_kind == "llm" and client is None:
        raise InfraError(
            E.E_LLM_UNAVAILABLE,
            f"loop run --agent {agent_kind} 需要可用的 MOD-03 推理客户端",
            hint="指定可加载的模型（如 --model qwen3-8b-q4；离线冒烟用 --model mock），"
                 "或改跑 --agent heuristic（或 loop smoke --no-reflect）")

    resume = bool(getattr(args, "resume", False))
    if resume:
        # 续跑：``k`` 以 state.json 为准（同一 run 的 k 不可在续跑时被改小/改大，
        # 否则周期边界会漂移）；CLI 的 --k 只对新建 run 生效。
        state = load_state(paths)
        if int(loop_cfg.k) != int(state.k):
            raise InfraError(
                E.E_IO_RUN_CONFLICT,
                f"--k {loop_cfg.k} 与 state.json 的 k={state.k} 不一致",
                hint="同一 run 的 K 必须恒定（周期幂等键 len(completed)//k）；"
                     "要改 K 请开新 run")
    else:
        state = init_state(run_id, k=int(loop_cfg.k), seeds=[],
                           harness_version=read_harness_version(harness_dir),
                           budget_cap=int(loop_cfg.max_episodes_per_generation))

    run_ctx = RunContext(run_id=run_id, paths=paths, config=loop_cfg, state=state,
                         app_config=cfg, episode_config=cfg.env.episode,
                         harness_dir=harness_dir,
                         memory_version=int(state.memory_version), memory=memory,
                         client=client)
    runner = EpisodeRunner(env_factory=_env_factory(cfg),
                           policy_factory=_policy_factory(cfg, client, agent_kind,
                                                          harness_dir),
                           memory=memory, writer=_build_writer(paths, loop_cfg),
                           config=loop_cfg, run_ctx=run_ctx, client=client)
    evolve_fn = None
    if not getattr(args, "no_evolve", False) and _EVOLVE_PROVIDER is not None:
        evolve_fn = _EVOLVE_PROVIDER(cfg, runner, run_ctx)
    orch = LoopOrchestrator(run_ctx=run_ctx, config=loop_cfg, runner=runner,
                            evolve_fn=evolve_fn)
    return orch, run_ctx


def _build_writer(paths: RunPaths, loop_cfg: Any) -> Any:
    from .trajectory import TrajectoryWriter

    return TrajectoryWriter(paths, io_retry=int(loop_cfg.io_retry),
                            io_backoff_ms=int(loop_cfg.io_backoff_ms),
                            max_run_bytes=int(loop_cfg.max_run_bytes))


def _build_memory(paths: RunPaths, cfg: Any, run_id: str,
                  args: argparse.Namespace) -> Any:
    if not bool(getattr(cfg.memory, "enabled", True)):
        return None
    try:
        from ..memory import MemoryStore

        return MemoryStore(paths.root, cfg.memory,
                           config_dir=Path(getattr(args, "config_dir", "configs")),
                           run_id=run_id)
    except Exception as exc: # noqa: BLE001 - 记忆是增强项：不可用即降级为不检索
        print(f"[{E.E_IO_MEMORY_WRITE_FAILED}] 记忆库不可用，本 run 关闭记忆：{exc}",
              file=sys.stderr)
        return None


def _build_client(cfg: Any, run_id: str, paths: RunPaths, args: argparse.Namespace,
                  *, agent_kind: str) -> Any:
    if agent_kind != "llm" or _CLIENT_PROVIDER is None:
        return None
    try:
        return _CLIENT_PROVIDER(cfg, run_id=run_id, runs_root=paths.root.parent)
    except InfraError:
        raise
    except Exception as exc: # noqa: BLE001 - 装配失败 → 视为模型不可用（退出码 3）
        raise InfraError(E.E_LLM_UNAVAILABLE,
                         f"MOD-03 客户端装配失败：{exc}",
                         hint="检查 inference.model_id 与权重是否已落盘") from exc


def _policy_factory(cfg: Any, client: Any, agent_kind: str,
                    harness_dir: Path) -> Callable[[int, str], Any]:
    """每个局一个 ``PolicyAgent``（FR-06-14：评估局并发时 MOD-04 不可共享实例）。"""
    from ..agent.policy import PolicyAgent

    base = cfg.agent.policy
    if agent_kind == "heuristic":
        base = replace(base, mode="baseline_heuristic", temperature=0.0)
    else:
        base = replace(base, mode="play")

    default_dir = Path(harness_dir)

    def factory(seed: int, _key: str, harness_dir: Path | str | None = None) -> Any:
        # F66（2026-10-14）：**按臂覆盖 harness 目录**。MOD-07 的候选臂跑在
        # ``generations/gen-NNN/staging/<id>/`` 上，而本工厂原先闭包在 run 级
        # ``harness/`` ⇒ 候选补丁从未进入决策 prompt（实测 40/40 个 seed 的
        # ``prompt_hash`` 与 parent 逐字节相同）。缺省仍用 run 级目录。
        target = default_dir if harness_dir is None else Path(harness_dir)
        return PolicyAgent(base, llm=client, memory=None, harness_dir=target)

    return factory


def _env_factory(cfg: Any) -> Callable[[int, str], Any]:
    """每个局一个 ``SandboxEnv``（与 ``_policy_factory`` 同理，FR-06-14 / F30）。

    ``SandboxEnv`` 是**有状态单实例**：``reset``/``step`` 会改写 ``_state``、
    ``_env_steps``、``_decision_index``、``_illegal_count``… 而 MOD-08 的
    ``evaluate_arm`` 用 ``n_workers``（默认 2）个线程驱动**同一个** runner。共享一个
    env 会让两局的 ``reset``/``step`` 交错，观测串局——模型按别的局的相位出动作 →
    语义非法 → 重试耗尽 → fallback（F30）。故此处按局新建，由
    ``EpisodeRunner._resolve_env`` 的 owned 路径在局末 ``close()``。

    构造代价实测 ≈1.1 ms/局（主因是配表加载），相对单局墙钟可忽略。
    """
    def factory(_seed: int, key: str) -> Any:
        return _make_env(cfg, episode_id=key)

    return factory


def _make_env(cfg: Any, *, episode_id: str) -> Any:
    """经 MOD-02 门面构造环境（INV-1：本模块不 import ``dice_ratchet.env``）。"""
    from ..sandbox.api import EpisodeConfig, make_default_env

    return make_default_env(episode_config=EpisodeConfig.from_any(cfg.env.episode),
                            episode_id=episode_id)
