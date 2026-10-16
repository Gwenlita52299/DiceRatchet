"""F42-A/B：每代评估的检查点、复用与提案复用。

现场（``runs/m2-s32b-smoke-c1-medium6``，2026-10-10）：MOD-07 的 resume 单元只有**整代**
（``loop/generation.py::_cycle_artifacts_exist`` 看 ``generations/gen-NNN/{report,decision}.json``），
不在就把 ``phase`` 重置为 ``playing`` **重跑整代**；一代 84 局评估 ≈ **3.75 h / ≈¥47**，
中断即再付一次。引擎早就有 ``run_eval(resume_results=…, on_result=…)``（F18/F19/F20），
但 ``evaluate_arm`` **不转发**它们 —— 「中间那层接不上」正是 F42。

本文件覆盖：
- A：``resume_results`` / ``on_result`` 薄转发；臂归档 ``metrics/arms/``；fail-closed 复用规则；
- B：提案检查点（指纹一致 → 同 candidate id、0 次提案）；
- 端到端：一代在 parent 臂之后中断 → resume → parent 臂未被重评、提案 0 次调用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ``evolve_support`` / ``test_evolve_generation`` 住在 tests/unit/evolve/：按仓库既有惯例
# （见 tests/integration/evolve/evolve_it_support.py）把该目录放进 sys.path，以便这条
# 「一代 → 中断 → resume」的端到端用例复用 MOD-07 的既有夹具，而不是复制一份。
_EVOLVE_UNIT = Path(__file__).resolve().parents[1] / "evolve"
if str(_EVOLVE_UNIT) not in sys.path:
    sys.path.insert(0, str(_EVOLVE_UNIT))

from dice_ratchet.evals import harness as H
from dice_ratchet.evals.harness import (ARM_PROVENANCE_KEYS, arm_archive_path,
                                        evaluate_arm, load_arm_archive, provenance_matches,
                                        write_arm_archive)
from dice_ratchet.evals.seeds import default_protocol

from evolve_support import proposal_payload  # noqa: E402  （tests/unit/evolve 已在 sys.path 上）
from evals_support import FakeRunner, make_result

_PROTO = default_protocol()
#: train 篮子的 seed（``evaluate_arm`` 会经 ``assert_split_isolation`` 校验 split 与 seed 的对应）
_TRAIN_SEEDS = (1000, 1001, 1002, 1003)


def _run(tmp_path: Path, runner: FakeRunner, **kw: Any) -> Any:
    """在临时 run 目录上跑一臂（``archive=True``，走真实的归档读写路径）。"""
    base = dict(split="train", basket=_TRAIN_SEEDS, memory_version=0, arm="parent",
                run_id="r-f42", protocol=_PROTO, runner=runner, generation=1,
                harness_version=0, run_dir=tmp_path)
    base.update(kw)
    return evaluate_arm(tmp_path / "harness", **base)


# =====================================================================
# A1：resume_results / on_result 薄转发
# =====================================================================

def test_evaluate_arm_accepts_resume_results_and_skips_those_seeds(tmp_path):
    """``resume_results`` 生效：已完成的 seed **不再执行**（spy 计数）。

    修复前该 kwarg 不存在 ⇒ ``TypeError: evaluate_arm() got an unexpected keyword argument
    'resume_results'``。
    """
    runner = FakeRunner()
    done = [make_result(1000, split="train", generation=1, harness_version=0),
            make_result(1001, split="train", generation=1, harness_version=0)]
    arm = _run(tmp_path, runner, resume_results=done, archive=False)
    assert sorted(runner.calls) == [1002, 1003], "已完成的 seed 又被跑了一遍（那是钱）"
    assert [r.seed for r in arm.results] == [1000, 1001, 1002, 1003]
    assert arm.resumed_n == 2
    assert arm.pending_seeds == () # 全部补齐了


def test_evaluate_arm_calls_on_result_for_each_fresh_episode(tmp_path):
    """``on_result`` 每完成一局立刻回调（F18 的逐局落盘契约经 evaluate_arm 可用）。"""
    seen: list[int] = []
    runner = FakeRunner()
    _run(tmp_path, runner, on_result=lambda r: seen.append(int(r.seed)), archive=False)
    assert sorted(seen) == list(_TRAIN_SEEDS)
    assert runner.calls == list(_TRAIN_SEEDS)


# =====================================================================
# A2：臂归档
# =====================================================================

def test_evaluate_arm_archives_the_arm_with_provenance(tmp_path):
    """每次臂评估落 ``metrics/arms/gen001-train-<basket>-<arm>.json`` + provenance。"""
    arm = _run(tmp_path, FakeRunner())
    path = arm_archive_path(tmp_path, generation=1, split="train",
                            basket=arm.basket, arm="parent")
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    prov = data["provenance"]
    for key in ("generation", "split", "basket", "arm", "harness_version", "memory_version",
                "config_hash", "seeds_hash", "episode_config_hash", "n_seeds", "created_at"):
        assert key in prov, f"provenance 缺 {key}"
    assert prov["generation"] == 1 and prov["split"] == "train" and prov["arm"] == "parent"
    assert prov["n_seeds"] == len(_TRAIN_SEEDS)
    assert data["metrics"]["n"] == len(_TRAIN_SEEDS)
    assert len(data["episodes"]) == len(_TRAIN_SEEDS)
    assert arm.archive_path == str(path)


def test_no_archive_when_the_run_directory_does_not_exist(tmp_path):
    """哑 ``run_id``（没有 run 目录）不产归档，也不读归档 —— 单测因此天然无副作用。"""
    arm = _run(tmp_path / "nope", FakeRunner(), run_dir=None, archive=True)
    assert arm.archive_path == ""
    assert not (tmp_path / "nope" / "metrics" / "arms").exists()


# =====================================================================
# A3：复用规则（fail-closed）
# =====================================================================

def test_second_identical_call_reuses_the_whole_arm_without_running_anything(tmp_path):
    """provenance 全等且覆盖完整 seed 列表 ⇒ **整臂复用**，一局都不跑，发 ``evals.arm_reused``。"""
    events: list[dict] = []
    first = FakeRunner()
    _run(tmp_path, first)
    assert first.calls == list(_TRAIN_SEEDS)

    second = FakeRunner()
    arm = _run(tmp_path, second)
    assert second.calls == [], "整臂复用失败：第二次调用又跑了局（每局都是钱）"
    assert [r.seed for r in arm.results] == list(_TRAIN_SEEDS)
    assert arm.dur_s == 0.0 and arm.resumed_n == len(_TRAIN_SEEDS)

    # 事件必须可观测（否则「省了钱」只能靠推断）
    import dice_ratchet.infra.events as events_mod
    events = []
    original = events_mod.emit
    events_mod.emit = lambda event, **kw: events.append({"event": event, **kw})
    try:
        _run(tmp_path, FakeRunner())
    finally:
        events_mod.emit = original
    assert any(e["event"] == "evals.arm_reused" for e in events), events


@pytest.mark.parametrize("field,value", [
    ("harness_version", 7),
    ("memory_version", 9),
    ("config_hash", "sha256:deadbeef"),
    ("seeds_hash", "other-seeds"),
    ("generation", 2),
    ("split", "heldout"),
    ("basket", "H1"),
    ("arm", "cand-xyz"),
    ("episode_config_hash", "sha256:abc"),
    # B-92 / F63：测量身份（play 面指纹）必须参与对拍——否则「打过补丁的候选臂」与
    # 「裸 v0 臂」在版本号相同（都是 accept 之前的号）时无法区分。
    ("harness_play_hash", "sha256:another-play-face"),
    # B-93 / F80、B-94 / F81：种子身份与模型身份同样要参与对拍
    ("resolved_seeds_hash", "sha256:another-seed-list"),
    ("model_hash", "another-model"),
    ("model_backend", "another-backend"),
])
def test_any_provenance_mismatch_disables_reuse(tmp_path, field, value):
    """**fail-closed**：provenance 任一字段不同 ⇒ 不复用，照常重算。"""
    _run(tmp_path, FakeRunner())
    path = arm_archive_path(tmp_path, generation=1, split="train",
                            basket="explicit", arm="parent")
    # 直接篡改归档里的该字段（模拟「换了 harness / 换了种子区间 / 换了局型」）
    if not path.is_file():
        path = next((tmp_path / "metrics" / "arms").glob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provenance"][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    runner = FakeRunner()
    _run(tmp_path, runner)
    assert sorted(runner.calls) == sorted(_TRAIN_SEEDS), \
        f"provenance 的 {field} 不一致却复用了"


def test_provenance_matches_ignores_created_at():
    """``created_at`` 不参与对拍（否则归档永远不可能被复用）。"""
    a = {"generation": 1, "split": "train", "basket": "explicit", "arm": "parent",
         "harness_version": 0, "memory_version": 0, "config_hash": None, "seeds_hash": "s",
         "episode_config_hash": None, "created_at": "2026-01-01T00:00:00Z"}
    b = dict(a, created_at="2030-01-01T00:00:00Z")
    assert provenance_matches(a, b)
    assert "created_at" not in ARM_PROVENANCE_KEYS


def test_partial_archive_only_reruns_the_missing_seeds(tmp_path):
    """只覆盖一部分 ⇒ 已完成的作为 ``resume_results``，只跑缺的局。"""
    _run(tmp_path, FakeRunner())
    # 把归档裁到前 2 局（模拟「跑到一半被杀」）
    path = next((tmp_path / "metrics" / "arms").glob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["episodes"] = data["episodes"][:2]
    path.write_text(json.dumps(data), encoding="utf-8")

    runner = FakeRunner()
    arm = _run(tmp_path, runner)
    assert sorted(runner.calls) == [1002, 1003], "局部复用没有只跑缺的局"
    assert [r.seed for r in arm.results] == list(_TRAIN_SEEDS)
    assert arm.resumed_n == 2


def test_corrupt_archive_is_ignored(tmp_path):
    """归档损坏 ⇒ 不复用、照常重算（不得抛错）。"""
    _run(tmp_path, FakeRunner())
    path = next((tmp_path / "metrics" / "arms").glob("*.json"))
    path.write_text("{not json", encoding="utf-8")
    runner = FakeRunner()
    _run(tmp_path, runner)
    assert sorted(runner.calls) == sorted(_TRAIN_SEEDS)


def test_archive_round_trip_preserves_every_episode_field(tmp_path):
    """归档 → 复用的逐局结果必须与首次评估**逐字段一致**（否则「省钱」换了测量结果）。"""
    first = _run(tmp_path, FakeRunner(outcomes={1000: "lose"}, degraded_seeds=(1001,)),
                 arm="parent")
    path = arm_archive_path(tmp_path, generation=1, split="train",
                            basket=first.basket, arm="parent")
    archive = load_arm_archive(path)
    assert archive is not None
    # 归档里的数字是**四舍五入后**的（可读性），逐字段比对用容差
    for key, value in archive["metrics"].items():
        expected = first.metrics.as_dict()[key]
        if isinstance(value, float):
            assert value == pytest.approx(expected, abs=1e-3), key
        elif isinstance(value, list) and value and isinstance(value[0], float):
            assert value == pytest.approx(expected, abs=1e-3), key
        else:
            assert value == expected, key
    reused = _run(tmp_path, FakeRunner())
    # ``arm`` 是**执行器**填的标签（`FakeRunner` 填 ""），归档原样还原它 ⇒ 不参与比对；
    # 其余每个测量字段必须逐字段一致。
    assert [{k: v for k, v in r.as_dict().items() if k != "arm"} for r in reused.results] == \
        [{k: v for k, v in r.as_dict().items() if k != "arm"} for r in first.results]
    assert reused.metrics.as_dict() == first.metrics.as_dict()


def test_write_arm_archive_is_deterministic_apart_from_created_at(tmp_path):
    """同一 ``ArmResult`` 写两次：除 ``created_at`` 外逐字节一致。"""
    arm = _run(tmp_path, FakeRunner())
    archive = load_arm_archive(arm.archive_path)
    assert archive is not None
    first = json.loads(Path(arm.archive_path).read_text(encoding="utf-8"))
    write_arm_archive(tmp_path, generation=1, split="train", basket=arm.basket, arm="parent",
                      result=arm, provenance=first["provenance"])
    second = json.loads(Path(arm.archive_path).read_text(encoding="utf-8"))
    # 归档内部记录的 `archive_path` 在首次写入时为空（路径由返回值补充），两次写入同源
    assert first["metrics"] == second["metrics"]
    assert first["episodes"] == second["episodes"]
    assert first["provenance"] == second["provenance"]


# =====================================================================
# 端到端：一代在 parent 臂之后中断 → resume → parent 0 重跑、提案 0 次调用
# =====================================================================
#
# 这条用例把 F42-A（臂归档）与 F42-B（提案检查点）接在**同一条生产路径**上：
# `evolve.generation.run_one_generation`（就是 `loop run` 调的那一个）。
# 断言的是**它跑了几局、调了几次 proposer**，而不是「事件里写了什么」。

class _Adapter:
    """``FakeRunner`` → MOD-08 ``EpisodeRunner`` 协议（与 ``EvalRunnerAdapter`` 同形）。"""

    def __init__(self, runner: Any, *, interrupt_after: int | None = None) -> None:
        self.runner = runner
        self.interrupt_after = interrupt_after
        self.calls: list[int] = []

    def run_episode(self, seed: int, *, split: str, arm: str, generation: int,
                    harness_version: int, episode_config: Any = None,
                    timeout_s: float | None = None) -> Any:
        self.calls.append(int(seed))
        if self.interrupt_after is not None and len(self.calls) > self.interrupt_after:
            raise KeyboardInterrupt("模拟：一代评估中途被 Ctrl-C")
        return self.runner.run_episode(seed, split=split, arm=arm, generation=generation,
                                       harness_version=harness_version,
                                       timeout_s=timeout_s)


class _CountingClient:
    """计数的 MOD-03 替身（**不消耗**脚本：每次成功返回同一份提案 payload）。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, messages: Any, schema: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return {"candidates": [proposal_payload()]}


