# MOD-06 loop 技术 PRD

> 版本：v1.0 · 状态：草案（待评审）· 所属里程碑：M2 · 上游 Spec：[模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md)（唯一事实来源，冲突时以 Spec 为准）· 作者：MOD-06 模块作者
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

单局回合循环（`observe → policy.decide → sandbox.step → log → 终止判定`，含 `max_steps` 上限与 `truncated` 语义）；局末反思编排（MOD-03/04 + `harness/prompts/reflection.md`，结构化后写 MOD-05，失败降级）；演化周期调度（每 K 局触发 MOD-07、`generation` 计数器、读取 `harness/version.json`、周期边界幂等）；run 级状态机、产物目录布局、`--resume` 断点续跑、计分幂等去重；失败恢复矩阵与 `E_*` 错误码映射（前缀取自 Spec §8.3）；HITL 挂起/恢复的跨进程状态设计（配合 `evolve approve|rollback`）；成本控制（单局步数上限、每代候选数、每代评估局预算、预算耗尽行为）；并发（评估局进程池并行、训练局串行 + 共享记忆写入保护）；结构化事件与指标埋点。

### 2.2 范围外（非目标）

逐字复用 Spec §3 中 MOD-06 的「不负责」清单：

> - **不负责**：判定候选改进是否被接受（MOD-07）、计算 held-out 指标（MOD-08）、harness 文件的具体 diff（MOD-07）。

补充边界澄清（非新增职责，只防漂移）：不实现游戏规则（MOD-01）、不做观测投影（MOD-02）、不加载模型（MOD-03）、不组装 prompt 正文（MOD-04）、不实现记忆存储（MOD-05）；不定义 seed 切分协议（MOD-08）与 seed 注册表（MOD-09），只**读取**已切分好的 seed 清单并消费；不自建日志格式与 `run_id` 生成（MOD-09）；不写 harness 文件、不做 git commit（MOD-07/09）。

## 3. 使用方与场景

| 使用方 | 场景 | 用到的接口 |
|---|---|---|
| 人类研究者（CLI） | 冒烟一局、跑 N 代、看进度、Ctrl-C 后续跑、处理 HITL | `loop run/resume/status`、`evolve approve\|rollback` |
| MOD-07 evolve | 请求「按候选在 held-out 上跑评估局」并拿回每局原始结果 | `run_evaluation_batch()` → `EvalBatchResult` |
| MOD-08 evals | 从轨迹与计分台账算指标 | 文件契约（§7），不调函数 |
| 回归测试 | 固定 seed 跑 3 局做 golden/冒烟 | `run_episode()` |
| 未来 MOD-10 distill | 复用 run 级状态与轨迹目录 | 文件契约 |

可复现命令（G6，必须可直接复制执行）：

```bash
python -m gamer_rsi loop run --agent llm --seeds train:1000-1029 --k 30 --hitl
python -m gamer_rsi loop status --run <run_id>
python -m gamer_rsi loop run --run <run_id> --resume
python -m gamer_rsi loop smoke --seeds train:1000-1002 --k 3 --no-reflect   # 只验编排，不调模型
```

## 4. 功能需求（FR-06-x，编号，每条可测试）

