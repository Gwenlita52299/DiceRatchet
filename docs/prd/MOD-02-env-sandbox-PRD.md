# MOD-02 env-sandbox 技术 PRD

> 版本：v1.2.5（2026-10-14 **F77-a：`audit leak` 改用快照的 `content_hash` 反查真实配表**——原实现**信任快照声明的 `env.content_dir`**，而运行时的权威是 MOD-01 `loader.GAME_CONTENT_DIR`（import 时解析、从不读 yaml）⇒ 两者不一致时审计会把 `future_room_leak`/`shop_content_leak` 建在**这些局从未用过的配表**上（假阳性/假阴性，F58/F59 同族），重放跑不动时还只回一条 warning（六类里两类实际未检查而 verdict 仅 `warn`）。现在按 digest 反查：命中者才算；有 digest 却无人相符 ⇒ 记 `io_errors` 判 **critical** 并降级这两类规则集，**绝不**用默认配表凑数。见 §9 与 §17）· 历史版本：v1.2.4（2026-10-14 F75：`env.sandbox.*` 逐键核完，15 键砍到 2 键，删掉的 13 个里四个是「不变式开关」；`configs/honeypot.yaml` 保留为契约夹具；新增「键集 == 已登记消费者表」护栏，见 §9/§14/§17）· v1.2.3（2026-10-14 F73/F74：`sandbox.audit.*` 收拢为规则集版本钉扎 `ruleset_pin` + 钉扎不符 fail-closed + 删掉「每代结束自动审计」措辞，见 §9/§10/§16/§18.2）· v1.2.2（2026-10-11 F45-2：删两张人工死名单、改成全字段扫描；seed 端口空间换六位，见 §17 与 §18.1 item 7）· v1.2.1（2026-10-03 追加 §18 实现落地记录）· 状态：草案（待复核）· 所属里程碑：M0（`SandboxEnv` + 投影 + 审计器 + 蜜罐自测全部交付）
> 上游 Spec：[../模块拆分Spec-DiceRatchet.md](../模块拆分Spec-DiceRatchet.md)（唯一事实来源，冲突以 Spec 为准）· 上游计划：[../../项目计划-DiceRatchet.md](../../项目计划-DiceRatchet.md) §3/§8
> 作者：DiceRatchet 技术文档（MOD-02 负责模块）

## 1. 背景与目标

计划 §3 与「米哈游小丑牌透视预读」教训给出本项目最硬的纪律：**agent 只能看到它该看到的**。MOD-02 是这条纪律在代码层的唯一执行者：它是 L0 与 L1 之间唯一合法数据通道，并提供三条独立防线——**投影裁剪（防泄漏）**、**代码/进程隔离（防越权）**、**审计与蜜罐（防未被发现的泄漏）**。

M0 目标：① 交给 L1+ 的环境数据只可能来自 `dice_ratchet/sandbox/projection.py::project_state()`，未在 `OBSERVATION_SCHEMA` 登记的字段无法出现在 `Observation`（INV-2）；② agent 侧 import `dice_ratchet.env.*` 在 CI 与运行期两级被拒（INV-1）；③ 自编辑 harness 代码只能在受限执行器内运行：无网络、无文件写、无 subprocess、无 env 访问（INV-4）；④ `audit leak` 能从轨迹与 prompt 重建「agent 可见信息集合」并对 6 类泄漏结构化判定（INV-2/3/5）；⑤ 蜜罐 canary 与假字段一旦泄漏 100% 被抓到（G4）。

## 2. 范围

### 2.1 范围内

- `dice_ratchet/sandbox/projection.py`：`OBSERVATION_SCHEMA` 注册表、字段级白名单投影、`obs_digest`、投影协议版本与 CHANGELOG 一致性校验。
- `dice_ratchet/sandbox/api.py`：`SandboxEnv` 门面（agent 侧唯一入口），私有持有 MOD-01 引擎，不暴露 `FullState`。
- `dice_ratchet/sandbox/guard.py`：import 门禁（静态 AST + 运行期 import hook + 子进程边界）、harness 代码受限执行（AST 白名单+黑名单）、资源与超时限制。
- `dice_ratchet/sandbox/audit.py`：可见性重建器、规则扫描器、`leak-report` 输出 schema（CLI 由 MOD-09 暴露）。
- `dice_ratchet/sandbox/honeypot.py`：canary/假字段注入、触发监控、P0 告警、自测用例生成。
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
| FR-02-9 | `check_imports()` 扫描 L1–L3（`agent/memory/loop/evolve/evals/inference`）中任何 `import dice_ratchet.env...` / `from dice_ratchet.env import ...`，命中即 `E_ENV_IMPORT_DENIED`。 |
| FR-02-10 | `install_agent_import_hook()` 安装不可卸载的 `MetaPathFinder`，按**调用方模块路径**判定：调用栈中发起 import 的帧属于 `dice_ratchet.{agent,memory,loop,evolve,evals,inference}` 时拒绝 `dice_ratchet.env.*` 并抛 `E_ENV_IMPORT_DENIED`；`dice_ratchet.sandbox` 自身的导入放行。另外，`SandboxEnv` 模块在安装 hook 之前预先 import env。 |
| FR-02-11 | 子进程边界 `run_agent_tick()`：`[sys.executable, "-I", "-S", "-c", shim]`，清空 `PYTHONPATH`、cwd 只读、stdin/stdout 仅 JSON 协议、超时即 kill；供不配合 hook 的第三方代码。 |
| FR-02-12 | `analyze_code()` 采用**节点黑名单 + 调用白名单**：<br>• 禁止的节点：§6 `BLOCKED_NODES`（`Import/ImportFrom` 仅放行 `tool_import_allowlist` 内的模块）。<br>• 禁止的调用、模块与属性：§6 `BLOCKED_CALLS`、`BLOCKED_MODULES`、`ATTR_DENY`（任何 `__*__` 属性）。<br>• **运算符、比较、布尔、切片、推导式等表达式节点一律允许**（v1.0 的节点白名单漏掉了 `Add/Eq/And/USub…` 这些运算符节点，任何写了 `a + b` 的合法工具都会被拒）。<br>返回全部违规的行号与错误码。 |
| FR-02-13 | `gate(artifact_dir)` 对 `harness/tools/*.py` 逐个 `analyze_code`，任一失败整批拒绝（fail-closed），写 `runs/<run_id>/audit/harness-gate.json`，旧 harness 保持可用。 |
| FR-02-14 | **常驻子进程 worker**（Spec B-20）：`load_harness_tools(harness_dir) -> ToolWorker` 启动 `[sys.executable, "-I", "-S", worker.py]`：<br>• `RLIMIT_AS/RLIMIT_CPU/RLIMIT_NOFILE` 只设在**子进程**上。<br>• 工具代码 exec 进 `guarded_globals()`。<br>• 父子进程之间按行传 JSON，请求为 `{tool, obs, candidates, memory}`。<br>• 每次调用有墙钟 `timeout_s`，超时就 `SIGKILL` 并重启 worker，抛 `E_ENV_TIMEOUT`；超出资源限制时 worker 退出并报 `E_ENV_RESOURCE`。调用方都降级为 MOD-04 fallback。<br>主进程持有模型，**绝不**设置 rlimit（v1.0 在进程内设 `RLIMIT_AS=256MB`，会直接杀掉持有 5～19 GB 权重的主进程）；Python 线程无法强杀，所以不使用线程超时。 |
| FR-02-15 | 受限执行期监测逃逸：代码触碰 `_HONEYPOT_*` 命名或 honeypot ledger 即产生 `E_ENV_ESCAPE` P0 事件（sentinel 属性触发）。 |
| FR-02-16 | `rebuild_visible_set()` 从 episodes JSONL + prompt/memory 文本重建 `VisibleSet`：`obs_paths`、`obs_digests`、`memory_texts`、`harness_version`、`seeds_seen`。 |
| FR-02-17 | 扫描 6 类：`seed_leak`、`future_room_leak`、`shop_content_leak`、`full_state_field_leak`、`hidden_field_name_leak`、`canary_leak`，每类输出 finding 明细。**⚠️ 2026-10-14 修订（RULESET_VERSION 1.1.0 / F58）**：`future_room_leak` 与隐藏 token 是**逐局作用域**——某局只用**本局**的 `hidden − shown`；`memory/` 全局库与 `events.jsonl` 这类跨局出口用 `unscoped`（= 任何一局都从未合法展示过）。 |
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
# dice_ratchet/sandbox/projection.py —— INV-2 唯一权威登记表
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

