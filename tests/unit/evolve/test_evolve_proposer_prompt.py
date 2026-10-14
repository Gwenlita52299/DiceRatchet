"""FR-07-1 / FR-07-3 / NFR-07-1：prompt 渲染、propose、确定性与去重（PRD §11 单测 4）。

「给定-当-则」逐条对应 PRD §11-4：

- 给定 ``render_proposer_prompt`` 产出，当扫描，则占位符 ⊆
  ``{observation, memory, harness, task}`` 且不含 ``heldout`` / ``test`` 子串（INV-3 回归）；
- 给定同一 ``(run_id, gen, seed)`` 与固定 stub LLM，当两次 ``propose``，
  则 ``candidate_id`` 列表逐字节相同（NFR-07-1）。
"""

from __future__ import annotations

from typing import Any

import pytest

from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol
_HELDOUT_SEED = validate_protocol(coerce_protocol(None)).splits["heldout"].baskets["H1"].start + 13

from dice_ratchet.evolve import proposer as P
from dice_ratchet.evolve.errors import HoldoutLeakError, ProposeFailedError
from dice_ratchet.evolve.schemas import EvidenceBundle, HarnessSnapshot
from dice_ratchet.infra.errors import E

from evolve_support import (StubLLM, make_app_config, make_candidate, make_config,
                            prompt_diff, proposal_payload, tool_diff)


def _bundle(*, traj=(), memory=(), reflect=()) -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="ev-test", memory_items=tuple(memory),
        trajectory_summaries=tuple(traj), reflections=tuple(reflect),
        harness_snapshot=HarnessSnapshot(version=0), source_tags=("harness:current",))


# =====================================================================
# prompt 渲染（INV-3 回归）
# =====================================================================

def test_prompt_slots_are_whitelisted():
    """模板产出的槽位 ⊆ ``PROMPT_PLACEHOLDERS``，且不留未替换的 ``{{...}}``。

    注：本 fixture 的 ``HarnessSnapshot`` 无 ``contents``；带正文时正文块内的 ``{{...}}``
    是**目标文件自己的**模板，属 F38 的例外（见下一条用例）。
    """
    system, user = P.render_proposer_prompt(_bundle(), make_config())
    text = system + "\n" + user
    assert "{{" not in text and "}}" not in text
    # 四个槽位各对应一个小节/块（observation / memory / harness / task）
    assert "## observation" in user
    assert "## memory" in user
    assert "## harness" in user
    assert "## 任务" in user
    assert set(P.PROMPT_PLACEHOLDERS) == {"observation", "memory", "harness", "task"}


def test_f38_harness_body_is_inlined_and_scopes_placeholder_rule(tmp_path):
    """F38：可改文件的**正文**必须逐字符进 prompt（否则 diff 的 hunk 无法对齐）。

    实测现场：只给 sha256 时模型产出的 diff 被打回
    ``E_EVOLVE_PATCH_INVALID: hunk 上下文不匹配（decision.md 第 1 行）``。
    同时界定范围：``decision.md`` 自己是**给 agent 的模板**（含 ``{{observation}}`` 等），
    正文块内的占位符**不**算 proposer 模板的未替换槽位。
    """
    from dice_ratchet.evolve.schemas import EvidenceBundle

    harness = tmp_path / "harness"
    (harness / "prompts").mkdir(parents=True)
    body = "# 决策模板\n\n## 当前观测\n{{observation}}\n"
    (harness / "prompts" / "decision.md").write_text(body, encoding="utf-8")

    snap = P.harness_snapshot(harness)
    assert ("prompts/decision.md", body) in snap.contents, "可改文件的正文必须进快照"
    assert "version.json" not in [name for name, _ in snap.contents]

    bundle = EvidenceBundle(bundle_id="ev-f38", memory_items=(),
                            trajectory_summaries=(), reflections=(),
                            harness_snapshot=snap, source_tags=("harness:current",))
    _system, user = P.render_proposer_prompt(bundle, make_config())

    assert body.rstrip("\n") in user, "F38：正文必须逐字符可见，模型才能写出对齐的 hunk"
    # 正文块之外的区域仍不得残留未替换槽位
    head = user.split("### 可修改文件的当前正文")[0]
    assert "{{" not in head and "}}" not in head


