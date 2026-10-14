# MOD-07 evolve 技术 PRD

> **版本**：v0.9（2026-10-11 **C5 收紧为三轴同构的「相对门 + 绝对 backstop」**——`illegal_tol`/`fallback_tol` 新增并取代绝对门、`truncation_backstop` 堵住 §8.2.2 的已知放宽、`condition_trace` 可审计；处置 D-M3-2 裁决 E；见 §8.2.1/§8.2/§9/§17）
> **历史版本**：v0.8（2026-10-11 **新增 §6.7：每代评估的检查点与复用**（臂归档 `metrics/arms/` + fail-closed 复用 + 提案检查点 + `n_workers`/`n_rep` 转发 + 中断穿透）——处置 F42-A+B+C；见 §17）
> **历史版本**：v0.7（2026-10-10 **新增 §6.6：accept/rollback 的失败自清与失败口径**——处置 F47-C2/D：失败时删掉本次新建的快照（否则顶掉版本号并留下可被 load/rollback 取用的幻影版本）、版本冲突也失败保守、基础设施失败记 `decision="error_reject"` 且 `report.json` 不得写成 `accept`；见 §17）
> **历史版本**：v0.6（2026-10-10 **新增 §6.5：提案输入与补丁应用的硬口径**——处置 F37（按调用方给输出预算）/F38（内联可改文件正文）/F39（容忍行偏移、不容忍 fuzz）/F40（prompt 必须列出校验器规则）/F41（冒烟臂 `-smoke` 的 `episode_key`）；见 §17）。
> **历史版本**：v0.5（2026-10-09 **处置缺陷 F28**：C5 的 `truncation` 由绝对门 0.15 改为**相对门** `candidate ≤ parent + truncation_tol(0.05)`，`illegal`/`fallback` 保留绝对门；新增 §8.2.2、更新 §8.2/§8.2.1/§9，对齐 Spec v2.0 B-44 用户裁决 ②+③；v0.4 = 2026-10-07 按 Spec v1.8 回填 C5 实测标定与 M3 相对门设计，见 §8.2.1/§17）· **状态**：待评审 · **所属里程碑**：M2（propose/evaluate/versioning/HITL 初版）→ M3（回滚/消融对接/统计口径收紧）· **作者**：DiceRatchet 技术文档 · **日期**：2026-10-09
> **上游 Spec**：[../模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md)（v1.8，**唯一事实来源**；冲突以 Spec 为准）· **关联不变量**：INV-3/INV-4/INV-5/INV-7
> **代码位置**：`dice_ratchet/evolve/{proposer,judge,versioning,gate}.py`、`harness/`、`runs/<run_id>/generations/`

---

## 1. 背景与目标

MOD-06 每 K 局（默认 30）触发演化周期，把「策略记忆 + 最近轨迹 + 反思」交给 MOD-07。MOD-07 负责 RSI 闭环中最危险的一段：**让 agent 改写自己的 harness，并由环境 ground-truth 决定改动去留**（计划 §5 步骤 1–5）。按 RSI 调研 §4 的失败模式，设计重心不是"生成更聪明的改动"，而是**在结构上掐死自我确认环路**：

- 自我确认环路：候选生成阶段**物理上看不到** heldout/test 数据（INV-5），裁判只看环境原生结果（INV-7），生成者与裁判者上下文不重叠。
- 模型坍缩：接受判定强制 `collapse_flag == False` + 熵斜率下限（C4）。
- 过度优化/指标刷分：改动面硬上限 + `win_rate_gap` 阈值 + 消融对照（M3）。

**目标（可验收）**：① `propose()` 稳定产出 2–3 个通过校验的候选，或返回显式空结果 + `E_EVOLVE_NO_CANDIDATE`，绝不静默退化；② 每个候选走「train 冒烟 → heldout 正式评估」，同 seed 同结果；③ 判定是可执行判定表（§8.2），六项全通过才 accept；④ 每次 accept 产生不可变版本记录 + git commit；⑤ HITL 开启时挂起等 `evolve approve|rollback`；⑥ `report.json` 逐字段对齐 Spec §4.5 并通过 round-trip 测试。

## 2. 范围

### 2.1 范围内

`propose()`（候选生成/去重/证据束白名单）；候选合法性校验（MOD-02 AST 扫描、改动面计数、禁用符号、JSON Schema）；`evaluate_candidate()`（train 冒烟 → heldout 正式评估，**只编排** MOD-08）；`judge()` 判定表与 `reason`；`versioning.py`（`harness/versions/v{N}/`、`version.json` 血缘、`diff.patch`、git commit + tag `harness-v{N}`、`rollback`）；`gate.py`（HITL 挂起/恢复/超时回滚）；代际日志与 `report.json`；`E_EVOLVE_*` 错误码的产生与上报（登记由 MOD-09 完成）。

### 2.2 范围外（非目标）

> 逐字复用 Spec §3「MOD-07 evolve」的**不负责**清单：

不负责：模型推理实现（MOD-03）、指标定义与统计口径（MOD-08）、seed 抽取规则（MOD-09 注册表 + MOD-08 协议）。

边界补充（不引入新职责）：不改 `dice_ratchet/agent/policy.py` 的解析与校验逻辑（MOD-04）；不做超参搜索；不做 LoRA/微调（MOD-10）；不决定 harness 内容的**语义**，只负责把 LLM 给出的 `diff` 落盘并版本化。

## 3. 使用方与场景

| 使用方 | 调用方式 | 场景 |
|---|---|---|
| MOD-06 loop | `propose → evaluate_candidate → judge → versioning.accept/rollback → gate.await_human` | `play → evaluate → reflect → evolve` 的 evolve 段 |
| MOD-08 evals | 被调用：`evaluate_arm(...)`、`compare_paired(a, b)`、`evaluate_generation(...)`（Spec §4.7） | 提供 heldout 指标、显著性与 `collapse_flag`；MOD-07 不做统计 |
| MOD-02 env-sandbox | 被调用：`guard.gate(harness_dir) -> GateReport`、`guard.load_harness_tools(harness_dir) -> ToolWorker`（Spec §4.7） | INV-4：新 harness 先静态扫描，再在子进程 worker 中加载 |
| 人类操作者 | `python -m dice_ratchet evolve approve\|rollback --generation N [--reason ...]` | HITL 确认/回滚，查看 `diff.patch` 与 `report.json` |
| MOD-09 infra / MOD-05 memory | 提供 config/events/git_ops/guards；只读 `retrieve_for_evolution(top_k)` | 配置优先级、结构化事件、git 封装、AST 守卫；策略记忆与反思（进证据束） |

正常流：`loop run --generations 3 --k 30 --hitl` → gen-1 propose 3 个候选 → 冒烟淘汰 1 → heldout 评 2 → 1 个 accept → 挂起 → `evolve approve --generation 1` → 续跑 gen-2。
被攻击流：`diff` 含 `import os` / heldout seed 字面量 / 改 `rank_actions` 签名 → 静态扫描直接拒绝，写 `E_EVOLVE_SCAN_FAILED`，**0 局 heldout 消耗**。

## 4. 功能需求（FR-07-x，编号，每条可测试）

- **FR-07-1 候选生成**：`propose()` 输入 `EvidenceBundle`，输出 `list[Candidate]`，长度 ∈ [1, `evolve.n_candidates`]（默认 2，上限 3；Spec §8.6 成本模型）。每条含 `kind ∈ {prompt_patch, tool_patch, config_patch}`、`diff`（unified diff）、`rationale`（≤300 字）、`expected_effect{metric,direction,priority}`。失败返回 `ProposalResult(candidates=[], error_code="E_EVOLVE_PROPOSE_FAILED")`。
- **FR-07-2 证据束白名单**：`EvidenceBundle` 只能由 `trajectory_summary(split="train")`、`memory.retrieve_for_evolution()`、`reflection_set(split="train")`、`harness_snapshot()` 构造；`build_evidence_bundle()` 是唯一构造函数，`__post_init__` 校验 `source_tags ⊆ {"memory:train","traj:train","reflect:train","harness:current"}`。
- **FR-07-3 防自我确认（生成侧）**：证据束条目 `split` 必须全为 `train`；`assert_no_holdout_leak()`（1）拒绝任何 `split != "train"` 条目，（2）对全部文本字段做 seed 字面量扫描——命中 heldout 注册表任一 seed 的整值 token（正则 `(?<!\d){seed}(?!\d)`）即抛 `E_EVAL_INV5_LEAK`，（3）拒绝出现 `heldout`/`test` 标签或 `seeds.yaml` 片段。`render_proposer_prompt()` 模板化产出，**不允许**插入证据束之外的上下文（INV-3）。
- **FR-07-4 修改面硬约束（单候选）**：改动行数 ≤ `evolve.max_diff_lines`（40）且字符数 ≤ `evolve.max_diff_chars`（4000）；文件数 ≤ 2 且必须 ∈ 白名单 `harness/prompts/decision.md`、`harness/prompts/reflection.md`、`harness/tools/*.py`、`harness/config.json`；**禁止**改 `rank_actions` 签名与 `@harness_tool` 语义；**禁止**新增第三方依赖（顶层 import 必须 ∈ `evolve.tool_import_allowlist` 或 `tools/` 内已有模块）；**禁止**网络/文件写/subprocess/env 内部访问（INV-4）；`config_patch` 只能改 `evolve.evolvable_keys` 声明的键。
- **FR-07-5 去重与排序**：按归一化 `diff` 的 sha256 去重；按 `expected_effect.priority` 降序、同优先级 `candidate_id` 升序（保证确定性）。
- **FR-07-6 训练冒烟**：
  - 冒烟 seed 取**本代训练块的末 5 个**（`evals.seeds.take("train", generation=g)[-5:]`），不再固定用 1000–1004（那是第 1 代的训练 seed）。
  - parent 在同一组 seed 上的冒烟结果 `parent_smoke` 与 parent heldout 一起计算。
  - 快速淘汰条件（任一命中即写 `E_EVOLVE_SMOKE_FAILED`）：
    - 冒烟胜率 < `parent_smoke` 胜率 − 0.2；
    - 非法率 > 0.30；
    - fallback 率 > 0.30；
    - harness 运行期异常。
- **FR-07-7 正式评估**：冒烟通过者在 `evolve.heldout_seeds`（默认 `heldout:2000-2039`，40 局）评估，**预算与当前版本完全相同**（同 seed 集合、同 `episode_config`、同采样参数，仅 `prompt_tokens` 允许差异并记录）；指标全部委托 MOD-08 `evaluate_arm()`；**parent 与候选必须使用同一个冻结的记忆快照** `memory_version`；parent 每代都要有**测量**，但它**未必每代都重测**——上一代被接受的候选已经作为新版本被完整测过一次，本代**继承**那批局（见 §8.1 的 F57 规则）。MOD-07 不做任何统计运算。
- **FR-07-8 判定表**：按 §8.2 顺序短路求值，六项全 True → `accept`，否则 `reject` 且 `reason` 含首个失败条件 id（如 `C2:heldout_win_rate 0.28<0.32`）。
- **FR-07-9 版本化**：`accept()` 生成 `harness/versions/v{N}/`（全量拷贝 + `version.json` + `diff.patch`），更新 `harness/version.json` 指针，经 MOD-09 `commit_harness` 提交到当前分支，并打 tag `harness-v{N}`（不建同名分支，Spec B-22），message = `harness(v{N}): gen-{G} {kind} {short_rationale}`；`N` = 现有最大 +1，**不可复用、不可重写历史**。
- **FR-07-10 回滚**：`rollback(to_version)` 用 `versions/v{to_version}/` 覆盖 `harness/`，写 `version.json`、生成 `revert.patch`、提交 `revert(harness): back to v{N}`，并写一条 `decision="rollback"` 的代际报告。
- **FR-07-11 HITL 门**：`evolve.hitl=true` 时 `gate.await_human()` 把决策置 `pending`、写 `gen-{N}/gate.json`（`generation, harness_version, parent_version, diff_path, report_path, status, expires_at`），loop **优雅退出（exit 0，可续跑）**；`evolve approve|rollback --generation N` 读取 gate 文件后执行 accept/rollback，写 `decided_by="human"` 与可选 `reason`；超时（默认 24h）→ 保守回滚 `parent_version`，`reason="hitl_timeout"`（对齐 Spec §8.5）。
- **FR-07-12 拒绝记录**：任何 reject（含冒烟淘汰、扫描失败）都落 `gen-{N}/candidates/{candidate_id}.json`，含 `candidate`、`report`（若有）、`decision`、`reason`、`error_code`。
- **FR-07-13 候选代码安全执行**：新 harness 使用前必须依次通过 `guard.gate()`（MOD-02 静态扫描：AST 规则/禁用符号/依赖检查）与 `guard.load_harness_tools()`（在子进程 worker 中加载，Spec B-20）；扫描失败 → 直接 reject，`E_EVOLVE_SCAN_FAILED`，**不进冒烟、不进 heldout**；受限加载失败 → 立即回滚 `parent_version`，`E_EVOLVE_LOAD_FAILED`。
- **FR-07-14 代际报告**：每 `gen-{N}` 写 `report.json`，键严格等于 Spec §4.5、缺失用 `null`、**不新增顶层键**；多候选时记录最终接受者（或最后尝试者），其余写 `candidates/` 明细；**FR-07-15** 空候选/全淘汰也必须落盘（`decision="reject"` + 对应错误码），保证「每次接受/拒绝都有 `report.json` 支撑」（Spec §8.4）。

