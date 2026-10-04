# GameRSI 模块拆分 Spec

> 版本：v1.4（2026-09-30 17:49，全部待裁决条目已裁决，见 §11/§12）· 状态：已评审 · 定位：**模块边界与接口的唯一事实来源（Single Source of Truth）**
> 上游：[项目计划-GameRSI.md](../项目计划-GameRSI.md) · [RSI调研纪要.md](../RSI调研纪要.md)
> 游戏本体：`docs/prd/GAME-00/01/02`（规则、观测、动作的游戏侧事实来源，B-09）
> 下游：`docs/prd/MOD-*.md`（每个模块的技术 PRD 必须与本文件一致；冲突时以本文件为准）

---

## 0. 文档定位

本 Spec 回答三个问题：

1. **切成几块**——模块清单、分层、职责与**非职责**；
2. **怎么接起来**——跨模块的数据结构、函数签名、CLI、目录与文件契约；
3. **谁先谁后**——依赖矩阵、开发顺序、里程碑到模块的映射、质量门。

**读者**：模块实现者（含 LLM agent）、评测复核者、后续接手的自己。
**不写**：具体算法调参、prompt 全文、UI 设计——这些下放到各模块 PRD。

---

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│ L3 测量与工程层   MOD-08 evals（裁判/指标/坍缩检测）  MOD-09 infra      │
│                    ▲ 只读轨迹与报告，不参与决策                        │
├──────────────────────────────────────────────────────────────────────┤
│ L2 进化层         MOD-06 loop（编排/反思/代际）  MOD-07 evolve（自编辑）│
│                    ▲ 消费轨迹与评分，产出新 harness 版本                │
├──────────────────────────────────────────────────────────────────────┤
│ L1 智能体层       MOD-03 inference（本地推理）  MOD-04 policy（决策）  │
│                   MOD-05 memory（策略记忆/反思库）                     │
│                    ▲ 只见 Observation，永不见 Env 内部状态              │
├──────────────────────────────────────────────────────────────────────┤
│ L0 环境层         MOD-01 env-rules（规则引擎）  MOD-02 env-sandbox     │
│                   （观测投影 / 隔离 / 审计 / 蜜罐）                     │
└──────────────────────────────────────────────────────────────────────┘
```

**核心不变量（违反即视为 P0 缺陷）**

| # | 不变量 | 由谁保证 |
|---|---|---|
| INV-1 | agent 只能通过 `SandboxEnv.observe/step` 触达环境；L1 及以上代码 **禁止 import `gamer_rsi.env.*` 内部模块** | MOD-02 静态检查 + MOD-09 import 门禁 |
| INV-2 | `Observation` 是白名单投影，任何新字段必须在 `OBSERVATION_SCHEMA` 登记 | MOD-02 |
| INV-3 | 进入 prompt 的上下文只允许：当前 Observation + 检索到的 memory + 当前 harness 工件 + 任务说明（`gamer_rsi/agent/prompts/task.md`，人工版本化，**不属**演化对象，B-27） | MOD-04 |
| INV-4 | harness 自编辑产出的代码在受限执行环境运行：无网络、无文件写、无 subprocess、无 env 内部访问 | MOD-02 + MOD-07 |
| INV-5 | train / heldout / test 三类 seed 严格隔离；heldout 与 test 的 seed 值不得进入任何 prompt 或记忆 | MOD-08 + MOD-09 |
| INV-6 | 所有随机性来自显式 seed（`env`、采样、演化），禁用全局 `random`/时间相关随机 | MOD-01 + MOD-09 |
| INV-7 | 评估主指标只用**环境原生结果**：`outcome`（win/lose，截断一律计 lose）为第一主指标，`stages_cleared` / `hearts_left` / `battles_won` 为并列次指标（全部取自 GAME `stats` 与 `player`）；shaped reward 仅用于学习与调试 | MOD-08 |

**不变量实施细则（v1.4 裁决）**：
- **INV-1 类型导入（B-23）**：动作类型 `Action`（GAME 动作对象的 TypedDict）与 `ACTION_KINDS` 定义在 `gamer_rsi/env/actions.py`，由 `gamer_rsi/sandbox/api.py` re-export。L1+ 只许 `from gamer_rsi.sandbox.api import Action`；直接 import `gamer_rsi.env.*` 一律由 import 门禁拒绝。
- **INV-1 全状态封装（B-24）**：MOD-01 的 `GameState` 只由 `SandboxEnv._engine` 私有持有，没有任何返回全状态、engine 或 rng 状态的公开成员。`gamer_rsi.env` 在 MOD-02 的 `BLOCKED_MODULES` 中对 L1+ 不可导入。v1.2 登记时的 `build_full_state()` 方案，因 MOD-01 v1.0 改为移植 GAME 内核而失去对象，改用上述等价的封装形式。
- **INV-2/INV-3 审计口径（B-25）**：MOD-02 审计器只做黑名单扫描 + canary 探针，不做"prompt 中每个数值都归因到可见集"的正向校验，因为那样噪声太大；M1 后若仍出现未知泄漏，再升级为正向校验。

---

## 2. 模块清单

| ID | 模块 | 层 | 一句话职责 | 里程碑 | 关键依赖 |
|---|---|---|---|---|---|
| MOD-01 | env-rules | L0 | GAME 内核（`src/game/kernel`）的纯 Python 等价移植 + golden 对拍 | M0 | GAME-01（契约）、`src/game/content`（配表） |
| MOD-02 | env-sandbox | L0 | 观测裁剪、进程/代码隔离、轨迹审计、蜜罐测试 | M0 | MOD-01 |
| MOD-03 | inference | L1 | 本地模型服务抽象（MLX/llama.cpp）+ 结构化输出与重试 | M1 | MOD-09 |
| MOD-04 | policy | L1 | prompt 组装、动作解析与校验、harness 工具函数契约 | M1 | MOD-02/03/05 |
| MOD-05 | memory | L1 | 策略记忆库与反思库：写入、检索、去重、版本 | M2 | MOD-04 |
| MOD-06 | loop | L2 | 局/代际编排：play→evaluate→reflect→evolve 调度 | M2 | MOD-01..05 |
| MOD-07 | evolve | L2 | harness 候选生成、held-out 裁判、接受/回滚、人在环门 | M2–M3 | MOD-06/08/09 |
| MOD-08 | evals | L3 | 基线、held-out 评估协议、指标、多样性/坍缩、消融 | M1–M3 | MOD-01/02 |
| MOD-09 | infra | L3 | 配置、日志/轨迹、run_id 与版本、CLI、绘图、git 操作 | M0–M3 | 全模块（被依赖） |
| MOD-10 | distill（预留） | L3 | STaR 式轨迹筛选 + LoRA 微调，产出新基座模型 | M4（可选） | MOD-06/08 |

---

## 3. 模块边界细则（职责 / 非职责）

> 每条 PRD 必须逐字复用本节的"非职责"清单作为 Out of scope，防止职责漂移。

### MOD-01 env-rules
- **负责**：按 GAME-01 逐位移植内核（`createGame/reduce/legalActions/observe/digest`、SplitMix64 + 命名流、配表加载与校验），与 `src/game/tests/golden/*.jsonl` 逐行对拍；`state_hash()`；非法动作判定。
- **不负责**：发明或修改规则（规则改动先改 GAME PRD 与 JS 内核，再同步移植）、观测裁剪（MOD-02）、prompt（MOD-04）、模型调用（MOD-03）、shaped reward 与评分统计（MOD-08）。

### MOD-02 env-sandbox
- **负责**：GAME 观测 → `obs-agent` 白名单投影与紧凑渲染；决策失败的唯一判负方（§4.3）；`SandboxEnv` 门面（唯一的 agent 侧入口）；harness 代码受限执行与静态扫描；轨迹泄漏审计器；蜜罐泄漏注入与告警。
- **不负责**：规则本身（MOD-01 / GAME）、shaped reward（MOD-08）、模型侧重试（MOD-03）。

### MOD-03 inference
- **负责**：本地后端抽象（MLX / llama.cpp）、模型加载与生命周期、`generate_json()`（JSON Schema 约束 + 失败重试 + 超时降级）、token/延迟埋点、temperature/top-p 等采样参数管控；以及**可选**辅助接口 `count_tokens()` 与 `embed()`（供 MOD-05 记忆检索；阶段一默认关闭，MOD-03 可返回 `E_LLM_UNSUPPORTED` 由调用方降级为 BM25）。
- **不负责**：决定 prompt 内容（MOD-04）、动作合法性判断（MOD-01/04）、模型微调（MOD-10）。

### MOD-04 policy
- **负责**：prompt 模板与上下文组装（含 harness 注入点）、LLM 原始输出 → `Action` 的解析与三级校验、非法动作的重试与轻罚触发、`rank_actions()` 等 harness 工具函数的签名与注册表、规则/启发式 fallback。
- **不负责**：加载模型（MOD-03）、改自己的 prompt 文件（那是 MOD-07 的写入动作）、记忆检索实现（MOD-05）。

### MOD-05 memory
- **负责**：策略记忆条目与反思条目的 schema、写入与去重、基于当前 Observation 的检索（关键词/tag/embedding 任选）、容量与淘汰策略、记忆的版本与可回滚（因为记忆本身也是被演化对象之一）。
- **不负责**：生成反思文本（MOD-06）、评估记忆是否有用（MOD-08 提供指标，MOD-07 做取舍）。

### MOD-06 loop
- **负责**：单局回合循环、局末反思触发、每 K 局触发一次演化周期、run 级状态与断点续跑、把各模块串成 `play → evaluate → reflect → evolve` 的编排层及其失败恢复。
- **不负责**：判定候选改进是否被接受（MOD-07）、计算 held-out 指标（MOD-08）、harness 文件的具体 diff（MOD-07）。

### MOD-07 evolve
- **负责**：候选改进生成（prompt 增删改 / 决策工具函数补丁 / 反思模板）、候选在 held-out 上的评估编排（调用 MOD-08）、接受/拒绝/回滚决策、harness 版本目录与 git commit、代际日志、人在环确认门。
- **不负责**：模型推理实现（MOD-03）、指标定义与统计口径（MOD-08）、seed 抽取规则（MOD-09 注册表 + MOD-08 协议）。

### MOD-08 evals
- **负责**：三类 seed 协议与 held-out 评估执行、基线（random / heuristic / fixed-prompt）实现与归档、指标计算（主指标 + 次级指标 + 置信区间）、shaped reward 定义（`evals/reward.py`，仅学习/调试用）、多样性/坍塌监控、训练-留出增益真实性判定、消融实验（去反思 / 去自编辑）。
- **不负责**：修改 harness（MOD-07）、写游戏规则（MOD-01）、编排训练局（MOD-06）。

### MOD-09 infra
- **负责**：配置加载与优先级、结构化日志与 JSONL 轨迹落地、`run_id`/`harness_version`/配置哈希、CLI 入口、绘图、git 操作封装、确定性守卫（禁全局随机、固定 PYTHONHASHSEED）、import 门禁检查。
- **不负责**：任何领域逻辑（不得在 infra 里写游戏或评估规则）。

---

## 4. 全局接口契约

> v1.3 起，游戏侧事实以 GAME-00/01/02 为准（B-09）：动作 kind、观测字段名、规则与配表全部**引用**游戏侧契约，本节只定义 agent 侧的投影、封装与跨模块调用。字段名沿用 GAME 的 camelCase（Python 侧以 dict 键访问，不做改名映射，避免对拍时出现一整类映射错误）。

### 4.1 环境接口（MOD-01/02 对外）

```python
# gamer_rsi/sandbox/api.py
class SandboxEnv:
    def reset(self, seed: int, *, episode_config: EpisodeConfig) -> Observation: ...
    def step(self, action: Action) -> StepResult: ...          # 单个 GAME 动作
    def reject(self, *, stage: Literal["syntax", "semantic"], reason: str,
               raw: str | None, final: bool) -> StepResult: ...  # 不改状态，记一次非法（B-15）
    def observe(self) -> Observation: ...
    def legal_actions(self) -> list[Action]: ...   # GAME legalActions() 去掉 abandon_run；同一状态下顺序确定
    def hints(self) -> dict: ...                   # GAME playHints()（伤害预览/非法原因，公开信息），供启发式使用
    def close(self) -> None: ...

@dataclass(frozen=True)
class EpisodeConfig:            # 展开为 GAME createGame 输入（GAME-00 §8.2）
    difficulty: Literal["easy", "medium", "hard"] = "hard"
    stage_count: Literal[3, 6, 10] = 6        # GAME D-34；默认局定义见 §8.6
    toggles: Mapping[str, bool] = ALL_ON       # mapBranching/relics/events/shops/schoolCounter
    character: str = "warrior"

@dataclass(frozen=True)
class StepResult:
    observation: Observation
    reward: float              # shaped，仅供学习/调试（INV-7）；计算归 MOD-08 `reward.py`
    terminated: bool           # outcome 已定（win / lose）
    truncated: bool            # 步数/决策数上限或 policy_failure（主指标一律计为 lose）
    info: dict                 # {"illegal": bool, "illegal_reason": str|None,
                               #  "outcome": "win"|"lose"|None, "truncation_reason": str|None,
                               #  "stages_cleared": int, "hearts_left": int, "battles_won": int,
                               #  "state_hash": str, "obs_digest": str}
```

### 4.2 Observation（agent 侧投影 `obs-agent-1.0.0`，MOD-02 唯一权威定义）

agent 侧观测 = GAME `observe(state, "full")`（GAME-00 §9.4 `obs-1.0.0`）的**键子集** + 3 个 agent 专属键。不改名，不新增游戏语义。

| 来源 | 字段 | 说明 |
|---|---|---|
| 保留 | `phase` `stage` `chapter` `turn` `difficulty` | `phase ∈ map/player_turn/shop/event/treasure/shrine/forge/rest/relic_swap/settle` |
| 保留 | `player{hearts,heartsMax,virtue,virtueMax,ward,starShards,statuses,deckSchool,purifyLeft,abilities}` | 自身状态 |
| 保留 | `hand[]` `slots[]` `enemies[]`（含 `intent`） | 战斗内；`intent` 在玩家回合开始时提交且不重掷，所以是真实信息 |
| 保留 | `map`（仅已揭示的行与 `reachable`/`preview`） | 地图 |
| 保留 | `shop` `event` `treasure` `forge` `shrine` `rest` `pendingRelic` | 仅在对应 `phase` 非 null |
| 保留 | `toggles` `relics` | 公开配置与遗物 |
| 改写 | `legalActionKinds` | 去掉 `abandon_run` |
| 新增 | `schemaVersion="obs-agent-1.0.0"` `episodeId` `decisionIndex` | 标识 |
| 新增 | `stageCount` | 本局关数（公开） |
| 新增 | `lastResult: {illegal, reason} \| null` | 上一次被拒绝的原因（B-15 重试反馈） |
| 删除 | `sessionId` `runId` `seed` `config` `stages` `stats` `outcomeReason` `truncationReason` | 与决策无关或属于审计信息 |

**严禁出现在 Observation 中**（同 GAME-00 §9.4）：seed、RNG 流状态、未来骰面、未揭示的地图节点与内容、商店未展示商品、敌人未公开状态、`GameState` 全量。`state_hash` 与 `obs_digest` 是单向摘要，只进 `info`，不进 prompt。

**token 预算**：MOD-04 渲染后的观测 p95 ≤ 1,500 token（8B 模型 `context_length=8192`）。GAME 原始观测约 12k 字符，所以 MOD-02 提供确定性的**紧凑渲染** `render_compact(obs) -> str`（地图只展开下一行可达节点，已访问的行折叠）。紧凑渲染是投影的一部分，版本随 `schemaVersion` 走。

### 4.3 决策与动作契约（MOD-04 产出，MOD-01 消费）

**决策点**（B-12）：`phase=player_turn` 时每回合一次决策，产出本回合的**动作计划**；其余 `phase` 每一步一次决策，计划长度固定为 1。模型输出统一为：

```json
{"reasoning": "≤2 句", "plan": [{"kind": "play_face", "dieId": "d-0007", "slotId": "slot_1", "targetId": "e-00"},
                                 {"kind": "end_turn"}]}
```

- `plan[i]` 就是 GAME 动作对象，kind 与字段**逐字引用 GAME-01 §4 分派表**。agent 可用的 kind：`play_face/discard_die/purify/use_ability/end_turn/map_choose/shop_buy/shop_refresh/shop_remove/shop_leave/event_choose/treasure_pick/forge_pick/shrine_choose/rest_choose/relic_swap`（`abandon_run` 对 agent 不开放）。
- 计划长度：`player_turn` 为 1..12，其余为 1。
- **执行语义**（MOD-04 `play_decision`）：
  - 按顺序逐个执行。每个动作执行前，都要用**当前**的 `legal_actions()` 校验。
  - 离开 `player_turn`（战斗结束或进入敌方回合）后，剩余动作全部丢弃，不计非法。
  - 计划执行完仍处于 `player_turn` → 开始下一个决策（不自动 `end_turn`）。
- **GBNF 友好**：schema 只用 `object/array/string/integer/enum/required/maxItems`，`kind` 用 enum，其余字段设为可选，由语义层校验。不用 `oneOf/allOf/if`（解决 P-09 与 MOD-03/04 schema 分叉的问题），schema 文件唯一，位于 `schemas/decision.schema.json`。

**三层校验与重试（唯一口径，B-15）**：

| 层 | 归属 | 失败处理 | 计非法？ |
|---|---|---|---|
| L0 格式修复 | MOD-03 `generate_json` 内部（首答 + ≤2 次修复） | 耗尽 → `E_LLM_SCHEMA_INVALID`，视为本次**尝试**失败 | 否（只进 `llm_calls.jsonl`） |
| L1 语法 / L2 语义 | MOD-04（schema + 当前 `legal_actions()`） | `env.reject(final=False)`：不改状态，`illegal_count += 1`，shaped `-0.05`；带 `lastResult` 重试 | 是（每次尝试计 1） |
| L3 执行 | MOD-01 `reduce` 拒绝 | 同 L2 | 是 |

- L1 的**修复 prompt 模板**由 MOD-03 `inference/schema.py::repair_messages` 独占拥有（只纠错、零领域知识），MOD-04 与 harness 都不得覆盖（B-28）。
- 每个决策最多 **3 次尝试**（首次 + `R=2` 次重试）。重试时换采样 seed：`derive_seed(run, episode, decision, attempt)`。
- 3 次都失败 → `env.reject(final=True)`，然后由**启发式 fallback** 执行 1 个动作（`source="fallback"`），决策完成。
- **判负只由 MOD-02 `SandboxEnv` 负责**：连续 3 个决策都以 `final=True` 结束 → `truncated=True, truncation_reason="policy_failure"`，主指标计为 lose。MOD-04/06 只读 `info`，不另行判负。
- GAME 内核自身的 `consecutiveIllegal`（Web 会话挂起用）对 agent 路径不生效。

### 4.4 Harness 工件契约（MOD-07 写，MOD-04 读）

```
harness/
  version.json          # {"version": int, "parent": int|null, "git_commit": str, "artifact_hash": str,
                        #  "created_at": iso, "reason": str, "accepted_by": "auto"|"human", "base_model": str|null}
  prompts/decision.md   # 决策 prompt 模板（含 {{observation}} {{memory}} {{legal_actions}} 占位）
  prompts/reflection.md # 反思模板
  tools/*.py            # 决策工具函数，纯函数 + @harness_tool 注册
  config.json           # 采样参数（训练期）、检索 top_k 等可演化配置；不得覆盖 §8.1 的评估温度
```

- `artifact_hash = sha256(按路径排序的 (相对路径, 文件 sha256) 列表)`，由 MOD-09 计算（B-19）。
- git tag 统一为 `harness-v{N}`（不建同名分支，B-22）。
- **历史快照布局（B-29）**：`harness/versions/v{N}/` 是不可变快照，内容为 `version.json` + `diff.patch`（相对 parent）+ `prompts/` + `tools/` + `config.json`（全量拷贝）。候选与门禁记录不进 `harness/`，写到 `runs/<run_id>/generations/gen-{NNN}/{candidates/,patches/,gate.json}`。
- `version.json.base_model` 为 MOD-10 预留：M0–M3 恒为 `null`，启用 LoRA 新基座后记录基座标识，并参与 `artifact_hash`（B-33）。

工具函数唯一签名（**禁止变更**）：

```python
@harness_tool
def rank_actions(obs: dict, candidates: list[dict], memory: list[dict]) -> list[dict]:
    """返回重排后的 candidates；纯函数，无 IO、无网络、无随机、无 import env。"""
```

工具在 MOD-02 提供的**常驻子进程 worker** 中执行（rlimit 设在子进程上，超时即杀并重启，B-20），主进程（持有模型）不设 rlimit。

### 4.5 轨迹与报告契约（schema 文件归 MOD-09 登记，字段由本节定义）

**文件布局**（B-16）：

```
runs/<run_id>/episodes/<split>/<episode_key>.jsonl
episode_key = e-{seed:06d}-g{generation:03d}-{arm}
arm ∈ train | parent | cand-{id} | baseline-{random|heuristic|fixed_prompt} | ablation-{name}
```

同一 seed 在不同代、不同候选下各有独立文件，互不覆盖；`episode_key` 同时作为 MOD-06 计分台账的幂等键（取代 P-13 的旧格式）。

**轨迹行**（每个 env 步一行；`reject` 也写一行，`illegal=true`，`action` 为模型原始动作）：

```jsonc
{"episode_key":"e-002013-g003-cand-b","generation":3,"harness_version":7,"arm":"cand-b",
 "seed":2013,"split":"heldout","step":14,"decision_index":6,"plan_index":1,"attempt":1,
 "source":"llm","obs_digest":"sha256:...","action":{"kind":"play_face","dieId":"d-0007","slotId":"slot_1"},
 "reward":0.03,"cum_reward":0.41,"illegal":false,"illegal_reason":null,
 "llm_raw":"...","llm_latency_ms":812,"prompt_tokens":1432,"completion_tokens":96,
 "prompt_hash":"sha256:...","memory_ids":["m-0012"],"tool_hash":"sha256:...","mode":"eval",
 "ts":"2026-09-30T12:00:00Z"}
```

- LLM 相关字段（`llm_raw/llm_latency_ms/*_tokens/prompt_hash/memory_ids`）只在决策内第一个执行的动作行上填写，其余行为 `null`。
- `source ∈ llm | fallback | heuristic | random | scripted`。
- 局末追加一行 `{"type":"episode_end", "outcome", "truncation_reason", "stages_cleared", "hearts_left", "battles_won", "decisions", "degraded", "exclude_reason"}`。
- **seed 不做 redaction**：轨迹属于审计侧，redaction 只作用于 prompt/memory/reflection（B-19）。

**代际报告** `runs/<run_id>/generations/gen-{NNN}/report.json`：

- schema 唯一文件为 `schemas/generation_report.schema.json`（`additionalProperties:false`），扩展量外置到 `metrics/gen-{NNN}.ext.json`（P-19）。
- **代数约定**：`gen-000` 是 harness v0 的基线评估（`decision="baseline"`），演化代从 1 开始；`harness_version ≥ 0`。
- **写入分工**：
  - MOD-08 `evaluate_generation()` 产出 `heldout/train/gain_realism/diversity/ablation` 段。
  - MOD-07 填写 `candidates/decision/decided_by/reason` 段。
  - MOD-09 `write_json_atomic()` 负责落盘并计算 sha256。

```jsonc
{"generation":3,"harness_version":7,"parent_version":6,"episode_config_hash":"sha256:...",
 "heldout":{"basket":"H1","n":40,"win_rate":0.475,"win_rate_ci95":[0.33,0.62],
            "mean_stages_cleared":4.1,"mean_hearts_left":0.9,"mean_battles_won":11.3,
            "mean_decisions":96.2,"illegal_rate":0.011,"fallback_rate":0.004,"truncation_rate":0.0,
            "degraded_n":0,"action_entropy_curve":[...]},
 "train":{"window":60,"win_rate":0.51},
 "gain_realism":{"win_rate_gap":0.035,"gap_threshold":0.15,"verdict":"null"},  // real_gain|null|overfit_suspect|regress|insufficient
 "diversity":{"entropy_slope":-0.004,"collapse_flag":false},
 "ablation":null,
 "candidates":[{"id":"b","kind":"tool_patch","gate":"pass","paired":{"method":"mcnemar","p":0.21,"delta":0.05}}],
 "decision":"reject","decided_by":"auto","reason":"C2 未显著"}
```

**HITL 挂起（B-32）**：`--hitl` 时，MOD-07 写 `generations/gen-{NNN}/gate.json`（`status="pending"`）；MOD-06 置 `phase="hitl_wait"` 并以**退出码 0** 结束，可用 `--resume` 续跑。挂起超过 `evolve.hitl_timeout_h`（默认 24）仍未批复的，下次 `--resume` 时保守回滚到 parent（`reason="hitl_timeout"`，`E_EVOLVE_GATE_TIMEOUT`）。

**数值精度（B-34）**：`report.json`、`metrics/*.json`、`generations.csv` 中的浮点数一律 `round(x, 4)`；本文示例中的 2 位小数只是展示。

### 4.6 命令面（MOD-09 统一暴露；新增命令必须回填本节）

```bash
python -m gamer_rsi env selftest --seed 1              # M0：规则/确定性自检 + GAME golden 对拍
python -m gamer_rsi play --agent llm|heuristic|random|fixed_prompt --seeds train:1000-1004 [--model ID]
python -m gamer_rsi baseline --kind random|heuristic|fixed_prompt --basket H1 [--model ID]
                       # --basket 与 --seeds 互斥；--basket 省略时按 --split 推导（train_window/test_full/H1）
                       # --model 仅 fixed_prompt 需要（冻结提示词 + 真实 LLM；拿不到即退出码 3）
python -m gamer_rsi loop run --generations 3 --k 30 [--hitl] [--run-id ID] [--resume]
python -m gamer_rsi loop status|smoke --run-id ID
python -m gamer_rsi evolve approve|rollback --run-id ID --generation 3 [--reason STR]
python -m gamer_rsi eval report --run-id ID --split heldout --baseline all
python -m gamer_rsi eval ablation|test --run-id ID [--basket H2]
python -m gamer_rsi eval diversity --run-id ID
python -m gamer_rsi audit leak --run-id ID            # INV-2/INV-3/INV-5 审计
python -m gamer_rsi inference probe|bench|robustness   # MOD-03
python -m gamer_rsi memory stats|snapshot|rollback|verify|retrieve   # MOD-05（B-08）
python -m gamer_rsi guards check [--root PATH]         # MOD-09（P-20）
```

统一约定：run 一律用 `--run-id ID` 指定，续跑用 `--resume` 开关；所有命令都支持 `--dry-run`。

### 4.7 模块间调用签名表（唯一登记处，B-17）

各 PRD 只能**引用**本表签名；改签名需要先改本表。

| 调用方 → 被调方 | 签名 | 说明 |
|---|---|---|
| MOD-02 → MOD-01 | `GameEngine(tables).create(cfg) / reduce(state, action) / legal_actions(state) / observe(state, view)` | GAME-01 §1 的 Python 等价；`tables` 读取 `src/game/content/*.json` |
| MOD-06 → MOD-04 | `PolicyAgent.reset(ctx: EpisodeContext) -> None` | 每局开始 |
| MOD-06 → MOD-04 | `PolicyAgent.play_decision(env: SandboxEnv, obs: Observation, *, ctx: EpisodeContext) -> DecisionResult` | 执行一个决策点（含计划、重试、fallback），返回 `steps: list[StepRecord]`、`source`、`attempts`、`llm: LLMCallMeta\|None`、`memory_ids`、`adopted_memory_ids`、`prompt_hash`、`last: StepResult` |
| MOD-04 → MOD-03 | `InferenceClient.generate_json(prompt, *, schema, mode, sampling, seed) -> JsonResult` | `mode ∈ train/eval` |
| MOD-04 → MOD-05 | `MemoryStore.retrieve(obs, *, top_k, token_budget, tick: int) -> list[MemoryHit]` | `tick` 为逻辑时钟（训练局序号），不用墙钟 |
| MOD-06 → MOD-05 | `MemoryStore.add_reflection(entry: ReflectionEntry, *, provenance: Provenance) -> str` · `record_usage(episode_key, memory_ids, outcome, *, adopted_ids, tick) -> None` · `snapshot(version: int) -> Path` · `frozen(version) -> MemoryView` | 评估局只用 `frozen` 只读视图 |
| MOD-06 → MOD-03 | `loop.reflect.reflect(summary: EpisodeSummary, *, client, template) -> ReflectionEntry` | 反思归 MOD-06；`ReflectionEntry` schema 归 MOD-05 |
| MOD-07 → MOD-08 | `evals.evaluate_arm(harness_dir, *, split, basket, memory_version, arm, run_id) -> ArmResult` | 单臂评估 |
| MOD-07 → MOD-08 | `evals.compare_paired(a: ArmResult, b: ArmResult) -> PairedTest` | 方法与显著性口径归 MOD-08 |
| MOD-07 → MOD-08 | `evals.evaluate_generation(gen, parent: ArmResult, winner: ArmResult\|None, *, run_id) -> dict` | 产出 report 指标段 |
| MOD-06/07 → MOD-08 | `evals.seeds.take(split, *, generation=None, basket=None) -> list[int]` | seed 唯一出口（INV-5） |
| MOD-07 → MOD-02 | `sandbox.guard.gate(harness_dir) -> GateReport` · `sandbox.guard.load_harness_tools(harness_dir) -> ToolWorker` | 子进程 worker |
| MOD-04/05/06 → MOD-02 | `sandbox.audit.scan_text(text, *, surface) -> list[Finding]` | prompt/memory/reflection 泄漏扫描 |
| MOD-04/08 → MOD-04 | `agent.heuristic.heuristic_policy(obs, legal: list[Action], hints: dict) -> Action`（`hints` 来自 `SandboxEnv.hints()`） | fallback 与 heuristic 基线共用，逻辑移植自 GAME `scripts/bots.js::greedy_counter` |
| MOD-07 → MOD-09 | `git_ops.commit_harness(repo, version, msg) -> sha` · `tag_version(repo, version)` · `rollback_to(repo, version) -> sha` | tag 为 `harness-v{N}` |
| 全部 → MOD-09 | `infra.events.emit(event: str, **fields)` · `infra.errors.E.<CODE>` · `infra.io.write_json_atomic(path, obj)` | 注册表见 §8.2/§8.3 |

---

## 5. 目录与配置契约

```
gamer_rsi/
  env/        engine.py rng.py digest.py loader.py combat.py map.py shop.py forge.py event.py relic.py  # MOD-01（与 src/game/kernel 一一对应）
  sandbox/    api.py projection.py guard.py audit.py honeypot.py  # MOD-02
  inference/  backend.py mlx_backend.py llama_backend.py schema.py # MOD-03
  agent/      policy.py parser.py heuristic.py prompts/ tools/  # MOD-04
  memory/     store.py retriever.py snapshot.py schemas.py  # MOD-05（snapshot.py：memory_v{N}.jsonl 读写与回滚，B-35）
  loop/       runner.py reflect.py generation.py          # MOD-06
  evolve/     proposer.py judge.py versioning.py gate.py  # MOD-07
  evals/      baselines.py harness.py metrics.py diversity.py ablation.py # MOD-08
  infra/      config.py logging.py events.py errors.py io.py cli.py plotting.py git_ops.py guards.py # MOD-09
schemas/     decision.schema.json trajectory_line.schema.json generation_report.schema.json  # 跨模块 schema（MOD-09 登记）
harness/     （当前生效工件，见 §4.4）
harness/versions/v{N}/   （历史工件快照，由 MOD-07 维护）
configs/     default.yaml seeds.yaml models.yaml
runs/        <run_id>/{config.json,state.json,events.jsonl,episodes/<split>/,reflections/,prompts/,memory/,
                       generations/,metrics/,plots/,audit/}
             metrics/ 内含 generations.csv（MOD-08 列定义）、scoreboard.json（MOD-06 计分台账）、
                      llm_calls.jsonl（MOD-03 推理埋点）、plots_data/（MOD-08 绘图数据）
tests/       unit/ integration/ golden/
docs/        模块拆分Spec-GameRSI.md  prd/  README.md
```

**配置优先级（B-26）**：CLI 参数 > 环境变量 `GAMERSI_*` > `harness/config.json`（可演化层）> `configs/default.yaml` 默认值。
- `harness/config.json` 只能包含白名单键：`policy.*` 的训练期采样参数、`memory.retrieve.*`、`agent.tools.*`。它不得包含 `episode.*`、`seeds.*`、`loop.*`、`evolve.*` 等静态键，也不得覆盖 §8.1 的评估温度；违规时 MOD-07 在 C3 门禁拒绝，MOD-09 加载时抛 `E_IO_CONFIG_INVALID`。
- 这一层属于 harness，因此计入 `artifact_hash`、不计入 `config_hash`（B-19）。
- **环境变量命名（B-30）**：嵌套键用 `__` 分隔，如 `GAMERSI_MEMORY_RETRIEVE__TOP_K=8` 对应 `memory.retrieve.top_k`。
**run_id** = `{git_short_sha}-{config_hash[:8]}-{utc_ts}`。`config_hash` 只覆盖**静态配置**（configs + EpisodeConfig），用于判定 run 身份与 `--resume` 一致性；harness 的变化由逐代记录的 `artifact_hash` 表达（B-19）。否则第一代被接受后 `--resume` 必然冲突。
**内容配表**：MOD-01 直接读取 `src/game/content/*.json`，与 JS 内核共用同一份，不另建 `configs/enemies.yaml`。
**忽略规则**：`runs/` 必须写进 `.gitignore`，否则 MOD-07 的 `worktree_clean_check()` 永远判定为 dirty。

---

## 6. 依赖矩阵与开发顺序

| 模块 | 依赖 | 可并行于 | 阻塞谁 |
|---|---|---|---|
| MOD-01 | GAME 内核与配表（已完成，`src/game`） | MOD-09 起步 | MOD-02/06/08 |
| MOD-02 | MOD-01 | MOD-03 | MOD-04/06/08 |
| MOD-03 | MOD-09 | MOD-01/02 | MOD-04/06 |
| MOD-04 | MOD-02/03/05 | MOD-08 | MOD-06/07 |
| MOD-05 | MOD-04（schema） | MOD-08 | MOD-06/07 |
| MOD-06 | MOD-01..05 | MOD-09 | MOD-07 |
| MOD-07 | MOD-06/08/09 | — | M2/M3 |
| MOD-08 | MOD-01/02/09 | MOD-05 | MOD-06/07 |
| MOD-09 | — | 全部 | 全部（横切） |

**关键路径**：MOD-01 → MOD-02 → MOD-03/04 → MOD-06 → MOD-07 → M3 验收。
**并行机会**：MOD-03 与 MOD-01/02 同时做；MOD-08 与 MOD-04/05 同时做；MOD-09 第一天启动。

---

## 7. 里程碑到模块的映射

| 里程碑 | 覆盖模块 | 交付物 | 验收（源自计划 §7） |
|---|---|---|---|
| M0 | MOD-01、MOD-02、MOD-09（骨架） | Python 移植 + 观测投影 + 审计器 + 单测 | `src/game/tests/golden` 5 组逐行对拍全过（g1–g5，见 [M0 验收报告](reviews/2026-10-03-M0验收报告.md)）；同一 seed 两次运行 `state_hash` 序列完全一致；Python 版 heuristic 在默认局（§8.6）上的通关率与 JS `greedy_counter` 相差 ≤ 3pp（n=200） |
| M1 | MOD-03、MOD-04、MOD-08（基线部分） | 8B agent + random/heuristic/fixed-prompt 三基线；推理后端只交付 `MLXBackend` + `MockBackend`，`LlamaCppBackend` 与阶段二 30B 同期上线，但 `Backend` 契约与一致性套件在 M1 定型（B-31） | 三个基线的 held-out 分数入档；JSON 格式鲁棒性报告 |
| M2 | MOD-05、MOD-06、MOD-07（初版） | 反思 + 记忆 + K 局自动演化 + HITL 门 | 闭环跑通 ≥1 轮演化，产出 diff 与决策记录 |
| M3 | MOD-08（完整）、MOD-07（回滚/消融） | 评估报告 + 曲线图 + 消融结论 | 计划 §6 三项验收全部通过 |
| M4（可选） | MOD-10 | STaR 筛选 + LoRA 新基座 | 跨代增益 vs 推理时演化增益对比 |

---

## 8. 横切关注点

### 8.1 确定性
所有随机都来自显式 seed，并设 `PYTHONHASHSEED=0`。禁止使用 `random.*`、基于时间的随机，以及未显式设 seed 的 `torch`/`numpy` 全局随机；MOD-09 `guards.py` 在 CI 中用 AST 扫描检查。

**评估局统一 `temperature=0.0`**，并且 seed 显式，不可被 harness `config.json` 覆盖。训练局温度由 harness 配置决定（上限 1.0）。MOD-03 在 `mode="eval"` 下强制执行（B-14）。

记忆、TTL 与时新度一律使用**逻辑时钟**（训练局序号 / 代数），不使用墙钟。

### 8.2 可观测性与事件注册
- 一次调用对应一行 JSONL。事件统一落到 `runs/<run_id>/events.jsonl`，事件名为 `<module>.<event>`（如 `loop.episode_end`、`evolve.decision`、`sandbox.gate_reject`）。
- 事件注册表位于 MOD-09 `infra/events.py::EVENT_REGISTRY`，由各 PRD「指标与埋点」节列出的事件汇总而成。未注册的事件在开发模式下抛异常，生产模式下写成 `infra.unknown_event`。
- 任何模块不得自建日志格式或自选落点（B-18）。

### 8.3 错误码与退出码
- 错误码前缀为 `E_ENV_*`、`E_ACTION_*`、`E_LLM_*`、`E_EVOLVE_*`、`E_EVAL_*`、`E_IO_*`，全集在 MOD-09 `infra/errors.py` 登记；各 PRD 只能引用已登记的码。
- 命名冲突已统一：
  - LLM 错误以 MOD-03 为准：`E_LLM_UNAVAILABLE / E_LLM_BACKEND_ERROR / E_LLM_SCHEMA_INVALID / E_LLM_TIMEOUT / E_LLM_OOM`。
  - 泄漏统一为 `E_EVAL_INV5_LEAK`。
  - 记忆写入失败统一为 `E_IO_MEMORY_WRITE_FAILED`。
- 退出码：

  | 码 | 含义 |
  |---|---|
  | 0 | 成功，含 HITL 挂起 |
  | 1 | 领域失败，如验收不达标 |
  | 2 | 配置/环境错误 |
  | 3 | 模型不可用 |
  | 4 | 沙箱/守卫违例 |
  | 5 | IO 落盘失败 |
  | 130 | Ctrl-C |

### 8.4 版本与审计
- `harness_version` 单调递增，历史不可重写；每次接受/拒绝都要有 `report.json` 支撑。
- 轨迹保留 ≥ 全部代际。
- redaction 只作用于 prompt/memory/reflection 三个出口，审计侧轨迹保留原值（B-19）。

### 8.5 降级
- 模型不可用 → 退化为 heuristic agent 并显眼标注，该局 `degraded=true`，不计入评估样本；但 MOD-08 必须单独报告 `degraded_n`。
- 演化裁判失败 → 保守拒绝候选。
- 超时、崩溃造成的 dropped 局**计为 lose 并单列**，不得从 n 中剔除，避免幸存者偏差。

### 8.6 「一局」的定义与成本模型（B-12/B-13）

**默认局**：`EpisodeConfig(difficulty="hard", stage_count=6, toggles=全开, character="warrior")`，即第 1–2 章，以第 6 关 Boss 收尾。

**复现命令（唯一登记处，M0 验收 F5）**：下表每一行都由同一条命令产出，**seed 区间是数字身份的一部分**：

```bash
node src/game/tests/bot-match.js 200 <stageCount> <difficulty> 1 --stats   # n=200，seed 1–200
```

口径：通关率 = `outcome=="win"` 占比；env 步 = 每成功 `reduce` 计 1 步；决策点 = `decisionIndex` 终值（Spec §4.3 / B-12 规则，与 MOD-02 `SandboxEnv` 同口径，由 `tests/golden/test_bot_match.py::test_js_decisions_match_sandboxenv_decision_index` 守卫）。实测（2026-10-03，`greedy_counter`，n=200，seed 1–200）：

| 局型 | greedy_counter 通关率 | env 步 p50/p90 | 决策点 p50/p90 |
|---|---|---|---|
| hard × 6 关（**默认**） | 45.5% | 232 / 284 | 96 / 116 |
| medium × 10 关（完整局） | 24.0% | 397 / 535 | 146 / 187 |
| easy × 3 关 | 100.0%（天花板，不用） | 105 / 124 | 38 / 43 |
| medium × 3 关 | 100.0%（天花板，不用） | 102 / 121 | 37 / 41 |
| hard × 3 关 | 97.0%（天花板，不用） | 91 / 114 | 38 / 44 |

> 换 seed 集数字会变（n=200 的 95% CI 约 ±6pp）：medium × 10 关在 seed 10000–10199 上是 35.5%（`src/game/docs/BALANCE.md` 的口径，`node scripts/balance.js --n 200 --strategy all --duel`，与 10 关默认局不同源）。引用本表时必须带上 seed 区间。

默认局的启发式通关率不在天花板（3 关局 97–100%），上升和下降都有空间可测，决策点数量约为完整局的三分之二。完整局（medium × 10）只作为 test 的次级「迁移」指标报告。

**上限**（唯一配置键，各模块只读）：

| 键 | 默认 | 说明 |
|---|---|---|
| `episode.max_env_steps` | 1000 | env 步硬上限（n=200、seed 1–200 实测最大：6 关 360，10 关 808） |
| `episode.max_decisions` | 300 | 决策点上限（同口径实测最大：6 关 149，10 关 250） |
| GAME `maxTurnsPerBattle` | 60 | **单场战斗**回合上限（内核自带，不是整局上限） |

超出上限 → `truncated=true`，计为 lose。

**成本模型**：设 `t_d` 为单个决策的推理耗时（MOD-03 实测 p50），`D` 为每局决策数（默认局实测 p50 = 96）。

> **2026-10-04 实测回填（M1 验收，run `m1-real-rob` / `m1-real-bench2`）**：8B 权重已落盘（`mlx-community/Qwen3-8B-4bit`，`weights_sha256=20303070…`），在 **200 条真实 MOD-04 决策 prompt** 上实测：
> `t_d` **p50 = 5.687 s**、p95 = 11.703 s；预填 p50 **2134 token**（原估 1.5k）、输出 p50 **35 token**（原估 150）；`tok_per_s` 12.0；
> 常驻显存 **4.6077 GB**（`resident_gb_est` 5.6 → 已回填）；首次尝试通过率 94%，故含重试的**有效** `t_d_eff ≈ 5.687 × 1.065 ≈ 6.06 s`。
> 原「预估约 3 s」**低估 1.9×** —— 这正是 [排期 §6](项目排期-GameRSI.md) 的首要风险，实跑耗时应按下表放大。

- 单局耗时 `t_ep ≈ D·t_d + t_reflect`：按实测 `t_d = 5.687 s` → 96 × 5.687 ≈ **546 s ≈ 9.1 分钟**（含重试 ≈ 9.7 分钟）；原估 5 分钟是 t_d=3 s 的产物。
- 每代局数 `E_gen = K + (H + S) + C·(S + H)`：训练 K=30；parent 的 heldout H=40 与冒烟 S=5（与候选使用同一个冻结的记忆快照，所以每代都要重跑）；候选数 C=**2**（上限 3），每个候选也跑冒烟 S 与 heldout H。默认值下 E_gen = 30 + 45 + 2×45 = **165 局**；按实测 → 165 × 546 s ≈ **25.0 小时/代**（原估 14 h）。
- M3 全程为 3 代，加上 gen-000 基线 40 局、fixed_prompt 基线 40 局、消融 2 臂 × 40 局、test 100 局：按实测线性放大 **≈ 114 小时（4.7 天）连续推理**（原估 60 h ≈ 2.5 天），含重试约 **121 小时（5.0 天）**。
- **衍生硬约束**：`inference.total_timeout_ms` 由 8000 ms 回填为 **120000 ms**（原值 < 实测 p50，正常决策会被判 `E_LLM_TIMEOUT`）。`evals.seed_timeout_s` 同时按实测单局 ≈546 s 把默认值由 300 s 调到 **1800 s**，但**该超时目前并未被真正执行**：`SandboxRunner.run_episode()` 收下 `timeout_s` 后从不消费，`run_eval` 也没有 `future.result(timeout=…)`，所以长跑中一个卡住的 worker 会让整个 eval 永久挂起（M1 验收 F20）——§8.5「超时/崩溃造成的 dropped 局计为 lose 并单列」在实现落地前是空转的。
- **若要把 M3 实跑压回 2.5 天**：优先按排期 §6 下调 C（候选数 2→1）与 S（冒烟 5→2），或改 K=30→15；`H=40` 与 test=100 属统计功效底线，不建议动。
- **⚠️ 口径警告（2026-10-04 实测，M1 验收 F22）**：`t_ep ≈ D·t_d` 只对**按决策点推理**的 agent 成立（MOD-04 `PolicyAgent` 一次 `play_decision()` 执行整个 plan）。MOD-08 的三个基线走的是 `SandboxRunner._agent_loop`，**每推进一个 env 步就问一次模型**：实测 `fixed_prompt` 的 env 步/决策点 = **3.1**，单局平均 **245 次推理**（决策点口径只需 ~79 次）。因此上表中**任何含基线的行**（gen-000 基线 40 局、fixed_prompt 基线 40 局、消融 2 臂）在 F22 修复前应按 **×3.1** 计算：`fixed_prompt` 40 局实测约 **12.3 h**（原估 4 h）。

**硬约束**：
- `E_gen` 由 MOD-06 按上式统一计算，超出 `loop.max_episodes_per_generation`（默认 170）即拒绝启动。MOD-07 不再单独设成本上限。
- 并发一律采用 P-10 的「局级并发 + 共享串行推理队列」，不得每个 worker 各载一份模型。
- 任何声称「n 局在 x 分钟内完成」的 NFR 都必须由本式推出。

---

## 9. 质量门（每个模块 PRD 的 DoD 基线）

| 门 | 要求 |
|---|---|
| G1 单测 | 核心路径单测覆盖；纯函数模块（MOD-01/02/08）覆盖率 ≥80% |
| G2 Golden | MOD-01 逐行对拍 `src/game/tests/golden/*.jsonl`（JS 为基准，Python 不得自行生成替代快照）；MOD-02 有投影 golden；改动必须显式更新并说明 |
| G3 确定性 | 同 seed 重跑产生逐字节相同的 `obs_digest`/`state_hash` 序列 |
| G4 泄漏审计 | `audit leak` 在自测 run 上 0 告警；蜜罐注入必须被抓到 |
| G5 Schema 校验 | 所有跨模块 JSON 有 schema 定义 + round-trip 测试 |
| G6 可复现命令 | 模块 README/PRD 中的命令可直接复制执行成功 |

---

## 10. PRD 索引

| 模块 | 技术 PRD |
|---|---|
| MOD-01 env-rules | [prd/MOD-01-env-rules-PRD.md](prd/MOD-01-env-rules-PRD.md) |
| MOD-02 env-sandbox | [prd/MOD-02-env-sandbox-PRD.md](prd/MOD-02-env-sandbox-PRD.md) |
| MOD-03 inference | [prd/MOD-03-inference-PRD.md](prd/MOD-03-inference-PRD.md) |
| MOD-04 policy | [prd/MOD-04-policy-PRD.md](prd/MOD-04-policy-PRD.md) |
| MOD-05 memory | [prd/MOD-05-memory-PRD.md](prd/MOD-05-memory-PRD.md) |
| MOD-06 loop | [prd/MOD-06-loop-PRD.md](prd/MOD-06-loop-PRD.md) |
| MOD-07 evolve | [prd/MOD-07-evolve-PRD.md](prd/MOD-07-evolve-PRD.md) |
| MOD-08 evals | [prd/MOD-08-evals-PRD.md](prd/MOD-08-evals-PRD.md) |
| MOD-09 infra | [prd/MOD-09-infra-PRD.md](prd/MOD-09-infra-PRD.md) |

---

## 11. 待回填决策登记表

各模块 PRD 在 Spec 未覆盖处做出的决策集中登记在此。**「待裁决」条目是当前实现的临时依据**：实现时按此执行，但需在对应里程碑评审时裁决并回填正文（回填后状态改为「已回填」并升版本号）。

### 11.1 v1.1 已回填

| # | 提出方 | 决策 | 回填位置 |
|---|---|---|---|
| B-01 | MOD-03 | 增加**可选**辅助接口 `count_tokens()` / `embed()`，阶段一默认关闭，不支持时返回 `E_LLM_UNSUPPORTED` 由 MOD-05 降级为 BM25 | §3 MOD-03 |
| B-02 | MOD-06/08/09 | `runs/<run_id>/metrics/` 增补 `scoreboard.json`（MOD-06 计分台账）、`llm_calls.jsonl`（MOD-03）、`plots_data/`（MOD-08） | §5 目录 |
| B-03 | MOD-04 | 新增 `runs/<run_id>/prompts/` 存放决策 prompt 快照（审计复现用） | §5 目录 |
| B-04 | MOD-05 | 新增 `runs/<run_id>/memory/memory_v{N}.jsonl` 记忆快照 | §5 目录 |
| B-05 | MOD-07 | 新增 `harness/versions/v{N}/` 历史工件快照目录 | §5 目录 |
| B-06 | MOD-08 / MOD-09 | **`metrics/generations.csv` 的列定义归 MOD-08**（属指标口径）；MOD-09 只冻结文件路径并提供 `plot_generations()` 读取 | §5 目录 + MOD-09 §3 |
| B-07 | MOD-08 | **seed 协议区间**（唯一权威）：train `1000–1999`（第 g 代取 `1000+30(g-1) … +29`）、heldout `2000–2099` 分三篮子 H1=`2000–2039`（代际裁判 n=40）/ H2=`2040–2079`（消融与复核）/ H3=`2080–2099`（应急）、test `9000–9099`（锁定，每里程碑单次解锁）。**v1.1 已据此修正 MOD-04/06/07/09 与本文档的命令示例**（原先误用 `train:1-5`、`heldout:1001-1040`） | §4.6、INV-5 |
| B-08 | MOD-05 | 新增 `memory stats\|snapshot\|rollback\|verify\|retrieve` 子命令族 | §4.6 |

### 11.1b v1.3 已回填（2026-09-30 17:21，按 MOD PRD 评审修订，见 [reviews/2026-09-30-MOD-PRD评审.md](reviews/2026-09-30-MOD-PRD评审.md)）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-09 | P-28 | 游戏本体以 GAME-00/01/02 + `src/game` 为规则唯一真相；MOD-01 定位为纯 Python 等价移植，直接读取 `src/game/content` 配表 | §2、§3、§5、§6、§7 |
| B-10 | P-29 | agent 侧观测 `obs-agent-1.0.0` = GAME `obs-1.0.0` 的键子集 + `episodeId/decisionIndex/stageCount/lastResult`，保留 camelCase；动作即 GAME 动作对象，`abandon_run` 对 agent 不开放 | §4.1–4.3 |
| B-11 | P-30 | 难度经 `applyDifficulty` 展开；失心语义同 GAME（`virtue` 归零扣 1 心，心数归零才 lose） | §4.1、MOD-01 |
| B-12 | 评审 P0-1/2 | 决策点粒度：战斗内每回合产出一个计划，其余每步一次；默认局为 hard × 6 关（GAME D-34）；上限统一为 `episode.max_env_steps` / `episode.max_decisions` | §4.3、§8.6 |
| B-13 | 评审 P0-3 | 成本模型 `E_gen = K + (H+S) + C·(S+H)`，默认 C=2、每代 165 局、约 14 小时；局级并发 + 共享推理队列 | §8.6 |
| B-14 | 评审 P0-4 | 评估温度统一为 0.0 + 显式 seed，harness 不可覆盖 | §8.1 |
| B-15 | 评审 P0-5 | 三层校验唯一口径：L0 修复不计非法；每个决策最多 3 次尝试，失败走 fallback；只有 SandboxEnv 判负（连续 3 个 `final` 失败 → `policy_failure`） | §4.3 |
| B-16 | 评审 P1-5/6/10 | 轨迹路径 `episodes/<split>/<episode_key>.jsonl`；`gen-000` 为基线；report 用唯一 schema 文件，写入分工为 MOD-08/07/09 | §4.5 |
| B-17 | 评审 P1-1/2/3/16 | 模块间调用签名表 | §4.7 |
| B-18 | 评审 P1-11/12/13/14 | 事件统一为 `<module>.<event>` 并落到 `events.jsonl`；错误码/退出码/CLI 集中登记 | §4.6、§8.2、§8.3 |
| B-19 | 评审 P0-7 | `config_hash` 只含静态配置，harness 变化用 `artifact_hash` 表达；redaction 不作用于轨迹 | §4.4、§5、§8.4 |
| B-20 | 评审 P0-6 | harness 工具在常驻子进程 worker 中执行，rlimit 设在子进程上 | §4.4 |
| B-21 | 评审 P2 | dropped 局计为 lose 并单列；记忆与 TTL 使用逻辑时钟 | §8.1、§8.5 |
| B-22 | 评审 P1-15 | git 只打 tag `harness-v{N}`，不建同名分支 | §4.4 |

**v1.3 结案的待裁决条目**：

| # | 结论 |
|---|---|
| P-02 | 采纳，并扩展 `final` 参数（§4.1/§4.3，B-15） |
| P-04 | 采纳，字段已写入 §4.5 轨迹行 |
| P-08 | 被 B-10 取代：`shop` 在 `phase=shop` 时随观测给出陈列 |
| P-09 | 采纳，并要求 schema 扁平化以便 GBNF 可用（§4.3） |
| P-10 | 采纳，并提升为 §8.6 硬约束 |
| P-12 | 采纳（§4.5 `episode_end`、§8.5） |
| P-13 | 被 B-16 的 `episode_key` 取代 |
| P-14 | 采纳，统一出口为 `evals.seeds.take`（§4.7） |
| P-15 | 采纳 |
| P-16 | 被 P-10/§8.6 取代：不再用进程池各载一份模型 |
| P-19 | 采纳（§4.5/§4.6） |
| P-20 | 采纳（§4.6） |
| P-21 | 被 GAME-01 §7 P-8 取代：规范化 JSON 以 JS `stableStringify` 为准（`ensure_ascii=True`） |
| P-22 | 被 GAME-01 §3 取代：派生算法为 xmur3 + SplitMix64（禁 hashlib），流名以 GAME `STREAM_NAMES` 为准 |
| P-23 | 被 GAME 取代：DSL 同 GAME-00 §4.3；不建 `configs/enemies.yaml`，配表读 `src/game/content` |
| P-25 | 采纳；C5 改为 `illegal_rate ≤ 0.05`、`fallback_rate ≤ 0.10`、`truncation_rate ≤ 0.05`（不再用回合数）；M3 另需 ≥1 次 accept |
| P-28 | 采纳，即 B-09 |
| P-29 | 采纳，即 B-10/B-12 |
| P-30 | 采纳，即 B-11 |

### 11.1c v1.4 已回填（2026-09-30 17:49，11 项待裁决全部确认，另收录 PRD 内 3 项未登记决策）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-23 | P-01（MOD-04） | `Action` 定义在 `env/actions.py`，由 `sandbox/api.py` re-export，L1+ 只许从门面导入 | §1 实施细则 |
| B-24 | P-06（MOD-02） | 全状态只由 `SandboxEnv` 私有持有，`gamer_rsi.env` 对 L1+ 不可导入（原 `build_full_state()` 方案按 MOD-01 v1.0 改为等价封装） | §1 实施细则 |
| B-25 | P-07（MOD-02） | 审计器只做黑名单扫描 + canary 探针，不做正向归因 | §1 实施细则 |
| B-26 | P-03（MOD-04） | 配置优先级 CLI > env > `harness/config.json` > default.yaml；harness 层只含白名单键、不得覆盖评估温度 | §5 |
| B-27 | P-05（MOD-04） | `agent/prompts/task.md` 是 INV-3 的第 ④ 类上下文，不属演化对象 | §1 INV-3 |
| B-28 | P-11（MOD-03） | 修复 prompt 由 `inference/schema.py` 独占 | §4.3 |
| B-29 | P-17（MOD-07） | `harness/versions/v{N}/` 只放不可变快照；候选、patches、gate.json 放在 `runs/<run_id>/generations/gen-{NNN}/`（按 MOD-07 v0.2 实际设计调整了 v1.2 的登记文字） | §4.4 |
| B-30 | P-18（MOD-05） | 环境变量嵌套键用 `__`；中文分词用字符 unigram/bigram；去重阈值 `sim≥0.85` 且 `situation_key` 相同（后两项属于 MOD-05 内部口径） | §5、MOD-05 |
| B-31 | P-24（MOD-03） | M1 只交付 `MLXBackend` + `MockBackend` | §7 M1 |
| B-32 | P-26（MOD-07） | HITL 挂起写 gate.json(pending) 并以退出码 0 结束；超时 24h 保守回滚 | §4.5 |
| B-33 | P-27（MOD-07） | `version.json.base_model` 预留给 MOD-10，M0–M3 恒为 null | §4.4 |
| B-34 | MOD-08 §6 | 报告数值统一保留 4 位小数 | §4.5 |
| B-35 | MOD-05 §3 | 新增 `memory/snapshot.py` | §5 |
| B-36 | MOD-06 §7 | seed 只能经 `evals.seeds.take` 取用，MOD-06 只校验 split 标签（与 P-14 结论一致） | §4.7 |

### 11.2 待裁决

（无。v1.4 起，新的 PRD 临时决策登记到这里，编号从 P-31 开始。）

**裁决原则**：涉及 **INV-1/INV-2/INV-5** 的条目优先级最高，必须在所属里程碑开工前裁决。**B-07（seed 协议）已裁决并全量对齐**，后续任何 seed 区间引用必须回查 MOD-08 PRD §4。

---

## 12. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版：9 模块 + MOD-10 预留、7 条不变量、全局契约、质量门 |
| v1.1 | 2026-09-30 | 配套 9 份模块 PRD 产出；回填 8 项跨模块契约（B-01..B-08，含 seed 协议全量对齐与 `generations.csv` 归属裁定）；新增 §11 待回填决策登记表（27 项待裁决） |
| v1.2 | 2026-09-30 | 新增 GAME 系列 PRD（游戏本体：GAME-00/01/02，见 [docs/README.md](README.md)）；登记 P-28..P-30：游戏本体改为前后端分离网页实现（MOD-11 待立项，MOD-01 转为纯 Python 等价移植）、动作集统一与新增、难度配置化与失心粒度上移 |
| v1.3 | 2026-09-30 17:21 | 按 [MOD PRD 评审](reviews/2026-09-30-MOD-PRD评审.md) 修订：裁决 P-28/29/30 并结案 19 项待裁决条目；§4 全面对齐 GAME（obs-agent-1.0.0、决策计划、三层校验唯一口径、轨迹路径与 report 写入分工）；新增 §4.7 模块间调用签名表；§8 拆分为 8.1–8.6，新增「一局」定义与成本模型；INV-7 主指标改为 outcome/stages_cleared/hearts_left/battles_won；新登记 B-09..B-22 |
| v1.4 | 2026-09-30 17:49 | 确认 v1.3 全部裁决；剩余 11 项待裁决全部确认并回填正文（B-23..B-33），另收录 3 项 PRD 内未登记决策（B-34..B-36）；新增 §1「不变量实施细则」；P-06、P-17 的登记文字按 v1.3 后的实际设计调整；§11.2 清零 |
