# MOD-06 loop 技术 PRD

> 版本：v1.2（2026-09-30 17:49 按 Spec v1.4 回填裁决，见 §17）· 状态：草案（待评审）· 所属里程碑：M2 · 上游 Spec：[模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md)（唯一事实来源，冲突时以 Spec 为准）· 作者：MOD-06 模块作者
> 代码路径：`gamer_rsi/loop/{runner.py, reflect.py, generation.py}` · 产物路径：`runs/<run_id>/`

## 1. 背景与目标

MOD-06 是 L2 进化层里的**编排器**：它自己不产生策略、不训练模型、不判定改进好坏，只负责让 `play → evaluate → reflect → evolve` 这条闭环在一局、K 局、一代三个时间尺度上正确、可复现、可中断、可恢复地跑起来。目标（对齐计划 §5 闭环图与 §7 里程碑 M2）：

1. **单局可跑通**：`EpisodeRunner` 用固定 seed 驱动完整 episode，产出逐字节可复现的 JSONL 轨迹。
2. **局末必反思**：每局结束（win/lose/truncated）触发一次反思，结构化后写入 MOD-05；反思失败只降级、不阻塞主循环。
3. **每 K 局一次演化周期**：默认 K=30，触发 MOD-07；周期边界幂等，中断重入不重复触发。
4. **run 级状态可续跑**：`runs/<run_id>/state.json` 记录 generation、已完成 seeds、待做队列、HITL 挂起标记；`--resume` 后不重复计分。
5. **失败不丢进度**：模型超时 / 环境异常 / 磁盘失败 / Ctrl-C 各有明确行为与 `E_*` 错误码。

**成功定义（M2 验收）**：`loop run --generations 3 --k 30`（或小 K 冒烟）无人工干预跑通 ≥1 轮演化；中途 `Ctrl-C` 后 `--resume` 接着跑完，且 `metrics/scoreboard.json` 中每个 `episode_key` 只出现一次计分记录。

## 2. 范围

### 2.1 范围内

单局决策循环（`observe → policy.play_decision → 逐行落盘 → 终止判定`，步数/决策上限读 Spec §8.6 的 `episode.*`）；局末反思编排（MOD-03/04 + `harness/prompts/reflection.md`，结构化后写 MOD-05，失败降级）；演化周期调度（每 K 局触发 MOD-07、`generation` 计数器、读取 `harness/version.json`、周期边界幂等）；run 级状态机、产物目录布局、`--resume` 断点续跑、计分幂等去重；失败恢复矩阵与 `E_*` 错误码映射（前缀取自 Spec §8.3）；HITL 挂起/恢复的跨进程状态设计（配合 `evolve approve|rollback`）；成本控制（按 Spec §8.6 统一计算每代局数 `E_gen`、预算耗尽行为）；并发（评估局局级并发 + 共享推理队列，训练局串行 + 共享记忆写入保护）；结构化事件与指标埋点。

### 2.2 范围外（非目标）

逐字复用 Spec §3 中 MOD-06 的「不负责」清单：

> - **不负责**：判定候选改进是否被接受（MOD-07）、计算 held-out 指标（MOD-08）、harness 文件的具体 diff（MOD-07）。

补充边界澄清（非新增职责，只防漂移）：不实现游戏规则（MOD-01）、不做观测投影（MOD-02）、不加载模型（MOD-03）、不组装 prompt 正文（MOD-04）、不实现记忆存储（MOD-05）；不定义 seed 切分协议（MOD-08）与 seed 注册表（MOD-09），只**读取**已切分好的 seed 清单并消费；不自建日志格式与 `run_id` 生成（MOD-09）；不写 harness 文件、不做 git commit（MOD-07/09）。

## 3. 使用方与场景

| 使用方 | 场景 | 用到的接口 |
|---|---|---|
| 人类研究者（CLI） | 冒烟一局、跑 N 代、看进度、Ctrl-C 后续跑、处理 HITL | `loop run/resume/status`、`evolve approve\|rollback` |
| MOD-07 evolve | 每代周期边界被调用；评估直接调 MOD-08 `evaluate_arm`（Spec §4.7），本模块提供 `run_episode` 作为底层执行器 | `evolve_fn(state, run_ctx)` |
| MOD-08 evals | 从轨迹与计分台账算指标 | 文件契约（§7），不调函数 |
| 回归测试 | 固定 seed 跑 3 局做 golden/冒烟 | `run_episode()` |
| 未来 MOD-10 distill | 复用 run 级状态与轨迹目录 | 文件契约 |

可复现命令（G6，必须可直接复制执行）：

```bash
python -m gamer_rsi loop run --agent llm --seeds train:1000-1029 --k 30 --hitl
python -m gamer_rsi loop status --run-id <run_id>
python -m gamer_rsi loop run --run-id <run_id> --resume
python -m gamer_rsi loop smoke --seeds train:1000-1002 --k 3 --no-reflect   # 只验编排，不调模型
```

## 4. 功能需求（FR-06-x，编号，每条可测试）

- **FR-06-1（单局时序）**：`run_episode()` 严格按以下顺序执行：
  1. `policy.reset(ctx)`；
  2. `env.reset(seed, episode_config)`；
  3. 循环：`policy.play_decision(env, obs, ctx)` → 把 `DecisionResult.steps` 逐行落盘 → 终止判定。

  终止条件**只看** `last.terminated or last.truncated`。步数上限、决策上限和 `policy_failure` 都由 MOD-02 统一判定并以 `truncated` 返回（Spec B-15），本模块不再维护 `illegal_streak` 或 `max_steps`。截断局 `outcome="lose"`，另记 `truncation_reason`，计入样本（Spec §8.6）。
