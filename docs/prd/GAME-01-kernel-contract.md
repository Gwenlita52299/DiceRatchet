# GAME-01 规则内核契约（Kernel Contract）

> 版本：v1.2（2026-09-30 17:21 补充局长选项，见 §12）· 状态：待评审 · 上游：[GAME-00-webgame-PRD.md](GAME-00-webgame-PRD.md)
> 定位：定义**规则内核的唯一接口与确定性语义**。这份文档的存在目的只有一个——
> **让"网页版游戏"与"未来纯 Python 模拟器（MOD-01）"能逐字段对拍**，从而使 RSI 训练数据可信；
> 以及让"视觉模型直接对游戏做 RSI"这一路线在接口上可行（§5.4）。
> 与 GAME-00 冲突时以 GAME-00 为准；本文件负责把 GAME-00 的规则**精确化到可实现、可对拍**。

---

## 0. 五条内核纪律（违反即 P0）

| # | 纪律 | 检查方式 |
|---|---|---|
| K-1 | **纯函数**：`reduce(state, action) → {state, events}`，不得修改入参，不得有模块级可变状态 | 单测冻结入参；`Object.freeze` 深冻结断言 |
| K-2 | **零环境依赖**：`kernel/` 内禁止 `node:fs`、`fetch`、`Date`、`performance`、`process`、`Math.random`、`console` | CI 正则/AST 扫描，命中即失败 |
| K-3 | **零浮点**：规则内所有数值为 `int`；倍率用千分整数 `mulPermille` | CI 扫描 `kernel/` 中的小数点字面量与 `/`（仅允许 `Math.floor(x/y)` 形式） |
| K-4 | **显式随机**：随机只来自 `rng.js` 的命名流，流状态是 `state` 的一部分 | golden 对拍 |
| K-5 | **观察白名单**：隐藏信息只存在于 `state`，`observe()` 是唯一出口，`digest()` 是唯一摘要 | T-10 黑名单 + 字段白名单双向校验 |

---

## 1. 内核模块清单与签名

```js
// kernel/index.js —— 全部对外 API（其余模块均为内部实现）
// v1.1（D-26）：内核不持有模块级状态，所有 API 显式接收 tables 作为最后一个参数
export function createGame(config, tables) -> GameState
export function reduce(state, action, tables) -> { state: GameState, events: GameEvent[], illegal: boolean }
export function observe(state, view = "full", tables) -> Observation
export function legalActions(state, tables) -> Action[]
export function isLegal(state, action, tables) -> { legal: boolean, reason: string|null, code: string|null }
export function playHints(state, tables) -> { [dieId]: { [slotId]: {legal, reason, previews} } }  // UI 伤害预览/非法原因（v1.1）
export function configHash(config) -> string          // "sha256:<16hex>"
export function digest(value) -> string               // "sha256:<64hex>"，规范化 JSON
export function applyDifficulty(baseConfig, difficulty, tables) -> GameConfig
export function validateConfig(config, tables) -> { ok: boolean, errors: string[] }
export function replay(config, actions, tables) -> { digests: string[], outcome: string|null }
```

> `replay` 是**便利函数**（`createGame` + 顺序 `reduce`），不属于新语义；它在 `kernel/` 内实现，用于后端 `/api/replay` 与测试。

### 1.1 `tables`（配表）注入契约

- 内核**不读文件**。`tables` 是 `kernel/loader.js` 产出的**冻结对象**：
  ```js
  { schemaVersion, schools, dice, skills, monsters, stages, events, relics, shops, difficulty }
  ```
- 每个配表项以 `id` 为键（对象映射而非数组），顺序无关。**任何依赖配表数组顺序的逻辑都是缺陷**（对拍必炸）。
- `loader.js` 职责：读文件 → 校验 schema → 按 `config.toggles` 过滤 → 冻结 → 建索引（`byId`、按学派/按 tier 分组）。

### 1.2 `GameConfig`

```jsonc
{
  "configVersion": "cfg-1",
  "difficulty": "medium",              // easy | medium | hard（仅用于可读性与日志；数值已由 applyDifficulty 展开）
  "seed": 12345,                        // int, 0 <= seed < 2^32
  "character": "warrior",
  "toggles": {
    "mapBranching": true, "relics": true, "events": true, "shops": true, "schoolCounter": true
  },
  // ↓ 以下全部由 applyDifficulty 从 difficulty.json 展开，落进 config 并参与 configHash
  "heartsMax": 3, "virtueMax": 10, "drawPerTurn": 4, "handMax": 8,
  "enemyHpMulPermille": 1000, "enemyDmgMulPermille": 1000, "enemyExtraDice": 0,
  "counterMulPermille": { "advantage": 1300, "disadvantage": 770 },
  "monoPenaltyPermille": 150, "monoThresholdPermille": 700,
  "shopPriceMulPermille": 1000, "goldMulPermille": 1000,
  "relicSlots": 3, "purifyPerStage": 1, "eliteStages": [3, 6, 9],
  "bossPhaseHpBonusPermille": 0, "reviveOnce": false,
  "stageCount": 10, "maxTurnsPerBattle": 60   // v1.1：由 200 改为 60（D-24）；v1.2：stageCount 可由开局输入覆盖为 3|6|10（D-34，validateConfig 拒绝其他值）
}
```

