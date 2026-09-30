# MOD-05 memory 技术 PRD

> 版本：v1.1（2026-09-30 17:21 按 Spec v1.3 修订，见 §17）· 状态：草案（待复核） · 所属里程碑：M2（MOD-05/06/07 初版） · 上游 Spec：[../模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md)（唯一事实来源，冲突以 Spec 为准） · 作者：MOD-05 技术文档 author

## 1. 背景与目标
MOD-05 是 L1 层的**策略记忆与反思库**：把 MOD-06 局末产出的反思文本转成可检索、可度量、可淘汰、可回滚的结构化记忆，供 MOD-04 组装 prompt 时按当前 `Observation` 取用。它是「反思注入」演化对象（项目计划 §5 改进对象 a/c）的落地载体，也是 MOD-07 可回滚的演化对象之一（Spec §3 MOD-05）。目标：① 写入→去重→冲突裁决→检索→淘汰→快照/回滚 全链路**确定性可复现**；② 效用证据只用**环境原生胜负**（Spec INV-7），避免自证式膨胀（调研纪要 §4.1）；③ 硬性落实 **INV-5**：heldout/test 的 seed 与相关信息**永不进入记忆**，四拦截点可审计。非目标见 §2.2——本模块只做存储、检索与生命周期，不生成反思文本，也不判定记忆价值。

## 2. 范围
### 2.1 范围内
- `MemoryEntry` / `ReflectionEntry` schema（JSON Schema 校验 + round-trip，对齐 G5）。
- 写入管线：MOD-06 反思入站 → 结构化抽取 → 去重合并 → 冲突检测 → 原子落盘；以及检索、容量、淘汰与归档。
- 检索 `retrieve(obs, top_k, token_budget)`：标签 + BM25 + 可选向量 + 效用 + 时新度打分，预算裁剪与顺序确定性。
- 快照 `memory_v{N}.jsonl` 与 `harness_version` 血缘、回滚命令；INV-5 四拦截点与 `audit/` 泄漏事件；阶段一存储选型与换向量库触发阈值。

### 2.2 范围外（非目标）
逐字复用 Spec §3 MOD-05 的「不负责」清单：
- 生成反思文本（MOD-06）、评估记忆是否有用（MOD-08 提供指标，MOD-07 做取舍）。

补充边界（不改变上述清单）：不做 prompt 拼装（MOD-04）；不做 embedding 服务实现（走 MOD-03 `embed()`）；不做 seed 注册表与 split 划分（MOD-09 `seeds.yaml` + MOD-08）；不实现泄漏扫描器（只调 MOD-02 `sandbox.audit.scan_text(text, surface="memory")`，Spec §4.7）；不做消融与记忆价值判定（MOD-08）。

## 3. 使用方与场景

| 使用方 | 调用点 | 期望 |
|---|---|---|
| MOD-04 policy | 每次组装决策 prompt 前 | `retrieve()` → 有序、预算内、无泄漏的条目与渲染文本 |
| MOD-06 loop | 局末反思后 / 局内 | `add_reflection(refl, provenance)`；`record_usage()` 回填引用、采纳与胜负 |
| MOD-07 evolve | 每代演化前 / 回滚 | `stats()`、读快照、`snapshot(reason=...)`、`rollback(v)` |
| MOD-08 evals | 消融「去反思」 | 只读 `stats()` 与每代 `entry_count`，不得写库 |
| MOD-09 infra / 人（审计者） | 横切 / `audit leak` | 配置与统一 JSONL 事件、CLI（§7.4） / 任一泄漏事件可定位到 reflection_id、entry_id 与命中串 |

典型场景（S1–S4，流程细节见 §8）：S1 净化值低仍全骰进攻的败局反思 → 抽 1 条 `avoid` 记忆并去重合并；S2 第 3 回合敌方意图 `purge 9`、玩家 `virtue=4` → 检索命中「净化值低先放光盾」置首；S3 同情境相反结论 → 保守共存 + 冲突组；S4 harness 回滚到 v6 时 `memory rollback --to-version 4` 恢复同代记忆。

## 4. 功能需求（FR-05-x）
- **FR-05-1 schema**：两个 entry 有 JSON Schema（draft 2020-12）；`to_json → from_json` round-trip 逐字段相等；`additionalProperties:false`，未知字段拒绝。
- **FR-05-2 反思入站与抽取**：`add_reflection()` 仅接受 `provenance.split == "train"`，否则抛 `MemoryIsolationError(E_EVAL_INV5_LEAK)`；`extract_candidates()` 用**确定性规则**从 `ReflectionEntry.text` 抽出 ≤ `max_candidates_per_reflection` 个候选（`situation/tags/lesson/polarity/action_hint`），无候选时仅存反思、不报错。
- **FR-05-3 去重与冲突检测**：`(situation_key, text_hash)` 相同 → 精确合并；`sim ≥ 0.85` 且 `situation_key` 相同 → 近似合并（合并不新增行，只累加 `outcome_stats` 与 `merged_from`）；同 `situation_key`、`polarity` 相反、`action_hint.kind` 相同（或均 `null`）→ 冲突，默认两条都 active + `conflict_group`，检索只返回组内高分者并标注。
- **FR-05-4 原子落盘**：追加 `memory_active.jsonl`，每 `compact_every` 次 tmp+`fsync`+`os.replace` 压实；崩溃不得留半行。
- **FR-05-5 检索**：`retrieve(obs, *, top_k, token_budget, tick)`（Spec §4.7）按 §6.3 打分，返回 `RetrieveResult`（entries/scores/tokens/dropped/conflict_groups/elapsed_ms）。
- **FR-05-6 顺序与裁剪确定性**：排序键 `(-round(S,6), entry_id)`；`S < min_score(0.15)` 剔除；预算裁剪**跳过**超预算条目而不截断文本，`dropped[]` 记原因。
- **FR-05-7 容量、淘汰与 TTL（逻辑时钟，Spec v1.3 §8.1）**：
  - `tick` = 本 run 已完成的**训练局序号**，由 MOD-06 传入；评估局不推进 tick。
  - `active > max_entries` 时按 `(utility, last_ref_tick, entry_id)` 升序淘汰到上限。免疫条件：`n_ref ≥ 3` 或 `age_ticks < min_age_ticks`（默认 30，即 1 代）。淘汰者 `status="evicted"` 并归档。
  - `expires_tick = created_tick + ttl_ticks`（默认 300，约 10 代），过期即淘汰（`evict_reason="ttl"`）。
  - v1.0 用墙钟（`min_age_days=7`、`ttl_days=90`），而一个 run 只持续几小时，所以全部条目都免于淘汰，容量上限永远不生效；重跑时 `created_at` 不同也破坏了 G3。
