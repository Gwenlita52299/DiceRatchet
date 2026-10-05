"""错误码与退出码注册表（Spec §8.3 / FR-09-16）。

全集 ``E_*`` 在此登记，其他模块只能引用、不得自定义字符串错误码。
每个错误码带：人类可读含义、默认退出码（Spec §8.3 的 0/1/2/3/4/5/130）。

退出码约定::

    0   成功（含 HITL 挂起）
    1   领域失败（如验收不达标）
    2   配置/环境错误
    3   模型不可用
    4   沙箱/守卫违例
    5   IO 落盘失败
    130 用户 Ctrl-C
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Mapping

__all__ = ["E", "EXIT_CODE", "ERROR_MESSAGE", "EXIT_OK", "EXIT_DOMAIN", "EXIT_CONFIG",
           "EXIT_MODEL", "EXIT_SANDBOX", "EXIT_IO", "EXIT_INTERRUPT", "error_code_of"]


# ---- 退出码（Spec §8.3）-----------------------------------------------------------
EXIT_OK: Final[int] = 0
EXIT_DOMAIN: Final[int] = 1
EXIT_CONFIG: Final[int] = 2
EXIT_MODEL: Final[int] = 3
EXIT_SANDBOX: Final[int] = 4
EXIT_IO: Final[int] = 5
EXIT_INTERRUPT: Final[int] = 130


class E(StrEnum):
    """全部错误码。命名前缀：``E_ENV_*`` / ``E_ACTION_*`` / ``E_LLM_*`` / ``E_EVOLVE_*`` / ``E_EVAL_*`` / ``E_IO_*``。"""

    # ---- E_ENV_*（MOD-01/02，环境与沙箱）-----------------------------------------
    E_ENV_CONTENT_INVALID = "E_ENV_CONTENT_INVALID"
    E_ENV_CONFIG_INVALID = "E_ENV_CONFIG_INVALID"
    E_ENV_INTERNAL = "E_ENV_INTERNAL"
    E_ENV_RNG = "E_ENV_RNG"
    E_ENV_PARITY_MISMATCH = "E_ENV_PARITY_MISMATCH"
    E_ENV_PROJECTION_MISSING = "E_ENV_PROJECTION_MISSING"
    E_ENV_PROJECTION_VERSION = "E_ENV_PROJECTION_VERSION"
    E_ENV_SCHEMA = "E_ENV_SCHEMA"
    E_ENV_IMPORT_DENIED = "E_ENV_IMPORT_DENIED"
    E_ENV_AST_DENIED = "E_ENV_AST_DENIED"
    E_ENV_CODE_SYNTAX = "E_ENV_CODE_SYNTAX"
    E_ENV_TIMEOUT = "E_ENV_TIMEOUT"
    E_ENV_RESOURCE = "E_ENV_RESOURCE"
    E_ENV_ESCAPE = "E_ENV_ESCAPE"
    E_ENV_AUDIT_IO = "E_ENV_AUDIT_IO"
    E_ENV_HONEYPOT = "E_ENV_HONEYPOT"

    # ---- E_ACTION_*（MOD-04，决策与动作）------------------------------------------
    E_ACTION_TEMPLATE = "E_ACTION_TEMPLATE"
    E_ACTION_PARSE = "E_ACTION_PARSE"
    E_ACTION_SCHEMA = "E_ACTION_SCHEMA"
    E_ACTION_ILLEGAL = "E_ACTION_ILLEGAL"
    E_ACTION_EXEC = "E_ACTION_EXEC"
    E_ACTION_LEGAL_NOISE = "E_ACTION_LEGAL_NOISE"
    E_ACTION_ILLEGAL_STREAK = "E_ACTION_ILLEGAL_STREAK"
    E_ACTION_TOOL_SIG = "E_ACTION_TOOL_SIG"
    E_ACTION_TOOL_GUARD = "E_ACTION_TOOL_GUARD"
    E_ACTION_TOOL_FAIL = "E_ACTION_TOOL_FAIL"
    E_ACTION_BUDGET = "E_ACTION_BUDGET"
    E_ACTION_FALLBACK = "E_ACTION_FALLBACK"
    E_ACTION_LEAK_BLOCK = "E_ACTION_LEAK_BLOCK"

    # ---- E_LLM_*（MOD-03，推理后端；Spec §8.3 以 MOD-03 为准）------------------------
    E_LLM_UNAVAILABLE = "E_LLM_UNAVAILABLE"
    E_LLM_LOAD_FAILED = "E_LLM_LOAD_FAILED"
    E_LLM_OOM = "E_LLM_OOM"
    E_LLM_TIMEOUT = "E_LLM_TIMEOUT"
    E_LLM_SCHEMA_INVALID = "E_LLM_SCHEMA_INVALID"
    E_LLM_CONTEXT_OVERFLOW = "E_LLM_CONTEXT_OVERFLOW"
    E_LLM_BACKEND_ERROR = "E_LLM_BACKEND_ERROR"
    E_LLM_UNSUPPORTED = "E_LLM_UNSUPPORTED" # B-01：可选辅助接口（count_tokens/embed）未实现
    E_LLM_EXTRACT_FAILED = "E_LLM_EXTRACT_FAILED"

    # ---- E_EVOLVE_*（MOD-07，harness 自进化）----------------------------------------
    E_EVOLVE_PROPOSE_FAILED = "E_EVOLVE_PROPOSE_FAILED"
    E_EVOLVE_NO_CANDIDATE = "E_EVOLVE_NO_CANDIDATE"
    E_EVOLVE_PATCH_INVALID = "E_EVOLVE_PATCH_INVALID"
    E_EVOLVE_SCAN_FAILED = "E_EVOLVE_SCAN_FAILED"
    E_EVOLVE_LOAD_FAILED = "E_EVOLVE_LOAD_FAILED"
    E_EVOLVE_SMOKE_FAILED = "E_EVOLVE_SMOKE_FAILED"
    E_EVOLVE_EVAL_FAILED = "E_EVOLVE_EVAL_FAILED"
    E_EVOLVE_NO_GAIN = "E_EVOLVE_NO_GAIN"
    E_EVOLVE_ILLEGAL_RATE = "E_EVOLVE_ILLEGAL_RATE"
    E_EVOLVE_OVERFIT_SUSPECT = "E_EVOLVE_OVERFIT_SUSPECT"
    E_EVOLVE_COLLAPSE = "E_EVOLVE_COLLAPSE"
    E_EVOLVE_JUDGE_FAILED = "E_EVOLVE_JUDGE_FAILED"
    E_EVOLVE_GATE_TIMEOUT = "E_EVOLVE_GATE_TIMEOUT"
    E_EVOLVE_VERSION_CONFLICT = "E_EVOLVE_VERSION_CONFLICT"
    E_EVOLVE_GIT_FAILED = "E_EVOLVE_GIT_FAILED"

    # ---- E_EVAL_*（MOD-08，测量）-----------------------------------------------------
    E_EVAL_INV5_LEAK = "E_EVAL_INV5_LEAK" # Spec §8.3：泄漏统一码
    E_EVAL_SEED_OVERLAP = "E_EVAL_SEED_OVERLAP"
    E_EVAL_TEST_LOCKED = "E_EVAL_TEST_LOCKED"
    E_EVAL_SEED_EXHAUSTED = "E_EVAL_SEED_EXHAUSTED"
    E_EVAL_INSUFFICIENT_N = "E_EVAL_INSUFFICIENT_N"
    E_EVAL_BUDGET_EXCEEDED = "E_EVAL_BUDGET_EXCEEDED"
    # 单局超过 ``evals.seed_timeout_s``：按 dropped 计 n 并按 lose 计（Spec §8.5）
    E_EVAL_TIMEOUT = "E_EVAL_TIMEOUT"
    E_EVAL_NONDETERMINISTIC = "E_EVAL_NONDETERMINISTIC"
    E_EVAL_SCHEMA = "E_EVAL_SCHEMA"
    E_EVAL_BASELINE_MISSING = "E_EVAL_BASELINE_MISSING"
    E_EVAL_ARM_NOT_ALLOWED = "E_EVAL_ARM_NOT_ALLOWED"
    E_EVAL_CONFLICT_UNRESOLVED = "E_EVAL_CONFLICT_UNRESOLVED"

    # ---- E_IO_*（MOD-09，工程横切；本模块为默认出口）----------------------------------
    E_IO_CONFIG_INVALID = "E_IO_CONFIG_INVALID" # 退出码 2
    E_IO_CONFIG_UNKNOWN = "E_IO_CONFIG_UNKNOWN" # 未知配置键：忽略+告警，不中断
    E_IO_RUN_CONFLICT = "E_IO_RUN_CONFLICT"
    E_IO_STATE_MISSING = "E_IO_STATE_MISSING"
    E_IO_RUN_STATE = "E_IO_RUN_STATE"
    E_IO_TRAJECTORY_SCHEMA = "E_IO_TRAJECTORY_SCHEMA"
    E_IO_EVENT_UNKNOWN = "E_IO_EVENT_UNKNOWN"
    E_IO_TRAILING_TRUNCATED = "E_IO_TRAILING_TRUNCATED" # 末行截断：丢弃+告警，不中断
    E_IO_REDACTION_HIT = "E_IO_REDACTION_HIT"
    E_IO_WRITE = "E_IO_WRITE"
    E_IO_DISK_FULL = "E_IO_DISK_FULL" # 退出码 5
    E_IO_RUNS_NOT_IGNORED = "E_IO_RUNS_NOT_IGNORED"
    E_IO_GIT_DIRTY = "E_IO_GIT_DIRTY"
    E_IO_GIT_UNAVAILABLE = "E_IO_GIT_UNAVAILABLE" # 降级为 nogit，不中断
    E_IO_GIT_FAILED = "E_IO_GIT_FAILED" # git 可用但命令失败：环境错误，立即失败
    E_IO_GUARD_RANDOM = "E_IO_GUARD_RANDOM" # 退出码 4
    E_IO_GUARD_LAYER = "E_IO_GUARD_LAYER" # 退出码 4
    E_IO_PLOT_UNAVAILABLE = "E_IO_PLOT_UNAVAILABLE" # 降级为警告，不中断
    E_IO_MEMORY_CORRUPT = "E_IO_MEMORY_CORRUPT"
    E_IO_MEMORY_LOCKED = "E_IO_MEMORY_LOCKED"
    E_IO_MEMORY_QUOTA = "E_IO_MEMORY_QUOTA"
    E_IO_MEMORY_WRITE_FAILED = "E_IO_MEMORY_WRITE_FAILED" # Spec §8.3：记忆写入失败统一码
    E_IO_SNAPSHOT_MISSING = "E_IO_SNAPSHOT_MISSING"
    E_IO_MODULE_PENDING = "E_IO_MODULE_PENDING" # 命令面已冻结、所属模块尚未落地（M0 过渡期）
    E_IO_SCHEMA_MISSING = "E_IO_SCHEMA_MISSING"


# ---- 默认退出码映射 ---------------------------------------------------------------
EXIT_CODE: Final[Mapping[E, int]] = {
    # 可复现性缺失 → 立即失败（退出码 2 / 4 / 5）
    E.E_IO_CONFIG_INVALID: EXIT_CONFIG,
    E.E_IO_RUN_CONFLICT: EXIT_CONFIG,
    E.E_IO_STATE_MISSING: EXIT_CONFIG,
    E.E_IO_RUN_STATE: EXIT_IO,
    E.E_IO_RUNS_NOT_IGNORED: EXIT_CONFIG,
    E.E_IO_GIT_DIRTY: EXIT_SANDBOX,
    E.E_IO_GIT_FAILED: EXIT_CONFIG, # git 命令失败属环境错误，绝不降级为 0
    E.E_IO_GUARD_RANDOM: EXIT_SANDBOX,
    E.E_IO_GUARD_LAYER: EXIT_SANDBOX,
    E.E_IO_DISK_FULL: EXIT_IO,
    E.E_IO_WRITE: EXIT_IO,
    E.E_IO_TRAJECTORY_SCHEMA: EXIT_CONFIG,
    E.E_ENV_CONTENT_INVALID: EXIT_CONFIG,
    E.E_ENV_CONFIG_INVALID: EXIT_CONFIG,
    E.E_ENV_PROJECTION_VERSION: EXIT_CONFIG,
    E.E_ENV_IMPORT_DENIED: EXIT_SANDBOX,
    E.E_ENV_AST_DENIED: EXIT_SANDBOX,
    E.E_ENV_ESCAPE: EXIT_SANDBOX,
    E.E_ENV_HONEYPOT: EXIT_SANDBOX,
    E.E_EVAL_SEED_OVERLAP: EXIT_CONFIG,
    E.E_EVAL_TEST_LOCKED: EXIT_CONFIG,
    E.E_EVAL_INV5_LEAK: EXIT_SANDBOX,
    E.E_EVAL_ARM_NOT_ALLOWED: EXIT_CONFIG,
    E.E_EVAL_BASELINE_MISSING: EXIT_DOMAIN,
    E.E_EVAL_TIMEOUT: EXIT_DOMAIN,
    E.E_EVOLVE_VERSION_CONFLICT: EXIT_CONFIG,
    E.E_LLM_UNAVAILABLE: EXIT_MODEL,
    E.E_LLM_LOAD_FAILED: EXIT_MODEL,
    E.E_LLM_OOM: EXIT_MODEL,
    E.E_LLM_UNSUPPORTED: EXIT_MODEL,
    E.E_IO_MODULE_PENDING: EXIT_CONFIG,
    # 可观测性缺失 → 降级 + 告警，不得非零退出
    E.E_IO_CONFIG_UNKNOWN: EXIT_OK,
    E.E_IO_GIT_UNAVAILABLE: EXIT_OK,
    E.E_IO_PLOT_UNAVAILABLE: EXIT_OK,
    E.E_IO_TRAILING_TRUNCATED: EXIT_OK,
    E.E_IO_REDACTION_HIT: EXIT_OK, # 命中本身写 guard_finding 事件，不直接退出
    E.E_ENV_PARITY_MISMATCH: EXIT_DOMAIN,
}


ERROR_MESSAGE: Final[Mapping[E, str]] = {
    E.E_IO_CONFIG_INVALID: "配置校验失败：键路径或类型不合法",
    E.E_IO_CONFIG_UNKNOWN: "未知配置键（已忽略，不中断）",
    E.E_IO_RUN_CONFLICT: "run 目录已存在且 config_hash 不一致：禁止两套配置写同一 run",
    E.E_IO_STATE_MISSING: "--resume 找不到 state.json",
    E.E_IO_RUN_STATE: "state.json 原子写失败",
    E.E_IO_TRAJECTORY_SCHEMA: "轨迹行不符合 trajectory_line schema",
    E.E_IO_EVENT_UNKNOWN: "事件名未登记在 EVENT_REGISTRY",
    E.E_IO_TRAILING_TRUNCATED: "JSONL 末行截断（已丢弃，不中断）",
    E.E_IO_REDACTION_HIT: "落盘前 redaction 命中敏感值",
    E.E_IO_WRITE: "写入失败",
    E.E_IO_DISK_FULL: "磁盘空间不足",
    E.E_IO_RUNS_NOT_IGNORED: "runs/ 未被 .gitignore 覆盖",
    E.E_IO_GIT_DIRTY: "git worktree 不干净，演化前门禁失败",
    E.E_IO_GIT_UNAVAILABLE: "git 不可用（降级为 nogit 占位）",
    E.E_IO_GIT_FAILED: "git 命令执行失败（git 可用，属环境错误）",
    E.E_IO_GUARD_RANDOM: "确定性守卫检出未显式 seed 的随机源",
    E.E_IO_GUARD_LAYER: "分层 import 门禁检出越权 import（违反 INV-1）",
    E.E_IO_PLOT_UNAVAILABLE: "matplotlib 不可用，绘图已降级",
    E.E_IO_MODULE_PENDING: "命令面已冻结，但所属模块尚未落地",
    E.E_IO_SCHEMA_MISSING: "schema 文件缺失",
    # MOD-03 推理后端（Spec §8.3 以 MOD-03 为准）
    E.E_LLM_UNAVAILABLE: "模型不可用：未知 model_id / 权重缺失 / sha256 不匹配",
    E.E_LLM_LOAD_FAILED: "模型加载失败（可退避后换 fallback_backend 重试 1 次）",
    E.E_LLM_OOM: "内存不足：Metal/系统 OOM，或预算前置拒绝（未提交推理）",
    E.E_LLM_TIMEOUT: "推理超时（total_timeout_ms / generate_timeout_ms）",
    E.E_LLM_SCHEMA_INVALID: "三级修复耗尽仍不满足 JSON Schema",
    E.E_LLM_CONTEXT_OVERFLOW: "prompt_tokens + max_tokens 超出 context_length",
    E.E_LLM_BACKEND_ERROR: "后端错误：采样参数越界 / server 5xx / grammar 不可用且无 JSON mode",
    E.E_LLM_UNSUPPORTED: "可选辅助接口未实现（B-01）",
    E.E_LLM_EXTRACT_FAILED: "LLM 抽取失败（MOD-05 降级为规则抽取）",
    # MOD-08 评估（Spec §8.5 的降级契约）
    E.E_EVAL_TIMEOUT: "单局超过 evals.seed_timeout_s：该局记 dropped，计 n 并按 lose 计",
}


def error_code_of(code: E | str) -> int:
    """错误码 → 默认退出码；未登记的码归入退出码 1（领域失败）。"""
    try:
        return EXIT_CODE[E(str(code))]
    except (KeyError, ValueError):
        return EXIT_DOMAIN


class InfraError(RuntimeError):
    """携带错误码的异常：``code`` + 人类可读信息 + 复现命令（NFR-09-4 三要素）。"""

    def __init__(self, code: E | str, message: str, *, hint: str | None = None) -> None:
        self.code = E(str(code))
        self.message = message or ERROR_MESSAGE.get(self.code, str(code))
        self.hint = hint
        super().__init__(f"[{self.code}] {self.message}" + (f"\n  hint: {hint}" if hint else ""))

    @property
    def exit_code(self) -> int:
        return error_code_of(self.code)
