"""MOD-02 门禁与受限执行：INV-1 / INV-4 的执行者（PRD FR-02-9 ~ FR-02-15）。

三层防线，全部 **fail-closed**（隔离类错误拒绝执行，NFR-02-4）：

1. **静态 AST 门禁**（``check_imports`` / ``analyze_code`` / ``gate``）：节点黑名单 +
   调用白名单。v1.1 起不再用节点白名单——它漏掉了 ``Add/Eq/And/USub`` 等运算符节点，
   任何写了 ``a + b`` 的合法工具都会被拒（FR-02-12）。
2. **运行期 import hook**（``install_agent_import_hook``）：按**调用方模块路径**判定，
   ``dice_ratchet.{agent,memory,loop,evolve,evals,inference}`` 发起 ``dice_ratchet.env.*``
   导入即抛 ``E_ENV_IMPORT_DENIED``；``dice_ratchet.sandbox`` 自身的导入放行（FR-02-10）。
3. **子进程边界**：``run_agent_tick``（一次性 tick）与 ``ToolWorker``（常驻工具执行）。
   ``RLIMIT_*`` **只设在子进程**——v1.0 在进程内设 ``RLIMIT_AS=256MB`` 会直接杀掉持有
   5～19 GB 权重的主进程（FR-02-14）；Python 线程无法强杀，所以用墙钟 + 子进程。
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError

__all__ = [
    "GuardViolation", "GuardFinding", "GuardReport", "ToolWorker",
    "BLOCKED_CALLS", "BLOCKED_MODULES", "BLOCKED_NODES", "ATTR_DENY",
    "GUARDED_BUILTINS", "DEFAULT_TOOL_IMPORT_ALLOWLIST", "AGENT_LAYER_ROOTS",
    "ENV_ROOTS", "check_imports", "analyze_code", "gate", "install_agent_import_hook",
    "guarded_globals", "run_agent_tick", "load_harness_tools", "GATE_SCHEMA_VERSION",
]

# ----------------------------------------------------------------------
# 黑/白名单（Spec §6；集合按字母序维护）
# ----------------------------------------------------------------------

BLOCKED_CALLS: Final[frozenset[str]] = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "breakpoint", "memoryview",
})

BLOCKED_MODULES: Final[frozenset[str]] = frozenset({
    "os", "sys", "io", "socket", "subprocess", "shutil", "pathlib", "importlib",
    "pickle", "marshal", "ctypes", "multiprocessing", "threading", "requests",
    "urllib", "http", "dice_ratchet.env", "dice_ratchet.sandbox",
})

BLOCKED_NODES: Final[frozenset[str]] = frozenset({
    "Import", "ImportFrom", "While", "Try", "With", "AsyncFunctionDef", "AsyncFor",
    "AsyncWith", "ClassDef", "Lambda", "Yield", "YieldFrom", "Await", "Global",
    "Nonlocal", "Delete", "Raise",
})

#: 任何以 ``__`` 开头的属性名（dunder 逃逸）。
ATTR_DENY: Final[tuple[str, ...]] = ("__",)

GUARDED_BUILTINS: Final[frozenset[str]] = frozenset({
    "len", "range", "sorted", "min", "max", "sum", "abs", "round", "int", "float",
    "str", "bool", "list", "dict", "tuple", "set", "enumerate", "zip", "reversed",
    "any", "all", "isinstance", "print", "ValueError", "TypeError", "KeyError",
    "IndexError", "ZeroDivisionError",
})

#: 工具代码唯一允许的 import 根（``evolve.tool_import_allowlist`` 的代码默认值）。
DEFAULT_TOOL_IMPORT_ALLOWLIST: Final[tuple[str, ...]] = (
    "math", "re", "json", "itertools", "functools", "typing", "dataclasses",
)

#: agent 侧分层根（Spec §4.7 / INV-1）：这些模块发起 ``dice_ratchet.env.*`` 导入即拒绝。
AGENT_LAYER_ROOTS: Final[tuple[str, ...]] = (
    "dice_ratchet.agent", "dice_ratchet.evolve", "dice_ratchet.evals", "dice_ratchet.inference",
    "dice_ratchet.loop", "dice_ratchet.memory",
)
ENV_ROOTS: Final[tuple[str, ...]] = ("dice_ratchet.env",)
SANDBOX_PREFIX: Final[str] = "dice_ratchet.sandbox"

GATE_SCHEMA_VERSION: Final[str] = "harness-gate-1.0.0"

_WORKER_PATH: Final[Path] = Path(__file__).resolve().parent / "_worker.py"
_IMPORTLIB_PREFIXES: Final[tuple[str, ...]] = ("importlib", "_frozen_importlib", "zipimport")


class GuardViolation(InfraError):
    """门禁/受限执行违例：携带错误码 + 可选的源码位置（PRD §6）。

    继承 ``InfraError``，因此 CLI 能直接拿到退出码（Spec §8.3：沙箱违例退出码 4）。
    """

    def __init__(self, code: str, message: str, *, lineno: int | None = None,
                 col: int | None = None, hint: str | None = None) -> None:
        self.lineno = lineno
        self.col = col
        self.message = message
        location = f"line {lineno}" + (f":{col}" if col is not None else "")
        super().__init__(code, f"{location}: {message}" if lineno is not None else message,
                         hint=hint)
        self.lineno = lineno
        self.col = col


@dataclass(frozen=True)
class GuardFinding:
    """一条门禁发现（PRD §6）。"""

    code: str
    lineno: int
    col: int
    detail: str
    file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "lineno": self.lineno, "col": self.col,
                "detail": self.detail, "file": self.file}


@dataclass(frozen=True)
class GuardReport:
    """一次门禁的整体结论（``ok=False`` 时调用方必须 fail-closed）。"""

    ok: bool
    findings: tuple[GuardFinding, ...] = ()
    n_imports: int = 0
    scanned_files: tuple[str, ...] = ()

    @property
    def violations(self) -> list[str]:
        return [f"{f.file or '<source>'}:{f.lineno}:{f.col} [{f.code}] {f.detail}"
                for f in self.findings]

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted({f.code for f in self.findings}))

    def raise_if_blocked(self) -> "GuardReport":
        if not self.ok:
            first = self.findings[0]
            raise GuardViolation(first.code, first.detail, lineno=first.lineno,
                                 col=first.col,
                                 hint="harness 工具必须是纯函数：无 IO / 无网络 / "
                                      "无 import env / 无 dunder（INV-4）")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": GATE_SCHEMA_VERSION, "ok": self.ok,
                "n_imports": self.n_imports,
                "scanned_files": list(self.scanned_files),
                "findings": [f.to_dict() for f in self.findings]}


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardViolation(E.E_ENV_CODE_SYNTAX, f"读不到文件：{path}") from exc


def _parse(source: str, filename: str) -> ast.Module:
    try:
        return ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise GuardViolation(E.E_ENV_CODE_SYNTAX,
                             f"AST 解析失败：{exc.msg}",
                             lineno=exc.lineno, col=exc.offset or 0) from exc


# ----------------------------------------------------------------------
# ① 静态 import 门禁（FR-02-9）
# ----------------------------------------------------------------------

def check_imports(paths: Iterable[Path], *,
                  roots: tuple[str, ...] = ENV_ROOTS) -> GuardReport:
    """扫描给定文件/目录，命中 ``import dice_ratchet.env...`` 即 ``E_ENV_IMPORT_DENIED``。

    返回 ``GuardReport``（带行号）；不抛异常，便于 CI 汇总全部命中后再失败。
    """
    findings: list[GuardFinding] = []
    scanned: list[str] = []
    n_imports = 0
    for path in _expand(paths):
        scanned.append(str(path))
        try:
            tree = _parse(_read_source(path), str(path))
        except GuardViolation as exc:
            findings.append(GuardFinding(exc.code, exc.lineno or 0, exc.col or 0,
                                         exc.message, file=str(path)))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                n_imports += 1
                for alias in node.names:
                    if _hits(alias.name, roots):
                        findings.append(GuardFinding(
                            E.E_ENV_IMPORT_DENIED, node.lineno, node.col_offset,
                            f"import {alias.name}：只能经 SandboxEnv 触达环境（INV-1）",
                            file=str(path)))
            elif isinstance(node, ast.ImportFrom):
                n_imports += 1
                module = node.module or ""
                if node.level:
                    continue # 相对 import 由引用点决定，静态无法判定包根
                if _hits(module, roots):
                    findings.append(GuardFinding(
                        E.E_ENV_IMPORT_DENIED, node.lineno, node.col_offset,
                        f"from {module} import ...：只能经 SandboxEnv 触达环境（INV-1）",
                        file=str(path)))
    return GuardReport(ok=not findings, findings=tuple(findings),
                       n_imports=n_imports, scanned_files=tuple(scanned))


def _hits(module: str, roots: Sequence[str]) -> bool:
    return any(module == r or module.startswith(r + ".") for r in roots)


def _expand(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
        else:
            out.append(path)
    return out


# ----------------------------------------------------------------------
# ② AST 黑名单门禁（FR-02-12）
# ----------------------------------------------------------------------

def analyze_code(source: str, *, filename: str = "<harness>",
                 import_allowlist: Sequence[str] | None = None) -> GuardReport:
    """节点黑名单 + 调用白名单；返回全部违规的行号与错误码。

    - 禁止节点：``BLOCKED_NODES``（``Import/ImportFrom`` 仅放行
      ``import_allowlist`` 内的模块根）。
    - 禁止调用/模块/属性：``BLOCKED_CALLS`` / ``BLOCKED_MODULES`` / ``ATTR_DENY``。
    - **运算符、比较、布尔、切片、推导式等表达式节点一律允许**（FR-02-12）。
    """
    allow = tuple(import_allowlist) if import_allowlist is not None \
        else DEFAULT_TOOL_IMPORT_ALLOWLIST
    findings: list[GuardFinding] = []
    n_imports = 0
    try:
        tree = _parse(source, filename)
    except GuardViolation as exc:
        return GuardReport(ok=False,
                           findings=(GuardFinding(exc.code, exc.lineno or 0, exc.col or 0,
                                                  exc.message, file=filename),),
                           scanned_files=(filename,))

    def add(node: ast.AST, code: str, detail: str) -> None:
        findings.append(GuardFinding(code, getattr(node, "lineno", 0),
                                     getattr(node, "col_offset", 0), detail,
                                     file=filename))

    for node in ast.walk(tree):
        name = type(node).__name__
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            n_imports += 1
            if isinstance(node, ast.ImportFrom) and node.level:
                add(node, E.E_ENV_AST_DENIED, "禁止相对 import（工具必须自包含）")
                continue
            modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                       else [node.module or ""])
            for module in modules:
                root = module.split(".")[0]
                if root in BLOCKED_MODULES or module in BLOCKED_MODULES:
                    add(node, E.E_ENV_AST_DENIED, f"禁止 import {module}（BLOCKED_MODULES）")
                elif root not in allow and module not in allow:
                    add(node, E.E_ENV_AST_DENIED,
                        f"禁止 import {module}：不在 tool_import_allowlist {list(allow)}")
            continue
        if name in BLOCKED_NODES:
            add(node, E.E_ENV_AST_DENIED, f"禁止的语法结构 {name}（BLOCKED_NODES）")
            continue
        if isinstance(node, ast.Attribute):
            if node.attr.startswith(ATTR_DENY):
                add(node, E.E_ENV_AST_DENIED, f"禁止 dunder 属性 .{node.attr}（ATTR_DENY）")
            dotted = _dotted(node)
            if dotted:
                head = dotted.split(".")[0]
                if dotted in BLOCKED_MODULES or head in BLOCKED_MODULES:
                    add(node, E.E_ENV_AST_DENIED, f"禁止访问 {dotted}（BLOCKED_MODULES）")
            continue
        if isinstance(node, ast.Name):
            if node.id.startswith(ATTR_DENY):
                add(node, E.E_ENV_AST_DENIED, f"禁止 dunder 名字 {node.id}")
            elif node.id == "__builtins__":
                add(node, E.E_ENV_AST_DENIED, "禁止访问 __builtins__")
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                add(node, E.E_ENV_AST_DENIED, f"禁止调用 {func.id}()（BLOCKED_CALLS）")
            elif isinstance(func, ast.Attribute):
                dotted = _dotted(func)
                head = (dotted or "").split(".")[0]
                if func.attr in BLOCKED_CALLS:
                    add(node, E.E_ENV_AST_DENIED, f"禁止调用 .{func.attr}()（BLOCKED_CALLS）")
                elif dotted and (dotted in BLOCKED_MODULES or head in BLOCKED_MODULES):
                    add(node, E.E_ENV_AST_DENIED, f"禁止调用 {dotted}()（BLOCKED_MODULES）")
            continue
    return GuardReport(ok=not findings, findings=tuple(findings),
                       n_imports=n_imports, scanned_files=(filename,))


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# ----------------------------------------------------------------------
# ③ 批次门禁 gate（FR-02-13）
# ----------------------------------------------------------------------

def _tools_dir(artifact_dir: Path) -> Path:
    """``harness/`` → ``harness/tools/``；也接受直接给 ``tools/`` 或其子目录。"""
    for candidate in (artifact_dir / "tools", artifact_dir):
        if candidate.is_dir() and (candidate.name == "tools"
                                   or any(candidate.glob("*.py"))):
            return candidate
    return artifact_dir / "tools"


def _harness_version(artifact_dir: Path) -> int:
    for candidate in (artifact_dir / "version.json", artifact_dir.parent / "version.json"):
        if candidate.is_file():
            try:
                return int(json.loads(candidate.read_text(encoding="utf-8")).get("version", 0))
            except (OSError, ValueError, TypeError):
                return 0
    return 0


def gate(artifact_dir: Path, *, run_dir: Path | None = None,
         harness_version: int | None = None,
         import_allowlist: Sequence[str] | None = None,
         max_tools: int = 32) -> GuardReport:
    """对 ``harness/tools/*.py`` 逐个 ``analyze_code``；任一失败整批拒绝（fail-closed）。

    写 ``runs/<run_id>/audit/harness-gate.json``（``run_dir`` 显式给出，或取环境变量
    ``DICERATCHET_RUN_DIR``）；旧 harness 保持可用——本函数不删改任何工件（FR-02-13）。
    """
    artifact_dir = Path(artifact_dir)
    tools_dir = _tools_dir(artifact_dir)
    files = sorted(p for p in tools_dir.glob("*.py") if p.name != "__init__.py") \
        if tools_dir.is_dir() else []
    version = harness_version if harness_version is not None \
        else _harness_version(artifact_dir)
    findings: list[GuardFinding] = []
    n_imports = 0
    if len(files) > max_tools:
        findings.append(GuardFinding(E.E_ENV_AST_DENIED, 0, 0,
                                     f"工具数 {len(files)} 超上限 {max_tools}",
                                     file=str(tools_dir)))
    for path in files:
        report = analyze_code(_read_source(path), filename=str(path),
                             import_allowlist=import_allowlist)
        n_imports += report.n_imports
        findings.extend(report.findings)
    report = GuardReport(ok=not findings, findings=tuple(findings),
                        n_imports=n_imports,
                        scanned_files=tuple(str(p) for p in files))
    _write_gate_report(report, artifact_dir=artifact_dir, run_dir=run_dir,
                       harness_version=version, max_tools=max_tools)
    for finding in report.findings[:16]:
        _emit("env.ast_denied", code=finding.code, path=finding.file,
              lineno=finding.lineno, detail=finding.detail[:200])
    _emit("sandbox.gate_pass" if report.ok else "sandbox.gate_reject",
         harness_version=version, violations=report.violations[:8],
         files=[p.name for p in files], findings=len(report.findings))
    return report


def _write_gate_report(report: GuardReport, *, artifact_dir: Path,
                       run_dir: Path | None, harness_version: int,
                       max_tools: int) -> Path | None:
    target = run_dir if run_dir is not None else _env_run_dir()
    if target is None:
        return None
    out = Path(target) / "audit" / "harness-gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {**report.to_dict(), "harness_version": harness_version,
               "artifact_dir": str(artifact_dir), "max_tools": max_tools,
               "created_at": _utc_now()}
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, out)
    try:
        os.chmod(out, 0o600)
    except OSError: # pragma: no cover - 平台差异
        pass
    return out


def _env_run_dir() -> Path | None:
    raw = os.environ.get("DICERATCHET_RUN_DIR")
    return Path(raw) if raw else None


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(event: str, **fields: Any) -> None:
    """统一埋点出口（Spec §8.2）；未绑定 run 时静默丢弃，绝不影响隔离逻辑。"""
    try:
        from dice_ratchet.infra import events

        events.emit(event, **fields)
    except Exception: # noqa: BLE001 - 埋点失败不得改变门禁结论（fail-closed 优先）
        pass


# ----------------------------------------------------------------------
# ④ 运行期 import hook（FR-02-10）
# ----------------------------------------------------------------------

class _AgentImportFinder:
    """不可卸载的 ``MetaPathFinder``：按调用方模块路径拒绝 ``dice_ratchet.env.*``。"""

    _dice_ratchet_permanent = True

    def find_spec(self, fullname: str, path: Any = None,
                  target: Any = None) -> None:
        if not (fullname == "dice_ratchet.env" or fullname.startswith("dice_ratchet.env.")):
            return None
        caller = _importer_module()
        if _is_agent_layer(caller):
            _emit("env.import_denied", module=fullname, caller=caller, path="",
                  lineno=0, phase="load")
            raise GuardViolation(
                E.E_ENV_IMPORT_DENIED,
                f"{caller} 不得 import {fullname}（INV-1：只能经 SandboxEnv 触达环境）",
                hint="agent 侧只允许 SandboxEnv.observe/step；import hook 不可卸载")
        return None


def _importer_module() -> str | None:
    """发起本次 import 的第一个「真实」帧的模块名（跳过 importlib 与本模块自身）。"""
    frame = sys._getframe(1)
    while frame is not None:
        mod = str(frame.f_globals.get("__name__", ""))
        if mod and not mod.startswith(_IMPORTLIB_PREFIXES) and mod != __name__:
            return mod
        frame = frame.f_back
    return None


def _is_agent_layer(module: str | None) -> bool:
    return bool(module) and any(
        module == root or module.startswith(root + ".") for root in AGENT_LAYER_ROOTS)


def _is_env_module(name: str) -> bool:
    return name == "dice_ratchet.env" or name.startswith("dice_ratchet.env.")


_HOOK: Final[_AgentImportFinder] = _AgentImportFinder()
_ORIGINAL_IMPORT: Final[Any] = builtins.__import__


def _agent_import_guard(name: str, globals_: Any = None, locals_: Any = None,
                        fromlist: Sequence[str] = (), level: int = 0) -> Any:
    """``builtins.__import__`` 包装：命中**已在 sys.modules** 的 env 模块也要拒绝。

    ``MetaPathFinder`` 只在缓存未命中时被咨询；而 ``SandboxEnv`` 按 FR-02-10 预先
    import 了环境层，因此 agent 侧的 ``import dice_ratchet.env.rng`` 会命中 ``sys.modules``
    缓存、绕过 finder。调用方的 ``globals["__name__"]`` 正好是发起 import 的模块名，
    用它判定即可覆盖缓存命中路径（无栈遍历开销）。
    """
    if level == 0 and _is_env_module(name):
        caller = str((globals_ or {}).get("__name__", "")) or _importer_module()
        if _is_agent_layer(caller):
            _emit("env.import_denied", module=name, caller=caller, path="",
                  lineno=0, phase="cached")
            raise GuardViolation(
                E.E_ENV_IMPORT_DENIED,
                f"{caller} 不得 import {name}（INV-1：只能经 SandboxEnv 触达环境）",
                hint="agent 侧只允许 SandboxEnv.observe/step；import hook 不可卸载")
    return _ORIGINAL_IMPORT(name, globals_, locals_, fromlist, level)


def install_agent_import_hook() -> None:
    """安装不可卸载的 import hook（幂等；被移除后再次调用会重新装回）。

    两层：``MetaPathFinder``（加载期拒绝，FR-02-10）+ ``builtins.__import__`` 包装
    （缓存命中期拒绝——agent 代码拿到的模块对象同样必须被拦下）。
    """
    if _HOOK not in sys.meta_path:
        sys.meta_path.insert(0, _HOOK)
    for finder in sys.meta_path:
        if isinstance(finder, _AgentImportFinder) and finder is not _HOOK: # pragma: no cover
            sys.meta_path.remove(finder)
    if builtins.__import__ is not _agent_import_guard:
        builtins.__import__ = _agent_import_guard


# ----------------------------------------------------------------------
# ⑤ 受限执行环境（FR-02-15）
# ----------------------------------------------------------------------

class _EscapeSentinel:
    """触碰即报 ``E_ENV_ESCAPE`` 的哨兵（FR-02-15：``_HONEYPOT_*`` 命名）。"""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "name", name)

    def _trip(self, action: str) -> Any:
        _emit("env.honeypot_escape", sentinel=self.name, action=action)
        raise GuardViolation(E.E_ENV_ESCAPE,
                             f"受限执行代码触碰蜜罐哨兵 {self.name}（{action}）",
                             hint="P0：立即卸载该 harness 版本并人工确认（PRD §10）")

    def __getattr__(self, item: str) -> Any:
        return self._trip(f"getattr:{item}")

    def __setattr__(self, item: str, value: Any) -> None:
        self._trip(f"setattr:{item}")

    def __getitem__(self, item: Any) -> Any:
        return self._trip(f"getitem:{item}")

    def __setitem__(self, item: Any, value: Any) -> None:
        self._trip(f"setitem:{item}")

    def __delitem__(self, item: Any) -> None:
        self._trip(f"delitem:{item}")

    def __iter__(self) -> Any:
        return self._trip("iter")

    def __len__(self) -> int:
        return self._trip("len") # type: ignore[return-value]

    def __bool__(self) -> bool:
        return self._trip("bool") # type: ignore[return-value]

    def __contains__(self, item: Any) -> bool:
        return self._trip("contains") # type: ignore[return-value]

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._trip("call")

    def __repr__(self) -> str:
        return f"<honeypot sentinel {self.name}>"

    def __str__(self) -> str:
        return self._trip("str")


#: 受限执行环境里可见的哨兵名字（触碰即 P0）。
HONEYPOT_SENTINEL_NAMES: Final[tuple[str, ...]] = (
    "_HONEYPOT_CANARY", "_HONEYPOT_LEDGER", "_HONEYPOT_PLAN", "honeypot_ledger",
)


def guarded_globals(*, import_allowlist: Sequence[str] | None = None) -> dict[str, Any]:
    """返回工具代码 ``exec`` 用的 globals：受限 builtins + 白名单 import + 蜜罐哨兵。

    ``__builtins__`` 被替换为白名单字典，因此 ``open``/``__import__``/``eval`` 等
    即使在运行期也无法触达（静态门禁被绕过的第二道防线，FR-02-14/15）。
    """
    import builtins as _builtins

    allow = tuple(import_allowlist) if import_allowlist is not None \
        else DEFAULT_TOOL_IMPORT_ALLOWLIST
    safe: dict[str, Any] = {}
    for name in sorted(GUARDED_BUILTINS):
        if hasattr(_builtins, name):
            safe[name] = getattr(_builtins, name)
    safe["__import__"] = _guarded_import(allow)
    g: dict[str, Any] = {
        "__builtins__": safe,
        "__name__": "harness_tool",
        "harness_tool": harness_tool,
    }
    for name in HONEYPOT_SENTINEL_NAMES:
        g[name] = _EscapeSentinel(name)
    return g


def harness_tool(func: Any) -> Any:
    """``@harness_tool`` 标记（Spec §4.4 的唯一签名约定）。"""
    func.__harness_tool__ = True
    return func


def _guarded_import(allow: Sequence[str]):
    import builtins as _builtins

    allowed = set(allow)

    def _imp(name: str, globals_: Any = None, locals_: Any = None,
             fromlist: Sequence[str] = (), level: int = 0) -> Any:
        if level:
            raise GuardViolation(E.E_ENV_AST_DENIED, "禁止相对 import（受限执行环境）")
        root = name.split(".")[0]
        if root not in allowed:
            raise GuardViolation(E.E_ENV_AST_DENIED,
                                 f"受限执行环境禁止 import {name}（白名单 {sorted(allowed)}）")
        return _builtins.__import__(name, globals_, locals_, fromlist, 0)

    return _imp


# ----------------------------------------------------------------------
# ⑥ 一次性子进程边界 run_agent_tick（FR-02-11）
# ----------------------------------------------------------------------

_TICK_SHIM: Final[str] = r"""
import json, sys
payload = json.loads(sys.stdin.readline() or "{}")
sys.path.insert(0, payload["repo_root"])
from dice_ratchet.sandbox.guard import guarded_globals  # noqa: E402
g = guarded_globals(import_allowlist=payload.get("import_allowlist"))
try:
    exec(compile(payload["code"], "<agent-tick>", "exec"), g)
    entry = g[payload["entry"]]
    result = entry(**payload.get("kwargs", {}))
    out = {"ok": True, "result": result}
except BaseException as exc:  # 边界内一律转为结构化错误
    code = getattr(exc, "code", "E_ENV_AST_DENIED")
    out = {"ok": False, "error": {"code": str(code), "message": str(exc)}}
sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
sys.stdout.flush()
"""


def _child_env() -> dict[str, str]:
    """子进程环境：清空 ``PYTHONPATH``，只留最小必需项（FR-02-11）。"""
    env = {
        "PYTHONPATH": "",
        "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", "0"),
        "PYTHONIOENCODING": "utf-8",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for key in ("SYSTEMROOT", "PATH", "HOME", "TMPDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def run_agent_tick(payload: Mapping[str, Any], *, timeout_s: float = 10.0) -> dict[str, Any]:
    """把不配合 hook 的第三方代码放进一次性子进程执行（FR-02-11）。

    ``[sys.executable, "-I", "-S", "-c", shim]`` + 清空 ``PYTHONPATH`` + 临时只读 cwd +
    stdin/stdout 仅 JSON 协议；超时即 kill 并抛 ``E_ENV_TIMEOUT``。
    """
    workdir = Path(tempfile.mkdtemp(prefix="dice-ratchet-tick-"))
    try:
        os.chmod(workdir, 0o500) # cwd 只读：子进程无法在此落盘
    except OSError: # pragma: no cover - 平台差异
        pass
    request = {"repo_root": _repo_root(), "entry": payload.get("entry", "tick"),
               "code": payload.get("code", ""),
               "kwargs": dict(payload.get("kwargs", {})),
               "import_allowlist": list(payload.get("import_allowlist",
                                                    DEFAULT_TOOL_IMPORT_ALLOWLIST))}
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", _TICK_SHIM],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(workdir), env=_child_env(), text=True, bufsize=1)
    try:
        try:
            out, err = proc.communicate(
                json.dumps(request, ensure_ascii=False) + "\n", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise GuardViolation(E.E_ENV_TIMEOUT,
                                 f"agent tick 超墙钟 {timeout_s}s，已 kill",
                                 hint="一次性子进程边界：超时即杀（FR-02-11）") from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if proc.returncode != 0 and not (out or "").strip():
        raise GuardViolation(E.E_ENV_RESOURCE if proc.returncode < 0 else E.E_ENV_AST_DENIED,
                            f"agent tick 子进程异常退出 rc={proc.returncode}："
                            f"{(err or '')[-400:]}")
    try:
        parsed = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise GuardViolation(E.E_ENV_AST_DENIED,
                             f"agent tick 输出不是 JSON：{(out or '')[:200]!r}") from exc
    if not parsed.get("ok", False):
        error = parsed.get("error") or {}
        raise GuardViolation(str(error.get("code", E.E_ENV_AST_DENIED)),
                             str(error.get("message", "tick 失败")))
    result = parsed.get("result")
    if not isinstance(result, dict):
        raise GuardViolation(E.E_ENV_AST_DENIED,
                             f"agent tick 必须返回 dict，实际 {type(result).__name__}")
    return result


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGKILL)
    except (OSError, ValueError): # pragma: no cover
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired: # pragma: no cover
        pass


# ----------------------------------------------------------------------
# ⑦ 常驻子进程 worker：ToolWorker（FR-02-14）
# ----------------------------------------------------------------------

class ToolWorker:
    """常驻子进程工具执行器（Spec §4.7：``load_harness_tools(harness_dir) -> ToolWorker``）。

    - ``RLIMIT_AS/RLIMIT_CPU/RLIMIT_NOFILE`` 只由子进程设置；主进程绝不设 rlimit。
    - 工具代码 ``exec`` 进 ``guarded_globals()``（受限 builtins + 蜜罐哨兵）。
    - 父子按行传 JSON：请求 ``{tool, obs, candidates, memory}``。
    - 每次调用有墙钟 ``timeout_s``：超时 → ``SIGKILL`` + 重启 + ``E_ENV_TIMEOUT``；
      资源超限 → worker 退出 → 重启 + ``E_ENV_RESOURCE``。调用方降级为 MOD-04 fallback。
    """

    def __init__(self, harness_dir: Path, *, timeout_s: float = 2.0,
                 mem_limit_mb: int = 256, cpu_limit_s: int = 2,
                 max_tools: int = 32,
                 import_allowlist: Sequence[str] | None = None,
                 python: str | None = None) -> None:
        self.harness_dir = Path(harness_dir)
        self.timeout_s = float(timeout_s)
        self.mem_limit_mb = int(mem_limit_mb)
        self.cpu_limit_s = int(cpu_limit_s)
        self.max_tools = int(max_tools)
        self.import_allowlist = tuple(import_allowlist) if import_allowlist is not None \
            else DEFAULT_TOOL_IMPORT_ALLOWLIST
        self.python = python or sys.executable
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = b""
        self._seq = 0
        self._workdir = Path(tempfile.mkdtemp(prefix="dice-ratchet-worker-"))
        self.restarts = 0

    # ---- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "ToolWorker":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "ToolWorker":
        if self.alive:
            return self
        options = {"harness_dir": str(self.harness_dir),
                   "mem_limit_mb": self.mem_limit_mb,
                   "cpu_limit_s": self.cpu_limit_s,
                   "max_tools": self.max_tools,
                   "import_allowlist": list(self.import_allowlist),
                   "repo_root": _repo_root()}
        self._proc = subprocess.Popen(
            [self.python, "-I", "-S", str(_WORKER_PATH), json.dumps(options)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(self._workdir), env=_child_env(), bufsize=0)
        self._buf = b""
        self._seq = 0
        return self

    def restart(self) -> None:
        _kill(self._proc) if self._proc is not None else None
        self._proc = None
        self.restarts += 1
        self.start()

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._request({"op": "shutdown"}, timeout_s=1.0)
            except Exception: # noqa: BLE001 - 关闭路径尽力而为
                pass
            _kill(self._proc)
            self._proc = None
        shutil.rmtree(self._workdir, ignore_errors=True)

    # ---- 协议 -------------------------------------------------------------
    def tools(self, *, timeout_s: float | None = None) -> list[str]:
        resp = self._request({"op": "list"}, timeout_s=timeout_s or self.timeout_s)
        return [str(x) for x in resp.get("tools", [])]

    def call(self, tool: str, *, obs: Mapping[str, Any],
             candidates: Sequence[Mapping[str, Any]],
             memory: Sequence[Mapping[str, Any]],
             timeout_s: float | None = None) -> list[dict[str, Any]]:
        """调用一个工具；返回重排后的 candidates（唯一签名，Spec §4.4）。"""
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        started = time.perf_counter()
        try:
            resp = self._request({"op": "call", "tool": tool, "obs": dict(obs),
                                  "candidates": [dict(c) for c in candidates],
                                  "memory": [dict(m) for m in memory]},
                                 timeout_s=timeout)
        except GuardViolation as exc:
            if exc.code == E.E_ENV_TIMEOUT:
                _emit("sandbox.tool_timeout", tool=tool, timeout_s=timeout)
            elif exc.code == E.E_ENV_RESOURCE:
                _emit("env.resource_limit", tool=tool, code=exc.code,
                      detail=str(exc.message)[:200])
            elif exc.code == E.E_ENV_ESCAPE:
                # 子进程的 emit 未绑定 sink（会丢），P0 事件由父进程留痕
                _emit("env.honeypot_escape", sentinel="worker", tool=tool)
            raise
        result = resp.get("result")
        if not isinstance(result, list):
            raise GuardViolation(E.E_ENV_AST_DENIED,
                                 f"工具 {tool} 必须返回 list，实际 {type(result).__name__}")
        duration = (time.perf_counter() - started) * 1000.0
        _emit("sandbox.exec", tool=tool, duration_ms=round(duration, 3), outcome="ok")
        return [dict(item) if isinstance(item, Mapping) else {"value": item}
                for item in result]

    def _request(self, message: Mapping[str, Any],
                 *, timeout_s: float) -> dict[str, Any]:
        if not self.alive:
            self.start()
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None: # pragma: no cover
            raise GuardViolation(E.E_ENV_RESOURCE, "工具 worker 启动失败")
        self._seq += 1
        payload = dict(message)
        payload["id"] = self._seq
        line = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.restart()
            raise GuardViolation(E.E_ENV_RESOURCE,
                                 "工具 worker 已退出（写管道失败），已重启",
                                 hint="连续 3 次则该 harness 版本标 degraded（PRD §10）") from None
        raw = self._read_line(timeout_s)
        if raw is None:
            died = proc.poll() is not None
            _kill(proc)
            self.restart()
            if died:
                raise GuardViolation(E.E_ENV_RESOURCE,
                                     "工具 worker 异常退出（资源超限或崩溃），已重启",
                                     hint="调用方降级为 MOD-04 fallback，不判该局失败")
            raise GuardViolation(E.E_ENV_TIMEOUT,
                                 f"工具调用超墙钟 {timeout_s}s，已 SIGKILL + 重启 worker",
                                 hint="调用方降级为 MOD-04 fallback，不判该局失败")
        try:
            resp = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.restart()
            raise GuardViolation(E.E_ENV_RESOURCE,
                                 f"worker 协议损坏（{exc}），已重启") from exc
        if not resp.get("ok", False):
            error = resp.get("error") or {}
            code = str(error.get("code", E.E_ENV_AST_DENIED))
            if code in (E.E_ENV_RESOURCE, E.E_ENV_ESCAPE):
                self.restart()
            raise GuardViolation(code, str(error.get("message", "worker 调用失败")))
        return resp

    def _read_line(self, timeout_s: float) -> bytes | None:
        proc = self._proc
        if proc is None or proc.stdout is None: # pragma: no cover - 防御
            return None
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + max(0.0, timeout_s)
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError: # pragma: no cover - 子进程已死
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line


def load_harness_tools(harness_dir: Path, **options: Any) -> ToolWorker:
    """启动常驻工具 worker（Spec §4.7）；``gate()`` 通过后再调用。"""
    worker = ToolWorker(harness_dir, **options)
    worker.start()
    return worker
