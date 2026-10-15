"""FR-07-2 / FR-07-3：证据束白名单与 INV-5 生成侧硬闸（PRD §11 单测 3）。

「给定-当-则」逐条对应 PRD §11-3：

- 给定证据束混入一条 ``split="heldout"`` 轨迹摘要，当 ``build_evidence_bundle``，
  则抛 ``E_EVAL_INV5_LEAK``；
- 给定反思文本出现 heldout seed ``731013`` 字面量（= 现行 H1 篮子 ``731000–731039`` 的
  第 14 个 seed，代码取 ``H1.start + 13``，不写死数字），当 ``assert_no_holdout_leak``，
  则抛 ``E_EVAL_INV5_LEAK``（出现 ``7310130`` / ``x731013y`` **不**误报）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dice_ratchet.evolve import proposer as P
from dice_ratchet.evolve.errors import HoldoutLeakError
from dice_ratchet.evolve.schemas import EvidenceBundle, HarnessSnapshot
from dice_ratchet.infra.errors import E

from evolve_support import make_config

#: H1 篮子内的 heldout seed（F45-2 起 H1 = 731000–731039；从协议取，避免魔法数字漂移）。
from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol  # noqa: E402

_PROTO = validate_protocol(coerce_protocol(None))
SEED_2013 = _PROTO.splits["heldout"].baskets["H1"].start + 13
SEED_2014 = SEED_2013 + 1
TEST_SEED_9001 = _PROTO.splits["test"].start + 1


# =====================================================================
# 构造证据束
# =====================================================================

def _bundle(*, memory=(), traj=(), reflect=(), tags=None, version=3) -> EvidenceBundle:
    source = tuple(tags) if tags is not None else ("harness:current",)
    return EvidenceBundle(
        bundle_id="ev-test", memory_items=tuple(memory),
        trajectory_summaries=tuple(traj), reflections=tuple(reflect),
        harness_snapshot=HarnessSnapshot(version=version),
        source_tags=source)


def test_source_tags_outside_whitelist_rejected():
    with pytest.raises(HoldoutLeakError) as excinfo:
        _bundle(tags=("harness:current", "heldout:731000"))
    assert str(excinfo.value.code) == str(E.E_EVAL_INV5_LEAK)


def test_clean_bundle_passes_leak_check():
    bundle = _bundle(traj=[{"split": "train", "episode_key": "e-001000-g001-train",
                            "stages_cleared": 2}])
    P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_non_train_split_in_trajectory_raises():
    bundle = _bundle(traj=[{"split": "heldout", "episode_key": "e-002000-g001-parent"}])
    with pytest.raises(HoldoutLeakError) as excinfo:
        P.assert_no_holdout_leak(bundle, cfg=make_config())
    assert str(excinfo.value.code) == str(E.E_EVAL_INV5_LEAK)


def test_non_train_split_in_memory_and_reflection_raises():
    for kwargs in ({"memory": [{"split": "test", "text": "教训"}]},
                   {"reflect": [{"split": "heldout", "lesson": "教训"}]}):
        with pytest.raises(HoldoutLeakError):
            P.assert_no_holdout_leak(_bundle(**kwargs), cfg=make_config())


# =====================================================================
# seed 字面量扫描（H1 六位端口空间：731000 + 13 = 731013；前导边界含 . ⇒ 浮点小数不误判）
# =====================================================================

@pytest.mark.parametrize("text", [
    f"heldout seed {SEED_2013} 的表现最好",
    f"seed={SEED_2013}",
    f"{SEED_2013}", # 整值 token
    f"scores: {SEED_2013},{SEED_2014}",
])
def test_seed_literal_is_leak(text):
    bundle = _bundle(reflect=[{"split": "train", "lesson": text}])
    with pytest.raises(HoldoutLeakError) as excinfo:
        P.assert_no_holdout_leak(bundle, cfg=make_config())
    assert str(excinfo.value.code) == str(E.E_EVAL_INV5_LEAK)


@pytest.mark.parametrize("text", [
    f"{SEED_2013}0",   # 整值 token 的右边界
    f"x{SEED_2013}y",  # 左右都不是数字（但它是独立 token！）
    f"1{SEED_2013}",
    f"{SEED_2013}5",
])
def test_seed_token_boundaries(text):
    """``20130`` / ``12013`` / ``20135`` 不误报。

    ``x2013y`` 中 ``2013`` 的左右字符非数字，按 PRD §11-3 的正则
    ``(?<!\\d)2013(?!\\d)`` 属**命中**——只有"与其它数字相邻"才排除。
    """
    bundle = _bundle(reflect=[{"split": "train", "lesson": text}])
    if text == f"x{SEED_2013}y":
        with pytest.raises(HoldoutLeakError):
            P.assert_no_holdout_leak(bundle, cfg=make_config())
    else:
        P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_train_seed_literal_is_not_leak():
    """train 区间（1000–1999）的 seed 字面量不触发（只在 heldout/test 集合里扫）。"""
    bundle = _bundle(reflect=[{"split": "train", "lesson": "train seed 1042 的教训"}])
    P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_test_seed_literal_is_leak():
    bundle = _bundle(memory=[{"split": "train", "text": f"test 池 {TEST_SEED_9001} 不可见"}])
    with pytest.raises(HoldoutLeakError):
        P.assert_no_holdout_leak(bundle, cfg=make_config())


@pytest.mark.parametrize("word", ["heldout", "seeds.yaml", "H1", "H2", "H3"])
def test_forbidden_labels_are_leak(word):
    bundle = _bundle(traj=[{"split": "train", "episode_key": f"note-{word}"}])
    with pytest.raises(HoldoutLeakError):
        P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_evolve_config_can_narrow_the_scan():
    """``evolve.heldout_seeds`` 显式给出时，扫描集合按它收敛（仍含注册表）。"""
    cfg = make_config(heldout_seeds=f"heldout:{_PROTO.splits['heldout'].baskets['H1'].start}-{_PROTO.splits['heldout'].baskets['H1'].end}")
    bundle = _bundle(reflect=[{"split": "train", "lesson": f"{SEED_2013}"}])
    with pytest.raises(HoldoutLeakError):
        P.assert_no_holdout_leak(bundle, cfg=cfg)


# =====================================================================
# build_evidence_bundle（唯一构造函数）
# =====================================================================

class _FakeMemory:
    """MOD-05 ``retrieve_for_evolution`` 的替身。"""

    def __init__(self, items):
        self.items = list(items)
        self.calls: list[int] = []

    def retrieve_for_evolution(self, *, top_k: int = 12):
        self.calls.append(int(top_k))
        return self.items[: int(top_k)]


def _write_train_episode(run_root: Path, *, seed: int, generation: int,
                         outcome: str = "lose", arm: str = "train") -> Path:
    path = run_root / "episodes" / "train" / f"e-{seed:06d}-g{generation:03d}-{arm}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "step", "episode_key": path.stem, "seed": seed, "arm": arm,
         "split": "train", "generation": generation, "step": 0},
        {"type": "episode_end", "episode_key": path.stem, "seed": seed, "arm": arm,
         "split": "train", "generation": generation, "outcome": outcome,
         "truncation_reason": None, "stages_cleared": 1, "hearts_left": 0,
         "battles_won": 1, "decisions": 5, "degraded": False},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def _write_reflection(run_root: Path, *, name: str, split: str = "train",
                      generation: int = 1, lesson: str = "优先清护卫") -> Path:
    path = run_root / "reflections" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"episode_key": name, "split": split,
                                "generation": generation, "lesson": lesson},
                               ensure_ascii=False), encoding="utf-8")
    return path


def test_build_evidence_bundle_only_train_sources(tmp_path):
    run_root = tmp_path / "runs" / "run-1"
    _write_train_episode(run_root, seed=1000, generation=1)
    _write_train_episode(run_root, seed=1001, generation=2) # 未来的代：必须被排除
    _write_reflection(run_root, name="e-001000-g001-train", generation=1)
    _write_reflection(run_root, name="heldout-note", split="heldout", generation=1)
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "version.json").write_text(json.dumps({"version": 2}), encoding="utf-8")

    memory = _FakeMemory([{"text": "训练侧教训", "entry_id": "m-1"}])
    bundle = P.build_evidence_bundle(run_id="run-1", n_traj=20, cfg=make_config(),
                                     memory=memory, run_dir=run_root, harness=harness,
                                     generation=1)
    assert set(bundle.source_tags) <= {"memory:train", "traj:train", "reflect:train",
                                       "harness:current"}
    assert [t["episode_key"] for t in bundle.trajectory_summaries] == [
        "e-001000-g001-train"]
    assert all(t["split"] == "train" for t in bundle.trajectory_summaries)
    assert all(r["split"] == "train" for r in bundle.reflections)
    assert len(bundle.reflections) == 1
    assert bundle.harness_snapshot.version == 2
    assert bundle.bundle_id.startswith("ev-")
    P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_build_evidence_bundle_is_deterministic(tmp_path):
    run_root = tmp_path / "runs" / "run-1"
    _write_train_episode(run_root, seed=1000, generation=1)
    memory = _FakeMemory([{"text": "教训", "entry_id": "m-1"}])
    kwargs = dict(run_id="run-1", n_traj=5, cfg=make_config(), memory=memory,
                  run_dir=run_root, harness=tmp_path / "harness", generation=1)
    (tmp_path / "harness").mkdir()
    first = P.build_evidence_bundle(**kwargs)
    second = P.build_evidence_bundle(**kwargs)
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.bundle_id == second.bundle_id


def test_build_evidence_bundle_tolerates_missing_memory_api(tmp_path):
    """MOD-05 未提供 ``retrieve_for_evolution`` 时降级为空，不炸（依赖可缺）。"""
    bundle = P.build_evidence_bundle(run_id="run-1", n_traj=5, cfg=make_config(),
                                     memory=object(), run_dir=None, generation=1)
    assert bundle.memory_items == ()
    assert "memory:train" not in bundle.source_tags


def test_harness_snapshot_excludes_versions_dir(tmp_path):
    harness = tmp_path / "harness"
    (harness / "prompts").mkdir(parents=True)
    (harness / "prompts" / "decision.md").write_text("x", encoding="utf-8")
    (harness / "versions" / "v1").mkdir(parents=True)
    (harness / "versions" / "v1" / "decision.md").write_text("old", encoding="utf-8")
    snap = P.harness_snapshot(harness)
    names = [name for name, _ in snap.files]
    assert "prompts/decision.md" in names
    assert not any(name.startswith("versions/") for name in names)


# =====================================================================
# F35：运行期元数据不得进入 agent 可见文本（撞 heldout 数字区间）
# =====================================================================

def test_f35_reflection_metadata_not_carried_into_bundle(tmp_path):
    """F35：带 ``entry.created_at`` 的反思记录不得把年份带进证据束。

    2026-10-09 §3.2 实跑现场：``configs/seeds.yaml`` 的 ``numeric_scan: true`` 占用了
    **2000-2099 / 9000-9099** 整个区间，而**日历年 2026 正在 H1（2000-2039）之内**；
    旧的 ``_reflection_set`` 拷的是**整条**记录 ⇒ ISO 时间戳
    ``"2026-10-09T16:34:13Z"`` 被判成「heldout seed 字面量」⇒ ``E_EVAL_INV5_LEAK``
    （退出码 4）、整代中止 ⇒ **闭环在 2000-2039 年永远跑不起来**。
    实测（真实证据束）：命中 3 处，全在 ``.reflections[i].entry.created_at``。

    这里刻意**同时**塞入 ``prompt_tokens=2026`` 与 ``llm_latency_ms=2031``，证明修法
    覆盖的不只是 ``created_at`` 一个字段。
    """
    run_root = tmp_path / "runs" / "run-1"
    path = run_root / "reflections" / "e-001000-g000-train.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "episode_key": "e-001000-g000-train", "split": "train", "generation": 0,
        "outcome": "win", "reflection_id": "refl-c5ae08ca726a",
        "entry": {"text": "优先清护卫", "tags": ["low_virtue"], "stages_cleared": 6,
                  "illegal_attempts": 7, "failure_cause": "virtue_management",
                  "created_at": "2026-10-09T16:34:13Z", "run_id": "run-1",
                  "prompt_tokens": 2026, "llm_latency_ms": 2031},
    }, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness"
    harness.mkdir()

    bundle = P.build_evidence_bundle(run_id="run-1", n_traj=20, cfg=make_config(),
                                     memory=None, run_dir=run_root, harness=harness,
                                     generation=1)

    assert len(bundle.reflections) == 1
    item = dict(bundle.reflections[0])
    entry = dict(item.get("entry") or {})
    for dropped in ("created_at", "run_id", "reflection_id", "prompt_tokens",
                    "llm_latency_ms"):
        assert dropped not in item, f"反思条目仍带运行期字段 {dropped}（F35）"
        assert dropped not in entry, f"反思 entry 仍带运行期字段 {dropped}（F35）"
    # 策略相关字段必须保留（别把修复做成「砍掉有用信息」）
    assert entry["text"] == "优先清护卫"
    assert entry["tags"] == ["low_virtue"]
    assert (entry["stages_cleared"], entry["illegal_attempts"]) == (6, 7)
    assert item["outcome"] == "win"

    # 这正是实跑里死掉的那一步：护栏必须过
    P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_f35_memory_metadata_not_carried_into_bundle(tmp_path):
    """F35 的同类面：记忆条目的 ``created_at`` / ``embedding`` 同样不得进证据束。"""
    memory = _FakeMemory([{
        "entry_id": "m-1", "lesson": "优先清护卫", "tags": ["low_virtue"],
        "created_at": "2026-10-09T16:34:13Z", "created_tick": 2026,
        "run_id": "run-1", "embedding": [2026.0, 2031.0],
        "content_sha256": "x" * 16,
    }])
    harness = tmp_path / "harness"
    harness.mkdir()
    bundle = P.build_evidence_bundle(run_id="run-1", n_traj=5, cfg=make_config(),
                                     memory=memory, run_dir=None, harness=harness,
                                     generation=1)

    item = dict(bundle.memory_items[0])
    for dropped in ("created_at", "created_tick", "embedding", "run_id",
                    "content_sha256"):
        assert dropped not in item, f"记忆条目仍带运行期字段 {dropped}（F35）"
    assert item["lesson"] == "优先清护卫"
    P.assert_no_holdout_leak(bundle, cfg=make_config())
