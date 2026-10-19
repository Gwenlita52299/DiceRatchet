# GAME-00 网页版骰子 roguelike 游戏 PRD

> 版本：v1.3（2026-10-17 与实现对齐：技能骰 15 → 17，见 §20）· 历史版本：v1.2（2026-09-30 17:21 为 RSI 训练补充局长选项）· 状态：待评审（可交付生成）· 定位：**这份 PRD 是「游戏实现」的唯一事实来源（SSoT）**
> 作者：GAME-00 owner · 日期：2026-09-30
> 上游：[项目计划-DiceRatchet.md](../../项目计划-DiceRatchet.md) · [模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md)
> 下游/平行：[MOD-01-env-rules-PRD.md](MOD-01-env-rules-PRD.md)（**本游戏跑通后**，按本 PRD 规则另行实现一份纯 Python 模拟器供 RSI 训练）
> 配套子文档：[GAME-01 规则内核契约](GAME-01-kernel-contract.md) · [GAME-02 配表规格](GAME-02-content-tables.md)

---

## 0. 一页速览（给实现者）

| 项 | 结论 |
|---|---|
| 一句话 | 一个**浏览器里直接玩**的骰子 roguelike：掷骰 → 把骰面放进技能槽 → 用**学派克制**压强敌；10 关、3 难度、可选局内系统开关。 |
| 交付物 | 前端静态 HTML/JS/CSS；后端 HTTP API + **纯函数规则内核**；共享 JSON 配表 |
| 核心创新 | ① 骰面三色风险分类（安全/均衡/冒险）② **骰子学派 × 怪物学派双向克制方阵** ③ 魔化骰必须回合内处理（风险债） |
| 关键约束 | 内核**零 IO、零 DOM、零全局随机**；规则只存在于内核；前端只渲染 `Observation` + 提交 `Action` |
| 10 关结构 | 3 章：Ch1 关 1–3 / Ch2 关 4–6 / Ch3 关 7–10；**Boss 关 = 第 3、6、10 关**；**精英战斗**出现于难度表 `eliteStages` 指定的关（第 3、5、6、7、9 关的普通战斗节点升格为精英）|
| 难度 | easy / medium / hard —— 只调**配置常量**（心数、敌 HP 倍率、伤害倍率、敌骰数、商店价格、掉落），**不改 AI 代码路径、不改 RNG 消费顺序** |
| 系统开关 | `mapBranching` / `relics` / `events` / `shops` / `schoolCounter` 五个独立开关，开局前可关；不全开也能通关 |
| 免构建 | 原生 ES Module，`node server.js` 即可运行（零 npm 依赖；v1.1 起不再支持纯静态服务器，见 D-33）；不引入打包器、不引入前端框架（可选 preact 但默认不用） |
| 为 RSI 预留 | 内核可 1:1 移植 Python；`Action`/`Observation`/配表 ID 全部是**语言无关的纯数据契约** |

**实现顺序建议**：内核（可单测）→ 后端 API → 前端战斗页 → 地图/商店/事件 → 开关与难度 → 打磨。**任何情况下不得先做 UI 再补内核。**

---

## 1. 背景与目标

### 1.1 背景

DiceRatchet 项目的终点是「小模型在自建游戏里自我进化」。项目原计划直接手写纯 Python 模拟器（MOD-01）作为训练环境。经评审，改为**分两步**：

1. **先做出一个人类真正愿意玩、且规则完备的游戏本体**（本 PRD）——有表现层、有美术表现、有完整的局内系统；
2. **再按同一套规则写纯 Python 模拟器**（MOD-01）供 RSI 闭环训练；未来若条件成熟，**接视觉模型直接对游戏本体做 RSI 训练，跳过模拟器**。

这个顺序带来两条硬性设计要求：

| 目标 | 对设计的要求 |
|---|---|
| 游戏本体可玩（人类向） | 必须有完整的 UI 表现、动画、反馈、音效钩子；操作手感优先 |
| 后续能写出等价模拟器 | 规则必须被**隔离在一个零依赖的纯函数内核**里，内核不得感知 UI、时间、网络、随机源；配表必须是纯数据 |
| 未来能接视觉模型 | 内核与表现层解耦，渲染层可整体替换；观测（Observation）与动作（Action）必须是**结构化、可序列化、语言无关**的 |

### 1.2 目标（可验收）

| # | 目标 | 验收方式 |
|---|---|---|
| G-1 | 浏览器打开即玩，无需构建步骤 | 全新机器 `node server.js` → 打开 `http://127.0.0.1:8080` 能完整通关（v1.1 修订，D-33） |
| G-2 | 三难度可玩且**难度差异可感** | 同一 seed 下 easy/medium/hard 的敌 HP、玩家心数、伤害倍率按 §7 配置表生效；三难度各通关一次 |
| G-3 | ≥10 关，含精英与 Boss | 完整跑通第 1–10 关并击败最终 Boss |
| G-4 | ≥3 个技能骰流派，且流派有**可感知的玩法差异** | 三个流派各通关一次，且胜局中使用该流派技能骰次数 ≥ 8 |
| G-5 | 怪物流派克制形成**骰子选取博弈** | 存在"同一敌人在不同流派构筑下胜率差 ≥ 20 个百分点"的可复现实例 |
| G-6 | 局内系统开关生效且互相独立 | 5 个开关的全关/全开/单开组合下，游戏均可正常开始并通关 |
| G-7 | 内核可移植 | 内核单测覆盖 ≥ 80%；同一 seed + 同一动作日志重放产生逐字段一致的 Observation 摘要 |

### 1.3 非目标（Out of scope，本版明确不做）

- ❌ 多人 / 联机 / 排行榜后端。
- ❌ 移动端适配优先（桌面浏览器为主，最小宽度 1280px；移动端仅保证不崩）。
- ❌ 账号系统、云存档、付费。（存档用 `localStorage`）
- ❌ 美术资产生产：本 PRD 只定义**资产清单与尺寸契约**，具体图片由实现者用占位符（CSS 渐变 + emoji/SVG）先行，后续替换。
- ❌ 音效音乐制作：只定义**触发点清单**，资产可后补。
- ❌ i18n 多语言：仅简体中文 UI（ID 用英文）。
- ❌ Python 模拟器本身（那是 MOD-01 的交付物，本 PRD 只保证其可被等价实现）。
- ❌ 训练/评估/演化逻辑（MOD-05/06/07/08）。

---

## 2. 关键架构决策（ADR）

### ADR-1：三层分离 —— 内核 / 后端 / 前端

```
┌───────────────────────────────────────────────────────────────┐
│  前端 frontend/  （静态 HTML+CSS+原生 ESM）                    │
│  · 只做：渲染 Observation、收集用户意图、播放动画               │
│  · 禁止：任何规则判定（伤害公式、合法性、随机数）               │
│  · 与后端交互：唯一的 fetch 封装 net.js                         │
└───────────────────────────┬───────────────────────────────────┘
                            │ HTTP + JSON（契约见 §9）
┌───────────────────────────▼───────────────────────────────────┐
│  后端 backend/  （Node ≥18，原生 http，零 npm 依赖）            │
│  · 薄壳：路由、会话、序列化、CORS、静态托管                     │
│  · 禁止：规则判定（规则全在 kernel）                            │
└───────────────────────────┬───────────────────────────────────┘
                            │ 纯函数调用
┌───────────────────────────▼───────────────────────────────────┐
│  规则内核 kernel/  （纯 ESM，零依赖）★ 全项目唯一规则真相       │
│  · createGame / reduce / observe / legalActions / configHash    │
│  · 零 IO、零 DOM、零 Date/Math.random、零网络、零日志            │
│  · 可被 Node 直接 import、被前端（无状态模式）复用、被移植 Python│
│  · 配表 content/*.json 由 kernel/loader 注入（内核不读文件）     │
└───────────────────────────────────────────────────────────────┘
```

**决策理由（用户问题的直接回答）**：这一形态**最有利于后续构建模拟器**——
1. 模拟器 = 同样的 `createGame/reduce/legalActions` 签名换成 Python；`reduce` 的输入输出是纯 JSON 可序列化数据，可逐字段对拍。
2. 规则只有一份真相，避免"网页版和模拟器规则漂移"这一类最致命的 RSI 数据污染。
3. 内核零 IO 使 Python 移植不需要处理文件/网络/时间差异，只需要处理**语言语义差异**（见 GAME-01 §7 移植清单）。
4. 为视觉模型路线预留：表现层可替换成 Canvas 渲染，内核接口不变；`observe()` 有 `view: "visual"` 档位（只给可见物，不给数值），见 GAME-01 §5.4。

### ADR-2：服务端持有会话（stateful session），而非无状态同步

- 后端在内存中持有 `GameState`，前端只持有 `sessionId` + 最近一次 `Observation`。
- 理由：`GameState` 含隐藏信息（未来骰面、RNG 流状态、未揭示地图），**绝不能下发前端**。服务端持有是防止作弊与防泄漏的最简方式（对齐 Spec INV-2 精神）。
- 同时提供**无状态重放端点** `POST /api/replay`：给定 `seed + config + actions[]` 重建并返回每步 Observation 摘要，用于审计、回放、以及未来模拟器对拍。

### ADR-3：确定性随机 —— 自实现 PRNG + 命名流

- 禁用 `Math.random()`。自实现 **SplitMix64**（32 位拆分为两个 32 位整数运算，避免 JS 大整数精度问题；实现细节见 GAME-01 §3）。
- **命名流**：`map` / `dice.player` / `dice.enemy` / `shop` / `event` / `loot` / `levelup` / `forge`。每个流独立派生自 `blake2b(seed|stream)` 的等价实现（JS 侧用自实现 `xmur3`+`splitmix64` 派生；Python 侧用 `hashlib.blake2b`，移植时必须对拍一致）。
- 理由：对齐 Spec INV-6；让模拟器、审计、未来训练共用同一随机语义。
- 配表/内容全部由**流内消费顺序**决定，任何"改玩法但不该改随机"的调整都不得改变流消费顺序（§8 难度表条目已遵守此约束）。

