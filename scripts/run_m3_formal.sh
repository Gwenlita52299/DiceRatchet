#!/usr/bin/env bash
# M3 正式实验起跑（D-M3-1..D-M3-6 裁决后的**生产口径**）。
#
# 与彩排（`scripts/run_m3_rehearsal.sh`）的区别 —— 一字一句都是刻意的：
#   · 篮子 = **H1**（731000–731039，`generation_judge`，40 局）＝ 判据 1 的裁判篮子，
#     不是彩排的 H3（`reserve` 20 局）。`evolve.heldout_basket` **不设**（默认即 H1）。
#   · 局型 = **hard × 6**（D-M3-1 裁决 A 的主口径）；`env.episode.difficulty` **不设**（默认 hard）。
#   · 代数 = **3**（判据 1 要求 ≥3 轮演化）；K = 30/代，train seed = `train:1000-1089`。
#   · HITL = **off**（D-M3-4 裁决 A：全自动，`decided_by="auto"`，报告须写明「本轮无人工确认」）。
#   · harness 目录 = **`harness/`（真实生效目录）**：彩排用 `runs/_sweep/<run>-harness` 副本 +
#     HITL 挂起，所以从不改仓库；正式实验 accept 时会经 MOD-09 `git_ops` **commit + tag
#     `harness-v{N}`**（这是 G6 要求的证据链），因此起跑处必须工作区干净（F47-A 门禁同源）。
#   · 反思（MOD-05）**开启**（彩排用了 `--no-reflect`，正式实验不得用 —— 它会让
#     `no_reflection` 消融失去对照）。
#
# 一次性资源边界（本脚本的硬约束）：
#   · **不碰 H2**（731040–731079，消融终检）与 **test**（947000–947099，一次性）
#     —— 它们是独立步骤（`--ablation` / `--test-unlock`，各自还要显式确认开关）。
#   · 起跑后断言 run 目录下没有 `test_attestation.json`。
#
# 预算 / 限流 / 墙钟（D-M3-6，2026-10-13 按 `C1`·hard×6 实测单价 ¥0.833/局 重算【彩排口径】，
# 并用 M2/M3 登记的 ¥0.879/局作上界；单价含**高峰档**，空闲档减半）：
#   · 主体（gen-000 基线 40 局 + 3 代 × E_gen 114 局 = 382 局）≈ ¥318–336，墙钟 ≈ 16 h；
#   · 消融 H2（`full`+`no_reflection`+`no_self_edit`，3 × 40 局 = 120 局）≈ ¥100，≈ 5 h；
#   · test 终检（100 局 + 迁移指标）≈ ¥83–88，≈ 4 h；
#   · 三基线（`random`/`heuristic` 本地免费；`fixed_prompt` H1 40 局）≈ ¥35；
#   · **合计 ≈ ¥540（高峰价上界，实测大概率更低）**；主体单独 ≤ ¥340。
#   · 单代墙钟上限 **6 h**（实测 4.8 h）：超时先 `--status` 看 phase，F42-A 的按臂归档使
#     中断最多损失「当前一臂」，`--resume` 不重付已完成局。
#   · 限流一律用默认（`inference.max_inflight=4` / `eval.n_workers=2` / `loop.train_concurrency=1`）：
#     彩排 12,397 次调用未触 429，**不改**（改了就换 config_hash = 换 run-id 族）。
#
# 用法：
#   scripts/run_m3_formal.sh --plan        # 免费：打印将执行的命令 + 预算 + 断言清单
#   scripts/run_m3_formal.sh --preflight   # 免费：全部前置断言 + `loop run --dry-run` + `eval test --check`
#   scripts/run_m3_formal.sh               # 真跑主体（花钱：≈¥340 / ≈16 h）
#   scripts/run_m3_formal.sh --resume      # 中断后续跑（config_hash 与起跑逐字节同源）
#   scripts/run_m3_formal.sh --status      # 免费：进度
#   scripts/run_m3_formal.sh --cost        # 免费：累计 cost_estimate（高峰价上界）
#   scripts/run_m3_formal.sh --post        # 免费：判据 1 判定器 / 报告 / 多样性 / leak 审计 / test 预检
#   M3_CONFIRM_H2=yes   scripts/run_m3_formal.sh --ablation     # 付费 + 一次性 H2
#   M3_CONFIRM_TEST=yes scripts/run_m3_formal.sh --test-unlock # 付费 + 一次性 test（每里程碑 1 次）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RUN_ID="${M3_FORMAL_RUN_ID:-m3-formal}"
PY=".venv/bin/python"
HARNESS_DIR="harness"          # 生产生效目录（accept ⇒ git commit + tag）
MODE="${1:-start}"