**规则**：`config` 是**完全展开的**——不允许"运行期再查 `difficulty.json`"。`difficulty` 字段仅作标识。这样 `configHash` 能唯一确定一整局的所有数值口径。

---

## 2. `GameState` 数据模型

```jsonc
{
  "stateVersion": "gs-1",
  "runId": "r-000001",
  "configHash": "sha256:...",
  "config": { /* §1.2 全文 */ },
  "stage": 4, "chapter": 2, "turn": 3, "steps": 120,
  "phase": "player_turn",
  // —— 玩家 ——
  "player": {
    "hearts": 3, "heartsMax": 3, "virtue": 8, "virtueMax": 10, "ward": 4,
    "starShards": 120, "statuses": [ {"kind":"strength","stacks":2} ],
    "purifyLeft": 1,
    "abilities": [ {"id":"warrior_reroll","usesLeft":1} ],
    "deck": ["die-0001","die-0002"],              // 骰池：dieId 列表（顺序稳定，入池序）
    "drawPile": ["die-0003"], "discardPile": [], "hand": ["die-0007"],
    "dice": {                                       // 实例表
      "die-0007": { "defId":"skill_blade_execute","faceIndex":2,"isCorrupt":false,"owner":"player" }
    },
    "slots": [ {"slotId":"slot_1","slotKind":"attack","constraint":"ge:3","filledBy":null,"bonus":0,"disabledUntilTurn":null} ],
    "relics": ["relic_blade_mark"],
    "deckSchool": "blade"
  },
  // —— 敌人 ——
  "enemies": [
    { "enemyId":"e-0","defId":"mon_verdant_slime","hp":14,"hpMax":20,"ward":0,
      "school":"verdant","phase":1,
      "statuses":[{"kind":"poison","stacks":2}],
      "slots":[{"slotId":"e_slot_0","constraint":"any","filledBy":null}],
      "intent":[{"slotId":"e_slot_0","pip":5,"school":"verdant","plannedFaceIndex":2,"committed":true}],
      "aiState": {} }
  ],
  // —— 地图/节点 ——
  "map": {
    "rows": [ [ {"nodeId":"n-03","kind":"battle","enemyGroupId":"g-04","visited":false} ] ],
    "currentRow": 2, "currentNodeId": "n-03", "path": ["n-01","n-03"]
  },
  "node": { "kind":"battle", "state":{} },          // 当前节点内部状态（商店陈列/事件进度/宝箱候选）
  // —— 随机 ——
  "rng": [ {"name":"map","counter":37,"s0":123,"s1":456}, ... ],   // 按 name 升序
  // —— 审计 ——
  "illegalCount": 0, "consecutiveIllegal": 0,
  "outcome": null,                                   // "win"|"lose"|null
  "truncationReason": null
}
```

**不变式（`state.js` 每次 `reduce` 后断言，仅在 `debug` 模式开启）**：

1. `hand.length <= config.handMax`。
2. `deck = drawPile ∪ discardPile ∪ hand ∪ {已消耗}`，无重复、无遗失。
3. 每颗骰子的 `faceIndex ∈ 0..5`；`isCorrupt=true` ⟺ 本回合打出过 `selfCorruption` 面。
4. `rng` 数组长度恰为流名全集，且按 `name` 升序。
5. 战斗阶段的 `sum(intent)` 与敌 `slots` 数量一致；`committed=true` 的意图在敌方回合内不得改变。
6. `rng[*].counter` 单调不减。
7. 无浮点数：递归扫描 `state`，任何 `typeof x === "number" && !Number.isInteger(x)` 即失败。

---

## 3. 确定性随机（`rng.js`）

### 3.1 命名流白名单

```js
export const STREAM_NAMES = Object.freeze([
  "map", "dice.player", "dice.enemy", "shop", "event", "loot", "levelup", "forge"
]);
```

未注册流名 → 抛 `E_KERNEL_RNG_STREAM_MISSING`。

### 3.2 派生（必须与 Python 侧对拍一致）

```
deriveSeed(rootSeed:int, streamName:string) -> u32
  JS  : xmur3(streamName) 得到 4 个 u32 作为种子材料，与 rootSeed 混合后 splitmix64 一次
  PY  : 同算法逐位实现（禁止用 hashlib，因为要与 JS 一致）
```
> **注意**：初版设计曾考虑 `blake2b`，但 Python 与 JS 实现 `blake2b` 需依赖库且存在编码歧义；**改用纯整数混合函数 `xmur3 + splitmix64`**，两侧各 15 行代码即可完全一致。这是本契约的**强制性**要求。

