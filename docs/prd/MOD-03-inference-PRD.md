# MOD-03 inference 技术 PRD

> 版本：v1.7（2026-10-10 **`generate_json` 增加按调用方的 `max_tokens`**（Spec v2.3 B-47，F37：决策点 ~200 token 与 harness 提案 ~500 token 不能共用同一个全局上限）；见 §6/§17）· 状态：草案（待复核）· 所属里程碑：M1（DeepSeek 后端为 Issue #1 追加）
> 上游 Spec：[../模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md)（**唯一事实来源**；冲突以 Spec 为准）
> 上游计划：[../../项目计划-DiceRatchet.md](../../项目计划-DiceRatchet.md) §4 技术栈（Mac M5 24GB / MLX 或 llama.cpp / Qwen3-8B → 27B Q4）、§8 风险（LLM 输出非 JSON）
> 作者：DiceRatchet 技术文档（MOD-03 负责模块）· 依赖：MOD-09 infra（配置、日志、run_id、CLI）

## 1. 背景与目标

MOD-03 是 L1 层唯一的"模型出口"：把 `Mac M5 24GB + 全程离线` 的本地推理收敛为**可测、可复现、可降级**的接口，供 MOD-04 每回合决策调用、供 MOD-06/08 在评估时并发调用。要解决的五个具体问题：① 8B 自由生成 JSON 的语法错误率不可接受（计划 §8 首条风险）；② MLX 与 llama.cpp 两条路线的差异上层不得感知；③ 24GB 是硬上限，OOM 是必然事件而非异常；④ held-out 评估要并发跑多局，而 LLM 是串行资源；⑤ INV-6 要求一切随机来自显式 seed，而采样温度就是随机性本身。

**目标（M1 可验收）**：① 200 条真实固定 prompt 上 `generate_json` 首答通过率 ≥ 90%、三级全失败率 ≤ 1%；② 评估模式同 seed 同配置生成文本逐字节一致（G3）；③ OOM 以 `E_LLM_OOM` 可捕获返回而不崩进程；④ `Backend` 契约定稿并配一致性测试套件，新增后端零改动上层。

## 2. 范围

### 2.1 范围内
- `dice_ratchet/inference/backend.py`：`Backend` Protocol、`Message`/`Usage`/`GenerationResult`/`BackendHealth`/`ModelSpec`、`MockBackend`、`BackendManager`。
- `dice_ratchet/inference/mlx_backend.py`：`MLXBackend`（`mlx-lm` 进程内；LoRA 挂载点预留给 MOD-10）。
- `dice_ratchet/inference/llama_backend.py`：`LlamaCppBackend`（`llama-cpp-python` 进程内）与 `LlamaServerBackend`（`llama-server` 子进程 + HTTP + GBNF）。
- `dice_ratchet/inference/schema.py`：JSON Schema 子集 → GBNF 编译器、`validate()`、修复 prompt 渲染（独占拥有，MOD-04 与 harness 都不得覆盖，Spec v1.4 B-28）；加载 `schemas/decision.schema.json`（Spec v1.3 §4.3 的唯一决策 schema，本模块与 MOD-04 共用，不再各自维护）。
- `configs/models.yaml` 模型注册表；采样参数管控、超时/取消、重试退避、token/延迟埋点、`E_LLM_*` 错误码与降级信号。
- **（v1.6 新增，Spec v2.0 B-42）**`dice_ratchet/inference/client.py`（三个角色共用的 `InferenceClient` 统一同步契约 + `BackendInferenceClient`）、`deepseek_backend.py`（`provider=api` 的外部模型后端）、`roles.py`（`RoleModelProvider` / `resolve_role_bindings` / `role_model_ids` / `run_provenance`）、`secrets.py`（`api_key_env` → 环境变量或本地 `.env` 的 key 读取，**只进内存**）。

### 2.2 范围外（非目标）

> 逐字复用 Spec §3 MOD-03 条目。

**不负责**：决定 prompt 内容（MOD-04）、动作合法性判断（MOD-01/04）、模型微调（MOD-10）。

补充边界澄清：本模块**不知道** Observation 结构、`legal_actions()` 与游戏规则；**不做**语义/合法性校验（那是 MOD-04 第二级）；**不写** `runs/**/*.jsonl`（只产出埋点记录对象，落盘归 MOD-09）；**不做** fallback 到 heuristic 的决策（归 MOD-04/06）。

## 3. 使用方与场景

| 使用方 | 场景 | 调用形态 |
|---|---|---|
| MOD-04 policy | 每个决策点：`obs` 组装的 prompt → 一个符合 `DECISION_SCHEMA` 的 JSON（含 1..12 个动作的计划） | `generate_json(messages, DECISION_SCHEMA)`，期望 p95 < 4 s（Spec §8.6 成本模型取 p50≈3 s） |
| MOD-04 policy / MOD-07 evolve | 反思、记忆、候选改进生成（自由文本） | `generate(messages)` |
| MOD-06 loop | 一个 run 内模型常驻，跨局/跨代复用 | `BackendManager.get(model_id)` |
| MOD-08 evals | held-out 局级并发评估（默认 2 局，共享推理队列，P-10）、三基线归档、格式鲁棒性报告 | `asyncio.gather` 共享 manager，信号量限流 |
| MOD-09 infra | 采集埋点、写 `metrics/llm_calls.jsonl`、算 `config_hash` | 消费 `InferenceCallRecord` |

关键约束：L1 及以上**禁止 import `dice_ratchet.env.*`**（INV-1），由 MOD-09 `guards.py` AST 扫描纳入 CI 门禁（FR-03-11）。

## 4. 功能需求（FR-03-x）

| # | 需求（可测试） |
|---|---|
| FR-03-01 | `BackendManager.load(model_id)` 幂等且 single-flight：同 id 返回同一实例、只触发一次真实加载；未知 id 抛 `E_LLM_UNAVAILABLE`。 |
| FR-03-02 | `unload()` 释放权重与 KV（MLX 释放引用 + `mx.clear_cache()`；server 模式 `SIGTERM` 子进程），其后 `health().state == "unloaded"`；未加载时 `unload` 不报错；`async with manager.session(model_id)` 退出自动卸载。 |
| FR-03-03 | `generate()` 支持 `max_tokens`/`stop`/`seed`/`temperature`/`top_p`/`top_k`/`repetition_penalty`/`timeout_ms`；`Usage` 必须含 `prompt_tokens`/`completion_tokens`/`latency_ms`/`ttft_ms`。 |
| FR-03-04 | `generate_json()` 三级策略按序执行：① 约束解码（后端 GBNF 或 JSON mode）→ ② `jsonschema.validate` → ③ 带校验错误的修复重试。任一级成功即返回，`attempts` 记录真实尝试次数（1..`max_attempts`）。 |
| FR-03-05 | 重试上限 `max_attempts` 默认 3（首答 + 2 修复）；退避 = `retry_backoff_ms × 2^(attempt-1)`（默认 200/400 ms）；全程受 `total_timeout_ms`（默认 120000，2026-10-04 实测由 8000 回填：8 s < 实测 p50 会把正常决策判成 `E_LLM_TIMEOUT`）约束，超时抛 `E_LLM_TIMEOUT` 且**不留半截结果**。 |
| FR-03-06 | 每次尝试产出一条 `InferenceCallRecord`（§13），由 MOD-09 写 `runs/<run_id>/metrics/llm_calls.jsonl`，并内嵌进 `episodes/<split>/<episode_key>.jsonl` 步记录（对齐 Spec §4.5）。 |
| FR-03-07 | 采样白名单：`temperature ∈ [0,1.5]`、`top_p ∈ (0,1]`、`top_k ∈ [0,200]`、`repetition_penalty ∈ [1.0,1.5]`；越界抛 `E_LLM_BACKEND_ERROR`（不静默裁剪）。`mode="eval"` 额外要求 `temperature == 0.0` 且 `seed is not None`（Spec v1.3 §8.1 B-14：评估温度统一为 0，harness 不可覆盖；v1.0 的「≤0.3」上限与 MOD-04 的 0.7 冲突，已废弃）。 |
| FR-03-08 | 并发限流与预算前置：`BackendManager` 持 `asyncio.Semaphore(max_inflight)`（默认 MLX=1、server=4）；acquire **之前**做内存预算检查，预计超预算抛 `E_LLM_OOM` 且**不提交**推理请求。 |
| FR-03-09 | 失败一律以 `E_LLM_*` 异常暴露，`GenerationResult.outcome_kind ∈ {"action","degraded","error"}`；本模块不自行 fallback，由上层决定。 |
| FR-03-10 | 上下文守卫：`prompt_tokens + max_tokens > context_length` 时，`context_overflow_policy="reject"`（默认）抛 `E_LLM_CONTEXT_OVERFLOW`；`"truncate"` 则从最早的非 system 消息截断并记 `truncated=true`。 |
| FR-03-11 | 不得 `import dice_ratchet.env.*`（INV-1），CI 静态扫描强制。 |
| FR-03-12 | 确定性：同 `model_id + prompt + 采样参数 + seed` 在同一后端、同一模型文件下，评估模式（`temperature=0`）生成文本逐字节一致（口径见 NFR-03-01）。 |

