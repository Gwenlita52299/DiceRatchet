# 新一轮 M3 正式实验：以「三轮补丁语义去重」为 P1 起点

> 日期：2026-10-17　　成本：**¥0**（本文只做取证 + 设计；不含任何付费局）
> 依据：`runs/m3-formal/`（2026-10-13/14 的上一轮正式 run）、Spec B-81 / D-M3-7 七步口径、
> `evolve/generation.py::_inherited_parent_heldout`、`loop/generation.py::{write_gen000_baseline,maybe_run_generation}`
> 关联：[D-M3-7 架构对照](2026-10-14-M3-DM37架构改造与7步对照.md)、[M3 主体结果](2026-10-14-M3正式实验主体结果.md)

---

## 0. 一句话

上一轮 `m3-formal` 三代**全拒**（0 accept，harness 仍停在 v0）；把三代候选补丁做**语义去重**得到
**P1**，作为新一轮的起点 harness；`gen-000` 的 40 局 heldout 与 30 局 train **按用户指示复用**
（代价：需要一处 provenance/标签迁移，见 §3）；随后按 D-M3-7 每 cycle 74 局往下演化。

---

## 1. 上一轮到底产出了什么（逐项取证）

| 项 | 实测 | 文件 |
|---|---|---|
| run 状态 | `phase=done`、`generation=3`、`harness_version=0`、`last_completed_cycle=3` | `runs/m3-formal/state.json` |
| 三代裁决 | gen-001/002/003 全 **reject**；gen-001 `C2:no_significant_gain`、gen-002 `C5:truncation 0.175>0.150`、gen-003 `C2` | `generations/gen-00{1,2,3}/report.json` |
| heldout 胜率 | gen-000 基线 **0.175**（n=40，v0）；候选臂 0.10 / 0.15 / 0.10（n=40）。parent 臂每代 40 局**真的重测了** | `metrics/arms/`、`generations/*/report.json` |
| train 段 | 报告里 `train.n=4`（F64 修复前把 `generation` 标错一代 ⇒ 只捞到 4 局冒烟）；**真训练局 90 局在 `episodes/train/`** | 同上 |
| 候选补丁 | 3 份，全部是 `harness/prompts/decision.md` 的**提示词追加**（10–12 号规则） | `generations/gen-00{1,2,3}/patches/*.patch` |
| 训练数据 | 3 代 × 30 局 = 90 局（seed 1000–1089，`arm=train`），反思 90/90 成功 | `episodes/train/`、`reflections/` |
| 记忆快照 | `memory_v1/v2/v3.jsonl`，`active_count=114`，`last_tick=90` | `memory/` |
| 旧 run 花费 | ~¥197.67（已花，不追回） | 主体结果报告 |

**关键点**：三代候选臂的补丁**互相高度重叠**（心数管理 / 提交前合法性自检 / 回合预算），
这正是用户要求「语义去重」的对象。

---

## 2. P1 补丁（去重后的成稿，待你点头）

三份原始补丁的语义归类（逐条对齐后）：

| 语义主题 | gen-001 | gen-002 | gen-003 |
|---|---|---|---|
| ① 心数是稀缺/硬约束，低心数优先防御回复 | 规则 10 | 规则 10 | 规则 10 前半 |
| ② 提交前自检合法性（id/配对/资源/次数 `x0`） | 规则 12 | 规则 11 | 规则 11 |
| ③ 回合预算有限，避免被回合上限截断 | 规则 11 | 规则 12 | （隐含） |
| ④ 商店/锻造等经济选择优先补防御与回血 | — | — | 规则 10 后半 |

去重后 **3 条**（②③ 在三份里反复出现，合并为一条；④ 是 gen-003 独有且与 ① 同源，并入 ①）：

```diff
--- a/harness/prompts/decision.md
+++ b/harness/prompts/decision.md
@@ -44,3 +44,14 @@
    过早 `end_turn` 会浪费本回合的其余出牌机会——`plan` 里的动作会按顺序全部执行，
    一轮只写一个动作等于把一回合拆成好几回合。
+10. **心数是稀缺资源（硬约束）**：进入中段关卡（第 3–4 关）前尽量保留 ≥2 心；心数 ≤1 时
+    优先选择防御/回复/规避类选项，不要为贪输出硬吃伤害。商店、锻造与事件里也优先补防御与
+    回血，而非纯伤害强化。
+11. **先校验再行动**：写进 `plan` 的每个动作都要先确认在当前状态下合法——动作（含
+    `dieId`+`slotId` 的配对）逐字出现在 `legal_actions` 里、资源与次数足够（`x0` 表示已用完）、
+    槽位与骰子未重复使用。不确定就少写动作：**任一动作失效会让整条决策作废重试**。
+12. **回合预算有限**：若本回合无法有效清场，优先提升每回合的有效伤害（集中打核心骰、
+    避免无效掷骰）而不是拖延；确无收益时用 `end_turn` 收尾，避免被回合上限截断判负。
```

