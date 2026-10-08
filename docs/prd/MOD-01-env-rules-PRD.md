# MOD-01 env-rules 技术 PRD

> 版本：v1.0（2026-09-30 17:21 按 Spec v1.3 整体重写，见 §17）· 状态：待评审 · 所属里程碑：M0
> 上游：[模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md) v1.3（冲突以 Spec 为准）· 规则事实来源：[GAME-00](GAME-00-webgame-PRD.md) / [GAME-01](GAME-01-kernel-contract.md) / [GAME-02](GAME-02-content-tables.md) v1.2 与 `src/game/kernel`
> 定位：**GAME 内核的纯 Python 等价移植**。本模块不定义任何游戏规则；规则、数值、配表都引用 GAME。v0.1 中基于旧《Dicey Dungeons》模型的内容（floors/HP/block/levelup/`configs/enemies.yaml`/shaped reward 表）已全部废弃。

## 1. 背景与目标

DiceRatchet 的智能体侧（MOD-02～08）全部用 Python，而游戏本体已经用 JS 实现并验收（`src/game`，零依赖）。训练若经 HTTP 调用 JS 会引入进程边界和延迟，也会让确定性审计依赖网络栈；因此需要一份**逐位等价**的 Python 内核，供 `SandboxEnv` 在进程内调用。

**唯一目标**：给定同一份配表、同一个 config 和同一个动作序列，Python 内核每一步的 `obsDigest`、`rngDigest`、`stateDigest` 都与 JS 内核**逐字节相等**。

**成功度量**：
- ① `src/game/tests/golden/*.jsonl` 5 组共约 1,470 步逐行对拍 100% 通过。
- ② 额外 200 局随机动作的差分对拍（JS 与 Python 各跑一遍）0 分歧。
- ③ Python 版 `greedy_counter` 在默认局（hard × 6 关，n=200）上的通关率与 JS 相差 ≤ 3pp。
- ④ 性能达到 §5 的要求。

## 2. 范围

### 2.1 范围内
- 与 `src/game/kernel/*.js` **一一对应**的 Python 模块：`rng/digest/loader/state/slots/counter/risk/relic/combat/map/shop/forge/event/index`。
- 对外门面 `GameEngine`（Spec §4.7 第 1 行）。
- 配表加载：直接读取 `src/game/content/*.json`，校验规则同 GAME-01 §8。
- 错误码映射：`E_KERNEL_*/E_CONTENT_*` → `E_ENV_*`，`E_ACTION_*` 原样保留。
- 对拍工具：golden 重放 + JS/Python 差分对拍脚本。
- `heuristic` 所需的只读辅助：`legal_actions`、`play_hints`（供 MOD-04 移植 `greedy_counter`）。

### 2.2 范围外（非目标）

> 逐字复用 Spec §3 MOD-01 的「不负责」清单：

**不负责**：发明或修改规则（规则改动先改 GAME PRD 与 JS 内核，再同步移植）、观测裁剪（MOD-02）、prompt（MOD-04）、模型调用（MOD-03）、shaped reward 与评分统计（MOD-08）。

补充：
- 不写文件、不发日志（归 MOD-09）。
- 不实现 `SandboxEnv`、`reject()`、`policy_failure` 判负（归 MOD-02）。
- 不生成 golden：golden 只能由 JS `scripts/golden-gen.js` 生成，Python 侧只读。
- 不实现 Web 会话挂起逻辑（GAME `consecutiveIllegal` 字段照常移植以保持 `stateDigest` 一致，但 agent 路径不据此判定）。

## 3. 使用方与场景

| 使用方 | 调用 | 场景 |
|---|---|---|
| MOD-02 | `GameEngine.create/reduce/legal_actions/observe/digest` | 构造 `SandboxEnv`；agent 只能经 MOD-02 触达 |
| MOD-04 | `GameEngine.play_hints`（经 MOD-02 `SandboxEnv.hints()` 转发） | 启发式 fallback |
| MOD-08 | 经 MOD-02 调用 | 基线、评估 |
| 开发者 / CI | `python -m gamer_rsi env selftest`、`pytest tests/golden` | 对拍 |

