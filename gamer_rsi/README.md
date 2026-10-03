# gamer_rsi —— RSI 训练链路（Python）

已落地：**MOD-01 env-rules**（GAME 内核的 Python 逐位移植）、**MOD-02 env-sandbox**
（观测投影 / 代码与进程隔离 / 泄漏审计 / 蜜罐）与 **MOD-03 inference**
（MLX/llama.cpp 后端契约、约束解码 + 三级 JSON 修复、并发限流与内存预算），对齐
[模块拆分 Spec v1.4](../docs/模块拆分Spec-GameRSI.md)、
[MOD-01 PRD](../docs/prd/MOD-01-env-rules-PRD.md)、
[MOD-02 PRD](../docs/prd/MOD-02-env-sandbox-PRD.md)、
[MOD-03 PRD](../docs/prd/MOD-03-inference-PRD.md)。
其余模块（MOD-04..08）尚未落地，其命令已按 Spec §4.6 注册，调用时返回
`E_IO_MODULE_PENDING`（退出码 2）。

MOD-03 按 Spec v1.4 B-31 交付 `MLXBackend` + `MockBackend`：`Backend` 契约与一致性
套件已定稿，`LlamaCppBackend` / `LlamaServerBackend` 同步实现（依赖懒加载，阶段二直接可用）。

## 环境要求

- Python ≥ 3.11（用到了 `StrEnum`）；依赖 `pyyaml` + `jsonschema`（draft 2020-12）。
- 绘图为可选依赖：缺 `matplotlib` 时 `plot_generations()` 降级为 `E_IO_PLOT_UNAVAILABLE`
  警告并返回 `[]`（FR-09-13）。

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python pyyaml jsonschema pytest matplotlib
```

## 快速开始（G6：可直接复制执行）

```bash
# MOD-09：确定性守卫 + 分层 import 门禁（AST，非正则；违例退出码 4）
python -m gamer_rsi guards check --root gamer_rsi

# MOD-01：规则/确定性自检 + GAME golden 对拍
python -m gamer_rsi env selftest --seed 1

# MOD-02：泄漏审计（0 无 finding / 1 仅 medium / 2 含 critical）+ 报告 0600 落盘
python -m gamer_rsi audit leak --run-id <run_id>

# MOD-02：单测 / golden / 集成
pytest tests/unit/sandbox/test_sandbox_projection.py -q
pytest tests/golden/sandbox tests/integration -q

# MOD-03：后端可用性探针（离线用 mock，无需权重）
python -m gamer_rsi inference probe --model mock --mode eval --seed 7 --run-id demo-0001

# MOD-03：延迟/吞吐基准（对照注册表 perf_budget；默认 n=50）
python -m gamer_rsi inference bench --model mock --mode eval --seed 7 --n 20 --run-id demo-0001

# MOD-03：JSON 格式鲁棒性（M1 交付物 runs/<id>/metrics/format_robustness.json；未达阈值退出码 1）
python -m gamer_rsi inference robustness --model mock --mode eval --seed 7 --n 200 --run-id demo-0001

# MOD-03：单测 / 集成 / golden
pytest tests/unit/inference tests/golden/inference -q
pytest tests/integration/test_inference_robustness.py -q

