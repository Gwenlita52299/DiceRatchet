"""MOD-08 三基线：``random`` / ``heuristic`` / ``fixed_prompt``（PRD §4 FR-08-11…14）。

纪律：

- **heuristic 基线与 MOD-04 fallback 共用同一份实现**（Spec §4.7；禁止复制第二份启发式
  逻辑）——``HeuristicAgent`` 直接委托 ``agent.heuristic.heuristic_policy``；
  它移植自 GAME ``scripts/bots.js::greedy_counter``，默认局通关率 **45.5%**
  （n=200，seed 1–200；复现命令见 Spec §8.6）。
- **fixed_prompt 走 MOD-04 的 ``PolicyAgent(mode="baseline_fixed_prompt")``**（原设计，
  M1 验收 F22）：一次 ``play_decision()``（= 一个**决策点**）一次推理，与演化 agent 的
  粒度一致。MOD-08 **不再**自带逐 env 步的固定提示词 agent——那会让成本变成
  ``env 步 × t_d``（实测 ×3.1）并让「相对基线的提升」掺入粒度差异。
- **fixed_prompt 冻结在 harness v0**：模板取 MOD-04 包内的 ``fixed_v1.md``（``policy.
  fixed_prompt_path`` 指向它，不在 ``harness/`` 里，因此不随演化漂移），
  ``harness_version`` 钉死 0、工具关闭、``memory_top_k=0``、``reflection_max=0``、
  温度 0.0（R-6 对策：基线不可随 harness 漂移）。
- 归档**不可覆盖**（FR-08-12）：同名时写 ``<kind>-<split>-<ts>.json`` 并向 ``registry.csv``
  追加一行；归档必须能回答「这条数字是哪个 commit、哪套 seed、哪个后端产出的」（F6）。
- 长跑**逐局增量落盘** + ``--resume`` 跳过已完成 seed（F18）。
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.infra.io import write_json_atomic

from .harness import (EPISODE_PROTOCOL, ArmResult, EvalSpec, SandboxRunner,
                      load_episode_results, read_episode_protocols, run_eval,
                      write_episode_summaries)
from .metrics import SplitMetrics, Split, BasketId, aggregate
from . import seeds as seeds_mod

__all__ = ["BaselineAgent", "RandomAgent", "HeuristicAgent", "fixed_prompt_policy_config",
           "build_agent", "archive_baseline", "run_baseline", "BASELINE_KINDS",
           "baselines_dir", "RANDOM_RNG_SALT", "baseline_arm", "baseline_protocol",
           "DEFAULT_HARNESS_DIR"]

#: 固定随机盐（PRD §6：``rng = np.random.default_rng(seed ^ 0x5EED)``）
RANDOM_RNG_SALT: int = 0x5EED

BASELINE_KINDS: tuple[str, ...] = ("random", "heuristic", "fixed_prompt")

#: ``harness/`` 根（fixed_prompt 的 ``PolicyAgent`` 需要它定位模板/工具；工具对基线关闭）。
DEFAULT_HARNESS_DIR: Path = Path("harness")

#: ``model_hash`` 在「本次基线根本没加载任何模型」时的取值（F6：不假装记录了模型）。
NO_MODEL: str = "none"


@runtime_checkable
class BaselineAgent(Protocol):
    """**逐 env 步**的基线 agent 契约（PRD §6；``random`` / ``heuristic``）。

    ``fixed_prompt`` 不实现本协议：它返回 MOD-04 ``PolicyAgent``（有 ``play_decision``），
    由 ``SandboxRunner`` 走决策点循环（F22）。
    """

    name: str

    def reset(self, seed: int) -> None: ...

    def act(self, obs: Mapping[str, Any], legal: Sequence[Mapping[str, Any]],
            hints: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


def baseline_arm(kind: str) -> str:
    """基线在 ``episodes/<split>/*.jsonl`` 里的 ``arm`` 标签（F5 的过滤键）。"""
    return f"baseline-{kind}"


def baseline_protocol(kind: str, *, config: Any = None,
                      harness_dir: Path | str = DEFAULT_HARNESS_DIR) -> str:
    """基线的「测量协议」标识：``EPISODE_PROTOCOL`` + **冻结提示词模板的摘要**。

    F18 的续跑一致性闸要拦住的不只是「逐 env 步 vs 逐决策点」这种代码级语义变化，还包括
    **模板被改过**（F23 一轮里就改过三次）：同一份 run 的局必须来自同一份提示词，
    否则「基线分数」是一锅两套提示词的混合物。
    """
    from gamer_rsi.agent.prompts.assembler import FIXED_TEMPLATE_PATH, resolve_path, sha256_of

    if kind != "fixed_prompt":
        return EPISODE_PROTOCOL
    policy = getattr(getattr(config, "agent", None), "policy", None)
    configured = str(getattr(policy, "fixed_prompt_path", "") or "") if policy else ""
    path = resolve_path(configured, harness_dir=Path(harness_dir)) if configured else None
    if path is None or not path.is_file():
        path = FIXED_TEMPLATE_PATH
    try:
        digest = sha256_of(Path(path).read_text(encoding="utf-8"))[7:23]
    except OSError: # pragma: no cover - 模板不可读时退化为协议本身
        return EPISODE_PROTOCOL
    return f"{EPISODE_PROTOCOL}+{digest}"


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


# =====================================================================
# fixed_prompt：MOD-04 PolicyAgent(mode="baseline_fixed_prompt")（F22）
# =====================================================================

def fixed_prompt_policy_config(config: Any = None) -> Any:
    """``fixed_prompt`` 基线专用的**冻结** ``PolicyConfig``（R-6 + F22）。

    在配置的 ``agent.policy`` 之上强制：

    - ``mode="baseline_fixed_prompt"`` —— 走 MOD-04 的冻结模板分支（``_ensure_spec``
      解析 ``fixed_prompt_path``，不可用时回落包内 ``fixed_v1.md``）；
    - ``temperature=0.0`` 与 ``eval`` 采样（``_llm_mode`` 对 baseline 模式恒返回 eval）；
    - ``tools_enabled=False`` + ``tool_timeout_ms`` 不再生效 —— harness 的工具演化
      **不得**改变基线 prompt（否则基线会随 harness 漂移）；``{{tools}}`` 段为空；
    - ``memory_top_k=0`` / ``reflection_max=0`` —— 记忆与反思对基线关闭（PRD FR-08-13）。
    """
    from dataclasses import replace

    from gamer_rsi.infra.config import PolicyConfig

    base = None
    if config is not None:
        base = getattr(getattr(config, "agent", None), "policy", None)
    if base is None:
        base = PolicyConfig()
    return replace(base, mode="baseline_fixed_prompt", temperature=0.0,
                   tools_enabled=False, memory_top_k=0, reflection_max=0,
                   heuristic_fallback=True)


def build_agent(kind: str, *, seed: int, harness_version: int = 0,
                client: Any = None, config: Any = None,
                harness_dir: Path | str = DEFAULT_HARNESS_DIR) -> Any:
    """按 kind 构造基线 agent（FR-08-11/13/14）。

    - ``random`` / ``heuristic`` → 逐 env 步 agent（``act``）；
    - ``fixed_prompt`` → MOD-04 ``PolicyAgent``（``play_decision``，每**决策点**一次推理）。
      **必须有可用的 MOD-03 客户端**：拿不到即 fail-closed 抛 ``E_LLM_UNAVAILABLE``，
      绝不静默降级成启发式（Spec §8.5 / M1 验收 F1）。
    - ``harness_version`` 一律归 0（R-6：传入更高版本也不可漂移）。
    """
    if kind == "random":
        return RandomAgent(seed)
    if kind == "heuristic":
        return HeuristicAgent(seed)
    if kind == "fixed_prompt":
        if client is None:
            raise InfraError(
                E.E_LLM_UNAVAILABLE,
                "fixed_prompt 基线需要可用的 MOD-03 推理客户端，当前 client=None",
                hint="指定可加载的模型（如 --model qwen3-8b-q4；离线冒烟用 --model mock）；"
                     "不建议降级为启发式——那会让 fixed_prompt 归档变成 heuristic 的副本")
        from gamer_rsi.agent.policy import PolicyAgent

        return PolicyAgent(fixed_prompt_policy_config(config), llm=client,
                           harness_dir=Path(harness_dir))
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
                     harness_version: int = 0, ts: str | None = None,
                     seeds: Sequence[int] | None = None,
                     seeds_hash: str | None = None, protocol_hash: str | None = None,
                     model_backend: str = NO_MODEL, generation: int = 0,
                     n_workers: int = 1, seed_timeout_s: int | None = None,
                     resumed_n: int = 0, degraded_reason: str | None = None) -> Path:
    """不可覆盖归档（FR-08-12）：``<kind>-<split>.json``；同名时加 ``-<ts>``。

    同时向 ``registry.csv`` 追加一行（等宽口径，供 MOD-09 绘图与人复核）。

    **溯源（F6）**：``git_commit`` 取真实短 sha、``seeds_hash``/``protocol_hash`` 取真值、
    ``model_hash``/``model_backend`` 记录**实际加载的**模型与后端（没加载模型时记
    ``"none"``，而不是照抄配置里的 ``model_id``）。归档同时保存 seed 列表、``n_workers``、
    ``seed_timeout_s``、``resumed_n``，使台账能独立回答「这条数字怎么跑出来的」。
    """
    base_dir = baselines_dir(run_id, run_dir, archive_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts or _utc_stamp()
    path = base_dir / f"{kind}-{split}.json"
    if path.exists():
        path = base_dir / f"{kind}-{split}-{stamp}.json"
    seed_list = [int(s) for s in (seeds or ())]
    payload = {
        "kind": kind, "split": split, "basket": metrics.basket,
        "harness_version": int(harness_version), "run_id": run_id,
        "generation": int(generation), "model_hash": model_hash or NO_MODEL,
        "model_backend": model_backend or NO_MODEL, "git_commit": git_commit,
        "ts": stamp,
        "metrics": metrics.as_dict(),
        "seeds_hash": seeds_hash,
        "protocol_hash": protocol_hash,
        "seeds": seed_list,
        "n_workers": int(n_workers),
        "seed_timeout_s": (None if seed_timeout_s is None else int(seed_timeout_s)),
        "resumed_n": int(resumed_n),
        "degraded_reason": degraded_reason,
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
        "model_hash": payload.get("model_hash"), "model_backend": payload.get("model_backend"),
        "git_commit": payload.get("git_commit"), "seeds_hash": payload.get("seeds_hash"),
        "protocol_hash": payload.get("protocol_hash"), "n_seeds": len(payload.get("seeds") or []),
        "resumed_n": payload.get("resumed_n"), "path": archive_path.name,
    }
    new = not path.is_file()
    columns = ["ts", "kind", "split", "basket", "harness_version", "n", "win_rate",
               "ci_lo", "ci_hi", "degraded_n", "n_dropped", "model_hash", "model_backend",
               "seeds_hash", "protocol_hash", "n_seeds", "resumed_n", "git_commit", "path"]
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
                 write_summaries: bool = True,
                 harness_dir: Path | str = DEFAULT_HARNESS_DIR,
                 resume: bool = False, model_backend: str = NO_MODEL,
                 seed_timeout_s: int | None = None) -> ArmResult:
    """跑通一个基线并（默认）归档（FR-08-11/12）；返回 ``ArmResult``。

    ``heuristic`` 复用 MOD-04 ``heuristic_policy``；``fixed_prompt`` 走 MOD-04
    ``PolicyAgent(mode="baseline_fixed_prompt")``（F22），因此推理次数 ≈ **决策点数**。

    - ``fixed_prompt`` **必须有可用的 MOD-03 客户端**：拿不到时按 Spec §8.5 fail-closed
      抛 ``E_LLM_UNAVAILABLE``（退出码 3），而不是静默降级成启发式——否则会把启发式的
      分数当成 LLM 基线入档（M1 验收 F1）。
    - ``write_summaries=True`` 且给了 ``run_dir`` 时**每完成一局立刻落盘**
      ``episodes/<split>/e-<seed>-g<gen>-baseline-<kind>.jsonl``（F18）；
    - ``resume=True`` 时按已落盘的局跳过重复 seed，只跑缺的（F18），事实记在
      ``ArmResult.resumed_n`` / ``pending_seeds``。

    返回值上附加的跑批事实（``resumed_n`` / ``model_backend`` / ``seed_timeout_s`` /
    ``archive_path`` / ``pending_seeds``）是 F6 的溯源要求：台账要能回答「这条数字是
    哪个 commit、哪套 seed、哪个后端、跑了几局」。
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
    if resume and run_dir is None:
        raise InfraError(E.E_IO_CONFIG_INVALID, "--resume 需要明确的 run 目录",
                         hint="用法：python -m gamer_rsi baseline --resume --run-id <已有 run>")
    proto = seeds_mod.validate_protocol(seeds_mod.coerce_protocol(protocol))
    eval_cfg = getattr(config, "evals", config)
    seed_list = tuple(seeds_mod.resolve_basket(basket, generation=generation, protocol=proto))
    if not seed_list:
        raise InfraError(
            E.E_EVAL_SEED_OVERLAP,
            f"基线 {kind!r} 的 seed 集合为空（split={split!r} basket={basket!r}）",
            hint="显式给出 --seeds（如 --seeds train:1000-1004），或换用有 seed 的篮子"
                 "（heldout H1/H2/H3）；train_window 在第 0 代为空是预期行为")
    timeout_s = (int(seed_timeout_s) if seed_timeout_s is not None
                 else int(getattr(eval_cfg, "seed_timeout_s", 1800)))
    harness_version = 0 # 三基线一律钉在 harness v0（R-6：基线不可随 harness 漂移）
    arm_name = baseline_arm(kind)
    protocol_id = baseline_protocol(kind, config=config, harness_dir=harness_dir)

    prior: list[Any] = []
    pending = seed_list
    if resume:
        # 续跑的一致性闸：已落盘的局必须与当前代码的**测量协议**一致。否则会把 F22 修复前
        # 的「逐 env 步推理」局与修复后的「逐决策点」局拼进同一份聚合（F18 的隐患）。
        protocols = read_episode_protocols(run_dir, split, generation=int(generation),
                                          arm=arm_name)
        stale = sorted(p for p in protocols if p != protocol_id)
        if stale:
            raise InfraError(
                E.E_EVAL_ARM_NOT_ALLOWED,
                f"--resume 的 run 里已有不同测量协议的局：{stale}（当前 {protocol_id}）",
                hint="换一个 --run-id 重跑，或删除那些局后再续跑；"
                     "两套单局语义（含提示词模板不同）不可混算")
        prior = load_episode_results(run_dir, split, generation=int(generation),
                                     arm=arm_name, seeds=seed_list)
        done = {r.seed for r in prior}
        pending = tuple(s for s in seed_list if s not in done)

    spec = EvalSpec(
        run_id=run_id, generation=int(generation), harness_version=harness_version,
        split=split, basket=(basket if isinstance(basket, str) else seed_list),
        seeds=pending, agent_kind=kind, n_workers=int(n_workers),
        seed_timeout_s=timeout_s,
        t_max=int(getattr(eval_cfg, "t_max", 120)),
        min_support=int(getattr(eval_cfg, "min_support", 3)),
        drop_rate_max=float(getattr(eval_cfg, "drop_rate_max", 0.10)), arm=arm_name)

    written: list[Path] = []

    def _on_result(result: Any) -> None:
        """每完成一局立刻落盘（F18；进程崩溃只丢当前局）。"""
        if run_dir is None or not write_summaries:
            return
        written.extend(write_episode_summaries(run_dir, [result], split=split,
                                               generation=int(generation), arm=arm_name,
                                               protocol=protocol_id))

    factory = _baseline_factory(kind, client=client, harness_version=harness_version,
                                config=config, harness_dir=harness_dir)
    env_cfg = getattr(config, "env", None) if config is not None else None
    sandbox_cfg = getattr(env_cfg, "sandbox", None) if env_cfg is not None else None
    runner = SandboxRunner(agent_factory=factory, run_id=run_id, config=config,
                           episode_config=episode_config,
                           policy_failure_streak=int(getattr(
                               sandbox_cfg, "policy_failure_streak", 3)))
    result = run_eval(spec, runner=runner, writer=None, protocol=proto,
                      resume_results=tuple(prior),
                      on_result=(_on_result if (run_dir is not None and write_summaries)
                                 else None))
    if result.metrics.n == 0 and result.metrics.degraded_n:
        # 整批降级 = 基础设施故障，不是一次测量。归档一个 n=0 的「基线分数」会污染台账
        # （Spec §8.5 + M1 验收 F1），因此 fail-closed，什么都不落。
        raise InfraError(
            E.E_LLM_UNAVAILABLE,
            f"{kind} 基线全部 {result.metrics.degraded_n} 局降级为启发式，无可采信样本",
            hint="检查模型权重/后端是否可用（inference probe --model <id>）；"
                 "离线冒烟可用 --model mock，或改用 --kind heuristic")
    resolved_model = ((model_hash or getattr(client, "model_id", "") or "")
                      if client is not None else NO_MODEL)
    resolved_backend = _resolve_backend(client, model_backend)
    archived: Path | None = None
    # resume 且没有剩余 seed → 不重复归档（FR-08-12 的不可覆盖只针对真正的新测量）
    if archive and (pending or not resume):
        archived = archive_baseline(
            kind, split, result.metrics, run_id=run_id, model_hash=resolved_model,
            git_commit=git_commit or _git_sha(run_dir), run_dir=run_dir,
            harness_version=harness_version, seeds=seed_list,
            seeds_hash=getattr(proto, "seeds_hash", None),
            protocol_hash=seeds_mod.protocol_hash(proto),
            model_backend=resolved_backend,
            generation=int(generation), n_workers=int(n_workers),
            seed_timeout_s=timeout_s, resumed_n=len(prior))
    returned = ArmResult(arm=arm_name, split=split, basket=spec.basket_id,
                         harness_version=harness_version, memory_version=0, run_id=run_id,
                         results=result.results, metrics=result.metrics,
                         model_hash=resolved_model,
                         error_codes=result.error_codes, dur_s=result.dur_s)
    # ArmResult 是 frozen dataclass：跑批事实（F6 溯源）用 replace 追加一次。
    return replace(returned, resumed_n=len(prior), pending_seeds=tuple(pending),
                   model_backend=resolved_backend,
                   seed_timeout_s=timeout_s,
                   archive_path=str(archived) if archived is not None else "")


def _resolve_backend(client: Any, model_backend: str) -> str:
    """归档该记的后端名（F6）：没有客户端 → ``none``；有客户端但没给出名字 → ``unknown``。

    绝不回落到「配置里的 backend」——``--model mock`` 时配置仍是 ``mlx``，照抄就是假账。
    """
    if client is None:
        return NO_MODEL
    if model_backend and model_backend != NO_MODEL:
        return str(model_backend)
    return str(getattr(client, "backend", "") or getattr(client, "backend_name", "")
               or "unknown")


def _git_sha(run_dir: Path | str | None) -> str:
    """真实短 sha（F6）；git 不可用时 ``git_short_sha`` 自己回落 ``nogit``。"""
    from gamer_rsi.infra.run import git_short_sha

    root = Path(run_dir).parent if run_dir is not None else Path(".")
    return git_short_sha(root if root.is_dir() else Path("."))


def _baseline_factory(kind: str, *, client: Any, harness_version: int,
                      config: Any = None,
                      harness_dir: Path | str = DEFAULT_HARNESS_DIR
                      ) -> Callable[[int, str], Any]:
    def factory(seed: int, arm: str) -> Any:
        return build_agent(kind, seed=seed, harness_version=harness_version, client=client,
                           config=config, harness_dir=harness_dir)

    return factory
