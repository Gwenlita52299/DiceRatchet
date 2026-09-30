# MOD-07 evolve 技术 PRD

> **版本**：v0.1（草稿）· **状态**：待评审 · **所属里程碑**：M2（propose/evaluate/versioning/HITL 初版）→ M3（回滚/消融对接/统计口径收紧）· **作者**：GameRSI 技术文档 · 日期：2026-09-30
> **上游 Spec**：[../模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md)（v1.0，**唯一事实来源**；冲突以 Spec 为准）· **关联不变量**：INV-3/INV-4/INV-5/INV-7
> **代码位置**：`gamer_rsi/evolve/{proposer,judge,versioning,gate}.py`、`harness/`、`runs/<run_id>/generations/`

---

## 1. 背景与目标

MOD-06 每 K 局（默认 30）触发演化周期，把「策略记忆 + 最近轨迹 + 反思」交给 MOD-07。MOD-07 负责 RSI 闭环中最危险的一段：**让 agent 改写自己的 harness，并由环境 ground-truth 决定改动去留**（计划 §5 步骤 1–5）。按 RSI 调研 §4 的失败模式，设计重心不是"生成更聪明的改动"，而是**在结构上掐死自我确认环路**：

- 自我确认环路：候选生成阶段**物理上看不到** heldout/test 数据（INV-5），裁判只看环境原生结果（INV-7），生成者与裁判者上下文不重叠。
- 模型坍缩：接受判定强制 `collapse_flag == False` + 熵斜率下限（C4）。
- 过度优化/指标刷分：改动面硬上限 + `win_rate_gap` 阈值 + 消融对照（M3）。

**目标（可验收）**：① `propose()` 稳定产出 2–3 个通过校验的候选，或返回显式空结果 + `E_EVOLVE_NO_CANDIDATE`，绝不静默退化；② 每个候选走「train 冒烟 → heldout 正式评估」，同 seed 同结果；③ 判定是可执行判定表（§8.2），六项全通过才 accept；④ 每次 accept 产生不可变版本记录 + git commit；⑤ HITL 开启时挂起等 `evolve approve|rollback`；⑥ `report.json` 逐字段对齐 Spec §4.5 并通过 round-trip 测试。

## 2. 范围

### 2.1 范围内

`propose()`（候选生成/去重/证据束白名单）；候选合法性校验（MOD-02 AST 扫描、改动面计数、禁用符号、JSON Schema）；`evaluate_candidate()`（train 冒烟 → heldout 正式评估，**只编排** MOD-08）；`judge()` 判定表与 `reason`；`versioning.py`（`harness/versions/v{N}/`、`version.json` 血缘、`diff.patch`、git commit/branch、`rollback`）；`gate.py`（HITL 挂起/恢复/超时回滚）；代际日志与 `report.json`；`E_EVOLVE_*` 错误码的产生与上报（登记由 MOD-09 完成）。

### 2.2 范围外（非目标）

> 逐字复用 Spec §3「MOD-07 evolve」的**不负责**清单：

不负责：模型推理实现（MOD-03）、指标定义与统计口径（MOD-08）、seed 抽取规则（MOD-09 注册表 + MOD-08 协议）。

边界补充（不引入新职责）：不改 `gamer_rsi/agent/policy.py` 的解析与校验逻辑（MOD-04）；不做超参搜索；不做 LoRA/微调（MOD-10）；不决定 harness 内容的**语义**，只负责把 LLM 给出的 `diff` 落盘并版本化。

## 3. 使用方与场景

| 使用方 | 调用方式 | 场景 |
|---|---|---|
| MOD-06 loop | `propose → evaluate_candidate → judge → versioning.accept/rollback → gate.await_human` | `play → evaluate → reflect → evolve` 的 evolve 段 |
| MOD-08 evals | 被调用：`run_heldout(harness_dir, seeds, budget, split)`、`paired_significance(a, b, ...)` | 提供 heldout 指标 / 显著性与 `collapse_flag`；MOD-07 不做统计 |
| MOD-02 env-sandbox | 被调用：`scan_harness(path) -> ScanReport`、`load_restricted(path)` | INV-4：新 harness 先静态扫描、再受限加载 |
| 人类操作者 | `python -m gamer_rsi evolve approve\|rollback --generation N [--reason ...]` | HITL 确认/回滚，查看 `diff.patch` 与 `report.json` |
| MOD-09 infra / MOD-05 memory | 提供 config/events/git_ops/guards；只读 `retrieve_for_evolution(top_k)` | 配置优先级、结构化事件、git 封装、AST 守卫；策略记忆与反思（进证据束） |

