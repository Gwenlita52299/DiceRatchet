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
from typing import Any, Callable, Final, Mapping

from .errors import E, InfraError
from .logging import REDACTED_SURFACES, JsonlWriter

__all__ = ["EventSpec", "EVENT_REGISTRY", "emit", "bind", "unbind", "is_registered",
           "set_dev_mode", "dev_mode", "event_names", "stats", "redaction_hit",
           "is_bound", "drain_latency_ms", "make_redacted_writer",
           "register_redaction_alert"]

_DEV_ENV: Final[str] = "DICERATCHET_DEV"


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
        ("plot_skipped", frozenset({"generation"})),
        ("plot_done", frozenset({"generation", "files"})),
        ("plot_missing_split", frozenset({"split"})),
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
                    ("exec", frozenset({"tool", "duration_ms", "outcome"})),
                    ("honeypot_hit", frozenset({"sentinel"})),
                    ("honeypot_selftest", frozenset({"cases", "caught"})),
                    ("audit_finding", frozenset({"severity"}))]:
        entries.append(_spec("sandbox", ev, "MOD-02 沙箱门禁/审计事件", req))
    # ---- llm（MOD-03）--------------------------------------------------------------
    for ev, req in [("call", frozenset({"model_id", "latency_ms"})),
                    ("degrade", frozenset({"code"})),
                    ("oom", frozenset({"code"})),
                    # Issue #1：API provider（deepseek）的调用与取证事件。审计工件落在
                    # runs/<run_id>/llm_api/，事件侧只记 hash / usage / 成本 / 来源。
                    ("api.call", frozenset({"model_id", "request_hash", "response_hash",
                                            "latency_ms", "api_key_source"})),
                    ("nondeterministic", frozenset({"code", "request_hash", "response_hash",
                                                    "previous_response_hash"})),
                    ("api.key_file_permissions", frozenset({"path"})),
                    ("api.record_failed", frozenset({"code", "path"})),
                    ("api.record_dir_fallback", frozenset({"path"}))]:
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
        ("ledger", frozenset({"generation", "status"})),
        # 2026-10-17（B-95）：起点守卫与检查点/继承/死路事件此前**只 emit 未登记**
        # ⇒ 生产模式被改写成 infra.unknown_event（丢名字），DICERATCHET_DEV=1 直接抛
        # E_IO_EVENT_UNKNOWN。这里按 emit 处实际字段补登记。
        ("start_guard", frozenset({"status", "play_hash"})),
        ("parent_inherit", frozenset({"generation", "status", "source"})),
        ("proposal_checkpoint", frozenset({"gen", "status"})),
        ("proposal_reused", frozenset({"gen", "n_candidates"})),
        ("dead_end", frozenset({"generation", "candidate_id"})),
        ("play_hash", frozenset({"generation", "status"})),
    ]:
        entries.append(_spec("evolve", ev, "MOD-07 harness 自进化事件", req))
    # ---- eval（MOD-08 PRD §13：eval.start/seed_start/seed_done/report/guard_violation/
    #      collapse/test_unlock）----------------------------------------------------------
    for ev, req in [
        ("start", frozenset({"split", "basket"})),
        ("seed_start", frozenset({"seed", "split", "basket"})),
        ("seed_done", frozenset({"seed", "outcome"})),
        ("report", frozenset({"generation"})),
        ("nondeterministic", frozenset({"generation", "determinism"})),
        ("guard_violation", frozenset({"error_code", "split", "destination"})),
        ("collapse", frozenset({"generation", "flag"})),
        ("test_unlock", frozenset({"generation", "attestation_path"})),
        # 2026-10-17：以下 6 个事件原先 emit 了但未登记（生产丢名字）。arm_reused 由
        # `evals/harness.py` 用复数前缀发出，靠 `_ALIASES` 归一为 `eval.arm_reused`。
        ("arm_reused", frozenset({"generation", "split", "basket", "arm", "n"})),
        ("generation_unresolved", frozenset({"split", "generation", "n"})),
        ("train_insufficient", frozenset({"generation", "run_id", "n", "expected"})),
        ("ledger_version_conflict", frozenset({"generation", "arm", "column"})),
        ("acceptance_skipped", frozenset({"code"})),
        ("kind_distribution_skipped", frozenset({"code"})),
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
    "evals.arm_reused": "eval.arm_reused",
    "evals.generation_unresolved": "eval.generation_unresolved",
    "evals.train_insufficient": "eval.train_insufficient",
    "evals.ledger_version_conflict": "eval.ledger_version_conflict",
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


def bind(events_path: Path, run_id: str, *, append: bool = True,
         fsync_every: int = 0, rotate_max_mb: int = 512) -> JsonlWriter:
    """绑定 ``events.jsonl`` 作为唯一落点（bootstrap 调用，Spec §8）。

    ``fsync_every`` / ``rotate_max_mb`` 来自 ``infra.*`` 配置（PRD §9），由调用方传入。
    """
    global _sink, _run_id
    unbind()
    _run_id = run_id
    _sink = JsonlWriter(Path(events_path), append=append, fsync_every=fsync_every,
                        rotate_max_mb=rotate_max_mb)
    return _sink


def unbind() -> None:
    global _sink, _run_id
    if _sink is not None:
        _sink.close()
    _sink = None
    _run_id = None


def is_bound() -> bool:
    return _sink is not None


def drain_latency_ms() -> list[float]:
    """取出（并清空）事件 sink 的单行写入耗时样本，供 ``metrics/io_latency.csv``（§13）。"""
    return _sink.drain_latency_ms() if _sink is not None else []


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
                             hint=f"合法事件见 dice_ratchet/infra/events.py（共 {len(EVENT_REGISTRY)} 个）；"
                                  f"新增事件需先登记（Spec §8.2）")
        unknown = {"original": event, "ts": _utcnow_iso(),
                   "run_id": _run_id, "module": "infra", **fields}
        _write("infra.unknown_event", unknown)
        return
    missing = sorted(spec.required_fields - set(fields))
    if missing and _dev:
        raise InfraError(E.E_IO_EVENT_UNKNOWN,
                         f"事件 {name!r} 缺必填字段 {missing}",
                         hint=f"字段定义见 dice_ratchet/infra/events.py")
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
    """FR-09-9：redaction 命中 > 0 时写 ``infra.redaction_hit``（并带 E_IO_REDACTION_HIT）。"""
    emit("infra.redaction_hit", hits=hits, code=E.E_IO_REDACTION_HIT, **extra)


