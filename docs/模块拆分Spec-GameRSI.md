# GameRSI 模块拆分 Spec

> 版本：v1.1 · 状态：定稿（2026-09-30）· 定位：**模块边界与接口的唯一事实来源（Single Source of Truth）**
> 上游：[项目计划-GameRSI.md](../项目计划-GameRSI.md) · [RSI调研纪要.md](../RSI调研纪要.md)
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
| INV-3 | 进入 prompt 的上下文只允许：当前 Observation + 检索到的 memory + 当前 harness 工件 + 任务说明 | MOD-04 |
| INV-4 | harness 自编辑产出的代码在受限执行环境运行：无网络、无文件写、无 subprocess、无 env 内部访问 | MOD-02 + MOD-07 |
| INV-5 | train / heldout / test 三类 seed 严格隔离；heldout 与 test 的 seed 值不得进入任何 prompt 或记忆 | MOD-08 + MOD-09 |
| INV-6 | 所有随机性来自显式 seed（`env`、采样、演化），禁用全局 `random`/时间相关随机 | MOD-01 + MOD-09 |
| INV-7 | 评估主指标只用**环境原生结果**（胜/负、剩余 HP、击杀、楼层），shaped reward 仅用于学习与调试 | MOD-08 |

---

## 2. 模块清单

| ID | 模块 | 层 | 一句话职责 | 里程碑 | 关键依赖 |
|---|---|---|---|---|---|
| MOD-01 | env-rules | L0 | 纯 Python《Dicey Dungeons》简化规则引擎 + seed 确定性 | M0 | — |
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
- **负责**：游戏规则状态机（骰子、装备槽、奇偶/大小限制、敌人意图、伤害结算）、关卡/商店/升级的最小实现、`reset/step`、奖励计算、非法动作判定、`full_state_hash()`。
- **不负责**：观测裁剪（MOD-02）、prompt（MOD-04）、模型调用（MOD-03）、评分与统计（MOD-08）。

### MOD-02 env-sandbox
- **负责**：`FullState → Observation` 白名单投影；`SandboxEnv` 门面（唯一的 agent 侧入口）；harness 代码受限执行与静态扫描；轨迹泄漏审计器；蜜罐泄漏注入与告警。
- **不负责**：规则本身（MOD-01）、奖励函数（MOD-01）、模型侧重试（MOD-03）。

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
- **负责**：三类 seed 协议与 held-out 评估执行、基线（random / heuristic / fixed-prompt）实现与归档、指标计算（主指标 + 次级指标 + 置信区间）、多样性/坍塌监控、训练-留出增益真实性判定、消融实验（去反思 / 去自编辑）。
- **不负责**：修改 harness（MOD-07）、写游戏规则（MOD-01）、编排训练局（MOD-06）。

### MOD-09 infra
- **负责**：配置加载与优先级、结构化日志与 JSONL 轨迹落地、`run_id`/`harness_version`/配置哈希、CLI 入口、绘图、git 操作封装、确定性守卫（禁全局随机、固定 PYTHONHASHSEED）、import 门禁检查。
- **不负责**：任何领域逻辑（不得在 infra 里写游戏或评估规则）。

---

## 4. 全局接口契约

### 4.1 环境接口（MOD-01/02 对外）

```python
# gamer_rsi/sandbox/api.py
class SandboxEnv:
    def reset(self, seed: int, *, episode_config: EpisodeConfig) -> Observation: ...
    def step(self, action: Action) -> StepResult: ...
    def observe(self) -> Observation: ...
    def legal_actions(self) -> list[Action]: ...   # 公开信息足以推出的合法动作，可含噪声提示
    def close(self) -> None: ...

@dataclass(frozen=True)
class StepResult:
    observation: Observation
    reward: float              # shaped，仅供学习/调试
    terminated: bool           # 胜负已分
    truncated: bool            # 超时/步数上限
    info: dict                 # {"illegal": bool, "illegal_reason": str|None,
                               #  "outcome": "win"|"lose"|None, "floors": int,
                               #  "kills": int, "state_hash": str}
```

