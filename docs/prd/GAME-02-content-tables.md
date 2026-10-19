# GAME-02 配表规格与内容数据

> 版本：v1.2（2026-10-17 与 `content/*.json` 对齐：技能骰 17、普通怪 15，见 §9）· 历史版本：v1.1（2026-09-30 16:39 按实现回写）· 状态：待评审 · 上游：[GAME-00-webgame-PRD.md](GAME-00-webgame-PRD.md) · [GAME-01-kernel-contract.md](GAME-01-kernel-contract.md)
> 定位：把 GAME-00 的规则**落到可实现的纯数据**。本文件的表即为 `content/*.json` 的**权威内容与数值基线**；
> 实现者可直接照抄，也可按 §8 的平衡流程调整数值（调整必须回写本文件）。
>
> **ID 规则**：`^[a-z][a-z0-9_]*$`（全 ASCII，保证 JS/Python 排序一致）。
> **数值规则**：全部整数；倍率用千分整数 `mulPermille`。
> **骰面索引**：`faces[0..5]` ↔ 骰面 1..6；`pip = index + 1`。

---

## 1. 玩家角色与初始骰池

### 1.1 角色 `warrior`（v1 唯一角色）

```jsonc
{
  "id": "warrior", "name": "破咒者", "desc": "以骰为刃，以净化为盾。",
  "heartsBonus": 0, "virtueBonus": 0,
  "abilities": [
    { "id": "warrior_reroll", "name": "重掷", "usesPerStage": 1, "cost": 0,
      "effect": "reroll_all_hand", "desc": "重掷手中所有未使用的骰子（消费 dice.player 流）" },
    { "id": "warrior_purify", "name": "净化", "usesPerStage": null, "cost": 0,
      "effect": "purify_one_corrupt", "desc": "无代价清除 1 颗魔化骰（次数由难度 config.purifyPerStage 决定）" },
    { "id": "warrior_focus", "name": "专注", "usesPerStage": 1, "cost": 0,
      "effect": "set_pip", "desc": "把 1 颗手骰的骰面改为任意值（1–6），每场 1 次" }
  ],
  "startingDeck": [
    "die_strike", "die_strike", "die_guard", "die_guard",
    "die_mend", "die_utility", "die_strike", "die_utility"
  ],
  "startingSlots": [
    { "slotId": "slot_1", "slotKind": "attack",   "constraint": "any",   "bonus": 0 },
    { "slotId": "slot_2", "slotKind": "attack",   "constraint": "ge:3",  "bonus": 1 },
    { "slotId": "slot_3", "slotKind": "defense",  "constraint": "any",   "bonus": 0 },
    { "slotId": "slot_4", "slotKind": "heal",     "constraint": "even",  "bonus": 1 },
    { "slotId": "slot_5", "slotKind": "utility",  "constraint": "any",   "bonus": 0, "reusable": true }   // v1.1：工具槽可重复使用（D-11）
  ]
}
```

> v1.1（D-23）：`usesPerStage` 按「每场战斗」重置（实现中字段名为 `usesPerBattle`）。
> `warrior_focus` 的 `set_pip` 需要额外输入（`value`），是本作**唯一**带附加参数的能力；`use_ability` 的 schema 需允许可选 `value: 1..6`。

### 1.2 初始普通骰（`normal` 类）

**`die_strike` 打击骰**（`riskTier: safe`）

| pip | label | kind | value | school | target | tags |
|---|---|---|---|---|---|---|
| 1 | 刺击 1 | `purge` | 1 | neutral | enemy | — |
| 2 | 刺击 2 | `purge` | 2 | neutral | enemy | — |
| 3 | 刺击 3 | `purge` | 3 | neutral | enemy | — |
| 4 | 刺击 4 | `purge` | 4 | neutral | enemy | — |
| 5 | 蓄力斩 6 | `purge` | 6 | neutral | enemy | `["charge"]` |
| 6 | 蓄力斩 7 | `purge` | 7 | neutral | enemy | `["charge"]` |

**`die_guard` 护盾骰**（`riskTier: safe`）

| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 光盾 2 | `ward` | 2 | neutral | self |
| 2 | 光盾 3 | `ward` | 3 | neutral | self |
| 3 | 光盾 4 | `ward` | 4 | neutral | self |
| 4 | 光盾 5 | `ward` | 5 | neutral | self |
| 5 | 光盾 6 | `ward` | 6 | neutral | self |
| 6 | 光盾 8 | `ward` | 8 | neutral | self |

**`die_mend` 修复骰**（`riskTier: safe`）

| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 修复 1 | `mend` | 1 | neutral | self |
| 2 | 修复 2 | `mend` | 2 | neutral | self |
| 3 | 修复 3 | `mend` | 3 | neutral | self |
| 4 | 修复 4 | `mend` | 4 | neutral | self |
| 5 | 修复 5 | `mend` | 5 | neutral | self |
| 6 | 修复 6 | `mend` | 6 | neutral | self |

**`die_utility` 工具骰**（`riskTier: safe`）— 教学用"任意门"

| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 抽骰 1 | `draw` | 1 | neutral | self |
| 2 | 抽骰 1 | `draw` | 1 | neutral | self |
| 3 | 星屑 8 | `gold` | 8 | neutral | self |
| 4 | 改骰 +1 | `modify` | 1 | neutral | self |
| 5 | 重掷 | `reroll` | 1 | neutral | self |
| 6 | 刺击 4 | `purge` | 4 | neutral | enemy |

### 1.3 锻造骰（`forged` 类，由锻造节点产出）

锻造产出的是**骰面**而非骰子：把一个候选面替换掉目标骰的某个面。候选面池（`forge` 流 5 选 1）：

