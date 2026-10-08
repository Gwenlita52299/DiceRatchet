"""性能回归（NFR-01-2）：golden 全量步骤的 reduce p95 ≤ 2 ms。

跑 5 组 golden 的全部 reduce；用 ``@pytest.mark.slow`` 标记，CI 可选。
若 p95 超标，PRD §14 的对策是只对 ``state.player``/``enemies`` 做结构共享。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import pytest

from dice_ratchet.env import GameEngine, load_tables

_GOLDEN = Path(__file__).resolve().parents[2] / "src/game/tests/golden"
_BUDGET_US = 2000.0  # 2 ms


@pytest.fixture(scope="module")
def timings() -> list[float]:
    engine = GameEngine(load_tables())
    out: list[float] = []
    for name in ["g1_all_off_medium", "g2_all_on_medium", "g3_easy_full_run",
                 "g4_hard_corrupt_path", "g5_hard_six_stage"]:
        path = _GOLDEN / f"{name}.jsonl"
        if not path.exists():
            continue
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        state = engine.create(lines[0]["config"])
        for line in lines[1:]:
            if line.get("type") == "run_end":
                break
            t0 = time.perf_counter_ns()
            state = engine.reduce(state, line["action"])["state"]
            out.append((time.perf_counter_ns() - t0) / 1000.0)
    return out


@pytest.mark.slow
def test_reduce_p95_within_budget(timings) -> None:
    assert timings, "没有可重放的 golden 步骤"
    # coverage 插桩会让逐行 trace 放大一个量级，此时只报告不断言（NFR-01-2 针对生产模式）
    if sys.gettrace() is not None:
        p95 = statistics.quantiles(timings, n=20)[18]
        pytest.skip(f"coverage 插桩下不适用性能预算（p95={p95:.0f}us）")
    p95 = statistics.quantiles(timings, n=20)[18]
    p50 = statistics.quantiles(timings, n=20)[9]
    print(f"\nreduce: n={len(timings)} p50={p50:.0f}us p95={p95:.0f}us max={max(timings):.0f}us"
          f"（预算 p95 ≤ {_BUDGET_US:.0f}us）")
    assert p95 <= _BUDGET_US
