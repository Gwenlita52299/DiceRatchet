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
from dataclasses import replace
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
from .harness import (VERDICT_DECISIONS, EvalSpec, evaluate_arm,
                      ledger_and_plots,
                      generation_report_path, load_episode_results, run_eval, utc_now,
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


#: 只有 **heldout** split 允许 `eval report` 写权威产物（report.json / 台账 / 绘图 / ext）。
#:
#: F71（2026-10-14）：`--split train` 走的是同一条代码路径，而它把 **train 的聚合**塞进
#: `report["heldout"]` 再交给 `_write_report_preserving_verdict` + `ledger_and_plots`
#: ⇒ 在 **gen-000（decision="baseline"）或 HITL pending 代**上（二者都不在
#: ``VERDICT_DECISIONS`` 里）会**覆盖 report.json 的 heldout 段**，并写出一条
#: `split="heldout"` 而数值来自 train 臂的台账行。修复前这条路径只是**碰巧**被 F61 的
#: 臂解析错误（退出码 2）挡住了——一旦把臂解析修好，就会变成一个静默的跨 split 污染。
#: 因此 `--split train` 现在是**只读**的（打印 + 落一份非权威 sidecar）。
_WRITABLE_SPLITS: Final[tuple[str, ...]] = ("heldout",)
#: train split 的臂名（F61）：F64 之后真训练局记 ``arm="train"``；旧 run 只有冒烟臂。
_TRAIN_ARM: Final[str] = "train"
_TRAIN_ARM_FALLBACKS: Final[tuple[str, ...]] = ("parent-smoke",)


def build_inference_client(cfg: Any, *, run_id: str, runs_root: Path) -> Any:
    """按已登记的提供者建客户端；未接线时返回 ``None``（交由 ``run_baseline`` fail-closed）。"""
    if _INFERENCE_CLIENT_PROVIDER is None:
        return None
    return _INFERENCE_CLIENT_PROVIDER(cfg, run_id=run_id, runs_root=Path(runs_root))



def _paths(args: argparse.Namespace, *, require_state: bool = True,
           read_existing: bool = False) -> tuple[Any, Any, str]:
    """建/定位 run（复用 MOD-09 的启动序列：load_config → run_id → events.bind）。

    ``require_state=False``：``baseline --resume`` 用——基线 run 从不写 ``state.json``
    （那是 MOD-06 的语义），因此续跑只要求 run 目录 + ``episodes/`` 已存在的局（F18）。

    ``read_existing=True``（F55，2026-10-12）：**只读命令**（``eval report`` /
    ``eval diversity`` / ``eval acceptance`` / ``rehearsal check`` / ``eval test --check``）
    用——它们**不改 run 的配置语义**，也不该被 ``config_hash`` 一致性门禁拦下：
    该门禁防的是「两套配置写同一个 run」，而只看已落盘产物不需要这层保护。
    现场（实测）：`loop run --generations 2` 的 run，`loop.max_generations=2` 进
    ``config_hash``；而 ``rehearsal check`` 没有 ``--generations`` override ⇒ 算出
    ``3`` ⇒ ``E_IO_RUN_CONFLICT``（`eaf6f18ce6e7865a != 9403576ed90e79ba`）。后果是
    `scripts/run_m3_rehearsal.sh` **结尾那一关必然失败**（彩排永远不可能「合格」）。
    """
    return infra_cli.cli_init_run(args, require_state=require_state,
                                  read_existing=read_existing)


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

    - ``--arm <name>``：只看见该臂；**臂名不存在即失败**（``E_EVAL_ARM_NOT_ALLOWED``，
      不再静默回落到 parent——那会让 `--arm` 的拼写错误安静地给出 parent 的数）；
    - ``--arm all``：**显式**申报的混合池（并在 stdout 与 ext 里标注 ``mixed_pool=true``），
      **禁止落进代际台账**（混合池没有「哪个臂」的物理含义）；
    - 缺省（F57，2026-10-12 起）：**``parent``**（该代的起点臂）。修复前默认是
      「该代胜出候选 → 唯一候选 → parent」，后果是**一次重算就能改写代际台账**：
      实测 `runs/_sweep/m3-rehearsal` gen-001 的 heldout 行先由 ``loop run`` 写成
      parent 的 `0.10`，被本命令按候选臂重算后变成 `0.35`，判据 1 的结论随之从
      「单调性不成立」翻成 `monotonic_ok=true`。候选自己的测量请显式
      ``--arm cand-<candidate_id>``（它落在**另一行**，见 ``arm`` 列）。
    - ``--split train``（**F71，2026-10-14**）：本命令是**只读**的——打印 + 落
      ``metrics/gen-NNN.<split>.readonly.json``，**不写** ``report.json`` / 代际台账 /
      绘图 / ext。原因见 ``_WRITABLE_SPLITS``：train 的聚合一旦写进那个 ``heldout``
      槽位，就会在 baseline/pending 代上覆盖权威报告的 heldout 段。
    """
    paths, cfg, run_id = _paths(args, read_existing=True)
    split = str(getattr(args, "split", "heldout") or "heldout")
    generation = _generation_of(args, paths.root)
    proto = _protocol(cfg, args.config_dir)
    eval_cfg = cfg.evals
    pool_results = load_episode_results(paths.root, split, generation=generation,  # type: ignore[arg-type]
                                        exclude_arms=_BASELINE_ARMS)
    requested_arm = getattr(args, "arm", None)
    arm, mixed = _resolve_report_arm(paths.root, generation, pool_results,
                                     requested=requested_arm, split=split)
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
    # F64（2026-10-14）：train 段与 `evals/harness.evaluate_generation` **同一口径**——
    # 取 ``take("train", generation)`` 那个块、按 ``arm="train"`` 真过滤（同代冒烟局用的是
    # 本代训练块的末尾 seed，不过滤会混进来）、不足则标注 ``insufficient``。
    # 修复前这里是 `train_window` + 无臂过滤 ⇒ 实测拿到的是**4 局冒烟局**（`train.n=4`），
    # 而它在 `generations.csv` 里长得跟真训练段一模一样。两处各写一半正是 F43/F56 的教训。
    if int(generation) >= 1:
        train_seeds = seeds_mod.take("train", generation=int(generation), protocol=proto)
        train_results = load_episode_results(paths.root, "train", generation=generation,
                                             harness_version=generation, seeds=train_seeds,
                                             arm="train")
    else:
        train_results = []
    train = aggregate(train_results, split="train", basket="train_window",
                      t_max=int(eval_cfg.t_max), min_support=int(eval_cfg.min_support),
                      drop_rate_max=float(eval_cfg.drop_rate_max))
    min_support = int(eval_cfg.min_support)
    if int(generation) < 1:
        train_source = "unavailable"
        train = replace(train, low_confidence=True)
    elif len(train_results) >= min_support:
        train_source = "measured"
    else:
        train_source = "insufficient"
        train = replace(train, low_confidence=True)
    # 落该代的 kind 分布（供 g+1 的 `c3` 与后续重算读回；见 harness.write_kind_distribution）
    try:
        from .diversity import kind_distribution
        from .harness import write_kind_distribution

        dist = kind_distribution(results or pool_results)
        if dist:
            write_kind_distribution(paths.root, generation, dist)
    except InfraError as exc:
        events.emit("eval.kind_distribution_skipped", code=str(exc.code),
                    detail=str(exc)[:200])
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
    # F60（2026-10-14）：版本列 / play 面指纹 / heldout 出处**只能来自已落盘的 report.json**。
    # 现场：这里原先凭空写 `harness_version=generation / parent_version=generation-1`，
    # 而这张**内存里**的 report 随后进了 `ledger_and_plots` ⇒ 台账按 (gen, split, arm) upsert
    # 把 loop 写下的 0/0 **静默覆盖**成 3/2（`runs/m3-formal` gen-003 实测），
    # 判据 1 因此把 `not_applicable` 翻成假结论「单调性不成立」（comparable_pairs 0→1）。
    # ``heldout`` 是 ``SplitMetrics``（没有 harness_version 字段）⇒ 版本从**逐局记录**取：
    # 那是「这批局真在哪份 harness 上跑的」的唯一落盘事实。
    arm_version = int(getattr(results[0], "harness_version", 0) or 0) if results else 0
    versions = _report_versions(paths.root, generation, arm_version=arm_version,
                               existing=_read_existing_report(paths.root, generation))
    if versions["source"] != "report_json":
        print(f"[{E.E_IO_RUN_CONFLICT}] 第 {generation} 代没有权威 report.json：版本列退回"
              f"**该臂归档里的实测版本**（harness_version={versions['harness_version']}），"
              f"不臆造 generation/generation-1", file=sys.stderr)
    report = render_report_sections(
        generation=generation,
        harness_version=int(versions["harness_version"]),
        parent_version=int(versions["parent_version"]),
        heldout=heldout, train=train, gain=gain, diversity=diversity, ablation=None,
        decision="baseline" if generation == 0 else "pending", decided_by="auto",
        reason="eval report（从轨迹重算）",
        heldout_source=str(versions["heldout_source"] or "measured"),
        play_hash=str(versions["play_hash"] or ""),
        train_source=str(versions["train_source"] or "measured"),
        extra={"power_note": power_note(heldout.n)})
    if split not in _WRITABLE_SPLITS:
        # F71（2026-10-14）：非 heldout split **只读**。这条分支必须在任何
        # `_write_report_preserving_verdict` / `_write_plots` / `write_ext_metrics` /
        # `ledger_and_plots` 之前返回——见 `_WRITABLE_SPLITS` 的说明（修复前 `--split train`
        # 会把 train 聚合写进 `report["heldout"]` 并落一条 split="heldout" 的台账行）。
        return _write_readonly_split_recompute(
            run_root=paths.root, generation=generation, split=split, arm=arm,
            mixed=mixed, heldout=heldout, train=train, report=report,
            arms_present=_arms_of(pool_results), requested_arm=requested_arm,
            provenance=_provenance(cfg, run_id, generation, heldout.basket, proto))
    path, sha = _write_report_preserving_verdict(
        paths.root, generation, report, existing_decided=_verdict_of(paths.root, generation))
    _write_plots(paths.root, generation, heldout, diversity)
    ext = _ext_payload(generation, heldout, gain, diversity, eval_cfg, baselines=baseline,
                       provenance=_provenance(cfg, run_id, generation, heldout.basket, proto))
    # F46-A：重算口径必须自解释（哪个臂 / 是不是混合池），否则「可复算」再次不可核验。
    # 不能塞进 report.json：`generation_report.schema.json` 是 `additionalProperties:false`。
    ext.update({"recomputed_arm": arm, "mixed_pool": bool(mixed),
                "pool_n": pool_metrics.n, "arms_present": _arms_of(pool_results),
                "requested_arm": (str(requested_arm) if requested_arm else None),
                # F60：版本列/play 面指纹的出处；`report_json` = 权威（MOD-07 写下的事实），
                # `arm_archive` = 没有报告时退回臂归档里的实测版本。
                "version_source": str(versions["source"]),
                "play_hash": str(versions["play_hash"] or "")})
    # F-M3-9：判据 1 的结论随报告一起可读。**只在台账里有两代可比时才算**——
    # 每代都算会退化；且单代 run（如 gen-000 基线）本来就没有「单调性」可言。
    # 配对 bootstrap 的开销按「可比较的代数」增长，不随本命令重复调用而放大。
    _augment_ext_with_acceptance(ext, paths.root, run_id, cfg)
    write_ext_metrics(paths.root, generation, ext)
    # F-M3-3（2026-10-11）：台账写**全部** split 行（heldout + train），且**用与 MOD-07 同一个
    # 构造器**（`generations_rows_from_report`）——原先这里用本地 `_csv_row` 只写 heldout 行，
    # 于是 train 一侧在台账里永远缺席（实测 8 个归档 run 的 train 行数全为 0），
    # `train_heldout_gap.png` 与 `win_rate.png` 的 train 对照曲线只能画成空白。
    # F56（2026-10-12）：台账与绘图走**同一个**助手（MOD-06 基线、MOD-07 代末、本命令
    # 三方共用），避免「谁记得写哪一半」再分叉。纪律不变：附属产物失败不阻塞报告。
    # F57：臂**显式**传进去（台账行按 `(generation, split, arm)` 落键）。`--arm all` 是
    # 混合池，没有「哪个臂」的物理含义 ⇒ **不允许**进代际台账（否则判据 1 会读到一个
    # 谁也不是的数）；此时按 parent 记行，并在 stderr 说清。
    # F57：该代 heldout 若是**继承**来的（本代没有真实测量），重算也**不新增台账行**
    # （`write_ledger=False`）——继承的出处只在 `report.json` 的 `heldout.source`；
    # 补一行会在图上多出一个与上一代**重合**的点。绘图仍照跑（读既有台账）。
    source = _ledger_source_of(paths.root, generation)
    inherited_row = source.startswith("inherited:")
    if mixed:
        print(f"[{E.E_EVAL_ARM_NOT_ALLOWED}] --arm all：heldout 是**混合池**"
              f"（n={heldout.n}，臂={_arms_of(pool_results)}），不是单臂口径："
              f"禁止当默认口径用，且**不写代际台账**", file=sys.stderr)
        ledger_and_plots(paths.root, generation, report, cfg=cfg, write_ledger=False)
    elif inherited_row:
        print(f"[{E.E_EVAL_ARM_NOT_ALLOWED}] 第 {generation} 代的 heldout 是**继承**来的"
              f"（{source}）：不新增台账行，只重画图", file=sys.stderr)
        ledger_and_plots(paths.root, generation, report, cfg=cfg, arm=arm,
                         write_ledger=False)
    else:
        ledger_and_plots(paths.root, generation, report, cfg=cfg, arm=arm)
    print(f"report: {path}")
    print(f"sha256: {sha}")
    print(f"arm: {arm}{'（混合池，--arm all）' if mixed else ''} n={heldout.n}")
    _print(report)
    return 0


def _arms_of(results: Sequence[Any]) -> list[str]:
    """结果集合里出现过的臂（升序去重）。"""
    return sorted({str(getattr(r, "arm", "") or "") for r in results})


def _resolve_report_arm(run_root: Path, generation: int, pool: Sequence[Any], *,
                        requested: Any, split: str = "heldout") -> tuple[str, bool]:
    """``eval report`` 该按哪个臂重算（F46-A；**默认臂 F57 改为 parent**）。

    返回 ``(arm, mixed_pool)``。``requested`` 的取值：

    - ``None``/``""`` → **``parent``**（该代的起点臂），前提是它在这个 split 里真的有局；
      否则回落该代胜出候选 → 唯一候选 → 该代唯一的臂 → 抛 ``E_EVAL_ARM_NOT_ALLOWED``。
      **为什么默认是 parent**：代际台账/判据 1 的口径是「这一代的**起点**在哪」，
      而候选测量是**代内**比较。F57 现场：默认解析到候选后，一次重算就把 gen-001 的
      台账从 parent 的 `0.10` 改成候选的 `0.35`，判据 1 的结论跟着翻面。
    - ``"all"`` → 混合池（``mixed_pool=True``）；调用方必须拒绝把它落进台账。
    - 其它字符串 → **必须真的在盘上**：不存在即抛 ``E_EVAL_ARM_NOT_ALLOWED``
      （修复前静默回落到 ``"parent"``，于是 ``--arm cand-typo`` 会安静地给出 parent 的数，
      调用方以为拿到的是候选的数）。

    ``split``（F61，2026-10-14）：``train`` split 的臂命名是另一套（F64 之后真训练局记
    ``arm="train"``，另有 ``parent-smoke`` / ``cand-*-smoke``）；不认这一套时
    ``eval report --split train`` 必然抛 ``E_EVAL_ARM_NOT_ALLOWED``（现场退出码 2，
    train 侧报告根本没产出）。**注意**：修好臂解析只是让这条命令能跑；
    ``--split train`` 本身是**只读**的（见 ``_WRITABLE_SPLITS``）。
    """
    text = str(requested or "").strip()
    if text.lower() == "all":
        return "all", True
    present = set(_arms_of(pool))
    if not present:
        # 一个臂都没有（该 split 的局缺失/为空）：退回**该 split 的**默认臂名，让下游按它
        # 去报「没有局」的具体数字与 `E_EVAL_ARM_NOT_ALLOWED` 消息——**不在这里猜**。
        return (_TRAIN_ARM if str(split) == "train" else "parent"), False
    if text:
        if text not in present:
            raise InfraError(
                E.E_EVAL_ARM_NOT_ALLOWED,
                f"第 {generation} 代在该 split 上没有臂 {text!r} 的局"
                f"（已出现：{sorted(present)}）",
                hint="臂名必须与落盘的 arm 逐字一致（候选臂是 `cand-<candidate_id>`）")
        return text, False
    if str(split) == "train":
        # F61（2026-10-14）：train split 的臂命名是另一套（F64 之后真训练局记
        # `arm="train"`，另有 `parent-smoke` / `cand-*-smoke`）。修复前解析器只认 heldout
        # 口径 ⇒ `eval report --split train` 必然抛 `E_EVAL_ARM_NOT_ALLOWED`（现场退出码 2）。
        for candidate_arm in (_TRAIN_ARM, *_TRAIN_ARM_FALLBACKS):
            if candidate_arm in present:
                return candidate_arm, False
    if "parent" in present:
        return "parent", False
    winner = _winner_arm_of(run_root, generation)
    if winner and winner in present:
        print(f"[{E.E_EVAL_ARM_NOT_ALLOWED}] 第 {generation} 代没有起点臂（parent）的局；"
              f"回落到胜出候选 {winner!r}（该代台账口径与别的代不同，注意）",
              file=sys.stderr)
        return winner, False
    if len(present) == 1:
        return next(iter(present)), False
    raise InfraError(
        E.E_EVAL_ARM_NOT_ALLOWED,
        f"第 {generation} 代既没有臂 'parent'，也推不出唯一候选臂"
        f"（已落盘：{sorted(present)}）",
        hint="显式传 --arm <name>；`--arm all` 是混合池，禁止当默认口径")


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
    # **修 `c3` 的死代码**（2026-10-11）：修复前这里是
    # `jsds = [None] * max(0, len(curves) - 1)` —— **硬编码 null**，
    # 于是 `c3`（冻结 + 胜率跳变）在 `eval report` 路径上**永不触发**
    # （实测归档里 17 个代的 `jsd_kind` 全是空）。现在读回各代落盘的 kind 分布算真值。
    #
    # 长度必须 == `len(curves)`：`collapse_check` 按代索引取 `jsds[i]`。
    from .harness import aligned_jsd_kinds, kind_distribution_path

    gens: list[int] = []
    for path in sorted((run_dir / "metrics" / "plots_data").glob("gen-*_entropy.json")):
        stem = path.stem.split("_")[0]        # "gen-000"
        if stem.startswith("gen-") and stem[4:].isdigit():
            gens.append(int(stem[4:]))
    if len(gens) != len(curves):
        gens = list(range(len(curves)))
    jsds = aligned_jsd_kinds(run_dir, gens, base=float(getattr(cfg.evals.entropy, "base", 2.0)))
    if len(jsds) != len(curves):
        jsds = [None] * len(curves)
    del kind_distribution_path  # 仅为显式说明落盘口径来自 harness（此处只读回）
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


def _ledger_source_of(run_root: Path | str, generation: int) -> str:
    """已落盘报告中该代 ``heldout.source``（F57）；读不到 → ``""``。

    用途只有一个：判断「这一代的 heldout 是**本代真测**还是**跨代继承**」——
    继承的代重算时**不得**新增台账行（写行会造出一个与上一代重合的数据点）。
    """
    data = _read_existing_report(run_root, generation)
    if not data:
        return ""
    return str((data.get("heldout") or {}).get("source") or "")


def _report_versions(run_root: Path | str, generation: int, *, arm_version: int,
                     existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """版本列 / play 面指纹 / heldout 出处的**唯一权威来源**（F60，2026-10-14）。

    优先级只有一个方向：**已落盘的 ``generations/gen-NNN/report.json``**（MOD-07/MOD-06
    写下的事实）。没有报告（纯测量 run、报告损坏）时退回**该臂归档里的实测版本**
    （``ArmResult.harness_version``，那是「这批局真在哪份 harness 上跑的」），
    ``parent_version`` 与它取同值——**绝不臆造 ``generation`` / ``generation-1``**。

    现场（F60）：``eval report`` 原先写死 ``harness_version=generation``，
    而它算出的行会按 ``(generation, split, arm)`` upsert 进台账 ⇒ 一条**只读**命令
    把 ``runs/m3-formal`` gen-003 的 0/0 改成 3/2，判据 1 的结论随之翻面。

    返回 ``{harness_version, parent_version, play_hash, heldout_source, train_source,
    source}``；``source ∈ {"report_json", "arm_archive"}``。
    """
    data = existing if existing is not None else _read_existing_report(run_root, generation)
    if data and "harness_version" in data:
        heldout = dict(data.get("heldout") or {})
        train = dict(data.get("train") or {})
        return {"harness_version": int(data.get("harness_version") or 0),
                "parent_version": int(data.get("parent_version") or 0),
                "play_hash": str(data.get("harness_play_hash") or ""),
                "heldout_source": str(heldout.get("source") or "measured"),
                "train_source": str(train.get("source") or "measured"),
                "source": "report_json"}
    return {"harness_version": int(arm_version), "parent_version": int(arm_version),
            "play_hash": "", "heldout_source": "measured", "train_source": "measured",
            "source": "arm_archive"}


def _write_readonly_split_recompute(*, run_root: Path | str, generation: int, split: str,
                                    arm: str, mixed: bool, heldout: Any, train: Any,
                                    report: Mapping[str, Any], arms_present: Sequence[str],
                                    requested_arm: Any,
                                    provenance: Mapping[str, Any]) -> int:
    """非 heldout split 的**只读**复算（F71，2026-10-14）：打印 + 落非权威 sidecar。

    为什么不写权威产物（**这条是本次修复的要害**）：`eval report` 的写入路径会把
    ``heldout`` 这个槽位的聚合写进 ``report.json`` 的 ``heldout`` 段，并派生一条
    ``split="heldout"`` 的台账行。`--split train` 复用同一条路径 ⇒ 一旦臂解析被修好
    （F61），它在 **gen-000（``decision="baseline"``）或 HITL ``pending`` 代**上
    （两者都不在 ``VERDICT_DECISIONS`` 里 ⇒ 不会被 sidecar 保护）会：
    ① 用 train 的聚合**覆盖 report.json 的 heldout 段**；② 写一条数值来自 train 臂
    的 heldout 台账行（只因它带 ``arm="train"`` 才恰好被判据 1 的
    ``arm_of_row == "parent"`` 过滤躲过）；③ 用 train 的熵曲线覆盖 ``plots_data``。
    修复前这条路径只是**碰巧**被 F61 的退出码 2 挡住。

    产物落 ``metrics/gen-NNN.<split>.readonly.json``：名字里带 ``readonly``，且**没有任何
    消费者**（台账/判据 1/绘图都不读它）——它只是把这次复算的输入口径留档。
    """
    payload = {
        "generation": int(generation), "split": str(split), "arm": str(arm),
        "mixed_pool": bool(mixed), "arms_present": list(arms_present),
        "requested_arm": (str(requested_arm) if requested_arm else None),
        "heldout": heldout.as_dict(), "train": train.as_dict(),
        "report": dict(report),
        "readonly": True,
        "note": ("非 heldout split 的只读复算：不写 report.json / 不写代际台账 / 不画图。"
                 "`--split heldout` 才是权威口径（F71，2026-10-14）"),
    }
    payload.update(dict(provenance or {}))
    path = Path(run_root) / "metrics" / f"gen-{int(generation):03d}.{split}.readonly.json"
    write_json_atomic(path, payload)
    print(f"readonly: {path}")
    print(f"arm: {arm}{'（混合池，--arm all）' if mixed else ''} n={heldout.n}")
    print(f"[只读] split={split} 不写 report.json / 台账 / 绘图；权威口径是 --split heldout")
    _print(payload)
    return 0


def _read_existing_report(run_root: Path | str, generation: int) -> dict[str, Any]:
    """读已存在的 ``report.json``（缺失/损坏 → 空 dict，不抛）。F57 用。"""
    path = generation_report_path(run_root, generation)
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except InfraError: # pragma: no cover - 损坏报告按「读不到」处理
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


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


# =====================================================================
# eval diversity
# =====================================================================

def cmd_eval_diversity(args: argparse.Namespace) -> int:
    """动作熵 / 坍缩监控（G6；写 ext 与 plots_data）。

    F46-A：`generation` 现在是真过滤（同 ``load_episode_results`` 口径）。``--arm`` 缺省
    仍是**整池**（坍缩是代际现象，只看单一臂会退化成单臂曲线）；给出具体臂名时只看该臂。
    """
    paths, cfg, run_id = _paths(args, read_existing=True)
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

    paths, cfg, run_id = _paths(args, read_existing=True)
    B = getattr(args, "bootstrap_B", None) or int(getattr(cfg.evals, "bootstrap_B", 10000))
    report = evaluate_acceptance(
        paths.root, run_id=run_id,
        min_heldout_n=int(getattr(cfg.evolve, "min_heldout_n", 20)),
        bootstrap_B=int(B))
    path = write_acceptance(paths.root, report)
    payload = report.as_dict()
    payload["acceptance_path"] = str(path)
    if bool(getattr(args, "sensitivity", False)):
        # §3.3 的**零成本**落地：把「tol=0.05 意味着多大漂移」变成实测数字。
        # 分母优先用本 run 的**实测**值；读不到才回落到 `C1` hard×6 的一代口径。
        from .tol_sensitivity import denominators_from_run, tolerance_sensitivity

        measured = denominators_from_run(paths.root)
        sens = tolerance_sensitivity(
            illegal_tol=float(getattr(cfg.evolve, "illegal_tol", 0.05)),
            fallback_tol=float(getattr(cfg.evolve, "fallback_tol", 0.05)),
            truncation_tol=float(getattr(cfg.evolve, "truncation_tol", 0.05)),
            denominators=(measured or None),
            rates={"truncation": 0.30})
        payload["tol_sensitivity"] = sens.as_dict()
        payload["tol_sensitivity"]["denominators_source"] = (
            "本 run 实测" if measured else "缺省估算（C1 hard×6 一代口径）")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not report.ok:
        print(f"判据 1 **不通过**：{'; '.join(report.reasons)}", file=sys.stderr)
    return 0


def cmd_eval_ablation(args: argparse.Namespace) -> int:
    """H2 上跑 3 臂消融并做配对检验（FR-08-21；G6）。"""
    paths, cfg, run_id = _paths(args, read_existing=True)
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
    paths, cfg, run_id = _paths(args, read_existing=True)
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

    paths, _cfg, run_id = _paths(args, read_existing=True)
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
