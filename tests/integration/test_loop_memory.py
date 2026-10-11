"""MOD-06 ↔ MOD-05 集成：训练局的反思真正落到记忆库，评估局只读（MOD-05 PRD §11）。

本用例用**真实** ``MemoryStore``（写在 ``tmp_path`` 的 run 目录里）+ 假 env/policy/LLM，
验证跨模块契约而不是两侧各自的实现：

- 训练局局末 → ``record_usage`` 更新引用/采纳计数 → ``add_reflection`` 落库 →
  下一局 ``retrieve`` 能取回它（闭环的「反思 → 记忆 → 下一局」这一段真的通了）；
- 评估局用 ``memory.frozen(v)`` 只读视图：既不能写库，也不写 scoreboard 主台账（INV-5）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakes import FakeEnv, FakePolicy, ScriptedClient
from dice_ratchet.evals.seeds import coerce_protocol, validate_protocol

_PROTO = validate_protocol(coerce_protocol(None))
_HELDOUT_SEED = _PROTO.splits["heldout"].baskets["H1"].start + 1

from dice_ratchet.infra.config import MemoryConfig
from dice_ratchet.loop.generation import LoopOrchestrator
from dice_ratchet.loop.state import Scoreboard, scoreboard_path
from loop_support import REPO_ROOT, build_paths, write_harness

_TRAIN_SEED = 1042
_LESSON = "净化值只剩 3 时继续硬吃伤害，应先用手牌清场再进战斗。"


@pytest.fixture
def memory_store(tmp_path):
    """真实 ``MemoryStore``：run 目录在 ``tmp_path``，seed 协议读仓库的 configs/。"""
    pytest.importorskip("dice_ratchet.memory")
    from dice_ratchet.memory import MemoryStore

    paths = build_paths(tmp_path, "r-loop-mem")
    return MemoryStore(paths.root, MemoryConfig(), config_dir=REPO_ROOT / "configs",
                       run_id="r-loop-mem"), paths


def _reflection_llm():
    return ScriptedClient([{"text": _LESSON, "failure_cause": "virtue_management",
                            "tags": ["low_virtue", "defense"]}])


def test_train_episode_writes_reflection_into_memory_store(tmp_path, memory_store):
    """训练局 → 反思 → MOD-05 落库 → 可检索（FR-06-3 + Spec §4.7）。"""
    store, paths = memory_store
    from dice_ratchet.loop.runner import EpisodeRunner, RunContext
    from dice_ratchet.loop.trajectory import TrajectoryWriter
    from dice_ratchet.sandbox.api import EpisodeConfig

    write_harness(tmp_path, version=0)
    cfg = _loop_cfg(reflect=True)
    runner, run_ctx = _wire(paths, tmp_path, cfg, store, client=_reflection_llm())
    orch = LoopOrchestrator(run_ctx=run_ctx, config=cfg, runner=runner,
                            evolve_fn=None,
                            scoreboard=Scoreboard(scoreboard_path(paths)))

    summary = orch.run(seeds=[_TRAIN_SEED])
    assert summary.exit_code == 0
    assert summary.phase == "done"

    # ---- MOD-05 侧：反思已入库 -------------------------------------------
    entries = store.entries()
    lessons = [e.lesson for e in entries]
    assert any(_LESSON[:12] in lesson for lesson in lessons), (
        f"反思没有落到记忆库：{lessons}")

    # ---- 下一局可以检索到它（闭环的最后一跳）------------------------------
    from dice_ratchet.sandbox.api import make_default_env

    env = make_default_env(episode_config=EpisodeConfig(difficulty="easy", stage_count=3),
                           episode_id="probe")
    try:
        obs = env.reset(_TRAIN_SEED)
        result = store.retrieve(obs, top_k=5, token_budget=900, tick=1)
    finally:
        env.close()
    hit_ids = [getattr(h, "entry_id", getattr(h, "memory_id", None)) for h in
               (getattr(result, "items", result) or [])]
    assert hit_ids, "训练局写入的反思在下一局检索不到"

    # ---- 用量回写：把该条目当成本局用过的记忆，局末 record_usage 必须回写 -------
    entry_id = entries[0].entry_id
    runner2, _ctx2 = _wire(paths, tmp_path, cfg, store, client=_reflection_llm(),
                           memory_ids=[entry_id], adopted=[entry_id])
    runner2.run_episode(_TRAIN_SEED + 1, split="train", arm="train", harness_version=0)
    updated = store.get(entry_id)
    assert updated is not None
    assert updated.n_ref == 1, "FR-06-3：局末必须回写本局的记忆使用情况"
    assert updated.n_adopted == 1

    record = json.loads((Path(paths.reflections) /
                         f"e-{_TRAIN_SEED:06d}-g000-train.json").read_text(encoding="utf-8"))
    assert record["status"] == "ok"


def test_eval_episode_uses_frozen_view_and_cannot_write(tmp_path, memory_store):
    """评估局只读：``frozen(v)`` 视图不出现在库写入路径上，也不写主台账（INV-5）。"""
    store, paths = memory_store
    store.snapshot(1)                       # 评估局需要 memory_v1.jsonl
    cfg = _loop_cfg(reflect=False)
    runner, run_ctx = _wire(paths, tmp_path, cfg, store, client=_reflection_llm(),
                            memory_version=1)

    rec = runner.run_episode(_HELDOUT_SEED, split="heldout", arm="parent",
                             harness_version=0)

    assert rec.episode_key == f"e-{_HELDOUT_SEED:06d}-g000-parent"
    assert store.entries() == (), "评估局不得写记忆库（frozen 只读视图）"
    # 轨迹写到 heldout/，主台账（训练计分）保持空
    assert (Path(paths.episodes) / "heldout"
            / f"e-{_HELDOUT_SEED:06d}-g000-parent.jsonl").is_file()
    assert not scoreboard_path(paths).is_file()


def test_reflection_scan_blocks_heldout_seed_text(tmp_path, memory_store):
    """反思文本里带 held-out seed 字面量 → MOD-05 隔离拦截，绝不入 prompt/memory（INV-5）。"""
    store, paths = memory_store
    cfg = _loop_cfg(reflect=True)
    leaky = ScriptedClient([{"text": f"下次别再用 seed {_HELDOUT_SEED} 的打法。",
                             "failure_cause": "unknown", "tags": ["combat"]}])
    runner, run_ctx = _wire(paths, tmp_path, cfg, store, client=leaky)
    orch = LoopOrchestrator(run_ctx=run_ctx, config=cfg, runner=runner,
                            evolve_fn=None,
                            scoreboard=Scoreboard(scoreboard_path(paths)))
    summary = orch.run(seeds=[_TRAIN_SEED])
    assert summary.exit_code == 0, "泄漏被拦截是降级，不是 run 失败"
    for entry in store.entries():
        assert str(_HELDOUT_SEED) not in entry.lesson


# =====================================================================
# 装配助手
# =====================================================================

def _loop_cfg(*, reflect: bool):
    from dice_ratchet.infra.config import LoopConfig

    return LoopConfig(k=99, reflect_enabled=reflect, reflect_timeout_s=5.0,
                      reflect_context_window=3, heldout_n=4, smoke_n=2, n_candidates=1)


def _wire(paths, tmp_path: Path, cfg, store, *, client, memory_version: int = 0,
          memory_ids: list[str] | None = None, adopted: list[str] | None = None):
    from dice_ratchet.loop.runner import EpisodeRunner, RunContext
    from dice_ratchet.loop.state import init_state
    from dice_ratchet.loop.trajectory import TrajectoryWriter
    from dice_ratchet.sandbox.api import EpisodeConfig

    write_harness(tmp_path, version=0)
    state = init_state("r-loop-mem", k=int(cfg.k), seeds=[_TRAIN_SEED],
                       harness_version=0)
    state.memory_version = int(memory_version)
    run_ctx = RunContext(run_id="r-loop-mem", paths=paths, config=cfg, state=state,
                         episode_config=EpisodeConfig(difficulty="easy", stage_count=3),
                         harness_dir=Path(tmp_path) / "harness",
                         memory_version=int(memory_version), memory=store, client=client)
    runner = EpisodeRunner(env=FakeEnv(steps_to_terminate=3, outcome="lose"),
                           policy=FakePolicy(memory_ids=memory_ids or [],
                                             adopted=adopted or []),
                           memory=store, writer=TrajectoryWriter(paths), config=cfg,
                           run_ctx=run_ctx, client=client)
    return runner, run_ctx