### 3.3 SplitMix64（用两个 u32 表示 u64，规避 JS 精度）

```js
// 内部：state 为 {hi, lo} 两个 u32（大端语义：value = hi*2^32 + lo）
// next() 返回 u32：取 splitmix64 结果的高 32 位（低 32 位丢弃）
// 关键：所有 64 位运算用 16 位分片乘法实现，禁止 BigInt（BigInt 在两侧可行但会引入 BigInt/Number 转换分歧）
export class RngStream {
  constructor(name, seed) { ... }             // state.counter = 0
  nextU32() -> u32
  randint(lo, hi) -> int                       // 闭区间 [lo,hi]，用拒绝采样消除模偏差
  choice(seq) -> element                        // seq.length 必须 > 0
  shuffle(seq) -> newSeq                        // Fisher–Yates，消费 n-1 次
  snapshot() -> {name, counter, s0, s1}
  static from(snapshot) -> RngStream
}
```

**算法约定（对拍关键）**：

| 项 | 约定 |
|---|---|
| `randint(lo,hi)` | `range = hi-lo+1`；`limit = floor(2^32 / range) * range`；循环 `x = nextU32()` 直到 `x < limit`；返回 `lo + (x % range)`。**必须用拒绝采样**，否则两侧取模一致性依赖同一 u32 序列——虽然一致，但拒绝采样便于将来扩展且无偏差。 |
| `choice(seq)` | 对**数组**用 `seq[randint(0, seq.length-1)]`；对**对象映射**先 `Object.keys(x).sort()` 再取（严禁依赖插入序）。 |
| `shuffle(seq)` | `for i from n-1 down to 1: j = randint(0, i); swap`。**方向必须是从后往前**。 |
| 流消费顺序 | 每个流**只被其负责的子系统消费**，消费点必须在 GAME-01 §4 的流表中显式登记。任何新消费点必须登记，否则对拍失效。 |

### 3.4 随机流消费登记表（唯一权威）

| 流 | 消费时机 | 消费内容 |
|---|---|---|
| `map` | `createGame` | 地图行数、每行节点数、节点类型、边连接、每节点敌人编队 id、商店/事件/宝箱的初选池 |
| `dice.player` | 战斗 `draw`+`roll` 阶段 | 抽骰堆洗牌（需要时）、每颗手骰的 `pip` |
| `dice.enemy` | 战斗 `roll` 阶段（**玩家回合开始前提交**） | 敌人 `intent[].pip` |
| `shop` | 进入商店节点 | 3 件商品、遗物、锻造候选面、刷新结果 |
| `event` | 进入事件节点 / 选项需要概率时 | 事件选取、概率结果判定 |
| `loot` | 战斗胜利 / 宝箱节点 | 金币数量、宝箱 3 选 1 候选、稀有度 |
| `levelup` | 章节结算（若启用） | 强化选项集合 |
| `forge` | 锻造节点 | 5 个候选骰面 |

> **魔化骰与 `intent` 的真实性硬约束**：敌人 `intent` 在**玩家回合开始时一次性提交**，敌方回合**不重掷、不重新规划**。这与 MOD-01 FR-01-06 完全一致——保证"意图"不是谎言（预读防护）。

---

## 4. `reduce` 分派表

`reduce(state, action)` 首先 `isLegal`，不合法则返回**原 state 引用 + 空 events + 非法事件**（由调用方决定是否计入 `illegalCount`）。

| `action.kind` | 合法 `phase` | 效果 |
|---|---|---|
| `play_face` | `player_turn` | 见 §4.1 |
| `discard_die` | `player_turn` | 骰入弃骰堆；若 `isCorrupt` → 玩家 `virtue -= ceil(face.value/2)` |
| `use_ability` | `player_turn` | 按 `abilities[]` 定义执行；`usesLeft -= 1` |
| `end_turn` | `player_turn` | 先校验"无残留魔化骰"，然后 `cleanup → enemy → cleanup → upkeep → draw → roll`（同一 `reduce` 内跑完整个敌方回合） |
| `purify` | `player_turn` | 清 1 颗魔化骰（不触发自魔化）；`purifyLeft -= 1` |
| `map_choose` | `map` | 选择 `currentRow+1` 的可达节点 |
| `shop_buy` | `shop` | 扣币、加骰/遗物、记已购 |
| `shop_refresh` | `shop` | 扣币、重新消费 `shop` 流生成陈列 |
| `shop_remove` | `shop` | 扣币、从 `deck` 移除指定骰 |
| `shop_leave` | `shop` / `forge` / `event` / `treasure` / `shrine` | 结束当前节点 |
| `event_choose` | `event` | 执行选项、按需消费 `event` 流 |
| `treasure_pick` | `treasure` | 3 选 1 加骰；或 `skip` 换币 |
| `forge_pick` | `forge` | 替换指定骰的指定面 |
| `shrine_choose` | `shrine` | 二/三选一 |
| `rest_choose` | `rest` | 休息点二选一：回 1 心 / 净化上限 +1（v1.1，D-16/D-31） |
| `relic_swap` | `relic_swap` | 遗物栏满时丢弃一个旧遗物换新遗物，或 `skip` 放弃新遗物（v1.1，D-28/D-31） |
| `abandon_run` | 任意 | 主动结束（`outcome="lose"`，标记 `reason="abandon"`） |

