# RRSI 参考项目深度分析（服务 GameRSI MOD-06/07/08）

> 分析对象：RRSI《Regularized Recursive Self-Improvement of Agent Harnesses》(arXiv:2609.24972v2)
> 代码：`ref/rrsi`（google-research/rrsi，Apache 2.0）· 论文全文：`ref/paper-text/rrsi.txt`
> 报告日期：2026-10-01 · 用途：GameRSI MOD-07 evolve 的机制选型与降级方案

---

## 1. 项目速览

RRSI 与 GameRSI 的 MOD-07 几乎同题：**在冻结的基座模型上演化 harness**（prompt / 控制流 / 工具 / 记忆 / 上下文管理），把「harness」而非「权重」当作优化变量。RRSI 的核心贡献不是演化本身，而是**正则化搜索轨迹**（regularizing the search trajectory）：不限制「能改什么」（edit space 保持开放），只限制「怎么在搜索空间里移动」。

| 维度 | RRSI | GameRSI |
|---|---|---|
| 冻结策略 | Claude Opus 4.8（API） | 本地 8B（MLX） |
| 演化对象 | harness 源码包（任意 Python、新模块、skill/memory/subagent） | `harness/prompts/*.md` + `harness/tools/*.py` + `harness/config.json`（≤2 文件 / 40 行 / 4000 字符） |
| 选择用的数据集 | **evolve set 本身**（与演化集同一批任务，自适应复用） | **heldout 篮子 H1**（heldout:2000–2039，40 局，INV-5 隔离） |
| 每轮候选 | m=2，T=20–40 轮（40–80 个候选/run） | C=2，M3 只跑 3 代（6 个候选） |
| 每次评估规模 | 61–120 任务 × k=2–4 试（约 244–9,600 局/轮） | 40 局/次 |
| 人在环 | 无 | HITL 门（`evolve.hitl=true`） |
| 候选代码执行 | 直接在 git worktree 里跑（无沙箱），唯一闸门是 critic | MOD-02 `guard.gate()` AST 扫描 + 子进程 worker（INV-4） |

**一句话定位**：RRSI 解决的是「演化会在 evolve set 上过拟合、增益不迁移」；GameRSI 用 held-out 选择（INV-5）在结构上已经规避了 RRSI 最尖锐的那个问题，但**完全没有 RRSI 的搜索轨迹正则化**——这恰是 RRSI 消融里价值最大的部分。两者正交，可叠加。

---

## 2. 论文核心

### 2.1 问题定义

论文把 harness 演化形式化为「自适应经验优化」：第 t 轮在 evolve set `D_evolve` 上跑当前 harness、汇总反馈 `F_t`、让 proposer 生成候选、在**同一个** `D_evolve` 上评估、取最优者继任（论文 Eq. 2）。关键在于 `D_evolve` 被**自适应复用**（adaptive reuse）：第 t 轮的候选依赖于前几轮在同一批任务上的测量。论文明确引用了 adaptive data analysis 的经典结论（Dwork et al., 2015）作为动机。

三种耦合的失败模式被列为一等公民（论文 §1）：

1. **Benchmark-specific fitting**：搜索把 benchmark 特有模式编码进 harness；
2. **Noise chasing**：候选被评估噪声推着走（"promote candidates favored by the evaluation noise"）；
3. **Complexity accumulation**：累积复杂度，只抬高 evolve 分数而不改进底层机制。

这三者共同拉大 evolve→transfer 的差距。**这与 GameRSI 列为头等失败模式的「自我确认环路 / 奖励投机 / 模式坍缩」高度对应**：noise chasing ≈ 自我确认（把随机波动当成进步），benchmark-specific fitting ≈ 奖励投机/环境作弊，complexity accumulation ≈ 模式坍缩的一种成因。

### 2.2 基线方法为何过拟合

论文的核心诊断：标准演化「选择经验分数最高的候选」，即使该分数反映的是泄漏、随机波动或代价增长。四个基线（Meta-Harness、AHE、TTHE、HarnessX）都从同一个 `H0` 出发、共享冻结策略、evolve set 和候选预算。它们的 evolve set 分数都很高，但 OOD 排名反转（见 §2.5）。

### 2.3 RRSI 的正则化机制（逐条）

论文把正则化类比为三种经典稀疏化方法，但明确声明**只是类比、不优化对应的范数惩罚目标**（论文 §3.1 / Appendix C）。

#### 提案侧（Proposal side）——约束「搜索容量怎么花」

**(A) L0 式退火更新稀疏性（Annealed update sparsity）**

一个候选里可捆绑的「独立可归因编辑数」有上限，并随轮次余弦退火（论文 Eq. 4）：

```
b_t = b_min + (b_max - b_min) · ½(1 + cos(πt/T))
```

早期允许捆绑若干协同改动以发现新机制，后期越来越稀疏、可归因。论文强调：`b_t` 约束的是**单次更新的基数**（`||z_t||_0`），不是「harness 最终能含哪些机制」。

**(B) 证据感知的信用分配（Evidence-aware credit assignment）**

对每个被评估过的候选，记录：改动的组件 ℓ、检验的假设 h、源码 diff d、分数变化 ΔS、代价变化 ΔC、是否被接受 a（论文 Eq. 10）。提案器在后续轮次条件化于这段历史：被拒绝的机制是**负证据**，成功的机制保留显式信用。因为后期 `b_t` 退火到 1，归因变得越来越干净。

**(C) 结构化探索（Structured exploration）**

历史还暴露「搜索是否塌缩到窄编辑族」（例如反复改 prompt 而从不碰结构机制）。**停滞判定**：过去 w 轮的进展落在经验噪声带 δ 内即视为停滞（`σ_t = 1[S_t − S_{t-w} ≤ δ]`）。停滞期间，把一部分提案预算**预留**给从未被练习过的组件（`U_t = K \ T_t`）。作用类似多样性/熵正则。

#### 选择侧（Selection side）——约束「哪些增益可以变成永久状态」

候选必须先通过若干**不可补偿**（non-compensatory）准则，其分数才有资格替换现任。

**(D) 泄漏筛查（Leakage screening）**：评估之前，critic 读候选 diff，拒绝显式编码任务名/实体名/任务特有取值/答案/其他 evolve-benchmark 特有逻辑的改动，以及添加惰性机械（inert machinery）的改动。**评估前筛查很关键**：泄漏候选永远不会拿到那个虚高的 evolve 分数，从而不会在后续轮次变得有吸引力。

**(E) 稳定性感知接受（噪声调整下限，Stability-aware acceptance）**：演化开始前反复评估不变的基础 harness，估计经验噪声带 δ。候选必须满足 `Ŝ(H') ≥ S★ − δ`（论文 Eq. 5），其中 `S★` 是迄今观察到的最优 evolve 分数。这阻止搜索「沿着一连串 individually 小到被误认为噪声的回归走下坡」。

**(F) Ridge/L2 式复杂度感知接受**：对增益超过噪声带的候选（ΔS > δ），要求（论文 Eq. 7）

```
ΔC ≤ β0 + β1·ΔS        （ΔC = 相对代价变化 = (Ĉ(H') − Ĉ(H_t)) / Ĉ(H_t)）
```

`β0` 是「几乎无增益时仍可容忍的代价增幅」，`β1` 是「随测量增益增大的代价配额」。即额外推理代价必须由可测量的性能提升来支付。对增益落在噪声带内的候选（ΔS ≤ δ），改用整形可接受条件（论文 Eq. 17）：

```
w_s·ΔS − w_c·ΔC + w_n·ν_t(H') > 0
```

其中 `ν_t` 只对**结构性组件**计数（`K_str = {client_tool, skill, memory, subagent}` 中现任从未有过被接受编辑的类型）。**coding 实例置 `w_s = 0`**：落在 δ 内的分数提升本身不足以让候选可接受，必须靠降代价或结构新颖性挣到足够信用。

**(G) Lasso/L1 式结构剪枝**：RRSI 跟踪最近被练习过的组件在固定剪枝窗口内是否产生过**严格正**的测量增益；没有产出的组件被作为删除目标交给提案器。机制必须持续「挣到自己的位置」，而不是因为「只看分数的演化没有动机删它」就苟着。

**(H) 领域特有不可补偿守卫**：engineering-design 实例额外拒绝「有效输出率相对现任下降 > 0.03」或「无提交率上升 > 0.02」的候选——防止主目标（通过率）的提升被基本执行有效性的显著恶化抵偿掉（论文 Appendix C.3）。

### 2.4 实验设置