## 5. 非功能需求（NFR-07-x：确定性/性能/可复现/可观测）

- **NFR-07-1 确定性**：随机性只来自显式 seed（`evolve.seed`，由 MOD-09 从 `run_id` 派生）；禁 `random.*`/`time`/`uuid4`，遍历用 `sorted()`。同 `(run_id, generation, seed)` 重跑，`candidate_id` 序列与 `report.json` 逐字节相同（`created_at`/`ts` 由 MOD-09 注入，可用 `--freeze-time` 冻结以支持 G3）。
- **NFR-07-2 安全（INV-4/INV-5）**：候选代码永不 `exec` 于主进程；受限加载由 MOD-02 提供；proposer 上下文白名单由 FR-07-3 代码级强制；heldout seed 值出现在 `harness/`、`runs/*/reflections/` 或 memory 库中即为 P0。
- **NFR-07-3 可复现**：每次决策可由 `report.json` + `candidates/*.json` + `diff.patch` + `version.json` 离线重建；`config_hash` 覆盖 evolve 配置（Spec §5）。
- **NFR-07-4 可观测**：只用 MOD-09 结构化事件（`evolve.propose|validate|scan|smoke|evaluate|decision|version|gate|cost`），一次调用 = 一行 JSONL，不自建日志格式。
- **NFR-07-5 成本**：
  - 候选共享同一 seed 集合只是**配对设计**，并不减少局数：单代评估局数 = `H + S + C·(S + H)`（parent 的 heldout 与冒烟 + 每个候选的冒烟与 heldout）。
  - **现行默认 C=1、S=2、H=40**（2026-10-06 D-1）时为 84 局评估；再加训练 K=30，每代 **114 局**（`E_gen = 30 + 2×42`），与 Spec §8.6 的 `E_gen` 一致（MOD-06 计算，parent 冒烟 2 局计入 S）。历史口径 C=2、S=5 时的 135 局 / 165 局已失效。
  - 本模块**不再单独设成本上限**。超出 `loop.max_episodes_per_generation` 时由 MOD-06 拒绝启动；本模块只按优先级截断候选数。
- **NFR-07-6 幂等可续跑**：`accept()`/`rollback()` 以 `(run_id, generation)` 为幂等键，重复调用返回首次结果，不产生第二个版本号。
- **NFR-07-7 失败保守**：裁判链路任何异常（MOD-08 抛错/报告缺字段/超时）→ 一律 reject 并保留 `parent_version`，记 `E_EVOLVE_JUDGE_FAILED`；演化失败不得中断 `play`。

## 6. 接口契约（真实 Python 签名 + JSON Schema）

### 6.1 核心 Python 签名（`dice_ratchet/evolve/`）

```python
# ---- proposer.py ----
@dataclass(frozen=True)
class Candidate:
    candidate_id: str                      # sha256(kind + diff + rationale)[:12]
    kind: Literal["prompt_patch", "tool_patch", "config_patch"]
    diff: str                              # unified diff，相对 harness/，路径禁 ../
    rationale: str                         # ≤300 字，必须引用 evidence 条目 id
    expected_effect: ExpectedEffect        # {"metric":str,"direction":"up"|"down","priority":1..3}；序列化为 [metric,direction,priority] 三元组省 token
    touched_files: tuple[str, ...]; diff_lines: int; gen_id: str
@dataclass(frozen=True)
class EvidenceBundle:
    bundle_id: str
    memory_items: tuple[dict, ...]          # MOD-05，split=train
    trajectory_summaries: tuple[dict, ...]  # 最近 N 条 train 轨迹摘要
    reflections: tuple[dict, ...]
    harness_snapshot: HarnessSnapshot       # version + 各文件 sha256
    source_tags: tuple[str, ...]            # ⊆ {"memory:train","traj:train","reflect:train","harness:current"}
def build_evidence_bundle(*, run_id: str, n_traj: int, cfg: EvolveConfig) -> EvidenceBundle: ...
def assert_no_holdout_leak(bundle: EvidenceBundle, *, cfg: EvolveConfig) -> None: ...
def render_proposer_prompt(bundle: EvidenceBundle, cfg: EvolveConfig) -> tuple[str, str]: ...  # (system, user)
def propose(bundle: EvidenceBundle, *, cfg: EvolveConfig, rng_seed: int) -> ProposalResult: ...
def validate_candidate(cand: Candidate, *, cfg: EvolveConfig) -> ValidationResult: ...
# ---- judge.py ----
def evaluate_candidate(cand: Candidate, parent: HarnessRef, *, cfg: EvolveConfig) -> CandidateReport: ...
def judge(cand_report: CandidateReport, parent_report: HeldoutReport, *, cfg: EvolveConfig) -> Verdict: ...
# ---- versioning.py ----
def stage_candidate(cand: Candidate, *, run_id: str, cfg: EvolveConfig) -> Path:  # staging 目录
def accept(cand: Candidate, verdict: Verdict, *, run_id: str, cfg: EvolveConfig) -> HarnessRef: ...
def rollback(to_version: int, *, run_id: str, reason: str, decided_by: str) -> HarnessRef: ...
def load_version(v: int) -> HarnessRef: ...
def build_decision_sections(gen: int, reports: list[CandidateReport], winner: CandidateReport | None, gate: GateState) -> dict: ...   # 只产出 candidates/decision/decided_by/reason
# ---- gate.py ----
def await_human(*, generation: int, run_id: str, diff_path: Path,
                report_path: Path, cfg: EvolveConfig) -> GateState: ...
def resolve_gate(*, generation: int, run_id: str, action: Literal["approve","rollback"],
                 reason: str | None) -> Verdict: ...
```

### 6.2 被依赖签名

```python
# 以下签名全部引用 Spec v1.3 §4.7，本模块不重复定义
# MOD-02
def gate(harness_dir: Path) -> GateReport: ...            # status: "pass"|"fail", violations[{code,file,line,symbol}], ast_digest
def load_harness_tools(harness_dir: Path) -> ToolWorker: ...   # 常驻子进程；失败抛 E_EVOLVE_LOAD_FAILED
# MOD-08
def evaluate_arm(harness_dir: Path, *, split: str, basket: str, memory_version: int, arm: str, run_id: str) -> ArmResult: ...
def compare_paired(a: ArmResult, b: ArmResult) -> PairedTest: ...   # {method, metric, p, delta, significant, direction:"up"|"down"|"flat", n_pairs}
def evaluate_generation(gen: int, parent: ArmResult, winner: ArmResult | None, *, run_id: str) -> dict: ...  # report 指标段
```

### 6.3 report.json（引用唯一 schema）

schema 为 `schemas/generation_report.schema.json`（Spec v1.3 §4.5），本模块不维护副本。本模块只负责填写 `candidates[]`、`decision`、`decided_by`、`reason` 四段，与 MOD-08 返回的指标段合并后，交给 MOD-09 `write_json_atomic` 落盘：

- `generation ≥ 1`（`gen-000` 是 v0 基线，由 MOD-06 调用 MOD-08 生成）。
- `verdict` 的枚举以 MOD-08 为准：`real_gain|null|overfit_suspect|regress|insufficient`。

### 6.4 harness 目录布局（Spec §4.4 + 本模块扩展）

```
harness/                     # 当前生效工件（MOD-04 只读）
  version.json               # {"version","parent","git_commit","created_at","reason","accepted_by"}
  prompts/decision.md  prompts/reflection.md  config.json   # evolvable_keys 内键可被改
  tools/*.py                 # @harness_tool 纯函数，rank_actions 签名冻结
  versions/v{N}/             # 每次 accept 的不可变快照：version.json + diff.patch + prompts/ tools/ config.json
runs/<run_id>/generations/gen-{N}/    # report.json  gate.json  decision.jsonl + candidates/{id}.json + patches/{id}.patch
```

**已裁决（Spec v1.4 B-29/B-32/B-33）**：`harness/versions/v{N}/` 只放不可变快照（`version.json` + `diff.patch` + 全量工件）；`candidates/`、`patches/`、`gate.json` 放在 `runs/<run_id>/generations/gen-{NNN}/`；HITL 挂起以退出码 0 结束，超时 24h 保守回滚；`version.json.base_model` 预留给 MOD-10，M0–M3 恒为 `null`。

### 6.5 提案输入与补丁应用的硬口径（v0.6 新增；F37–F41）

闭环在 2026-10-09 首次实跑时，**提案→补丁**这一段连续暴露五个缺陷；本节把它们固化成口径。

**① 提案的输出预算按调用方给（F37，Spec B-47）**：`_call_generate_json` 传
`max_tokens=_PROPOSE_MAX_TOKENS`（**2048**；实测候选需 ~500）。**不得**改为抬高全局
`inference.max_tokens`——那会让 `mock`（4096）与 8B（8192）臂的 `guard_context` 余量崩掉
（实测 71 例 `E_LLM_CONTEXT_OVERFLOW`）。

**② harness 段必须内联「可改文件」的正文（F38，Spec B-48）**：候选是 unified diff，hunk
上下文必须与目标文件**逐字符一致**；只给 `sha256` 时模型对没见过的文件写 diff ⇒ 必然
`E_EVOLVE_PATCH_INVALID`。`HarnessSnapshot.contents` 承载正文（schema →
`evolve-harness-snapshot-1.1.0`），只含 `_is_allowed_path` 白名单内的文件、逐文件 20 KB 上限
（超限只留摘要，**不截断**——截断会让上下文失真）。范围界定：`prompts/decision.md` 本身是
**给 agent 的模板**（含 `{{task}}`/`{{observation}}` …），故 PRD §11-4 的「无未替换 `{{...}}`」
只适用**正文块之外**的区域。

**③ 补丁应用容忍 offset、不容忍 fuzz（F39，Spec B-49）**：`_apply_hunks` 在 ±200 行内按
**逐字**匹配原文块（上下文行 + 删除行）来定位落点，声明行号只是起点。实测：模型产出的 hunk
声明 `@@ -44,6 +44,10 @@`，其上下文逐字存在于第 53–56 行（**偏移 +9**）——抄对了原文，只是
不会数行号；原实现只认声明行号 ⇒ 每个真实候选都被判无效。歧义（同一距离两侧都命中）即拒绝，
**绝不**用 fuzz（忽略上下文差异）去凑。

**④ prompt 必须列出校验器强制的规则（F40）**：`config_patch` 只能改 `evolve.evolvable_keys`
声明的点分键——该清单**必须出现在 prompt 里**（否则模型只能盲猜，实测首轮即
`config_key_not_evolvable` 作废）；`rationale` 上限同时写在 prompt（硬上限 + 建议值留 20 字符
余量）与候选 JSON Schema 的 `maxLength`，且**校验器直接取 schema 的 `maxLength`**
（`_RATIONALE_MAX_CHARS`），三者不再各自写字面量。

**⑤ 冒烟臂的 `episode_key` 必须合法（F41，Spec B-50）**：冒烟臂标签是 `parent-smoke` /
`cand-{id}-smoke`，MOD-09 的 `episode_key` 登记模式必须允许 `-smoke` 后缀——否则冒烟局**每一行**
轨迹都失败、该局 dropped（轨迹文件 0 行）⇒ `n_dropped/n = 1.0` ⇒ `low_confidence` ⇒
`E_EVAL_INSUFFICIENT_N` ⇒ `smoke_fast_reject` ⇒ **每个候选在冒烟关即死，本模块永远产不出新版本**。

