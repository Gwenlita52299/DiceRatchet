#!/usr/bin/env bash
# M3 起跑前标定数据（D-M3-5 的 §3.3/§3.4 落地）——**零成本优先**。
#
# 三件事，成本从 0 开始单调递增：
#   ① 零成本：C5 三个 `tol` 的**最小可判效应（MDE）换算**（只用分母与粒度，不跑批）；
#   ② 零成本：`heuristic` 基线在 H3 上的**代际抖动参照**（启发式是秒级）；
#   ③ 付费：`fixed_prompt` 基线在 H3 上（40 局 ≈1.7 h / ≈¥35）——**需用户点头**。
#
# 为什么需要 ②③ 而不是只做 ①：`c2_ratio_threshold` 的定义是「相对**第 0 代**的比例」，
# 没有基线的**代际**数据就结构上标不了；而 `c1` 是局内口径、`c3` 刚修好（见 Spec v2.14）、
# `c4` 用 `distinct_sequence_ratio`。⇒ 标定必须有 ≥2 代的实测。
#
# ⚠️ 一次性资源隔离：本脚本**只用 H3 731080–731099**，不碰 H1/H2/test。
#
# 用法：
#   scripts/run_m3_calibration.sh --free      # 只做 ①②（¥0）
#   scripts/run_m3_calibration.sh             # 含 ③（≈¥35，需点头）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RUN_ID="${CALIB_RUN_ID:-m3-calibration}"
PY=".venv/bin/python"
FREE_ONLY=0
[[ "${1:-}" == "--free" ]] && FREE_ONLY=1

export DICERATCHET_INFRA__RUNS_ROOT=runs/_sweep
export DICERATCHET_EXPERIMENT__ARM=C1
export DICERATCHET_ENV__EPISODE__DIFFICULTY=hard
export DICERATCHET_INFERENCE__MODE=eval
export DICERATCHET_INFERENCE__SEED=0
# heldout 缩到 H3 的 20 局（正式实验用 H1 的 40 局）
export DICERATCHET_EVAL__N_HELDOUT=20

echo "=== M3 标定数据（run-id=${RUN_ID}）==="
echo "heldout: H3 731080–731099（20 局）——**不用 H1/H2/test**"
echo

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区不干净（F47-A 会拦）。先 commit 或 stash。" >&2
  exit 4
fi

# --- ① 零成本：tol 的 MDE 换算 -----------------------------------------------------
echo "--- ① C5 tol 的最小可判效应（MDE）换算（¥0）---"
"$PY" - "$RUN_ID" <<'PY'
import json, sys
from pathlib import Path
from dice_ratchet.evals.tol_sensitivity import tolerance_sensitivity
s = tolerance_sensitivity(rates={"illegal": 0.0318, "fallback": 0.0017, "truncation": 0.30})
for a in s.axes:
    print(f"  {a.axis:11s} {a.reading()}")
print()
print("  ⇒ 结论：truncation 是**约束轴**（tol 只相当于 2 局）；")
print("     illegal / fallback 是**防真·退化**（tol 相当于上千个步级事件）。三轴不可互换。")
out = Path("runs/_sweep") / f"{sys.argv[1]}-tol-sensitivity.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(s.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  已落盘：{out}")
PY

# --- ② 零成本：heuristic 基线的代际抖动参照 ----------------------------------------
echo
echo "--- ② heuristic 基线在 H3 上（秒级，¥0）---"
echo "    用途：给出「同口径下一个已知强度策略的代际抖动」量级参照。"
echo "    ⚠️ 边界：启发式的**绝对熵**与 LLM 不同源 ⇒ 只用它定抖动**量级**，不用于定绝对值。"
"$PY" -m dice_ratchet baseline --kind heuristic --split heldout --basket H3 \
  --run-id "$RUN_ID" || echo "  （heuristic 基线已存在或失败——不阻塞）"

if [[ "$FREE_ONLY" == "1" ]]; then
  echo
  echo "（--free：跳过 ③ 付费项。C5 的 tol 与 c1–c4 的阈值仍按「起点值 + 待回填」处理。）"
  exit 0
fi

# --- ③ 付费：fixed_prompt 基线 ------------------------------------------------------
echo
echo "--- ③ fixed_prompt 基线在 H3 上（40 局 ≈1.7 h / ≈¥35）---"
echo "    ⚠️ 这一步**花钱**。没有用户明确点头就退出。"
if [[ "${CALIB_CONFIRM_PAID:-}" != "yes" ]]; then
  echo "    未设置 CALIB_CONFIRM_PAID=yes ⇒ 跳过（避免误烧钱）。" >&2
  exit 0
fi
"$PY" -m dice_ratchet baseline --kind fixed_prompt --split heldout --basket H3 \
  --run-id "$RUN_ID"

echo
echo "=== 标定数据已采集；把结果按轴回填进 Spec §8.6 与 MOD-07 §8.2.1 ==="
echo "（回填前不得把任何阈值描述成「已标定」）"
