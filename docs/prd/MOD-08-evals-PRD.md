# MOD-08 evals 技术 PRD

> 版本：v0.14（2026-10-14 **判据 1 的基线选择偏差标注（F70）+ `--split train` 只读化（F61/F71）**：`acceptance.json`/ext 新增 `baseline_selection`（上一代 accept ⇒ 本代基线是**选择后的**测量，winner's curse 必须显式标注）；`eval report` 只允许对 `heldout` split 写权威产物（train 侧走只读 sidecar，修复前会覆盖 `report.json` 的 heldout 段）。见 §12/§13/§17）
> 历史版本：v0.13（2026-10-11 **`c3` 三处缺陷修复 F49 + C5 `tol` 的 MDE 换算**：`kind_distribution/` 落盘、`c3` 改单代触发、`eval acceptance --sensitivity`；见 §13/§17）· 历史版本：v0.12（2026-10-11 **判据 1 判定器 F-M3-9**：`eval acceptance` + `metrics/acceptance.json`，并补 `P(gap>0)` 的定义与一处同名异义校正，见 §12/§13/§17）· 历史版本：v0.11（2026-10-11 **样本资格口径澄清 D-M3-4**：`--hitl=false` 全自动跑，`decided_by="auto"` + `condition_trace` 完整即计入 M3 验收样本，报告须记「本轮无人工确认」，见 §12）· 历史版本：v0.10（2026-10-11 **确定性口径按臂分层 D-M3-3**：G3 接进生产执行点但仅对本地臂硬执行，API 臂发 `eval.nondeterministic` 而不抛 `E_EVAL_NONDETERMINISTIC`——修复前该检查**没有任何生产调用点**，见 §5/§12/§13/§17）· 历史版本：v0.9（2026-10-11 **M3 主口径落定 D-M3-1**：`hard × 6` 唯一主口径 + `win_rate` 第一判定量、`medium × 6` 降为外部效度报告项，判定表补齐「≥1 次 accept」，见 §12/§13/§17）· 历史版本：v0.8（2026-10-11 F46-A / F42-A+B+C / F45-2：`generation` 真过滤 + `--arm` 臂显式化、按臂归档与 fail-closed 复用、heldout/test 换六位端口空间，见 §13/§17）· v0.7（2026-10-10 `VERDICT_DECISIONS` 纳入 `error_reject`，处置 F47-D）· v0.6（2026-10-06 按 M1 验收复核更正 §15 口径引用）· 状态：待评审 · 所属里程碑：M1（基线）→ M2（执行器/指标）→ M3（完整：坍缩 + 消融 + test 终检）
> 上游 Spec：[../模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md)（**唯一事实来源**，冲突以 Spec 为准）· 上游计划：[../../项目计划-DiceRatchet.md](../../项目计划-DiceRatchet.md)（§5 防坍缩、§6 三项验收、§8 沙箱泄漏）
> 作者：MOD-08 owner · 落地：`dice_ratchet/evals/{harness,metrics,diversity,ablation,baselines}.py`

## 1. 背景与目标

MOD-08 是 L3 测量层，**只读轨迹与报告，不参与决策**（Spec §1）。它的唯一理由是让"自我改进是否真的发生"成为**可复现、可审计、可证伪**的数字，而不是模型自评。

- **G-1 可信主指标**：主指标只用环境原生结果（INV-7）；shaped reward 不得进入任何判定路径。
- **G-2 防自证**：held-out 与训练局严格隔离（INV-5），`verdict` 明确区分 `real_gain` 与 `overfit_suspect`（计划 §5）。
- **G-3 防坍缩**：动作熵曲线 + 跨代分布 JS 散度产出 `collapse_flag`，对齐调研 §4 失败模式 2（多样性/模型坍缩）。
- **G-4 可归因**：消融（去反思 / 去自编辑）回答"增益来自哪条机制"（计划 §6 验收 2）；**G-5 可复现**：同 `(run_id, generation, split, seeds)` 重跑得到逐字节相同的 `report.json`（G3/G5）。

## 2. 范围

### 2.1 范围内

- `configs/seeds.yaml` 的**协议部分**（区间、篮子、exposure 策略、hash 固化与 CI 校验规则；文件加载归 MOD-09）；三类 seed 隔离的**代码级护栏**（INV-5）。
- 评估执行器（`harness_version + seed 列表 + 预算` → 并行跑局并汇总）；基线 `random`/`heuristic`/`fixed_prompt` 的实现、与 MOD-04 的复用关系、不可变归档。
- 指标（Spec v1.3 INV-7）：`win_rate` + Wilson 95% CI、`mean_stages_cleared`、`mean_hearts_left`、`mean_battles_won`、`mean_decisions`、`illegal_rate`、`fallback_rate`、`truncation_rate`、归一化动作熵与每个决策点的熵曲线。
- shaped reward 定义（`evals/reward.py`，只供学习与调试，Spec v1.3 §3 由 MOD-01 移交过来）。
- 增益真实性（`win_rate_gap` + `verdict`）、多样性/坍缩（`entropy_slope`、`js_divergence`、`collapse_flag`）、消融两变体。
- 报告落地：`report.json`、`metrics/generations.csv`、绘图 tidy 数据（绘图本身是 MOD-09）。

### 2.2 范围外（非目标）

逐字复用 Spec §3 MOD-08「不负责」清单：

- **不负责：修改 harness（MOD-07）**——MOD-08 只出数字与 `verdict`，接受/拒绝/回滚由 MOD-07 `gate.py` 决定。
- **不负责：写游戏规则（MOD-01）**——胜负、HP、击杀、楼层由 `info` 原生给出，MOD-08 不做规则解释或二次判定。
- **不负责：编排训练局（MOD-06）**——train seed 的消耗顺序由 MOD-06 驱动，MOD-08 只登记与统计。

## 3. 使用方与场景

| 使用方 | 场景 | 消费接口 |
|---|---|---|
| MOD-07 evolve/judge | 每候选在 heldout 篮子 H1 上评估取 `verdict` | `evaluate_generation()` / `report.json` |
| MOD-06 loop | 每 K 局的代际报告入口 | `evaluate_generation(generation, ...)` |
| MOD-07 回滚/消融 | M3 终检：去反思 / 去自编辑各一次 | `run_ablation()` / `ablation/gen-XXX.json` |
| MOD-09 infra/CLI + plotting | `eval report/diversity/ablation`、`baseline` 命令；得分曲线、熵曲线、gap 柱 | 全部公开函数、`generations.csv`、`plots_data/*.json` |
| 人（复核者/作品集） | 核对增益真实性与坍缩证据、写 README | `report.json` + `audit` 轨迹 |

## 4. 功能需求（FR-08-x，编号，每条可测试）

- **FR-08-01** 加载 `seeds.yaml` 并校验三类区间两两不相交；任一重叠抛 `E_EVAL_SEED_OVERLAP`。
- **FR-08-02** `train`（默认 1000–1999）按 `per_generation=30` 顺序切块：第 g 代用 `1000+30(g-1) … 1029+30(g-1)`；越界抛 `E_EVAL_SEED_EXHAUSTED`。
- **FR-08-03** `heldout`（**v0.8 / F45-2 起 731000–731099**，此前 2000–2099）切成固定篮子 `H1=731000–731039`（代际裁判）、`H2=731040–731079`（消融/终检复核）、`H3=731080–731099`（应急）；篮子成员不得重新抽样。**为什么换六位**：旧四位空间与耗时（`duration_us=2072.8`）、token 预算（`max_tokens=2048`）、ISO 年份（`2026`）撞车，迫使审计维护两张人工死名单（实测 118,646 次命中、12 个键）；六位端口空间让「数字串相等即泄漏」重新健全。**代价**：`config_hash` **不含** seeds ⇒ 必须由 `baseline_protocol()` 把 `seeds_hash` 绑进测量协议（否则新旧局会被 `--resume` 静默混算），且已归档的 v2 证据与新协议**不可比**。
- **FR-08-04** `test`（**v0.8 / F45-2 起 947000–947099**，此前 9000–9099）默认锁定；仅 `test_unlock` 解锁，每里程碑 ≤1 次，落 `test_attestation.json`；结果**不得**进入代际决策路径。test 同时跑两种局：默认局（主结论）和完整局 medium × 10 关（次级「迁移」指标，Spec §8.6，只报告不判定）。
- **FR-08-05 / 06** `guard_prompt_context()` 在 prompt/memory/reflection 三 destination 上断言文本不含 heldout/test seed 数值 token；`split != "train"` 的局任何 memory 写入与 reflection 落库被拦截；两者命中均抛 `E_EVAL_INV5_LEAK`。
- **FR-08-07 / 08 / 09 / 10** `run_eval()` 支持 `n_workers≥1`，结果按 `seed` 升序归一化聚合且与串行逐字节一致；评估 `temperature=0.0`（训练沿用计划 §3 的 ≥0.7）；每 seed 默认 1 局（后端非确定时 `n_rep=3` 多数票）；超时或崩溃的局记为 `dropped`，**计入 n 并按 lose 计**（Spec v1.3 §8.5，防止幸存者偏差），另外单列 `n_dropped`；`dropped/n > 0.10` 时抛 `E_EVAL_INSUFFICIENT_N` 并标 `low_confidence=true`。`degraded` 局（模型不可用、由启发式代打）则从 n 中剔除，并单列 `degraded_n`。
  - **单局超时真的执行**（2026-10-05，M1 验收 F20）：`evals.seed_timeout_s` 经 `run_eval` 传给 `EpisodeRunner.run_episode(timeout_s=…)`，`SandboxRunner` 把它变成决策循环的 deadline（超预算 → `dropped=true` + `E_EVAL_TIMEOUT`，计 n 按 lose 计）；`run_eval` 另设 `seed_timeout_s × n_rep + 60 s` 的硬看门狗，超限即放弃该 worker、记 dropped，并**换新线程池**继续跑剩余 seed（否则 `n_workers=1` 时一个卡死的 worker 会让其余 seed 永远排不上队）。
  - **⚠️ 这是「墙钟」预算，与并发度耦合**（2026-10-05，M1 验收 F24）：`n_workers` 个局共享同一个模型实例（`max_inflight=1`），所以 **单局墙钟 ≈ D × 每决策调用数 × t_d × n_workers**；默认值已由 1800 s 经实测标定为 **14400 s**（实测 `D≈185–207 决策点 × 1.6–1.8 调用/决策点 × t_d p50 7.9 s / p95 10.8 s × 2 worker` → 单局墙钟 ≈90 min；取最坏 300 决策点的 ≈13000 s）。原值 1800 s 与 5400 s 分别把两批 n=40 判成 `E_EVAL_TIMEOUT`/dropped。**M2 建议改成计算量预算**（`max_llm_calls` 或等效推理秒数），别让预算随 `n_workers` 漂移。**2026-10-05 n=10 实测校验**：`runs/m1-real-h1n10b/` 里 10 局全部自然终局，**没有任何一局触发 `E_EVAL_TIMEOUT`**（`n_dropped=0`），且实测 `d_wall / d_model = 1.02`——该式里的 `× n_workers` 在实测中**不成立**（模型串行，`n_workers` 不放大单局墙钟），14400 s 因此是偏宽的安全余量；M2 改计算量预算时应按 `单局模型时间（≈19.6 min 实测均值，上界约 45 min）` 定，而不是按 `× n_workers` 放大。
  - **逐局增量落盘 + 续跑**（2026-10-05，M1 验收 F18）：`run_eval(..., on_result=…)` 每完成一局即回调，`on_result` 缺省由 `run_baseline` 用来立刻写 `episodes/<split>/<episode_key>.jsonl`；`run_eval(..., resume_results=…)` 把 `--resume` 时已完成的局直接并入聚合。`baseline --resume` 按已落盘的局跳过重复 seed，**不要求** `state.json`（那是 MOD-06 编排续跑的语义）。