- **FR-06-2（每步必落盘）**：每个 `StepRecord`（含 reject 行）立即追加到 `episodes/<split>/<episode_key>.jsonl` 并 `flush`，字段按 Spec v1.3 §4.5（MOD-09 schema 校验）。LLM 字段只挂在决策内第一个执行的动作行上。局末追加 `episode_end` 行。崩溃时最多丢当前决策。
- **FR-06-3（局末反思与用量回写）**：训练局结束（含截断）后：
  1. 调用 `record_usage(episode_key, memory_ids, outcome, adopted_ids=…, tick=…)` 回写本局所有决策的记忆使用情况（Spec §4.7）。
  2. 若 `reflect_enabled`，由 `loop.reflect.reflect(summary, client, template)` 渲染 `harness/prompts/reflection.md` → MOD-03 `generate_json()` → 通过 MOD-05 的 `ReflectionEntry` schema → `add_reflection(entry, provenance)`。

  `EpisodeSummary` 是确定性摘要：结局、关数、心数、战斗数、非法/fallback 次数，以及最近 `reflect_context_window` 局的同类摘要；由本模块从轨迹生成，不经 LLM。

  评估局不反思、不回写（INV-5）。
- **FR-06-3a（逻辑时钟）**：`tick` = 已完成的训练局数，每局结束 +1，写入 `state.json`；它是 MOD-05 淘汰、TTL 与时新度的唯一时间源（Spec §8.1）。
- **FR-06-4（反思降级）**：反思任一步失败（超时/JSON 非法/schema 不符/MOD-05 写失败）→ 事件 `reflect` 记 `status="skipped"`，`reflections/<seed>.json` 写 `{"skipped": true, "reason": "<E_*>"}`，主循环继续。**反思失败绝不判该局失败、绝不阻塞 run**。
- **FR-06-5（周期触发）**：每完成 K 局训练局触发一次演化周期，`K = loop.k`（默认 30）；条件为 `len(completed_train_seeds) % K == 0` 且 `generation < loop.max_generations` 且 `phase == "playing"`。
- **FR-06-6（周期幂等）**：同一 `cycle_index` 至多触发一次：先原子落盘 `phase="evolving"` + `evolving_cycle=cycle_index`，再调 MOD-07；返回后写 `last_completed_cycle=cycle_index` 并回 `phase="playing"`。若进程在 `evolving` 阶段被杀，`--resume` **不重跑** MOD-07，而是检查其产物（`generations/gen-XXX/report.json` / `decision.json`）存在性：存在则标该 cycle 完成，否则重置 `phase="playing"` 后重新触发。
- **FR-06-7（generation 与 harness_version）**：`state.generation` 单调不减，仅在 MOD-07 返回「本代已裁决」（接受或拒绝均算）后 `+1`；`harness_version` 每局开始时从 `harness/version.json` 重读（不跨代缓存），故 HITL 挂起期间人工切换 harness 立即生效。
- **FR-06-8（run 状态机）**：`state.json` 至少含 `run_id / phase / generation / harness_version / k / completed_train_seeds / pending_queue / hitl_pending / counters / budget / updated_at`（§7.1）；`phase ∈ {pending, playing, reflecting, evolving, hitl_wait, done, failed}`。
- **FR-06-9（断点续跑）**：`loop run --run-id <id> --resume` 读 `state.json`（`config_hash` 不含 harness，所以演化过的 run 也能续跑，Spec B-19），跳过 `completed_train_seeds`，从 `pending_queue` 头部继续，不重复计分（§7.3 幂等键）。
- **FR-06-10（计分幂等）**：计分只发生在 episode 正常收尾；写入前检查 `metrics/scoreboard.json` 是否已有该 `episode_key`，追加后 fsync。同 seed 因 `--force-rerun` 重跑时，旧记录标 `superseded_by` 而非删除。
- **FR-06-11（HITL 挂起）**：`loop.hitl=true` 时 MOD-07 产出候选与决策记录后不直接生效；MOD-06 写 `state.hitl_pending={"cycle","generation","candidate_dir","report"}`、置 `phase="hitl_wait"`、退出码 `0`（正常挂起非错误）。人工执行 `evolve approve|rollback --generation g`（MOD-07 命令，MOD-06 只消费结果状态）后，`loop run --run-id <id> --resume` 读 `harness/version.json` 与 `hitl_pending`，清空标记并继续。
- **FR-06-12（评估执行器）**：MOD-08 `evaluate_arm()` 通过注入的 `EpisodeRunner.run_episode(split="heldout", arm=...)` 执行评估局。本模块只负责执行与落盘，**不做任何聚合指标**（聚合归 MOD-08）。v1.0 的 `run_evaluation_batch()` 已并入 MOD-08 门面。
- **FR-06-13（成本闸门，Spec v1.3 §8.6）**：
  - 每代启动前按 `E_gen = K + (H + S) + C·(S + H)` 计算本代局数：训练 K、parent 的 heldout H 与冒烟 S、C 个候选各自的冒烟与 heldout。默认 30 + 45 + 2×45 = 165。
  - `E_gen > loop.max_episodes_per_generation`（**默认 120**，2026-10-06 M2 开工 D-1 由 170 下调；D-1 下 `E_gen = K + (1+C)(S+H) = 30 + 2×42 = 114`）时拒绝启动该代，记 `loop.budget_exhausted`，退出码 1，并提示下调 C 或 S。这是全系统**唯一**的成本上限，MOD-07 不另设。
  - 每代开始写一条 `loop.cycle_start{e_gen, est_hours}` 事件，其中 `est_hours = E_gen × D × t_d`，`t_d` 取 MOD-03 实测 p50。
- **FR-06-13a（gen-000 基线）**：run 启动时（`generation=0`），先对 harness v0 在 H1 上调 MOD-08 `evaluate_arm` 与 `evaluate_generation`，生成 `generations/gen-000/report.json`（`decision="baseline"`），后续各代与它对比。
- **FR-06-14（并发纪律）**：
  - 训练局串行（`train_concurrency` 固定 1）；写共享记忆须持 `runs/<run_id>/locks/memory.lock` 文件锁。
  - 评估局采用**局级并发**：`eval_concurrency` 默认 2，同一进程内用 asyncio/线程，**共享同一个 `BackendManager` 推理队列**（Spec P-10 / §8.6）。评估局使用冻结的 harness 快照与 `memory.frozen(v)` 只读视图，禁止写记忆库。
  - v1.0 的进程池方案每个 worker 各载一份权重，24 GB 内存放不下，已废弃。
- **FR-06-15（信号处理）**：捕获 `SIGINT`/`SIGTERM`：完成当前 step 落盘 → 写 `state.json`（附 `interrupted_at`）→ flush → 退出码 `130`；第二次信号立即 `os._exit(130)`。

