"""CLI 子命令树（FR-09-10 / FR-09-11 / FR-09-12，对齐 Spec §4.6）。

本模块只负责**解析与转发**：各模块通过 ``register(subparser)`` 挂载自己的命令。
尚未落地的模块命令以 ``E_IO_MODULE_PENDING``（退出码 2）显式拒绝，保证命令面
与 Spec §4.6 逐字一致——新增命令必须同步回填 Spec §4.6（FR-09-10）。

退出码（FR-09-11 / Spec §8.3）：``0`` 成功（含 HITL 挂起）；``1`` 领域失败；
``2`` 配置/环境错误；``3`` 模型不可用；``4`` 沙箱/守卫违例；``5`` IO 落盘失败；
``130`` Ctrl-C。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Final, Sequence

from . import events, guards
from .config import canonical_json, config_hash, load_config
from .errors import EXIT_INTERRUPT, E, InfraError, error_code_of
from .run import git_short_sha, init_run_dir, make_run_id, resolve_run_dir
from .guards import GuardFinding
from .process import bootstrap_process

__all__ = ["main", "build_parser", "parse_seeds_spec", "PENDING_MODULES"]

# 各命令的归属模块（尚未落地的模块在 M0 过渡期返回 E_IO_MODULE_PENDING）
PENDING_MODULES: Final[dict[str, str]] = {
    "env": "MOD-01 env-rules", "play": "MOD-04 policy", "baseline": "MOD-08 evals",
    "loop": "MOD-06 loop", "evolve": "MOD-07 evolve", "eval": "MOD-08 evals",
    "audit": "MOD-02 env-sandbox", "inference": "MOD-03 inference",
    "memory": "MOD-05 memory",
}

_SEEDS_SPEC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?P<split>[a-zA-Z_]+):)?(?P<rest>.+)$")


# =====================================================================
# 入口
# =====================================================================

def main(argv: Sequence[str] | None = None, *, reexec: bool = True) -> int:
    """CLI 入口；返回退出码（FR-09-11）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap_process(reexec=reexec, module="gamer_rsi")
        parser = build_parser()
        args = parser.parse_args(argv)
        if getattr(args, "dry_run", False):
            return _dry_run(parser, args, argv)
        handler = getattr(args, "func", None)
        if handler is None:
            parser.print_help()
            return 0
        return int(handler(args) or 0)
    except InfraError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        print(f"  reproduce: python -m gamer_rsi {' '.join(argv)}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n[interrupted] 用户中断 (Ctrl-C)", file=sys.stderr)
        return EXIT_INTERRUPT
    except SystemExit as exc: # argparse 的非法参数（退出码 2）
        return int(exc.code) if isinstance(exc.code, int) else 2


def _dry_run(parser: argparse.ArgumentParser, args: argparse.Namespace,
             argv: Sequence[str]) -> int:
    """FR-09-12：不调模型、不写 runs/，stdout 输出可解析 JSON。"""
    cfg = load_config(_config_overrides(args), config_dir=Path(args.config_dir))
    plan = {"command": " ".join(argv), "dry_run": True,
            "config": json.loads(canonical_json(cfg)),
            "config_hash": config_hash(cfg),
            "would": _plan_of(args)}
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """从解析结果提取 ``loop.k`` / ``evolve.*`` 等扁平覆盖。"""
    out: dict[str, Any] = {}
    for key in ("k", "generations", "hitl"):
        if hasattr(args, key) and getattr(args, key) is not None:
            name = {"k": "loop.k", "generations": "loop.max_generations"}.get(key, f"loop.{key}")
            out[name] = getattr(args, key)
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
        prog="gamer_rsi",
        description="GameRSI 训练链路：本地小模型在自建骰子 roguelike 里递归自我改进",
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
    _set_func(st, _pending("env"), ["env", "selftest"])
    p.set_defaults(func=None)


def _add_play(sub: Any) -> None:
    p = _sp(sub, "play", help="用指定 agent 玩若干局（MOD-04）")
    p.add_argument("--agent", choices=["llm", "heuristic", "fixed", "fixed_prompt"],
                   default="llm")
    p.add_argument("--seeds", default="train:1000-1004",
                   help="seed 规格：train:1000-1004 / heldout:2000-2099 / 9000,9001 / file:seeds.txt")
    p.add_argument("--episodes", type=int, default=1)
    p.add_argument("--mode", choices=["train", "eval"], default="train")
    _common_run_id(p)
    _set_func(p, _pending("play"), ["play"])


def _add_baseline(sub: Any) -> None:
    p = _sp(sub, "baseline", help="跑基线（MOD-08）")
    p.add_argument("--kind", choices=["random", "heuristic", "fixed_prompt"],
                   default="heuristic")
    p.add_argument("--seeds", default="heldout:2000-2039")
    p.add_argument("--basket", default="H1", help="seed 篮子（MOD-08 seeds.yaml）")
    _common_run_id(p)
    _set_func(p, _pending("baseline"), ["baseline"])


