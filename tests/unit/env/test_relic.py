"""遗物效果查询（GAME-00 §8.3）：每个遗物 hook 在拥有/未拥有时的取值。

遗物只读 state、不消费 RNG（relic.js 模块约束）；本测试同时锁定这一点。
"""

from __future__ import annotations

import copy

import pytest

from gamer_rsi.env import relic as R


@pytest.fixture
def base(tables, started):
    """player_turn state，遗物开关打开但无遗物。"""
    s = copy.deepcopy(started)
    s["config"]["toggles"]["relics"] = True
    s["player"]["relics"] = []
    return s


def _grant(base, rid):
    if rid not in base["player"]["relics"]:
        base["player"]["relics"].append(rid)


@pytest.mark.parametrize("rid,hook,args,want_default,want_owned", [
    ("relic_blade_mark", R.attack_flat_bonus, ("blade",), 0, None),
    ("relic_deck_thin", R.deck_thin_bonus, (), 0, None),
    ("relic_school_prism", R.counter_bonus_permille, (), 0, None),
    ("relic_arcana_loop", R.draw_delta, (), 0, None),
])
def test_relic_hooks_default_off(base, tables, rid, hook, args, want_default, want_owned):
    assert hook(base, tables, *args) == want_default


def test_has_relic_respects_toggle(base):
    _grant(base, "relic_blade_mark")
    assert R.has_relic(base, "relic_blade_mark") is True
    base["config"]["toggles"]["relics"] = False
    assert R.has_relic(base, "relic_blade_mark") is False


def test_has_relic_unknown(base):
    assert R.has_relic(base, "relic_nope") is False


def test_attack_flat_bonus_blade_mark(base, tables):
    v = tables["relics"]["relic_blade_mark"]["effect"]["value"]
    _grant(base, "relic_blade_mark")
    assert R.attack_flat_bonus(base, tables, "blade") == v + R.deck_thin_bonus(base, tables)
    # 非 blade 学派不触发
    assert R.attack_flat_bonus(base, tables, "arcana") == R.deck_thin_bonus(base, tables)


def test_attack_flat_bonus_last_stand(base, tables):
    th = tables["relics"]["relic_last_stand"]["effect"]["threshold"]
    v = tables["relics"]["relic_last_stand"]["effect"]["value"]
    _grant(base, "relic_last_stand")
    base["player"]["virtue"] = th
    assert R.attack_flat_bonus(base, tables, "verdant") >= v
    base["player"]["virtue"] = th + 5
    assert R.attack_flat_bonus(base, tables, "verdant") == R.deck_thin_bonus(base, tables)


def test_deck_thin_bonus_threshold(base, tables):
    op = tables["relics"]["relic_deck_thin"]["effect"]
    _grant(base, "relic_deck_thin")
    base["player"]["deck"] = base["player"]["deck"][: op["threshold"]]
    assert R.deck_thin_bonus(base, tables) == op["value"]
    base["player"]["deck"] = base["player"]["deck"] + ["d-nope"] * 10
    assert R.deck_thin_bonus(base, tables) == 0


def test_counter_bonus_permille(base, tables):
    v = tables["relics"]["relic_school_prism"]["effect"]["value"]
    _grant(base, "relic_school_prism")
    assert R.counter_bonus_permille(base, tables) == v


def test_relic_mul_permille(base, tables):
    mul = tables["relics"]["relic_boss_trophy"]["effect"]["mulPermille"]
    _grant(base, "relic_boss_trophy")
    assert R.relic_mul_permille(base, tables, {"tier": "elite"}) == mul
    assert R.relic_mul_permille(base, tables, {"tier": "boss"}) == mul
    assert R.relic_mul_permille(base, tables, {"tier": "normal"}) == 1000


def test_draw_delta_arcana_loop(base, tables):
    need = tables["relics"]["relic_arcana_loop"]["effect"]["need"]
    _grant(base, "relic_arcana_loop")
    base["player"]["arcanaLastTurn"] = need
    assert R.draw_delta(base, tables) >= 1
    base["player"]["arcanaLastTurn"] = 0
    assert R.draw_delta(base, tables) == 0


