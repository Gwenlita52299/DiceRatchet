"""FR-07-1 / FR-07-3 / NFR-07-1：prompt 渲染、propose、确定性与去重（PRD §11 单测 4）。

「给定-当-则」逐条对应 PRD §11-4：

- 给定 ``render_proposer_prompt`` 产出，当扫描，则占位符 ⊆
  ``{observation, memory, harness, task}`` 且不含 ``heldout`` / ``test`` 子串（INV-3 回归）；
- 给定同一 ``(run_id, gen, seed)`` 与固定 stub LLM，当两次 ``propose``，
  则 ``candidate_id`` 列表逐字节相同（NFR-07-1）。
"""

from __future__ import annotations

import pytest

from dice_ratchet.evolve import proposer as P
from dice_ratchet.evolve.errors import HoldoutLeakError, ProposeFailedError
from dice_ratchet.evolve.schemas import EvidenceBundle, HarnessSnapshot
from dice_ratchet.infra.errors import E

from evolve_support import (StubLLM, make_candidate, make_config, prompt_diff,
                            proposal_payload, tool_diff)


def _bundle(*, traj=(), memory=(), reflect=()) -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="ev-test", memory_items=tuple(memory),
        trajectory_summaries=tuple(traj), reflections=tuple(reflect),
        harness_snapshot=HarnessSnapshot(version=0), source_tags=("harness:current",))


# =====================================================================
# prompt 渲染（INV-3 回归）
# =====================================================================

def test_prompt_slots_are_whitelisted():
    """模板产出的槽位 ⊆ ``PROMPT_PLACEHOLDERS``，且不留未替换的 ``{{...}}``。"""
    system, user = P.render_proposer_prompt(_bundle(), make_config())
    text = system + "\n" + user
    assert "{{" not in text and "}}" not in text
    # 四个槽位各对应一个小节/块（observation / memory / harness / task）
    assert "## observation" in user
    assert "## memory" in user
    assert "## harness" in user
    assert "## 任务" in user
    assert set(P.PROMPT_PLACEHOLDERS) == {"observation", "memory", "harness", "task"}


def test_prompt_has_no_forbidden_substrings():
    system, user = P.render_proposer_prompt(_bundle(), make_config())
    joined = (system + "\n" + user).lower()
    for word in ("heldout", "seeds.yaml", "test_full"):
        assert word not in joined, word
    for label in ("H1", "H2", "H3"):
        assert f" {label.lower()} " not in joined


def test_prompt_guard_catches_a_bad_template():
    with pytest.raises(HoldoutLeakError):
        P.assert_prompt_is_clean("请参考 heldout 数据")


def test_prompt_includes_evidence_and_constraints():
    bundle = _bundle(traj=[{"split": "train", "episode_key": "e-001000-g001-train",
                            "stages_cleared": 2}],
                     memory=[{"split": "train", "text": "优先清护卫"}],
                     reflect=[{"split": "train", "lesson": "别忽略反击"}])
    system, user = P.render_proposer_prompt(bundle, make_config())
    assert "e-001000-g001-train" in user
    assert "优先清护卫" in user
    assert "别忽略反击" in user
    assert "harness/prompts/decision.md" in system
    assert str(make_config().max_diff_lines) in system


def test_output_schema_envelope():
    schema = P.candidate_schema_dict()
    assert schema["required"] == ["candidates"]
    item = schema["properties"]["candidates"]["items"]
    assert set(item["required"]) == {"kind", "diff", "rationale", "expected_effect"}


# =====================================================================
# propose：正常路径
# =====================================================================

def _two_candidate_llm() -> StubLLM:
    return StubLLM([{"candidates": [
        proposal_payload(kind="prompt_patch", diff=prompt_diff(added="提示 A"),
                         rationale="依据 traj:train#0", effect=("win_rate", "up", 2)),
        proposal_payload(kind="prompt_patch",
                         diff=prompt_diff(path="harness/prompts/reflection.md",
                                          added="提示 B"),
                         rationale="依据 reflect:train#0", effect=("win_rate", "up", 1)),
    ]}])


def test_propose_returns_validated_candidates():
    result = P.propose(_bundle(), cfg=make_config(n_candidates=2), rng_seed=7,
                       client=_two_candidate_llm())
    assert result.ok
    assert len(result.candidates) == 2
    assert [c.expected_effect.priority for c in result.candidates] == [2, 1]
    assert result.error_code is None


def test_propose_truncates_to_n_candidates():
    result = P.propose(_bundle(), cfg=make_config(n_candidates=1), rng_seed=7,
                       client=_two_candidate_llm())
    assert len(result.candidates) == 1
    assert result.n_generated == 2


def test_propose_drops_invalid_candidates():
    llm = StubLLM([{"candidates": [
        proposal_payload(kind="prompt_patch", diff=prompt_diff(added="合法"),
                         rationale="依据 traj:train#0"),
        proposal_payload(kind="prompt_patch", diff=prompt_diff(path="harness/loop.py"),
                         rationale="依据 traj:train#1"),
    ]}])
    result = P.propose(_bundle(), cfg=make_config(n_candidates=2), rng_seed=7, client=llm)
    assert len(result.candidates) == 1


def test_propose_dedupes_identical_diffs():
    same = prompt_diff(added="同一处改动")
    llm = StubLLM([{"candidates": [
        proposal_payload(diff=same, rationale="理由一"),
        proposal_payload(diff=same + "\n", rationale="理由二"),
    ]}])
    result = P.propose(_bundle(), cfg=make_config(n_candidates=2), rng_seed=7, client=llm)
    assert len(result.candidates) == 1
    assert result.n_deduped == 1


# =====================================================================
# propose：显式失败（绝不静默退化）
# =====================================================================

