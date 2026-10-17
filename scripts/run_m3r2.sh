#!/usr/bin/env bash
# 新一轮正式实验（run-id=`m3r2`，产物目录 `runs/m3r2/`）：以**P1**（上一轮三代补丁的语义去重）
# 为起点 harness，复用 gen-000 的 30 局训练学习状态，跑 **1 个演化 cycle**（先看 P2）。
#
# 与上一轮 `scripts/run_m3_formal.sh` 的区别（一字一句刻意）：
#   · 起点 harness = **v0 + P1**（人工去重，见 docs/reviews/2026-10-17-M3-next-round-P1起点设计.md）：
#     `harness/prompts/decision.md` 的第 10–12 条。版本号仍是 0（P1 不是自动 accept ⇒
#     不建 `versions/v1/`、不打 tag `harness-v1`），play 面指纹与裸 v0 不同。
#   · **训练学习状态复用**：`scripts/import_prior_run.sh` 已把 `runs/m3-formal` 的 gen-000
#     训练块（seed 1000–1029 + 30 份反思 + `memory_v1.jsonl`）按 F64 口径重标后导进 `runs/m3r2/`。
#     因此本 run 的 cycle 0 训练**不再重跑**（省 30 局 ≈¥26），首个 cycle 直接由**导入状态**触发。
#   · cycle 数 = **1**（用户裁决：先看 P2；判据 1 要的"第二对"留给下一轮或后续 --resume）。
#   · 一步一停：主体跑完默认**不**自动消耗任何一次性资源。
#
# 一次性资源边界（硬约束，与上一轮同源）：
#   · **不碰 H2**（731040–731079 消融）与 **test**（947000–947099，每里程碑仅一次）。
#   · 起跑后断言 run 目录下没有 `test_attestation.json`。
#
# 预算 / 墙钟（沿用 M3 登记口径：≈¥0.879/局上界、4.8 h/代）：
#   · gen-000 起点测量（v0+P1 在 H1 上 40 局）≈ ¥21 / ≈1.5 h；
#   · cycle 1（30 训练 + 2+2 冒烟 + 40 候选 heldout = 74 局）≈ ¥65 / ≈3 h；
#   · **主体合计 114 局 ≈ ¥100 / ≈4.5 h**；H2 消融与 test **本轮不动**。
#
# 用法：
#   scripts/run_m3r2.sh --plan        # 免费：打印将执行的命令 + 预算 + 断言清单
#   scripts/run_m3r2.sh --preflight   # 免费：全部前置断言 + `loop run --dry-run` + `eval test --check`
#   scripts/run_m3r2.sh               # 真跑主体（花钱：≈¥100 / ≈4.5 h）
#   scripts/run_m3r2.sh --resume      # 中断后续跑（config_hash 与起跑逐字节同源）
#   scripts/run_m3r2.sh --status      # 免费：进度
#   scripts/run_m3r2.sh --cost        # 免费：累计 cost_estimate（高峰价上界）
#   scripts/run_m3r2.sh --post        # 免费：判据 / 报告 / 多样性 / leak 审计 / test 预检
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RUN_ID="${M3R2_RUN_ID:-m3r2}"
PY=".venv/bin/python"
HARNESS_DIR="harness"          # 生产生效目录（accept ⇒ git commit + tag）
MODE="${1:-start}"