## 5. 非功能需求（NFR-03-x）

| # | 类别 | 要求 |
|---|---|---|
| NFR-03-01 | 确定性 | 评估模式固定 `temperature=0.0, top_p=1.0, top_k=0` + 显式 seed；训练模式 `temperature ∈ [0.7,1.0]`（计划 §3.3 防确定性窥探），故**训练模式不承诺文本级复现**。三级口径：**L1**（硬要求）同后端同模型文件重跑 3 次文本逐字节相同；**L2**（软要求，不保证）跨后端文本一致——kernel 与量化不同；**L3**（提交口径）轨迹级可复现以 `obs_digest` + `action` JSON 为准，`llm_raw` 不参与 `state_hash`（INV-2/INV-6）。 |
| NFR-03-02 | 性能 | 8B-Q4，prompt ≤1024 tok、`max_tokens=128`：p50 ≤1.5 s、p95 ≤3 s、吞吐 ≥25 tok/s；30B-A3B-Q4 同条件 p95 ≤8 s、吞吐 ≥12 tok/s。阈值登记在注册表 `perf_budget`，CI 每日基准超阈告警。 |
| NFR-03-03 | 资源 | 常驻内存（权重 + KV + 运行时）≤ `ram_budget_gb`（默认 20.0，24GB 留 4GB 给 Python/环境/OS）；实测值写 `runs/<run_id>/config.json#inference.resident_gb_measured`。 |
| NFR-03-04 | 可复现 | 采样参数属 `harness/config.json#inference`（可演化，被 `config_hash` 覆盖）；`configs/models.yaml` 是静态注册表，M1–M3 不参与演化，只允许人工变更并记 CHANGELOG。 |
| NFR-03-05 | 可观测 | 一次调用 = 一条结构化埋点；禁止自建日志格式（Spec §8.2）。常规日志只存 `prompt_digest`，原文仅 `debug_dump_raw=true` 时落 `runs/<run_id>/raw/`。 |
| NFR-03-06 | 离线 | 零外网；`llama-server` 只监听 `127.0.0.1`；加载前校验权重 `sha256`，不匹配抛 `E_LLM_UNAVAILABLE`。 |
| NFR-03-07 | 可维护 | 新增后端只需实现 5 个方法 + `name`/`model_id` 属性；同一套一致性测试套件覆盖全部实现。 |

## 6. 接口契约

```python
# dice_ratchet/inference/backend.py
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
OutcomeKind = Literal["action", "degraded", "error"]
BackendName = Literal["mlx", "llama_cpp", "llama_server", "mock"]

@dataclass(frozen=True)
class Message: role: Literal["system", "user", "assistant"]; content: str

@dataclass(frozen=True)
class Usage:
    prompt_tokens: int; completion_tokens: int; latency_ms: int; ttft_ms: int | None = None
    prefill_ms: int | None = None; decode_ms: int | None = None

@dataclass(frozen=True)
class BackendHealth:
    state: Literal["unloaded", "loading", "ready", "degraded", "error"]; backend: BackendName
    model_id: str | None; resident_gb: float | None; inflight: int; last_error_code: str | None

@dataclass(frozen=True)
class GenerationResult:
    text: str; parsed: dict[str, Any] | None; usage: Usage; attempts: int   # attempts ∈ 1..max_attempts
    retry_reasons: list[str]                                                # ["ValidationError:$.action.kind"]
    finish_reason: Literal["stop", "length", "timeout", "error"]; outcome_kind: OutcomeKind
    model_id: str; backend: BackendName; prompt_digest: str                 # "sha256:<hex>"
    raw_texts: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class ModelSpec:                                     # configs/models.yaml 一行的类型化形态
    model_id: str; backend: BackendName; weights_path: str; weights_sha256: str; quantization: str
    tokenizer_path: str | None; context_length: int; kv_cache_bits: int; resident_gb_est: float
    kv_gb_per_1k_tokens: float; max_inflight: int; supports_grammar: bool; supports_json_mode: bool
    default_sampling: dict[str, float | int]; perf_budget: dict[str, float]
    fallback_backend: BackendName | None = None; server_args: list[str] = field(default_factory=list)

@runtime_checkable
class Backend(Protocol):
    """MOD-03 唯一后端契约。实现：MLX / LlamaCpp / LlamaServer / Mock 四者。"""
    name: BackendName          # 只读属性
    model_id: str
    async def load(self, spec: ModelSpec) -> None:
        """加载权重与 tokenizer；幂等。失败抛 E_LLM_LOAD_FAILED / E_LLM_OOM。"""
    async def unload(self) -> None:
        """释放权重、KV 缓存、子进程；幂等。"""
    async def generate(
        self, messages: list[Message], *, max_tokens: int = 256, temperature: float = 1.0,
        top_p: float = 0.95, top_k: int = 0, repetition_penalty: float = 1.0, seed: int | None = None,
        stop: list[str] | None = None, timeout_ms: int = 30_000,
        grammar: str | None = None, json_mode: bool = False,
    ) -> GenerationResult:
        """自由文本生成；每次尝试均记埋点。grammar/json_mode 由 generate_json 注入。"""
    async def generate_json(
        self, messages: list[Message], schema: dict[str, Any], *, max_attempts: int = 3,
        retry_backoff_ms: int = 200, total_timeout_ms: int = 8_000, **sampling: Any,
    ) -> GenerationResult:
        """约束解码 → schema 校验 → 修复重试。全部失败抛 E_LLM_SCHEMA_INVALID。"""
    async def health(self) -> BackendHealth:
        """无副作用探针（不触发推理）。"""

class BackendManager:  # 单例门面（v1.0 误写的父类 BuildManagerFacade 已删除）
    def __init__(self, registry_path: str = "configs/models.yaml", *,
                 mode: Literal["train", "eval"] = "train", ram_budget_gb: float = 20.0,
                 infra: "InfraFacade | None" = None) -> None
    def get(self, model_id: str) -> Backend                         # 已加载则直接返回，否则 E_LLM_UNAVAILABLE
    async def load(self, model_id: str) -> Backend                  # 幂等 + single-flight
    async def unload(self, model_id: str) -> None
    async def unload_all(self) -> None
    def session(self, model_id: str) -> AbstractAsyncContextManager[Backend]   # 退出自动 unload
    def estimate_required_gb(self, model_id: str, *, max_inflight: int, context_length: int) -> float
    async def generate_json(self, model_id: str, messages: list[Message],
                            schema: dict[str, Any], **kwargs: Any) -> GenerationResult
    async def health(self) -> dict[str, BackendHealth]
```

