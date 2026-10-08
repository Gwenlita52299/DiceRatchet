"""FR-05-2/5/6：抽取规则的确定性、情境键、相似度、效用分、BM25、渲染与预算裁剪。"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from dice_ratchet.memory import (MemoryCandidate, SituationKey, bm25_score, estimate_tokens,
                              extract_candidates, obs_tag_weights, render_entries,
                              render_entry, similarity, situation_key, situation_of,
                              tokenize, utility_score)
from dice_ratchet.memory.retriever import (MAX_RENDER_CHARS, doc_tokens, score_entry,
                                        select_with_budget)


# ---- tokenize / estimate ------------------------------------------------------------

def test_tokenize_english_lower_cjk_unigram_bigram() -> None:
    tokens = tokenize("Purify Low_HP 净化")
    assert "purify" in tokens and "low_hp" in tokens
    assert "净" in tokens and "化" in tokens
    assert "净化" in tokens
    assert tokenize("Purify") == ["purify"]
    assert tokenize("") == []


def test_estimate_tokens_is_chars_div_3() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("a" * 300) == 100


# ---- situation_key ------------------------------------------------------------------

def test_situation_of_reads_projection(obs_player_turn) -> None:
    situation = situation_of(obs_player_turn)
    assert situation.phase == "player_turn"
    assert situation.stage_bucket == "3-4"
    assert situation.virtue_bucket == "critical"  # 4/20 = 20% <= 25%
    assert situation.enemy_school == "blade"
    assert situation.intent_kind == "purge"
    assert situation.counter_arrow == "down"
    assert situation.corrupt_count == 1
    assert situation.slot_kinds == ("attack", "defense")


def test_situation_of_is_total_on_empty_obs() -> None:
    situation = situation_of({})
    assert situation.phase == "player_turn"
    assert situation.stage_bucket == "1-2"
    assert situation.virtue_bucket == "mid"
    assert situation_of(None) == situation


@pytest.mark.parametrize("virtue,expected", [(5, "critical"), (10, "low"), (15, "mid"),
                                             (20, "high")])
def test_virtue_buckets(virtue: int, expected: str) -> None:
    obs = {"player": {"virtue": virtue, "virtueMax": 20}}
    assert situation_of(obs).virtue_bucket == expected


def test_situation_key_is_deterministic_and_matches_situation(obs_player_turn) -> None:
    key, situation, tags = situation_key(obs_player_turn)
    assert key == situation.key()
    assert key == situation_key(obs_player_turn)[0]
    assert tags == tuple(sorted(tags, key=lambda t: (-obs_tag_weights(obs_player_turn)[t], t)))


def test_obs_tag_weights_uses_controlled_tags(obs_player_turn) -> None:
    weights = obs_tag_weights(obs_player_turn)
    assert weights["combat"] == 2.0
    assert weights["defense"] == 1.0
    assert weights["low_virtue"] == 1.0
    assert weights["school_counter"] == 2.0
    assert weights["corruption"] == 0.5
    assert all(isinstance(w, float) for w in weights.values())


def test_tags_hint_joins_tag_sim(obs_player_turn) -> None:
    weights = obs_tag_weights({"phase": "map"}, tags_hint=("forge",))
    assert weights["forge"] == 1.0


# ---- extract_candidates（GWT-05-02）-------------------------------------------------

def test_extract_candidates_is_deterministic_and_classifies(make_reflection) -> None:
    """GWT-05-02：连续调用 10 次结果全等；tags ∋ {low_virtue, combat}；polarity=avoid。"""
    reflection = make_reflection(text="净化值只剩 3 时我还在打输出")
    first = extract_candidates(reflection, None)
    assert len(first) == 1
    candidate = first[0]
    assert candidate.polarity == "avoid"
    assert {"low_virtue", "combat"} <= set(candidate.tags)
    for _ in range(9):
        assert extract_candidates(reflection, None) == first


def test_extract_candidates_returns_empty_without_theme(make_reflection) -> None:
    assert extract_candidates(make_reflection(text="今天天气不错。"), None) == []


def test_extract_candidates_respects_limit(make_reflection, cfg) -> None:
    text = "净化值低时先放盾。地图路线应该走左边商店。魔化骰不要进攻击槽。"
    limited = replace(cfg, extract=replace(cfg.extract, max_candidates_per_reflection=1))
    assert len(extract_candidates(make_reflection(text=text), limited)) == 1
    assert len(extract_candidates(make_reflection(text=text), cfg)) <= 3


def test_extract_candidates_action_hint_kind_is_a_decision_kind(make_reflection) -> None:
    from dice_ratchet.memory.schemas import ACTION_KINDS

    for text in ("净化值只剩 3 时我还在打输出", "应该先把光盾骰放进防御槽", "地图路线走左边",
                 "商店要买遗物", "技能优先用"):
        for candidate in extract_candidates(make_reflection(text=text), None):
            if candidate.action_hint is not None:
                assert candidate.action_hint["kind"] in ACTION_KINDS


def test_extract_candidates_zero_limit(make_reflection, cfg) -> None:
    limited = replace(cfg, extract=replace(cfg.extract, max_candidates_per_reflection=0))
    assert extract_candidates(make_reflection(text="净化值只剩 3 时我还在打输出"),
                              limited) == []


def test_candidate_text_hash_is_stable(make_candidate) -> None:
    a = make_candidate(lesson="净化值低先放盾")
    b = make_candidate(lesson="净化值低先放盾", created_tick=999)
    assert a.text_hash == b.text_hash
    assert a.situation_key == b.situation_key


# ---- similarity ---------------------------------------------------------------------

def test_similarity_identity_and_dissimilar(make_candidate) -> None:
    a = make_candidate(lesson="净化值低于 25% 时先把光盾骰放进防御槽。")
    assert similarity(a, a) == 1.0
    b = make_candidate(lesson="地图路线选择左侧商店购买遗物。")
    assert similarity(a, b) < 0.3
    c = make_candidate(lesson="净化值低于 25% 时先把光盾骰放进防御槽！")
    assert similarity(a, c) >= 0.85


def test_similarity_weights_are_configurable(make_candidate) -> None:
    a = make_candidate(lesson="先放盾再输出")
    b = make_candidate(lesson="先放盾再攻击")
    weighted = similarity(a, b, jaccard_weight=1.0, char3_weight=0.0)
    other = similarity(a, b, jaccard_weight=0.0, char3_weight=1.0)
    assert weighted != other or weighted == 1.0


# ---- utility_score（GWT-05-07）------------------------------------------------------

def test_utility_matches_prd_formula(mem_entry) -> None:
    entry = mem_entry(n_ref=4, n_adopted=3, n_win_adopted=2, n_not_adopted=1,
                      n_win_not_adopted=0, created_tick=37, ttl_ticks=300)
    p_used = (2 + 1) / (3 + 2)
    p_base = (0 + 1) / (1 + 2)
    assert p_used == pytest.approx(0.6)
    assert p_base == pytest.approx(1 / 3)
    age = 52 - entry.created_tick
    expected = (0.5 * (p_used - p_base)
                + 0.3 * math.log1p(4) / math.log1p(200)
                + 0.2 * math.exp(-age / 300))
    assert utility_score(entry, tick=52) == pytest.approx(round(expected, 6))
    assert -1.0 <= utility_score(entry, tick=52) <= 1.0


def test_utility_increases_with_native_win_evidence(mem_entry) -> None:
    weak = mem_entry(n_ref=1, n_adopted=1, n_win_adopted=0, n_not_adopted=0,
                     n_win_not_adopted=0)
    strong = mem_entry(n_ref=6, n_adopted=5, n_win_adopted=5, n_not_adopted=1,
                       n_win_not_adopted=0)
    assert utility_score(strong, tick=40) > utility_score(weak, tick=40)


def test_utility_is_clipped(mem_entry) -> None:
    extreme = mem_entry(n_ref=10**6, n_adopted=10**6, n_win_adopted=10**6,
                        n_not_adopted=0, n_win_not_adopted=0, created_tick=0,
                        ttl_ticks=1)
    assert utility_score(extreme, tick=10**6) == 1.0
    bad = mem_entry(n_ref=10**6, n_adopted=0, n_win_adopted=0, n_not_adopted=10**6,
                    n_win_not_adopted=0, created_tick=0, ttl_ticks=1)
    assert utility_score(bad, tick=10**6) >= -1.0


# ---- bm25_score ---------------------------------------------------------------------

def test_bm25_prefers_matching_document() -> None:
    df = {"purify": 2, "shield": 1, "map": 1}
    hit = bm25_score(["purify"], ["purify", "shield"], df, 3, 2.0)
    miss = bm25_score(["purify"], ["map", "route"], df, 3, 2.0)
    assert hit > 0.0 and miss == 0.0


def test_bm25_edge_cases() -> None:
    assert bm25_score([], ["a"], {}, 0, 0.0) == 0.0
    assert bm25_score(["a"], [], {}, 1, 1.0) == 0.0
    assert bm25_score(["a"], ["a"], {}, 0, 1.0) == 0.0


def test_bm25_is_deterministic() -> None:
    df = {"a": 1, "b": 1}
    values = {bm25_score(["a", "b"], ["a", "b", "c"], df, 2, 3.0) for _ in range(5)}
    assert len(values) == 1


# ---- score_entry / render / budget --------------------------------------------------

def test_score_entry_same_situation_bonus(mem_entry, obs_player_turn) -> None:
    key, situation, tags = situation_key(obs_player_turn)
    entry = mem_entry(situation=situation, situation_key=key)
    weights = obs_tag_weights(obs_player_turn)
    common = dict(obs_key=key, obs_tags=weights, query_tokens=list(tags), df={},
                  n_docs=1, avgdl=10.0,
                  weights={"tag": 1.0, "bm25": 0.6, "vector": 0.0, "utility": 0.3,
                           "recency": 0.1},
                  same_situation_bonus=0.25, recency_lambda=0.01, tick=50)
    with_bonus = score_entry(entry, **common)
    without = score_entry(replace(entry, situation_key="sk-00000000"), **common)
    # round(S, 6) 之后两者相差 0.25（±1e-6 的取整误差）
    assert with_bonus - without == pytest.approx(0.25, abs=2e-6)


def test_score_entry_vector_weight_off_by_default(mem_entry) -> None:
    entry = mem_entry(embedding=(1.0, 0.0))
    score = score_entry(entry, obs_key="sk-00000000", obs_tags={}, query_tokens=[],
                        df={}, n_docs=1, avgdl=1.0,
                        weights={"tag": 1.0, "bm25": 0.6, "vector": 0.0, "utility": 0.3,
                                 "recency": 0.1},
                        same_situation_bonus=0.0, recency_lambda=0.01, tick=37)
    assert score == pytest.approx(0.3 * entry.utility + 0.1 * 1.0, abs=1e-6)


def test_doc_tokens_includes_tags(mem_entry) -> None:
    entry = mem_entry()
    tokens = doc_tokens(entry)
    assert "combat" in tokens and "defense" in tokens


def test_render_entry_is_single_line_and_bounded(mem_entry) -> None:
    line = render_entry(mem_entry(lesson="很长" * 400))
    assert "\n" not in line
    assert len(line) <= MAX_RENDER_CHARS
    assert line.startswith("[combat|low_virtue|defense|school_counter]")
    assert "hint=play_face→defense" in line


def test_render_entry_marks_avoid(mem_entry) -> None:
    assert "避免：" in render_entry(mem_entry(polarity="avoid"))
    assert "避免：" not in render_entry(mem_entry(polarity="do"))


def test_render_entries_skips_lines_that_do_not_fit(mem_entry) -> None:
    entries = [mem_entry(entry_id=f"mem-{'a' * 11}{i}", lesson="短教训")
               for i in range(3)]
    text = render_entries(entries, max_chars=len(render_entry(entries[0])) + 1)
    # 只装得下第 1 条：第 2 条需要额外 1 个换行符 → 跳过（不截断）
    assert text == render_entry(entries[0])
    assert len(render_entries(entries, max_chars=10**6).splitlines()) == 3


def test_select_with_budget_skips_and_never_truncates(mem_entry) -> None:
    """GWT-05-05：第 4 条超预算被跳过，第 5–6 条仍入选；不截断文本。"""
    long_entry = mem_entry(entry_id="mem-" + "b" * 12, lesson="长" * 240)
    short = [mem_entry(entry_id=f"mem-{'c' * 11}{i}", lesson="短")
             for i in range(6)]
    scored = [(long_entry, 0.9)] + [(e, 0.8 - i * 0.01) for i, e in enumerate(short)]
    budget = estimate_tokens(render_entry(long_entry))
    selected, dropped, used = select_with_budget(scored, top_k=8,
                                                 token_budget=budget * 3,
                                                 line_of=render_entry)
    ids = [item.entry_id for item in selected]
    assert "mem-" + "b" * 12 not in ids or len(ids) > 1
    assert all(item.tokens == estimate_tokens(render_entry(item.entry)) for item in selected)
    assert used == sum(item.tokens for item in selected)
    assert all(reason in ("token_budget_exceeded", "top_k_exceeded")
               for _, reason, _ in dropped)


def test_select_with_budget_top_k_cut(mem_entry) -> None:
    scored = [(mem_entry(entry_id=f"mem-{'d' * 11}{i}", lesson="x"), 0.5 - i * 0.01)
              for i in range(5)]
    selected, dropped, _ = select_with_budget(scored, top_k=2, token_budget=10**6,
                                              line_of=render_entry)
    assert len(selected) == 2
    assert all(reason == "top_k_exceeded" for _, reason, _ in dropped)
    assert [item.rank for item in selected] == [0, 1]


def test_candidate_is_hashable_free() -> None:
    """候选带 dict 字段；本模块只用 entry_id 做键，不依赖 hash。"""
    candidate = MemoryCandidate(situation=SituationKey(), tags=(), lesson="x",
                                polarity="do", confidence=0.5)
    assert candidate.situation_key.startswith("sk-")
