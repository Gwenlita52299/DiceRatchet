# Reef 深度分析：参考项目对 DiceRatchet 的可移植性评估

> 报告日期：2026-10-01 · 分析对象：`ref/reef`（Human-Agent-Society/reef）+ 4 篇 Reef 方法论文
> 服务对象：DiceRatchet（本地 8B 模型 + Mac + 骰子 roguelike 沙箱的 RSI 自改进实验）
> 引用约定：代码引用一律 `path:line`，相对 `ref/reef/` 仓库根；论文引用附 arXiv 号与原文表格编号

---

## 1. 项目速览

### 1.1 一句话定位

Reef 自称「首个面向持续自改进 agent 的开源基础设施」（`README.md:21`）。它把四件事串成一条闭环：**agent 推理 → 反馈 → 学习 → 版本化交付**。与 DiceRatchet 的「玩 → 验证 → 反思 → 改进」是同构的，只是 Reef 面向 GPU 训练集群与服务化部署，DiceRatchet 面向单台 Mac。

### 1.2 两条学习路径

Reef 明确区分两条互斥的学习表面（`README.md:113-114`，`docs/user-guide/evolve-your-harness.rst:8-14`）：

| 路径 | 演化对象 | 需要什么 | DiceRatchet 对应物 |
|---|---|---|---|
| **(a) 模型权重训练** | `weights`（LoRA/全参） | 可训练模型 + GPU 栈（Slime/SGLang/Megatron） | **不适用**（Mac 上 8B 模型不可训） |
| **(b) harness 自改进** | harness tree（config/rules/prompts/skills/extension code） | 一个模型 endpoint + 代表性任务 + 评分器；**无需本地 GPU** | **完全对应** MOD-07（演化 harness）+ MOD-05（记忆） |

`README.md:41-45` 的选型表把第三条「科学发现」路径（test-time training）单列，对应 TTTD。

关键事实：**harness 演化路径明确标注「Reef needs no GPU for this」**（`docs/user-guide/evolve-your-harness.rst:13`）。这是本报告最重要的结论之一——Reef 中与 DiceRatchet 最相关的那一半，其本身就不依赖 GPU。

### 1.3 核心抽象三件套

- **Recipe**：一个方法的绑定单元，把 processor（数据侧）+ objective（训练目标）+ runtime + surface 捆成一个 dataclass（`reef/recipe/base.py:327` 起）。
- **Release chain**：一个 scenario 的发布历史，每次接受 = 一个 commit + 一个带 parent 的 release。
- **Receipt**：一次推理记录的 id（`x-reef-agent-record-id`），把结果绑到产生它的版本（`docs/reference/glossary.rst:29-35`）。

---

## 2. 代码级架构拆解

### 2.1 顶层目录结构

```
ref/reef/
├── reef/                  # 核心包（发到 PyPI 的 reef-infra）
│   ├── core/              # 共享值类型、wire 契约、错误（无训练栈依赖）
│   ├── dispatcher.py      # 应用内核：协调、scenario 状态、commit 排序、恢复（81KB）
│   ├── scenario/          # scenario 生命周期、committer（rollback 在此）
│   ├── service/           # HTTP、认证、流式、部署
│   ├── recipe/            # Recipe 契约、cordis（harness 演化装配）、reefine
│   ├── train/             # processor、训练后端（slime/tinker/cordis）、algos
│   ├── runtime/           # 后端中立 runtime 契约、调度、publication、recovery
│   ├── inference/         # sglang / vllm 具体集成
│   ├── surface/           # 发布物的投递（weights / harnesses / skills / files）
│   ├── artifact/          # 版本化 artifact 与仓库（git + git-lfs）
│   ├── storage/           # 记录存储（SQLite/Postgres）、retention
│   ├── harness/           # harness 适配器、渲染、episode runner、tree
│   ├── record2dataset/    # 生成服务（Harbor 任务设计）
│   └── observability/     # 横切可观测
├── recipes/               # 方法插件（不进 wheel，core 不 import）
│   ├── basic/ gepa/ meta_harness/ openclawrl/ sao/ sdft/ sdpo/ skillclaw/ tttd/
├── docs/rfcs/             # 演化边界、方法集成、continual-opd 等 RFC
├── tests/ tutorials/ docker/ third_party/
```

架构约束（`docs/contributing/codebase-structure.rst`、`AGENTS.md` 的边界表）：
- **共享机制在 `reef/`，方法策略在 `recipes/`**；`reef/`绝不 import cookbook 方法，`recipes/` 不随 wheel 发货。
- Recipe 用 dotted 引用 `recipes.gepa.recipe:GEPARecipe` 显式选择（`reef/recipe/registry.py:31` 的 `recipe_class_for`），无全局注册表——「导入 Reef 永远不会导入方法代码」。
- `reef-client` 保持零依赖的纯协议客户端；harness 只消费 `reef_client`，不依赖 Reef 的 service/training 栈。

这条「内核/方法」分层对 DiceRatchet 直接可移植：**DiceRatchet 应把演化机制（版本化、回滚、 receipts、沙箱执行）放进不可变内核，把「怎么提候选、怎么判分」作为可替换的方法插件**。

### 2.2 核心抽象：WeightTrainingSpec / loss_family / StepSignal / StepScheduling

这是 Reef 权重训练路径的契约四件套，全部是后端中立的纯数据类：

**(1) `TrainingObjective`（`reef/train/algos/objective.py:13-49`）**
```python
class TrainingObjective(ABC):
    name: str = ""
    loss_family: str = ""
    supports_multiple_epochs: bool = False
    def validate_scheduling(self, scheduling: StepScheduling) -> None: ...
    @abstractmethod
    def prepare(self, batch: TrainingBatch, state: Mapping[str, Any]) -> StepSignal: ...
```
要点：`prepare()` 拿到**完整保留的 batch**（因此组内统计量不依赖 optimizer/DP 划分），返回 `StepSignal`；**模型相关项留在后端 loss 实现里**；本模块不依赖 torch/Slime/Tinker——「recipe 的 loss family 必须在 worker 启动前就可被检查」。

**(2) `StepSignal`（`reef/train/algos/signals.py:14-21`）**
```python
@dataclass(frozen=True, slots=True)
class StepSignal:
    action: Literal["train", "skip"]
    next_algorithm_state: Mapping[str, Any]
    metrics: Mapping[str, Any] = field(default_factory=dict)
    advantages: tuple[float, ...] | None = None
```
这是「算法输出」与「后端 wire payload」之间的防火墙：`action="skip"` 允许方法跳过一个 step 而不产生更新；`next_algorithm_state` 是**提议**，trainer 拥有 commit 权（含重试与恢复）。

**(3) `StepScheduling`（`reef/core/batches.py:111-169`）**
recipe 的选择（不是 objective 的）：`unit`（`comparison_set`|`sample`，rollout 是不可跨 step 切分的单元）、`batch_size`（`"configured"`|`"actual"`|int）、`epochs`、`shuffle`、`remainder`（`partial`|`drop`|`error`）。`epochs>1` 时旧策略 logprob 只在第一遍前算一次，故后续 pass 是 PPO 式 off-policy——只有声明 `supports_multiple_epochs`（即 clipped）的 objective 才允许。

**(4) `WeightTrainingSpec`（`reef/recipe/base.py:327-348`）**
```python
@dataclass(frozen=True)
class WeightTrainingSpec:
    objective: str
    processor: type[DataProcessor] | None = None
    scheduling: StepScheduling = field(default_factory=StepScheduling)
    @property
    def loss_family(self) -> str:
        return resolve_objective(self.objective).loss_family
```
`loss_family` 是**由 objective 拥有**的 backend loss 选择器（`sao`/`tttd`/`openclawrl`/`sdft`，见 `docs/developer-guide/loss-families.rst`）。driver 启动时会校验 `loss_family` 与 `--loss-type` flag 一致，拒绝不匹配。

**对 DiceRatchet 的可移植抽象**：这四个类里，`StepSignal` 的「提议/commit 分离」与 `action="skip"` 设计，以及「objective 只准备数据、不 commit 状态」的契约，**完全可以搬到 DiceRatchet 的 MOD-07**：候选生成只产 `Candidate + 提议的下一代状态`，commit/回滚权归版本化层。而 `loss_family`/`StepScheduling` 的 GPU 切分语义对 DiceRatchet 无意义。

### 2.3 候选选择抽象（gate）

`reef/core/evaluation.py` 定义了与 artifact 无关的候选选择契约：
- `UpdateCandidate`（`:12`）：`candidate_id` 是稳定幂等键。
- `EvaluationResult`（`:27`）：带 `evaluator` + `evaluator_version`（版本化判分器，可审计）。
- `SelectionDecision`（`:51`）：`outcome ∈ {select, reject}` + `policy` + `policy_version` + `reason`——**每个拒绝都带可读理由**。
- `CandidateEvaluator`（`:97`，只测量不决策）/ `CandidateSelector`（`:86`，只决策不测量）/ `CandidateEvaluationPlugin`（`:104`，两者组合）。

**测量与决策被刻意拆成两个能力**。`docs/reference/glossary.rst:104-113` 明确：「candidate selection 就是 docs 也叫 gate 的那个东西；每个 recipe 都带这个 plugin，它与 checkpoint 频率与 retention 无关」。

