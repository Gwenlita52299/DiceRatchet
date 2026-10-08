"""MOD-04 LLM 输出解析与三级校验（PRD FR-04-7/8、§6、§11 鲁棒性矩阵）。

三级校验的唯一口径（Spec §4.3）：

- **① 语法**（本模块）：``json.loads``（含单层围栏剥离）+ ``schemas/decision.schema.json``
  结构校验 + ``kind`` 必填字段代码表。失败 → ``E_ACTION_PARSE`` / ``E_ACTION_SCHEMA``。
- **② 语义**（本模块）：动作是否落在**当前** ``legal_actions()`` 内。失败 → ``E_ACTION_ILLEGAL``。
- **③ 执行**（MOD-01）：``step()`` 的 ``info["illegal"]``。本模块只消费，不改判。

纯函数、无 IO、无随机（NFR-04-1）；本模块**不**触碰环境（INV-1）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

from dice_ratchet.infra.errors import E
from dice_ratchet.sandbox.projection import canonical_json

__all__ = [
    "REQUIRED_FIELDS", "OPTIONAL_FIELDS", "ALLOWED_FIELDS", "ParseResult",
    "parse_llm_output", "validate_syntax", "validate_schema", "validate_semantic",
    "semantic_match", "canonical_action", "strip_fence", "decision_schema_dict",
    "allowed_kinds",
]

#: 每个 ``kind`` 的必填字段（PRD §6 代码表；schema 只声明类型，必填关系在 ① 级校验）。
REQUIRED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "play_face": ("dieId", "slotId"),
    "discard_die": ("dieId",),
    "purify": ("dieId",),
    "shop_remove": ("dieId",),
    "use_ability": ("abilityId",),
    "map_choose": ("nodeId",),
    "shop_buy": ("itemId",),
    "event_choose": ("optionId",),
    "shrine_choose": ("optionId",),
    "rest_choose": ("optionId",),
    "treasure_pick": ("defId",),
    "forge_pick": ("dieId", "faceIndex", "candidateId"),
    "relic_swap": ("relicId",),
    "end_turn": (),
    "shop_refresh": (),
    "shop_leave": (),
}

#: 每个 ``kind`` 的登记可选字段（仅用于诊断信息，不构成拒绝理由）。
OPTIONAL_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "play_face": ("faceIndex", "targetId"),
    "discard_die": (),
    "purify": (),
    "shop_remove": (),
    "use_ability": ("dieId", "value"),
    "map_choose": (),
    "shop_buy": (),
    "event_choose": (),
    "shrine_choose": (),
    "rest_choose": (),
    "treasure_pick": (),
    "forge_pick": (),
    "relic_swap": (),
    "end_turn": (),
    "shop_refresh": (),
    "shop_leave": (),
}

#: ``schemas/decision.schema.json`` 允许的全部动作字段（语义比对时只看这些键）。
_DECLARED_FIELDS: Final[tuple[str, ...]] = (
    "kind", "dieId", "slotId", "faceIndex", "targetId", "abilityId", "value",
    "nodeId", "itemId", "optionId", "defId", "candidateId", "relicId",
)
ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(_DECLARED_FIELDS)

_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*```[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)\r?\n?```[ \t]*$",
    re.DOTALL)

_SCHEMA_CACHE: dict[str, Any] = {}


def _reject_constant(token: str) -> Any:
    """``NaN`` / ``Infinity`` 不是合法 JSON（§11 矩阵第 5 行）→ 直接判语法失败。"""
    raise ValueError(f"非法 JSON 常量 {token!r}")


def decision_schema_dict() -> dict[str, Any]:
    """唯一决策 schema（与 MOD-03 共用 ``schemas/decision.schema.json``）。"""
    if "schema" not in _SCHEMA_CACHE:
        from dice_ratchet.inference.schema import decision_schema

        _SCHEMA_CACHE["schema"] = decision_schema()
    return _SCHEMA_CACHE["schema"]


def allowed_kinds() -> tuple[str, ...]:
    """``kind`` 枚举（逐字取自共享 schema，避免代码表与 schema 分叉）。"""
    schema = decision_schema_dict()
    items = schema.get("properties", {}).get("plan", {}).get("items", {})
    kinds = items.get("properties", {}).get("kind", {}).get("enum", [])
    return tuple(str(k) for k in kinds)


@dataclass(frozen=True)
class ParseResult:
    """一次解析的结论（PRD §6）。

    ``level`` 取 ``syntax`` / ``schema`` / ``semantic`` / ``ok``——``schema`` 是 §11 鲁棒性
    矩阵的行级口径（PRD §6 注释只列了 syntax/semantic/ok，本模块按矩阵补齐）。
    """

    ok: bool
    action: dict[str, Any] | None
    reasoning: str
    level: str
    error_code: str | None = None
    error_detail: str | None = None
    warnings: list[str] = field(default_factory=list)
    plan: tuple[dict[str, Any], ...] = ()

    @property
    def actions(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self.plan]


# ----------------------------------------------------------------------
# ① 语法
# ----------------------------------------------------------------------

def strip_fence(text: str) -> tuple[str, str | None]:
    """剥掉**一层** Markdown 围栏（§11 矩阵第 2 行）。

    两层及以上、或围栏外夹散文 → 返回错误（由调用方判 ``E_ACTION_PARSE``）。
    """
    match = _FENCE_RE.match(text)
    if match is None:
        return text, None
    body = match.group("body")
    if body.lstrip().startswith("```"):
        return body, "多层代码围栏（只允许剥一层）"
    return body, None


def validate_syntax(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """``json.loads`` + 单层围栏剥离；失败返回 ``(None, detail)`` → ``E_ACTION_PARSE``。"""
    if not isinstance(raw, str):
        return None, f"LLM 原始输出类型非法：{type(raw).__name__}"
    text = raw.strip()
    if not text:
        return None, "空输出（无任何字符）"
    text, fence_error = strip_fence(text)
    if fence_error is not None:
        return None, fence_error
    text = text.strip()
    if not text:
        return None, "围栏内为空"
    try:
        payload = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"json.loads 失败：{exc}"
    if not isinstance(payload, dict):
        return None, f"顶层必须是 JSON 对象，实际 {type(payload).__name__}"
    return payload, None


# ----------------------------------------------------------------------
# ① 结构 / Schema + kind 必填字段
# ----------------------------------------------------------------------

def _validator(reasoning_max_chars: int):
    """构造缓存的 jsonschema 校验器；``reasoning`` 的 maxLength 交由本模块按配置截断。"""
    from jsonschema import Draft202012Validator

    key = f"v{int(reasoning_max_chars)}"
    cached = _SCHEMA_CACHE.get(key)
    if cached is not None:
        return cached
    schema = json.loads(json.dumps(decision_schema_dict()))
    properties = schema.get("properties", {})
    reasoning = properties.get("reasoning")
    if isinstance(reasoning, dict):
        # MOD-03 用 schema 的 maxLength 做 L0 约束；MOD-04 侧按 policy.reasoning_max_chars
        # 截断（§11 矩阵第 10 行），因此这里放宽，不把超长 reasoning 判成 schema 失败。
        reasoning.pop("maxLength", None)
    validator = Draft202012Validator(schema)
    _SCHEMA_CACHE[key] = validator
    return validator


def _first_error(payload: dict[str, Any], *, reasoning_max_chars: int) -> str | None:
    errors = sorted(_validator(reasoning_max_chars).iter_errors(payload),
                    key=lambda err: (list(err.absolute_path), err.message))
    if not errors:
        return None
    err = errors[0]
    path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                         for p in err.absolute_path)
    return f"{path}: {err.message}"


def _check_required_fields(plan: Sequence[Any]) -> str | None:
    for index, item in enumerate(plan):
        if not isinstance(item, Mapping):
            return f"$.plan[{index}]: 动作必须是对象"
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in REQUIRED_FIELDS:
            return f"$.plan[{index}].kind: 未知动作类型 {kind!r}"
        missing = [name for name in REQUIRED_FIELDS[kind] if name not in item]
        if missing:
            return f"$.plan[{index}]: kind={kind} 缺少必填字段 {missing}"
    return None


def _normalize_plan_nulls(payload: Mapping[str, Any]) -> dict[str, Any]:
    """去掉计划项里**显式 null** 的可选字段（GAME D-19：缺省由内核规范化）。

    GAME ``legal_actions()`` 会稳定地输出 ``targetId: null`` 这类"无目标"占位；模型逐字
    照抄时 ``null`` 会撞上 ``decision.schema.json`` 的 ``type: string``。语义上二者等价，
    统一在 ① 级归一到"省略该键"。
    """
    out: dict[str, Any] = dict(payload)
    plan = out.get("plan")
    if isinstance(plan, list):
        normalized: list[Any] = []
        for item in plan:
            if isinstance(item, Mapping):
                normalized.append({key: value for key, value in item.items()
                                   if value is not None})
            else:
                normalized.append(item)
        out["plan"] = normalized
    return out


def _validate_schema(payload: dict[str, Any], *, phase: str = "",
                     reasoning_max_chars: int = 400, max_plan_len: int = 12
                     ) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    payload = _normalize_plan_nulls(payload)
    error = _first_error(payload, reasoning_max_chars=reasoning_max_chars)
    if error is not None:
        return None, error, []
    plan = payload.get("plan")
    if not isinstance(plan, list):
        return None, "$.plan: 必须是数组", []
    error = _check_required_fields(plan)
    if error is not None:
        return None, error, []
    if len(plan) > int(max_plan_len):
        return None, f"$.plan: 长度 {len(plan)} 超过 policy.max_plan_len={max_plan_len}", []
    if phase and phase != "player_turn" and len(plan) != 1:
        return None, (f"$.plan: phase={phase} 只允许 1 个动作，实际 {len(plan)} 个"), []

    warnings: list[str] = []
    decision = dict(payload)
    reasoning = str(decision.get("reasoning") or "")
    if len(reasoning) > int(reasoning_max_chars):
        decision["reasoning"] = reasoning[: int(reasoning_max_chars)]
        warnings.append(f"reasoning 超长已截断（>{reasoning_max_chars} 字符）")
    decision["plan"] = [
        {key: value for key, value in item.items() if key in ALLOWED_FIELDS}
        for item in plan
    ]
    return decision, None, warnings


def validate_schema(payload: dict[str, Any], **options: Any
                    ) -> tuple[dict[str, Any] | None, str | None]:
    """公开的 ① 级结构校验（PRD §6 签名）；返回 ``(decision, detail)``。

    ``decision`` 是规范化后的决策对象（``{"reasoning": str, "plan": [...]}``），失败时
    第二项为可读 detail、第一项为 ``None``。更细的诊断（warning）由
    ``parse_llm_output`` 暴露。
    """
    decision, error, _ = _validate_schema(payload, **options)
    return decision, error


# ----------------------------------------------------------------------
# ② 语义（集合成员判定）
# ----------------------------------------------------------------------

def canonical_action(action: Mapping[str, Any]) -> str:
    """动作的规范化字符串（复用 MOD-01 ``stable_stringify``，与 JS 逐位一致）。"""
    return canonical_json({str(k): v for k, v in action.items()}).decode("utf-8")


def _signature(action: Mapping[str, Any]) -> str:
    """去掉「显式 null」后的规范化签名（GAME D-19：可选字段缺省由内核补默认值）。"""
    trimmed = {str(k): v for k, v in action.items() if v is not None}
    return canonical_action(trimmed)


def semantic_match(action: Mapping[str, Any], legal_actions: Sequence[Mapping[str, Any]]
                   ) -> tuple[bool, str | None, str | None]:
    """② 级判定；返回 ``(ok, error_detail, warning_code)``。

    - 精确命中（去掉显式 null 后逐字节相等）→ ``(True, None, None)``；
    - ``play_face`` 缺 ``targetId`` 且同 ``(dieId, slotId)`` 的合法动作唯一 → 视为内核会
      规范化的等价动作，``(True, None, E_ACTION_LEGAL_NOISE)``；
    - 其余 → ``(False, detail, None)``（调用方记 ``E_ACTION_ILLEGAL``）。
    """
    if not isinstance(action, Mapping):
        return False, f"动作必须是对象，实际 {type(action).__name__}", None
    if "kind" not in action:
        return False, "动作缺少 kind", None
    target = _signature(action)
    legal_sigs = {_signature(item) for item in legal_actions if isinstance(item, Mapping)}
    if target in legal_sigs:
        return True, None, None

    if action.get("kind") == "play_face" and "targetId" not in action:
        candidates = [item for item in legal_actions
                      if isinstance(item, Mapping) and item.get("kind") == "play_face"
                      and item.get("dieId") == action.get("dieId")
                      and item.get("slotId") == action.get("slotId")]
        if len(candidates) == 1:
            return True, None, E.E_ACTION_LEGAL_NOISE
    legal_kinds = sorted({str(item.get("kind")) for item in legal_actions
                          if isinstance(item, Mapping)})
    return (False,
            f"动作不在合法集合内：{target}（当前合法 kind：{legal_kinds}）",
            None)


def validate_semantic(action: dict[str, Any], legal_actions: list[dict[str, Any]]
                      ) -> tuple[bool, str | None]:
    """② 级公开签名（PRD §6）：动作是否落在**当前** ``legal_actions()`` 内。"""
    ok, detail, _ = semantic_match(action, legal_actions)
    return ok, detail


# ----------------------------------------------------------------------
# 顶层：只做 ①（② 在执行时逐个校验，FR-04-7）
# ----------------------------------------------------------------------

def parse_llm_output(raw: str, *, phase: str = "", reasoning_max_chars: int = 400,
                     max_plan_len: int = 12) -> ParseResult:
    """解析 LLM 原始输出（① 语法 + schema + kind 必填字段）。

    语义（②）不在这里做：每个动作执行前都要拿**当前** ``legal_actions()`` 重新判定
    （FR-04-8 禁止缓存）。
    """
    payload, detail = validate_syntax(raw)
    if payload is None:
        return ParseResult(ok=False, action=None, reasoning="", level="syntax",
                           error_code=E.E_ACTION_PARSE, error_detail=detail)
    decision, error, warnings = _validate_schema(
        payload, phase=phase, reasoning_max_chars=reasoning_max_chars,
        max_plan_len=max_plan_len)
    if decision is None:
        return ParseResult(ok=False, action=None, reasoning="", level="schema",
                           error_code=E.E_ACTION_SCHEMA, error_detail=error,
                           warnings=list(warnings))
    plan = tuple(dict(item) for item in decision["plan"])
    return ParseResult(ok=True, action=dict(plan[0]) if plan else None,
                       reasoning=str(decision.get("reasoning") or ""), level="ok",
                       warnings=list(warnings), plan=plan)
