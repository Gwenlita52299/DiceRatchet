# MOD-02 env-sandbox 技术 PRD

> 版本：v1.1（2026-09-30 17:21 按 Spec v1.3 修订，见 §17）· 状态：草案（待复核）· 所属里程碑：M0（`SandboxEnv` + 投影 + 审计器 + 蜜罐自测全部交付）
> 上游 Spec：[../模块拆分Spec-GameRSI.md](../模块拆分Spec-GameRSI.md)（唯一事实来源，冲突以 Spec 为准）· 上游计划：[../../项目计划-GameRSI.md](../../项目计划-GameRSI.md) §3/§8
> 作者：GameRSI 技术文档（MOD-02 负责模块）

## 1. 背景与目标

计划 §3 与「米哈游小丑牌透视预读」教训给出本项目最硬的纪律：**agent 只能看到它该看到的**。MOD-02 是这条纪律在代码层的唯一执行者：它是 L0 与 L1 之间唯一合法数据通道，并提供三条独立防线——**投影裁剪（防泄漏）**、**代码/进程隔离（防越权）**、**审计与蜜罐（防未被发现的泄漏）**。

M0 目标：① 交给 L1+ 的环境数据只可能来自 `gamer_rsi/sandbox/projection.py::project_state()`，未在 `OBSERVATION_SCHEMA` 登记的字段无法出现在 `Observation`（INV-2）；② agent 侧 import `gamer_rsi.env.*` 在 CI 与运行期两级被拒（INV-1）；③ 自编辑 harness 代码只能在受限执行器内运行：无网络、无文件写、无 subprocess、无 env 访问（INV-4）；④ `audit leak` 能从轨迹与 prompt 重建「agent 可见信息集合」并对 6 类泄漏结构化判定（INV-2/3/5）；⑤ 蜜罐 canary 与假字段一旦泄漏 100% 被抓到（G4）。

## 2. 范围

### 2.1 范围内

- `gamer_rsi/sandbox/projection.py`：`OBSERVATION_SCHEMA` 注册表、字段级白名单投影、`obs_digest`、投影协议版本与 CHANGELOG 一致性校验。
- `gamer_rsi/sandbox/api.py`：`SandboxEnv` 门面（agent 侧唯一入口），私有持有 MOD-01 引擎，不暴露 `FullState`。
- `gamer_rsi/sandbox/guard.py`：import 门禁（静态 AST + 运行期 import hook + 子进程边界）、harness 代码受限执行（AST 白名单+黑名单）、资源与超时限制。
- `gamer_rsi/sandbox/audit.py`：可见性重建器、规则扫描器、`leak-report` 输出 schema（CLI 由 MOD-09 暴露）。
- `gamer_rsi/sandbox/honeypot.py`：canary/假字段注入、触发监控、P0 告警、自测用例生成。
- 落地文件：`runs/<run_id>/audit/leak-<scope>.json`、`harness-gate.json`、`honeypot.jsonl`。

### 2.2 范围外（非目标）

> 以下逐字复用 Spec §3 中 MOD-02 的「不负责」清单。

- 规则本身（MOD-01）、奖励函数（MOD-01）、模型侧重试（MOD-03）。

补充（不扩大范围）：非法动作的**语义判定**属 MOD-01/MOD-04，MOD-02 只保证 `info["illegal"]` 被如实投影；`full_state_hash()` 由 MOD-01 实现，MOD-02 只保证不复用为 `obs_digest` 且不入 Observation。

## 3. 使用方与场景

| 使用方 | 场景 | 依赖接口 |
|---|---|---|
| MOD-04 policy | 取 `obs` dict 组装 prompt；取注册表工具函数 | `observe/step/legal_actions`、`load_harness_tools` |
| MOD-06 loop | 驱动单局、落地轨迹 | `reset/step/close`、`obs_digest` |
| MOD-07 evolve | 候选工具先过静态门禁与受限执行再入库 | `gate`、`load_harness_tools`（Spec §4.7，返回子进程 `ToolWorker`） |
| MOD-08 evals | 泄漏审计与蜜罐告警纳入评估报告 | `run_audit`、honeypot 事件 |
| MOD-09 infra | CLI `audit leak`、CI import 门禁、`E_ENV_*` 登记 | `run_audit`、`audit_to_console` |
| 评测复核者 | 事后回答「它有没有看到不该看的」 | `leak-report.schema.json` 全字段 |

## 4. 功能需求（FR-02-x，编号，每条可测试）