`DECISION_SCHEMA`（唯一文件 `schemas/decision.schema.json`，Spec v1.3 §4.3；**扁平、无条件关键字，所以 GBNF 可以直接编译**。kind 与字段名逐字取自 GAME-01 §4 分派表，kind 对应的必填字段由 MOD-04 的语义层校验）：

```python
DECISION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False, "required": ["reasoning", "plan"],
    "properties": {
        "reasoning": {"type": "string", "maxLength": 200},
        "plan": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False, "required": ["kind"],
            "properties": {
                "kind": {"enum": ["play_face", "discard_die", "purify", "use_ability", "end_turn", "map_choose",
                                  "shop_buy", "shop_refresh", "shop_remove", "shop_leave", "event_choose",
                                  "treasure_pick", "forge_pick", "shrine_choose", "rest_choose", "relic_swap"]},
                "dieId": {"type": "string", "maxLength": 16}, "slotId": {"type": "string", "maxLength": 16},
                "faceIndex": {"type": "integer", "minimum": 0, "maximum": 11},
                "targetId": {"type": "string", "maxLength": 16}, "abilityId": {"type": "string", "maxLength": 32},
                "value": {"type": "integer", "minimum": 0, "maximum": 99},
                "nodeId": {"type": "string", "maxLength": 16}, "itemId": {"type": "string", "maxLength": 32},
                "optionId": {"type": "string", "maxLength": 32}, "defId": {"type": "string", "maxLength": 48},
                "candidateId": {"type": "string", "maxLength": 32}, "relicId": {"type": "string", "maxLength": 48}}}}}}
```

`plan` 的长度（`player_turn` 为 1..12，其余 phase 为 1）由 MOD-04 按当前 phase 在语义层校验，不写进 schema，以保持 schema 与 phase 无关、可缓存编译。

**GBNF 支持子集（`compile_grammar()`）**：`type ∈ {object,string,integer,number,boolean,array}`、`enum`、`required`、`additionalProperties:false`、`maxLength`、`minimum`/`maximum`。另支持 `minItems/maxItems`。遇 `allOf`/`if`/`$ref`/`oneOf` 返回 `None`（= 无 grammar 可用，**不是错误**），自动降级 JSON mode + 运行时 `jsonschema` 校验；禁止静默忽略关键字（Spec P-09 结案）。`DECISION_SCHEMA` 必须能编译出 grammar，由单测保证。

**两个后端实现的差异（显式声明，不隐藏）**

| 维度 | `MLXBackend`（`mlx-lm`） | `LlamaCppBackend` / `LlamaServerBackend` |
|---|---|---|
| 运行形态 | 进程内 `mlx_lm.load()` + `stream_generate()`，`asyncio.to_thread` 不阻塞事件循环 | 进程内 `Llama.create_completion`；或子进程 `llama-server` + `POST /v1/chat/completions` |
| 结构化输出 | JSON mode：自定义 logits 白名单掩码；**无 GBNF**（`supports_grammar: false`） | GBNF：`LlamaGrammar.from_string(gbnf)`；server 模式走请求体 `"grammar"` 字段 |
| 并发 | `max_inflight=1` | server 模式 `--parallel 4 --cont-batching` → `max_inflight=4`；进程内 1 |
| 卸载 | 释放引用 + `mx.clear_cache()` + `mx.metal.clear_cache()` | 进程内置 `None`；server 发 `SIGTERM` 并 `wait()` |
| 典型失败 | Metal OOM → 包装 `E_LLM_OOM` | server 未就绪/端口占用 → `E_LLM_LOAD_FAILED`；5xx → `E_LLM_BACKEND_ERROR` |
| 选型 | 阶段一默认（Apple Silicon 更快、迭代快） | 阶段二默认（30B MoE GGUF 生态成熟 + GBNF 稳定），需真并发时用 server 模式 |

两者对上层完全等价：同一份 `DECISION_SCHEMA`、同一套 `GenerationResult`、同一组 `E_LLM_*`；差异只由注册表 `supports_grammar` / `max_inflight` 驱动降级路径。

### 6.1 `generate_json` 的按调用方输出预算（v1.7 新增；Spec v2.3 B-47，F37）

```python
def generate_json(self, messages, schema, *, mode: str,
                  sampling: Mapping[str, Any], seed: int,
                  max_tokens: int | None = None) -> Any: ...
```

`max_tokens` 是**按调用方**给的输出预算：`None` ⇒ 用 `inference.max_tokens`。`BackendManager`
把预算透传给 `guard_context`（`est_prompt + max_tokens ≤ context_length`）与实际生成。

**为什么必须按调用方给**（实测，2026-10-09）：不同调用方的输出体量差一个数量级——决策点是
一个动作 JSON（~200 token），而 MOD-07 的 harness 提案是「unified diff + rationale +
expected_effect」（实测需 **~500** completion token，且随 `max_diff_chars=4000` 上探）。
提案沿用 256 时响应一律 `finish_reason=length` 截断、JSON 在半个对象里断掉 → 被报成
`E_LLM_SCHEMA_INVALID` → **proposer 永远 0 候选、闭环静默定格**。

**禁止**用「全局抬高 `inference.max_tokens`」解决：2048 时 `mock`（`context_length=4096`）
实测触发 **71 例** `E_LLM_CONTEXT_OVERFLOW`（`est_prompt 2052 + 2048 > 4096`），并吃掉
`qwen3-8b`（8192）臂的余量。按调用方给预算既不打断小窗口模型，也不改 `config_hash`。

## 7. 数据模型与存储

`configs/models.yaml`（flow-style 仅为文档紧凑；落盘用等价块式 YAML）：

```yaml
ram_budget_gb: 20.0            # 24GB 硬上限留 4GB 余量；registry_version: 1
models:
  qwen3-8b-q4:                 # 阶段一（M1）；weights_path 下文件 gitignore，仅登记 weights_sha256
    backend: mlx
    weights_path: models/Qwen3-8B-4bit
    tokenizer_path: models/Qwen3-8B-4bit
    quantization: 4bit
    context_length: 8192; kv_cache_bits: 8; resident_gb_est: 4.6077   # ⚠️ 2026-10-14 修订：M1 实测回填（NFR-03-03）；原估 5.6 偏保守
    kv_gb_per_1k_tokens: 0.15
    max_inflight: 1; supports_grammar: false; supports_json_mode: true; fallback_backend: llama_cpp
    default_sampling: {temperature: 0.7, top_p: 0.95, top_k: 0, repetition_penalty: 1.05}
    perf_budget: {p50_latency_ms: 1500, p95_latency_ms: 3000, min_tok_per_s: 25}
  qwen3-30b-a3b-q4:            # 阶段二；见预算结论：32768 / 16384 均超预算
    backend: llama_server
    weights_path: models/Qwen3-30B-A3B-Q4_K_M.gguf
    quantization: q4_k_m
    context_length: 8192; kv_cache_bits: 8; resident_gb_est: 18.8; kv_gb_per_1k_tokens: 0.05
    max_inflight: 1; supports_grammar: true; supports_json_mode: true
    server_args: ["--parallel", "1", "--host", "127.0.0.1"]   # port 由 manager 分配
    perf_budget: {p50_latency_ms: 5000, p95_latency_ms: 6000, min_tok_per_s: 10}   # 阶段一 qwen3-8b-q4 实测回填后的值
```

**内存预算公式**：`required_gb = resident_gb_est + kv_gb_per_1k_tokens × (context_length/1000) × max_inflight + 1.0`（1.0 = 临时缓冲）。`required_gb > ram_budget_gb` → 在 acquire 前拒绝并抛 `E_LLM_OOM`。代入 30B：`32768/4` → `18.8 + 0.05×32×4 + 1.0 = 26.2 > 20.0`（拒绝）；`16384/2` → `21.4`（拒绝）；`8192/1` → `20.2`（临界，需实测调 `kv_cache_bits=4` 或修正 `resident_gb_est`）。**已裁决（Spec v1.3 P-10 → §8.6）**：30B-A3B 在 24GB 机器上默认 `context_length=8192, max_inflight=1`；"评估要并发跑多局"通过**局级并发（`loop.eval_concurrency` 个 episode，默认 2，共享一个串行推理队列）**实现，而非模型级批处理——与 Spec §8.6 成本可控一致，并避免 OOM 抖动。

