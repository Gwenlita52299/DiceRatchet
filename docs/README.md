# DiceRatchet 文档中心

> 本目录承载 DiceRatchet 项目的**规格层文档**：从项目计划 → 模块拆分 Spec → 各模块技术 PRD。

## 阅读顺序

| 顺序 | 文档 | 作用 |
|---|---|---|
| 0 | [RSI调研纪要.md](../RSI调研纪要.md) | 为什么做（理论/实证/失败模式/治理） |
| 1 | [项目计划-DiceRatchet.md](../项目计划-DiceRatchet.md) | 做什么（目标、沙箱纪律、里程碑、验收） |
| 2 | [模块拆分Spec-DiceRatchet.md](模块拆分Spec-DiceRatchet.md) | **切成几块、怎么接起来**——唯一事实来源 |
| 2.5 | `prd/GAME-*.md` | **游戏本体怎么做**（先做出可玩的网页游戏，再据此写模拟器） |
| 3 | `prd/MOD-*.md` | 每块怎么实现（本文档集） |
| 4 | [项目排期-DiceRatchet.md](项目排期-DiceRatchet.md) | 什么时候做完（甘特图、关键路径、风险）；模块依赖图见 [assets/module-deps.svg](assets/module-deps.svg) |

> **游戏本体与模拟器的关系**：先用 [GAME-00](prd/GAME-00-webgame-PRD.md) 做出**人类可玩的网页版游戏**；
> 游戏跑通后，按同一套规则实现**纯 Python 模拟器**（MOD-01）供 RSI 训练；未来可接视觉模型直接对游戏本体训练。
> GAME 系列是**游戏规则**的唯一事实来源，MOD-01 是该规则的 Python 等价移植（对拍契约见 [GAME-01](prd/GAME-01-kernel-contract.md) §7）。

**冲突处理**：PRD 与 Spec 冲突时以 Spec 为准；Spec 未覆盖处由 PRD 决策并以「决策（本 PRD 提出，待回填 Spec）」标注，汇总登记在 [模块拆分Spec-DiceRatchet.md](模块拆分Spec-DiceRatchet.md) **§11 待回填决策登记表**（v1.4：36 项已回填、0 项待裁决）。

> 🔎 **评审入口**：Spec §11。v1.3（2026-09-30 17:21）已按 [MOD PRD 评审](reviews/2026-09-30-MOD-PRD评审.md) 修订；v1.4（2026-09-30 17:49）已确认全部裁决，§11.2 待裁决清零。新的临时决策从 P-31 起登记。跨模块签名以 Spec §4.7 为唯一登记处，「一局」的定义与成本模型见 Spec §8.6。
>
> **当前版本**：Spec **v2.35**（2026-10-17，**文档与命令面对齐现状（纯文档 + 两处代码登记，¥0）**：① 新增 **`loop baseline`**（只测一个给定 harness 的 held-out，不训练/不演化/不提交 git；幂等键=run 目录）与 **`loop run --allow-ungated-start`**；② 新增错误码 **`E_EVOLVE_UNGATED_START`**（退出码 2）与**起点守卫**：`loop run` 起跑顺序 = git 干净检查 → 起点守卫 → gen-000 基线，起点 `play_hash` 必须 ∈ {`harness/BASELINE.json` 的原始 v0, `harness/versions/v*/` 任一已接受快照}；③ §4.6 更正 `eval report --arm` 缺省口径为 **parent**（F57 起）；④ 登记 **B-95**（协议口径纠正：P1 判负、下一轮测 `v0+P2`）与 **B-96**（全库 PRD 现状断言对齐，含 8 个未登记事件与 `error_reject` schema 枚举两处代码修复）。见 [Spec §11.1m](模块拆分Spec-DiceRatchet.md)）
>
> **历史版本索引**（只列关键节点，完整变更见 [Spec §12](模块拆分Spec-DiceRatchet.md)）：v2.34（F80/F81 种子/模型身份进对拍）· v2.33（F52/F63 `tol` 分母 + 测量身份）· v2.27（全库文档一致性修订）· v2.26（取消 C2 统计门）· v2.24（D-M3-7 起点继承按 play 面）· v2.6（F45-2 六位 seed 端口空间 + F42 检查点）· v2.5（F47 起跑前 git 门禁）· v2.0（三角色解耦 + DeepSeek 后端）。
> 当前在役 harness 为 **version 4**（裸 v0 + prompt 前缀缓存重排；2026-10-18 登记；A/B 7/40 vs 7/40、McNemar p=1.0；唯一缓存 10.8%→40.1%；40 局 `cost_estimate` ≈¥33→¥19.74）。v4 **不含** v3 的 P1/P2 规则（实测候选为裸 v0+cache，`play_hash=1ebe12e1`）；`v0+P2` 的 40 局 held-out 测量仍待单独裁决。见 [2026-10-17 API 缓存命中修复](reviews/2026-10-17-API缓存命中修复.md) §3.2。

