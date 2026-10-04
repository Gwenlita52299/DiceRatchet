"""MOD-08 单测的 pytest 夹具（辅助实现在 ``evals_support.py``，避免顶层撞名）。"""

from __future__ import annotations

import pytest

from evals_support import FakeRunner, make_result


@pytest.fixture
def mk_result():
    return make_result


@pytest.fixture
def fake_runner():
    return FakeRunner
