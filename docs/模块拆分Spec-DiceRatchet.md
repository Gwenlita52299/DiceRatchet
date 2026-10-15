# DiceRatchet 模块拆分 Spec

> 版本：v2.29（2026-10-14，**`sandbox.audit.*` 整块收拢为「规则集版本钉扎」+ 钉扎不符 fail-closed（F73 裁决 ② / F74；代码改动，¥0）**：回查初版 MOD-02 PRD 的设计初衷后确认——`enabled`/`surfaces`/`ruleset`/`fail_on`/`evidence_chars` **五个键全部 0 消费者**（只被做成了 `run_audit()` 的参数 + 同名默认值），且「每代结束自动审计」**从来没有 FR 支撑**。落法：五键删除，只留 `sandbox.audit.ruleset_pin`（**有**消费者），CLI 把它喂给 `run_audit(ruleset=…)` 并由 `check_ruleset_pin()` 断言「== 代码常量 `RULESET_VERSION`」，不符即 **`E_ENV_AUDIT_RULESET`**（守卫轴退出码 **4**，不占结论轴 2）——报告里的 `ruleset_version` 因此**不可被配置伪造**。11 条护栏修复前 11/11 全红。见 §11.1m B-86 与变更记录）
> **上一版**：v2.28（2026-10-14，**删除 `docs/html/**` 手工 PRD 镜像（F72，用户裁决「无人浏览，删除 10 个文件」；纯删除，¥0）**：这套 10 页 / 5,323 行 HTML 无生成器、无 CI 检查、长期落后（实测 **11 个缺整小节 + 24 行缺变更记录**，其中 MOD-07 变更记录停在 v0.5、MOD-08 停在 v0.6），是「同一事实两份真相」的常驻源头 ⇒ 整目录删除，讲解与引用一律以 markdown PRD 为准。见 §11.1m B-85 与变更记录）
> **上一版**：v2.27（2026-10-14，**全库文档一致性修订（纯文档，¥0）**：对照代码与运行产物逐条修掉「当前状态」描述里的过期断言——C2 判据/签名/错误码、冒烟三门与 `smoke_n=2`、**公式 114 vs 实际 74 局**、`--split train` 只读、`acceptance.json` 的 `baseline_selection`、`RULESET_VERSION=1.1.0`、MOD-03 的 `resident_gb_est`/`cache_prompt`、MOD-04 的 F66 解析口径、四份 PRD 的上游版本号；并修掉三处**版本号自我冲突**（MOD-07 两个 v0.11、MOD-08 两个 v0.12、M3 报告两个 v1.3）；新登记 **F73**（`sandbox.audit.ruleset` 死键）。见 §11.1m B-84 与变更记录）
> **上一版**：v2.26（2026-10-14，**判据本身修好：取消 C2 的统计显著性门（改判「play 面确实变了」）+ `--split train` 只读化（F61/F71）+ 冒烟胜率轴删除（F67）+ 基线选择偏差标注（F70）+ 两处环境性门禁噪声按根因修（F68）**。**这一版的要害**：n=40 上 C2 的等效门槛是「净 +15 pp / 0.45 关」而 M3 实测效应只有 +2.5–7.5 pp ⇒ 是**门没有功效**；但**不能只删**（零改动的 accept 会让判据 1 再落回 `not_applicable`），因此槽位改判「补丁真的改变了打法」，统计量降级为 `paired.by_metric` 报告项。归档 run 零成本反事实复核：三代候选**都改了 play 面**，gen-003 被 **C5** 拒绝（truncation 回归）⇒ 新门可达、C5 有真诊断力。见 §11.1m B-83 与变更记录)
> **上一版**：v2.25（2026-10-14，**F58/F59/F60 全部修掉**：审计 `future_room_leak` 改**逐局作用域**（同一批工件 `critical/5852 → clean/0`）、数字扫描不再把**浮点小数部分**当 seed、台账版本列**以 `report.json` 为唯一权威**并修好被污染的 gen-003 行（`3/2 → 0/0`，判据 1 的假结论撤销）。见 §11.1m B-82 与变更记录）
> **上一版**：v2.24（2026-10-14，**D-M3-7：按用户架构重写「起点继承」——以 play 面指纹为准 + 每代只测候选 + 拒绝的轻量版本推进 + 死路记录**；同批修掉 **F64**（训练局 generation 差 1）与 **F69**（CI 不自洽时绘图抛非 InfraError 炸整代）；登记 **F67/F68/F70**。见 §11.1m B-81 与变更记录）
> **上一版**：v2.23（2026-10-14，**F66：候选补丁从未进入决策 prompt** —— `evaluate_arm` 注入 runner 时 `harness_dir` 被架空，候选臂与 parent 的 `prompt_hash` 逐字节相同（40/40）⇒ **判据 2 在本 run 无数据**，M2 冒烟/M3 彩排的候选结论待复核；同批登记 F63–F65。见 §11.1m B-80 与变更记录）
> **上一版**：v2.22（2026-10-14，**M3 正式实验主体执行结果**：3 代跑完、**0 accept** ⇒ 判据 1 `ok=false`（唯一原因是退化通过闸）；**同一 harness 四次实测**给出判据轴噪声底（`win_rate` 极差 7.5 pp、`stages` 0.275 关）；免费基线 `heuristic` 0.325 **全面优于 `C1`**；登记 **F58–F62**（审计跨局误报 / 浮点小数误判 / 重算伪造版本列覆盖台账 / train 侧臂解析 / C2 指标口径）。见 §11.1m B-80 与变更记录）
> **上一版**：v2.21（2026-10-13，**M3 正式实验起跑口径 + 预算/限流/墙钟上限落定**：run-id `m3-formal`、4 个 run 级 override、H1 40 局、hard×6、3 代、真实 `harness/` 自动 accept、主体不含 H2/test；脚本 `scripts/run_m3_formal.sh`。见 §11.1m B-79 与变更记录）
> **上一版**：v2.20（2026-10-12，**F57 代际口径定死为「起点臂」**：台账加 `arm` 三键、判据 1 同版本豁免、parent 测量跨代继承；见 §11.1m B-77/B-78 与变更记录）
> **上一版**：v2.19（2026-10-12，**F56 台账 + 绘图收敛到一处**：`harness.ledger_and_plots` 由 MOD-06 基线 / MOD-07 代末 / `eval report` 三方共用；见 §11.1m B-76 与变更记录）
> **上一版**：v2.18（2026-10-12，**F55 只读命令与 config_hash 硬门解耦**：`resolve_run_dir(strict_hash=False)` + `cli_init_run(read_existing=True)` 接到 6 个只读 handler；写侧仍硬校验。见 §11.1m B-75 与变更记录）
> **上一版**：v2.17（2026-10-11，**F54 held-out 篮子可配置**：彩排事故的处置 —— `evolve.heldout_basket` 让 gen-000 基线与候选评估同源，默认仍是 H1；见 §11.1m B-74 与变更记录）
> **上一版**：v2.13（2026-10-11，**M3 前置决策 D-M3-1..4 + F-M3-3/F48 绘图 + F-M3-9 判据 1 判定器**：`hard×6` 唯一主口径与 `win_rate` 第一判定量；C5 三轴相对门 + backstop；G3 按臂分层；HITL/样本资格口径；绘图接生产调用点并修掉空白图与台账零 train 行；`eval acceptance` 判定器；见 §8.1/§8.6、§11.1m B-63..B-69、§12）
> 历史版本：v2.6（2026-10-11，**F45-2 + F42 + F46 处置**：heldout/test 移出四位数字空间（协议级，`configs/seeds.yaml` 换六位端口 + 代际隔离闸 + 删两张人工死名单，登记 B-62）；每代评估的检查点/复用/提案检查点（F42-A+B+C，B-60/B-61）；`eval report` 的 `generation` 变真过滤 + 臂显式化（F46-A，B-59）；见 §11.1l B-59..B-62、§12）
> **历史版本**：v2.5（2026-10-10，**F47 处置（A+C+D）**：FR-09-14 的「演化前门禁」原先只长在 `commit_harness` 内部——脏工作区要等整代跑完才拒（实测白花 3.75 h / ≈¥47），且 `report.json` 反而写 `decision="accept"`（幻影 accept）；改为 MOD-06 在起跑前调用 + MOD-07 失败自清 + 基础设施失败与裁判拒绝分口径（`error_reject`）；见 §11.1k B-56..B-58、§12）。
> **历史版本**：v2.4（2026-10-10，**§3.2 实跑合格 + F42–F46 处置**：生成侧台账与 `eval report` 的路径冲突（F43）、`audit leak` 把多行 JSON 当 JSONL（F44/F45）、每代评估无检查点（F42，仅登记）、`eval report` 的 `generation` 不筛文件（F46，待裁决）；见 §11.1j B-52..B-55）· v2.3（2026-10-10，**F32–F41 处置**：闭环路径上被测试掩护的十个死缺陷（含 `episode_key` 漏 `-smoke` 臂、提案无文件正文、hunk 不容忍 offset、提案预算被决策点预算卡死）；并**改写 §3.2 验收判据**为「机制性失败为零」；见 §4.5/§4.7、§11.1i B-47..B-51、§12/changelog）· 状态：已评审 · 定位：**模块边界与接口的唯一事实来源（Single Source of Truth）**
> 上游：[项目计划-DiceRatchet.md](../项目计划-DiceRatchet.md) · [RSI调研纪要.md](../RSI调研纪要.md)
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
| INV-1 | agent 只能通过 `SandboxEnv.observe/step` 触达环境；L1 及以上代码 **禁止 import `dice_ratchet.env.*` 内部模块** | MOD-02 静态检查 + MOD-09 import 门禁 |
| INV-2 | `Observation` 是白名单投影，任何新字段必须在 `OBSERVATION_SCHEMA` 登记 | MOD-02 |
| INV-3 | 进入 prompt 的上下文只允许：当前 Observation + 检索到的 memory + 当前 harness 工件 + 任务说明（`dice_ratchet/agent/prompts/task.md`，人工版本化，**不属**演化对象，B-27） | MOD-04 |
| INV-4 | harness 自编辑产出的代码在受限执行环境运行：无网络、无文件写、无 subprocess、无 env 内部访问 | MOD-02 + MOD-07 |
| INV-5 | train / heldout / test 三类 seed 严格隔离；heldout 与 test 的 seed 值不得进入任何 prompt 或记忆 | MOD-08 + MOD-09 |
| INV-6 | 所有随机性来自显式 seed（`env`、采样、演化），禁用全局 `random`/时间相关随机 | MOD-01 + MOD-09 |
| INV-7 | 评估主指标只用**环境原生结果**：`outcome`（win/lose，截断一律计 lose）为第一主指标，`stages_cleared` / `hearts_left` / `battles_won` 为并列次指标（全部取自 GAME `stats` 与 `player`）；shaped reward 仅用于学习与调试 | MOD-08 |

**不变量实施细则（v1.4 裁决）**：
- **INV-1 类型导入（B-23）**：动作类型 `Action`（GAME 动作对象的 TypedDict）与 `ACTION_KINDS` 定义在 `dice_ratchet/env/actions.py`，由 `dice_ratchet/sandbox/api.py` re-export。L1+ 只许 `from dice_ratchet.sandbox.api import Action`；直接 import `dice_ratchet.env.*` 一律由 import 门禁拒绝。
- **INV-1 全状态封装（B-24）**：MOD-01 的 `GameState` 只由 `SandboxEnv._engine` 私有持有，没有任何返回全状态、engine 或 rng 状态的公开成员。`dice_ratchet.env` 在 MOD-02 的 `BLOCKED_MODULES` 中对 L1+ 不可导入。v1.2 登记时的 `build_full_state()` 方案，因 MOD-01 v1.0 改为移植 GAME 内核而失去对象，改用上述等价的封装形式。
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
# dice_ratchet/sandbox/api.py
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
- **冻结基线模板不在 `harness/` 里**（R-6 / M1 验收 F12 + F22）：MOD-08 `fixed_prompt` 基线用的
  模板是**随包冻结件** `dice_ratchet/agent/prompts/fixed_v1.md`（配置键 `agent.policy.fixed_prompt_path`），
  任务说明同理取 `dice_ratchet/agent/prompts/task.md`（`agent.policy.task_path`）。二者**不参与**
  MOD-07 的代际演化、也不进 `artifact_hash`——否则「基线不随 harness 漂移」（R-6）不成立。
  基线 agent 由 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")` 承担（`tools_enabled=false`、
  `harness_version` 恒 0），MOD-08 不再自带第二套固定提示词 agent（F22）。
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
      （以上任一可带 `-smoke` 后缀，标识 MOD-07 FR-07-6 的冒烟臂）
```

同一 seed 在不同代、不同候选下各有独立文件，互不覆盖；`episode_key` 同时作为 MOD-06 计分台账的幂等键（取代 P-13 的旧格式）。

⚠️ **2026-10-09 修订（F41）**：`arm` 位原先**不含** `-smoke`，而 MOD-07 的冒烟臂正是 `parent-smoke` / `cand-{id}-smoke` ⇒ 冒烟局每行轨迹校验失败（`E_IO_TRAJECTORY_SCHEMA`）→ 该局 dropped（轨迹文件 0 行）→ `n_dropped/n = 1.0 > drop_rate_max` → `low_confidence` → `E_EVAL_INSUFFICIENT_N` → 冒烟快速淘汰 ⇒ **每个候选在冒烟关即死，`harness/versions/` 永不可能有新版本**。该语法在**三处**有副本，改动必须同步：`infra/logging.py` 的 `_EPISODE_KEY_RE`、`schemas/trajectory_line.schema.json` 的 `pattern`、MOD-09 PRD §6.2（前两者由 `tests/unit/infra/test_logging.py` 的一致性用例钉住）。

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
 "candidates":[{"id":"b","kind":"tool_patch","gate":"pass","paired":{"method":"mcnemar","p":0.21,"delta":0.05,"by_metric":{"win_rate":{...},"mean_stages_cleared":{...},"mean_hearts_left":{...}}}}],  // F62：`paired` 另带 `by_metric`（逐指标报告项）
 "decision":"reject","decided_by":"auto","reason":"C2:no_play_change"}   // F62/D-M3-7：C2 已是「play 面指纹是否变了」，不再是「未显著」
```

**HITL 挂起（B-32）**：`--hitl` 时，MOD-07 写 `generations/gen-{NNN}/gate.json`（`status="pending"`）；MOD-06 置 `phase="hitl_wait"` 并以**退出码 0** 结束，可用 `--resume` 续跑。挂起超过 `evolve.hitl_timeout_h`（默认 24）仍未批复的，下次 `--resume` 时保守回滚到 parent（`reason="hitl_timeout"`，`E_EVOLVE_GATE_TIMEOUT`）。

**数值精度（B-34）**：`report.json`、`metrics/*.json`、`generations.csv` 中的浮点数一律 `round(x, 4)`；本文示例中的 2 位小数只是展示。

**代际口径（F57，v2.20 / B-77）——`report.json` 的三个版本/来源字段不得混用**：

| 字段 | 含义 | 谁写 |
|---|---|---|
| `harness_version` | 本代**结束**时在役的版本（accept 即新版本；**⚠️ 2026-10-14 修订（D2=A）**：拒绝也**轻量推进 `+1`**——只写 `version.json` 指针 + commit，无快照、无 tag）——台账的连续性靠它 | MOD-07 经 MOD-08 |
| `parent_version` | 本代的**起点** harness 版本（F57 起恒等于起点臂版本；**⚠️ 2026-10-14 修订（D-M3-7）**：语义收紧为「起点」，**不再**表示「`heldout` 段测在哪份 harness 上」——该信息由 `heldout.source` 与 `harness_version` 共同表达） | 同上 |
| `heldout.source` | `measured` = 本代真测；`inherited:gen-00X` = 跨代继承第 X 代的同一臂测量（**⚠️ D-M3-7 起 `heldout` 段 = 本代结束时在役那份 harness 的测量**） | 同上 |

`metrics/generations.csv` 的键是 **`(generation, split, arm)` 三键**（F57 起；此前 `generation` 单键 → `(generation, split)` 双键）：同代**同时存在**起点臂与候选臂两套测量，少 `arm` 就会**无声覆盖**。判据 1 与绘图主曲线只读 `arm="parent"`；`heldout.source` 以 `inherited:` 开头时**不写 heldout 行**（没有真实测量就不写行，同 F48 的 `train.n == 0`）。

### 4.6 命令面（MOD-09 统一暴露；新增命令必须回填本节）

```bash
python -m dice_ratchet env selftest --seed 1              # M0：规则/确定性自检 + GAME golden 对拍
python -m dice_ratchet play --agent llm|heuristic|random|fixed_prompt --seeds train:1000-1004 [--model ID]
python -m dice_ratchet baseline --kind random|heuristic|fixed_prompt --basket H1 [--model ID] [--resume]
                       # --basket 与 --seeds 互斥；--basket 省略时按 --split 推导（train_window/test_full/H1）
                       # --model 仅 fixed_prompt 需要（冻结提示词 + 真实 LLM；拿不到即退出码 3）
                       # fixed_prompt 走 MOD-04 PolicyAgent(mode="baseline_fixed_prompt")：**每决策点一次推理**
                       # （M1 验收 F22）；--resume 按 episodes/<split>/ 已完成的局跳过重复 seed，
                       # 且**不要求** state.json（那是 loop 的续跑语义，见下方统一约定）
python -m dice_ratchet loop run --generations 3 --k 30 [--hitl] [--run-id ID] [--resume]
python -m dice_ratchet loop status|smoke --run-id ID
python -m dice_ratchet evolve approve|rollback --run-id ID --generation 3 [--reason STR]
python -m dice_ratchet eval report --run-id ID --split heldout --baseline all [--arm NAME|all]
                       # --arm 缺省 = 该代 report.json 里的胜出/候选臂（无候选则 parent）；
                       # all = 显式申报的混合池（标注 mixed_pool，禁止当默认口径，F46-A）
python -m dice_ratchet eval ablation|test --run-id ID [--basket H2]
python -m dice_ratchet eval diversity --run-id ID [--arm NAME|all]  # 缺省/ all = 该代整池
python -m dice_ratchet eval acceptance --run-id ID [--bootstrap-B N]
                       # 判据 1 判定器（F-M3-9，B-69）：held-out 单调不降 + ≥1 次 accept
                       # → metrics/acceptance.json；**总是返回 0**（判定器不是守卫，
                       #   不通过时原因写 stderr；需要门禁时读产物里的 ok）
python -m dice_ratchet audit leak --run-id ID            # INV-2/INV-3/INV-5 审计
python -m dice_ratchet inference probe|bench|robustness   # MOD-03
python -m dice_ratchet memory stats|snapshot|rollback|verify|retrieve   # MOD-05（B-08）
python -m dice_ratchet guards check [--root PATH]         # MOD-09（P-20）
```

统一约定：run 一律用 `--run-id ID` 指定，续跑用 `--resume` 开关；所有命令都支持 `--dry-run`。
两类续跑语义（M1 验收 F18 后明确）：

- **编排续跑**（`loop run`）由 `state.json` 决定（缺失即 `E_IO_STATE_MISSING`），语义归 MOD-06；
- **测量续跑**（`baseline --resume`）只看 `episodes/<split>/` 里已完成的局：跳过重复 seed、只跑缺的，
  且逐局增量落盘（`run_eval` 的 `on_result` 钩子），**不依赖** `state.json`。

另：`--model/--mode/--seed/--timeout-ms/--k/--generations/--hitl/--log-level` 会并入 `config_hash`
（FR-09-1），因此**同一 `--run-id` 只能承载覆盖集合完全相同的命令**，否则 `E_IO_RUN_CONFLICT`
（退出码 2）——这正是 M1 验收 F10 的成因，各 README 的快速开始块按「族」分 run_id。

### 4.7 模块间调用签名表（唯一登记处，B-17）

各 PRD 只能**引用**本表签名；改签名需要先改本表。

| 调用方 → 被调方 | 签名 | 说明 |
|---|---|---|
| MOD-02 → MOD-01 | `GameEngine(tables).create(cfg) / reduce(state, action) / legal_actions(state) / observe(state, view)` | GAME-01 §1 的 Python 等价；`tables` 读取 `src/game/content/*.json` |
| MOD-06 → MOD-04 | `PolicyAgent.reset(ctx: EpisodeContext) -> None` | 每局开始 |
| MOD-06 → MOD-04 | `PolicyAgent.play_decision(env: SandboxEnv, obs: Observation, *, ctx: EpisodeContext) -> DecisionResult` | 执行一个决策点（含计划、重试、fallback），返回 `steps: list[StepRecord]`、`source`、`attempts`、`llm: LLMCallMeta\|None`、`memory_ids`、`adopted_memory_ids`、`prompt_hash`、`last: StepResult` |
| MOD-04 → MOD-03 | `InferenceClient.generate_json(prompt, *, schema, mode, sampling, seed, max_tokens=None) -> JsonResult` | `mode ∈ train/eval`；`max_tokens` 是**按调用方**的输出预算（`None` = 用 `inference.max_tokens`），见 B-47 |
| 组合根 → MOD-04/06/07 | `RoleModelProvider.client_for(role) -> InferenceClient`（`role ∈ policy/reflection/proposer`） | 三角色的模型绑定**全部来自配置**；MOD-04/06/07 只做鸭子类型解析（`coerce_client`/`client_for_role`），不 import 提供者类型（B-42） |
| 全部 → MOD-03 | `inference.roles.resolve_role_bindings(cfg) -> RoleBinding` · `role_model_ids(cfg) -> dict[role, model_id]` · `run_provenance(cfg, run_id) -> dict` | 优先级 `experiment.roles.<role>` > `experiment.arms[arm].<role>` > `models.<role>` > `inference.model_id`；未知角色/未知臂/空 `model_id` 一律 fail-closed（不静默降级）；落 `runs/<run_id>/models_provenance.json` |
| 全部 → MOD-03 | `inference.secrets.lookup_api_key(model_spec) -> str` | `api_key_env` 只登记**变量名**；真值取自真实环境变量或本地 `.env`（`.env` 被 gitignore，绝不入 `harness/`、run JSON、事件或 Git） |
| MOD-04 → MOD-05 | `MemoryStore.retrieve(obs, *, top_k, token_budget, tick: int) -> list[MemoryHit]` | `tick` 为逻辑时钟（训练局序号），不用墙钟 |
| MOD-06 → MOD-05 | `MemoryStore.add_reflection(entry: ReflectionEntry, *, provenance: Provenance) -> str` · `record_usage(episode_key, memory_ids, outcome, *, adopted_ids, tick) -> None` · `snapshot(version: int) -> Path` · `frozen(version) -> MemoryView` | 评估局只用 `frozen` 只读视图 |
| MOD-06 → MOD-03 | `loop.reflect.reflect(summary: EpisodeSummary, *, client, template) -> ReflectionEntry` | 反思归 MOD-06；`ReflectionEntry` schema 归 MOD-05 |
| MOD-07 → MOD-08 | `evals.evaluate_arm(harness_dir, *, split, basket, memory_version, arm, run_id, protocol=None, runner=None, generation=0, harness_version=0, config=None, model_hash="", n_workers=1, n_rep=1, episode_config=None, resume_results=(), on_result=None, run_dir=None, archive=True) -> ArmResult` | 单臂评估（**F42-C**：`n_workers`/`n_rep` 按配置转发；**B-60**：`resume_results`/`on_result`/`run_dir`/`archive`） |
| MOD-07 → MOD-08 | `evals.compare_paired(a: ArmResult, b: ArmResult, metric: str = "win_rate", *, alpha: float = 0.05, B: int = 10000, rng=None) -> PairedTest` | 方法与显著性口径归 MOD-08；`metric="win_rate"` ⇒ McNemar，连续量 ⇒ 配对 bootstrap |
| MOD-07 → MOD-08 | `evals.compare_all_metrics(a: ArmResult, b: ArmResult, *, alpha: float = 0.05, B: int = 10000, rng=None) -> dict[str, PairedTest]` | **F62（2026-10-14）新增调用**：C2 取消统计门后，逐指标统计作为 `paired["by_metric"]` **报告项**（0 局新成本） |
| MOD-07 → MOD-08 | `evals.restore_arm_from_archive(archive, *, generation, harness_version, arm, required_seeds=(), t_max=120, min_support=3, drop_rate_max=0.10) -> ArmResult \| None` | **D-M3-7 新增调用**：把臂归档还原成 `ArmResult`（**不跑局**），供起点 heldout 的跨代继承 |
| MOD-07 → MOD-08 | `evals.evaluate_generation(gen, parent: ArmResult, winner: ArmResult\|None, *, run_id, run_dir=None, config=None, protocol=None, heldout_source="measured", retained_arm=None, retained_version=None, play_hash="") -> dict` | 产出 report 指标段（**D-M3-7**：`heldout` 段 = 本代结束时在役那份 harness 的测量） |
| MOD-06/07 → MOD-08 | `evals.seeds.take(split, *, generation=None, basket=None) -> list[int]` | seed 唯一出口（INV-5） |
| MOD-07 → MOD-02 | `sandbox.guard.gate(harness_dir) -> GateReport` · `sandbox.guard.load_harness_tools(harness_dir) -> ToolWorker` | 子进程 worker |
| MOD-04/05/06 → MOD-02 | `sandbox.audit.scan_text(text, *, surface) -> list[Finding]` | prompt/memory/reflection 泄漏扫描 |
| MOD-04/08 → MOD-04 | `agent.heuristic.heuristic_policy(obs, legal: list[Action], hints: dict) -> Action`（`hints` 来自 `SandboxEnv.hints()`） | fallback 与 heuristic 基线共用，逻辑移植自 GAME `scripts/bots.js::greedy_counter` |
| MOD-07 → MOD-09 | `git_ops.commit_harness(repo, version, msg) -> sha` · `tag_version(repo, version)` · `rollback_to(repo, version) -> sha` | tag 为 `harness-v{N}` |
| 全部 → MOD-09 | `infra.events.emit(event: str, **fields)` · `infra.errors.E.<CODE>` · `infra.io.write_json_atomic(path, obj)` | 注册表见 §8.2/§8.3 |

---

## 5. 目录与配置契约

```
dice_ratchet/
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
docs/        模块拆分Spec-DiceRatchet.md  prd/  README.md
```

**配置优先级（B-26）**：CLI 参数 > 环境变量 `DICERATCHET_*` > `harness/config.json`（可演化层）> `configs/default.yaml` 默认值。
- `harness/config.json` 只能包含白名单键：`policy.*` 的训练期采样参数、`memory.retrieve.*`、`agent.tools.*`。它不得包含 `episode.*`、`seeds.*`、`loop.*`、`evolve.*` 等静态键，也不得覆盖 §8.1 的评估温度；违规时 MOD-07 在 C3 门禁拒绝，MOD-09 加载时抛 `E_IO_CONFIG_INVALID`。
- 这一层属于 harness，因此计入 `artifact_hash`、不计入 `config_hash`（B-19）。
- **环境变量命名（B-30）**：嵌套键用 `__` 分隔，如 `DICERATCHET_MEMORY_RETRIEVE__TOP_K=8` 对应 `memory.retrieve.top_k`。
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

**⚠️ 2026-10-11（D-M3-3 裁决 A，登记 B-65）：确定性承诺按层分列。** 上面三条是**可复现层**的承诺（代码 / 配置 / seed / 聚合口径 / 并发归一化），**本地臂仍然硬执行**，违反即 `E_EVAL_NONDETERMINISTIC`。**LLM 层不承诺逐字节**：当前主力臂 `C1` 使用 `provider=api` 的 `deepseek-flash`（注册表标注 `api_supports_seed=false`，实测「字节相同的请求 9 次返回 9 个不同响应」），而 `temperature=0.0` **只是采样参数**，不承诺跨时间确定性。因此：采样照记、响应按 request hash 归档、变化即标 `llm.nondeterministic`（MOD-03 口径）；**臂内含任何 `provider=api` 的角色时，G3 的 sha256 硬门跳过硬执行**、发 `eval.nondeterministic{determinism=api_nondeterministic}` 事件（执行点与 fail-closed 方向见 MOD-08 §5 NFR-08-01）。**不得**据此宣称「API 臂的 M3 结果可逐字节复现」；M3 报告必须显式声明「本轮无逐字节复现证据」。

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

**bring-up 口径（2026-10-06 M2 开工 D-2 裁决；2026-10-09 **B-45 复核确认**）**：默认局仍是 hard × 6（M3 主口径），但 M2 的闭环联调与 §3.2 冒烟改用 `difficulty="medium", stage_count=6, toggles=全开`（run 级 override，不改 `configs/default.yaml` 的默认值）。**2026-10-09 复核**：该口径在对 `C1`（DeepSeek）臂上**有了第二重理由**——`medium × 6` 实测 `truncation_rate = 0.00`（`hard × 6` 是 0.30，会撞内核回合上限），且该局型上 API 臂的 `win_rate` 有方差（80% vs 启发式 70%）。**可执行的 override 写法是环境变量**（B-30：`__` 分层；M1 的 CLI 没有通用 `--set`，只有 `--k/--generations/--hitl/--model/--mode/--seed/--timeout-ms/--log-level`）：

```bash
DICERATCHET_ENV__EPISODE__DIFFICULTY=medium <cmd>   # 等价于 --set env.episode.difficulty=medium
```

`difficulty` 参与 `config_hash`（实测 hard → `141bef5f…`、medium → `2dc294eb…`），所以 medium 口径属另一个 run-id 族（F10），不会与 hard 基线的归档混族。理由：hard × 6 上 `fixed_prompt` 基线 `win_rate = 0.0%`（n=10，`runs/m1-real-h1n10b`），win_rate 在该局型上没有方差、无法判定演化；medium × 6 先提供可判定的信号，闭环跑通后再回到 hard × 6。**medium × 6 的 `greedy_counter` 通关率与 `D` 尚未实测**（下表只有 medium × 10 与 medium × 3），M2 冒烟前必须补测并登记——`random`/`heuristic` 是秒级，`fixed_prompt` n=10 ≈3.3 h。**补测的判据是"既不是地板也不是天花板"**：若 8B 在 medium × 6 上通关率→100% 或 `mean_stages_cleared` 顶到 6（对照上表 medium × 3 = 100% 的天花板），bring-up 同样没有可判定的方差，D-2 必须再调（改用 medium × 10，或回 hard × 6 并只走连续指标）。

**复现命令（唯一登记处，M0 验收 F5）**：下表每一行都由同一条命令产出，**seed 区间是数字身份的一部分**：

```bash
node src/game/tests/bot-match.js 200 <stageCount> <difficulty> 1 --stats   # n=200，seed 1–200
```

口径：通关率 = `outcome=="win"` 占比；env 步 = 每成功 `reduce` 计 1 步；决策点 = `decisionIndex` 终值（Spec §4.3 / B-12 规则，与 MOD-02 `SandboxEnv` 同口径，由 `tests/golden/test_bot_match.py::test_js_decisions_match_sandboxenv_decision_index` 守卫）。实测（2026-10-03，`greedy_counter`，n=200，seed 1–200）：

| 局型 | greedy_counter 通关率 | env 步 p50/p90 | 决策点 p50/p90 |
|---|---|---|---|
| hard × 6 关（**默认**、M3 主口径） | 45.5% | 232 / 284 | 96 / 116 |
| **medium × 6 关（M2 bring-up，2026-10-07 新增实测行）** | **91.0%** | **249 / 301** | **92 / 103** |
| medium × 10 关（完整局） | 24.0% | 397 / 535 | 146 / 187 |
| easy × 3 关 | 100.0%（**对启发式**是天花板） | 105 / 124 | 38 / 43 |
| medium × 3 关 | 100.0%（**对启发式**是天花板） | 102 / 121 | 37 / 41 |
| hard × 3 关 | 97.0%（**对启发式**是天花板） | 91 / 114 | 38 / 44 |

