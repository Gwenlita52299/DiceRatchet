"""MOD-06 单测共享工具：在 ``tmp_path`` 里搭 run 目录树 + 装配 ``EpisodeRunner``。

放在 ``tests/unit/loop/loop_support.py``（不用 ``conftest.py`` 之外的通用名），避免与
``tests/unit/*/…`` 下的同名模块在 pytest 的 ``prepend`` 导入模式下撞名。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pytest

from fakes import FakeMemory, FakePolicy, ScriptedClient
from dice_ratchet.infra.config import LoopConfig
from dice_ratchet.infra.run import RunPaths
from dice_ratchet.loop.generation import LoopOrchestrator
from dice_ratchet.loop.runner import EpisodeRunner, RunContext
from dice_ratchet.loop.state import RunState, Scoreboard, init_state, scoreboard_path
from dice_ratchet.loop.trajectory import TrajectoryWriter

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "r-loop-test"
SUBDIRS = ("episodes/train", "episodes/heldout", "episodes/test", "episodes/demo",
           "reflections", "generations", "metrics", "audit", "prompts", "memory",
           "locks", "plots")

REFLECTION_TEMPLATE = """# 反思（测试模板）
episode_key: {{episode_key}}
outcome: {{outcome}} truncation_reason: {{truncation_reason}}
stages: {{stages_cleared}}/{{stage_count}} hearts: {{hearts_left}} battles: {{battles_won}}
decisions: {{decisions}} illegal: {{illegal_attempts}} fallback: {{fallback_decisions}}
reward: {{cum_reward}}
recent:
{{recent}}
"""


def build_paths(root: Path, run_id: str = RUN_ID) -> RunPaths:
    """在 ``root`` 下建出 Spec §5 的目录树（不写 config.json，单测不需要 git 检查）。"""
    base = Path(root) / run_id
    for sub in SUBDIRS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    return RunPaths(base)


def write_harness(root: Path, *, version: int = 0,
                  template: str = REFLECTION_TEMPLATE,
                  with_template: bool = True) -> Path:
    """建出最小 ``harness/``（``version.json`` + ``prompts/reflection.md``）。"""
    harness = Path(root) / "harness"
    (harness / "prompts").mkdir(parents=True, exist_ok=True)
    (harness / "version.json").write_text(json.dumps(
        {"version": int(version), "parent": None, "git_commit": "",
         "artifact_hash": "", "created_at": "2026-10-06T00:00:00Z",
         "reason": "test", "accepted_by": "human", "base_model": None}),
        encoding="utf-8")
    if with_template:
        (harness / "prompts" / "reflection.md").write_text(template, encoding="utf-8")
    return harness


def build_runner(root: Path, *, policy: Any = None, env: Any = None,
                 memory: Any = None, loop_cfg: LoopConfig | None = None,
                 state: RunState | None = None, run_id: str = RUN_ID,
                 harness_version: int = 0, client: Any = None,
                 seeds: Sequence[int] = (1000, 1001, 1002),
                 template: str = REFLECTION_TEMPLATE,
                 with_template: bool = True, memory_version: int = 0,
                 app_config: Any = None) -> tuple[EpisodeRunner, RunContext]:
    """装配一个把轨迹写进 ``tmp_path`` 的 ``EpisodeRunner``。"""
    from dice_ratchet.sandbox.api import EpisodeConfig

    paths = build_paths(root, run_id)
    harness = write_harness(root, version=harness_version, template=template,
                            with_template=with_template)
    cfg = loop_cfg or LoopConfig(k=3, reflect_timeout_s=5.0, reflect_context_window=3,
                                 heldout_n=4, smoke_n=2, n_candidates=1)
    run_state = state or init_state(run_id, k=int(cfg.k), seeds=seeds,
                                    harness_version=int(harness_version))
    run_ctx = RunContext(run_id=run_id, paths=paths, config=cfg, state=run_state,
                         app_config=app_config, episode_config=EpisodeConfig(),
                         harness_dir=harness, memory_version=int(memory_version),
                         memory=memory, client=client)
    runner = EpisodeRunner(env=env, policy=policy, memory=memory,
                           writer=TrajectoryWriter(paths), config=cfg, run_ctx=run_ctx,
                           client=client)
    return runner, run_ctx


def build_orchestrator(root: Path, *, policy: Any = None, env: Any = None,
                       memory: Any = None, loop_cfg: LoopConfig | None = None,
                       evolve_fn: Any = None, client: Any = None,
                       seeds: Sequence[int] = (1000, 1001, 1002),
                       run_id: str = RUN_ID, harness_version: int = 0,
                       template: str = REFLECTION_TEMPLATE,
                       with_template: bool = True, state: RunState | None = None,
                       memory_version: int = 0,
                       app_config: Any = None) -> tuple[LoopOrchestrator, RunContext]:
    """装配 ``LoopOrchestrator``（含 runner 与 scoreboard）。

    ``state`` 非空时复用它（``--resume`` 场景：状态来自 ``load_state``）。
    """
    cfg = loop_cfg or LoopConfig(k=3, reflect_timeout_s=5.0, reflect_context_window=3,
                                 heldout_n=4, smoke_n=2, n_candidates=1)
    runner, run_ctx = build_runner(root, policy=policy, env=env, memory=memory,
                                   loop_cfg=cfg, run_id=run_id,
                                   harness_version=harness_version, client=client,
                                   seeds=seeds, template=template,
                                   with_template=with_template, state=state,
                                   memory_version=memory_version,
                                   app_config=app_config)
    orch = LoopOrchestrator(run_ctx=run_ctx, config=cfg, runner=runner,
                            evolve_fn=evolve_fn,
                            scoreboard=Scoreboard(scoreboard_path(run_ctx.paths)))
    return orch, run_ctx


def reconfigure(orch: Any, run_ctx: RunContext, **overrides: Any) -> Any:
    """把 ``LoopConfig`` 的覆盖同时写到 orchestrator / runner / run_ctx。

    ``maybe_run_generation`` 读 ``run_ctx.config``，``LoopOrchestrator._loop`` 读
    ``self.config``，两者必须是同一份（生产代码里它们本来就同源）。
    """
    cfg = replace(orch.config, **overrides)
    orch.config = cfg
    orch.runner.config = cfg
    run_ctx.config = cfg
    run_ctx.state.k = int(cfg.k)
    return cfg


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def step_rows(path: Path) -> list[dict[str, Any]]:
    return [row for row in read_rows(path) if row.get("type") != "episode_end"]


def end_row(path: Path) -> dict[str, Any]:
    rows = [row for row in read_rows(path) if row.get("type") == "episode_end"]
    assert len(rows) == 1, f"episode_end 行应恰好 1 条，实际 {len(rows)}"
    return rows[0]


def scoreboard_rows(run_ctx: RunContext) -> list[dict[str, Any]]:
    path = scoreboard_path(run_ctx.paths)
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["episodes"]


# ---- pytest 夹具 -----------------------------------------------------------------

@pytest.fixture
def mk_paths():
    return build_paths


@pytest.fixture
def mk_runner():
    return build_runner


@pytest.fixture
def mk_orchestrator():
    return build_orchestrator


@pytest.fixture
def mk_env():
    from fakes import FakeEnv

    return FakeEnv


@pytest.fixture
def mk_policy():
    return FakePolicy


@pytest.fixture
def mk_memory():
    return FakeMemory


@pytest.fixture
def mk_client():
    return ScriptedClient


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


__all__ = ["REFLECTION_TEMPLATE", "RUN_ID", "build_orchestrator", "build_paths",
           "build_runner", "end_row", "read_rows", "reconfigure", "scoreboard_rows",
           "step_rows", "write_harness"]
