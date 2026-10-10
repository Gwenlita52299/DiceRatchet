# MOD-09 infra 技术 PRD

> 版本：v0.5（2026-10-10 新增 FR-09-20 起跑前门禁 + `commit_harness` 失败自清，并改写 FR-09-14；处置 F47-A/C1，见 §17）
> **历史版本**：v0.4（2026-10-10 `episode_key` 的 `arm` 位允许 `-smoke`）· v0.3.1（2026-10-03 修复 `bootstrap_process()` 早发 `infra.run_start`，见 §17；此前误留在 v0.3）· 状态：待评审 · 所属里程碑：M0–M3（L3 工程层，横切全模块）
> 上游 Spec：[../模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md)（唯一事实来源；与本文件冲突时以 Spec 为准）
> 作者：主代理（MOD-09 owner）· 日期：2026-09-30
> v0.3 起 Spec §11.2 已清零；新的临时决策按 Spec §11.2 从 P-31 起登记。

## 1. 背景与目标

MOD-09 是 Spec §6 依赖矩阵中**唯一不被任何人阻塞、却阻塞所有人**的模块：M0 第一天就要有配置、日志、CLI 与确定性守卫，否则后续模块会各自发明日志格式、各自调用全局随机、各自写 `runs/` 目录——这三件事一旦发生，Spec §8 的可复现性与 INV-6 就同时失效，M3 的「增益真实性」验收将不可信。

本模块的存在理由是把**工程横切关注点**从领域逻辑里彻底剥离：

1. **可复现**——`run_id` + `config_hash` + 环境固定，使任意一次 run 可用一条命令重放；对抗「结果无法归因到配置」。
2. **可观测**——唯一 JSONL 落地实现，轨迹行 schema 严格对齐 Spec §4.5；对抗「各模块自建日志、指标无法聚合」。
3. **可审计**——集中 redaction + 错误码注册表 + 泄漏审计的落盘出口；对抗 INV-5 被日志侧绕过。
4. **可拦截**——`guards.py` 用 AST 静态扫描把 INV-1/INV-6 变成可执行的门禁，而不是口头纪律。

**成功度量**：① 同一 `AppConfig` 两次 `config_hash()` 完全一致；② 轨迹行 schema 校验通过率 100%；③ `guards` 能在故意埋雷的 fixture 上 100% 报出违例；④ 进程被 `SIGKILL` 后，已落盘轨迹行的 JSON 完整性 100%（最多丢最后一行）。

## 2. 范围

### 2.1 范围内

- `configs/*.yaml` 的加载、优先级合并、schema 校验、`config_hash` 计算、配置快照落盘。
- `run_id` 生成规则与 `runs/<run_id>/` 目录树初始化（对齐 Spec §5）。
- `JsonlWriter`：追加写、逐行 flush、崩溃安全、轮转；事件类型枚举与事件 schema。
- 单步轨迹行落盘与 schema 校验（对齐 Spec §4.5），含 heldout/test seed 的自动 redaction。
- CLI 子命令树（对齐 Spec §4.6）、参数解析、退出码约定、`--dry-run` / `--resume` 透传。
- 绘图：代际指标曲线与熵曲线输出到 `runs/<run_id>/plots/`。
- git 操作封装：`worktree_clean_check` / `commit_harness` / `tag_version` / `rollback_to`。
- `guards.py`：确定性扫描（INV-6）与分层 import 门禁（INV-1），可独立运行并接入 CI。
- 错误码注册表：`E_ENV_*` / `E_ACTION_*` / `E_LLM_*` / `E_EVOLVE_*` / `E_EVAL_*` / `E_IO_*` 的集中定义与含义表。
- 进程级确定性环境固化（`PYTHONHASHSEED=0`、时区、locale、线程数）。

### 2.2 范围外（非目标）

> 逐字复用 Spec §3 MOD-09 的「不负责」清单：

**不负责**：任何领域逻辑（不得在 infra 里写游戏或评估规则）。

补充非目标（不改变上述清单语义）：不定义指标口径与统计方法（MOD-08）；不生成演化候选、不做接受判定（MOD-07）；不组装 prompt（MOD-04）；不做观测投影与泄漏判定的**判定逻辑**（MOD-02，本模块只提供落盘出口与 redaction 的兜底）；不选择与加载模型（MOD-03）；不实现游戏规则（MOD-01）；不实现闭环状态机与断点语义（MOD-06，本模块只提供 `state.json` 的**读写原语**）。

## 3. 使用方与场景

| 使用方 | 调用点 | 契约 |
|---|---|---|
| 全体模块 | `load_config()` / `derive_seed()` | 只读配置；随机数一律经 `derive_seed`，禁自建 |
| 全体模块 | `JsonlWriter.write(event)` | 事件必须命中 `EVENT_TYPES` 且过 schema 校验 |
| MOD-06 loop | `init_run_dir()` / `read_state()` / `write_state()` | 目录结构由本模块定义，状态语义由 MOD-06 定义 |
| MOD-07 evolve | `commit_harness()` / `tag_version()` / `rollback_to()` / `worktree_clean_check()` | 演化前必须 `worktree_clean_check()` 通过 |
| MOD-08 evals | `plot_generations()` 读取 `metrics/generations.csv` | **列定义归 MOD-08**（属指标口径），本模块只冻结文件路径与绘图接口 |
| MOD-02 sandbox | `Redactor` 注册表 | 审计器把发现的敏感值注册进 redactor，落盘前统一擦除 |
| 研究者（人） | CLI（Spec §4.6） | 退出码见 §6.4；所有命令支持 `--dry-run` |

典型场景：① 冷启动 `loop run --generations 3 --k 30`（初始化 run 目录 → 每步写轨迹 → 每代写报告）；② 事故复现（拿 `run_id` 重放）；③ CI 跑 `guards` 拦下越权 import；④ 演化前 git 门禁；⑤ 崩溃后 `--resume`。

