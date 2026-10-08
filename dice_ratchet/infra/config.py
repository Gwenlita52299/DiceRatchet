"""配置体系（FR-09-1..4 / Spec §5 配置契约）。

优先级（Spec v1.4 B-26）::

    CLI 显式参数 > DICERATCHET_* 环境变量 > harness/config.json（可演化层）
                   > configs/default.yaml > 代码内 dataclass 默认值

键路径到 ``AppConfig`` 八个段的映射（保持各 PRD 文档的键名不变）::

    env.*                -> EnvConfig            （含 env.episode.* / env.sandbox.*）
    episode.*            -> env.episode.*        （Spec §8.6 唯一只读上限）
    sandbox.*            -> env.sandbox.*        （MOD-02）
    policy.*             -> agent.policy.*       （MOD-04）
    inference.*          -> InferenceConfig      （MOD-03）
    models.*             -> ModelRolesConfig     （MOD-03：角色 → model_id 绑定，Issue #1）
    experiment.*         -> ExperimentConfig     （MOD-03/06：实验臂与角色覆盖，Issue #1）
    memory.*             -> MemoryConfig         （MOD-05）
    loop.*               -> LoopConfig           （MOD-06）
    evolve.*             -> EvolveConfig         （MOD-07）
    eval.*               -> EvalConfig           （MOD-08）
    infra.*              -> InfraConfig          （MOD-09）

``harness/config.json`` 只接受白名单前缀 ``agent.policy.`` / ``memory.retrieve.`` /
``agent.tools.``；其他键（含 ``episode.*`` / ``loop.*`` / ``evolve.*`` 等静态键）
→ ``E_IO_CONFIG_INVALID``。这一层计入 ``artifact_hash``，不计入 ``config_hash``（B-19）。
"""

from __future__ import annotations

import hashlib
import json
import os
import types
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import (Any, Final, Mapping, Sequence, Union, get_args, get_origin)

import yaml

from .errors import E, InfraError

__all__ = [
    "AppConfig", "EnvConfig", "EpisodeConfig", "SandboxConfig", "AgentConfig",
    "PolicyConfig", "InferenceConfig", "ModelRolesConfig", "ExperimentConfig",
    "MemoryConfig", "LoopConfig", "EvolveConfig",
    "EvalConfig", "InfraConfig", "SeedsProtocol",
    "MODEL_ROLES", "EXPERIMENT_ARMS", "ARM_HARNESS_MODES",
    "load_config", "load_seeds", "load_models", "config_hash", "canonical_json",
    "derive_seed", "artifact_hash", "flatten_config", "apply_overrides",
    "harness_overridden_keys", "env_applied_keys", "full_config",
]

# harness/config.json 白名单前缀（FR-09-1 / Spec §5 B-26）
HARNESS_CONFIG_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    "agent.policy.", "memory.retrieve.", "agent.tools.",
)
ENV_PREFIX: Final[str] = "DICERATCHET_"

# 不参与 config_hash 的易变键（FR-09-3）
_HASH_VOLATILE_KEYS: Final[frozenset[str]] = frozenset({
    "infra.runs_root", "infra.log_level", "infra.threads",
    "infra.config_dir", "infra.schemas_dir",
})

# 私有溯源属性：由 ``load_config()`` 挂到 AppConfig 实例上（不参与 dataclass 字段/asdict/相等性）
#   _HARNESS_STATIC_ATTR: {键路径: (静态基线是否存在, 静态值)}，供 config_hash 回退 harness 覆盖
#   _ENV_APPLIED_ATTR:   真正生效的 DICERATCHET_* 变量名，供 config.json 的环境变量白名单
_HARNESS_STATIC_ATTR: Final[str] = "_harness_static_values"
_ENV_APPLIED_ATTR: Final[str] = "_env_applied_keys"


# =====================================================================
# 各段配置（默认值取自各模块 PRD 的「配置项」表）
# =====================================================================

@dataclass(frozen=True)
class EpisodeConfig:
    """默认局（Spec §8.6）+ 只读上限。config_hash 覆盖此段（Spec §5）。

    ``toggles`` 是 GAME 内核 ``TOGGLE_KEYS`` 的**开启清单**：列出为开、未列出为关
    （默认列全 = 全开）。``infra`` 不得 import ``dice_ratchet.env``（NFR-09-5），
    因此名字合法性**不在本段校验**，由 MOD-02 ``SandboxEnv`` 经
    ``sandbox/api.py::EpisodeConfig.from_any`` 对照 ``TOGGLE_KEYS`` 校验，
    未登记的名字报 ``E_ENV_CONFIG_INVALID``（M0 验收 F1）。
    """

    difficulty: str = "hard"
    stage_count: int = 6
    character: str = "warrior"
    toggles: tuple[str, ...] = (
        "mapBranching", "relics", "events", "shops", "schoolCounter",
    )
    max_env_steps: int = 1000 # env 步硬上限（6 关实测最大约 350）
    max_decisions: int = 300 # 决策点上限

    def validate(self) -> None:
        _check("episode.difficulty", self.difficulty, str, choices={"easy", "medium", "hard"})
        _check("episode.stage_count", self.stage_count, int, minv=1, maxv=20)
        _check("episode.max_env_steps", self.max_env_steps, int, minv=1, maxv=10000)
        _check("episode.max_decisions", self.max_decisions, int, minv=1, maxv=1000)


@dataclass(frozen=True)
class SandboxConfig:
    """MOD-02 沙箱：投影、守卫、受限执行、审计、蜜罐。"""

    @dataclass(frozen=True)
    class Projection:
        mode: str = "strict" # strict 记裁剪计数；assert 命中隐藏字段抛 E_ENV_SCHEMA
        schema_version: str = "obs-agent-1.0.0"

    @dataclass(frozen=True)
    class Render:
        max_tokens_p95: int = 1500

    @dataclass(frozen=True)
    class Guard:
        enforce: str = "enforce" # enforce | warn
        scan_roots: tuple[str, ...] = (
            "dice_ratchet/agent", "dice_ratchet/memory", "dice_ratchet/loop",
            "dice_ratchet/evolve", "dice_ratchet/evals", "dice_ratchet/inference",
        )
        process_boundary: bool = False

    @dataclass(frozen=True)
    class Exec:
        timeout_s: float = 2.0
        mem_limit_mb: int = 256
        cpu_limit_s: int = 2
        max_tools: int = 32

    @dataclass(frozen=True)
    class Audit:
        enabled: bool = True
        surfaces: tuple[str, ...] = ("prompt", "memory", "log")
        ruleset: str = "1.0.0"
        fail_on: str = "critical"
        evidence_chars: int = 12

    @dataclass(frozen=True)
    class Honeypot:
        enabled: bool = True
        plan_path: str = "configs/honeypot.yaml"
        self_test: bool = True

    projection: Projection = field(default_factory=Projection)
    render: Render = field(default_factory=Render)
    guard: Guard = field(default_factory=Guard)
    exec: Exec = field(default_factory=Exec)
    policy_failure_streak: int = 3
    audit: Audit = field(default_factory=Audit)
    honeypot: Honeypot = field(default_factory=Honeypot)

    def validate(self) -> None:
        _check("sandbox.projection.mode", self.projection.mode, str,
               choices={"strict", "assert"})
        _check("sandbox.projection.schema_version", self.projection.schema_version, str)
        _check("sandbox.render.max_tokens_p95", self.render.max_tokens_p95, int, minv=100)
        _check("sandbox.guard.enforce", self.guard.enforce, str, choices={"enforce", "warn"})
        _check("sandbox.exec.timeout_s", self.exec.timeout_s, float, minv=0.1)
        _check("sandbox.exec.max_tools", self.exec.max_tools, int, minv=0, maxv=256)
        _check("sandbox.policy_failure_streak", self.policy_failure_streak, int, minv=1)
        _check("sandbox.audit.fail_on", self.audit.fail_on, str,
               choices={"critical", "warning", "info"})