这条拆分对 DiceRatchet 的 INV-7 有直接对应：**MOD-08 出数字（evaluator），MOD-07 做判定（selector）**，DiceRatchet 的 Spec 已经这么切了（MOD-08 PRD §2.2「不负责：修改 harness……接受/拒绝/回滚由 MOD-07 gate.py 决定」），与 Reef 不谋而合。Reef 的 `SelectionDecision.reason` 字段对应 DiceRatchet 要求的「判定表可执行、逐项可解释」。

### 2.4 Release chain：版本化交付的实现

**三重身份严格分离**（`docs/reference/glossary.rst:69-88`，`docs/advanced_topics/state-model.rst`）：

| 身份 | 含义 | 生命周期 |
|---|---|---|
| `release_id` | 一次**发布决策**（chain 上的一环） | 追加，永不回退 |
| `content_id` | 被**选中的内容**（权重字节/harness 树） | 相等即同内容，与发布时机无关 |
| `runtime_load_id` | 一次具体的**引擎权重加载**（`<incarnation>:<sequence>`） | serving fence；重启即失效，但不影响 release chain |

`ArtifactRef`（`reef/core/artifact_ref.py:10-24`）是不可变 dataclass：`content_id / release_id / parent_release_id`；`LiveWeightArtifactRef`（`:27-36`）加 `runtime_load_id`，表示「字节只在引擎内存里」的 live 发布。

**核心设计：回滚不重写历史**（`docs/reference/glossary.rst:78-80`）：
> Rollback therefore creates a new release that can point to the same `content_id` as an older release; history is appended, never rewound.

实现见 `reef/scenario/committer.py:356-449` 的 `rollback()`：「Publish a durable copy of an older version as a new fenced commit」。步骤：校验目标有 durable checkpoint bytes → 拒绝「训练结果 pending commit」时的回滚（`:373-374`）→ 解析旧 release → 暂存/stage → 重新跑 admission 检查（`admit_restored`，`:409`）→ 必要时 `pause_admission()` + `restore_checkpoint()`（`:425-428`）→ commit 到 store（带 `operation="rollback"`、`rollback_target_release_id`，`:445-449`）。

**Stale-publish fencing**（防止过期发布覆盖）：
- `Repository.advance_current()` 是**compare-and-swap**：调用者传入准备时观察到的 head，不匹配即 `ArtifactConflict`（`reef/artifact/repository.py:235-250`，`:244-248` 的错误信息明确写出「serving head advanced from X to Y; refusing to advance to Z」）。
- `install_checkpoint()`（`:252-256`）同样双 head CAS。
- Durable release 是 **Git-backed，一个 scenario 一个 ref**，head 只能靠 CAS 移动，push 带 lease（`docs/advanced_topics/state-model.rst`：「Heads move only by compare-and-swap … so a stale publication conflicts instead of overwriting」）。
- 权重传输层有 `WeightUpdateLock`（`reef/runtime/publication.py:572-618`）：`acquire/release/poison`，poison 后必须重连传输；`complete_phase/phase_status` 记录每个阶段完成情况。这是「权重可能只应用了一半」时的 fence（`:1108`：「Weights may be half-applied: keep inference fenced until recovery」）。
- **staleness 策略**（`reef/runtime/scheduler.py:240-258`）：`max_staleness` 窗口内允许「生成它的版本」与「当前服务版本」有 lag；跨 incarnation 一律 drop（`cross_incarnation`，`:243`）；`future_producing_runtime_load_id` / `policy_lag_exceeded` 等明确 drop reason。`WeightTrainingRecipe.max_staleness`（`reef/recipe/base.py:385`）默认 0 = 精确版本训练，且必须与 runtime 的值一致（`:404-409`）。

**多组件 release**（`reef/core/components.py:58-105`）：一个 release 可绑定多个命名组件（`weights`/`harness`/`skills`）；单组件走 flat 布局，多组件每组件一个目录 + metadata 里的 manifest。组合 `content_id` 是各组件 content_id 排序后的 sha256（`:95-105`）——「两个绑定相同组合的 release 共享一个 content_id」。**这意味着 weights 与 harness 可以作为同一 release 的两个组件一起回滚**（`docs/advanced_topics/state-model.rst`：「a rollback restores every component together」）。

**对 DiceRatchet 的直接指导**：DiceRatchet 的 `harness/versions/v{N}/` + `memory_v{N}.jsonl` 目前是两条独立的版本线。Reef 的组件化设计给出一个更干净的方案：**把 harness 与 memory 作为同一个「代际 release」的两个组件，content_id 各自独立但 release_id 统一，回滚按 release 整体回**。这恰好解决了 DiceRatchet MOD-05 FR-05-9 里「harness 回滚时 memory 也要回滚到同代」的需求（S4 场景），把「两个回滚命令要人肉对齐」变成结构上的原子操作。

### 2.5 Receipts：结果与版本的绑定

- 推理响应头 `x-reef-agent-record-id`（`reef/service/routes/inference.py:107`），流式时在终端 SSE metadata 里给 `reef.agent_record_id`（`:134`）。
- Report 在 `references` 里引用 receipts（`docs/reference/glossary.rst:33-35`），Reef 对每个 report **最多消费一次**（含迟到与重试）。
- 记录存储：`AgentRecord` 带 id、scenario、请求类型、payload、以及**服务它的 release**（`docs/reference/glossary.rst:23-27`）。

这正是 INV-7 在基础设施层面的实现方式：**任何结果都能追溯到产生它的版本**。DiceRatchet 的 `report.json` 应该带一个等价的 `served_harness_version` 字段（MOD-05 的 `harness_version` 血缘已在做这件事，方向一致）。

### 2.6 Harness 演化引擎（CordisBackend）

这是与 DiceRatchet MOD-07 最直接对应的部分，位于 `reef/train/cordis_backend/`。`__init__.py:1-27` 描述了完整的一步：

> `propose` 读当前 composition 与 batched traces → 返回 `Mutation`/序列/`StepProposal` → backend 在一个 snapshot 下应用 → candidate 与 current 各自渲染、每个 task 跑一个 headless episode → 方法的 `EpisodeScorer` 打分 → recipe 选的 `CandidateEvaluationPlugin` 配对测量与选择策略。**一个序列是一个原子组合提议，只产生一个 selection decision**。

**(a) Mutation 值与准入规则**（`reef/harness/tree/mutations.py`）
```python
@dataclass(frozen=True)
class Mutation:                      # :19-43
    op: str                          # create | update | remove
    id: str                          # 根级 id，":"-qualified 被拒
    options: Mapping[str, Any] | None
```
`_apply_mutation`（`:104-143`）的硬约束：保留 id（`RESERVED_ENTRY_IDS`，reef 自己的入口，提议改不了，`:106-111`）；`create` 拒绝已存在 id；`update` **不能改 kind**（改 kind 必须 remove+create，`:134-139`）；所有 op 拒绝 group entry 与嵌套 id（flat tree 规则）。

`admit_mutations`（`:146-174`）是**每个提议都要过的唯一准入**：在全新 loader 上应用 mutation → 检查 fiber 状态（FAILED 带错误拒绝）→ 检查 adapter 是否渲染该 kind → **真正跑一次 render**（render 失败就拒绝，`:168`）→ native host 启动期拒绝项（`:177-193`：「一个 serve 进程会回滚的树不能赢 gate」）。

**这条「渲染失败 = 提议失败」的准入非常关键**：候选不是「语法合法」就算合法，而是「真的能跑起来」才算。DiceRatchet MOD-07 的 FR-07-4 目前是 AST 扫描 + 改动面计数 + 禁用符号，建议**加一层「在沙箱里真渲染/真加载一次」的准入**（对应 MOD-02 的 `guard.load_harness_tools`），把「能编译但跑不起来」的候选挡在 heldout 评估之前。

**(b) 双侧对照执行**
Reef 对每个 task 跑 candidate 与 current 两份（`docs/user-guide/evolve-your-harness.rst`「Cost」节）：`2 × len(tasks) × episode_repeats` 个 headless episode，**interleaved 使两侧看到相同上游条件**。每个 episode 在一个 throwaway root 里（只有渲染出的树，没有仓库、没有本机文件）。

**(c) 评估套件的自动生长与反作弊**（同文档「Promotion」节）
- `evolution.promote_failures: true` 把失败轨迹的 prompt 永久加入评估套件——但必须先过**凭据绊线**（key 形字面量：永不 promote、永不持久化、永不重跑）与**指令覆盖筛查**（`ignore the previous instructions`、伪造 system message、chat-template 控制 token）；单个 client 最多 `max_promoted_per_client` 条，防止单一发送者塞满套件。
- `evolution.recheck_every: N`：每 N 步用**已生长的**套件重新评估上次发布的树；若旧树现在赢了，**发布旧树 = 回滚**。「只有最近一次发布的树保留为回滚目标，且回滚会消耗它，所以 recheck 只修正一次坏发布而不是回退整个历史」。

**(d) 三种 sandbox 执行器**（`docs/user-guide/evolve-your-harness.rst`「Running episodes」）
- `local`：纯子进程，开发与测试用，**不强制任何隔离**。
- `sandbox`：bubblewrap jail（fresh non-root namespace、只读基础 FS、显式凭据、资源限制、默认无网络）；native adapter 下**每个 tool call 再套一层 nested jail**，按 tool 声明的 capability withheld network/workspace 写/shell。
- `e2b`：**macOS 或无 Linux namespace 的主机用 disposable cloud sandbox**（`evolution.executor: e2b`）。