### ADR-4：内容与代码分离，全部纯数据

- 所有骰面、技能骰、怪物、关卡、事件、遗物都是 `content/*.json`，由配表 ID 引用。
- 内核只接受**已加载的配表对象**；配表加载与校验由 `kernel/loader.js` 负责（加载器可以读文件，内核不行）。
- 理由：MOD-01 的配表可直接复用同一份 JSON；数值平衡可迭代而不改代码；未来可让 agent 演化配表。

### ADR-5：不引入前端框架与构建器

- 原生 ES Module + CSS 变量 + 事件委托。允许使用 `fetch`/`localStorage`/`Web Animations API`。
- 理由：**零构建 = "在 html 中可运行" 的硬要求**；也避免 Node 版本/依赖地狱拖慢"做出可玩游戏"这一首要目标。

---

## 3. 术语表（全项目统一，中英对照，实现时用英文 ID）

| 中文 | ID | 定义 |
|---|---|---|
| 骰子 | `Die` | 一颗有 6 个面的骰子对象，属于玩家骰池或敌人。 |
| 骰面 | `Face` | 骰子的一个面，含 `kind`（效果类型）+ `value`（数值）+ `school`（学派）。 |
| 骰池 | `pool` | 玩家拥有的骰子集合（抽骰堆 + 手骰 + 弃骰堆）。 |
| 手骰 | `hand` | 本回合已掷出、可操作的骰子；上限 8。 |
| 净化值 | `virtue` | 玩家的**战斗内血条**（每场战斗开始回满）。归零 → 失去 1 心。 |
| 心 | `hearts` | 玩家的**局内生命**（跨战斗保留）。归零 → 本局失败。 |
| 光盾 | `ward` | 临时减伤，回合开始时清零。 |
| 魔化 | `corruption` | 敌人血条名；也是**施加给目标的负面伤害类型**。 |
| 自魔化 | `selfCorruption` | 骰面对**自己**造成魔化值的负面效果（风险来源）。 |
| 魔化骰 | `corruptFace` | 带 `selfCorruption` 的骰面。**回合结束时若手骰/槽内仍有未处理的魔化骰，无法结束回合。** |
| 技能槽 | `slot` | 玩家装备的"技能"，每回合提供可放置骰面的位置，含约束与效果。 |
| 学派 | `school` | 骰子/怪物所属流派，参与克制方阵。 |
| 克制 | `counter` | 学派间 1.3× / 0.77× 的双向倍率关系 (§5.1)。 |
| 章节 | `chapter` | 10 关分 3 章，章末为 Boss。 |
| 关 | `stage` | 一次遭遇（战斗 / 精英 / Boss / 商店 / 事件 / 宝箱 / 神殿 / 锻造）。 |

---

## 4. 游戏核心循环与规则

### 4.1 一局游戏（Run）的流程

```
[开局配置页]  选难度 / 开关 / 角色 / 种子
      │
      ▼
[地图页·第 1 关] ── 选择相邻节点 ──► [遭遇页]
      ▲                                   │
      │                                   ├─ 战斗/精英/Boss → 战斗页 ──胜利──┐
      │                                   ├─ 商店   → 商店页               │
      │                                   ├─ 事件   → 事件页（选项）        │
      │                                   ├─ 宝箱   → 骰子三选一            │
      │                                   ├─ 神殿   → 二选一               │
      │                                   └─ 锻造   → 改造骰面             │
      └──────────── 节点结算 ──────────────────────────────────────────────┘
      │
   第 10 关 = 最终 Boss → 胜利结算页（Run 结束）
      │
   任意战斗失败（心数归零）→ 失败结算页（Run 结束）
```

### 4.2 战斗流程（核心，必须精确实现）

**回合结构**，每个玩家回合：

| 阶段 | 名称 | 说明 |
|---|---|---|
| P1 | `upkeep` | ① 光盾清零 ② 中毒/灼烧等持续状态结算 ③ 检查玩家 `virtue ≤ 0` → 失心 |
| P2 | `draw` | 从抽骰堆抽 `drawPerTurn`（默认 4）颗骰子；手骰上限 8，超出直接进弃骰堆；抽骰堆空则把弃骰堆洗回（消费 `dice.player` 流） |
| P3 | `roll` | 为每颗新手骰掷出骰面（消费 `dice.player` 流）；**同一时刻提交敌人本回合的意图骰并公开展示**（消费 `dice.enemy` 流）。敌人意图在玩家行动期间**不可变更**，保证 `intent` 不是谎言（对应 MOD-01 FR-01-06 的防预读约束） |
| P4 | `player` | 玩家自由行动（见下） |
| P5 | `end_turn` 检查 | 手骰与技能槽中**不得残留未处理的魔化骰**；否则「结束回合」按钮置灰并高亮残留物 |
| P6 | `enemy` | 敌人按已提交意图**从左至右**依次结算（**不重掷**） |
| P7 | `cleanup` | 手骰全部入弃骰堆；`turn += 1` → 回到 P1 |

**P4 玩家可执行的动作（`Action` 全集）**：

| kind | 参数 | 说明 |
|---|---|---|
| `play_face` | `dieId`, `slotId`, `faceIndex?`, `targetId?` | 把一颗手骰放到一个技能槽位；`targetId` 为攻击/对敌状态面的敌人 ID，或 `reroll/modify/duplicate_face` 的目标手骰 ID，缺省由内核规范化（D-19） |
| `discard_die` | `dieId` | 弃掉一颗手骰（丢弃魔化骰的**代价路径**，见 §5.3） |
| `purify` | `dieId` | 消耗 1 次净化，无代价清除 1 颗魔化骰（D-31） |
| `use_ability` | `abilityId`, `dieId?`, `value?` | 使用角色主动技能（每场战斗限次，D-23） |
| `end_turn` | — | 结束回合（受 P5 约束） |

**胜负判定**：
- 敌人 `corruption ≤ 0` → 击杀 → 掉落 + 进入节点结算。
- 玩家 `virtue ≤ 0` → `hearts -= 1`；`hearts > 0` 则本场战斗**立即结束并判定失败**？→ **决策 D-05**：玩家 `virtue` 归零且仍有心时，**本场战斗判负、失去 1 心、本场战斗结束回到地图**（但战斗节点视为已通过，不再重复），`virtue` 回满。这样避免"磨血"策略破坏心数经济。
- 玩家 `hearts ≤ 0` → Run 失败。

### 4.3 技能槽（Slot）系统

玩家不直接"打牌"，而是**把骰子放进装备的技能槽**。每颗骰子是一个"任意门"，其`face.kind` 决定它能被哪些槽接受。

**槽类型（`slotKind`）与兼容的骰面 kind**：

| slotKind | 接受 face.kind | 约束字段 | 说明 |
|---|---|---|---|
| `attack` | `purge`（敌人向） | `constraint` | 造成魔化 |
| `defense` | `ward`（自向） | `constraint` | 获得光盾 |
| `heal` | `mend`（自向） | `constraint` | 恢复净化值 |
| `utility` | 任意 | `constraint` | 触发骰面自身效果；**可在同一回合重复使用**（`reusable: true`）。`status/draw/reroll/modify/gold/nothing/duplicate_face` 面只能进此槽（D-11） |
| `cursed` | 仅 `selfCorruption` 面 | — | 敌人施加的"毒槽"，占位并可被净化 |

**约束 DSL（`constraint`）**，与 MOD-01 §4 保持同一文法以便直接移植：

```
any | odd | even | (ge|gt|le|lt|eq):int | in:int,int,...
```

阶段一只允许**单骰单条件**。多骰/组合条件列入 §12 待定项。

### 4.4 章节与关卡推进

- 一章 = 一张地图，一张地图含若干**层（`rows`）**，每层 2–4 个节点，玩家每层只能走一个节点，相邻层之间有边连接（类《杀戮尖塔》）。
- **关（stage）编号全局 1–10，贯穿三章**；章末最后一层强制 Boss 节点。
- 地图生成消费 `map` 流；**同一 seed + 同一 config 生成完全相同的地图**。

| 关 | 章节 | 建议节点类型 | Boss | 备注 |
|---|---|---|---|---|
| 1 | Ch1 | battle 普通 | — | 教学：只用基础骰，本关与第 2 关不出现 `selfCorruption` 面 |
| 2 | Ch1 | battle + 宝箱 | — | 首次获得技能骰 |
| 3 | Ch1 | battle + 商店 + **elite** | ✅ `boss_verdant_bloom` | 首次精英与首次 Boss |
| 4 | Ch2 | battle + 商店 | — | 教学结束：敌人学派标签首次强制展示、克制箭头常驻 |
| 5 | Ch2 | battle + 事件 + 宝箱 | — | medium/hard 在此关有精英 |
| 6 | Ch2 | battle + 锻造 + 商店 + **elite** | ✅ `boss_arcana_spiral` | |
| 7 | Ch3 | battle + 商店 + 事件 | — | hard 在此关有精英 |
| 8 | Ch3 | battle + 神殿 + 宝箱 | — | |
| 9 | Ch3 | battle + 锻造 + **elite** | — | 前哨战 |
| 10 | Ch3 | battle + 商店 + 神殿 | ✅ `boss_chaos_core` | 最终 Boss，三阶段三学派 |

> **精英与 Boss 是两种不同的节点**：Boss 关由 `stages[].boss` 决定（第 3/6/10 关，每关最后一个节点）；精英则是在 `eliteStages` 指定的关内，把**普通战斗节点**的敌人编队替换为精英编队（`g_e*`）。两者可同时出现在同一关（第 3、6 关）。
> 每关的具体行数、节点预算、怪物编队见 [GAME-02 配表规格](GAME-02-content-tables.md) §4。

---

## 5. 骰子系统（核心设计）

### 5.1 学派与克制方阵（本作的策略心脏）

三个学派：**锋刃（Blade）/ 秘法（Arcana）/ 自然（Verdant）**，形成三角克制。

