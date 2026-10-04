"""MOD-04 单测共享工具：可脚本化假 ``InferenceClient`` 与常用动作构造。

放在独立模块（不用 ``support.py`` 这个通用名）以避免与 ``tests/unit/inference/support.py``
在 pytest 的 ``prepend`` 导入模式下重名。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = ["FakeUsage", "FakeResponse", "ScriptedClient", "legal_json", "plan"]


@dataclass
class FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    latency_ms: int = 7
    ttft_ms: int | None = None
    prefill_ms: int | None = None
    decode_ms: int | None = None


@dataclass
class FakeResponse:
    """MOD-03 ``GenerationResult`` 的最小鸭子类型。"""

    text: str
    parsed: Mapping[str, Any] | None = None
    usage: Any = field(default_factory=FakeUsage)
    attempts: int = 1
    prompt_digest: str = "sha256:" + "0" * 64
    model_id: str = "fake-8b"
    backend: str = "mock"


class ScriptedClient:
    """按脚本逐次返回文本 / 抛异常；记录每次调用的 messages 与采样参数。"""

    def __init__(self, script: Sequence[str | BaseException] = (), *,
                 responder: Callable[[int, Sequence[Any]], str] | None = None) -> None:
        self.script = list(script)
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], *,
                      mode: str, sampling: Mapping[str, Any], seed: int) -> FakeResponse:
        index = len(self.calls)
        self.calls.append({"messages": list(messages), "schema": schema, "mode": mode,
                           "sampling": dict(sampling), "seed": int(seed)})
        if self.responder is not None:
            text = self.responder(index, messages)
            return FakeResponse(text=text)
        item = self.script[min(index, len(self.script) - 1)] if self.script else ""
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(text=str(item))


def plan(*actions: Mapping[str, Any], reasoning: str = "test") -> str:
    """构造一个合法 LLM 输出文本。"""
    return json.dumps({"reasoning": reasoning, "plan": [dict(a) for a in actions]},
                      ensure_ascii=False)


def legal_json(env: Any, **overrides: Any) -> str:
    """用 ``env.legal_actions()`` 的首个动作造一个可执行计划。"""
    action = dict(env.legal_actions()[0])
    action.update(overrides)
    return plan(action)