## 4. 功能需求（FR-01-x）

- **FR-01-1 模块一一对应**：每个 JS 文件对应一个同名 Python 文件（`index.js` → `engine.py`），函数名改为 snake_case，**参数与返回的 dict 键保持 camelCase 不变**（与 GAME 状态、观测、动作逐字相同，所以 `stateDigest` 可以直接对拍）。
- **FR-01-2 随机**：逐位移植 `rng.js`。
  - `xmur3(streamName)` 与 rootSeed 混合 → SplitMix64，64 位运算用**16 位分片**（GAME-01 §7 P-5）。
  - `STREAM_NAMES` 与 JS 完全一致：`dice.enemy, dice.player, event, forge, levelup, loot, map, shop`。
  - `randint` 用拒绝采样；`shuffle` 为 Fisher–Yates。
  - **禁止** `random`、`hashlib` 参与派生（sha256 只用于 digest）。
- **FR-01-3 规范化与摘要**：`stable_stringify` 的行为与 JS `stableStringify` 完全相同：递归排序键、`ensure_ascii=True`、`separators=(",",":")`、禁止 float。`digest(v) = "sha256:" + sha256(stable_stringify(v))`。
- **FR-01-4 整数算术**：全面遵守 GAME-01 §7 P-1～P-14。
  - 禁止 `/` 和 `int()` 截断；`Math.floor(x/y)` 一律写成 `x // y`（除数恒为正）。
  - 位运算后 `& 0xFFFFFFFF`。
  - 千分倍率与 JS `applyMul` 同式。
- **FR-01-5 配置**：`apply_difficulty(base, difficulty, tables)` 与 JS 同名函数输出相同的 dict，包括 D-34 `stageCount ∈ allowed_stage_counts(tables)`；`validate_config` 返回相同的错误键列表。
- **FR-01-6 reduce**：`reduce(state, action, tables) -> {"state", "events", "illegal", "code", "reason", "error"}`。
  - 语义同 GAME-01 §4：不合法时返回原 state 与 `illegal=True`，**不抛异常**。
  - 内核异常时 `error={"code","message"}`，state 不变。
  - `end_turn` 在同一次 reduce 内按 §4.2 的 E0～E12 顺序跑完整个敌方回合。
- **FR-01-7 写时构造**：不原地修改入参 state（GAME-01 P-14）。先 `copy.deepcopy` 再修改；性能不足时改为结构共享，但对外语义不变。
- **FR-01-8 legal_actions**：与 JS `legalActions` 的**元素与顺序**完全一致（MOD-02 据此去掉 `abandon_run`）。
- **FR-01-9 observe**：实现 `view ∈ {"full","visual"}`，输出与 JS `observe` 相同，含 `stats`、`legalActionKinds`、`seed:null`。
- **FR-01-10 错误码映射**：

  | JS 码 | Python 码 |
  |---|---|
  | `E_KERNEL_BAD_CONFIG` | `E_ENV_CONFIG_INVALID` |
  | `E_CONTENT_*` | `E_ENV_CONTENT_INVALID`（`detail` 字段保留原码） |
  | `E_KERNEL_INVARIANT` / `E_KERNEL_EXCEPTION` / `E_KERNEL_DISPATCH` | `E_ENV_INTERNAL` |
  | `E_KERNEL_RNG_*` | `E_ENV_RNG` |
  | `E_KERNEL_TURN_LIMIT` | 不是错误；表现为 `truncationReason="turn_limit"` |
  | `E_ACTION_*` | 原样保留（与 GAME 一致，便于跨语言比对） |

  映射表在 MOD-09 `errors.py` 登记。
- **FR-01-11 原生结果**：`native_result(state) -> {"outcome", "truncationReason", "stagesCleared", "heartsLeft", "battlesWon", "turns", "kills"}`，供 MOD-02 填写 `StepResult.info`（INV-7）。
- **FR-01-12 版本**：`KERNEL_VERSION = "kernel-1.0.0"`、`OBS_SCHEMA = "obs-1.0.0"`，与 JS 同值。JS 升版时 Python 必须同步升版，CI 比对两侧常量。