- **三个 domain、八个 benchmark**：coding（Terminal-Bench 2.1 = 89 个容器化 terminal 任务 → SWE-bench Verified 作 OOD）；agentic workspace（Harvey LAB = 25 个执业领域、固定 120 任务 evolve set + 40 任务 ID held-out → JobBench / GDPval / APEX-Agents 作 OOD）；engineering design（EngDesign = 61 个任务，每个由自己的冻结模拟器评分 → Frontier-Eng 作 OOD）。
- **冻结策略**：Claude Opus 4.8；proposer、写跨轮反馈的 analyst、泄漏 critic 也都是 Claude Opus 4.8；Harvey LAB 的裁判是 Gemini 3.5 Flash。
- **基座 harness**：coding 用 Terminus-2；workspace/eng 用 ReAct over MCP gateway + dynamic toolbelt + ReSum 式上下文管理。
- **关键协议细节**：所有数字都在**同一窗口**内对着未演化的 `H0` 测量，因此增益不能归因于评估基础设施的漂移（论文 §4.2）。APEX 的缺失 rollout 一律计为失败而非排除，「防止在难世界上崩溃的 harness 看起来比尝试它的 harness 更好」。

### 2.5 关键定量结论

**主结果（vs 同窗口 `H0`）**

| Domain | Benchmark | 角色 | H0 | RRSI | Δ |
|---|---|---|---|---|---|
| Coding | Terminal-Bench 2.1 | evolve | 74.2 | 80.2 | **+6.0** |
| Coding | SWE-bench Verified | OOD | 82.0 | 83.8 | +1.8 |
| Agentic | Harvey LAB | evolve | 89.4 | 90.5 | +1.1 |
| Agentic | Harvey LAB（ID held-out） | ID held-out | 86.9 | 89.2 | +2.3 |
| Agentic | JobBench | OOD | 36.0 | 40.7 | **+4.7** |
| Agentic | GDPval | OOD | 48.8 | 52.3 | +3.5 |
| Agentic | APEX-Agents | OOD | 34.2 | 37.9 | +3.7 |
| Eng | EngDesign | evolve | 50.0 | 54.9 | +4.9 |
| Eng | Frontier-Eng | OOD | 17.7 | 22.0 | +4.3（相对 +24.3%） |

没有任一 held-out 分裂出现回归。OOD 增益 3.5–4.7 点，相当于 7.2%–13.1% 的相对提升；RRSI 比「先前基线的平均值」高出最多 **22.9%**。

**与四个先前方法对比（agentic workspace 实例，论文 Table 1）**

| Method | Harvey LAB (Evolve) | ID Held-out | JobBench | GDPval | APEX |
|---|---|---|---|---|---|
| H0 | 89.4 | 86.9 | 36.0 | 48.8 | 34.2 |
| Meta-Harness | **93.0** | 89.2 | 37.1 | 49.1 | 35.7 |
| AHE | 90.7 | 88.7 | 37.2 | 47.2 | 33.1 |
| TTHE | 91.1 | 88.5 | 35.2 | 47.0 | 31.7 |
| HarnessX | 91.8 | 89.1 | 36.3 | 48.5 | 34.3 |
| **RRSI** | 90.5 | 89.2 | **40.7** | **52.3** | **37.9** |

OOD 平均：RRSI **43.6** vs H0 **39.7**。Meta-Harness 在 evolve set 上最强（+3.6），OOD 只 +0.9；HarnessX 落回基线；AHE 与 TTHE **低于起步时的 harness**（TTHE 低 1.7 点）。论文原话：RRSI 是所有已演化 harness 里 **evolve set 增益最小的**，却是唯一一个 OOD 平均值能甩开 `H0` 一点以上的（43.6 vs 39.7）——「这正是正则化器要做的那笔交易」。

**这是给 GameRSI 最重要的单条结论**：演化越狠、evolve 涨得越多、OOD 越接近「没演化」。M3 用「heldout 主指标单调不降」做验收是对的，但**train 分数涨得快本身是警报，不是进步**。

**代价**（论文 Figure 4 / §4.3）：AHE 每试 **3.82M** policy tokens，比 RRSI（2.42M）多 **58%**，OOD 还低 4.4 点；RRSI 每试 26.3 步，先前方法 27.3–34.6 步。但**没有任何已演化 harness 比 `H0` 便宜**（H0 = 1.56M tokens / 21.2 步）：「演化确实用测试时算力买到了一部分增益，预算决定买多少」。→ 没有代价规则的演化必然膨胀。

**策略鲁棒性**（论文 Table 3/4）：把 coding 演化换成 Gemini 3.5 Flash 冻结策略，Terminal-Bench 64.6 → 78.7（**+14.1**），SWE-bench 76.8 → 79.0（+2.2）；把用 Gemini 3.5 Flash 搜出来的 harness 原样交给从未参与搜索的更小模型 Gemini 3.1 Flash Lite，Terminal-Bench 从 11.2 → 14.6（+3.4，相对 **+30.4%**）。→ 正则化搜出来的机制不绑定基座能力等级。

### 2.6 消融结果（论文 Table 2，agentic workspace 实例）

| Variant | Evolve | ID Held-out | OOD Avg | Tokens/trial (M) ↓ |
|---|---|---|---|---|
| H0（不演化） | 89.4 | 86.9 | 39.7 | 1.56 |
| 完全去正则演化 | **92.8**（全场最高） | 88.9 | **40.3** | 3.80 |
| 去掉提案侧正则 | 90.7 | 88.8 | 41.9 | 2.69 |
| 去掉接受侧正则 | 91.5 | 88.7 | 41.0 | 3.59 |
| **RRSI** | 90.5 | **89.2** | **43.6** | **2.42** |

关键拆解：
- **完全去正则演化 = 白演化**：evolve 涨 3.4 点，OOD 只比 H0 高 0.6 点，代价却是 2.4 倍。
- **去掉接受侧正则**：evolve 90.5→91.5，OOD 43.6→41.0，token 代价涨一半——「不受约束的选择规则把大部分被接受的编辑花在了噪声和上下文上」。
- **去掉提案侧正则**：evolve 只掉 0.2 点，OOD 掉 **1.7** 点——「即使什么都不拒绝，引导搜索往哪看也很重要」。
- 相对 H0：接受侧正则 +0.7 OOD（39.7→41.0 中的 41.0），提案侧 +1.6（→41.9），两者叠加 +3.3（→43.6）。**提案侧正则的边际收益比接受侧更大**，而 GameRSI 目前**完全没有提案侧正则**。

**case study（论文 Table 6）**：Coding R0-A（有界的完成前验证审计 + 非阻塞轮询）被接受，evolve +3.93 点；R0-B（相似机制、增益更小但更费 token）被代价规则拒绝（+1.69 点 / +26.1% 代价）；R8-B（把任务说明钉进完成门）**虽然省 13.6% token 仍被噪声下限拒绝**（−2.81 点）；Engineering R2（针对 "workdir must be an existing directory" 报错的有界恢复提示）被接受（122/244 → 128/244，+1.6% token）。→ 「演化不只由分数驱动：候选的具体性、评估稳定性、推理代价共同决定一个改动是否被保留。」

### 2.7 超参数（论文 Table 5）

| 超参 | 含义 | Coding | Agentic | Eng |
|---|---|---|---|---|
| T | 演化轮数 | 20 | 20 | 40 |
| k | 每任务试次 | 2 | 2 | 4 |
| δ | 经验噪声带 | 0.017 | 0.004 | 0.020 |
| b_min / b_max | 编辑预算上下限 | 1 / 4 | 1 / 3 | 1 / 4 |
| w | 停滞窗口 | 3 | 3 | 3 |
| m_draft | 预留探索提案数 | 1 | 1 | 1 |
| n_prune | 剪枝窗口 | 4 | 4 | 5 |
| β0 | 基础代价配额 | 0.10 | 0.10 | 0.15 |
| β1 | 增益相关代价配额 | 44.5 | 35.4 | 24.4 |

可解释单位：δ 在 coding 上 = 89×k=178 试中的 3 次通过；agentic = 约 14,100 条 criteria 判决中的 60 条；eng = 61×k=244 试中的 5 次通过。β1 在 coding 上 = 每多一次通过允许 25% token，agentic = 每 100 条 criteria 25%，eng = 每次通过 10%。所有值**只用 evolve 环境选定**，从不调 held-out/OOD。

### 2.8 论文自陈的局限

1. 只研究冻结基座的 harness 级 RSI，**不涉及演化期间更新模型权重**的情形（GameRSI MOD-10 正是后者，RRSI 不提供参考）。
2. **仍依赖有限的 evolve set 和若干正则化超参数**，有效性可能取决于反馈信号质量与所选搜索预算。
3. 跨 domain/benchmark/policy 的迁移已验证，但「更广泛地推广到显著不同的 agent 架构、工具生态、更长运行的自改进过程」仍需验证。

---

## 3. 代码级机制拆解

### 3.1 目录结构