# dice_ratchet/sandbox/api.py —— 唯一 agent 侧入口；内部 engine 为 MOD-01 实例，不对外暴露
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

# dice_ratchet/sandbox/guard.py
class GuardViolation(Exception):
    def __init__(self, code: str, message: str, *, lineno: int | None = None, col: int | None = None) -> None: ...

# 黑/白名单（集合按字母序维护；命中 → E_ENV_CODE_* / E_ENV_AST_DENIED）
BLOCKED_CALLS = frozenset({"eval","exec","compile","__import__","open","input","globals","locals","vars","dir","getattr","setattr","delattr","breakpoint","memoryview"})
BLOCKED_MODULES = frozenset({"os","sys","io","socket","subprocess","shutil","pathlib","importlib","pickle","marshal","ctypes","multiprocessing","threading","requests","urllib","http","dice_ratchet.env","dice_ratchet.sandbox"})
BLOCKED_NODES = frozenset({"Import","ImportFrom","While","Try","With","AsyncFunctionDef","AsyncFor","AsyncWith","ClassDef","Lambda","Yield","YieldFrom","Await","Global","Nonlocal","Delete","Raise"})
# v1.1：只用 BLOCKED_NODES 黑名单 + BLOCKED_CALLS/ATTR_DENY；节点白名单 ALLOWED_NODES 已废弃（漏了 Add/Eq/And/USub 等运算符节点），原列表仅供对照：
# ALLOWED_NODES = frozenset({"Module","FunctionDef","arguments","arg","Return","Assign","AnnAssign","AugAssign","If","For","Expr","Pass","Dict","List","Tuple","Set","Constant","Name","Load","Store","BinOp","BoolOp","UnaryOp","Compare","Call","keyword","Subscript","Slice","Attribute","comprehension","ListComp","DictComp","SetComp","GeneratorExp","IfExp","JoinedStr","FormattedValue","Starred"})
ATTR_DENY = ("__",)      # 任何以 __ 开头的属性名（dunder 逃逸）
GUARDED_BUILTINS: frozenset[str] = frozenset({"len","range","sorted","min","max","sum","abs","round","int","float","str","bool","list","dict","tuple","set","enumerate","zip","reversed","any","all","isinstance","print","ValueError","TypeError","KeyError","IndexError","ZeroDivisionError"})

@dataclass(frozen=True)
class GuardFinding: code: str; lineno: int; col: int; detail: str
@dataclass(frozen=True)
class GuardReport: ok: bool; findings: tuple[GuardFinding, ...]; n_imports: int; scanned_files: tuple[str, ...]

def check_imports(paths: Iterable[Path], *, roots: tuple[str, ...] = ("dice_ratchet.env",)) -> GuardReport: ...
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

# dice_ratchet/sandbox/audit.py
RULESET_VERSION: str = "1.1.0"   # ⚠️ 2026-10-14 修订：1.0.0 为历史版本；1.1.0 起 future_room/隐藏 token 逐局作用域 + 数字前导边界含 "."（F58/F59）
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
def check_ruleset_pin(pin: str, *, source: str = "sandbox.audit.ruleset_pin") -> str: ...   # 不符 → E_ENV_AUDIT_RULESET
def run_audit(run_dir: Path, *, ruleset: str = RULESET_VERSION, split: str = "train",
              config_dir: Path | None = None, surfaces: Sequence[str] = SURFACES,
              evidence_chars: int = 12, self_test: bool = True, replay: bool = True,
              generated_at: str | None = None,
              forbidden_extra: Mapping[str, str] | None = None,
              seed_splits: Sequence[str] = DEFAULT_SEED_SPLITS) -> AuditResult: ...
def write_report(result: AuditResult, out: Path) -> Path: ...     # 0600
def audit_to_console(result: AuditResult) -> int: ...             # 0/1/2

# dice_ratchet/sandbox/honeypot.py
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

`runs/<run_id>/audit/leak-<scope>.json` → `dice_ratchet/sandbox/audit/report.schema.json` 顶层字段：
`run_id` / `generated_at`(iso) / `ruleset_version` / `scope="leak"` / `verdict∈{clean,warn,critical}`（critical 优先级：canary 泄漏或蜜罐失效 > 其余）/ `scanned{episodes,steps,memory_entries}` / `visible_set{obs_paths[],obs_digests[],harness_version}` / `findings[]{leak_class,surface,episode_id,step,token_class,evidence,line_ref}` / `leak_counts{}` / `honeypot{injected,triggered,caught_rate}`。示例：
`{"run_id":"4f2a1c9e-9d1c4e77","generated_at":"2026-09-30T12:00:00Z","ruleset_version":"1.1.0","scope":"leak","verdict":"critical","scanned":{"episodes":60,"steps":1840,"memory_entries":42},"visible_set":{"obs_paths":["schema_version","turn","phase"],"obs_digests":["sha256:..."],"harness_version":7},"findings":[{"leak_class":"seed_leak","surface":"prompt","episode_id":"e-000123","step":14,"token_class":"seed","evidence":"...2013...","line_ref":"prompts/e-001042-g003-train.txt:37"}],"leak_counts":{"seed_leak":1},"honeypot":{"injected":4,"triggered":0,"caught_rate":1.0}}`（**⚠️ 2026-10-14 修订：示例中的 `ruleset_version` 同步为当前的 `1.1.0`**）

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
运行期: import dice_ratchet.env（sandbox 预导入）→ install_agent_import_hook() → L1+ 帧发起 find_spec("dice_ratchet.env.*") → GuardViolation(E_ENV_IMPORT_DENIED)
不配合: run_agent_tick() → [python -I -S -c shim] ←JSON→ 超时 kill
# harness 准入与受限执行
gate(harness/tools) → 全通过? 否 → 整批拒绝(E_ENV_AST_DENIED)，保留旧版本
  是 → load_harness_tools() 启动子进程 worker(exec 进 guarded_globals()，子进程 rlimit) → worker.call(超时即 kill + 重启)
       → E_ENV_TIMEOUT|E_ENV_RESOURCE → MOD-04 规则 fallback（该局标注降级，不计入评估样本）
# 审计 + 蜜罐
rebuild_visible_set(run_dir) → VisibleSet；对每个 prompt/log/memory：
  scan_text(forbidden = seeds ∪ future_room_ids ∪ shop_items ∪ hidden_field_names ∪ canary)
  # ⚠️ 2026-10-14 修订（RULESET_VERSION 1.1.0 / F58）：future_room/hidden token 按**出口所属的局**
  #   取该局的 hidden − shown；跨局出口（memory/ 全局库、events.jsonl）用 unscoped（任何一局都从未合法展示过）
  honeypot.scan(text)                      # 字节级匹配，无正则
