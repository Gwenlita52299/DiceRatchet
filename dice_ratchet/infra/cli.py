"""CLI 子命令树（FR-09-10 / FR-09-11 / FR-09-12，对齐 Spec §4.6）。

本模块只负责**解析与转发**：命令面（argparse 树、退出码、`--dry-run`/`--resume`）
在此冻结，各模块的实现经 ``register(path, handler)`` 注入（组合根见 ``dice_ratchet/app.py``，
NFR-09-5：``infra`` 不 import 任何其他子包）。未注册的命令以 ``E_IO_MODULE_PENDING``
（退出码 2）显式拒绝，保证命令面与 Spec §4.6 逐字一致——新增命令必须同步回填
Spec §4.6（FR-09-10）。

退出码（FR-09-11 / Spec §8.3）：``0`` 成功（含 HITL 挂起）；``1`` 领域失败；
``2`` 配置/环境错误；``3`` 模型不可用；``4`` 沙箱/守卫违例；``5`` IO 落盘失败；
``130`` Ctrl-C。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Final, Sequence

from . import events, guards
from .config import canonical_json, config_hash, load_config
from .errors import (EXIT_CONFIG, EXIT_DOMAIN, EXIT_INTERRUPT, E, InfraError,
                     error_code_of)
from .run import git_short_sha, init_run_dir, make_run_id, resolve_run_dir
from .guards import GuardFinding
from .process import bootstrap_process, process_fingerprint, process_snapshot

__all__ = ["main", "build_parser", "parse_seeds_spec", "PENDING_MODULES",
           "register", "registered_commands", "handler_for", "config_overrides"]

Handler = Callable[[argparse.Namespace], int]

# 各命令的归属模块（未注册实现的命令在 M0 过渡期返回 E_IO_MODULE_PENDING）
PENDING_MODULES: Final[dict[str, str]] = {
    "env": "MOD-01 env-rules", "play": "MOD-04 policy", "baseline": "MOD-08 evals",
    "loop": "MOD-06 loop", "evolve": "MOD-07 evolve", "eval": "MOD-08 evals",
    "audit": "MOD-02 env-sandbox", "inference": "MOD-03 inference",
    "memory": "MOD-05 memory",
}

#: 命令路径 → 实现（由组合根 ``dice_ratchet/app.py`` 经 ``register()`` 注入）
_HANDLERS: Final[dict[tuple[str, ...], Handler]] = {}

_SEEDS_SPEC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?P<split>[a-zA-Z_]+):)?(?P<rest>.+)$")


# =====================================================================
# 注册表（FR-09-10：MOD-09 只解析与转发）
# =====================================================================

def register(path: Sequence[str] | str, handler: Handler) -> None:
    """注册某个命令路径的实现；组合根在 ``main()`` 之前调用（幂等覆盖）。"""
    key = tuple(path.split()) if isinstance(path, str) else tuple(path)
    if not key:
        raise InfraError(E.E_IO_CONFIG_INVALID, "register() 的路径不能为空")
    _HANDLERS[key] = handler


def handler_for(path: Sequence[str] | str) -> Handler | None:
    key = tuple(path.split()) if isinstance(path, str) else tuple(path)
    return _HANDLERS.get(key)


def registered_commands() -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(_HANDLERS))


#: 配表摘要提供者（MOD-01 的 ``content_hash``）：由组合根注册，避免 infra 反向依赖 env
_CONTENT_HASH_PROVIDER: Callable[[], str | None] | None = None


def register_content_hash_provider(provider: Callable[[], str | None] | None) -> None:
    """注册 ``content_hash`` 提供者（``config.json`` 快照用，Spec §7）。"""
    global _CONTENT_HASH_PROVIDER
    _CONTENT_HASH_PROVIDER = provider


def _content_hash() -> str | None:
    if _CONTENT_HASH_PROVIDER is None:
        return None
    try:
        return _CONTENT_HASH_PROVIDER()
    except Exception: # 提供者失败不应阻断建 run；快照里记 None 即可
        return None


#: 进程内当前活跃 run（``cli_init_run`` 设置，``main`` 收尾时发 ``infra.run_end``）
_ACTIVE_RUN: dict[str, Any] | None = None


# =====================================================================
# 入口
# =====================================================================

def main(argv: Sequence[str] | None = None, *, reexec: bool = True) -> int:
    """CLI 入口；返回退出码（FR-09-11）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    code = 0
    try:
        bootstrap_process(reexec=reexec, module="dice_ratchet")
        parser = build_parser()
        args = parser.parse_args(argv)
        if getattr(args, "dry_run", False):
            return _dry_run(parser, args, argv) # dry-run 不建 run，无需 run_end
        handler = getattr(args, "func", None)
        if handler is None:
            parser.print_help()
            return 0
        code = int(handler(args) or 0)
    except InfraError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        print(f"  reproduce: python -m dice_ratchet {' '.join(argv)}", file=sys.stderr)
        code = exc.exit_code
    except KeyboardInterrupt:
        print("\n[interrupted] 用户中断 (Ctrl-C)", file=sys.stderr)
        code = EXIT_INTERRUPT
    except SystemExit as exc: # argparse 的非法参数（退出码 2，T-09-15）
        code = int(exc.code) if isinstance(exc.code, int) else EXIT_CONFIG
        if code != 0:
            # T-09-15：非法参数必须以退出码 2 结束，且 stderr 含错误码
            print(f"[{E.E_IO_CONFIG_INVALID}] 命令行参数非法"
                  f"（argparse 细节见上方 usage）", file=sys.stderr)
            print(f"  reproduce: python -m dice_ratchet {' '.join(argv)}", file=sys.stderr)
    except BaseException: # 未预期异常：run_end 记领域失败后原样抛出（保留 traceback）
        code = code or EXIT_DOMAIN
        raise
    finally:
        _finalize_run(code)
    return code