```
rrsi.py                        CLI 入口（baseline/calibrate/round/run/readjudicate/reevaluate/heldout/smoke/status）
rrsi/
  config.py                    RRSIConfig：论文全部超参 + 工程旋钮（repair_rounds、invalid_missing_frac、n_fail_traces…）
  domain.py                    Domain 接口：核心从不读轨迹格式或 benchmark 目录
  loop.py                      Run：一轮的 7 步编排、断点续跑、readjudicate/reevaluate
  driver.py                    顺序驱动：STOP 文件、轮次幂等、连续基础设施失败熔断
  schedule.py                  退火编辑预算 b_t（纯函数）
  history.py                   编辑历史 L_t + T_t / g_t / B_t / σ_t / E_t（纯函数 + JSONL）
  components.py                组件词表 K / K_str、diff→组件归一化、新颖度 ν_t
  propose.py                   正则化提案器（严格 JSON 动作协议的 agentic 编辑循环）
  critic.py                    泄漏筛查（确定性 denylist + LLM 复审 + 有界修复）
  analyst.py                   Analyze(H_t, D)：调度只读 digester、聚合三透镜报告
  digester.py                  单轨迹只读 digest 子 agent
  evaluate.py                  Evaluate(H', D, k)：S_hat / C_hat 聚合
  calibrate.py                 噪声带 δ 的两种标定方式
  selection.py                 Algorithm 2：下限 / 代价规则 / 领域守卫 / argmax（纯函数）
  gitops.py                    worktree 候选隔离、diff 收集、fast-forward
  llm.py                       Claude 客户端（轮询 GCP 项目重试、ephemeral cache）
domains/<coding|workspace|eng>/
  adapter.py                   Domain 实现（run/score/load_trial/render_trace/task_row/smoke/guards/critic_patterns/component_signals）
  briefs.py                    四个搜索角色的领域文案
  SKILL.md                     提案器「宪法」（硬规则、杠杆清单、两个陷阱）
  PATTERNS.md                  机制模式库（参考而非允许清单）
  rrsi.json                    该实例的超参
  data/split_*.json            evolve/held-out 切分（workspace 由 split_workspace.py 确定性生成，pin 住上游 commit）
tests/test_core.py             不依赖 API/benchmark 的方法核心单测
```

**核心设计原则**（`rrsi/domain.py:28-43`）：`rrsi` 核心从不跑 agent、判分发物或读轨迹格式——全部由 domain adapter 提供。这使得三个实例共用同一份约 3,300 行的方法核心。**GameRSI 应直接借鉴这个分层**：MOD-07/08 只依赖一个稳定的「评估面」接口，游戏侧事实全部留在 MOD-01/02。

### 3.2 核心类与函数

**提案侧**

- `rrsi/schedule.py:43-50` — `edit_budget(t, T, b_min, b_max)`：论文 Eq. 4 的 `ceil` 版本，`round(v, 9)` 防止 `t = T` 时浮点误差把 1.0000000002 变成 2。这类细节在短 run 上会咬人。
- `rrsi/history.py:77-99` — `append_candidate()`：**一个 JSONL 记录对应一个编辑**；一个捆绑了 n 个编辑的候选只有一次测量（ΔS, ΔC, a），捆绑内的所有编辑共享它——「随着 b_t 退火到 1，记录就变成关于单一组件的证据」。
- `rrsi/history.py:113-119` — `measured()` / `tried()`：`T_t` 只统计**有测量**的编辑；critic 拒绝 / smoke 失败 / 评估无效的候选以 `delta_S = None` 记录，**不进入 `T_t` 或 `g_t`**。
- `rrsi/history.py:143-150` — `yield_g(t, n_prune)`：`g_t(ℓ)` = 剪枝窗口内该组件的最大测量增益，`max ∅ = −∞`。
- `rrsi/history.py:152-163` — `prune_set()`：`B_t`，且**带上现任里属于该组件的已接受机械清单**，让提案器知道要删什么。
- `rrsi/history.py:189-193` — `stall_flag(trajectory, t, w, δ)`：`σ_t = 1[S_t − S_{t−w} ≤ δ]`；历史不足 w 轮时返回 0。
- `rrsi/history.py:196-210` — `exploration()`：生成 `E_t` 文本。停滞时写 "RESERVED EXPLORATION SLOT"，未停滞时仍列出未练习组件（「不强制，但关于它们的证据仍然缺失」）。
- `rrsi/history.py:166-185` — `render()`：给提案器上下文的压缩视图。**未测量的门禁失败只保留最近 4 条**，注释写明「一墙 abort 是反馈环路，不是证据」。→ 这是「不该进 prompt 的东西」的精妙处理，直接对应 INV-3 的反面。
- `rrsi/components.py:44-46` — `K = {prompt, control_flow, config, output_plumbing, context_mgmt, client_tool, skill, memory, subagent}`，`K_STR = {client_tool, skill, memory, subagent}`。
- `rrsi/components.py:61-69` — `text_only(diff)`：所有改动行都是字符串字面量或注释 → 归类为 `prompt`（「无论散文写了什么」）。→ 便宜的 diff→组件分类器。
- `rrsi/components.py:95-100` — `normalize(declared, diff, signals)`：提案器声明的组件标签必须由 diff **提供证据**，否则按 diff 重新打标。注释点明动机：「提案器可以声明一个 skill/memory/tool/subagent 编辑却没真的交付，或把一个控制流改动伪装成未练习组件来满足预留探索槽，未验证的标签会污染 `T_t`、`U_t` 和新颖度项」。
- `rrsi/components.py:103-108` — `novelty()`：`ν_t` = 候选触及的、现任从未有过被接受编辑的**结构性**组件类型数。
- `rrsi/propose.py:60-137` — 提案器 system prompt。三条关键纪律：(1) 「策略 LLM 是冻结的，且是和你**不同**的模型：不要假设它和你共享能力、习惯或判断」；(2) 严格 JSON 单动作协议；(3) **「没有 abort 动作。你必须交出一个候选。一轮什么都不交就什么也没检验。」**（`rrsi/propose.py:107-111`）。
- `rrsi/propose.py:140-194` — `Workspace`：路径囚禁的文件操作；`edit()` 要求 `old` 串在文件里**恰好出现一次**，否则报错要求更长的唯一上下文。
- `rrsi/propose.py:328-369` — `done()` 的契约校验（预算在此强制执行）：声明了编辑却零文件改动会被退回（`rrsi/propose.py:331-335`）；超过 `b_t` 退回（`rrsi/propose.py:338-339`）；缺 `id/component/hypothesis/targets_mode/predicted_affected/retroactive_check` 退回；组件不在 `K` 退回；持有预留探索槽却没碰未练习组件退回（`rrsi/propose.py:349-354`）。
- `rrsi/propose.py:370-379` — abort 动作被退回 3 次后才以 `status="abort"` 结束（随后在 `rrsi/loop.py:495-496` 被记为 `no_proposal` 门禁失败）。
- `rrsi/propose.py:264-304` — 上下文装配：历史 L_t + 归因记分板 + 探索指令 E_t + 剪枝目标 B_t + 三透镜报告 + 逐任务 digest + **整个 harness 源码 dump** + 本轮预算 b_t。修复轮走单独分支（只改 reviewer 反对的部分）。

**分析侧（Analyze → 三透镜报告）**

- `rrsi/loop.py:134-150` — `build_traces()`：取分数最低任务的**最差试次** + 分数最高任务的**最佳试次**（「成功习惯是提案器绝不能打破的东西」）。`n_fail_traces=22` / `n_success_traces=6`（eng 实例），不需要全部轨迹。
- `rrsi/analyst.py:52-102` — analyst system prompt：**不自己读轨迹**，调度只读 digester；三透镜 = `failure_modes`（跨任务聚类的失分阻断点）/ `capability_gaps`（想做但做不到）/ `success_habits`（让通过任务干净完成的可复用行为，跨轮保留作为回归守卫）。聚合规则含「**用稳定的 mode 命名**（相同机制沿用旧标签）」「描述必须 entity-free」「不要开药方、不要归咎模型 vs 脚手架」。
- `rrsi/digester.py:51-54, 112-119` — 只读 digester：bash 白名单 + DENY 正则（禁重定向、反引号、`$(`、`sed -i`、`rm/mv/cp`、`python`…），`_safe()` 把路径囚禁在轨迹目录。
- `rrsi/loop.py:159-178` — `attribute()`：把候选声明的 `predicted_affected` 与实测对比，算 `hit_rate`，并标出**未被预测的回归**（`unpredicted_regressions`），追加进 `attribution.jsonl`，下轮作为「归因记分板」喂回提案器（`rrsi/propose.py:272-274`）。**这是把提案器的自评变成可证伪预测的关键机制。**

**筛选与评估侧**