### 4.2 Observation（白名单，MOD-02 唯一权威定义）

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 投影协议版本，变更需 +1 并记 CHANGELOG |
| `episode_id` / `turn` | str / int | 标识与回合 |
| `phase` | enum | `player_turn` / `enemy_turn` / `shop` / `levelup` / `game_over` |
| `self_hp` / `self_max_hp` / `self_block` | int | 自身状态 |
| `dice` | list[int] | **已掷出**的骰面；未掷出时为空 |
| `slots` | list[SlotView] | `{slot_id, kind, constraint, filled_by, used}` |
| `enemies` | list[EnemyView] | `{enemy_id, hp, max_hp, status, intent}` |
| `floor` | int | 当前楼层 |
| `allowed_actions` | list[str] | 合法动作 **kind** 摘要，不含隐藏信息 |

**严禁出现在 Observation 中**：seed、后续房间/遭遇、商店与升级台内容、敌人未公开状态、模拟器内部随机数状态、未来骰面、`full_state`。这些只能存在于审计侧（`state_hash` 是单向摘要，允许）。

### 4.3 动作契约（MOD-04 产出，MOD-01 消费）

模型必须输出：

```json
{"reasoning": "≤2 句", "action": {"kind": "place_die", "die_index": 0, "slot_id": "sword_1"}}
```

`kind` 枚举与必填字段：

| kind | 必填 | 说明 |
|---|---|---|
| `place_die` | `die_index`, `slot_id` | 单颗骰入单个槽（§2 约束：单次改动 ≤2 模块） |
| `end_turn` | — | 主动结束回合 |
| `use_ability` | `ability_id` | 使用特殊能力 |
| `shop_buy` | `item_id` | 商店购买 |
| `shop_leave` | — | 离开商店 |
| `levelup_choose` | `option_id` | 升级选择 |

**三级校验**：① 语法（JSON 可解析 + schema）→ ② 语义（落在 `legal_actions()` 内）→ ③ 执行。
**失败策略**：前两级失败不执行动作，返回带 `illegal_reason` 的 Observation + `reward -= 0.05`，计入 `illegal_rate`；同一步最多重试 `R=2` 次；连续 3 次非法判该局失败（防死循环）。

### 4.4 Harness 工件契约（MOD-07 写，MOD-04 读）

```
harness/
  version.json          # {"version": int, "parent": int|null, "git_commit": str,
                        #  "created_at": iso, "reason": str, "accepted_by": "auto"|"human"}
  prompts/decision.md   # 决策 prompt 模板（含 {{observation}} {{memory}} 占位）
  prompts/reflection.md # 反思模板
  tools/*.py            # 决策工具函数，纯函数 + @harness_tool 注册
  config.json           # 采样参数、检索 top_k 等可演化配置
```

工具函数唯一签名（**禁止变更**）：

```python
@harness_tool
def rank_actions(obs: dict, candidates: list[dict], memory: list[dict]) -> list[dict]:
    """返回重排后的 candidates；纯函数，无 IO、无网络、无随机、无 import env。"""
```

### 4.5 轨迹与报告契约（MOD-09 落地，MOD-08 消费）

```jsonc
// runs/<run_id>/episodes/<seed>.jsonl —— 每步一行
{"episode_id":"e-000123","generation":3,"harness_version":7,"seed":1042,"split":"train",
 "step":14,"obs_digest":"sha256:...","action":{...},"reward":0.03,"cum_reward":0.41,
 "illegal":false,"illegal_reason":null,"llm_raw":"...","llm_latency_ms":812,
 "prompt_tokens":932,"completion_tokens":61,"ts":"2026-09-30T12:00:00Z"}
```

