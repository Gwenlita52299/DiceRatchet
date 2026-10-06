"""MOD-05 记忆库异常（错误码一律取自 ``infra.errors.E``，本模块不自定义字符串码）。

PRD §10 的错误处置：

- ``MemoryIsolationError`` → ``E_EVAL_INV5_LEAK``（P0，拒绝写入/拒绝返回）；
- ``MemoryCorruptError`` → ``E_IO_MEMORY_CORRUPT``（坏行入 quarantine，其余照常加载）；
- ``MemoryLockedError`` → ``E_IO_MEMORY_LOCKED``（压实期 ``memory.lock`` 被占用 > 5 s，跳过本次压实）；
- ``MemoryQuotaError`` → ``E_IO_MEMORY_QUOTA``（单文件 > 16 MB 或 ``active > 2*max_entries``）；
- ``SnapshotMissingError`` → ``E_IO_SNAPSHOT_MISSING``（回滚目标缺失，CLI 退出码 2）；
- ``MemoryWriteFailedError`` → ``E_IO_MEMORY_WRITE_FAILED``（磁盘/权限；本局继续）。

异常构造统一为 ``MemoryXxxError(message, hint=...)``：错误码由子类的 ``code_default``
决定，调用点不需要（也不允许）手写字符串码。
"""

from __future__ import annotations

from gamer_rsi.infra.errors import E, InfraError

__all__ = [
    "MemoryError", "MemoryIsolationError", "MemoryCorruptError", "MemoryLockedError",
    "MemoryQuotaError", "SnapshotMissingError", "MemoryWriteFailedError",
    "MEMORY_ERROR_CODES",
]

#: 本模块会用到的全部错误码（测试与 CLI 复用；不得在别处硬编码字符串）。
MEMORY_ERROR_CODES: frozenset[E] = frozenset({
    E.E_EVAL_INV5_LEAK, E.E_IO_MEMORY_CORRUPT, E.E_IO_MEMORY_LOCKED,
    E.E_IO_MEMORY_QUOTA, E.E_IO_SNAPSHOT_MISSING, E.E_IO_MEMORY_WRITE_FAILED,
    E.E_IO_CONFIG_INVALID, E.E_EVAL_CONFLICT_UNRESOLVED,
})


class MemoryError(InfraError):
    """MOD-05 异常基类：``code`` 由 ``code_default`` 提供（退出码见 ``EXIT_CODE``）。"""

    code_default: E = E.E_IO_MEMORY_CORRUPT

    def __init__(self, message: str, *, code: E | str | None = None,
                 hint: str | None = None) -> None:
        super().__init__(code or self.code_default, message, hint=hint)


class MemoryIsolationError(MemoryError):
    """I1–I4 任一拦截点命中（INV-5），或非 train 局尝试写入。"""

    code_default = E.E_EVAL_INV5_LEAK


class MemoryCorruptError(MemoryError):
    """行解析失败 / ``content_sha256`` 不符 / ``entry_id`` 重复 / schema 不符。"""

    code_default = E.E_IO_MEMORY_CORRUPT


class MemoryLockedError(MemoryError):
    """``memory.lock`` 被其它进程占用超过 5 s：跳过本次压实（写入仍走追加）。"""

    code_default = E.E_IO_MEMORY_LOCKED


class MemoryQuotaError(MemoryError):
    """单文件 > 16 MB 或 ``active > 2 * max_entries``：强制淘汰+压实，仍超则停写。"""

    code_default = E.E_IO_MEMORY_QUOTA


class SnapshotMissingError(MemoryError):
    """``rollback`` 目标快照 ``memory_v{N}.jsonl`` 不存在（CLI 退出码 2）。"""

    code_default = E.E_IO_SNAPSHOT_MISSING


class MemoryWriteFailedError(MemoryError):
    """追加/压实落盘失败（磁盘错误、权限）；记忆是增强项，本局不因此失败。"""

    code_default = E.E_IO_MEMORY_WRITE_FAILED