**存储落点（均由 MOD-09 写，本模块只产出对象）**：`runs/<run_id>/metrics/llm_calls.jsonl`（每次尝试一行，§13）；`runs/<run_id>/episodes/<split>/<episode_key>.jsonl` 步内嵌 `llm_latency_ms`/`prompt_tokens`/`completion_tokens`/`llm_raw`（Spec §4.5）；配置来源 `configs/models.yaml` + `harness/config.json#inference`，二者均被 `config_hash` 覆盖。无独立数据库。

### 7.1 三角色装配、实验臂与 provenance（v1.6 新增；Spec v2.0 B-42）

**角色**：`policy`（游戏决策）/ `reflection`（局末反思）/ `proposer`（harness 修改提案）三个角色**只依赖 `InferenceClient` 契约**，绑定全部来自配置层。优先级（唯一实现 `roles.py::_resolve_role`，两个入口共用避免漂移）：

```
experiment.roles.<role>          > experiment.arms[experiment.arm].<role> > models.<role> > inference.model_id
（手工覆盖，最高）                    （实验臂差分）                            （角色默认）      （兜底，CLI --model 一次覆盖全部角色）
```

空字符串 = 未绑定，继续向下继承。**fail-closed**：未知角色 / 未知臂 / 空 `model_id` / 注册表里不存在的模型 / `provider=api` 但未登记 key 环境变量 —— 一律显式报错，**绝不静默降级到别的模型**（验收标准 A 第 5 条）。臂定义见 `configs/default.yaml#experiment.arms`（`A0/A1/B0/B1/C0/C1`，六臂只做角色差分；`harness` 字段只**登记**该臂的 harness 是否演化）。

```yaml
# configs/default.yaml 片段
models:
  policy: ""; reflection: ""; proposer: ""   # 空 = 用 inference.model_id
experiment:
  arm: A1                                    # 默认臂；M2 闭环主力臂见 Spec §8.6 B-43（= C1）
  roles: {}                                  # 手工覆盖，优先级最高
  arms:
    C1: {policy: deepseek-flash, reflection: deepseek-flash, proposer: deepseek-flash, harness: evolve}
```

**provenance**：每个 run 落 `runs/<run_id>/models_provenance.json`（原子写），记三角色各自的 `model_id` / `provider` / `backend` / `model_revision` / `online` / `api_key_present`，以及臂、`harness` 模式与 `config_hash`。**key 本体绝不落盘**（`api_key_present` 只是布尔）。装配缓存键含 `config_hash` ⇒ 换臂/换模型必然是新的装配。

**DeepSeek 注册表条目（`provider=api`，Spec §8.6）**：

```yaml
  deepseek-flash:
    backend: deepseek; provider: api; online: true
    context_length: 1048576            # 实测 1M
    api_base: https://api.deepseek.com/v1; api_model: deepseek-flash
    api_key_env: DEEPSEEK_API_KEY      # 只登记变量名；真值来自环境变量或 inference.api_key_file
    api_supports_seed: false           # 不承诺跨时间确定性 → 采样照记 + 响应按 request hash 归档 + 变化标 llm.nondeterministic
    api_thinking: disabled             # flash 默认开思考模式会吃光 max_tokens 使 content 返空（每次白花一次调用）
    model_revision: DeepSeek-V4.1-Flash
    cost_per_1k_input: 0.002; cost_per_1k_output: 0.008; cost_currency: CNY; cost_basis: peak
    supports_grammar: false            # 无 GBNF，走 response_format=json_object + jsonschema 校验
    default_sampling: {temperature: 0.0, top_p: 1.0, top_k: 0, repetition_penalty: 1.0}
```

**成本口径（2026-10-08 实测，人民币、高峰档上界）**：`deepseek-flash` 输入 ¥1/百万 token（缓存未命中）、输出 ¥4/百万；登记的值取**高峰档双倍**（空闲档是它的一半）。`deepseek-v4-pro`（更强更贵：输入 ¥9 / 输出 ¥27）已登记但**当前不被任何臂使用**。

## 8. 关键流程

```python
# dice_ratchet/inference/backend.py::BackendManager.generate_json（伪代码，真实控制流）
async def generate_json(self, model_id, messages, schema, **kw) -> GenerationResult:
    spec = self._spec(model_id)                                     # 未知 → E_LLM_UNAVAILABLE
    sampling = validate_sampling(kw, mode=self.mode, spec=spec)      # FR-03-07，越界即抛
    grammar = compile_grammar(schema) if spec.supports_grammar else None   # None → 降级 JSON mode
    deadline = now_ms() + kw.get("total_timeout_ms", 120000)
    async with self._inflight(spec, messages, sampling):             # FR-03-08，超预算 E_LLM_OOM
        result = await self._attempt_loop(spec, messages, schema, grammar, sampling, deadline)
    log_call(result)                                                 # NFR-03-05
    return result.decorate(model_id=model_id, backend=spec.backend)

async def _attempt_loop(spec, messages, schema, grammar, sampling, deadline) -> GenerationResult:
    convo, raw_texts, retry_reasons = list(messages), [], []
    for attempt in range(1, max_attempts + 1):
        if deadline - now_ms() <= 0: raise E_LLM_TIMEOUT(attempts=attempt - 1)   # 不留半截结果
        try:
            out = await asyncio.wait_for(self._backend.generate(
                convo, grammar=grammar, json_mode=True, **sampling), timeout=(deadline - now_ms()) / 1000)
        except asyncio.TimeoutError: raise E_LLM_TIMEOUT(...)
        except OOMError as e:        raise E_LLM_OOM(...) from e
        raw_texts.append(out.text)
        try:
            parsed = json.loads(out.text); jsonschema.validate(parsed, schema)     # ② 校验
            return make_result(parsed, attempts=attempt, retry_reasons=retry_reasons, usage=out.usage)
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            retry_reasons.append(f"{type(e).__name__}:{getattr(e, 'json_path', '$')}")
            if attempt == max_attempts: raise E_LLM_SCHEMA_INVALID(attempts=attempt, reasons=retry_reasons)
            await asyncio.sleep(backoff_ms(attempt) / 1000)            # 200, 400 ms
            convo = repair_messages(messages, bad=out.text, error=render_error(e), schema=schema)  # ③
```

**修复 prompt（`schema.py::repair_messages`，只纠错、零领域知识）**：在 MOD-04 传入的 `messages` 之后追加一轮对话。system："你是 JSON 修复器。只输出一个符合给定 JSON Schema 的 JSON 对象，不要解释，不要 markdown 代码块。"；user："你的上一次输出无法通过校验。--- 原始输出 --- {bad_output} --- 校验错误 --- {jsonschema message} @ {json_path} --- 目标 Schema --- {schema_json} 请输出修正后的 JSON。"。**已裁决（Spec v1.4 B-28）**：修复 prompt 模板由本节定义，并由 `schema.py` 独占拥有，MOD-04 与 harness 都不得覆盖。

**时序**：MOD-04 → `generate_json(messages, DECISION_SCHEMA)` → 校验采样参数（越界 `E_LLM_BACKEND_ERROR`）→ 内存预算检查（超则 `E_LLM_OOM`，零提交）→ `acquire(semaphore)` → attempt 1..3：`generate(grammar, json_mode)` → `json.loads` → `jsonschema.validate`；失败则 `sleep(200/400)` + `repair_messages` 再试 → 成功返回 `GenerationResult(parsed, usage, attempts, retry_reasons)` → MOD-04 做语义校验（`legal_actions()`）后 `step`。

## 9. 配置项

优先级（Spec §5）：CLI 参数 > 环境变量 `DICERATCHET_INFERENCE_*` > `configs/default.yaml#inference` > 代码默认值。