@dataclass(frozen=True)
class EnvConfig:
    """MOD-01 环境规则 + MOD-02 沙箱 + 局配置。"""

    content_dir: str = "src/game/content"
    golden_dir: str = "src/game/tests/golden"
    check_invariants: bool = True
    episode: EpisodeConfig = field(default_factory=EpisodeConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    def validate(self) -> None:
        _check("env.content_dir", self.content_dir, str)
        _check("env.golden_dir", self.golden_dir, str)
        _check("env.check_invariants", self.check_invariants, bool)
        self.episode.validate()
        self.sandbox.validate()


@dataclass(frozen=True)
class PolicyConfig:
    """MOD-04 决策策略（policy.*）。训练期采样参数可被 harness/config.json 覆盖。"""

    mode: str = "play" # play | eval | baseline_fixed_prompt | baseline_heuristic
    template_path: str = "harness/prompts/decision.md"
    # F12（M1 验收）：默认值必须指向**真实存在**的文件。基线模板与任务说明都随包冻结
    # （R-6：`fixed_prompt` 基线不随 harness 演化），因此指向包内路径而不是 harness/。
    fixed_prompt_path: str = "dice_ratchet/agent/prompts/fixed_v1.md"
    task_path: str = "dice_ratchet/agent/prompts/task.md"
    prompt_token_budget: int = 3072
    token_estimator: str = "chars_div_3" # chars_div_3 | backend_tokenizer
    memory_top_k: int = 5
    reflection_max: int = 3
    retry_max: int = 2 # 同步重试次数 R
    illegal_streak_limit: int = 3
    illegal_penalty: float = -0.05
    temperature: float = 0.7 # 训练探索温度（运行期 clamp 0.7–1.2）
    top_p: float = 0.95
    max_tokens: int = 320
    max_plan_len: int = 12
    tool_timeout_ms: int = 50
    tools_enabled: bool = True
    heuristic_fallback: bool = True
    reasoning_max_chars: int = 400
    sampling_seed_salt: str = "policy-v1" # 采样 seed 派生盐（INV-6）
    leak_scan: bool = True # 恒 true；配成 false 视为配置错误

    def validate(self) -> None:
        _check("policy.mode", self.mode, str,
               choices={"play", "eval", "baseline_fixed_prompt", "baseline_heuristic"})
        _check("policy.prompt_token_budget", self.prompt_token_budget, int, minv=256)
        _check("policy.token_estimator", self.token_estimator, str,
               choices={"chars_div_3", "backend_tokenizer"})
        _check("policy.memory_top_k", self.memory_top_k, int, minv=0, maxv=64)
        _check("policy.retry_max", self.retry_max, int, minv=0, maxv=5)
        _check("policy.illegal_streak_limit", self.illegal_streak_limit, int, minv=1)
        _check("policy.temperature", self.temperature, float, minv=0.0, maxv=1.2)
        _check("policy.top_p", self.top_p, float, minv=0.0, maxv=1.0)
        _check("policy.max_tokens", self.max_tokens, int, minv=1, maxv=4096)
        _check("policy.max_plan_len", self.max_plan_len, int, minv=1, maxv=64)
        if not self.leak_scan:
            raise InfraError(E.E_IO_CONFIG_INVALID, "policy.leak_scan 恒必须为 true（INV-5）",
                             hint="检查 configs/default.yaml 或 harness/config.json")


@dataclass(frozen=True)
class AgentConfig:
    """MOD-04 agent 层：policy + tools（tools.* 为 harness 可演化白名单）。"""

    policy: PolicyConfig = field(default_factory=PolicyConfig)
    tools: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.policy.validate()
        _check("agent.tools", self.tools, dict)


@dataclass(frozen=True)
class InferenceConfig:
    """MOD-03 推理后端（inference.*）。"""

    registry_path: str = "configs/models.yaml"
    model_id: str = "qwen3-8b-q4" # 阶段二切 qwen3-30b-a3b-q4
    mode: str = "train" # train | eval；eval 收紧采样并强制 seed（FR-03-07）
    backend: str = "mlx" # mlx | llama | mock
    fallback_backend: str = "mock"
    ram_budget_gb: float = 20.0
    max_attempts: int = 3
    retry_backoff_ms: int = 200
    total_timeout_ms: int = 120000 # 实测回填（与 default.yaml 同值）；原 8000 < 实测 p50，会把正常决策判成 E_LLM_TIMEOUT
    generate_timeout_ms: int = 30000
    max_tokens: int = 256
    reasoning_max_tokens: int = 160
    context_overflow_policy: str = "reject" # reject | truncate
    train_temperature_min: float = 0.7
    seed: int | None = None # 评估模式必须非空（由 MOD-09 seed 注册表传入）
    cache_prompt: bool = True
    kv_cache_bits: int = 4
    max_inflight: int = 4 # 评估模式强制 1
    debug_dump_raw: bool = False
    skip_hash_check: bool = False # 对应 DICERATCHET_INFERENCE_SKIP_HASH=1；CI 必须 false
    llama_server_startup_timeout_ms: int = 60000
    llama_server_port: int = 0 # 0 = 自动分配空闲端口
    # ---- API provider（deepseek 等在线后端；Issue #1「DeepSeek API 适配要求」）----------
    #: ``live`` 真调 API；``replay`` 只从 ``api_replay_dir`` 读历史响应（复现/调试，
    #: 不得冒充新的独立实验样本）。Qwen 主线与两者无关，始终可离线运行。
    api_mode: str = "live"
    api_replay_dir: str = "" # api_mode=replay 时必须非空
    api_record_dir: str = "llm_api" # 相对 run 目录：请求/响应 hash、usage、成本
    api_record_enabled: bool = True # 关掉只影响审计工件，不影响调用
    api_connect_timeout_ms: int = 10000 # 单次 HTTP 连接/读取超时
    api_max_retries: int = 2 # 传输层有限重试（429/5xx/超时）；schema 修复重试仍归 MOD-03 三级策略
    #: API key 的**本地便利文件**（默认仓库根的 ``.env``，已在 .gitignore 内）。
    #: 只用于「不想每次 export」：真实环境变量优先；文件里除注册表声明的 key 名以外
    #: 的行一律不生效（不会改进程环境）。``""`` = 关闭该文件。
    api_key_file: str = ".env"

    def validate(self) -> None:
        _check("inference.model_id", self.model_id, str)
        _check("inference.mode", self.mode, str, choices={"train", "eval"})
        _check("inference.backend", self.backend, str, choices={"mlx", "llama", "mock", "deepseek"})
        _check("inference.ram_budget_gb", self.ram_budget_gb, float, minv=1.0)
        _check("inference.max_attempts", self.max_attempts, int, minv=1, maxv=5)
        _check("inference.max_tokens", self.max_tokens, int, minv=1, maxv=8192)
        _check("inference.context_overflow_policy", self.context_overflow_policy, str,
               choices={"reject", "truncate"})
        _check("inference.train_temperature_min", self.train_temperature_min, float,
               minv=0.0, maxv=1.2)
        _check("inference.api_mode", self.api_mode, str, choices={"live", "replay"})
        _check("inference.api_max_retries", self.api_max_retries, int, minv=0, maxv=5)
        _check("inference.api_connect_timeout_ms", self.api_connect_timeout_ms, int, minv=100)
        _check("inference.api_record_dir", self.api_record_dir, str)
        _check("inference.api_key_file", self.api_key_file, str)
        if self.api_mode == "replay" and not self.api_replay_dir:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             "inference.api_mode=replay 时 inference.api_replay_dir 必须非空",
                             hint="指向含 <request_hash>.json 的历史响应目录")
        if self.mode == "eval" and self.seed is None:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             "inference.mode=eval 时 inference.seed 必须非空（INV-6/B-14）")
        if self.skip_hash_check:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             "inference.skip_hash_check=true 仅限本地调试，CI 必须 false")


