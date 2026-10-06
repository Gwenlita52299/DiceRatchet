"""组合根（composition root）：把各模块的实现注册进 MOD-09 的命令面。

**为什么单独一个模块**：NFR-09-5 规定 ``gamer_rsi/infra`` 不得 import 任何其他子包，
由 ``guards.scan_layer_imports()`` 静态强制。因此"谁能把 ``MOD-01/02/...`` 的实现接到
``infra.cli`` 上"必须发生在本目录（顶层，不属于任何 L1–L3 层）。本模块是唯一允许
同时 import ``infra`` 与领域模块的位置。

CLI 入口：``python -m gamer_rsi ...`` → ``gamer_rsi/__main__.py`` → 本模块 ``main()``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .infra import cli
from .infra.config import load_config
from .infra.errors import E, InfraError
from .infra.run import RunPaths

__all__ = ["wire", "main"]

_WIRED = False


def wire() -> None:
    """把已落地模块的命令实现与提供者注册进 ``infra.cli``（幂等）。"""
    global _WIRED
    if _WIRED:
        return
    cli.register(["env", "selftest"], _env_selftest)
    cli.register(["audit", "leak"], _audit_leak)
    cli.register(["play"], _play)
    # MOD-03 推理后端子命令（G6：probe / bench / robustness）
    from .inference.cli import HANDLERS as _INFERENCE_HANDLERS

    for path, handler in _INFERENCE_HANDLERS.items():
        cli.register(path, handler)
    # MOD-08 测量子命令（G6：baseline / eval report|diversity|ablation|test）
    from .evals.cli import HANDLERS as _EVAL_HANDLERS
    from .evals.cli import set_inference_client_provider

    for path, handler in _EVAL_HANDLERS.items():
        cli.register(path, handler)
    # MOD-04 → MOD-03：fixed_prompt 基线 / play --agent llm 需要的客户端由组合根装配
    # （evals 与 agent 都不自己 import 组合根；与 register_content_hash_provider 同一模式）。
    set_inference_client_provider(_inference_client)
    # Spec §7：config.json 需要 MOD-01 的配表摘要 content_hash；
    # infra 不能反向 import env（NFR-09-5），因此由组合根注入提供者。
    cli.register_content_hash_provider(_content_hash)
    _WIRED = True


def _content_hash() -> str | None:
    """MOD-01 的配表摘要（``env.digest.content_hash``）；失败则记 None，不阻断建 run。"""
    try:
        from .env import load_tables
        from .env.digest import content_hash
        return content_hash(load_tables())
    except Exception: # pragma: no cover - 配表不可用时不应拦住建 run
        return None


def main(argv: Sequence[str] | None = None, *, reexec: bool = True) -> int:
    """完整 CLI 入口：先接线，再交给 ``infra.cli.main``（退出码见 FR-09-11）。"""
    wire()
    return cli.main(argv, reexec=reexec)


# =====================================================================
# MOD-01 命令实现
# =====================================================================

def _env_selftest(args: argparse.Namespace) -> int:
    from .env.selftest import run_selftest

    return run_selftest(getattr(args, "seed", 1))


# =====================================================================
# MOD-02 命令实现
# =====================================================================

def _audit_leak(args: argparse.Namespace) -> int:
    """`audit leak`（MOD-02）。退出码 `0/1/2/4`，两根正交轴（MOD-02 PRD §18.2）：

    - 泄漏结论（FR-02-19）：`0` 无 finding / `1` 仅 medium / `2` 含 critical；
    - 沙箱守卫违例（Spec §8.3，与 `infra.errors.EXIT_CODE` 一致）：`4`，如
      `E_ENV_HONEYPOT`（蜜罐自测 <100% ⇒ 审计器自身失效，verdict 不可按原义采信）。
      4 优先于 2；报告仍记 `verdict=critical`。
    """
    from .sandbox.audit import audit_to_console, run_audit, write_report

    run_id = getattr(args, "run_id", None)
    if not run_id:
        raise InfraError(E.E_IO_CONFIG_INVALID, "audit leak 必须指定 --run-id",
                         hint="用法：python -m gamer_rsi audit leak --run-id <run_id>")
    cfg = load_config(cli.config_overrides(args), config_dir=Path(args.config_dir))
    root = Path(cfg.infra.runs_root) / str(run_id)
    if not root.is_dir():
        raise InfraError(E.E_ENV_AUDIT_IO, f"run 目录不存在：{root}",
                         hint="检查 --run-id；缺证据 ≠ 无泄漏，审计按 critical 处理")
    paths = RunPaths(root)
    result = run_audit(
        root, split=args.split, config_dir=Path(args.config_dir),
        replay=not args.no_replay, self_test=not args.no_self_test)
    out = Path(args.out) if args.out else paths.audit / f"leak-{result.scope}.json"
    written = write_report(result, out)
    print(f"report: {written}")
    return audit_to_console(result)


# =====================================================================
# MOD-04 命令实现（play；G6：Spec §4.6 / MOD-04 PRD §12）
# =====================================================================

#: ``play --agent`` → 基线 agent 种类（MOD-08 ``BASELINE_KINDS`` 的别名映射）。
_PLAY_AGENTS: dict[str, str] = {
    "random": "random",
    "heuristic": "heuristic",
    "fixed_prompt": "fixed_prompt",
    "llm": "llm",
}


def _play(args: argparse.Namespace) -> int:
    """``play``：用指定 agent 跑若干局并落盘摘要（MOD-04 G6）。

    - ``llm`` 走 MOD-04 ``PolicyAgent``（需要 MOD-03 客户端；拿不到即 ``E_LLM_UNAVAILABLE``
      / 退出码 3，**不静默降级**——Spec §8.5 要求降级必须显眼标注）；
    - ``random`` / ``heuristic`` / ``fixed_prompt`` 复用 MOD-08 的三基线 agent，
      保证「同一种 agent」在 ``play`` 与 ``baseline`` 下是同一份实现（Spec §4.7）。
    """
    from .evals.baselines import build_agent
    from .evals.harness import EvalSpec, SandboxRunner, run_eval, write_episode_summaries
    from .evals.seeds import coerce_protocol, split_of_seed, validate_protocol

    agent_kind = _PLAY_AGENTS[str(getattr(args, "agent", "llm"))]
    paths, cfg, run_id = cli.cli_init_run(args)
    parsed = cli.parse_seeds_spec(str(getattr(args, "seeds", "train:1000-1004")))
    seeds = tuple(sorted({s for values in parsed.values() for s in values}))
    if not seeds:
        raise InfraError(E.E_IO_CONFIG_INVALID, "--seeds 未解析出任何 seed",
                         hint="用法：--seeds train:1000-1004 / heldout:2000-2099")
    proto = validate_protocol(coerce_protocol(None))
    declared = next((name for name, values in parsed.items()
                     if name != "default" and values), "")
    split = declared or str(split_of_seed(seeds[0], protocol=proto) or "train")
    if split not in ("train", "heldout", "test"):
        raise InfraError(E.E_IO_CONFIG_INVALID, f"--seeds 的 split 前缀非法：{split!r}",
                         hint="只允许 train: / heldout: / test: 前缀，或省略前缀")
    mode = str(getattr(args, "mode", "train") or "train")
    client = _inference_client(cfg, run_id=run_id, runs_root=paths.root.parent) \
        if agent_kind in ("llm", "fixed_prompt") else None
    if agent_kind in ("llm", "fixed_prompt") and client is None:
        raise InfraError(
            E.E_LLM_UNAVAILABLE,
            f"play --agent {agent_kind} 需要可用的 MOD-03 推理客户端",
            hint="指定可加载的模型（如 --model qwen3-8b-q4；离线冒烟用 --model mock）；"
                 "离线只能跑 --agent random|heuristic")
    eval_cfg = cfg.evals
    spec = EvalSpec(
        run_id=run_id, generation=0, harness_version=0, split=split,  # type: ignore[arg-type]
        basket=seeds, seeds=seeds, agent_kind=agent_kind, n_workers=1,
        seed_timeout_s=int(getattr(eval_cfg, "seed_timeout_s", 14400)),
        t_max=int(getattr(eval_cfg, "t_max", 120)),
        min_support=int(getattr(eval_cfg, "min_support", 3)),
        drop_rate_max=float(getattr(eval_cfg, "drop_rate_max", 0.10)),
        arm=f"play-{agent_kind}")

    def factory(seed: int, arm: str) -> Any:
        if agent_kind == "llm":
            from .agent.policy import EpisodeContext, PolicyAgent

            agent = PolicyAgent(cfg.agent.policy, llm=client, harness_dir=Path("harness"))
            agent.reset(EpisodeContext(run=run_id, episode=f"play-{seed:06d}", seed=int(seed),
                                       split=split, mode=mode, tick=int(seed)))
            return agent
        return build_agent(agent_kind, seed=seed, client=client, config=cfg)

    runner = SandboxRunner(agent_factory=factory, run_id=run_id, config=cfg)
    result = run_eval(spec, runner=runner, protocol=proto)
    write_episode_summaries(paths.root, result.results, split=split, generation=0,
                            arm=f"play-{agent_kind}")
    metrics = result.metrics
    print(f"run: {paths.root}")
    print(f"agent={agent_kind} split={split} episodes={len(result.results)}")
    print(f"n={metrics.n} win_rate={metrics.win_rate:.4f} "
          f"ci95={metrics.win_rate_ci95} degraded_n={metrics.degraded_n} "
          f"illegal_rate={metrics.illegal_rate:.4f}")
    if metrics.degraded_n:
        print(f"[{E.E_LLM_BACKEND_ERROR}] {metrics.degraded_n}/{len(result.results)} 局已降级为"
              f"启发式（Spec §8.5）；降级局不计入 n", file=sys.stderr)
    if metrics.n == 0:
        # 没有任何可用样本：不能以 0 退出（否则脚本会把空结果当成成功测量）
        print(f"[{E.E_LLM_UNAVAILABLE}] 没有可用样本（n=0，degraded={metrics.degraded_n}）",
              file=sys.stderr)
        return 3 if metrics.degraded_n else 1
    return 0


# =====================================================================
# MOD-03 客户端装配（供 MOD-04 / MOD-08 复用）
# =====================================================================

def _inference_client(cfg: Any, *, run_id: str, runs_root: Path) -> Any:
    """装配 MOD-03 ``BackendManager`` 并适配为同步 ``InferenceClient``。

    ``mode`` 一律用 ``eval``：基线/play 都是评估场景，B-14 要求 ``temperature=0`` 且
    seed 显式（``validate_sampling`` 会在越界时抛错）。权重缺失等加载问题在首次调用时
    由 MOD-03 抛 ``E_LLM_*``，调用方据此 fail-closed。
    """
    from .agent.policy import BackendInferenceClient
    from .inference.backend import BackendManager, InfraBridge

    inference = cfg.inference
    bridge = InfraBridge(run_id=run_id, runs_root=Path(runs_root),
                         debug_dump_raw=bool(getattr(inference, "debug_dump_raw", False)))
    manager = BackendManager(
        str(inference.registry_path), mode="eval",
        ram_budget_gb=float(inference.ram_budget_gb), infra=bridge, config=inference)
    return BackendInferenceClient(
        manager, model_id=str(inference.model_id),
        total_timeout_ms=int(inference.total_timeout_ms),
        max_attempts=int(inference.max_attempts))


if __name__ == "__main__": # pragma: no cover - 便于 `python -m gamer_rsi.app`
    sys.exit(main(reexec=True))
