"""MOD-08 三基线：``random`` / ``heuristic`` / ``fixed_prompt``（PRD §4 FR-08-11…14）。

纪律：

- **heuristic 基线与 MOD-04 fallback 共用同一份实现**（Spec §4.7；禁止复制第二份启发式
  逻辑）——``HeuristicAgent`` 直接委托 ``agent.heuristic.heuristic_policy``；
  它移植自 GAME ``scripts/bots.js::greedy_counter``，默认局通关率 **45.5%**
  （n=200，seed 1–200；复现命令见 Spec §8.6）。
- **fixed_prompt 冻结在 harness v0**：模板取 MOD-04 包内的 ``fixed_v1.md``（不在
  ``harness/`` 里，因此不随演化漂移），``memory_enabled=false``、``reflection_enabled=false``、
  ``temperature=0.0``（R-6 对策：基线不可随 harness 漂移）。
- 归档**不可覆盖**（FR-08-12）：同名时写 ``<kind>-<split>-<ts>.json`` 并向 ``registry.csv``
  追加一行。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.infra.io import write_json_atomic

from .harness import ArmResult, EvalSpec, SandboxRunner, run_eval
from .metrics import SplitMetrics, Split, BasketId, aggregate
from . import seeds as seeds_mod

__all__ = ["BaselineAgent", "RandomAgent", "HeuristicAgent", "FixedPromptAgent",
           "build_agent", "archive_baseline", "run_baseline", "BASELINE_KINDS",
           "baselines_dir", "RANDOM_RNG_SALT"]

#: 固定随机盐（PRD §6：``rng = np.random.default_rng(seed ^ 0x5EED)``）
RANDOM_RNG_SALT: int = 0x5EED

BASELINE_KINDS: tuple[str, ...] = ("random", "heuristic", "fixed_prompt")


@runtime_checkable
class BaselineAgent(Protocol):
    """基线 agent 契约（PRD §6）。``act`` 只返回 ``legal`` 中的动作。"""

    name: str

    def reset(self, seed: int) -> None: ...

    def act(self, obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
            hints: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


class RandomAgent:
    """在 ``legal_actions`` 上均匀采样（``seed ^ 0x5EED`` 的显式 rng，INV-6）。"""

    name = "random"

    def __init__(self, seed: int) -> None:
        self.seed = int(seed)
        self.rng = np.random.default_rng(int(seed) ^ RANDOM_RNG_SALT)

    def reset(self, seed: int) -> None:
        self.seed = int(seed)
        self.rng = np.random.default_rng(int(seed) ^ RANDOM_RNG_SALT)

    def act(self, obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
            hints: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not legal:
            return {}
        return dict(legal[int(self.rng.integers(0, len(legal)))])


class HeuristicAgent:
    """委托 MOD-04 ``heuristic_policy``（Spec §4.7；**禁止复制第二份实现**）。"""

    name = "heuristic"

    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)
        self.fallback_decisions = 0

    def reset(self, seed: int) -> None:
        self.seed = int(seed)

    def act(self, obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
            hints: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from gamer_rsi.agent.heuristic import heuristic_policy

        return heuristic_policy(obs, legal, hints)


class FixedPromptAgent:
    """冻结提示词基线（harness v0；memory/reflection 关闭；``temperature=0``）。

    注入 MOD-03 客户端时走真实 LLM 路径（模板 ``fixed_v1.md`` + 三级解析的 ①/② 级），
    否则按 Spec §8.5 **显式降级**为启发式（``degraded=True``），不假装是 LLM 局。
    """

    name = "fixed_prompt"

    def __init__(self, *, seed: int = 0, client: Any = None,
                 harness_version: int = 0, memory_enabled: bool = False,
                 reflection_enabled: bool = False) -> None:
        self.seed = int(seed)
        self.client = client
        self.harness_version = int(harness_version)
        self.memory_enabled = bool(memory_enabled)
        self.reflection_enabled = bool(reflection_enabled)
        self.degraded = False
        self.degraded_reason: str | None = None
        self._call_index = 0

    def reset(self, seed: int) -> None:
        self.seed = int(seed)
        self.degraded = False
        self.degraded_reason = None
        self._call_index = 0

    # ---- 模板渲染 -----------------------------------------------------
    def render(self, obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
               hints: Mapping[str, Any] | None = None) -> str:
        from gamer_rsi.agent.prompts.assembler import FIXED_TEMPLATE_PATH

        text = Path(FIXED_TEMPLATE_PATH).read_text(encoding="utf-8")
        try:
            from gamer_rsi.sandbox.projection import render_compact

            observation = render_compact(obs)
        except Exception: # 投影渲染不可用时退化为 JSON（仍是白名单观测）
            observation = json.dumps(dict(obs), ensure_ascii=False, sort_keys=True)
        legal_text = "\n".join(json.dumps(dict(a), ensure_ascii=False, sort_keys=True)
                               for a in legal)
        replacements = {
            "{{task}}": "在《GameRSI》骰子 roguelike 中做出当前最优决策。",
            "{{harness_version}}": str(self.harness_version),
            "{{tools}}": "（基线：无工具）",
            "{{observation}}": observation,
            "{{legal_actions}}": legal_text,
            "{{reflections}}": "（基线：关闭）",
            "{{memory}}": "（基线：关闭）",
            "{{constraints}}": "",
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    def act(self, obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
            hints: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not legal:
            return {}
        if self.client is None:
            self.degraded = True
            return HeuristicAgent(self.seed).act(obs, legal, hints)
        prompt = self.render(obs, legal, hints)
        self._call_index += 1
        try:
            from gamer_rsi.agent.parser import decision_schema_dict, parse_llm_output
            from gamer_rsi.inference import Message

            # MOD-03 的 ``generate_json`` 契约收 ``Message`` 对象（有 ``.content``），
            # 不是 dict——传 dict 会在 token 估算处 AttributeError，进而被下面的
            # except 静默降级（M1 验收 F1）。
            result = self.client.generate_json(
                [Message(role="user", content=prompt)], decision_schema_dict(),
                mode="eval",
                sampling={"temperature": 0.0, "top_p": 1.0, "top_k": 0,
                          "seed": int(self.seed)},
                seed=int(self.seed))
            text = getattr(result, "text", None)
            if text is None and isinstance(result, Mapping):
                text = result.get("text", "")
            parsed = parse_llm_output(str(text or ""))
            action = parsed.action
            if action is not None and _in_legal(action, legal):
                return dict(action)
            # 解析出动作但不在 legal 里 → **不是**降级（模型可用），由执行器的
            # fallback_rate 记账（Spec §8.5 的 degraded 专指模型不可用）。
        except Exception as exc: # 模型不可用/解析失败 → 显式降级（Spec §8.5）
            self.degraded = True
            self.degraded_reason = f"{type(exc).__name__}: {exc}"[:200]
        return HeuristicAgent(self.seed).act(obs, legal, hints)


def _in_legal(action: Mapping[str, Any], legal: Sequence[Mapping[str, Any]]) -> bool:
    def sig(item: Mapping[str, Any]) -> str:
        return json.dumps({str(k): v for k, v in item.items() if v is not None},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    target = sig(action)
    return any(sig(item) == target for item in legal)


def build_agent(kind: str, *, seed: int, harness_version: int = 0,
                client: Any = None, config: Any = None) -> Any:
    """按 kind 构造基线 agent（FR-08-11/13/14）。

    ``heuristic`` 复用 MOD-04 ``heuristic_policy``；``fixed_prompt`` 固定
    ``harness_version=0`` 且关闭 memory/reflection。
    """
    if kind == "random":
        return RandomAgent(seed)
    if kind == "heuristic":
        return HeuristicAgent(seed)
    if kind == "fixed_prompt":
        # R-6：fixed_prompt 基线钉死 harness v0，传入更高版本也一律归 0（不可漂移）
        return FixedPromptAgent(seed=seed, client=client, harness_version=0,
                                memory_enabled=False, reflection_enabled=False)
    raise InfraError(E.E_EVAL_ARM_NOT_ALLOWED, f"未知基线 kind={kind!r}",
                     hint=f"合法值：{list(BASELINE_KINDS)}")


# =====================================================================
# 归档（FR-08-11/12）
# =====================================================================

def baselines_dir(run_id: str, run_dir: Path | str | None = None,
                  archive_dir: str = "baselines") -> Path:
    root = Path(run_dir) if run_dir is not None else Path("runs") / run_id
    return root / archive_dir


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def archive_baseline(kind: str, split: Split, metrics: SplitMetrics, *,
                     run_id: str, model_hash: str, git_commit: str,
                     run_dir: Path | str | None = None,
                     archive_dir: str = "baselines",
                     harness_version: int = 0, ts: str | None = None) -> Path:
    """不可覆盖归档（FR-08-12）：``<kind>-<split>.json``；同名时加 ``-<ts>``。

    同时向 ``registry.csv`` 追加一行（等宽口径，供 MOD-09 绘图与人复核）。
    """
    base_dir = baselines_dir(run_id, run_dir, archive_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts or _utc_stamp()
    path = base_dir / f"{kind}-{split}.json"
    if path.exists():
        path = base_dir / f"{kind}-{split}-{stamp}.json"
    payload = {
        "kind": kind, "split": split, "basket": metrics.basket,
        "harness_version": int(harness_version), "run_id": run_id,
        "model_hash": model_hash, "git_commit": git_commit, "ts": stamp,
        "metrics": metrics.as_dict(),
        "seeds_hash": None,
        "reproduce": f"python -m gamer_rsi baseline --kind {kind} --split {split} "
                     f"--basket {metrics.basket} --run-id {run_id}",
    }
    write_json_atomic(path, payload)
    _append_registry(base_dir / "registry.csv", payload, path)
    return path


def _append_registry(path: Path, payload: Mapping[str, Any], archive_path: Path) -> None:
    metrics = dict(payload.get("metrics") or {})
    ci = list(metrics.get("win_rate_ci95") or [0.0, 0.0])
    row = {
        "ts": payload.get("ts"), "kind": payload.get("kind"), "split": payload.get("split"),
        "basket": payload.get("basket"), "harness_version": payload.get("harness_version"),
        "n": metrics.get("n"), "win_rate": metrics.get("win_rate"),
        "ci_lo": ci[0] if ci else None, "ci_hi": ci[1] if len(ci) > 1 else None,
        "degraded_n": metrics.get("degraded_n"), "n_dropped": metrics.get("n_dropped"),
        "model_hash": payload.get("model_hash"), "git_commit": payload.get("git_commit"),
        "path": archive_path.name,
    }
    new = not path.is_file()
    columns = ["ts", "kind", "split", "basket", "harness_version", "n", "win_rate",
               "ci_lo", "ci_hi", "degraded_n", "n_dropped", "model_hash", "git_commit", "path"]
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow({k: (round(v, 4) if isinstance(v, float) else v)
                         for k, v in row.items()})


# =====================================================================
# 跑一个基线（G6：python -m gamer_rsi baseline --kind … --split heldout --basket H1）
# =====================================================================

def run_baseline(kind: str, *, split: Split, basket: BasketId | Sequence[int],
                 run_id: str, run_dir: Path | str | None = None,
                 config: Any = None, protocol: seeds_mod.SeedProtocol | Any = None,
                 client: Any = None, model_hash: str = "", git_commit: str = "",
                 n_workers: int = 1, generation: int = 0,
                 episode_config: Any = None, archive: bool = True,
                 write_summaries: bool = True) -> ArmResult:
    """跑通一个基线并（默认）归档（FR-08-11/12）。

    ``heuristic`` 复用 MOD-04 ``heuristic_policy``；``fixed_prompt`` 固定 harness v0。

    ``fixed_prompt`` **必须有可用的 MOD-03 客户端**：基线语义就是「冻结提示词 + 真实
    LLM」。拿不到客户端时按 Spec §8.5 fail-closed 抛 ``E_LLM_UNAVAILABLE``（退出码 3），
    而不是静默降级成启发式——否则会把启发式的分数当成 LLM 基线入档（M1 验收 F1）。
    """
    if kind not in BASELINE_KINDS:
        raise InfraError(E.E_EVAL_ARM_NOT_ALLOWED, f"未知基线 kind={kind!r}",
                         hint=f"合法值：{list(BASELINE_KINDS)}")
    if kind == "fixed_prompt" and client is None:
        raise InfraError(
            E.E_LLM_UNAVAILABLE,
            "fixed_prompt 基线需要可用的 MOD-03 推理客户端，当前 client=None",
            hint="指定可加载的模型（如 --model qwen3-8b-q4；离线冒烟用 --model mock）；"
                 "不建议降级为启发式——那会让 fixed_prompt 归档变成 heuristic 的副本")
    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    eval_cfg = getattr(config, "evals", config)
    seed_list = tuple(seeds_mod.resolve_basket(basket, generation=generation, protocol=proto))
    if not seed_list:
        raise InfraError(
            E.E_EVAL_SEED_OVERLAP,
            f"基线 {kind!r} 的 seed 集合为空（split={split!r} basket={basket!r}）",
            hint="显式给出 --seeds（如 --seeds train:1000-1004），或换用有 seed 的篮子"
                 "（heldout H1/H2/H3）；train_window 在第 0 代为空是预期行为")
    harness_version = 0 # 三基线一律钉在 harness v0（R-6：基线不可随 harness 漂移）
    spec = EvalSpec(
        run_id=run_id, generation=int(generation), harness_version=harness_version,
        split=split, basket=(basket if isinstance(basket, str) else seed_list),
        seeds=seed_list, agent_kind=kind, n_workers=int(n_workers),
        seed_timeout_s=int(getattr(eval_cfg, "seed_timeout_s", 300)),
        t_max=int(getattr(eval_cfg, "t_max", 120)),
        min_support=int(getattr(eval_cfg, "min_support", 3)),
        drop_rate_max=float(getattr(eval_cfg, "drop_rate_max", 0.10)), arm=f"baseline-{kind}")
    factory = _baseline_factory(kind, client=client, harness_version=harness_version)
    env_cfg = getattr(config, "env", None) if config is not None else None
    sandbox_cfg = getattr(env_cfg, "sandbox", None) if env_cfg is not None else None
    runner = SandboxRunner(agent_factory=factory, run_id=run_id, config=config,
                           episode_config=episode_config,
                           policy_failure_streak=int(getattr(
                               sandbox_cfg, "policy_failure_streak", 3)))
    result = run_eval(spec, runner=runner, writer=None, protocol=proto)
    if result.metrics.n == 0 and result.metrics.degraded_n:
        # 整批降级 = 基础设施故障，不是一次测量。归档一个 n=0 的「基线分数」会污染台账
        # （Spec §8.5 + M1 验收 F1），因此 fail-closed，什么都不落。
        raise InfraError(
            E.E_LLM_UNAVAILABLE,
            f"{kind} 基线全部 {result.metrics.degraded_n} 局降级为启发式，无可采信样本",
            hint="检查模型权重/后端是否可用（inference probe --model <id>）；"
                 "离线冒烟可用 --model mock，或改用 --kind heuristic")
    if run_dir is not None and write_summaries:
        from .harness import write_episode_summaries

        write_episode_summaries(run_dir, result.results, split=split,
                                generation=int(generation), arm=f"baseline-{kind}")
    arm = ArmResult(arm=f"baseline-{kind}", split=split, basket=spec.basket_id,
                    harness_version=harness_version, memory_version=0, run_id=run_id,
                    results=result.results, metrics=result.metrics, model_hash=model_hash,
                    error_codes=result.error_codes, dur_s=result.dur_s)
    if archive:
        archive_baseline(kind, split, result.metrics, run_id=run_id,
                         model_hash=model_hash, git_commit=git_commit, run_dir=run_dir,
                         harness_version=harness_version)
    return arm


def _baseline_factory(kind: str, *, client: Any, harness_version: int
                      ) -> Callable[[int, str], Any]:
    def factory(seed: int, arm: str) -> Any:
        return build_agent(kind, seed=seed, harness_version=harness_version, client=client)

    return factory