| 编号 | 需求（每条可测试） |
|---|---|
| FR-02-1 | `project_state()` 是纯函数：不读全局、不调用 `random`/`time`/`os.environ`；同输入两次调用 canonical JSON 逐字节相同。 |
| FR-02-2 | 只输出 `OBSERVATION_SCHEMA` 中 `enabled=True` 的字段；源 state 存在但未登记的字段一律丢弃，只记计数、不记名称。 |
| FR-02-3 | 输出不得包含 Spec v1.3 §4.2「严禁出现」的任何值：seed、RNG 流状态、未来骰面（抽骰堆顺序）、未揭示的地图行、商店未展示商品、敌人未公开状态、`GameState` 全量；以逐字段哨兵替换测试验证不可反向推回。 |
| FR-02-3a | **投影源是 MOD-01 `observe(state, "full")`**（GAME `obs-1.0.0`，已经是游戏侧白名单），本模块在其上再做键子集 + 3 个 agent 专属键（`episodeId/decisionIndex/lastResult`）+ `stageCount`，字段名保留 camelCase（Spec §4.2 / B-10）。不直接读 `GameState`，因此隐藏信息有两道防线。 |
| FR-02-3b | `render_compact(obs) -> str`：确定性的紧凑文本渲染，供 MOD-04 填入 `{{observation}}`。地图只展开下一行的可达节点，手骰、槽、敌人各占一行。p95 ≤ 1,500 token（8B 分词器）。渲染规则随 `schemaVersion` 版本化，golden 覆盖。 |
| FR-02-4 | `OBSERVATION_SCHEMA` 不可变；字段增删/类型变更必须 `OBSERVATION_SCHEMA_VERSION` 主版本 +1，并在 `docs/prd/OBSERVATION_CHANGELOG.md` 追加「版本/日期/字段/digest 影响/PR 号」。 |
| FR-02-5 | **CI** 校验 CHANGELOG 末行版本 == `OBSERVATION_SCHEMA_VERSION`，不一致则 `E_ENV_PROJECTION_VERSION`。运行时不读文档目录（v1.0 在 import 时读 `docs/`，打包后会失效）。 |
| FR-02-6 | `schema_version` 由投影层强制注入，同名源字段被覆盖（防伪造，字段名见 §6）。 |
| FR-02-7 | `SandboxEnv` 私有持有 `self._engine`，**无任何返回 `FullState` 的公开成员**；`step()` 内先算 `state_hash`/`obs_digest` 再丢弃 full_state 引用。 |
| FR-02-8 | 非法动作由 MOD-01 判定；`step()` 返回 `reward=-0.05, info={"illegal":True,...}` 且不抛异常、观测不变（`lastResult` 更新）。 |
| FR-02-8a | `reject(*, stage, reason, raw, final)`（Spec §4.1/§4.3，P-02 结案）：L1/L2 校验失败时由 MOD-04 调用，不改状态，`illegal_count += 1`，shaped `-0.05`，写一行 `illegal=true` 的轨迹。 |
| FR-02-8b | **唯一判负方**（B-15）：连续 3 次 `reject(final=True)`（中间没有成功的决策）→ 返回 `truncated=True, info.truncation_reason="policy_failure"`，主指标计为 lose。另外 `episode.max_env_steps`/`max_decisions` 超限 → `truncation_reason="step_limit"/"decision_limit"`。MOD-04/06 不另行判负。 |
| FR-02-8c | `legal_actions()` = MOD-01 `legal_actions` 去掉 `abandon_run`，顺序保持不变；`hints()` 转发 `play_hints` 供启发式使用（只含公开信息）。 |
| FR-02-9 | `check_imports()` 扫描 L1–L3（`agent/memory/loop/evolve/evals/inference`）中任何 `import gamer_rsi.env...` / `from gamer_rsi.env import ...`，命中即 `E_ENV_IMPORT_DENIED`。 |
| FR-02-10 | `install_agent_import_hook()` 安装不可卸载的 `MetaPathFinder`，按**调用方模块路径**判定：调用栈中发起 import 的帧属于 `gamer_rsi.{agent,memory,loop,evolve,evals,inference}` 时拒绝 `gamer_rsi.env.*` 并抛 `E_ENV_IMPORT_DENIED`；`gamer_rsi.sandbox` 自身的导入放行。另外，`SandboxEnv` 模块在安装 hook 之前预先 import env。 |
| FR-02-11 | 子进程边界 `run_agent_tick()`：`[sys.executable, "-I", "-S", "-c", shim]`，清空 `PYTHONPATH`、cwd 只读、stdin/stdout 仅 JSON 协议、超时即 kill；供不配合 hook 的第三方代码。 |
| FR-02-12 | `analyze_code()` 采用**节点黑名单 + 调用白名单**：<br>• 禁止的节点：§6 `BLOCKED_NODES`（`Import/ImportFrom` 仅放行 `tool_import_allowlist` 内的模块）。<br>• 禁止的调用、模块与属性：§6 `BLOCKED_CALLS`、`BLOCKED_MODULES`、`ATTR_DENY`（任何 `__*__` 属性）。<br>• **运算符、比较、布尔、切片、推导式等表达式节点一律允许**（v1.0 的节点白名单漏掉了 `Add/Eq/And/USub…` 这些运算符节点，任何写了 `a + b` 的合法工具都会被拒）。<br>返回全部违规的行号与错误码。 |
| FR-02-13 | `gate(artifact_dir)` 对 `harness/tools/*.py` 逐个 `analyze_code`，任一失败整批拒绝（fail-closed），写 `runs/<run_id>/audit/harness-gate.json`，旧 harness 保持可用。 |
| FR-02-14 | **常驻子进程 worker**（Spec B-20）：`load_harness_tools(harness_dir) -> ToolWorker` 启动 `[sys.executable, "-I", "-S", worker.py]`：<br>• `RLIMIT_AS/RLIMIT_CPU/RLIMIT_NOFILE` 只设在**子进程**上。<br>• 工具代码 exec 进 `guarded_globals()`。<br>• 父子进程之间按行传 JSON，请求为 `{tool, obs, candidates, memory}`。<br>• 每次调用有墙钟 `timeout_s`，超时就 `SIGKILL` 并重启 worker，抛 `E_ENV_TIMEOUT`；超出资源限制时 worker 退出并报 `E_ENV_RESOURCE`。调用方都降级为 MOD-04 fallback。<br>主进程持有模型，**绝不**设置 rlimit（v1.0 在进程内设 `RLIMIT_AS=256MB`，会直接杀掉持有 5～19 GB 权重的主进程）；Python 线程无法强杀，所以不使用线程超时。 |
| FR-02-15 | 受限执行期监测逃逸：代码触碰 `_HONEYPOT_*` 命名或 honeypot ledger 即产生 `E_ENV_ESCAPE` P0 事件（sentinel 属性触发）。 |
| FR-02-16 | `rebuild_visible_set()` 从 episodes JSONL + prompt/memory 文本重建 `VisibleSet`：`obs_paths`、`obs_digests`、`memory_texts`、`harness_version`、`seeds_seen`。 |
| FR-02-17 | 扫描 6 类：`seed_leak`、`future_room_leak`、`shop_content_leak`、`full_state_field_leak`、`hidden_field_name_leak`、`canary_leak`，每类输出 finding 明细。 |
| FR-02-18 | 扫描面覆盖 `prompt`（LLM 真实入参）、`memory`（记忆写入）、`log`（JSONL 全字段）；任一出口命中即 P0。 |
| FR-02-19 | `run_audit()` 输出严格符合 `audit/report.schema.json`；退出码 `0` 无 finding / `1` 仅 medium / `2` 含 critical，供 CLI 透传；`evidence` 仅保留命中 token 前后各 `evidence_chars`（默认 12）字符，报告权限 `0600`，避免报告自身成为泄漏载体。 |
| FR-02-20 | `honeypot.inject()` 的注入点改为 GAME 状态中真实存在的隐藏位置：<br>• 未揭示地图行的节点 `preview.enemyNames` 中嵌入 `HONEYPOT_CANARY`<br>• `state.node.shop` 未展示的库存加 `_np_secret_price`<br>• 抽骰堆 `player.drawPile` 末尾的虚构骰 `_np_future_die`<br>• `enemies[]._np_hidden_status`<br>注入只作用于投影前 state 的**副本**，不影响 `stateDigest` 对拍。 |
| FR-02-21 | canary 与 3 个假字段名不得出现在 `OBSERVATION_SCHEMA` 任何 `source_path` 中；注册表导入时断言，违反抛 `E_ENV_HONEYPOT`。 |
| FR-02-22 | `honeypot.scan(text)` 用字节级 `.find()`（非正则）匹配每个 canary/假字段，命中产 `HoneypotHit` + `audit_honeypot_triggered` 事件（MOD-09 格式）。 |
| FR-02-23 | `honeypot.self_test()` 生成 12 条用例（prompt/memory/log × canary/假字段），命中率必须 100%；`audit leak` 前强制执行，低于 100% 直接判 `critical`。 |
| FR-02-24 | `mode="strict"` 记 `dropped_hidden` 计数并告警；`mode="assert"` 命中隐藏字段即抛 `E_ENV_SCHEMA`（供 golden/CI）。 |