- **FR-05-8 效用更新**：`record_usage(episode_key, memory_ids, outcome, *, adopted_ids)`（Spec §4.7）在**局末**由 MOD-06 调用一次，更新引用、采纳、胜负计数与 `last_ref_tick`，按 §6.4 重算 `utility`。`adopted_ids` 由 MOD-04 在 `DecisionResult` 中给出（判定规则见 §6.4）。
- **FR-05-9 快照、血缘与回滚**：每代（或显式）产出 `memory_v{N}.jsonl`，header 登记 `(memory_version, parent_memory_version, harness_version, generation, config_hash, content_sha256)`；`rollback(target)` 校验 sha256 → 原子替换 active 库 → 新版本号（不回退编号，Spec §8.4 不可重写历史）→ 事件 `memory_rollback`。
- **FR-05-10 INV-5 隔离**：§6.5 四拦截点全部生效，命中即拒写/拒返回并落 `audit/leak_events.jsonl`。
- **FR-05-11 可观测与一致性自检**：`stats()` 返回条目数/版本/去重率/冲突数/淘汰数/命中率（事件见 §13）；`verify()` 查行完整性、`entry_id` 唯一、`content_sha256`、`situation_key` 可复算，坏行移入 `memory/quarantine.jsonl` 记 `E_IO_MEMORY_CORRUPT` 且不中断 loop。
- **FR-05-12 陈旧血缘过滤**：条目带写入时 `harness_version`；检索默认排除 `harness_version > current_harness_version` 的条目。

## 5. 非功能需求
- **NFR-05-1 确定性（G3）**：打分、排序、淘汰只用逻辑时钟 `tick`（由调用方显式传入），不读墙钟；`created_at` 只作审计展示，不参与打分，也不计入 `content_sha256`；禁 `import random`（MOD-09 AST 守卫）；同 seed 同输入两次 `retrieve()` 逐字节相同。
- **NFR-05-2 性能与内存**：`active ≤ 2000` 时 `retrieve()` P95 ≤ 50 ms、`add_reflection()` P95 ≤ 15 ms（不含扫描与 embedding）、启动全量加载 ≤ 2 s、常驻内存 ≤ 64 MB（M5/24GB，纯 Python）。
- **NFR-05-3 可复现、稳健与可观测**：快照 `content_sha256` 可跨机复核，`config_hash` 覆盖全部 `memory.*` 键；记忆是增强而非依赖，任何写入/检索失败不得让本局失败（§10）；事件单行 JSONL（公共字段 `run_id/episode_id/harness_version/memory_version/event/ts`），不自建日志格式。
- **NFR-05-4 容量**：单文件 ≤ 16 MB；单条渲染 ≤ 400 字符；`token_budget` 默认 900；`active` 默认上限 2000。

