"""MOD-03 → 调用方的**统一同步客户端契约**（Issue #1 §2 / 验收标准 A）。

为什么这个文件在 MOD-03，而不是继续留在 MOD-04 ``agent/policy.py``：角色装配件
``RoleModelProvider``（``roles.py``）必须返回一个实现统一契约的客户端；若契约与实现
定义在 MOD-04，MOD-03 就得反向 import MOD-04（分层倒挂）。因此契约（``InferenceClient``）
与同步适配实现（``BackendInferenceClient``）都下沉到本文件，
``dice_ratchet.agent.policy`` 只做 re-export —— 名字、签名、行为保持完全不变，
既有调用方与测试无需改动。

同步语义：MOD-03 的 ``BackendManager`` 在内部缓存 ``asyncio.Lock`` / ``Semaphore``，
它们一旦在某个 loop 上被 await 就与该 loop 绑定；所以所有同步调用共用一个常驻后台
loop（见 ``_background_loop``），不能每次 ``asyncio.run()`` 新建 loop。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..infra.errors import E, InfraError
from .backend import Message
from .errors import InferenceError

__all__ = ["InferenceClient", "BackendInferenceClient", "coerce_client", "client_for_role",
           "role_model_id", "coerce_messages"]


def coerce_messages(messages: Sequence[Any]) -> list[Message]:
    """把调用方的消息统一成 MOD-03 的 ``Message``（Issue #1 §2：统一契约）。

    调用方历史上混用三种形状：MOD-03 的 ``Message``、MOD-04 自己的 ``Message``
    （``role``/``content`` 同名）、以及裸 ``dict``（MOD-06 反思、MOD-07 提案）。
    统一在这里归一 —— 否则 ``estimate_prompt_tokens`` 会对着 ``dict`` 取 ``.content``
    而抛 ``AttributeError``（真实现场：MOD-07 的提案调用在真实客户端下必然失败）。
    """
    out: list[Message] = []
    for index, item in enumerate(messages):
        if isinstance(item, Message):
            out.append(item)
            continue
        if isinstance(item, Mapping):
            role, content = item.get("role"), item.get("content")
        else:
            role, content = getattr(item, "role", None), getattr(item, "content", None)
        if role is None or content is None:
            raise InferenceError(
                E.E_LLM_BACKEND_ERROR,
                f"messages[{index}] 既不是 Message 也不是含 role/content 的映射/对象"
                f"（收到 {type(item).__name__}）",
                retryable=False,
                hint="统一契约：{'role': 'system'|'user'|'assistant', 'content': str}")
        out.append(Message(role=str(role), content=str(content)))  # type: ignore[arg-type]
    return out


@runtime_checkable
class InferenceClient(Protocol):
    """MOD-04/06/07 → MOD-03 的统一同步门面（真实实现是 ``BackendInferenceClient``）。

    三个模型角色（policy / reflection / proposer）只依赖本协议：不接触 MLX、
    llama.cpp、DeepSeek/OpenAI SDK 或任何 HTTP 细节。
    """

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], *,
                      mode: str, sampling: Mapping[str, Any], seed: int) -> Any: ...


#: 持久后台事件循环（``_run_sync`` 用）。MOD-03 的 ``BackendManager`` 在内部缓存
#: ``asyncio.Lock`` / ``Semaphore``，它们一旦在某个 loop 上被 await 就与该 loop 绑定；
#: 若每次调用都 ``asyncio.run()`` 新建 loop，第二次调用即 ``bound to a different event
#: loop``。因此所有同步调用共用一个常驻 loop（M1 验收 F1 的实跑路径依赖这一点）。
_LOOP_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None


def _background_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP is not None and not _LOOP.is_closed():
            return _LOOP
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="dice-ratchet-inference",
                                  daemon=True)
        thread.start()
        _LOOP, _LOOP_THREAD = loop, thread
        return loop


def _run_sync(coro: Any) -> Any:
    """在同步上下文中执行 MOD-03 的协程（CLI/loop 均为同步编排）。

    - 已在事件循环内（如被异步调用方驱动）→ 丢到一次性线程里，避免嵌套 ``run_until_complete``；
    - 否则 → 提交到常驻后台 loop，保证 MOD-03 的锁/信号量始终绑在同一个 loop 上。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run_coroutine_threadsafe(coro, _background_loop()).result()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool: # pragma: no cover
        return pool.submit(lambda: asyncio.run(coro)).result()