## 5. 非功能需求（NFR-02-x：确定性/性能/可复现/可观测）

| 编号 | 需求 |
|---|---|
| NFR-02-1 确定性 | `project_state`/`obs_digest` 无随机、无时间依赖；canonical JSON 统一用 MOD-01 `stable_stringify`（`sort_keys=True, ensure_ascii=True, separators=(",",":")`，与 JS 一致，Spec P-21 结案）；`PYTHONHASHSEED=0` 下同 seed 重跑 `obs_digest`/`state_hash` 序列逐字节相同（G3）。 |
| NFR-02-2 性能 | 投影 + 紧凑渲染 p95 ≤ 2 ms/步（GAME 观测约 12k 字符，v1.0 的 200 µs 是按旧观测估的）；审计 ≤2 s/1000 步；工具 worker 单次调用往返 ≤ 20 ms（常驻进程，不含首次启动）；worker 冷启动 ≤ 150 ms。 |
| NFR-02-3 可复现 | 报告含 `ruleset_version`/`run_id`/`config_hash`；同 run 两次审计除 `generated_at` 外逐字节相同。 |
| NFR-02-4 可观测+fail-closed | 拒绝/超时/触发全部产结构化事件，不自建日志格式；`E_ENV_*` 在 `infra/logging.py` 集中登记；隔离与门禁的内部错误默认拒绝（拒绝执行/判 critical），绝不静默放行。 |
| NFR-02-5 可测性 | 5 个文件仅用 stdlib；`projection`/`audit`/`honeypot` 为纯函数模块，单测覆盖率 ≥80%（G1）。 |

## 6. 接口契约（真实 Python 签名 + JSON Schema）

