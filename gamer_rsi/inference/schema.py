"""JSON Schema 子集 → GBNF 编译器、校验与修复 prompt（PRD §2.1 / §6 / §8）。

本模块**独占拥有**三件事（Spec v1.4 B-28，MOD-04 与 harness 都不得覆盖）：

1. 唯一的决策 schema 加载（``schemas/decision.schema.json``，Spec v1.3 §4.3）；
2. 结构化输出的校验与 ``json_path`` 归一（``validate()`` / ``json_path_of()``）；
3. 修复 prompt 模板（``repair_messages()``，只纠错、零领域知识）。

``compile_grammar()`` 支持的关键字白名单见 ``_SUPPORTED_KEYWORDS``；遇到 ``allOf`` /
``if`` / ``$ref`` / ``oneOf`` 等不支持的关键字返回 ``None``（= 无 grammar 可用，
**不是错误**），自动降级 JSON mode + 运行时 ``jsonschema`` 校验。禁止静默忽略关键字
（Spec P-09 结案）——任何不认识的关键字都会让编译整体返回 ``None``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from ..infra.errors import E
from .errors import InferenceError

__all__ = [
    "DECISION_SCHEMA_PATH", "DECISION_SCHEMA_ID", "load_decision_schema", "decision_schema",
    "compile_grammar", "validate", "json_path_of", "render_error", "retry_reason",
    "repair_messages", "repair_prompt_text", "REPAIR_SYSTEM", "validate_grammar_syntax",
    "grammar_problems", "unsupported_keywords", "SUPPORTED_KEYWORDS",
]

#: 唯一决策 schema 的相对路径（Spec §4.3）。
DECISION_SCHEMA_PATH: Final[str] = "schemas/decision.schema.json"

#: 决策 schema 在埋点里的 id（``InferenceCallRecord.schema_id``）。
DECISION_SCHEMA_ID: Final[str] = "decision.v1"

#: 修复 prompt 的 system 消息（逐字取自 PRD §8；golden 快照见 tests/golden/inference/）。
REPAIR_SYSTEM: Final[str] = (
    "你是 JSON 修复器。只输出一个符合给定 JSON Schema 的 JSON 对象，"
    "不要解释，不要 markdown 代码块。"
)

# ---- 关键字白名单 ---------------------------------------------------------------

#: 允许参与编译的约束关键字（PRD §6「GBNF 支持子集」）。
SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset({
    "type", "enum", "required", "additionalProperties", "properties", "items",
    "maxLength", "minimum", "maximum", "minItems", "maxItems",
})

#: 纯注解关键字：不影响约束语义，允许出现且不参与编译。
_ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset({
    "$schema", "$id", "title", "description", "default", "examples", "$comment",
})

#: 明确不支持的关键字（出现即返回 None，绝不静默忽略）。
_UNSUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset({
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "$ref", "$defs",
    "definitions", "pattern", "format", "multipleOf", "uniqueItems", "minLength",
    "minProperties", "maxProperties", "patternProperties", "propertyNames", "const",
    "contains", "prefixItems", "unevaluatedProperties", "unevaluatedItems",
    "exclusiveMinimum", "exclusiveMaximum", "dependentRequired", "dependentSchemas",
    "dependencies", "contentEncoding", "contentMediaType", "readOnly", "writeOnly",
})

_SUPPORTED_TYPES: Final[frozenset[str]] = frozenset({
    "object", "string", "integer", "number", "boolean", "array",
})

#: GBNF 公共规则（JSON 词法；命名统一 ``j-`` 前缀，避免与 schema 生成规则冲突）。
_PRELUDE: Final[str] = (
    r'j-ws ::= ([ \t\n] j-ws)?' "\n"
    r'j-char ::= [^"\\\x7F\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F]'
    r' [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])' "\n"
    r'j-string ::= "\"" j-char* "\""' "\n"
    r'j-int ::= "-"? ([0-9] | [1-9] [0-9]*)' "\n"
    r'j-number ::= "-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?'
)

_RULE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*::=\s*(?P<body>.+)$")
_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


class _Unsupported(Exception):
    """内部信号：schema 落在支持子集之外（对外一律转成 ``compile_grammar() -> None``）。"""


# =====================================================================
# 唯一决策 schema
# =====================================================================

_DECISION_CACHE: dict[str, Any] | None = None


def decision_schema_path(root: Path | None = None) -> Path:
    """定位 ``schemas/decision.schema.json``（先看 CWD，再看仓库根）。"""
    candidates = []
    if root is not None:
        candidates.append(Path(root) / DECISION_SCHEMA_PATH)
        candidates.append(Path(root) / "decision.schema.json")
    candidates.append(Path(DECISION_SCHEMA_PATH))
    candidates.append(Path(__file__).resolve().parents[2] / DECISION_SCHEMA_PATH)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise InferenceError(
        E.E_LLM_UNAVAILABLE,
        f"找不到唯一决策 schema：{DECISION_SCHEMA_PATH}",
        hint="MOD-03 独占拥有该文件；仓库应包含 schemas/decision.schema.json（Spec §4.3）")


def load_decision_schema(path: str | Path | None = None) -> dict[str, Any]:
    """加载并做结构自检的唯一决策 schema（Spec §4.3；本模块与 MOD-04 共用）。"""
    target = Path(path) if path is not None else decision_schema_path()
    try:
        schema = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InferenceError(E.E_LLM_UNAVAILABLE, f"决策 schema 读取失败：{target}\n{exc}",
                             hint="文件必须存在且为合法 JSON") from exc
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise InferenceError(E.E_LLM_UNAVAILABLE,
                             f"决策 schema 顶层必须是 object：{target}",
                             hint="Spec §4.3 规定 DECISION_SCHEMA 是扁平、无条件关键字的对象")
    return schema


def decision_schema() -> dict[str, Any]:
    """缓存的 ``DECISION_SCHEMA``（模块级 ``__getattr__`` 也走这里）。"""
    global _DECISION_CACHE
    if _DECISION_CACHE is None:
        _DECISION_CACHE = load_decision_schema()
    return _DECISION_CACHE


def __getattr__(name: str) -> Any: # pragma: no cover - 由 import 机制触发
    """``from gamer_rsi.inference.schema import DECISION_SCHEMA`` 的惰性入口。"""
    if name == "DECISION_SCHEMA":
        return decision_schema()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# =====================================================================
# GBNF 编译
# =====================================================================

def unsupported_keywords(schema: Any) -> list[str]:
    """返回 schema 树里所有落在支持子集之外的关键字（空 = 可编译）。"""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _ANNOTATION_KEYWORDS:
                    continue
                if key not in SUPPORTED_KEYWORDS:
                    found.add(str(key))
                    continue
                if key == "additionalProperties":
                    if value is not False:
                        found.add("additionalProperties(非 false)")
                    continue
                if key == "properties":
                    if isinstance(value, dict):
                        for sub in value.values():
                            walk(sub)
                    continue
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return sorted(found)


def compile_grammar(schema: Mapping[str, Any] | None) -> str | None:
    """把 JSON Schema 子集编译为 GBNF 文本；不支持则返回 ``None``（不是错误）。

    返回的文本可被 ``llama_cpp.LlamaGrammar.from_string()`` 解析；``DECISION_SCHEMA``
    必须能编译出 grammar（由单测保证，PRD §11）。
    """
    if not isinstance(schema, Mapping):
        return None
    if unsupported_keywords(schema):
        return None
    compiler = _Compiler()
    try:
        root_expr = compiler.value(dict(schema))
    except _Unsupported:
        return None
    return compiler.render(root_expr)


class _Compiler:
    """递归下降的 JSON Schema → GBNF 生成器（值表达式不消费尾部空白）。"""

    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self._seq = 0

    # ---- 规则簿记 ---------------------------------------------------------------
    def add_rule(self, body: str) -> str:
        self._seq += 1
        name = f"r{self._seq}"
        self.rules[name] = body
        return name

    def render(self, root_expr: str) -> str:
        lines = [f"root ::= {root_expr}"]
        lines.extend(f"{name} ::= {body}" for name, body in self.rules.items())
        lines.append(_PRELUDE)
        return "\n".join(lines) + "\n"

    # ---- 入口 -------------------------------------------------------------------
    def value(self, node: Mapping[str, Any]) -> str:
        if not isinstance(node, Mapping):
            raise _Unsupported
        if "enum" in node:
            return self._enum(node["enum"])
        node_type = node.get("type")
        if isinstance(node_type, list):
            if len(node_type) != 1:
                raise _Unsupported
            node_type = node_type[0]
        if node_type is None:
            raise _Unsupported
        builder = {
            "object": self._object, "array": self._array, "string": self._string,
            "integer": self._integer, "number": self._number,
        }.get(str(node_type))
        if builder is not None:
            return self.add_rule(builder(node))
        if node_type == "boolean":
            return '("true" | "false")'
        raise _Unsupported

    # ---- 各类型 -----------------------------------------------------------------
    @staticmethod
    def _enum(values: Any) -> str:
        if not isinstance(values, (list, tuple)) or not values:
            raise _Unsupported
        literals: list[str] = []
        for value in values:
            if isinstance(value, str):
                literals.append(_gbnf_literal(json.dumps(value, ensure_ascii=False)))
            elif isinstance(value, bool):
                literals.append(_gbnf_literal("true" if value else "false"))
            elif isinstance(value, int):
                literals.append(_gbnf_literal(str(value)))
            elif value is None:
                literals.append(_gbnf_literal("null"))
            else:
                raise _Unsupported
        return "(" + " | ".join(literals) + ")"

    def _object(self, node: Mapping[str, Any]) -> str:
        properties = node.get("properties", {})
        if not isinstance(properties, Mapping):
            raise _Unsupported
        if node.get("additionalProperties", True) is not False:
            # 未声明 additionalProperties:false 时无法既编译白名单又不静默收紧语义
            raise _Unsupported
        required = list(node.get("required", []))
        if any(name not in properties for name in required):
            raise _Unsupported
        required_kv: list[str] = []
        optional_kv: list[str] = []
        for name, sub in properties.items():
            key_literal = _gbnf_literal(json.dumps(str(name), ensure_ascii=False))
            kv = f'{key_literal} j-ws ":" j-ws {self.value(sub)}'
            (required_kv if name in required else optional_kv).append(kv)
        if not required_kv and not optional_kv:
            return '"{" j-ws "}"'
        opt_alt = "(" + " | ".join(optional_kv) + ")" if optional_kv else None
        if required_kv:
            inner = " j-ws \",\" j-ws ".join(required_kv)
            if opt_alt is not None:
                inner = f"{inner} ( j-ws \",\" j-ws {opt_alt} )*"
        else:
            inner = f"( {opt_alt} )? ( j-ws \",\" j-ws {opt_alt} )*"
        return f'"{{" j-ws {inner} j-ws "}}"'

    def _array(self, node: Mapping[str, Any]) -> str:
        items = node.get("items")
        if not isinstance(items, Mapping):
            raise _Unsupported
        min_items, max_items = node.get("minItems", 0), node.get("maxItems")
        for value in (min_items, max_items):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise _Unsupported
        min_items = int(min_items or 0)
        if max_items is not None and min_items > int(max_items):
            raise _Unsupported
        atom = f"( {self.value(items)} )"
        if max_items is None:
            inner = (atom if min_items <= 1
                     else f"{atom} ( j-ws \",\" j-ws {atom} ){{{min_items - 1},}}")
            if min_items == 0:
                inner = f"{atom} ( j-ws \",\" j-ws {atom} )*"
                inner = f"( {inner} )?"
        else:
            max_items = int(max_items)
            if min_items == 0:
                inner = f"( {atom} ( j-ws \",\" j-ws {atom} ){{0,{max_items - 1}}} )?"
            else:
                inner = f"{atom} ( j-ws \",\" j-ws {atom} )" \
                        f"{{{min_items - 1},{max_items - 1}}}"
        return f'"[" j-ws {inner} j-ws "]"'

    @staticmethod
    def _string(node: Mapping[str, Any]) -> str:
        max_length = node.get("maxLength")
        if max_length is None:
            return "j-string"
        if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 0:
            raise _Unsupported
        return f'"\\"" j-char{{0,{max_length}}} "\\""'

    @staticmethod
    def _integer(node: Mapping[str, Any]) -> str:
        minimum, maximum = node.get("minimum"), node.get("maximum")
        if minimum is None and maximum is None:
            return "j-int"
        if isinstance(minimum, bool) or isinstance(maximum, bool):
            raise _Unsupported
        if (minimum is not None and not isinstance(minimum, int)) or \
                (maximum is not None and not isinstance(maximum, int)):
            raise _Unsupported
        return _int_range_expr(int(minimum) if minimum is not None else 0,
                               int(maximum) if maximum is not None else 10 ** 18)

    @staticmethod
    def _number(node: Mapping[str, Any]) -> str:
        minimum, maximum = node.get("minimum"), node.get("maximum")
        if minimum is None and maximum is None:
            return "j-number"
        if isinstance(minimum, bool) or isinstance(maximum, bool) or \
                not isinstance(minimum, int) or not isinstance(maximum, int):
            raise _Unsupported
        return f"( {_int_range_expr(minimum, maximum)} ( \".\" [0-9]+ )? )"


def _gbnf_literal(text: str) -> str:
    """把一段**字面量文本**编码成 GBNF 字符串字面量（含转义）。"""
    body = text.replace("\\", "\\\\").replace('"', '\\"')
    body = body.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{body}"'


def _fixed_width(lo: int, hi: int, width: int) -> str:
    """``[lo, hi]`` 内所有 ``width`` 位十进制整数的 GBNF 表达式。"""
    if width == 0:
        return '""'
    s_lo, s_hi = str(lo).zfill(width), str(hi).zfill(width)
    if s_lo[0] == s_hi[0]:
        if width == 1:
            return f'"{s_lo}"'
        return f'"{s_lo[0]}" {_fixed_width(lo % 10 ** (width - 1), hi % 10 ** (width - 1), width - 1)}'
    alternatives = []
    for digit in range(int(s_lo[0]), int(s_hi[0]) + 1):
        if width == 1:
            alternatives.append(f'"{digit}"')
            continue
        low = lo % 10 ** (width - 1) if digit == int(s_lo[0]) else 0
        high = hi % 10 ** (width - 1) if digit == int(s_hi[0]) else 10 ** (width - 1) - 1
        alternatives.append(f'"{digit}" {_fixed_width(low, high, width - 1)}')
    return "( " + " | ".join(alternatives) + " )"


def _int_range_expr(lo: int, hi: int) -> str:
    """``[lo, hi]`` 的十进制整数（非负）GBNF 表达式；负数区间整体加 ``"-"``。"""
    if lo < 0 and hi > 0:
        raise _Unsupported
    if hi < 0:
        return f'"-" {_int_range_expr(-hi, -lo)}'
    if lo == hi:
        return f'"{lo}"'
    parts: list[str] = []
    for width in range(len(str(lo)), len(str(hi)) + 1):
        group_lo = max(lo, 0 if width == 1 else 10 ** (width - 1))
        group_hi = min(hi, 10 ** width - 1)
        if group_lo > group_hi:
            continue
        parts.append(_fixed_width(group_lo, group_hi, width))
    if not parts: # pragma: no cover - lo<=hi 时必有解
        raise _Unsupported
    return parts[0] if len(parts) == 1 else "( " + " | ".join(parts) + " )"


# =====================================================================
# GBNF 语法自检（不依赖 llama_cpp；CI 无 llama-cpp-python 时仍可守门）
# =====================================================================

def _split_rules(grammar: str) -> tuple[dict[str, str], list[str]]:
    rules: dict[str, str] = {}
    problems: list[str] = []
    for lineno, raw in enumerate(grammar.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _RULE_RE.match(line)
        if not match:
            problems.append(f"第 {lineno} 行不是合法的规则定义：{line!r}")
            continue
        name = match.group("name")
        if name in rules:
            problems.append(f"规则 {name} 重复定义（第 {lineno} 行）")
        rules[name] = match.group("body")
    return rules, problems


def _strip_literals(body: str) -> str:
    """剥掉字符串字面量与字符类，只留结构（用于标识符引用检查）。"""
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == '"':
            index += 1
            while index < len(body) and body[index] != '"':
                index += 1 if body[index] != "\\" else 2
            index += 1
            out.append(" ")
        elif char == "[":
            index += 1
            while index < len(body) and body[index] != "]":
                index += 1 if body[index] != "\\" else 2
            index += 1
            out.append(" ")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def grammar_problems(grammar: str) -> list[str]:
    """检查 GBNF 文本的结构合法性（规则齐备、引用已定义、括号/字面量配平）。"""
    rules, problems = _split_rules(grammar)
    if "root" not in rules:
        problems.append("缺少 root 规则")
    for name, body in rules.items():
        stripped = _strip_literals(body)
        if stripped.count("(") != stripped.count(")"):
            problems.append(f"规则 {name} 的括号不配平")
        problems.extend(f"规则 {name} 的{p}" for p in _unbalanced_literals(body))
        for token in _IDENT_RE.findall(stripped):
            if token not in rules:
                problems.append(f"规则 {name} 引用了未定义规则 {token}")
    return problems


def _unbalanced_literals(body: str) -> list[str]:
    """检测未闭合的字符串字面量 / 字符类（``_strip_literals`` 会吞掉它们）。"""
    problems: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char in ('"', "["):
            closing = '"' if char == '"' else "]"
            label = "字符串字面量不配平" if char == '"' else "字符类不配平"
            index += 1
            while index < len(body) and body[index] != closing:
                index += 1 if body[index] != "\\" else 2
            if index >= len(body):
                problems.append(label)
                return problems
        index += 1
    return problems


def validate_grammar_syntax(grammar: str) -> None:
    """结构自检；失败抛 ``ValueError``（``llama_cpp`` 不可用时的替代守门）。"""
    problems = grammar_problems(grammar)
    if problems:
        raise ValueError("非法 GBNF：\n" + "\n".join(problems))


# =====================================================================
# 校验与 json_path
# =====================================================================

def _validator_for(schema: Mapping[str, Any]) -> Any:
    from jsonschema import validators

    validator_cls = validators.validator_for(dict(schema))
    validator_cls.check_schema(dict(schema))
    return validator_cls(dict(schema))


def validate(parsed: Any, schema: Mapping[str, Any]) -> None:
    """按 JSON Schema 校验；失败抛 ``jsonschema.ValidationError``（带 ``json_path``）。"""
    _validator_for(schema).validate(parsed)


def json_path_of(error: BaseException) -> str:
    """异常 → ``$.plan[0].kind`` 形态的路径（``ValidationError`` / ``JSONDecodeError``）。"""
    path = getattr(error, "absolute_path", None)
    if path is not None:
        out = "$"
        for part in path:
            out += f"[{part}]" if isinstance(part, int) else f".{part}"
        return out
    return "$"


def render_error(error: BaseException | str) -> str:
    """异常 → 单行可读信息（**不含** json_path；路径由 ``repair_prompt_text`` 追加）。"""
    if isinstance(error, str):
        return error
    message = getattr(error, "message", None) or str(error)
    if error.__class__.__name__ == "JSONDecodeError":
        return f"JSONDecodeError: {getattr(error, 'msg', message)} " \
               f"(line {getattr(error, 'lineno', '?')} col {getattr(error, 'colno', '?')})"
    if error.__class__.__name__ == "ValidationError":
        return f"{message} (validator={getattr(error, 'validator', '?')})"
    return message


def retry_reason(error: BaseException) -> str:
    """``retry_reasons`` 的单条口径：``ValidationError:$.plan[0].kind``（PRD §6/§8）。"""
    return f"{type(error).__name__}:{json_path_of(error)}"


# =====================================================================
# 修复 prompt（本模块独占拥有，Spec v1.4 B-28）
# =====================================================================

def repair_prompt_text(bad: str, error: BaseException | str, schema: Mapping[str, Any]) -> str:
    """渲染修复轮次的 user 消息（纯函数，便于 golden 快照）。"""
    schema_json = json.dumps(dict(schema), ensure_ascii=False, indent=2, sort_keys=True)
    path = json_path_of(error) if isinstance(error, BaseException) else "$"
    return (
        "你的上一次输出无法通过校验。\n"
        "--- 原始输出 ---\n"
        f"{bad}\n"
        "--- 校验错误 ---\n"
        f"{render_error(error)} @ {path}\n"
        "--- 目标 Schema ---\n"
        f"{schema_json}\n"
        "请输出修正后的 JSON。"
    )


def repair_messages(messages: Sequence[Any], *, bad: str,
                    error: BaseException | str, schema: Mapping[str, Any]) -> list[Any]:
    """在原始 ``messages`` 之后追加一轮「system + user」修复对话。

    不可变契约：**不修改**入参 ``messages``（返回新 list，PRD §11 单测）。
    ``Message`` 从 ``backend.py`` 延迟导入，避免 backend → schema → backend 的循环 import。
    """
    from .backend import Message # 延迟导入：解除模块级循环依赖

    repaired = list(messages)
    repaired.append(Message(role="system", content=REPAIR_SYSTEM))
    repaired.append(Message(role="user",
                            content=repair_prompt_text(bad, error, schema)))
    return repaired
