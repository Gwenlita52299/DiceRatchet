"""配表加载/校验/冻结（GAME-01 §1.1 / §8）—— 用篡改后的原始 JSON 触发各校验分支。

真实配表必须是干净的（``load_tables()`` 正常返回）；本文件用副本逐项破坏，锁定每条
``E_CONTENT_*`` 校验与 JS 侧同语义。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from dice_ratchet.env.error import KernelError
from dice_ratchet.env.loader import (DEFAULT_CONTENT_DIR, TABLE_FILES, build_tables,
                                 filter_by_toggles, load_tables)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _raw() -> dict:
    raw: dict[str, object] = {}
    for name in TABLE_FILES:
        raw[name] = json.loads((DEFAULT_CONTENT_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return raw


@pytest.fixture(scope="module")
def tables():
    return load_tables()


def test_real_tables_load_clean():
    t = load_tables()
    assert t["schemaVersion"] == "content-1"
    assert set(TABLE_FILES) <= set(t)
    assert not t["warnings"]  # 冻结后为空 tuple


def test_load_tables_missing_dir():
    # 文件不存在时是原生异常（JS 侧 readFileSync 同样抛 ENOENT），交给边界映射
    with pytest.raises(FileNotFoundError):
        load_tables(Path("/nonexistent/content"))


def test_load_tables_missing_schema_version(tmp_path):
    for name in TABLE_FILES:
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"$schemaVersion": "content-1"}), encoding="utf-8")
    (tmp_path / "dice.json").write_text(json.dumps({"dice": {}}), encoding="utf-8")
    with pytest.raises(KernelError) as excinfo:
        load_tables(tmp_path)
    assert excinfo.value.code == "E_CONTENT_SCHEMA"


def _mutate(tables_key, path, value):
    raw = _raw()
    cur = raw[tables_key]
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return raw


def _expect_code(raw, code):
    with pytest.raises(KernelError) as excinfo:
        build_tables(raw)
    assert excinfo.value.code == code, f"期望 {code}，实际 {excinfo.value.code}"


def test_validate_die_face_index(raw=_raw()):
    r = copy.deepcopy(raw)
    r["dice"]["dice"]["die_strike"]["faces"][0]["faceIndex"] = 3
    _expect_code(r, "E_CONTENT_FACES")


def test_validate_die_face_kind():
    r = _mutate("dice", ["dice", "die_strike", "faces", 0, "kind"], "nope")
    _expect_code(r, "E_CONTENT_FACE_KIND")


def test_validate_die_bad_status_kind():
    r = _mutate("dice", ["dice", "die_strike", "faces", 0, "statusKind"], "nope")
    # 先把面类型改成 status，否则 statusKind 不被校验
    r["dice"]["dice"]["die_strike"]["faces"][0]["kind"] = "status"
    _expect_code(r, "E_CONTENT_FACE_KIND")


def test_validate_die_bad_school():
    r = _mutate("dice", ["dice", "die_strike", "faces", 0, "school"], "nope")
    _expect_code(r, "E_CONTENT_FACE_KIND")


def test_validate_die_bad_value():
    r = _mutate("dice", ["dice", "die_strike", "faces", 0, "value"], "x")
    _expect_code(r, "E_CONTENT_FACE_KIND")


def test_validate_die_face_count():
    r = _raw()
    r["dice"]["dice"]["die_strike"]["faces"] = r["dice"]["dice"]["die_strike"]["faces"][:5]
    _expect_code(r, "E_CONTENT_FACES")


def test_validate_die_bad_id():
    r = _raw()
    die = r["dice"]["dice"].pop("die_strike")
    r["dice"]["dice"]["BAD-ID"] = die
    _expect_code(r, "E_CONTENT_ID_FORMAT")


def test_validate_die_risk_mismatch():
    r = _mutate("dice", ["dice", "die_curse_static", "riskTier"], "safe")
    _expect_code(r, "E_CONTENT_RISK_MISMATCH")


def test_validate_die_school_rule():
    r = _raw()
    # 把技能骰的 own 面降到 <2，触发 own<2 校验（保持 category=skill）
    did = next(did for did, d in r["skills"]["skills"].items()
               if d["category"] == "skill" and d["school"] != "neutral")
    own_school = r["skills"]["skills"][did]["school"]
    for f in r["skills"]["skills"][did]["faces"]:
        if f["school"] == own_school:
            f["school"] = "neutral"
    _expect_code(r, "E_CONTENT_SCHOOL_RULE")


def test_validate_forge_face_bad_id():
    r = _raw()
    ff = r["dice"]["forgeFaces"].pop(next(iter(r["dice"]["forgeFaces"])))
    r["dice"]["forgeFaces"]["BAD-FACE"] = ff
    _expect_code(r, "E_CONTENT_ID_FORMAT")


def test_validate_monster_bad_id():
    r = _raw()
    mid = next(iter(r["monsters"]["monsters"]))
    m = r["monsters"]["monsters"].pop(mid)
    r["monsters"]["monsters"]["BAD-MON"] = m
    _expect_code(r, "E_CONTENT_ID_FORMAT")


def test_validate_monster_bad_school():
    first = next(iter(_raw()["monsters"]["monsters"]))
    r = _mutate("monsters", ["monsters", first, "school"], "nope")
    _expect_code(r, "E_CONTENT_MONSTER")


def test_validate_monster_bad_tier():
    first = next(iter(_raw()["monsters"]["monsters"]))
    r = _mutate("monsters", ["monsters", first, "tier"], "nope")
    _expect_code(r, "E_CONTENT_MONSTER")


def test_validate_monster_empty_slots():
    first = next(iter(_raw()["monsters"]["monsters"]))
    r = _mutate("monsters", ["monsters", first, "slots"], [])
    _expect_code(r, "E_CONTENT_MONSTER")


def test_validate_monster_bad_slot_constraint():
    first = next(iter(_raw()["monsters"]["monsters"]))
    r = _mutate("monsters", ["monsters", first, "slots", 0, "constraint"], "nope:1")
    _expect_code(r, "E_KERNEL_CONSTRAINT_SYNTAX")


def test_validate_monster_dangling_curse_ref():
    first = next(iter(_raw()["monsters"]["monsters"]))
    r = _raw()
    # mon_arcana_mirror 的 add_curse 槽引用 die_curse_static
    slots = r["monsters"]["monsters"]["mon_arcana_mirror"]["slots"]
    next(s for s in slots if s["effect"]["kind"] == "add_curse")["effect"]["dieId"] = "die_nope"
    _expect_code(r, "E_CONTENT_DANGLING_REF")


def test_validate_group_dangling_member():
    gid = next(iter(_raw()["monsters"]["groups"]))
    r = _mutate("monsters", ["groups", gid, "members", 0], "mon_nope")
    _expect_code(r, "E_CONTENT_DANGLING_REF")


def test_validate_stages_missing():
    r = _raw()
    r["stages"]["stageCount"] = r["stages"]["stageCount"] + 1
    _expect_code(r, "E_CONTENT_STAGES")


def test_validate_stages_final_not_boss():
    r = _raw()
    last = next(s for s in r["stages"]["stages"] if s["stage"] == r["stages"]["stageCount"])
    last.pop("boss")
    _expect_code(r, "E_CONTENT_STAGES")


def test_validate_stages_dangling_boss():
    r = _raw()
    boss_stage = next(s for s in r["stages"]["stages"] if s.get("boss"))
    boss_stage["bossId"] = "mon_nope"
    _expect_code(r, "E_CONTENT_DANGLING_REF")


def test_validate_stages_node_budget_length():
    r = _raw()
    s = r["stages"]["stages"][0]
    s["nodeBudget"] = s["nodeBudget"][:-1]
    _expect_code(r, "E_CONTENT_STAGES")


def test_validate_event_bad_id():
    eid = next(iter(_raw()["events"]["events"]))
    r = _raw()
    ev = r["events"]["events"].pop(eid)
    r["events"]["events"]["BAD-EVENT"] = ev
    _expect_code(r, "E_CONTENT_ID_FORMAT")


def test_validate_event_too_few_options():
    eid = next(iter(_raw()["events"]["events"]))
    r = _mutate("events", ["events", eid, "options"],
                _raw()["events"]["events"][eid]["options"][:2])
    _expect_code(r, "E_CONTENT_EVENT")


def test_validate_event_no_positive_option():
    eid = next(iter(_raw()["events"]["events"]))
    r = _raw()
    for o in r["events"]["events"][eid]["options"]:
        o["risk"] = "risk"
    _expect_code(r, "E_CONTENT_EVENT")


def test_validate_event_no_risk_option():
    eid = next(iter(_raw()["events"]["events"]))
    r = _raw()
    for o in r["events"]["events"][eid]["options"]:
        o["risk"] = "positive"
    _expect_code(r, "E_CONTENT_EVENT")


def test_validate_event_dangling_die_ref():
    r = _raw()
    target = None
    for ev in r["events"]["events"].values():
        for o in ev["options"]:
            for out in o["outcomes"]:
                for eff in out["effect"]:
                    if eff.get("op") in ("gain_die", "add_curse") and eff.get("defId"):
                        target = eff
                        break
    if target is None:
        pytest.skip("无带 defId 的事件效果")
    target["defId"] = "die_nope"
    _expect_code(r, "E_CONTENT_DANGLING_REF")


def test_validate_event_dangling_face_ref():
    r = _raw()
    target = None
    for ev in r["events"]["events"].values():
        for o in ev["options"]:
            for out in o["outcomes"]:
                for eff in out["effect"]:
                    if eff.get("faceId"):
                        target = eff
                        break
    if target is None:
        pytest.skip("无带 faceId 的事件效果")
    target["faceId"] = "ff_nope"
    _expect_code(r, "E_CONTENT_DANGLING_REF")


def test_validate_relic_bad_id():
    rid = next(iter(_raw()["relics"]["relics"]))
    r = _raw()
    rl = r["relics"]["relics"].pop(rid)
    r["relics"]["relics"]["BAD-RELIC"] = rl
    _expect_code(r, "E_CONTENT_ID_FORMAT")


def test_validate_relic_school_rule():
    r = _raw()
    # 删掉所有 school 标签遗物 → < 6
    for rl in r["relics"]["relics"].values():
        rl["tags"] = [t for t in rl["tags"] if t != "school"]
    _expect_code(r, "E_CONTENT_RELIC_RULE")


def test_validate_relic_cost_rule():
    r = _raw()
    for rl in r["relics"]["relics"].values():
        rl["tags"] = [t for t in rl["tags"] if t != "cost"]
    _expect_code(r, "E_CONTENT_RELIC_RULE")


def test_validate_character_dangling_die():
    cid = next(iter(_raw()["characters"]["characters"]))
    r = _mutate("characters", ["characters", cid, "startingDeck", 0], "die_nope")
    _expect_code(r, "E_CONTENT_DANGLING_REF")


def test_validate_character_bad_slot_constraint():
    cid = next(iter(_raw()["characters"]["characters"]))
    r = _mutate("characters", ["characters", cid, "startingSlots", 0, "constraint"], "nope")
    _expect_code(r, "E_KERNEL_CONSTRAINT_SYNTAX")


def test_tables_are_deep_frozen(tables):
    import types

    def check(o, path):
        if isinstance(o, dict):
            assert isinstance(o, types.MappingProxyType), f"{path} 未冻结"
            for k, v in o.items():
                check(v, f"{path}.{k}")
        elif isinstance(o, list):
            assert isinstance(o, tuple), f"{path} 列表未冻结为 tuple"
            for i, v in enumerate(o):
                check(v, f"{path}[{i}]")

    check(tables, "tables")


def test_filter_by_toggles(tables):
    full = filter_by_toggles(tables, {"events": True, "relics": True})
    assert len(full["events"]) == len(tables["events"])
    assert full["warnings"] == []

    no_events = filter_by_toggles(tables, {"events": False, "relics": True})
    assert no_events["events"] == {}

    no_relics = filter_by_toggles(tables, {"events": True, "relics": False})
    assert no_relics["relics"] == {}
    filtered = [e for e in tables["events"].values() if e.get("relicsOnly")]
    if filtered:
        assert len(no_relics["events"]) == len(tables["events"]) - len(filtered)
        assert any("filtered: relics disabled" in w for w in no_relics["warnings"])
    else:
        assert len(no_relics["events"]) == len(tables["events"])


def test_compute_risk_tier_exported(tables):
    from dice_ratchet.env.loader import compute_risk_tier

    did = next(iter(tables["dice"]))
    assert compute_risk_tier(tables["dice"][did]) in ("safe", "balanced", "risky", "cursed")