**这条对 DiceRatchet 极其重要**：Reef 明确承认 macOS 上没有 Linux namespace，因此评估 episode 走 E2B 云沙箱。DiceRatchet 跑在 Mac 上，MOD-02 的沙箱方案必须自己解决「macOS 无 namespace」问题（用子进程 + seatbelt/sandbox-exec，或接受比 bubblewrap 弱的隔离边界），不能照搬。

**(e) 沙箱与提案的分离**：`evolution.proposer_agent.sandbox: e2b` 独立于 `evolution.executor`——**提议（生成候选）与评估（判分）跑在不同的沙箱里**。对应 DiceRatchet 的「生成者与裁判者上下文不重叠」防自证设计。

---

## 3. 四个方法论文要点提炼

### 3.1 SAO — Single-Rollout Asynchronous Optimization（arXiv:2607.07508）

**解决什么**：异步 RL 里 GRPO 的 group-wise sampling 不适配长程 agentic 任务（要等最慢样本），而单 rollout 又有高方差。

5 条要点：
1. **直接双侧重要性采样（DIS）**（§3.1）：直接用 `π_rollout` 当行为代理、`π_θ` 做重要性采样，**丢弃不准的 `π_old`**；ratio `r_t = exp(log π_θ − log π_rollout)` 直接用 rollout 日志算，省掉维护历史 checkpoint 集合。
2. **双侧 calibration 而非单侧 clip**（式 3）：落在 `[1−ε_l, 1+ε_h]` 区间外的 token **直接从梯度里 mask 掉**（不是像 PPO 那样只 clip 被选中的 off-policy token）。作者称之为「更激进的 clip」，换取稳定性。
3. **单 rollout 采样**（§3.2）：一个样本生成完立即进训练，不等组。代价是梯度方差高（类 REINFORCE），用三个 value-model 设计补：**policy 每更新 1 次、value 更新 K=2 次**（faster value）；**冻结 value 的 attention 只训 MoE**（发现不稳定主要来自 Full Attention 层，MoE 层稳定）；**Skip-Observation GAE**——显式改 Bellman target 绕过环境反馈 token，把动作 i 的价值直接连到动作 i+1（`Â(a_{i,N}) = δ + γλ Â(a_{i+1,0})`），过滤掉模型不生成的观测 token 的随机性。
4. **结果**（表 1，Qwen3-30B-A3B）：AIME2025 97.3 / BeyondAIME 74.8 / HMMT-Nov-2025 88.3 / IMOAnswerBench 74.0；对比 GRPO(w/ python) 84.2/54.8/76.0/55.8，Vanilla GRPO 训练约 90 步崩塌。SWE-Bench Verified：23.0 → +GRPO(DIS) 27.0 → +SAO 29.8（表 2）。消融（表 4）：去掉 faster value 掉到 95.0/69.8，去掉 frozen attention 掉到 90.6/74.5，running-mean baseline 只有 79.8/55.3。
5. **在线学习模拟**（§4.5）：设计非平稳写作任务，奖励偏好依次在 cute / chuunibyou / classical 间切换；SAO 每次偏好切换后快速重对齐，running-mean 基线因滑窗惯性有明显适应滞后。**已用于训练开源 GLM-5.2（750B-A40B）**。

**对 DiceRatchet 的意义**：**权重训练路径，Mac 上不可行**。但有两个可移植的思想：(i) 「冻结 attention 只训 MoE」的**诊断方法**（先定位不稳定来自哪一层再决定训什么）值得 DiceRatchet 在做 prompt/记忆消融时类比；(ii) Skip-Observation GAE 的洞察——**不要跨环境观测边界算信用分配**——对 DiceRatchet 的反思信用分配同样适用：骰子游戏的「环境事件」（敌方行动、骰子结果）不是 agent 的动作，反思不应把它们的随机性归因给策略。

### 3.2 TTT-Discover — Learning to Discover at Test Time（arXiv:2601.16175）

**解决什么**：让 LLM 在**单个测试问题上**做 test-time training，目标是产出「一个伟大解」而非「平均很多好解」。

5 条要点：
1. **discovery ≠ 标准 RL**（§3.1）：标准 RL 优化期望奖励、产物是策略、有部署阶段；discovery 优化**最大值**、产物是**一个状态**、无部署阶段——「一个策略可以有很低的期望奖励，只要它曾达到一次新 SOTA」。因此朴素 PPO/GRPO 有三处错位：objective（平均 vs 最大）、短有效 horizon（无复用就到不了复杂解）、探索（会坍缩到安全高回报动作）。
2. **entropic objective**（§3.2，式 `J_β`）：`∇J_β = E_s,a[ w_β(s)(a) ∇log π_θ(a|s) ]`，权重 `w_β(s)(a) = e^{β(s)R(s,a)} / E_π[e^{β(s)R}]`，并加 KL 惩罚塑形优势 `A = w_β − 1 − λ log(π_θ/π_θ0)`。β→∞ 时趋于 max。**β(s) 按初始状态自适应**（约束诱导策略的 KL），因为「找到一个跨任务恒定好用的常数 β 很难」。
3. **PUCT 复用**（§3.2）：用 PUCT 启发式选初始状态，`Q(s) + c·P(s)·√(T/(1+n(s)))`。关键差异：**Q(s) 用子节点的最大奖励而非均值**（「我们关心从某状态出发的最好结果，不是平均」）；P(s) 正比于状态在 buffer 里的奖励排名。
4. **实现与成本**（§3.3）：gpt-oss-120b + Tinker，LoRA rank 32，50 步，每步 512 rollout（8 组 × 64，同组共享同一初始状态），context 限 32,768 token，**每问题成本约 $500**。
5. **结果**（摘要表 + 表 8 消融）：Erdős 最小重叠问题 0.380876（人类 0.380927，前 AI 0.380924）；TriMul kernel H100 1161µs（人类最佳 1371µs，达 2× 于前 AI）；AtCoder 启发式竞赛 567062（人类 566997）；单细胞去噪 0.71（人类 0.64）。消融（表 8，TriMul H100 最佳运行时间）：完整 TTT-Discover 1203.10µs；常量 β 1483.83；期望奖励（无 entropic）1985.67；无 TTT 2060.70；PUCT 换 ε-greedy 1328.89；**无复用 5274.03**；朴素 test-time RL 5328.73；Best-of-N(25600) 5352.36。

**对 DiceRatchet 的意义**：**「复用」带来的增益（5274 → 1203，约 4.4×）是全文最大的单一消融贡献**，且这条不需要 GPU。DiceRatchet 的「策略记忆」在功能上就是 TTTD 的 reuse buffer：把上一局的好的中间状态/策略作为下一局的起点，而不是每次从零开始。TTTD 的两个具体设计可直接搬用：**(a) Q(s) 用最大而非均值**——记忆检索排序若用「平均胜率」会低估那些「偶尔打出神仙局」的策略；**(b) 探索项 `c·P(s)·√(T/(1+n))`**——防止过度复用少数高分记忆。但注意：TTTD 的 **权重训练部分（entropic objective + LoRA）在 Mac 上不可行**，只有 **PUCT archive + 复用启发式**这半边可移植——这与演化边界 RFC 的裁决完全一致（见 §4）。

### 3.3 OpenClaw-RL — Train Any Agent Simply by Talking（arXiv:2603.10165）

**解决什么**：把 agent 每次交互产生的 **next-state 信号**（用户回复/工具输出/终端或 GUI 状态变化）回收为在线学习源。

5 条要点：
1. **server–client 架构 + 四组件完全解耦**（§2.1-2.2）：RL server 把 policy 托管在无状态 completion API 后；policy serving、environment hosting、reward judging、policy training 是**四个完全独立的异步组件，无阻塞依赖**。「模型服务下一个用户请求的同时，PRM 判决上一个回复、trainer 施加梯度更新」。权重更新只在**定义良好的边界**推送到 serving engine，live 用户永远看到一致策略、永远不等训练。
2. **两种互补信号**（§3.1，表 2）：**evaluative**（PRM 投票出标量 `r_t ∈ {+1,−1,0}`，每个被评分 turn 都有，频率高但信息量 = 1 个标量）与 **directive**（把 next state 蒸馏成 `[HINT_START]...[HINT_END]` 的 hint，追加到 prompt 得 teacher 分布 `π_T(·|s_t⊕h)`，token 级指导但**稀疏**——只在 next state 真的携带可抽取修正时才触发）。**hybrid objective**：`L_hybrid = w_RL·L_GRPO + w_OPD·L_OPD`，默认权重各 1，共享同一轨迹与同一策略更新。
3. **overlap-guided hint selection**（§3.2）：on-policy distillation 的核心失败模式是 teacher-student 分布不匹配导致 importance ratio 爆炸。**不按 hint 长度或 teacher 置信度等表面启发式选 hint，而是按诱导 teacher 分布的几何**：`O[h,i] = |S_i^q ∩ S_{i,h}^p|`（学生 top-k 与 teacher top-k 的交集），sequence-level 或 token-level 取最大。因为选中的 teacher 已在学生的高概率区重叠，蒸馏给出有信息量的梯度同时把 off-policy ratio 压近 1。
4. **log-probability-difference clip**：对 per-token advantage 加 `|log π_T − log π_old|` 的 clip（RLVR 设 `C=2`），进一步稳定。
5. **结果**（表 3，Qwen3-4B-Thinking，5 次试验均值，指标 = 达到优化效果所需的最少会话数，越少越好）：**joint 优化 hybrid RL 平均 10.3**，GRPO 14.1，OPD 29.7，Mem0 29.7，Cognee 14.9；separate 优化 hybrid 15.0。hint 选择消融（表 4，Qwen3-32B）：sequence-optimal 12.5 / token-optimal 12.4 / random 16.1 / GRPO 15.8 / OPD 29.9。通用 agent：128 并行环境（terminal）/64（GUI、SWE）/32（tool-call）；集成 outcome+process 奖励在 tool-call 达 0.25、GUI 0.33，而纯 outcome 只有 0.19、0.31。

