"""MOD-07 单测夹具（实现见 ``evolve_support.py``，避免顶层模块名撞名）。"""

from __future__ import annotations

import pytest

from evolve_support import (CONFIG_JSON, DECISION_MD, REFLECTION_MD, RUN_ID, TOOL_PY,
                            FakeArm, FakeRunContext, FakeState, ScriptedArms, StubLLM,
                            context_for, heldout_dict, make_app_config, make_candidate,
                            make_config, make_report, paired_dict, prompt_diff,
                            proposal_payload, tool_diff, write_harness)


@pytest.fixture
def mk_config():
    return make_config


@pytest.fixture
def mk_app_config():
    return make_app_config


@pytest.fixture
def mk_harness():
    return write_harness


@pytest.fixture
def mk_context():
    return context_for


@pytest.fixture
def mk_candidate():
    return make_candidate


@pytest.fixture
def mk_report():
    return make_report


@pytest.fixture
def mk_arm():
    return FakeArm


@pytest.fixture
def mk_llm():
    return StubLLM


@pytest.fixture
def mk_state():
    return FakeState


@pytest.fixture
def mk_run_ctx():
    return FakeRunContext


@pytest.fixture
def helpers():
    class _H:
        write_harness = staticmethod(write_harness)
        context_for = staticmethod(context_for)
        make_config = staticmethod(make_config)
        make_app_config = staticmethod(make_app_config)
        make_candidate = staticmethod(make_candidate)
        make_report = staticmethod(make_report)
        prompt_diff = staticmethod(prompt_diff)
        tool_diff = staticmethod(tool_diff)
        proposal_payload = staticmethod(proposal_payload)
        heldout_dict = staticmethod(heldout_dict)
        paired_dict = staticmethod(paired_dict)
        RUN_ID = RUN_ID
        DECISION_MD = DECISION_MD
        REFLECTION_MD = REFLECTION_MD
        TOOL_PY = TOOL_PY
        CONFIG_JSON = CONFIG_JSON

    return _H


__all__ = ["mk_app_config", "mk_arm", "mk_candidate", "mk_config", "mk_context",
           "mk_harness", "mk_llm", "mk_report", "mk_run_ctx", "mk_state", "helpers",
           "ScriptedArms"]
