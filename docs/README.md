# DiceRatchet 文档中心

> 本目录承载 DiceRatchet 项目的**规格层文档**：从项目计划 → 模块拆分 Spec → 各模块技术 PRD。

## 阅读顺序

| 顺序 | 文档 | 作用 |
|---|---|---|
| 0 | [RSI调研纪要.md](../RSI调研纪要.md) | 为什么做（理论/实证/失败模式/治理） |
| 1 | [项目计划-DiceRatchet.md](../项目计划-DiceRatchet.md) | 做什么（目标、沙箱纪律、里程碑、验收） |
| 2 | [模块拆分Spec-DiceRatchet.md](模块拆分Spec-DiceRatchet.md) | **切成几块、怎么接起来**——唯一事实来源 |
| 2.5 | `prd/GAME-*.md` | **游戏本体怎么做**（先做出可玩的网页游戏，再据此写模拟器） |
| 3 | `prd/MOD-*.md` | 每块怎么实现（本文档集） |
| 4 | [项目排期-DiceRatchet.md](项目排期-DiceRatchet.md) | 什么时候做完（甘特图、关键路径、风险）；模块依赖图见 [assets/module-deps.svg](assets/module-deps.svg) |

> **游戏本体与模拟器的关系**：先用 [GAME-00](prd/GAME-00-webgame-PRD.md) 做出**人类可玩的网页版游戏**；
> 游戏跑通后，按同一套规则实现**纯 Python 模拟器**（MOD-01）供 RSI 训练；未来可接视觉模型直接对游戏本体训练。
> GAME 系列是**游戏规则**的唯一事实来源，MOD-01 是该规则的 Python 等价移植（对拍契约见 [GAME-01](prd/GAME-01-kernel-contract.md) §7）。

**冲突处理**：PRD 与 Spec 冲突时以 Spec 为准；Spec 未覆盖处由 PRD 决策并以「决策（本 PRD 提出，待回填 Spec）」标注，汇总登记在 [模块拆分Spec-DiceRatchet.md](模块拆分Spec-DiceRatchet.md) **§11 待回填决策登记表**（v1.4：36 项已回填、0 项待裁决）。