## 5. 非功能需求（NFR-06-x：确定性/性能/可复现/可观测）

- **NFR-06-1（确定性）**：同一 `run_id` + 同 seed 清单 + 同 `harness_version` + `mode=eval`（温度 0）时，评估局与并发度无关，且`obs_digest`/`action`/`reward` 序列逐字节一致；所有随机来自 `gamer_rsi.infra.rng_for(seed, namespace)`，禁用 `random`/`time` 相关随机（MOD-09 `guards.py` CI 扫描）。
- **NFR-06-2（可复现）**：`state.json` 能独立重建「下一步做什么」，不依赖内存态；`runs/<run_id>/config.json` 固化合并后配置（含 `config_hash`）。
- **NFR-06-3（性能）**：编排层单步开销（不含推理与环境 step）≤ 5 ms（M5 本机）；每步 ≤ 1 次 append+flush；代际目录用增量读，不整目录 `glob`。
- **NFR-06-4（可观测）**：事件统一经 MOD-09 `emit` 写入 `events.jsonl`，名称为 `loop.{episode_start,episode_end,cycle_start,reflect,budget_exhausted,interrupted,resume}`（Spec §8.2）。轨迹行不是事件。v1.0 的 `metrics/loop_trace.jsonl` 已废弃。
- **NFR-06-5（崩溃安全）**：`state.json` 与 `scoreboard.json` 一律「临时文件 + `os.replace`」原子替换；`episodes/*.jsonl` 追加+flush；任意时刻 `SIGKILL` 最多丢当前步，状态文件不自相矛盾。
- **NFR-06-6（可测性）**：`EpisodeRunner` 依赖可注入（`env/policy/memory/clock/log_writer`），全部可用 `tests/fakes.py` 假实现替换；`loop smoke --no-reflect` 不触网不调模型，< 2 s 跑完 3 局。
- **NFR-06-7（内存）**：常驻内存不随局数增长：轨迹流式写盘不累积 `list[StepResult]`，反思上下文只保留最近 `reflect_context_window` 局摘要。

## 6. 接口契约（真实 Python 签名 + JSON Schema）

```python
# gamer_rsi/loop/runner.py —— 对 MOD-04/05 的调用签名以 Spec v1.3 §4.7 为准
class PolicyProto(Protocol):
    def reset(self, ctx: "EpisodeContext") -> None: ...
    def play_decision(self, env: SandboxEnv, obs: Observation, *, ctx: "EpisodeContext") -> "DecisionResult": ...
class MemoryProto(Protocol):
    def add_reflection(self, entry: "ReflectionEntry", *, provenance: "Provenance") -> str: ...
    def record_usage(self, episode_key: str, memory_ids: Sequence[str], outcome: str, *, adopted_ids: Sequence[str], tick: int) -> None: ...
    def snapshot(self, version: int) -> Path: ...
    def frozen(self, version: int) -> "MemoryView": ...

@dataclass(frozen=True)
class RecordedEpisode:
    episode_key: str; seed: int; split: Literal["train","heldout","test"]; arm: str
    generation: int; harness_version: int; steps: int; decisions: int; cum_reward: float
    outcome: Literal["win","lose"]; truncation_reason: str | None      # 截断计 lose
    stages_cleared: int; hearts_left: int; battles_won: int              # INV-7（来自 info）
    illegal_attempts: int; fallback_decisions: int; degraded: bool; exclude_reason: str | None
    jsonl_path: str; reflection_id: str | None; wall_ms: int

class EpisodeRunner:
    def __init__(self, *, env: SandboxEnv, policy: PolicyProto, memory: MemoryProto,
                 writer: "JsonlWriter", config: "LoopConfig", run_ctx: "RunContext") -> None: ...
    def run_episode(self, seed: int, *, split: str, arm: str,
                    episode_config: EpisodeConfig, harness_version: int) -> RecordedEpisode: ...

# gamer_rsi/loop/reflect.py —— 返回 None = 降级跳过，由调用方写 skipped 标注（FR-06-4）
def summarize_episode(rec: RecordedEpisode, *, window: Sequence[RecordedEpisode]) -> "EpisodeSummary": ...  # 确定性摘要
def reflect(summary: "EpisodeSummary", *, client: "InferenceClient", template: Path,
            timeout_s: float = 60.0) -> "ReflectionEntry | None": ...

# gamer_rsi/loop/generation.py —— 评估一律委托 MOD-08 evaluate_arm（Spec §4.7），本模块不自建评估批
def episodes_per_generation(cfg: "LoopConfig") -> int: ...              # FR-06-13：K + (H+S) + C·(S+H)
def maybe_run_generation(*, state: "RunState", run_ctx: "RunContext",
                         evolve_fn: "Callable[[RunState, RunContext], EvolveOutcome]",
                         force: bool = False) -> "RunState": ...   # 幂等：同 cycle_index 只调一次
class LoopOrchestrator:
    def __init__(self, *, run_ctx: "RunContext", config: "LoopConfig",
                 runner: EpisodeRunner, evolve_fn=None) -> None: ...
    def run(self, seeds: Sequence[int] | None = None, *, resume: bool = False) -> "RunSummary": ...
    def status(self) -> "RunState": ...
```

