"""MOD-03 子命令实现（G6）：``inference probe`` / ``bench`` / ``robustness``。

命令面（参数与退出码）由 MOD-09 ``infra.cli`` 冻结，实现经组合根
``dice_ratchet/app.py::wire()`` 注册（NFR-09-5）。三个命令都在一个 ``asyncio.run`` 内完成
加载 → 调用 → 卸载，避免跨事件循环复用锁与信号量。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..infra import cli as infra_cli
from ..infra.errors import error_code_of
from ..infra.io import write_json_atomic
from .backend import BackendManager, InfraBridge, SAMPLING_KEYS
from .errors import InferenceError
from .metrics import aggregate, robustness_report
from .prompts import load_robustness_prompts, probe_messages
from .records import CallContext, InferenceCallRecord
from .schema import decision_schema
from .registry import ModelSpec

__all__ = ["cmd_probe", "cmd_bench", "cmd_robustness"]


# =====================================================================
# 公共装配
# =====================================================================

def _init(args: argparse.Namespace) -> tuple[Any, Any, str]:
    """建 run 目录 + 绑定事件 + 加载配置（复用 MOD-09 的启动序列）。"""
    paths, cfg, run_id = infra_cli.cli_init_run(args)
    return paths, cfg, run_id


def _build_manager(inference: Any, *, run_id: str, runs_root: Path,
                   record_sink: Callable[[InferenceCallRecord], None]) -> BackendManager:
    bridge = InfraBridge(run_id=run_id, runs_root=runs_root,
                         debug_dump_raw=bool(inference.debug_dump_raw))
    return BackendManager(inference.registry_path, mode=inference.mode,
                          ram_budget_gb=inference.ram_budget_gb, infra=bridge,
                          config=inference, record_sink=record_sink,
                          runs_root=runs_root)


def _sampling(spec: ModelSpec, inference: Any, seed: int | None = None) -> dict[str, Any]:
    """采样参数：注册表默认值 + eval 归一 + ``--seed`` / ``inference.seed``（PRD §9）。"""
    sampling = {k: v for k, v in (spec.default_sampling or {}).items() if k in SAMPLING_KEYS}
    if inference.mode == "eval":
        sampling.update({"temperature": 0.0, "top_p": 1.0, "top_k": 0})
    if seed is None:
        seed = inference.seed
    if seed is not None:
        sampling["seed"] = int(seed)
    return sampling


def _ctx(run_id: str, *, purpose: str, step: int | None = None) -> CallContext:
    return CallContext(run_id=run_id, purpose=purpose, caller="mod-03", step=step,
                       schema_id="decision.v1")


def _print_report(report: dict[str, Any], path: Path | None) -> None:
    if path is not None:
        print(f"report: {path}")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(exc: InferenceError) -> int:
    print(f"[{exc.code}] {exc.message}", file=sys.stderr)
    if exc.detail:
        print(f"  detail: {json.dumps(exc.detail, ensure_ascii=False, default=str)}",
              file=sys.stderr)
    if exc.hint:
        print(f"  hint: {exc.hint}", file=sys.stderr)
    return error_code_of(exc.code)


def _override_output(args: argparse.Namespace, default: Path) -> Path:
    out = getattr(args, "out", None)
    return Path(out) if out else default


# =====================================================================
# inference probe
# =====================================================================

def cmd_probe(args: argparse.Namespace) -> int:
    """加载模型 → 发一次 ``generate_json`` → 打印 health/usage → 卸载（G6）。"""
    paths, cfg, run_id = _init(args)
    inference = cfg.inference
    collected: list[InferenceCallRecord] = []
    manager = _build_manager(inference, run_id=run_id, runs_root=paths.root.parent,
                             record_sink=collected.append)
    try:
        report = asyncio.run(_probe(manager, inference, run_id, getattr(args, "seed", None)))
    except InferenceError as exc:
        return _fail(exc)
    output = _override_output(args, paths.metrics / "inference_probe.json")
    write_json_atomic(output, report)
    _print_report(report, output)
    return 0


async def _probe(manager: BackendManager, inference: Any, run_id: str,
                 seed: int | None) -> dict[str, Any]:
    spec = manager.registry.spec(inference.model_id)
    try:
        backend = await manager.load(inference.model_id)
        before = await backend.health()
        started = time.monotonic()
        result = await manager.generate_json(
            inference.model_id, probe_messages(), decision_schema(),
            call_context=_ctx(run_id, purpose="probe"),
            **_sampling(spec, inference, seed))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        after = await manager.health()
        return {
            "kind": "probe",
            "run_id": run_id,
            "model_id": inference.model_id,
            "backend": spec.backend,
            "mode": inference.mode,
            "context_length": spec.context_length,
            "weights_path": spec.weights_path,
            "health_before": _health_dict(before),
            "health_after": _health_dict(after.get(inference.model_id, before)),
            "outcome_kind": result.outcome_kind,
            "attempts": result.attempts,
            "finish_reason": result.finish_reason,
            "retry_reasons": result.retry_reasons,
            "degraded_reason": result.degraded_reason,
            "prompt_digest": result.prompt_digest,
            "parsed_ok": result.parsed is not None,
            "usage": _usage_dict(result.usage),
            "wall_ms": elapsed_ms,
        }
    finally:
        await manager.unload_all()


def _health_dict(health: Any) -> dict[str, Any]:
    return {"state": health.state, "backend": health.backend, "model_id": health.model_id,
            "resident_gb": health.resident_gb, "inflight": health.inflight,
            "last_error_code": health.last_error_code}


def _usage_dict(usage: Any) -> dict[str, Any]:
    return {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
            "cache_hit_tokens": usage.cache_hit_tokens,
            "cache_miss_tokens": usage.cache_miss_tokens,
            "cache_hit_rate": usage.cache_hit_rate,
            "latency_ms": usage.latency_ms, "ttft_ms": usage.ttft_ms,
            "prefill_ms": usage.prefill_ms, "decode_ms": usage.decode_ms}


# =====================================================================
# inference bench
# =====================================================================

def cmd_bench(args: argparse.Namespace) -> int:
    """``--n`` 次固定 prompt 生成 + 延迟/吞吐聚合对照 ``perf_budget``（G6、NFR-03-02）。"""
    paths, cfg, run_id = _init(args)
    inference = cfg.inference
    collected: list[InferenceCallRecord] = []
    manager = _build_manager(inference, run_id=run_id, runs_root=paths.root.parent,
                             record_sink=collected.append)
    n = int(getattr(args, "n", None) or 50)
    try:
        report = asyncio.run(_bench(manager, inference, run_id, n, collected,
                                    getattr(args, "seed", None)))
    except InferenceError as exc:
        return _fail(exc)
    output = _override_output(args, paths.metrics / "inference_bench.json")
    write_json_atomic(output, report)
    _print_report(report, output)
    return 0


async def _bench(manager: BackendManager, inference: Any, run_id: str, n: int,
                 collected: list[InferenceCallRecord],
                 seed: int | None) -> dict[str, Any]:
    spec = manager.registry.spec(inference.model_id)
    sampling = _sampling(spec, inference, seed)
    messages = probe_messages()
    errors: list[dict[str, Any]] = []
    started = time.monotonic()
    resident_gb: float | None = None
    try:
        await manager.load(inference.model_id)
        resident_gb = (await manager.health())[inference.model_id].resident_gb
        for index in range(max(1, n)):
            try:
                await manager.generate_json(
                    inference.model_id, messages, decision_schema(),
                    call_context=_ctx(run_id, purpose="bench", step=index), **sampling)
            except InferenceError as exc:
                errors.append(exc.as_dict())
    finally:
        await manager.unload_all()
    wall_ms = int((time.monotonic() - started) * 1000)
    agg = aggregate(collected)
    budget = dict(spec.perf_budget or {})
    overall = agg["overall"]
    checks: dict[str, bool] = {}
    if "p50_latency_ms" in budget:
        checks["p50_latency_ms"] = overall["latency_ms"]["p50"] <= budget["p50_latency_ms"]
    if "p95_latency_ms" in budget:
        checks["p95_latency_ms"] = overall["latency_ms"]["p95"] <= budget["p95_latency_ms"]
    if "min_tok_per_s" in budget and overall["tok_per_s"] is not None:
        checks["min_tok_per_s"] = overall["tok_per_s"] >= budget["min_tok_per_s"]
    return {
        "kind": "bench", "run_id": run_id, "model_id": inference.model_id,
        "backend": spec.backend, "mode": inference.mode, "n": max(1, n),
        "n_ok": agg["overall"]["n_calls"], "n_failed": len(errors), "wall_ms": wall_ms,
        # NFR-03-03：实测常驻内存（回填 config.json#inference.resident_gb_measured 的证据）
        "resident_gb_measured": resident_gb, "resident_gb_est": spec.resident_gb_est,
        "ram_budget_gb": manager.ram_budget_gb,
        "aggregate": agg, "perf_budget": budget, "budget_checks": checks,
        "budget_ok": all(checks.values()) if checks else None, "errors": errors,
    }


# =====================================================================
# inference robustness（M1 交付物）
# =====================================================================

def cmd_robustness(args: argparse.Namespace) -> int:
    """``--n`` 条固定 prompt 跑 ``generate_json``，产出 ``format_robustness.json``。

    退出码：通过阈值 0；未达 M1 阈值（首答 ≥90%、全失败 ≤1%）返回 1（领域失败）。
    """
    paths, cfg, run_id = _init(args)
    inference = cfg.inference
    collected: list[InferenceCallRecord] = []
    manager = _build_manager(inference, run_id=run_id, runs_root=paths.root.parent,
                             record_sink=collected.append)
    n = int(getattr(args, "n", None) or 200)
    try:
        report = asyncio.run(_robustness(manager, inference, run_id, n,
                                         getattr(args, "prompts", None), collected,
                                         getattr(args, "seed", None)))
    except InferenceError as exc:
        return _fail(exc)
    output = _override_output(args, paths.metrics / "format_robustness.json")
    write_json_atomic(output, report)
    _print_report(report, output)
    return 0 if report["passed"] else 1


async def _robustness(manager: BackendManager, inference: Any, run_id: str, n: int,
                      prompts_path: str | None,
                      collected: list[InferenceCallRecord],
                      seed: int | None) -> dict[str, Any]:
    spec = manager.registry.spec(inference.model_id)
    prompts, source = load_robustness_prompts(n, prompts_path)
    sampling = _sampling(spec, inference, seed)
    errors: list[dict[str, Any]] = []
    try:
        await manager.load(inference.model_id)
        for index, messages in enumerate(prompts):
            try:
                await manager.generate_json(
                    inference.model_id, messages, decision_schema(),
                    call_context=_ctx(run_id, purpose="robustness", step=index), **sampling)
            except InferenceError as exc:
                errors.append({**exc.as_dict(), "step": index})
    finally:
        await manager.unload_all()
    return robustness_report(collected, model_id=inference.model_id, backend=spec.backend,
                             mode=inference.mode, n_prompts=len(prompts),
                             prompt_source=source, errors=errors)


# 供组合根注册（dice_ratchet/app.py::wire）
HANDLERS: dict[tuple[str, ...], Callable[[argparse.Namespace], int]] = {
    ("inference", "probe"): cmd_probe,
    ("inference", "bench"): cmd_bench,
    ("inference", "robustness"): cmd_robustness,
}