def _add_loop(sub: Any) -> None:
    p = _sp(sub, "loop", help="闭环编排（MOD-06）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")

    run = _sp(sp, "run", help="运行演化闭环")
    run.add_argument("--generations", type=int, default=3, help="演化代数")
    run.add_argument("--k", type=int, default=None, help="每 K 局训练触发一次演化（覆盖 loop.k）")
    run.add_argument("--hitl", dest="hitl", action="store_true", default=None,
                     help="每代挂起等人工 approve/rollback")
    run.add_argument("--no-hitl", dest="hitl", action="store_false", default=None)
    _common_run_id(run)
    _set_func(run, _pending("loop"), ["loop", "run"])

    for name, help_text in (("status", "查看 run 进度"), ("smoke", "冒烟跑一代（5 局 train）")):
        sp_run = _sp(sp, name, help=help_text)
        _common_run_id(sp_run)
        _set_func(sp_run, _pending("loop"), ["loop", name])
    p.set_defaults(func=None)


def _add_evolve(sub: Any) -> None:
    p = _sp(sub, "evolve", help="harness 自进化（MOD-07）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    for name in ("approve", "rollback"):
        sp_run = _sp(sp, name)
        _common_run_id(sp_run)
        sp_run.add_argument("--generation", type=int, required=True)
        sp_run.add_argument("--reason", default="")
        _set_func(sp_run, _pending("evolve"), ["evolve", name])
    p.set_defaults(func=None)


def _add_eval(sub: Any) -> None:
    p = _sp(sub, "eval", help="测量（MOD-08）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")

    rep = _sp(sp, "report", help="代际报告")
    _common_run_id(rep)
    rep.add_argument("--split", choices=["train", "heldout", "test"], default="heldout")
    rep.add_argument("--baseline", default="none", help="none|all|<kind>")
    _set_func(rep, _pending("eval"), ["eval", "report"])

    for name in ("ablation", "test"):
        sp_run = _sp(sp, name)
        _common_run_id(sp_run)
        sp_run.add_argument("--basket", choices=["H1", "H2", "H3"], default="H2")
        _set_func(sp_run, _pending("eval"), ["eval", name])

    div = _sp(sp, "diversity", help="动作熵 / 坍缩监控")
    _common_run_id(div)
    _set_func(div, _pending("eval"), ["eval", "diversity"])
    p.set_defaults(func=None)


def _add_audit(sub: Any) -> None:
    p = _sp(sub, "audit", help="泄漏审计（MOD-02，INV-2/3/5）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    leak = _sp(sp, "leak")
    _common_run_id(leak)
    _set_func(leak, _pending("audit"), ["audit", "leak"])
    p.set_defaults(func=None)


def _add_inference(sub: Any) -> None:
    p = _sp(sub, "inference", help="推理后端（MOD-03）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    for name in ("probe", "bench", "robustness"):
        sp_run = _sp(sp, name)
        sp_run.add_argument("--model", default=None, help="模型 id（默认 inference.model_id）")
        _set_func(sp_run, _pending("inference"), ["inference", name])
    p.set_defaults(func=None)


def _add_memory(sub: Any) -> None:
    p = _sp(sub, "memory", help="反思记忆库（MOD-05）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    for name in ("stats", "snapshot", "rollback", "verify", "retrieve"):
        sp_run = _sp(sp, name)
        _common_run_id(sp_run)
        _set_func(sp_run, _pending("memory"), ["memory", name])
    p.set_defaults(func=None)


# ---- guards（MOD-09 本模块实现）------------------------------------------------------

def _add_guards(sub: Any) -> None:
    p = _sp(sub, "guards", help="确定性守卫与分层 import 门禁（MOD-09）")
    sp = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    chk = _sp(sp, "check", help="扫描 root，违例退出码 4")
    chk.add_argument("--root", default="gamer_rsi", help="扫描根目录")
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
    """解析 ``train:1000-1004`` / ``heldout:2000-2099`` / ``9000,9001`` / ``file:seeds.txt``。

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

def cli_init_run(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """CLI 命令通用的启动序列（bootstrap 之后）：load_config → make_run_id → init_run_dir。"""
    cfg = load_config(_config_overrides(args), config_dir=Path(args.config_dir))
    run_id, _sha = make_run_id(cfg, explicit=getattr(args, "run_id", None))
    if getattr(args, "resume", False):
        paths = resolve_run_dir(run_id, cfg, Path(cfg.infra.runs_root), resume=True)
    else:
        paths = init_run_dir(run_id, cfg, Path(cfg.infra.runs_root))
    events.bind(paths.events, run_id)
    events.emit("infra.run_start", config_hash=config_hash(cfg),
                git_sha=git_short_sha(), run_id=run_id)
    return paths, cfg, run_id
