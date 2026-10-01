# Raven（EverMind-AI）参考项目深度分析

> 服务于 GameRSI 的「参考项目深度分析」。分析对象：Raven（arXiv:2609.33439，自称 "The Harness of Harnesses, built for RSI"）。
> 代码库：`ref/Raven`（clone @ 2026-10-01，git HEAD `e84152c`，单 commit squash，240MB）；论文全文：`ref/paper-text/raven.txt`（5237 行）。
> 分析日期：2026-10-02。所有代码引用为 `path:line`，论文引用为 `raven.txt:line`。

---

## 1. 项目速览

| 维度 | 事实 |
|---|---|
| 定位 | 开源多 agent 生态：把每个「可执行的 model–harness 对」当作可组合的智能单元；Host Agent 分解目标、匹配子任务、协调依赖、整合结果（`raven.txt:201-217`） |
| 自进化主张 | 「Harness of Harnesses」：对 harness 做迭代改进（诊断失败 → 提出候选 → 评估 → 采纳通过验证的改进），基于 HarnessBank 的语义基因库 + 门控筛选（`raven.txt:220-226, 1550-1559`） |
| 仓库规模 | `raven/` 774 个 Python 文件 / 约 **245,000 行**；`evolver/` 约 **13,600 行**（38 个模块）；`benchmarks/appworld/evolve/` 3,086 行；`tests/` **956** 个测试文件（其中 evolver 相关 9 个） |
| 版本状态 | `README.md:31`：「**Raven is pre-alpha. Interfaces and configuration may change quickly.**」`CHANGELOG.md:5` 为 Unreleased，唯一正式版本 `v0.1.0 - Public Preview - 2026-06-30`（`CHANGELOG.md:1247`） |
| 论文里的四个原生专家 | Raven-Research / Raven-Code / Raven-Design / Raven-Oncall；另有 Raven-PPT 在仓库中（`agents/raven-ppt`） |
| 第三方 agent | 通过 ACP / CLI / OpenAI-compatible API 接入，预置 **13** 个（`README.md:200`）。注意 `bridge/` 是 **WhatsApp 消息桥**（TypeScript，`bridge/src/whatsapp.ts`），**不是** agent 适配器——名字有误导性 |
| Skill 生态 | 仓库内仅 35 个 `SKILL.md`（多数在 `experimental/simulation` 场景里）；README 宣传 SkillHub 有 **114,190** 个技能（`README.md:285`，外部语料） |
| 与 GameRSI 的根本差异 | Raven 是**多 agent 编排平台 + 外部 API 模型 + 外部 benchmark**；GameRSI 是**单 agent + 单本地模型 + 自建游戏沙箱 + 不可作弊测量** |

**一句话定性**：Raven 是一个工程上极其扎实、但「自进化」部分目前是**两套未合并的实验机制**的大型 pre-alpha 平台。论文中关于自进化的章节（§4/§7.2）对应的是其中一套机制（`evolver/`，且其结果转引自另一篇论文 HarnessBank），而 README 主推的「Runtime Self-Evolution」是另一套（`experimental/curator`，论文里**一次都没提到**）。这个事实决定了 GameRSI 应该借鉴什么、忽略什么。

---

## 2. 论文核心（raven.txt）

### 2.1 RSI 机制设计（§4，`raven.txt:1550-1961`）

Raven 的自进化是**在冻结模型上改 harness**（`raven.txt:1551`），不是改权重。三个角色分离：Task Agent 执行任务、Evolver Agent 提议修改、**固定的** Evaluator 测量结果（`raven.txt:1555-1557`）。

**(a) 可编辑的策略接口。** harness 形式化为 `h = (id_h, Mem_h, Plan_h, Cap_h, Act_h; θ_h)`（`raven.txt:1601`，Eq.39）——记忆（组装工作上下文）、规划（准备每轮建议）、能力（选择暴露给模型的工具集）、动作（获取模型响应并判决）。这四个角色在代码里确实存在：`raven/agent/harness/{memory,planning,capability,action}.py`（合计 1011 行）。

**(b) 可编辑空间的形式化。** `X_edit`（允许改动位置）与 `K_fixed`（不可变位置）互斥；搜索空间 `H(X_edit)` 要求 `scope(Δpatch) ⊆ X_edit`、`h|K_fixed = h0|K_fixed`、`InterfaceValid(h)=1`（`raven.txt:1646-1656`，Eq.42）。**不可变内核保护的是评估、记账和必需的执行接口**（`raven.txt:1641-1642`）。

**(c) 诊断驱动。** 每个提议 = 可执行 patch + 语义描述符 + **在候选评估之前就固定**的 activation specification（`raven.txt:1709-1711`）。关键约束：**Evaluator 独立于 Evolver 的解释来记录 activation**（`raven.txt:1711-1714`）——这是防止自我确认的核心设计。

**(d) 语义基因库（GSME）。** bank 按 `(编辑类别 w ∈ {prompt, knowledge, runtime, config}) × (失败病理 y)` 分格，每格一个 elite，是有质量-多样性搜索（`raven.txt:1724-1741`）。论文明确：**「Pathology labels remain hypotheses, not established causes」**（`raven.txt:1741`）——标签是假设不是因果。

**(e) 门控筛选。** 三道闸（`raven.txt:1794-1886`）：
- **有效性** `g_valid`：日志必须协议有效；
- **激活** `g_act`：声明机制至少执行一次（存在性闸，不要求每次都触发，也不建立因果贡献，`raven.txt:1822-1825`）；
- **配对增益** `g_sig`：`γ̄ > 0` 且 `t_pair ≥ λ_screen = 1.96`（`raven.txt:1867-1873`）。论文明确「不把它解释为精确的有限样本双侧 5% 检验」「不分配校准 p 值」（`raven.txt:1867-1871`）。

**(f) 最终选择。** Algorithm 4（`raven.txt:1930-1950`）在 train 上选完后冻结，再在同一个 held-out 协议下评估；**「Test outcomes do not revise proposals, thresholds, archive cells, or the selected harness」**（`raven.txt:1955-1961`）。论文自陈「重复的自适应筛选不提供 family-wise 显著性保证」。

### 2.2 DAG 编排与多 agent 架构（§3，`raven.txt:783-1548`）

- **节点生命周期**：pending → running → judge → completed/exception/failed/skipped/cancelled（`raven.txt:970-1003`）。异常**不传播**，因为 host 可能修复该节点。
- **准入检查**（论文 Table 1，`raven.txt:980-995`）：格式 / 图结构（无环、依赖合法、路径不越界）/ agent 能力 / agent 状态 / 环境（dispatch budget 等）。
- **依赖就绪调度**：`Submitted`（已提交）与 `Unsettled`（待裁决）两个集合防止重复提交和读取未裁决输出；共享并发信号量（`raven.txt:1005-1024`）。
- **完成裁决**：返回响应本身不代表任务完成；**一次独立模型调用**读 rendered prompt + 输出 + transcript 尾部来判决（`raven.txt:1026-1036`）。论文这里有与 GameRSI 直接相关的自陈：**「the verdict does not certify objective correctness. Under the availability fallback, a normally returned call is treated as accomplished if the judge fails or times out」**（`raven.txt:1037-1039`）。

### 2.3 Multi-Agent Orchestration Benchmark（§7.1，`raven.txt:2734-3021`）