**身份口径（必须写进证据链）**：P1 是**人工（专家）构造的起点**，不是 MOD-07 的自动 accept。
它只改 `harness/prompts/decision.md` ⇒ `play_hash` 变化（`config.json` 与 `tools/*.py` 不变）。
落地时**不写** `versions/v1/`、**不打** `harness-v1` tag——那会伪造一条不存在的代际裁决；
改为 git commit（消息注明「P1：人工去重起点」）+ 在 `version.json.reason` 里写明来源。

---

## 3. 复用 `gen-000` 数据的可行性取证（这里是唯一有技术风险的地方）

用户要求复用 `gen-000` 的 40 局 heldout + 30 局 train。逐项核对**当前 HEAD（`efcd3ba`）**的复用闸：

### 3.1 行为口径：等价 ✅

| 项 | 旧 run（2026-10-13） | 当前 HEAD | 判定 |
|---|---|---|---|
| 模型（policy/reflection/proposer） | `deepseek-flash` / `deepseek` | 同 | ✅ |
| 局型 | hard × 6，H1（40 局） | 同（默认值） | ✅ |
| `harness/prompts/decision.md` | v0 原文 | 逐字节同 | ✅（P1 之前） |
| `config.json`（harness 内） | `{}` | `{}` | ✅ |
| `env.sandbox.*` | 15 键（ruleset 1.0.0、guard/honeypot/projection/render 一组死键） | 2 键（`policy_failure_streak` + `audit.ruleset_pin=1.1.0`） | ⚠️ **行为等价**：F73/F75 后其余的键**全库 0 消费者**（见 `configs/default.yaml` 32–51 行的逐键说明），删除不改玩法 |
| `config_hash` | `06d684e260540893` | `6406b96ed13b9a2b` | ❌ **不同**（唯一差异就是上面这组死键） |

**结论**：局的**结果**可比（模型、局型、篮子、harness 都同），但**归档复用闸**按
`config_hash` 逐字段对拍（`ARM_PROVENANCE_KEYS`）⇒ 旧归档**不会被自动复用**，会静默重跑 40 局（≈¥21）。

### 3.2 旧归档**本身**就已经对不上闸（与 config_hash 无关）❌

`runs/m3-formal/metrics/arms/*.json` 是 2026-10-14 之前写的，缺少 F63/F80/F81 之后新增的 4 个键：

| 键 | 旧归档 | 当前闸要求 | 说明 |
|---|---|---|---|
| `harness_play_hash` | `None`（字段不存在） | v0 的真实指纹 | 代码注释已写死：缺该键 ⇒ **一律重算**（fail-closed，防「身份未知的测量冒充已知」） |
| `resolved_seeds_hash` | `None` | H1 解析后 seed 列表的 digest | 同上 |
| `model_hash` | `""` | `deepseek-flash` | F81 前 18/18 归档都是空串 |
| `model_backend` | `""` | `deepseek` | 同上 |

`runs/m3-formal/generations/gen-000/report.json` 同样**没有** `harness_play_hash` 字段 ⇒
`_inherited_parent_heldout` 会走「旧 run 回落」分支（要求 `decision=="accept"`），而基线是
`decision=="baseline"`、且 v0 ≠ P1 的 play 面 ⇒ **继承不成立**。

### 3.3 训练局的标签也错一代 ❌

`episodes/train/e-001000-g000-train.jsonl` 是 **F64 修复前**落的：标签是 `state.generation`（=0），
而当前代码要求训练局标「**它服务的那一代**」（gen-001 的训练块 = seed 1000–1029、`generation=1`）。
直接拷进来，`evaluate_generation` 的 train 段会取不到这 30 局（`take("train", 1)` 落空 ⇒
`train.source="insufficient"`）。

### 3.4 三条可选的处置（需要你选）

| 处置 | 做法 | 代价 | 诚实性 |
|---|---|---|---|
| **① 迁移（推荐）** | 写一个**只读旧 run、只写新 run** 的导入步骤：把 P1 的 `play_hash`、`resolved_seeds_hash`、`model_hash/backend`、`config_hash` **回填**进新 run 的归档副本；训练轨迹拷进新 run 并按 F64 口径**重标** `generation=1`（旧 run 一个字节不改） | ¥0（不含跑局）；开发 ~半天 | 高：附录记「回填的 5 个身份键从何而来 + 旧 run 的 config_hash 差异只有死键」；旧 run 保持原样可审计 |
| **② 重跑 gen-000** | 不复用，按新口径重测 40 局 + 重跑 30 局训练 | ≈¥62 / ≈3 h | 最高（零解释成本） |
| **③ 只复用 heldout** | 只迁移 40 局 heldout，30 局训练重跑 | ≈¥26 | 中：训练学习状态从头开始，与「复用」指示不符 |