```jsonc
// runs/<run_id>/state.json —— JSON Schema（draft 2020-12 摘要）
{"type":"object","required":["run_id","phase","generation","harness_version","k",
  "completed_train_seeds","pending_queue","hitl_pending","counters","budget","updated_at"],
 "properties":{"run_id":{"type":"string"},
  "phase":{"enum":["pending","playing","reflecting","evolving","hitl_wait","done","failed"]},
  "generation":{"type":"integer","minimum":0},"harness_version":{"type":"integer","minimum":0},
  "k":{"type":"integer","minimum":1,"default":30},
  "completed_train_seeds":{"type":"array","items":{"type":"integer"},"uniqueItems":true},
  "pending_queue":{"type":"array","items":{"type":"integer"}},
  "hitl_pending":{"type":["object","null"],"properties":{"cycle":{"type":"integer"},
    "generation":{"type":"integer"},"candidate_dir":{"type":"string"},"report":{"type":"string"}}},
  "last_completed_cycle":{"type":["integer","null"]},"evolving_cycle":{"type":["integer","null"]},
  "counters":{"type":"object","properties":{"episodes_total":{"type":"integer"},
    "episodes_truncated":{"type":"integer"},"reflections_ok":{"type":"integer"},
    "reflections_skipped":{"type":"integer"},"illegal_actions":{"type":"integer"},
    "env_errors":{"type":"integer"},"llm_timeouts":{"type":"integer"},
    "io_retries":{"type":"integer"}}},
  "budget":{"type":"object","properties":{"eval_episodes_used":{"type":"integer"},
    "eval_episodes_cap":{"type":"integer"},"candidates_used":{"type":"integer"}}},
  "interrupted_at":{"type":["string","null"]},"updated_at":{"type":"string","format":"date-time"}}}

// runs/<run_id>/reflections/<episode_key>.json —— 内容为 MOD-05 ReflectionEntry（schema 归 MOD-05）+ 调用元数据
{"reflection_id":"refl-88ac1d0b2fe4","episode_key":"e-001042-g003-train","seed":1042,"generation":3,
 "harness_version":7,"tick":97,"outcome":"lose","truncation_reason":null,
 "status":"ok|skipped","reason":null,
 "entry":{"text":"...","failure_cause":"virtue_management","stages_cleared":4,"hearts_left":0,
          "battles_won":9,"tags":["low_virtue","defense"]},
 "llm":{"model":"qwen3-8b","latency_ms":1900,"prompt_tokens":1200,"completion_tokens":180},
 "template_sha256":"sha256:..."}

// runs/<run_id>/metrics/scoreboard.json —— 每个训练局一条，计分幂等依据
{"episodes":[{"episode_key":"e-001042-g003-train","seed":1042,"split":"train","generation":3,
  "harness_version":7,"outcome":"lose","truncation_reason":null,"steps":231,"decisions":97,
  "cum_reward":-0.12,"stages_cleared":4,"hearts_left":0,"battles_won":9,
  "illegal_attempts":2,"fallback_decisions":0,"superseded_by":null}]}
```

## 7. 数据模型与存储

### 7.1 run 产物目录（扩展 Spec §5）

```
runs/<run_id>/
  config.json             # 合并后配置 + config_hash（MOD-09 写，本模块只读）
  state.json              # run 级状态机（本模块权威，§6 schema）
  queue.jsonl             # 待做队列追加式事件日志（add/done/skip），供恢复与审计
  events.jsonl            # 结构化事件（MOD-09 emit）
  episodes/<split>/<episode_key>.jsonl   # 每步一行（Spec v1.3 §4.5 字段，本模块写）
  reflections/<seed>.json # 每局一份反思或 skipped 标注
  generations/gen-<NNN>/  # MOD-07 写的候选与报告；本模块只读、只检查存在性
  metrics/scoreboard.json # 计分台账（幂等键 episode_key）
  locks/memory.lock       # 训练局共享记忆写入文件锁
  audit/                  # MOD-02/09 审计产物
```

### 7.2 状态机转移

`pending → playing → (reflecting → playing)* → evolving → (hitl_wait → playing | playing) → … → done`；异常边：任意 phase → `failed`（不可恢复，如 `E_IO_RUN_STATE`）；`playing → interrupted`（临时态，恢复回 `playing`）。

### 7.3 幂等键

- **计分幂等**：`episode_key = f"e-{seed:06d}-g{generation:03d}-{arm}"`（Spec v1.3 §4.5 / B-16，取代 P-13 的旧格式）。同一 seed 在不同代、不同臂下各计一条记录，同代同臂内重复执行只保留一条。
- **周期幂等**：`cycle_index = len(completed_train_seeds) // k`，以 `last_completed_cycle` + `evolving_cycle` + MOD-07 产物存在性三重校验。
- **反思幂等**：`reflections/<seed>.json` 已 `status=="ok"` 则跳过；`skipped` 需 `--retry-reflections` 显式重试。
- **已裁决（Spec v1.4 B-36，与 P-14 一致）**：训练/评估 seed **必须**经 MOD-08 `evals.seeds.take` 取用，MOD-06 不得自行按 `seed % 2` 之类规则切分；仅校验传入 seed 的 split 标签与 MOD-08 注册表一致，不一致立即抛 `E_EVAL_SPLIT_VIOLATION`（守 INV-5）。

## 8. 关键流程（伪代码或时序）

