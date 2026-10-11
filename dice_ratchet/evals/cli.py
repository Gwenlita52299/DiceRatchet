"""MOD-08 子命令实现（G6 / Spec §4.6）::

    python -m dice_ratchet baseline --kind random|heuristic|fixed_prompt --split heldout --basket H1
    python -m dice_ratchet eval report --split heldout --baseline all --run-id <run_id>
    python -m dice_ratchet eval diversity --run-id <run_id> --generation 3
    python -m dice_ratchet eval ablation --run-id <run_id> --generation 3 --arms no_reflection,no_self_edit
    python -m dice_ratchet eval test --run-id <run_id> --unlock-once --attestation runs/<run_id>/generations/gen-003/test_attestation.json

命令面（参数与退出码）由 MOD-09 ``infra.cli`` 冻结，实现经组合根
``dice_ratchet/app.py::wire()`` 注册（NFR-09-5）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from dice_ratchet.infra import cli as infra_cli
from dice_ratchet.infra import events
from dice_ratchet.infra.config import load_config
from dice_ratchet.infra.errors import (EXIT_DOMAIN, E, InfraError,
                                      error_code_of)
from dice_ratchet.infra.io import read_json, write_json_atomic

from . import seeds as seeds_mod
from .ablation import ablation_path, run_ablation
from .baselines import BASELINE_KINDS, baseline_arm, baselines_dir, run_baseline
from .diversity import (CollapseConfig, collapse_check, distinct_sequence_ratio,
                        js_divergence, kind_distribution, slot_distribution)
from .harness import (VERDICT_DECISIONS, EvalSpec, append_generations_csv, evaluate_arm,
                      generation_report_path, generations_rows_from_report,
                      load_episode_results, run_eval, utc_now,
                      write_ext_metrics, write_generation_report, write_plots_data)
from .metrics import (aggregate, bootstrap_seed, judge_gain, mcnemar_exact_p, power_note,
                      render_report_sections)

__all__ = ["HANDLERS", "cmd_baseline", "cmd_eval_report", "cmd_eval_diversity",
           "cmd_eval_ablation", "cmd_eval_acceptance", "cmd_eval_test",
           "cmd_rehearsal_check",
           "set_inference_client_provider"]


#: MOD-03 推理客户端提供者（组合根 ``dice_ratchet/app.py::wire()`` 注入）。
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
    # Issue #1：固定 prompt 基线用的就是 **policy** 角色的模型（不再是"全系统唯一的
    # inference.model_id"）。归档必须记真实绑定的 id，否则换臂后 model_hash 会对不上。
    model_id = policy_model_id(cfg)
    arm = run_baseline(kind, split=split, basket=basket, run_id=run_id,  # type: ignore[arg-type]
                       run_dir=paths.root, config=cfg, protocol=proto, client=client,
                       model_hash=model_id,
                       git_commit=str(getattr(args, "git_commit", "") or ""),
                       n_workers=int(getattr(cfg.evals, "n_workers", 1)),
                       resume=resume,
                       model_backend=_registry_backend(cfg, model_id),
                       seed_timeout_s=int(getattr(cfg.evals, "seed_timeout_s", 14400)))
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
        from dice_ratchet.inference.registry import load_registry

        return str(load_registry(str(cfg.inference.registry_path)).spec(model_id).backend)
    except Exception: # 注册表不可用不该阻断测量；如实记 unknown
        return "unknown"


def policy_model_id(cfg: Any) -> str:
    """policy 角色最终生效的 model_id（Issue #1：基线归档与埋点的权威来源）。"""
    try:
        from dice_ratchet.inference.roles import role_model_ids

        return str(role_model_ids(cfg).get("policy") or cfg.inference.model_id)
    except Exception: # 配置形状异常时退回旧口径，不阻断测量
        return str(getattr(cfg.inference, "model_id", "") or "")


#: ``eval report`` / ``eval diversity`` 必须排除的臂（F5：基线局不得混进代际分母）
_BASELINE_ARMS: tuple[str, ...] = tuple(baseline_arm(k) for k in BASELINE_KINDS)


