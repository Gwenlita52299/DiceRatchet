"""格式鲁棒性集成：200 条固定 prompt（PRD §11 ``test_inference_robustness.py`` / M1 交付物）。

用 ``MockBackend`` 注入「真实会出现的三种情况」：首答即过、修复一次后过、三次耗尽。
断言 M1 目标：首答通过率 ≥ 90%、三级全失败率 ≤ 1%，且 ``format_robustness.json`` 通过。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dice_ratchet.infra.io import write_json_atomic
from dice_ratchet.inference.backend import Message
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.metrics import robustness_report
from dice_ratchet.inference.prompts import load_robustness_prompts, probe_messages
from dice_ratchet.inference.records import CallContext
from dice_ratchet.inference.schema import DECISION_SCHEMA

from inference_support import BAD_KIND, GOOD, MODEL_ID, mock_factory, registry_of

N_PROMPTS = 200
RETRY_COUNT = 12
FAIL_COUNT = 2


def _script(n: int = N_PROMPTS) -> list[str]:
    first_pass = n - RETRY_COUNT - FAIL_COUNT
    script = [GOOD] * first_pass
    script += [BAD_KIND, GOOD] * RETRY_COUNT
    script += [BAD_KIND] * (3 * FAIL_COUNT)
    assert len(script) == first_pass + 2 * RETRY_COUNT + 3 * FAIL_COUNT
    return script


def test_robustness_meets_m1_thresholds(manager_builder, tmp_path: Path):
    factory, created = mock_factory(_script())
    records = []
    manager = manager_builder(registry_of(), factory=factory, sink=records.append)
    prompts, source = load_robustness_prompts(N_PROMPTS)
    # M1 验收 F2：必须用真实 MOD-04 快照跑批，而不是合成兜底
    assert len(prompts) == N_PROMPTS and source.startswith("file:")

    errors: list[dict] = []

    async def run():
        try:
            for index, messages in enumerate(prompts):
                try:
                    await manager.generate_json(
                        MODEL_ID, messages, DECISION_SCHEMA,
                        call_context=CallContext(purpose="robustness", step=index),
                        temperature=0.0, top_p=1.0, top_k=0, seed=index + 1)
                except InferenceError as exc:
                    errors.append({**exc.as_dict(), "step": index})
        finally:
            await manager.unload_all()

    asyncio.run(run())

    assert len(errors) == FAIL_COUNT
    assert len(records) == (N_PROMPTS - RETRY_COUNT - FAIL_COUNT) + 2 * RETRY_COUNT \
        + 3 * FAIL_COUNT

    report = robustness_report(records, model_id=MODEL_ID, backend="mock", mode="train",
                               n_prompts=N_PROMPTS, prompt_source=source, errors=errors)
    overall = report["overall"]
    assert overall["n_calls"] == N_PROMPTS
    assert overall["first_pass_valid_rate"] >= 0.90
    assert overall["schema_fail_rate"] <= 0.01
    assert overall["retry_histogram"] == {"1": 186, "2": 12, "3": 2}
    assert report["passed"] is True

    path = tmp_path / "format_robustness.json"
    write_json_atomic(path, report)
    written = path.read_text(encoding="utf-8")
    assert "first_pass_valid_rate" in written and '"passed": true' in written


def test_robustness_prompts_are_deterministic():
    first, source_a = load_robustness_prompts(20)
    second, source_b = load_robustness_prompts(20)
    assert source_a == source_b
    assert [[(m.role, m.content) for m in p] for p in first] == \
        [[(m.role, m.content) for m in p] for p in second]
    digests = {hash(tuple((m.role, m.content) for m in p)) for p in first}
    assert len(digests) == 20 # 每条 prompt 互不相同（prompt_digest 才有区分度）


def test_robustness_fails_when_thresholds_are_missed(manager_builder):
    """反例：首答通过率 50% → report.passed 必须为 False（门禁不是空转）。"""
    factory, _ = mock_factory([BAD_KIND, GOOD] * 4)
    records: list = []
    manager = manager_builder(registry_of(), factory=factory, sink=records.append)
    messages = probe_messages()

    async def run():
        for _ in range(4):
            await manager.generate_json(MODEL_ID, messages, DECISION_SCHEMA,
                                        temperature=0.0, seed=1)
        await manager.unload_all()

    asyncio.run(run())
    report = robustness_report(records, model_id=MODEL_ID, backend="mock", mode="train",
                               n_prompts=4, prompt_source="synthetic:test")
    assert report["overall"]["first_pass_valid_rate"] == 0.0
    assert report["passed"] is False


def test_prompts_come_from_the_real_mod04_snapshot_and_are_role_shaped():
    """M1 验收 F2：默认 prompt 源必须是 MOD-04 决策模板快照，不再退化为合成兜底。

    快照由 ``tests/golden/inference/regen_robustness_prompts.py`` 从真实 ``SandboxEnv``
    观测 + ``PolicyAgent.prompt_bundle()`` 生成，因此每条是**单轮 user** 消息（模板自带
    系统指令，无独立 system turn）。
    """
    prompts, source = load_robustness_prompts(3)
    assert source.startswith("file:"), f"prompt 源退化为 {source!r}（快照缺失？）"
    assert "synthetic" not in source
    for messages in prompts:
        assert all(isinstance(m, Message) for m in messages)
        assert {m.role for m in messages} <= {"system", "user", "assistant"}
        assert messages[-1].role == "user" and messages[-1].content.strip()


@pytest.mark.slow
def test_real_model_robustness_when_weights_present():
    """有真实权重时跑 200 条（M1 联调）；无权重/依赖时跳过（CI 用 mock 路径）。"""
    model_dir = Path("models/Qwen3-8B-4bit")
    if not model_dir.exists():
        pytest.skip("未落盘 models/Qwen3-8B-4bit（离线权重），跳过真实模型跑批")
    pytest.importorskip("mlx_lm")
    from dice_ratchet.inference.backend import BackendManager

    manager = BackendManager("configs/models.yaml", mode="eval",
                             config=None) # 真实跑批由 `inference robustness` 承担
    assert manager.registry.spec("qwen3-8b-q4").supports_grammar is False


def test_fixed_v1_snapshot_is_the_frozen_baseline_template():
    """M1 验收 F21：冻结基线模板的 prompt 快照必须与主快照同源、可对照。

    ``robustness_prompts_fixed_v1.jsonl`` 与 ``robustness_prompts.jsonl`` 来自**同一批真实
    观测**（同 seed 区间、同 ``PolicyAgent.prompt_bundle()`` 出口），只换模板 —— 因此两份
    ``format_robustness.json`` 的「首答通过率」之差可以干净地归因到**模板契约强度**
    （修复前 ``fixed_v1.md`` 52% vs ``decision.md`` 94%，F21）。
    """
    import json

    repo = Path(__file__).resolve().parents[2]
    path = repo / "tests" / "fixtures" / "inference" / "robustness_prompts_fixed_v1.jsonl"
    assert path.is_file(), f"缺 F21 的对照快照：{path}"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == N_PROMPTS
    assert len({row["prompt_hash"] for row in rows}) == N_PROMPTS
    assert len({row["template_hash"] for row in rows}) == 1, "必须全部来自同一份冻结模板"
    for row in rows[:5]:
        messages = row["messages"]
        assert [m["role"] for m in messages] == ["user"]
        assert "fixed-prompt baseline v1" in messages[0]["content"]
        assert "输出契约" in messages[0]["content"], "F21：冻结模板必须带同等明确度的输出契约"