```
            ┌───────────────┐
            │   锋刃 Blade   │  高爆发、直伤、处决
            └───────┬───────┘
              克制  │  ▲  被克制
                    ▼  │
   ┌────────────┐        ┌────────────┐
   │ 自然 Verdant│◄──────│ 秘法 Arcana │
   │ 恢复/减伤/毒│ 克制   │ 控制/过牌/连携│
   └────────────┘        └────────────┘
```

| 攻击方 | 目标 | 倍率 | 名称 |
|---|---|---|---|
| Blade | Verdant | ×1.30 | 克 |
| Arcana | Blade | ×1.30 | 克 |
| Verdant | Arcana | ×1.30 | 克 |
| Verdant | Blade | ×0.77 | 被克 |
| Blade | Arcana | ×0.77 | 被克 |
| Arcana | Verdant | ×0.77 | 被克 |
| 同学派 / `neutral` | 任意 | ×1.00 | 平 |

**双向生效规则（`schoolCounter` 开关开启时）**：
- **玩家 → 敌人**：玩家骰面的 `school` 决定玩家侧倍率。
- **敌人 → 玩家**：敌人的 `school` 决定敌伤倍率，
  - 玩家**当前携带的技能槽/骰池主学派**（`deckSchool` = 骰池中学派骰数量最多的学派）作为玩家侧防守学派；
  - `enemy.school` 克 `deckSchool` → 敌伤 ×1.30；被克 → ×0.77。
- **同源多次加成只算一次**：一颗骰面只算一次克制倍率。
- 倍率在**取整前**应用，最终伤害 `floor(base × mul)`，最小 1（攻击方向）。

**博弈性设计要求（G-5 的可验收来源）**：
1. 战斗开始前，敌人学派与"预测最强攻击面"公开可见（`EnemyView.school` + `revealedTaunt`）。
2. 玩家在**宝箱/商店**看到的是「三选一」，其中两个选项为**同学派**，第三个为**克当前敌学派**的学派 → 形成"选克制还是选构筑"的取舍。
3. 商店/宝箱的学派倾向受**当前所在关卡敌人学派**偏置（`biasStrength` 可配置，默认 0.5）——即"这条路的敌人是自然，商店更容易刷出锋刃骰"。
4. 骰池主学派 `deckSchool` 会被敌人用于防守克制，因此**单一学派堆叠会被针对**，逼迫混合构筑 → 这正是要求 4 的"增加骰子选取博弈性"。

> **决策 D-01（本 PRD 提出，待回填 Spec）**：克制倍率取 `1.30 / 0.77`（互为倒数，便于平衡推导）；`deckSchool` 用"骰池中学派骰计数最大者，平局取注册顺序靠前者"。

### 5.2 骰子分类总览

```
Die
├── normal 骰（普通骰）      6 面，同色系，构成骰池主体
│   ├── starter   基础骰    开局 8 颗（白角），普通骰的初始形态
│   └── forged    锻造骰    普通骰经「锻造」改造骰面后的产物
└── skill 骰（技能骰）       6 面，含本学派机制，决定构筑方向
    ├── common    通用技能骰   任意流派可用，无强学派倾向
    ├── blade     锋刃系
    ├── arcana    秘法系
    └── verdant   自然系
```

**与 Astrea 的对应关系**（参考来源见 §13）：Astrea 把骰子按**骰面风险**分为安全骰/均衡骰/冒险骰（安全骰 6 面全有益；均衡骰 2–3 面含不利；冒险骰 4–6 面含不利），并设有独立的"神勇骰"（Epic）作为技能骰。本作**保留风险分类作为骰面的横切属性**，同时用**学派**作为构筑分类轴——即「一颗技能骰 = 一个学派 × 一个风险等级」的二维定位。

### 5.3 骰面（Face）规格

每颗骰子有 6 个面。**每颗骰子的 6 个面固定绑定 6 个 pip 值（1–6），骰面效果按 faceIndex（0–5，对应骰面 1–6）索引。** 掷骰 = 从 1–6 均匀取一个 pip → 取该 pip 对应的面。

| 字段 | 类型 | 说明 |
|---|---|---|
| `faceIndex` | 0..5 | 对应骰面 1..6 |
| `label` | string | 骰面名称（UI 展示，如「净斩 3」） |
| `kind` | enum | `purge`(对敌魔化) / `ward`(光盾) / `mend`(回净化) / `status`(上状态) / `selfCorruption`(自魔化) / `draw`(抽骰) / `reroll`(重掷) / `modify`(改骰面) / `gold`(得币) / `nothing` |
| `value` | int | 效果数值（`purge`/`ward`/`mend`/`selfCorruption`/`status` 用） |
| `school` | enum | `blade` / `arcana` / `verdant` / `neutral` |
| `target` | enum | `enemy` / `self` / `none` |
| `statusKind` | enum? | `kind=status` 时必填：`poison` / `burn` / `weak` / `strength` / `regen` |
| `tags` | string[] | 用于遗物/事件条件匹配（如 `["execute"]`） |

**风险分类（riskTier，按面统计自动判定，不手填）**：

> **v1.1 修订（D-09）**：不利面 = 含 `selfCorruption` 分量的面或 `nothing` 面。原规则（balanced 2–3 面 / risky 4–6 面）与 GAME-02 配表矛盾，改为能使全部配表自洽的阈值。

| riskTier | 判定规则 | 色 | 定位 |
|---|---|---|---|
| `safe` 安全骰 | 6 面**全部**为有益（无不利面） | 蓝 | 稳定构筑基底 |
| `balanced` 均衡骰 | 6 面中 **1–2 面**为不利面 | 绿 | 默认推荐 |
| `risky` 冒险骰 | 6 面中 **≥3 面**为不利面 | 红 | 高数值高代价 |
| `cursed` 厄咒骰 | 由敌人/事件强行塞入骰池，恒有 `selfCorruption` 且不可锻造 | 紫 | 负面物 |

**魔化骰（corruptFace）与风险债机制**（本作独有的策略压力）：

- 任何 `selfCorruption` 面打出或被使用后，该骰子被标记为**魔化骰**，留在手骰区无法被正常弃掉。
- **P5 检查**：玩家结束回合时，若手骰区或技能槽中仍存在魔化骰 → 「结束回合」被禁用。
- **清除魔化骰的三条路径**（每条都有代价，构成取舍）：
  1. `discard_die`：弃掉它 → 玩家立即承受 **该面 `value` 的 50%（向上取整）** 自魔化。
  2. 放入任意 `utility` 槽 → 承受 **100%** 自魔化，但触发骰面附带的正向效果（若该面有）。
  3. 消耗 1 点 `purify`（净化能力，角色技能，每场 2 次）→ **无代价**清除 1 颗魔化骰（角色 build 资源）。
- 设计意图：让"高风险高回报"成为**每回合都要重新决策**的东西，而不是一次性构筑选择。

### 5.4 骰面效果结算与伤害公式

```
base(face, die) =
    face.value
  + slot.bonus                       # 槽位被动加成
  + owner.status.strength            # 力量层数（攻击向）
  - owner.status.weak                # 虚弱层数

最终值 = floor( base × schoolMul(sourceSchool, targetSchool) × relicMul )   # 攻击向最小 1

purge  → target.corruption -= 最终值           # 对敌
ward   → owner.ward        += 最终值           # 自向，回合开始清零
mend   → owner.virtue       = min(max, +最终值)
selfCorruption → owner.virtue -= 最终值（不吃学派倍率、不吃光盾、不吃力量）
status → target.status[kind] += value          # 中毒/灼烧在 upkeep 结算
```

**结算顺序硬约束**（照抄 MOD-01 FR-01-05 的精神，便于对拍）：

```
1. 光盾吸收：absorbed = min(target.ward, dmg); target.ward -= absorbed
2. 实伤：target.corruption(virtue) -= (dmg - absorbed)
3. 死亡判定：corruption ≤ 0 → 击杀；virtue ≤ 0 → 失心
```

**禁止**先扣血再算光盾。所有中间值用**整数**，倍率在最后一步 `floor`。

### 5.5 技能骰三流派设计（≥3 流派，此处定义 3 个 + 通用）

**流派机制关键词**（每系一个独占资源/机制，决定玩法差异）：

| 学派 | 独占机制 | 玩法节奏 | 反制手段（克制谁） |
|---|---|---|---|
| **锋刃 blade** | **蓄势（charge）**：每次打出锋刃攻击面，基础值 + 当前层数，然后层数 +1（上限 5）；回合末本回合未打出锋刃攻击则层数减半。`execute` 标签面在目标 HP ≤ 50% 时基础值 ×2（D-20） | 爆发型，前期猥琐后期斩杀 | 克 `verdant` |
| **秘法 arcana** | **过牌与重掷**：低单次伤害，靠抽骰/重掷/复制骰面堆叠连携 | 组合型，靠手牌质量取胜 | 克 `blade` |
| **自然 verdant** | **续航与毒**：小额回净化 + 中毒 DoT + 减伤；长线消耗 | 控制型，越打越稳 | 克 `arcana` |

**通用技能骰（common，任意构筑可用，无强学派倾向）**：提供"过牌/金币/改骰面"等工具效果，`school=neutral` 不参与克制。

**技能骰配表（示例锚点，完整 17 颗见 [GAME-02](GAME-02-content-tables.md) §2/§3）**：

