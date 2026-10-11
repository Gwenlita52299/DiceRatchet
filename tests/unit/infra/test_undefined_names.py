"""静态守卫：函数里引用的**全局名**必须真的存在（F32/F33 的缺陷类）。

2026-10-09 的 §3.2 实跑暴露了一类特别隐蔽的缺陷：**测试永远不走的 else 分支里引用了
不存在的名字**。两个实例都在闭环路径上，而且 1800+ 用例全绿毫无察觉：

- **F32** ``evolve/generation.py``：``getattr(run_ctx, "harness_dir", default_harness_dir())``
  —— Python 会**无条件求值**默认参数，而 ``default_harness_dir`` 从不存在；单测都显式传
  ``harness=``，于是生产装配（``harness=None``）一跑就 ``NameError``，白烧 ¥34.62 / 92 min。
- **F33** ``loop/generation.py``：``_hitl_gate_pending`` 用了没有模块级导入的 ``read_json``
  （只在另一个函数体内局部导入）；只在 **HITL resume** 时触发，尚未爆过。

用 stdlib ``symtable`` 逐作用域比对即可零成本抓住这一类：某名字在函数/类作用域里是
**global 引用**，却在模块级既未赋值也未导入，且不是 builtin ⇒ 运行时必炸。ruff/flake8
都没装在 venv 里（``ruff`` 缺失），故这里自带一份最小实现，并**带反例自检**保证它不空转
（与 ``scripts/ci_gate.sh`` 的「门禁反例 fixture 必须被拦下」同一先例）。
"""

from __future__ import annotations

import builtins
import symtable
from pathlib import Path

#: 由 import 系统注入的模块属性（``__file__`` 等）：``symtable`` 看不见赋值，
#: 但它们**确实**存在于模块命名空间，故不是缺陷。
_DUNDER_EXEMPT = frozenset({"__file__", "__name__", "__doc__", "__spec__", "__loader__",
                            "__package__", "__builtins__", "__path__", "__dict__",
                            "__annotations__", "__cached__"})

_BUILTINS = frozenset(dir(builtins))


def _module_level_names(top: symtable.SymbolTable) -> set[str]:
    """模块级绑定的名字（含赋值 / 导入 / def / class / import * ）。"""
    names: set[str] = set()
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            names.add(sym.get_name())
    return names


def _walk(table: symtable.SymbolTable, known: set[str],
          out: list[tuple[str, int]]) -> None:
    for child in table.get_children():
        if child.get_type() in ("function", "class"):
            for sym in child.get_symbols():
                name = sym.get_name()
                if not (sym.is_global() and sym.is_referenced()):
                    continue
                if name in known or name in _BUILTINS or name in _DUNDER_EXEMPT:
                    continue
                out.append((name, int(child.get_lineno())))
        _walk(child, known, out)


def undefined_globals(source: str, *, filename: str = "<inline>") -> list[tuple[str, int]]:
    """返回 ``[(名字, 行号)]``：函数/类里引用了、但模块级未定义且非 builtin 的全局名。"""
    top = symtable.symtable(source, filename, "exec")
    hits: list[tuple[str, int]] = []
    _walk(top, _module_level_names(top), hits)
    return hits


def _scan_package(root: Path) -> list[tuple[str, str, int]]:
    """扫 ``dice_ratchet/`` 下所有 ``*.py``，返回 ``[(相对路径, 名字, 行号)]``。"""
    hits: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            found = undefined_globals(src, filename=str(path))
        except SyntaxError as exc: # pragma: no cover - 语法错由 compileall/pytest 先拦
            hits.append((str(path.relative_to(root)), f"<SyntaxError: {exc.msg}>", 0))
            continue
        for name, line in found:
            hits.append((str(path.relative_to(root)), name, line))
    return hits


def test_scanner_catches_the_historical_f32_pattern():
    """反例自检：F32 的原写法必须被扫出来（否则这个守卫是空转的）。"""
    src = (
        "def f(run_ctx):\n"
        "    return getattr(run_ctx, 'harness_dir', default_harness_dir())\n"
    )
    assert [name for name, _line in undefined_globals(src)] == ["default_harness_dir"]

    # 反例 2：F33 的原写法（模块级只导 read_jsonl）
    src2 = (
        "from dice_ratchet.infra.io import read_jsonl\n"
        "def g(base):\n"
        "    return read_json(base)\n"
    )
    assert [name for name, _line in undefined_globals(src2)] == ["read_json"]

    # 正例：模块级导入后不应命中
    ok = (
        "from dice_ratchet.infra.io import read_json\n"
        "def g(base):\n"
        "    return read_json(base)\n"
    )
    assert undefined_globals(ok) == []


def test_no_undefined_global_names_in_package(repo_root):
    """``dice_ratchet/`` 里不得存在「函数引用了模块级未定义的名字」（F32/F33 的缺陷类）。"""
    hits = _scan_package(Path(repo_root) / "dice_ratchet")
    assert not hits, (
        "以下位置在函数里引用了模块级未定义的名字（运行时必 NameError；"
        "通常是「测试永远不走的死分支」，请在模块级导入或定义）：\n"
        + "\n".join(f"  {path}:{line}  {name}" for path, name, line in hits))