```python
# EpisodeRunner.run_episode —— 决策时序（FR-06-1/2）
def run_episode(self, seed, *, split, arm, episode_config, harness_version):
    key = f"e-{seed:06d}-g{self.run_ctx.state.generation:03d}-{arm}"
    ctx = EpisodeContext(run=self.run_ctx.run_id, episode=key, seed=seed, split=split,
                         mode="train" if split == "train" else "eval", tick=self.run_ctx.state.tick,
                         memory=self.memory if split == "train" else self.memory.frozen(self.run_ctx.memory_version))
    self.policy.reset(ctx)
    obs = self.env.reset(seed, episode_config=episode_config)
    emit("loop.episode_start", episode_key=key, seed=seed, split=split, arm=arm, harness_version=harness_version)
    used, adopted, cum = [], [], 0.0
    with self.writer.episode(split, key) as w:        # episodes/<split>/<key>.jsonl，append + flush
        while True:
            d = self.policy.play_decision(self.env, obs, ctx=ctx)   # MOD-04：计划 + 重试 + fallback
            for i, rec in enumerate(d.steps):
                cum += rec.result.reward
                w.write_step(rec, cum_reward=cum, llm=d.llm if i == first_llm_step(d) else None,
                             prompt_hash=d.prompt_hash, memory_ids=d.memory_ids)   # Spec §4.5（FR-06-2）
            used += d.memory_ids; adopted += d.adopted_memory_ids
            obs = d.last.observation
            if d.last.terminated or d.last.truncated: break    # 截断由 MOD-02 统一判定（B-15）
        w.write_end(native_of(d.last.info))                    # episode_end 行
    rec = RecordedEpisode(episode_key=key, outcome=d.last.info["outcome"] or "lose", ...)
    if split == "train":
        self.memory.record_usage(key, dedup(used), rec.outcome, adopted_ids=dedup(adopted), tick=ctx.tick)
    emit("loop.episode_end", **asdict(rec))
    return rec

# LoopOrchestrator.run —— 局/周期双层调度（FR-06-5/6/9）
def run(self, seeds=None, *, resume=False):
    st = load_state(self.run_ctx) if resume else init_state(self.run_ctx, seeds)
    st.pending_queue = [s for s in (seeds or st.pending_queue)
                        if s not in set(st.completed_train_seeds)]   # 不重复做（FR-06-9）
    save_state(st)                                                   # 原子替换
    while st.pending_queue:
        st.phase, st.harness_version = "playing", read_harness_version()  # 每局重读（FR-06-7）
        seed = st.pending_queue[0]
        rec = self.runner.run_episode(seed, split="train", arm="train", harness_version=st.harness_version,
                                      episode_config=self.run_ctx.episode_config)
        score_once(st, rec)                       # 幂等：episode_key 已存在则跳过（FR-06-10）
        st.completed_train_seeds.append(seed); st.pending_queue.pop(0)
        st.counters.episodes_total += 1
        st.counters.episodes_truncated += (rec.truncation_reason is not None)
        st.tick += 1                              # 逻辑时钟（FR-06-3a）
        st.phase = "reflecting"; save_state(st)
        r = reflect(summarize_episode(rec, window=recent(st)), client=self.client,
                    template=harness_path("prompts/reflection.md"))            # FR-06-3
        write_reflection_or_skip(rec, r)          # None → {"skipped":true,...}（FR-06-4）
        st.phase = "playing"; save_state(st)
        cycle = len(st.completed_train_seeds) // st.k
        prev = -1 if st.last_completed_cycle is None else st.last_completed_cycle
        if cycle > prev and st.generation < self.config.max_generations:   # 唯一触发条件（FR-06-5 以此为准）
            if episodes_per_generation(self.config) > self.config.max_episodes_per_generation:
                emit("loop.budget_exhausted"); st.phase = "failed"; save_state(st); return summarize(st)   # FR-06-13
            st.phase, st.evolving_cycle = "evolving", cycle
            save_state(st)                        # 先落盘再调 MOD-07（FR-06-6）
            out = self.evolve_fn(st, self.run_ctx)     # MOD-07：候选生成+held-out 裁判+裁决
            st.last_completed_cycle, st.evolving_cycle = cycle, None
            st.generation += 1                    # 接受或拒绝均 +1（FR-06-7）
            if self.config.hitl and out.pending_human:
                st.hitl_pending = {"cycle": cycle, "generation": st.generation,
                                   "candidate_dir": out.candidate_dir, "report": out.report}
                st.phase = "hitl_wait"; save_state(st); return summarize(st)   # 退出码 0
            st.phase = "playing"; save_state(st)
    st.phase = "done"; save_state(st); return summarize(st)
```

**并发时序（FR-06-14）**：评估一律经 MOD-08 `evaluate_arm()` 执行：局级并发 `eval_concurrency`（默认 2），所有局共享同一个 `BackendManager` 串行推理队列（P-10），每局有独立的 `SandboxEnv` 与 `memory.frozen(v)` 只读视图。评估局**不写** `scoreboard.json` 主台账，只写 `episodes/<split>/`（Spec P-15 已采纳）。并发只能掩盖 env 与 IO 的开销，不能缩短推理瓶颈；耗时按 Spec §8.6 估算。

## 9. 配置项（键名、类型、默认值、含义）

| 键 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `loop.k` | int | 30 | 每 K 局训练触发一次演化周期（计划 §5：K=30） |
| `loop.max_generations` | int | 3 | run 内最大演化代数（M3 验收 ≥3 轮） |
| `episode.max_env_steps` / `episode.max_decisions` | int | 1000 / 300 | 只读（Spec §8.6 唯一键，由 MOD-02 执行）；v1.0 的 `loop.max_steps_per_episode=120` 小于默认局 p50 的 232 步，已删除 |
| `episode.difficulty` / `episode.stage_count` | str / int | `hard` / `6` | 默认局（Spec §8.6） |
| `loop.action_retry_r` | int | 2 | 非法动作同一步重试次数（MOD-04 执行，本模块传入） |
| `loop.hitl` | bool | false | 开启后每代产出挂起等人工 `evolve approve\|rollback` |
| `loop.reflect_enabled` | bool | true | 局末反思总开关（消融「去反思」置 false） |
| `loop.reflect_timeout_s` / `loop.reflect_context_window` | float / int | 60.0 / 5 | 单次反思超时（超时即降级跳过）/ 传入模板的最近局摘要条数 |
| `loop.train_concurrency` | int | 1 | 训练局并发度，**固定 1**（共享记忆写入），>1 拒绝启动 |
| `loop.eval_concurrency` | int | 2 | 评估局的局级并发数（共享推理队列） |
| `loop.max_candidates_per_generation` | int | 3 | 每代候选数上限，硬上限 3（Spec §8.6） |
| `loop.eval_seeds_per_candidate` | int | 40 | 单候选评估局数；`× candidates` 不得超下一条 |
| `loop.max_episodes_per_generation` | int | **120** | 每代总局数上限，与 `E_gen` 比较（FR-06-13，全系统唯一成本上限）；2026-10-06 D-1：170 → 120，与 `C=1/S=2/H=40` 一起定（`E_gen=114`） |
| `loop.n_candidates` / `loop.smoke_n` / `loop.heldout_n` | int | **1 / 2 / 40** | 参与 `E_gen` 计算；`n_candidates` 与 MOD-07 `evolve.n_candidates`、`heldout_n` 与 MOD-08 `n_heldout` 由 `load_config()` 校验一致（`config.py:558/562`）；**`smoke_n` 与 `evolve.smoke_n` 无自动校验，改 S 必须两处同步**（2026-10-06 D-1） |
| `loop.io_retry` / `loop.io_backoff_ms` | int / int | 3 / 200 | 磁盘写失败重试次数与指数退避基数（200/400/800 ms） |
| `loop.llm_timeout_s` | float | 90.0 | 单次决策生成超时（透传 MOD-03） |
| `loop.fallback_on_llm_failure` | bool | true | 模型连续失败降级 heuristic，该局标 `not_for_eval` |
| `loop.state_fsync` | bool | true | `state.json` 替换后 fsync 目录（更慢但抗断电） |
| `loop.retry_reflections_on_resume` | bool | false | `--resume` 时是否重试此前 `skipped` 的反思 |
| `loop.max_run_bytes` | int | 2 GB | 单 run 轨迹配额，超限告警（不中断） |