> ⚠️ 上表的「天花板」只对 **`greedy_counter` 启发式**成立（2026-10-03 口径）。**对 8B agent 不成立**：medium × 6 上启发式 91%（内核）/70%（Python，H1 n=10），而 8B `fixed_prompt` 通关率仍是 **0/10 = 0.0%**（2026-10-07 实测）。选 bring-up 局型时不能把本表的「天花板」当 agent 的天花板。

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
> 原「预估约 3 s」**低估 1.9×** —— 这正是 [排期 §6](项目排期-DiceRatchet.md) 的首要风险，实跑耗时应按下表放大。

- 单局耗时 `t_ep ≈ D·t_d + t_reflect`：按实测 `t_d = 5.687 s` → 96 × 5.687 ≈ **546 s ≈ 9.1 分钟**（含重试 ≈ 9.7 分钟）；原估 5 分钟是 t_d=3 s 的产物。
- ⚠️ **本条的示例数值（C=2、S=5、165 局、25.0 h/代）是历史口径，已被下方「2026-10-06 M2 开工定价」与「2026-10-07 §3.1 实测回填」取代**：现行口径为 `C=1 / S=2 / H=40 / K=30 → E_gen=114 局 ≈37 h/代`。保留本行只为记录公式与推导来源。**⚠️ 2026-10-14 修订（D-M3-7）**：`E_gen` 公式与默认值未变，114 仍是与 `loop.max_episodes_per_generation=120` 比较的**保守上限**；但 parent 的 heldout 改为**按 play 面指纹继承、不重测**后，继承命中时**实际每 cycle = 74 局** = `K 30 + parent 冒烟 2 + 候选(冒烟 2 + heldout 40)`，只有找不到可复用测量时才退回 114。
- 每代局数 `E_gen = K + (H + S) + C·(S + H)`：训练 K=30；parent 的 heldout H=40 与冒烟 S=5（与候选使用同一个冻结的记忆快照，所以每代都要重跑）；候选数 C=**2**（上限 3），每个候选也跑冒烟 S 与 heldout H。默认值下 E_gen = 30 + 45 + 2×45 = **165 局**；按实测 → 165 × 546 s ≈ **25.0 小时/代**（原估 14 h）。**⚠️ 2026-10-14 修订（D-M3-7）**：本行「parent 的 heldout（同一冻结记忆快照）**所以每代都要重跑**」的论证**已不成立** —— parent 的 heldout 改为**按 play 面指纹继承**同 play 面的已有测量（命中即 0 局，不再重测），只有找不到可复用测量时才退回实测；继承命中时实际每 cycle = **74 局**（`K 30 + parent 冒烟 2 + 候选(冒烟 2 + heldout 40)`）。本行的 165 局 / 25.0 h 及其下方的示例口径均按历史口径保留。
- M3 全程为 3 代，加上 gen-000 基线 40 局、fixed_prompt 基线 40 局、消融 2 臂 × 40 局、test 100 局：按实测线性放大 **≈ 114 小时（4.7 天）连续推理**（原估 60 h ≈ 2.5 天），含重试约 **121 小时（5.0 天）**。
- **衍生硬约束**：`inference.total_timeout_ms` 由 8000 ms 回填为 **120000 ms**（原值 < 实测 p50，正常决策会被判 `E_LLM_TIMEOUT`）。`evals.seed_timeout_s` 同时按实测单局 ≈546 s 把默认值由 300 s 调到 **1800 s**。**（2026-10-05 更新，M1 验收 F20 已修）** 该超时现在**真正被执行**，两道闸：① `SandboxRunner.run_episode()` 把 `timeout_s` 作为 deadline 传给决策循环，超预算的局收尾为 `dropped=true` + `E_EVAL_TIMEOUT`（计 n、按 lose 计）；② `run_eval` 以 `seed_timeout_s + 60 s` 为上限巡检，超限即放弃该 worker、记 dropped，并**换新线程池**继续跑剩余 seed（否则 `n_workers=1` 时一个卡死的 worker 会让其余 seed 永远排不上队）。残余风险：Python 无法杀死已运行的线程，被放弃的 worker 由 MOD-03 的调用级超时（`generate_timeout_ms`/`total_timeout_ms`）收尾。
- **若要把 M3 实跑压回 2.5 天**：优先按排期 §6 下调 C（候选数 2→1）与 S（冒烟 5→2），或改 K=30→15；`H=40` 与 test=100 属统计功效底线，不建议动。
- **⚠️ 单局预算与并发度耦合（2026-10-05，M1 验收 F24）**：`evals.seed_timeout_s` 是**墙钟**预算，而 `n_workers` 个局共享同一个模型实例（`max_inflight=1`），因此 **单局墙钟 ≈ D × 每决策调用数 × t_d × n_workers**。默认值已按此式由 1800 s 调到 **14400 s**（实测 `300 决策点 × 1.8 调用 × 12 s(p95) × 2 worker ≈ 13000 s`）；任何「n 局在 x 分钟内完成」的推算都必须同时带上 `n_workers`。**（2026-10-05 n=10 实测校验）** 上式里的 `× n_workers` 在实测中**不成立**：`runs/m1-real-h1n10b/` 的 `d_wall / d_model = 1.02`，即模型串行、`n_workers` 不放大单局墙钟（它只并行非模型段）。`n` 局墙钟 ≈ `n × 单局模型时间`；14400 s 因此是偏宽的安全余量，M2 若改计算量预算，应按**单局模型时间**（实测均值 19.6 min，上界约 45 min）定。
- **⚠️ 每决策动作数决定实际的 `D`（2026-10-05，M1 验收 F25）**：`D` 不是常数，它取决于 agent 每回合打几张牌。同一局（hard × 6）实测：启发式 `steps/decisions ≈ 3.0`（D≈96），而模板未写明「一次决策 = 一个回合」时真实 8B 只有 **1.17**（D≈250，且会撞 `episode.max_decisions=300`）；两条模板补第 9 条后 A/B 的合法动作/决策点由 1.35 → **2.32**。成本模型必须用**该 agent 实测的 `D`**，不能直接沿用 96。
- **🔴 2026-10-05 真实实测重标定（M1 验收报告 §14.18，`runs/m1-real-h1n40e/`）**：`E_gen`/M3 的推算**必须用该 agent 实测的 `D` 与调用率**，不能沿用 `D=96 / 1 次调用 / t_d=5.687 s`。8B+MLX 上 `fixed_prompt` 的实测：**`D ≈ 185`（自然结束的完整局：185 决策点 / 330 env 步 / 4-6 关 / lose）、每决策点后端调用 1.6–1.8 次、`t_d` p50 **7.9 s**（p95 10.8 s，实际 prompt 2500–3000 token）** → **单局模型时间 ≈43 min（旧模型 9.7 min，×4.7）**；`E_gen=165 局` → **≈118 h/代**（旧 25 h）；M3 全程 → **≈540 h ≈ 22 天**（旧 114 h ≈ 4.7 天）。n=40 的 `fixed_prompt` 基线单独就要 **≈29–34 h**（旧估 4 h）。三个放大因子：`D` 96→185（LLM 的 `steps/D`=1.78 vs 启发式 3.0）、调用/决策点 1.07→1.6–1.8、`t_d` 5.69→7.9 s。**排期必须按此重排**，并用 §8.6 下方列出的杠杆（下调 C/S、KV 前缀复用、更小模型/更好推理栈）压回。
- **✅ 2026-10-05 n=10 子集实测回填（M1 验收报告 §14.19，`runs/m1-real-h1n10b/`，seed 2000–2009）**：上一条的 **n=40 `fixed_prompt` ≈29–34 h** 是按 n40e **重尾高端的 2 局（D=185/207）** 外推的**上界**；n=10 子集实测墙钟 **3.32 h**（模型时间 3.26 h、1,551 次后端调用、`t_d` p50 7.46 s / p95 10.13 s、schema 全失败率 5.8%、`fallback` 122 次、`illegal_rate=0.3021`、`fallback_rate=0.181`），**`win_rate = 0/10 = 0.0%`（Wilson 95% CI [0.000, 0.278]）**；同 run 的 `random` 0/10 = 0.0%、`heuristic` 4/10 = 40.0%（与旧 n=10 子集逐位一致）。**关键修正**：n=10 实测 `D` 均值只有 **97.1**、中位 69（min 33 / max 245，跨局差 7.4 倍），单局模型时间实测 **19.6 min**（预测 43 min，0.46×），按此均值外推 n=40 的模型独占下限 ≈ **13.1 h**——成本模型应按**实测 `D` 分布**算并**按上界留预算**，不能把上界当期望值。另两条口径结论：① `d_wall / d_model = 1.02`，**「2 worker 共享一个模型」不产生墙钟收益**（`max_inflight=1`），`n` 局墙钟 ≈ `n × 单局模型时间`，加大 `n_workers` 不提速；② `D` 的方差主导总耗时，`E_gen`/M3 推算必须带 `D` 的分布而不是单点值。
- **✅ 口径修正（2026-10-05，M1 验收 F22 已修）**：`t_ep ≈ D·t_d` 现在对**基线也成立**。修复前 MOD-08 的三个基线走 `SandboxRunner._agent_loop`，**每推进一个 env 步就问一次模型**（实测基线 `fixed_prompt` 的 env 步/决策点 = **3.1**，单局平均 **245 次推理**，决策点口径只需 ~79 次）；修复后基线改走 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")`，**每个决策点一次推理**，与演化 agent 粒度一致。受控实测（假客户端、真实沙箱）：`steps/calls = 2.5–2.7`、`calls/决策点 = 1.07`。因此上表中**含基线的行按 `D·t_d` 正常计算**，不再需要 ×3.1。

**硬约束**：
- `E_gen` 由 MOD-06 按上式统一计算，超出 `loop.max_episodes_per_generation`（2026-10-06 D-1 由 170 定为 **120**）即拒绝启动。MOD-07 不再单独设成本上限。
- 并发一律采用 P-10 的「局级并发 + 共享串行推理队列」，不得每个 worker 各载一份模型。
- 任何声称「n 局在 x 分钟内完成」的 NFR 都必须由本式推出。

**✅ 2026-10-06 M2 开工定价（D-1 成本 / D-2 难度 / D-3 功效，见 [M2 开工检查单 §2](tasks/2026-10-06-M2开工检查单.md)）**：
- **D-1 成本口径落定**：`loop.max_episodes_per_generation = 120`、`C = n_candidates = 1`、`S = smoke_n = 2`、`H = heldout_n = 40`、`K = 30`。按 `E_gen = K + (1+C)(S+H)` → **E_gen = 30 + 2×42 = 114 局 ≤ 120**（上限留 6 局余量）。按 n=10 实测单价 **19.6 min/局**（`D` 均值 97.1）→ **≈37 h/代**（按上限 120 算是 ≈39 h）。`loop.n_candidates == evolve.n_candidates` 有自动校验（`config.py:558`），但 **`loop.smoke_n` 与 `evolve.smoke_n` 没有**——改 `S` 必须两处同步改。**`H` 的边际成本是 `(1+C)` 支臂**：C=1 时 H 每 +10 局 = +20 局 = **+6.5 h/代**（不是单臂的 +3.3 h）；且 H=50 会让 E_gen=134 > 120，即 **H=40 是用满本上限的最大值**。**⚠️ 2026-10-14 修订（D-M3-7）**：公式、默认值与「与上限 120 比较」的口径都不变（114 是保守上限）；但 parent 的 heldout 改为按 play 面指纹继承后，继承命中时**实际每 cycle = 74 局**（`K 30 + parent 冒烟 2 + 候选(冒烟 2 + heldout 40)`）⇒ 本行的「≈37 h/代」（本地 8B 口径）只在继承未命中、退回实测时成立。
- **D-2 难度口径：混合裁决**。默认局保持 hard × 6（M3 主口径），M2 用 medium × 6 做 bring-up（见上方「bring-up 口径」）。由此**遗留一项必须在 MOD-06 联调前写进判据**：hard × 6 上 `fixed_prompt` 基线 win_rate = 0.0%，M3 的「相对基线提升」不能建在 win_rate 上，须改用 `mean_stages_cleared` / `mean_battles_won`（与下方 D-3 同一条结论，对齐 MOD-07 C2 已有的 `metric ∈ {win_rate, mean_stages_cleared, mean_hearts_left}`）。**⚠️ 2026-10-14 修订（F62）**：C2 已改判「候选 `play_hash` ≠ 起点 `play_hash`」，**不再选指标**；上表那组指标现为 `paired["by_metric"]` 的**报告项**，判据 2 的归因仍按本节与 B-39 的口径（连续轴优先）。
- **D-3 功效口径（H 与 n_rep 不变，改两处判据）**：① **H 保持 40**、`n_rep` 保持 1（B-14 已定 `eval_temperature=0.0` + `decode_seed=0`；全面 `n_rep=3` 在 heldout 臂上是 **+52 h/代**，代价高于整代预算），另在 M3 前做**一次性确定性验证**（H1 前 5 个 seed 重跑对 sha256，≈1.6 h），失败走既有的 `E_EVAL_NONDETERMINISTIC → n_rep=3` 升级路径；② `evolve.win_rate_tol: 0.0 → 0.05`，`evolve.gap_threshold` 保持 0.15（H 未变，不随 H 重标；等 §3.2 冒烟产出第一批 train/heldout 配对后再定）。
  > **⚠️ 2026-10-11 修订（D-M3-3 裁决 A，B-65）**：本条①的「H1 前 5 个 seed 重跑对 sha256」**在主力臂上不成立**——`C1` 是 API 臂（`api_supports_seed=false`，实测字节相同的请求 9 次返回 9 个不同响应），端到端逐字节可复现物理上不可能，跑这 ≈1.6 h 必然失败，且失败**不代表管线有缺陷**。口径已按层改写（见 §8.1 的按层承诺）：本地臂硬执行 G3（`E_EVAL_NONDETERMINISTIC`）、API 臂记 `eval.nondeterministic`。**`n_rep` 仍为 1**（`n_rep=3` 的 +52 h/代 结论不变）。
- **D-3 的统计依据（n=40 下 win_rate 无法坐实 4–5pp，连续指标可以）**：精确配对 McNemar 在 `p01 − p10 = 0.05` 时的功效——每 seed 不一致率 q=0.10 时 n=40→**0.03**、n=80→0.17、n=160→0.43、n=320→0.78；q=0.05 时 n=40→0.01、n=160→0.82。即 MOD-08 §8.2 的「n=40 最小可判不一致对数 k=6」等价于 **n=40 只能判 ≥15pp 的净跳变**（Δ=15pp→power 0.57、20pp→0.84）。而连续指标 `mean_stages_cleared`（0–6）在 n=40、配对 sd 0.5 / 0.8 下：Δ=0.30 关（= 6 关制的 **5.0pp**）→ power **0.97 / 0.66**。故 **M3 的「4–5pp 量级增益」以 `mean_stages_cleared` 的配对 bootstrap 为准，`win_rate` 降为报告项 + 粗门（<15pp 不用它下结论）**，并保留 MOD-08 报告里的 `power_note`。

**✅ 2026-10-11 M3 主口径落定（D-M3-1，用户裁决 A；见 [M3 验收前置条件确认 §D-M3-1](tasks/2026-10-11-M3验收前置条件确认.md)）**：

D-2 的难度口径在 2026-10-09 被重开后一直未再裁决，而 M3 的三项判据全部锚在「哪个局型 + 哪个指标」上。**裁决 A：`hard × 6` 是 M3 唯一主口径，`win_rate` 保持第一判定量**。三条要点：

1. **局型 = `hard × 6`（主口径）**，`configs/default.yaml` 的 `env.episode.difficulty: hard` **不动**（`difficulty` 参与 `config_hash`，实测 hard → `141bef5f…`、medium → `2dc294eb…` ⇒ 改它等于换 run-id 族，会作废与 §8.6 历史参考值（启发式 45.5%、`D` p50=96）的可比性）。
2. **`medium × 6` 降为「外部效度」报告口径**：同一代在 medium 上**可选**再测一次并作为报告项落 `metrics/`，**不进判据 1/2/3 的判定路径**。依据是同一批 seed（旧四位 2000–2009 / n=10）上 `C1` 实测 hard×6 `win_rate=10%` 而 medium×6 `=80%`——medium 的 80% 已贴天花板（6 关制的上限），「单调不降」在上面没有上升空间。
3. **判据 1 的轴 = `heldout.win_rate`**（`ci_lo(g) ≥ win_rate(g−1) − 0.02` 或 `P(gap>0) ≥ 0.90`，且 ≥1 次 accept），**判据 2 的「4–5pp 量级」仍由 `mean_stages_cleared` 的配对 bootstrap 承担**（D-3 原结论不变），`mean_stages_cleared` / `mean_battles_won` 作并列报告证据。

**诚实边界（本裁决的已知代价，不得在 M3 报告里隐去）**：
- `C1` 在 hard × 6 上 `win_rate=10%`（n=10 旧协议实测）⇒ 判据 1 的单调性判定在 10% 附近**由 1–2 局的抖动主导**（1 局 = 2.5pp，`ci_lo ≥ prev − 0.02` 的容差不足 1 局）。因此 **判据 1 通过只说明「没有显著退步」，不足以单独证明「有提升」**——提升结论必须由判据 2 的连续指标给出。
- hard × 6 上 `fixed_prompt` 基线 `win_rate = 0.0%`（n=10，`runs/m1-real-h1n10b`）⇒ 判据 1 若用 `win_rate` 与基线对比，方向可能是**反的**；判据 2 的基线对照因此以 `mean_stages_cleared` 为主。
- hard × 6 实测 `truncation_rate=0.30`（撞内核 `maxTurnsPerBattle=60`），截断局按 lose 计入 `n` ⇒ 有效样本被持续压低。
- **未做**：`hard × 6` 的 `C1` 基线（`random`/`heuristic`/`fixed_prompt`）**从未在新 H1 六位区间（731000–731039）上测过**（旧的 40%/0% 都是四位区间 n=10 的结果）⇒ 判据 2 的「相对最好基线」在新协议下**尚无实测锚点**，起跑前必须补（付费，需用户点头）。

**✅ 2026-10-07 §3.1 实测回填与 C5 标定（裁决 A+C，见 [§3.1 实测报告](reviews/2026-10-07-M2-S31-full-agent基线实测.md)）**：
- **实测（full `PolicyAgent`，H1 子集 seed 2000–2009，n=10，eval 模式，`runs/m2-s31-full-baseline/`）**：`illegal_rate` **0.3016**、`fallback_rate` **0.2146**、`truncation_rate` **0.10**、`win_rate` 0.0（CI [0.000, 0.2775]）、`mean_stages_cleared` 3.00、`mean_battles_won` 7.50、`mean_decisions` 89.0（决策点 121.7）；**n=10 / degraded_n=0 / n_dropped=0**，墙钟 3.51 h。对照 `fixed_prompt` 基线（0.3021 / 0.1810 / 0.0）——**full agent 在同一量级，且 fallback 与 truncation 更差**。
- **裁定 A（现在先松开）**：`evolve.max_illegal_rate` 0.05→**0.35**、`max_fallback_rate` 0.10→**0.25**、`max_truncation_rate` 0.05→**0.15**（实测上界 + 余量）。原阈值下超限 **6.03× / 2.15× / 2.00×**，C5 会 reject 每一个候选（parent 也是如此），M2 验收判据「闭环跑通 ≥1 轮可判定的演化」不可能达成。**副作用已知**：C5 由此成为非约束门（任何候选都能过），诊断价值临时让位于 M2 判据。
- **裁定 C（M3 收紧路径）**：C5 由**绝对门**改为**相对门** `candidate.<metric> ≤ parent.<metric> + tol`（illegal / fallback / truncation 同构），保留「抓退化」的诊断力；`tol` 待 §3.2 冒烟产出第一批 train/heldout 配对后标定。**不得**用「把 A 的新值改回 0.05」的方式收紧。
  > **✅ 2026-10-11 落地（B-64，D-M3-2 裁决 E，用户）**：裁定 C **已执行完毕**，并比原计划多补了 truncation 的绝对 backstop。
  > 三轴同构判定式 `candidate ≤ min(parent + tol, backstop)`：`illegal_tol = 0.05` / `fallback_tol = 0.05` /
  > `truncation_tol = 0.05` / `truncation_backstop = 0.60`；`max_illegal_rate`(0.35) / `max_fallback_rate`(0.25)
  > **保留但语义降为 backstop**（键名不变 ⇒ 老配置仍可加载）。
  > **为什么必须收紧（实测）**：`C1` 实测 illegal 0.0318 / fallback 0.0017，而绝对门是 0.35 / 0.25 ⇒ 松 **11× / 147×**，
  > 等于非约束门。后果不是「少抓一个坏候选」，而是**判据 2 的归因结论不成立**（候选可靠推高非法率换 `win_rate` 而照样过闸）。
  > **`tol` 的起点值论证与诚实边界**（0.05 不是 §3.2 的实测标定值；按轴分列：illegal/fallback 是步级、
  > truncation 是局级）见 [MOD-07 §8.2.1](prd/MOD-07-evolve-PRD.md)。
- **根因判定（排除渲染/契约缺陷）**：离线复现 2,220 个决策点的提示词组装，**F13 压缩渲染 0 次触发**（最大 `token_estimate` 2,694 < 预算 3,072，最大 36 条合法动作）→ 模型看到的是**完整** `legal_actions`（含真实 `dieId`×`slotId` 配对）；非法尝试 **65.2% 是 `play_face`**（模板第 6 条已明确警告的配对问题）；`llm.call` 的 `grammar_used` **1,892/1,892 = false**（MLX 无语法约束）。故 0.30 是 8B 的能力/遵从问题；要压到门内需**语法约束解码**（MOD-03 工程）或提示词改造，**不作为 M2 的前置**。

**⚠️ 2026-10-07 D-2 medium × 6 基线实测（bring-up 口径的判据检验，run `runs/m2-d2-medium6-h1n10/`）**：
- **三臂（H1 子集 seed 2000–2009，n=10，同源）**：`random` **0/10 = 0.0%**；`heuristic` **7/10 = 70.0%**（CI [0.3968, 0.8922]）；`fixed_prompt`（真实 8B）**0/10 = 0.0%**（CI [0.000, 0.2775]）、`mean_stages_cleared` **2.50**、`mean_battles_won` 4.80、`illegal_rate` 0.2829、`fallback_rate` 0.1795、`truncation_rate` 0.10、**n=10 / degraded_n=0 / n_dropped=0**，墙钟 **2.574 h**。内核侧对照（`node src/game/tests/bot-match.js 200 6 medium 1 --stats`）通关率 **91.0%**、env 步 249/301、决策点 92/103（已并入上表）。
- **对照 hard × 6（同 seed 集，`runs/m1-real-h1n10b/`）**：`fixed_prompt` 0/10、`mean_stages_cleared` 2.40、`mean_battles_won` 6.10、`illegal_rate` 0.3021、`decisions_llm` 均值 97.1、墙钟 3.32 h。
- **判定：medium × 6 没有达成 D-2 的目的——对 8B 的 `win_rate` 仍是地板。** 检查单 D-2 的检验条款只写了天花板侧（通关率→100% 或 `mean_stages_cleared`→6），而实际失败在**地板侧**：8B 通关率 0.0% 与 hard × 6 逐位相同，`mean_stages_cleared` 只从 2.40 挪到 2.50（0–6 制），`mean_battles_won` 反而 6.10 → 4.80。可用信号只剩连续指标（2.5 落在中段），而这正是 D-3 已指定的 M3 判据轴。
- **成本口径（若 bring-up 用 medium × 6）**：`D`（`decisions_llm`）均值 **83.7**（hard 97.1，−13.8%）、单局墙钟 **15.44 min**（hard 19.6–19.9 min，−22%）⇒ `E_gen=114` 局 ≈ **29.3 h/代**。**M3 主口径仍是 hard × 6，故 37 h/代 不变**；本行只用于 bring-up 的成本估算。
- **前置缺陷（F26，已修 `cf934fc`）**：`baseline`/`play` 曾**完全忽略** `env.episode.*`（`SandboxRunner` 落到 `EpisodeConfig()` 的代码默认 hard × 6），使第一次 medium 补测与 hard × 6 逐位相同。**本行的数字是修复后重跑的**；M1 归档未受影响（代码默认恰等于当时配置默认）。溯源缺口另见 F27：逐臂归档 `baselines/*.json` 与 `registry.csv` 不记录 episode 口径。
- **待裁决（D-2 再调）**：检查单原文给的两条路都需重新评估——**medium × 10 对启发式只有 24.0%（比 hard × 6 更难），大概率让 8B 的胜率地板更深**；若目标是让 8B 的 `win_rate` 离开地板，应测**对启发式已到天花板、但对 8B 可能落在中段的 3 关局型**（`hard × 3` 启发式 97.0%、`medium × 3`/`easy × 3` 100.0%），或按 D-3 的结论直接**放弃 win_rate 轴、全用连续指标**。

