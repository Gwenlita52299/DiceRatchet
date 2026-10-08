"""MOD-07 HITL 门（PRD FR-07-11；§8.3；Spec §8.5）。

人在环（Human-In-The-Loop）的**最小**实现：MOD-07 只负责

1. 拒绝（reject）时**不挂起**——直接落报告、回 ``status="rejected"``；
2. 有 winner 且 ``evolve.hitl=true`` 时把决策置 ``pending``、写
   ``gen-{N}/gate.json``（``generation, harness_version, parent_version, diff_path,
   report_path, status, expires_at``），loop **优雅退出（exit 0，可续跑）**；
3. ``evolve approve|rollback --generation N`` 读 gate 文件 → 执行 accept/rollback，
   写 ``decided_by="human"`` 与可选 ``reason``；
4. 超时（``evolve.gate_timeout_hours``，默认 24h）→ **保守回滚** ``parent_version``，
   ``reason="hitl_timeout"``（``E_EVOLVE_GATE_TIMEOUT``）。

状态机：``pending → approved | rejected | timeout``（只前进一步，不可回退）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from dice_ratchet.infra import events
from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import read_json, write_json_atomic

from .errors import GateTimeoutError
from .paths import GenPaths, expires_at_from, utc_now
from .schemas import Candidate, GateState, Verdict, validate_gate_state

__all__ = [
    "GATE_STATUSES", "await_human", "expire_if_needed", "gate_expired", "load_gate",
    "raise_for_timeout", "read_gate", "resolve_gate", "verdict_from_gate", "write_gate",
]

GATE_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "timeout")

_ACCEPTED_ACTIONS: Mapping[str, str] = {"approve": "approved", "rollback": "rejected"}


# =====================================================================
# gate.json 读写
# =====================================================================

def write_gate(path: Path | str, state: GateState) -> str:
    """原子写 ``gate.json``（先自校验）。"""
    validate_gate_state(state.as_dict())
    return write_json_atomic(Path(path), state.as_dict())


def read_gate(path: Path | str) -> GateState | None:
    """读 ``gate.json``；不存在 → ``None``（**不**抛错：HITL 关闭时本来就没有）。"""
    target = Path(path)
    if not target.is_file():
        return None
    raw = read_json(target)
    if not isinstance(raw, Mapping):
        raise InfraError(E.E_EVOLVE_VERSION_CONFLICT,
                         f"gate.json 不是 JSON 对象：{target}")
    return GateState.from_dict(raw)


def gate_expired(state: GateState, *, now: datetime | None = None) -> bool:
    """``expires_at`` 已过（解析失败一律按"未过期"处理，交由人工裁决）。"""
    if state.status != "pending" or not state.expires_at:
        return False
    try:
        deadline = datetime.strptime(state.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    moment = now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc) > deadline


def load_gate(run_dir: Path | str, generation: int) -> GateState | None:
    return read_gate(GenPaths.of(run_dir, generation).gate)


# =====================================================================
# await_human（FR-07-11 的挂起点）
# =====================================================================

def await_human(*, generation: int, run_id: str, diff_path: Path | str,
                report_path: Path | str, cfg: Any, run_dir: Path | str | None = None,
                winner: Candidate | None = None, parent_version: int = 0,
                harness_version: int | None = None, now: Any = None) -> GateState:
    """HITL 门（FR-07-11）。

    - ``evolve.hitl == false`` → 直接 ``approved``（``decided_by="auto"``，**不写**
      ``gate.json``，PRD §11-6）；
    - 已有 ``gate.json`` 且状态非 ``pending`` → 原样返回（续跑幂等）；
    - 已有 ``gate.json`` 且 ``pending`` 且已超时 → ``timeout``（保守回滚，PRD §10）；
    - 其余 → 写 ``pending`` 并返回（由调用方优雅退出，exit 0）。
    """
    gen = int(generation)
    root = Path(run_dir) if run_dir is not None else Path("runs") / str(run_id)
    paths = GenPaths.of(root, gen)
    version = int(harness_version if harness_version is not None else parent_version)
    existing = read_gate(paths.gate)

    if not bool(getattr(cfg, "hitl", False)):
        state = GateState(generation=gen, status="approved", harness_version=version,
                          parent_version=int(parent_version),
                          diff_path=str(diff_path), report_path=str(report_path),
                          expires_at="", created_at=utc_now(now), resolved_by="auto",
                          reason="hitl_disabled",
                          candidate_id=(winner.candidate_id if winner else ""))
        events.emit("evolve.gate", generation=gen, status="approved", waited_s=0.0,
                    decided_by="auto")
        return state

    if existing is not None and existing.status != "pending":
        if existing.status == "timeout":
            events.emit("evolve.gate", generation=gen, status="timeout", waited_s=0.0,
                        decided_by=existing.resolved_by or "auto")
        return existing

    created_at = utc_now(now)
    deadline = expires_at_from(
        created_at, float(getattr(cfg, "gate_timeout_hours", 24)))
    state = GateState(
        generation=gen, status="pending", harness_version=version,
        parent_version=int(parent_version), diff_path=str(diff_path),
        report_path=str(report_path), expires_at=deadline, created_at=created_at,
        resolved_by="", reason="",
        candidate_id=(winner.candidate_id if winner else ""))
    paths.ensure()
    write_gate(paths.gate, state)
    events.emit("evolve.gate", generation=gen, status="pending", waited_s=0.0,
                decided_by="auto")
    events.emit("evolve.hitl_pending", generation=gen)
    return state


def expire_if_needed(state: GateState, *, run_dir: Path | str, now: datetime | None = None
                     ) -> GateState:
    """把已超时的 ``pending`` 推进为 ``timeout``（保守回滚；``E_EVOLVE_GATE_TIMEOUT``）。"""
    if not gate_expired(state, now=now):
        return state
    paths = GenPaths.of(run_dir, state.generation)
    updated = replace(state, status="timeout", resolved_by="auto",
                      reason="hitl_timeout", error_code=str(E.E_EVOLVE_GATE_TIMEOUT))
    write_gate(paths.gate, updated)
    events.emit("evolve.gate", generation=int(state.generation), status="timeout",
                waited_s=0.0, decided_by="auto")
    return updated


# =====================================================================
# resolve_gate（CLI：evolve approve|rollback）
# =====================================================================

def resolve_gate(*, generation: int, run_id: str,
                 action: Literal["approve", "rollback"], reason: str | None = None,
                 run_dir: Path | str | None = None, decided_by: str = "human",
                 cfg: Any = None) -> GateState:
    """人工裁决：读 ``gate.json`` → 置 ``approved``/``rejected`` → 写回（FR-07-11）。

    ``approved`` → accept；``rejected`` → 回滚 ``parent_version``。
    实现细节（accept 本身）由 ``generation.run_one_generation`` 在续跑时执行，
    本函数只推进 HITL 状态机，保证「CLI 与 loop 都只认 gate.json 这一份事实」。
    """
    gen = int(generation)
    root = Path(run_dir) if run_dir is not None else Path("runs") / str(run_id)
    paths = GenPaths.of(root, gen)
    state = read_gate(paths.gate)
    if state is None:
        raise InfraError(
            E.E_EVOLVE_JUDGE_FAILED,
            f"gen-{gen:03d} 没有 gate.json：该代没有挂起等人工裁决",
            hint=f"gate 文件路径：{paths.gate}；HITL 关闭时不会产生 gate.json"
                 "（decided_by=auto）")
    if state.status != "pending":
        return state
    status = _ACCEPTED_ACTIONS.get(str(action), "")
    if not status:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"未知 HITL 动作：{action!r}",
                         hint="合法值：approve｜rollback")
    updated = replace(state, status=status, resolved_by=str(decided_by or "human"),
                      reason=str(reason or ""))
    write_gate(paths.gate, updated)
    events.emit("evolve.gate", generation=gen, status=status, waited_s=0.0,
                decided_by=updated.resolved_by)
    return updated


def verdict_from_gate(state: GateState, *, generation: int | None = None) -> Verdict:
    """把已裁决的 ``GateState`` 翻成 ``Verdict``（供 accept/rollback 分流）。"""
    gen = int(generation if generation is not None else state.generation)
    if state.status == "approved":
        return Verdict(decision="accept", decided_by=state.resolved_by or "human",
                       reason=state.reason, candidate_id=state.candidate_id,
                       generation=gen)
    if state.status in ("rejected", "timeout"):
        return Verdict(
            decision="rollback", decided_by=state.resolved_by or "human",
            reason=state.reason or ("hitl_timeout" if state.status == "timeout" else ""),
            error_code=(str(E.E_EVOLVE_GATE_TIMEOUT) if state.status == "timeout"
                        else state.error_code),
            candidate_id=state.candidate_id, generation=gen)
    return Verdict(decision="pending", decided_by="auto", reason="hitl_pending",
                   candidate_id=state.candidate_id, generation=gen)


def raise_for_timeout(state: GateState) -> None:
    """超时 → ``E_EVOLVE_GATE_TIMEOUT``（调用方据此走保守回滚）。"""
    if state.status == "timeout":
        raise GateTimeoutError(
            f"gen-{int(state.generation):03d} HITL 超时（expires_at={state.expires_at}）",
            hint="按 Spec §8.5 保守回滚 parent_version，reason=hitl_timeout")