**对 DiceRatchet 的意义**：
- **「用着用着就变好」的框架级对比**：论文明确指出 hybrid RL 比 **memory（Mem0）与 skill-evolution 方法**效率高得多（10.3 vs 29.7 sessions）。DiceRatchet 选的是 memory + harness 演化路线，**这正是 OpenClaw-RL 的手下败将那条路**。这不意味着 DiceRatchet 走错了（DiceRatchet 不能改权重），但意味着：**必须把「效率」作为一等指标来测**，否则无法证明「记忆路线在本地 8B 上的合理性」。这正好是 MOD-08 的职责。
- **可直接搬用的基础设施思想**：四组件解耦 + 「权重更新只在良好边界推送」；next-state 信号的 evaluative/directive 二分——**游戏沙箱的 `Observation` 天然就是 next state**。DiceRatchet 的反思若能区分「这步错了」（evaluative）与「应该先做 X」（directive，来自环境结构而非 LLM 自评），能显著提升反思信号质量，且**不涉及权重训练**。
- **不可行部分**：PRM 需要一个独立推理服务（论文用 Qwen3-8B/32B 当 PRM，多轮投票）；DiceRatchet 在 Mac 上跑 8B 主模型已紧张，再跑一个 PRM 会成本翻倍。折中：用**环境原生结果**当 evaluative（INV-7 已经这么规定）、只在必要时用主模型自己当 PRM。

### 3.4 GEPA — Reflective Prompt Evolution（arXiv:2507.19457，ICLR 2026 Oral）

**这是与 DiceRatchet 的 prompt 自改进最直接相关的方法。**

**解决什么**：GRPO 等权重 RL 需要数千 rollout 才能学新任务；GEPA 论证「语言的可解释性是比稀疏标量策略梯度丰富得多的学习介质」。

6 条要点：
1. **遗传式 prompt 演化，权重冻结**（§3）：输入是一个含若干 LLM prompt 的复合系统 Φ；GEPA **只演化 prompt 集合 Π_Φ，权重 Θ_Φ 固定**。候选池初始化为只含 base 系统；循环 = 选父 → 在 minibatch 上提出并评估变体 → 若优于父则加入池并在 `D_pareto` 上全量评估；预算耗尽后返回 `D_pareto` 上平均分最高的候选。
2. **反思式 prompt 变异（Reflective Prompt Mutation）**（§3，Algorithm 1 左）：把模块的输入/输出/推理轨迹 + 最终结果（成功或失败）配对，让 LLM 做**隐式信用分配**，把结果归因到具体模块，再做定向更新。GEPA 执行选中候选于一个 minibatch，抽取模块输入/输出/推理，调 feedback 函数 `µ_f` 拿到**数值分 + 文本反馈**（如编译器错误信息、失败的 rubric）；按 **round-robin** 选要改的模块；reflection LM 看到 `(当前 prompt, 轨迹, 分数, 反馈)` 后改写。**新版在同一 minibatch 上重跑，只有严格更优才进池**。
3. **评估轨迹（evaluation traces）作为第二诊断源**（§3）：不只是执行轨迹，「环境在算奖励前产生的文本」（编译/执行/profiling 的自然语言轨迹）也是宝贵信号。GEPA 把奖励 `µ` 扩展成 feedback 函数 `µ_f`，返回 `feedback_text`；有时还能做到**模块特异**（多跳系统每跳后给反馈）。**这条对 DiceRatchet 特别有价值**：破咒骰的沙箱审计日志、非法动作拒绝原因、fallback 触发原因，全都是「环境在算结果前产生的文本」，是比「胜负」丰富得多的 `µ_f` 输入，且完全环境原生、不违反 INV-7 的精神（它是诊断输入而非主指标）。
4. **Pareto 前沿候选选择（最核心机制）**（§3.1，Algorithm 2）：朴素「总选当前最佳」会陷局部最优（图 6a：找到一个新策略后反复试图精炼、失败、耗尽预算）。GEPA 用「illumination」策略：**对每个训练实例记录所有候选的最高分，形成该实例的 Pareto 前沿；至少在一个任务上取得最高分的候选被保留，严格被支配的剪枝；从剪枝后集合按「该候选领先多少个任务」加权随机采样父节点**。这使「整体平庸但在某一类问题上异常好的候选」仍有机会当父，保持探索又不过度膨胀。
5. **结果**（Qwen3 8B，表 1）：6 个 benchmark aggregate，Baseline 45.23 → GRPO 48.91（+3.68，24,000 rollout）→ MIPROv2 47.84（+2.61）→ **GEPA 54.85（+9.62，约 3,936 rollout）**。IFBench 上 GEPA 用 **678 rollout** 达 38.61%，超过 GRPO 用 24,000 rollout 的 35.88%（**最高 35× 更少 rollout**）。GEPA 匹配 GRPO 最佳验证分只需 243/402/330/1143/1179/306 rollout（**最高 78× 样本效率**）。GPT-4.1 Mini（表 2）：GEPA 65.22（+12.19）、GEPA+Merge 66.36（+13.33），均超 MIPROv2(+5.64)/TextGrad(+6.11)/Trace(+3.27)。**跨模型泛化**：用 Qwen3-8B 优化、在 GPT-4.1-Mini 上评估仍 +9.00 aggregate（HotpotQA 高达 +27.67），优于那些直接在 GPT-4.1-Mini 上优化的基线。
6. **候选选择策略的对照（表 3）**：SelectBestCandidate +6.05、BeamSearch(N=4) +5.11、**GEPA Pareto +12.44**；GEPA 比 BeamSearch 最高高 11.33%、比 SelectBestCandidate 高 8.17%。另：演化出的指令比 MIPROv2 的 few-shot **短最多 9.2×**（Observation 4）。

**Reef 的 GEPA 复现作为可直接读的参考实现**（`recipes/gepa/`）：
- `archive.py:275-289` `fronts()`：任务索引 → 持有该任务最高分的候选集合；严格更高分替换该任务前沿，相等分加入。
- `archive.py:420-442` `_remove_dominated()`：复现 GEPA 的 `remove_dominated_programs`，**按分数从低到高考虑**，互相覆盖时丢低分者。
- `archive.py:357-377` `select_parent()`：先 `_remove_dominated`，再按「出现在多少个前沿上」建频率表，`rng.choice(sampling)`——「这就是让 GEPA 从专家而非总是当前最佳开始探索的原因」。
- `method.py:197-243` 一次提议：`take_parent` → 取 minibatch（**若父就是当前服务中的 composition，minibatch 免费：引擎已批量记录了它的真实流量与分数**，`:283-289`）→ round-robin 选组件（`next_component`，游标被子节点继承，`:223-242`）→ reflection LM 改写 → 子在同 minibatch 上打分 → `sum(child_scores) <= sum(parent_scores)` 即 abandon（`:231-232`，**严格改进才接受**）。
- `method.py:366-413` `decide()`：全量验证通过后才 `record_validation`；**发布判据是「严格 mean improvement over the served composition」，使服务中的树始终等于 archive 的最佳候选**；但前沿**记录所有**，故「mean 上输了的候选仍能通过其专长继续当父」。
- `archive.py:197-202` `persist()`：原子写（tmp + `os.replace`）；Reef-bound archive 关掉 autosave，**只在 scenario commit 落盘后从 post-commit hook 写 JSON 镜像**——「失败的 activation 或 commit 不能推进搜索」（`archive.py:6-9`）。
- `reflection.py`：`PROMPT_TEMPLATE` 是 GEPA 0.1.2 的 `InstructionProposalSignature.default_prompt_template` **逐字复制（MIT，带版权声明）**；`PLACEHOLDERS = ("<curr_param>", "<side_info>")` 在发送前检查，避免模板缺槽静默丢数据。
- Reef 复现的验证结果（`recipes/gepa/README.md`）：AIME-2025 验证契约下，两 seed 平均 held-out 提升 **+16.00 pp**（本方法）vs **+12.67 pp**（上游 GEPA）；但「selected-score difference 在两 seed 间变号，故这些结果验证了方法但未确立优越性」——**这是一段极其重要的方法学自律声明**，DiceRatchet 应照此写自己的 held-out 结论。

---