# 只打印将执行的动作与配置摘要（stdout 为可解析 JSON，不落盘）
python -m gamer_rsi loop run --generations 3 --k 30 --hitl --run-id demo-0001 --dry-run
python -m gamer_rsi --help
```

真实模型（`--model qwen3-8b-q4`）需要先落盘权重（离线、`models/` 已 gitignore）：

```bash
uv pip install --python .venv/bin/python 'mlx-lm>=0.20'          # 阶段一（MLX）
uv pip install --python .venv/bin/python 'llama-cpp-python>=0.2' # 阶段二（GBNF / server）
python -m gamer_rsi inference robustness --model qwen3-8b-q4 --mode eval --seed 1042 --n 200
```

## 模块结构（`gamer_rsi/sandbox/`，MOD-02）

| 文件 | 职责 | 关键 FR |
|---|---|---|
| `projection.py` | `OBSERVATION_SCHEMA`（34 字段，INV-2 唯一登记表）、`project_state`、`obs_digest`、`validate_observation`、`render_compact`、CHANGELOG 版本校验 | FR-02-1..6/3a/3b/24 |
| `api.py` | `SandboxEnv`（agent 侧唯一入口；私有 `_engine`，无任何返回 `FullState` 的成员）、`EpisodeConfig`/`StepResult`、`reject(final)` 与 `policy_failure` 唯一判负 | FR-02-7/8/8a/8b/8c |
| `guard.py` | AST 门禁（`check_imports`/`analyze_code`/`gate`）、import hook、`guarded_globals`、`run_agent_tick`、常驻 `ToolWorker` | FR-02-9..15 |
| `audit.py` | 可见集合重建、6 类泄漏规则、轨迹重放补齐隐藏 token、`run_audit`/`write_report`/`audit_to_console` | FR-02-16..19 |
| `honeypot.py` | 4 个隐藏位置注入 canary/假字段、字节级 `scan`、12 用例 `self_test`、`reset_canary` ledger | FR-02-20..23 |
| `_worker.py` | 私有：常驻工具执行子进程（子进程内设 rlimit + `exec` 进 `guarded_globals`） | FR-02-14 |

配套：`schemas/{observation,audit_report,harness_gate}.schema.json`、
`../../docs/prd/OBSERVATION_CHANGELOG.md`（字段变更必须主版本 +1 并追加行）、
`../../configs/honeypot.yaml`（注入计划，与实现不一致即 `E_ENV_HONEYPOT`）、
`../../tests/golden/sandbox/`（G2 golden，由 `regen_sandbox_golden.py` 显式重生成）。

## MOD-02 约定

- **agent 只能经 `SandboxEnv.observe/step` 触达环境**（INV-1）：`gamer_rsi.env.*` 对
  L1–L3 在 CI（`check_imports`）与运行期（不可卸载 import hook）两级被拒；sandbox
  自身的导入放行。导入 `gamer_rsi.sandbox` 即安装 hook。
- **观测只可能来自 `projection.project_state()`**（INV-2）：未登记字段一律丢弃，且
  `ProjectionStat` **只记计数不记名称**；字段增删/类型变更必须 `OBSERVATION_SCHEMA_VERSION`
  主版本 +1 并追加 `OBSERVATION_CHANGELOG.md`（CI 校验末行版本）。
- **harness 工具受限执行**（INV-4）：`gate()` 静态 AST 门禁（fail-closed：任一文件失败
  整批拒绝、旧 harness 保持可用）→ `load_harness_tools()` 常驻子进程（rlimit 只设在
  子进程、超时 `E_ENV_TIMEOUT` 即 SIGKILL + 重启、逃逸 `E_ENV_ESCAPE` 为 P0）。主进程
  持有模型，绝不设 rlimit。
- **审计**（INV-3/INV-5）：扫 `prompt`/`memory`/`log` 三个出口；canary / heldout-test seed /
  未来房间 / 商店未展示内容判 `critical`，纯字段名类判 `medium`；轨迹缺失或 JSONL 损坏
  （`E_ENV_AUDIT_IO`）同样判 `critical`（缺证据 ≠ 无泄漏）。报告权限 `0600`，`evidence`
  只保留命中 token 前后各 12 字符。
- **`python -m gamer_rsi audit leak`** 退出码 `0/1/2/4`（两根正交轴，MOD-02 PRD §18.2）：
  `0` 无 finding / `1` 仅 medium / `2` 含 critical（泄漏结论，FR-02-19）；`4` 审计器自身
  守卫违例（`machinery_errors`，如蜜罐自测 <100% 的 `E_ENV_HONEYPOT`，与
  `infra.errors.EXIT_CODE` 一致），优先级 `4 > 2 > 1 > 0`，报告仍记 `verdict=critical`。

## 模块结构（`gamer_rsi/infra/`，MOD-09）

| 文件 | 职责 | 关键 FR |
|---|---|---|
| `config.py` | `AppConfig`（frozen dataclass，8 段）、优先级合并、`config_hash` / `canonical_json` / `derive_seed` / `artifact_hash`、`harness/config.json` 白名单 | FR-09-1..4 |
| `run.py` | `RunPaths`、`make_run_id`、`init_run_dir`（幂等目录树 + run 冲突 + `.gitignore` 检查）、`state.json` 原子读写 | FR-09-4/5/12/19 |
| `logging.py` | `JsonlWriter`（逐行 flush / fsync / 轮转 / 崩溃安全）、`Redactor`（仅 prompt/memory/reflection）、`validate_trajectory_line` | FR-09-6/8/9 |
| `events.py` | `EVENT_REGISTRY`（`<module>.<event>`）+ `emit()`，未登记事件开发模式抛错 / 生产降级；`make_redacted_writer()` 把 redaction 命中接到 `infra.redaction_hit` 事件与 MOD-02 告警回调 | FR-09-7/9 |
| `errors.py` | 全部 `E_*` 枚举 + `EXIT_CODE` 映射（Spec §8.3）+ `InfraError` | FR-09-16 |
| `io.py` | `write_json_atomic`（tmp + `os.replace`，report/state/version 唯一写入口）、schema `validate`、末行截断容忍 | FR-09-18 |
| `guards.py` | `scan_determinism`（INV-6；别名归一的 `random`/`numpy.random`/`time`/`uuid`/`torch` 规则，显式 seed 的工厂放行）/ `scan_layer_imports`（INV-1 + NFR-09-5，绝对与相对 import 一视同仁）、`# guard: allow` 白名单 | FR-09-15 |
| `git_ops.py` | `worktree_clean_check` / `commit_harness` / `tag_version` / `rollback_to`（tag = `harness-v{N}`） | FR-09-14 |
| `cli.py` | 子命令树、退出码、`--dry-run` / `--resume`、`--seeds SPEC` 解析、`register(path, handler)` 转发注册表、收尾发 `infra.run_end` 并写 `metrics/io_latency.csv` | FR-09-10/11/12 |
| `plotting.py` | `plot_generations()`（5 张 png，n 标注 + ci95 误差棒） | FR-09-13 |
| `process.py` | `bootstrap_process()`：`PYTHONHASHSEED=0` / `TZ=UTC` / locale / threads=1（`process_snapshot()` 供 `run_start` 记录，本模块不发事件） | FR-09-17 |

