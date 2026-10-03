"""env 测试公共夹具：从 golden 重放出各 phase 的真实 state。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.env import GameEngine, load_tables

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN = _REPO_ROOT / "src/game/tests/golden"


@pytest.fixture(scope="module")
def tables():
    return load_tables()


@pytest.fixture(scope="module")
def engine(tables):
    return GameEngine(tables)


def _replay_until(engine, golden: str, phase: str | None, max_steps: int = 400):
    """重放 golden 直到出现指定 phase（返回该 state）；phase=None 时返回首个 player_turn。"""
    path = _GOLDEN / f"{golden}.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    state = engine.create(lines[0]["config"])
    target = phase or "player_turn"
    n = 0
    for line in lines[1:]:
        if line.get("type") == "run_end":
            break
        state = engine.reduce(state, line["action"])["state"]
        n += 1
        if state["phase"] == target:
            return state
        if n >= max_steps:
            break
    raise AssertionError(f"{golden} 前 {n} 步未出现 phase={target}")


@pytest.fixture(scope="module")
def started(engine):
    return _replay_until(engine, "g1_all_off_medium", "player_turn")


@pytest.fixture(scope="module")
def phases(engine):
    """各 phase 的真实 state 字典（从最长的 easy 全量局重放）。"""
    out: dict[str, object] = {}

    def get(phase: str, golden: str = "g4_hard_corrupt_path"):
        if phase not in out:
            out[phase] = _replay_until(engine, golden, phase)
        return out[phase]

    return get