- `rrsi/critic.py:50-51` — 通用 denylist（凭证）。
- `rrsi/critic.py:105-110` — `precheck()`：确定性正则黑名单命中即拒绝，**不耗费 LLM**。
- `rrsi/critic.py:113-147` — `review()`：LLM 复审 6 类拒绝理由（泄漏/任务特化、退化、**评分器投机**、未声明捆绑、**运行时记忆/skill 泄漏**、无界工作）。判据是「litmus test：这个改动换一个同类工作的陌生任务，是否仍然有意义、仍然有帮助？」（`rrsi/critic.py:69-71`）。3 次解析失败 → **fail-closed 判 reject**（`rrsi/critic.py:145-146`）。
- `rrsi/evaluate.py:104-119` — `aggregate()`：缺失试次 `r = 0` 且**保留完整分母**；支持权重（Harvey LAB 的 reward = 通过 criteria 数 / 总数，weight = 总数，于是 `S_hat` 就是 benchmark 自己的指标）。
- `rrsi/evaluate.py:131-134` — `relative_cost_change()`：任一侧无 token 计数则 ΔC = 0（代价规则自动失效而非崩掉）。
- `rrsi/calibrate.py:54-68` — `bootstrap_se()`：在**每个任务内**重采样试次估计 `se(S_hat)`（尊重权重）。
- `rrsi/calibrate.py:85-108` — `calibrate()`：≥2 次基础评估时直接观察零假设差异（`sd_null = stdev(scores)·√2`），否则用 bootstrap（`sd_boot = √2·se·√(k_pooled/k)`）；`δ = z · sd_null`，默认 `z = 2.0`（「不变的 harness 有约 97.5% 概率通过 `S★ − δ` 下限」）。

**选择侧（Algorithm 2，纯函数）**

- `rrsi/selection.py:81-94` — `cost_rule()`：两个分支。`ΔS > δ` 用 `ΔC ≤ β0 + β1·ΔS`；否则用 `w_s·ΔS − w_c·ΔC + w_n·ν > 0`。（注意：增益超带且又省 token 的候选恒通过，因为 `ΔC ≤ 0 ≤ β0 + β1·ΔS`。）
- `rrsi/selection.py:97-121` — `judge()`：顺序为**噪声下限 → 代价规则 → 领域守卫**，全部不可补偿；任一失败即不可接受并写明首个失败原因。
- `rrsi/selection.py:124-136` — `select_round()`：在可接受候选里按 `S'` 取 argmax；无可接受者则返回 `None`（保留现任）。
- `rrsi/domains/eng/adapter.py:221-229` — `guards()`：`valid_rate` 下降 > 0.03 或 `no_payload_rate` 上升 > 0.02 即判违例（论文 Appendix C.3 的 0.03/0.02）。
- `rrsi/domains/workspace/adapter.py` — workspace 实例**不设守卫**（用默认空实现），只有 eng 设。→ 守卫是 per-domain 的，不是普适的。

**版本与隔离侧**

- `rrsi/gitops.py:80-94` — `worktree_add()` / `worktree_remove()`：每个候选在**自己的 worktree + 自己的分支**上（`<domain>/r<t><variant>`），变体之间看不到彼此的改动，可并发评估；worktree 移除时连分支一起删。
- `rrsi/gitops.py:107-112` — `diff_with_new_files()`：用 `git add -N` 让**新建文件**也进入 diff，从而被 critic 完整复审。→ 没有这一步，提案器可以新建一整个模块躲过评估前复审。
- `rrsi/gitops.py:115-125` — `commit_path()` / `fast_forward()`：接受 = 把 `evolve/<domain>` 快进到胜者的 commit；`merge-base --is-ancestor` 保证只能快进。
- `rrsi/gitops.py:67-68` — `tree_hash()`：frontier 记录的是 harness 目录的 **tree hash**，所以 harness 之外的 commit 永远不会看起来像一次改动。
- `rrsi/loop.py:235-237` — 每轮开始先校验「分支上的 harness tree == frontier 记录的 harness tree」，不一致直接 `SystemExit`。→ 廉价的完整性自检，抓住「有人手改了 `evolve/<domain>`」。
- `rrsi/loop.py:348-422` — `readjudicate(t)`：**在已存储的测量上重跑 Algorithm 2**（改了 δ 或权重后免费重判，不花任何新评估）。关键约束：t 之后的轮次必须先被移除，因为它们的候选是从旧的 `H_{t+1}` 起草的。且重判时用 `update-ref` 而非 fast-forward，并校验 `merge-base --is-ancestor`（`rrsi/loop.py:399-401`）。
- `rrsi/loop.py:424-450` — `reevaluate(t)`：基础设施失败后重新测量已 commit 的候选，再自动重判。
- `rrsi/driver.py:44-98` — `drive()`：`touch runs/<domain>/STOP` 在当前轮后停止；`settled_rounds()` 做轮次幂等；**`MAX_CONSECUTIVE_INFRA = 3`** 连续基础设施失败即退出，「防止坏掉的环境烧光整个预算」。

### 3.3 数据流：一轮的 7 步

`rrsi/loop.py:231-346`（`Run.round(t)`）：

```
1. F_t ← Analyze(H_t, D_evolve)                      loop.py:249-268
     从现任自己的评估里 build_traces（最差失败 + 最佳成功）→ analyst 调度 digester
     → 三透镜报告；跨轮保留 failure_modes/success_habits 到 global_analysis.json
2. b_t ← 退火编辑预算                                  loop.py:274
3. σ_t, T_t, U_t, E_t, B_t ← 从 history + frontier 算出  loop.py:275-288（落 directives.json）
4. 对 m 个变体各起一个 worktree：                       loop.py:290-297, 453-576
     propose（条件化于 F_t, L_t, b_t, E_t, B_t，受困于路径囚禁的 Workspace）
     → critic 复审 + 有界修复（repair_rounds=5）       loop.py:498-548
     → 标签按 diff 归一化                              loop.py:554-558
     → liveness smoke（编译 / 构造 / 少量任务）          loop.py:562-568
5. 对通过门禁的候选并行 Evaluate(H', D_evolve, k)        loop.py:299-302, 578-605
     （缺失试次 > invalid_missing_frac 触发一次重试，仍不行则 eval_invalid）
6. Algorithm 2：可接受性 → argmax → S★ → 历史 + 归因      loop.py:304-325
     结果分 ACCEPTED / LOST（可接受但分数更低）/ REJECTED / 门禁失败
7. 快进 evolve/<domain> 到胜者，更新 frontier，清理 worktree   loop.py:327-346
```

### 3.4 值得注意的工程细节

- **coding 的泄漏黑名单是动态生成的**（`domains/coding/adapter.py:136-141`）：把 evolve 任务的**每一个任务名**编译成 denylist 正则，diff 里出现任一任务名即被 precheck 拒绝。这是「演化集 == 打分集」逼出来的强约束。GameRSI 的等价物是「diff 里出现任一 seed 字面量即拒」。
- **infra 失败 vs agent 失败必须分开**（`domains/coding/adapter.py:83-95`）：docker/镜像/网络错误算 infra（→ missing，不是 harness 的锅），`AgentTimeoutError` 算 agent 失败。GameRSI 的 §8.5 已有类似区分（degraded 局剔除、dropped 局计 lose）。
- **runner 的续跑语义不统一**（`domains/coding/adapter.py:166-172`）：harbor 无法续跑一个部分完成的 job，所以不完整的 job 被改名为 `.stale.<ts>` 后**从头重跑**——否则一个被杀掉的目录会被按「大部分缺失」计分。→ 「部分完成的评估目录被静默计分」是一个真实的陷阱。
- **ephemeral cache**（`rrsi/llm.py:92-120`）：把 constitution + harness 源码作为缓存前缀块，重复轮次只付一次读取成本。对本地 8B 的等价物是 **KV-cache 前缀复用**，在 Mac 上是成本杠杆。
- **有界修复**（`rrsi/loop.py:500-548`）：critic 拒绝后把反对意见回灌提案器，只让改反对意见涉及的部分（「minimal additional edits」），最多 `repair_rounds=5` 轮。
- **标签争论不值得花一个候选**（`rrsi/loop.py:507-522`）：声明标签与 diff 不符时**直接按 diff 重打标**，只有当重打标破坏了预留探索槽才退回——注释说「和提案器争论标签要花掉一个候选，却什么都买不到」。→ 务实的优先级取舍。

---

## 4. 与 GameRSI 的对标矩阵

### 4.1 顶层差异（决定了移植策略）

