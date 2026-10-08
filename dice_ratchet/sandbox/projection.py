"""MOD-02 观测投影：INV-2 的唯一权威登记表与纯函数投影（PRD §4/§6/§8）。

数据通道（FR-02-3a）：``GameEngine.observe(state, "full")``（GAME ``obs-1.0.0``，
游戏侧已是白名单）→ 键子集 + 3 个 agent 专属键（``episodeId``/``decisionIndex``/
``lastResult``）+ ``stageCount``，字段名保留 camelCase（Spec §4.2 / B-10）。
本模块**不直接读 GameState**，所以隐藏信息有两道防线。

不变量：

- 纯函数、无随机、无时间依赖（FR-02-1 / NFR-02-1）：同输入两次调用 canonical JSON
  逐字节相同；canonical JSON 统一复用 MOD-01 ``stable_stringify``（JS 逐位一致）。
- 只输出 ``OBSERVATION_SCHEMA`` 中 ``enabled=True`` 的字段；源里存在但未登记的字段
  一律丢弃，**只记计数、不记名称**（FR-02-2/FR-02-24，防止日志反向泄漏字段名）。
- ``schemaVersion`` 由本层覆盖式注入，同名源字段被丢弃（FR-02-6）。
- 字段增删/类型变更必须主版本 +1 并追加 ``docs/prd/OBSERVATION_CHANGELOG.md``
  （FR-02-4）；CHANGELOG 一致性只在 CI 校验，运行时不读文档目录（FR-02-5）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from dice_ratchet.env.digest import stable_stringify
from dice_ratchet.infra.errors import E, InfraError

from .honeypot import FAKE_FIELD_NAMES, HONEYPOT_CANARY

__all__ = [
    "OBSERVATION_SCHEMA_VERSION", "ObsFieldSpec", "ProjectionStat", "ProjectionCtx",
    "OBSERVATION_SCHEMA", "OBSERVATION_SCHEMA_BY_NAME", "obs_field_paths",
    "obs_field_names", "project_state", "canonical_json", "obs_digest",
    "validate_observation", "render_compact", "freeze_observation",
    "changelog_version", "check_changelog", "CHANGELOG_PATH", "MODE_STRICT",
    "MODE_ASSERT", "DELETED_SOURCE_PATHS",
]

OBSERVATION_SCHEMA_VERSION: Final[str] = "obs-agent-1.0.0"
MODE_STRICT: Final[str] = "strict"
MODE_ASSERT: Final[str] = "assert"
_MODES: Final[frozenset[str]] = frozenset({MODE_STRICT, MODE_ASSERT})

#: agent 专属键的 source_path 前缀（值由 ``ProjectionCtx`` 提供，不从 GAME obs 取）。
_AGENT_PREFIX: Final[str] = "@"
_CTX_PREFIX: Final[str] = "@ctx."
_SCHEMA_VERSION_PATH: Final[str] = "@schemaVersion"

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CHANGELOG_PATH: Final[Path] = _REPO_ROOT / "docs/prd/OBSERVATION_CHANGELOG.md"

#: Spec §4.2「删除」的来源字段：GAME obs 里有、但不得进入 agent 观测（仅记账）。
DELETED_SOURCE_PATHS: Final[tuple[str, ...]] = (
    "runId", "seed", "config", "stages", "stats",
    "outcomeReason", "truncationReason",
)

#: 允许出现在输出中的类型表达式（``|`` 分隔的并集）——见 ``_type_ok``。


def _f(name: str, source_path: str, type_: str, doc: str,
       status: str = "kept", since: str = OBSERVATION_SCHEMA_VERSION) -> "ObsFieldSpec":
    """构造一条字段规格；``status``：kept / agent / rewrite。"""
    return ObsFieldSpec(name=name, source_path=source_path, type=type_, doc=doc,
                        since=since, optional=status == "optional", enabled=True)


@dataclass(frozen=True)
class ObsFieldSpec:
    """一条观测字段规格（PRD §6，全部 ``frozen`` + 显式类型，禁可变默认值）。"""

    name: str # 输出路径（点分；camelCase，与 GAME 一致）
    source_path: str # GAME obs 的取值路径，或 ``@ctx.*`` / ``@schemaVersion``
    type: str # "int" / "string" / "object" / "array" / "object|null" ...
    doc: str
    since: str
    optional: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class ProjectionStat:
    """投影统计：**只记计数不记名称**（FR-02-2 / PRD §7）。"""

    kept: tuple[str, ...]
    dropped_hidden: int
    missing_optional: tuple[str, ...]


@dataclass(frozen=True)
class ProjectionCtx:
    """投影上下文：agent 专属键的唯一来源（``episodeId``/``decisionIndex``/...）。"""

    episode_id: str = "e-local"
    decision_index: int = 0
    stage_count: int = 6
    last_result: Mapping[str, Any] | None = None


# ----------------------------------------------------------------------
# 注册表（唯一权威；字段集合 = Spec §4.2 保留表 + agent 专属键）
# ----------------------------------------------------------------------

OBSERVATION_SCHEMA: tuple[ObsFieldSpec, ...] = (
    _f("schemaVersion", _SCHEMA_VERSION_PATH, "string",
       "投影协议版本，覆盖式注入（FR-02-6）", "agent"),
    _f("episodeId", _CTX_PREFIX + "episodeId", "string",
       "MOD-06 的 episode_key（agent 专属键）", "agent"),
    _f("decisionIndex", _CTX_PREFIX + "decisionIndex", "int",
       "决策点序号（agent 专属键）", "agent"),
    _f("phase", "phase", "string", "map/player_turn/shop/event/..."),
    _f("stage", "stage", "int", "当前关（1 起）"),
    _f("stageCount", _CTX_PREFIX + "stageCount", "int",
       "本局关数（公开；agent 专属键）"),
    _f("chapter", "chapter", "int", "当前章"),
    _f("turn", "turn", "int", "当前回合"),
    _f("difficulty", "difficulty", "string", "easy/medium/hard"),
    _f("player.hearts", "player.hearts", "int", "生命"),
    _f("player.heartsMax", "player.heartsMax", "int", "生命上限"),
    _f("player.virtue", "player.virtue", "int", "德性"),
    _f("player.virtueMax", "player.virtueMax", "int", "德性上限"),
    _f("player.ward", "player.ward", "int", "护盾"),
    _f("player.starShards", "player.starShards", "int", "星屑（货币）"),
    _f("player.statuses", "player.statuses", "array", "自身状态列表"),
    _f("player.deckSchool", "player.deckSchool", "string", "骰池主学派"),
    _f("player.purifyLeft", "player.purifyLeft", "int", "本关剩余净化次数"),
    _f("player.abilities", "player.abilities", "array", "技能与剩余次数"),
    _f("hand", "hand", "array", "手骰（战斗内；phase 外为空表）"),
    _f("slots", "slots", "array", "骰槽与约束（GAME-00 §4.3 文法）"),
    _f("enemies", "enemies", "array", "敌人公开状态 + 已提交 intent"),
    _f("map", "map", "object|null", "仅已揭示的行与 reachable/preview", "optional"),
    _f("shop", "shop", "object|null", "phase=shop 时的陈列（含 itemId）", "optional"),
    _f("event", "event", "object|null", "phase=event 时的事件与选项", "optional"),
    _f("treasure", "treasure", "object|null", "phase=treasure 时的宝箱", "optional"),
    _f("forge", "forge", "object|null", "phase=forge 时的锻造候选", "optional"),
    _f("shrine", "shrine", "object|null", "phase=shrine 时的神龛选项", "optional"),
    _f("rest", "rest", "object|null", "phase=rest 时的休整选项", "optional"),
    _f("pendingRelic", "pendingRelic", "object|null", "待取舍的遗物", "optional"),
    _f("toggles", "toggles", "object", "公开配置开关"),
    _f("relics", "relics", "array", "已持有遗物（公开）"),
    _f("legalActionKinds", "legalActionKinds", "array",
       "合法动作种类（去掉 abandon_run，Spec §4.2 改写）"),
    _f("lastResult", _CTX_PREFIX + "lastResult", "object|null",
       "上一次被拒绝的原因（B-15 重试反馈）", "agent"),
)

OBSERVATION_SCHEMA_BY_NAME: Final[Mapping[str, ObsFieldSpec]] = {
    spec.name: spec for spec in OBSERVATION_SCHEMA
}

#: 注册表的顶层命名空间（``player`` 这类容器也算已登记）。
_TOP_LEVEL_NAMES: Final[frozenset[str]] = frozenset(
    spec.name.split(".")[0] for spec in OBSERVATION_SCHEMA if spec.enabled)

# 只在投影层做改写、不改变 GAME 语义的字段（Spec §4.2「改写」行）
_REWRITES: Final[Mapping[str, str]] = {"legalActionKinds": "drop_abandon_run"}


def _assert_no_honeypot_in_registry() -> None:
    """FR-02-21：canary 与 3 个假字段名不得出现在任何 ``source_path``/``name`` 中。"""
    tokens = (HONEYPOT_CANARY, *FAKE_FIELD_NAMES)
    for spec in OBSERVATION_SCHEMA:
        haystack = f"{spec.name}\x00{spec.source_path}"
        for token in tokens:
            if token in haystack:
                raise InfraError(
                    E.E_ENV_HONEYPOT,
                    f"OBSERVATION_SCHEMA 字段 {spec.name!r} 的路径含蜜罐哨兵 {token!r}",
                    hint="哨兵只允许存在于注入副本中，注册表导入即断言（FR-02-21）")


_assert_no_honeypot_in_registry()


# ----------------------------------------------------------------------
# 注册表查询
# ----------------------------------------------------------------------

def obs_field_paths() -> tuple[str, ...]:
    """全部 ``enabled`` 字段的 ``name``（审计器据此重建可见集合，FR-02-16）。"""
    return tuple(spec.name for spec in OBSERVATION_SCHEMA if spec.enabled)


def obs_field_names() -> tuple[str, ...]:
    """``obs_field_paths()`` 的别名（对外文档用词统一为 paths）。"""
    return obs_field_paths()


# ----------------------------------------------------------------------
# 投影（FR-02-1 ~ FR-02-6/FR-02-24）
# ----------------------------------------------------------------------

def _resolve(source: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """按点分路径取值；返回 ``(是否命中, 值)``。"""
    cur: Any = source
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _type_ok(value: Any, type_expr: str, path: str) -> bool:
    for alt in type_expr.split("|"):
        if alt == "any":
            return True
        if alt == "null" and value is None:
            return True
        if alt == "string" and isinstance(value, str):
            return True
        if alt == "int" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if alt == "bool" and isinstance(value, bool):
            return True
        if alt == "object" and isinstance(value, Mapping):
            return True
        if alt == "array" and isinstance(value, (list, tuple)):
            return True
    raise InfraError(E.E_ENV_SCHEMA,
                     f"字段 {path} 的值 {type(value).__name__} 不匹配声明类型 {type_expr!r}",
                     hint="OBSERVATION_SCHEMA 的 type 与 GAME obs 不一致；"
                          "类型变更需主版本 +1（FR-02-4）")


def _ctx_value(ctx: ProjectionCtx, key: str) -> tuple[bool, Any]:
    if key == "episodeId":
        return True, ctx.episode_id
    if key == "decisionIndex":
        return True, ctx.decision_index
    if key == "stageCount":
        return True, ctx.stage_count
    if key == "lastResult":
        return True, dict(ctx.last_result) if ctx.last_result else None
    raise InfraError(E.E_ENV_SCHEMA, f"未登记的 agent 专属键 {key!r}",
                     hint="agent 键只有 episodeId/decisionIndex/stageCount/lastResult")


def _assign(out: dict[str, Any], name: str, value: Any) -> None:
    parts = name.split(".")
    cur = out
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _drop_abandon_run(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        return value
    return [k for k in value if k != "abandon_run"]


def _rewrite(field: str, value: Any) -> Any:
    rule = _REWRITES.get(field)
    if rule is None:
        return value
    if rule == "drop_abandon_run":
        return _drop_abandon_run(value)
    raise InfraError(E.E_ENV_SCHEMA, f"未知改写规则 {rule!r}")  # pragma: no cover


#: 注册表的 source 根（顶层键）——用于统计未登记字段（只记计数）
_SOURCE_ROOTS: Final[frozenset[str]] = frozenset(
    spec.source_path.split(".")[0] for spec in OBSERVATION_SCHEMA
    if not spec.source_path.startswith(_AGENT_PREFIX)
)
#: 容器级子键白名单（目前只有 ``player`` 逐键登记）
_CONTAINER_SUBKEYS: Final[Mapping[str, frozenset[str]]] = {
    "player": frozenset(spec.source_path.split(".", 1)[1] for spec in OBSERVATION_SCHEMA
                        if spec.source_path.startswith("player.")),
}


def project_state(game_obs: Mapping[str, Any], *, ctx: ProjectionCtx,
                  mode: str = MODE_STRICT) -> tuple[dict[str, Any], ProjectionStat]:
    """GAME ``observe(state,"full")`` → ``obs-agent-1.0.0``（纯函数，FR-02-3a）。

    - ``mode="strict"``：丢弃未登记字段并记入 ``dropped_hidden``（FR-02-24）。
    - ``mode="assert"``：投影后跑 ``validate_observation``，任一违规抛 ``E_ENV_SCHEMA``
      （供 golden/CI；包含未登记字段的隐藏哨兵）。
    - 非 ``optional`` 字段源缺失 → ``E_ENV_PROJECTION_MISSING``（不返回部分观测）。
    """
    if mode not in _MODES:
        raise InfraError(E.E_ENV_SCHEMA, f"未知投影模式 {mode!r}",
                         hint=f"合法模式：{sorted(_MODES)}")
    if not isinstance(game_obs, Mapping):
        raise InfraError(E.E_ENV_PROJECTION_MISSING,
                         f"投影源必须是 Mapping，实际 {type(game_obs).__name__}")

    dropped = 0
    for key in game_obs:
        if key not in _SOURCE_ROOTS:
            dropped += 1
    for root, subkeys in _CONTAINER_SUBKEYS.items():
        container = game_obs.get(root)
        if isinstance(container, Mapping):
            dropped += sum(1 for k in container if k not in subkeys)

    out: dict[str, Any] = {}
    kept: list[str] = []
    missing_optional: list[str] = []
    for spec in OBSERVATION_SCHEMA:
        if not spec.enabled:
            continue
        path = spec.source_path
        if path == _SCHEMA_VERSION_PATH:
            found, value = True, OBSERVATION_SCHEMA_VERSION
        elif path.startswith(_CTX_PREFIX):
            found, value = _ctx_value(ctx, path[len(_CTX_PREFIX):])
        else:
            found, value = _resolve(game_obs, path)
        if not found or (value is None and "null" not in spec.type):
            if spec.optional:
                missing_optional.append(spec.name)
                _assign(out, spec.name, None)
                kept.append(spec.name)
                continue
            raise InfraError(
                E.E_ENV_PROJECTION_MISSING,
                f"非 optional 字段 {spec.name!r} 在 GAME 观测中缺失（source={path}）",
                hint="MOD-01 observe 契约变更或注册表 source_path 漂移；"
                     "终止该局，不返回部分观测")
        _type_ok(value, spec.type, spec.name)
        _assign(out, spec.name, _rewrite(spec.name, value))
        kept.append(spec.name)

    stat = ProjectionStat(kept=tuple(kept), dropped_hidden=dropped,
                          missing_optional=tuple(missing_optional))
    if mode == MODE_ASSERT:
        violations = validate_observation(out)
        if violations:
            raise InfraError(E.E_ENV_SCHEMA,
                             "mode='assert' 下观测校验失败：" + "; ".join(violations[:8]),
                             hint="golden/CI 用 assert 模式（FR-02-24）")
    return out, stat


# ----------------------------------------------------------------------
# canonical JSON / digest（NFR-02-1）
# ----------------------------------------------------------------------

def canonical_json(obj: Any) -> bytes:
    """canonical JSON 字节串：复用 MOD-01 ``stable_stringify``（与 JS 逐位一致）。

    MOD-01 v1.0 的 ``stable_stringify`` 对非 ASCII 字符**不转义**（JS
    ``JSON.stringify`` 行为），这是 ``digest.py`` 的既有裁决；本模块直接复用，
    保证 ``obs_digest`` 与环境侧摘要同一套规范化。内核无浮点（K-3），
    遇到 ``float`` 会抛 ``TypeError``（观测里不出现浮点）。
    """
    return stable_stringify(obj).encode("utf-8")


def obs_digest(obs: Mapping[str, Any]) -> str:
    """``"sha256:<hex>"``，含 ``schemaVersion`` 的 canonical JSON 摘要（FR-02-7）。"""
    return "sha256:" + hashlib.sha256(canonical_json(obs)).hexdigest()


# ----------------------------------------------------------------------
# 校验（G5 round-trip）
# ----------------------------------------------------------------------

#: 输出中绝不允许出现的键名（含蜜罐假字段 + Spec §4.2 的「删除」来源）——出现即为投影 bug。
_FORBIDDEN_KEY_NAMES: Final[frozenset[str]] = frozenset({
    "seed", "rng", "rngDigest", "stateDigest", "stateVersion", "configHash",
    "runId", "drawPile", "discardPile", "fullState", "GameState",
    *FAKE_FIELD_NAMES, *DELETED_SOURCE_PATHS,
})
_FORBIDDEN_ANYWHERE: Final[tuple[str, ...]] = (HONEYPOT_CANARY, *FAKE_FIELD_NAMES)


def _walk(path: str, value: Any, violations: list[str]) -> None:
    if isinstance(value, Mapping):
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k) in _FORBIDDEN_KEY_NAMES or str(k).startswith("__"):
                violations.append(f"{p}: 禁止出现的键名 {k!r}")
            _walk(p, v, violations)
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk(f"{path}[{i}]", item, violations)
        return
    if isinstance(value, str):
        for token in _FORBIDDEN_ANYWHERE:
            if token in value:
                violations.append(f"{path}: 值含蜜罐哨兵 {token!r}")


def validate_observation(obs: Mapping[str, Any]) -> list[str]:
    """校验一个观测：返回违规路径列表（空列表 = 合法），供 G5 round-trip。

    检查：① 顶层/``player`` 层键均为已登记且 enabled；② 类型与类型声明一致；
    ③ 必填字段齐全；④ 无禁止键名/哨兵值；⑤ ``schemaVersion`` 等于注册表版本。
    """
    violations: list[str] = []
    if not isinstance(obs, Mapping):
        return [f"$: 观测必须是 Mapping，实际 {type(obs).__name__}"]
    for key in obs:
        if key not in _TOP_LEVEL_NAMES:
            violations.append(f"{key}: 未登记字段")
    for name in _CONTAINER_SUBKEYS:
        container = obs.get(name)
        if isinstance(container, Mapping):
            allowed = _CONTAINER_SUBKEYS[name]
            for sub in container:
                if sub not in allowed:
                    violations.append(f"{name}.{sub}: 未登记字段")
    for spec in OBSERVATION_SCHEMA:
        if not spec.enabled:
            continue
        found, value = _resolve(obs, spec.name)
        if not found:
            if not spec.optional:
                violations.append(f"{spec.name}: 缺必填字段")
            continue
        if value is None:
            if "null" not in spec.type:
                violations.append(f"{spec.name}: 不允许为 null")
            continue
        try:
            _type_ok(value, spec.type, spec.name)
        except InfraError as exc:
            violations.append(f"{spec.name}: {exc.message}")
    if obs.get("schemaVersion") != OBSERVATION_SCHEMA_VERSION:
        violations.append("schemaVersion: 与 OBSERVATION_SCHEMA_VERSION 不一致"
                          f"（{obs.get('schemaVersion')!r}）")
    _walk("", obs, violations)
    return violations


def freeze_observation(obs: Mapping[str, Any]) -> Any:
    """递归冻结为只读 ``MappingProxyType``（PRD §7 的只读语义，按需调用）。"""
    from types import MappingProxyType

    if isinstance(obs, Mapping):
        return MappingProxyType({k: freeze_observation(v) for k, v in obs.items()})
    if isinstance(obs, list):
        return tuple(freeze_observation(v) for v in obs)
    return obs


# ----------------------------------------------------------------------
# 紧凑渲染（FR-02-3b，供 MOD-04 的 {{observation}}）
# ----------------------------------------------------------------------

def _join(pairs: list[tuple[str, Any]]) -> str:
    return " ".join(f"{k}={_scalar(v)}" for k, v in pairs if v is not None)


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_scalar(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{k}:{_scalar(value[k])}" for k in sorted(value)) + "}"
    return str(value)


def _statuses(items: Any) -> str:
    if not isinstance(items, (list, tuple)) or not items:
        return "-"
    return "+".join(f"{s.get('kind')}x{s.get('stacks')}" for s in items
                    if isinstance(s, Mapping))


def _render_player(p: Mapping[str, Any]) -> str:
    line = _join([
        ("hearts", f"{p.get('hearts')}/{p.get('heartsMax')}"),
        ("virtue", f"{p.get('virtue')}/{p.get('virtueMax')}"),
        ("ward", p.get("ward")), ("shards", p.get("starShards")),
        ("school", p.get("deckSchool")), ("purify", p.get("purifyLeft")),
    ])
    st = _statuses(p.get("statuses"))
    if st != "-":
        line += f" status={st}"
    abilities = p.get("abilities")
    if isinstance(abilities, (list, tuple)) and abilities:
        line += " abilities=" + ",".join(
            f"{a.get('id')}x{a.get('usesLeft')}" for a in abilities if isinstance(a, Mapping))
    return line


def _render_hand(hand: Any) -> list[str]:
    rows: list[str] = []
    if not isinstance(hand, (list, tuple)):
        return rows
    for d in hand:
        if not isinstance(d, Mapping):
            continue
        rows.append("  - " + _join([
            ("die", d.get("dieId")), ("def", d.get("defId")), ("school", d.get("school")),
            ("risk", d.get("riskTier")), ("face", d.get("faceIndex")),
            ("kind", d.get("kind")), ("value", d.get("value")),
            ("label", d.get("label")), ("corrupt", d.get("isCorrupt")),
        ]))
    return rows


def _render_slots(slots: Any) -> list[str]:
    rows: list[str] = []
    if not isinstance(slots, (list, tuple)):
        return rows
    for s in slots:
        if not isinstance(s, Mapping):
            continue
        filled = s.get("filledBy")
        by = filled.get("dieId") if isinstance(filled, Mapping) else None
        rows.append("  - " + _join([
            ("slot", s.get("slotId")), ("kind", s.get("slotKind")),
            ("constraint", s.get("constraint")), ("effect", s.get("effectKind")),
            ("bonus", s.get("bonus")), ("by", by), ("disabled", s.get("disabled")),
        ]))
    return rows


def _render_enemies(enemies: Any) -> list[str]:
    rows: list[str] = []
    if not isinstance(enemies, (list, tuple)):
        return rows
    for e in enemies:
        if not isinstance(e, Mapping):
            continue
        intent = e.get("intent")
        itxt = "-"
        if isinstance(intent, (list, tuple)) and intent:
            itxt = ";".join(
                _join([("kind", i.get("kind")), ("value", i.get("value")),
                       ("school", i.get("school")), ("arrow", i.get("arrow")),
                       ("slot", i.get("slotId"))])
                for i in intent if isinstance(i, Mapping))
        rows.append("  - " + _join([
            ("enemy", e.get("enemyId")), ("def", e.get("defId")), ("school", e.get("school")),
            ("hp", f"{e.get('hp')}/{e.get('hpMax')}"), ("ward", e.get("ward")),
            ("phase", e.get("phase")), ("taunt", e.get("taunt")),
        ]) + f" status={_statuses(e.get('statuses'))} intent={itxt}")
    return rows


def _render_map(map_state: Any) -> list[str]:
    """地图只展开下一行的可达节点，已访问的行折叠（FR-02-3b）。"""
    rows: list[str] = []
    if not isinstance(map_state, Mapping):
        return rows
    current = map_state.get("currentRow", -1)
    rows.append(f"  stage={map_state.get('stage')} row={current} "
                f"node={map_state.get('currentNodeId')} path={_scalar(map_state.get('path'))}")
    stages_rows = map_state.get("rows")
    if not isinstance(stages_rows, (list, tuple)):
        return rows
    for r, row in enumerate(stages_rows):
        if not isinstance(row, (list, tuple)):
            continue
        if r > (current if isinstance(current, int) else -1) + 1:
            rows.append(f"  row{r}: {len(row)} node(s)（未揭示）")
            continue
        for n in row:
            if not isinstance(n, Mapping):
                continue
            preview = n.get("preview")
            pv = "-"
            if isinstance(preview, Mapping):
                pv = _join([("enemies", preview.get("enemyNames")),
                            ("schools", preview.get("schools")),
                            ("arrow", preview.get("arrow"))])
            rows.append("  " + _join([
                ("node", n.get("nodeId")), ("kind", n.get("kind")),
                ("visited", n.get("visited")), ("reachable", n.get("reachable")),
                ("edges", n.get("edges")),
            ]) + f" preview={pv}")
    return rows


def _render_shop(shop: Any) -> list[str]:
    rows: list[str] = []
    if not isinstance(shop, Mapping):
        return rows
    rows.append("  " + _join([
        ("refreshLeft", shop.get("refreshLeft")), ("refreshPrice", shop.get("refreshPrice")),
        ("removePrice", shop.get("removePrice")), ("counter", shop.get("counterSchool")),
    ]))
    items = shop.get("items")
    if isinstance(items, (list, tuple)):
        for it in items:
            if not isinstance(it, Mapping):
                continue
            rows.append("  - " + _join([
                ("item", it.get("itemId")), ("kind", it.get("kind")),
                ("def", it.get("defId")), ("name", it.get("name")),
                ("price", it.get("price")), ("sold", it.get("sold")),
                ("affordable", it.get("affordable")),
            ]))
    removable = shop.get("removable")
    if isinstance(removable, (list, tuple)):
        rows.append("  removable=" + ",".join(str(x) for x in removable))
    return rows


def _render_node(obs: Mapping[str, Any]) -> list[str]:
    rows: list[str] = []
    for key in ("event", "treasure", "forge", "shrine", "rest", "pendingRelic"):
        value = obs.get(key)
        if value in (None, {}, []):
            continue
        rows.append(f"  {key}: {_scalar(value)}")
    return rows


def render_compact(obs: Mapping[str, Any]) -> str:
    """确定性紧凑渲染（FR-02-3b；随 ``schemaVersion`` 版本化，golden 覆盖）。

    规则：地图只展开下一行可达节点（更远的行折叠为节点数），手骰/槽/敌人各占一行；
    不输出 ``lastResult`` 以外的回溯信息，不输出任何隐藏字段。
    """
    lines: list[str] = [
        f"phase={obs.get('phase')} stage={obs.get('stage')}/{obs.get('stageCount')} "
        f"ch={obs.get('chapter')} turn={obs.get('turn')} diff={obs.get('difficulty')} "
        f"decision={obs.get('decisionIndex')}",
        "player: " + _render_player(obs.get("player") or {}),
    ]
    hand = _render_hand(obs.get("hand"))
    lines.append(f"hand({len(hand)}):")
    lines.extend(hand)
    slots = _render_slots(obs.get("slots"))
    lines.append(f"slots({len(slots)}):")
    lines.extend(slots)
    enemies = _render_enemies(obs.get("enemies"))
    lines.append(f"enemies({len(enemies)}):")
    lines.extend(enemies)
    lines.append("legal: " + ",".join(str(k) for k in (obs.get("legalActionKinds") or [])))
    last = obs.get("lastResult")
    if isinstance(last, Mapping):
        lines.append("last: " + _join([("illegal", last.get("illegal")),
                                       ("reason", last.get("reason"))]))
    if obs.get("map") is not None:
        lines.append("map:")
        lines.extend(_render_map(obs.get("map")))
    if obs.get("shop") is not None:
        lines.append("shop:")
        lines.extend(_render_shop(obs.get("shop")))
    lines.extend(_render_node(obs))
    relics = obs.get("relics")
    if isinstance(relics, (list, tuple)) and relics:
        lines.append("relics: " + ",".join(
            str(r.get("relicId") if isinstance(r, Mapping) else r) for r in relics))
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# CHANGELOG 一致性（FR-02-4/FR-02-5：只在 CI 校验，运行时不读文档目录）
# ----------------------------------------------------------------------

_CHANGELOG_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\|\s*v?(\d+\.\d+\.\d+)\s*\|")


def changelog_version(path: Path | None = None) -> str:
    """读 ``OBSERVATION_CHANGELOG.md`` 末行的版本号（形如 ``1.0.0``）。"""
    target = Path(path) if path is not None else CHANGELOG_PATH
    try:
        lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as exc:
        raise InfraError(E.E_ENV_PROJECTION_VERSION,
                         f"读不到 OBSERVATION_CHANGELOG：{target}") from exc
    for line in reversed(lines):
        match = _CHANGELOG_VERSION_RE.match(line.strip())
        if match:
            return match.group(1)
    raise InfraError(E.E_ENV_PROJECTION_VERSION,
                     f"OBSERVATION_CHANGELOG 末行没有版本表格行：{target}",
                     hint="末行必须是 `| v1.0.0 | <date> | <changes> |`（FR-02-4）")


def check_changelog(path: Path | None = None) -> str:
    """CI：CHANGELOG 末行版本 == ``OBSERVATION_SCHEMA_VERSION``，否则报错（FR-02-5）。"""
    want = OBSERVATION_SCHEMA_VERSION.split("obs-agent-")[-1]
    got = changelog_version(path)
    if got != want:
        raise InfraError(
            E.E_ENV_PROJECTION_VERSION,
            f"OBSERVATION_CHANGELOG 版本 {got} != OBSERVATION_SCHEMA_VERSION {want}",
            hint="字段增删/类型变更必须主版本 +1 并追加 CHANGELOG 行（FR-02-4）")
    return got
