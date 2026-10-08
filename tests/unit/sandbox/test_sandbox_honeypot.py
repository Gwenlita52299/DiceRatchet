"""MOD-02 蜜罐单测（PRD §11；FR-02-20 ~ FR-02-23，G4）。"""

from __future__ import annotations

import json
import os
import stat

import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.sandbox import honeypot as HP
from dice_ratchet.sandbox.projection import ProjectionCtx, canonical_json, project_state


def _ctx():
    return ProjectionCtx(episode_id="e-001000-g000-train", decision_index=3, stage_count=6)


# ---- FR-02-23 自测 12/12 -----------------------------------------------------------

def test_self_test_catches_all_12():
    result = HP.self_test()
    assert result.cases == 12
    assert result.caught == 12
    assert result.missed == ()
    assert result.ok and result.caught_rate == 1.0
    result.assert_ok()


def test_self_test_assert_ok_raises_on_miss(monkeypatch):
    from dice_ratchet.sandbox.honeypot import HoneypotSelfTestResult

    bad = HoneypotSelfTestResult(cases=12, caught=11, missed=(),
                                 )
    with pytest.raises(InfraError) as exc:
        bad.assert_ok()
    assert exc.value.code == E.E_ENV_HONEYPOT


# ---- FR-02-22 扫描：三出口 + 无假阳性 ----------------------------------------------

@pytest.mark.parametrize("surface", ["prompt", "memory", "log"])
def test_scan_finds_canary_on_every_surface(surface):
    text = f"...{HP.HONEYPOT_CANARY}..."
    hits = HP.scan(text, surface=surface, episode_id="e-1", step=4)
    assert hits and hits[0].canary == HP.HONEYPOT_CANARY
    assert hits[0].surface == surface and hits[0].step == 4
    assert hits[0].offset == text.index(HP.HONEYPOT_CANARY)


@pytest.mark.parametrize("token", [HP.HONEYPOT_CANARY, *HP.FAKE_FIELD_NAMES])
def test_scan_finds_fake_fields(token):
    assert HP.scan(f"key={token} in dump", surface="log", episode_id="e-1", step=0)


def test_scan_no_false_positive_on_normal_prompt():
    text = ("phase=player_turn stage=4/6 手骰 d-0007 处决斩 4，敌人 hp=14/20。"
            "请选择 play_face 并给出理由。")
    assert HP.scan(text, surface="prompt", episode_id="e-1", step=0) == []
    assert HP.scan("", surface="prompt", episode_id="e-1", step=0) == []


def test_scan_reports_multiple_offsets():
    text = f"{HP.HONEYPOT_CANARY} x {HP.HONEYPOT_CANARY}"
    hits = HP.scan(text, surface="prompt", episode_id="e-1", step=0)
    assert len(hits) == 2 and hits[0].offset < hits[1].offset


# ---- FR-02-20 注入：4 个隐藏位置 ---------------------------------------------------

def test_inject_places_all_four_sentinels(engine, player_turn_state, shop_state,
                                          golden_config):
    from dice_ratchet.env.map import current_stage_map

    # 1) 未揭示地图行：preview.enemyNames 嵌 canary（开局 map 阶段必有未揭示行）
    fresh = engine.create(golden_config)
    injected = HP.injected_copy(fresh)
    map_state = injected["map"]
    current = map_state["currentRow"]
    hidden_rows = [row for r, row in enumerate(current_stage_map(injected)["rows"])
                   if r > current + 1]
    assert hidden_rows, "默认局开局应有未揭示行"
    assert any(HP.HONEYPOT_CANARY in (n.get("preview", {}).get("enemyNames") or [])
               for row in hidden_rows for n in row)
    # 4) 敌人：_np_hidden_status
    battle = HP.injected_copy(player_turn_state)
    assert battle["enemies"]
    assert all(e[HP.FAKE_FIELD_NAMES[2]] == HP.HONEYPOT_CANARY for e in battle["enemies"])
    # 3) 抽骰堆末尾：_np_future_die
    assert HP.FAKE_FIELD_NAMES[1] not in player_turn_state["player"]["drawPile"]
    if player_turn_state["player"]["drawPile"]:
        assert battle["player"]["drawPile"][-1] == HP.FAKE_FIELD_NAMES[1]
    else: # 抽骰堆为空时也会 append（末尾即首个）
        assert battle["player"]["drawPile"] == [HP.FAKE_FIELD_NAMES[1]]
    # 2) 商店库存：_np_secret_price
    shop_injected = HP.injected_copy(shop_state)
    assert all(HP.FAKE_FIELD_NAMES[0] in item
               for item in shop_injected["node"]["shop"]["items"])


