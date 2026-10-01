# gamer_rsi —— RSI 训练链路（Python）

M0 阶段交付 **MOD-09 infra 骨架**（配置 / 事件 / JSONL / 守卫 / git / CLI / 绘图），
对齐 [模块拆分 Spec v1.4](../docs/模块拆分Spec-GameRSI.md) §4.5/§4.6/§5/§8 与
[MOD-09 PRD](../docs/prd/MOD-09-infra-PRD.md)。其余模块（MOD-01..08）尚未落地，
其命令已按 Spec §4.6 注册，调用时返回 `E_IO_MODULE_PENDING`（退出码 2）。

## 环境要求

- Python ≥ 3.11（用到了 `StrEnum`）；依赖 `pyyaml` + `jsonschema`（draft 2020-12）。
- 绘图为可选依赖：缺 `matplotlib` 时 `plot_generations()` 降级为 `E_IO_PLOT_UNAVAILABLE`
  警告并返回 `[]`（FR-09-13）。

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python pyyaml jsonschema pytest matplotlib
```

## 快速开始（G6：可直接复制执行）

```bash
# 确定性守卫 + 分层 import 门禁（AST，非正则；违例退出码 4）
python -m gamer_rsi guards check --root gamer_rsi

# 只打印将执行的动作与配置摘要（stdout 为可解析 JSON，不落盘）
python -m gamer_rsi loop run --generations 3 --k 30 --hitl --run-id demo-0001 --dry-run

# 各命令的帮助（命令面逐字对齐 Spec §4.6）
python -m gamer_rsi --help
python -m gamer_rsi guards check --help
```

## 模块结构（`gamer_rsi/infra/`）

| 文件 | 职责 | 关键 FR |
|---|---|---|
| `config.py` | `AppConfig`（frozen dataclass，8 段）、优先级合并、`config_hash` / `canonical_json` / `derive_seed` / `artifact_hash`、`harness/config.json` 白名单 | FR-09-1..4 |
| `run.py` | `RunPaths`、`make_run_id`、`init_run_dir`（幂等目录树 + run 冲突 + `.gitignore` 检查）、`state.json` 原子读写 | FR-09-4/5/12/19 |
| `logging.py` | `JsonlWriter`（逐行 flush / fsync / 轮转 / 崩溃安全）、`Redactor`（仅 prompt/memory/reflection）、`validate_trajectory_line` | FR-09-6/8/9 |
| `events.py` | `EVENT_REGISTRY`（`<module>.<event>`）+ `emit()`，未登记事件开发模式抛错 / 生产降级 | FR-09-7 |
| `errors.py` | 全部 `E_*` 枚举 + `EXIT_CODE` 映射（Spec §8.3）+ `InfraError` | FR-09-16 |
| `io.py` | `write_json_atomic`（tmp + `os.replace`，report/state/version 唯一写入口）、schema `validate`、末行截断容忍 | FR-09-18 |
| `guards.py` | `scan_determinism`（INV-6）/ `scan_layer_imports`（INV-1 + NFR-09-5）、`# guard: allow` 白名单 | FR-09-15 |
| `git_ops.py` | `worktree_clean_check` / `commit_harness` / `tag_version` / `rollback_to`（tag = `harness-v{N}`） | FR-09-14 |
| `cli.py` | 子命令树、退出码、`--dry-run` / `--resume`、`--seeds SPEC` 解析 | FR-09-10/11/12 |
| `plotting.py` | `plot_generations()`（5 张 png，n 标注 + ci95 误差棒） | FR-09-13 |
| `process.py` | `bootstrap_process()`：`PYTHONHASHSEED=0` / `TZ=UTC` / locale / threads=1 | FR-09-17 |

## 约定

- **配置优先级**（B-26）：CLI > `GAMERSI_*` 环境变量（`__` 分层，如 `GAMERSI_MEMORY__RETRIEVE__TOP_K`）
  > `harness/config.json`（白名单：`agent.policy.*` / `memory.retrieve.*` / `agent.tools.*`）
  > `configs/default.yaml` > 代码默认值。
- **`config_hash`** 不含 `harness_version`（B-19），harness 变化由 `artifact_hash` 表达；
  排除 `infra.runs_root` / `infra.log_level` / `infra.threads` 与所有绝对路径。
- **退出码**：`0` 成功（含 HITL 挂起）· `1` 领域失败 · `2` 配置/环境 · `3` 模型不可用
  · `4` 沙箱/守卫违例 · `5` IO 落盘 · `130` Ctrl-C。
- **依赖单向**（NFR-09-5）：`infra` 不 import 任何其他 `gamer_rsi` 子包，由
  `guards.scan_layer_imports()` 静态强制（`python -m gamer_rsi guards check` 自检通过）。
- 文档与代码冲突时以 Spec 为准。