GENERATIONS=3
K=30
TRAIN_SEEDS="train:1000-1089"  # 3 代 × 30 局（1000-1029 / 1030-1059 / 1060-1089）

# --- 环境：正式实验口径 ------------------------------------------------------------
# 只有 4 个 run 级 override，其余**一律走生产默认值**（少一个 override = 少一处口径漂移）。
# 这 4 个与 M2 §3.2/§3.3 已验收的 `m2-s32b-smoke-c1-medium6` 同源（除 difficulty/basket，
# 那两项由 M3 的 D-M3-1/D-M3-5 改回 hard/H1）。
export DICERATCHET_EXPERIMENT__ARM=C1         # 主力臂（Spec v2.0 B-43）
export DICERATCHET_INFERENCE__MODE=eval       # 评估口径（与 M2 准出 run 同源）
export DICERATCHET_INFERENCE__SEED=0          # eval 模式要求显式 seed（B-14）
export DICERATCHET_EVOLVE__HITL=false         # D-M3-4 裁决 A：全自动
# ⚠️ 以下**故意不设**（设了就是另一套 config_hash、偏离 M3 口径）：
#   DICERATCHET_ENV__EPISODE__DIFFICULTY      （默认 hard = D-M3-1 主口径）
#   DICERATCHET_EVOLVE__HELDOUT_BASKET        （默认 H1 = 代际裁判篮子）
#   DICERATCHET_EVAL__N_HELDOUT / LOOP__HELDOUT_N（默认 40 / 40，必须相等）
#   DICERATCHET_INFRA__RUNS_ROOT              （默认 runs/ = 正式产物区）

CMD=("$PY" -m dice_ratchet loop run
     --agent llm
     --no-hitl
     --k "$K" --generations "$GENERATIONS"
     --seeds "$TRAIN_SEEDS"
     --harness-dir "$HARNESS_DIR"
     --run-id "$RUN_ID")

# --- 辅助 --------------------------------------------------------------------------
_run() { caffeinate -i "$@"; }

_banner() {
  echo "=== M3 正式实验（run-id=${RUN_ID}）==="
  echo "分支 / HEAD    : $(git rev-parse --abbrev-ref HEAD) / $(git rev-parse --short HEAD)"
  echo "臂 / 局型      : C1 / hard × 6（D-M3-1 主口径）"
  echo "held-out 篮子  : H1 731000–731039（40 局，代际裁判；**不是**彩排的 H3）"
  echo "代数 / K       : ${GENERATIONS} / ${K}    train seeds: ${TRAIN_SEEDS}"
  echo "HITL           : off（decided_by=auto；报告须写明「本轮无人工确认」）"
  echo "harness 目录   : ${HARNESS_DIR}（accept ⇒ git commit + tag harness-v{N}）"
  echo "一次性资源     : H2 / test **本轮不消耗**（另有 --ablation / --test-unlock 两步）"
  echo "预算           : 主体 ≈¥340（高峰价上界） / ≈16 h；全 M3 ≈¥540"
  echo "单代墙钟上限   : 6 h（实测 ≈4.8 h）；超时先 --status"
  echo
}