## 4. 功能需求（FR-09-x，编号，每条可测试）

- **FR-09-1 配置优先级（Spec v1.4 B-26）**：合并顺序 `CLI 显式参数 > DICERATCHET_* 环境变量 > harness/config.json（可演化层）> configs/default.yaml > 代码内 dataclass 默认值`。harness 层只接受白名单键（`policy.*` 训练期采样参数、`memory.retrieve.*`、`agent.tools.*`），出现其他键或试图设置评估温度 → `E_IO_CONFIG_INVALID`（退出码 2）；这一层计入 `artifact_hash`，不计入 `config_hash`。环境变量命名规则 `DICERATCHET_<SECTION>__<KEY>`（双下划线分层），未知键 → 记 `E_IO_CONFIG_UNKNOWN` 并**忽略**（不中断），未知键清单写入 run 启动日志。
- **FR-09-2 schema 校验**：`AppConfig` 为 frozen dataclass + 显式 `validate()`（避免引入 pydantic 作为阶段一硬依赖）；类型不符或越界 → `E_IO_CONFIG_INVALID` 并以退出码 2 终止，错误信息必须指出键路径与期望类型。
- **FR-09-3 config_hash**：`config_hash = sha256(json.dumps(_canonical(cfg), sort_keys=True, separators=(",",":"), ensure_ascii=False))[:16]`；`_canonical` **排除**易变字段（`run_id`、`created_at`、`log_level`、`paths.runs_root`）与所有绝对路径，**不包含** `harness_version`（Spec v1.3 B-19：否则第一代被接受后 `--resume` 必然冲突）。harness 的变化由 `artifact_hash(harness_dir)` 表达：对 `(相对路径, 文件 sha256)` 按路径排序后整体 sha256，逐代写入 `version.json` 与 report。
- **FR-09-4 run_id**：`run_id = f"{git_short_sha}-{config_hash[:8]}-{utc_ts}"`，`utc_ts = YYYYMMDDTHHMMSSZ`；`git_short_sha` 取自 `git rev-parse --short HEAD`，仓库不可用（无 git / dirty 之外的情况）→ 用 `nogit` 占位并记 `E_IO_GIT_UNAVAILABLE`。**已回填（Spec v1.3 §4.6）**：`--run-id` 显式指定时以显式值为准，用于复现与测试。
- **FR-09-5 目录初始化**：`init_run_dir(run_id, cfg) -> RunPaths` 幂等创建 Spec §5 的目录树（`config.json`、`events.jsonl`、`episodes/<split>/`、`reflections/`、`generations/`、`metrics/`、`plots/`、`audit/`）。目录已存在且 `config.json` 的 `config_hash` 不一致 → `E_IO_RUN_CONFLICT` 并退出码 2（禁止两套配置写同一 run）。
- **FR-09-6 JsonlWriter 写入语义**：每行 `json.dumps(..., ensure_ascii=False)` + `\n`；每次 `write()` 后 `flush()`；`infra.fsync_every`（默认 0 = 不 fsync，>0 表示每 N 行 `os.fsync`）。**崩溃安全**：读取端必须容忍末行截断（`json.JSONDecodeError` 时丢弃最后一行并记 `E_IO_TRAILING_TRUNCATED`）；写入端保证「一行要么完整要么不存在」——先构造整行字符串再单次 `write`。
- **FR-09-7 事件注册表**（Spec v1.3 §8.2）：
  - 事件名为 `<module>.<event>`，全部登记在 `infra/events.py::EVENT_REGISTRY`（§6.5），统一落到 `runs/<run_id>/events.jsonl`。
  - 通过 `emit(event, **fields)` 写入，自动附加 `ts/run_id/module`。
  - 未登记的事件：开发模式抛 `E_IO_EVENT_UNKNOWN`；生产模式改写为 `infra.unknown_event` 并保留原名。
  - 轨迹行不是事件，走 `JsonlWriter` 写入 `episodes/`。
- **FR-09-8 轨迹行 schema**：
  - 字段严格对齐 Spec v1.3 §4.5，schema 文件为 `schemas/trajectory_line.schema.json`（§6.2）。
  - 已登记的字段之外，只允许 `x_` 前缀字段（`additionalProperties:false` + `patternProperties`）。
  - 缺必填字段 → `E_IO_TRAJECTORY_SCHEMA`。
  - 局末的 `episode_end` 行用独立的 schema 分支校验。
  - `ts` 统一为 UTC ISO-8601，带 `Z`。
- **FR-09-9 redaction（作用域收窄，Spec v1.3 B-19）**：
  - `Redactor` 持有敏感值集合，由 MOD-02 审计器注册：heldout/test seed、canary 串、`GameState` 专有键名。
  - **只作用于 agent 可见的三个出口**：`prompts/` 快照、`memory/` 与 `reflections/`。落盘前做值级替换（如 `<REDACTED:seed>`）并计数。
  - 命中数 > 0 → 写 `infra.redaction_hit` 事件，并触发 MOD-02 告警回调（这类命中本身说明发生了 INV-5 泄漏）。
  - **轨迹（`episodes/`）、`events.jsonl`、`generations/` 不做 redaction**：它们属于审计侧，MOD-08 的配对检验需要原始 seed。
  - 只擦除不阻断，阻断属于 MOD-04/MOD-02 的职责。
- **FR-09-10 CLI 一致性**：
  - 子命令树与 Spec v1.3 §4.6 逐字一致，包括 `inference *`、`loop status|smoke`、`eval ablation|test`、`memory *`、`guards check`。
  - run 一律用 `--run-id ID` 指定，续跑用 `--resume` 开关。本模块只负责解析与转发到各模块的 `register(subparser)`；**新增命令必须同步更新 Spec §4.6**。