GENERATIONS=3                  # = 配置默认值（不构成 config_hash override）
K=30
# **本轮只跑 CYCLES 个 cycle**（用户 2026-10-17 裁决：先看 P2）。
# 教训（2026-10-17 实测）：原先把 `TRAIN_SEEDS` 写死成 `train:1000-1089`（三代），而导入已覆盖
# 1000–1029 ⇒ 实际跑成了 3 个 cycle（262 局 / ¥241），与"1 个 cycle / ¥100"的裁决不符。
# 现在由 `CYCLES` **推出** seed 上界：总代数 = 已导入代数 + CYCLES。
CYCLES="${M3R2_CYCLES:-1}"
_imported_generations() {
  "$PY" - "runs/${RUN_ID}/state.json" <<'EOF' 2>/dev/null || echo 0
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
print(int(json.loads(p.read_text(encoding="utf-8")).get("generation") or 0) if p.is_file() else 0)
EOF
}
IMPORTED_GENS="$(_imported_generations)"
TOTAL_GENS=$(( IMPORTED_GENS + CYCLES ))
TRAIN_BASE="$("$PY" -c 'from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol, take; print(take("train", generation=1, protocol=validate_protocol(coerce_protocol(None)))[0])')"
TRAIN_LAST=$(( TRAIN_BASE + K * TOTAL_GENS - 1 ))
TRAIN_SEEDS="train:${TRAIN_BASE}-${TRAIN_LAST}"

# --- 环境：与上一轮正式实验逐字同源（4 个 run 级 override，其余走生产默认值）---------------
export DICERATCHET_EXPERIMENT__ARM=C1
export DICERATCHET_INFERENCE__MODE=eval
export DICERATCHET_INFERENCE__SEED=0
export DICERATCHET_EVOLVE__HITL=false
# ⚠️ 以下**故意不设**（设了就是另一套 config_hash、偏离口径）：
#   DICERATCHET_ENV__EPISODE__DIFFICULTY      （默认 hard）
#   DICERATCHET_EVOLVE__HELDOUT_BASKET        （默认 H1）
#   DICERATCHET_EVAL__N_HELDOUT / LOOP__HELDOUT_N（默认 40 / 40，必须相等）
#   DICERATCHET_INFRA__RUNS_ROOT              （默认 runs/；本轮的产物在 runs/m3r2/）

CMD=("$PY" -m dice_ratchet loop run
     --agent llm
     --no-hitl
     --k "$K" --generations "$GENERATIONS"
     --seeds "$TRAIN_SEEDS"
     --harness-dir "$HARNESS_DIR"
     --run-id "$RUN_ID")

_run() { caffeinate -i "$@"; }

_banner() {
  echo "=== ${RUN_ID} 新一轮正式实验（P1 起点 + 复用 gen-000 训练状态）==="
  echo "分支 / HEAD    : $(git rev-parse --abbrev-ref HEAD) / $(git rev-parse --short HEAD)"
  echo "臂 / 局型      : C1 / hard × 6"
  echo "起点 harness   : v0 + P1（人工去重；version 仍为 0，play 面 ≠ 裸 v0）"
  echo "held-out 篮子  : H1 731000–731039（40 局，代际裁判；与上一轮同一把尺子）"
  echo "代数 / K       : max_generations=${GENERATIONS}；本轮 ${CYCLES} 个 cycle（已导入 ${IMPORTED_GENS} 代）/ K=${K}"
  echo "训练 seed      : ${TRAIN_SEEDS}（${TRAIN_BASE}–$(( TRAIN_BASE + K * IMPORTED_GENS - 1 )) 由导入提供，其后待跑）"
  echo "HITL           : off（decided_by=auto；报告须写明「本轮无人工确认」）"
  echo "harness 目录   : ${HARNESS_DIR}（accept ⇒ git commit + tag harness-v{N}）"
  echo "一次性资源     : H2 / test **本轮不消耗**"
  echo "预算           : 主体 114 局 ≈¥100（高峰价上界）/ ≈4.5 h"
  echo
}

