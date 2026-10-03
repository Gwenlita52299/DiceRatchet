"""MOD-03 单测夹具：可控时钟、临时注册表、由 ``MockBackend`` 驱动的 ``BackendManager``。

共享常量与构造器在 ``support.py``（独立模块名，避免多目录 ``conftest`` 同名冲突）。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import pytest

from gamer_rsi.infra.config import InferenceConfig
from gamer_rsi.inference.backend import BackendManager, MockBackend
from gamer_rsi.inference.records import InferenceCallRecord
from gamer_rsi.inference.registry import ModelRegistry, build_registry

from support import GOOD, FakeClock, inference_config, registry_data


@pytest.fixture
def registry_factory() -> Callable[..., ModelRegistry]:
    def _make(**kwargs: Any) -> ModelRegistry:
        return build_registry(registry_data(**kwargs))
    return _make


@pytest.fixture
def manager_factory(registry_factory: Callable[..., ModelRegistry]):
    """``make(script, ...) -> (manager, records, backends)``。"""

    def _make(script: Sequence[Any] = (GOOD,), *, model_id: str = "mock-a",
              mode: str = "train", config: InferenceConfig | None = None, delay_ms: int = 0,
              clock: FakeClock | None = None, registry: ModelRegistry | None = None,
              **registry_kwargs: Any):
        records: list[InferenceCallRecord] = []
        reg = registry if registry is not None else registry_factory(model_id=model_id,
                                                                    **registry_kwargs)
        created: dict[str, MockBackend] = {}
        texts = [script] if isinstance(script, str) else list(script)

        def factory(spec):
            backend = MockBackend(texts, delay_ms=delay_ms, model_id=spec.model_id)
            created[spec.model_id] = backend
            return backend

        cfg = config if config is not None else inference_config(
            mode=mode, seed=(1042 if mode == "eval" else None))
        manager = BackendManager(
            registry=reg, mode=mode, config=cfg, backend_factory=factory,
            clock=clock.now if clock else None, sleep=clock.sleep if clock else None,
            record_sink=records.append)
        return manager, records, created

    return _make