正常流：`loop run --generations 3 --k 30 --hitl` → gen-1 propose 3 个候选 → 冒烟淘汰 1 → heldout 评 2 → 1 个 accept → 挂起 → `evolve approve --generation 1` → 续跑 gen-2。
被攻击流：`diff` 含 `import os` / heldout seed 字面量 / 改 `rank_actions` 签名 → 静态扫描直接拒绝，写 `E_EVOLVE_SCAN_FAILED`，**0 局 heldout 消耗**。

## 4. 功能需求（FR-07-x，编号，每条可测试）

- **FR-07-1 候选生成**：`propose()` 输入 `EvidenceBundle`，输出 `list[Candidate]`，长度 ∈ [2, `evolve.n_candidates`]（默认 3）。每条含 `kind ∈ {prompt_patch, tool_patch, config_patch}`、`diff`（unified diff）、`rationale`（≤300 字）、`expected_effect{metric,direction,priority}`。失败返回 `ProposalResult(candidates=[], error_code="E_EVOLVE_PROPOSE_FAILED")`。
- **FR-07-2 证据束白名单**：`EvidenceBundle` 只能由 `trajectory_summary(split="train")`、`memory.retrieve_for_evolution()`、`reflection_set(split="train")`、`harness_snapshot()` 构造；`build_evidence_bundle()` 是唯一构造函数，`__post_init__` 校验 `source_tags ⊆ {"memory:train","traj:train","reflect:train","harness:current"}`。
- **FR-07-3 防自我确认（生成侧）**：证据束条目 `split` 必须全为 `train`；`assert_no_holdout_leak()`（1）拒绝任何 `split != "train"` 条目，（2）对全部文本字段做 seed 字面量扫描——命中 heldout 注册表任一 seed 的整值 token（正则 `(?<!\d){seed}(?!\d)`）即抛 `E_EVOLVE_HOLDOUT_LEAK`，（3）拒绝出现 `heldout`/`test` 标签或 `seeds.yaml` 片段。`render_proposer_prompt()` 模板化产出，**不允许**插入证据束之外的上下文（INV-3）。
- **FR-07-4 修改面硬约束（单候选）**：改动行数 ≤ `evolve.max_diff_lines`（40）且字符数 ≤ `evolve.max_diff_chars`（4000）；文件数 ≤ 2 且必须 ∈ 白名单 `harness/prompts/decision.md`、`harness/prompts/reflection.md`、`harness/tools/*.py`、`harness/config.json`；**禁止**改 `rank_actions` 签名与 `@harness_tool` 语义；**禁止**新增第三方依赖（顶层 import 必须 ∈ `evolve.tool_import_allowlist` 或 `tools/` 内已有模块）；**禁止**网络/文件写/subprocess/env 内部访问（INV-4）；`config_patch` 只能改 `evolve.evolvable_keys` 声明的键。
- **FR-07-5 去重与排序**：按归一化 `diff` 的 sha256 去重；按 `expected_effect.priority` 降序、同优先级 `candidate_id` 升序（保证确定性）。
- **FR-07-6 训练冒烟**：`evolve.smoke_seeds`（默认 `train:1000-1004`，5 局）先跑。快速淘汰（任一命中，写 `E_EVOLVE_SMOKE_FAILED`）：冒烟胜率 < 当前版本冒烟胜率 − 0.1；非法率 > 0.30；harness 运行期异常；5 局全负且 `mean_turns < 5`。
- **FR-07-7 正式评估**：冒烟通过者在 `evolve.heldout_seeds`（默认 `heldout:2000-2039`，40 局）评估，**预算与当前版本完全相同**（同 seed 集合、同 `episode_config`、同采样参数，仅 `prompt_tokens` 允许差异并记录）；指标全部委托 MOD-08 `run_heldout()`，MOD-07 不做任何统计运算。
- **FR-07-8 判定表**：按 §8.2 顺序短路求值，六项全 True → `accept`，否则 `reject` 且 `reason` 含首个失败条件 id（如 `C2:heldout_win_rate 0.28<0.32`）。
- **FR-07-9 版本化**：`accept()` 生成 `harness/versions/v{N}/`（全量拷贝 + `version.json` + `diff.patch`），更新 `harness/version.json` 指针，git 提交到分支 `harness/v{N}`，message = `harness(v{N}): gen-{G} {kind} {short_rationale}`；`N` = 现有最大 +1，**不可复用、不可重写历史**。
- **FR-07-10 回滚**：`rollback(to_version)` 用 `versions/v{to_version}/` 覆盖 `harness/`，写 `version.json`、生成 `revert.patch`、提交 `revert(harness): back to v{N}`，并写一条 `decision="rollback"` 的代际报告。
- **FR-07-11 HITL 门**：`evolve.hitl=true` 时 `gate.await_human()` 把决策置 `pending`、写 `gen-{N}/gate.json`（`generation, harness_version, parent_version, diff_path, report_path, status, expires_at`），loop **优雅退出（exit 0，可续跑）**；`evolve approve|rollback --generation N` 读取 gate 文件后执行 accept/rollback，写 `decided_by="human"` 与可选 `reason`；超时（默认 24h）→ 保守回滚 `parent_version`，`reason="hitl_timeout"`（对齐 Spec §8.5）。
- **FR-07-12 拒绝记录**：任何 reject（含冒烟淘汰、扫描失败）都落 `gen-{N}/candidates/{candidate_id}.json`，含 `candidate`、`report`（若有）、`decision`、`reason`、`error_code`。
- **FR-07-13 候选代码安全执行**：新 harness 使用前必须依次通过 `scan_harness()`（MOD-02 静态扫描：AST 白名单/禁用符号/依赖检查）与 `load_restricted()`（受限加载）；扫描失败 → 直接 reject，`E_EVOLVE_SCAN_FAILED`，**不进冒烟、不进 heldout**；受限加载失败 → 立即回滚 `parent_version`，`E_EVOLVE_LOAD_FAILED`。
- **FR-07-14 代际报告**：每 `gen-{N}` 写 `report.json`，键严格等于 Spec §4.5、缺失用 `null`、**不新增顶层键**；多候选时记录最终接受者（或最后尝试者），其余写 `candidates/` 明细；**FR-07-15** 空候选/全淘汰也必须落盘（`decision="reject"` + 对应错误码），保证「每次接受/拒绝都有 `report.json` 支撑」（Spec §8.4）。

