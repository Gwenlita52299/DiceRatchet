"""单局执行器（MOD-06 PRD FR-06-1/2/12；Spec §4.5/§4.7）。

``EpisodeRunner.run_episode`` 是**唯一**的单局时序实现，三个使用方共用它：

- ``LoopOrchestrator`` 的训练局（``split="train", arm="train"``）；
- MOD-07 经 MOD-08 ``evaluate_arm(runner=...)`` 注入的评估局（``arm ∈ {parent, cand-*}``）；
- 回归测试的固定 seed 冒烟（``loop smoke``）。

时序**只看** MOD-02 的 ``last.terminated or last.truncated``：步数上限、决策上限、
``policy_failure`` 都由 MOD-02 统一判定并以 ``truncated`` 返回（Spec B-15），本模块
不维护 ``illegal_streak`` / ``max_steps``，也**不**自行判负。

依赖全部可注入（``env/policy/memory/writer/clock``，NFR-06-6）：``loop smoke --no-reflect``
用 ``tests/fakes.py`` 的假实现跑通，不触网、不调模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from ..evals.harness import kind_index, slot_key
from ..evals.metrics import KIND_SPACE
from ..infra import events
from ..infra.errors import E, InfraError
from ..infra.io import read_json
from ..infra.run import RunPaths
from .state import RunState
from .trajectory import StepMeta, TrajectoryWriter, first_executed_index, seq_hash

__all__ = ["DecisionResultLike", "EpisodeRunner", "PolicyProto", "RecordedEpisode",
           "RunContext", "episode_key", "read_harness_version"]


# =====================================================================
# 契约（Spec §4.7；本模块只依赖协议，不 import MOD-04 的实现）
# =====================================================================

@runtime_checkable
class PolicyProto(Protocol):
    """MOD-04 ``PolicyAgent`` 的最小面（``reset`` + ``play_decision``）。"""

    def reset(self, ctx: Any) -> None: ...

    def play_decision(self, env: Any, obs: Mapping[str, Any], *,
                      ctx: Any) -> Any: ...


DecisionResultLike = Any


def episode_key(seed: int, generation: int, arm: str) -> str:
    """Spec §4.5 B-16 的幂等键：``e-{seed:06d}-g{generation:03d}-{arm}``。"""
    return f"e-{int(seed):06d}-g{int(generation):03d}-{arm}"


def read_harness_version(harness_dir: Path | str = Path("harness")) -> int:
    """读 ``harness/version.json`` 的 ``version``（FR-06-7：**每局**重读，不跨代缓存）。

    文件缺失或不可解析 → 降级为 0（``harness_version ≥ 0``，Spec §4.5）。
    """
    path = Path(harness_dir) / "version.json"
    if not path.is_file():
        return 0
    try:
        data = read_json(path)
    except InfraError:
        return 0
    if not isinstance(data, Mapping):
        return 0
    try:
        return max(0, int(data.get("version", 0)))
    except (TypeError, ValueError): # pragma: no cover - 损坏的 version.json
        return 0


# =====================================================================
# 记录与上下文
# =====================================================================

@dataclass(frozen=True)
class RecordedEpisode:
    """一局的权威记录（PRD §6 的 ``RecordedEpisode``）。

    末尾的 ``x_`` 组字段是 MOD-08 ``EpisodeResult`` 的桥接面（本模块不聚合任何指标，
    只把**本局**的原始计数带出去，让 MOD-08 能从轨迹复算熵/序列多样性）。
    """

    episode_key: str
    seed: int
    split: str
    arm: str
    generation: int
    harness_version: int
    steps: int
    decisions: int
    cum_reward: float
    outcome: str
    truncation_reason: str | None
    stages_cleared: int
    hearts_left: int
    battles_won: int
    illegal_attempts: int
    fallback_decisions: int
    degraded: bool
    exclude_reason: str | None
    jsonl_path: str
    reflection_id: str | None
    wall_ms: int
    # ---- MOD-08 桥接（镜像 ``evals.metrics.EpisodeResult`` 的字段名）--------------
    kind_counts: tuple[int, ...] = ()
    slot_counts: Mapping[str, int] = field(default_factory=dict)
    seq_hash: str = ""
    decision_kinds: tuple[int, ...] = ()
    decisions_llm: int = 0
    dropped: bool = False
    error_code: str | None = None
    memory_ids: tuple[str, ...] = ()
    adopted_memory_ids: tuple[str, ...] = ()
    tick: int = 0
    run_id: str = ""
    stage_count: int = 6

    @property
    def win(self) -> bool:
        return self.outcome == "win" and not self.dropped

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_key": self.episode_key, "seed": self.seed, "split": self.split,
            "arm": self.arm, "generation": self.generation,
            "harness_version": self.harness_version, "steps": self.steps,
            "decisions": self.decisions, "cum_reward": self.cum_reward,
            "outcome": self.outcome, "truncation_reason": self.truncation_reason,
            "stages_cleared": self.stages_cleared, "hearts_left": self.hearts_left,
            "battles_won": self.battles_won, "illegal_attempts": self.illegal_attempts,
            "fallback_decisions": self.fallback_decisions, "degraded": self.degraded,
            "exclude_reason": self.exclude_reason, "jsonl_path": self.jsonl_path,
            "reflection_id": self.reflection_id, "wall_ms": self.wall_ms,
            "kind_counts": list(self.kind_counts), "slot_counts": dict(self.slot_counts),
            "seq_hash": self.seq_hash, "decision_kinds": list(self.decision_kinds),
            "decisions_llm": self.decisions_llm, "dropped": self.dropped,
            "error_code": self.error_code, "tick": self.tick, "run_id": self.run_id,
            "stage_count": self.stage_count,
        }


@dataclass
class RunContext:
    """一次 ``loop run`` 的不可变环境（run 目录 + 配置 + 状态引用）。

    ``state`` 是**共享引用**：``EpisodeRunner`` 读它拿 ``generation`` / ``tick``，
    ``LoopOrchestrator`` 写它并原子落盘。
    """

    run_id: str
    paths: RunPaths
    config: Any
    state: RunState
    app_config: Any = None
    episode_config: Any = None
    harness_dir: Path = field(default_factory=lambda: Path("harness"))
    memory_version: int = 0
    memory_dir: Path | None = None
    #: MOD-05 ``MemoryStore``：代际边界快照用（FR-06-14 的 ``memory.frozen(v)`` 前置）
    memory: Any = None
    #: MOD-03 反思客户端（由组合根注入；缺失时反思降级跳过）
    client: Any = None

    def harness_version(self) -> int:
        """FR-06-7：每局开始时重读 ``version.json``（HITL 期间人工切换立即生效）。"""
        return read_harness_version(self.harness_dir)

    @property
    def loop_config(self) -> Any:
        return self.config


# =====================================================================
# EpisodeRunner
# =====================================================================

class EpisodeRunner:
    """按 FR-06-1 的时序跑一局并把每一步落盘（``episodes/<split>/<key>.jsonl``）。"""

    def __init__(self, *, env: Any = None, policy: PolicyProto | None = None,
                 memory: Any = None, writer: TrajectoryWriter | None = None,
                 config: Any = None, run_ctx: RunContext,
                 env_factory: Callable[[int, str], Any] | None = None,
                 policy_factory: Callable[[int, str], PolicyProto] | None = None,
                 close_env: bool | None = None, client: Any = None,
                 harness_version_reader: Callable[[], int] | None = None) -> None:
        self.env = env
        self.env_factory = env_factory
        self.policy = policy
        self.policy_factory = policy_factory
        self.memory = memory
        #: MOD-03 客户端（反思用；由组合根注入，缺失时反思降级跳过）
        self.client = client
        self.run_ctx = run_ctx
        self.config = config if config is not None else run_ctx.config
        self.writer = writer or TrajectoryWriter(
            run_ctx.paths,
            io_retry=int(getattr(self.config, "io_retry", 3)),
            io_backoff_ms=int(getattr(self.config, "io_backoff_ms", 200)),
            max_run_bytes=int(getattr(self.config, "max_run_bytes", 0)),
            on_retry=self._note_io_retry)
        # ``env`` 是实例 → 复用；是工厂/None → 每局新建并在结束时关闭
        self.close_env = (env is None or env_factory is not None) if close_env is None \
            else bool(close_env)
        self._harness_version_reader = harness_version_reader
        self._episode_id_support: dict[type, bool] = {}

    # ---- 内部助手 ---------------------------------------------------------
    def _note_io_retry(self, exc: InfraError) -> None:
        self.run_ctx.state.counters.io_retries += 1

    def _resolve_policy(self, seed: int, key: str) -> PolicyProto:
        """评估局并发时每个局必须拿到**自己的** ``PolicyAgent``（MOD-04 非线程安全）。

        共享的是 MOD-03 的推理队列与 MOD-05 的只读冻结视图（FR-06-14 / P-10）。
        """
        if self.policy_factory is not None:
            return self.policy_factory(int(seed), key)
        if self.policy is None:
            raise InfraError(E.E_IO_CONFIG_INVALID, "EpisodeRunner 需要 policy",
                             hint="注入 MOD-04 PolicyAgent / policy_factory，"
                                  "或 tests/fakes.py 的假策略")
        return self.policy

    def _current_harness_version(self) -> int:
        if self._harness_version_reader is not None:
            return int(self._harness_version_reader())
        return self.run_ctx.harness_version()

    def _resolve_env(self, seed: int, key: str) -> tuple[Any, bool]:
        """返回 ``(env, owned)``；``owned=True`` 时由本方法负责 ``close()``。"""
        if self.env_factory is not None:
            return self.env_factory(int(seed), key), True
        if self.env is not None:
            return self.env, self.close_env
        from ..sandbox.api import EpisodeConfig, make_default_env

        episode_config = self.run_ctx.episode_config
        return make_default_env(episode_config=EpisodeConfig.from_any(episode_config),
                                episode_id=key), True

    def _memory_for(self, split: str) -> Any:
        """训练局写记忆；评估局只读 ``frozen(v)`` 视图（FR-06-14 / INV-5）。"""
        if self.memory is None:
            return None
        if split == "train":
            return self.memory
        version = int(self.run_ctx.memory_version)
        frozen = getattr(self.memory, "frozen", None)
        if frozen is None:
            return None
        try:
            return frozen(version)
        except Exception: # noqa: BLE001 - 冻结快照缺失 → 本局不检索记忆（降级不阻塞）
            return None

    def _reset_env(self, env: Any, seed: int, cfg: Any, key: str) -> Any:
        """``env.reset(seed, episode_config=…, episode_id=…)``，对老签名保持兼容。

        ``episode_id`` 必须传：Observation 的 ``episodeId`` 是投影的一部分，若不设置，
        ``obs_digest`` 会带上调用方的默认值，破坏 NFR-06-1 的「不同 run_id 同 seed
        逐字节一致」。老式假环境没有该形参时退回两参调用（只影响该假环境的摘要）。
        """
        if self._accepts_episode_id(env):
            return env.reset(int(seed), episode_config=cfg, episode_id=key)
        return env.reset(int(seed), episode_config=cfg)

    def _accepts_episode_id(self, env: Any) -> bool:
        cached = self._episode_id_support.get(type(env))
        if cached is None:
            try:
                params = inspect.signature(env.reset).parameters
            except (TypeError, ValueError): # pragma: no cover - C 扩展对象
                cached = False
            else:
                cached = "episode_id" in params or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
            self._episode_id_support[type(env)] = cached
        return cached

    def _episode_config(self, episode_config: Any) -> Any:
        from ..sandbox.api import EpisodeConfig

        value = episode_config if episode_config is not None else self.run_ctx.episode_config
        return EpisodeConfig.from_any(value)

    # ---- 入口 -------------------------------------------------------------
    def run_episode(self, seed: int, *, split: str, arm: str,
                    episode_config: Any = None, harness_version: int | None = None,
                    generation: int | None = None,
                    timeout_s: float | None = None) -> RecordedEpisode:
        """FR-06-1 的严格时序：``policy.reset`` → ``env.reset`` → 决策循环 → 落盘。

        返回 ``RecordedEpisode``（协议上同时满足 MOD-08 的 ``EpisodeRunner``：它只按
        鸭子类型访问 ``as_dict()`` 与同名字段）。
        """
        if self.policy is None and self.policy_factory is None:
            raise InfraError(E.E_IO_CONFIG_INVALID, "EpisodeRunner 需要 policy",
                             hint="注入 MOD-04 PolicyAgent 或 tests/fakes.py 的假策略")
        state = self.run_ctx.state
        generation = int(state.generation if generation is None else generation)
        harness_version = int(self._current_harness_version()
                              if harness_version is None else harness_version)
        key = episode_key(seed, generation, arm)
        mode = "train" if split == "train" else "eval"
        memory = self._memory_for(split)
        cfg = self._episode_config(episode_config)
        policy = self._resolve_policy(int(seed), key)

        from ..agent.policy import EpisodeContext

        ctx = EpisodeContext(run=self.run_ctx.run_id, episode=key, seed=int(seed),
                             split=split, mode=mode, tick=int(state.tick),
                             generation=generation, harness_version=harness_version,
                             memory=memory)
        started = time.perf_counter()
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        events.emit("loop.episode_start", episode_key=key, seed=int(seed), split=split,
                    arm=arm, generation=generation, harness_version=harness_version)

        env, owned = self._resolve_env(int(seed), key)
        steps = illegal = fallback = 0
        kind_counts = [0] * len(KIND_SPACE)
        decision_kinds: list[int] = []
        slot_counts: dict[str, int] = {}
        actions: list[dict[str, Any]] = []
        used: list[str] = []
        adopted: list[str] = []
        cum_reward = 0.0
        degraded = False
        llm_decisions = 0
        timed_out = False
        last: Any = None
        obs: Mapping[str, Any] = {}

        try:
            policy.reset(ctx)                                        # 1) reset
            obs = self._reset_env(env, int(seed), cfg, key)          # 2) env.reset
            with self.writer.episode(split=split, episode_key=key, seed=int(seed),
                                     arm=arm, generation=generation,
                                     harness_version=harness_version, mode=mode) as w:
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
                        break
                    decision = policy.play_decision(env, obs, ctx=ctx)
                    records = list(getattr(decision, "steps", ()) or ())
                    if not records: # MOD-04 契约：一个决策点至少产出一个 StepRecord
                        raise InfraError(
                            E.E_EVAL_EPISODE,
                            f"play_decision 未产出任何 StepRecord（{key}）",
                            hint="MOD-04 契约要求每个决策点至少执行/flush 一个动作或 reject 行")
                    meta_index = first_executed_index(records)
                    llm_meta = StepMeta.of_decision(decision)
                    for index, record in enumerate(records):
                        cum_reward += float(getattr(record.result, "reward", 0.0))
                        w.write_step(record, cum_reward=cum_reward,
                                     meta=llm_meta if index == meta_index else None)
                        if bool(getattr(record, "illegal", False)):
                            illegal += 1
                            continue
                        steps += 1
                        action = getattr(record, "action", None) or {}
                        actions.append(dict(action))
                        kind = kind_index(action)
                        if kind >= 0:
                            kind_counts[kind] += 1
                            decision_kinds.append(kind)
                        slot = slot_key(action, obs)
                        if slot:
                            slot_counts[slot] = slot_counts.get(slot, 0) + 1
                    if any(str(getattr(rec, "source", "")) in ("llm", "fixed_prompt")
                           for rec in records):
                        llm_decisions += 1
                    fallback += 1 if str(getattr(decision, "source", "")) == "fallback" else 0
                    degraded = degraded or bool(getattr(decision, "degraded", False))
                    used.extend(str(m) for m in (getattr(decision, "memory_ids", ()) or ()))
                    adopted.extend(str(m) for m in
                                   (getattr(decision, "adopted_memory_ids", ()) or ()))
                    last = getattr(decision, "last", None)
                    if last is not None:
                        obs = getattr(last, "observation", obs) or obs
                    # 3) 终止判定：只看 MOD-02 的 terminated / truncated（FR-06-1）
                    if last is not None and (bool(getattr(last, "terminated", False))
                                             or bool(getattr(last, "truncated", False))):
                        break
                info = _last_info(last, obs)
                outcome = "lose" if timed_out else (
                    "win" if str(info.get("outcome")) == "win" else "lose")
                truncation = (str(info.get("truncation_reason"))
                              if info.get("truncation_reason") else None)
                exclude_reason = "not_for_eval" if degraded else None
                w.write_end({
                    "outcome": outcome, "truncation_reason": truncation,
                    "stages_cleared": int(info.get("stages_cleared") or 0),
                    "hearts_left": int(info.get("hearts_left") or 0),
                    "battles_won": int(info.get("battles_won") or 0),
                    "decisions": int(obs.get("decisionIndex") or 0),
                    "degraded": bool(degraded), "exclude_reason": exclude_reason,
                    "reward": cum_reward, "cum_reward": cum_reward, "illegal": bool(illegal),
                    "x_steps": steps, "x_illegal_attempts": illegal,
                    "x_fallback_decisions": fallback, "x_decisions_llm": llm_decisions,
                    "x_dropped": timed_out, "x_kind_counts": kind_counts,
                    "x_decision_kinds": decision_kinds, "x_slot_counts": slot_counts,
                    "x_seq_hash": seq_hash(actions),
                })
            jsonl_path = str(self.writer.path_for(split, key))
        finally:
            if owned:
                close = getattr(env, "close", None)
                if callable(close):
                    close()

        wall_ms = int(round((time.perf_counter() - started) * 1000.0))
        record = RecordedEpisode(
            episode_key=key, seed=int(seed), split=split, arm=arm,
            generation=generation, harness_version=harness_version, steps=steps,
            decisions=int(obs.get("decisionIndex") or 0), cum_reward=cum_reward,
            outcome=outcome, truncation_reason=truncation,
            stages_cleared=int(info.get("stages_cleared") or 0),
            hearts_left=int(info.get("hearts_left") or 0),
            battles_won=int(info.get("battles_won") or 0),
            illegal_attempts=illegal, fallback_decisions=fallback, degraded=bool(degraded),
            exclude_reason=exclude_reason, jsonl_path=jsonl_path, reflection_id=None,
            wall_ms=wall_ms, kind_counts=tuple(kind_counts),
            slot_counts=dict(slot_counts), seq_hash=seq_hash(actions),
            decision_kinds=tuple(decision_kinds), decisions_llm=llm_decisions,
            dropped=timed_out,
            error_code=(str(E.E_EVAL_TIMEOUT) if timed_out else None),
            memory_ids=tuple(dict.fromkeys(used)),
            adopted_memory_ids=tuple(dict.fromkeys(adopted)), tick=int(state.tick),
            run_id=str(self.run_ctx.run_id),
            stage_count=int(getattr(cfg, "stage_count", 6) or 6))
        if split == "train":
            # FR-06-3 第 1 步：局末回写本局所有决策的记忆使用情况（Spec §4.7）
            self._record_usage(record)
        state.counters.illegal_actions += illegal
        if degraded:
            state.counters.llm_timeouts += 1
        events.emit("loop.episode_end", episode_key=key, seed=int(seed), split=split,
                    arm=arm, outcome=record.outcome, truncation_reason=truncation,
                    steps=steps, decisions=record.decisions, cum_reward=round(cum_reward, 4),
                    stages_cleared=record.stages_cleared, hearts_left=record.hearts_left,
                    battles_won=record.battles_won, illegal_attempts=illegal,
                    fallback_decisions=fallback, degraded=bool(degraded),
                    dropped=timed_out, wall_ms=wall_ms)
        return record

    # ---- 记忆回写（E_IO_MEMORY_WRITE_FAILED 重试 ≤2 后跳过，不阻塞）--------------
    def _record_usage(self, record: RecordedEpisode) -> None:
        if self.memory is None or not record.memory_ids:
            return
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self.memory.record_usage(record.episode_key, list(record.memory_ids),
                                         record.outcome,
                                         adopted_ids=list(record.adopted_memory_ids),
                                         tick=int(record.tick))
                return
            except Exception as exc: # noqa: BLE001 - 记忆是增强项，不阻塞主循环
                last_error = exc
                if attempt < 2:
                    time.sleep(0.05 * (2 ** attempt))
        events.emit("loop.reflect", episode_key=record.episode_key, status="skipped",
                    reason=str(E.E_IO_MEMORY_WRITE_FAILED), detail=repr(last_error))


def _last_info(last: Any, obs: Mapping[str, Any]) -> dict[str, Any]:
    """最近一次 ``StepResult.info``（主指标白名单，INV-7）；无步时退回观测。"""
    info = getattr(last, "info", None)
    if info:
        return dict(info)
    return {"outcome": obs.get("outcome"), "truncation_reason": None,
            "stages_cleared": 0, "hearts_left": 0, "battles_won": 0}


# =====================================================================
# 评估执行器桥（FR-06-12：把 MOD-06 的 runner 注入 MOD-08 evaluate_arm）
# =====================================================================

class EvalRunnerAdapter:
    """把 ``EpisodeRunner`` 适配为 MOD-08 的 ``EpisodeRunner`` 协议。

    MOD-08 的 ``run_eval`` 需要 ``EpisodeResult``（用于聚合与多样性），而 MOD-06 的
    runner 返回 ``RecordedEpisode``。适配器**只做字段搬运，不做任何聚合**
    （聚合归 MOD-08），并且通过 ``read_jsonl`` 读回刚落的轨迹，保证
    ``episode_result_from_rows`` 是唯一的口径来源。
    """

    def __init__(self, runner: EpisodeRunner) -> None:
        self.runner = runner

    def run_episode(self, seed: int, *, split: str, arm: str, generation: int,
                    harness_version: int, episode_config: Any = None,
                    timeout_s: float | None = None) -> Any:
        from ..evals.harness import episode_result_from_rows
        from ..infra.io import read_jsonl

        record = self.runner.run_episode(int(seed), split=split, arm=arm,
                                         episode_config=episode_config,
                                         harness_version=int(harness_version),
                                         generation=int(generation), timeout_s=timeout_s)
        rows, _truncated = read_jsonl(Path(record.jsonl_path))
        result = episode_result_from_rows(rows, seed=int(seed), split=split,
                                          generation=int(generation),
                                          harness_version=int(harness_version), arm=arm)
        if result is None: # pragma: no cover - run_episode 必定写 episode_end 行
            raise InfraError(E.E_EVAL_EPISODE,
                             f"轨迹缺少 episode_end 行：{record.jsonl_path}")
        return result
