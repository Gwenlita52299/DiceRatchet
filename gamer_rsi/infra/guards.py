"""确定性守卫与分层 import 门禁（FR-09-15 / INV-1 / INV-6）。

用 **AST**（非正则）扫描，把不变量变成可执行门禁：

- ``scan_determinism()`` → ``E_IO_GUARD_RANDOM``：
  ``import random``、``numpy.random.*`` 未显式 seed、``time.time()`` 参与采样、
  ``uuid4()`` 用于标识 id。
- ``scan_layer_imports()`` → ``E_IO_GUARD_LAYER``：
  ``gamer_rsi/{agent,memory,loop,evolve,evals,inference}`` 中出现
  ``import gamer_rsi.env.*``（违反 INV-1）；以及 ``gamer_rsi/infra`` import 其他子包
  （违反 NFR-09-5 单向依赖）。

白名单注释 ``# guard: allow <CODE> reason=...`` 可豁免（同行），但豁免也会记
``guard_finding`` 事件留痕。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator, Sequence

from .errors import E
from . import events

__all__ = ["GuardFinding", "scan_determinism", "scan_layer_imports", "scan_all",
           "ALLOW_PREFIX", "LAYER_ROOTS"]

# agent 侧模块不得直接 import 环境层（INV-1：只能经 SandboxEnv 触达环境）
LAYER_ROOTS: Final[tuple[str, ...]] = (
    "agent", "memory", "loop", "evolve", "evals", "inference",
)
# 环境层包：agent 侧禁止直接依赖
ENV_PACKAGE: Final[str] = "gamer_rsi.env"
# infra 不得 import 任何其他 gamer_rsi 子包（NFR-09-5）
INFRA_PACKAGE: Final[str] = "gamer_rsi.infra"
ALLOW_PREFIX: Final[str] = "# guard: allow"

_EXEMPT_RE: Final[re.Pattern[str]] = re.compile(
    r"#\s*guard:\s*allow\s+(?P<codes>[^#]*?)\s*(?:reason=(?P<reason>\S+))?\s*(?:#.*)?$"
)

# 确定性禁止的属性调用：模块名前缀 -> 具体属性（None 表示该模块的任意属性）
_BANNED_CALLS: Final[tuple[tuple[str, frozenset[str] | None], ...]] = (
    ("random", None), # 任意 random.* 调用
    ("numpy.random", None),
    # 仅墙钟 time.time() 参与采样才违例；perf_counter/monotonic 用于计时，不参与采样
    ("time", frozenset({"time", "time_ns"})),
)
_BANNED_UUID: Final[frozenset[str]] = frozenset({"uuid1", "uuid4"})
# 确定性安全的 uuid（基于命名空间 + 名字，可复现）
_OK_UUID: Final[frozenset[str]] = frozenset({"uuid3", "uuid5"})


@dataclass(frozen=True)
class GuardFinding:
    """一条守卫发现。``exempted=True`` 表示被 ``# guard: allow`` 白名单豁免（仍留痕）。"""

    path: str
    line: int
    col: int
    code: str
    message: str
    exempted: bool = False
    exempt_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "col": self.col, "code": self.code,
                "message": self.message, "exempted": self.exempted,
                "exempt_reason": self.exempt_reason}

    def hint(self) -> str:
        """供 CLI ``--fix-hints``：建议的白名单注释。"""
        rel = Path(self.path).name
        return (f"{self.path}:{self.line}  {rel}  "
                f"# guard: allow {self.code} reason=<填豁免理由>")


# =====================================================================
# 扫描入口
# =====================================================================

def scan_determinism(root: Path) -> list[GuardFinding]:
    """INV-6：随机性只来自显式 seed。"""
    return list(_scan(root, _DeterminismVisitor))


def scan_layer_imports(root: Path) -> list[GuardFinding]:
    """INV-1 + NFR-09-5：分层 import 门禁。"""
    return list(_scan(root, _LayerImportVisitor))


def scan_all(root: Path) -> list[GuardFinding]:
    """CI 与 CLI 复用：合并两类扫描。"""
    return scan_determinism(root) + scan_layer_imports(root)