- **构成**：140 个职业任务请求，配 4 个专家（research/coding/content/oncall）上的参考 DAG；11 种域组合全覆盖（2 域 66 / 3 域 47 / 4 域 27）。参考图平均 2.72 节点、1.84 条边，共 257 条边；98 个纯串行、42 个含并行（`raven.txt:2821-2832`）。
- **构造方法**：**先定参考图再生成请求**（reference-graph-first），用 Claude Opus 5 写图、GLM-5.2 反向生成请求文本；**泄漏过滤**拒绝枚举步骤（first…then…finally）或直接点名域的请求（`raven.txt:2751-2774`）。作者承认「词法过滤不能排除改写过的规划线索」（`raven.txt:2773-2774`）。
- **四个指标**（`raven.txt:2834-2936`，全部在域级折叠后的图上算）：**Node F1**（专家选择）、**Edge F1**（在传递归约上算，冗余边不扣分）、**Partial Order Accuracy**（共享节点对的先后）、**Exact Match**（节点集 + 可达性都相同）。四者**不合成单一分数**。
- **结果**（`raven.txt:2957-3021`，同 backbone 对比 Claude Code / Hermes Agent）：

| 指标 | Qwen3.8-27B：Raven vs 最强基线 | DeepSeek-V4-Flash-0731：Raven vs 最强基线 |
|---|---|---|
| Node F1 | **0.923** vs 0.776（+0.147） | **0.963** vs 0.854（+0.109） |
| Edge F1 | **0.812** vs 0.744（+0.068） | **0.897** vs 0.813（+0.084） |
| Partial Order Acc | **0.950** vs 0.780（+0.170） | **0.918** vs 0.844（+0.074） |
| Exact Match | **0.711** vs 0.607（+0.104） | **0.867** vs 0.762（+0.105） |

Edge F1 在所有系统上都低于 Node F1——依赖预测是更难的部分（`raven.txt:3016-3021`）。

### 2.4 自进化实验结果（§7.2，`raven.txt:3023-3077`）——本报告最关键的事实

论文 §7.2 的七项 benchmark 结果**不是 Raven 仓库跑出来的**：

> 「The published HarnessBank experiments [39] evaluate the diagnosis, search, and screening procedure on tasks withheld from evolution.」（`raven.txt:3026-3027`）

附录 B.4 更明确（`raven.txt:4903-4912`）：

> 「The seven benchmark results in Section 7.2 and Table 11 are **reproduced from HarnessBank v2**, Section 4 and Table 1 [39]. The reported experiments use a frozen Qwen3.6-27B task model, **Claude Opus 4.8 as the evolver**, and three attempts per task...」
> 「The source paper does not provide a complete per-run manifest for the seven results... **Reconstructing those runs therefore requires their run records.**」

转引的数字（Table 11，`raven.txt:4984-4991`；frozen Qwen3.6-27B，held-out Pass@1，K=3）：

| Benchmark | Initial → Evolved | 增益 |
|---|---|---|
| AppWorld | 41.3 → 56.7 | **+15.4** |
| BrowseComp+ | 16.9 → 30.8 | **+13.9** |
| LiveCodeBench | 58.1 → 71.8 | **+13.7** |
| Omni-MATH | 54.3 → 66.0 | +11.7 |
| Terminal-Bench-2 | 36.1 → 45.4 | +9.3 |
| GDPval | 43.7 → 52.9 | +9.2 |
| SWE-bench Verified | 47.4 → 52.6 | +5.1（26 任务 test split，**未通过配对增益判据**） |

论文自陈：7 个对比中 **6 个**通过源论文的配对增益判据（`raven.txt:3041-3042`）。

**其余结果**（Figure 1，`raven.txt:31-85`，均为同 backbone 下 vs 最强基线）：Raven-Research 在 DeepResearch Mixed 上 +6.6 / +3.3 / +7.6 pp（三 backbone）；Raven-Code 在 SWE-bench Pro +2.0pp（54.4 vs 52.4）、SWE-bench Verified +0.6pp（91.0 vs 90.4）、WorkBuddy-Code +1.4pp、SWE-Refactor +9.5（7.0→16.5）；Raven-Design +1.9~+2.8pp；Raven-Oncall **+17.6pp 成功率（82.35% vs 64.71%）且成本降 62.5%（$5.10 vs $13.60）**。

### 2.5 论文自陈的局限与失败案例

1. **增益不稳定 / 只部分迁移 / 强依赖执行模型**：引用 HarnessDev 的实证结论——「harness gains obtained by evolution can be unstable, transfer only partially to held-out tasks, and depend strongly on the model that executes the harness」（`raven.txt:1790-1792`）。
2. **信用分配问题**（§8.2）：「A candidate may change a recovery branch, a memory instruction, or a tool policy while the measured task score reflects all of them together. **Without a stable edit vocabulary and a fixed screening protocol, the evolver can select a patch whose causal effect is unclear or whose gain does not transfer.**」（`raven.txt:3819-3823`）。这正是 GameRSI 把「奖励投机」列为一等公民失败模式的同一类风险。
3. **判决不保证客观正确性**：DAG 完成判决「does not certify objective correctness」，且 judge 失败/超时即算完成（`raven.txt:1037-1039`）。共享组记忆 verdict「remain observational judgments by the host and may be incorrect」；组记忆默认关闭且**未在本报告中评估**（`raven.txt:1539-1541`）。
4. **无族错误率保证**：自适应筛选不提供 family-wise 显著性保证（`raven.txt:1958-1960`）。
5. **MAOB 的构造局限**：域组合覆盖对 4 专家全集是穷尽的，但不覆盖所有职业场景或工具配置（`raven.txt:2747-2749`）；参考图本身是模型生成 + 人工评审的，不是唯一正确解（`raven.txt:2927-2933`）。
6. **复现缺口**：源论文未提供完整 run manifest（`raven.txt:4909-4912`）。

---

## 3. 代码级机制拆解

### 3.1 `evolver/` 自进化循环：七步漏斗的状态机化

核心设计理念（`evolver/orchestrator/DESIGN.md:84-101`）：**控制反转**——把「循环如何推进、何时停止、不要早退」从 prompt 里搬到确定性状态机里，模型每步只做一次**范围受限 + schema 校验 + 有界重试**的判断。目的是让弱模型（qwen/kimi）也能驱动整个循环。

**七步循环**（`evolver/orchestrator/DESIGN.md:18-66`，代码在 `evolver/orchestrator/loop.py`）：

| 步 | 内容 | 代码位置 |
|---|---|---|
| 冷启动 | 跑未修改基线 K 次，产生**完整基线记录** + failure map；基线是全 run 的**固定比较基准** | `loop.py:208-225`（`backend.cold_start()` → `FrozenColdStartBaseline`） |
| ① 诊断 | 读失败轨迹分类成 WHY；failure map **跨轮累积**；已诊断过的 parent 不重复诊断 | `loop.py:442-447`；`nodes/diagnose.py:1-40`（复用 judge 栈） |
| ② 选靶 + 设计 | 确定性 WHY 排序（按 why_distribution）+ 驱动模型设计 patch；**每个 patch 必须环境变量门控、默认关闭，关闭时与基线逐字节相同** | `nodes/design.py:43-77`（确定性选靶）、`nodes/design.py:94-117`（prompt 里写死 "MUST be env-gated and default OFF"） |
| ③ 零成本预筛 | 触发条件在历史轨迹里从未命中 = 永不激活，零推理成本砍掉；崩溃型和惰性型都抓 | `loop.py:450-469`；`production.make_zero_hit_preflight`（**默认关闭**，见 3.6） |
| ④ 应用 + 建节点 | git apply 到 parent，产生真实子 commit + 节点台账 | `loop.py:510`；`tree/store.py` |
| ⑤a 筛选 | 小子集 K=1 探针，**宽通**：只砍明显更差的 | `gates/strategies.py:46-97 / 100-221` |
| ⑤b 全量确认 | 幸存者跑全 train 集 K=3 | 同上 |
| ⑥ 三闸门 | 见 3.2 | `gates/pipeline.py:54-106` |
| ⑦ 选父 + 终止 | **argmax**：本轮最佳 gate-passer 只有严格超过 incumbent 的 train 分才接管；平局保留现任 | `loop.py:592-599`；`termination.py:29-80` |