组合根 `../app.py`（顶层，不属于任何 L1–L3 层）把各模块实现经 `infra.cli.register()` 注入，
并注册 `content_hash` 提供者；`python -m gamer_rsi` → `__main__.py` → `app.main()`。

## 模块结构（`gamer_rsi/inference/`，MOD-03）

| 文件 | 职责 | 关键 FR |
|---|---|---|
| `backend.py` | `Message`/`Usage`/`BackendHealth`/`GenerationResult`、`Backend` Protocol（5 方法 + `name`/`model_id`）、`MockBackend`、`BackendManager`（single-flight 加载、信号量限流、内存预算前置、三级 JSON 重试）、`InfraFacade`/`NullInfra`/`InfraBridge` | FR-03-01..10 |
| `registry.py` | `configs/models.yaml` 加载 + `schemas/models.schema.json` 校验 → `ModelSpec`；`estimate_required_gb`（PRD §7 公式） | FR-03-01/08，NFR-03-04 |
| `schema.py` | JSON Schema 子集 → GBNF 编译器（不支持即返回 `None`，绝不静默忽略）、`validate`/`json_path`、**修复 prompt 独占拥有**、唯一 `DECISION_SCHEMA` 加载 | Spec §4.3 B-28 |
| `mlx_backend.py` | `mlx-lm` 进程内 + `asyncio.to_thread`、JSON mode（无 GBNF）、`unload` 释放 + `mx.clear_cache()`（`supports_grammar=false` ⇒ `outcome_kind="degraded"`） | PRD §6 / R-1/R-2 |
| `llama_backend.py` | `llama-cpp-python` 进程内（`LlamaGrammar`）+ `llama-server` 子进程/HTTP/GBNF（只监听 `127.0.0.1`，`SIGTERM` + `atexit` 回收） | PRD §6 / NFR-03-06 / R-7 |
| `records.py` | `InferenceCallRecord`（每次尝试一条，§13）+ `CallContext` + `prompt_digest`（可离线复算） | FR-03-06，NFR-03-05 |
| `metrics.py` | §13 聚合：延迟 p50/p95/max、tok/s、`first_pass_valid_rate`、`schema_fail_rate`、`retry_histogram`、错误率、每局成本；M1 `format_robustness.json` | §13 / G5 |
| `weights.py` | 权重存在性与 `sha256` 校验（目录按相对路径+摘要整体哈希），`GAMERSI_INFERENCE_SKIP_HASH=1` 逃生开关 | NFR-03-06 / Q-6 |
| `prompts.py` | probe/bench/robustness 的固定 prompt（MOD-04 快照 JSONL，缺失时确定性合成兜底） | PRD §11 |
| `cli.py` | `inference probe/bench/robustness` 实现（经 `app.py::wire()` 注册） | G6 |
| `errors.py` | `InferenceError(code/attempts/retryable/detail)`，只引用 `infra.errors.E` 的 `E_LLM_*` | Spec §8.3 |

