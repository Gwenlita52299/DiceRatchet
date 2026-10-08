"""MOD-07 演化异常（错误码一律取自 ``infra.errors.E``，本模块不自定义字符串码）。

PRD §10 的错误处置（降级总则：**任何不确定 → 拒绝候选，保留 parent**）：

- ``ProposeFailedError`` → ``E_EVOLVE_PROPOSE_FAILED``（生成失败/重试耗尽/JSON 不合 schema）；
- ``NoCandidateError`` → ``E_EVOLVE_NO_CANDIDATE``（生成成功但 0 候选，含全被去重）；
- ``HoldoutLeakError`` → ``E_EVAL_INV5_LEAK``（**致命**：证据束含非 train split 或
  heldout seed 字面量；Spec §8.3 的泄漏统一码，v0.1 的 ``E_EVOLVE_HOLDOUT_LEAK`` 并入）；
- ``PatchInvalidError`` → ``E_EVOLVE_PATCH_INVALID``（行数/文件/签名/依赖/白名单）；
- ``ScanFailedError`` → ``E_EVOLVE_SCAN_FAILED``（MOD-02 ``guard.gate`` 返回 fail）；
- ``LoadFailedError`` → ``E_EVOLVE_LOAD_FAILED``（受限加载失败，已 accept 则立即回滚）；
- ``SmokeFailedError`` → ``E_EVOLVE_SMOKE_FAILED``（冒烟快速淘汰命中）；
- ``EvalFailedError`` → ``E_EVOLVE_EVAL_FAILED``（MOD-08 超时/异常）；
- ``JudgeFailedError`` → ``E_EVOLVE_JUDGE_FAILED``（判定链路异常/报告缺字段）；
- ``GateTimeoutError`` → ``E_EVOLVE_GATE_TIMEOUT``（HITL 超时 → 保守回滚）；
- ``VersionConflictError`` → ``E_EVOLVE_VERSION_CONFLICT``（版本号已存在/指针不一致）；
- ``GitFailedError`` → ``E_EVOLVE_GIT_FAILED``（commit/tag 失败，丢弃未提交 accept）。

异常构造统一为 ``EvolveXxxError(message, hint=...)``：错误码由子类的 ``code_default``
决定，调用点不需要（也不允许）手写字符串码。
"""

from __future__ import annotations

from dice_ratchet.infra.errors import E, InfraError

__all__ = [
    "EvolveError", "ProposeFailedError", "NoCandidateError", "HoldoutLeakError",
    "PatchInvalidError", "ScanFailedError", "LoadFailedError", "SmokeFailedError",
    "EvalFailedError", "JudgeFailedError", "GateTimeoutError", "VersionConflictError",
    "GitFailedError", "EVOLVE_ERROR_CODES",
]

#: 本模块会用到的全部错误码（测试与 CLI 复用；不得在别处硬编码字符串）。
EVOLVE_ERROR_CODES: frozenset[E] = frozenset({
    E.E_EVOLVE_PROPOSE_FAILED, E.E_EVOLVE_NO_CANDIDATE, E.E_EVOLVE_PATCH_INVALID,
    E.E_EVOLVE_SCAN_FAILED, E.E_EVOLVE_LOAD_FAILED, E.E_EVOLVE_SMOKE_FAILED,
    E.E_EVOLVE_EVAL_FAILED, E.E_EVOLVE_NO_GAIN, E.E_EVOLVE_ILLEGAL_RATE,
    E.E_EVOLVE_OVERFIT_SUSPECT, E.E_EVOLVE_COLLAPSE, E.E_EVOLVE_JUDGE_FAILED,
    E.E_EVOLVE_GATE_TIMEOUT, E.E_EVOLVE_VERSION_CONFLICT, E.E_EVOLVE_GIT_FAILED,
    E.E_EVAL_INV5_LEAK,
})


class EvolveError(InfraError):
    """MOD-07 异常基类：``code`` 由 ``code_default`` 提供（退出码见 ``EXIT_CODE``）。"""

    code_default: E = E.E_EVOLVE_PROPOSE_FAILED

    def __init__(self, message: str, *, code: E | str | None = None,
                 hint: str | None = None) -> None:
        super().__init__(code or self.code_default, message, hint=hint)


class ProposeFailedError(EvolveError):
    """候选生成失败：LLM 不可用 / schema 重试耗尽 / 输出无法解析。"""

    code_default = E.E_EVOLVE_PROPOSE_FAILED


class NoCandidateError(EvolveError):
    """生成成功但 0 候选（含全部被去重或优先级截断）。"""

    code_default = E.E_EVOLVE_NO_CANDIDATE


class HoldoutLeakError(EvolveError):
    """证据束含非 train split 或 heldout seed 字面量（INV-5，P0，致命）。"""

    code_default = E.E_EVAL_INV5_LEAK


class PatchInvalidError(EvolveError):
    """``validate_candidate`` 失败（行数/字符数/文件白名单/签名/依赖/配置键）。"""

    code_default = E.E_EVOLVE_PATCH_INVALID


class ScanFailedError(EvolveError):
    """MOD-02 ``guard.gate`` 静态扫描判 fail（INV-4）。"""

    code_default = E.E_EVOLVE_SCAN_FAILED


class LoadFailedError(EvolveError):
    """MOD-02 ``guard.load_harness_tools`` 受限加载失败（worker 启动/加载）。"""

    code_default = E.E_EVOLVE_LOAD_FAILED


class SmokeFailedError(EvolveError):
    """冒烟快速淘汰命中 / 冒烟异常。"""

    code_default = E.E_EVOLVE_SMOKE_FAILED


class EvalFailedError(EvolveError):
    """MOD-08 ``evaluate_arm`` 超时/异常（保守 reject，保留当前 harness）。"""

    code_default = E.E_EVOLVE_EVAL_FAILED


class JudgeFailedError(EvolveError):
    """判定链路异常 / 报告缺字段（Spec §8.5 保守拒绝）。"""

    code_default = E.E_EVOLVE_JUDGE_FAILED


class GateTimeoutError(EvolveError):
    """HITL 超时：保守回滚 ``parent_version``，``reason="hitl_timeout"``。"""

    code_default = E.E_EVOLVE_GATE_TIMEOUT


class VersionConflictError(EvolveError):
    """版本号已存在 / 指针不一致 / git 冲突（不自动 merge，人工介入）。"""

    code_default = E.E_EVOLVE_VERSION_CONFLICT


class GitFailedError(EvolveError):
    """git commit/tag 失败：丢弃未提交 accept，``version.json`` 不变。"""

    code_default = E.E_EVOLVE_GIT_FAILED
