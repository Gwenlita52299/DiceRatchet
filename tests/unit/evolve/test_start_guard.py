"""起点守卫（B，2026-10-17）：未经门禁裁决的 play 面不得起跑。

现场：P1（人工去重补丁）在起跑前被写进 ``harness/`` 并记成 version 0；循环的契约是
「起点 harness = 基线」（``write_gen000_baseline`` 只测不裁决），于是 P1 从未作为 candidate
过门禁，却被当成父代继续演化——P2 的 accept 建立在 held-out 0.05（vs 裸 v0 0.175）之上。

守卫的判据只有一条：起点的 ``play_hash`` 必须等于
  * ``harness/BASELINE.json`` 登记的原始 v0，或
  * 任一 ``harness/versions/v{N}/`` 已接受快照。
否则 ``E_EVOLVE_UNGATED_START``（退出码 2），**一局都不跑**。
"""

from __future__ import annotations

import pytest

from dice_ratchet.evolve import versioning as V
from dice_ratchet.evolve.versioning import (assert_start_play_plane_accepted, record_baseline,
                                            snapshot_harness)
from dice_ratchet.infra.errors import E, InfraError

from evolve_support import DECISION_MD, write_harness


def _edit_play(base):
    (base / "prompts" / "decision.md").write_text(DECISION_MD + "\n13. 新增一条规则\n",
                                                  encoding="utf-8")


def test_pristine_baseline_passes(tmp_path):
    base = write_harness(tmp_path, with_v0=False)
    record_baseline(base, note="原始 v0")
    result = assert_start_play_plane_accepted(base)
    assert result["ok"] is True
    assert result["source"].startswith("baseline:")


def test_ungated_play_change_is_refused(tmp_path):
    base = write_harness(tmp_path, with_v0=False)
    record_baseline(base)
    _edit_play(base)
    with pytest.raises(InfraError) as excinfo:
        assert_start_play_plane_accepted(base)
    assert str(excinfo.value.code) == str(E.E_EVOLVE_UNGATED_START)
    assert "没有被门禁裁决过" in excinfo.value.message


def test_acceptance_allow_ungated_is_traced(tmp_path):
    base = write_harness(tmp_path, with_v0=False)
    record_baseline(base)
    _edit_play(base)
    result = assert_start_play_plane_accepted(base, allow_ungated=True)
    assert result["ok"] is True
    assert result["source"] == "allow_ungated"


def test_accepted_version_snapshot_passes(tmp_path):
    base = write_harness(tmp_path, with_v0=False)
    record_baseline(base)
    _edit_play(base)
    snapshot_harness(1, harness=base, reason="accepted")
    result = assert_start_play_plane_accepted(base)
    assert result["ok"] is True
    assert result["source"] == "accepted:v1"


def test_learning_plane_change_does_not_trip_guard(tmp_path):
    """``dead_ends.md`` / ``version.json`` 不在 play 面内：死路记录不改变打法，不该拦起跑。"""
    base = write_harness(tmp_path, with_v0=False)
    record_baseline(base)
    (base / "dead_ends.md").write_text("## gen-002 · x\n- 结局：reject\n", encoding="utf-8")
    assert_start_play_plane_accepted(base)  # 不抛


def test_missing_baseline_record_refuses_everything(tmp_path):
    """连锚点都没登记 ⇒ 白名单为空 ⇒ fail-closed（新项目必须先 ``record_baseline``）。"""
    base = write_harness(tmp_path, with_v0=False)
    with pytest.raises(InfraError) as excinfo:
        assert_start_play_plane_accepted(base)
    assert str(excinfo.value.code) == str(E.E_EVOLVE_UNGATED_START)
    assert "BASELINE.json" in excinfo.value.hint
