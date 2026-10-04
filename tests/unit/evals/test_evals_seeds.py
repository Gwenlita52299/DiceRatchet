"""T-07 / T-08 + FR-08-01…04：seed 协议、INV-5 护栏、test 解锁。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.evals import seeds as S

_REPO = Path(__file__).resolve().parents[3]


# ---- FR-08-01：区间两两不相交 ------------------------------------------------------

def test_default_protocol_validates():
    proto = S.validate_protocol(S.default_protocol())
    assert proto.split("train").start == 1000
    assert S.resolve_basket("H1", protocol=proto)[0] == 2000
    assert len(S.resolve_basket("H1", protocol=proto)) == 40


def test_overlapping_ranges_raise():
    bad = S.SeedProtocol(splits={
        "train": S.SplitRange("train", 1000, 1999, per_generation=30),
        "heldout": S.SplitRange("heldout", 1500, 2099, baskets={
            "H1": S.Basket("H1", 2000, 2039, "generation_judge")}),
        "test": S.SplitRange("test", 9000, 9099, locked=True),
    })
    with pytest.raises(InfraError) as exc:
        S.validate_protocol(bad)
    assert exc.value.code == E.E_EVAL_SEED_OVERLAP


def test_seed_basket_membership_is_fixed():
    proto = S.default_protocol()
    assert S.resolve_basket("H2", protocol=proto)[0] == 2040
    assert S.resolve_basket("H3", protocol=proto)[0] == 2080
    assert len(S.resolve_basket("test_full", protocol=proto)) == 100


# ---- FR-08-02：train 切块与耗尽 ----------------------------------------------------

def test_train_block_by_generation():
    proto = S.default_protocol()
    assert S.take("train", generation=1, protocol=proto)[:3] == [1000, 1001, 1002]
    assert S.take("train", generation=1, protocol=proto)[-1] == 1029
    assert S.take("train", generation=2, protocol=proto)[0] == 1030
    with pytest.raises(InfraError) as exc:
        S.take("train", generation=34, protocol=proto) # 1000+30*33=1990..2019 越界
    assert exc.value.code == E.E_EVAL_SEED_EXHAUSTED


def test_train_generation_zero_rejected():
    with pytest.raises(InfraError) as exc:
        S.take("train", generation=0)
    assert exc.value.code == E.E_EVAL_SEED_EXHAUSTED


def test_train_window_is_rolling():
    assert S.train_window(0) == []
    assert S.train_window(1) == list(range(1000, 1030))
    assert len(S.train_window(3)) == 60
    assert S.train_window(3)[-1] == 1089


# ---- FR-08-04：test 锁定 -----------------------------------------------------------

def test_test_locked_by_default():
    with pytest.raises(InfraError) as exc:
        S.take("test")
    assert exc.value.code == E.E_EVAL_TEST_LOCKED


def test_test_unlock_writes_attestation_and_is_single_use(tmp_path):
    proto = S.default_protocol()
    unlocked = S.SeedProtocol(version=proto.version, hash_algo=proto.hash_algo,
                              splits=proto.splits, exposure_guard=proto.exposure_guard,
                              seeds_hash=proto.seeds_hash, unlocked=True)
    assert len(S.take("test", protocol=unlocked)) == 100
    path = S.test_unlock(run_id="r-test", generation=3, run_dir=tmp_path,
                         protocol=unlocked, reason="M3 终检")
    assert path.is_file()
    payload = path.read_text(encoding="utf-8")
    assert '"decision_path": false' in payload
    with pytest.raises(InfraError) as exc:
        S.test_unlock(run_id="r-test", generation=3, run_dir=tmp_path, protocol=unlocked)
    assert exc.value.code == E.E_EVAL_TEST_LOCKED


# ---- FR-08-05：文本护栏（T-07）-----------------------------------------------------

def test_guard_prompt_context_catches_heldout_token():
    with pytest.raises(InfraError) as exc:
        S.guard_prompt_context(split="heldout", seed=2043, text="seed 2043 是正确答案",
                               destination="prompt")
    assert exc.value.code == E.E_EVAL_INV5_LEAK


def test_guard_prompt_context_allows_plain_numbers():
    # 无误报：23 / 4 不是 4 位 heldout/test token
    S.guard_prompt_context(split="train", seed=1000, text="hp 23, dice 4",
                           destination="prompt")


def test_guard_prompt_context_catches_forbidden_words():
    with pytest.raises(InfraError) as exc:
        S.guard_prompt_context(split="train", seed=1000, text="see H1 basket",
                               destination="memory")
    assert exc.value.code == E.E_EVAL_INV5_LEAK


def test_guard_prompt_context_catches_test_token():
    with pytest.raises(InfraError) as exc:
        S.guard_prompt_context(split="test", seed=9001, text="run 9001",
                               destination="reflection")
    assert exc.value.code == E.E_EVAL_INV5_LEAK


# ---- FR-08-06：非 train 局禁写记忆（T-08）------------------------------------------

def test_guard_memory_write_blocks_non_train():
    with pytest.raises(InfraError) as exc:
        S.guard_memory_write("heldout", seed=2001, destination="memory")
    assert exc.value.code == E.E_EVAL_INV5_LEAK
    with pytest.raises(InfraError) as exc2:
        S.guard_memory_write("test", seed=9001, destination="reflection")
    assert exc2.value.code == E.E_EVAL_INV5_LEAK


def test_guard_memory_write_allows_train():
    S.guard_memory_write("train", seed=1000, destination="memory")


# ---- split 归属与隔离 ---------------------------------------------------------------

def test_assert_split_isolation_rejects_cross_split_seed():
    with pytest.raises(InfraError) as exc:
        S.assert_split_isolation("heldout", [1000, 2000])
    assert exc.value.code == E.E_EVAL_SEED_OVERLAP
    S.assert_split_isolation("heldout", [2000, 2039])


# ---- seeds_hash 与 configs/seeds.yaml 一致（CI 校验）--------------------------------

def test_configs_seeds_hash_matches_protocol():
    proto = S.load_protocol(_REPO / "configs")
    assert proto.seeds_hash == S.protocol_hash(proto), (
        "configs/seeds.yaml 的 seeds_hash 与协议不一致：改区间后必须重算并写回")