## 5. 非功能需求（NFR-07-x：确定性/性能/可复现/可观测）

- **NFR-07-1 确定性**：随机性只来自显式 seed（`evolve.seed`，由 MOD-09 从 `run_id` 派生）；禁 `random.*`/`time`/`uuid4`，遍历用 `sorted()`。同 `(run_id, generation, seed)` 重跑，`candidate_id` 序列与 `report.json` 逐字节相同（`created_at`/`ts` 由 MOD-09 注入，可用 `--freeze-time` 冻结以支持 G3）。
- **NFR-07-2 安全（INV-4/INV-5）**：候选代码永不 `exec` 于主进程；受限加载由 MOD-02 提供；proposer 上下文白名单由 FR-07-3 代码级强制；heldout seed 值出现在 `harness/`、`runs/*/reflections/` 或 memory 库中即为 P0。
- **NFR-07-3 可复现**：每次决策可由 `report.json` + `candidates/*.json` + `diff.patch` + `version.json` 离线重建；`config_hash` 覆盖 evolve 配置（Spec §5）。
- **NFR-07-4 可观测**：只用 MOD-09 结构化事件（`evolve.propose|validate|scan|smoke|evaluate|decision|version|gate|cost`），一次调用 = 一行 JSONL，不自建日志格式。
- **NFR-07-5 成本上限**：候选 ≤ 3；冒烟 ≤ 5 局；heldout 局数由 MOD-08 协议给定（40）且**不因候选数放大**（候选共享同一 seed 集合），单代总成本 ≤ `evolve.cost_cap_episodes_per_generation`（150），超限按优先级截断候选。
- **NFR-07-6 幂等可续跑**：`accept()`/`rollback()` 以 `(run_id, generation)` 为幂等键，重复调用返回首次结果，不产生第二个版本号。
- **NFR-07-7 失败保守**：裁判链路任何异常（MOD-08 抛错/报告缺字段/超时）→ 一律 reject 并保留 `parent_version`，记 `E_EVOLVE_JUDGE_FAILED`；演化失败不得中断 `play`。

## 6. 接口契约（真实 Python 签名 + JSON Schema）

### 6.1 核心 Python 签名（`gamer_rsi/evolve/`）

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
def write_generation_report(gen: int, payload: dict, *, run_id: str) -> Path: ...
# ---- gate.py ----
def await_human(*, generation: int, run_id: str, diff_path: Path,
                report_path: Path, cfg: EvolveConfig) -> GateState: ...
def resolve_gate(*, generation: int, run_id: str, action: Literal["approve","rollback"],
                 reason: str | None) -> Verdict: ...
