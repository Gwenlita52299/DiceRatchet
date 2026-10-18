"""模型注册表加载与类型化（MOD-03 定义 schema，PRD §7 / NFR-03-04）。

``configs/models.yaml`` 是**静态注册表**：M1–M3 不参与 harness 演化，只允许人工变更并记
CHANGELOG。加载后统一成 ``ModelSpec``（``backend.py`` 对外 re-export），供后端与
``BackendManager`` 使用；schema 校验走 MOD-09 的 ``infra.io.validate("models", ...)``。

内存预算（PRD §7）::

    required_gb = resident_gb_est + kv_gb_per_1k_tokens × (context_length/1000) × max_inflight + 1.0
    required_gb > ram_budget_gb → acquire 前拒绝并抛 E_LLM_OOM（零提交）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping

from ..infra.errors import E, InfraError
from ..infra.io import validate as _validate_schema
from .errors import InferenceError

__all__ = ["BackendName", "ModelProviderName", "ModelSpec", "ModelRegistry",
           "DEFAULT_REGISTRY_PATH", "load_registry", "build_registry",
           "estimate_required_gb", "BUDGET_BUFFER_GB"]

BackendName = Literal["mlx", "llama_cpp", "llama_server", "mock", "deepseek"]

#: 模型提供方（Issue #1 §2）：``local`` = 本地权重/进程；``api`` = 远程 API。
ModelProviderName = Literal["local", "api"]

#: 注册表默认路径（PRD §9 ``inference.registry_path``）。
DEFAULT_REGISTRY_PATH: Final[str] = "configs/models.yaml"

#: 内存预算里的临时缓冲（PRD §7：1.0 GB）。
BUDGET_BUFFER_GB: Final[float] = 1.0


@dataclass(frozen=True)
class ModelSpec:
    """``configs/models.yaml`` 一行的类型化形态（PRD §6）。"""

    model_id: str
    backend: BackendName
    weights_path: str
    weights_sha256: str
    quantization: str
    tokenizer_path: str | None
    context_length: int
    kv_cache_bits: int
    resident_gb_est: float
    kv_gb_per_1k_tokens: float
    max_inflight: int
    supports_grammar: bool
    supports_json_mode: bool
    default_sampling: dict[str, float | int] = field(default_factory=dict)
    perf_budget: dict[str, float] = field(default_factory=dict)
    fallback_backend: BackendName | None = None
    server_args: list[str] = field(default_factory=list)
    notes: str = ""
    # ---- Issue #1：把「模块接口」与「具体模型」解耦所需的描述性字段 --------------------
    #: ``local`` / ``api``（决定 run provenance 里的 provider 与 online 标记）
    provider: ModelProviderName = "local"
    #: 显式在线标记：API 模式必须为 True（不许把远程调用记成本地）
    online: bool = False
    #: 模型族与参数量（报告分维度用；仅登记，不参与任何判定）
    family: str = ""
    params_b: float = 0.0
    #: 服务方发布的版本号（若提供）；API 响应里的 ``model`` 字段会覆盖它作为实际 revision
    model_revision: str = ""
    #: API 连接参数（``provider=api`` 时使用；key 只从 api_key_env 指向的环境变量读）
    api_base: str = ""
    api_model: str = ""
    api_key_env: str = ""
    #: 服务方是否支持可复现采样参数（DeepSeek 不承诺跨时间确定性 → 默认 False）
    api_supports_seed: bool = False
    #: 思考模式（``"enabled"`` / ``"disabled"`` / ``""`` = 不发送、用服务方默认）。
    #: 结构化 JSON 流水线应显式 ``disabled``：思考模式会把 ``max_tokens`` 花在
    #: ``reasoning_content`` 上，导致 ``content`` 为空串（实测 flash 默认开启）。
    api_thinking: str = ""
    #: 成本估算（**按 cost_currency 计价的 1k tokens 单价**；仅登记与报告，不参与判定）。
    #: 注意服务方可能分时段计价：cost_basis 说明登记的是哪一档（如「peak 上界」）。
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    #: 缓存命中输入的 1k tokens 单价（服务方前缀缓存；未登记时按未命中价计，绝不猜 0）
    cost_per_1k_input_cache_hit: float | None = None
    cost_currency: str = ""
    cost_basis: str = ""
    #: 是否**显式登记**过单价：未登记时 ``estimate_cost`` 返回 ``None``（记 null，不猜 0）
    cost_registered: bool = False
    #: 是否显式登记过缓存命中单价（未登记 ⇒ 命中 token 也按未命中价计，保守上界）
    cache_cost_registered: bool = False

    @property
    def has_weights(self) -> bool:
        """是否有需要落盘校验的权重（``mock`` / ``api`` 后端为 False）。"""
        return bool(self.weights_path)

    def estimate_cost(self, *, prompt_tokens: int, completion_tokens: int,
                      cache_hit_tokens: int = 0) -> dict[str, Any] | None:
        """按登记单价估算一次调用成本；**未登记即返回 None**（报告里记 null，绝不猜 0）。

        返回 ``{"amount": float, "currency": str, "basis": str}``：金额与币种分开记，
        因为服务方可能按人民币计价、且分高峰/空闲两档（``basis`` 说明登记的是哪一档）。
        金额保留 9 位而不是 6 位：单次调用常在 1e-5 量级，6 位会把 0.0000107 抹成
        0.000011（约 3% 误差），长跑汇总会系统性偏高。

        ``cache_hit_tokens``：服务方前缀缓存命中的输入 token。显式登记了
        ``cost_per_1k_input_cache_hit`` 时，命中部分按缓存价、其余按未命中价；
        未登记时**全部按未命中价**（保守上界，不会把未知折扣当成已知）。
        """
        if not self.cost_registered:
            return None
        prompt = max(0, int(prompt_tokens))
        hit = max(0, min(int(cache_hit_tokens), prompt))
        miss = prompt - hit
        hit_price = (float(self.cost_per_1k_input_cache_hit)
                     if self.cache_cost_registered
                     and self.cost_per_1k_input_cache_hit is not None
                     else self.cost_per_1k_input)
        amount = round(
            hit / 1000.0 * hit_price
            + miss / 1000.0 * self.cost_per_1k_input
            + completion_tokens / 1000.0 * self.cost_per_1k_output, 9)
        return {"amount": amount, "currency": self.cost_currency or "unspecified",
                "basis": self.cost_basis or "unspecified"}

    def required_gb(self, *, max_inflight: int | None = None,
                    context_length: int | None = None,
                    kv_cache_bits: int | None = None) -> float:
        """本 spec 在给定并发/上下文下的内存预算（PRD §7 公式）。"""
        return estimate_required_gb(
            self,
            max_inflight=self.max_inflight if max_inflight is None else max_inflight,
            context_length=self.context_length if context_length is None else context_length,
            kv_cache_bits=self.kv_cache_bits if kv_cache_bits is None else kv_cache_bits)


def estimate_required_gb(spec: ModelSpec, *, max_inflight: int, context_length: int,
                         kv_cache_bits: int | None = None) -> float:
    """``required_gb = resident + kv/1k × (ctx/1000) × inflight + 1.0``（PRD §7）。

    ``kv_cache_bits`` 只做登记（4bit 可救 30B 预算，Q-4）：预算公式仍用注册表里的
    ``kv_gb_per_1k_tokens``，实测回填由 ``inference bench`` 完成（NFR-03-02/03）。
    """
    if max_inflight < 1:
        raise InferenceError(E.E_LLM_BACKEND_ERROR,
                             f"max_inflight={max_inflight} 必须 ≥ 1（参数类错误，不可重试）",
                             retryable=False)
    if context_length < 1:
        raise InferenceError(E.E_LLM_BACKEND_ERROR,
                             f"context_length={context_length} 必须 ≥ 1",
                             retryable=False)
    kv_gb = spec.kv_gb_per_1k_tokens * (context_length / 1000.0) * max_inflight
    return spec.resident_gb_est + kv_gb + BUDGET_BUFFER_GB


@dataclass(frozen=True)
class ModelRegistry:
    """``configs/models.yaml`` 的类型化视图。"""

    registry_version: int
    ram_budget_gb: float
    fallback_backend: BackendName | None
    default_model: str | None
    defaults: dict[str, Any]
    models: dict[str, ModelSpec]
    raw: dict[str, Any]

    def ids(self) -> tuple[str, ...]:
        return tuple(self.models)

    def spec(self, model_id: str) -> ModelSpec:
        """按 id 取 spec；未知 id 抛 ``E_LLM_UNAVAILABLE``（FR-03-01）。"""
        try:
            return self.models[model_id]
        except KeyError:
            raise InferenceError(
                E.E_LLM_UNAVAILABLE,
                f"未知 model_id={model_id!r}（注册表共 {len(self.models)} 个模型）",
                hint=f"合法 id：{', '.join(self.ids())}") from None


# =====================================================================
# 加载
# =====================================================================

def _read_yaml(path: Path) -> dict[str, Any]:
    import yaml

    if not path.is_file():
        raise InferenceError(E.E_LLM_UNAVAILABLE, f"模型注册表不存在：{path}",
                             hint=f"检查 inference.registry_path（默认 {DEFAULT_REGISTRY_PATH}）")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InferenceError(E.E_LLM_UNAVAILABLE, f"模型注册表 YAML 解析失败：{path}\n{exc}",
                             hint="落盘必须是等价块式 YAML（PRD §7）") from exc
    if not isinstance(data, dict):
        raise InferenceError(E.E_LLM_UNAVAILABLE,
                             f"模型注册表顶层必须是对象：{path}",
                             hint="schema 见 schemas/models.schema.json")
    return data


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH, *,
                  schema_validate: bool = True) -> ModelRegistry:
    """读取 + schema 校验 + 归一为 ``ModelRegistry``。"""
    return build_registry(_read_yaml(Path(path)), source=str(path),
                          schema_validate=schema_validate)


def build_registry(data: Mapping[str, Any], *, source: str = "<memory>",
                   schema_validate: bool = True,
                   default_model: str | None = None) -> ModelRegistry:
    """把已解析的注册表 dict 归一为 ``ModelRegistry``。"""
    if schema_validate:
        try:
            _validate_schema("models", dict(data))
        except InfraError as exc:
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 f"模型注册表 schema 校验失败（{source}）：{exc.message}",
                                 hint="schema 见 schemas/models.schema.json（PRD §7）") from exc
    defaults = dict(data.get("defaults") or {})
    raw_models = data.get("models")
    if not isinstance(raw_models, Mapping) or not raw_models:
        raise InferenceError(E.E_LLM_UNAVAILABLE, f"模型注册表缺少非空 models（{source}）")
    registry_fallback = data.get("fallback_backend")
    models: dict[str, ModelSpec] = {}
    for model_id, entry in raw_models.items():
        if not isinstance(entry, Mapping):
            raise InferenceError(E.E_LLM_UNAVAILABLE,
                                 f"模型 {model_id!r} 的定义必须是对象（{source}）")
        models[str(model_id)] = _build_spec(str(model_id), entry, defaults,
                                            registry_fallback=registry_fallback)
    registry = ModelRegistry(
        registry_version=int(data.get("registry_version", 1)),
        ram_budget_gb=float(data.get("ram_budget_gb", 20.0)),
        fallback_backend=registry_fallback,
        default_model=default_model,
        defaults=defaults, models=models, raw=dict(data))
    return registry


def _pick(entry: Mapping[str, Any], defaults: Mapping[str, Any], key: str,
          fallback: Any = None) -> Any:
    if key in entry and entry[key] is not None:
        return entry[key]
    if key in defaults and defaults[key] is not None:
        return defaults[key]
    return fallback


def _build_spec(model_id: str, entry: Mapping[str, Any], defaults: Mapping[str, Any], *,
                registry_fallback: Any) -> ModelSpec:
    weights_sha256 = entry.get("weights_sha256") or ""
    backend = str(entry.get("backend"))
    fallback_backend = entry.get("fallback_backend", registry_fallback)
    server_args = entry.get("server_args") or []
    if not isinstance(server_args, (list, tuple)):
        raise InferenceError(E.E_LLM_UNAVAILABLE, f"模型 {model_id} 的 server_args 必须是数组")
    # provider 缺省由 backend 推导（deepseek → api），并据此定 online；显式值优先。
    provider = str(_pick(entry, defaults, "provider", "api" if backend == "deepseek"
                         else "local"))
    if provider not in ("local", "api"):
        raise InferenceError(E.E_LLM_UNAVAILABLE,
                             f"模型 {model_id} 的 provider={provider!r} 非法（local/api）")
    online = bool(_pick(entry, defaults, "online", provider == "api"))
    return ModelSpec(
        model_id=model_id,
        backend=backend, # type: ignore[arg-type]
        weights_path=str(entry.get("weights_path") or ""),
        weights_sha256=str(weights_sha256),
        quantization=str(_pick(entry, defaults, "quantization", "unknown")),
        tokenizer_path=(str(entry["tokenizer_path"]) if entry.get("tokenizer_path") else None),
        context_length=int(_pick(entry, defaults, "context_length", 8192)),
        kv_cache_bits=int(_pick(entry, defaults, "kv_cache_bits", 8)),
        resident_gb_est=float(entry.get("resident_gb_est", 0.0)),
        kv_gb_per_1k_tokens=float(_pick(entry, defaults, "kv_gb_per_1k_tokens", 0.0)),
        max_inflight=int(_pick(entry, defaults, "max_inflight", 1)),
        supports_grammar=bool(_pick(entry, defaults, "supports_grammar", False)),
        supports_json_mode=bool(_pick(entry, defaults, "supports_json_mode", True)),
        default_sampling=dict(entry.get("default_sampling") or {}),
        perf_budget={k: float(v) for k, v in (entry.get("perf_budget") or {}).items()},
        fallback_backend=(str(fallback_backend) if fallback_backend else None), # type: ignore[arg-type]
        server_args=[str(a) for a in server_args],
        notes=str(entry.get("notes") or ""),
        provider=provider, # type: ignore[arg-type]
        online=online,
        family=str(_pick(entry, defaults, "family", "")),
        params_b=float(_pick(entry, defaults, "params_b", 0.0)),
        model_revision=str(entry.get("model_revision") or ""),
        api_base=str(_pick(entry, defaults, "api_base", "")),
        api_model=str(_pick(entry, defaults, "api_model", "")),
        api_key_env=str(_pick(entry, defaults, "api_key_env", "")),
        api_supports_seed=bool(_pick(entry, defaults, "api_supports_seed", False)),
        api_thinking=str(_pick(entry, defaults, "api_thinking", "")),
        cost_per_1k_input=float(_pick(entry, defaults, "cost_per_1k_input", 0.0)),
        cost_per_1k_output=float(_pick(entry, defaults, "cost_per_1k_output", 0.0)),
        cost_per_1k_input_cache_hit=(
            float(_pick(entry, defaults, "cost_per_1k_input_cache_hit"))
            if ("cost_per_1k_input_cache_hit" in entry
                or "cost_per_1k_input_cache_hit" in defaults) else None),
        cost_currency=str(_pick(entry, defaults, "cost_currency", "")),
        cost_basis=str(_pick(entry, defaults, "cost_basis", "")),
        # 只有**显式**登记过单价才算「有成本口径」；仅靠 defaults 兜底不算（避免假账）
        cost_registered=any(k in entry for k in ("cost_per_1k_input", "cost_per_1k_output")),
        cache_cost_registered="cost_per_1k_input_cache_hit" in entry,
    )


def registry_to_json(registry: ModelRegistry) -> str:
    """注册表的规范 JSON（``config_hash`` 之外的静态工件，供报告与 golden）。"""
    return json.dumps(registry.raw, ensure_ascii=False, indent=2, sort_keys=True)