| ID | label | kind | value | school | target | 权重 |
|---|---|---|---|---|---|---|
| `ff_purge3` | 魔化 3 | `purge` | 3 | neutral | enemy | 3 |
| `ff_purge5` | 魔化 5 | `purge` | 5 | neutral | enemy | 1 |
| `ff_ward4` | 光盾 4 | `ward` | 4 | neutral | self | 3 |
| `ff_mend3` | 修复 3 | `mend` | 3 | neutral | self | 2 |
| `ff_draw1` | 抽骰 1 | `draw` | 1 | neutral | self | 2 |
| `ff_gold12` | 星屑 12 | `gold` | 12 | neutral | self | 2 |
| `ff_poison3` | 剧毒 3 | `status`(poison) | 3 | verdant | enemy | 2 |
| `ff_weak2` | 虚弱 2 | `status`(weak) | 2 | arcana | enemy | 2 |
| `ff_strength1` | 力量 1 | `status`(strength) | 1 | blade | self | 1 |
| `ff_blade_purge6` | 锋刃 6 | `purge` | 6 | blade | enemy | 1 |
| `ff_risky_purge9` | 献祭 9 | `purge`+`selfCorruption 3` | 9 | blade | enemy | 1 |

> `ff_risky_purge9` 是**复合面**（§GAME-01 Q-1 允许的唯一复合形态）。

---

## 2. 技能骰（`skill` 类，17 颗：blade 5 / arcana 5 / verdant 5 / common 2）

**风险等级由 loader 依面统计自动判定**（GAME-00 §5.3）。下表 `risk` 为**期望值**，loader 校验必须一致。

### 2.1 锋刃系 `blade`（5 颗）

**`skill_blade_flurry` 连环突刺** — risk `safe` — 稳定叠蓄势
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 突刺 2 | `purge` | 2 | blade | enemy |
| 2 | 突刺 2 | `purge` | 2 | blade | enemy |
| 3 | 突刺 3 | `purge` | 3 | blade | enemy |
| 4 | 突刺 3 | `purge` | 3 | blade | enemy |
| 5 | 突刺 4 | `purge` | 4 | blade | enemy |
| 6 | 贯通 5 | `purge` | 5 | arcana | enemy |

**`skill_blade_execute` 处决斩** — risk `balanced` — 高单点
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 2 | 斩击 3 | `purge` | 3 | blade | enemy |
| 3 | 斩击 4 | `purge` | 4 | blade | enemy |
| 4 | 斩击 5 | `purge` | 5 | blade | enemy |
| 5 | 斩击 6 | `purge` | 6 | blade | enemy |
| 6 | 自损 3 | `selfCorruption` | 3 | neutral | self |

**`skill_blade_charge` 蓄势** — risk `safe` — 蓄势引擎
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 蓄势 | `status`(strength) | 1 | blade | self |
| 2 | 蓄势 | `status`(strength) | 1 | blade | self |
| 3 | 蓄势 | `status`(strength) | 2 | blade | self |
| 4 | 刺击 2 | `purge` | 2 | blade | enemy |
| 5 | 刺击 3 | `purge` | 3 | blade | enemy |
| 6 | 毒刺 3 | `status`(poison) | 3 | verdant | enemy |

**`skill_blade_berserk` 狂战** — risk `risky` — 极高爆发
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 自损 4 | `selfCorruption` | 4 | neutral | self |
| 2 | 自损 4 | `selfCorruption` | 4 | neutral | self |
| 3 | 狂斩 7 | `purge` | 7 | blade | enemy |
| 4 | 狂斩 8 | `purge` | 8 | blade | enemy |
| 5 | 狂斩 9 | `purge` | 9 | blade | enemy |
| 6 | 自损 5 | `selfCorruption` | 5 | neutral | self |

**`skill_blade_reap` 收割** — risk `balanced` — 处决/斩杀
| pip | label | kind | value | school | target | tags |
|---|---|---|---|---|---|---|
| 1 | 斩 2 | `purge` | 2 | blade | enemy | — |
| 2 | 自损 2 | `selfCorruption` | 2 | neutral | self | — |
| 3 | 斩 4 | `purge` | 4 | blade | enemy | — |
| 4 | 斩 5 | `purge` | 5 | blade | enemy | `["execute"]` |
| 5 | 斩 6 | `purge` | 6 | blade | enemy | `["execute"]` |
| 6 | 藤蔓 2 | `status`(poison) | 2 | verdant | enemy | — |

### 2.2 秘法系 `arcana`（5 颗）

**`skill_arcana_echo` 回响** — risk `safe` — 过牌
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 抽骰 1 | `draw` | 1 | arcana | self |
| 2 | 抽骰 1 | `draw` | 1 | arcana | self |
| 3 | 抽骰 2 | `draw` | 2 | arcana | self |
| 4 | 魔弹 2 | `purge` | 2 | arcana | enemy |
| 5 | 魔弹 3 | `purge` | 3 | arcana | enemy |
| 6 | 锋刃 4 | `purge` | 4 | blade | enemy |

**`skill_arcana_shift` 移位** — risk `balanced` — 手骰质量修正
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 2 | 重掷 | `reroll` | 1 | arcana | self |
| 3 | 重掷 | `reroll` | 1 | arcana | self |
| 4 | 改骰 +1 | `modify` | 1 | arcana | self |
| 5 | 魔弹 3 | `purge` | 3 | arcana | enemy |
| 6 | 自损 1 | `selfCorruption` | 1 | neutral | self |

**`skill_arcana_hex` 咒印** — risk `balanced` — 降敌输出
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 咒印 2 | `status`(weak) | 2 | arcana | enemy |
| 2 | 咒印 2 | `status`(weak) | 2 | arcana | enemy |
| 3 | 魔弹 2 | `purge` | 2 | arcana | enemy |
| 4 | 魔弹 3 | `purge` | 3 | arcana | enemy |
| 5 | 灼烧 3 | `status`(burn) | 3 | arcana | enemy |
| 6 | 自损 3 | `selfCorruption` | 3 | neutral | self |

