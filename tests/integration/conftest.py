"""MOD-03 集成测试夹具（共享构造器见 ``inference_support.py``）。"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from dice_ratchet.inference.backend import Backend

from inference_support import FakeClock, build_manager, inference_config


@pytest.fixture
def manager_builder():
    """``build(registry, *, factory, mode, config, clock, sink) -> BackendManager``。"""

    def _build(registry, *, factory: Callable[[Any], Backend] | None = None,
               mode: str = "train", config=None, clock: FakeClock | None = None,
               sink=None):
        return build_manager(registry, factory=factory, mode=mode, config=config,
                             clock=clock, sink=sink)

    return _build


__all__ = ["manager_builder", "inference_config"]