| 键（`inference.` 前缀） | 类型 | 默认 | 含义 |
|---|---|---|---|
| `registry_path` / `model_id` | str | `configs/models.yaml` / `qwen3-8b-q4` | 注册表路径 / 默认模型（阶段二切 `qwen3-30b-a3b-q4`） |
| `mode` | enum | `train` | `train`/`eval`；`eval` 收紧采样并强制 seed（FR-03-07） |
| `ram_budget_gb` | float | `20.0` | 内存预算上限；注册表可覆盖 |
| `max_attempts` / `retry_backoff_ms` / `total_timeout_ms` | int | `3` / `200` / `120000` | 尝试次数（含首答）/ 退避基数（指数倍增）/ 全程墙钟预算（2026-10-04 实测由 8000 回填；**2026-10-06 起 dataclass 默认与 `configs/default.yaml` 一致**，此前 `InferenceConfig.total_timeout_ms` 停留在 8000——只影响「不读 YAML 直接构造配置」的路径，`config_hash` 不变；守门用例见 `tests/unit/infra/test_config.py::test_inference_defaults_match_yaml`） |
| `generate_timeout_ms` / `max_tokens` / `reasoning_max_tokens` | int | `30000` / `256` / `160` | 单次 `generate` 超时 / 决策 tok 上限（Spec §8.6）/ 分两次生成时 `reasoning` 预算 |
| `context_overflow_policy` | enum | `reject` | `reject` → `E_LLM_CONTEXT_OVERFLOW`；`truncate` → 截断最早的 user 消息 |
| `eval_temperature` / `train_temperature_min` | float | `0.0`（固定）/ `0.7` | 评估温度（B-14，不可配置为其他值）/ 训练温度下限（计划 §3.3） |
| `seed` | int\|null | `null` | 采样 seed；评估模式必须非空（由 MOD-09 seed 注册表传入） |
| `cache_prompt` | bool | `true` | 复用前缀 KV（system + few-shot 不变时显著降延迟）。**⚠️ 2026-10-14 修订（F62 批次的实测）**：该键**只被本地 `llama_backend` 消费**（`llama-server` 请求体）⇒ 对 **API 臂（`C1`）无收益**（无本地 prefill）；且注册表只有 `cost_per_1k_input/output`、`llm_api/*.json` 的 `usage` **没有 cache 字段** ⇒ **API 侧命中率不可直接测**（`cost_estimate` 一律按未命中计价，会系统性高估） |
| `kv_cache_bits` / `max_inflight` | int | 注册表值 | 覆盖注册表；`kv_cache_bits=4` 可救 30B 预算（Q-4）；评估模式强制 `max_inflight=1` |
| `debug_dump_raw` | bool | `false` | 落 `runs/<run_id>/raw/llm/<call_id>.txt`；默认只存 digest |
| `skip_hash_check` | bool | `false` | 对应 `DICERATCHET_INFERENCE_SKIP_HASH=1`；CI 必须为 false（Q-6） |
| `llama_server_startup_timeout_ms` / `llama_server_port` | int | `60000` / `0` | 等 server `/health` 就绪（超时 `E_LLM_LOAD_FAILED`）/ `0` = 自动分配空闲端口 |
| **`api_key_file`**（v1.6 新增） | str | `.env` | 本地密钥文件路径（相对仓库根）。语义：① **真实环境变量优先**，已 `export DEEPSEEK_API_KEY=…` 时本文件不覆盖；② 只读注册表里声明的 `api_key_env` 名字（其余名字一律不生效，**该文件不能用来改配置**）；③ key 只进内存，不写 `harness/`、run JSON、事件或日志；④ 空字符串 = 关闭该文件，只用环境变量。`.env` 被 `.gitignore` 忽略 |
| **`models.policy` / `models.reflection` / `models.proposer`**（v1.6 新增） | str | `""` | 三角色的默认 `model_id`；空 = 用 `inference.model_id`。优先级见 §7.1 |
| **`experiment.arm` / `experiment.roles` / `experiment.arms`**（v1.6 新增） | str / map / map | `A1` / `{}` / 六臂 | 实验臂与角色差分；臂定义**计入 `config_hash`** ⇒ 换臂即换 run-id 族（Spec F10），六臂产物不互相污染。详见 §7.1 |

## 10. 错误处理与降级

```python
# dice_ratchet/inference/errors.py
class InferenceError(RuntimeError):
    code: str; attempts: int; retryable: bool; detail: dict[str, Any]
```

| 错误码 | 触发 | `retryable` | 上层行为 |
|---|---|---|---|
| `E_LLM_UNAVAILABLE` | `model_id` 不在注册表 / 权重缺失 / `sha256` 不匹配 | False | 立即 fallback heuristic；该局 `exclude_reason=E_LLM_UNAVAILABLE` |
| `E_LLM_LOAD_FAILED` | 权重加载异常、server 起不来、端口占用 | True（退避后换 `fallback_backend` 重试 1 次） | 仍失败 → fallback heuristic，标注"模型不可用"（Spec §8.5） |
| `E_LLM_OOM` | Metal/系统 OOM；或预算检查前置拒绝 | True（降 `max_inflight`/`kv_cache_bits` 后重试 1 次） | 仍失败 → fallback heuristic，run 级 `degraded_runs += 1` |
| `E_LLM_TIMEOUT` | 超 `total_timeout_ms` 或 `generate_timeout_ms` | True（最多 1 次） | 仍失败 → fallback heuristic；**该局不计入评估样本**（Spec §8.5） |
| `E_LLM_SCHEMA_INVALID` | 三次尝试后仍不满足 schema | False | 返回 `outcome_kind="error"`；MOD-04 把它计为该决策的**一次失败尝试**（Spec §4.3 L0 层：修复过程本身不计非法） |
| `E_LLM_CONTEXT_OVERFLOW` | `prompt_tokens + max_tokens > context_length` 且策略 `reject` | False | MOD-04 缩短上下文后重试（上下文裁剪策略归 MOD-04） |
| `E_LLM_BACKEND_ERROR` | 采样参数越界、server 5xx、grammar 编译失败且后端无 JSON mode | 参数类 False / 后端类 True（1 次） | 参数类直接上报；后端类重试后仍失败 → fallback heuristic |

**降级契约**：① 本模块**只抛码，绝不静默兜底**——不返回空动作、不猜测 JSON。② `outcome_kind="degraded"` 仅用于"约束解码不可用但 JSON 校验通过"这类可用但质量降级的情形（如 `supports_grammar=false` 时纯 JSON mode 一次通过），原因记 `degraded_reason`。③ **局级降级**（模型不可用/超时/OOM → heuristic agent）由 MOD-04/06 执行，必须写 `episode_end.degraded=true` + `exclude_reason=<E_LLM_*>`，MOD-08 据此把该局从评估样本剔除（Spec §8.5）。**已回填（Spec v1.3 §4.5 `episode_end` 行，P-12）**：字段名为 `degraded: bool` 与 `exclude_reason: str|null`。

## 11. 测试计划