def test_f40_prompt_states_the_rules_the_validator_enforces():
    """F40：prompt 必须给出**校验器实际强制**的规则，否则候选只能盲猜。

    实测（2026-10-09 干跑）：`config_patch` 因
    ``config_key_not_evolvable:['enabled','min_decisions','reflection']`` 作废——
    `evolvable_keys` 从未出现在 prompt 里；另一候选因 rationale 322 字符越过 300 硬上限作废。
    """
    from dice_ratchet.evolve.schemas import CANDIDATE_SCHEMA

    cfg = make_config(evolvable_keys=("retrieval.top_k", "sampling.temperature"))
    system, _user = P.render_proposer_prompt(_bundle(), cfg)

    for key in ("retrieval.top_k", "sampling.temperature"):
        assert key in system, f"prompt 必须列出可改的 config 键：{key}"
    assert str(P._RATIONALE_MAX_CHARS) in system, "prompt 必须给出 rationale 硬上限"
    assert str(P._RATIONALE_GUIDE_CHARS) in system, "prompt 必须给出留余量的建议值"
    # 同一上限只许有一个事实来源：校验器直接取候选 JSON Schema 的 maxLength
    assert P._RATIONALE_MAX_CHARS == CANDIDATE_SCHEMA["properties"]["rationale"]["maxLength"]



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


# =====================================================================
# F36：回退解析客户端时，provider 收到的是 AppConfig 而不是 EvolveConfig 段
# =====================================================================

class _RoleProviderStub:
    """``app._role_provider`` 的最小替身：**必须**读完整 ``AppConfig`` 的 ``inference``。"""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def __call__(self, cfg, **kwargs):
        self.seen.append(cfg)
        if not hasattr(cfg, "inference"): # 真实 provider 在这里 `cfg.inference` → AttributeError
            raise AttributeError("'EvolveConfig' object has no attribute 'inference'")
        return StubLLM([{"candidates": [proposal_payload()]}])

    def client_for(self, role): # RoleModelProvider 的形状
        return self


def test_f36_fallback_provider_receives_app_config(monkeypatch):
    """F36：``client=None`` 的回退路径上，provider 必须拿到完整 ``AppConfig``。

    实测现场（2026-10-09）：``loop run --agent heuristic`` + 已注册 provider →
    ``AttributeError: 'EvolveConfig' object has no attribute 'inference'``。原因是
    ``propose(cfg=…)`` 的 ``cfg`` 是 **``EvolveConfig`` 段**（渲染/重试/护栏都要用它），
    早先被直接传给了需要完整 ``AppConfig`` 的 provider。真实 LLM 臂因 ``client`` 非空
    而短路，所以这个缺陷一直藏着。
    """
    provider = _RoleProviderStub()
    monkeypatch.setattr(P, "_CLIENT_PROVIDER", provider)
    app_cfg = make_app_config()
    evolve_cfg = make_config(n_candidates=1)

    result = P.propose(_bundle(), cfg=evolve_cfg, rng_seed=7, client=None,
                       run_id="r-1", generation=1, app_config=app_cfg)

    assert provider.seen and provider.seen[0] is app_cfg, \
        "provider 必须收到 AppConfig（F36：传 EvolveConfig 段会 AttributeError）"
    assert result.ok, f"回退路径应当能产出候选：{result.error_code} / {result.reason}"


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
    bundle = _bundle(reflect=[{"split": "train", "lesson": f"{_HELDOUT_SEED} 最好"}])
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
    # Issue #1：提案走 MOD-03 统一同步契约（mode/sampling/seed），温度在 sampling 里
    assert llm.calls[0]["kwargs"]["mode"] == "eval"
    assert llm.calls[0]["kwargs"]["sampling"]["temperature"] == 0.0


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


# =====================================================================
# D1=A（2026-10-14）：死路记录进提案 prompt（**只读**）
# =====================================================================

def _dead_ends_bundle(harness) -> EvidenceBundle:
    return EvidenceBundle(bundle_id="ev-dead", memory_items=(),
                          trajectory_summaries=(), reflections=(),
                          harness_snapshot=P.harness_snapshot(harness),
                          source_tags=("harness:current",))


