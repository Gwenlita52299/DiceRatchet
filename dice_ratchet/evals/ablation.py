"""MOD-08 消融与归因（PRD §4 FR-08-21 / §8.4，计划 §6 验收 2）。

三臂（同一批 H2 seed，**臂内并行、臂间串行**）::

    full          当前 harness_version
    no_reflection 禁用 MOD-06 反思触发 + MOD-05 反思条目检索注入（策略记忆保留）
    no_self_edit  harness 冻结于 v0（prompts/tools 只读），反思与记忆演化保留

``delta_full[arm] = full.win_rate - arm.win_rate``；显著性口径为配对 McNemar（单侧
"该臂劣于 full"）+ Holm–Bonferroni（默认 ``alpha=0.05``；两臂时首个阈值 ``0.025``，
与 PRD §8.4 的写法一致）。最终

``attribution ∈ {reflection, self_edit, both, none}``
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.io import write_json_atomic

from .harness import ArmResult, compare_paired, evaluate_arm
from .metrics import mcnemar_exact_p, holm_bonferroni
from . import seeds as seeds_mod

__all__ = ["AblationArm", "AblationReport", "run_ablation", "attribution",
           "combine_attribution", "ARM_MECHANISM", "ablation_path"]

AblationArm = Literal["full", "no_reflection", "no_self_edit"]

#: 臂 → 被移除的机制（full 无）。
ARM_MECHANISM: Mapping[str, str] = {
    "full": "none",
    "no_reflection": "reflection",
    "no_self_edit": "self_edit",
}


@dataclass(frozen=True)
class AblationReport:
    """消融结果（PRD §7：``ablation/gen-XXX.json``）。"""

    generation: int
    basket: str
    alpha: float
    arms: Mapping[str, float]
    delta_full: Mapping[str, float]
    mcnemar_p: Mapping[str, float]
    holm: Mapping[str, Mapping[str, Any]]
    attribution: str
    n: Mapping[str, int] = field(default_factory=dict)
    run_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": int(self.generation), "basket": self.basket,
            "alpha": self.alpha, "arms": dict(self.arms),
            "delta_full": dict(self.delta_full), "mcnemar_p": dict(self.mcnemar_p),
            "holm": {k: dict(v) for k, v in self.holm.items()},
            "attribution": self.attribution, "n": dict(self.n), "run_id": self.run_id,
        }


def ablation_path(run_dir: Path | str, generation: int) -> Path:
    return Path(run_dir) / "ablation" / f"gen-{int(generation):03d}.json"


def _aligned(a: ArmResult, b: ArmResult) -> tuple[list[bool], list[bool]]:
    b_map = b.by_seed()
    left: list[bool] = []
    right: list[bool] = []
    for result in sorted(a.results, key=lambda r: r.seed):
        match = b_map.get(result.seed)
        if match is not None:
            left.append(result.win)
            right.append(match.win)
    return left, right


def attribution(full: Sequence[bool], arm: Sequence[bool], *, alpha: float = 0.05,
                arm_name: str = "") -> str:
    """单臂归因（FR-08-21）：该臂显著劣于 full 时返回被移除的机制名。

    - ``arm_name`` ∈ ``{"no_reflection","no_self_edit"}``：返回 ``reflection`` / ``self_edit``；
    - 未给 ``arm_name``：显著劣于 full 返回 ``"significant"``，否则 ``"none"``；
    - 多臂合成用 ``combine_attribution``（→ ``{reflection, self_edit, both, none}``）。
    """
    if len(full) != len(arm):
        raise InfraError(E.E_EVAL_SCHEMA,
                         f"归因需要等长配对样本：{len(full)} != {len(arm)}")
    if not full:
        return "none"
    p = mcnemar_exact_p(list(arm), list(full), "less") # 该臂显著**劣于** full
    if p >= alpha:
        return "none"
    if arm_name:
        return str(ARM_MECHANISM.get(arm_name, "significant"))
    return "significant"


def combine_attribution(reflection_sig: bool, self_edit_sig: bool) -> str:
    """两机制显著性 → ``{reflection, self_edit, both, none}``（Holm 校正后）。"""
    if reflection_sig and self_edit_sig:
        return "both"
    if reflection_sig:
        return "reflection"
    if self_edit_sig:
        return "self_edit"
    return "none"


def run_ablation(generation: int, *, arms: Sequence[AblationArm],
                 run_id: str, split_basket: str = "H2", alpha: float = 0.05,
                 harness_dir: Path | str = Path("harness"),
                 config: Any = None, protocol: seeds_mod.SeedProtocol | Any = None,
                 run_dir: Path | str | None = None,
                 memory_version: int = 0, model_hash: str = "",
                 evaluator: Callable[[AblationArm, int, str], ArmResult] | None = None,
                 write: bool = True) -> AblationReport:
    """跑 3 臂消融并做配对检验（FR-08-21 / §8.4）。

    ``evaluator(arm, generation, basket) -> ArmResult`` 可注入（MOD-06/07 用它挂真实
    执行器与记忆快照）；缺省用 ``evaluate_arm``，``no_self_edit`` 冻结在 harness v0。
    臂内并行由执行器决定，臂间**串行**（同 seed 同预算，避免资源互相干扰）。
    """
    if "full" not in arms:
        raise InfraError(E.E_EVAL_ARM_NOT_ALLOWED, "消融必须包含 full 臂")
    for arm in arms:
        if arm not in ARM_MECHANISM:
            raise InfraError(E.E_EVAL_ARM_NOT_ALLOWED, f"未知消融臂 {arm!r}",
                             hint=f"合法值：{sorted(ARM_MECHANISM)}")
    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    seed_list = seeds_mod.resolve_basket(split_basket, generation=generation, protocol=proto)

    def _default_evaluator(arm: AblationArm, gen: int, basket: str) -> ArmResult:
        harness_version = 0 if arm == "no_self_edit" else int(gen)
        return evaluate_arm(
            harness_dir, split="heldout", basket=seed_list, memory_version=memory_version,
            arm=("ablation-full" if arm == "full" else f"ablation-{arm}"),
            run_id=run_id, protocol=proto, generation=gen,
            harness_version=harness_version, config=config, model_hash=model_hash)

    evaluate = evaluator or _default_evaluator
    results: dict[str, ArmResult] = {}
    for arm in arms: # 臂间串行（§8.4）
        results[arm] = evaluate(arm, int(generation), split_basket)

    full = results["full"]
    delta_full: dict[str, float] = {}
    mcnemar_p: dict[str, float] = {}
    ordered_arms = [a for a in arms if a != "full"]
    for arm in ordered_arms:
        delta_full[arm] = float(full.metrics.win_rate) - float(results[arm].metrics.win_rate)
        full_bools, arm_bools = _aligned(full, results[arm])
        mcnemar_p[arm] = mcnemar_exact_p(arm_bools, full_bools, "less")
    corrected = holm_bonferroni([mcnemar_p[a] for a in ordered_arms], alpha=alpha)
    holm = {ordered_arms[i]: {"threshold": threshold, "significant": significant}
            for i, threshold, significant in corrected}
    sig = {arm: bool(holm[arm]["significant"]) for arm in ordered_arms}
    label = combine_attribution(sig.get("no_reflection", False),
                                sig.get("no_self_edit", False))
    report = AblationReport(
        generation=int(generation), basket=split_basket, alpha=float(alpha),
        arms={arm: float(results[arm].metrics.win_rate) for arm in arms},
        delta_full=delta_full, mcnemar_p=mcnemar_p, holm=holm, attribution=label,
        n={arm: int(results[arm].metrics.n) for arm in arms}, run_id=run_id)
    if write and run_dir is not None:
        write_json_atomic(ablation_path(run_dir, generation), report.as_dict())
    return report