# =====================================================================
# eval report
# =====================================================================

def cmd_eval_report(args: argparse.Namespace) -> int:
    """从 run 的轨迹重算指标段并落 ``report.json`` + ``report.sha256``（FR-08-17/22/23）。

    **臂是显式的**（F46-A）。修复前这里 ``exclude_arms=_BASELINE_ARMS`` 之后把该代所有
    非基线臂（``parent`` + 各候选）聚成**一个** ``heldout``：在真实 run
    ``runs/m2-s32b-smoke-c1-medium6`` 上 gen-1 得到 ``n=77``（parent 40 + 候选 37），
    而 MOD-07 的权威报告是 ``heldout.n=40 / win_rate=0.625``（**parent 臂**）+
    ``candidates[0].heldout.n=37 / win_rate=0.6757``。两个口径都不等于 77。
    现在：

    - ``--arm <name>``：只看见该臂；
    - ``--arm all``：**显式**申报的混合池（并在 stdout 与 ext 里标注 ``mixed_pool=true``）；
    - 缺省：该代 ``report.json`` 里**胜出的候选臂**（``winner``，其 ``decision=accept``）
      或唯一候选；该代没有候选/候选全被拒时回落到 ``parent``。
    """
    paths, cfg, run_id = _paths(args)
    split = str(getattr(args, "split", "heldout") or "heldout")
    generation = _generation_of(args, paths.root)
    proto = _protocol(cfg, args.config_dir)
    eval_cfg = cfg.evals
    pool_results = load_episode_results(paths.root, split, generation=generation,  # type: ignore[arg-type]
                                        exclude_arms=_BASELINE_ARMS)
    requested_arm = getattr(args, "arm", None)
    arm, mixed = _resolve_report_arm(paths.root, generation, pool_results,
                                     requested=requested_arm)
    results = pool_results if mixed else [r for r in pool_results if r.arm == arm]
    if not results:
        print(f"[{E.E_EVAL_ARM_NOT_ALLOWED}] 第 {generation} 代在 {split} 上没有臂 "
              f"{arm!r} 的局（已落盘的臂：{_arms_of(pool_results)}）", file=sys.stderr)
    heldout = aggregate(results, split=split, basket=_basket_of(args),  # type: ignore[arg-type]
                        t_max=int(eval_cfg.t_max), min_support=int(eval_cfg.min_support),
                        drop_rate_max=float(eval_cfg.drop_rate_max))
    # 坍缩判定必须看**该代整池**（`generations_row_from_report` 与 MOD-07 同口径），
    # 只看单一臂会让「代际」熵曲线退化成单臂曲线。
    pool_metrics = aggregate(pool_results, split=split, basket=_basket_of(args),  # type: ignore[arg-type]
                             t_max=int(eval_cfg.t_max), min_support=int(eval_cfg.min_support),
                             drop_rate_max=float(eval_cfg.drop_rate_max))
    window = seeds_mod.train_window(generation, protocol=proto, window=60)
    train_results = load_episode_results(paths.root, "train", generation=generation,
                                         harness_version=generation, seeds=window,
                                         exclude_arms=_BASELINE_ARMS)
    train = aggregate(train_results, split="train", basket="train_window",
                      t_max=int(eval_cfg.t_max), min_support=int(eval_cfg.min_support),
                      drop_rate_max=float(eval_cfg.drop_rate_max))
    diversity = _diversity_from_history(paths.root, generation, cfg, pool_metrics, pool_results)
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
    path, sha = _write_report_preserving_verdict(
        paths.root, generation, report, existing_decided=_verdict_of(paths.root, generation))
    _write_plots(paths.root, generation, heldout, diversity)
    ext = _ext_payload(generation, heldout, gain, diversity, eval_cfg, baselines=baseline,
                       provenance=_provenance(cfg, run_id, generation, heldout.basket, proto))
    # F46-A：重算口径必须自解释（哪个臂 / 是不是混合池），否则「可复算」再次不可核验。
    # 不能塞进 report.json：`generation_report.schema.json` 是 `additionalProperties:false`。
    ext.update({"recomputed_arm": arm, "mixed_pool": bool(mixed),
                "pool_n": pool_metrics.n, "arms_present": _arms_of(pool_results),
                "requested_arm": (str(requested_arm) if requested_arm else None)})
    # F-M3-9：判据 1 的结论随报告一起可读。**只在台账里有两代可比时才算**——
    # 每代都算会退化；且单代 run（如 gen-000 基线）本来就没有「单调性」可言。
    # 配对 bootstrap 的开销按「可比较的代数」增长，不随本命令重复调用而放大。
    _augment_ext_with_acceptance(ext, paths.root, run_id, cfg)
    write_ext_metrics(paths.root, generation, ext)
    # F-M3-3（2026-10-11）：台账写**全部** split 行（heldout + train），且**用与 MOD-07 同一个
    # 构造器**（`generations_rows_from_report`）——原先这里用本地 `_csv_row` 只写 heldout 行，
    # 于是 train 一侧在台账里永远缺席（实测 8 个归档 run 的 train 行数全为 0），
    # `train_heldout_gap.png` 与 `win_rate.png` 的 train 对照曲线只能画成空白。
    for row in generations_rows_from_report(report, cfg=cfg):
        append_generations_csv(paths.root, row)
    # F-M3-3：**绘图的生产调用点**。原先 `plot_generations` 只被它自己的单测调用
    # ⇒ `runs/*/plots/` 全空、M3 的「曲线图」交付物不会产出。绘图是附属产物：
    # 失败**不阻塞**报告/台账落盘，但必须**非静默**（事件 + stderr）。
    _plot_or_warn(paths.root, generation)
    if mixed:
        print(f"[{E.E_EVAL_ARM_NOT_ALLOWED}] --arm all：heldout 是**混合池**"
              f"（n={heldout.n}，臂={_arms_of(pool_results)}），不是单臂口径，禁止当默认口径用",
              file=sys.stderr)
    print(f"report: {path}")
    print(f"sha256: {sha}")
    print(f"arm: {arm}{'（混合池，--arm all）' if mixed else ''} n={heldout.n}")
    _print(report)
    return 0


