# MOD-07 evolve 技术 PRD

> **版本**：v0.5（2026-10-09 新增 §8.2.2：C5 的臂相关性 → 登记缺陷 F28 与待裁决的处置，对齐 Spec v2.0 B-44；v0.4 = 2026-10-07 按 Spec v1.8 回填 C5 实测标定与 M3 相对门设计，见 §8.2.1/§17）· **状态**：待评审 · **所属里程碑**：M2（propose/evaluate/versioning/HITL 初版）→ M3（回滚/消融对接/统计口径收紧）· **作者**：DiceRatchet 技术文档 · 日期：2026-10-07
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
- **FR-07-7 正式评估**：冒烟通过者在 `evolve.heldout_seeds`（默认 `heldout:2000-2039`，40 局）评估，**预算与当前版本完全相同**（同 seed 集合、同 `episode_config`、同采样参数，仅 `prompt_tokens` 允许差异并记录）；指标全部委托 MOD-08 `evaluate_arm()`；**parent 与候选必须使用同一个冻结的记忆快照** `memory_version`，所以 parent 每代都要重新评估（Spec §8.6）。MOD-07 不做任何统计运算。
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
| C5 | 合法性与健壮性 | `illegal_rate ≤ cfg.evolve.max_illegal_rate`（**0.35**）且 `fallback_rate ≤ cfg.evolve.max_fallback_rate`（**0.25**）且 `truncation_rate ≤ cfg.evolve.max_truncation_rate`（**0.15**）（Spec v1.3 P-25 结案；不再用回合数，因为 GAME 的回合上限针对单场战斗而非整局。**三个数值于 2026-10-07 按 Spec §3.1 实测由 0.05/0.10/0.05 重标定**，见下方 §8.2.1） | `C5:illegal/fallback/truncation` / `E_EVOLVE_ILLEGAL_RATE` |

`accept ⟺ C0∧C1∧C2∧C3∧C4∧C5`。多候选全通过时取 `(C2 delta 最大, candidate_id 最小)` 为 winner，其余记 `reject/reason="superseded"`（不计失败、无错误码）。

### 8.2.1 C5 的实测标定与 M3 收紧路径（2026-10-07）

**实测依据**（[M2 §3.1 实测报告](../../reviews/2026-10-07-M2-S31-full-agent基线实测.md)，run `runs/m2-s31-full-baseline/`）：full `PolicyAgent` 在 H1 子集 seed 2000–2009（n=10，eval 模式）测得 `illegal_rate` **0.3016**、`fallback_rate` **0.2146**、`truncation_rate` **0.10**，与 `fixed_prompt` 基线（0.3021 / 0.1810 / 0.0）同量级。原阈值 0.05/0.10/0.05 分别被超限 **6.03× / 2.15× / 2.00×**，即 **parent 与候选都会被 C5 reject**，闭环形式上跑通、实则一个候选都留不下。

**① 现行口径（先松开，Spec B-40）**：三项上限取「实测上界 + 余量」= **0.35 / 0.25 / 0.15**。**已知副作用**：C5 由此对 M2/M3 的候选不再构成约束（任何候选都能过）；这是为 M2 判据「闭环跑通 ≥1 轮可判定的演化」做的刻意取舍，诊断价值临时让位于此。

**② M3 收紧路径 = 相对门（Spec B-41），不是把数值改回 0.05**：C5 改为锚定 parent 的残差门——

```
C5' ⟺ candidate.illegal_rate    ≤ parent.illegal_rate    + evolve.illegal_tol
   ∧ candidate.fallback_rate    ≤ parent.fallback_rate    + evolve.fallback_tol
   ∧ candidate.truncation_rate  ≤ parent.truncation_rate  + evolve.truncation_tol
```

parent 与候选在**同一 held-out 篮子、同一预算**下评估（§8.2 C0/C1 已经这样跑），因此差值是可比的配对量。设计要点：

- **保留诊断力**：绝对门在真实 8B 上不成立（见①），但「候选把非法率/兜底率显著推高」仍必须拦住 —— 这正是残差门能抓的。
- **`tol` 待标定**：`tol` 不能凭猜。§3.2 冒烟产出第一批 train/heldout 配对后，按 parent 侧 `illegal_rate`/`fallback_rate` 的**同一代内抖动**定 `tol`（起点 0.05），并同步登记 Spec §8.6。
- **实现归属 M3**：新增 `evolve.illegal_tol` / `evolve.fallback_tol` / `evolve.truncation_tol` 三个键，C5 的求值从「绝对比较」改为「parent + tol」；本 PRD 的 §9 配置表与 Spec §8.6 同时在 M3 落地时更新。
- **保守兜底**：残差门之外**保留**一个宽松绝对上限（即①的 0.35/0.25/0.15）作为 backstop，避免 parent 自身已经很高时残差门被无限放宽。

### 8.2.2 🔴 C5 是「臂相关」的：换臂会让绝对门静默锁死闭环（2026-10-09 复检，缺陷 F28）

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

