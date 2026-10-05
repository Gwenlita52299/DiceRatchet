# 交付任务：跑完并归档 `fixed_prompt` 基线的 **n=10** 子集（M1 §13.2 第 6 项）

> 这是给**另一个 session** 执行的任务说明。请把它当作完整的自包含上下文：你不需要读本次对话历史，
> 但需要按下面的顺序**先核对前提**、再跑批、最后回填文档。
> 预计耗时：**约 8 小时墙钟**（10 局 × ≈43 min 模型时间，`n_workers=2` 共享一个模型实例，模型是瓶颈）。
> 仓库根：`<workspace>/GameRSI`（下文相对路径都相对它）。

---

## 0. 背景（一句话版）

《M1 验收报告》§13.2 第 6 项原要求「重跑并归档 n=40 的 `fixed_prompt`」，但 2026-10-05 用真实 8B
实测后确认 **n=40 需要 ≈29–34 h**（旧估 4 h），成本不可接受；用户决定**缩小到 n=10**。
本任务 = 在**最终契约/配置**上把 n=10 跑完、归档、并把真实数字回填文档。

三个必须先理解的事实（报告 §14.15–§14.18）：

- **F22**：`fixed_prompt` 已改走 MOD-04 `PolicyAgent(mode="baseline_fixed_prompt")`，**每个决策点一次推理**；
- **F23/F25**：两条 prompt 模板（`harness/prompts/decision.md` 与 `gamer_rsi/agent/prompts/fixed_v1.md`）
  都补了第 6/7/8/9 条契约（配对/不重复/省略可选字段/一次决策=一个回合）——**跑批期间绝对不要再改模板**；
- **F24**：`evals.seed_timeout_s` 是**墙钟**预算且被 `n_workers` 共享放大，已按实测调到 **14400 s**。

---

## 1. 前提核对（不通过就先停下报告，不要硬跑）

```bash
cd <workspace>/GameRSI
git log --oneline -1                       # 应为 §13.2 修复后的提交（≥ 84c48eb）
git status --short                         # 应为空（或只有 docs/html 的同步改动）

# a) 权重与依赖
ls models/Qwen3-8B-4bit >/dev/null && .venv/bin/python -c "import mlx, mlx_lm; print('mlx ok')"
.venv/bin/python -c "
from gamer_rsi.inference.weights import weights_digest
print(weights_digest('models/Qwen3-8B-4bit'))"   # 应为 sha256:20303070eb19e8a4…

# b) 配置
grep -n 'seed_timeout_s' configs/default.yaml    # 应为 14400
.venv/bin/python -c "
from dataclasses import replace
from pathlib import Path
from gamer_rsi.evals.baselines import baseline_protocol
from gamer_rsi.infra.config import load_config
c = load_config(config_dir=Path('configs'))
print('seed_timeout_s =', c.evals.seed_timeout_s, '| n_workers =', c.evals.n_workers)
print('protocol =', baseline_protocol('fixed_prompt', config=c))
print('frozen_v1 template =', c.agent.policy.fixed_prompt_path, '| task =', c.agent.policy.task_path)"

# c) 模板确实带四条契约（F23/F25）
grep -c '整条记录\|不得重复使用同一个\|直接省略\|一次决策 = 一个回合' harness/prompts/decision.md
grep -c '整条记录\|不得重复使用同一个\|直接省略\|一次决策 = 一个回合' gamer_rsi/agent/prompts/fixed_v1.md

# d) 测试与门禁（应全绿）
.venv/bin/python -m pytest -q -x -m "not slow"      # 约 80 s
.venv/bin/python -m gamer_rsi guards check --root gamer_rsi
```

---

## 2. 跑批（一条 shell，串行，防休眠）

> **必须串行**：本机 24 GB，MLX 一次只跑一个 8B；跑批期间**不要**再跑别的 MLX 任务（会污染 `t_d` 与耗时）。
> 用 `caffeinate -i` 防 macOS 空闲休眠（M1 报告 F19：不加会把 8 h 墙钟压成 30 min 计算量）。