配置优先级：CLI 参数 > 环境变量 `GAMERSI_LOOP_*` > `configs/default.yaml`（Spec §5）。

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀）

| 触发条件 | 错误码 | MOD-06 行为 | 计入评估样本 |
|---|---|---|---|
| 模型推理超时 | `E_LLM_TIMEOUT` | 依 MOD-03 重试 ≤R=2；仍失败且 `fallback_on_llm_failure` → 本局余下步骤用 heuristic，事件标 `degraded=true` | **否**（标 `not_for_eval`，对齐 Spec §8.5） |
| 模型后端不可用/加载失败 | `E_LLM_UNAVAILABLE` / `E_LLM_LOAD_FAILED` | 先按 MOD-03 契约换 `fallback_backend` 重试 1 次；仍失败 → 中止 run，`phase="failed"`，退出码 `3`，提示改用 `--agent heuristic` 冒烟 | 否 |
| 模型输出 JSON 非法/不符 schema | `E_LLM_SCHEMA_INVALID` | 由 MOD-04 计为一次失败尝试并重试；尝试耗尽走 fallback；连续 3 个 fallback 决策由 MOD-02 判 `policy_failure` | 是（模型真实表现，计为 lose） |
| 环境内核错误 | `E_ENV_INTERNAL` / `E_ENV_RNG`（MOD-01） | 该局立即终止，标 `degraded=true, exclude_reason="E_ENV_INTERNAL"`，写 `audit/env_error_<seed>.json`（含 `state_hash` 与入参）；**不重试**（不掩盖 bug），继续下一 seed；同类异常累计 ≥3 → 中止 run（退出码 `4`） | 否 |
| 环境 `reset` 失败/配置非法 | `E_ENV_CONFIG_INVALID` | 跳过该 seed，向 `queue.jsonl` 写 `skip` 事件，run 继续 | 否 |
| 磁盘写失败（ENOSPC/EACCES） | `E_IO_DISK_FULL` | 指数退避重试 `loop.io_retry` 次；仍失败 → 中止 run（`phase="failed"`，退出码 `5`）。**不允许静默丢步** | — |
| `state.json` 替换失败 | `E_IO_RUN_STATE` | 同 `E_IO_WRITE` 重试；仍失败立即 `os._exit(5)`（状态不可信，禁止继续） | — |
| 反思生成失败/超时 | `E_LLM_TIMEOUT` / `E_LLM_SCHEMA_INVALID` | 降级跳过：写 `status="skipped"`，`reflections_skipped += 1`，**run 继续**（FR-06-4） | 不影响该局样本 |
| 记忆写入失败 | `E_IO_MEMORY_WRITE_FAILED` | 重试 ≤2；仍失败跳过写入并打 WARN，不阻塞 | 不影响 |
| MOD-07 抛 `E_EVOLVE_*` | `E_EVOLVE_*` | 保守处理：不更新 harness（Spec §8.5「裁判失败 → 保守拒绝」），`generation += 1` 并记 `decision="error_reject"`，回 `playing`（退出码 0） | — |
| 评估局失败 | `E_EVAL_EPISODE` | 由 MOD-08 记为 dropped，**按 lose 计入 n**（Spec §8.5）；dropped 率 >10% 则 `low_confidence` | — |
| 种子 split 标签越权 | `E_EVAL_SPLIT_VIOLATION` | 立即中止（INV-5 违反，P0） | — |
| 配置非法（如 `train_concurrency>1`） | `E_IO_CONCURRENCY` | 启动即拒绝，退出码 `2` | — |
| 收到 `SIGINT`/`SIGTERM` | `E_IO_INTERRUPTED`（事件级） | 完成当前 step 落盘 → 写 `state.json` → 退出码 `130`；`--resume` 可无损续跑 | — |

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例）

单测（`tests/unit/test_loop_*.py`，纯假依赖，不调模型、不写真实 `runs/`）：

