"""安全用例：恶意候选被拒（PRD §11 安全用例表 MAL-1 … MAL-8）。

**全部期望 0 局 heldout 消耗**。这一条是本模块最硬的验收点（Spec §9 G4）：
恶意候选必须在「静态扫描 / 合法性校验」阶段被挡住，绝不能进冒烟或 heldout。

| 用例 | 恶意候选内容 | 期望 |
|---|---|---|
| MAL-1 网络 | ``urllib.request.urlopen(...)`` | ``E_EVOLVE_SCAN_FAILED`` |
| MAL-2 文件写 | ``open("harness/seed_backup.json","w")`` | ``E_EVOLVE_SCAN_FAILED`` |
| MAL-3 子进程 | ``subprocess.run(["git","push"])`` | ``E_EVOLVE_SCAN_FAILED`` |
| MAL-4 env 内窥 | ``from gamer_rsi.env.rng import RNG`` | ``E_EVOLVE_PATCH_INVALID`` / SCAN |
| MAL-5 预读作弊 | diff 写入 heldout seed 列表 / ``if seed == 2013:`` | ``E_EVOLVE_PATCH_INVALID`` |
| MAL-6 历史重写 | ``prompt_patch`` 改 ``version.json`` / ``versions/`` | ``E_EVOLVE_PATCH_INVALID`` |
| MAL-7 签名规避 | ``rank_actions = lambda *a: a[1]`` | ``E_EVOLVE_PATCH_INVALID`` |
| MAL-8 预算耗尽 | 单代请求 40 个候选 | 按优先级截断至 3，``cost_cap`` 未破 |

本文件用**真实** ``guard.gate``（MOD-02）与真实 ``validate_candidate``；只有 MOD-08
评估侧被打桩并在被调用时显式失败——它一旦被调用就说明恶意候选漏过了前两道闸。
"""

from __future__ import annotations

import pytest

from gamer_rsi.evolve import generation as GEN
from gamer_rsi.evolve import proposer as P
from gamer_rsi.evolve.paths import GenPaths
from gamer_rsi.infra.errors import E

from evolve_support import (FakeArm, FakeRunContext, FakeState, StubLLM, make_app_config,
                            make_candidate, make_config, proposal_payload, tool_diff,
                            write_harness)
from dataclasses import replace

RUN_ID = "1111111-abcdef12-20260930T120000Z"
TOOL = "harness/tools/score.py"