def _finalize_run(code: int) -> None:
    """收尾：写 ``infra.run_end`` + ``metrics/io_latency.csv``，再解绑事件 sink（§13）。"""
    global _ACTIVE_RUN
    run, _ACTIVE_RUN = _ACTIVE_RUN, None
    if run is None:
        return
    try:
        if events.is_bound():
            events.emit("infra.run_end", exit_code=code, run_id=run["run_id"],
                        duration_s=round(time.monotonic() - run["t0"], 4))
            _write_io_latency(run["paths"], run["run_id"], code)
    finally:
        events.unbind()


def _write_io_latency(paths: Any, run_id: str, exit_code: int) -> None:
    """把单行写入耗时 p50/p99 追加进 ``metrics/io_latency.csv``（PRD §13）。"""
    samples = sorted(events.drain_latency_ms())
    if not samples:
        return
    n = len(samples)
    p50 = samples[min(n - 1, int(0.50 * n))]
    p99 = samples[min(n - 1, int(0.99 * n))]
    path = Path(paths.metrics) / "io_latency.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["run_id", "n", "p50_ms", "p99_ms", "max_ms", "exit_code"])
        writer.writerow([run_id, n, round(p50, 4), round(p99, 4),
                         round(samples[-1], 4), exit_code])


def _dry_run(parser: argparse.ArgumentParser, args: argparse.Namespace,
             argv: Sequence[str]) -> int:
    """FR-09-12：不调模型、不写 runs/，stdout 输出可解析 JSON。

    例外：``memory rollback --dry-run``（MOD-05 PRD §8.3）要的是一份**真实**的
    ``RollbackReport(entries_delta, active_delta, lost_entry_ids)``，静态计划 JSON
    满足不了该契约，因此按命令路径转发给所属模块的实现（仍不写库）。
    """
    if tuple(getattr(args, "command_path", []) or ()) == ("memory", "rollback"):
        handler = getattr(args, "func", None)
        if handler is not None:
            return int(handler(args) or 0)
    cfg = load_config(config_overrides(args), config_dir=Path(args.config_dir))
    plan = {"command": " ".join(argv), "dry_run": True,
            "config": json.loads(canonical_json(cfg)),
            "config_hash": config_hash(cfg),
            "would": _plan_of(args)}
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """从解析结果提取 CLI 显式覆盖（FR-09-1：CLI 优先级最高）。

    覆盖 ``--k`` / ``--generations`` / ``--hitl`` / ``--log-level`` 与 MOD-03 的
    ``--model`` / ``--mode`` / ``--seed`` / ``--timeout-ms``；其余命令参数由各自模块读取。
    """
    out: dict[str, Any] = {}
    for key in ("k", "generations", "hitl"):
        if hasattr(args, key) and getattr(args, key) is not None:
            name = {"k": "loop.k", "generations": "loop.max_generations"}.get(key, f"loop.{key}")
            out[name] = getattr(args, key)
    for key, path in (("model", "inference.model_id"), ("mode", "inference.mode"),
                      ("seed", "inference.seed"),
                      ("timeout_ms", "inference.total_timeout_ms")):
        value = getattr(args, key, None)
        if value is not None:
            out[path] = value
    log_level = getattr(args, "log_level", None)
    if log_level:
        out["infra.log_level"] = log_level
    return out