- **T-06-1 单局时序**：给定假 `PolicyProto`（每个决策 1 步）与假 `SandboxEnv`（3 步后 `terminated=True`）；当 `run_episode(seed=1, split="train", arm="train")`；则 JSONL 恰好 3 行步骤 + 1 行 `episode_end`，`outcome` 取自 `info["outcome"]`，每行 `obs_digest` 以 `sha256:` 开头，文件路径为 `episodes/train/e-000001-g000-train.jsonl`。
- **T-06-2 截断与计负**：给定假 env 在第 5 步返回 `truncated=True, truncation_reason="step_limit"`（另一组为 `"policy_failure"`）；当 跑一局；则 `outcome=="lose"`、`truncation_reason` 原样记录、truncated 计数 +1，且本模块自身不做任何步数计数。
- **T-06-3 计划内多步**：给定一个决策返回 4 个 `StepRecord`（第 2 个为 reject 行）；当 落盘；则 4 行都写出，只有第 1 个执行行带 LLM 字段，`cum_reward` 逐行累加。
- **T-06-4 反思成功与用量回写**：给定 `reflect()` 返回合法 `ReflectionEntry`；当 训练局局末；则 `reflections/<episode_key>.json` 为 `status=="ok"`，`memory.add_reflection` 与 `memory.record_usage` 各调用 1 次，`tick` +1；评估局两者都不调用。
- **T-06-5 反思失败降级**：给定 `reflect()` 抛 `TimeoutError`；当 局末；则 文件为 `{"status":"skipped","reason":"E_LLM_TIMEOUT"}`、`reflections_skipped+1`，且下一局照常开跑（断言 `run()` 正常返回）。
- **T-06-6 周期触发与幂等**：给定 `k=3` 与记录调用的假 `evolve_fn`；当 跑 6 局；则 `evolve_fn` 恰被调 2 次、`generation==2`、`last_completed_cycle==1`；当 再对同一 state 调 `maybe_run_generation()`；则调用次数不增。
- **T-06-7 断点续跑不重复计分**：给定 `k=3` 跑到第 4 局后中断（状态已落盘）；当 `--resume`；则 `completed_train_seeds` 无重复、`scoreboard.episodes` 的 `episode_key` 全局唯一、总局数等于期望值。
- **T-06-8 中断粒度**：给定第 7 局第 3 步后抛 `KeyboardInterrupt`；当 恢复；则前 2 步已在 JSONL、该 seed 从 `pending_queue` 头部重跑且不产生重复 `episode_key`。
- **T-06-9 HITL 挂起/恢复**：给定 `hitl=true`、假 `evolve_fn` 返回 `pending_human=True`；当 `run()`；则 `phase=="hitl_wait"`、`hitl_pending.cycle` 正确、退出码 0；当 写好新 harness 后 `--resume`；则 `hitl_pending is None` 且 `harness_version` 为新值。
- **T-06-10 成本闸门**：给定 `K=30, H=40, S=5, C=3`（E_gen=210）与 `max_episodes_per_generation=170`；当 到达周期边界；则不调用 MOD-07，发 `loop.budget_exhausted{e_gen:210,cap:170}`，退出码 1；把 C 改为 2（E_gen=165）后正常触发。**另加 D-1 默认档用例**：给定默认 `K=30, H=40, S=2, C=1`（E_gen=114）与 `max_episodes_per_generation=120`；当 到达周期边界；则**不**触发预算闸门（114 ≤ 120），正常调用 MOD-07；把 `H` 改为 50（E_gen=134）后则发 `loop.budget_exhausted{e_gen:134,cap:120}`（这条用例同时锁住「`H` 每 +10 在 C=1 下是 +20 局」的算式）。
- **T-06-11 训练并发守卫**：给定 `train_concurrency=2`；当 启动；则 立即以 `E_IO_CONCURRENCY` 拒绝（退出码 2）。
- **T-06-12 split 越权**：给定 seed 标 `heldout` 却被当训练局传入；当 run；则 抛 `E_EVAL_SPLIT_VIOLATION`（INV-5 回归）。
- **T-06-13 Schema round-trip（G5）**：`RunState → JSON → RunState` 完全相等，且 `state.json` 通过 §6 JSON Schema 校验。

集成测试（`tests/integration/test_loop_closure.py`）：

- **T-06-14 闭环冒烟（G6）**：给定 `loop smoke --seeds train:1000-1002 --k 3 --no-reflect`；当 执行；则 < 2 s 完成、退出码 0、`state.json` 为 `phase=="done"`、产出 3 个 `episodes/*.jsonl`。
- **T-06-15 固定 seed 确定性（G2/G3）**：给定 `--seeds train:1000-1004 --agent heuristic --eval-workers 1`；当 跑两次（不同 `run_id`）；则两次 `obs_digest+action+reward` 序列逐字节相同。
- **T-06-16 真实演化一轮（M2）**：给定 本地小模型 + `--k 3 --generations 1`；当 执行；则 `generations/gen-000/`（基线，`decision="baseline"`）与 `generations/gen-001/`（第 1 代裁决）都有 `report.json`，`state.generation==1`，`events.jsonl` 含 `loop.cycle_start` 与 `evolve.decision` 事件。
- **T-06-17 断电模拟**：给定 在 `state.json` 原子替换前后注入 `kill -9`；当 `--resume`；则 状态可解析、无重复计分、无已完成 seed 丢失。

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

- **G1 单测**：`gamer_rsi/loop/**` 行覆盖率 ≥ 75%（编排层含 IO，阈值低于纯函数模块的 80%）；T-06-1…T-06-13 全绿。
- **G2 Golden**：本模块不产出 golden 轨迹（归 MOD-01/02），但必须消费其 golden seed 集跑通且不修改既有 golden 文件。
- **G3 确定性**：T-06-15 通过；`loop/` 经 MOD-09 `guards.py` AST 扫描 0 处 `random`/`time` 依赖。
- **G4 泄漏审计**：`python -m gamer_rsi audit leak --run-id <run_id>` 对本模块产出的 run 0 告警；`state.json` 与 `reflections/*.json` 中不得出现 held-out/test seed 值（按 MOD-08 seed 注册表白名单正则扫描）。
- **G5 Schema**：§6 的 JSON Schema 落 `gamer_rsi/loop/schemas/`，round-trip 测试通过。
- **G6 可复现命令**：§3 四条命令在干净 checkout 上可直接复制执行成功（含 `--no-reflect` 免模型路径）。
- **M2 验收（计划 §7 / Spec §7）**：闭环自动运行 ≥1 轮演化并产出 diff 与决策记录；`--resume` 与 HITL 挂起/恢复各有一条端到端演示记录（README 附命令与日志片段）。
- **计分正确性**：任意中断/恢复组合下，`scoreboard.json` 的 `episode_key` 集合等于「实际完整跑完的 episode 集合」，无重复、无幻影。

## 13. 指标与埋点

`runs/<run_id>/events.jsonl` 每行一个事件（`ts` + `run_id` + `event` + 载荷，由 MOD-09 `emit` 落地，事件名带 `loop.` 前缀）：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `loop.episode_start` | `episode_key, seed, split, arm, generation, harness_version` | 局级吞吐、代际归属 |
| `loop.episode_end` | `steps, decisions, outcome, truncation_reason, cum_reward, stages_cleared, hearts_left, battles_won, illegal_attempts, fallback_decisions, wall_ms` | 主/次级指标原料（MOD-08 聚合） |
| （非法尝试） | 由 MOD-04 发 `policy.reject`，本模块不重复发 | `illegal_rate`、格式鲁棒性 |
| `loop.reflect` | `episode_key, status(ok\|skipped), reason, latency_ms, template_sha256` | 反思成功率、模板版本追溯 |
| `loop.cycle_start` | `cycle, generation, e_gen, est_hours` | 周期开销与预算追踪 |
| `evolve.decision` | `generation, decision, decided_by`（**由 MOD-07 发出**） | 代际曲线 |
| `loop.budget_exhausted` | `e_gen, cap` | 成本失控告警 |
| `loop.interrupted` / `loop.resume` | `phase, completed, pending, tick` | 续跑可靠性 |

