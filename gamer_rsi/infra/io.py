"""IO 原语：原子写与 schema 注册表（FR-09-18）。

- ``write_json_atomic()``：``tmp`` + ``os.replace``，``report.json`` / ``state.json`` /
  ``version.json`` 的唯一写入口（Spec §5）。
- ``validate(name, obj)``：跨模块 JSON schema 统一入口（Spec §4.5、§9 G5）。
- ``read_json()`` / ``read_jsonl()``：读取端容忍末行截断（FR-09-6、NFR-09-1）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Final, Iterator, Mapping

from .errors import E, InfraError

__all__ = [
    "write_json_atomic", "read_json", "read_jsonl", "write_bytes_atomic",
    "sha256_file", "sha256_bytes", "schema_dir", "validate", "schema_error_code",
    "register_schema_dir", "TruncatedLine",
]

#: schema 名（去 ``.schema.json`` 后缀）→ 校验失败时的错误码（FR-09-18 / Spec §4.5）。
#:
#: 归属原则：**schema 由哪个模块拥有，就用哪个模块的前缀**——轨迹行归 MOD-09（IO）、
#: 观测与沙箱侧审计/门禁记录归 MOD-02（ENV）、测量报告归 MOD-08（EVAL）。旧实现用
#: ``name.startswith("trajectory")`` 的二分法，把所有非轨迹 schema 都报成 MOD-08 的
#: ``E_EVAL_SCHEMA``（2026-10-03 审计 P2-4）。
#:
#: 新增 ``schemas/*.schema.json`` 必须在此登记：
#: ``tests/unit/infra/test_logging.py::test_every_schema_has_a_registered_error_code``
#: 断言该表与实际 schema 文件**一一对应**，漏登记会红。
_SCHEMA_ERROR_CODES: Final[Mapping[str, E]] = {
    "trajectory_line": E.E_IO_TRAJECTORY_SCHEMA, # MOD-09：轨迹行
    "observation": E.E_ENV_SCHEMA, # MOD-02：agent 侧观测
    "audit_report": E.E_ENV_SCHEMA, # MOD-02：泄漏审计报告
    "harness_gate": E.E_ENV_SCHEMA, # MOD-02：harness 工具门禁记录
    "decision": E.E_LLM_SCHEMA_INVALID, # MOD-03：唯一决策 schema（Spec §4.3）
    "models": E.E_LLM_UNAVAILABLE, # MOD-03：模型注册表
    "inference_call": E.E_LLM_BACKEND_ERROR, # MOD-03：推理埋点记录
    "generation_report": E.E_EVAL_SCHEMA, # MOD-08：代际报告（Spec §4.5 B-16）
}

#: 未登记 schema 的兜底码（MOD-08 测量报告等）；登记表是一一对应的，正常不该走到这里。
_SCHEMA_ERROR_FALLBACK: Final[E] = E.E_EVAL_SCHEMA


def schema_key(name: str) -> str:
    """``"audit_report"`` / ``"audit_report.schema.json"`` / ``"schemas/x.schema.json"`` → 登记键。"""
    base = Path(name).name
    return base[: -len(".schema.json")] if base.endswith(".schema.json") else base


def schema_error_code(name: str) -> E:
    """schema 名 → 校验失败时的错误码（登记表见 ``_SCHEMA_ERROR_CODES``）。"""
    return _SCHEMA_ERROR_CODES.get(schema_key(name), _SCHEMA_ERROR_FALLBACK)

# schema 根目录默认为仓库根的 schemas/（Spec §5）
_SCHEMA_ROOTS: list[Path] = [Path("schemas")]
_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

_DEFAULT_SCHEMA_ROOT = Path(__file__).resolve().parent.parent.parent / "schemas"
if _DEFAULT_SCHEMA_ROOT.is_dir():
    _SCHEMA_ROOTS.append(_DEFAULT_SCHEMA_ROOT)

# import jsonschema 延迟到首次校验（绘图等轻量路径不触发）
_validator_mod: Any = None


def schema_dir(path: Path | str) -> None:
    """注册额外的 schema 查根目录（测试与插件用）。"""
    root = Path(path)
    if root not in _SCHEMA_ROOTS:
        _SCHEMA_ROOTS.insert(0, root)
    with _LOCK:
        _SCHEMA_CACHE.clear()


def register_schema_dir(path: Path | str) -> None: # pragma: no cover - 别名，语义同 schema_dir
    schema_dir(path)


def _load_schema(name: str) -> dict[str, Any]:
    fname = name if name.endswith(".schema.json") else f"{name}.schema.json"
    with _LOCK:
        if fname in _SCHEMA_CACHE:
            return _SCHEMA_CACHE[fname]
    for root in _SCHEMA_ROOTS:
        candidate = Path(root) / fname
        if candidate.is_file():
            try:
                schema = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise InfraError(E.E_IO_SCHEMA_MISSING,
                                 f"schema 文件损坏：{candidate}\n{exc}") from exc
            with _LOCK:
                _SCHEMA_CACHE[fname] = schema
            return schema
    raise InfraError(E.E_IO_SCHEMA_MISSING,
                     f"找不到 schema {fname}（搜索根：{[str(r) for r in _SCHEMA_ROOTS]}）")


def _get_jsonschema() -> Any:
    global _validator_mod
    if _validator_mod is None:
        import jsonschema # 延迟导入：JSON 校验是硬依赖，但绘图路径不需要
        _validator_mod = jsonschema
    return _validator_mod


def validate(name: str, obj: Any) -> None:
    """按 ``schemas/<name>.schema.json`` 校验 ``obj``；失败抛携带 json_path 的错误。

    ``name`` 可带 ``.schema.json`` 后缀（也可带目录，取 basename）。失败时的错误码由
    ``_SCHEMA_ERROR_CODES`` 按 **schema 归属模块** 选定，不再用名字前缀硬编码；
    登记原则与兜底见该表。
    """
    schema = _load_schema(name)
    try:
        _get_jsonschema().validate(instance=obj, schema=schema)
    except Exception as exc: # jsonschema.ValidationError / SchemaError
        path = getattr(exc, "json_path", "$")
        message = getattr(exc, "message", str(exc))
        raise InfraError(schema_error_code(name),
                         f"schema 校验失败 [{name}] at {path}: {message}") from exc


# ---- 原子写 -------------------------------------------------------------------------

_TMP_SUFFIX: Final[str] = ".tmp"


def write_bytes_atomic(path: Path, data: bytes, *, fsync: bool = False) -> None:
    """``tmp`` + ``os.replace`` 原子写字节（FR-09-18）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        _raise_io_error(exc, path)


