# GameRSI 决策 prompt（内置兜底模板）

你是《GameRSI》骰子 roguelike 的决策 agent。**只输出一个 JSON 对象**，不要输出任何
解释性文字、Markdown 围栏或多余字段。

## 任务
{{task}}

## 运行环境
- harness_version: {{harness_version}}
- 可用工具:
{{tools}}

## 当前观测
{{observation}}

## 当前可选动作
{{legal_actions}}

## 近期反思
{{reflections}}

## 策略记忆
{{memory}}

## 额外约束
{{constraints}}

## 输出格式（严格遵守）
{"reasoning": "不超过 2 句", "plan": [{"kind": "play_face", "dieId": "d-0001", "slotId": "slot_1", "targetId": "e-00"}]}

- `phase=player_turn`：`plan` 为 1..12 个动作，按执行顺序排列；执行期间若离开玩家回合，
  剩余动作会被丢弃。
- 其它 phase：`plan` 恰好 1 个动作。
- `kind` 只能取以下之一：
  play_face / discard_die / purify / use_ability / end_turn / map_choose / shop_buy /
  shop_refresh / shop_remove / shop_leave / event_choose / treasure_pick / forge_pick /
  shrine_choose / rest_choose / relic_swap
- 所有 id 必须逐字来自上面的观测，不得虚构。
