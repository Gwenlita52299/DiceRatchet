# DiceRatchet：本地小模型在自建骰子 roguelike 里递归自我改进

> 让一个跑在 Mac 上的 8B 级本地模型，在自己搭建的防作弊沙箱里反复玩一款骰子 roguelike。每一局它都经历"玩 → 验证 → 反思 → 改进自己"（策略记忆、prompt、harness 代码），并用**不可作弊的 held-out 测量**回答一个问题：它真的在变强吗？
>
> 定位：学习 + 作品集 · 状态：**G0 游戏本体已完成；M0 环境已完成并通过验收（2026-10-03），下一步 M1 基线**（排期见 [项目排期](docs/项目排期-DiceRatchet.md)，验收见 [M0 验收报告](docs/reviews/2026-10-03-M0验收报告.md)）

## 为什么做这个

RSI（Recursive Self-Improvement，递归自我改进）的公开讨论大多停留在论文或思想实验里。本项目想在消费级硬件上**真实跑通一个最小闭环**，并把文献中反复出现的几种失败模式当作一等公民来测量：

- **自我确认环路**：模型"觉得"自己变强了。对策是所有结论只看 held-out 与 test seed，并且 seed 三方隔离（INV-5）。
- **奖励投机 / 环境作弊**：对策是只给白名单观测，harness 代码在受限子进程里执行，蜜罐和泄漏审计全程开启（INV-1～4）。
- **模式坍缩**：对策是监控动作熵曲线与 JSD，做消融，并区分"反思 + 记忆"和"自编辑 harness"各自贡献了多少增益。

背景调研见 [RSI调研纪要](RSI调研纪要.md) 与 [文献综述 2024–2026](文献综述-RSI-2024-2026.md)。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 调研与计划 | 调研纪要、文献综述、项目计划、指导建议 | ✅ |
| G0 游戏本体 | 网页版骰子 roguelike「破咒骰」：纯函数规则内核 + Node 后端 + 原生前端（`src/game`） | ✅ 49 项测试通过，5 组 golden |
| 规格层 | 模块拆分 Spec v1.4 + GAME-00～02 + MOD-01～09 技术 PRD；已完成一轮评审，36 项决策全部回填 | ✅ |
| M0 环境 | MOD-09 骨架、MOD-01 Python 移植、MOD-02 沙箱 | ✅ 2026-10-03 验收通过（[报告](docs/reviews/2026-10-03-M0验收报告.md)）：5 组 golden 逐行对拍、200 局差分 0 分歧、heuristic 差 0.0pp、G1–G6 全过 |
| M1 基线 | MOD-03 本地推理：后端契约 + 三级 JSON 修复 + 内存预算/并发限流 + 聚合指标 | ✅ 代码落地（`tests/unit/inference` / `tests/integration/test_inference_*` / `tests/golden/inference`）；真实 8B 200 条跑批待权重落盘（`inference robustness`） |
| M1 基线 | 决策策略（MOD-04）+ 三基线 | 计划 → 2027-01-14 |
| M2 闭环 | 反思记忆（MOD-05）+ 闭环编排（MOD-06）+ harness 自进化（MOD-07） | ✅ 代码落地：`tests/unit/{memory,loop,evolve}` + `tests/integration/{test_loop_*,evolve}`；主力臂已定（`C1` = 三角色全 `deepseek-flash`，Spec v2.5 B-43/B-45/B-46/B-51/B-52/B-56..B-58）、C5 的门已按该臂改造（`truncation` 改相对门）；**端到端跑代已跑通**：§3.2 实验（run `m2-s32b-smoke-c1-medium6`）产出真实一代（候选 `win_rate 0.6757` vs parent `0.6250`、McNemar `p=0.7905` ⇒ 合法拒绝，实证 D-3），并已处置 F32–F47；见检查单 §12/§13 |
| M3 测量 | held-out 裁判、消融、多样性、test 终检 | 计划 → 2027-04-22 |

## 系统结构

![模块依赖图](docs/assets/module-deps.svg)

四层共九个模块，另预留 MOD-10：

