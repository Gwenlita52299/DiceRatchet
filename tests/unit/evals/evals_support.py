"""MOD-08 单测公共辅助：构造 ``EpisodeResult`` 与假执行器（不依赖真实环境/模型）。

命名避开 ``conftest`` 顶层模块名：同时跑 ``tests/unit`` 与 ``tests/integration`` 时，
两个目录的 ``conftest.py`` 会在 ``sys.path`` 上撞名（见 tests/unit/agent/policy_support.py
的同款做法）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from gamer_rsi.evals.metrics import KIND_SPACE, EpisodeResult


def make_result(seed: int, *, split: str = "heldout", outcome: str = "win",
                generation: int = 1, harness_version: int = 1,
                stages_cleared: int = 6, hearts_left: int = 2, battles_won: int = 12,
                decisions: int = 96, steps: int = 232, illegal_attempts: int = 0,
                fallback_decisions: int = 0, truncation_reason: str | None = None,
                dropped: bool = False, degraded: bool = False,
                kind_counts: Sequence[int] | None = None,
                slot_counts: Mapping[str, int] | None = None,
                decision_kinds: Sequence[int] | None = None,
                seq: str | None = None) -> EpisodeResult:
    """构造一局；默认值取 Spec §8.6 默认局（hard×6 关）的实测 p50。"""
    counts = list(kind_counts) if kind_counts is not None else [0] * len(KIND_SPACE)
    if kind_counts is None:
        counts[0] = 1
    return EpisodeResult(
        seed=int(seed), split=split, generation=int(generation),  # type: ignore[arg-type]
        harness_version=int(harness_version), outcome=outcome,  # type: ignore[arg-type]
        truncation_reason=truncation_reason, stages_cleared=int(stages_cleared),
        hearts_left=int(hearts_left), battles_won=int(battles_won),
        decisions=int(decisions), steps=int(steps),
        illegal_attempts=int(illegal_attempts), fallback_decisions=int(fallback_decisions),
        kind_counts=tuple(counts), slot_counts=dict(slot_counts or {}),
        seq_hash=seq or f"seq-{seed}", dropped=bool(dropped), degraded=bool(degraded),
        decision_kinds=tuple(decision_kinds if decision_kinds is not None
                             else [0, 1, 2, 3]))


class FakeRunner:
    """确定性假执行器：``outcomes`` 按 seed 取，未列出的 seed 按 ``default``。"""

    def __init__(self, outcomes: Mapping[int, str] | None = None, *,
                 default: str = "win", degraded_seeds: Sequence[int] = (),
                 fail_seeds: Sequence[int] = ()) -> None:
        self.outcomes = dict(outcomes or {})
        self.default = default
        self.degraded_seeds = set(degraded_seeds)
        self.fail_seeds = set(fail_seeds)
        self.calls: list[int] = []

    def run_episode(self, seed: int, *, split: str, arm: str, generation: int,
                    harness_version: int, episode_config: Any = None,
                    timeout_s: float | None = None) -> EpisodeResult:
        self.calls.append(int(seed))
        if int(seed) in self.fail_seeds:
            raise RuntimeError(f"boom {seed}")
        outcome = self.outcomes.get(int(seed), self.default)
        return make_result(seed, split=split, outcome=outcome, generation=generation,
                           harness_version=harness_version, degraded=int(seed) in self.degraded_seeds)