# --- 前置断言（fail-closed；任何一条不过就 *不花钱* 地退出）---------------------------
preflight() {
  local fail=0
  _check() { local desc="$1"; shift; if "$@" >/dev/null 2>&1; then echo "  OK   ${desc}"; else echo "  FAIL ${desc}" >&2; fail=1; fi; }

  echo "--- 前置断言（免费）---"
  # 1) 篮子 / 局数 / 局型 / 反思 / HITL：与上一轮同源的口径断言
  if [[ "${DICERATCHET_EVOLVE__HELDOUT_BASKET:-H1}" != "H1" ]]; then
    echo "  FAIL held-out 篮子必须是 H1（当前 ${DICERATCHET_EVOLVE__HELDOUT_BASKET}）" >&2; fail=1
  else echo "  OK   held-out 篮子 = H1（默认，未被 override）"; fi
  local nh="${DICERATCHET_EVAL__N_HELDOUT:-40}" ln="${DICERATCHET_LOOP__HELDOUT_N:-40}"
  if [[ "$nh" != "40" || "$ln" != "40" ]]; then
    echo "  FAIL heldout 局数必须是 40/40（当前 eval=${nh} loop=${ln}）" >&2; fail=1
  else echo "  OK   heldout 局数 = 40 / 40"; fi
  if [[ "${DICERATCHET_ENV__EPISODE__DIFFICULTY:-hard}" != "hard" ]]; then
    echo "  FAIL 局型必须是 hard（当前 ${DICERATCHET_ENV__EPISODE__DIFFICULTY}）" >&2; fail=1
  else echo "  OK   局型 = hard（默认，未被 override）"; fi
  if printf '%s' "${CMD[*]}" | grep -q -- "--no-reflect"; then
    echo "  FAIL 正式实验不得关闭反思" >&2; fail=1
  else echo "  OK   反思开启（无 --no-reflect）"; fi
  if [[ "${DICERATCHET_EVOLVE__HITL:-}" != "false" ]]; then
    echo "  FAIL evolve.hitl 必须是 false（当前 ${DICERATCHET_EVOLVE__HITL:-<未设>}）" >&2; fail=1
  else echo "  OK   HITL = off（全自动）"; fi
  # 2) 工作区必须干净（F47-A 会在起跑处 E_IO_GIT_DIRTY 拦下）
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "  FAIL 工作区不干净（F47-A 会在起跑处拦下）" >&2; git status --short >&2; fail=1
  else echo "  OK   工作区干净"; fi
  # 3) API key 可用（只查来源，不打印 key）
  if "$PY" - <<'EOF' >/dev/null 2>&1
from pathlib import Path
from dice_ratchet.infra.config import load_config
from dice_ratchet.inference.secrets import env_file_path, lookup_api_key
cfg = load_config(config_dir=Path("configs"))
ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=env_file_path(cfg.inference))
raise SystemExit(0 if ref.present else 1)
EOF
  then echo "  OK   DEEPSEEK_API_KEY 可用"; else echo "  FAIL DEEPSEEK_API_KEY 不可用" >&2; fail=1; fi
  # 4) 起点 harness 必须是 P1（第 10–12 条在；裸 v0 会让整轮实验的起点失去意义）
  if grep -q "心数是稀缺资源" "${HARNESS_DIR}/prompts/decision.md" \
     && grep -q "先校验再行动" "${HARNESS_DIR}/prompts/decision.md"; then
    echo "  OK   起点 harness 含 P1（decision.md 第 10–12 条）"
  else
    echo "  FAIL 起点 harness 不含 P1 —— 先落 P1 再起跑" >&2; fail=1
  fi
  # 5) 导入状态就位（训练学习状态复用；缺了就会重跑 30 局训练 ≈¥26）
  if [[ -f "runs/${RUN_ID}/import_prior.json" && -f "runs/${RUN_ID}/state.json" ]]; then
    if "$PY" - "$RUN_ID" <<'EOF' >/dev/null 2>&1
import json, sys
from pathlib import Path
run = sys.argv[1]
state = json.load(open(Path("runs") / run / "state.json", encoding="utf-8"))
ok = (len(state.get("completed_train_seeds") or []) == 30
      and not (state.get("pending_queue") or [])
      and int(state.get("memory_version") or 0) == 1
      and int(state.get("generation") or 0) == 1)