#: 模型角色（Issue #1 §2）：policy / reflection / proposer。
#: 三个角色都必须经 MOD-03 的统一 ``InferenceClient`` 取模型，不得各自绑定具体后端。
MODEL_ROLES: Final[tuple[str, ...]] = ("policy", "reflection", "proposer")

#: harness 演化开关在臂注册表里的取值（Issue #1 §1 的 Harness 列）。
ARM_HARNESS_MODES: Final[tuple[str, ...]] = ("fixed", "evolve")

#: 六个实验臂（Issue #1 §1）。``arms`` 只描述「相对 ``models`` 段的差异」，因此
#: A0/A1 不需要任何角色覆盖（全部继承 ``inference.model_id``）。
EXPERIMENT_ARMS: Final[tuple[str, ...]] = ("A0", "A1", "B0", "B1", "C0", "C1")


def _default_arms() -> dict[str, dict[str, str]]:
    """六个实验臂的代码默认值（与 ``configs/default.yaml`` 的 ``experiment.arms`` 等价）。

    臂是**差分**：未列出的角色继承 ``models.<role>`` → ``inference.model_id``。这样
    ``--model mock``（离线冒烟）在 A0/A1 下仍能一次覆盖全部角色（既有语义不破），
    而 B/C 臂显式钉住 proposer / policy 的 DeepSeek 绑定（明确的实验意图不得被静默覆盖）。
    """
    qwen = {"policy": "", "reflection": "", "proposer": ""}
    return {
        "A0": {**qwen, "harness": "fixed"},
        "A1": {**qwen, "harness": "evolve"},
        "B0": {**qwen, "proposer": "deepseek-chat", "harness": "fixed"},
        "B1": {**qwen, "proposer": "deepseek-chat", "harness": "evolve"},
        "C0": {"policy": "deepseek-chat", "reflection": "deepseek-chat",
               "proposer": "deepseek-chat", "harness": "fixed"},
        "C1": {"policy": "deepseek-chat", "reflection": "deepseek-chat",
               "proposer": "deepseek-chat", "harness": "evolve"},
    }


@dataclass(frozen=True)
class ModelRolesConfig:
    """角色 → ``model_id`` 绑定（Issue #1 §2「建议配置形态」的第一种）。

    空字符串 = 继承 ``inference.model_id``。这条继承规则是**刻意保留**的：CLI
    ``--model`` 写的是 ``inference.model_id``，它在「角色显式绑定」之外一次覆盖全部角色，
    所以 ``--model mock`` 的离线冒烟语义不变；而配置里显式写了角色键，就是明确的绑定意图，
    优先级高于 ``--model``。
    """

    policy: str = ""
    reflection: str = ""
    proposer: str = ""

    def validate(self) -> None:
        for role in MODEL_ROLES:
            _check(f"models.{role}", getattr(self, role), str)


@dataclass(frozen=True)
class ExperimentConfig:
    """实验臂（Issue #1 §1 / §2 的第二种配置形态）。

    优先级（高 → 低）::

        experiment.roles.<role>  >  experiment.arms[experiment.arm].<role>
                                 >  models.<role>  >  inference.model_id

    ``arm=""`` 表示不选臂，只用 ``models`` / ``inference.model_id`` 绑定（冒烟与自定义
    对照用）。``arms[arm].harness`` 只登记该臂的 harness 是否演化：本段**只做登记与
    校验**，不改 MOD-06 的演化开关（那是 Issue #1 任务 6 的范围）。
    """

    arm: str = "A1"
    arms: dict[str, dict[str, str]] = field(default_factory=_default_arms)
    roles: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        _check("experiment.arm", self.arm, str)
        if self.arm and self.arm not in self.arms:
            raise InfraError(
                E.E_IO_CONFIG_INVALID,
                f"experiment.arm={self.arm!r} 不在 experiment.arms 内",
                hint=f"合法臂：{', '.join(sorted(self.arms))}（空字符串 = 不选臂）")
        for arm_id, spec in self.arms.items():
            if not isinstance(spec, dict):
                raise InfraError(E.E_IO_CONFIG_INVALID,
                                 f"experiment.arms.{arm_id} 必须是对象")
            unknown = set(spec) - set(MODEL_ROLES) - {"harness"}
            if unknown:
                raise InfraError(
                    E.E_IO_CONFIG_INVALID,
                    f"experiment.arms.{arm_id} 含未知键：{', '.join(sorted(unknown))}",
                    hint=f"合法键：{', '.join(MODEL_ROLES)}, harness")
            harness = str(spec.get("harness", ""))
            if harness not in ARM_HARNESS_MODES:
                raise InfraError(
                    E.E_IO_CONFIG_INVALID,
                    f"experiment.arms.{arm_id}.harness={harness!r} 非法",
                    hint=f"合法值：{', '.join(ARM_HARNESS_MODES)}")
            for role in MODEL_ROLES:
                _check(f"experiment.arms.{arm_id}.{role}", str(spec.get(role, "")), str)
        for role in self.roles:
            if role not in MODEL_ROLES:
                raise InfraError(
                    E.E_IO_CONFIG_INVALID,
                    f"experiment.roles 含未知角色：{role!r}",
                    hint=f"合法角色：{', '.join(MODEL_ROLES)}")
            _check(f"experiment.roles.{role}", self.roles[role], str)

    def harness_mode(self) -> str:
        """当前臂登记的 harness 模式（``fixed`` / ``evolve``；未选臂时回落到 ``evolve``）。"""
        if not self.arm:
            return "evolve"
        return str(self.arms.get(self.arm, {}).get("harness", "evolve"))