def _arms_of(results: Sequence[Any]) -> list[str]:
    """结果集合里出现过的臂（升序去重）。"""
    return sorted({str(getattr(r, "arm", "") or "") for r in results})


def _resolve_report_arm(run_root: Path, generation: int, pool: Sequence[Any], *,
                        requested: Any) -> tuple[str, bool]:
    """``eval report`` 该按哪个臂重算（F46-A）。

    返回 ``(arm, mixed_pool)``。``requested`` 的取值：``None``/``""`` → 自动（该代
    ``report.json`` 的胜出候选，无则唯一候选，再退 ``parent``）；``"all"`` → 混合池；
    其它字符串 → 原样使用。
    """
    text = str(requested or "").strip()
    if text.lower() == "all":
        return "all", True
    if text:
        return text, False
    winner = _winner_arm_of(run_root, generation)
    present = set(_arms_of(pool))
    if winner and winner in present:
        return winner, False
    if winner: # 胜出臂的局不在这个 split/run 里（例如只跑了 parent）：如实告知调用方
        print(f"[{E.E_EVAL_ARM_NOT_ALLOWED}] 第 {generation} 代的胜出臂 {winner!r} 在已落盘的"
              f"局里没有出现（已落盘：{sorted(present)}）；回落到 parent", file=sys.stderr)
    return "parent", False