```python
# gamer_rsi/sandbox/projection.py —— INV-2 唯一权威登记表
OBSERVATION_SCHEMA_VERSION: str = "obs-agent-1.0.0"
@dataclass(frozen=True)  # 全部数据类 frozen + 显式类型，禁可变默认值
class ObsFieldSpec:                  # name/source_path/type/doc/since/optional=False/enabled=True
    name: str; source_path: str; type: str; doc: str; since: str
    optional: bool = False; enabled: bool = True
@dataclass(frozen=True)
class ProjectionStat: kept: tuple[str, ...]; dropped_hidden: int; missing_optional: tuple[str, ...]

OBSERVATION_SCHEMA: tuple[ObsFieldSpec, ...]      # 字段集合见下方 Observation JSON
def obs_field_paths() -> tuple[str, ...]:      # 所有 enabled 字段 name；审计器据此重建可见集合
def project_state(game_obs: Mapping[str, Any], *, ctx: ProjectionCtx, mode: str = "strict") -> tuple[dict, ProjectionStat]:
    """GAME observe(state,"full") → obs-agent-1.0.0（纯函数，FR-02-3a）。mode ∈ {"strict","assert"}；非 optional 字段缺失 → E_ENV_PROJECTION_MISSING。"""
def canonical_json(obj: Any) -> bytes: ...
def obs_digest(obs: Mapping[str, Any]) -> str:          # 含 schema_version 的 canonical JSON → "sha256:<hex>"
def validate_observation(obs: Mapping[str, Any]) -> list[str]:   # 违规字段路径（空=合法），供 G5 round-trip
def render_compact(obs: Mapping[str, Any]) -> str: ...            # FR-02-3b，供 prompt

# gamer_rsi/sandbox/api.py —— 唯一 agent 侧入口；内部 engine 为 MOD-01 实例，不对外暴露
class SandboxEnv:
    def __init__(self, *, engine: Any, episode_config: EpisodeConfig, projection_mode: str = "strict") -> None: ...
    def reset(self, seed: int, *, episode_config: EpisodeConfig | None = None) -> Observation: ...
    def step(self, action: Action) -> StepResult: ...      # StepResult/动作契约见 Spec §4.1/§4.3
    def reject(self, *, stage: str, reason: str, raw: str | None, final: bool) -> StepResult: ...   # FR-02-8a/8b
    def observe(self) -> Observation: ...
    def legal_actions(self) -> list[Action]: ...           # MOD-01 legal_actions 去掉 abandon_run（FR-02-8c）
    def hints(self) -> dict: ...                           # play_hints 转发（公开信息）
    def close(self) -> None: ...
    def last_obs_digest(self) -> str: ...                  # 审计用旁路，不进入 Observation
    def __enter__(self) -> "SandboxEnv": ...;  def __exit__(self, *exc: object) -> None: ...
    # 严禁：任何返回 FullState、engine、rng_state 的公开方法/属性

# gamer_rsi/sandbox/guard.py
class GuardViolation(Exception):
    def __init__(self, code: str, message: str, *, lineno: int | None = None, col: int | None = None) -> None: ...

# 黑/白名单（集合按字母序维护；命中 → E_ENV_CODE_* / E_ENV_AST_DENIED）
BLOCKED_CALLS = frozenset({"eval","exec","compile","__import__","open","input","globals","locals","vars","dir","getattr","setattr","delattr","breakpoint","memoryview"})
BLOCKED_MODULES = frozenset({"os","sys","io","socket","subprocess","shutil","pathlib","importlib","pickle","marshal","ctypes","multiprocessing","threading","requests","urllib","http","gamer_rsi.env","gamer_rsi.sandbox"})
BLOCKED_NODES = frozenset({"Import","ImportFrom","While","Try","With","AsyncFunctionDef","AsyncFor","AsyncWith","ClassDef","Lambda","Yield","YieldFrom","Await","Global","Nonlocal","Delete","Raise"})
# v1.1：只用 BLOCKED_NODES 黑名单 + BLOCKED_CALLS/ATTR_DENY；节点白名单 ALLOWED_NODES 已废弃（漏了 Add/Eq/And/USub 等运算符节点），原列表仅供对照：
# ALLOWED_NODES = frozenset({"Module","FunctionDef","arguments","arg","Return","Assign","AnnAssign","AugAssign","If","For","Expr","Pass","Dict","List","Tuple","Set","Constant","Name","Load","Store","BinOp","BoolOp","UnaryOp","Compare","Call","keyword","Subscript","Slice","Attribute","comprehension","ListComp","DictComp","SetComp","GeneratorExp","IfExp","JoinedStr","FormattedValue","Starred"})
ATTR_DENY = ("__",)      # 任何以 __ 开头的属性名（dunder 逃逸）
GUARDED_BUILTINS: frozenset[str] = frozenset({"len","range","sorted","min","max","sum","abs","round","int","float","str","bool","list","dict","tuple","set","enumerate","zip","reversed","any","all","isinstance","print","ValueError","TypeError","KeyError","IndexError","ZeroDivisionError"})

@dataclass(frozen=True)
class GuardFinding: code: str; lineno: int; col: int; detail: str
@dataclass(frozen=True)
class GuardReport: ok: bool; findings: tuple[GuardFinding, ...]; n_imports: int; scanned_files: tuple[str, ...]

def check_imports(paths: Iterable[Path], *, roots: tuple[str, ...] = ("gamer_rsi.env",)) -> GuardReport: ...
def analyze_code(source: str, *, filename: str = "<harness>") -> GuardReport: ...
def gate(artifact_dir: Path) -> GuardReport: ...
def install_agent_import_hook() -> None: ...
def guarded_globals() -> dict[str, Any]: ...
class ToolWorker:                                      # FR-02-14 常驻子进程
    def call(self, tool: str, *, obs: dict, candidates: list[dict], memory: list[dict], timeout_s: float) -> list[dict]: ...
    def restart(self) -> None: ...;  def close(self) -> None: ...
# v1.0 的 run_harness_callable（进程内设 rlimit）已删除，由 ToolWorker 取代
def run_agent_tick(payload: dict, *, timeout_s: float = 10.0) -> dict: ...
def load_harness_tools(harness_dir: Path) -> ToolWorker: ...   # Spec §4.7

# gamer_rsi/sandbox/audit.py
RULESET_VERSION: str = "1.0.0"
class LeakClass(str, Enum):   # 6 类泄漏 = FR-02-17
    SEED_LEAK="seed_leak"; FUTURE_ROOM_LEAK="future_room_leak"; SHOP_CONTENT_LEAK="shop_content_leak"; FULL_STATE_FIELD_LEAK="full_state_field_leak"; HIDDEN_FIELD_NAME_LEAK="hidden_field_name_leak"; CANARY_LEAK="canary_leak"
@dataclass(frozen=True)
class VisibleSet: obs_paths: tuple[str,...]; obs_digests: tuple[str,...]; memory_texts: tuple[str,...]; harness_version: int; seeds_seen: tuple[int,...]   # seeds_seen 仅 split=="train"
@dataclass(frozen=True)
class LeakFinding: leak_class: LeakClass; surface: str; episode_id: str; step: int; token_class: str; evidence: str; line_ref: str   # surface∈{"prompt","memory","log"}
@dataclass(frozen=True)
class AuditResult: run_id: str; ruleset_version: str; scope: str = "leak"; scanned: dict[str,int]; findings: tuple[LeakFinding,...]; leak_counts: dict[str,int]; honeypot: dict[str,int]; verdict: str

def rebuild_visible_set(run_dir: Path, *, split: str) -> VisibleSet: ...
def scan_text(text: str, *, forbidden: Mapping[str, str], surface: str, episode_id: str, step: int) -> list[LeakFinding]: ...
def run_audit(run_dir: Path, *, ruleset: str = RULESET_VERSION) -> AuditResult: ...
def write_report(result: AuditResult, out: Path) -> Path: ...     # 0600
def audit_to_console(result: AuditResult) -> int: ...             # 0/1/2

# gamer_rsi/sandbox/honeypot.py
HONEYPOT_CANARY: str = "HP-CANARY-7f3a9c1e-4d2b-4a8f-9c11-0b5e8d6a2f47"
FAKE_FIELD_NAMES: tuple[str, ...] = ("_np_secret_price", "_np_future_die", "_np_hidden_status")
@dataclass(frozen=True)
class HoneypotHit: canary: str; surface: str; episode_id: str; step: int; offset: int
def inject(full_state: MutableMapping[str, Any], *, plan_path: Path | None = None) -> MutableMapping[str, Any]: ...
def scan(text: str, *, surface: str, episode_id: str, step: int) -> list[HoneypotHit]: ...
def self_test() -> HoneypotSelfTestResult:   # {"cases":12,"caught":12,"missed":[]}；missed 非空 → E_ENV_HONEYPOT
def reset_canary(*, salt: str) -> str:       # 按 run_id 派生 canary，写 honeypot ledger
```