### 6.6 accept / rollback 的失败自清与失败口径（v0.7 新增；F47-C2 / F47-D）

**① 失败不得留下本次新建的快照（F47-C2，Spec B-57）**：`accept()` 在建完不可变快照
（`harness/versions/v{N}/`）**之后**才做 git 提交；提交失败时原先只 `_restore()` 恢复**工作区**，
而 `_rollback_files` 有意跳过 `versions/`（保护不可变快照）⇒ 本次刚建的那一个被留下。
`rollback()` 同形（`new_snap`）。修法：`_discard_new_snapshot(created_snap)`——**只删调用方明确
记录为「本次新建」的路径**，历史快照与回滚目标（`rollback()` 的 `snap` 是**只读目标**）绝不删。

**残留的实际危害（2026-10-10 更正开发期的一处推断）**：`next_version = max(指针, 现有版本目录)+1`，
所以残留目录**不会**让后续 accept 抛 `VersionConflictError`（它会跳号），真实危害有两条，均已
用用例钉住：**① 顶掉一个版本号**（版本序列出现永远填不上的空洞）；**② 留下一个可被
`load_version` / `rollback` 取用的「幻影版本」**——它没有账本记录、`git_commit` 为空，是对
「不可变已采纳版本」语义的伪造。

**② 版本冲突也必须失败保守**：`except VersionConflictError` 原先直接 `raise`（不回滚工作区），
而此前 `apply_unified_diff` 已经把候选写进生效工件 ⇒ 冲突会留下「已改内容 + 旧指针」的静默错位。
改为先 `_restore()` 再抛。

**③ 基础设施失败 ≠ 裁判拒绝（F47-D，Spec B-58）**：accept/rollback 抛 `InfraError`
（如 `E_EVOLVE_GIT_FAILED`）时记 `decision="error_reject"`（沿用 MOD-06 `_call_evolve` 的既有词汇），
并加入 MOD-08 的 `VERDICT_DECISIONS`（重算台账不得覆盖）。**原先报告是自相矛盾的**：
`_finish_report` 写 `decision="reject"`，而 `build_decision_sections()` 只看 gate 状态——此时
gate 仍是 `approved` ⇒ **`report.json` 写 `decision="accept"`，磁盘上却没有新版本**（幻影 accept）。
实测探针（修复前）：`outcome.decision='reject'`、`error_code='E_EVOLVE_GIT_FAILED'`、
`report.decision='accept'`、`versions=['v0']`。修法：`_build_report()` 在 `result.decision ==
"error_reject"` 时以 result 为准覆写 `decision`/`reason`，使报告与 `evolve.decision` 事件同口径。

**④ 提交前门禁的位置**：`commit_harness()` 内部的 `worktree_clean_check()` 只是**兜底**——它
位于整代花费之后。真正的「演化前门禁」由 MOD-06 在 `loop run` 起跑处调用（MOD-06 §9.3、
MOD-09 FR-09-20）。本模块保留该兜底检查，并保证它失败时不留 staged 残留（MOD-09 FR-09-14）。

**护栏**：`tests/unit/evolve/test_evolve_versioning.py::
test_accept_git_failure_leaves_no_snapshot_and_can_retry`（含「残留 → 跳号 + 幻影可 load」反例
自检与「清理后重试成功」正例）、`::test_rollback_git_failure_leaves_no_snapshot`、
`tests/unit/evolve/test_evolve_generation.py::test_f47_accept_infra_failure_is_recorded_as_error_reject`
（含 `build_decision_sections` 的 gate 分支反例自检）。修复前：前两条分别断言
`['v0','v1'] == ['v0']`、`['v0','v1','v2'] == ['v0','v1']` 失败；第三条
`assert 'reject' == 'error_reject'` 失败。

### 6.7 每代评估的检查点与复用（v0.8 新增；F42-A+B，Spec B-60/B-61）

**为什么必须做**：MOD-06 的 resume 单元原先只有**整代**（`generations/gen-NNN/{report,decision}.json`
不在就把 phase 重置为 playing **重跑整代**），而一代 84 局评估实测 **≈3.75 h / ≈¥47**
（`runs/m2-s32b-smoke-c1-medium6` 的 gen-1）——中断即**再付一次**。引擎早就有长跑能力
（`run_eval` 的 `resume_results` / `on_result`，M1 验收 F18/F19/F20），缺的是「中间那层」：
`evaluate_arm` 不转发它们（修复前签名：`TypeError: evaluate_arm() got an unexpected keyword
argument 'resume_results'`）。

**三层检查点**

1. **臂归档**（`evals/harness.py::evaluate_arm`）：每个臂落
   `metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json` =
   `ArmResult.as_dict()` + `provenance` + **逐局明细**。`provenance` 字段：
   `generation / split / basket / arm / harness_version / memory_version / n_seeds /
   n_dropped / config_hash / seeds_hash / protocol_id / episode_config_hash / created_at`。
2. **复用规则（fail-closed）**：
   - `provenance` 在 `ARM_PROVENANCE_KEYS` 上**逐字段全等**（**不含** `created_at`）且归档覆盖
     完整 seed 列表 → **整臂复用**：**一局都不跑**，发 `evals.arm_reused`；
   - 只覆盖一部分 → 已完成的局并入 `resume_results`，**只跑缺的**；每完成一局即重写归档
     （原子写），所以再挂一次也只丢当前局；
   - 任一字段不一致 / 归档损坏 / run 目录不存在 → **不复用**（照常重算）。改了 harness 版本、
     记忆版本、篮子、局型（`episode_config`）或 seed 区间，都必然走到这一支。
3. **提案检查点**（`evolve/checkpoint.py`）：**评估之前**把 `ProposalResult` 落
   `generations/gen{gen:03d}/proposals/proposal.json`，连同**输入指纹**
   （`run_id` / `generation` / `propose_seed` / `harness_version` / `memory_version` /
   `prompt_digest`）。resume 时重算指纹：一致 → **复用同一 `candidate_id` 与 diff**、
   **0 次 proposer 调用**（发 `evolve.proposal_reused`）；不一致 / 版本不符 / 损坏 → 照常重新提案。

**为什么提案也要检查点**：API 提案**不可复现**（注册表标注 `api_supports_seed=false`，实测
「字节相同的请求 9 次返回 9 个不同响应」）⇒ 不落检查点则 resume 时候选 id 必然变，而
`metrics/arms/` 的路径含 `cand-<id>` ⇒ 候选臂归档必然 miss，能省的只有 parent 侧。

**fingerprint 的诚实边界**：只覆盖**输入**，不含模型权重（proposer 客户端不向 MOD-07 暴露模型
指纹）；`harness_snapshot` 本身是 prompt 的一部分，因此 **accept 之后**的检查点天然不再命中
（那时 parent 已前进，重新提案才是正确行为）。指纹由 `proposer.proposal_inputs()` 与真正送进
LLM 的渲染**共用同一句**，避免「检查点按另一份参数算指纹」的静默漂移。

**`n_workers` / `n_rep` 必须按配置转发**（F42-C）：`CandidateEvaluator._evaluate_arm` 原先不传
它们 ⇒ 落到 `evaluate_arm` 的默认 `n_workers=1`（一代 84 局**串行**），而 gen-000 基线路径传的是
`cfg.evals.n_workers=2`。现在两条路径都按 `cfg.evals` 对齐；**不改任何配置值** ⇒ `config_hash` 不变。

**起点的跨代继承（D-M3-7，2026-10-14；Spec v2.24 B-81，取代 F57 的资格判据）**：`_inherited_parent_heldout`
按 **`play_hash`（`prompts/decision.md` + `config.json` + `tools/*.py`）** 扫更早的代，命中**同 play 面且真有测量**
的那一份就**继承**（**一次 `evaluate_arm` 都不调**），资格（任一不满足即重测，fail-closed）：
1. 某个更早代的 `report.json` 记的 `harness_play_hash` 与本代起点**逐字节相同**
   （**不能用版本号**：D2=A 让拒绝也推进版本号；旧 run 缺该字段时回落 F57 的「accept + `harness_version` 相同」）；
2. 那个代**真的测过**它当时在役的 harness：`gen-000` 的基线臂，或某代 `decision="accept"` 的候选臂
   （reject / inherited 的代没有新测量可继承，继续往前找）；
3. 归档 `metrics/arms/gen{prev:03d}-heldout-*-{arm}.json` 能被 `harness.restore_arm_from_archive` 还原且
   逐局 seed **覆盖本代篮子**（篮子变了就重测）。

**裁决「继承谁」的记账**：每代 `report.json` 的 `heldout.source ∈ {measured, inherited:gen-00X}`、
`harness_play_hash` = 该代**结束时在役** harness 的 play 面指纹。**每代只测候选**（起点不重跑）⇒
每 cycle 局数 **114 → 74**（K=30 + 2+2 冒烟 + 40 候选 heldout）。

**冒烟仍按本代 seed 重测**（冒烟 seed 取本代训练块末尾、逐代不同，而候选冒烟跑在本代 seed 上 ⇒ FR-07-7 的「同预算」
要求 parent 冒烟落在同一批 seed；2 局 ≈¥1.7 不值得为它破坏配对前提）。
**落盘（D-M3-7 修订）**：`heldout` 段 = **本代结束时在役那份 harness 的测量**——被拒 ⇒ 起点那份
（`inherited:gen-00X`，**不新增 heldout 台账行**，同 F48 的 `train.n == 0` 不写行）；**被接受 ⇒ 那个候选自己的臂**
（`source="measured"`，**写**台账行）。F57 的「恒取 parent」已废止：它让「接受了一代」在台账里永远不可见，
判据 1 的「held-out 单调不降」会退化成拿起点与起点比的恒真命题。
`evolve.parent_inherit` 事件记录命中/失败；被拒时另写 `harness/dead_ends.md`（D1=A：只给提案方看，
**禁止候选改动**、渲染处脱敏）并**轻量推进版本号**（D2=A：`v_{N+1} = v_N + 死路记录`，有指针/commit、
无快照/无 tag）。
**为什么值得做**：不只是省 ≈20 局 / ≈¥17/代——`C1` 是 API 臂（`api_supports_seed=false`），同一份 harness 同一批 seed
两次测量实测 `0.15` vs `0.10`（5pp = 本 run 的噪声底）⇒ 重测不是「更准」，是**引入不可复现的抖动**。

**中断语义**：`_parent_arms` 让 `KeyboardInterrupt` / `SystemExit` **穿透**（`except Exception`
本就不捕获它们，这里显式写明契约）。Ctrl-C 是最常见的打断方式；把它折成「保守 reject」会写出
`report.json`，让 MOD-06 把该代记成**已完成**，整代已评估结果白扔。

**端到端验收（实测，全部本地替身、0 成本）**：一代在 parent heldout 第 1 局之后 Ctrl-C →
resume → proposer **0 次调用**、parent heldout 已归档的局**一局都没重跑**、该代跑完。

## 7. 数据模型与存储

| 对象 | 落盘位置 | 关键字段 | 生命周期 |
|---|---|---|---|
| `Candidate` / `CandidateReport` | `candidates/{id}.json` | `candidate_id,kind,diff,rationale,expected_effect,touched_files,diff_lines,gen_id`；同文件 `report` 键：`smoke{win_rate,illegal_rate,episodes}`、`heldout:HeldoutReport\|null`、`paired`、`elapsed_s`、`error_code` | 随 run 永久（≥ 全部代际，Spec §8.4） |
| `HeldoutReport` | `metrics/heldout-gen{N}-{id}.json`（MOD-08 写、本模块只读回链） | MOD-08 定义 | 永久 |
| `Verdict` | `report.json` 的 `decision/decided_by/reason` + `decision.jsonl` | `decision,decided_by,reason,condition_trace` | 永久 |
| `version.json` | `harness/version.json` 与 `harness/versions/v{N}/version.json` | `version,parent,git_commit,created_at,reason,accepted_by` | 除当前指针外不可变 |
| `GateState` / 事件流 | `gen-{N}/gate.json`；`runs/<run_id>/events.jsonl`（MOD-09 落地） | `generation,harness_version,parent_version,diff_path,report_path,status,expires_at,resolved_by,reason`；`evolve.*` | 永久 |