| 轴 | RRSI | GameRSI | 影响 |
|---|---|---|---|
| **选择用的数据** | evolve set 本身（与演化集同一批） | heldout H1（INV-5 三方隔离） | GameRSI 在结构上比 RRSI 更安全；但 **H1 跨代复用**，同样的自适应漂移压力只是更慢，仍需要 RRSI 的下限/代价规则 |
| **配对设计** | 无（现任用旧的存储评估，候选重新评估，非配对；`calibrate.py:33-36` 显式建模 `sd = √2·se` 的非配对零假设） | 有（parent 与候选用**同一 seed 集**，McNemar 配对检验） | GameRSI 的方差更小，是**优势，应保留**；δ 应从**配对**零假设推导，比 RRSI 更紧 |
| **编辑空间** | 完全开放（新模块 / subagent / skill / memory 都可加） | 严格限制（≤2 文件 / 40 行 / 4000 字符 / 4 个白名单位置 / 禁新依赖 / `rank_actions` 签名冻结） | 哲学相反：RRSI「正则化轨迹而非限制空间」。GameRSI 已有限制，**正则化轨迹是净增量** |
| **候选代码执行** | 直接跑（唯一闸门是 critic） | MOD-02 `guard.gate()` + 子进程 worker（INV-4） | GameRSI 在「奖励投机/环境作弊」轴上**严格更强**；RRSI 在「语义泄漏」轴上更强（LLM 复审）→ 互补 |
| **人在环** | 无 | HITL 门 | GameRSI 更强，且在 T=3 的短 run 上是实用必需 |
| **算力** | 一个 run 数千次完整 agent episode（Opus 4.8） | M3 全程约 60 小时、约 500 局（8B 本地） | **约 1000× 差距**：RRSI「每轮在完整 evolve set 上评估所有候选」不可复制；所有超参必须为短 run 重标定 |
| **模型同源性** | 策略与提案器是**不同模型**（`propose.py:61-64` 明说） | 策略 / 提案器 / critic / 反思写作**同一个 8B 权重** | RRSI 不成立的自确认风险在 GameRSI 里是真实的；角色隔离（不同 system prompt、不共享上下文、无测量的主张不进证据）是必须的 |
| **坍缩的定义** | 搜索塌缩到窄编辑族（proposer 一直改 prompt） | 策略动作熵坍缩（C4） | **是两层不同的坍缩**，GameRSI 目前只监控了策略层，缺搜索层 |

### 4.2 逐机制矩阵

#### A. 可直接移植（改动小、风险低、收益明确）

| RRSI 机制 | 代码位置 | 映射到 | 说明 |
|---|---|---|---|
| **退火编辑预算 b_t** | `rrsi/schedule.py:43-50`，`propose.py:336-339` 强制 | MOD-07 FR-07-4 + 判定表 | **M2/M3 直接置 `b_t = 1`**（一个候选一个机制）：T=3 时退火公式几乎不动（`b_max=3, b_min=1, T=3` → `[3, 3, 2]`），而 6 个候选根本赔不起捆绑编辑。把「编辑按独立性计数」写进 FR-07-4（现在只数行数/文件数，8B 会把多机制塞进 40 行） |
| **编辑历史 L_t（每编辑一条记录，捆绑共享测量）** | `rrsi/history.py:77-99` | MOD-07 代际产物 + `render_proposer_prompt` | GameRSI 已有 `candidates/{id}.json` + `report.json`，**缺的是「喂回提案器」这一步**（RRSI 在 `propose.py:266-271` 把历史塞进上下文）。这是**单项收益最高的移植** |
| **负证据纪律 + 未测量记录抑制** | `rrsi/history.py:166-185`（只留最近 4 条未测量） | MOD-07 / MOD-06 反思 | 直接对抗自我确认环路：「一墙 abort 是反馈环路，不是证据」。GameRSI 的拒绝记录也应分级，只把**有测量的**结论当证据 |
| **归因记分板（预测命中率 / 未预测回归）** | `rrsi/loop.py:159-178`，`propose.py:272-274` | MOD-07 `expected_effect` | GameRSI 的 `expected_effect{metric,direction,priority}` 太弱；加一个 `predicted_affected`（预测会动的 seed 或失败模式 id），下轮把命中率喂回。**把提案器的自评变成可证伪预测**，成本几乎为零 |
| **确定性黑名单（含动态生成）** | `rrsi/critic.py:105-110`；`domains/coding/adapter.py:136-141` | MOD-07 FR-07-3 / MOD-02 guard | GameRSI 已扫 heldout/test seed 字面量；**应扩展到全部 seed（含 train）**——`if seed == 1042:` 式的分支永远不是通用机制（RRSI 扫的是全部 evolve 任务名，因为它 evolve==打分）。MAL-5 已覆盖 heldout 分支，补 train 分支即可 |
| **diff 里新建文件必须被复审** | `rrsi/gitops.py:107-112`（`git add -N`） | MOD-02 guard / MOD-07 | 若 GameRSI 的 tool_patch 允许新建 `tools/*.py`，静态扫描必须覆盖新增文件全文，而不是只看 patch 上下文 |
| **frontier 完整性自检（harness tree hash）** | `rrsi/loop.py:235-237`，`gitops.py:67-68` | MOD-09 / MOD-07 | 每代开始校验「磁盘 harness 的内容 hash == version.json 记录」，不一致则拒绝开代。抓住手改 harness 的操作 |
| **连续基础设施失败熔断（3 次）** | `rrsi/driver.py:44` | MOD-06 | 直接加到 `LoopOrchestrator`：连续 3 代因 `E_EVAL_*` 基础设施失败则停止 run，「防止坏掉的环境烧光预算」 |
| **未解析/异常 → fail-closed** | `rrsi/critic.py:145-146`（3 次后 reject） | MOD-03 `generate_json` / MOD-07 | 任何裁判链路解析失败一律保守拒绝。GameRSI 的 NFR-07-7 已有此总则，应对 critic 输出也显式适用 |
| **「同一个窗口重测 H0」漂移控制** | 论文 §4.2（所有数字对同窗口 H0 测）+ `calibrate.py` | MOD-08 基线归档 | **每 G 代（或每里程碑）把 v0/fixed_prompt 基线在 H1 上重跑一次**并写进报告。H1 跨代复用必然漂移，这是唯一能分辨「真增益」与「漂移」的手段，且代价只有 40 局 |
| **领域守卫（不可补偿的次轴）** | `domains/eng/adapter.py:221-229` | MOD-07 判定表 C5 | GameRSI 的 C5（illegal/fallback/truncation 率上限）已经是同类；可补一个「合法动作率」之外的**决策密度**守卫（见 §6 M3） |
| **三透镜分析 + 稳定命名 + success_habits 回归守卫** | `rrsi/analyst.py:52-102` | MOD-06 `reflect.py` / MOD-07 证据束 | 让反思产出**跨代稳定的 snake_case 失败模式词表** + 一个「绝不能打破的成功习惯」清单。RRSI 明确要求 analyst「不要开药方、不要归咎模型 vs 脚手架」——职责干净 |

#### B. 需要改造（方向清楚，但参数/形态要为 GameRSI 重做）

| RRSI 机制 | 为什么不能照搬 | 改造方案 |
|---|---|---|
| **噪声带 δ 标定**（`calibrate.py:85-108`：≥2 次基础评估，或任务内试次 bootstrap） | GameRSI 每 seed 只 1 局（FR-08-09），**没有试次可 bootstrap**；重复一次基础评估 = 40 局 ≈ 3.3h ≈ M3 总预算的 6% | 用 **seed 级配对 bootstrap**（MOD-08 已有 `paired_bootstrap_gap` B=10000）推零假设分布，取 `δ = z·sd(ΔS_null)`，`z=2.0`；把 C1 的 `win_rate_tol` 从硬编码 `0.0` 换成 δ。**tol=0.0 + n=40 意味着「真正等价的候选有约一半概率因少赢 1 局被拒」**——M3 只要 1 次 accept，这个过拒风险可以接受，但应知道代价 |
| **代价规则 `ΔC ≤ β0 + β1·ΔS`**（`selection.py:81-94`） | RRSI 的 C 是 policy tokens/试；GameRSI 的 INV-7 规定主指标只能用环境原生结果，且成本模型是 wall-clock/局数 | ΔC 用**每局平均决策数**（`mean_decisions`，MOD-08 已有）或**每决策平均 prompt token**（FR-07-7 已允许差异并记录）。先 β0 取小（如 0.05）、β1 按「win_rate 每涨 1pp 允许决策数涨 X%」标定。**必须报告 ΔC**——否则就是 AHE 的结局（+58% token 换 −4.4 点 OOD） |
| **新颖度 ν_t（结构性组件）**（`components.py:103-108`） | RRSI 的 `K_str = {client_tool, skill, memory, subagent}` 在 GameRSI 里不存在 | GameRSI 的 `K = {prompt, tool, config}`（对齐三种 candidate kind），`K_str = {tool}`（新 harness 工具函数 = 新机械）。`w_n` 相应变小。只有 3 个组件时新颖度很快饱和，主要价值在**早期把搜索推向 tool_patch** |
| **结构化探索 / 停滞重定向**（`history.py:189-210`，`loop.py:294`） | RRSI 的 `w=3` 在 T=3 的 M3 里等于「整个 run 一开始就停滞」；`n_prune=4-5 > T` 让剪枝窗口包住全 run | 短 run：把 `w` 设为 2，并把「未练习组件」从**组件类型**换成**失败模式 id**（GameRSI 的搜索空间小，组件 3 个一下就用完；失败模式有十几个，是更有意义的探索轴）。预留槽逻辑保留：停滞时强制一个候选针对从未被针对过的失败模式 |
| **结构剪枝 B_t**（`history.py:152-163`） | GameRSI 的 harness 太小（几个文件），累积风险低；但 **MOD-05 记忆库本身是被演化对象且无界增长** | 剪枝目标改为「记忆条目 + harness 工具函数 + prompt 小节」三类；每代列出「窗口内测量贡献 ≤ 0」的条目作为删除目标交给提案器。删除本身算一次合法编辑（RRSI 也这么干，`propose.py:122-125`） |
| **critic 的 LLM 复审**（`critic.py:113-147`） | 8B 做「读 diff 判 6 类拒绝」比 Opus 弱得多，误报/漏报都会发生 | 降级为**建议层 + fail-closed**：确定性黑名单（seed 字面量、env 内窥符号、网络/文件写/subprocess、`rank_actions` 签名改动）作为**唯一硬闸门**（MOD-02 `guard.gate` 已具备）；LLM 复审用 8B 跑一遍，输出 `accept/reject/uncertain`，`uncertain` 或解析失败 → 交给 HITL（GameRSI 有人，RRSI 没有，这是降级的关键支撑）。判据换成 GameRSI 自己的 litmus test：「这个机制换一个从未见过的 seed，还会帮忙吗？」 |
| **analyst + digester 两层管线**（`analyst.py`、`digester.py`） | 每轮 6 路并发 digester × 若干任务 = 大量 LLM 调用，Mac 上是成本杀手 | 砍掉 digester 层：直接从 MOD-06 已有的结构化**逐局摘要**（EpisodeResult + kind_counts + 失败原因）聚合三透镜报告；只在「某局摘要说不清」时按需对**最差的那一局**做一次单轨迹深读（RRSI 自己也只取 22 失败 / 6 成功轨迹，`loop.py:138-139`） |
| **提案器形态**（`propose.py` 的 40 轮 agentic 编辑循环，`MAX_TURNS=40`） | 8B 做不动多轮严格 JSON 动作协议；而且 GameRSI 的编辑面本来就小 | 用**单次结构化生成**（一个 JSON 里给出 unified diff + rationale + predicted_affected），由 MOD-03 `generate_json` 的 JSON Schema 约束解码兜底。这是**正确的降级方向**：RRSI 那套协议是为「开放编辑空间 + 强模型」设计的，GameRSI 的小编辑面不需要 |
| **git worktree 候选管理**（`gitops.py:80-94`） | GameRSI 已裁决（Spec B-29）用 `harness/versions/v{N}/` 全量快照 + tag，更易审计 | 保留快照方案（GameRSI 的选择更符合「版本不可重写」的审计要求）。可借鉴的两点：(1) 快照里记录 harness 目录的 **tree/content hash**；(2) 候选 staging 目录之间**必须物理隔离**（RRSI 用 worktree 保证变体互不可见） |
| **readjudicate（不重评估只重判）**（`loop.py:348-422`） | 直接可用，但 GameRSI 的代际依赖图不同（GameRSI 的 parent 每代重测） | 加一个 `evolve readjudicate --generation N` CLI：用已存的 `candidates/{id}.json` + `report.json` 重跑判定表。前提是**每代记录 (candidate, parent_version, 测量指标) 三元组**。这是做「判定表阈值敏感性分析」的免费工具，M3 验收时极有用 |

