"""B-93（F80）：臂归档的「种子身份」必须是**解析后列表**的指纹，而不是一个恒空的键。

现场（F80，2026-10-15）：`ARM_PROVENANCE_KEYS` 里一直有 `seeds_hash`，但训练/演化/基线路径
拿到的协议是 `coerce_protocol(None)`（代码默认，`seeds_hash=None`），而
`harness._seed_hash` 少了 `baselines.py::_seeds_protocol_tag` 那一句
`proto.seeds_hash or protocol_hash(proto)` ⇒ **实测 18/18 归档的该键都是空串**，和谁比都相等。
而 `configs/seeds.yaml` 不属 `AppConfig` ⇒ 换 seed 区间**不改** `config_hash`（F45-2 的
docstring 自己写了这条）⇒ 种子身份在整个复用决策里**没有任何一环**。

本文件钉住：① 归档必须记**解析后列表**的指纹（显式列表的 `basket_id` 全是 `"explicit"`、
协议摘要区分不了它们）；② 协议摘要那一栏必须是真的；③ 换 seed 列表 ⇒ 不复用。
"""

from __future__ import annotations

import json
from typing import Any

from dice_ratchet.evals import seeds as seeds_mod
from dice_ratchet.evals.harness import (ARM_PROVENANCE_KEYS, evaluate_arm,
                                        load_arm_archive)
from dice_ratchet.evals.seeds import default_protocol

from evals_support import FakeRunner

_PROTO = default_protocol()


def _run(tmp_path: Any, runner: FakeRunner, *, seeds: list[int], arm: str = "parent",
         **kw: Any) -> Any:
    base = dict(split="train", basket=tuple(seeds), memory_version=0, arm=arm,
                run_id="r-b93", protocol=_PROTO, runner=runner, generation=1,
                harness_version=0, run_dir=tmp_path)
    base.update(kw)
    return evaluate_arm(tmp_path / "harness", **base)


def test_b93_resolved_seed_list_digest_is_recorded(tmp_path):
    """归档 provenance 必须带**本次请求解析出的 seed 列表**的指纹。"""
    seeds = [1000, 1001, 1002, 1003]
    arm = _run(tmp_path, FakeRunner(), seeds=seeds)
    prov = load_arm_archive(arm.archive_path)["provenance"]
    assert "resolved_seeds_hash" in ARM_PROVENANCE_KEYS
    assert prov["resolved_seeds_hash"] == seeds_mod.seeds_digest(seeds)
    assert prov["resolved_seeds_hash"].startswith("sha256:")


def test_b93_protocol_seeds_hash_is_no_longer_empty(tmp_path):
    """`seeds_hash` 这一栏修复前**恒为空串**（协议默认值 `None` 没 fallback）。"""
    arm = _run(tmp_path, FakeRunner(), seeds=[1000, 1001])
    prov = load_arm_archive(arm.archive_path)["provenance"]
    assert prov["seeds_hash"] != "", "协议摘要又退回空串了（F80 的原始症状）"
    assert prov["seeds_hash"] == seeds_mod.protocol_hash(_PROTO)


def test_b93_different_explicit_seed_lists_are_not_the_same_measurement(tmp_path):
    """两个不同的**显式** seed 列表（`basket_id` 都是 ``"explicit"``）不得互相复用。

    修复前现场：第二次只跑 1004/1005，把归档里的 1002/1003 当成本次测量的一部分——
    provenance 却判「同一次测量」。
    """
    _run(tmp_path, FakeRunner(), seeds=[1000, 1001, 1002, 1003])
    second = FakeRunner()
    arm = _run(tmp_path, second, seeds=[1002, 1003, 1004, 1005])
    assert sorted(second.calls) == [1002, 1003, 1004, 1005], \
        "换了 seed 列表却复用了归档（F80 现场）"
    assert [r.seed for r in arm.results] == [1002, 1003, 1004, 1005]


def test_b93_same_set_in_another_order_still_matches(tmp_path):
    """反向护墙：**同一个集合**换个书写顺序 = 同一批测量，仍应整臂复用（不制造无谓重跑）。"""
    _run(tmp_path, FakeRunner(), seeds=[1000, 1001, 1002, 1003])
    second = FakeRunner()
    _run(tmp_path, second, seeds=[1003, 1000, 1002, 1001])
    assert second.calls == [], "同一 seed 集合换个顺序不该被当成另一批测量"


def test_b93_seeds_digest_is_normalized_and_total():
    """指纹按「排序 + 去重」归一；空列表也能表示（gen-000 的 train 窗口就是空）。"""
    assert seeds_mod.seeds_digest([3, 1, 2]) == seeds_mod.seeds_digest([1, 2, 3, 3])
    assert seeds_mod.seeds_digest([1, 2]) != seeds_mod.seeds_digest([1, 3])
    assert seeds_mod.seeds_digest([]).startswith("sha256:")


def test_b93_archive_collision_is_visible_in_the_path(tmp_path):
    """所有显式列表共用同一个归档文件名 —— 正因为如此，身份只能靠 `resolved_seeds_hash`。"""
    first = _run(tmp_path, FakeRunner(), seeds=[1000, 1001])
    second = _run(tmp_path, FakeRunner(), seeds=[1500, 1501])
    assert first.archive_path == second.archive_path
    assert json.loads(open(second.archive_path, encoding="utf-8").read())["provenance"][
        "resolved_seeds_hash"] == seeds_mod.seeds_digest([1500, 1501])