**我建议 ①**（用户明确说可复用，且 §3.1 已证行为等价），但把结论与证据写进产物。
注意：**复用训练数据**意味着新一轮的 cycle 0 学习状态（记忆/反思）继承自上一轮 —— 这本来就是
「复用」的语义，但也意味着新一轮的提案输入不是「裸 v0 + 全新训练」，需要在报告里写明。

---

## 4. 新一轮的目标态（待你确认）

```
起点 harness  v1_0 = v0 + P1（人工去重）        ← 不再测「裸 v0」
gen-000       在 H1 上测 v1_0 的 40 局 heldout   ← 唯一的「起点测量」（= D-M3-7 的步骤 ①，但起点换了）
cycle 1       v1_0 train 30 局 → 补丁 P2 → v1_0+P2 测 40 局 heldout → 门禁
cycle 2       （若 v1_0 或 v1_0+P2 在役）train 30 局 → P3 → 40 局 heldout → 门禁
```

* 每 cycle 74 局（K=30 + 2+2 冒烟 + 40 候选 heldout；parent 的 40 局按 D-M3-7 继承，0 局）
  —— `E_gen=114 ≤ cap 120`，继承生效时实跑 74。
* 复用项：cycle 0 的 30 局 train（+反思 +记忆快照）与 40 局 heldout 历史（作为「裸 v0」的锚点，
  用于**事后**量化 P1 本身的效果：裸 v0 的 **0.175**（gen-000 基线，n=40）vs v1_0 的起点测量）。

### 4.1 预算与墙钟（按 `¥0.879/局` 上界、`4.8 h/代` 实测外推）

| 方案 | 跑局 | ≈¥ | ≈墙钟 |
|---|---|---|---|
| **A**（起点测量 + 2 cycle）＝推荐 | 40 + 74 + 74 = **188** | ≈¥165 | ≈7 h |
| B（起点测量 + 1 cycle，先看 P2） | 40 + 74 = **114** | ≈¥100 | ≈4.3 h |
| C（起点测量 + 3 cycle） | 40 + 3×74 = **262** | ≈¥230 | ≈9.5 h |

一次性资源 **H2（消融）与 test 本轮不动**（0 accept 的 run 里 `no_self_edit` 与 `full` 构造上相同；
test 每里程碑一次）。

---

## 5. 需要你点头的三件事

1. **起点测量**：P1 起点要不要**花 40 局（≈¥21）真测一次**？不测就只能拿「裸 v0 的 gen-000」
   冒充 P1 的起点 —— 那会让第一对比较（起点 vs P2）混入 P1 自身的效应，**不推荐**。
2. **跑几个 cycle**：A（2 cycle，≈¥165）／B（1 cycle，≈¥100）／C（3 cycle，≈¥230）。
3. **训练数据复用**：按 §3.4 的 ①（迁移，¥0 开发）／②（重跑 30 局 ≈¥26）／③（只复用 heldout）。

点头后我会先做（全部免费、失败不花钱）：P1 落进 harness + git 提交 → 迁移/导入步骤 + 它自己的
回归用例 → 新一轮 `run_m3_next.sh`（`--plan/--preflight/--status/--post` 全套断言）→ 只跑到
`--preflight` 全绿为止，**再**由你确认起跑。

---

## 6. 实际执行回填（2026-10-17，事后追加）

本节是**回填**，不是设计：§4 的「1 个 cycle / ≈¥100 / ≈4.5 h」没有兑现。

| 项 | 设计 | 实际 | 原因 |
|---|---|---|---|
| cycle 数 | 1（先看 P2） | **3** | `run_m3r2.sh` 把 `TRAIN_SEEDS` 硬编码成 `train:1000-1089`（3 块），导入只覆盖第 1 块 ⇒ 多跑 2 个 cycle |
| 局数 / 成本 / 墙钟 | 114 / ≈¥100 / ≈4.5 h | **262 / ¥240.94 / 12 h 22 m** | 同上 |
| 判据 1 | — | **`ok=true`，accepts=1**（项目首次 accept） | P2 把起点 0.05 抬到 0.275 |
| P1 的效果 | 预期"至少不差" | **0.05 vs 裸 v0 0.175**（点估计 −0.125，CI 近乎相接） | 人工去重的三条规则在 heldout 上**没有**带来增益 |

已修：`CYCLES` 推出 seed 上界 + 前置断言用 `parse_seeds_spec` 精确核对 seed 数（见 [主体结果 §6](2026-10-17-M3R2-正式实验主体结果.md)）。
逐代数字、P2 的 diff、两条死路、成本与诚实边界全在 [主体结果](2026-10-17-M3R2-正式实验主体结果.md)。