原则：`*.patch` 为文本，进 git 便于审计；`metrics/*.json` 存储策略（git LFS 或 `runs/` gitignore）由 MOD-09 决定，本模块不引入存储决策。

## 8. 关键流程（伪代码或时序）

### 8.1 单代流程（伪代码）

```python
def run_generation(run_id, gen, cfg) -> dict:
    parent = versioning.load_version(current_version())
    mem_v = memory.snapshot(version=gen)                                                  # 冻结记忆快照，parent 与候选共用
    bundle = build_evidence_bundle(run_id=run_id, n_traj=cfg.evolve.n_traj, cfg=cfg)      # FR-07-2/3
    prop = propose(bundle, cfg=cfg, rng_seed=derive_seed(run_id, gen, "propose"))
    if not prop.candidates:
        return finish(gen, parent_ho=None, reports=[], winner=None, error=prop.error_code)
    smoke_seeds = evals.seeds.take("train", generation=gen)[-5:]
    parent_ho = evals.evaluate_arm(parent.dir, split="heldout", basket="H1", memory_version=mem_v, arm="parent", run_id=run_id)
    parent_smoke = evals.evaluate_arm(parent.dir, split="train", basket=smoke_seeds, memory_version=mem_v, arm="parent", run_id=run_id)
    reports, winner = [], None
    for cand in sorted(prop.candidates, key=lambda c: (-c.expected_effect.priority, c.candidate_id)):
        staging = versioning.stage_candidate(cand, run_id=run_id, cfg=cfg)
        if validate_candidate(cand, cfg=cfg).status == "fail":                    # FR-07-4
            reports.append(reject(cand, "E_EVOLVE_PATCH_INVALID")); continue
        if guard.gate(staging).status == "fail":                                 # FR-07-13
            reports.append(reject(cand, "E_EVOLVE_SCAN_FAILED")); continue
        arm = f"cand-{cand.candidate_id}"
        smoke = evals.evaluate_arm(staging, split="train", basket=smoke_seeds, memory_version=mem_v, arm=arm, run_id=run_id)
        if smoke_fast_reject(smoke, baseline=parent_smoke, cfg=cfg):             # FR-07-6
            reports.append(reject(cand, "E_EVOLVE_SMOKE_FAILED")); continue
        ho = evals.evaluate_arm(staging, split="heldout", basket="H1", memory_version=mem_v, arm=arm, run_id=run_id)
        paired = evals.compare_paired(ho, parent_ho)
        cr = CandidateReport(cand, smoke, ho, paired); cr.verdict = judge(cr, parent_ho, cfg=cfg)
        reports.append(cr)
        if cr.verdict.decision == "accept" and (winner is None or better(cr, winner)): winner = cr
    return finish(gen, parent_ho, reports, winner)

def better(a: CandidateReport, b: CandidateReport) -> bool:   # §8.2：C2 delta 大者优先，并列时 candidate_id 小者优先
    return (a.paired.delta, -ord_id(a)) > (b.paired.delta, -ord_id(b))

def finish(gen, parent_ho, reports, winner, error=None) -> dict:
    gate_state = gate.await_human(generation=gen, winner=winner, cfg=cfg) if winner else None
    if gate_state and gate_state.status == "approved": versioning.accept(winner.cand, winner.verdict, run_id=run_id, cfg=cfg)
    elif gate_state and gate_state.status == "rejected": versioning.rollback(parent.version, run_id=run_id, reason=gate_state.reason, decided_by="human")
    report = {**evals.evaluate_generation(gen, parent_ho, winner and winner.heldout, run_id=run_id),
              **build_decision_sections(gen, reports, winner, gate_state)}          # pending 时 decision="pending"
    infra.io.write_json_atomic(paths.generation(gen) / "report.json", report)        # MOD-09 校验 schema + sha256
    return report
```

### 8.2 接受判定表（短路求值，逐条写 `condition_trace`）

| # | 条件 | 表达式 | 不满足时 reason / 错误码 |
|---|---|---|---|
| C0 | 前置完备 | `report.heldout.n >= cfg.evolve.min_heldout_n` 且关键字段非 `null` | `C0:incomplete_report` / `E_EVOLVE_JUDGE_FAILED` |
| C1 | 主指标不降 | `report.win_rate >= parent.win_rate - cfg.evolve.win_rate_tol`（**`tol=0.05`**，2026-10-06 D-3 由 0.0 上调；n=40 的 win_rate 粒度是 1/40=0.025，tol<0.025 等于"少赢 1 局即拒"，会拒掉约一半真正等价的候选） | `C1:win_rate {v}<{p}` / `E_EVOLVE_NO_GAIN` |
| C2 | 至少一指标显著提升 | `paired.significant == True and paired.direction == "up"`，`metric ∈ {win_rate, mean_stages_cleared, mean_hearts_left}`，方法与口径全部由 MOD-08 `compare_paired` 决定（胜率用 McNemar，连续量用配对 bootstrap B=10000）。**2026-10-06 D-3：H=40 下 `win_rate` 只能判 ≥15pp（见 MOD-08 §8.2），M3 的 4–5pp 结论必须由 `mean_stages_cleared` 承担** | `C2:no_significant_gain` / `E_EVOLVE_NO_GAIN` |
| C3 | 增益真实性 | `train.win_rate - heldout.win_rate <= cfg.evolve.gap_threshold`（0.15，对齐 Spec §4.5 `gap_threshold`） | `C3:gap {gap}>{thr}` / `E_EVOLVE_OVERFIT_SUSPECT` |
| C4 | 无坍缩 | `diversity.collapse_flag == False and diversity.entropy_slope >= cfg.evolve.min_entropy_slope`（−0.01；Spec 示例 −0.004 属通过区间） | `C4:collapse` / `E_EVOLVE_COLLAPSE` |
| C5 | 合法性与健壮性 | **三轴同构的相对门 + 绝对 backstop**（2026-10-11 D-M3-2 裁决 E，Spec v2.8 B-64）：`illegal_rate ≤ min(parent.illegal_rate + evolve.illegal_tol(0.05), evolve.max_illegal_rate(0.35))` 且 `fallback_rate ≤ min(parent.fallback_rate + evolve.fallback_tol(0.05), evolve.max_fallback_rate(0.25))` 且 `truncation_rate ≤ min(parent.truncation_rate + evolve.truncation_tol(0.05), evolve.truncation_backstop(0.60))`。修复前三轴中前两轴是绝对门，对 `C1` 实测值（0.0318 / 0.0017）松 11×/147× ⇒ **非约束门**（Spec v1.3 P-25 结案：不再用回合数，因为 GAME 的回合上限针对单场战斗而非整局。历史：2026-10-07 按 §3.1 实测把 illegal/fallback 由 0.05/0.10 重标定为 0.35/0.25；2026-10-09 truncation 由绝对门 0.15 改相对门 B-44；2026-10-11 illegal/fallback 也改相对门并给 truncation 补 backstop，见 §8.2.1/§8.2.2） | `C5:illegal/fallback/truncation` / `E_EVOLVE_ILLEGAL_RATE` |

`accept ⟺ C0∧C1∧C2∧C3∧C4∧C5`。多候选全通过时取 `(C2 delta 最大, candidate_id 最小)` 为 winner，其余记 `reject/reason="superseded"`（不计失败、无错误码）。

### 8.2.1 C5 的实测标定与 M3 收紧路径（2026-10-07）

**实测依据**（[M2 §3.1 实测报告](../reviews/2026-10-07-M2-S31-full-agent基线实测.md)，run `runs/m2-s31-full-baseline/`）：full `PolicyAgent` 在 H1 子集 seed 2000–2009（n=10，eval 模式）测得 `illegal_rate` **0.3016**、`fallback_rate` **0.2146**、`truncation_rate` **0.10**，与 `fixed_prompt` 基线（0.3021 / 0.1810 / 0.0）同量级。原阈值 0.05/0.10/0.05 分别被超限 **6.03× / 2.15× / 2.00×**，即 **parent 与候选都会被 C5 reject**，闭环形式上跑通、实则一个候选都留不下。

**① 当时的「先松开」口径（Spec B-40）**：三项上限取「实测上界 + 余量」= **0.35 / 0.25 / 0.15**。**已知副作用**：C5 由此对 M2/M3 的候选不再构成约束（任何候选都能过）；这是为 M2 判据「闭环跑通 ≥1 轮可判定的演化」做的刻意取舍，诊断价值临时让位于此。
> **⚠️ 2026-10-09 修订**：本条的第三项（`max_truncation_rate = 0.15`）**已作废并删除**——它按本地 8B 标定，而 M2 主力臂换成了 `C1`（DeepSeek），`hard × 6` 实测 0.30 会让 C5 **全拒候选**（缺陷 F28）。现状：`illegal`/`fallback` 保留 0.35/0.25 绝对门，`truncation` 改相对门 `tol=0.05`。见 §8.2.2。

**② M3 收紧路径 = 相对门（Spec B-41），不是把数值改回 0.05**：C5 改为锚定 parent 的残差门——

```
C5' ⟺ candidate.illegal_rate    ≤ parent.illegal_rate    + evolve.illegal_tol
   ∧ candidate.fallback_rate    ≤ parent.fallback_rate    + evolve.fallback_tol
   ∧ candidate.truncation_rate  ≤ parent.truncation_rate  + evolve.truncation_tol
```

parent 与候选在**同一 held-out 篮子、同一预算**下评估（§8.2 C0/C1 已经这样跑），因此差值是可比的配对量。设计要点：

- **保留诊断力**：绝对门在真实 8B 上不成立（见①），但「候选把非法率/兜底率显著推高」仍必须拦住 —— 这正是残差门能抓的。
- **`tol` 待标定**：`tol` 不能凭猜。§3.2 冒烟产出第一批 train/heldout 配对后，按 parent 侧 `illegal_rate`/`fallback_rate` 的**同一代内抖动**定 `tol`（起点 0.05），并同步登记 Spec §8.6。
- **实现归属**：原计划 M3 新增 `evolve.illegal_tol` / `evolve.fallback_tol` / `evolve.truncation_tol` 三个键。**2026-10-09 已提前落地其中一项**：`truncation` 的相对门已在 M2 落地（`evolve.truncation_tol`，见 §8.2.2），`illegal_tol` / `fallback_tol` 仍归 M3。
- **保守兜底**：残差门之外**保留**一个宽松绝对上限作 backstop，避免 parent 自身已经很高时残差门被无限放宽。**2026-10-09 的边界**：`illegal` / `fallback` 保留绝对门（0.35 / 0.25）本身就充当了 backstop；`truncation` 按用户裁决**不设绝对 backstop**（相对门的对称放宽已在 §8.2.2 的「已知放宽」里写明，M3 标定 `tol` 时再评估）。