**`skill_arcana_duplicate` 复制** — risk `balanced` — 面复制
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 复制 | `duplicate_face` | 1 | arcana | self |
| 2 | 魔弹 2 | `purge` | 2 | arcana | enemy |
| 3 | 抽骰 1 | `draw` | 1 | arcana | self |
| 4 | 魔弹 4 | `purge` | 4 | arcana | enemy |
| 5 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 6 | 自损 2 | `selfCorruption` | 2 | neutral | self |

**`skill_arcana_surge` 法潮** — risk `risky` — 高风险连环
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 自损 3 | `selfCorruption` | 3 | neutral | self |
| 2 | 自损 3 | `selfCorruption` | 3 | neutral | self |
| 3 | 魔弹 6 | `purge` | 6 | arcana | enemy |
| 4 | 魔弹 7 | `purge` | 7 | arcana | enemy |
| 5 | 抽骰 2 | `draw` | 2 | arcana | self |
| 6 | 自损 4 | `selfCorruption` | 4 | neutral | self |

### 2.3 自然系 `verdant`（5 颗）

**`skill_verdant_bloom` 绽放** — risk `safe` — 稳定续航
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 修复 2 | `mend` | 2 | verdant | self |
| 2 | 修复 3 | `mend` | 3 | verdant | self |
| 3 | 修复 3 | `mend` | 3 | verdant | self |
| 4 | 修复 4 | `mend` | 4 | verdant | self |
| 5 | 修复 5 | `mend` | 5 | verdant | self |
| 6 | 魔弹 3 | `purge` | 3 | arcana | enemy |

**`skill_verdant_venom` 荆棘毒** — risk `safe` — DoT
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 剧毒 2 | `status`(poison) | 2 | verdant | enemy |
| 2 | 剧毒 2 | `status`(poison) | 2 | verdant | enemy |
| 3 | 剧毒 3 | `status`(poison) | 3 | verdant | enemy |
| 4 | 剧毒 4 | `status`(poison) | 4 | verdant | enemy |
| 5 | 藤击 2 | `purge` | 2 | verdant | enemy |
| 6 | 锋刃 4 | `purge` | 4 | blade | enemy |

**`skill_verdant_bark` 树皮甲** — risk `balanced` — 高减伤
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 光盾 4 | `ward` | 4 | verdant | self |
| 2 | 光盾 5 | `ward` | 5 | verdant | self |
| 3 | 光盾 5 | `ward` | 5 | verdant | self |
| 4 | 光盾 6 | `ward` | 6 | verdant | self |
| 5 | 光盾 7 | `ward` | 7 | verdant | self |
| 6 | 自损 1 | `selfCorruption` | 1 | neutral | self |

**`skill_verdant_regrow` 再生** — risk `safe` — 长线
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 再生 1 | `status`(regen) | 1 | verdant | self |
| 2 | 再生 1 | `status`(regen) | 1 | verdant | self |
| 3 | 再生 2 | `status`(regen) | 2 | verdant | self |
| 4 | 修复 2 | `mend` | 2 | verdant | self |
| 5 | 修复 3 | `mend` | 3 | verdant | self |
| 6 | 咒印 1 | `status`(weak) | 1 | arcana | enemy |

**`skill_verdant_thorn` 血荆棘** — risk `risky` — 反伤型高收益
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 自损 3 | `selfCorruption` | 3 | neutral | self |
| 2 | 自损 3 | `selfCorruption` | 3 | neutral | self |
| 3 | 剧毒 6 | `status`(poison) | 6 | verdant | enemy |
| 4 | 剧毒 7 | `status`(poison) | 7 | verdant | enemy |
| 5 | 修复 6 | `mend` | 6 | verdant | self |
| 6 | 自损 4 | `selfCorruption` | 4 | neutral | self |

### 2.4 通用系 `common`（`neutral`，2 颗）

**`skill_common_forge` 锻造锤** — risk `safe` — 改骰与理财
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 改骰 +1 | `modify` | 1 | neutral | self |
| 2 | 改骰 +1 | `modify` | 1 | neutral | self |
| 3 | 改骰 +2 | `modify` | 2 | neutral | self |
| 4 | 星屑 10 | `gold` | 10 | neutral | self |
| 5 | 星屑 15 | `gold` | 15 | neutral | self |
| 6 | 魔化 3 | `purge` | 3 | neutral | enemy |

**`skill_common_gamble` 赌徒** — risk `risky` — 纯风险
| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 无事 | `nothing` | 0 | neutral | none |
| 2 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 3 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 4 | 魔化 6 | `purge` | 6 | neutral | enemy |
| 5 | 魔化 8 | `purge` | 8 | neutral | enemy |
| 6 | 无事 | `nothing` | 0 | neutral | none |

### 2.5 厄咒骰（`cursed` 类，敌人/事件塞入）

**`die_curse_heavy` 重咒骰** — risk `cursed`（不可锻造、不可删除，除事件）

| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1 | 自损 3 | `selfCorruption` | 3 | neutral | self |
| 2 | 自损 3 | `selfCorruption` | 3 | neutral | self |
| 3 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 4 | 自损 2 | `selfCorruption` | 2 | neutral | self |
| 5 | 自损 4 | `selfCorruption` | 4 | neutral | self |
| 6 | 自损 1 | `selfCorruption` | 1 | neutral | self |

**`die_curse_static` 滞涩骰** — risk `cursed`

| pip | label | kind | value | school | target |
|---|---|---|---|---|---|
| 1–4 | 滞涩 | `nothing` | 0 | neutral | none |
| 5 | 自损 1 | `selfCorruption` | 1 | neutral | self |
| 6 | 自损 2 | `selfCorruption` | 2 | neutral | self |