## 5. 非功能需求（NFR-01-x）

- **NFR-01-1 确定性**：结果与 `PYTHONHASHSEED`、TZ、locale、平台无关；CI 在 `PYTHONHASHSEED ∈ {0,12345}` × `TZ ∈ {UTC, Asia/Tokyo}` 下各跑一次 golden。
- **NFR-01-2 性能**（Python 3.11，M 系列）：
  - `reduce` p95 ≤ 2 ms（JS 实测 0.163 ms，按约 10× 的解释器差距估算）。
  - 默认局整局 bot 自玩 ≤ 1.5 s。
  - `observe` p95 ≤ 1 ms。
  - 相对单个 LLM 决策（约 3 s）可以忽略不计。
- **NFR-01-3 零依赖**：只用标准库。
- **NFR-01-4 隔离**：不 import `gamer_rsi` 的其他子包（除 `infra.errors`），由 MOD-09 guards 强制。

## 6. 接口契约

```python
# gamer_rsi/env/engine.py
KERNEL_VERSION: Final = "kernel-1.0.0"
OBS_SCHEMA: Final = "obs-1.0.0"
ACTION_KINDS: Final[tuple[str, ...]]      # 同 JS ACTION_KINDS（17 个，含 abandon_run）
TOGGLE_KEYS: Final[tuple[str, ...]]

class GameEngine:
    def __init__(self, tables: Tables) -> None: ...
    def create(self, config_input: Mapping[str, Any]) -> GameState: ...          # = createGame
    def reduce(self, state: GameState, action: Mapping[str, Any]) -> ReduceResult: ...
    def legal_actions(self, state: GameState) -> list[dict]: ...
    def is_legal(self, state: GameState, action: Mapping[str, Any]) -> LegalCheck: ...
    def observe(self, state: GameState, view: Literal["full", "visual"] = "full") -> dict: ...
    def play_hints(self, state: GameState) -> dict: ...
    def native_result(self, state: GameState) -> NativeResult: ...
    def replay(self, config: Mapping[str, Any], actions: Sequence[Mapping]) -> ReplayResult: ...

def apply_difficulty(base: Mapping, difficulty: str, tables: Tables) -> dict: ...
def allowed_stage_counts(tables: Tables) -> list[int]: ...                    # [3, 6, 10]
def digest(value: Any) -> str: ...                                            # "sha256:<64hex>"
def state_hash(state: GameState) -> str: ...                                  # = digest(state)

# gamer_rsi/env/loader.py
def load_tables(content_dir: Path = GAME_CONTENT_DIR) -> Tables: ...
GAME_CONTENT_DIR: Final = Path("src/game/content")                            # 可由 GAMERSI_ENV__CONTENT_DIR 覆盖

GameState = dict[str, Any]         # 结构同 GAME-01 §2 GameState（camelCase 键）
Tables = Mapping[str, Any]         # 只读；load_tables 返回深度冻结视图
```

`ReduceResult`、`NativeResult` 为 `TypedDict`，键名同 JS。

## 7. 数据模型与存储

- **不自建数据模型**：`GameState`、`Observation`、`Action` 的结构全部由 GAME-01 §2/§5 与 GAME-00 §9.4 定义。Python 侧用 `TypedDict` 做类型提示（`gamer_rsi/env/types.py`），由 GAME-01 生成。
- **配表**：只读 `src/game/content/*.json`，不复制、不转换。`content_hash = digest(tables)` 记入 `config.json`（MOD-09）。
- **持久化**：本模块不写盘；续跑所需的状态快照是 `GameState` dict 本身（可 JSON 序列化），由 MOD-06 保存。

## 8. 关键流程