| ID | 名称 | 学派 | risk | 核心骰面 | 机制 |
|---|---|---|---|---|---|
| `skill_blade_execute` | 处决斩 | blade | balanced | 4×`purge 4`＋2×`selfCorruption 2` | 高单点，蓄势满时额外 ×2 |
| `skill_blade_flurry` | 连环突刺 | blade | safe | 6×`purge 2` | 稳定叠蓄势 |
| `skill_blade_berserk` | 狂战 | blade | risky | 4×`purge 7`＋2×`selfCorruption 4` | 极高爆发，代价大 |
| `skill_arcana_echo` | 回响 | arcana | safe | 3×`draw 1`＋3×`purge 2` | 过牌 |
| `skill_arcana_shift` | 移位 | arcana | balanced | 3×`reroll`＋3×`purge 3` | 手骰质量修正 |
| `skill_arcana_hex` | 咒印 | arcana | balanced | 3×`status weak 2`＋3×`purge 2` | 降敌输出 |
| `skill_verdant_bloom` | 绽放 | verdant | safe | 6×`mend 3` | 稳定续航 |
| `skill_verdant_venom` | 荆棘毒 | verdant | safe | 3×`status poison 4`＋3×`purge 1` | DoT |
| `skill_verdant_bark` | 树皮甲 | verdant | balanced | 4×`ward 5`＋2×`selfCorruption 1` | 高减伤 |
| `skill_common_forge` | 锻造锤 | neutral | safe | 3×`modify +1`＋3×`gold 5` | 改骰、理财 |
| `skill_common_gamble` | 赌徒 | neutral | risky | 4×`purge 6`＋2×`nothing` | 白板面=纯风险 |

> **设计规则（实现时必须遵守）**：
> 1. 每颗技能骰必须至少 **2 个面**与所属学派的独占机制相关（否则该骰不构成"流派骰"）。
> 2. 每颗技能骰的 6 个面中，**至少 1 面**是"反制本系天敌"的通用面（如 natural 骰带 1 面 `purge`），保证单系构筑不至于完全打不动天敌。
> 3. `risky` 骰的**期望值必须高于**同造价 `safe` 骰约 25–40%（收益补偿风险），该数值由实现者用模拟脚本标定并写入配表备注。

---

## 6. 怪物系统

### 6.1 怪物规格

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `name` / `desc` | string | |
| `school` | enum | `blade`/`arcana`/`verdant`/`neutral`，参与克制 |
| `tier` | enum | `normal` / `elite` / `boss` |
| `maxCorruption` | int | 敌血 |
| `slots` | `EnemySlot[]` | 敌人的技能槽（含约束与效果），意图由槽推导 |
| `diceCount` / `diceSides` | int | 每回合意图骰数/面数 |
| `ai` | enum | `greedy_damage` / `greedy_block` / `scripted` / `boss_pattern` |
| `intentRules` | object | 意图可见性：`revealCount`（显示几个意图骰）/ `hiddenUntil`（第几回合起全揭示） |
| `taunt` | string | 开局展示的学派倾向提示（供玩家做骰子取舍） |
| `drops` | object | 掉落：`starShards`（币）/ `relicChance` / `dieGrade` |

### 6.2 怪物学派与克制（要求 4 的落点）

- **怪物学派不是标签，是有后果的**：
  - 怪物用 `school` 攻击玩家 → 按 `deckSchool` 计算倍率（§5.1）。
  - 玩家用对应学派骰子攻击它 → 按克制表受益/受损。
- **怪物意图含学派**：敌人意图骰上直接显示学派图标与克制箭头（`↑` 克你 / `↓` 被你克 / `=` 平），玩家可在**开战前**就据此调整骰池思路。
- **对局内的学派博弈强度分级（`counterIntensity`）**：
  - `off`（开关关闭）→ 全部 ×1.00；
  - `soft`（简单模式默认）→ 克制 ×1.20 / 被克 ×0.85；
  - `standard`（中等默认）→ ×1.30 / ×0.77；
  - `punishing`（困难默认）→ ×1.45 / ×0.70。
  该值属于**难度配置**，可被配表覆盖（§7）。
- **单一学派惩罚**：骰池学派技能骰 ≥ 3 颗（`monoMinDice`，D-22）且 `deckSchool` 集中度 ≥ 70% 时，敌人额外获得 `monoPenalty`（默认 +15% 伤害）——逼迫混合构筑，制造"选骰博弈"。
- **学派盲区提示**：若玩家骰池**完全没有**能克制当前敌人的学派骰，战斗开始前弹出非阻塞提示 `「你的骰池没有能克制<学派>的骰子」`。

### 6.3 怪物清单（结构，完整配表见 GAME-02 §2）

| tier | 数量要求 | 设计约束 |
|---|---|---|
| normal | ≥ 12 只 | 每学派 ≥ 3 只；HP 范围随关卡；单槽到三槽递进 |
| elite | ≥ 4 只 | 每学派 ≥ 1 只；HP ≈ 同关 normal 的 2.5–3×；至少 1 只带"厄咒槽"（向玩家骰池塞 `cursed` 骰） |
| boss | 3 只（第 3、6、10 关各一） | 每只必须有**阶段（phase）**机制：HP 阈值切换 AI 模板与学派 |

**第 10 关最终 Boss 设计（三阶段）**：

| 阶段 | 触发 | 学派 | 行为 |
|---|---|---|---|
| P1 | 100%–67% HP | `arcana` | 大量 `weak` + 抽骰压制，逼迫玩家处理魔化骰 |
| P2 | 66%–34% HP | `blade` | 爆发直伤 + 破坏玩家技能槽（临时禁用 1 槽） |
| P3 | 33%–0% HP | `verdant` | 自回血 + 中毒 DoT，考验长线续航 |

> 三阶段三种学派 = **单一学派构筑必然在某一阶段吃亏**，这是要求 4 的最终检验点。

---

## 7. 难度系统

### 7.1 三难度差异（**只调配置常量，不改代码路径，不改 RNG 消费顺序**）

| 配置键 | easy | medium | hard | 说明 |
|---|---|---|---|---|
| `heartsMax` | 4 | 3 | 2 | 玩家局内心数 |
| `virtueMax` | **22**（原 12） | **24**（原 10） | **24**（原 9） | 每场战斗的净化值上限（v1.1 平衡标定） |
| `drawPerTurn` | 4 | 4 | 3 | 每回合抽骰数 |
| `handMax` | 8 | 8 | 5 | 手骰上限 |
| `enemyHpMul` | **0.90**（原 0.80） | **0.90**（原 1.00） | **0.60**（原 1.35） | 敌血倍率（取整；v1.1 平衡标定） |
| `enemyDmgMul` | **1.00**（原 0.80） | **0.85**（原 1.00） | **0.575**（原 1.25） | 敌伤倍率（v1.1 平衡标定） |
| `enemyExtraDice` | 0 | 0 | +1 | 敌人每回合额外意图骰（只提高满足槽约束的概率，每槽每回合至多触发一次，D-13） |
| `eliteStages` | `[3,6]` | `[3,6,9]` | `[3,5,7,9]` | 哪些关的普通战斗节点升格为精英战斗（Boss 关固定为 3/6/10，不在此列） |
| `counterIntensity` | soft (1.20/0.85) | standard (1.30/0.77) | punishing (1.45/0.70) | 克制强度 |
| `monoPenalty` | 0.00 | 0.15 | 0.25 | 单学派集中惩罚 |
| `shopPriceMul` | 0.85 | 1.00 | 1.20 | 商店价格 |
| `goldMul` | 1.25 | 1.00 | 0.80 | 掉落币倍率 |
| `relicSlots` | 4 | 3 | 2 | 遗物栏位（`relics` 开时） |
| `potionPerStage` | 1 | 1 | 0 | 「净化」使用次数（清魔化骰，§5.3） |
| `bossPhaseHpBonus` | 0 | 0 | +20% | Boss 阶段血量加成 |
| `reviveOnce` | true | false | false | easy 独有：首次失心不结束战斗，改为 virtue 回满 50% |

> **v1.1 平衡标定说明（2026-09-30 16:39）**：按 GAME-02 §8 流程，仅调整了 `virtueMax` / `enemyHpMul` / `enemyDmgMul` 三项，其余键保持原值。启发式 bot（`greedy_counter`）200 局/难度通关率：easy 61.0% / medium 35.5% / hard 12.5%，均落在 §7.2 目标区间。
> hard 的倍率低于 medium 是有意为之：hard 的结构性惩罚（抽 3、手骰上限 5、0 次净化、+1 意图骰、克制 ×1.45/×0.70、单系惩罚 +25%、Boss +20% HP、2 心）叠加后远超倍率本身，"hard 结构 + medium 倍率"实测通关率为 0%。详见 `src/game/docs/BALANCE.md`。

> **决策 D-02（待回填 Spec）**：难度**只允许**通过上表键调整；新增难度差异必须走配置键，禁止任何 `if (difficulty === 'hard')` 式的分支出现在内核的业务逻辑中（允许出现在 `applyDifficulty(config)` 这一个函数里）。

### 7.2 难度校验要求

- 三个难度必须分别有**可通关**的构筑路径（实现者用启发式脚本自动跑 200 局/难度，easy 通关率目标 45–65%，medium 25–40%，hard 10–20%）。
- 难度不得通过"提高随机性"实现（不得改变 RNG 流消费量）——否则无法与模拟器对拍。

---

## 8. 局内系统与**前置开关**

### 8.1 开关定义（开局配置页，默认值标注）

| 开关 ID | 默认 | 关闭后行为 | 开启后新增 |
|---|---|---|---|
| `mapBranching` | **开** | 每关只有**单一线性节点**，地图页自动推进，不显示分支 | 逐层二选一/三选一节点，地图页展示路径与预览 |
| `relics` | **开** | 无遗物系统，所有遗物相关 UI 隐藏，事件/商店不产出遗物 | 遗物栏（数量受难度限制），被动全局加成，可在商店/精英/事件获得 |
| `events` | **开** | 「事件」节点不生成；`?` 节点改为宝箱 | 随机叙事事件（选项 → 后果，含正面/负面/赌博型） |
| `shops` | **开** | 「商店」与「锻造」节点不生成；改为宝箱 | 商店买骰/遗物/删骰；锻造改造骰面 |
| `schoolCounter` | **开** | 所有克制倍率固定 ×1.00，UI 隐藏克制箭头（怪物学派仍显示为风味标签） | 学派克制方阵生效（§5.1） |

**组合约束（必须校验）**：
- 全部关闭 → 游戏退化为**纯战斗线性闯关**（"战斗 + 精英/Boss 模式"），仍须可通关。这是回归测试的**基线配置**。
- `relics=false` 时，任何配表/事件里引用遗物的条目必须被 loader 过滤并发出 warning。
- `events=false` 时，事件池不加载。
- 只开 `mapBranching` → 增加策略深度但不引入随机事件，**推荐的中间档**。

