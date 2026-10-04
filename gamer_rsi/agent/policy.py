"""MOD-04 ``PolicyAgent``：一个决策点的计划、重试、fallback 编排（PRD §8）。

对外唯一决策入口是 ``play_decision(env, obs, ctx)``：

- 组装 prompt（``prompts/assembler``）→ 强制泄漏扫描（MOD-02 ``audit``）→ MOD-03
  ``generate_json`` → ① 语法/schema → ② 逐个动作的当前 ``legal_actions()`` → ③ ``step()``；
- 每个决策最多 ``1 + policy.retry_max`` 次尝试，每次失败都走 ``env.reject(final=False)``
  触发轻罚，并把 ``error_code + error_detail`` 写进重试的 ``{{constraints}}``；
- 尝试耗尽 → ``env.reject(final=True)`` + 启发式出招（``source="fallback"``）；
  模型不可用/超时 → 立即启发式并标 ``degraded=True``（该局不计入评估样本）；
- **判负只由 MOD-02 负责**（连续 ``final=True`` → ``truncation_reason="policy_failure"``），
  本模块不判负、不写日志、不落盘（NFR-04-4：只产出事件与字段）。
"""

from __future__ import annotations

import concurrent.futures
import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from gamer_rsi.infra import events as _events
from gamer_rsi.infra.config import derive_seed
from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.sandbox import audit as sandbox_audit
from gamer_rsi.sandbox.api import SandboxEnv, StepResult
from gamer_rsi.sandbox.projection import obs_digest, render_compact

from .heuristic import heuristic_policy_explained
from .parser import (canonical_action, decision_schema_dict, parse_llm_output,
                     semantic_match)
from .prompts.assembler import (FIXED_TEMPLATE_PATH, MemoryItem, PromptBundle,
                                PromptContext, PromptSpec, assemble_prompt, load_task_text,
                                load_template, resolve_path)
from .tools.registry import ToolRegistry, load_harness_tools, tool_hash, tool_summary

__all__ = ["StepRecord", "DecisionResult", "LLMCallMeta", "EpisodeContext", "PolicyAgent",
           "InferenceClient", "MemoryReader", "BackendInferenceClient", "coerce_client",
           "FORMAT_REPAIR_CONSTRAINTS"]

#: 重试时写入 ``{{constraints}}`` 的前缀（FR-04-9：只放 error_code + error_detail）。
FORMAT_REPAIR_CONSTRAINTS: Final[str] = (
    "上一次输出被拒绝。请严格按输出契约重新输出一个 JSON 对象，"
    "不要重复上次的无效动作。")

#: 触发全局降级（整局 degraded）的 MOD-03 错误码（Spec §8.5，P-12）。
_GLOBAL_DEGRADE_CODES: Final[frozenset[str]] = frozenset({
    E.E_LLM_UNAVAILABLE, E.E_LLM_TIMEOUT, E.E_LLM_OOM, E.E_LLM_LOAD_FAILED,
    E.E_LLM_BACKEND_ERROR,
})


# ----------------------------------------------------------------------
# 数据模型（PRD §6/§7）
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class StepRecord:
    """一个 env 步（含 ``reject`` 行）；MOD-06 据此写轨迹行（Spec §4.5）。"""

    action: dict[str, Any] | None
    result: StepResult
    plan_index: int
    attempt: int
    source: str # "llm" | "fallback" | "heuristic" | "fixed_prompt"
    illegal: bool
    illegal_reason: str | None = None

    @property
    def reward(self) -> float:
        return float(self.result.reward)


@dataclass(frozen=True)
class LLMCallMeta:
    """一次决策里**第一次成功的 LLM 调用**的元数据（只挂一次，Spec §4.5）。"""

    raw: str = ""
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: int = 1
    prompt_digest: str | None = None
    model_id: str | None = None
    backend: str | None = None


@dataclass(frozen=True)
class DecisionResult:
    """一个决策点的完整结果（PRD §6 / Spec §4.7）。"""

    steps: list[StepRecord]
    source: str
    attempts: int
    llm: LLMCallMeta | None
    memory_ids: list[str]
    adopted_memory_ids: list[str]
    prompt_hash: str
    tool_hash: str | None
    reasoning: str
    last: StepResult
    error_codes: list[str]
    degraded: bool = False
    mode: str = "play"
    template_hash: str = ""

    @property
    def illegal(self) -> bool:
        return any(step.illegal for step in self.steps)


@dataclass(frozen=True)
class EpisodeContext:
    """MOD-06 每局注入的上下文（Spec §4.7 / MOD-06 §8）。

    ``memory`` 为 MOD-05 的 ``MemoryStore`` 或冻结视图；留 ``None`` 表示本局不检索记忆。
    """

    run: str = "r-local"
    episode: str = "e-local"
    seed: int = 0
    split: str = "train"
    mode: str = "train" # MOD-03 采样模式：train | eval
    tick: int = 0
    generation: int = 0
    harness_version: int = 0
    memory: Any = None
    reflections: tuple[ReflectionItem, ...] = ()


