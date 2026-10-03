"""MOD-02 ``SandboxEnv``：agent 侧触碰 L0 环境的**唯一合法入口**（INV-1，PRD FR-02-7/8）。

- 私有持有 MOD-01 引擎与 ``GameState``（``self._engine`` / ``self._state``），
  **任何公开成员都不返回 FullState/engine/rng_state**；``__getattr__`` 对越权属性名
  直接 ``AttributeError``。``step()`` 内先算 ``state_hash``/``obs_digest`` 再丢弃
  ``full_state`` 引用（FR-02-7）。
- 观测只可能来自 ``projection.project_state()``（INV-2）；投影前若开了蜜罐，会对
  ``GameState`` 的**轻量副本**注入哨兵（FR-02-20），真实 state 与 ``state_hash`` 不受影响。
- **唯一判负方**（B-15，FR-02-8b）：连续 ``policy_failure_streak`` 次
  ``reject(final=True)``（中间没有成功决策）→ ``truncated=True`` +
  ``truncation_reason="policy_failure"``，主指标计为 lose；另外 ``max_env_steps`` /
  ``max_decisions`` 超限 → ``step_limit`` / ``decision_limit``。MOD-04/06 不另行判负。
- ``SandboxEnv`` 模块在安装 import hook **之前**预先 import 环境层（FR-02-10）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Final, Mapping

from gamer_rsi.env import GameEngine, KernelError, TOGGLE_KEYS, map_error_code, native_result, state_hash
from gamer_rsi.infra.errors import E, InfraError

from . import honeypot as HP
from .guard import install_agent_import_hook
from .projection import (OBSERVATION_SCHEMA_VERSION, MODE_STRICT, ProjectionCtx,
                         obs_digest, project_state, render_compact)

__all__ = ["EpisodeConfig", "StepResult", "SandboxEnv", "ALL_ON_TOGGLES", "REWARD_PLACEHOLDER"]

#: Spec §4.1：``toggles: Mapping[str, bool] = ALL_ON``（全开）。
ALL_ON_TOGGLES: Final[Mapping[str, bool]] = {k: True for k in TOGGLE_KEYS}

#: 合法动作的 shaped reward 占位（INV-7：真正 shaping 归 MOD-08 ``reward.py``）。
REWARD_PLACEHOLDER: Final[float] = 0.0


def _toggle_error(detail: str, hint: str) -> InfraError:
    return InfraError(E.E_ENV_CONFIG_INVALID, detail, hint=hint)


def _normalize_toggles(value: Any) -> dict[str, bool]:
    """把 ``toggles`` 归一为恰好 ``TOGGLE_KEYS`` 的 ``{name: bool}``（M0 验收 F1）。

    两种写法，各有明确语义：

    ① ``Mapping[str, bool]``——Spec §4.1 的 GAME ``createGame`` 原生形态：显式给出的键
       取给定值，**未给出的键取内核默认 ``True``**（与 ``env/engine.py::apply_difficulty``
       逐位一致，不改内核行为）。
    ② ``Sequence[str]``——MOD-09 ``configs/default.yaml`` 的 ``env.episode.toggles``：
       语义是**开启清单**，列出为开、未列出为关；空清单 = 全关（g1 golden 的合法配置）。

    未在 ``TOGGLE_KEYS`` 登记的名字一律 ``E_ENV_CONFIG_INVALID``（fail-closed）。原因：
    内核 ``createGame`` 对未知键**静默忽略**、对缺失键默认 ``True``，所以把配置里的名字
    直接透传会让「关掉某个模块」的配置**静默失效**——这正是 M0 验收发现的缺陷
    （``configs/default.yaml`` 曾写 6 个名字，其中 4 个不是内核开关，且被整体忽略）。
    """
    if value is None:
        return dict(ALL_ON_TOGGLES)
    if isinstance(value, Mapping):
        unknown = sorted(str(k) for k in value if k not in ALL_ON_TOGGLES)
        if unknown:
            raise _toggle_error(
                f"toggles 含未登记的开关名 {unknown}",
                f"合法开关名（GAME kernel TOGGLE_KEYS）：{list(TOGGLE_KEYS)}")
        return {k: bool(value[k]) if k in value else True for k in TOGGLE_KEYS}
    if isinstance(value, (list, tuple, set, frozenset)):
        unknown = sorted({str(n) for n in value if n not in ALL_ON_TOGGLES})
        if unknown:
            raise _toggle_error(
                f"toggles 开启清单含未登记的开关名 {unknown}",
                f"合法开关名（GAME kernel TOGGLE_KEYS）：{list(TOGGLE_KEYS)}")
        on = {str(n) for n in value}
        return {k: k in on for k in TOGGLE_KEYS}
    raise _toggle_error(
        f"toggles 的值 {value!r} 类型为 {type(value).__name__}，"
        f"期望 Mapping[str, bool]（GAME 原生）或 Sequence[str]（开启清单）",
        f"合法开关名（GAME kernel TOGGLE_KEYS）：{list(TOGGLE_KEYS)}")


@dataclass(frozen=True)
class EpisodeConfig:
    """一局配置，展开为 GAME ``createGame`` 输入（Spec §4.1 / §8.6 默认局）。"""

    difficulty: str = "hard"
    stage_count: int = 6
    toggles: Mapping[str, bool] = field(
        default_factory=lambda: dict(ALL_ON_TOGGLES))
    character: str = "warrior"
    max_env_steps: int = 1000
    max_decisions: int = 300

    def to_game_input(self, seed: int) -> dict[str, Any]:
        return {"difficulty": self.difficulty, "seed": int(seed),
                "character": self.character, "stageCount": int(self.stage_count),
                "toggles": {k: bool(v) for k, v in self.toggles.items()}}

    @classmethod
    def from_any(cls, value: Any) -> "EpisodeConfig":
        """接受 ``EpisodeConfig`` / Mapping / MOD-09 的 ``env.episode`` 配置对象。

        MOD-09 的 ``EpisodeConfig`` 字段名是 ``stage_count``、``toggles`` 为**开启清单**
        （``tuple[str, ...]``），这里做一次显式适配（Spec §4.1 与 §5 的两处同名类不同源），
        并按 ``_normalize_toggles`` 校验开关名——配置里的 ``toggles`` 必须真的生效。
        """
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            data = dict(value)
            return cls(difficulty=str(data.get("difficulty", "hard")),
                       stage_count=int(data.get("stage_count", data.get("stageCount", 6))),
                       toggles=_normalize_toggles(data.get("toggles")),
                       character=str(data.get("character", "warrior")),
                       max_env_steps=int(data.get("max_env_steps", 1000)),
                       max_decisions=int(data.get("max_decisions", 300)))
        return cls(
            difficulty=str(getattr(value, "difficulty", "hard")),
            stage_count=int(getattr(value, "stage_count", 6)),
            toggles=_normalize_toggles(getattr(value, "toggles", None)),
            character=str(getattr(value, "character", "warrior")),
            max_env_steps=int(getattr(value, "max_env_steps", 1000)),
            max_decisions=int(getattr(value, "max_decisions", 300)))


@dataclass(frozen=True)
class StepResult:
    """``step()`` / ``reject()`` 的返回（Spec §4.1）。"""

    observation: Mapping[str, Any]
    reward: float
    terminated: bool
    truncated: bool
    info: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"observation": dict(self.observation), "reward": self.reward,
                "terminated": self.terminated, "truncated": self.truncated,
                "info": dict(self.info)}


class SandboxEnv:
    """agent 侧唯一环境门面（Spec §4.1；严禁任何返回 FullState 的公开成员）。"""

    _DENY_ATTRS: Final[frozenset[str]] = frozenset({
        "engine", "state", "full_state", "fullState", "rng", "rng_state",
        "rngState", "game_state", "raw_state",
    })

    def __init__(self, *, engine: Any, episode_config: EpisodeConfig | None = None,
                 projection_mode: str = MODE_STRICT, honeypot: bool = True,
                 episode_id: str | None = None, policy_failure_streak: int = 3,
                 illegal_penalty: float = -0.05,
                 max_env_steps: int | None = None,
                 max_decisions: int | None = None) -> None:
        # 环境层在安装 hook 之前就已被本模块 import（FR-02-10）
        self._engine = engine
        self._episode_config = EpisodeConfig.from_any(episode_config)
        if max_env_steps is not None:
            self._episode_config = EpisodeConfig(
                **{**self._episode_config.__dict__, "max_env_steps": int(max_env_steps)})
        if max_decisions is not None:
            self._episode_config = EpisodeConfig(
                **{**self._episode_config.__dict__, "max_decisions": int(max_decisions)})
        self._projection_mode = projection_mode
        self._honeypot = bool(honeypot)
        self._episode_id = episode_id
        self._policy_failure_streak = max(1, int(policy_failure_streak))
        self._illegal_penalty = float(illegal_penalty)
        self._state: Mapping[str, Any] | None = None
        self._obs: Mapping[str, Any] | None = None
        self._obs_digest = ""
        self._state_hash = ""
        self._env_steps = 0
        self._decision_index = -1
        self._decision_key: tuple[Any, ...] | None = None
        self._illegal_count = 0
        self._final_streak = 0
        self._last_result: Mapping[str, Any] | None = None
        self._last_stat: dict[str, Any] = {}
        self._truncated = False
        self._truncation_reason: str | None = None
        self._closed = False
        install_agent_import_hook()

    # ---- 上下文管理 -------------------------------------------------------
    def __enter__(self) -> "SandboxEnv":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        if name in SandboxEnv._DENY_ATTRS:
            raise AttributeError(
                f"SandboxEnv 不暴露 {name!r}：agent 只能经 observe()/step() 触达环境（INV-1）")
        raise AttributeError(f"SandboxEnv 没有属性 {name!r}")

    def __repr__(self) -> str: # 不打印任何 state 内容
        return (f"SandboxEnv(episode_id={self._episode_id!r}, "
                f"phase={self._phase()}, steps={self._env_steps}, "
                f"decision={self._decision_index}, closed={self._closed})")

    # ---- 生命周期 ---------------------------------------------------------
    def reset(self, seed: int, *, episode_config: EpisodeConfig | None = None,
              episode_id: str | None = None) -> Mapping[str, Any]:
        """新建一局，返回首个 ``Observation``（FR-02-7）。"""
        self._ensure_open()
        if episode_config is not None:
            self._episode_config = EpisodeConfig.from_any(episode_config)
        if episode_id is not None:
            self._episode_id = episode_id
        try:
            state = self._engine.create(self._episode_config.to_game_input(seed))
        except KernelError as exc:
            code, _ = map_error_code(exc.code)
            raise InfraError(code or E.E_ENV_CONFIG_INVALID,
                             f"createGame 失败：{exc}",
                             hint="检查 EpisodeConfig（difficulty/stageCount/character）"
                                  "与 src/game/content 配表") from exc
        self._state = state
        self._env_steps = 0
        self._decision_index = -1
        self._decision_key = None
        self._illegal_count = 0
        self._final_streak = 0
        self._last_result = None
        self._truncated = False
        self._truncation_reason = None
        return self._refresh()

    def step(self, action: Mapping[str, Any]) -> StepResult:
        """执行一个 GAME 动作（Spec §4.1）。

        - 非法动作**不抛异常**：``reward=illegal_penalty``、``info["illegal"]=True``、
          状态与观测不变（只更新 ``lastResult``，FR-02-8）。
        - 内核异常（MOD-01 ``error``）→ 抛携带 ``E_ENV_*`` 的 ``InfraError``（fail-closed）。
        """
        self._ensure_state()
        previous = self._state
        result = self._engine.reduce(previous, action)
        if result.get("error"):
            error = result["error"]
            raise InfraError(error.get("code", E.E_ENV_INTERNAL),
                             f"GAME 内核异常：{error.get('message')}",
                             hint="该步状态未变；属 MOD-01 契约问题")
        illegal = bool(result.get("illegal"))
        if illegal:
            self._illegal_count += 1
            self._last_result = {"illegal": True, "reason": result.get("reason") or "",
                                 "code": result.get("code"), "stage": "l3"}
            reward = self._illegal_penalty
        else:
            self._state = result["state"]
            self._env_steps += 1
            self._final_streak = 0
            self._last_result = None
            reward = REWARD_PLACEHOLDER
        obs = self._refresh()
        if not illegal and self._state is previous:
            raise InfraError(E.E_ENV_INTERNAL,
                             "合法动作后未产生新 state（MOD-01 写时拷贝契约破坏）",
                             hint="reduce() 必须返回新对象；非法动作才原对象返回")
        truncated, reason = self._evaluate_truncation()
        terminated = self._outcome() is not None
        return StepResult(observation=obs, reward=reward, terminated=terminated,
                          truncated=truncated,
                          info=self._info(illegal=illegal,
                                          illegal_reason=(self._last_result or {}).get("reason"),
                                          truncation_reason=reason if truncated else None))

    def reject(self, *, stage: str, reason: str, raw: str | None = None,
               final: bool = False) -> StepResult:
        """L1/L2 校验失败时由 MOD-04 调用（FR-02-8a/8b；不改状态）。

        - ``illegal_count += 1``、shaped ``illegal_penalty``、``lastResult`` 更新；
        - ``final=True`` 且连续达到 ``policy_failure_streak`` 次（中间无成功决策）
          → ``truncated=True, truncation_reason="policy_failure"``，主指标计为 lose。

        轨迹落地归 MOD-06（Spec §4.5）：本方法把 ``illegal=True`` 与 ``reason`` 放进
        ``StepResult.info``，由 MOD-06 写那一行 ``illegal=true`` 的轨迹。
        """
        self._ensure_state()
        if not isinstance(stage, str) or not stage:
            raise InfraError(E.E_ENV_SCHEMA, "reject(stage=...) 必须是非空字符串",
                             hint="Spec §4.1：stage ∈ {'syntax','semantic'}（L1/L2）")
        if not isinstance(reason, str):
            raise InfraError(E.E_ENV_SCHEMA, "reject(reason=...) 必须是字符串")
        self._illegal_count += 1
        self._last_result = {"illegal": True, "reason": reason, "stage": stage,
                             "raw": raw}
        if final:
            self._final_streak += 1
            if self._final_streak >= self._policy_failure_streak:
                self._truncated = True
                self._truncation_reason = "policy_failure"
        obs = self._refresh(recompute_hash=False) # reject 不改状态
        truncated, reason_out = self._evaluate_truncation()
        return StepResult(observation=obs, reward=self._illegal_penalty, terminated=False,
                          truncated=truncated,
                          info=self._info(illegal=True, illegal_reason=reason,
                                          truncation_reason=reason_out if truncated else None,
                                          extra={"reject_stage": stage, "final": bool(final)}))

    def observe(self) -> Mapping[str, Any]:
        """重新投影当前状态并返回 ``Observation``（幂等，纯函数投影）。"""
        self._ensure_state()
        return self._refresh(recompute_hash=False) # 不改状态 → 复用 state_hash

    def legal_actions(self) -> list[dict[str, Any]]:
        """MOD-01 ``legal_actions`` 去掉 ``abandon_run``，顺序保持不变（FR-02-8c）。"""
        self._ensure_state()
        return [a for a in self._engine.legal_actions(self._state)
                if a.get("kind") != "abandon_run"]

    def hints(self) -> dict[str, Any]:
        """转发 MOD-01 ``play_hints``（伤害预览/非法原因，只含公开信息，FR-02-8c）。"""
        self._ensure_state()
        return self._engine.play_hints(self._state) or {}

    def close(self) -> None:
        """释放对 ``GameState`` 的引用（幂等）。"""
        self._state = None
        self._obs = None
        self._closed = True

    # ---- 审计旁路（不进入 Observation）------------------------------------
    def last_obs_digest(self) -> str:
        """最近一次观测摘要（审计用旁路；不进 Observation、不进 prompt）。"""
        return self._obs_digest

    def last_state_hash(self) -> str:
        """最近一次 ``state_hash``（审计对拍用旁路，单向摘要）。"""
        return self._state_hash

    def projection_stat(self) -> dict[str, Any]:
        """最近一次投影统计（**只有计数、没有字段名**，FR-02-2）。"""
        return dict(self._last_stat)

    def render_compact(self) -> str:
        """当前观测的紧凑渲染（FR-02-3b，供 MOD-04 的 ``{{observation}}``）。"""
        self._ensure_state()
        return render_compact(self._obs or self._refresh())

    # ---- 内部 -------------------------------------------------------------
    def _phase(self) -> str | None:
        return None if self._state is None else str(self._state.get("phase"))

    def _outcome(self) -> str | None:
        if self._state is None:
            return None
        outcome = self._state.get("outcome")
        return str(outcome) if outcome else None

    def _ensure_open(self) -> None:
        if self._closed:
            raise InfraError(E.E_ENV_SCHEMA, "SandboxEnv 已 close()，不能继续使用")

    def _ensure_state(self) -> None:
        self._ensure_open()
        if self._state is None:
            raise InfraError(E.E_ENV_SCHEMA, "SandboxEnv 尚未 reset(seed)",
                             hint="先调用 reset(seed, episode_config=...)")

    def _ctx(self) -> ProjectionCtx:
        state = self._state or {}
        episode_id = self._episode_id or str(state.get("runId") or "e-local")
        return ProjectionCtx(episode_id=episode_id,
                             decision_index=max(0, self._decision_index),
                             stage_count=self._episode_config.stage_count,
                             last_result=self._last_result)

    def _game_obs(self) -> Mapping[str, Any]:
        """GAME ``observe(state, "full")``；开了蜜罐就先对副本注入哨兵（FR-02-20）。"""
        state = self._state
        if self._honeypot:
            probe = HP.injected_copy(state) # type: ignore[arg-type]
        else:
            probe = state
        return self._engine.observe(probe, "full")

    def _refresh(self, *, recompute_hash: bool = True) -> Mapping[str, Any]:
        # 决策点序号：player_turn 每回合一次；其余 phase 每步一次（Spec §4.3 / B-12）
        started = time.perf_counter()
        state = self._state or {}
        phase = str(state.get("phase"))
        turn = state.get("turn")
        key: tuple[Any, ...] = ((phase, turn) if phase == "player_turn"
                                else (phase, turn, self._env_steps))
        if key != self._decision_key:
            self._decision_key = key
            self._decision_index += 1
        obs, stat = project_state(self._game_obs(), ctx=self._ctx(),
                                  mode=self._projection_mode)
        if obs.get("schemaVersion") != OBSERVATION_SCHEMA_VERSION: # pragma: no cover - 防御
            raise InfraError(E.E_ENV_SCHEMA, "schemaVersion 注入失败")
        self._obs = obs
        self._obs_digest = obs_digest(obs)
        if recompute_hash and state:
            # ``state_hash``（整个 GameState 的 canonical 摘要）是 step 里最贵的一步：
            # observe()/reject() 不改状态，直接复用上一次的值（NFR-02-2）。
            self._state_hash = state_hash(state)
        self._last_stat = {"kept": len(stat.kept), "dropped_hidden": stat.dropped_hidden,
                           "missing_optional": len(stat.missing_optional)}
        # PRD §13：每次投影一个结构化事件（fields/dropped_hidden/duration_us）
        _emit("env.projection_pruned", fields=len(stat.kept),
              dropped_hidden=stat.dropped_hidden,
              missing_optional=list(stat.missing_optional),
              duration_us=round((time.perf_counter() - started) * 1_000_000, 1))
        return obs

    def _evaluate_truncation(self) -> tuple[bool, str | None]:
        if self._truncated:
            return True, self._truncation_reason
        if self._state is not None and self._state.get("truncationReason"):
            self._truncated = True
            self._truncation_reason = str(self._state["truncationReason"])
            return True, self._truncation_reason
        if self._env_steps >= self._episode_config.max_env_steps:
            self._truncated = True
            self._truncation_reason = "step_limit"
            return True, "step_limit"
        if (self._decision_index + 1) > self._episode_config.max_decisions:
            self._truncated = True
            self._truncation_reason = "decision_limit"
            return True, "decision_limit"
        return False, None

    def _info(self, *, illegal: bool, illegal_reason: str | None,
              truncation_reason: str | None,
              extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        state = self._state or {}
        native = native_result(state) if state else {}
        info: dict[str, Any] = {
            "illegal": bool(illegal),
            "illegal_reason": illegal_reason,
            "outcome": native.get("outcome"),
            "truncation_reason": truncation_reason,
            "stages_cleared": native.get("stagesCleared", 0),
            "hearts_left": native.get("heartsLeft", 0),
            "battles_won": native.get("battlesWon", 0),
            "state_hash": self._state_hash,
            "obs_digest": self._obs_digest,
            "env_steps": self._env_steps,
            "decision_index": max(0, self._decision_index),
            "illegal_count": self._illegal_count,
        }
        if extra:
            info.update(extra)
        return info


def _emit(event: str, **fields: Any) -> None:
    try:
        from gamer_rsi.infra import events

        events.emit(event, **fields)
    except Exception: # noqa: BLE001 - 埋点失败不得改变投影结果
        pass


def make_default_env(*, episode_config: EpisodeConfig | None = None,
                     content_dir: Any = None, **options: Any) -> SandboxEnv:
    """便捷构造：加载配表 + ``GameEngine`` + ``SandboxEnv``（G6 命令与测试用）。"""
    from gamer_rsi.env import load_tables_checked

    engine = GameEngine(load_tables_checked(content_dir) if content_dir
                        else load_tables_checked())
    return SandboxEnv(engine=engine, episode_config=episode_config, **options)
