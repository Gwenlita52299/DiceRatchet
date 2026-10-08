# MOD-04 policy —— 决策收口（Observation → prompt → Action）

对齐 [模块拆分 Spec v1.4](../../docs/模块拆分Spec-DiceRatchet.md) §4.3/§4.4/§4.7 与
[MOD-04 PRD](../../docs/prd/MOD-04-policy-PRD.md)。本模块是"LLM 输出非 JSON / 非法动作多"
这条最大风险的唯一收口处：把 `Observation` 组装成 prompt，把 LLM 原始文本变成 `Action`，
并保证模型再离谱也不污染环境与指标。

## 文件与职责

| 文件 | 职责 | PRD |
|---|---|---|
| `policy.py` | `PolicyAgent`：一个决策点的计划 + 重试 + fallback 编排；`DecisionResult`/`StepRecord`/`LLMCallMeta`/`EpisodeContext` | FR-04-7..10、14..16 |
| `parser.py` | 三级校验的 ① 语法/schema（kind 必填字段代码表）与 ② 语义集合判定 | FR-04-7/8、§11 矩阵 |
| `heuristic.py` | `heuristic_policy(obs, legal, hints)`：GAME `bots.js::greedy_counter` 的纯 obs 移植；fallback 与 MOD-08 heuristic 基线共用 | FR-04-14 |
| `prompts/assembler.py` | INV-3 唯一注入出口：模板加载/占位符/预算裁剪/`prompt_hash` | FR-04-1..6 |
| `prompts/task.md` | INV-3 第 ④ 来源（人工版本化，**非**演化对象，B-27） | §7 |
| `prompts/decision_fallback.md` | 模板不可用时的内置兜底（`fallback_used=True`） | FR-04-1 |
| `prompts/fixed_v1.md` | MOD-08 `baseline_fixed_prompt` 的冻结模板 | §7 |
| `tools/registry.py` | `@harness_tool` / `rank_actions` 注册表：`gate()` 静态门禁 → 逐文件弃用 → 子进程 worker → 恒等降级 | FR-04-11..13 |

`harness/prompts/decision.md` 是 **MOD-07 演化面**内的工件（v0 已随仓库提供）；MOD-04 只读，
不在运行期写入。

## 对外接口

```python
from dice_ratchet.agent import EpisodeContext, PolicyAgent, BackendInferenceClient

agent = PolicyAgent(cfg.agent.policy, llm_client, memory_store, harness_dir=Path("harness"))
agent.reset(EpisodeContext(run=run_id, episode=key, seed=seed, split=split,
                           mode="train", tick=tick, memory=frozen_view))
result = agent.play_decision(env, obs, ctx=ctx)     # 唯一对外决策入口
for step in result.steps:                           # MOD-06 逐行写轨迹
    ...
agent.close()
```

- `llm_client` 只需实现同步 `generate_json(messages, schema, *, mode, sampling, seed)`；
  真实 MOD-03 后端用 `BackendInferenceClient(backend, model_id=...)` 适配（内部
  `asyncio.run`）。
- 失败语义：模型不可用/超时 → `source="fallback"` 且 `degraded=True`（整局不计入评估样本）；
  尝试耗尽/泄漏阻断 → `env.reject(final=True)` + 启发式一招；**判负只由 MOD-02 负责**。
- MOD-04 不写日志、不建目录、不落盘（NFR-04-4）；事件只发 `policy.decision` /
  `policy.fallback` / `policy.reject` / `sandbox.audit_finding`。

## 配置（`policy.*`，登记在 MOD-09 `infra/config.py`）

见 PRD §9。要点：`retry_max=2`（每决策共 3 次尝试）、`temperature` 训练期 clamp 到
0.7–1.2、评估期恒 `0.0`（B-14，`harness/config.json` 不可覆盖）、`leak_scan` 恒 `true`、
`tool_timeout_ms=50`、`prompt_token_budget=3072`。

## 命令（G6，可直接复制执行）

```bash
# 单测（prompt / parser / tools / heuristic / 决策编排 / 性能回归）
.venv/bin/python -m pytest tests/unit/agent -q

# golden：prompt 正文与 prompt_hash 快照（改动模板/组装逻辑后必须显式重生成）
.venv/bin/python -m pytest tests/golden/agent -q
.venv/bin/python tests/golden/agent/regen_policy_golden.py

# 集成：真实 SandboxEnv（G3 确定性 / G4 泄漏扫描 / G5 轨迹行 schema）
.venv/bin/python -m pytest tests/integration/test_policy_with_sandbox.py -q

# MOD-09 守卫：确定性（禁 random/墙钟）+ 分层 import（INV-1 禁 dice_ratchet.env.*）
.venv/bin/python -m dice_ratchet guards check --root dice_ratchet/agent

# 覆盖率门（NFR-04-5：parser/assembler ≥85%，policy ≥70%）
.venv/bin/python -m pytest tests/unit/agent tests/golden/agent \
  --cov=dice_ratchet.agent --cov-report=term-missing -q
```

