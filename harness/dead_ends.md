## gen-002 · 733b0037f65d
- 类型：prompt_patch
- 结局：reject（C1:win_rate 0.0750<0.2250）
- 触及文件：harness/harness/prompts/decision.md（5 行）
- 指标：Δwin_rate=-0.2000；p=0.0215；候选 win_rate=0.0750；stages=4.5000
- 提案理由：证据 reflect:train#3/#11/#13/#15 显示非法尝试 10~24 次并伴随 E_KERNEL_TURN_LIMIT 截断判负；traj:train#3/#8/#15 均为回合上限截断。现有第 11 条只泛泛说「先校验」，未把非法尝试与回合预算烧毁直接挂钩。此改动在「回合预算」段前置一条硬性自检清单，与 dead_ends 无重叠（dead_ends 为空）。
## gen-003 · 1082757314fc
- 类型：prompt_patch
- 结局：reject（C1:win_rate 0.1750<0.2250）
- 触及文件：harness/harness/prompts/decision.md（4 行）
- 指标：Δwin_rate=-0.1000；p=0.4240；候选 win_rate=0.1750；stages=4.5250
- 提案理由：reflect:train#0/#1/#2/#3/#7/#16 一致指出心数在第 3-4 关耗尽、防御与续航是瓶颈而非输出；traj:train#0-#3 均 stages=4 且 hearts=0。现有第 10 条只在约束段泛泛提及，未进入「回合预算」决策前置。与 dead_ends 的 gen-002 不同：那条改的是非法动作自检清单，本条改的是心数/防御优先级，机制不同。
