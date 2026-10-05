"""FR-04-3 + M1 验收 F13：``legal_actions`` 压缩与「任何 bundle 都装得进预算」。

真实场景（seed 1000 的一个 ``phase=forge`` 决策点）：合法动作是笛卡尔积
（23 张骰 × 6 个面 × 5 个候选 = 690 条 ``forge_pick``），完整枚举 54,393 字符
≈ 19,633 token，而 ``policy.prompt_token_budget=3072``、观测本身只有 2,798 字符。
修复前 ``assemble_prompt`` 只能返回 ``E_ACTION_BUDGET`` → 该决策点被启发式接管。
"""

from __future__ import annotations

from pathlib import Path

from gamer_rsi.agent.policy import (_LEGAL_COMPACT_SAMPLES, _render_legal,
                                    _render_legal_compact, _spread)
from gamer_rsi.agent.prompts.assembler import (DEFAULT_TEMPLATE_PATH, load_template,
                                               PromptContext, assemble_prompt,
                                               estimate_tokens)


def _forge_legal() -> list[dict]:
    return [{"kind": "forge_pick", "dieId": f"d-{die:04d}", "faceIndex": face,
             "candidateId": candidate}
            for die in range(1, 24) for face in range(6)
            for candidate in ("ff_ward4", "ff_purge3", "ff_poison3", "ff_purge5", "ff_weak2")
            ] + [{"kind": "shop_leave"}]


def _ctx(**overrides) -> PromptContext:
    legal = _forge_legal()
    base = dict(
        observation="phase=forge stage=6/6 decision=83\nforge: forgeable=[d-0001]",
        task="TASK-TEXT",
        harness_version=0,
        legal_actions=_render_legal(legal),
        legal_actions_compact=_render_legal_compact(legal),
        token_budget=3072,
    )
    base.update(overrides)
    return PromptContext(**base)


def test_compact_render_is_much_smaller_but_keeps_every_kind_and_field():
    legal = _forge_legal()
    full = _render_legal(legal)
    compact = _render_legal_compact(legal)
    assert len(full) > 50_000 and len(compact) < 4_000
    assert "forge_pick" in compact and "shop_leave" in compact
    assert "dieId∈[" in compact and "faceIndex∈[" in compact and "candidateId∈[" in compact
    # 等距抽样：每种 kind 都给出可直接照抄的**真实合法**样例
    assert compact.count("例：") == _LEGAL_COMPACT_SAMPLES + 1


def test_compact_render_is_deterministic_and_samples_the_whole_space():
    legal = _forge_legal()
    assert _render_legal_compact(legal) == _render_legal_compact(legal)
    lines = _render_legal_compact(legal).splitlines()
    first_group = lines[:lines.index(next(line for line in lines if "shop_leave" in line))]
    picks = [line for line in first_group if line.startswith("  例：")]
    assert len(picks) == _LEGAL_COMPACT_SAMPLES
    # 等距抽样覆盖整个取值空间（不是只给前 12 条：最大骰子 id 必须远超前 12 条的范围）
    assert "d-0001" in picks[0]
    assert any(f"d-{die:04d}" in picks[-1] for die in range(18, 24))


def test_spread_is_deterministic_and_bounded():
    items = list(range(100))
    assert _spread(items, 0) == []
    assert _spread(items, 200) == items
    picked = _spread(items, 10)
    assert picked == _spread(items, 10)
    assert len(picked) == 10 and picked[0] == 0 and picked[-1] >= 90


def test_oversize_legal_actions_are_compressed_instead_of_overflowing():
    """F13 的回归点：修复前这里返回 ``E_ACTION_BUDGET``（→ 启发式接管决策点）。"""
    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx()
    assert estimate_tokens(ctx.legal_actions, "chars_div_3") > ctx.token_budget
    bundle = assemble_prompt(spec, ctx)
    assert bundle.error_codes == ()
    assert bundle.token_estimate <= ctx.token_budget
    assert any(record.kind == "legal_actions" for record in bundle.truncated)
    # 观测段永不裁剪（FR-04-3）
    assert ctx.observation in bundle.text


def test_observation_only_overflow_still_fails_closed():
    """压缩 legal 也装不下（观测单独超限）→ 仍然 ``E_ACTION_BUDGET``（保守，不静默丢观测）。"""
    from gamer_rsi.infra.errors import E

    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx(observation="x" * 100_000, legal_actions="", legal_actions_compact="")
    bundle = assemble_prompt(spec, ctx)
    assert E.E_ACTION_BUDGET in bundle.error_codes
    assert ctx.observation in bundle.text


def test_no_compact_form_keeps_the_old_behaviour():
    from gamer_rsi.infra.errors import E

    spec = load_template(DEFAULT_TEMPLATE_PATH)
    ctx = _ctx(legal_actions_compact="")
    bundle = assemble_prompt(spec, ctx)
    assert E.E_ACTION_BUDGET in bundle.error_codes


def test_memory_and_reflections_are_dropped_before_compressing_legal_actions():
    """优先级不变：先用完整 legal 丢 memory/reflections；只有仍超预算才压缩 legal。"""
    from gamer_rsi.agent.prompts.assembler import MemoryItem

    spec = load_template(DEFAULT_TEMPLATE_PATH)
    big = MemoryItem("m-1", "y" * 9_000, 0.9)
    # legal 很小 → 丢光 memory 就装得下，不该动 legal 清单（既有 golden 的路径）
    ctx = _ctx(memory=(big,), legal_actions='{"kind":"shop_leave"}')
    bundle = assemble_prompt(spec, ctx)
    assert bundle.memory_ids == ()
    assert any(record.kind == "memory" for record in bundle.truncated)
    assert not any(record.kind == "legal_actions" for record in bundle.truncated)
    assert bundle.error_codes == ()

    # legal 巨大 + 巨 memory → 取「更省」的组合：压缩 legal 并重新按优先级丢 memory
    ctx2 = _ctx(memory=(big,))
    bundle2 = assemble_prompt(spec, ctx2)
    assert bundle2.token_estimate <= ctx2.token_budget
    assert any(record.kind == "legal_actions" for record in bundle2.truncated)
    assert bundle2.error_codes == ()


def test_real_episode_prompts_never_exceed_the_budget(make_env, cfg, ctx):
    """真环境上的不变式：任何决策点的 ``token_estimate`` 都不得超过 ``prompt_token_budget``。"""
    from gamer_rsi.agent import PolicyAgent
    from gamer_rsi.agent.heuristic import heuristic_policy

    agent = PolicyAgent(cfg, harness_dir=Path("harness"))
    env = make_env(seed=1000)
    ctx = ctx.__class__(run="r-f13", episode="e-001000-g000-train", seed=1000,
                        split="train", mode="train", tick=1)
    agent.reset(ctx)
    obs = env.reset(1000, episode_id=ctx.episode)
    oversize = 0
    checked = 0
    while not obs.get("outcome"):
        legal = env.legal_actions()
        if not legal:
            break
        bundle = agent.prompt_bundle(obs, ctx=ctx, legal=legal)
        checked += 1
        if bundle.token_estimate > cfg.prompt_token_budget:
            oversize += 1
        step = env.step(heuristic_policy(obs, legal, env.hints()))
        obs = step.observation
        if step.terminated or step.truncated:
            break
    assert checked > 50, f"决策点太少（{checked}），用例可能没真的跑到 forge 阶段"
    assert oversize == 0