def write_json_atomic(path: Path, obj: Any, *, fsync: bool = False,
                      indent: int = 2, sort_keys: bool = True) -> str:
    """JSON 原子写；返回 ``"sha256:<hex>"``（report/version.json 需要）。

    浮点精度对齐 Spec §4.5 B-34：``round(x, 4)``。
    """
    payload = json.dumps(_round_floats(obj), ensure_ascii=False, indent=indent,
                         sort_keys=sort_keys, default=_json_default)
    write_bytes_atomic(Path(path), payload.encode("utf-8"), fsync=fsync)
    return "sha256:" + sha256_bytes(payload.encode("utf-8"))


def _round_floats(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def _json_default(obj: Any) -> Any:
    from pathlib import PurePath
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"不可 JSON 序列化：{type(obj).__name__}")


def _raise_io_error(exc: OSError, path: Path) -> None:
    if exc.errno in (28, 39): # ENOSPC / ENOSPC(mac)
        raise InfraError(E.E_IO_DISK_FULL, f"磁盘空间不足，写入失败：{path}") from exc
    raise InfraError(E.E_IO_WRITE, f"写入失败：{path}\n{exc}") from exc


# ---- 读取 -------------------------------------------------------------------------

class TruncatedLine:
    """读取端检测到的末行截断（FR-09-6 / NFR-09-1）。"""

    def __init__(self, raw: str, reason: str) -> None:
        self.raw = raw
        self.reason = reason

    def __repr__(self) -> str: # pragma: no cover
        return f"TruncatedLine(reason={self.reason!r}, raw={self.raw[:80]!r})"


def read_json(path: Path) -> Any:
    """读取原子写的 JSON；文件缺失抛 ``E_IO_STATE_MISSING``（state.json 语义）。"""
    path = Path(path)
    if not path.is_file():
        raise InfraError(E.E_IO_STATE_MISSING, f"文件不存在：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InfraError(E.E_IO_STATE_MISSING, f"JSON 解析失败：{path}\n{exc}") from exc
    except OSError as exc:
        raise InfraError(E.E_IO_WRITE, f"读取失败：{path}\n{exc}") from exc


def read_jsonl(path: Path, *, drop_truncated: bool = True) -> tuple[list[dict[str, Any]], list[TruncatedLine]]:
    """读取 JSONL，容忍末行截断（NFR-09-1）。

    返回 ``(行列表, 截断行列表)``。非末行的损坏行直接抛 ``E_IO_WRITE``——
    只有末行允许截断（「一行要么完整要么不存在」，FR-09-6）。
    """
    path = Path(path)
    if not path.is_file():
        return [], []
    rows: list[dict[str, Any]] = []
    truncated: list[TruncatedLine] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if idx == len(lines) - 1 and drop_truncated:
                truncated.append(TruncatedLine(line, str(exc)))
                continue
            raise InfraError(E.E_IO_WRITE,
                             f"JSONL 第 {idx + 1} 行损坏（非末行，拒绝继续）：{path}\n{exc}") from exc
    return rows, truncated


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """流式读取 JSONL；末行截断时静默停止（调用方可读 ``metrics`` 用）。"""
    rows, _ = read_jsonl(path)
    yield from rows


# ---- 哈希 -------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(Path(path), "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