- **FR-06-1（单局时序）**：`run_episode()` 严格按 `reset(seed) → 循环{observe → policy.decide → sandbox.step → 落盘一行 → 终止判定}`；终止条件为 `step_result.terminated or step_result.truncated or step >= loop.max_steps_per_episode or illegal_streak >= loop.max_illegal_streak`。`truncated=True` 的局记 `terminated_by="truncated"`、`outcome=None`，不计为失败但计入样本（是否纳入指标由 MOD-08 定）。
- **FR-06-2（每步必落盘）**：每 step 结束立即向 `episodes/<seed>.jsonl` 追加一行并 `flush`，字段同 Spec §4.5；崩溃最多丢当前步。
- **FR-06-3（局末反思）**：episode 结束（terminated/truncated/max_steps 均算）后，若 `reflect_enabled`，用 `harness/prompts/reflection.md` 渲染上下文 → MOD-03 `generate_json()` → 通过 `ReflectionRecord` schema → 写入 MOD-05。
- **FR-06-4（反思降级）**：反思任一步失败（超时/JSON 非法/schema 不符/MOD-05 写失败）→ 事件 `reflect` 记 `status="skipped"`，`reflections/<seed>.json` 写 `{"skipped": true, "reason": "<E_*>"}`，主循环继续。**反思失败绝不判该局失败、绝不阻塞 run**。
- **FR-06-5（周期触发）**：每完成 K 局训练局触发一次演化周期，`K = loop.k`（默认 30）；条件为 `len(completed_train_seeds) % K == 0` 且 `generation < loop.max_generations` 且 `phase == "playing"`。
- **FR-06-6（周期幂等）**：同一 `cycle_index` 至多触发一次：先原子落盘 `phase="evolving"` + `evolving_cycle=cycle_index`，再调 MOD-07；返回后写 `last_completed_cycle=cycle_index` 并回 `phase="playing"`。若进程在 `evolving` 阶段被杀，`--resume` **不重跑** MOD-07，而是检查其产物（`generations/gen-XXX/report.json` / `decision.json`）存在性：存在则标该 cycle 完成，否则重置 `phase="playing"` 后重新触发。
- **FR-06-7（generation 与 harness_version）**：`state.generation` 单调不减，仅在 MOD-07 返回「本代已裁决」（接受或拒绝均算）后 `+1`；`harness_version` 每局开始时从 `harness/version.json` 重读（不跨代缓存），故 HITL 挂起期间人工切换 harness 立即生效。
- **FR-06-8（run 状态机）**：`state.json` 至少含 `run_id / phase / generation / harness_version / k / completed_train_seeds / pending_queue / hitl_pending / counters / budget / updated_at`（§7.1）；`phase ∈ {pending, playing, reflecting, evolving, hitl_wait, done, failed}`。
- **FR-06-9（断点续跑）**：`loop run --run <id> --resume` 读 `state.json`，跳过 `completed_train_seeds`，从 `pending_queue` 头部继续，不重复计分（§7.3 幂等键）。
- **FR-06-10（计分幂等）**：计分只发生在 episode 正常收尾；写入前检查 `metrics/scoreboard.json` 是否已有该 `episode_key`，追加后 fsync。同 seed 因 `--force-rerun` 重跑时，旧记录标 `superseded_by` 而非删除。
- **FR-06-11（HITL 挂起）**：`loop.hitl=true` 时 MOD-07 产出候选与决策记录后不直接生效；MOD-06 写 `state.hitl_pending={"cycle","generation","candidate_dir","report"}`、置 `phase="hitl_wait"`、退出码 `0`（正常挂起非错误）。人工执行 `evolve approve|rollback --generation g`（MOD-07 命令，MOD-06 只消费结果状态）后，`loop run --run <id> --resume` 读 `harness/version.json` 与 `hitl_pending`，清空标记并继续。
- **FR-06-12（评估批量编排）**：`run_evaluation_batch()` 供 MOD-07 在指定候选 harness 上跑 held-out 评估局；只回传每局原始结果与执行元数据（耗时、失败 seed、预算消耗），**不做任何聚合指标**（聚合归 MOD-08）。
- **FR-06-13（成本闸门）**：`max_steps_per_episode`（默认 120）、`max_candidates_per_generation`（默认 3，硬上限 3，对齐 Spec §8.6）、`max_eval_episodes_per_generation`（默认 120）。预算耗尽时停止本轮剩余工作，落盘 `budget_exhausted` 事件；评估超预算回 `playing`，代数达上限则 `done`，退出码 `0`。
- **FR-06-14（并发纪律）**：训练局串行（`train_concurrency` 固定 1，写共享记忆须持 `runs/<run_id>/locks/memory.lock` 文件锁）；评估局可并行（`eval_workers`，默认 2），评估 worker 使用冻结 harness 快照与只读记忆快照，禁止写全局记忆库。
- **FR-06-15（信号处理）**：捕获 `SIGINT`/`SIGTERM`：完成当前 step 落盘 → 写 `state.json`（附 `interrupted_at`）→ flush → 退出码 `130`；第二次信号立即 `os._exit(130)`。

## 5. 非功能需求（NFR-06-x：确定性/性能/可复现/可观测）