> **v1.1 补充（D-19）**：`play_face` 增加可选 `targetId`——攻击面/对敌状态面填敌人 ID，`reroll/modify/duplicate_face` 填另一颗手骰 ID；缺省时内核规范化为首个存活敌人 / 首个其他手骰。

**阶段推进的实现方式**：`end_turn` 是唯一会**跨阶段大步推进**的动作（因为它要跑完敌方整回合）。替代设计（每个子阶段一个 action）会让外部动作流变长且易被误用；本契约选择"`end_turn` 内聚"，但要求 `reduce` 在内部**按 §4.2 顺序**逐步执行并发出全部 `events`，且任一时刻 `state.rng` 的消费量与顺序都完全确定。

### 4.1 `play_face` 详细流程

```
play_face(dieId, slotId, faceIndex?)
 1. 校验 phase == player_turn                          → E_ACTION_PHASE
 2. 校验 dieId ∈ player.hand                             → E_ACTION_DIE_NOT_IN_HAND
 3. 该骰当前 faceIndex 由掷骰决定；若传 faceIndex 且 ≠ 当前值 → E_ACTION_FACE_MISMATCH
 4. 校验 slotId ∈ player.slots 且 !disabled               → E_ACTION_SLOT_DISABLED / UNKNOWN_SLOT
 5. 槽已占且非 reusable                                  → E_ACTION_SLOT_OCCUPIED
 6. 槽类型与骰面 kind 兼容（GAME-00 §4.3 表）             → E_ACTION_KIND_MISMATCH
 7. 约束校验 checkConstraint(slot.constraint, pip)        → E_ACTION_CONSTRAINT_VIOLATION
 8. 计算 final = floor(base * schoolMul/1000 * relicMul/1000)，攻击向最小 1
 9. 按 GAME-00 §5.4 结算顺序：光盾吸收 → 实伤 → 死亡判定
10. 写 filledBy、把骰移出 hand 进 discardPile
11. 若 face.kind == "selfCorruption"（或骰面含该分量）→ 该骰 isCorrupt = true（在弃骰堆中保持标记，
    玩家可用 purify 的时机为"打出前的手骰状态"）
12. 发 events：damage / ward_gain / mend / status_applied / self_corruption / enemy_died / player_lost_heart
```

> **魔化骰的语义澄清（实现必须一致）**：`isCorrupt` 标记表示"这颗骰的**当前骰面**含 `selfCorruption` 分量"。它只在对**手骰**进行 `end_turn` 前置检查与 `discard_die`/`purify` 时起作用。骰子进弃骰堆后标记清除（回合结束清空手骰时，未处理的魔化骰**已经**触发过"不能结束回合"，因此不存在"逃逸"）。
> 若一颗骰的**当前骰面**是 `selfCorruption`（`kind === "selfCorruption"`），`play_face` 会先施加自魔化再执行该面附带的正向分量（见 GAME-02 中 `dual` 复合面）。

### 4.2 `end_turn` 内部顺序（**硬约束，对拍关键**）

```
E1. 校验：不存在 isCorrupt 的手骰 / 槽内残留                → 否则 E_ACTION_CORRUPT_DIE_REMAINS
E2. 手骰全部入弃骰堆（清空 hand），清空 isCorrupt 标记
E3. 进入敌方回合（phase = "enemy_turn"）
E4. 对 enemies[] 按 enemyId 升序（字符串字典序）依次执行：
      E4-0. 该敌人回合开始：ward 清零（v1.1，D-17）→ 中毒/灼烧结算（可致死，D-30）→ 虚弱减半（D-12）→ 被动（回血/叠力量）
      E4a. 逐 slotId 升序处理已提交 intent
      E4b. 应用 enemyDmgMul、学派克制（源 = enemy.school，目标 = player.deckSchool）、单系惩罚，逐步 floor，最小 1
      E4c. 应用玩家 ward 吸收 → player.virtue 扣减
      E4d. 若 player.virtue <= 0 → 失心流程（§4.3），中断敌人行动
E5. 若战斗未结束：检查所有敌人 hp <= 0 → 胜利结算（node 结算，phase → 节点结算）
E6. （v1.1：敌方 ward 清零与状态结算已移至 E4-0，此步为空）
E7. 若战斗继续：turn += 1；phase = "player_turn"
E8. upkeep：玩家 ward 清零；poison/burn 结算（非致死至少留 1）；regen 结算；weak 减半（v1.1，D-12）
E9. 检查 player.virtue <= 0（upkeep 致死）→ 失心流程
E10. draw：抽 drawPerTurn 颗（洗牌消费 dice.player）；hand 超 handMax 的部分直接入弃骰堆
E11. roll：为 hand 中每颗骰消费 dice.player 取 pip → faceIndex
E12. 敌人重新提交 intent（消费 dice.enemy），marked committed
     （v1.1，D-13）意图骰降序贪心分配给敌槽（ai 决定槽优先级），每槽每回合至多触发一次；enemyExtraDice 只提高满足约束的概率
E13. 发出全部 events（顺序与 E1..E12 一致）
```