## 6. 接口契约（真实 Python 签名 + JSON Schema）
### 6.1 模块布局（对齐 Spec §5；`snapshot.py` 为新增）
```python
# gamer_rsi/memory/schemas.py 类型 + JSON Schema + round-trip；store.py MemoryStore：写入/去重/冲突/淘汰/快照/回滚
# gamer_rsi/memory/retriever.py retrieve() 打分与预算裁剪；snapshot.py memory_v{N}.jsonl 读写、sha256、回滚  ← 决策（待回填 Spec §5）
```
### 6.2 数据类与函数签名
```python
# gamer_rsi/memory/schemas.py
MEMORY_SCHEMA_VERSION = "1.0"
@dataclass(frozen=True)
class SituationKey:                       # 受控取值，用于硬过滤与情境加成
    phase: Literal["map","player_turn","shop","event","treasure","shrine","forge","rest","relic_swap"]   # GAME phase（obs-agent）
    stage_bucket: Literal["1-2","3-4","5-6","7+"]; node_kind: str | None               # battle/elite/boss/…（战斗内取当前节点）
    virtue_bucket: Literal["critical","low","mid","high"]; hearts_left: int            # virtue/virtueMax 分档 <=25%/<=50%/<=75%/其余
    enemy_school: str | None; intent_kind: str | None; counter_arrow: Literal["up","down","eq"] | None   # enemies[0] 的学派/首个意图/对我方学派的克制箭头
    hand_count: int; corrupt_count: int; slot_kinds: tuple[str, ...]                   # 手骰数、魔化骰数、槽类型排序去重
@dataclass(frozen=True)
class MemoryEntry:
    entry_id: str; schema_version: str; created_at: str; created_tick: int; run_id: str; generation: int; harness_version: int; text_hash: str; content_sha256: str
    source_episode_id: str; source_reflection_id: str; situation: SituationKey; situation_key: str; tags: tuple[str, ...]; lesson: str; action_hint: dict | None
    polarity: Literal["do","avoid"]; confidence: float; last_ref_tick: int | None; n_ref: int; n_adopted: int; n_win_adopted: int; n_not_adopted: int; n_win_not_adopted: int
    utility: float; ttl_ticks: int; expires_tick: int; status: Literal["active","evicted","superseded","quarantined"]; supersedes: str | None; superseded_by: str | None
    conflict_group: str | None; merged_from: tuple[str, ...]; embedding: tuple[float, ...] | None
    def to_json(self) -> str: ...
    def from_json(cls, line: str) -> "MemoryEntry": ...        # @classmethod，round-trip 入口
@dataclass(frozen=True)
class ReflectionEntry:                    # 文本由 MOD-06 产出，本模块不生成
    reflection_id: str; schema_version: str; created_at: str; run_id: str; episode_id: str; seed: int; split: Literal["train"]
    generation: int; harness_version: int; tick: int; text: str; outcome: Literal["win","lose"]; truncation_reason: str | None
    stages_cleared: int; hearts_left: int; battles_won: int; decisions: int; illegal_attempts: int; fallback_decisions: int   # INV-7 口径（Spec v1.3）
    failure_cause: Literal["none","illegal_action","low_damage","wrong_target","virtue_management","corruption_misuse","counter_ignored","route_choice","economy","policy_failure","unknown"]
    tags: tuple[str, ...]; extracted_memory_ids: tuple[str, ...]; scan: dict   # {"status":"pass"|"quarantined","hits":[{"pattern":str,"span":[int,int]}]}
    llm_latency_ms: int | None; prompt_tokens: int | None; completion_tokens: int | None
@dataclass(frozen=True)
class Provenance:                         # 写入准入凭证：唯一的 seed 入口，且只允许 train
    run_id: str; episode_id: str; split: Literal["train"]; seed: int; generation: int; harness_version: int
# gamer_rsi/memory/store.py
class MemoryStore:
    def __init__(self, run_dir: Path, cfg: MemoryConfig) -> None: ...        # 不注入墙钟；时间只来自调用方传入的 tick
    def add_reflection(self, reflection: ReflectionEntry, *, provenance: Provenance) -> AddResult: ...
    def add_memory(self, candidate: MemoryCandidate, *, provenance: Provenance) -> AddResult: ...
    def record_usage(self, episode_key: str, memory_ids: Sequence[str], outcome: Literal["win","lose"], *, adopted_ids: Sequence[str], tick: int) -> None: ...
    def frozen(self, memory_version: int) -> "MemoryView": ...   # 评估局只读视图（Spec §4.7）
    def retrieve(self, obs: ObservationDict, *, top_k: int, token_budget: int, tick: int,
                 tags_hint: Sequence[str] = (), exclude_stale_harness: bool = True) -> RetrieveResult: ...
    def snapshot(self, *, reason: str, memory_version: int | None = None) -> Path: ...
    def rollback(self, target_version: int, *, dry_run: bool = False) -> RollbackReport: ...
    def stats(self) -> StoreStats: ...
    def verify(self, *, full: bool = False) -> VerifyReport: ...
# gamer_rsi/memory/retriever.py（纯函数；本模块不接收 seed 与 FullState）
def situation_key(obs: ObservationDict) -> tuple[str, SituationKey, tuple[str, ...]]: ...
def extract_candidates(reflection: ReflectionEntry, cfg: MemoryConfig) -> list[MemoryCandidate]: ...
def similarity(a: MemoryCandidate, b: MemoryCandidate) -> float: ...
def utility_score(entry: MemoryEntry, *, tick: int) -> float: ...
def bm25_score(query_tokens: Sequence[str], doc_tokens: Sequence[str], df: Mapping[str, int], n_docs: int,
               avgdl: float, k1: float = 1.2, b: float = 0.75) -> float: ...
def tokenize(text: str) -> list[str]: ...        # 英文 [a-z0-9_]+ 小写 + 中文单字 + 相邻中文二元组
def render_entries(entries: Sequence[MemoryEntry], *, max_chars: int = 1200) -> str: ...
```

### 6.3 打分公式（五项权重全部可配置，键见 §9）
```
IDF(t)    = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
BM25(q,d) = Σ_{t∈q} IDF(t) * f(t,d)(k1+1) / (f(t,d) + k1(1 - b + b|d|/avgdl));   BM25Norm = BM25/(BM25+1) ∈[0,1)
TagSim    = Σ_{t ∈ tags(obs) ∩ e.tags} w(t) / Σ_{t ∈ tags(obs)} w(t)   # w: phase/intent_kind/counter_arrow=2.0，virtue_bucket/enemy_school/slot_kind=1.0，stage_bucket/node_kind/corrupt_count=0.5
VecSim(e) = (1 + cos(q_vec, e.embedding)) / 2;   Recency = exp(-λ * (tick - e.created_tick))   # 阶段一 w_vec=0 不计算 embedding；λ=0.01/训练局
S(e) = w_tag*TagSim + w_bm25*BM25Norm + w_vec*VecSim + w_util*U(e) + w_rec*Recency + (0.25 if e.situation_key == key(obs))
硬过滤: status != "active" | expires_tick <= tick | (exclude_stale_harness 且 e.harness_version > current)
返回: 剔除 S < min_score(0.15) → 按 (-round(S,6), entry_id) 升序 → 贪心填 token_budget（跳过装不下的、不截断）→ 截到 top_k
```
冲突组内只保留 S 最高者，被压制者记 `dropped[].reason="conflict_suppressed"`。

### 6.4 效用分（证据只用环境原生胜负，INV-7）
```
p_used = (n_win_adopted+1)/(n_adopted+2);  p_base = (n_win_not_adopted+1)/(n_not_adopted+2)   # Laplace α=1
utility = clip(0.5*(p_used - p_base) + 0.3*log1p(n_ref)/log1p(200) + 0.2*exp(-age_ticks/ttl_ticks), -1.0, 1.0)
# adopted 由 MOD-04 判定并经 DecisionResult.adopted_memory_ids 传给 MOD-06：本局某个决策的计划中存在一个动作，与条目 action_hint 的
# (kind, slotKind, 骰子 school) 三元组一致。v1.0 只比较 kind，而 GAME 中大部分动作都是 play_face，这样判定几乎恒为真。MOD-05 不做语义判断。
```

### 6.5 INV-5 拦截点（四个，缺一不可）
| # | 位置 | 机制 | 失败行为 |
|---|---|---|---|
| I1 源头 | `add_reflection`/`add_memory` 入口 | `provenance.split == "train"` 且 `seed ∈ MOD-09 seeds.yaml.train` | 抛 `MemoryIsolationError(E_EVAL_INV5_LEAK)`，不落盘 |
| I2 文本 | 落盘前逐字段扫 `text/lesson/tags/action_hint` | 调 `sandbox.audit.scan_text(text, surface="memory")`：正则 `\bseed\b\s*[:=#]?\s*\d{1,7}` + 命中 heldout/test seed 集合的裸整数 | `scan.status="quarantined"` → 条目入 `memory/quarantine.jsonl`，事件入 `audit/leak_events.jsonl` |
| I3 兜底 | `snapshot()` 与 `verify(full=True)` | `infra.guards.seed_token_guard()` 复扫全库 | 拒绝出快照（`E_EVAL_INV5_LEAK`），active 条目标 `quarantined` |
| I4 出口 | `retrieve()` 返回前 | 对入选条目做廉价字面量复检（覆盖旧快照载入的条目） | 命中条目丢弃并记事件，不计入返回 |

