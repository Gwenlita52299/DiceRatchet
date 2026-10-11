"""F45-2：heldout/test 移出四位数字空间 + 代际隔离闸 + 全字段扫描。

现场（2026-10-10 实测）：旧 heldout/test 是四位（2000–2099 / 9000–9099），与耗时
（``duration_us=2072.8``）、token 预算（``max_tokens=2048``）、ISO 年份（``2026``）、
秒级浮点（``dur_s=3.9022``）共用数字空间 ⇒「数字串相等即泄漏」不健全，只能靠
``LOG_NON_CONTENT_KEYS`` / ``MEMORY_NON_CONTENT_KEYS`` 两张人工死名单豁免
（扩过四次；实测 12 个键撞上、总命中 118,646 次，其中 ``ts`` 占 94%）。

F45-2 把 heldout/test 移到**六位**端口空间，于是：
- 死名单可以整删，扫描面比带名单时**更严**；
- 但 ``config_hash`` **不含** seeds ⇒ 必须**另立**代际隔离闸，否则新旧局静默混算。

本文件覆盖：区间取值、协议串含 seeds_hash（隔离闸）、零碰撞反例自检、死名单已删、
全字段扫描（框架播种计划三键除外）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.evals import seeds as S
from dice_ratchet.infra.errors import E, InfraError

_REPO = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO / "configs"

_PROTO = S.validate_protocol(S.load_protocol(_CONFIG_DIR))
_H1 = _PROTO.splits["heldout"].baskets["H1"]
_H2 = _PROTO.splits["heldout"].baskets["H2"]
_H3 = _PROTO.splits["heldout"].baskets["H3"]
_TEST = _PROTO.splits["test"]


# =====================================================================
# 1) 新区间：六位、与常见预算/时长/日历字面量相撞的概率极低
# =====================================================================

def test_heldout_and_test_live_in_six_digit_ranges():
    """heldout/test 必须是**六位**（``[0-9]{3,6}`` 的正则不必放宽就能扫到）。"""
    assert (_H1.start, _H1.end) == (731000, 731039)
    assert (_H2.start, _H2.end) == (731040, 731079)
    assert (_H3.start, _H3.end) == (731080, 731099)
    assert (_TEST.start, _TEST.end) == (947000, 947099)
    for value in (_H1.start, _H1.end, _H2.start, _H2.end, _H3.start, _H3.end,
                  _TEST.start, _TEST.end):
        assert len(str(value)) == 6
    # train 不变（且本就不在 forbidden 集合里）
    train = _PROTO.splits["train"]
    assert (train.start, train.end) == (1000, 1999)


def test_new_ranges_avoid_common_budget_and_duration_literals():
    """避开常见预算/时长/日历字面量（这是选六位区间的**选择标准**，不是巧合）。"""
    common = {100000, 120000, 128000, 200000, 256000, 512000, 900000, 1000000,
              2026, 2048, 2072, 9000, 9022, 4096}
    forbidden = set(range(_H1.start, _H3.end + 1)) | set(range(_TEST.start, _TEST.end + 1))
    assert not (common & forbidden), sorted(common & forbidden)


def test_protocol_forbids_the_new_range_literals_and_not_the_old_ones():
    assert "731000-731099" in _PROTO.exposure_guard.forbidden_tokens
    assert "947000-947099" in _PROTO.exposure_guard.forbidden_tokens
    assert "2000-2099" not in _PROTO.exposure_guard.forbidden_tokens
    assert "9000-9099" not in _PROTO.exposure_guard.forbidden_tokens


def test_seeds_hash_in_yaml_matches_the_protocol():
    """``seeds_hash`` 必须与协议摘要一致（改区间后必须重算并写回）。"""
    raw = (_CONFIG_DIR / "seeds.yaml").read_text(encoding="utf-8")
    written = json.loads('"' + raw.split('seeds_hash: "')[1].split('"')[0] + '"')
    assert written == S.protocol_hash(_PROTO) == _PROTO.seeds_hash


# =====================================================================
# 2) 代际隔离闸：换区间必须改「测量协议」，否则新旧局静默混算
# =====================================================================

def test_baseline_protocol_binds_the_seeds_hash():
    """``config_hash`` 不含 seeds ⇒ 协议串必须**自己**带上 ``seeds_hash``。"""
    from dice_ratchet.evals.baselines import baseline_protocol

    from dice_ratchet.infra.config import load_config

    cfg = load_config(config_dir=_CONFIG_DIR)
    tag = "seeds-" + str(_PROTO.seeds_hash).removeprefix("sha256:")[:12]
    assert tag in baseline_protocol("heuristic", config=cfg)
    assert tag in baseline_protocol("fixed_prompt", config=cfg)


def test_resume_refuses_to_mix_two_seed_protocols(tmp_path):
    """一个旧协议局 + 一个新协议局的 run：``--resume`` 必须**拒绝**，不得静默合并。

    ``read_episode_protocols`` 是那条闸的唯一数据来源；这里直接构造两行不同 ``protocol``
    的工件（真实现场是「换区间前跑的局」与「换区间后要跑的局」）。
    """
    from dice_ratchet.evals.baselines import baseline_protocol
    from dice_ratchet.evals.harness import read_episode_protocols

    from dice_ratchet.infra.config import load_config

    cfg = load_config(config_dir=_CONFIG_DIR)
    current = baseline_protocol("heuristic", config=cfg)
    legacy = "episode-protocol-v2+seeds-2ba235a71a12" # F45-2 之前的旧区间标签
    assert current != legacy
    root = tmp_path / "episodes" / "heldout"
    root.mkdir(parents=True)
    for seed, protocol in ((731000, legacy), (731001, current)):
        key = f"e-{seed:06d}-g000-baseline-heuristic"
        (root / f"{key}.jsonl").write_text(
            json.dumps({"type": "episode_end", "episode_key": key, "seed": seed,
                        "arm": "baseline-heuristic", "generation": 0, "split": "heldout",
                        "protocol": protocol}) + "\n", encoding="utf-8")
    found = read_episode_protocols(tmp_path, "heldout", generation=0,
                                   arm="baseline-heuristic")
    assert found == {legacy, current}
    stale = sorted(p for p in found if p != current)
    assert stale == [legacy], "新旧种子协议的局必须在续跑前被识别出来（不得静默合并）"


def test_scan_forbidden_tokens_catches_six_digit_seeds():
    """文本护栏的数值正则原本写死 4 位 ⇒ 六位 seed **根本扫不到**（会静默失效）。"""
    assert S.scan_forbidden_tokens(f"seed={_H1.start}") == [str(_H1.start)]
    assert S.scan_forbidden_tokens(f"seed={_TEST.start + 1}") == [str(_TEST.start + 1)]
    # 反例：旧四位的数字串现在**不再**是泄漏（它们与 train/其它语义无关）
    assert S.scan_forbidden_tokens("history seed=2043 best") == []


# =====================================================================
# 3) 死名单已删；扫描面更严（只有框架播种计划三键除外）
# =====================================================================

def test_the_two_hand_maintained_lists_are_gone():
    from dice_ratchet.sandbox import audit as A

    assert not hasattr(A, "LOG_NON_CONTENT_KEYS"), \
        "LOG_NON_CONTENT_KEYS 应已删除（F45-2：六位端口空间让豁免不再必要）"
    assert not hasattr(A, "MEMORY_NON_CONTENT_KEYS")
    assert not hasattr(A, "_content_projection")
    # 唯一保留的豁免是「框架播种计划」三键，且**不含**任何耗时/预算/哈希键
    assert A.LOG_STRUCTURAL_KEYS == frozenset({"seed", "episode_key", "spec"})
    for banned in ("ts", "duration_us", "dur_s", "max_tokens", "prompt_tokens",
                   "request_hash", "response_hash", "prompt_digest", "2026", "n"):
        assert banned not in A.LOG_STRUCTURAL_KEYS, banned


def test_timing_and_budget_fields_are_now_scanned_not_exempted():
    """删除名单后，耗时/预算字段**进入扫描面**——只是新区间不再与它们撞车。"""
    from dice_ratchet.sandbox import audit as A

    # 反例自检：拿**旧**区间的数字串去扫，耗时/预算字段会命中（证明它们确实在面内）
    old = set(A.load_forbidden_seeds(_CONFIG_DIR, split_filter=("heldout", "test")))
    assert "2000" not in old and "731000" in old # 已经换到新区间
    row = {"event": "llm.api.call", "duration_us": 2072.8, "max_tokens": 2048,
           "ts": "2026-10-10T00:00:00Z", "prompt_tokens": 1673}
    text = json.dumps(row, sort_keys=True)
    findings = A.scan_text(text, forbidden={str(s): "seed" for s in
                                            range(731000, 731100)},
                           surface="log", episode_id="e-1", step=0)
    assert findings == [], "新区间下耗时/预算字段不该再命中（这是 F45-2 的收益）"


def test_the_runs_own_seed_plan_is_not_a_leak_but_content_still_is(tmp_path):
    """框架播种计划（``seed``/``spec.seeds``）不判泄漏；同一 seed 出现在内容字段必须判。"""
    from dice_ratchet.sandbox import audit as A

    seed = _H1.start + 5
    root = tmp_path / "r-f452"
    (root / "episodes" / "heldout").mkdir(parents=True)
    (root / "events.jsonl").write_text(
        json.dumps({"event": "eval.start", "spec": {"seeds": [seed]}}) + "\n",
        encoding="utf-8")
    key = f"e-{seed:06d}-g001-parent"
    (root / "episodes" / "heldout" / f"{key}.jsonl").write_text(
        json.dumps({"type": "episode_end", "episode_key": key, "seed": seed,
                    "generation": 1, "arm": "parent",
                    "protocol": "episode-protocol-v2"}) + "\n", encoding="utf-8")

    clean = A.run_audit(root)
    assert clean.leak_counts.get("seed_leak") is None, clean.findings
    assert clean.verdict == A.VERDICT_CLEAN

    # 反例自检：同一批工件，把 seed 写进**内容**字段（模型产出面）就必须抓到
    (root / "events.jsonl").write_text(
        json.dumps({"event": "eval.start", "spec": {"seeds": [seed]},
                    "reason": f"注意：seed {seed} 表现最好"}) + "\n", encoding="utf-8")
    dirty = A.run_audit(root)
    assert dirty.leak_counts.get("seed_leak") == 1, dirty.findings
    assert dirty.verdict == A.VERDICT_CRITICAL