def test_generation_resume_does_not_re_evaluate_the_parent_arm(tmp_path, monkeypatch):
    """干跑一代 → parent 臂之后中断 → resume：parent 侧 0 重评、提案 0 次调用。"""
    import importlib

    from dice_ratchet.infra.errors import E
    from test_evolve_generation import RUN_ID, _OkReport, _paired, _setup  # noqa: E402

    # MOD-02 打桩（静态扫描通过 + 受限加载返回哑对象），与 test_evolve_generation 同款
    class _Worker:
        def close(self) -> None:
            pass

    gen_mod = importlib.import_module("dice_ratchet.evolve.generation")
    judge_mod = importlib.import_module("dice_ratchet.evolve.judge")
    monkeypatch.setattr(gen_mod, "guard_gate", lambda staging, run_dir=None: _OkReport())
    monkeypatch.setattr(gen_mod, "load_harness_tools_checked", lambda staging: _Worker())
    monkeypatch.setattr(judge_mod, "_compare_paired",
                        lambda candidate_arm, parent_arm: _paired(delta=0.10))

    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)

    # 第一轮：提案成功、parent heldout 跑完**第 1 局**就被杀。
    # 用默认 ``n_workers=1``（串行）让中断点确定：并发时 ``_pump`` 会预先提交多个 seed，
    # 「第 N 次调用就抛」不再等价于「恰好 N 局落盘」。
    client = _CountingClient()
    first = _Adapter(FakeRunner(), interrupt_after=1)
    evaluator = judge_mod.CandidateEvaluator(
        protocol=None, config=app_cfg, memory_version=1, run_id=RUN_ID, generation=1,
        smoke_seeds=(1000, 1001), runner=first, run_dir=run_root)
    with pytest.raises(KeyboardInterrupt):
        gen_mod.run_one_generation(
            state=state, run_ctx=run_ctx, runner=first, config=app_cfg,
            evaluator=evaluator, client=client, harness=harness, run_dir=run_root, seed=7)
    assert client.calls == 1, "第一轮应当真实调用过一次 proposer"
    first_calls = list(first.calls)
    # 反例自检：parent 臂的归档必须真的落盘，否则「0 重跑」是空转
    arms = sorted(p.name for p in (run_root / "metrics" / "arms").glob("*.json"))
    assert any("parent" in name for name in arms), arms

    assert gen_mod.GenPaths.of(run_root, 1).report.is_file() is False

    # resume：提案复用（0 次调用），parent 臂已完成的 seed 不再执行
    second_client = _CountingClient()
    second = _Adapter(FakeRunner())
    evaluator2 = judge_mod.CandidateEvaluator(
        protocol=None, config=app_cfg, memory_version=1, run_id=RUN_ID, generation=1,
        smoke_seeds=(1000, 1001), runner=second, run_dir=run_root)
    outcome = gen_mod.run_one_generation(
        state=state, run_ctx=run_ctx, runner=second, config=app_cfg,
        evaluator=evaluator2, client=second_client, harness=harness, run_dir=run_root,
        seed=7)

    assert second_client.calls == 0, "提案被复用后仍然调了 proposer（每次调用都是钱）"
    assert outcome.generation == 1
    assert gen_mod.GenPaths.of(run_root, 1).report.is_file(), "resume 后该代必须跑完"
    assert first_calls, "第一轮一局都没跑？这条用例失去意义"

    # 「parent 侧重复评估局数 = 0」的口径：parent **heldout** 臂在 resume 时完全没跑
    # （它的前 2 局已在第一轮落进 `metrics/arms/`）。注意 parent-smoke 是**另一个臂**
    # （不同 `arm` 名 ⇒ 不同归档路径），本代的 smoke 仍要跑——那是设计如此，不是重复劳动。
    parent_archive = next((run_root / "metrics" / "arms").glob("gen001-heldout-H1-parent.json"),
                          None)
    assert parent_archive is not None, sorted(
        p.name for p in (run_root / "metrics" / "arms").glob("*.json"))
    data = json.loads(parent_archive.read_text(encoding="utf-8"))
    assert data["provenance"]["n_seeds"] == 40
    assert data["resumed_n"] >= 1, (
        f"resume 时 parent heldout 没有复用已完成的局：resumed_n={data['resumed_n']}")
    assert data["metrics"]["n"] == len(data["episodes"]) == 40, \
        "resume 后 parent heldout 必须完整（40 局 H1）"