# MOD-02 告警回调：redaction 命中本身说明发生了 INV-5 泄漏，只擦除不阻断（FR-09-9）
_ALERT_CALLBACK: Callable[[int, Any], None] | None = None


def register_redaction_alert(callback: Callable[[int, Any], None] | None) -> None:
    """注册 MOD-02 告警回调（``callback(hits, scrubbed_obj)``）；组合根或 MOD-02 调用。"""
    global _ALERT_CALLBACK
    _ALERT_CALLBACK = callback


def make_redacted_writer(path: Path, redactor: Any, *, surface: str,
                         fsync_every: int = 0,
                         rotate_max_mb: int = 512) -> JsonlWriter:
    """为 agent 可见的**三个出口**构造带 redaction 的 JSONL 写入器（FR-09-9 / B-19）。

    ``surface`` 必须是 ``prompts`` / ``memory`` / ``reflections`` 之一——作用域在 API 层
    强制，避免轨迹（``episodes/``）、``events.jsonl``、``generations/`` 被误加 redaction
    （它们是审计侧，MOD-08 的配对检验需要原始 seed）。

    命中 > 0 时：写 ``infra.redaction_hit`` 事件 + 触发已注册的 MOD-02 告警回调。
    """
    if surface not in REDACTED_SURFACES:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"redaction 只允许作用于 {REDACTED_SURFACES}，收到 surface={surface!r}",
                         hint="轨迹/事件/generations 属审计侧，不做 redaction（B-19）")
    path = Path(path)

    def _on_hit(hits: int, scrubbed: Any) -> None:
        redaction_hit(hits, surface=surface, path=str(path))
        if _ALERT_CALLBACK is not None:
            _ALERT_CALLBACK(hits, scrubbed)

    return JsonlWriter(path, fsync_every=fsync_every, rotate_max_mb=rotate_max_mb,
                       redactor=redactor, on_redaction_hit=_on_hit)


def stats() -> dict[str, int]:
    return {"emitted": _emitted, "dropped_unbound": _dropped}