# --- 前置断言（fail-closed；任何一条不过就 *不花钱* 地退出）---------------------------
preflight() {
  local fail=0
  _check() { # _check <说明> <条件命令...>
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then echo "  OK   ${desc}"; else echo "  FAIL ${desc}" >&2; fail=1; fi
  }

  echo "--- 前置断言（免费）---"
  # 1) 篮子必须 H1：显式设成 H2/H3 会让「判据 1 的裁判篮子」变成别的池子（F54 的教训）
  if [[ "${DICERATCHET_EVOLVE__HELDOUT_BASKET:-H1}" != "H1" ]]; then
    echo "  FAIL held-out 篮子必须是 H1（当前 ${DICERATCHET_EVOLVE__HELDOUT_BASKET}）" >&2; fail=1
  else echo "  OK   held-out 篮子 = H1（默认，未被 override）"; fi
  # 2) heldout 局数必须 40（H1 全篮），且 loop/evals 两侧一致（F51 的死锁）
  local nh="${DICERATCHET_EVAL__N_HELDOUT:-40}" ln="${DICERATCHET_LOOP__HELDOUT_N:-40}"
  if [[ "$nh" != "40" || "$ln" != "40" ]]; then
    echo "  FAIL heldout 局数必须是 40/40（当前 eval=${nh} loop=${ln}）" >&2; fail=1
  else echo "  OK   heldout 局数 = 40 / 40"; fi
  # 3) 局型必须 hard（D-M3-1 主口径）
  if [[ "${DICERATCHET_ENV__EPISODE__DIFFICULTY:-hard}" != "hard" ]]; then
    echo "  FAIL 局型必须是 hard（当前 ${DICERATCHET_ENV__EPISODE__DIFFICULTY}）" >&2; fail=1
  else echo "  OK   局型 = hard（默认，未被 override）"; fi
  # 4) 反思必须开（正式实验不得用 --no-reflect）
  if printf '%s' "${CMD[*]}" | grep -q -- "--no-reflect"; then
    echo "  FAIL 正式实验不得关闭反思（--no-reflect 是消融臂）" >&2; fail=1
  else echo "  OK   反思开启（无 --no-reflect）"; fi
  # 5) HITL 必须 off
  if [[ "${DICERATCHET_EVOLVE__HITL:-}" != "false" ]]; then
    echo "  FAIL evolve.hitl 必须是 false（当前 ${DICERATCHET_EVOLVE__HITL:-<未设>}）" >&2; fail=1
  else echo "  OK   HITL = off（全自动）"; fi
  # 6) 工作区必须干净（F47-A 会在起跑处 E_IO_GIT_DIRTY 拦下；这里提前给可读提示）
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "  FAIL 工作区不干净（F47-A 会在起跑处拦下）" >&2; git status --short >&2; fail=1
  else echo "  OK   工作区干净"; fi
  # 7) API key 可用（只查来源，不打印 key）
  if "$PY" - <<'EOF' >/dev/null 2>&1
from pathlib import Path
from dice_ratchet.infra.config import load_config
from dice_ratchet.inference.secrets import env_file_path, lookup_api_key
cfg = load_config(config_dir=Path("configs"))
ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=env_file_path(cfg.inference))
raise SystemExit(0 if ref.present else 1)  # 只看「有没有」，不打印 key
EOF
  then echo "  OK   DEEPSEEK_API_KEY 可用"; else echo "  FAIL DEEPSEEK_API_KEY 不可用" >&2; fail=1; fi
  # 8) run 目录不得已存在（真跑路径；--resume 才允许存在）
  if [[ "$MODE" != "--resume" && -e "runs/${RUN_ID}" ]]; then
    echo "  FAIL runs/${RUN_ID} 已存在 —— 用 --resume，或换 M3_FORMAL_RUN_ID" >&2; fail=1
  else echo "  OK   runs/${RUN_ID} 状态符合本模式"; fi
  # 9) test 解锁未被消耗过（本 run 内）
  if find "runs/${RUN_ID}" -name test_attestation.json 2>/dev/null | grep -q .; then
    echo "  FAIL 本 run 已有 test_attestation.json（test 是一次性资源）" >&2; fail=1
  else echo "  OK   本 run 无 test 存证"; fi

  if [[ "$fail" == "0" ]]; then echo "--- 断言全过 ---"; return 0; fi
  echo "--- 断言未过：**不起跑**（退出码 4）---" >&2
  return 4
}

# --- 模式 --------------------------------------------------------------------------
case "$MODE" in
  --plan)
    _banner
    echo "将执行："; printf '  %q' "${CMD[@]}"; echo; echo
    echo "随后（免费，post）："
    echo "  $PY -m dice_ratchet eval acceptance --run-id $RUN_ID --sensitivity"
    echo "  $PY -m dice_ratchet eval report --run-id $RUN_ID --split heldout --baseline all"
    echo "  $PY -m dice_ratchet eval diversity --run-id $RUN_ID"
    echo "  $PY -m dice_ratchet eval test --run-id $RUN_ID --check        # 只读预检，不消耗解锁"
    echo "  $PY -m dice_ratchet audit leak --run-id $RUN_ID --split train"
    echo "  $PY -m dice_ratchet baseline --kind random    --split heldout --basket H1 --run-id $RUN_ID   # ¥0"
    echo "  $PY -m dice_ratchet baseline --kind heuristic --split heldout --basket H1 --run-id $RUN_ID   # ¥0"
    echo
    echo "再随后（付费 + 一次性资源，需显式确认）："
    echo "  M3_CONFIRM_H2=yes   $0 --ablation     # H2 731040–731079 消融终检（≈¥100）"
    echo "  M3_CONFIRM_TEST=yes $0 --test-unlock  # test 947000–947099 一次性终检（≈¥88）"
    echo
    echo "可选（判据 2 的基线锚点，≈¥35）："
    echo "  $PY -m dice_ratchet baseline --kind fixed_prompt --split heldout --basket H1 --run-id $RUN_ID"
    exit 0
    ;;
  --preflight)
    _banner
    preflight || exit 4          # fail-closed：断言不过就**不花钱**地退出（F47-A 同源）
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
  --status)
    "$PY" -m dice_ratchet loop status --run-id "$RUN_ID"
    exit 0
    ;;
  --cost)
    "$PY" - "$RUN_ID" <<'EOF'