→ findings → verdict；canary 漏判或蜜罐自测失败 ⇒ "critical"
```

## 9. 配置项（键名、类型、默认值、含义）

| 键名 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `sandbox.policy_failure_streak` | int | `3` | 连续 `reject(final=True)` 的次数，达到即判 `policy_failure`（FR-02-8b） |
| `sandbox.audit.ruleset_pin` | str | `1.1.0` | **规则集版本钉扎**（Spec B-86，2026-10-14）：必须 == 代码常量 `sandbox/audit.py::RULESET_VERSION`，否则 `audit leak` **fail-closed**（`E_ENV_AUDIT_RULESET`，守卫轴退出码 4）。它**入报告**（`ruleset_version` = 该常量），是「这份 clean 按哪版规则判的」的唯一凭据（NFR-02-3）⇒ **不可被配置伪造**：配置只作声明，代码常量说了算 |

**⚠️ 2026-10-14 清表（Spec v2.30 / B-87，F75 裁决：按 F73 同一套规矩逐个核）**：本表原有 **15 个键，实测只有上表 2 个有消费者**，其余 **13 个整体删除**——它们**不是「忘了接线」，而是「本就不该是可配项」**：

| 删掉的键 | 为什么不该配 |
|---|---|
| `sandbox.guard.enforce` | `enforce`/`warn` 两档里的 `warn` = 让静态扫描失败的 harness 照样进冒烟/heldout ⇒ **直接削弱 INV-4**（自编辑代码只在受限执行器内运行）。该档从未实现，也不该实现 |
| `sandbox.guard.scan_roots` | INV-1 的扫描面是**分层结构**（L1–L3 六个模块），权威是 `guard.py::ENV_ROOTS` 常量；改它等于改架构，不是调参 |
| `sandbox.guard.process_boundary` | 工具执行的子进程边界已经在 `ToolWorker` 上（`load_harness_tools` 起 worker）；该键指的是另一条 payload 边界 `run_agent_tick()`，**无生产调用点** ⇒ 键无法接线而不改设计（另登记 **F76**） |
| `sandbox.honeypot.enabled` | 注入是**检测不变式**（G4：canary/假字段一旦泄漏 100% 被抓到）。做成开关 = 给不变式留一个静默关闭点；只有测试需要关，它们直接传 `SandboxEnv(honeypot=False)` |
| `sandbox.honeypot.plan_path` | `configs/honeypot.yaml` 的实际消费者是**测试夹具**（`load_plan()` 校验 4 个注入点与实现一致），运行时不读；文件保留并标注 `[契约]/[占位]`，键删除 |
| `sandbox.honeypot.self_test` | 该旋钮在 **CLI**（`audit leak --no-self-test`）与 `run_audit(self_test=)` 上，配置键冗余 |
| `sandbox.projection.mode` | `strict` 是生产唯一档；`assert` 是**调试/测试档**，golden 与 §11 用例**显式传参**（`SandboxEnv(projection_mode="assert")`），不经配置 |
| `sandbox.projection.schema_version` | 投影协议的版本身份**必须是代码常量**（`projection.py::OBSERVATION_SCHEMA_VERSION`，FR-02-5 由 CI 对 CHANGELOG 末行）——与 `audit.ruleset_pin` 同理：可追溯性要求它**不可被配置伪造** |
| `sandbox.render.max_tokens_p95` | `render_compact(obs)` 不收预算参数，**全库没有任何断言读它** ⇒ 纯装饰。FR-02-3b 的「p95 ≤ 1500 token（8B 分词器）」是**设计目标**（CI 无该分词器），留在 §4/NFR 里，不做成键 |
| `sandbox.exec.timeout_s` / `mem_limit_mb` / `cpu_limit_s` / `max_tools` | 这四个是 `ToolWorker` 的 **containment 参数**（`RLIMIT_AS`/`RLIMIT_CPU`/墙钟/工具数），是 INV-4 的一部分而**不是策略**——让配置（乃至可演化的 harness）抬高自己的沙箱上限正是 INV-4 要防的事。权威落 `guard.py::ToolWorker` 的默认参数（`2.0 / 256 / 2 / 32`），由用例 `test_b87_tool_worker_limits_are_code_constants` 钉住数值 |


**审计的触发方式是显式 CLI**（`python -m dice_ratchet audit leak --run-id <id>`），**不存在「每代结束自动审计」**：`loop/`、`evolve/`、`evals/` 里没有任何 `run_audit` 调用点，正式实验也是跑完之后手工补一刀（`scripts/run_m3_formal.sh`）。v1.0 的配置表在这里写过 `sandbox.audit.enabled` =「每代结束自动审计」，但**从来没有 FR 支撑**（FR-02-16..19 全是「给定 run 目录 → 扫描」）；该键于 2026-10-14 **删除**（F74 裁决：认定为文档错误）。

**⚠️ 2026-10-14 整块收拢（Spec v2.29 / B-86，F73 裁决 ②）**：上表原有的 `sandbox.audit.{enabled, surfaces, fail_on, evidence_chars, ruleset}` **五个键一并删除**——实现侧只把它们做成了 `run_audit()` 的**参数 + 同名默认值**，配置→调用的接线从未发生（全库 0 消费者；该 yaml 块自 M0 骨架 `5d3574c` 建立后**从未被改过一次**，B-82 把规则集 1.0.0→1.1.0 时也没人想起改它）。其中 `fail_on` 的分级诉求（§16 开放问题 3）已裁决为「退出码按 §18.2 的两根正交轴恒定，不做配置化」。

环境变量前缀 `DICERATCHET_SANDBOX_*`；优先级 CLI > 环境变量 > `configs/default.yaml`（Spec §5）。

## 10. 错误处理与降级（使用 Spec §8 的错误码前缀）

| 错误码 | 触发 | 处理/降级 |
|---|---|---|
| `E_ENV_PROJECTION_MISSING` | 非 optional 字段源缺失 | 终止该局，不返回部分观测 |
| `E_ENV_PROJECTION_VERSION` | CHANGELOG 末行 ≠ 注册表版本 | CI 失败（运行时不读文档） |
| `E_ENV_SCHEMA` | `mode="assert"` 命中隐藏字段、类型不符、观测校验失败 | 抛错；golden/CI 直接红 |
| `E_ENV_IMPORT_DENIED` | agent 侧 import `dice_ratchet.env.*` | 静态：CI 失败；运行期：终止该 tick + P0 事件 |
| `E_ENV_AST_DENIED` / `E_ENV_CODE_SYNTAX` | 黑名单节点/调用/属性；AST 解析失败 | 拒绝该文件；`gate()` 整批拒绝并保留旧 harness |
| `E_ENV_TIMEOUT` | 工具调用超墙钟 | 该次调用失败 → MOD-04 fallback，不判该局失败 |
| `E_ENV_RESOURCE` | 内存/CPU 超限 | 同上；连续 3 次则该 harness 版本标 `degraded` |
| `E_ENV_ESCAPE` | 受限执行触及蜜罐 sentinel | P0 事件 + 立即卸载该 harness 版本，人工确认 |
| `E_ENV_AUDIT_IO` | 轨迹缺失/JSONL 损坏/权限不足 | 判 `critical`（缺证据 ≠ 无泄漏），输出可读原因 |
| `E_ENV_AUDIT_RULESET` | **规则集版本钉扎不符**（`sandbox.audit.ruleset_pin` ≠ `RULESET_VERSION`，或 `run_audit(ruleset=…)` 收了别的值） | **fail-closed 拒绝审计**（不产出报告）：守卫轴退出码 **4**；消息同时打印两个版本与修法（Spec B-86） |
| `E_ENV_HONEYPOT` | 自测命中率 <100%，或注册表含蜜罐字段 | CI 红；`audit leak` 恒判 `critical` |

原则：隔离类错误 **fail-closed**（拒绝执行）；能力类错误 **fail-soft**（退回 fallback，该局标注降级且不计入评估样本，遵循 Spec §8.5）。

## 11. 测试计划（单测/集成，用「给定-当-则」写验收用例）

单测（「给定-当-则」，路径 `tests/unit/sandbox/test_sandbox_{projection,api,guard,audit,honeypot}.py`）：
- 给定 同一 `FullState`，当 `project_state` 调两次，则两次 canonical JSON 与 `obs_digest` 完全相同（FR-02-1）。
- 给定 源含未登记字段 `hidden_shop_preview`，当 `mode="strict"`，则输出无该字段、`dropped_hidden==1`、日志文本不含该字段名（FR-02-2）。
- 给定 seed/后续房间/商店条目/RNG 状态/未来骰面各替换为唯一哨兵值，当 投影后逐字段比对，则任一哨兵出现在 obs 即失败（FR-02-3）。
- 给定 注册表加字段却不改版本，且源 `schema_version="9.9.9"`，当 import projection / 执行投影，则分别抛 `E_ENV_PROJECTION_VERSION`、obs 取注册表版本（FR-02-5/6）。
- 给定 含 `from dice_ratchet.env.engine import Engine` 的假 L1 文件 + 已装 import hook，当 `check_imports` / `import dice_ratchet.env.rng`，则 `ok=False`（带行号）、抛 `E_ENV_IMPORT_DENIED`（FR-02-9/10）。
- 给定 8 个恶意片段（`open("/etc/passwd")`、`import socket`、`__import__("os")`、`eval`、`exec`、`getattr(x,"_np_secret_price")`、`while True`、`class X`），当 `analyze_code`，则全部被拒且码/行号正确（FR-02-12）。
- 给定 合法 `rank_actions`（纯函数，使用 `+ - * //`、比较、`and/or/not`、推导式、`sorted/len/range/max/min`），当 `gate` + `worker.call`，则通过并返回正确重排（FR-02-12/14，**正例必测**）。
- 给定 `time.sleep(5)` 与申请 1 GB 的两个工具，当 `worker.call(timeout_s=0.2)`，则分别抛 `E_ENV_TIMEOUT`（worker 被 kill 并自动重启，下一次调用正常）与 `E_ENV_RESOURCE`；主进程 RSS 与 rlimit 不变（FR-02-14）。
- 给定 连续 3 个 `reject(final=True)`，当 第 3 次调用，则 `truncated=True`、`truncation_reason="policy_failure"`；中间插入一次成功决策则计数清零（FR-02-8b）。
- 给定 `legal_actions()`，则不含 `abandon_run`，其余元素与顺序同 MOD-01（FR-02-8c）。
- 给定 每个 canary/假字段各注入 prompt、memory、log 三类文本，当 `honeypot.self_test()`，则 12/12 命中、`missed==[]`（FR-02-23，G4）；给定 无 canary 的正常 prompt，当 `honeypot.scan`，则 0 命中（无假阳性）；给定 含 train seed 值且该 seed 未出现在 obs 的 synthetic prompt，当 `run_audit`，则 `verdict="critical"`、`leak_class="seed_leak"`、`surface="prompt"`（FR-02-17/19）。