`Observation` JSON（Spec v1.3 §4.2 的机器可读形式，字段集合由 `OBSERVATION_SCHEMA` 唯一决定；以下为 `player_turn` 的节选，其余键同 GAME-00 §9.4）：
`{"schemaVersion":"obs-agent-1.0.0","episodeId":"e-001042-g003-train","decisionIndex":17,"phase":"player_turn","stage":4,"stageCount":6,"chapter":2,"turn":3,"difficulty":"hard","player":{"hearts":2,"heartsMax":2,"virtue":8,"virtueMax":24,"ward":4,"starShards":120,"statuses":[],"deckSchool":"blade","purifyLeft":0,"abilities":[{"id":"warrior_reroll","usesLeft":1}]},"hand":[{"dieId":"d-0007","defId":"skill_blade_execute","school":"blade","riskTier":"balanced","faceIndex":2,"label":"处决斩 4","kind":"purge","value":4,"statusKind":null,"isCorrupt":false,"corruptionValue":0}],"slots":[{"slotId":"slot_1","slotKind":"attack","constraint":"ge:3","filledBy":null,"effectKind":"purge","bonus":0,"disabled":false}],"enemies":[{"enemyId":"e-00","defId":"mon_verdant_slime","school":"verdant","hp":14,"hpMax":20,"ward":0,"statuses":[],"intent":[{"kind":"purge","value":5,"school":"verdant","arrow":"up","slotId":"e_slot_0"}],"phase":1}],"legalActionKinds":["discard_die","end_turn","play_face","use_ability"],"lastResult":null,"toggles":{"mapBranching":true,"relics":true,"events":true,"shops":true,"schoolCounter":true},"relics":[]}`

`runs/<run_id>/audit/leak-<scope>.json` → `gamer_rsi/sandbox/audit/report.schema.json` 顶层字段：
`run_id` / `generated_at`(iso) / `ruleset_version` / `scope="leak"` / `verdict∈{clean,warn,critical}`（critical 优先级：canary 泄漏或蜜罐失效 > 其余）/ `scanned{episodes,steps,memory_entries}` / `visible_set{obs_paths[],obs_digests[],harness_version}` / `findings[]{leak_class,surface,episode_id,step,token_class,evidence,line_ref}` / `leak_counts{}` / `honeypot{injected,triggered,caught_rate}`。示例：
`{"run_id":"4f2a1c9e-9d1c4e77","generated_at":"2026-09-30T12:00:00Z","ruleset_version":"1.0.0","scope":"leak","verdict":"critical","scanned":{"episodes":60,"steps":1840,"memory_entries":42},"visible_set":{"obs_paths":["schema_version","turn","phase"],"obs_digests":["sha256:..."],"harness_version":7},"findings":[{"leak_class":"seed_leak","surface":"prompt","episode_id":"e-000123","step":14,"token_class":"seed","evidence":"...2013...","line_ref":"prompts/e-001042-g003-train.txt:37"}],"leak_counts":{"seed_leak":1},"honeypot":{"injected":4,"triggered":0,"caught_rate":1.0}}`

## 7. 数据模型与存储

- `Observation`：不可变 `Mapping`（`types.MappingProxyType` 递归冻结），键与类型见 GAME-00 §9.4 + Spec §4.2；`slots[].constraint` 的文法同 GAME-00 §4.3（`any|odd|even|(ge|gt|le|lt|eq):int|in:...`），v1.0 的 `min:N` 已删除；`enemies[].intent` 在玩家回合开始时提交，保证真实。round-trip 无损（G5）。
- `ProjectionStat` 只记计数不记名称（防止经日志反向泄漏隐藏字段名）。
- 落地：`runs/<run_id>/audit/leak-<scope>.json`、`harness-gate.json`、`honeypot.jsonl`（每行 `HoneypotHit`+`run_id`/`generation`），权限 `0600`，不写到 `runs/` 之外；不引入数据库，审计只按文件重放。

## 8. 关键流程（伪代码/时序）

```
# 投影
full_state ─project_state(mode)→ {obs, ProjectionStat}：校验 schema_version ↔ CHANGELOG → 按
  OBSERVATION_SCHEMA.enabled 逐字段取 source_path → 类型强校验 → 丢弃未登记字段 → 覆盖式注入
  schema_version → obs_digest = sha256(canonical_json(obs))          # 单向摘要，可入轨迹
# import 门禁（CI 静态 / 运行期 hook / 不配合时的子进程边界）
CI:     check_imports([agent,memory,loop,evolve,evals,inference]) → 命中即红
运行期: import gamer_rsi.env（sandbox 预导入）→ install_agent_import_hook() → L1+ 帧发起 find_spec("gamer_rsi.env.*") → GuardViolation(E_ENV_IMPORT_DENIED)
不配合: run_agent_tick() → [python -I -S -c shim] ←JSON→ 超时 kill
# harness 准入与受限执行
gate(harness/tools) → 全通过? 否 → 整批拒绝(E_ENV_AST_DENIED)，保留旧版本
  是 → load_harness_tools() 启动子进程 worker(exec 进 guarded_globals()，子进程 rlimit) → worker.call(超时即 kill + 重启)
       → E_ENV_TIMEOUT|E_ENV_RESOURCE → MOD-04 规则 fallback（该局标注降级，不计入评估样本）
# 审计 + 蜜罐
rebuild_visible_set(run_dir) → VisibleSet；对每个 prompt/log/memory：
  scan_text(forbidden = seeds ∪ future_room_ids ∪ shop_items ∪ hidden_field_names ∪ canary)
  honeypot.scan(text)                      # 字节级匹配，无正则
→ findings → verdict；canary 漏判或蜜罐自测失败 ⇒ "critical"
```

## 9. 配置项（键名、类型、默认值、含义）