**终止三条件**（`evolver/orchestrator/termination.py:67-80`）：`patience=10` 连续轮无候选超过 vanilla / `max_rounds=20` 硬上限 / `max_consecutive_errors=5` 连续整轮错误。**关键纪律**：耐心信号永远对 **vanilla** 比、永不对上一轮 parent、**永不对 test 集**（`termination.py:1-22`）。

**鲁棒性设计**：任何单候选/单轮的错误都被 catch 成 `errored` 记录在台账里，绝不中止整个 run（`loop.py:506-550`）。

### 3.2 三闸门（Gate-f → Gate-b → Gate2）

`evolver/orchestrator/gates/pipeline.py:54-106`：

- **Gate-f（测量有效性）**：基础设施失败（崩溃/超时）**不得从分母里剔除**——「dropping shrinks the denominator and overestimates pass@1」（`pipeline.py:6-9`）。可恢复的基础设施失败先走重试阶梯，仍失败则记 0 分但保留在分母里。
- **Gate-b（归因）**：只在候选机制**真正触发**的任务上给功劳——「a patch can't get credit for a task it never touched」（`pipeline.py:14-17`）。触发集来自 activation ledger（beacon）。Python patch **必须**包含 `activation_beacon()` 调用，缺了直接拒（`DESIGN.md:118`）。三态语义：无仪器数据 = None = fail-open 跳过；有标记零触发 = 诚实的 0 = 不归因。
- **Gate2（显著性）**：配对提升 + 2σ（`gates/paired.py:61-102`）。**promote（入库）的判据是宽松的 navigator 条件（均值超过对照）**，2σ 是单独的 `credited_2sigma` 标签——两者分离（`paired.py:18-25`，`strategies.py:13-16`）。配对是关键：同任务配对消掉了任务间难度差，标准误是**任务级差值**的散布，不是原始通过率的散布（`paired.py:7-9`）。若候选让每个共享任务提升相同幅度，`se==0`，确定性胜利记为 `z=inf`（`paired.py:84-85`）。

**两条具体 gate 线**（`gates/strategies.py`）：`PairedTwoSigmaGate`（SWE 线，K=1 anchor 宽通 → K=3 全量三闸）与 `FocusedFisherGate`（AppWorld 线，聚焦子集单侧 Fisher 宽通 + 哨兵回归守卫 → 全量三闸）。

**一个值得 GameRSI 直接抄的实战经验**——**哨兵分层**（`loop.py:257-296`）：回归控制集一半取 stable-pass、一半取 borderline，且按候选 id 哈希轮换。注释记录了真实事故：**「a candidate with a 58% passing-task regression rate sailed through 3 stable sentinels」**（`loop.py:281-283`）——只用稳定任务做控制，会系统性对「过度触发的回归」失明。

### 3.3 sealed test：把「铁律」机制化

`evolver/orchestrator/sealed/runner.py` 是整个 evolver 里对 GameRSI 最有价值的单文件：