Golden `tests/golden/sandbox/`：给定 `src/game/tests/golden/g5_hard_six_stage.jsonl` 的动作脚本，当 经 SandboxEnv 重放，则 agent 侧 `obs_digest` 序列与 `tests/golden/sandbox/g5_obs_agent.txt` 逐字节相同、`render_compact` 输出与 `tests/golden/sandbox/g5_render.txt` 相同（G2/G3）；给定 clean run，当 审计，则报告除 `generated_at` 外与 `tests/golden/sandbox/leak-report-clean.json` 相同（NFR-02-3）。

集成 `tests/integration/`：给定 含 canary 的 fake run，当 `python -m dice_ratchet audit leak --run-id <id>`，则退出码 2、`verdict="critical"`、`leak_counts.canary_leak>=1`；给定 干净自测 run，当 审计，则退出码 0、`findings==[]`、`honeypot.caught_rate==1.0`（G4）；给定 JSONL 损坏一行，当 审计，则 `E_ENV_AUDIT_IO` 且判 `critical`；给定 `FullState` 字段清单快照变更，当 `test_env_sandbox_contract.py`，则注册表漂移被捕获。

## 12. 验收标准（DoD，对齐 Spec §9 质量门 G1–G6）

对齐 Spec §9（G1–G6）与 §7（M0：同一 seed 两次 `state_hash` 序列一致；交付可复现模拟器 + 观测投影 + 审计器 + 单测）。

| 门 | MOD-02 判据 |
|---|---|
| G1 单测 | 5 个模块行覆盖 ≥80%；§11 单测全绿 |
| G2 Golden | 固化基于 g5 默认局的 agent 观测摘要、紧凑渲染与 clean 审计报告；改动需 PR 说明 + 显式更新 |
| G3 确定性 | 同 seed 双跑 `obs_digest`/`state_hash` 序列逐字节相同（CI diff） |
| G4 泄漏审计 | 自测 run `audit leak` 0 告警；12/12 蜜罐用例被抓到；故意注入 canary 100% 触发 P0 |
| G5 Schema | `Observation`、`report.schema.json`、`harness-gate.json` 有 schema + round-trip 测试 |
| G6 命令 | 可直接复制：`python -m dice_ratchet env selftest --seed 1`、`python -m dice_ratchet audit leak --run-id <run_id>`、`pytest tests/unit/sandbox/test_sandbox_projection.py -q`、`./scripts/ci_gate.sh`（门禁总入口） |
| M0 交付 | `SandboxEnv` 可用且不暴露 `FullState`；L1 import env 的 CI 门禁上线（`./scripts/ci_gate.sh` + `.github/workflows/ci.yml`，见 [2026-10-03 M0 验收报告](../reviews/2026-10-03-M0验收报告.md) F2）；`OBSERVATION_CHANGELOG.md` 写入 1.0.0 |

**与不变量逐条对应**：

| 不变量 | MOD-02 的落实点 | 验证方式 |
|---|---|---|
| INV-1 agent 只能经 `SandboxEnv.observe/step` 触达环境；L1+ 禁 import `dice_ratchet.env.*` | FR-02-7（门面唯一入口、无 FullState 出口）、FR-02-9/10/11（静态 + import hook + 子进程）；`dice_ratchet.env` 在 `BLOCKED_MODULES` | 假 L1 文件门禁单测、import hook 单测、`sandbox_import_denied` 事件 |
| INV-2 `Observation` 是白名单投影，新字段必须登记 | FR-02-2/4/5/6、§6 `OBSERVATION_SCHEMA` 与 JSON、FR-02-24 | 哨兵替换测试、未登记字段丢弃测试、CHANGELOG 版本校验测试 |
| INV-3 进入 prompt 的上下文仅限当前 Observation + memory + harness + 任务说明 | 审计器对 `prompt` 面重建可见集合（FR-02-16/17/18），检测 prompt 中出现非 obs 的 seed/隐藏字段；`obs_paths` 来自 `OBSERVATION_SCHEMA` 与轨迹求交 | 集成：synthetic seed 泄漏 → 退出码 2；`audit_leak` 事件 |
| INV-4 harness 自编辑代码受限执行：无网络/无文件写/无 subprocess/无 env 访问 | FR-02-12（AST 黑/白名单：`import`/`open`/`subprocess`/`socket`/`__import__`/`eval`/`exec`/`getattr`）、FR-02-13（gate fail-closed）、FR-02-14（超时/内存/CPU）、FR-02-15（逃逸监测）；MOD-07 侧写入动作配合 | 8 个恶意片段拒收单测、超时/资源单测、`E_ENV_ESCAPE` P0 测试、`sandbox_harness_gate` 事件 |
| INV-5 train/heldout/test seed 隔离，heldout/test seed 值不得进入 prompt 或记忆 | `VisibleSet.seeds_seen` 只收 `split=="train"`（FR-02-16）；`seed_leak` 扫描面含 `prompt`/`memory`/`log`（FR-02-17/18），heldout/test seed 值入 forbidden 集合 | 集成：heldout seed 出现在记忆文本 → `verdict="critical"`、`leak_counts.seed_leak>=1` |
| INV-6/INV-7（确定性；主指标用环境原生结果） | 不在 MOD-02 责任范围（MOD-01/09、MOD-08 保证）；MOD-02 只保证 `obs_digest` 确定性（NFR-02-1）为其提供可复现证据 | 同 seed 双跑 digest 一致（G3） |