- **FR-08-11 / 12** 三基线可用同一 `EvalSpec` 跑通并归档；归档不可覆盖：同名时写 `<kind>-<split>-<ts>.json` 并向 `registry.csv` 追加一行。
  - **溯源字段必须真实**（2026-10-05，M1 验收 F6）：归档与 `registry.csv` 记录真实短 sha（`git_short_sha()`）、真实 `seeds_hash`/`protocol_hash`、**实际加载的**模型 id 与后端名（没加载模型时 `none`，有客户端但未报后端时 `unknown`——不得照抄配置里的 `model_id`/`backend`）、seed 列表、`n_workers`、`seed_timeout_s`、`resumed_n`。
- **FR-08-13 / 14** `heuristic` 复用 MOD-04 `agent.heuristic.heuristic_policy()`（Spec §4.7，禁止复制第二份启发式逻辑；它移植自 GAME `greedy_counter`，默认局上的参考通关率为 45.5%（n=200，seed 1–200，复现命令见 Spec §8.6））；`fixed_prompt` 由 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")` 承担，**恒 `harness_version=0`、`temperature=0`、关闭工具与记忆/反思**，**每个决策点一次推理**（2026-10-05，M1 验收 F22：此前 MOD-08 自带一套逐 env 步的固定提示词 agent，成本是决策点口径的 2.5–3.1 倍，且与演化 agent 推理粒度不一致）。
- **FR-08-15 / 16** 计算 `win_rate`/`wilson_ci()`/`mean_stages_cleared`/`mean_hearts_left`/`mean_battles_won`/`mean_decisions`/`illegal_rate`/`fallback_rate`/`truncation_rate`，全部由整数累加器聚合后单次相除。`action_entropy_curve` 以**决策点序号**为横轴，长度 `T_max`（默认 120），样本数 < `min_support` 的位置写 `null`。截断局的 outcome 一律记为 lose。
- **FR-08-17** 生成 `report.json` 并通过 `additionalProperties=false` schema 校验；扩展指标写入 `metrics/gen-XXX.ext.json`。
- **FR-08-18** 计算 `win_rate_gap = train.win_rate - heldout.win_rate`，按 §8.2 有序规则产出 `verdict`。
- **FR-08-19** 按 §8.3 的 OR 条件 + 两点确认产出 `collapse_flag`；为真时禁止 `real_gain`（`collapse_veto`）。
- **FR-08-20 / 21** 计算跨代 `js_divergence`，有两个口径：`action.kind`，以及 `play_face` 的 `slotKind×school`；`run_ablation()` 在 H2 上跑 3 臂（full/no_reflection/no_self_edit）并做配对检验，产出 `attribution ∈ {reflection, self_edit, both, none}`。
- **FR-08-25 MOD-07 门面**（Spec §4.7）：
  - `evaluate_arm(harness_dir, *, split, basket, memory_version, arm, run_id) -> ArmResult`
  - `compare_paired(a, b) -> PairedTest`：胜率用 McNemar 精确检验，连续量用配对 bootstrap B=10000；返回 `method/p/delta/significant/direction`
  - `evaluate_generation(gen, parent, winner, *, run_id) -> dict`：只返回 report 的指标段，MOD-07 负责合并，MOD-09 原子写入

  MOD-07 不得自行实现统计检验。
- **FR-08-22** 生成 `metrics/generations.csv`（每代追加一行）与 `metrics/plots_data/gen-XXX_entropy.json`。
- **FR-08-23 / 24** 主指标聚合路径**结构上不可**读取 `reward`/`cum_reward`（INV-7）：`EpisodeResult` 不含这两个字段；报告落盘后写 `report.sha256`，同参数重跑必须一致，否则抛 `E_EVAL_NONDETERMINISTIC`（**2026-10-11 D-M3-3 裁决 A 修订**：该硬门按臂分层执行——**仅本地臂**；API 臂记 `eval.nondeterministic{determinism=api_nondeterministic}` 而不抛，见 NFR-08-01）。

## 5. 非功能需求（NFR-08-x：确定性/统计严谨/可复现/可观测）

- **NFR-08-01（确定性）** 所有随机性来自显式 seed：bootstrap 用 `np.random.default_rng(rng_seed)`，`rng_seed = int.from_bytes(sha256(f"{run_id}:{generation}:{basket}".encode()).digest()[:8])`，禁 `random.*`（INV-6 + MOD-09 `guards.py` AST 扫描）；环境部分由 seed 完全决定、模型部分在 `temperature=0` 下尽力确定，后端不保证时必须显式降级（`E_EVAL_NONDETERMINISTIC` 告警 + `n_rep=3`），不得静默。
  - **⚠️ 2026-10-11（D-M3-3 裁决 A，Spec v2.9 / B-65）——确定性口径按臂分层。** 两个独立事实：① `check_report_determinism` 从 M1 起**没有任何生产调用点**（只有单测调用）⇒「同参数重跑 `report.sha256` 必须一致」这条承诺**在真实 run 里从未被执行过**；② 当前主力臂 `C1` 是 **API 臂**（`configs/models.yaml` 标 `deepseek-flash` 为 `api_supports_seed: false`，实测字节相同的请求 9 次返回 9 个不同响应）⇒ **端到端逐字节可复现在物理上不成立**。
  - **裁决**：把可复现性承诺**精确化到能兑现的层级**——**可复现层**（代码 / 配置 / seed / 聚合口径 / 并发归一化，即 `n_workers=1` 与 `n>1` 逐字节一致）⇒ **本地臂硬执行**；**LLM 层** ⇒ 不承诺逐字节，采样照记、响应按 request hash 归档、变化即标 `llm.nondeterministic`（MOD-03 既有口径）；**臂内含任何 `provider=api` 的角色** ⇒ 跳过硬门、发 `eval.nondeterministic` 事件（`determinism=api_nondeterministic`，含 `repro_scope`），**不抛 `E_EVAL_NONDETERMINISTIC`**。
  - **执行点**：`evals/harness.py::enforce_report_determinism(run_dir, generation, report, *, app_cfg)`，生产调用点 = `evolve/generation.py` 的**写报告之前**（先判可复现性，再落盘）。`reproducibility_of(app_cfg)` 是判定函数，**fail-closed 方向**：配置不可解析 ⇒ 判「不可复现」（误判为可复现会让 API run 假报 `E_EVAL_NONDETERMINISTIC`，代价 ≈¥47/代；误判为不可复现只是少执行一次检查）。
  - **⚠️ 诚实边界**：这**降低了「可复现」的强度**，必须在 M3 报告里显式声明「本轮无逐字节复现证据」；**不得**据此宣称「API 臂的 M3 结果可逐字节复现」。
- **NFR-08-02（统计严谨）** 任何"提升"结论必须给点估计 + 区间 + 检验；配对比较用 McNemar，次级指标用 BH-FDR(q=0.10)，消融用 Holm–Bonferroni。
- **NFR-08-03（可复现）** `config_hash`/`harness_version`/`seeds_hash`/`bootstrap_seed` 全部写入报告；§12 命令可直接复制执行（G6）。
- **NFR-08-04（可观测）** 每次评估 ≥1 行 `eval_index.jsonl`；埋点走 MOD-09 结构化日志，**禁止自建日志格式**（Spec §8.2）。
- **NFR-08-05（成本）** 耗时由 Spec v1.3 §8.6 成本模型推出：单局 ≈ `D·t_d`（默认局，8B）。**2026-10-05 真实实测重标定**：`D` 不是常数、`t_d` 也不是 3 s——8B+MLX 上实测单局模型时间 **≈19.6 min**（n=10 均值：`D` 均值 97.1、中位 69、max 245；`calls/决策点` 1.62；`t_d` p50 7.46 s），故 **n=40 的 heldout 约 13.1 h（按均值）到 29–34 h（按重尾上界）**，而非原估 3.3 h。局级并发共享同一推理队列（`max_inflight=1`，P-10），实测 `d_wall / d_model = 1.02`，**`n_workers` 对总墙钟几乎无贡献**（`n` 局墙钟 ≈ `n × 单局模型时间`），只能掩盖 env/IO 开销。报告必须写实测 `dur_s`，不设不可达的 wall-clock 目标。推导与逐局明细见 [M1 验收报告 §14.18/§14.19](../reviews/2026-10-04-M1验收报告.md)。。**2026-10-05 实测修正**（n=10 `fixed_prompt`，`runs/m1-real-h1n10b/`）：`t_d` 实测 p50 **7.46 s**（不是 3 s）、`D` 均值 **97.1**（`decisions_llm` 口径，中位 69、max 245）、`calls/决策点 1.62`，故单局模型时间 **19.6 min**、n=10 墙钟 **3.32 h**；且 `d_wall / d_model = 1.02`，**`n_workers` 对总墙钟几乎无贡献**（`max_inflight=1`），`n` 局墙钟 ≈ `n × 单局模型时间`——本条的「并发掩盖 env/IO 开销」只在模型不是瓶颈时才成立，实测里模型就是瓶颈。

## 6. 接口契约（真实 Python 签名 + JSON Schema）