# =====================================================================
# B-92（F63）：臂归档必须记「测量身份」（play 面指纹），跨代继承按它还原
# =====================================================================

def test_b92_archive_records_the_measured_play_hash(tmp_path):
    """`evaluate_arm(play_hash=…)` 把本臂实际测的 play 面写进 provenance，并参与复用对拍。"""
    first = FakeRunner()
    arm = _run(tmp_path, first, play_hash="sha256:play-v0")
    data = json.loads(Path(arm.archive_path).read_text(encoding="utf-8"))
    assert data["provenance"]["harness_play_hash"] == "sha256:play-v0"
    assert "harness_play_hash" in ARM_PROVENANCE_KEYS

    # 同一个 play 面 ⇒ 整臂复用（0 局）
    same = FakeRunner()
    _run(tmp_path, same, play_hash="sha256:play-v0")
    assert same.calls == [], "同一 play 面的第二次评估又跑了局"

    # 换了 play 面 ⇒ 不复用（修复前版本号相同就复用，F63 现场）
    other = FakeRunner()
    _run(tmp_path, other, play_hash="sha256:play-v1")
    assert sorted(other.calls) == sorted(_TRAIN_SEEDS), "play 面不同却复用了归档"


def _archive_with_play(tmp_path, *, play_hash, harness_version=0, seeds=_TRAIN_SEEDS):
    arm = _run(tmp_path, FakeRunner(), harness_version=harness_version,
               play_hash=play_hash)
    return load_arm_archive(arm.archive_path)