```bash
cd <workspace>/GameRSI
RUN=m1-real-h1n10b          # 注意：M1 已有一个 m1-real-h1n10（旧实现），不要复用
SEEDS='heldout:2000-2009'   # H1 的前 10 个 seed（与 M1 的 n=10 子集同源，便于对比）

# 1) 秒级的两个对照基线（同 run、同 config_hash、同 seed 集合）
caffeinate -i .venv/bin/python -m gamer_rsi baseline --kind random    --split heldout --seeds "$SEEDS" --run-id "$RUN"
caffeinate -i .venv/bin/python -m gamer_rsi baseline --kind heuristic --split heldout --seeds "$SEEDS" --run-id "$RUN"

# 2) 主体：fixed_prompt（真实 8B，长跑；逐局增量落盘，中断不会全废）
caffeinate -i .venv/bin/python -m gamer_rsi baseline --kind fixed_prompt \
    --split heldout --seeds "$SEEDS" --run-id "$RUN" 2>&1 | tee logs/fixed-prompt-n10.log
```

**中断后接着跑**（逐局增量落盘 + 跳过已完成 seed + 协议闸）：

```bash
caffeinate -i .venv/bin/python -m gamer_rsi baseline --kind fixed_prompt \
    --split heldout --seeds "$SEEDS" --run-id "$RUN" --resume
```

**预期与观察口径**（用来判断跑批是否正常）：

| 量 | 预期（实测基准 `runs/m1-real-h1n40e/`） |
|---|---|
| 单局决策点数 `D` | ≈ 185（完整局；`steps/D` ≈ 1.8） |
| 后端调用/决策点 | 1.6–1.8（含 JSON 修复与语义重试） |
| `t_d` p50 | ≈ 8 s（prompt 2500–3000 token） |
| 单局耗时 | ≈ 43 min 模型时间 / ≈ 90 min 墙钟（2 worker 共享） |
| 10 局总耗时 | **≈ 7.5–8.5 h** |
| 正常信号 | 每 ~90 min 出现 2 个 `episodes/heldout/*baseline-fixed_prompt*.jsonl`；`n_dropped=0` |
| 异常信号 | 出现 `error_code=E_EVAL_TIMEOUT`（超时太紧）或大量 `degraded=true`（模型加载失败） |

进度自查（随时可跑，不改任何状态）：

```bash
.venv/bin/python - <<'PY'
import json, collections
from pathlib import Path
run = Path("runs/m1-real-h1n10b")
per = collections.Counter(); calls = 0; sf = 0; first = 0; lat = []
for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines():
    try: r = json.loads(line)
    except Exception: continue
    if r.get("event") == "policy.decision": per[r.get("episode_key")] += 1
    if r.get("event") == "llm.call":
        calls += 1; lat.append(r.get("latency_ms") or 0)
        if r.get("attempt") == 1: first += 1
        if r.get("error_code") == "E_LLM_SCHEMA_INVALID": sf += 1
done = sorted((run / "episodes" / "heldout").glob("*fixed_prompt*.jsonl"))
lat.sort()
print(f"已完成 {len(done)}/10 局｜调用 {calls}｜schema 失败 {sf}/{first}｜"
      f"p50 {lat[len(lat)//2] if lat else '-'} ms")
for k, v in sorted(per.items()): print("  进行中:", k, v)
for p in done:
    d = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    print(f"  ✅ seed {d['seed']}: {d['outcome']} dropped={d['dropped']} D={d['decisions_llm']} "
          f"steps={d['steps']} stages={d['stages_cleared']}/6 fallback={d['fallback_decisions']}")
PY
```

> 跑批结束后脚本会自动写 `runs/$RUN/baselines/fixed_prompt-heldout.json` 与
> `runs/$RUN/baselines/registry.csv`（**不可覆盖**：同名时加时间戳后缀）。若进程被杀，
> 「已完成 N 局」不会被归档——必须在**所有 seed 都完成**后 `run_baseline` 才归档。

---

## 3. 验收（逐条给证据，不要只看退出码）

