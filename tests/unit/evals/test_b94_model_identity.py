"""B-94（F81）：臂归档的「模型身份」必须被真的写下来**并参与复用对拍**。

现场（F81，2026-10-15）：`model_hash` / `model_backend` 写在归档**顶层**、**不在**
`ARM_PROVENANCE_KEYS` 里，而且训练/演化/`eval test` 三条路径**都不传**它们 ⇒ 实测
**18/18 归档两个字段都是空串** ⇒「换了模型/权重」在复用决策里完全不可见。只有
`evals/cli.py` 的 baseline 路径传了真值（同一个字段，两种现实）。

本文件钉住：① 调用方不给时按 **policy 角色**从配置推导并写进 provenance；② 显式传参优先；
③ 换模型 ⇒ 不复用；④ 修复前那种「身份为空」的旧归档不得冒充已知身份。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dice_ratchet.evals.harness import (ARM_PROVENANCE_KEYS, evaluate_arm,
                                        load_arm_archive)
from dice_ratchet.evals.seeds import default_protocol
from dice_ratchet.inference.roles import role_model_ids
from dice_ratchet.infra.config import load_config

from evals_support import FakeRunner

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs"
_PROTO = default_protocol()
_SEEDS = [1000, 1001, 1002, 1003]


def _cfg(**overrides: Any) -> Any:
    return load_config(cli_overrides=overrides, config_dir=CONFIGS)


def _run(tmp_path: Any, runner: FakeRunner, *, config: Any = None, arm: str = "parent",
         seeds: Any = None, **kw: Any) -> Any:
    base = dict(split="train", basket=tuple(seeds or _SEEDS), memory_version=0, arm=arm,
                run_id="r-b94", protocol=_PROTO, runner=runner, generation=1,
                harness_version=0, run_dir=tmp_path, config=config)
    base.update(kw)
    return evaluate_arm(tmp_path / "harness", **base)


def test_b94_model_identity_is_derived_from_config(tmp_path):
    """调用方不传身份时，按 **policy 角色**（跑本臂的就是它）从配置推导，写进 provenance。"""
    cfg = _cfg()
    arm = _run(tmp_path, FakeRunner(), config=cfg)
    prov = load_arm_archive(arm.archive_path)["provenance"]
    assert {"model_hash", "model_backend"} <= set(ARM_PROVENANCE_KEYS)
    assert prov["model_hash"] == role_model_ids(cfg)["policy"] != ""
    assert prov["model_backend"] != "", "注册表可用时后端不该是空串"
    assert load_arm_archive(arm.archive_path)["model_hash"] == prov["model_hash"]


def test_b94_explicit_identity_wins(tmp_path):
    """显式传参优先于推导（`eval baseline` 那条路就是这么用的）。"""
    arm = _run(tmp_path, FakeRunner(), config=_cfg(), model_hash="m-x",
               model_backend="b-x")
    prov = load_arm_archive(arm.archive_path)["provenance"]
    assert prov["model_hash"] == "m-x" and prov["model_backend"] == "b-x"


def test_b94_changing_the_policy_model_disables_reuse(tmp_path):
    """同一个 run/臂/seed，换 policy 角色的模型 ⇒ 归档不得复用（修复前：复用）。"""
    _run(tmp_path, FakeRunner(), config=_cfg(**{"models.policy": "qwen3-8b-q4"}))
    second = FakeRunner()
    _run(tmp_path, second, config=_cfg(**{"models.policy": "mock"}))
    assert sorted(second.calls) == sorted(_SEEDS), \
        "换了模型却复用了上一次的测量（F81 现场）"


def test_b94_archive_with_unknown_identity_is_not_reused(tmp_path):
    """修复前落盘的归档（身份为空 = 未知）不得冒充已知身份 ⇒ fail-closed 重算。"""
    cfg = _cfg()
    arm = _run(tmp_path, FakeRunner(), config=cfg)
    path = Path(arm.archive_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provenance"]["model_hash"] = ""
    data["provenance"]["model_backend"] = ""
    data["model_hash"] = ""
    data["model_backend"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")

    second = FakeRunner()
    _run(tmp_path, second, config=cfg)
    assert sorted(second.calls) == sorted(_SEEDS)


def test_b94_without_config_identity_stays_unknown_and_reuse_still_works(tmp_path):
    """反向护墙：拿不到配置时身份就是「未知」（空串），同一条件下仍可整臂复用。"""
    _run(tmp_path, FakeRunner())
    second = FakeRunner()
    _run(tmp_path, second)
    assert second.calls == []


def test_b94_partial_archive_also_carries_the_identity(tmp_path):
    """跑到一半被杀时由 `_archive_on_result` 写的局部归档同样要带身份（否则续跑会丢）。"""
    class _Boom(FakeRunner):
        def run_episode(self, seed, **kw):  # noqa: ANN001, ANN003
            if len(self.calls) >= 2:
                raise KeyboardInterrupt("模拟：中途 Ctrl-C")
            return super().run_episode(seed, **kw)

    try:
        _run(tmp_path, _Boom(), config=_cfg())
    except KeyboardInterrupt:
        pass
    archives = list((tmp_path / "metrics" / "arms").glob("*.json"))
    assert archives, "局部归档没有落盘"
    prov = json.loads(archives[0].read_text(encoding="utf-8"))["provenance"]
    assert prov["model_hash"] == role_model_ids(_cfg())["policy"] != ""
