"""FR-07-4 / FR-07-5：候选改动面硬约束、去重与排序（PRD §11 单测 1/2）。

「给定-当-则」逐条对应 PRD §11：

1. 给定 3 个候选 diff，当 ``validate_candidate``，则超过 ``max_diff_lines=40`` 者
   ``status="fail"`` 且 ``reason`` 含 ``diff_lines``；给定 ``tool_patch`` 把签名改成
   ``def rank_actions(obs, candidates, memory, extra)``，则拒绝 ``E_EVOLVE_PATCH_INVALID``；
2. 给定含 ``import numpy`` / ``import requests`` / ``urllib.request`` 的 ``tool_patch``，
   或 ``config_patch`` 修改 ``evolvable_keys`` 外的键（如 ``sampling.seed``），则拒绝。
"""

from __future__ import annotations

import pytest

from dice_ratchet.evolve import proposer as P
from dice_ratchet.evolve.errors import PatchInvalidError
from dice_ratchet.infra.errors import E

from evolve_support import make_candidate, make_config, prompt_diff, tool_diff

# =====================================================================
# FR-07-4：行数 / 字符数上限
# =====================================================================


def test_diff_lines_over_limit_is_rejected_with_reason():
    """超过 ``max_diff_lines=40`` → fail 且 reason 含 ``diff_lines``。"""
    added = "\n".join(f"+第 {i} 行新内容" for i in range(45))
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -1,0 +1,45 @@\n" + added + "\n")
    cand = make_candidate(diff=diff)
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert "diff_lines" in result.reason
    assert result.error_code == str(E.E_EVOLVE_PATCH_INVALID)
    assert result.diff_lines == 45


def test_diff_chars_over_limit_is_rejected():
    diff = prompt_diff(added="x" * 5000)
    cand = make_candidate(diff=diff)
    result = P.validate_candidate(cand, cfg=make_config(max_diff_chars=4000))
    assert result.status == "fail"
    assert any(v.startswith("diff_chars") for v in result.violations)


def test_clean_prompt_patch_passes():
    cand = make_candidate()
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "pass"
    assert result.touched_files == ("harness/prompts/decision.md",)


