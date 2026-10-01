# GitHub 相关开源仓库调研 · RSI / 自改进 Agent

> 2026-10-01 调研。配合 [论文清单](README-论文清单.md)（26 篇 PDF 已重新下载到本目录，84.8 MB）。
> 所有仓库均通过 GitHub API 逐一核实存在；Star 数为当日值。

## 一、清单论文的对应代码仓库

| 论文（清单编号） | 仓库 | 类型 | Star |
|---|---|---|---|
| SPIN（2401.01335） | [uclaml/SPIN](https://github.com/uclaml/SPIN) | 官方实现 | 1.3k |
| Self-Rewarding LM（2401.10020） | [lucidrains/self-rewarding-lm-pytorch](https://github.com/lucidrains/self-rewarding-lm-pytorch) | 非官方 PyTorch 实现 | 1.4k |
| Self-Rewarding LM（2401.10020） | [Oxen-AI/Self-Rewarding-Language-Models](https://github.com/Oxen-AI/Self-Rewarding-Language-Models) | 社区复现 | 138 |
| Sycophancy→Subterfuge（2406.10162） | [anthropics/sycophancy-to-subterfuge-paper](https://github.com/anthropics/sycophancy-to-subterfuge-paper) | 官方（已归档） | 25 |
| Absolute Zero（2505.03335） | [LeapLabTHU/Absolute-Zero-Reasoner](https://github.com/LeapLabTHU/Absolute-Zero-Reasoner) | 官方 | 1.9k |
| Absolute Zero（2505.03335） | [ovshake/minAZR](https://github.com/ovshake/minAZR) | 极简复现 | 0 |
| The AI Scientist（2408.06292） | [SakanaAI/AI-Scientist](https://github.com/SakanaAI/AI-Scientist) | 官方 | 14.6k |
| （同主题 v2） | [SakanaAI/AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) | 官方后续 | 7.2k |
| AlphaEvolve（2506.13131） | [google-deepmind/alphaevolve_results](https://github.com/google-deepmind/alphaevolve_results) | 官方结果集 | 301 |
| AlphaEvolve（2506.13131） | [google-deepmind/alphaevolve_repository_of_problems](https://github.com/google-deepmind/alphaevolve_repository_of_problems) | 官方问题集 | 236 |
| AlphaEvolve（2506.13131） | [algorithmicsuperintelligence/openevolve](https://github.com/algorithmicsuperintelligence/openevolve) | 开源复现 | 7.5k |
| RRSI（2609.24972） | [google-research/rrsi](https://github.com/google-research/rrsi) | 官方 | 1.1k |

**未找到官方/对应代码**：V-STaR、RISE、Self-Taught Evaluators、SAIL、AREX、AIDE²、DemoEvolve、GameBoyWorlds、REUSE，以及 E/F 类多数 2026 年新论文（仅 arXiv 存在）。

## 二、RSI 资源清单类（快速追踪前沿）

| 仓库 | 说明 | Star |
|---|---|---|
| [lobehub/awesome-rsi](https://github.com/lobehub/awesome-rsi) | RSI 研究地图：模型 / agent / harness / 评估 | 372 |
| [theseus-labs-rsi/awesome-rsi](https://github.com/theseus-labs-rsi/awesome-rsi) | RSI 论文与资源列表 | 132 |
| [KaiWU5/Awesome-AI4AI](https://github.com/KaiWU5/Awesome-AI4AI) | 「AI 能否可靠改进 AI」综述，223 篇 | 178 |

## 三、RSI Agent / Harness 级开源项目（与 GameRSI MOD-06/07 最相关）

| 仓库 | 说明 | Star |
|---|---|---|
| [google-research/rrsi](https://github.com/google-research/rrsi) | **正则化 RSI of agent harnesses**——与 MOD-07「harness 候选生成 + held-out 裁判 + 回滚」直接对标的官方实现 | 1.1k |
| [EverMind-AI/Raven](https://github.com/EverMind-AI/Raven) | 「Harness of Harnesses」，面向 RSI 的可信持久自演化元框架 | 5.0k |
| [Human-Agent-Society/reef](https://github.com/Human-Agent-Society/reef) | 持续自改进 agent 的基础设施层 | 7.4k |
| [Arvid-pku/Godel_Agent](https://github.com/Arvid-pku/Godel_Agent) | Gödel Agent：自指框架的 RSI（arXiv 2410.04444） | 228 |
| [letta-ai/letta](https://github.com/letta-ai/letta) | 有状态 / 可自学习记忆的 agent 平台 | 25.0k |
| [aymenfurter/microagents](https://github.com/aymenfurter/microagents) | 轻量 self-improving agents | 826 |
| [YihongT/CoAutoResearch](https://github.com/YihongT/CoAutoResearch) | 自主研究系统 + 递归自改进 | 69 |
| [hankbesser/recursive-agents](https://github.com/hankbesser/recursive-agents) | 自改进 LLM 的元框架，强调可透明推理 | 46 |
| [sentrux/sentrux](https://github.com/sentrux/sentrux) | Rust 实时架构传感器，帮 agent 闭合反馈回路 | 3.3k |
| [lihouwenbin/ai-redteam-recursive-self-improvement](https://github.com/lihouwenbin/ai-redteam-recursive-self-improvement) | RSI 治理的领域无关红队框架（对 F 类安全议题有参考） | 35 |

## 四、自主科研闭环（D 类参考）

| 仓库 | 说明 | Star |
|---|---|---|
| [EvoScientist/EvoScientist](https://github.com/EvoScientist/EvoScientist) | 自演化 AI 科学家 | 5.0k |
| [ResearAI/DeepScientist](https://github.com/ResearAI/DeepScientist) | 深度科研 agent | 3.3k |
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) | AI 科学家技能库 | 47k |

## 五、与 GameRSI 的对标建议

项目核心是「游戏沙箱 + harness 自进化 + 不可作弊的 held-out 测量」，优先级建议：

1. **google-research/rrsi** —— 与 MOD-07 几乎同题：harness 自演化的正则化、如何避免退化、版本化与回滚。**最该精读源码的一座**。
2. **EverMind-AI/Raven / Human-Agent-Society/reef** —— 参考别人如何把「自进化 harness」做成可持久化、可审计的工程框架（MOD-06 断点续跑、MOD-09 审计）。
3. **lobehub/awesome-rsi** —— 持续追踪新论文与评测，补充本清单。
4. **algorithmicsuperintelligence/openevolve** —— 演化式代码生成的工程实现，对 MOD-07「harness 候选生成」的变异算子设计有借鉴。
5. **anthropics/sycophancy-to-subterfuge-paper** —— 奖励投机 / 测试篡改的真实样本，可直接用作 INV-1~4 蜜罐与审计的设计输入。