---

## 3. 技能骰设计规则自检（loader 强制，GAME-00 §5.5）

| 骰 | 本系机制面数（需 ≥2） | 反制天敌面（需 ≥1） | 通过 |
|---|---|---|---|
| `skill_blade_flurry` | 5（全 `blade` purge，喂蓄势） | 1（`arcana` 贯通 5） | ✅ |
| `skill_blade_execute` | 4（`blade` purge ×4） | 0 ❌ → **修正**：pip3 改为 `arcana` 学派 | 修正后 ✅ |
| `skill_blade_charge` | 4（strength ×3 + blade purge ×2… 计 3） | 1（`verdant` 毒刺） | ✅ |
| `skill_blade_berserk` | 3（blade purge ×3） | 0 ❌ → **修正**：pip1 改为 `arcana` 学派自损面 | 修正后 ✅ |
| `skill_blade_reap` | 3 | 1（`verdant` 藤蔓） | ✅ |
| `skill_arcana_echo` | 3（`arcana` draw ×3） | 1（`blade` 锋刃 4） | ✅ |
| `skill_arcana_shift` | 3（reroll/modify `arcana` ×3） | 0 ❌ → **修正**：pip4 改为 `verdant` 学派 | 修正后 ✅ |
| `skill_arcana_hex` | 4 | 0 ❌ → **修正**：pip5 灼烧改 `blade` 学派 | 修正后 ✅ |
| `skill_arcana_duplicate` | 3（`arcana` 复制/抽/魔弹） | 0 ❌ → **修正**：pip4 改 `verdant` 学派魔弹 4 | 修正后 ✅ |
| `skill_arcana_surge` | 3 | 0 ❌ → **修正**：pip3 魔弹 6 改 `blade` 学派 | 修正后 ✅ |
| `skill_verdant_bloom` | 5（`verdant` mend ×5） | 1（`arcana` 魔弹） | ✅ |
| `skill_verdant_venom` | 5 | 1（`blade` 锋刃 4） | ✅ |
| `skill_verdant_bark` | 5（`verdant` ward ×5） | 0 ❌ → **修正**：pip4 改为 `arcana` 学派光盾（叙事：咒法护壁） | 修正后 ✅ |
| `skill_verdant_regrow` | 5 | 1（`arcana` weak） | ✅ |
| `skill_verdant_thorn` | 3 | 0 ❌ → **修正**：pip3 剧毒改 `arcana` 学派（咒毒） | 修正后 ✅ |
| `skill_common_forge` | n/a（通用，免检） | n/a | ✅ |
| `skill_common_gamble` | n/a（通用，免检） | n/a | ✅ |

> ⚠️ **实现者注意**：上表已标注 8 处**必须应用的修正**（`execute` / `berserk` / `shift` / `hex` / `duplicate` / `surge` / `bark` / `thorn`）。
> 修正方式统一为：**把其中 1 个面的 `school` 改为"克制本系天敌的学派"**（blade→arcana，arcana→verdant，verdant→blade），效果数值与 kind 不变。
> 这些修正已在下文 §3.1 给出最终配表（**以 §3.1 为准**，§2 只用于展示设计过程）。

### 3.1 修正后的最终 skill 配表（**实现以此为准**）

| diceId | 修正点 | 最终 `school` 序列（pip 1→6） |
|---|---|---|
| `skill_blade_flurry` | — | blade, blade, blade, blade, blade, arcana |
| `skill_blade_execute` | pip3 → arcana | neutral, blade, **arcana**, blade, blade, neutral |
| `skill_blade_charge` | — | blade, blade, blade, blade, blade, verdant |
| `skill_blade_berserk` | pip1 → arcana | **arcana**, neutral, blade, blade, blade, neutral |
| `skill_blade_reap` | — | blade, neutral, blade, blade, blade, verdant |
| `skill_arcana_echo` | — | arcana, arcana, arcana, arcana, arcana, blade |
| `skill_arcana_shift` | pip4 → verdant | neutral, arcana, arcana, **verdant**, arcana, neutral |
| `skill_arcana_hex` | pip5 → blade | arcana, arcana, arcana, arcana, **blade**, neutral |
| `skill_arcana_duplicate` | pip4 → verdant | arcana, arcana, arcana, **verdant**, neutral, neutral |
| `skill_arcana_surge` | pip3 → blade | neutral, neutral, **blade**, arcana, arcana, neutral |
| `skill_verdant_bloom` | — | verdant, verdant, verdant, verdant, verdant, arcana |
| `skill_verdant_venom` | — | verdant, verdant, verdant, verdant, verdant, blade |
| `skill_verdant_bark` | pip4 → arcana | verdant, verdant, verdant, **arcana**, verdant, neutral |
| `skill_verdant_regrow` | — | verdant, verdant, verdant, verdant, verdant, arcana |
| `skill_verdant_thorn` | pip3 → arcana | neutral, neutral, **arcana**, verdant, verdant, neutral |
| `skill_common_forge` | — | neutral ×5, neutral |
| `skill_common_gamble` | — | neutral ×6 |

---

## 4. 关卡结构（10 关 / 3 章）