1. **`assert_no_test_leak`**（`sealed/runner.py:38-49`）：任何 test id 出现在 anchor 或 train 集里，启动时直接 raise。
2. **`SealedTestRunner.score` 返回 `None`**（`sealed/runner.py:72-95`）：测试结果写进一个决策路径**从不读取**的目录，「there is no path for a test number to reach a gate or a verdict」。只有循环结束后的 `unseal()` 才读回。
3. **固定分母**（`sealed/runner.py:82-84`）：test 任务没产出就记 0，绝不从分母里去掉。
4. **unseal 只按 train argmax 选交付物，绝不 max-over-test**（`sealed/runner.py:163-187`）：作者把理由写得很清楚——「picking the max of many noisy sealed measurements would inflate the reported gain by selection (winner's curse) and void the paper's no-multiple-comparison argument. Overfitting shows up as an honestly-low retention, not as a silently swapped deliverable.」
5. **`retention = test_lift / train_lift`**（`sealed/runner.py:107-112`）：泛化率，train 无提升则返回 None。

### 3.4 GSME 基因库与跨格重组

`evolver/orchestrator/archive.py`：

- **入库门槛 = 打败 vanilla**（`archive.py:269-272`）：故意不用可能已被棘轮抬高的 promotion 基线——「an independently-good mechanism is banked even when it loses to the current champion」。
- **WHERE 机械绑定**（`archive.py:134-160`）：格子的 WHERE 坐标由 patch **实际碰过的文件**决定（路径→lever 启发式），驱动模型自报的 `patch_where` 只进台账供审计、**永不决定坐标**——「zero modeling noise on the WHERE axis, noise concentrated on WHY」（`DESIGN.md:121`）。
- **重组**（`archive.py:296-323`）：把别的格子的 elite 编辑按字节叠到当前 parent 上。排除：lineage 已覆盖的格、已试过的配对、**改过同一个文件的配对**——「byte-level stacking cannot merge same-file edits, only replace — an overlapping "stack" would silently drop one mechanism and measure a lie」（`archive.py:300-304`）。
- 重组候选走与设计候选**完全相同**的 apply → gate 流程；archive 从不决定功劳（`archive.py:21-26`）。

### 3.5 不可变内核与白名单（对 GameRSI INV-1/INV-4 最直接对应）

- `evolver/applier/path_guard.py:42-94`：`IMMUTABLE_PATTERNS` 分五层（`path_guard.py:24-31`）：L1 自引用（`evolver/`）、L2 评估基底（`eval_engine` + 外部 grader + AppWorld 的 `grade.py`/`batch.py`）、L3 能力契约（agent loop / 工具注册 / providers / sandbox / session / memory / config schema）、L4 审计完整性、L5 测试与 CI。注释明说：**「a candidate that can edit it can reshape its own denominator」**（`path_guard.py:55-56`）。
- 匹配算法刻意不用 glob：精确路径 + 目录子树，「Glob patterns invite surprises」（`path_guard.py:19-22`）。默认 mutable-by-default（`path_guard.py:172-174`）。
- `benchmarks/appworld/evolve/sandbox.py:16-27`：白名单前缀只有 `benchmarks/appworld/` 和 `raven/agent/`，其他改动一律 revert。设计哲学是 **「edit-then-commit」**——让驱动模型在 worktree 里自由 bash 编辑，再捕获它实际改了什么，因为「qwen is unreliable at writing applyable unified diffs」（`sandbox.py:4-10`）。

**Raven 自己的威胁模型自陈**（`evolver/README.md:206-210`）——GameRSI 必须认真对待：

> 「**These guards stop an LLM designer from taking shortcuts, not a determined adversary.** Candidate code runs in the same process as the (immutable-on-disk) grader and the benchmark oracle is reachable over HTTP, so **in-memory tampering or oracle probing is possible in principle.** Promoted candidates are a handful of small commits — diff-audit them before citing numbers.」

### 3.6 诚实的差距清单

`docs/specs/self-evolution-loop-raven-mapping.md:114-139` 明列了未接线部分：zero-hit 预筛默认关闭（`mapping.md:116-122`）；`tree_aware_bandit` 借用机制**存在但未接线**（`mapping.md:123-126`）；anchor 的 affinity 数据源未移植（`mapping.md:127-131`，上游 `affinity_picker.py` 未 port）；WHERE 只有 4 档比 judge schema 的 14 类粗。同时 `mapping.md:92-112` 也列了超出 SOP 的地方（sealed-test 机制化、QD 重组、inert 死亡反馈环、harm replay）。

### 3.7 Host Agent 核心循环

- **四策略座位**确实落地：`raven/agent/harness/{memory,planning,capability,action}.py`。值得注意 `planning.py:7-17`——**默认 Planning 是透传**，且注释明确记录了删掉全局规划器的理由：「the party with the least context made the most expensive call. It was removed.」（`planning.py:11-14`）。这是一个 GameRSI 值得吸收的反例：不要在缺历史时就做最贵的决策。
- **AgentLoop**：`raven/agent/loop/main.py:67`（913 行），组合 `TurnPathMixin / WiringMixin / McpGlueMixin / OrganGlueMixin`。
- **DAG 编排**全在 `raven/agent/subagent/dag_*.py`（`dag_tool.py` 2456 行 + `dag_runner.py` 2053 + `dag_graph.py` 644 + `dag_adjudication.py` 425 + `dag_verdict.py` 348 等，合计 6000+ 行）：`dag_graph.py:36-60` 是唯一的节点定义 `DagNodeSpec`（playbook 与 DAG 共用，防止三套定义漂移）；准入校验在 `dag_graph.py` 的 `validate_and_order`；并发调度在 `raven/spine/scheduler.py`（679 行，lane/worker 模型，每会话串行 + 取消）。
- **完成裁决**：`raven/agent/subagent/dag_verdict.py`。两个细节对 GameRSI 极有教益：
  - **fails open**：「A judge call that raises, times out, or answers without calling the tool yields `accomplished`... failing closed would suspend every node of every graph on one provider hiccup, an outage worse than the bug this fixes」（`dag_verdict.py:12-16`）。
  - **防自证**：判决必须走 tool call 而非散文，因为「a node that writes "verdict: accomplished" into its own answer must have no route to the outcome. Prose would be that route; a tool argument is not.」（`dag_verdict.py:78-81`）。

### 3.8 记忆持久化：EverOS 是可插拔后端，不是硬依赖

- `raven/memory_engine/` 提供 `MemoryBackend` 插件契约 + 共享契约测试（`memory_engine/contract_test.py:1-34`）；EverOS 是**独立发行** `plugins-dist/everos-memory`（`pyproject.toml:151, 286`），不是核心循环的硬依赖。
- `store_pipeline.py` 做轮次外的异步投递（per-session 串行，因为 EverOS 写是 append + consolidate，`store_pipeline.py:83-85`）。
- 语义固化在 `memory_engine/consolidate/`（consolidator / attention / behaviors_extractor）。
- 论文里聚类参数（cosine 0.65 / 7 天窗口）见 `raven.txt:4875`。

### 3.9 Skill Forge

- `raven/memory_engine/skill_forge/`：`router.py` 用加权 RRF 融合多源排序（`router.py:57-70`，`fusion.rrf_merge_weighted`），over-fetch 后收窄；`gate.py` 做安全门禁；三源 local / memory / hub 权重 1.0 / 0.9 / 0.85（论文 `raven.txt:4883`）。
- `raven/skill_hub/`：外部 SkillHub 语料的 client / policy / audit。
- **技能自演化**（论文 §6.3）在仓库内证据薄弱：仓库只 35 个 `SKILL.md`，且多数属于 `experimental/simulation` 的场景素材。`skill_forge/` 是**检索**层，从经验里提取新技能的生成层未见独立实现。

### 3.10 重大发现：仓库里有**两套**自进化机制，论文只讲了一套

| | `evolver/`（离线 benchmark 驱动） | `experimental/curator/`（运行时、人在环） |
|---|---|---|
| 论文对应 | §4 + §7.2（但结果转引自 HarnessBank） | **论文 0 次提及 "Curator"**（我对全文做了 grep，0 匹配） |
| README 位置 | `README.md:283` 一行表格条目 | `README.md:188-196` 大段专题「Runtime Self-Evolution」，并称「Raven is built for this from the ground up」 |
| 机制 | 诊断失败 → 设计 patch → 三闸门 → 入库；git commit 即候选 | Curator 改 harness 的四个策略面；**交互方给反馈**，多轮迭代 |
| 评分 | 外部 benchmark 的程序化 scorer | 交互方/评价者（Analyst）的判断 |
| 状态 | `evolver/README.md:3-5`：**「retirement of this tree is planned... pending partner sign-off」**（2026-09-01 的 step-8 退休波） | `README.md:192`：「**The Curator is experimental: it ships with the repository rather than the installed package.**」 |
| 验证 | 126 测试 + 1 轮真实 AppWorld round | 旅行社模拟场景 + 一个展示案例（`experimental/simulation/cases/s0925c/`，第 3 轮全部红线通过） |
| 对 raven/ 的侵入 | 通过 `benchmarks/` 与 `evolver/` 分离 | **零侵入**：「整个目录对 Raven 源码零侵入：`raven/` 没有为它改动，也不引用它」（`experimental/README.md`） |

这个分裂对 GameRSI 的借鉴决策至关重要：**论文 §4 那套严谨的统计门控机制，在仓库里是一个正在被安排退休的树；README 主推的自进化是另一套人在环的、论文里没写的实验机制。**

---

## 4. 成熟度与可信度评估

### 4.1 「声称」vs「实际实现」

| 主张 | 来源 | 实际状况 | 判定 |
|---|---|---|---|
| 自进化在 7 个 benchmark 上 held-out 提升 +5.1~+15.4 pp | `raven.txt:3023-3042` | **转引自 HarnessBank v2**（`raven.txt:4903`）；本仓库未复现；源论文无完整 run manifest | **非本仓库证据**。可作为「该方法论在天时地利下能达到的上限」参考，不可作为「Raven 这套代码能复现」的证据 |
| 诊断 / 搜索 / 筛选协议已实现 | `raven.txt:1554` | `evolver/` 约 13,600 行，SOP→代码映射表完整（`DESIGN.md:107-127`），且 mapping doc 诚实列出未接线项 | **真实实现**，完成度高，但有个别未接线（3.6） |
| 全自动七步循环可无人值守跑完 | `evolver/README.md:20-24` | `evolver/README.md:187-189` 自陈：「**Not yet exercised at scale:** a full-size run (90 tasks x K=3, multi-round, hours) and the sealed-test unseal/retention path on a real benchmark have only been exercised via tests, not a production run.」 | **机制已实现，规模未验证**。真实跑通过的最完整闭环是**一轮** AppWorld（`DESIGN.md:176-180`） |
| MAOB（140 任务新 benchmark） | `raven.txt:2735` | 仓库内**找不到** MAOB（我对全仓库 grep `MAOB\|multi_agent_orchestration` 无匹配）；`benchmarks/` 只有 appworld / clawbench / pinchbench / proactivity_eval / skill_evals | **未随论文发布** |
| 四策略接口（Mem/Plan/Cap/Act） | `raven.txt:1601` | `raven/agent/harness/{memory,planning,capability,action}.py` 真实存在（1011 行）；Planning 默认透传 | **真实实现**，但 Planning 是透传默认值 |
| Runtime Self-Evolution（Curator） | `README.md:188-196` | 在 `experimental/`，**论文未提**，不随安装包发布 | **实验性**，且与论文的 evolver 是两条路线 |
| SkillHub 114,190 技能 | `README.md:285` | 外部语料（SkillCorpus），仓库内仅 35 个 SKILL.md | **外部依赖**，非本仓库能力 |
| 第三方 agent 适配 | `README.md:200` | `bridge/` 是 WhatsApp 桥（误导性命名）；真实接入在 `raven/agent/subagent/` 的 ACP/CLI/OpenAI 后端 + `presets.py` / `vendored_agents.py` | **真实实现**，但在 `raven/agent/subagent/` 而非 `bridge/` |
| 「pre-alpha」 | `README.md:31` | 与观察一致：核心循环庞大（245k 行）但自进化部分是实验分支、关键 benchmark 未发布、evolver 待退休 | **自陈准确** |

### 4.2 定量成熟度信号

- **测试**：956 个测试文件；evolver 相关 9 个文件，但 `DESIGN.md:181-182` 记载「full evolver suite 545 passed / 16 skipped」，`evolver/README.md:181-186` 记载 126 个测试覆盖 gate 数学 / infra 重试阶梯 / git 原语 / 沙箱白名单 / 驱动传输 / 配置 / 守卫 / 幂等 / interrupt→resume→finalize。测试质量看起来很高（连「中断→恢复→定案」都有自动化测试）。
- **文档质量**：`evolver/` 的文档质量**显著高于**一般开源项目——`DESIGN.md` 把 SOP 每一步映射到代码并标注偏差，`mapping.md` 主动列出未接线部分，`README.md` 主动列出已知局限和威胁模型。这是强可信度信号。
- **开发节奏**：git 历史被 squash 成 1 个 commit，无法从 commit 评估节奏；但 `docs/specs/` 有 60+ 个带日期的设计文档（2026-08-05 → 2026-09-21，几乎每个工作日一份），CHANGELOG Unreleased 段落极长。**开发强度很高**。
- **工程规范**：`AGENTS.md` 有硬约束（uv only / Conventional Commits / 全英文源码 + 命名豁免区 / 文件大小门禁 / CI 检查脚本），`pyproject.toml` 34KB，有完整的 lint/依赖分层。这是一个**认真维护的生产级代码库**，只是自进化部分不在主干路径上。

### 4.3 可信度结论

**可信的部分**：
1. `evolver/` 的**方法论代码化**质量极高，且明显是在真实数据上迭代过的（哨兵分层、WHERE 机械绑定、unseal 防 winner's curse 等设计都带有「在这里栽过跟头」的具体性）。
2. 论文对自进化**局限的自陈是诚实的**（`raven.txt:1790-1792, 3819-3823, 1958-1960`）。
3. 仓库的威胁模型自陈诚实（`evolver/README.md:206-210`）——明确说自己的守卫挡不住有决心的对手。

**不可信的部分**：
1. **自进化的量化结果不是这个仓库产出的**，且 `evolver/` 树即将退休——不能把 Raven 当作「已验证的 RSI 平台」。
2. **README 主推的自进化特性（Curator）论文里没有**，是 experimental 且不随包发布。
3. **论文的新 benchmark（MAOB）未随论文发布**，无法独立核验其 140 个任务与评分约定。
4. 全规模自进化运行（多轮、小时级、真实 benchmark）**从未在生产中跑过**。

**对 GameRSI 的含义**：Raven 的价值不在「可复现的 RSI 平台」，而在**「一份把 RSI 方法论编译成确定性代码的参考实现」**——`evolver/` 的控制反转、三闸门、sealed-test 机制化、GSME、不可变内核，都是可以直接迁移到 GameRSI MOD-06/07/08 的**设计模式**，而且因为 GameRSI 有环境原生的胜负（INV-7），Measurement 这层可以做得比 Raven 更硬。

---

## 5. 与 GameRSI 的对标矩阵

### 5.1 可直接借鉴（机制形状对，改名即可落地）

| # | Raven 机制 | 代码 | GameRSI 落点 | 说明 |
|---|---|---|---|---|
| 1 | 七步漏斗 = 确定性状态机（控制反转） | `evolver/orchestrator/loop.py:168-617` | MOD-06 loop / MOD-07 evolve | 「何时停、不要早退」是代码不是 prompt；本地 8B 模型指令遵循弱，这条比 Raven 更必要 |
| 2 | 冷启动冻结基线（vanilla 是全 run 固定比较基准） | `loop.py:208-225` | MOD-08 三基线入档 | 已与 GameRSI 的 random/heuristic/fixed_prompt 三基线对齐 |
| 3 | argmax 父选择 + 平局保留现任 | `loop.py:592-599` | MOD-07 接受/回滚 | 保证 champion 链单调，无 churn |
| 4 | 终止三条件（patience 10 / max_rounds 20 / errors 5），耐心信号只对 vanilla、**从不对 test** | `evolver/orchestrator/termination.py:29-80` | MOD-06 代际调度 | errored 轮不烧 patience（单独计数） |
| 5 | **sealed test 三重机制化**：启动泄漏断言 + score 返回 None + unseal 只按 train argmax | `evolver/orchestrator/sealed/runner.py:38-49, 72-95, 163-187` | MOD-08 / **INV-5** | 直接对应 GameRSI 的 train(1000–1999) / heldout(2000–2099) / test(9000–9099) 三分；`assert_no_test_leak` 的等价物 = seed 集合交集断言，M0 就应写 |
| 6 | retention = test_lift / train_lift | `sealed/runner.py:107-112` | MOD-08 验收 | 比「绝对分数」更能暴露过拟合 |
| 7 | 配对统计（任务级差值的标准误，非原始通过率）+ 宽松 promote / 严格 credited 两标签分离 | `evolver/orchestrator/gates/paired.py:61-102` | MOD-08 指标 | GameRSI 每代 165 局，任务级配对比 Wilson CI 更灵敏；两标签分离避免「不显著但被采纳」 |
| 8 | **宽通筛选**：小样本探针只砍明显更差的 | `evolver/orchestrator/gates/strategies.py:13-16` | MOD-07 候选筛选 | K=1/K=3 探针方差巨大，「略低」不构成证据 |
| 9 | flip table（rescued/regressed/still-failing）反馈给下一轮设计 | `loop.py:553-564`；`evolver/orchestrator/scoring.flip_summary` | MOD-06 反思 / MOD-07 候选生成 | 让下一轮看到因果反馈而非静态失败集 |
| 10 | failure map 跨轮累积 + 已诊断 parent 不重复诊断 | `loop.py:432-447` | MOD-05 反思库 / MOD-06 | 省驱动模型算力，避免重复计数 |
| 11 | 不可变内核（L1 自引用 / L2 评估基底 / L3 能力契约 / L4 审计 / L5 测试） | `evolver/applier/path_guard.py:24-94` | MOD-02 / **INV-1/INV-4** | 直接映射 GameRSI：评估代码、沙箱门面、prompt 注册表必须对 agent 不可编辑 |
| 12 | 白名单 + 越界一律 revert | `benchmarks/appworld/evolve/sandbox.py:16-27` | MOD-02 受限执行 | GameRSI 更严：无网络/无文件写/无 subprocess（INV-4） |
| 13 | env-gated default-off patch（关闭时逐字节等于基线） | `evolver/orchestrator/nodes/design.py:94-117` | MOD-07 | 保证基线永不被污染 |
| 14 | git commit = 候选身份；journal 断点续跑 + config fingerprint | `evolver/launch/runner.py:1-45`；`evolver/orchestrator/state/journal.py` | MOD-09 git_ops / MOD-06 续跑 | config 漂移拒绝恢复——候选臂与对照臂必须可比 |
| 15 | 台账：每候选一个 JSON（身份 + git anchor + 状态 + gate 统计） | `loop.py:637-675` | MOD-09 / MOD-07 代际日志 | 审计链 |
| 16 | 「不在缺历史时做最贵的决策」——默认 Planning 透传 | `raven/agent/harness/planning.py:7-17` | MOD-04 | GameRSI 的 prompt 组装应同样保持薄 |
| 17 | 判决走 tool call 不走散文，防自证 | `raven/agent/subagent/dag_verdict.py:78-81` | MOD-04 动作校验 | 任何「agent 自评」都必须无法被 agent 自己的输出污染 |

### 5.2 需改造（形状对，但假设不同）

| # | Raven 机制 | 差异 | GameRSI 改造方向 |
|---|---|---|---|
| 1 | **Gate-b activation beacon**（`evolver/orchestrator/gates/pipeline.py:14-17`；`DESIGN.md:118`） | Raven 靠候选代码里插入 `activation_beacon()` + 子进程注入私有 beacon 目录 | GameRSI 的「机制是否执行」应来自**环境原生事件**（如本轮是否调用了 reroll/是否进入某决策分支），由 MOD-02 审计器记录，而非候选自报——更满足 INV-2/INV-7 |
| 2 | **Gate-f 基础设施失败处理**（`gates/pipeline.py:5-13`） | Raven：infra 失败重试≤2 次后仍失败记 0 但留分母 | GameRSI 已定「截断一律计 lose」，口径更严；但应保留「**推理失败 ≠ 游戏失败**」的区分（MOD-03 JSON 鲁棒性报告已覆盖），否则模型抽风会污染基线 |
| 3 | **哨兵分层回归控制**（`loop.py:257-296`） | Raven 用稳定通过/边缘任务做回归控制 | GameRSI 的等价物：稳定通关 seed / 边缘 seed（胜率 45–55%）。**直接吸收那条 58% 回归穿透 3 个稳定哨兵的教训**（`loop.py:281-283`） |
| 4 | **GSME (WHERE×WHY) 基因库**（`evolver/orchestrator/archive.py:218-323`） | Raven 的 WHERE 有 prompt/knowledge/runtime/config 四类，因为它的可编辑面是整个 agent 树 | GameRSI 可编辑面只有三类（prompt 增删改 / 决策工具函数补丁 / 反思模板，见 MOD-07）；格次维度可降为 (编辑面 × 失败病理)。**WHERE 仍应机械绑定**（`archive.py:145-160`）而非自报 |
| 5 | **诊断 WHY 分类**（`nodes/diagnose.py`） | Raven 用 LLM judge 把轨迹分类成 14 类缺陷 | GameRSI 的失败类型更结构化（败因可由环境原生数据部分推出：剩余心、关卡、骰子使用）；LLM 诊断只作辅助，**病理标签始终视为假设**（`raven.txt:1741`） |
| 6 | **记忆 / 语义固化**（`raven/memory_engine/`） | EverOS 是独立服务 + HTTP + 向量索引 | GameRSI 单机本地：sqlite/json + 显式 seed 化的检索即可；但「episodic → consolidation → 检索」三段生命周期与「异步落盘不阻塞主循环」（`store_pipeline.py`）值得吸收 |
| 7 | **单轮多次尝试 K_att=3**（`raven.txt:1663-1670`） | Raven 每任务 3 次取均值 | GameRSI 每代 165 局已是多次采样；注意 Raven 的提醒：二值分数下这是「平均每次成功概率」不是「K 次里至少成功一次的概率」（`raven.txt:1776-1777`） |
| 8 | **人在环门** | Raven 有两处：Curator 全程人在环（`experimental/`）；evolver 的 `finalize` 需 `--yes` | GameRSI MOD-07 已列「人在环确认门」；建议只在「晋升到新基线」和「test 解锁」两个点设门，避免每代都打断 |
| 9 | **`λ_screen = 1.96` 固定正态截断**（`raven.txt:1867`） | Raven 明说不把它当精确有限样本 t 检验 | GameRSI heldout n=40，可直接用配对 t 的正确自由度（n−1），并报 Wilson CI；比 Raven 更严谨一档 |

### 5.3 不适用（多 agent 编排特定，GameRSI 单 agent）

| # | Raven 机制 | 为何不适用 |
|---|---|---|
| 1 | DAG 多 agent 编排（`raven/agent/subagent/dag_*.py`，6000+ 行）、Host Agent 分解/匹配/整合 | GameRSI 是单 agent 单模型；四专家匹配无对象 |
| 2 | MAOB 及其四个指标（Node/Edge F1、POA、Exact Match） | 测的是「规划图 vs 参考图」；GameRSI 没有参考图，只有环境胜负 |
| 3 | 完成裁决 judge（`raven/agent/subagent/dag_verdict.py`） | **并且直接违反 INV-7**：它是 LLM 自评且 fails open（`dag_verdict.py:12-16`）。GameRSI 的主指标只能是环境原生胜负。论文自己也承认「the verdict does not certify objective correctness」（`raven.txt:1037`） |
| 4 | Skill Forge / SkillHub 检索（`raven/memory_engine/skill_forge/`） | GameRSI 单游戏域，程序性知识直接进 MOD-05 反思库；不需要跨域技能语料 |
| 5 | 无 benchmark 的 LLM judge gate 线（`evolver/orchestrator/loop.py:10` 提到的 "no-benchmark LLM judge"） | **GameRSI 明确禁止**：INV-7 主指标只用环境原生结果 |
| 6 | ACP / A2A / 13 个第三方 agent 预置 | 无第三方 agent |
| 7 | Curator 的「交互方给反馈改进」范式 | GameRSI 的反馈来自 held-out 裁判，不是人；但「多轮迭代到全部红线通过」的验收思路（`experimental/simulation/cases/s0925c/`）可作 M3 验收的叙述模板 |

### 5.4 关于 INV-7 的正面结论

Raven 的自评估**确实**存在违反 GameRSI INV-7 的设计，且**论文自己给出了警告**：

1. DAG 完成判决是 LLM 自评 + fails open（`raven.txt:1037-1039`）；
2. 共享组记忆的 verdict「remain observational judgments by the host and may be incorrect」，且组记忆默认关闭、未评估（`raven.txt:1539-1541`）；
3. 深度研究的最终核查是「an independent reviewer, a separate model call without the agent's context」（`raven.txt:3090-3092`）——注意这是**独立模型调用**，不是环境证据；
4. Raven-Research 的 benchmark 准确率由 LLM judge 打分（`raven.txt:3098-3099, 5007-5010`），且「Regrading the same answers changed accuracy by approximately 1.3 percentage points」（`raven.txt:5010-5011`）——**judge 本身有约 1.3pp 的重评噪声**。

**但关键在于**：Raven 的**自进化循环（evolver）恰恰不用 LLM judge 做主评分**——它用 benchmark 的程序化 scorer（AppWorld 的 `grade.py`，且被列入不可变内核 `path_guard.py:49-57`）。这说明 Raven 自己也把「评分不可被候选污染」当原则。GameRSI 应继承这个原则并做得更彻底：**主指标 = 环境原生胜负，且打分代码在不可变内核里（INV-1/INV-4/INV-7 三条联合保证）**。LLM 只能用于诊断（WHY 分类）与候选生成，且其产出必须被视为假设（`raven.txt:1741`）。

---

## 6. 风险与陷阱

1. **最大陷阱：把 Raven 的自进化数字当作「这套代码的证据」。** §7.2 的七项结果是转引自 HarnessBank v2（`raven.txt:4903`），且源论文无完整 run manifest（`raven.txt:4909-4912`）。更糟的是 `evolver/` 树**自身标记即将退休**（`evolver/README.md:3-5`）。**结论：只借鉴设计模式，不假设可复现性，不要 fork `evolver/`。**

2. **两套自进化机制混淆。** 论文只写 evolver，README 只推 Curator。GameRSI 需要的是前者（不可作弊测量 + 统计门控），不是后者（人在环 + 交互方反馈）。

3. **论文自己承认的信用分配风险**（`raven.txt:3819-3823`）：候选可能同时改了恢复分支、记忆指令、工具策略，而任务分数是三者合起来测的；没有稳定编辑词表和固定筛选协议，evolver 可能选出「因果不明或增益不迁移」的 patch。**GameRSI 对策**：MOD-07 的可编辑面收窄到三类 + WHERE 机械绑定 + 每个候选单机制 + Gate-b 归因。

4. **Raven 守卫的真实强度低于 GameRSI 要求。** 「These guards stop an LLM designer from taking shortcuts, not a determined adversary... in-memory tampering or oracle probing is possible in principle」（`evolver/README.md:206-210`），且「the design-step sandbox is not filesystem/network jailed」（`evolver/README.md:213-215`）。**GameRSI 的 INV-4 更严（无网络/无文件写/无 subprocess/无 env 内部访问）**，必须真的做到进程级隔离，不能只做路径白名单。这正是 GameRSI 相对 Raven 的**核心增量价值**。

5. **小样本 + 多重比较。** 论文明示「Repeated adaptive screening... does not provide a family-wise significance guarantee」（`raven.txt:1958-1960`）。GameRSI 每代 165 局、heldout n=40、最多 20 代，多重比较累积下界很松。**对策**：retention 作为主验收（`sealed/runner.py:107-112`）+ test 集锁定每里程碑只解锁一次（GameRSI 已定）+ unseal 只按 train argmax（`sealed/runner.py:183-187`）。

6. **Harness 增益不稳定 / 模型依赖。** HarnessDev 的实证：增益可能不稳定、只部分迁移到 held-out、强依赖执行模型（`raven.txt:1790-1792`）。**对策**：GameRSI 冻结同一个本地 8B 模型（模型不变是特性不是限制），并把 retention 而非 train 提升作为成败判据。

7. **「宽通筛选」可能放行噪声候选。** Raven 的 ⑤a 只砍明显更差的，因此每轮可能把多个噪声候选送进昂贵的全量确认。**对策**：GameRSI 算力有限（每代 165 局 ≈ 14h），需把 `N_keep`（每轮幸存上限）设小，Raven 默认 `N_keep ≤ J`、`J` 为提议上限（`raven.txt:1721-1722`），GameRSI 建议 N_keep=1~2。

8. **模式坍缩的隐性来源。** Raven 的 `patience=10` 会让循环在「连续 10 轮无改进」后停——如果失败模式坍缩（agent 只用一种策略），可能表现为「无改进」而提前终止，看起来像收敛。**对策**：GameRSI 的坍缩监控（MOD-08）应同时监测**行为多样性**（动作分布熵），而不只看分数是否停滞。

9. **失败状态被静默吞掉。** `evolver` 大量 `except Exception: record + continue`（如 `loop.py:470-478, 493-500, 511-521, 540-550, 576-577`）。这对无人值守很必要，但会让「为什么这轮没候选」只存在台账里。**对策**：M2 冒烟阶段必须人工逐轮读 `findings.md` 与 `nodes/*.json`，不要只看 stop_reason。

---

## 7. 分阶段行动清单（M0 → M3）

### M0 环境（2026-10-08 → 2026-11-20）—— 现在就埋下测量不可作弊的地基

| 行动 | 来源 | 落点 |
|---|---|---|
| **写 seed 三分泄漏断言**（train/holdout/test 集合交集为空，启动时 raise） | `evolver/orchestrator/sealed/runner.py:38-49` | MOD-09 / INV-5。现在 seed 协议已定（train 1000–1999 / heldout 2000–2099 / test 9000–9099），断言是零成本的保护 |
| **定不可变内核清单**：评估打分、沙箱门面、prompt 注册表、seed 注册表对 agent 不可编辑 | `evolver/applier/path_guard.py:24-94`（L1–L5 分层） | MOD-02 / INV-1/INV-4。GameRSI 比 Raven 更严：进程级隔离 + 无网络/无文件写/无 subprocess |
| **白名单 + 越界 revert** 的实现范式（而不是 diff 审查） | `benchmarks/appworld/evolve/sandbox.py:16-27` | MOD-02 受限执行 |
| **审计口径**：黑名单扫描 + canary 探针（GameRSI 已定 B-25） | Raven 的 L4 审计层（`path_guard.py:85-86`） | MOD-02 审计 + 蜜罐 |
| **确定性优先**：所有随机只来自显式 seed | `evolver/orchestrator/DESIGN.md:70-73`（「arithmetic work must be done by deterministic code」） | MOD-01 / INV-6 |

### M1 基线（2026-11-20 → 2027-01-14）—— 把「比较基准」做硬

| 行动 | 来源 | 落点 |
|---|---|---|
| **冷启动冻结基线**：三基线（random/heuristic/fixed_prompt）的 held-out 分数一次跑定、全 run 冻结，配置漂移拒绝恢复 | `evolver/orchestrator/loop.py:208-225`；`evolver/launch/state.py:39-106`（`config_fingerprint` + 单向 unseal 戳） | MOD-08 基线入档 |
| **固定分母口径**：超时/截断计 lose 且不从分母剔除 | `evolver/orchestrator/sealed/runner.py:82-84`；`gates/pipeline.py:6-9` | MOD-08（GameRSI 已定截断计 lose，与 Raven 一致） |
| **区分「推理失败」与「游戏失败」**：JSON 解析失败重试有界，仍失败计 lose 但单独标记 | Raven 的 infra 重试阶梯 + infra_attempts 字段（`gates/pipeline.py:49-51`） | MOD-03 JSON 鲁棒性报告 |
| **报告配对统计而非裸通过率**：任务级差值的标准误；promote 判据与显著性标签分离 | `evolver/orchestrator/gates/paired.py:61-102` | MOD-08 指标。n=40 heldout 用配对 t(n−1)，比 Raven 的固定 1.96 更严谨 |

### M2 闭环（2027-01-14 → 2027-03-26）—— 把 RSI loop 编译成状态机

| 行动 | 来源 | 落点 |
|---|---|---|
| **七步漏斗状态机**：冷启动 → 诊断 → 设计 → 零成本预筛 → 应用 → 宽通筛选/全量确认 → 三闸门 → argmax 选父 | `evolver/orchestrator/loop.py:168-617`；表见本报告 §3.1 | MOD-06 + MOD-07 初版 |
| **控制反转**：循环推进/停止/不早退是代码；模型每步只做一次 schema 校验 + 有界重试的判断 | `evolver/orchestrator/DESIGN.md:84-101` | MOD-06。对本地 8B 模型尤其关键 |
| **三闸门**：Gate-f（固定分母）→ Gate-b（机制真触发才归因，用环境原生事件不用自报 beacon）→ Gate2（配对 2σ） | `evolver/orchestrator/gates/pipeline.py:54-106` | MOD-07 gate / MOD-08 |
| **宽通筛选 + N_keep=1~2** | `evolver/orchestrator/gates/strategies.py:13-16`；`raven.txt:1721-1722` | MOD-07。算力约束下宁缺毋滥 |
| **哨兵分层回归控制**：稳定通关 seed / 边缘 seed 各半，按候选 id 哈希轮换 | `evolver/orchestrator/loop.py:257-296`（含 58% 回归穿透事故） | MOD-08 |
| **flip table**：rescued/regressed/still-failing 回写给下一轮设计 | `evolver/orchestrator/loop.py:553-564` | MOD-06 反思 → MOD-05 |
| **failure map 跨轮累积 + 不重复诊断** | `evolver/orchestrator/loop.py:432-447` | MOD-05 反思库 |
| **env-gated default-off 候选**：关闭时与基线逐字节相同 | `evolver/orchestrator/nodes/design.py:94-117` | MOD-07 |
| **journal 断点续跑 + config fingerprint** | `evolver/launch/runner.py:1-45` | MOD-06 续跑 + MOD-09 |
| **每候选台账 JSON**（身份 + git SHA + 状态 + gate 统计 + WHY/WHERE） | `evolver/orchestrator/loop.py:637-675` | MOD-07 代际日志 / MOD-09 |
| **人在环门只设两处**：晋升新基线、test 解锁 | `evolver/cli.py:44-50`（finalize --yes 一次性） | MOD-07 |
| **M2 冒烟必须人工逐轮读 findings/台账**，不只看 stop_reason | 本报告 §6 风险 9 | M2 验收 |

### M3 测量（2027-03-26 → 2027-04-22）—— 回答「它真的在变强吗」

| 行动 | 来源 | 落点 |
|---|---|---|
| **unseal 只按 train argmax，绝不 max-over-test**（防 winner's curse） | `evolver/orchestrator/sealed/runner.py:163-187` | MOD-08 / INV-5。这是整个项目可信度的关键 |
| **retention = test_lift / train_lift 作为主验收** | `evolver/orchestrator/sealed/runner.py:107-112` | MOD-08 / 项目计划 §6 验收 |
| **终止三条件**（patience / max_rounds / errors），耐心信号只对冻结基线、从不对 test | `evolver/orchestrator/termination.py:29-80` | MOD-06 |
| **GSME 精英库 + 重组**（若代际足够）：(编辑面 × 失败病理) 每格一个 elite；WHERE 机械绑定；同文件冲突的配对跳过 | `evolver/orchestrator/archive.py:218-323, 134-160` | MOD-07。GameRSI 编辑面只有三类，格次维度降为 3×N |
| **坍缩监控同时看行为多样性（动作分布熵），不只看分数停滞** | 本报告 §6 风险 8 | MOD-08 坍缩检测 |
| **消融**：H2 篮子（2040–2079）做「关掉某机制看 retention 变化」 | Raven 的 ablation 约定；H1/H2/H3 分篮见 GameRSI spec B-07（`docs/模块拆分Spec-GameRSI.md:524`） | MOD-08 消融 |
| **验收叙述**：用「多轮迭代后 held-out 全部指标通过」的方式写结论（借鉴 Curator 展示案例的叙述形状，但证据来自环境） | `experimental/simulation/cases/s0925c/` | M3 验收报告 |

---

## 附：核心结论（5 条）

1. **Raven 的自进化结果不是 Raven 仓库产的。** 论文 §7.2 的七项 held-out 提升（+5.1~+15.4 pp）转引自 HarnessBank v2（`raven.txt:4903`），本仓库未复现；且 `evolver/` 树在自己的 README 里标记「retirement of this tree is planned」（`evolver/README.md:3-5`）。**Raven 不是「已验证的 RSI 平台」，而是一份「RSI 方法论的参考实现」。** 借鉴设计模式，不要假设可复现性，不要 fork `evolver/`。

2. **仓库里有两套自进化机制，论文只写了一套。** 论文 §4 对应 `evolver/`（离线、benchmark 驱动、统计门控），而 README 主推的「Runtime Self-Evolution」是 `experimental/curator`（运行时、人在环），**论文全文 0 次提及 Curator**，且它不随安装包发布。GameRSI 需要的是前者。

3. **最值得抄的三件东西**：(a) **sealed test 三重机制化**（启动泄漏断言 + score 返回 None + unseal 只按 train argmax 防 winner's curse，`evolver/orchestrator/sealed/runner.py`）；(b) **三闸门**（固定分母 / 机制真触发才归因 / 配对 2σ，且 promote 与显著性标签分离，`evolver/orchestrator/gates/`）；(c) **控制反转**——循环推进与停止是确定性代码不是 prompt（`evolver/orchestrator/DESIGN.md:84-101`），这对指令遵循较弱的本地 8B 模型比对 Raven 更关键。

4. **Raven 的自评估确实违反 INV-7，而且论文自己承认了。** DAG 完成判决是 LLM 自评且 fails open（`raven.txt:1037-1039`；`raven/agent/subagent/dag_verdict.py:12-16`）；Raven-Research 的准确率由 LLM judge 打分，重评噪声约 1.3pp（`raven.txt:5010-5011`）。**但 Raven 的自进化循环恰恰不用 LLM 做主评分**——它用程序化 scorer 并把打分代码锁进不可变内核（`evolver/applier/path_guard.py:49-57`）。这正是 GameRSI 应该继承并做得更彻底的原则：主指标 = 环境原生胜负，打分代码不可被候选编辑，LLM 只用于诊断与候选生成且其产出视为假设（`raven.txt:1741`）。

5. **GameRSI 相对 Raven 的核心增量在「测量不可作弊」这一层，且这一层 Raven 自己承认没做到位。** Raven 的威胁模型自陈「guards stop an LLM designer from taking shortcuts, not a determined adversary... in-memory tampering or oracle probing is possible in principle」（`evolver/README.md:206-210`），且设计沙箱「not filesystem/network jailed」（`evolver/README.md:213-215`）。GameRSI 的 INV-1/INV-2/INV-4（进程级隔离 + 白名单观测投影 + 无网络/无文件写/无 subprocess）+ INV-7（环境原生指标）组合，严格于 Raven。**这应该成为 GameRSI 项目的核心卖点：不是「能自进化的 agent」，而是「自进化的增益可以用不可作弊的方式被测量」。**