```bash
RUN=m1-real-h1n10b
cat runs/$RUN/baselines/registry.csv          # 三行：random / heuristic / fixed_prompt
cat runs/$RUN/baselines/fixed_prompt-heldout.json   # 逐字段核对下面这张表
.venv/bin/python -m gamer_rsi audit leak --run-id $RUN          # 必须 clean / 退出码 0（G4）
.venv/bin/python -m gamer_rsi eval report --split heldout --baseline all --run-id $RUN
```

| 检查项 | 期望 |
|---|---|
| 归档存在 | `runs/$RUN/baselines/fixed_prompt-heldout.json` |
| `metrics.n` | **10**（若 9 说明有整局降级被剔除，必须解释；见下） |
| `metrics.degraded_n` | **0**（>0 说明模型/后端不可用，该局不算样本） |
| `metrics.n_dropped` | **0**（>0 = 有局超时/崩溃，必须查 `error_code` 并在报告里如实记） |
| `model_hash` | `qwen3-8b-q4`（**实际加载的**模型 id，不是配置照抄） |
| `model_backend` | `mlx` |
| `git_commit` | 跑批时的真实短 sha（非空） |
| `seeds_hash` / `protocol_hash` | `sha256:2ba235a7…`（与 `configs/seeds.yaml` 一致） |
| `seeds` | `[2000…2009]`（10 个） |
| `protocol`（逐局 jsonl 里） | `episode-protocol-v2+<fixed_v1 模板摘要>`（10 局必须**完全一致**） |
| `reproduce` | 形如 `python -m gamer_rsi baseline --kind fixed_prompt --split heldout --seeds 2000,…,2009 --run-id m1-real-h1n10b`（**不含** `--basket explicit`；若含，说明 `_reproduce_command` 的修复不在，请报告） |
| `seed_timeout_s` | `14400` |
| `n_workers` | `2` |
| `win_rate` + Wilson CI | 填进报告（这是本任务的**核心产出**）；与 M1 的 n=10 子集（`fixed_prompt` 1/9 = 11.1%，CI [0.02, 0.44]）对比是否一致 |
| `audit leak` | `verdict=clean`、0 findings、退出码 0（评估 run 上不得有告警，F3） |
| `eval report` | 生成 `generations/gen-000/report.json` + `report.sha256`；`heldout.n` 只含代际局（**不含** `baseline-*`，F5） |

**必须记录的实测耗时**（本任务的价值所在，写到分钟）：

- 跑批起止时间（`date -u +%FT%TZ`，从随机基线开始到 `fixed_prompt` 结束）；
- 10 局的 `D` / `steps` / `decisions_llm` 列表与均值；
- 后端调用总数、`t_d` p50/p95、`schema 失败率`、`fallback` 总数；
- 据此算出的「单局模型时间」与「n=10 墙钟」，与 §14.18 的预测（43 min / 8.5 h）对照。

---

## 4. 回填文档（改完跑测试再提交）

| 文件 | 改什么 |
|---|---|
| `docs/reviews/2026-10-04-M1验收报告.md` | ① §13.2 第 6 项状态改为「✅ 已完成（n=10，见 §14.6）」并写明 n=40 的部分另见 §14.18；② §14.6 的表格把 `fixed_prompt` 行填上 `n` / `win_rate` / CI / `degraded_n` / 实测耗时；③ §14.18 顶部加一句「按用户决定已缩小到 n=10，实测归档见 `runs/m1-real-h1n10b/`，n=40 的成本实测结论不变」；④ §10 变更记录追加 v1.4 一行 |
| `docs/模块拆分Spec-GameRSI.md` | §8.6 的成本模型段把「n=40 fixed_prompt ≈29–34 h」补上「（n=10 子集实测：<墙钟> h；win_rate …）」 |
| `docs/项目排期-GameRSI.md` | §2 的 M1 行把「n=40 未跑完」改为「n=10 子集已归档（win_rate / CI / 实测耗时）」；§8 追加 v1.4 |
| `docs/prd/MOD-08-evals-PRD.md` | §15 实际交付里补一句 n=10 归档事实与实测速率 |
| `docs/prd/MOD-04-policy-PRD.md` | §15 里若引用 n=40 的地方同步为 n=10 |