- **NFR-06-1（确定性）**：同一 `run_id` + 同 seed 清单 + 同 `harness_version` + `eval_workers=1` 时，`obs_digest`/`action`/`reward` 序列逐字节一致；所有随机来自 `gamer_rsi.infra.rng_for(seed, namespace)`，禁用 `random`/`time` 相关随机（MOD-09 `guards.py` CI 扫描）。
- **NFR-06-2（可复现）**：`state.json` 能独立重建「下一步做什么」，不依赖内存态；`runs/<run_id>/config.json` 固化合并后配置（含 `config_hash`）。
- **NFR-06-3（性能）**：编排层单步开销（不含推理与环境 step）≤ 5 ms（M5 本机）；每步 ≤ 1 次 append+flush；代际目录用增量读，不整目录 `glob`。
- **NFR-06-4（可观测）**：跨模块事件统一走 MOD-09 结构化日志，事件名固定为 `episode_start / step / illegal / reflect / evolve / decision / budget_exhausted / interrupted / resume`，任何模块不得自建格式。
- **NFR-06-5（崩溃安全）**：`state.json` 与 `scoreboard.json` 一律「临时文件 + `os.replace`」原子替换；`episodes/*.jsonl` 追加+flush；任意时刻 `SIGKILL` 最多丢当前步，状态文件不自相矛盾。
- **NFR-06-6（可测性）**：`EpisodeRunner` 依赖可注入（`env/policy/memory/clock/log_writer`），全部可用 `tests/fakes.py` 假实现替换；`loop smoke --no-reflect` 不触网不调模型，< 2 s 跑完 3 局。
- **NFR-06-7（内存）**：常驻内存不随局数增长：轨迹流式写盘不累积 `list[StepResult]`，反思上下文只保留最近 `reflect_context_window` 局摘要。

## 6. 接口契约（真实 Python 签名 + JSON Schema）

```python
# gamer_rsi/loop/runner.py
class PolicyProto(Protocol):
    def decide(self, obs: Observation, memory: list[dict]) -> "Decision": ...
    def reflect(self, trajectory: "TrajectoryView") -> "ReflectionRecord": ...
class MemoryProto(Protocol):
    def write_reflection(self, rec: "ReflectionRecord") -> str: ...   # 返回 reflection_id
    def retrieve(self, obs: Observation) -> list[dict]: ...
    def snapshot(self) -> str: ...                    # 评估进程用只读记忆快照路径

@dataclass(frozen=True)
class RecordedEpisode:
    episode_id: str; seed: int; split: Literal["train","heldout","test"]
    generation: int; harness_version: int; steps: int; cum_reward: float
    floors: int; kills: int; illegal_count: int; jsonl_path: str
    outcome: Literal["win","lose","truncated","max_steps","env_error"]
    terminated_by: Literal["terminated","truncated","max_steps","illegal_streak","env_error"]
    reflection_id: str | None; wall_ms: int

class EpisodeRunner:
    def __init__(self, *, env: SandboxEnv, policy: PolicyProto, memory: MemoryProto,
                 logger: "JsonlLogger", config: "LoopConfig", run_ctx: "RunContext") -> None: ...
    def run_episode(self, seed: int, *, split: str = "train",
                    episode_config: EpisodeConfig, harness_version: int) -> RecordedEpisode: ...

# gamer_rsi/loop/reflect.py —— 返回 None = 降级跳过，由调用方写 skipped 标注（FR-06-4）
def make_reflection(*, trajectory: "TrajectoryView", tail: "StepResult", policy: PolicyProto,
                    memory: MemoryProto, template_path: str = "harness/prompts/reflection.md",
                    timeout_s: float = 60.0) -> "ReflectionRecord | None": ...

# gamer_rsi/loop/generation.py
@dataclass(frozen=True)
class EvalBatchResult:
    candidate_id: str; harness_version: int; seeds: list[int]; episodes: list[RecordedEpisode]
    failed_seeds: list[int]; budget_consumed: int; wall_ms: int
def run_evaluation_batch(*, candidate_id: str, harness_dir: str, seeds: Sequence[int],
                         run_ctx: "RunContext", workers: int = 2,
                         episode_config: EpisodeConfig) -> EvalBatchResult: ...
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

// runs/<run_id>/reflections/<seed>.json
{"reflection_id":"r-000123","episode_id":"e-000123","seed":1042,"generation":3,
 "harness_version":7,"outcome":"lose","created_at":"2026-09-30T12:00:00Z",
 "status":"ok|skipped","reason":null,
 "reflection":{"what_worked":"...","what_failed":"...","next_time":"...",
               "tags":["dice_economy","enemy_intent"],"confidence":0.6},
 "llm":{"model":"qwen3-8b","latency_ms":1900,"prompt_tokens":1200,"completion_tokens":180},
 "template_sha256":"sha256:..."}

// runs/<run_id>/metrics/scoreboard.json —— 每 episode 一条，计分幂等依据
{"episodes":[{"episode_key":"e-000123-v7","seed":1042,"split":"train","generation":3,
  "harness_version":7,"outcome":"lose","steps":41,"cum_reward":-0.12,"floors":2,"kills":1,
  "illegal_count":2,"scored_at":"2026-09-30T12:00:00Z","superseded_by":null}]}
```

