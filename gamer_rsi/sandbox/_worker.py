"""MOD-02 常驻工具执行子进程（FR-02-14；**不是**对外接口，仅由 ``ToolWorker`` 启动）。

启动方式：``[sys.executable, "-I", "-S", _worker.py, <options-json>]``。

- ``RLIMIT_AS/RLIMIT_CPU/RLIMIT_NOFILE`` **只在本子进程**设置（主进程持有模型，
  绝不设 rlimit）。
- 工具代码 ``exec`` 进 ``guarded_globals()``（受限 builtins + 白名单 import + 蜜罐哨兵）。
- 协议：父 → 子按行 JSON ``{id, op, ...}``；子 → 父 ``{id, ok, result|error}``。
- 触碰蜜罐哨兵 → ``E_ENV_ESCAPE`` 并退出（父进程收到后重启，P0 事件留痕）。
"""

from __future__ import annotations

import json
import resource
import sys
import types
from pathlib import Path
from typing import Any


def _set_limits(mem_limit_mb: int, cpu_limit_s: int, nofile: int = 64) -> None:
    """尽力而为的资源上限（macOS 上 ``RLIMIT_AS`` 不完全生效，见 PRD §14 风险表）。"""
    for name, value in (("RLIMIT_CPU", int(cpu_limit_s)),
                        ("RLIMIT_NOFILE", int(nofile)),
                        ("RLIMIT_AS", int(mem_limit_mb) * 1024 * 1024)):
        limit = getattr(resource, name, None)
        if limit is None: # pragma: no cover - Windows
            continue
        try:
            soft, hard = resource.getrlimit(limit)
            cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (cap, hard))
        except (ValueError, OSError): # pragma: no cover - 平台差异
            pass


def _load_tools(harness_dir: Path, options: dict[str, Any]) -> dict[str, Any]:
    """加载 ``tools/*.py``（或目录下的 ``*.py``）到受限 globals。"""
    from gamer_rsi.sandbox.guard import DEFAULT_TOOL_IMPORT_ALLOWLIST, guarded_globals

    allow = options.get("import_allowlist") or DEFAULT_TOOL_IMPORT_ALLOWLIST
    max_tools = int(options.get("max_tools", 32))
    root = Path(harness_dir)
    tools_dir = root / "tools"
    if not tools_dir.is_dir():
        tools_dir = root
    files = sorted(p for p in tools_dir.glob("*.py") if p.name != "__init__.py")
    tools: dict[str, Any] = {}
    for path in files:
        if len(tools) >= max_tools:
            break
        namespace = guarded_globals(import_allowlist=allow)
        source = path.read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), namespace) # noqa: S102 - 受限 globals
        # 只用 FunctionType 过滤：蜜罐哨兵是普通对象，任何 getattr 反射都会触发
        # E_ENV_ESCAPE（FR-02-15 的设计意图），加载器必须绕开它们。
        functions = {k: v for k, v in namespace.items()
                     if isinstance(v, types.FunctionType)}
        marked = {k: v for k, v in functions.items()
                  if getattr(v, "__harness_tool__", False)}
        chosen = marked or {k: v for k, v in functions.items()
                            if not k.startswith("_")
                            and getattr(v, "__module__", "") == "harness_tool"}
        tools.update(chosen)
    return tools


def _respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    options = json.loads(argv[1]) if len(argv) > 1 else {}
    repo_root = options.get("repo_root")
    if repo_root and repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    _set_limits(int(options.get("mem_limit_mb", 256)), int(options.get("cpu_limit_s", 2)))

    from gamer_rsi.infra.errors import E

    try:
        tools = _load_tools(Path(options.get("harness_dir", ".")), options)
    except Exception as exc: # noqa: BLE001 - 启动失败必须让父进程看到
        sys.stderr.write(json.dumps({"ok": False, "phase": "load",
                                     "error": {"code": str(getattr(exc, "code",
                                                                   "E_ENV_AST_DENIED")),
                                               "message": str(exc)}},
                                    ensure_ascii=False) + "\n")
        return 4

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _respond({"id": -1, "ok": False,
                      "error": {"code": E.E_ENV_RESOURCE, "message": "协议 JSON 解析失败"}})
            continue
        req_id = request.get("id")
        op = request.get("op")
        if op == "shutdown":
            _respond({"id": req_id, "ok": True, "result": None})
            return 0
        if op == "list":
            _respond({"id": req_id, "ok": True, "tools": sorted(tools)})
            continue
        if op != "call":
            _respond({"id": req_id, "ok": False,
                      "error": {"code": E.E_ENV_AST_DENIED, "message": f"未知 op {op!r}"}})
            continue
        name = str(request.get("tool", ""))
        func = tools.get(name)
        if func is None:
            _respond({"id": req_id, "ok": False,
                      "error": {"code": E.E_ACTION_TOOL_SIG,
                                "message": f"未注册的工具 {name!r}（可用：{sorted(tools)}）"}})
            continue
        try:
            result = func(dict(request.get("obs") or {}),
                          [dict(c) for c in (request.get("candidates") or [])],
                          [dict(m) for m in (request.get("memory") or [])])
        except MemoryError:
            _respond({"id": req_id, "ok": False,
                      "error": {"code": E.E_ENV_RESOURCE, "message": "工具超出内存上限"}})
            return 3
        except Exception as exc: # noqa: BLE001 - 哨兵/工具体异常统一转结构化错误
            code = str(getattr(exc, "code", E.E_ACTION_TOOL_FAIL))
            _respond({"id": req_id, "ok": False,
                      "error": {"code": code, "message": f"{type(exc).__name__}: {exc}"}})
            if code == E.E_ENV_ESCAPE:
                return 5 # P0：父进程收到后重启 worker 并卸载该 harness 版本
            continue
        if not isinstance(result, list):
            _respond({"id": req_id, "ok": False,
                      "error": {"code": E.E_ACTION_TOOL_SIG,
                                "message": f"工具必须返回 list，实际 {type(result).__name__}"}})
            continue
        _respond({"id": req_id, "ok": True, "result": result})
    return 0


if __name__ == "__main__": # pragma: no cover - 子进程入口
    sys.exit(main(sys.argv))
