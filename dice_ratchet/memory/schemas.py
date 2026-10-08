"""MOD-05 数据类 + JSON Schema + round-trip（FR-05-1 / G5，PRD §6.2/§6.6）。

设计要点（与 PRD §6 逐条对齐）：

1. **唯一事实来源**：本模块的 ``MEMORY_ENTRY_SCHEMA`` / ``REFLECTION_ENTRY_SCHEMA`` 是
   schema 的权威定义；``schemas/memory_entry.schema.json`` 等文件由
   ``tests/unit/memory/test_schemas.py`` 断言逐字段相等（防漂移）。
2. **round-trip 字节相同**：``from_json`` 只做校验与重构，``to_json`` 按固定字段顺序 +
   ``separators=(",", ":")`` + ``ensure_ascii=False`` 输出；浮点一律 ``round(x, 6)``。
3. **确定性（G3）**：``created_at`` 只作审计展示，**不计入** ``content_sha256``，
   也不参与打分；本模块不读墙钟（时间只来自调用方传入的 ``created_at`` / ``tick``）。
4. ``additionalProperties: false``：未知字段在 ``from_json`` 时即拒绝。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Mapping, Sequence

from dice_ratchet.infra.errors import E, InfraError

__all__ = [
    "ENTRY_SCHEMA_VERSION", "STORE_SCHEMA_VERSION", "MEMORY_SCHEMA_VERSION",
    "MEMORY_TAGS", "GAME_PHASES", "STAGE_BUCKETS", "VIRTUE_BUCKETS", "POLARITIES",
    "ENTRY_STATUSES", "FAILURE_CAUSES", "ACTION_KINDS", "COUNTER_ARROWS",
    "MEMORY_ENTRY_SCHEMA", "REFLECTION_ENTRY_SCHEMA", "ADD_RESULT_SCHEMA",
    "RETRIEVE_RESULT_SCHEMA", "RETRIEVED_MEMORY_SCHEMA", "DROPPED_ENTRY_SCHEMA",
    "SITUATION_SCHEMA",
    "canonical_json", "content_sha256_of", "stable_id", "round6",
    "SituationKey", "MemoryEntry", "ReflectionEntry", "Provenance", "MemoryCandidate",
    "AddResult", "DroppedEntry", "RetrievedMemory", "RetrieveResult", "StoreStats",
    "VerifyReport", "RollbackReport", "MemoryViewProtocol",
    "validate_memory_entry", "validate_reflection_entry",
]

#: ``MemoryEntry.schema_version`` 的取值（PRD §6.6 的 ``const``）。
ENTRY_SCHEMA_VERSION: Final[str] = "1.1"
#: 存储层 schema 版本（快照 header 的 ``store_schema_version``）。
STORE_SCHEMA_VERSION: Final[str] = "1.0"
#: PRD §6.2 的模块常量（历史别名，等价于 ``STORE_SCHEMA_VERSION``）。
MEMORY_SCHEMA_VERSION: Final[str] = STORE_SCHEMA_VERSION

#: ``MemoryEntry.tags`` 的受控取值（PRD §6.6）。
MEMORY_TAGS: Final[tuple[str, ...]] = (
    "combat", "low_virtue", "defense", "corruption", "school_counter", "target_choice",
    "map_route", "shop", "event", "forge", "relic", "elite", "boss", "status_effect",
    "end_turn", "ability",
)
GAME_PHASES: Final[tuple[str, ...]] = (
    "map", "player_turn", "shop", "event", "treasure", "shrine", "forge", "rest",
    "relic_swap",
)
STAGE_BUCKETS: Final[tuple[str, ...]] = ("1-2", "3-4", "5-6", "7+")
VIRTUE_BUCKETS: Final[tuple[str, ...]] = ("critical", "low", "mid", "high")
POLARITIES: Final[tuple[str, ...]] = ("do", "avoid")
ENTRY_STATUSES: Final[tuple[str, ...]] = ("active", "evicted", "superseded", "quarantined")
COUNTER_ARROWS: Final[tuple[str, ...]] = ("up", "down", "eq")
FAILURE_CAUSES: Final[tuple[str, ...]] = (
    "none", "illegal_action", "low_damage", "wrong_target", "virtue_management",
    "corruption_misuse", "counter_ignored", "route_choice", "economy", "policy_failure",
    "unknown",
)
#: decision schema 的 action kind 全集（``schemas/decision.schema.json``）。
ACTION_KINDS: Final[tuple[str, ...]] = (
    "play_face", "discard_die", "purify", "use_ability", "end_turn", "map_choose",
    "shop_buy", "shop_refresh", "shop_remove", "shop_leave", "event_choose",
    "treasure_pick", "forge_pick", "shrine_choose", "rest_choose", "relic_swap",
)

_JSON_SEP: Final[tuple[str, str]] = (",", ":")
_FLOAT_NDIGITS: Final[int] = 6


# =====================================================================
# 通用确定性工具
# =====================================================================

def round6(value: float) -> float:
    """确定性浮点：一律 ``round(x, 6)``（PRD §6.3 的 ``round(S, 6)`` 同口径）。"""
    return round(float(value), _FLOAT_NDIGITS)


def canonical_json(obj: Any) -> str:
    """规范化 JSON（键排序、无空白、UTF-8 原样）：一切哈希的输入形式。"""
    return json.dumps(obj, ensure_ascii=False, separators=_JSON_SEP, sort_keys=True,
                      default=str)


def content_sha256_of(payload: Mapping[str, Any]) -> str:
    """对规范化 JSON 求 sha256（快照 ``content_sha256`` 与条目 ``content_sha256`` 同口径）。"""
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any, width: int = 12) -> str:
    """确定性 id：``sha256(规范化 parts)[:width]``。"""
    digest = content_sha256_of({"parts": [_normalize_part(p) for p in parts]})
    return f"{prefix}-{digest[:width]}"


def _normalize_part(part: Any) -> Any:
    if isinstance(part, (tuple, list)):
        return [_normalize_part(p) for p in part]
    if isinstance(part, Mapping):
        return {str(k): _normalize_part(v) for k, v in part.items()}
    if isinstance(part, float):
        return round6(part)
    return part


def _validate(schema: Mapping[str, Any], obj: Mapping[str, Any], *, name: str,
              code: E) -> None:
    """按已编译的 validator 校验（NFR-05-2：2000 条全量加载 ≤ 2 s）。

    ``jsonschema.validate()`` 每次都会重新检查 schema 本身（数百微秒/条），加载 2000 行
    时会突破启动预算；这里缓存 ``Draft202012Validator`` 实例，只做实例校验。
    """
    validator = _validator_for(schema, name)
    try:
        validator.validate(dict(obj))
    except Exception as exc:  # noqa: BLE001 - jsonschema.ValidationError / SchemaError
        path = getattr(exc, "json_path", "$")
        message = getattr(exc, "message", str(exc))
        raise InfraError(code, f"{name} schema 校验失败 at {path}: {message}") from exc


_VALIDATOR_CACHE: dict[str, Any] = {}

# ---- 加载期快速校验（NFR-05-2：2000 条全量加载 ≤ 2 s）-------------------------
#
# ``Draft202012Validator`` 虽然已缓存，但每次 ``validate()`` 仍要递归下钻整份 schema：
# 2000 行实测 ~235 ms（占启动加载 268 ms 的 88%，CI 上直接顶穿 2 s 预算）。这里按同一份
# schema 生成「检查计划」并直接执行——语义覆盖类型 / required / 未知字段 / enum / pattern /
# items，但不走 jsonschema 的递归分派。**仅用于加载本进程写出的行**：写入路径仍由
# ``MemoryEntry.from_json`` 调 ``validate_memory_entry`` 做全量 schema 校验。
_PLAN: Final[dict[str, Any]] = {}

# ``const`` 未声明时的哨兵（``None`` 是合法 const 值，不能拿它当缺省）。
_MISSING: Final[Any] = object()

_TYPE_CHECKS: Final[dict[str, Any]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _build_plan(schema: Mapping[str, Any]) -> dict[str, Any]:
    """JSON Schema → 快速校验计划（只覆盖本模块用到的关键字）。"""
    props = {str(k): _build_plan(v) for k, v in (schema.get("properties") or {}).items()}
    declared = schema.get("type")
    if isinstance(declared, str):  # ``"type": "object"`` 与 ``["object"]`` 等价
        types: tuple[str, ...] = (declared,)
    else:
        types = tuple(declared or ())
    return {
        "types": types,
        "enum": None if schema.get("enum") is None else tuple(schema["enum"]),
        "const": schema.get("const", _MISSING),
        "pattern": schema.get("pattern"),
        "required": tuple(schema.get("required") or ()),
        "props": props,
        "items": (_build_plan(schema["items"]) if isinstance(schema.get("items"), Mapping)
                  else None),
        "extra": schema.get("additionalProperties"),
        "minimum": schema.get("minimum"),
        "maximum": schema.get("maximum"),
        "min_items": schema.get("minItems"),
        "max_items": schema.get("maxItems"),
        "min_length": schema.get("minLength"),
        "max_length": schema.get("maxLength"),
    }


def _fail_fast(name: str, path: str, message: str, code: E) -> None:
    raise InfraError(code, f"{name} schema 校验失败 at {path}: {message}")


def _check_fast(plan: Mapping[str, Any], value: Any, *, name: str, code: E,
                path: str = "$") -> None:
    types = plan["types"]
    if types and not any(_TYPE_CHECKS[t](value) for t in types):
        _fail_fast(name, path, f"{value!r} is not of type {list(types)}", code)
    if plan["const"] is not _MISSING and value != plan["const"]:
        _fail_fast(name, path, f"{plan['const']!r} was expected", code)
    if plan["enum"] is not None and value not in plan["enum"]:
        _fail_fast(name, path, f"{value!r} is not one of {list(plan['enum'])}", code)
    if plan["pattern"] is not None and isinstance(value, str) \
            and not re.search(plan["pattern"], value):
        _fail_fast(name, path, f"{value!r} does not match {plan['pattern']!r}", code)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if plan["minimum"] is not None and value < plan["minimum"]:
            _fail_fast(name, path, f"{value!r} is less than the minimum {plan['minimum']}",
                       code)
        if plan["maximum"] is not None and value > plan["maximum"]:
            _fail_fast(name, path,
                       f"{value!r} is greater than the maximum {plan['maximum']}", code)
    if isinstance(value, str):
        if plan["min_length"] is not None and len(value) < plan["min_length"]:
            _fail_fast(name, path, f"{value!r} is too short", code)
        if plan["max_length"] is not None and len(value) > plan["max_length"]:
            _fail_fast(name, path, f"{value!r} is too long", code)
    if isinstance(value, Mapping) and plan["props"]:
        missing = set(plan["required"]) - set(value)
        if missing:
            _fail_fast(name, path, f"{sorted(missing)!r} are required properties", code)
        if plan["extra"] is False:
            extra = [k for k in value if k not in plan["props"]]
            if extra:
                _fail_fast(name, path,
                           f"Additional properties are not allowed ({extra!r} unexpected)",
                           code)
        for key, item in value.items():
            sub = plan["props"].get(key)
            if sub is not None:
                _check_fast(sub, item, name=name, code=code, path=f"{path}.{key}")
    elif isinstance(value, list):
        if plan["min_items"] is not None and len(value) < plan["min_items"]:
            _fail_fast(name, path, f"{value!r} is too short", code)
        if plan["max_items"] is not None and len(value) > plan["max_items"]:
            _fail_fast(name, path, f"{value!r} is too long", code)
        if plan["items"] is not None:
            for index, item in enumerate(value):
                _check_fast(plan["items"], item, name=name, code=code,
                            path=f"{path}[{index}]")


def validate_memory_entry_fast(obj: Mapping[str, Any]) -> None:
    """``MemoryEntry`` 行的加载期快速校验（语义同 ``validate_memory_entry``，更快）。"""
    plan = _PLAN.get("memory_entry")
    if plan is None:
        plan = _build_plan(MEMORY_ENTRY_SCHEMA)
        _PLAN["memory_entry"] = plan
    _check_fast(plan, obj, name="memory_entry", code=E.E_IO_MEMORY_CORRUPT)


def _validator_for(schema: Mapping[str, Any], key: str) -> Any:
    cached = _VALIDATOR_CACHE.get(key)
    if cached is not None:
        return cached
    import jsonschema  # 延迟导入（与 infra.io 同策略：轻路径不触发）

    validator = jsonschema.Draft202012Validator(dict(schema))
    _VALIDATOR_CACHE[key] = validator
    return validator


# =====================================================================
# JSON Schema（draft 2020-12）
# =====================================================================

SITUATION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["phase", "stage_bucket", "virtue_bucket", "hearts_left", "hand_count"],
    "properties": {
        "phase": {"enum": list(GAME_PHASES)},
        "stage_bucket": {"enum": list(STAGE_BUCKETS)},
        "node_kind": {"type": ["string", "null"]},
        "virtue_bucket": {"enum": list(VIRTUE_BUCKETS)},
        "hearts_left": {"type": "integer", "minimum": 0},
        "enemy_school": {"type": ["string", "null"]},
        "intent_kind": {"type": ["string", "null"]},
        "counter_arrow": {"enum": ["up", "down", "eq", None]},
        "hand_count": {"type": "integer", "minimum": 0, "maximum": 8},
        "corrupt_count": {"type": "integer", "minimum": 0},
        "slot_kinds": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
}

MEMORY_ENTRY_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/memory_entry.schema.json",
    "title": "MemoryEntry",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "entry_id", "schema_version", "created_at", "created_tick", "expires_tick",
        "ttl_ticks", "run_id", "generation", "harness_version", "source_episode_id",
        "source_reflection_id", "situation", "situation_key", "tags", "lesson",
        "polarity", "confidence", "n_ref", "n_adopted", "n_win_adopted",
        "n_not_adopted", "n_win_not_adopted", "utility", "status", "text_hash",
        "content_sha256",
    ],
    "properties": {
        "entry_id": {"type": "string", "pattern": "^mem-[0-9a-f]{12}$"},
        "schema_version": {"const": ENTRY_SCHEMA_VERSION},
        "created_at": {"type": "string", "format": "date-time"},
        "created_tick": {"type": "integer", "minimum": 0},
        "expires_tick": {"type": "integer", "minimum": 0},
        "ttl_ticks": {"type": "integer", "minimum": 1},
        "run_id": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
        "harness_version": {"type": "integer", "minimum": 0},
        "source_episode_id": {"type": "string"},
        "source_reflection_id": {"type": "string"},
        "situation": SITUATION_SCHEMA,
        "situation_key": {"type": "string", "pattern": "^sk-[0-9a-f]{8}$"},
        "tags": {"type": "array", "maxItems": 8, "items": {"enum": list(MEMORY_TAGS)}},
        "lesson": {"type": "string", "minLength": 1, "maxLength": 240},
        "action_hint": {"type": ["object", "null"]},
        "polarity": {"enum": list(POLARITIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "last_ref_tick": {"type": ["integer", "null"]},
        "n_ref": {"type": "integer", "minimum": 0},
        "n_adopted": {"type": "integer", "minimum": 0},
        "n_win_adopted": {"type": "integer", "minimum": 0},
        "n_not_adopted": {"type": "integer", "minimum": 0},
        "n_win_not_adopted": {"type": "integer", "minimum": 0},
        "utility": {"type": "number", "minimum": -1, "maximum": 1},
        "status": {"enum": list(ENTRY_STATUSES)},
        "supersedes": {"type": ["string", "null"]},
        "superseded_by": {"type": ["string", "null"]},
        "conflict_group": {"type": ["string", "null"]},
        "merged_from": {"type": "array", "items": {"type": "string"}},
        "embedding": {"type": ["array", "null"], "items": {"type": "number"}},
        "text_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}

REFLECTION_ENTRY_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/reflection_entry.schema.json",
    "title": "ReflectionEntry",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reflection_id", "schema_version", "created_at", "run_id", "episode_id", "seed",
        "split", "generation", "harness_version", "tick", "text", "outcome",
        "stages_cleared", "hearts_left", "battles_won", "decisions", "illegal_attempts",
        "fallback_decisions", "failure_cause", "tags", "extracted_memory_ids", "scan",
    ],
    "properties": {
        "reflection_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "schema_version": {"const": ENTRY_SCHEMA_VERSION},
        "created_at": {"type": "string", "format": "date-time"},
        "run_id": {"type": "string"},
        "episode_id": {"type": "string"},
        "seed": {"type": "integer", "minimum": 0},
        "split": {"const": "train"},
        "generation": {"type": "integer", "minimum": 0},
        "harness_version": {"type": "integer", "minimum": 0},
        "tick": {"type": "integer", "minimum": 0},
        "text": {"type": "string", "minLength": 1},
        "outcome": {"enum": ["win", "lose"]},
        "truncation_reason": {"type": ["string", "null"]},
        "stages_cleared": {"type": "integer", "minimum": 0},
        "hearts_left": {"type": "integer", "minimum": 0},
        "battles_won": {"type": "integer", "minimum": 0},
        "decisions": {"type": "integer", "minimum": 0},
        "illegal_attempts": {"type": "integer", "minimum": 0},
        "fallback_decisions": {"type": "integer", "minimum": 0},
        "failure_cause": {"enum": list(FAILURE_CAUSES)},
        "tags": {"type": "array", "items": {"type": "string"}},
        "extracted_memory_ids": {"type": "array", "items": {"type": "string"}},
        "scan": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {"enum": ["pass", "quarantined"]},
                "hits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["pattern", "span"],
                        "properties": {
                            "pattern": {"type": "string"},
                            "span": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 0},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                        },
                    },
                },
            },
        },
        "llm_latency_ms": {"type": ["integer", "null"]},
        "prompt_tokens": {"type": ["integer", "null"]},
        "completion_tokens": {"type": ["integer", "null"]},
    },
}

DROPPED_ENTRY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_id", "reason"],
    "properties": {
        "entry_id": {"type": "string"},
        "reason": {
            "enum": ["below_min_score", "token_budget_exceeded", "top_k_exceeded",
                     "conflict_suppressed", "inv5_leak", "stale_harness", "expired",
                     "not_active"],
        },
        "score": {"type": "number"},
    },
}

RETRIEVED_MEMORY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_id", "score", "tokens", "rank", "text"],
    "properties": {
        "entry_id": {"type": "string"},
        "score": {"type": "number"},
        "tokens": {"type": "integer", "minimum": 0},
        "rank": {"type": "integer", "minimum": 0},
        "text": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "polarity": {"enum": list(POLARITIES)},
        "harness_version": {"type": "integer", "minimum": 0},
        "situation_key": {"type": "string"},
        "conflict_group": {"type": ["string", "null"]},
        "action_hint": {"type": ["object", "null"]},
    },
}

RETRIEVE_RESULT_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/retrieve_result.schema.json",
    "title": "RetrieveResult",
    "type": "object",
    "additionalProperties": False,
    "required": ["entries", "scores", "tokens", "dropped", "conflict_groups",
                 "elapsed_ms", "text", "obs_digest", "n_candidates"],
    "properties": {
        "entries": {"type": "array", "items": RETRIEVED_MEMORY_SCHEMA},
        "scores": {"type": "array", "items": {"type": "number"}},
        "tokens": {"type": "integer", "minimum": 0},
        "dropped": {"type": "array", "items": DROPPED_ENTRY_SCHEMA},
        "conflict_groups": {"type": "array", "items": {"type": "string"}},
        "elapsed_ms": {"type": "number", "minimum": 0},
        "text": {"type": "string"},
        "obs_digest": {"type": "string"},
        "n_candidates": {"type": "integer", "minimum": 0},
    },
}

ADD_RESULT_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://diceratchet.local/schemas/add_result.schema.json",
    "title": "AddResult",
    "type": "object",
    "additionalProperties": False,
    "required": ["added", "merged", "conflicted", "quarantined", "memory_version"],
    "properties": {
        "added": {"type": "integer", "minimum": 0},
        "merged": {"type": "integer", "minimum": 0},
        "conflicted": {"type": "integer", "minimum": 0},
        "quarantined": {"type": "integer", "minimum": 0},
        "evicted": {"type": "integer", "minimum": 0},
        "memory_version": {"type": "integer", "minimum": 0},
        "entry_ids": {"type": "array", "items": {"type": "string"}},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
}


# =====================================================================
# 数据类
# =====================================================================

@dataclass(frozen=True)
class SituationKey:
    """受控情境键：用于硬过滤、TagSim 与「同情境」加成（PRD §6.2）。

    全部字段来自 ``obs-agent-1.0.0`` 的投影字段；缺字段时取保守缺省（不抛异常），
    保证 MOD-04 在任意 phase 上调用 ``retrieve()`` 都不会失败。
    """

    phase: str = "player_turn"
    stage_bucket: str = "1-2"
    node_kind: str | None = None
    virtue_bucket: str = "mid"
    hearts_left: int = 0
    enemy_school: str | None = None
    intent_kind: str | None = None
    counter_arrow: str | None = None
    hand_count: int = 0
    corrupt_count: int = 0
    slot_kinds: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """固定键序的原始字典（``to_json`` / ``situation_key`` 的唯一输入形式）。"""
        return {
            "phase": self.phase if self.phase in GAME_PHASES else "player_turn",
            "stage_bucket": self.stage_bucket if self.stage_bucket in STAGE_BUCKETS else "1-2",
            "node_kind": self.node_kind,
            "virtue_bucket": (self.virtue_bucket if self.virtue_bucket in VIRTUE_BUCKETS
                              else "mid"),
            "hearts_left": int(self.hearts_left),
            "enemy_school": self.enemy_school,
            "intent_kind": self.intent_kind,
            "counter_arrow": (self.counter_arrow if self.counter_arrow in COUNTER_ARROWS
                              else None),
            "hand_count": int(self.hand_count),
            "corrupt_count": int(self.corrupt_count),
            "slot_kinds": list(self.slot_kinds),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SituationKey":
        raw = dict(data or {})
        slots = raw.get("slot_kinds") or ()
        return cls(
            phase=str(raw.get("phase") or "player_turn"),
            stage_bucket=str(raw.get("stage_bucket") or "1-2"),
            node_kind=_opt_str(raw.get("node_kind")),
            virtue_bucket=str(raw.get("virtue_bucket") or "mid"),
            hearts_left=int(raw.get("hearts_left") or 0),
            enemy_school=_opt_str(raw.get("enemy_school")),
            intent_kind=_opt_str(raw.get("intent_kind")),
            counter_arrow=_opt_str(raw.get("counter_arrow")),
            hand_count=int(raw.get("hand_count") or 0),
            corrupt_count=int(raw.get("corrupt_count") or 0),
            slot_kinds=tuple(str(s) for s in slots),
        )

    def key(self) -> str:
        """``sk-<8 hex>``：``sha256`` 规范化情境字典的前 8 位。"""
        digest = content_sha256_of(self.to_dict())
        return f"sk-{digest[:8]}"


@dataclass(frozen=True)
class MemoryEntry:
    """一条可检索、可度量、可淘汰、可回滚的策略记忆（PRD §6.2/§6.6）。"""

    entry_id: str
    schema_version: str
    created_at: str
    created_tick: int
    run_id: str
    generation: int
    harness_version: int
    text_hash: str
    content_sha256: str
    source_episode_id: str
    source_reflection_id: str
    situation: SituationKey
    situation_key: str
    tags: tuple[str, ...]
    lesson: str
    action_hint: dict[str, Any] | None
    polarity: str
    confidence: float
    last_ref_tick: int | None = None
    n_ref: int = 0
    n_adopted: int = 0
    n_win_adopted: int = 0
    n_not_adopted: int = 0
    n_win_not_adopted: int = 0
    utility: float = 0.0
    ttl_ticks: int = 300
    expires_tick: int = 0
    status: str = "active"
    supersedes: str | None = None
    superseded_by: str | None = None
    conflict_group: str | None = None
    merged_from: tuple[str, ...] = ()
    embedding: tuple[float, ...] | None = None

    # ---- 序列化 ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """固定的 33 键字典（顺序即 ``to_json`` 的字节顺序）。"""
        return {
            "entry_id": self.entry_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "created_tick": int(self.created_tick),
            "run_id": self.run_id,
            "generation": int(self.generation),
            "harness_version": int(self.harness_version),
            "text_hash": self.text_hash,
            "content_sha256": self.content_sha256,
            "source_episode_id": self.source_episode_id,
            "source_reflection_id": self.source_reflection_id,
            "situation": self.situation.to_dict(),
            "situation_key": self.situation_key,
            "tags": list(self.tags),
            "lesson": self.lesson,
            "action_hint": None if self.action_hint is None else dict(self.action_hint),
            "polarity": self.polarity,
            "confidence": round6(self.confidence),
            "last_ref_tick": (None if self.last_ref_tick is None
                              else int(self.last_ref_tick)),
            "n_ref": int(self.n_ref),
            "n_adopted": int(self.n_adopted),
            "n_win_adopted": int(self.n_win_adopted),
            "n_not_adopted": int(self.n_not_adopted),
            "n_win_not_adopted": int(self.n_win_not_adopted),
            "utility": round6(self.utility),
            "ttl_ticks": int(self.ttl_ticks),
            "expires_tick": int(self.expires_tick),
            "status": self.status,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "conflict_group": self.conflict_group,
            "merged_from": list(self.merged_from),
            "embedding": None if self.embedding is None else list(self.embedding),
        }

    def content_payload(self) -> dict[str, Any]:
        """``content_sha256`` 的输入：去掉 ``created_at`` 与 ``content_sha256`` 本身。"""
        payload = self.to_dict()
        payload.pop("created_at", None)
        payload.pop("content_sha256", None)
        return payload

    def compute_content_sha256(self) -> str:
        return content_sha256_of(self.content_payload())

    def with_updates(self, **changes: Any) -> "MemoryEntry":
        """派生新条目并重算 ``content_sha256``（同字段的旧值不参与）。"""
        updated = replace(self, **changes)
        return replace(updated, content_sha256=updated.compute_content_sha256())

    def to_json(self) -> str:
        """规范 JSON 单行（无 ``\\n``）；``from_json`` 与它逐字节可逆。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=_JSON_SEP)

    @classmethod
    def from_json(cls, line: str) -> "MemoryEntry":
        """解析并校验一行 ``MemoryEntry``；未知字段/缺字段/类型错 → ``InfraError``。

        快照与 active 库的行都带 ``_type":"memory_entry"`` 前缀（两种文件因此逐字节同构，
        ``rollback`` 可直接原子替换 active 库）；解析时先剥掉该前缀再走 schema 校验。

        校验走 ``validate_memory_entry_fast``（同一份 schema 编译成的检查计划）：加载
        2000 行是热路径，jsonschema 的递归校验实测占启动加载 88%（NFR-05-2 的 2 s 预算
        在 CI 上因此被顶穿）。**写路径仍用全量 ``validate_memory_entry``**——外部输入在
        那里被拦下，加载看到的是本进程写出的行；两者语义一致，测试逐条覆盖（未知字段、
        缺字段、类型错、枚举、pattern 都会被拒）。
        """
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InfraError(E.E_IO_MEMORY_CORRUPT,
                             f"MemoryEntry 行不是合法 JSON：{exc}") from exc
        if not isinstance(raw, Mapping):
            raise InfraError(E.E_IO_MEMORY_CORRUPT,
                             f"MemoryEntry 行必须是 JSON 对象，实际 {type(raw).__name__}")
        if "_type" in raw:
            if raw.get("_type") != "memory_entry":
                raise InfraError(E.E_IO_MEMORY_CORRUPT,
                                 f"未知行类型 _type={raw.get('_type')!r}")
            raw = {k: v for k, v in raw.items() if k != "_type"}
        validate_memory_entry_fast(raw)
        return cls(
            entry_id=str(raw["entry_id"]),
            schema_version=str(raw["schema_version"]),
            created_at=str(raw["created_at"]),
            created_tick=int(raw["created_tick"]),
            run_id=str(raw["run_id"]),
            generation=int(raw["generation"]),
            harness_version=int(raw["harness_version"]),
            text_hash=str(raw["text_hash"]),
            content_sha256=str(raw["content_sha256"]),
            source_episode_id=str(raw["source_episode_id"]),
            source_reflection_id=str(raw["source_reflection_id"]),
            situation=SituationKey.from_dict(raw["situation"]),
            situation_key=str(raw["situation_key"]),
            tags=tuple(str(t) for t in raw["tags"]),
            lesson=str(raw["lesson"]),
            action_hint=(None if raw.get("action_hint") is None
                         else dict(raw["action_hint"])),
            polarity=str(raw["polarity"]),
            confidence=round6(raw["confidence"]),
            last_ref_tick=(None if raw.get("last_ref_tick") is None
                           else int(raw["last_ref_tick"])),
            n_ref=int(raw.get("n_ref", 0)),
            n_adopted=int(raw.get("n_adopted", 0)),
            n_win_adopted=int(raw.get("n_win_adopted", 0)),
            n_not_adopted=int(raw.get("n_not_adopted", 0)),
            n_win_not_adopted=int(raw.get("n_win_not_adopted", 0)),
            utility=round6(raw.get("utility", 0.0)),
            ttl_ticks=int(raw.get("ttl_ticks", 300)),
            expires_tick=int(raw.get("expires_tick", 0)),
            status=str(raw.get("status", "active")),
            supersedes=_opt_str(raw.get("supersedes")),
            superseded_by=_opt_str(raw.get("superseded_by")),
            conflict_group=_opt_str(raw.get("conflict_group")),
            merged_from=tuple(str(m) for m in (raw.get("merged_from") or ())),
            embedding=(None if raw.get("embedding") is None
                       else tuple(float(x) for x in raw["embedding"])),
        )


