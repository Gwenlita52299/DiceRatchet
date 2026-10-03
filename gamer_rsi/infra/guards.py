"""确定性守卫与分层 import 门禁（FR-09-15 / INV-1 / INV-6）。

用 **AST**（非正则）扫描，把不变量变成可执行门禁：

- ``scan_determinism()`` → ``E_IO_GUARD_RANDOM``：
  ``import random`` / 未显式 seed 的 ``numpy.random.*`` / ``time.time()`` /
  ``uuid1|uuid4()`` / 未显式 seed 的 ``torch.*`` 全局随机。别名会先归一
  （``import numpy as np`` 与 ``numpy.random.*`` 等价、``import numpy.random as nr``
  与 ``numpy.random.*`` 等价），并按 Spec §8.1 放行**显式带 seed 的**工厂
  （``numpy.random.default_rng(seed)`` 等，MOD-08 NFR-08-01 的指定写法）。
- ``scan_layer_imports()`` → ``E_IO_GUARD_LAYER``：
  ``gamer_rsi/{agent,memory,loop,evolve,evals,inference}`` 中出现
  ``gamer_rsi.env.*``（违反 INV-1，含 ``from ..env import ...`` 相对写法）；
  以及 ``gamer_rsi/infra`` import 其他子包（违反 NFR-09-5 单向依赖）。

白名单注释 ``# guard: allow <CODE> reason=...`` 可豁免（同行），两类扫描都生效，
但豁免也会记 ``guard_finding`` 事件留痕。
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
           "emit_findings", "ALLOW_PREFIX", "LAYER_ROOTS"]

# agent 侧模块不得直接 import 环境层（INV-1：只能经 SandboxEnv 触达环境）
LAYER_ROOTS: Final[tuple[str, ...]] = (
    "agent", "memory", "loop", "evolve", "evals", "inference",
)
# 环境层包：agent 侧禁止直接依赖
ENV_PACKAGE: Final[str] = "gamer_rsi.env"
# infra 不得 import 任何其他 gamer_rsi 子包（NFR-09-5）
INFRA_PACKAGE: Final[str] = "gamer_rsi.infra"
ROOT_PACKAGE: Final[str] = "gamer_rsi"
ALLOW_PREFIX: Final[str] = "# guard: allow"

_EXEMPT_RE: Final[re.Pattern[str]] = re.compile(
    r"#\s*guard:\s*allow\s+(?P<codes>[^#]*?)\s*(?:reason=(?P<reason>\S+))?\s*(?:#.*)?$"
)

# 确定性禁止：模块名（别名归一后）→ 具体属性集合；None 表示该模块任意属性调用
_BANNED_CALLS: Final[tuple[tuple[str, frozenset[str] | None], ...]] = (
    ("random", None), # 任意 random.* 调用
    ("numpy.random", None), # 见 _numpy_call_is_exempt：显式 seed 的工厂放行
    # 仅墙钟 time.time() 参与采样才违例；perf_counter/monotonic 用于计时，不参与采样
    ("time", frozenset({"time", "time_ns"})),
)
_BANNED_UUID: Final[frozenset[str]] = frozenset({"uuid1", "uuid4"})
# 显式带 seed 即可放行的 numpy 随机工厂（Spec §8.1 / MOD-08 NFR-08-01）
_NUMPY_SEEDED_FACTORIES: Final[frozenset[str]] = frozenset({
    "default_rng", "seed", "RandomState", "Generator", "PCG64", "PCG64DXSM",
    "Philox", "SFC64", "MT19937", "SeedSequence",
})
# torch 全局随机（Spec §8.1：未显式设 seed 的 torch 全局随机禁用）
_TORCH_GLOBAL_RANDOM: Final[frozenset[str]] = frozenset({
    "rand", "randn", "randint", "randperm", "random", "normal", "multinomial",
    "bernoulli", "poisson", "dropout", "rand_like", "randn_like", "randint_like",
})


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
        visitor = visitor_cls(path, tree)
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


class _ExemptVisitor(ast.NodeVisitor):
    """带 ``# guard: allow <CODE> reason=...`` 同行豁免能力的访问器基类。"""

    def __init__(self, path: Path, tree: ast.AST | None = None) -> None:
        self.path = path
        self.tree = tree
        self.findings: list[GuardFinding] = []
        self._lines: list[str] | None = None

    def _line(self, lineno: int) -> str:
        if self._lines is None:
            self._lines = self.path.read_text(encoding="utf-8").splitlines()
        return self._lines[lineno - 1] if 0 < lineno <= len(self._lines) else ""

    def _exemption(self, node: ast.AST, code: str, message: str) -> GuardFinding:
        """构造 finding；同行注释命中 ``# guard: allow <code> reason=...`` 时标记豁免。"""
        lineno = getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0)
        match = _EXEMPT_RE.search(self._line(lineno))
        if not match or not match.group("reason"):
            return GuardFinding(str(self.path), lineno, col, code, message)
        codes = match.group("codes").split()
        if "*" not in codes and code not in codes:
            return GuardFinding(str(self.path), lineno, col, code, message)
        return GuardFinding(str(self.path), lineno, col, code, message,
                            exempted=True, exempt_reason=match.group("reason"))

    def add(self, node: ast.AST, code: str, message: str) -> None:
        self.findings.append(self._exemption(node, code, message))