派生计数（本模块只计数，不做统计推断）：`episodes_per_hour`、`mean_wall_ms_per_episode`、`truncated_rate`、`reflection_skip_rate`、`resume_count`、`eval_budget_utilization`。

## 14. 依赖与风险

依赖（Spec §6，签名见 §4.7）：MOD-01（经 MOD-02）、MOD-02（`SandboxEnv`，INV-1 唯一入口，唯一判负方）、MOD-03（反思用 `generate_json`）、MOD-04（`reset`/`play_decision`）、MOD-05（`add_reflection`/`record_usage`/`frozen`）、MOD-08（`evaluate_arm`、seed 出口 `seeds.take`）、MOD-09（配置/事件/`run_id`/CLI/git）。下游：MOD-07（周期边界被调用）。

| 风险 | 影响 | 对策 |
|---|---|---|
| 状态先写、轨迹未写完导致不一致 | 重复计分或漏局 | 先写轨迹并 flush、后写 `state.json`；幂等键去重；T-06-7/8/17 覆盖 |
| 评估并发抢占推理资源 | 延迟抖动、OOM | 只做局级并发 + 共享推理队列（P-10），不在多进程里复制权重 |
| 训练局并发导致记忆库竞态 | 记忆污染、不可复现 | `train_concurrency` 固定 1 + `locks/memory.lock` 文件锁，拒绝 >1 配置 |
| HITL 挂起期间周期被重复触发 | 重复演化、浪费算力 | `evolving_cycle` + `last_completed_cycle` + MOD-07 产物存在性三重校验（FR-06-6） |
| 反思把 held-out 信息带入记忆（INV-5） | 泄漏、增益虚假 | 仅训练局写记忆；seed 只从 MOD-08 拿；G4 正则审计 |
| 长 run 轨迹目录膨胀 | 磁盘打满 → `E_IO_DISK_FULL` | `loop.max_run_bytes` 配额告警；轨迹保留 ≥ 全部代际（Spec §8.4） |
| MOD-07 接口在 M2 前未就绪 | 无法联调 | `evolve_fn` 依赖注入 + `--no-evolve` 开关，用 stub 跑通 M2 前半段 |

## 15. 里程碑与工作量估算（人日）

| 工作项 | 人日 | 交付 |
|---|---|---|
| `run_episode` 步循环 + JSONL 落盘 + 终止判定 | 1.5 | FR-06-1/2，T-06-1..3 |
| 反思编排 + 降级 + MOD-05 写入 | 1.0 | FR-06-3/4，T-06-4/5 |
| run 状态机 + `--resume` + 计分幂等 | 2.0 | FR-06-8/9/10，T-06-7/8/13/17 |
| 周期调度 + 幂等 + generation/harness_version | 1.5 | FR-06-5/6/7，T-06-6 |
| 评估执行器接口 + 局级并发 + `E_gen` 成本闸门 + gen-000 基线 | 2.0 | FR-06-12/13/13a/14，T-06-10/11 |
| HITL 挂起/恢复（跨进程） | 1.0 | FR-06-11，T-06-9 |
| 失败恢复矩阵 + 信号处理 + `E_*` 映射 | 1.5 | §10，T-06-8/11 |
| schema + 埋点 + CLI 接线 + 文档命令 | 1.5 | §6/§13，G5/G6 |
| 集成测试与闭环联调（含 MOD-07 stub） | 2.0 | T-06-14..16，M2 |
| **合计** | **14.0** | M2 交付（MOD-07 完整能力在 M2–M3） |

关键路径：本模块是 MOD-07 的硬阻塞项（Spec §6「MOD-06 → MOD-07」）。建议先交付「状态机 + 周期调度 + `evolve_fn` 注入」三件套（5 人日）供 MOD-07 尽早联调。

## 16. 开放问题

1. **K 的取值**：计划 §5 给 K=30，但 M2 冒烟只有 K=3 才现实。是否引入 `k_schedule`（前 3 代为 5、之后 30）？需 MOD-08 给出统计功效依据后再定。
2. **计分幂等键是否应含 `config_hash`**：现为 `e-<seed>-v<harness_version>`。若同一 `run_id` 下改了 `episode_config`（如难度），是否需扩展键？倾向扩展，但会让历史数据对比复杂化。
3. **`truncated` 样本处置**：当前计为样本但 `outcome=None`。是否应像降级局一样标 `not_for_eval`？待 MOD-08 定口径后回填 Spec §4.1。
4. **单模型下的评估并行**：24GB 未必能容两份 8B 权重 + KV cache。是否需 MOD-03 提供「单进程批量推理服务器」作为第二方案（本 PRD 仅保留 `workers=1` 顺序退化）。
5. **HITL 挂起期间 harness 被外部改动**：`--resume` 只读 `version.json`，无法判断改动是否对应本次挂起的候选。是否要求 MOD-07 在 `version.json` 写 `pending_cycle` 供校验？
6. **`state.json` 是否保留历史快照**：现只保留最新态 + `queue.jsonl` 事件流。若需重建任意时刻状态，可能要请 MOD-09 增加状态快照归档（与「轨迹保留 ≥ 全部代际」对齐）。
7. **skipped 反思的补偿**：run 结束时是否统一补跑一次（批量、离线）？补跑会改变记忆内容进而影响后续局，破坏确定性，倾向不做，需确认。

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版 |
| v1.1 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：单局循环改为调用 `play_decision`，终止只看 MOD-02 的 terminated/truncated，删除 `max_steps=120` 与自有的 illegal_streak（P0-2/5）；对 MOD-04/05 的调用签名改为 Spec §4.7（P1-1/2）；轨迹路径与 `episode_key` 改为 `e-{seed}-g{gen}-{arm}`（P1-10）；成本闸门按 `E_gen` 公式统一计算，并作为全系统唯一上限（P0-3、P1-8）；评估改为局级并发 + 共享推理队列，委托 MOD-08（P1-9）；新增 gen-000 基线与逻辑时钟 tick；错误码、退出码、CLI、事件名对齐 MOD-09 注册表（P1-11～14） |
| v1.2 | 2026-09-30 17:49 | seed 取用规则回填为 Spec B-36；HITL 挂起语义以 Spec §4.5 B-32 为准 |