```jsonc
// runs/<run_id>/generations/gen-003/report.json —— 代际报告
{"generation":3,"harness_version":7,"parent_version":6,
 "heldout":{"n":40,"win_rate":0.32,"win_rate_ci95":[0.20,0.47],"mean_hp_left":9.4,
            "mean_kills":2.8,"mean_turns":31.2,"illegal_rate":0.011,
            "action_entropy_curve":[...]},
 "train":{"n":60,"win_rate":0.51},
 "gain_realism":{"win_rate_gap":0.19,"gap_threshold":0.15,"verdict":"overfit_suspect"},
 "diversity":{"entropy_slope":-0.004,"collapse_flag":false},
 "ablation":{"no_reflection":0.18,"no_self_edit":0.22},
 "decision":"reject","decided_by":"auto","reason":"heldout 未提升且 gap 超阈"}
```

### 4.6 命令面（MOD-09 统一暴露）

```bash
python -m gamer_rsi env selftest --seed 1              # M0 验收：规则/确定性自检
python -m gamer_rsi play --agent fixed --seeds train:1000-1004
python -m gamer_rsi baseline --kind random|heuristic|fixed_prompt
python -m gamer_rsi loop run --generations 3 --k 30 --hitl        # HITL=人在环确认
python -m gamer_rsi evolve approve|rollback --generation 3
python -m gamer_rsi eval report --split heldout --baseline all
python -m gamer_rsi audit leak --run <run_id>          # INV-2/INV-3 审计
python -m gamer_rsi eval diversity --run <run_id>
```

---

## 5. 目录与配置契约

```
gamer_rsi/
  env/        engine.py models.py rng.py actions.py      # MOD-01
  sandbox/    api.py projection.py guard.py audit.py honeypot.py  # MOD-02
  inference/  backend.py mlx_backend.py llama_backend.py schema.py # MOD-03
  agent/      policy.py parser.py prompts/ tools/         # MOD-04
  memory/     store.py retriever.py schemas.py            # MOD-05
  loop/       runner.py reflect.py generation.py          # MOD-06
  evolve/     proposer.py judge.py versioning.py gate.py  # MOD-07
  evals/      baselines.py harness.py metrics.py diversity.py ablation.py # MOD-08
  infra/      config.py logging.py cli.py plotting.py git_ops.py guards.py # MOD-09
harness/     （当前生效工件，见 §4.4）
harness/versions/v{N}/   （历史工件快照，由 MOD-07 维护）
configs/     default.yaml seeds.yaml models.yaml
runs/        <run_id>/{config.json,state.json,episodes/,reflections/,prompts/,memory/,
                       generations/,metrics/,plots/,audit/}
             metrics/ 内含 generations.csv（MOD-08 列定义）、scoreboard.json（MOD-06 计分台账）、
                      llm_calls.jsonl（MOD-03 推理埋点）、plots_data/（MOD-08 绘图数据）
tests/       unit/ integration/ golden/
docs/        模块拆分Spec-GameRSI.md  prd/  README.md
```

**配置优先级**：CLI 参数 > 环境变量 `GAMERSI_*` > `configs/default.yaml` 默认值。
**run_id** = `{git_short_sha}-{config_hash[:8]}-{utc_ts}`；`config_hash` 覆盖 configs + harness_version，保证可复现。

---

## 6. 依赖矩阵与开发顺序

| 模块 | 依赖 | 可并行于 | 阻塞谁 |
|---|---|---|---|
| MOD-01 | — | MOD-09 起步 | MOD-02/06/08 |
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
| M0 | MOD-01、MOD-02、MOD-09（骨架） | 可复现模拟器 + 观测投影 + 审计器 + 单测 | 同一 seed 两次运行 `state_hash` 序列完全一致；人工策略示范局可通关 |
| M1 | MOD-03、MOD-04、MOD-08（基线部分） | 8B agent + random/heuristic/fixed-prompt 三基线 | 三个基线的 held-out 分数入档；JSON 格式鲁棒性报告 |
| M2 | MOD-05、MOD-06、MOD-07（初版） | 反思 + 记忆 + K 局自动演化 + HITL 门 | 闭环跑通 ≥1 轮演化，产出 diff 与决策记录 |
| M3 | MOD-08（完整）、MOD-07（回滚/消融） | 评估报告 + 曲线图 + 消融结论 | 计划 §6 三项验收全部通过 |
| M4（可选） | MOD-10 | STaR 筛选 + LoRA 新基座 | 跨代增益 vs 推理时演化增益对比 |