| 层 | 模块 | 一句话 |
|---|---|---|
| L0 环境 | MOD-01 env-rules · MOD-02 env-sandbox | JS 内核的 Python 逐位移植；白名单观测投影、受限执行、审计与蜜罐 |
| L1 智能体 | MOD-03 inference · MOD-04 policy · MOD-05 memory | MLX 本地推理 + 约束解码；决策计划、三层校验与 fallback；反思记忆库 |
| L2 进化 | MOD-06 loop · MOD-07 evolve | 闭环编排、断点续跑与成本闸门；harness 候选生成、held-out 裁判、版本化与回滚 |
| L3 测量与工程 | MOD-08 evals · MOD-09 infra | 基线、指标、坍缩监控与消融；配置、事件、CLI、守卫、git |

一局游戏的默认设定是 hard 难度 × 6 关：启发式 bot 的通关率 45.5%，中位数 232 env 步 / 96 个决策点（n=200，seed 1–200；复现命令见 Spec §8.6）。每代共 **114 局**（训练 30 局 + parent 与 1 个候选的冒烟 2 局与 held-out 40 局；2026-10-06 D-1 定稿）。**每代耗时按臂分列**：本地 8B（MLX）实测单价 **19.6 min/局 ⇒ ≈37 小时/代**；**当前 M2 闭环主力臂 `C1`（三角色全 `deepseek-flash`）实测 2.09–2.52 min/局 ⇒ ≈4.0–4.8 小时/代、约 ¥84–100/代**（2026-10-09 裁决，Spec v2.0 B-43）——API 臂下真正的瓶颈是花费而不是墙钟。详见 [Spec §8.6](docs/模块拆分Spec-DiceRatchet.md)。

### 七条不变量

任何 PR 都不得违反：

| # | 内容 |
|---|---|
| INV-1 | agent 只能通过 `SandboxEnv` 触达环境 |
| INV-2 | 观测是白名单投影 |
| INV-3 | prompt 只能包含四类白名单上下文 |
| INV-4 | harness 代码受限执行 |
| INV-5 | train / heldout / test 的 seed 三方隔离 |
| INV-6 | 随机性只来自显式 seed |
| INV-7 | 主指标只用环境原生结果（胜负 / 通关数 / 剩余心 / 胜场） |

## 快速开始（游戏本体）

需要 Node ≥ 18，零 npm 依赖：

```bash
cd src/game
node server.js                                          # 浏览器打开 http://127.0.0.1:8080
node --test "tests/**/*.test.js"                         # 全部测试
node scripts/balance.js --n 200 --strategy all --duel    # 平衡报告：通关率 + 学派×敌人矩阵
node scripts/golden-gen.js                               # 重新生成 golden 快照（改规则或配表后运行，并审阅 diff）
```

玩法、架构和内核 API 见 [src/game/README.md](src/game/README.md)。

RSI 训练链路（Python，`dice_ratchet/`）已落地 MOD-01…09（含 MOD-07 evolve 的初版），
命令面对齐 [Spec §4.6](docs/模块拆分Spec-DiceRatchet.md)：

```bash
python -m dice_ratchet env selftest --seed 1                                       # 期望退出码 0
# 注意（F10）：--model/--mode/--seed 会并入 config_hash——同一 run-id 只能跑覆盖集合相同的命令
# 下面三条 inference 覆盖相同（--model mock --mode eval --seed 7），可共用 <inf_run_id>
python -m dice_ratchet inference probe      --model mock --mode eval --seed 7         --run-id <inf_run_id>   # 期望退出码 0
python -m dice_ratchet inference bench      --model mock --mode eval --seed 7 --n 50  --run-id <inf_run_id>   # 期望退出码 0
python -m dice_ratchet inference robustness --model mock --mode eval --seed 7 --n 200 --run-id <inf_run_id>   # 期望退出码 0
# MOD-06 闭环 + MOD-07 自进化：冒烟不触网（--no-reflect 免模型），真实演化需要 MOD-03 客户端
python -m dice_ratchet loop smoke --seeds train:1000-1002 --k 3 --no-reflect           # 期望退出码 0
python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl --run-id <loop_run_id>
python -m dice_ratchet loop status --run-id <loop_run_id>                              # 期望退出码 0
# HITL 挂起（退出码 0）→ 人工裁决 → 续跑执行 accept/rollback
python -m dice_ratchet evolve approve  --run-id <loop_run_id> --generation 1           # 期望退出码 0
python -m dice_ratchet evolve rollback --run-id <loop_run_id> --generation 1 --reason "效果可疑"
python -m dice_ratchet loop run --run-id <loop_run_id> --resume                        # 期望退出码 0
# eval report 不带 --model/--mode/--seed（与 inference 族覆盖不同）→ 必须换一个 run-id
python -m dice_ratchet eval report --run-id <eval_run_id> --split heldout --baseline all   # 期望退出码 0
```

