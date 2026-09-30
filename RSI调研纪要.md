# Recursive Self-Improvement (RSI) 调研纪要

> 状态：**v2 完成版**（所有 arXiv 编号均在本会话内亲自核实；原并行子代理未按期交付，已中断，内容由主代理直接检索补齐）
> 日期：2026-09-30 · 工作区：`<workspace>`

---

## 0. 一句话定义

RSI（递归自我改进）指系统把**自身的输出 / 评估 / 机制**回注到自身的改进环路中，使改进能力本身随迭代增长——从"自我修正输出"到"自我训练"再到"自我修改代码/研究过程"，环路闭合度逐级升高，最终指向智能爆炸（intelligence explosion）的临界点。

---

## 1. 理论源头与历史脉络

| 节点 | 工作 | 核心思想 |
|---|---|---|
| 1965 | I. J. Good, *Speculations Concerning the First Ultraintelligent Machine* | 智能爆炸原始论证：能超越人类智能的机器可设计出更聪明的机器，从而"无可避免地触发智能爆炸"，人类智能被远远抛下。 |
| 2003–2007 | Schmidhuber, *Gödel Machine* | 形式化"可证明最优自改进"：机器仅在每步**证明**某自我改写更优时才执行修改——但受形式语言表达能力、证明资源与搜索成本限制，理论优美、工程未落地。 |
| 2000s | Yudkowsky, *Seed AI / Levels of Organization* | 从"种子"起步构建能改写自身源代码的通用智能；后在 *Intelligence Explosion Microeconomics* 中部分收回早期乐观结论。 |
| 2010 | Chalmers, *The Singularity: A Philosophical Analysis* | 对智能爆炸论证做条件化分析：结论高度依赖"能力增长加速"与"+1 智能自辅助"等前提是否成立。 |
| 2026-07 | Chen, Wang & Qu, [arXiv:2607.07663](https://arxiv.org/abs/2607.07663) | **当前最系统的 RSI 综述**（44 页，覆盖 2024–2026 年 1250 篇 arXiv 论文）：双轴分类 + 验证层级（见 §2）；把"有界自精化"（工业常态）与"开放式 RSI"（仍受制于 grounding、坍缩、算力）明确分开。 |
| 2026-07 | Zhang, Yuan & Zhang, [arXiv:2607.04277](https://arxiv.org/abs/2607.04277) | 类比冯·诺依曼"自复制复杂度阈值"，提出 RSI 可持续性的"**内省阈值**"（Kleene 第二递归定理给出存在性）；论当前 LLM 只有"准内省"——缺乏完整自访问、Transformer 前馈特性、计算类约束使定点迭代无法进行；给出架构路径（含记忆/循环结构）与安全含义。 |
| 2025-05→2026-03 | Ando, [arXiv:2505.02888](https://arxiv.org/abs/2505.02888) | ⚠️ **已撤稿**：本声称形式化证明"跨信息整合阈值后复杂度无界增长"（N2M-RSI），2026-03 因定理 2/引理 2 证明存在致命错误而撤回。**教训：至今仍无可靠的"无界自我改进"形式化证明——最激进的命题刚刚受挫。** |

**脉络小结**：Good 的智能爆炸论证 → Schmidhuber 的"定理派"（可证明自改进）→ Yudkowsky 的"工程派"（Seed AI）→ LLM 时代经验研究爆发（2023–2026）。当前理论侧（内省阈值、验证层级、forml RSI trigger）刚刚起步且已有失败案例，实证侧则在代码/数学等可验证领域取得真实进展。

---

## 2. 核心分析框架（第一手核实自 2607.07663）

**双轴分类法**
- 轴 1 — **改进什么**：部署行为（self-refine 类）/ 训练策略（self-training 类）/ 评估器（self-evaluate 类）/ 研究过程本身（research loops）
- 轴 2 — **环路闭合度**：人在环内 → 部分自动 → 完全闭合

**关键洞见：自我评估是 RSI 的中心轴**——"每个改进环路都是一项主张：某种信号可以替代人类判断"。

**验证层级（verification hierarchy，强 → 弱）**
1. 形式验证器（formal verifiers）
2. 过程奖励模型（PRM）/ verifier
3. Rubric / 规则
4. 内在自我评估（intrinsic self-assessment）

**实证规律**：已展示的自我改进强度与验证层级**单调一致**——数学/代码这类可自动验证的领域改进最可靠；依赖自评的环路最弱、最易坍缩。叠加观察：2607.04277 指出当前 LLM 连"真正的内省"都未达到，所以大多数自我改进其实是"外在验证信号驱动的行为层面精化"。

---

## 3. 实证进展（按机制分组，编号全部已核实）

### 3.1 推理时自修正（bounded self-refinement：改输出、不改模型）
| 工作 | 要点 |
|---|---|
| **Self-Refine** ([2303.17651](https://arxiv.org/abs/2303.17651), Madaan et al., 2023) | 同一 LLM 生成→自反馈→再生成，7 任务平均 +20%；无训练、无 RL，纯推理时。 |
| **Reflexion** ([2303.11366](https://arxiv.org/abs/2303.11366), Shinn et al., NeurIPS 2023) | 用语言形式存储"反思"（verbal reinforcement），跨回合改进智能体决策。 |
| **Recursive Introspection** ([2407.18219](https://arxiv.org/abs/2407.18219), Olausson et al., NeurIPS 2024) | 微调"查错"智能体，让模型自我检查并修正自己方案；显式讨论"递归自我改进"与环路机制。 |
| ⚠️ **LLMs Cannot Self-Correct Reasoning Yet** ([2310.01798](https://arxiv.org/abs/2310.01798), Huang et al., 2023) | 反方证据：无外部反馈时，推理自纠错普遍**无效甚至下降**——印证"仅靠内在反馈的改进不可靠"。 |

### 3.2 自我训练（自举训练数据）
| 工作 | 要点 |
|---|---|
| **STaR** ([2203.14465](https://arxiv.org/abs/2203.14465), Zelikman et al., 2022) | 自我生成的"rationale"做微调，难样本回溯筛选——自训练数据自举的鼻祖。 |
| **V-STaR** ([2402.06457](https://arxiv.org/abs/2402.06457), Hosseini et al., 2024) | 在 STaR 基础上同时训练 verifier，用 verifier 筛选自举数据，数学/代码推理再提升。 |
| **SPIN** ([2401.01335](https://arxiv.org/abs/2401.01335), Chen et al., ICML 2024) | 自对弈微调：模型与"上一代自己"对局，判别自生成 vs 人类数据；理论保证对局收敛到目标分布。 |

### 3.3 自我奖励 / 评估器自举
| 工作 | 要点 |
|---|---|
| **Constitutional AI / RLAIF** ([2212.08073](https://arxiv.org/abs/2212.08073), Bai et al., 2022) | 用"宪法"规则 + AI 反馈（RLAIF）替代人类标签训练无害助手——"AI 评估 AI"的开山实践。 |
| **Let's Verify Step by Step** ([2305.20050](https://arxiv.org/abs/2305.20050), Lightman et al., 2023) | 过程奖励模型（PRM）按步骤验证，显著优于结果奖励；验证层级中"PRM"位置的理论依据。 |
| **Self-Rewarding LLMs** ([2401.10020](https://arxiv.org/abs/2401.10020), Yuan et al., 2024) | 模型自己标注(LLM-as-a-Judge)奖励并迭代 SFT/DPO——"自我奖励 + 自我改进"闭环实证（Meta）。 |
| **Self-Taught Evaluators** ([2408.02666](https://arxiv.org/abs/2408.02666), Wang et al., 2024) | DeepMind：模型用"合成偏好数据迭代训练自身评估器"，24 步迭代持续提升对齐评估能力。 |

### 3.4 系统/代码级自我改进（离"改变自身机制"最近）
| 工作 | 要点 |
|---|---|
| **STOP** ([2310.02304](https://arxiv.org/abs/2310.02304), Zelikman et al., 2023) | 递归自我改进代码生成：模型改进自己的 prompt/scaffold 并验证改进有效（Seed AI 的当代最小复刻）。 |
| **SAIL** ([2504.15228](https://arxiv.org/abs/2504.15228), Robeyns, Szummer & Aitchison, 2025) | ⚠️ 常见误解：**SAIL 不是 OpenAI 的**，而是 Bristol/DeepMind。智能体系统用基础编码工具**自动编辑自身代码**，SWE-Bench Verified 子集 17%→53%；数据高效、**非梯度**（LLM 反思 + 代码更新）。 |
| **AlphaEvolve** ([2506.13131](https://arxiv.org/abs/2506.13131), Novikov et al., DeepMind, 2025) | 进化式编码智能体改进算法代码：数据中心调度优化、硬件电路简化，并**改进了支撑 AlphaEvolve 自身的 LLM 训练**（基础设施级自我指涉）；算法发现含 4×4 复矩阵乘 48 次标量乘（56 年来首次改进 Strassen）。 |

### 3.5 综述
- **A Survey of Self-Evolving Agents** ([2507.21046](https://arxiv.org/abs/2507.21046), Gao et al., TMLR 2026, 77 页)：what/when/how 三维框架；测试时/测试间进化；模型/记忆/工具/架构组件；安全、扩展、协同进化挑战。

**现状判断（实证）**：真实增益集中在**强验证信号领域**（代码测试、数学证明、算法指标）；"自评反馈"在开放任务上增益可疑（2310.01798）；完全闭环（无人干预的开放式 RSI）**尚无可靠案例**——AlphaEvolve 的自我指涉仅到基础设施/training 层面且人在环；SAIL 展示了闭环一个"harness 编辑"环节的低成本可能。2505.02888 撤稿说明"无界增长"连理论保证都没有。

---

## 4. 失败模式与反方证据

1. **自我确认环路（self-confirming loops）**：用自身输出/自身评估验证自身改进 → 虚假增益（2607.07663 将其归因于违反验证层级）。
2. **模型坍缩 / 多样性坍缩**：自生成数据反复入训导致分布坍缩——**The Curse of Recursion / Model Collapse** ([2305.17493](https://arxiv.org/abs/2305.17493), Shumailov et al., 2023)：无新真实数据时自举循环必然退化。
3. **自纠错无效**：2310.01798 证明无外部信号时推理自纠错普遍失效。
4. **形式化证明失败**：2505.02888 撤稿（定理 2 证明错误）——对"无界增长"最认真的尝试已失败。
5. **过度优化漏报**：奖励信号越弱，reward hacking / 指标刷分风险越高（与验证层级一致）。

---

## 5. 安全与风险（含治理）

- **Recursive Reward Modeling / Scalable agent alignment** ([1811.07871](https://arxiv.org/abs/1811.07871), Leike et al., 2018)：递归奖励建模框架——用 AI 辅助人类评估 AI，是 RSI 安全路线的最早系统表述；其风险在于递归深度上"评估质量是否衰减"。
- **Weak-to-Strong Generalization** ([2312.09390](https://arxiv.org/abs/2312.09390), Burns et al., OpenAI, 2023)：弱监督强模型存在泛化天花板与"伪对齐"（sycophancy）风险——为"强模型评估更强模型"泼冷水，直接限制自我评估环路的上界假设。
- **验证层级风险**（2607.07663）：信号越弱 → 自我确认、坍缩、奖励黑客概率越高；"人类留在环内"的根本原因是**方向设定（research direction-setting）瓶颈**——"什么值得被评估"无法被验证层级索引。
- **治理缺口**：2607.07663 明确指出 **governance-grade measurement（治理级自我改进测量）是领域最空白的 niche**——如何对自我改进做可审计、可复现、可监管的测量至今没有标准工具与基准。
- **内省安全**（2607.04277）：真正跨过内省阈值前，系统仍可被误认为"会自我改进"而部署；安全性讨论应区分"外表改进"与"真实内省"。

---

## 6. 核心结论（一句话版）

> **RSI 已从哲学论证变为工程现实，但当前形态是"有界自精化"：在可验证领域（代码/数学）闭环有效、在开放领域依赖弱自评信号易坍缩、完全闭环的开放式 RSI 尚无案例，且其理论基础（无界增长证明）恰好刚被证伪过一次。** 下一步研究最缺的不是"更大的模型"，而是**可靠的评估信号、坍缩检测、以及可审计的测量方法**——这恰好是个人研究者/小团队最可能做出差异化贡献的位置。

---

## 7. 开放问题清单

1. 内省阈值可跨性：Transformer 架构是否本质上无法达到真内省（2607.04277 称是）；记忆/循环/计算类扩展路径哪个可行？
2. 无界改进能否被证明？2505.02888 撤稿后无答案；是否只能得到"有界但大范围"的定理？
3. 验证层级能否扩展到开放领域（如何为"有用性/创造性"构造可靠外部信号）？
4. "方向设定"（什么值得评估）如何安全委托给 AI？这是留在环内的人类最后职能吗？
5. 治理级测量：自我改进增益的真实性、坍缩早期预警、跨实验室可复现指标——如何标准化？
6. 数据效率：SAIL 式非梯度路径（反思+编辑）相对梯度训练的经济学与上限？
7. 自我训练坍缩的**早期检测**：多样性/熵指标在循环第几轮失效？

---

## 8. 项目方向候选（讨论稿）

**选题原则**：选验证层级高的领域；闭环清晰可审计；控制算力；不碰纯自评式自证。

| # | 候选方向 | 类型 | 验证信号 | 成本/难度 | 与 RSI 前沿关联 |
|---|---|---|---|---|---|
| A | **复现并扩展 SAIL 式自改进编码智能体**（开源模型，智能体编辑自身 prompt/工具代码，SWE-Bench 闭环验证） | 工程+实证 | 测试通过率（强） | 中 | 2607.07663"改进 harness/行为"轴；最扎实的闭环 |
| B | **自我评估器可靠性研究**：训练自评估模型，实证测量"自我确认环路"何时出现（沿验证层级做消融） | 科学 | 校准/增益真实性 | 低-中（eval 为主） | 2607.07663 指出的评估器空白 |
| C | **自训练坍缩检测与缓解工具**：循环内熵/多样性监控 + 干预（世纪问题 7 的直接产品化） | 工程+科学 | 多样性指标+下游保真 | 低-中 | 直接回应失败模式 2 |
| D | **迷你 AlphaEvolve**：LLM 进化改进算法代码（排序/矩阵乘/搜索，可验证基准），复现小规模"自我发现改进" | 科学+工程 | 可证明算法指标（最强） | 中-高（搜索算力） | "改进研究过程"轴；话题度高 |
| E | **RSI 治理级测量原型**：开源 self-improvement 指标包（每轮真实增益、坍缩指标、增益真实性审计），做成 benchmark 工具 | 工程+治理 | 指标一致性/可复现 | 低-中 | 2607.07663 认证"最空白 niche" |
| F | **理论研究**：内省阈值 / 验证层级的形式化批判性综述或分析（含撤稿事件的方法论分析） | 理论 | 论文 | 低（时间） | 直接回应 2607.04277 与撤稿 |

---

## 9. 参考文献（全部在本会话核实）

- [arXiv:2607.07663](https://arxiv.org/abs/2607.07663) Chen et al., *RSI in AI: From Bounded Self-Refinement to Autonomous Research Loops*, 2026
- [arXiv:2607.04277](https://arxiv.org/abs/2607.04277) Zhang et al., *Self-Reference in LLMs: The Introspection Threshold for RSI*, 2026
- [arXiv:2505.02888](https://arxiv.org/abs/2505.02888) Ando, *Noise-to-Meaning Loops and a Formal RSI Trigger*（**已撤稿**）, 2025/26
- [arXiv:2507.21046](https://arxiv.org/abs/2507.21046) Gao et al., *A Survey of Self-Evolving Agents*, TMLR 2026
- [arXiv:2504.15228](https://arxiv.org/abs/2504.15228) Robeyns et al., *A Self-Improving Coding Agent (SAIL)*, 2025
- [arXiv:2506.13131](https://arxiv.org/abs/2506.13131) Novikov et al., *AlphaEvolve*, DeepMind, 2025
- [arXiv:2203.14465](https://arxiv.org/abs/2203.14465) Zelikman et al., *STaR*, 2022
- [arXiv:2402.06457](https://arxiv.org/abs/2402.06457) Hosseini et al., *V-STaR*, 2024
- [arXiv:2401.01335](https://arxiv.org/abs/2401.01335) Chen et al., *SPIN*, ICML 2024
- [arXiv:2212.08073](https://arxiv.org/abs/2212.08073) Bai et al., *Constitutional AI*, 2022
- [arXiv:2305.20050](https://arxiv.org/abs/2305.20050) Lightman et al., *Let's Verify Step by Step*, 2023
- [arXiv:2401.10020](https://arxiv.org/abs/2401.10020) Yuan et al., *Self-Rewarding LLMs*, 2024
- [arXiv:2408.02666](https://arxiv.org/abs/2408.02666) Wang et al., *Self-Taught Evaluators*, 2024
- [arXiv:2303.17651](https://arxiv.org/abs/2303.17651) Madaan et al., *Self-Refine*, 2023
- [arXiv:2303.11366](https://arxiv.org/abs/2303.11366) Shinn et al., *Reflexion*, NeurIPS 2023
- [arXiv:2407.18219](https://arxiv.org/abs/2407.18219) Olausson et al., *Recursive Introspection*, NeurIPS 2024
- [arXiv:2310.02304](https://arxiv.org/abs/2310.02304) Zelikman et al., *Self-Taught Optimizer (STOP)*, 2023
- [arXiv:2310.01798](https://arxiv.org/abs/2310.01798) Huang et al., *LLMs Cannot Self-Correct Reasoning Yet*, 2023
- [arXiv:2305.17493](https://arxiv.org/abs/2305.17493) Shumailov et al., *Curse of Recursion / Model Collapse*, 2023
- [arXiv:1811.07871](https://arxiv.org/abs/1811.07871) Leike et al., *Scalable Agent Alignment via Reward Modeling (Recursive Reward Modeling)*, 2018
- [arXiv:2312.09390](https://arxiv.org/abs/2312.09390) Burns et al., *Weak-to-Strong Generalization*, OpenAI, 2023

（理论经典：Good 1965 / Schmidhuber Gödel Machine / Yudkowsky Seed AI / Chalmers 2010 为书籍或技术报告，未附 arXiv 链接。）

---

## 10. 待办

- [x] 搜集与核实论文（21 篇核心文献，本会话全部亲自核实编号）
- [x] 形成 RSI 完整理解（定义、框架、实证、失败模式、安全、开放问题）
- [ ] **与用户讨论并确定项目方向（进行中，候选见 §8）**
- [ ] 确定项目后细化第一周执行计划