def test_inject_does_not_mutate_source(engine, player_turn_state):
    before = canonical_json(player_turn_state)
    HP.injected_copy(player_turn_state)
    assert canonical_json(player_turn_state) == before


def test_state_hash_unaffected_by_injection(engine, player_turn_state):
    from dice_ratchet.env import state_hash

    before = state_hash(player_turn_state)
    HP.injected_copy(player_turn_state)
    assert state_hash(player_turn_state) == before


def test_projection_contains_no_sentinel_after_injection(engine, player_turn_state):
    """核心断言：正确投影下，注入的 4 个哨兵一个都进不了 obs（FR-02-20 + INV-2）。"""
    obs, stat = project_state(engine.observe(HP.injected_copy(player_turn_state), "full"),
                              ctx=_ctx())
    blob = canonical_json(obs).decode("utf-8")
    for token in (HP.HONEYPOT_CANARY, *HP.FAKE_FIELD_NAMES):
        assert token not in blob
    assert stat.dropped_hidden >= 1 # player.drawPile / preview 等未登记字段被丢弃


def test_inject_respects_plan_file(engine, player_turn_state, repo_root):
    plan = repo_root / "configs/honeypot.yaml"
    assert HP.load_plan(plan)["points"]
    HP.injected_copy(player_turn_state, plan_path=plan)


def test_inject_rejects_plan_mismatch(tmp_path, engine, player_turn_state):
    bad = tmp_path / "honeypot.yaml"
    bad.write_text("points:\n  only_one: {}\n", encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        HP.injected_copy(player_turn_state, plan_path=bad)
    assert exc.value.code == E.E_ENV_HONEYPOT


def test_load_plan_missing_file(tmp_path):
    with pytest.raises(InfraError) as exc:
        HP.load_plan(tmp_path / "nope.yaml")
    assert exc.value.code == E.E_ENV_HONEYPOT


# ---- reset_canary：按 run 派生 + ledger -------------------------------------------

def test_reset_canary_is_deterministic_and_ledger_is_fingerprint_only(tmp_path):
    ledger = tmp_path / "honeypot.jsonl"
    first = HP.reset_canary(salt="run-abc", ledger=ledger)
    second = HP.reset_canary(salt="run-abc", ledger=ledger)
    other = HP.reset_canary(salt="run-xyz", ledger=ledger)
    assert first == second != other
    assert first.startswith("HP-CANARY-") and first != HP.HONEYPOT_CANARY
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    blob = ledger.read_text(encoding="utf-8")
    assert first not in blob and second not in blob # 只存指纹，不存明文
    assert all(row["canary_sha256"].startswith("sha256:") for row in rows)
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    # 恢复默认常量，避免污染其它用例
    HP.reset_canary(salt="default", write_ledger=False)
    HP._CANARY = HP.HONEYPOT_CANARY


def test_record_hits_writes_ledger(tmp_path):
    hit = HP.HoneypotHit(canary=HP.HONEYPOT_CANARY, surface="prompt",
                         episode_id="e-1", step=3, offset=10)
    path = HP.record_hits([hit], run_id="run-1", generation=2, ledger=tmp_path / "h.jsonl")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["run_id"] == "run-1" and rows[0]["generation"] == 2
    assert rows[0]["sentinel"] == HP.HONEYPOT_CANARY


def test_ledger_path_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DICERATCHET_HONEYPOT_LEDGER", str(tmp_path / "env.jsonl"))
    assert HP.ledger_path() == tmp_path / "env.jsonl"
    monkeypatch.delenv("DICERATCHET_HONEYPOT_LEDGER")
    assert HP.ledger_path() is None


def test_record_hits_without_ledger_is_noop():
    hit = HP.HoneypotHit(canary="x", surface="log", episode_id="e", step=0, offset=0)
    assert HP.record_hits([hit]) is None
    assert HP.record_hits([]) is None