#### C. 不适用（明确不要做）

| RRSI 机制 | 为什么不适用 |
|---|---|
| **在 evolve set 上做选择**（论文的核心前提） | GameRSI 有 INV-5 三方隔离 + heldout 选择，结构上更安全。**不要为了模仿 RRSI 而把选择改成 train 分数** |
| **K 里的 client_tool / skill / memory / subagent 组件** | GameRSI 的 harness 不含这些层（skill/memory 在 MOD-05，是 agent 侧资产而非 harness 编辑面）。强行加会模糊 MOD-05 与 MOD-07 的边界 |
| **MCP gateway / harbor / Docker / Terminus-2 / archipelago** | 具体 harness 与 runner 无关 |
| **k=2–4 试次/任务** | GameRSI 每 seed 1 局 + 配对设计；用增加试次换方差不如用配对 + bootstrap |
| **T=20–40 轮** | M3 只有 3 代；所有依赖轮次深度的机制（退火、停滞、剪枝窗口）必须重标定或冻结 |
| **critic/proposer/analyst 用 Opus 级模型** | 本地 8B；见 B 中的降级方案 |

### 4.3 与 INV-1 ~ INV-7 的关系

| 不变量 | RRSI 的对应物（或缺失） | 结论 |
|---|---|---|
| **INV-1**（agent 只能经 SandboxEnv 触达环境） | **无沙箱**：候选 harness 直接在 worktree 里跑，唯一闸门是 critic 的规则 3（评分器投机）与规则 5（运行时记忆/skill 泄漏，`critic.py:84-90`） | GameRSI **严格更强**。可移植的只有 critic 规则 5 的**审查清单**：若 GameRSI 将来允许 harness 机制持久化状态，把「这条代码路径能否跨局注入或持久化任务特有运行时数据」作为 MOD-02 审计器的一条检查 |
| **INV-2**（观测是白名单投影） | 无观测投影。弱类比：`render_trace` 只给 analyst/digester **渲染后的轨迹投影**，而非原始状态 | 理念一致（给分析侧投影而非原始内部状态），GameRSI 已在 MOD-02 实现，无需改动 |
| **INV-3**（prompt 只能含四类白名单上下文） | 提案器上下文是**事后由 critic 筛**的大杂烩（`propose.py:264-304`），没有白名单 | GameRSI 的 `render_proposer_prompt` 模板化强制（FR-07-3）**更强**，应保留。RRSI 值得抄的是**「不放进 prompt 的东西」的纪律**：未测量的门禁失败不进证据（`history.py:166-185`），因为「一墙 abort 是反馈环路」 |
| **INV-4**（harness 代码受限执行） | **完全没有**：候选代码有完整网络/文件/subprocess 访问权 | GameRSI **严格更强**（`guard.gate` AST 扫描 + 子进程 worker）。RRSI 唯一可补的是 **liveness smoke 层**（编译 → 构造 → 少量任务，`domains/eng/adapter.py:266-294`），作为 `guard.gate` 之上的廉价兜底 |
| **INV-5**（train/heldout/test seed 三方隔离） | **RRSI 的核心问题正是这个的缺失**：evolve set == 打分集。它的替代品是 (a) critic 泄漏筛查（含动态任务名黑名单）、(b) held-out/OOD benchmark **从不进入选择**、(c) 确定性切分生成器 pin 住上游 commit（`split_workspace.py`） | GameRSI 结构更强（heldout 选择 + seed 字面量扫描）。**RRSI 补的两件事值得做**：(1) 黑名单覆盖**全部** seed 而非仅 heldout/test；(2) 一个问「换陌生 seed 是否仍然有用」的 diff 复审层（litmus test，`critic.py:69-71`），因为 seed 字面量扫描抓不到「只在 train 出现的敌人/地图模式」这类语义泄漏。**注意**：GameRSI 的 H1 跨代复用，所以 INV-5 防住的是「heldout 值泄漏进 prompt」，**没有**防住「H1 分数跨代自适应漂移」——那需要 §6 的漂移控制 |
| **INV-6**（随机性只来自显式 seed） | 部分满足（`calibrate.py:55` 用 `random.Random(seed)`），但提案器 LLM 采样并未确定性化 | 无冲突。GameRSI 更严（MOD-09 AST 守卫），保持即可。RRSI 的 `bootstrap_se` 用固定 seed 的做法可直接沿用 |
| **INV-7**（主指标只用环境原生结果） | RRSI 的 S 恒为 benchmark 自己的指标（单元测试通过率 / criteria 通过率 / 冻结模拟器判定），代价 C 只出现在代价规则里、**从不作为目标** | **两者完全一致**。GameRSI 应坚持：`mean_decisions` / prompt token 只能进代价规则和报告，**绝不能进主指标**（RRSI 的 `select_round` 永远按 `S'` 取 argmax，`selection.py:136`）。同时注意 RRSI 的 `aggregate`（`evaluate.py:104-119`）与 GameRSI 的「dropped 计 lose 且不剔除分母」（FR-08-09）是同一条原则，APEX 的「缺失 rollout 计失败」也是——**已对齐，无需改动** |

### 4.4 降级方案总览（消费级 Mac + 单 8B 模型 + 单游戏）

| RRSI 组件 | 降级路径 | 代价 |
|---|---|---|
| Opus 提案器（40 轮 agentic 编辑） | 单次结构化 diff 生成（JSON Schema 约束解码） | 编辑面本来就小，几乎无损失 |
| Opus critic | 确定性黑名单为硬闸门 + 8B 复审为建议层 + `uncertain` → HITL | 漏报由 heldout 选择（INV-5）与 HITL 兜底 |
| Opus analyst + N 个 digester | 从结构化逐局摘要直接聚三透镜；仅深读最差一局 | 失去细粒度证据引用，用 trace 的 `truncation_reason` 等枚举字段补 |
| 每轮全 evolve set 评估（244–9,600 局） | 每代 40 局 heldout + 5 局冒烟（已规划，165 局/代） | 统计功效大幅下降 → 必须配对 + bootstrap + δ 标定 |
| k=2–4 试次 | 1 局/seed + 配对设计 | 配对比独立试次方差更小，部分补偿 |
| T=20–40 轮 | T=3（M3）→ 冻结 `b_t=1`、`w=2`、剪枝窗口 = 全 run | 退火/停滞/剪枝的动态价值在短 run 里用不上，先保留**数据记录**，等 T 变大再开 |
| 重复基础评估标定 δ | seed 级配对 bootstrap 推零假设 | δ 变成配对口径，更紧也更贴合 C1/C2 |
| 8 个 benchmark / 3 domain 的 OOD 面 | 从游戏配置构造 OOD 面（见 §6 M0/M3） | 只能测「配置泛化」而非「任务类型泛化」，但对单游戏项目是唯一可行解 |