@runtime_checkable
class InferenceClient(Protocol):
    """MOD-04 → MOD-03 的同步门面（真实实现在 ``BackendInferenceClient``）。"""

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], *,
                      mode: str, sampling: Mapping[str, Any], seed: int) -> Any: ...


@runtime_checkable
class MemoryReader(Protocol):
    """MOD-04 → MOD-05 的只读检索门面（Spec §4.7）。"""

    def retrieve(self, obs: Mapping[str, Any], *, top_k: int, token_budget: int,
                 tick: int) -> Any: ...


#: 持久后台事件循环（``_run_sync`` 用）。MOD-03 的 ``BackendManager`` 在内部缓存
#: ``asyncio.Lock`` / ``Semaphore``，它们一旦在某个 loop 上被 await 就与该 loop 绑定；
#: 若每次调用都 ``asyncio.run()`` 新建 loop，第二次调用即 ``bound to a different event
#: loop``。因此所有同步调用共用一个常驻 loop（M1 验收 F1 的实跑路径依赖这一点）。
_LOOP_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None


def _background_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP is not None and not _LOOP.is_closed():
            return _LOOP
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="gamer-rsi-inference",
                                  daemon=True)
        thread.start()
        _LOOP, _LOOP_THREAD = loop, thread
        return loop


