"""成功度量③：Python ``greedy_counter`` 在默认局（hard × 6 关，n=200）的通关率与 JS 相差 ≤ 3pp。

两侧策略同源（``tests/golden/greedy_bot.py`` ↔ ``src/game/scripts/bots.js``），
内核已由 golden/差分对拍证明逐位等价，因此**逐局结局也应相同**；本测试同时断言
逐局一致与汇总通关率。``@pytest.mark.slow``；无 node 时只跑 Python 侧并报告通关率。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, load_tables

sys.path.insert(0, str(Path(__file__).resolve().parent))
from greedy_bot import run_game  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOT_MATCH = _REPO_ROOT / "src/game/tests/bot-match.js"
_N = int(os.environ.get("GAMERSI_BOT_MATCH_N", "200"))
_STAGE_COUNT = 6
_DIFFICULTY = "hard"


@pytest.fixture(scope="module")
def engine() -> GameEngine:
    return GameEngine(load_tables())


@pytest.fixture(scope="module")
def python_outcomes(engine) -> list[dict]:
    out = []
    for i in range(_N):
        seed = 1 + i
        try:
            r = run_game(engine, difficulty=_DIFFICULTY, seed=seed,
                         stage_count=_STAGE_COUNT, max_steps=4000)
            out.append({"seed": seed, "outcome": r["outcome"], "stage": r["stage"],
                        "steps": r["steps"]})
        except Exception as exc:  # noqa: BLE001
            out.append({"seed": seed, "error": str(exc)[:300]})
    return out


@pytest.fixture(scope="module")
def js_outcomes(python_outcomes) -> list[dict] | None:
    if not shutil.which("node") or not _BOT_MATCH.exists():
        return None
    proc = subprocess.run(
        ["node", str(_BOT_MATCH), str(_N), str(_STAGE_COUNT), _DIFFICULTY, "1"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        pytest.fail(f"bot-match.js 失败：\n{proc.stdout[-1000:]}\n{proc.stderr[-1000:]}")
    return json.loads(proc.stdout)


@pytest.mark.slow
def test_greedy_counter_win_rate_matches_js(python_outcomes, js_outcomes) -> None:
    py_wins = sum(1 for x in python_outcomes if x.get("outcome") == "win")
    py_rate = py_wins / len(python_outcomes)
    if js_outcomes is None:
        pytest.skip(f"node 不可用（Python 侧通关率 {py_rate:.1%}，无 JS 参照）")
    js_by_seed = {x["seed"]: x for x in js_outcomes}
    js_wins = sum(1 for x in js_outcomes if x.get("outcome") == "win")
    js_rate = js_wins / len(js_outcomes)
    diffs = []
    for py in python_outcomes:
        js = js_by_seed.get(py["seed"])
        if js is None or js.get("outcome") != py.get("outcome"):
            diffs.append(py["seed"])
    print(f"\ngreedy_counter(hard×{_STAGE_COUNT}, n={_N}): "
          f"python={py_rate:.1%} js={js_rate:.1%} 逐局不一致={diffs}")
    assert not diffs, f"逐局结局不一致（前 10：{diffs[:10]}）"
    assert abs(py_rate - js_rate) <= 0.03, f"通关率相差 > 3pp（{py_rate:.1%} vs {js_rate:.1%}）"


@pytest.mark.slow
def test_greedy_bot_no_kernel_errors(python_outcomes) -> None:
    errors = [x for x in python_outcomes if x.get("error")]
    assert not errors, f"Python bot 触发内核错误/非法动作：{errors[:3]}"