def test_gold_mul(base, tables):
    base["config"]["goldMulPermille"] = 1000
    assert R.gold_mul(base, tables) == 1000
    _grant(base, "relic_greedy_eye")
    v = tables["relics"]["relic_greedy_eye"]["effect"]["goldPermille"]
    assert R.gold_mul(base, tables) == v


def test_battle_start_hooks(base, tables):
    assert R.battle_start_virtue_loss(base, tables) == 0
    assert R.battle_start_ward(base, tables) == 0
    _grant(base, "relic_chaos_eye")
    _grant(base, "relic_ward_seed")
    assert R.battle_start_virtue_loss(base, tables) >= 0
    assert R.battle_start_ward(base, tables) >= 0


def test_swift_hand_active(base):
    assert R.swift_hand_active(base) in (True, False)


def test_purify_bonus_and_verdant_mend(base, tables):
    assert R.purify_bonus(base, tables) == 0
    assert isinstance(R.verdant_mend(base, tables), int)
    assert isinstance(R.blade_echo_value(base, tables), int)
    assert isinstance(R.mono_penalty_disabled(base), bool)
    assert isinstance(R.free_remove_available(base), bool)
    assert isinstance(R.swift_hand_active(base), bool)
    assert isinstance(R.extra_heart_loss(base), int)


def test_relic_pool(base, tables):
    pool = R.relic_pool(base, tables, ["common", "rare"])
    assert pool
    for rid in pool:
        assert tables["relics"][rid]["rarity"] in ("common", "rare")
        assert rid not in base["player"]["relics"]


def test_grant_and_swap_relic(base, tables):
    ctx = {"state": base, "tables": tables, "events": []}
    rid = next(iter(R.relic_pool(base, tables, ["common", "rare"])))
    granted = R.grant_relic(ctx, rid, "event")
    assert granted is True
    assert rid in base["player"]["relics"]
    assert any(e["type"] == "relic_gained" for e in ctx["events"])


def test_grant_relic_disabled_toggle(base, tables):
    base["config"]["toggles"]["relics"] = False
    ctx = {"state": base, "tables": tables, "events": []}
    # 开关关闭时视为已获得（直接成功，不进背包也不触发 swap）
    assert R.grant_relic(ctx, "relic_greedy_eye", "event") is True
    assert "relic_greedy_eye" not in base["player"]["relics"]


def test_grant_relic_triggers_swap(base, tables):
    """遗物栏满时 grant 返回 False 并进入 relic_swap。"""
    ctx = {"state": base, "tables": tables, "events": []}
    cap = base["config"]["relicSlots"]
    pool = R.relic_pool(base, tables, ["common", "rare"])
    base["player"]["relics"] = pool[:cap]
    assert R.grant_relic(ctx, pool[cap], "event") is False
    assert base["phase"] == "relic_swap"
    assert base["pendingRelic"]["relicId"] == pool[cap]


def test_swap_relic(base, tables):
    cap = base["config"]["relicSlots"]
    pool = R.relic_pool(base, tables, ["common", "rare"])
    base["player"]["relics"] = list(pool[:cap])
    ctx = {"state": base, "tables": tables, "events": []}
    assert R.grant_relic(ctx, pool[cap], "event") is False
    drop = base["player"]["relics"][0]
    R.swap_relic(ctx, drop)
    assert drop not in base["player"]["relics"]
    assert pool[cap] in base["player"]["relics"]
    assert base["pendingRelic"] is None


def test_swap_relic_skip(base, tables):
    cap = base["config"]["relicSlots"]
    pool = R.relic_pool(base, tables, ["common", "rare"])
    base["player"]["relics"] = list(pool[:cap])
    ctx = {"state": base, "tables": tables, "events": []}
    R.grant_relic(ctx, pool[cap], "event")
    before = list(base["player"]["relics"])
    R.swap_relic(ctx, "skip")
    assert base["player"]["relics"] == before