def _scan(root: Path, visitor_cls: type[ast.NodeVisitor]) -> Iterator[GuardFinding]:
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py")):
        if _is_scan_exempt_path(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            yield GuardFinding(str(path), getattr(exc, "lineno", 1) or 1,
                               getattr(exc, "offset", 1) or 1, E.E_ENV_CODE_SYNTAX,
                               f"AST 解析失败：{exc}")
            continue
        visitor = visitor_cls(path)
        # 只用 NodeVisitor 自身的递归（generic_visit），不与 ast.walk 叠加，避免重复访问
        visitor.visit(tree)
        yield from visitor.findings


def _is_scan_exempt_path(path: Path) -> bool:
    parts = path.parts
    if ".venv" in parts or "site-packages" in parts or "__pycache__" in parts:
        return True
    # 守卫 fixture 必须被扫描（G2：fixture 纳入版本控制）；其余 tests/ 内文件豁免
    # （测试自身常需 import random 等用于构造用例）
    if "fixtures" in parts:
        return False
    return "tests" in parts or path.name in ("conftest.py",)


# =====================================================================
# 确定性扫描（INV-6）
# =====================================================================

class _DeterminismVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[GuardFinding] = []
        self._lines: list[str] | None = None
        self._has_seed = False

    def _line(self, lineno: int) -> str:
        if self._lines is None:
            self._lines = self.path.read_text(encoding="utf-8").splitlines()
        return self._lines[lineno - 1] if 0 < lineno <= len(self._lines) else ""

    def _exemption(self, node: ast.AST, code: str, message: str) -> GuardFinding:
        text = self._line(getattr(node, "lineno", 1))
        match = _EXEMPT_RE.search(text)
        if not match or not match.group("reason"):
            return GuardFinding(str(self.path), node.lineno, node.col_offset, code, message)
        codes = match.group("codes").split()
        if "*" not in codes and code not in codes:
            return GuardFinding(str(self.path), node.lineno, node.col_offset, code, message)
        return GuardFinding(str(self.path), node.lineno, node.col_offset, code, message,
                            exempted=True, exempt_reason=match.group("reason"))

    # ---- import random / numpy.random -------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            mod = alias.name or ""
            if mod == "random" or mod.startswith("random."):
                self.findings.append(self._exemption(
                    node, E.E_IO_GUARD_RANDOM,
                    f"import {mod}：随机性必须经 infra.derive_seed 派生（INV-6）"))
            if mod.startswith("numpy.random"):
                self.findings.append(self._exemption(
                    node, E.E_IO_GUARD_RANDOM,
                    f"import {mod}：numpy 随机源必须显式 seed（INV-6）"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod == "random" or mod.startswith("random."):
            names = ", ".join(a.name for a in node.names)
            self.findings.append(self._exemption(
                node, E.E_IO_GUARD_RANDOM,
                f"from {mod} import {names}：随机性必须经 infra.derive_seed 派生（INV-6）"))
        if mod.startswith("numpy.random"):
            self.findings.append(self._exemption(
                node, E.E_IO_GUARD_RANDOM,
                f"from {mod} import ...：numpy 随机源必须显式 seed（INV-6）"))
        self.generic_visit(node)

    # ---- 调用：random.* / numpy.random.* / time.time() / uuid4() ------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        qual = _dotted_name(func)
        if qual is not None:
            head, _, tail = qual.partition(".")
            for mod, attrs in _BANNED_CALLS:
                if qual.startswith(mod):
                    if attrs is None or tail in attrs:
                        self.findings.append(self._exemption(
                            node, E.E_IO_GUARD_RANDOM,
                            f"{qual}()：未显式 seed 的随机/时间源（INV-6）"))
            if head == "uuid" and tail in _BANNED_UUID:
                self.findings.append(self._exemption(
                    node, E.E_IO_GUARD_RANDOM,
                    f"uuid.{tail}() 不是确定性 id 生成器；请用 derive_seed（INV-6）"))
        self.generic_visit(node)


def _dotted_name(node: ast.expr) -> str | None:
    """把 ``a.b.c`` 形式的属性访问还原为点分字符串；否则返回 None。"""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# =====================================================================
# 分层 import 门禁（INV-1 / NFR-09-5）
# =====================================================================

class _LayerImportVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[GuardFinding] = []
        self._layer = _layer_of(path)

    def _report(self, node: ast.AST, module: str, code: str, message: str) -> None:
        self.findings.append(GuardFinding(str(self.path), node.lineno, node.col_offset,
                                          code, message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(node, alias.name or "")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._check_module(node, node.module)
        self.generic_visit(node)

    def _check_module(self, node: ast.AST, module: str) -> None:
        if not module.startswith("gamer_rsi"):
            return
        if self._layer in LAYER_ROOTS:
            # agent 侧不得直接 import 环境层（INV-1）
            if module == ENV_PACKAGE or module.startswith(ENV_PACKAGE + "."):
                self._report(
                    node, module, E.E_IO_GUARD_LAYER,
                    f"{self._layer} 层不得 import {module}：只能通过 SandboxEnv 触达环境（INV-1）")
        elif self._layer == "infra":
            # infra 不得 import 任何其他子包（NFR-09-5）
            other = module.split(".", 2)[:2]
            if other != ["gamer_rsi", "infra"]:
                self._report(
                    node, module, E.E_IO_GUARD_LAYER,
                    f"infra 不得 import {'.'.join(other)}（NFR-09-5：依赖单向）")


def _layer_of(path: Path) -> str:
    """相对 ``gamer_rsi/`` 的一级子包名（如 ``agent``）；infra 内部文件返回 ``infra``。"""
    parts = path.parts
    if "gamer_rsi" not in parts:
        return ""
    idx = parts.index("gamer_rsi")
    return parts[idx + 1] if idx + 1 < len(parts) else ""


# =====================================================================
# 事件留痕（FR-09-15：豁免也记 guard_finding）
# =====================================================================

def emit_findings(findings: Sequence[GuardFinding]) -> None:
    for f in findings:
        events.emit("infra.guard_finding", code=f.code, path=f.path, line=f.line,
                    exempted=f.exempted, message=f.message,
                    reason=f.exempt_reason)