```python
# dice_ratchet/evals/harness.py
Split = Literal["train","heldout","test"]; BasketId = Literal["H1","H2","H3","test_full","train_window"]
@dataclass(frozen=True)
class EvalSpec:   # run_id, generation, harness_version, split, basket, seeds, agent_kind, max_steps, n_rep, n_workers, seed_timeout_s, budget_seconds
    ...
def assert_split_isolation(split: Split, seeds: Sequence[int]) -> None: ...
def guard_prompt_context(*, split: Split, seed: int, text: str, destination: Literal["prompt","memory","reflection"]) -> None: ...
def resolve_basket(basket: BasketId, *, generation: int, protocol: SeedProtocol) -> tuple[int, ...]: ...
# dice_ratchet/evals/seeds.py —— Spec §4.7 规定的 seed 唯一出口（INV-5）；MOD-06/07 不得自行切分
def take(split: Split, *, generation: int | None = None, basket: BasketId | None = None) -> list[int]: ...   # train 需 generation，heldout 需 basket
def run_eval(spec: EvalSpec, *, runner: EpisodeRunner, writer: TraceWriter, protocol: SeedProtocol) -> EvalResult: ...
def evaluate_arm(harness_dir: Path, *, split: Split, basket: BasketId, memory_version: int, arm: str, run_id: str) -> ArmResult: ...   # Spec §4.7
def compare_paired(a: ArmResult, b: ArmResult) -> PairedTest: ...                                                            # Spec §4.7
def evaluate_generation(generation: int, parent: ArmResult, winner: ArmResult | None, *, run_id: str) -> dict: ...            # Spec §4.7，返回 report 指标段
# dice_ratchet/evals/metrics.py
KIND_SPACE = ("play_face","discard_die","purify","use_ability","end_turn","map_choose","shop_buy","shop_refresh",
              "shop_remove","shop_leave","event_choose","treasure_pick","forge_pick","shrine_choose","rest_choose","relic_swap")  # 16，= Spec §4.3 agent 可用 kind
MAIN_METRIC_SOURCES = ("info.outcome","info.truncation_reason","info.stages_cleared","info.hearts_left","info.battles_won")   # INV-7 白名单
def wilson_ci(wins: int, n: int, z: float = 1.959964) -> tuple[float, float]: ...
def normalized_entropy(counts: Mapping[str,int], *, kind_space: int = 16, base: float = 2.0) -> float: ...
def action_entropy_curve(results: Sequence[EpisodeResult], *, t_max: int = 120, min_support: int = 3) -> list[float|None]: ...   # 横轴 = decision_index
def bootstrap_mean_ci(values: Sequence[float], *, B: int = 10000, rng: np.random.Generator) -> tuple[float,float]: ...
def paired_bootstrap_gap(a: Sequence[float], b: Sequence[float], *, B: int = 10000, rng: np.random.Generator) -> tuple[float, tuple[float,float]]: ...
def mcnemar_exact_p(a: Sequence[bool], b: Sequence[bool], alternative: Literal["two-sided","greater","less"]) -> float: ...
def aggregate(results: Sequence[EpisodeResult], *, t_max: int = 120) -> SplitMetrics: ...
def render_report_sections(*, generation: int, harness_version: int, parent_version: int, heldout: SplitMetrics, train: SplitMetrics, gain: GainVerdict, diversity: CollapseVerdict, ablation: Mapping[str,float], decision: str, decided_by: Literal["auto","human"], reason: str) -> dict: ...
# 便捷访问器（签名同形，(results: Sequence[EpisodeResult]) -> float）：win_rate / mean_stages_cleared / mean_hearts_left / mean_battles_won / mean_decisions / illegal_rate / fallback_rate / truncation_rate
# dice_ratchet/evals/diversity.py
def js_divergence(p: Mapping[str,float], q: Mapping[str,float], *, base: float = 2.0) -> float: ...
def entropy_slope(curve: Sequence[float|None]) -> float: ...   # 非 null 点上的 OLS 斜率
def collapse_check(*, curves: Sequence[Sequence[float|None]], jsd_kind: Sequence[float], win_rates: Sequence[float], cfg: CollapseConfig) -> CollapseVerdict: ...
# 便捷函数（签名同形）：kind_distribution / slot_distribution(results) -> dict[str,float]；distinct_sequence_ratio(results) -> float
# dice_ratchet/evals/ablation.py
AblationArm = Literal["full","no_reflection","no_self_edit"]
def run_ablation(generation: int, *, arms: Sequence[AblationArm], run_id: str, split_basket: BasketId = "H2", alpha: float = 0.05) -> AblationReport: ...
def attribution(full: Sequence[bool], arm: Sequence[bool], *, alpha: float = 0.05) -> str: ...
# dice_ratchet/evals/baselines.py
class BaselineAgent(Protocol):   # name: str; reset(seed) -> None; act(obs, legal) -> Action
# RandomAgent: rng = np.random.default_rng(seed ^ 0x5EED) 在 legal_actions 上均匀采样；HeuristicAgent: 委托 MOD-04 agent.heuristic.heuristic_policy(obs, legal)
# fixed_prompt **没有**自己的 agent 类：build_agent 返回 MOD-04 PolicyAgent(mode="baseline_fixed_prompt")
#   （SandboxRunner 检测 play_decision 即走决策点循环）：harness_version 恒 0、temperature=0、
#   tools_enabled=false、memory_top_k=0、reflection_max=0、冻结模板 agent.policy.fixed_prompt_path（F22）
EPISODE_PROTOCOL = "episode-protocol-v2"   # 逐局摘要的「测量协议」标识（单局语义变了就递增）
def baseline_protocol(kind: str, *, config: Any = None,
                      harness_dir: Path | str = Path("harness")) -> str: ...
    # baseline --resume 的一致性键：EPISODE_PROTOCOL（fixed_prompt 再拼上冻结模板摘要）
def fixed_prompt_policy_config(config: Any = None) -> PolicyConfig: ...   # 基线专用冻结策略配置
def build_agent(kind: str, *, seed: int, harness_version: int = 0, client: Any = None,
                config: Any = None, harness_dir: Path | str = Path("harness")) -> Any: ...
    # fixed_prompt 无 client → E_LLM_UNAVAILABLE（fail-closed，绝不静默降级成启发式）
def run_baseline(kind, *, split, basket, run_id, run_dir=None, config=None, protocol=None,
                 client=None, model_hash="", git_commit="", n_workers=1, generation=0,
                 episode_config=None, archive=True, write_summaries=True,
                 harness_dir=Path("harness"), resume=False, model_backend="none",
                 seed_timeout_s=None) -> ArmResult: ...   # resume=True 时跳过已完成的 seed（F18）
def archive_baseline(kind: str, split: Split, metrics: SplitMetrics, *, run_id: str,
                     model_hash: str, git_commit: str, seeds=None, seeds_hash=None,
                     protocol_hash=None, model_backend="none", seed_timeout_s=None,
                     resumed_n=0, ...) -> Path: ...   # 溯源字段（F6）
def load_episode_results(run_dir, split, *, generation=None, harness_version=0, seeds=None,
                         arm: str | Sequence[str] = "", exclude_arms=()) -> list[EpisodeResult]: ...
    # arm/exclude_arms 是**真过滤**（F5）：episodes/<split>/ 下同时住着 parent/candidates/smoke/baseline-*
```

`report.json` schema：唯一文件为 `schemas/generation_report.schema.json`（Spec v1.3 §4.5 / B-16，由 MOD-09 登记），本模块不再单独维护副本。与 v0.1 相比有以下变化：

- `generation ≥ 0`，其中 `gen-000` 是 v0 基线，`decision="baseline"`。
- `heldout` 段的字段改为 INV-7 新指标，并加入 `basket`、`fallback_rate`、`truncation_rate`、`degraded_n`。
- `verdict` 的枚举增加 `insufficient`（`low_confidence` 时使用）。
- 新增 `candidates[]` 与 `episode_config_hash`，由 MOD-07 填写。
- `ablation` 可以为 `null`（非消融代）。

## 7. 数据模型与存储

```python
@dataclass(frozen=True)
class EpisodeResult:      # INV-7：结构上不含 reward / cum_reward，主指标无法误用 shaped reward
    seed: int; split: Split; generation: int; harness_version: int
    outcome: Literal["win","lose"]; truncation_reason: str | None      # 截断计 lose（Spec §8.6）
    stages_cleared: int; hearts_left: int; battles_won: int
    decisions: int; steps: int; illegal_attempts: int; fallback_decisions: int
    kind_counts: tuple[int, ...]   # 长度 16，对齐 KIND_SPACE
    slot_counts: Mapping[str, int]; seq_hash: str    # sha256(动作序列)，供 distinct_sequence_ratio
    dropped: bool = False; degraded: bool = False
@dataclass(frozen=True)
class SplitMetrics:
    split: Split; basket: BasketId; n: int; n_dropped: int; low_confidence: bool
    win_rate: float; win_rate_ci95: tuple[float,float]; mean_stages_cleared: float; mean_hearts_left: float
    mean_battles_won: float; mean_decisions: float; illegal_rate: float; fallback_rate: float
    truncation_rate: float; degraded_n: int; entropy_mean_norm: float
    action_entropy_curve: tuple[float | None, ...]
@dataclass(frozen=True)
class GainVerdict:
    win_rate_gap: float; gap_threshold: float; verdict: str; collapse_veto: bool
    mcnemar_p: float | None; bootstrap_gap_ci95: tuple[float,float]
@dataclass(frozen=True)
class CollapseVerdict:    # collapse_reasons 落 ext 文件，不进 report.json
    entropy_slope: float; collapse_flag: bool; jsd_kind: float | None; collapse_reasons: tuple[str, ...]
```

| 路径（`runs/<run_id>/` 下） | 内容 | 可覆盖 |
|---|---|---|
| `generations/gen-003/report.json` + `report.sha256` | 严格 Spec §4.5 schema + G3 证据 | 否 |
| `generations/gen-003/test_attestation.json` | test 解锁与结果存证 | 否 |
| `metrics/generations.csv` | 每代一行；绘图与单调性判据来源 | 追加 |
| `metrics/gen-003.ext.json` | `jsd_kind`/`collapse_reasons`/`power_note`/`mcnemar_p`/`low_confidence` | 追加 |
| `metrics/plots_data/gen-003_entropy.json` | tidy：`{"generation":3,"buckets":[{"turn":1,"h_norm":0.71,"n":40}]}` | 否 |
| `baselines/<kind>-<split>.json` + `baselines/registry.csv` | 基线归档（M1 交付物） | 否 |
| `ablation/gen-003.json` | 消融三臂结果 + 归因 | 否 |
| （v0.2 删除 `eval/eval_index.jsonl`，改为 `events.jsonl` 中的 `eval.report` 事件） | — | — |

数值序列化统一 `round(x, 4)`；Spec §4.5 示例中的 2 位小数仅为展示。**已裁决（Spec v1.4 B-34）**：报告精度统一为 4 位小数。

## 8. 关键流程（伪代码或时序）

**8.1 代际评估主流程**
```
evaluate_generation(g, harness_version, split, basket):
  seeds = resolve_basket(basket, generation=g, protocol=load_seeds())   # load_seeds 由 MOD-09 提供
  assert_split_isolation(split, seeds)                                  # FR-08-01/05
  spec = EvalSpec(run_id, g, harness_version, split, basket, seeds, ...)
  with EpisodePool(n_workers, shared_client) as ex:   # 局级并发 + 共享推理队列（P-10），按 seed 排序收集
      for seed in seeds: ex.submit(play_one, seed, spec)
  heldout = aggregate(sorted(results, key=lambda r: r.seed))            # 整数累加后单次相除
  train   = aggregate(train_window(g))                                  # 滚动窗口，默认 60 局
  gain = judge_gain(train, heldout, parent_metrics, rng)                # §8.2
  div  = collapse_check(curves_by_gen, jsd_series, win_rates, cfg)      # §8.3
  sections = render_report_sections(...)   # 交给 MOD-07 合并 decision 段，MOD-09 write_json_atomic + sha256
```
**8.2 `verdict` 有序规则（短路求值）**
```
gap = train.win_rate - heldout.win_rate
p_improve = mcnemar_exact_p(cand, parent, "greater")   # 配对：cand 与 parent 跑同一 H1 篮子
p_regress = mcnemar_exact_p(cand, parent, "less")
if p_regress < alpha:                                       verdict = "regress"
elif gap > cfg.gap_threshold:                               verdict = "overfit_suspect"
elif p_improve < alpha and gap <= threshold and not collapse_flag:  verdict = "real_gain"
elif collapse_flag:                                         verdict = "null"; collapse_veto = True
else:                                                       verdict = "null"
```
**n=40 的现实约束（必须写入报告 `power_note`）**：配对 McNemar 精确检验在 n=40 下最小可判显著的不一致对数 `k=6`（k=6 双尾 p=0.031，k=5 p=0.0625）。故 `verdict` 实际由 `gap` 主导、显著性为辅——"未判 `real_gain`" **不等于**"无提升"，只是证据不足。

> **2026-10-06（M2 开工 D-3 裁决，Spec §8.6 / B-39）：把 `k=6@n=40` 换算成效应量，并据此换主判据。**
> 精确配对 McNemar 在真实 `+5pp` 胜率增益（`p01 − p10 = 0.05`）下的功效：每 seed 不一致率 q=0.10 时 n=40→**0.03**、n=80→0.17、n=160→0.43、n=320→0.78；q=0.05 时 n=40→0.01、n=160→0.82。**即 `k=6@n=40` 等价于「只能判 ≥15pp 的净跳变」**（n=40 下 Δ=15pp→power 0.57、20pp→0.84）。结论：
> 1. **`win_rate` 不再承担 4–5pp 量级的结论**，它在 H=40 上是粗门（<15pp 不出结论），保留为第一报告指标；
> 2. **4–5pp 量级的增益改由 `mean_stages_cleared`（0–6）的配对 bootstrap 承担**：n=40、配对 sd 0.5 / 0.8 时，Δ=0.30 关（= 6 关制的 **5.0pp**）→ power **0.97 / 0.66**（Δ=0.50 关 → 1.00 / 0.98）；
> 3. `H` 保持 **40**（H1 篮子 2000–2039 定义即 40；H>40 会动 B-07 seed 协议、占用 H2 消融篮子，且 H 每 +10 局在 C=1 下是 +20 局 = **+6.5 h/代**，H=50 即顶破 `loop.max_episodes_per_generation=120`）；
> 4. `n_rep` 保持 1（B-14 已定 `eval_temperature=0.0` + `decode_seed=0`）；全面 `n_rep=3` 在 heldout 臂上是 **+52 h/代**，故改为：M3 前做**一次性确定性验证**（H1 前 5 个 seed 重跑对 sha256，≈1.6 h），失败再走既有的 `E_EVAL_NONDETERMINISTIC → n_rep=3` 升级路径；
> 5. `evals.gap_threshold` 与 `evolve.gap_threshold` 本次保持 0.15（H 未变，不随 H 重标）；等 M2 §3.2 冒烟产出第一批 train/heldout 配对后按实测 `win_rate_gap` 分布决定是否重标。

