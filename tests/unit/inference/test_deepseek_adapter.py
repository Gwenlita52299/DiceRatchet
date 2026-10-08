"""DeepSeek API 适配器（Issue #1 §「DeepSeek API 适配要求」/ 验收标准 B）。

**全部离线**：HTTP 由假 transport 承担（或走 ``api_mode=replay``），CI 不触网、不需要 key。

覆盖：请求构造（json mode / seed 口径 / 不送 top_k）、认证 fail-closed、错误映射
（401/400 不可重试；429/5xx/连接失败有限重试）、超时预算、schema 校验与修复重试复用
``BackendManager`` 三级策略、usage 与成本、request/response hash 审计、key 不落盘、
非确定性标记（不覆盖旧证据）、replay 复现。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from dice_ratchet.infra.config import InferenceConfig
from dice_ratchet.infra.errors import E
from dice_ratchet.inference.backend import BackendManager, Message
from dice_ratchet.inference.deepseek_backend import (API_JSON_INSTRUCTION, ApiRequest,
                                                     ApiResponse, DeepSeekAPIAdapter,
                                                     TransportError,
                                                     build_request_payload,
                                                     canonical_request_hash)
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.registry import build_registry, load_registry
from support import GOOD, decision_json, inference_config, registry_data

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs"
MESSAGES = [Message(role="system", content="你是决策器，输出 json"),
            Message(role="user", content="当前观测")]

#: 注册表里真实登记的 deepseek 模型（配置必须能被 schema 接受）
DEEPSEEK_REGISTRY = load_registry(CONFIGS / "models.yaml")


def shipped_deepseek_spec():
    """``configs/models.yaml`` 里真实的 deepseek-chat 规格（验证出厂配置可用）。"""
    return DEEPSEEK_REGISTRY.spec("deepseek-chat")


# =====================================================================
# 夹具
# =====================================================================

class RecordingInfra:
    """记录事件 / run_id 的 ``InfraFacade`` 假件（同时验证「审计走 infra.emit」）。"""

    def __init__(self, run_id: str | None = "r-api") -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._run_id = run_id
        self.dumps: list[str] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def now_ms(self) -> float:
        return 0.0

    def run_id(self) -> str | None:
        return self._run_id

    def dump_raw(self, call_id: str, text: str) -> str | None:
        self.dumps.append(text)
        return None

    def codes(self) -> list[str]:
        return [str(f.get("code")) for _, f in self.events if f.get("code")]


class FakeTransport:
    """脚本化 transport：``(status, body)`` 或 ``TransportError`` 逐次出队（末项粘住）。"""

    def __init__(self, *items: Any) -> None:
        self.items = list(items) or [ok_response(GOOD)]
        self.requests: list[ApiRequest] = []

    def __call__(self, request: ApiRequest) -> ApiResponse:
        self.requests.append(request)
        item = self.items[min(len(self.requests) - 1, len(self.items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def ok_response(text: str, *, model: str = "deepseek-chat",
                prompt_tokens: int = 11, completion_tokens: int = 7) -> ApiResponse:
    body = {"id": "x", "model": model, "choices": [{"message": {"role": "assistant",
                                                                "content": text},
                                                    "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}
    return ApiResponse(status=200, text=json.dumps(body, ensure_ascii=False))


def error_response(status: int, message: str = "boom",
                  headers: Mapping[str, str] | None = None) -> ApiResponse:
    return ApiResponse(status=status, text=json.dumps({"error": {"message": message}}),
                       headers=dict(headers or {}))


#: 单测用 deepseek 模型的注册表条目（``registry_data`` 的 ``extra`` 只能走 extra）
_API_EXTRA: dict[str, Any] = {
    "provider": "api", "online": True, "api_base": "https://example.invalid/v1",
    "api_model": "ds", "api_key_env": "DEEPSEEK_API_KEY", "api_supports_seed": False,
}


def deepseek_registry(**extra: Any):
    """单模型 deepseek 注册表；``extra`` 覆盖 API 描述字段。"""
    return build_registry(registry_data(model_id="ds", backend="deepseek",
                                        supports_grammar=False,
                                        extra={**_API_EXTRA, **extra}))


def deepseek_spec(**extra: Any):
    return deepseek_registry(**extra).spec("ds")


async def _load(adapter: DeepSeekAPIAdapter, spec: Any) -> DeepSeekAPIAdapter:
    await adapter.load(spec)
    return adapter


def make_adapter(transport: Any, *, infra: RecordingInfra | None = None,
                 config: InferenceConfig | None = None, **kwargs: Any) -> DeepSeekAPIAdapter:
    return DeepSeekAPIAdapter(infra=infra or RecordingInfra(), transport=transport,
                              config=config or inference_config(api_max_retries=2),
                              **kwargs)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _generate(adapter: DeepSeekAPIAdapter, *, grammar: str | None = None,
                    json_mode: bool = False, max_tokens: int = 256,
                    timeout_ms: int = 30_000, seed: int | None = 7) -> Any:
    return await adapter.generate(MESSAGES, max_tokens=max_tokens, temperature=0.0, top_p=1.0,
                                 seed=seed, grammar=grammar, json_mode=json_mode,
                                 timeout_ms=timeout_ms)


# =====================================================================
# 请求构造
# =====================================================================

def test_request_payload_json_mode_merges_instruction_into_system_message():
    spec = shipped_deepseek_spec()
    payload = build_request_payload(spec, MESSAGES, max_tokens=64,
                                    sampling={"temperature": 0.0, "top_p": 1.0,
                                              "top_k": 5, "repetition_penalty": 1.2,
                                              "seed": 42},
                                    json_mode=True)
    assert payload["model"] == "deepseek-chat"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0]["role"] == "system"
    assert API_JSON_INSTRUCTION in payload["messages"][0]["content"]
    assert "json" in payload["messages"][0]["content"].lower() # 服务方硬要求
    assert len(payload["messages"]) == len(MESSAGES) # 不新增轮次
    assert payload["temperature"] == 0.0 and payload["top_p"] == 1.0
    # 服务方不接受的采样键一律不发；seed 未声明支持也不发（不假装有确定性）
    assert "top_k" not in payload and "repetition_penalty" not in payload
    assert "seed" not in payload


def test_request_payload_seed_only_when_spec_declares_support():
    spec = deepseek_spec(api_supports_seed=True)
    payload = build_request_payload(spec, MESSAGES, max_tokens=16,
                                    sampling={"temperature": 0.0, "top_p": 1.0, "seed": 9},
                                    json_mode=False, stop=["\n\n"])
    assert payload["seed"] == 9 and payload["stop"] == ["\n\n"]
    assert "response_format" not in payload


def test_request_hash_is_stable_and_order_independent():
    a = canonical_request_hash({"model": "m", "messages": [], "temperature": 0.0})
    b = canonical_request_hash({"temperature": 0.0, "messages": [], "model": "m"})
    assert a == b and a.startswith("sha256:")


# =====================================================================
# 认证与 replay（fail-closed）
# =====================================================================

def test_missing_api_key_env_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = make_adapter(FakeTransport(), config=inference_config())
    with pytest.raises(InferenceError) as exc:
        run(_load(adapter, shipped_deepseek_spec()))
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert exc.value.retryable is False
    assert "DEEPSEEK_API_KEY" in (exc.value.hint or "")
    assert adapter._state == "unloaded" # 未就绪：零提交


def test_api_model_without_key_env_name_fails_closed(tmp_path):
    spec = deepseek_spec(api_key_env="")
    adapter = make_adapter(FakeTransport())
    with pytest.raises(InferenceError) as exc:
        run(_load(adapter, spec))
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert "api_key_env" in exc.value.message


def test_replay_requires_existing_dir():
    adapter = make_adapter(FakeTransport(), config=inference_config(api_mode="replay",
                                                                    api_replay_dir=""))
    with pytest.raises(InferenceError) as exc:
        run(_load(adapter, deepseek_spec()))
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert "api_replay_dir" in (exc.value.hint or "")


def test_grammar_rejected_explicitly(tmp_path):
    adapter = make_adapter(FakeTransport(), api_key="k")
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter, grammar='root ::= "{}"'))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR
    assert exc.value.retryable is False


# =====================================================================
# 成功路径：usage / 成本 / 审计落盘 / key 不落盘
# =====================================================================

def test_generate_success_writes_audit_without_key(tmp_path):
    infra = RecordingInfra(run_id="r-api-ok")
    transport = FakeTransport(ok_response(decision_json()))
    adapter = DeepSeekAPIAdapter(infra=infra, transport=transport, api_key="sk-secret-xyz",
                                config=inference_config(api_record_dir="llm_api"),
                                runs_root=tmp_path)
    run(_load(adapter, shipped_deepseek_spec()))
    result = run(_generate(adapter, json_mode=True))

    assert result.text == decision_json()
    assert result.backend == "deepseek" and result.model_id == "deepseek-chat"
    assert result.usage.prompt_tokens == 11 and result.usage.completion_tokens == 7

    # 审计记录：request/response/prompt hash + sampling + usage + 成本
    files = sorted((tmp_path / "r-api-ok" / "llm_api").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    payload = build_request_payload(shipped_deepseek_spec(), MESSAGES, max_tokens=256,
                                    sampling={"temperature": 0.0, "top_p": 1.0, "top_k": 0,
                                              "repetition_penalty": 1.0, "seed": 7},
                                    json_mode=True)
    assert record["request_hash"] == canonical_request_hash(payload)
    assert record["response_hash"].startswith("sha256:")
    assert record["prompt_hash"].startswith("sha256:")
    assert record["provider"] == "api" and record["online"] is True
    assert record["replay"] is False and record["model_revision"] == "deepseek-chat"
    assert record["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
    assert record["cost_usd"] == pytest.approx(11 / 1000 * 0.00027 + 7 / 1000 * 0.0011,
                                              rel=1e-6)
    assert record["json_mode"] is True and record["status"] == 200
    assert record["api_key_env"] == "DEEPSEEK_API_KEY"
    # 绝不落盘 key：记录文件与事件里都不许出现
    blob = files[0].read_text(encoding="utf-8") + json.dumps(infra.events, ensure_ascii=False)
    assert "sk-secret-xyz" not in blob
    assert [name for name, _ in infra.events] == ["llm.api.call"]
    # 请求头带 key（只在内存），但 ApiRequest 的可落盘视图不带
    assert transport.requests[0].api_key == "sk-secret-xyz"
    assert "authorization" not in {k.lower() for k in
                                   transport.requests[0].with_key_redacted()["headers"]}


def test_usage_falls_back_to_estimate_when_absent(tmp_path):
    body = {"model": "deepseek-chat",
            "choices": [{"message": {"content": GOOD}, "finish_reason": "stop"}]}
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(),
                                transport=FakeTransport(ApiResponse(200, json.dumps(body))),
                                api_key="k", runs_root=tmp_path)
    run(_load(adapter, deepseek_spec()))
    result = run(_generate(adapter))
    assert result.usage.prompt_tokens > 0 and result.usage.completion_tokens == 0


def test_key_is_redacted_from_error_messages(tmp_path):
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), api_key="sk-leak-me",
                                config=inference_config(api_max_retries=0),
                                transport=FakeTransport(error_response(
                                    400, "bad request: sk-leak-me is invalid")))
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert "sk-leak-me" not in str(exc.value)
    assert "***" in str(exc.value)


# =====================================================================
# 错误映射与有限重试
# =====================================================================

def test_retry_on_429_then_success(tmp_path):
    sleeps: list[float] = []
    transport = FakeTransport(error_response(429, "rate limited", {"Retry-After": "1"}),
                             ok_response(GOOD))
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=2), transport=transport, api_key="k", runs_root=tmp_path,
        sleep=lambda seconds: _record_sleep(sleeps, seconds))
    run(_load(adapter, deepseek_spec()))
    result = run(_generate(adapter))
    assert result.text == GOOD
    assert len(transport.requests) == 2 and sleeps == [0.5]
    record = json.loads(next((tmp_path / "r-api" / "llm_api").glob("*.json"))
                        .read_text(encoding="utf-8"))
    assert record["attempts"] == 2 # 重试次数如实入账


async def _record_sleep(bucket: list[float], seconds: float) -> None:
    bucket.append(round(float(seconds), 3))


def test_429_exhausts_retries_and_is_retryable(tmp_path):
    transport = FakeTransport(error_response(429, "rate limited"))
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=1), transport=transport, api_key="k", runs_root=tmp_path,
        sleep=lambda _s: asyncio.sleep(0))
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR
    assert exc.value.retryable is True
    assert exc.value.detail["status"] == 429
    assert len(transport.requests) == 2 # 1 次 + 1 次重试
    assert adapter._state == "ready" # 传输失败不改变后端就绪状态


@pytest.mark.parametrize("status,code,retryable,attempts", [
    (401, E.E_LLM_UNAVAILABLE, False, 1),
    (403, E.E_LLM_UNAVAILABLE, False, 1),
    (404, E.E_LLM_UNAVAILABLE, False, 1),
    (400, E.E_LLM_BACKEND_ERROR, False, 1),
    (500, E.E_LLM_BACKEND_ERROR, True, 3),
    (503, E.E_LLM_BACKEND_ERROR, True, 3),
])
def test_status_mapping(status, code, retryable, attempts):
    transport = FakeTransport(error_response(status))
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=2), transport=transport, api_key="k", runs_root="/tmp/dr-ds",
        sleep=lambda _s: asyncio.sleep(0))
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == code
    assert exc.value.retryable is retryable
    assert len(transport.requests) == attempts
    if not retryable:
        assert adapter._state == "ready"


def test_connection_failure_is_retryable_then_maps_unavailable():
    transport = FakeTransport(TransportError("connect", "Connection refused"))
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=0), transport=transport, api_key="k", runs_root="/tmp/dr-ds")
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert exc.value.retryable is True


def test_timeout_budget_exhaustion_maps_timeout():
    transport = FakeTransport(TransportError("timeout", "read timed out"))
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=5, api_connect_timeout_ms=10), transport=transport, api_key="k",
        runs_root="/tmp/dr-ds", sleep=lambda _s: asyncio.sleep(0))
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter, timeout_ms=1))
    assert exc.value.code == E.E_LLM_TIMEOUT
    assert exc.value.retryable is True


def test_malformed_json_body_is_retryable():
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=0), transport=FakeTransport(ApiResponse(200, "not json")),
        api_key="k", runs_root="/tmp/dr-ds")
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR and exc.value.retryable is True


def test_missing_choices_is_retryable():
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=0),
        transport=FakeTransport(ApiResponse(200, json.dumps({"choices": []}))),
        api_key="k", runs_root="/tmp/dr-ds")
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR


def test_empty_content_is_explicit_error():
    body = {"choices": [{"message": {"role": "assistant", "reasoning_content": "hmm"},
                         "finish_reason": "stop"}]}
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), config=inference_config(
        api_max_retries=0), transport=FakeTransport(ApiResponse(200, json.dumps(body))),
        api_key="k", runs_root="/tmp/dr-ds")
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR
    assert "content" in exc.value.message


# =====================================================================
# 非确定性：不覆盖旧证据
# =====================================================================

def test_response_change_is_marked_and_old_evidence_kept(tmp_path):
    infra = RecordingInfra(run_id="r-nondet")
    other = '{"reasoning": "second", "plan": [{"kind": "end_turn"}]}'
    transport = FakeTransport(ok_response(GOOD), ok_response(other))
    adapter = DeepSeekAPIAdapter(infra=infra, config=inference_config(), transport=transport,
                                api_key="k", runs_root=tmp_path)
    run(_load(adapter, deepseek_spec()))
    first = run(_generate(adapter))
    second = run(_generate(adapter))
    assert first.text != second.text

    directory = tmp_path / "r-nondet" / "llm_api"
    files = sorted(p.name for p in directory.glob("*.json"))
    assert len(files) == 2 and any(".v2.json" in name for name in files)
    base = json.loads(next(p for p in directory.glob("*.json")
                           if ".v" not in p.name).read_text(encoding="utf-8"))
    # 旧证据未被覆盖：记录的仍是**第一次**的原始响应体哈希
    assert base["response_hash"] == record_hash_of(ok_response(GOOD).text)
    nondet = [f for name, f in infra.events if name == "llm.nondeterministic"]
    assert len(nondet) == 1
    assert nondet[0]["code"] == str(E.E_LLM_NONDETERMINISTIC)
    assert nondet[0]["response_hash"] != nondet[0]["previous_response_hash"]


def record_hash_of(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_identical_response_is_idempotent(tmp_path):
    infra = RecordingInfra(run_id="r-idem")
    adapter = DeepSeekAPIAdapter(infra=infra, config=inference_config(),
                                transport=FakeTransport(ok_response(GOOD)), api_key="k",
                                runs_root=tmp_path)
    run(_load(adapter, deepseek_spec()))
    run(_generate(adapter))
    run(_generate(adapter))
    files = list((tmp_path / "r-idem" / "llm_api").glob("*.json"))
    assert len(files) == 1
    assert not [f for name, f in infra.events if name == "llm.nondeterministic"]


# =====================================================================
# replay
# =====================================================================

def test_replay_reproduces_recorded_response_without_network(tmp_path):
    live_dir = tmp_path / "live" / "r-replay" / "llm_api"
    live = DeepSeekAPIAdapter(infra=RecordingInfra(run_id="r-replay"),
                             config=inference_config(), transport=FakeTransport(
                                 ok_response(decision_json())), api_key="k",
                             runs_root=tmp_path / "live")
    run(_load(live, deepseek_spec()))
    expected = run(_generate(live, json_mode=True))
    assert live_dir.is_dir()

    # 把录制文件搬成 replay 目录的 <request_hash>.json 形状
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    for path in live_dir.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["response"] = json.loads(ok_response(decision_json()).text)
        (replay_dir / path.name).write_text(json.dumps(record, ensure_ascii=False),
                                           encoding="utf-8")

    replay = DeepSeekAPIAdapter(
        infra=RecordingInfra(run_id="r-replay2"),
        config=inference_config(api_mode="replay", api_replay_dir=str(replay_dir)),
        transport=FakeTransport(error_response(500, "network must not be used")),
        runs_root=tmp_path / "replay-runs")
    run(_load(replay, deepseek_spec()))
    result = run(_generate(replay, json_mode=True))
    assert result.text == expected.text
    # replay 结果单独记档，且标 replay=true（不得冒充新样本）
    records = list((tmp_path / "replay-runs" / "r-replay2" / "llm_api_replay").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["replay"] is True


def test_replay_missing_record_fails_closed(tmp_path):
    replay_dir = tmp_path / "empty"
    replay_dir.mkdir()
    adapter = DeepSeekAPIAdapter(
        infra=RecordingInfra(run_id="r-x"),
        config=inference_config(api_mode="replay", api_replay_dir=str(replay_dir)),
        transport=FakeTransport(ok_response(GOOD)), runs_root=tmp_path)
    run(_load(adapter, deepseek_spec()))
    with pytest.raises(InferenceError) as exc:
        run(_generate(adapter))
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert exc.value.retryable is False
    assert "replay" in exc.value.message


# =====================================================================
# 与本地后端同口径：复用 BackendManager 的三级 JSON 策略
# =====================================================================

def _manager_with(transport: Any, *, config: InferenceConfig, records: list[Any] | None = None):
    registry = deepseek_registry()
    sink = records if records is not None else []
    manager = BackendManager(
        registry=registry, mode="eval", config=config,
        backend_factory=lambda spec: DeepSeekAPIAdapter(infra=RecordingInfra("r-mgr"),
                                                        config=config, transport=transport,
                                                        api_key="k"),
        record_sink=sink.append)
    return manager, sink


def test_manager_schema_validation_and_repair_retry_reused():
    # 第一次返回坏 JSON，第二次（修复重试）返回合法动作 → 与本地后端同一条三级策略
    transport = FakeTransport(ok_response("{not json"), ok_response(decision_json()))
    manager, records = _manager_with(transport, config=inference_config(
        mode="eval", seed=1042, api_max_retries=0))
    result = run(_wait(manager.generate_json(
        "ds", MESSAGES, {"type": "object", "required": ["reasoning", "plan"]},
        temperature=0.0, seed=1042)))
    assert result.parsed and result.attempts == 2
    assert result.backend == "deepseek" and result.outcome_kind == "degraded"
    assert len(transport.requests) == 2 and len(records) == 2


def test_manager_eval_mode_hardening_applies_to_api_backend():
    # B-14：eval 模式下 temperature != 0 直接拒绝（API 后端不例外）
    manager, _ = _manager_with(FakeTransport(ok_response(decision_json())),
                               config=inference_config(mode="eval", seed=1042))
    with pytest.raises(InferenceError) as exc:
        run(_wait(manager.generate_json("ds", MESSAGES, {"type": "object"},
                                       temperature=0.7, seed=1042)))
    assert exc.value.code == E.E_LLM_BACKEND_ERROR


def test_manager_never_exceeds_budget_for_api_model():
    manager, _ = _manager_with(FakeTransport(ok_response(decision_json())),
                               config=inference_config(mode="eval", seed=1042))
    assert manager.estimate_required_gb("ds") <= manager.ram_budget_gb


async def _wait(coro: Any) -> Any:
    return await coro