反思文本里出现 seed 数字**必须**由 MOD-02 扫描（I2）拦下；I3/I4 是本模块兜底，不替代 MOD-02。

### 6.6 JSON Schema（`MemoryEntry`；`ReflectionEntry` 同风格另存 `schemas.REFLECTION_SCHEMA`）
```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","title":"MemoryEntry","type":"object","additionalProperties":false,
 "required":["entry_id","schema_version","created_at","created_tick","expires_tick","ttl_ticks","run_id","generation","harness_version","source_episode_id","source_reflection_id","situation","situation_key","tags","lesson","polarity","confidence","n_ref","n_adopted","n_win_adopted","n_not_adopted","n_win_not_adopted","utility","status","text_hash","content_sha256"],
 "properties":{"entry_id":{"type":"string","pattern":"^mem-[0-9a-f]{12}$"},"schema_version":{"const":"1.1"},"created_at":{"type":"string","format":"date-time"},"created_tick":{"type":"integer","minimum":0},"expires_tick":{"type":"integer","minimum":0},"ttl_ticks":{"type":"integer","minimum":1},"generation":{"type":"integer","minimum":0},"harness_version":{"type":"integer","minimum":0},"situation_key":{"type":"string","pattern":"^sk-[0-9a-f]{8}$"},
  "situation":{"type":"object","additionalProperties":false,"required":["phase","stage_bucket","virtue_bucket","hearts_left","hand_count"],"properties":{"phase":{"enum":["map","player_turn","shop","event","treasure","shrine","forge","rest","relic_swap"]},"stage_bucket":{"enum":["1-2","3-4","5-6","7+"]},"node_kind":{"type":["string","null"]},"virtue_bucket":{"enum":["critical","low","mid","high"]},"hearts_left":{"type":"integer","minimum":0},"enemy_school":{"type":["string","null"]},"intent_kind":{"type":["string","null"]},"counter_arrow":{"enum":["up","down","eq",null]},"hand_count":{"type":"integer","minimum":0,"maximum":8},"corrupt_count":{"type":"integer","minimum":0},"slot_kinds":{"type":"array","items":{"type":"string"},"maxItems":8}}},
  "tags":{"type":"array","maxItems":8,"items":{"enum":["combat","low_virtue","defense","corruption","school_counter","target_choice","map_route","shop","event","forge","relic","elite","boss","status_effect","end_turn","ability"]}},
  "lesson":{"type":"string","minLength":1,"maxLength":240},"action_hint":{"type":["object","null"]},"polarity":{"enum":["do","avoid"]},"confidence":{"type":"number","minimum":0,"maximum":1},"last_ref_tick":{"type":["integer","null"]},"n_ref":{"type":"integer","minimum":0},"utility":{"type":"number","minimum":-1,"maximum":1},"status":{"enum":["active","evicted","superseded","quarantined"]},
  "supersedes":{"type":["string","null"]},"superseded_by":{"type":["string","null"]},"conflict_group":{"type":["string","null"]},"merged_from":{"type":"array","items":{"type":"string"}},"embedding":{"type":["array","null"],"items":{"type":"number"}},"text_hash":{"type":"string","pattern":"^[0-9a-f]{64}$"},"content_sha256":{"type":"string","pattern":"^[0-9a-f]{64}$"}}}
```
示例（`memory_active.jsonl` 的一行；`text_hash` / `content_sha256` 实际为 64 位 hex，此处缩写）：
```json
{"entry_id":"mem-3f1c9ab24d07","schema_version":"1.1","created_at":"2026-10-02T09:14:11Z","created_tick":37,"run_id":"a1b2c3d4-5e6f7a8b-20261002T090000Z","generation":2,"harness_version":1,"source_episode_id":"e-001037-g002-train","source_reflection_id":"refl-88ac1d0b2fe4","situation":{"phase":"player_turn","stage_bucket":"3-4","node_kind":"elite","virtue_bucket":"critical","hearts_left":1,"enemy_school":"blade","intent_kind":"purge","counter_arrow":"down","hand_count":4,"corrupt_count":1,"slot_kinds":["attack","defense","heal","utility"]},"situation_key":"sk-71a0c4d2","tags":["combat","low_virtue","defense","school_counter"],"lesson":"净化值低于 25% 且敌方意图伤害 ≥ 当前净化值时，先把光盾骰放进防御槽，再考虑输出。","action_hint":{"kind":"play_face","slotKind":"defense","school":"neutral"},"polarity":"do","confidence":0.7,"n_ref":4,"n_adopted":3,"n_win_adopted":2,"n_not_adopted":1,"n_win_not_adopted":0,"last_ref_tick":52,"utility":0.42,"ttl_ticks":300,"expires_tick":337,"status":"active","supersedes":null,"superseded_by":null,"conflict_group":null,"merged_from":[],"embedding":null,"text_hash":"9c1f…","content_sha256":"4d2a…"}
```