> **✅ 2026-10-11 标定现状（Spec v2.15 / B-71/B-72）——C4 的输入与 C5 的 `tol` 都还没标定。**
> ① **C4 的上游判据 `c3` 修好了，但阈值没标**：`c3`（「分布冻结 + 胜率跳变」）此前**从未触发**（实测 17 个归档代的 `jsd_kind` 全空；三个独立原因见 Spec B-71——生成侧不落 kind 分布、`evaluate_generation` 的 pair 错位、`confirm_generations=2` 对 `c3` 结构上不可达）。现已改为**单代即触发**并落 `metrics/kind_distribution/`。**但** `0.010` / `0.05` 这两个阈值**仍未经基线标定**。
> ② **C5 的 `tol=0.05` 能算 MDE、标不出标定值**：`illegal` 的 0.05 ≈ **1,392 个步级事件**（≈48 个 se，分母 27,836 尝试/代）、`fallback` ≈ **451 个**（≈115 个 se，9,021 决策）、`truncation` ≈ **2 局**（单臂 40 局）⇒ **truncation 是约束轴**，三轴不可互换。标定（按同代内抖动）需要 ≥2 代配对实测，而**现有归档只有 1 个 run 有 2 代** ⇒ 结构上标不了。
> ③ **M3 报告必须写明**：C4/C5 的阈值是**「起点值 + 待回填」**，不是实测标定值。工具：`scripts/run_m3_calibration.sh --free`（零成本部分）。
> **✅ 2026-10-11 落地（D-M3-2 裁决 E，Spec v2.8 / B-64）**：本节 ② 的 M3 收紧路径**已完成**，
> 且比原计划多了一项——`truncation` 也补上了绝对 backstop。C5 的最终形态（**三轴同构**）：
>
> ```
> C5 ⟺ candidate.illegal_rate    ≤ min(parent.illegal_rate    + evolve.illegal_tol,    evolve.max_illegal_rate)
>    ∧ candidate.fallback_rate    ≤ min(parent.fallback_rate    + evolve.fallback_tol,   evolve.max_fallback_rate)
>    ∧ candidate.truncation_rate  ≤ min(parent.truncation_rate  + evolve.truncation_tol, evolve.truncation_backstop)
> ```
>
> - **为什么必须收紧**（实测，不是推断）：`illegal`/`fallback` 的绝对门对 `C1` 的实测值
>   （0.0318 / 0.0017）松 **11× / 147×** ⇒ 实际是**非约束门**。后果不是「少抓一个坏候选」，
>   而是**判据 2 的归因结论不成立**——候选完全可以靠推高非法率换 `win_rate` 而照样过闸。
> - **`tol` 的语义按轴分列、不可互换**（这是本次最容易写错的一处）：
>   `illegal` 是**步级**（分母 = 成功步 + 非法尝试，`C1` hard×6 约 12k）⇒ se ≈ 0.0017；
>   `fallback` 是**步级**（分母 = 决策点，约 3.7k）⇒ se ≈ 0.0030；
>   `truncation` 是**局级**（粒度 1/40 = 0.025）。
>   故 0.05 对前两轴远大于批内噪声（优先防「真·退化」，**不承担**「检微小漂移」），
>   对 truncation 则恰好是 2 局噪声（与 `win_rate_tol` 同一依据 D-3）。
> - **新增/变更的配置键**：新增 `evolve.illegal_tol=0.05`、`evolve.fallback_tol=0.05`、
>   `evolve.truncation_backstop=0.60`；`evolve.max_illegal_rate`（0.35）与
>   `evolve.max_fallback_rate`（0.25）**保留但语义降为 backstop**（键不变 ⇒ 老配置仍可加载）。
> - **fail-closed 语义随之扩展**：三轴都是相对门 ⇒ parent 的 `illegal_rate` / `fallback_rate`
>   与 `truncation_rate` 一样进 **C0 前置完备性检查**；缺任一项记 `C0:incomplete_report` 并 reject，
>   **不静默跳门**（跳门 = 该轴失去诊断力，正是本次要修的病）。
> - **可审计性**：`condition_trace` 的 C5 条目带 `value`（候选值 + 三个 parent 值）与
>   `threshold`（三个**生效门限** + 三个 `tol` + 三个 backstop），并随
>   `CandidateReport.as_dict()` → `candidates/{id}.json.verdict` 落盘。
> - **⚠️ 诚实边界**：`tol=0.05` 是「与既有 `win_rate_tol`/`truncation_tol` 同口径 + 远大于
>   步级噪声」定的**起点值**，**不是 §3.2 的实测标定值**（标定需一批配对实测，未付费、未做）；
>   标定后只改数值，不改本节的门结构。

### 8.2.2 ✅ C5 是「臂相关」的：换臂曾让绝对门静默锁死闭环（2026-10-09 复检发现 F28 → **同日本节内处置**）

> **本节的结论：C5 三个阈值不是被测对象的属性，而是「标定臂 × 局型」的属性。**
> 2026-10-07 的标定（§8.2.1 的①）用的是**本地 MLX 8B**；而 M2 闭环主力臂已在 2026-10-09 由用户裁决为 **`C1`（三角色全 `deepseek-flash`）**（Spec v2.0 B-43）。
> 用当前臂的实测值重算，**`hard × 6` 上 C5 会 reject 每一个候选**。

**复现（真实 `judge()` + 真实 `load_config()`，C5 = 0.35/0.25/0.15）**：构造一个 C1–C4 全过、`mean_stages_cleared` 显著 `+0.40` 的候选，只改 `heldout` 段的来源：

| 指标来源（n=40 形态） | illegal | fallback | truncation | `judge()` 判定 |
|---|---|---|---|---|
| 本地 8B full-agent（`m2-s31-full-baseline`，**标定依据**） | 0.3016 | 0.2146 | 0.10 | ✅ `accept`（六条全过） |
| **`C0` DeepSeek · `hard × 6`**（`r-c0-hard6-h1n10`） | 0.0291 | 0.0008 | **0.30** | ❌ `reject` / `C5:illegal/fallback/truncation` / `E_EVOLVE_ILLEGAL_RATE` |
| `C0` DeepSeek · `medium × 6`（`r-c0-medium6-h1n10`） | 0.0316 | 0.0000 | 0.00 | ✅ `accept` |

**三个可验证的事实**：

1. **illegal / fallback 两个门对 DeepSeek 臂已松了约 10 倍**（0.0291 vs 门 0.35、0.0008 vs 门 0.25）⇒ C5 由「非约束门」进一步退化为**完全无约束门**；B-40 的「先松开」是为 8B 的 0.30/0.21 松的。
2. **truncation 门反向变紧**：`hard × 6` 上 **3/10 局**撞内核单场战斗回合上限（`src/game/kernel/combat.js:460` → `state.truncationReason='E_KERNEL_TURN_LIMIT'`，`maxTurnsPerBattle=60`），实测 `truncation_rate = 0.30 > 0.15`。被截断的局**计 lose 但不算 dropped**（`n_dropped=0`），所以不会被 `n_dropped` 那一路拦住。该值是**局型 × 策略强度**的合成值（同 run 的 `heuristic` 为 0.00）。
3. 同一个候选在 8B 数字上 `accept`、在 `C1` 数字上 `reject` ⇒ **C5 判的不是候选退化，而是「用了哪条臂」**。结果正是本 PRD 与检查单 §3.1 要防的「闭环形式上跑通、实则一个候选都留不下」——**它没有被消除，只是从 illegal 轴搬到了 truncation 轴**。

**闭环为什么「静默」**：`reject` 不是错误——MOD-06 `maybe_run_generation` 对接受与拒绝**同样**执行 `generation += 1` 并回到 `playing`（退出码 0，FR-06-7）；MOD-07 照常写 `generations/gen-XXX/{report,decision}.json` 并 emit `evolve.decision(decision="reject")`。因此一个全拒的闭环会**一路跑完所有代数、退出码 0、日志无异常**，但 `harness/versions/` 永不出现新版本、`artifact_hash` 永不变——直到有人去查 decision 记录才会发现。

**✅ 处置（已裁决，2026-10-09，Spec v2.0 B-44）：用户选 ② + ③。**

- **③（门结构）**：**C5 只保留 `illegal` / `fallback` 的绝对门**（0.35 / 0.25），**`truncation` 改为相对门**
  `candidate.truncation_rate ≤ parent.truncation_rate + cfg.evolve.truncation_tol`（**`truncation_tol = 0.05`**，
  即 n=40 的 2 局，与 `win_rate_tol` 同一依据 D-3）。**绝对门 `max_truncation_rate` 已从 `EvolveConfig` 与
  `configs/default.yaml` 中删除**；实现见 `evolve/judge.py`（常量 `_C5_TRUNCATION_TOL`）。**M3 若需再加宽松
  绝对上限作 backstop，由 §8.2.1 的 tol 标定一并决定**——本次不设（用户裁决「只对 illegal+fallback 设绝对门」）。
- **配套的 fail-closed 语义**：相对门需要 `parent.truncation_rate`，因此它和 `win_rate` 一样进 **C0 前置完备性检查**——
  parent 的 `heldout` 缺 `truncation_rate` 时记 `C0:incomplete_report` 并 reject，**不静默跳过门**
  （跳过会让 C5 对 truncation 彻底失去诊断力）。回归护栏见 `tests/unit/evolve/test_evolve_judge.py`
  的 `test_c5_truncation_is_relative_to_parent_not_absolute` / `test_c5_truncation_relative_boundary` /
  `test_c0_rejects_when_parent_lacks_truncation_rate`。
- **②（局型）**：**bring-up / §3.2 冒烟改用 `medium × 6`**（该局型上 DeepSeek 臂实测 `truncation_rate = 0.00`，
  且 `win_rate` 有方差：80% vs 启发式 70%）。写法仍是 run 级 override：
  `DICERATCHET_EXPERIMENT__ARM=C1 DICERATCHET_ENV__EPISODE__DIFFICULTY=medium <cmd>`。默认局仍是 `hard × 6`（M3 主口径）。
- **已知放宽（诚实边界）**：相对门是**对称放宽**——parent 自身截断率很高时，候选只要不比它差就能过。例如
  parent = 0.90 时候选 0.60 也放行（见参数化用例的 `(0.60, 0.90, True)`）。兜底靠 C0–C4（尤其 C2 要求显著提升）
  与 M3 的 tol 标定；本轮不引入绝对 backstop。已在测试里显式钉住这一行为，避免被误读为缺陷修复。

### 8.3 HITL 时序 / 8.4 回滚流程

```
loop(MOD-06) --accept?--> gate(MOD-07): hitl? → 写 gate.json(status=pending) → loop 退出 exit 0
loop run --resume --> CLI(MOD-09) 读 gate.json --> approve/rollback --> gate: accept()/rollback() --> git commit + tag harness-v{N}
```

`rollback(v)`：① 校验 `v` 存在且 `v <= current`；② `harness/` 内容替换为 `versions/v/`；③ 写 `version.json`（`version=v`，`parent=current_version`，新 `git_commit`，`reason`，`accepted_by=decided_by`）；④ 生成 `revert.patch`（current → v）；⑤ git commit，并打 tag `harness-v{new}`；⑥ 写 `gen-{N}/report.json`，`decision="rollback"`。

## 9. 配置项（键名、类型、默认值、含义）

| 键（`configs/default.yaml`） | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `evolve.n_candidates` | int | **1** | 每代候选数（2026-10-06 D-1：2 → 1，上限 3；**必须 == `loop.n_candidates`**，`config.py` 有自动校验。C=1 会削弱「多候选择优」，由 §3.2 冒烟确认仍能产出可判定的演化） |
| `evolve.n_traj` / `evolve.memory_top_k` | int | 20 / 12 | 证据束最近 train 轨迹摘要条数 / MOD-05 检索条数 |
| `evolve.max_diff_lines` / `evolve.max_diff_chars` | int | 40 / 4000 | 单候选改动面硬上限 |
| `evolve.smoke_n` | int | **2** | 冒烟局数，取本代训练块末尾（FR-07-6）。2026-10-06 D-1：5 → 2；**必须与 `loop.smoke_n` 同步改**（该处无自动校验）。v0.1 的 `train_eval_seeds` 已删除：`train.win_rate` 按 MOD-08 的滚动窗口口径，1030–1069 与第 2～3 代训练块重叠 |
| `evolve.heldout_seeds` | str | 由 MOD-09 注册表注入 | heldout 评估组（40 局），**不得写入任何 prompt** |
| `evolve.min_heldout_n` / `evolve.win_rate_tol` | int / float | 20 / **0.05** | C0 门槛 / C1 允许下降幅度（2026-10-06 D-3：0.0 → 0.05） |
| `evolve.alpha` | float | 0.05 | C2 显著性水平（方法归 MOD-08） |
| `evolve.gap_threshold` / `evolve.min_entropy_slope` | float | 0.15 / −0.01 | C3 `win_rate_gap` 阈值 / C4 熵斜率下限 |
| `evolve.max_illegal_rate` / `evolve.max_fallback_rate` | float | **0.35 / 0.25** | C5 的**绝对 backstop**（2026-10-07 §3.1 实测标定：0.05/0.10 → 本值，当时是绝对门；**2026-10-11 D-M3-2 起语义降为 backstop**——主门改为相对门，本值只用于堵「parent 自身很差时跟着烂」，键名保留 ⇒ 老配置仍可加载） |
| `evolve.illegal_tol` / `evolve.fallback_tol` | float | **0.05 / 0.05** | C5 的 **illegal / fallback 相对门**容差：`candidate ≤ min(parent + tol, backstop)`。**2026-10-11 新增**（D-M3-2 裁决 E）。两轴的 `*_rate` 都是**步级**（illegal 分母 = 成功步+非法尝试 ≈12k ⇒ se≈0.0017；fallback 分母 = 决策点 ≈3.7k ⇒ se≈0.0030），故 0.05 远大于批内噪声 ⇒ 优先防「真·退化」，**不承担**「检微小漂移」。⚠️ **诚实边界**：0.05 是起点值，**不是 §3.2 的实测标定值**（标定未做） |
| `evolve.truncation_tol` | float | **0.05** | C5 的 **truncation 相对门**容差：`candidate.truncation_rate ≤ min(parent.truncation_rate + tol, evolve.truncation_backstop)`（0.05 = **局级**粒度 1/40 的 2 局，同 `win_rate_tol` 依据）。**2026-10-09 新增，取代已删除的 `evolve.max_truncation_rate`**（Spec v2.0 B-44 裁决 ②+③；原绝对门 0.15 按本地 8B 标定，换 `C1` 后 `hard×6` 实测 0.30 会全拒候选 —— 缺陷 F28，见 §8.2.2） |
| `evolve.truncation_backstop` | float | **0.60** | C5 的 **truncation 绝对 backstop**：**2026-10-11 新增**（D-M3-2 裁决 E）。原设计（B-44）刻意不设，代价是 §8.2.2 记录的已知放宽——parent=0.90 时候选 0.60 也能过；本值把上限钉在 0.60。取 0.60 的理由：C1 hard×6 实测 truncation 0.30（撞内核 `maxTurnsPerBattle=60`），0.60 = 实测值的 2× 余量，且远低于「几乎全截断」的 0.90 |
| `evolve.hitl` / `evolve.gate_timeout_hours` | bool / int | true / 24 | 人在环门开关 / 超时 → 保守回滚 |
| `evolve.propose_max_retries` | int | 2 | JSON-schema 失败重试（在 MOD-03 `generate_json` 之上再包一层）；成本上限改由 MOD-06 `loop.max_episodes_per_generation` 统一控制 |
| `evolve.evolvable_keys` | list[str] | `["retrieval.top_k","sampling.temperature","prompt.show_intent"]` | `config_patch` 白名单键 |
| `evolve.tool_import_allowlist` | list[str] | `["math","re","json","itertools","functools","typing","dataclasses"]` | `tool_patch` 允许的顶层 import |

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀）

