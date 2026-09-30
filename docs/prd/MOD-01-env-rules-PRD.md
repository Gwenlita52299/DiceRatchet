# MOD-01 env-rules 技术 PRD

> 版本：v0.1 · 状态：草案（待 M0 评审）· 所属里程碑：M0 · 上游 Spec：[模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md) · 作者：MOD-01 owner
> 定位：实现 Spec §3 MOD-01 的规则状态机；与 Spec 冲突以 Spec 为准。Spec 未覆盖处均以「决策（本 PRD 提出，待回填 Spec）」标注，汇总见 §16。
>
> ⚠️ **重要变更（2026-09-30）**：游戏规则的事实来源已转移到 **GAME 系列** —— 项目先做**可玩的网页版游戏本体**
> （[GAME-00-webgame-PRD.md](GAME-00-webgame-PRD.md)），本模块随后按同一套规则实现**纯 Python 等价移植**供 RSI 训练。
> 因此本 PRD 中的规则数值（难度、奖励、槽位、怪物）**一律以 GAME-00/GAME-02 为准**；本文件仍有效的部分是其
> **工程契约**（接口签名、错误码、确定性、golden、覆盖率门槛）。逐字段对拍要求见
> [GAME-01 内核契约 §7 Python 移植清单](GAME-01-kernel-contract.md#7-python-移植清单mod-01-对拍必读)。
> 受影响的既有决策：D-04（难度配置化，已被 GAME-00 §7.1 扩展）、§8（`outcome="lose"` 粒度上移，见 GAME-00 §17 D-05）。

## 1. 背景与目标

MOD-01 是本项目**唯一的形式验证器**：M0 验收「真人策略可复现通关」、held-out 主指标、INV-7 的「环境原生结果」全部由它提供。它必须是纯 Python、无引擎依赖、无 IO/网络/全局随机的确定性状态机，使训练局与 held-out 局在任何机器上逐字节复现。目标：① 实现《Dicey Dungeons》简化战斗规则（掷骰 → 单骰入单槽（奇偶/大小/多骰限制）→ 结算：格挡 → 减 HP → 死亡判定 → 敌人 intent 公开与执行 → 击杀/升级/进层 → 胜负）；② 提供 `reset/step`、`legal_actions`、奖励计算、非法判定、`full_state_hash()`，由 MOD-02 包装为 `SandboxEnv`；③ 通过 G1/G2/G3 并产出 M0 自检命令。

## 2. 范围

### 2.1 范围内

- 战斗状态机与全部规则判定：骰面生成、槽位约束、效果与结算顺序、状态（strength/poison）、回合移交。
- 敌人 AI 与 intent（提交未来骰面 + 玩家可见意图）；关卡推进最小实现（battle/shop/levelup/boss 节点序列、商店购买、升级选项）。
- shaped reward 计算、非法动作判定与失败计数、`full_state_hash()` 与规范化序列化；阶段一范围：Warrior 单角色 + 单敌人/单 Boss 战 + 前 N 层；代码 `gamer_rsi/env/{engine,models,rng,actions}.py`、配置 `configs/enemies.yaml`（新增）、测试 `tests/unit/`、`tests/golden/mod01/`。

### 2.2 范围外（非目标）

- 观测裁剪（MOD-02）
- prompt（MOD-04）
- 模型调用（MOD-03）
- 评分与统计（MOD-08）

以下为 Spec §3 未覆盖、由本 PRD 明确排除（决策（本 PRD 提出，待回填 Spec））：

- 不写任何文件、不发结构化日志：JSONL 轨迹、`run_id`、事件名一律归 MOD-09；MOD-01 只返回 `info` dict；不定义 `Observation`/`OBSERVATION_SCHEMA`（MOD-02 拥有），只保证 `FullState` 含所需的全部原始字段。
- 不实现 `SandboxEnv` 门面、`close()`、进程/代码隔离、蜜罐（MOD-02）；不做随机/启发式基线 agent（MOD-08）、不做 CLI 子命令实现（MOD-09 `cli.py` 调用本模块）；不做多角色、遗物、诅咒、装备替换/改骰类扩展（阶段一后评估）。

## 3. 使用方与场景

| 使用方 | 使用方式 | 场景 |
|---|---|---|
| MOD-02 env-sandbox | `GameEngine.reset/step/legal_actions` + `FullState` 字段 | 构造 `SandboxEnv`、白名单投影；agent 只经此入口 |
| MOD-06 loop | `GameEngine` 同步调用（无异步、无 IO） | 单局回合循环、断点续跑（`to/from_canonical_dict`） |
| MOD-08 evals | `GameEngine` 直连（不加载模型） | random/heuristic 基线、消融、held-out 评估 |
| MOD-09 infra / 人类开发者 | `python -m gamer_rsi env selftest --seed 1`、`pytest tests/golden/mod01` | M0 规则与确定性自检、golden 回归、审计回放 |

MOD-01 **不感知 agent 存在**：不 import `gamer_rsi.{sandbox,agent,evals,loop,evolve,inference,memory}`，也不接收 prompt。

## 4. 功能需求（FR-01-x，编号，每条可测试）

| ID | 需求 | 可测试点 |
|---|---|---|
| FR-01-01 | **reset**：`GameEngine.reset(seed, episode_config)` 由 `seed` 派生全部 RNG 流，生成节点序列、玩家装备/骰、敌人实例、首回合骰面，返回 `FullState`。 | 同 `(seed, config)` 两次 reset 的 `full_state_hash` 相同；不同 seed 必不同 |
| FR-01-02 | **骰面生成**：玩家回合开始按 `dice_count × dice_sides` 掷骰，消费 `dice.player` 流；`face ∈ [1, sides]`；`difficulty` 只改玩法参数，不改随机流消费顺序。 | 固定 seed 下骰面序列与 golden 一致；`used=False` |
| FR-01-03 | **槽位约束 DSL**：`parse_constraint(text)`，文法 `any \| odd \| even \| (ge\|gt\|le\|lt\|eq):int \| in:int,int,...`；`check_constraint(text, pip)` 判定。 | 12 条表驱动用例；非法文本抛 `E_ENV_CONFIG_INVALID` |
| FR-01-04 | **place_die**：校验 `die_index` 存在且未用、`slot_id` 存在、槽未被占（非 reusable）、骰面满足约束；通过后写 `filled_by`、触发 `effect`、骰置 `used=True`。 | 4 类失败各返回对应 `E_ENV_*` 且 `state_hash` 不变 |
| FR-01-05 | **结算顺序**（硬约束）：① 格挡吸收 `absorbed=min(block,dmg)`、`block-=absorbed`；② 实伤 `dmg-absorbed` 扣 `hp`；③ `hp<=0` 死亡判定（敌人→击杀/掉落；玩家→`outcome="lose"`）。禁止先扣 HP 再算格挡。 | `block≥dmg` / `block<dmg` / 致死 三类边界断言 HP、block、outcome |
| FR-01-06 | **敌人 intent 与 AI**：`enemy_plan(state, enemy_id)` 在玩家回合开始前提交该敌下回合骰面（消费 `dice.enemy`）并按 `ai` 策略贪心规划；intent 真实且不可反悔（敌方回合不重掷）。 | 同 seed 下 `intent.projected_damage` 与实际自伤一致；重跑一致 |
| FR-01-07 | **end_turn / 回合移交**：未用骰在结束时丢弃；`player_turn → enemy_turn → player_turn`；敌方按已提交计划逐槽结算（顺序 = `slot_id` 字典序）。 | `turn+1`；越 phase 动作返回 `E_ENV_PHASE_MISMATCH` |
| FR-01-08 | **关卡推进**：`node_kinds` 长度 = `floors`，末层强制 boss；击杀后 `floor+=1`、`kills+=1`、结算掉落（gold/xp）；全层通过 → `outcome="win"`。 | 3 层 episode 的 `floor/kills` 序列与 golden 一致 |
| FR-01-09 | **商店与升级**：`shop` 节点按 `shop` 流生成 3 件商品（`item_id/price/effect`），`shop_buy` 扣金币加装备、`shop_leave` 结束节点；XP 达阈进 `levelup`，`levelup_choose` 从 `hp_up/dice_up/slot_up` 选 1。 | 金币不足 → `E_ENV_INSUFFICIENT_GOLD`；已购商品 → `E_ENV_UNKNOWN_ITEM`；选项集合同 seed 可复现 |
| FR-01-10 | **奖励计算**：`compute_reward(prev, next, illegal)` 按 §9 `env.reward.*` 整数表计算；内部以 1e-3 定点整数累计后除 1000 返回 `float`。 | 每奖励项单测；非法动作恰为 `-0.05`；**shaped 不得进入主指标（INV-7）** |
| FR-01-11 | **非法动作与失败策略**：`legal_actions(state)` 返回穷举合法集（顺序：骰 index 升序 × `slot_id` 字典序 × phase 追加动作）；非法 `step` 不改状态、`reward=-0.05`、写 `info["illegal_reason"]`、`consecutive_illegal+=1`，连续 3 次判负；`terminated` 只由胜负置真，`truncated` 由 `turn>max_turns` 或 `steps>max_steps` 置真并写 `truncation_reason="E_ENV_TURN_LIMIT"`。 | 非法步前后 `state_hash` 相等；第 3 次连续非法 `terminated=True`；截断局不计胜负且两标志互斥 |
| FR-01-12 | **full_state_hash()**：对 §7 规范化字典做 sha256，返回 `"sha256:<64hex>"`；覆盖 RNG 流状态与未来骰面。 | 跨进程（`PYTHONHASHSEED` 变化）、跨平台一致；round-trip 稳定 |
| FR-01-13 | **use_ability**：阶段一仅 Warrior 的 `warrior_reroll`（重掷未用骰，消费 `dice.player.reroll` 流）与 `warrior_block_up`（+3 block），每局各 1 次。 | 次数用尽 → `E_ENV_ABILITY_EXHAUSTED`；重掷后骰面可复现 |

## 5. 非功能需求（NFR-01-x：确定性/性能/可复现/可观测）

| ID | 需求 |
|---|---|
| NFR-01-01 | **INV-6 确定性**：随机只经 `rng.py` 命名流；`gamer_rsi/env/` 内禁 `random`/`time`/`os.urandom`/`numpy`/`torch`，`hash()` 不得参与决策；MOD-09 `guards.py` AST 扫描 0 命中。 |
| NFR-01-02 | **纯函数内核**：变换写成 `FullState -> FullState`（`frozen=True` + `tuple` 容器），除 `GameEngine.step` 外无可变态；不得依赖 dict 插入序做规则判定（哈希前一律 sort）。 |
| NFR-01-03 | **性能**：`step` p95 ≤ 1 ms、`reset` ≤ 5 ms、单局（≤200 回合）≤ 250 ms（M5 24GB 单线程）；`enemy_plan` 搜索规模 ≤ `dice_count × len(slots)`（≤24），禁全排列。 |
| NFR-01-04 | **可复现/可观测**：golden 快照须在无网络、任意 `PYTHONHASHSEED`、Python 3.11/3.12 小版本差异下逐字节一致；每次 `step` 的 `info` 至少含 Spec §4.1 的 6 个键，不写文件、不自建日志格式（MOD-09 独占）；`import gamer_rsi.env.engine` 不触发 IO 且 ≤ 50 ms，跨层 import 由 CI `git grep` 门禁拦截。 |

## 6. 接口契约（真实 Python 签名 + JSON Schema）

```python
# gamer_rsi/env/actions.py —— 动作与 kind 枚举 SSoT（MOD-04 生产，MOD-01 消费）
ActionKind = Literal["place_die","end_turn","use_ability","shop_buy","shop_leave","levelup_choose"]
@dataclass(frozen=True)
class Action:
    kind: ActionKind
    die_index: int|None = None; slot_id: str|None = None; ability_id: str|None = None
    item_id: str|None = None; option_id: str|None = None
# gamer_rsi/env/rng.py —— 唯一随机入口
STREAM_NAMES = ("map","dice.player","dice.player.reroll","dice.enemy","shop","levelup","loot")
def derive_seed(root_seed: int, stream_name: str) -> int: ...   # blake2b(f"{root}|{name}")
class RngStream:
    def __init__(self, seed: int, name: str) -> None: ...
    def randint(self, lo: int, hi: int) -> int: ...             # 闭区间
    def choice(self, seq: Sequence[Any]) -> Any: ...
    def clone_with_offset(self, offset: int) -> "RngStream": ...
    def state_hash(self) -> str: ...
# gamer_rsi/env/models.py
@dataclass(frozen=True)
class Effect:                       # value = pip + amount（base="pip"）或 amount（base="const"）
    kind: Literal["attack","block","heal","status","modify_die"]
    base: Literal["pip","const"] = "pip"; amount: int = 0; status_kind: Literal["strength","poison"]|None = None
@dataclass(frozen=True)
class SlotSpec:                     # slot_id 正则 ^[a-z_]+[0-9]+$；constraint = DSL 文本（FR-01-03）
    slot_id: str; kind: Literal["weapon","armor","utility"]; constraint: str; effect: Effect; reusable: bool = False
@dataclass(frozen=True)
class EnemySpec:
    enemy_id: str; name: str; max_hp: int; dice_count: int; dice_sides: int
    slots: tuple[SlotSpec, ...]; ai: Literal["greedy_damage","greedy_block","scripted"]
@dataclass(frozen=True)
class EpisodeConfig:
    episode_id: str; difficulty: Literal["easy","normal","hard"] = "normal"; floors: int = 3
    enemies: tuple[str, ...] = ("slime","goblin","boss_dragon")   # 长度须 == floors
    max_turns: int = 200; max_steps: int = 1000; starting_dice: int = 2; dice_sides: int = 6
    shop_enabled: bool = True; levelup_enabled: bool = True
    shaped_reward_version: str = "srv-1"; variant: str = "stage1-warrior"
# gamer_rsi/env/engine.py —— MOD-02 以此包装 SandboxEnv；以下模块级纯函数同文件（无 IO，可单测）
class GameEngine:
    def reset(self, seed: int, episode_config: EpisodeConfig) -> FullState: ...
    def step(self, state: FullState, action: Action) -> "Transition": ...
    def legal_actions(self, state: FullState) -> list[Action]: ...
    def full_state_hash(self, state: FullState) -> str: ...
@dataclass(frozen=True)
class Transition:
    state: FullState; reward: float; terminated: bool; truncated: bool
    info: dict[str, Any]   # illegal, illegal_reason, error_code, outcome, floors, kills, state_hash
def roll_dice(state, *, reason: str) -> FullState: ...
def place_die(state, die_index: int, slot_id: str) -> FullState: ...
def resolve_effect(state, source: str, target: str, effect: Effect, pip: int) -> FullState: ...
def parse_constraint(text) -> SlotConstraint: ...      def check_constraint(text, pip) -> bool: ...
def enemy_plan(state, enemy_id) -> tuple[Intent, FullState]: ...   def compute_reward(prev, nxt, illegal) -> float: ...
def to_canonical_dict(state) -> dict[str, Any]: ...    def full_state_hash(state) -> str: ...
```

```jsonc
// Action JSON Schema（MOD-04 输出校验用；enum 为 SSoT，变更需 Spec 升版）
{"type":"object","additionalProperties":false,"required":["kind"],
 "properties":{"kind":{"enum":["place_die","end_turn","use_ability","shop_buy","shop_leave","levelup_choose"]},
  "die_index":{"type":"integer","minimum":0,"maximum":5},"slot_id":{"type":"string","pattern":"^[a-z_]+[0-9]+$"},
  "ability_id":{"enum":["warrior_reroll","warrior_block_up"]},"item_id":{"type":"string"},"option_id":{"enum":["hp_up","dice_up","slot_up"]}},
 "allOf":[{"if":{"properties":{"kind":{"const":"place_die"}}},"then":{"required":["die_index","slot_id"]}},{"if":{"properties":{"kind":{"const":"use_ability"}}},"then":{"required":["ability_id"]}},
  {"if":{"properties":{"kind":{"const":"shop_buy"}}},"then":{"required":["item_id"]}},{"if":{"properties":{"kind":{"const":"levelup_choose"}}},"then":{"required":["option_id"]}}]}
// EpisodeConfig JSON Schema：episode_id ^e-[0-9]{6,}$；difficulty enum(easy|normal|hard)；floors int 1..10；enemies array(1..10)；
// max_turns/max_steps int ≥10；starting_dice 1..6；dice_sides 2..12；shop/levelup bool；shaped_reward_version const "srv-1"；
// variant const "stage1-warrior"；additionalProperties:false
```

## 7. 数据模型与存储

```python
@dataclass(frozen=True)
class FullState:                   # 唯一真相；MOD-02 投影后 agent 永远看不到它
    schema_version: str            # "fs-1"；episode_id: str；seed: int（严禁进入 Observation）
    config: EpisodeConfig
    turn: int; steps: int; floor: int             # 均从 1 起
    phase: Phase                   # player_turn|enemy_turn|shop|levelup|game_over
    node_kinds: tuple[str, ...]                   # 长度 == floors，reset 时生成
    self_hp: int; self_max_hp: int; self_block: int; self_status: tuple[Status, ...]
    self_level: int; self_xp: int; self_xp_next: int; self_gold: int
    self_dice: tuple[Die, ...]                    # Die(index, sides, face, used)
    self_slots: tuple[SlotSpec, ...]              # 装备，跨层保留
    enemies: tuple[EnemyState, ...]               # (spec, hp, block, status, intent, dice, slots)
    shop: ShopState|None            # items=(ShopItem(item_id, price, effect),...), bought=tuple[str,...]
    levelup: LevelupState|None      # options=("hp_up","dice_up","slot_up"), chosen: str|None
    rng: tuple[RngState, ...]       # (name, counter, state)，按 name 排序
    consecutive_illegal: int; outcome: Literal["win","lose"]|None; truncation_reason: str|None
```

- `Intent(enemy_id, next_rolls: tuple[int, ...], planned: tuple[tuple[int, str], ...], projected_damage: int, projected_block: int)`；`next_rolls` 属未来骰面，只存 `FullState`，MOD-02 的 `EnemyView.intent` 仅允许导出 `{kind, value, count}`。`Status(kind, stacks)` 阶段一仅 `strength`（攻击 `+stacks`）、`poison`（玩家回合开始扣 `stacks` HP，非致死至少留 1）。
- **存储与文件**：MOD-01 **零持久化**；序列化仅 `to_canonical_dict()`（哈希/审计）与 `FullState.from_canonical_dict()`（MOD-06 断点续跑），轨迹 JSONL 由 MOD-09 写入 `runs/<run_id>/episodes/<seed>.jsonl`。文件清单：`gamer_rsi/env/{engine,models,rng,actions}.py`；`configs/enemies.yaml`、`configs/default.yaml`（`env:` 段，见 §9）；`tests/unit/test_env_*.py`；`tests/golden/mod01/{seed_0001,seed_1042,easy_seed_0007}.jsonl`。

## 8. 关键流程（伪代码或时序）

```
reset(seed, cfg):
  validate cfg(floors == len(enemies), 1 <= starting_dice <= 6) else E_ENV_CONFIG_INVALID
  st = FullState(seed, turn=1, steps=0, phase=player_turn, floor=1, ...)
  st.node_kinds = gen_nodes(cfg, STREAM["map"])          # 末层 "battle_boss"
  st.self_slots = warrior_loadout(); st.enemies = (spawn(cfg.enemies[0]),)
  st.self_dice = roll_dice(st, reason="turn_start")      # 消费 dice.player
  st.enemies[0].intent = enemy_plan(st, ...)             # 消费 dice.enemy，提交 next_rolls
  return st
step(state, action):
  if state.phase == game_over: return Transition(state, 0.0, True, state.truncation_reason is not None, info)
  if action not in legal_actions(state):                 # 语义级非法：不执行、状态不变
      if state.consecutive_illegal + 1 >= 3: return Transition(state, -0.05, True, False, info(outcome="lose"))
      return Transition(state, -0.05, False, False, info(illegal=True, error_code=code))
  next = dispatch(state, action)                         # place_die/end_turn/use_ability/shop_*/levelup_choose
  next, truncated = apply_limits(next)                   # max_turns / max_steps → E_ENV_TURN_LIMIT
  return Transition(next, compute_reward(state, next, False), next.outcome is not None, truncated, info(next))
place_die(st, i, slot_id):
  assert st.phase == player_turn                                  # else E_ENV_PHASE_MISMATCH
  die = st.self_dice[i]                                           # E_ENV_DIE_INDEX_OOR / E_ENV_DIE_USED
  slot = st.self_slots[slot_id]                                   # E_ENV_UNKNOWN_SLOT
  if slot.filled_by is not None and not slot.reusable: E_ENV_SLOT_OCCUPIED
  if not check_constraint(slot.constraint, die.face):  E_ENV_CONSTRAINT_VIOLATION
  return resolve_effect(st, src="self", tgt=slot_target(slot), effect=slot.effect, pip=die.face + strength_bonus(actor))
resolve_effect(st, src, tgt, effect, pip):              # 结算顺序 = 硬约束 FR-01-05
  v = pip + effect.amount if effect.base == "pip" else effect.amount
  case "attack": absorbed = min(tgt.block, v); tgt.block -= absorbed   # ① 格挡吸收
                 tgt.hp -= (v - absorbed)                             # ② 再减 HP
                 if tgt.hp <= 0: on_death(st, tgt)                    # ③ 死亡判定
  case "block": src.block += v            case "heal": src.hp = min(src.max_hp, src.hp + v)
  case "status": apply_status(tgt, effect.status_kind, effect.amount)
  case "modify_die": src.dice[free_index].face = clamp(face + effect.amount, 1, sides)
  return mark_slot_and_die(...)
end_turn(st):
  discard_unused_dice(st); st.phase = enemy_turn
  for e in alive(st.enemies):                           # 只执行已提交计划，不重掷
      for (i, slot_id) in sorted(e.intent.planned, key=lambda p: p[1]): execute(e, i, slot_id)
      if st.self_hp <= 0: st.outcome, st.phase = "lose", game_over; break
  if st.outcome is None:
      if all dead: advance_floor(st)                    # floor/kills/gold/xp → shop|levelup|battle|win
      st.turn += 1; st.phase = player_turn
      st.self_dice = roll_dice(st); st.enemies[*].intent = enemy_plan(...)  # 玩家回合可见
  return st
full_state_hash(st) = "sha256:" + sha256(canonical_json(to_canonical_dict(st))).hexdigest()
```

**`to_canonical_dict` 规范化定义（FR-01-12）**：`json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",",":"), allow_nan=False).encode("utf-8")`；所有数值必须 `int`（奖励以 1e-3 定点整数入库），**禁 float**；字符串 `unicodedata.normalize("NFC", s)`；排序用 UTF-8 码点序（不用 `locale`/`hash`）；`state_hash` 字段自身不参与哈希；`episode_id`、`seed`、`rng`、`enemies[*].intent.next_rolls` 全部参与。

## 9. 配置项（键名、类型、默认值、含义）

`configs/default.yaml` 新增 `env:` 段（优先级 CLI > `GAMERSI_*` > 本文件；由 MOD-09 `infra/config.py` 注入 `EpisodeConfig`）：

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `env.default_episode.floors` / `.difficulty` | int / enum | `3` / `normal` | 前 N 层（M0 验收用 1，M2 用 3）；难度变体 |
| `env.default_episode.enemies` | list[str] | `["slime","goblin","boss_dragon"]` | 敌人配表，长度须 = `floors`，末位为 Boss |
| `env.default_episode.max_turns` / `.max_steps` / `.starting_dice` / `.dice_sides` | int | `200` / `1000` / `2` / `6` | 截断阈值 → `truncated`；Warrior 起始骰数/面数 |
| `env.default_episode.shop_enabled` / `.levelup_enabled` / `.shaped_reward_version` | bool / bool / str | `true` / `true` / `"srv-1"` | 是否生成对应节点；奖励表版本（随奖励改动递增） |
| `env.difficulty.easy.enemy_hp_mul` | float | `0.75` | 敌 HP ×0.75（取整）；M0 示范局用 easy |
| `env.difficulty.hard.enemy_hp_mul` / `.extra_enemy_die` | float / int | `1.5` / `1` | 硬难度：HP ×1.5 且敌骰 +1 |
| `env.reward.damage_dealt` / `.player_damage_taken` | int(1e-3) | `10` / `-20` | 有效伤害 +0.010/点；自伤 −0.020/点 |
| `env.reward.kill` / `.floor_advance` | int(1e-3) | `500` / `300` | 击杀 +0.500；进层 +0.300 |
| `env.reward.episode_win` / `.episode_lose` | int(1e-3) | `3000` / `-1000` | 通关 +3.000；失败 −1.000 |
| `env.reward.step_penalty` / `.idle_end_turn` | int(1e-3) | `-2` / `-10` | 每步 −0.002（抑制空转）；仍有可放骰却结束回合 −0.010 |
| `env.illegal.penalty` / `.consecutive_limit` / `env.rng.algorithm` / `.streams` | float / int / str / list[str] | `-0.05` / `3` / `"splitmix64"` / `STREAM_NAMES` | Spec §4.3 固定罚分，不得偏移；连续非法达 3 判负；自实现 PRNG（禁 `random`），未注册流抛 `E_ENV_RNG_STREAM_MISSING` |

`configs/enemies.yaml`（新增）：`slime{hp:8,1d6,slots:[bite(any,attack pip)]}`、`goblin{hp:12,2d6,slots:[stab(any,attack pip), guard(odd,block pip)]}`、`boss_dragon{hp:40,3d6,slots:[claw(ge:3,attack pip), tail(even,attack pip+1), scales(any,block pip)], ai:greedy_damage}`。`EpisodeConfig.enemies` 引用这些 `enemy_id`，未知名 → `E_ENV_UNKNOWN_ENEMY`。

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀 E_ENV_*）

| 错误码 | 触发条件 | 处理 |
|---|---|---|
| `E_ENV_CONFIG_INVALID` | `floors != len(enemies)`、骰数/面数越界、约束文本语法错、奖励版本未知 | `reset` 抛异常（fail fast，不静默降级） |
| `E_ENV_PHASE_MISMATCH` / `E_ENV_TURN_LIMIT` | 动作与 `phase` 不符 / 超 `max_turns`/`max_steps` | 前者=非法动作；后者非错误 → `truncated=True` + `truncation_reason`，该局不计胜负 |
| `E_ENV_DIE_INDEX_OOR` / `E_ENV_DIE_USED` / `E_ENV_UNKNOWN_SLOT` / `E_ENV_SLOT_OCCUPIED` | 骰越界、骰已用、槽不存在、槽已占且非 reusable | 非法动作：状态不变、`reward -= 0.05` |
| `E_ENV_CONSTRAINT_VIOLATION` | 骰面不满足奇偶/大小/多骰限制 | 同上 |
| `E_ENV_INSUFFICIENT_GOLD` / `E_ENV_UNKNOWN_ITEM` | 金币不足 / 商品已售或不存在 | 同上 |
| `E_ENV_UNKNOWN_ABILITY` / `E_ENV_ABILITY_EXHAUSTED` | 未知能力 / 本局次数用尽 | 同上 |
| `E_ENV_UNKNOWN_ENEMY` / `E_ENV_RNG_STREAM_MISSING` / `E_ENV_HASH_MISMATCH` | 配表引用未知 `enemy_id` / 请求未注册随机流 / `full_state_hash` 复算 ≠ `info["state_hash"]` | 抛异常：防隐式随机源；哈希不一致属 P0 确定性缺陷，CI 立即失败 |

降级策略：MOD-01 **不做任何自动降级**（规则引擎静默纠错会污染 M0 验收）；唯一「软」路径是 `truncated`。所有错误码同时写入 `Transition.info["error_code"]`，由 MOD-09 集中登记。

## 11. 测试计划（单测/集成/golden/确定性，用「给定-当-则」写验收用例）

| 用例 | 给定 | 当 | 则 |
|---|---|---|---|
| T-01 reset 稳定 | `seed=1042, floors=3` | 连续两次 `reset` | 两次 `full_state_hash` 相等且以 `sha256:` 开头 |
| T-02 骰面范围 | `seed=1..200`（参数化） | `roll_dice` | 每颗 `face ∈ [1, sides]`；`dice.player` counter 增量 = `dice_count` |
| T-03 约束 DSL | 12 条 `(constraint, pip, expected)` 表 | `check_constraint` | 全部命中；`"ge:xx"` 抛 `E_ENV_CONFIG_INVALID` |
| T-04 结算顺序 | 目标 `block=5, hp=10, dmg=3`；另 `block=2, dmg=7`；另 `hp=4, block=0, dmg=4` | `resolve_effect(kind="attack")` | 分别得 `(block=2,hp=10)`、`(block=0,hp=5)`、击杀（`kills+1` 且发 gold/xp）；HP 绝不先于格挡减少 |
| T-05 非法动作 | 任意 `player_turn` 状态；另 `consecutive_illegal=2` | `step(Action("place_die", die_index=99))`；再一步非法 | `reward == -0.05`、前后 `state_hash` 相等、`error_code=="E_ENV_DIE_INDEX_OOR"`；连续第 3 次 `terminated=True, outcome="lose"` |
| T-06 intent 真实性 / 楼层推进 | `seed=7`，玩家连续 `end_turn` 3 回合；另 `floors=3` 脚本逐层击杀 | 对比 `intent.projected_damage` 与实际自伤；走完全程 | 两者相等（含 strength/block 修正）；`floor` 序列 `1,2,3`、`kills=3`、`outcome=="win"` |
| T-07 商店/升级/截断 | `shop_enabled=true`，金币充足/不足两组；`max_turns=5` 且 5 次 `end_turn` | `shop_buy` / `levelup_choose` / `step` | 正常路径生效；不足报 `E_ENV_INSUFFICIENT_GOLD`；`truncated=True` 且 `outcome is None` |
| T-08 **golden + 确定性**（G2/G3） | `tests/golden/mod01/*.jsonl`（3 份固定 seed 快照）；另子进程 `PYTHONHASHSEED=0` 与 `=12345` | 重放固定动作脚本；各跑一次并比对 | `state_hash` 序列逐行相等且两种 `PYTHONHASHSEED` 下逐字节相同；仅 `GAMERSI_UPDATE_GOLDEN=1` 允许重写并要求 CHANGELOG |
| T-09 覆盖率 / import 门禁 / schema（G1/G5） | `pytest --cov=gamer_rsi.env`；`gamer_rsi/env/**`；§6 两份 JSON Schema | 跑单测；MOD-09 `guards.py` AST 扫描；序列化→校验→反序列化 | 行覆盖 ≥ 80%；0 处 `random\|time\|urandom\|numpy\|torch` 与 0 处跨层 import；round-trip 等值且非法样例被拒 |

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6 与里程碑 M0）

| 门 | MOD-01 的 DoD |
|---|---|
| G1 单测 | `pytest tests/unit/test_env_*.py` 全绿；`gamer_rsi/env/` 行覆盖 ≥80%，`engine.py` 分支覆盖 ≥75% |
| G2 Golden | `tests/golden/mod01/` ≥3 份固定 seed 快照（含 1 份 `difficulty=easy`、1 份 `floors=1` Boss 局）；变更须 `GAMERSI_UPDATE_GOLDEN=1` + CHANGELOG |
| G3 确定性 | 同 `(seed, EpisodeConfig, 动作脚本)` 两次运行 `state_hash` 序列逐字节相同；`PYTHONHASHSEED` 变化不影响；CI 中 `E_ENV_HASH_MISMATCH` 零出现 |
| G4 泄漏审计 | 本模块不产 Observation，只需通过 MOD-02 投影审计：`FullState` 的 `seed`/`next_rolls`/`node_kinds`/`shop` 字段进入 MOD-02 黑名单单测 |
| G5 Schema | §6 的 `Action`/`EpisodeConfig` schema 落盘 `gamer_rsi/env/schemas/`，round-trip 测试通过 |
| G6 可复现命令 | `python -m pytest tests/unit/test_env_engine.py -q`、`python -m pytest tests/golden/mod01 -q`、`python -m gamer_rsi env selftest --seed 1`（CLI 由 MOD-09 提供）可直接复制执行成功 |
| M0 | `floors=1` 的真人策略示范局可通关；`state_hash` 序列可复现；与 MOD-02 `SandboxEnv` 联调一次通过 |

## 13. 指标与埋点

MOD-01 只**产出**字段，落地与聚合归 MOD-09/MOD-08。

- 逐步（`Transition.info`）：`illegal`(bool)、`illegal_reason`(str\|None)、`error_code`、`outcome`、`floors`、`kills`、`state_hash`；局末：`turns`、`steps`、`kills`、`floors_reached`、`native_outcome`(win/lose/truncated)、`hp_left`、`damage_dealt`、`damage_taken`、`illegal_count`、`shaped_return`（**仅学习/调试，INV-7 禁止进入主指标**）、`reward_version="srv-1"`、`final_state_hash`；供 MOD-08 次级指标：每回合 `damage_dealt` 曲线与 `illegal_count / steps`。禁止项：不写自建日志格式；不把 `seed`、`next_rolls`、`node_kinds`、`shop` 内容放进任何 agent 可见通道。

## 14. 依赖与风险

| 依赖 | 说明 |
|---|---|
| MOD-09 `infra/config.py` | `configs/*.yaml` 加载并注入 `EpisodeConfig`；本模块不直接读 yaml（避免 IO 依赖） |
| MOD-02 / MOD-08 | 前者是 `FullState` 唯一消费者（字段改名属破坏性变更）；后者直接用 `GameEngine` 跑基线，不需要 Observation；运行时仅标准库（Python 3.11+） |

| 风险 | 对策 |
|---|---|
| 约束 DSL 范围蔓延（多骰/组合条件）拖慢 M0 | 阶段一只支持单骰单条件，`in:`/比较符覆盖奇偶大小；组合条件记 §16 |
| intent 真实性与确定性冲突（重掷 → intent 变谎言 → 类 P0 泄漏） | 定死「回合开始提交 `next_rolls`，敌方回合不掷」，T-06 断言 |
| 浮点进入哈希或奖励；golden 快照频繁失效；敌人 AI 过强/过弱 | 奖励以 1e-3 定点整数累计，`to_canonical_dict` 禁 float + 类型校验；快照只存 `state_hash` 序列，变更需 CHANGELOG + 评审；难度只调 `enemy_hp_mul` 与敌骰数，不改 AI 代码路径，M0 示范局用 `easy` 入档 |

## 15. 里程碑与工作量估算（人日）

| # | 任务 | 产出 | 人日 |
|---|---|---|---|
| 1 | `models.py` + `rng.py`（`derive_seed`、流注册、`state_hash`） | 数据模型与确定性随机 | 1.0 |
| 2 | `engine.py` 核心：reset、掷骰、约束 DSL、`place_die` | FR-01-01..04 | 1.5 |
| 3 | 结算顺序与状态、`end_turn`、敌人 intent/AI | FR-01-05/06/07/13 | 2.0 |
| 4 | 楼层推进、商店、升级、奖励与非法动作策略 | FR-01-08/09/10/11 | 1.5 |
| 5 | `to_canonical_dict`/`full_state_hash` + golden/确定性测试 + 覆盖率与 schema round-trip | FR-01-12、G1/G2/G3/G5 | 1.5 |
| | **合计（M0 的 MOD-01 交付）** | | **7.5** |

顺序 1→2→3→4→5（第 4 项可与 MOD-02 起步并行）；第 5 项若在 M0 截止前未完成，则 M0「`state_hash` 序列一致」不可证。

## 16. 开放问题

**本 PRD 提出的决策（以下每条均为「决策（本 PRD 提出，待回填 Spec）」，累积后回填 Spec 升版）**

| D# | 决策 | 位置 |
|---|---|---|
| D-01 | MOD-01 内部接口为 `GameEngine`（`FullState → Transition`），`SandboxEnv` 由 MOD-02 包装；规则的 `reset/step` 实现在 MOD-01；`Action`/`ActionKind` 在 `gamer_rsi/env/actions.py` 定义为 SSoT | §6 |
| D-02 | 自实现 SplitMix64 PRNG + 命名流（禁 `random` 模块），流名白名单 `STREAM_NAMES`；敌方 `next_rolls` 在玩家回合开始前提交、敌方回合不重掷（intent 必然真实）；`to_canonical_dict` 用 `sort_keys`/`ensure_ascii`/无 float/NFC/UTF-8 码点序 | §6, §8 |
| D-03 | shaped reward 数值表 `srv-1`（§9 各行），仅学习与调试可用（INV-7） | §9 |
| D-04 | 约束 DSL 文法 `any\|odd\|even\|(ge\|gt\|le\|lt\|eq):int\|in:...`，阶段一单骰单条件；难度只通过 `enemy_hp_mul` 与敌骰数实现，不改 AI 代码路径 | §4, §9 |
| D-05 | `EpisodeConfig` 字段集与 JSON Schema（`floors == len(enemies)` 等 fail-fast）；新增 `configs/enemies.yaml`；`use_ability` 仅两个 Warrior 能力；golden 更新仅允许 `GAMERSI_UPDATE_GOLDEN=1` + CHANGELOG | §6, §9, §11 |

**待定项（需 M0 评审或上游确认）**

1. 「需 2 颗骰同时放入」的多骰限制是否纳入阶段一？影响 DSL 文法与动作空间（现阶段仅单骰 `place_die`）。
2. `floors` 默认取值：M0 用 1（单 Boss 局）还是 3？本 PRD 定默认 3、M0 验收用 1。
3. `warrior_reroll` 是否消耗一整回合？影响回合数与启发式基线强度；状态效果是否扩到 `weak`/`blind`（阶段一只留 `strength`/`poison`）？格挡清零时机（现为「回合开始清零」）需与 MOD-08 基线对齐。
4. 是否把 §9 奖励表整体划归 MOD-08（MOD-01 只提供原生计数）？INV-7 隔离更彻底，但需改 Spec §3 职责划分。
