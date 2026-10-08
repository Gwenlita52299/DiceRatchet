"""NFR-02-4：拒绝 / 超时 / 触发全部产**已登记**的结构化事件（Spec §8.2 / PRD §13）。

做法：绑定一个真实 ``events.jsonl`` sink，把各条失败路径跑一遍，再断言事件名出现在
落盘文件里且**没有** ``infra.unknown_event``（未登记事件在开发模式下会被替换/丢弃）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dice_ratchet.infra import events
from dice_ratchet.sandbox import guard as G
from dice_ratchet.sandbox.api import EpisodeConfig, SandboxEnv
from dice_ratchet.sandbox.audit import run_audit
from dice_ratchet.sandbox.honeypot import HONEYPOT_CANARY


@pytest.fixture
def sink(tmp_path, monkeypatch):
    # 精确保存/恢复 dev 模式：``set_dev_mode(None)`` 会重读环境变量，而 monkeypatch
    # 撤销 DICERATCHET_DEV 发生在 fixture teardown 之后，会把 dev 模式泄漏给后续用例。
    previous = events.dev_mode()
    monkeypatch.setenv("DICERATCHET_DEV", "1")
    events.set_dev_mode(True)
    path = tmp_path / "events.jsonl"
    events.bind(path, "evt-test", append=False)
    yield path
    events.unbind()
    events.set_dev_mode(previous)


def _names(path: Path) -> list[str]:
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _tool(path: Path, name: str, body: str) -> None:
    (path / "tools").mkdir(parents=True, exist_ok=True)
    (path / "tools" / name).write_text(body, encoding="utf-8")


def test_failure_paths_emit_registered_events(sink, tmp_path, game_engine, build_run):
    # 1) 投影事件（含 duration_us / dropped_hidden）
    env = SandboxEnv(engine=game_engine, episode_config=EpisodeConfig())
    env.reset(1000, episode_id="e-001000-g000-train")
    env.observe()

    # 2) import 拒绝事件
    try:
        exec(compile("import dice_ratchet.env.rng", "<agent>", "exec"),
             {"__name__": "dice_ratchet.loop.runner"})
    except G.GuardViolation:
        pass

    # 3) gate：先拒（env.ast_denied + sandbox.gate_reject），再通过（sandbox.gate_pass）
    bad = tmp_path / "bad-harness"
    _tool(bad, "bad.py", "import os\n")
    G.gate(bad)
    good = tmp_path / "good-harness"
    _tool(good, "rank.py", "def rank_actions(obs, candidates, memory):\n"
                           "    return candidates\n")
    G.gate(good)

    # 4) worker：成功（sandbox.exec）、超时（sandbox.tool_timeout）、逃逸（env.honeypot_escape）
    harness = tmp_path / "worker-harness"
    _tool(harness, "rank.py", "def rank_actions(obs, candidates, memory):\n"
                              "    return candidates\n")
    _tool(harness, "slow.py", "import time\n\n\n"
                              "def slow_rank(obs, candidates, memory):\n"
                              "    time.sleep(5)\n"
                              "    return candidates\n")
    _tool(harness, "sneaky.py", "def sneaky(obs, candidates, memory):\n"
                                "    return [len(_HONEYPOT_LEDGER)]\n")
    worker = G.load_harness_tools(harness, timeout_s=0.5,
                                  import_allowlist=("time",))
    try:
        worker.call("rank_actions", obs={}, candidates=[{"kind": "end_turn"}], memory=[])
        with pytest.raises(G.GuardViolation):
            worker.call("slow_rank", obs={}, candidates=[], memory=[], timeout_s=0.3)
        worker.restart()
        with pytest.raises(G.GuardViolation):
            worker.call("sneaky", obs={}, candidates=[], memory=[])
    finally:
        worker.close()

    # 5) 审计事件（audit_finding / honeypot_selftest / honeypot_hit）
    built = build_run(tmp_path / "runs", run_id="evt-run",
                      prompt_text=f"泄漏 {HONEYPOT_CANARY}\n")
    run_audit(built.root, generated_at="2026-09-30T12:00:00Z")

    names = _names(sink)
    for expected in ("env.projection_pruned", "env.import_denied", "env.ast_denied",
                     "sandbox.gate_reject", "sandbox.gate_pass", "sandbox.exec",
                     "sandbox.tool_timeout", "env.honeypot_escape",
                     "sandbox.audit_finding", "sandbox.honeypot_selftest",
                     "sandbox.honeypot_hit"):
        assert expected in names, f"缺事件 {expected}；实际={sorted(set(names))}"
    assert "infra.unknown_event" not in names # 事件名全部已登记（Spec §8.2）

    rows = [json.loads(x) for x in sink.read_text(encoding="utf-8").splitlines() if x.strip()]
    projection = next(r for r in rows if r["event"] == "env.projection_pruned")
    assert {"fields", "dropped_hidden", "duration_us"} <= set(projection)
    denied = next(r for r in rows if r["event"] == "env.import_denied")
    assert denied["module"] == "dice_ratchet.env.rng"
    assert denied["caller"] == "dice_ratchet.loop.runner"
    overflow = next(r for r in rows if r["event"] == "sandbox.exec")
    assert {"tool", "duration_ms", "outcome"} <= set(overflow)