---

## 8. 横切关注点

1. **确定性**：所有随机来自显式 seed；`PYTHONHASHSEED=0`；禁止 `random.*`、`time`-based 随机、`torch`/`numpy` 全局 seed 缺省；MOD-09 `guards.py` 提供 CI 级 AST 扫描。
2. **可观测性**：一次调用 = 一行 JSONL + 结构化事件（`episode_start/step/illegal/reflect/evolve/decision`）；任何模块不得自建日志格式。
3. **错误分类码**：`E_ENV_*`、`E_ACTION_*`、`E_LLM_*`、`E_EVOLVE_*`、`E_EVAL_*`、`E_IO_*`，随日志与 `info` 一并返回（MOD-09 集中登记）。
4. **版本与审计**：`harness_version` 单调递增且不可重写历史；每次接受/拒绝都要有 `report.json` 支撑；轨迹保留 ≥ 全部代际。
5. **降级**：模型不可用 → 退化为 heuristic agent 并显眼标注（该局不计入评估样本）；演化裁判失败 → 保守拒绝候选。
6. **成本**：单局上限步数、单次生成 tok 上限、每代候选数 ≤3，全部可配置并有默认。

---

## 9. 质量门（每个模块 PRD 的 DoD 基线）

| 门 | 要求 |
|---|---|
| G1 单测 | 核心路径单测覆盖；纯函数模块（MOD-01/02/08）覆盖率 ≥80% |
| G2 Golden | MOD-01/02 有固定 seed 的 golden 轨迹快照，改动必须显式更新并说明 |
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

### 11.2 待裁决

