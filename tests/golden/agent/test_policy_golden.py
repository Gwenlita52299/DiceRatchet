"""MOD-04 golden（G2/NFR-04-3）：固定输入的 prompt 正文与 ``prompt_hash``。

golden 由 ``regen_policy_golden.py`` 显式生成；本文件只读比对。改动模板或组装逻辑
必须显式重跑 regen 并在 PR 说明。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from policy_case import (HERE, golden_context, golden_filename, golden_spec)  # noqa: E402

from dice_ratchet.agent.prompts.assembler import assemble_prompt, sha256_of

METADATA = HERE / "policy_prompt.json"


def _metadata() -> dict:
    return json.loads(METADATA.read_text(encoding="utf-8"))


def test_prompt_hash_matches_golden():
    bundle = assemble_prompt(golden_spec(), golden_context())
    meta = _metadata()
    assert bundle.prompt_hash == meta["prompt_hash"], (
        "prompt 组装产物变化：确认是否有意为之；有意则运行 "
        "`.venv/bin/python tests/golden/agent/regen_policy_golden.py` 并说明")
    assert bundle.template_hash == meta["template_hash"]
    assert bundle.token_estimate == meta["token_estimate"]
    assert list(bundle.memory_ids) == meta["memory_ids"]


def test_prompt_text_matches_golden_file():
    bundle = assemble_prompt(golden_spec(), golden_context())
    target = HERE / golden_filename(bundle.prompt_hash)
    assert target.is_file(), f"golden 快照缺失：{target.name}（运行 regen 脚本）"
    assert target.read_text(encoding="utf-8") == bundle.text
    assert _metadata()["file"] == target.name


def test_golden_hash_is_self_consistent():
    meta = _metadata()
    target = HERE / meta["file"]
    text = target.read_text(encoding="utf-8")
    assert sha256_of(text) == meta["prompt_hash"]


def test_assembly_is_byte_deterministic():
    first = assemble_prompt(golden_spec(), golden_context())
    second = assemble_prompt(golden_spec(), golden_context())
    assert first.text == second.text
    assert first.prompt_hash == second.prompt_hash


def test_changing_observation_changes_hash():
    from dataclasses import replace

    ctx = golden_context()
    other = replace(ctx, observation=ctx.observation + "\nmutated")
    assert assemble_prompt(golden_spec(), other).prompt_hash != \
        assemble_prompt(golden_spec(), ctx).prompt_hash


@pytest.mark.parametrize("name", ["policy_prompt.json"])
def test_golden_files_are_tracked(name):
    assert (HERE / name).is_file()
    assert list(Path(HERE).glob("policy_prompt_*.txt")), "缺少 prompt 正文快照"
