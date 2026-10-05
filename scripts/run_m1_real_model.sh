#!/usr/bin/env bash
# scripts/run_m1_real_model.sh —— M1 验收 §13.2 的真实模型跑批（可复制、可续跑）
#
# 覆盖 §13.2 的第 6 项（重跑并归档 n=40 的 fixed_prompt）与第 5 项（F21 的对照证据）。
#
# 跑批顺序（串行，避免两个 8B 实例同时占显存；24GB 机器上 MLX 一次只跑一个）：
#   1) fixed_v1 冻结模板冒烟 n=5        —— 若首答率异常，立刻 Ctrl-C，别开始长跑
#   2) random  基线 H1 全量 n=40（秒级）
#   3) heuristic 基线 H1 全量 n=40（秒级）
#   4) fixed_prompt 基线 H1 全量 n=40（真实 8B，**长跑**，本脚本的主体）
#   5) decision.md 主快照 robustness n=200（F13 重新生成快照后刷新判据 2 的证据）
#   6) fixed_v1 冻结模板 robustness n=200（F21 的对照：52% → ?）
#   7) audit leak / eval report（G4 与报告落盘）
#
# 用法：
#   caffeinate -i scripts/run_m1_real_model.sh 2>&1 | tee logs/m1-real-$(date -u +%Y%m%dT%H%M%SZ).log
#
# 断点续跑：第 4 步中断后，直接重跑
#   .venv/bin/python -m gamer_rsi baseline --kind fixed_prompt --split heldout \
#       --basket H1 --run-id m1-real-h1n40d --resume
# （逐局增量落盘见 F18；已完成的 seed 会跳过，未完成的接着跑。）
set -uo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
RUN="${RUN_ID:-m1-real-h1n40d}"
mkdir -p logs

say() { echo; echo "=== $(date -u +%FT%TZ) $* ==="; }

say "0/7 环境自检（权重 / mlx / caffeinate）"
ls models/Qwen3-8B-4bit >/dev/null || { echo "缺 models/Qwen3-8B-4bit"; exit 3; }
"$PY" -c "import mlx, mlx_lm; print('mlx ok', mlx_lm.__version__ if hasattr(mlx_lm,'__version__') else '')"
pmset -g | grep -i "sleep" | head -3 || true

say "1/7 冻结模板 fixed_v1 冒烟（n=5，eval 模式）"
"$PY" -m gamer_rsi inference robustness --model qwen3-8b-q4 --n 5 --mode eval --seed 7 \
    --prompts tests/fixtures/inference/robustness_prompts_fixed_v1.jsonl \
    --run-id m1-f21-smoke3

say "2/7 random 基线 H1（n=40）→ run=$RUN"
"$PY" -m gamer_rsi baseline --kind random --split heldout --basket H1 --run-id "$RUN"

say "3/7 heuristic 基线 H1（n=40）→ run=$RUN"
"$PY" -m gamer_rsi baseline --kind heuristic --split heldout --basket H1 --run-id "$RUN"

say "4/7 fixed_prompt 基线 H1（n=40，真实 8B，长跑）→ run=$RUN"
"$PY" -m gamer_rsi baseline --kind fixed_prompt --split heldout --basket H1 --run-id "$RUN"

say "5/7 decision.md 主快照 robustness（n=200，eval 模式；刷新判据 2 证据）"
"$PY" -m gamer_rsi inference robustness --model qwen3-8b-q4 --n 200 --mode eval --seed 7 \
    --run-id m1-rob-refresh3

say "6/7 fixed_v1 冻结模板 robustness（n=200，eval 模式；F21 对照）"
"$PY" -m gamer_rsi inference robustness --model qwen3-8b-q4 --n 200 --mode eval --seed 7 \
    --prompts tests/fixtures/inference/robustness_prompts_fixed_v1.jsonl \
    --run-id m1-f21-fixed3

say "7/7 泄漏审计 + 代际报告（G4 / 工件落盘）"
"$PY" -m gamer_rsi audit leak --run-id "$RUN"
"$PY" -m gamer_rsi eval report --split heldout --baseline all --run-id "$RUN"
cat "runs/$RUN/baselines/registry.csv"

say "完成：$(date -u +%FT%TZ)"
