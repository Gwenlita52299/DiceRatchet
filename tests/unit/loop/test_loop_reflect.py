"""MOD-06 局末反思与降级（PRD §11 的 T-06-4 / T-06-5；FR-06-3/4）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakes import FakeEnv, FakePolicy
from gamer_rsi.infra.errors import E


def _reflection_path(ctx, episode_key: str) -> Path:
    return Path(ctx.paths.reflections) / f"{episode_key}.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# =====================================================================
# T-06-4 反思成功与用量回写
# =====================================================================

def test_t06_4_train_episode_reflects_and_records_usage(tmp_path, mk_orchestrator,
                                                        mk_memory, mk_client):
    """给定 ``reflect()`` 返回合法 ``ReflectionEntry``；当训练局局末；

    则 ``reflections/<episode_key>.json`` 为 ``status=="ok"``、
    ``memory.add_reflection`` 与 ``memory.record_usage`` 各调用 1 次、``tick`` +1。
    """
    memory = mk_memory()
    client = mk_client([{"text": "净化值低时优先防守。", "failure_cause": "low_damage",
                         "tags": ["defense"]}])
    policy = FakePolicy(memory_ids=["m-1", "m-1"], adopted=["m-1"])
    env = FakeEnv(steps_to_terminate=3, outcome="lose")
    orch, ctx = mk_orchestrator(tmp_path, policy=policy, env=env, memory=memory,
                                client=client, seeds=(1000,))

    summary = orch.run(seeds=[1000])

    assert summary.exit_code == 0
    record = _load(_reflection_path(ctx, "e-001000-g000-train"))
    assert record["status"] == "ok"
    assert record["episode_key"] == "e-001000-g000-train"
    assert record["entry"]["text"] == "净化值低时优先防守。"
    assert record["entry"]["failure_cause"] == "low_damage"
    assert record["entry"]["tags"] == ["defense"]
    assert record["llm"]["model"] == "fake-8b"
    assert record["template_sha256"].startswith("sha256:")
    assert len(memory.reflections) == 1
    assert len(memory.usages) == 1
    usage = memory.usages[0]
    assert usage["episode_key"] == "e-001000-g000-train"
    assert usage["memory_ids"] == ["m-1"]      # 去重
    assert usage["adopted_ids"] == ["m-1"]
    assert usage["outcome"] == "lose"
    assert ctx.state.tick == 1                  # FR-06-3a：逻辑时钟 +1
    assert ctx.state.counters.reflections_ok == 1
    # 只调一次推理（反思），且用 eval 温度
    assert client.n_calls == 1
    assert client.calls[0]["mode"] == "eval"
    assert client.calls[0]["sampling"]["temperature"] == 0.0


def test_t06_4_eval_episode_never_reflects_or_writes(tmp_path, mk_runner, mk_memory,
                                                     mk_client):
    """评估局不反思、不回写（INV-5）。"""
    memory = mk_memory()
    client = mk_client([{"text": "should not be used", "failure_cause": "unknown",
                         "tags": []}])
    policy = FakePolicy(memory_ids=["m-1"], adopted=["m-1"])
    runner, ctx = mk_runner(tmp_path, policy=policy, env=FakeEnv(steps_to_terminate=2),
                            memory=memory, client=client, memory_version=1)
    rec = runner.run_episode(2000, split="heldout", arm="parent", harness_version=0)
    assert rec.episode_key == "e-002000-g000-parent"
    assert memory.usages == []
    assert memory.reflections == []
    assert client.n_calls == 0
    # 评估局用 frozen(v) 只读视图（FR-06-14）
    assert memory.frozen_calls == [1]
    assert not _reflection_path(ctx, rec.episode_key).exists()


def test_t06_4_reflect_disabled_writes_skipped(tmp_path, mk_orchestrator, mk_memory,
                                               mk_client):
    """``reflect_enabled=false`` → 不调模型，写 ``reflect_disabled`` 标注（消融路径）。"""
    memory = mk_memory()
    client = mk_client([])
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=memory,
                                client=client, seeds=(1000,))
    from dataclasses import replace

    orch.config = replace(orch.config, reflect_enabled=False)
    orch.runner.config = orch.config
    orch.run(seeds=[1000])
    record = _load(_reflection_path(ctx, "e-001000-g000-train"))
    assert record["status"] == "skipped"
    assert record["reason"] == "reflect_disabled"
    assert client.n_calls == 0
    assert ctx.state.counters.reflections_skipped == 1


# =====================================================================
# T-06-5 反思失败降级
# =====================================================================

def test_t06_5_reflect_timeout_degrades(tmp_path, mk_orchestrator, mk_memory, mk_client):
    """给定 ``reflect()`` 抛 ``TimeoutError``；当局末；

    则文件为 ``{"status":"skipped","reason":"E_LLM_TIMEOUT"}``、``reflections_skipped+1``，
    且下一局照常开跑（断言 ``run()`` 正常返回）。
    """
    memory = mk_memory()
    client = mk_client([TimeoutError("boom")], default=TimeoutError("boom"))
    env = FakeEnv(steps_to_terminate=2)
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(), env=env, memory=memory,
                                client=client, seeds=(1000, 1001))

    summary = orch.run(seeds=[1000, 1001])

    assert summary.exit_code == 0
    assert summary.phase == "done"
    record = _load(_reflection_path(ctx, "e-001000-g000-train"))
    assert record["status"] == "skipped"
    assert record["reason"] == str(E.E_LLM_TIMEOUT)
    assert record["entry"] is None
    # 第二局照常开跑（反思失败绝不阻塞 run）
    assert ctx.state.completed_train_seeds == [1000, 1001]
    assert len(env.resets) == 2
    assert memory.reflections == []
    assert ctx.state.counters.reflections_skipped == 2


@pytest.mark.parametrize("failure,expected", [
    (ValueError("bad json"), E.E_LLM_SCHEMA_INVALID),
    (RuntimeError("model down"), E.E_LLM_SCHEMA_INVALID),
])
def test_t06_5_reflect_failure_reasons(tmp_path, mk_orchestrator, mk_memory, mk_client,
                                       failure, expected):
    """非超时类失败同样降级，并映射到已登记的 ``E_*``（PRD §10）。"""
    memory = mk_memory()
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=memory,
                                client=mk_client([failure]), seeds=(1000,))
    orch.run(seeds=[1000])
    record = _load(_reflection_path(ctx, "e-001000-g000-train"))
    assert record["status"] == "skipped"
    assert record["reason"] == str(expected)


def test_t06_5_memory_write_failure_degrades(tmp_path, mk_orchestrator, mk_memory,
                                             mk_client):
    """MOD-05 写失败（``E_IO_MEMORY_WRITE_FAILED``）也只降级，不阻塞 run（FR-06-4）。"""
    from gamer_rsi.infra.errors import InfraError

    memory = mk_memory(fail_add=InfraError(E.E_IO_MEMORY_WRITE_FAILED, "disk"))
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=memory,
                                client=mk_client([{"text": "x", "failure_cause": "unknown",
                                                  "tags": []}]), seeds=(1000,))
    summary = orch.run(seeds=[1000])
    assert summary.exit_code == 0
    record = _load(_reflection_path(ctx, "e-001000-g000-train"))
    assert record["status"] == "skipped"
    assert record["reason"] == str(E.E_IO_MEMORY_WRITE_FAILED)
    assert memory.reflections == []


# =====================================================================
# 摘要与模板（FR-06-3：EpisodeSummary 是确定性摘要，不经 LLM）
# =====================================================================

def test_summary_is_deterministic_and_reflect_renders(tmp_path, mk_runner):
    from gamer_rsi.loop.reflect import summarize_episode

    runner, ctx = mk_runner(tmp_path, policy=FakePolicy(), env=FakeEnv(steps_to_terminate=2))
    rec = runner.run_episode(1000, split="train", arm="train", harness_version=0)
    first = summarize_episode(rec, window=[rec])
    second = summarize_episode(rec, window=[rec])
    assert first == second
    assert first.to_dict() == second.to_dict()
    assert first.episode_key == "e-001000-g000-train"
    assert first.stage_count == 6
    assert len(first.recent) == 1


# =====================================================================
# skipped 反思的补偿（loop.retry_reflections_on_resume / --retry-reflections）
# =====================================================================

def test_skipped_reflections_are_retried_on_resume(tmp_path, mk_orchestrator, mk_memory,
                                                   mk_client):
    """``--retry-reflections`` 时 ``--resume`` 补跑此前 skipped 的反思（默认关闭）。"""
    from dataclasses import replace

    from gamer_rsi.loop.state import load_state
    from loop_support import build_orchestrator

    memory = mk_memory()
    client = mk_client([TimeoutError("boom")], default=TimeoutError("boom"))
    orch, ctx = mk_orchestrator(tmp_path, policy=FakePolicy(),
                                env=FakeEnv(steps_to_terminate=2), memory=memory,
                                client=client, seeds=(1000,))
    orch.run(seeds=[1000])
    record = _load(_reflection_path(ctx, "e-001000-g000-train"))
    assert record["status"] == "skipped"

    # 默认关闭：resume 不补跑
    reloaded = load_state(ctx.paths)
    orch2, ctx2 = build_orchestrator(tmp_path, policy=FakePolicy(),
                                     env=FakeEnv(steps_to_terminate=2), memory=memory,
                                     client=mk_client([]), state=reloaded, seeds=(1000,))
    orch2.run(None, resume=True)
    assert _load(_reflection_path(ctx2, "e-001000-g000-train"))["status"] == "skipped"
    assert memory.reflections == []

    # 显式开启：补跑成功（模型这次返回合法 JSON）
    reloaded = load_state(ctx.paths)
    good = mk_client([{"text": "补跑成功。", "failure_cause": "economy",
                       "tags": ["shop"]}])
    orch3, ctx3 = build_orchestrator(tmp_path, policy=FakePolicy(),
                                     env=FakeEnv(steps_to_terminate=2), memory=memory,
                                     client=good, state=reloaded, seeds=(1000,))
    orch3.config = replace(orch3.config, retry_reflections_on_resume=True)
    orch3.runner.config = orch3.config
    ctx3.config = orch3.config
    orch3.run(None, resume=True)
    record = _load(_reflection_path(ctx3, "e-001000-g000-train"))
    assert record["status"] == "ok"
    assert record["entry"]["text"] == "补跑成功。"
    assert len(memory.reflections) == 1
    assert good.n_calls == 1
