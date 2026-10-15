# MOD-08 evals —— L3 测量层

> PRD：[`../../docs/prd/MOD-08-evals-PRD.md`](../../docs/prd/MOD-08-evals-PRD.md)（v0.3）
> 上游 Spec：[`../../docs/模块拆分Spec-DiceRatchet.md`](../../docs/模块拆分Spec-DiceRatchet.md) §4.5/§4.6/§4.7

**只读轨迹与报告，不参与决策**（Spec §1）。本模块把"自我改进是否真的发生"变成
可复现、可审计、可证伪的数字：held-out 评估协议、三基线、INV-7 主指标、坍缩监控与消融。

## 文件职责

| 文件 | 职责 | 关键需求 |
|---|---|---|
| `seeds.py` | seed 唯一出口 `take()` / 篮子 `resolve_basket()` / `train_window()`；三类区间两两不相交（train 1000–1999 / heldout 731000–731099 / test 947000–947099，F45-2 起为六位端口空间）；协议摘要 `seeds_hash`（当前 `sha256:686b2618330db5eb8291c1810499dc07fc47a956da5711803015bbb09ec2e904`；`config_hash` 不覆盖 `configs/seeds.yaml`，故 `baselines.baseline_protocol()` 追加 `+seeds-<hash12>`，新旧 seed 协议不可能在 `--resume` 上静默混用）；INV-5 文本护栏与记忆拦截；test 解锁 + `test_attestation.json` | FR-08-01…06 |
| `metrics.py` | `EpisodeResult`（INV-7：**不含** shaped 分数字段）/ `SplitMetrics` / `GainVerdict` / `CollapseVerdict` / `PairedTest`；Wilson CI、归一化熵、Bootstrap、McNemar 精确检验、聚合、§8.2 verdict、报告渲染 | FR-08-15…18, 23/24 |
| `diversity.py` | `js_divergence`、`entropy_slope`、四条件坍缩判定（C1–C4，两点确认） | FR-08-19/20 |
| `harness.py` | `EvalSpec` / `run_eval`（并行、超时、dropped/degraded、预算）；MOD-07 门面 `evaluate_arm` / `compare_paired` / `evaluate_generation`（接受 `resume_results` / `on_result` / `run_dir`）；`evaluate_arm` 逐臂归档 `metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json`（`ArmResult.as_dict()` + provenance + 逐局明细）与 fail-closed 复用（provenance 全等且覆盖完整 seed 列表 ⇒ 整臂复用、**0 局**并发 `evals.arm_reused`；部分覆盖只补缺局；任一不一致即重算）；`report.json` + `report.sha256` + `generations.csv` + `plots_data/`；**台账是「每代每 split 一行」**（2026-10-11 F48；**⚠️ 2026-10-14 修订（F57）：upsert 键升为 `(generation, split, arm)` 三键**，`train` 行按 `n > 0` 判定写入，读入时折叠历史重复行；**只有 `heldout` split 写权威产物**——`eval report --split train` 只读：打印 + 落 `metrics/gen-NNN.train.readonly.json`，**不写** `report.json` / `generations.csv` / `plots_data/` / `ext`，F61/F71）——原先只写 heldout 行 ⇒ train 对照曲线与 `train_heldout_gap.png` 无源；列清单的唯一副本在 `infra/plotting.py`（本模块直接引用）；`SandboxRunner` | FR-08-07…10, 17, 22, 25 |
| `baselines.py` | `random` / `heuristic` / `fixed_prompt` 三基线（heuristic 复用 MOD-04 `heuristic_policy`；fixed_prompt 钉死 harness v0）、不可覆盖归档 + `registry.csv` | FR-08-11…14 |
| `ablation.py` | H2 三臂（`full`/`no_reflection`/`no_self_edit`）配对检验 + Holm–Bonferroni + 归因 | FR-08-21 |
| `reward.py` | shaped reward 定义（**仅学习/调试**；不进任何判定路径） | Spec §3 移交 |
| `cli.py` | `baseline` / `eval report|diversity|ablation|test` 实现（经 `app.py::wire()` 注册）；`report` / `diversity` 支持 `--arm <name|all>`（F46-A） | G6 |