### 8.2 开局配置页字段

```jsonc
{
  "difficulty": "medium",        // easy | medium | hard
  "seed": 12345,                 // 整数；留空则前端随机生成
  "character": "warrior",        // 阶段一仅 1 个角色
  "toggles": {
    "mapBranching": true, "relics": true, "events": true,
    "shops": true, "schoolCounter": true
  },
  "debug": { "showHiddenInfo": false, "logActions": true },
  "stageCount": 10               // v1.2（D-34）：局长，只能取章末 Boss 关 3 | 6 | 10；缺省 10。开局页不展示，仅 API / RSI 训练使用
}
```
> `debug.showHiddenInfo` **仅在开发模式可用**，字段必须存在于 `config` 哈希中，但它**不得**影响 `observe()` 的服务端输出（隐藏信息永不通过 API 下发，调试靠后端终端日志）。

### 8.3 遗物（`relics=true`）规格

| 字段 | 说明 |
|---|---|
| `id` / `name` / `desc` / `rarity` | `common`/`rare`/`boss` |
| `trigger` | `on_battle_start` / `on_turn_start` / `on_play_face` / `on_kill` / `on_shop` / `passive` |
| `condition` | 可选：学派/骰面 tag/骰池集中度 等 |
| `effect` | 声明式效果列表（同 `Face` 的 kind 集合 + `modifier` 型） |

**遗物设计约束**：
- 至少 **6 个**遗物必须**与学派联动**（如「锋刃印记：打出 blade 骰面时 +1 蓄势」）——这样 `schoolCounter` 与 `relics` 两个系统互相加权。
- 至少 **2 个**遗物是**负面/代价型**（如「贪婪之眼：金币 +50%，但每场战斗失去 2 点初始净化值」），制造取舍。
- 遗物效果不得修改 RNG 消费顺序。

### 8.4 事件（`events=true`）规格

- 每个事件 = `{id, title, text, options[], weight, stageRange[], relicsOnly?}`。
- 每个选项 = `{label, requirement?, outcomes[]}`；`outcomes` 为 `{weight, effect[], text}`，支持**确定性结果**与**概率结果**（概率消费 `event` 流）。
- 事件必须**至少 3 个选项**且**至少 1 个选项的期望为正**、**至少 1 个为负险**。
- 事件数量 ≥ 10 个，其中 ≥ 3 个与学派相关（可指定换取某学派骰子）。

### 8.5 商店与锻造（`shops=true`）规格

| 商店类型 | 陈列 | 价格基准 | 刷新 |
|---|---|---|---|
| `shop_dice` | 3 颗技能骰（含 1 颗**克制当前关敌学派**的骰，见 §5.1 博弈性要求 3） | 45–75 星屑 | 20 星屑 |
| `shop_relic` | 2 个遗物（`relics=true` 时） | 80–150 | 30 |
| `shop_remove` | 删除 1 颗骰子 | 60（每删一次 +15） | — |
| `forge` | 改造 1 个骰面（在 5 个随机候选面中选 1 替换） | 35–55 | 15 |

价格统一乘以 `shopPriceMul`（§7.1）。

---

## 9. 后端 API 契约

### 9.1 通用约定

- 协议：HTTP/1.1，`Content-Type: application/json; charset=utf-8`。
- 所有响应包 `{ "ok": true, "data": ... }` 或 `{ "ok": false, "error": { "code": "E_...", "message": "..." } }`。
- 错误码前缀（与 Spec §8 对齐）：`E_API_*` / `E_KERNEL_*` / `E_ACTION_*` / `E_STATE_*`。
- 端口默认 `8080`，可用 `--port` 覆盖；同时托管 `frontend/` 静态文件。
- 无鉴权；仅监听 `127.0.0.1`（**默认必须是本机**，避免误暴露整局隐藏信息）。

### 9.2 端点清单

| 方法 | 路径 | 请求 | 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/health` | — | `{version, kernelVersion, buildTime}` | 健康检查 |
| GET | `/api/config/schema` | — | 难度表 + 开关表 + 枚举全集 + 默认值 | 前端配置页由此**动态渲染**，不得硬编码 |
| POST | `/api/game` | `{difficulty, seed?, character, toggles}` | `{sessionId, configHash, observation}` | 开局 |
| GET | `/api/game/:sid` | — | `{observation, legalActions[]}` | 拉取当前状态 |
| POST | `/api/game/:sid/action` | `{action}` | `{observation, events[], legalActions[], outcome?}` | 执行动作 |
| POST | `/api/game/:sid/reset` | `{config?}` | 新 `sessionId` + `observation` | 重开（可换配置） |
| POST | `/api/replay` | `{config, actions[]}` | `{steps:[{obsDigest, events}], outcome}` | **无状态重放**，审计/对拍用 |
| GET | `/api/content/:table` | — | 配表内容 | 前端展示用（见 §9.4 可见性） |
| POST | `/api/seed` | `{difficulty}` | `{seed}` | 生成随机种子（服务端 RNG 允许用真随机，**仅此一处**） |

### 9.3 请求/响应示例

```jsonc
// POST /api/game/:sid/action
{ "action": { "kind": "play_face", "dieId": "d-0007", "slotId": "slot_1", "faceIndex": 2 } }

// →
{
  "ok": true,
  "data": {
    "observation": { /* 见 §9.4 */ },
    "events": [
      { "type": "damage", "target": "e-slime-0", "value": 6, "school": "blade",
        "schoolMul": 1.3, "crit": false, "label": "处决斩" },
      { "type": "self_corruption", "target": "player", "value": 2 },
      { "type": "enemy_died", "enemyId": "e-slime-0", "drops": {"starShards": 25} }
    ],
    "legalActions": [ /* kind 摘要 + 必要 id */ ],
    "outcome": null            // "win" | "lose" | "node_complete" | null
  }
}
```

### 9.4 `Observation` 契约（**白名单，唯一权威**）

**服务端只下发这些字段；其余一律不下发。**

```jsonc
{
  "schemaVersion": "obs-1.0.0",
  "sessionId": "s-...",
  "runId": "r-...",
  "phase": "player_turn",           // player_turn|enemy_turn|map|shop|event|treasure|shrine|forge|settle
  "stage": 4,                        // 1..10
  "chapter": 2,
  "turn": 3,
  "difficulty": "medium",
  "player": {
    "hearts": 3, "heartsMax": 3,
    "virtue": 8, "virtueMax": 10,
    "ward": 4,
    "starShards": 120,
    "statuses": [ {"kind":"strength","stacks":2}, {"kind":"poison","stacks":3} ],
    "deckSchool": "blade",           // 用于 UI 展示"你被谁克制"
    "purifyLeft": 1,
    "abilities": [ {"id":"warrior_reroll","usesLeft":1} ]
  },
  "hand": [
    { "dieId":"d-0007", "defId":"skill_blade_execute", "school":"blade",
      "riskTier":"balanced", "faceIndex":2, "label":"处决斩 4",
      "kind":"purge", "value":4, "statusKind":null, "isCorrupt":false, "corruptionValue":0 }
  ],
  "slots": [
    { "slotId":"slot_1", "slotKind":"attack", "constraint":"ge:3",
      "filledBy":null, "effectKind":"purge", "bonus":0, "disabled":false }
  ],
  "enemies": [
    { "enemyId":"e-slime-0", "defId":"mon_verdant_slime", "name":"腐生史莱姆",
      "school":"verdant", "hp":14, "hpMax":20, "ward":0,
      "statuses": [ {"kind":"poison","stacks":2} ],
      "intent": [ {"kind":"purge","value":5,"school":"verdant","schoolMul":1.3,
                   "arrow":"up","slotId":"e_slot_0"} ],
      "phase": 1, "taunt": "自然 · 克秘法" }
  ],
  "map": {
    "rows": [ [ {"nodeId":"n-03","kind":"battle","school":"blade","visited":false,"reachable":true,"preview":{"enemyName":"锋刃幼体"}} ] ]
  },
  "shop": null,           // 或 {items:[{itemId,price,kind,defId,school}], refreshLeft}
  "event": null,          // 或 {eventId,title,text,options:[{optionId,label,requirement,affordable}]}
  "treasure": null,       // 或 {offers:[{defId,name,school,riskTier,faces}]}
  "toggles": { "mapBranching":true, "relics":true, "events":true, "shops":true, "schoolCounter":true },
  "relics": [ {"relicId":"relic_blade_mark","name":"锋刃印记","desc":"..."} ],
  "legalActionKinds": ["play_face","discard_die","use_ability","end_turn"],
  "seed": null            // 永不填充；字段保留以便审计侧显式置 null
}
```

**严禁出现在 `Observation` 中**：`seed`、RNG 流状态、未来骰面、未揭示的地图节点与内容、商店未展示商品、敌人未公开状态、`GameState` 全量、动作日志内部结构。
**审计用** `obsDigest`（`sha256` 规范化 JSON 摘要）可下发，用于与 `POST /api/replay` 对拍。

### 9.5 合法性校验与失败策略

| 层级 | 校验 | 失败处理 |
|---|---|---|
| L1 语法 | JSON 可解析、`action.kind` 在枚举内、必填字段存在 | `400 E_ACTION_SYNTAX`，**不改状态** |
| L2 语义 | 落在 `legalActions()` 内 | `200 + ok:false, E_ACTION_ILLEGAL` + `reason`，**不改状态**，`illegalCount += 1` |
| L3 执行 | 内核 `reduce` | 例外 → `500 E_KERNEL_*` + 状态回滚（内核纯函数天然回滚） |

- 连续 3 次 L2 失败 → 该会话标记 `suspended`（防死循环自动化客户端刷爆）。
- 每一步响应必须带 `obsDigest`，且**同一 state 的 `obsDigest` 必须稳定**。

---

## 10. 前端设计

### 10.1 页面与路由（hash 路由，无服务端路由）

| 路由 | 页面 | 关键要素 |
|---|---|---|
| `#/menu` | 主菜单 | 开始新局 / 继续（localStorage）/ 图鉴 / 设置 |
| `#/setup` | 开局配置 | 难度三选卡片、**开关五项**、种子输入、角色选择；由 `/api/config/schema` 动态渲染 |
| `#/map` | 地图 | 逐层节点、路径连线、节点预览（敌名 + 学派 + 克制箭头）、当前位、已走路径；`mapBranching=false` 时自动推进不显示 |
| `#/battle` | 战斗 | 见 §10.2 |
| `#/shop` | 商店 | 三栏陈列、价格、刷新、购买/删除/锻造 |
| `#/event` | 事件 | 叙事文本 + 选项 + 后果反馈 |
| `#/treasure` | 宝箱 | 骰子三选一（展示 6 个骰面 + 学派 + 风险色） |
| `#/shrine` | 神殿 | 二选一（回心 / 换遗物 / 拿币） |
| `#/forge` | 锻造 | 选骰 → 选面 → 5 选 1 替换 |
| `#/settle` | 结算 | 胜利/失败、统计（回合数、伤害、击杀、克制触发次数）、重开 |