@dataclass(frozen=True)
class ReflectionEntry:
    """一局反思（文本由 MOD-06 产出，本模块不生成；PRD §6.2）。"""

    reflection_id: str
    schema_version: str
    created_at: str
    run_id: str
    episode_id: str
    seed: int
    split: str
    generation: int
    harness_version: int
    tick: int
    text: str
    outcome: str
    truncation_reason: str | None = None
    stages_cleared: int = 0
    hearts_left: int = 0
    battles_won: int = 0
    decisions: int = 0
    illegal_attempts: int = 0
    fallback_decisions: int = 0
    failure_cause: str = "unknown"
    tags: tuple[str, ...] = ()
    extracted_memory_ids: tuple[str, ...] = ()
    scan: dict[str, Any] = field(default_factory=lambda: {"status": "pass", "hits": []})
    llm_latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reflection_id": self.reflection_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "episode_id": self.episode_id,
            "seed": int(self.seed),
            "split": self.split,
            "generation": int(self.generation),
            "harness_version": int(self.harness_version),
            "tick": int(self.tick),
            "text": self.text,
            "outcome": self.outcome,
            "truncation_reason": self.truncation_reason,
            "stages_cleared": int(self.stages_cleared),
            "hearts_left": int(self.hearts_left),
            "battles_won": int(self.battles_won),
            "decisions": int(self.decisions),
            "illegal_attempts": int(self.illegal_attempts),
            "fallback_decisions": int(self.fallback_decisions),
            "failure_cause": self.failure_cause,
            "tags": list(self.tags),
            "extracted_memory_ids": list(self.extracted_memory_ids),
            "scan": dict(self.scan),
            "llm_latency_ms": self.llm_latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=_JSON_SEP)

    @classmethod
    def from_json(cls, line: str) -> "ReflectionEntry":
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InfraError(E.E_EVAL_SCHEMA, f"ReflectionEntry 行不是合法 JSON：{exc}") from exc
        if not isinstance(raw, Mapping):
            raise InfraError(E.E_EVAL_SCHEMA, "ReflectionEntry 行必须是 JSON 对象")
        validate_reflection_entry(raw)
        return cls(
            reflection_id=str(raw["reflection_id"]),
            schema_version=str(raw["schema_version"]),
            created_at=str(raw["created_at"]),
            run_id=str(raw["run_id"]),
            episode_id=str(raw["episode_id"]),
            seed=int(raw["seed"]),
            split=str(raw["split"]),
            generation=int(raw["generation"]),
            harness_version=int(raw["harness_version"]),
            tick=int(raw["tick"]),
            text=str(raw["text"]),
            outcome=str(raw["outcome"]),
            truncation_reason=_opt_str(raw.get("truncation_reason")),
            stages_cleared=int(raw.get("stages_cleared", 0)),
            hearts_left=int(raw.get("hearts_left", 0)),
            battles_won=int(raw.get("battles_won", 0)),
            decisions=int(raw.get("decisions", 0)),
            illegal_attempts=int(raw.get("illegal_attempts", 0)),
            fallback_decisions=int(raw.get("fallback_decisions", 0)),
            failure_cause=str(raw.get("failure_cause", "unknown")),
            tags=tuple(str(t) for t in (raw.get("tags") or ())),
            extracted_memory_ids=tuple(str(m) for m in (raw.get("extracted_memory_ids") or ())),
            scan=dict(raw.get("scan") or {"status": "pass", "hits": []}),
            llm_latency_ms=_opt_int(raw.get("llm_latency_ms")),
            prompt_tokens=_opt_int(raw.get("prompt_tokens")),
            completion_tokens=_opt_int(raw.get("completion_tokens")),
        )


