"""权重落盘校验（NFR-03-06：加载前校验 ``sha256``，不匹配抛 ``E_LLM_UNAVAILABLE``）。

目录形态（HF/MLX）按「相对路径 + 文件摘要」排序后整体哈希，保证同一目录在任何机器上
得到同一 digest；文件形态（GGUF）直接哈希文件。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

from ..infra.errors import E
from ..infra.io import sha256_file
from .errors import InferenceError
from .registry import ModelSpec

__all__ = ["weights_digest", "verify_weights", "skip_hash_check_enabled", "SKIP_HASH_ENV"]

#: 逃生开关（Q-6 / PRD §9）：CI 必须为 false（配置层的 ``skip_hash_check`` 由
#: ``infra.config.InferenceConfig.validate`` 挡在 true 之外，本变量是唯一的本地通道）。
SKIP_HASH_ENV: Final[str] = "DICERATCHET_INFERENCE_SKIP_HASH"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def skip_hash_check_enabled(config: object | None = None) -> bool:
    """``skip_hash_check`` 是否生效：配置项 **或** 环境逃生开关（``=1``）。"""
    if bool(getattr(config, "skip_hash_check", False)):
        return True
    return str(os.environ.get(SKIP_HASH_ENV, "")).strip().lower() in _TRUTHY


def weights_digest(path: Path | str) -> str:
    """权重文件/目录的 sha256 十六进制串。"""
    target = Path(path)
    if target.is_file():
        return sha256_file(target)
    items: list[tuple[str, str]] = []
    for child in sorted(target.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(target)
        # 跳过**任意层级**的隐藏项（`.cache/`、`CACHEDIR.TAG`、`.gitignore` 等）。
        # 2026-10-04（M1 验收 F17）：只判 `child.name` 会把 HuggingFace 下载缓存
        # （`models/…/.cache/huggingface/download/*.metadata`、`.lock`）算进摘要，
        # 于是「同一份权重、不同下载时间/重试」会得到不同 sha256，Q-6 的锁定形同虚设。
        if any(part.startswith(".") for part in rel.parts):
            continue
        items.append((rel.as_posix(), sha256_file(child)))
    payload = "\n".join(f"{name}\x1f{digest}" for name, digest in items).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_weights(spec: ModelSpec, *, skip_hash_check: bool = False,
                   require_weights: bool = True) -> str | None:
    """校验权重存在与 ``sha256``；返回实测 digest（跳过校验时返回 ``None``）。"""
    if not spec.has_weights:
        if require_weights:
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 f"模型 {spec.model_id!r} 缺少 weights_path",
                                 hint="在 configs/models.yaml 登记 weights_path（PRD §7）")
        return None
    path = Path(spec.weights_path)
    if not path.exists():
        raise InferenceError(E.E_LLM_UNAVAILABLE, f"权重缺失：{path}",
                             hint="离线下载权重；CI 用 mock 后端（NFR-03-06）")
    if not spec.weights_sha256 or skip_hash_check:
        return None # 注册表未登记 sha256（Q-6）或显式逃生开关
    actual = weights_digest(path)
    if actual != spec.weights_sha256:
        raise InferenceError(
            E.E_LLM_UNAVAILABLE,
            f"权重 sha256 不匹配：{spec.model_id}\n  期望 {spec.weights_sha256}\n  实际 {actual}",
            hint="重新下载权重或更新注册表（Q-6）")
    return actual