def _winner_arm_of(run_root: Path | str, generation: int) -> str | None:
    """从 ``generations/gen-NNN/report.json`` 推出该代的候选臂名（F46-A）。

    ``candidate_id`` → ``cand-<candidate_id>``（与 ``CandidateEvaluator.candidate_arms``
    的命名一致）。优先取 ``decision="accept"`` 的候选；没有 accept 时取唯一候选
    （``decision="reject"`` 且 ``reason != "superseded"``）——它仍是该代被评估的候选臂。
    读不到（无报告/损坏/形状异常）→ ``None``（调用方回落到 ``parent``）。
    """
    path = generation_report_path(run_root, generation)
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except InfraError: # pragma: no cover - 损坏报告按「推不出」处理
        return None
    candidates = [c for c in (data.get("candidates") or []) if isinstance(c, Mapping)]
    if not candidates:
        return None
    accepted = [c for c in candidates if str(c.get("decision") or "") == "accept"]
    chosen = accepted[0] if accepted else candidates[0]
    candidate_id = str(chosen.get("candidate_id") or "")
    return f"cand-{candidate_id}" if candidate_id else None


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
    """NFR-08-03：``config_hash`` / ``seeds_hash`` / ``bootstrap_seed`` 全部入档。

    F45-2：额外记 ``seeds_protocol_id``（协议版本）与 ``seeds_protocol_tag``（与局行里的
    ``protocol`` 同一口径）。``config_hash`` **不含** seeds（``SeedsProtocol`` 是独立文件），
    所以没有这两个字段就无法回答「这条数字是哪套 seed 区间产出的」。
    """
    from dice_ratchet.infra.config import config_hash

    return {"config_hash": config_hash(cfg), "seeds_hash": proto.seeds_hash,
            "seeds_protocol_id": str(getattr(proto, "version", "") or ""),
            "seeds_protocol_tag": _seeds_protocol_tag(proto),
            "protocol_hash": seeds_mod.protocol_hash(proto),
            "bootstrap_seed": bootstrap_seed(run_id, generation, basket)}


def _seeds_protocol_tag(proto: Any) -> str:
    """``seeds-<hash 前 12 位>``（与 ``baselines._seeds_protocol_tag`` 同口径）。"""
    try:
        digest = getattr(proto, "seeds_hash", None) or seeds_mod.protocol_hash(proto)
    except Exception: # noqa: BLE001 - 协议不可读不阻断报告落盘
        return ""
    text = str(digest)
    if text.startswith("sha256:"):
        text = text[7:]
    return f"seeds-{text[:12]}"


def _verdict_of(run_root: Path | str, generation: int) -> str | None:
    """读已存在的 ``report.json`` 的 ``decision``（缺失/损坏 → ``None``）。"""
    path = generation_report_path(run_root, generation)
    if not path.is_file():
        return None
    try:
        return str(read_json(path).get("decision") or "") or None
    except InfraError: # pragma: no cover - 损坏报告按「未裁决」处理，不阻塞重算
        return None


def _write_report_preserving_verdict(run_root: Path | str, generation: int,
                                     report: Mapping[str, Any],
                                     *, existing_decided: str | None) -> tuple[Path, str]:
    """写重算报告；**已裁决的代不覆盖** MOD-07 的判决（F43）。

    ``eval report`` 的产物是「从轨迹**重算**」的指标段（``decision`` 恒为
    ``baseline``/``pending``），而 MOD-07 ``_finish_report`` 已经把该代的**真实判决**
    写在同一个 ``generations/gen-NNN/report.json``。原先两者**同一路径**，于是
    「跑 `eval report` 产出 `metrics/generations.csv`（判据②）」必然抹掉「谁采纳了哪个
    候选（判据①）」——用它自己的话说，满足②只能靠破坏①。
    现在：已裁决的代写 sidecar ``report.eval.json``（+ ``.sha256``）；未裁决的（含
    gen-000 基线）维持原路径，行为不变。
    """
    from dice_ratchet.infra.io import write_bytes_atomic, write_json_atomic

    if existing_decided in VERDICT_DECISIONS:
        side = generation_report_path(run_root, generation).with_name("report.eval.json")
        sha = write_json_atomic(side, dict(report))
        write_bytes_atomic(side.with_suffix(".sha256"), (sha + "\n").encode("utf-8"))
        print(f"[{E.E_IO_RUN_CONFLICT}] 该代已由 MOD-07 裁决（decision={existing_decided}）；"
              f"重算报告改写 {side.name}，不覆盖 report.json", file=sys.stderr)
        return side, sha
    return write_generation_report(run_root, generation, report)