**8.3 `collapse_flag` 触发条件（对齐调研 §4 失败模式 2）**

| # | 条件 | 默认阈值 | 含义 |
|---|---|---|---|
| C1 | `entropy_slope <= -0.010` | -0.010 | 动作分布随回合快速收敛 |
| C2 | `entropy_mean_norm(g) < 0.60 × entropy_mean_norm(gen_0)` | 0.60 | 相对首代多样性退火 |
| C3 | `jsd_kind(g,g-1) < 0.010` 且 `win_rate` 提升 ≥0.05 且连续 2 代 | 0.010 | 行为分布冻结但分数上涨 → 疑似刷分/自我确认 |
| C4 | `distinct_sequence_ratio < 0.50` | 0.50 | 轨迹高度重复（坍缩最直接表征） |

`collapse_flag = C4 OR (C1/C2/C3 任一在连续 2 代触发)`（两点确认，避免单代噪声）；`collapse_reasons` 记录被触发的条件 ID。

**8.4 消融与归因**
```
run_ablation(g, arms=[full,no_reflection,no_self_edit], split_basket="H2"):   # 同一批 40 seed，配对
  no_reflection: 禁用 MOD-06 反思触发 + MOD-05 反思条目检索注入（策略记忆保留）
  no_self_edit : harness 冻结于 v0（prompts/tools 只读），反思与记忆演化保留
  full         : 当前 harness_version；三臂同 seed 同预算，臂内并行、臂间串行
  delta_full[arm] = full.win_rate - arm.win_rate
  attribution = {reflection|self_edit|both|none}   # 由 Holm–Bonferroni(α=0.025) 下显著项决定
```
**8.5 时序（一次代际，含 MOD-07）**：`MOD-06 收尾(g)` → `evaluate_generation(g, train_window)` → `MOD-07 生成候选` → 每候选在 H1 上与 parent 同 seed 评估 → `judge_gain` → `collapse_check` → `report.json` + sha256 → `MOD-07 gate` 决策 →（HITL）`approve|rollback` → 追加 `generations.csv`。

## 9. 配置项（键名、类型、默认值、含义）

```yaml
# configs/seeds.yaml
version: 1; hash_algo: sha256
splits:
  train:   {start: 1000, end: 1999, per_generation: 30, consume: in_order, expose_to: [prompt, memory, reflection]}
  heldout:
    expose_to: []
    baskets:
      H1: {start: 731000, end: 731039, role: generation_judge}   # F45-2 起六位端口空间
      H2: {start: 731040, end: 731079, role: ablation_final}
      H3: {start: 731080, end: 731099, role: reserve}
  test:    {start: 947000, end: 947099, role: final_acceptance, locked: true, unlock_policy: single_use_per_milestone}
exposure_guard:
  destinations_forbidden: [prompt, memory, reflection]
  numeric_scan: true            # 文本中 3–7 位数字 token 落 731000-731099 / 947000-947099 即告警
  forbidden_tokens: [heldout, H1, H2, H3, "731000-731099", "947000-947099"]
seeds_hash: "sha256:686b2618330db5eb8291c1810499dc07fc47a956da5711803015bbb09ec2e904"  # F45-2 重算；由 CLI 生成、CI 校验；改动必须同步更新并在 PR 说明
# configs/default.yaml 的 eval 段
eval:
  n_heldout: 40; n_test: 100; n_ablation: 40        # 依次为 H1 / test_full / H2 篮子
  bootstrap_B: 10000; alpha: 0.05; fdr_q: 0.10; gap_threshold: 0.15
  t_max: 120; n_workers: 2;   # 步数/决策上限读 Spec §8.6 的 episode.*，本模块不另设
  seed_timeout_s: 14400; min_support: 3; drop_rate_max: 0.10   # 墙钟预算；2026-10-05 实测标定（见 NFR-08-05：n_workers 不放大单局墙钟）
  budget_seconds: null; entropy: {base: 2.0, kind_space: 16}
  collapse: {c1_slope_threshold: -0.010, c2_ratio_threshold: 0.60, c3_jsd_freeze: 0.010,
             c3_winrate_jump: 0.05, c4_seq_ratio: 0.50, confirm_generations: 2}
  determinism: {eval_temperature: 0.0,   # Spec §8.1 B-14，MOD-03 强制
                 train_temperature: 0.7, n_rep: 1, n_rep_nondeterministic: 3, decode_seed: 0}
  baseline: {kinds: [random, heuristic, fixed_prompt], archive_dir: baselines, fixed_prompt_harness_version: 0}
  ablation: {arms: [full, no_reflection, no_self_edit], split_basket: H2}
```

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀 E_EVAL_*）

| 错误码 | 触发 | 严重度 | 降级行为 |
|---|---|---|---|
| `E_EVAL_INV5_LEAK` | prompt/memory/reflection 含 heldout/test seed，或非 train 局写记忆 | P0 | 立即终止该局标 `dropped`；整代评估作废并要人工复核；禁出 `real_gain` |
| `E_EVAL_SEED_OVERLAP` / `E_EVAL_TEST_LOCKED` | 三类区间相交 / 未解锁即跑 test | P0 / P1 | 拒绝启动（必须修 yaml）；test 直接拒绝执行 |
| `E_EVAL_SEED_EXHAUSTED` | train 切块越界 | P1 | 停止演化，提示扩大 train 池或降低 K |
| `E_EVAL_INSUFFICIENT_N` / `E_EVAL_BUDGET_EXCEEDED` | 有效 n 不足、`dropped/n > drop_rate_max`、软预算耗尽 | P1 / P2 | 出报告但 `low_confidence=true`，`verdict` 只能为 `null`/`regress`（保守，对齐 Spec §8.5） |
| `E_EVAL_TIMEOUT` | 单局超过 `evals.seed_timeout_s`（协同预算到期，或硬看门狗放弃 worker） | P2 | 该局记 `dropped=true` + `error_code=E_EVAL_TIMEOUT`，**计 n 并按 lose 计**；整批超时进 `EvalResult.error_codes`（M1 验收 F20） |
| `E_EVAL_NONDETERMINISTIC` | 重跑 sha256 不一致 | P2 | 转 `n_rep=3` 多数票；报告标注，禁 `real_gain` |
| `E_EVAL_SCHEMA` | `report.json` 校验失败 | P1 | 不写正式路径，保留 `.invalid.json` 供排查（不得静默丢弃） |
| `E_EVAL_BASELINE_MISSING` / `E_EVAL_ARM_NOT_ALLOWED` | `--baseline all` 但归档缺失 / 请求未知消融臂 | P2 | 报告 `baselines: null` 并标注对照缺失；拒绝该臂，其余继续 |

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例；含指标计算的手算校验用例）

单测 `tests/unit/evals/`（覆盖率 ≥80%）：

- **T-01 Wilson CI 手算**：给定 wins=3, n=5（p̂=0.6）→ 当 `wilson_ci(3,5)` → 则返回 `(0.2307, 0.8824)`（z²=3.841459；中心 0.556523，半宽 0.325828），容差 1e-4；边界 wins=0,n=40 → 则 ≈`(0.0000, 0.0876)`；n=0 → 则抛 `E_EVAL_INSUFFICIENT_N`。
- **T-02 归一化熵手算**：给定 `kind_counts={play_face:6, end_turn:2}`、`kind_space=16` → 则原始熵 `0.811278` bits、`normalized_entropy=0.811278/log2(16)=0.202820`（容差 1e-6）。v0.1 的手算值 `0.313852` 有误，按 log2(6) 的正确值是 0.313845。
- **T-03 JSD 手算**：给定 P=(1.0,0.0)、Q=(0.8,0.2) → 则 `js_divergence=0.108031`（0.5·(0.152003+0.064060)）；P=Q → 则 0.0；含零概率项不得产生 NaN。
- **T-04 熵曲线与斜率**：给定 3 条构造轨迹、`min_support=3` → 则曲线长度 48 且无支撑回合为 `null`；给定单调下降曲线 → 则 `entropy_slope < 0`。
- **T-05 主指标与非法率公式**：给定 outcome=`["win","lose","win"]`、stages_cleared=`[6,3,6]`、hearts_left=`[1,0,2]` → 则 `win_rate=0.6667`、`mean_stages_cleared=5.0`、`mean_hearts_left=1.0`；另给定 1 局 `truncation_reason="policy_failure"` → 按 lose 计入 n；给定步级非法 3、总步 300 → 则 `illegal_rate=0.01`（局级 2/10 只进 ext）。
- **T-06 INV-7 结构检查**：给定对 `metrics.py` 做 AST 扫描 → 则主指标计算路径不出现 `reward`/`cum_reward` 标识符。
- **T-07 INV-5 数值护栏**：给定 `guard_prompt_context(text="seed 2043 …", split="heldout")` → 则抛 `E_EVAL_INV5_LEAK`；给定 `text="hp 23, dice 4"` → 则通过（无误报）。
- **T-08 INV-5 记忆拦截**：给定 split="heldout" 触发 `memory.write` → 则抛 `E_EVAL_INV5_LEAK`，且 `episodes/*.jsonl` 无 `reflect` 事件。
- **T-09 确定性**：给定同一 `EvalResult` 与 `rng_seed` → 当两次跑 bootstrap → 则 CI 逐位相同、`report.sha256` 一致。
- **T-10 verdict + McNemar 手算**：给定 4 组 `(gap, p_improve, p_regress, collapse_flag)` 参数化用例 → 则依次返回 `regress/overfit_suspect/real_gain/null`，且 `collapse_flag=true` 时永不返回 `real_gain`；给定 b=6,c=0 → 则双尾 p=0.03125、单尾 0.015625，b=5,c=0 → 则双尾 0.0625（不显著）。
- **T-11 坍缩两点确认**：给定 C1 仅第 g 代触发 → 则 `collapse_flag=false`；第 g、g+1 连续触发 → 则 `true`。
- **T-12 基线复用**：给定 `HeuristicAgent` 与 MOD-04 `heuristic_policy` 在同一 obs 上 → 则选同一动作（防逻辑分叉）。
- **T-19 单局预算真的被消费**（M1 验收 F20）：给定 `timeout_s` 极小 → 当 `SandboxRunner.run_episode()`（逐 env 步循环与决策点循环各一例）→ 则结果 `dropped=true`、`error_code=E_EVAL_TIMEOUT`、`win=false`，且 `aggregate()` 计入 n 与 `n_dropped`；给定一个**无视预算**的 runner → 当 `run_eval()` → 则硬看门狗在 `seed_timeout_s + 60 s` 内放弃该 worker、记 dropped，**其余 seed 仍被新线程池接着跑**，且被放弃的池以 `shutdown(wait=False)` 收尾（`test_abandoned_pools_are_not_joined`）；给定预算充足 → 则不得误报 dropped。
- **T-20 逐局增量落盘与续跑**（M1 验收 F18）：给定 `on_result` 钩子 → 当 `run_eval()` 跑 3 局 → 则回调顺序 = 完成顺序且每次落一个 `episodes/<split>/*.jsonl`；给定删掉一局后的 run → 当 `run_baseline(resume=True)` → 则只补那一局（`pending_seeds=(seed,)`）、`resumed_n=2`、聚合仍是 n=3；给定无剩余 seed → 则不重复归档。**两道一致性闸**：逐局摘要带 `protocol` 字段（`EPISODE_PROTOCOL`，`fixed_prompt` 再拼上冻结模板摘要 `baseline_protocol()`）；`--resume` 见到不同协议（含 F22 修复前的无字段旧局、或换过提示词模板）→ 抛 `E_EVAL_ARM_NOT_ALLOWED` 拒绝续跑，不允许把两套单局语义拼进同一份聚合。
- **T-21 臂过滤**（M1 验收 F5）：给定同一 `episodes/<split>/` 下混装 `parent` / `candidates` / `baseline-*` → 当 `load_episode_results(arm=…/exclude_arms=…)` → 则按行内的 `arm` **真过滤**且结果带出 `arm`；给定 `eval report` / `eval diversity` → 则 `baseline-*` 三类臂不进入 held-out/train 分母。用例：`tests/unit/evals/test_evals_longrun_guards.py`。
- **T-22 `fixed_prompt` 的推理粒度**（M1 验收 F22）：给定按 prompt 的 `legal_actions` 段组 plan 并显式 `end_turn` 的假客户端 → 当 `run_baseline("fixed_prompt", …)` 跑真实沙箱 → 则 `决策点数 ≤ 调用数 ≤ 1.25 × 决策点数` 且 `steps ≥ 2 × 调用数`（修复前是逐 env 步：`调用数 == env 步数`）。用例：`tests/integration/evals/test_m1_blocker_guards.py`。
- **T-23 归档溯源**（M1 验收 F6）：给定 `archive_baseline(..., git_commit="abc123", seeds_hash=…, model_backend=…)` → 则归档 JSON 与 `registry.csv` 的对应字段逐一同值；`model_hash` 无客户端时为 `none`。用例：`tests/unit/evals/test_evals_baselines.py`。