## 13. 指标与埋点

结构化事件（Spec §8.2 命名，`infra/logging.py` 落地）：`sandbox_projection`（fields/dropped_hidden/duration_us）、`sandbox_import_denied`（module/path/lineno）、`sandbox_harness_gate`（files/ok/findings）、`sandbox_exec`（tool/duration_ms/outcome∈{ok,timeout,resource,error}）、`audit_leak`（verdict/leak_counts/ruleset_version）、`audit_honeypot_triggered`（surface/episode_id/step/canary_hash）、`audit_honeypot_selftest`（cases/caught）。

入档指标（`runs/<run_id>/metrics/`）：`sandbox_dropped_hidden_total`、`sandbox_projection_us_p95`、`sandbox_exec_timeout_rate`、`sandbox_gate_reject_rate`、`leak_count_by_class`、`honeypot_caught_rate`。硬约束：`honeypot_caught_rate` 恒为 `1.0`、`canary_leak==0`；任一违反即 `verdict="critical"` 并阻断该代际接受（MOD-07）。

## 14. 依赖与风险

依赖：MOD-01 的 `FullState` 字段契约与 `state_hash()`；MOD-09 的 `infra/logging.py`、CLI、`guards.py` CI 作业、`DICERATCHET_*` 配置加载；MOD-04/07 消费工具注册表与门禁结果。

| 风险 | 影响 | 对策 |
|---|---|---|
| `getattr` 黑名单使合法工具无法表达，或动态逃逸绕过静态扫描 | 误杀/漏杀 | 静态门禁 + 运行期受限 builtins + 蜜罐 sentinel 三重；放宽白名单必须附测试与 PR 说明 |
| macOS 上 `RLIMIT_AS` 不完全生效 | 资源限制虚设 | 以墙钟/子进程超时为第一道；内存限制为尽力而为，测试用 CPU 密集与墙钟超时用例兜底 |
| 子进程边界启动开销与协议复杂度 | 性能与实现成本 | 工具执行走 `ToolWorker` 子进程（默认路径）；另一条 payload 边界 `run_agent_tick()` **当前无生产调用点**（F76）。~~默认 `process_boundary=false`~~ **⚠️ 2026-10-14（B-87）：该配置键已删除**——它不是可配项，是否启用某条边界是**设计选择**，不是运行期开关 |
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

**实际交付（M0）**：M0.1–M0.6 于 **2026-10-03** 由单个提交 `e35279e` 交付（投影/隔离/审计/蜜罐 + golden + 单测）。
本项目**未单独记工**，「实际人日」无法从仓库证据还原；按提交时间窗折算 ≤ 1 人日，与估算 18.0 人日的偏差 ≥ 90%，已在 [排期 §8](../项目排期-DiceRatchet.md) 记录。
M0.6 的「CI 门禁接线」在 2026-10-03 验收时缺失（验收报告 F2），同日补齐为 `scripts/ci_gate.sh`（平台中立，本地与 CI 同一份）+ `.github/workflows/ci.yml`（gate / parity 两个作业）。
证据见 [M0 验收报告](../reviews/2026-10-03-M0验收报告.md)；M1 起按排期 §7 逐任务记录实际人日。
## 16. 开放问题

1. **已裁决（Spec v1.4 B-24）**：全状态只由 `SandboxEnv._engine` 私有持有，`dice_ratchet.env` 列入 `BLOCKED_MODULES`，对 L1+ 不可导入。由于 MOD-01 v1.0 改为移植 GAME 内核，原 `build_full_state()` 方案已不再适用，改用这种等价封装；适配层归 MOD-02（`sandbox/api.py`）。`OBSERVATION_SCHEMA` 与 `FullState` 字段清单的契约测试落在 `tests/integration/test_env_sandbox_contract.py`，双方 PRD 各引用一次。
2. **已裁决（Spec v1.4 B-25）**：审计器只做黑名单扫描 + canary 探针，不做「prompt 中每个数值都必须归因到 `VisibleSet`」的正向校验（噪声大）；若 M1 后仍出现未知泄漏再升级。
3. **待定**：canary 是否按 `run_id` 派生（`reset_canary(salt=run_id)`）？跨 run 复用便于测试，但可能被 harness 长期记住；倾向按 run 派生。（原文此处的另一半——「`sandbox.audit.fail_on` 是否允许 `warn`（本地调试）需 MOD-09 确认，CI 建议恒为 `critical`」——**已于 2026-10-14 裁决并结案**，见下方第 5 条。）
4. **已结案（Spec v1.3 P-08 → B-10）**：`phase="shop"` 时，陈列随 `obs-agent-1.0.0` 的 `shop` 字段给出，`legal_actions()` 直接带 `itemId`，不再需要单独的 `shop_items` 扩展。
5. **已裁决（2026-10-14，Spec v2.29 B-86；F73/F74）**：`sandbox.audit.fail_on` 的「是否允许 `warn`（本地调试）」**不再是一个配置问题**——`audit leak` 的退出码按 §18.2 的两根正交轴**恒定**（结论轴 0/1/2、守卫轴 4），配置文件不得让它漂移；该键与 `enabled`/`surfaces`/`evidence_chars`/`ruleset` 一并删除，整块收拢为一个**钉扎** `sandbox.audit.ruleset_pin`（与代码常量不符即 fail-closed，见 §9/§10）。若将来真需要本地放宽，落点应是**CLI flag**（例如 `audit leak --fail-on warn`）而不是配置项。

---

**变更记录**：v1.0（草案）· 首版：投影注册表、门禁与受限执行、审计规则集、蜜罐自测、INV-1..INV-5 对应表。

## 17. 变更记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-09-30 | 首版 |
| v1.1 | 2026-09-30 17:21 | 按 Spec v1.3 与 MOD PRD 评审修订：投影源改为 GAME `observe`，产出 `obs-agent-1.0.0`，新增紧凑渲染（P0-1）；新增 `reject(final)` 与唯一判负 `policy_failure`（P0-5）；工具执行改为常驻子进程 worker，主进程不设 rlimit（P0-6）；AST 改为节点黑名单 + 调用白名单，放行运算符（P0-6）；import hook 按调用方判定（P0-6）；蜜罐注入点改为 GAME 的隐藏位置；canonical JSON 统一为 `ensure_ascii=True`（P1-18）；CHANGELOG 校验移到 CI；函数名对齐 Spec §4.7（P1-16）；`min:N` 改为 GAME DSL |
| v1.2 | 2026-09-30 17:49 | P-06/P-07 裁决回填为 Spec B-24/B-25（全状态封装按 MOD-01 v1.0 改为等价形式）；开放问题 4 按 B-10 结案 |
| v1.2.1 | 2026-10-03 | 追加 §18 实现落地记录（代码对照 + 8 处实现裁决）；不改任何 FR 语义 |

---

## 18. 实现落地记录（2026-10-03，代码对照）

M0 的五个交付文件已全部落地并有单测/集成/golden 覆盖；节号与 §2.1 一一对应。

