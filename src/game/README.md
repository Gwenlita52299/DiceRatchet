# 破咒骰 · DiceRatchet 网页版骰子 roguelike

按 [`docs/prd/GAME-00`](../../docs/prd/GAME-00-webgame-PRD.md) / [`GAME-01`](../../docs/prd/GAME-01-kernel-contract.md) / [`GAME-02`](../../docs/prd/GAME-02-content-tables.md) 实现的浏览器骰子 roguelike：
掷骰 → 把骰面放进技能槽 → 用**学派克制**压制强敌；10 关 3 章、3 难度、5 个局内系统开关。

## 三条命令

```bash
node server.js                                          # 启动 → http://127.0.0.1:8080（--port 可改）
node --test "tests/**/*.test.js"                         # 全部测试（或 npm test）
node scripts/balance.js --n 200 --strategy all --duel    # 平衡：通关率 + 学派×敌人胜率矩阵
```

零 npm 依赖，需要 Node ≥ 18。其他脚本：`node scripts/golden-gen.js` 会重新生成 golden 快照（配表或规则变更后运行，并审阅 diff）。

## 玩法速览

- **骰子 → 技能槽**：每回合抽 4 颗骰子并掷出，把骰面拖进（或先点骰再点槽）装备的技能槽：攻击槽只收魔化面、防御槽收光盾面、恢复槽收修复面，工具槽什么面都收，而且可以重复使用。槽位还带点数约束，例如 `≥3`、`偶数`。
- **学派克制**：锋刃 → 自然 → 秘法 → 锋刃。你的攻击按骰面学派对敌人学派结算倍率。敌人则按你骰池的**主学派**反向克制你，单一学派堆叠会额外受罚。
- **魔化骰（风险债）**：掷出自损面的骰子必须在回合内处理，否则不能结束回合。处理方式有三种：放进工具槽（承受 100%）、弃掉（承受 50%）、净化（免费，次数有限）。
- **净化值 / 心**：净化值是战斗内血条，每场战斗回满；归零就失去 1 心，本场判负。心数归零则本局失败。
- 快捷键：`1–8` 选骰，`Q W E R T` 选槽，`空格` 结束回合，`D` 弃骰，`P` 净化，`Esc` 取消，右键骰子查看全部骰面。

## 架构（ADR-1：三层分离）

```
frontend/  静态 HTML + CSS + 原生 ESM：只渲染 Observation、收集输入、播放动画（无任何规则判定）
backend/   Node 原生 http 薄壳：路由、会话、L1 校验、轨迹日志 runs/<runId>/actions.jsonl
kernel/    纯函数规则内核（唯一规则真相）：零 IO / DOM / Date / Math.random / 浮点
content/   纯数据配表（JSON），由 kernel/loader.js 校验后注入
```

| 内核文件 | 职责 |
|---|---|
| `index.js` | `createGame / reduce / observe / legalActions / isLegal / replay / applyDifficulty / configHash / digest / playHints / configSchema` |
| `rng.js` | SplitMix64（双 u32、16 位分片乘法）+ xmur3 派生的 8 条命名流 |
| `combat.js` | 骰面结算、光盾→实伤顺序、状态、敌人意图、`end_turn` E1–E13 |
| `slots.js` / `counter.js` / `risk.js` | 约束 DSL / 克制方阵 / 风险等级 |
| `map.js` / `shop.js` / `forge.js` / `event.js` / `relic.js` | 地图、商店·宝箱·神殿·休息、锻造、事件、遗物 |
| `loader.js` | 唯一读文件的内核模块：配表 schema 与设计规则校验 |
| `digest.js` | 规范化 JSON + 纯 JS SHA-256 |

**确定性**：同一组 `(seed, config, actions[])` 产生逐字段一致的 Observation 与 `obsDigest` 序列。`POST /api/replay` 可以与在线对局逐行对拍，`tests/golden/*.jsonl` 固化了 4 组快照（其中 1 组全部开关关闭、1 组是插入非法 `end_turn` 的魔化骰路径）。

**无泄漏**：Observation 采用白名单显式构造。seed、RNG 状态、抽骰堆顺序、未揭示的地图、敌人编队 ID 一律不下发，由 `tests/api.test.js` 做黑名单扫描。

## API

`GET /api/health` · `GET /api/config/schema` · `POST /api/game` · `GET /api/game/:sid` · `POST /api/game/:sid/action` · `POST /api/game/:sid/reset` · `POST /api/replay` · `GET /api/content/:table` · `POST /api/seed`

响应统一为 `{ok, data}` 或 `{ok:false, error:{code, message}}`。动作接口的响应包含 `observation`、`obsDigest`、`events[]`、`legalActions[]`、`hints` 和 `outcome`；其中 `hints` 是内核算好的伤害预览与非法原因，供 UI 展示。

## 与 PRD 的偏差

见 [`docs/DECISIONS.md`](docs/DECISIONS.md)（补充决策 D-09～D-33 与平衡数值调整）和 [`docs/BALANCE.md`](docs/BALANCE.md)（最新平衡报告）。