**✅ 2026-10-09 臂与成本口径回填（N1 裁决：M2 闭环先在 DeepSeek 臂上跑；依据 [Issue #1 落地报告](tasks/2026-10-08-issue-1-角色解耦落地报告.md) 与 [M2 开工检查单 §8](tasks/2026-10-06-M2开工检查单.md)）**：

- **实验臂（`experiment.arm`）成为一等配置**：六个臂只做**角色差分**，`harness` 字段只**登记**该臂的 harness 是否演化；空串 = 该角色不绑定、继续向下继承。臂定义计入 `config_hash` ⇒ 换臂即换 run-id 族（F10），六臂产物不会互相污染。
  - ⚠️ **已知限制 F29（2026-10-09 复检登记）**：`harness` 字段**只做登记**——`ExperimentConfig.harness_mode()` 的唯一消费者是 provenance（`roles.py` 把它写进 `models_provenance.json`），**没有任何代码据此开关演化**。即 **`C0`（fixed）与 `C1`（evolve）当前行为等价**：选 `C0` 跑闭环照样会演化 harness。强制「fixed 臂不演化」属 Issue #1 任务 6（**未做**）。因此本节的「主力臂 = `C1`」在**行为上**等价于「把三角色钉到 `deepseek-flash`」；引用臂名时不要据此推断 harness 被冻结。

- **✅ 缺陷 F30（MOD-06 评估并发污染单个 `SandboxEnv`）—— 2026-10-09 已修（方案 A；本版登记 B-46）**：§3.2 冒烟（`C1` + `medium×6`）跑到 5/40 局时被主动中止——gen-000 基线读数 `fallback_rate≈0.29` / `illegal_rate≈0.285`，而同臂/同模型/同难度/同 seed、**同一时间窗并发**的 `play` 路径是 `0.000` / `0.032`（⇒ 差在代码路径，不在模型）。**根因**：`evaluate_arm(n_workers=cfg.evals.n_workers=2)` 用两个线程驱动**同一个** `EpisodeRunner`，而它只持有一个 `SandboxEnv` 实例且 `close_env=False`；`SandboxEnv` 承载全部局状态，两局的 `reset`/`step` 交错 ⇒ 观测串局 ⇒ 模型按**别的局**的相位出动作（典型签名：战斗阶段出现 `map_choose`，而当前合法 kind 只有 `discard_die/end_turn/play_face/use_ability`）⇒ 语义非法 ⇒ 重试耗尽 ⇒ fallback。对照 MOD-08 的 `SandboxRunner` 在 `run_episode` 内**每局新建 env**，故 `play` 干净。**危害等级**：污染的是**被测量的量本身**（比 F28 的门错位更靠上游），且完全静默（无报错、退出码 0）。**修复**：`loop/cli.py` 由 `env=<共享实例>, close_env=False` 改为注入 `env_factory=_env_factory(cfg)`（每局 `episode_id=key`、局末 `close()`，与 `SandboxRunner` 同构）；构造代价 1.1 ms/局。**护栏**：`tests/integration/test_loop_env_isolation.py` 从组合根装配，断言 `runner.env is None` 且 `n_workers=1` 与 `=2` 的逐局结果**逐字段一致**（含 `seq_hash`）；`git stash` 掉修复后 **5/5 稳定失败**，签名即 F30。独立复现（H1 全 40 局、`heuristic` 臂）：共享 env **13/40 局有非法动作、0/40 胜**；每局新 env **0/40、18/40 = 45.0%**（与 `greedy_counter` hard×6 **45.5%** 一致）。
  - **影响范围（别扩大也别缩小）**：只污染 **MOD-06 `loop run` 自己的评估口**。C0 臂既有实测（`runs/r-c0-hard6-h1n10/`、`runs/r-c0-medium6-h1n10/` 的 `play-llm` 轨迹）走的是 MOD-08 `SandboxRunner`（每局新 env、`n_workers=1`）⇒ **未被污染**，B-45 与 D-2 的证据基础成立。
  - ⚠️ **残留 F31（低危，未修）**：`loop/runner.py:446-448`/`:219` 局末 `state.counters.illegal_actions` / `llm_timeouts` / `io_retries += …` 是跨线程读改写，`n_workers=2` 下理论丢更新；这三个计数器在 `dice_ratchet/` 内**零读取点**（不进门禁/裁决），仅影响 `state.json` 的上报值。

| 臂 | policy | reflection | proposer | harness | 用途 |
|---|---|---|---|---|---|
| `A0` | —（继承 `inference.model_id`） | — | — | fixed | 本地 8B 基线对照 |
| `A1`（**默认**） | — | — | — | evolve | 本地 8B 闭环 |
| `B0` | — | — | deepseek-flash | fixed | 只换 proposer |
| `B1` | — | — | deepseek-flash | evolve | proposer 对照 |
| `C0` | deepseek-flash | deepseek-flash | deepseek-flash | fixed | 全 API 对照 |
| **`C1`** | deepseek-flash | deepseek-flash | deepseek-flash | evolve | **M2 闭环主力臂（N1 裁决，2026-10-09）** |

- **角色绑定优先级（唯一实现 `inference/roles.py::_resolve_role`）**：`experiment.roles.<role>` > `experiment.arms[arm].<role>` > `models.<role>` > `inference.model_id`。未知臂/未知角色/空 `model_id`/注册表里没有的模型/`provider=api` 但未登记 key 环境变量 → **fail-closed**（绝不静默降级到别的模型）。三角色各自落 `runs/<run_id>/models_provenance.json`（`model_id`/`provider`/`backend`/`model_revision`/`online`/`api_key_present` + 臂 + `harness` 模式 + `config_hash`）。臂绑定经组合根注入，**MOD-04/06/07 都拿得到**（`loop run --agent llm` 与 MOD-07 提案走同一个 `RoleModelProvider`）。
- **`backend: deepseek`（MOD-03 新增后端）**：`provider=api` 的外部模型。注册表新增 `deepseek-flash` 与 `deepseek-v4-pro`（实测该账号/接口版本**没有** `deepseek-chat`，旧名已下线）。三条硬口径：① `api_supports_seed=false`——DeepSeek **不承诺跨时间确定性**（实测字节相同的请求 9 次返回 9 个不同响应）⇒ 采样照记、响应按 request hash 归档、变化即标 `llm.nondeterministic`；② `api_thinking: disabled`——flash 默认开思考模式会把 `max_tokens` 吃在思维链上、`content` 返空，导致每次白花一次调用；③ key 只经 `inference/secrets.py` 从真实环境变量或本地 `.env` 读（**真实环境变量优先**），**只进内存**，绝不入 `harness/`、run JSON、事件或 Git。
- **2026-10-08/09 实测（`C0` 臂，H1 子集 seed 2000–2009，n=10，`temperature=0.0`）**：

| 臂 / 局型 | `win_rate` | CI95 | `mean_stages_cleared` | `mean_decisions` | `illegal_rate` | `fallback_rate` | `truncation_rate` |
|---|---|---|---|---|---|---|---|
| `C0` DeepSeek · **medium × 6** | **8/10 = 80.0%** | [49.0%, 94.3%] | 5.6/6 | 98.3 | 0.0316 | 0.0 | 0.00 |
| `C0` DeepSeek · **hard × 6** | **1/10 = 10.0%** | [1.8%, 40.4%] | 4.3/6 | 124.5 | 0.0291 | 0.0008 | **0.30** |
| `heuristic` · medium × 6 | 70.0% | [39.7%, 89.2%] | 5.7/6 | 97.1 | 0.0 | 0.0 | 0.00 |
| `heuristic` · hard × 6 | 40.0% | [16.8%, 68.7%] | 5.4/6 | 104.1 | 0.0 | 0.0 | 0.00 |

  同 run 内 `random` 两难度均 0/10；全部 `status=200` / `degraded_n=0` / `fallback_decisions=0` / `n_dropped=0`。**注意**：`hard × 6 = 10%` 这一行**与 8B 的结论相反**——`win_rate` 在 API 臂上**有**方差（8B 在两难度上都是 0.0%）。`hard × 6` 的 `truncation_rate=0.30` 来自 **3/10 局撞内核单场战斗回合上限**（`src/game/kernel/combat.js:460` → `state.truncationReason='E_KERNEL_TURN_LIMIT'`，`maxTurnsPerBattle=60`），是**局型 × 策略强度**的合成值而非纯局型属性（同 run 的 `heuristic` 为 0.00）。

- **成本模型按臂分列（实测；`play` 相位墙钟 = 第三个 `eval.start` → 最后一个事件）**：

| 臂 / 局型 | 单局墙钟 | 后端调用（n=10） | 花费 | `E_gen = 114 局/代` |
|---|---|---|---|---|
| 本地 8B（`m1-real-h1n10b`，上方全部定价的依据） | 19.6 min | 1,551 | 0（本地） | **≈37 h/代** |
| **`C0`/`C1` DeepSeek · hard × 6** | **2.52 min** | 1,360 | ¥0.879/局 | **≈4.8 h/代 ≈ ¥100/代** |
| **`C0`/`C1` DeepSeek · medium × 6** | **2.09 min** | 1,080 | ¥0.733/局 | **≈4.0 h/代 ≈ ¥84/代** |

  ⇒ **上方「19.6 min/局 / 37 h/代」只对本地 8B 成立**。API 臂下 `E_gen` 的约束由**墙钟转为 API 花费/限流**（快约 8×）。连带结论：`inference.cache_prompt`（KV 前缀复用）对 API 臂**无收益**（无本地 prefill），只对本地臂有效。
  ⇒ **⚠️ 2026-10-14 修订（D-M3-7）**：上表的每代成本按 `E_gen = 114 局/代` 折算；公式与默认值未变（114 仍是与上限 120 比较的保守值），但 parent 的 heldout 按 play 面指纹继承命中时**实际每 cycle = 74 局**。按本 run（`m3-formal`）实测账户口径 ≈¥0.52/局 ⇒ `C1`·hard×6 ≈ **¥38/代**；官方高峰价 ¥0.833–0.879/局 ⇒ ≈ **¥62–65/代**。
- **✅ 缺陷 F28（C5 绝对门与臂耦合）—— 2026-10-09 已处置（用户裁决 ②+③，登记 B-45）**：C5 的三阈值原按**本地 8B** 实测标定（B-40），而 M2 主力臂同日改为 `C1`（DeepSeek）。复算：`hard × 6` 的 `truncation_rate = 0.30 > 门 0.15` ⇒ **C5 会 reject 每一个候选**（`reject` / `C5:illegal/fallback/truncation` / `E_EVOLVE_ILLEGAL_RATE`），闭环形式上跑通、实则一个候选都留不下——**正是 §3.1 要防的失败模式，只是从 illegal 轴搬到了 truncation 轴**；同时 illegal/fallback 两门反向松了约 10×（0.03 vs 0.35、0.00 vs 0.25）。
  - **③ 门结构（本次落地）**：**C5 = `illegal` / `fallback` 绝对门（0.35 / 0.25）+ `truncation` 相对门** `candidate.truncation_rate ≤ parent.truncation_rate + evolve.truncation_tol`，**`truncation_tol = 0.05`**（n=40 的 2 局，同 `win_rate_tol` 的依据 D-3）。**绝对门 `max_truncation_rate` 已从 `configs/default.yaml` 与 `EvolveConfig` 中删除**（不是改成 0.35 —— 重标会在下次换臂时重演 F28）。理由：`truncation` 是**局型 × 策略强度**的合成属性（同 run 内 `heuristic` 0.00、DeepSeek hard×6 0.30、8B hard×6 0.10），不像 `illegal`/`fallback` 那样有跨臂的绝对意义。
  - **配套 fail-closed**：相对门需要 `parent.truncation_rate`，故它进 **C0 前置完备性检查**——parent 缺该字段记 `C0:incomplete_report` 并 reject，**不静默跳门**（跳过会让 C5 对 truncation 失去诊断力）。
  - **② 局型**：bring-up / §3.2 冒烟用 `medium × 6`（实测 truncation = 0.00，`win_rate` 有方差 80% vs 70%）；默认局仍 `hard × 6`。
  - **已知放宽（诚实边界）**：相对门是**对称**放宽——parent 自身截断率很高时，候选只要不比它差就放行（如 parent = 0.90 时候选 0.60 也过）。本轮**不设绝对 backstop**（按裁决「只对 illegal+fallback 设绝对门」）；该行为已由参数化用例显式钉住，M3 标定 `tol` 时再评估是否补 backstop。
  - **落点**：`evolve/judge.py`（`_C5_TRUNCATION_TOL` + C0 parent 校验）、`infra/config.py` / `configs/default.yaml`（键替换）、`tests/unit/evolve/test_evolve_judge.py`（3 组新护栏）、MOD-07 PRD §8.2/§8.2.1/§8.2.2/§9、`docs/html/MOD-07-evolve.html`。
- **D-2 难度口径随之重开**：2026-10-07 的「medium × 6 对 M2 没有信号」只对 8B 成立；对 `C1` 臂实测 **medium × 6 = 80% vs hard × 6 = 10%**，两难度**都有**方差（对照启发式 70% / 40%）。D-2 需按当前臂重裁（检查单 §8.5 R3）。

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
| B-07 | MOD-08 | **seed 协议区间**（唯一权威，**v2.6 更新为六位端口空间**）：train `1000–1999`（第 g 代取 `1000+30(g-1) … +29`）、heldout `731000–731099` 分三篮子 H1=`731000–731039`（代际裁判 n=40）/ H2=`731040–731079`（消融与复核）/ H3=`731080–731099`（应急）、test `947000–947099`（锁定，每里程碑单次解锁）。**v1.1 已据此修正 MOD-04/06/07/09 与本文档的命令示例**（原先误用 `train:1-5`、`heldout:1001-1040`）；**v2.6（F45-2）把 heldout/test 从四位（H1 2000–2039 / test 9000–9099）移到六位**——原四位空间与耗时/预算/日历字面量撞车，迫使审计维护两张人工死名单；`INV-5` 的**字面口径不需要改**（这正是选 F45-2 而非「放宽数字串规则」的收益，见 §12 v2.6） | §4.6、INV-5 |
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
| P-25 | 采纳；C5 改为 `illegal_rate ≤ 0.05`、`fallback_rate ≤ 0.10`、`truncation_rate ≤ 0.05`（不再用回合数）；M3 另需 ≥1 次 accept —— **⚠️ 三个数值已于 v1.8/B-40 按 §3.1 实测重标定为 0.35 / 0.25 / 0.15（先松开），M3 由 B-41 的相对门收紧；本行的 0.05/0.10/0.05 是历史裁决，不再是现行口径** |
| P-28 | 采纳，即 B-09 |
| P-29 | 采纳，即 B-10/B-12 |
| P-30 | 采纳，即 B-11 |

### 11.1c v1.4 已回填（2026-09-30 17:49，11 项待裁决全部确认，另收录 PRD 内 3 项未登记决策）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-23 | P-01（MOD-04） | `Action` 定义在 `env/actions.py`，由 `sandbox/api.py` re-export，L1+ 只许从门面导入 | §1 实施细则 |
| B-24 | P-06（MOD-02） | 全状态只由 `SandboxEnv` 私有持有，`dice_ratchet.env` 对 L1+ 不可导入（原 `build_full_state()` 方案按 MOD-01 v1.0 改为等价封装） | §1 实施细则 |
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

### 11.1d v1.7 已回填（2026-10-06，M2 开工 D-1/D-2/D-3 裁决）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-37 | M2 开工 D-1 | 成本硬约束落定为 `max_episodes_per_generation=120 / C=1 / S=2 / H=40 / K=30` → `E_gen=114` 局 ≤ 120（≈37 h/代）；`n_candidates` 两处与 `smoke_n` 两处的同步要求、`H` 的 `(1+C)` 支臂边际成本（+10 H = +6.5 h）一并写明 | §8.6 |
| B-38 | M2 开工 D-2 | 难度**混合裁决**：默认局保持 hard × 6（M3 主口径），M2 联调/冒烟用 medium × 6 bring-up（run 级 override）；hard × 6 的 `fixed_prompt` 基线 win_rate=0.0% ⇒ M3 判据改用 `mean_stages_cleared`/`mean_battles_won`；medium × 6 的基线率与 `D` 待实测 | §8.6 |
| B-39 | M2 开工 D-3 | 功效口径：`H` 保持 40、`n_rep` 保持 1（一次性确定性验证 + `E_EVAL_NONDETERMINISTIC → n_rep=3` 升级）、`evolve.win_rate_tol` 0.0→0.05、`gap_threshold` 保持 0.15 暂不重标；**M3 的 4–5pp 结论以 `mean_stages_cleared` 配对 bootstrap 为准**（n=40 的 win_rate 只能判 ≥15pp） | §8.6、MOD-08 §8.2/§13、MOD-07 §8.2 |

### 11.1e v1.8 已回填（2026-10-07，§3.1 实测 → C5 标定）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-40 | [§3.1 实测](reviews/2026-10-07-M2-S31-full-agent基线实测.md) | **C5 三阈值按实测先松开**：`max_illegal_rate` 0.05→**0.35**、`max_fallback_rate` 0.10→**0.25**、`max_truncation_rate` 0.05→**0.15**。依据：full `PolicyAgent` 实测 0.3016 / 0.2146 / 0.10（原门超限 6.03× / 2.15× / 2.00×，会 reject 每个候选）。**写明「先松开、M3 再收紧」**，并声明副作用（C5 临时成为非约束门） | §8.6、MOD-07 §8.2/§9、`configs/default.yaml`、`config.py` |
| B-41 | [§3.1 实测](reviews/2026-10-07-M2-S31-full-agent基线实测.md) | **C5 的 M3 收紧路径 = 绝对门改相对门**：`candidate.<metric> ≤ parent.<metric> + tol`（illegal/fallback/truncation 同构），保留抓退化的诊断力；`tol` 待 §3.2 冒烟产出第一批 train/heldout 配对后标定。**禁止用「把 B-40 的值改回 0.05」来收紧** | §8.6、MOD-07 §8.2 |

### 11.1f v2.0 已回填（2026-10-09，实验臂/角色解耦 + DeepSeek 后端 + M2 闭环臂裁决）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-42 | [Issue #1 落地报告](tasks/2026-10-08-issue-1-角色解耦落地报告.md) | **三角色解耦 + 实验臂成为一等配置**：`policy`/`reflection`/`proposer` 只依赖统一 `InferenceClient` 契约，绑定全部来自配置（优先级 `experiment.roles.<role>` > `experiment.arms[arm].<role>` > `models.<role>` > `inference.model_id`，唯一实现 `_resolve_role`，fail-closed）；新增 `experiment.arm` 与六臂 `A0/A1/B0/B1/C0/C1`（只做角色差分，`harness` 字段只登记是否演化）；臂定义计入 `config_hash`（换臂即换 run-id 族）。新增 MOD-03 `backend: deepseek`（`provider=api`、`api_supports_seed=false`、`api_thinking=disabled`、`inference/secrets.py` 的 `.env` 读取、`models_provenance.json` 三角色溯源） | §4.7、§8.6、MOD-03 §6/§7、MOD-06/MOD-07 |
| B-43 | M2 开工检查单 §8（N1 裁决，用户） | **M2 闭环主力臂 = `C1`（policy/reflection/proposer 全 `deepseek-flash` + `harness: evolve`）**。理由：API 臂单局 2.09–2.52 min（本地 8B 19.6 min）⇒ `E_gen=114` ≈ 4.0–4.8 h/代、≈¥84–100/代，闭环能在当晚跑通；且 `win_rate` 在 API 臂上有方差（medium×6 = 80% / hard×6 = 10%），而 8B 两难度都是 0.0%。**成本口径随之由墙钟转为 API 花费/限流** | §8.6 |
| B-44 | M2 开工检查单 §8（复检） | **登记 F28：C5 绝对门与臂耦合**——C5 三阈值按本地 8B 标定（B-40），换臂后 `hard×6` 的 `truncation_rate=0.30 > 0.15` 会 reject 每个候选（illegal/fallback 反向松约 10×）。**处置候选**（按当前臂重标 / bring-up 用 medium×6 / truncation 并入相对门）；同时 D-2 难度口径按当前臂**重开** | §8.6、MOD-07 §8.2、[检查单 §8.5](tasks/2026-10-06-M2开工检查单.md) |

### 11.1g v2.1 已回填（2026-10-09，F28 的处置：C5 truncation 改相对门 + bring-up 用 medium × 6）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-45 | M2 开工检查单 §8（N2 裁决，用户选 **②+③**） | **C5 由「三项绝对门」改为「`illegal`/`fallback` 绝对门 + `truncation` 相对门」**：`candidate.truncation_rate ≤ parent.truncation_rate + evolve.truncation_tol`，**`truncation_tol = 0.05`**（n=40 的 2 局，同 `win_rate_tol` 依据）；**绝对门 `max_truncation_rate` 删除**（重标会在下次换臂时重演 F28）。**配套 fail-closed**：`parent.truncation_rate` 进 C0 前置完备性检查，缺则 `C0:incomplete_report`（不静默跳门）。**② 局型**：bring-up / §3.2 冒烟用 `medium × 6`（实测 truncation = 0.00、`win_rate` 有方差 80% vs 70%），默认局仍 `hard × 6`。**已知放宽**：相对门对称放宽（parent 0.90 时候选 0.60 也过），本轮不设绝对 backstop，M3 标定 `tol` 时再评估 | §8.6、MOD-07 §8.2/§8.2.1/§8.2.2/§9、`evolve/judge.py`、`infra/config.py`、`configs/default.yaml`、`tests/unit/evolve/test_evolve_judge.py`、`docs/html/MOD-07-evolve.html` |

### 11.1h v2.2 已回填（2026-10-09，F30 的处置：MOD-06 改为每局一个 `SandboxEnv`）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-46 | M2 开工检查单 §10（F30 现场；用户选**方案 A**） | **MOD-06 的 `EpisodeRunner` 不得跨评估局共享 `SandboxEnv`**：`loop run` 的装配由 `env=<单个实例>, close_env=False` 改为注入 **`env_factory`**（每局 `_make_env(cfg, episode_id=key)`，由 `_resolve_env` 的 owned 路径在局末 `close()`），与 MOD-08 `SandboxRunner` 的既有做法同构。理由：`evaluate_arm(n_workers=2)` 用多线程驱动同一 runner，而 `SandboxEnv` 是有状态单实例 ⇒ `reset`/`step` 交错、观测串局（实测 `illegal_rate` 0.285 / `fallback_rate` 0.29，同臂 `play` 路径为 0.032 / 0.000）。**配套护栏**：`tests/integration/test_loop_env_isolation.py`（结构面 + 行为面「`n_workers=1` 与 `=2` 逐字段一致」）。**同源约束**：这与 FR-06-14「评估局并发时 MOD-04 不可共享实例」是同一条理由，只是此前只落到了 `policy` 上；`env` 与 `policy` 都必须**按局**实例化。另登记**残留 F31**（局末 `state.counters.*` 的跨线程读改写，仅影响上报值） | §8.6、MOD-06 PRD §9.1、`loop/cli.py`、`tests/integration/test_loop_env_isolation.py` |

### 11.1i v2.3 已回填（2026-10-10，F32–F41 处置：闭环路径的十个死缺陷）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-47 | M2 检查单 §11（干跑链） | **MOD-03 的 `generate_json` 增加可选 `max_tokens`**（按调用方给输出预算）：决策点只需 ~200 token，而 MOD-07 的 harness 提案是「unified diff + rationale + expected_effect」（实测 ~500，且随 `max_diff_chars=4000` 上探）。**禁止**用「全局抬高 `inference.max_tokens`」解决——`guard_context` 按 `est_prompt + max_tokens ≤ context_length` 判定，抬到 2048 会让 `mock`（4096）触发 71 例 `E_LLM_CONTEXT_OVERFLOW` 并吃掉 8B（8192）臂余量。MOD-03 的 `BackendManager` 本就支持 per-call `max_tokens`（已有用例），缺的只是同步门面暴露它 | §4.7、MOD-03 §6/§17、`inference/client.py`、`evolve/proposer.py` |
| B-48 | M2 检查单 §11（干跑链） | **MOD-07 的提案输入必须内联「可改文件」的正文**：候选是 unified diff，hunk 上下文必须与目标文件**逐字符一致**；只给 sha256 时模型对没见过的文件写 diff ⇒ `E_EVOLVE_PATCH_INVALID`。新增 `HarnessSnapshot.contents`（schema → `evolve-harness-snapshot-1.1.0`，逐文件 20 KB 上限，只含候选白名单内的文件）。**范围界定**：`prompts/decision.md` 本身是给 agent 的模板（含 `{{...}}`），故「无未替换占位符」这条只适用**正文块之外** | §4.5、MOD-07 §6.2/§17、`evolve/schemas.py`、`evolve/proposer.py` |
| B-49 | M2 检查单 §11（干跑链） | **补丁应用容忍行偏移、但不容忍 fuzz**：`_apply_hunks` 原先只认 hunk 声明的行号，而模型产出的上下文常常**逐字正确、行号偏几行**（实测偏移 +9）⇒ 每个真实候选都被判 `E_EVOLVE_PATCH_INVALID`。改为在 ±200 行内按**逐字**匹配定位原文块（标准 `patch` 的 offset 语义），**不做 fuzz**（上下文差一个字符即拒），同一距离两侧都命中即判歧义并拒绝 | MOD-07 §6.2/§17、`evolve/proposer.py` |
| B-50 | M2 检查单 §11（干跑链） | **`episode_key` 的 `arm` 位允许 `-smoke` 后缀**（F41）：MOD-07 FR-07-6 的冒烟臂是 `parent-smoke` / `cand-{id}-smoke`，而登记模式不含它 ⇒ 冒烟局每行轨迹失败 → 该局 dropped（0 行）→ `low_dropped` → `E_EVAL_INSUFFICIENT_N` → 冒烟快速淘汰 ⇒ **每个候选在冒烟关即死，闭环零产出**。该语法在**三处**有副本，必须同步：`infra/logging.py`、`schemas/trajectory_line.schema.json`、MOD-09 PRD §6.2（前两者加了一致性用例防漂移） | §4.5、MOD-09 §6.2/§17 |
| B-51 | M2 检查单 §3.2（用户裁决） | **改写 §3.2 验收判据**：由「`harness/versions/` 必须有新版本」改为「**机制性失败为零** + 版本机制被真实使用并有明确 `decision` + gate 判定有信息量」。理由：parent 在 `medium×6` 已能赢 65–80%，小改动拿不到显著增益是**统计结论**（实测首个真实候选正是 `C2:no_significant_gain`、`delta=0.0,p=1.0`）；硬要 accept 会逼着后续「凑一个能过门的改动」，属反向激励 | M2 检查单 §3.2/§7/§11.3、排期 §4 |

### 11.1j v2.4 已回填（2026-10-10，§3.2 实跑合格 + F42–F46）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-52 | M2 检查单 §12（F43） | **生成侧台账与重算报告不得互相覆盖**：`metrics/generations.csv` 由**写报告的一方**（MOD-07 `_finish_report`）自己追写，`eval report` 对**已裁决**的代改写 sidecar `report.eval.json`；台账按 `generation` **upsert**（每代恰好一行）并**保留已裁决的 `decision`/`verdict`**。原先两者写同一个 `generations/gen-NNN/report.json` ⇒ 「产出 csv（判据②）」只能靠「抹掉 MOD-07 的判决（判据①）」 | MOD-08 §13/§17、MOD-07 §6.3、`evals/harness.py`、`evals/cli.py`、`evolve/generation.py` |
| B-53 | M2 检查单 §12（F44/F45） | **`audit leak` 必须按文件形状解析**：`.jsonl` 逐行、`.json` 整文档（并下探 `entry` 取正文）。MOD-06 的 `reflections/<key>.json` 是 51 行 pretty JSON，按 JSONL 解析 ⇒ `E_ENV_AUDIT_IO` ⇒ 退出码 2 ⇒ **任何含反思的 `loop run` 都无法 clean**（判据③不可达）；同一形状问题还让扫描循环的 `MEMORY_NON_CONTENT_KEYS` 豁免被绕过（年份 `2026` 刷假阳性） | MOD-02 §17、`sandbox/audit.py` |
| B-54 | M2 检查单 §12（F45b） | **`LOG_NON_CONTENT_KEYS` 补入 `llm.*` 的纯遥测键**（`max_tokens`/`temperature`/`top_p`/`cost_estimate`/`request_hash`/`response_hash`/`prompt_digest`）。触发点是 B-47 刚加的按调用方预算 `max_tokens: 2048` —— 合法配置值落进 heldout 的 2000–2099 区间即刷 `seed_leak/critical` | MOD-02 §17、`sandbox/audit.py` |
| B-55 | M2 检查单 §12（**待裁决**） | **登记 F46**：`load_episode_results` 的 `generation` 参数**只贴标签、不筛文件** ⇒ `eval report --generation 1` 把 gen-000 基线的 `arm="parent"` 也算进 gen-1（`n=117` vs 实际的 40+40+37）⇒ **台账每代数字没有物理含义**（M1 F5 修了混**臂**、没修混**代**）。修法待裁决 | MOD-08 §17、检查单 §12.4 |

### 11.1k v2.5 已回填（2026-10-10，F47 处置：演化前门禁、失败自清、失败口径）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-56 | M2 检查单 §11（F47-A，用户裁决） | **「演化前门禁」必须真的在演化之前**：MOD-06 在 `loop run` 起跑处（`cmd_loop_run`，`evolve_fn` 非空时）调用 MOD-09 `preflight_worktree_clean()`，**任何推理调用与 gen-000 基线之前**失败（`E_IO_GIT_DIRTY`，退出码 4）。原先唯一调用点在 `commit_harness()` 内部（accept 的第 3 步）⇒ 脏工作区要等整代跑完才拒（实测 3.75 h / ≈¥47）。**范围界定**：只拦会落版本的 run（`--no-evolve`/`loop smoke` 不拦），且不放进 `LoopOrchestrator.run()` 内部（否则程序化嵌入与测试一律被拦）；非 git 路径 → 降级跳过（Spec §8.5） | MOD-09 §6.3 FR-09-14/FR-09-20、MOD-06 §9.3、`infra/git_ops.py`、`loop/generation.py`、`loop/cli.py` |
| B-57 | M2 检查单 §11（F47-C，用户裁决） | **失败路径必须自清自己造的东西**：①`commit_harness` 改为**先门禁后 `git add`**，失败时 `git reset -- <harness>`（原先失败会留下 staged 残留：实测 `MM harness/decision.md` + `A  harness/versions/v2/decision.md`）；②`accept()`/`rollback()` 在**建完快照之后**失败时删掉**本次新建**的那一个快照（`_discard_new_snapshot`，只收调用方记录的新建路径，历史快照与回滚目标绝不删）。残留快照的实测危害不是「后续 accept 报冲突」（`next_version = max(指针, 现有目录)+1` 会跳号），而是**顶掉一个版本号**并留下**可被 `load_version`/`rollback` 取用的幻影版本**（无账本记录、`git_commit` 为空）；③版本冲突分支也改为**失败保守**（`_restore` 回滚工作区） | MOD-09 §6.3、MOD-07 §6.6、`infra/git_ops.py`、`evolve/versioning.py` |
| B-58 | M2 检查单 §11（F47-D，用户裁决） | **基础设施失败 ≠ 裁判拒绝**：accept/rollback 抛 `InfraError` 时记 `decision="error_reject"`（沿用 `loop._call_evolve` 既有词汇），并把它加入 MOD-08 `VERDICT_DECISIONS`（重算台账不得覆盖）。原先 `_finish_report` 写 `decision="reject"`，而 `build_decision_sections` 只看 gate 状态（此时 gate 仍是 `approved`）⇒ **`report.json` 实际写 `decision="accept"` 而磁盘上没有新版本**（幻影 accept；2026-10-10 实测：`outcome.decision='reject'` / `report.decision='accept'` / `versions=['v0']`）。同时 **§3.2 的机制性失败判据由枚举改为按前缀**（`E_EVOLVE_*`/`E_IO_GIT_*`/未捕获异常/非 0 退出），并要求 report 的 `decision`/`error_code` 出现在判定表中 | MOD-07 §6.6、MOD-08 §13/§17、检查单 §3.2/§11、`evolve/generation.py`、`evals/harness.py` |

### 11.1l v2.6 已回填（2026-10-11，F46-A / F42-A+B+C / F45-2）

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-59 | 任务 prompt §3（F46-A，用户裁决 A） | **`eval report` 的 `generation` 必须是真过滤，臂必须显式**。① `load_episode_results` / `read_episode_protocols` / `evaluate_generation` 的训练窗口读取：`generation is not None` 时只保留该代的局（行里的 `generation` 优先，缺失时按文件名 `-g{gen:03d}-` 解析，两者都判不出 → **跳过**并发 `evals.generation_unresolved`），且**不再覆盖** `EpisodeResult.generation`（原先形参无条件盖掉行里的值——正是跨代混算逃过过滤的原因）。② `eval report` 新增 `--arm`：**缺省 = 该代 `report.json` 里的胜出/候选臂**（优先 `decision="accept"`，无则唯一候选，再退 `parent`）；`--arm all` 是**显式申报**的混合池（stdout 与 ext 同时标注 `mixed_pool`），**不得当默认**；坍缩判定改用该代整池（与 `generations_row_from_report` 同口径）。③ **不动** MOD-07 的判决权威性：已裁决的代仍只写 sidecar `report.eval.json`（F43 规则保持）。实测对拍（`runs/m2-s32b-smoke-c1-medium6`）：缺省候选臂 `n=37 / 0.6757`、`--arm parent` `n=40 / 0.6250`，与 MOD-07 报告逐项一致；修复前是 `n=117 / 0.6496`。`report.json` 与 `report.sha256` 重算前后**逐字节未变** | MOD-08 §13/§17、MOD-09 §6（命令面 `--arm`）、`evals/harness.py`、`evals/cli.py`、`infra/cli.py` |
| B-60 | 任务 prompt §4（F42-A/B，用户裁决 A+B） | **每代评估必须有检查点，且复用必须 fail-closed**。① `evaluate_arm` 增加 `resume_results` / `on_result` / `run_dir` / `archive`，**薄转发**给 `run_eval`（不复制执行逻辑）；每个臂落 `metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json` = `ArmResult.as_dict()` + provenance（`generation/split/basket/arm/harness_version/memory_version/n_seeds/n_dropped/config_hash/seeds_hash/protocol_id/episode_config_hash/created_at`）+ **逐局明细**。② 复用规则：provenance **逐字段全等**（`ARM_PROVENANCE_KEYS`，不含 `created_at`）且覆盖完整 seed 列表 → **整臂复用**（0 局 + `evals.arm_reused` 事件）；只覆盖一部分 → 已完成局并入 `resume_results`，只跑缺的；任一不一致/损坏/run 目录不存在 → **不复用**（照常重算）。③ `loop` 的 resume 单元仍是「整代已裁决」（`_cycle_artifacts_exist`），但重入时发 `loop.resume`（含 `completed_arms`）并**不清** `metrics/arms/`。④ 顺带修两个真缺陷：`run_eval` 原先只把 `resume_results` 并进聚合字典而**队列仍会重跑**那些 seed（契约「已完成的局不重跑」实际不成立）；`str(getattr(proto,"seeds_hash","") or "")` 对 `None` 无效（`str(None)=="None"`）⇒ 归档与对拍永远对不上 | MOD-08 §13/§17、MOD-06 §9、`evals/harness.py`、`evolve/judge.py`、`loop/generation.py` |
| B-61 | 任务 prompt §4.3（F42-B，用户裁决 B） | **提案检查点**：MOD-07 在**评估之前**把 `ProposalResult` 落 `generations/gen{gen:03d}/proposals/proposal.json`，连同输入指纹（`run_id`/`generation`/`propose_seed`/`harness_version`/`memory_version`/`prompt_digest`）；resume 时指纹一致 → **复用同一 candidate id 与 diff**、**0 次 proposer 调用**（`evolve.proposal_reused`），不一致/损坏 → 照常重新提案。理由：API 提案**不可复现**（`api_supports_seed=false`），不落检查点则候选 id 必变、`metrics/arms/` 按 `cand-<id>` 命名的归档必然 miss，中断后只能省 parent 侧。`proposer.proposal_inputs()` 让「指纹渲染」与「真正送进 LLM」共用同一句。**诚实边界**：指纹只覆盖**输入**，不含模型权重；`harness_snapshot` 是 prompt 的一部分，因此 `accept` 之后的检查点天然不再命中（那时 parent 已前进，重提案是正确行为）。另：`_parent_arms` 让 `KeyboardInterrupt`/`SystemExit` **穿透**——Ctrl-C 折成「保守 reject」会写出 report.json，让 loop 把该代记成已完成，整代结果白扔 | MOD-07 §6.7/§17、`evolve/checkpoint.py`、`evolve/proposer.py`、`evolve/generation.py` |
| B-62 | 任务 prompt §5（F45-2，用户裁决 2） | **heldout/test 移出四位数字空间（协议级）**。① 新区间：heldout H1 `731000–731039` / H2 `731040–731079` / H3 `731080–731099`、test `947000–947099`、train `1000–1999` 不变；`seeds_hash` = `sha256:686b2618330db5eb8291c1810499dc07fc47a956da5711803015bbb09ec2e904`。② **代际隔离闸（必须补）**：`config_hash` = `sha256(canonical_json(AppConfig))`，而 `SeedsProtocol` 是**另一个文件** ⇒ 换区间不改 `config_hash`；`baselines.baseline_protocol()` 追加 `+seeds-<hash12>`，`eval report` 的 ext provenance 增记 `seeds_protocol_id`/`seeds_protocol_tag`。于是旧局（`…+seeds-2ba235a71a12`）与新局在 `--resume` 的一致性闸上必然不同。③ **删两张人工死名单**（`LOG_NON_CONTENT_KEYS`/`MEMORY_NON_CONTENT_KEYS`/`_content_projection`）⇒ 扫描面比带名单时**更严**；唯一保留的豁免是 `LOG_STRUCTURAL_KEYS = {seed, episode_key, spec}`（框架自己的**播种计划**，不是模型可见内容——不豁免则每个 run 都会因记录了自己的 seed 被判 critical）。④ 修 `evals/seeds.py::_NUM4_RE` 写死 4 位 ⇒ 六位 seed **扫不到** 的静默失效（改 3–7 位）。⑤ **实测**：全量归档 1311 个文件 / 188.0 MB，新区间 **0 命中**，旧区间（反例自检）**799,868 命中**（`ts` 699,305 占 87%）。⑥ **INV-5 措辞不需要改**（保住字面口径正是选 2 而非 1 的收益）。⑦ **代价**：22 个含 heldout/test 局的归档 run 属 protocol v2 证据，与新协议**不可比**（不删、不擅自重跑） | MOD-08 §4/§9/§13/§17、MOD-02 §17、configs/seeds.yaml、configs/default.yaml、`evals/seeds.py`、`evals/baselines.py`、`sandbox/audit.py`、4 个 README |

### 11.1m v2.7 已回填（2026-10-11，M3 验收前置决策 D-M3-1..D-M3-6）

> 来源：[M3 验收前置条件确认](tasks/2026-10-11-M3验收前置条件确认.md) §2。D-M3-1 在本节；
> D-M3-2（C5 三 `tol` + backstop）、D-M3-3（API 臂确定性口径）、D-M3-4（HITL 与样本资格）
> 按「一个决策一个 commit」逐项落在本文档后续小节与对应 PRD 中。

| # | 来源 | 决策 | 回填位置 |
|---|---|---|---|
| B-63 | M3 检查单 §2 D-M3-1（用户裁决 A） | **M3 主口径 = `hard × 6` + `win_rate` 为第一判定量；`medium × 6` 降为外部效度报告项**。① `env.episode.difficulty: hard` **不动**（改动即换 run-id 族，且作废与 §8.6 历史参考值的可比性）。② 判据 1 的轴 = `heldout.win_rate` + `ci_lo/ci_hi` + ≥1 次 accept；**判据 2 的 4–5pp 量级仍由 `mean_stages_cleared` 的配对 bootstrap 承担**（D-3/B-39 不变），`mean_battles_won` 作并列证据；报告必须同时给两个口径的 `delta + CI + p + method` 与 `power_note`。③ **诚实边界**：`C1` 在 hard×6 上 `win_rate=10%`（旧四位 n=10）⇒ 判据 1 由 1–2 局抖动主导，**通过只等于「无显著退步」**，提升结论必须由判据 2 给；`fixed_prompt` 基线 0% ⇒ 判据 2 的基线对照以 `mean_stages_cleared` 为主；`truncation=0.30` 压低有效样本；**hard×6 三基线在新六位 H1 上尚无实测锚点**，起跑前必须补（付费需用户点头） | §8.6（v2.7 条目）、MOD-08 §8.2/§12、排期 §4 |
| B-64 | M3 检查单 §2 D-M3-2（用户裁决 E） | **C5 三轴同构的「相对门 + 绝对 backstop」**（§8.6 裁定 C 的落地）。判定式 `candidate ≤ min(parent + tol, backstop)`：新增 `evolve.illegal_tol=0.05` / `evolve.fallback_tol=0.05` / `evolve.truncation_backstop=0.60`；`evolve.max_illegal_rate`(0.35) / `evolve.max_fallback_rate`(0.25) **保留但语义降为 backstop**（键名不变 ⇒ 老配置仍可加载）。**为什么**：`C1` 实测 illegal 0.0318 / fallback 0.0017 vs 绝对门 0.35 / 0.25 ⇒ 松 **11× / 147×**，等于非约束门 ⇒ **判据 2 的归因结论不成立**。**`tol` 按轴分列、不可互换**：illegal 是步级（分母 = 成功步+非法尝试 ≈12k ⇒ se≈0.0017）、fallback 是步级（分母 = 决策点 ≈3.7k ⇒ se≈0.0030）、truncation 是局级（粒度 1/40 = 0.025）⇒ 0.05 对前两轴远大于批内噪声（防真退化，不承担检微小漂移），对 truncation 恰为 2 局噪声。**fail-closed 扩展**：三轴都是相对门 ⇒ parent 的 `illegal_rate`/`fallback_rate` 与 `truncation_rate` 一样进 **C0 前置完备性检查**（缺则 `C0:incomplete_report`，不静默跳门）。**可审计**：C5 的 `condition_trace` 带 `value`（候选值 + 3 个 parent 值）与 `threshold`（3 个生效门限 + 3 个 `tol` + 3 个 backstop），随 `CandidateReport.as_dict()` 落 `candidates/{id}.json.verdict`。**诚实边界**：`tol=0.05` 是起点值，**不是 §3.2 实测标定值**（标定未做）；标定后只改数值不改门结构 | MOD-07 §8.2/§8.2.1/§9、`configs/default.yaml`、`infra/config.py`、`evolve/judge.py`、`tests/unit/evolve/test_evolve_judge.py` |
| B-65 | M3 检查单 §2 D-M3-3（用户裁决 A） | **G3（同参数重跑 `report.sha256` 一致）按臂分层执行，并接上生产调用点**。**现场（两个独立事实）**：① `check_report_determinism` 从 M1 起**没有任何生产调用点**（`grep -rn check_report_determinism dice_ratchet/` 只命中它自己的定义与 `__all__`）⇒ 这条承诺在真实 run 里**从未被执行过**；② 主力臂 `C1` 是 API 臂（`api_supports_seed=false`，实测字节相同的请求 9 次返回 9 个不同响应）⇒ **端到端逐字节可复现在物理上不成立**。**裁决**：可复现性承诺**精确化到能兑现的层级**——**可复现层**（代码/配置/seed/聚合口径/并发归一化）本地臂**硬执行**（仍抛 `E_EVAL_NONDETERMINISTIC`）；**LLM 层**不承诺逐字节（采样照记 + 响应按 request hash 归档 + 变化标 `llm.nondeterministic`）；**臂内含任何 `provider=api` 的角色** ⇒ 跳过硬门 + 发新增事件 `eval.nondeterministic{generation,determinism,reason,repro_scope}`，**不抛异常**（否则第一个 API run 必然假报，一代 ≈4.8 h/≈¥100 白跑）。**执行点**：`evals/harness.py::enforce_report_determinism`，生产调用点 = `evolve/generation.py` **写报告之前**；判定函数 `reproducibility_of(app_cfg)` **fail-closed 方向**为「不可复现」（误判为可复现 ⇒ 假报 `E_EVAL_NONDETERMINISTIC`；误判为不可复现 ⇒ 只少执行一次检查，代价不对称）。**诚实边界**：这**降低**了「可复现」的强度，M3 报告必须显式声明「本轮无逐字节复现证据」；**不得**宣称「API 臂结果可逐字节复现」；`n_rep` 仍为 1（`n_rep=3` 在 heldout 臂上 +52 h/代，D-3 已否决）。**未做**：MOD-08 §3.1 的「H1 前 5 seed 重跑对 sha256」一次性验证**不再执行**（口径已改写；本地臂上的等价验证见门禁 G3 的跨 `PYTHONHASHSEED` 复跑） | MOD-08 §5（NFR-08-01）/§12（G3）/§13（事件）/§17、Spec §8.1、`evals/harness.py`、`evals/__init__.py`、`evolve/generation.py`、`infra/events.py`、`tests/unit/evals/test_evals_determinism_api.py` |
| B-76 | 2026-10-12 M3 彩排实测（**F56**） | **台账 + 绘图必须由同一个助手写全**。现场：`loop run` 的 run `plots/` 全空、台账缺 gen-000 基线行 ⇒ 彩排 2 项判据必然不合格。根因：绘图只挂 `eval report`、台账只由 MOD-07/`eval report` 写 ⇒ 「谁记得写哪一半」决定产物完整度。修法：`harness.ledger_and_plots` 一次写全，MOD-06 基线 / MOD-07 代末 / `eval report` 三方共用；`plot_generations(close_after=)` 防 figure 泄漏。**诚实边界**：本次 run 的产物是事后补跑得到，补跑后 6/6 合格但不得读成「一次跑完就合格」 | `evals/harness.py`、`evals/cli.py`、`loop/generation.py`、`evolve/generation.py`、`infra/plotting.py`、`tests/unit/evals/test_evals_cli.py`、`tests/integration/test_loop_closure.py` |
| B-77 | 2026-10-12 M3 彩排实测（**F57，已修**） | **台账缺 `arm` 列 + 重算按 `(generation, split)` 双键 upsert ⇒ 判据 1 结论可被一次 `eval report` 翻转**。① **准确表述**：`generational report.json` **内部自洽**（`evaluate_generation` 在 `winner=None` 时回落 `parent.metrics`，而 MOD-07 只在 `decision=="accept"` 时设 `result.winner`）—— F57 **不是**「文件里的数字自相矛盾」，而是「**台账没有臂维度**」加上「重算会覆盖同一行」。② **现场**：`metrics/generations.csv` 的 `generation=1, split=heldout` 行，先由 `loop run` 写 parent 的 `0.10`（`evolve.ledger` 13:34:26），后被 `eval report`（默认臂解析到候选）覆盖成 `0.35`（14:05:00）；`GENERATIONS_CSV_COLUMNS` 27 列无 `arm` ⇒ 覆盖无声。③ **后果**：同一批产物，判据 1 由「单调性不成立」（`ci_lo=0.0279 < 0.15−0.02`）翻成 `monotonic_ok=true`（`ci_lo=0.1812`、`p_gap=0.9615`）。④ **待裁决**：建议口径 A = 代际台账/判据 1 用 **parent**、候选测量归 `candidates/*.json` 与 `metrics/arms/`；口径 B = 保持现状但给台账补 `arm` 列并让判据 1 显式声明读候选臂。**两条都建议**：upsert 时臂不同即**拒绝覆盖**（fail-closed）。⑤ **用户裁决（口径 A + 两条加固）**：(a) 代际台账/判据 1 用 **parent**、候选测量归 `candidates/*.json` 与 `metrics/arms/`；(b) 台账补 `arm` 列且 upsert 升为 **`(generation, split, arm)` 三键**（臂不同即**不覆盖**，fail-closed）；(c) **豁免**——相邻两代 `harness_version` **相同** ⇒ 记 `not_applicable`（不判失败、也不计入可比对数），因为「不降」在两个**逐字节相同**的起点之间是恒真命题；(d) **补丁被拒 ⇒ 不重跑同一 `harness_version`**。⑥ **修法**：`evaluate_generation` 的 `heldout` **恒取 `parent.metrics`**（不再 `winner.metrics` 回落）、diversity/gain 同步走同一臂；`eval report` 默认臂改 **parent** 且未知臂名**报错**（`E_EVAL_ARM_NOT_ALLOWED`，原为静默回落）、`--arm all` 不写台账；台账列清单加 `arm`（`infra/plotting.py::GENERATIONS_CSV_COLUMNS`，28 列）、绘图主曲线按 `arm ∈ {parent,parent-smoke}` 分层；报告新增 `heldout.source`（`measured` / `inherited:gen-00X`，schema 加可选字段）。⑦ **零成本实证（真实彩排产物上重放）**：把 `runs/_sweep/m3-rehearsal` 的 gen-001 heldout 行按修复后口径写回（parent `0.10` / `v0`）⇒ 判定器给 `not_applicable=True`、`comparable=0`、`monotonic_ok=False`，**唯一原因是 `accepts=0`**；而「修复前」那份（候选 `0.35` / `v1`）给出 `comparable=1, monotonic_ok=true` —— 同一批局两个相反结论，正是 F57 本身。⑧ **诚实边界**：判据 1 在「全代都被拒绝」的 run 上仍然 `ok=false`（退化通过闸）；`not_applicable` **不是通过**，只是「不判」。 | `evals/harness.py`、`evals/cli.py`、`evals/acceptance.py`、`evolve/generation.py`、`evolve/judge.py`、`infra/plotting.py`（列清单）、`schemas/generation_report.schema.json`、`docs/reviews/2026-10-12-M3彩排执行结果.md` §6.2 |
| B-78 | 2026-10-12 M3 彩排实测（**F57 第二步：parent 测量跨代继承**） | **上一代被接受的候选就是本代的 parent，而它已经被完整测过 ⇒ 不得重测**。① **现场成本**：`evolve/judge.py::CandidateEvaluator.parent_arms` 原先每代都重跑 parent 的 heldout(20 局)+冒烟(2 局) ≈¥18.7/代；而 accept 之后 `version.json` 指针已指向新版本，本模块第 0 步的 `load_version(current_version())` 自动取到它 —— 再测一遍是**为同一份 harness 再买一次同样的局**。② **更坏的是数字**：`C1` 是 API 臂（`api_supports_seed=false`）⇒ 同一 harness 同一批 seed 两次测量实测 `0.15` vs `0.10`，**5pp 就是本 run 的噪声底**（赢的 seed 集合都不同：731080/731081/731092 vs 731083/731094）—— 重测不是「更准」，是**引入不可复现的抖动**。③ **修法**：新增 `evolve/generation.py::_inherited_parent_arms` —— 资格四条全满足才继承（上一代 `decision="accept"` / 该报告 `harness_version == parent.version` / 能由 `candidates[]` 推出 `cand-<id>` / `metrics/arms/gen{prev:03d}-heldout-*-cand-*.json` 能被 `harness.restore_arm_from_archive` 还原且 seed 覆盖本代篮子）；`CandidateEvaluator.parent_arms(inherited=)` 命中时 heldout **一次 `evaluate_arm` 都不调**；**冒烟仍按本代 seed 重测**（冒烟 seed 取本代训练块末尾，逐代不同；候选冒烟跑在本代 seed 上 ⇒ 「同预算」要求 parent 冒烟同批 seed；2 局 ≈¥1.7 不值得为它破坏配对前提）。④ **落盘纪律**：继承的代**不新增 heldout 台账行**（沿用 F48「没有测量就不写行」的先例），只在 `report.json` 记 `heldout.source="inherited:gen-00X"`；train 行照写（本代的训练局确实跑了）。⑤ **一个隐蔽的 0 陷阱（实测踩到并修）**：资格判断里**不能**写 `int(report.get("harness_version") or -1)` —— `harness_version == 0`（v0，最初始的 harness）是**合法值且 falsy**，那样写会把它读成 `-1` 而静默跳过一个真正可继承的来源。改用显式 `None` 判定（`_int_or_none`）。⑥ **护栏**：单测 `_inherited_parent_arms` 的四种资格（reject / 版本不匹配 / 归档缺失 / 正例）+ 真实 `CandidateEvaluator` 上的集成用例（把 `evals.harness.evaluate_arm` 换成「一调就失败」，因此实现只要偷偷重测就红）；另加 `restore_arm_from_archive` 的 fail-closed 三反例。⑦ **诚实边界**：本修复**不改变**任何已花成本；「继承」的收益要在 M3 正式跑里按 `evolve.parent_inherit` 事件核对。 | `evolve/generation.py`、`evolve/judge.py`、`evals/harness.py`、`tests/unit/evolve/test_evolve_generation.py`、`tests/unit/evals/test_evals_harness.py`、`tests/integration/test_loop_closure.py` |
| B-74 | 2026-10-11 彩排事故（**F54**） | **`evolve.heldout_basket`：held-out 篮子可配置**。现场：`scripts/run_m3_rehearsal.sh` 声明只用 H3，实测跑在 **H1**（一次性资源）上 —— `e-731000…731004-g000-parent.jsonl`、792 次 API、≈¥5.1、H1 的 5 个 seed 被写入 run 工件。根因：篮子**两处写死且无配置项**（`write_gen000_baseline(basket="H1")` + `CandidateEvaluator.heldout_basket=HOLDOUT_BASKET="H1"`）⇒ 彩排的「不碰 H1/H2/test」在代码层不可达。修法：新增配置键（默认 `H1`）、**基线与候选评估同源**、`validate()` 前移校验（`H1|H2|H3`）、彩排脚本显式指 H3 + 起跑前 fail-closed 断言。护栏 7 条（含从**真实落盘文件名**断言的集成用例 + 「只加键不改接线」的半成品反例）。诚实边界：不追回已花的≈¥5.1；默认行为不变；彩排是否合格另见报告 | `evolve/judge.py`、`evolve/generation.py`、`loop/generation.py`、`infra/config.py`、`configs/default.yaml`、`scripts/run_m3_rehearsal.sh`、`tests/{unit/evolve,unit/infra,integration}/` |
| B-75 | 2026-10-12 彩排实测（**F55**） | **只读命令与 `config_hash` 硬门解耦**。现场：`rehearsal check` 读不了 `loop run --generations 2` 建的 run（`loop.max_generations` 进 `config_hash`，而只读命令的 CLI 面里没有它）⇒ `E_IO_RUN_CONFLICT`；后果是彩排脚本的**结尾判据必然失败**（彩排永远不可能合格）。修法：`resolve_run_dir(strict_hash=False)` + `cli_init_run(read_existing=True)`（不要求 `state.json`）+ 6 个只读 handler 走该路径；**写侧仍硬校验**。护栏 1 条（含 `--resume` 仍拒的反向断言）。防御对象：「只读」与「写」共用同一条启动序列时，**写侧的不变量被误用到只读侧**，把「合法的只读」判成冲突 | `infra/run.py`、`infra/cli.py`、`evals/cli.py`、`tests/unit/loop/test_loop_cli.py` |
| B-73 | M3 检查单 §5 **F50**（2026-10-11 新增，修 F49 时被 `--with-slow` 暴露） | **`write_gen000_baseline` 漏传 `run_dir` ⇒ 按代产物落到真实 `runs/`，并连带让 F42-A 的复用提前生效**。现场：`loop/generation.py` 调 `evaluate_generation(0, parent, None, run_id=...)` **不给 `run_dir`**，而 MOD-08 从 F48/F49 起会在该路径落 `metrics/kind_distribution/gen-XXX.json`；`evaluate_generation` 的 `run_dir=None` 回落到**硬编码 `Path("runs")`**。**为什么后果比「位置错」严重**：真实 `runs/<run_id>/` 一旦被建出来，F42-A 的臂归档（`_arm_archive_dir` 按 `runs_root/run_id` 推导**且要求目录已存在**）就生效 ⇒ **第二次评估复用归档、不执行任何局**（`heldout.n` 仍 > 0，因为结果来自归档）⇒ `episodes/heldout/` 为空。实测：`test_gen000_baseline_report_is_written` **首次通过、复跑必失败**，并留下 `runs/r-loop-test/`。**为什么门禁没早抓到**：CI 每次都是干净签出 ⇒ **首次运行**通过；本地复跑才会暴露（且会被误读成 flaky）。**处置**：传 `run_dir=paths.root`；并把该用例的失败改成**确定性**的——新增反向护墙，断言真实 `runs/r-loop-test/metrics/kind_distribution` 不存在（不再依赖运行次序，也不需要给测试加环境变量绕过）。**核对**：MOD-07 的两处 `evaluate_generation` 调用点**都已传** `run_dir`，只有 MOD-06 这处漏了。 | `loop/generation.py`、`evals/harness.py`、`tests/integration/test_loop_closure.py` |
| B-71 | M3 检查单 §5 **F49**（2026-10-11 新增，§3.3/§3.4 标定时发现） | **`c3`（坍缩判据「分布冻结 + 胜率跳变」）有三处独立缺陷，从未在生产里触发过**。实测：全部 17 个归档代的 `jsd_kind` **全是空**。三个原因：① **生成侧从不落 kind 分布** ⇒ `eval report` 的重算路径**手上没有 g−1 的分布**，只能写死 `jsds = [None] * (n_gen - 1)`（`evals/cli.py:401`）；② **`evaluate_generation` 的 pair 错位**：`jsds = [None] + [jsd(parent, winner)]` 配 `win_rates = [parent.win_rate, winner.win_rate]` ⇒ `c3` 在 `i=1` 上拿「父→子的分布漂移」配「子相对父的胜率变化」，**不是同一对**；③ **`confirm_generations=2` 对 `c3` 结构上不可达**：`c3_flags` 只在 `i ≥ 1` 有定义，要「末尾连续 2 代为 True」需要 **≥3 代里连续两次**「冻结+跳变」，而 M3 只有 3 代 ⇒ 实测两代数据下 `c3` 恒不触发（旧实现要三代连续才确认）。**处置（D-M3-5 裁决 A）**：① 新增 `metrics/kind_distribution/gen-XXX.json` + `write/read_kind_distribution`，生成侧（`evaluate_generation`）与重算侧（`eval report`）都落盘；② 新增 `adjacent_jsd_kind` / `aligned_jsd_kinds`，两条路径统一用**等长对齐**的 `jsd_kind`（`jsds[i]` 恒为 `jsd(g_{i-1}, g_i)`），并修 `win_rates` 到同一 pair；③ **`c3` 改为单代即触发**（与 C4 同类：分布冻结 + 胜率跳变**本身即直接证据**，不是趋势推断 ⇒ 不适用「两点确认」）。**缺失的 jsd 记 `None` 而**不是** `0.0`**（0.0 会被读成「分布完全没变」⇒ 误报冻结）。**实测**：修复后两代数据下 `c3` 正确触发；三种反例（跳变不足 / 分布真变了 / jsd 缺测）均不误报。**`c1`/`c2`/`c4` 的 `confirm_generations` 不变**（它们能累积 flag）。**诚实边界**：阈值本身（`0.010`/`0.05`）仍**未经基线标定**（§3.4 的数据不足以标定，见 B-72）——本行修的是「判据能生效」，不是「阈值已标定」。 | MOD-08 §13、`evals/diversity.py`、`evals/harness.py`、`evals/cli.py`、`evolve/generation.py` |
| B-72 | M3 检查单 §3.3/§3.4（用户裁决 A） | **C5 的 `tol` 与坍缩阈值的标定现状：可算 MDE，但标不出标定值**。① **零成本可算**：新增 `evals/tol_sensitivity.py` 把 `0.05` 换算成**按轴的原始单位**——`illegal`（步级，分母 27,836 尝试/代）≈ **1,392 个步级事件**；`fallback`（步级，9,021 决策）≈ **451 个**；`truncation`（局级，单臂 40 局）≈ **2 局**。⇒ 结论：**truncation 是约束轴**（tol 只相当于 2 局）；`illegal`/`fallback` 是**防真·退化**（tol 相当于上千个步级事件，≈48/115 个 se）。**三轴不可互换**。② **现有数据不足以标定**：`c2` 的定义是「相对**第 0 代**的比例」⇒ 需要 ≥2 代的实测，而归档里**只有 1 个 run 有 2 代**；`c1` 是**局内**（决策点序）斜率，不是代际斜率，实测 -0.0026…+0.0005 与阈值 -0.010 差一个量级。③ **诚实边界（不得省略）**：C5 的 `tol=0.05` 与 `c1/c2/c3/c4` 的阈值**仍是「起点值 + 待回填」**，**不是**实测标定值。M3 报告必须写明这一点；彩排/正式实验跑完后按轴回填。④ **工具**：`scripts/run_m3_calibration.sh --free`（① + `heuristic` 基线的代际抖动参照，¥0）；去掉 `--free` 且设 `CALIB_CONFIRM_PAID=yes` 才跑 `fixed_prompt` 基线（≈¥35）。`eval acceptance --sensitivity` 可随时重算 MDE（分母优先用本 run 实测）。 | MOD-07 §8.2.1、MOD-08 §13、`evals/tol_sensitivity.py`、`scripts/run_m3_calibration.sh` |
| B-70 | M3 检查单 §2 D-M3-5（用户裁决 A） | **M3 彩排的 seed 与「一次性资源隔离」**。① **test 的不可逆风险（实测的顺序问题）**：`cmd_eval_test` 先跑 100 局 test（≈4.2 h / ≈¥88，指标要先算出来才能写进取证）、**之后**才调 `test_unlock`；而 `test_unlock` 的唯一「单次」判据是 **attestation 文件是否已存在** ⇒ 「路径不可写 / 父目录不存在 / 该代已解锁过」一旦发生，**那 100 局已花掉却拿不到有效存证**。这是 test 池上唯一真正不可逆的失败模式，且可零成本预检 ⇒ 新增 **`eval test --check`**（只读预检：协议可解锁/`test_full` seed 数/`validate_protocol`/`protocol_hash`/存证路径不存在/父目录可写；**不调用 `evaluate_arm`、不写存证、不消耗解锁**；不通过 ⇒ 退出码 1）。② **彩排范围**：`train` 窗口 + **H3 731080–731099**（`role: reserve`，协议未 locked ⇒ 可重复用），**2 代**（1 代时判据 1 判定器的 `comparable=0`，验不到「判定器能给出结论」这件事）；**H1/H2/test 一律不出现**。成本 ≈2 h / ≈¥40（付费需用户点头）。③ **合格判据写成代码**（`dice_ratchet/evals/rehearsal.py` + `rehearsal check` 子命令）：机制性失败为零（逐字复刻 M2 §3.2 的 5 条 + 裁判拒绝正向白名单）/ **5 张 PNG 有数据点**/台账含 heldout **与** train 行 / 判据 1 判定器能产出结论 / 每代臂归档存在 / **一次性资源未被消耗（扫 `episodes/heldout/` 的实际 seed 与 `test_attestation.json`）**。④ **为什么「有数据点」不能查文件**：实测空白图 20–27 KB、非白像素比例 **3.43%**，有数据图 **3.60%** —— 「文件存在」「文件大小」「像素比例」三个判据**都分不开**空白图（坐标轴与图例本身贡献 3.4% 墨迹）。唯一可靠的是读**画进 figure 的对象**（`plot_generations(figures=[...])` → `Line2D.get_xydata()` 数点数）。⑤ **脚本**：`scripts/run_m3_rehearsal.sh`（`--plan` 免费打印计划；真跑前断言工作区干净、harness 复制到 `runs/_sweep/<run>-harness` ⇒ 真实 `harness/` 零风险；结尾断言无 `test_attestation.json`）。**`--plan` 实测通过**。⑥ **未执行**：**彩排本身没有跑**（≈¥40 需用户点头）⇒ 本行只登记「彩排已可执行且判据已代码化」，**不得**把它读成「彩排已合格」。 | Spec §4.6、`scripts/run_m3_rehearsal.sh`、`evals/rehearsal.py`、`evals/cli.py`、`infra/cli.py`、`tests/unit/evals/test_evals_rehearsal.py` |
| B-69 | M3 检查单 §5 **F-M3-9**（2026-10-11 新增，第 1 步落地） | **判据 1 的判定器**（M3 验收的机读结论）。**现场**：MOD-08 §12 判定表第 1 行要求 `monotonic_ok=true`，而 `grep -rn monotonic_ok dice_ratchet/ schemas/ scripts/` **0 命中**（该标识符只在文档里出现 3 次）⇒ 口径定了、**判定器没写**；M3 跑完 3 代后「判据 1 通过/不通过」无法由代码产出，只能人肉读 csv（正是 F46-A 刚修掉的病）。**裁决 A（含三处必须写清的口径）**：① **`g−1` = 上一个已存在且 `split="heldout"` 的代**，不是字面 `g−1`——`gen-000` 是基线（`decision="baseline"`），拿它当「上一代」会把「基线 vs 第一代」混进单调性判定（那是判据 2 的活）；**第一代不参与判定**（无比较对象），但**至少要有一对可比**，否则只有基线的 run 不能算通过。② **`P(gap>0)` 的定义（本次补的，写回 MOD-08 §12）**：**相邻两代 heldout 的 `mean_stages_cleared` 的配对 bootstrap** 中 `Δ>0` 的比例，`B` 取 `eval.bootstrap_B`、RNG 用 `bootstrap_seed(run_id, generation, "acceptance")`；**只在两代共有的 seed 上配对**，无共有 seed ⇒ 记 `null`（**不得记 0.0**——0.0 会被读成「显著退步」）。**关键校正**：原文写的 `P(gap>0)` 与 `GainVerdict.win_rate_gap` 同名但语义不同——后者是 `train − heldout`，`P(train > heldout)` 恰恰是判据 1 **不**想要的。选连续指标而非 `win_rate`：相隔一代后的赢/输不是配对样本，且 n=40 的 `win_rate` 只能判 ≥15pp（D-3）。③ **接受记录以各代 `report.json` 的 `decision` 为准**（`VERDICT_DECISIONS` 同口径，F43 的权威），**指标值以 `metrics/generations.csv` 为准**（判据的机读来源，F46-A），且**必须按 `split="heldout"` 过滤 + 按代去重取末条**（F48 之后台账每代每 split 一行）。**产物**：`AcceptanceReport`（`metrics/acceptance.json` + `eval report` 的 ext 段 `acceptance`），逐代给 `{win_rate, ci_lo, prev_generation, prev_win_rate, threshold, pass_ci, p_gap_gt_0, p_gap_n_pairs, pass_p_gap, decision, passed}` ——只给一个布尔无法复核「第 2 代为什么算过」。**退出码**：`eval acceptance` **总是返回 0**（判定器不是守卫；把「验收不合格」编码成退出码会让「命令失败」与「实验结论是否定」混淆，F47-D 刚把这两件事分开）；不通过时原因写 stderr。**fail-closed**：`n < evolve.min_heldout_n` ⇒ `ok=false` + `reasons`，**不静默跳过**。**实测**：归档 run `m2-s32b-smoke-c1-medium6` 上判 `monotonic_ok=false`（`comparable=0`，只有 gen-1 一代）+ `accepts=0`（其台账 `n=117` 是 F46-A 修掉的混代聚合）⇒ `ok=false`；12 条新用例覆盖两条通过路径、退化通过、fail-closed、`null` vs `0.0`、train 行忽略、重复代取末条。**修复前**：`ModuleNotFoundError`（模块不存在）+ `monotonic_ok` 生产代码 0 命中。 | MOD-08 §12/§13/§17、Spec §8.6、`evals/acceptance.py`、`evals/cli.py`、`infra/cli.py` |
| B-68 | M3 检查单 §5 **F48**（2026-10-11 新增，第 1 步落地 F-M3-3 时发现） | **绘图链路三重缺陷 + 台账零 train 行**。① `plot_generations` **无任何生产调用点**（只被它自己的单测调用）⇒ `runs/*/plots/` 全空。② `infra/plotting.py` **自维护一份列清单**，其中 `heldout_win_rate` / `heldout_ci_lo` / `heldout_ci_hi` / `train_win_rate` **四个列名在生产台账里不存在**（MOD-08 写的是 `win_rate`/`ci_lo`/`ci_hi`，且 train 是**另一行**而不是另一列），而 `_g()` 用 `r.get(key)` ⇒ 缺列时**静默返回空列表**、matplotlib 对空序列不报错 ⇒ 5 张 PNG「生成成功」、各 20–27 KB、**打开是空白图**。这直接违反 B-06（列定义归 MOD-08）。③ `append_generations_csv` 按 `generation` **单键** upsert，且历史行 `split` 为空时比较恒不等 ⇒ **每次追加**（实测 `runs/g6-eval-smoke` 同代同值 **5 行**）；`generations_row_from_report` 只产出 **heldout** 行 ⇒ 8 个归档 run 的台账 **train 行数全为 0**，`train_heldout_gap.png` 与 `win_rate.png` 的 train 对照曲线**无源**，`win_rate_gap`（判据 3 的量）在台账里无可核验来源。**处置（裁决 A 七条）**：(a) **列清单唯一化**——权威定义落 `infra.plotting.GENERATIONS_CSV_COLUMNS`，`evals.harness.GENERATIONS_COLUMNS` 直接引用它（**方向**：`infra → evals` 被分层门禁 `E_IO_GUARD_LAYER` 禁止（NFR-09-5），`evals → infra` 合法；台账是 IO 工件，schema 落 IO 层）；(b) 台账增发 `split="train"` 行（`n > 0` 才写——引擎无训练局时写的是 `n=0` 的**非空** dict，用 `if train:` 会写出假的「win_rate=0」零点）；(c) 按 `split` 取行 + 按代**去重取末条**；(d) `_require_points`：**任一图无数据点即抛 `E_IO_PLOT_NO_DATA`**（「文件存在」不等于「有图」）；(e) 生产调用点 = `eval report` 末尾（`_plot_or_warn`：绘图失败**不阻断**报告/台账落盘，但必须**非静默**——发 `infra.plot_done`/`infra.plot_skipped` 事件 + stderr）；(f) 台账 upsert 改 **`(generation, split)` 双键** + **读入时折叠历史重复行**；(g) 测试夹具改用**权威列名**建行（修复前夹具用的是那 4 个不存在的列名——**这正是缺陷能潜伏的原因**）。**实测**：真实台账 `runs/m2-s32b-smoke-c1-medium6` 上修复前 5 张空白 PNG、修复后 `win_rate.png` 带数据点（`0.650 / n=117`）；空指标列 ⇒ `E_IO_PLOT_NO_DATA`。**新增/改 15 条用例**（列清单同源、无数据点拒绝、split 是行维度、按代去重、空 split 旧行被替换、双键不互相覆盖、`n=0` 不写行、端到端 `eval report` 出 5 图）。 | Spec §4.7/B-06、MOD-09 FR-09-13/§17、MOD-08 §13/§17、`infra/plotting.py`、`infra/events.py`、`infra/errors.py`、`evals/harness.py`、`evals/cli.py`、`evolve/generation.py` |
| B-67 | M3 检查单 §5 F-M3-9（2026-10-11 新增，D-M3-1 写回时发现） | **判据 1 目前没有判定器**：MOD-08 §12 判定表第 1 行与本检查单 §1 的通过条件是「`ci_lo(g) ≥ win_rate(g−1) − 0.02` 或 `P(gap>0) ≥ 0.90`，`monotonic_ok=true`，且 ≥1 次 accept」，但 `grep -rn monotonic_ok` 在 `dice_ratchet/`/`schemas/`/`scripts/` 上 **0 命中**，仅在文档里出现 3 次；`eval report` 只写逐代指标行，**不计算逐代单调性**。即：口径（D-M3-1 裁决 A）已定，**但「M3 判据 1 通过/不通过」这句话在 M3 起跑时无法由代码产出**。**必须补**：逐代 `ci_lo`/`P(gap>0)` 比较 + `monotonic_ok` + 与「≥1 次 accept」的联合判定，落 `report.eval.json` 的 ext 段并加回归用例（先证明修复前失败）。**归属**：M3 的 MOD-08 编码（排期 §4 的 MOD-08 行），不阻塞第 0 步 | MOD-08 §12/§13、`evals/harness.py`、`evals/cli.py` |
| B-66 | M3 检查单 §2 D-M3-4（用户裁决 A） | **M3 的 HITL 模式与「样本资格」口径澄清**。**矛盾现场**：MOD-07 §14 风险④ 写「`--hitl=false` 跑批量实验（标 `decided_by="auto"`，**不计入 M3 验收样本**）」，而 MOD-07 §12 / MOD-08 §12 判定表第 1 行又要求「**≥1 次 accept**」⇒ 全自动跑时 accept 出自 auto、按原文不计入样本；改用 `--hitl` 则每代有人工延迟，且 24 h 无响应会 `hitl_timeout` **保守回滚**（一代白跑 ≈4.8 h / ≈¥100）。**裁决**：① M3 跑 `--hitl=false` 全程自动（可复现、无人在环干预）；② **样本资格 = `decided_by="auto"` + `condition_trace` 完整**（C0–C5 六项齐全，每项带 `passed`/`value`/`threshold`）⇒ **计入**验收样本；缺 trace 的 auto 决策不计入；③ 报告必须**显式记录「本轮无人工确认」**。**依据**：M2 准出 run（`runs/m2-s32b-smoke-c1-medium6`）**事实就是走 auto 路径**（归档 `decided_by="auto"`、`decision="reject"`、`reason="C2:no_significant_gain"`，`candidates/{id}.json.verdict.condition_trace` 完整）⇒ 本裁决是**追认既有做法**，不是引入新风险；选 `--hitl` 的代价不对称。**⚠️ 诚实边界**：auto accept 的说服力弱于人工确认 ⇒ 报告**不得**把 auto 决策描述为「经人工复核」 | MOD-07 §12/§14、MOD-08 §12 |

| B-79 | M3 检查单 §2 **D-M3-6 / F-M3-8**（2026-10-13 起跑；用户指令「现在开始正式实验」） | **M3 正式实验的起跑口径、预算、限流与墙钟上限落定，并固化成脚本**（原 D-M3-6 的三项「待定」全关）。① **run-id / 环境**：`m3-formal`；**只有 4 个 run 级 override** —— `experiment.arm=C1`、`inference.mode=eval`、`inference.seed=0`、`evolve.hitl=false`（与 M2 准出 run `m2-s32b-smoke-c1-medium6` 同源，除难度/篮子按 M3 口径改回）。**故意不设**：`env.episode.difficulty`（默认 `hard` = D-M3-1 主口径）、`evolve.heldout_basket`（默认 **H1** 40 局 = 代际裁判篮子，**不是**彩排的 H3）、`eval.n_heldout`/`loop.heldout_n`（默认 40/40）、`infra.runs_root`（默认 `runs/`）。② **代数 / seed**：`--generations 3 --k 30 --seeds train:1000-1089`（三代各 30 局：1000-1029 / 1030-1059 / 1060-1089）。③ **harness 目录 = 真实 `harness/`**（生产生效目录）：`--hitl=false` 下的 accept 会经 MOD-09 `git_ops` **commit + tag `harness-v{N}`**（G6 证据链），因此起跑处工作区必须干净（与 F47-A 门禁同源）；**反思开启**（彩排用的 `--no-reflect` 是消融臂，正式实验不得用）。④ **一次性资源边界**：主体**不碰 H2（731040–731079）与 test（947000–947099）**；二者是独立步骤、各自还要显式确认开关（`M3_CONFIRM_H2=yes` / `M3_CONFIRM_TEST=yes`），脚本结尾按 `events.jsonl` 的 `loop.episode_start` seed 扫描做 fail-closed 断言。⑤ **预算**（`C1`·hard×6，¥0.833/局 = 彩排实测、¥0.879/局 = 登记上界；单价取**高峰档**，空闲档减半）：主体 = gen-000 基线 40 局 + 3 代 × `E_gen` 114 局 = 382 局 ⇒ **≈¥318–336 / ≈16 h**；消融 H2（`full`+`no_reflection`+`no_self_edit`，3×40 局）≈¥100 / ≈5 h；test 终检（100 局）≈¥83–88 / ≈4 h；三基线（`random`/`heuristic` 本地 ¥0；`fixed_prompt` H1 40 局）≈¥35 ⇒ **全 M3 高峰价上界 ≈¥540**。⑥ **限流**：一律用默认（`inference.max_inflight=4` / `evals.n_workers=2` / `loop.train_concurrency=1`）——彩排 12,397 次调用未触 429；**改它就是换 `config_hash` = 换 run-id 族**。⑦ **单代墙钟上限 6 h**（实测 ≈4.8 h/代）：F42-A 的按臂归档使中断最多损失「当前一臂」，`--resume` 不重付已完成局。⑧ **脚本**：`scripts/run_m3_formal.sh`（`--plan`/`--preflight`/`--status`/`--cost`/`--post` 全部免费；`--preflight` = 9 条 fail-closed 断言 + `loop run --dry-run` + `eval test --check`；**断言不过退出码 4，不花钱**）。⑨ **零成本实证**：`--preflight` 9/9 通过（篮子 H1 / heldout 40-40 / hard / 反思开 / HITL off / 工作区干净 / key 可用 / run 目录无冲突 / 无 test 存证），`loop run --dry-run` 得 `config_hash=06d684e260540893`，全量门禁 `scripts/ci_gate.sh --with-slow` EXIT=0。⑩ **顺带复核彩排报告 §6.3 的担忧**：`llm.degrade` **12,352/12,397**（99.6%，`reason=grammar_unavailable`）**不额外花钱** —— 同 run 的 `llm.api.call` 与 `llm.call` 各 **12,397** 条、一一对应（`grammar_unavailable` 是对**已成功调用**打的「未用 grammar」标签，不是重试）⇒「成本/延迟翻倍」的读法**不成立**；M3 报告须写明这条复核。⑪ **诚实边界**：账户余额与预算是两件事 —— 起跑前实测 DeepSeek 余额 **¥352.78**（`/user/balance`），只覆盖**主体**；消融与 test 需另行确认（可能需充值）。⑫ 本版**不动**排期日历日期。 | `scripts/run_m3_formal.sh`、`configs/default.yaml`、[M3 验收前置条件确认](tasks/2026-10-11-M3验收前置条件确认.md) §2 D-M3-6、排期 §4 |

| B-80 | M3 正式实验**主体**执行结果（2026-10-13 → 10-14，用户指令「现在开始正式实验」） | **run `m3-formal` 跑完 gen-000 基线 + 3 代演化（19 h 50 min / 382 局 / 52,319 次调用 / 实测 ≈¥0.52/局）；3 次提案全部在 C2 被合法拒绝 ⇒ 0 accept ⇒ M3 判据 1 `ok=false`（唯一原因是退化通过闸，不是单调性被破坏）**。① **判据 1 机读结论**（`metrics/acceptance.json`）：`accepts=0`、`comparable_pairs=0`、`not_applicable_pairs=3`（相邻两代同为 v0 ⇒ F57 同版本豁免）、`monotonic_ok=false`、`ok=false`；`reasons` 只有「接受率 = 0」。② **候选**：3 个 `prompt_patch`（5–6 行、均只改 `harness/prompts/decision.md`），`win_rate` delta **+2.5 / +7.5 / +5.0 pp**（McNemar `p=1.000 / 0.549 / 0.754`）；**零成本复算**文档指定的连续指标口径（`mean_stages_cleared` 配对 bootstrap B=10000）后 delta **+0.05 / +0.325 / +0.15 关**、**95% CI 全部含 0** ⇒ 三次拒绝与「C2 选哪个指标」无关。③ **本轮最重要的实测（判据轴的噪声底）**：**同一份 harness v0** 在同一批 40 个 H1 seed 上被测 4 次（gen-000 基线 + 三代起点）⇒ `win_rate` **0.175 / 0.100 / 0.150 / 0.100（极差 7.5 pp）**、`mean_stages_cleared` **4.425 / 4.250 / 4.150 / 4.325（极差 0.275 关）** ⇒ 判据 1 的轴在 n=40 上**噪声主导**（D-3 的「只能判 ≥15 pp」第二次被实测证实），M3 结论必须写「**判据 1 在当前 n 与当前臂下不可判**」。④ **免费基线锚点**（新六位 H1，补上 D-M3-1 缺的两格）：`random` 0.000 / 2.350 关、**`heuristic` 0.325 / 4.975 关** ⇒ **`C1`（0.100–0.175 / 4.150–4.425）在两条轴上都劣于启发式 bot**，判据 2 在当前臂上无成立迹象（`fixed_prompt` 基线仍缺，≈¥35 待裁决）。⑤ **成本**：注册表高峰价累加 ¥344.44、**账户实际扣减 ¥197.67**（352.78→155.11，≈0.57×，差额来自空闲档 + 输入缓存）；`llm.degrade` 52,135/52,319（99.6%）**不额外花钱**（`llm.api.call` 与 `llm.call` 各 52,319 条一一对应）；`llm.nondeterministic` 3,576。⑥ **抓出 5 个结构性缺陷（待裁决，逐条见 [结果报告](reviews/2026-10-14-M3正式实验主体结果.md) §6）**：**F58**（🔴 `audit leak` 的 `future_room_leak` **跨局误报**：派生集合并 + 全局扫描，节点 id 跨局共享 ⇒ 200 findings / 5,852 occurrences **全是假阳性**；逐局复核 **280 局 / 96,583 行 0 命中** ⇒ 「audit clean」在多局 run 上结构性不可满足，与 F55 同族）；**F59**（🟠 `seed_leak` 数字扫描把**浮点小数部分**当 seed：`elapsed_ms: 0.731042` 命中 H2 731040–731079、`0.947084/0.947042/1.947041` 命中 test 947000–947099）；**F60**（🔴 `eval report` 重算路径**伪造** `harness_version=generation / parent_version=generation−1` 并按 `(generation,split,arm)` upsert **静默覆盖** loop 写入的正确行（0/0 → 3/2）⇒ **一条只读命令把 gen-3 从 `not_applicable` 翻成假结论「单调性不成立」**，有 before/after 实证）；**F61**（🟡 `eval report --split train` 无可解析臂 ⇒ 退出码 2）；**F63**（🟡 候选臂 provenance 不含**补丁哈希**，`harness_version` 亦为 0 ⇒ 打过补丁的局与裸 v0 局在版本维不可区分）；**F64**（🔴 训练局 `generation` 记的是 **cycle 序号**（差 1）⇒ `evaluate_generation` 的 `train` 段**永远筛不到那 30 局真实训练局**，只筛到本代 4 局冒烟 ⇒ 台账 train 行 / `train_heldout_gap.png` / **C3 的 `win_rate_gap`** 全建在 4 局上，而代码意图是 60 局滚动窗口，**静默降级无告警**）；**F65**（🟠 parent heldout **每代重测**（跨代继承只在上一代 accept 时生效）⇒ 同一份 v0 在同样 40 seed 上被完整测 4 次（160 局 ≈¥83）；两面性：这 4 次重复正是噪声底 7.5 pp 的唯一来源）；**F66**（🔴🔴 **候选补丁从未进入决策 prompt**：`evals/harness.py:849` 的 `executor = runner or _default_runner(harness_dir=…)` 使 `harness_dir` 在**注入 runner 时被架空**，而 MOD-07 给每次候选评估都注入 loop 的 runner（策略工厂绑在 run 级 `harness/`）⇒ 候选臂与 parent 的**首个决策点 `prompt_hash` 逐字节相同（40/40）**、整臂交集 320（模板不同必须为 0）、首个分叉步中位数 8（动作首次分歧处）；影响：**判据 2 在本 run 无数据**、「0 accept」是「补丁从未被测试」而非「补丁无效」，且 **M2 §3.2 冒烟与 M3 彩排的候选结论一并待复核**（同一代码路径）；§5 噪声底升级为**同一 v0 的 7 次测量**（`win_rate` 极差 **12.5 pp**，候选组 vs parent 组 Welch t=+1.00）；修法三选一 + **必需护栏**（臂归档记实际加载的 `template_hash`，有 `prompt_patch` 时断言 ≠ parent，fail-closed））；**F62**（🟡 C2 恒用 `win_rate`/McNemar，D-3/B-39 要求的连续指标主检验**在生产路径上从未执行**——本轮不影响结论，但下一次 Δ≈0.3 关的候选会被误杀/误放）。⑦ **未做**：消融 H2 与 test 解锁（**建议留到「修完判据 + 出现真 accept」之后**：0 accept ⇒ `no_self_edit` 与 `full` 构造上相同，且 test 每里程碑只有一次）；`fixed_prompt` 基线。⑧ **诚实边界**：判据 1 的结论是「**不可判 + 未满足**」，**不是**「模型没变强」——三次候选的观测方向全为正，只是在 n=40 的噪声与检验口径下无法与噪声区分；`audit leak` 的 `critical` **不是泄漏证据**（F58/F59 已定位为假阳性），但在修复并复跑之前只能记「未通过（假阳性，待修）」；C4/C5 阈值仍是起点值（本轮 4 代 × 2 臂 × 40 局的数据**足以做第一次按轴回填**）。 | [结果报告](reviews/2026-10-14-M3正式实验主体结果.md)、`runs/m3-formal/`、`scripts/run_m3_formal.sh`、`dice_ratchet/evals/{acceptance,harness,cli}.py`、`dice_ratchet/sandbox/audit.py` |
| B-81 | 2026-10-14，用户口述架构（**D-M3-7**，逐字见 [架构对照](reviews/2026-10-14-M3-DM37架构改造与7步对照.md)） | **「起点继承」的判据从「上一代 accept」改成「play 面指纹相同」，并把代际测量从「每代重测起点」改成「每代只测候选」**。① **用户口径（七步）**：gen-000 用 v0 跑 40 轮 heldout；cycle 0 用 v0 train 30 轮得 P1；v0+P1 跑 40 轮 heldout，门禁过则 `v1=v0+P1` 且**后续比较的 heldout 就是这份**，不过则 `v1=v0+（仅 P1 不可行的提示词）` 且**后续比较的 heldout 继承 v0 的**；cycle 1/2 同理（`v2`/`v3`）；**继承不重跑 heldout，直接复用数据**。② **play 面指纹**（`versioning.play_hash` = `prompts/decision.md` + `config.json` + `tools/*.py` 的 sha256 复合；**不含** `reflection.md` 与 `dead_ends.md`）：D2=A 让版本号在**拒绝**时也推进 ⇒ 「版本相同 ⇔ 起点相同」不再成立，继承资格、判据 1 的同起点豁免、F66 的第二半（补丁是否真的改变打法）三处全部改问它；旧 run 缺该字段时回落「版本号相同」判据（不静默放宽）。③ **每代只测候选**：`_inherited_parent_heldout` 扫更早的代找**同 play 面且真有测量**的那份（gen-000 基线臂 / 某代 accept 的候选臂），命中即 0 局复用；找不到才退回实测。每 cycle 局数 **114 → 74**（K=30 + 2+2 冒烟 + 40 候选 heldout），一代省 ≈40 局 ≈¥21 与 ≈1.7 h。④ **`heldout` 段的口径改定**（**取代 F57 的「恒取 parent」**）：= **本代结束时在役那份 harness 的测量**——被拒 ⇒ 起点那份（`source=inherited:gen-00X`，按 F57 不写 heldout 台账行）；被接受 ⇒ **那个候选自己的臂**（`source=measured`，写行）。F57 的原口径让「接受了一代」在台账里**永远不可见**，判据 1 退化成拿起点与起点比的恒真命题；F57 当初要修的**症状**（两个写入方写不同的数）已由 F60 的「报告是唯一权威」承担。⑤ **D2=A 轻量版本推进**（`versioning.record_rejection`）：拒绝时 `v_{N+1} = v_N + 死路记录`——推进 `version.json` 指针 + `git commit`，**不复制** `versions/v{N}/`、**不打** tag（play 面逐字节未变，没有可回滚的内容）；账本条目**不带** `versions` 键（否则 `accept()` 的幂等分支会 `load_version` 一个不存在的版本号）；git 失败 ⇒ 指针回滚 + `E_EVOLVE_GIT_FAILED`（不留幻影版本号）。**只在真的评估过候选且构成死路时推进**：空候选 / 提案失败 / MOD-08 基础设施故障（`E_EVAL_*`）都不推（F47-D 同一条纪律）。⑥ **D1=A 死路记录**（`versioning.write_dead_end` → `harness/dead_ends.md`）：记录类型 / 触及文件 / 落到哪个判定条件 / 指标 / 提案理由（同 `(generation, candidate_id)` 替换而非追加，`--resume` 幂等）；**只给提案方看**（`HarnessSnapshot.context`，`evolve-harness-snapshot-1.2.0`），**禁止候选改动**（`_is_forbidden_path`，否则模型能抹掉自己的拒绝记录再提同一份补丁）；渲染处**脱敏**（`_sanitize_agent_text`，落盘原文保持可审计）——自由文本里的 `heldout`/`H1` 会让整代以 `E_EVAL_INV5_LEAK` 中止；提案 system prompt 加第 11 条硬约束「不得重复 dead_ends 里的改动」。⑦ **F64**：训练局的 `generation` 标签改为**它服务的那一代**（`loop/generation.py::_run_training_episode` 传 `state.generation + 1`），`evaluate_generation` 的 train 段改取 `take("train", generation)` 那个块并**按 `arm="train"` 真过滤**（同代冒烟局用的是本代训练块末尾 seed，不过滤会混进来）；取不到足量局 ⇒ `train.source="insufficient"` + `low_confidence=true` + `evals.train_insufficient` 事件（**不许静默**）；gen-000 记 `unavailable`（`take("train", 0)` 按契约抛 `E_EVAL_SEED_EXHAUSTED`）。⑧ **F69**：`win_rate.png` 的 `yerr` 原先直接算 `ci_lo/ci_hi` 差值，CI 与点估计不自洽时 matplotlib 抛 `ValueError`（**非 `InfraError`**）⇒ 穿透 `ledger_and_plots` 与 `_finish_report`，**在报告已落盘之后炸掉整代**；改为夹到 ≥ 0。⑨ **同批修正**：`evaluate_generation` 的 `harness_version` 改由 `retained_version` 决定（F57 时代它恒取 `_arm_like(winner, harness_version=parent_version)` ⇒ **接受一代后报告仍写旧版本号**）；判据 1 的 `not_applicable` 改判 play_hash（`GenerationAcceptance` 加 `play_hash/prev_play_hash`）；`_episode_stages` 跟随 `inherited:gen-00X` 链取逐局数据（否则 `P(gap>0)` 因空集被静默废掉）；`generation_report.schema.json` 加 `harness_play_hash` 与 `train.source`。⑩ **新增/改护栏 23 条**（逐条在修复前失败：`git stash` 后 23 failed；本行末尾 ⑪ 列的语义修订项另有既有用例同步更新）。⑪ **诚实边界**：本版**不动**已花的 ¥197.67，也不改变「上一轮 `m3-formal` 的判据 1 `ok=false`」；改变的是**判据能不能被满足**——修 F66+F64+D-M3-7 之前「0 accept」既可归因于「补丁无效」也可归因于「补丁从未被测试」，之后不能。**未做**：F58/F59（审计假阳性）、F60（`eval report` 伪造版本列）、F61、F62（C2 指标口径）、F67（冒烟胜率门在 n=2 上无诊断力）、F70（**新登记**：被接受的候选其测量成为下一代基线 ⇒ 选择偏差抬高了后续比较的门槛，即 winner's curse）。**成本：¥0**（本版全部为代码 + 测试 + 文档）。 | `dice_ratchet/evolve/{versioning,generation,proposer,schemas}.py`、`dice_ratchet/evals/{harness,metrics,acceptance}.py`、`dice_ratchet/loop/generation.py`、`dice_ratchet/infra/plotting.py`、`schemas/generation_report.schema.json`、`tests/unit/{evolve,evals}/`、`tests/integration/`、`harness/`（运行时产物） |
| B-82 | 2026-10-14，用户指令「先修复 F58-60」 | **审计的两类假阳性 + 台账版本列被只读命令伪造——三条一起修掉，全部有 before/after 实测**。① **F58（🔴）`future_room_leak` 跨局误报**：根因是**作用域错配**——`derive_hidden_tokens(split="train")` 把整批 train 局的未揭示节点并成**一个集合**，而 `log` 面扫描遍历 `episodes/**`（含 heldout）；节点 id（`n-<stage>-<row>-<col>`）是**跨局复用的结构 id** ⇒「A 局未揭示」被当成「B 局出现即泄漏」，而 B 局里它是合法揭示的。修法：新增 `HiddenTokenIndex`（`by_episode: episode_key → {token: class}` + `unscoped` 兜底 + `replayed/failed/no_trajectory`），`derive_hidden_tokens(splits=)` 让**派生面 = 扫描面**（重放 `episodes/` 下全部有局文件的 split），逐局出口只用**本局**的 `hidden − shown`；`memory/` 全局库与 `events.jsonl` 用 `unscoped` =「**任何一局都从未合法展示过**」的 token（fail-closed 且无跨局假阳性）；`reflections/<episode_key>.json` 同样逐局。**实测**：`runs/m3-formal/audit/leak-leak.json` 由 `critical exit=2 / future_room_leak=5852 / 204 findings` → **`clean exit=0 / 0 findings`**（旧报告留档 `leak-leak.F58-before.json`）；顺带纠正记账——只有 `episode_end` 的基线臂摘要（本 run **80/462**）**没有可重放的步**，不算「重放降级」，否则每个含基线的 run 都会无谓地停在 `warn`。② **F59（🟠）浮点小数被当 seed**：`_NUM_RUN_RE` 前导边界 `(?<![0-9A-Za-z_])` → `(?<![0-9A-Za-z_.])`，**只放宽「紧跟在 `.` 之后」**：`0.731042` 不命中（H2 假阳性消失）、而 `"reason": 731042` / `947042.5` / `-947042` / 文本字段里的 `seed 947042` **仍然命中**（方向刻意不对称 = fail-closed）。实测 4 条 `seed_leak` → 0。③ **F60（🔴）`eval report` 伪造版本列并静默覆盖台账**：`cmd_eval_report` 原先写死 `harness_version=generation / parent_version=generation-1`，而那份**内存里的** report 随后进 `ledger_and_plots` ⇒ 按 `(generation, split, arm)` upsert 把 `loop run` 写下的 0/0 覆盖成 3/2（现场 gen-003；1.8 s 内两次写入），判据 1 因此把 `not_applicable` 翻成假结论「单调性不成立」（`comparable_pairs` 0→1）。修法两条：**(a)** 新增 `_report_versions()`——版本列 / `harness_play_hash` / `heldout.source` / `train.source` **一律以 `generations/gen-NNN/report.json` 为权威**，无报告时退回「该臂归档里的实测版本」（`EpisodeResult.harness_version`，写进 ext 的 `version_source`），**绝不从代数推**；**(b)** `append_generations_csv` 同口径：与报告不符时**以报告为准**并把分歧**非静默**记录（`evals.ledger_version_conflict` + stderr 打 before/after），报告不存在时保持调用方值。**实测**：重跑 `eval report --generation 3` 后 gen-003 的 `(hv, pv)` 由 `(3, 2)` → `(0, 0)`（heldout **与** train 两行），`eval acceptance` 由 `na=2/comparable=1/reasons=[gen-003 单调性不成立, 接受率=0]` 回到报告里写的 `na=3/comparable=0/reasons=[接受率=0]`。④ **顺带**：`cmd_eval_report` 内**自己那份 train 段口径**仍是 `train_window` + 不过滤臂（实测拿到 **4 局冒烟局**）——这是 F64 在第二条路径上的残留，已与 `evaluate_generation` 统一（`take("train", g)` + `arm="train"` + `source∈{measured,insufficient,unavailable}`）。⑤ **`RULESET_VERSION` 1.0.0 → 1.1.0**（规则语义变了：逐局作用域 + 数字边界；历史报告里的 1.0.0 因此可辨识），golden `tests/golden/sandbox/leak-report-clean.json` 同步。⑥ **护栏 9 条**（F58 4 条 / F59 2 条 / F60 3 条，含三条「不得把审计变瞎」的反例自检），`git stash push -- dice_ratchet/` 实测**修复前失败 5 条**（另外 4 条是反例自检，修复前后都该绿）；全量门禁 **2096 passed / 2 skipped / 1 failed**（唯一失败为满载下的 50 ms 性能预算计时抖动，见 F68）。**成本：¥0**（审计/报告/判定器全部本地）。 | `dice_ratchet/sandbox/audit.py`、`dice_ratchet/evals/{cli,harness}.py`、`schemas/audit_report.schema.json`（未改）、`tests/unit/{sandbox,evals}/`、`runs/m3-formal/{audit,metrics}/`（复跑产物） |

| B-83 | 2026-10-14，用户裁决「评估现有框架下这些问题都该如何修改，**F62 直接取消 C2**」+ 三选（C2 落法 B / F70 只标注 / 现在实施） | **取消 C2 的统计显著性门（改判「play 面确实变了」）+ 修掉 `--split train` 的两层问题 + 冒烟门去掉无诊断力的胜率轴 + 基线选择偏差标注**。① **F62（用户裁决 B）：C2 从「至少一指标显著提升」改判为「候选 ``play_hash`` ≠ 起点 ``play_hash``」**。为什么必须改：M3 正式实验实测三代**全部**止步 C2（`C2:no_significant_gain`、p=1.000/0.549/0.754），而 n=40 上 C2 的**等效门槛**是「净 +15 pp / 0.45 关」，实际效应只有 +2.5–7.5 pp / +0.05–0.325 关 ⇒ **是门没有功效，不是候选不够好**。为什么**不能只删**：零改动候选若被 accept，它会因为 `play_hash` 未变而在判据 1 里落回 `not_applicable` ⇒「≥1 次 accept」**第二次**变成退化通过（B-66/D-M3-4 立这条正是为防它）。判据 `c2_ok = bool(play_hash) and bool(parent_play_hash) and play_hash != parent_play_hash`，**任一为空即 fail-closed 拒绝**（`C2:play_hash_unavailable`），指纹取自**候选 staging 目录**与起点在役 harness（`judge(..., play_hash=, parent_play_hash=)` 两个关键字入参，纯函数边界不变）。**统计量降级为报告项**：`compare_all_metrics`（MOD-08 早已存在、此前**零生产调用点**）每次候选评估全跑一遍，落 `paired["by_metric"]`（`paired` 是自由 object ⇒ **免改 schema**），C2 的 `condition_trace` 同时带两个指纹与显著/方向/delta/p ⇒ 判据 2 第一次拿到连续轴证据。**排序随之改轴**：多候选由「C2 的 `win_rate` delta 最大」改为**连续轴优先**（`mean_stages_cleared` → `win_rate` → `candidate_id`）。**诚实边界**：accept 的语义从「**显著变强**」变成「**打法确实变了且没有可判定的退化**」；只改学习侧（`reflection.md`/`dead_ends.md`，不在 play_hash 里）的提案会被 C2 合法拒绝——D-M3-7 下**演化的单位就是 play 面**，要改学习侧必须连带一次 play 面改动。② **F61+F71（新增 F71）：`--split train` 的臂解析 + 只读化**。F61：`_resolve_report_arm(split=)` 补上 train 侧命名（`train` → `parent-smoke` → 通用回落），修复前实测抛 `E_EVAL_ARM_NOT_ALLOWED`（现场退出码 2）。**F71（修 F61 时才看见的更深一层）**：`cmd_eval_report` 把 train 的聚合塞进 `report["heldout"]` 再交给写路径 ⇒ 在 **gen-000（`decision="baseline"`）或 HITL `pending` 代**（都不在 `VERDICT_DECISIONS` 里 ⇒ 不受 sidecar 保护）会**覆盖 report.json 的 heldout 段**并写一条 `split="heldout"` 而数值来自 train 臂的台账行（只因带 `arm="train"` 才恰好被判据 1 的 `arm_of_row=="parent"` 过滤躲过），还会用 train 的熵曲线覆盖 `plots_data` ⇒ 修复前这条路径只是**碰巧**被 F61 的退出码 2 挡住。处置：新增 `_WRITABLE_SPLITS=("heldout",)`，非 heldout split 走**只读**（打印 + 落 `metrics/gen-NNN.<split>.readonly.json`，**无任何消费者**）；`scripts/run_m3_formal.sh --post` 的 `|| true` 一并去掉。③ **F67：冒烟胜率轴删除**。`smoke_n=2` ⇒ 胜率分辨率 0.5，而 `_SMOKE_WIN_RATE_MARGIN=0.2` **结构上不可达**（差值只能是 0/0.5/1.0）——它实际等价于一条没写清楚的硬条件，并附带 2–3% 的等价候选误杀；冒烟的定位是 **triage 而非证据**（≈¥1.7 换掉 ≈¥21 的 40 局），保留 error/illegal/fallback **三条安全硬门**（非法率与 fallback 是**步级**统计，2 局也有上千次尝试 ⇒ 有诊断力；局级胜率的诊断力归 C1 的 40 局）。④ **F70（用户裁决「只做 ② 标注」）**：`AcceptanceReport` 增 `baseline_selected`/`baseline_source`/`selected_baseline_pairs`/`baseline_selection_note` —— 上一代 `decision="accept"` ⇒ 本代起点基线就是**那个候选自己的测量**（含 winner's curse，n=40 下约 +5–10 pp 量级），产物里逐对标注 + 一句诚实边界；**不重测、不换篮子**（裁决：花钱的 ① 与破坏同篮子的 ③ 都不做）。⑤ **F68：两处环境性门禁噪声按根因修**（不是重试）。HTTP 桩：`_get_ok` 内部写死 2 s 超时，而用例断言「**一次**探测必须为真」⇒ 满载时把「慢」判成「没起来」；改为**有截止期的轮询**（`_wait_until_serving`，夹具与断言都用它）并把桩调用超时 5 s → 30 s。性能预算：`_p95_ms` 由**单批**改为 **best-of-3 批的最小值**（与同文件早已存在的 `_calibration_seconds`/`_io_seconds` 的 best-of-3 口径对齐）——瞬时争用属环境噪声，真退化会让**每一批**都超。⑥ **归档 run 的零成本反事实复核（¥0）**：用归档 staging 目录算 play 指纹 ⇒ 三代候选**都改变了 play 面**（`6f62007…`/`ee48cf5…`/`1751db1…` ≠ `af817e7…`），再按归档臂复算 C1/C5 ⇒ gen-001 过 C1+C5、gen-002 过 C1+C5、**gen-003 被 C5 拒**（truncation 0.175 > 0.100+0.05）⇒ 新门**可达**且 **C5 有真诊断力**（它此前因短路**从未被求值**）。**诚实边界**：这是**反事实**复算——三代候选臂当时跑的是**未打补丁的 v0**（F66，已修），且 gen-002 一旦被接受 gen-003 的起点就不是 v0 ⇒ 它只证明「门是可达的」，不证明「那个 run 本来会成功」。⑦ **护栏 24 条**（F62 单测 9 + 生成链 3 + F61/F71 3 + F67 2 + F70 2 + F68 2 + 既有用例的语义同步 3），`git stash push -- dice_ratchet/` 实测**修复前 80 项失败**（含签名缺失的级联；关键三条的失败形态逐条留证：F71 修复前**真的写**了 `gen-001/report.json`、并**覆盖**了 gen-000 的 heldout 段、F61 抛 `E_EVAL_ARM_NOT_ALLOWED`）⇒ 全量门禁 **2108 passed / 2 skipped / 0 failed**（`scripts/ci_gate.sh --with-slow` 退出码 0，含 node 侧与 golden）。⑨ **归档 run 上的只读实测**：`eval report --run-id m3-formal --split train --generation 3` 现在退出码 0、落 `metrics/gen-003.train.readonly.json`，而 `generations/gen-003/report.json` 与 `metrics/generations.csv` **sha256 逐字节不变**（修复前会写）。⑧ **诚实边界**：**不动**已花的 ¥197.67，不改变上一轮 `m3-formal` 的 `ok=false`（那是历史事实）；改变的是**下一轮判据的可达性与可读性**——修完之后判据 1 才第一次**可能**出现「可比的第二对」。H2 消融 / test 解锁 / `fixed_prompt` 基线**仍未跑**。**成本：¥0**（全部本地：代码 + 测试 + 文档 + 归档复算）。 | `dice_ratchet/evolve/judge.py`、`dice_ratchet/evolve/generation.py`、`dice_ratchet/evolve/schemas.py`、`dice_ratchet/evals/cli.py`、`dice_ratchet/evals/acceptance.py`、`tests/unit/{evolve,evals,memory,inference}/`、`tests/integration/evolve/`、`scripts/run_m3_formal.sh`、`runs/m3-formal/`（只读复核） |

| B-86 | 2026-10-14，用户裁决（F73 = **② 整块收拢**「ruleset 改 pin + 断言 fail-closed，其余键删除」；F74 = **认定为文档错误**「删掉『每代结束自动审计』的措辞」） | **`sandbox.audit.*` 从五个死键收拢为一个「有消费者的钉扎」+ 钉扎不符 fail-closed**。① **回查设计初衷**（初版 MOD-02 PRD §9 配置表 + §16 开放问题 3，首个提交 `17808fe`）：这五个键原本是一张**审计策略表**——`enabled` =「每代结束自动审计」、`surfaces` = 扫描出口可裁剪、`ruleset` =「**规则集版本（入报告）**」（配 NFR-02-3「报告含 `ruleset_version`/`run_id`/`config_hash`」⇒ 诉求是**证据可追溯**）、`fail_on` =「非零退出门槛」（§16 开放问题 3 的诉求是**本地调试放宽 vs CI 恒 critical**，**该问题从未裁决**）、`evidence_chars` = 证据裁剪宽度（FR-02-19）。② **它们为何全成死键（机制）**：实现侧把这五者都做成了 `run_audit()` 的**参数 + 同名默认值**（默认值与配置值恰好相同，所以无人察觉）——**「参数已存在」被误当成「配置已接线」**；唯一生产入口 `app.py::_audit_leak` 只传了 `split`/`config_dir`/`replay`/`self_test`（这四个本身就带 CLI flag 语义）。**最硬的证据**：该 yaml 块自 M0 骨架 `5d3574c` 建立后**从未被修改过一次**（`git log -L` 实测），B-82 把 `RULESET_VERSION` 1.0.0→1.1.0 并同步 golden 时**没人想起改它**——活键在规则语义变更时是必须跟改的。③ **F73 的落法（用户选 ②）**：**为什么不能「真接线让配置说了算」**——`ruleset_version` 是「这份 clean 按哪版规则判的」的唯一凭据，可追溯性**要求它不可被配置伪造**；且接线只作用于 CLI 入口，golden/单测直连 `run_audit()` 默认值 ⇒ 同一次审计经不同入口会写出**不同**的 `ruleset_version`，字段失去唯一含义。故：五键**删除**，只留 **`sandbox.audit.ruleset_pin`**（str，默认 `1.1.0`）；`app.py` 把它喂给 `run_audit(ruleset=pin)`；新增 **`check_ruleset_pin(pin, *, source)`** 断言 `pin == RULESET_VERSION`，不符即 **`E_ENV_AUDIT_RULESET`**（**新错误码**，`EXIT_CODE` 映射 **4 = 守卫轴**，**刻意不占结论轴 2**：`audit leak` 的 2 表示「含 critical 泄漏」，与「配置与代码不一致」要做的处置相反）；`run_audit` 内部同样过一遍该断言 ⇒ 报告里的 `ruleset_version` **在任何调用路径上都不可能不等于代码常量**；拒绝时**不产出报告**。④ **F74 落法**：删掉「每代结束自动审计」的措辞，并在 MOD-02 §9 写明**触发方式 = 显式 CLI**（`loop/`/`evolve/`/`evals/` 无任何 `run_audit` 调用点；正式实验也是跑完手工补刀）——FR-02-16..19 全是「给定 run 目录 → 扫描」，**从来没有自动触发的 FR**，故认定为**文档错误**而非未实现 FR。⑤ **§16 开放问题 3 结案**：`fail_on` 不做配置化；真需要本地放宽应落 **CLI flag**（例如 `audit leak --fail-on warn`），不让证据链退出码随配置文件漂移。⑥ **护栏 11 条**（`tests/unit/sandbox/test_sandbox_audit.py::test_b86_*`）：钉扎相符照常、四种不符形态 fail-closed、消息含两个版本 + 修法、**拒绝时不写报告**、`SandboxConfig.Audit()` 默认值 == 代码常量、**`configs/default.yaml` 的 pin == 代码常量**（正是 B-82 漏掉的那一步）、五个死键不再是合法扁平键而别名 `sandbox.audit.ruleset_pin` 仍可用、**CLI 端到端真的读了配置键**（pin 坏 ⇒ 退出码 4 + 不产出报告；pin 好 ⇒ 干净 0）。`git stash push -- dice_ratchet/ configs/` 实测**修复前 11/11 全红**。⑦ **诚实边界**：默认值仍是**字面量**（`infra` 不能 import `sandbox`：分层约束 + 循环导入）⇒ 用第 ⑥ 条的两条用例把「字面量 == 代码常量」钉住；`guard.enforce`/`scan_roots`/`process_boundary`、`honeypot.enabled`/`plan_path`/`self_test` 与 `audit` 是**同一个病**（全库 0 消费者）但**未在本轮处理**，另登记 F75；MOD-02 §17 里那条畸形的变更记录标签 `\| v?-2026-10-10 \|` 属历史遗留（本轮不动）。⑧ **成本：¥0**（纯本地）。 | `dice_ratchet/sandbox/audit.py`、`dice_ratchet/sandbox/__init__.py`、`dice_ratchet/infra/config.py`、`dice_ratchet/infra/errors.py`、`dice_ratchet/app.py`、`configs/default.yaml`、`tests/unit/sandbox/test_sandbox_audit.py`、`docs/prd/MOD-02-env-sandbox-PRD.md`、`docs/README.md`、`dice_ratchet/README.md` |
| B-85 | 2026-10-14，用户裁决 F72 = **② 删除**（「删除 html 下的 10 个文件即可，无人浏览」；同轮问「`sandbox.audit` 最初设计的初衷是什么」⇒ F73/F74 另裁） | **F72 关闭：整套 `docs/html/**` 手工 PRD 镜像删除（10 页 / 5,323 行；纯删除，¥0）**。① **它是什么**：`docs/html/MOD-01..09-*.html` + `index.html`，9 份 MOD PRD（共 4,035 行 markdown）的**手工重排版**（侧栏锚点 + hero 徽章 + 卡片样式），自 2026-10-05 起靠人手增量同步。② **为什么删**：**没有生成器、没有检查**（`docs/tools/` 只有 `gen_diagrams.py`；`scripts/ci_gate.sh` 的 G1–G6 无任何 HTML 步骤）⇒ 不进门禁、也不会红，只能靠人记得同步，长期必然漂移。**删除前逐页实测：落后 35 处**——**11 个整小节缺失**（MOD-03 §6.1 按调用方输出预算 / §7.1 三角色装配与 provenance；MOD-06 §9.0 模型角色与实验臂 / §9.1 评估局环境隔离 / §9.2 `--resume` 可重入 / §9.3 演化起跑前 git 门禁；MOD-07 §6.5 提案与补丁硬口径 / §6.6 失败自清口径 / §6.7 每代检查点与复用 / §8.2.1 C5 实测标定 / §8.2.2 C5 是「臂相关」的）＋ **24 行变更记录缺失**（MOD-02 1 / MOD-03 2 / MOD-06 2 / **MOD-07 7（停在 v0.5，现 v0.12）** / **MOD-08 9（停在 v0.6，现 v0.15）** / MOD-09 3）。三处自相矛盾可作证：MOD-06 镜像的变更记录宣布「新增 §9.0/§9.1」而正文无此节；MOD-07 的 C5 行写「见 MOD-07 §8.2.1/§8.2.2」而该页无此节（**页内悬空引用**）；MOD-07/MOD-08 的变更记录停在 9 月底 ⇒ 浏览器读者**看不到** D-M3-7、F62 取消 C2 统计门、`acceptance.json` 新字段。③ **替代与失效说明**：Spec §8.6 的「✅ 缺陷 F28 …落点」等历史行、M2/M3 检查单与 M1 验收报告里的 `docs/html/...` 提法**一律保持原样**（那是当时的落点记录，且都是纯文本非链接）；**本行是它们统一的失效说明：镜像已删，以对应 markdown PRD 为准**。④ **顺带纠正**：v2.27 行、任务检查单 F72 行里的「约 17 处落后」是**目测估计**，实测为 **35 处**（口径见②），两处文案按实测值更正——这正是「目测数字也会变成第二真相」的一个小样本。⑤ **成本：¥0**（无任何付费调用）。 | `docs/html/**`（删除 10 个文件）、`docs/模块拆分Spec-DiceRatchet.md`、`docs/tasks/2026-10-11-M3验收前置条件确认.md` |
| B-84 | 2026-10-14，用户指令「**检索所有和现状不符的文档，进行增量修改**」 | **全库文档一致性修订（纯文档，¥0）**：对照「代码 + 运行产物 + 本轮裁决」逐条修掉**当前状态**描述里与现状不符的句子，**不动任何历史行**（变更记录 `\| vX.Y \|`、`B-nn` 裁决行、带日期的结论段一律原样保留，必要时只追加带日期的 `⚠️ 20XX-XX-XX 修订` 短注）。① **判定表 C2**：`judge` 的判据/签名/错误码在 Spec §4.7、MOD-07 PRD §6.2/§8.2/§9/§11/§14、`evolve/README`、`docs/html/MOD-07-*.html` 全部改为「**候选 `play_hash` ≠ 起点 `play_hash`**」（含 fail-closed `C2:play_hash_unavailable`、`C2:no_play_change`）；`SPEC §4.5` 的 `report.json` 示例补 `paired["by_metric"]`；§4.7 **新增两行 MOD-07 → MOD-08 签名**（`compare_all_metrics`、`restore_arm_from_archive`）并补全 `evaluate_arm`/`compare_paired`/`evaluate_generation` 的真实参数面。② **冒烟门**：`smoke_n=2`（末 2 个 seed）、胜率轴已删（三条安全硬门）——修掉 MOD-07 PRD/HTML、`docs/README`、Spec 里所有「末 5 个 / 冒烟胜率 < parent−0.2」。③ **每代局数**：明确「**公式 `E_gen=114`（保守上限、与 `max_episodes_per_generation=120` 比较）vs **D-M3-7 起实际 74 局**（parent heldout 按 play 面继承）」，覆盖 Spec §8.6、MOD-06 PRD §8/§9.0/§15、`loop/README`、排期、HTML；成本同步为「账户口径 ≈¥0.52/局 ⇒ ≈¥38/代；官方高峰价 ⇒ ≈¥62–65/代」。④ **`eval report` / 台账 / 判据 1**：`--split train` **只读**（F71）、默认臂 `parent`、`--arm all` 不写台账、版本列以 `report.json` 为权威（F60）、`acceptance.json` 新增 `baseline_selection` 与每代 `play_hash`/`baseline_selected`/`baseline_source`（F70）——覆盖 MOD-08/MOD-09 PRD、`evals/README`、`dice_ratchet/README`、HTML。⑤ **审计**：`RULESET_VERSION` **1.1.0**、`future_room_leak` 逐局作用域、数字前导边界含 `.`、两张人工死名单已整删——覆盖 MOD-02 PRD/HTML 与 `memory/README`。⑥ **其它**：MOD-03 的 `resident_gb_est` 回填为 **4.6077**、`cache_prompt` 只对本地臂有效（API 侧命中率不可测）；MOD-04 补记 **F66** 的「模板必须从本臂 `harness_dir` 解析、否则 `E_EVOLVE_PATCH_INVALID`」；四份 PRD/模块 README 的**上游版本号**对齐（Spec v2.27 / MOD-06 v1.6 / MOD-07 v0.12 / MOD-08 v0.15）。⑦ **版本号去重（本次发现的自我不一致）**：MOD-07 changelog 原有**两个 `v0.11`**（D-M3-7 与本轮的 F62 批）、MOD-08 原有**两个 `v0.12`**（F-M3-9 与 F57）、M3 结果报告原有**两个 `v1.3`**（F66 与 F58–F60）⇒ 分别改为 `v0.11/v0.12`、`v0.12/v0.14`、`v1.3/v1.4`（并把行按版本降序重排、头部版本链补齐）；MOD-07 里两条同号的 `v0.5` 保留并给后者加「同版补记」。⑧ **`docs/tools/gen_diagrams.py` 同步**：排期 §4 是该脚本的产物，脚本里的三条任务文案（M1 三基线、M2 冒烟、M3 正式实验）一并改写并**重跑生成 `docs/assets/schedule-gantt.svg`**（3 行标签）。⑨ **登记 F73（新）**：`sandbox.audit.ruleset` 是**死键**——`configs/default.yaml` 与 `infra/config.py::AuditConfig.Audit` 仍写 `1.0.0`（**⚠️ 2026-10-14 更正：原文误写为 `EvolveConfig`；同轮进一步查明死键不止一个，见 F73/F74**），而 `run_audit(ruleset=)` 从不接收它（实际恒用代码常量 `RULESET_VERSION=1.1.0`）⇒ 配置里那个值既不生效也会误导（建议：把 ruleset 明确为**代码常量**并删键，或真正接线）。⑩ **诚实边界**：本轮**只动文档/注释**（外加 `docs/tools/gen_diagrams.py` 的文案与重生成的 SVG）；**不动**已花的 ¥197.67；`docs/html/*.html` 这套手工镜像**仍有约 17 处已知落后**（缺整节、变更记录尾部、示例 JSON 等），本轮按现状增量修订了徽章与关键技术点，**是否建生成器/删除留待 F72 裁决**。**成本：¥0**。 | `docs/**/*.md`（Spec / 10 份 PRD / tasks / reviews / 排期 / README）、`dice_ratchet/**/README.md`、`docs/html/*.html`、`docs/tools/gen_diagrams.py`、`docs/assets/schedule-gantt.svg`、`tests/unit/evolve/test_evolve_evidence.py`（仅注释） |

### 11.2 待裁决

（无。v1.4 起，新的 PRD 临时决策登记到这里，编号从 P-31 开始。）

**裁决原则**：涉及 **INV-1/INV-2/INV-5** 的条目优先级最高，必须在所属里程碑开工前裁决。**B-07（seed 协议）已裁决并全量对齐**，后续任何 seed 区间引用必须回查 MOD-08 PRD §4。

---

## 12. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v2.29 | 2026-10-14 | **`sandbox.audit.*` 整块收拢为「规则集版本钉扎」+ 钉扎不符 fail-closed（登记 B-86；依据用户裁决 F73 = ② 整块收拢 / F74 = 认定为文档错误）——代码改动，¥0**。① **回查初衷**：初版 MOD-02 PRD 的这五个键是一张「审计策略表」（开关 / 扫描面 / **规则集版本入报告以求证据可追溯** / 本地调试 vs CI 的分级 / 证据裁剪宽度），但实现侧只做成了 `run_audit()` 的**参数 + 同名默认值**，**配置→调用的接线从未发生**（全库 0 消费者；该 yaml 块自 `5d3574c` 后**从未被改过一次**，B-82 改规则语义时也没人想起它）。② **落法**：五键删除，只留 **`sandbox.audit.ruleset_pin`**；`app.py` 把它喂给 `run_audit(ruleset=…)`；新增 **`check_ruleset_pin()`** 断言「== 代码常量 `RULESET_VERSION`」，不符即 **`E_ENV_AUDIT_RULESET`**（**新错误码**，映射退出码 **4 = 守卫轴**，**刻意不占结论轴 2**），**拒绝时不产出报告**；`run_audit` 内部同样断言 ⇒ 报告的 `ruleset_version` 在任何路径上都不可能不等于代码常量（**不可被配置伪造**）。③ **为什么不能真接线**：那样 CLI（读配置）与 golden/单测（直连默认值）会写出**不同**的 `ruleset_version`，字段失去唯一含义。④ **F74**：删掉「每代结束自动审计」（`loop`/`evolve`/`evals` 无任何 `run_audit` 调用点；FR-02-16..19 全是「给定 run 目录 → 扫描」）⇒ 认定为文档错误，MOD-02 §9 写明触发方式 = **显式 CLI**；§16 开放问题 3（`fail_on` 是否允许 `warn`）结案：不做配置化，真需要就落 CLI flag。⑤ **护栏 11 条**（`test_b86_*`，含「yaml 的 pin == 代码常量」「死键不再是合法扁平键」「CLI 端到端真的读了配置键」），`git stash push -- dice_ratchet/ configs/` 实测**修复前 11/11 全红**。⑥ **诚实边界**：默认值仍是字面量（`infra` 不能 import `sandbox`），由用例钉住；`guard.*`/`honeypot.*` 的同族死键**未处理**（另登记 F75）。**成本：¥0**。 |
| v2.28 | 2026-10-14 | **删除 `docs/html/**` 手工 PRD 镜像（登记 B-85；依据用户裁决「删除 html 下的 10 个文件即可，无人浏览」）——纯删除，¥0**。① **删除对象**：`docs/html/MOD-01..09-*.html` + `index.html`（10 页 / 5,323 行；9 份 MOD PRD 共 4,035 行 markdown 的手工重排版）。② **删除理由（实测而非感觉）**：**没有生成器、没有 CI 检查**（`docs/tools/` 仅 `gen_diagrams.py`；`ci_gate.sh` G1–G6 无 HTML 步骤）⇒ 不进门禁也不会红，只能靠人记得同步；删除前逐页比对 PRD 标题与变更记录，**累计落后 35 处 = 11 个整小节 + 24 行变更记录**（MOD-07 变更记录停在 v0.5、MOD-08 停在 v0.6；MOD-06 的变更记录宣布新增 §9.0/§9.1 而正文无此节；MOD-07 的 C5 行写「见 §8.2.1/§8.2.2」而页面无此节）。③ **不动历史行**：Spec §8.6 的 F28 落点、M2/M3 检查单、M1 验收报告里的 `docs/html/...` 提法保持原样（当时的落点记录，纯文本非链接），失效说明统一由 B-85 承担。④ **更正**：「约 17 处落后」是本轮 v2.27 的目测估计，实测 35 处，已按实测值更正。⑤ **诚实边界**：本轮**零代码改动、零付费**；`docs/html` 目录已不存在。**成本：¥0**。 |
| v2.27 | 2026-10-14 | **全库文档一致性修订（登记 B-84；依据用户指令「检索所有和现状不符的文档，进行增量修改」）——纯文档，¥0**。① 逐文件修掉**当前状态**的过期断言：C2（判据 + `judge` 签名 + 错误码 + `paired["by_metric"]`）、冒烟（`smoke_n=2`、删胜率轴）、**公式 `E_gen=114` vs 实际 74 局**与成本（≈¥38/代；高峰 ≈¥62–65/代）、`eval report --split train` 只读、默认臂 `parent`、版本列以 `report.json` 为权威、`acceptance.json` 的 `baseline_selection`/`play_hash`/`baseline_selected`、`RULESET_VERSION=1.1.0` 与逐局作用域、`resident_gb_est=4.6077`、`cache_prompt` 只对本地臂有效、F66 的臂内模板解析；② **Spec §4.7** 补全 `evaluate_arm`/`compare_paired`/`evaluate_generation` 真实签名并新增 `compare_all_metrics`/`restore_arm_from_archive` 两行；§4.5 示例补 `by_metric`；③ **版本号去重**：MOD-07 `v0.11`×2 → `v0.11/v0.12`、MOD-08 `v0.12`×2 → `v0.12/v0.14`、M3 报告 `v1.3`×2 → `v1.3/v1.4`（并按版本降序重排、头部链补齐）；④ `docs/tools/gen_diagrams.py` 文案同步 + 重跑 `schedule-gantt.svg`；⑤ **新登记 F73**（`sandbox.audit.ruleset` 死键：配置写 1.0.0、无消费者，报告恒用代码常量 1.1.0）；⑥ **诚实边界**：只动文档/注释；`docs/html/*.html` 仍有约 17 处已知落后（F72 待裁决）。（**⚠️ 2026-10-14 同日更正与后续**：实测为 **35 处**落后——**11 个整小节 + 24 行变更记录**；该套镜像**已于同日整套删除**，见 v2.28 / B-85。） |
| v2.26 | 2026-10-14 | **「取消 C2」的落地 + 判据可达性修复（登记 B-83；依据用户裁决：C2 落法 B / F70 只标注 / 现在实施）**。① **F62**：C2 由「至少一指标显著提升」改判为「**`play_hash` 确实变了**」（确定性、¥0、fail-closed），统计量降级为 `paired["by_metric"]` 报告项（`compare_all_metrics` 第一次接上生产调用点），多候选排序改**连续轴优先**；**为什么不能只删 C2**：零改动的 accept 在判据 1 里是 `not_applicable` ⇒「≥1 accept」会第二次变成退化通过。② **F61**：`--split train` 认 `train`/`parent-smoke` 臂（修复前 `E_EVAL_ARM_NOT_ALLOWED`，退出码 2）。③ **F71（新登记）**：`--split train` 原先会**覆盖 `report.json` 的 heldout 段**（baseline/pending 代不受 sidecar 保护）+ 写一条数值来自 train 臂的 heldout 台账行 + 覆盖 `plots_data` ⇒ 非 heldout split 现为**只读**（打印 + `metrics/gen-NNN.<split>.readonly.json`）。④ **F67**：删掉冒烟胜率轴（n=2 分辨率 0.5 > margin 0.2，结构上不可达），保留 error/illegal/fallback 三条安全硬门。⑤ **F70**：判据 1 产物逐对标注「基线是选择后的测量」（winner's curse，用户裁决「只做 ② 标注」，不重测、不换篮子）。⑥ **F68**：HTTP 就绪探测改**有截止期的轮询**（原为写死 2 s 的单次探测，满载下把「慢」判成「没起来」）+ 桩调用超时 5 s→30 s；性能预算 `_p95_ms` 改 **best-of-3 批最小值**（与同文件折算口径的 best-of-3 对齐）。⑦ **归档 run 零成本反事实复核**：三代 staging 的 play 指纹**都变了**；按归档臂复算 C1/C5 ⇒ gen-001/002 可 accept、**gen-003 被 C5 拒**（truncation 0.175 > 0.150）⇒ 新门可达且 **C5 有真诊断力**（此前因短路从未求值）。⑧ **护栏 24 条**，`git stash push -- dice_ratchet/` 实测**修复前 80 项失败**（含签名级联；F71 修复前真的写了 `report.json` 并覆盖了 gen-000 的 heldout 段）。⑨ **诚实边界**：accept 语义改为「打法变了且无判定出的退化」（不再是「显著变强」）；只改学习侧的提案会被 C2 合法拒绝；不动已花的 ¥197.67；H2/test/`fixed_prompt` 仍未跑。**成本：¥0**。 |
| v2.25 | 2026-10-14 | **F58/F59/F60 修复（登记 B-82；依据用户指令「先修复 F58-60」）**。① **F58**：`future_room_leak` 改**逐局作用域**（`HiddenTokenIndex` + 派生面=扫描面 + `unscoped` 兜底 + `reflections/*` 逐局）⇒ 同一批工件复跑 `audit leak`：`critical/future_room_leak=5852/204 findings` → **`clean/0 findings`**。② **F59**：数字 run 前导边界加 `.`（小数部分不算 seed；整数形态与「整数部分等于 seed 的浮点」仍然命中）。③ **F60**：台账版本列**以 `report.json` 为唯一权威**（`eval report` 不再伪造；无报告时退回臂归档的实测版本并记 `version_source`），分歧**非静默**（事件 + stderr）；**修好被污染的 gen-003 行** `(3,2)→(0,0)`，`eval acceptance` 的假「单调性不成立」被撤销（回到 `comparable=0/na=3`）。④ 顺带统一 `cmd_eval_report` 的 train 段口径（F64 的第二处残留）。⑤ 护栏 9 条，修复前 **5 条失败**；全量门禁 2096 passed。**成本：¥0**。 |
| v2.24 | 2026-10-14 | **D-M3-7：按用户口述架构重写「起点继承」（登记 B-81；逐字口径与逐步代码路径对照见 [架构对照](reviews/2026-10-14-M3-DM37架构改造与7步对照.md)）**。① **判据换轴**：继承与判据 1 的同起点豁免从「上一代 accept / 版本号相同」改为 **`play_hash` 相同**（`prompts/decision.md` + `config.json` + `tools/*.py`），因为 D2=A 让**拒绝也推进版本号**。② **每代只测候选**：起点 heldout 直接复用同 play 面的已有测量（gen-000 基线臂或某代 accept 的候选臂），每 cycle 局数 **114 → 74**（省 ≈40 局 / ≈¥21 / ≈1.7 h 每代）。③ **`heldout` 段改定为「本代结束时在役那份 harness 的测量」**（取代 F57「恒取 parent」）：接受 ⇒ 记候选的数（写台账行），拒绝 ⇒ 记起点那份（`inherited:gen-00X`，不写行）；否则「接受了一代」在台账里永远不可见。④ **D2=A**：拒绝时 `v_{N+1} = v_N + dead_ends.md`（推进指针 + commit，**无快照、无 tag**；账本不带 `versions` 键；git 失败回滚指针）。⑤ **D1=A**：`harness/dead_ends.md` 进提案 prompt（`HarnessSnapshot.context`，schema `1.2.0`）但**禁止候选改动**，渲染处脱敏，system prompt 加「不得重复」硬约束。⑥ **F64**：训练局标签 = 它服务的那一代；train 段取 `take("train", g)` 的块 + `arm="train"` 真过滤 + `source∈{measured,insufficient,unavailable}` fail-closed。⑦ **F69**：CI 不自洽时绘图抛非 `InfraError` 炸整代 ⇒ `yerr` 夹到 ≥0。⑧ 同批：`harness_version` 由 `retained_version` 决定（修「接受后仍写旧版本号」）、判据 1 豁免改 play_hash、`_episode_stages` 跟随继承链、报告 schema 加两字段；**23 条新护栏在修复前失败**（`git stash` 实测）。⑨ **登记 F67（冒烟胜率门 n=2 无诊断力）、F68（环境性门禁噪声：回环 HTTP / 50 ms 性能预算）、F70（winner's curse：被接受的候选测量成为下一基线）**。⑩ **诚实边界**：不动已花的 ¥197.67，不改变上一轮 `ok=false`；改变的是判据**可达性**。**未做**：F58/F59/F60/F61/F62、H2 消融、test 解锁。**成本：¥0**。 |
| v2.23 | 2026-10-14 | **F66（🔴🔴）：候选补丁从未进入决策 prompt —— M3 判据 2 在本 run 上没有数据（依据 [结果报告](reviews/2026-10-14-M3正式实验主体结果.md) §9.6）**。① **触发**：用户追问「⑤ parent heldout 与 ⑧ 候选 heldout 有什么区别」。② **实测**：候选臂与 parent 在**同 40 个 seed 上的首个决策点 `prompt_hash` 逐字节相同（40/40）**，整臂集合交集 320（parent 4776 / 候选 4393），首个分叉步中位数 **8**（= 两条轨迹**动作**首次分歧处，step 0 从无分歧）——**若候选模板真含那 6 行补丁，交集必须为 0**。③ **根因**：`evals/harness.py:849` `executor = runner or _default_runner(harness_dir=harness_dir, …)` ⇒ **注入 runner 时 `harness_dir` 完全不参与策略构造**；而 `evolve/judge.py:538-540` 给**每一次候选评估**都注入 `EvalRunnerAdapter`（其 `policy_factory` 在 `loop run` 起跑时用 **run 级 `harness/`** 建好）⇒ staging 目录进了臂归档 provenance（`harness_version=0`）却从未到达 `PolicyAgent`；`resolve_path` 的探测路径 `staging/harness/prompts/decision.md` 不存在，回落 cwd 的**未打补丁**模板。④ **影响**：判据 2 无数据；「0 accept」=「补丁从未被测试」而非「补丁无效」；三次 C2 拒绝形式正确、内容空洞；**`loop run` 路径下的所有候选测量都受影响**——**M2 §3.2 冒烟（候选 +5.07 pp）与 M3 彩排（候选 0.35 vs parent 0.10）的结论一并待复核**；不受影响的是 `eval ablation`、`eval report` 重算、M1 基线、以及各代 parent 测量。⑤ **噪声底升级**：三次「候选」测量其实也是同一份 v0 ⇒ 同一 harness 在同一批 40 seed 上共 **7 次独立测量**，`win_rate` = 0.175/0.100/0.125/0.150/**0.225**/0.100/0.150 ⇒ 均值 0.1464、SD 0.041、**极差 12.5 pp**；候选组均值 0.167 vs parent 组 0.131、Welch t=+1.00 ⇒ **不可区分**。⑥ **修法**：让 runner 感知 per-arm `harness_dir`（或候选评估不注入 loop runner）；**必需护栏**：臂归档记**实际加载的 `template_hash`** + MOD-07 断言「有 `prompt_patch` 的候选其 `template_hash` ≠ parent」fail-closed（该护栏会让 F66 在 30 秒内现形）。⑦ **诚实边界**：不追回已花的 ¥197.67，不改变「判据 1 `ok=false`」；改变的是**这批数字能支持什么结论**——本 run **不能再用来回答「补丁有没有效」**，判据 2 必须修复 F66 后重跑。⑧ 同批登记 **F63**（候选补丁哈希不入 provenance）、**F64**（训练局 `generation` 差 1 ⇒ train 段只拿到 4 局冒烟）、**F65**（parent 每代重测）。 |
| v2.22 | 2026-10-14 | **M3 正式实验主体结果（登记 B-80；依据用户指令「现在开始正式实验」）**。① **执行**：run `m3-formal`（`config_hash=06d684e260540893`、`C1`/hard×6/H1 40 局/HITL off/反思开/真实 `harness/`）跑完 gen-000 基线 + 3 代 = 382 局、19 h 50 min、52,319 次调用；**无 accept ⇒ `harness/version.json` 仍 v0、无 commit/tag**；H2/test **未消耗**。② **判据 1**：`metrics/acceptance.json` ⇒ `accepts=0 / comparable_pairs=0 / not_applicable_pairs=3 / monotonic_ok=false / ok=false`，**唯一原因是退化通过闸**（三代与基线同为 v0 ⇒ F57 同版本豁免）。③ **候选**：3 个 `prompt_patch` 全部止步 C2（`win_rate` delta +2.5/+7.5/+5.0 pp，`p=1.000/0.549/0.754`）；**零成本换口径复算**（`mean_stages_cleared` 配对 bootstrap）delta +0.05/+0.325/+0.15 关、CI 全含 0 ⇒ 拒绝结论与指标选择无关。④ **噪声底（本轮最有价值的实测）**：同一份 v0 在同一批 40 seed 上测 4 次 ⇒ `win_rate` 0.175/0.100/0.150/0.100（**极差 7.5 pp**）、`stages` 4.425/4.250/4.150/4.325（极差 0.275 关）⇒ 判据 1 的轴在 n=40 上噪声主导，结论应为「**不可判**」而非「没变强」。⑤ **基线锚点**：`random` 0.000/2.350 关、`heuristic` **0.325/4.975 关**（免费，新六位 H1）⇒ `C1` 全面劣于启发式；`fixed_prompt` 仍缺（≈¥35）。⑥ **成本**：高峰价估算 ¥344.44 / **实际扣减 ¥197.67**（≈¥0.52/局）；`llm.degrade` 99.6% 经复核**不额外花钱**。⑦ **登记 F58–F62（待裁决）**：审计 `future_room_leak` 跨局误报（逐局复核 280 局/96,583 行 0 命中）、`seed_leak` 误把浮点小数当 seed、`eval report` 伪造版本列覆盖台账（**可把 gen-3 从 `not_applicable` 翻成假「单调性不成立」**，有 before/after 实证）、`eval report --split train` 无可解析臂（退出码 2）、C2 恒用 win_rate/McNemar 与 D-3/B-39 口径不一致（本轮不影响结论）。⑧ **建议（未执行，需点头）**：先修 F58–F62 并复跑 `audit leak`（¥0）、按本轮 4 代数据首次回填 C5 `tol` 与 C1–C4 阈值（¥0）；**H2 消融与 test 解锁建议留到「修完判据 + 出现真 accept」之后**（0 accept ⇒ `no_self_edit` 与 `full` 构造上相同；test 每里程碑只有一次）。 |
| v2.20 | 2026-10-12 | **处置 F57：代际口径定死为「起点臂」+ 台账加 `arm` 三键 + parent 测量跨代继承（登记 B-77/B-78）**。① **口径（用户裁决）**：每一代的 `heldout` 是**这一代的起点（parent）**在哪；候选的测量归 `candidates/*.json` 与 `metrics/arms/`。② **代码**：`evaluate_generation` 的 `heldout` 恒取 `parent.metrics`（diversity/gain 同步同臂）；`eval report` 默认臂改 **parent**、未知臂名报错、`--arm all` 不进台账；台账列清单加 `arm` 并升为 **`(generation, split, arm)` 三键 upsert**；绘图主曲线按臂分层；报告新增 `heldout.source`。③ **判据 1 豁免**：相邻两代 `harness_version` 相同 ⇒ `not_applicable`（不判失败、不计入可比对数）。④ **成本**：新增 parent 测量**跨代继承**（上一代 accept 的候选已被完整测过）⇒ 命中时 heldout **0 次 `evaluate_arm`**，一代省 ≈20 局 / ≈¥17；更关键的是**不再引入同 harness 重测的不可复现抖动**（实测同一 harness 两次测量差 5pp = 本 run 噪声底）。⑤ **零成本实证**：在真实彩排产物上重放修复后口径，判据 1 从「`monotonic_ok=true`（读的是候选的 0.35）」回到「gen-001 与 gen-000 同为 v0 ⇒ `not_applicable`，`ok=false` 的唯一原因是 `accepts=0`」。⑥ **新增/改护栏 12 条**（修复前失败的证据逐条记在各 B 行）。**门禁**：`ci_gate.sh` EXIT=0。**成本**：¥0。 |
| v2.21 | 2026-10-13 | **M3 正式实验起跑：口径 + 预算/限流/墙钟上限落定并脚本化（登记 B-79；依据 M3 检查单 D-M3-6 / F-M3-8，用户指令「现在开始正式实验」）**。① **run-id `m3-formal`**，只有 4 个 run 级 override（`C1` / `mode=eval` / `seed=0` / `evolve.hitl=false`），其余全走生产默认 ⇒ **篮子 = H1（40 局，代际裁判）**、**局型 = hard × 6**（D-M3-1）、**3 代 × K=30**（`--seeds train:1000-1089`）。② **harness 目录 = 真实 `harness/`**：`--hitl=false` 下的 accept 经 MOD-09 提交并打 tag `harness-v{N}`（G6 证据链），起跑处工作区必须干净（F47-A 同源）。③ **一次性资源边界**：主体不碰 **H2 / test**；消融（H2）与 test 是独立步骤、各有显式确认开关，脚本按 `events.jsonl` 做 seed 级 fail-closed 断言。④ **预算（高峰价上界；空闲档减半）**：主体 382 局 ≈ **¥318–336 / ≈16 h**；消融 H2（3×40 局）≈¥100；test（100 局）≈¥84；三基线（`fixed_prompt` H1 40 局）≈¥35 ⇒ 全 M3 ≈ **¥540**。⑤ **单代墙钟上限 6 h**；限流一律默认（`max_inflight=4` / `n_workers=2` / `train_concurrency=1`，改它即换 run-id 族）。⑥ **脚本** `scripts/run_m3_formal.sh`：`--plan`/`--preflight`/`--status`/`--cost`/`--post` 免费，8 条断言 fail-closed（不过则退出码 4，**不花钱**）。⑦ **零成本实证**：`--preflight` 8/8 过、`loop run --dry-run` 得 `config_hash=06d684e260540893`、`ci_gate.sh --with-slow` EXIT=0。⑧ **复核彩排报告 §6.3**：`llm.degrade` 12,352/12,397 与 `llm.api.call`/`llm.call` 各 12,397 条一一对应 ⇒ `grammar_unavailable` 只是对成功调用打的标签，**不额外花钱**。⑨ **诚实边界**：起跑前实测 DeepSeek 余额 **¥352.78**，只覆盖主体；消融/test 需另行确认（可能需充值）。⑩ 本版**不动**排期日历日期。 |
| v2.19b | 2026-10-12 | **登记 F57（未修、待裁决）：台账缺 `arm` 列 + 重算按 `(generation, split)` 覆盖同一行**（§11.1m B-77）。准确表述：`report.json` 内部**自洽**（`winner=None ⇒ parent.metrics`）—— 问题在**台账没有臂维度**而重算默认臂会覆盖它；实测同一批产物判据 1 结论可翻转（`单调性不成立` ↔ `monotonic_ok=true`）。并**更正** [彩排执行结果](reviews/2026-10-12-M3彩排执行结果.md) §5 表的一处笔误（gen-000 基线 `0.10` → 实测 `0.15`）。**成本**：¥0。 |
| v2.19 | 2026-10-12 | **处置 F56：台账 + 绘图收敛到一处（登记 B-76）**。① **现场（M3 彩排实测）**：`loop run` 跑完 2 代后 `plots/` **全空**、`metrics/generations.csv` **缺 gen-000 基线行** ⇒ 彩排 6 项判据里**2 项必然不合格**（「5 张 PNG 有数据点」「判据 1 判定器能给出结论」），与彩排质量无关。② **根因**：台账与绘图**各写一半**且靠调用方记得 —— 绘图只挂在 `eval report`（`_plot_or_warn`），MOD-06 `write_gen000_baseline` 与 MOD-07 `_finish_report` 都不调；台账只由 MOD-07 与 `eval report` 写，基线不写 ⇒ 缺 gen-000 行。③ **修法**：新增 `evals/harness.py::ledger_and_plots(run_dir, generation, report, cfg=)` **一次写全**，三方共用（MOD-06 基线 / MOD-07 代末 / `eval report`）；删除 `evals/cli.py::_plot_or_warn`。纪律不变（F-M3-3 第 7 条）：失败不阻塞、但必须非静默（`evolve.ledger` / `infra.plot_skipped`）。④ **顺带**：`plot_generations(..., close_after=True)`（生产路径每代都调，不关会撞 matplotlib 20-figure 上限；默认 False 不变，彩排判据要在返回后读 figure）。⑤ **护栏 4 条**（修复前 3 failed）：`test_f56_ledger_and_plots_writes_ledger_row_and_pngs` / `...degrades_without_raising` / `test_f56_gen000_baseline_enters_ledger`（真实基线路径断言台账行 **且** 5 张 PNG）。⑥ **彩排结果的诚实边界**：本次 run 的这两块产物是**事后零成本补跑** `eval report`（generation 0/1）得到的；补跑后 `rehearsal check` **6/6 合格**，但**不得**读成「一次跑完就合格」。⑦ **门禁**：`ci_gate.sh` EXIT=0（**2036 passed / 2 skipped**）。⑧ **成本**：¥0（本修复）；彩排本身 ≈¥79.15。 |
| v2.18 | 2026-10-12 | **处置 F55：只读命令不再被 `config_hash` 硬门挡住（登记 B-75）**。① **现场（彩排实测）**：`rehearsal check --run-id m3-rehearsal` ⇒ `[E_IO_RUN_CONFLICT] … eaf6f18ce6e7865a != 9403576ed90e79ba`。根因：`loop run --generations 2` 把 `loop.max_generations=2` 写进 `config_hash`，而 `rehearsal check` **没有** `--generations` 这个 CLI 面（算出默认 3）。**后果**：`scripts/run_m3_rehearsal.sh` 的结尾那一关就是它 ⇒ **彩排永远不可能「合格」**（与彩排质量无关）。实测对照：`loop run --dry-run --generations 2` ⇒ `eaf6f18ce6e7865a`；`rehearsal check --dry-run` ⇒ `9403576ed90e79ba`（唯一差异是 `loop.max_generations`）。② **修法**：把「写侧不变量」与「只读定位」分开 —— `resolve_run_dir(..., strict_hash=False)`（定位已有 run 但不校验 hash）+ `cli_init_run(..., read_existing=True)`（且**不要求** `state.json`）+ `evals/cli.py::_paths(read_existing=True)` 接到 6 个只读 handler（`eval report` / `eval diversity` / `eval acceptance` / `eval ablation` / `eval test --check` / `rehearsal check`）。**写侧不变**：`loop run` / `--resume` / `baseline`（新建）仍硬校验 —— 「两套配置不许写同一个 run」保持。③ **护栏**：`tests/unit/loop/test_loop_cli.py::test_f55_readonly_command_reads_run_with_different_max_generations`（修复前实测 `15b74fdfb31aaf1a != d9324d7d253e208e`；**反向断言 `--resume` 仍硬拒**，防止把「只读放开」误扩成「写侧也放开」）。④ **直接收益**：`rehearsal check` 现在读得到彩排 run，`arm_archive_present` **OK（1 个臂归档）**、`one_shot_resources_untouched` **OK（H3 内 20 个 seed、无存证）**。⑤ **门禁**：`ci_gate.sh` EXIT=0（**2033 passed / 2 skipped**）、`--with-slow` EXIT=0（**2042 passed**）。⑥ **成本**：¥0。 |
| v2.17 | 2026-10-11 | **处置 F54：held-out 篮子可配置（彩排不再跑 H1，登记 B-74）**。① **现场（实测事故）**：`scripts/run_m3_rehearsal.sh` 起跑 17.5 min，评估局落在 **H1（一次性资源）**而不是脚本声明的 H3 —— 已落盘 `e-731000…731004-g000-parent.jsonl`（3 局完整 + 2 局中断），代价 ≈¥5.1（792 次 deepseek-flash 调用，2.15M prompt + 102k completion tokens）+ H1 的 5 个 seed；事件里 `"basket":"H1"` 7 次、H3 **0** 次。② **根因**：held-out 篮子**两处写死且无配置项** —— `loop/generation.py::write_gen000_baseline(basket="H1")`（生产调用方从不传它）与 `evolve/judge.py::CandidateEvaluator.heldout_basket = HOLDOUT_BASKET = "H1"` ⇒ D-M3-5 裁决 A 的「彩排只用 H3」在**代码层不可达**；「声明与实际不一致」这件事**没有任何检查**能发现。③ **修法（默认行为完全不变）**：新增 `evolve.heldout_basket`（默认 `H1`）**一个键同时决定** gen-000 基线与候选 arm 的 held-out 评估 ⇒ 两者不可能再分叉（C0 的「同预算」前提）；`write_gen000_baseline(basket=None)` 缺省从该键取；`_build_evaluator` 转发给 `CandidateEvaluator`（缺键时不传、回落类级默认，不静默换篮子）；`EvolveConfig.validate()` **前移**校验篮子名必须 ∈ `H1|H2|H3`（修复前拼错要等到评估那一刻才由 `resolve_basket` 抛错 —— 那时代价是「gen-000 已经花了钱」）；彩排脚本显式设 `DICERATCHET_EVOLVE__HELDOUT_BASKET=H3` 并在起跑前 **fail-closed 断言**（退出码 4）。④ **护栏 7 条 + 修复前失败证据**：修复前 `TypeError: EvolveConfig.__init__() got an unexpected keyword argument 'heldout_basket'`；**半成品反例**（只加键、不改接线）实测 `config.evolve.heldout_basket=H3` 生效而 `HOLDOUT_BASKET=H1` + `write_gen000_baseline` 默认 `H1` ⇒ 篮子仍是 H1；新用例覆盖**真实落盘文件名**（`tests/integration/test_loop_closure.py::test_f54_gen000_baseline_uses_configured_basket`）、接线与缺键回落（`tests/unit/evolve/test_evolve_generation.py::test_f54_*`）、配置面默认/覆盖/5 个非法名 fail-closed（`tests/unit/infra/test_config.py::test_heldout_basket_*`）。⑤ **零成本验证**：启发式探针 1 代实测 `"basket":"H3"` 21 次、`H1` **0** 次。⑥ **诚实边界**：本修复**不追回**已花的 ≈¥5.1，也不能让那 5 个 H1 seed 变得「未用过」；默认行为不变（正式实验仍走 H1）；彩排本身是否合格见后续报告；`tol` 与 `c1–c4` 阈值仍未标定。⑦ **原始现场**（sha256/尺寸/判决/config_hash）登记在 [M3 彩排事故记录-F54](reviews/2026-10-11-M3彩排事故记录-F54.md)。⑧ **成本**：¥0（本修复）；事故 ≈¥5.1。 |
| v1.0 | 2026-09-30 | 首版：9 模块 + MOD-10 预留、7 条不变量、全局契约、质量门 |
| v1.1 | 2026-09-30 | 配套 9 份模块 PRD 产出；回填 8 项跨模块契约（B-01..B-08，含 seed 协议全量对齐与 `generations.csv` 归属裁定）；新增 §11 待回填决策登记表（27 项待裁决） |
| v1.2 | 2026-09-30 | 新增 GAME 系列 PRD（游戏本体：GAME-00/01/02，见 [docs/README.md](README.md)）；登记 P-28..P-30：游戏本体改为前后端分离网页实现（MOD-11 待立项，MOD-01 转为纯 Python 等价移植）、动作集统一与新增、难度配置化与失心粒度上移 |
| v1.3 | 2026-09-30 17:21 | 按 [MOD PRD 评审](reviews/2026-09-30-MOD-PRD评审.md) 修订：裁决 P-28/29/30 并结案 19 项待裁决条目；§4 全面对齐 GAME（obs-agent-1.0.0、决策计划、三层校验唯一口径、轨迹路径与 report 写入分工）；新增 §4.7 模块间调用签名表；§8 拆分为 8.1–8.6，新增「一局」定义与成本模型；INV-7 主指标改为 outcome/stages_cleared/hearts_left/battles_won；新登记 B-09..B-22 |
| v1.4 | 2026-09-30 17:49 | 确认 v1.3 全部裁决；剩余 11 项待裁决全部确认并回填正文（B-23..B-33），另收录 3 项 PRD 内未登记决策（B-34..B-36）；新增 §1「不变量实施细则」；P-06、P-17 的登记文字按 v1.3 后的实际设计调整；§11.2 清零 |
| v1.5 | 2026-10-05 | M1 验收 §13.2 的接口面回填（不改架构与模块边界）：§4.4 明确**冻结基线模板是包内工件**（`dice_ratchet/agent/prompts/fixed_v1.md` / `task.md`，不进 `harness/`、不进 `artifact_hash`；基线由 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")` 承担，F12/F22）；§4.6 补 `baseline --resume` 与「两类续跑语义」、并把「覆盖并入 `config_hash` → 同 run-id 只能同族」写成命令面约定（F10/F18）；§8.6 把 F20 的「`seed_timeout_s` 未被消费」改为「两道闸真被执行」，把 F22 的「基线成本 ×3.1」口径警告改为「已修，按 `D·t_d` 计算」 |
| v1.6 | 2026-10-05 | **§8.6 成本模型按 n=10 实测回填**（用真实 8B 跑完 `fixed_prompt` n=10 子集并归档于 `runs/m1-real-h1n10b/`，seed 2000–2009）：新增一条实测条目——n=10 **墙钟 3.32 h**、模型时间 3.26 h、1,551 次调用、`t_d` p50 7.46 s / p95 10.13 s、schema 全失败率 5.8%、`fallback` 122 次、`illegal_rate=0.3021`、**`win_rate` 0/10 = 0.0%（CI [0.000, 0.278]）**、`D` 均值 **97.1**（中位 69、min 33、max 245）；并把 §14.18 的「单局 43 min / n=40 29–34 h」标注为**按重尾高端 2 局外推的上界**（按本次均值外推 n=40 模型独占下限 ≈13.1 h）。同时修正「单局墙钟 × `n_workers`」的表述：实测 `d_wall / d_model = 1.02`，**`n_workers` 不放大单局墙钟**，`n` 局墙钟 ≈ `n × 单局模型时间`。明细见 [M1 验收报告 §14.19](reviews/2026-10-04-M1验收报告.md) |
| v1.7 | 2026-10-06 | **M2 开工 D-1/D-2/D-3 裁决回填**（依据 [M2 开工检查单](tasks/2026-10-06-M2开工检查单.md) §2，登记 B-37..B-39）：§8.6 新增「2026-10-06 M2 开工定价」条目与「bring-up 口径」段。① **D-1**：`loop.max_episodes_per_generation` 170→**120**、`C`（`loop.n_candidates`=`evolve.n_candidates`）2→**1**、`S`（`smoke_n` 两处）5→**2**、`H` 保持 **40** → `E_gen=114` 局（≈37 h/代）；写明 `smoke_n` 无自动校验、`H` 的 `(1+C)` 支臂边际成本为 +10 H = +6.5 h/代（纠正检查单的 +3.3 h）。② **D-2**：难度**混合裁决**——默认局保持 hard × 6，M2 联调/冒烟用 medium × 6 bring-up（run 级 override，`configs/default.yaml` 默认值不动）；hard × 6 的 `fixed_prompt` 基线 win_rate=0.0% ⇒ M3 判据改用 `mean_stages_cleared`/`mean_battles_won`；medium × 6 的基线率与 `D` 列为待实测。③ **D-3**：`H=40`、`n_rep=1` 保持不变（改为 M3 前一次性确定性验证 + 既有的 `n_rep=3` 升级路径），`evolve.win_rate_tol` 0.0→**0.05**、`gap_threshold` 保持 0.15；并以精确配对 McNemar / 配对 bootstrap 的功效计算证明 **n=40 的 win_rate 只能判 ≥15pp**，4–5pp 量级增益改由 `mean_stages_cleared`（Δ=0.30 关 = 6 关制的 5.0pp，power 0.66–0.97）承担。同步改 `configs/default.yaml` 与 `dice_ratchet/infra/config.py` 的对应默认值 |
| v1.8 | 2026-10-07 | **M2 开工检查单 §3.1 实测回填与 C5 标定（裁决 A+C，登记 B-40/B-41；明细见 [§3.1 实测报告](reviews/2026-10-07-M2-S31-full-agent基线实测.md)）**。① **实测**：full `PolicyAgent` 在 H1 子集（seed 2000–2009，n=10，eval 模式，`runs/m2-s31-full-baseline/`）测得 `illegal_rate` **0.3016**、`fallback_rate` **0.2146**、`truncation_rate` **0.10**、`win_rate` 0.0（CI [0.000, 0.2775]）、`mean_stages_cleared` 3.00，`n=10 / degraded_n=0 / n_dropped=0`，墙钟 3.51 h——与 `fixed_prompt` 基线（0.3021 / 0.1810 / 0.0）同量级且 fallback/truncation 更差。② **裁定 A（先松开）**：`evolve.max_illegal_rate` 0.05→**0.35**、`max_fallback_rate` 0.10→**0.25**、`max_truncation_rate` 0.05→**0.15**（原门超限 6.03×/2.15×/2.00×，会 reject 每个候选 → M2 判据不可能达成）；副作用（C5 临时成为非约束门）已在正文写明。③ **裁定 C（M3 收紧）**：绝对门改**相对门** `candidate.<metric> ≤ parent.<metric> + tol`（登记的 B-41），`tol` 待 §3.2 冒烟产出配对后标定；禁止用「改回 0.05」收紧。④ **根因判定**：离线复现 2,220 个决策点的提示词组装——F13 压缩渲染 **0 次触发**（最大 `token_estimate` 2,694 < 3,072）、非法尝试 **65.2% 是 `play_face`** 配对问题、`grammar_used` **1,892/1,892 = false**（MLX 无语法约束）⇒ 0.30 属 8B 能力/遵从问题，不是渲染或契约缺陷；压到门内需语法约束解码（MOD-03）或提示词改造，**不作为 M2 前置**。⑤ §11 新增 11.1e，并给 P-25 行加「数值已被 B-40 取代」注记 |
| v1.9 | 2026-10-07 | **D-2 的 medium × 6 基线实测回填（§8.6 新增条目 + 基线表新增 medium × 6 行）**。① **实测**（`runs/m2-d2-medium6-h1n10/`，H1 子集 seed 2000–2009，n=10，同源三臂）：`random` 0/10；`heuristic` **7/10 = 70.0%**；`fixed_prompt` **0/10 = 0.0%**、`mean_stages_cleared` **2.50**、`mean_battles_won` 4.80、`illegal_rate` 0.2829、墙钟 **2.574 h**；内核侧 `medium × 6` 启发式 **91.0%**（env 步 249/301、决策点 92/103）。② **判定：medium × 6 未达成 D-2 的目的**——对 8B 的 `win_rate` 仍是地板（0.0%，与 hard × 6 相同），`mean_stages_cleared` 只 2.40→2.50，`mean_battles_won` 反而 6.10→4.80；检查单的检验条款只写了天花板侧，实际失败在地板侧。可用信号仅剩连续指标（D-3 已指定的轴）。③ **成本**：`D` 均值 83.7（hard 97.1）、单局 15.44 min（hard 19.6–19.9 min）⇒ 若 bring-up 用 medium × 6 则 `E_gen=114` ≈ **29.3 h/代**；**M3 主口径仍 hard × 6，37 h/代不变**。④ **登记两个缺陷**：**F26**（`baseline`/`play` 完全忽略 `env.episode.*`，已修 `cf934fc` + 回归用例）——第一次 medium 补测因此与 hard 逐位相同，本版数字是修复后重跑的；**F27**（逐臂归档 `baselines/*.json` 与 `registry.csv` 不记录 episode 口径，D-2 引入两个局型族后溯源只能靠 run-id 约定，待裁决是否补 `episode_config_hash`）。⑤ **D-2 再调待裁决**：medium × 10 对启发式仅 24.0%（比 hard × 6 更难，大概率加深地板）；备选是测 3 关局型（`hard × 3` 启发式 97.0%）或按其结论放弃 win_rate 轴 |
| v2.0 | 2026-10-09 | **实验臂/角色解耦 + DeepSeek API 后端回填，并落定 M2 闭环主力臂（登记 B-42/B-43/B-44、缺陷 F28）**。① **§4.7 新增三条签名**：组合根注入的 `RoleModelProvider.client_for(role)`、`inference.roles.resolve_role_bindings` / `role_model_ids` / `run_provenance`、`inference.secrets.lookup_api_key`。② **§8.6 新增「2026-10-09 臂与成本口径」条目**：六臂 `A0/A1/B0/B1/C0/C1` 的角色差分表与 `harness` 字段语义、角色绑定四级优先级（fail-closed）、`backend: deepseek` 的三条硬口径（`api_supports_seed=false`／`api_thinking=disabled`／key 只进内存）、`C0` 臂 H1 n=10 的实测四行（**medium×6 = 80%、hard×6 = 10%**；对照启发式 70% / 40%）、以及**按臂分列的成本表**。③ **B-43（N1 裁决）**：**M2 闭环主力臂 = `C1`**（三角色全 `deepseek-flash` + `harness: evolve`）——API 臂单局 2.09–2.52 min（本地 8B 19.6 min）⇒ `E_gen=114` ≈ **4.0–4.8 h/代 ≈ ¥84–100/代**；**成本约束由墙钟转为 API 花费/限流**，且 `inference.cache_prompt`（KV 前缀复用）对 API 臂**无收益**（只对本地臂有效）。上方「19.6 min/局 / 37 h/代」从此**只对本地 8B 成立**。④ **F28（新登记，处置待裁决）**：**C5 绝对门与臂耦合**——B-40 的三阈值（0.35/0.25/0.15）是按本地 8B 标定的，换到 `C1` 后 `hard×6` 的 `truncation_rate=0.30 > 0.15` 会 **reject 每一个候选**（`E_EVOLVE_ILLEGAL_RATE`，已用真实 `judge()` 复现），而 illegal/fallback 反向松了约 10×。即 §3.1 的「静默锁死闭环」风险**未被消除，只是从 illegal 轴搬到 truncation 轴**。⑤ **D-2 随之重开**：「medium × 6 对 M2 没有信号」只对 8B 成立；`C1` 臂上两难度都有方差。⑥ **F29（新登记）**：臂的 `harness: fixed/evolve` 字段**只做登记、无消费者**（`harness_mode()` 只进 provenance）⇒ **`C0` 与 `C1` 当前行为等价**；强制「fixed 臂不演化」属 Issue #1 任务 6（未做），引用臂名时不得据此推断 harness 被冻结 |
| v2.1 | 2026-10-09 | **处置 F28：C5 的 `truncation` 由绝对门改为相对门（登记 B-45；用户裁决 ②+③）**。① **③ 门结构**：**C5 = `illegal` / `fallback` 绝对门（0.35 / 0.25）+ `truncation` 相对门** `candidate.truncation_rate ≤ parent.truncation_rate + evolve.truncation_tol`，**`truncation_tol = 0.05`**（n=40 的 2 局，同 `win_rate_tol` 的依据 D-3）。**绝对门 `max_truncation_rate` 从 `configs/default.yaml` 与 `EvolveConfig` 中删除**（而非重标成 0.35——重标会在下次换臂时重演 F28）；理由：`truncation` 是**局型 × 策略强度**的合成属性（同 run 内 `heuristic` 0.00、DeepSeek hard×6 0.30、8B hard×6 0.10），不像 `illegal`/`fallback` 那样有跨臂的绝对意义。② **配套 fail-closed**：相对门依赖 `parent.truncation_rate`，故它进 **C0 前置完备性检查**——parent 缺该字段记 `C0:incomplete_report` 并 reject，**不静默跳门**（跳过会让 C5 对 truncation 失去诊断力）。③ **② 局型**：bring-up / §3.2 冒烟复核确认用 `medium × 6`（实测 truncation = 0.00、`win_rate` 有方差 80% vs 启发式 70%）；默认局仍 `hard × 6`。④ **已知放宽（诚实边界）**：相对门是**对称**放宽——parent 自身截断率很高时，候选只要不比它差就放行（如 parent = 0.90 时候选 0.60 也过）；本轮**不设绝对 backstop**，该行为已由参数化用例显式钉住，M3 标定 `tol` 时再评估。⑤ **落点**：`evolve/judge.py`（`_C5_TRUNCATION_TOL` + C0 parent 校验）、`infra/config.py` / `configs/default.yaml`（键替换）、`tests/unit/evolve/test_evolve_judge.py`（3 组新回归护栏 + 默认值护栏含 `not hasattr(max_truncation_rate)`）、MOD-07 PRD §8.2/§8.2.1/§8.2.2/§9 与 `docs/html/MOD-07-evolve.html`（v0.5） |
| v2.2 | 2026-10-09 | **处置 F30：MOD-06 的 `EpisodeRunner` 改为每局一个 `SandboxEnv`（登记 B-46；用户选方案 A）**。① **现场**：§3.2 冒烟（`C1` + `medium×6`）在 **5/40 局**被主动中止——gen-000 基线 `fallback_rate≈0.29` / `illegal_rate≈0.285`，而同臂/同模型/同难度/同 seed、**同一时间窗并发**的 `play` 路径是 `0.000`/`0.032`（⇒ 差在代码路径，不在模型）。**根因**：`evaluate_arm(n_workers=cfg.evals.n_workers=2)` 用两个线程驱动**同一个** `EpisodeRunner`，而它只持有一个 `SandboxEnv`（`close_env=False`）；`SandboxEnv` 承载全部局状态 ⇒ 两局 `reset`/`step` 交错、观测串局 ⇒ 模型按**别的局**的相位出动作（典型签名：战斗阶段出现 `map_choose`）⇒ 语义非法 ⇒ 重试耗尽 ⇒ fallback。② **修复**：`loop/cli.py` 由 `env=<共享实例>, close_env=False` 改为注入 `env_factory=_env_factory(cfg)`（每局 `episode_id=key`、局末 `close()`，与 MOD-08 `SandboxRunner` 同构）；构造代价实测 1.1 ms/局。③ **护栏**：`tests/integration/test_loop_env_isolation.py` 从组合根装配，断言 `runner.env is None` 且 `n_workers=1` 与 `=2` 逐字段一致（含 `seq_hash`）；`git stash` 掉修复后 **5/5 稳定失败**，签名即 F30。独立复现（H1 全 40 局、`heuristic` 臂）：共享 env **13/40 局有非法动作、0/40 胜**；每局新 env **0/40、18/40 = 45.0%**（对上 `greedy_counter` hard×6 45.5%）。④ **影响范围澄清**：只污染 **MOD-06 `loop run` 自己的评估口**；C0 臂既有实测走 `play-llm`（MOD-08 `SandboxRunner`，每局新 env、`n_workers=1`）⇒ **未被污染**，B-45 与 D-2 的证据基础成立。⑤ **门禁**：`scripts/ci_gate.sh` 全绿（G1–G5 + G3 跨 `PYTHONHASHSEED` 确定性），**1870 passed / 2 skipped**（较修前 1868 增 2 条新用例）。⑥ **残留 F31（登记，本轮未修）**：`loop/runner.py:446-448`/`:219` 局末 `state.counters.illegal_actions/llm_timeouts/io_retries += …` 是跨线程读改写，`n_workers=2` 下理论丢更新；这三个计数器在 `dice_ratchet/` 内**零读取点**，仅影响 `state.json` 上报值 |
| v2.3 | 2026-10-10 | **处置 F32–F41：闭环路径上被测试掩护的十个死缺陷（登记 B-47..B-51），并改写 §3.2 验收判据**。背景：§3.2 实跑（`C1`+`medium×6`）在修掉 F30 后仍未跑通，逐层暴露出 **十个只在「测试从不执行的参数组合/else 分支」里存在的缺陷**——前九个全被 1700+ 条全绿用例掩护。① **MOD-06**：`--resume` 落在「队列已空、周期边界未完成」时静默 `phase=done`（F34）——`_loop` 是 `while pending_queue:`，空队列不重入边界，而 `maybe_run_generation` 的 docstring 本就写了该语义；修法是在队列排空后补一次调用（`cycle_due` 的双重幂等键保证不重复触发）。② **MOD-07 提案输入**：prompt 只给 harness 文件的 sha256、**不给正文**（F38）⇒ 模型对没见过的文件写 unified diff ⇒ `E_EVOLVE_PATCH_INVALID`；改为内联可改文件正文（`HarnessSnapshot.contents`，schema 1.1.0）。③ **MOD-07 补丁应用**：只认 hunk 声明行号、无偏移搜索（F39）⇒ 上下文**逐字正确但行号偏 +9** 的真实候选被判无效；改为 ±200 行内逐字定位（容忍 offset、不做 fuzz、歧义即拒）。④ **MOD-07 提案预算**：沿用 `inference.max_tokens=256`（F37）⇒ 响应一律 `finish_reason=length` 截断、被报成 `E_LLM_SCHEMA_INVALID` ⇒ **0 候选**；实测提案需 ~500。改为**按调用方给预算**（`generate_json(..., max_tokens=)`，proposer 传 2048）；**明确否掉**「全局抬高」路线（2048 时 `mock` 4096 触发 71 例 `E_LLM_CONTEXT_OVERFLOW`、并吃掉 8B 余量）——纯代码路线还保住了 `config_hash`，已付的 ¥34.62 基线仍可复用。⑤ **MOD-07 提案 prompt 漏规则**（F40）：`evolvable_keys` 从未出现在 prompt 里（`config_patch` 只能盲猜 ⇒ `config_key_not_evolvable` 作废）；rationale 上限只写在 schema 的 `maxLength`（API 不强制）⇒ 实测 322>300 作废。改为 prompt 列出可改键 + 硬/建议上限，且上限取自候选 JSON Schema 的 `maxLength`（单一事实来源）。⑥ **跨模块语法（F41，本版最要紧的一条）**：`episode_key` 的 `arm` 位**不含 `-smoke`**，而 MOD-07 的冒烟臂正是 `parent-smoke`/`cand-{id}-smoke` ⇒ 冒烟局每行轨迹校验失败 → 该局 dropped（轨迹文件 **0 行**）→ `n_dropped/n=1.0` → `low_confidence` → `E_EVAL_INSUFFICIENT_N` → `smoke_fast_reject` ⇒ **每个候选都在冒烟关被淘汰，`harness/versions/` 永不可能有新版本**。已在**三处**同步（`infra/logging.py`、`schemas/trajectory_line.schema.json`、MOD-09 §6.2），并加一致性用例防漂移。⑦ **§3.2 判据改写**（B-51，用户裁决）：机制全修好后首个真实候选走到完整裁判链（`C0` n=40 ✅、`C1` win_rate 0.8000 vs 0.8000 ✅、`C2` `significant=False delta=0.0 p=1.0` ❌ ⇒ `E_EVOLVE_NO_GAIN`）——**候选被合法拒绝**。原判据硬要新版本，会把这一正确结论记成验收失败；改为「机制性失败为零 + 版本机制被真实使用 + gate 判定有信息量」。⑧ **方法**：本轮靠一条**近零成本干跑链**（`--agent heuristic --no-reflect` + 复制 harness + `EVOLVE__HITL=true` + 新 run-id ⇒ 84 局评估免费、只有几次 proposer 调用花钱、不提交版本不动 git）逐个逼出缺陷；单次 ≈40–60 s / ¥0.01–0.05，累计 11 次真实调用 **¥0.11**。它三次拦下本会静默烧钱的失败（F35/F37+F38/F41）。⑨ **门禁**：`scripts/ci_gate.sh` 全绿（G1–G5 + G3 跨 `PYTHONHASHSEED` 确定性），收集 1895 条；十条新护栏均先证明「修复前失败」。⑩ **未处置**：F31（计数器跨线程读改写）、F29（臂的 `harness` 字段无消费者）、F27（episode 口径溯源）、D-2 难度口径、§4.1 KV 复用 |
| v2.4 | 2026-10-10 | **§3.2 实跑合格（闭环首次真正跑通）+ F42–F46 处置（登记 B-52..B-55）**。① **实跑**：`--resume` 复用已付基线（¥34.62，因 F37 走纯代码路线而 `config_hash` 未变）【**⚠️ 2026-10-11 追注（D-M3-1 裁决 A 的连带项，用户裁决）：这笔 ¥34.62 基线与全部 22 个含 heldout/test 局的归档 run 一律记为「旧协议（protocol v2）历史参照」——不删、不重算、不参与 M3 判据。因为 F45-2 把 H1 从旧四位 `2000–2039` 移到六位 `731000–731039`，两者**交集为空**，该基线在 M3 协议下**不可复用**，M3 必须重新付一次 gen-000（≈¥70）。见 [M3 检查单 §3.8](tasks/2026-10-11-M3验收前置条件确认.md)】跑通 gen-1（3.75 h / ≈¥47.4）；闭环第一次产出真实数字——候选 heldout `n=37`、`win_rate 0.6757` vs parent `0.6250`（+5.07pp）、McNemar `p=0.7905` ⇒ **合法拒绝**（`C2:no_significant_gain`）。**这实证了 D-3**（n=40 只能判 ≥15pp），也证明 §3.2 判据改写为「机制性失败为零」是必要的。② **F43**：`eval report` 与 MOD-07 写**同一个** `generations/gen-NNN/report.json` ⇒ 判据②（csv）只能靠破坏判据①（判决）——改为 MOD-07 自追写 csv + `eval report` 对已裁决代写 sidecar + 台账按代 upsert 并保留判决。③ **F44/F45**：`audit leak` 把 MOD-06 的 51 行 pretty JSON 反思文件当 JSONL ⇒ `E_ENV_AUDIT_IO` ⇒ 退出码 2（判据③不可达）；同一形状问题还绕过了 `MEMORY_NON_CONTENT_KEYS` 豁免。按形状解析 + 文档投影修好，另补 `LOG_NON_CONTENT_KEYS`（`max_tokens` 等纯遥测键——正是 B-47 引入的合法值触发的假阳性）。修后真实 run：`verdict=clean exit=0`、0 finding。④ **F42（仅登记）**：MOD-07 每代评估**无检查点**，中断即整代重跑（本次差点白扔 ¥47.27；M3 每代 4–5 h 会放大）。⑤ **F46（新发现，待裁决）**：`load_episode_results` 的 `generation` 只贴标签不筛文件 ⇒ 台账每代数字是混代聚合（`n=117`）。⑥ **门禁**：`scripts/ci_gate.sh` 全绿；本轮 6 条新护栏（F43×3、F44/F45×3）**均先证明修复前失败**。 |
| v2.5 | 2026-10-10 | **处置 F47：把「演化前门禁」移到演化之前 + 失败路径自清 + 失败口径分离（登记 B-56..B-58；用户裁决 A+C+D，B 暂不动）**。① **现场（全是实测，不是推断）**：`FR-09-14` 与 `git_ops` 模块 docstring 都写「演化前门禁」，但**唯一调用点**在 `commit_harness()` 内部 = accept 第 3 步 —— 即 propose/冒烟/parent heldout/候选 heldout/裁判全部付完之后（本次实跑一代 = 3.75 h / ≈¥47）。`dice_ratchet/loop/*.py` 里**没有任何 git 门禁**，所以脏工作区拦不住起跑。② **后果比「白花钱」更糟**：`_finish_report` 的 `except InfraError` 把它记成 `decision="reject"`（与「候选不显著」同形），而 `build_decision_sections` 只看 gate 状态（此时 `approved`）⇒ **`report.json` 实际写 `decision="accept"`，磁盘上却没有新版本**（幻影 accept）。实测探针：`outcome.decision='reject'`、`error_code='E_EVOLVE_GIT_FAILED'`、`report.decision='accept'`、`versions=['v0']`。③ **A（B-56）**：MOD-06 在 `cmd_loop_run` 起跑处（`evolve_fn` 非空时）调 `preflight_worktree_clean()`，**早于 gen-000 基线**；只拦会落版本的 run（`--no-evolve`/`loop smoke` 不拦，否则本地带未提交改动连零成本诊断都做不了）；非 git 路径降级跳过。④ **C（B-57）**：`commit_harness` 改为**先门禁后 `git add`** 且失败时 `git reset -- harness`（实测旧顺序残留 `MM`/`A` 索引条目）；`accept()`/`rollback()` 失败时删掉**本次新建**的快照。**此处更正开发中的一处推断**：残留快照**不会**让后续 accept 抛 `VersionConflictError`（`next_version = max(指针, 现有目录)+1` 会跳号），真实危害是**顶掉一个版本号**并留下**可被 `load_version`/`rollback` 取用的幻影版本**（无账本、`git_commit` 空）——两条都有用例钉住。⑤ **D（B-58）**：基础设施失败记 `decision="error_reject"`（加入 MOD-08 `VERDICT_DECISIONS`），report/事件同口径；**§3.2 的机制性失败由枚举改为按前缀**（`E_EVOLVE_*`/`E_IO_GIT_*`/未捕获异常/非 0 退出）——枚举清单会继续漏（`E_EVOLVE_GIT_FAILED`/`E_EVOLVE_VERSION_CONFLICT` 就都不在原清单里，且退出码为 0）。⑥ **范围界定与代价**：门禁只在 CLI 边界生效，不进 `LoopOrchestrator.run()`（否则程序化嵌入、测试、本地诊断一律被拦）；为保持用例与宿主工作区无关，4 条集成/CLI 用例改用**仓库外的 harness 拷贝**（`--harness-dir`）。⑦ **护栏**：9 条新用例（A×3、C×4、D×2）**全部先 `git stash` 掉源码证明修复前失败**，且都带反例自检（旧顺序的索引残留、`build_decision_sections` 的 gate 分支、残留快照的跳号与幻影可加载性）。⑧ **门禁**：`scripts/ci_gate.sh` 全绿。 |
| v2.16 | 2026-10-11 | **处置 F50：`write_gen000_baseline` 漏传 `run_dir`（登记 B-73）**。① **现场**：MOD-06 这处 `evaluate_generation(0, parent, None, run_id=...)` 不给 `run_dir`；MOD-08 从 F48/F49 起在该路径落 `metrics/kind_distribution/gen-XXX.json` ⇒ 落到**真实** `runs/<run_id>/`（`run_dir=None` 回落硬编码 `Path("runs")`）。② **后果比「位置错」严重**：真实 run 目录一旦存在，F42-A 的臂归档随即生效 ⇒ **第二次评估复用归档、不执行任何局**，`episodes/heldout/` 为空。实测 `test_gen000_baseline_report_is_written` **首次通过、复跑必失败**，并污染工作区。③ **为什么没早抓到**：CI 每次干净签出 ⇒ 首次运行通过；本地复跑才暴露，容易被误读为 flaky。④ **处置**：传 `run_dir=paths.root`；该用例加**反向护墙**（断言真实 runs 下无 `kind_distribution`）⇒ 修复前的失败变**确定性**，且**不需要**给测试加环境变量绕过（真正的修法在生产代码）。⑤ **核对**：MOD-07 的两处调用点都已传 `run_dir`，只有 MOD-06 漏了。⑥ **门禁教训（写给下一个我）**：`./scripts/ci_gate.sh | tail` 会**吞掉退出码** —— 必须看脚本自己的 `✔/✘` 行或 `$?`，不能只看 tail 的最后一行。⑦ **成本**：¥0。 |
| v2.15 | 2026-10-11 | **处置 §3.3/§3.4 标定：修 `c3` 的三处缺陷（B-71）+ `tol` 的 MDE 换算（B-72）（用户裁决表 4 全选 A）**。① **`c3` 从未触发过**（实测 17 个归档代的 `jsd_kind` 全空），三个独立原因：生成侧**从不落 kind 分布**、`evaluate_generation` 的 **pair 错位**、`confirm_generations=2` 对 `c3` **结构上不可达**（两代数据下恒不触发，旧实现要三代连续）。② **处置**：新增 `metrics/kind_distribution/gen-XXX.json` + `write/read_kind_distribution`；新增 `adjacent_jsd_kind`/`aligned_jsd_kinds` 让两条路径用**等长对齐**的 jsd 与同一 pair 的 `win_rates`；**`c3` 改为单代即触发**（同 C4：直接证据不适用两点确认）；缺测记 `None` 而非 `0.0`。③ **`tol` 的 MDE 换算**（零成本）：`evals/tol_sensitivity.py` + `eval acceptance --sensitivity`——`illegal` ≈ 1,392 个步级事件、`fallback` ≈ 451 个、`truncation` ≈ **2 局** ⇒ **truncation 是约束轴**，三轴不可互换。④ **§4.6 命令面**新增 `--sensitivity`。⑤ **实测**：修复后两代数据下 `c3` 正确触发，三种反例（跳变不足/分布真变/jsd 缺测）均不误报；修复前两代下恒 `collapse=False`。护栏 8 条（kind 分布往返、对齐长度、缺测 None、`evaluate_generation` 落盘、c3 单代与三种反例、MDE 按轴分列、未知轴报错、实测分母）。⑥ **⚠️ 诚实边界**：**阈值本身仍未经基线标定**——`c1/c2/c4` 与 C5 的三个 `tol` 是「起点值 + 待回填」；`c2` 结构上需要 ≥2 代实测，而归档里只有 1 个 run 有 2 代。标定工具已就绪（`scripts/run_m3_calibration.sh --free` 零成本部分）。⑦ **成本**：¥0。 |
| v2.14 | 2026-10-11 | **处置 D-M3-5：彩排就绪 + `eval test --check` 预检 + 一次性资源代码断言（登记 B-70；用户裁决表 3 全选 A）**。① **test 不可逆风险**（实测）：`cmd_eval_test` 先跑 100 局（≈¥88）、**之后**才 `test_unlock`；而「单次」判据是 attestation 文件是否存在 ⇒ 「路径不可写/父目录不存在/已解锁过」会让 100 局白花且无存证 ⇒ 新增 `eval test --check`（只读、不消耗解锁、不调用 `evaluate_arm`、不写文件；不通过 → 退出码 1）。② **彩排范围**：`train` + **H3**（reserve，可重复），**2 代**，**不碰 H1/H2/test**。③ **合格判据代码化**：`evals/rehearsal.py`（6 项，含「5 张 PNG **有数据点**」与「一次性资源未被消耗」的 seed 级扫描）+ `rehearsal check` 子命令 + `scripts/run_m3_rehearsal.sh`（`--plan` 免费）。④ **「有数据点」为什么不能查文件**：空白图与有数据图的非白像素比例是 3.43% vs 3.60%，分不开；必须读 `Line2D.get_xydata()`。⑤ **§4.6 命令面**新增 `eval acceptance` 与 `rehearsal check`。⑥ **实测**：彩排判据在旧归档 run 上正确报出 5 项不合格（含 `plots/` 空、台账无 train 行、`comparable=0`、无臂归档，以及**该 run 用的是四位 seed `2000–2004`** —— 独立佐证 protocol v2）；17 条新用例；**修复前 `ModuleNotFoundError`**（模块不存在）。⑦ **⚠️ 未执行**：**彩排本身没跑**（≈¥40 需用户点头）⇒ 本版只把「可执行 + 判据已代码化」落地，**不得**读成「彩排已合格」。⑧ **成本**：¥0。 |
| v2.13 | 2026-10-11 | **处置 F-M3-9：判据 1 的判定器（登记 B-69；用户裁决表 2 全选 A）**。① **现场**：`monotonic_ok` 在 `dice_ratchet/`/`schemas/`/`scripts/` 上 **0 命中**（只在文档里出现 3 次）⇒ 判据 1 的通过条件写了两处、**判定器一处都没写**；M3 跑完 3 代无法机读结论。② **新增** `dice_ratchet/evals/acceptance.py`（`evaluate_acceptance` / `write_acceptance` / `AcceptanceReport` / `GenerationAcceptance`）+ `eval acceptance` 子命令 + `eval report` 的 ext 段 `acceptance`。③ **三处口径写清**（见 B-69）：`g−1` = 上一个已存在的 heldout 代（第一代不参与判定，但至少要有**一对可比**）；`P(gap>0)` = 相邻两代 heldout `mean_stages_cleared` 的配对 bootstrap（**只在共有 seed 上配对**，无共有 seed 记 `null` 而非 `0.0`）；接受记录以 `report.json` 的 `decision` 为准、指标以台账为准并按 `split="heldout"` 过滤。④ **校正原文一处同名异义**：文档的 `P(gap>0)` 与 `GainVerdict.win_rate_gap`（= `train − heldout`）同名但语义不同——`P(train > heldout)` 恰是判据 1 **不**想要的。⑤ **退出码**：`eval acceptance` **总是 0**（判定器 ≠ 守卫）。⑥ **实测**：归档 run 上 `monotonic_ok=false`（`comparable=0`）+ `accepts=0` ⇒ `ok=false`；12 条新用例；**修复前 `ModuleNotFoundError` + 生产代码 0 命中**。⑦ **诚实边界**：`P(gap>0)` 用的是**全代非基线臂**的逐局 `stages_cleared`（同 seed 多臂取 max），不绑定具体臂名——M3 正式实验里 H1 上只有 parent 与候选两套局，读者需知道这个口径。⑧ **成本**：¥0。 |
| v2.12 | 2026-10-11 | **处置 F48：绘图链路三重缺陷 + 台账零 train 行（登记 B-68；用户裁决表 1 全选 A）**。① **现场（实测，不是推断）**：(a) `plot_generations` 无生产调用点；(b) `plotting.py` 的四列名 `heldout_win_rate`/`heldout_ci_lo`/`heldout_ci_hi`/`train_win_rate` 在生产台账里**不存在**，而 `_g()` 缺列静默返回空 ⇒ 5 张 PNG「成功」但**打开是空白图**（各 20–27 KB，实测确认）；(c) 台账**零 train 行**（8 个归档 run 全为 0），且 `g6-eval-smoke` 同代同值 **5 行**。② **处置**：列清单**唯一化**（权威定义在 `infra.plotting`，MOD-08 引用——`infra → evals` 被 NFR-09-5 的分层门禁禁止，实测被 `E_IO_GUARD_LAYER` 拦下后改为合法方向）；台账增发 train 行（`n > 0` 判定）；按 `split` 取行 + 按代去重；**无数据点即抛 `E_IO_PLOT_NO_DATA`**；生产调用点落 `eval report` 末尾且**降级不静默**；upsert 改 `(generation, split)` 双键并折叠历史重复行；夹具改用权威列名。③ **B-06 的「列定义归 MOD-08」按此解释**：**口径**（哪个指标、什么语义、写哪些列）归 MOD-08；**字面清单的唯一副本**落 IO 层，因为它是 IO 工件的 schema，且分层门禁只允许这个方向。④ **实测证据**：真实台账上修复后 `win_rate.png` 带数据点（`0.650 / n=117`）；空指标列 ⇒ `E_IO_PLOT_NO_DATA`；`guards check` 0 findings。⑤ **未做/诚实边界**：本轮没有跑真实 `loop run` 产出 `runs/<id>/plots/`（端到端用例走的是`eval report` + 临时 run 目录）——**M3 彩排时必须实测真实 run 的 5 张图**（§3.2）。⑥ **成本**：¥0。 |
| v2.11 | 2026-10-11 | **登记 B-67（判据 1 没有判定器）**——D-M3-1 写回过程中的实测发现。MOD-08 §12 判定表第 1 行与本检查单 §1 要求 `monotonic_ok=true`，但 `grep -rn monotonic_ok` 在 `dice_ratchet/`/`schemas/`/`scripts/` 上 **0 命中**（仅在文档里出现 3 次）：`eval report` 只写逐代指标行，**不计算逐代单调性**。即 D-M3-1 裁决 A 定的是**口径**（轴 = `heldout.win_rate` + ≥1 次 accept），而**判定器本身尚未实现** ⇒ 若不补，M3 起跑后「判据 1 通过/不通过」无法由代码产出，只能靠人肉读 csv（那正是 F46-A 修掉的病：台账数字必须有物理含义，判据也必须有机读结论）。归属 M3 的 MOD-08 编码（排期 §4），**不阻塞第 0 步**。本版纯文档，零成本 |
| v2.10 | 2026-10-11 | **M3 的 HITL 模式与「样本资格」口径澄清（D-M3-4 裁决 A，登记 B-66）**（依据 [M3 验收前置条件确认](tasks/2026-10-11-M3验收前置条件确认.md) §2 D-M3-4）。① **矛盾现场**：MOD-07 §14 风险④ 写「`--hitl=false` 跑批量实验（标 `decided_by="auto"`，**不计入 M3 验收样本**）」，而 MOD-07 §12 又要求「≥1 次 accept」——两条无法同时满足。② **裁决 A**：M3 跑 **`--hitl=false` 全程自动**；**样本资格 = `decided_by="auto"` + `condition_trace` 完整**（C0–C5 六项齐全，每项带 `passed`/`value`/`threshold`）⇒ **计入**验收样本；缺 trace 的 auto 决策不计入；报告必须**显式记录「本轮无人工确认」**。③ **§14 风险④ 的限定收窄**为「只适用于 `condition_trace` 不完整的 auto 决策」。④ **依据**：M2 准出 run（`runs/m2-s32b-smoke-c1-medium6`）**事实就是走 auto 路径**（归档 `decided_by="auto"`、`candidates/{id}.json.verdict.condition_trace` 完整）⇒ 本裁决是**追认既有做法**；而选 `--hitl` 的代价不对称（一代白跑 ≈¥100 + 长跑必须守夜）。⑤ **⚠️ 诚实边界**：auto accept 的说服力弱于人工确认 ⇒ M3 报告**不得**把 auto 决策描述为「经人工复核」。⑥ **成本**：¥0（纯文档裁决）。 |
| v2.9 | 2026-10-11 | **G3 按臂分层执行并接上生产调用点（D-M3-3 裁决 A，登记 B-65）**（依据 [M3 验收前置条件确认](tasks/2026-10-11-M3验收前置条件确认.md) §2 D-M3-3）。① **现场（两个独立事实，都是实测）**：(a) `check_report_determinism` 从 M1 起**没有任何生产调用点**（`grep -rn check_report_determinism dice_ratchet/` 只命中它自己的定义与 `__all__`）⇒「同参数重跑 `report.sha256` 必须一致，否则抛 `E_EVAL_NONDETERMINISTIC`」这条承诺**在真实 run 里从未被执行过**；(b) 主力臂 `C1` 是 API 臂（`api_supports_seed=false`，实测字节相同的请求 9 次返回 9 个不同响应）⇒**端到端逐字节可复现在物理上不成立**。② **裁决**：承诺**精确化到能兑现的层级**——**可复现层**（代码/配置/seed/聚合口径/并发归一化）本地臂**硬执行**；**LLM 层**不承诺逐字节（采样照记 + 响应按 request hash 归档 + 变化标 `llm.nondeterministic`）；**臂内含任何 `provider=api` 的角色** ⇒ 跳过硬门 + 发新增事件 `eval.nondeterministic{generation,determinism,reason,repro_scope}`，**不抛异常**（否则第一个 API run 必然假报，一代 ≈4.8 h/≈¥100 白跑）。③ **执行点**：`evals/harness.py::enforce_report_determinism`，生产调用点 = `evolve/generation.py` **写报告之前**；判定函数 `reproducibility_of(app_cfg)` **fail-closed 方向**为「不可复现」（代价不对称：误判为可复现 ⇒ 假报 `E_EVAL_NONDETERMINISTIC`；误判为不可复现 ⇒ 只少执行一次检查）。④ **§8.1 新增按层承诺**；**§8.6 D-3 的「H1 前 5 seed 重跑对 sha256」标为在主力臂上不成立**（`n_rep` 仍为 1）。⑤ **护栏**：10 条新用例，**修复前 `ImportError`**（`enforce_report_determinism` 不存在）+ 反事实脚本实测旧硬门在 API 臂漂移上确实抛 `E_EVAL_NONDETERMINISTIC`；含「生产调用点必须存在且早于写报告」这条元护栏（上一版的病正是「函数存在但无调用点」）。⑥ **诚实边界**：这**降低**了「可复现」的强度，M3 报告必须显式声明「本轮无逐字节复现证据」；**不得**宣称「API 臂结果可逐字节复现」。⑦ **成本**：¥0。 |
| v2.8 | 2026-10-11 | **C5 收紧为三轴同构的「相对门 + 绝对 backstop」（D-M3-2 裁决 E，登记 B-64）**（依据 [M3 验收前置条件确认](tasks/2026-10-11-M3验收前置条件确认.md) §2 D-M3-2）。① **现场（实测）**：`illegal`/`fallback` 原为绝对门（0.35 / 0.25），对 `C1` 实测值（0.0318 / 0.0017）松 **11× / 147×** ⇒ 等于**非约束门**。后果不是「少抓一个坏候选」，而是**判据 2 的归因结论不成立**（候选可靠推高非法率换 `win_rate` 而照样过闸）。② **门结构**：三轴统一 `candidate ≤ min(parent + tol, backstop)`；新增 `evolve.illegal_tol=0.05` / `evolve.fallback_tol=0.05` / `evolve.truncation_backstop=0.60`；`max_illegal_rate`/`max_fallback_rate` **保留但语义降为 backstop**（键名不变 ⇒ 老配置仍可加载）。③ **堵住 B-44 的已知放宽**：原设计刻意不给 truncation 设绝对 backstop，代价是 parent=0.90 时候选 0.60 也能过；0.60 把它钉住（= `C1` hard×6 实测 0.30 的 2× 余量）。④ **`tol` 语义按轴分列、不可互换**：illegal/fallback 是**步级**（se≈0.0017 / 0.0030）⇒ 0.05 远大于批内噪声（防真退化，不承担检微小漂移）；truncation 是**局级**（1/40 = 0.025）⇒ 0.05 恰为 2 局噪声。⑤ **fail-closed 扩展**：三轴都是相对门 ⇒ parent 的 `illegal_rate`/`fallback_rate` 进 **C0 前置完备性检查**，缺则 `C0:incomplete_report`，不静默跳门。⑥ **可审计性**：C5 的 `condition_trace` 带 `value`（候选值 + 3 个 parent 值）与 `threshold`（3 个生效门限 + 3 个 `tol` + 3 个 backstop），随 `candidates/{id}.json.verdict` 落盘。⑦ **护栏**：12 条新/改用例全部**先 `git stash` 掉源码证明修复前失败**（实测 **11 failed / 68 passed**），且带反例自检（`0.30 ≤ 0.35` 必落在旧绝对门内、`0.601 ≤ 0.90+0.05` 必落在无 backstop 的相对门内）；快速门禁 **1959 passed / 2 skipped**。⑧ **诚实边界**：`tol=0.05` 是**起点值，不是 §3.2 实测标定值**（标定未做）；标定后只改数值，不改门结构。⑨ **成本**：¥0。 |
| v2.7 | 2026-10-11 | **M3 主口径落定（D-M3-1，用户裁决 A）：`hard × 6` 为唯一主口径、`win_rate` 保持第一判定量**（依据 [M3 验收前置条件确认](tasks/2026-10-11-M3验收前置条件确认.md) §2 D-M3-1）。① **局型**：`env.episode.difficulty: hard` **不动**——`difficulty` 参与 `config_hash`（hard `141bef5f…` / medium `2dc294eb…`），改它即换 run-id 族并作废与 §8.6 历史参考值（启发式 45.5%、`D` p50=96）的可比性。② **`medium × 6` 降为外部效度报告项**，不进判据 1/2/3 的判定路径；依据是同一批 seed 上 `C1` 实测 hard×6 `win_rate=10%` / medium×6 `=80%`——medium 的 80% 已贴 6 关制天花板，「单调不降」无上升空间。③ **判据 1 的轴 = `heldout.win_rate`**（`ci_lo(g) ≥ win_rate(g−1) − 0.02` 或 `P(gap>0) ≥ 0.90`，且 ≥1 次 accept）；**判据 2 的「4–5pp 量级」仍由 `mean_stages_cleared` 的配对 bootstrap 承担**（D-3 结论不变），`mean_stages_cleared`/`mean_battles_won` 作并列报告证据。④ **本版同时纠正三处互相矛盾的旧文案**：MOD-08 §8.2「`win_rate` 第一主指标」、MOD-08 §12 判定表第 1 行（漏写「≥1 次 accept」）、排期 §5 的 M3 行——**统一到本裁决**。⑤ **诚实边界（写进 §8.6，不得在 M3 报告里隐去）**：`C1` 在 hard×6 上仅 10% ⇒ 判据 1 由 1–2 局抖动主导，通过只等于「无显著退步」，**提升结论必须由判据 2 的连续指标给**；`fixed_prompt` 基线 0% ⇒ 判据 2 的基线对照以 `mean_stages_cleared` 为主；`truncation=0.30` 持续压低有效样本；**hard×6 的三基线从未在新六位 H1（731000–731039）上测过** ⇒ 判据 2 在新协议下尚无实测锚点，起跑前必须补（付费，需用户点头）。⑥ **成本**：0（纯文档裁决）。⑦ **不重排日历日期**。 |
| v2.6 | 2026-10-11 | **处置 F46-A / F42-A+B+C / F45-2（登记 B-59..B-62；用户裁决 F46=A、F42=A+B+C、F45=2）**。① **F46-A（B-59）**：`load_episode_results` 的 `generation` 原先只贴标签、且**覆盖行里的值** ⇒ `eval report --generation 1` 把 `g000-parent(40)+g001-parent(40)+g001-cand(37)` 聚成一个 `n=117 / 0.6496`（台账 gen-1 行就是它）；同代还跨臂混算（`n=77`）。改为**真过滤**（行里的 generation 优先、缺失按文件名解析、判不出即跳过并发可观测事件），**不再覆盖** `EpisodeResult.generation`；`eval report` 新增 `--arm`（缺省=该代候选臂、`all`=显式混合池），坍缩判定改用整池。对拍实测：候选臂 `n=37 / 0.6757`、parent 臂 `n=40 / 0.6250`，与 MOD-07 报告逐项一致；归档 `report.json`/`report.sha256` 重算前后逐字节未变（走 sidecar，F43 规则保持）。② **F42-A+B+C（B-60/B-61）**：现场是「引擎早有 `resume_results`/`on_result`，但 `evaluate_arm` 不转发」（修复前签名 `TypeError: evaluate_arm() got an unexpected keyword argument 'resume_results'`），且 MOD-07 的 resume 单元只有整代 ⇒ 一代 84 局（≈3.75 h / ≈¥47）中断即再付一次。补上按臂归档 `metrics/arms/`（provenance + 逐局明细）与 fail-closed 复用规则（全等且覆盖完整→整臂 0 局 + `evals.arm_reused`；部分→只跑缺的；任一不一致→重算），以及**提案检查点** `generations/gen-NNN/proposals/proposal.json`（评估前落盘 + 输入指纹；resume 命中即同 candidate id、**0 次 proposer 调用**）。顺带修 `run_eval` 的「已完成 seed 仍被排进队列重跑」与 `str(None)=="None"` 让归档复用闸永远对不上的两处真缺陷；`CandidateEvaluator._evaluate_arm` 原先不传 `n_workers`（84 局串行，而 gen-000 基线传 2）也一并按配置对齐。端到端用例：一代在 parent heldout 第 1 局后 Ctrl-C → resume → proposer **0 次调用**、parent heldout 已归档的局**一局都没重跑**、该代跑完。③ **F45-2（B-62，协议级）**：heldout/test 从四位（2000–2099 / 9000–9099）移到六位端口（731000–731099 / 947000–947099）。**为什么**：旧四位与耗时（`duration_us=2072.8`）、token 预算（`max_tokens=2048`）、ISO 年份（`2026`）、秒级浮点（`dur_s=3.9022`）共用一个数字空间 ⇒「数字串相等即泄漏」不健全，只能靠两张人工死名单豁免（扩过四次；实测 12 个键撞上、**118,646 命中**，其中 `ts` 占 94%）。**实测零碰撞**：全量归档 **1311 个文件 / 188.0 MB**，新区间 **0 命中**、旧区间（反例自检）**799,868 命中**。**代价**：`config_hash` 不含 seeds ⇒ 必须**另立**代际隔离闸（`baseline_protocol()` 追加 `+seeds-<hash12>` + ext provenance 增记 seeds 协议标签），否则新旧局静默混算；已归档的 protocol v2 证据（22 个含 heldout/test 局的 run）与 v3 **不可比**——不删数据、不擅自重跑。死名单整删后扫描面**更严**；唯一保留的豁免是框架自己的播种计划三键（`{seed, episode_key, spec}`）。**INV-5 措辞不需要改**（这是选 2 而非「放宽数字串规则」的直接收益，已写进 §11.1l/§4.6）。④ **护栏**：三条任务共 30+ 条新用例，**全部先 `git stash` 掉源码证明修复前失败**，且都带反例自检（同 fixture 下无过滤必须能拿到跨代跨臂全部文件；归档被篡改 9 个 provenance 字段逐一不复用；同 seed 进内容字段必判泄漏、进 `spec.seeds` 不判）。⑤ **门禁**：`scripts/ci_gate.sh` 全绿（G1–G5 + G3 跨 `PYTHONHASHSEED` 确定性），**1925 passed**。⑥ **成本**：0（三项任务全部未做付费跑批）。⑦ **未做/诚实边界**：未做干跑链的真实端到端跑批（端到端用例用注入替身）、未实测 API 臂并发提速、提案指纹不含模型权重、`train` split 同代仍混臂（heldout 是验收判据来源，优先修） |
