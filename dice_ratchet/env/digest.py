"""``kernel/digest.js`` 的逐位移植 —— 规范化 JSON（递归排序键）+ SHA-256。

等价性要点（对拍关键）：

- ``stable_stringify`` 递归排序键、过滤 ``undefined``（Python 侧不会出现 undefined，
  故过滤恒为空）、数组保序、布尔/整数/null 直出。
- **字符串转义**：JS ``JSON.stringify`` **不转义**非 ASCII 字符（中文原样输出），
  因此 Python 侧必须用 ``ensure_ascii=False``。PRD FR-01-3 写的 ``ensure_ascii=True``
  会让含中文的摘要（如敌人名「腐生史莱姆」、obs 的 chapterName）与 JS 不一致——
  以 golden 对拍（G2/T-04）为硬约束，本模块采用 ``ensure_ascii=False``。
- ``sha256`` 摘要在 **UTF-8 字节**上计算（JS 侧的 ``utf8()`` 编码器同理）。
- 内核无浮点（K-3）：遇到 ``float`` 直接抛 ``TypeError``（T-02），不做 IEEE754 兜底。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

__all__ = ["stable_stringify", "sha256_hex", "digest", "config_hash", "content_hash"]

DIGEST_PREFIX: Final[str] = "sha256:"


def _quote(text: str) -> str:
    """与 JS ``JSON.stringify(text)`` 逐字节一致（含 ``\\b \\f \\n \\r \\t \\" \\\\\" 与控制字符）。"""
    return json.dumps(text, ensure_ascii=False)


def stable_stringify(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError(f"float is forbidden in the kernel (K-3): {value!r}")
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(stable_stringify(v) for v in value) + "]"
    if isinstance(value, Mapping):
        keys = sorted(k for k in value if value[k] is not _UNDEFINED)
        return "{" + ",".join(f"{_quote(str(k))}:{stable_stringify(value[k])}" for k in keys) + "}"
    raise TypeError(f"unstable type in stable_stringify: {type(value).__name__}")


class _Undefined:
    """哨兵：模拟 JS 的 ``undefined``（出现在显式置空的键上时被过滤）。"""

    __slots__ = ()


_UNDEFINED = _Undefined()


def sha256_hex(text: str) -> str:
    """SHA-256 over UTF-8 bytes → 64 位小写 hex（JS 侧为纯 JS 实现，结果标准一致）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return DIGEST_PREFIX + sha256_hex(stable_stringify(value))


def config_hash(config: Any) -> str:
    """``"sha256:<16hex>"``。"""
    return DIGEST_PREFIX + sha256_hex(stable_stringify(config))[:16]


def content_hash(tables: Any) -> str:
    """配表摘要，记入 ``config.json``（MOD-09）。"""
    return digest(tables)