> `tests/unit/agent/test_policy_heuristic.py::test_heuristic_win_rate_within_3pp_of_js_reference_on_default_run`
> 是 `@pytest.mark.slow`：按 Spec §8.6 的 n=200、seed 1–200 跑一遍默认局（约 1 分钟），
> 断言 MOD-04 启发式与 JS 参考 45.5% 的差距 ≤ 3pp（实测 48.0%，差 2.5pp）。
> 这批 seed 与 `baseline --basket H1`（40 局，seed 2000–2039）不是同一批，勿混用口径。

`play` 已由组合根接线（2026-10-04，M1 验收 F4）：

```bash
# 离线可直接跑（不需要权重）：random / heuristic
python -m dice_ratchet play --agent heuristic --seeds train:1000-1004 --run-id demo-0001             # 期望退出码 0

# 需要 MOD-03 后端的 agent：llm（PolicyAgent 全链路）/ fixed_prompt
# 注意（F10）：--model 会并入 config_hash——带/不带 --model、或换 --model 都必须换 run-id，否则退出码 2
python -m dice_ratchet play --agent llm --model mock --seeds train:1000-1004 --run-id demo-0002        # 期望退出码 0
python -m dice_ratchet play --agent llm --model qwen3-8b-q4 --seeds train:1000-1004 --run-id demo-0003 # 需权重，期望 0；否则 3
```

拿不到模型时 `llm` / `fixed_prompt` 按 Spec §8.5 降级并**显眼标注**（打印 `degraded_n`），
整批降级则退出码 3（`E_LLM_UNAVAILABLE`），不会以 0 退出掩盖空测量。
`--agent` 取值与 Spec §4.6 一致：`llm|heuristic|random|fixed_prompt`。

## 不变量

- **INV-1**：只从 `dice_ratchet.sandbox.api` 导入环境类型，禁止 `import dice_ratchet.env.*`
  （由 `guards check` 与 CI 拦截）。
- **INV-3**：`prompts/assembler.py::assemble_prompt()` 是唯一 prompt 注入出口。
- **INV-6**：采样 seed 一律 `derive_seed("policy", salt, run, episode, seed, decision, attempt,
  harness_version)`，无 `random`、无墙钟。

## 启发式基线的可观测性缺口（需 MOD-02 裁决）

`greedy_counter` 的两条分支依赖 `deck`（牌库列表）：宝箱在 `len(deck) >= 20` 时跳过、
商店在 `len(deck) >= 14` 时不再买骰子、锻造目标取 `defId == "die_strike"` 的第一颗。
`obs-agent-1.0.0` 的字段表（Spec §4.2）**不含 `deck`**，因此投影观测上无法逐字复刻。

现状与实测（2026-10-04，n=200，seed 1–200，默认局 hard × 6 关）：

| 场景 | 与 JS `greedy_counter` 的一致性 |
|---|---|
| 观测含 `deck`（`GameEngine.observe(state,"full")`） | **零分歧**（20 局、约 4.7k 决策全 phase 对拍，`test_heuristic_is_line_by_line_port_when_obs_has_deck`） |
| 投影观测（`obs-agent-1.0.0`，无 `deck`） | 通关率 **48.0%** vs JS 参考 **45.5%** → 差 **2.5pp ≤ 3pp**（`test_heuristic_win_rate_within_3pp_of_js_reference_on_default_run`） |

已用的公开代理：`shop.removable` 实测等价于全牌库的 `dieId` 列表（`len` 与 `deck` 一致），
因此商店的 `deck_len < 14` 判定是精确的；剩余差异只来自宝箱的 `deck >= 20` 跳过与
`die_utility` 定向移除（无 defId 可用时保守地不移除）。

建议（不属 MOD-04 权限）：在 MOD-02 观测投影中增加只读的 `player.deckCount`（牌库张数，
不含牌序），即可让启发式在投影观测上逐字等价；属 `OBSERVATION_SCHEMA` 变更，需
schemaVersion 次版本 +1 与 `OBSERVATION_CHANGELOG.md` 记录（FR-02-4）。