```jsonc
{
  "stageCount": 10,
  "chapters": [
    { "chapter": 1, "name": "腐蚀礁岩", "stages": [1,2,3],  "theme": "verdant" },
    { "chapter": 2, "name": "星域遗迹", "stages": [4,5,6],  "theme": "arcana" },
    { "chapter": 3, "name": "异常原点", "stages": [7,8,9,10], "theme": "blade" }
  ],
  "stages": [
    { "stage":1,  "chapter":1, "rows":2, "nodeBudget":[3,2], "kinds":["battle"],                    "boss":false, "tutorial":true },
    { "stage":2,  "chapter":1, "rows":3, "nodeBudget":[3,3,3], "kinds":["battle","treasure"],      "boss":false, "tutorial":true },
    { "stage":3,  "chapter":1, "rows":3, "nodeBudget":[3,4,1], "kinds":["battle","shop","rest"],   "boss":true,  "bossId":"boss_verdant_bloom" },
    { "stage":4,  "chapter":2, "rows":3, "nodeBudget":[3,4,3], "kinds":["battle","shop"],           "boss":false },
    { "stage":5,  "chapter":2, "rows":4, "nodeBudget":[3,3,4,3], "kinds":["battle","event","treasure"], "boss":false },
    { "stage":6,  "chapter":2, "rows":3, "nodeBudget":[4,4,1], "kinds":["battle","forge","shop"],   "boss":true,  "bossId":"boss_arcana_spiral" },
    { "stage":7,  "chapter":3, "rows":4, "nodeBudget":[3,4,4,3], "kinds":["battle","shop","event"], "boss":false },
    { "stage":8,  "chapter":3, "rows":4, "nodeBudget":[4,4,4,3], "kinds":["battle","shrine","treasure"], "boss":false },
    { "stage":9,  "chapter":3, "rows":3, "nodeBudget":[4,3,2], "kinds":["battle","forge","rest"],   "boss":false },
    { "stage":10, "chapter":3, "rows":3, "nodeBudget":[3,3,1], "kinds":["battle","shop","shrine"],  "boss":true,  "bossId":"boss_chaos_core" }
  ]
}
```

**节点类型（`kind`）与开关的对应**：

> `stages[].kinds` 是**候选池**，不是"本关必定出现全部类型"。地图生成时：每关第 1 行必为 `battle`；最后一个节点若 `boss=true` 则为 `boss`，否则为 `battle` 或 `elite`（若该关在 `eliteStages` 内）；其余行从 `kinds` 中按 `map` 流抽取（**同一种非战斗类型在同一关内至多出现 1 次**，保证节奏）。

| kind | 依赖开关 | 关闭时替代 |
|---|---|---|
| `battle` | — | — |
| `elite` | — | — （由难度 `eliteStages` 决定出现） |
| `boss` | — | — |
| `treasure` | — | — |
| `shop` | `shops` | `treasure` |
| `forge` | `shops` | `treasure` |
| `event` | `events` | `treasure` |
| `shrine` | `relics` | `rest` |
| `rest` | — | — （v1.1：回 1 心 / 净化上限永久 +1，二选一；原「回复 30% virtueMax」在「每场回满」规则下无意义，D-16）|

> `mapBranching=false` 时：整关压成**单一线性路径**，节点类型取该关 `kinds` 中按 `map` 流抽 1 种，`rows` 节点依次连接。

> **v1.1 地图规则补充（D-18 / D-15）**：
> - Boss 关（3/6/10）最后一行固定为 1 个 Boss 节点（第 3、6 关 `nodeBudget` 末行 2→1）。
> - 精英放置：非 Boss 的精英关，最后一行全部为精英；Boss 精英关（3/6），倒数第二行的首个战斗节点升格为精英。
> - 每场战斗胜利都进入「骰子 3 选 1」（loot 流，两颗构筑学派 + 一颗克制本章敌学派；第 1–2 关只出安全骰），精英/Boss 另掉 1 个遗物。

**精英关**（由难度 `eliteStages` 决定哪些 stage 出现精英，放置规则见上方 v1.1 补充）：

```jsonc
{ "eliteStages": { "easy":[3,6], "medium":[3,6,9], "hard":[3,5,7,9] } }
```

---

## 5. 怪物配表

### 5.1 普通怪物（`normal`，15 只：verdant 5 / arcana 5 / blade 4 / neutral 1）

| id | 名称 | school | hp | 槽（constraint→effect） | 骰 | ai |
|---|---|---|---|---|---|---|
| `mon_verdant_slime` | 腐生史莱姆 | verdant | 12 | `any`→purge 3；`odd`→ward 3 | 1×d6 | greedy_damage |
| `mon_verdant_bramble` | 荆棘藤 | verdant | 16 | `any`→purge 4；`any`→status poison 2 | 2×d6 | greedy_damage |
| `mon_verdant_mossbeast` | 苔兽 | verdant | 22 | `ge:3`→purge 5；`any`→ward 5 | 2×d6 | greedy_block |
| `mon_verdant_sporeling` | 孢子群 | verdant | 14 | `any`→status weak 2；`any`→purge 3 | 2×d4 | scripted |
| `mon_verdant_thornback` | 棘背兽 | verdant | 26 | `even`→purge 6；`any`→ward 4 | 3×d6 | greedy_damage |
| `mon_arcana_wisp` | 游荡咒火 | arcana | 10 | `any`→purge 3；`ge:4`→status burn 3 | 1×d6 | greedy_damage |
| `mon_arcana_hexer` | 咒术师 | arcana | 18 | `any`→status weak 3；`any`→purge 4 | 2×d6 | scripted |
| `mon_arcana_golem` | 符石傀儡 | arcana | 30 | `ge:4`→purge 7；`any`→ward 6 | 2×d8 | greedy_block |
| `mon_arcana_mirror` | 镜像体 | arcana | 20 | `in:2,4,6`→purge 5；`any`→add_cursed_die | 2×d6 | scripted |
| `mon_arcana_eye` | 窥视之眼 | arcana | 15 | `any`→status weak 2；`odd`→purge 5 | 2×d6 | greedy_damage |
| `mon_blade_grunt` | 锋刃幼体 | blade | 16 | `any`→purge 4；`ge:4`→purge 6 | 2×d6 | greedy_damage |
| `mon_blade_duelist` | 决斗者 | blade | 24 | `odd`→purge 6；`any`→ward 4 | 2×d6 | greedy_damage |
| `mon_blade_brute` | 蛮兵 | blade | 34 | `ge:3`→purge 8；`any`→purge 3 | 3×d6 | greedy_damage |
| `mon_blade_assassin` | 刺客 | blade | 18 | `even`→purge 7；`any`→status strength 1 | 2×d6 | scripted |
| `mon_neutral_shell` | 空壳 | neutral | 28 | `any`→purge 3；`any`→ward 8 | 2×d6 | greedy_block |