## 4. 「演化边界」论证及其对 DiceRatchet MOD-05/MOD-07 的指导

### 4.1 论证本身（`docs/rfcs/evolution-scope.rst`）

**起点**：三个方法提出同一个设计问题——TTTD 的 PUCT archive 该不该移进训练后端？ACE 式 playbook 每 batch 无 gate 合并，算 Reef 的活还是 harness 的？GEPA 的候选前沿该在 Reef 里还是外面？

**核心论证：gating 不能作为放置规则**（`evolution-scope.rst:34-41`）：
> 「no update is guaranteed to be an improvement. A PPO step can regress. The `openclawrl` and `sao` algorithms publish every batch with no gate at all, and their safety net is not a gate but the release chain: every publish is a commit, receipts tie each outcome to the exact version that produced it, and rollback restores any earlier one.」

即：既然连权重更新都可以不加 gate 地持续发布，那「有没有 gate」就不足以决定一段状态该放哪。放置取决于**两个正交的轴**。

**轴 1：什么在变（决定代码放哪）**（`:48-61`）
- **Served artifacts → Reef**：凡是能回答「哪个版本产生了这个响应」的东西——模型权重、`SKILL.md`、harness tree 的 config/rules/prompts/skills/extension code。它们**长寿命、被后续每个请求共享**，对它们的修改正是需要版本身份、receipts、stale-publish fencing、回滚的东西。「更新它们是训练后端的活，每次更新都落到 release chain 上」。
- **Run state → harness**：由**执行固定规则**产生的状态——PUCT archive 的访问计数与剪枝、一次搜索的工作内存、一次对话的上下文。这些更新是**稠密、有序、由算法定义的簿记**，不是待裁决的假说。「对一次访问计数递增没有 accept/reject 决策可做，gate 它会破坏算法语义」。其作用域是一次 run 或一个问题实例。「harness 拥有它们；Reef 至多持久化快照（§5）」。
- **反方向的同一规则**（`:63-65`）：「*learned state is runtime output, never committed to the evolved artifact*。skill 或 policy 文本通过 Reef 演化；它在运行时积累的记忆不进入产物。」

**轴 2：如何被接受（每个后端的风险策略）**（`:94-109`，三类策略全部在 Reef 侧、全部落到同一条 release chain）

| 策略 | 例子 | 安全网 |
|---|---|---|
| 无 gate 持续发布 | `openclawrl`、`sao`（权重）；ACE 式 playbook 合并 | release chain、回滚、receipts |
| 与当前比较、赢多输少才选 | `CordisBackend` | 先决策，再 release chain |
| 种群/Pareto 保前沿 | GEPA 式 prompt 演化 | 先前沿，再 release chain |

「gate 是任何 served artifact 都可用的风险管理选择，但**没有一个是必需的**。权重可以无 gate 更新而 harness artifact 目前不能，这是**实现缺口而非原则**（§5）。」

**放置判据是数据流方向，不是代码有多像 agent**（`:148-158`）：
- 输入是请求或环境、输出是**下一个请求** → harness。包括任务编排、判分、TTTD 的 PUCT 选择这类搜索循环。**跑在 Reef 外**。
- 输入是**record store**、输出是**release chain 上的一次 publish** → recipe backend。包括权重步、skill 编辑、ACE 的 reflector/curator pass。**即使它调 LLM 也跑在 Reef 内**：它从不回答用户请求、从不碰环境。

**TTTD 的实例分析**（`:67-84`）：TTTD 运行期间 harness 代码不变，只有 archive 状态变，所以搜索循环留在外部（`recipes/tttd/examples/tttd/`），Reef 侧恰好是需要训练后端的那部分（分组优势计算 + `tttd` loss）。「archive 是 state 而非 genome，因为它的每步更新**不能作为相互竞争的候选被重放**。因此 accept-or-reject gate 不适用。」反向地，TTTD 的 harness **确实**含可以通过 Reef 演化的 genome：prompt 模板、探索常数、`search_value` 设计、两阶段 prefill 文本——「从跨 run 分数演化这些，就是 `CordisBackend` 今天在跑的 gated text-artifact 循环。而 archive 仍然不会移动。」

**这个拆分解锁的三件事**（`:171-188`）：
1. **harness artifact 的无 gate 持续发布**（解锁 ACE）：与在线权重训练结构相同，只是 artifact 取代权重位置；reflector/curator 每 batch 合并 → 发布 → release chain 当安全网。「回滚解决 ACE 自身的 context collapse 失败模式；gate 是可选加固，不是前提。」
2. **种群/Pareto 接受**（解锁 GEPA）：把 gate 从单一 incumbent 推广到后端状态里携带的候选前沿。「Reef 的 record store 已经持有反思式变异器所需的执行轨迹与分数；外部实现将不得不重建这套捕获与版本化。」
3. **run-state 快照上 release chain**（TTTD 持久化，可选）：`PUCTArchive.state_dict()` 已是 JSON-ready；让快照作为附件搭 release chain，能让崩溃 harness 的 resume 与它训练时的 runtime load ID 对齐。「只增加持久化；选择逻辑仍在 harness 侧。」

### 4.2 对 DiceRatchet MOD-05 / MOD-07 边界划分的直接指导

DiceRatchet 当前的模块归属（来自 Spec 与各 PRD）：
- **MOD-05 memory**：反思记忆库——写入、去重、冲突裁决、检索、淘汰、快照/回滚。PRD 自述「偏 run state」（本报告任务描述）。
- **MOD-07 evolve**：harness 候选生成、held-out 裁判、版本化与回滚。PRD 自述「偏 served artifacts」。

把演化边界的两条轴逐条应用到 DiceRatchet：

**(1) MOD-05 的主体是 run state，不应进入 served artifact。**

按轴 1 的判据：MOD-05 的 `memory_active.jsonl` 是「执行固定规则（玩 → 反思 → 记一条）」产生的稠密、有序簿记。**对「追加一条记忆」没有 accept/reject 决策可做**——它不是待裁决的假说，而是算法的记账。它的作用域是「一次 run」（MOD-05 PRD 的 `tick` = 本 run 已完成的训练局序号，评估局不推进 tick——这正是「作用域是一次 run」的本地化表达）。因此：

- **✅ 当前设计已对**：MOD-05 PRD §2.2 明确「不负责：评估记忆是否有用（MOD-08 提供指标，MOD-07 做取舍）」——记忆库不判定自己价值。
- **⚠️ 一个需要修的点**：MOD-05 PRD FR-05-9 让 memory 每代产 `memory_v{N}.jsonl` 快照并支持 `rollback`。按演化边界，**run state 的正确持久化方式是「快照」而非「作为 served artifact 上链」**。Reef 对此的措辞是「Reef may at most persist snapshots」，且明确指出快照的作用是**崩溃恢复时与训练时的版本对齐**（§5.3 的 TTTD durability）。所以 DiceRatchet 的 memory 快照应定位为**断点续跑的恢复点**（MOD-06 的「断点续跑」需求），而不是「可被独立 promote/回滚的演化产物」。当前 MOD-05 把 memory rollback 做成独立 CLI（`memory rollback --to-version 4`）在方向上可以，但**触发必须绑定在 harness 回滚上**（见第 3 点）。
- **⚠️ 反向规则同样适用**：「learned state is runtime output, never committed to the evolved artifact」。MOD-07 的候选生成**不能把当前记忆内容写进它提议的 harness prompt**（否则 prompt 里会固化不断膨胀的历史记忆，正是 ACE 的 context collapse 失败模式）。DiceRatchet 的 INV-3（prompt 只能含四类白名单上下文）已在规则层面挡住了这一点；需要在实现层面确认：**`propose()` 产出的 diff 里不含记忆正文**，记忆只能由 MOD-04 在运行时注入（run state，不入库）。这条是本报告给出的最具体的一条边界裁决。

**(2) MOD-07 的产物是 served artifact，必须完整继承 release chain 机制。**

harness 的 prompts/rules/config/tools 是长寿命、被后续每局共享的，对它们的修改正是需要版本身份、receipts、stale-publish fencing、回滚的东西。MOD-07 PRD 已经在做（`harness/versions/v{N}/`、`version.json` 血缘、`diff.patch`、git commit + tag、rollback），方向正确。**但要把 Reef 的三件事一并搬来**：

- **回滚 = 追加新版本，不重写历史**（Reef 的 `committer.py:356` 实现）。DiceRatchet MOD-05 FR-05-9 已写明「不回退编号」，一致。
- **stale-publish fencing**：DiceRatchet 的「代际」概念里，一个候选从「评估」到「accept」之间，可能已有新一代被 accept。Reef 的解法是 CAS（`repository.py:235-250`）+ 明确 drop reason。DiceRatchet 应在 `versioning.accept()` 里做等价校验：**accept 时检查「评估时所见 parent 版本 == 当前 head」，否则冲突**（MOD-07 PRD 的 `stale_result_policy` 三选项 merge/reevaluate/refuse 正是 Reef 的同构设计，`docs/advanced_topics/state-model.rst` 明确 harness backend 默认 `merge`——理由是「每个 episode 配对在它运行时的相同条件下比较了 candidate 与 current」）。**建议 DiceRatchet 的默认值也设为 `merge` 而非 `refuse`**，理由与 Reef 相同，且能省掉重跑的算力。
- **receipts**：每次评估局的 `report.json` 必须记录「服务它的 harness 版本」。这是 INV-7 可审计性的基础设施形式。

