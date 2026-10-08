"""MOD-05 记忆库 CLI（PRD §7.4，B-08；经组合根注册进 MOD-09 命令面）::

    python -m dice_ratchet memory stats    --run-id <run_id>
    python -m dice_ratchet memory snapshot --run-id <run_id> --reason gen-003
    python -m dice_ratchet memory rollback --run-id <run_id> --to-version 4 [--dry-run]
    python -m dice_ratchet memory verify   --run-id <run_id> [--full] [--tick 120]
    python -m dice_ratchet memory retrieve --run-id <run_id> [--obs obs.json] [--top-k 8]
                                        [--token-budget 900] [--tick 120]

命令面参数与退出码由 ``infra.cli`` 冻结；实现经 ``dice_ratchet/app.py::wire()`` 注册
（NFR-09-5：``infra`` 不 import 任何子包）。退出码：``0`` 成功 / ``1`` 领域失败（verify
不健康）/ ``2`` 配置错误（回滚目标缺失）/ ``5`` IO 失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from dice_ratchet.infra import cli as infra_cli
from dice_ratchet.infra.errors import E, EXIT_DOMAIN, InfraError
from dice_ratchet.infra.io import read_json

from .snapshot import snapshot_path
from .store import MemoryStore

__all__ = ["HANDLERS", "cmd_stats", "cmd_snapshot", "cmd_rollback", "cmd_verify",
           "cmd_retrieve", "open_store"]


def open_store(args: argparse.Namespace) -> tuple[MemoryStore, Any, str]:
    """定位/创建 run 并构造 ``MemoryStore``（空 run 上也可直接执行，G6）。"""
    paths, cfg, run_id = infra_cli.cli_init_run(args, require_state=False)
    store = MemoryStore(paths.root, cfg.memory, config_dir=Path(args.config_dir),
                        run_id=run_id)
    return store, paths, run_id


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


# =====================================================================
# stats
# =====================================================================

def cmd_stats(args: argparse.Namespace) -> int:
    store, _paths, run_id = open_store(args)
    payload = {"run_id": run_id, **store.stats().to_dict()}
    payload["snapshots"] = [version for version, _ in store.snapshots()]
    _print(payload)
    return 0


# =====================================================================
# snapshot
# =====================================================================

def cmd_snapshot(args: argparse.Namespace) -> int:
    store, _paths, _run_id = open_store(args)
    reason = str(getattr(args, "reason", None) or "manual")
    version = getattr(args, "memory_version", None)
    path = store.snapshot(reason, memory_version=version)
    store.close()
    _print({"snapshot": str(path), "memory_version": store.memory_version,
            "parent_memory_version": store.parent_memory_version, "reason": reason})
    return 0


# =====================================================================
# rollback
# =====================================================================

def cmd_rollback(args: argparse.Namespace) -> int:
    store, _paths, _run_id = open_store(args)
    to_version = getattr(args, "to_version", None)
    if to_version is None:
        raise InfraError(E.E_IO_CONFIG_INVALID, "memory rollback 必须指定 --to-version",
                         hint="用法：python -m dice_ratchet memory rollback "
                              "--run-id <run_id> --to-version 4 [--dry-run]")
    report = store.rollback(int(to_version), dry_run=bool(getattr(args, "dry_run", False)))
    store.close()
    if report.dry_run:
        print(f"memory_version: {report.from_version} -> {report.to_version} "
              f"(dry-run, would restore from v{report.target_version}, "
              f"entries {report.entries_delta:+d}, active {report.active_delta:+d})")
    else:
        print(f"memory_version: {report.from_version} -> {report.to_version} "
              f"(restored from v{report.target_version}, "
              f"harness_version={report.harness_version})")
    _print(report.to_dict())
    return 0


# =====================================================================
# verify
# =====================================================================

def cmd_verify(args: argparse.Namespace) -> int:
    store, _paths, _run_id = open_store(args)
    tick = getattr(args, "tick", None)
    report = store.verify(tick, full=bool(getattr(args, "full", False)))
    store.close()
    _print(report.to_dict())
    if not report.healthy:
        print(f"[{E.E_IO_MEMORY_CORRUPT}] verify 发现坏行/重复/泄漏："
              f"bad={report.bad_lines} dup={report.duplicates} "
              f"sha={report.sha256_mismatch} leak={report.leak_hits}", file=sys.stderr)
        return EXIT_DOMAIN
    return 0


# =====================================================================
# retrieve
# =====================================================================

def _load_obs(raw: str | None) -> dict[str, Any]:
    """``--obs`` 接受文件路径或内联 JSON；缺省用最小观测（``top_k`` 仍可返回同情境条目）。"""
    if not raw:
        return {"phase": "player_turn"}
    text = raw
    candidate = Path(raw)
    if candidate.is_file():
        text = candidate.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"--obs 既不是可读文件也不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise InfraError(E.E_IO_CONFIG_INVALID, "--obs 顶层必须是 JSON 对象")
    return payload


def cmd_retrieve(args: argparse.Namespace) -> int:
    store, _paths, _run_id = open_store(args)
    obs = _load_obs(getattr(args, "obs", None))
    tags_hint = tuple(t for t in str(getattr(args, "tags", "") or "").split(",") if t)
    result = store.retrieve(obs, top_k=getattr(args, "top_k", None),
                            token_budget=getattr(args, "token_budget", None),
                            tick=int(getattr(args, "tick", 0) or 0),
                            tags_hint=tags_hint)
    store.close()
    if result.text:
        print(result.text)
    _print(result.to_dict())
    return 0


#: 命令路径 → 实现（组合根 ``dice_ratchet/app.py::wire()`` 注册进 ``infra.cli``）。
HANDLERS: dict[tuple[str, ...], Callable[[argparse.Namespace], int]] = {
    ("memory", "stats"): cmd_stats,
    ("memory", "snapshot"): cmd_snapshot,
    ("memory", "rollback"): cmd_rollback,
    ("memory", "verify"): cmd_verify,
    ("memory", "retrieve"): cmd_retrieve,
}