> **敌人行动顺序**用 `enemyId` 字符串字典序（`e-0`, `e-1`, `e-10`, `e-2`…）。**实现者注意**：如果编队用数字后缀，必须**零填充**（`e-00`, `e-01`, `e-10`）以保证字典序 == 数值序。这是对拍与 UI 顺序一致性的要求。

### 4.3 失心（`player_lost_heart`）流程

```
virtue <= 0                                   →  players_lost_heart
  若 config.reviveOnce 且 !reviveUsed:
     reviveUsed = true; virtue = floor(virtueMax/2); 战斗继续
  否则:
     hearts -= 1
     若 hearts > 0:
        本场战斗判负（战斗节点标记 cleared=true，不重复触发）
        virtue 回满；phase → "map"；本节点结束
     否则:
        outcome = "lose"；phase → "settle"
```

> **决策 D-05 的落地**：`state.outcome` 只在 `hearts <= 0` 时置 `"lose"`。单场战斗的失败是**节点级事件**，不是 run 级 `outcome`。这与 MOD-01（单场战斗粒度的 `outcome="lose"`）的差异登记在 GAME-00 §17 D-05。

---

## 5. `observe()` 投影

### 5.1 版本

`Observation.schemaVersion = "obs-1.0.0"`。字段**只能增**（新增可选字段，小版本 +1）；**删除或改语义必须大版本 +1 并记录 CHANGELOG**。

### 5.2 投影规则

- `observe(state, "full")` → GAME-00 §9.4 的完整白名单对象。
- **禁止透传**：实现必须是**显式构造**新对象（逐字段挑选），不得用对象展开 `{...state}` 或 `JSON.parse(JSON.stringify(state))` 后再删字段。CI 检查 `kernel/` 中 `observe` 函数体内不出现 `...state`。
- **数值口径**：`constraint` 以**文本**下发（让 UI 显示"需要 ≥3"），不解析成结构；`intent[].pip` 下发（玩家本就该看到意图骰面）。

### 5.3 黑名单（0 出现，自动测试）

`seed`（实际值）、`rng`、`config.seed`、`map.rows[*][*].enemyGroupId` 的**未揭示成员**、`node.state` 内部字段、`drawPile` 的**顺序**（只允许下发数量）、`discardPile` 顺序、未来行的节点内容（只允许下发"存在性 + 是否可达 + 预览种类"）、`stateVersion`/`state` 本体、`aiState`。

> `map.rows` 的揭示策略（v1.1 精确化为 `row ≤ max(currentRow, 0) + 1`，D-27）：**当前行与下一行完整下发**（含节点 kind 与 enemy 学派预览）；再往后的行只下发 `{count, reachable:false}` 占位。这把"预读"限制在 1 行之内，既保证可玩性也保证审计可控。

### 5.4 `view` 档位（为视觉模型路线预留）

| view | 内容 | 用途 |
|---|---|---|
| `"full"` | GAME-00 §9.4 全字段 | 人类 UI / 文本 agent |
| `"text"` | 全字段但数值以文本标签替代部分 id（便于 LLM 阅读） | 未来文本 agent |
| `"visual"` | **仅**：本帧应当渲染的实体列表（骰子位置与面值文本、敌人位置与血条比例、槽位占用、粒子事件队列），**不含** `deckSchool`、`counterMul`、`hpMax`、`deck` 等隐藏/推导信息 | 未来视觉模型：客户端渲染成图像，模型只看像素；此档定义"渲染输入"以保证渲染可复现 |

> `"visual"` 档是**前瞻性设计**，本版不实现渲染器，但 `observe` 必须接受该参数并返回符合上表的对象（可以先返回最小实现）。这样未来接视觉模型时不需要改内核。

---

## 6. 事件（`GameEvent`）与轨迹

### 6.1 `GameEvent` 契约

```jsonc
{ "type": "damage", "target": "e-0", "value": 6, "raw": 5,
  "school": "blade", "mulPermille": 1300, "relicMulPermille": 1000,
  "source": "play_face", "dieId": "d-0007", "slotId": "slot_1", "label": "处决斩" }
```