### 10.2 战斗页布局（关键交互）

```
┌───────────────────────────────────────────────────────────────────────┐
│ 关 4/10 · 第 3 回合 · [自然·克秘法]   ♥♥♡  净化 ████████░░ 8/10  盾 4  │
├───────────────────────────────────────┬───────────────────────────────┤
│ 敌方区                                 │  学派克制方阵（可展开）        │
│  [史莱姆 自然] HP ███████░░ 14/20      │     锋刃 → 自然 → 秘法 → 锋刃  │
│   意图: ⚔5(↑克你)  🛡3                │  你的主流派: 锋刃 (被秘法克)   │
│                                        │                               │
├───────────────────────────────────────┼───────────────────────────────┤
│ 技能槽区（拖放目标）                    │  遗物栏 [🜁][🜂][ ]            │
│  [槽1 ge:3 → 魔化]  [槽2 any → 光盾]   │                               │
│  [槽3 odd → 回复]   [槽4 any → 任意]   │  抽骰堆 12 · 弃骰堆 4          │
├───────────────────────────────────────┴───────────────────────────────┤
│ 手骰区                                                                 │
│  ⚀[锋刃]  ⚂[自然]  ⚄[秘法]  ⚅[锋刃·魔化!]                            │
│                        ↑ 魔化骰：必须打出/弃掉/净化才能结束回合         │
├───────────────────────────────────────────────────────────────────────┤
│ [结束回合]  [能力: 重掷 (1)]  [净化 (1)]        伤害预览: 5 ×1.3 = 6    │
└───────────────────────────────────────────────────────────────────────┘
```

**必须实现的交互细节**：
1. **拖放（含点击等价路径）**：拖骰到槽；也支持"先点骰再点槽"（无障碍 & 移动端）。
2. **实时伤害预览**：悬停骰+槽组合时，显示 `base × schoolMul = final`，并标出克制来源（`↑克`/`↓被克`）；这是让克制机制**可学习**的关键 UI。
3. **非法选择反馈**：不满足约束的槽在拖拽中变灰 + 显示原因（`需要 ≥3`）。
4. **魔化骰高亮**：红色脉冲描边；「结束回合」在残留时置灰并显示 `还有 N 颗魔化骰未处理`，点击时高亮残留物。
5. **意图学派箭头**：`↑`(红，克你) / `↓`(绿，被你克) / `=`(灰)。
6. **事件动画队列**：`events[]` 按序播放（伤害飘字、盾破碎、击杀特效），动画期间禁用输入；提供「跳过动画」。
7. **键盘**：`1–8` 选骰、`Q/W/E/R` 选槽、`空格` 结束回合、`Esc` 取消。
8. **伤害数字分级**：普通 / 克制（放大 + 金色）/ 被克（缩小 + 灰色）/ 自魔化（紫色）。

### 10.3 美术资产清单（实现者先用占位符）

| 类型 | 数量 | 尺寸契约 | 占位方案 |
|---|---|---|---|
| 骰子图标 | 学派 4 色 × 风险 4 级 | 64×64 | CSS 圆角方块 + 字母 |
| 骰面图标 | 每 kind 一枚（10） | 48×48 | 内联 SVG symbol |
| 怪物立绘 | 每只 1 张 | 256×256, 透明 PNG | 彩色渐变圆 + 符号 |
| 学派徽记 | 4 | 32×32 | SVG |
| 遗物图标 | 每个 1 张 | 64×64 | emoji |
| 槽位框 | 5 种 slotKind | 96×96 | CSS border-image |
| 图标集 | ~30（心/盾/毒/火/虚弱/金币/骰堆…） | 24×24 | 内联 SVG sprite |

> 全部资产通过 `frontend/assets/manifest.json` 声明；缺失资产**必须降级到占位符而不是报错**。

### 10.4 音效触发点清单（资产可后补）

`dice_roll` / `dice_place` / `hit_normal` / `hit_counter`（克制命中，音调更高）/ `hit_weak` / `ward_gain` / `ward_break` / `self_corruption`（低频刺耳）/ `enemy_die` / `victory` / `defeat` / `ui_click` / `ui_deny`。

---

## 11. 目录结构与技术约定

```
/                                   # 项目根（本游戏独立子目录，见下方说明）
  server.js                         # HTTP 入口（静态托管 + API 路由），零 npm 依赖
  backend/
    api.js                          # 路由与序列化
    session.js                      # 会话表（内存 Map + TTL 清理）
    validate.js                     # L1/L2 校验
    errors.js                       # 错误码集中登记
  kernel/                           # ★ 纯函数规则内核（唯一规则真相）
    index.js                        # createGame / reduce / observe / legalActions / configHash
    state.js                        # GameState 构造与不变式断言
    rng.js                          # SplitMix64 + 命名流
    counter.js                      # 学派克制方阵
    combat.js                       # 伤害结算、状态、死亡
    slots.js                        # 槽约束 DSL、放置校验
    map.js                          # 章节/关卡/地图生成
    shop.js  event.js  relic.js  forge.js
    loader.js                       # 配表加载 + 校验 + 开关过滤（唯一可读文件处）
    digest.js                       # 规范化 JSON + sha256
  content/                          # 纯数据配表（JSON）
    dice.json  skills.json  monsters.json  stages.json
    events.json  relics.json  shops.json  difficulty.json  schools.json
  frontend/
    index.html
    css/  (base, layout, battle, dice, map, shop, anim)
    js/   (main, router, state, net, render/*, anim, audio, i18n, debug)
    assets/ (manifest.json + 占位图)
  tests/
    kernel/*.test.js                # 内核单测（node:test）
    golden/*.jsonl                  # 固定 seed 的动作脚本 + obsDigest 快照
    replay.test.js                  # 重放一致性
  scripts/
    run.sh                          # 一键启动
    balance.js                      # 平衡脚本：跑 N 局输出通关率
  docs/                             # 本 PRD 与子文档
```

**技术约定**：

| 约定 | 要求 |
|---|---|
| 运行时 | Node ≥ 18（只用 `node:` 内置模块）；浏览器 Chrome/Edge/Firefox/Safari 最近两个大版本 |
| 模块 | `"type": "module"`，全 ESM |
| 依赖 | **npm 依赖数 = 0**（测试用 `node:test`） |
| 命名 | 文件 `kebab-case.js`；类型 `PascalCase`；函数 `camelCase`；配表 ID `snake_case`；常量 `SCREAMING_SNAKE` |
| ID 格式 | 配表 ID 正则 `^[a-z][a-z0-9_]*$`；运行时实例 ID `^(d|e|n|s|r)-[0-9a-zA-Z_-]+$` |
| 数字 | 全部**整数**运算；禁止在规则中使用浮点（倍率用 `mulPermille` 千分整数，如 1300 = ×1.3，最终 `floor(x*m/1000)`） |
| 时间 | 内核禁止 `Date.now()`/`performance.now()`；动画计时只允许在 `frontend/` 与 `backend/` |
| 随机 | 内核禁止 `Math.random()`；CI 用 AST/正则扫描 `kernel/` 命中即失败 |
| 文件读写 | 仅 `kernel/loader.js`、`backend/`、`scripts/` 允许；其余模块禁止 `node:fs` |
| `content/*.json` | 形状由 `kernel/loader.js` 校验，含 `$schemaVersion` 字段；schema 见 GAME-02 |

---

## 12. 非功能需求

| ID | 需求 |
|---|---|
| NFR-01 | **确定性**：同 `(seed, config, actions[])` 在任意机器、任意 Node 版本下产生逐字段相同的 `Observation` 序列与 `obsDigest` 序列 |
| NFR-02 | **性能**：内核 `reduce` p95 ≤ 0.5 ms；单场战斗（≤60 回合）全流程 ≤ 20 ms；完整 Run 的纯计算 ≤ 200 ms |
| NFR-03 | **响应**：`/api/game/:sid/action` p95 ≤ 30 ms（本机）；首屏可交互 ≤ 1 s |
| NFR-04 | **无泄漏**：§9.4 的禁止字段在 API 响应中 0 出现（自动测试用黑名单字符串扫描 + 字段白名单双向校验） |
| NFR-05 | **可观测**：每步写一行 JSONL 到 `runs/<runId>/actions.jsonl`（含 `obsDigest`、`action`、`events`、`ts`），格式见 GAME-01 §6 |
| NFR-06 | **健壮**：后端任何未捕获异常返回结构化错误并保留会话可继续；内核不得让异常逃逸出 `reduce`（统一 `E_KERNEL_*`） |
| NFR-07 | **零依赖交付**：`git clone && node server.js` 即可运行；不要求 `npm install` |
| NFR-08 | **可测**：内核单测覆盖 ≥ 80%；golden 快照 ≥ 3 组（含 1 组全开关关闭的"纯战斗"配置） |
| NFR-09 | **配置即文档**：`GET /api/config/schema` 必须包含全部难度键、开关、枚举及其默认值与说明文案，前端不得硬编码任何难度数值 |

