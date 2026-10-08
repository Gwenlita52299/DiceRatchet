# MOD-07 evolve —— harness 自进化

> 上游：[MOD-07 evolve 技术 PRD](../../docs/prd/MOD-07-evolve-PRD.md)（v0.3）·
> [模块拆分 Spec](../../docs/模块拆分Spec-DiceRatchet.md)（v1.4，唯一事实来源）
> 交付里程碑：M2（propose/evaluate/versioning/HITL 初版）→ M3（回滚/消融对接/统计口径收紧）
> 代码路径：`dice_ratchet/evolve/`

MOD-07 负责 RSI 闭环中最危险的一段：**让 agent 改写自己的 harness，并由环境
ground-truth 决定改动去留**。设计重心不是"生成更聪明的改动"，而是**在结构上掐死
自我确认环路**：

- 候选生成阶段**物理上看不到** heldout/test 数据（INV-5）；
- 裁判只看环境原生结果（INV-7），生成者与裁判者上下文不重叠（INV-3）；
- 接受判定强制 `collapse_flag == False` + 熵斜率下限（C4）；
- 改动面硬上限 + `win_rate_gap` 阈值 + 消融对照。

## 1. 模块布局

| 文件 | 职责 | PRD 对应 |
|---|---|---|
| `schemas.py` | `Candidate` / `EvidenceBundle` / `Verdict` / `GateState` + JSON Schema + round-trip | §6.1 / §7 |
| `proposer.py` | 证据束白名单、INV-5 硬闸、prompt 渲染、`propose`、`validate_candidate`、diff 应用 | FR-07-1…6 |
| `judge.py` | `evaluate_candidate`（只编排 MOD-08）、`judge` 判定表 C0–C5、冒烟快速淘汰 | FR-07-6/7/8 |
| `versioning.py` | `stage_candidate` / `accept` / `rollback` / `versions/v{N}/` + git commit + tag | FR-07-9/10 |
| `gate.py` | HITL：`await_human` / `resolve_gate` / 超时保守回滚 | FR-07-11 |
| `generation.py` | `run_one_generation` —— MOD-06 `evolve_fn` 的实现（§8.1 单代流程） | §8.1 / FR-07-14/15 |
| `cli.py` | `evolve approve\|rollback`（经组合根注册进 MOD-09 命令面） | Spec §4.6 |
| `paths.py` | `harness/` 与 `runs/<run_id>/generations/` 的布局与 `EvolveContext` | Spec B-29/B-32 |
| `errors.py` | `E_EVOLVE_*` 异常（`code_default` 单一出处） | §10 |

PRD §6.1 只列了 `proposer/judge/versioning/gate` 四个文件；本实现额外拆出
`schemas.py`（类型 + schema 单一出处）、`paths.py`（布局）、`generation.py`（编排）、
`errors.py`（异常）、`cli.py`（命令面），原因是**所有者不同**：schema 要能被 MOD-08/09
复用，布局要能被 MOD-06 的 `--resume` 复读，编排要能被组合根注入。

## 2. 对外接口

```python
from dice_ratchet.evolve import (run_one_generation, propose, validate_candidate,
                              evaluate_candidate, judge, accept, rollback, await_human)

# MOD-06 的 evolve_fn（组合根注入）
outcome = run_one_generation(state=state, run_ctx=run_ctx, runner=runner)   # EvolveOutcome

# 手工驱动（测试/调试）
bundle = build_evidence_bundle(run_id=..., n_traj=20, cfg=evolve_cfg, memory=store)
prop = propose(bundle, cfg=evolve_cfg, rng_seed=derive_seed(run_id, gen, "propose"))
report = evaluate_candidate(cand, parent_ref, cfg=evolve_cfg, staging_dir=staging, ...)
verdict = judge(report, parent_heldout, cfg=evolve_cfg, generation=gen)
```

- `evaluate_candidate` **只编排**：冒烟 → 快速淘汰 → heldout → `compare_paired`；
  所有统计口径归 MOD-08，MOD-07 不做任何统计运算（FR-07-7）。
- `judge` 是纯函数（G1 覆盖率主力）：§8.2 六项短路求值，逐条写 `condition_trace`。
- `accept` / `rollback` 以 `(run_id, generation)` 为幂等键，账本落在
  `runs/<run_id>/generations/gen-{N}/version_ledger.json`（NFR-07-6）。

## 3. 目录布局（Spec v1.4 B-29/B-32）

```
harness/                          # 当前生效工件（MOD-04 只读）
  version.json                    # {version,parent,git_commit,artifact_hash,created_at,
                                  #  reason,accepted_by,base_model,diff_path}
  prompts/decision.md  prompts/reflection.md  config.json   # evolvable_keys 内键可改
  tools/*.py                      # @harness_tool 纯函数，rank_actions 签名冻结
  versions/v{N}/                  # 不可变快照：version.json + diff.patch + 全量工件

runs/<run_id>/generations/gen-{N}/
  report.json  report.sha256  gate.json  version_ledger.json  decision.jsonl
  candidates/{candidate_id}.json  patches/{candidate_id}.patch  staging/{candidate_id}/
```

