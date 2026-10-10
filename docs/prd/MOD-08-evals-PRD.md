# MOD-08 evals 技术 PRD

> 版本：v0.6（2026-10-06 按 M1 验收复核更正 §15 口径引用，见 §17）· 状态：待评审 · 所属里程碑：M1（基线）→ M2（执行器/指标）→ M3（完整：坍缩 + 消融 + test 终检）
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
- **FR-08-03** `heldout`（2000–2099）切成固定篮子 `H1=2000–2039`（代际裁判）、`H2=2040–2079`（消融/终检复核）、`H3=2080–2099`（应急）；篮子成员不得重新抽样。
- **FR-08-04** `test`（9000–9099）默认锁定；仅 `test_unlock` 解锁，每里程碑 ≤1 次，落 `test_attestation.json`；结果**不得**进入代际决策路径。test 同时跑两种局：默认局（主结论）和完整局 medium × 10 关（次级「迁移」指标，Spec §8.6，只报告不判定）。
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
- **FR-08-23 / 24** 主指标聚合路径**结构上不可**读取 `reward`/`cum_reward`（INV-7）：`EpisodeResult` 不含这两个字段；报告落盘后写 `report.sha256`，同参数重跑必须一致，否则抛 `E_EVAL_NONDETERMINISTIC`。

## 5. 非功能需求（NFR-08-x：确定性/统计严谨/可复现/可观测）

- **NFR-08-01（确定性）** 所有随机性来自显式 seed：bootstrap 用 `np.random.default_rng(rng_seed)`，`rng_seed = int.from_bytes(sha256(f"{run_id}:{generation}:{basket}".encode()).digest()[:8])`，禁 `random.*`（INV-6 + MOD-09 `guards.py` AST 扫描）；环境部分由 seed 完全决定、模型部分在 `temperature=0` 下尽力确定，后端不保证时必须显式降级（`E_EVAL_NONDETERMINISTIC` 告警 + `n_rep=3`），不得静默。
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
      H1: {start: 2000, end: 2039, role: generation_judge}
      H2: {start: 2040, end: 2079, role: ablation_final}
      H3: {start: 2080, end: 2099, role: reserve}
  test:    {start: 9000, end: 9099, role: final_acceptance, locked: true, unlock_policy: single_use_per_milestone}
exposure_guard:
  destinations_forbidden: [prompt, memory, reflection]
  numeric_scan: true            # 文本中 4 位数字 token 落 2000-2099 / 9000-9099 即告警
  forbidden_tokens: [heldout, H1, H2, H3, "2000-2099", "9000-9099"]
seeds_hash: "sha256:<>"         # 由 CLI 生成、CI 校验；改动必须同步更新并在 PR 说明
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

- **G1**：`pytest tests/unit/evals --cov=dice_ratchet.evals --cov-fail-under=80` 通过（MOD-08 属纯函数模块）。**G2**：指标 golden 快照存在（T-13）；改动需显式更新并在 PR 说明。**G3**：同 `(run_id, generation, split, seeds)` 重跑 `report.sha256` 一致（T-09/T-15）。
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
| 1. 闭环自动运行 ≥3 轮演化且 held-out 单调不降 | `generations.csv.heldout_win_rate` + `ci_lo/ci_hi` | `eval report --split heldout --run-id <run_id>` | g=1..3 均满足 `ci_lo(g) >= win_rate(g-1) - 0.02` 或 `P(gap>0) >= 0.90`；`monotonic_ok=true` |
| 2. ≥1 个指标相对基线有可归因提升（含消融） | `ablation.delta_full` + `attribution` + 基线对照 | `eval ablation …` + `eval report --baseline all` | H2 上至少一臂显著劣于 full（Holm–Bonferroni α=0.025），或相对最好基线的 Wilson CI 不重叠；`attribution != "none"` |
| 3. 增益真实性通过（gap < 阈值、无坍缩） | `gain_realism.verdict`、`diversity.collapse_flag`、test 复核 | `eval report` + `eval test --unlock-once` | `verdict ∈ {real_gain, null}` 且 `collapse_flag=false`；test 上 `win_rate` 的 Wilson CI 下界 > 最好基线点估计，且与 heldout 点估计差 ≤0.20 |