- **FR-09-11 退出码**：`0` 成功（含 HITL 挂起）；`1` 领域失败（如验收不达标）；`2` 配置/环境错误；`3` 模型不可用；`4` 沙箱/守卫违例；`5` IO 落盘失败；`130` 用户 Ctrl-C（与 Spec v1.3 §8.3 一致）。约定写入 `dice_ratchet/infra/exit_codes.py` 并被所有命令复用。
- **FR-09-12 dry-run 与 resume**：`--dry-run` 对所有命令生效——不调用模型、不写 `runs/`、只打印将执行的动作与配置摘要（stdout 为可解析 JSON）；`--run-id ID --resume` 由 MOD-06 实现语义，本模块负责定位目录、校验 `config_hash` 一致（`config_hash` 不含 harness，所以演化过的 run 也能续跑）、并在 `state.json` 缺失时报 `E_IO_STATE_MISSING`。
- **FR-09-13 绘图**：`plot_generations(csv_path, out_dir)` 输出 `win_rate.png`、`stages_cleared.png`、`hearts_left.png`、`entropy.png`、`train_heldout_gap.png`（INV-7 口径，Spec v1.3），每图含 `n`、`ci95` 误差棒与数据点标注；无 matplotlib 时降级为 `E_IO_PLOT_UNAVAILABLE` 警告而不失败。
- **FR-09-14 git 封装**：`worktree_clean_check()` 在工作区不干净时抛 `E_IO_GIT_DIRTY`（退出码 4）；`preflight_worktree_clean(repo, allow=())` 是它的**起跑前**形态（FR-09-20）；`commit_harness(version, message)` 提交 `harness/` 并返回 commit sha，提交信息格式 `harness(v{N}): {message}`——**先门禁后 `git add`**，任何失败都 `git reset -- <harness>` 收尾（F47-C1，不得留下 staged 残留）；`tag_version(N)` 打 `harness-v{N}` 轻量 tag（已存在则跳过；不建同名分支，避免 `refname is ambiguous`）；`rollback_to(N)` 以 `git checkout harness-v{N} -- harness/` 恢复工件目录并返回新 commit sha。
- **FR-09-20 起跑前门禁（F47-A）**：`preflight_worktree_clean(repo, allow=())` 把同一判据前移到**任何推理调用之前**：非 git worktree → 返回 `False` 并降级跳过（Spec §8.5）；否则脏即抛 `E_IO_GIT_DIRTY`。由 MOD-06 在 `loop run` 起跑处调用（见 MOD-06 §9.3）。**为什么必须有它**：FR-09-14 的判据原先只长在 `commit_harness()` 内部，即 accept 的最后一步——脏工作区要等**整代跑完**才被拒（实测一代 3.75 h / ≈¥47），且 `_finish_report` 会把它记成 `decision="reject"`、`build_decision_sections` 又会按 gate 状态写成 `decision="accept"`（幻影 accept）。
- **FR-09-15 确定性守卫（AST，非正则）**：`scan_determinism()` 报出 `E_IO_GUARD_RANDOM`（`import random` / `numpy.random.*` 未显式 seed / `time.time()` 参与采样 / `uuid4()` 用于标识 id）、`scan_layer_imports()` 报出 `E_IO_GUARD_LAYER`（`dice_ratchet/agent|memory|loop|evolve|evals/*` 中出现 `import dice_ratchet.env`，违反 INV-1）。白名单注释 `# guard: allow <CODE> reason=...` 可豁免，但会记 `guard_finding` 事件留痕。
- **FR-09-16 错误码注册表**：`dice_ratchet/infra/errors.py` 集中定义全部 `E_*` 枚举 + 含义表 + 默认退出码映射；禁止其他模块自定义字符串错误码。
- **FR-09-18 schema 与原子写**：`schemas/*.schema.json` 由本模块登记并提供 `validate(name, obj)`；`write_json_atomic(path, obj)`（tmp + `os.replace`）是 `report.json`、`state.json`、`version.json` 唯一的写入口。
- **FR-09-19 git 忽略检查**：`init_run_dir` 检查 `runs_root` 已被 `.gitignore` 覆盖（`git check-ignore`），否则记 `E_IO_RUNS_NOT_IGNORED` 并退出码 2，防止 `worktree_clean_check()` 永远判定为 dirty。
- **FR-09-17 进程确定性环境**：`bootstrap_process()` 设置 `PYTHONHASHSEED=0`（若未设置则 re-exec 一次）、`TZ=UTC`、`LC_ALL=C.UTF-8`，并把 `infra.threads=1` 传给后端；设置内容经 `process_snapshot()` / `process_fingerprint()` 由**真正的** `infra.run_start` 事件记录（`cli.cli_init_run()`，此时配置与 sink 都已就绪）。`bootstrap_process()` **自身不发事件**：它在 `main()` 最开头执行，既没有 `config_hash` 也没有 `events` sink，早发只会产生一条缺必填字段、且必然被丢弃的 `run_start`（v0.3.1 修复）。

## 5. 非功能需求（NFR-09-x：确定性/性能/可复现/可观测）

- **NFR-09-1 崩溃安全**：`SIGKILL` 后已落盘行 100% 可解析；末行截断可检测（`E_IO_TRAILING_TRUNCATED`）。
- **NFR-09-2 性能**：单次 `write()` p99 < 1 ms（本地 SSD，不含 fsync）；`load_config()` < 50 ms；不得成为单步决策的瓶颈（单步预算见 MOD-04）。
- **NFR-09-3 确定性**：`config_hash`、`run_id` 中除时间戳外全部稳定；`derive_seed` 对同一输入返回同一 64-bit 整数，且与进程、平台、`PYTHONHASHSEED` 无关（内部用 `hashlib.blake2b` 而非 `hash()`）。**作用域**：只用于 agent 侧随机（采样 seed、bootstrap、候选 id）；环境侧的流派生由 MOD-01 按 GAME-01 §3 的 xmur3 + SplitMix64 实现，两者互不替代。
- **NFR-09-4 可观测**：任何失败必须带错误码 + 人类可读信息 + 复现命令三要素。
- **NFR-09-5 依赖方向单向**：`infra` 不 import 任何其他 `dice_ratchet` 子包（除 `errors` 自身）；`guards` 对此做静态检查。