@dataclass(frozen=True)
class Provenance:
    """写入准入凭证：唯一的 seed 入口，且只允许 ``train``（PRD §6.2 / I1）。"""

    run_id: str
    episode_id: str
    split: str
    seed: int
    generation: int
    harness_version: int

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "episode_id": self.episode_id, "split": self.split,
                "seed": int(self.seed), "generation": int(self.generation),
                "harness_version": int(self.harness_version)}


@dataclass(frozen=True)
class MemoryCandidate:
    """``extract_candidates`` 的产物（尚未落盘的记忆候选）。"""

    situation: SituationKey
    tags: tuple[str, ...]
    lesson: str
    polarity: str
    confidence: float
    action_hint: dict[str, Any] | None = None
    source_episode_id: str = ""
    source_reflection_id: str = ""
    created_at: str = "1970-01-01T00:00:00Z"
    created_tick: int = 0
    run_id: str = ""
    generation: int = 0
    harness_version: int = 0
    ttl_ticks: int | None = None
    embedding: tuple[float, ...] | None = None
    situation_key: str = ""

    def __post_init__(self) -> None:
        if not self.situation_key:
            object.__setattr__(self, "situation_key", self.situation.key())

    @property
    def text_hash(self) -> str:
        """lesson 规范化后的 sha256（去重按 ``(situation_key, text_hash)``）。"""
        return hashlib.sha256(_normalize_text(self.lesson).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {"situation": self.situation.to_dict(), "situation_key": self.situation_key,
                "tags": list(self.tags), "lesson": self.lesson, "polarity": self.polarity,
                "confidence": round6(self.confidence), "action_hint": self.action_hint,
                "source_episode_id": self.source_episode_id,
                "source_reflection_id": self.source_reflection_id,
                "created_at": self.created_at, "created_tick": int(self.created_tick),
                "text_hash": self.text_hash}


@dataclass(frozen=True)
class AddResult:
    """``add_reflection`` / ``add_memory`` 的返回值（PRD §8.1）。"""

    added: int = 0
    merged: int = 0
    conflicted: int = 0
    quarantined: int = 0
    evicted: int = 0
    memory_version: int = 0
    entry_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"added": int(self.added), "merged": int(self.merged),
                "conflicted": int(self.conflicted), "quarantined": int(self.quarantined),
                "evicted": int(self.evicted), "memory_version": int(self.memory_version),
                "entry_ids": list(self.entry_ids), "reasons": list(self.reasons)}

    def __str__(self) -> str:  # MOD-06 历史签名 `add_reflection(...) -> str` 的兼容
        return (f"added={self.added} merged={self.merged} conflicted={self.conflicted} "
                f"quarantined={self.quarantined} memory_version={self.memory_version}")