```text
# 对拍（CI 必跑）
for f in src/game/tests/golden/*.jsonl:
    meta, *steps = read(f)
    s = eng.create(meta.config)
    assert digest(eng.observe(s)) == steps[0].obsDigest and digest(s["rng"]) == steps[0].rngDigest
    for st in steps[1:]:
        r = eng.reduce(s, st.action); assert r.illegal == st.illegal; s = r.state
        assert (digest(eng.observe(s)), digest(s["rng"]), digest(s)) == (st.obsDigest, st.rngDigest, st.stateDigest)
    # 首个分歧：打印 step、JSON Pointer 路径、JS 值 / Python 值（用 --dump-state 从 JS 侧导出同一步的全状态）

# 差分对拍（nightly）
node src/game/scripts/golden-gen.js --random-walk --n 200 --out /tmp/diff   # JS 侧随机合法动作
pytest tests/golden/test_random_walk.py
```

移植顺序：`rng → digest → loader → state/slots/counter/risk → combat → map/shop/forge/event/relic → engine`。每一层都先用 GAME-01 §9 的测试向量单测通过，再进入下一层。

## 9. 配置项

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `env.content_dir` | str | `src/game/content` | 配表目录（与 JS 共用） |
| `env.golden_dir` | str | `src/game/tests/golden` | 对拍基准目录 |
| `env.check_invariants` | bool | `true` | 每次 reduce 后执行 `assertInvariants`（同 JS；性能测试时可关闭） |

游戏参数（难度、开关、局长）不在本模块配置，由 `EpisodeConfig`（Spec §4.1）传入。

## 10. 错误处理与降级

| 错误码 | 触发 | 处理 |
|---|---|---|
| `E_ENV_CONTENT_INVALID` | 配表校验失败 | 启动即失败，退出码 2 |
| `E_ENV_CONFIG_INVALID` | `apply_difficulty/validate_config` 失败 | 抛出，由调用方退出码 2 |
| `E_ENV_INTERNAL` | 不变式违反或分派异常 | `reduce` 返回 `error`，state 不变；MOD-02 将该局标为 `degraded` |
| `E_ENV_RNG` | 未注册流名或 randint 区间非法 | 同上 |
| `E_ENV_PARITY_MISMATCH` | 对拍不一致（仅测试/自检） | CI 失败，退出码 1 |
| `E_ACTION_*` | 非法动作 | 正常返回 `illegal=True`，不是错误 |

不降级：内核错误说明 Python 与 JS 的等价性被破坏，**禁止**用"跳过该步"之类的方式掩盖。

## 11. 测试计划

| # | 给定 | 当 | 则 |
|---|---|---|---|
| T-01 | GAME-01 §9 的 RNG 测试向量 | `derive_seed/next_u32/randint/shuffle` | 与 JS 输出逐值相等 |
| T-02 | 含中文、嵌套、整数的任意对象 | `stable_stringify/digest` | 与 JS 逐字节相等；含 float 时抛异常 |
| T-03 | `src/game/content` | `load_tables` | 校验通过；对篡改副本（悬空引用、坏 id、风险等级不符）分别报 `E_ENV_CONTENT_INVALID(detail=E_CONTENT_*)` |
| T-04 | 5 组 golden | 逐行重放 | `obsDigest/rngDigest/stateDigest/illegal` 全部相等 |
| T-05 | JS 随机游走 200 局 | 差分对拍 | 0 分歧 |
| T-06 | `stageCount=4` | `create` | `E_ENV_CONFIG_INVALID`；`stageCount=6` 时击败第 6 关 Boss 得到 `outcome="win"`、`stagesCleared=6` |
| T-07 | 任意 `player_turn` 状态 | reduce 一个非法动作 | 返回原 state 对象（`is`），`illegal=True`，`code` 同 JS |
| T-08 | 子进程 `PYTHONHASHSEED=0/12345`、`TZ=UTC/Asia/Tokyo` | 跑 T-04 | 四种组合全部通过 |
| T-09 | 默认局 n=200（seed 1–200） | Python `greedy_counter` | 通关率与 JS 逐局一致（实测两侧均 45.5%，差 0.0pp ≤ 3pp；复现命令见 Spec §8.6） |
| T-10 | 同一 state | 连续调用 `reduce` 两次 | 入参 state 的 `digest` 不变（写时构造） |

## 12. 验收标准（DoD，对齐 Spec §9）