@dataclass(frozen=True)
class MemoryConfig:
    """MOD-05 反思记忆库（memory.*）。"""

    @dataclass(frozen=True)
    class Extract:
        max_candidates_per_reflection: int = 3
        use_llm: bool = False

    @dataclass(frozen=True)
    class Dedup:
        sim_threshold: float = 0.85
        jaccard_weight: float = 0.5
        char3_weight: float = 0.5

    @dataclass(frozen=True)
    class Conflict:
        auto_supersede: bool = False
        min_refs: int = 5
        winrate_delta: float = 0.15

    @dataclass(frozen=True)
    class Retrieve:
        top_k: int = 8
        token_budget: int = 900
        min_score: float = 0.15
        same_situation_bonus: float = 0.25
        weights: dict[str, float] = field(default_factory=lambda: {
            "tag": 1.0, "bm25": 0.6, "vector": 0.0, "utility": 0.3, "recency": 0.1,
        })
        bm25: dict[str, float] = field(default_factory=lambda: {"k1": 1.2, "b": 0.75})
        recency_lambda: float = 0.01

    @dataclass(frozen=True)
    class Embedding:
        enabled: bool = False
        model: str = "bge-small-zh-v1.5"
        dim: int = 512

    @dataclass(frozen=True)
    class Snapshot:
        every_generation: bool = True

    @dataclass(frozen=True)
    class Scan:
        on_write: bool = True

    enabled: bool = True
    store_format: str = "jsonl" # 阶段一唯一取值
    max_entries: int = 2000
    ttl_ticks: int = 300 # 默认存活训练局数（约 10 代）
    min_age_ticks: int = 30 # 淘汰免疫保护期（1 代）
    compact_every: int = 200
    extract: Extract = field(default_factory=Extract)
    dedup: Dedup = field(default_factory=Dedup)
    conflict: Conflict = field(default_factory=Conflict)
    retrieve: Retrieve = field(default_factory=Retrieve)
    embedding: Embedding = field(default_factory=Embedding)
    snapshot: Snapshot = field(default_factory=Snapshot)
    scan: Scan = field(default_factory=Scan)

    def validate(self) -> None:
        _check("memory.store_format", self.store_format, str, choices={"jsonl"})
        _check("memory.max_entries", self.max_entries, int, minv=1)
        _check("memory.ttl_ticks", self.ttl_ticks, int, minv=1)
        _check("memory.compact_every", self.compact_every, int, minv=1)
        _check("memory.dedup.sim_threshold", self.dedup.sim_threshold, float, minv=0.0, maxv=1.0)
        _check("memory.retrieve.top_k", self.retrieve.top_k, int, minv=1, maxv=64)
        _check("memory.retrieve.token_budget", self.retrieve.token_budget, int, minv=1)
        _check("memory.retrieve.min_score", self.retrieve.min_score, float, minv=0.0, maxv=1.0)
        for k, v in self.retrieve.weights.items():
            _check(f"memory.retrieve.weights.{k}", v, float, minv=0.0)


@dataclass(frozen=True)
class LoopConfig:
    """MOD-06 闭环编排（loop.*）。"""

    k: int = 30 # 每 K 局训练触发一次演化周期
    max_generations: int = 3
    action_retry_r: int = 2
    hitl: bool = False
    reflect_enabled: bool = True
    reflect_timeout_s: float = 60.0
    reflect_context_window: int = 5
    train_concurrency: int = 1 # 固定 1（共享记忆写入），>1 拒绝启动
    eval_concurrency: int = 2
    max_candidates_per_generation: int = 3 # 硬上限 3（Spec §8.6）
    eval_seeds_per_candidate: int = 40
    max_episodes_per_generation: int = 120 # 全系统唯一成本上限（Spec §8.6 D-1：170 → 120）
    n_candidates: int = 1 # D-1：2 → 1
    smoke_n: int = 2 # D-1：5 → 2（必须与 evolve.smoke_n 同步）
    heldout_n: int = 40 # D-3：保持 40（H1 篮子；必须 == eval.n_heldout）
    io_retry: int = 3
    io_backoff_ms: int = 200
    llm_timeout_s: float = 90.0
    fallback_on_llm_failure: bool = True
    state_fsync: bool = True
    retry_reflections_on_resume: bool = False
    max_run_bytes: int = 2 * 1024**3 # 2 GB

    def validate(self) -> None:
        _check("loop.k", self.k, int, minv=1)
        _check("loop.max_generations", self.max_generations, int, minv=1)
        # FR-06-14：训练局必须串行（共享记忆写入）→ 非法并发用 E_IO_CONCURRENCY（退出码 2），
        # 而不是泛化的 E_IO_CONFIG_INVALID（MOD-06 PRD §10 明确登记了该码）。
        if int(self.train_concurrency) != 1:
            raise InfraError(
                E.E_IO_CONCURRENCY,
                f"loop.train_concurrency={self.train_concurrency} 非法：训练局必须串行（固定 1）",
                hint="共享记忆写入无法在训练局之间并发（Spec §8.6 P-10 / FR-06-14）；"
                     "评估局的并发由 loop.eval_concurrency 单独控制")
        _check("loop.eval_concurrency", self.eval_concurrency, int, minv=1, maxv=8)
        _check("loop.max_candidates_per_generation", self.max_candidates_per_generation,
               int, minv=1, maxv=3, extra="Spec §8.6 硬上限 3")
        _check("loop.max_episodes_per_generation", self.max_episodes_per_generation,
               int, minv=1, maxv=1000)


@dataclass(frozen=True)
class EvolveConfig:
    """MOD-07 harness 自进化（evolve.*）。"""

    n_candidates: int = 1 # D-1：2 → 1（必须 == loop.n_candidates）
    n_traj: int = 20 # 证据束最近 train 轨迹摘要条数
    memory_top_k: int = 12
    max_diff_lines: int = 40
    max_diff_chars: int = 4000
    smoke_n: int = 2 # D-1：5 → 2（必须与 loop.smoke_n 同步）
    min_heldout_n: int = 20 # C0 门槛
    win_rate_tol: float = 0.05 # C1 允许下降幅度（D-3：0.0 → 0.05；n=40 的 win_rate 粒度 0.025）
    alpha: float = 0.05 # C2 显著性水平
    gap_threshold: float = 0.15 # C3 win_rate_gap 阈值
    min_entropy_slope: float = -0.01 # C4 熵斜率下限
    max_illegal_rate: float = 0.35 # C5（2026-10-07 §3.1 实测标定：0.05 → 0.35，先松开）
    max_fallback_rate: float = 0.25 # C5（0.10 → 0.25：实测 0.2146）
    max_truncation_rate: float = 0.15 # C5（0.05 → 0.15：实测 0.10）
    # 标定依据：full PolicyAgent 在 H1 子集（seed 2000–2009，n=10，eval）实测
    # illegal 0.3016 / fallback 0.2146 / truncation 0.10，与 fixed_prompt 基线同量级，
    # 原阈值下 C5 会 reject 每一个候选（见 docs/reviews/2026-10-07-M2-S31-full-agent基线实测.md）。
    # M3 的收紧路径是相对门 `candidate ≤ parent + tol`（Spec B-41），不是把本值改回去。
    hitl: bool = True
    gate_timeout_hours: int = 24
    propose_max_retries: int = 2
    evolvable_keys: tuple[str, ...] = (
        "retrieval.top_k", "sampling.temperature", "prompt.show_intent",
    )
    tool_import_allowlist: tuple[str, ...] = (
        "math", "re", "json", "itertools", "functools", "typing", "dataclasses",
    )

    def validate(self) -> None:
        _check("evolve.n_candidates", self.n_candidates, int, minv=1, maxv=3)
        _check("evolve.max_diff_lines", self.max_diff_lines, int, minv=1)
        _check("evolve.max_diff_chars", self.max_diff_chars, int, minv=1)
        _check("evolve.alpha", self.alpha, float, minv=0.0, maxv=1.0)
        _check("evolve.gap_threshold", self.gap_threshold, float, minv=0.0, maxv=1.0)
        _check("evolve.max_illegal_rate", self.max_illegal_rate, float, minv=0.0, maxv=1.0)