raise SystemExit(0 if ok else 1)
EOF
    then echo "  OK   导入状态就位（30 局训练 + memory_v1 + generation=1）"
    else echo "  FAIL runs/${RUN_ID}/state.json 不是预期的导入状态" >&2; fail=1; fi
  else
    echo "  FAIL 缺 runs/${RUN_ID}/{state.json,import_prior.json} —— 先跑 scripts/import_prior_run.sh" >&2; fail=1
  fi
  # 6) gen-000 起点测量尚未做过（真跑路径；--resume 才允许已存在）
  if [[ "$MODE" != "--resume" && -e "runs/${RUN_ID}/generations/gen-000/report.json" ]]; then
    echo "  FAIL runs/${RUN_ID} 已有 gen-000 报告 —— 用 --resume，或换 M3R2_RUN_ID" >&2; fail=1
  else echo "  OK   gen-000 起点测量状态符合本模式"; fi
  # 7) test 解锁未被消耗过
  if find "runs/${RUN_ID}" -name test_attestation.json 2>/dev/null | grep -q .; then
    echo "  FAIL 本 run 已有 test_attestation.json（test 是一次性资源）" >&2; fail=1
  else echo "  OK   本 run 无 test 存证"; fi
  # 8) **范围断言**：`--seeds` 实际解析出的 seed 数必须**恰好**等于
  #    「已导入代数 + CYCLES」× K（2026-10-17 实测教训：多给一块 seed 就多跑一个 cycle，
  #    每块 ≈¥65 / ≈3 h —— 本轮因此从裁决的 1 个 cycle 跑成了 3 个）。
  local resolved
  if resolved="$("$PY" - "$TRAIN_SEEDS" "$K" "$TOTAL_GENS" <<'EOF'
import sys
from dice_ratchet.infra import cli as infra_cli
spec, k, total = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
parsed = infra_cli.parse_seeds_spec(spec)
seeds = sorted({int(s) for values in parsed.values() for s in values})
want = k * total
print(f"{len(seeds)}/{want}")
raise SystemExit(0 if len(seeds) == want else 1)
EOF
)"; then
    echo "  OK   范围 = 已导入 ${IMPORTED_GENS} 代 + ${CYCLES} 个 cycle（解析出 ${resolved} 个训练 seed）"
  else
    echo "  FAIL --seeds 解析出的 seed 数与「已导入 ${IMPORTED_GENS} + ${CYCLES}」个块不符（${resolved:-解析失败}）" >&2
    fail=1
  fi
  # 9) 成本闸门：E_gen 不超 cap（FR-06-13）
  if "$PY" - <<'EOF' >/dev/null 2>&1
from pathlib import Path
from dice_ratchet.infra.config import load_config
from dice_ratchet.loop.generation import episodes_per_generation
cfg = load_config(config_dir=Path("configs"))
e = episodes_per_generation(cfg.loop)
raise SystemExit(0 if e <= int(cfg.loop.max_episodes_per_generation) else 1)
EOF
  then echo "  OK   E_gen ≤ max_episodes_per_generation"
  else echo "  FAIL E_gen 超上限（FR-06-13 会拒绝启动该代）" >&2; fail=1; fi

  if [[ "$fail" == "0" ]]; then echo "--- 断言全过 ---"; return 0; fi
  echo "--- 断言未过：**不起跑**（退出码 4）---" >&2
  return 4
}