> **v1.1 敌人行为补充**：
> - 意图骰降序贪心分配给敌槽，每槽每回合至多触发一次；`enemyExtraDice` 只提高满足约束的概率（D-13）。
> - 敌人塞入玩家骰池的厄咒骰（镜像体/腐化母株/咒环）**仅本场战斗有效**，战斗结束移除；事件塞入的厄咒骰永久（D-14）。

### 5.2 精英（`elite`，6 只）

| id | 名称 | school | hp | 特色 | 骰 |
|---|---|---|---|---|---|
| `elite_verdant_warden` | 林苑守望者 | verdant | 60 | 每回合回血 2；中毒免疫 | 3×d6 |
| `elite_verdant_blight` | 腐化母株 | verdant | 55 | 向玩家骰池塞 1 颗 `die_curse_static` | 3×d6 |
| `elite_arcana_weaver` | 织咒者 | arcana | 52 | 每回合给玩家 2 层 weak | 3×d6 |
| `elite_arcana_lattice` | 咒阵 | arcana | 66 | 受击时反弹 2 点自魔化 | 3×d8 |
| `elite_blade_champion` | 锋刃冠军 | blade | 58 | 连续攻击叠 strength（每回合 +1） | 3×d6 |
| `elite_neutral_warden` | 铁壁 | neutral | 72 | 高光盾，每回合 ward 12 | 2×d10 |

### 5.3 Boss（`boss`，3 只）

**`boss_verdant_bloom` 腐化绽放**（第 3 关）— school `verdant`，hp 75，3×d6

| 阶段 | HP 阈值 | school | 行为模板 |
|---|---|---|---|
| P1 | 100–51% | verdant | `any`→purge 5；`any`→status poison 3 |
| P2 | 50–0% | verdant | `ge:3`→purge 7；`any`→ward 8；`any`→回血 4 |

**`boss_arcana_spiral` 咒环**（第 6 关）— school `arcana`，hp 110，3×d8

| 阶段 | HP 阈值 | school | 行为模板 |
|---|---|---|---|
| P1 | 100–61% | arcana | `any`→status weak 3；`any`→purge 5 |
| P2 | 60–26% | arcana | `in:2,4,6`→purge 8；`any`→塞 `die_curse_heavy` |
| P3 | 25–0% | arcana | `any`→purge 6 ×2；`any`→ward 10 |

**`boss_chaos_core` 异常原点**（第 10 关，最终 Boss）— hp 160，3×d8，**三阶段三学派**

| 阶段 | HP 阈值 | school | 行为模板 | 设计意图 |
|---|---|---|---|---|
| P1 | 100–67% | arcana | `any`→status weak 3；`any`→purge 6；`any`→抽骰压制（弃玩家 1 颗手骰） | 逼迫处理魔化骰 |
| P2 | 66–34% | blade | `ge:3`→purge 11；`any`→purge 7；`any`→禁用玩家 1 个技能槽（2 回合） | 爆发压力 |
| P3 | 33–0% | verdant | `any`→回血 6；`any`→status poison 5；`any`→ward 12 | 长线续航考验 |

> **GAME-00 §6.3 的落点**：三阶段三学派 ⇒ 任何单一学派构筑必有一阶段吃亏。这是验收 A6 的核心检验点。

> v1.1：上表「特色」已落为具体槽位与被动（如 `regen` / `poison_immune` / `thorns` / `ramp_strength`），权威数值见 `src/game/content/monsters.json`。

### 5.4 敌人编队（`enemyGroupId`）

编队由 `map` 流按 stage 抽取。**受难度影响的只是 HP/伤害倍率与额外骰，编队构成不变**（保证 RNG 消费一致）。

| 编队 id | 成员 | 适用 stage |
|---|---|---|
| `g_01` | `mon_verdant_slime` | 1 |
| `g_02` | `mon_verdant_slime` + `mon_arcana_wisp` | 1–2 |
| `g_03` | `mon_blade_grunt` | 2–3 |
| `g_04` | `mon_verdant_bramble` + `mon_blade_grunt` | 2–4 |
| `g_05` | `mon_arcana_hexer` + `mon_arcana_eye` | 4–5 |
| `g_06` | `mon_verdant_mossbeast` | 4–6 |
| `g_07` | `mon_blade_duelist` + `mon_arcana_wisp` | 5–7 |
| `g_08` | `mon_arcana_golem` + `mon_verdant_sporeling` | 6–8 |
| `g_09` | `mon_blade_brute` + `mon_blade_assassin` | 7–9 |
| `g_10` | `mon_arcana_mirror` + `mon_verdant_thornback` | 8–10 |
| `g_11` | `mon_blade_brute` + `mon_arcana_golem` | 9–10 |
| `g_12` | `mon_neutral_shell` | 3–6（v1.1 新增，D-32） |
| `g_e1` | `elite_verdant_warden` | elite |
| `g_e2` | `elite_arcana_weaver` | elite |
| `g_e3` | `elite_blade_champion` | elite |
| `g_e4` | `elite_verdant_blight` + `mon_verdant_sporeling` | elite |
| `g_e5` | `elite_arcana_lattice` | elite |
| `g_e6` | `elite_neutral_warden` + `mon_arcana_wisp` | elite |

