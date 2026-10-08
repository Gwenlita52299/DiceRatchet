"""本地 API key 文件（``inference.api_key_file``，默认 ``.env``）——真跑 API 的前置管道。

覆盖两件事：

1. **能用**：``cp .env.example .env`` + 粘 key 之后，B/C 臂不必每次 ``export``；
2. **安全边界**（与 Issue #1「认证与网络」一致）：
   - 真实环境变量优先，文件不覆盖；
   - 只有注册表声明的 ``api_key_env`` 会被读取 —— 文件里写 ``DICERATCHET_*`` 之类的行
     一律无效（本文件不能用来偷改配置）；
   - 查找过程**不修改进程环境**（受限子进程 / config.json 环境快照都看不到它）；
   - key 不落 harness/、run JSON、事件或审计记录（审计只记来源 ``api_key_source``）。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from dice_ratchet.infra.errors import E
from dice_ratchet.inference.backend import Message
from dice_ratchet.inference.deepseek_backend import ApiRequest, ApiResponse, DeepSeekAPIAdapter
from dice_ratchet.inference.errors import InferenceError
from dice_ratchet.inference.roles import resolve_role_bindings
from dice_ratchet.inference.registry import build_registry
from dice_ratchet.inference.secrets import (DEFAULT_API_KEY_FILE, env_file_path,
                                            lookup_api_key, parse_env_file,
                                            permissions_warning)
from support import inference_config, registry_data

from dataclasses import replace

KEY = "sk-local-file-key-not-a-real-secret"
MESSAGES = [Message(role="system", content="s"), Message(role="user", content="u")]


# =====================================================================
# 解析
# =====================================================================

def test_parse_env_file_handles_comments_exports_quotes_and_crlf():
    text = (
        "# 注释行\r\n"
        "\r\n"
        "DEEPSEEK_API_KEY=sk-abc   # 行内注释\r\n"
        "export OTHER_KEY=\"带 空格 的值\"\r\n"
        "SINGLE='单引号'\r\n"
        "WITH_EQUALS=a=b=c\r\n"
        "not a valid line\r\n"
        "1BAD=ignored\r\n"
        "NO_EQUALS\r\n"
    )
    parsed = parse_env_file(text)
    assert parsed["DEEPSEEK_API_KEY"] == "sk-abc"
    assert parsed["OTHER_KEY"] == "带 空格 的值"
    assert parsed["SINGLE"] == "单引号"
    assert parsed["WITH_EQUALS"] == "a=b=c"
    assert "NO_EQUALS" not in parsed and "1BAD" not in parsed
    assert parsed["DEEPSEEK_API_KEY"]  # 行内注释被剥掉且没有尾随空格


def test_parse_env_file_last_wins():
    assert parse_env_file("K=1\nK=2\n")["K"] == "2"


def test_env_file_path_from_config(tmp_path):
    assert env_file_path(inference_config()) == Path(DEFAULT_API_KEY_FILE)
    assert env_file_path(inference_config(api_key_file="configs/my.env")) == \
        Path("configs/my.env")
    assert env_file_path(inference_config(api_key_file="")) is None  # 显式关闭
    assert env_file_path(None) == Path(DEFAULT_API_KEY_FILE)


# =====================================================================
# 查找优先级与安全边界
# =====================================================================

def test_environment_wins_over_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=path,
                         environ={"DEEPSEEK_API_KEY": "from-environ"})
    assert (ref.source, ref.value) == ("environment", "from-environ")


def test_file_used_when_environment_missing(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=path, environ={})
    assert (ref.source, ref.value) == ("env_file", KEY)
    assert ref.present is True and "***" in repr(ref) # repr 不泄漏 key


def test_missing_file_or_name_is_missing_not_error(tmp_path):
    assert lookup_api_key("DEEPSEEK_API_KEY", env_file=tmp_path / "nope.env",
                          environ={}).source == "missing"
    path = tmp_path / ".env"
    path.write_text("OTHER=1\n", encoding="utf-8")
    ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=path, environ={})
    assert ref.source == "missing" and ref.value == ""
    assert lookup_api_key("", env_file=path, environ={}).source == "missing"


def test_lookup_never_touches_process_environment(tmp_path, monkeypatch):
    """本文件（含 DICERATCHET_* 行）绝不进进程环境 —— 不能用来偷改配置。"""
    monkeypatch.delenv("DEEPRATCHET_PLACEHOLDER", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "DEEPSEEK_API_KEY=" + KEY + "\n"
        "DICERATCHET_EXPERIMENT__ARM=C1\n"
        "DICERATCHET_INFERENCE__MODEL_ID=mock\n",
        encoding="utf-8")
    before = dict(os.environ)
    ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=path)
    assert ref.value == KEY
    assert dict(os.environ) == before # 进程环境零改动
    assert "DICERATCHET_EXPERIMENT__ARM" not in os.environ
    assert "DICERATCHET_INFERENCE__MODEL_ID" not in os.environ


def test_lookup_only_returns_the_requested_name(tmp_path):
    """只按名字精确查表：文件里别的 key（哪怕也是 *_API_KEY）不会被顺手带出来。"""
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=sk-other\n", encoding="utf-8")
    ref = lookup_api_key("DEEPSEEK_API_KEY", env_file=path, environ={})
    assert ref.source == "missing" and ref.value == ""
    # 反向：请求那个确实存在的名字才会命中（本文件不能"碰巧"给别的 key 兜底）
    assert lookup_api_key("OPENAI_API_KEY", env_file=path, environ={}).value == "sk-other"


@pytest.mark.skipif(os.name != "posix", reason="权限位检查仅 posix")
def test_permissions_warning(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    assert permissions_warning(path) is None
    os.chmod(path, 0o644)
    assert "chmod 600" in (permissions_warning(path) or "")
    assert permissions_warning(tmp_path / "nope") is None
    assert permissions_warning(None) is None


# =====================================================================
# 适配器接线
# =====================================================================

class RecordingInfra:
    def __init__(self, run_id: str | None = "r-key") -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._run_id = run_id

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def now_ms(self) -> float:
        return 0.0

    def run_id(self) -> str | None:
        return self._run_id

    def dump_raw(self, call_id: str, text: str) -> str | None:
        return None


class CaptureTransport:
    """记录请求（含内存里的 key）并返回合法响应。"""

    def __init__(self, text: str = '{"reasoning": "ok", "plan": [{"kind": "end_turn"}]}') -> None:
        self.text = text
        self.requests: list[ApiRequest] = []

    def __call__(self, request: ApiRequest) -> ApiResponse:
        self.requests.append(request)
        body = {"model": "deepseek-chat",
                "choices": [{"message": {"content": self.text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        return ApiResponse(200, json.dumps(body))


def _spec():
    return build_registry(registry_data(
        model_id="ds", backend="deepseek", supports_grammar=False,
        extra={"provider": "api", "online": True, "api_base": "https://example.invalid/v1",
               "api_model": "ds", "api_key_env": "DEEPSEEK_API_KEY"})).spec("ds")


def _adapter(tmp_path: Path, transport: CaptureTransport, *, infra: RecordingInfra | None = None,
             **overrides: Any) -> DeepSeekAPIAdapter:
    return DeepSeekAPIAdapter(
        infra=infra or RecordingInfra(), transport=transport,
        config=inference_config(**overrides), runs_root=tmp_path / "runs")


def test_adapter_reads_key_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text(f"# 本地 key\nDEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    transport = CaptureTransport()
    adapter = _adapter(tmp_path, transport, api_key_file=key_file)

    import asyncio

    asyncio.run(adapter.load(_spec()))
    result = asyncio.run(adapter.generate(MESSAGES, temperature=0.0, seed=1))
    assert "end_turn" in result.text # 解析在 BackendManager 的 execute_json_call 里
    assert transport.requests[0].api_key == KEY # 真的用上了文件里的 key
    assert adapter._api_key_source == "env_file"


def test_adapter_environment_beats_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-environ")
    key_file = tmp_path / ".env"
    key_file.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    transport = CaptureTransport()
    adapter = _adapter(tmp_path, transport, api_key_file=key_file)
    import asyncio

    asyncio.run(adapter.load(_spec()))
    asyncio.run(adapter.generate(MESSAGES, temperature=0.0, seed=1))
    assert transport.requests[0].api_key == "from-environ"
    assert adapter._api_key_source == "environment"


def test_adapter_missing_key_hint_points_at_local_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = _adapter(tmp_path, CaptureTransport(), api_key_file=tmp_path / ".env")
    import asyncio

    with pytest.raises(InferenceError) as exc:
        asyncio.run(adapter.load(_spec()))
    assert exc.value.code == E.E_LLM_UNAVAILABLE
    assert ".env" in (exc.value.hint or "") and ".env.example" in (exc.value.hint or "")


def test_adapter_env_file_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    adapter = DeepSeekAPIAdapter(infra=RecordingInfra(), transport=CaptureTransport(),
                                 config=inference_config(api_key_file=""),
                                 api_key_file=Path(""), runs_root=tmp_path / "runs")
    import asyncio

    with pytest.raises(InferenceError) as exc:
        asyncio.run(adapter.load(_spec()))
    assert exc.value.code == E.E_LLM_UNAVAILABLE


def test_audit_records_source_but_never_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    infra = RecordingInfra("r-key-audit")
    transport = CaptureTransport()
    adapter = _adapter(tmp_path, transport, infra=infra, api_key_file=key_file)
    import asyncio

    asyncio.run(adapter.load(_spec()))
    asyncio.run(adapter.generate(MESSAGES, temperature=0.0, seed=1, json_mode=True))

    record = json.loads(next((tmp_path / "runs" / "r-key-audit" / "llm_api").glob("*.json"))
                        .read_text(encoding="utf-8"))
    assert record["api_key_source"] == "env_file"
    assert record["api_key_env"] == "DEEPSEEK_API_KEY"
    blob = json.dumps(record, ensure_ascii=False) + json.dumps(infra.events, ensure_ascii=False)
    assert KEY not in blob


@pytest.mark.skipif(os.name != "posix", reason="权限位检查仅 posix")
def test_loose_permissions_emit_warning_event(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    os.chmod(key_file, 0o644)
    infra = RecordingInfra()
    adapter = _adapter(tmp_path, CaptureTransport(), infra=infra, api_key_file=key_file)
    import asyncio

    asyncio.run(adapter.load(_spec()))
    warnings = [f for name, f in infra.events if name == "llm.api.key_file_permissions"]
    assert len(warnings) == 1 and "chmod 600" in warnings[0]["reason"]
    assert KEY not in json.dumps(infra.events, ensure_ascii=False)

    # 收紧后不再告警
    os.chmod(key_file, 0o600)
    infra2 = RecordingInfra()
    adapter2 = _adapter(tmp_path, CaptureTransport(), infra=infra2, api_key_file=key_file)
    asyncio.run(adapter2.load(_spec()))
    assert not [f for name, f in infra2.events if name == "llm.api.key_file_permissions"]


# =====================================================================
# provenance
# =====================================================================

def _cfg_with_arm(arm: str, **inference_overrides: Any):
    repo = Path(__file__).resolve().parents[3]
    from dice_ratchet.infra.config import load_config

    cfg = load_config(config_dir=repo / "configs")
    cfg = replace(cfg, inference=replace(cfg.inference, **inference_overrides))
    return replace(cfg, experiment=replace(cfg.experiment, arm=arm))


def test_provenance_reports_env_file_source(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    bindings = resolve_role_bindings(_cfg_with_arm("B1", api_key_file=str(key_file)))
    proposer = bindings["proposer"]
    assert proposer.model_id == "deepseek-chat"
    assert proposer.api_key_present is True
    assert proposer.api_key_source == "env_file"
    assert KEY not in json.dumps(proposer.to_json(), ensure_ascii=False)
    # 决策/反思是本地模型 → 与 key 无关
    assert bindings["policy"].api_key_source == "missing"


def test_provenance_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-environ")
    key_file = tmp_path / ".env"
    key_file.write_text(f"DEEPSEEK_API_KEY={KEY}\n", encoding="utf-8")
    bindings = resolve_role_bindings(_cfg_with_arm("C1", api_key_file=str(key_file)))
    assert bindings["policy"].api_key_source == "environment"
    assert bindings["policy"].api_key_present is True


def test_provenance_missing_when_file_has_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / ".env"
    key_file.write_text("# 还没填\nDEEPSEEK_API_KEY=\n", encoding="utf-8")
    bindings = resolve_role_bindings(_cfg_with_arm("B1", api_key_file=str(key_file)))
    assert bindings["proposer"].api_key_present is False
    assert bindings["proposer"].api_key_source == "missing"