| 层（`tests/unit/inference/`、`tests/integration/`） | 用例（给定-当-则） |
|---|---|
| 单测 `test_backend_protocol.py` | 给定任一后端实现，当跑同一套一致性套件（`load → health → generate → unload`），则 5 个方法 + `name`/`model_id` 齐备且 `isinstance(x, Backend)` 为真（`@runtime_checkable`，NFR-03-07）。 |
| 单测 `test_schema_compile.py` | 给定 `DECISION_SCHEMA`，当 `compile_grammar()`，则输出能被 `llama_cpp.LlamaGrammar` 解析的合法 GBNF（不得返回 None）；给定含 `allOf` 的 schema，则返回 `None` 而不抛错。 |
| 单测 `test_validate_roundtrip.py` | 给定 12 正例 / 9 反例，当 `jsonschema.validate`，则正例全过、反例全失败且 `json_path` 指向 `$.action.kind` 等预期路径（G5）。 |
| 单测 `test_sampling_guard.py` | 给定 `mode="eval", temperature=0.3`（或 0.9），当 `generate_json`，则抛 `E_LLM_BACKEND_ERROR`，且**零埋点**、`MockBackend.generate` 调用数为 0。 |
| 单测 `test_memory_budget.py` | 给定 `qwen3-30b-a3b-q4, context_length=32768, max_inflight=4`，当 `estimate_required_gb`，则 > 20.0，且 `generate_json` 抛 `E_LLM_OOM` 而零提交。 |
| 单测 `test_retry_backoff.py` | 给定 `MockBackend` 前两次坏 JSON、第三次合法，当 `generate_json`，则 `attempts==3`、`len(retry_reasons)==2`、假时钟观测退避 `[200, 400] ms`。 |
| 单测 `test_timeout.py` | 给定 `total_timeout_ms=1000`、每次 `sleep(800ms)`，当 `generate_json`，则抛 `E_LLM_TIMEOUT` 且 `raw_texts` 不含半截输出。 |
| 单测 `test_cli_smoke.py` | 给定 `_robustness` 返回 `passed=false`（未达 M1 阈值），当 `inference robustness`，则**退出码 1**（领域失败，非 2/3 的配置/模型错误）且报告仍落盘；给定 `passed=true`，当带 `--out` 跑，则退出码 0 且写到指定路径；给定未知模型，当 `inference probe`，则退出码 3 且**不产出报告文件**（`metrics/` 目录由 `init_run_dir` 预建，故判据是文件不存在而非目录不存在）。（2026-10-06 复核 M-5 补：集成套件 `test_inference_cli.py` 覆盖成功路径，本文件补它观测不到的阈值失败分支。） |
| 单测 `test_repair_prompt.py` | 给定坏 JSON + `ValidationError`，当 `repair_messages`，则追加的 user 消息同时含原始输出、错误消息、`json_path`、schema JSON，且**不修改**原 `messages`（不可变契约）。 |
| 集成 `test_inference_robustness.py` | 给定 200 条真实固定 prompt（MOD-04 决策模板快照），当对 `qwen3-8b-q4` 跑 `generate_json`，则首答通过率 ≥ 90%、三级全失败率 ≤ 1%，产出 `runs/<run_id>/metrics/format_robustness.json`（M1 交付物）。**2026-10-06 复核 M-1**：该工件必须与**当前快照/模板**同批产出——旧工件（2026-10-04）早于 F13/F23/F25 的模板改动，已用当前快照重跑 n=200 覆盖（首答 0.94 → **0.995**、`schema_fail_rate` 0.005 → **0.0**）；换快照/模板后重跑并覆盖属 G2 要求的显式变更。 |
| 集成 `test_determinism.py` | 给定 `mode="eval", temperature=0, seed=1042`，当同 prompt 连跑 3 次，则 `text` 逐字节相同且 `prompt_digest` 相同；换 seed 后允许不同（G3/INV-6）。 |
| 集成 `test_concurrency.py` | 给定 `max_inflight=1` 与 4 个并发 `generate_json`，当 `asyncio.gather`，则观测最大 `inflight==1`、全部成功、总墙钟 ≈ 4 × 单次延迟（验证局级并发的排队语义）。 |
| 集成 `test_backend_swap.py` | 给定同 prompt 分别打到 `MLXBackend` 与 `LlamaCppBackend`，当比较 `parsed`，则两者都通过 `DECISION_SCHEMA`（**不**断言文本相同，见 NFR-03-01 L2）。 |
| 集成 `test_unload_releases.py` | 给定已加载模型，当 `unload()`，则 `health().state=="unloaded"` 且 `resident_gb < 1.0`（macOS 用 `psutil` 断言）。 |

**`MockBackend`（测试基石，零依赖）**：`MockBackend(script: list[str | Exception], *, delay_ms: int = 0, name: BackendName = "mock", model_id: str = "mock-1")` —— 按脚本逐次返回预设输出，支持注入延迟 / 异常 / OOM，供全部单测使用。

## 12. 验收标准（DoD）

| 门 | MOD-03 具体要求 | 证据 |
|---|---|---|
| G1 单测 | §11 单测全绿；`dice_ratchet/inference` 行覆盖率 ≥ 80%（`schema.py` ≥ 90%）；后端一致性套件 100% 通过 | `pytest tests/unit/inference -q --cov=dice_ratchet.inference` |
| G2 Golden | `DECISION_SCHEMA`、其 GBNF 产物、修复 prompt 快照入 `tests/golden/inference/`（`decision_schema.json` / `.gbnf` / `repair_prompt.txt`），变更必须显式更新并在 PR 说明 | golden diff 为空 |
| G3 确定性 | `test_determinism.py` 通过；评估模式 3 次重跑 `llm_raw` 逐字节一致 | CI 日志 |
| G4 泄漏审计 | 输出只含模型文本与埋点，不含 `seed`/`full_state`/未公开信息；`audit leak` 在本模块自测 run 上 0 告警 | `python -m dice_ratchet audit leak --run-id <run_id>` |
| G5 Schema 校验 | `DECISION_SCHEMA`、`InferenceCallRecord`、`models.yaml` 三者均有 schema 定义 + round-trip 测试 | `test_validate_roundtrip.py`、`test_registry_schema.py` |
| G6 可复现命令 | 可复制执行成功：`python -m dice_ratchet inference probe --model qwen3-8b-q4`；`... inference bench --model qwen3-8b-q4 --n 50`；`... inference robustness --model qwen3-8b-q4 --n 200` | 退出码 0 + 报告落盘 |
| M1 对齐 | 8B agent 与 MOD-04 联调跑通；**JSON 格式鲁棒性报告**入档（Spec §7 M1）；random / heuristic / fixed-prompt 三基线不被本模块阻塞 | `runs/<run_id>/metrics/format_robustness.json` |

## 13. 指标与埋点

`InferenceCallRecord`（每次尝试一行 → `runs/<run_id>/metrics/llm_calls.jsonl`）：

```jsonc
{"call_id":"c-000412","run_id":"a1b2c3d4-9f0e1a2b-20260930T120000Z","ts":"2026-09-30T12:00:00Z",
 "event":"llm_call","episode_id":"e-000123","generation":3,"harness_version":7,"seed":1042,"step":14,
 "model_id":"qwen3-8b-q4","backend":"mlx","mode":"eval","purpose":"decision",
 "schema_id":"action.v1","caller":"mod-04","prompt_digest":"sha256:...",
 "prompt_tokens":932,"completion_tokens":61,"max_tokens":256,
 "temperature":0.0,"top_p":1.0,"top_k":0,"repetition_penalty":1.05,"sampling_seed":1042,
 "grammar_used":false,"json_mode":true,
 "latency_ms":812,"ttft_ms":180,"prefill_ms":210,"decode_ms":600,"tok_per_s":101.7,
 "attempt":1,"attempts":3,"retry_reasons":["ValidationError:$.action.kind"],
 "outcome_kind":"action","finish_reason":"stop","degraded_reason":null,"error_code":null,
 "resident_gb":5.7,"inflight":1}
```

聚合指标 `runs/<run_id>/metrics/inference_agg.json`（MOD-08 消费）：`latency_ms` p50/p95/max（按 `model_id × purpose` 分组）；`tok_per_s = completion_tokens / decode_ms × 1000`；`first_pass_valid_rate = (attempts==1 且 outcome_kind=="action") / 总调用`（M1 格式鲁棒性核心）；`schema_fail_rate`（目标 ≤ 1%）；`retry_histogram`（`attempts` 分布）；`degraded_call_rate`；`error_rate_by_code`（目标 `E_LLM_TIMEOUT` ≤ 2%、`E_LLM_OOM` == 0）；`calls_per_episode` / `tokens_per_episode`（成本，Spec §8.6）。