**敌人 ID 零填充要求**（GAME-01 §4.2）：`e-00`, `e-01`, … `e-10`，保证字典序 == 数值序。

---

## 6. 事件配表（`events`，12 个）

每个事件 ≥3 选项，≥1 个正期望、≥1 个风险选项。**效果语法**（声明式）：

```
{ "effect": [ {"op":"gain_gold","value":60}, {"op":"lose_virtue","value":4} ] }
op 枚举：gain_gold | lose_gold | gain_die | remove_die | gain_relic | lose_relic |
        gain_heart | lose_heart | heal_virtue | lose_virtue | add_curse | modify_face |
        gain_status | remove_status | upgrade_die | random_gamble
```

| id | 标题 | 选项（label / 要求 / 结果） | 学派 |
|---|---|---|---|
| `ev_shrine_shards` | 星屑矿脉 | ①挖掘：得 70 星屑，下场战斗初始净化 -3 ②谨慎：得 30 星屑 ③离开：无 | — |
| `ev_blade_altar` | 锋刃祭坛 | ①献祭：失 1 心，得 2 颗 `blade` 技能骰 ②触碰：得 `ff_strength1` 面改造 ③离开 | blade |
| `ev_arcana_library` | 咒文图书馆 | ①研读：得 `skill_arcana_echo` ②赌博：50% 得 `skill_arcana_surge` / 50% 塞 1 颗 `die_curse_heavy` ③离开 | arcana |
| `ev_verdant_grove` | 藤蔓林 | ①采摘：清除净化惩罚并在下场战斗 +3 净化，带 1 层 poison ②焚毁：得 60 星屑，失 1 心（需 ≥2 心） ③离开 | verdant |
| `ev_dice_forge` | 废弃锻造台 | ①锻造：支付 40 星屑，从 5 选 1 改造骰面 ②拆解：随机移除 1 颗骰，得 45 星屑 ③离开 | — |
| `ev_mirror_pool` | 镜像池 | ①凝视：复制骰池中 1 颗随机骰 ②打碎：下场战斗初始净化 -2，得 1 颗 `blade` 技能骰 ③离开 | — |
| `ev_lost_oracle` | 迷失的神谕使 | ①救助：失 50 星屑，得 1 个随机遗物（`relics` 关闭时整条事件被过滤） ②无视：得 1 颗 `die_curse_static` ③绕路：失 10 星屑 | — |
| `ev_blood_pact` | 血契 | ①签订：一颗骰获得 `ff_risky_purge9` 面，失 2 心（需 ≥3 心） ②拒绝：下场战斗初始净化 -5 ③撕毁：50% 得 40 星屑 / 50% 下场战斗初始净化 -3 | blade |
| `ev_wandering_merchant` | 流浪商人 | ①交易：失 80 星屑，得 1 颗**克制本章 BOSS 学派**的技能骰 ②小交易：失 40，得 1 颗随机技能骰 ③离开 | — |
| `ev_ancient_slot` | 古代槽位 | ①激活：本局永久获得 1 个攻击槽（any） ②货币化：得 100 星屑，但塞入 1 颗 `die_curse_static` ③离开 | — |
| `ev_corrupt_spring` | 腐化泉 | ①饮用：回 1 心，塞 2 颗 `die_curse_static` ②净化：失 30 星屑，移除全部厄咒骰 ③离开 | verdant |
| `ev_school_trial` | 学派试炼 | ①锋刃试炼：得 1 颗 `blade` 技能骰 + 下场战斗 1 层 strength ②秘法试炼：得 1 颗 `arcana` 技能骰 + 下场战斗首回合抽骰 +2 ③自然试炼：得 1 颗 `verdant` 技能骰 + 下场战斗 +5 净化 ④挑战三座：50% 各得 1 颗 / 50% 失 1 心（需 ≥2 心） | 三系 |

**叙事文本与结果文案**由实现者撰写（≥120 字/事件），本表只定义机制。

> **v1.1 修订说明**：净化值每场战斗回满，故战斗外的「失/回净化」一律改为「下一场战斗初始净化修正」（D-16）；为满足 GAME-01 §8「每个事件 ≥1 个风险选项」，`ev_lost_oracle`/`ev_blood_pact`/`ev_ancient_slot`/`ev_corrupt_spring`/`ev_school_trial` 调整或新增了选项。权威数据见 `src/game/content/events.json`。

---

## 7. 遗物配表（`relics`，14 个）

`trigger` 取值：`passive` / `on_battle_start` / `on_turn_start` / `on_play_face` / `on_kill` / `on_shop` / `on_lose_heart`。
**学派联动遗物 ≥6**（标 ★）；**代价型 ≥2**（标 ⚠）。