约定：

- **契约优先**（NFR-03-07）：新增后端只需 5 个方法 + `name`/`model_id`，
  一致性套件见 `tests/unit/inference/test_backend_protocol.py`。
- **不自行兜底**（PRD §10）：本模块只抛 `E_LLM_*`，fallback 到 heuristic 由 MOD-04/06 决定。
- **不写 `runs/**/*.jsonl`**：只产出 `InferenceCallRecord`，落盘归 MOD-09。
- **评估口径**（B-14）：`mode="eval"` 强制 `temperature=0`、显式 `seed`、`top_p=1.0`、`top_k=0`、
  `max_inflight=1`，harness 不可覆盖。
- 文档与代码冲突时以 Spec 为准。

## 约定

- **配置优先级**（B-26）：CLI > `GAMERSI_*` 环境变量（`__` 分层，如 `GAMERSI_MEMORY__RETRIEVE__TOP_K`）
  > `harness/config.json`（白名单：`agent.policy.*` / `memory.retrieve.*` / `agent.tools.*`）
  > `configs/default.yaml` > 代码默认值。
- **`config_hash`** 不含 `harness_version`（B-19）：`harness/config.json` 覆盖的键在 hash 视图里
  回退到静态基线，harness 变化由 `artifact_hash` 表达；排除 `infra.runs_root` / `infra.log_level`
  / `infra.threads` / `infra.config_dir` / `infra.schemas_dir` 与所有绝对路径。
- **退出码**：`0` 成功（含 HITL 挂起）· `1` 领域失败 · `2` 配置/环境 · `3` 模型不可用
  · `4` 沙箱/守卫违例 · `5` IO 落盘 · `130` Ctrl-C。
- **依赖单向**（NFR-09-5）：`infra` 不 import 任何其他 `gamer_rsi` 子包，由
  `guards.scan_layer_imports()` 静态强制（绝对与相对 import 都会被抓；
  `python -m gamer_rsi guards check` 自检通过）。命令面与领域模块的接线一律放
  `gamer_rsi/app.py`。
- **配置类型校验**（FR-09-2）：类型由 dataclass 注解统一校验（YAML 的 list 会归一为 tuple），
  各段 `validate()` 再补区间/枚举/跨段一致性；未校验的键不再是漏网之鱼。
- **redaction 作用域**（B-19）：只允许 `prompts/` / `memory/` / `reflections/` 三个出口，
  经 `events.make_redacted_writer(path, redactor, surface=...)` 构造，其他 surface 直接拒绝。
- 文档与代码冲突时以 Spec 为准。
