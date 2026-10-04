"""T-12 + FR-08-11…14：三基线复用、确定性与不可覆盖归档。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.agent.heuristic import heuristic_policy
from gamer_rsi.evals.baselines import (FixedPromptAgent, HeuristicAgent, RandomAgent,
                                       archive_baseline, build_agent)
from gamer_rsi.evals.metrics import aggregate

from evals_support import make_result

_OBS = {"phase": "rest", "player": {"hearts": 2}}
_LEGAL = [{"kind": "rest_choose", "optionId": "heart"},
          {"kind": "rest_choose", "optionId": "gold"}]


def test_heuristic_agent_delegates_to_mod04():
    # T-12：同一 obs/legal 上必须与 MOD-04 heuristic_policy 选同一动作（防逻辑分叉）
    agent = HeuristicAgent(seed=7)
    assert agent.act(_OBS, _LEGAL) == heuristic_policy(_OBS, _LEGAL, {})


def test_random_agent_is_seed_deterministic_and_legal():
    a = RandomAgent(seed=2000)
    b = RandomAgent(seed=2000)
    picks_a = [a.act(_OBS, _LEGAL) for _ in range(20)]
    picks_b = [b.act(_OBS, _LEGAL) for _ in range(20)]
    assert picks_a == picks_b
    assert all(p in _LEGAL for p in picks_a)


def test_fixed_prompt_is_frozen_and_degrades_explicitly():
    agent = FixedPromptAgent(seed=1, client=None)
    assert agent.harness_version == 0
    assert agent.memory_enabled is False and agent.reflection_enabled is False
    action = agent.act(_OBS, _LEGAL)
    assert action in _LEGAL
    assert agent.degraded is True # 无 MOD-03 客户端 → 显式降级，不静默


def test_build_agent_kinds():
    assert isinstance(build_agent("random", seed=1), RandomAgent)
    assert isinstance(build_agent("heuristic", seed=1), HeuristicAgent)
    fixed = build_agent("fixed_prompt", seed=1, harness_version=9)
    assert isinstance(fixed, FixedPromptAgent) and fixed.harness_version == 0
    with pytest.raises(Exception):
        build_agent("nope", seed=1)


def test_archive_baseline_is_not_overwritable(tmp_path):
    metrics = aggregate([make_result(2000, outcome="win")], basket="H1")
    first = archive_baseline("heuristic", "heldout", metrics, run_id="r-1",
                             model_hash="m1", git_commit="abc", run_dir=tmp_path,
                             ts="20260101T000000Z")
    second = archive_baseline("heuristic", "heldout", metrics, run_id="r-1",
                              model_hash="m1", git_commit="abc", run_dir=tmp_path,
                              ts="20260102T000000Z")
    assert first.name == "heuristic-heldout.json"
    assert second.name == "heuristic-heldout-20260102T000000Z.json"
    assert json.loads(first.read_text(encoding="utf-8"))["kind"] == "heuristic"
    registry = (tmp_path / "baselines" / "registry.csv").read_text(encoding="utf-8")
    assert len(registry.strip().splitlines()) == 3 # 表头 + 两行