| # | 提出方 | 决策（当前实现依据） | 影响面 |
|---|---|---|---|
| P-01 | MOD-04 | `Action` 类仍定义在 `gamer_rsi/env/actions.py`，但由 `sandbox/api.py` re-export，L1+ 只许从门面导入——用于消解 INV-1 与 §4.3 的表面冲突 | INV-1、§4.3 |
| P-02 | MOD-04 | 新增 `SandboxEnv.reject(*, stage, reason, raw) -> StepResult`：一级校验失败时产生带 `illegal_reason` 的 Observation + `-0.05`，不改环境状态 | MOD-02 接口、MOD-01 记账 |
| P-03 | MOD-04 | 配置优先级插入可演化的 `harness/config.json`：CLI > `GAMERSI_*` > `harness/config.json` > `default.yaml` | §5 配置契约 |
| P-04 | MOD-04 | 轨迹行新增 `prompt_hash` / `memory_ids` / `tool_hash` / `source` / `mode` / `attempts` 字段（`x_` 前缀规则放宽为显式登记） | §4.5 schema |
| P-05 | MOD-04 | `agent/prompts/task.md` 为 INV-3 的第 ④ 类白名单上下文，且**不属**演化对象 | INV-3 |
| P-06 | MOD-02 | `FullState` 只经 `build_full_state() -> Mapping[str, Any]` 暴露一次，L1+ 在 `BLOCKED_MODULES` 语义下不可调用 | INV-1、MOD-01 接口 |
| P-07 | MOD-02 | 审计器只做黑名单扫描 + canary 探针，不做「prompt 中每个数值都归因到 `VisibleSet`」的正向校验 | INV-2/INV-3 |
| P-08 | MOD-02/04 | `phase="shop"` 时 `allowed_actions` 仅含 kind；商品 `item_id` 需以 `OBSERVATION_SCHEMA` 1.1.0 新增可选字段 `shop_items` 逐步揭示 | §4.2、INV-2 |
| P-09 | MOD-03 | GBNF 编译支持子集：`object/string/integer/number/boolean/array` + `enum/required/additionalProperties/maxLength/minimum/maximum`；遇 `allOf/if/$ref/oneOf` 返回 `None` 并降级 JSON mode | §4.3 结构化输出 |
| P-10 | MOD-03 | 30B-A3B 在 24GB 机器默认 `context_length=8192, max_inflight=1`；并发靠**局级并发**（多局共享串行推理队列）而非模型级批处理 | §8.6 成本 |
| P-11 | MOD-03 | 修复 prompt 模板由 `inference/schema.py` 独占拥有，MOD-04 不得覆盖 | §4.3、MOD-04/03 边界 |
| P-12 | MOD-03 | 局级降级需写 `degraded: bool` + `exclude_reason: str\|null`，MOD-08 据此剔除样本 | §4.5、§8.5 |
| P-13 | MOD-06 | `episode_key = e-{seed:06d}-v{harness_version}`：同 seed 跨代计两条、同代内只计一条 | 计分幂等 |
| P-14 | MOD-06 | 训练/评估 seed **必须**经 MOD-08 split 协议取用；MOD-06 自行切分则抛 `E_EVAL_SPLIT_VIOLATION` | INV-5 |
| P-15 | MOD-06 | 评估局不写 `scoreboard.json` 主台账，只写 `episodes/` 与 `EvalBatchResult` | 计分一致性 |
| P-16 | MOD-06 | 24GB 下 `eval_workers ≤ 2`，内存不足退化为 `workers=1` 顺序模式（行为等价） | §8.6 |
| P-17 | MOD-07 | `harness/versions/v{N}/` 布局：`version.json` / `gate.json` / `candidates/` / `patches/` | §4.4 |
| P-18 | MOD-05 | 记忆配置嵌套键用 `__`（如 `GAMERSI_MEMORY_RETRIEVE__TOP_K`）；新增 `memory` 子命令族；中文分词用字符 unigram/bigram（零依赖）；去重阈值 `sim≥0.85` 且 `situation_key` 相同 | §5、§4.6 |
| P-19 | MOD-08 | 报告数值统一 `round(x, 4)`；新增 `--basket` 选项与 `eval ablation` / `eval test` 子命令；`report.json` 严格只含 §4.5 字段（`additionalProperties:false`），扩展量（JSD/collapse_reasons/power_note）外置到 `metrics/gen-XXX.ext.json` 以避免 schema 漂移 | §4.5、§4.6 |
| P-20 | MOD-09 | `--run-id` 显式指定时以显式值为准（复现/测试用）；新增 `guards check` 子命令 | §4.6 |
| P-21 | MOD-01 | `state_hash` 规范化序列化规则：sort_keys + `ensure_ascii` + 禁 float（奖励用 1e-3 定点整数）+ NFC 归一 + UTF-8 码点序 | §4.1、INV-6 |
| P-22 | MOD-01 | 随机底座自实现 SplitMix64 + **命名流**（禁 `random`），流名白名单 `STREAM_NAMES`；敌方 `next_rolls` 在玩家回合开始前提交、敌方回合不重掷（使 `intent` 真实，防预读） | INV-6、§4.2 |
| P-23 | MOD-01 | 槽位约束 DSL 文法 `any\|odd\|even\|(ge\|gt\|le\|lt\|eq):int\|in:...`（阶段一仅单骰单条件）；难度只经 `enemy_hp_mul`/敌骰数调节，不改 AI 路径；新增 `configs/enemies.yaml` | §4.2、§5 |
| P-24 | MOD-03 | **M1 只交付 `MLXBackend` + `MockBackend`**，`LlamaCppBackend` 与阶段二 30B 同期上（省约 2.5 人日） | §4 技术栈、M1 范围 |
| P-25 | MOD-07 | 接受判定表 C0–C5（短路求值 + `condition_trace`）：C1 主指标不降（tol=0.0）、C2 配对显著且方向为升、C3 `win_rate_gap ≤ 0.15`、C4 无坍缩且 `entropy_slope ≥ −0.01`、C5 非法率/回合数上限；多候选同时通过时**只接受 1 个**；`paired_significance` 口径与最小样本量归 MOD-08 | §4.5、MOD-08 统计口径 |
| P-26 | MOD-07 | HITL 挂起写 `gen-{N}/gate.json(status=pending)` 并以退出码 0 结束（可续跑）；超时 24h 保守回滚（`hitl_timeout`） | §4.5、§5 |
| P-27 | MOD-07 | `version.json` 预留 `base_model` 字段以承接 MOD-10（会改 §4.4 契约） | §4.4、MOD-10 |
| P-28 | GAME-00 | **游戏本体改为前后端分离的网页实现（MOD-11 `game-web` 待立项）**：`frontend/`（静态 HTML，只渲染与收集输入）+ `backend/`（HTTP 薄壳，零依赖）+ `kernel/`（纯函数规则引擎，零 IO/DOM/时间/随机，唯一规则真相，禁浮点、禁 `Math.random`）。MOD-01 的定位从"唯一环境实现"改为**该本体的纯 Python 等价移植**，对拍契约见 GAME-01 §7。INV-6 语义不变（随机仍只来自显式 seed，改由自实现 SplitMix64 + 命名流承载）；INV-1 在 Web 场景由 API 观测白名单（GAME-00 §9.4）承担。**同时新增五系统前置开关** `mapBranching`/`relics`/`events`/`shops`/`schoolCounter`（全关 = 纯战斗线性闯关，必须可通关，作为回归基线）。见 [prd/GAME-00-webgame-PRD.md](prd/GAME-00-webgame-PRD.md) §2/§8/§17 | §1 INV-1/INV-6、§2 模块清单、§3 MOD-01、§5 配置契约、§6 依赖矩阵 |
| P-29 | GAME-00 | **动作集统一与新增**：游戏侧动作 `play_face`/`discard_die`/`use_ability`/`purify`/`end_turn`/`map_choose`/`shop_buy`/`shop_refresh`/`shop_remove`/`shop_leave`/`event_choose`/`treasure_pick`/`forge_pick`/`shrine_choose`/`abandon_run`，与 Spec §4.3 的 6 动作枚举合并（`place_die`→`play_face`，`levelup_choose` 暂缓）；`discard_die` 与 `purify` 为新增。**观测白名单 `obs-1.0.0` 与 MOD-02 的 `OBSERVATION_SCHEMA` 建议合并为同一契约**（字段名以 GAME-00 §9.4 为游戏侧基准，MOD-02 增补 agent 侧字段后再投影） | §4.2、§4.3 |
| P-30 | GAME-00 | **难度只经 `applyDifficulty(config)` 展开为全量整数配置**（`*Permille` 千分整数），业务逻辑禁止出现难度分支；**扩展 MOD-01 D-04**（原定"难度只调 `enemy_hp_mul`/敌骰数"）为整套配置化。另：玩家 `virtue`（战斗内血条）归零且仍有心时，**单场战斗判负但 `outcome` 不置 `lose`**，失心上移到 MOD-06 的 episode 层（修订 MOD-01 §8 的 `outcome="lose"` 粒度） | MOD-01 D-04/§8、§5 配置契约 |

**裁决原则**：涉及 **INV-1/INV-2/INV-5** 的条目（P-01、P-02、P-06、P-07、P-08、P-14）优先级最高，必须在 M0/M1 评审时先行裁决，其余可在对应里程碑裁决。**B-07（seed 协议）已裁决并全量对齐**，后续任何 seed 区间引用必须回查 MOD-08 PRD §4。

---

## 12. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版：9 模块 + MOD-10 预留、7 条不变量、全局契约、质量门 |
| v1.1 | 2026-09-30 | 配套 9 份模块 PRD 产出；回填 8 项跨模块契约（B-01..B-08，含 seed 协议全量对齐与 `generations.csv` 归属裁定）；新增 §11 待回填决策登记表（27 项待裁决） |
| v1.2（待评审） | 2026-09-30 | 新增 GAME 系列 PRD（游戏本体：GAME-00/01/02，见 [docs/README.md](README.md)）；登记 P-28..P-30：游戏本体改为前后端分离网页实现（MOD-11 待立项，MOD-01 转为纯 Python 等价移植）、动作集统一与新增、难度配置化与失心粒度上移 |