def _augment_ext_with_acceptance(ext: dict[str, Any], run_root: Path, run_id: str,
                                 cfg: Any) -> None:
    """把判据 1 的结论放进 ext（F-M3-9）。**失败不阻断报告**，但发事件。"""
    from .acceptance import evaluate_acceptance

    try:
        report = evaluate_acceptance(
            run_root, run_id=run_id,
            min_heldout_n=int(getattr(cfg.evolve, "min_heldout_n", 20)),
            bootstrap_B=int(getattr(cfg.evals, "bootstrap_B", 10000)))
    except InfraError as exc:
        events.emit("eval.acceptance_skipped", code=str(exc.code),
                    detail=str(exc)[:200])
        return
    if len(report.comparable) < 1:
        # 只有一代（或一代可比都没有）：判据 1 无从判定，记下原因而不是塞一个假的 ok。
        ext["acceptance"] = {"ok": None, "monotonic_ok": None,
                             "reason": "可比较的代不足（至少需要两代 heldout）",
                             "accepts": report.accepts}
        return
    ext["acceptance"] = report.as_dict()


def _plot_or_warn(run_root: Path, generation: int) -> list[Path]:
    """F-M3-3 的绘图生产调用点：画 ``runs/<run_id>/plots/`` 下的 5 张 PNG。

    纪律（2026-10-11 裁决 A 的第 7 条）：**降级可以，静默不行**。绘图失败或没有数据点时
    发 ``infra.plot_skipped`` 事件 + 写 stderr，**但不让 ``eval report`` 失败**——
    报告与台账（判据的机读来源）必须先落盘。

    ``PLOT_NO_DATA`` 单独提示：它意味着台账里**没有该 split 的数据点**，此时生成的会是
    **空白图**（matplotlib 对空序列不报错）——正是 F-M3-3 修复前 `runs/*/plots/` 全空的
    真实成因，必须显式说出来，不能让「文件存在」冒充「有图」。
    """
    from dice_ratchet.infra.plotting import plot_generations

    csv_path = run_root / "metrics" / "generations.csv"
    out_dir = run_root / "plots"
    try:
        written = plot_generations(csv_path, out_dir)
    except InfraError as exc:
        events.emit("infra.plot_skipped", generation=int(generation),
                    code=str(exc.code), detail=str(exc)[:200])
        print(f"[{exc.code}] 绘图跳过：{exc.message}", file=sys.stderr)
        return []
    if not written:
        events.emit("infra.plot_skipped", generation=int(generation),
                    code=str(E.E_IO_PLOT_UNAVAILABLE), detail="matplotlib 不可用或无数数据点")
        print(f"[{E.E_IO_PLOT_UNAVAILABLE}] 绘图未产出任何文件（matplotlib 缺失或无数据点）",
              file=sys.stderr)
        return []
    events.emit("infra.plot_done", generation=int(generation),
                files=[p.name for p in written], out_dir=str(out_dir))
    return written


# =====================================================================
# eval diversity
# =====================================================================

def cmd_eval_diversity(args: argparse.Namespace) -> int:
    """动作熵 / 坍缩监控（G6；写 ext 与 plots_data）。

    F46-A：`generation` 现在是真过滤（同 ``load_episode_results`` 口径）。``--arm`` 缺省
    仍是**整池**（坍缩是代际现象，只看单一臂会退化成单臂曲线）；给出具体臂名时只看该臂。
    """
    paths, cfg, run_id = _paths(args)
    generation = _generation_of(args, paths.root)
    pool_results = load_episode_results(paths.root, "heldout", generation=generation,
                                        exclude_arms=_BASELINE_ARMS)
    requested_arm = str(getattr(args, "arm", "") or "").strip()
    results = ([r for r in pool_results if r.arm == requested_arm]
               if requested_arm and requested_arm.lower() != "all" else pool_results)
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