| 门 | MOD-01 的 DoD |
|---|---|
| G1 | `gamer_rsi/env/` 行覆盖 ≥ 90%（JS 侧为 95.8%） |
| G2 | T-04 全部通过；golden 只读，禁止 Python 侧生成替代快照 |
| G3 | T-08 通过 |
| G4 | 本模块不产出 agent 可见内容；MOD-02 投影测试覆盖 `seed/rng` 键 |
| G5 | `types.py` 与 GAME-01 §2 字段一致性由脚本检查 |
| G6 | `python -m gamer_rsi env selftest --seed 1` 与 `pytest tests/golden -q` 可直接复制执行 |
| M0 | T-04/T-05/T-09 通过，并与 MOD-02 `SandboxEnv` 联调完成一局默认局 |

## 13. 指标与埋点

本模块只产出字段（`native_result`、`reduce.events`），不落盘。事件名由 MOD-02 以 `env.*` 前缀转发给 MOD-09。

## 14. 依赖与风险

| 依赖 | 说明 |
|---|---|
| `src/game/kernel` + `content` + `tests/golden` | 移植基准；JS 侧任何改动都必须同一 PR 同步 Python，并重新生成 golden |
| MOD-09 `errors.py` | 错误码登记 |

| 风险 | 对策 |
|---|---|
| JS 与 Python 语义差异（整除、键序、字符串比较） | GAME-01 §7 的 14 条逐条单测；差分对拍 nightly |
| 深拷贝导致性能不达标 | 先保证正确；若 `reduce` p95 > 2 ms，只对 `state.player`/`enemies` 做结构共享 |
| JS 改规则后 Python 漂移 | CI 检查 `KERNEL_VERSION` 一致，并比对 JS golden 的 `kernelVersion` |

## 15. 里程碑与工作量估算（人日）

JS 内核约 2,780 行，逐位移植并对拍：

| # | 任务 | 人日 |
|---|---|---|
| 1 | `rng` + `digest` + `loader`（含测试向量） | 1.0 |
| 2 | `state/slots/counter/risk/relic` | 1.0 |
| 3 | `combat`（end_turn E0–E12，最易分歧） | 2.0 |
| 4 | `map/shop/forge/event` + `engine` 门面 | 1.5 |
| 5 | golden 对拍、差分对拍脚本、性能调优 | 1.5 |
| | **合计** | **7.0** |

**实际交付（M0）**：2026-10-03 提交 `98ded4a`（`gamer_rsi/env/` 全量移植 + golden 对拍）。
本项目**未单独记工**，「实际人日」无法从仓库证据还原；按提交时间窗折算 ≤ 1 人日，与估算 7.0 人日的偏差 ≥ 85%（排期 §7 的 20% 阈值被触发，已在 [排期 §8](../项目排期-GameRSI.md) 记录）。
T-01～T-10 全部通过（T-08 于 2026-10-03 由验收补齐为 pytest 用例），证据见 [M0 验收报告](../reviews/2026-10-03-M0验收报告.md)。M1 起按排期 §7 逐任务记录实际人日。

## 16. 开放问题

1. GAME-01 P-5 要求 Python 也用 16 位分片做 64 位乘法。Python 原生大整数加掩码的结果与之相同且更快，是否放宽为"算法任选，测试向量必须通过"？（建议放宽，需修订 GAME-01。）
2. 差分对拍需要 JS 侧新增 `--random-walk` 模式（`scripts/golden-gen.js`），以及 `--dump-state` 调试导出。归 GAME 侧，工作量约 0.3 人日。
3. 如果将来需要多进程并行跑环境，`Tables` 的冻结视图是否要支持 pickle？

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v0.1 | 2026-09-30 | 首版（基于旧《Dicey Dungeons》规则模型） |
| v1.0 | 2026-09-30 17:21 | 按 Spec v1.3（B-09）与 MOD PRD 评审整体重写：定位改为 GAME 内核的逐位移植；废弃 v0.1 的 D-01～D-05（规则相关）；随机派生改为 xmur3 + SplitMix64（原 blake2b 违反 GAME-01 §3.2）；对拍基准改为 `src/game/tests/golden`（新增 `stateDigest` 与 g5 默认局） |