## 7. 数据模型与存储

### 7.1 run 产物目录（扩展 Spec §5）

```
runs/<run_id>/
  config.json             # 合并后配置 + config_hash（MOD-09 写，本模块只读）
  state.json              # run 级状态机（本模块权威，§6 schema）
  queue.jsonl             # 待做队列追加式事件日志（add/done/skip），供恢复与审计
  episodes/<seed>.jsonl   # 每步一行（Spec §4.5 字段，本模块写）
  reflections/<seed>.json # 每局一份反思或 skipped 标注
  generations/gen-<NNN>/  # MOD-07 写的候选与报告；本模块只读、只检查存在性
  metrics/scoreboard.json # 计分台账（幂等键 episode_key）
  metrics/loop_trace.jsonl# 编排事件流（§13）
  locks/memory.lock       # 训练局共享记忆写入文件锁
  audit/                  # MOD-02/09 审计产物
```

### 7.2 状态机转移

`pending → playing → (reflecting → playing)* → evolving → (hitl_wait → playing | playing) → … → done`；异常边：任意 phase → `failed`（不可恢复，如 `E_IO_RUN_STATE`）；`playing → interrupted`（临时态，恢复回 `playing`）。

### 7.3 幂等键

- **计分幂等**：`episode_key = f"e-{seed:06d}-v{harness_version}"`。**决策（本 PRD 提出，待回填 Spec）**：键含 `harness_version`，使同一 seed 跨代计分为两条不同记录（代际对比需要），而**同代内**重复执行同一 seed 只保留一条。
- **周期幂等**：`cycle_index = len(completed_train_seeds) // k`，以 `last_completed_cycle` + `evolving_cycle` + MOD-07 产物存在性三重校验。
- **反思幂等**：`reflections/<seed>.json` 已 `status=="ok"` 则跳过；`skipped` 需 `--retry-reflections` 显式重试。
- **决策（本 PRD 提出，待回填 Spec）**：训练/评估 seed **必须**经 MOD-08 `split` 协议取用，MOD-06 不得自行按 `seed % 2` 之类规则切分；仅校验传入 seed 的 split 标签与 MOD-08 注册表一致，不一致立即抛 `E_EVAL_SPLIT_VIOLATION`（守 INV-5）。

## 8. 关键流程（伪代码或时序）

