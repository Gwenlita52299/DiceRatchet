# MOD-06 loop —— 闭环编排器

> 上游：[MOD-06 loop 技术 PRD](../../docs/prd/MOD-06-loop-PRD.md)（v1.6）·
> [模块拆分 Spec](../../docs/模块拆分Spec-DiceRatchet.md)（唯一事实来源）
> 交付里程碑：M2 · 代码路径：`dice_ratchet/loop/`

MOD-06 是 L2 进化层里的**编排器**：它不产生策略、不训练模型、不判定改进好坏，
只负责让 `play → evaluate → reflect → evolve` 这条闭环在一局、K 局、一代三个时间
尺度上**正确、可复现、可中断、可恢复**地跑起来。

## 1. 模块布局

| 文件 | 职责 | PRD 对应 |
|---|---|---|
| `state.py` | `RunState` / `Scoreboard` / `QueueLog`——`state.json` 的语义所有者 | FR-06-8/9/10 |
| `trajectory.py` | `episodes/<split>/<key>.jsonl` 的逐步落盘（append + flush + 退避重试） | FR-06-2 |
| `runner.py` | `EpisodeRunner.run_episode`——**唯一**的单局时序实现 | FR-06-1/2/12 |
| `reflect.py` | 确定性 `EpisodeSummary` + 反思编排 + 降级 | FR-06-3/4 |
| `generation.py` | `E_gen` 成本闸门 + 周期幂等 + `LoopOrchestrator` + 信号处理 | FR-06-5…15 |
| `cli.py` | `loop run\|status\|smoke`（经组合根注册进 MOD-09 命令面） | Spec §4.6 |

PRD §6 只列了 `runner.py / reflect.py / generation.py` 三个文件；本实现把「状态机」
与「轨迹写入」拆成独立模块（`state.py` / `trajectory.py`），因为它们的**所有者不同**：
`state.json` 的字段语义归 MOD-06，而原子写原语归 MOD-09（`infra.run`）；轨迹行的
schema 校验归 MOD-09（`infra.logging`）。拆开后可注入、可单测，且分层守卫能证明
`loop/` 没有 import `dice_ratchet.env`（INV-1）。

## 2. 对外接口

```python
from dice_ratchet.loop import (EpisodeRunner, LoopOrchestrator, RunContext, RecordedEpisode,
                            episodes_per_generation, maybe_run_generation, EvolveOutcome,
                            init_state, load_state, save_state, Scoreboard)

# 单局（MOD-08 也可注入 evaluate_arm(runner=EvalRunnerAdapter(runner))）
runner = EpisodeRunner(env=..., policy_factory=..., memory=..., writer=..., config=..., run_ctx=...)
rec = runner.run_episode(seed, split="train", arm="train", harness_version=0)

# run 级编排（MOD-07 经 evolve_fn 注入）
orch = LoopOrchestrator(run_ctx=ctx, config=cfg, runner=runner, evolve_fn=mod07_fn)
summary = orch.run(seeds, resume=False)      # RunSummary(exit_code=…)
state = orch.status()                        # RunState
```

- `run_episode` 的终止判定**只看** MOD-02 的 `last.terminated or last.truncated`；
  步数/决策上限与 `policy_failure` 全由 MOD-02 判定（Spec B-15），MOD-06 不自行判负。
- `maybe_run_generation(state=, run_ctx=, evolve_fn=)` 是周期边界的**幂等**入口：
  `last_completed_cycle` + `evolving_cycle` + MOD-07 产物存在性三重校验（FR-06-6）。
- 依赖全部可注入（`env/policy/memory/writer/client`，NFR-06-6）；`tests/fakes.py`
  提供 `FakeEnv / FakePolicy / FakeMemory / ScriptedClient`。

## 3. 配置项

`loop.*` 的权威定义在 `dice_ratchet/infra/config.py::LoopConfig` 与 `configs/default.yaml`。
与成本相关的四个键（`k / n_candidates / smoke_n / heldout_n`）决定 `E_gen`：

```
E_gen = K + (H + S) + C·(S + H)           # 默认 30 + 42 + 1×42 = 114 ≤ max_episodes_per_generation(120)
```

**⚠️ 2026-10-14 修订（D-M3-7）**：上式与默认值未变，`E_gen=114` 仍是与
`max_episodes_per_generation`(120) 比较的**保守上限**；但 parent 的 heldout 改为**按 play 面
指纹继承**（同 play 面已有测量命中即 0 局、不重测）⇒ 继承命中时**实际每 cycle = 74 局**
（`K 30 + parent 冒烟 2 + 候选(冒烟 2 + heldout 40)`），只有继承未命中时才退回 114。