| 键名 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `sandbox.projection.mode` | enum | `strict` | `strict` 记裁剪计数；`assert` 命中隐藏字段抛 `E_ENV_SCHEMA`（golden 用） |
| `sandbox.projection.schema_version` | str | `obs-agent-1.0.0` | 期望投影协议版本（CHANGELOG 一致性由 CI 校验，FR-02-5） |
| `sandbox.render.max_tokens_p95` | int | `1500` | 紧凑渲染预算（FR-02-3b） |
| `sandbox.guard.enforce` / `scan_roots` | enum / list[str] | `enforce` / `gamer_rsi/{agent,memory,loop,evolve,evals,inference}` | 阻断或仅告警（CI 强制 enforce）；import 门禁扫描范围 |
| `sandbox.guard.process_boundary` | bool | `false` | 是否对不可信 agent 代码启用子进程边界 |
| `sandbox.exec.timeout_s` / `mem_limit_mb` / `cpu_limit_s` / `max_tools` | float/int/int/int | `2.0` / `256` / `2` / `32` | 工具调用墙钟上限；**worker 子进程**的 `RLIMIT_AS`；`RLIMIT_CPU`；单 harness 工具数上限 |
| `sandbox.policy_failure_streak` | int | `3` | 连续 `reject(final=True)` 的次数，达到即判 `policy_failure`（FR-02-8b） |
| `sandbox.audit.enabled` / `surfaces` | bool / list[str] | `true` / `prompt,memory,log` | 每代结束自动审计；扫描出口 |
| `sandbox.audit.ruleset` / `fail_on` / `evidence_chars` | str/enum/int | `1.0.0` / `critical` / `12` | 规则集版本（入报告）；非零退出门槛；证据前后保留字符数 |
| `sandbox.honeypot.enabled` / `plan_path` / `self_test` | bool / str / bool | `true` / `configs/honeypot.yaml` / `true` | 是否注入；注入计划（canary 生成方式、注入点）；审计前是否强制跑 `self_test()` |

环境变量前缀 `GAMERSI_SANDBOX_*`；优先级 CLI > 环境变量 > `configs/default.yaml`（Spec §5）。

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀）

| 错误码 | 触发 | 处理/降级 |
|---|---|---|
| `E_ENV_PROJECTION_MISSING` | 非 optional 字段源缺失 | 终止该局，不返回部分观测 |
| `E_ENV_PROJECTION_VERSION` | CHANGELOG 末行 ≠ 注册表版本 | CI 失败（运行时不读文档） |
| `E_ENV_SCHEMA` | `mode="assert"` 命中隐藏字段、类型不符、观测校验失败 | 抛错；golden/CI 直接红 |
| `E_ENV_IMPORT_DENIED` | agent 侧 import `gamer_rsi.env.*` | 静态：CI 失败；运行期：终止该 tick + P0 事件 |
| `E_ENV_AST_DENIED` / `E_ENV_CODE_SYNTAX` | 黑名单节点/调用/属性；AST 解析失败 | 拒绝该文件；`gate()` 整批拒绝并保留旧 harness |
| `E_ENV_TIMEOUT` | 工具调用超墙钟 | 该次调用失败 → MOD-04 fallback，不判该局失败 |
| `E_ENV_RESOURCE` | 内存/CPU 超限 | 同上；连续 3 次则该 harness 版本标 `degraded` |
| `E_ENV_ESCAPE` | 受限执行触及蜜罐 sentinel | P0 事件 + 立即卸载该 harness 版本，人工确认 |
| `E_ENV_AUDIT_IO` | 轨迹缺失/JSONL 损坏/权限不足 | 判 `critical`（缺证据 ≠ 无泄漏），输出可读原因 |
| `E_ENV_HONEYPOT` | 自测命中率 <100%，或注册表含蜜罐字段 | CI 红；`audit leak` 恒判 `critical` |

原则：隔离类错误 **fail-closed**（拒绝执行）；能力类错误 **fail-soft**（退回 fallback，该局标注降级且不计入评估样本，遵循 Spec §8.5）。

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例）

单测（「给定-当-则」，路径 `tests/unit/test_sandbox_{projection,api,guard,audit,honeypot}.py`）：
- 给定 同一 `FullState`，当 `project_state` 调两次，则两次 canonical JSON 与 `obs_digest` 完全相同（FR-02-1）。
- 给定 源含未登记字段 `hidden_shop_preview`，当 `mode="strict"`，则输出无该字段、`dropped_hidden==1`、日志文本不含该字段名（FR-02-2）。
- 给定 seed/后续房间/商店条目/RNG 状态/未来骰面各替换为唯一哨兵值，当 投影后逐字段比对，则任一哨兵出现在 obs 即失败（FR-02-3）。
- 给定 注册表加字段却不改版本，且源 `schema_version="9.9.9"`，当 import projection / 执行投影，则分别抛 `E_ENV_PROJECTION_VERSION`、obs 取注册表版本（FR-02-5/6）。
- 给定 含 `from gamer_rsi.env.engine import Engine` 的假 L1 文件 + 已装 import hook，当 `check_imports` / `import gamer_rsi.env.rng`，则 `ok=False`（带行号）、抛 `E_ENV_IMPORT_DENIED`（FR-02-9/10）。
- 给定 8 个恶意片段（`open("/etc/passwd")`、`import socket`、`__import__("os")`、`eval`、`exec`、`getattr(x,"_np_secret_price")`、`while True`、`class X`），当 `analyze_code`，则全部被拒且码/行号正确（FR-02-12）。
- 给定 合法 `rank_actions`（纯函数，使用 `+ - * //`、比较、`and/or/not`、推导式、`sorted/len/range/max/min`），当 `gate` + `worker.call`，则通过并返回正确重排（FR-02-12/14，**正例必测**）。
- 给定 `time.sleep(5)` 与申请 1 GB 的两个工具，当 `worker.call(timeout_s=0.2)`，则分别抛 `E_ENV_TIMEOUT`（worker 被 kill 并自动重启，下一次调用正常）与 `E_ENV_RESOURCE`；主进程 RSS 与 rlimit 不变（FR-02-14）。
- 给定 连续 3 个 `reject(final=True)`，当 第 3 次调用，则 `truncated=True`、`truncation_reason="policy_failure"`；中间插入一次成功决策则计数清零（FR-02-8b）。
- 给定 `legal_actions()`，则不含 `abandon_run`，其余元素与顺序同 MOD-01（FR-02-8c）。
- 给定 每个 canary/假字段各注入 prompt、memory、log 三类文本，当 `honeypot.self_test()`，则 12/12 命中、`missed==[]`（FR-02-23，G4）；给定 无 canary 的正常 prompt，当 `honeypot.scan`，则 0 命中（无假阳性）；给定 含 train seed 值且该 seed 未出现在 obs 的 synthetic prompt，当 `run_audit`，则 `verdict="critical"`、`leak_class="seed_leak"`、`surface="prompt"`（FR-02-17/19）。