```

### 6.2 被依赖签名

```python
# gamer_rsi/sandbox/guard.py（MOD-02，Spec §3 承诺）
def scan_harness(path: Path, *, policy: ScanPolicy) -> ScanReport:  # ScanReport(status:"pass"|"fail", violations:[{"code","file","line","symbol"}], ast_digest:str, third_party_imports:list[str], disallowed_calls:list[str])
    """扫描失败返回 status="fail"，violations 逐条落到 candidates/{id}.json；本模块遇 fail 立即 reject。"""
def load_restricted(path: Path, *, allow_write: bool = False) -> "HarnessHandle": ...
# gamer_rsi/evals/harness.py（MOD-08，需 MOD-08 PRD 回填确认）
def run_heldout(*, harness_dir: Path, seeds: str, budget: EvalBudget,
                split: Literal["train","heldout"], run_id: str) -> HeldoutReport: ...
# HeldoutReport: {"n","win_rate","win_rate_ci95","mean_hp_left","mean_kills","mean_turns","illegal_rate",
#                 "action_entropy_curve","entropy_slope","collapse_flag","ablation":{"no_reflection","no_self_edit"},"seed_set_hash"}
def paired_significance(a: HeldoutReport, b: HeldoutReport, *, alpha: float, method: str) -> dict:  # {"significant":bool,"p_value":float,"delta":float,"metric":str,"method":str,"n_pairs":int,"direction":"up"|"down"|"flat"}
    """方向由 MOD-08 按 metric 约定给出；本模块只消费 significant/direction/delta/metric。"""
```

### 6.3 JSON Schema（`runs/<run_id>/generations/gen-{N}/report.json`，严格对齐 Spec §4.5）

```jsonc
// 顶层："type":"object","additionalProperties":false（禁止新增顶层键）；required = 下列 11 键，顺序同 Spec §4.5
{"generation":int>=1, "harness_version":int>=1, "parent_version":int|null,
 "heldout":{"n":int, "win_rate":num, "win_rate_ci95":[num,num]（恰好2项）, "mean_hp_left":num, "mean_kills":num,
            "mean_turns":num, "illegal_rate":num, "action_entropy_curve":[num]},
 "train":{"n":int, "win_rate":num},
 "gain_realism":{"win_rate_gap":num, "gap_threshold":num, "verdict":"real"|"overfit_suspect"|"unknown"},
 "diversity":{"entropy_slope":num, "collapse_flag":bool},
 "ablation":{"no_reflection":num|null, "no_self_edit":num|null},
 "decision":"accept"|"reject"|"rollback"|"pending",
 "decided_by":"auto"|"human", "reason":str}