@dataclass(frozen=True)
class EvalConfig:
    """MOD-08 测量（eval.*）。"""

    @dataclass(frozen=True)
    class Entropy:
        base: float = 2.0
        kind_space: int = 16

    @dataclass(frozen=True)
    class Collapse:
        c1_slope_threshold: float = -0.010
        c2_ratio_threshold: float = 0.60
        c3_jsd_freeze: float = 0.010
        c3_winrate_jump: float = 0.05
        c4_seq_ratio: float = 0.50
        confirm_generations: int = 2

    @dataclass(frozen=True)
    class Determinism:
        eval_temperature: float = 0.0 # Spec §8.1 B-14：固定值，不可配置为其他值
        train_temperature: float = 0.7
        n_rep: int = 1
        n_rep_nondeterministic: int = 3
        decode_seed: int = 0

    @dataclass(frozen=True)
    class Baseline:
        kinds: tuple[str, ...] = ("random", "heuristic", "fixed_prompt")
        archive_dir: str = "baselines"
        fixed_prompt_harness_version: int = 0

    @dataclass(frozen=True)
    class Ablation:
        arms: tuple[str, ...] = ("full", "no_reflection", "no_self_edit")
        split_basket: str = "H2"

    n_heldout: int = 40 # H1
    n_test: int = 100 # test_full
    n_ablation: int = 40 # H2
    bootstrap_B: int = 10000
    alpha: float = 0.05
    fdr_q: float = 0.10
    gap_threshold: float = 0.15
    t_max: int = 120
    n_workers: int = 2
    # 2026-10-04 实测回填（原 300 s）；注意该超时目前未被 SandboxRunner 执行，见 M1 验收报告 F20
    # 单局**墙钟**预算（F24）：必须 ≥ D×每决策调用数×t_d×n_workers。
    # 实测（fixed_prompt，8B+MLX，2 worker）：D≈185-207、1.6-1.8 调用/决策点、t_d p50 7.9 s
    # → 单局墙钟 ≈90 min；取最坏 300 决策点的 ≈13000 s 作为默认 14400 s。
    seed_timeout_s: int = 14400
    min_support: int = 3
    drop_rate_max: float = 0.10
    budget_seconds: int | None = None
    entropy: Entropy = field(default_factory=Entropy)
    collapse: Collapse = field(default_factory=Collapse)
    determinism: Determinism = field(default_factory=Determinism)
    baseline: Baseline = field(default_factory=Baseline)
    ablation: Ablation = field(default_factory=Ablation)

    def validate(self) -> None:
        _check("eval.n_heldout", self.n_heldout, int, minv=1)
        _check("eval.n_test", self.n_test, int, minv=1)
        _check("eval.alpha", self.alpha, float, minv=0.0, maxv=1.0)
        _check("eval.seed_timeout_s", self.seed_timeout_s, int, minv=1)
        if self.determinism.eval_temperature != 0.0:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             "eval.determinism.eval_temperature 固定为 0.0（Spec §8.1 B-14）")


@dataclass(frozen=True)
class InfraConfig:
    """MOD-09 工程横切（infra.*，本 PRD §9）。"""

    runs_root: str = "runs" # 不参与 config_hash
    log_level: str = "INFO" # 不参与 config_hash
    fsync_every: int = 0 # 0 = 不 fsync
    rotate_max_mb: int = 512
    threads: int = 1 # 传给推理后端与 numpy（确定性）
    utc: bool = True
    redaction_enabled: bool = True
    plot_enabled: bool = True
    git_enabled: bool = True
    exit_on_guard: bool = True
    config_dir: str = "configs" # 不参与 config_hash
    schemas_dir: str = "schemas" # 不参与 config_hash

    def validate(self) -> None:
        _check("infra.log_level", self.log_level, str,
               choices={"DEBUG", "INFO", "WARNING", "ERROR"})
        _check("infra.rotate_max_mb", self.rotate_max_mb, int, minv=1)
        _check("infra.threads", self.threads, int, minv=1, maxv=8)


@dataclass(frozen=True)
class AppConfig:
    """顶层配置（frozen dataclass，Spec §6.1）。"""

    env: EnvConfig = field(default_factory=EnvConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    models: ModelRolesConfig = field(default_factory=ModelRolesConfig)
    experiment: ExperimentConfig = field(default_factory=ExperimentConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    evolve: EvolveConfig = field(default_factory=EvolveConfig)
    evals: EvalConfig = field(default_factory=EvalConfig)
    infra: InfraConfig = field(default_factory=InfraConfig)

    def validate(self) -> None:
        """显式校验（FR-09-2）：失败抛 E_IO_CONFIG_INVALID，信息含键路径与期望类型。

        先按 dataclass 注解递归做**类型**校验（覆盖全部字段），各段 ``validate()``
        再补区间/枚举/跨段一致性。
        """
        _validate_types(self)
        self.env.validate()
        self.agent.validate()
        self.inference.validate()
        self.models.validate()
        self.experiment.validate()
        self.memory.validate()
        self.loop.validate()
        self.evolve.validate()
        self.evals.validate()
        self.infra.validate()
        # 跨段一致性（MOD-06 §9：由 MOD-09 配置合并时校验）
        if self.loop.n_candidates != self.evolve.n_candidates:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"loop.n_candidates={self.loop.n_candidates} 必须等于 "
                             f"evolve.n_candidates={self.evolve.n_candidates}")
        if self.loop.heldout_n != self.evals.n_heldout:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"loop.heldout_n={self.loop.heldout_n} 必须等于 "
                             f"eval.n_heldout={self.evals.n_heldout}")


# =====================================================================
# 校验辅助
# =====================================================================

def _check(path: str, value: Any, typ: type, *, minv: Any = None, maxv: Any = None,
           choices: Any = None, extra: str | None = None) -> None:
    """单字段类型/区间校验，错误信息必含键路径与期望类型（FR-09-2）。"""
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _type_error(path, value, typ)
    elif typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error(path, value, typ)
    elif not isinstance(value, typ):
        raise _type_error(path, value, typ)
    if minv is not None and value < minv:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"{path}={value!r} 小于下界 {minv!r}" + (f"（{extra}）" if extra else ""))
    if maxv is not None and value > maxv:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"{path}={value!r} 大于上界 {maxv!r}" + (f"（{extra}）" if extra else ""))
    if choices is not None and value not in choices:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"{path}={value!r} 不在允许集合 {sorted(map(str, choices))} 内")


def _type_error(path: str, value: Any, typ: type) -> InfraError:
    return InfraError(E.E_IO_CONFIG_INVALID,
                      f"配置项 {path} 的值 {value!r} 类型为 {type(value).__name__}，"
                      f"期望 {typ.__name__}",
                      hint="检查 configs/default.yaml、harness/config.json、"
                           "DICERATCHET_* 环境变量或 CLI 覆盖")


# =====================================================================
# 由 dataclass 注解派生的类型校验（FR-09-2：类型不符 → E_IO_CONFIG_INVALID）
# =====================================================================

def _hint_label(hint: Any) -> str:
    if isinstance(hint, str):
        return hint
    return getattr(hint, "__name__", None) or str(hint).replace("typing.", "")


def _matches_hint(value: Any, hint: Any) -> bool:
    """值是否符合注解（``int | None`` / ``tuple[str, ...]`` / ``dict[str, float]`` 等）。

    标量策略：``bool`` 必须恰好是 bool；``int`` 拒绝 bool；``float`` 接受 int
    （YAML/环境变量里的 ``1`` 与 ``1.0`` 等价）。无法解析的注解（字符串）不阻塞。
    """
    if hint is Any or hint is object or hint is None or isinstance(hint, str):
        return True
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        return any(_matches_hint(value, arg) for arg in get_args(hint))
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if hint is str:
        return isinstance(value, str)
    if origin is tuple:
        if not isinstance(value, (tuple, list)):
            return False
        args = get_args(hint)
        if not args:
            return True
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches_hint(v, args[0]) for v in value)
        return len(args) == len(value) and all(
            _matches_hint(v, a) for v, a in zip(value, args))
    if origin is list:
        if not isinstance(value, (list, tuple)):
            return False
        args = get_args(hint)
        return all(_matches_hint(v, args[0]) for v in value) if args else True
    if origin in (dict, Mapping):
        if not isinstance(value, Mapping):
            return False
        args = get_args(hint)
        if len(args) != 2:
            return True
        return all(_matches_hint(k, args[0]) and _matches_hint(v, args[1])
                   for k, v in value.items())
    if isinstance(hint, type):
        return isinstance(value, hint)
    return True


