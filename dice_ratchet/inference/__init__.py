"""MOD-03 inference：本地推理后端抽象 + 结构化输出（PRD 见 docs/prd/MOD-03-inference-PRD.md）。

对外唯一入口面（MOD-04/06/08 只用这里导出的名字）::

    from dice_ratchet.inference import BackendManager, Message, DECISION_SCHEMA

- ``BackendManager`` / ``Backend``：加载生命周期、并发限流、内存预算、三级 JSON 重试；
- ``Message`` / ``Usage`` / ``GenerationResult`` 等数据模型；
- ``DECISION_SCHEMA`` / ``compile_grammar`` / ``repair_messages``（``schema.py`` 独占拥有）；
- ``InferenceError``（``E_LLM_*`` 错误码，Spec §8.3）。
"""

from __future__ import annotations

from .backend import (Backend, BackendHealth, BackendManager, GenerationResult, InfraBridge,
                      InfraFacade, LocalBackend, Message, MockBackend, NullInfra, Usage,
                      estimate_prompt_tokens, execute_json_call, make_result,
                      validate_sampling)
from .errors import InferenceError
from .records import CallContext, InferenceCallRecord, prompt_digest
from .registry import (BackendName, ModelRegistry, ModelSpec, estimate_required_gb,
                       load_registry)
from .schema import (DECISION_SCHEMA_ID, compile_grammar, decision_schema,
                     load_decision_schema, repair_messages, validate)

__all__ = [
    # 契约与门面
    "Backend", "BackendManager", "LocalBackend", "MockBackend",
    # 数据模型
    "Message", "Usage", "BackendHealth", "GenerationResult", "ModelSpec", "ModelRegistry",
    "BackendName", "InferenceCallRecord", "CallContext",
    # 基础设施与工具
    "InfraFacade", "InfraBridge", "NullInfra", "InferenceError", "estimate_required_gb",
    "estimate_prompt_tokens", "prompt_digest", "validate_sampling", "make_result",
    "execute_json_call", "load_registry",
    # schema（独占拥有）
    "DECISION_SCHEMA", "DECISION_SCHEMA_ID", "decision_schema", "load_decision_schema",
    "compile_grammar", "repair_messages", "validate",
]


def __getattr__(name: str): # pragma: no cover - 惰性转发，避免 import 期读盘
    """``from dice_ratchet.inference import DECISION_SCHEMA`` 的惰性入口。"""
    if name == "DECISION_SCHEMA":
        return decision_schema()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