def test_d1a_dead_ends_are_inlined_into_proposer_prompt(tmp_path):
    """死路记录必须**真的**出现在提案 prompt 里，并标注为只读。

    为什么需要（现场）：m3-formal 三代的候选补丁高度重合——证据束里只有
    memory + train 轨迹 + 反思，**没有拒绝历史**，模型在同一片信息上重复提出同一个
    已被否决的改动。本用例把「提案方看得到」钉在渲染产物上。
    """
    from dice_ratchet.evolve.versioning import write_dead_end
    from dice_ratchet.evolve.schemas import Candidate

    harness = tmp_path / "harness"
    (harness / "prompts").mkdir(parents=True)
    (harness / "prompts" / "decision.md").write_text("# 决策模板\n", encoding="utf-8")
    write_dead_end(
        harness, generation=1,
        candidate=Candidate.create(
            kind="prompt_patch",
            diff="--- a/harness/prompts/decision.md\n+++ b/harness/prompts/decision.md\n"
                 "@@ -1 +1 @@\n-# 决策模板\n+# 决策模板（加约束）\n",
            rationale="在输出契约里加一条「先清护卫」", expected_effect=("win_rate", "up", 2),
            touched_files=["harness/prompts/decision.md"]),
        reason="C2:no_significant_gain", error_code="E_EVOLVE_NO_GAIN",
        metrics={"paired": {"metric": "win_rate", "delta": 0.025, "p": 1.0},
                 "heldout": {"win_rate": 0.125, "mean_stages_cleared": 4.45}})

    snap = P.harness_snapshot(harness)
    assert [name for name, _ in snap.context] == ["dead_ends.md"], "死路记录必须是只读上下文"
    assert "dead_ends.md" not in [name for name, _ in snap.contents], "不得混进可改文件"

    _system, user = P.render_proposer_prompt(_dead_ends_bundle(harness), make_config())
    assert "## dead_ends" in user and "只读" in user
    assert "gen-001" in user and "C2:no_significant_gain" in user
    assert "先清护卫" in user, "提案理由（下一次提案要避开的东西）必须可见"
    # system 里必须写明「不得重复 dead_ends 里的改动」这条硬约束
    system, _user = P.render_proposer_prompt(_dead_ends_bundle(harness), make_config())
    assert "dead_ends" in system


def test_d1a_dead_ends_free_text_is_sanitized_before_reaching_the_prompt(tmp_path):
    """死路记录里的**自由文本**必须脱敏后才进 prompt（否则整代以 E_EVAL_INV5_LEAK 中止）。

    现场纪律：``assert_no_holdout_leak`` / ``assert_prompt_is_clean`` 对**全部**渲染文本做
    INV-3/INV-5 禁用词扫描，命中即中止本代（PRD §10）。而死路记录里的 ``rationale`` 与判定
    ``reason`` 都是自由文本 ⇒ 唯一的预防点就在渲染处。落盘原文保持可审计。
    """
    from dice_ratchet.evolve.versioning import write_dead_end
    from dice_ratchet.evolve.schemas import Candidate

    harness = tmp_path / "harness"
    (harness / "prompts").mkdir(parents=True)
    (harness / "prompts" / "decision.md").write_text("# 决策模板\n", encoding="utf-8")
    write_dead_end(
        harness, generation=1,
        candidate=Candidate.create(
            kind="prompt_patch", diff="", rationale="试试 heldout 上的 H1 篮子表现",
            expected_effect=("win_rate", "up", 1),
            touched_files=["harness/prompts/decision.md"]),
        reason="C2", error_code="E_EVOLVE_NO_GAIN")

    bundle = _dead_ends_bundle(harness)
    # 证据束的 INV-5 扫描也必须过（它扫的是同一份 context 文本）
    P.assert_no_holdout_leak(bundle, cfg=make_config())
    system, user = P.render_proposer_prompt(bundle, make_config())
    lowered = (system + "\n" + user).lower()
    for word in ("heldout", "seeds.yaml", "test_full"):
        assert word not in lowered, f"禁用词 {word!r} 泄漏进 prompt"
    assert "[已屏蔽]" in user, "脱敏必须留下痕迹（读者能看出这里被替换过）"
    # 落盘原文不脱敏：审计要看到模型当时写了什么
    assert "heldout" in (harness / "dead_ends.md").read_text(encoding="utf-8")


def test_d1a_dead_ends_cannot_be_modified_by_a_candidate(tmp_path):
    """候选**不得改写** ``harness/dead_ends.md``：否则可以抹掉自己的拒绝记录再提同一份补丁。"""
    from dice_ratchet.evolve.schemas import Candidate

    cand = Candidate.create(
        kind="prompt_patch",
        diff="--- a/harness/dead_ends.md\n+++ b/harness/dead_ends.md\n"
             "@@ -1 +1 @@\n-## gen-001\n+（清空）\n",
        rationale="引用 harness:current#0", expected_effect=("win_rate", "up", 1),
        touched_files=["harness/dead_ends.md"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert not result.ok
    assert any("forbidden_path" in v for v in result.violations), result.violations
    assert not P._is_allowed_path("harness/dead_ends.md")