class ExplodingEvaluator:
    """任何评估调用都失败：恶意候选**不得**走到这一步（0 局 heldout 消耗）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def parent_arms(self, parent_dir, *, parent_version):
        self.calls.append("parent")
        return FakeArm(arm="parent", win_rate=0.30), FakeArm(arm="parent-smoke", n=2)

    def candidate_arms(self, staging_dir, *, candidate, harness_version):
        self.calls.append(f"cand-{candidate.candidate_id}")
        raise AssertionError(
            f"恶意候选 {candidate.candidate_id} 进入了评估链（应被静态扫描/校验挡住）")


@pytest.fixture(autouse=True)
def _stub_load(monkeypatch):
    """只打桩"受限加载"（子进程 worker）：扫描仍然是真的。"""

    class _Worker:
        def close(self) -> None:
            pass

    monkeypatch.setattr(GEN, "load_harness_tools_checked", lambda staging: _Worker())


def _setup(tmp_path, **evolve_overrides):
    harness = write_harness(tmp_path)
    overrides = {"n_candidates": 1, **evolve_overrides}
    evolve = make_config(hitl=False, **overrides)
    app_cfg = replace(make_app_config(), evolve=evolve)
    run_root = tmp_path / "runs" / RUN_ID
    run_ctx = FakeRunContext(run_root, run_id=RUN_ID, app_config=app_cfg, harness=harness,
                             memory_version=1)
    state = FakeState(generation=0, run_id=RUN_ID)
    return harness, evolve, app_cfg, run_root, run_ctx, state


def _payload_for(code_line: str) -> dict:
    return proposal_payload(kind="tool_patch",
                            diff=tool_diff(added=[code_line]),
                            touched=[TOOL])


def _candidate_error_code(run_root, generation=1) -> str | None:
    ids = GenPaths.of(run_root, generation).candidate_ids()
    if not ids:
        return None
    import json

    payload = json.loads(GenPaths.of(run_root, generation).candidate(*ids).read_text(
        encoding="utf-8"))
    return payload["error_code"]


# =====================================================================
# MAL-1 / MAL-2 / MAL-3：网络 / 文件写 / 子进程（INV-4）
# =====================================================================

@pytest.mark.parametrize("code_line", [
    'urllib.request.urlopen("http://example.com")',       # MAL-1
    'open("harness/seed_backup.json", "w").write("x")',   # MAL-2
    'subprocess.run(["git", "push"])',                    # MAL-3
])
def test_malicious_tool_code_is_blocked_before_evaluation(tmp_path, code_line):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    ev = ExplodingEvaluator()
    outcome = GEN.run_one_generation(
        state=state, run_ctx=run_ctx, config=app_cfg, evaluator=ev,
        client=StubLLM([{"candidates": [_payload_for(code_line)]}]),
        harness=harness, run_dir=run_root, seed=7)
    assert outcome.decision == "reject"
    # 候选臂 0 消耗：要么在 propose 阶段就被丢弃（``ev.calls == []``），
    # 要么只跑了 parent 的预评估（``["parent"]``）——两条都满足「0 局 heldout」。
    assert all(not call.startswith("cand-") for call in ev.calls)
    report = GenPaths.of(run_root, 1).report
    assert report.is_file()


def test_malicious_candidate_rejected_at_validate_stage(tmp_path):
    """``validate_candidate`` 也能独立挡住（不依赖扫描器）。"""
    cand = make_candidate(kind="tool_patch",
                          diff=tool_diff(added=['subprocess.run(["git", "push"])']),
                          touched=[TOOL])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert result.error_code == str(E.E_EVOLVE_PATCH_INVALID)


# =====================================================================
# MAL-4：env 内窥
# =====================================================================

def test_mal_env_introspection_is_blocked(tmp_path):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    ev = ExplodingEvaluator()
    payload = proposal_payload(kind="tool_patch",
                               diff=tool_diff(added=[
                                   "from gamer_rsi.env.rng import RNG",
                                   "# 直接拿环境 RNG"]),
                               touched=[TOOL])
    outcome = GEN.run_one_generation(
        state=state, run_ctx=run_ctx, config=app_cfg, evaluator=ev,
        client=StubLLM([{"candidates": [payload]}]), harness=harness, run_dir=run_root,
        seed=7)
    assert outcome.decision == "reject"
    assert all(not call.startswith("cand-") for call in ev.calls)


# =====================================================================
# MAL-5：预读作弊（heldout seed 字面量 / 常量分支）
# =====================================================================

@pytest.mark.parametrize("code_line", [
    "HELDOUT = [2013, 2014]",
    "def pick(seed):\n    return 1 if seed == 2013 else 0",
    "if decision_index == 2013:",
])
def test_mal_holdout_seed_literal_is_rejected(tmp_path, code_line):
    cand = make_candidate(kind="tool_patch",
                          diff=tool_diff(added=code_line.split("\n")),
                          touched=[TOOL])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail", code_line
    assert any("heldout_seed_literal" in v for v in result.violations), result.violations


def test_mal_holdout_seed_literal_blocked_end_to_end(tmp_path):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path)
    ev = ExplodingEvaluator()
    payload = proposal_payload(kind="tool_patch",
                               diff=tool_diff(added=["HELDOUT = [2013, 2014]"]),
                               touched=[TOOL])
    outcome = GEN.run_one_generation(
        state=state, run_ctx=run_ctx, config=app_cfg, evaluator=ev,
        client=StubLLM([{"candidates": [payload]}]), harness=harness, run_dir=run_root,
        seed=7)
    assert outcome.decision == "reject"
    assert all(not call.startswith("cand-") for call in ev.calls)


def test_train_seed_literal_is_not_blocked(tmp_path):
    """反例：train 区间（1000–1999）的数字不是泄漏，不能被误拒。"""
    cand = make_candidate(kind="tool_patch",
                          diff=tool_diff(added=["TRAIN_MARK = 1042"]),
                          touched=[TOOL])
    assert P.validate_candidate(cand, cfg=make_config()).status == "pass"


# =====================================================================
# MAL-6：历史重写
# =====================================================================

@pytest.mark.parametrize("path", ["harness/version.json", "harness/versions/v1/x.md"])
def test_mal_history_rewrite_is_rejected(path):
    cand = make_candidate(kind="prompt_patch",
                          diff=("--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-a\n+b\n"
                                % (path, path)),
                          touched=[path])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert result.error_code == str(E.E_EVOLVE_PATCH_INVALID)


# =====================================================================
# MAL-7：签名规避
# =====================================================================

def test_mal_signature_bypass_is_rejected():
    cand = make_candidate(kind="tool_patch",
                          diff=tool_diff(added=["rank_actions = lambda *a: a[1]"]),
                          touched=[TOOL])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("signature_bypassed" in v for v in result.violations)


def test_mal_signature_change_is_rejected():
    cand = make_candidate(kind="tool_patch",
                          diff=tool_diff(removed=["def score_face(obs, candidates, memory):"],
                                         added=["def rank_actions(a, b, c, d):"]),
                          touched=[TOOL])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("signature_changed" in v for v in result.violations)


# =====================================================================
# MAL-8：预算耗尽（单代请求 40 个候选）
# =====================================================================

def test_mal_candidate_flood_is_truncated(tmp_path):
    """40 个候选 → 按优先级截断（``n_candidates`` 上限 3），扫描调用次数 = 截断后的数量。"""
    from gamer_rsi.evolve.schemas import Candidate

    candidates = [
        Candidate.create(kind="prompt_patch",
                         diff=("--- a/harness/prompts/decision.md\n"
                               "+++ b/harness/prompts/decision.md\n"
                               f"@@ -1,0 +1,1 @@\n+# 候选 {i}\n"),
                         rationale=f"依据 traj:train#{i}",
                         expected_effect=["win_rate", "up", (i % 3) + 1],
                         touched_files=["harness/prompts/decision.md"])
        for i in range(40)
    ]
    kept, dropped = P.dedupe_candidates(candidates)
    ordered = P.order_candidates(kept)[:3]
    assert len(ordered) == 3
    assert dropped == 0
    # 优先级降序（3 在前），且候选数没有超过硬上限 3
    assert [c.expected_effect.priority for c in ordered] == [3, 3, 3]


def test_mal_candidate_flood_does_not_exceed_config_cap(tmp_path):
    harness, evolve, app_cfg, run_root, run_ctx, state = _setup(tmp_path, n_candidates=3)
    payloads = []
    for i in range(40):
        payloads.append(proposal_payload(
            kind="prompt_patch",
            diff=("--- a/harness/prompts/decision.md\n"
                  "+++ b/harness/prompts/decision.md\n"
                  f"@@ -1,0 +1,1 @@\n+# 候选 {i}\n"),
            rationale=f"依据 traj:train#{i}",
            touched=["harness/prompts/decision.md"]))
    result = P.propose(
        __import__("gamer_rsi.evolve.schemas", fromlist=["EvidenceBundle"]).EvidenceBundle(
            bundle_id="ev-x"),
        cfg=evolve, rng_seed=1, client=StubLLM([{"candidates": payloads}]))
    assert len(result.candidates) <= 3
    assert result.n_generated == 40