case "$MODE" in
  --plan)
    _banner
    echo "将执行："; printf '  %q' "${CMD[@]}"; echo; echo
    echo "随后（免费，post）："
    echo "  $PY -m dice_ratchet eval acceptance --run-id $RUN_ID --sensitivity"
    echo "  $PY -m dice_ratchet eval report --run-id $RUN_ID --split heldout --baseline all"
    echo "  $PY -m dice_ratchet eval report --run-id $RUN_ID --split train"
    echo "  $PY -m dice_ratchet eval diversity --run-id $RUN_ID"
    echo "  $PY -m dice_ratchet eval test --run-id $RUN_ID --check"
    echo "  $PY -m dice_ratchet audit leak --run-id $RUN_ID --split train"
    exit 0
    ;;
  --preflight)
    _banner
    preflight || exit 4
    echo
    echo "--- 导入状态复核（只读）---"
    bash scripts/import_prior_run.sh --verify | tail -12
    echo
    echo "--- loop run --dry-run（免费；只装配配置，不跑局）---"
    "$PY" -m dice_ratchet loop run --agent llm --no-hitl --k "$K" --generations "$GENERATIONS" \
        --seeds "$TRAIN_SEEDS" --harness-dir "$HARNESS_DIR" --run-id "$RUN_ID" --dry-run \
      | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("  config_hash =", d.get("config_hash")); print("  would       =", json.dumps(d.get("would"), ensure_ascii=False))'
    echo
    echo "--- test 终检只读预检（不消耗解锁）---"
    "$PY" -m dice_ratchet eval test --run-id "$RUN_ID" --check || true
    exit 0
    ;;
  --status) "$PY" -m dice_ratchet loop status --run-id "$RUN_ID"; exit 0 ;;
  --cost)
    "$PY" - "$RUN_ID" <<'EOF'
import glob, json, os, sys
run = sys.argv[1]
files = sorted(glob.glob(os.path.join("runs", run, "llm_api", "*.json")))
cost = 0.0
for p in files:
    try:
        with open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        continue
    cost += float((rec.get("cost_estimate") or {}).get("amount") or 0.0)
print(f"run={run} api_calls={len(files)} cost_estimate_sum=¥{cost:.2f}（高峰价上界；空闲档约减半）")
EOF
    exit 0
    ;;
  --post)
    echo "=== ${RUN_ID} 后置（免费）==="
    echo "--- 判据 1 判定器 ---"; "$PY" -m dice_ratchet eval acceptance --run-id "$RUN_ID" --sensitivity || true
    echo "--- 代际报告（heldout，parent 臂 / F57 口径）---"; "$PY" -m dice_ratchet eval report --run-id "$RUN_ID" --split heldout --baseline all || true
    echo "--- 代际报告（train，只读）---"; "$PY" -m dice_ratchet eval report --run-id "$RUN_ID" --split train || true
    echo "--- 多样性 / 坍缩 ---"; "$PY" -m dice_ratchet eval diversity --run-id "$RUN_ID" || true
    echo "--- test 只读预检 ---"; "$PY" -m dice_ratchet eval test --run-id "$RUN_ID" --check || true
    echo "--- leak 审计（train）---"; "$PY" -m dice_ratchet audit leak --run-id "$RUN_ID" --split train || true
    echo "--- 成本 ---"; "$0" --cost
    exit 0
    ;;
  --resume)
    _banner
    preflight || exit 4
    echo "=== 续跑（caffeinate -i）==="
    _run "$PY" -m dice_ratchet loop run --agent llm --no-hitl --k "$K" --generations "$GENERATIONS" \
         --seeds "$TRAIN_SEEDS" --harness-dir "$HARNESS_DIR" --run-id "$RUN_ID" --resume
    exit 0
    ;;
  start)
    _banner
    preflight || exit 4
    echo "=== 起跑（caffeinate -i；长跑防休眠 F19）==="
    _run "${CMD[@]}"
    echo
    echo "=== 主体完成 —— 运行 --post 取判据（免费）==="
    "$0" --post
    if find "runs/${RUN_ID}/generations" -name test_attestation.json 2>/dev/null | grep -q .; then
      echo "❌ 主体消耗了 test 一次性资源" >&2; exit 1
    fi
    exit 0
    ;;
  *) echo "未知模式：$MODE（见脚本头部的用法）" >&2; exit 2 ;;
esac
