"""MOD-05 golden（G2，PRD §12）：固定输入的检索输出与 ``memory_v0`` 快照 sha256。

golden 由 ``regen_memory_golden.py`` 显式生成；本文件只读比对。任何改变检索打分、
渲染、去重/合并或快照字节的改动都必须显式重跑 regen 并在 PR 说明。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from memory_case import (CONFIG_DIR, OBS, RETRIEVE_GOLDEN, SHA256_GOLDEN, build,
                         golden_view)  # noqa: E402

from dice_ratchet.memory import MemoryStore


@pytest.fixture
def case(tmp_path: Path):
    return build(tmp_path)


def test_retrieve_matches_golden(case) -> None:
    """GWT-05-05：固定 tick 与 golden → 输出逐字节相同（G3）。"""
    expected = json.loads(RETRIEVE_GOLDEN.read_text("utf-8"))
    assert case.retrieve == expected, (
        "检索产物变化：确认是否有意为之；有意则运行 "
        "`.venv/bin/python tests/golden/memory/regen_memory_golden.py` 并说明")


def test_retrieve_is_reproducible_in_second_store(tmp_path: Path, case) -> None:
    second = build(tmp_path / "again")
    assert second.retrieve["text"] == case.retrieve["text"]
    assert second.retrieve["scores"] == case.retrieve["scores"]
    assert second.snapshot_sha256 == case.snapshot_sha256


def test_memory_v0_sha256_matches_golden(case) -> None:
    expected = SHA256_GOLDEN.read_text("utf-8").strip()
    assert case.snapshot_sha256 == expected, (
        "快照字节变化：确认是否有意为之；有意则运行 regen 脚本并说明")


def test_golden_store_can_be_reloaded(tmp_path: Path, case) -> None:
    """重开 store：条目与索引一致，且再次 retrieve 与 golden 相同。"""
    expected = json.loads(RETRIEVE_GOLDEN.read_text("utf-8"))
    reopened = MemoryStore(tmp_path, case.store.cfg, config_dir=CONFIG_DIR,
                           run_id=case.store.run_id)
    assert len(reopened.entries()) == len(case.store.entries())
    result = reopened.retrieve(OBS, top_k=8, token_budget=900, tick=12)
    assert golden_view(result, memory_version=reopened.memory_version) == expected


def test_golden_has_no_heldout_tokens(case) -> None:
    """G4：golden 文件本身不得包含 heldout/test seed（2000–2099 / 9000–9099）。"""
    raw = RETRIEVE_GOLDEN.read_text("utf-8")
    for token in ("2000", "2011", "2039", "9000", "9001", "2040"):
        assert token not in raw
    assert case.retrieve["text"]