## 6. 接口契约（真实 Python 签名 + JSON Schema）

### 6.1 配置与随机

```python
# dice_ratchet/infra/config.py
@dataclass(frozen=True)
class AppConfig:
    env: EnvConfig; agent: AgentConfig; inference: InferenceConfig
    memory: MemoryConfig; loop: LoopConfig; evolve: EvolveConfig
    evals: EvalConfig; infra: InfraConfig

def load_config(cli_overrides: Mapping[str, Any] | None = None,
                env: Mapping[str, str] | None = None,
                config_dir: Path = Path("configs")) -> AppConfig: ...

def config_hash(cfg: AppConfig) -> str: ...                  # -> "a1b2c3d4e5f60718"
def canonical_json(cfg: AppConfig) -> str: ...
def derive_seed(*parts: object) -> int: ...                  # blake2b -> int(64)

# dice_ratchet/infra/run.py
@dataclass(frozen=True)
class RunPaths:
    root: Path; config: Path; episodes: Path; reflections: Path
    generations: Path; metrics: Path; plots: Path; audit: Path; state: Path

def make_run_id(cfg: AppConfig, *, now: datetime | None = None,
                explicit: str | None = None) -> str: ...
def init_run_dir(run_id: str, cfg: AppConfig, runs_root: Path = Path("runs")) -> RunPaths: ...
def write_state(paths: RunPaths, state: dict) -> None: ...    # 原子写: tmp + os.replace
def read_state(paths: RunPaths) -> dict: ...
```

### 6.2 事件与轨迹

```python
# dice_ratchet/infra/logging.py
EVENT_TYPES: Final[frozenset[str]]

class Redactor:
    def register(self, value: str | int, label: str) -> None: ...
    def redact(self, obj: Any) -> tuple[Any, int]: ...       # (擦除后对象, 命中数)

class JsonlWriter:
    def __init__(self, path: Path, *, fsync_every: int = 0,
                 redactor: Redactor | None = None,
                 schema: type[TrajectoryLine] | None = None) -> None: ...
    def write(self, event: Mapping[str, Any]) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> "JsonlWriter": ...
    def __exit__(self, *exc: object) -> None: ...

def validate_trajectory_line(line: Mapping[str, Any]) -> None: ...  # 失败抛 E_IO_TRAJECTORY_SCHEMA
```

轨迹行 JSON Schema（`schemas/trajectory_line.schema.json`，严格对齐 Spec v1.3 §4.5）：

```jsonc
{"$schema":"https://json-schema.org/draft/2020-12/schema","title":"TrajectoryLine",
 "type":"object","additionalProperties":false,
 "required":["episode_key","generation","harness_version","arm","seed","split","step",
             "decision_index","plan_index","attempt","source","obs_digest","action",
             "reward","cum_reward","illegal","mode","ts"],
 "properties":{
   "episode_key":{"type":"string","pattern":"^e-[0-9]{6}-g[0-9]{3}-(train|parent|cand-[a-z0-9]+|baseline-[a-z_]+|ablation-[a-z_]+)(-smoke)?$"},
   "generation":{"type":"integer","minimum":0},
   "harness_version":{"type":"integer","minimum":0},
   "arm":{"type":"string"},
   "seed":{"type":"integer","minimum":0},
   "split":{"enum":["train","heldout","test","demo"]},
   "step":{"type":"integer","minimum":0},
   "decision_index":{"type":"integer","minimum":0},
   "plan_index":{"type":"integer","minimum":0},
   "attempt":{"type":"integer","minimum":1,"maximum":3},
   "source":{"enum":["llm","fallback","heuristic","random","scripted"]},
   "obs_digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},
   "action":{"type":["object","null"]},
   "reward":{"type":"number"}, "cum_reward":{"type":"number"},
   "illegal":{"type":"boolean"}, "illegal_reason":{"type":["string","null"]},
   "llm_raw":{"type":["string","null"]},
   "llm_latency_ms":{"type":["integer","null"],"minimum":0},
   "prompt_tokens":{"type":["integer","null"],"minimum":0},
   "completion_tokens":{"type":["integer","null"],"minimum":0},
   "prompt_hash":{"type":["string","null"]}, "memory_ids":{"type":["array","null"],"items":{"type":"string"}},
   "tool_hash":{"type":["string","null"]}, "mode":{"enum":["train","eval"]},
   "ts":{"type":"string","format":"date-time"}},
 "patternProperties":{"^x_":{}}}
```

局末行 `{"type":"episode_end", ...}` 的字段见 Spec §4.5，由同一文件中的 `$defs/EpisodeEnd` 校验。

### 6.3 git / 绘图 / 守卫

```python
# dice_ratchet/infra/git_ops.py
def worktree_clean_check(repo: Path = Path(".")) -> None: ...
def commit_harness(repo: Path, version: int, message: str) -> str: ...
def tag_version(repo: Path, version: int) -> str: ...
def rollback_to(repo: Path, version: int) -> str: ...

# dice_ratchet/infra/plotting.py
def plot_generations(csv_path: Path, out_dir: Path) -> list[Path]: ...

# dice_ratchet/infra/guards.py
@dataclass(frozen=True)
class GuardFinding:
    path: str; line: int; col: int; code: str; message: str; exempted: bool

def scan_determinism(root: Path) -> list[GuardFinding]: ...
def scan_layer_imports(root: Path) -> list[GuardFinding]: ...
def scan_all(root: Path) -> list[GuardFinding]: ...          # 供 CI 与 CLI 复用
```

### 6.4 CLI 签名与退出码