## 可视化讲解（docs/html）

按当前代码重画的**理解路径**（不是 PRD 镜像）：总览 + GAME + 九个模块各一页，共享样式、纯静态、可离线打开。

入口：[docs/html/index.html](html/index.html) · 说明见 [docs/html/README.md](html/README.md) · 本轮审计记录见 [2026-10-17-PRD现状审计与html讲解.md](reviews/2026-10-17-PRD现状审计与html讲解.md)

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
| GAME-00 | game-web（网页版骰子 roguelike 本体，已实现于 `src/game`） | 游戏 | G0 | [PRD](prd/GAME-00-webgame-PRD.md) v1.3（技能骰 17 / 普通怪 15 已与配表对齐） |
| GAME-01 | 规则内核契约（确定性 / API / 对拍） | 游戏 | G0 | [契约](prd/GAME-01-kernel-contract.md) v1.2 |
| GAME-02 | 配表规格与内容数据 | 游戏 | G0 | [配表](prd/GAME-02-content-tables.md) v1.2 |

### RSI 训练链路（MOD 系列）

| ID | 模块 | 层 | 里程碑 | PRD（当前版本） |
|---|---|---|---|---|
| MOD-01 | env-rules（GAME 内核的 Python 逐位移植） | L0 | M0 | [PRD](prd/MOD-01-env-rules-PRD.md) v1.3（2026-10-17 与代码对齐；§9 三个配置键删除：**配表目录的权威是 `loader.GAME_CONTENT_DIR`**（import 时解析，从不读 yaml），审计改用 run 快照的 `content_hash` 反查真实配表；「每次 reduce 后校验不变式」生产局从未执行（2026-10-14 F77）；**逐步校验已真正接上**：MOD-02 `SandboxEnv.step` 提交前跑 `assert_invariants`，违反 ⇒ 该步不提交 + `E_ENV_INTERNAL`/退出码 4（训练路径同类累计 ≥3 中止 run；MOD-08 批量测量记 dropped），实测 ≈137.5 µs/步——2026-10-15 F78） |
| MOD-02 | env-sandbox（投影/隔离/审计/蜜罐） | L0 | M0 | [PRD](prd/MOD-02-env-sandbox-PRD.md) v1.2.7（2026-10-17 与代码对齐；审计的触发方式 = **显式 CLI**；`sandbox.audit.*` 收敛为**规则集版本钉扎** `ruleset_pin`，不符即 fail-closed `E_ENV_AUDIT_RULESET`/退出码 4——2026-10-14 F73/F74；**`env.sandbox.*` 15 键砍到 2 键**，删掉的 13 个里四个是「不变式开关」，配置面只留真的会被读的键——同日 F75；`audit leak` 改为**按 `content_hash` 反查真实配表**，对不上即判 critical 并降级 `future_room`/`shop` 规则集——同日 F77-a；**FR-02-8d**：每个合法动作提交前跑 MOD-01 `assert_invariants`（内核那次被游戏侧 `debug.showHiddenInfo` 门着、生产局为零），违反 ⇒ 该步不提交 + `E_ENV_INTERNAL`/退出码 4（处置矩阵见 FR-02-8d）——2026-10-15 F78） |
| MOD-03 | inference（模型推理：本地 MLX/llama.cpp + DeepSeek API，三角色装配） | L1 | M1 | [PRD](prd/MOD-03-inference-PRD.md) v1.9（2026-10-17 与代码对齐：补 `deepseek` 后端 / `E_LLM_NONDETERMINISTIC`，更正 perf_budget / 落盘工件 / 测试文件名） |
| MOD-04 | policy（prompt/解析/工具契约） | L1 | M1 | [PRD](prd/MOD-04-policy-PRD.md) v0.7（2026-10-17 与代码对齐：轨迹字段 / 事件名 / 环境变量 / 判负归属 / `tool_timeout_ms=50`） |
| MOD-05 | memory（策略记忆与反思库） | L1 | M2 | [PRD](prd/MOD-05-memory-PRD.md) v1.3（2026-10-17 与代码对齐：I3 = `_seed_guard_all()`、I2 本地扫描 + MOD-02 交叉校验、§13 事件名点分化） |
| MOD-06 | loop（闭环编排） | L2 | M2 | [PRD](prd/MOD-06-loop-PRD.md) v1.8（**新增 `loop baseline` + 起点守卫**：起跑顺序 git 干净检查 → 起点守卫 → gen-000 基线；gen-000 基线的 `play_hash` 交给 `evaluate_arm` ⇒ 写进臂归档，后代继承按测量身份还原——2026-10-15 F63；2026-10-17 B-95） |
| MOD-07 | evolve（harness 自进化） | L2 | M2–M3 | [PRD](prd/MOD-07-evolve-PRD.md) v0.15 ✅ 代码落地（[README](../dice_ratchet/evolve/README.md)）；**C2 = 「候选 `play_hash` ≠ 起点 `play_hash`」**（2026-10-14 F62 取消统计显著性门，统计量降级为 `paired["by_metric"]` 报告项）；冒烟门只留 error/illegal/fallback 三条（F67）；C5 = **三轴相对门 + 绝对 backstop**（`tol=0.05`，backstop 0.35/0.25/0.60；F28→B-44→B-64，原标定依据见 [2026-10-07 §3.1 实测](reviews/2026-10-07-M2-S31-full-agent基线实测.md)）；**跨代继承按「测量身份 = `harness_play_hash`」还原归档**（F63：修复前按版本号判，而被接受那代的候选臂归档记的是 accept **之前**的号 ⇒ 继承静默退化为重测 40 局 ≈¥21），并新增「声明改了 play 面却实测没变 ⇒ 花钱前 fail-closed」——2026-10-15 |
| MOD-08 | evals（评估与测量） | L3 | M1–M3 | [PRD](prd/MOD-08-evals-PRD.md) v0.18（2026-10-17 与代码对齐；臂归档 provenance 的**种子身份**（`resolved_seeds_hash`，解析后列表的指纹；`seeds_hash` 补 `protocol_hash` fallback）与**模型身份**（`model_hash`/`model_backend`，调用方不给时按 policy 角色从配置推导）都进对拍键——2026-10-15 F80/F81；另增**测量身份** `harness_play_hash` 并进对拍键；`tol` 的实测分母改为读 `metrics/arms/` 的**同一代两臂**（原整 run 累加 ⇒ 分母随 run 长度漂移、`se` 低估 ≈2.2×）——2026-10-15 F52/F63） |
| MOD-09 | infra（配置/日志/CLI/守卫） | L3 | M0–M3 | [PRD](prd/MOD-09-infra-PRD.md) v0.9（2026-10-17：命令面补 `loop baseline`/`--allow-ungated-start`、绘图调用点改 `ledger_and_plots`、§9 键名更正；`E_IO_RUN_CONFLICT` 报错附**键级漂移摘要**（新增/删除/值变化各多少键并点名，口径同 `config_hash`）⇒ 区分「某个值被改了」与「配置结构变了」——2026-10-15 F79） |

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

详见 [模块拆分Spec-DiceRatchet.md](模块拆分Spec-DiceRatchet.md) §1。
