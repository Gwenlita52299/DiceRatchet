#!/usr/bin/env bash
# 把上一轮 run 的**训练学习状态**导入新一轮 run（本轮的 gen-000 训练块复用）。
#
# 为什么要它：用户口径「gen-000 的 30 局 train 可以直接复用」——那 30 局跑在
# `runs/m3-formal/`（seed 1000–1029 + 反思 + memory_v1.jsonl）。但旧轨迹是 F64 修复前落的
# （`generation` 标成 0，而当前代码要求标"它服务的那一代" = 1），直接拷会取不到。
# 本脚本**只读旧 run、只写新 run**：旧 run 一个字节不改。
#
# 用法（全部免费，不跑任何局）：
#   scripts/import_prior_run.sh --dry-run          # 打印计划（拷贝清单 + 落盘 state）并自校验
#   scripts/import_prior_run.sh                    # 真导入（写 runs/m3r2/）
#   scripts/import_prior_run.sh --verify           # 只读校验已导入的 run（幂等，可反复跑）
#
# 环境（与正式实验逐字同源，config_hash 必须与起跑一致，否则导入的东西口径不同）：
#   DICERATCHET_EXPERIMENT__ARM=C1
#   DICERATCHET_INFERENCE__MODE=eval
#   DICERATCHET_INFERENCE__SEED=0
#   DICERATCHET_EVOLVE__HITL=false
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY=".venv/bin/python"
FROM_RUN="${M3R2_FROM_RUN:-m3-formal}"
TO_RUN="${M3R2_RUN_ID:-m3r2}"
GENERATIONS="${M3R2_IMPORT_GENERATIONS:-1}"   # 服务代数：1 ⇒ cycle 0 的 30 局（seed 1000–1029）

export DICERATCHET_EXPERIMENT__ARM=C1
export DICERATCHET_INFERENCE__MODE=eval
export DICERATCHET_INFERENCE__SEED=0
export DICERATCHET_EVOLVE__HITL=false

MODE="${1:-apply}"

exec "$PY" - "$FROM_RUN" "$TO_RUN" "$GENERATIONS" "$MODE" <<'PYEOF'
import sys
from pathlib import Path

from dice_ratchet.infra.config import load_config
from dice_ratchet.loop.import_prior import apply_import, plan_import

from_run, to_run, generations_raw, mode = sys.argv[1:5]
generations = [int(g) for g in generations_raw.split(",") if g.strip() != ""]

runs_root = Path("runs")
source_root = runs_root / from_run
target_root = runs_root / to_run
cfg = load_config(config_dir=Path("configs"))
app = getattr(cfg, "app", cfg)                       # 兼容两种 AppConfig 形状
k = int(getattr(app.loop, "k", 30))

plan = plan_import(source_root=source_root, target_root=target_root,
                   generations=generations, k=k,
                   eval_episodes_cap=int(getattr(app.loop, "max_episodes_per_generation", 120)),
                   harness_version=0, strict_target=(mode != "--verify"))
import json
print("=== 导入计划（只读旧 run）===")
print(f"  源：{source_root}   →   目标：{target_root}")
for item in plan.generations:
    print(f"  gen-{item.generation:03d}（旧标签 g{item.legacy_generation:03d}）："
          f"{len(item.episodes)} 局轨迹 + {len(item.reflections)} 份反思 + "
          f"记忆快照 {item.memory_snapshot or '（缺，不导入）'}")
    print(f"        counters: {item.counters}")
print(f"  落盘 state：generation={plan.state['generation']} "
      f"completed_train_seeds={len(plan.state['completed_train_seeds'])} "
      f"last_completed_cycle={plan.state['last_completed_cycle']} "
      f"memory_version={plan.state['memory_version']} tick={plan.state['tick']}")
for warning in plan.warnings:
    print(f"  ⚠️  {warning}")

if mode == "--dry-run":
    print("=== dry-run：不写任何文件 ===")
    raise SystemExit(0)

if mode == "--verify":
    from dice_ratchet.loop.import_prior import verify_import
    result = verify_import(plan)
    print("=== 校验 ===")
    for line in result["checks"]:
        print(f"  {'OK  ' if line['ok'] else 'FAIL'} {line['name']}：{line['detail']}")
    raise SystemExit(0 if all(c["ok"] for c in result["checks"]) else 4)

report = apply_import(plan, memory_config=app.memory)
print(f"=== 已写入 {len(report.written)} 个文件 ===")
for path in report.written:
    print(f"  {path}")

from dice_ratchet.loop.import_prior import verify_import
result = verify_import(plan)
print("=== 自校验 ===")
for line in result["checks"]:
    print(f"  {'OK  ' if line['ok'] else 'FAIL'} {line['name']}：{line['detail']}")
raise SystemExit(0 if all(c["ok"] for c in result["checks"]) else 4)
PYEOF
