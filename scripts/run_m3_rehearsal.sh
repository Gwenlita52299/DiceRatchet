#!/usr/bin/env bash
# M3 彩排（D-M3-5 裁决 A）——**不碰 H1 / H2 / test**，用 train 窗口 + H3。
#
# 要回答的问题：M3 正式实验起跑前，**全链路**能不能走通，并且留下可机读的证据。
# 与「干跑链」（M2 检查单 §11.2）的分工：
#   · 干跑链是**开发期**工具（`--agent heuristic` ⇒ 84 局全免费），用来逼出缺陷；
#   · 本脚本是**起跑前**的彩排：把真实代码路径（`evaluate_candidate` → 多样性 → 消融
#     → 绘图 → 判据判定器）**按 M3 的形态**跑一遍，产出的证据交给 `rehearsal_check`。
#
# 一次性资源隔离（本脚本的硬约束）：
#   · H1 (731000–731039)、H2 (731040–731079)、test (947000–947099) **一律不出现**；
#   · heldout 只用 **H3 731080–731099**（role: reserve，协议未 locked ⇒ 可重复用）；
#   · 脚本结尾断言本次 run 目录下**没有** test_attestation.json。
#   `tests/unit/evals/test_evals_rehearsal.py` 另有代码级断言守住同一件事。
#
# 规模：2 代（1 代时判据 1 判定器 `comparable=0` ⇒ 验不到「判定器能给出结论」这件事）。
# 成本：H3 20 局/臂 ⇒ 每代约 54 局；2 代 ≈2 h / ≈¥40（`C1` hard×6 单价，Spec §8.6）。
#   **付费跑批必须先经用户点头。**
#
# 用法：
#   scripts/run_m3_rehearsal.sh              # 真跑（花钱）
#   scripts/run_m3_rehearsal.sh --plan       # 只打印将执行的命令与成本，不跑（免费）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RUN_ID="${REHEARSAL_RUN_ID:-m3-rehearsal}"
HARNESS_DIR="runs/_sweep/${RUN_ID}-harness"
PY=".venv/bin/python"
PLAN_ONLY=0
[[ "${1:-}" == "--plan" ]] && PLAN_ONLY=1

# --- 环境（与 M2 干跑链同源，只改必要项）-------------------------------------------
# RUNS_ROOT 指到 runs/_sweep（runs/ 已被 .gitignore 忽略，_sweep 是约定的草稿区）
# ARM=C1 保持与正式实验同臂；DIFFICULTY=hard 与 D-M3-1 裁决一致（**不再用 medium bring-up**）
# HITL=true：accept 挂在人工裁决前 ⇒ 不提交 harness 版本、不动 git（彩排不该改仓库状态）
export DICERATCHET_INFRA__RUNS_ROOT=runs/_sweep
export DICERATCHET_EXPERIMENT__ARM=C1
export DICERATCHET_ENV__EPISODE__DIFFICULTY=hard
export DICERATCHET_INFERENCE__SEED=0
export DICERATCHET_INFERENCE__MODE=eval
export DICERATCHET_EVOLVE__HITL=true
# 彩排把 heldout 缩到 H3 的 20 局（正式实验用 H1 的 40 局）
export DICERATCHET_EVAL__N_HELDOUT=20

GENERATIONS=2
K=30
TRAIN_SEEDS="train:1000-1029"     # 第 1 代的 30 局训练窗口（K=30）

echo "=== M3 彩排计划 ==="
echo "run-id        : $RUN_ID"
echo "runs root     : $DICERATCHET_INFRA__RUNS_ROOT"
echo "harness 副本  : $HARNESS_DIR"
echo "代数 / K      : $GENERATIONS / $K"
echo "train seeds   : $TRAIN_SEEDS"
echo "heldout       : H3 731080–731099（20 局）——**不用 H1/H2**"
echo "difficulty    : ${DICERATCHET_ENV__EPISODE__DIFFICULTY}（D-M3-1 裁决：hard×6）"
echo "估算          : ≈2 h / ≈¥40（付费需用户点头）"
echo

CMD=("$PY" -m dice_ratchet loop run
     --agent llm --no-reflect
     --k "$K" --generations "$GENERATIONS"
     --seeds "$TRAIN_SEEDS"
     --harness-dir "$HARNESS_DIR"
     --run-id "$RUN_ID")

if [[ "$PLAN_ONLY" == "1" ]]; then
  echo "将执行："
  printf '  %q' "${CMD[@]}"; echo
  echo
  echo "随后执行："
  echo "  $PY -m dice_ratchet eval ablation --run-id $RUN_ID --basket H3 --arms no_reflection,no_self_edit"
  echo "  $PY -m dice_ratchet eval acceptance --run-id $RUN_ID"
  echo "  $PY -m dice_ratchet eval test --run-id $RUN_ID --check          # 只读预检，不消耗解锁"
  echo "  $PY -m dice_ratchet rehearsal check --run-id $RUN_ID            # 彩排合格判据"
  exit 0
fi

# 起跑前：工作区必须干净（F47-A 的起跑门禁也会拦，这里提前给可读的提示）
if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区不干净（F47-A 会在起跑处 E_IO_GIT_DIRTY 拦下）。先 commit 或 stash。" >&2
  git status --short >&2
  exit 4
fi

mkdir -p "$HARNESS_DIR"
if [[ ! -f "$HARNESS_DIR/version.json" ]]; then
  cp -R harness/. "$HARNESS_DIR/"
  echo "已复制 harness → ${HARNESS_DIR}（真实 harness/ 零风险）"
fi

echo "=== 起跑（caffeinate -i，长跑防休眠 F19）==="
caffeinate -i "${CMD[@]}"

echo
echo "=== 彩排后置检查 ==="
"$PY" -m dice_ratchet eval acceptance --run-id "$RUN_ID" || true
"$PY" -m dice_ratchet eval test --run-id "$RUN_ID" --check || true
"$PY" -m dice_ratchet rehearsal check --run-id "$RUN_ID"

# --- 一次性资源隔离的最后一道断言（脚本侧）-----------------------------------------
ATT="$(find "runs/_sweep/$RUN_ID/generations" -name test_attestation.json 2>/dev/null | head -1)"
if [[ -n "$ATT" ]]; then
  echo "❌ 彩排消耗了 test 一次性资源：$ATT" >&2
  exit 1
fi
echo "✔ 未发现 test_attestation.json —— 一次性资源未被消耗"