| 命令 | 关键参数 | 退出码 |
|---|---|---|
| `env selftest` | `--seed INT` | 0/4 |
| `play` | `--agent {llm,heuristic,random,fixed_prompt}` `--seeds SPEC` `--episodes N` `--model ID` `--dry-run` | 0/1/2/3 |
| `baseline` | `--kind {random,heuristic,fixed_prompt}` `--split {train,heldout,test}` `--seeds SPEC` \| `--basket {H1,H2,H3,test_full,train_window}`（二者互斥，省略 basket 时按 split 推导）`--model ID` `--resume` | 0/1/2/3 |
| `loop run` | `--generations N` `--k N` `--hitl/--no-hitl` `--run-id ID` `--resume` | 0/1/2/3/4/5 |
| `loop status\|smoke` | `--run-id ID` | 0/1/2 |
| `evolve approve\|rollback` | `--run-id ID` `--generation N` `--reason STR` | 0/2/4 |
| `eval report` | `--run-id ID` `--split {train,heldout,test}` `--baseline {none,all,...}` | 0/1/2 |
| `eval ablation\|test` | `--run-id ID` `--basket {H1,H2,H3}` | 0/1/2/3 |
| `inference probe\|bench\|robustness` | `--model ID` | 0/1/3 |
| `memory stats\|snapshot\|rollback\|verify\|retrieve` | `--run-id ID` | 0/1/2 |
| `audit leak` | `--run-id ID` `--split {train,heldout,test,demo}` `--out PATH` | 0/1/2/4（注） |
| `eval diversity` | `--run-id ID` | 0/1/4 |
| `guards check` | `--root PATH` `--fix-hints` | 0/4 |

> 注（2026-10-03，按评审修订）：`audit leak` 的退出码由**归属模块** MOD-02 决定，是
> **两根正交轴**——泄漏结论 `0` 无 finding / `1` 仅 medium / `2` 含 critical（FR-02-19、
> §11 集成用例），以及沙箱守卫违例 `4`（Spec §8.3；蜜罐自测失败等 `E_ENV_HONEYPOT`，
> 与本模块 `EXIT_CODE[E_ENV_HONEYPOT] = 4` 的登记一致），优先级 `4 > 2 > 1 > 0`。
> 详见 MOD-02 PRD §18.2。本模块只负责命令面与报告落盘出口
> （`resolve_run_dir`/`RunPaths`/`infra.io.validate`）。

`--seeds SPEC` 语法：`train:1000-1004`、`heldout:2000-2099`、`9000,9001`、`file:seeds.txt`。`guards check` 已回填 Spec v1.3 §4.6（P-20）。

`--resume` 的两类语义（2026-10-05，M1 验收 F18）：`loop run` 走 `resolve_run_dir(resume=True)`，
要求 `state.json` 存在（缺失即 `E_IO_STATE_MISSING`），语义归 MOD-06；`baseline` 走
`cli_init_run(require_state=False)`，只按 `episodes/<split>/` 已完成的局跳过重复 seed、只跑缺的
（基线从不写 `state.json`）。另：`--model/--mode/--seed/--timeout-ms` 等覆盖会并入 `config_hash`
（`config_overrides()`），因此同一 `--run-id` 只能承载覆盖集合完全相同的命令，否则
`E_IO_RUN_CONFLICT`（退出码 2）——这是 M1 验收 F10 的成因。

### 6.5 事件与错误码注册表（Spec v1.3 §8.2/§8.3 的落地）

```python
# dice_ratchet/infra/events.py
EVENT_REGISTRY: Final[Mapping[str, EventSpec]]   # 键为 "<module>.<event>"
def emit(event: str, **fields: Any) -> None: ...

# dice_ratchet/infra/errors.py
class E(StrEnum): ...                             # 全部错误码
EXIT_CODE: Final[Mapping[E, int]]
```

登记来源为各 PRD「指标与埋点」节。v0.2 的初始清单：

- `infra.{run_start,run_end,guard_finding,redaction_hit,unknown_event}`
- `env.*`（MOD-02 转发内核 events）
- `sandbox.{gate_pass,gate_reject,tool_timeout,honeypot_hit,audit_finding}`
- `llm.{call,degrade,oom}`
- `policy.{decision,fallback,reject}`
- `memory.{add,dedup,evict,snapshot,rollback,retrieve}`
- `loop.{episode_start,episode_end,cycle_start,budget_exhausted,interrupted,resume}`
- `evolve.{propose,gate,judge,decision,hitl_pending,rollback}`
- `eval.{start,seed_done,report,collapse}`

错误码以 Spec §8.3 的统一命名为准；每个 PRD 新增错误码时，必须在同一个 PR 里更新 `errors.py`。

## 7. 数据模型与存储

```
runs/<run_id>/
  config.json                      # AppConfig 快照 + config_hash + content_hash + git sha + 环境变量白名单
  state.json                       # MOD-06 语义；本模块提供原子写原语
  events.jsonl                     # 全部结构化事件（§6.5）
  episodes/<split>/<episode_key>.jsonl   # 轨迹（每步一行，schema 见 §6.2）
  reflections/<episode_id>.json    # MOD-06 产出
  generations/gen-{N}/             # report.json / diff.patch / harness snapshot
  metrics/scoreboard.json          # MOD-06 计分台账（episode_key 幂等）
  metrics/llm_calls.jsonl          # MOD-03 每次推理尝试一行
  metrics/generations.csv          # MOD-08 定义列并写入（列清单见 MOD-08 PRD §7）；本模块只读取绘图
  metrics/plots_data/*.json        # MOD-08 产出，本模块绘图输入
  prompts/                         # MOD-04 决策 prompt 快照（供审计复现）
  memory/memory_v{N}.jsonl         # MOD-05 记忆快照
  plots/*.png                      # win_rate.png / stages_cleared.png / hearts_left.png / entropy.png / train_heldout_gap.png
  audit/leak_report.json           # MOD-02 产出
```

