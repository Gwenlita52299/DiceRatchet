"""B-92（F63）：**测量身份 = play 面指纹**必须从编排层传到臂归档，并在花钱前 fail-closed。

现场（F63，2026-10-14 正式实验主体发现）：候选臂归档只带 ``harness_version``，而它记的是
accept **之前**在役的号 ⇒ gen-001 的 ``parent`` 与「打过补丁的 ``cand-98119a8a4a90``」归档
provenance 除 ``arm``/``created_at`` 外**逐字段相同**（实测），于是：

1. 跨代继承（D-M3-7）用版本号比对 → 被接受的那代测量**永远还原不了** ⇒ 每代静默重测
   40 局 heldout ≈¥21 ≈1.7 h（``restore_arm_from_archive`` 的闸门见
   ``tests/unit/evals/test_evals_arm_checkpoint.py::test_b92_restore_*``）；
2. 「补丁是否真的改变了打法」这个 F66 的第二半没有可断言的凭据。

本文件覆盖 MOD-07 一侧：指纹的**传递**（编排层 → judge → evaluate_arm）与**花钱前的窄口径
断言**（声明改了 play 面、实测却没变 ⇒ 机器故障，不是「候选不好」）。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

# ``dice_ratchet.evolve.judge`` 这个**包属性**被同名函数遮住了（``evolve/__init__.py``
# 导出 ``judge`` 判定表函数）⇒ 取模块必须走 importlib，否则 monkeypatch 打不到模块上。
judge_mod = importlib.import_module("dice_ratchet.evolve.judge")
from dice_ratchet.evolve.judge import (_assert_play_face_actually_changed,
                                       _declared_play_plane_change)
from dice_ratchet.evolve.schemas import Candidate
from dice_ratchet.infra.config import AppConfig
from dice_ratchet.infra.errors import E, InfraError

from evolve_support import FakeArm, make_candidate  # noqa: E402


# ---- 窄口径：只有「声明改了 play 面」的候选才走断言 ------------------------------------

@pytest.mark.parametrize("touched,expected", [
    # **真实口径**（实测 runs/m3-formal 的三份候选 JSON）：带 `harness/` 前缀
    (["harness/prompts/decision.md"], True),
    (["harness/config.json"], True),
    (["harness/tools/score.py"], True),
    (["a/harness/prompts/decision.md"], True),   # diff 头的 `a/` 前缀
    (["b/harness/config.json"], True),
    (["prompts/decision.md"], True),             # 裸路径也要认（文档口径）
    (["./prompts/decision.md"], True),
    (["config.json"], True),
    (["tools/score.py"], True),
    (["harness/reflection.md"], False),          # 学习侧（不在 play_hash 内）⇒ 交给 C2 拒绝
    (["reflection.md"], False),
    (["dead_ends.md"], False),
    (["dice_ratchet/agent/prompts/fixed_v1.md"], False),  # 非 play 面的模板
    ([], False),                                  # 提案方没填 ⇒ 宁漏报，不误杀
])
def test_b92_play_plane_declaration_is_narrow(touched, expected):
    assert _declared_play_plane_change(make_candidate(touched=touched)) is expected


_ARCHIVED_CANDIDATES = sorted(Path(__file__).resolve().parents[3].glob(
    "runs/m3-formal/generations/gen-00*/candidates/*.json"))


@pytest.mark.skipif(not _ARCHIVED_CANDIDATES, reason="归档 run 不在（不影响逻辑本身）")
def test_b92_declaration_recognizes_the_real_archived_candidates():
    """**用真实归档兜底**：三份正式实验候选的 ``touched_files` 必须被判成「声明改了 play 面」。

    修复前（只认裸 ``prompts/decision.md``）这三份**一份都认不出来** ⇒ 断言永不触发，
    等于又写了一个「宣布了却不存在」的判据（F73 家族）。
    """
    import json as _json

    for path in _ARCHIVED_CANDIDATES:
        raw = _json.loads(path.read_text(encoding="utf-8"))
        candidate = Candidate.from_dict(raw.get("candidate") or raw)
        assert _declared_play_plane_change(candidate), (path.name, candidate.touched_files)


def test_b92_guard_fires_only_when_declared_and_unchanged():
    declared = make_candidate(touched=["prompts/decision.md"])
    learning_only = make_candidate(touched=["reflection.md"])
    # 声明改了 play 面、实测却与起点逐字节相同 ⇒ 机器故障
    with pytest.raises(InfraError) as exc:
        _assert_play_face_actually_changed(declared, play_hash="sha256:H",
                                           parent_play_hash="sha256:H")
    assert exc.value.code == E.E_EVOLVE_PATCH_INVALID
    # 真的变了 ⇒ 不抛
    _assert_play_face_actually_changed(declared, play_hash="sha256:H2",
                                       parent_play_hash="sha256:H")
    # 只改学习侧 ⇒ 不抛（规格上由 C2 合法拒绝，不是机器故障）
    _assert_play_face_actually_changed(learning_only, play_hash="sha256:H",
                                       parent_play_hash="sha256:H")
    # 拿不到指纹 ⇒ 不抛（交给 C2 判）
    _assert_play_face_actually_changed(declared, play_hash="", parent_play_hash="sha256:H")


# ---- 花钱之前：脏提案 0 局消耗 ---------------------------------------------------------

class _ExplodingEvaluator:
    """任何评估调用都失败——用来证明断言发生在**评估之前**。"""

    def eval_limits(self) -> dict[str, float]:
        return {"t_max": 120, "min_support": 3, "drop_rate_max": 0.10}

    def parent_arms(self, *a: Any, **kw: Any) -> Any:
        raise AssertionError("不该评估 parent（脏提案必须在花钱前被拦下）")

    def candidate_arms(self, *a: Any, **kw: Any) -> Any:
        raise AssertionError("不该评估候选（脏提案必须在花钱前被拦下）")


def test_b92_dirty_candidate_is_rejected_before_spending(monkeypatch, tmp_path):
    """声明改了 play 面但指纹没变 ⇒ ``E_EVOLVE_PATCH_INVALID``，且**一局都不跑**。"""
    from dice_ratchet.infra import events as events_mod

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(events_mod, "emit",
                        lambda event, **kw: emitted.append({"event": event, **kw}))
    report = judge_mod.evaluate_candidate(
        make_candidate(touched=["prompts/decision.md"]),
        type("P", (), {"version": 0, "dir": str(tmp_path)})(),
        cfg=AppConfig(), staging_dir=tmp_path, evaluator=_ExplodingEvaluator(),
        parent_heldout=FakeArm(arm="parent"), parent_smoke=FakeArm(arm="parent-smoke"),
        harness_version=0, generation=1, smoke_seeds=(1000,),
        play_hash="sha256:same", parent_play_hash="sha256:same")
    assert report.error_code == str(E.E_EVOLVE_PATCH_INVALID)
    assert report.verdict is None, "机器故障不该被当成规则判词"
    assert any(e["event"] == "evolve.evaluate"
               and e.get("error_code") == str(E.E_EVOLVE_PATCH_INVALID) for e in emitted), emitted


# ---- 传递：judge → evaluate_arm --------------------------------------------------------

def test_b92_arms_forward_the_play_hash_to_evaluate_arm(monkeypatch, tmp_path):
    """``parent_arms`` / ``candidate_arms`` 必须把指纹交给 ``evaluate_arm``（否则归档没身份）。"""
    from dice_ratchet import evals as evals_pkg

    captured: list[dict[str, Any]] = []

    def fake_evaluate_arm(harness_dir, **kwargs):
        captured.append({"harness_dir": str(harness_dir), **kwargs})
        return {"ok": True}

    monkeypatch.setattr(evals_pkg, "evaluate_arm", fake_evaluate_arm)
    staged = tmp_path / "staging" / "cand-abc"
    (staged / "prompts").mkdir(parents=True)
    (staged / "prompts" / "decision.md").write_text("# patched\n", encoding="utf-8")

    ev = judge_mod.CandidateEvaluator(config=AppConfig(), runner=object(), run_id="r-b92",
                                      generation=1, smoke_seeds=(1000,))
    ev.parent_arms(tmp_path, parent_version=0, play_hash="sha256:P")
    candidate = make_candidate(touched=["prompts/decision.md"])
    ev.candidate_arms(staged, candidate=candidate, harness_version=0, play_hash="sha256:C")

    assert [c["play_hash"] for c in captured] == ["sha256:P", "sha256:P",
                                                 "sha256:C", "sha256:C"]
    assert [c["arm"] for c in captured] == [
        "parent", "parent-smoke", f"cand-{candidate.candidate_id}-smoke",
        f"cand-{candidate.candidate_id}"]


# ---- 传递：编排层 → judge（parent 与候选各拿各的指纹，不串） ----------------------------

class _Recorder:
    """``CandidateEvaluator`` 的替身：只记录「谁拿到了哪个指纹」。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.smoke_seeds = (1000,)

    def parent_arms(self, parent_dir: Any, *, parent_version: int, inherited: Any = None,
                    play_hash: str = "", **kw: Any) -> Any:
        self.calls.append({"arm": "parent", "play_hash": str(play_hash)})
        return FakeArm(arm="parent"), FakeArm(arm="parent-smoke", n=2)

    def candidate_arms(self, staging_dir: Any, *, candidate: Any, harness_version: int,
                       play_hash: str = "", **kw: Any) -> Any:
        self.calls.append({"arm": f"cand-{candidate.candidate_id}",
                           "play_hash": str(play_hash)})
        return (FakeArm(arm="smoke", n=2),
                FakeArm(arm=f"cand-{candidate.candidate_id}"), {})


class _Verdict:
    error_code = None
    reason = ""


def test_b92_evaluate_candidate_hands_each_arm_its_own_hash(monkeypatch, tmp_path):
    """候选拿候选的指纹、起点拿起点的指纹——串了就等于给归档写了错误的身份。"""
    rec = _Recorder()
    monkeypatch.setattr(judge_mod, "smoke_fast_reject", lambda *a, **k: None)
    monkeypatch.setattr(judge_mod, "_compare_paired", lambda *a, **k: {})
    monkeypatch.setattr(judge_mod, "_paired_dict", lambda *a, **k: {})
    monkeypatch.setattr(judge_mod, "_paired_by_metric", lambda *a, **k: {})
    monkeypatch.setattr(judge_mod, "_diversity_dict", lambda *a, **k: {})
    monkeypatch.setattr(judge_mod, "judge", lambda *a, **k: _Verdict())

    candidate = make_candidate(touched=["prompts/decision.md"])
    report = judge_mod.evaluate_candidate(
        candidate, type("P", (), {"version": 0, "dir": str(tmp_path)})(),
        cfg=AppConfig(), staging_dir=tmp_path, evaluator=rec,
        harness_version=0, generation=1, smoke_seeds=(1000,),
        play_hash="sha256:C", parent_play_hash="sha256:P")
    assert report.error_code is None
    assert {c["arm"]: c["play_hash"] for c in rec.calls} == {
        f"cand-{candidate.candidate_id}": "sha256:C", "parent": "sha256:P"}