| type | 必备字段 | 说明 |
|---|---|---|
| `roll` | `dieIds[]`, `pips[]` | 掷骰 |
| `damage` | `target`, `value`, `raw`, `mulPermille` | 对敌魔化 |
| `self_corruption` | `target:"player"`, `value` | 自魔化 |
| `ward_gain` / `ward_absorb` | `target`, `value` | 光盾 |
| `mend` | `target`, `value` | 回净化 |
| `status_applied` | `target`, `statusKind`, `stacks` | 状态 |
| `enemy_died` | `enemyId`, `drops` | 击杀 |
| `player_lost_heart` | `heartsLeft` | 失心 |
| `turn_end` / `battle_end` / `node_enter` / `node_leave` | — | 流程节点 |
| `illegal` | `code`, `reason` | 非法动作 |
| `boss_phase` | `enemyId`, `phase`, `school` | 阶段切换 |

**约束**：`events[]` 是**展示与审计**用的，**不得**作为状态载体（客户端不得依据 events 自行推进状态）。`value` 必须是最终值（已含所有倍率），`raw` 是倍率前的值——UI 用 `raw × mul = value` 展示克制。

### 6.2 轨迹（`runs/<runId>/actions.jsonl`）

```jsonc
{"runId":"r-000001","configHash":"sha256:...","step":120,"phase":"player_turn","stage":4,"turn":3,
 "obsDigest":"sha256:...","action":{"kind":"play_face","dieId":"d-0007","slotId":"slot_1"},
 "events":[...],"rngDigest":"sha256:...","ts":"2026-09-30T12:00:00Z","illegal":false}
```

- `ts` 由 `backend/` 注入（**内核不产生时间**）。
- `rngDigest = digest(state.rng)`，用于快速定位对拍分歧点。
- 会话结束追加一行 `{"type":"run_end","outcome":"win","steps":431,"digests":"sha256:..."}`。

---

## 7. Python 移植清单（MOD-01 对拍必读）

移植时**语义差异风险点**逐条列出。每条都必须在 `tests/golden/` 的对拍中覆盖。

| # | 风险 | JS 行为 | Python 行为 | 强制约定 |
|---|---|---|---|---|
| P-1 | 整数除法 | `/` 得浮点 | `//` 得整数 | 内核**禁止** `/`；一律 `Math.floor(x / y)` ↔ `x // y`。**仅允许除数为正常数** |
| P-2 | 负数取整 | `Math.floor(-7/2) = -4` | `-7 // 2 = -4` | 一致；但 `Math.trunc` 语义不同，**禁止 `Math.trunc`/`int()` 截断** |
| P-3 | 取模 | `%` 结果符号随被除数 | 同 | 一致；但**所有取模操作数必须非负**（内核保证） |
| P-4 | u32 溢出 | 需 `>>> 0` 强制 | 需 `& 0xFFFFFFFF` | 每次位运算后**必须**显式掩码；约定函数 `u32(x)` |
| P-5 | 64 位乘法 | 需 16 位分片 | 原生大整数自动正确 | 用**同一套 16 位分片算法**写两侧，禁止 Python 直接用大整数（否则结果虽同但便于审阅一致性） |
| P-6 | 对象键排序 | `Object.keys` 默认整数字符串键**前置升序**（陷阱！） | `dict` 保插入序、`sorted()` 通用 | **禁止依赖任何语言的键序**；一律显式 `keys.sort()` 后用**码点序**（JS `sort()` 是 UTF-16 码元序，Python `sorted()` 是码点序 → 对 BMP 字符一致；**约定所有排序键局限在 ASCII**，`id` 正则已保证） |
| P-7 | 字符串比较 | UTF-16 码元序 | Unicode 码点序 | 同上：**所有排序/比较键限定 ASCII**（配表 id 校验保证） |
| P-8 | JSON 序列化 | `JSON.stringify` 保留插入序 | `json.dumps(sort_keys=True)` | **两侧都必须排序键**：JS 用 `stableStringify`（递归排序键）；Python `sort_keys=True, ensure_ascii=True, separators=(",",":")` |
| P-9 | 浮点表示 | 都是 IEEE754 双精度 | 同 | **内核无浮点**（K-3），从根本上消除 |
| P-10 | 随机种子类型 | `number`（双精度安全整数） | `int` | `seed` 限制 `< 2^32` |
| P-11 | 数组排序稳定性 | ES2019 起稳定 | `sorted` 稳定 | 两侧稳定；但仍要求**比较键唯一**（用 id 兜底） |
| P-12 | 表示缺失/null | `undefined` vs `null` | `None` | **内核禁止 `undefined`**：缺失一律 `null`；CI 扫描 `undefined` 字面量 |
| P-13 | 混合类型比较 | `"2" > 1` 隐式转换 | 抛 `TypeError` | 全字段强类型；`loader.js` 校验 schema |
| P-14 | 深浅拷贝 | 对象引用共享 | 同 | 内核必须**写时构造**新对象（结构化克隆式），不原地改；两侧用同一策略 |