## 13. 指标与埋点

- **主指标（INV-7 白名单来源）**：`win_rate`（第一主指标），`mean_stages_cleared` / `mean_hearts_left` / `mean_battles_won`（并列次指标）。
  - **2026-10-06（M2 开工 D-3 裁决，Spec §8.6 / B-39）判据分工**：`win_rate` 仍是第一报告指标，但**在 H=40（H1 篮子）下只能判 ≥15pp 的净跳变**（见 §8.2 的功效换算），故 M3 的「4–5pp 量级增益」结论**由 `mean_stages_cleared` 的配对 bootstrap 承担**（Δ=0.30 关 = 6 关制的 5.0pp，n=40 下 power 0.66–0.97），`mean_battles_won` 作并列证据。**hard × 6 上 `fixed_prompt` 基线 win_rate = 0.0%（n=10），win_rate 无方差可用**，D-2 已据此把 M3 判据改为 `mean_stages_cleared`/`mean_battles_won`。报告必须同时给出两个口径的 `delta + CI + p + method`，并保留 `power_note`。
- **次级指标与判定量**：`illegal_rate`、`entropy_mean_norm`、`entropy_slope`、`jsd_kind`、`jsd_slot`、`distinct_sequence_ratio`、`llm_latency_ms_p50/p95`、`mean_cum_reward_debug`（**仅调试，禁止进入 verdict**）；判定量为 `win_rate_gap`、`verdict`、`collapse_flag`、`attribution`。
- **埋点事件**（统一经 MOD-09 `emit` 写入 `events.jsonl`）：`eval.start{spec}`、`eval.seed_start{seed,split,basket}`、`eval.seed_done{seed,outcome,steps,decisions,dur_s,dropped,degraded,error_code}`、`eval.report{n,n_dropped,degraded_n,dur_s,report_sha256}`、`eval.guard_violation{error_code,split,seed,destination,matched_token}`、`eval.collapse{generation,reasons}`、`eval.test_unlock{generation,attestation_path,decided_by}`。
- **绘图数据（MOD-09 消费）**：`metrics/generations.csv` 列固定为 `generation,harness_version,parent_version,split,basket,n,win_rate,ci_lo,ci_hi,mean_stages_cleared,mean_hearts_left,mean_battles_won,mean_decisions,illegal_rate,fallback_rate,truncation_rate,entropy_mean_norm,entropy_slope,jsd_kind,collapse_flag,win_rate_gap,verdict,decision,bootstrap_seed,config_hash,git_commit,ts`。

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

1. **test 池与冗余子集**：9000–9099（n=100）在 M3 只允许一次性使用；M4 前是否需要扩成两个不相交 test 篮子（9000–9049 / 9050–9099）以支持"跨代增益 vs 推理时增益"长期对比？另：若 train 池耗尽后循环复用同一 seed，代际 `win_rate` 比较是否仍有效（倾向：禁止复用，耗尽即停）？
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
| v0.x-2026-10-10 | 2026-10-10 | **F43 与 F46（对齐 Spec v2.4 B-52/B-55）**。① **F43**：`eval report` 与 MOD-07 `_finish_report` 写**同一个** `generations/gen-NNN/report.json`，而判据②要的 `metrics/generations.csv` 只有 `eval report` 会产 ⇒ 「满足②」只能靠「抹掉①的判决」。修法：MOD-07 在代末自己追写 csv（新增 `generations_row_from_report`，列集与 `_csv_row` 共用 `GENERATIONS_COLUMNS`）；`eval report` 对**已裁决**的代改写 sidecar `report.eval.json`；`append_generations_csv` 按 `generation` **upsert**（每代恰好一行）并保留已裁决的 `decision`/`verdict`。② **F46（待裁决）**：`load_episode_results` 的 `generation` 参数**只贴标签、不筛文件** ⇒ `eval report --generation 1` 把 gen-000 基线的 `arm="parent"`（40 局）也算进 gen-1，实测 `n=117`（= g000/parent 40 + g001/parent 40 + g001/cand 37 有效）而真实只有 40+40+37。**台账每代数字因此没有物理含义**——这正是 M1 验收 F5 修过的「混**臂**聚合」的同类残留（那次修了 arm、没修 generation） |
