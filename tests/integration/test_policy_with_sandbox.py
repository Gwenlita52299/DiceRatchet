"""MOD-04 × MOD-02 集成：真实 ``SandboxEnv`` + 可注入的假 MOD-03 后端（PRD §11 集成）。

断言：

1. 同 seed 重跑 3 次，MOD-04 的 ``prompt_hash`` 序列与动作序列逐字节一致（G3）；
2. ``DecisionResult`` 能按 Spec §4.5 组装出合法轨迹行，并通过 ``trajectory_line`` schema
   round-trip（G5）；
3. prompt 出口在 MOD-02 泄漏扫描下 0 finding，且蜜罐哨兵注入必被抓（G4）；
4. MOD-04 不 import ``dice_ratchet.env.*``（INV-1 / NFR-04-6 静态门禁）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dice_ratchet.agent import EpisodeContext, PolicyAgent
from dice_ratchet.agent.heuristic import heuristic_policy
from dice_ratchet.infra.config import PolicyConfig
from dice_ratchet.infra.errors import E
from dice_ratchet.infra.guards import scan_layer_imports
from dice_ratchet.sandbox.api import EpisodeConfig, SandboxEnv
from dice_ratchet.sandbox.audit import (VisibleSet, build_forbidden, derive_hidden_tokens,
                                     scan_text)
from dice_ratchet.sandbox.honeypot import HONEYPOT_CANARY
from dice_ratchet.sandbox.projection import obs_digest

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "schemas/trajectory_line.schema.json").read_text(encoding="utf-8"))

_ENGINE: Any = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from dice_ratchet.env import GameEngine, load_tables

        _ENGINE = GameEngine(load_tables())
    return _ENGINE


@dataclass
class _Usage:
    prompt_tokens: int = 12
    completion_tokens: int = 6
    latency_ms: int = 9


@dataclass
class _Response:
    text: str
    parsed: Any = None
    usage: Any = None
    attempts: int = 1
    prompt_digest: str = "sha256:" + "0" * 64
    model_id: str = "fake-8b"
    backend: str = "mock"

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = _Usage()


class _PlanClient:
    """由调用方预设文本；用于把启发式选择喂给 MOD-04 的 LLM 路径。"""

    def __init__(self) -> None:
        self.next_text = ""
        self.calls = 0

    def generate_json(self, messages, schema, *, mode, sampling, seed):
        self.calls += 1
        text = self.next_text or '{"reasoning":"noop","plan":[{"kind":"end_turn"}]}'
        return _Response(text=text)


def _run_episode(seed: int = 1000, *, max_decisions: int = 10):
    """用启发式的合法选择驱动 MOD-04 LLM 路径，返回 ``(决策序列, 观测摘要序列)``。"""
    env = SandboxEnv(engine=_engine(), episode_config=EpisodeConfig())
    obs = env.reset(seed)
    client = _PlanClient()
    agent = PolicyAgent(PolicyConfig(), client, harness_dir=REPO / "harness")
    ctx = EpisodeContext(run="r-integration", episode=f"e-{seed:06d}-g000-train",
                         seed=seed, split="train", mode="train", tick=1)
    agent.reset(ctx)
    decisions: list[Any] = []
    digests: list[str] = []
    for _ in range(max_decisions):
        action = heuristic_policy(obs, env.legal_actions(), env.hints())
        client.next_text = json.dumps({"reasoning": "integration",
                                       "plan": [dict(action)] if action else []},
                                      ensure_ascii=False)
        decision = agent.play_decision(env, obs, ctx=ctx)
        decisions.append(decision)
        digests.append(obs_digest(obs))
        obs = decision.last.observation
        if decision.last.terminated or decision.last.truncated:
            break
    agent.close()
    env.close()
    return decisions, digests


def _actions(decision) -> list[dict]:
    return [s.action for s in decision.steps if s.action]


# ----------------------------------------------------------------------
# G3：确定性
# ----------------------------------------------------------------------

def test_prompt_hash_and_action_sequence_are_deterministic():
    first, digests_a = _run_episode(seed=1000)
    second, digests_b = _run_episode(seed=1000)
    assert digests_a == digests_b
    assert [d.prompt_hash for d in first] == [d.prompt_hash for d in second]
    assert [_actions(d) for d in first] == [_actions(d) for d in second]
    assert all(d.source == "llm" for d in first), [d.error_codes for d in first]
    assert all(d.prompt_hash.startswith("sha256:") for d in first)


def test_three_runs_identical_on_another_seed():
    runs = [_run_episode(seed=1001)[0] for _ in range(3)]
    hashes = [[d.prompt_hash for d in run] for run in runs]
    assert hashes[0] == hashes[1] == hashes[2]
    assert [[_actions(d) for d in run] for run in runs][0] == \
        [[_actions(d) for d in run] for run in runs][1]


# ----------------------------------------------------------------------
# G5：轨迹行 schema + round-trip
# ----------------------------------------------------------------------

def test_decision_produces_schema_valid_trajectory_line():
    decisions, digests = _run_episode(seed=1000, max_decisions=3)
    decision = decisions[0]
    step = next(s for s in decision.steps if s.action)
    row = {
        "episode_key": "e-001000-g000-train",
        "generation": 0,
        "harness_version": 0,
        "arm": "train",
        "seed": 1000,
        "split": "train",
        "step": 0,
        "decision_index": 0,
        "plan_index": step.plan_index,
        "attempt": step.attempt,
        "source": step.source,
        "obs_digest": digests[0],
        "action": step.action,
        "reward": round(step.result.reward, 4),
        "cum_reward": round(step.result.reward, 4),
        "illegal": step.illegal,
        "illegal_reason": step.illegal_reason,
        "llm_raw": decision.llm.raw if decision.llm else None,
        "llm_latency_ms": decision.llm.latency_ms if decision.llm else None,
        "prompt_tokens": decision.llm.prompt_tokens if decision.llm else None,
        "completion_tokens": decision.llm.completion_tokens if decision.llm else None,
        "prompt_hash": decision.prompt_hash,
        "memory_ids": list(decision.memory_ids),
        "tool_hash": decision.tool_hash,
        "mode": "train",
        "ts": "2026-09-30T12:00:00Z",
    }
    _validate(row)
    assert json.loads(json.dumps(row, ensure_ascii=False)) == row


def _validate(row: dict) -> None:
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(row),
                    key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def test_decision_error_codes_are_registered_codes():
    decisions, _ = _run_episode(seed=1000, max_decisions=5)
    known = {str(code) for code in E}
    for decision in decisions:
        assert set(decision.error_codes) <= known


# ----------------------------------------------------------------------
# G4：泄漏扫描
# ----------------------------------------------------------------------

def test_prompt_surface_has_no_leak_findings(tmp_path, build_run):
    built = build_run(tmp_path, run_id="policy-integration", seed=1000)
    # F58（2026-10-14）：``derive_hidden_tokens`` 现在返回**逐局索引**
    # （``HiddenTokenIndex``）。单局场景取该局的隐藏集合；跨局出口用 ``unscoped`` 兜底。
    hidden = derive_hidden_tokens(built.root, split="train")
    episode_hidden = hidden.by_episode.get("e-001000-g000-train", hidden.unscoped)
    visible = VisibleSet(obs_paths=(), obs_digests=(), memory_texts=(),
                         harness_version=0, seeds_seen=(1000,))
    forbidden = build_forbidden(visible, hidden=episode_hidden)

    agent = PolicyAgent(PolicyConfig(), harness_dir=REPO / "harness")
    env = SandboxEnv(engine=_engine(), episode_config=EpisodeConfig())
    obs = env.reset(1000)
    bundle = agent.prompt_bundle(obs, ctx=EpisodeContext(seed=1000))
    findings = scan_text(bundle.text, forbidden=forbidden, surface="prompt",
                         episode_id="e-001000-g000-train", step=0)
    assert findings == []
    decisions, _ = _run_episode(seed=1000, max_decisions=2)
    assert decisions
    agent.close()
    env.close()


def test_honeypot_canary_in_prompt_is_caught():
    agent = PolicyAgent(PolicyConfig(), harness_dir=REPO / "harness")
    env = SandboxEnv(engine=_engine(), episode_config=EpisodeConfig())
    obs = env.reset(1000)
    bundle = agent.prompt_bundle(obs, ctx=EpisodeContext(seed=1000))
    tainted = bundle.text + f"\nleaked: {HONEYPOT_CANARY}\n"
    forbidden = build_forbidden(VisibleSet(obs_paths=(), obs_digests=(), memory_texts=(),
                                           harness_version=0, seeds_seen=()))
    findings = scan_text(tainted, forbidden=forbidden, surface="prompt",
                         episode_id="e", step=0)
    assert findings, "蜜罐哨兵注入必须被抓到（G4）"
    assert any(getattr(f.leak_class, "value", "") == "canary_leak" for f in findings)
    agent.close()
    env.close()


# ----------------------------------------------------------------------
# INV-1 静态门禁
# ----------------------------------------------------------------------

def test_agent_package_has_no_env_imports():
    findings = [f for f in scan_layer_imports(REPO / "dice_ratchet" / "agent")
                if not f.exempted]
    assert findings == [], [f.as_dict() for f in findings]