| 错误码 | 触发 | 处理 | 消耗 heldout 预算 |
|---|---|---|---|
| `E_EVOLVE_PROPOSE_FAILED` / `E_EVOLVE_NO_CANDIDATE` | 生成失败/重试耗尽/JSON 不合 schema；或生成成功但 0 候选（含全被去重） | 该代 `decision="reject"`，写报告，不开评估；`reason="no_candidate"` | 否 |
| `E_EVAL_INV5_LEAK`（v0.1 的 `E_EVOLVE_HOLDOUT_LEAK` 并入，Spec §8.3） | 证据束含非 train split 或 heldout seed 字面量 | **致命**：中止本代 + 写 `audit/` 告警；连续 2 次中止 run（INV-5） | 否 |
| `E_EVOLVE_PATCH_INVALID` | `validate_candidate` 失败（行数/文件/签名/依赖/白名单） | 该候选 reject，继续下一候选 | 否 |
| `E_EVOLVE_SCAN_FAILED` | MOD-02 `guard.gate` 返回 `fail` | 该候选 reject，`violations` 落 `candidates/{id}.json` | 否 |
| `E_EVOLVE_LOAD_FAILED` | `guard.load_harness_tools` 抛错（worker 启动或加载失败） | 已 accept 则立即回滚 `parent_version`，否则 reject | 是（已发生） |
| `E_EVOLVE_SMOKE_FAILED` | 冒烟快速淘汰命中 / 冒烟异常 | reject，不进 heldout | 否（仅 5 局 train） |
| `E_EVOLVE_EVAL_FAILED` | MOD-08 超时/异常 | reject（保守），保留当前 harness | 是 |
| `E_EVOLVE_NO_GAIN` / `E_EVOLVE_ILLEGAL_RATE` | C1、C2 或 C5 失败 | reject（C5 记 `illegal_rate`/`fallback_rate`/`truncation_rate` 明细） | 是 |
| `E_EVOLVE_OVERFIT_SUSPECT` / `E_EVOLVE_COLLAPSE` | C3 失败 / C4 失败 | reject + `gain_realism.verdict="overfit_suspect"` / reject + `diversity` 明细与告警 | 是 |
| `E_EVOLVE_JUDGE_FAILED` | 判定链路异常/报告缺字段 | reject（Spec §8.5 保守拒绝） | 视情况 |
| `E_EVOLVE_GATE_TIMEOUT` | HITL 超时 | 回滚 `parent_version`，`reason="hitl_timeout"` | 否 |
| `E_EVOLVE_VERSION_CONFLICT` | 版本号已存在/指针不一致/git 冲突 | 中止本代 accept，人工介入（不自动 merge） | 否 |
| `E_EVOLVE_GIT_FAILED` | git commit/branch 失败 | 丢弃未提交 accept，`version.json` 不变，`decision="reject"` | 否 |

**降级总则**：任何不确定 → 拒绝候选（保留 parent），绝不"先进再修"。

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例）

**单测（`tests/unit/evolve/`）** —— 给定-当-则：

1. 给定 3 个候选 diff，当 `validate_candidate`，则超过 `max_diff_lines=40` 者 `status="fail"` 且 `reason` 含 `diff_lines`；给定 `tool_patch` 把签名改为 `def rank_actions(obs, candidates, memory, extra)`，则拒绝 `E_EVOLVE_PATCH_INVALID`（FR-07-4）。
2. 给定含 `import numpy` / `import requests` / `urllib.request` 的 `tool_patch`，或 `config_patch` 修改 `evolvable_keys` 外的键（如 `sampling.seed`），当校验，则拒绝。
3. 给定证据束混入一条 `split="heldout"` 轨迹摘要，当 `build_evidence_bundle`，则抛 `E_EVAL_INV5_LEAK`；给定反思文本出现 heldout seed `2013` 字面量，当 `assert_no_holdout_leak`，则抛 `E_EVAL_INV5_LEAK`（出现 `20130`/`x2013y` **不**误报，正则 `(?<!\d)2013(?!\d)`；v0.1 示例用的 1042 属于 train 区间，已更正）。
4. 给定 `render_proposer_prompt` 产出，当扫描，则占位符 ⊆ `{observation, memory, harness, task}` 且不含 `heldout`/`test` 子串（INV-3 回归）；给定同一 `(run_id, gen, seed)` 与固定 stub LLM，当两次 `propose`，则 `candidate_id` 列表逐字节相同（NFR-07-1）。
5. 判定表表驱动 12 例：C1 降 0.01 → reject(`E_EVOLVE_NO_GAIN`)；C2 `significant=False` → reject；C3 `gap=0.19>0.15` → reject(`overfit_suspect`)；C4 `collapse_flag=True` → reject；C5 `illegal_rate=0.11` → reject；六项全真 → accept；并覆盖短路顺序。
6. 给定已存在 `(run_id, gen)`，当重复 `accept`，则返回首次结果且 `harness/versions/` 无新增目录（幂等）；给定 current=5，当 `rollback(2)`，则 `harness/` 的 sha256 等于 `versions/v2/`，`version.json.parent==5`，产生 `revert.patch`；给定 `evolve.hitl=false`，当 accept，则不写 `gate.json` 且 `decided_by="auto"`；给定 `hitl=true` 且 gate 未响应至超时，则 `decision="rollback"`、`reason="hitl_timeout"`。

**集成（`tests/integration/evolve/`）**：① 给定 stub LLM + 缩配（`smoke_seeds=train:1000-1001`、`heldout_seeds=heldout:2000-2005`），当跑 `run_generation`，则产出 `report.json` 且通过 §6.3 Schema（G5 round-trip）；② 给定 3 代 run（`--hitl`）且每代 `evolve approve`，则 `harness/version.json.version == 3` 且 `harness/v1..v3` 三个 commit 存在（G6）；③ 给定 MOD-08 抛 `E_EVAL_TIMEOUT`，当 `evaluate_candidate`，则候选 reject 且当前 harness 不变（NFR-07-7）；④ 给定 run 全目录，当 `audit leak --run-id <run_id>`，则在 MOD-07 产出物上 0 告警（G4）。

**安全用例（恶意候选被拒，`tests/unit/evolve/test_malicious_candidates.py`；全部期望 0 局 heldout 消耗）**

| 用例 | 恶意候选内容 | 期望 |
|---|---|---|
| MAL-1 网络 / MAL-2 文件写 / MAL-3 子进程 | `urllib.request.urlopen(...)`；`open("harness/seed_backup.json","w")`；`subprocess.run(["git","push"])` | 均 `E_EVOLVE_SCAN_FAILED`（INV-4） |
| MAL-4 env 内窥 / MAL-5 预读作弊 / MAL-7 签名规避 | `from dice_ratchet.env.rng import RNG`；diff 写入 heldout seed 列表或 `if seed == 1042:` 常量分支；`rank_actions = lambda *a: a[1]` 绕过 `@harness_tool` | 均 `E_EVOLVE_SCAN_FAILED`（INV-1 + 注册表完整性/AST 校验） |
| MAL-6 历史重写 / MAL-8 预算耗尽 | `prompt_patch` 改 `version.json` 或 `versions/`；单代请求 40 个候选 | `E_EVOLVE_PATCH_INVALID`（路径白名单）；按优先级截断至 3、`cost_cap` 未破（NFR-07-5） |

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

**对齐 Spec §9 质量门**：G1 —— `proposer.py`/`judge.py`/`versioning.py`（判定表与 diff 计数为纯函数）覆盖率 ≥80%，`gate.py` ≥60%（含 IO）；G3 —— 同 `run_id` + `--freeze-time` 重跑，`candidate_id` 序列与 `report.json`（除 `created_at`）逐字节相同；G4 —— 自测 run 上 `audit leak` 0 告警，MAL-5 蜜罐式候选必须被 `guard.gate` 抓到；G5 —— `report.json`/`version.json`/`gate.json`/`Candidate` 四个 schema 有 round-trip 测试；G6 —— 本节命令可直接复制执行成功。

**对齐 M2**：`python -m dice_ratchet loop run --generations 1 --k 30 --hitl` 闭环跑通 ≥1 轮演化，产出 `diff.patch` + `report.json` + `gate.json`，`evolve approve` 后 `harness/version.json.version == 1`。
**对齐 M3**：≥3 代演化，heldout 主指标单调不降，**并且至少有 1 次 accept**（否则「每代都拒绝」也能满足单调不降，属于退化通过），报告写明接受率（计划 §6 验收 1）；至少 1 个指标相对基线可归因提升且 `ablation.no_self_edit` 差异被记录（验收 2）；`win_rate_gap < gap_threshold` 且 `collapse_flag == False`（验收 3）。

> **✅ 2026-10-11（D-M3-4 裁决 A，Spec v2.10 / B-66）：M3 的 HITL 模式与「样本资格」口径已澄清。**
> §14 风险④ 原文写「`--hitl=false` 跑批量实验（标 `decided_by="auto"`，**不计入 M3 验收样本**）」，
> 而本节又要求「≥1 次 accept」——两条**互相矛盾**：全自动跑时 accept 出自 auto、按原文不计入样本；
> 改用 `--hitl` 则每代有人工延迟，且 24 h 无响应会 `hitl_timeout` **保守回滚**（一代白跑 ≈4.8 h / ≈¥100）。
> **裁决 A（记录在案）**：
> ① **M3 实验跑 `--hitl=false` 全程自动**（`evolve.hitl=false`；可复现、无人在环干预）；
> ② **样本资格改为**「`decided_by="auto"` **+ `condition_trace` 完整**（C0–C5 六项齐全，每项带
>    `passed`/`value`/`threshold`）」——满足则该 auto 决策**计入** M3 验收样本；缺 `condition_trace`
>    的 auto 决策**不计入**；
> ③ 报告必须**显式记录「本轮无人工确认」**（不得让读者误以为有 HITL 复核）；
> ④ §14 风险④ 的「不计入 M3 验收样本」**收窄为**只适用于「`condition_trace` 不完整的 auto 决策」，
>    不再适用于全部 auto 决策。
> **依据**：M2 准出 run（`runs/m2-s32b-smoke-c1-medium6`）**事实就是走 auto 路径**——归档 `report.json` 为
> `decided_by="auto"`、`decision="reject"`、`reason="C2:no_significant_gain"`，且
> `candidates/{id}.json.verdict.condition_trace` 完整 ⇒ 本裁决是**追认既有做法**，不是引入新风险；
> 而选 `--hitl` 的代价不对称（一代白跑 ≈¥100 + 长跑必须守夜）。
> **⚠️ 诚实边界**：auto accept 的说服力弱于人工确认——M3 报告**不得**把 auto 决策描述为「经人工复核」。
**专属 DoD**：任意 reject 都有 `report.json`；任意 accept 都有 `version.json` + `diff.patch` + git commit；任意恶意候选都在静态扫描阶段被挡且 0 局 heldout 消耗。

