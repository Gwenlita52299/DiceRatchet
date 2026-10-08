"""MOD-04 工具注册表单测（PRD §11 U7/U8 + FR-04-11..13）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from policy_support import FakeResponse, plan

from dice_ratchet.agent.tools.registry import (RANK_ACTIONS_ARGS, ToolRegistry, harness_tool,
                                            load_harness_tools, tool_hash, tool_signatures,
                                            tool_summary)
from dice_ratchet.infra.errors import E

GOOD = '''
@harness_tool
def rank_actions(obs, candidates, memory):
    def key(c):
        return (0 if c.get("kind") == "play_face" else 1, str(c.get("dieId") or ""))
    return sorted([dict(c) for c in candidates], key=key)
'''

BAD_IMPORT = '''
import requests

@harness_tool
def rank_actions(obs, candidates, memory):
    return list(candidates)
'''

RAISES = '''
@harness_tool
def rank_actions(obs, candidates, memory):
    return 1 // 0
'''

WRONG_SIGNATURE = '''
@harness_tool
def rank_actions(x, y):
    return []
'''

NON_LIST = '''
@harness_tool
def rank_actions(obs, candidates, memory):
    return {"not": "a list"}
'''

NOT_EQUIVALENT = '''
@harness_tool
def rank_actions(obs, candidates, memory):
    return [dict(c) for c in candidates][:1]
'''

CANDIDATES = [{"kind": "end_turn"},
              {"kind": "play_face", "dieId": "d-2", "slotId": "slot_1"},
              {"kind": "play_face", "dieId": "d-1", "slotId": "slot_1"}]


def make_harness(root: Path, files: dict[str, str]) -> Path:
    tools = root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (tools / name).write_text(body, encoding="utf-8")
    return root


# ----------------------------------------------------------------------
# U8：静态扫描命中 → 弃用该文件，其余可用
# ----------------------------------------------------------------------

def test_u8_guarded_file_discarded_others_usable(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD, "bad_import.py": BAD_IMPORT})
    registry = load_harness_tools(root)
    try:
        assert E.E_ACTION_TOOL_GUARD in registry.codes()
        assert registry.usable is True
        assert registry.available == ("rank_actions",)
        result = registry.rank_actions({}, CANDIDATES, [])
        assert [c["dieId"] for c in result if "dieId" in c] == ["d-1", "d-2"]
    finally:
        registry.close()


def test_u8_all_files_guarded_means_identity_order(tmp_path):
    root = make_harness(tmp_path / "h", {"bad_import.py": BAD_IMPORT})
    registry = load_harness_tools(root)
    assert E.E_ACTION_TOOL_GUARD in registry.codes()
    assert registry.usable is False
    assert registry.rank_actions({}, CANDIDATES, []) == CANDIDATES


# ----------------------------------------------------------------------
# U7：工具抛异常 → 原始候选序 + E_ACTION_TOOL_FAIL
# ----------------------------------------------------------------------

def test_u7_tool_exception_falls_back_to_original_order(tmp_path):
    root = make_harness(tmp_path / "h", {"boom.py": RAISES})
    registry = load_harness_tools(root)
    try:
        result = registry.rank_actions({}, CANDIDATES, [])
        assert result == CANDIDATES
        assert E.E_ACTION_TOOL_FAIL in registry.codes()
    finally:
        registry.close()


def test_tool_non_list_and_non_equivalent_fall_back(tmp_path):
    for name, body in (("non_list.py", NON_LIST), ("not_equiv.py", NOT_EQUIVALENT)):
        root = make_harness(tmp_path / name, {"t.py": body})
        registry = load_harness_tools(root)
        try:
            assert registry.rank_actions({}, CANDIDATES, []) == CANDIDATES
            assert E.E_ACTION_TOOL_FAIL in registry.codes()
        finally:
            registry.close()


# ----------------------------------------------------------------------
# 签名（FR-04-11）
# ----------------------------------------------------------------------

def test_signature_mismatch_discards_whole_dir(tmp_path):
    root = make_harness(tmp_path / "h", {"badsig.py": WRONG_SIGNATURE, "good.py": GOOD})
    registry = load_harness_tools(root)
    assert E.E_ACTION_TOOL_SIG in registry.codes()
    assert registry.usable is False
    assert registry.rank_actions({}, CANDIDATES, []) == CANDIDATES


def test_tool_signature_summary(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    signatures = tool_signatures(root)
    assert [(s.name, s.args) for s in signatures] == [("rank_actions", RANK_ACTIONS_ARGS)]
    assert tool_summary(root).strip().startswith("- rank_actions(obs, candidates, memory)")


def test_tool_hash_deterministic_and_none_when_empty(tmp_path):
    empty = tmp_path / "empty"
    (empty / "tools").mkdir(parents=True)
    assert tool_hash(empty) is None
    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    assert tool_hash(root) == tool_hash(root)
    assert str(tool_hash(root)).startswith("sha256:")
    make_harness(tmp_path / "h2", {"good.py": GOOD})
    assert tool_hash(root) == tool_hash(tmp_path / "h2")


def test_harness_tool_marks_function():
    def fn(obs, candidates, memory):
        return []

    marked = harness_tool(fn)
    assert marked.__harness_tool__ is True


# ----------------------------------------------------------------------
# 开关与注入（tools_enabled / 自带 worker）
# ----------------------------------------------------------------------

def test_tools_disabled_is_identity(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    registry = load_harness_tools(root, enabled=False)
    assert registry.rank_actions({}, CANDIDATES, []) == CANDIDATES
    assert registry.codes() == ()
    assert registry.usable is False


def test_empty_tools_dir_is_identity(tmp_path):
    (tmp_path / "tools").mkdir(parents=True)
    registry = load_harness_tools(tmp_path)
    assert registry.rank_actions({}, CANDIDATES, []) == CANDIDATES
    assert registry.usable is False
    assert registry.codes() == ()


def test_injected_fake_worker_used_without_subprocess(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD})

    class FakeWorker:
        closed = False

        def tools(self):
            return ["rank_actions"]

        def call(self, tool, *, obs, candidates, memory, timeout_s=None):
            return list(reversed([dict(c) for c in candidates]))

        def close(self):
            type(self).closed = True

    fake = FakeWorker()
    registry = load_harness_tools(root, worker=fake)
    try:
        assert registry.rank_actions({}, CANDIDATES, []) == list(reversed(CANDIDATES))
        assert registry.available == ("rank_actions",)
    finally:
        registry.close()
    assert FakeWorker.closed is False # 外部注入的 worker 由调用方负责关闭


def test_registry_call_generic_entrypoint(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    registry = load_harness_tools(root)
    try:
        result = registry.call("rank_actions", obs={}, candidates=CANDIDATES, memory=[])
        assert [c["dieId"] for c in result if "dieId" in c] == ["d-1", "d-2"]
    finally:
        registry.close()


def test_registry_call_unknown_tool_raises(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    registry = load_harness_tools(root)
    try:
        with pytest.raises(Exception) as excinfo:
            registry.call("nope", obs={}, candidates=[], memory=[])
        assert str(getattr(excinfo.value, "code", "")) in (E.E_ACTION_TOOL_SIG,
                                                           E.E_ACTION_TOOL_FAIL)
    finally:
        registry.close()


def test_registry_close_is_idempotent(tmp_path):
    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    registry: ToolRegistry = load_harness_tools(root)
    registry.close()
    registry.close()


# ----------------------------------------------------------------------
# PolicyAgent 集成：工具重排真实执行顺序
# ----------------------------------------------------------------------

def test_policy_agent_uses_tool_reordering(make_env, tmp_path):
    from dice_ratchet.agent import EpisodeContext, PolicyAgent
    from dice_ratchet.agent.heuristic import heuristic_policy
    from dice_ratchet.infra.config import PolicyConfig

    root = make_harness(tmp_path / "h", {"good.py": GOOD})
    (root / "version.json").write_text(json.dumps({"version": 3}), encoding="utf-8")
    env = make_env(seed=1000)
    obs = env.observe()
    for _ in range(80): # 用启发式推进到战斗回合
        if obs.get("phase") == "player_turn" and len(env.legal_actions()) >= 2:
            break
        env.step(heuristic_policy(obs, env.legal_actions(), env.hints()))
        obs = env.observe()
    else: # pragma: no cover - 真实内核下不会走到
        pytest.skip("未推进到玩家回合")

    legal = env.legal_actions()

    class Client:
        def generate_json(self, messages, schema, *, mode, sampling, seed):
            return FakeResponse(text=plan(*[dict(a) for a in legal[:2]]))

    agent = PolicyAgent(PolicyConfig(), Client(), harness_dir=root)
    ctx = EpisodeContext(run="r", episode="e-001000-g000-train", seed=1000)
    agent.reset(ctx)
    decision = agent.play_decision(env, obs, ctx=ctx)
    executed = [s.action for s in decision.steps if s.action]
    assert executed, decision.error_codes
    # 工具把 play_face 排到前面（若 legal[:2] 含 play_face）
    if all(a.get("kind") == "play_face" for a in executed):
        assert executed[0]["dieId"] <= executed[-1]["dieId"]
    assert decision.tool_hash == tool_hash(root)
    agent.close()