Golden `tests/golden/`：给定 `src/game/tests/golden/g5_hard_six_stage.jsonl` 的动作脚本，当 经 SandboxEnv 重放，则 agent 侧 `obs_digest` 序列与 `golden/sandbox/g5_obs_agent.txt` 逐字节相同、`render_compact` 输出与 `golden/sandbox/g5_render.txt` 相同（G2/G3）；给定 clean run，当 审计，则报告除 `generated_at` 外与 `golden/leak-report-clean.json` 相同（NFR-02-3）。

集成 `tests/integration/`：给定 含 canary 的 fake run，当 `python -m gamer_rsi audit leak --run <id>`，则退出码 2、`verdict="critical"`、`leak_counts.canary_leak>=1`；给定 干净自测 run，当 审计，则退出码 0、`findings==[]`、`honeypot.caught_rate==1.0`（G4）；给定 JSONL 损坏一行，当 审计，则 `E_ENV_AUDIT_IO` 且判 `critical`；给定 `FullState` 字段清单快照变更，当 `test_env_sandbox_contract.py`，则注册表漂移被捕获。

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

对齐 Spec §9（G1–G6）与 §7（M0：同一 seed 两次 `state_hash` 序列一致；交付可复现模拟器 + 观测投影 + 审计器 + 单测）。

| 门 | MOD-02 判据 |
|---|---|
| G1 单测 | 5 个模块行覆盖 ≥80%；§11 单测全绿 |
| G2 Golden | 固化基于 g5 默认局的 agent 观测摘要、紧凑渲染与 clean 审计报告；改动需 PR 说明 + 显式更新 |
| G3 确定性 | 同 seed 双跑 `obs_digest`/`state_hash` 序列逐字节相同（CI diff） |
| G4 泄漏审计 | 自测 run `audit leak` 0 告警；12/12 蜜罐用例被抓到；故意注入 canary 100% 触发 P0 |
| G5 Schema | `Observation`、`report.schema.json`、`harness-gate.json` 有 schema + round-trip 测试 |
| G6 命令 | 可直接复制：`python -m gamer_rsi env selftest --seed 1`、`python -m gamer_rsi audit leak --run <run_id>`、`pytest tests/unit/test_sandbox_projection.py -q` |
| M0 交付 | `SandboxEnv` 可用且不暴露 `FullState`；L1 import env 的 CI 门禁上线；`OBSERVATION_CHANGELOG.md` 写入 1.0.0 |

**与不变量逐条对应**：

| 不变量 | MOD-02 的落实点 | 验证方式 |
|---|---|---|
| INV-1 agent 只能经 `SandboxEnv.observe/step` 触达环境；L1+ 禁 import `gamer_rsi.env.*` | FR-02-7（门面唯一入口、无 FullState 出口）、FR-02-9/10/11（静态 + import hook + 子进程）；`gamer_rsi.env` 在 `BLOCKED_MODULES` | 假 L1 文件门禁单测、import hook 单测、`sandbox_import_denied` 事件 |
| INV-2 `Observation` 是白名单投影，新字段必须登记 | FR-02-2/4/5/6、§6 `OBSERVATION_SCHEMA` 与 JSON、FR-02-24 | 哨兵替换测试、未登记字段丢弃测试、CHANGELOG 版本校验测试 |
| INV-3 进入 prompt 的上下文仅限当前 Observation + memory + harness + 任务说明 | 审计器对 `prompt` 面重建可见集合（FR-02-16/17/18），检测 prompt 中出现非 obs 的 seed/隐藏字段；`obs_paths` 来自 `OBSERVATION_SCHEMA` 与轨迹求交 | 集成：synthetic seed 泄漏 → 退出码 2；`audit_leak` 事件 |
| INV-4 harness 自编辑代码受限执行：无网络/无文件写/无 subprocess/无 env 访问 | FR-02-12（AST 黑/白名单：`import`/`open`/`subprocess`/`socket`/`__import__`/`eval`/`exec`/`getattr`）、FR-02-13（gate fail-closed）、FR-02-14（超时/内存/CPU）、FR-02-15（逃逸监测）；MOD-07 侧写入动作配合 | 8 个恶意片段拒收单测、超时/资源单测、`E_ENV_ESCAPE` P0 测试、`sandbox_harness_gate` 事件 |
| INV-5 train/heldout/test seed 隔离，heldout/test seed 值不得进入 prompt 或记忆 | `VisibleSet.seeds_seen` 只收 `split=="train"`（FR-02-16）；`seed_leak` 扫描面含 `prompt`/`memory`/`log`（FR-02-17/18），heldout/test seed 值入 forbidden 集合 | 集成：heldout seed 出现在记忆文本 → `verdict="critical"`、`leak_counts.seed_leak>=1` |
| INV-6/INV-7（确定性；主指标用环境原生结果） | 不在 MOD-02 责任范围（MOD-01/09、MOD-08 保证）；MOD-02 只保证 `obs_digest` 确定性（NFR-02-1）为其提供可复现证据 | 同 seed 双跑 digest 一致（G3） |

## 13. 指标与埋点

结构化事件（Spec §8.2 命名，`infra/logging.py` 落地）：`sandbox_projection`（fields/dropped_hidden/duration_us）、`sandbox_import_denied`（module/path/lineno）、`sandbox_harness_gate`（files/ok/findings）、`sandbox_exec`（tool/duration_ms/outcome∈{ok,timeout,resource,error}）、`audit_leak`（verdict/leak_counts/ruleset_version）、`audit_honeypot_triggered`（surface/episode_id/step/canary_hash）、`audit_honeypot_selftest`（cases/caught）。