提交前：

```bash
.venv/bin/python -m pytest -q                 # 全量必须全绿（当前基线：1279 passed / 2 skipped）
.venv/bin/python -m gamer_rsi guards check --root gamer_rsi    # 0 findings
git status --short                            # 确认没有 runs/、logs/、models/ 混进来
```

提交信息建议：`docs(M1): 归档 fixed_prompt n=10 子集实测（win_rate …，耗时 …）；§13.2 第 6 项收口`

---

## 5. 硬约束（违反会让测量作废）

1. **跑批期间不要改 `harness/prompts/decision.md` 或 `gamer_rsi/agent/prompts/fixed_v1.md`**：
   每个 episode 都会重新读模板，改了就会得到「一半旧契约、一半新契约」的混合样本。
   若必须改，就换新 `--run-id` 从头跑（`--resume` 会被测量协议闸拒绝，这是设计行为）。
2. **不要并发跑第二个 MLX 任务**（`inference probe/bench/robustness`、`play --agent llm`、
   另一个 `baseline`）——模型是单实例，`max_inflight=1`，并发会让 `t_d` 与耗时全部失真。
3. **不要为了让数字好看而调 `--seeds` 或删局**：`episodes/` 里的局是取证工件；要排除某局必须
   在报告里说明理由（并记住 `--resume` 会把它当作已完成）。
4. **不要 `git add runs/ logs/ models/`**（都已在 `.gitignore` 里）；`docs/html/` 的同步由另一个
   session 负责，本任务不要动。
5. 跑批前先 `pmset -g` 确认机器不休眠；笔记本合盖仍会睡（F19），长跑请保持开盖或插电。

---

## 6. 失败模式与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| `E_EVAL_TIMEOUT`（`n_dropped>0`） | 单局墙钟超过 `seed_timeout_s`（共享模型时 ≈90 min） | 说明该局确实超预算；如实记录，并把 `seed_timeout_s` 再调到 21600 s 后**换新 run-id** 重跑受影响 seed |
| 大量 `degraded=true` / `E_LLM_LOAD_FAILED` | 权重/依赖问题 | 停下来查 `models/Qwen3-8B-4bit` 与 `mlx_lm` 版本，修好后重跑（别把 degraded 局当样本） |
| `E_IO_RUN_CONFLICT` | 该 run 目录的 `config_hash` 与当前配置不一致（改过 `configs/`） | 换新 `--run-id`（不要删旧 run，它是证据） |
| `E_EVAL_ARM_NOT_ALLOWED …测量协议…`（`--resume` 时） | `episodes/` 里有不同协议/不同模板的局 | 换新 `--run-id` 重跑 |
| 跑批看起来「卡住」但机器没在算 | macOS 空闲休眠（F19） | 确认用了 `caffeinate -i`；`events.jsonl` 里相邻事件的间隔会暴露休眠 |
| 退出码 0 但 10 局只归档了 9 局 | 有整局降级被剔除（Spec §8.5） | 查 `degraded_reason`；在报告里如实说明（M1 的 n=10 就是这么来的：9 局有效） |

---

## 7. 完成标准（Definition of Done）

- [ ] `runs/m1-real-h1n10b/baselines/` 下三条 `*-heldout.json` + `registry.csv`，溯源字段全部合规（§3 的表逐条打勾）；
- [ ] `fixed_prompt` 的 `n` / `win_rate` / Wilson CI / `degraded_n` / `n_dropped` 已回填报告 §14.6；
- [ ] 实测耗时（单局模型时间、n=10 墙钟、调用数、`t_d` p50/p95、schema 失败率、fallback 数）已回填 §14.18 与 Spec §8.6；
- [ ] `audit leak` 在评估 run 上 clean / 退出码 0；
- [ ] 全量 `pytest -q` 全绿、`guards check` 0 findings；
- [ ] 文档改动已提交（一个提交即可），提交信息含 win_rate 与实测耗时；
- [ ] 最终回复里给出：归档路径、三条基线的 win_rate + CI、实测耗时表、「与 M1 n=10 子集对比」的一句话结论。
