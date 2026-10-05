"""MOD-08 子命令实现（G6 / Spec §4.6）::

    python -m gamer_rsi baseline --kind random|heuristic|fixed_prompt --split heldout --basket H1
    python -m gamer_rsi eval report --split heldout --baseline all --run-id <run_id>
    python -m gamer_rsi eval diversity --run-id <run_id> --generation 3
    python -m gamer_rsi eval ablation --run-id <run_id> --generation 3 --arms no_reflection,no_self_edit
    python -m gamer_rsi eval test --run-id <run_id> --unlock-once --attestation runs/<run_id>/generations/gen-003/test_attestation.json

命令面（参数与退出码）由 MOD-09 ``infra.cli`` 冻结，实现经组合根
``gamer_rsi/app.py::wire()`` 注册（NFR-09-5）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gamer_rsi.infra import cli as infra_cli
from gamer_rsi.infra.config import load_config
from gamer_rsi.infra.errors import E, InfraError, error_code_of
from gamer_rsi.infra.io import read_json, write_json_atomic

from . import seeds as seeds_mod
from .ablation import ablation_path, run_ablation
from .baselines import BASELINE_KINDS, baseline_arm, baselines_dir, run_baseline
from .diversity import (CollapseConfig, collapse_check, distinct_sequence_ratio,
                        js_divergence, kind_distribution, slot_distribution)
from .harness import (EvalSpec, append_generations_csv, evaluate_arm, generation_report_path,
                      load_episode_results, run_eval, utc_now, write_ext_metrics,
                      write_generation_report, write_plots_data)
from .metrics import (aggregate, bootstrap_seed, judge_gain, mcnemar_exact_p, power_note,
                      render_report_sections)

__all__ = ["HANDLERS", "cmd_baseline", "cmd_eval_report", "cmd_eval_diversity",
           "cmd_eval_ablation", "cmd_eval_test", "set_inference_client_provider"]


#: MOD-03 推理客户端提供者（组合根 ``gamer_rsi/app.py::wire()`` 注入）。
#:
#: MOD-08 属测量层（L3），**不自己构造** MOD-03 的 ``BackendManager``——装配发生在组合根
#: （与 ``cli.register_content_hash_provider`` 同一模式）。签名::
#:
#:     provider(cfg, *, run_id: str, runs_root: Path) -> InferenceClient
#:
#: ``fixed_prompt`` 基线必须拿到客户端才允许启动（Spec §8.5：不得静默降级成启发式）。
_INFERENCE_CLIENT_PROVIDER: Callable[..., Any] | None = None


def set_inference_client_provider(provider: Callable[..., Any] | None) -> None:
    """登记 MOD-03 客户端提供者（幂等；``None`` 表示未接线）。"""
    global _INFERENCE_CLIENT_PROVIDER
    _INFERENCE_CLIENT_PROVIDER = provider


def build_inference_client(cfg: Any, *, run_id: str, runs_root: Path) -> Any:
    """按已登记的提供者建客户端；未接线时返回 ``None``（交由 ``run_baseline`` fail-closed）。"""
    if _INFERENCE_CLIENT_PROVIDER is None:
        return None
    return _INFERENCE_CLIENT_PROVIDER(cfg, run_id=run_id, runs_root=Path(runs_root))



def _paths(args: argparse.Namespace, *, require_state: bool = True) -> tuple[Any, Any, str]:
    """建/定位 run（复用 MOD-09 的启动序列：load_config → run_id → events.bind）。

    ``require_state=False``：``baseline --resume`` 用——基线 run 从不写 ``state.json``
    （那是 MOD-06 的语义），因此续跑只要求 run 目录 + ``episodes/`` 已存在的局（F18）。
    """
    return infra_cli.cli_init_run(args, require_state=require_state)


def _default_basket_for(split: str) -> str:
    """ ``--basket`` 未给定时按 split 推导默认篮子（避免 train split 套 heldout 篮子）。"""
    return {"heldout": "H1", "train": "train_window", "test": "test_full"}.get(split, "H1")


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _latest_generation(run_dir: Path) -> int:
    """run 里已有的最大代数（``generations/gen-*`` 或 ``metrics/gen-*.ext.json``）。"""
    gens: list[int] = []
    root = run_dir / "generations"
    if root.is_dir():
        for path in root.glob("gen-*"):
            try:
                gens.append(int(path.name.split("-")[1]))
            except (IndexError, ValueError):
                continue
    metrics = run_dir / "metrics"
    if metrics.is_dir():
        for path in metrics.glob("gen-*.ext.json"):
            try:
                gens.append(int(path.name.split("-")[1].split(".")[0]))
            except (IndexError, ValueError):
                continue
    return max(gens) if gens else 0


def _protocol(cfg: Any, config_dir: str) -> seeds_mod.SeedProtocol:
    return seeds_mod.load_protocol(Path(config_dir))


def _baseline_summary(run_dir: Path, run_id: str) -> dict[str, Any] | None:
    """读取已归档基线（``--baseline all``）；缺失 → ``None``（PRD §10 降级）。"""
    root = baselines_dir(run_id, run_dir)
    if not root.is_dir():
        return None
    out: dict[str, Any] = {}
    for path in sorted(root.glob("*.json")):
        try:
            data = read_json(path)
        except InfraError:
            continue
        out[f"{data.get('kind')}-{data.get('split')}"] = data.get("metrics")
    return out or None


# =====================================================================
# baseline
# =====================================================================

def cmd_baseline(args: argparse.Namespace) -> int:
    """跑一个基线并归档（FR-08-11/12；G6 第一条命令）。

    ``fixed_prompt`` 需要真实 MOD-03 客户端：先经组合根登记的提供者装配，拿不到就
    fail-closed（``E_LLM_UNAVAILABLE`` / 退出码 3），绝不静默降级成启发式（M1 验收 F1）。
    ``fixed_prompt`` 走 MOD-04 ``PolicyAgent(mode="baseline_fixed_prompt")``（F22）：
    每**决策点**一次推理。

    ``--resume``（F18）：跳过 ``episodes/<split>/`` 里已完成的 seed，只跑缺的；
    归档时把 ``resumed_n`` 一并入档。基线的续跑**不要求** ``state.json``（没有它时
    先找 ``episodes/``；一个已完成的 run 上重复 ``--resume`` 不会重复归档）。
    """
    resume = bool(getattr(args, "resume", False))
    paths, cfg, run_id = _paths(args, require_state=not resume)
    kind = str(getattr(args, "kind", "heuristic"))
    if kind not in BASELINE_KINDS:
        raise InfraError(E.E_EVAL_ARM_NOT_ALLOWED, f"未知基线 kind={kind!r}",
                         hint=f"合法值：{list(BASELINE_KINDS)}")
    split = str(getattr(args, "split", "heldout") or "heldout")
    basket_arg = getattr(args, "basket", None)
    seeds_arg = getattr(args, "seeds", None)
    if seeds_arg:
        # ``--seeds`` 与 ``--basket`` 互斥（infra.cli 已用 mutually exclusive group 守住）；
        # 显式 seed 规格是权威输入，不再套 basket 默认值（否则用户写的 split 会被静默覆盖）。
        parsed = infra_cli.parse_seeds_spec(str(seeds_arg))
        basket: Any = tuple(sorted({s for values in parsed.values() for s in values}))
    else:
        basket = str(basket_arg or _default_basket_for(split))
    proto = _protocol(cfg, args.config_dir)
    client = (build_inference_client(cfg, run_id=run_id, runs_root=paths.root.parent)
              if kind == "fixed_prompt" else None)
    model_id = str(cfg.inference.model_id)
    arm = run_baseline(kind, split=split, basket=basket, run_id=run_id,  # type: ignore[arg-type]
                       run_dir=paths.root, config=cfg, protocol=proto, client=client,
                       model_hash=model_id,
                       git_commit=str(getattr(args, "git_commit", "") or ""),
                       n_workers=int(getattr(cfg.evals, "n_workers", 1)),
                       resume=resume,
                       model_backend=_registry_backend(cfg, model_id),
                       seed_timeout_s=int(getattr(cfg.evals, "seed_timeout_s", 1800)))
    payload = {"kind": kind, "split": split, "basket": basket, "run_id": run_id,
               "archive_dir": str(baselines_dir(run_id, paths.root)),
               "archive_path": arm.archive_path or None,
               "metrics": arm.metrics.as_dict(), "dur_s": arm.dur_s,
               "error_codes": list(arm.error_codes), "power_note": power_note(arm.metrics.n),
               "inference_client": bool(client), "model_id": model_id,
               "model_hash": arm.model_hash, "model_backend": arm.model_backend,
               "seeds": list(arm.seeds), "resumed_n": arm.resumed_n,
               "pending_seeds": list(arm.pending_seeds),
               "seed_timeout_s": arm.seed_timeout_s}
    _print(payload)
    return 0


def _registry_backend(cfg: Any, model_id: str) -> str:
    """注册表里该模型的真实后端名（F6：归档要记「哪个后端产出的」）。"""
    try:
        from gamer_rsi.inference.registry import load_registry

        return str(load_registry(str(cfg.inference.registry_path)).spec(model_id).backend)
    except Exception: # 注册表不可用不该阻断测量；如实记 unknown
        return "unknown"


#: ``eval report`` / ``eval diversity`` 必须排除的臂（F5：基线局不得混进代际分母）
_BASELINE_ARMS: tuple[str, ...] = tuple(baseline_arm(k) for k in BASELINE_KINDS)


# =====================================================================
# eval report
# =====================================================================

def cmd_eval_report(args: argparse.Namespace) -> int:
    """从 run 的轨迹重算指标段并落 ``report.json`` + ``report.sha256``（FR-08-17/22/23）。"""
    paths, cfg, run_id = _paths(args)
    split = str(getattr(args, "split", "heldout") or "heldout")
    generation = _generation_of(args, paths.root)
    proto = _protocol(cfg, args.config_dir)
    eval_cfg = cfg.evals
    results = load_episode_results(paths.root, split, generation=generation,  # type: ignore[arg-type]
                                   harness_version=generation,
                                   exclude_arms=_BASELINE_ARMS)
    heldout = aggregate(results, split=split, basket=_basket_of(args),  # type: ignore[arg-type]
                        t_max=int(eval_cfg.t_max), min_support=int(eval_cfg.min_support),
                        drop_rate_max=float(eval_cfg.drop_rate_max))
    window = seeds_mod.train_window(generation, protocol=proto, window=60)
    train_results = load_episode_results(paths.root, "train", generation=generation,
                                         harness_version=generation, seeds=window,
                                         exclude_arms=_BASELINE_ARMS)
    train = aggregate(train_results, split="train", basket="train_window",
                      t_max=int(eval_cfg.t_max), min_support=int(eval_cfg.min_support),
                      drop_rate_max=float(eval_cfg.drop_rate_max))
    diversity = _diversity_from_history(paths.root, generation, cfg, heldout, results)
    gain = judge_gain(train=train, heldout=heldout,
                      gap_threshold=float(eval_cfg.gap_threshold),
                      alpha=float(eval_cfg.alpha), collapse_flag=diversity.collapse_flag,
                      low_confidence=heldout.low_confidence)
    baseline = None
    requested = str(getattr(args, "baseline", "none") or "none")
    if requested != "none":
        baseline = _baseline_summary(paths.root, run_id)
        if baseline is None: # PRD §10：baselines 缺失 → 标注，不中断
            print(f"[{E.E_EVAL_BASELINE_MISSING}] 基线归档缺失：{baselines_dir(run_id, paths.root)}",
                  file=sys.stderr)
    report = render_report_sections(
        generation=generation, harness_version=generation, parent_version=max(0, generation - 1),
        heldout=heldout, train=train, gain=gain, diversity=diversity, ablation=None,
        decision="baseline" if generation == 0 else "pending", decided_by="auto",
        reason="eval report（从轨迹重算）", extra={"power_note": power_note(heldout.n)})
    path, sha = write_generation_report(paths.root, generation, report)
    _write_plots(paths.root, generation, heldout, diversity)
    ext = _ext_payload(generation, heldout, gain, diversity, eval_cfg, baselines=baseline,
                       provenance=_provenance(cfg, run_id, generation, heldout.basket, proto))
    write_ext_metrics(paths.root, generation, ext)
    append_generations_csv(paths.root, _csv_row(generation, heldout, diversity, gain,
                                                report, cfg, eval_cfg))
    print(f"report: {path}")
    print(f"sha256: {sha}")
    _print(report)
    return 0


def _generation_of(args: argparse.Namespace, run_dir: Path) -> int:
    value = getattr(args, "generation", None)
    return int(value) if value is not None else _latest_generation(run_dir)


def _basket_of(args: argparse.Namespace) -> str:
    split = str(getattr(args, "split", "heldout") or "heldout")
    if split == "test":
        return "test_full"
    if split == "train":
        return "train_window"
    return str(getattr(args, "basket", "H1") or "H1")


def _diversity_from_history(run_dir: Path, generation: int, cfg: Any, heldout: Any,
                            results: Sequence[Any] | None = None) -> Any:
    """按 ``metrics/plots_data`` 历史 + 当前曲线做坍缩判定（§8.3）。"""
    collapse_cfg = CollapseConfig.from_eval_config(cfg.evals)
    curves: list[list[float | None]] = []
    win_rates: list[float] = []
    plots = run_dir / "metrics" / "plots_data"
    if plots.is_dir():
        for path in sorted(plots.glob("gen-*_entropy.json")):
            try:
                data = read_json(path)
            except InfraError:
                continue
            curves.append([b.get("h_norm") for b in (data.get("buckets") or [])])
            win_rates.append(float(data.get("win_rate") or 0.0))
    current = list(heldout.action_entropy_curve)
    if curves and len(curves[-1]) == len(current):
        curves[-1] = current
        win_rates[-1] = heldout.win_rate
    else:
        curves.append(current)
        win_rates.append(heldout.win_rate)
    jsds: list[float | None] = [None] * max(0, len(curves) - 1)
    seq_ratios = [distinct_sequence_ratio(results)] if results else None
    return collapse_check(curves=curves, jsd_kind=jsds, win_rates=win_rates,
                          cfg=collapse_cfg, seq_ratios=seq_ratios, generation=generation)


def _write_plots(run_dir: Path, generation: int, heldout: Any, diversity: Any) -> None:
    write_plots_data(run_dir, generation, heldout.action_entropy_curve,
                     basket=heldout.basket, win_rate=heldout.win_rate, n=heldout.n)


def _ext_payload(generation: int, heldout: Any, gain: Any, diversity: Any,
                 eval_cfg: Any, *, baselines: Any = None,
                 provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """扩展量（不进 report.json；FR-08-17/P-19 / NFR-08-03）。"""
    payload = {
        "generation": generation,
        "jsd_kind": diversity.jsd_kind,
        "jsd_slot": diversity.jsd_slot,
        "collapse_reasons": list(diversity.collapse_reasons),
        "distinct_sequence_ratio": diversity.distinct_sequence_ratio,
        "power_note": power_note(heldout.n),
        "mcnemar_p": gain.mcnemar_p,
        "low_confidence": heldout.low_confidence,
        "bootstrap_B": int(getattr(eval_cfg, "bootstrap_B", 10000)),
        "baselines": baselines,
        "debug": {"note": "shaped reward 仅调试，禁止进入 verdict（INV-7）"},
    }
    payload.update(dict(provenance or {}))
    return payload


def _provenance(cfg: Any, run_id: str, generation: int, basket: str,
                proto: seeds_mod.SeedProtocol) -> dict[str, Any]:
    """NFR-08-03：``config_hash`` / ``seeds_hash`` / ``bootstrap_seed`` 全部入档。"""
    from gamer_rsi.infra.config import config_hash

    return {"config_hash": config_hash(cfg), "seeds_hash": proto.seeds_hash,
            "protocol_hash": seeds_mod.protocol_hash(proto),
            "bootstrap_seed": bootstrap_seed(run_id, generation, basket)}


def _csv_row(generation: int, heldout: Any, diversity: Any, gain: Any, report: Any,
             cfg: Any, eval_cfg: Any) -> dict[str, Any]:
    from gamer_rsi.infra.config import config_hash
    from gamer_rsi.infra.run import git_short_sha

    ci = list(heldout.win_rate_ci95 or (0.0, 0.0))
    return {
        "generation": generation, "harness_version": report.get("harness_version"),
        "parent_version": report.get("parent_version"), "split": heldout.split,
        "basket": heldout.basket, "n": heldout.n, "win_rate": heldout.win_rate,
        "ci_lo": ci[0], "ci_hi": ci[1], "mean_stages_cleared": heldout.mean_stages_cleared,
        "mean_hearts_left": heldout.mean_hearts_left,
        "mean_battles_won": heldout.mean_battles_won,
        "mean_decisions": heldout.mean_decisions, "illegal_rate": heldout.illegal_rate,
        "fallback_rate": heldout.fallback_rate, "truncation_rate": heldout.truncation_rate,
        "entropy_mean_norm": heldout.entropy_mean_norm,
        "entropy_slope": float(diversity.entropy_slope),
        "jsd_kind": diversity.jsd_kind, "collapse_flag": diversity.collapse_flag,
        "win_rate_gap": gain.win_rate_gap, "verdict": gain.verdict,
        "decision": report.get("decision"), "bootstrap_seed": None,
        "config_hash": config_hash(cfg), "git_commit": git_short_sha(), "ts": utc_now(),
    }


# =====================================================================
# eval diversity
# =====================================================================

def cmd_eval_diversity(args: argparse.Namespace) -> int:
    """动作熵 / 坍缩监控（G6；写 ext 与 plots_data）。"""
    paths, cfg, run_id = _paths(args)
    generation = _generation_of(args, paths.root)
    results = load_episode_results(paths.root, "heldout", generation=generation,
                                   harness_version=generation,
                                   exclude_arms=_BASELINE_ARMS)
    metrics = aggregate(results, split="heldout", basket="H1",
                        t_max=int(cfg.evals.t_max), min_support=int(cfg.evals.min_support))
    diversity = _diversity_from_history(paths.root, generation, cfg, metrics)
    kind_dist = kind_distribution(results)
    slot_dist = slot_distribution(results)
    seq_ratio = distinct_sequence_ratio(results)
    jsd_slot = None
    if slot_dist:
        jsd_slot = 0.0 # 单代无对照：写 0.0 而不是 None，便于绘图
    write_plots_data(paths.root, generation, metrics.action_entropy_curve, basket="H1",
                     win_rate=metrics.win_rate, n=metrics.n)
    ext = _ext_payload(generation, metrics, _null_gain(), diversity, cfg.evals,
                       provenance=_provenance(cfg, run_id, generation, "H1",
                                              _protocol(cfg, args.config_dir)))
    ext.update({"kind_distribution": kind_dist, "slot_distribution": slot_dist,
                "jsd_slot": jsd_slot,
                "distinct_sequence_ratio": seq_ratio})
    write_ext_metrics(paths.root, generation, ext)
    _print({"run_id": run_id, "generation": generation,
            "entropy_slope": diversity.entropy_slope,
            "collapse_flag": diversity.collapse_flag,
            "collapse_reasons": list(diversity.collapse_reasons),
            "distinct_sequence_ratio": seq_ratio, "n": metrics.n})
    return 0


class _NullGain:
    win_rate_gap = 0.0
    verdict = "null"
    mcnemar_p = None
    gap_threshold = 0.15
    collapse_veto = False
    bootstrap_gap_ci95 = (0.0, 0.0)


def _null_gain() -> Any:
    return _NullGain()


# =====================================================================
# eval ablation
# =====================================================================

def cmd_eval_ablation(args: argparse.Namespace) -> int:
    """H2 上跑 3 臂消融并做配对检验（FR-08-21；G6）。"""
    paths, cfg, run_id = _paths(args)
    generation = _generation_of(args, paths.root)
    arms_arg = str(getattr(args, "arms", "") or "")
    arms = [a.strip() for a in arms_arg.split(",") if a.strip()] or ["full"]
    if "full" not in arms:
        arms = ["full"] + arms
    proto = _protocol(cfg, args.config_dir)
    basket = str(getattr(args, "basket", "H2") or "H2")
    report = run_ablation(generation, arms=arms, run_id=run_id,  # type: ignore[arg-type]
                          split_basket=basket, alpha=float(cfg.evals.alpha),
                          config=cfg, protocol=proto, run_dir=paths.root,
                          harness_dir=Path("harness"))
    print(f"ablation: {ablation_path(paths.root, generation)}")
    _print(report.as_dict())
    return 0


# =====================================================================
# eval test
# =====================================================================

def cmd_eval_test(args: argparse.Namespace) -> int:
    """test 终检（FR-08-04；默认锁定，``--unlock-once`` 解锁并存证）。"""
    paths, cfg, run_id = _paths(args)
    generation = _generation_of(args, paths.root)
    proto = _protocol(cfg, args.config_dir)
    if not bool(getattr(args, "unlock_once", False)):
        raise InfraError(E.E_EVAL_TEST_LOCKED,
                         "test 池默认锁定：需显式 --unlock-once（每里程碑 ≤1 次）",
                         hint="python -m gamer_rsi eval test --run-id <id> --unlock-once "
                              "--attestation runs/<id>/generations/gen-003/test_attestation.json")
    unlocked = seeds_mod.SeedProtocol(
        version=proto.version, hash_algo=proto.hash_algo, splits=proto.splits,
        exposure_guard=proto.exposure_guard, seeds_hash=proto.seeds_hash, unlocked=True)
    seeds_mod.validate_protocol(unlocked)
    arm = evaluate_arm(Path("harness"), split="test", basket="test_full",
                       memory_version=0, arm="llm-test", run_id=run_id,
                       protocol=unlocked, generation=generation,
                       harness_version=generation, config=cfg)
    # test 同时跑两种局：默认局（主结论）+ 完整局 medium×10 关（次级「迁移」指标，Spec §8.6）。
    # 迁移指标**只报告不判定**；失败即降级为 null，不影响主结论。
    transfer: dict[str, Any] | None = None
    if bool(getattr(args, "transfer", True)):
        try:
            transfer_arm = evaluate_arm(
                Path("harness"), split="test", basket="test_full", memory_version=0,
                arm="llm-test-transfer", run_id=run_id, protocol=unlocked,
                generation=generation, harness_version=generation, config=cfg,
                episode_config={"difficulty": "medium", "stage_count": 10})
            transfer = {"episode": "medium×10", "n": transfer_arm.metrics.n,
                        "win_rate": transfer_arm.metrics.win_rate,
                        "win_rate_ci95": list(transfer_arm.metrics.win_rate_ci95),
                        "decision_path": False}
        except Exception as exc: # 迁移局失败不影响 test 主结论（PRD §10 降级）
            print(f"[E_EVAL_ARM_NOT_ALLOWED] 迁移局降级为 null：{exc}", file=sys.stderr)
    attestation = getattr(args, "attestation", None)
    path = seeds_mod.test_unlock(
        run_id=run_id, generation=generation, run_dir=paths.root,
        attestation_path=Path(attestation) if attestation else None,
        reason="test 终检（--unlock-once）", decided_by="human", protocol=unlocked,
        test_metrics={"n": arm.metrics.n, "win_rate": arm.metrics.win_rate,
                      "win_rate_ci95": list(arm.metrics.win_rate_ci95),
                      "transfer": transfer})
    write_ext_metrics(paths.root, generation,
                      _ext_payload(generation, arm.metrics, _null_gain(),
                                   _empty_diversity(), cfg.evals, baselines=None,
                                   provenance=_provenance(cfg, run_id, generation,
                                                          "test_full", unlocked))
                      | {"transfer": transfer, "attestation": str(path)})
    print(f"attestation: {path}")
    _print({"run_id": run_id, "generation": generation, "test": arm.metrics.as_dict(),
            "transfer": transfer, "power_note": power_note(arm.metrics.n)})
    return 0


def _empty_diversity() -> Any:
    from .metrics import CollapseVerdict

    return CollapseVerdict(entropy_slope=0.0, collapse_flag=False)


# ---- 注册（组合根 import 本模块的 HANDLERS）------------------------------------------

HANDLERS = {
    ("baseline",): cmd_baseline,
    ("eval", "report"): cmd_eval_report,
    ("eval", "diversity"): cmd_eval_diversity,
    ("eval", "ablation"): cmd_eval_ablation,
    ("eval", "test"): cmd_eval_test,
}
