"""MOD-04 prompt 组装单测（PRD §11 U1/U2 + FR-04-1..4）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dice_ratchet.agent import EpisodeContext, PolicyAgent
from dice_ratchet.agent.prompts.assembler import (DEFAULT_TEMPLATE_PATH, FIXED_TEMPLATE_PATH,
                                               PromptContext, PromptTemplateError,
                                               ReflectionItem, check_placeholders,
                                               load_template)
from dice_ratchet.agent.prompts.assembler import MemoryItem, assemble_prompt, sha256_of
from dice_ratchet.infra.errors import E, InfraError


def _ctx(**overrides):
    base = dict(
        observation="phase=player_turn stage=1/6 decision=3\nplayer: hearts=2/2",
        task="TASK-TEXT",
        harness_version=7,
        tools="- rank_actions(obs, candidates, memory) -> list[dict]",
        legal_actions='{"kind":"end_turn"}',
        reflections=(ReflectionItem("r-2", "newest", 4), ReflectionItem("r-1", "older", 2)),
        memory=(MemoryItem("m-b", "second", 0.4), MemoryItem("m-a", "first", 0.9)),
        obs_digest="sha256:" + "a" * 64,
        token_budget=3072,
        reflection_max=3,
        memory_top_k=5,
    )
    base.update(overrides)
    return PromptContext(**base)


def test_u1_assemble_prompt_hash_and_no_truncation():
    """U1：合法模板 + obs + memory → 无裁剪，hash 与正文自洽。"""
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    bundle = assemble_prompt(spec, _ctx())
    assert bundle.truncated == ()
    assert bundle.unknown_placeholders == ()
    assert bundle.error_codes == ()
    assert bundle.fallback_used is False
    assert bundle.prompt_hash == sha256_of(bundle.text)
    assert bundle.prompt_hash.startswith("sha256:")
    assert len(bundle.prompt_hash) == len("sha256:") + 64
    assert bundle.memory_ids == ("m-a", "m-b") # (score desc, id asc)
    assert "TASK-TEXT" in bundle.text
    assert "m-a" in bundle.text and "newest" in bundle.text
    assert bundle.token_estimate > 0


def test_u1_prompt_hash_is_deterministic_and_order_insensitive():
    """FR-04-4：同 obs + harness_version + sorted(memory_ids) + mode → 同 hash。"""
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    first = assemble_prompt(spec, _ctx())
    second = assemble_prompt(spec, _ctx())
    assert first.prompt_hash == second.prompt_hash

    shuffled = _ctx(memory=(MemoryItem("m-a", "first", 0.9), MemoryItem("m-b", "second", 0.4)),
                    reflections=(ReflectionItem("r-1", "older", 2),
                                 ReflectionItem("r-2", "newest", 4)))
    third = assemble_prompt(spec, shuffled)
    assert third.prompt_hash == first.prompt_hash


def test_u2_budget_trimming_priority_and_records():
    """U2：observation 段完整；反思留最新；memory 先丢最低分；每次丢弃有记录。"""
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx(
        observation="OBS-KEEP-" + "x" * 50,
        memory=tuple(MemoryItem(f"m-{i}", "y" * 40, score=i / 10) for i in range(1, 6)),
        reflections=tuple(ReflectionItem(f"r-{g}", "z" * 40, g) for g in range(5)),
        memory_top_k=3,
        reflection_max=2,
        token_budget=100_000,
    )
    bundle = assemble_prompt(spec, ctx)
    assert "OBS-KEEP-" in bundle.text
    assert bundle.memory_ids == ("m-5", "m-4", "m-3")
    kept_reflections = [r for r in ("r-4", "r-3") if r in bundle.text]
    assert kept_reflections == ["r-4", "r-3"]
    assert "r-0" not in bundle.text and "r-1" not in bundle.text
    reasons = {(t.kind, t.reason) for t in bundle.truncated}
    assert ("memory", "top_k") in reasons
    assert ("reflection", "reflection_max") in reasons


def test_fr_04_3_over_budget_drops_memory_then_reflections():
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx(
        memory=tuple(MemoryItem(f"m-{i}", "y" * 300, score=i / 10) for i in range(1, 6)),
        reflections=tuple(ReflectionItem(f"r-{g}", "z" * 300, g) for g in range(4)),
        token_budget=900,
        memory_top_k=5,
        reflection_max=4,
    )
    bundle = assemble_prompt(spec, ctx)
    assert len(bundle.memory_ids) < 5
    assert any(t.reason == "budget" for t in bundle.truncated)
    assert bundle.token_estimate <= 900 or E.E_ACTION_BUDGET in bundle.error_codes


def test_fr_04_3_observation_alone_over_budget_emits_budget_code():
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx(observation="X" * 5000, memory=(), reflections=(), token_budget=256)
    bundle = assemble_prompt(spec, ctx)
    assert E.E_ACTION_BUDGET in bundle.error_codes
    assert bundle.token_estimate > 256


def test_fr_04_1_missing_template_falls_back(tmp_path):
    """FR-04-1 / U3：模板缺失 → E_ACTION_TEMPLATE + 兜底模板可用。"""
    spec = load_template(tmp_path / "does-not-exist.md")
    assert spec.fallback_used is True
    assert E.E_ACTION_TEMPLATE in spec.error_codes
    assert spec.has("observation") and spec.has("memory")
    bundle = assemble_prompt(spec, _ctx())
    assert E.E_ACTION_TEMPLATE in bundle.error_codes
    assert bundle.fallback_used is True
    assert "phase=player_turn" in bundle.text


def test_fr_04_1_missing_required_placeholder_falls_back(tmp_path):
    path = tmp_path / "tpl.md"
    path.write_text("只有 {{observation}}，没有 memory 占位符\n", encoding="utf-8")
    spec = load_template(path)
    assert spec.fallback_used is True
    assert E.E_ACTION_TEMPLATE in spec.error_codes
    assert spec.path == str(DEFAULT_TEMPLATE_PATH)


def test_check_placeholders_raises_template_error():
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    check_placeholders(spec) # 不抛
    broken = type(spec)(path="x", text="{{observation}}", template_hash=sha256_of("x"),
                        placeholders=("observation",))
    with pytest.raises(PromptTemplateError) as excinfo:
        check_placeholders(broken)
    assert str(excinfo.value.code) == E.E_ACTION_TEMPLATE


def test_fr_04_1_undecodable_template_falls_back(tmp_path):
    path = tmp_path / "bad.md"
    path.write_bytes(b"\xff\xfe\x00\x00{{observation}} {{memory}}")
    spec = load_template(path)
    assert spec.fallback_used is True
    assert E.E_ACTION_TEMPLATE in spec.error_codes


def test_fr_04_2_single_pass_and_unknown_placeholders(tmp_path):
    """FR-04-2：单遍替换；未知占位符原样保留并登记。"""
    path = tmp_path / "tpl.md"
    path.write_text("O={{observation}}\nM={{memory}}\nU={{custom}}\n", encoding="utf-8")
    spec = load_template(path)
    assert spec.unknown_placeholders == ("custom",)
    bundle = assemble_prompt(spec, _ctx(observation="has {{memory}} inside"))
    # 单遍：observation 值里的 {{memory}} 不得被二次替换
    assert "has {{memory}} inside" in bundle.text
    assert "{{custom}}" in bundle.text
    assert bundle.unknown_placeholders == ("custom",)


def test_fr_04_2_all_known_placeholders_replaced():
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx(constraints="- error_code: E_ACTION_PARSE", legal_actions='{"kind":"end_turn"}')
    bundle = assemble_prompt(spec, ctx)
    for token in ("{{task}}", "{{harness_version}}", "{{tools}}", "{{observation}}",
                  "{{reflections}}", "{{memory}}", "{{constraints}}"):
        assert token not in bundle.text
    assert "E_ACTION_PARSE" in bundle.text


def test_fr_04_5_bundle_records_source_ids():
    """INV-3：进入 prompt 的四类来源都被显式登记。"""
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    bundle = assemble_prompt(spec, _ctx())
    joined = "\n".join(bundle.source_ids)
    assert "obs:" in joined
    assert "template:" in joined
    assert "task:" in joined
    assert "harness:7" in joined
    assert "memory:m-a" in joined
    assert "reflection:r-2" in joined


def test_baseline_fixed_prompt_uses_frozen_template(harness_dir):
    from dice_ratchet.infra.config import PolicyConfig

    agent = PolicyAgent(PolicyConfig(mode="baseline_fixed_prompt"), harness_dir=harness_dir)
    spec = agent.template_spec
    assert Path(spec.path) == FIXED_TEMPLATE_PATH
    assert spec.fallback_used is False


def test_harness_template_assembles_with_legal_actions(harness_dir):
    from dice_ratchet.infra.config import PolicyConfig

    agent = PolicyAgent(PolicyConfig(), harness_dir=harness_dir)
    spec = agent.template_spec
    assert Path(spec.path) == harness_dir / "prompts" / "decision.md"
    assert spec.unknown_placeholders == ()
    assert not spec.error_codes


def test_agent_emits_template_error_code_on_fallback(tmp_path, cfg):
    from dice_ratchet.infra.config import PolicyConfig

    bad = PolicyConfig(template_path=str(tmp_path / "missing.md"))
    agent = PolicyAgent(bad, harness_dir=tmp_path)
    obs = {"phase": "map", "decisionIndex": 0, "player": {}, "hand": [], "slots": [],
           "enemies": [], "legalActionKinds": ["map_choose"], "map": {"rows": []},
           "stage": 1, "stageCount": 6, "chapter": 1, "turn": 0, "difficulty": "hard",
           "schemaVersion": "obs-agent-1.0.0", "episodeId": "e", "toggles": {}, "relics": []}
    bundle = agent.prompt_bundle(obs, ctx=EpisodeContext(seed=1))
    assert E.E_ACTION_TEMPLATE in bundle.error_codes


def test_infra_error_code_is_registered_for_template():
    assert str(E.E_ACTION_TEMPLATE) == "E_ACTION_TEMPLATE"
    assert issubclass(PromptTemplateError, InfraError)