```python
# EpisodeRunner.run_episode —— 每步时序（FR-06-1/2）
def run_episode(self, seed, *, split="train", episode_config, harness_version):
    ep_id = self.run_ctx.next_episode_id()          # e-%06d，由 state 计数器派生
    obs = self.env.reset(seed, episode_config=episode_config)
    self.logger.event("episode_start", episode_id=ep_id, seed=seed, split=split,
                      generation=self.run_ctx.state.generation, harness_version=harness_version)
    cum, illegal_streak, step = 0.0, 0, 0
    with self.logger.episode_writer(seed) as w:     # episodes/<seed>.jsonl，append + flush
        while True:
            mem = self.memory.retrieve(obs)                 # MOD-05 只读
            decision = self.policy.decide(obs, mem)         # MOD-04；内部含 R=2 次重试
            res = self.env.step(decision.action)            # MOD-02 唯一入口（INV-1）
            step += 1; cum += res.reward
            illegal_streak = illegal_streak + 1 if res.info["illegal"] else 0
            w.write_step(step=step, obs_digest=digest(obs), action=decision.action,
                         reward=res.reward, cum_reward=cum, illegal=res.info["illegal"],
                         illegal_reason=res.info["illegal_reason"], llm_raw=decision.raw,
                         llm_latency_ms=decision.latency_ms,
                         prompt_tokens=decision.prompt_tokens,
                         completion_tokens=decision.completion_tokens)   # 立即 flush（FR-06-2）
            obs = res.observation
            if res.terminated:  term, outcome = "terminated", res.info["outcome"]; break
            if res.truncated:   term, outcome = "truncated", "truncated"; break
            if step >= self.config.max_steps_per_episode:
                                term, outcome = "max_steps", "max_steps"; break
            if illegal_streak >= self.config.max_illegal_streak:
                                term, outcome = "illegal_streak", "lose"; break
    rec = RecordedEpisode(..., terminated_by=term, jsonl_path=w.path)
    self.logger.event("episode_end", **asdict(rec))          # 结构化事件（§13）
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
        rec = self.runner.run_episode(seed, split="train", harness_version=st.harness_version,
                                      episode_config=self.run_ctx.episode_config)
        score_once(st, rec)                       # 幂等：episode_key 已存在则跳过（FR-06-10）
        st.completed_train_seeds.append(seed); st.pending_queue.pop(0)
        st.counters.episodes_total += 1
        st.counters.episodes_truncated += (rec.terminated_by == "truncated")
        st.phase = "reflecting"; save_state(st)
        r = make_reflection(trajectory=file_view(rec.jsonl_path), tail=last_step(rec),
                            policy=self.runner.policy, memory=self.runner.memory)  # FR-06-3
        write_reflection_or_skip(rec, r)          # None → {"skipped":true,...}（FR-06-4）
        st.phase = "playing"; save_state(st)
        cycle = len(st.completed_train_seeds) // st.k
        prev = -1 if st.last_completed_cycle is None else st.last_completed_cycle
        if cycle > prev and st.generation < self.config.max_generations:
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

**并发时序（FR-06-14）**：`run_evaluation_batch` 用 `ProcessPoolExecutor(max_workers=workers)`，每个子进程独立 `SandboxEnv` + 独立模型后端 + `memory.snapshot()` 只读目录；seed 按 `index % workers` 静态分片，结果以 `{seed: episode_jsonl_path}` 回传主进程。**决策（本 PRD 提出，待回填 Spec）**：评估局**不写** `scoreboard.json` 主台账，只写 `episodes/` 与 `EvalBatchResult`，避免与训练计分混淆，聚合与存档归 MOD-08。24GB 统一内存下 `eval_workers ≤ 2`（每 worker 各持一份权重）；内存不足时退化为 `workers=1` 顺序模式（同接口、行为等价、仅慢）。

## 9. 配置项（键名、类型、默认值、含义）

| 键 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `loop.k` | int | 30 | 每 K 局训练触发一次演化周期（计划 §5：K=30） |
| `loop.max_generations` | int | 3 | run 内最大演化代数（M3 验收 ≥3 轮） |
| `loop.max_steps_per_episode` | int | 120 | 单局步数上限，达到即 `truncated`/`max_steps`（FR-06-1/13） |
| `loop.max_illegal_streak` | int | 3 | 连续非法动作上限，达到判该局失败（对齐 Spec §4.3） |
| `loop.action_retry_r` | int | 2 | 非法动作同一步重试次数（MOD-04 执行，本模块传入） |
| `loop.hitl` | bool | false | 开启后每代产出挂起等人工 `evolve approve\|rollback` |
| `loop.reflect_enabled` | bool | true | 局末反思总开关（消融「去反思」置 false） |
| `loop.reflect_timeout_s` / `loop.reflect_context_window` | float / int | 60.0 / 5 | 单次反思超时（超时即降级跳过）/ 传入模板的最近局摘要条数 |
| `loop.train_concurrency` | int | 1 | 训练局并发度，**固定 1**（共享记忆写入），>1 拒绝启动 |
| `loop.eval_workers` | int | 2 | 评估局进程池并发度，内存不足时退化为 1 |
| `loop.max_candidates_per_generation` | int | 3 | 每代候选数上限，硬上限 3（Spec §8.6） |
| `loop.eval_seeds_per_candidate` | int | 40 | 单候选评估局数；`× candidates` 不得超下一条 |
| `loop.max_eval_episodes_per_generation` | int | 120 | 每代评估局总预算（如 3 候选 × 40 held-out 局） |
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
| 模型后端不可用/加载失败 | `E_LLM_BACKEND` | 立即中止 run，`phase="failed"`，退出码 `3`；提示改用 `--agent heuristic` 冒烟 | 否 |
| 模型输出 JSON 非法/不符 schema | `E_LLM_SCHEMA` | 由 MOD-04 三级校验重试；连续 3 次 → 按 `illegal_streak` 判该局失败 | 是（模型真实表现） |
| 环境 `step` 抛异常 | `E_ENV_STEP` | 该局立即终止，`outcome="env_error"`，写 `audit/env_error_<seed>.json`（含 `state_hash` 与入参）；**不重试**（不掩盖 bug），继续下一 seed；同类异常累计 ≥3 → 中止 run（退出码 `4`） | 否 |
| 环境 `reset` 失败/seed 非法 | `E_ENV_RESET` | 跳过该 seed，向 `queue.jsonl` 写 `skip` 事件，run 继续 | 否 |
| 磁盘写失败（ENOSPC/EACCES） | `E_IO_WRITE` | 指数退避重试 `loop.io_retry` 次；仍失败 → 中止 run（`phase="failed"`，退出码 `5`）。**不允许静默丢步** | — |
| `state.json` 替换失败 | `E_IO_RUN_STATE` | 同 `E_IO_WRITE` 重试；仍失败立即 `os._exit(5)`（状态不可信，禁止继续） | — |
| 反思生成失败/超时 | `E_LLM_TIMEOUT` / `E_LLM_SCHEMA` | 降级跳过：写 `status="skipped"`，`reflections_skipped += 1`，**run 继续**（FR-06-4） | 不影响该局样本 |
| 记忆写入失败 | `E_IO_MEMORY` | 重试 ≤2；仍失败跳过写入并打 WARN，不阻塞 | 不影响 |
| MOD-07 抛 `E_EVOLVE_*` | `E_EVOLVE_*` | 保守处理：不更新 harness（Spec §8.5「裁判失败 → 保守拒绝」），`generation += 1` 并记 `decision="error_reject"`，回 `playing`（退出码 0） | — |
| 评估局失败（子进程异常） | `E_EVAL_EPISODE` | 该 seed 进 `failed_seeds`（不放宽预算）；失败率 >50% 则该候选整批标 `invalid` 交 MOD-07 裁决 | — |
| 种子 split 标签越权 | `E_EVAL_SPLIT_VIOLATION` | 立即中止（INV-5 违反，P0） | — |
| 配置非法（如 `train_concurrency>1`） | `E_IO_CONCURRENCY` | 启动即拒绝，退出码 `2` | — |
| 收到 `SIGINT`/`SIGTERM` | `E_IO_INTERRUPTED`（事件级） | 完成当前 step 落盘 → 写 `state.json` → 退出码 `130`；`--resume` 可无损续跑 | — |

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例）

单测（`tests/unit/test_loop_*.py`，纯假依赖，不调模型、不写真实 `runs/`）：

- **T-06-1 单局时序**：给定 `SandboxEnv` 假实现（3 步后 `terminated=True`）；当 `run_episode(seed=1)`；则 JSONL 恰好 3 行、`terminated_by=="terminated"`、`outcome` 取自 `info["outcome"]`、每行 `obs_digest` 以 `sha256:` 开头。
- **T-06-2 步数上限**：给定环境永不 terminated、`max_steps_per_episode=5`；当 跑一局；则 `steps==5`、`terminated_by=="max_steps"`、truncated 计数 +1 且不抛异常。
- **T-06-3 环境 truncated 分支**：给定第 2 步 `truncated=True`；当 跑一局；则 `outcome=="truncated"`、`terminated_by=="truncated"`。
- **T-06-4 反思成功**：给定 `policy.reflect` 返回合法 `ReflectionRecord`；当 局末；则 `reflections/<seed>.json` 为 `status=="ok"`，`memory.write_reflection` 调用 1 次。
- **T-06-5 反思失败降级**：给定 `policy.reflect` 抛 `TimeoutError`；当 局末；则 文件为 `{"status":"skipped","reason":"E_LLM_TIMEOUT"}`、`reflections_skipped+1`，且下一局照常开跑（断言 `run()` 正常返回）。
- **T-06-6 周期触发与幂等**：给定 `k=3` 与记录调用的假 `evolve_fn`；当 跑 6 局；则 `evolve_fn` 恰被调 2 次、`generation==2`、`last_completed_cycle==1`；当 再对同一 state 调 `maybe_run_generation()`；则调用次数不增。
- **T-06-7 断点续跑不重复计分**：给定 `k=3` 跑到第 4 局后中断（状态已落盘）；当 `--resume`；则 `completed_train_seeds` 无重复、`scoreboard.episodes` 的 `episode_key` 全局唯一、总局数等于期望值。
- **T-06-8 中断粒度**：给定第 7 局第 3 步后抛 `KeyboardInterrupt`；当 恢复；则前 2 步已在 JSONL、该 seed 从 `pending_queue` 头部重跑且不产生重复 `episode_key`。
- **T-06-9 HITL 挂起/恢复**：给定 `hitl=true`、假 `evolve_fn` 返回 `pending_human=True`；当 `run()`；则 `phase=="hitl_wait"`、`hitl_pending.cycle` 正确、退出码 0；当 写好新 harness 后 `--resume`；则 `hitl_pending is None` 且 `harness_version` 为新值。
- **T-06-10 预算耗尽**：给定 `max_eval_episodes_per_generation=10`、3 候选 × 4 局；当 run；则只执行到预算上限、发 `budget_exhausted` 事件、`budget.eval_episodes_used` 与执行计数一致。
- **T-06-11 训练并发守卫**：给定 `train_concurrency=2`；当 启动；则 立即以 `E_IO_CONCURRENCY` 拒绝（退出码 2）。
- **T-06-12 split 越权**：给定 seed 标 `heldout` 却被当训练局传入；当 run；则 抛 `E_EVAL_SPLIT_VIOLATION`（INV-5 回归）。
- **T-06-13 Schema round-trip（G5）**：`RunState → JSON → RunState` 完全相等，且 `state.json` 通过 §6 JSON Schema 校验。

集成测试（`tests/integration/test_loop_closure.py`）：

- **T-06-14 闭环冒烟（G6）**：给定 `loop smoke --seeds train:1000-1002 --k 3 --no-reflect`；当 执行；则 < 2 s 完成、退出码 0、`state.json` 为 `phase=="done"`、产出 3 个 `episodes/*.jsonl`。
- **T-06-15 固定 seed 确定性（G2/G3）**：给定 `--seeds train:1000-1004 --agent heuristic --eval-workers 1`；当 跑两次（不同 `run_id`）；则两次 `obs_digest+action+reward` 序列逐字节相同。
- **T-06-16 真实演化一轮（M2）**：给定 本地小模型 + `--k 3 --generations 1`；当 执行；则 `generations/gen-000/` 有裁决产物、`state.generation==1`、`loop_trace.jsonl` 含 `evolve` 与 `decision` 事件。
- **T-06-17 断电模拟**：给定 在 `state.json` 原子替换前后注入 `kill -9`；当 `--resume`；则 状态可解析、无重复计分、无已完成 seed 丢失。

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

- **G1 单测**：`gamer_rsi/loop/**` 行覆盖率 ≥ 75%（编排层含 IO，阈值低于纯函数模块的 80%）；T-06-1…T-06-13 全绿。
- **G2 Golden**：本模块不产出 golden 轨迹（归 MOD-01/02），但必须消费其 golden seed 集跑通且不修改既有 golden 文件。
- **G3 确定性**：T-06-15 通过；`loop/` 经 MOD-09 `guards.py` AST 扫描 0 处 `random`/`time` 依赖。
- **G4 泄漏审计**：`python -m gamer_rsi audit leak --run <run_id>` 对本模块产出的 run 0 告警；`state.json` 与 `reflections/*.json` 中不得出现 held-out/test seed 值（按 MOD-08 seed 注册表白名单正则扫描）。
- **G5 Schema**：§6 的 JSON Schema 落 `gamer_rsi/loop/schemas/`，round-trip 测试通过。
- **G6 可复现命令**：§3 四条命令在干净 checkout 上可直接复制执行成功（含 `--no-reflect` 免模型路径）。
- **M2 验收（计划 §7 / Spec §7）**：闭环自动运行 ≥1 轮演化并产出 diff 与决策记录；`--resume` 与 HITL 挂起/恢复各有一条端到端演示记录（README 附命令与日志片段）。
- **计分正确性**：任意中断/恢复组合下，`scoreboard.json` 的 `episode_key` 集合等于「实际完整跑完的 episode 集合」，无重复、无幻影。

## 13. 指标与埋点

`runs/<run_id>/metrics/loop_trace.jsonl` 每行一个事件（`ts` + `run_id` + `event` + 载荷，由 MOD-09 logger 落地）：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `episode_start` | `episode_id, seed, split, generation, harness_version` | 局级吞吐、代际归属 |
| `episode_end` | `steps, outcome, terminated_by, cum_reward, floors, kills, illegal_count, wall_ms` | 主/次级指标原料（MOD-08 聚合） |
| `illegal` | `episode_id, step, reason, retry_index` | `illegal_rate`、格式鲁棒性 |
| `reflect` | `seed, status(ok\|skipped), reason, latency_ms, template_sha256` | 反思成功率、模板版本追溯 |
| `evolve` | `cycle, generation, candidates, eval_episodes_used, wall_ms` | 周期开销与预算追踪 |
| `decision` | `generation, decision, decided_by`（**MOD-07 产生，本模块只落盘**） | 代际曲线 |
| `budget_exhausted` | `scope(eval\|generation), used, cap` | 成本失控告警 |
| `interrupted` / `resume` | `phase, completed, pending` | 续跑可靠性 |

派生计数（本模块只计数，不做统计推断）：`episodes_per_hour`、`mean_wall_ms_per_episode`、`truncated_rate`、`reflection_skip_rate`、`resume_count`、`eval_budget_utilization`。

## 14. 依赖与风险

依赖（Spec §6）：MOD-01（规则与 `step`）、MOD-02（`SandboxEnv`，INV-1 唯一入口）、MOD-03（`generate_json` + 超时）、MOD-04（`decide`/`reflect` + 三级校验）、MOD-05（记忆写入与检索）、MOD-09（配置/日志/`run_id`/CLI/git）。下游：MOD-07（消费 `run_evaluation_batch` 与状态机）。

| 风险 | 影响 | 对策 |
|---|---|---|
| 状态先写、轨迹未写完导致不一致 | 重复计分或漏局 | 先写轨迹并 flush、后写 `state.json`；幂等键去重；T-06-7/8/17 覆盖 |
| 评估进程池内存翻倍（24GB 统一内存） | OOM 崩溃 | `eval_workers` 默认 2，启动前按 `loop.eval_memory_budget_gb` 校验，不足自动降为 1（§8） |
| 训练局并发导致记忆库竞态 | 记忆污染、不可复现 | `train_concurrency` 固定 1 + `locks/memory.lock` 文件锁，拒绝 >1 配置 |
| HITL 挂起期间周期被重复触发 | 重复演化、浪费算力 | `evolving_cycle` + `last_completed_cycle` + MOD-07 产物存在性三重校验（FR-06-6） |
| 反思把 held-out 信息带入记忆（INV-5） | 泄漏、增益虚假 | 仅训练局写记忆；seed 只从 MOD-08 拿；G4 正则审计 |
| 长 run 轨迹目录膨胀 | 磁盘打满 → `E_IO_WRITE` | `loop.max_run_bytes` 配额告警；轨迹保留 ≥ 全部代际（Spec §8.4） |
| MOD-07 接口在 M2 前未就绪 | 无法联调 | `evolve_fn` 依赖注入 + `--no-evolve` 开关，用 stub 跑通 M2 前半段 |

## 15. 里程碑与工作量估算（人日）

| 工作项 | 人日 | 交付 |
|---|---|---|
| `run_episode` 步循环 + JSONL 落盘 + 终止判定 | 1.5 | FR-06-1/2，T-06-1..3 |
| 反思编排 + 降级 + MOD-05 写入 | 1.0 | FR-06-3/4，T-06-4/5 |
| run 状态机 + `--resume` + 计分幂等 | 2.0 | FR-06-8/9/10，T-06-7/8/13/17 |
| 周期调度 + 幂等 + generation/harness_version | 1.5 | FR-06-5/6/7，T-06-6 |
| `run_evaluation_batch` + 进程池并发 | 2.0 | FR-06-12/14，T-06-10/11 |
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
