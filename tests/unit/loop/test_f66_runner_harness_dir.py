"""F66 回归（执行链）：候选臂的 harness 目录必须真的到达 MOD-04 的策略工厂。

修复前：``evals/harness.py`` 的 ``executor = runner or _default_runner(harness_dir=…)``
在**注入 runner 时忽略 harness_dir**，而 MOD-07 每次候选评估都注入
``EvalRunnerAdapter``（其 ``policy_factory`` 绑在 run 级 ``harness/``）⇒
补丁从未进入 prompt。

本文件用**假 LLM**（``ScriptedClient``）跑一局真实 ``EpisodeRunner``，直接看
模型收到的 messages —— 这是"补丁真的进了 prompt"的端到端证据（**修复前必失败**）。
"""
from __future__ import annotations

from pathlib import Path

from fakes import FakeEnv, ScriptedClient
from loop_support import build_runner

from dice_ratchet.agent import PolicyAgent
from dice_ratchet.infra.config import PolicyConfig
from dice_ratchet.loop.runner import EvalRunnerAdapter

REPO_ROOT = Path(__file__).resolve().parents[3]
DECISION = {"reasoning": "r", "plan": [{"kind": "end_turn"}]}
MARKER = "10. **F66-STAGED-MARKER**：本行只存在于 staging 模板里。"


def _stage(root: Path) -> Path:
    """``<root>/staging/<id>/prompts/decision.md``（harness 根布局 + 补丁行）。"""
    staged = root / "staging" / "cand-f66"
    (staged / "prompts").mkdir(parents=True, exist_ok=True)
    src = (REPO_ROOT / "harness" / "prompts" / "decision.md").read_text(encoding="utf-8")
    (staged / "prompts" / "decision.md").write_text(src + "\n" + MARKER + "\n",
                                                   encoding="utf-8")
    return staged


def _run_one_episode(tmp_path, *, harness_dir):
    client = ScriptedClient(default=DECISION)
    runner, _ctx = build_runner(tmp_path, policy=None, env=FakeEnv(steps_to_terminate=2),
                                client=client)

    def factory(seed: int, _key: str, harness_dir: Path | str | None = None):
        target = Path(harness_dir) if harness_dir is not None else REPO_ROOT / "harness"
        return PolicyAgent(PolicyConfig(mode="play"), llm=client, memory=None,
                           harness_dir=target)

    runner.policy_factory = factory           # MOD-06 组合根装的正是这个 3 参工厂
    adapter = EvalRunnerAdapter(runner, harness_dir=harness_dir)
    adapter.run_episode(1000, split="train", arm="cand-f66", generation=1,
                        harness_version=0)
    return client


def test_f66_staged_template_reaches_model_messages(tmp_path):
    """给定 staging 模板（含唯一 marker），当用适配器绑该目录跑一局，
    则模型收到的 messages 里**出现** marker。

    修复前失败签名：messages 里没有 marker（用的是 run 级/仓库模板）。
    """
    staged = _stage(tmp_path)
    client = _run_one_episode(tmp_path, harness_dir=staged)
    assert client.n_calls >= 1
    text = "\n".join(str(getattr(m, "content", "")) for call in client.calls
                     for m in call["messages"])
    assert "F66-STAGED-MARKER" in text, "补丁没有进入 prompt（F66 未修复）"


def test_f66_default_run_level_harness_has_no_marker(tmp_path):
    """反向护墙：不绑 staging（``harness_dir=None``）时**不得**出现 marker。"""
    _stage(tmp_path)
    client = _run_one_episode(tmp_path, harness_dir=None)
    text = "\n".join(str(getattr(m, "content", "")) for call in client.calls
                     for m in call["messages"])
    assert "F66-STAGED-MARKER" not in text


def test_f66_two_arg_factory_still_supported(tmp_path):
    """兼容性：只有两参的旧工厂在**没有** harness_dir 时照常工作（不得回归）。"""
    client = ScriptedClient(default=DECISION)
    runner, _ctx = build_runner(tmp_path, policy=None, env=FakeEnv(steps_to_terminate=2),
                                client=client)
    seen: list[int] = []

    def two_arg_factory(seed: int, _key: str):
        seen.append(int(seed))
        return PolicyAgent(PolicyConfig(mode="play"), llm=client, memory=None,
                           harness_dir=REPO_ROOT / "harness")

    runner.policy_factory = two_arg_factory
    runner.run_episode(1000, split="train", arm="train", harness_version=0,
                       generation=0)
    assert seen == [1000]