集成 `tests/integration/`：

- **T-13 Golden 指标快照**：给定固定 5 seed 的 `episodes/*.jsonl` 夹具 → 当 `aggregate()` → 则与 `tests/golden/evals/split_metrics.json` 逐字段一致。
- **T-14 报告 schema round-trip**：给定 `render_report()` 输出 → 则通过 `GenerationReport` 校验，且 `json.loads(json.dumps(report))` 与原对象等值（G5）。
- **T-15 并行=串行**：给定同一 spec，`n_workers=1` vs `4` → 则 `SplitMetrics` 与 `report.json` 逐字节相同（G3）。
- **T-16 蜜罐 + 消融端到端**：给定在 heldout 局注入含 `2043` 的假 prompt → 则 `audit leak` 命中且 `E_EVAL_INV5_LEAK` 被记录（G4）；给定 3 臂 × 40 seed 小规模夹具 → 则产出 `ablation/gen-XXX.json`，含 `delta_full`/`mcnemar_p`/`attribution`。
- **T-17 可复现命令**：给定 §12 中每条命令 → 则在干净 run 上复制执行成功（G6）。
- **T-18 PRD 测试路径守门**（M1 验收 F9 防退化）：给定 `docs/prd/*.md` 全文 → 当提取其中每个 `tests/...` 路径 token → 则每个 token 或文件/目录存在、或 glob 能命中、或为 `*`/`<...>`/`{...}` 命名模式（占位符不是可复制路径，故放宽）、或命中白名单（MOD-05/06/07 未实现模块的前瞻登记，条目在模块落地后自动失效）→ 否则失败并列出「文档:行号 + 写错的路径 + 建议候选」。用例：`tests/integration/test_prd_paths.py`（Spec §9 G6）。

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6 与计划 §6 三项验收）

- **G1**：`pytest tests/unit/evals --cov=dice_ratchet.evals --cov-fail-under=80` 通过（MOD-08 属纯函数模块）。**G2**：指标 golden 快照存在（T-13）；改动需显式更新并在 PR 说明。**G3**：同 `(run_id, generation, split, seeds)` 重跑 `report.sha256` 一致（T-09/T-15）——**2026-10-11 D-M3-3 裁决 A：按臂分层执行**（本地臂硬门 → `E_EVAL_NONDETERMINISTIC`；API 臂记 `eval.nondeterministic`，**不抛**；执行点 `enforce_report_determinism` 在 `evolve/generation.py` 写报告之前）。
- **G4**：`audit leak --run-id <run_id>` 在 eval run 上 0 告警，蜜罐注入必被抓到（T-16）。**G5**：`report.json` / `metrics/*.json` / `seeds.yaml` 均有 schema + round-trip 测试（T-14）。**G6**：下列命令可直接复制执行成功：

```bash
python -m dice_ratchet baseline --kind random|heuristic|fixed_prompt --split heldout --basket H1
python -m dice_ratchet eval report --split heldout --baseline all --run-id <run_id>
python -m dice_ratchet eval diversity --run-id <run_id> --generation 3
python -m dice_ratchet eval ablation --run-id <run_id> --generation 3 --arms no_reflection,no_self_edit
python -m dice_ratchet eval test --run-id <run_id> --unlock-once --attestation runs/<run_id>/generations/gen-003/test_attestation.json
```

**已回填 Spec v1.3 §4.6**：`--basket` 选项、`eval ablation`、`eval test` 子命令。

| 计划 §6 验收 | 判定指标 | 判定命令 | 通过条件 |
|---|---|---|---|
| 1. 闭环自动运行 ≥3 轮演化且 held-out 单调不降 | **`generations.csv.heldout_win_rate`**（第一判定量，D-M3-1 裁决 A）+ `ci_lo/ci_hi`；**且接受率 > 0** | `eval report --split heldout --run-id <run_id>` + **`eval acceptance --run-id <run_id>`** | g=1..3 均满足 `ci_lo(g) >= win_rate(g-1) - 0.02` 或 `P(gap>0) >= 0.90`；`monotonic_ok=true`（**判定器 = `eval acceptance`**，见 §13）；**且 ≥1 次 accept**（否则「每代都拒绝」也算单调不降，属退化通过——MOD-07 §12）。**样本资格（D-M3-4 裁决 A，2026-10-11）**：本轮跑 `--hitl=false`，`decided_by="auto"` **计入**验收样本，前提是 `condition_trace` 完整（C0–C5 六项齐全，每项带 `passed`/`value`/`threshold`）；报告须显式记录「本轮无人工确认」，**不得**把 auto 决策描述为「经人工复核」。**基线选择偏差标注（F70，2026-10-14，用户裁决「只做 ② 标注」）**：上一代 `decision="accept"` ⇒ 本代起点基线就是**那个候选自己的 heldout 测量**（含 winner's curse，n=40 下约 +5–10 pp 量级）⇒ `acceptance.json` / ext 的 `baseline_selection` 逐对给 `baseline_selected` + `baseline_source` + 一句边界；**不重测、不换篮子**（花钱的复测与破坏同篮子可比性的独立子集都不做） |
| 2. ≥1 个指标相对基线有可归因提升（含消融） | `ablation.delta_full` + `attribution` + 基线对照；**4–5pp 量级判在 `mean_stages_cleared` 的配对 bootstrap 上**（D-3：`win_rate` 在 n=40 下只能判 ≥15pp） | `eval ablation …` + `eval report --baseline all` | H2 上至少一臂显著劣于 full（Holm–Bonferroni α=0.025），或相对最好基线的 Wilson CI 不重叠；`attribution != "none"`；**基线对照以 `mean_stages_cleared` 为主**（hard×6 的 `fixed_prompt` 基线 `win_rate=0.0%`） |
| 3. 增益真实性通过（gap < 阈值、无坍缩） | `gain_realism.verdict`、`diversity.collapse_flag`、test 复核 | `eval report` + `eval test --unlock-once` | `verdict ∈ {real_gain, null}` 且 `collapse_flag=false`；test 上 `win_rate` 的 Wilson CI 下界 > 最好基线点估计，且与 heldout 点估计差 ≤0.20 |

## 13. 指标与埋点

- **主指标（INV-7 白名单来源）**：`win_rate`（**第一判定量**），`mean_stages_cleared` / `mean_hearts_left` / `mean_battles_won`（并列次指标）。
  - **2026-10-06（M2 开工 D-3 裁决，Spec §8.6 / B-39）判据分工**：`win_rate` 是第一判定量，但**在 H=40（H1 篮子）下只能判 ≥15pp 的净跳变**（见 §8.2 的功效换算），故 M3 的「4–5pp 量级增益」结论**由 `mean_stages_cleared` 的配对 bootstrap 承担**（Δ=0.30 关 = 6 关制的 5.0pp，n=40 下 power 0.66–0.97），`mean_battles_won` 作并列证据。报告必须同时给出两个口径的 `delta + CI + p + method`，并保留 `power_note`。
  - **2026-10-11（D-M3-1 裁决 A，Spec v2.7 / B-63）**：**M3 主口径 = `hard × 6`，`medium × 6` 降为「外部效度」报告项**（不进判据判定路径）。判据 1 的轴 = `heldout.win_rate` + `ci_lo/ci_hi` + ≥1 次 accept；判据 2 的 4–5pp 量级仍由 `mean_stages_cleared` 承担。**诚实边界**：`C1` 在 hard×6 上 `win_rate=10%` ⇒ 判据 1 由 1–2 局抖动主导，**通过只等于「无显著退步」**（原「hard×6 上 `win_rate` 无方差可用」的表述针对 `fixed_prompt` 基线 0.0% 与 8B 时代，`C1` 上实为 10%，两者都不足以单独支撑提升结论）；判据 2 的基线对照因此以 `mean_stages_cleared` 为主。
- **次级指标与判定量**：`illegal_rate`、`entropy_mean_norm`、`entropy_slope`、`jsd_kind`、`jsd_slot`、`distinct_sequence_ratio`、`llm_latency_ms_p50/p95`、`mean_cum_reward_debug`（**仅调试，禁止进入 verdict**）；判定量为 `win_rate_gap`、`verdict`、`collapse_flag`、`attribution`。
- **埋点事件**（统一经 MOD-09 `emit` 写入 `events.jsonl`）：`eval.start{spec}`、`eval.seed_start{seed,split,basket}`、`eval.seed_done{seed,outcome,steps,decisions,dur_s,dropped,degraded,error_code}`、`eval.report{n,n_dropped,degraded_n,dur_s,report_sha256}`、`eval.guard_violation{error_code,split,seed,destination,matched_token}`、`eval.collapse{generation,reasons}`、`eval.test_unlock{generation,attestation_path,decided_by}`、**`eval.nondeterministic{generation,determinism,reason,repro_scope}`（2026-10-11 新增，D-M3-3：API 臂跳过硬门时必须可观测——「G3 未执行」不得变成新的静默缺口）**。
- **坍缩判据 `c3` 的三处缺陷已修（v0.13 / F49，Spec v2.15 B-71）**：`c3`（「分布冻结 + 胜率跳变」）**从未在生产里触发过**（实测 17 个归档代的 `jsd_kind` 全空）。三个独立原因：① 生成侧**从不落 kind 分布** ⇒ 本模块的重算路径只能写死 `jsds = [None] * (n_gen-1)`（`evals/cli.py:401` 的硬编码）；② `evaluate_generation` 的 **pair 错位**（`jsd(parent,winner)` 配 `win_rates[i] - win_rates[i-1]`，不是同一对）；③ `confirm_generations=2` 对 `c3` **结构上不可达**（`c3_flags` 只在 `i ≥ 1` 有定义，两代数据下恒不触发；要三代连续「冻结+跳变」才确认）。**处置**：新增 `metrics/kind_distribution/gen-XXX.json`（`write/read_kind_distribution`）+ `adjacent_jsd_kind`/`aligned_jsd_kinds`（等长对齐、同一 pair），**`c3` 改为单代即触发**（同 C4：直接证据不适用两点确认）；**缺测记 `None` 而非 `0.0`**（0.0 会被读成「分布完全没变」⇒ 误报冻结）。`c1`/`c2`/`c4` 的 `confirm_generations` 不变。
- **C5 的 `tol` 与坍缩阈值的标定现状（v0.13 / Spec v2.15 B-72）**：**可算 MDE，但标不出标定值**。① **零成本可算**（`evals/tol_sensitivity.py` + `eval acceptance --sensitivity`）：`illegal`（步级，分母 27,836 尝试/代）`tol=0.05` ≈ **1,392 个步级事件**（≈48 个 se）；`fallback`（9,021 决策）≈ **451 个**（≈115 个 se）；`truncation`（局级，单臂 40 局）≈ **2 局** ⇒ **truncation 是约束轴**，**三轴不可互换**。② **现有数据不足以标定**：`c2` 的定义是「相对**第 0 代**的比例」⇒ 需要 ≥2 代实测，而归档里只有 1 个 run 有 2 代；`c1` 是**局内**（决策点序）斜率，不是代际斜率。③ **诚实边界**：所有阈值（C5 三个 `tol` + `c1/c2/c3/c4`）**仍是「起点值 + 待回填」**，**不是实测标定值**；M3 报告必须写明。标定工具：`scripts/run_m3_calibration.sh --free`。
- **判据 1 的判定器（v0.12 / F-M3-9，Spec v2.13 B-69）**：`metrics/acceptance.json` + `eval report` 的 ext 段 `acceptance`；实现 `evals/acceptance.py::evaluate_acceptance`。**三处口径**（原文没写、本次补）：① `g−1` = **上一个已存在且 `split="heldout"` 的代**，不是字面 `g−1`（`gen-000` 是基线，拿它当上一代会把「基线 vs 第一代」混进判据 1）；**第一代不参与判定**（无比较对象），但**至少要有一对可比**。② **`P(gap>0)` 的定义**：**相邻两代 heldout 的 `mean_stages_cleared` 的配对 bootstrap** 中 `Δ>0` 的比例，`B` = `eval.bootstrap_B`，RNG = `bootstrap_seed(run_id, generation, "acceptance")`；**只在两代共有的 seed 上配对**；无共有 seed ⇒ `null`（**不得记 0.0**——0.0 会被读成「显著退步」）。**⚠️ 同名异义校正**：本判定表原文的 `P(gap>0)` 与 `GainVerdict.win_rate_gap` （= `train − heldout`）同名但语义不同——`P(train > heldout)` 恰是判据 1 **不**想要的（判据 1 要「heldout 不降」）。③ 指标值以 `metrics/generations.csv` 为准（**必须按 `split="heldout"` 过滤 + 按代去重**，F48 之后台账每代每 split 一行），**接受记录以各代 `report.json` 的 `decision` 为准**（F43 的权威）。`eval acceptance` **总是返回 0**（判定器不是守卫；不通过时原因写 stderr）。`n < evolve.min_heldout_n` ⇒ fail-closed（`ok=false` + `reasons`）。
  **2026-10-14（F70）新增字段**：`GenerationAcceptance.baseline_selected` / `baseline_source`、`AcceptanceReport.selected_baseline_pairs` / `baseline_selection_note`，`as_dict()` 里汇总为 `baseline_selection: {selected_pairs, note}`（只有真的存在选择后的基线时 `note` 才非空）。**为什么放在判定器里**：这条偏差影响的是「基线」这个参考点，而参考点正是判据 1 的读数前提。