`state.json` 原子写：先写 `state.json.tmp` + `os.replace()`，保证读者永不见半截文件。

## 8. 关键流程（伪代码或时序）

```python
# 启动引导（任何 CLI 命令的第一步）
def bootstrap(argv):
    bootstrap_process()                       # FR-09-17: PYTHONHASHSEED/TZ/locale/threads
    cfg = load_config(cli_overrides=parse_cli(argv))     # FR-09-1/2
    if "--dry-run" in argv:
        return print(json.dumps({"config": canonical_json(cfg),
                                 "plan": plan_of(argv)}))   # 只打印，不落盘
    run_id = make_run_id(cfg, explicit=arg("--run-id"))  # FR-09-4
    paths = init_run_dir(run_id, cfg)                    # FR-09-5
    redactor = Redactor(); mod02_audit.register_sensitive(redactor)   # FR-09-9
    events.bind(paths.root / "events.jsonl", run_id)             # FR-09-7
    events.emit("infra.run_start", config_hash=config_hash(cfg), git_sha=git_sha())
    return paths, cfg, redactor                                  # redactor 只交给 prompt/memory/reflection 写入方
```

```python
# 单步落盘
def log_step(writer, step_result, policy_step, obs):
    line = {"episode_id": ..., "generation": ..., "harness_version": ...,
            "seed": ..., "split": ..., "step": ..., "obs_digest": obs.digest(),
            "action": policy_step.action.to_dict(), "reward": step_result.reward,
            "cum_reward": ..., "illegal": step_result.info["illegal"],
            "illegal_reason": step_result.info.get("illegal_reason"),
            "llm_raw": policy_step.raw, "llm_latency_ms": policy_step.latency_ms,
            "prompt_tokens": ..., "completion_tokens": ..., "ts": utcnow_iso()}
    validate_trajectory_line(line)        # FR-09-8
    writer.write(line)                    # FR-09-6 + redaction(FR-09-9)
```

```python
# 起跑前门禁（MOD-06 在 loop run 起跑处调用，FR-09-20）——任何推理调用之前
preflight_worktree_clean(repo)            # 脏 → E_IO_GIT_DIRTY（退出码 4）；非 git → False（降级）
# 提交前门禁（MOD-07 accept 的第 3 步调用，FR-09-14）——兜底，此时钱已经花完
worktree_clean_check(repo, allow=("harness",))   # 脏 → E_IO_GIT_DIRTY，拒绝演化
findings = scan_all(Path("dice_ratchet"))    # 候选 harness 落地后、评估前再跑一次
if any(f.code == "E_IO_GUARD_LAYER" for f in findings): raise SandboxViolation(...)
```

## 9. 配置项（键名、类型、默认值、含义）

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `infra.runs_root` | str | `runs` | run 根目录（不参与 config_hash） |
| `infra.log_level` | str | `INFO` | 日志级别（不参与 config_hash） |
| `infra.fsync_every` | int | `0` | 每 N 行 fsync；0 = 不 fsync |
| `infra.rotate_max_mb` | int | `512` | 单 JSONL 文件轮转阈值 |
| `infra.threads` | int | `1` | 传给推理后端与 numpy 的线程数（确定性） |
| `infra.utc` | bool | `true` | 强制 UTC 时间戳 |
| `infra.redaction.enabled` | bool | `true` | 关闭需显式记 `guard_finding` |
| `infra.plot.enabled` | bool | `true` | 无 matplotlib 时自动降级 |
| `infra.git.enabled` | bool | `true` | 关闭后 `commit_harness` 为 no-op 并告警 |
| `infra.exit_on_guard` | bool | `true` | `guards` 违例是否直接非零退出 |

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀）

| 错误码 | 触发 | 处理 |
|---|---|---|
| `E_IO_CONFIG_INVALID` | schema 校验失败 | 打印键路径 + 期望类型，退出码 2 |
| `E_IO_CONFIG_UNKNOWN` | 未知配置键 | 忽略并告警，记入 run_start |
| `E_IO_RUN_CONFLICT` | 同 run_id 不同 config_hash | 退出码 2，提示改用 `--run-id` 或新目录 |
| `E_IO_STATE_MISSING` | `--resume` 但无 state.json | 退出码 2 |
| `E_IO_TRAJECTORY_SCHEMA` | 轨迹行缺字段/类型错 | 开发期抛异常；生产写 `illegal` 兜底行并计数 |
| `E_IO_EVENT_UNKNOWN` | 事件名不在 `EVENT_TYPES` | 同上 |
| `E_IO_TRAILING_TRUNCATED` | 读到最后一行 JSON 不完整 | 丢弃该行并告警（不中断） |
| `E_IO_REDACTION_HIT` | 落盘前擦除命中 | 写 `guard_finding` + 触发 MOD-02 告警 |
| `E_IO_DISK_FULL` | 写入 `OSError` | 立即停止 run，保留已落盘数据，退出码 5 |
| `E_IO_RUNS_NOT_IGNORED` | `runs/` 未被 `.gitignore` 覆盖 | 退出码 2 |
| `E_IO_GIT_DIRTY` / `E_IO_GIT_UNAVAILABLE` | git 门禁失败 / 无 git | 前者阻断演化；后者降级为 `nogit` |
| `E_IO_GUARD_RANDOM` / `E_IO_GUARD_LAYER` | 守卫检出违例 | 退出码 4；演化场景直接拒绝候选 |
| `E_IO_PLOT_UNAVAILABLE` | 缺 matplotlib | 降级为警告 |

**降级原则**：可观测性缺失（绘图、git、fsync）→ 降级 + 告警；**可复现性缺失**（config 非法、run 冲突、守卫违例）→ 立即失败，绝不静默继续。

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例）

