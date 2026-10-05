#!/usr/bin/env bash
# 等待真实模型跑批结束并打印结果摘要（不干扰被观察的进程）。
#
# 用法：bash scripts/wait_m1_real_model.sh [logfile]
#   退出条件：log 里出现「完成：」或跑批进程消失；随后打印摘要与关键工件路径。
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="${1:-logs/m1-real-final2.log}"
RUN="${RUN_ID:-m1-real-h1n40d}"

while true; do
  if grep -q "^=== .*完成：" "$LOG" 2>/dev/null; then
    echo "[watch] 跑批正常结束"; break
  fi
  if ! pgrep -f "run_m1_real_model.sh" >/dev/null 2>&1; then
    echo "[watch] 跑批进程已不在（可能中断）"; break
  fi
  sleep 120
done

echo
echo "=== 最后 25 行日志 ==="
tail -25 "$LOG"
echo
echo "=== 基线台账 ==="
cat "runs/$RUN/baselines/registry.csv" 2>/dev/null || echo "(缺 registry.csv)"
echo
echo "=== 三条 held-out 的 n / win_rate ==="
"${PYTHON:-.venv/bin/python}" - "$RUN" <<'PY' 2>/dev/null || echo "(解析失败)"
import json, sys
from pathlib import Path
run = Path("runs") / sys.argv[1]
for path in sorted((run / "baselines").glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    m = data.get("metrics") or {}
    print(f"  {data.get('kind'):12s} n={m.get('n')} win_rate={m.get('win_rate')} "
          f"ci={m.get('win_rate_ci95')} degraded_n={m.get('degraded_n')} "
          f"model={data.get('model_hash')}/{data.get('model_backend')}")
PY
echo
echo "=== robustness 报告（如有）==="
for r in m1-rob-refresh3 m1-f21-fixed3 m1-f21-smoke3; do
  f="runs/$r/metrics/format_robustness.json"
  [ -f "$f" ] && "${PYTHON:-.venv/bin/python}" - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
o = d.get("overall") or {}
print(f"  {sys.argv[1]}: n={d.get('n_prompts')} mode={d.get('mode')} passed={d.get('passed')} "
      f"m1_evidence={d.get('m1_evidence')} first_pass={o.get('first_pass_valid_rate')} "
      f"retry={o.get('retry_histogram')} p50={ (o.get('latency_ms') or {}).get('p50') } "
      f"tpl={d.get('prompt_source')}")
PY
done
