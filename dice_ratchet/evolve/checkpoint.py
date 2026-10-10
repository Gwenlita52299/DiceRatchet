"""提案检查点（F42-B）：一代的提案在**评估之前**落盘，resume 时指纹一致则复用。

为什么要它：MOD-07 的 resume 单元原先只有**整代**（``loop/generation.py::_cycle_artifacts_exist``）。
一代 84 局评估 ≈ 3.75 h / ≈¥47（实测 ``m2-s32b-smoke-c1-medium6`` 的 gen-1），中断即再付一次。
而 API 提案**不可复现**（注册表标注 ``api_supports_seed=false``，实测「字节相同的请求 9 次
返回 9 个不同响应」）——所以**只把 parent 的评估复用起来是不够的**：候选 id 一变，
候选臂的 ``metrics/arms/`` 归档必然 miss（归档路径含 ``cand-<id>``），能省的只有 parent 侧。

机制（按裁决「A+B+C 的 B」）：

1. 评估**之前**把 ``ProposalResult`` 落 ``generations/gen{gen:03d}/proposals/proposal.json``，
   连同**输入指纹**：``run_id`` / ``generation`` / ``propose_seed`` / ``harness_version`` /
   ``memory_version`` / ``prompt_digest``（渲染后的 system+user+schema 的 sha256）；
2. resume 时重算指纹，一致 → 复用同一 ``candidate_id`` 与 diff，**0 次 proposer 调用**；
   不一致（换了 harness/记忆/模板/seed）→ 调用方照常重新提案（fail-closed，绝不静默复用）。

指纹只覆盖**输入**，不覆盖模型权重（proposer 客户端不向 MOD-07 暴露模型指纹）：同输入 +
换模型仍会命中检查点。这是**有意的保守选择**——复用已评估过的候选比重新烧钱更安全；
模型换了要重提案时，清掉 ``proposals/`` 即可。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from dice_ratchet.infra import events
from dice_ratchet.infra.errors import InfraError
from dice_ratchet.infra.io import read_json, write_json_atomic

from .schemas import Candidate, ProposalResult

__all__ = ["PROPOSAL_CHECKPOINT_VERSION", "proposal_fingerprint", "proposal_checkpoint_path",
           "load_proposal_checkpoint", "save_proposal_checkpoint"]

#: 检查点格式版本（结构变化时递增；版本不符 → 不复用）。
PROPOSAL_CHECKPOINT_VERSION: int = 1


def proposal_fingerprint(*, run_id: str, generation: int, propose_seed: int,
                         harness_version: int, memory_version: int,
                         inputs: Mapping[str, Any]) -> str:
    """提案输入的指纹（sha256；``prompt_digest`` = 渲染后的三段）。"""
    payload = {
        "run_id": str(run_id),
        "generation": int(generation),
        "propose_seed": int(propose_seed),
        "harness_version": int(harness_version),
        "memory_version": int(memory_version),
        "prompt_digest": _digest_of(inputs),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_of(inputs: Mapping[str, Any]) -> str:
    text = json.dumps(dict(inputs), sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def proposal_checkpoint_path(run_root: Path | str, generation: int) -> Path:
    """``generations/gen{gen:03d}/proposals/proposal.json``。"""
    return Path(run_root) / "generations" / f"gen-{int(generation):03d}" / "proposals" \
        / "proposal.json"


def save_proposal_checkpoint(run_root: Path | str, *, generation: int, fingerprint: str,
                             proposal: ProposalResult,
                             inputs: Mapping[str, Any] | None = None) -> Path:
    """落检查点：候选 diff / rationale / ``expected_effect`` + 输入指纹。"""
    path = proposal_checkpoint_path(run_root, generation)
    payload = {
        "version": PROPOSAL_CHECKPOINT_VERSION,
        "generation": int(generation),
        "fingerprint": str(fingerprint),
        "prompt_digest": _digest_of(inputs) if inputs is not None else None,
        "proposal": proposal.as_dict(),
    }
    write_json_atomic(path, payload)
    events.emit("evolve.proposal_checkpoint", gen=int(generation), status="saved",
                n_candidates=len(proposal.candidates), path=str(path))
    return path


def load_proposal_checkpoint(run_root: Path | str, generation: int, *,
                             fingerprint: str) -> ProposalResult | None:
    """读回检查点；**指纹不一致 / 版本不符 / 损坏 → ``None``**（调用方照常重新提案）。"""
    path = proposal_checkpoint_path(run_root, generation)
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except InfraError:  # pragma: no cover - 损坏检查点按「没有」处理
        return None
    if not isinstance(data, Mapping):
        return None
    if int(data.get("version") or 0) != PROPOSAL_CHECKPOINT_VERSION:
        return None
    if str(data.get("fingerprint") or "") != str(fingerprint):
        return None
    raw = data.get("proposal") or {}
    try:
        candidates = tuple(Candidate.from_dict(c) for c in (raw.get("candidates") or ()))
    except Exception:  # noqa: BLE001 - 反序列化失败：不复用（fail-closed），照常重提案
        return None
    if not candidates:
        return None
    events.emit("evolve.proposal_reused", gen=int(generation),
                n_candidates=len(candidates), path=str(path),
                candidate_ids=[c.candidate_id for c in candidates])
    return ProposalResult(candidates=candidates,
                          error_code=raw.get("error_code"),
                          n_generated=int(raw.get("n_generated") or 0),
                          n_deduped=int(raw.get("n_deduped") or 0),
                          prompt_tokens=int(raw.get("prompt_tokens") or 0),
                          latency_ms=int(raw.get("latency_ms") or 0),
                          reason=str(raw.get("reason") or ""))