import glob, json, os, sys
run = sys.argv[1]
files = sorted(glob.glob(os.path.join("runs", run, "llm_api", "*.json")))
cost = 0.0; calls = len(files)
for p in files:
    try:
        with open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        continue
    cost += float((rec.get("cost_estimate") or {}).get("amount") or 0.0)
print(f"run={run} api_calls={calls} cost_estimate_sum=¥{cost:.2f}（高峰价上界；空闲档约减半）")
EOF
    exit 0
    ;;
  --ablation)
    if [[ "${M3_CONFIRM_H2:-}" != "yes" ]]; then
      echo "消融会消耗一次性 H2（731040–731079）：设 M3_CONFIRM_H2=yes 才执行。" >&2
      exit 4
    fi
    _run "$PY" -m dice_ratchet eval ablation --run-id "$RUN_ID" --basket H2 \
         --generation "$GENERATIONS" --arms no_reflection,no_self_edit
    exit 0
    ;;
  --test-unlock)
    if [[ "${M3_CONFIRM_TEST:-}" != "yes" ]]; then
      echo "test 是一次性资源（947000–947099）：设 M3_CONFIRM_TEST=yes 才执行。" >&2
      exit 4
    fi
    _run "$PY" -m dice_ratchet eval test --run-id "$RUN_ID" --generation "$GENERATIONS" --unlock-once
    exit 0
    ;;
  --post)
    echo "=== M3 正式实验后置（免费）==="
    echo "--- 判据 1 判定器 ---"; "$PY" -m dice_ratchet eval acceptance --run-id "$RUN_ID" --sensitivity || true
    echo "--- 代际报告（heldout，parent 臂 / F57 口径）---"; "$PY" -m dice_ratchet eval report --run-id "$RUN_ID" --split heldout --baseline all || true
    # F61/F71（2026-10-14）：`--split train` 现在能解析 `train` 臂且是**只读**的
    # （打印 + `metrics/gen-NNN.train.readonly.json`，不写 report.json / 台账 / 绘图）。
    # 因此这里**不再 `|| true`**（原先是「我的脚本吞掉了退出码」，见结果报告 §6 F61）。
    echo "--- 代际报告（train，只读）---"; "$PY" -m dice_ratchet eval report --run-id "$RUN_ID" --split train
    echo "--- 多样性 / 坍缩 ---"; "$PY" -m dice_ratchet eval diversity --run-id "$RUN_ID" || true
    echo "--- test 只读预检 ---"; "$PY" -m dice_ratchet eval test --run-id "$RUN_ID" --check || true
    echo "--- leak 审计（train）---"; "$PY" -m dice_ratchet audit leak --run-id "$RUN_ID" --split train || true
    echo "--- 成本 ---"; "$0" --cost
    echo
    echo "下一步（付费，需确认）："
    echo "  M3_CONFIRM_H2=yes   $0 --ablation"
    echo "  M3_CONFIRM_TEST=yes $0 --test-unlock"
    exit 0
    ;;
  --resume)
    _banner
    preflight || exit 4          # fail-closed：断言不过就**不花钱**地退出（F47-A 同源）
    echo "=== 续跑（caffeinate -i）==="
    _run "$PY" -m dice_ratchet loop run --agent llm --no-hitl --k "$K" --generations "$GENERATIONS" \
         --seeds "$TRAIN_SEEDS" --harness-dir "$HARNESS_DIR" --run-id "$RUN_ID" --resume
    exit 0
    ;;
  start)
    _banner
    preflight || exit 4          # fail-closed：断言不过就**不花钱**地退出（F47-A 同源）
    echo "=== 起跑（caffeinate -i；长跑防休眠 F19）==="
    _run "${CMD[@]}"
    echo
    echo "=== 主体完成 —— 运行 --post 取判据（免费）==="
    "$0" --post
    # 一次性资源隔离的最后一道断言（脚本侧）
    if find "runs/${RUN_ID}/generations" -name test_attestation.json 2>/dev/null | grep -q .; then
      echo "❌ 主体消耗了 test 一次性资源" >&2; exit 1
    fi
    if "$PY" - "$RUN_ID" <<'EOF'
import json, os, sys
run = sys.argv[1]
h2 = range(731040, 731080)
bad = []
for line in open(os.path.join("runs", run, "events.jsonl"), encoding="utf-8"):
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get("event") == "loop.episode_start" and int(e.get("seed", -1)) in h2:
        bad.append(int(e["seed"]))
raise SystemExit(1 if bad else 0)
EOF
    then echo "✔ 未消耗 test / H2 一次性资源"; else
      echo "❌ 主体出现了 H2 seed（731040–731079）轨迹" >&2; exit 1
    fi
    exit 0
    ;;
  *)
    echo "未知模式：$MODE（见脚本头部的用法）" >&2
    exit 2
    ;;
esac