| 文件 | 落地内容 | 对应 FR |
|---|---|---|
| `dice_ratchet/sandbox/projection.py` | `OBSERVATION_SCHEMA`（34 字段，含 4 个 agent 专属键）、`project_state`、`canonical_json`/`obs_digest`、`validate_observation`、`render_compact`、`changelog_version`/`check_changelog` | FR-02-1..6、3a、3b、24 |
| `dice_ratchet/sandbox/api.py` | `SandboxEnv`（私有 `_engine`/`_state`、`__getattr__` 拒绝越权属性）、`EpisodeConfig`/`StepResult`、`reject(final)` 与 `policy_failure` 唯一判负、`max_env_steps`/`max_decisions` | FR-02-7、8、8a、8b、8c |
| `dice_ratchet/sandbox/guard.py`（+ 私有 `_worker.py`） | `check_imports`、`analyze_code`（节点黑名单 + 调用白名单）、`gate`（fail-closed + `harness-gate.json`）、`install_agent_import_hook`、`guarded_globals`、`run_agent_tick`、`ToolWorker`/`load_harness_tools` | FR-02-9..15 |
| `dice_ratchet/sandbox/audit.py` | `rebuild_visible_set`、6 类 `LeakClass`、`scan_text`、轨迹重放补齐 future_room/shop 规则集（**配表按快照 `content_hash` 反查，见 `_resolve_content_dir`**）、`check_ruleset_pin`、`run_audit`/`write_report`/`audit_to_console`、`register_sensitive` | FR-02-16..19 |
| `dice_ratchet/sandbox/honeypot.py` | 4 个注入点（`injected_copy` 只复制被触碰容器）、`scan`（字节级 `find`）、`self_test`（12/12）、`reset_canary` + ledger（只存指纹） | FR-02-20..23 |
| `schemas/{observation,audit_report,harness_gate}.schema.json`、`docs/prd/OBSERVATION_CHANGELOG.md`、`configs/honeypot.yaml` | G5 schema + FR-02-4/5 的版本账本 + 注入计划（与实现不一致即 `E_ENV_HONEYPOT`） | FR-02-4、5、21 |

G6 可复制命令（均已实测）：

```bash
python -m dice_ratchet env selftest --seed 1
python -m dice_ratchet audit leak --run-id <run_id>
python -m dice_ratchet guards check --root dice_ratchet
pytest tests/unit/sandbox/test_sandbox_projection.py -q
pytest tests/unit/sandbox tests/golden/sandbox tests/integration -q
```

G1 覆盖率（`--cov=dice_ratchet.sandbox`）：`projection` 90% / `audit` 83% / `honeypot` 90% /
`guard` 87% / `api` 92%（均 ≥80%）；`_worker.py` 是私有子进程入口，79%，其真实路径由
`ToolWorker`/`run_agent_tick` 的子进程用例覆盖（子进程内不进覆盖率统计）。
G2 golden：`tests/golden/sandbox/{g5_obs_agent.txt,g5_render.txt,leak-report-clean.json}`，
由 `tests/golden/sandbox/regen_sandbox_golden.py` **显式**重生成（212 步摘要）。

### 18.1 实现裁决（不改 FR 语义，逐条可复核）

1. **`audit leak` 退出码为 0/1/2/4**：`0/1/2` 是泄漏结论轴（FR-02-19 与 §11 集成用例），
   `4` 是"审计器自身守卫违例"轴（Spec §8.3，与 `infra.errors.EXIT_CODE` 登记一致）。
   详见 §18.2；MOD-09 PRD §6.4 的 `0/4` 因此只覆盖了后一根轴，已加注。
2. **`Observation` 是 JSON-native `dict`**：§6 的 `project_state(...) -> tuple[dict, ...]`
   与 §6 的 obs JSON 示例都是普通映射；§7 的「不可变」由 `freeze_observation()`
   （递归 `MappingProxyType`）与 `validate_observation()` 按需提供——递归冻结会破坏
   `deepcopy`/序列化，故不设为默认返回类型。
3. **轨迹行落地归 MOD-06**：`reject()` 不直接写 JSONL（写一行合法轨迹需要
   `episode_key/generation/harness_version/arm/ts`，只有 MOD-06 持有），而是把
   `illegal=True` + `reason` + `reject_stage` 放进 `StepResult.info` 供 MOD-06 单点写行。
4. **`legalActionKinds` 的 `abandon_run` 改写**落在 `projection._REWRITES` 表（§6 的
   `ObsFieldSpec` 没有 `transform` 字段，故不外扩数据类）。
5. **注册表字段集 = §4.2「保留」表的严格实现**：额外排除 `chapterName`/`piles`/`deck`/
   `battleHints`/`battleKind`/`outcome`（token 预算 + §4.2「删除」的语义；终局结果经
   `StepResult.info.outcome` 提供，不进 prompt）。`piles` 被排除还使蜜罐向 `drawPile`
   注入虚构骰时**不改变** `piles.draw` 计数。
6. **schema 落 `schemas/` 而不是 §6 示例的 `dice_ratchet/sandbox/audit/report.schema.json`**：
   `sandbox/audit.py` 已是模块，同名包目录会与模块解析冲突；`schemas/` 是 MOD-09
   登记的跨模块 schema 根（`infra.io.validate` 默认搜索路径）。
7. **seed 规则集默认只扫 heldout/test**：依 `configs/seeds.yaml::exposure_guard.numeric_scan`
   （**F45-2 起**：「3–6 位数字落 **731000-731099 / 947000-947099** 即告警」；此前是
   「4 位落 2000-2099 / 9000-9099」）与 INV-5 的 `expose_to`；需要更严时
   `run_audit(seed_splits=("train","heldout","test"))`。数值 token 要求两侧非字母数字（**⚠️ 2026-10-14 修订（F59，RULESET_VERSION 1.1.0）：前导边界另含 `.`** ⇒ 浮点小数部分 `0.731042` 不再被当 seed；`"reason": 731042`、`947042.5`、`-947042` 仍命中——整数部分相等即 fail-closed 报出）。
   **扫描面（F45-2）**：原先有两张人工死名单（`LOG_NON_CONTENT_KEYS` /
   `MEMORY_NON_CONTENT_KEYS`，扩过四次，用来跳过 `obs_digest`/`ts`/耗时/token 计数/哈希…），
   现已**整删**：六位端口空间不再与耗时（`duration_us=2072.8`）、token 预算
   （`max_tokens: 2048`）、ISO 年份（`2026`）、秒级浮点（`dur_s=3.9022`）撞车，
   「数字串相等即泄漏」重新健全，于是**全字段**都在扫描面内、代码更简单。
   唯一保留的豁免是 `LOG_STRUCTURAL_KEYS = {seed, episode_key, spec}`——框架自己的**播种计划**
   （每条 `episode_end` 行都带本局 seed、`eval.start` 带 `spec.seeds`）：它们是 run 的测量
   元数据而不是模型可见内容，不豁免则每个 run 都会因记录了自己的 seed 被判 critical。
   豁免**只按这三个键**、不做通配；同一 seed 出现在任何其它字段仍会被抓
   （`sandbox` 单测里有正反两例）。
8. **import hook 双层**：`MetaPathFinder`（加载期，§4.10 的字面要求）+ `builtins.__import__`
   包装（缓存命中期）。因为 `SandboxEnv` 按 FR-02-10 预先 import 了环境层，agent 侧
   `import dice_ratchet.env.rng` 会命中 `sys.modules`、绕过 finder；只装 finder 会让
   FR-02-10 与 §11 的「import dice_ratchet.env.rng → 抛 E_ENV_IMPORT_DENIED」无法成立。
9. **`GuardViolation` 继承 `InfraError`**（仍是 `Exception` 子类，符合 §6 签名），
   从而 CLI 直接拿到 Spec §8.3 的退出码 4，无需第二套错误通道。