def _validate_types(obj: Any, prefix: str = "") -> None:
    """递归按注解校验全部配置段（覆盖 182 个标量键，补齐手写 ``_check`` 的盲区）。"""
    hints = _resolved_hints(type(obj))
    for f in fields(obj):
        value = getattr(obj, f.name)
        path = f"{prefix}.{f.name}" if prefix else f.name
        if is_dataclass(value) and not isinstance(value, type):
            _validate_types(value, path)
            continue
        hint = hints.get(f.name, f.type)
        if not _matches_hint(value, hint):
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"配置项 {path} 的值 {value!r} 类型为 {type(value).__name__}，"
                             f"期望 {_hint_label(hint)}",
                             hint="检查 configs/default.yaml、harness/config.json、"
                                  "DICERATCHET_* 环境变量或 CLI 覆盖")


def _coerce_value(value: Any, hint: Any) -> Any:
    """YAML 的 list → 注解声明的 tuple（保持 frozen dataclass 的类型契约）。"""
    if get_origin(hint) is tuple and isinstance(value, list):
        return tuple(value)
    return value


# =====================================================================
# 扁平化 / 覆盖
# =====================================================================

def _field_map(cfg: AppConfig) -> dict[str, Any]:
    """段名 → 段对象。"""
    return {f.name: getattr(cfg, f.name) for f in fields(cfg)}


def flatten_config(cfg: AppConfig) -> dict[str, Any]:
    """把 AppConfig 展平为 ``{"env.content_dir": ..., "loop.k": ..., ...}``（任意深度）。

    tuple/dict 值原样保留（list 化交给 JSON 序列化端）。
    """
    return _flatten(_to_plain(cfg), "")


def _flatten(obj: Any, prefix: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
    return out


def _set_by_path(cfg_dict: dict[str, Any], path: str, value: Any) -> None:
    """按 ``section.sub.key`` 写入嵌套 dict（段/子段不存在则创建）。"""
    parts = path.split(".")
    if parts[0] not in _SECTIONS:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"未知配置段 {parts[0]!r}（键 {path}）",
                         hint=f"合法段：{', '.join(_SECTIONS)}")
    node = cfg_dict
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"键 {path} 与现有值冲突（{p} 不是对象）")
    node[parts[-1]] = value


_SECTIONS: Final[tuple[str, ...]] = tuple(f.name for f in fields(AppConfig))


# 文档键前缀 → AppConfig 键路径的映射（保持各 PRD 的键名不变）
# policy.* -> agent.policy.* / episode.* -> env.episode.* / sandbox.* -> env.sandbox.*
# eval.*   -> evals.*
_KEY_PREFIX_ALIASES: Final[dict[str, str]] = {
    "policy.": "agent.policy.",
    "episode.": "env.episode.",
    "sandbox.": "env.sandbox.",
    "eval.": "evals.",
}
# YAML 顶层段名归一（目前只有 eval -> evals 一个真实冲突）
_SECTION_NAME_ALIASES: Final[dict[str, str]] = {"eval": "evals"}


def _normalize_flat_key(key: str) -> str:
    """``eval.n_heldout`` → ``evals.n_heldout`` 等（见模块 docstring 的映射表）。"""
    for alias, target in _KEY_PREFIX_ALIASES.items():
        if key == alias[:-1] or key.startswith(alias):
            return target + key[len(alias):]
    return key


def _normalize_sections(data: dict[str, Any]) -> dict[str, Any]:
    """YAML 顶层段名归一（``eval:`` → ``evals:``）。"""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        new_key = _SECTION_NAME_ALIASES.get(key, key)
        if new_key in out:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"配置段 {key} 与 {new_key} 同时出现（互为别名）")
        out[new_key] = value
    return out


def apply_overrides(cfg_dict: dict[str, Any], overrides: Mapping[str, Any],
                    *, source: str = "cli") -> tuple[dict[str, Any], list[str]]:
    """把扁平 ``{"loop.k": 7}`` 覆盖到嵌套 dict；返回 (新 dict, 未知键清单)。

    未知键不中断（FR-09-1：记 ``E_IO_CONFIG_UNKNOWN`` 并忽略）。
    """
    out = _deep_copy(cfg_dict)
    unknown: list[str] = []
    legal = _legal_flat_keys()
    for key, value in overrides.items():
        nkey = _normalize_flat_key(key)
        if nkey not in legal:
            unknown.append(key)
            continue
        _set_by_path(out, nkey, value)
    return out, unknown


def _legal_flat_keys() -> frozenset[str]:
    """全部合法的扁平键（用于识别未知键）。"""
    return frozenset(_flatten(_to_plain(AppConfig()), ""))


