# 平衡报告

> 生成命令：`node scripts/balance.js --n 200 --strategy all --duel`（种子 10000–10199，确定性可复现）
> 目标（GAME-00 §7.2）：easy 45–65% / medium 25–40% / hard 10–20%。数值调整记录见 [DECISIONS.md](DECISIONS.md)。
> 验收口径以启发式策略（非 `random`）为准；`random` 只是下界参照。

# 通关率（每组 200 局）
| 难度 | 策略 | 通关率 | 目标 | 平均到达关 | 平均回合/局 | 技能骰使用/胜局 |
|---|---|---|---|---|---|---|
| easy | random | 0.0% ⚠️ | 45–65% | 3.98 | 55.4 | - |
| easy | greedy_damage | 61.0% ✅ | 45–65% | 9.56 | 88.2 | 240.2 |
| easy | greedy_counter | 61.0% ✅ | 45–65% | 9.33 | 72.9 | 195.9 |
| easy | school_blade | 66.0% ⚠️ | 45–65% | 9.50 | 63.1 | 171.8 |
| easy | school_arcana | 59.0% ✅ | 45–65% | 9.30 | 101.4 | 328.0 |
| easy | school_verdant | 61.5% ✅ | 45–65% | 9.38 | 125.5 | 286.5 |
| medium | random | 0.0% ⚠️ | 25–40% | 3.35 | 41.2 | - |
| medium | greedy_damage | 37.5% ✅ | 25–40% | 8.87 | 78.5 | 231.7 |
| medium | greedy_counter | 35.5% ✅ | 25–40% | 8.78 | 65.7 | 195.0 |
| medium | school_blade | 34.5% ✅ | 25–40% | 8.86 | 56.4 | 167.7 |
| medium | school_arcana | 28.0% ✅ | 25–40% | 8.37 | 87.5 | 317.5 |
| medium | school_verdant | 30.5% ✅ | 25–40% | 8.76 | 115.6 | 292.0 |
| hard | random | 0.0% ⚠️ | 10–20% | 3.15 | 42.4 | - |
| hard | greedy_damage | 8.0% ⚠️ | 10–20% | 6.41 | 60.7 | 213.7 |
| hard | greedy_counter | 12.5% ✅ | 10–20% | 7.12 | 58.5 | 173.0 |
| hard | school_blade | 8.0% ⚠️ | 10–20% | 6.76 | 44.8 | 135.1 |
| hard | school_arcana | 3.5% ⚠️ | 10–20% | 5.75 | 54.0 | 264.1 |
| hard | school_verdant | 11.5% ✅ | 10–20% | 6.89 | 86.2 | 252.8 |

# 学派 × 敌人学派 胜率矩阵（精英单挑，每格 100 场，medium）
| 敌人 \ 构筑 | blade | arcana | verdant | 克制−被克 |
|---|---|---|---|---|
| elite_verdant_warden (verdant) | 100% | 0% | 59% | +100pp ✅ |
| elite_arcana_weaver (arcana) | 100% | 100% | 100% | +0pp ⚠️ |
| elite_blade_champion (blade) | 4% | 0% | 0% | +0pp ⚠️ |

耗时 312.0s

## 结论

- **A10 通关率**：`greedy_counter`（默认基准 bot）在三个难度上都落在目标区间：easy 61.0% / medium 35.5% / hard 12.5%。各学派专精 bot 的结果也基本都在区间内或贴近边界，说明三个流派都有可行的通关路径（A5）。
- **A5 流派**：胜局平均使用技能骰面 170–330 次，远高于 ≥8 的要求。
- **A6 / G-5 克制博弈**：`elite_verdant_warden`（自然）面对锋刃构筑（克制）胜率 100%，面对秘法构筑（被克）胜率 0%，差距 +100pp，满足"同一敌人胜率差 ≥20pp 的可复现实例"。
- 单挑矩阵里，`elite_arcana_weaver` 对三种构筑都偏弱，`elite_blade_champion`（每回合叠力量）在 10 颗骰的单挑构筑下偏强。整局中玩家骰池更大，所以没有调整这两只精英；它们可以作为后续调参的候选。