10. **`_worker.py` 是第 6 个（私有）文件**：常驻工具子进程的入口必须能被
    `[sys.executable, "-I", "-S", <file>]` 直接执行；5 个公开模块仍只用 stdlib +
    `infra.errors`（`pyyaml` 只在读注入计划时惰性导入）。

### 18.2 `audit leak` 的四码退出码：两根正交轴（2026-10-03 评审补充）

一次 `audit leak` 必须同时回答两个**互不替代**的问题，因此退出码是两根轴而不是一根：

| 轴 | 问题 | 取值 | 依据 |
|---|---|---|---|
| 泄漏结论 | **有没有泄漏？** | `0` 无 finding / `1` 仅 medium / `2` 含 critical | FR-02-19、§11 集成用例、§6 verdict 三级 |
| 沙箱守卫 | **审计器自己还可信吗？** | `4`（`machinery_errors` 非空，如 `E_ENV_HONEYPOT`） | Spec §8.3「4 沙箱/守卫违例」；`infra.errors.EXIT_CODE[E_ENV_HONEYPOT/E_ENV_ESCAPE/E_ENV_IMPORT_DENIED/E_ENV_AST_DENIED] = 4` |

**优先级 `4 > 2 > 1 > 0`**，理由：检测器失效时，报告写 `clean` 还是 `critical` 都不能按原义
采信——把蜜罐自测失败报成 `2` 会让 CI 去追一个不存在的泄漏（假警报），而 `E_ENV_HONEYPOT`
在错误码注册表里本来就是 4。报告**同时**保留 `verdict=critical`（FR-02-23：「低于 100%
直接判 critical」）与 `machinery_errors`，信息不丢，只是分通道表达。

归类（谁进哪根轴）：

| 情形 | 轴 | 退出码 |
|---|---|---|
| 干净 run | 结论 | 0 |
| 仅 `full_state_field_leak` / `hidden_field_name_leak`（medium） | 结论 | 1 |
| `seed_leak` / `future_room_leak` / `shop_content_leak` | 结论 | 2 |
| `canary_leak`（canary 真的进了 prompt/memory/log） | 结论 | 2（**是发现，不是守卫违例**） |
| 蜜罐自测 <100%、注册表含哨兵（`E_ENV_HONEYPOT`） | 守卫 | 4 |
| 审计路径上触发 import hook / AST 门禁 / 逃逸（`E_ENV_*`，当前审计本身不执行工具，理论路径） | 守卫 | 4（由 CLI 通用 `except InfraError` 给出，无需 `run_audit` 参与） |
| `E_ENV_AUDIT_IO`（轨迹缺失 / JSONL 损坏 / 权限不足） | 结论 | 2（§10「判 critical」：缺证据 ≠ 无泄漏，处理方式与 critical 相同；码本身在 stdout/报告中显式打印，便于分类。**2026-10-03 评审确认：保留在结论轴，不抬到 4**） |
| `E_ENV_AUDIT_RULESET`（`ruleset_pin` ≠ 代码常量；Spec B-86，2026-10-14） | **守卫** | **4**（审计器**自己声称的规则集版本**不可采信 ⇒ 它写出的任何 verdict 都不能按原义读；在扫盘之前就拒绝，不产出报告。**刻意不占 2**：若与「含 critical 泄漏」同码，运维无法区分「有泄漏」与「配置与代码不一致」——两者要做的处置完全相反） |