> 🔎 **评审入口**：Spec §11。v1.3（2026-09-30 17:21）已按 [MOD PRD 评审](reviews/2026-09-30-MOD-PRD评审.md) 修订；v1.4（2026-09-30 17:49）已确认全部裁决，§11.2 待裁决清零。新的临时决策从 P-31 起登记。跨模块签名以 Spec §4.7 为唯一登记处，「一局」的定义与成本模型见 Spec §8.6。
>
> **当前版本**：Spec **v2.27**（2026-10-14，**全库文档一致性修订（纯文档，¥0）+ 上一批的判据可达性修复与「取消 C2」落地**：C2 由「至少一指标显著提升」改判「**play 面确实变了**」（统计量降级为 `paired["by_metric"]` 报告项）、`eval report --split train` **只读化**（F61/F71）、冒烟胜率轴删除（F67）、判据 1 基线**选择偏差标注**（F70）、两处环境性门禁噪声按根因修（F68）；护栏 24 条修复前失败，全量门禁绿。见 [Spec §11.1m B-83](模块拆分Spec-DiceRatchet.md)）
> 下面这条是 **v2.6 的旧横幅**（保留原文，不改写历史）
> **当前版本**：Spec **v2.6**（2026-10-11；**v2.6 = 处置 F46-A / F42-A+B+C / F45-2**：① **F45-2（协议级）** heldout/test 移出四位数字空间（H1 731000–731039 / H2 731040–731079 / H3 731080–731099、test 947000–947099；`seeds_hash` 重算为 `sha256:686b2618330d…`）——旧四位空间与耗时（`duration_us=2072.8`）、token 预算（`max_tokens: 2048`）、ISO 年份（`2026`）撞车，迫使审计维护两张人工死名单（实测 118,646 命中 / 12 个键）；六位端口空间让「数字串相等即泄漏」重新健全，死名单**整删**（全量归档 1311 文件 / 188 MB：新区间 **0** 命中、旧区间 **799,868** 命中）。因 `config_hash` **不含** seeds，另立**代际隔离闸**（`baseline_protocol()` 追加 `+seeds-<hash12>`），否则新旧局会被 `--resume` 静默混算；已归档的 22 个含 heldout/test 局的 run 属 protocol v2 证据、与新协议**不可比**（不删数据、不擅自重跑）。`INV-5` 措辞**不需要改**。② **F46-A** `eval report` 的 `generation` 变**真过滤** + `--arm` 臂显式化：实测候选臂 `n=37 / 0.6757`、`--arm parent` `n=40 / 0.6250`，与 MOD-07 报告逐项一致（修复前是混代混臂的 `n=117 / 0.6496`），归档 `report.json` sha 未变（走 sidecar）。③ **F42-A+B+C** 每代评估的检查点与复用：臂归档 `metrics/arms/` + fail-closed 复用（整臂命中即 0 局）+ 提案检查点（评估前落盘，resume 命中即同 candidate id、**0 次 proposer 调用**）；顺带把 `n_workers`/`n_rep` 按配置转发（原先是 84 局串行）。登记 B-59..B-62，门禁 `scripts/ci_gate.sh` 全绿（1925 passed），本轮**成本 0**。**v2.5 = 处置缺陷 F47（用户裁决 A+C+D）**：「演化前门禁」原先只长在 `commit_harness()` 内部（accept 第 3 步）⇒ 脏工作区要等整代跑完才拒（实测一代 3.75 h / ≈¥47），且失败口径自相矛盾——`_finish_report` 写 `reject`、而 `build_decision_sections` 只看 gate 状态（此时仍 `approved`）⇒ **报告写 `decision="accept"` 却没有新版本**（幻影 accept；实测 `outcome.decision='reject'` / `report.decision='accept'` / `versions=['v0']`）。改为：MOD-06 在 `loop run` 起跑处（早于 gen-000 基线）调用 `preflight_worktree_clean()`（只拦会落版本的 run、非 git 路径降级）；`commit_harness` 先门禁后 `git add` 且失败时 `git reset -- harness`；`accept()`/`rollback()` 失败时删掉**本次新建**的快照（否则顶掉版本号并留下可被 `load_version`/`rollback` 取用的幻影版本）；基础设施失败记 `decision="error_reject"` 并纳入 `VERDICT_DECISIONS`；**§3.2 判据由负向枚举改为「判决口径 + 裁判拒绝正向白名单」**（原清单漏 `E_EVOLVE_GIT_FAILED`/`E_EVOLVE_VERSION_CONFLICT`，且退出码为 0）。登记 B-56..B-58，护栏 9 条（均先证明修复前失败），门禁 `scripts/ci_gate.sh` 全绿。**v2.4 = §3.2 实跑合格 + F42–F46**（闭环首次真正跑通：gen-1 ≈3.75 h / ≈¥47.4、基线复用未重付；候选 `win_rate 0.6757` vs parent `0.6250`、McNemar `p=0.7905` ⇒ 合法拒绝，实证 D-3；F43 台账/重算互相覆盖、F44/F45 `audit leak` 把多行 JSON 当 JSONL、F42 无检查点、F46 混代聚合）。**v2.3 = F32–F41 十个死缺陷 + §3.2 判据改写**（闭环路径上被 1700+ 条全绿用例掩护的缺陷：`episode_key` 漏 `-smoke`、提案无文件正文、hunk 不容忍 offset、提案预算被决策点预算卡死…；另加常驻未定义名扫描）。**v2.2 = 处置缺陷 F30****v2.2 = 处置缺陷 F30**：MOD-06 的 `EpisodeRunner` 由「与 MOD-08 共享单个 `SandboxEnv`」改为**每局一个 env**（`loop/cli.py` 注入 `env_factory`，局末 `close()`）——`evaluate_arm(n_workers=2)` 曾用双线程驱动同一个有状态 env，导致 `reset`/`step` 交错、观测串局、模型按别的局的相位出动作（实测 `illegal_rate` ≈0.285 / `fallback_rate` ≈0.29，而同臂同 seed 的 `play` 路径为 0.032 / 0.000；`heuristic` 臂 H1 全 40 局复现：共享 env 13/40 局非法、0/40 胜 → 每局新 env 0/40、18/40=45.0%）；登记 B-46 与残留 F31，护栏 `tests/integration/test_loop_env_isolation.py`，门禁 1870 passed / 2 skipped。**v2.1 = 处置缺陷 F28**：C5 的 `truncation` 由绝对门 0.15 改为**相对门** `candidate ≤ parent + truncation_tol(0.05)`、`illegal`/`fallback` 保留绝对门（0.35/0.25），并复核 bring-up 用 `medium × 6`；登记 B-45，已用真实 `judge()` 复验 `hard×6` 不再全拒。**v2.0 = 实验臂/角色解耦 + DeepSeek API 后端回填**：三角色（`policy`/`reflection`/`proposer`）绑定全部来自配置、六臂 `A0/A1/B0/B1/C0/C1`、§8.6 新增按臂分列的成本表，并**落定 M2 闭环主力臂 = `C1`（三角色全 `deepseek-flash`）**——API 臂单局 2.09–2.52 min ⇒ `E_gen=114` ≈ 4.0–4.8 h/代≈¥84–100（本地 8B 是 37 h/代），登记 B-42/B-43/B-44 与 F28/F29；v1.9 = D-2 的 medium × 6 基线实测回填 §8.6 并登记 F26/F27；v1.8 = §8.6 回填 M2 §3.1 的 full `PolicyAgent` 实测并按裁决 A+C 标定 C5：**先松开到 0.35/0.25/0.15**、M3 由相对门 `candidate ≤ parent + tol` 收紧，登记 B-40/B-41；v1.6 为 n=10 成本模型回填、v1.7 为 M2 开工 D-1/D-2/D-3）；M1 已验收通过（**含口径限定**：判据 2 的工件已用当前快照重跑刷新，MOD-04 §12 的 `illegal_rate`/`fallback_rate` 归 M3 判定），见 [M1 验收报告](reviews/2026-10-04-M1验收报告.md) 与独立的 [M1 验收复核](reviews/2026-10-05-M1验收复核.md)。