## 模型角色与实验臂（配置切换，Issue #1）

三个模型角色 —— `policy`（游戏决策）、`reflection`（局末反思）、`proposer`（harness 修改提案）——
都只经 MOD-03 的统一 `InferenceClient` 取模型。**换模型、换角色组合、切对照臂全部只改配置**，
MOD-04/06/07/08 的业务代码不动，也不出现 `from mlx_lm import ...` / `from openai import ...`。

优先级（高 → 低）：`experiment.roles.<role>` > `experiment.arms[arm]` > `models.<role>` > `inference.model_id`。
空字符串表示「继承下一层」，所以 `--model mock`（写的是 `inference.model_id`）在 A 臂下仍一次覆盖全部角色。

六个实验臂（[configs/default.yaml](configs/default.yaml) 的 `experiment.arms`，臂定义计入 `config_hash` → 换臂即换 run-id 族）：

| Arm | 决策 / 反思 | Proposer | Harness | 用途 |
|---|---|---|---|---|
| A0 / A1 | Qwen3-8B | Qwen3-8B | 固定 / 演化 | Qwen 主线 baseline 与 RSI 主实验臂 |
| B0 / B1 | Qwen3-8B | `deepseek-flash` | 固定 / 演化 | 「proposer 能力」对照 |
| C0 / C1 | `deepseek-flash` | `deepseek-flash` | 固定 / 演化 | 全量外部模型对照 |

外部模型是 **`deepseek-flash`**（DeepSeek-V4.1-Flash，1M 上下文）：2026-10-08 实测
`GET https://api.deepseek.com/v1/models` 只有 `deepseek-flash` 与 `deepseek-v4-pro`，
原登记名 `deepseek-chat` 已下线（见 [configs/models.yaml](configs/models.yaml)）。
后者的 `deepseek-v4-pro` 已登记但未被任何臂使用，供后续「proposer 能力 vs 成本」对照直接切。
成本按官方定价登记为**人民币 + 高峰档上界**，审计里记 `cost_estimate{amount,currency,basis}`；
未登记单价的模型记 `null`（不拿 0 冒充免费）。

**flash 必须显式关闭思考模式**（`api_thinking: disabled`）：该模型默认开思考模式，思维链会把
`max_tokens` 吃光、`content` 返回空串，于是每次都靠 MOD-03 的修复重试才拿到 JSON —— 白花一次调用。
实测（同一 probe）：开思考 895 输入/166 输出 token、墙钟 3459 ms、`attempts=2`；
关思考 215/109 token、869 ms、`attempts=1`。官方文档也印证：思考模式忽略 `temperature`，
非思考模式下 `temperature` 才生效，正是 B-14 需要的确定性口径。

```bash
# 方式一：改 configs/default.yaml 的 experiment.arm / models.* / experiment.roles
# 方式二：DICERATCHET_* 环境变量（B-30 的 __ 分层；CLI 目前只有 8 个显式覆盖键，没有通用 --set）
export DICERATCHET_EXPERIMENT__ARM=B1                    # 等价于 experiment.arm: B1
export DICERATCHET_MODELS__PROPOSER=deepseek-flash        # 只钉 proposer，其余角色不动
python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl --run-id <loop_run_id>
```

