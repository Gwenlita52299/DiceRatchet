# GameRSI 文档中心

> 本目录承载 GameRSI 项目的**规格层文档**：从项目计划 → 模块拆分 Spec → 各模块技术 PRD。

## 阅读顺序

| 顺序 | 文档 | 作用 |
|---|---|---|
| 0 | [RSI调研纪要.md](../RSI调研纪要.md) | 为什么做（理论/实证/失败模式/治理） |
| 1 | [项目计划-GameRSI.md](../项目计划-GameRSI.md) | 做什么（目标、沙箱纪律、里程碑、验收） |
| 2 | [模块拆分Spec-GameRSI.md](模块拆分Spec-GameRSI.md) | **切成几块、怎么接起来**——唯一事实来源 |
| 2.5 | `prd/GAME-*.md` | **游戏本体怎么做**（先做出可玩的网页游戏，再据此写模拟器） |
| 3 | `prd/MOD-*.md` | 每块怎么实现（本文档集） |

> **游戏本体与模拟器的关系**：先用 [GAME-00](prd/GAME-00-webgame-PRD.md) 做出**人类可玩的网页版游戏**；
> 游戏跑通后，按同一套规则实现**纯 Python 模拟器**（MOD-01）供 RSI 训练；未来可接视觉模型直接对游戏本体训练。
> GAME 系列是**游戏规则**的唯一事实来源，MOD-01 是该规则的 Python 等价移植（对拍契约见 [GAME-01](prd/GAME-01-kernel-contract.md) §7）。

**冲突处理**：PRD 与 Spec 冲突时以 Spec 为准；Spec 未覆盖处由 PRD 决策并以「决策（本 PRD 提出，待回填 Spec）」标注，汇总登记在 [模块拆分Spec-GameRSI.md](模块拆分Spec-GameRSI.md) **§11 待回填决策登记表**（当前 8 项已回填、27 项待裁决）。

> 🔎 **评审入口**：开工前只需看 Spec §11。其中涉及 INV-1/INV-2/INV-5 的 6 条（P-01/02/06/07/08/14）建议在 M0–M1 评审时先行裁决。

## 模块地图

```
L3 测量与工程  MOD-08 evals        MOD-09 infra
L2 进化        MOD-06 loop         MOD-07 evolve
L1 智能体      MOD-03 inference    MOD-04 policy    MOD-05 memory
L0 环境        MOD-01 env-rules    MOD-02 env-sandbox
（预留）       MOD-10 distill（M4 可选：STaR + LoRA）
```

## 技术 PRD 索引

### 游戏本体（GAME 系列，先行）

| ID | 模块 | 层 | 里程碑 | PRD |
|---|---|---|---|---|
| GAME-00 | game-web（网页版骰子 roguelike 本体） | 游戏 | G0 | [PRD](prd/GAME-00-webgame-PRD.md) |
| GAME-01 | 规则内核契约（确定性 / API / 对拍） | 游戏 | G0 | [契约](prd/GAME-01-kernel-contract.md) |
| GAME-02 | 配表规格与内容数据 | 游戏 | G0 | [配表](prd/GAME-02-content-tables.md) |

### RSI 训练链路（MOD 系列）

| ID | 模块 | 层 | 里程碑 | PRD |
|---|---|---|---|---|
| MOD-01 | env-rules（游戏规则引擎 / Python 模拟器） | L0 | M0 | [PRD](prd/MOD-01-env-rules-PRD.md) |
| MOD-02 | env-sandbox（投影/隔离/审计/蜜罐） | L0 | M0 | [PRD](prd/MOD-02-env-sandbox-PRD.md) |
| MOD-03 | inference（本地模型推理） | L1 | M1 | [PRD](prd/MOD-03-inference-PRD.md) |
| MOD-04 | policy（prompt/解析/工具契约） | L1 | M1 | [PRD](prd/MOD-04-policy-PRD.md) |
| MOD-05 | memory（策略记忆与反思库） | L1 | M2 | [PRD](prd/MOD-05-memory-PRD.md) |
| MOD-06 | loop（闭环编排） | L2 | M2 | [PRD](prd/MOD-06-loop-PRD.md) |
| MOD-07 | evolve（harness 自进化） | L2 | M2–M3 | [PRD](prd/MOD-07-evolve-PRD.md) |
| MOD-08 | evals（评估与测量） | L3 | M1–M3 | [PRD](prd/MOD-08-evals-PRD.md) |
| MOD-09 | infra（配置/日志/CLI/守卫） | L3 | M0–M3 | [PRD](prd/MOD-09-infra-PRD.md) |

## 关键路径（开工顺序）

```
MOD-09 骨架 ─┐
MOD-01 规则 ─→ MOD-02 沙箱 ─→ MOD-03/04 推理+策略 ─→ MOD-06 编排 ─→ MOD-07 进化 ─→ M3 验收
                 MOD-08 评估（可与 MOD-04/05 并行）
```

## 七条不变量（任何人任何 PR 都不得违反）

INV-1 agent 不得触达 Env 内部 · INV-2 观测必须白名单投影 · INV-3 prompt 只含四类白名单上下文 ·
INV-4 harness 代码受限执行 · INV-5 train/heldout/test 三方隔离 · INV-6 随机性只来自显式 seed ·
INV-7 主指标只用环境原生结果（shaped reward 不得用于结论）。

详见 [模块拆分Spec-GameRSI.md](模块拆分Spec-GameRSI.md) §1。
