#!/usr/bin/env bash
# scripts/ci_gate.sh —— GameRSI 统一 CI 门禁（M0 验收 F2 的「CI 门禁接线」落点）
#
# 把 Spec §9 的 G1–G6 与 INV-1/INV-6 变成一条**可本地复跑、也可被 CI 调用**的命令：
#   G1 单测      全量 pytest
#   G2 Golden    tests/golden（含 5 组逐行对拍与 MOD-02 投影 golden）
#   G3 确定性    跨 PYTHONHASHSEED 复跑 golden + infra（MOD-01 PRD T-08 / MOD-09 PRD G3）
#   G4 泄漏审计  env selftest + audit 相关用例（含蜜罐 12 例自测）
#   G5 Schema    schema round-trip 用例
#   G6 命令      guards check / env selftest 原样执行
#   INV-1/6      guards check（AST 分层 + 确定性扫描），违例退出码 4
#
# 用法：
#   scripts/ci_gate.sh                 快速门禁（约 1 分钟）：guards + selftest + pytest -m "not slow"
#   scripts/ci_gate.sh --with-slow     追加深慢门：golden/200 局差分/200 局 bot 对拍 + GAME 侧 node 测试
#   scripts/ci_gate.sh --coverage      附带行/分支覆盖率报告（G1 判据）
#   scripts/ci_gate.sh --with-slow --coverage
#
# 环境变量：
#   PYTHON   指定解释器（默认优先 .venv/bin/python，其次 python3）
#   NODE     指定 node（默认 node）；GAME 侧测试需要 Node ≥ 21（`--test` 的 glob 支持）
#
# 退出码：0 全部通过；非 0 为第一个失败的门（CI 只看非 0 与日志）。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WITH_SLOW=0
COVERAGE=0
for arg in "$@"; do
  case "$arg" in
    --with-slow) WITH_SLOW=1 ;;
    --coverage) COVERAGE=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（见 $0 --help）" >&2; exit 2 ;;
  esac
done

# ---- 解释器 -----------------------------------------------------------------
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="python3"
fi
NODE_BIN="${NODE:-node}"

FAILED=0
step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
run() {
  local label="$1"; shift
  step "$label"
  if "$@"; then
    echo "✔ $label"
  else
    echo "✘ $label（退出码 $?）" >&2
    FAILED=1
  fi
}

echo "GameRSI CI 门禁：repo=$REPO_ROOT"
echo "  python = $PY ($($PY -V 2>&1))"
echo "  node   = $($NODE_BIN -v 2>&1 || echo '不可用')"
echo "  mode   = with_slow=$WITH_SLOW coverage=$COVERAGE"

# ---- G6 / INV-1 / INV-6：守卫门禁 -------------------------------------------
run "G6/INV-1/INV-6 guards check" "$PY" -m gamer_rsi guards check --root gamer_rsi
# 反例必须被拦下（fail-closed 自检：门禁不是空转）
step "门禁反例自检（必须失败）"
if "$PY" -m gamer_rsi guards check --root tests/fixtures/guards >/dev/null 2>&1; then
  echo "✘ 反例 fixture 竟通过：guards 门禁已失效" >&2
  FAILED=1
else
  echo "✔ 反例 fixture 被拦下（非 0 退出码）"
fi

# ---- G4 / G6：环境自检（含确定性双跑与 golden 烟雾对拍）--------------------
run "G4/G6 env selftest" "$PY" -m gamer_rsi env selftest --seed 1

# ---- G1–G5：测试套件 --------------------------------------------------------
PYTEST_ARGS=(-q)
if [[ "$COVERAGE" == "1" ]]; then
  PYTEST_ARGS+=(--cov=gamer_rsi --cov-report=term-missing:skip-covered)
fi
if [[ "$WITH_SLOW" == "0" ]]; then
  run "G1–G5 pytest（不含 slow）" "$PY" -m pytest "${PYTEST_ARGS[@]}" -m "not slow"
else
  run "G1–G5 pytest（全量，含 slow 差分对拍）" "$PY" -m pytest "${PYTEST_ARGS[@]}"
fi

# ---- G3：跨 PYTHONHASHSEED 复跑（T-08 / MOD-09 G3）--------------------------
run "G3 确定性（PYTHONHASHSEED=12345）" \
  env PYTHONHASHSEED=12345 "$PY" -m pytest -q -m "not slow" tests/golden tests/unit/infra

# ---- G2 / G6：GAME 侧 JS 回归（仅深门）--------------------------------------
if [[ "$WITH_SLOW" == "1" ]]; then
  run "G2/G6 GAME 侧 node 测试" \
    bash -c "cd '$REPO_ROOT/src/game' && '$NODE_BIN' --test 'tests/**/*.test.js'"

  # G2 前提守卫：Python 只读 golden，基准必须来自 JS。重生成后逐字节比对（Node 侧
  # golden-gen.js 会就地写 tests/golden/，因此失败即留下新快照，需审阅 git diff）。
  step "G2 golden 基准独立性（JS 重生成 == 仓库快照）"
  golden_tmp="$(mktemp -d)"
  cp -R "$REPO_ROOT/src/game/tests/golden/." "$golden_tmp/"
  if (cd "$REPO_ROOT/src/game" && "$NODE_BIN" scripts/golden-gen.js >/dev/null) \
     && diff -r -x __pycache__ -x sandbox "$golden_tmp" "$REPO_ROOT/src/game/tests/golden" >/dev/null; then
    echo "✔ JS 重生成的 golden 与仓库快照逐字节一致"
  else
    echo "✘ golden 漂移：src/game/tests/golden 已按 JS 重生成，请审阅 git diff 后显式提交" >&2
    FAILED=1
  fi
  rm -rf "$golden_tmp"
fi

echo
if [[ "$FAILED" == "0" ]]; then
  echo "✔ CI 门禁全部通过"
else
  echo "✘ CI 门禁存在失败项（见上方 ✘）" >&2
fi
exit "$FAILED"
