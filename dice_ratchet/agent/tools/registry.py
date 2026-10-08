"""MOD-04 harness 工具注册表与受限加载（PRD FR-04-11..13、NFR-04-6）。

链路：``gate()`` 静态门禁 → 逐文件筛选（违规文件弃用，其余保留）→ 签名校验
（唯一签名 ``rank_actions(obs, candidates, memory)``）→ MOD-02 常驻子进程 worker 执行。

任何一级失败都**不得**异常穿透到 MOD-06：

- 静态扫描命中 → ``E_ACTION_TOOL_GUARD``，弃用该文件；
- 签名不符 → ``E_ACTION_TOOL_SIG``，整体弃用工具目录（恒等排序）；
- 抛异常 / 超时 / 返回非 list / 返回元素与 ``candidates`` 不等价 → ``E_ACTION_TOOL_FAIL``，
  **返回原始候选序**（不做部分采纳）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError

__all__ = ["harness_tool", "ToolRegistry", "ToolSignature", "load_harness_tools",
           "tool_files", "tool_hash", "tool_signatures", "tool_summary",
           "RANK_ACTIONS", "RANK_ACTIONS_ARGS"]

RANK_ACTIONS: Final[str] = "rank_actions"
RANK_ACTIONS_ARGS: Final[tuple[str, ...]] = ("obs", "candidates", "memory")


def harness_tool(fn: Any) -> Any:
    """``@harness_tool`` 标记（Spec §4.4）。语义与 MOD-02 ``guard.harness_tool`` 一致。"""
    fn.__harness_tool__ = True
    return fn


# ----------------------------------------------------------------------
# 工具目录的静态视图（纯函数，供 prompt 与签名校验使用）
# ----------------------------------------------------------------------

def tool_files(harness_dir: Path | str) -> tuple[Path, ...]:
    """``harness/tools/*.py``（按文件名排序；不含 ``__init__.py``）。"""
    root = Path(harness_dir)
    tools_dir = root / "tools"
    if not tools_dir.is_dir():
        tools_dir = root
    if not tools_dir.is_dir():
        return ()
    return tuple(sorted(p for p in tools_dir.glob("*.py") if p.name != "__init__.py"))


def tool_hash(harness_dir: Path | str) -> str | None:
    """``sha256`` 摘要：按路径排序的 ``(相对路径, 文件 sha256)`` 列表（B-19 同口径）。"""
    files = tool_files(harness_dir)
    if not files:
        return None
    root = Path(harness_dir)
    digest = hashlib.sha256()
    for path in files:
        try:
            name = str(path.relative_to(root))
        except ValueError: # pragma: no cover - 防御
            name = path.name
        digest.update(name.encode("utf-8"))
        digest.update(b"\x1f")
        try:
            payload = path.read_bytes()
        except OSError: # pragma: no cover - 读取失败按空文件处理
            payload = b""
        digest.update(hashlib.sha256(payload).hexdigest().encode("ascii"))
        digest.update(b"\x1f")
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class ToolSignature:
    """一个 ``@harness_tool`` 函数的签名摘要。"""

    name: str
    args: tuple[str, ...]
    path: str
    marked: bool

    def as_text(self) -> str:
        return f"{self.name}({', '.join(self.args)})"


def _parse_source(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def tool_signatures(harness_dir: Path | str) -> tuple[ToolSignature, ...]:
    """全部 ``@harness_tool``（或名为 ``rank_actions``）函数的签名（按路径+行号确定序）。"""
    out: list[ToolSignature] = []
    for path in tool_files(harness_dir):
        tree = _parse_source(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            marked = any(_decorator_name(d) == "harness_tool" for d in node.decorator_list)
            if not marked and node.name != RANK_ACTIONS:
                continue
            args = tuple(a.arg for a in node.args.args)
            out.append(ToolSignature(node.name, args, str(path), marked))
    return tuple(out)


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def tool_summary(harness_dir: Path | str) -> str:
    """``{{tools}}`` 的填充值：工具签名摘要（INV-3 第 ③ 来源，无源码正文）。"""
    signatures = tool_signatures(harness_dir)
    if not signatures:
        return "(no harness tools)"
    return "\n".join(f"- {sig.as_text()} -> list[dict]" for sig in signatures)


# ----------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------

@dataclass
class ToolRegistry:
    """``rank_actions`` 的唯一调用入口（恒等排序是唯一的失败形态）。"""

    harness_dir: Path | None = None
    worker: Any | None = None
    enabled: bool = True
    timeout_ms: int = 50
    available: tuple[str, ...] = ()
    signature_ok: bool = True
    hash: str | None = None
    error_codes: list[str] = field(default_factory=list)
    owns_worker: bool = False
    staging_dir: str | None = None

    # ---- 查询 -------------------------------------------------------------
    @property
    def tool_hash(self) -> str | None:
        return self.hash

    @property
    def usable(self) -> bool:
        return (self.enabled and self.signature_ok and self.worker is not None
                and RANK_ACTIONS in self.available)

    def codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.error_codes))

    def _fail(self, code: str) -> None:
        self.error_codes.append(code)

    # ---- 调用 -------------------------------------------------------------
    def rank_actions(self, obs: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]],
                     memory: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """调用工具重排候选；任何问题都退化为**原始候选序**（FR-04-13）。"""
        original = [dict(c) for c in candidates]
        if not original or not self.usable:
            return original
        try:
            result = self.worker.call(  # type: ignore[union-attr]
                RANK_ACTIONS, obs=dict(obs), candidates=original,
                memory=[dict(m) for m in memory],
                timeout_s=max(0.001, float(self.timeout_ms) / 1000.0))
        except InfraError:
            self._fail(E.E_ACTION_TOOL_FAIL)
            return original
        except Exception: # noqa: BLE001 - 工具侧异常一律降级，不穿透到 MOD-06
            self._fail(E.E_ACTION_TOOL_FAIL)
            return original
        if not isinstance(result, list) or not _equivalent(result, original):
            self._fail(E.E_ACTION_TOOL_FAIL)
            return original
        return [dict(item) if isinstance(item, Mapping) else {"value": item} for item in result]

    def call(self, name: str, /, **kwargs: Any) -> Any:
        """通用调用入口（唯一签名：``(obs, candidates, memory)``）。"""
        if name == RANK_ACTIONS:
            return self.rank_actions(kwargs.get("obs") or {},
                                     kwargs.get("candidates") or [],
                                     kwargs.get("memory") or [])
        if not self.usable or name not in self.available:
            raise InfraError(E.E_ACTION_TOOL_SIG,
                             f"工具 {name!r} 不可用（可用：{sorted(self.available)}）",
                             hint="Spec §4.4：工具目录唯一签名为 rank_actions(obs, candidates, memory)")
        try:
            return self.worker.call(  # type: ignore[union-attr]
                name, obs=dict(kwargs.get("obs") or {}),
                candidates=[dict(c) for c in kwargs.get("candidates") or []],
                memory=[dict(m) for m in kwargs.get("memory") or []],
                timeout_s=max(0.001, float(self.timeout_ms) / 1000.0))
        except InfraError:
            self._fail(E.E_ACTION_TOOL_FAIL)
            raise

    def close(self) -> None:
        if self.owns_worker and self.worker is not None:
            try:
                self.worker.close()
            except Exception: # noqa: BLE001 - 关闭路径尽力而为
                pass
        self.worker = None
        if self.staging_dir:
            shutil.rmtree(self.staging_dir, ignore_errors=True)
            self.staging_dir = None

    def __enter__(self) -> "ToolRegistry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _equivalent(result: Sequence[Any], candidates: Sequence[Mapping[str, Any]]) -> bool:
    """多重集等价：工具只能重排，不能增删或改写候选（FR-04-13）。"""

    def key(item: Any) -> str:
        if not isinstance(item, Mapping):
            return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        return json.dumps({str(k): v for k, v in item.items()}, sort_keys=True,
                          ensure_ascii=False, default=str)

    return sorted(key(x) for x in result) == sorted(key(x) for x in candidates)


# ----------------------------------------------------------------------
# 受限加载（FR-04-11/12）
# ----------------------------------------------------------------------

def _group_findings(report: Any) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for finding in getattr(report, "findings", ()) or ():
        grouped.setdefault(str(getattr(finding, "file", "") or ""), []).append(finding)
    return grouped


def _stage_clean_files(files: Sequence[Path], workdir: Path) -> Path:
    """把通过门禁的文件复制进临时目录，供 worker 只加载干净子集（FR-04-12）。"""
    tools_dir = workdir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, tools_dir / path.name)
    return workdir


def load_harness_tools(harness_dir: Path | str, *, guard: Any | None = None,
                       enabled: bool = True, timeout_ms: int = 50,
                       worker: Any | None = None,
                       worker_factory: Callable[[Path], Any] | None = None,
                       max_tools: int = 32) -> ToolRegistry:
    """加载并门禁 ``harness/tools/*.py``，返回恒等安全的 ``ToolRegistry``。

    每一步都不抛异常给调用方（FR-04-12 的"某文件违规不拖垮其余工具"）：门禁结果、
    签名结论与降级码全部记在 ``registry.error_codes``。
    """
    root = Path(harness_dir)
    digest = tool_hash(root)
    if not enabled:
        return ToolRegistry(harness_dir=root, enabled=False, timeout_ms=timeout_ms,
                            hash=digest, signature_ok=True)
    files = tool_files(root)
    if not files:
        return ToolRegistry(harness_dir=root, enabled=True, timeout_ms=timeout_ms,
                            hash=digest, signature_ok=True, available=())

    if guard is None: # pragma: no cover - 生产路径由调用方注入真实 guard
        from dice_ratchet import sandbox

        guard = sandbox.guard

    error_codes: list[str] = []
    clean: list[Path] = []
    try:
        report = guard.gate(root, max_tools=max_tools)
        findings_by_file = _group_findings(report)
    except Exception: # noqa: BLE001 - 门禁本身失败 → fail-closed：整目录弃用
        return ToolRegistry(harness_dir=root, enabled=True, timeout_ms=timeout_ms,
                            hash=digest, signature_ok=False,
                            error_codes=[E.E_ACTION_TOOL_GUARD])
    for path in files:
        if findings_by_file.get(str(path)):
            error_codes.append(E.E_ACTION_TOOL_GUARD)
            continue
        clean.append(path)

    signatures = [sig for sig in tool_signatures(root) if sig.name == RANK_ACTIONS]
    for sig in signatures:
        if tuple(sig.args) != RANK_ACTIONS_ARGS:
            # 签名不符 → 整体弃用工具目录（FR-04-11）
            error_codes.append(E.E_ACTION_TOOL_SIG)
            return ToolRegistry(harness_dir=root, enabled=True, timeout_ms=timeout_ms,
                                hash=digest, signature_ok=False,
                                error_codes=error_codes)

    if not clean:
        return ToolRegistry(harness_dir=root, enabled=True, timeout_ms=timeout_ms,
                            hash=digest, signature_ok=True, available=(),
                            error_codes=error_codes)

    staging: str | None = None
    register_dir = root
    if len(clean) != len(files):
        staging = tempfile.mkdtemp(prefix="dice-ratchet-tools-")
        register_dir = _stage_clean_files(clean, Path(staging))

    owns_worker = False
    try:
        if worker is None:
            if worker_factory is not None:
                worker = worker_factory(register_dir)
            else:
                worker = guard.load_harness_tools(register_dir)
            owns_worker = True
        available = tuple(worker.tools())
    except Exception: # noqa: BLE001 - worker 起不来 → 恒等排序，不判局失败
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        return ToolRegistry(harness_dir=root, enabled=True, timeout_ms=timeout_ms,
                            hash=digest, signature_ok=True, available=(),
                            error_codes=[*error_codes, E.E_ACTION_TOOL_FAIL])

    if signatures and RANK_ACTIONS not in available:
        error_codes.append(E.E_ACTION_TOOL_SIG)
        if owns_worker:
            try:
                worker.close()
            except Exception: # noqa: BLE001
                pass
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        return ToolRegistry(harness_dir=root, enabled=True, timeout_ms=timeout_ms,
                            hash=digest, signature_ok=False,
                            error_codes=error_codes)

    return ToolRegistry(harness_dir=root, worker=worker, enabled=True,
                        timeout_ms=timeout_ms, available=available, signature_ok=True,
                        hash=digest, error_codes=error_codes, owns_worker=owns_worker,
                        staging_dir=staging)