@dataclass(frozen=True)
class DroppedEntry:
    """被剔除的候选条目及原因（FR-05-6：``dropped[]`` 记原因）。"""

    entry_id: str
    reason: str
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, "reason": self.reason,
                "score": round6(self.score)}


@dataclass(frozen=True)
class RetrievedMemory:
    """一条入选记忆（同时满足 MOD-04 ``MemoryItem`` 的四字段读取）。"""

    entry: MemoryEntry
    score: float
    tokens: int
    rank: int
    text: str

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def memory_id(self) -> str:  # MOD-04 `MemoryItem.memory_id`
        return self.entry.entry_id

    @property
    def lesson(self) -> str:
        return self.entry.lesson

    @property
    def tags(self) -> tuple[str, ...]:
        return self.entry.tags

    @property
    def polarity(self) -> str:
        return self.entry.polarity

    @property
    def harness_version(self) -> int:
        return self.entry.harness_version

    @property
    def situation_key(self) -> str:
        return self.entry.situation_key

    @property
    def conflict_group(self) -> str | None:
        return self.entry.conflict_group

    @property
    def action_hint(self) -> dict[str, Any] | None:
        return self.entry.action_hint

    @property
    def utility(self) -> float:
        return self.entry.utility

    def to_dict(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, "score": round6(self.score),
                "tokens": int(self.tokens), "rank": int(self.rank), "text": self.text,
                "tags": list(self.tags), "polarity": self.polarity,
                "harness_version": self.harness_version,
                "situation_key": self.situation_key,
                "conflict_group": self.conflict_group,
                "action_hint": self.action_hint}


