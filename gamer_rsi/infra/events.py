"""事件注册表与统一埋点出口（FR-09-7 / Spec §8.2）。

- 事件名 ``<module>.<event>``，全部登记在 ``EVENT_REGISTRY``，统一落到
  ``runs/<run_id>/events.jsonl``。
- ``emit(event, **fields)`` 自动附加 ``ts`` / ``run_id`` / ``module``。
- 未登记事件：开发模式抛 ``E_IO_EVENT_UNKNOWN``；生产模式改写为
  ``infra.unknown_event`` 并保留原名。
- 轨迹行不是事件，走 ``JsonlWriter`` 写入 ``episodes/``。

登记来源为各 PRD「指标与埋点」节；新增事件必须同步本表（Spec §8.2）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from .errors import E, InfraError
from .logging import JsonlWriter

__all__ = ["EventSpec", "EVENT_REGISTRY", "emit", "bind", "unbind", "is_registered",
           "set_dev_mode", "dev_mode", "event_names", "stats", "redaction_event"]

_DEV_ENV: Final[str] = "GAMERSI_DEV"


@dataclass(frozen=True)
class EventSpec:
    """一个已登记事件的规格。"""

    name: str # "<module>.<event>"
    module: str
    description: str
    required_fields: frozenset[str] = field(default_factory=frozenset)


def _spec(module: str, event: str, desc: str = "",
          required: frozenset[str] | None = None) -> tuple[str, EventSpec]:
    name = f"{module}.{event}"
    return name, EventSpec(name=name, module=module, description=desc,
                           required_fields=frozenset(required or ()))


def _build_registry() -> dict[str, EventSpec]:
    entries: list[tuple[str, EventSpec]] = []
    # ---- infra（MOD-09 本模块）---------------------------------------------------
    for ev, req in [
        ("run_start", frozenset({"config_hash"})),
        ("run_end", frozenset({"exit_code"})),
        ("guard_finding", frozenset({"code"})),
        ("redaction_hit", frozenset({"hits"})),
        ("unknown_event", frozenset({"original"})),
        ("config_unknown_key", frozenset({"keys"})),
        ("trailing_truncated", frozenset({"path"})),
        ("plot_unavailable", frozenset()),
    ]:
        entries.append(_spec("infra", ev, "MOD-09 工程横切事件", req))
    # ---- env（MOD-01/02 转发内核事件）----------------------------------------------
    for ev in ["selftest_pass", "selftest_fail", "parity_mismatch", "invariant_violation",
               "projection_pruned", "import_denied", "ast_denied", "resource_limit",
               "honeypot_escape"]:
        entries.append(_spec("env", ev, "MOD-01/02 环境/沙箱事件"))
    # ---- sandbox（MOD-02）----------------------------------------------------------
    for ev, req in [("gate_pass", frozenset({"harness_version"})),
                    ("gate_reject", frozenset({"violations"})),
                    ("tool_timeout", frozenset({"tool"})),
                    ("honeypot_hit", frozenset({"sentinel"})),
                    ("audit_finding", frozenset({"severity"}))]:
        entries.append(_spec("sandbox", ev, "MOD-02 沙箱门禁/审计事件", req))
    # ---- llm（MOD-03）--------------------------------------------------------------
    for ev, req in [("call", frozenset({"model_id", "latency_ms"})),
                    ("degrade", frozenset({"code"})),
                    ("oom", frozenset({"code"}))]:
        entries.append(_spec("llm", ev, "MOD-03 推理后端事件", req))
    # ---- policy（MOD-04）-----------------------------------------------------------
    for ev, req in [("decision", frozenset({"episode_key", "source"})),
                    ("fallback", frozenset({"reason"})),
                    ("reject", frozenset({"reason"}))]:
        entries.append(_spec("policy", ev, "MOD-04 决策事件", req))
    # ---- memory（MOD-05；含各 PRD 文档的下划线变体别名）------------------------------
    for ev, req in [
        ("add", frozenset({"entry_id", "result"})),
        ("dedup", frozenset({"survivor_id"})),
        ("evict", frozenset({"entry_id", "evict_reason"})),
        ("merge", frozenset({"survivor_id", "merged_id"})),
        ("conflict", frozenset({"entry_ids"})),
        ("quarantine", frozenset({"reason"})),
        ("snapshot", frozenset({"to_version", "content_sha256"})),
        ("rollback", frozenset({"from_version", "to_version"})),
        ("retrieve", frozenset({"obs_digest", "returned_ids"})),
        ("retrieve_failed", frozenset({"code"})),
        ("write_failed", frozenset({"code"})),
        ("seed_leak_blocked", frozenset({"interception_point"})),
    ]:
        entries.append(_spec("memory", ev, "MOD-05 反思记忆事件", req))
    # ---- loop（MOD-06）--------------------------------------------------------------
    for ev, req in [
        ("episode_start", frozenset({"episode_key", "seed", "split", "arm"})),
        ("episode_end", frozenset({"episode_key", "outcome"})),
        ("cycle_start", frozenset({"cycle", "e_gen"})),
        ("budget_exhausted", frozenset({"e_gen", "cap"})),
        ("interrupted", frozenset({"phase"})),
        ("resume", frozenset({"phase"})),
        ("reflect", frozenset({"episode_key", "status"})),
        ("io_latency", frozenset({"p50_ms", "p99_ms"})),
    ]:
        entries.append(_spec("loop", ev, "MOD-06 闭环编排事件", req))
    # ---- evolve（MOD-07）-------------------------------------------------------------
    for ev, req in [
        ("propose", frozenset({"gen", "n_candidates"})),
        ("validate", frozenset({"candidate_id", "status"})),
        ("scan", frozenset({"candidate_id", "status"})),
        ("smoke", frozenset({"candidate_id", "win_rate"})),
        ("evaluate", frozenset({"candidate_id"})),
        ("gate", frozenset({"generation", "status"})),
        ("judge", frozenset({"gen", "decision"})),
        ("decision", frozenset({"gen", "decision", "decided_by"})),
        ("version", frozenset({"from_version", "to_version", "git_commit"})),
        ("hitl_pending", frozenset({"generation"})),
        ("rollback", frozenset({"to_version"})),
        ("cost", frozenset({"episodes_spent", "episodes_cap"})),
    ]:
        entries.append(_spec("evolve", ev, "MOD-07 harness 自进化事件", req))
    # ---- eval（MOD-08）----------------------------------------------------------------
    for ev, req in [
        ("start", frozenset({"split", "basket"})),
        ("seed_done", frozenset({"seed", "outcome"})),
        ("report", frozenset({"generation"})),
        ("collapse", frozenset({"generation", "flag"})),
    ]:
        entries.append(_spec("eval", ev, "MOD-08 测量事件", req))
    return dict(entries)


EVENT_REGISTRY: Final[Mapping[str, EventSpec]] = _build_registry()

# 部分 PRD 用 ``<module>_<event>`` 下划线写法登记埋点；统一映射到点分规范名（Spec §8.2）
_ALIASES: Final[Mapping[str, str]] = {
    "memory_retrieve": "memory.retrieve",
    "memory_snapshot": "memory.snapshot",
    "memory_rollback": "memory.rollback",
    "memory_evict": "memory.evict",
    "memory_add": "memory.add",
    "loop_episode_start": "loop.episode_start",
    "loop_episode_end": "loop.episode_end",
    "evolve_decision": "evolve.decision",
}

# ---- 运行时绑定 ---------------------------------------------------------------------

_sink: JsonlWriter | None = None
_run_id: str | None = None
_dev: bool = os.environ.get(_DEV_ENV, "0") not in ("0", "", "false", "False")
_dropped: int = 0
_emitted: int = 0


def set_dev_mode(enabled: bool | None = None) -> bool:
    """切换开发模式：未登记事件抛异常而非降级。``None`` 读环境变量。"""
    global _dev
    if enabled is None:
        _dev = os.environ.get(_DEV_ENV, "0") not in ("0", "", "false", "False")
    else:
        _dev = enabled
    return _dev


def dev_mode() -> bool:
    return _dev


def bind(events_path: Path, run_id: str, *, append: bool = True) -> JsonlWriter:
    """绑定 ``events.jsonl`` 作为唯一落点（bootstrap 调用，Spec §8）。"""
    global _sink, _run_id
    unbind()
    _run_id = run_id
    _sink = JsonlWriter(Path(events_path), append=append)
    return _sink


def unbind() -> None:
    global _sink, _run_id
    if _sink is not None:
        _sink.close()
    _sink = None
    _run_id = None


def is_registered(event: str) -> bool:
    return event in EVENT_REGISTRY or event in _ALIASES


def event_names() -> list[str]:
    return sorted(EVENT_REGISTRY)


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def emit(event: str, **fields: Any) -> None:
    """写一行结构化事件，自动附加 ``ts`` / ``run_id`` / ``module``。

    未登记事件：开发模式抛 ``E_IO_EVENT_UNKNOWN``；生产模式改写为
    ``infra.unknown_event``（保留 ``original``）。
    """
    global _dropped, _emitted
    name = _ALIASES.get(event, event)
    spec = EVENT_REGISTRY.get(name)
    if spec is None:
        if _dev:
            raise InfraError(E.E_IO_EVENT_UNKNOWN,
                             f"事件 {event!r} 未登记在 EVENT_REGISTRY",
                             hint=f"合法事件见 gamer_rsi/infra/events.py（共 {len(EVENT_REGISTRY)} 个）；"
                                  f"新增事件需先登记（Spec §8.2）")
        unknown = {"original": event, "ts": _utcnow_iso(),
                   "run_id": _run_id, "module": "infra", **fields}
        _write("infra.unknown_event", unknown)
        return
    missing = sorted(spec.required_fields - set(fields))
    if missing and _dev:
        raise InfraError(E.E_IO_EVENT_UNKNOWN,
                         f"事件 {name!r} 缺必填字段 {missing}",
                         hint=f"字段定义见 gamer_rsi/infra/events.py")
    row = {"ts": _utcnow_iso(), "run_id": _run_id, "module": spec.module, **fields}
    _write(name, row)


def _write(name: str, row: dict[str, Any]) -> None:
    global _dropped, _emitted
    if _sink is None:
        # 未绑定 run（如独立运行的 guards check）：丢弃并计数，不阻断
        _dropped += 1
        if _dev:
            print(f"[events] (unbound, dropped) {name}: {row}", file=sys.stderr, flush=True)
        return
    row = {"event": name, **row}
    _sink.write(row)
    _emitted += 1


def redaction_hit(hits: int, **extra: Any) -> None:
    """FR-09-9：redaction 命中 > 0 时写 ``infra.redaction_hit``。"""
    emit("infra.redaction_hit", hits=hits, **extra)


def stats() -> dict[str, int]:
    return {"emitted": _emitted, "dropped_unbound": _dropped}
