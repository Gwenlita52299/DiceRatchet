"""``kernel/loader.js`` 的逐位移植 —— 配表加载 + 校验 + 冻结（GAME-01 §1.1 / §8）。

这是 ``env`` 内唯一读文件的模块。校验失败抛 ``KernelError("E_CONTENT_*")``，
由 ``load_tables`` 的调用方映射为 ``E_ENV_CONTENT_INVALID``（FR-01-10）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Mapping as TypingMapping

from .error import KernelError
from .risk import compute_risk_tier, face_has_self_corruption, self_corruption_value
from .slots import parse_constraint

__all__ = ["GAME_CONTENT_DIR", "DEFAULT_CONTENT_DIR", "load_tables", "build_tables",
           "filter_by_toggles", "compute_risk_tier", "face_has_self_corruption",
           "self_corruption_value"]

ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
FACE_KINDS: Final[tuple[str, ...]] = ("purge", "ward", "mend", "status", "selfCorruption",
                                      "draw", "reroll", "modify", "gold", "nothing", "duplicate_face")
STATUS_KINDS: Final[tuple[str, ...]] = ("poison", "burn", "weak", "strength", "regen")
SCHOOL_ENUM: Final[tuple[str, ...]] = ("blade", "arcana", "verdant", "neutral")
TABLE_FILES: Final[tuple[str, ...]] = ("schools", "difficulty", "dice", "skills", "characters",
                                       "monsters", "stages", "events", "relics", "shops")

# 相对于仓库根（CWD）；可由环境变量覆盖（PRD §9 env.content_dir）
GAME_CONTENT_DIR: Final[Path] = Path(
    os.environ.get("GAMERSI_ENV__CONTENT_DIR") or "src/game/content")
DEFAULT_CONTENT_DIR: Final[Path] = GAME_CONTENT_DIR


def _fail(code: str, msg: str) -> None:
    raise KernelError(code, f"{code}: {msg}")


def _deep_freeze(o: Any) -> Any:
    """深度冻结：dict → 只读 MappingProxy，list → tuple（JS 侧 ``deepFreeze`` 的等价物）。"""
    if isinstance(o, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in o.items()})
    if isinstance(o, list):
        return tuple(_deep_freeze(v) for v in o)
    return o


def _validate_face(die_id: str, f: TypingMapping, i: int) -> None:
    if f["faceIndex"] != i:
        _fail("E_CONTENT_FACES", f"{die_id} faceIndex {i}")
    if f["kind"] not in FACE_KINDS:
        _fail("E_CONTENT_FACE_KIND", f"{die_id}[{i}] kind {f['kind']}")
    if f["kind"] == "status" and f.get("statusKind") not in STATUS_KINDS:
        _fail("E_CONTENT_FACE_KIND", f"{die_id}[{i}] statusKind")
    if f.get("school") not in SCHOOL_ENUM:
        _fail("E_CONTENT_FACE_KIND", f"{die_id}[{i}] school {f.get('school')}")
    if not _is_int(f.get("value")):
        _fail("E_CONTENT_FACE_KIND", f"{die_id}[{i}] value")


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _validate_die(die_id: str, def_: TypingMapping) -> str:
    if not ID_RE.match(die_id):
        _fail("E_CONTENT_ID_FORMAT", die_id)
    faces = def_.get("faces")
    if not isinstance(faces, list) or len(faces) != 6:
        _fail("E_CONTENT_FACES", f"{die_id} must have 6 faces")
    for i, f in enumerate(faces):
        _validate_face(die_id, f, i)
    tier = compute_risk_tier(def_)
    if def_.get("riskTier") and def_["riskTier"] != tier:
        _fail("E_CONTENT_RISK_MISMATCH", f"{die_id}: declared {def_['riskTier']}, computed {tier}")
    # 学派设计规则（GAME-00 §5.5，决策 D-10）
    if def_["category"] == "skill" and def_["school"] != "neutral":
        own = sum(1 for f in faces if f["school"] == def_["school"])
        cross = sum(1 for f in faces if f["school"] != def_["school"] and f["school"] != "neutral")
        if own < 2 or cross < 1:
            _fail("E_CONTENT_SCHOOL_RULE", f"{die_id}: own={own} cross={cross}")
    return tier


def load_tables(dir_: Path = DEFAULT_CONTENT_DIR) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for name in TABLE_FILES:
        file = Path(dir_) / f"{name}.json"
        raw[name] = json.loads(file.read_text(encoding="utf-8"))
        if not raw[name].get("$schemaVersion"):
            _fail("E_CONTENT_SCHEMA", f"{name}.json missing $schemaVersion")
    return build_tables(raw)


def build_tables(raw: TypingMapping) -> dict[str, Any]:
    dice: dict[str, Any] = {}
    skills: dict[str, Any] = {}
    for did, def_ in raw["dice"]["dice"].items():
        dice[did] = {**def_, "id": did, "riskTier": _validate_die(did, def_)}
    for sid, def_ in raw["skills"]["skills"].items():
        skills[sid] = {**def_, "id": sid, "riskTier": _validate_die(sid, def_)}
    all_dice = {**dice, **skills}

    forge_faces: dict[str, Any] = {}
    for fid, ff in raw["dice"]["forgeFaces"].items():
        if not ID_RE.match(fid):
            _fail("E_CONTENT_ID_FORMAT", fid)
        forge_faces[fid] = {"id": fid, "weight": ff["weight"], "face": ff["face"]}

    monsters: dict[str, Any] = {}
    for mid, m in raw["monsters"]["monsters"].items():
        if not ID_RE.match(mid):
            _fail("E_CONTENT_ID_FORMAT", mid)
        if m.get("school") not in SCHOOL_ENUM:
            _fail("E_CONTENT_MONSTER", f"{mid} school")
        if m.get("tier") not in ("normal", "elite", "boss"):
            _fail("E_CONTENT_MONSTER", f"{mid} tier")
        slot_sets = [p["slots"] for p in m["phases"]] if m.get("phases") else [m["slots"]]
        for ss in slot_sets:
            if not ss:
                _fail("E_CONTENT_MONSTER", f"{mid} slots empty")
            for s in ss:
                parse_constraint(s["constraint"])
                if s["effect"]["kind"] == "add_curse" and s["effect"]["dieId"] not in dice:
                    _fail("E_CONTENT_DANGLING_REF", f"{mid} → {s['effect']['dieId']}")
        monsters[mid] = {**m, "id": mid}

    groups: dict[str, Any] = {}
    for gid, g in raw["monsters"]["groups"].items():
        for member in g["members"]:
            if member not in monsters:
                _fail("E_CONTENT_DANGLING_REF", f"{gid} → {member}")
        groups[gid] = {**g, "id": gid}

    st = raw["stages"]
    for i in range(1, st["stageCount"] + 1):
        if not any(s["stage"] == i for s in st["stages"]):
            _fail("E_CONTENT_STAGES", f"missing stage {i}")
    last = next((s for s in st["stages"] if s["stage"] == st["stageCount"]), None)
    if not last or not last.get("boss"):
        _fail("E_CONTENT_STAGES", "final stage must have boss")
    for s in st["stages"]:
        if s.get("boss") and s["bossId"] not in monsters:
            _fail("E_CONTENT_DANGLING_REF", f"stage {s['stage']} boss")
        if len(s["nodeBudget"]) != s["rows"]:
            _fail("E_CONTENT_STAGES", f"stage {s['stage']} nodeBudget")

    events: dict[str, Any] = {}
    for eid, ev in raw["events"]["events"].items():
        if not ID_RE.match(eid):
            _fail("E_CONTENT_ID_FORMAT", eid)
        if len(ev["options"]) < 3:
            _fail("E_CONTENT_EVENT", f"{eid} needs ≥3 options")
        if not any(o.get("risk") == "positive" for o in ev["options"]):
            _fail("E_CONTENT_EVENT", f"{eid} needs a positive option")
        if not any(o.get("risk") == "risk" for o in ev["options"]):
            _fail("E_CONTENT_EVENT", f"{eid} needs a risky option")
        for o in ev["options"]:
            for out in o["outcomes"]:
                for eff in out["effect"]:
                    if eff.get("defId") and eff["defId"] not in all_dice:
                        _fail("E_CONTENT_DANGLING_REF", f"{eid} → {eff['defId']}")
                    if eff.get("faceId") and eff["faceId"] not in forge_faces:
                        _fail("E_CONTENT_DANGLING_REF", f"{eid} → {eff['faceId']}")
        events[eid] = {**ev, "id": eid}

    relics: dict[str, Any] = {}
    for rid, r in raw["relics"]["relics"].items():
        if not ID_RE.match(rid):
            _fail("E_CONTENT_ID_FORMAT", rid)
        relics[rid] = {**r, "id": rid}
    relic_list = list(relics.values())
    if sum(1 for r in relic_list if "school" in r["tags"]) < 6:
        _fail("E_CONTENT_RELIC_RULE", "need ≥6 school relics")
    if sum(1 for r in relic_list if "cost" in r["tags"]) < 2:
        _fail("E_CONTENT_RELIC_RULE", "need ≥2 cost relics")

    for cid, c in raw["characters"]["characters"].items():
        for d in c["startingDeck"]:
            if d not in all_dice:
                _fail("E_CONTENT_DANGLING_REF", f"{cid} → {d}")
        for s in c["startingSlots"]:
            parse_constraint(s["constraint"])

    return _deep_freeze({
        "schemaVersion": "content-1",
        "schools": raw["schools"],
        "difficulty": raw["difficulty"],
        "dice": dice, "skills": skills, "allDice": all_dice, "forgeFaces": forge_faces,
        "characters": raw["characters"]["characters"],
        "monsters": monsters, "groups": groups,
        "stages": st,
        "events": events, "relics": relics,
        "shops": raw["shops"],
        "warnings": [],
    })


def filter_by_toggles(tables: TypingMapping, toggles: TypingMapping) -> dict[str, Any]:
    """按开关过滤（relics=false 时遗物事件被过滤并记 warning；events=false 时事件池不加载）。"""
    warnings: list[str] = []
    events = tables["events"]
    relics = tables["relics"]
    if not toggles["events"]:
        events = {}
    if not toggles["relics"]:
        relics = {}
        kept: dict[str, Any] = {}
        for eid, ev in events.items():
            if ev.get("relicsOnly"):
                warnings.append(f"event {eid} filtered: relics disabled")
            else:
                kept[eid] = ev
        events = kept
    return {**tables, "events": events, "relics": relics,
            "warnings": [*tables["warnings"], *warnings]}