class BackendInferenceClient:
    """把 MOD-03 的异步后端适配为同步 ``InferenceClient``。

    ``backend`` 可以是 ``Backend``（``generate_json(messages, schema, **kw)``）或
    ``BackendManager``（``generate_json(model_id, messages, schema, **kw)``，需给 ``model_id``）。

    ``role`` 只作**登记**（Issue #1：run provenance 要还原「哪个角色用了哪个模型」）：
    它不改变调用语义，`RoleModelProvider` 会为每个角色各建一个实例。
    """

    def __init__(self, backend: Any, *, model_id: str | None = None,
                 total_timeout_ms: int | None = None, max_attempts: int | None = None,
                 role: str | None = None) -> None:
        self.backend = backend
        self.model_id = model_id
        self.total_timeout_ms = total_timeout_ms
        self.max_attempts = max_attempts
        self.role = role

    def generate_json(self, messages: Sequence[Any], schema: Mapping[str, Any], *,
                      mode: str, sampling: Mapping[str, Any], seed: int) -> Any:
        kwargs = dict(sampling)
        kwargs["seed"] = int(seed)
        # 把调用方声明的 train/eval 口径带进 MOD-03：B-14 的收紧只在评估口生效，
        # 训练局的探索温度（0.7–1.2）不再被门面层的默认 mode 误判为越界。
        kwargs["mode"] = str(mode)
        if self.total_timeout_ms is not None:
            kwargs["total_timeout_ms"] = int(self.total_timeout_ms)
        if self.max_attempts is not None:
            kwargs["max_attempts"] = int(self.max_attempts)
        normalized = coerce_messages(messages)
        if self.model_id is not None:
            coro = self.backend.generate_json(self.model_id, normalized, schema, **kwargs)
        else:
            coro = self.backend.generate_json(normalized, schema, **kwargs)
        return _run_sync(coro)

    def __repr__(self) -> str: # pragma: no cover - 仅调试可读性
        return (f"BackendInferenceClient(role={self.role!r}, model_id={self.model_id!r},"
                f" backend={type(self.backend).__name__})")


def client_for_role(llm: Any, role: str) -> Any:
    """按角色取客户端（Issue #1 §2）。

    注入的对象可能是**角色提供者**（有 ``client_for(role)``，由组合根装配的
    ``RoleModelProvider``），也可能只是一个普通客户端（测试假件、单角色装配）。
    后者原样返回 —— 这条鸭子类型规则让 MOD-04/06/07 不必 import MOD-03 的提供者类型。
    """
    getter = getattr(llm, "client_for", None)
    if callable(getter):
        return getter(role)
    return llm


def role_model_id(llm: Any, role: str) -> str:
    """该角色实际使用的 ``model_id``（报告/埋点用；取不到返回空串）。"""
    getter = getattr(llm, "model_id_for", None)
    if callable(getter):
        return str(getter(role) or "")
    return str(getattr(llm, "model_id", "") or "")


def coerce_client(llm: Any, *, model_id: str | None = None, role: str | None = None) -> Any:
    """把 MOD-03 的后端/提供者对象自动适配为同步 ``InferenceClient``。

    - 角色提供者（有 ``client_for``）→ 按 ``role``（缺省 ``policy``）取该角色客户端；
    - ``BackendManager``（``registry/session`` 形状）→ 需要 ``model_id``（显式参数、
      对象属性或 ``config.model_id``）；拿不到即 fail-fast，避免把 ``messages`` 误传给
      ``model_id`` 形参。
    - 单个 ``Backend``（有 ``name`` / ``model_id``）→ 直接包装。
    - 其它对象按鸭子类型原样返回（测试用假 client 走这条路径）。
    """
    if llm is None:
        return None
    if callable(getattr(llm, "client_for", None)):
        return client_for_role(llm, role or "policy")
    try: # pragma: no cover - 依赖 MOD-03 类型
        from dice_ratchet.inference import BackendManager

        if isinstance(llm, BackendManager):
            resolved = model_id or getattr(llm, "model_id", None) \
                or getattr(getattr(llm, "config", None), "model_id", None)
            if not resolved:
                raise InfraError(
                    E.E_LLM_BACKEND_ERROR,
                    "BackendManager 需要显式 model_id 才能适配为 MOD-04 的 InferenceClient",
                    hint="用 BackendInferenceClient(manager, model_id='qwen3-8b-q4') 构造，"
                         "或在 manager.config.model_id 中配置")
            return BackendInferenceClient(llm, model_id=str(resolved), role=role)
    except InfraError:
        raise
    except Exception: # noqa: BLE001 - 导入失败时按鸭子类型处理
        return llm
    if hasattr(llm, "name") and hasattr(llm, "model_id"):
        return BackendInferenceClient(llm, role=role)
    return llm