口径要求：一次调用一条记录，禁止聚合后丢失单次细节；`prompt_digest = "sha256:" + sha256(␟ 连接各 `f"{m.role}\x1f{m.content}"`)，可离线复算。

## 14. 依赖与风险

| 依赖 | 形态/版本 | 说明 |
|---|---|---|
| MOD-09 infra | 必须先交付 `config.py`/`logging.py`/`run_id` | Spec §6：MOD-03 依赖 MOD-09；通过 `InfraFacade` 协议松耦合，测试注入 `NullInfra` |
| `mlx` / `mlx-lm` | `mlx-lm ≥ 0.20` | 阶段一后端；替代路径见 R-1 |
| `llama-cpp-python` 或 `llama-server` | pip / Homebrew / 源码 | GBNF 与 server 模式来源 |
| `jsonschema` | `≥ 4.21`（draft 2020-12） | 校验与错误路径 `json_path` |
| 模型文件 | `models/Qwen3-8B-4bit`、`models/Qwen3-30B-A3B-Q4_K_M.gguf` | 离线下载、gitignore、登记 `sha256` |

| # | 风险 | 对策 |
|---|---|---|
| R-1 | `mlx-lm` API 快速变动（生成入口签名 / logits processor 接口） | 只用 `mlx_lm.load` + 高层生成入口；MLX 调用全部收敛在 `mlx_backend.py` 一层；每次升级跑 §11 集成套件 |
| R-2 | MLX 无 GBNF，JSON mode 约束强度弱于 llama.cpp | 阶段一即实现 `allOf` 运行时校验 + 修复重试；`format_robustness.json` 按后端对比；不达标则阶段一切 `llama_cpp` |
| R-3 | 30B-A3B-Q4 常驻 18.6GB + KV 逼近 24GB | 默认 `max_inflight=1`、`context_length=8192`、`kv_cache_bits=8`；预算前置拒绝；必要时 `kv_cache_bits=4`（Q-4） |
| R-4 | Metal OOM 可能表现为进程崩溃而非可捕获异常 | 加载前 `estimate_required_gb` 拒绝；`unload` 后强制 `mx.clear_cache()`；集成测试断言 `E_LLM_OOM` 可捕获 |
| R-5 | 确定性随批处理/`--parallel` 退化 | 评估模式强制 `max_inflight=1`、关闭 `--cont-batching`；承诺口径限定 L1（NFR-03-01） |
| R-6 | 训练温度下限 ≥0.7 与"可复现"冲突 | 训练模式复现性以 `obs_digest` + `state_hash` 为准（INV-2/INV-6），文本级一致只在评估模式承诺 |
| R-7 | `llama-server` 子进程泄漏（测试崩溃后残留） | `session()` 上下文管理器 + `atexit` 注册 + 随机端口；测试后断言无残留进程 |

## 15. 里程碑与工作量估算（人日）

| 任务 | 人日 | 里程碑 | 依赖 |
|---|---|---|---|
| `backend.py`：契约 + 数据模型 + `MockBackend` + `BackendManager`（信号量/预算/single-flight） | 2.0 | M1 | MOD-09 骨架 |
| `mlx_backend.py`：load/unload/stream、JSON mode、`to_thread` | 2.0 | M1 | MOD-09 |
| `llama_backend.py`：`llama-cpp-python` + `llama-server` 子进程 + GBNF | 2.5 | M1 | — |
| `schema.py`：Schema 子集 → GBNF、`validate`、修复 prompt | 1.5 | M1 | — |
| `configs/models.yaml` + 内存预算 + 注册表 schema 校验 | 1.0 | M1 | — |
| 埋点记录 + 聚合指标 + `inference probe/bench/robustness` 子命令（挂 MOD-09 CLI） | 1.0 | M1 | MOD-09 CLI |
| 降级 / `E_LLM_*` 全路径 + 错误路径单测 | 1.0 | M1 | — |
| 单测 + 集成（含 8B 真实模型 200 条跑批与调参） | 2.5 | M1 | 模型文件就绪 |
| PRD/README 命令核对（G6） | 0.5 | M1 | 全部 |
| **M1 合计** | **14.0** | | |
| 阶段二 30B-A3B 接入 + 内存/并发调优 + 性能基准 | 3.0 | M2 前 | R-3 |
| 可选：真批处理（同前缀分组 + `--cont-batching`） | 2.0 | M3+ | 视评估吞吐需求 |

**实际交付（M1）**：代码于 **2026-10-04** 落地（提交 `bff9982`，含 MOD-04/08 与 M1 验收修复；MOD-03 主体为 `2103157`）。
本项目**未单独记工**；按提交时间窗折算 ≤ 3 人日（计划 14.0），偏差 ≥ 78%。
**真实权重联调在同一日完成**（M1 验收原判定为「缺权重」，不属于计划内工作量）：落盘 `models/Qwen3-8B-4bit`（4.6 GB）后，
实测暴露并修掉 4 个只有真模型才能发现的缺陷 —— MLX `load()` 形参漂移、`stream_generate` 无 `seed` 形参、
Qwen3 思考模式烧光 token 预算、probe prompt 欠约束（详见 [M1 验收报告 §12](../reviews/2026-10-04-M1验收报告.md)），
并回填 `resident_gb_est` / `weights_sha256` / `perf_budget`（Q-2 阶段一部分、Q-6）。

**§13.2 收口（2026-10-05，`<本轮提交>`）**：

- 主 prompt 快照 `tests/fixtures/inference/robustness_prompts.jsonl` 因 **F13**（`legal_actions`
  超预算压缩）与 **F21/F23/F25**（模板契约补齐）**显式重新生成**（G2：变更需审阅 diff）；生成器
  `regen_robustness_prompts.py` 增加 `--template {decision,fixed_v1}` / `--out`，可产出**同源换模板**的
  对照快照 `robustness_prompts_fixed_v1.jsonl`（F21 的「52% → ?」对照）。
- `inference robustness` 的 `m1_evidence` 门是「真实后端 + 真实快照 + **阈值通过**」三者同时成立
  （`metrics.py`），因此拿着 `--prompts` 跑冻结模板对照时它只作对照、不当 M1 证据；**eval 模式必须
  同时给 `--seed`**（`inference.mode=eval` 要求显式 seed，INV-6/B-14），本轮实测踩到过这个。
- 本轮最终的真实模型证据（主快照 n=200、冻结模板对照 n=200、三基线 n=40）见
  [M1 验收报告 §14.6/§14.15](../reviews/2026-10-04-M1验收报告.md)。

## 16. 开放问题

| # | 问题 | 影响 | 建议决策 |
|---|---|---|---|
| Q-1 | **已裁决（Spec v1.4 B-31）** M1 是否必须同时维护 MLX 与 llama.cpp 两个后端（+4.5 人日）？ | §15 砍 2.5 人日 | M1 只交付 `MLXBackend` + `MockBackend`，`LlamaCppBackend` 与阶段二 30B 一起上；但 `Backend` 契约与一致性套件必须在 M1 定稿 |
| Q-2 | **部分关闭（2026-10-04 实测）** 30B-A3B 在 24GB 上 `concurrent>1` 是否可行？需实测 MoE 真实常驻值（MLX 4bit vs GGUF Q4_K_M） | 决定评估并发是局级排队还是真批处理 | 8B（阶段一）已实测：`resident_gb_measured = 4.6077 GB`（n=30，MLX 进程内），`resident_gb_est` 已回填 5.6 → **4.6077**，预算 `4.6077+0.1229+1.0 = 6.84 ≤ 20`，`max_inflight=1` 维持不动。**30B-A3B 仍未实测**（其权重未落盘）：Q-2 只对阶段一关闭，阶段二启用前必须按 mod-03 `inference bench --model qwen3-30b-a3b-q4` 重测 |
| Q-3 | 评估模式 `temperature=0` 是否会让 MOD-08 的动作熵/坍缩监控失真？ | 影响 `action_entropy_curve` 的解释 | 评估模式保持 `temperature=0` 保可比性；坍缩监控另开"训练模式同 seed 多采样 k=5"专项 run，由 MOD-08 定义 |
| Q-4 | `kv_cache_bits=4` 对 JSON 格式鲁棒性的实测影响未知 | 内存换质量的应急开关 | 默认 8；开启后必须重跑 `format_robustness` 做前后对比 |
| Q-5 | 是否允许本模块"多候选并行采样 + 自选最优"（`n>1`）？ | 与 MOD-04 `rank_actions()` 职责重叠 | **不允许**。MOD-03 只做单次采样；候选排序归 MOD-04 的 harness 工具函数（Spec §4.4） |
| Q-6 | **已关闭（2026-10-04）** 每次加载都校验 `weights_sha256` 是否太慢？ | 启动时间 | 已实测：`models/Qwen3-8B-4bit` 目录摘要（含 `model.safetensors` 4.6 GB）**2.1 s**，`weights_sha256` 已写入 `configs/models.yaml` 并通过 `verify_weights` 校验；开销可接受，默认开启不变，逃生开关仅本地使用。顺带修掉摘要把 HuggingFace 下载缓存（`.cache/**`）算进去导致**同权重不同摘要**的缺陷（M1 验收 F17） |

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版 |
| v1.1 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：评估温度固定为 0.0（P0-4）；`ACTION_SCHEMA` 改为扁平的 `DECISION_SCHEMA`（动作计划，GBNF 可编译，与 MOD-04 共用同一文件，P1-19）；L0 修复失败计为一次尝试（P0-5）；评估并发默认 2 局、共享推理队列；删除误写的父类 `BuildManagerFacade` |
| v1.2 | 2026-09-30 17:49 | P-11、P-24 裁决回填为 Spec B-28/B-31；Q-1 结案 |
| v1.3 | 2026-10-06 | **按 M1 验收复核（M-1/M-5）对齐 §11 测试登记**：① 新增单测 `test_cli_smoke.py` 条目——补集成套件观测不到的 `robustness` **未达阈值退出码 1** 分支（集成 `test_inference_cli.py` 已覆盖成功路径；此前单测口径下 `cli.py` 显示 0%，是"不收集 `tests/integration/`"造成的假象，补后 0% → 42%）；并注明「`init_run_dir` 预建 `metrics/`，失败判据是报告文件不存在」这一易错点。② 集成 `test_inference_robustness.py` 条目补「工件必须与当前快照/模板同批产出」——旧工件早于 F13/F23/F25 模板改动，已用当前快照重跑 n=200 覆盖（首答 0.94 → 0.995、`schema_fail_rate` 0.005 → 0.0）。依据 [M1 验收复核 §M-1/§M-5](../reviews/2026-10-05-M1验收复核.md) |
| v1.5 | 2026-10-06 | **对齐超时默认值的三层漂移**（复核登记 → 已修）：① `InferenceConfig.total_timeout_ms` dataclass 默认 8000 → **120000**（与 `configs/default.yaml` 一致）；② `inference/backend.py` 的同键 `getattr` 兜底 8_000 → **120_000**；③ 连带把 `app.py` / `evals/cli.py` 的 `seed_timeout_s` 兜底（300 / 1800）对齐为 **14400**（`EvalConfig` / `EvalSpec` / YAML 三方原已一致）。依据：`_default_dict()` 取自 dataclass、YAML 逐键覆盖，故改 dataclass 默认**不改变生效值**（仍 120000 / 14400），**`config_hash` 实测不变**（`b6f119ca325e282d`），存量 run 不会 `E_IO_RUN_CONFLICT`；差异只影响「不读 YAML 直接构造配置」的路径。新增守门用例 `tests/unit/infra/test_config.py`（7 条，含源码文本级兜底校验），并做变异验证 |
| v1.4 | 2026-10-06 | **补记 2026-10-04 的真实模型实测回填**（此前只在 §16 Q-2/Q-6 与配置注释里，变更记录缺一条）：权重 `mlx-community/Qwen3-8B-4bit` 落盘（`weights_sha256=20303070…`）、`resident_gb_est` 5.6 → **4.6077**、`perf_budget` 1500/3000/25 → **5000/6000/10**（原目标实测未达成，按实测重设）、`total_timeout_ms` 8000 ms → **120000 ms**（8 s < 实测 p50，正常决策会被判 `E_LLM_TIMEOUT`）；主快照 n=200 实测 `m1_evidence=true`、首答 0.94、`schema_fail_rate` 0.005、`t_d` p50 5.687 s / p95 11.70 s；并登记只有真实权重能暴露的 **F14**（mlx-lm API 漂移：`load(tokenizer_path=)` / `stream_generate(seed=)` / `mx.metal.*` 改名）、**F15**（Qwen3 思考模式烧光 token 预算 → 显式 `enable_thinking=False`）、**F16**（probe/bench 固定 prompt 欠约束）、**F17**（权重摘要误把 HF 下载缓存算进去）。依据 [M1 验收报告 §12](../reviews/2026-10-04-M1验收报告.md) |
| v1.6 | 2026-10-09 | **三角色解耦 + DeepSeek API 后端回填**（对齐 Spec v2.0 B-42；依据 [Issue #1 落地报告](../tasks/2026-10-08-issue-1-角色解耦落地报告.md)）。① §2.1 新增四个文件：`client.py`（三角色共用的 `InferenceClient` 同步契约）、`deepseek_backend.py`、`roles.py`（`RoleModelProvider` / `resolve_role_bindings` / `role_model_ids` / `run_provenance`）、`secrets.py`。② **新增 §7.1**：三角色（`policy`/`reflection`/`proposer`）的绑定**全部来自配置**，四级优先级 `experiment.roles.<role>` > `experiment.arms[arm].<role>` > `models.<role>` > `inference.model_id`，**fail-closed**（未知角色/未知臂/空 `model_id`/注册表缺模型/`provider=api` 缺 key 环境变量一律显式报错，绝不静默降级）；每个 run 落 `runs/<run_id>/models_provenance.json`（三角色各自的 `model_id`/`provider`/`backend`/`model_revision`/`online`/`api_key_present` + 臂 + `harness` 模式 + `config_hash`，**key 本体绝不落盘**）。③ 注册表新增 `deepseek-flash`（`provider=api`、`api_supports_seed=false`、`api_thinking=disabled`、`supports_grammar=false` 走 `json_object` + jsonschema）与 `deepseek-v4-pro`（已登记、当前无臂使用）；实测该账号**已无 `deepseek-chat`**（旧名下线）。④ §9 新增三行配置：`api_key_file`（默认 `.env`；真实环境变量优先、只读注册表声明的 `api_key_env`、key 只进内存、空串即关闭）、`models.policy/reflection/proposer`、`experiment.arm/roles/arms`（臂定义计入 `config_hash`）。⑤ **登记两条实测口径**：(a) DeepSeek **不承诺跨时间确定性**——字节相同的请求 9 次返回 9 个不同响应，故 `E_EVAL_NONDETERMINISTIC` 的判据改为按 request hash 比对 response hash；(b) `flash` 默认开思考模式会让 `content` 返空、每次白花一次调用，故结构化流水线显式 `api_thinking: disabled` |
| v1.7 | 2026-10-10 | **`generate_json` 增加按调用方的 `max_tokens`（F37，对齐 Spec v2.3 B-47）**。① 背景：本键同时是「调用方未显式指定时的模态默认」，MOD-07 的提案也走它，而 256 只够决策点 ⇒ 提案一律被截断（`finish_reason=length`）→ 报成 `E_LLM_SCHEMA_INVALID` → 0 候选。② 修法：契约增加可选 `max_tokens`，`BackendInferenceClient` 原样带给 MOD-03（`BackendManager` 本就支持 per-call 覆盖，已有用例）。③ **明确否掉全局抬高**：2048 时 `mock`（4096）触发 71 例 `E_LLM_CONTEXT_OVERFLOW`、并吃掉 8B 余量；纯代码路线还保住了 `config_hash`（已付基线可复用）。④ 新增 §6.1 说明；护栏两条（门面转发 + proposer 传值），修复前均失败。 |