# =====================================================================
# 确定性扫描（INV-6）
# =====================================================================

class _DeterminismVisitor(_ExemptVisitor):
    def __init__(self, path: Path, tree: ast.AST | None = None) -> None:
        super().__init__(path, tree)
        self._aliases: dict[str, str] | None = (
            _module_aliases_of(tree) if tree is not None else None)
        self._torch_seeded: bool | None = None

    def _alias_map(self) -> dict[str, str]:
        if self._aliases is None:
            self._aliases = _module_aliases(self.path)
        return self._aliases

    def _torch_is_seeded(self) -> bool:
        """文件内是否出现 ``torch.manual_seed(<arg>)``（显式 seed ⇒ 全局随机可复现）。"""
        if self._torch_seeded is None:
            seeded = False
            tree = self.tree if self.tree is not None else _parse_or_none(self.path)
            if tree is not None:
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    qual = _resolve_alias(_dotted_name(node.func), self._alias_map()) or ""
                    if qual in ("torch.manual_seed", "torch.seed", "torch.cuda.manual_seed",
                                "torch.cuda.manual_seed_all"):
                        seeded = True
                        break
            self._torch_seeded = seeded
        return self._torch_seeded

    # ---- import random / numpy.random -------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            mod = alias.name or ""
            # Spec §8.1 全禁 ``random.*``；numpy.random 本身是合法 API 面（seed 由调用点校验）
            if mod == "random" or mod.startswith("random."):
                self.add(node, E.E_IO_GUARD_RANDOM,
                         f"import {mod}：随机性必须经 infra.derive_seed 派生（INV-6）")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if node.level:
            self.generic_visit(node)
            return
        if mod == "random" or mod.startswith("random."):
            names = ", ".join(a.name for a in node.names)
            self.add(node, E.E_IO_GUARD_RANDOM,
                     f"from {mod} import {names}：随机性必须经 infra.derive_seed 派生（INV-6）")
        elif mod.startswith("numpy.random"):
            risky = [a.name for a in node.names
                     if a.name not in _NUMPY_SEEDED_FACTORIES]
            if risky: # ``from numpy.random import default_rng`` 合法；引入 rand 等全局函数才违例
                self.add(node, E.E_IO_GUARD_RANDOM,
                         f"from {mod} import {', '.join(risky)}：numpy 随机源必须显式 seed（INV-6）")
        self.generic_visit(node)

    # ---- 调用：random.* / numpy.random.* / time.time() / uuid4() / torch.* --------
    def visit_Call(self, node: ast.Call) -> None:
        qual = _resolve_alias(_dotted_name(node.func), self._alias_map())
        if qual is not None:
            head = qual.split(".", 1)[0]
            tail = qual.rsplit(".", 1)[-1]
            for mod, attrs in _BANNED_CALLS:
                if qual == mod or qual.startswith(mod + "."):
                    if attrs is None:
                        if mod == "numpy.random" and self._numpy_call_is_seeded_factory(tail, node):
                            break
                        self.add(node, E.E_IO_GUARD_RANDOM,
                                 f"{qual}()：未显式 seed 的随机源（INV-6）")
                    elif tail in attrs:
                        self.add(node, E.E_IO_GUARD_RANDOM,
                                 f"{qual}()：未显式 seed 的随机/时间源（INV-6）")
                    break
            if head == "uuid" and tail in _BANNED_UUID:
                self.add(node, E.E_IO_GUARD_RANDOM,
                         f"uuid.{tail}() 不是确定性 id 生成器；请用 derive_seed（INV-6）")
            if ((qual == "torch" or qual.startswith("torch."))
                    and tail in _TORCH_GLOBAL_RANDOM and not self._torch_is_seeded()):
                self.add(node, E.E_IO_GUARD_RANDOM,
                         f"{qual}()：未显式 seed 的 torch 全局随机（Spec §8.1）")
        self.generic_visit(node)

    @staticmethod
    def _numpy_call_is_seeded_factory(tail: str, node: ast.Call) -> bool:
        """``numpy.random.default_rng(seed)`` 等显式带 seed 的工厂放行（Spec §8.1）。"""
        if tail not in _NUMPY_SEEDED_FACTORIES:
            return False
        if node.args:
            return True
        return any(kw.arg in ("seed", "entropy") for kw in node.keywords)