| id | 名称 | rarity | trigger | 效果 | 标记 |
|---|---|---|---|---|---|
| `relic_blade_mark` | 锋刃印记 | common | on_play_face | 打出 `blade` 骰面时，该面 `purge` 值 +1 | ★ |
| `relic_arcana_loop` | 秘法回环 | common | on_turn_start | 若上回合打出 ≥3 颗 `arcana` 骰面，本回合 `drawPerTurn +1` | ★ |
| `relic_verdant_root` | 自然根须 | common | passive | 每次打出 `verdant` 骰面回 1 净化值 | ★ |
| `relic_school_prism` | 学派棱镜 | rare | on_play_face | 克制倍率对己方额外 `+150‰`（即 1300→1450） | ★ |
| `relic_stubborn_mix` | 杂派护符 | rare | passive | `monoPenalty` 失效；但你每回合抽骰 -1 | ★ ⚠️ |
| `relic_blade_echo` | 锋刃回响 | rare | on_kill | 击杀敌人后，**首颗**（按手骰顺序）`blade` 手骰骰面 +2（v1.1：改为确定性选择，遗物不得消费 RNG） | ★ |
| `relic_greedy_eye` | 贪婪之眼 | rare | passive | 星屑获取 ×1.5，但每场战斗初始净化值 -2 | ⚠️ |
| `relic_glass_heart` | 玻璃之心 | rare | passive | `virtueMax +4`，但每次失心立即再失 1 心 | ⚠️ |
| `relic_iron_bark` | 铁树皮 | common | on_battle_start | 战斗开始获得 8 光盾 | — |
| `relic_swift_hand` | 疾风之手 | common | on_turn_start | 每回合首颗打出的骰面视为 `school` = 你的 `deckSchool`，且值 +1 | — |
| `relic_purifier` | 净化之瓶 | common | on_battle_start | `purifyLeft +1` | — |
| `relic_last_stand` | 背水一战 | rare | passive | `virtue ≤ 3` 时所有 `purge` 值 +3 | — |
| `relic_boss_trophy` | 王的遗骸 | boss | on_battle_start | 对 `boss`/`elite` 伤害 +20% | — |
| `relic_deck_thin` | 极简教义 | boss | on_shop | 每次进商店首次删骰免费；骰池 ≤5 颗时魔化/光盾/修复面 +1 | — |

**遗物栏位**由难度决定（`relicSlots`: 4/3/2）。超出栏位的遗物**拾取时提示替换**（必须选择丢哪一个）。

---

## 8. 平衡流程与数值标定（实现者必做）

```
1. node scripts/balance.js --n 200 --difficulty easy   --strategies all
2. 输出：通关率 / 平均到达关数 / 每场战斗平均回合数 / 各学派构筑胜率矩阵
3. 目标区间（GAME-00 §7.2）：easy 45–65%、medium 25–40%、hard 10–20%
4. 若偏离：优先调 enemyHpMulPermille / enemyDmgMulPermille / virtueMax，
   其次调怪物 hp 与槽位数值；**禁止**调 counterMulPermille（克制强度属设计口径，直接决定博弈性）
5. 标定"学派 × 敌人学派"胜率矩阵，必须体现：克制的构筑对目标敌人胜率显著更高（≥20pp，验收 A6）
6. 所有改动回写本文件 §4–§7 的对应表，并在 §9 变更记录登记
```

**`scripts/balance.js` 的策略集合（自动 bot）**：

| 策略 | 说明 |
|---|---|
| `random` | 随机合法动作 |
| `greedy_damage` | 优先最大 `purge`，无则 `ward`，有魔化骰优先 `purify` |
| `greedy_counter` | 优先打出**克制当前敌人学派**的骰面 |
| `school_blade` / `school_arcana` / `school_verdant` | 优先使用本系骰面 + 本系机制（蓄势/过牌/续航） |

### 8.1 v1.1 标定结果（2026-09-30 16:39）

按第 4 步只调整了难度三项（其余难度键与全部怪物数值不变）：

| 键 | easy（原 → 现） | medium | hard |
|---|---|---|---|
| `virtueMax` | 12 → 22 | 10 → 24 | 9 → 24 |
| `enemyHpMulPermille` | 800 → 900 | 1000 → 900 | 1350 → 600 |
| `enemyDmgMulPermille` | 800 → 1000 | 1000 → 850 | 1250 → 575 |

`node scripts/balance.js --n 200 --strategy all --duel`（种子 10000–10199）结果：

| 难度 | greedy_counter | greedy_damage | school_blade | school_arcana | school_verdant |
|---|---|---|---|---|---|
| easy | 61.0% ✅ | 61.0% ✅ | 66.0% | 59.0% ✅ | 61.5% ✅ |
| medium | 35.5% ✅ | 37.5% ✅ | 34.5% ✅ | 28.0% ✅ | 30.5% ✅ |
| hard | 12.5% ✅ | 8.0% | 8.0% | 3.5% | 11.5% ✅ |

完整报告见 `src/game/docs/BALANCE.md`。

- A6：`elite_verdant_warden` 面对锋刃构筑（克制）胜率 100%，面对秘法构筑（被克）胜率 0%，差距 +100pp。
- 待调：单挑时 `elite_arcana_weaver` 对三种构筑都偏弱、`elite_blade_champion` 都偏强（整局中玩家骰池更大，暂未调整）。

> 这些 bot 同时是 GAME-00 §15 第 11 项与验收 A6/A10 的计算基础，**并且可直接复用为 MOD-08 的 heuristic 基线**——这是本 PRD 对 RSI 路线的另一处前置投资。

---

## 9. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版：角色与初始骰池、17 颗普通/技能/厄咒骰的完整 6 面配表、8 处学派自检修正与最终 `school` 序列、10 关 3 章结构、精英/普通/Boss 共 23 只怪物、编队表、12 个事件、14 个遗物、平衡流程与 5 种 bot 策略 |
| v1.1 | 2026-09-30 16:39 | 按 `src/game` 实现回写：§1.1 `slot_5` 可重复使用、能力按场计；§4 第 3/6 关 `nodeBudget` 末行 2→1、休息点选项、精英放置与战斗奖励规则；§5 敌人意图分配与厄咒骰持续性、新增编队 `g_12`；§6 事件表与实现对齐；§7 `relic_blade_echo`/`relic_deck_thin` 精确化；§8.1 难度三项平衡标定与通关率 |
| v1.2 | 2026-10-17 | **与实际配表对齐（纯文档，¥0）**：① §2 技能骰 15 → **17**（`skills.json` 实测 17 条，§2.1–§2.4 与 §3.1 最终表本来就是 17 行）；② §5.1 普通怪物 14 → **15**（`monsters.json` 实测 normal 15 / elite 6 / boss 3 = 24；分布 verdant 5 / arcana 5 / blade 4 / neutral 1，原文「每学派 ≥4」对 neutral 不成立）。 |
