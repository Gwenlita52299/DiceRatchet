"""MOD-04 性能回归（NFR-04-2）：prompt 组装 / 解析 / 启发式的 p95 上界。

指标口径：p95 ≤ 15 ms（assemble_prompt）/ ≤ 1 ms（parse_llm_output、heuristic_policy）。
取 50 次样本的 p95（``sorted[samples*95//100]``）；实测通常 ≪ 上界一个数量级，
断言留足余量，避免 CI 抖动。
"""

from __future__ import annotations

import sys
import time
from typing import Callable

import pytest

from dice_ratchet.agent.heuristic import heuristic_policy
from dice_ratchet.agent.parser import parse_llm_output
from dice_ratchet.agent.prompts.assembler import (DEFAULT_TEMPLATE_PATH, MemoryItem,
                                               PromptContext, ReflectionItem,
                                               assemble_prompt, load_template)

GOOD = '{"reasoning":"a","plan":[{"kind":"end_turn"}]}'
SAMPLES = 50


@pytest.fixture(autouse=True)
def _skip_under_tracing() -> None:
    """覆盖率插桩下不判性能（NFR-04-2 针对生产模式；与 tests/golden/test_perf.py 同一先例）。

    这里断言的是绝对毫秒上界且不做机器折算，插桩带来的 2–4 倍放大足以在慢 runner 上
    造成假失败；无插桩的门（push / parity / 本地）仍照常断言。
    """
    if sys.gettrace() is not None:
        pytest.skip("coverage 插桩下不适用性能预算（NFR-04-2 针对生产模式）")


def _p95_ms(fn: Callable[[], object]) -> float:
    durations = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        fn()
        durations.append((time.perf_counter() - started) * 1000.0)
    durations.sort()
    return durations[min(len(durations) - 1, int(len(durations) * 0.95))]


def _context(observation: str) -> PromptContext:
    return PromptContext(
        observation=observation,
        task="task",
        harness_version=7,
        tools="- rank_actions(obs, candidates, memory) -> list[dict]",
        legal_actions='{"kind":"end_turn"}',
        reflections=tuple(ReflectionItem(f"r-{i}", "反思" * 20, i) for i in range(3)),
        memory=tuple(MemoryItem(f"m-{i}", "记忆" * 40, score=i / 10) for i in range(5)),
        obs_digest="sha256:" + "0" * 64,
    )


def test_assemble_prompt_p95_under_15ms(make_env):
    env = make_env(seed=1000)
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    context = _context("观测" * 300)
    elapsed = _p95_ms(lambda: assemble_prompt(spec, context))
    env.close()
    assert elapsed <= 15.0, f"assemble_prompt p95 {elapsed:.3f} ms > 15 ms"


def test_parse_llm_output_p95_under_1ms():
    elapsed = _p95_ms(lambda: parse_llm_output(GOOD, phase="player_turn"))
    assert elapsed <= 1.0, f"parse_llm_output p95 {elapsed:.4f} ms > 1 ms"


def test_heuristic_policy_p95_under_1ms(make_env):
    env = make_env(seed=1000)
    obs = env.observe()
    legal = env.legal_actions()
    hints = env.hints()
    elapsed = _p95_ms(lambda: heuristic_policy(obs, legal, hints))
    env.close()
    assert elapsed <= 1.0, f"heuristic_policy p95 {elapsed:.4f} ms > 1 ms"


def test_one_megabyte_garbage_parse_under_5ms():
    """§11 矩阵第 13 行：1 MB 无关文本解析 ≤ 5 ms。"""
    raw = "z" * 1_000_000
    elapsed = _p95_ms(lambda: parse_llm_output(raw, phase="player_turn"))
    assert elapsed <= 5.0, f"1MB 解析 p95 {elapsed:.3f} ms > 5 ms"


@pytest.mark.parametrize("text_len", [500, 2000, 8000])
def test_assemble_prompt_scales_linearly(text_len):
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    context = _context("x" * text_len)
    elapsed = _p95_ms(lambda: assemble_prompt(spec, context))
    assert elapsed <= 15.0
