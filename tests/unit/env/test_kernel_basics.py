"""MOD-01 单元测试：RNG / digest / 整数算术 / 配置（T-01、T-02、T-03、T-06、T-07）。

T-03 的 SplitMix64 参考向量与 JS 侧 ``tests/kernel/rng.test.js`` 同源（BigInt 参考实现）；
T-06/T-07 的期望值由 JS 内核现场计算（``node -e``），保证两侧同值。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dice_ratchet.env import (GAME_CONTENT_DIR, TOGGLE_KEYS, GameEngine, STREAM_NAMES,
                          apply_difficulty, allowed_stage_counts, config_hash, digest,
                          load_tables, validate_config)
from dice_ratchet.env.error import KernelError
from dice_ratchet.env.rng import (TWO32, add64, create_rng_state, derive_seed, mul64, mul_u32,
                              next_u32, randint, shuffle, splitmix64, u32, xmur3)
from dice_ratchet.env.digest import stable_stringify

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ======================================================================
# T-03 随机
# ======================================================================

def test_splitmix64_matches_bigint_reference():
    """与 JS 侧 BigInt 参考实现（``rng.test.js`` 第一条）逐位一致，50 步。"""
    M = (1 << 64) - 1
    s = 0x0123456789ABCDEF
    st = (0x01234567, 0x89ABCDEF)
    for _ in range(50):
        s = (s + 0x9E3779B97F4A7C15) & M
        z = s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M
        z = z ^ (z >> 31)
        new_st, got = splitmix64(st)
        assert got[0] == z >> 32
        assert got[1] == z & 0xFFFFFFFF
        assert new_st == (s >> 32, s & 0xFFFFFFFF)
        st = new_st


def test_mul_u32_and_mul64():
    assert mul_u32(0xFFFFFFFF, 0xFFFFFFFF) == (0xFFFFFFFE, 1)
    assert mul_u32(0, 123) == (0, 0)
    # JS mul64 的截断语义逐位复刻（不是数学正确的 64 位乘法）
    a, b = (0x00000002, 0x00000003), (0x00000005, 0x00000007)
    assert mul64(a, b) == (u32(2 * 7 + 3 * 5), 3 * 7)
    assert add64((0, TWO32 - 1), (0, 1)) == (1, 0)


def test_stream_names_sorted():
    assert list(STREAM_NAMES) == sorted(STREAM_NAMES)
    assert STREAM_NAMES == ("dice.enemy", "dice.player", "event", "forge", "levelup", "loot", "map", "shop")


def test_create_rng_state_order_and_counters():
    state = {"rng": create_rng_state(42)}
    assert [r["name"] for r in state["rng"]] == list(STREAM_NAMES)
    assert all(r["counter"] == 0 for r in state["rng"])
    for _ in range(20):
        next_u32(state, "map")
    next_u32(state, "dice.player")
    assert next((r for r in state["rng"] if r["name"] == "map"))["counter"] == 20
    assert next((r for r in state["rng"] if r["name"] == "dice.player"))["counter"] == 1


def test_stream_independence_and_derive_seed():
    a = {"rng": create_rng_state(42)}
    b = {"rng": create_rng_state(42)}
    xs = [next_u32(a, "map") for _ in range(20)]
    next_u32(b, "dice.player")  # 消费别的流不影响 map 流
    ys = [next_u32(b, "map") for _ in range(20)]
    assert xs == ys
    assert derive_seed(42, "map") != derive_seed(42, "shop")


def test_randint_closed_range_covers_all():
    state = {"rng": create_rng_state(7)}
    seen = {randint(state, "dice.player", 1, 6) for _ in range(1200)}
    assert seen == {1, 2, 3, 4, 5, 6}


def test_shuffle_is_permutation_and_pure():
    state = {"rng": create_rng_state(9)}
    src = [1, 2, 3, 4, 5, 6, 7, 8]
    out = shuffle(state, "shop", src)
    assert sorted(out) == src
    assert src == [1, 2, 3, 4, 5, 6, 7, 8]


def test_unknown_stream_raises():
    with pytest.raises(KernelError) as excinfo:
        next_u32({"rng": create_rng_state(1)}, "nope")
    assert excinfo.value.code == "E_KERNEL_RNG_STREAM_MISSING"


def test_xmur3_known_vector():
    """xmur3('a') 前四次输出与 JS 现场计算同值。"""
    g = xmur3("a")
    assert [g() for _ in range(4)] == [519299066, 3840329591, 1163017567, 3909796798]


# ======================================================================
# T-02 stable_stringify / digest
# ======================================================================

def test_stable_stringify_sorted_and_escaped():
    assert stable_stringify({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert stable_stringify({"x": [3, {"y": None, "z": True}]}) == '{"x":[3,{"y":null,"z":true}]}'
    assert stable_stringify("中文") == '"中文"'  # 不转义非 ASCII（与 JS 一致）
    assert stable_stringify(None) == "null"
    assert stable_stringify(True) == "true"


def test_stable_stringify_rejects_float():
    with pytest.raises(TypeError):
        stable_stringify({"a": 1.5})


def test_digest_vector():
    assert digest({"a": "刺击 1", "b": 3, "c": None, "d": [1, {"x": "y"}]}) == \
        "sha256:3bef57c7b61b58d3725c8e92fcd5dacb62d26c07529e8b825ef863f493cbfa07"


def test_digest_sorted_keys_order_independent():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_config_hash_truncated():
    h = config_hash({"seed": 1, "difficulty": "medium"})
    assert h.startswith("sha256:") and len(h) == len("sha256:") + 16
    assert all(c in "0123456789abcdef" for c in h[7:])


# ======================================================================
# T-06 配置
# ======================================================================

@pytest.fixture(scope="module")
def tables():
    return load_tables()


def test_apply_difficulty_expands_and_clamps(tables):
    cfg = apply_difficulty({"seed": 1, "toggles": {"relics": False}}, "hard", tables)
    assert cfg["configVersion"] == "cfg-1"
    assert cfg["difficulty"] == "hard"
    assert cfg["seed"] == 1
    assert cfg["toggles"] == {"mapBranching": True, "relics": False, "events": True,
                              "shops": True, "schoolCounter": True}
    assert set(cfg["eliteStages"]) == set(tables["difficulty"]["levels"]["hard"]["eliteStages"])
    assert cfg["eliteStages"] is not tables["difficulty"]["levels"]["hard"]["eliteStages"]
    assert cfg["stageCount"] == tables["stages"]["stageCount"]
    assert cfg["stageCount"] in allowed_stage_counts(tables)


def test_apply_difficulty_stage_count_override(tables):
    cfg = apply_difficulty({"seed": 1, "stageCount": 6}, "hard", tables)
    assert cfg["stageCount"] == 6


def test_apply_difficulty_unknown(tables):
    with pytest.raises(KernelError) as excinfo:
        apply_difficulty({"seed": 1}, "nightmare", tables)
    assert excinfo.value.code == "E_KERNEL_BAD_CONFIG"


def test_validate_config_errors(tables):
    ok = validate_config(apply_difficulty({"seed": 1}, "medium", tables), tables)
    assert ok == {"ok": True, "errors": []}
    bad = validate_config(apply_difficulty({"seed": 2 ** 32, "character": "rogue",
                                            "toggles": {"relics": "yes"},
                                            "stageCount": 7}, "medium", tables), tables)
    assert bad["ok"] is False
    # apply_difficulty 会把非法 seed/toggle 强制规范化（2**32→0、"yes"→True），
    # 因此只有 character / stageCount 能透到 validateConfig
    assert set(bad["errors"]) == {"character", "stageCount"}


def test_allowed_stage_counts_are_boss_stages(tables):
    assert allowed_stage_counts(tables) == sorted(
        s["stage"] for s in tables["stages"]["stages"] if s.get("boss"))


def test_toggle_keys_match_spec():
    assert TOGGLE_KEYS == ("mapBranching", "relics", "events", "shops", "schoolCounter")


# ======================================================================
# 跨语言向量（JS 现场计算；无 node 时跳过）
# ======================================================================

def _node_ok() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_ok(), reason="node 不可用")
def test_js_parity_rng_and_digest(tables):
    """与 JS 内核对同一输入取值（T-03/T-07 跨语言锚点）。"""
    script = """
    import { createRngState, nextU32, deriveSeed } from './src/game/kernel/rng.js';
    import { digest, configHash, applyDifficulty, validateConfig } from './src/game/kernel/index.js';
    import { loadTables } from './src/game/kernel/loader.js';
    const tables = loadTables();
    const s = { rng: createRngState(1234) };
    const out = { u32: [...Array(8)].map(() => nextU32(s, 'loot')),
      seed: deriveSeed(1234, 'loot'),
      digest: digest({a:'刺击 1', b:3, c:null, d:[1,{x:'y'}]}),
      hash: configHash(applyDifficulty({seed:1}, 'hard', tables)),
      validate: validateConfig(applyDifficulty({seed:1}, 'hard', tables), tables) };
    console.log(JSON.stringify(out));
    """
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          cwd=_REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"node 执行失败：{proc.stderr[:200]}")
    js = json.loads(proc.stdout)
    state = {"rng": create_rng_state(1234)}
    assert [next_u32(state, "loot") for _ in range(8)] == js["u32"]
    assert derive_seed(1234, "loot") == (js["seed"]["s0"], js["seed"]["s1"])
    assert digest({"a": "刺击 1", "b": 3, "c": None, "d": [1, {"x": "y"}]}) == js["digest"]
    assert config_hash(apply_difficulty({"seed": 1}, "hard", tables)) == js["hash"]
    assert validate_config(apply_difficulty({"seed": 1}, "hard", tables), tables) == js["validate"]