## 13. 指标与埋点

| 事件（MOD-09 `events.jsonl`） | 字段 |
|---|---|
| `evolve.propose` | `run_id, gen, n_candidates, n_deduped, kinds[], prompt_tokens, latency_ms, error_code` |
| `evolve.validate` / `evolve.scan` | `candidate_id, status, violations[], diff_lines, touched_files[]` / `ast_digest, third_party_imports[], disallowed_calls[]` |
| `evolve.smoke` / `evolve.evaluate` | `candidate_id, n, win_rate, illegal_rate, elapsed_s, fast_reject_reason` / `win_rate_ci95, mean_stages_cleared, mean_hearts_left, delta_vs_parent, p_value, episodes_spent` |
| `evolve.decision` / `evolve.version` / `evolve.gate` / `evolve.cost` | `gen, candidate_id, decision, decided_by, condition_trace[], reason, error_code` / `from_version, to_version, git_commit, branch` / `status(pending\|approved\|rolled_back\|timeout), waited_s, decided_by` / `episodes_spent, episodes_cap, candidates_evaluated` |

看板：候选接受率（按 kind）、冒烟淘汰率、heldout 预算消耗/代、`win_rate_gap` 与 `entropy_slope` 时间序列、HITL 平均等待时长、`E_EVOLVE_SCAN_FAILED` 占比。**红线**：`E_EVAL_INV5_LEAK` 任意一次；`collapse_flag` 连续 2 代为真。

## 14. 依赖与风险

| 依赖 | 内容 | 缺失影响 |
|---|---|---|
| MOD-06 loop | 触发周期、传入 `run_id`/`gen`、断点续跑 | 无法端到端跑，单测用 stub 驱动 |
| MOD-08 evals | `evaluate_arm`、`compare_paired`、`evaluate_generation`（含 `collapse_flag`、`entropy_slope`） | C2/C3/C4 不可判 → 全部保守 reject |
| MOD-09 infra | config/events/git_ops/run_id/guards | accept 与报告落盘不可用 |
| MOD-02 sandbox | `guard.gate` + `guard.load_harness_tools` | **阻塞上线**：无扫描即违反 INV-4（M0 已交付） |
| MOD-03 inference / MOD-04 policy | `generate_json()` 结构化输出 / `harness_tool` 注册表与 prompt 渲染契约 | `propose` 不可用 → `E_EVOLVE_PROPOSE_FAILED`；新 harness 无法被加载验证 |

**风险**：① 8B 牌力弱导致判定表全拒 → M2 先用宽松档（`alpha=0.10`、`win_rate_tol=0.05`；默认档已是 `win_rate_tol=0.05`，宽松档只把 `alpha` 放宽到 0.10，见 2026-10-06 D-3）跑通管线，M3 收紧到默认值并给出收紧前后对比；② heldout 预算被候选数放大 → 冒烟前置 + `cost_cap` 硬截断（2026-10-06 D-1：`C=1`、`S=2`、`H=40`、`E_gen=114 ≤ loop.max_episodes_per_generation=120`）；③ diff 质量低 → `propose_max_retries=2` + 去重，`propose` 失败率 >50% 时回看 MOD-04 prompt 契约；④ HITL 拖慢实验节奏 → `--hitl=false` 跑批量实验（标 `decided_by="auto"`，不计入 M3 验收样本——**2026-10-11 D-M3-4 裁决 A 修订**：此限定**收窄为**只适用于「`condition_trace` 不完整的 auto 决策」；完整的 auto 决策（C0–C5 六项齐全，每项带 `passed`/`value`/`threshold`）**计入** M3 验收样本，M3 实验跑 `--hitl=false` 全程自动并在报告显式记录「本轮无人工确认」（理由与依据见 §12 的裁决块））；⑤ git 分支污染主仓 → 只经 MOD-09 `git_ops`，禁裸 `subprocess`，`E_EVOLVE_GIT_FAILED` 时不动 `version.json`；⑥ **（2026-10-06 D-2/D-3）`C=1` 削弱"多候选择优"信号、且 `win_rate` 在 H=40 上只能判 ≥15pp** → 必须在 M2 §3.2 冒烟里确认 C=1 仍能产出 ≥1 轮可判定的演化（判据落在 `mean_stages_cleared` 的配对显著性上，而非 win_rate 点估计），否则回头调 D-1。

## 15. 里程碑与工作量估算（人日）

| 阶段 | 交付 | 人日 |
|---|---|---|
| M2-a/b | `proposer.py`（schema/白名单/去重/`validate_candidate`）+ `versioning.py`（staging/accept/rollback/versions/git）+ 单测 | 4.5 |
| M2-c/d | `gate.py`（pending 落盘/CLI 对接/超时回滚 + loop 续跑联调）+ `judge.py`（判定表/报告落盘/Schema round-trip） | 2.5 |
| M3-a/b | `evaluate_candidate` 编排（冒烟/heldout/显著性/成本上限）+ 安全用例 MAL-1..8 与 `audit leak` 联动 | 2.5 |
| 缓冲 | 联调、stub 造数、报告模板与文档命令核对 | 0.5 |
| **合计** | | **10.0** |

关键路径：依赖 MOD-06/08/09，M2 初版需 MOD-06 与 MOD-08 的 heldout 协议先就绪；本模块可与 MOD-08 指标实现并行（按 §6.2 签名先写 mock）。

## 16. 开放问题