## G6：可直接复制执行的命令

```bash
# 基线（三选一，拆成三条真实可执行命令；heuristic 的 45.5% 参考通关率是 Spec §8.6 的
# n=200 / seed 1–200 口径，与这里的 --basket H1（40 局，seed 731000–731039）不是同一批）
# random / heuristic 不带 --model 覆盖，可与下文 eval report 等共用同一个 <run_id>（F10）
python -m dice_ratchet baseline --kind random    --split heldout --basket H1 --run-id <run_id>     # 期望退出码 0
python -m dice_ratchet baseline --kind heuristic --split heldout --basket H1 --run-id <run_id>     # 期望退出码 0
# fixed_prompt 必须带 --model（同样并入 config_hash）：单独用一个 run-id，换 --model 再换一次
python -m dice_ratchet baseline --kind fixed_prompt --split heldout --basket H1 \
    --model qwen3-8b-q4 --run-id <fixed_run_id>    # 需权重，期望 0；否则 3

# 代际报告（从 run 的 episodes/ 重算指标 → generations/gen-000/report.json + report.sha256）
# F46-A：--generation 现在是真过滤（修复前只做标签覆盖，会把跨代/跨臂的局混成一个池）；
#   --arm <name|all> 选臂——缺省 = parent（该代起点臂，F57 起不再默认取候选臂；无 parent 局
#   才回落胜出候选/唯一候选）；盘上不存在的臂名报 E_EVAL_ARM_NOT_ALLOWED（退出码 2）；
#   all = 显式申报的混合池（stdout/ext 标 mixed_pool=true，禁止当默认口径，且**不写台账**）
#   --split train 只读（F61/F71）：只打印 + 落 metrics/gen-NNN.train.readonly.json，
#   不写 report.json / generations.csv / plots_data/ / ext
python -m dice_ratchet eval report --split heldout --baseline all --run-id <run_id>
python -m dice_ratchet eval report --split heldout --generation 1 --arm parent --run-id <run_id>

# M3 判据 1（held-out 单调不降 + ≥1 次 accept）→ metrics/acceptance.json
#   **命令恒返回 0**（判定器不是守卫；结论看产物里的 ok，不通过时原因写 stderr）
python -m dice_ratchet eval acceptance --run-id <run_id>

# 动作熵 / 坍缩监控（写 metrics/gen-*.ext.json 与 metrics/plots_data/gen-*_entropy.json）
# --arm 缺省 / all = 该代整池（坍缩是代际现象）；给具体臂名时只看该臂
python -m dice_ratchet eval diversity --run-id <run_id> --generation 3

# 消融（H2 三臂，产出 ablation/gen-XXX.json）
python -m dice_ratchet eval ablation --run-id <run_id> --generation 3 --arms no_reflection,no_self_edit

# test 终检（默认锁定；每里程碑单次解锁并存证）
python -m dice_ratchet eval test --run-id <run_id> --unlock-once \
    --attestation runs/<run_id>/generations/gen-003/test_attestation.json
```

## 测试（G1/G2/G3/G5）

```bash
pytest tests/unit/evals --cov=dice_ratchet.evals --cov-fail-under=80     # G1（实测 ~85%）
pytest tests/integration/evals -q                                     # T-13 golden / T-16 消融 / T-17 命令
python tests/golden/evals/regen_metrics_golden.py                     # G2：指标 golden 重生成（改口径后必跑并审阅 diff）
```

`tests/unit/evals/test_evals_metrics_inv7.py` 用 AST 扫 `metrics.py`：主指标计算路径
**不得出现** shaped 分数字段标识符（INV-7 的结构性保证）。

## 口径速查

- **主指标**（INV-7 白名单）：`win_rate` + Wilson 95% CI、`mean_stages_cleared`、
  `mean_hearts_left`、`mean_battles_won`、`mean_decisions`、`illegal_rate`、
  `fallback_rate`、`truncation_rate`、归一化动作熵与熵曲线。
- **截断一律计 lose**；**dropped 计入 n 且按 lose 计**（防幸存者偏差）；
  **degraded 剔出 n** 并单列 `degraded_n`。