**(3) 最关键的直接指导：把 MOD-05 与 MOD-07 组装成「一个 release 的两个组件」。**

演化边界 RFC 的轴 1 划分了「什么该上链」，但 §4 的组件机制给出了**如何让 run state 与 served artifact 同步回滚**的工程答案：Reef 的多组件 release（`components.py:58-105`）允许一个 release 绑定 `weights` + `harness` + `skills` 多个命名组件，每组件独立 `content_id`，但**回滚按 release 整体**（「a rollback restores every component together」）。

映射到 DiceRatchet：**一代 = 一个 release，含两个组件：`harness`（served artifact）与 `memory_snapshot`（run state 的持久化附件）**。这样：
- DiceRatchet MOD-05 PRD 的 S4 场景（「harness 回滚到 v6 时 memory rollback --to-version 4 恢复同代记忆」）从「两个 CLI 要人肉对齐」变成**结构上的原子操作**；
- memory 快照精确落在 RFC §5.3 定位里——「作为附件搭 release chain」，「只增加持久化；选择逻辑仍在 harness 侧」；
- 同时不违反轴 1：memory 仍然是 run state（检索/淘汰/冲突裁决逻辑仍在 MOD-05 内，不上链），上链的只是**快照字节**。

这是本报告认为**最值得直接采纳**的一条设计迁移。

**(4) 数据流判据在 DiceRatchet 上的具体应用。**

把「输入是请求/环境、输出是下一个请求 → harness（跑在 Reef 外）」与「输入是 record store、输出是 publish → backend（跑在 Reef 内）」翻译成 DiceRatchet：

| DiceRatchet 代码 | 输入 → 输出 | 归属 |
|---|---|---|
| MOD-04 policy 决策循环 | Observation → 下一个 Action | **harness 侧**（等价 Reef 的 loop） |
| MOD-06 的「玩」阶段 | seed → 轨迹 | **harness 侧**（任务编排） |
| MOD-06 的「反思」阶段 | 轨迹 → 反思文本 | **harness 侧**（产出 run state） |
| MOD-05 检索/淘汰/去重 | 记忆库状态 → 记忆库状态 | **harness 侧**（run state 簿记） |
| MOD-07 `propose()` | 证据束（轨迹/记忆/反思）→ 候选 diff | **backend 侧**（输入是记录，输出待 publish 的内容）——**即使它调 LLM** |
| MOD-07 `judge()` + `versioning.accept()` | 评估结果 → release chain 一次 publish | **backend 侧** |
| MOD-08 评估执行器 | harness_version + seeds → 指标 | **harness 侧**（判分/测量， Reef 把 `evaluate` 也归在引擎外的方法侧） |

**一个容易搞错的点**：MOD-07 的 `propose()` 调 LLM 生成 diff，看起来像「agent 在做事」，但按数据流判据它是 backend——**它从不碰环境、从不回答游戏请求**。反之 MOD-04 的决策循环看起来是「agent 的脑子」，但它是 harness。演化边界 RFC 的原话值得作为 DiceRatchet 的代码评审检查项：「The placement litmus is the direction of data flow, not how agent-like the code is」。

**(5) 「gate 不是必需的」对 DiceRatchet 的实际含义。**

Reef 明确：`openclawrl`/`sao` 每 batch 无 gate 发布，安全网是 release chain 而非 gate；harness artifact 目前不能无 gate 是实现缺口。DiceRatchet 的 MOD-07 设计了相当重的 gate（六项判定表 + HITL 挂起）。**这不是错**——DiceRatchet 面对的是「agent 改写自己的 harness」，风险高于权重更新，且项目把「自我确认环路」当一等公民。但演化边界的论证给出一条重要的**设计纪律**：

> gate 的存在与否是风险策略，不应是「因为我不信任这个更新所以连版本化都省了」的理由。**版本化/receipts/回滚是无条件的基础设施；gate 是加在上面的可选加固。**

这意味着 DiceRatchet 即使在 gate 极严的阶段，也必须先把 release chain 做扎实（因为「拒绝」的靠谱程度完全取决于「回滚」的靠谱程度）。反过来，**M3 阶段可以考虑一个「无 gate 持续演化」的实验臂**（对应 RFC §5.1 解锁的 ACE 路径）：把 prompt 类改动做成每代自动发布、靠回滚 + recheck 兜底，code 类改动仍走 HITL。这正是 Reef 用 `evolution.publish: review` + `evolution.review_kinds: [code_extension]` 实现的**分级发布策略**（`docs/user-guide/evolve-your-harness.rst`）：「rules 和 config 自动发布，code 等人」；而 `native_loop` 类的改动**无论是否列入 review_kinds 都强制等待**（「the model proposes a loop, a person serves it」）。**这条分级策略建议直接搬到 DiceRatchet MOD-07**：`prompt_patch` 自动发布、`tool_patch`/`config_patch` 走 HITL。

---

## 5. 与 DiceRatchet 的对标矩阵

映射维度：Reef 机制 → DiceRatchet 的 MOD-xx / INV-x。可移植性三档：**✅ 可直接移植**、**🟡 需改造**、**❌ 不适用**。

### 5.1 基础设施思想（可移植性高）

| Reef 机制 | 关键引用 | DiceRatchet 落点 | 档位 | 说明 |
|---|---|---|---|---|
| Release chain：三重身份分离（release_id / content_id / runtime_load_id） | `artifact_ref.py:10-36`，`glossary.rst:69-88` | MOD-07 versioning | ✅ | DiceRatchet 的 `version.json` 血缘已是雏形；补 `content_id`（内容哈希）与「服务版本」分离 |
| 回滚 = 追加新版本、不重写历史 | `committer.py:356-449`，`glossary.rst:78-80` | MOD-07 `rollback`、MOD-05 FR-05-9 | ✅ | MOD-05 已写「不回退编号」，一致 |
| Stale-publish fencing（CAS head + lease + drop reason） | `repository.py:235-250`，`state-model.rst` | MOD-07 `accept()` | ✅ | accept 前校验「评估时 parent == 当前 head」；采用 `merge` 默认 |
| 多组件 release（harness + memory_snapshot 同链回滚） | `components.py:58-105` | **MOD-07 × MOD-05 联动** | ✅ | **本报告最推荐的一条**（见 §4.2 第 3 点） |
| Receipts（结果 ↔ 版本绑定，report 至多消费一次） | `inference.py:107`，`glossary.rst:29-35` | MOD-08 `report.json` | ✅ | 加 `served_harness_version` 字段 |
| 测量/决策分离（Evaluator vs Selector vs Plugin） | `evaluation.py:86-105` | MOD-08（出数字）× MOD-07（判定） | ✅ | DiceRatchet 已这么切；补 `SelectionDecision.reason` 的结构化 |
| Recipe = 方法绑定单元 + dotted 显式引用，core 不 import 方法 | `recipe/__init__.py`，`registry.py:31` | MOD-09 的包结构 | ✅ | 演化机制进内核，提候选/判分作可插拔方法 |
| 建议与 commit 分离（`StepSignal.action="skip"`，trainer 拥有 commit） | `signals.py:14-21` | MOD-07 propose → versioning | ✅ | 候选生成不 commit，版本化层独占 commit 权 |
| commit 排序与重试幂等（`commit_step(expected_step=...)`，相同重试返回原 commit） | `state-model.rst`「Commit ordering」 | MOD-06 断点续跑 / MOD-07 | ✅ | 代际号 = expected_step，崩溃重跑幂等 |
| 分级发布（prompt 自动 / code 等人 / loop 强制等人） | `evolve-your-harness.rst`「Promotion」 | MOD-07 `gate.py` | ✅ | `prompt_patch` 自动、`tool_patch` HITL（见 §4.2 第 5 点） |
| 评估套件自动生长 + 凭据/指令注入筛查 + 单 client 上限 | `evolve-your-harness.rst`「Promotion」 | MOD-08 种子协议 + MOD-07 | 🟡 | DiceRatchet 的 INV-5 seed 隔离已覆盖大部分；「单 client 上限」在单机项目里对应「单一代际贡献上限」 |
| recheck（长成的套件回查已发布树，输了就回滚） | `evolve-your-harness.rst`「Rechecking」 | MOD-08 × MOD-07 | ✅ | 对 DiceRatchet 的「坍缩监控」是有力补充：回查比只看当前代更能发现渐进退化 |
| 拒绝历史喂给 proposer（`max_rejected_history`） | `evolve-your-harness.rst` | MOD-07 propose | ✅ | 防止重复提已被拒的改动，省算力 |
| 「渲染失败即提议失败」的准入 | `mutations.py:146-174` | MOD-07 候选校验 | ✅ | 在 AST 扫描后加一层「沙箱真加载一次」 |
| 改 kind 必须 remove+create；保留 id 不可动 | `mutations.py:106-139` | MOD-07 diff 约束 | ✅ | 对应「禁止改 `rank_actions` 签名」 |
| Runtime staleness 窗口（`max_staleness`，跨 incarnation drop） | `scheduler.py:240-258`，`base.py:385` | MOD-06 局间版本一致性 | 🟡 | Mac 单机无多引擎，但「用旧 harness 版本玩的局是否仍计入训练」是同一问题 |