## 7. 数据模型与存储
### 7.1 文件布局与引擎（`reflections/` 见 Spec §5；`memory/` 目录与 `snapshot.py` 为决策）
```
runs/<run_id>/reflections/{reflections.jsonl,leak_quarantine.jsonl}   # ReflectionEntry 一局一行（仅 train）；扫描不通过者保留文本供审计
runs/<run_id>/memory/{memory_active.jsonl,memory_evicted.jsonl,memory_v{N}.jsonl,quarantine.jsonl,index.json,memory.lock}
runs/<run_id>/audit/leak_events.jsonl                                 # INV-5 命中事件（MOD-02 审计器读）
runs/<run_id>/metrics/memory.jsonl                                    # §13 埋点
```
- **阶段一选型：单文件 JSONL + 全量内存索引**（`dict[entry_id]`、倒排 `tag→ids`、BM25 倒排 `token→ids` 与 `df`）：2000 条量级加载 <2 s、零外部依赖、可肉眼 diff、快照天然是文件。**换库触发阈值**（任一满足）→ 第一跳 SQLite FTS5（+`sqlite-vec`），第二跳 FAISS/LanceDB：① `active > 20,000`；② `retrieve()` P95 > 200 ms；③ 启用 embedding 且条目 > 50,000；④ 需要多进程并发写。换库时 `MemoryStore` 对外签名不变（决策）。
- 压实与容错：每 `compact_every=200` 次追加或每次 `snapshot()` 做 tmp 写 + `fsync` + `os.replace`；追加行 ≤4 KB 且自带 `\n`；读侧遇半行按 `E_IO_MEMORY_CORRUPT` 截断到最后完整 `\n`。
- 快照 header（首行）：`{"_type":"memory_snapshot_header","memory_version":5,"parent_memory_version":4,"harness_version":7,"generation":3,"run_id":"...","created_at":"...","config_hash":"...","store_schema_version":"1.0","entry_count":812,"active_count":640,"reason":"generation_3","content_sha256":"..."}`；`content_sha256` = 其后所有 `_type:"memory_entry"` 行按序拼接的 sha256。

### 7.2 版本与血缘（与 harness_version 的关系）
`memory_version` 单调递增、**永不重写历史**；`parent_memory_version` 指向派生来源（正常代际 = 上一版；回滚 = 目标版本）。每条记忆携带写入时 `harness_version`，即只属于产生它的 harness 代际：MOD-07 回滚 harness 到 `h'` 后，`harness_version > h'` 的条目默认不参与检索（FR-05-12），直到再次被引用确认。血缘三元组 `(generation, harness_version, memory_version)` 写入 `index.json`，MOD-08 报告按此对齐「去反思」消融。

### 7.3 检索输出（给 MOD-04）
`render_entries()` 每条一行：`"[combat|low_virtue|defense] 净化值低于 25%…（hint=play_face→defense, conf=0.70, util=0.42）"`，按分数顺序 `\n` 连接；`RetrieveResult.text` 即 `{{memory}}` 的填充值。

### 7.4 CLI（走 MOD-09 CLI 注册；**决策（本 PRD 提出，待回填 Spec §4.6）**）
```bash
python -m gamer_rsi memory stats|snapshot|rollback|verify|retrieve --run-id <run_id> [--reason gen-003] [--to-version 4] [--dry-run] [--full] [--obs <obs.json>] [--top-k 8] [--token-budget 900]
# 例：python -m gamer_rsi memory snapshot --run-id <run_id> --reason gen-003
# 例：python -m gamer_rsi memory rollback --run-id <run_id> --to-version 4 --dry-run
```
`rollback` 打印 `memory_version: 5 -> 6 (restored from v4, harness_version=6)`；目标缺失退出码 2（`E_IO_SNAPSHOT_MISSING`）。

## 8. 关键流程
### 8.1 写入管线
```
add_reflection(refl, provenance):
  I1: if provenance.split != "train" or provenance.seed not in seeds.train: raise E_EVAL_INV5_LEAK
  I2: scan = audit.scan_text(all_text_fields(refl), surface="memory")    # MOD-02
      if scan.status != "pass": append leak_quarantine + audit/leak_events; return QUARANTINED
  append reflections.jsonl
  for c in extract_candidates(refl, cfg)[: max_candidates_per_reflection]:
      key = situation_key_of(c); hits = index.same_situation(key)
      if any(h.text_hash == c.text_hash for h in hits): merge(survivor(h), c); continue        # 精确重复
      near = [h for h in hits if similarity(h, c) >= cfg.dedup.sim_threshold]                  # 近似重复（默认 0.85）
      if near: merge(survivor(near), c); continue
      conf = [h for h in hits if h.polarity != c.polarity and same_hint_kind(h, c)]             # 冲突：同情境相反结论
      if conf: c.conflict_group = sha1(key + "|" + kind(c))[:12]; 给 conf 同组标记
               if cfg.conflict.auto_supersede and evidence_ok(c, conf):
                   old.status = "superseded"; old.superseded_by = c.entry_id; c.supersedes = old.entry_id
      append memory_active.jsonl; update index
  if active_count > max_entries: evict()
  if appends % compact_every == 0: compact()
  return AddResult(added, merged, conflicted, quarantined, memory_version)
```
去重度量为 `similarity(a,b) = 0.5*Jaccard(tokenize(a.lesson), tokenize(b.lesson)) + 0.5*cos(char3gram_tfidf(a.lesson), char3gram_tfidf(b.lesson))`，阈值 `sim_threshold=0.85` 且要求 `situation_key` 相同（跨情境的不同结论不算重复）。`merge`：survivor = `utility` 高者（并列取 `created_tick` 小者，再并列取 `entry_id` 小者），`outcome_stats` 逐项相加，`confidence = max`，`merged_from += other.entry_id`，重算 `text_hash/content_sha256`，**不新增行**。

### 8.2 淘汰与 TTL
```
evict(tick):  pool = [e for e in active if not (e.n_ref >= 3 or tick - e.created_tick < min_age_ticks)]; for e in sorted(pool, key=(utility, last_ref_tick or -1, entry_id))[: active_count - max_entries]: e.status="evicted"; archive("capacity")
sweep_ttl(tick): for e in active: if e.expires_tick <= tick: e.status="evicted"; archive("ttl")      # 写入与 verify() 顺带触发
```

### 8.3 快照 / 回滚
```
snapshot(reason):  I3: seed_token_guard(所有 active 条目)，命中即 E_EVAL_INV5_LEAK 并中止
                   N = index.memory_version + 1；write memory_v{N}.jsonl（header + entries，含 content_sha256）
                   index.json = {memory_version:N, parent:current, harness_version:current, entry_count, active_count}
rollback(target, dry_run): 文件不存在 → E_IO_SNAPSHOT_MISSING（退出码 2）；sha256 不符 → E_IO_MEMORY_CORRUPT
                   dry_run → 返回 report(entries_delta, active_delta, lost_entry_ids)
                   否则原子替换 memory_active.jsonl；写新 header(memory_version=current+1, parent_memory_version=target, reason="rollback_to_v{target}")；发 memory_rollback 事件
```
S4 时序：MOD-07 判负 → 恢复 `harness/version.json` 到 v6 → `memory.rollback(同代快照)` → MOD-07 重跑 heldout 确认 → 报告记录 `(generation, harness_version=6, memory_version=6)`。

