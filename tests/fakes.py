"""MOD-06 loop 的假依赖（PRD NFR-06-6：``EpisodeRunner`` 依赖全部可注入）。

提供四组可脚本化替身，够跑通一局、一段 run、一次反思，且**不触网、不调模型、不写
真实 ``runs/``**：

- ``FakeEnv``：``SandboxEnv`` 的最小鸭子类型（``reset/step/reject/legal_actions/hints``），
  按脚本产出 ``terminated`` / ``truncated`` / ``outcome``；
- ``FakePolicy``：``PolicyProto``（``reset`` + ``play_decision``），每个决策返回 N 个
  ``StepRecord``，可指定 illegal / source / memory_ids；
- ``FakeMemory``：``add_reflection/record_usage/snapshot/frozen`` 全记录调用；
- ``ScriptedClient`` / ``FakeReflectionClient``：MOD-03 ``generate_json`` 的替身，
  可返回合法反思 JSON、非法 JSON 或直接抛超时。

命名与 ``tests/unit/*/…_support.py`` 一致：放在 ``tests/fakes.py``（PRD §11 登记的路径），
由 ``tests/conftest.py`` 所在目录被 pytest 加入 ``sys.path`` 后以 ``import fakes`` 使用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from dice_ratchet.agent.policy import DecisionResult, EpisodeContext, LLMCallMeta, StepRecord
from dice_ratchet.memory import Provenance, ReflectionEntry
from dice_ratchet.sandbox.api import EpisodeConfig, StepResult

__all__ = ["FakeEnv", "FakeMemory", "FakeMemoryView", "FakePolicy", "FakeJsonResult",
           "ScriptedClient", "decision", "decision_script", "make_reflection_entry",
           "obs", "reflection_json", "step_result"]


# =====================================================================
# 观测与 StepResult
# =====================================================================

def obs(*, decision_index: int = 1, phase: str = "player_turn",
        outcome: str | None = None, stage: int = 1) -> dict[str, Any]:
    """最小的 agent 侧 Observation（含 ``decisionIndex`` 与若干 legal 动作）。"""
    return {
        "schemaVersion": "obs-agent-1.0.0",
        "episodeId": "e-local", "decisionIndex": int(decision_index),
        "phase": phase, "stage": int(stage), "chapter": 1, "turn": 1,
        "difficulty": "hard", "stageCount": 6,
        "legalActionKinds": ["play_face", "end_turn"],
        "player": {"hearts": 3, "heartsMax": 3, "virtue": 5, "virtueMax": 5, "ward": 0,
                   "starShards": 0, "statuses": [], "deckSchool": "blade",
                   "purifyLeft": 0, "abilities": []},
        "hand": [{"dieId": "d-0001", "school": "blade", "face": 3}],
        "slots": [{"slotId": "slot_1", "slotKind": "attack"}],
        "enemies": [], "map": None, "shop": None, "event": None, "treasure": None,
        "forge": None, "shrine": None, "rest": None, "pendingRelic": None,
        "toggles": {}, "relics": [], "lastResult": None,
        "outcome": outcome,
    }


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def step_result(observation: Mapping[str, Any], *, reward: float = 0.0,
                terminated: bool = False, truncated: bool = False,
                illegal: bool = False, illegal_reason: str | None = None,
                outcome: str | None = None, truncation_reason: str | None = None,
                stages_cleared: int = 0, hearts_left: int = 3,
                battles_won: int = 0) -> StepResult:
    """构造一个 ``StepResult``，``info`` 按 Spec §4.1 的字段面。"""
    info = {
        "illegal": bool(illegal), "illegal_reason": illegal_reason,
        "outcome": outcome if outcome is not None else observation.get("outcome"),
        "truncation_reason": truncation_reason,
        "stages_cleared": int(stages_cleared), "hearts_left": int(hearts_left),
        "battles_won": int(battles_won),
        "state_hash": _digest({"o": dict(observation), "n": observation.get("decisionIndex")}),
        "obs_digest": _digest(observation),
        "decision_index": int(observation.get("decisionIndex") or 0),
        "env_steps": int(observation.get("decisionIndex") or 0),
        "illegal_count": 1 if illegal else 0,
    }
    return StepResult(observation=observation, reward=float(reward),
                      terminated=bool(terminated), truncated=bool(truncated), info=info)


# =====================================================================
# FakeEnv
# =====================================================================

class FakeEnv:
    """按脚本产出的假 ``SandboxEnv``（T-06-1/2/3 的"给定假 env"）。

    - ``steps_to_terminate``：执行多少**执行步**（非 reject）后 ``terminated=True``；
    - ``truncate_at``：在第 N 个执行步返回 ``truncated=True`` + ``truncation_reason``；
    - ``outcome``：终止时的 ``info["outcome"]``；
    - ``raise_on_reset`` / ``raise_on_step``：注入环境异常（PRD §10 的恢复矩阵）。
    """

    def __init__(self, *, steps_to_terminate: int = 3, steps_per_decision: int = 1,
                 outcome: str = "win", truncate_at: int | None = None,
                 truncation_reason: str = "step_limit", stage_count: int = 6,
                 stages_cleared: int = 6, hearts_left: int = 2, battles_won: int = 9,
                 raise_on_reset: BaseException | None = None,
                 raise_on_step: BaseException | None = None,
                 reward: float = 0.0) -> None:
        self.steps_to_terminate = int(steps_to_terminate)
        self.steps_per_decision = int(steps_per_decision)
        self.outcome = outcome
        self.truncate_at = truncate_at
        self.truncation_reason = truncation_reason
        self.stage_count = int(stage_count)
        self.stages_cleared = int(stages_cleared)
        self.hearts_left = int(hearts_left)
        self.battles_won = int(battles_won)
        self.raise_on_reset = raise_on_reset
        self.raise_on_step = raise_on_step
        self.reward = float(reward)
        self.resets: list[int] = []
        self.steps: list[Mapping[str, Any]] = []
        self.rejects: list[dict[str, Any]] = []
        self.closed = False
        self._obs = obs()
        self._executed = 0
        self._decision = 0
        self._truncated = False

    # ---- 生命周期 ---------------------------------------------------------
    def reset(self, seed: int, *, episode_config: Any = None,
              episode_id: str | None = None) -> Mapping[str, Any]:
        if self.raise_on_reset is not None:
            raise self.raise_on_reset
        self.resets.append(int(seed))
        self._executed = 0
        self._decision = 0
        self._truncated = False
        cfg = EpisodeConfig.from_any(episode_config)
        self._obs = obs(decision_index=0, stage=1)
        self._obs["stageCount"] = int(cfg.stage_count)
        return dict(self._obs)

    def step(self, action: Mapping[str, Any]) -> StepResult:
        if self.raise_on_step is not None:
            raise self.raise_on_step
        self.steps.append(dict(action))
        self._executed += 1
        self._decision += 1
        if self.truncate_at is not None and self._executed >= int(self.truncate_at):
            self._truncated = True
            self._obs = obs(decision_index=self._decision, phase="player_turn")
            return step_result(self._obs, reward=self.reward, truncated=True,
                               outcome=None, truncation_reason=self.truncation_reason,
                               stages_cleared=min(self.stages_cleared, 4),
                               hearts_left=0, battles_won=self.battles_won)
        terminated = self._executed >= self.steps_to_terminate
        self._obs = obs(decision_index=self._decision,
                        phase="settle" if terminated else "player_turn",
                        outcome=self.outcome if terminated else None)
        return step_result(self._obs, reward=self.reward, terminated=terminated,
                           outcome=self.outcome if terminated else None,
                           truncation_reason=None,
                           stages_cleared=self.stages_cleared if terminated else 1,
                           hearts_left=self.hearts_left if terminated else 3,
                           battles_won=self.battles_won if terminated else 0)

    def reject(self, *, stage: str, reason: str, raw: str | None = None,
               final: bool = False) -> StepResult:
        self.rejects.append({"stage": stage, "reason": reason, "raw": raw, "final": final})
        return step_result(self._obs, reward=-0.05, illegal=True,
                           illegal_reason=reason)

    def observe(self) -> Mapping[str, Any]:
        return dict(self._obs)

    def legal_actions(self) -> list[dict[str, Any]]:
        return [{"kind": "play_face", "dieId": "d-0001", "slotId": "slot_1"},
                {"kind": "end_turn"}]

    def hints(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        self.closed = True

    @property
    def truncated(self) -> bool:
        return self._truncated


# =====================================================================
# FakePolicy
# =====================================================================

def decision(env: FakeEnv, plan: Sequence[Mapping[str, Any]], *,
             source: str = "llm", attempt: int = 1,
             memory_ids: Sequence[str] = (), adopted: Sequence[str] = (),
             degraded: bool = False, llm: LLMCallMeta | None = None,
             reject_rows: Sequence[Mapping[str, Any]] = (),
             prompt_hash: str = "sha256:" + "b" * 64) -> DecisionResult:
    """按 ``plan`` 在 ``env`` 上执行一串动作，产出 ``DecisionResult``。"""
    script: list[dict[str, Any]] = [{"reject": dict(r)} for r in reject_rows]
    script += [{"action": dict(a)} for a in plan]
    return decision_script(env, script, source=source, attempt=attempt,
                           memory_ids=memory_ids, adopted=adopted, degraded=degraded,
                           llm=llm, prompt_hash=prompt_hash)


def decision_script(env: FakeEnv, script: Sequence[Mapping[str, Any]], *,
                    source: str = "llm", attempt: int = 1,
                    memory_ids: Sequence[str] = (), adopted: Sequence[str] = (),
                    degraded: bool = False, llm: LLMCallMeta | None = None,
                    prompt_hash: str = "sha256:" + "b" * 64) -> DecisionResult:
    """按 ``script`` 执行一个决策点：每项是 ``{"action": {...}}`` 或 ``{"reject": {...}}``。

    ``reject`` 项会调用 ``env.reject()``（不改状态、记一次非法，Spec B-15 的 L1/L2 层），
    因此可以精确构造 T-06-3 的「计划内第 2 个是 reject 行」。
    """
    steps: list[StepRecord] = []
    last: StepResult | None = None
    plan_index = 0
    for item in script:
        if "reject" in item:
            rej = dict(item["reject"])
            result = env.reject(stage=str(rej.get("stage", "semantic")),
                               reason=str(rej.get("reason", "illegal")),
                               raw=rej.get("raw"), final=bool(rej.get("final", False)))
            steps.append(StepRecord(action=rej.get("action"), result=result,
                                    plan_index=plan_index, attempt=attempt,
                                    source=source, illegal=True,
                                    illegal_reason=str(rej.get("reason", "illegal"))))
            last = result
            continue
        action = dict(item["action"])
        last = env.step(action)
        steps.append(StepRecord(action=action, result=last, plan_index=plan_index,
                                attempt=attempt, source=source,
                                illegal=bool(last.info.get("illegal")),
                                illegal_reason=last.info.get("illegal_reason")))
        plan_index += 1
        if last.terminated or last.truncated:
            break
    if last is None: # pragma: no cover - 计划与 reject 都为空时兜底
        last = step_result(env.observe())
    return DecisionResult(steps=steps, source=source, attempts=attempt, llm=llm,
                          memory_ids=list(memory_ids), adopted_memory_ids=list(adopted),
                          prompt_hash=prompt_hash, tool_hash=None, reasoning="",
                          last=last, error_codes=[], degraded=degraded,
                          mode="play", template_hash="")


class FakePolicy:
    """``PolicyProto`` 替身：每个决策返回一组 ``StepRecord``。

    - ``plans``：每次决策的**动作**序列（reject 行由 ``rejects_first`` 补在最前）；
    - ``scripts``：更强的形式，逐项 ``{"action": …}`` / ``{"reject": …}``（T-06-3 用）；
    - 两者都耗尽后回落到 ``default_plan``。
    """

    def __init__(self, *, default_plan: Sequence[Mapping[str, Any]] = (
                    {"kind": "play_face", "dieId": "d-0001", "slotId": "slot_1"},),
                 plans: Sequence[Sequence[Mapping[str, Any]]] | None = None,
                 scripts: Sequence[Sequence[Mapping[str, Any]]] | None = None,
                 source: str = "llm", memory_ids: Sequence[str] = (),
                 adopted: Sequence[str] = (), degraded: bool = False,
                 llm: LLMCallMeta | None = None, with_llm_meta: bool = True,
                 rejects_first: Sequence[Mapping[str, Any]] = ()) -> None:
        self.default_plan = [dict(a) for a in default_plan]
        self.plans = [[dict(a) for a in plan] for plan in (plans or [])]
        self.scripts = [[dict(item) for item in script] for script in (scripts or [])]
        self.source = source
        self.memory_ids = list(memory_ids)
        self.adopted = list(adopted)
        self.degraded = degraded
        self.llm = llm if llm is not None else (
            LLMCallMeta(raw='{"reasoning":"","plan":[]}', latency_ms=7, prompt_tokens=11,
                        completion_tokens=3, attempts=1, model_id="fake-8b")
            if with_llm_meta else None)
        self.rejects_first = [dict(r) for r in rejects_first]
        self.resets: list[EpisodeContext] = []
        self.decisions: list[dict[str, Any]] = []

    def reset(self, ctx: EpisodeContext) -> None:
        self.resets.append(ctx)

    def play_decision(self, env: FakeEnv, obs: Mapping[str, Any], *,
                      ctx: EpisodeContext) -> DecisionResult:
        index = len(self.decisions)
        self.decisions.append({"episode": ctx.episode, "seed": ctx.seed, "tick": ctx.tick,
                               "mode": ctx.mode})
        common = {"source": self.source, "memory_ids": self.memory_ids,
                  "adopted": self.adopted, "degraded": self.degraded, "llm": self.llm}
        if index < len(self.scripts):
            return decision_script(env, self.scripts[index], **common)
        plan = self.plans[index] if index < len(self.plans) else self.default_plan
        return decision(env, plan, reject_rows=(self.rejects_first if index == 0 else ()),
                        **common)


# =====================================================================
# FakeMemory
# =====================================================================

class FakeMemory:
    """``MemoryProto`` 替身：记录 ``add_reflection`` / ``record_usage`` / ``frozen``。"""

    def __init__(self, *, fail_add: BaseException | None = None,
                 fail_usage: BaseException | None = None) -> None:
        self.reflections: list[tuple[ReflectionEntry, Provenance]] = []
        self.usages: list[dict[str, Any]] = []
        self.snapshots: list[int] = []
        self.frozen_calls: list[int] = []
        self.fail_add = fail_add
        self.fail_usage = fail_usage

    def add_reflection(self, entry: ReflectionEntry, *, provenance: Provenance) -> str:
        if self.fail_add is not None:
            raise self.fail_add
        self.reflections.append((entry, provenance))
        return entry.reflection_id

    def record_usage(self, episode_key: str, memory_ids: Sequence[str], outcome: str, *,
                     adopted_ids: Sequence[str], tick: int) -> None:
        if self.fail_usage is not None:
            raise self.fail_usage
        self.usages.append({"episode_key": episode_key, "memory_ids": list(memory_ids),
                            "outcome": outcome, "adopted_ids": list(adopted_ids),
                            "tick": int(tick)})

    def snapshot(self, version: int) -> Any:
        self.snapshots.append(int(version))
        return f"memory_v{int(version)}.jsonl"

    def frozen(self, version: int) -> "FakeMemoryView":
        self.frozen_calls.append(int(version))
        return FakeMemoryView(self, int(version))

    @property
    def snapshot_every_generation(self) -> bool:
        return True


@dataclass
class FakeMemoryView:
    """``MemoryView`` 替身（评估局只读；禁止写库）。"""

    store: FakeMemory
    version: int
    retrieved: list[Any] = field(default_factory=list)

    def retrieve(self, _obs: Mapping[str, Any], *, top_k: int, token_budget: int,
                 tick: int) -> list[Any]:
        return []


# =====================================================================
# 反思客户端
# =====================================================================

def make_reflection_entry(*, text: str = "净化值低时优先防守。", failure_cause: str = "defense",
                          tags: Sequence[str] = ("defense",), **over: Any) -> ReflectionEntry:
    """构造合法的 MOD-05 ``ReflectionEntry``。"""
    base: dict[str, Any] = {
        "reflection_id": "refl-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        "schema_version": "1.1", "created_at": "2026-10-06T00:00:00Z", "run_id": "r-test",
        "episode_id": "e-001000-g000-train", "seed": 1000, "split": "train",
        "generation": 0, "harness_version": 0, "tick": 1, "text": text,
        "outcome": "lose", "truncation_reason": None, "stages_cleared": 3,
        "hearts_left": 0, "battles_won": 4, "decisions": 60, "illegal_attempts": 0,
        "fallback_decisions": 0, "failure_cause": failure_cause, "tags": tuple(tags),
        "extracted_memory_ids": (), "scan": {"status": "pass", "hits": []},
    }
    base.update(over)
    return ReflectionEntry(**base)


@dataclass
class FakeUsage:
    latency_ms: int | None = 12
    prompt_tokens: int | None = 100
    completion_tokens: int | None = 20


@dataclass
class FakeJsonResult:
    """MOD-03 ``JsonResult`` 的最小鸭子类型。"""

    text: str
    parsed: Mapping[str, Any] | None = None
    usage: Any = field(default_factory=FakeUsage)
    attempts: int = 1
    model_id: str = "fake-8b"


class ScriptedClient:
    """按脚本逐次返回 / 抛异常，并记录每次调用的 messages 与采样参数。"""

    def __init__(self, script: Sequence[str | BaseException | Mapping[str, Any]] = (), *,
                 default: str | Mapping[str, Any] | BaseException | None = None,
                 model_id: str = "fake-8b") -> None:
        self.script = list(script)
        self.default = default
        self.model_id = model_id
        self.calls: list[dict[str, Any]] = []

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], *,
                      mode: str, sampling: Mapping[str, Any], seed: int) -> FakeJsonResult:
        index = len(self.calls)
        self.calls.append({"messages": list(messages), "schema": dict(schema),
                           "mode": mode, "sampling": dict(sampling), "seed": int(seed)})
        item: Any = self.script[index] if index < len(self.script) else self.default
        if item is None:
            item = {"text": "默认反思。", "failure_cause": "unknown", "tags": []}
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, Mapping):
            return FakeJsonResult(text=json.dumps(dict(item), ensure_ascii=False),
                                  parsed=dict(item), model_id=self.model_id)
        return FakeJsonResult(text=str(item), parsed=None, model_id=self.model_id)


def reflection_json(text: str = "净化值低时优先防守。", *,
                    failure_cause: str = "defense",
                    tags: Sequence[str] = ("defense",)) -> str:
    return json.dumps({"text": text, "failure_cause": failure_cause, "tags": list(tags)},
                      ensure_ascii=False)