def cmd_eval_acceptance(args: argparse.Namespace) -> int:
    """``eval acceptance``：算 M3 判据 1（held-out 单调不降 + ≥1 次 accept）。

    **总是返回 0**：它是**判定器**不是守卫。把「验收不合格」编码成进程退出码，会让
    「命令失败」与「实验结论是否定」混淆——F47-D 刚把这两件事分开
    （`decision="error_reject"` vs `"reject"`）。结论写在 stdout 与
    `metrics/acceptance.json` 的 `ok` 上；需要 CI 门禁时由调用方读 `ok`。

    归属：F-M3-9（Spec v2.13 / B-69）。修复前 `monotonic_ok` 在代码里 **0 命中**
    （只在文档里出现 3 次）⇒ 判据 1 无法由代码产出结论。
    """
    from .acceptance import evaluate_acceptance, write_acceptance

    paths, cfg, run_id = _paths(args)
    B = getattr(args, "bootstrap_B", None) or int(getattr(cfg.evals, "bootstrap_B", 10000))
    report = evaluate_acceptance(
        paths.root, run_id=run_id,
        min_heldout_n=int(getattr(cfg.evolve, "min_heldout_n", 20)),
        bootstrap_B=int(B))
    path = write_acceptance(paths.root, report)
    payload = report.as_dict()
    payload["acceptance_path"] = str(path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not report.ok:
        print(f"判据 1 **不通过**：{'; '.join(report.reasons)}", file=sys.stderr)
    return 0


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
    """test 终检（FR-08-04；默认锁定，``--unlock-once`` 解锁并存证）。

    ``--check``：**只读预检**（D-M3-5 裁决 A）。它回答「真跑之前，存证这一步会不会失败」，
    不调用 ``evaluate_arm``、不写存证、**不消耗解锁**。

    **为什么必须有它（实测的顺序问题）**：``cmd_eval_test`` 先跑 100 局 test（≈4.2 h /
    ≈¥88）（指标要先算出来才能写进取证），**之后**才调 ``test_unlock``；而
    ``test_unlock`` 的唯一「单次」判据是 **attestation 文件是否已存在**。因此
    「attestation 路径不可写 / 父目录不存在 / 该代已解锁过」这几种失败一旦发生，
    **那 100 局已经花掉、却拿不到有效存证** —— 这是 test 池上唯一真正不可逆的失败模式，
    而它可以零成本提前查出来。
    """
    paths, cfg, run_id = _paths(args)
    generation = _generation_of(args, paths.root)
    proto = _protocol(cfg, args.config_dir)
    if bool(getattr(args, "check", False)):
        return _preflight_test(args, paths=paths, proto=proto, generation=generation,
                               run_id=run_id)
    if not bool(getattr(args, "unlock_once", False)):
        raise InfraError(E.E_EVAL_TEST_LOCKED,
                         "test 池默认锁定：需显式 --unlock-once（每里程碑 ≤1 次）",
                         hint="python -m dice_ratchet eval test --run-id <id> --unlock-once "
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


def cmd_rehearsal_check(args: argparse.Namespace) -> int:
    """``rehearsal check``：M3 彩排的机器可读合格判据（D-M3-5 裁决 A）。

    **只读、不付费**：全部判据都作用于已落盘的产物（`generations/*/report.json`、
    `metrics/generations.csv`、`plots/*.png`、`episodes/heldout/*.jsonl`）。
    唯一会「重算」的是把台账重渲染一遍好数数据点——画到临时目录，不覆盖交付物。

    **返回非 0 表示彩排不合格**（这是**守卫**语义：不合格就不该起跑正式实验）。
    """
    from .rehearsal import run_rehearsal_checks

    paths, _cfg, run_id = _paths(args)
    report = run_rehearsal_checks(paths.root, run_id=run_id,
                                  min_comparable=int(getattr(args, "min_comparable", 1)))
    payload = report.as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not report.ok:
        failed = "; ".join(f"{c.name}: {c.detail}" for c in report.failed)
        print(f"彩排**不合格**（{len(report.failed)} 项）：{failed}", file=sys.stderr)
        return EXIT_DOMAIN
    return 0

def _preflight_test(args: argparse.Namespace, *, paths: Any, proto: Any,
                    generation: int, run_id: str) -> int:
    """``eval test --check``：test 终检的只读预检（D-M3-5）。

    **不消耗解锁**：本函数不调 ``evaluate_arm``、不调 ``test_unlock``、不写任何文件
    （只做一次**可写性探测**：在目标目录建一个临时文件再删掉）。
    退出码与守卫同口径：预检不通过 ⇒ 非 0（这是**守卫**，不是判定器）。
    """
    import tempfile

    checks: list[dict[str, Any]] = []
    seed_count = 0

    def _add(name: str, ok: bool, detail: str) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    all_ok = True

    # ① test 池锁定状态：应为**锁定**（未解锁）。已解锁 ⇒ 本里程碑已用过（再跑会被
    #    `test_unlock` 拒），且更重要的：不该再有第二次。
    unlocked = seeds_mod.is_test_unlocked(proto)
    all_ok &= _add("test_pool_locked", not unlocked,
                   "已解锁（本里程碑已用过 test 池）" if unlocked else "锁定（符合预期）")

    # ② 篮子规模：test_full 的 seed 数必须 > 0（真跑要 100 局，但契约只保证非空；
    #    实际条数写进 detail 供人核对）。
    try:
        seeds_list = list(seeds_mod.basket_seeds("test_full", protocol=proto))
        seed_count = len(seeds_list)
        all_ok &= _add("test_basket_seeds", seed_count > 0,
                       f"test_full 共 {seed_count} 个 seed")
    except InfraError as exc:
        all_ok &= _add("test_basket_seeds", False, f"{exc.code}: {exc.message}")

    # ③ 解锁后的协议必须能通过校验（真跑时构造的就是它）
    try:
        unlocked_proto = seeds_mod.SeedProtocol(
            version=proto.version, hash_algo=proto.hash_algo, splits=proto.splits,
            exposure_guard=proto.exposure_guard, seeds_hash=proto.seeds_hash, unlocked=True)
        seeds_mod.validate_protocol(unlocked_proto)
        all_ok &= _add("unlocked_protocol_valid", True, "validate_protocol 通过")
    except InfraError as exc:
        all_ok &= _add("unlocked_protocol_valid", False, f"{exc.code}: {exc.message}")

    # ④ seeds_hash 可算（存证里要写 `protocol_hash`）
    try:
        digest = seeds_mod.protocol_hash(proto)
        all_ok &= _add("protocol_hash", bool(digest), str(digest))
    except InfraError as exc:
        all_ok &= _add("protocol_hash", False, f"{exc.code}: {exc.message}")

    # ⑤ 存证路径：**本预检的核心**——它必须「尚不存在」且「父目录可写」。
    requested = getattr(args, "attestation", None)
    att_path = (Path(requested) if requested
                else seeds_mod.test_attestation_path(paths.root, generation))
    all_ok &= _add("attestation_not_exists", not att_path.exists(),
                   f"{att_path}" + ("（已存在 ⇒ 该代已解锁）" if att_path.exists() else ""))
    parent = att_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkstemp(dir=str(parent), prefix=".preflight-")[1])
        probe.unlink()
        all_ok &= _add("attestation_dir_writable", True, str(parent))
    except OSError as exc:
        all_ok &= _add("attestation_dir_writable", False, f"{parent}: {exc}")

    payload = {"run_id": run_id, "generation": int(generation),
               "attestation_path": str(att_path), "test_seeds": seed_count,
               "ok": bool(all_ok), "checks": checks}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not all_ok:
        failed = ", ".join(c["check"] for c in checks if not c["ok"])
        print(f"[{E.E_IO_CONFIG_INVALID}] eval test 预检不通过：{failed}"
              "（**未消耗解锁**；修好再真跑）", file=sys.stderr)
        return EXIT_DOMAIN
    print("预检通过：test 终检可以安全起跑（解锁尚未消耗）", file=sys.stderr)
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
    ("eval", "acceptance"): cmd_eval_acceptance,
    ("rehearsal", "check"): cmd_rehearsal_check,
    ("eval", "test"): cmd_eval_test,
}
