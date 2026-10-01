"""配置体系（FR-09-1..4 / Spec §5 配置契约）。

优先级（Spec v1.4 B-26）::

    CLI 显式参数 > GAMERSI_* 环境变量 > harness/config.json（可演化层）
                   > configs/default.yaml > 代码内 dataclass 默认值

键路径到 ``AppConfig`` 八个段的映射（保持各 PRD 文档的键名不变）::

    env.*                -> EnvConfig            （含 env.episode.* / env.sandbox.*）
    episode.*            -> env.episode.*        （Spec §8.6 唯一只读上限）
    sandbox.*            -> env.sandbox.*        （MOD-02）
    policy.*             -> agent.policy.*       （MOD-04）
    inference.*          -> InferenceConfig      （MOD-03）
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
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Final, Mapping

import yaml

from .errors import E, InfraError

__all__ = [
    "AppConfig", "EnvConfig", "EpisodeConfig", "SandboxConfig", "AgentConfig",
    "PolicyConfig", "InferenceConfig", "MemoryConfig", "LoopConfig", "EvolveConfig",
    "EvalConfig", "InfraConfig", "SeedsProtocol",
    "load_config", "load_seeds", "load_models", "config_hash", "canonical_json",
    "derive_seed", "artifact_hash", "flatten_config", "apply_overrides",
]

# harness/config.json 白名单前缀（FR-09-1 / Spec §5 B-26）
HARNESS_CONFIG_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    "agent.policy.", "memory.retrieve.", "agent.tools.",
)
ENV_PREFIX: Final[str] = "GAMERSI_"

# 不参与 config_hash 的易变键（FR-09-3）
_HASH_VOLATILE_KEYS: Final[frozenset[str]] = frozenset({
    "infra.runs_root", "infra.log_level", "infra.threads",
})


# =====================================================================
# 各段配置（默认值取自各模块 PRD 的「配置项」表）
# =====================================================================

@dataclass(frozen=True)
class EpisodeConfig:
    """默认局（Spec §8.6）+ 只读上限。config_hash 覆盖此段（Spec §5）。"""

    difficulty: str = "hard"
    stage_count: int = 6
    character: str = "warrior"
    toggles: tuple[str, ...] = (
        "relics", "forge", "shop", "events", "shrines", "blessings",
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
            "gamer_rsi/agent", "gamer_rsi/memory", "gamer_rsi/loop",
            "gamer_rsi/evolve", "gamer_rsi/evals", "gamer_rsi/inference",
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
    fixed_prompt_path: str = "harness/prompts/fixed_v1.md"
    task_path: str = "harness/prompts/task.md"
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
    total_timeout_ms: int = 8000
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
    skip_hash_check: bool = False # 对应 GAMERSI_INFERENCE_SKIP_HASH=1；CI 必须 false
    llama_server_startup_timeout_ms: int = 60000
    llama_server_port: int = 0 # 0 = 自动分配空闲端口

    def validate(self) -> None:
        _check("inference.model_id", self.model_id, str)
        _check("inference.mode", self.mode, str, choices={"train", "eval"})
        _check("inference.backend", self.backend, str, choices={"mlx", "llama", "mock"})
        _check("inference.ram_budget_gb", self.ram_budget_gb, float, minv=1.0)
        _check("inference.max_attempts", self.max_attempts, int, minv=1, maxv=5)
        _check("inference.max_tokens", self.max_tokens, int, minv=1, maxv=8192)
        _check("inference.context_overflow_policy", self.context_overflow_policy, str,
               choices={"reject", "truncate"})
        _check("inference.train_temperature_min", self.train_temperature_min, float,
               minv=0.0, maxv=1.2)
        if self.mode == "eval" and self.seed is None:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             "inference.mode=eval 时 inference.seed 必须非空（INV-6/B-14）")
        if self.skip_hash_check:
            raise InfraError(E.E_IO_CONFIG_INVALID,
                             "inference.skip_hash_check=true 仅限本地调试，CI 必须 false")


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
    max_episodes_per_generation: int = 170 # 全系统唯一成本上限
    n_candidates: int = 2
    smoke_n: int = 5
    heldout_n: int = 40
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
        _check("loop.train_concurrency", self.train_concurrency, int, minv=1, maxv=1,
               extra="训练局并发度固定 1（共享记忆写入）")
        _check("loop.eval_concurrency", self.eval_concurrency, int, minv=1, maxv=8)
        _check("loop.max_candidates_per_generation", self.max_candidates_per_generation,
               int, minv=1, maxv=3, extra="Spec §8.6 硬上限 3")
        _check("loop.max_episodes_per_generation", self.max_episodes_per_generation,
               int, minv=1, maxv=1000)


@dataclass(frozen=True)
class EvolveConfig:
    """MOD-07 harness 自进化（evolve.*）。"""

    n_candidates: int = 2
    n_traj: int = 20 # 证据束最近 train 轨迹摘要条数
    memory_top_k: int = 12
    max_diff_lines: int = 40
    max_diff_chars: int = 4000
    smoke_n: int = 5
    min_heldout_n: int = 20 # C0 门槛
    win_rate_tol: float = 0.0 # C1 允许下降幅度
    alpha: float = 0.05 # C2 显著性水平
    gap_threshold: float = 0.15 # C3 win_rate_gap 阈值
    min_entropy_slope: float = -0.01 # C4 熵斜率下限
    max_illegal_rate: float = 0.05 # C5
    max_fallback_rate: float = 0.10
    max_truncation_rate: float = 0.05
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
    seed_timeout_s: int = 300
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
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    evolve: EvolveConfig = field(default_factory=EvolveConfig)
    evals: EvalConfig = field(default_factory=EvalConfig)
    infra: InfraConfig = field(default_factory=InfraConfig)

    def validate(self) -> None:
        """显式校验（FR-09-2）：失败抛 E_IO_CONFIG_INVALID，信息含键路径与期望类型。"""
        self.env.validate()
        self.agent.validate()
        self.inference.validate()
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
    if typ is float and isinstance(value, bool):
        raise _type_error(path, value, typ)
    if typ is int and isinstance(value, bool):
        raise _type_error(path, value, typ)
    if not isinstance(value, typ):
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
                           "GAMERSI_* 环境变量或 CLI 覆盖")


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


def apply_overrides(cfg_dict: dict[str, Any], overrides: Mapping[str, Any],
                    *, source: str = "cli") -> tuple[dict[str, Any], list[str]]:
    """把扁平 ``{"loop.k": 7}`` 覆盖到嵌套 dict；返回 (新 dict, 未知键清单)。

    未知键不中断（FR-09-1：记 ``E_IO_CONFIG_UNKNOWN`` 并忽略）。
    """
    out = _deep_copy(cfg_dict)
    unknown: list[str] = []
    legal = _legal_flat_keys()
    for key, value in overrides.items():
        if key not in legal:
            unknown.append(key)
            continue
        _set_by_path(out, key, value)
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


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """``GAMERSI_<SECTION>__<KEY>`` → ``{"section.key": value}``（B-30，``__`` 分层）。

    值做轻量类型推断：``true/false`` → bool，纯整数 → int，其余保持 str。
    """
    out: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        body = name[len(ENV_PREFIX):]
        # 跳过非配置类的 GAMERSI_ 开关（如 GAMERSI_INFERENCE_SKIP_HASH 显式键不在此列）
        if "__" not in body:
            continue
        parts = body.lower().split("__")
        if not parts or parts[0] not in _SECTIONS:
            continue
        out[".".join(parts)] = _infer_type(raw)
    return out


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
    file_cfg = _load_yaml(config_dir / "default.yaml")
    merged = _merge_dicts(merged, file_cfg)

    # 2) harness/config.json（可演化层，白名单校验）
    harness_cfg = _load_harness_config(harness_config)
    merged = _merge_dicts(merged, harness_cfg)

    # 3) GAMERSI_* 环境变量
    env_over = _env_overrides(env if env is not None else {})
    if env_over:
        merged, unknown = apply_overrides(merged, env_over, source="env")
        unknown_all.extend(unknown)

    # 4) CLI 显式参数（最高优先级）
    if cli_overrides:
        merged, unknown = apply_overrides(merged, cli_overrides, source="cli")
        unknown_all.extend(unknown)

    cfg = _from_dict(merged)
    cfg.validate()
    load_config.unknown_keys = unknown_all # type: ignore[attr-defined]
    return cfg


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


def _load_harness_config(path: Path) -> dict[str, Any]:
    """harness/config.json：只接受白名单前缀，否则 E_IO_CONFIG_INVALID（FR-09-1）。"""
    if not Path(path).is_file():
        return {}
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            flat = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise InfraError(E.E_IO_CONFIG_INVALID, f"harness/config.json 解析失败：{exc}") from exc
    if not isinstance(flat, dict):
        raise InfraError(E.E_IO_CONFIG_INVALID, "harness/config.json 顶层必须是对象")
    bad = [k for k in flat
           if not any(k == p.rstrip(".") or k.startswith(p) for p in HARNESS_CONFIG_ALLOWED_PREFIXES)]
    if bad:
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"harness/config.json 含非白名单键 {bad}（白名单："
                         f"{list(HARNESS_CONFIG_ALLOWED_PREFIXES)}）",
                         hint="harness 层只能含 policy.* 训练期采样参数、"
                              "memory.retrieve.*、agent.tools.*")
    return flat


def _from_dict(merged: Mapping[str, Any]) -> AppConfig:
    """嵌套 dict → AppConfig，未知段/键在构造期即被 dataclass 拒绝。"""
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
            kwargs[key] = value
    return typ(**kwargs)


# =====================================================================
# 哈希与种子（FR-09-3 / NFR-09-3）
# =====================================================================

def _canonical(cfg: AppConfig) -> dict[str, Any]:
    """排除易变字段与所有绝对路径后的可哈希视图（FR-09-3）。"""
    data = json.loads(json.dumps(asdict(cfg), default=_json_default))
    for key in _HASH_VOLATILE_KEYS:
        _del_path(data, key)
    _strip_absolute_paths(data)
    return data


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


def canonical_json(cfg: AppConfig) -> str:
    """规范 JSON 文本（排序、紧凑、非 ASCII 保留）。"""
    return json.dumps(_canonical(cfg), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(cfg: AppConfig) -> str:
    """``sha256(canonical_json(cfg))[:16]``（FR-09-3）。

    不含 ``harness_version``（B-19：否则第一代被接受后 ``--resume`` 必然冲突）；
    harness 的变化由 ``artifact_hash`` 表达。
    """
    return hashlib.sha256(canonical_json(cfg).encode("utf-8")).hexdigest()[:16]


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
