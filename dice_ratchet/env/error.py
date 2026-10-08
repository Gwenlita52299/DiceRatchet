"""内核异常（``kernel/state.js`` 的 ``kernelError`` 的共享实现）。

JS 侧 ``err.code`` 挂在 ``Error`` 上；Python 侧统一为 ``KernelError``。
之所以单列一个模块而非放在 ``state.py``，是因为 ``rng.py`` 也要抛内核错误，
而 ``state.py`` 依赖 ``rng.py``（GAME-01 §10 的依赖方向不可反转）。

错误码映射（MOD-01 FR-01-10）在 ``engine.py`` 的 ``map_error_code`` 中登记。
"""

from __future__ import annotations

__all__ = ["KernelError"]


class KernelError(RuntimeError):
    """携带内核错误码的异常（对应 JS 的 ``kernelError(code, message)``）。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code)
        super().__init__(message or code)
