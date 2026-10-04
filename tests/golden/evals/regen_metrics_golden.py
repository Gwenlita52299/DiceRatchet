"""重新生成 MOD-08 golden 指标快照（T-13）。

用法::

    python tests/golden/evals/regen_metrics_golden.py

改动 ``tests/fixtures/evals/episodes/`` 或指标口径后运行，并**审阅 diff** 再提交
（G2：golden 改动必须显式更新并在 PR 说明）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from gamer_rsi.evals.harness import load_episode_results  # noqa: E402
from gamer_rsi.evals.metrics import aggregate  # noqa: E402

FIXTURES = _REPO / "tests" / "fixtures" / "evals"
GOLDEN = _REPO / "tests" / "golden" / "evals" / "split_metrics.json"


def main() -> int:
    results = load_episode_results(FIXTURES, "heldout", harness_version=0)
    metrics = aggregate(results, split="heldout", basket="H1", t_max=120, min_support=3)
    payload = {"split": "heldout", "basket": "H1", "n_fixture_episodes": len(results),
               "metrics": metrics.as_dict()}
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"wrote {GOLDEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
