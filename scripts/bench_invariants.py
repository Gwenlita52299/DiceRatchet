"""实测「逐步内核不变式校验」的开销（B-89 / F78 的裁决依据，可随时复算）。

用法::

    PYTHONPATH=. .venv/bin/python scripts/bench_invariants.py

判据：``SandboxEnv.step`` 现在每个合法动作都会在**提交前**跑 MOD-01
``env.state.assert_invariants``（内核自带的那次被游戏侧 ``debug.showHiddenInfo`` 门着，
生产局为零）。这里把同一批真实对局（``src/game/tests/golden/*.jsonl``，JS 侧生成的
逐位对拍基准）重放两遍口径，比较每步开销：

- ``reduce``：内核 ``GameEngine.reduce``（真实对局的主要 CPU 成本）；
- ``assert``：``assert_invariants`` 本身。

结论（2026-10-15，M 系列机器）：约 102 µs/步 vs 202 µs/步 ⇒ 一局约 345 步 ⇒ +35 ms/局，
相对以 LLM 决策为主的整局耗时（秒级）可忽略。**这不是性能门禁**，数字随机器变化属正常；
若哪天不变式集合显著变重（或每步状态变大），重跑本脚本再决定是否要降级校验频率。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from dice_ratchet.env import GameEngine, load_tables_checked
from dice_ratchet.env.state import assert_invariants

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "src/game/tests/golden"


def _replay(engine: GameEngine, path: Path) -> tuple[int, float, float]:
    """重放一个 golden 局，返回 ``(步数, reduce 秒, assert 秒)``。"""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    state = engine.create(rows[0]["config"])
    steps = 0
    t_reduce = t_assert = 0.0
    for row in rows[1:]:
        if row.get("type") == "run_end" or "action" not in row:
            continue
        t0 = time.perf_counter()
        result = engine.reduce(state, row["action"])
        t1 = time.perf_counter()
        state = result["state"]
        assert_invariants(state)
        t2 = time.perf_counter()
        t_reduce += t1 - t0
        t_assert += t2 - t1
        steps += 1
    return steps, t_reduce, t_assert


def main() -> None:
    engine = GameEngine(load_tables_checked())
    steps = 0
    t_reduce = t_assert = 0.0
    for path in sorted(GOLDEN.glob("*.jsonl")):
        n, r, a = _replay(engine, path)
        steps += n
        t_reduce += r
        t_assert += a
        print(f"{path.name:32s} steps={n:4d}  reduce={r * 1000:7.1f} ms  assert={a * 1000:6.1f} ms")
    print("-" * 72)
    print(f"合计 steps={steps}")
    print(f"reduce  {t_reduce * 1e6 / steps:7.1f} µs/步")
    print(f"assert  {t_assert * 1e6 / steps:7.1f} µs/步"
          f"（= reduce 的 {t_assert / t_reduce * 100:.0f}%）")


if __name__ == "__main__":
    main()