### 5.2 harness 演化方法（需改造）

| Reef 机制 | 关键引用 | DiceRatchet 落点 | 档位 | 改造点 |
|---|---|---|---|---|
| 双侧对照执行（candidate vs current 同 task、interleaved） | `evolve-your-harness.rst`「Cost」 | MOD-07 `evaluate_candidate` | ✅ | DiceRatchet 的配对评估同构；interleaving 在单机顺序执行下用「同 seed 组内交替」实现 |
| `min_win_margin` 噪声地板 | `evolve-your-harness.rst` | MOD-07 判定表 | ✅ | 对应 `win_rate_gap` 阈值 |
| GEPA 的 Pareto 前沿候选选择 | `recipes/gepa/archive.py:275-289, 357-377, 420-442` | **MOD-07 proposer** | 🟡 | **最值得引入的算法机制**（见 §5.3）。改造：DiceRatchet 的「任务」是 seed 而非 benchmark 题，前沿按「在哪些 seed 上最强」建 |
| GEPA 的 round-robin 组件选择（游标被子节点继承） | `archive.py:223-242`，`method.py:197-243` | MOD-07 | ✅ | 直接可用：避免总改同一个 prompt 文件 |
| GEPA 的「父=当前服务组合时 minibatch 免费」 | `method.py:283-289` | MOD-07 成本控制 | ✅ | 直接可用：复用已记录的训练局轨迹，不重跑 |
| GEPA 的 feedback 函数 `µ_f`（分数 + 环境文本轨迹） | `recipes/gepa/README.md`，论文 §3 | MOD-07 证据束 + MOD-06 反思 | ✅ | **用沙箱审计日志/非法原因/fallback 原因当 `feedback_text`**，环境原生且丰富 |
| GEPA archive 只在 commit 后落盘（失败不推进搜索） | `archive.py:6-9, 169-195` | MOD-05 / MOD-07 状态持久化 | ✅ | 直接可用：原子 tmp+replace + post-commit 镜像 |
| TTTD 的 PUCT 复用（Q 取最大、探索项防过用） | `recipes/tttd/examples/tttd/harness/search.py`，论文 §3.2 | **MOD-05 检索排序** | 🟡 | 见 §3.2：Q 用最大而非均值；加 `√(T/(1+n))` 探索项。改造：DiceRatchet 无搜索树，把「状态」换成「记忆条目」 |
| OpenClaw-RL 的 evaluative/directive 二分 | 论文 §3.1 | MOD-06 反思信号 | 🟡 | 用环境原生结果当 evaluative；directive 从环境结构（非法原因、fallback）而非 LLM 自评抽取 |
| OpenClaw-RL 的四组件解耦 | 论文 §2.2 | MOD-06 loop | ✅ | 玩/反思/演化/评估解耦，版本切换只在良好边界 |
| ACE 式无 gate 持续发布 | `evolution-scope.rst:171-177` | MOD-07 M3 实验臂 | 🟡 | 见 §4.2 第 5 点 |

### 5.3 权重训练路径（不适用）

| Reef/论文机制 | 为什么不适用 | 残余价值 |
|---|---|---|
| Slime / Megatron 异步训练后端（`reef/train/slime_backend/`） | 需要 CUDA 集群；Mac 上 MLX 8B 无梯度训练路径 | — |
| SGLang / vLLM serving（`reef/inference/sglang/`、`reef/inference/vllm/`） | Mac 上用 MLX 本地推理；DiceRatchet MOD-03 已自定 | — |
| `loss_family`（sao/tttd/openclawrl/sdft）+ Slime `--loss-type` 校验 | 纯 GPU 训练语义 | 「objective 声明 loss family、driver 启动校验一致」的**校验思想**可移植到 DiceRatchet（演化方法声明其判分器版本，启动时校验） |
| `StepScheduling`（comparison_set/sample、epochs、remainder、DP/micro-batch） | GPU 切分语义 | 「rollout 是不可切分单元」的思想可保留 |
| SAO 的 DIS / 双侧 mask / faster value / frozen-attention / Skip-Obs GAE | 权重训练算法 | Skip-Obs GAE 的**洞察**（不跨环境观测边界做信用分配）可用于反思设计（§3.1） |
| TTTD 的 entropic objective + LoRA | 权重训练 | 「优化最大值而非均值」的**目标函数哲学**可用于 MOD-08 的指标设计（关注最佳局而非平均） |
| OpenClaw-RL 的 hybrid RL objective / overlap-guided hint selection / logprob clip | 权重训练 + 需独立 PRM 服务 | PRM 在 Mac 上成本翻倍；evaluative/directive 二分可保留（§5.2） |
| Live weight release（`LiveWeightArtifactRef`、引擎内字节、checkpoint cadence） | 无可热切换的推理引擎 | — |
| `WeightUpdateLock` / `WeightPublisher` / `TrainingPublication` 权重传输 fencing | 无权重传输 | 「半应用状态的 fence」思想可迁移到 harness 热加载（MOD-02 worker 切换） |
| git-lfs 大文件存储（`reef/artifact/git_lfs.py`，877 行） | DiceRatchet 的产物是文本（prompt/diff/jsonl），无大文件 | — |

---

## 6. 风险与陷阱

### 6.1 权重训练路径在 Mac 上不可行（致命，但易误判）

- **直接不可行**：Slime/Megatron 需 CUDA；SAO 的 value model 需要 policy 2 倍更新频率；TTTD 需要 LoRA rank 32 + 每步 512 rollout（论文说每问题约 $500，用的是 Tinker 云 API）。8B 模型在消费级 Mac 上做这些，单是显存/统一内存就不够。
- **易误判的点**：Reef 的 README 和 docs 大量篇幅在讲权重训练（`evolve-your-model.rst`、loss families、Slime 集成）。**读 Reef 时必须有意识地忽略这一半**，否则会把不适合的抽象（`loss_family`、`StepScheduling`、`max_staleness` 的引擎语义）引入 DiceRatchet，增加复杂度却没有收益。
- **一个真实的反向信号**：OpenClaw-RL 的实验（表 3）显示 hybrid RL（改权重）在个性化任务上把 memory（Mem0）与 skill-evolution 方法甩开 3 倍（10.3 vs 29.7 sessions）。**DiceRatchet 选的正是被论文证明更慢的那条路**。这不构成否证（DiceRatchet 的约束就是不能改权重），但意味着：**DiceRatchet 必须在 MOD-08 里明确测量「记忆/演化路线在本地 8B 上的效率曲线」**，并诚实报告「我们选了受限但可行的路径，代价是 X 倍的样本」。GEPA 的结果其实给出了反击弹药：**prompt 演化用 678 rollout 打败 24,000 rollout 的 GRPO（35× 更少）**——在不改权重的前提下，GEPA 路线反而比权重 RL 更样本高效。

### 6.2 依赖重、运行规模差异大

- Reef 是**服务化多租户系统**：HTTP API、多 scenario 隔离、SQLite/Postgres 记录存储、Git/LFS artifact 仓库、bubblewrap 沙箱、E2B 云沙箱、多 trainer 并发与 commit 序列化。`uv.lock` 1MB+，`pyproject.toml` 有 slime/tinker/runtime 等可选 extra。
- DiceRatchet 是**单机单进程实验**。**不要引入 Reef 的 service 层**（`reef/service/`、dispatcher、scenario registry）。可移植的是**值类型与契约**（`core/` 下的 dataclass、`evaluation.py`、`components.py`），不是运行时。
- 具体建议：把 `reef/core/` 的几个纯数据类（`ArtifactRef`、`ReleaseComponents`、`SelectionDecision`/`EvaluationResult`/`UpdateCandidate`、`StepSignal`）**按语义重写为 TypeScript/项目语言**，而不是包依赖。它们都是 <200 行的纯值类型，重写成本远低于依赖成本。

### 6.3 macOS 沙箱的隔离边界弱于 Reef 的假设

- Reef 的 `sandbox` executor 依赖 **bubblewrap（Linux namespace）**，并明确指出 **macOS 或无 Linux namespace 的主机必须走 E2B 云沙箱**（`evolve-your-harness.rst`「Running episodes」）。native adapter 下每个 tool call 再套一层 nested jail。
- DiceRatchet 在 Mac 上跑，且 MOD-02 的「防作弊沙箱」是核心卖点。**必须自己定义并记录隔离边界**：用 `sandbox-exec`/seatbelt 或子进程 + 降权，但要诚实承认「比 bubblewrap 弱」，并把「沙箱逃逸检测」（蜜罐、审计扫描）作为一等公民——DiceRatchet 的 MOD-02 PRD 已经在这么做（白名单观测投影 + 蜜罐）。**建议在 MOD-02 文档里显式引用 Reef 的边界声明作为对照**，说明「我们知道自己比 Reef 的 Linux 方案弱在哪，以及用什么补偿」。

### 6.4 「agent 改自己的 harness」比 Reef 的场景更危险