```

### 6.4 harness 目录布局（Spec §4.4 + 本模块扩展）

```
harness/                     # 当前生效工件（MOD-04 只读）
  version.json               # {"version","parent","git_commit","created_at","reason","accepted_by"}
  prompts/decision.md  prompts/reflection.md  config.json   # evolvable_keys 内键可被改
  tools/*.py                 # @harness_tool 纯函数，rank_actions 签名冻结
  versions/v{N}/             # 每次 accept 的不可变快照：version.json + diff.patch + prompts/ tools/ config.json
runs/<run_id>/generations/gen-{N}/    # report.json  gate.json  decision.jsonl + candidates/{id}.json + patches/{id}.patch
```

**决策（本 PRD 提出，待回填 Spec）**：`versions/v{N}/` 子目录、`gate.json`、`candidates/`/`patches/` 布局与 `evolve.*` 键名为 Spec §4.4/§5 未覆盖项。

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
    bundle = build_evidence_bundle(run_id=run_id, n_traj=cfg.evolve.n_traj, cfg=cfg)      # FR-07-2/3
    prop = propose(bundle, cfg=cfg, rng_seed=derive_seed(run_id, gen, "propose"))
    if not prop.candidates:
        return write_generation_report(gen, reject_payload(prop.error_code), run_id=run_id)
    parent_ho = evals.run_heldout(harness_dir=parent.dir, seeds=cfg.evolve.heldout_seeds,
                                  budget=cfg.eval_budget, split="heldout", run_id=run_id)
    reports, winner = [], None
    for cand in sorted(prop.candidates, key=lambda c: (-c.expected_effect.priority, c.candidate_id)):
        staging = versioning.stage_candidate(cand, run_id=run_id, cfg=cfg)
        if validate_candidate(cand, cfg=cfg).status == "fail":                    # FR-07-4
            reports.append(reject(cand, "E_EVOLVE_PATCH_INVALID")); continue
        if guard.scan_harness(staging, policy=cfg.scan_policy).status == "fail":  # FR-07-13
            reports.append(reject(cand, "E_EVOLVE_SCAN_FAILED")); continue
        smoke = evals.run_heldout(harness_dir=staging, seeds=cfg.evolve.smoke_seeds, budget=cfg.eval_budget,
                                  split="train", run_id=run_id)                   # FR-07-6
        if smoke_fast_reject(smoke, baseline=parent_smoke, cfg=cfg):
            reports.append(reject(cand, "E_EVOLVE_SMOKE_FAILED")); continue
        report = evals.run_heldout(harness_dir=staging, seeds=cfg.evolve.heldout_seeds, budget=cfg.eval_budget,
                                   split="heldout", run_id=run_id)               # FR-07-7
        paired = evals.paired_significance(report, parent_ho, alpha=cfg.evolve.alpha, method=cfg.evolve.sig_method)
        cr = CandidateReport(cand, smoke, report, paired); cr.verdict = judge(cr, parent_ho, cfg=cfg)
        reports.append(cr)
        if cr.verdict.decision == "accept" and (winner is None or better(cr, winner)): winner = cr
    if winner is None: return write_generation_report(gen, payload_of(first(reports)), run_id=run_id)
    gate_state = gate.await_human(generation=gen, diff_path=staging_patch(winner), cfg=cfg)
    payload = payload_of(winner)                          # 严格 Spec §4.5 字段
    if gate_state.status == "approved": versioning.accept(winner.cand, winner.verdict, run_id=run_id, cfg=cfg)
    elif gate_state.status == "pending": payload["decision"] = "pending"   # loop 优雅退出，等 CLI
    else: versioning.rollback(parent.version, run_id=run_id, reason=gate_state.reason, decided_by="human")
    return write_generation_report(gen, payload, run_id=run_id)
```

### 8.2 接受判定表（短路求值，逐条写 `condition_trace`）

| # | 条件 | 表达式 | 不满足时 reason / 错误码 |
|---|---|---|---|
| C0 | 前置完备 | `report.heldout.n >= cfg.evolve.min_heldout_n` 且关键字段非 `null` | `C0:incomplete_report` / `E_EVOLVE_JUDGE_FAILED` |
| C1 | 主指标不降 | `report.win_rate >= parent.win_rate - cfg.evolve.win_rate_tol`（`tol=0.0`，不允许下降） | `C1:win_rate {v}<{p}` / `E_EVOLVE_NO_GAIN` |
| C2 | 至少一指标显著提升 | `paired.significant == True and paired.direction == "up"`，`metric ∈ {win_rate, mean_hp_left, mean_kills}`，口径由 MOD-08 提供（`bootstrap_paired`，`alpha=0.05`，1000 次重采样，固定 seed） | `C2:no_significant_gain` / `E_EVOLVE_NO_GAIN` |
| C3 | 增益真实性 | `train.win_rate - heldout.win_rate <= cfg.evolve.gap_threshold`（0.15，对齐 Spec §4.5 `gap_threshold`） | `C3:gap {gap}>{thr}` / `E_EVOLVE_OVERFIT_SUSPECT` |
| C4 | 无坍缩 | `diversity.collapse_flag == False and diversity.entropy_slope >= cfg.evolve.min_entropy_slope`（−0.01；Spec 示例 −0.004 属通过区间） | `C4:collapse` / `E_EVOLVE_COLLAPSE` |
| C5 | 合法性与成本 | `heldout.illegal_rate <= cfg.evolve.max_illegal_rate`（0.05）且 `heldout.mean_turns <= cfg.evolve.max_mean_turns`（60，防刷回合） | `C5:illegal_rate/turns` / `E_EVOLVE_ILLEGAL_RATE` |

`accept ⟺ C0∧C1∧C2∧C3∧C4∧C5`。多候选全通过时取 `(C2 delta 最大, candidate_id 最小)` 为 winner，其余记 `reject/reason="superseded"`（不计失败、无错误码）。

### 8.3 HITL 时序 / 8.4 回滚流程

```
loop(MOD-06) --accept?--> gate(MOD-07): hitl? → 写 gate.json(status=pending) → loop 退出 exit 0
loop run --resume --> CLI(MOD-09) 读 gate.json --> approve/rollback --> gate: accept()/rollback() --> git commit harness/v{N}
```

`rollback(v)`：① 校验 `v` 存在且 `v <= current`；② `harness/` 内容替换为 `versions/v/`；③ 写 `version.json`（`version=v`，`parent=current_version`，新 `git_commit`，`reason`，`accepted_by=decided_by`）；④ 生成 `revert.patch`（current → v）；⑤ git commit `harness/v{v}`；⑥ 写 `gen-{N}/report.json`，`decision="rollback"`。

## 9. 配置项（键名、类型、默认值、含义）

| 键（`configs/default.yaml`） | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `evolve.n_candidates` | int | 3 | 每代候选数上限（Spec §8.6：≤3） |
| `evolve.n_traj` / `evolve.memory_top_k` | int | 20 / 12 | 证据束最近 train 轨迹摘要条数 / MOD-05 检索条数 |
| `evolve.max_diff_lines` / `evolve.max_diff_chars` | int | 40 / 4000 | 单候选改动面硬上限 |
| `evolve.smoke_seeds` / `evolve.train_eval_seeds` | str | `train:1000-1004` / `train:1030-1069` | 冒烟组（5 局，快速淘汰）/ `report.train.win_rate` 评估组 |
| `evolve.heldout_seeds` | str | 由 MOD-09 注册表注入 | heldout 评估组（40 局），**不得写入任何 prompt** |
| `evolve.min_heldout_n` / `evolve.win_rate_tol` | int / float | 20 / 0.0 | C0 门槛 / C1 允许下降幅度 |
| `evolve.alpha` / `evolve.sig_method` | float / str | 0.05 / `bootstrap_paired` | C2 显著性口径（实现归 MOD-08） |
| `evolve.gap_threshold` / `evolve.min_entropy_slope` | float | 0.15 / −0.01 | C3 `win_rate_gap` 阈值 / C4 熵斜率下限 |
| `evolve.max_illegal_rate` / `evolve.max_mean_turns` | float / int | 0.05 / 60 | C5 上限 |
| `evolve.hitl` / `evolve.gate_timeout_hours` | bool / int | true / 24 | 人在环门开关 / 超时 → 保守回滚 |
| `evolve.cost_cap_episodes_per_generation` / `evolve.propose_max_retries` | int | 150 / 2 | 单代评估局数硬上限 / JSON-schema 失败重试（在 MOD-03 `generate_json` 之上再包一层） |
| `evolve.evolvable_keys` | list[str] | `["retrieval.top_k","sampling.temperature","prompt.show_intent"]` | `config_patch` 白名单键 |
| `evolve.tool_import_allowlist` | list[str] | `["math","re","json","itertools","functools","typing","dataclasses"]` | `tool_patch` 允许的顶层 import |

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀）

| 错误码 | 触发 | 处理 | 消耗 heldout 预算 |
|---|---|---|---|
| `E_EVOLVE_PROPOSE_FAILED` / `E_EVOLVE_NO_CANDIDATE` | 生成失败/重试耗尽/JSON 不合 schema；或生成成功但 0 候选（含全被去重） | 该代 `decision="reject"`，写报告，不开评估；`reason="no_candidate"` | 否 |
| `E_EVOLVE_HOLDOUT_LEAK` | 证据束含非 train split 或 heldout seed 字面量 | **致命**：中止本代 + 写 `audit/` 告警；连续 2 次中止 run（INV-5） | 否 |
| `E_EVOLVE_PATCH_INVALID` | `validate_candidate` 失败（行数/文件/签名/依赖/白名单） | 该候选 reject，继续下一候选 | 否 |
| `E_EVOLVE_SCAN_FAILED` | MOD-02 `scan_harness` 返回 `fail` | 该候选 reject，`violations` 落 `candidates/{id}.json` | 否 |
| `E_EVOLVE_LOAD_FAILED` | `load_restricted` 抛错 | 已 accept 则立即回滚 `parent_version`，否则 reject | 是（已发生） |
| `E_EVOLVE_SMOKE_FAILED` | 冒烟快速淘汰命中 / 冒烟异常 | reject，不进 heldout | 否（仅 5 局 train） |
| `E_EVOLVE_EVAL_FAILED` | MOD-08 超时/异常 | reject（保守），保留当前 harness | 是 |
| `E_EVOLVE_NO_GAIN` / `E_EVOLVE_ILLEGAL_RATE` | C1、C2 或 C5 失败 | reject（C5 记 `illegal_rate`/`mean_turns` 明细） | 是 |
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
3. 给定证据束混入一条 `split="heldout"` 轨迹摘要，当 `build_evidence_bundle`，则抛 `E_EVOLVE_HOLDOUT_LEAK`；给定反思文本出现 heldout seed `1042` 字面量，当 `assert_no_holdout_leak`，则抛 `E_EVOLVE_HOLDOUT_LEAK`（出现 `10420`/`x1042y` **不**误报，正则 `(?<!\d)1042(?!\d)`）。
4. 给定 `render_proposer_prompt` 产出，当扫描，则占位符 ⊆ `{observation, memory, harness, task}` 且不含 `heldout`/`test` 子串（INV-3 回归）；给定同一 `(run_id, gen, seed)` 与固定 stub LLM，当两次 `propose`，则 `candidate_id` 列表逐字节相同（NFR-07-1）。
5. 判定表表驱动 12 例：C1 降 0.01 → reject(`E_EVOLVE_NO_GAIN`)；C2 `significant=False` → reject；C3 `gap=0.19>0.15` → reject(`overfit_suspect`)；C4 `collapse_flag=True` → reject；C5 `illegal_rate=0.11` → reject；六项全真 → accept；并覆盖短路顺序。
6. 给定已存在 `(run_id, gen)`，当重复 `accept`，则返回首次结果且 `harness/versions/` 无新增目录（幂等）；给定 current=5，当 `rollback(2)`，则 `harness/` 的 sha256 等于 `versions/v2/`，`version.json.parent==5`，产生 `revert.patch`；给定 `evolve.hitl=false`，当 accept，则不写 `gate.json` 且 `decided_by="auto"`；给定 `hitl=true` 且 gate 未响应至超时，则 `decision="rollback"`、`reason="hitl_timeout"`。

**集成（`tests/integration/evolve/`）**：① 给定 stub LLM + 缩配（`smoke_seeds=train:1000-1001`、`heldout_seeds=heldout:2000-2005`），当跑 `run_generation`，则产出 `report.json` 且通过 §6.3 Schema（G5 round-trip）；② 给定 3 代 run（`--hitl`）且每代 `evolve approve`，则 `harness/version.json.version == 3` 且 `harness/v1..v3` 三个 commit 存在（G6）；③ 给定 MOD-08 抛 `E_EVAL_TIMEOUT`，当 `evaluate_candidate`，则候选 reject 且当前 harness 不变（NFR-07-7）；④ 给定 run 全目录，当 `audit leak --run <run_id>`，则在 MOD-07 产出物上 0 告警（G4）。

**安全用例（恶意候选被拒，`tests/unit/evolve/test_malicious_candidates.py`；全部期望 0 局 heldout 消耗）**

| 用例 | 恶意候选内容 | 期望 |
|---|---|---|
| MAL-1 网络 / MAL-2 文件写 / MAL-3 子进程 | `urllib.request.urlopen(...)`；`open("harness/seed_backup.json","w")`；`subprocess.run(["git","push"])` | 均 `E_EVOLVE_SCAN_FAILED`（INV-4） |
| MAL-4 env 内窥 / MAL-5 预读作弊 / MAL-7 签名规避 | `from gamer_rsi.env.rng import RNG`；diff 写入 heldout seed 列表或 `if seed == 1042:` 常量分支；`rank_actions = lambda *a: a[1]` 绕过 `@harness_tool` | 均 `E_EVOLVE_SCAN_FAILED`（INV-1 + 注册表完整性/AST 校验） |
| MAL-6 历史重写 / MAL-8 预算耗尽 | `prompt_patch` 改 `version.json` 或 `versions/`；单代请求 40 个候选 | `E_EVOLVE_PATCH_INVALID`（路径白名单）；按优先级截断至 3、`cost_cap` 未破（NFR-07-5） |

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

**对齐 Spec §9 质量门**：G1 —— `proposer.py`/`judge.py`/`versioning.py`（判定表与 diff 计数为纯函数）覆盖率 ≥80%，`gate.py` ≥60%（含 IO）；G3 —— 同 `run_id` + `--freeze-time` 重跑，`candidate_id` 序列与 `report.json`（除 `created_at`）逐字节相同；G4 —— 自测 run 上 `audit leak` 0 告警，MAL-5 蜜罐式候选必须被 `scan_harness` 抓到；G5 —— `report.json`/`version.json`/`gate.json`/`Candidate` 四个 schema 有 round-trip 测试；G6 —— 本节命令可直接复制执行成功。

**对齐 M2**：`python -m gamer_rsi loop run --generations 1 --k 30 --hitl` 闭环跑通 ≥1 轮演化，产出 `diff.patch` + `report.json` + `gate.json`，`evolve approve` 后 `harness/version.json.version == 1`。
**对齐 M3**：≥3 代演化且 heldout 主指标单调不降（计划 §6 验收 1）；至少 1 个指标相对基线可归因提升且 `ablation.no_self_edit` 差异被记录（验收 2）；`win_rate_gap < gap_threshold` 且 `collapse_flag == False`（验收 3）。
**专属 DoD**：任意 reject 都有 `report.json`；任意 accept 都有 `version.json` + `diff.patch` + git commit；任意恶意候选都在静态扫描阶段被挡且 0 局 heldout 消耗。

## 13. 指标与埋点

| 事件（MOD-09 `events.jsonl`） | 字段 |
|---|---|
| `evolve.propose` | `run_id, gen, n_candidates, n_deduped, kinds[], prompt_tokens, latency_ms, error_code` |
| `evolve.validate` / `evolve.scan` | `candidate_id, status, violations[], diff_lines, touched_files[]` / `ast_digest, third_party_imports[], disallowed_calls[]` |
| `evolve.smoke` / `evolve.evaluate` | `candidate_id, n, win_rate, illegal_rate, elapsed_s, fast_reject_reason` / `win_rate_ci95, mean_hp_left, mean_kills, delta_vs_parent, p_value, episodes_spent` |
| `evolve.decision` / `evolve.version` / `evolve.gate` / `evolve.cost` | `gen, candidate_id, decision, decided_by, condition_trace[], reason, error_code` / `from_version, to_version, git_commit, branch` / `status(pending\|approved\|rolled_back\|timeout), waited_s, decided_by` / `episodes_spent, episodes_cap, candidates_evaluated` |

看板：候选接受率（按 kind）、冒烟淘汰率、heldout 预算消耗/代、`win_rate_gap` 与 `entropy_slope` 时间序列、HITL 平均等待时长、`E_EVOLVE_SCAN_FAILED` 占比。**红线**：`E_EVOLVE_HOLDOUT_LEAK` 任意一次；`collapse_flag` 连续 2 代为真。

## 14. 依赖与风险

| 依赖 | 内容 | 缺失影响 |
|---|---|---|
| MOD-06 loop | 触发周期、传入 `run_id`/`gen`、断点续跑 | 无法端到端跑，单测用 stub 驱动 |
| MOD-08 evals | `run_heldout`、`paired_significance`、`collapse_flag`、`entropy_slope` | C2/C3/C4 不可判 → 全部保守 reject |
| MOD-09 infra | config/events/git_ops/run_id/guards | accept 与报告落盘不可用 |
| MOD-02 sandbox | `scan_harness` + `load_restricted` | **阻塞上线**：无扫描即违反 INV-4（M0 已交付） |
| MOD-03 inference / MOD-04 policy | `generate_json()` 结构化输出 / `harness_tool` 注册表与 prompt 渲染契约 | `propose` 不可用 → `E_EVOLVE_PROPOSE_FAILED`；新 harness 无法被加载验证 |

**风险**：① 8B 牌力弱导致判定表全拒 → M2 先用宽松档（`alpha=0.10`、`win_rate_tol=0.02`）跑通管线，M3 收紧到默认值并给出收紧前后对比；② heldout 预算被候选数放大 → 冒烟前置 + `cost_cap` 硬截断；③ diff 质量低 → `propose_max_retries=2` + 去重，`propose` 失败率 >50% 时回看 MOD-04 prompt 契约；④ HITL 拖慢实验节奏 → `--hitl=false` 跑批量实验（标 `decided_by="auto"`，不计入 M3 验收样本）；⑤ git 分支污染主仓 → 只经 MOD-09 `git_ops`，禁裸 `subprocess`，`E_EVOLVE_GIT_FAILED` 时不动 `version.json`。

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

1. `paired_significance` 方法与最小样本量归 MOD-08：40 局能否检出 +5% 胜率提升？若不能，C2 是否退化为"CI 不重叠 + 效应量 ≥ 0.05"？
2. `versions/v{N}/` 全量拷贝 vs 只存 `diff.patch` 按需重建（仓库膨胀 vs 可审计）；多候选同时通过时只接受 1 个（当前决策）是否低估单代增益，是否允许"1 个 canonical + 1 个 config_patch"？
3. Tie 语义：`win_rate` 相同但 `mean_hp_left` 显著提升时，C1 当前按"不降"通过——是否应额外要求次级指标不得恶化？`E_EVOLVE_HOLDOUT_LEAK` 处置是否过重（替代：告警 + 该代作废 + 移交人工）？
4. MOD-10 介入后 harness 与模型权重需联合版本化：`version.json` 是否新增 `base_model`（改 Spec §4.4，需先回填）？`harness/` 整体入 git（含 `versions/`）还是只提交生效工件 + `diff.patch`（影响 `rollback` 的 git 语义）？
