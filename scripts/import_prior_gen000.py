"""把**上一轮 gen-000 的 v0 基线**导入新一轮 run（40 局 heldout 的测量 + 臂归档）。

与 ``scripts/import_prior_run.sh`` 的分工：
* 那个脚本导入 **30 局 train + 反思 + memory_v1**（学习状态）；
* 本脚本导入 **gen-000 的 40 局 heldout 测量**（v0 的基线），并把报告写进新 run 的
  ``generations/gen-000/``、把臂归档写进 ``metrics/arms/``，最后补台账/绘图。

**为什么可以跨 run 复用这份测量**（而不是重跑 40 局）：
* 局的**定义**没变：两轮 ``episode_config_hash`` 逐位相同（``sha256:113728b4…``）；
* 模型的**身份**没变：deepseek-flash / deepseek；harness 的 play 面 = 原始 v0；
* 唯一的 ``config_hash`` 差异来自旧 run 的 15 个 ``env.sandbox.*`` 死键在 F73/F75 后被删
  （行为等价，见 docs/reviews/2026-10-17-M3-next-round-P1起点设计.md §3.1）。

**诚实边界**：报告里 ``heldout.source`` 保持 ``measured``（它确实是测出来的，只是在另一个
run 里测的）；跨 run 的事实记在 ``import_prior.json`` 的 ``gen000_heldout`` 段，不伪造
``inherited:`` 来源。臂归档只回填 ``harness_play_hash``（旧归档缺这个键），逐局结果一字不改。

用法：
    .venv/bin/python scripts/import_prior_gen000.py --from-run m3-formal --to-run m3r3
    .venv/bin/python scripts/import_prior_gen000.py --from-run m3-formal --to-run m3r3 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dice_ratchet.evals.harness import (arm_archive_path, arms_dir, ledger_and_plots,
                                        load_arm_archive, restore_arm_from_archive)
from dice_ratchet.evals.harness import write_generation_report
from dice_ratchet.evolve.versioning import play_hash
from dice_ratchet.infra.config import load_config, config_hash
from dice_ratchet.infra.io import read_json, write_json_atomic


def _backfill_arm(src_arm: Path, dst_arm: Path, *, play_hash_value: str,
                  run_id: str) -> dict:
    data = load_arm_archive(src_arm)
    if not data:
        raise SystemExit(f"源臂归档不可读：{src_arm}")
    provenance = dict(data.get("provenance") or {})
    provenance["harness_play_hash"] = play_hash_value
    data = dict(data)
    data["provenance"] = provenance
    data["run_id"] = run_id
    data["archive_path"] = str(dst_arm)
    dst_arm.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(dst_arm, data)   # 逐局结果**原样**保留，只改 provenance/来源字段
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-run", default="m3-formal")
    ap.add_argument("--to-run", default="m3r3")
    ap.add_argument("--basket", default="H1")
    ap.add_argument("--generation", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src = Path("runs") / args.from_run
    dst = Path("runs") / args.to_run
    src_report = src / "generations" / f"gen-{args.generation:03d}" / "report.json"
    if not src_report.is_file():
        raise SystemExit(f"源 gen-000 报告不存在：{src_report}")
    report = read_json(src_report)
    if not isinstance(report, dict):
        raise SystemExit(f"源报告不是对象：{src_report}")

    harness_dir = dst / "harness"
    v0_play = str(play_hash(harness_dir))
    src_arm = arm_archive_path(src, generation=args.generation,
                               split="heldout", basket=args.basket, arm="parent")
    if not src_arm.is_file():
        raise SystemExit(f"源臂归档不存在：{src_arm}")

    report = dict(report)
    report["harness_version"] = 0
    report["harness_play_hash"] = v0_play
    # 旧报告的 `episode_config_hash` 是 null（写报告时该字段还没被填），但**臂归档**的
    # provenance 记了真实值——它是"局的定义"的测量身份，必须回填，否则新 run 的审计链缺一环。
    src_arm_data = load_arm_archive(src_arm) or {}
    arm_prov = src_arm_data.get("provenance") or {}
    if not report.get("episode_config_hash"):
        report["episode_config_hash"] = arm_prov.get("episode_config_hash")
    payload = {
        "from_run": args.from_run, "to_run": args.to_run,
        "generation": int(args.generation), "basket": args.basket,
        "source_report": str(src_report), "source_arm": str(src_arm),
        "harness_play_hash": v0_play,
        "episode_config_hash": report.get("episode_config_hash"),
        "arm_episode_config_hash": arm_prov.get("episode_config_hash"),
        "config_hash_source": config_hash(load_config(config_dir=Path("configs"))),
        "note": ("跨 run 复用 v0 的 heldout 测量：局定义与模型身份不变，config_hash 差异只来自"
                 "旧 run 的 sandbox 死键；逐局结果不改，仅回填 harness_play_hash。"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("=== dry-run：不写任何文件 ===")
        return 0

    # 1) 报告（走 MOD-08 的唯一写入口：schema + sha256 sidecar）
    write_generation_report(dst, int(args.generation), report)
    # 2) 臂归档（回填 play_hash）
    dst_arm = arm_archive_path(dst, generation=args.generation,
                               split="heldout", basket=args.basket, arm="parent")
    _backfill_arm(src_arm, dst_arm, play_hash_value=v0_play, run_id=args.to_run)
    # 3) 台账 + 绘图（F56 要求 gen-000 也进台账）
    cfg = load_config(config_dir=Path("configs"))
    ledger_and_plots(dst, int(args.generation), report, cfg=cfg)

    # 4) 校验：生产读路径能还原 40 局
    from dice_ratchet.evals.seeds import coerce_protocol, resolve_basket, validate_protocol

    proto = validate_protocol(coerce_protocol(None))
    seeds = tuple(resolve_basket(args.basket, generation=int(args.generation), protocol=proto))
    restored = restore_arm_from_archive(
        load_arm_archive(dst_arm), generation=int(args.generation), harness_version=0,
        arm="parent", required_seeds=seeds, play_hash=v0_play)
    ok = restored is not None and int(restored.metrics.n) == len(seeds)
    print(json.dumps({
        "written": [str(dst / "generations" / f"gen-{args.generation:03d}" / "report.json"),
                    str(dst_arm)],
        "restore_ok": bool(ok),
        "restored_n": None if restored is None else int(restored.metrics.n),
        "win_rate": None if restored is None else restored.metrics.win_rate,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