- Reef 的 harness 演化面向**人类操作的编码 agent**（pi/opencode/Claude Code/Codex 等），且对 `code_extension` 默认 `publish: review`、对 `native_loop` **无条件等人 promote**（「the model proposes a loop, a person serves it」）。
- DiceRatchet 是**无人值守的闭环自改进**，agent 改的是**判自己胜负的那个 harness**。INV-4（harness 代码受限执行）必须比 Reef 更严：Reef 的 `code_extension` 允许 harness 在进程内加载代码，DiceRatchet 的 MOD-02 把 harness 工具放进子进程 worker + AST 守卫，方向正确。
- **一个具体陷阱**：Reef 的 `admit_mutations` 会**真正跑一次 render** 来验证提议。DiceRatchet 若只做 AST 扫描而不「真加载一次」，会放过「能编译但运行时偷偷读 seed 注册表」的候选。MOD-07 FR-07-4 的「禁止网络/文件写/subprocess/env 访问」需要**运行时强制**（沙箱 capability），不能只靠静态扫描。

### 6.5 结果声明的方法学自律

- Reef 的 GEPA 复现（`recipes/gepa/README.md`）在报告 +16.00 pp vs 上游 +12.67 pp 时明确写：「the selected-score difference changes sign between seeds, so these results validate the method but do not establish superiority」。Meta-Harness 的 RESULTS.md 在 Terminal-Bench 复现中写：「Both selectors made identical decisions when replaying the same completed score histories…… This verifies the overall selection rule given identical observations; independent proposals and scores can differ」，并标注「These results did not feed back into search and are not a held-out task evaluation」。
- **DiceRatchet 的失败模式正是一等公民的「自我确认环路」**。这两段写法应作为 DiceRatchet MOD-08 报告模板的范例：**区分「验证了机制」与「确立了优越性」；区分「喂回搜索的数据」与「held-out 评估」；任何 replay 对照都要声明它验证了什么、没验证什么**。

---

## 7. 分阶段行动清单（M0 → M3）

按 DiceRatchet 的里程碑（M0 环境 → M1 → M2 → M3）排列，每项标注来源与映射。

### M0（环境层）

| # | 行动 | 来源 | 映射 |
|---|---|---|---|
| M0-1 | 确立「演化机制内核 vs 方法插件」的包结构：版本化/回滚/receipts/沙箱执行在内核；propose/判分可插拔 | `docs/contributing/codebase-structure.rst`；`AGENTS.md` 边界表 | MOD-09 包结构，对应 Reef `reef/` vs `recipes/` 分层 |
| M0-2 | 定义三重版本身份的纯值类型：`VersionId`（发布决策）/ `ContentId`（内容哈希）/ `ServedVersion`（当前服务版本） | `artifact_ref.py:10-36`，`glossary.rst:69-88` | MOD-07 versioning 数据模型 |
| M0-3 | 定义「测量/决策分离」的契约：`EvaluationResult`（带 `evaluator_version`）+ `SelectionDecision`（带 `policy`、`reason`） | `evaluation.py:27-83` | MOD-08 ↔ MOD-07 接口 |
| M0-4 | 沙箱边界声明文档：明确 macOS 隔离手段与弱于 bubblewrap 的补偿（审计/蜜罐/capability） | `evolve-your-harness.rst`「Running episodes」 | MOD-02，见 §6.3 |

### M1（测量与基线）

| # | 行动 | 来源 | 映射 |
|---|---|---|---|
| M1-1 | `report.json` 加 `served_harness_version`（receipt），使每个结果可追溯到版本 | `inference.py:107`，`glossary.rst:29-35` | MOD-08，INV-7 可审计性 |
| M1-2 | 基线对比必须包含「样本效率曲线」而非单点分数（GEPA 用 678 rollout 打败 GRPO 24,000） | GEPA 表 1 | MOD-08 基线，回应 §6.1 的 OpenClaw-RL 反向信号 |
| M1-3 | 建立环境原生 `feedback_text` 通道：沙箱审计日志、非法动作原因、fallback 原因结构化记录，供反思与 `µ_f` 使用 | GEPA 论文 §3「evaluation traces」 | MOD-06 反思 × MOD-02 审计 |
| M1-4 | 代际号作为 `expected_step` 的幂等 commit 点：崩溃重跑返回原 commit | `state-model.rst`「Commit ordering」 | MOD-06 断点续跑 |

### M2（演化与记忆初版）

| # | 行动 | 来源 | 映射 |
|---|---|---|---|
| M2-1 | **把 memory 快照作为 release 的附件组件**，与 harness 同链发布、整体回滚 | `components.py:58-105`；`evolution-scope.rst:184-188` | **MOD-07 × MOD-05 联动**（本报告最推荐，见 §4.2-3） |
| M2-2 | 候选准入加「沙箱真渲染/真加载一次」层，静态扫描之后、heldout 之前 | `mutations.py:146-174` | MOD-07 FR-07-4 强化 |
| M2-3 | accept 前做 CAS 校验（评估时 parent == 当前 head），默认 `merge` 策略 | `repository.py:235-250`；`state-model.rst` stale_result_policy | MOD-07 versioning |
| M2-4 | **引入 GEPA 的 Pareto 前沿候选选择**：按「在哪些 train seed 上最强」建前沿、剪支配、按领先任务数加权采样父节点 | `archive.py:275-289, 357-377, 420-442`；GEPA Algorithm 2 | **MOD-07 proposer**（对应论文 +12.44 vs SelectBest +6.05） |
| M2-5 | round-robin 组件选择（游标继承），避免总改同一 prompt 文件 | `method.py:197-243`，`archive.py:379-387` | MOD-07 |
| M2-6 | 「父=当前服务版本时复用已记录轨迹，不重跑」 | `method.py:283-289` | MOD-07 成本控制 |
| M2-7 | 拒绝历史喂给 proposer（去重 + 不重复提被拒改动） | `evolve-your-harness.rst`（`max_rejected_history`） | MOD-07 FR-07-5 强化 |
| M2-8 | MOD-05 检索排序引入 TTTD 复用启发式：Q 取「该记忆参与过的最佳局」而非平均胜率；加探索项防过用少数高分记忆 | TTTD 论文 §3.2；`search.py` PUCTArchive | **MOD-05 检索打分** |
| M2-9 | 反思信号区分 evaluative（环境原生结果）与 directive（环境结构化修正原因） | OpenClaw-RL 论文 §3.1 | MOD-06 反思 |

### M3（回滚、消融、终检）

| # | 行动 | 来源 | 映射 |
|---|---|---|---|
| M3-1 | recheck 机制：用已生长的评估套件回查上次发布树，输了自动回滚 | `evolve-your-harness.rst`「Rechecking」 | MOD-08 × MOD-07，坍缩监控的补充 |
| M3-2 | 分级发布：`prompt_patch` 自动发布、`tool_patch`/`config_patch` 走 HITL；含 harness 循环代码的改动无条件等人 | `evolve-your-harness.rst`（`publish: review` + `review_kinds` + native_loop 强制） | MOD-07 `gate.py` |
| M3-3 | 「无 gate 持续演化」实验臂（对应 ACE 路径）：prompt 类每代自动发布、靠回滚+recheck 兜底，与 gated 臂对照 | `evolution-scope.rst:171-177` | MOD-07 消融 |
| M3-4 | held-out 结论按 Reef 的方法学自律写：区分「验证机制」与「确立优越性」，声明哪些数据喂回了搜索 | `recipes/gepa/README.md`；`recipes/meta_harness/RESULTS.md` | MOD-08 报告模板 |
| M3-5 | 消融必须包含「记忆路线 vs 纯 prompt 演化」的效率对照，诚实报告样本效率代价 | OpenClaw-RL 表 3（10.3 vs 29.7 sessions）；GEPA 表 1 | MOD-08 消融，回应 §6.1 |

---

## 附录：核心引用速查

| 主题 | 引用 |
|---|---|
| 演化边界论证 | `docs/rfcs/evolution-scope.rst:34-41`（gating 不能定放置）、`:48-61`（两轴）、`:94-109`（三策略）、`:148-158`（数据流判据）、`:171-188`（三解锁） |
| 三重身份 | `reef/core/artifact_ref.py:10-36`；`docs/reference/glossary.rst:69-88` |
| 回滚实现 | `reef/scenario/committer.py:356-449` |
| Stale fencing | `reef/artifact/repository.py:235-250`；`reef/runtime/scheduler.py:240-258`；`reef/runtime/publication.py:572-618` |
| 多组件 release | `reef/core/components.py:58-105` |
| 训练契约四件套 | `reef/train/algos/objective.py:13-49`；`reef/train/algos/signals.py:14-21`；`reef/core/batches.py:111-169`；`reef/recipe/base.py:327-348` |
| 候选选择契约 | `reef/core/evaluation.py:12-105` |
| Harness 演化引擎 | `reef/train/cordis_backend/__init__.py:1-27`；`reef/harness/tree/mutations.py:19-43, 104-174` |
| GEPA archive | `recipes/gepa/archive.py:197-202, 275-289, 357-377, 420-442` |
| GEPA proposer/selector | `recipes/gepa/method.py:129-243, 353-413` |
| GEPA 反思 prompt | `recipes/gepa/reflection.py`（MIT 逐字复制，带版权声明） |
| TTTD PUCT archive | `recipes/tttd/examples/tttd/harness/search.py` |
| 双侧对照执行与沙箱 | `docs/user-guide/evolve-your-harness.rst`「Running episodes」「Cost」「Promotion」「Rechecking」 |