- **验证判定**（§8.2 有序短路）：`regress` → `overfit_suspect` → `real_gain` → `null`；
  `collapse_flag=true` 时永不 `real_gain`（`collapse_veto`）；`low_confidence` 时为 `insufficient`。
- **判据 1 判定器（F-M3-9 / F70）**：`eval acceptance` 产出 `metrics/acceptance.json`
  （`ok` / `monotonic_ok` / `comparable_pairs` / `not_applicable_pairs` /
  **`baseline_selection`{selected_pairs,note}** / `accepts` / `per_generation[]`），每代带
  `play_hash` / `prev_play_hash` / `not_applicable` / **`baseline_selected` / `baseline_source`**；
  上一代 `decision="accept"` ⇒ 本代基线是**选择后的**测量（winner's curse，逐对标注，不重测、不换篮子）。
  命令**恒返回 0**（判定器不是守卫），不通过时原因写 stderr。
- **统计**：配对 McNemar 精确检验（胜率）、配对 bootstrap B=10000（连续量）、
  Holm–Bonferroni（消融，两臂时首个阈值 0.025）、BH-FDR q=0.10（次级指标）。
- **复现**：同 `(run_id, generation, split, seeds)` 重跑得到逐字节相同的
  `report.json`（`report.sha256` 比对），否则 `E_EVAL_NONDETERMINISTIC`。
  **2026-10-11（D-M3-3 裁决 A，Spec v2.9 / B-65）按臂分层**：这条硬门只对**本地臂**成立；
  臂内含任何 `provider=api` 的角色（如主力臂 `C1` 的 `deepseek-flash`，
  `api_supports_seed=false`，实测字节相同的请求 9 次返回 9 个不同响应）时，端到端逐字节
  可复现物理上不成立 ⇒ 该臂上跳过 sha256 硬门、发
  `eval.nondeterministic{determinism=api_nondeterministic, reason, repro_scope}` 事件，
  **不抛异常**。生产执行点是 `harness.enforce_report_determinism(root, gen, report,
  app_cfg=...)`，调用点在 `evolve/generation.py` 的**写报告之前**——不显式调用它，
  这条承诺就只是一句文档（2026-10-11 之前的实际状态：`check_report_determinism`
  存在、被导出、有单测，却**没有任何生产调用点**）。可复现层（代码/配置/seed/聚合口径/
  并发归一化）的承诺不变。
- **seed 协议**（F45-2）：train 保持 `1000–1999`；heldout H1 `731000–731039`、
  H2 `731040–731079`、H3 `731080–731099`；test `947000–947099`（六位端口空间，
  避开耗时/预算/日历字面量）。`seeds_hash =
  sha256:686b2618330db5eb8291c1810499dc07fc47a956da5711803015bbb09ec2e904`，
  `baselines.baseline_protocol()` 追加 `+seeds-<hash12>`，`eval report` 的 ext provenance 另记
  `seeds_protocol_id` / `seeds_protocol_tag`，`--resume` 时新旧协议不可静默混用。
- **断点续跑**（F42-A/B/C）：`evaluate_arm` 逐臂归档 `metrics/arms/gen{gen:03d}-{split}-{basket}-{arm}.json`，
  provenance 全等且覆盖完整 seed 列表 ⇒ **整臂复用、0 局**（`evals.arm_reused`），部分覆盖只补
  缺局，任一不一致即重算；MOD-07 在评估**之前**把提案落
  `generations/gen{gen:03d}/proposals/proposal.json`，输入指纹（`run_id`/`generation`/
  `propose_seed`/`harness_version`/`memory_version`/`prompt_digest`）一致 ⇒ 复用同一 candidate id
  与 diff、**0 次 proposer 调用**（`evolve.proposal_reused`）。理由：一代 84 局评估 ≈ 3.75 h /
  ≈¥47，中断即再付一次；而 API 提案不可复现（`api_supports_seed=false`）。
- **审计扫描面**（F45-2）：`sandbox/audit.py` 的 `LOG_NON_CONTENT_KEYS` /
  `MEMORY_NON_CONTENT_KEYS` 两张人工死名单已删除，seed 规则扫整行字段，仅豁免框架自身的
  播种计划 `LOG_STRUCTURAL_KEYS = {seed, episode_key, spec}`。