---

## 5. 风险与陷阱

### 5.1 论文报告的失败模式（都是 GameRSI 的一等公民）

1. **白演化**：完全去正则演化把 evolve 拉到 92.8（全场最高），OOD 只有 40.3（H0 = 39.7），代价 3.80M tokens（H0 = 1.56M）。**演化本身不产生迁移性增益，除非正则化**。
2. **四个基线里三个 OOD 不如不演化**：TTHE 低 1.7 点，AHE 低 2.6 点（OOD 平均），HarnessX 平。Meta-Harness evolve 最强（93.0）但 OOD 只 +0.9。→ **evolve set 分数是 OOD 分数的反向指标**。
3. **代价必然膨胀**：没有任何已演化 harness 比 H0 便宜。AHE 多花 58% token 换 −4.4 点 OOD。→ 没有代价规则的演化会把算力换成空气；GameRSI 的 Mac 算力是硬约束，**代价规则不是可选项**。
4. **噪声投机**：「重复在噪声评估中做选择会把随机赢家变成永久搜索状态」。GameRSI 的 n=40 heldout、win_rate 约 45% 时二项 se ≈ 0.079——**每个接受决策都落在一个非常宽的噪声带里**。C1 的 `tol=0.0` 是「一个真正等价的候选有约一半概率因少赢 1 局被拒」。
5. **复杂度累积**：接受侧去正则后 token 涨一半，「不受约束的选择规则把大部分被接受的编辑花在了噪声和上下文上」。
6. **评分器投机在工程实例里「不是假设」**（`domains/eng/SKILL.md:105-113`）：一个判定面是「同样任务 + 关掉九个评分漏洞」的 hardened 版本，所以「学会投机评分器的脚手架在这里分数高、在那里掉下去」。→ GameRSI 的等价物是 **heldout/test seed 必须包含 train 没见过的内容形态**，否则学到的「seed 特有捷径」在 H1 上照样有效，C3 的 gap 也测不出来。

### 5.2 RRSI 假设但 GameRSI 不满足的前提

1. **提案器是强模型且与策略不同源**。RRSI 明确写「策略 LLM 是冻结的，且是和你**不同**的模型」（`propose.py:61-64`）。GameRSI 用同一个 8B 权重充当策略 + 提案器 + critic + 反思作者。**自我确认环路在 RRSI 里不存在、在 GameRSI 里是结构性风险**。缓解：角色间用不同 system prompt、不共享上下文、**任何主张必须带测量**（`history.py:166-185` 的做法），并且反思不得引用提案器自己的假设作为证据。
2. **足够的测量精度**。RRSI 的 δ 在 workspace 实例上是 14,100 条判决中的 60 条（0.004）；GameRSI 的 H1 是 40 局，δ 量级约 0.08（二项 se ≈ 0.079）。**约 20 倍的噪声**。C2 要求 McNemar 显著在 n=40 上功效很低——多数代会判 `insufficient`。这不是 bug，是约束：**M3 的「至少 1 次 accept」应该靠把候选做得真的更好，而不是靠放宽 α**。
3. **足够的轮次深度**。`w=3`、`n_prune=4-5`、退火曲线都是为 T=20–40 设计的。T=3 时停滞判定在第 3 代必然触发、剪枝窗口包住全 run、退火几乎不动。**必须显式重标定，否则这些机制会以荒谬的方式触发**。
4. **丰富的组件词表**（9 个组件）。GameRSI 只有 3 个候选 kind，新颖度 ν_t 一代后就饱和，结构化探索很快无路可重定向。**应把探索轴从「组件类型」换成「失败模式 id」**。
5. **开放编辑空间**。RRSI 的正则化是「代替空间限制」的；GameRSI 已经限制了空间（40 行 / 2 文件 / 白名单位置）。这意味着 **RRSI 的提案侧正则化在 GameRSI 里的边际收益会小一些**（因为搜索空间本来就窄），但**归因与负证据纪律的收益不打折**——8B 提案器在窄空间里同样会重复提同样被拒绝的东西。

### 5.3 代码里值得注意的工程坑

1. **部分完成的评估目录会被静默按「大部分缺失」计分**（`domains/coding/adapter.py:166-172`）。harbor 无法续跑，所以不完整的 job 被改名为 `.stale` 从头重跑。→ GameRSI 的 `run_eval` 续跑语义必须明确：一个 seed 目录存在但 trial 未完成时，是补跑、重跑、还是计 dropped，必须由 MOD-08 显式规定并测过。
2. **infra 失败与 agent 失败必须分类**（`domains/coding/adapter.py:83-95`）：把 docker/网络错误当成 agent 失败会冤枉候选，反过来会掩盖真实退化。GameRSI 的 degraded（剔除）/ dropped（计 lose）区分已对齐，但**分类规则要写成可测的正则/枚举**，不能靠异常类型名字。
3. **标签污染会静默腐蚀搜索状态**（`rrsi/components.py:82-92`）：一个被错误标记为 skill 的 prompt 改动会同时污染 `T_t`（tried）、`U_t`（untried）和 `ν_t`（新颖度）。→ GameRSI 若引入组件/失败模式标签，**必须由 diff 或行为证据验证标签**，而不是信提案器的自报。
4. **「和提案器争论标签要花掉一个候选，却什么都买不到」**（`rrsi/loop.py:507-522`）：RRSI 选择直接按 diff 重打标而非退回。→ 短 run 里候选极其昂贵（M3 只有 6 个），**任何会浪费一个候选的强制退回都要三思**；能自动修复的就自动修复。
5. **重判的代际依赖约束**（`rrsi/loop.py:349-353`）：重跑 Algorithm 2 时不重评估，但 t 之后的轮次必须先移除，因为它们的候选是从旧的 `H_{t+1}` 起草的。→ GameRSI 若做 `readjudicate`，必须连带校验「被重判代的 parent_version 链」。
6. **新建文件躲过复审**（`rrsi/gitops.py:107-112`）：没有 `git add -N`，提案器新建的模块不在评估前 diff 里。→ MOD-02 的静态扫描必须扫**新增文件全文**。
7. **frontier 与分支不一致的静默漂移**（`rrsi/loop.py:235-237`）：RRSI 用 harness tree hash 做每轮开头的断言。→ 每代开头校验「磁盘 harness == version.json 记录」，否则一次手改会让后续所有代际的 parent_version 撒谎。
8. **`critic` 解析失败 fail-closed**（`rrsi/critic.py:145-146`）：3 次后判 reject。→ 8B 输出 JSON 的失败率显著高于 Opus，GameRSI 的重试与降级路径（`E_LLM_SCHEMA_INVALID` → HITL 或 reject）必须显式实现并测过，否则「critic 总是拒绝」会静默杀死所有候选。
9. **ephemeral cache 是成本杠杆**（`rrsi/llm.py:92-120`）：RRSI 把 constitution + harness 源码作缓存前缀。→ 本地 8B 的等价物是 **MOD-03 的 KV-cache 前缀复用**；提案器每轮都读同一份 harness 全文 + 历史前缀，不缓存的话 Mac 上不可行。

---

## 6. 分阶段行动清单

### M0（环境：MOD-01 / MOD-02 / MOD-09 骨架）

1. **定义「评估面」接口契约**（抄 `rrsi/domain.py:55-111` 的形状）：`evolve_ids/heldout_ids/smoke_ids`、`run/score`、`load_trial/render_trace/task_row`、`smoke`、`guards`、静态黑名单。让 MOD-07/08 从第一天起就对着稳定接口写，游戏侧事实留在 MOD-01/02。这是 RRSI 三个实例共用 3,300 行核心的根本原因。
2. **注册 OOD 面**（RRSI 最有价值的测量是「增益是否迁移」，而 GameRSI 只有一个游戏）：在 `seeds.yaml` 里登记一个**只报告、不进入选择**的 OOD split，用游戏配置构造（例如 medium × 10 关 / 非 warrior 角色 / toggle 组合，选 train/holdout 从未用过的组合）。M0 决定代价为零，M3 之后再加要改 seed 协议。
3. **把 trace renderer 当投影做**（`rrsi/analyst.py` 的教训）：分析侧（反思/提案器）只看渲染后的轨迹投影，不见 env 内部状态——与 INV-2 同构，且让 MOD-06 的反思输入稳定。
4. **`smoke` 闸门分层**（`domains/eng/adapter.py:266-294`）：编译/导入 → 构造期契约（关键函数签名、模板字段仍在）→ 少量 seed 实跑。作为 MOD-02 `guard.gate` 之上的廉价兜底，**不是选择规则**（RRSI 明确把 smoke 排除在选择规则之外，只作 liveness）。
5. **在 MOD-09 里实现 harness 内容 hash**（`gitops.py:67-68`）：`version.json` 记录 harness 目录的 tree/content hash，供后续的代际完整性自检。

