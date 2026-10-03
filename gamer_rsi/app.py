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
from typing import Sequence

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


if __name__ == "__main__": # pragma: no cover - 便于 `python -m gamer_rsi.app`
    sys.exit(main(reexec=True))