---

## 13. 验收标准（DoD）

| 门 | 标准 |
|---|---|
| A1 可运行 | `node server.js` → 浏览器打开 → 从主菜单进入战斗，无 console 报错；`node --test` 全绿 |
| A2 通关 | 人工在 easy 与 medium 各通关一次（第 1–10 关）；hard 至少到达第 8 关 |
| A3 难度 | §7.1 全部配置键生效；三难度差异在 UI 可见（心数/抽骰数/克制强度提示） |
| A4 关卡 | ≥10 关；Boss 关 = 第 3/6/10 关（共 3 个，第 10 关三阶段三学派）；精英战斗覆盖 easy[3,6] / medium[3,6,9] / hard[3,5,7,9] |
| A5 流派 | ≥3 技能骰学派，每系 ≥4 颗技能骰；三系各通关一次；`skill_*` 使用次数在胜局中 ≥ 8 |
| A6 克制 | 同一敌人在"克制学派构筑"与"被克学派构筑"下胜率差 ≥ 20 个百分点（`scripts/balance.js` 出数） |
| A7 开关 | 5 开关 × {全关, 全开, 单开} 共 ≥ 7 种组合，每种可开局并通过前 3 关；`relics=false` 时无任何遗物 UI 与产出 |
| A8 确定性 | `POST /api/replay` 与在线对局的 `obsDigest` 序列逐行一致（≥ 3 个固定 seed 的 golden 快照） |
| A9 无泄漏 | 自动测试：响应 JSON 中不含 `seed` 实际值、`rng`、`node_kinds` 等黑名单字段；`obsDigest` 之外无可反推隐藏信息的字段 |
| A10 平衡 | `scripts/balance.js` 200 局/难度，通关率落在 §7.2 目标区间内 |
| A11 文档 | README 含 3 条可复制命令（启动/测试/平衡）；本文档与实际的偏差必须回写 |

---

## 14. 测试计划

| 用例 | 给定 | 当 | 则 |
|---|---|---|---|
| T-01 骰面索引 | 任意骰 `defId` | 掷出 pip = p | 取 `faces[p-1]`；6 面全被覆盖；`faceIndex ∈ 0..5` |
| T-02 约束 DSL | 12 条 `(constraint, pip, expected)` 表 | `checkConstraint` | 全部命中；非法文本抛 `E_KERNEL_CONSTRAINT_SYNTAX` |
| T-03 结算顺序 | 目标 `ward=5, hp=10, dmg=3`；`ward=2, dmg=7`；`hp=4, ward=0, dmg=4` | 攻击结算 | 分别 `(ward=2,hp=10)`、`(ward=0,hp=5)`、击杀；**血量绝不先于光盾减少** |
| T-04 克制方阵 | 9 组 `(atkSchool, defSchool)` | `schoolMul` | 与 §5.1 表完全一致；`counterIntensity=off` 时恒 1000 |
| T-05 魔化骰约束 | 手骰含 1 颗魔化骰 | `end_turn` | 非法 `E_ACTION_ILLEGAL/corrupt_die_remains`；`discard_die` 后合法 |
| T-06 弃骰代价 | 魔化骰 `value=4` | `discard_die` | 玩家 `virtue -= 2`（50% 上取整）；`purify` 路径为 0 |
| T-07 开关矩阵 | 5 开关的 7 种组合 | `createGame` + 走完前 3 关（脚本化） | 均能完成；全关时地图为线性，无事件/商店/遗物节点 |
| T-08 难度矩阵 | 3 难度同 seed | 创建游戏并读 `observe()` | 心数/抽骰数/敌血按 §7.1；**`rng` 流消费步数在关掉倍率后一致** |
| T-09 确定性/golden | `tests/golden/*.jsonl`（3 组） | 重放动作脚本 | `obsDigest` 序列逐行相等；跨 `TZ`/`LANG` 环境一致 |
| T-10 无泄漏 | 一局完整对局的全部 API 响应 | 黑名单扫描 | 0 命中；且响应字段集合 ⊆ §9.4 白名单 |
| T-11 老板阶段 | 第 10 关 Boss | 打到 67%/34% 阈值 | 学派与 AI 模板切换；意图刷新且可见 |
| T-12 平衡 | `scripts/balance.js --n 200` | 三难度各跑 | 通关率在目标区间；输出每学派构筑的胜率表 |
| T-13 前端交互 | 战斗页 | 拖放/点击/键盘三种路径 + 结束回合禁用 | 行为一致；无非法状态提交 |

---

## 15. 实施计划与工作量估算（人日）

| # | 阶段 | 产出 | 人日 |
|---|---|---|---|
| 1 | 内核骨架：state / rng / digest / 配表 loader + 单测 | `kernel/{state,rng,digest,loader}.js` | 2.0 |
| 2 | 战斗规则：骰面、槽、结算顺序、状态、胜负 | `kernel/{combat,slots}.js` + T-01..T-04 | 2.5 |
| 3 | 学派克制 + 魔化骰风险债 | `kernel/counter.js` + T-05/T-06 | 1.0 |
| 4 | 地图/关卡/难度/开关 | `kernel/{map}.js` + `content/{stages,difficulty}.json` + T-07/T-08 | 2.0 |
| 5 | 局内系统：商店/锻造/事件/宝箱/神殿/遗物 | `kernel/{shop,event,relic,forge}.js` + 配表 | 3.0 |
| 6 | 后端 API + 会话 + 校验 + 轨迹 | `server.js` + `backend/*` | 1.5 |
| 7 | 前端骨架：路由/主菜单/配置页/地图页 | `frontend/index.html` + `router/state/net` | 2.0 |
| 8 | 前端战斗页（拖放、预览、魔化骰、意图、动画队列） | `frontend/js/render/battle.js` | 4.0 |
| 9 | 前端其余页面（商店/事件/宝箱/神殿/锻造/结算） | `frontend/js/render/*` | 2.5 |
| 10 | 打磨：资产占位、音效钩子、键盘、localStorage、调试面板 | — | 2.0 |
| 11 | 平衡与验收：`scripts/balance.js`、golden、T-09..T-13、README | — | 2.5 |
| | **合计** | | **25.0** |

**关键路径**：1 → 2 → 3 → 4 → 6 → 7 → 8 → 11。第 5 项可与第 6/7 并行；第 9/10 可与第 11 交错。

**最小可玩切片（MVS，建议 1 天内先跑通）**：内核 1+2+3 + 后端 6 + 前端 7+8（单关单怪）→ 验证"能打完一场有克制关系的战斗"。**这个切片跑通后再扩展到 10 关。**

---

## 16. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 学派克制把游戏变成"唯一正确答案"（无博弈） | 要求 4 落空 | ① 敌人学派在开战前公开 ② 克制只 ×1.3 而非 ×2 ③ 单系集中度惩罚（`monoPenalty`）④ 保证每系有 1 面"打天敌"的通用面（§5.5 设计规则 2） |
| 魔化骰机制导致回合被"卡死" | 体验挫败 | 三条清除路径 + easy 给更多 `purify`；UI 明确提示；`handMax` 在 hard 降到 5 以平衡 |
| 内核与前端边界被侵蚀（规则漏到 UI） | 模拟器无法等价实现，RSI 数据报废 | CI 扫描 `frontend/` 内禁止出现伤害公式与合法性判定；`kernel/` 禁止 import 任何前端/后端模块 |
| 10 关 + 3 难度 + 5 开关组合爆炸，测试不全 | 隐藏死锁/崩溃 | T-07 开关矩阵 + T-08 难度矩阵 + `scripts/balance.js` 自动跑；任何新开关必须登记进矩阵 |
| 难度只调数值导致 hard 变成"更肉"而非"更聪明" | 体验单调 | hard 额外给敌人 `enemyExtraDice` 与更强 `counterIntensity`；Boss 增加阶段（§6.3） |
| 后续 MOD-01 移植时语义漂移（整数除法/取整/排序/字符串序） | 对拍失败、训练数据不可信 | 全部倍率用 `mulPermille` 整数；§11 规定"整型运算"；GAME-01 §7 给出逐条移植风险清单；`POST /api/replay` 作为永久对拍工具 |
| 零依赖限制导致开发效率低 | 进度慢 | 允许用 `node:test` 与原生 ESM；若确需工具链，仅在 `scripts/` 内放宽（不得进入 `frontend/`） |

---

## 17. 与现有文档的关系（需回填 Spec 的决策）

| # | 决策 | 冲突对象 | 建议处理 |
|---|---|---|---|
| D-01 | 克制倍率 `1300/770` 千分整数；`deckSchool` 定义 | 无（新增） | 直接写入 GAME 系列，作为游戏规则 SSoT |
| D-02 | 难度只经 `applyDifficulty(config)` 生效，业务逻辑不得分支 | MOD-01 D-04（难度只调 `enemy_hp_mul`/敌骰数） | **修订 MOD-01 D-04**：扩展为"难度只调配置"，与 §7.1 对齐 |
| D-03 | 游戏本体为前后端分离 Web 实现（HTML+JS 内核），**不是**纯 Python 模拟器 | Spec §3 MOD-01「纯 Python、无引擎依赖」、Plan §4「环境：纯 Python」、INV-1/INV-6 的实现方式 | **回填 Spec §2/§3**：新增 MOD-11 `game-web`（游戏本体），并声明 MOD-01 = 该本体的**纯 Python 等价移植**；INV-6 保持不变（内核禁 `Math.random`），INV-1 在 Web 场景下由 API 白名单（§9.4）承担 |
| D-04 | 观测白名单 `obs-1.0.0` 与 MOD-02 的 `OBSERVATION_SCHEMA` 命名分离 | MOD-02 §4.2 | 建议：**合并为同一契约**，字段名以本 PRD §9.4 为游戏侧基准，MOD-02 增补 agent 侧字段（`episode_id`、`allowed_actions`）后再投影 |
| D-05 | 玩家 `virtue` 归零且仍有心 → 本场战斗判负、失去 1 心、回地图（战斗节点视为已过） | MOD-01 §8（`outcome="lose"` 为整局失败） | 建议：MOD-01 保留"单场战斗"粒度，把"失心"上移到 MOD-06 的 episode 层 |
| D-06 | 五系统前置开关（`mapBranching`/`relics`/`events`/`shops`/`schoolCounter`） | 无（新增） | 新增到 Spec §5 配置契约：`configs/game.yaml`（或并入 `default.yaml` 的 `game:` 段），并规定"全关组合必须可通关"作为回归基线 |
| D-07 | 骰面风险分类（safe/balanced/risky/cursed）与学派为正交维度 | 无（新增） | 写入 GAME-02 配表 schema |
| D-08 | 玩家动作集 `play_face/discard_die/use_ability/end_turn` | Spec §4.3 动作契约（`place_die/end_turn/use_ability/shop_buy/shop_leave/levelup_choose`） | 建议：**统一为同一枚举**，游戏侧把 `shop_buy` 等节点动作并入；`discard_die` 为新增，需回填 Spec §4.3 |