## 9. 配置项
`configs/default.yaml` 的 `memory:` 段；优先级（Spec §5）：CLI > 环境变量 > 默认值，嵌套键用 `__`（如 `GAMERSI_MEMORY_RETRIEVE__TOP_K`）——决策，待回填 Spec §5。

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `memory.enabled` / `memory.store_format` | bool / str | `true` / `"jsonl"` | 关闭时 `retrieve()` 返回空、写入 no-op（「去反思」消融用） / 阶段一唯一取值，`"sqlite"` 留给升级路径 |
| `memory.max_entries` / `.ttl_ticks` / `.min_age_ticks` / `.compact_every` | int | `2000` / `300` / `30` / `200` | active 容量上限 / 默认存活训练局数（约 10 代）/ 淘汰免疫保护期（1 代）/ 追加多少次后压实 |
| `memory.extract.max_candidates_per_reflection` / `.use_llm` | int / bool | `3` / `false` | 单条反思最多抽多少候选；true 时经 MOD-03 `generate_json(temperature=0, seed=0)` 抽取，失败降级为规则抽取并记 `E_LLM_*` |
| `memory.dedup.sim_threshold` / `.jaccard_weight` / `.char3_weight` | float | `0.85` / `0.5` / `0.5` | 近似重复阈值与 `similarity` 两项权重 |
| `memory.conflict.auto_supersede` / `.min_refs` / `.winrate_delta` | bool / int / float | `false` / `5` / `0.15` | 是否允许证据充分时自动替代旧结论，及门槛（Wilson 95% CI 不重叠） |
| `memory.retrieve.top_k` / `.token_budget` / `.min_score` / `.same_situation_bonus` | int / int / float / float | `8` / `900` / `0.15` / `0.25` | 返回条数 / 渲染 token 上限 / 分数下限 / 情境完全相同的加成 |
| `memory.retrieve.weights.tag` / `.bm25` / `.vector` / `.utility` / `.recency` | float | `1.0` / `0.6` / `0.0` / `0.3` / `0.1` | 五项权重（`.vector=0` 即关闭向量检索） |
| `memory.retrieve.bm25.k1` / `.b` / `memory.retrieve.recency_lambda` | float | `1.2` / `0.75` / `0.01` | BM25 词频饱和 / 长度归一 / 时新度衰减（每训练局） |
| `memory.embedding.enabled` / `.model` / `.dim` | bool / str / int | `false` / `"bge-small-zh-v1.5"` / `512` | 向量检索开关、模型、维度校验（走 MOD-03 `embed()`） |
| `memory.snapshot.every_generation` / `memory.scan.on_write` | bool | `true` / `true` | 每代自动快照 / 写入时调用 MOD-02 扫描（仅测试可关） |

## 10. 错误处理与降级
错误码前缀沿用 Spec §8.3（`E_IO_*` / `E_EVAL_*`），由 MOD-09 集中登记；标注（新增）者为本 PRD 提出的码。

| 码 | 触发 | 处置 / 降级 |
|---|---|---|
| `E_EVAL_INV5_LEAK`（v1.0 为 `E_EVAL_SEED_LEAK`，按 Spec §8.3 统一） | I1–I4 任一命中 | **P0**：拒写/拒返回；事件入 `audit/leak_events.jsonl`；loop 不中断，该反思标记为不可用于演化 |
| `E_IO_MEMORY_CORRUPT` | 行解析失败 / `content_sha256` 不符 / `entry_id` 重复 | 坏行移入 `memory/quarantine.jsonl`，其余正常加载，检索继续 |
| `E_IO_MEMORY_LOCKED`（新增） / `E_IO_MEMORY_QUOTA`（新增） / `E_IO_SNAPSHOT_MISSING`（新增） | 压实期 `.lock` 被占用 >5 s / 单文件 >16 MB 或 `active > 2*max_entries` / `rollback` 目标快照缺失 | 跳过本次压实改下次（写入仍走追加） / 强制淘汰+压实，仍超则停写并告警（读不受影响） / 回滚失败，保持当前版本，CLI 退出码 2 |
| `E_EVAL_CONFLICT_UNRESOLVED`（新增） | `auto_supersede=false` 下冲突无法裁决 | 保守：两条共存，检索压制其一（FR-05-3），发 `memory_conflict` 事件 |
| `E_IO_MEMORY_WRITE_FAILED`（新增） | 磁盘错误 / 权限 | 本局继续（记忆是增强）；同 run 第 3 次失败后本 run 停写并显眼标注（该 run 不参与消融对比） |
| `E_LLM_EXTRACT_FAILED` | `extract.use_llm=true` 且 MOD-03 失败 | 降级为规则抽取；反思仍入库 |

检索侧任何异常一律降级为空记忆（MOD-04 走无记忆 prompt，符合 INV-3 中「检索到的 memory」可为空），并发 `memory_retrieve_failed` 事件。

## 11. 测试计划（给定-当-则）
单测 `tests/unit/memory/{test_schemas,test_store,test_retriever,test_dedup,test_conflict,test_eviction,test_snapshot,test_isolation}.py`；集成 `tests/integration/test_loop_memory.py`；golden `tests/golden/memory/{retrieve_seed1042_g0.json,memory_v0_sha256.txt}`。