@dataclass(frozen=True)
class RetrieveResult:
    """``retrieve()`` 的返回值（PRD FR-05-5）；``text`` 即 ``{{memory}}`` 的填充值。"""

    entries: tuple[RetrievedMemory, ...] = ()
    scores: tuple[float, ...] = ()
    tokens: int = 0
    dropped: tuple[DroppedEntry, ...] = ()
    conflict_groups: tuple[str, ...] = ()
    elapsed_ms: float = 0.0
    text: str = ""
    obs_digest: str = ""
    n_candidates: int = 0

    def __iter__(self):  # 兼容 Spec §4.7 的 `list[MemoryHit]` 读法
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [e.to_dict() for e in self.entries],
                "scores": [round6(s) for s in self.scores],
                "tokens": int(self.tokens),
                "dropped": [d.to_dict() for d in self.dropped],
                "conflict_groups": list(self.conflict_groups),
                "elapsed_ms": round6(self.elapsed_ms),
                "text": self.text, "obs_digest": self.obs_digest,
                "n_candidates": int(self.n_candidates)}

    def dropped_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.dropped:
            out[item.reason] = out.get(item.reason, 0) + 1
        return out


@dataclass(frozen=True)
class StoreStats:
    """``stats()`` 的返回值（FR-05-11 / §13 的派生指标）。"""

    memory_version: int = 0
    parent_memory_version: int = 0
    harness_version: int = 0
    generation: int = 0
    entry_count: int = 0
    active_count: int = 0
    evicted_count: int = 0
    quarantined_count: int = 0
    merged_count: int = 0
    conflicted_count: int = 0
    candidate_count: int = 0
    dedup_rate: float = 0.0
    conflict_rate: float = 0.0
    retrieve_hit_rate: float = 0.0
    seed_leak_blocks: int = 0
    write_failures: int = 0
    snapshot_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_version": int(self.memory_version),
            "parent_memory_version": int(self.parent_memory_version),
            "harness_version": int(self.harness_version),
            "generation": int(self.generation),
            "entry_count": int(self.entry_count),
            "active_count": int(self.active_count),
            "evicted_count": int(self.evicted_count),
            "quarantined_count": int(self.quarantined_count),
            "merged_count": int(self.merged_count),
            "conflicted_count": int(self.conflicted_count),
            "candidate_count": int(self.candidate_count),
            "dedup_rate": round6(self.dedup_rate),
            "conflict_rate": round6(self.conflict_rate),
            "retrieve_hit_rate": round6(self.retrieve_hit_rate),
            "seed_leak_blocks": int(self.seed_leak_blocks),
            "write_failures": int(self.write_failures),
            "snapshot_count": int(self.snapshot_count),
        }


