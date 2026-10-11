#!/usr/bin/env bash
# 续跑 M3 彩排（`scripts/run_m3_rehearsal.sh` 的中断后续跑；2026-10-11 用户暂停在 gen-001 第 10 局）。
#
# **为什么需要单独一个脚本**：`loop run --resume` 走 `cli_init_run` 的 **config_hash 一致性校验**
# ——续跑时的配置必须与起跑时**逐字节同源**，否则被 `E_IO_RUN_CONFLICT` 拒（退出码 2）。
# 彩排的配置里有一串 run 级 override（difficulty=hard / n_heldout=20 / **heldout_basket=H3** /
# HITL=true …），手敲极易漏一个（漏了就得删 run 重跑，≈¥40 白花）。本脚本把它们固化成一份。
#
# **不变量**（改动任何一个都会换 `config_hash`，从而让续跑被拒）：
#   DICERATCHET_INFRA__RUNS_ROOT=runs/_sweep
#   DICERATCHET_EXPERIMENT__ARM=C1
#   DICERATCHET_ENV__EPISODE__DIFFICULTY=hard
#   DICERATCHET_INFERENCE__SEED=0
#   DICERATCHET_INFERENCE__MODE=eval
#   DICERATCHET_EVOLVE__HITL=true
#   DICERATCHET_EVAL__N_HELDOUT=20  +  DICERATCHET_LOOP__HELDOUT_N=20   （必须成对）
#   DICERATCHET_EVOLVE__HELDOUT_BASKET=H3                              （F54）
# 起跑时的 `config_hash = eaf6f18ce6e7865a`（2026-10-11 实测，见报告）。
#
# 用法：
#   scripts/resume_m3_rehearsal.sh --status     # 只看进度与 config_hash 是否还对得上（免费）
#   scripts/resume_m3_rehearsal.sh              # 真续跑（付费）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RUN_ID="${REHEARSAL_RUN_ID:-m3-rehearsal}"
HARNESS_DIR="runs/_sweep/${RUN_ID}-harness"
PY=".venv/bin/python"
STATUS_ONLY=0
[[ "${1:-}" == "--status" ]] && STATUS_ONLY=1

export DICERATCHET_INFRA__RUNS_ROOT=runs/_sweep
export DICERATCHET_EXPERIMENT__ARM=C1
export DICERATCHET_ENV__EPISODE__DIFFICULTY=hard
export DICERATCHET_INFERENCE__SEED=0
export DICERATCHET_INFERENCE__MODE=eval
export DICERATCHET_EVOLVE__HITL=true
export DICERATCHET_EVAL__N_HELDOUT=20
export DICERATCHET_LOOP__HELDOUT_N=20
# F54：彩排的 held-out 篮子必须是 H3（reserve）；改它 = 换 config_hash = 续跑被拒。
export DICERATCHET_EVOLVE__HELDOUT_BASKET=H3

echo "=== M3 彩排续跑（run-id=${RUN_ID}）==="
echo "held-out 篮子: ${DICERATCHET_EVOLVE__HELDOUT_BASKET}（H3 731080–731099，**不碰 H1/H2/test**）"
echo

if [[ "$STATUS_ONLY" == "1" ]]; then
  "$PY" -m dice_ratchet loop status --run-id "$RUN_ID"
  exit 0
fi

# 起跑前：工作区必须干净（F47-A 会在起跑处 E_IO_GIT_DIRTY 拦下；这里提前给可读提示）
if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区不干净（F47-A 会在起跑处拦下）。先 commit 或 stash。" >&2
  git status --short >&2
  exit 4
fi

# 与 run_m3_rehearsal.sh 同一组 loop run 参数（缺一个都会换 config_hash）
CMD=("$PY" -m dice_ratchet loop run
     --agent llm --no-reflect
     --k 30 --generations 2
     --seeds train:1000-1029
     --harness-dir "$HARNESS_DIR"
     --run-id "$RUN_ID"
     --resume)

echo "=== 续跑（caffeinate -i）==="
caffeinate -i "${CMD[@]}"

echo
echo "=== 彩排后置检查 ==="
"$PY" -m dice_ratchet eval acceptance --run-id "$RUN_ID" || true
"$PY" -m dice_ratchet eval test --run-id "$RUN_ID" --check || true
"$PY" -m dice_ratchet rehearsal check --run-id "$RUN_ID"

ATT="$(find "runs/_sweep/$RUN_ID/generations" -name test_attestation.json 2>/dev/null | head -1)"
if [[ -n "$ATT" ]]; then
  echo "❌ 彩排消耗了 test 一次性资源：$ATT" >&2
  exit 1
fi
echo "✔ 未发现 test_attestation.json —— 一次性资源未被消耗"
