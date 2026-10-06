"""``evolve approve|rollback`` 的实现（Spec §4.6；PRD FR-07-11；§8.3）。

可复现命令（MOD-07 PRD §3，G6 必须可直接复制执行）::

    python -m gamer_rsi loop run --generations 3 --k 30 --hitl
    python -m gamer_rsi evolve approve  --generation 1 [--reason "..."]
    python -m gamer_rsi evolve rollback --generation 1 --reason "回滚理由"
    python -m gamer_rsi loop run --run-id <run_id> --resume

CLI 的职责**只有推进 HITL 状态机**（读 ``gen-{N}/gate.json`` → 置
``approved``/``rejected`` → 写回）：``accept()`` / ``rollback()`` 由 MOD-06 续跑的
``run_one_generation`` 执行。这样「CLI 与 loop 只认 gate.json 这一份事实」，也保证
版本号只由一条路径产生（NFR-07-6 幂等）。

退出码（Spec §8.3 / PRD §10）：``0`` 成功 / ``2`` 配置错误（无 gate.json 等）/
``4`` 沙箱守卫违例。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from ..infra import cli as infra_cli
from ..infra.errors import E, InfraError
from . import gate as gate_mod
from .paths import GenPaths

__all__ = ["HANDLERS", "cmd_evolve_approve", "cmd_evolve_rollback"]

_HITL_ACTIONS: dict[str, str] = {"approve": "approve", "rollback": "rollback"}


def _run_root(args: argparse.Namespace, cfg: Any, run_id: str) -> Path:
    """``runs/<run_id>/``；缺 ``--run-id`` 或目录不存在 → ``E_IO_CONFIG_INVALID``。"""
    if not run_id:
        raise InfraError(E.E_IO_CONFIG_INVALID, "evolve approve|rollback 必须指定 --run-id",
                         hint="用法：python -m gamer_rsi evolve approve --run-id <run_id> "
                              "--generation 1")
    root = Path(getattr(cfg.infra, "runs_root", "runs")) / str(run_id)
    if not root.is_dir():
        raise InfraError(E.E_IO_CONFIG_INVALID, f"run 目录不存在：{root}",
                         hint="检查 --run-id 是否正确（见 runs/ 下的目录名）")
    return root


def _load(args: argparse.Namespace) -> tuple[Any, str, Path]:
    cfg = infra_cli.load_config(infra_cli.config_overrides(args),
                                config_dir=Path(args.config_dir))
    run_id = str(getattr(args, "run_id", "") or "")
    return cfg, run_id, _run_root(args, cfg, run_id)


def _resolve(args: argparse.Namespace, action: str) -> int:
    cfg, run_id, root = _load(args)
    generation = int(getattr(args, "generation", 0) or 0)
    reason = str(getattr(args, "reason", "") or "")
    state = gate_mod.resolve_gate(generation=generation, run_id=run_id, action=action,
                                  reason=reason or None, run_dir=root)
    paths = GenPaths.of(root, generation)
    payload = {"run_id": run_id, "generation": int(generation), "action": action,
               "gate": state.as_dict(), "gate_path": str(paths.gate),
               "report_path": str(paths.report)}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if state.status == "pending": # pragma: no cover - resolve_gate 保证不会
        raise InfraError(E.E_EVOLVE_JUDGE_FAILED, "gate 仍为 pending：裁决未生效")
    print(f"gate={state.status}；执行 accept/rollback："
          f"python -m gamer_rsi loop run --run-id {run_id} --resume", file=__import__("sys").stderr)
    return 0


def cmd_evolve_approve(args: argparse.Namespace) -> int:
    """``evolve approve``：人工确认候选 → ``gate.status=approved``。"""
    return _resolve(args, _HITL_ACTIONS["approve"])


def cmd_evolve_rollback(args: argparse.Namespace) -> int:
    """``evolve rollback``：人工拒绝候选 → ``gate.status=rejected``（回滚 parent）。"""
    return _resolve(args, _HITL_ACTIONS["rollback"])


HANDLERS: dict[tuple[str, ...], Callable[[argparse.Namespace], int]] = {
    ("evolve", "approve"): cmd_evolve_approve,
    ("evolve", "rollback"): cmd_evolve_rollback,
}
