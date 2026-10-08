"""MOD-04 单测夹具：真实 MOD-01 引擎 + 真实 MOD-02 ``SandboxEnv``。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from dice_ratchet.agent import EpisodeContext
from dice_ratchet.env import GameEngine, load_tables
from dice_ratchet.infra.config import PolicyConfig
from dice_ratchet.sandbox.api import EpisodeConfig, SandboxEnv

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def engine() -> GameEngine:
    return GameEngine(load_tables())


@pytest.fixture
def make_env(engine: GameEngine) -> Callable[..., SandboxEnv]:
    """``make_env(seed=1000, **episode_kwargs) -> SandboxEnv``（已 ``reset``）。"""

    def factory(seed: int = 1000, *, reset: bool = True, **kwargs: Any) -> SandboxEnv:
        env = SandboxEnv(engine=engine, episode_config=EpisodeConfig.from_any(kwargs or None))
        if reset:
            env.reset(seed, episode_id=f"e-{seed:06d}-g000-train")
        return env

    return factory


@pytest.fixture
def cfg() -> PolicyConfig:
    return PolicyConfig()


@pytest.fixture
def ctx() -> EpisodeContext:
    return EpisodeContext(run="r-test", episode="e-001000-g000-train", seed=1000,
                          split="train", mode="train", tick=1)


@pytest.fixture(scope="session")
def harness_dir() -> Path:
    return REPO_ROOT / "harness"


@pytest.fixture
def make_harness_dir(tmp_path: Path) -> Callable[..., Path]:
    """``make_harness_dir(name, {"tool.py": source}) -> Path``（含 ``tools/`` 子目录）。"""

    def factory(name: str, files: dict[str, str]) -> Path:
        root = tmp_path / name
        tools = root / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        for filename, body in files.items():
            (tools / filename).write_text(body, encoding="utf-8")
        (root / "version.json").write_text('{"version": 1}', encoding="utf-8")
        return root

    return factory
