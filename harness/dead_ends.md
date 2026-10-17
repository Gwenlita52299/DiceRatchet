## gen-002 · 733b0037f65d
- 类型：prompt_patch
- 结局：reject（C1:win_rate 0.0750<0.2250）
- 触及文件：harness/harness/prompts/decision.md（5 行）
- 指标：Δwin_rate=-0.2000；p=0.0215；候选 win_rate=0.0750；stages=4.5000
- 提案理由：证据 reflect:train#3/#11/#13/#15 显示非法尝试 10~24 次并伴随 E_KERNEL_TURN_LIMIT 截断判负；traj:train#3/#8/#15 均为回合上限截断。现有第 11 条只泛泛说「先校验」，未把非法尝试与回合预算烧毁直接挂钩。此改动在「回合预算」段前置一条硬性自检清单，与 dead_ends 无重叠（dead_ends 为空）。
