# DiceRatchet：本地小模型在自建骰子 roguelike 里递归自我改进

[![ci](https://github.com/Gwenlita52299/DiceRatchet/actions/workflows/ci.yml/badge.svg)](https://github.com/Gwenlita52299/DiceRatchet/actions/workflows/ci.yml)

> 让一个跑在 Mac 上的 8B 级本地模型，在自己搭建的防作弊沙箱里反复玩一款骰子 roguelike。每一局它都经历「玩 → 验证 → 反思 → 改进自己」（策略记忆、prompt、harness 配置），再用**不可作弊的 held-out 测量**回答一个问题：它真的在变强吗？
>
> 定位：学习 + 作品集 · 状态（2026-10-25）：**闭环已打通，测量链已修到「可判」；但「变强」尚未被证明**。

## 先说结论

这个项目最重要的产出不是「模型变强了」，而是一条**能诚实地否证自己**的流水线。

**已经证明的**

- **闭环能真跑完**：提案 → 静态校验 → 冒烟 → held-out 测量 → C0–C5 门禁 → 版本化 → 落盘，全程无人干预（HITL 门除外），跨代可续跑、可回滚。
- **测量链本身可信**：审计、seed 三方隔离、play 面指纹、臂归档身份、候选补丁是否真的进入决策 prompt —— 每一条都被实测校验过。`m3r5` 的 C2 记录到 `play_hash 1ebe12e1 → 65fabe1a`，证明「候选补丁真的改了打法」，而不是只改了几个字节。
- **accept 的语义是明确的**：「打法确实变了（`play_hash` 变化）+ 没有触发任何判负门」，**不是**「变强了」。
- **优化能降本**：prompt cache-first 改造把唯一 prompt 的缓存命中率从 10.8% 提到 40.1%，成本下降，胜率不变（7/40 vs 7/40，McNemar p=1.0）。这正是一个诚实的负结果：省了钱，没变强。

**还没有证明的**

- **没有任何一次被接受的变化带来统计上可测的提升。** `m3r2` 台账里出现过 1 次 accept（0.05 → 0.275），但事后协议复核认定：它的起点里焊着一个**已被判负**的补丁 P1，对裸 v0 的真实增益只有约 +10 pp，且置信区间大幅重叠。`m3r5` 的第二个 accept 候选（0.100 → 0.250）McNemar **p=0.55**、净 +3 局、平均关数反而从 4.75 降到 4.70。两者都不能当「自我改进」的证据。
- **`C1`（三角色全 `deepseek-flash`）始终不如一个免费的启发式 bot。** 同一个 H1 篮子（hard × 6 关，40 局）：`heuristic` **0.325**，`C1` 最好一次 **0.275**，`m3r5` 起点只有 0.175。
- **在 n=40 上判据不可判，而不是「没提升」。** 同一份 harness、同一批 40 个 seed 的 7 次测量：`0.175 / 0.100 / 0.125 / 0.150 / 0.225 / 0.100 / 0.150`，均值 0.146、SD 0.041、**极差 12.5 pp**。要在 n=40 上以 80% 功效分辨 0.30 关的差异，需要约 **180 局/臂**（每代多 ≈¥145）。

所以本仓库的诚实标题是：**一个把 RSI 从论文拉回可测量地面的负结果 + 一套为此修到自洽的测量仪器。**

## 试验结果总表

所有金额都是 `cost_estimate` 的**高峰价上界**（不是账单）；「实扣」单列。`H1` = hard × 6 关 × 40 局（seed 731000–731039），`H3` = 20 局彩排篮子。

| run-id | 日期 | 臂 / 篮子 | 局数 | 成本（峰值） | 代数 | 结果 |
|---|---|---|---|---|---|---|
| `m1-real-h1n10b` | 10-05 | 三基线 / 旧四位 n=10 | 30 | ¥0 | — | `random` 0/10、`heuristic` 4/10、`fixed_prompt` 0/10 |
| `m1-real-h1n40` | 10-05 | 基线 / 旧 H1（seed 2000–2039） | 80 | ¥0 | — | `random` 0.000、`heuristic` 0.45（**旧篮子**，与今天 H1 不是同一批 seed） |
| `cachefix-b0` | 10-17 | C1 / H1 | 40 | ¥19.74 | A/B | 7/40 vs 7/40，**p=1.0**；缓存命中 10.8% → 40.1% |
| `m2-s32b-smoke-c1-medium6` | 10-09 | C1 / medium×6 | 77 | — | 1 | 候选 0.6757 vs parent 0.6250、p=0.79 → 合法拒绝（结论待复核，见 F66） |
| `m3-formal` | 10-13→14 | C1 / H1 | 382 | ¥344.44（实扣 ¥197.67） | 3 | 0 accept；4 次 parent 测量 0.175/0.100/0.150/0.100；抓出 F58–F71 |
| `m3r2` | 10-16→17 | C1 / H1（起点 v0+P1） | 262 | ¥240.94 | 3（裁决 1） | **1 accept** 0.05→0.275；事后判定起点含已判负的 P1，**不算数** |
| `m3r3` | 10-17 | C1 / H1 | 0 | ¥0.06 | 0 | 中止（9 次调用）⇒ **P2 的独立有效性至今无数据** |
| `m3r4` | 10-18 | C1 / H1（复用 gen-000） | 30 | ¥22.10 | 1 | 提案写了嵌套 config key ⇒ reject / `no_candidate` |
| `m3r5` | 10-19→20 | C1 / H1（起点 v4） | 148 | ¥90.80 | 2 | gen-001 reject 0.100；gen-002 **accept 候选 0.250（p=0.55）**，HITL 门过期、**未落地** |
| `mvp1` | 10-18 | C1 / H3 | 88 | ¥49.66 | 2 | reject 0.30；accept 0.45（`temperature` 在 eval 下不生效 ⇒ 空转） |
| `mvp2` | 10-18 | C1 / H3 | 54 | ¥28.24 | 1 | reject（`memory_top_k`，held-out 无记忆 ⇒ 纯噪声） |
| `mvp3` | 10-18→19 | C1 / H3 | 54 | ¥28.40 | 1 | reject C2（`max_plan_len` 不在生效指纹里） |
| `mvp4` | 10-19 | C1 / H3 | 54 | ¥28.19 | 1 | **accept**（`retry_max:3`，+0.10、n=20、p=0.73）；首次跑完 C0–C5 |

**成本与时长（实测，2026-10 口径）**

| 口径 | 实测值 |
|---|---|
| 每代（74 局：30 train + 4 冒烟 + 40 held-out） | **¥67.70 / ¥70.97 / ¥69.03**（m3r2 三代）；≈**¥0.92/局** |
| 每局（按臂） | `m3r2` ¥0.92 · `m3r4` ¥0.74 · `m3r5` ¥0.61 · MVP ¥0.52–0.56 · `cachefix-b0` ¥0.49 |
| 每代墙钟 | ≈4.1 h（m3r2）· ≈3.1 h（m3r5）· ≈1.5–1.8 h（MVP，44 局/代） |
| 本地 8B（MLX） | ≈19.6 min/局（只用于 M1 真实模型跑批，未进入正式 M3 臂） |

> 早期 README 里的「74 局 ≈¥38/代」「≈¥0.52/局」已作废：前者是旧单价外推，后者只是 `m3-formal` 的**账户实扣**单价，不能当现行成本。

## 为什么做这个

RSI（Recursive Self-Improvement，递归自我改进）的公开讨论大多停留在论文或思想实验里。本项目想在消费级硬件上**真实跑通一个最小闭环**，并把文献中反复出现的几种失败模式当作一等公民来测量：

- **自我确认环路**：模型「觉得」自己变强了。对策是所有结论只看 held-out 与 test seed，并且 seed 三方隔离（INV-5）。
- **奖励投机 / 环境作弊**：对策是只给白名单观测，harness 代码在受限子进程里执行，蜜罐和泄漏审计全程开启（INV-1～4）。
- **模式坍缩**：对策是监控动作熵曲线与 JSD，做消融，并区分「反思 + 记忆」和「自编辑 harness」各自贡献了多少增益。
- **测量噪声**：这一条是项目后期才真正付学费学到的 —— 40 局、40 个 seed 的篮子在 0.15–0.22 的胜率区间里，单次测量的抖动就有 12.5 pp。任何「这一代提升了」的结论都必须先过功效这一关。

背景调研见 [RSI调研纪要](RSI调研纪要.md) 与 [文献综述 2024–2026](文献综述-RSI-2024-2026.md)。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 调研与计划 | 调研纪要、文献综述、项目计划、指导建议 | ✅ |
| G0 游戏本体 | 网页版骰子 roguelike「破咒骰」：纯函数规则内核 + Node 后端 + 原生前端（`src/game`） | ✅ `node --test` 全绿 + 5 组 golden |
| 规格层 | 模块拆分 Spec + GAME-00～02 + MOD-01～09 技术 PRD，决策登记表回填至 **B-96** | ✅ Spec **v2.35** |
| M0 环境 | MOD-09 骨架、MOD-01 Python 移植、MOD-02 沙箱 | ✅ 2026-10-03 验收：5 组 golden 逐行对拍、200 局差分 0 分歧 |
| M1 基线 | MOD-03 本地推理 + MOD-04 策略 + 三基线 | ✅ 契约与单测落地；`random` 0.000 / `heuristic` 0.325（H1 40 局）；`fixed_prompt` 的 H1 40 局**仍未跑** |
| M2 闭环 | MOD-05 记忆 + MOD-06 编排 + MOD-07 harness 自进化 | ✅ 端到端跑通（`m2-s32b-smoke-c1-medium6`）；但该冒烟的候选结论受 F66 影响，**待复核** |
| M3 测量 | held-out 裁判、消融、多样性、test 终检 | 🟡 **仪器已修到自洽，结论是负结果**：多轮正式/彩排（上表），无一次被接受的变化有可测增益；H2 消融与 test 终检**未跑**（建议留到出现真 accept 之后） |

> 2026-10-14 起，C2 的「统计显著性门」被取消（用户裁决 F62）：门只判「候选 `play_hash` ≠ 起点 `play_hash`」，统计量降级为报告项。原因是一个纯改字节、不改打法的 accept 会让「≥1 次 accept」结构性地变成退化通过。之后发现的 F66（候选补丁从未进入决策 prompt）也已修好，并由 `m3r5` 的 C2 实测确认。

## 系统结构

![模块依赖图](docs/assets/module-deps.svg)

四层共九个模块，另预留 MOD-10：

| 层 | 模块 | 一句话 |
|---|---|---|
| L0 环境 | MOD-01 env-rules · MOD-02 env-sandbox | JS 内核的 Python 逐位移植；白名单观测投影、受限执行、审计与蜜罐 |
| L1 智能体 | MOD-03 inference · MOD-04 policy · MOD-05 memory | MLX 本地推理 + 约束解码；决策计划、三层校验与 fallback；反思记忆库 |
| L2 进化 | MOD-06 loop · MOD-07 evolve | 闭环编排、断点续跑与成本闸门；harness 候选生成、held-out 裁判、版本化与回滚 |
| L3 测量与工程 | MOD-08 evals · MOD-09 infra | 基线、指标、坍缩监控与消融；配置、事件、CLI、守卫、git |

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

RSI 训练链路（Python，`dice_ratchet/`）已落地 MOD-01…09，命令面对齐 [Spec §4.6](docs/模块拆分Spec-DiceRatchet.md)：

```bash
python -m dice_ratchet env selftest --seed 1                                       # 期望退出码 0
# 注意（F10）：--model/--mode/--seed 会并入 config_hash——同一 run-id 只能跑覆盖集合相同的命令
# 下面三条 inference 覆盖相同（--model mock --mode eval --seed 7），可共用 <inf_run_id>
python -m dice_ratchet inference probe      --model mock --mode eval --seed 7         --run-id <inf_run_id>
python -m dice_ratchet inference bench      --model mock --mode eval --seed 7 --n 50  --run-id <inf_run_id>
python -m dice_ratchet inference robustness --model mock --mode eval --seed 7 --n 200 --run-id <inf_run_id>
# MOD-06 闭环 + MOD-07 自进化：冒烟不触网（--no-reflect 免模型），真实演化需要 MOD-03 客户端
python -m dice_ratchet loop smoke --seeds train:1000-1002 --k 3 --no-reflect           # 期望退出码 0
python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl --run-id <loop_run_id>
python -m dice_ratchet loop status --run-id <loop_run_id>
# HITL 挂起（退出码 0）→ 人工裁决 → 续跑执行 accept/rollback
python -m dice_ratchet evolve approve  --run-id <loop_run_id> --generation 1
python -m dice_ratchet evolve rollback --run-id <loop_run_id> --generation 1 --reason "效果可疑"
python -m dice_ratchet loop run --run-id <loop_run_id> --resume
# eval report 不带 --model/--mode/--seed → 必须换一个 run-id
# F46-A：--generation 是真过滤；--arm <name|all> 选臂（缺省 = 该代胜出/候选臂，all = 混合池）
python -m dice_ratchet eval report --run-id <eval_run_id> --split heldout --baseline all
```

## 模型角色与实验臂

三个模型角色 —— `policy`（游戏决策）、`reflection`（局末反思）、`proposer`（harness 修改提案）—— 都只经 MOD-03 的统一 `InferenceClient` 取模型。**换模型、换角色组合、切对照臂全部只改配置**，MOD-04/06/07/08 的业务代码不动。

优先级（高 → 低）：`experiment.roles.<role>` > `experiment.arms[arm]` > `models.<role>` > `inference.model_id`。

| Arm | 决策 / 反思 | Proposer | Harness | 用途 |
|---|---|---|---|---|
| A0 / A1 | Qwen3-8B | Qwen3-8B | 固定 / 演化 | Qwen 主线 baseline 与 RSI 主实验臂 |
| B0 / B1 | Qwen3-8B | `deepseek-flash` | 固定 / 演化 | 「proposer 能力」对照 |
| C0 / C1 | `deepseek-flash` | `deepseek-flash` | 固定 / 演化 | 全量外部模型对照（**实际跑的是 C1**） |

外部模型是 **`deepseek-flash`**（DeepSeek-V4.1-Flash，1M 上下文）：2026-10-08 实测 `GET https://api.deepseek.com/v1/models` 只有 `deepseek-flash` 与 `deepseek-v4-pro`，原登记名 `deepseek-chat` 已下线（见 [configs/models.yaml](configs/models.yaml)）。

**必须显式关闭思考模式**（`api_thinking: disabled`）：该模型默认开思考模式，思维链会把 `max_tokens` 吃光、`content` 返回空串。实测同一 probe：开思考 895 输入/166 输出 token、3459 ms、`attempts=2`；关思考 215/109 token、869 ms、`attempts=1`。

```bash
# 方式一：改 configs/default.yaml 的 experiment.arm / models.* / experiment.roles
# 方式二：DICERATCHET_* 环境变量（B-30 的 __ 分层；CLI 目前只有 8 个显式覆盖键，没有通用 --set）
export DICERATCHET_EXPERIMENT__ARM=B1
export DICERATCHET_MODELS__PROPOSER=deepseek-flash
python -m dice_ratchet loop run --agent llm --seeds train:1000-1029 --k 30 --hitl --run-id <loop_run_id>
```

## API key 与隐私

真跑 B/C 臂需要 `DEEPSEEK_API_KEY`。除了 `export`，也可以放在仓库根一个**被 .gitignore 忽略**的 `.env` 里：

```bash
cp .env.example .env && chmod 600 .env    # 然后把 key 粘到 DEEPSEEK_API_KEY= 后面
```

语义（实现见 [dice_ratchet/inference/secrets.py](dice_ratchet/inference/secrets.py)）：

- **真实环境变量优先**；只读注册表声明的那个名字（`configs/models.yaml` 的 `api_key_env`），文件里其它行一律无效；
- **不改进程环境**，key 只进内存：不写 `harness/`、run JSON、事件或日志；审计只记 `api_key_source` 与「是否可用」；
- 文件对同组/其他用户可读时记一条告警（建议 `chmod 600`）；
- 想只用环境变量：把 `inference.api_key_file` 设为空字符串。

本仓库的公开版本做过一次全历史清洗：**任何 commit 里都不含真实 key**（已用真实 key 前缀逐 commit 校验），文档与 golden 里的本机绝对路径也已替换为 `<repo>` / `<workspace>` / `<home>`。prompt 的 `source_ids` 现在只记**仓库相对**模板路径，避免 run 工件再次写入用户名与目录结构。

## CI 门禁

门禁是**一条脚本**，本地与 CI 跑的是同一份（Spec §9 G1–G6 与 INV-1/INV-6 的可执行化）：

```bash
./scripts/ci_gate.sh                  # 快速门：guards + selftest + pytest -m "not slow" + 跨 PYTHONHASHSEED
./scripts/ci_gate.sh --with-slow      # 深门：全量 pytest（5 组 golden / 200 局差分 / 200 局 bot 对拍）+ node 测试
./scripts/ci_gate.sh --coverage       # 附带覆盖率报告（G1 判据）
```

`guards check` 反例自检也内含其中：门禁必须能拦下 `tests/fixtures/guards/`（退出码 4），否则视为门禁失效。

CI 侧（[.github/workflows/ci.yml](.github/workflows/ci.yml)，Node 22 / Python 3.12）把两者拆成两个作业，按需触发以省 CI 分钟：

| 事件 | 快速门 | 深门（对拍） |
|---|---|---|
| `push` | ✅ | — |
| `pull_request` | ✅ | ✅ |
| tag push（`refs/tags/**`） | ✅ | ✅ |
| 手动 `workflow_dispatch` | ✅ | ✅（可 `-f slow=false` 关掉） |

## 技术栈

| 组件 | 选型 |
|---|---|
| 硬件 | Apple Silicon Mac，24 GB 统一内存 |
| 模型 | 阶段一 Qwen3-8B（MLX 4bit），阶段二 Qwen3-30B-A3B（MoE，Q4）；正式实验主臂用 `deepseek-flash` |
| 游戏 | Node.js（原生 http + ESM）、原生 HTML/CSS/JS，内核为整数纯函数 |
| 训练链路 | Python 3.11+；确定性 RNG 为 xmur3 + SplitMix64，与 JS 内核逐位一致 |

## 仓库结构

```
.
├── README.md                     # 本文件
├── LICENSE                       # MIT
├── RSI调研纪要.md                 # 为什么做：理论 / 实证 / 失败模式 / 治理
├── 文献综述-RSI-2024-2026.md
├── 项目计划-DiceRatchet.md            # 做什么：目标、沙箱纪律、里程碑、验收
├── 项目指导建议-DiceRatchet.md
├── docs/
│   ├── README.md                 # 文档中心（阅读顺序、PRD 索引）
│   ├── 模块拆分Spec-DiceRatchet.md    # 模块边界与接口的唯一事实来源（v2.35 / B-96）
│   ├── 项目排期-DiceRatchet.md        # 排期与甘特图
│   ├── prd/                      # GAME-00～02、MOD-01～09 技术 PRD
│   ├── reviews/                  # 每轮实验与评审记录（含全部数字出处）
│   ├── handoff/ · tasks/         # 交接提示与任务单
│   ├── patches/                  # 已归档的 prompt/缓存补丁
│   ├── assets/                   # 生成的 SVG（模块依赖图、甘特图）
│   └── tools/gen_diagrams.py     # 图表生成脚本（零依赖）
├── schemas/                      # 跨模块 JSON schema（Spec §4.5）
├── harness/                      # harness 工件：config.json + prompts/ + tools/ + versions/v{N}/
├── tests/                        # unit / integration / golden（Python 训练链路）
├── dice_ratchet/                 # RSI 训练链路：env / sandbox / inference / infra / ...
├── scripts/ci_gate.sh            # CI 门禁入口（本地与 CI 同一份）
├── .github/workflows/ci.yml      # CI：gate（快速）+ parity（golden / 差分对拍）
├── src/game/                     # G0 游戏本体：kernel / backend / frontend / content / tests
└── papers/                       # 参考论文清单与元数据（PDF 未入库）
```

## 复现与数据

- 每轮实验的**完整产物**（逐局轨迹、LLM 请求/响应审计、成本、图）落在本地 `runs/<run_id>/`，约 1.7 GB，**不入库**；README 与 `docs/reviews/` 里每个数字都能追到对应文件与行号。
- 复现的最小入口是 mock 后端（零成本、零网络）：`--model mock` 加上面的 `inference bench` / `loop smoke`。
- 真跑一轮（C1 / H1）的价格量级：≈**¥70/代**、≈**3–4 小时/代**；跑之前先读 [项目排期](docs/项目排期-DiceRatchet.md) 和最近一份 `docs/reviews/` 里的现场记录。

## 文档导航

1. [项目计划](项目计划-DiceRatchet.md)：目标与验收。
2. [模块拆分 Spec](docs/模块拆分Spec-DiceRatchet.md)：切成几块、怎么接起来（v2.35）。
3. [GAME PRD](docs/prd/)：游戏规则的唯一真相。
4. [MOD PRD](docs/prd/)：每个模块怎么实现。
5. [项目排期](docs/项目排期-DiceRatchet.md)：什么时候完成。
6. [评审与实验记录](docs/reviews/)：改了什么、为什么改、数字从哪来。

最值得先读的三份复盘：

- [M3 正式实验主体结果](docs/reviews/2026-10-14-M3正式实验主体结果.md)（噪声、F58–F71）
- [M3R2 正式实验主体结果](docs/reviews/2026-10-17-M3R2-正式实验主体结果.md)（首次 accept 与它的口径问题）
- [MVP1 打通与版本化缺陷](docs/reviews/2026-10-18-MVP1打通与版本化缺陷.md)（harness 血缘）

文档与代码冲突时，以 Spec 为准。新增的临时决策登记到 Spec §11.2。

## 许可

[MIT](LICENSE)。游戏内容、文档与实验数据同样按 MIT 发布；第三方参考仓库没有纳入本仓库。