入档指标（`runs/<run_id>/metrics/`）：`sandbox_dropped_hidden_total`、`sandbox_projection_us_p95`、`sandbox_exec_timeout_rate`、`sandbox_gate_reject_rate`、`leak_count_by_class`、`honeypot_caught_rate`。硬约束：`honeypot_caught_rate` 恒为 `1.0`、`canary_leak==0`；任一违反即 `verdict="critical"` 并阻断该代际接受（MOD-07）。

## 14. 依赖与风险

依赖：MOD-01 的 `FullState` 字段契约与 `state_hash()`；MOD-09 的 `infra/logging.py`、CLI、`guards.py` CI 作业、`GAMERSI_*` 配置加载；MOD-04/07 消费工具注册表与门禁结果。

| 风险 | 影响 | 对策 |
|---|---|---|
| `getattr` 黑名单使合法工具无法表达，或动态逃逸绕过静态扫描 | 误杀/漏杀 | 静态门禁 + 运行期受限 builtins + 蜜罐 sentinel 三重；放宽白名单必须附测试与 PR 说明 |
| macOS 上 `RLIMIT_AS` 不完全生效 | 资源限制虚设 | 以墙钟/子进程超时为第一道；内存限制为尽力而为，测试用 CPU 密集与墙钟超时用例兜底 |
| 子进程边界启动开销与协议复杂度 | 性能与实现成本 | 默认 `process_boundary=false`，仅对不配合 hook 的代码启用 |
| canary 自身经日志/异常栈泄漏进 prompt | 假告警或真泄漏 | canary 只存 honeypot ledger；`evidence` 脱敏；异常栈过滤 sentinel |
| `dropped_hidden` 或日志反向泄漏隐藏字段名 | 违反 INV-2 | 只记计数；`ProjectionStat` 无名称字段；单测断言日志文本 |
| 审计规则集过窄（假阴性），或 `FullState` 字段改名未同步注册表 | 0 告警的假象 / `E_ENV_PROJECTION_MISSING` | 每类泄漏必须有正向用例，canary 作为规则集有效性的活体探针，`ruleset_version` 入报告；字段清单契约快照测试与 MOD-01 PRD 联动 |

## 15. 里程碑与工作量估算（人日）

| 阶段 | 内容 | 人日 |
|---|---|---|
| M0.1 | `projection.py`（注册表/纯函数投影/digest/版本校验）+ 单测 + golden 骨架 | 3.5 |
| M0.2 | `api.py`（`SandboxEnv` 门面、不暴露 FullState、`StepResult` 组装） | 2.0 |
| M0.3 | `guard.py`（AST 门禁 + import hook + 受限执行 + 资源限制） | 4.5 |
| M0.4 | `audit.py`（可见集合重建 + 6 类规则 + 报告 schema） | 4.0 |
| M0.5 | `honeypot.py`（注入/扫描/12 用例自测/P0 事件） | 2.0 |
| M0.6 | 集成、golden 固化、CHANGELOG 首版、CI 门禁接线（与 MOD-09） | 2.0 |
| 合计 | M0 内 | **18.0** |
| M1 追加（可选） | 子进程边界强化 + 审计规则集 v1.1（未来房间/商店内容解析） | 3.0 |

关键路径：MOD-01 → MOD-02 → MOD-03/04；M0.1–M0.5 是解锁 MOD-04 的前置。
## 16. 开放问题

1. **决策（本 PRD 提出，待回填 Spec）**：`FullState` 只经 `build_full_state() -> Mapping[str, Any]` 暴露一次，且该名在 `BLOCKED_MODULES` 语义下不可被 L1+ 调用；若 MOD-01 选择完全私有化，则 `SandboxEnv` 与 MOD-01 的适配层归属需 Spec 明确。`OBSERVATION_SCHEMA` 与 `FullState` 字段清单的契约测试落在 `tests/integration/test_env_sandbox_contract.py`，双方 PRD 各引用一次。
2. **决策（本 PRD 提出，待回填 Spec）**：审计器只做黑名单扫描 + canary 探针，不做「prompt 中每个数值都必须归因到 `VisibleSet`」的正向校验（噪声大）；若 M1 后仍出现未知泄漏再升级。
3. **待定**：canary 是否按 `run_id` 派生（`reset_canary(salt=run_id)`）？跨 run 复用便于测试，但可能被 harness 长期记住；倾向按 run 派生。`sandbox.audit.fail_on` 是否允许 `warn`（本地调试）需 MOD-09 确认，CI 建议恒为 `critical`。
4. **待定**：`phase="shop"` 时 `allowed_actions` 是否携带商品 `item_id`？Spec §4.2 称其「不含隐藏信息」，故本 PRD 采用仅 kind；但 MOD-04 需要 id 才能 `shop_buy`，需在 MOD-04 PRD 明确「进入 shop 后逐步揭示的商品列表」如何以 1.1.0 版本加入 `OBSERVATION_SCHEMA`（预期新增可选字段 `shop_items`）。

---

**变更记录**：v1.0（草案）· 首版：投影注册表、门禁与受限执行、审计规则集、蜜罐自测、INV-1..INV-5 对应表。

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版 |
| v1.1 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：投影源改为 GAME `observe`，产出 `obs-agent-1.0.0`，新增紧凑渲染（P0-1）；新增 `reject(final)` 与唯一判负 `policy_failure`（P0-5）；工具执行改为常驻子进程 worker，主进程不设 rlimit（P0-6）；AST 改为节点黑名单 + 调用白名单，放行运算符（P0-6）；import hook 按调用方判定（P0-6）；蜜罐注入点改为 GAME 的隐藏位置；canonical JSON 统一为 `ensure_ascii=True`（P1-18）；CHANGELOG 校验移到 CI；函数名对齐 Spec §4.7（P1-16）；`min:N` 改为 GAME DSL |
