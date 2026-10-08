"""M1 真实模型复核的守门用例（2026-10-04，F14–F17）。

这四条缺陷**只有落盘真实权重后才会暴露**：此前单元/集成测试一律用假 ``mlx_lm``
模块覆盖我方代码路径，一致性套件只测我方 Protocol，因此 MLX 后端在真实库上完全
跑不起来也不会有任何红灯（详见 M1 验收报告 §6/§12）。本文件把四条都钉住。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from dice_ratchet.inference.mlx_backend import MLXBackend
from dice_ratchet.inference.prompts import PROBE_MESSAGES, probe_messages
from dice_ratchet.inference.weights import weights_digest

from test_backends_faked import MESSAGES, _FakeTokenizer, _spec  # noqa: E402


def _install_fake_mlx_lm(monkeypatch: pytest.MonkeyPatch, *, load_params: str) -> dict:
    """装上假 ``mlx_lm``；``load_params`` 控制 ``load()`` 的形参（模拟新旧 mlx-lm）。"""
    calls: dict = {"stream": []}
    module = types.ModuleType("mlx_lm")
    sample = types.ModuleType("mlx_lm.sample_utils")
    namespace: dict = {}
    if load_params == "new":
        exec("def load(path):\n    calls['load'] = (path, None)\n    return 'MODEL', TOK", # noqa: S102
             {"calls": calls, "TOK": _FakeTokenizer()}, namespace)
    else:
        exec("def load(path, tokenizer_path=None):\n"  # noqa: S102
             "    calls['load'] = (path, tokenizer_path)\n    return 'MODEL', TOK",
             {"calls": calls, "TOK": _FakeTokenizer()}, namespace)
    module.load = namespace["load"]

    def stream_generate(model, tokenizer, prompt=None, max_tokens=8, sampler=None):
        calls["stream"].append({"prompt": prompt, "max_tokens": max_tokens})
        yield types.SimpleNamespace(text='{"kind": "end_turn"}', token_ids=[1, 2, 3])

    module.stream_generate = stream_generate
    sample.make_sampler = lambda **kwargs: "SAMPLER"
    monkeypatch.setitem(sys.modules, "mlx_lm", module)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample)

    core = types.ModuleType("mlx.core")
    core.get_active_memory = lambda: 1_000_000_000
    core.clear_cache = lambda: None
    core.random = types.SimpleNamespace(seed=lambda value: calls.__setitem__("seed", value))
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return calls


# ---- F14：MLX API 漂移（load 形参 / 无 seed 形参）-----------------------------------

@pytest.mark.parametrize("load_params", ["new", "old"])
def test_load_adapts_to_installed_mlx_lm_signature(tmp_path, monkeypatch, load_params):
    """新版 mlx-lm 的 ``load()`` **没有** ``tokenizer_path``，旧版才有——两者都不得 TypeError。"""
    calls = _install_fake_mlx_lm(monkeypatch, load_params=load_params)
    weights = tmp_path / "model"
    weights.mkdir()
    (weights / "model.safetensors").write_bytes(b"w")
    backend = MLXBackend()

    import asyncio

    async def run():
        await backend.load(_spec(weights_path=str(weights)))
        return await backend.generate(MESSAGES, max_tokens=8, seed=7)

    result = asyncio.run(run())
    assert result.text == '{"kind": "end_turn"}'
    assert calls["load"][0] == str(weights)
    # 新版 mlx-lm 不再接受 tokenizer_path（保持 None），旧版收到显式路径
    assert calls["load"][1] == (None if load_params == "new" else str(weights))
    # 显式 seed 走 mx.random.seed（generate_step 已无 seed 形参）
    assert calls.get("seed") == 7
    assert "seed" not in calls["stream"][0]


def test_load_fails_loudly_when_separate_tokenizer_is_unsupported(tmp_path, monkeypatch):
    """新版 mlx-lm + tokenizer 与权重不同源 → 明确报错，不静默用错 tokenizer。"""
    _install_fake_mlx_lm(monkeypatch, load_params="new")
    weights = tmp_path / "model"
    weights.mkdir()
    (weights / "model.safetensors").write_bytes(b"w")
    other = tmp_path / "tok"
    other.mkdir()
    backend = MLXBackend()

    import asyncio

    from dice_ratchet.inference.errors import InferenceError

    async def run():
        await backend.load(_spec(weights_path=str(weights), tokenizer_path=str(other)))

    with pytest.raises(InferenceError) as exc:
        asyncio.run(run())
    assert exc.value.code == "E_LLM_BACKEND_ERROR"


# ---- F15：Qwen3 思考模式必须显式关闭 --------------------------------------------------

def test_chat_template_disables_thinking_when_supported():
    """模板支持 ``enable_thinking`` 时必须显式传 False（否则 <think> 烧光 token 预算）。"""
    recorded: dict = {}

    class _ThinkingCapable:
        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True,
                                **kwargs):
            recorded.update(kwargs)
            return "TEMPLATED"

    backend = MLXBackend()
    backend._tokenizer = _ThinkingCapable()
    assert backend._apply_chat_template(MESSAGES) == "TEMPLATED"
    assert recorded.get("enable_thinking") is False


def test_chat_template_falls_back_when_template_rejects_enable_thinking():
    """非 Qwen3 模板不接受该关键字时，退回调用于不抛异常。"""

    class _NoThinkingKwarg:
        templated: list = []

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
            self.templated.append((messages, {}))
            return "PLAIN"

    backend = MLXBackend()
    backend._tokenizer = _NoThinkingKwarg()
    assert backend._apply_chat_template(MESSAGES) == "PLAIN"


# ---- F16：探针 prompt 必须自带输出契约与 legal_actions ------------------------------

def test_probe_prompt_carries_contract_and_legal_actions():
    """``probe``/``bench`` 的 prompt 与真实 MOD-04 prompt 等价：给契约、给可选动作。

    修复前只写「字段为 reasoning 与 plan」，真实 8B 把 plan 写成自然语言、首答 100% 失败。
    """
    messages = probe_messages()
    assert messages == list(PROBE_MESSAGES)
    text = "\n".join(m.content for m in messages)
    assert "plan" in text and "kind" in text
    assert "legal_actions" in text
    # 至少给出一个 schema 里真实存在的 kind，模型只需照抄
    assert '"play_face"' in text and '"end_turn"' in text


# ---- F17：权重摘要不得被下载缓存污染 --------------------------------------------------

def test_weights_digest_ignores_hidden_cache_paths(tmp_path):
    root = tmp_path / "Qwen3-8B-4bit"
    root.mkdir()
    (root / "model.safetensors").write_bytes(b"weights")
    (root / "config.json").write_text("{}", encoding="utf-8")
    baseline = weights_digest(root)

    # HuggingFace 下载缓存：文件名本身不以 "." 开头，只有目录是隐藏的
    cache = root / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "model.safetensors.metadata").write_text("etag=1", encoding="utf-8")
    (cache / "model.safetensors.lock").write_text("", encoding="utf-8")
    (root / ".cache" / "huggingface" / "CACHEDIR.TAG").write_text("tag", encoding="utf-8")
    assert weights_digest(root) == baseline, "缓存文件混进了权重摘要（Q-6 锁定会随机失效）"

    # 真实权重变化必须改变摘要
    (root / "model.safetensors").write_bytes(b"weights2")
    assert weights_digest(root) != baseline


def test_weights_digest_matches_shipped_registry_if_weights_present():
    """权重在场时，注册表里的 ``weights_sha256`` 必须能通过校验（Q-6 的端到端守门）。"""
    from dice_ratchet.inference.registry import load_registry
    from dice_ratchet.inference.weights import verify_weights

    repo = Path(__file__).resolve().parents[3]
    registry = load_registry(str(repo / "configs" / "models.yaml"))
    spec = registry.spec("qwen3-8b-q4")
    if not Path(spec.weights_path).exists():
        pytest.skip("未落盘 models/Qwen3-8B-4bit（离线权重）")
    assert verify_weights(spec) == spec.weights_sha256
