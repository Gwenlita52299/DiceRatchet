# MOD-08 evals —— L3 测量层

> PRD：[`../../docs/prd/MOD-08-evals-PRD.md`](../../docs/prd/MOD-08-evals-PRD.md)（v0.3）
> 上游 Spec：[`../../docs/模块拆分Spec-GameRSI.md`](../../docs/模块拆分Spec-GameRSI.md) §4.5/§4.6/§4.7

**只读轨迹与报告，不参与决策**（Spec §1）。本模块把"自我改进是否真的发生"变成
可复现、可审计、可证伪的数字：held-out 评估协议、三基线、INV-7 主指标、坍缩监控与消融。

## 文件职责

| 文件 | 职责 | 关键需求 |
|---|---|---|
| `seeds.py` | seed 唯一出口 `take()` / 篮子 `resolve_basket()` / `train_window()`；三类区间两两不相交；INV-5 文本护栏与记忆拦截；test 解锁 + `test_attestation.json` | FR-08-01…06 |
| `metrics.py` | `EpisodeResult`（INV-7：**不含** shaped 分数字段）/ `SplitMetrics` / `GainVerdict` / `CollapseVerdict` / `PairedTest`；Wilson CI、归一化熵、Bootstrap、McNemar 精确检验、聚合、§8.2 verdict、报告渲染 | FR-08-15…18, 23/24 |
| `diversity.py` | `js_divergence`、`entropy_slope`、四条件坍缩判定（C1–C4，两点确认） | FR-08-19/20 |
| `harness.py` | `EvalSpec` / `run_eval`（并行、超时、dropped/degraded、预算）；MOD-07 门面 `evaluate_arm` / `compare_paired` / `evaluate_generation`；`report.json` + `report.sha256` + `generations.csv` + `plots_data/`；`SandboxRunner` | FR-08-07…10, 17, 22, 25 |
| `baselines.py` | `random` / `heuristic` / `fixed_prompt` 三基线（heuristic 复用 MOD-04 `heuristic_policy`；fixed_prompt 钉死 harness v0）、不可覆盖归档 + `registry.csv` | FR-08-11…14 |
| `ablation.py` | H2 三臂（`full`/`no_reflection`/`no_self_edit`）配对检验 + Holm–Bonferroni + 归因 | FR-08-21 |
| `reward.py` | shaped reward 定义（**仅学习/调试**；不进任何判定路径） | Spec §3 移交 |
| `cli.py` | `baseline` / `eval report|diversity|ablation|test` 实现（经 `app.py::wire()` 注册） | G6 |

## G6：可直接复制执行的命令

```bash
# 基线（三选一，拆成三条真实可执行命令；heuristic 的 45.5% 参考通关率是 Spec §8.6 的
# n=200 / seed 1–200 口径，与这里的 --basket H1（40 局，seed 2000–2039）不是同一批）
# random / heuristic 不带 --model 覆盖，可与下文 eval report 等共用同一个 <run_id>（F10）
python -m gamer_rsi baseline --kind random    --split heldout --basket H1 --run-id <run_id>     # 期望退出码 0
python -m gamer_rsi baseline --kind heuristic --split heldout --basket H1 --run-id <run_id>     # 期望退出码 0
# fixed_prompt 必须带 --model（同样并入 config_hash）：单独用一个 run-id，换 --model 再换一次
python -m gamer_rsi baseline --kind fixed_prompt --split heldout --basket H1 \
    --model qwen3-8b-q4 --run-id <fixed_run_id>    # 需权重，期望 0；否则 3

# 代际报告（从 run 的 episodes/ 重算指标 → generations/gen-000/report.json + report.sha256）
python -m gamer_rsi eval report --split heldout --baseline all --run-id <run_id>

# 动作熵 / 坍缩监控（写 metrics/gen-*.ext.json 与 metrics/plots_data/gen-*_entropy.json）
python -m gamer_rsi eval diversity --run-id <run_id> --generation 3

# 消融（H2 三臂，产出 ablation/gen-XXX.json）
python -m gamer_rsi eval ablation --run-id <run_id> --generation 3 --arms no_reflection,no_self_edit

# test 终检（默认锁定；每里程碑单次解锁并存证）
python -m gamer_rsi eval test --run-id <run_id> --unlock-once \
    --attestation runs/<run_id>/generations/gen-003/test_attestation.json
```

## 测试（G1/G2/G3/G5）

```bash
pytest tests/unit/evals --cov=gamer_rsi.evals --cov-fail-under=80     # G1（实测 ~85%）
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
- **统计**：配对 McNemar 精确检验（胜率）、配对 bootstrap B=10000（连续量）、
  Holm–Bonferroni（消融，两臂时首个阈值 0.025）、BH-FDR q=0.10（次级指标）。
- **复现**：同 `(run_id, generation, split, seeds)` 重跑得到逐字节相同的
  `report.json`（`report.sha256` 比对），否则 `E_EVAL_NONDETERMINISTIC`。