def _plan_of(args: argparse.Namespace) -> dict[str, Any]:
    """dry-run 打印的「将执行的动作」摘要。"""
    cmd = getattr(args, "command_path", [])
    plan: dict[str, Any] = {"command": cmd}
    for key in sorted(vars(args)):
        if key in ("func", "dry_run", "config_dir", "command_path"):
            continue
        plan[key] = getattr(args, key)
    return plan


# =====================================================================
# parser 构建
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dice_ratchet",
        description="DiceRatchet 训练链路：本地小模型在自建骰子 roguelike 里递归自我改进",
        epilog="命令面对齐 Spec §4.6；文档冲突时以 Spec 为准。")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    _add_env(sub)
    _add_play(sub)
    _add_baseline(sub)
    _add_loop(sub)
    _add_evolve(sub)
    _add_eval(sub)
    _add_audit(sub)
    _add_inference(sub)
    _add_memory(sub)
    _add_guards(sub)
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    """全局选项：所有命令（含子命令）都支持 ``--dry-run``（Spec §4.6）。"""
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将执行的动作与配置摘要（stdout 为可解析 JSON），不落盘")
    parser.add_argument("--config-dir", default="configs", help="configs/ 目录")
    parser.add_argument("--log-level", default=None,
                        help="覆盖 infra.log_level（DEBUG/INFO/WARNING/ERROR）")


_COMMON_PARENTS: list[argparse.ArgumentParser] = []


def _common_parents() -> list[argparse.ArgumentParser]:
    if not _COMMON_PARENTS:
        parent = argparse.ArgumentParser(add_help=False)
        _add_common(parent)
        _COMMON_PARENTS.append(parent)
    return _COMMON_PARENTS


def _sp(sub: Any, name: str, **kw: Any) -> argparse.ArgumentParser:
    """``add_parser`` 的包装：自动继承全局选项（``--dry-run`` / ``--config-dir``），任意层级。"""
    kw.setdefault("parents", _common_parents())
    return sub.add_parser(name, **kw)


def _pending(module_key: str) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        raise InfraError(E.E_IO_MODULE_PENDING,
                         f"命令已注册，但所属模块 {PENDING_MODULES[module_key]} 尚未落地",
                         hint="M0 交付 MOD-09 infra 骨架；该命令在其模块的里程碑交付后可用")
    return run


def _deferred(path: list[str], module_key: str) -> Callable[[argparse.Namespace], int]:
    """转发到所属模块注册的实现；未注册则按 ``E_IO_MODULE_PENDING`` 拒绝（FR-09-10）。"""
    def dispatch(args: argparse.Namespace) -> int:
        handler = _HANDLERS.get(tuple(path))
        if handler is None:
            return _pending(module_key)(args)
        return handler(args)
    return dispatch


def _common_run_id(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-id", dest="run_id", metavar="ID",
                   help="run 标识（默认由 git sha + config_hash + 时间戳生成）")
    p.add_argument("--resume", action="store_true", help="续跑开关（定位已有 run 目录）")