**对拍流程**：

```
1. JS 侧：node scripts/golden-gen.js --seed 1042 --config <cfg> --script <actions.json>
   → tests/golden/<name>.jsonl  （每行 obsDigest + rngDigest + stateDigest(v1.2，全状态摘要) + action）
2. PY 侧：pytest tests/golden/  （MOD-01 实现 replay 并逐行比对）
3. 任一行不等 → 打印首个分歧点的 step、字段路径、两侧值；CI 失败
```

新增 golden 快照必须至少覆盖：
- 1 组 `toggles` 全关（纯战斗线性）
- 1 组 `toggles` 全开
- 1 组含 Boss 阶段切换
- 1 组含"魔化骰残留 → 非法 end_turn → discard → 再 end_turn"的完整路径
- 1 组含"失心 + reviveOnce"
- （v1.2）1 组 Spec §8.6 默认局 hard × `stageCount=6`（`g5_hard_six_stage`，seed 3100 不属于任何 split）

---

## 8. 配表校验规则（`loader.js` 必须实现）

| 校验 | 失败 |
|---|---|
| 每个 `id` 匹配 `^[a-z][a-z0-9_]*$` | `E_CONTENT_ID_FORMAT` |
| `dice/skills` 每颗**恰好 6 个面**，`faceIndex` 覆盖 0..5 无重复 | `E_CONTENT_FACES` |
| 每个 `face.kind` 在枚举内；`kind=status` 必有 `statusKind` | `E_CONTENT_FACE_KIND` |
| `riskTier` 与面统计**一致**（不手填，由 loader 计算并回写校验；阈值见 GAME-00 §5.3 v1.1 / D-09） | `E_CONTENT_RISK_MISMATCH` |
| 每个非中立 `skill`：≥2 面属本系学派，且 ≥1 面属其他非中立学派（v1.1，D-10） | `E_CONTENT_SCHOOL_RULE` |
| 每个 `monster` 的 `school` 在枚举内；`slots` 非空；`tier` 合法 | `E_CONTENT_MONSTER` |
| `stages` 覆盖 1..`stageCount`，第 10 关含 `boss` 节点 | `E_CONTENT_STAGES` |
| 每个 `event` ≥3 选项、≥1 正期望、≥1 风险选项 | `E_CONTENT_EVENT` |
| `relics` 中 ≥6 个学派联动、≥2 个代价型（用 `tags` 标记） | `E_CONTENT_RELIC_RULE` |
| 引用完整性：所有 `defId`/`itemId`/`relicId` 可解析 | `E_CONTENT_DANGLING_REF` |
| `toggles.relics=false` 时，`relics` 相关条目被过滤而不是报错 | warning + 过滤 |

---

## 9. 一致性测试向量（实现者必须让这些数值成立）

### 9.1 克制方阵（`counterMulPermille` = {1300, 770}）

| atk \ def | blade | arcana | verdant | neutral |
|---|---|---|---|---|
| blade | 1000 | 770 | 1300 | 1000 |
| arcana | 1300 | 1000 | 770 | 1000 |
| verdant | 770 | 1300 | 1000 | 1000 |
| neutral | 1000 | 1000 | 1000 | 1000 |

`toggles.schoolCounter=false` → 全表恒 1000。

### 9.2 伤害计算向量

| 用例 | base | atkSchool | defSchool | mul | relicMul | 期望 final |
|---|---|---|---|---|---|---|
| C1 | 5 | blade | verdant | 1300 | 1000 | `floor(5*1300/1000)=6` |
| C2 | 5 | blade | arcana | 770 | 1000 | `floor(5*770/1000)=3` |
| C3 | 4 | neutral | blade | 1000 | 1000 | 4 |
| C4 | 1 | blade | arcana | 770 | 1000 | `floor(0.77)=0` → **攻击向最小 1** ⇒ 1 |
| C5 | 6 | arcana | blade | 1300 | 1000 | `floor(7.8)=7` |
| C6 | 6 | blade | verdant | 1300 | 1250 | `floor(6*1300/1000=7.8→7)` 再 `floor(7*1250/1000=8.75→8)` ⇒ **8**（先学派后遗物，各自 floor） |
| C7 | 3 | verdant | arcana | 1300 | 1000 | 3 |

> **C6 是关键**：两次倍率**分别 floor**，不做一次合并乘。此约定必须写死在实现里。

### 9.3 魔化骰向量