- **`generation` 是真过滤，臂是显式的（v0.8 / F46-A，Spec B-59）**：`load_episode_results` / `read_episode_protocols` / `evaluate_generation` 的训练窗口读取在给定 `generation` 时只保留该代的局（行里的 `generation` 优先，缺失按文件名 `-g{gen:03d}-` 解析，两者都判不出 → 跳过并发 `evals.generation_unresolved`），且**不再覆盖** `EpisodeResult.generation`。`eval report --arm`：缺省 = 该代 `report.json` 里的候选臂（优先 `decision="accept"`，无则唯一候选，再退 `parent`）；`all` = **显式申报**的混合池（ext 标 `mixed_pool=true`，禁止当默认）。实测对拍（`runs/m2-s32b-smoke-c1-medium6`）：候选臂 `n=37 / 0.6757`、parent 臂 `n=40 / 0.6250`，与 MOD-07 报告一致；修复前是混代混臂的 `n=117 / 0.6496`。
- **臂归档与复用（v0.8 / F42-A/B，Spec B-60/B-61）**：每个臂落 `metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json`（`ArmResult.as_dict()` + provenance + 逐局明细）。复用是 **fail-closed**：provenance 逐字段全等（`ARM_PROVENANCE_KEYS` = generation/split/basket/arm/harness_version/memory_version/config_hash/seeds_hash/episode_config_hash，不含 `created_at`）且覆盖完整 seed 列表 → **整臂复用**（0 局 + `evals.arm_reused`）；只覆盖一部分 → 只跑缺的局；任一不一致/损坏/run 目录不存在 → **不复用**。提案另有独立检查点 `generations/gen{gen:03d}/proposals/proposal.json`（指纹一致 → 同 candidate id、0 次 proposer 调用）。
- **绘图数据（MOD-09 消费）**：`metrics/generations.csv` 列固定为 `generation,harness_version,parent_version,split,basket,arm,n,win_rate,ci_lo,ci_hi,mean_stages_cleared,mean_hearts_left,mean_battles_won,mean_decisions,illegal_rate,fallback_rate,truncation_rate,entropy_mean_norm,entropy_slope,jsd_kind,collapse_flag,win_rate_gap,verdict,decision,bootstrap_seed,config_hash,git_commit,ts`。
  - **✅ 2026-10-12（F57，Spec v2.20 / B-77）——代际 `heldout` 恒取「起点臂」，台账多了 `arm` 列**：
    ① **`evaluate_generation` 的 `heldout` 段恒取 `parent.metrics`**（不再 `winner.metrics if winner else parent.metrics`）。
    理由：判据 1 问的是「**这一代的起点在哪**」，候选的测量是**代内**比较。修复前的现场（`runs/_sweep/m3-rehearsal` gen-001）：
    同一批产物，`loop run` 写下 parent 的 `0.10`（据此判「单调性不成立」），`eval report` 按候选臂重算后写成 `0.35`
    （据此判 `monotonic_ok=true`）——**同一批局两个相反结论**。`harness_version` 仍取本代**结束**时在役的版本
    （accept 即新版本；台账连续性靠它），`parent_version` 表示 `heldout` **测在哪份 harness 上**（F57 起恒等于起点臂版本）。
    ② **报告新增 `heldout.source`**：`measured` = 本代真测，`inherited:gen-00X` = 跨代继承第 X 代的同一臂测量
    （见 MOD-07 §8.1 的继承规则）。**继承来的代不写 heldout 台账行**（沿用 F48「没有测量就不写行」的先例）。
    ③ **列清单加 `arm`（28 列）**，upsert 键升为 **`(generation, split, arm)`**：`(generation, split)` 双键在同代有
    parent 与候选两套测量时**不足以区分它们** ⇒ 后写的**无声覆盖**先写的。三键之后两行并列共存。
    ④ **`eval report` 默认臂 = `parent`**（原为「胜出候选 → 唯一候选 → parent」）；臂名不存在时**报错**
    （`E_EVAL_ARM_NOT_ALLOWED`，原来是静默回落到 parent）；`--arm all`（混合池）**不写台账**。
    ⑤ **`restore_arm_from_archive`**：把**另一代**的臂归档还原成 `ArmResult`（不跑任何局），校验「同 harness 版本 +
    seed 覆盖完整 + 自洽 basket」，任一不满足返回 `None` ⇒ 调用方**重测**（fail-closed）。
    与 `evaluate_arm` 的 `arm_reused` 分支的区别：后者要求 provenance 的 `generation` **相同**（同代重跑）。
  - **✅ 2026-10-11（F48，Spec v2.12 / B-68）——台账现在是「每代每 split 一行」**：除上面这一行 **heldout** 之外，**只要该代真的有训练局（`train.n > 0`）就再写一行 `split=train`**（basket=`train_window`）。原先只写 heldout 行 ⇒ 8 个归档 run 的台账 **train 行数全为 0**，`train_heldout_gap.png` 与 `win_rate.png` 的 train 对照曲线**无源**、`win_rate_gap`（判据 3 的量）在台账里**没有可核验来源**。**upsert 键是 `(generation, split)`**（修复前是 `generation` 单键 ⇒ 加上 train 行后两个 split 会互相覆盖），且读入时**折叠历史重复行**（实测 `runs/g6-eval-smoke` 同代同值 5 行）。**列清单的字面唯一副本在 `infra/plotting.py`**（IO 工件 schema；`infra → evals` 被 NFR-09-5 分层门禁禁止），本模块的 `GENERATIONS_COLUMNS` 直接引用它——B-06 的「列定义归 MOD-08」解释为**口径归本模块、字面清单唯一副本在 IO 层**。

## 14. 依赖与风险

依赖：MOD-01（原生 `info.outcome/stages_cleared/hearts_left/battles_won`）、MOD-02（`SandboxEnv` 门面、轨迹审计）、MOD-09（config、JSONL、CLI、`run_id`/hash、绘图、`guards.py`）、MOD-04（`heuristic_policy` 供 heuristic 复用）、MOD-05（反思条目开关供消融）、MOD-06（train seed 消耗顺序）。

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| R-1 | n=40 功效不足，把噪声当增益 | 虚假 `real_gain` | `gap` 主导 + 配对设计 + M3 用 test n=100 复核；报告强制写 `power_note` |
| R-2 / R-5 | LLM 采样非确定破坏 G3；4 worker 并发抢同一模型实例显存（24GB / 8B） | 无法复现、崩溃或静默降级 | 评估 `temperature=0`，后端不保证时 `n_rep=3` + `E_EVAL_NONDETERMINISTIC` 告警；评估期共享单实例并限并发，超时记 `dropped` |
| R-3 | heldout seed 泄漏进 prompt/memory（米哈游教训 / INV-5） | 全部结论作废 | 三重护栏：结构拦截 + 文本扫描 + `audit leak`；命中即 P0 |
| R-4 | 次级指标多重比较致假阳性 | 误判增益 | BH-FDR(q=0.10)；主指标预注册单次检验，不做校正 |
| R-6 | `fixed_prompt` 基线随 harness 漂移 | 基线不可比 | 基线钉死 `harness_version=0` + `model_hash` 入档；归档不可覆盖 |
| R-7 | 坍缩阈值凭经验设定，误报/漏报 | 错误否决或漏检 | M3 用基线熵曲线标定阈值；`confirm_generations=2` 降误报；`collapse_reasons` 全量留证 |
| R-8 | train 与 heldout 难度不一致（seed 分布偏差） | `gap` 系统性偏移 | 两侧样本量足够大；同时记录 `stages_cleared` 分布并在 ext 文件告警 |

## 15. 里程碑与工作量估算（人日）

| 里程碑 | 交付 | 工作量 |
|---|---|---|
| M1 | `baselines.py` 三基线 + 归档 + `registry.csv`；主指标与 Wilson CI；`eval report` 骨架 | 4 |
| M2 | `harness.py` 执行器（并行/预算/超时/dropped）+ `report.json` schema + `generations.csv` + 重跑确定性 | 5 |
| M3 | `diversity.py`（熵曲线/斜率/JSD/`collapse_flag`）+ `ablation.py`（3 臂 + 归因）+ test 终检与存证 + 阈值标定 | 6 |
| 横切 | INV-5 三重护栏 + 蜜罐用例 + golden 指标快照 + README 命令 | 3 |
| **合计** | | **18 人日**（含约 +15% 评审与返工缓冲） |

**实际交付（M1）**：`dice_ratchet/evals/`（`seeds.py` / `metrics.py` / `harness.py` / `baselines.py`）于
**2026-10-04** 交付（提交 `bff9982`），并**提前**交付了原属 M3 的 `diversity.py` / `ablation.py` 与 test 单次解锁存证。
本项目**未单独记工**；按提交时间窗折算 ≤ 2 人日（计划 M1 部分 4 人日），偏差 ≥ 50%。
M1 验收的 **F1（三基线之一假入档）**、**F3（评估 run 上 `audit leak` 误判 critical）** 已在本模块修复；
真实 8B 跑批后三条 held-out 分数已入档（**最终口径 run `runs/m1-real-h1n10b/`**，seed 2000–2009、n=10、三基线同源；旧 `runs/m1-real-h1n40/` 只有 random/heuristic 的 n=40 与中止在 18% 的 fixed_prompt），`format_robustness.json` 以 `m1_evidence=true` 通过（**2026-10-06 按 M1 验收复核 M-1 用当前快照重跑 n=200 覆盖归档**：首答 0.94 → **0.995**、`schema_fail_rate` 0.005 → **0.0**）。