**处置候选（未裁决，Spec v2.0 B-44）**：① 按当前臂重标（`0.30` → 门 `0.35`，但会再次把 C5 变成非约束门）② bring-up 改用 `medium × 6`（实测 truncation = 0.00，且 API 臂在该局型上 `win_rate` 有方差）③ **C5 只保留 illegal + fallback 的绝对门，把 truncation 并入 §8.2.1 的相对门**（推荐方向：truncation 由局型与策略强度共同决定，本就不适合做跨臂的绝对门）。**在裁决前不得宣称 M2 准入完成**。

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
| `evolve.max_illegal_rate` / `evolve.max_fallback_rate` / `evolve.max_truncation_rate` | float | **0.35 / 0.25 / 0.15** | C5 上限（2026-10-07 §3.1 实测标定：0.05/0.10/0.05 → 本值，先松开；M3 由 §8.2.1 的相对门收紧）。**⚠️ 2026-10-09：本值随臂失效**——它们标的是本地 8B，而 M2 主力臂是 `C1`（DeepSeek）；`hard × 6` 会因 `truncation=0.30 > 0.15` 全拒，见 §8.2.2 / 缺陷 F28 |
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

**风险**：① 8B 牌力弱导致判定表全拒 → M2 先用宽松档（`alpha=0.10`、`win_rate_tol=0.05`；默认档已是 `win_rate_tol=0.05`，宽松档只把 `alpha` 放宽到 0.10，见 2026-10-06 D-3）跑通管线，M3 收紧到默认值并给出收紧前后对比；② heldout 预算被候选数放大 → 冒烟前置 + `cost_cap` 硬截断（2026-10-06 D-1：`C=1`、`S=2`、`H=40`、`E_gen=114 ≤ loop.max_episodes_per_generation=120`）；③ diff 质量低 → `propose_max_retries=2` + 去重，`propose` 失败率 >50% 时回看 MOD-04 prompt 契约；④ HITL 拖慢实验节奏 → `--hitl=false` 跑批量实验（标 `decided_by="auto"`，不计入 M3 验收样本）；⑤ git 分支污染主仓 → 只经 MOD-09 `git_ops`，禁裸 `subprocess`，`E_EVOLVE_GIT_FAILED` 时不动 `version.json`；⑥ **（2026-10-06 D-2/D-3）`C=1` 削弱"多候选择优"信号、且 `win_rate` 在 H=40 上只能判 ≥15pp** → 必须在 M2 §3.2 冒烟里确认 C=1 仍能产出 ≥1 轮可判定的演化（判据落在 `mean_stages_cleared` 的配对显著性上，而非 win_rate 点估计），否则回头调 D-1。

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
| v0.4 | 2026-10-07 | **C5 按实测标定 + M3 相对门设计（Spec v1.8 B-40/B-41；依据 [M2 §3.1 实测报告](../../reviews/2026-10-07-M2-S31-full-agent基线实测.md)）**。① §8.2 C5 行与 §9 配置表：`max_illegal_rate`/`max_fallback_rate`/`max_truncation_rate` 0.05/0.10/0.05 → **0.35/0.25/0.15**（full `PolicyAgent` 实测 0.3016/0.2146/0.10，原门超限 6.03×/2.15×/2.00×，会 reject 每个候选；写明是「先松开、M3 再收紧」并声明副作用）。② 新增 **§8.2.1**：M3 的收紧路径是把 C5 从绝对门改为**相对门** `candidate ≤ parent + tol`，含 `tol` 标定方式、M3 新增的三个配置键、以及保留宽松绝对上限作 backstop。③ 顺带补齐 D-1 的表格滞后：§9 的 `evolve.n_candidates` 2→**1**、`evolve.smoke_n` 5→**2**（与 `configs/default.yaml` 及 `config.py` 对齐）。④ 版本头对齐 Spec v1.8 |
| v0.5 | 2026-10-09 | **新增 §8.2.2：C5 是「臂相关」的 → 登记缺陷 F28 与待裁决的处置**（对齐 Spec v2.0 B-44）。起因：C5 三阈值（§8.2.1 的①）是 2026-10-07 按**本地 8B** 标定的，而 M2 闭环主力臂已于 2026-10-09 由用户裁决为 **`C1`（三角色全 `deepseek-flash`）**（Spec B-43）。用当前臂实测值 + **真实 `judge()`** 复算：`hard × 6` 的 `truncation_rate = 0.30 > 门 0.15` ⇒ **C5 会 reject 每一个候选**（`E_EVOLVE_ILLEGAL_RATE`）；同时 illegal/fallback 反向松了约 10×。① 结论：**C5 的阈值不是被测对象的属性，而是「标定臂 × 局型」的属性**；§3.1 要防的「静默锁死闭环」风险未被消除，只是从 illegal 轴搬到 truncation 轴（`hard × 6` 上 3/10 局撞内核 `maxTurnsPerBattle=60` → `E_KERNEL_TURN_LIMIT`，且被截断的局计 lose 不算 dropped）。② 补记「闭环为什么静默」：`reject` 不是错误，MOD-06 对接受与拒绝同样 `generation += 1` 并回到 `playing`（退出码 0），MOD-07 照常写 decision 记录 ⇒ 全拒的闭环会跑完全部代数、退出码 0、日志无异常，但 `harness/versions/` 永不出现新版本。③ 列出三个处置候选（按当前臂重标 / bring-up 改用 medium×6 / **truncation 并入相对门**）并写明**裁决前不得宣称 M2 准入完成** |