@dataclass(frozen=True)
class VerifyReport:
    """``verify()`` 的返回值（FR-05-11）。"""

    checked: int = 0
    ok: int = 0
    bad_lines: int = 0
    quarantined: int = 0
    duplicates: int = 0
    sha256_mismatch: int = 0
    situation_key_mismatch: int = 0
    evicted_ttl: int = 0
    leak_hits: int = 0
    codes: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not (self.bad_lines or self.quarantined or self.duplicates
                    or self.sha256_mismatch or self.situation_key_mismatch
                    or self.leak_hits)

    def to_dict(self) -> dict[str, Any]:
        return {"checked": int(self.checked), "ok": int(self.ok),
                "bad_lines": int(self.bad_lines), "quarantined": int(self.quarantined),
                "duplicates": int(self.duplicates),
                "sha256_mismatch": int(self.sha256_mismatch),
                "situation_key_mismatch": int(self.situation_key_mismatch),
                "evicted_ttl": int(self.evicted_ttl), "leak_hits": int(self.leak_hits),
                "healthy": self.healthy, "codes": list(self.codes)}


@dataclass(frozen=True)
class RollbackReport:
    """``rollback()`` 的返回值（FR-05-9 / §8.3）。"""

    from_version: int = 0
    to_version: int = 0
    target_version: int = 0
    dry_run: bool = False
    entries: int = 0
    active: int = 0
    entries_delta: int = 0
    active_delta: int = 0
    lost_entry_ids: tuple[str, ...] = ()
    content_sha256: str = ""
    harness_version: int = 0
    snapshot_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": int(self.from_version), "to_version": int(self.to_version),
            "target_version": int(self.target_version), "dry_run": bool(self.dry_run),
            "entries": int(self.entries), "active": int(self.active),
            "entries_delta": int(self.entries_delta),
            "active_delta": int(self.active_delta),
            "lost_entry_ids": list(self.lost_entry_ids),
            "content_sha256": self.content_sha256,
            "harness_version": int(self.harness_version),
            "snapshot_path": self.snapshot_path,
        }


class MemoryViewProtocol:  # pragma: no cover - 仅作文档化协议
    """``MemoryStore`` / ``MemoryView`` 的共同只读面（类型提示用）。"""

    def retrieve(self, obs: Mapping[str, Any], **kwargs: Any) -> RetrieveResult:
        raise NotImplementedError


# =====================================================================
# 校验入口
# =====================================================================

def validate_memory_entry(obj: Mapping[str, Any]) -> None:
    """``MemoryEntry`` 字典校验（未知字段拒绝）；失败 → ``E_IO_MEMORY_CORRUPT``。"""
    _validate(MEMORY_ENTRY_SCHEMA, obj, name="memory_entry", code=E.E_IO_MEMORY_CORRUPT)


def validate_reflection_entry(obj: Mapping[str, Any]) -> None:
    """``ReflectionEntry`` 字典校验；失败 → ``E_EVAL_SCHEMA``。"""
    _validate(REFLECTION_ENTRY_SCHEMA, obj, name="reflection_entry", code=E.E_EVAL_SCHEMA)


# ---- 内部小工具 -------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """确定性文本规范化：折叠空白、去首尾空白（不改大小写，保留中文原貌）。"""
    return " ".join(str(text or "").split())


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