### API key：本地密钥文件（不入库）

真跑 B/C 臂需要 `DEEPSEEK_API_KEY`。除了 `export`，也可以放在仓库根一个**被 .gitignore 忽略**的
`.env` 里（省得每次 export）：

```bash
cp .env.example .env && chmod 600 .env    # 然后把 key 粘到 DEEPSEEK_API_KEY= 后面
```

语义（实现在 [dice_ratchet/inference/secrets.py](dice_ratchet/inference/secrets.py)）：

- **真实环境变量优先**：已经 `export` 时以环境变量为准，文件不覆盖；
- **只读注册表声明的那个名字**（`configs/models.yaml` 的 `api_key_env`，当前是 `DEEPSEEK_API_KEY`）——
  文件里其它行一律无效，包括 `DICERATCHET_*`，所以这个文件**不能用来偷改配置**；
- **不改进程环境**：只做查找，绝不 `os.environ.update(...)`，受限子进程与 `config.json` 的环境快照都看不到它；
- key 只进内存：不写 `harness/`、run JSON、事件或日志；审计与 provenance 只记来源
  （`api_key_source: environment | env_file | missing`）与「是否可用」的布尔值；
- 文件对同组/其他用户可读时记一条 `llm.api.key_file_permissions` 告警（建议 `chmod 600`）；
- 想只用环境变量：把 `inference.api_key_file` 设为空字符串。

`.gitignore` 里必须有 `.env` 这条：`infra.git_ops` 把 `git status --porcelain` 的任何输出都算
「worktree 不干净」（未跟踪文件也在内），否则 MOD-07 会直接拒绝演化。

臂与角色绑定都是**静态实验决定**：它们不进 `harness/config.json` 白名单（MOD-07 不能自己改实验臂），
但会进 `config_hash`，所以换臂后必须换 run-id。

每个 run 落 `runs/<run_id>/models_provenance.json`：三角色各自的 model_id / provider / backend /
model_revision / online / `api_key_present` / `api_key_source`，加上臂、harness 模式与 `config_hash`。
DeepSeek 侧另有审计：`runs/<run_id>/llm_api/` 按 request hash 存 prompt hash / sampling /
response hash / usage / 成本估算 / **响应正文**（`response_text`，记录 v2）；同一请求而响应变化时
**不覆盖旧证据**，另存变体（`.v<n>.json`）并记 `llm.nondeterministic`（`E_LLM_NONDETERMINISTIC`）。
离线复现用 `inference.api_mode=replay`（只读历史响应，缺记录**或缺响应正文**即 fail-closed，
绝不把审计元数据当模型输出，也不冒充新样本）。**实测提醒**（报告 §11）：`deepseek-flash` 在
`temperature=0.0` 下也不保证跨时间确定 —— 两次同配置真跑之间 9 个逐字节相同的请求拿到
9 个不同响应，
所以 A/B/C 对照必须 `n≫1`、按 episode seed 配对，逐位复现只能靠 replay。
跨 run 比对用只读脚本：`.venv/bin/python scripts/compare_api_runs.py <run_a> <run_b>`。

## CI 门禁

门禁是**一条脚本**，本地与 CI 跑的是同一份（Spec §9 G1–G6 与 INV-1/INV-6 的可执行化）：

```bash
./scripts/ci_gate.sh                  # 快速门（约 1 分钟）：guards + selftest + pytest -m "not slow" + 跨 PYTHONHASHSEED
./scripts/ci_gate.sh --with-slow      # 深门（约 10 分钟）：全量 pytest（5 组 golden / 200 局差分 / 200 局 bot 对拍）+ node 测试 + golden 基准独立性
./scripts/ci_gate.sh --coverage       # 附带覆盖率报告（G1 判据）
```

`guards check` 反例自检也内含其中：门禁必须能拦下 `tests/fixtures/guards/`（退出码 4），否则视为门禁失效。

CI 侧（[.github/workflows/ci.yml](.github/workflows/ci.yml)，Node 22 / Python 3.12）把两者拆成两个作业，**按需触发以省 CI 分钟**（实测：快速门 ≈4.5 分钟、深门 ≈13 分钟）：