| 用例 | 输入 | 期望 |
|---|---|---|
| M1 | 手骰含 1 颗 `isCorrupt`，其余无 | `end_turn` 返回 `E_ACTION_CORRUPT_DIE_REMAINS`，state 不变 |
| M2 | 同上，`discard_die` 该骰（面 `value=5`） | `player.virtue -= ceil(5/2) = 3` |
| M3 | 同上，`purify` 该骰 | `virtue` 不变，`purifyLeft -= 1` |
| M4 | `purifyLeft=0` 时 `purify` | `E_ACTION_NO_PURIFY` |
| M5 | 打出一颗 `kind=selfCorruption, value=4` 的面 | `virtue -= 4`（**不吃学派倍率、不吃光盾、不吃力量**） |
| M6 | 复合面 `purge 3 + selfCorruption 2` | 先 `selfCorruption 2` 后 `purge 3`（顺序固定：**负面先结算**，让"取舍"立刻可见） |

### 9.4 长度/边界

| 用例 | 期望 |
|---|---|
| `hand` 抽到超过 `handMax` | 超出部分直接入弃骰堆，`hand.length == handMax` |
| 抽骰堆空 | 弃骰堆洗入抽骰堆（消费 `dice.player` 的 `shuffle`）后再抽 |
| `maxTurnsPerBattle` 到达 | `truncationReason = "E_KERNEL_TURN_LIMIT"`，本场战斗判负但不失心（防护性截断） |
| 空 `slots` | 所有 `play_face` 非法；仍可 `end_turn`（用于防死锁测试） |
| `deck.length == 0` | `createGame` 拒绝：`E_KERNEL_EMPTY_DECK` |

---

## 10. 模块内部依赖（禁止反向依赖）

```
index.js
 ├─ state.js      （构造/断言/深冻结）
 ├─ rng.js        （无依赖）
 ├─ digest.js     （无依赖）
 ├─ counter.js    （依赖 tables.schools）
 ├─ slots.js      （依赖 digest? 否；依赖 counter? 否）
 ├─ combat.js     （依赖 counter, slots, rng）
 ├─ map.js        （依赖 rng）
 ├─ shop/event/relic/forge.js （依赖 rng, counter）
 └─ loader.js     （唯一允许 node:fs 的内核文件）
```

**禁止**：`combat.js` → `map.js`、`slots.js` → `combat.js`（循环）、任何 `kernel/*` → `backend/*` 或 `frontend/*`。

---

## 11. 开放问题

| # | 问题 | 现状 | 影响 |
|---|---|---|---|
| Q-1 | 复合骰面（一面上两个效果）是否纳入 v1？§9.3 M6 已给出顺序约定 | **纳入**，但配表 v1 只允许 `purge+selfCorruption` 一种复合 | 配表 schema、UI 显示 |
| Q-2 | `use_ability` 是否消耗回合？ | **已定（v1.1）：不消耗**（每场限次已足够约束） | 平衡；MOD-01 §16 待定项 3 |
| Q-3 | 状态 `weak`/`strength` 的衰减规则 | **已定（v1.1，D-12）：weak 在持有者回合开始时减半；strength 不衰减** | 长战斗平衡 |
| Q-4 | `handMax` 超限时是"丢弃最新抽到的"还是"玩家选择" | **已定（v1.1）：丢弃最新**（超出的骰子不掷、直接入弃骰堆） | UI 提示 |
| Q-5 | 多骰同时入槽（MOD-01 待定项 1） | v1 **不做** | 动作空间 |
| Q-6 | `"visual"` 档的具体渲染输入 schema | v1 只返回最小实现 | 未来视觉模型路线 |
| Q-7 | 敌人 `ai` 的 `boss_pattern` 如何声明 | 配表用 `phases[]` 声明，内核按 HP 阈值切换 | 配表 schema |
| Q-8 | 章节结算是否给永久强化（持久跨关成长） | 暂定**只给金币与骰子**，不做永久强化 | 与 `levelup` 流的关系 |

---

## 12. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版：内核 5 条纪律、API 签名、GameState、SplitMix64+命名流与消费登记表、reduce 分派与 end_turn 12 步顺序、观察投影与 3 个 view 档、事件与轨迹契约、14 条 Python 移植风险清单、配表校验、9 组一致性测试向量 |
| v1.1 | 2026-09-30 16:39 | 按 `src/game` 实现回写：§1 签名增加 `tables` 参数与 `playHints`（D-26）；§1.2 `maxTurnsPerBattle` 200→60（D-24）；§4 新增 `rest_choose`/`relic_swap` 与 `play_face.targetId`（D-19/D-31）；§4.2 敌人光盾/状态结算移至各自回合开始、虚弱减半、意图骰每槽至多触发一次（D-12/D-13/D-17/D-30）；§5.3 揭示规则精确化（D-27）；§8 风险与学派校验规则修订（D-09/D-10）；§11 Q-2/Q-3/Q-4 定案 |
| v1.2 | 2026-09-30 17:21 | §1.2 `stageCount` 可覆盖为章末 Boss 关（D-34，新增导出 `allowedStageCounts(tables)`）；最终 Boss 胜利两条路径都累加 `stats.stagesCleared`（D-35）；§7 golden 行新增 `stateDigest`，新增 `g5_hard_six_stage`（原 4 组其余字段不变） |
