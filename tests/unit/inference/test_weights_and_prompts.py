"""权重校验（NFR-03-06）与固定 prompt 装载（probe/bench/robustness 输入）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gamer_rsi.infra.errors import E
from gamer_rsi.inference.backend import Message
from gamer_rsi.inference.errors import InferenceError
from gamer_rsi.inference.prompts import (DEFAULT_PROMPTS_PATH, load_robustness_prompts,
                                         probe_messages, synthetic_prompts)
from gamer_rsi.inference.registry import ModelSpec
from gamer_rsi.inference.weights import weights_digest, verify_weights


def _spec(weights_path: Path | None, sha: str = "") -> ModelSpec:
    return ModelSpec(
        model_id="m", backend="llama_cpp",
        weights_path=str(weights_path) if weights_path else "", weights_sha256=sha,
        quantization="q4", tokenizer_path=None, context_length=1024, kv_cache_bits=8,
        resident_gb_est=1.0, kv_gb_per_1k_tokens=0.1, max_inflight=1,
        supports_grammar=True, supports_json_mode=True)


# ---- 权重 ---------------------------------------------------------------------------

def test_weights_digest_for_file_and_directory(tmp_path: Path):
    file_path = tmp_path / "model.gguf"
    file_path.write_bytes(b"weights")
    assert weights_digest(file_path) == hashlib.sha256(b"weights").hexdigest()

    model_dir = tmp_path / "model"
    (model_dir / "sub").mkdir(parents=True)
    (model_dir / "a.bin").write_bytes(b"a")
    (model_dir / "sub" / "b.bin").write_bytes(b"b")
    (model_dir / ".hidden").write_bytes(b"hidden") # 隐藏文件不参与
    first = weights_digest(model_dir)
    assert first == weights_digest(model_dir) # 稳定
    (model_dir / "sub" / "b.bin").write_bytes(b"c")
    assert weights_digest(model_dir) != first # 内容变化即变化


def test_verify_weights_requires_path_when_required(tmp_path: Path):
    with pytest.raises(InferenceError) as excinfo:
        verify_weights(_spec(None))
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert verify_weights(_spec(None), require_weights=False) is None


def test_verify_weights_missing_file(tmp_path: Path):
    with pytest.raises(InferenceError) as excinfo:
        verify_weights(_spec(tmp_path / "nope.gguf"))
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "权重缺失" in excinfo.value.message


def test_verify_weights_skips_when_sha_missing_or_escape_hatch(tmp_path: Path):
    path = tmp_path / "m.gguf"
    path.write_bytes(b"x")
    assert verify_weights(_spec(path)) is None # 注册表未登记 sha256 → 跳过
    assert verify_weights(_spec(path, sha="a" * 64), skip_hash_check=True) is None


def test_verify_weights_detects_mismatch(tmp_path: Path):
    path = tmp_path / "m.gguf"
    path.write_bytes(b"x")
    with pytest.raises(InferenceError) as excinfo:
        verify_weights(_spec(path, sha="a" * 64))
    assert excinfo.value.code == E.E_LLM_UNAVAILABLE
    assert "sha256 不匹配" in excinfo.value.message


def test_verify_weights_accepts_matching_digest(tmp_path: Path):
    path = tmp_path / "m.gguf"
    path.write_bytes(b"x")
    digest = weights_digest(path)
    assert verify_weights(_spec(path, sha=digest)) == digest


def test_skip_hash_env_escape_hatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PRD §9/Q-6：``GAMERSI_INFERENCE_SKIP_HASH=1`` 是唯一的本地逃生开关。"""
    from gamer_rsi.inference.weights import SKIP_HASH_ENV, skip_hash_check_enabled

    path = tmp_path / "m.gguf"
    path.write_bytes(b"x")
    spec = _spec(path, sha="a" * 64)
    assert skip_hash_check_enabled(None) is False
    monkeypatch.setenv(SKIP_HASH_ENV, "1")
    assert skip_hash_check_enabled(None) is True
    assert verify_weights(spec, skip_hash_check=skip_hash_check_enabled(None)) is None
    monkeypatch.setenv(SKIP_HASH_ENV, "0")
    assert skip_hash_check_enabled(None) is False

    class _Cfg:
        skip_hash_check = True

    assert skip_hash_check_enabled(_Cfg()) is True


# ---- prompt 装载 --------------------------------------------------------------------

def test_load_prompts_from_jsonl(tmp_path: Path):
    path = tmp_path / "prompts.jsonl"
    rows = [{"messages": [{"role": "system", "content": "s"},
                          {"role": "user", "content": f"u{i}"}]} for i in range(3)]
    rows.append({"messages": "not-a-list"}) # 非法行：跳过
    rows.append(["not", "a", "dict"]) # 非法行：跳过
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")
    prompts, source = load_robustness_prompts(3, path)
    assert source == f"file:{path}"
    assert [p[-1].content for p in prompts] == ["u0", "u1", "u2"]


def test_load_prompts_cycles_and_marks_extras(tmp_path: Path):
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"messages": [{"role": "user", "content": "u"}]}),
                    encoding="utf-8")
    prompts, _ = load_robustness_prompts(5, path)
    assert len(prompts) == 5
    assert len(prompts[0]) == 1 and len(prompts[-1]) == 2
    assert "决策点 #5" in prompts[-1][-1].content


def test_synthetic_prompts_are_deterministic_and_distinct():
    first = synthetic_prompts(12)
    second = synthetic_prompts(12)
    assert [[(m.role, m.content) for m in p] for p in first] == \
        [[(m.role, m.content) for m in p] for p in second]
    assert len({p[-1].content for p in first}) == 12
    assert synthetic_prompts(0) == []


def test_default_prompt_path_constant():
    assert DEFAULT_PROMPTS_PATH.endswith("robustness_prompts.jsonl")


def test_probe_messages_are_stable():
    messages = probe_messages()
    assert [m.role for m in messages] == ["system", "user"]
    assert all(isinstance(m, Message) for m in messages)
    assert probe_messages() == messages