配置优先级：CLI 参数 > `DICERATCHET_LOOP__*` 环境变量 > `configs/default.yaml`。
`loop.retry_reflections_on_resume`（或 `loop run --resume --retry-reflections`）**默认关闭**：
补跑 skipped 反思会改变记忆内容进而影响后续局与确定性，所以必须是显式选择
（PRD §16 开放问题 7）；开启后只对**已完成且有计分记录**的训练局补跑。

run 级 override（B-30 的分层写法，参与 `config_hash`）：

```bash
DICERATCHET_ENV__EPISODE__DIFFICULTY=medium DICERATCHET_ENV__EPISODE__STAGE_COUNT=6 \
  python -m dice_ratchet loop run --agent heuristic --seeds train:1000-1002 --k 3
```

## 4. 可复现命令（G6）

```bash
# ① 跑 N 代（--agent llm 需要可用的 MOD-03 客户端）
python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl

# ② 看进度
python -m dice_ratchet loop status --run-id <run_id>

# ③ 续跑（**必须重复①里的 --k/--generations/--hitl 覆盖集合**，见下）
python -m dice_ratchet loop run --run-id <run_id> --resume --k 30 --hitl

# ④ 免模型冒烟（不触网、不调模型）
python -m dice_ratchet loop smoke --seeds train:1000-1002 --k 3 --no-reflect

# ⑤ M2 联调用的 bring-up 口径（medium × 6，秒级）
DICERATCHET_ENV__EPISODE__DIFFICULTY=medium DICERATCHET_ENV__EPISODE__STAGE_COUNT=6 \
  python -m dice_ratchet loop run --agent heuristic --seeds train:1000-1002 --k 3 \
  --generations 1 --no-reflect --no-baseline --run-id bringup-m6
```

**关于 ③**：Spec §4.6（F10/F18）规定 `--k/--generations/--hitl/--model/…` 并入
`config_hash`，因此**同一 `--run-id` 只能承载覆盖集合完全相同的命令**。PRD §3 把它写成
不含覆盖的裸 `--resume`——那只有在①用的都是默认值时才成立（`--k 30` 就是默认值，
但 `--hitl` 不是）。本模块按 Spec 执行，并补了一条 `--k` 与 `state.json` 的一致性校验
（同一 run 的 K 恒定，否则周期幂等键 `len(completed)//k` 会漂移）。

## 5. 事件与埋点

统一经 MOD-09 `infra.events.emit` 落 `runs/<run_id>/events.jsonl`（NFR-06-4）：

| 事件 | 触发点 |
|---|---|
| `loop.episode_start` | 每局开始（含 `episode_key/seed/split/arm/generation/harness_version`） |
| `loop.episode_end` | 每局结束（主/次级指标原料，见 PRD §13） |
| `loop.reflect` | 每次反思（`status=ok\|skipped`、`reason`、`template_sha256`） |
| `loop.cycle_start` | 每代开始（`cycle/generation/e_gen/est_hours`） |
| `loop.budget_exhausted` | `E_gen > max_episodes_per_generation`（全系统唯一成本上限） |
| `loop.interrupted` / `loop.resume` | 信号中断 / 续跑 |

## 6. 与 PRD 的已知偏差（实现裁决记录）

1. **周期触发的 `% K == 0`**：PRD §8 的伪代码把 `prev` 初始化为 `-1`，那会让第 1 局
   之后（`cycle_index == 0 > -1`）就触发一次演化，与 FR-06-5「每完成 K 局训练局触发
   一次」直接冲突。实现以 FR-06-5 为准，额外要求 `len(completed) > 0 and len(completed) % K == 0`。
2. **`last_completed_cycle` 的取值**：按 §7.3 的 `cycle_index = len(completed)//k`，
   跑满 6 局（K=3）后是 **2**（PRD §11 的 T-06-6 文本写作 1）。§7.3/§8 是规范文本，
   本实现以它们为准；测试断言 2 并注明来源。
3. **JSON Schema 位置**：PRD G5 写 `dice_ratchet/loop/schemas/`，仓库既有约定是顶层
   `schemas/*.schema.json`（MOD-09 `infra.io.validate` 的搜索根，且
   `tests/unit/infra/test_config.py::test_schema_ids_are_uniform` 对顶层目录逐一登记）。
   本实现遵循仓库约定：`schemas/{run_state,scoreboard,loop_reflection}.schema.json`。
