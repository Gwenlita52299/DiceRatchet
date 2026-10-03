"""MOD-03 推理错误码与异常（PRD §10 / Spec §8.3）。

本模块**只引用** ``gamer_rsi.infra.errors.E`` 里已登记的 ``E_LLM_*`` 码，不自定义字符串
错误码（Spec §8.3）。``InferenceError`` 携带四要素：``code`` / ``attempts`` /
``retryable`` / ``detail``，供上层（MOD-04/06）决定 fallback 与局级降级。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from ..infra.errors import E, ERROR_MESSAGE, error_code_of

__all__ = ["InferenceError", "RETRYABLE", "INFERENCE_MESSAGES", "is_retryable",
           "exit_code_of", "LLM_CODES"]

#: 各错误码的默认可重试性（PRD §10 表）。
#:
#: ``E_LLM_BACKEND_ERROR`` 一码两用：参数越界（不可重试）与后端 5xx（可重试 1 次）。
#: 默认按参数类记 ``False``，后端类由 ``InferenceError(..., retryable=True)`` 显式覆盖。
RETRYABLE: Final[Mapping[str, bool]] = {
    E.E_LLM_UNAVAILABLE: False,
    E.E_LLM_LOAD_FAILED: True,
    E.E_LLM_OOM: True,
    E.E_LLM_TIMEOUT: True,
    E.E_LLM_SCHEMA_INVALID: False,
    E.E_LLM_CONTEXT_OVERFLOW: False,
    E.E_LLM_BACKEND_ERROR: False,
    E.E_LLM_UNSUPPORTED: False,
    E.E_LLM_EXTRACT_FAILED: False,
}

#: MOD-03 的全部错误码（供测试与文档核对）。
LLM_CODES: Final[tuple[str, ...]] = tuple(RETRYABLE)

#: 人类可读信息：优先 infra 注册表，缺失时回退到码本身。
INFERENCE_MESSAGES: Final[Mapping[str, str]] = {
    code: ERROR_MESSAGE.get(E(code), code) for code in LLM_CODES
}


def is_retryable(code: E | str, *, default: bool = False) -> bool:
    """错误码 → 默认可重试性；未登记的码返回 ``default``。"""
    return RETRYABLE.get(str(code), default)


def exit_code_of(code: E | str) -> int:
    """错误码 → CLI 退出码（登记表见 ``infra.errors.EXIT_CODE``）。"""
    return error_code_of(code)


class InferenceError(RuntimeError):
    """MOD-03 的唯一失败出口（PRD §10）。

    本模块**只抛码，绝不静默兜底**——不返回空动作、不猜测 JSON。``detail`` 里可以带
    ``raw_texts`` / ``retry_reasons`` / ``reason`` 等可复现证据，但绝不带模型私藏信息。
    """

    def __init__(self, code: E | str, message: str = "", *, attempts: int = 0,
                 retryable: bool | None = None, detail: Mapping[str, Any] | None = None,
                 hint: str | None = None) -> None:
        self.code = E(str(code))
        self.message = message or INFERENCE_MESSAGES.get(str(self.code), str(self.code))
        self.attempts = int(attempts)
        self.retryable = is_retryable(self.code) if retryable is None else bool(retryable)
        self.detail: dict[str, Any] = dict(detail or {})
        self.hint = hint
        super().__init__(f"[{self.code}] {self.message}")

    @property
    def exit_code(self) -> int:
        return exit_code_of(self.code)

    def as_dict(self) -> dict[str, Any]:
        """结构化形态（埋点 / 上层降级记录用）。"""
        return {"code": str(self.code), "message": self.message, "attempts": self.attempts,
                "retryable": self.retryable, "detail": dict(self.detail)}

    def with_detail(self, **extra: Any) -> "InferenceError":
        """返回携带附加证据的同类异常（不修改原异常）。"""
        detail = dict(self.detail)
        detail.update(extra)
        return InferenceError(self.code, self.message, attempts=self.attempts,
                              retryable=self.retryable, detail=detail, hint=self.hint)