| # | 给定 | 当 | 则 |
|---|---|---|---|
| T-09-1 | `default.yaml` 中 `loop.k=30`，环境变量 `DICERATCHET_LOOP__K=5` | `load_config()` | `cfg.loop.k == 5` |
| T-09-2 | 同上且 CLI 传 `--k 7` | `load_config(cli_overrides={"loop.k":7})` | `cfg.loop.k == 7`（CLI 胜出） |
| T-09-3 | 同一 AppConfig，两次不同 `run_id`/时间 | `config_hash()` | 两次结果相同 |
| T-09-4 | 仅 `harness_version` 或 harness 文件不同 | `config_hash()` / `artifact_hash()` | `config_hash` 相同；`artifact_hash` 不同（B-19） |
| T-09-5 | `derive_seed("a", 1, 2)` 在不同 `PYTHONHASHSEED` 子进程 | 调用 | 返回值完全相同 |
| T-09-6 | 已写入 100 行后 `kill -9` | 重新读取文件 | 前 99 行全部可解析；末行截断被 `E_IO_TRAILING_TRUNCATED` 识别 |
| T-09-7 | redactor 注册了 heldout seed `2042` | ① 写 prompt 快照 `"…seed 2042…"`；② 写 heldout 轨迹行 `"seed": 2042` | ① 替换为 `<REDACTED:seed>` 并产生 1 条 `infra.redaction_hit`；② 原样落盘、schema 校验通过、不产生事件 |
| T-09-16 | 已接受 1 代（harness v1）的 run | `loop run --run-id X --resume` | 通过 `config_hash` 校验并继续 |
| T-09-8 | 轨迹行缺 `obs_digest` | `validate_trajectory_line()` | 抛 `E_IO_TRAJECTORY_SCHEMA` |
| T-09-9 | fixture 中 `dice_ratchet/agent/policy.py` 写了 `import dice_ratchet.env.engine` | `scan_layer_imports()` | 返回 1 条 `E_IO_GUARD_LAYER`，定位到具体行号 |
| T-09-10 | fixture 中 `random.random()` 未豁免 | `scan_determinism()` | 返回 `E_IO_GUARD_RANDOM`；加 `# guard: allow` 注释后 `exempted=True` 且仍可见 |
| T-09-11 | 干净的 git 仓库 | `commit_harness(3, "add heuristic")` | 产生 commit，信息以 `harness(v3):` 开头，返回 40 位 sha |
| T-09-12 | 已存在同 run_id 且 config_hash 不同 | `init_run_dir()` | 抛 `E_IO_RUN_CONFLICT` |
| T-09-13 | `--dry-run` 的 `loop run` | 执行 | stdout 为可解析 JSON，`runs/` 无任何新文件 |
| T-09-14 | 伪造 `generations.csv` | `plot_generations()` | 生成 5 张 png；缺 matplotlib 时返回 `[]` 且不抛异常 |
| T-09-15 | 各 CLI 子命令传非法参数 | 执行 | 退出码 2，stderr 含错误码 |

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

- **G1 单测**：`dice_ratchet/infra/` 覆盖率 ≥ 80%（`config`、`logging`、`guards` 三个文件 ≥ 90%）。
- **G2 Golden**：§11 全部 15 条用例作为回归集，`guards` 的 fixture 目录纳入版本控制。
- **G3 确定性**：`config_hash` 与 `derive_seed` 的稳定性由 T-09-3/4/5 保证，并在 CI 中跨 `PYTHONHASHSEED` 跑一次。
- **G4 泄漏审计**：T-09-7 通过；`audit leak` 的输出文件由本模块写入 `audit/`。
- **G5 Schema 校验**：轨迹行 schema 有 round-trip 测试（`dict → validate → jsonl → parse → dict` 等价）。
- **G6 可复现命令**：`README`/PRD 中出现的命令可原样复制执行。
- **M0 交付判定**：`python -m dice_ratchet guards check` 与 `python -m dice_ratchet env selftest --seed 1` 均可运行；`init_run_dir` 能创建完整目录树；JSONL 轨迹可被 MOD-08 的汇总脚本直接消费。

## 13. 指标与埋点

`run_start` / `run_end` 事件（`run_id`、`config_hash`、`git_sha`、`duration_s`、`exit_code`）；`guard_finding` 事件（`code`、`path`、`line`、`exempted`）；`E_IO_REDACTION_HIT` 计数（应为 0，非 0 即沙箱告警）；单行写入耗时 p50/p99 记入 `metrics/io_latency.csv`。

## 14. 依赖与风险

| 依赖 | 说明 |
|---|---|
| Python 3.11+ 标准库 | `dataclasses` / `json` / `hashlib` / `ast` / `subprocess`；绘图可选 matplotlib |
| git CLI | `git_ops` 与 `run_id`；缺失时降级 |
| MOD-02 | 提供敏感值集合给 `Redactor`；反向依赖必须通过回调注册，避免循环 import |

| 风险 | 对策 |
|---|---|
| infra 被塞进领域逻辑（最易发生的腐化） | NFR-09-5：`infra` 不得 import 其他子包，由 `guards` 强制 |
| redaction 误伤（把正常数字当成 seed 擦掉） | 只作用于 prompt/memory/reflection 出口，并且只匹配带词边界的精确值；T-09-7 ② 为反向用例 |
| fsync 拖慢单步 | 默认 `fsync_every=0`，仅在长跑 run 建议设 200 |
| 时间戳/时区导致不可复现 | 强制 UTC + `ts` 不参与任何 hash |
| CLI 与 Spec 漂移 | FR-09-10：新增命令必须同步更新 Spec §4.6，PR 检查项 |

## 15. 里程碑与工作量估算（人日）

| 阶段 | 内容 | 人日 |
|---|---|---|
| M0a | config + run_id + 目录树 + JsonlWriter + 错误码注册表 | 2.0 |
| M0b | guards（AST 扫描 + CI 钩子）+ 确定性环境引导 | 1.5 |
| M1 | CLI 子命令树 + `--dry-run` + 退出码 + 轨迹 schema 校验 | 1.5 |
| M2 | git_ops + redaction + state 原子写 + `--resume` 定位 | 1.0 |
| M3 | plotting + 报告汇总脚本（`generations.csv` 列定义归 MOD-08） | 1.0 |
| 合计 | | **7.0 人日** |