### 17.1 实现阶段补充决策（v1.1，2026-09-30 16:39 回写）

以下决策在实现中补充，完整理由见 `src/game/docs/DECISIONS.md`。

| # | 决策 | 影响文档 |
|---|---|---|
| D-09 | 风险等级：不利面（自损面/`nothing` 面）0 → safe，1–2 → balanced，≥3 → risky | GAME-00 §5.3、GAME-01 §8 |
| D-10 | 技能骰学派规则：≥2 面属本系，且 ≥1 面属其他非中立学派 | GAME-02 §3、GAME-01 §8 |
| D-11 | 工具槽可重复使用；功能面（状态/抽骰/重掷/改骰/得币/无事/复制）只能进工具槽 | GAME-00 §4.3、GAME-02 §1.1 |
| D-12 | 虚弱在持有者回合开始时减半；力量不衰减 | GAME-01 §4.2 / §11 Q-3 |
| D-13 | 敌人意图骰贪心分配，每槽每回合至多触发一次 | GAME-00 §7.1、GAME-01 §4.2 |
| D-14 | 敌人塞入的厄咒骰仅本场战斗有效；事件塞入的永久 | GAME-02 §5 |
| D-15 | 每场战斗胜利：星屑 + 骰子 3 选 1；精英/Boss 另掉 1 遗物 | GAME-00 §4.1、GAME-02 §4 |
| D-16 | 战斗外"失/回净化"记为下一场战斗的初始净化修正；休息点改为"回 1 心 / 净化上限 +1" | GAME-02 §4 / §6 |
| D-17 | 敌人光盾在其自身回合开始时清零 | GAME-01 §4.2 |
| D-18 | Boss 关末行固定 1 个 Boss 节点；精英放置规则见 GAME-02 §4 | GAME-02 §4 |
| D-19 | `play_face` 增加可选 `targetId` | GAME-00 §4.2、GAME-01 §4 |
| D-20 | 蓄势与处决的具体数值规则 | GAME-00 §5.5 |
| D-21 | `duplicate_face`：再执行目标手骰当前面的正向效果 | GAME-02 §2.2 |
| D-22 | 单系惩罚需学派骰 ≥3 颗 | GAME-00 §6.2 |
| D-23 | 能力次数按"每场战斗"计 | GAME-02 §1.1 |
| D-24 | `maxTurnsPerBattle = 60` | GAME-01 §1.2 |
| D-25 | `runId` 由后端随机生成；`/api/replay` 接受 `config.runId`；`sessionId` 放响应外层 | GAME-00 §9 |
| D-26 | 内核 API 显式接收 `tables` 作为最后一个参数 | GAME-01 §1 |
| D-27 | 地图揭示 `row ≤ max(currentRow,0)+1` | GAME-01 §5.3 |
| D-28 | 遗物栏满 → `relic_swap` 阶段 | GAME-02 §7 |
| D-29 | 第 1–2 关奖励只出安全骰，不显示学派盲区提示 | GAME-00 §4.4 |
| D-30 | 玩家中毒/灼烧非致死；敌人中毒可致死 | GAME-01 §4.2 |
| D-31 | 动作全集新增 `purify` / `rest_choose` / `relic_swap` | GAME-01 §4 |
| D-32 | 新增编队 `g_12`（空壳，第 3–6 关） | GAME-02 §5.4 |
| D-33 | 启动方式为 `node server.js`（ADR-2 要求服务端持有会话，与纯静态服务器冲突） | GAME-00 §0 / G-1 |
| D-34 | （v1.2）开局配置新增可选 `stageCount ∈ {3,6,10}`（只能在章末 Boss 关截断），击败第 `stageCount` 关 Boss 即 `outcome="win"`；缺省 10，原 golden 不变。用途：RSI 训练/评估用短局（Spec §8.6），hard×6 关启发式通关率 45.5%、每局 p50 232 env 步 / 96 个决策点（n=200，seed 1–200，口径与复现命令见 Spec §8.6） | GAME-00 §8.2 / GAME-01 §1.2 |
| D-35 | （v1.2）修复：Boss 在玩家出骰时被击杀而直接结束 Run 的路径漏记 `stats.stagesCleared`，现与节点结算路径一致（胜局恒有 `stagesCleared == stageCount`）；`stagesCleared` 是 Spec INV-7 的主指标之一 | GAME-01 §5 |

> 建议新增 Spec 模块 **MOD-11 `game-web`**（L0 之外的独立层：游戏本体），其 PRD 即本文档。若采纳，Spec §2 模块清单、§6 依赖矩阵、§10 PRD 索引需同步更新。

---

## 18. 参考资料

| 用途 | 链接 |
|---|---|
| 骰子风险分类与"魔化骰必须处理"机制的原始参考 | [《Astrea: Six-Sided Oracles》给新玩家的基本攻略（Steam 社区）](https://steamcommunity.com/sharedfiles/filedetails/?l=schinese&id=3048666999) |
| 同上（英文新手向） | [Beginner Tips For Astrea: Six-Sided Oracles（TheGamer）](https://www.thegamer.com/astrea-six-sided-oracles-beginner-tips-guide/) |
| 游戏本体 | [Astrea: Six-Sided Oracles（Steam）](https://store.steampowered.com/app/1755830) |
| 骰子+技能槽规则的原始设计依据 | [Dicey Dungeons（维基百科）](https://en.wikipedia.org/wiki/Dicey_Dungeons) |
| 项目定位与沙箱纪律 | [项目计划-DiceRatchet.md](../../项目计划-DiceRatchet.md) |
| 模块边界与不变量 | [模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md) |
| 未来纯 Python 模拟器规格 | [MOD-01-env-rules-PRD.md](MOD-01-env-rules-PRD.md) |

---

## 19. 附：给实现 Agent 的执行摘要（可直接作为提示词）

> 你要实现一个**浏览器里可玩的骰子 roguelike**，规则见本 PRD。
>
> **硬性要求**：
> 1. 三层：`kernel/`（纯函数规则内核，零 IO/DOM/时间/随机）、`backend/`（HTTP 薄壳）、`frontend/`（只渲染与收集输入）。规则**只允许**存在 `kernel/`。
> 2. 原生 ESM，**零 npm 依赖**，`node server.js` 启动，浏览器 `http://127.0.0.1:8080` 打开即玩。
> 3. `kernel/` 用自实现 SplitMix64 + 命名流，禁 `Math.random()`；全整数运算，倍率用千分整数。
> 4. 实现 `createGame / reduce / observe / legalActions / configHash / digest`；`POST /api/replay` 必须与在线对局逐行 `obsDigest` 一致。
> 5. 10 关（3 章，第 3/6/10 关为 Boss/精英），3 难度（easy/medium/hard，差异**只在配置**），5 个前置开关（`mapBranching`/`relics`/`events`/`shops`/`schoolCounter`）。
> 6. 骰子分 `normal` 与 `skill`；技能骰至少 3 个学派（blade/arcana/verdant）+ 通用；骰面按风险分 safe/balanced/risky/cursed；**魔化骰（selfCorruption）未处理则不能结束回合**，三条清除路径各有代价。
> 7. 怪物有学派，与玩家骰面**双向克制**（×1.3 / ×0.77，按难度可调），UI 实时显示 `base × mul = final` 与克制箭头；单一学派集中度 ≥70% 会受到惩罚。
> 8. 先跑通**最小可玩切片**（1 场有克制关系的战斗）再扩展；`node --test` 覆盖 ≥80%；产出 3 组 golden 快照（含 1 组全开关关闭）。
> 9. 任何本文档未覆盖的规则细节，**先在文档里补一条决策再实现**，不要默默发明规则。
> 10. 资产用占位符（CSS/emoji/内联 SVG），不得因缺资产而报错。

---

## 20. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版 |
| v1.1 | 2026-09-30 16:39 | 按 `src/game` 实现回写：§0/G-1 启动方式改为 `node server.js`；§4.2 动作全集补充 `targetId` 与 `purify`；§4.3 工具槽可重复使用；§5.3 风险等级阈值修订；§5.5 蓄势/处决规则具体化；§6.2 单系惩罚最小骰数；§7.1 `virtueMax`/`enemyHpMul`/`enemyDmgMul` 平衡标定并附通关率；§17.1 登记实现决策 D-09～D-33 |
| v1.2 | 2026-09-30 17:21 | 为 RSI 训练（Spec v1.3 §8.6）补充：§8.2 新增可选 `stageCount`（D-34）；修复胜局 `stagesCleared` 漏记（D-35）。内核测试 48/48，golden 未变 |
| v1.3 | 2026-10-17 | **与实现对齐（纯文档，¥0）**：技能骰总数 15 → **17**（`src/game/content/skills.json` 实测 17 条：blade 5 / arcana 5 / verdant 5 / common 2）。内核测试现状以 `node --test` 实测为准（49 项通过 / 5 组 golden）。 |