def test_b92_restore_matches_by_play_face_across_versions(tmp_path):
    """跨代继承的**核心回归**：被接受的那代候选归档记的是 accept 之前的版本号。

    实测现场（`runs/m3-formal/metrics/arms/gen001-heldout-H1-cand-98119a8a4a90.json`）：
    归档 `harness_version=0`，而下一代 `parent.version=1` ⇒ 修复前
    `restore_arm_from_archive(harness_version=1)` 返回 `None`，D-M3-7 的省钱路径在**唯一
    它想优化的场景**（accept 之后）静默退化成重测 40 局 ≈¥21。
    """
    from dice_ratchet.evals.harness import restore_arm_from_archive

    archive = _archive_with_play(tmp_path, play_hash="sha256:playP")
    # 版本号对不上（0 vs 1），但 play 面相同 ⇒ 必须能还原
    restored = restore_arm_from_archive(archive, generation=2, harness_version=1,
                                       arm="parent", required_seeds=_TRAIN_SEEDS,
                                       play_hash="sha256:playP")
    assert restored is not None, "play 面相同却按版本号误杀（F63 的隐形税）"
    assert [int(r.seed) for r in restored.results] == list(_TRAIN_SEEDS)
    assert all(int(r.harness_version) == 1 for r in restored.results), \
        "还原时版本号应取请求值（它扮演的是本代 parent）"