**实际交付（M0）**：M0a+M0b（config/事件/JSONL/守卫/git/CLI）于 **2026-10-01 → 2026-10-03** 分 4 个提交交付（`5d3574c`、`bbdc02e`、`aa62ab8`、`e621112`），并**额外提前交付了 M1 的 CLI 子命令树与退出码**。
本项目**未单独记工**；按提交时间窗折算 ≤ 3 人日（计划 M0a+M0b = 3.5 人日），偏差详见 [排期 §8](../项目排期-DiceRatchet.md)。
T-09-1～T-09-16 全部落地（`bbdc02e` 起）；证据见 [M0 验收报告](../reviews/2026-10-03-M0验收报告.md)（G1–G6 与 INV-1/INV-6 全通过，CI 门禁接线已补齐为 `scripts/ci_gate.sh` + `.github/workflows/ci.yml`）。

## 16. 开放问题

1. `AppConfig` 用 frozen dataclass 还是引入 pydantic？阶段一选前者（少依赖），若配置项超过 ~60 个则重新评估。
2. `state.json` 的并发写：目前假设单 run 单写进程；若 MOD-06 引入进程池共享状态，需加文件锁（`fcntl.flock`）。
3. 轨迹 CSV 与 JSONL 的职责边界——是否在 M3 增加 Parquet 导出以支撑更复杂分析？
4. `--dry-run` 是否需要模拟「一次完整的代际演化」计划（含候选数、预计局数、预计耗时）？会显著提升可操作性但也增加耦合。
5. `E_IO_*` 与其他模块 `E_*` 的归属：模型不可用引发的 IO 失败应报 `E_LLM_*` 还是 `E_IO_*`？建议以**根因方**为准并写入 Spec §8。
6. 是否需要把 `guards` 做成 pre-commit hook（当前只做 CLI + CI）？

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v0.1 | 2026-09-30 | 首版 |
| v0.2 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：`config_hash` 不再包含 harness_version，新增 `artifact_hash`（P0-7）；redaction 只作用于 prompt/memory/reflection（P0-7）；事件改为 `<module>.<event>` 注册表并统一落到 `events.jsonl`（P1-11）；轨迹 schema 对齐 §4.5 新字段与 `episode_key` 路径（P1-10）；退出码新增 5（P1-13）；CLI 对齐 §4.6，统一使用 `--run-id`（P1-14）；git tag 改为 `harness-v{N}`（P1-15）；新增 FR-09-18 schema 与原子写、FR-09-19 `runs/` 忽略检查 |
| v0.3 | 2026-09-30 17:49 | FR-09-1 按 Spec B-26 插入 harness/config.json 层及白名单校验 |
| v0.3.1 | 2026-10-03 | 修复 `bootstrap_process()` 早发 `infra.run_start`（缺必填 `config_hash`）：`DICERATCHET_DEV=1` 下任何命令都会以退出码 1 失败（用 `guards check` 复现）。改为进程快照由 `cli_init_run()` 的 run_start 记录，并新增 `process_snapshot()`（FR-09-17）；`audit leak` 退出码注释同步为 `0/1/2/4` |
| v0.5 | 2026-10-10 | **F47-A/C1：`worktree_clean_check` 的「演化前门禁」落点 + `commit_harness` 失败自清（对齐 Spec v2.5 B-56/B-57）**。① 新增 **FR-09-20 `preflight_worktree_clean(repo, allow=())`**：同一判据的**起跑前**形态——非 git worktree 返回 `False` 降级跳过（Spec §8.5），否则脏即抛 `E_IO_GIT_DIRTY`（退出码 4）；由 MOD-06 在 `loop run` 起跑处调用（MOD-06 §9.3）。成因：FR-09-14 的判据原先**唯一**调用点在 `commit_harness()` 内部 = accept 第 3 步 ⇒ 脏工作区要等整代跑完才拒（实测 3.75 h / ≈¥47）。② **`commit_harness` 改为先门禁后 `git add`**，并在失败路径 `git reset --quiet -- harness`（实测旧顺序失败后残留 `MM harness/decision.md` 与 `A  harness/versions/v2/decision.md`）。③ §8 伪代码把两种调用时机分开写清（起跑前 = FR-09-20 / 提交前 = FR-09-14 兜底）。④ 测试：`tests/unit/infra/test_git_ops.py` 新增 4 条（preflight 干净/脏/非 git、失败无 staged 残留 + 旧顺序反例自检、成功路径仍正常暂存）。 |
| v0.4 | 2026-10-10 | **轨迹 `episode_key` 的 `arm` 位允许 `-smoke` 后缀（F41，对齐 Spec v2.3 B-50）**。MOD-07 FR-07-6 的冒烟臂是 `parent-smoke` / `cand-{id}-smoke`，而登记模式不含该后缀 ⇒ 冒烟局**每一行**轨迹都失败 `E_IO_TRAJECTORY_SCHEMA` → 该局 dropped（轨迹文件 0 行）→ `n_dropped/n = 1.0 > drop_rate_max` → `low_confidence` → `error_codes` 带 `E_EVAL_INSUFFICIENT_N` → `judge.smoke_fast_reject` 返回 `smoke_error:…` ⇒ **每个候选都在冒烟关被淘汰，`harness/versions/` 永不可能有新版本**。该语法在**三处**有副本，本版三处同步：§6.2 的 schema 片段、`infra/logging.py` 的 `_EPISODE_KEY_RE`、`schemas/trajectory_line.schema.json`；前两处加了一致性用例 `test_episode_key_patterns_stay_in_sync` 防再次漂移（F41 的成因就是「写了两遍、只改一处」）。 |