def test_more_than_two_files_rejected():
    diff = (prompt_diff(path="harness/prompts/decision.md")
            + prompt_diff(path="harness/prompts/reflection.md")
            + prompt_diff(path="harness/config.json"))
    cand = make_candidate(diff=diff,
                          touched=["harness/prompts/decision.md",
                                   "harness/prompts/reflection.md",
                                   "harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any(v.startswith("too_many_files") for v in result.violations)


def test_path_outside_whitelist_rejected():
    cand = make_candidate(kind="prompt_patch", diff=prompt_diff(path="harness/loop.py"),
                          touched=["harness/loop.py"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("path_not_in_whitelist" in v for v in result.violations)


def test_version_json_and_versions_dir_are_forbidden_paths():
    for path in ("harness/version.json", "harness/versions/v1/prompts/decision.md"):
        cand = make_candidate(kind="prompt_patch", diff=prompt_diff(path=path),
                              touched=[path])
        result = P.validate_candidate(cand, cfg=make_config())
        assert result.status == "fail", path
        assert any("forbidden_path" in v for v in result.violations), result.violations


def test_path_traversal_rejected():
    cand = make_candidate(
        kind="prompt_patch",
        diff="--- a/harness/../secrets.md\n+++ b/harness/../secrets.md\n@@ -1 +1 @@\n-a\n+b\n",
        touched=["harness/../secrets.md"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"


# =====================================================================
# FR-07-4：签名冻结 / 依赖白名单 / 禁用调用
# =====================================================================


def test_rank_actions_signature_change_is_rejected():
    """给定把签名改成 4 参 → 拒绝 ``E_EVOLVE_PATCH_INVALID``（MAL-7 同源）。"""
    diff = tool_diff(removed=["def score_face(obs, candidates, memory):"],
                     added=["def rank_actions(obs, candidates, memory, extra):"])
    cand = make_candidate(kind="tool_patch", diff=diff,
                          touched=["harness/tools/score.py"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("signature_changed" in v for v in result.violations)
    assert result.error_code == str(E.E_EVOLVE_PATCH_INVALID)


def test_rank_actions_lambda_bypass_is_rejected():
    diff = tool_diff(added=["rank_actions = lambda *a: a[1]"])
    cand = make_candidate(kind="tool_patch", diff=diff,
                          touched=["harness/tools/score.py"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("signature_bypassed" in v for v in result.violations)


def test_harness_tool_decorator_removal_is_rejected():
    diff = tool_diff(removed=["@harness_tool"], added=["# 去掉装饰器"])
    cand = make_candidate(kind="tool_patch", diff=diff,
                          touched=["harness/tools/score.py"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("decorator" in v for v in result.violations)


@pytest.mark.parametrize("imprt", ["import numpy", "import requests",
                                   "import urllib.request", "import os",
                                   "from dice_ratchet.env.rng import RNG"])
def test_third_party_import_rejected(imprt):
    diff = tool_diff(added=[imprt])
    cand = make_candidate(kind="tool_patch", diff=diff,
                          touched=["harness/tools/score.py"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail", imprt
    assert any("import" in v for v in result.violations), result.violations


@pytest.mark.parametrize("code", [
    'urllib.request.urlopen("http://x")',
    'open("harness/seed_backup.json", "w")',
    'subprocess.run(["git", "push"])',
    'os.environ["X"]',
])
def test_banned_calls_rejected(code):
    """MAL-1/2/3/4：网络 / 文件写 / 子进程 / env 内窥。"""
    diff = tool_diff(added=[code])
    cand = make_candidate(kind="tool_patch", diff=diff,
                          touched=["harness/tools/score.py"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail", code
    assert any("banned_call" in v for v in result.violations), result.violations


def test_allowlisted_import_passes():
    diff = tool_diff(added=["import math", "# 使用 math 打分"])
    cand = make_candidate(kind="tool_patch", diff=diff,
                          touched=["harness/tools/score.py"])
    assert P.validate_candidate(cand, cfg=make_config()).status == "pass"


# =====================================================================
# FR-07-4：config_patch 白名单
# =====================================================================


def test_config_patch_evolvable_key_passes():
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -2,1 +2,1 @@\n-  "agent.policy.memory_top_k": 5,\n'
            '+  "agent.policy.memory_top_k": 3,\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "pass", result.violations


def test_config_patch_non_evolvable_key_rejected():
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -3,1 +3,1 @@\n-  "seed": 0\n+  "seed": 7\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "fail"
    assert any("config_key_not_evolvable" in v for v in result.violations)


def test_config_patch_nested_object_rejected_with_actionable_reason():
    """F-M3-4：m3r4 cycle 1 的现场形态 ``{"retrieval": {"top_k": 3}}``。

    嵌套对象不是 ``harness/config.json`` 的合法形态（加载器只收扁平点分键），
    必须在**花钱之前**拒绝，并告诉提案方该写什么；不能像旧实现那样只报
    ``config_key_not_evolvable:['retrieval']``（看起来像「键名不对」）。
    """
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"sampling": {"memory_top_k": 3}}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "fail"
    assert any("config_patch_nested_object" in v for v in result.violations)
    assert any("agent.policy.memory_top_k" in v for v in result.violations)


def test_config_patch_conceptual_short_key_rejected():
    """概念名（``sampling.memory_top_k``）不能直接写进 config.json。"""
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"sampling.memory_top_k": 3}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "fail"
    assert any("config_key_not_evolvable" in v for v in result.violations)


def test_config_patch_actual_agent_policy_key_passes():
    """``agent.policy.temperature`` 是 ``sampling.temperature`` 的实际键。"""
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"agent.policy.temperature": 0.8}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.temperature",)))
    assert result.status == "pass", result.violations


def test_config_patch_stale_show_intent_key_rejected():
    """``prompt.show_intent`` 已从白名单移除（F-M3-5）：旧配置里残留它也不放行。

    它是 Spec §5 的概念占位键，``PolicyConfig`` 从来没有这个字段。历史实现靠一个
    专门的 ``config_key_unimplemented`` 分支拦住它；现在走通用路径（列表外的键），
    理由更直接：它就不是一个可演化键。
    """
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"agent.policy.show_intent": true}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k", "sampling.temperature",
                        "prompt.show_intent")))
    assert result.status == "fail"
    # 白名单里有、规格表里没有 ⇒ 「没有实现」，与「不在白名单」区分开
    assert any("config_key_unsupported" in v for v in result.violations)


def test_config_patch_unknown_dotted_path_rejected():
    """``agent.policy.foo.top_k`` 不能靠「末段相同」混过白名单。"""
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"agent.policy.foo.top_k": 3}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "fail"


def test_config_patch_container_replaced_by_scalar_rejected():
    """把整个容器替换成标量（``{"retrieval": 3}``）不是「改一个键」，必须拒绝。"""
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1 +1 @@\n-{}\n+{"retrieval": 3}\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "fail"
    assert any("config_key_not_evolvable" in v for v in result.violations)


def test_config_patch_multiline_nested_fragment_rejected():
    """逐行 diff 的嵌套片段（非完整 JSON）同样不是扁平键形态 ⇒ 拒绝。"""
    diff = ('--- a/harness/config.json\n+++ b/harness/config.json\n'
            '@@ -1,2 +1,4 @@\n'
            ' {\n'
            '+  "retrieval": {\n'
            '+    "top_k": 3\n'
            '+  }\n'
            ' }\n')
    cand = make_candidate(kind="config_patch", diff=diff,
                          touched=["harness/config.json"])
    result = P.validate_candidate(cand, cfg=make_config(
        evolvable_keys=("sampling.memory_top_k",)))
    assert result.status == "fail"
    assert any("config_key_not_evolvable" in v for v in result.violations)


def test_tool_patch_requires_tool_file():
    """``kind="tool_patch"`` 但 diff 只碰 prompt 文件 → 拒绝。"""
    cand = make_candidate(kind="tool_patch", diff=prompt_diff(),
                          touched=["harness/prompts/decision.md"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("tool_patch_without_tool_file" in v for v in result.violations)


def test_declared_touched_files_must_match_diff():
    """``touched_files`` 只能声明 diff 真实碰到的文件（防止掩盖改动面）。"""
    cand = make_candidate(kind="prompt_patch", diff=prompt_diff(),
                          touched=["harness/prompts/reflection.md"])
    result = P.validate_candidate(cand, cfg=make_config())
    assert result.status == "fail"
    assert any("declared_files_mismatch" in v for v in result.violations)


# =====================================================================
# FR-07-5：去重与排序（确定性）
# =====================================================================


def test_dedupe_by_normalized_diff():
    base = prompt_diff(added="新增一行")
    same_but_trailing_ws = base.replace("+新增一行", "+新增一行  ") + "\n\n"
    a = make_candidate(diff=base, rationale="first")
    b = make_candidate(diff=same_but_trailing_ws, rationale="second")
    kept, dropped = P.dedupe_candidates([a, b])
    assert len(kept) == 1
    assert dropped == 1
    assert kept[0].rationale == "first"


def test_order_by_priority_desc_then_id_asc():
    low = make_candidate(diff=prompt_diff(added="低优先级"), effect=("win_rate", "up", 1),
                         rationale="low")
    high_b = make_candidate(diff=prompt_diff(added="高优先级-b"),
                            effect=("win_rate", "up", 3), rationale="high-b")
    high_a = make_candidate(diff=prompt_diff(added="高优先级-a"),
                            effect=("win_rate", "up", 3), rationale="high-a")
    ordered = P.order_candidates([low, high_b, high_a])
    assert [c.expected_effect.priority for c in ordered] == [3, 3, 1]
    ids = [c.candidate_id for c in ordered[:2]]
    assert ids == sorted(ids)


def test_order_is_stable_and_deterministic():
    cands = [make_candidate(diff=prompt_diff(added=f"行{i}")) for i in range(5)]
    assert P.order_candidates(cands) == P.order_candidates(list(reversed(cands)))


def test_candidate_id_is_content_addressed():
    a = make_candidate(rationale="同一个理由")
    b = make_candidate(rationale="同一个理由")
    c = make_candidate(rationale="另一个理由")
    assert a.candidate_id == b.candidate_id
    assert a.candidate_id != c.candidate_id
    assert len(a.candidate_id) == 12


# =====================================================================
# diff 应用（staging 的前置）
# =====================================================================


def test_apply_unified_diff_modifies_and_verifies(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "decision.md").write_text("a\nb\nc\n")
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -1,2 +1,3 @@\n a\n+b2\n b\n")
    P.apply_unified_diff(tmp_path, diff, paths=["harness/prompts/decision.md"])
    assert (tmp_path / "prompts" / "decision.md").read_text() == "a\nb2\nb\nc\n"


def test_apply_unified_diff_context_mismatch_raises(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "decision.md").write_text("x\ny\nz\n")
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -1,2 +1,3 @@\n WRONG\n+b2\n y\n")
    with pytest.raises(PatchInvalidError):
        P.apply_unified_diff(tmp_path, diff, paths=["harness/prompts/decision.md"])


def test_apply_unified_diff_rejects_dev_null(tmp_path):
    (tmp_path / "prompts").mkdir()
    with pytest.raises(PatchInvalidError):
        P.apply_unified_diff(
            tmp_path,
            "--- /dev/null\n+++ b/harness/prompts/new.md\n@@ -0,0 +1 @@\n+hi\n")


def test_apply_unified_diff_declared_files_must_match(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "decision.md").write_text("a\n")
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n@@ -1,0 +1,1 @@\n+b\n")
    with pytest.raises(PatchInvalidError):
        P.apply_unified_diff(tmp_path, diff, paths=["harness/prompts/reflection.md"])


def test_count_diff_lines_excludes_file_headers():
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -1 +1,2 @@\n-a\n+b\n+c\n")
    assert P.count_diff_lines(diff) == 3


# =====================================================================
# F39：hunk 定位容忍 offset（标准 ``patch`` 语义），但**不做 fuzz**
# =====================================================================

_BASE_12 = "\n".join(f"line-{i}" for i in range(1, 13)) + "\n"


def _write_prompt(tmp_path, text: str = _BASE_12):
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "prompts" / "decision.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_f39_hunk_offset_is_tolerated(tmp_path):
    """F39：声明行号偏了、但上下文**逐字**命中时必须能应用。

    实测现场：``deepseek-flash`` 产出 ``@@ -44,6 +44,10 @@``，其 4 行上下文全部逐字
    存在于第 53–56 行（偏移 +9）——模型抄对了原文，只是不会数行号。原实现只认声明行号，
    把**每一个**真实候选都判成 ``E_EVOLVE_PATCH_INVALID``，harness 永远无法演化。
    """
    path = _write_prompt(tmp_path)
    # 正文在第 5–6 行，但 hunk 声明从第 9 行开始（偏移 -4）
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -9,2 +9,3 @@\n"
            " line-5\n"
            " line-6\n"
            "+inserted\n")
    touched = P.apply_unified_diff(tmp_path, diff)

    assert touched == ("prompts/decision.md",)
    out = path.read_text(encoding="utf-8").split("\n")
    # ``+`` 行落在其**前导上下文行之后**（unified diff 的语义）：line-5/line-6 之后插入。
    assert out[:7] == ["line-1", "line-2", "line-3", "line-4", "line-5", "line-6",
                       "inserted"], "补丁必须落在**上下文命中**处，而不是声明行号处"


def test_f39_fuzz_is_not_allowed(tmp_path):
    """容忍 offset **不等于**容忍 fuzz：上下文差一个字符就必须拒绝。"""
    _write_prompt(tmp_path)
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -5,2 +5,3 @@\n"
            " line-5-CHANGED\n"
            " line-6\n"
            "+inserted\n")
    with pytest.raises(PatchInvalidError) as excinfo:
        P.apply_unified_diff(tmp_path, diff)
    assert "hunk 上下文不匹配" in str(excinfo.value)


def test_f39_ambiguous_offset_is_rejected(tmp_path):
    """同一距离上两侧都逐字命中 → 歧义 → 拒绝（宁可判无效，也不赌落点）。"""
    _write_prompt(tmp_path, "same\nfiller\nsame\nfiller\n")
    diff = ("--- a/harness/prompts/decision.md\n"
            "+++ b/harness/prompts/decision.md\n"
            "@@ -2,1 +2,2 @@\n"
            " same\n"
            "+inserted\n")
    with pytest.raises(PatchInvalidError) as excinfo:
        P.apply_unified_diff(tmp_path, diff)
    assert "歧义" in str(excinfo.value)