**§13.2 前置项收口（2026-10-05，`<本轮提交>`）**：本模块的 5 项全部闭环 ——
**F5**（`load_episode_results` 的 `arm`/`exclude_arms` 真过滤，基线臂不再进入代际分母，T-21）、
**F6**（归档与 `registry.csv` 的溯源字段真实化，T-23）、
**F18**（`on_result` 逐局增量落盘 + `baseline --resume`，T-20）、
**F20**（`E_EVAL_TIMEOUT` + 协同预算 + 硬看门狗，T-19）、
**F22**（`fixed_prompt` 改走 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")`，推理粒度回到决策点，T-22）。
新增 `tests/unit/evals/test_evals_longrun_guards.py`（9 条）与 `tests/integration/evals/test_m1_blocker_guards.py`
的 F22 用例；`tests/fixtures/evals/episodes/heldout/*.jsonl` 的 `arm` 由 `baseline-heuristic` 更正为
`parent`（它们充当的是「某一代的 held-out 局」），**golden 指标快照逐字节不变**。

**n=10 `fixed_prompt` 归档与实测速率（2026-10-05）**：`fixed_prompt` 基线的 n=40 因实测成本（≈29–34 h）
按用户决定缩到 n=10，已在最终契约上跑完并归档于 `runs/m1-real-h1n10b/`（seed 2000–2009，三基线同 run 同源，
`--seeds` 显式集合、`seed_timeout_s=14400`、`n_workers=2`、`git_commit=2d6274f`）：`n=10 / degraded_n=0 /
n_dropped=0`、10 局 `protocol` 完全一致、`audit leak` clean / 退出码 0、`eval report` 产出
`generations/gen-000/report.json` + `report.sha256`（`heldout.n=0`，基线局不进代际分母，F5 生效）。
结果：`random` 0.0%（CI [0.000, 0.278]）、`heuristic` 40.0%（CI [0.168, 0.687]）、
**`fixed_prompt` 0/10 = 0.0%（Wilson 95% CI [0.000, 0.278]）**。
**实测速率**（[M1 验收报告 §14.19](../reviews/2026-10-04-M1验收报告.md)）：n=10 **墙钟 3.32 h**、模型时间 3.26 h、
1,551 次后端调用（`calls/决策点 = 1.62`）、`t_d` p50 **7.46 s** / p95 10.13 s、schema 全失败率 5.8%、
`fallback` 122 次、`illegal_rate=0.3021`、`fallback_rate=0.181`；`D` 均值 **97.1**（中位 69、min 33、max 245，
跨局差 7.4 倍）。**两条口径结论**：① `d_wall / d_model = 1.02`，即 `max_inflight=1` 下
**「2 worker 共享一个模型」不产生墙钟收益**，`n` 局墙钟 ≈ `n × 单局模型时间`；② FR-08-12 的
`D` 应取该 agent 在 H1 上的**实测分布**（本版 `D` 中位 69 / 均值 97.1，而 §14.18 用于外推的 n40e 两局是
185/207 的重尾高端），预算按上界留余量。

## 16. 开放问题

