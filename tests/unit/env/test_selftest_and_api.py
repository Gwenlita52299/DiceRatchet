"""自检入口（PRD G6）与引擎辅助 API（play_hints / debug_start_battle / replay）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.env import GameEngine, digest
from dice_ratchet.env import selftest as ST
from dice_ratchet.env.error import KernelError
from dice_ratchet.env.state import assert_invariants, is_mono

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_selftest_passes():
    assert ST.run_selftest(seed=1) == 0


def test_selftest_cli_path(engine, tables):
    """CLI 命令实际可执行（G6：可直接复制执行）。"""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-m", "dice_ratchet", "env", "selftest", "--seed", "1"],
                          cwd=_REPO_ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "selftest 全部通过" in proc.stdout


def test_play_hints_structure(engine, phases, tables):
    state = phases("player_turn")
    hints = engine.play_hints(state)
    assert hints is not None
    p = state["player"]
    for die_id in p["hand"]:
        assert die_id in hints
        for s in p["slots"]:
            h = hints[die_id][s["slotId"]]
            assert set(h.keys()) == {"legal", "code", "reason", "needsTarget", "previews"}
            if h["legal"]:
                assert h["code"] is None
            else:
                assert h["code"]
        # 弃置代价（魔化骰才有）
        assert isinstance(hints[die_id]["__discardCost"], int)


def test_play_hints_only_in_player_turn(engine, phases):
    assert engine.play_hints(phases("map")) is None


def test_play_hints_purge_previews(engine, phases, tables):
    """purge 面对每个敌人给预览值；ward/mend 给自身预览。"""
    state = phases("player_turn")
    hints = engine.play_hints(state)
    p = state["player"]
    for die_id in p["hand"]:
        from dice_ratchet.env.state import current_face
        kind = current_face(state, tables, die_id)["kind"]
        for s in p["slots"]:
            h = hints[die_id][s["slotId"]]
            if h["legal"] and kind == "purge":
                assert h["previews"] is not None and len(h["previews"]) == len(state["enemies"])
                assert all("enemyId" in pv and "value" in pv for pv in h["previews"])
            elif h["legal"] and kind in ("ward", "mend"):
                assert h["previews"] and h["previews"][0]["enemyId"] is None


def test_play_hints_matches_legal_actions(engine, phases):
    """hints 标记合法的 (die, slot) 组合，与 legal_actions 的 play_face 集合一致。"""
    state = phases("player_turn")
    hints = engine.play_hints(state)
    legal = engine.legal_actions(state)
    plays = {(a["dieId"], a["slotId"]) for a in legal if a["kind"] == "play_face"}
    hinted_ok = {(d, s) for d in hints for s in hints[d]
                 if s != "__discardCost" and hints[d][s]["legal"]}
    assert hinted_ok == plays


def test_debug_start_battle(engine, tables):
    """debug_start_battle 直接开打（测试/平衡脚本用）。"""
    state = engine.create({"difficulty": "medium", "seed": 7})
    s2 = engine.reduce(state, {"kind": "map_choose", "nodeId": "n-01-0-0"})["state"]
    member = s2["enemies"][0]["defId"] if s2["enemies"] else "mon_verdant_slime"
    from dice_ratchet.env.engine import debug_start_battle
    s3 = debug_start_battle(engine.create({"difficulty": "medium", "seed": 7}),
                            [member], tables, "elite")
    assert s3["phase"] == "player_turn"
    assert s3["node"]["nodeId"] == "n-debug"
    assert s3["enemies"]


def test_replay_reproduces_golden(engine):
    from dice_ratchet.env.engine import observe, replay
    path = _REPO_ROOT / "src/game/tests/golden/g2_all_on_medium.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    actions = [l["action"] for l in lines[1:] if l.get("action")]
    r = replay(lines[0]["config"], actions, engine._tables)
    want = [l["obsDigest"] for l in lines[1:] if l.get("step") is not None]
    assert r["digests"] == want
    assert r["outcome"] == lines[-1].get("outcome")


def test_native_result_fields(engine, phases):
    from dice_ratchet.env.engine import native_result
    nr = native_result(phases("player_turn"))
    assert set(nr.keys()) == {"outcome", "truncationReason", "stagesCleared",
                              "heartsLeft", "battlesWon", "turns", "kills"}


def test_config_schema(engine, tables):
    from dice_ratchet.env.engine import config_schema
    schema = config_schema(tables)
    assert set(schema.keys()) == {"difficulties", "difficultyKeys", "toggles", "stageCounts",
                                  "characters", "enums", "schools", "defaults"}
    assert {t["id"] for t in schema["toggles"]} == {"mapBranching", "relics", "events",
                                                    "shops", "schoolCounter"}
    assert "actionKinds" in schema["enums"]
    assert schema["stageCounts"] == sorted(schema["stageCounts"])


def test_state_hash_and_digest(engine):
    state = engine.create({"difficulty": "easy", "seed": 3})
    from dice_ratchet.env.engine import state_hash
    h = state_hash(state)
    assert h.startswith("sha256:") and len(h) == len("sha256:") + 64
    assert state_hash(state) == h


def test_assert_invariants_catches_violations(engine, tables):
    """违反 hand 上限 / 重复骰子 / deck 不一致时抛 KernelError。"""
    state = engine.create({"difficulty": "medium", "seed": 1})
    p = state["player"]
    a_die = p["hand"][0] if p["hand"] else p["deck"][0]

    # hand 超上限
    p["hand"] = [*p["hand"], "d-9999"]
    with pytest.raises(KernelError) as excinfo:
        assert_invariants(state)
    assert excinfo.value.code == "E_KERNEL_INVARIANT"
    p["hand"] = p["hand"][:-1]

    # 牌堆里出现重复（create 时牌堆为空，骰子都在 deck；塞两颗一样的）
    p["drawPile"] = [a_die, a_die]
    with pytest.raises(KernelError):
        assert_invariants(state)
    p["drawPile"] = []

    # 非法 faceIndex
    p["dice"][a_die]["faceIndex"] = 9
    with pytest.raises(KernelError):
        assert_invariants(state)
    p["dice"][a_die]["faceIndex"] = 0

    # 浮点数污染（JS Number 精度风险的兜底）
    p["virtue"] = 1.5
    with pytest.raises(KernelError):
        assert_invariants(state)

    # 牌堆里出现不在 deck 的骰子
    p["discardPile"] = [*p["discardPile"], "d-8888"]
    with pytest.raises(KernelError):
        assert_invariants(state)


def test_deck_school_info(tables, started):
    from dice_ratchet.env.state import deck_school_info
    info = deck_school_info(started, tables)
    assert set(info.keys()) == {"school", "counts", "total", "concentrationPermille"}
    assert info["school"] in ("blade", "arcana", "verdant", "neutral")
    assert sum(info["counts"].values()) == info["total"]
    assert info["concentrationPermille"] <= 1000
