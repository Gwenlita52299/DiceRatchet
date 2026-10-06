"""MOD-06 单测夹具（实现见 ``loop_support.py``，避免顶层模块名撞名）。"""

from __future__ import annotations

import pytest

from fakes import FakeEnv, FakeMemory, FakePolicy, ScriptedClient
from loop_support import (REFLECTION_TEMPLATE, RUN_ID, build_orchestrator, build_paths,
                          build_runner, end_row, read_rows, reconfigure, scoreboard_rows,
                          step_rows, write_harness)


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
def helpers():
    class _H:
        build_paths = staticmethod(build_paths)
        build_runner = staticmethod(build_runner)
        build_orchestrator = staticmethod(build_orchestrator)
        write_harness = staticmethod(write_harness)
        read_rows = staticmethod(read_rows)
        step_rows = staticmethod(step_rows)
        end_row = staticmethod(end_row)
        scoreboard_rows = staticmethod(scoreboard_rows)
        reconfigure = staticmethod(reconfigure)
        RUN_ID = RUN_ID
        REFLECTION_TEMPLATE = REFLECTION_TEMPLATE

    return _H


__all__ = ["mk_paths", "mk_runner", "mk_orchestrator", "mk_env", "mk_policy", "mk_memory",
           "mk_client", "helpers"]
