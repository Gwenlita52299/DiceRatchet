"""FR-07-2 / FR-07-3：证据束白名单与 INV-5 生成侧硬闸（PRD §11 单测 3）。

「给定-当-则」逐条对应 PRD §11-3：

- 给定证据束混入一条 ``split="heldout"`` 轨迹摘要，当 ``build_evidence_bundle``，
  则抛 ``E_EVAL_INV5_LEAK``；
- 给定反思文本出现 heldout seed ``2013`` 字面量，当 ``assert_no_holdout_leak``，
  则抛 ``E_EVAL_INV5_LEAK``（出现 ``20130`` / ``x2013y`` **不**误报，
  正则 ``(?<!\\d)2013(?!\\d)``）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamer_rsi.evolve import proposer as P
from gamer_rsi.evolve.errors import HoldoutLeakError
from gamer_rsi.evolve.schemas import EvidenceBundle, HarnessSnapshot
from gamer_rsi.infra.errors import E

from evolve_support import make_config

SEED_2013 = 2013 # H1 篮子内的 heldout seed（configs/seeds.yaml: H1=2000–2039）


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
        _bundle(tags=("harness:current", "heldout:2000"))
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
# seed 字面量扫描（正则 (?<!\d)2013(?!\d)）
# =====================================================================

@pytest.mark.parametrize("text", [
    "heldout seed 2013 的表现最好",
    "seed=2013",
    "2013", # 整值 token
    "scores: 2013,2014",
])
def test_seed_literal_is_leak(text):
    bundle = _bundle(reflect=[{"split": "train", "lesson": text}])
    with pytest.raises(HoldoutLeakError) as excinfo:
        P.assert_no_holdout_leak(bundle, cfg=make_config())
    assert str(excinfo.value.code) == str(E.E_EVAL_INV5_LEAK)


@pytest.mark.parametrize("text", [
    "20130",      # 整值 token 的右边界
    "x2013y",     # 左右都不是数字（但 2013 是独立 token！）
    "12013",
    "20135",
])
def test_seed_token_boundaries(text):
    """``20130`` / ``12013`` / ``20135`` 不误报。

    ``x2013y`` 中 ``2013`` 的左右字符非数字，按 PRD §11-3 的正则
    ``(?<!\\d)2013(?!\\d)`` 属**命中**——只有"与其它数字相邻"才排除。
    """
    bundle = _bundle(reflect=[{"split": "train", "lesson": text}])
    if text == "x2013y":
        with pytest.raises(HoldoutLeakError):
            P.assert_no_holdout_leak(bundle, cfg=make_config())
    else:
        P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_train_seed_literal_is_not_leak():
    """train 区间（1000–1999）的 seed 字面量不触发（只在 heldout/test 集合里扫）。"""
    bundle = _bundle(reflect=[{"split": "train", "lesson": "train seed 1042 的教训"}])
    P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_test_seed_literal_is_leak():
    bundle = _bundle(memory=[{"split": "train", "text": "test 池 9001 不可见"}])
    with pytest.raises(HoldoutLeakError):
        P.assert_no_holdout_leak(bundle, cfg=make_config())


@pytest.mark.parametrize("word", ["heldout", "seeds.yaml", "H1", "H2", "H3"])
def test_forbidden_labels_are_leak(word):
    bundle = _bundle(traj=[{"split": "train", "episode_key": f"note-{word}"}])
    with pytest.raises(HoldoutLeakError):
        P.assert_no_holdout_leak(bundle, cfg=make_config())


def test_evolve_config_can_narrow_the_scan():
    """``evolve.heldout_seeds`` 显式给出时，扫描集合按它收敛（仍含注册表）。"""
    cfg = make_config(heldout_seeds="heldout:2000-2039")
    bundle = _bundle(reflect=[{"split": "train", "lesson": "2013"}])
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