def _run_sync(coro: Any) -> Any:
    """在同步上下文中执行 MOD-03 的协程（CLI/loop 均为同步编排）。

    - 已在事件循环内（如被异步调用方驱动）→ 丢到一次性线程里，避免嵌套 ``run_until_complete``；
    - 否则 → 提交到常驻后台 loop，保证 MOD-03 的锁/信号量始终绑在同一个 loop 上。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run_coroutine_threadsafe(coro, _background_loop()).result()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool: # pragma: no cover
        return pool.submit(lambda: asyncio.run(coro)).result()


class BackendInferenceClient:
    """把 MOD-03 的异步后端适配为 MOD-04 需要的同步 ``InferenceClient``。

    ``backend`` 可以是 ``Backend``（``generate_json(messages, schema, **kw)``）或
    ``BackendManager``（``generate_json(model_id, messages, schema, **kw)``，需给 ``model_id``）。
    """

    def __init__(self, backend: Any, *, model_id: str | None = None,
                 total_timeout_ms: int | None = None, max_attempts: int | None = None) -> None:
        self.backend = backend
        self.model_id = model_id
        self.total_timeout_ms = total_timeout_ms
        self.max_attempts = max_attempts

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], *,
                      mode: str, sampling: Mapping[str, Any], seed: int) -> Any:
        kwargs = dict(sampling)
        kwargs["seed"] = int(seed)
        if self.total_timeout_ms is not None:
            kwargs["total_timeout_ms"] = int(self.total_timeout_ms)
        if self.max_attempts is not None:
            kwargs["max_attempts"] = int(self.max_attempts)
        if self.model_id is not None:
            coro = self.backend.generate_json(self.model_id, list(messages), schema, **kwargs)
        else:
            coro = self.backend.generate_json(list(messages), schema, **kwargs)
        return _run_sync(coro)


def coerce_client(llm: Any, *, model_id: str | None = None) -> Any:
    """把 MOD-03 的后端对象自动适配为同步 ``InferenceClient``。

    - ``BackendManager``（``registry/session`` 形状）→ 需要 ``model_id``（显式参数、
      对象属性或 ``config.model_id``）；拿不到即 fail-fast，避免把 ``messages`` 误传给
      ``model_id`` 形参。
    - 单个 ``Backend``（有 ``name`` / ``model_id``）→ 直接包装。
    - 其它对象按鸭子类型原样返回（测试用假 client 走这条路径）。
    """
    if llm is None:
        return None
    try: # pragma: no cover - 依赖 MOD-03 类型
        from gamer_rsi.inference import BackendManager

        if isinstance(llm, BackendManager):
            resolved = model_id or getattr(llm, "model_id", None) \
                or getattr(getattr(llm, "config", None), "model_id", None)
            if not resolved:
                raise InfraError(
                    E.E_LLM_BACKEND_ERROR,
                    "BackendManager 需要显式 model_id 才能适配为 MOD-04 的 InferenceClient",
                    hint="用 BackendInferenceClient(manager, model_id='qwen3-8b-q4') 构造，"
                         "或在 manager.config.model_id 中配置")
            return BackendInferenceClient(llm, model_id=str(resolved))
    except InfraError:
        raise
    except Exception: # noqa: BLE001 - 导入失败时按鸭子类型处理
        return llm
    if hasattr(llm, "name") and hasattr(llm, "model_id"):
        return BackendInferenceClient(llm)
    return llm


# ----------------------------------------------------------------------
# PolicyAgent
# ----------------------------------------------------------------------

class PolicyAgent:
    """MOD-04 的唯一对外决策入口（Spec §4.7）。"""

    def __init__(self, cfg: Any, llm: Any = None, memory: Any = None, *,
                 harness_dir: Path | str = Path("harness"),
                 tools: ToolRegistry | None = None,
                 guard: Any | None = None,
                 forbidden: Mapping[str, str] | None = None,
                 scanner: Any | None = None) -> None:
        self.cfg = cfg
        self.llm = coerce_client(llm)
        self.memory = memory
        self.harness_dir = Path(harness_dir)
        self._guard = guard
        self._scanner = scanner
        self._forbidden = dict(forbidden) if forbidden is not None else None
        self._tools: ToolRegistry | None = tools
        self._ctx: EpisodeContext = EpisodeContext()
        self._spec: PromptSpec | None = None
        self._task_text: str | None = None
        self._harness_version: int | None = None
        self._memory_payload: list[dict[str, Any]] = []
        self._last_items: tuple[MemoryItem, ...] = ()
        self._last_template_error = False
        self._last_tool_codes: list[str] = []

    # ---- 生命周期 ---------------------------------------------------------
    def reset(self, ctx: EpisodeContext) -> None:
        """每局开始（Spec §4.7）；模板/工具在代际边界随新 ``PolicyAgent`` 重建。"""
        self._ctx = ctx
        if ctx.memory is not None:
            self.memory = ctx.memory
        self._memory_payload = []
        self._last_items = ()
        if ctx.harness_version:
            self._harness_version = int(ctx.harness_version)

    def close(self) -> None:
        """释放工具 worker（幂等）。"""
        if self._tools is not None:
            self._tools.close()
            self._tools = None

    def __enter__(self) -> "PolicyAgent":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 只读属性 ---------------------------------------------------------
    @property
    def harness_version(self) -> int:
        if self._harness_version is None:
            self._harness_version = self._read_harness_version()
        return int(self._harness_version)

    @property
    def template_spec(self) -> PromptSpec:
        return self._ensure_spec()

    # ---- 对外：prompt -----------------------------------------------------
    def prompt_bundle(self, obs: Mapping[str, Any], *, ctx: EpisodeContext | None = None,
                      legal: Sequence[Mapping[str, Any]] | None = None,
                      attempt: int = 1,
                      feedback: tuple[str, str] | None = None) -> PromptBundle:
        """组装一个决策点的 prompt（INV-3 唯一注入出口，FR-04-5）。"""
        ctx = ctx or self._ctx
        spec = self._ensure_spec()
        items, payload = self._retrieve(obs, ctx)
        self._last_items = items
        self._memory_payload = payload
        constraints = ""
        if feedback is not None:
            code, detail = feedback
            constraints = (f"{FORMAT_REPAIR_CONSTRAINTS}\n"
                           f"- error_code: {code}\n- error_detail: {detail}")
        prompt_ctx = PromptContext(
            observation=render_compact(obs),
            task=self._task(),
            harness_version=self.harness_version,
            tools=tool_summary(self.harness_dir),
            legal_actions=_render_legal(legal),
            reflections=tuple(ctx.reflections or ()),
            memory=items,
            constraints=constraints,
            obs_digest=obs_digest(obs),
            token_budget=int(self.cfg.prompt_token_budget),
            token_estimator=str(self.cfg.token_estimator),
            reflection_max=int(self.cfg.reflection_max),
            memory_top_k=int(self.cfg.memory_top_k),
            mode=self._llm_mode(ctx),
        )
        return assemble_prompt(spec, prompt_ctx)

    # ---- 对外：唯一决策入口 -------------------------------------------------
    def play_decision(self, env: SandboxEnv, obs: Mapping[str, Any], *,
                      ctx: EpisodeContext | None = None) -> DecisionResult:
        """执行一个决策点（计划 + 重试 + fallback）；**不抛模型/工具类异常**。"""
        ctx = ctx or self._ctx
        self._ctx = ctx
        if self.cfg.mode == "baseline_heuristic":
            return self._heuristic_decision(env, obs, ctx, source="heuristic")
        try:
            return self._play_llm(env, dict(obs), ctx)
        except InfraError as exc: # 环境层契约异常：尽力降级，不静默吞掉
            code = str(exc.code)
            if not code.startswith("E_ENV_"):
                raise
            try:
                return self._fallback_decision(
                    env, dict(obs), ctx, [], 0, None, [code], final=False, degraded=True)
            except Exception: # noqa: BLE001 - 降级也失败 → 交回调用方（fail-closed）
                raise exc

    # ---- 内部：LLM 分支 ---------------------------------------------------
    def _play_llm(self, env: SandboxEnv, obs: dict[str, Any],
                  ctx: EpisodeContext) -> DecisionResult:
        cfg = self.cfg
        max_attempts = 1 + max(0, int(cfg.retry_max))
        steps: list[StepRecord] = []
        error_codes: list[str] = []
        bundle: PromptBundle | None = None
        llm_meta: LLMCallMeta | None = None
        reasoning = ""
        feedback: tuple[str, str] | None = None
        current = obs
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            bundle = self.prompt_bundle(current, ctx=ctx, legal=env.legal_actions(),
                                        attempt=attempts, feedback=feedback)
            error_codes.extend(bundle.error_codes)
            if E.E_ACTION_BUDGET in bundle.error_codes:
                # Observation 单独超预算 → 丢光记忆仍装不下（§10）→ 直接启发式
                return self._fallback_decision(env, current, ctx, steps, attempts, bundle,
                                               error_codes, final=False, degraded=False)
            if cfg.leak_scan is False: # pragma: no cover - PolicyConfig.validate 已拦截
                raise InfraError(E.E_IO_CONFIG_INVALID,
                                 "policy.leak_scan 恒必须为 true（INV-5 / FR-04-6）")
            findings = self._scan(bundle, ctx, current)
            if findings:
                error_codes.append(E.E_ACTION_LEAK_BLOCK)
                self._emit_audit_leak(findings, ctx, current)
                break # → final reject + 启发式（FR-04-14 触发条件之一）

            try:
                out = self._generate(bundle, ctx, attempt=attempts, obs=current)
            except Exception as exc: # noqa: BLE001 - 只处理 MOD-03 的 InferenceError
                code = str(getattr(exc, "code", ""))
                if not code:
                    raise
                if llm_meta is None:
                    llm_meta = LLMCallMeta(raw="", attempts=attempts)
                error_codes.append(code)
                if code in _GLOBAL_DEGRADE_CODES:
                    return self._fallback_decision(env, current, ctx, steps, attempts, bundle,
                                                   error_codes, final=False, degraded=True)
                detail = str(getattr(exc, "message", "") or exc)[:300]
                result = self._reject(env, "syntax", f"{code}: {detail}", None)
                steps.append(self._record(None, result, -1, attempts, "llm", True, code))
                current = dict(result.observation)
                feedback = (code, detail)
                continue

            text = _out_text(out)
            if llm_meta is None:
                llm_meta = _call_meta(out, text, attempts)
            parsed = parse_llm_output(text, phase=str(current.get("phase") or ""),
                                      reasoning_max_chars=int(cfg.reasoning_max_chars),
                                      max_plan_len=int(cfg.max_plan_len))
            if parsed.reasoning:
                reasoning = parsed.reasoning
            if not parsed.ok:
                code = str(parsed.error_code or E.E_ACTION_PARSE)
                error_codes.append(code)
                stage = "semantic" if parsed.level == "semantic" else "syntax"
                detail = str(parsed.error_detail or "")
                result = self._reject(env, stage, f"{code}: {detail}", text)
                steps.append(self._record(None, result, -1, attempts, "llm", True, code))
                current = dict(result.observation)
                feedback = (code, detail[:300])
                continue

            plan = list(parsed.plan[: int(cfg.max_plan_len)])
            if self._tools_enabled():
                plan = self._reorder(env, current, plan, error_codes)
            plan_obs = dict(current)
            steps, current, ok, feedback = self._execute_plan(
                env, steps, current, plan, ctx, attempts, error_codes)
            if ok:
                return self._finish(steps, "llm", attempts, llm_meta, bundle, ctx,
                                    reasoning, error_codes, degraded=False, obs=plan_obs)

        # 尝试耗尽 或 泄漏阻断（FR-04-10）
        reason = E.E_ACTION_LEAK_BLOCK if E.E_ACTION_LEAK_BLOCK in error_codes \
            else "attempts_exhausted"
        result = self._reject(env, "semantic", reason, None, final=True)
        steps.append(self._record(None, result, -1, max(1, attempts), "llm", True,
                                  reason))
        if E.E_ACTION_FALLBACK not in error_codes:
            error_codes.append(E.E_ACTION_FALLBACK)
        self._note_streak(result, error_codes)
        current = dict(result.observation)
        return self._fallback_decision(env, current, ctx, steps, max(1, attempts), bundle,
                                       error_codes, final=True, degraded=False)

    def _execute_plan(self, env: SandboxEnv, steps: list[StepRecord], current: dict[str, Any],
                      plan: Sequence[Mapping[str, Any]], ctx: EpisodeContext, attempt: int,
                      error_codes: list[str]
                      ) -> tuple[list[StepRecord], dict[str, Any], bool,
                                 tuple[str, str] | None]:
        for index, action in enumerate(plan):
            legal = env.legal_actions() # FR-04-8：每个动作执行前重取，禁止缓存
            valid, detail, warning = semantic_match(action, legal)
            if not valid:
                error_codes.append(E.E_ACTION_ILLEGAL)
                result = self._reject(env, "semantic", str(detail), canonical_action(action))
                steps.append(self._record(None, result, index, attempt, "llm", True,
                                          E.E_ACTION_ILLEGAL))
                return steps, dict(result.observation), False, (E.E_ACTION_ILLEGAL, str(detail)[:300])
            if warning:
                error_codes.append(warning)
            result = env.step(action)
            illegal = bool(result.info.get("illegal"))
            steps.append(self._record(dict(action), result, index, attempt, "llm", illegal,
                                      result.info.get("illegal_reason")))
            current = dict(result.observation)
            if illegal: # ③ 执行层：消费 MOD-01 裁决，不改判
                error_codes.append(E.E_ACTION_EXEC)
                return (steps, current, False,
                        (E.E_ACTION_EXEC, str(result.info.get("illegal_reason") or "")[:300]))
            if result.terminated or result.truncated:
                return steps, current, True, None
            if str(current.get("phase") or "") != "player_turn":
                # 离开玩家回合 → 剩余动作直接丢弃，不计非法（FR-04-7）
                return steps, current, True, None
        return steps, current, True, None

    # ---- 内部：启发式分支 --------------------------------------------------
    def _heuristic_decision(self, env: SandboxEnv, obs: Mapping[str, Any],
                            ctx: EpisodeContext, *, source: str) -> DecisionResult:
        action, reasoning = self._heuristic(env, obs)
        steps: list[StepRecord] = []
        error_codes: list[str] = []
        if action:
            result = env.step(action)
            steps.append(self._record(action, result, 0, 1, source,
                                      bool(result.info.get("illegal")),
                                      result.info.get("illegal_reason")))
        else:
            result = self._reject(env, "semantic", "no_legal_action", None, final=True)
            steps.append(self._record(None, result, -1, 1, source, True, E.E_ACTION_FALLBACK))
            error_codes.append(E.E_ACTION_FALLBACK)
            self._note_streak(result, error_codes)
        return DecisionResult(steps=steps, source=source, attempts=1, llm=None,
                              memory_ids=[], adopted_memory_ids=[], prompt_hash="",
                              tool_hash=self._tool_hash(), reasoning=reasoning,
                              last=steps[-1].result, error_codes=error_codes,
                              mode=self._decision_mode(ctx))

    def _fallback_decision(self, env: SandboxEnv, obs: Mapping[str, Any],
                           ctx: EpisodeContext, prior_steps: Sequence[StepRecord],
                           attempts: int, bundle: PromptBundle | None,
                           error_codes: Sequence[str], *, final: bool,
                           degraded: bool) -> DecisionResult:
        steps = list(prior_steps)
        source = "fallback"
        codes = list(dict.fromkeys(error_codes))
        self._emit("policy.fallback", reason=str(error_codes[-1]) if error_codes else "fallback",
                   episode_key=ctx.episode, degraded=bool(degraded))
        if not bool(getattr(self.cfg, "heuristic_fallback", True)):
            # 仅供消融：关掉启发式后失败即结束（判负仍由 MOD-02 的 final 连续计数负责）
            if not steps:
                result = self._reject(env, "semantic", "heuristic_fallback_disabled", None,
                                      final=True)
                steps.append(self._record(None, result, -1, max(1, attempts), source, True,
                                          E.E_ACTION_FALLBACK))
            self._note_streak(steps[-1].result, codes)
            return DecisionResult(steps=steps, source=source, attempts=max(1, attempts),
                                  llm=None, memory_ids=list(bundle.memory_ids) if bundle else [],
                                  adopted_memory_ids=[],
                                  prompt_hash=bundle.prompt_hash if bundle else "",
                                  tool_hash=self._tool_hash(),
                                  reasoning="heuristic fallback disabled",
                                  last=steps[-1].result, error_codes=codes,
                                  degraded=degraded, mode=self._decision_mode(ctx),
                                  template_hash=bundle.template_hash if bundle else "")
        action, reasoning = self._heuristic(env, obs)
        if action:
            result = env.step(action)
            steps.append(self._record(action, result, 0, max(1, attempts), source,
                                      bool(result.info.get("illegal")),
                                      result.info.get("illegal_reason")))
        else:
            result = self._reject(env, "semantic", "no_legal_action", None, final=True)
            steps.append(self._record(None, result, -1, max(1, attempts), source, True,
                                      E.E_ACTION_FALLBACK))
        self._note_streak(steps[-1].result, codes)
        return DecisionResult(steps=steps, source=source, attempts=max(1, attempts), llm=None,
                              memory_ids=list(bundle.memory_ids) if bundle else [],
                              adopted_memory_ids=[], prompt_hash=bundle.prompt_hash if bundle else "",
                              tool_hash=self._tool_hash(), reasoning=reasoning,
                              last=steps[-1].result,
                              error_codes=codes, degraded=degraded,
                              mode=self._decision_mode(ctx),
                              template_hash=bundle.template_hash if bundle else "")

    # ---- 内部：LLM 调用与采样 ---------------------------------------------
    def _generate(self, bundle: PromptBundle, ctx: EpisodeContext, *, attempt: int,
                  obs: Mapping[str, Any]) -> Any:
        if self.llm is None:
            from gamer_rsi.inference import InferenceError

            raise InferenceError(E.E_LLM_UNAVAILABLE, "PolicyAgent 未注入 InferenceClient",
                                 hint="构造时传入 llm 或使用 BackendInferenceClient")
        mode = self._llm_mode(ctx)
        decision_index = int(obs.get("decisionIndex") or 0)
        sampling = self._sampling(ctx, mode=mode, attempt=attempt,
                                  decision_index=decision_index)
        return self.llm.generate_json(bundle.messages, decision_schema_dict(), mode=mode,
                                      sampling=sampling, seed=int(sampling["seed"]))

    def _sampling(self, ctx: EpisodeContext, *, mode: str, attempt: int,
                  decision_index: int) -> dict[str, Any]:
        """采样参数（FR-04-15）：``eval`` 恒 0.0；``play`` clamp 到 0.7–1.2；seed 显式派生。"""
        cfg = self.cfg
        if mode == "eval":
            temperature = 0.0
            top_p = 1.0
        else:
            temperature = min(1.2, max(0.7, float(cfg.temperature)))
            top_p = float(cfg.top_p)
        seed = derive_seed("policy", str(cfg.sampling_seed_salt), str(ctx.run),
                           str(ctx.episode), int(ctx.seed), int(decision_index),
                           int(attempt), int(self.harness_version))
        return {"temperature": temperature, "top_p": top_p,
                "max_tokens": int(cfg.max_tokens), "seed": int(seed)}

    def _llm_mode(self, ctx: EpisodeContext) -> str:
        """``train`` / ``eval``：评估口不得被 harness 覆盖（B-14）。"""
        if str(self.cfg.mode) in ("eval", "baseline_fixed_prompt"):
            return "eval"
        if str(ctx.mode) == "eval":
            return "eval"
        if str(ctx.split) in ("heldout", "test"):
            return "eval"
        return "train"

    def _decision_mode(self, ctx: EpisodeContext) -> str:
        """``DecisionResult.mode`` = 轨迹 ``mode`` 字段口径（train|eval，Spec §4.5）。"""
        if str(self.cfg.mode) == "baseline_heuristic":
            return "eval" if str(ctx.split) in ("heldout", "test") or str(ctx.mode) == "eval" \
                else "train"
        return self._llm_mode(ctx)

    # ---- 内部：工具 -------------------------------------------------------
    def _tools_enabled(self) -> bool:
        return bool(getattr(self.cfg, "tools_enabled", True))

    def _registry(self) -> ToolRegistry:
        if self._tools is None:
            self._tools = load_harness_tools(
                self.harness_dir, guard=self._guard,
                enabled=self._tools_enabled(),
                timeout_ms=int(getattr(self.cfg, "tool_timeout_ms", 50)))
        return self._tools

    def _reorder(self, env: SandboxEnv, obs: Mapping[str, Any],
                 plan: list[Mapping[str, Any]], error_codes: list[str]) -> list[dict[str, Any]]:
        registry = self._registry()
        before = list(registry.codes())
        reordered = registry.rank_actions(obs, plan, self._memory_payload)
        for code in registry.codes():
            if code not in before:
                error_codes.append(code)
        self._last_tool_codes = list(registry.codes())
        return reordered

    def _tool_hash(self) -> str | None:
        if self._tools is not None:
            return self._tools.tool_hash
        return tool_hash(self.harness_dir)

    # ---- 内部：记忆检索 ---------------------------------------------------
    def _retrieve(self, obs: Mapping[str, Any], ctx: EpisodeContext
                  ) -> tuple[tuple[MemoryItem, ...], list[dict[str, Any]]]:
        reader = ctx.memory if ctx.memory is not None else self.memory
        if reader is None:
            return (), []
        try:
            result = reader.retrieve(obs, top_k=int(self.cfg.memory_top_k),
                                     token_budget=int(getattr(self.cfg, "memory_token_budget",
                                                              _DEFAULT_MEMORY_TOKEN_BUDGET)),
                                     tick=int(ctx.tick))
        except Exception: # noqa: BLE001 - 检索失败不得阻断决策（§1 目标 4）
            return (), []
        items = tuple(_memory_items(result))
        payload = [{"memory_id": item.memory_id, "text": item.text, "score": item.score}
                   for item in items]
        return items, payload

    # ---- 内部：泄漏扫描 ---------------------------------------------------
    def _forbidden_set(self) -> Mapping[str, str]:
        if self._forbidden is None:
            visible = sandbox_audit.VisibleSet(
                obs_paths=(), obs_digests=(), memory_texts=(),
                harness_version=self.harness_version, seeds_seen=())
            self._forbidden = dict(sandbox_audit.build_forbidden(visible))
        return self._forbidden

    def _scan(self, bundle: PromptBundle, ctx: EpisodeContext,
              obs: Mapping[str, Any]) -> list[Any]:
        """FR-04-6：每次 LLM 调用前强制扫描；``policy.leak_scan`` 恒 true。"""
        if self._scanner is not None:
            return list(self._scanner(bundle.text, surface="prompt"))
        return list(sandbox_audit.scan_text(
            bundle.text, forbidden=self._forbidden_set(), surface="prompt",
            episode_id=ctx.episode, step=int(obs.get("decisionIndex") or 0)))

    # ---- 内部：杂项 -------------------------------------------------------
    def _ensure_spec(self) -> PromptSpec:
        if self._spec is None:
            if str(self.cfg.mode) == "baseline_fixed_prompt":
                # 冻结基线模板必须真的冻结：配置路径不可用时用包内冻结件，
                # 而不是退化到决策兜底模板（否则 baseline 语义就变了）。
                resolved = resolve_path(self.cfg.fixed_prompt_path,
                                        harness_dir=self.harness_dir)
                if not resolved.is_file():
                    resolved = FIXED_TEMPLATE_PATH
            else:
                resolved = resolve_path(self.cfg.template_path, harness_dir=self.harness_dir)
            spec = load_template(resolved)
            self._last_template_error = spec.fallback_used
            if spec.fallback_used:
                self._emit("policy.reject", reason=E.E_ACTION_TEMPLATE, code=E.E_ACTION_TEMPLATE)
            self._spec = spec
        return self._spec

    def _task(self) -> str:
        if self._task_text is None:
            configured = str(getattr(self.cfg, "task_path", "") or "")
            path: Path | None = None
            if configured:
                candidate = resolve_path(configured, harness_dir=self.harness_dir)
                # configs/default.yaml 的默认值指向 harness/；INV-3 第 ④ 来源实为
                # gamer_rsi/agent/prompts/task.md（PRD §7），不可用时回落内置件。
                path = candidate if candidate.is_file() else None
            self._task_text = load_task_text(path)
        return self._task_text

    def _read_harness_version(self) -> int:
        for candidate in (self.harness_dir / "version.json",
                          self.harness_dir.parent / "harness" / "version.json"):
            if candidate.is_file():
                try:
                    return int(json.loads(candidate.read_text(encoding="utf-8")).get("version", 0))
                except (OSError, ValueError, TypeError):
                    return 0
        return 0

    def _heuristic(self, env: SandboxEnv, obs: Mapping[str, Any]
                   ) -> tuple[dict[str, Any], str]:
        try:
            legal = env.legal_actions()
            hints = env.hints()
        except InfraError:
            raise
        return heuristic_policy_explained(obs, legal, hints)

    def _reject(self, env: SandboxEnv, stage: str, reason: str, raw: str | None,
                *, final: bool = False) -> StepResult:
        result = env.reject(stage=stage, reason=str(reason)[:400], raw=raw, final=final)
        self._emit("policy.reject", reason=str(reason)[:200], stage=stage, final=bool(final),
                   episode_key=self._ctx.episode)
        return result

    def _note_streak(self, result: StepResult, error_codes: list[str]) -> None:
        """只读 MOD-02 的 ``info``：连续 final 判负后补记 ``E_ACTION_ILLEGAL_STREAK``。

        判负本身仍**只由 MOD-02 负责**（FR-04-10）；MOD-04 只是把这个既成事实写进
        ``DecisionResult.error_codes``，供 MOD-06/08 记账（§10 E_ACTION_ILLEGAL_STREAK）。
        """
        info = result.info if isinstance(result.info, Mapping) else {}
        if str(info.get("truncation_reason") or "") == "policy_failure" \
                and E.E_ACTION_ILLEGAL_STREAK not in error_codes:
            error_codes.append(E.E_ACTION_ILLEGAL_STREAK)

    def _record(self, action: Mapping[str, Any] | None, result: StepResult, plan_index: int,
                attempt: int, source: str, illegal: bool,
                illegal_reason: Any) -> StepRecord:
        return StepRecord(action=dict(action) if action is not None else None, result=result,
                          plan_index=int(plan_index), attempt=int(attempt), source=source,
                          illegal=bool(illegal),
                          illegal_reason=None if illegal_reason is None else str(illegal_reason))

    def _finish(self, steps: list[StepRecord], source: str, attempts: int,
                llm_meta: LLMCallMeta | None, bundle: PromptBundle | None,
                ctx: EpisodeContext, reasoning: str, error_codes: Sequence[str],
                *, degraded: bool, obs: Mapping[str, Any] | None = None) -> DecisionResult:
        executed = [step.action for step in steps if step.action]
        adopted = self._adopted(bundle, executed, obs)
        result = DecisionResult(
            steps=steps, source=source, attempts=attempts, llm=llm_meta,
            memory_ids=list(bundle.memory_ids) if bundle else [],
            adopted_memory_ids=adopted,
            prompt_hash=bundle.prompt_hash if bundle else "",
            tool_hash=self._tool_hash(), reasoning=reasoning, last=steps[-1].result,
            error_codes=list(dict.fromkeys(error_codes)), degraded=degraded,
            mode=self._decision_mode(ctx), template_hash=bundle.template_hash if bundle else "")
        self._emit("policy.decision", episode_key=ctx.episode, source=source,
                   attempts=attempts, prompt_hash=result.prompt_hash,
                   error_codes=result.error_codes, degraded=degraded)
        return result

    def _adopted(self, bundle: PromptBundle | None,
                 executed: Sequence[Mapping[str, Any] | None],
                 obs: Mapping[str, Any] | None) -> list[str]:
        """``adopted_memory_ids``：计划中动作命中条目 ``action_hint`` 的 (kind, slotKind, school)。"""
        if bundle is None or not self._last_items:
            return []
        observation = obs or {}
        out: list[str] = []
        for item in self._last_items:
            hint = item.action_hint
            if not isinstance(hint, Mapping):
                continue
            kind = hint.get("kind")
            if not isinstance(kind, str):
                continue
            for action in executed:
                if not action or action.get("kind") != kind:
                    continue
                slot_kind = hint.get("slotKind")
                if slot_kind and not _slot_kind_matches(observation, action, str(slot_kind)):
                    continue
                school = hint.get("school")
                if school and not _school_matches(observation, action, str(school)):
                    continue
                out.append(item.memory_id)
                break
        return out

    def _emit(self, event: str, **fields: Any) -> None:
        try:
            _events.emit(event, **fields)
        except Exception: # noqa: BLE001 - 埋点失败不得改变决策结果
            pass

    def _emit_audit_leak(self, findings: Sequence[Any], ctx: EpisodeContext,
                         obs: Mapping[str, Any]) -> None:
        for finding in findings:
            self._emit("sandbox.audit_finding", severity="critical",
                       leak_class=str(getattr(finding, "leak_class", "")),
                       surface=str(getattr(finding, "surface", "prompt")),
                       token_class=str(getattr(finding, "token_class", "")),
                       evidence=str(getattr(finding, "evidence", ""))[:80],
                       episode_key=ctx.episode,
                       step=int(obs.get("decisionIndex") or 0))


# ----------------------------------------------------------------------
# 辅助纯函数
# ----------------------------------------------------------------------

_DEFAULT_MEMORY_TOKEN_BUDGET: Final[int] = 900


def _render_legal(legal: Sequence[Mapping[str, Any]] | None) -> str:
    if not legal:
        return "(none)"
    return "\n".join(json.dumps(dict(item), ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"))
                     for item in legal)


def _out_text(out: Any) -> str:
    text = str(getattr(out, "text", "") or "")
    if text:
        return text
    parsed = getattr(out, "parsed", None)
    if parsed is not None:
        return json.dumps(parsed, ensure_ascii=False)
    return ""


def _call_meta(out: Any, text: str, attempt: int) -> LLMCallMeta:
    usage = getattr(out, "usage", None)
    backend = getattr(out, "backend", None)
    return LLMCallMeta(
        raw=text,
        latency_ms=_int_or_none(getattr(usage, "latency_ms", None)),
        prompt_tokens=_int_or_none(getattr(usage, "prompt_tokens", None)),
        completion_tokens=_int_or_none(getattr(usage, "completion_tokens", None)),
        attempts=int(getattr(out, "attempts", attempt) or attempt),
        prompt_digest=getattr(out, "prompt_digest", None),
        model_id=getattr(out, "model_id", None),
        backend=None if backend is None else str(backend),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _memory_items(result: Any) -> list[MemoryItem]:
    """把 MOD-05 的 ``RetrieveResult`` / 条目列表归一为 ``MemoryItem``（宽松、零异常）。"""
    if result is None:
        return []
    entries = getattr(result, "entries", None)
    if entries is None:
        entries = result if isinstance(result, (list, tuple)) else []
    out: list[MemoryItem] = []
    for entry in entries or ():
        memory_id = _pick(entry, "memory_id", "entry_id", "id")
        if not memory_id:
            continue
        text = _pick(entry, "text", "lesson", "content", "render") or ""
        score = _pick(entry, "score", "utility") or 0.0
        version = _pick(entry, "harness_version", "version")
        hint = _pick(entry, "action_hint")
        out.append(MemoryItem(
            memory_id=str(memory_id), text=str(text), score=_float_or_zero(score),
            version=_int_or_none(version),
            action_hint=dict(hint) if isinstance(hint, Mapping) else None))
    return out


def _pick(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _slot_kind_matches(obs: Mapping[str, Any], action: Mapping[str, Any], slot_kind: str) -> bool:
    slot_id = action.get("slotId")
    for slot in obs.get("slots") or ():
        if isinstance(slot, Mapping) and slot.get("slotId") == slot_id:
            return str(slot.get("slotKind")) == slot_kind
    return False


def _school_matches(obs: Mapping[str, Any], action: Mapping[str, Any], school: str) -> bool:
    die_id = action.get("dieId")
    for die in obs.get("hand") or ():
        if isinstance(die, Mapping) and die.get("dieId") == die_id:
            return str(die.get("school")) == school
    return False