| 事件 | 快速门 | 深门（对拍） |
|---|---|---|
| `push` | ✅ | — |
| `pull_request` | ✅ | ✅ |
| tag push（`refs/tags/**`） | ✅ | ✅ |
| 手动 `workflow_dispatch` | ✅ | ✅（可 `-f slow=false` 关掉） |

```bash
gh workflow run ci.yml -F slow=true    # 需要时手动补一次慢门（push 不会自动跑）
```

## 技术栈

| 组件 | 选型 |
|---|---|
| 硬件 | Apple Silicon Mac，24 GB 统一内存 |
| 模型 | 阶段一 Qwen3-8B（MLX 4bit），阶段二 Qwen3-30B-A3B（MoE，Q4） |
| 游戏 | Node.js（原生 http + ESM）、原生 HTML/CSS/JS，内核为整数纯函数 |
| 训练链路 | Python 3.11；确定性 RNG 为 xmur3 + SplitMix64，与 JS 内核逐位一致 |

## 仓库结构

```
.
├── README.md                     # 本文件
├── RSI调研纪要.md                 # 为什么做：理论 / 实证 / 失败模式 / 治理
├── 文献综述-RSI-2024-2026.md
├── 项目计划-DiceRatchet.md            # 做什么：目标、沙箱纪律、里程碑、验收
├── 项目指导建议-DiceRatchet.md
├── docs/
│   ├── README.md                 # 文档中心（阅读顺序、PRD 索引）
│   ├── 模块拆分Spec-DiceRatchet.md    # 模块边界与接口的唯一事实来源（v1.4）
│   ├── 项目排期-DiceRatchet.md        # 排期与甘特图
│   ├── prd/                      # GAME-00～02、MOD-01～09 技术 PRD
│   ├── reviews/                  # 评审记录
│   ├── assets/                   # 生成的 SVG（模块依赖图、甘特图）
│   └── tools/gen_diagrams.py     # 图表生成脚本（零依赖）
├── schemas/                      # 跨模块 JSON schema（Spec §4.5，MOD-09 登记）
├── harness/                      # harness 工件（Spec §4.4）：config.json（可演化层，v1 = {} 无覆盖）
│                                 #   + prompts/（MOD-04 读）+ tools/（MOD-07 写）+ versions/v{N}/（MOD-07 不可变快照）
├── tests/                        # unit / integration / golden（Python 训练链路）
├── dice_ratchet/                    # RSI 训练链路：env / sandbox / inference / infra
├── scripts/ci_gate.sh            # CI 门禁入口（本地与 CI 同一份）
├── .github/workflows/ci.yml      # CI：gate（快速）+ parity（golden / 差分对拍）
├── src/game/                     # G0 游戏本体：kernel / backend / frontend / content / tests
└── papers/                       # 参考论文 PDF（未纳入 git）
```

## 文档导航

1. [项目计划](项目计划-DiceRatchet.md)：目标与验收。
2. [模块拆分 Spec](docs/模块拆分Spec-DiceRatchet.md)：切成几块、怎么接起来。
3. [GAME PRD](docs/prd/)：游戏规则的唯一真相。
4. [MOD PRD](docs/prd/)：每个模块怎么实现。
5. [项目排期](docs/项目排期-DiceRatchet.md)：什么时候完成。
6. [评审记录](docs/reviews/)：改了什么、为什么改。

文档与代码冲突时，以 Spec 为准。新增的临时决策登记到 Spec §11.2（从 P-31 起编号）。

## 重新生成图表

```bash
python3 docs/tools/gen_diagrams.py    # 写入 docs/assets/*.svg，并打印排期明细表
```

## 说明

- `papers/` 下的 PDF 约 85 MB，没有纳入版本控制，可按文件名中的 arXiv 编号重新下载。
- `runs/`、`logs/`、`.local/` 是运行产物，已写入 `.gitignore`。
- 许可协议未定；在确定之前，代码与文档保留所有权利。
