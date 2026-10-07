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
| 4 | [项目排期-GameRSI.md](项目排期-GameRSI.md) | 什么时候做完（甘特图、关键路径、风险）；模块依赖图见 [assets/module-deps.svg](assets/module-deps.svg) |

> **游戏本体与模拟器的关系**：先用 [GAME-00](prd/GAME-00-webgame-PRD.md) 做出**人类可玩的网页版游戏**；
> 游戏跑通后，按同一套规则实现**纯 Python 模拟器**（MOD-01）供 RSI 训练；未来可接视觉模型直接对游戏本体训练。
> GAME 系列是**游戏规则**的唯一事实来源，MOD-01 是该规则的 Python 等价移植（对拍契约见 [GAME-01](prd/GAME-01-kernel-contract.md) §7）。

**冲突处理**：PRD 与 Spec 冲突时以 Spec 为准；Spec 未覆盖处由 PRD 决策并以「决策（本 PRD 提出，待回填 Spec）」标注，汇总登记在 [模块拆分Spec-GameRSI.md](模块拆分Spec-GameRSI.md) **§11 待回填决策登记表**（v1.4：36 项已回填、0 项待裁决）。

> 🔎 **评审入口**：Spec §11。v1.3（2026-09-30 17:21）已按 [MOD PRD 评审](reviews/2026-09-30-MOD-PRD评审.md) 修订；v1.4（2026-09-30 17:49）已确认全部裁决，§11.2 待裁决清零。新的临时决策从 P-31 起登记。跨模块签名以 Spec §4.7 为唯一登记处，「一局」的定义与成本模型见 Spec §8.6。
>
> **当前版本**：Spec **v1.8**（2026-10-07，§8.6 回填 M2 §3.1 的 full `PolicyAgent` 实测并按裁决 A+C 标定 C5：**先松开到 0.35/0.25/0.15**，M3 由相对门 `candidate ≤ parent + tol` 收紧，登记 B-40/B-41；v1.6 为 n=10 成本模型回填、v1.7 为 M2 开工 D-1/D-2/D-3）；M1 已验收通过（**含口径限定**：判据 2 的工件已用当前快照重跑刷新，MOD-04 §12 的 `illegal_rate`/`fallback_rate` 归 M3 判定），见 [M1 验收报告](reviews/2026-10-04-M1验收报告.md) 与独立的 [M1 验收复核](reviews/2026-10-05-M1验收复核.md)。

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
| GAME-00 | game-web（网页版骰子 roguelike 本体，已实现于 `src/game`） | 游戏 | G0 | [PRD](prd/GAME-00-webgame-PRD.md) v1.2 |
| GAME-01 | 规则内核契约（确定性 / API / 对拍） | 游戏 | G0 | [契约](prd/GAME-01-kernel-contract.md) v1.2 |
| GAME-02 | 配表规格与内容数据 | 游戏 | G0 | [配表](prd/GAME-02-content-tables.md) |

### RSI 训练链路（MOD 系列）

| ID | 模块 | 层 | 里程碑 | PRD（当前版本） |
|---|---|---|---|---|
| MOD-01 | env-rules（GAME 内核的 Python 逐位移植） | L0 | M0 | [PRD](prd/MOD-01-env-rules-PRD.md) v1.0 |
| MOD-02 | env-sandbox（投影/隔离/审计/蜜罐） | L0 | M0 | [PRD](prd/MOD-02-env-sandbox-PRD.md) v1.2.1 |
| MOD-03 | inference（本地模型推理） | L1 | M1 | [PRD](prd/MOD-03-inference-PRD.md) v1.5 |
| MOD-04 | policy（prompt/解析/工具契约） | L1 | M1 | [PRD](prd/MOD-04-policy-PRD.md) v0.6 |
| MOD-05 | memory（策略记忆与反思库） | L1 | M2 | [PRD](prd/MOD-05-memory-PRD.md) v1.2 |
| MOD-06 | loop（闭环编排） | L2 | M2 | [PRD](prd/MOD-06-loop-PRD.md) v1.2 |
| MOD-07 | evolve（harness 自进化） | L2 | M2–M3 | [PRD](prd/MOD-07-evolve-PRD.md) v0.4 ✅ 代码落地（[README](../gamer_rsi/evolve/README.md)）；C5 标定见 [2026-10-07 §3.1 实测](reviews/2026-10-07-M2-S31-full-agent基线实测.md) |
| MOD-08 | evals（评估与测量） | L3 | M1–M3 | [PRD](prd/MOD-08-evals-PRD.md) v0.6 |
| MOD-09 | infra（配置/日志/CLI/守卫） | L3 | M0–M3 | [PRD](prd/MOD-09-infra-PRD.md) v0.3.1 |

## 关键路径（开工顺序）

```
MOD-09 骨架 ─┐
MOD-01 规则 ─→ MOD-02 沙箱 ─→ MOD-03/04 推理+策略 ─→ MOD-06 编排 ─→ MOD-07 进化 ─→ M3 验收
                 MOD-08 评估（可与 MOD-04/05 并行）
```

## 七条不变量（任何人任何 PR 都不得违反）

INV-1 agent 不得触达 Env 内部 · INV-2 观测必须白名单投影 · INV-3 prompt 只含四类白名单上下文 ·
INV-4 harness 代码受限执行 · INV-5 train/heldout/test 三方隔离 · INV-6 随机性只来自显式 seed ·
INV-7 主指标只用环境原生结果（outcome 为第一主指标，stages_cleared/hearts_left/battles_won 为次指标；shaped reward 不得用于结论）。

详见 [模块拆分Spec-GameRSI.md](模块拆分Spec-GameRSI.md) §1。