### M1（基线：MOD-03 / MOD-04 / MOD-08 基线部分）

1. **标定噪声带 δ**（`calibrate.py:85-108` 的精神）：用 seed 级配对 bootstrap（MOD-08 已有 `paired_bootstrap_gap`）在 fixed_prompt 基线上推 `ΔS` 的零假设分布，`δ = z·sd`（`z=2.0`），写入 `metrics/calibration.json`。**把 C1 的 `win_rate_tol` 从硬编码 0.0 换成 δ**，并记录「tol=0.0 会拒掉约一半真正等价的候选」这一取舍。
2. **建立「同窗口 H0 重测」协议**（论文 §4.2）：每个报告里的每个数字都对着**同窗口**重跑的 v0/fixed_prompt 基线。H1 跨代复用必然漂移，这是唯一能分辨真增益与漂移的手段。每 G 代或每里程碑重跑一次（40 局 ≈ 3.3h）。
3. **记录代价基线**（为 M3 的代价规则做准备）：`mean_decisions`、每决策 prompt token、wall-clock——三个都进基线归档。RRSI 的代价规则是建在 `C_hat` 上的，没有基线就没有 ΔC。
4. **三基线 + 三透镜词表种子**：归档 random/heuristic/fixed_prompt（已规划），同时让反思产出**跨代稳定的 snake_case 失败模式 id**（`analyst.py:96-98` 要求 stable naming）。现在就定词表，M2 的历史/新颖度才有挂靠点。
5. **MLX 前缀缓存**（`llm.py:92-120` 的等价物）：MOD-03 实现 KV-cache 前缀复用。M2 的提案器每轮要读同一份 harness 全文 + 历史，这是 Mac 上能不能跑得起闭环的前提。

### M2（闭环：MOD-05 / MOD-06 / MOD-07 初版）

1. **【最高优先】把搜索历史喂回提案器**：加一个 per-edit 的 `search_history.jsonl`（RRSI `history.py:77-99` 的形状：组件 kind、假设、ΔS、ΔC、verdict），在 `render_proposer_prompt` 里注入最近 N 条（`propose.py:266-271`）。**GameRSI 现在把这些写进了 `candidates/*.json`，但没喂回去**——提案器每次都是失忆的，会重复提被拒绝的机制。
2. **未测量记录不进证据**（`history.py:166-185`）：拒绝/失败记录分级，只把有测量的结论当证据；门禁失败最多保留最近几条。直接对抗自我确认环路。
3. **可证伪预测 + 记分板**（`loop.py:159-178`）：`expected_effect` 加 `predicted_affected`（预测会动的 seed 或失败模式 id），下轮把 `hit_rate` 与 `unpredicted_regressions` 喂回提案器。零额外局数，直接约束 8B 的自评倾向。
4. **冻结 `b_t = 1` 并写进 FR-07-4**：M2/M3 时期「一个候选一个独立机制」，并把「按独立性计数编辑」写进校验（现在只数行数/文件数）。T 变大后再开退火公式（`schedule.py:43-50`）。
5. **黑名单覆盖全部 seed**（`domains/coding/adapter.py:136-141` 的做法）：把 `seeds.yaml` 里**所有** seed（含 train）编译成 denylist，diff 里出现任一 seed 字面量即拒。`if seed == 1042:` 永远不是通用机制。
6. **连续基础设施失败熔断**（`driver.py:44`）：MOD-06 加「连续 3 代 `E_EVAL_*` 失败 → 停止 run」。
7. **代际完整性自检**（`loop.py:235-237`）：每代开头校验磁盘 harness 的内容 hash == `version.json` 记录，不一致拒绝开代。
8. **critic 降级形态**：确定性黑名单为硬闸门（MOD-02 已具备，补「全部 seed 字面量」与「新增文件全文扫描」）；8B 复审作建议层，输出 `accept/reject/uncertain`，`uncertain` 或解析失败 → HITL（不自动拒，因为候选太贵）。保留 RRSI 的 6 类拒绝理由作为复审清单（`critic.py:62-96`），判据换成「换一个从未见过的 seed，这个机制还会帮忙吗？」。
9. **HITL 保留**：RRSI 没有人，GameRSI 有。在 T=3 + 8B 的设定下，HITL 是「弱 critic」的必要补偿，不要为了自动化而关掉。

### M3（测量：MOD-08 完整 + MOD-07 回滚/消融）

1. **加代价规则作为新的接受条件**（`selection.py:81-94`）：`ΔC ≤ β0 + β1·ΔS`，ΔC 用 `mean_decisions`（或每决策 prompt token）的相对变化。β0 从严（如 0.05），β1 按「win_rate 每涨 1pp 允许决策数涨 X%」标定，只用 train/H1 标（绝不碰 test）。**每代报告 ΔC**，并把「cost 未超基线 X%」写进 M3 验收，避免重蹈 AHE（+58% token 换 −4.4 点）。
2. **加搜索层停滞检测**（与策略层 C4 区分！）：若 H1 win rate 在 w 代内升幅不超过 δ，把一个候选槽**预留**给从未被针对过的失败模式（不是组件类型——GameRSI 只有 3 个 kind，一代就饱和）。这是 RRSI「structured exploration」的 GameRSI 版本。
3. **结构剪枝**（`history.py:152-163`）：每代列出「窗口内测量贡献 ≤ 0」的记忆条目 / harness 工具函数 / prompt 小节，作为删除目标交给提案器；删除算一次合法编辑。MOD-05 的记忆库无界增长，这是唯一的自动收缩机制。
4. **漂移控制与 OOD 报告**：每代报告 `win_rate_gap`（C3）+ OOD 面分数（只报告）。**把「train 涨、H1 平」列为首要过拟合警报**，而不是把 train 涨当进步。
5. **`evolve readjudicate --generation N`**（`loop.py:348-422`）：用已存的测量重跑判定表，做 C1/C2/C3 阈值的敏感性分析。前提：每代记录 `(candidate, parent_version, 测量指标)` 三元组；重判时连带校验 parent 链。
6. **消融对齐 RRSI Table 2 的形状**：若预算允许，在缩短的 T 上跑 {去正则, 去提案侧, 去接受侧, 完整} 四臂并报告 (evolve, heldout, OOD, 代价) 四列；若预算不够，**至少记录每代「哪个正则器在何时触发」**（RRSI 的 `decisions.json` 就是这个），这样「没有 accept」也能区分是「没有候选」还是「被代价规则挡了」。
7. **M3 验收口径校正**：把「至少 1 次 accept 且 `win_rate_gap < gap_threshold` 且代价未超基线」作为完整验收，而不是「heldout 单调不降」（后者可以被「每代都拒绝」退化满足——GameRSI 的 DoD 已经预见了这一点，RRSI 的数据进一步印证：**保守到从不接受 = 白演化**）。

---

## 7. 一页总结

- **RRSI 与 GameRSI 的关系是正交的**：GameRSI 在「隔离」（INV-5 heldout 选择）、「代码安全」（INV-4 沙箱）、「语义控制」（INV-3 prompt 白名单）三条轴上严格强于 RRSI；RRSI 在「搜索轨迹正则化」这条轴上强于 GameRSI，而 GameRSI 目前**完全没有**这条轴。
- **最该抄的是提案侧，不是接受侧**：消融显示提案侧正则值 +1.6 OOD、接受侧值 +0.7、两者叠加 +3.3（相对 H0 +3.9）；而完全去正则演化只值 +0.6——**演化不做正则化几乎是白做**。GameRSI 的 C0–C5 判定表已经覆盖了接受侧的大部分价值，提案侧（编辑历史喂回、负证据纪律、可证伪预测、单机制预算）是净增量，且**大部分不需要多跑任何一局**。
- **evolve/train 分数是 OOD/heldout 分数的反向指标**：四个先前方法里三个 OOD 不如不演化；RRSI 是 evolve 涨得最少、却是唯一 OOD 显著超基线的。GameRSI 的 M3 验收口径必须据此校正。
- **代价规则在 Mac 上是必需品**：没有任何已演化 harness 比基线便宜；AHE 多花 58% token 换 −4.4 点 OOD。GameRSI 的算力是硬约束，`ΔC ≤ β0 + β1·ΔS` 应直接进入判定表。
- **所有依赖轮次深度的机制必须为 T=3 重标定**：退火几乎不动（`[3,3,2]`）、停滞窗口 = 全 run、剪枝窗口 > T。务实方案是 M2/M3 冻结 `b_t=1`、把探索轴从组件类型换成失败模式 id、先把数据记录下来等 T 变大再开动态部分。