def test_b92_restore_rejects_a_different_play_face(tmp_path):
    """play 面不同 ⇒ fail-closed 返回 ``None``（调用方重测）。"""
    from dice_ratchet.evals.harness import restore_arm_from_archive

    archive = _archive_with_play(tmp_path, play_hash="sha256:playP")
    assert restore_arm_from_archive(archive, generation=2, harness_version=0,
                                    arm="parent", required_seeds=_TRAIN_SEEDS,
                                    play_hash="sha256:playQ") is None


def test_b92_restore_legacy_archive_falls_back_to_version(tmp_path):
    """**旧归档**（没有 play 面指纹）仍按版本号判，行为与修复前一致。"""
    from dice_ratchet.evals.harness import restore_arm_from_archive

    archive = _archive_with_play(tmp_path, play_hash="")
    # 修复前的归档里根本没有这个键（不是空串）——按「键缺失」模拟
    archive["provenance"].pop("harness_play_hash", None)
    assert "harness_play_hash" not in archive["provenance"]
    assert restore_arm_from_archive(archive, generation=2, harness_version=0,
                                    arm="parent", required_seeds=_TRAIN_SEEDS,
                                    play_hash="sha256:whatever") is not None
    assert restore_arm_from_archive(archive, generation=2, harness_version=1,
                                    arm="parent", required_seeds=_TRAIN_SEEDS,
                                    play_hash="sha256:whatever") is None
