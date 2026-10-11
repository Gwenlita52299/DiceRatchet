"""D-M3-3 裁决 A（Spec v2.9 / B-65）：G3 的执行口径按臂分层。

**现场（两个独立事实，都是实测）**：

1. ``check_report_determinism``（FR-08-23/24 / G3）从 M1 起**没有任何生产调用点**——
   ``grep -rn check_report_determinism dice_ratchet/`` 只命中它自己的定义与 ``__all__``。
   也就是说「同参数重跑 ``report.sha256`` 必须一致，否则抛 ``E_EVAL_NONDETERMINISTIC``」
   这条承诺**在真实 run 里从未被执行过**，它只是一句文档。
2. 当前主力臂 ``C1`` 是 **API 臂**：``configs/models.yaml`` 给 ``deepseek-flash`` 标注
   ``api_supports_seed: false``，实测「字节相同的请求 9 次返回 9 个不同响应」
   ⇒ **端到端逐字节可复现在物理上不成立**。

把这两件事放在一起就得到本裁决：**不能**把旧的硬门原样接进生产（第一个 API run 就会
假报 ``E_EVAL_NONDETERMINISTIC``），也**不能**让它继续没有任何执行点。改为按臂分层：

- **可复现层**（代码 / 配置 / seed / 聚合口径 / 并发归一化）⇒ 本地臂**仍然硬执行**；
- **LLM 层** ⇒ 不承诺逐字节；采样照记、响应按 request hash 归档、变化即标
  ``llm.nondeterministic``（MOD-03 既有口径）；
- **臂内含任何 ``provider=api`` 的角色** ⇒ 跳过硬门、发 ``eval.nondeterministic``
  事件（``determinism=api_nondeterministic``），**不抛异常**。

诚实边界（本文件的用例也是在钉住这句话）：这**降低了「可复现」的强度**，必须在报告里
显式声明，不得据此宣称「API 臂的 M3 结果可逐字节复现」。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dice_ratchet.evals.harness import (check_report_determinism,
                                        enforce_report_determinism, report_sha_path,
                                        reproducibility_of, write_generation_report)
from dice_ratchet.evals.metrics import (aggregate, judge_gain,
                                        render_report_sections)
from dice_ratchet.evals.diversity import CollapseConfig, collapse_check
from dice_ratchet.infra.config import load_config
from dice_ratchet.infra.errors import E, InfraError

from evals_support import make_result

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs"
HARNESS_CONFIG = REPO / "harness/config.json"

#: H1 的首个 seed 取自**协议本身**（不写死数字：F45-2 把区间从四位移到六位后，
#: 写死 2000 会让本文件静默测另一个 split）。
_H1_START = 731000


@pytest.fixture(scope="module")
def base_cfg():
    return load_config(config_dir=CONFIGS, harness_config=HARNESS_CONFIG)


def _app_cfg(base, arm: str):
    return replace(base, experiment=replace(base.experiment, arm=arm))


def _report():
    rows = [make_result(_H1_START + i, outcome="win" if i % 2 else "lose")
            for i in range(10)]
    heldout = aggregate(rows, basket="H1")
    train = aggregate(rows, split="train", basket="train_window")
    gain = judge_gain(train=train, heldout=heldout)
    diversity = collapse_check(curves=[list(heldout.action_entropy_curve)],
                               jsd_kind=[None], win_rates=[heldout.win_rate],
                               cfg=CollapseConfig())
    return render_report_sections(generation=1, harness_version=1, parent_version=0,
                                  heldout=heldout, train=train, gain=gain,
                                  diversity=diversity, ablation=None,
                                  decision="pending", decided_by="auto", reason="")


# =====================================================================
# 口径判定：哪些臂能承诺端到端逐字节可复现
# =====================================================================

def test_local_arm_is_reproducible(base_cfg):
    """默认臂 `A1` 三角色全是本地 MLX ⇒ 承诺可复现，锁硬执行。"""
    ok, reason = reproducibility_of(_app_cfg(base_cfg, "A1"))
    assert ok is True, reason
    assert reason == "no_api_roles"


def test_api_arm_is_not_reproducible(base_cfg):
    """`C1` 三角色全是 `provider=api` ⇒ 不承诺可复现，且 reason 要点名是哪个角色哪个模型。

    反例自检：若实现写成「若任意角色 provider != local 则算不可复现」，本用例仍会过；
    故额外断言 `reason` 携带 `api_role:` 前缀与 `deepseek-flash`——只有真的读到了
    注册表里的 provider 才能给出这个名字。
    """
    ok, reason = reproducibility_of(_app_cfg(base_cfg, "C1"))
    assert ok is False
    assert reason.startswith("api_role:")
    assert "deepseek-flash" in reason


def test_registry_unavailable_fails_closed_to_not_reproducible():
    """**fail-closed 方向**：配置不可解析 ⇒ 判「不可复现」，绝不判「可复现」。

    判错的代价是不对称的：误判为**可复现** ⇒ API run 假报
    `E_EVAL_NONDETERMINISTIC`（一代白跑，≈¥47）；误判为**不可复现** ⇒ 只是少执行
    一次检查（并发归一化的一致性另有 T-15 的 n=1 vs n>1 用例守着）。
    """
    ok, reason = reproducibility_of(object())
    assert ok is False
    assert reason.startswith("registry_unavailable:")


# =====================================================================
# 生产入口：enforce_report_determinism
# =====================================================================

def test_enforce_is_a_hard_gate_for_local_arms(tmp_path, base_cfg):
    """本地臂：行为与旧的 `check_report_determinism` **完全一致**（含抛异常）。"""
    report = _report()
    _path, sha = write_generation_report(tmp_path, 1, report)
    assert enforce_report_determinism(tmp_path, 1, report,
                                      app_cfg=_app_cfg(base_cfg, "A1")) == sha

    tampered = dict(report)
    tampered["generation"] = 2
    with pytest.raises(InfraError) as exc:
        enforce_report_determinism(tmp_path, 1, tampered,
                                   app_cfg=_app_cfg(base_cfg, "A1"))
    assert exc.value.code == E.E_EVAL_NONDETERMINISTIC


def test_enforce_does_not_raise_for_api_arms(tmp_path, base_cfg):
    """**核心用例**：API 臂重跑报告字节不同 ⇒ 不得抛 `E_EVAL_NONDETERMINISTIC`。

    修复前若把 `check_report_determinism` 原样接进生产，本场景就是第一个 API run 的
    必然结局（一代 ≈4.8 h / ≈¥100 白跑）。
    """
    report = _report()
    write_generation_report(tmp_path, 1, report)
    # 模拟「API 臂重跑后数值略有不同」——真实原因见 `api_supports_seed=false`
    drifted = dict(report)
    drifted["heldout"] = dict(report["heldout"])
    drifted["heldout"]["win_rate"] = float(report["heldout"]["win_rate"]) + 0.025
    got = enforce_report_determinism(tmp_path, 1, drifted,
                                     app_cfg=_app_cfg(base_cfg, "C1"))
    # 返回值是**本次**报告的 sha，且必须与盘上旧值不同（不同是预期行为，不是缺陷）
    stored = report_sha_path(tmp_path, 1).read_text(encoding="utf-8").strip()
    assert got != stored


def test_enforce_emits_observable_event_for_api_arms(tmp_path, base_cfg, monkeypatch):
    """跳过硬门**必须可观测**：否则「G3 未执行」会变成新的静默缺口（与 F30/F47 同型）。"""
    seen: list[tuple[str, dict]] = []
    import dice_ratchet.infra.events as events_mod

    monkeypatch.setattr(events_mod, "emit",
                        lambda ev, **f: seen.append((ev, f)))
    enforce_report_determinism(tmp_path, 1, _report(), app_cfg=_app_cfg(base_cfg, "C1"))
    assert [e for e, _ in seen] == ["eval.nondeterministic"]
    _, fields = seen[0]
    assert fields["determinism"] == "api_nondeterministic"
    assert fields["generation"] == 1
    assert "request hash" in fields["repro_scope"]


def test_enforce_emits_nothing_for_local_arms(tmp_path, base_cfg, monkeypatch):
    """反例自检的对称面：本地臂**不得**发 `eval.nondeterministic`（否则该事件失去含义）。"""
    seen: list[str] = []
    import dice_ratchet.infra.events as events_mod

    monkeypatch.setattr(events_mod, "emit", lambda ev, **f: seen.append(ev))
    report = _report()
    write_generation_report(tmp_path, 1, report)
    enforce_report_determinism(tmp_path, 1, report, app_cfg=_app_cfg(base_cfg, "A1"))
    assert seen == []


def test_event_is_registered():
    """新事件必须先登记（`infra/events.py` 的生产模式会把未登记事件改写成
    `infra.unknown_event` ⇒ 静默丢失必需字段）。"""
    from dice_ratchet.infra.events import EVENT_REGISTRY

    spec = EVENT_REGISTRY["eval.nondeterministic"]
    assert spec.module == "eval"
    assert {"generation", "determinism"} <= set(spec.required_fields)


def test_production_call_site_exists():
    """**这是本裁决最关键的一条护栏**：函数存在但没有调用点 = 承诺从未被执行。

    上一版就是这个病：`check_report_determinism` 有定义、有导出、有单测，
    `grep -rn check_report_determinism dice_ratchet/` 却只命中它自己。
    所以这里直接断言**生产源码里有一个调用点**。
    """
    src = (REPO / "dice_ratchet/evolve/generation.py").read_text(encoding="utf-8")
    assert "enforce_report_determinism(" in src
    # 且它必须出现在**写报告之前**（先判可复现性，再落盘）
    assert src.index("enforce_report_determinism(") < src.index(
        "write_generation_report(run_root, gen, report)")


def test_reproducibility_reason_is_json_safe(base_cfg):
    """事件字段要能进 `events.jsonl`（MOD-09 序列化）。"""
    for arm in ("A1", "C1"):
        _ok, reason = reproducibility_of(_app_cfg(base_cfg, arm))
        json.dumps({"reason": reason}, ensure_ascii=False)