def test_propose_without_client_returns_explicit_error(monkeypatch):
    """没有客户端（也没注册 provider）时必须**显式**失败，不得静默退化。"""
    monkeypatch.setattr(P, "_CLIENT_PROVIDER", None)
    result = P.propose(_bundle(), cfg=make_config(), rng_seed=7, client=None)
    assert result.candidates == ()
    assert result.error_code == str(E.E_EVOLVE_PROPOSE_FAILED)
    assert result.reason == "no_client"


def test_propose_empty_candidates_returns_no_candidate_code():
    result = P.propose(_bundle(), cfg=make_config(), rng_seed=7,
                       client=StubLLM([{"candidates": []}]))
    assert result.candidates == ()
    assert result.error_code == str(E.E_EVOLVE_NO_CANDIDATE)
    assert result.reason == "no_candidate"


def test_propose_all_invalid_returns_no_candidate_code():
    llm = StubLLM([{"candidates": [proposal_payload(diff=prompt_diff(path="harness/x.py"))]},
                   {"candidates": [proposal_payload(diff=prompt_diff(path="harness/x.py"))]},
                   {"candidates": [proposal_payload(diff=prompt_diff(path="harness/x.py"))]}])
    result = P.propose(_bundle(), cfg=make_config(propose_max_retries=2), rng_seed=7,
                       client=llm)
    assert result.error_code == str(E.E_EVOLVE_NO_CANDIDATE)


def test_propose_retries_then_succeeds():
    llm = StubLLM([RuntimeError("boom"), _two_candidate_llm().payloads[0]])
    result = P.propose(_bundle(), cfg=make_config(n_candidates=2, propose_max_retries=2),
                       rng_seed=7, client=llm)
    assert result.ok
    assert len(llm.calls) == 2


def test_propose_exhausted_retries_returns_error():
    llm = StubLLM([RuntimeError("a"), RuntimeError("b")])
    result = P.propose(_bundle(), cfg=make_config(propose_max_retries=1), rng_seed=7,
                       client=llm)
    assert result.error_code == str(E.E_EVOLVE_PROPOSE_FAILED)
    assert "RuntimeError" in result.reason


def test_propose_malformed_payload_returns_error():
    result = P.propose(_bundle(), cfg=make_config(), rng_seed=7,
                       client=StubLLM(["not json at all"]))
    assert result.error_code == str(E.E_EVOLVE_PROPOSE_FAILED)


@pytest.mark.parametrize("payload", [
    {"candidates": [{"kind": "prompt_patch"}]}, # 缺 diff/rationale/effect
    {"candidates": ["不是对象"]},
    {"nothing": 1},
])
def test_parse_proposal_payload_rejects_bad_shapes(payload):
    if payload == {"candidates": [{"kind": "prompt_patch"}]}:
        # 形状可解析但字段缺失 → 归一化阶段报错
        with pytest.raises((ProposeFailedError, Exception)):
            raw = P.parse_proposal_payload(payload)
            [P.normalize_generated_candidate(item, gen_id="g001") for item in raw]
    else:
        with pytest.raises(ProposeFailedError):
            P.parse_proposal_payload(payload)


def test_propose_propagates_holdout_leak():
    """INV-5 是致命错误：``propose`` 不吞它，交由上层中止本代（PRD §10）。"""
    bundle = _bundle(reflect=[{"split": "train", "lesson": "2013 最好"}])
    with pytest.raises(HoldoutLeakError):
        P.propose(bundle, cfg=make_config(), rng_seed=7, client=_two_candidate_llm())


# =====================================================================
# NFR-07-1：确定性
# =====================================================================

def test_same_seed_gives_identical_candidate_ids():
    kwargs = dict(cfg=make_config(n_candidates=2), rng_seed=12345)
    first = P.propose(_bundle(), client=_two_candidate_llm(), **kwargs)
    second = P.propose(_bundle(), client=_two_candidate_llm(), **kwargs)
    assert [c.candidate_id for c in first.candidates] == \
        [c.candidate_id for c in second.candidates]


def test_llm_receives_deterministic_seed():
    llm = _two_candidate_llm()
    P.propose(_bundle(), cfg=make_config(), rng_seed=99, client=llm)
    assert llm.calls[0]["kwargs"]["seed"] == 99
    assert llm.calls[0]["kwargs"]["temperature"] == 0.0


def test_attempt_seed_is_deterministic_and_changes():
    assert P._attempt_seed(7, 0) == 7
    assert P._attempt_seed(7, 1) == P._attempt_seed(7, 1)
    assert P._attempt_seed(7, 1) != P._attempt_seed(7, 2)


def test_propose_seed_derivation_is_stable():
    from dice_ratchet.evolve.generation import propose_seed

    a = propose_seed("run-1", 3)
    b = propose_seed("run-1", 3)
    c = propose_seed("run-1", 4)
    assert a == b != c
    assert propose_seed("run-1", 3, seed=42) == 42


# =====================================================================
# 候选归一化
# =====================================================================

def test_normalize_derives_touched_files_from_diff():
    cand = P.normalize_generated_candidate(
        proposal_payload(diff=prompt_diff(), touched=[]), gen_id="g002")
    assert cand.touched_files == ("harness/prompts/decision.md",)
    assert cand.gen_id == "g002"


def test_normalize_rejects_bad_effect():
    payload = proposal_payload()
    payload["expected_effect"] = ["unknown_metric", "up", 9]
    with pytest.raises(Exception):
        P.normalize_generated_candidate(payload, gen_id="g001")


def test_tool_candidate_normalizes_paths():
    cand = make_candidate(kind="tool_patch",
                          diff=tool_diff(added=["# 微调打分"]),
                          touched=["harness/tools/score.py"])
    assert cand.touched_files == ("harness/tools/score.py",)