def _set_func(p: argparse.ArgumentParser, func: Callable[[argparse.Namespace], int],
              path: list[str]) -> None:
    p.set_defaults(func=func, command_path=path)


# ---- env -------------------------------------------------------------------------

def _add_env(sub: Any) -> None:
    p = _sp(sub, "env", help="环境层（MOD-01/02）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    st = _sp(sp, "selftest", help="规则/确定性自检 + GAME golden 对拍")
    st.add_argument("--seed", type=int, default=1, help="自检 seed")
    _set_func(st, _deferred(["env", "selftest"], "env"), ["env", "selftest"])
    p.set_defaults(func=None)


def _add_play(sub: Any) -> None:
    p = _sp(sub, "play", help="用指定 agent 玩若干局（MOD-04）")
    p.add_argument("--agent", choices=["llm", "heuristic", "random", "fixed_prompt"],
                   default="llm", help="Spec §4.6 命令面：llm|heuristic|random|fixed_prompt")
    p.add_argument("--seeds", default="train:1000-1004",
                   help="seed 规格：train:1000-1004 / heldout:731000-731039 / 947000,947001 / file:seeds.txt")
    p.add_argument("--episodes", type=int, default=1)
    p.add_argument("--mode", choices=["train", "eval"], default="train")
    p.add_argument("--model", default=None, help="MOD-03 模型 id（覆盖 inference.model_id）")
    _common_run_id(p)
    _set_func(p, _deferred(["play"], "play"), ["play"])


def _add_baseline(sub: Any) -> None:
    p = _sp(sub, "baseline", help="跑基线（MOD-08）")
    p.add_argument("--kind", choices=["random", "heuristic", "fixed_prompt"],
                   default="heuristic")
    p.add_argument("--split", choices=["train", "heldout", "test"], default="heldout",
                   help="基线评估的 split（FR-08-11）")
    seeds = p.add_mutually_exclusive_group()
    seeds.add_argument("--seeds", default=None,
                       help="显式 seed 规格（与 --basket 互斥）：train:1000-1004 / 2000,2001")
    seeds.add_argument("--basket", default=None, help="seed 篮子（MOD-08 seeds.yaml，默认按 split 推导）")
    p.add_argument("--model", default=None,
                   help="MOD-03 模型 id（fixed_prompt 需要，覆盖 inference.model_id；"
                        "离线冒烟用 mock）")
    _common_run_id(p)
    _set_func(p, _deferred(["baseline"], "baseline"), ["baseline"])


def _add_loop(sub: Any) -> None:
    p = _sp(sub, "loop", help="闭环编排（MOD-06）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")

    run = _sp(sp, "run", help="运行演化闭环")
    run.add_argument("--generations", type=int, default=None, help="演化代数（覆盖 loop.max_generations）")
    run.add_argument("--k", type=int, default=None, help="每 K 局训练触发一次演化（覆盖 loop.k）")
    run.add_argument("--hitl", dest="hitl", action="store_true", default=None,
                     help="每代挂起等人工 approve/rollback")
    run.add_argument("--no-hitl", dest="hitl", action="store_false", default=None)
    run.add_argument("--agent", choices=["llm", "heuristic"], default="llm",
                     help="训练局使用的 agent（Spec §4.6；smoke 默认 heuristic，不触网）")
    run.add_argument("--seeds", default=None,
                     help="seed 规格：train:1000-1029（省略时按 MOD-08 seeds.take 取各代 train 块）")
    run.add_argument("--no-reflect", dest="no_reflect", action="store_true",
                     help="关闭局末反思（消融「去反思」；免模型路径）")
    run.add_argument("--no-evolve", dest="no_evolve", action="store_true",
                     help="不调用 MOD-07（用 stub 只验编排，PRD §14）")
    run.add_argument("--force-rerun", dest="force_rerun", action="store_true",
                     help="同 seed 重跑时把旧计分记录标 superseded_by（不删除）")
    run.add_argument("--retry-reflections", dest="retry_reflections", action="store_true",
                     help="--resume 时重试此前 status=skipped 的反思（FR-06-4）")
    run.add_argument("--harness-dir", default="harness", help="harness 工件目录")
    run.add_argument("--model", default=None, help="MOD-03 模型 id（覆盖 inference.model_id）")
    run.add_argument("--mode", choices=["train", "eval"], default=None,
                     help="MOD-03 采样模式（训练局默认 train）")
    run.add_argument("--timeout-ms", dest="timeout_ms", type=int, default=None,
                     help="覆盖 inference.total_timeout_ms")
    run.add_argument("--baseline", dest="baseline", action="store_true", default=None,
                     help="run 启动时先跑 gen-000 基线（默认：使用真实 MOD-07 时开启）")
    run.add_argument("--no-baseline", dest="baseline", action="store_false", default=None,
                     help="跳过 gen-000 基线（只验编排时用）")
    _common_run_id(run)
    _set_func(run, _deferred(["loop", "run"], "loop"), ["loop", "run"])

    for name, help_text in (("status", "查看 run 进度"), ("smoke", "冒烟跑一代（免模型）")):
        sp_run = _sp(sp, name, help=help_text)
        if name == "smoke":
            sp_run.add_argument("--seeds", default="train:1000-1002",
                                help="seed 规格（默认 train:1000-1002）")
            sp_run.add_argument("--k", type=int, default=3, help="每 K 局触发一次演化（默认 3）")
            sp_run.add_argument("--generations", type=int, default=1)
            sp_run.add_argument("--agent", choices=["llm", "heuristic"],
                                default="heuristic", help="smoke 默认启发式（不触网）")
            sp_run.add_argument("--no-reflect", dest="no_reflect", action="store_true",
                                default=True, help="smoke 恒不反思（免模型路径）")
            sp_run.add_argument("--no-evolve", dest="no_evolve", action="store_true",
                                default=True)
            sp_run.add_argument("--force-rerun", dest="force_rerun", action="store_true")
            sp_run.add_argument("--retry-reflections", dest="retry_reflections",
                                action="store_true")
            sp_run.add_argument("--harness-dir", default="harness")
            sp_run.add_argument("--baseline", dest="baseline", action="store_true",
                                default=None)
            sp_run.add_argument("--no-baseline", dest="baseline", action="store_false",
                                default=None)
        _common_run_id(sp_run)
        _set_func(sp_run, _deferred(["loop", name], "loop"), ["loop", name])
    p.set_defaults(func=None)


def _add_evolve(sub: Any) -> None:
    p = _sp(sub, "evolve", help="harness 自进化（MOD-07）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    for name in ("approve", "rollback"):
        sp_run = _sp(sp, name)
        _common_run_id(sp_run)
        sp_run.add_argument("--generation", type=int, required=True)
        sp_run.add_argument("--reason", default="")
        _set_func(sp_run, _deferred(["evolve", name], "evolve"), ["evolve", name])
    p.set_defaults(func=None)


def _add_eval(sub: Any) -> None:
    p = _sp(sub, "eval", help="测量（MOD-08）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")

    rep = _sp(sp, "report", help="代际报告")
    _common_run_id(rep)
    rep.add_argument("--split", choices=["train", "heldout", "test"], default="heldout")
    rep.add_argument("--baseline", default="none", help="none|all|<kind>")
    rep.add_argument("--basket", default="H1", help="seed 篮子（heldout）")
    rep.add_argument("--generation", type=int, default=None, help="代数（默认取最大已存在代）")
    rep.add_argument("--arm", default=None,
                     help="臂（默认 = 该代胜出/候选臂；all = 申报的混合池；"
                          "也可显式给 parent / cand-<id> 等，F46-A）")
    _set_func(rep, _deferred(["eval", "report"], "eval"), ["eval", "report"])

    abl = _sp(sp, "ablation", help="消融三臂（FR-08-21）")
    _common_run_id(abl)
    abl.add_argument("--basket", choices=["H1", "H2", "H3"], default="H2")
    abl.add_argument("--generation", type=int, default=None)
    abl.add_argument("--arms", default="no_reflection,no_self_edit",
                     help="逗号分隔的消融臂（full 自动补上）")
    _set_func(abl, _deferred(["eval", "ablation"], "eval"), ["eval", "ablation"])

    tst = _sp(sp, "test", help="test 终检（默认锁定）")
    _common_run_id(tst)
    tst.add_argument("--basket", choices=["test_full"], default="test_full")
    tst.add_argument("--generation", type=int, default=None)
    tst.add_argument("--unlock-once", dest="unlock_once", action="store_true",
                     help="每里程碑单次解锁 test 池并写 test_attestation.json（FR-08-04）")
    tst.add_argument("--no-transfer", dest="transfer", action="store_false", default=True,
                     help="跳过完整局（medium×10 关）的次级「迁移」指标（Spec §8.6）")
    tst.add_argument("--attestation", default=None, help="存证落盘路径")
    _set_func(tst, _deferred(["eval", "test"], "eval"), ["eval", "test"])

    acc = _sp(sp, "acceptance", help="判据 1 判定器（held-out 单调不降 + ≥1 次 accept）")
    _common_run_id(acc)
    acc.add_argument("--bootstrap-B", dest="bootstrap_B", type=int, default=None,
                     help="P(gap>0) 的配对 bootstrap 次数（默认取 eval.bootstrap_B）")
    _set_func(acc, _deferred(["eval", "acceptance"], "eval"), ["eval", "acceptance"])

    div = _sp(sp, "diversity", help="动作熵 / 坍缩监控")
    _common_run_id(div)
    div.add_argument("--generation", type=int, default=None)
    div.add_argument("--arm", default=None,
                     help="臂（缺省/ all = 该代整池；也可显式给 parent / cand-<id> 等，F46-A）")
    _set_func(div, _deferred(["eval", "diversity"], "eval"), ["eval", "diversity"])
    p.set_defaults(func=None)


def _add_audit(sub: Any) -> None:
    p = _sp(sub, "audit", help="泄漏审计（MOD-02，INV-2/3/5）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    leak = _sp(sp, "leak")
    _common_run_id(leak)
    leak.add_argument("--split", choices=["train", "heldout", "test", "demo"],
                      default="train", help="审计哪个 split 的轨迹（默认 train）")
    leak.add_argument("--out", default=None, help="报告路径（默认 runs/<id>/audit/leak-leak.json）")
    leak.add_argument("--no-replay", action="store_true",
                      help="跳过轨迹重放（future_room/shop 规则集降级）")
    leak.add_argument("--no-self-test", action="store_true", help="跳过蜜罐自测（不建议）")
    _set_func(leak, _deferred(["audit", "leak"], "audit"), ["audit", "leak"])
    p.set_defaults(func=None)


def _add_inference(sub: Any) -> None:
    p = _sp(sub, "inference", help="推理后端（MOD-03）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    for name in ("probe", "bench", "robustness"):
        sp_run = _sp(sp, name)
        _common_run_id(sp_run)
        sp_run.add_argument("--model", default=None, help="模型 id（默认 inference.model_id）")
        sp_run.add_argument("--mode", choices=["train", "eval"], default=None,
                            help="train/eval；eval 强制 temperature=0 且必须 --seed（B-14）")
        sp_run.add_argument("--seed", type=int, default=None, help="采样 seed（eval 必填）")
        sp_run.add_argument("--n", type=int, default=None,
                            help="bench 生成次数（默认 50）/ robustness prompt 条数（默认 200）")
        sp_run.add_argument("--prompts", default=None,
                            help="robustness 的固定 prompt JSONL（默认 tests/fixtures/...）")
        sp_run.add_argument("--out", default=None, help="报告落盘路径（默认 runs/<id>/metrics/）")
        sp_run.add_argument("--timeout-ms", dest="timeout_ms", type=int, default=None,
                            help="覆盖 inference.total_timeout_ms")
        _set_func(sp_run, _deferred(["inference", name], "inference"), ["inference", name])
    p.set_defaults(func=None)


def _add_memory(sub: Any) -> None:
    p = _sp(sub, "memory", help="反思记忆库（MOD-05）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    st = _sp(sp, "stats", help="条目数/版本/去重率/冲突数/淘汰数/命中率")
    _common_run_id(st)
    _set_func(st, _deferred(["memory", "stats"], "memory"), ["memory", "stats"])

    snap = _sp(sp, "snapshot", help="产出 memory_v{N}.jsonl（I3 复扫全库）")
    _common_run_id(snap)
    snap.add_argument("--reason", default="manual", help="快照原因（如 gen-003）")
    snap.add_argument("--version", dest="memory_version", type=int, default=None,
                      help="显式版本号（默认 index.memory_version + 1）")
    _set_func(snap, _deferred(["memory", "snapshot"], "memory"), ["memory", "snapshot"])

    back = _sp(sp, "rollback", help="回滚到 memory_v{N}.jsonl（校验 sha256）")
    _common_run_id(back)
    back.add_argument("--to-version", dest="to_version", type=int, default=None,
                      help="目标快照版本号；缺失/不存在 → 退出码 2（E_IO_SNAPSHOT_MISSING）")
    _set_func(back, _deferred(["memory", "rollback"], "memory"), ["memory", "rollback"])

    ver = _sp(sp, "verify", help="行完整性 / entry_id 唯一 / sha256 复算")
    _common_run_id(ver)
    ver.add_argument("--full", action="store_true",
                     help="全量自检：复算 content_sha256/situation_key 并执行 I3 复扫")
    ver.add_argument("--tick", type=int, default=None, help="逻辑时钟（默认库内最大 tick）")
    _set_func(ver, _deferred(["memory", "verify"], "memory"), ["memory", "verify"])

    ret = _sp(sp, "retrieve", help="按观测检索（供人工复核；MOD-04 走 API）")
    _common_run_id(ret)
    ret.add_argument("--obs", default=None, help="观测 JSON 文件路径或内联 JSON")
    ret.add_argument("--top-k", dest="top_k", type=int, default=None)
    ret.add_argument("--token-budget", dest="token_budget", type=int, default=None)
    ret.add_argument("--tick", type=int, default=0, help="逻辑时钟（训练局序号）")
    ret.add_argument("--tags", default="", help="逗号分隔的标签提示（并入 TagSim）")
    _set_func(ret, _deferred(["memory", "retrieve"], "memory"), ["memory", "retrieve"])
    p.set_defaults(func=None)


# ---- guards（MOD-09 本模块实现）------------------------------------------------------

def _add_guards(sub: Any) -> None:
    p = _sp(sub, "guards", help="确定性守卫与分层 import 门禁（MOD-09）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    chk = _sp(sp, "check", help="扫描 root，违例退出码 4")
    chk.add_argument("--root", default="dice_ratchet", help="扫描根目录")
    chk.add_argument("--fix-hints", action="store_true",
                     help="为每条发现打印建议的白名单注释")
    _set_func(chk, _run_guards, ["guards", "check"])
    p.set_defaults(func=None)


def _run_guards(args: argparse.Namespace) -> int:
    root = Path(args.root)
    findings: list[GuardFinding] = guards.scan_all(root)
    guards.emit_findings(findings)
    hard = [f for f in findings if not f.exempted]
    if not findings:
        print(f"guards: OK — 0 findings in {root}")
        return 0
    print(f"guards: {len(findings)} finding(s) in {root} "
          f"({len(hard)} hard, {len(findings) - len(hard)} exempted)")
    for f in findings:
        flag = "EXEMPTED" if f.exempted else "VIOLATION"
        print(f"  [{flag}] {f.code} {f.path}:{f.line}:{f.col} — {f.message}")
        if f.exempt_reason:
            print(f"           exempt_reason={f.exempt_reason}")
        if args.fix_hints:
            print(f"           hint: {f.hint()}")
    if hard:
        return error_code_of(E.E_IO_GUARD_LAYER)
    return 0


# =====================================================================
# seeds 规格解析（--seeds SPEC，FR-09-10 附注）
# =====================================================================

def parse_seeds_spec(spec: str, *, seeds_file_dir: Path = Path(".")) -> dict[str, list[int]]:
    """解析 ``train:1000-1004`` / ``heldout:731000-731039`` / ``947000,947001`` / ``file:seeds.txt``。

    返回 ``{split: [seeds]}``；无 split 前缀时键为 ``"default"``。
    """
    spec = spec.strip()
    # file: 形式可能含绝对路径（macOS 临时目录带连字符），优先单独处理
    if spec.startswith("file:"):
        path = Path(seeds_file_dir) / spec[len("file:"):]
        if not path.is_file():
            raise InfraError(E.E_IO_CONFIG_INVALID, f"seed 文件不存在：{path}")
        values = [int(x) for x in re.split(r"[,\s]+", path.read_text(encoding="utf-8"))
                  if x.strip()]
        return {"default": values}
    match = _SEEDS_SPEC_RE.match(spec)
    split = (match.group("split") or "default") if match else "default"
    rest = match.group("rest").strip() if match else spec
    if "-" in rest and not rest.startswith("-"):
        lo, hi = rest.split("-", 1)
        return {split: list(range(int(lo), int(hi) + 1))}
    return {split: [int(x) for x in re.split(r"[,\s]+", rest) if x.strip()]}


# ---- 供各模块复用的 run 定位助手 ------------------------------------------------------

def cli_init_run(args: argparse.Namespace, *,
                 require_state: bool = True) -> tuple[Any, Any, Any]:
    """CLI 命令通用的启动序列（bootstrap 之后）：load_config → make_run_id → init_run_dir。

    同时：
    - 按 ``infra.fsync_every`` / ``infra.rotate_max_mb`` 建事件 sink（PRD §9 的配置真正生效）；
    - 发 ``infra.run_start``（含进程确定性快照 FR-09-17、未知键清单 FR-09-1）；
    - 未知键非空时补发 ``infra.config_unknown_key``（E_IO_CONFIG_UNKNOWN）；
    - 登记本进程活跃 run，供 ``main()`` 收尾发 ``infra.run_end`` 并写 io_latency（§13）。

    ``require_state=False``（``baseline --resume``，M1 验收 F18）：``--resume`` 定位已有 run
    时只校验 ``config_hash``，**不要求** ``state.json``——基线的续跑语义是「跳过
    ``episodes/`` 里已完成的局」，而 ``state.json`` 的语义归 MOD-06（基线从不写它）。
    """
    global _ACTIVE_RUN
    cfg = load_config(config_overrides(args), config_dir=Path(args.config_dir))
    unknown = list(getattr(load_config, "unknown_keys", []) or [])
    run_id, _sha = make_run_id(cfg, explicit=getattr(args, "run_id", None))
    if getattr(args, "resume", False):
        paths = resolve_run_dir(run_id, cfg, Path(cfg.infra.runs_root),
                                resume=bool(require_state))
    else:
        paths = init_run_dir(run_id, cfg, Path(cfg.infra.runs_root),
                             content_hash=_content_hash())
    events.bind(paths.events, run_id,
                fsync_every=cfg.infra.fsync_every,
                rotate_max_mb=cfg.infra.rotate_max_mb)
    # FR-09-17：进程确定性环境由**这一次**（已 bind、已加载配置）的 run_start 记录。
    # 调用序列契约：cli.main() 先 bootstrap_process()（固化 env），再走本函数；
    # bootstrap_process() 自身不产事件（那里既无 config_hash 也无 sink），
    # 因此本函数的 process_fingerprint() 才读得到真实 env。
    events.emit("infra.run_start", config_hash=config_hash(cfg),
                git_sha=git_short_sha(), run_id=run_id,
                env=dict(process_snapshot()), unknown_keys=unknown,
                **process_fingerprint())
    if unknown: # FR-09-1：未知键 → 记 E_IO_CONFIG_UNKNOWN 并忽略（不中断）
        events.emit("infra.config_unknown_key", keys=unknown,
                    code=E.E_IO_CONFIG_UNKNOWN)
    _ACTIVE_RUN = {"run_id": run_id, "paths": paths, "t0": time.monotonic()}
    return paths, cfg, run_id

