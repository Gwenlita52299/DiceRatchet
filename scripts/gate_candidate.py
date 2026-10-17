"""对一个**预先存在的候选补丁**跑门禁（不训练、不提议），协议口径 A。

场景（2026-10-17，m3r3）：P1 判负后 v1 = v0；下一步要测的是**上一轮的 P2** 直接作用在
裸 v0 上（``v0+P2``）。标准 ``loop run`` 只能门禁"本 cycle 训练后产出的候选"，没有"测一个
已有补丁"的入口；本脚本把这条路径补齐：

1. 把候选 diff 应用到 v0 的 staging 拷贝；
2. 用与 ``evolve/judge.evaluate_candidate`` **同一套**判据（C1/C2/C5 + 配对检验）跑：
   候选冒烟 2 局 → 候选 heldout 40 局 → 与 parent（gen-000 的 v0 测量，0 局复用）比较；
3. 落 ``generations/gen-{g}/candidates/{id}.json`` + ``gate_candidate.json``，打印裁决。

**不写版本、不打 tag、不提交 git**：接受之后的版本落盘（versions/vN + harness-vN）是另一个
显式动作（当前 m3r2 已占用 v1，需要先决定 lineage 修复方式）。本脚本只回答"过不过门禁"。

成本：parent 冒烟 2 + 候选冒烟 2 + 候选 heldout 40 = **44 局**（≈¥38）；parent heldout 复用
gen-000 的归档（0 局）。``--dry-run`` 不花钱。

用法：
    .venv/bin/python scripts/gate_candidate.py --run-id m3r3 --dry-run
    .venv/bin/python scripts/gate_candidate.py --run-id m3r3
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dice_ratchet.evals.harness import arm_archive_path, load_arm_archive, restore_arm_from_archive
from dice_ratchet.evals.seeds import coerce_protocol, resolve_basket, take, validate_protocol
from dice_ratchet.evolve.judge import CandidateEvaluator, evaluate_candidate
from dice_ratchet.evolve.proposer import apply_unified_diff
from dice_ratchet.evolve.schemas import Candidate, HarnessRef
from dice_ratchet.evolve.versioning import play_hash
from dice_ratchet.infra import cli as infra_cli
from dice_ratchet.infra.config import load_config
from dice_ratchet.infra.io import write_json_atomic
from dice_ratchet.loop.cli import _build_orchestrator
from dice_ratchet import app as _app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="m3r3")
    ap.add_argument("--generation", type=int, default=1)
    ap.add_argument("--basket", default="H1")
    ap.add_argument("--candidate-id", default="2994b90ad15d")
    ap.add_argument("--proposal", default="runs/m3r2/generations/gen-001/proposals/proposal.json")
    ap.add_argument("--config-dir", default="configs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    _app.wire()   # 组合根：注册 MOD-03 客户端提供者（否则 _build_orchestrator 报 E_LLM_UNAVAILABLE）
    run_id = args.run_id
    run_root = Path("runs") / run_id
    harness_dir = run_root / "harness"
    if not harness_dir.is_dir():
        raise SystemExit(f"run 的 harness 不存在：{harness_dir}")

    # 1) 运行上下文（复用 loop 的装配：客户端 / 记忆 / runner / run_ctx）
    ns = SimpleNamespace(
        config_dir=args.config_dir, run_id=run_id, resume=True, dry_run=False,
        log_level=None, harness_dir=str(harness_dir), agent="llm", model=None, mode=None,
        k=None, generations=None, seeds=None, no_reflect=False, no_evolve=True, hitl=None,
        timeout_ms=None, baseline=None, force_rerun=False, retry_reflections=False,
        allow_ungated_start=True,
    )
    paths, cfg, run_id = infra_cli.cli_init_run(ns)
    orch, run_ctx = _build_orchestrator(paths, cfg, run_id, ns)

    proto = validate_protocol(coerce_protocol(None))
    heldout_seeds = tuple(resolve_basket(args.basket, generation=args.generation, protocol=proto))
    smoke_seeds = tuple(take("train", generation=args.generation, protocol=proto)[-2:])
    v0_hash = play_hash(harness_dir)

    # 2) parent heldout：复用 gen-000 的 v0 测量（0 局）
    parent_arm_path = arm_archive_path(run_root, generation=0, split="heldout",
                                       basket=args.basket, arm="parent")
    ev = CandidateEvaluator(
        protocol=proto, config=cfg, memory_version=int(run_ctx.state.memory_version),
        run_id=run_id, generation=int(args.generation), smoke_seeds=smoke_seeds,
        runner=orch.runner, episode_config=run_ctx.episode_config,
        run_dir=paths.root, heldout_basket=args.basket)
    parent_heldout = restore_arm_from_archive(
        load_arm_archive(parent_arm_path), generation=0, harness_version=0, arm="parent",
        required_seeds=heldout_seeds, play_hash=v0_hash, **ev.eval_limits())
    if parent_heldout is None:
        raise SystemExit(f"parent heldout 无法还原（gen-000 归档）：{parent_arm_path}")

    # 3) 候选：从上一轮的 proposal 取同一条补丁
    proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    raw = (proposal.get("proposal") or {}).get("candidates") or []
    match = [c for c in raw if str(c.get("candidate_id")) == args.candidate_id]
    if not match:
        raise SystemExit(f"proposal 里没有候选 {args.candidate_id}")
    cand = Candidate.from_dict({**match[0], "gen_id": f"gen-{int(args.generation):03d}"})

    # 4) staging：v0 拷贝 + 应用补丁
    staging = paths.root / "generations" / f"gen-{int(args.generation):03d}" / "staging" / cand.candidate_id
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(harness_dir, staging)
    if (staging / "versions").exists():
        shutil.rmtree(staging / "versions")
    touched = apply_unified_diff(staging, cand.diff, paths=cand.touched_files)
    cand_hash = play_hash(staging)

    print(json.dumps({
        "run_id": run_id, "generation": args.generation, "basket": args.basket,
        "candidate_id": cand.candidate_id, "staging": str(staging),
        "touched": list(touched),
        "parent_play_hash": v0_hash, "candidate_play_hash": cand_hash,
        "parent_heldout": {"n": parent_heldout.metrics.n,
                           "win_rate": parent_heldout.metrics.win_rate,
                           "source": f"restored:{parent_arm_path.name}"},
        "would_spend_episodes": 2 + 2 + len(heldout_seeds),
        "smoke_seeds": list(smoke_seeds),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    if args.dry_run:
        print("=== dry-run：不调用模型、不花钱 ===")
        return 0

    # 5) parent 冒烟（2 局；heldout 用还原的，避免重测 40 局）
    _, parent_smoke = ev.parent_arms(str(harness_dir), parent_version=0,
                                     inherited=parent_heldout, play_hash=v0_hash)

    # 6) 门禁：同一套判据
    report = evaluate_candidate(
        cand, HarnessRef(version=0, dir=str(harness_dir)), cfg=cfg.evolve,
        staging_dir=staging, evaluator=ev, parent_heldout=parent_heldout,
        parent_smoke=parent_smoke, harness_version=0, generation=int(args.generation),
        smoke_seeds=smoke_seeds, play_hash=cand_hash, parent_play_hash=v0_hash)

    out_dir = paths.root / "generations" / f"gen-{int(args.generation):03d}"
    write_json_atomic(out_dir / "candidates" / f"{cand.candidate_id}.json", report.as_dict())
    summary = {
        "run_id": run_id, "generation": int(args.generation), "basket": args.basket,
        "candidate_id": cand.candidate_id,
        "decision": report.verdict.decision if report.verdict else None,
        "reason": report.reason,
        "error_code": report.error_code,
        "parent_play_hash": v0_hash, "candidate_play_hash": cand_hash,
        "parent_heldout": {"n": int(parent_heldout.metrics.n),
                           "win_rate": parent_heldout.metrics.win_rate},
        "candidate_heldout": report.heldout,
        "paired": report.paired,
        "smoke": report.smoke,
        "cost_note": "44 局（parent 冒烟 2 + 候选冒烟 2 + 候选 heldout 40）；parent heldout 复用 gen-000",
    }
    write_json_atomic(out_dir / "gate_candidate.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