- **GWT-05-01/02 schema 与抽取（G5）**：给定一条合法 `MemoryEntry` JSON，当 `from_json` 后 `to_json`，则字节相同；给定多一个字段 `"foo":1`，则校验失败；给定反思文本「净化值只剩 3 时我还在打输出」，当 `extract_candidates` 连续调用 10 次，则返回 1 条 `polarity="avoid"`、`tags ∋ {low_virtue, combat}`，且 10 次结果全等。
- **GWT-05-03 去重（精确 + 近似）**：给定同 `situation_key` 且 `sim=0.87 ≥ 0.85` 的既有条目，当写入近义 lesson，则 `memory_active.jsonl` 行数不变、`merged_from` 长度 +1、胜负计数累加；给定同 lesson 但 `phase`/`virtue_bucket` 不同，当写入，则新增一行。
- **GWT-05-04 冲突保守**：给定同 `situation_key`、hint kind 相同、polarity 相反的两条，当写入第二条，则两条均 `status="active"` 且共享 `conflict_group`；当 `retrieve`，则只返回组内高分者 + `dropped.reason="conflict_suppressed"`。
- **GWT-05-05 顺序、预算与 golden（G3）**：给定 12 条候选、`token_budget` 只够 3 条，当 `retrieve(obs, top_k=8, token_budget=900)`，则返回 3 条、顺序为 `(-round(S,6), entry_id)` 升序、`dropped` 含 9 条（原因 `below_min_score` / `token_budget_exceeded`）；给定第 4 条超预算但第 5–6 条很短，则第 4 条被跳过、第 5–6 条仍入选；给定固定 `tick` 与 golden `retrieve_seed1042_g0.json`，则输出与 golden 逐字节相同。
- **GWT-05-06 淘汰保护与 TTL**：给定 `max_entries=3`、`min_age_ticks=30`，其中一条 `n_ref=5`、一条 `age_ticks=1`，当 `tick=100` 时写入第 4 条，则被淘汰的是 `(utility,last_ref_tick,entry_id)` 最低且不免疫者，受保护条目仍在 active；给定 `ttl_ticks=1` 与 `tick = created_tick + 2`，当 `verify(tick)`，则条目 `status="evicted"` 并归档 `memory_evicted.jsonl`。
- **GWT-05-07 效用分**：给定 `n_adopted=3,n_win_adopted=2,n_not_adopted=1,n_win_not_adopted=0`，当 `record_usage`，则 Laplace 率 `=3/5=0.6`、`p_base=1/3≈0.333`、`utility` 严格增大且 ∈[-1,1]。
- **GWT-05-08 快照 + 回滚**：给定已生成 `memory_v3.jsonl` 后又写入 5 条，当 `rollback(3)`，则 `memory_active.jsonl` 与 v3 逐字节相同、新 `memory_version=current+1`、`parent_memory_version=3`、`memory_rollback` 事件一条。
- **GWT-05-09 损坏行隔离**：给定 active 文件中间被截断一行，当 `MemoryStore.__init__`，则加载成功、坏行进 `quarantine.jsonl`、事件码 `E_IO_MEMORY_CORRUPT`、`stats().entry_count` = 合法行数。
- **GWT-05-10 INV-5 / I1 + I2**：给定 `provenance.split="heldout"`，当 `add_reflection`，则抛 `MemoryIsolationError(E_EVAL_INV5_LEAK)`、两份 JSONL 行数不变；给定 train 反思含「如果 seed 是 2001 我会先放盾」（2001 ∈ heldout 集合），当 `add_reflection`，则条目不落 active 库、`reflections/leak_quarantine.jsonl` +1、`audit/leak_events.jsonl` +1 且 `hits[0].pattern` 记录命中串。
- **GWT-05-11 INV-5 / 蜜罐（G4）与 I3 + I4**：给定 `sandbox.honeypot` 注入含 test seed 的伪反思，当走完整写入管线，则 `audit leak --run-id <run_id>` ≥1 告警且 `stats().seed_leak_blocks ≥ 1`；给定手工构造含裸 seed 数字的旧快照，当 `verify(full=True)` 与 `retrieve()`，则前者拒绝出快照并抛 `E_EVAL_INV5_LEAK`，后者丢弃该条目且不计入返回。
- **GWT-05-12 端到端（M2 集成）**：给定 train 循环 5 局 + 1 轮演化，当 `loop run --generations 1 --k 5`，则 `generations/gen-001/report.json` 可读到 `memory_version`，且每个 `memory_v{N}.jsonl` 的 `content_sha256` 校验通过。

## 12. 验收标准（DoD）
对齐 Spec §9 质量门与里程碑 M2：
- **G1 单测**：`tests/unit/memory/` 全绿；`schemas/retriever/dedup/utility` 等纯函数**行覆盖 ≥80%**，`store.py` 分支覆盖 ≥70%。
- **G2 Golden**：`retrieve_seed1042_g0.json` 与 `memory_v0_sha256.txt` 入库；任何改变检索输出的改动必须显式更新 golden 并在 PR 说明。
- **G3 确定性**：同 run/同 seed/同 `tick` 两次 `retrieve()` 逐字节相同，两次重跑得到的记忆库 `content_sha256` 相同；`gamer_rsi/memory/` 中无 `random`/墙钟随机（MOD-09 AST 守卫通过）。
- **G4 泄漏审计**：自测 run 上 `python -m gamer_rsi audit leak --run-id <run_id>` **0 告警**；蜜罐注入**必须**产生 ≥1 告警（GWT-05-11）。
- **G5/G6 Schema 与命令**：两个 entry 有 JSON Schema + round-trip 测试，跨模块 `RetrieveResult`/`AddResult` 有 schema；§7.4 五条 CLI 在空 run 上可直接复制执行成功。
- **M2 交付**：闭环 ≥1 轮演化中「反思入库 → 记忆检索注入 → 快照 → 回滚演练」全链路跑通；`report.json` 引用 `(memory_version, harness_version)` 血缘；`memory rollback` 演练 ≥1 次并留事件。**明确不验收**：反思文本质量（MOD-08 指标 + MOD-07 取舍）、记忆是否提升胜率（MOD-08 消融）。

## 13. 指标与埋点
事件单行 JSONL → `runs/<run_id>/metrics/memory.jsonl`，公共字段 `ts/run_id/episode_id/generation/harness_version/memory_version/event`：

| event | 额外字段 |
|---|---|
| `memory.add` | `entry_id, source_reflection_id, tags, polarity, result(added\|merged\|conflicted\|quarantined)` |
| `memory_merge` / `memory_conflict` | `survivor_id, merged_id, sim, reason(exact\|near)` / `conflict_group, entry_ids, winner_id, rule(auto\|suppress)` |
| `memory_quarantine` / `memory_seed_leak_blocked` / `memory.evict` | `entry_id\|reflection_id, reason(corrupt\|conflict\|scan)` / `interception_point(I1..I4), field, matched_pattern` / `entry_id, evict_reason(capacity\|ttl), utility, n_ref` |
| `memory_retrieve` | `obs_digest, n_candidates, returned_ids, dropped_reasons{}, tokens, elapsed_ms` |
| `memory_snapshot` / `memory_rollback` | `from_version, to_version, entry_count, content_sha256` |
| `memory_write_failed` / `memory_retrieve_failed` | `code(E_IO_*), message` |