`run_audit`/`write_report` 不抛异常表达上面任何一条（只读文件、结论入报告）；`AuditResult.exit_code`
与 `AuditResult.exit_reason` 是 CLI 的唯一出口。
| v?-2026-10-10 | 2026-10-10 | **`audit leak` 按文件形状解析 + 遥测键豁免（F44/F45，对齐 Spec v2.4 B-53/B-54）**。① **F44**：`reflections/<episode_key>.json` 是 MOD-06 写的**多行 pretty JSON**（实测 51 行），而审计对 `*.jsonl` 与 `*.json` 都走 `_read_jsonl` ⇒ 第 2 行起 `Extra data` ⇒ `E_ENV_AUDIT_IO` ⇒ **退出码 2** ⇒ 任何含反思的 `loop run` 都无法让 `audit leak` clean。改为 `.jsonl` 逐行、`.json` 整文档（并下探 `entry` 取正文——反思正文才是泄漏面）。② **F45a**：同一形状问题在**扫描循环**里还有第二处：逐行 `json.loads` 必失败 ⇒ `row=None` ⇒ 既有的 `MEMORY_NON_CONTENT_KEYS` 豁免被**绕过** ⇒ `created_at` 的年份 `2026` 撞 heldout 区间刷 3 条 critical 假阳性；改为文档整份投影后只扫内容字段。③ **F45b**：`LOG_NON_CONTENT_KEYS` 补入 `llm.*` 纯遥测键（`max_tokens`/`temperature`/`top_p`/`cost_estimate`/`request_hash`/`response_hash`/`prompt_digest`）——触发点正是 Spec B-47 刚加的按调用方输出预算 `max_tokens: 2048`（合法配置值落进 2000–2099 即被判成 seed 泄漏）。该死名单此前已被扩过三次 |
| v1.2.2 | 2026-10-11 | **F45-2：删两张人工死名单、改成全字段扫描（对齐 Spec v2.6 B-62）**。① **为什么现在能删**：两张名单（`LOG_NON_CONTENT_KEYS` / `MEMORY_NON_CONTENT_KEYS`）四次扩容（M1 F3 的 136 条、`episode_s`/`cpu_s`/`elapsed_s`、F35 的年份 `2026`、F45b 的 `max_tokens: 2048`）本质上都在绕同一件事：旧 heldout/test 的四位区间（2000–2099 / 9000–9099）与耗时、token 预算、ISO 年份共用一个数字空间。F45-2 把 heldout/test 移到六位端口（731000–731099 / 947000–947099）后，这些字面量**不再可能与 seed 相等**，名单因此变成纯粹的盲区。② **实测（删除的依据）**：去掉全部豁免重扫 → 旧区间在 1311 个归档工件 / 188.0 MB 上命中 **799,868** 次（`ts` 699,305 占 87%、`seed` 44,173、`prompt_tokens` 28…），新区间 **0** 命中。③ **保留的唯一豁免**：`LOG_STRUCTURAL_KEYS = {seed, episode_key, spec}`（框架播种计划）。④ **顺带更正错误论据**：名单注释里「64 位 hex 约 3.7% 概率撞 20XX」**实测为假**（20000 个随机 sha256 命中 0/20000；真实 run 的 `request_hash`/`response_hash`/`prompt_digest` 各 9200 样本命中 0，正则的前后环视已把 hex 内部数字段排除）——这三键正是按该错误论据进名单的。⑤ **副产品**：`evals/seeds.py::_NUM4_RE` 写死 4 位（`\d{4}`）⇒ 六位 seed 根本扫不到，`guard_prompt_context`/`scan_forbidden_tokens` 会静默失效；改为 3–7 位（同 SPEC B-62）。⑥ 门禁：`scripts/ci_gate.sh` 全绿（1925 passed）。 |
| v1.2.3 | 2026-10-14 | **F73/F74：`sandbox.audit.*` 整块收拢为一个**规则集版本钉扎**（Spec v2.29 / B-86；依据用户裁决 F73 = ②、F74 = 认定为文档错误）**。① **F73 的实况比登记更宽**：原登记只指认 `ruleset` 一个死键，回查初版 PRD 设计初衷后确认——`enabled`/`surfaces`/`ruleset`/`fail_on`/`evidence_chars` **五个键全部 0 消费者**（实现侧只把它们做成 `run_audit()` 的参数 + 同名默认值 ⇒「参数已存在」被误当成「配置已接线」）；最硬的证据是 `configs/default.yaml` 的该块自 M0 骨架 `5d3574c` 建立后**从未被改过一次**，B-82 把 `RULESET_VERSION` 1.0.0→1.1.0 并同步 golden 时也没人想起改它。② **为何不能直接接线**：报告里的 `ruleset_version` 是「这份 clean 按哪版规则判的」的唯一凭据（NFR-02-3 可复现）⇒ 它**必须不可被配置伪造**；若让配置说了算，一次 `audit leak` 经 CLI（读配置）与经测试/golden（直连默认值）会写出**不同**的版本号，字段失去唯一含义。③ **落法（用户选 ②）**：五键删除，只留 **`sandbox.audit.ruleset_pin`**（str，默认 `1.1.0`），CLI 把它喂给 `run_audit(ruleset=…)`，由新增的 **`check_ruleset_pin()`** 断言「== 代码常量 `RULESET_VERSION`」，不符 **fail-closed**（新增 **`E_ENV_AUDIT_RULESET`**，§18.2 归**守卫轴 = 退出码 4**，刻意不占结论轴 2——「有泄漏」与「配置不符」要做的处置相反）。④ **F74**：删掉 `sandbox.audit.enabled` 的「**每代结束自动审计**」措辞——`loop/`/`evolve/`/`evals/` 里没有任何 `run_audit` 调用点，正式实验也是跑完手工补刀（`scripts/run_m3_formal.sh`）；FR-02-16..19 全是「给定 run 目录 → 扫描」，**从来没有自动触发的 FR**，故认定为文档错误而非未实现 FR。⑤ **§16 开放问题 3** 随之结案（`fail_on` 不做配置化；真需要本地放宽应落 CLI flag）。⑥ **护栏 11 条**（`tests/unit/sandbox/test_sandbox_audit.py::test_b86_*`，含「yaml 的 pin == 代码常量」「死键不再是合法扁平键」「CLI 真的读了这个键，且拒绝时不产出报告」）——`git stash push -- dice_ratchet/ configs/` 实测**修复前 11/11 全红**。**诚实边界**：钉扎是「两处版本号 + 一条断言」，代码默认值仍是字面量（`infra` 不能 import `sandbox`，分层 + 循环）⇒ 由用例 `test_b86_config_default_pin_equals_code_constant` 把它钉住；`guard.scan_roots`/`process_boundary`、`honeypot.enabled`/`plan_path`/`self_test` 同为 0 消费者（**未在本轮处理**，另登记）。 |
| v1.2.4 | 2026-10-14 | **F75：`env.sandbox.*` 逐键核完 —— 15 键砍到 2 键（Spec v2.30 / B-87；依据用户裁决「按 F73 同一套规矩逐个核」）**。① **核的结果**：整棵 `env.sandbox` 原有 15 个键，实测**只有 `policy_failure_streak` 与 `audit.ruleset_pin` 有消费者**（前者 `evals/baselines.py` → `SandboxEnv`，后者 `app.py` → `check_ruleset_pin`）。② **删掉的 13 个**（逐键理由见 §9 的新表）：`guard.enforce`（`warn` 档=削弱 INV-4）、`guard.scan_roots`（INV-1 的分层结构，权威 `guard.py::ENV_ROOTS`）、`guard.process_boundary`（指向无生产调用点的 `run_agent_tick()`，另登记 **F76**）、`honeypot.enabled`（注入是检测不变式 G4）、`honeypot.plan_path`（`configs/honeypot.yaml` 的消费者是**测试夹具**，运行时不读）、`honeypot.self_test`（旋钮在 CLI）、`projection.mode`（`assert` 是测试档，golden 显式传参）、`projection.schema_version`（版本身份必须是代码常量，FR-02-5 由 CI 校验）、`render.max_tokens_p95`（`render_compact()` 不收预算参数、全库无断言读它 ⇒ 纯装饰）、`exec.{timeout_s,mem_limit_mb,cpu_limit_s,max_tools}`（`ToolWorker` 的 **containment 参数**，是 INV-4 的一部分而不是策略——让配置或可演化的 harness 抬高自己的沙箱上限正是 INV-4 要防的事；权威落 `guard.py` 默认参数，由用例钉住 `2.0/256/2/32`）。③ **契约夹具**：`configs/honeypot.yaml` 保留并逐字段标注 `[契约]`（`load_plan()` 校验的 4 个注入点）/`[占位]`（运行时不读，只是设计意图与开放问题的记录）。④ **能力一个没删**：`SandboxEnv(projection_mode="assert")`、`SandboxEnv(honeypot=False)`、`load_plan()`、`run_agent_tick()` 全部保留，由反向护墙用例钉住。⑤ **系统性护栏（新增）**：`tests/unit/infra/test_config.py::test_b87_every_sandbox_key_declares_a_consumer` 要求 **`env.sandbox.*` 的键集 == 已登记消费者表**——以后新增配置键必须同时登记消费者，否则 CI 红；这条护栏把「写完配置就以为接上了」的成本抬到与接线同高。⑥ **诚实边界**：该护栏证明「有人声明了消费者且该文件提到键名」，**不是数据流证明**（数据流证据由各键自己的用例承担，如 `test_b86_audit_leak_cli_consumes_the_pin`）；护栏用例自身也有两条反向用例（`ToolWorker` 数值不变、能力仍在）在修复前就是绿的。⑦ **同批发现（未处理，另登记 F77）**：`env.content_dir`/`env.golden_dir`/`env.check_invariants` 三个 **EnvConfig 层**键同样 0 消费者（`content_dir` 只出现在 run 快照里、由审计从快照回读，不经配置）。⑧ **测试**：B-87 五条用例，`git stash push -- dice_ratchet/ configs/` 实测**修复前 3 failed / 2 passed**（两条绿的正是设计使然的反向护墙）。 |
| v1.2.5 | 2026-10-14 | **F77-a：`audit leak` 改用快照的 `content_hash` **反查**真实配表（Spec v2.31 / B-88；依据用户裁决「一起做完」）**。① **病灶**：MOD-01 的 `GAME_CONTENT_DIR` 是 import 时解析的常量（`DICERATCHET_ENV__CONTENT_DIR` 或字面默认），**从不读 yaml**；而配置层曾把 `env.content_dir` 写进 run 快照，审计又**信任快照的声明**去推隐藏 token ⇒ 声明与运行时一旦不一致（在 yaml 里指到另一个**真实存在**的配表目录即可），`future_room_leak`/`shop_content_leak` 就会建在**这些局从未用过的配表**上 ⇒ 假阳性/假阴性（F58/F59 同族）；重放若因此跑不动，`derive_hidden_tokens` 只回一条 warning ⇒ 六类里两类**实际未检查**而 verdict 仅 `warn`。② **修法（①+③）**：`env.content_dir` 键**删除**（权威回 `loader.GAME_CONTENT_DIR`），审计改为 `_resolve_content_dir()`——候选目录逐个按 `content_hash(load_tables(dir))` 与快照的 `content_hash` 比对，**命中的那套才算这些局用过的**；三态：命中 → 用它；快照**无** `content_hash`（旧协议/夹具）→ 退回声明或仓库默认（**仅在声明了非默认目录时**记一条 warning，避免把每个旧 run 从 clean 掉成 warn）；**有 digest 但无人相符** → 记 `io_errors`（→ `_verdict` 判 **critical**，退出码 2）并**降级**这两类规则集，**绝不**拿默认配表凑数。③ **实测**：真实归档 `runs/m3-formal` 的 `content_hash`（`sha256:964d6915…`）能被反查命中、零 io_errors ⇒ **历史证据未被污染**；构造「声明一个存在但不同的配表目录」的快照，修复前静默用错表、修复后被判 critical。④ **诚实边界**：`_content_hash` 提供者未注册（测试夹具）时快照无该字段 ⇒ 走旧路径（上面第二条）；护栏 5 条（`test_b88_*`），`git stash` 实测修复前全红。 |