## 模块地图

```
L3 测量与工程  MOD-08 evals        MOD-09 infra
L2 进化        MOD-06 loop         MOD-07 evolve
L1 智能体      MOD-03 inference    MOD-04 policy    MOD-05 memory
L0 环境        MOD-01 env-rules    MOD-02 env-sandbox
（预留）       MOD-10 distill（M4 可选：STaR + LoRA）
```

## 技术 PRD 索引

### 游戏本体（GAME 系列，先行）

| ID | 模块 | 层 | 里程碑 | PRD |
|---|---|---|---|---|
| GAME-00 | game-web（网页版骰子 roguelike 本体，已实现于 `src/game`） | 游戏 | G0 | [PRD](prd/GAME-00-webgame-PRD.md) v1.2 |
| GAME-01 | 规则内核契约（确定性 / API / 对拍） | 游戏 | G0 | [契约](prd/GAME-01-kernel-contract.md) v1.2 |
| GAME-02 | 配表规格与内容数据 | 游戏 | G0 | [配表](prd/GAME-02-content-tables.md) |

### RSI 训练链路（MOD 系列）

| ID | 模块 | 层 | 里程碑 | PRD（当前版本） |
|---|---|---|---|---|
| MOD-01 | env-rules（GAME 内核的 Python 逐位移植） | L0 | M0 | [PRD](prd/MOD-01-env-rules-PRD.md) v1.0 |
| MOD-02 | env-sandbox（投影/隔离/审计/蜜罐） | L0 | M0 | [PRD](prd/MOD-02-env-sandbox-PRD.md) v1.2.4（审计的触发方式 = **显式 CLI**；`sandbox.audit.*` 收敛为**规则集版本钉扎** `ruleset_pin`，不符即 fail-closed `E_ENV_AUDIT_RULESET`/退出码 4——2026-10-14 F73/F74；**`env.sandbox.*` 15 键砍到 2 键**，删掉的 13 个里四个是「不变式开关」，配置面只留真的会被读的键——同日 F75） |
| MOD-03 | inference（模型推理：本地 MLX/llama.cpp + DeepSeek API，三角色装配） | L1 | M1 | [PRD](prd/MOD-03-inference-PRD.md) v1.7 |
| MOD-04 | policy（prompt/解析/工具契约） | L1 | M1 | [PRD](prd/MOD-04-policy-PRD.md) v0.6 |
| MOD-05 | memory（策略记忆与反思库） | L1 | M2 | [PRD](prd/MOD-05-memory-PRD.md) v1.2 |
| MOD-06 | loop（闭环编排） | L2 | M2 | [PRD](prd/MOD-06-loop-PRD.md) v1.6 |
| MOD-07 | evolve（harness 自进化） | L2 | M2–M3 | [PRD](prd/MOD-07-evolve-PRD.md) v0.12 ✅ 代码落地（[README](../dice_ratchet/evolve/README.md)）；**C2 = 「候选 `play_hash` ≠ 起点 `play_hash`」**（2026-10-14 F62 取消统计显著性门，统计量降级为 `paired["by_metric"]` 报告项）；冒烟门只留 error/illegal/fallback 三条（F67）；C5 = **三轴相对门 + 绝对 backstop**（`tol=0.05`，backstop 0.35/0.25/0.60；F28→B-44→B-64，原标定依据见 [2026-10-07 §3.1 实测](reviews/2026-10-07-M2-S31-full-agent基线实测.md)） |
| MOD-08 | evals（评估与测量） | L3 | M1–M3 | [PRD](prd/MOD-08-evals-PRD.md) v0.15 |
| MOD-09 | infra（配置/日志/CLI/守卫） | L3 | M0–M3 | [PRD](prd/MOD-09-infra-PRD.md) v0.7 |

## 关键路径（开工顺序）

```
MOD-09 骨架 ─┐
MOD-01 规则 ─→ MOD-02 沙箱 ─→ MOD-03/04 推理+策略 ─→ MOD-06 编排 ─→ MOD-07 进化 ─→ M3 验收
                 MOD-08 评估（可与 MOD-04/05 并行）
```

## 七条不变量（任何人任何 PR 都不得违反）

INV-1 agent 不得触达 Env 内部 · INV-2 观测必须白名单投影 · INV-3 prompt 只含四类白名单上下文 ·
INV-4 harness 代码受限执行 · INV-5 train/heldout/test 三方隔离 · INV-6 随机性只来自显式 seed ·
INV-7 主指标只用环境原生结果（outcome 为第一主指标，stages_cleared/hearts_left/battles_won 为次指标；shaped reward 不得用于结论）。

详见 [模块拆分Spec-DiceRatchet.md](模块拆分Spec-DiceRatchet.md) §1。
