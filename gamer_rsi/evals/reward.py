"""MOD-08 shaped reward 定义（Spec v1.3 §3 由 MOD-01 移交；**只供学习与调试**）。

INV-7 的边界（必须逐字遵守）：

- 本模块的返回值**只允许**用于训练期的学习信号与调试可视化；
- 它**不得**进入任何判定路径：``metrics.py`` 不 import 本模块，``EpisodeResult`` 也没有
  对应字段（``tests/unit/evals/test_metrics_inv7.py`` 用 AST 扫描守这两条）；
- 报告的判定量只来自环境原生结果（``win_rate`` / ``stages_cleared`` / ``hearts_left`` /
  ``battles_won``）。

形状（shape）刻意保持**与规则同构**、可解释、可手算：非法动作轻罚；通关/楼层/胜场给正
奖励；剩余心与决策数给小额塑形。所有常数集中在此处，禁止散落到调用点。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

__all__ = [
    "ILLEGAL_PENALTY", "WIN_BONUS", "STAGE_BONUS", "BATTLE_BONUS", "HEART_BONUS",
    "DECISION_COST", "TRUNCATION_PENALTY", "shaped_reward", "cumulative_reward",
    "mean_cum_reward_debug", "REWARD_WEIGHTS",
]

ILLEGAL_PENALTY: Final[float] = -0.05
WIN_BONUS: Final[float] = 1.0
STAGE_BONUS: Final[float] = 0.10
BATTLE_BONUS: Final[float] = 0.01
HEART_BONUS: Final[float] = 0.02
DECISION_COST: Final[float] = 0.001
TRUNCATION_PENALTY: Final[float] = -0.30

REWARD_WEIGHTS: Final[Mapping[str, float]] = {
    "illegal": ILLEGAL_PENALTY, "win": WIN_BONUS, "stage": STAGE_BONUS,
    "battle": BATTLE_BONUS, "heart": HEART_BONUS, "decision": DECISION_COST,
    "truncation": TRUNCATION_PENALTY,
}


def shaped_reward(*, illegal: bool = False, outcome: str | None = None,
                  stages_cleared: int = 0, battles_won: int = 0, hearts_left: int = 0,
                  decisions: int = 0, truncated: bool = False,
                  previous: Mapping[str, Any] | None = None,
                  current: Mapping[str, Any] | None = None) -> float:
    """一步/一局的 shaped reward（**仅调试**）。

    ``previous`` / ``current`` 为 ``{"stages_cleared","battles_won","hearts_left",
    "decisions"}`` 的增量视图；给了增量就只对增量计分，否则用绝对值（终局调用）。
    """
    value = 0.0
    if illegal:
        return ILLEGAL_PENALTY
    if previous is not None and current is not None:
        for key, weight in (("stages_cleared", STAGE_BONUS), ("battles_won", BATTLE_BONUS),
                            ("hearts_left", HEART_BONUS), ("decisions", -DECISION_COST)):
            delta = float(current.get(key, 0)) - float(previous.get(key, 0))
            value += weight * delta
        return value
    value += STAGE_BONUS * int(stages_cleared)
    value += BATTLE_BONUS * int(battles_won)
    value += HEART_BONUS * int(hearts_left)
    value -= DECISION_COST * int(decisions)
    if outcome == "win":
        value += WIN_BONUS
    if truncated:
        value += TRUNCATION_PENALTY
    return float(value)


def cumulative_reward(step_rewards: Sequence[float]) -> float:
    """累计 shaped reward（调试曲线用）。"""
    return float(sum(float(x) for x in step_rewards))


def mean_cum_reward_debug(episodes: Sequence[Sequence[float]]) -> float:
    """每局累计 shaped reward 的均值（**只进 ext 调试段，禁止进 verdict**）。"""
    if not episodes:
        return 0.0
    return sum(cumulative_reward(ep) for ep in episodes) / len(episodes)