1. **test 池与冗余子集**：947000–947099（n=100，F45-2 前为 9000–9099）在 M3 只允许一次性使用；M4 前是否需要扩成两个不相交 test 篮子以支持"跨代增益 vs 推理时增益"长期对比？另：若 train 池耗尽后循环复用同一 seed，代际 `win_rate` 比较是否仍有效（倾向：禁止复用，耗尽即停）？
2. **熵的归一化分母**：固定 `log2(6)`（可跨代比较）vs 每回合 `log2(|legal kinds|)`（更公平但不可比）——当前取前者，需用 M3 基线数据验证是否掩盖收敛；坍缩阈值 C1/C2/C4 是否应按游戏阶段分段而非全局单一阈值？
3. **`mean_turns` 定义**：取终局 `obs.turn` 还是 `steps`？依赖 MOD-01 对 `turn` 最终语义的确认（当前取 `turn`，备选 `steps` 写入 ext）。
4. **模型更新的可比性**：阶段一换 27B Q4 后，历史 `report.json` 是否因 `model_hash` 不同而标记不可比，并强制重跑全部基线？
5. **人在环覆盖与消融预算**：HITL 覆盖 `verdict` 时写 `decided_by="human"` 并保留自动结论，是否需在 schema 增加 `overridden_verdict`（涉及 Spec §4.5 变更 → 待回填）？`no_self_edit` 冻结臂当前不消耗 train 池、仅在 H2 评估，是否应改为同样消耗以对齐预算？

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v0.1 | 2026-09-30 | 首版 |
| v0.2 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：指标改为 INV-7 新口径（outcome/stages_cleared/hearts_left/battles_won），新增 fallback/truncation/degraded（P0-1）；步数上限改读 Spec §8.6，删除 `max_steps=60`（P0-2）；NFR-08-05 改由成本模型推出（P0-3）；`KIND_SPACE` 改为 16 个 GAME kind，熵曲线横轴改为决策点；修正 T-02 手算值；dropped 计为 lose（防幸存者偏差）；新增 FR-08-25 MOD-07 门面（P1-3/4）；report schema 改为引用唯一文件（P1-5/6）；heuristic 改为委托 `heuristic_policy`（P1-16）；事件改名为 `eval.*` |
| v0.3 | 2026-09-30 17:49 | 报告精度回填为 Spec B-34；CLI 标注改为已回填 §4.6 |
| v0.4 | 2026-10-05 | M1 验收 §13.2 收口：FR-08-07…10 增加「单局超时真的执行（F20）」与「逐局增量落盘 + 续跑（F18）」；FR-08-11/12 增加溯源字段要求（F6）；FR-08-13/14 明确 `fixed_prompt` 走 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")`、每决策点一次推理（F22）；§6 签名表更新（`fixed_prompt_policy_config`/`build_agent`/`run_baseline`/`archive_baseline`/`load_episode_results` 的 `arm` 过滤）；§10 增加 `E_EVAL_TIMEOUT`；§11 新增 T-19（超时）/T-20（续跑）/T-21（臂过滤）/T-22（推理粒度）/T-23（归档溯源）与 subagent 登记的 T-18（PRD 路径守门，F9）；§15 更新实际交付说明 |
| v0.5 | 2026-10-05 | **n=10 `fixed_prompt` 归档回填**：§15 补归档事实与实测速率（run `m1-real-h1n10b`，seed 2000–2009，`n=10 / degraded_n=0 / n_dropped=0`、`fixed_prompt` 0/10 = 0.0%、墙钟 3.32 h、`t_d` p50 7.46 s、`calls/决策点` 1.62、schema 全失败率 5.8%、`D` 均值 97.1）；NFR-08-05 与 FR-08-10 的 `seed_timeout_s` 推导补上实测值，并写明 **`d_wall / d_model = 1.02`：`n_workers` 对总墙钟几乎无贡献**（`max_inflight=1`，`n` 局墙钟 ≈ `n × 单局模型时间`）。数字来源见 [M1 验收报告 §14.19](../reviews/2026-10-04-M1验收报告.md) |
| v0.6 | 2026-10-06 | **按 M1 验收复核更正陈旧口径与示例值**（不新增功能）：① §15 那句「三条 held-out 分数已入档（`runs/m1-real-h1/baselines/`）」是 v1.5 之前的旧口径——已改为最终 run `runs/m1-real-h1n10b/`（seed 2000–2009、n=10、三基线同源），并注明旧 `runs/m1-real-h1n40/` 只有 random/heuristic 的 n=40 与中止在 18% 的 fixed_prompt；② §15 补 `format_robustness.json` 的**工件新鲜度**要求（2026-10-06 重跑覆盖：首答 0.94 → **0.995**、`schema_fail_rate` 0.005 → **0.0**）；③ **NFR-08-05** 由「单局 ≈5 min、n=40 ≈3.3 h」改为实测口径（单局模型时间 **≈19.6 min**、`D` 均值 97.1 / 中位 69 / max 245、`calls/决策点` 1.62、`t_d` p50 7.46 s → n=40 约 **13.1 h（均值）～29–34 h（上界）**），并写明 `d_wall / d_model = 1.02`（`n_workers` 不缩短总墙钟）；④ 示例配置 `seed_timeout_s` 5400 → **14400**。依据 [M1 验收复核 §M-1](../reviews/2026-10-05-M1验收复核.md) 与 [M1 验收报告 §14.18/§14.19](../reviews/2026-10-04-M1验收报告.md) |
| v0.7 | 2026-10-10 | **`VERDICT_DECISIONS` 纳入 `error_reject`（F47-D，对齐 Spec v2.5 B-58）**。MOD-07 在 accept/rollback 抛 `InfraError`（如 `E_EVOLVE_GIT_FAILED`：脏工作区）时记 `decision="error_reject"`——它是「阈值内但落版本失败」的**唯一可见信号**，因此 `append_generations_csv` 的 upsert 必须像对待 `accept`/`reject`/`rollback` 一样**保留它**，`eval report` 对这类代也要写 sidecar：否则重算的 `pending` 会把它盖掉，§3.2 的「机制性失败为零」判据就无从查证。护栏：`tests/unit/evals/test_evals_harness.py::test_f47_error_reject_is_a_verdict_and_survives_recompute`（修复前 `"error_reject" in VERDICT_DECISIONS` 失败）。 |
| v0.x-2026-10-10 | 2026-10-10 | **F43 与 F46（对齐 Spec v2.4 B-52/B-55）**。① **F43**：`eval report` 与 MOD-07 `_finish_report` 写**同一个** `generations/gen-NNN/report.json`，而判据②要的 `metrics/generations.csv` 只有 `eval report` 会产 ⇒ 「满足②」只能靠「抹掉①的判决」。修法：MOD-07 在代末自己追写 csv（新增 `generations_row_from_report`，列集与 `_csv_row` 共用 `GENERATIONS_COLUMNS`）；`eval report` 对**已裁决**的代改写 sidecar `report.eval.json`；`append_generations_csv` 按 `generation` **upsert**（每代恰好一行）并保留已裁决的 `decision`/`verdict`。② **F46（待裁决）**：`load_episode_results` 的 `generation` 参数**只贴标签、不筛文件** ⇒ `eval report --generation 1` 把 gen-000 基线的 `arm="parent"`（40 局）也算进 gen-1，实测 `n=117`（= g000/parent 40 + g001/parent 40 + g001/cand 37 有效）而真实只有 40+40+37。**台账每代数字因此没有物理含义**——这正是 M1 验收 F5 修过的「混**臂**聚合」的同类残留（那次修了 arm、没修 generation） |
| v0.14 | 2026-10-14 | **判据 1 的基线选择偏差标注 + `eval report` 的 split 写入边界（F70 / F61+F71，对齐 Spec v2.26 B-83）**。① **F70（用户裁决「只做 ② 标注」）**：上一代 `decision="accept"` ⇒ 本代起点基线就是**那个候选自己的 heldout 测量**（它是在这 40 个 seed 上「通过了门禁」才被留下的）⇒ 含 **winner's curse**（n=40 下约 +5–10 pp 量级）；`AcceptanceReport` 新增 `baseline_selected`/`baseline_source`/`selected_baseline_pairs`/`baseline_selection_note`，`as_dict()` 汇总为 `baseline_selection`。**为什么必须进产物**：不标注会让「held-out 单调不降」的参考点被当成无偏。**不重测、不换篮子**（裁决）。② **F61**：`_resolve_report_arm(split=)` 补 train 侧命名（`train` → `parent-smoke` → 通用回落）——修复前 `eval report --split train` 抛 `E_EVAL_ARM_NOT_ALLOWED`（现场退出码 2）。③ **F71**：`eval report` 会把 `heldout` 槽位的聚合写进 `report.json` 的 `heldout` 段并派生一条 `split="heldout"` 的台账行 ⇒ `--split train` 在 **baseline/pending 代**（不在 `VERDICT_DECISIONS` 里、不受 sidecar 保护）会**覆盖权威报告的 heldout 段**（实测 before/after：`n=40/win_rate=0.175/measured` 被 train 聚合替换）并污染 `plots_data`；新增 `_WRITABLE_SPLITS=("heldout",)`，非 heldout split **只读**（打印 + `metrics/gen-NNN.<split>.readonly.json`，无消费者）。④ **护栏 5 条**（F70 2 条 + F61/F71 3 条，修复前全部失败；F71 的 before 证据是「真的写了 report.json」与「gen-000 的报告被逐字节改写」）。**成本：¥0** |
| v0.13 | 2026-10-11 | **`c3` 的三处缺陷 + C5 `tol` 的 MDE 换算（F49，对齐 Spec v2.15 B-71/B-72）**。① **`c3` 从未触发**（实测 17 个归档代 `jsd_kind` 全空），三个独立原因见 §13 新增条目。② **处置**：`kind_distribution/` 落盘 + `aligned_jsd_kinds`（等长对齐、同一 pair）+ **`c3` 单代即触发** + 缺测记 `None` 而非 `0.0`。③ **`evals/tol_sensitivity.py`** + `eval acceptance --sensitivity`：把 `tol=0.05` 按轴换算成原始单位——`illegal`≈1,392 个步级事件、`fallback`≈451 个、`truncation`≈**2 局**。④ **实测**：修复后两代数据下 `c3` 正确触发；三种反例（跳变不足/分布真变/jsd 缺测）不误报；修复前两代下恒 `collapse=False`。⑤ **⚠️ 诚实边界**：**阈值本身仍未经标定**（「起点值 + 待回填」），M3 报告必须写明。⑥ **成本**：¥0 |
| v0.12 | 2026-10-11 | **判据 1 的判定器（F-M3-9，对齐 Spec v2.13 B-69）**。① **现场**：§12 判定表第 1 行要求 `monotonic_ok=true`，而 `grep -rn monotonic_ok dice_ratchet/ schemas/ scripts/` **0 命中**（只在文档里出现 3 次）⇒ 口径写了两处、**判定器一处都没写**；M3 跑完 3 代无法机读结论。② **新增** `evals/acceptance.py`（`evaluate_acceptance`/`write_acceptance`/`AcceptanceReport`/`GenerationAcceptance`）+ `eval acceptance` 子命令 + `eval report` 的 ext 段 `acceptance`。③ §12 判定表第 1 行补 `eval acceptance`；§13 补三处口径：`g−1` 的定义、**`P(gap>0)` 的定义**、两个数据来源的分工。④ **校正一处同名异义**：原文的 `P(gap>0)` 与 `GainVerdict.win_rate_gap`（= `train − heldout`）同名但语义不同——`P(train > heldout)` 恰是判据 1 **不**想要的。⑤ **退出码**：`eval acceptance` **总是 0**（判定器 ≠ 守卫；把「验收不合格」编码成退出码会让「命令失败」与「实验结论是否定」混淆）。⑥ **实测**：归档 run 上 `monotonic_ok=false`（`comparable=0`，只有 gen-1 一代）+ `accepts=0` ⇒ `ok=false`；12 条新用例；**修复前 `ModuleNotFoundError` + 生产代码 0 命中**。⑦ **诚实边界**：`P(gap>0)` 用**全代非基线臂**的逐局 `stages_cleared`（同 seed 多臂取 max），不绑定具体臂名。⑧ **成本**：¥0 |
| v0.12 | 2026-10-12 | **处置 F57：代际口径定死为「起点臂」+ 台账 `arm` 三键 + 报告 `heldout.source`（Spec v2.20 B-77）**。① `evaluate_generation` 的 `heldout` **恒取 `parent.metrics`**（diversity/gain 同步同臂）——判据 1 问的是「起点在哪」；修复前同批产物可给出两个相反结论（parent `0.10` ↔ 候选 `0.35`）。② 报告新增 `heldout.source`（`measured` / `inherited:gen-00X`），继承的代**不写 heldout 台账行**。③ 列清单加 `arm`、upsert 升为 `(generation, split, arm)` 三键。④ `eval report` 默认臂 = parent、未知臂名报错、`--arm all` 不写台账。⑤ 新增 `restore_arm_from_archive`（跨代还原，fail-closed）。 |
| v0.11 | 2026-10-11 | **样本资格口径澄清（D-M3-4 裁决 A，对齐 Spec v2.10 B-66）**。① **矛盾现场**：MOD-07 §14 风险④ 写「`--hitl=false` 跑批量实验（标 `decided_by="auto"`，**不计入 M3 验收样本**）」，而 MOD-07 §12 / 本文件 §12 判定表第 1 行又要求「**≥1 次 accept**」——全自动跑时 accept 出自 auto、按原文不计入样本；改用 `--hitl` 则每代有人工延迟，且 24 h 无响应会 `hitl_timeout` **保守回滚**（一代白跑 ≈4.8 h / ≈¥100）。② **裁决**：M3 跑 `--hitl=false` 全程自动；**样本资格 = `decided_by="auto"` + `condition_trace` 完整**（C0–C5 六项齐全，每项带 `passed`/`value`/`threshold`）⇒ 计入验收样本；缺 trace 的 auto 决策不计入。③ **§12 判定表第 1 行**补该样本资格口径与「报告须显式记录『本轮无人工确认』」。④ **依据**：M2 准出 run（`runs/m2-s32b-smoke-c1-medium6`）**事实就是走 auto 路径**（归档 `decided_by="auto"`、`decision="reject"`、`reason="C2:no_significant_gain"`，`candidates/{id}.json.verdict.condition_trace` 完整）⇒ 本裁决是**追认既有做法**。⑤ **⚠️ 诚实边界**：auto accept 的说服力弱于人工确认 —— 报告**不得**把 auto 决策描述为「经人工复核」。⑥ 本版为**纯文档**变更，零成本 |
| v0.10 | 2026-10-11 | **确定性口径按臂分层（D-M3-3 裁决 A，对齐 Spec v2.9 B-65）**。① **现场（两个独立事实，都是实测）**：(a) `check_report_determinism`（FR-08-23/24 / G3）从 M1 起**没有任何生产调用点**——`grep -rn check_report_determinism dice_ratchet/` 只命中它自己的定义与 `__all__` ⇒「同参数重跑 `report.sha256` 必须一致，否则抛 `E_EVAL_NONDETERMINISTIC`」这条承诺**在真实 run 里从未被执行过**，它只是一句文档；(b) 当前主力臂 `C1` 是 **API 臂**（`deepseek-flash` 标 `api_supports_seed: false`，实测字节相同的请求 9 次返回 9 个不同响应）⇒ **端到端逐字节可复现在物理上不成立**。② **裁决**：把承诺**精确化到能兑现的层级**——**可复现层**（代码/配置/seed/聚合口径/并发归一化）本地臂**硬执行**；**LLM 层**不承诺逐字节（采样照记 + 响应按 request hash 归档 + 变化标 `llm.nondeterministic`，MOD-03 既有口径）；**臂内含任何 `provider=api` 的角色**⇒ 跳过硬门、发**新增事件 `eval.nondeterministic{generation,determinism,reason,repro_scope}`**，**不抛** `E_EVAL_NONDETERMINISTIC`。③ **执行点**：`evals/harness.py::enforce_report_determinism(run_dir, generation, report, *, app_cfg)`，生产调用点 = `evolve/generation.py` **写报告之前**（先判可复现性再落盘）；判定函数 `reproducibility_of(app_cfg)` **fail-closed 方向**为「不可复现」（误判为可复现会让 API run 假报 `E_EVAL_NONDETERMINISTIC`，≈¥47/代；误判为不可复现只是少执行一次检查）。④ **护栏**：10 条新用例（本地臂硬门仍抛 ×1、API 臂不抛 ×1、事件可观测 ×1、本地臂**不得**发该事件 ×1、`reproducibility_of` 双向 ×3、新事件已登记 ×1、**生产调用点存在且早于写报告** ×1、reason JSON-safe ×1）；**修复前 `ImportError`（`enforce_report_determinism` 不存在）**，另用反事实脚本实测旧硬门在 API 臂漂移上确实抛 `E_EVAL_NONDETERMINISTIC`。⑤ **⚠️ 诚实边界**：这**降低了「可复现」的强度**，M3 报告必须显式声明「本轮无逐字节复现证据」，**不得**宣称「API 臂结果可逐字节复现」；`n_rep` 仍是 1（`n_rep=3` 在 heldout 臂上是 +52 h/代，D-3 已否决）。⑥ **成本**：¥0 |
| v0.9 | 2026-10-11 | **M3 主口径落定（D-M3-1 裁决 A，对齐 Spec v2.7 B-63）**。① §13 主指标口径改写：`win_rate` 是**第一判定量**（原写「第一主指标」），`mean_stages_cleared`/`mean_hearts_left`/`mean_battles_won` 并列次指标；**`hard × 6` 为 M3 唯一主口径，`medium × 6` 降为外部效度报告项**（不进判据判定路径）。② **§12 判定表第 1 行补齐两处**：判定量写明是第一判定量 `heldout_win_rate`，通过条件补 **「且 ≥1 次 accept」**（原表漏写——MOD-07 §12 早有该条款，否则「每代都拒绝」也算单调不降，属退化通过）；第 2 行补「4–5pp 量级判在 `mean_stages_cleared` 的配对 bootstrap 上」与「基线对照以 `mean_stages_cleared` 为主」（hard×6 的 `fixed_prompt` 基线 `win_rate=0.0%`）。③ **诚实边界**：原「hard × 6 上 `win_rate` 无方差可用」的表述只对 `fixed_prompt` 基线（0.0%）与 8B 成立，`C1` 上实为 10%——两者都不足以单独支撑提升结论；`C1` 在 10% 附近由 1–2 局抖动主导 ⇒ **判据 1 通过只等于「无显著退步」**。④ **未做**：hard×6 三基线**从未在新六位 H1（731000–731039）上测过**，判据 2 在新协议下尚无实测锚点（起跑前必须补，付费需用户点头）。⑤ 本版为**纯文档**变更，零成本 |
| v0.8 | 2026-10-11 | **F46-A / F42-A+B+C / F45-2（对齐 Spec v2.6 B-59..B-62）**。① **F46-A**：`generation` 由「只贴标签」改为**真过滤**（原先还覆盖行里的值）；`eval report --arm`（缺省=该代候选臂、`all`=显式混合池）；坍缩判定改用该代整池。实测对拍：候选臂 `n=37 / 0.6757`、parent 臂 `n=40 / 0.6250`，与 MOD-07 报告逐项一致（修复前 `n=117 / 0.6496`）；已裁决代仍只写 sidecar（F43 规则不变）。② **F42-A+B+C**：`evaluate_arm` 转发 `resume_results`/`on_result` 并落 `metrics/arms/` 臂归档（provenance + 逐局明细）+ fail-closed 复用；提案检查点（评估前落盘 + 输入指纹，resume 命中即 0 次 proposer 调用）；`n_workers`/`n_rep` 按配置转发。③ **F45-2**：heldout 731000–731099（H1/H2/H3）、test 947000–947099，`seeds_hash` 重算为 `sha256:686b2618330db5eb8291c1810499dc07fc47a956da5711803015bbb09ec2e904`；`baseline_protocol()` 与 ext provenance 绑上 seeds 协议标签（代际隔离闸）；`numeric_scan` 与 `_NUM4_RE` 由 4 位改为 3–7 位（六位 seed 原本扫不到）。④ **归档影响**：22 个含 heldout/test 局的归档 run 属 protocol v2 证据，与新协议**不可比**；不删数据、不擅自重跑。⑤ 门禁 `scripts/ci_gate.sh` 全绿（1925 passed）；本版未做付费跑批。 |