def _parse_or_none(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError): # pragma: no cover - _scan 已先行解析
        return None


def _module_aliases(path: Path) -> dict[str, str]:
    """``局部名 → 绝对模块路径``（``import numpy as np`` / ``from numpy.random import default_rng``）。

    只处理绝对导入；相对导入无法可靠映射到顶层模块，交由分层门禁（``level``）负责。
    """
    tree = _parse_or_none(path)
    return _module_aliases_of(tree) if tree is not None else {}


def _module_aliases_of(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                if alias.asname:
                    aliases.setdefault(alias.asname, name)
                else:
                    # ``import a.b.c`` 只绑定顶层名 ``a``
                    aliases.setdefault(name.split(".")[0], name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level: # 相对导入：不映射为顶层模块
                continue
            base = node.module or ""
            if not base:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases.setdefault(alias.asname or alias.name, f"{base}.{alias.name}")
    return aliases


def _resolve_alias(qual: str | None, aliases: dict[str, str]) -> str | None:
    """把 ``np.random.rand`` 这类点分名按别名表还原为绝对模块路径。"""
    if qual is None:
        return None
    head, _, rest = qual.partition(".")
    canonical = aliases.get(head)
    if canonical is None or canonical == head:
        return qual
    return f"{canonical}.{rest}" if rest else canonical


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

class _LayerImportVisitor(_ExemptVisitor):
    def __init__(self, path: Path, tree: ast.AST | None = None) -> None:
        super().__init__(path, tree)
        self._layer = _layer_of(path)
        self._package = _package_parts(path)

    def _report(self, node: ast.AST, module: str, code: str, message: str) -> None:
        self.add(node, code, message)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(node, alias.name or "")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self._absolute_module(node)
        if module:
            self._check_module(node, module)
        self.generic_visit(node)

    def _absolute_module(self, node: ast.ImportFrom) -> str:
        """把 ``from ..env.engine import X``（level=2）还原为 ``gamer_rsi.env.engine``。

        相对导入同样受 INV-1 / NFR-09-5 约束——只认绝对写法的门禁可被 ``..`` 绕过。
        """
        if not node.level:
            return node.module or ""
        if not self._package:
            return "" # 不在 gamer_rsi 包内（如临时目录 fixture）：无法解析，跳过
        drop = node.level - 1
        base = self._package[: len(self._package) - drop] if drop < len(self._package) else []
        if not base:
            return ""
        return f"{'.'.join(base)}.{node.module}" if node.module else ".".join(base)

    def _check_module(self, node: ast.AST, module: str) -> None:
        if not module.startswith(ROOT_PACKAGE):
            return
        parts = module.split(".")
        if len(parts) < 2:
            return # 仅 ``gamer_rsi`` 本身：无法判断目标子包，不误报
        if self._layer in LAYER_ROOTS:
            # agent 侧不得直接 import 环境层（INV-1，绝对与相对写法一视同仁）
            if module == ENV_PACKAGE or module.startswith(ENV_PACKAGE + "."):
                self._report(
                    node, module, E.E_IO_GUARD_LAYER,
                    f"{self._layer} 层不得 import {module}：只能通过 SandboxEnv 触达环境（INV-1）")
        elif self._layer == "infra":
            # infra 不得 import 任何其他子包（NFR-09-5）
            other = parts[:2]
            if other != [ROOT_PACKAGE, "infra"]:
                self._report(
                    node, module, E.E_IO_GUARD_LAYER,
                    f"infra 不得 import {'.'.join(other)}（NFR-09-5：依赖单向）")


def _layer_of(path: Path) -> str:
    """相对 ``gamer_rsi/`` 的一级子包名（如 ``agent``）；infra 内部文件返回 ``infra``。"""
    parts = path.parts
    if ROOT_PACKAGE not in parts:
        return ""
    idx = parts.index(ROOT_PACKAGE)
    return parts[idx + 1] if idx + 1 < len(parts) else ""


def _package_parts(path: Path) -> tuple[str, ...]:
    """文件所在包的点分成分（``gamer_rsi/infra/cli.py`` → ``("gamer_rsi", "infra")``）。"""
    parts = path.parts
    if ROOT_PACKAGE not in parts:
        return ()
    idx = parts.index(ROOT_PACKAGE)
    return tuple(parts[idx:-1])


# =====================================================================
# 事件留痕（FR-09-15：豁免也记 guard_finding）
# =====================================================================

def emit_findings(findings: Sequence[GuardFinding]) -> None:
    for f in findings:
        events.emit("infra.guard_finding", code=f.code, path=f.path, line=f.line,
                    exempted=f.exempted, message=f.message,
                    reason=f.exempt_reason)
