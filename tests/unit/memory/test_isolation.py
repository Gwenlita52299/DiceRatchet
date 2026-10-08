"""FR-05-10 / GWT-05-10/11：INV-5 四拦截点（I1 源头 / I2 文本 / I3 兜底 / I4 出口）。

四拦截点缺一不可（PRD §6.5）；本文件逐一给出可失败的断言。蜜罐（G4）走完整写入管线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.memory import (MemoryEntry, MemoryIsolationError, MemoryStore,
                              Provenance, ReflectionEntry, extract_candidates)
from dice_ratchet.sandbox.honeypot import current_canary

HELD_OUT_SEED = 2001       # heldout H1 篮子 2000–2039
TEST_SEED = 9001           # test 池 9000–9099
TRAIN_SEED = 1042


def _lines(path) -> list[str]:
    text = Path(path).read_text("utf-8") if Path(path).is_file() else ""
    return [line for line in text.splitlines() if line.strip()]


def _events(store, name: str) -> list[dict]:
    return [json.loads(line) for line in _lines(store.metrics_path)
            if json.loads(line)["event"] == name]


def _snapshot_files(store) -> list[Path]:
    return [path for _, path in store.snapshots()]


# ---- I1 源头 -------------------------------------------------------------------------

def test_i1_rejects_non_train_split_and_writes_nothing(store_factory, make_reflection,
                                                       make_prov) -> None:
    """GWT-05-10：``provenance.split="heldout"`` → 抛错且两份 JSONL 行数不变。"""
    store = store_factory()
    prov = make_prov(split="heldout", seed=HELD_OUT_SEED)
    reflection = make_reflection(seed=HELD_OUT_SEED, split="heldout")
    with pytest.raises(MemoryIsolationError) as exc:
        store.add_reflection(reflection, provenance=prov)
    assert exc.value.code == E.E_EVAL_INV5_LEAK
    assert not Path(store.active_path).exists()
    assert not Path(store.reflections_path).exists()
    assert store.stats().seed_leak_blocks == 0  # I1 不是 I2/3/4 的扫描命中


def test_i1_rejects_seed_outside_train_window(store_factory, make_reflection,
                                              make_prov) -> None:
    store = store_factory()
    prov = make_prov(split="train", seed=2500)
    with pytest.raises(MemoryIsolationError):
        store.add_reflection(make_reflection(seed=2500), provenance=prov)


def test_i1_rejects_seed_mismatch_between_reflection_and_provenance(store_factory,
                                                                   make_reflection,
                                                                   make_prov) -> None:
    store = store_factory()
    with pytest.raises(MemoryIsolationError):
        store.add_reflection(make_reflection(seed=TRAIN_SEED),
                             provenance=make_prov(seed=1100))


def test_i1_rejects_add_memory_without_provenance_train(store_factory, make_candidate,
                                                        make_prov) -> None:
    store = store_factory()
    with pytest.raises(MemoryIsolationError):
        store.add_memory(make_candidate(), provenance=make_prov(split="test", seed=TEST_SEED))


# ---- I2 文本 -------------------------------------------------------------------------

def test_i2_quarantines_reflection_with_heldout_seed(store_factory, make_reflection,
                                                     make_prov) -> None:
    """GWT-05-10：正文含「seed 是 2001」→ 不落 active 库 + 两份隔离文件 + leak 事件。"""
    store = store_factory()
    reflection = make_reflection(text=f"如果 seed 是 {HELD_OUT_SEED} 我会先放盾。")
    result = store.add_reflection(reflection, provenance=make_prov(seed=TRAIN_SEED))
    assert result.quarantined == 1 and result.added == 0
    assert len(store.entries()) == 0
    assert not Path(store.active_path).exists()
    assert len(_lines(store.leak_quarantine_path)) == 1
    assert len(_lines(store.leak_events_path)) == 1
    assert len(_lines(store.quarantine_path)) == 1

    event = json.loads(_lines(store.leak_events_path)[0])
    assert event["interception_point"] == "I2"
    assert event["reflection_id"] == reflection.reflection_id
    assert event["hits"][0]["pattern"] == str(HELD_OUT_SEED)
    assert event["hits"][0]["token"] == str(HELD_OUT_SEED)
    assert event["hits"][0]["span"][1] > event["hits"][0]["span"][0]
    assert store.stats().seed_leak_blocks >= 1


def test_i2_holds_seed_pattern_regex(store_factory, make_reflection, make_prov) -> None:
    store = store_factory()
    store.add_reflection(make_reflection(text=f"seed={HELD_OUT_SEED} 时先放盾。"),
                         provenance=make_prov())
    event = json.loads(_lines(store.leak_events_path)[0])
    assert event["hits"][0]["pattern"] == r"\bseed\b\s*[:=#]?\s*\d{1,7}"


def test_i2_blocks_test_seed_in_lesson_fields(store_factory, make_candidate,
                                              make_prov) -> None:
    store = store_factory()
    result = store.add_memory(
        make_candidate(lesson=f"第 {TEST_SEED} 局的对手会先用清扫。"),
        provenance=make_prov())
    assert result.quarantined == 1
    assert not Path(store.active_path).exists()
    assert json.loads(_lines(store.leak_events_path)[0])["interception_point"] == "I2"


def test_i2_train_seed_is_allowed(store_factory, make_reflection, make_prov) -> None:
    """INV-5 只禁 heldout/test；train seed 出现在反思里是合法的（可复现）。"""
    store = store_factory()
    result = store.add_reflection(make_reflection(text=f"本局 seed={TRAIN_SEED} 净化值低。"),
                                  provenance=make_prov())
    assert result.quarantined == 0
    assert len(store.entries()) >= 1


def test_i2_catches_honeypot_canary(store_factory, make_reflection, make_prov) -> None:
    """GWT-05-11：蜜罐注入的伪反思（canary / 假字段）必须被拦下。"""
    store = store_factory()
    result = store.add_reflection(
        make_reflection(text=f"我看到了 {current_canary()} 这个字段，还有 _np_secret_price。"),
        provenance=make_prov(seed=TRAIN_SEED))
    assert result.quarantined == 1
    assert store.stats().seed_leak_blocks >= 1
    assert _events(store, "memory.seed_leak_blocked")


def test_i2_honeypot_with_test_seed_blocks_full_pipeline(store_factory, make_reflection,
                                                         make_prov) -> None:
    """GWT-05-11：蜜罐 + test seed 走完整写入管线 → 命中事件可被 audit 读到。"""
    store = store_factory()
    store.add_reflection(
        make_reflection(text=f"下一局 seed 是 {TEST_SEED}，我会先放光盾。"),
        provenance=make_prov())
    assert store.entries() == ()
    event = json.loads(_lines(store.leak_events_path)[0])
    assert event["interception_point"] == "I2"
    assert store.stats().seed_leak_blocks >= 1


# ---- I3 兜底 -------------------------------------------------------------------------

def _inject_leaked_entry(store, make_candidate, make_prov) -> str:
    """模拟「旧快照直接载入」的条目（绕过 I2）。"""
    store.add_memory(make_candidate(lesson="先观察再行动。"), provenance=make_prov())
    entry = store.entries()[0]
    leaked = entry.with_updates(lesson=f"第 {HELD_OUT_SEED} 局的清扫意图要先用光盾挡。")
    # 走 _replace（而非直接改 dict）：同时刷新行缓存，模拟「旧快照被重新加载」
    store._replace(leaked)
    return leaked.entry_id


def test_i3_snapshot_refuses_and_quarantines(store_factory, make_candidate,
                                            make_prov) -> None:
    """GWT-05-11：手工构造含裸 seed 的旧条目 → ``snapshot()`` 拒绝出快照。"""
    store = store_factory()
    entry_id = _inject_leaked_entry(store, make_candidate, make_prov)
    with pytest.raises(MemoryIsolationError) as exc:
        store.snapshot(reason="gen-000")
    assert exc.value.code == E.E_EVAL_INV5_LEAK
    assert _snapshot_files(store) == []
    assert store.get(entry_id).status == "quarantined"
    assert json.loads(_lines(store.leak_events_path)[0])["interception_point"] == "I3"


def test_i3_verify_full_raises(store_factory, make_candidate, make_prov) -> None:
    store = store_factory()
    _inject_leaked_entry(store, make_candidate, make_prov)
    with pytest.raises(MemoryIsolationError):
        store.verify(5, full=True)
    assert store.stats().quarantined_count == 1


def test_i3_verify_not_full_does_not_raise(store_factory, make_candidate,
                                           make_prov) -> None:
    store = store_factory()
    _inject_leaked_entry(store, make_candidate, make_prov)
    report = store.verify(5, full=False)
    assert report.leak_hits == 0


# ---- I4 出口 -------------------------------------------------------------------------

def test_i4_retrieve_drops_leaked_entry(store_factory, make_candidate, make_prov) -> None:
    """GWT-05-11：``retrieve()`` 丢弃命中条目且不计入返回。"""
    store = store_factory()
    entry_id = _inject_leaked_entry(store, make_candidate, make_prov)
    result = store.retrieve({"phase": "player_turn", "stage": 3}, tick=10)
    assert result.entries == ()
    assert [d.reason for d in result.dropped] == ["inv5_leak"]
    assert result.dropped[0].entry_id == entry_id
    assert store.stats().seed_leak_blocks >= 1
    event = json.loads(_lines(store.leak_events_path)[0])
    assert event["interception_point"] == "I4"
    assert event["matched_pattern"]


def test_i4_does_not_quarantine_by_default(store_factory, make_candidate,
                                           make_prov) -> None:
    store = store_factory()
    entry_id = _inject_leaked_entry(store, make_candidate, make_prov)
    store.retrieve({"phase": "player_turn"}, tick=10)
    # I4 只丢弃并记事件，不改状态（quarantine 归 I3）
    assert store.get(entry_id).status == "active"


# ---- 静态确定性（G3）-----------------------------------------------------------------

def test_memory_package_has_no_random_or_wallclock_scoring() -> None:
    """G3 / MOD-09 AST 守卫：``dice_ratchet/memory`` 无 random、无 ``time.time()`` 采样。"""
    from dice_ratchet.infra import guards

    root = Path(__file__).resolve().parents[3] / "dice_ratchet" / "memory"
    findings = [f for f in guards.scan_determinism(root) if not f.exempted]
    assert findings == [], [f.message for f in findings]


def test_memory_package_passes_layer_guard() -> None:
    """INV-1：memory 层不得直接 import ``dice_ratchet.env``。"""
    from dice_ratchet.infra import guards

    root = Path(__file__).resolve().parents[3] / "dice_ratchet" / "memory"
    findings = [f for f in guards.scan_layer_imports(root) if not f.exempted]
    assert findings == [], [f.message for f in findings]


def test_reflection_text_with_heldout_seed_has_no_candidate_leak(
        make_reflection) -> None:
    """抽取器本身不做判断（拦截归 I2）；此处只锁「抽取不会凭空产出 seed 文本」。"""
    candidates = extract_candidates(make_reflection(text="先放盾再输出。"), None)
    for candidate in candidates:
        assert "2000" not in candidate.lesson and "9000" not in candidate.lesson


# ---- G4：与 MOD-02 审计器的端到端一致性 -----------------------------------------------

def _audit_run(root: Path, config_dir: Path):
    from dice_ratchet.sandbox.audit import run_audit

    return run_audit(Path(root), split="train", config_dir=Path(config_dir))


@pytest.fixture
def audit_run(tmp_path, config_dir):
    """建一个真实的 run 目录（config.json + 目录树），供 ``run_audit`` 复检。"""
    from dice_ratchet.infra.config import load_config
    from dice_ratchet.infra.run import init_run_dir

    cfg = load_config()
    paths = init_run_dir("r-audit", cfg, tmp_path / "runs", git_check=False)
    return paths.root, cfg, config_dir


def test_g4_audit_leak_is_clean_on_memory_run(audit_run) -> None:
    """PRD §12 G4：写入记忆 + 快照后，``audit leak`` 必须 0 告警。

    回归点：``created_at`` 的年份 2026 落在 heldout H1（2000–2039）区间，若 memory
    面按原始行扫 seed，会把每个时间戳都判成泄漏（实测 7 条 critical 假阳性）。
    """
    root, cfg, config_dir = audit_run
    store = MemoryStore(root, cfg.memory, config_dir=config_dir, run_id="r-audit")
    store.add_reflection(_reflection_for_audit(), provenance=_provenance_for_audit())
    store.snapshot(reason="gen-000")
    store.close()

    result = _audit_run(root, config_dir)
    assert result.verdict == "clean", dict(result.leak_counts)
    assert result.findings == ()
    assert result.scanned["memory_entries"] >= 1


def test_g4_audit_reports_quarantined_honeypot(audit_run) -> None:
    """GWT-05-11：蜜罐伪反思（含 test seed）走完整管线后，审计必须有告警。"""
    root, cfg, config_dir = audit_run
    store = MemoryStore(root, cfg.memory, config_dir=config_dir, run_id="r-audit")
    result = store.add_reflection(
        _reflection_for_audit(text=f"如果 seed 是 {TEST_SEED} 我会先放盾。"),
        provenance=_provenance_for_audit())
    store.close()
    assert result.quarantined == 1

    audit = _audit_run(root, config_dir)
    assert audit.leak_counts.get("seed_leak", 0) >= 1
    assert audit.verdict != "clean"


def _reflection_for_audit(text: str = "净化值只剩 4 时我还在打输出，应该先放光盾。") -> ReflectionEntry:
    return ReflectionEntry(
        reflection_id="refl-audit", schema_version="1.1",
        created_at="2026-10-02T09:14:11Z", run_id="r-audit",
        episode_id="e-001042-g000-train", seed=TRAIN_SEED, split="train",
        generation=0, harness_version=1, tick=5, text=text, outcome="lose",
        truncation_reason=None, stages_cleared=2, hearts_left=0, battles_won=1,
        decisions=20, illegal_attempts=0, fallback_decisions=0,
        failure_cause="virtue_management", tags=(), extracted_memory_ids=(),
        scan={"status": "pass", "hits": []})


def _provenance_for_audit() -> Provenance:
    return Provenance(run_id="r-audit", episode_id="e-001042-g000-train", split="train",
                      seed=TRAIN_SEED, generation=0, harness_version=1)