4. **`loop smoke` 的 < 2 s**：NFR-06-6 的「< 2 s 跑完 3 局」是**假依赖**路径的口径
   （NFR-06-3 也把编排开销与环境 step 分开：≤5 ms/步）。真实环境冒烟的墙钟取决于
   环境本身：hard×6 约 5.2 s / 3 局，easy×3 约 2.9 s / 3 局（含解释器启动）。
   集成用例据此断言「3 局 easy×3 在 30 s 内」，另有一条假依赖的 ≤5 ms/步用例。
5. **`reflections/<episode_key>.json`**：PRD §7.1 的目录树写 `<seed>.json`，但 §6 的
   schema 与 T-06-4 都写 `<episode_key>.json`。实现取后者（与 `episode_key` 幂等键一致，
   同 seed 跨代不互相覆盖）。
6. **中断重跑会重写该局轨迹文件**：FR-06-2 要求「每步 append+flush」，T-06-8 要求
   「前 2 步已在 JSONL」。两者都满足（中断时磁盘上确实留着已完成的那些行），但**重跑**
   同 `episode_key` 时文件被整体重写——否则文件里会出现两段从 `step=0` 开始的孤儿行，
   MOD-02 的 `audit leak` 轨迹重放会把它判成 parity mismatch。
7. **`E_ENV_INTERNAL` 的退出码**：PRD §10 要求环境内核错误累计 ≥3 时以退出码 `4` 中止。
   `E_ENV_INTERNAL` 原本未在 `infra/errors.py::EXIT_CODE` 登记（默认落到 1），
   已在 MOD-09 侧登记为 `EXIT_SANDBOX`。
8. **新增错误码**：`E_IO_CONCURRENCY`（退出码 2）、`E_IO_INTERRUPTED`（130）、
   `E_EVAL_SPLIT_VIOLATION`（4）、`E_EVAL_EPISODE`（1）——PRD §10 引用了它们，
   按 Spec §8.3「各 PRD 只能引用已登记的码」在 MOD-09 侧补登记。

## 7. 测试

```bash
python -m pytest tests/unit/loop tests/integration/test_loop_closure.py \
                 tests/integration/test_loop_memory.py -q
```

- 单测（`tests/unit/loop/`）：T-06-1…T-06-13，纯假依赖、不调模型、不写真实 `runs/`；
- 集成（`tests/integration/test_loop_closure.py`）：T-06-14…T-06-17 + gen-000 基线 +
  HITL 端到端 + 编排开销（NFR-06-3）；
- 跨模块（`tests/integration/test_loop_memory.py`）：训练局反思→真实 MOD-05 落库→
  可检索；评估局只读 `frozen(v)`、不写主台账（INV-5）。

## 8. 失败恢复矩阵（PRD §10 的落地位置）

| 触发条件 | 错误码 | 落地位置 |
|---|---|---|
| 模型推理超时 / 后端不可用 | `E_LLM_*` | MOD-03/04（本模块只读 `degraded`） |
| 环境内核错误 | `E_ENV_INTERNAL` 等 | `LoopOrchestrator._run_training_episode`（≥3 → 退出码 4） |
| 环境 `reset` 失败 | `E_ENV_CONFIG_INVALID` | 同上（跳过该 seed，`queue.jsonl` 记 `skip`） |
| 磁盘写失败 | `E_IO_DISK_FULL`/`E_IO_WRITE` | `TrajectoryWriter`（`loop.io_retry` 退避） |
| `state.json` 写失败 | `E_IO_RUN_STATE` | `state.save_state` |
| 反思失败/超时 | `E_LLM_TIMEOUT`/`E_LLM_SCHEMA_INVALID` | `reflect.reason_of` + `_reflect_one`（只降级） |
| 记忆写入失败 | `E_IO_MEMORY_WRITE_FAILED` | `EpisodeRunner._record_usage`（重试 ≤2 后跳过） |
| MOD-07 抛 `E_EVOLVE_*` | `E_EVOLVE_*` | `_call_evolve`（保守拒绝，`generation += 1`） |
| 成本超限 | `E_EVAL_BUDGET_EXCEEDED` | `maybe_run_generation`（退出码 1） |
| 非法并发 | `E_IO_CONCURRENCY` | `LoopConfig.validate` + `cli._guard_concurrency`（退出码 2） |
| seed split 越权 | `E_EVAL_SPLIT_VIOLATION` | `_verify_train_splits`（P0，立即中止） |
| `SIGINT`/`SIGTERM` | `E_IO_INTERRUPTED` | `SignalGuard`（退出码 130，`--resume` 无损续跑） |