1. `compare_paired` 的方法与最小样本量归 MOD-08：40 局能否检出 +5% 胜率提升？若不能，C2 是否退化为"CI 不重叠 + 效应量 ≥ 0.05"？
2. ~~`versions/v{N}/` 全量拷贝 vs 只存 `diff.patch`~~：已裁决为全量拷贝 + `diff.patch`（B-29）；多候选同时通过时只接受 1 个（当前决策）是否低估单代增益，是否允许"1 个 canonical + 1 个 config_patch"？
3. Tie 语义：`win_rate` 相同但 `mean_stages_cleared` 显著提升时，C1 当前按"不降"通过——是否应额外要求次级指标不得恶化？`E_EVAL_INV5_LEAK` 处置是否过重（替代：告警 + 该代作废 + 移交人工）？
4. MOD-10 介入后 harness 与模型权重需联合版本化：`base_model` 字段已预留（B-33），启用后计入 `artifact_hash`；`harness/` 整体入 git（含 `versions/`）还是只提交生效工件 + `diff.patch`（影响 `rollback` 的 git 语义）？

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v0.1 | 2026-09-30 | 首版 |
| v0.2 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：MOD-08/MOD-02 调用改为 Spec §4.7 签名（P1-3/16）；显著性方法归 MOD-08（P1-4）；report 改为引用唯一 schema，本模块只填写决策段（P1-5/6）；删除 `train_eval_seeds`（与训练块重叠，P1-7）；成本算术改正、候选数默认 2（P1-8、Spec §8.6）；parent 与候选共用冻结记忆快照；冒烟 seed 改为本代训练块末尾；补充 `better()` 定义；C5 改为 illegal/fallback/truncation；git 改为只打 tag `harness-v{N}`（P1-15）；M3 追加「≥1 次 accept」防止退化通过 |
| v0.3 | 2026-09-30 17:49 | P-17/P-26/P-27 裁决回填为 Spec B-29/B-32/B-33；开放问题 2、4 部分结案 |
| v0.4 | 2026-10-07 | **C5 按实测标定 + M3 相对门设计（Spec v1.8 B-40/B-41；依据 [M2 §3.1 实测报告](../reviews/2026-10-07-M2-S31-full-agent基线实测.md)）**。① §8.2 C5 行与 §9 配置表：`max_illegal_rate`/`max_fallback_rate`/`max_truncation_rate` 0.05/0.10/0.05 → **0.35/0.25/0.15**（full `PolicyAgent` 实测 0.3016/0.2146/0.10，原门超限 6.03×/2.15×/2.00×，会 reject 每个候选；写明是「先松开、M3 再收紧」并声明副作用）。② 新增 **§8.2.1**：M3 的收紧路径是把 C5 从绝对门改为**相对门** `candidate ≤ parent + tol`，含 `tol` 标定方式、M3 新增的三个配置键、以及保留宽松绝对上限作 backstop。③ 顺带补齐 D-1 的表格滞后：§9 的 `evolve.n_candidates` 2→**1**、`evolve.smoke_n` 5→**2**（与 `configs/default.yaml` 及 `config.py` 对齐）。④ 版本头对齐 Spec v1.8 |
| v0.5 | 2026-10-09 | **处置缺陷 F28：C5 的 truncation 改相对门（Spec v2.0 B-44，用户裁决 ②+③）**。起因：§8.2.1 的阈值是按**本地 8B** 标定的，而 M2 闭环主力臂同日被裁决为 **`C1`（三角色全 `deepseek-flash`）**（Spec B-43）——用当前臂实测值 + **真实 `judge()`** 复算，`hard × 6` 的 `truncation_rate = 0.30 > 门 0.15` ⇒ **C5 会 reject 每一个候选**（`E_EVOLVE_ILLEGAL_RATE`），而 MOD-06 对接受与拒绝同样 `generation += 1` 且退出码 0 ⇒ **闭环静默锁死**（`harness/versions/` 永不出现新版本）。① **新增 §8.2.2**：记录复现表（8B 数字 `accept` vs DeepSeek 数字 `reject`）、三个可验证事实、以及「闭环为什么静默」。② **③（门结构）**：**C5 = `illegal`/`fallback` 绝对门（0.35/0.25）+ `truncation` 相对门** `candidate ≤ parent + truncation_tol`；**删除绝对门 `max_truncation_rate`**，新增 `evolve.truncation_tol = 0.05`（n=40 的 2 局，同 `win_rate_tol` 依据）。③ **新增 fail-closed 语义**：相对门需要 `parent.truncation_rate`，故它与 `win_rate` 一样进 **C0 前置完备性检查**——parent 缺该字段时记 `C0:incomplete_report` 并 reject，**不静默跳门**。④ **②（局型）**：bring-up / §3.2 冒烟改用 **`medium × 6`**（DeepSeek 臂实测 truncation = 0.00，`win_rate` 有方差 80% vs 启发式 70%）；默认局仍 `hard × 6`。⑤ **同步更新的落点**：`evolve/judge.py`（`_C5_TRUNCATION_TOL` + C0 parent 校验）、`infra/config.py` 与 `configs/default.yaml`（键替换）、`tests/unit/evolve/test_evolve_judge.py`（3 组新回归护栏 + 默认值护栏含 `not hasattr(max_truncation_rate)`）、Spec §8.6、本 HTML 版与检查单 §8。⑥ **诚实边界**：相对门是对称放宽（parent 自身很高时候选不比它差就放行，如 parent=0.90 时候选 0.60 也过），本轮**不设绝对 backstop**；该行为已被参数化用例显式钉住，M3 标定 `tol` 时再评估 |
| v0.5 | 2026-10-09 | **新增 §8.2.2：C5 是「臂相关」的 → 登记缺陷 F28 与待裁决的处置**（对齐 Spec v2.0 B-44）。起因：C5 三阈值（§8.2.1 的①）是 2026-10-07 按**本地 8B** 标定的，而 M2 闭环主力臂已于 2026-10-09 由用户裁决为 **`C1`（三角色全 `deepseek-flash`）**（Spec B-43）。用当前臂实测值 + **真实 `judge()`** 复算：`hard × 6` 的 `truncation_rate = 0.30 > 门 0.15` ⇒ **C5 会 reject 每一个候选**（`E_EVOLVE_ILLEGAL_RATE`）；同时 illegal/fallback 反向松了约 10×。① 结论：**C5 的阈值不是被测对象的属性，而是「标定臂 × 局型」的属性**；§3.1 要防的「静默锁死闭环」风险未被消除，只是从 illegal 轴搬到 truncation 轴（`hard × 6` 上 3/10 局撞内核 `maxTurnsPerBattle=60` → `E_KERNEL_TURN_LIMIT`，且被截断的局计 lose 不算 dropped）。② 补记「闭环为什么静默」：`reject` 不是错误，MOD-06 对接受与拒绝同样 `generation += 1` 并回到 `playing`（退出码 0），MOD-07 照常写 decision 记录 ⇒ 全拒的闭环会跑完全部代数、退出码 0、日志无异常，但 `harness/versions/` 永不出现新版本。③ 列出三个处置候选（按当前臂重标 / bring-up 改用 medium×6 / **truncation 并入相对门**）并写明**裁决前不得宣称 M2 准入完成** |
| v0.7 | 2026-10-10 | **新增 §6.6：accept/rollback 的失败自清与失败口径（F47-C2/D，对齐 Spec v2.5 B-57/B-58）**。① **失败自清**：`accept()` 在建完快照后失败（git 提交失败）时，原先只 `_restore()` 恢复工作区，`_rollback_files` 又会跳过 `versions/` ⇒ 本次新建的快照被留下；`rollback()` 同形。改为删掉**本次新建**的那一个（`_discard_new_snapshot(created_snap)`；回滚目标 `snap` 只读、绝不删）。**开发期更正**：残留的危害不是「后续 accept 抛 `VersionConflictError`」（`next_version = max(指针, 现有目录)+1` 会跳号），而是**顶掉一个版本号** + 留下**可被 `load_version`/`rollback` 取用的幻影版本**（无账本、`git_commit` 空）——两条都有反例用例。② **版本冲突也失败保守**：`except VersionConflictError` 原先直接 `raise`，而 diff 已写进生效工件 ⇒ 留下「已改内容 + 旧指针」；改为先 `_restore()`。③ **失败口径分离**：accept/rollback 抛 `InfraError` 时记 `decision="error_reject"`（加入 MOD-08 `VERDICT_DECISIONS`）。原先 `_finish_report` 写 `reject` 而 `build_decision_sections` 只看 gate（此时 `approved`）⇒ **report 写 `accept` 却没有新版本**（幻影 accept）。实测探针：`outcome.decision='reject'` / `report.decision='accept'` / `versions=['v0']`；修法是 `_build_report` 在 `error_reject` 时以 result 覆写 decision/reason。④ 明确 `commit_harness` 内部的门禁只是**兜底**（位于整代花费之后），真正的「演化前门禁」在 MOD-06 §9.3 / MOD-09 FR-09-20。 |
| v0.6 | 2026-10-10 | **新增 §6.5：提案输入与补丁应用的硬口径（F37–F41，对齐 Spec v2.3 B-47..B-50）**。本轮是**闭环首次真正跑通提案→补丁→冒烟→裁判**，五个缺陷依次暴露（全部只在「测试不走的参数组合」里，被 1700+ 条全绿用例掩护）：① **F37** 提案沿用 `inference.max_tokens=256` ⇒ 响应一律截断 → 报成 `E_LLM_SCHEMA_INVALID` → **0 候选**；改为按调用方给预算（2048），**否掉**全局抬高路线。② **F38** prompt 只给 harness 文件的 sha256、不给正文 ⇒ 模型对没见过的文件写 diff ⇒ `E_EVOLVE_PATCH_INVALID`；改为内联可改文件正文（`HarnessSnapshot.contents`，schema 1.1.0）。③ **F39** `_apply_hunks` 只认声明行号 ⇒ 上下文逐字正确但偏 +9 的真实候选被判无效；改为 ±200 行逐字定位（容忍 offset、不做 fuzz、歧义即拒）。④ **F40** `evolvable_keys` 从未进 prompt（`config_patch` 盲猜即作废）、rationale 上限只在 schema 的 `maxLength`（API 不强制）⇒ 改为 prompt 明列 + 上限取自 schema（单一事实来源）。⑤ **F41** 冒烟臂 `parent-smoke`/`cand-{id}-smoke` 不在 `episode_key` 登记模式里 ⇒ 冒烟局 0 行轨迹 ⇒ `E_EVAL_INSUFFICIENT_N` ⇒ **每个候选在冒烟关即死**；已三处同步（MOD-09 §6.2 / `infra/logging.py` / `schemas/trajectory_line.schema.json`）。**修好后的首个真实候选**走到完整裁判链并**被合法拒绝**（`C2:no_significant_gain`、`delta=0.0,p=1.0`）⇒ 据此**改写 §3.2 验收判据**为「机制性失败为零」（Spec B-51，用户裁决）。 |
| v0.11 | 2026-10-14 | **D-M3-7：起点的跨代继承改按 `play_hash`，并把代际测量改成「每代只测候选」；`heldout` 段的口径由「恒取 parent」改为「本代结束时在役那份 harness 的测量」（Spec v2.24 B-81；取代 F57 的资格判据与落盘口径）**。① **为什么换判据**：D2=A 让**拒绝也推进版本号**（`v_{N+1} = v_N + dead_ends`）⇒「版本相同 ⇔ 起点相同」不再成立；判据 1 的同起点豁免、继承资格、F66 的第二半（补丁是否真的改变打法）三处统一问 `play_hash`。② **为什么换落盘口径**：全拒的 run 里每一代都继承起点那份，若 `heldout` 恒取 parent 则台账永远只有一个数据点、`comparable_pairs` 恒 0 ⇒ 判据 1 **结构上不可满足**；接受一代必须留下新数字。③ **成本**：每 cycle 114 → 74 局（省 ≈40 局 / ≈¥21 / ≈1.7 h）。④ **D1=A/D2=A**：死路记录进提案 prompt（`HarnessSnapshot.context`，schema `1.2.0`；只读 + 禁止候选改动 + 渲染脱敏 + system prompt 第 11 条硬约束）；拒绝轻量推进版本号（无快照、无 tag、账本不带 `versions` 键、git 失败回滚指针）。⑤ **同批修 F64**（训练局标签 = 它服务的那一代；train 段取本代训练块并按 `arm="train"` 真过滤；不足即 `source="insufficient"`）与 **F69**（绘图 `yerr` 夹到 ≥0，辅助产物不得毁主产物）。⑥ **诚实边界**：被接受候选的测量成为下一代基线 ⇒ 含选择偏差（winner's curse），登记 **F70** 待裁决。**成本：¥0**。 |
| v0.10 | 2026-10-12 | **新增 §8.1 的 F57 规则：parent 测量跨代继承（Spec v2.20 B-78）**。① `accept()` 之后 parent 就是上一代被接受的候选，而它已被完整测过 ⇒ `_inherited_parent_arms` **继承**那批局，heldout **0 次 `evaluate_arm`**（省 ≈20 局 / ≈¥17 每代）。② 资格四条 fail-closed（accept / 版本相同 / 能推出候选臂 / 归档可还原且 seed 覆盖本代篮子）；任一不满足即重测。③ **冒烟仍按本代 seed 重测**（同预算要求同批 seed）。④ 继承的代**不新增 heldout 台账行**，只在 `report.json` 记 `heldout.source="inherited:gen-00X"`。⑤ 动机不只是钱：API 臂同 harness 两次测量差 5pp（本 run 噪声底）⇒ 重测引入不可复现抖动。⑥ 隐性陷阱（实测踩到）：资格判断**不能**写 `or -1`——`harness_version == 0` 是合法且 falsy 的值，会静默跳过 v0 上的可继承来源。 |
| v0.9 | 2026-10-11 | **C5 收紧为三轴同构的「相对门 + 绝对 backstop」（D-M3-2 裁决 E，对齐 Spec v2.8 B-64）**。① **现场（实测，不是推断）**：`illegal`/`fallback` 的绝对门（0.35 / 0.25）对 `C1` 实测值（0.0318 / 0.0017）松 **11× / 147×** ⇒ 实际是**非约束门**（与 M2 的「先松开」同一状态）。后果不是「少抓一个坏候选」，而是**判据 2 的归因结论不成立**——候选完全可以靠推高非法率换 `win_rate` 而照样过闸。② **门结构**：三轴统一为 `candidate ≤ min(parent + tol, backstop)`；新增 `evolve.illegal_tol=0.05` / `evolve.fallback_tol=0.05` / `evolve.truncation_backstop=0.60`，`max_illegal_rate`/`max_fallback_rate` **保留但语义降为 backstop**（键名不变 ⇒ 老配置仍可加载）。③ **堵住 §8.2.2 的已知放宽**：原设计（B-44）刻意不给 truncation 设绝对 backstop，代价是 parent=0.90 时候选 0.60 也能过；`truncation_backstop=0.60` 把它钉住（0.60 = C1 hard×6 实测 0.30 的 2× 余量）。④ **`tol` 语义按轴分列、不可互换**（最易写错处）：illegal 是**步级**（分母 = 成功步+非法尝试 ≈12k ⇒ se≈0.0017）、fallback 是**步级**（分母 = 决策点 ≈3.7k ⇒ se≈0.0030）、truncation 是**局级**（粒度 1/40 = 0.025）；故 0.05 对前两轴远大于批内噪声（防「真·退化」，不承担「检微小漂移」），对 truncation 恰好是 2 局噪声。⑤ **fail-closed 扩展**：三轴都是相对门 ⇒ parent 的 `illegal_rate`/`fallback_rate` 与 `truncation_rate` 一样进 **C0 前置完备性检查**，缺任一项记 `C0:incomplete_report` 并 reject，不静默跳门。⑥ **可审计性**：`condition_trace` 的 C5 条目带 `value`（候选值 + 三个 parent 值）与 `threshold`（三个**生效门限** + 三个 `tol` + 三个 backstop），随 `CandidateReport.as_dict()` → `candidates/{id}.json.verdict` 落盘。⑦ **护栏**：12 条新/改用例（相对门边界 ×6、illegal/fallback 相对性 ×2、backstop 封顶 ×2、truncation backstop ×2、C0 缺 parent 值 ×2、trace 序列化往返 ×1）；**全部先 `git stash` 掉源码证明修复前失败**（实测 **11 failed / 68 passed**，两处「修复前后同结论」的边界保留为对照），且都带反例自检（`0.30 ≤ 0.35` 必落在旧绝对门内、`0.601 ≤ 0.90+0.05` 必落在无 backstop 的相对门内）。⑧ **⚠️ 诚实边界**：`tol=0.05` 是「与既有 `win_rate_tol`/`truncation_tol` 同口径 + 远大于步级噪声」定的**起点值**，**不是 §3.2 的实测标定值**（标定需一批配对实测，未付费、未做）；标定后只改数值，不改门结构。⑨ **成本**：¥0 |
| v0.8 | 2026-10-11 | **新增 §6.7：每代评估的检查点与复用（F42-A+B+C，对齐 Spec v2.6 B-60/B-61）**。① **现场**：MOD-06 的 resume 单元只有整代（`_cycle_artifacts_exist`），中断即重跑整代——一代 84 局实测 ≈3.75 h / ≈¥47；而引擎早有 `run_eval(resume_results/on_result)`，只是 `evaluate_arm` 不转发（修复前签名 `TypeError: evaluate_arm() got an unexpected keyword argument 'resume_results'`）。② **臂归档**：`metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json` = `ArmResult.as_dict()` + provenance（13 字段）+ 逐局明细。③ **fail-closed 复用**：provenance 逐字段全等且覆盖完整 seed 列表 → 整臂复用（0 局 + `evals.arm_reused`）；部分覆盖 → 只跑缺的；任一不一致/损坏/无 run 目录 → 重算。④ **提案检查点**：评估前落 `generations/gen{gen:03d}/proposals/proposal.json` + 输入指纹（含 `prompt_digest`）；命中即同 candidate id、**0 次 proposer 调用**。理由：API 提案不可复现（`api_supports_seed=false`），不落检查点则 `cand-<id>` 归档必然 miss，只能省 parent 侧。⑤ **F42-C**：`CandidateEvaluator._evaluate_arm` 原先不传 `n_workers`（84 局串行，而 gen-000 基线传 2）⇒ 按 `cfg.evals` 对齐（不改配置 ⇒ `config_hash` 不变）。⑥ **中断语义**：`_parent_arms` 让 `KeyboardInterrupt`/`SystemExit` 穿透（折成保守 reject 会写出 report.json，让 loop 把该代记成已完成）。⑦ **顺带修**：`run_eval` 原先只把 `resume_results` 并进聚合字典、队列仍会重跑那些 seed；`str(None)=="None"` 让归档复用闸永远对不上。⑧ **端到端实测（0 成本替身）**：parent heldout 第 1 局后 Ctrl-C → resume → proposer 0 次调用、parent 已归档局一局未重跑、该代跑完。 |