`candidates/`、`patches/`、`gate.json` **不进** `harness/versions/`：前者是过程与裁决，
后者是"可回滚的生效工件"（B-32 已裁决）。

## 4. 使用（G6：可直接复制执行）

```bash
# 闭环（HITL 开启时在代际边界优雅挂起，退出码 0）
python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl --run-id <run_id>

# 人工裁决（读 gate.json → 置 approved/rejected；accept/rollback 在续跑时执行）
python -m dice_ratchet evolve approve  --run-id <run_id> --generation 1
python -m dice_ratchet evolve rollback --run-id <run_id> --generation 1 --reason "效果可疑"

# 续跑（MOD-07 认得已裁决的 gate，执行 accept/rollback 并 +1 代）
python -m dice_ratchet loop run --run-id <run_id> --resume
```

退出码（Spec §8.3）：`0` 成功（含 HITL 挂起）/ `2` 配置错误（缺 `--run-id`、run 目录或
`gate.json` 不存在）/ `4` 沙箱守卫违例。

## 5. 判定表（§8.2，短路求值）

| # | 条件 | 表达式 | 不满足时 |
|---|---|---|---|
| C0 | 前置完备 | `heldout.n >= min_heldout_n` 且关键字段非 null | `E_EVOLVE_JUDGE_FAILED` |
| C1 | 主指标不降 | `win_rate >= parent.win_rate - win_rate_tol`（0.05） | `E_EVOLVE_NO_GAIN` |
| C2 | 至少一指标显著提升 | `paired.significant and direction == "up"` | `E_EVOLVE_NO_GAIN` |
| C3 | 增益真实性 | `train.win_rate - heldout.win_rate <= gap_threshold`（0.15） | `E_EVOLVE_OVERFIT_SUSPECT` |
| C4 | 无坍缩 | `collapse_flag == False and entropy_slope >= -0.01` | `E_EVOLVE_COLLAPSE` |
| C5 | 合法性与健壮性 | `illegal ≤ 0.05 且 fallback ≤ 0.10 且 truncation ≤ 0.05` | `E_EVOLVE_ILLEGAL_RATE` |

`accept ⟺ C0∧C1∧C2∧C3∧C4∧C5`；多候选全通过时取 `(C2 delta 最大, candidate_id 最小)`，
其余写 `reject/reason="superseded"`（不计失败、无错误码）。

## 6. 测试

```bash
python -m pytest tests/unit/evolve tests/integration/evolve -q
```

| 文件 | 覆盖 |
|---|---|
| `tests/unit/evolve/test_evolve_proposer_validation.py` | FR-07-4/5：改动面、白名单、签名、依赖、去重排序、diff 应用 |
| `tests/unit/evolve/test_evolve_evidence.py` | FR-07-2/3：证据束白名单、INV-5 seed 字面量扫描（含 `20130` 不误报） |
| `tests/unit/evolve/test_evolve_proposer_prompt.py` | FR-07-1/3 + NFR-07-1：prompt INV-3 回归、propose 显式失败、确定性 |
| `tests/unit/evolve/test_evolve_judge.py` | §8.2 判定表 12 例 + 短路顺序 + 冒烟快速淘汰 + winner 选择 |
| `tests/unit/evolve/test_evolve_versioning.py` | FR-07-9/10 + NFR-07-6：快照、幂等 accept、回滚、决策段、git 失败恢复 |
| `tests/unit/evolve/test_evolve_gate.py` | FR-07-11：pending 落盘、人工裁决、超时回滚 |
| `tests/unit/evolve/test_evolve_generation.py` | §8.1：空候选/全淘汰/扫描失败/冒烟淘汰/accept/HITL 挂起/MOD-08 异常 |
| `tests/unit/evolve/test_malicious_candidates.py` | MAL-1…8：恶意候选全部在静态扫描/校验阶段被挡，**0 局 heldout 消耗** |
| `tests/unit/evolve/test_evolve_cli.py` | `evolve approve\|rollback` 的退出码与幂等 |
| `tests/integration/evolve/test_evolve_contracts.py` | G4/G5/G6：四 schema round-trip、`audit leak` clean、命令面接线 |

## 7. 已知取舍

- **`time.perf_counter` 只做耗时观测**（`elapsed_s` / `latency_ms`），不参与任何判定；
  报告里的耗时**不进** `report.json` 的 schema（只在 `candidates/*.json` 明细里）。
- **C5 的 `fallback_rate`/`truncation_rate` 取 aggregate 口径**：`aggregate()` 已经
  按"合法步"分母算好，MOD-07 直接读，不再自己算。
- **parent 每代重算**（FR-07-7）虽然贵，但这是 parent 与候选**共用同一冻结记忆快照**
  的必要代价；成本由 MOD-06 的 `loop.max_episodes_per_generation` 统一约束。
- **HITL 挂起时也会写 `report.json`（`decision="pending"`）**，`gate.json` 同时存在；
  MOD-06 的周期幂等检查会因此放行该 cycle（见 `loop/generation.py::_cycle_artifacts_exist`），
  否则 `--resume` 会空转。