派生指标（`memory stats` 与代际报告引用）：`dedup_rate = merged/added_candidates`、`conflict_rate`、`retrieve_hit_rate = 被引用条目中 adopted 占比`、`retrieve_p95_ms`、`budget_utilization = tokens/token_budget`、`memory_growth_per_generation = Δactive/Δgen`（膨胀告警阈值 >200/代）、`seed_leak_blocks`（期望恒 0；蜜罐测试时 ≥1）。埋点不得含 heldout/test 相关字段——I4 同样约束事件，`obs_digest` 为单向摘要且不含 seed。

## 14. 依赖与风险
依赖：MOD-02（`guard.scan_text`、`Observation`）、MOD-04（schema 协作方与 `action_hint` 消费方，Spec §6 记 MOD-05 依赖 MOD-04 schema）、MOD-06（反思文本与 `provenance`）、MOD-09（配置/日志/`run_id`/CLI/`infra.guards.seed_token_guard`/`seeds.yaml`）、MOD-03（可选 `count_tokens`、`embed()`）。

| 风险 | 影响 | 对策 |
|---|---|---|
| 规则式抽取召回低（反思表述自由） | 记忆增长慢、注入退化 | 用受控反思模板（MOD-07 演化对象 c）约束句式；`extract.use_llm=true` 为可选增强（temperature=0 + 固定 seed，保持确定性） |
| 自我确认（只引用自己的结论） | 虚假增益（调研纪要 §4.1） | 效用只用环境原生胜负；时新度衰减 + 冲突压制；MOD-08「去反思」消融对照 |
| 记忆膨胀 / 噪声堆积 | 检索变慢、prompt 被稀释 | 容量 + TTL + 效用淘汰三重闸；`memory_growth_per_generation` 告警 |
| INV-5 旁路（反思里带 seed） | P0 沙箱泄漏（米哈游教训） | 四拦截点 + MOD-02 扫描 + 蜜罐测试纳入 G4 门 |
| 快照与 harness 版本错配 / 冲突裁决误杀 / JSONL 重写成本上升 | 演化对照失效 / 行为退步 / P95 延迟破线 | 血缘三元组写入 `index.json`，陈旧 `harness_version` 条目默认不检索（FR-05-12）；默认 `auto_supersede=false` 保守共存，仅证据阈值（n_ref≥5、胜率差≥0.15、CI 不重叠）下自动替代；§7.1 触发阈值换库且签名不变 |

## 15. 里程碑与工作量估算（人日）

| 工作项 | 人日 | 依赖 |
|---|---|---|
| schema + JSON Schema + round-trip + 类型（FR-05-1） | 1.0 | MOD-04 schema 冻结 |
| `MemoryStore` 骨架：JSONL 读写、原子追加/压实、索引、`verify()`、效用/容量/TTL/淘汰（FR-05-4/7/8/11） | 3.5 | — |
| 抽取 + 去重 + 冲突（FR-05-2/3） | 1.5 | 上项 |
| `retriever`：BM25/标签/效用/预算裁剪 + golden（FR-05-5/6） | 2.0 | MOD-03 `count_tokens`（或本地估算） |
| 快照 / 血缘 / 回滚 + CLI（FR-05-9、§7.4） | 1.5 | MOD-09 CLI |
| INV-5 四拦截点 + 蜜罐与隔离测试（FR-05-10） | 1.5 | MOD-02 扫描、MOD-09 seed 注册表 |
| 埋点 + docs + 集成测试（FR-05-11/12、§13、GWT-05-12） | 1.0 | 全部 |
| **合计** | **12.0 人日**（含缓冲；纯实现约 9.5） | — |

排期：与 MOD-08 同期启动（Spec §6 标注可并行）；前三项 ≈4.5 人日完成即可先跑通「反思入库 + 检索注入」最小闭环供 MOD-06 集成。

## 16. 开放问题
1. **换向量库阈值**：本 PRD 提出 §7.1 的四个触发条件，第一跳 SQLite FTS5（+`sqlite-vec`）、第二跳 FAISS/LanceDB；是否需用真实规模实验校准 `active > 20,000` 与 `> 50,000`？
2. **`adopted` 判定归属**：本 PRD 定为 MOD-04/06 计算并传入（MOD-05 不做语义判断）；若 MOD-04 认为属记忆侧职责，需在 Spec 层重新划界。
3. **冲突自动替代证据标准与版本号统一**：`n_ref ≥ 5`、胜率差 `≥ 0.15` 为拍定值，需 M2 数据回填（也可能永久保持 `auto_supersede=false`）；`memory_version` 与 `harness_version` 是否统一为 `artifact_version`（待 MOD-07/MOD-09 决策）。
4. **Spec 回填**：`memory/` 目录、`memory` 子命令、`snapshot.py` 均未在 Spec §5/§4.6 登记；不回填即违反「下游 PRD 必须与 Spec 一致」。
5. **跨 run 记忆复用与中文分词**：阶段一每 run 独库、分词用字符 unigram/bigram（零依赖）；跨 run 迁移的 INV-5 复查成本、是否引入 `jieba`（增加依赖需确定性校验）均待定。

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版 |
| v1.1 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：时间改为逻辑时钟 `tick`（训练局序号），`ttl_ticks=300`、`min_age_ticks=30`，修复淘汰永不生效和 G3 不成立的问题；`SituationKey`、tags、`ReflectionEntry` 改为 GAME/INV-7 口径（P0-1）；`adopted` 改为 `(kind, slotKind, school)` 三元组判定，由 MOD-04 给出、MOD-06 在局末调用 `record_usage`（P1-2）；新增 `frozen()` 只读视图；`scan_text` 改为 `sandbox.audit`（P1-16）；`E_EVAL_SEED_LEAK` 统一为 `E_EVAL_INV5_LEAK`（P1-12）；schema_version 升为 1.1 |