def _deep_copy(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _deep_copy(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_deep_copy(v) for v in d]
    return d


# =====================================================================
# 加载
# =====================================================================

def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise InfraError(E.E_IO_CONFIG_INVALID, f"YAML 解析失败：{path}\n{exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"{path} 顶层必须是对象，实际是 {type(data).__name__}")
    return data


def _env_overrides(env: Mapping[str, str]
                   ) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """``DICERATCHET_<SECTION>__<KEY>`` → ``{"section.key": value}``（B-30，``__`` 分层）。

    返回 ``(覆盖, 未知清单, 生效变量名)``。未知键**不中断**（FR-09-1）：既记录
    推导出的键路径，也记录原始变量名，便于直接定位拼写错误。值做轻量类型推断：
    ``true/false`` → bool，纯整数 → int，其余保持 str。
    """
    out: dict[str, Any] = {}
    unknown: list[str] = []
    applied: dict[str, str] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        body = name[len(ENV_PREFIX):]
        # 跳过非配置类的 DICERATCHET_ 开关（如 DICERATCHET_INFERENCE_SKIP_HASH 显式键不在此列）
        if "__" not in body:
            continue
        parts = body.lower().split("__")
        key = _normalize_flat_key(".".join(parts)) # eval.* -> evals.* 等
        if not key or key.split(".", 1)[0] not in _SECTIONS:
            # 未知段：记录后忽略（FR-09-1 的「记 E_IO_CONFIG_UNKNOWN 并忽略」）
            unknown.extend([key, name] if key else [name])
            continue
        out[key] = _infer_type(raw)
        applied[key] = name
    return out, unknown, applied


def _infer_type(raw: Any) -> Any:
    raw = str(raw)
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def load_config(cli_overrides: Mapping[str, Any] | None = None,
                env: Mapping[str, str] | None = None,
                config_dir: Path = Path("configs"),
                harness_config: Path = Path("harness/config.json")) -> AppConfig:
    """按优先级合并并校验（FR-09-1 / FR-09-2）。

    cli_overrides 为扁平 ``{"loop.k": 7}``；env 为 ``os.environ`` 视图。
    返回 ``AppConfig``；未知键通过 ``load_config.unknown_keys`` 附加返回。
    """
    config_dir = Path(config_dir)
    merged: dict[str, Any] = _deep_copy(_default_dict())
    unknown_all: list[str] = []

    # 1) configs/default.yaml（+ 可选的 configs/<profile>.yaml，由 CLI 传 profile）
    file_cfg = _normalize_sections(_load_yaml(config_dir / "default.yaml"))
    merged = _merge_dicts(merged, file_cfg)
    # 静态基线：harness 层之前的视图，供 config_hash 回退 harness 覆盖（B-19）
    static_baseline = _deep_copy(merged)

    # 2) harness/config.json（可演化层，白名单校验；计入 artifact_hash，不计入 config_hash）
    harness_keys, harness_cfg = _load_harness_config(harness_config)
    merged = _merge_dicts(merged, harness_cfg)

    # 3) DICERATCHET_* 环境变量
    env_over, env_unknown, env_applied = _env_overrides(os.environ if env is None else env)
    unknown_all.extend(env_unknown)
    applied_names: list[str] = []
    if env_over:
        merged, unknown = apply_overrides(merged, env_over, source="env")
        unknown_all.extend(unknown)
        rejected = set(unknown)
        applied_names = [env_applied[k] for k in sorted(env_over)
                         if k not in rejected and k in env_applied]

    # 4) CLI 显式参数（最高优先级）
    if cli_overrides:
        merged, unknown = apply_overrides(merged, cli_overrides, source="cli")
        unknown_all.extend(unknown)

    cfg = _from_dict(merged)
    cfg.validate()
    # 溯源信息挂在实例私有属性上：不参与 asdict / 字段列表 / 相等性 / JSON 快照
    object.__setattr__(cfg, _HARNESS_STATIC_ATTR,
                       _harness_static_values(static_baseline, harness_keys))
    object.__setattr__(cfg, _ENV_APPLIED_ATTR, tuple(applied_names))
    load_config.unknown_keys = unknown_all # type: ignore[attr-defined]
    return cfg


def _harness_static_values(static: Mapping[str, Any],
                           keys: Sequence[str]) -> dict[str, tuple[bool, Any]]:
    """harness 覆盖键 → ``(静态基线是否存在, 静态值)``（B-19 的 hash 回退依据）。"""
    out: dict[str, tuple[bool, Any]] = {}
    for key in keys:
        node: Any = static
        found = True
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                found = False
                break
        out[key] = (found, _deep_copy(node) if found else None)
    return out


def _to_plain(obj: Any) -> Any:
    """递归把 dataclass 实例转成纯 dict/list（任意深度），供默认值合并与扁平化复用。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    return obj


def _default_dict() -> dict[str, Any]:
    """AppConfig 全默认值的嵌套 dict 视图（任意深度）。"""
    return {name: _to_plain(sec) for name, sec in _field_map(AppConfig()).items()}


def _merge_dicts(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = _deep_copy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dicts(out[k], v)
        else:
            out[k] = _deep_copy(v)
    return out


def _load_harness_config(path: Path) -> tuple[list[str], dict[str, Any]]:
    """harness/config.json：扁平点分键，只接受白名单，否则 ``E_IO_CONFIG_INVALID``（FR-09-1）。

    返回 ``(归一化后的键路径列表, 已展开为嵌套结构的 dict)``。键名允许用文档短前缀
    （``policy.*``）或全路径（``agent.policy.*``），统一归一后校验白名单。键路径列表
    是 B-19 的 hash 回退依据：这一层不计入 ``config_hash``。
    """
    if not Path(path).is_file():
        return [], {}
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            flat = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise InfraError(E.E_IO_CONFIG_INVALID, f"harness/config.json 解析失败：{exc}") from exc
    if not isinstance(flat, dict):
        raise InfraError(E.E_IO_CONFIG_INVALID, "harness/config.json 顶层必须是对象")
    normalized = {_normalize_flat_key(k): v for k, v in flat.items()}
    bad = [k for k in normalized
           if not any(k == p.rstrip(".") or k.startswith(p)
                      for p in HARNESS_CONFIG_ALLOWED_PREFIXES)]
    if bad:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"harness/config.json 含非白名单键 {bad}（白名单："
                         f"{list(HARNESS_CONFIG_ALLOWED_PREFIXES)}）",
                         hint="harness 层只能含 agent.policy.* 训练期采样参数、"
                              "memory.retrieve.*、agent.tools.*")
    expanded: dict[str, Any] = {}
    for key, value in normalized.items():
        _set_by_path(expanded, key, value)
    return list(normalized), expanded


def _from_dict(merged: Mapping[str, Any]) -> AppConfig:
    """嵌套 dict → AppConfig，未知段/键在构造期即被 dataclass 拒绝。"""
    unknown_sections = set(merged) - set(_SECTIONS)
    if unknown_sections:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"未知配置段 {sorted(unknown_sections)}",
                         hint=f"合法段：{', '.join(_SECTIONS)}")
    kwargs: dict[str, Any] = {}
    for name in _SECTIONS:
        sec_data = merged.get(name, {})
        if not isinstance(sec_data, dict):
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"配置段 {name} 必须是对象，实际是 {type(sec_data).__name__}")
        kwargs[name] = _build_section(_SECTION_TYPES[name], sec_data, name)
    return AppConfig(**kwargs)


_SECTION_TYPES: Final[dict[str, type]] = {
    "env": EnvConfig, "agent": AgentConfig, "inference": InferenceConfig,
    "models": ModelRolesConfig, "experiment": ExperimentConfig,
    "memory": MemoryConfig, "loop": LoopConfig, "evolve": EvolveConfig,
    "evals": EvalConfig, "infra": InfraConfig,
}


_HINT_CACHE: dict[type, dict[str, Any]] = {}


def _resolved_hints(typ: type) -> dict[str, Any]:
    """解析 dataclass 字段类型（``from __future__ import annotations`` 使 f.type 为字符串）。"""
    hints = _HINT_CACHE.get(typ)
    if hints is None:
        import typing
        try:
            hints = typing.get_type_hints(typ)
        except Exception: # pragma: no cover - 无前向引用的 dataclass 不会失败
            hints = {f.name: f.type for f in fields(typ)}
        _HINT_CACHE[typ] = hints
    return hints


def _build_section(typ: type, data: Mapping[str, Any], prefix: str) -> Any:
    field_types = _resolved_hints(typ)
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in field_types:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"未知配置项 {prefix}.{key}",
                             hint=f"合法项：{', '.join(sorted(field_types))}")
        ftype = field_types[key]
        if isinstance(ftype, type) and is_dataclass(ftype):
            if not isinstance(value, dict):
                raise InfraError(E.E_IO_CONFIG_INVALID,
                                 f"{prefix}.{key} 必须是对象，实际是 {type(value).__name__}")
            kwargs[key] = _build_section(ftype, value, f"{prefix}.{key}")
        else:
            kwargs[key] = _coerce_value(value, ftype)
    return typ(**kwargs)


# =====================================================================
# 哈希与种子（FR-09-3 / NFR-09-3）
# =====================================================================

def _canonical(cfg: AppConfig, *, harness_neutral: bool = False) -> dict[str, Any]:
    """排除易变字段与所有绝对路径后的可哈希视图（FR-09-3）。

    ``harness_neutral=True`` 时额外把 ``harness/config.json`` 覆盖过的键**回退到静态基线**
    （``configs/default.yaml`` + dataclass 默认值）。这是 B-19 的要求：harness 层计入
    ``artifact_hash``、不计入 ``config_hash``，否则第一代被接受后 ``--resume`` 必然冲突。
    基线中不存在的键（如 harness 新增的 ``agent.tools.*``）整键删除。
    """
    data = json.loads(json.dumps(asdict(cfg), default=_json_default))
    if harness_neutral:
        for path, (present, value) in getattr(cfg, _HARNESS_STATIC_ATTR, {}).items():
            baseline = json.loads(json.dumps(value, default=_json_default)) if present else None
            _restore_path(data, path, baseline, present)
    for key in _HASH_VOLATILE_KEYS:
        _del_path(data, key)
    _strip_absolute_paths(data)
    return data


def _restore_path(data: dict[str, Any], path: str, value: Any, present: bool) -> None:
    """把 ``path`` 回退为静态基线（``present=False`` 时删除该键）。"""
    parts = path.split(".")
    node: Any = data
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return
        node = node[p]
    if not isinstance(node, dict):
        return
    if present:
        node[parts[-1]] = value
    else:
        node.pop(parts[-1], None)


def harness_overridden_keys(cfg: AppConfig) -> tuple[str, ...]:
    """``harness/config.json`` 覆盖过的键路径（不参与 ``config_hash``，见 B-19）。"""
    return tuple(sorted(getattr(cfg, _HARNESS_STATIC_ATTR, {})))


def env_applied_keys(cfg: AppConfig) -> tuple[str, ...]:
    """本次加载真正生效的 ``DICERATCHET_*`` 变量名（供 config.json 环境变量白名单）。"""
    return tuple(getattr(cfg, _ENV_APPLIED_ATTR, ()))


def _del_path(data: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = data
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return
        node = node[p]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _strip_absolute_paths(data: Any) -> None:
    if isinstance(data, dict):
        for k, v in list(data.items()):
            if isinstance(v, str) and os.path.isabs(v):
                del data[k]
            else:
                _strip_absolute_paths(v)
    elif isinstance(data, list):
        for item in data:
            _strip_absolute_paths(item)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (tuple, set, frozenset)):
        return sorted(obj)
    raise TypeError(f"不可 JSON 序列化：{type(obj).__name__}")


def full_config(cfg: AppConfig) -> dict[str, Any]:
    """**完整**生效配置（含 volatile 字段与绝对路径），供 ``config.json`` 快照（Spec §7）。

    与 ``canonical_json()`` 的区别：后者为 hash/复现裁剪（剔除易变字段与绝对路径），
    前者必须是忠实的 AppConfig 快照，读者可据此独立复现该次 run 的环境。
    """
    return json.loads(json.dumps(_to_plain(cfg), default=_json_default))


def _dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json(cfg: AppConfig) -> str:
    """规范 JSON 文本（排序、紧凑、非 ASCII 保留）——**生效视图**（含 harness 覆盖）。"""
    return _dumps(_canonical(cfg))


def config_hash(cfg: AppConfig) -> str:
    """``sha256(canonical_json(cfg))[:16]``（FR-09-3），**不含 harness 层**（B-19）。

    harness 的变化由 ``artifact_hash`` 表达；否则第一代被接受后 ``--resume`` 必然冲突。
    """
    return hashlib.sha256(_dumps(_canonical(cfg, harness_neutral=True)).encode("utf-8")
                          ).hexdigest()[:16]


def artifact_hash(harness_dir: Path) -> str:
    """``(相对路径, 文件 sha256)`` 按路径排序后整体 sha256（FR-09-3）。

    harness 层（含 ``harness/config.json``）的指纹，逐代写入 ``version.json`` 与 report。
    """
    harness_dir = Path(harness_dir)
    if not harness_dir.is_dir():
        return "sha256:" + "0" * 64
    items: list[tuple[str, str]] = []
    for path in sorted(harness_dir.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(harness_dir).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        items.append((rel, digest))
    payload = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_seed(*parts: object) -> int:
    """对同一输入返回同一 64-bit 整数（NFR-09-3）。

    用 ``hashlib.blake2b`` 而非 ``hash()``，与进程/平台/``PYTHONHASHSEED`` 无关。
    作用域：agent 侧随机（采样 seed、bootstrap、候选 id）；环境侧流派生由 MOD-01
    按 GAME-01 §3 的 xmur3 + SplitMix64 实现。
    """
    h = hashlib.blake2b(digest_size=8)
    for part in parts:
        h.update(_encode_part(part).encode("utf-8"))
        h.update(b"\x1f") # 单元分隔符，避免 "ab"+"c" 与 "a"+"bc" 撞哈希
    return int.from_bytes(h.digest(), byteorder="big", signed=False)


def _encode_part(part: object) -> str:
    if part is None:
        return "n:"
    if isinstance(part, bool):
        return f"b:{part}"
    if isinstance(part, int):
        return f"i:{part}"
    if isinstance(part, float):
        return f"f:{part!r}"
    if isinstance(part, str):
        return f"s:{part}"
    if isinstance(part, (list, tuple, set, frozenset)):
        return "l:" + json.dumps([_encode_part(p) for p in part], ensure_ascii=False)
    if isinstance(part, dict):
        return "d:" + json.dumps({k: _encode_part(v) for k, v in sorted(part.items())},
                                 ensure_ascii=False)
    if isinstance(part, Path):
        return f"p:{part.as_posix()}"
    return f"o:{type(part).__name__}:{part!r}"


# =====================================================================
# seeds.yaml / models.yaml（加载归 MOD-09，协议校验归 MOD-08）
# =====================================================================

@dataclass(frozen=True)
class SeedsProtocol:
    """``configs/seeds.yaml`` 的协议部分（MOD-08 §9）。"""

    version: int = 1
    hash_algo: str = "sha256"
    splits: dict[str, Any] = field(default_factory=dict)
    exposure_guard: dict[str, Any] = field(default_factory=dict)
    seeds_hash: str | None = None

    def validate(self) -> None:
        if self.hash_algo != "sha256":
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             f"seeds.hash_algo 只支持 sha256，实际 {self.hash_algo!r}")
        ranges = []
        for name, spec in self.splits.items():
            if "start" in spec and "end" in spec:
                ranges.append((name, int(spec["start"]), int(spec["end"])))
            for bname, bspec in (spec.get("baskets") or {}).items():
                ranges.append((f"{name}/{bname}", int(bspec["start"]), int(bspec["end"])))
        for i in range(len(ranges)):
            for j in range(i + 1, len(ranges)):
                a, b = ranges[i], ranges[j]
                if a[0].split("/")[0] == b[0].split("/")[0] and a[0] != b[0]:
                    continue # 同 split 内的篮子由 MOD-08 自校验
                if not (a[2] < b[1] or b[2] < a[1]):
                    raise InfraError(E.E_EVAL_SEED_OVERLAP,
                                     f"seed 区间重叠：{a[0]}[{a[1]}..{a[2]}] 与 "
                                     f"{b[0]}[{b[1]}..{b[2]}]")
        # test 锁定（Spec §8.6 / MOD-08：locked 未解锁即跑 test 直接拒绝）
        test_spec = self.splits.get("test", {})
        if test_spec.get("locked") and not test_spec.get("unlocked"):
            pass # 解锁语义归 MOD-08；本模块只保证加载与区间


def load_seeds(config_dir: Path = Path("configs")) -> SeedsProtocol:
    path = Path(config_dir) / "seeds.yaml"
    data = _load_yaml(path)
    if not data:
        return SeedsProtocol()
    proto = SeedsProtocol(
        version=int(data.get("version", 1)),
        hash_algo=str(data.get("hash_algo", "sha256")),
        splits=data.get("splits", {}),
        exposure_guard=data.get("exposure_guard", {}),
        seeds_hash=data.get("seeds_hash"),
    )
    proto.validate()
    return proto


def load_models(config_dir: Path = Path("configs")) -> dict[str, Any]:
    """``configs/models.yaml`` 模型注册表（MOD-03 定义 schema，本模块只负责加载）。"""
    return _load_yaml(Path(config_dir) / "models.yaml")


load_config.unknown_keys: list[str] = [] # type: ignore[attr-defined]
