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


@pytest.fixture(scope="module")
def find_state(engine):
    """``find(golden, predicate, *, max_steps=4000, label=None)``：扫到第一个满足
    ``predicate(state)`` 的 state 为止，**扫不到就 AssertionError，不退化成 skip**。

    为什么需要它：M0 验收（2026-10-03）发现 5 个用例把「golden 里的第一个 player_turn /
    event」当夹具，而它们要的前置条件（带魔化骰的手牌、需要目标之外的面、可扣心的事件、
    开关全开的局）在那个快照上都不成立 —— 于是**恒跳过、从不真正断言**，且 skipped 计数
    把它伪装成「环境条件不满足」。前置条件在 golden / 配表里是**确定存在**的，扫不到就是
    golden 或配表变了（契约变更），必须响亮失败。

    返回的是重放中的**原 state 对象**：调用方若要在其上做破坏性操作，先 ``copy.deepcopy``
    （每个调用都会重新重放，不会污染其他用例）。
    """
    def find(golden: str, predicate, *, max_steps: int = 4000, label: str | None = None):
        path = _GOLDEN / f"{golden}.jsonl"
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        state = engine.create(lines[0]["config"])
        n = 0
        for line in lines[1:]:
            if line.get("type") == "run_end" or n >= max_steps:
                break
            state = engine.reduce(state, line["action"])["state"]
            n += 1
            if predicate(state):
                return state
        raise AssertionError(
            f"{golden} 前 {n} 步未出现满足「{label or 'predicate'}」的 state"
            "（前置条件是契约的一部分：golden/配表变更需同步本用例）")

    return find
