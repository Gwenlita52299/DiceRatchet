# MOD-05 memory —— 策略记忆与反思库

> 实现依据：[`docs/prd/MOD-05-memory-PRD.md`](../../docs/prd/MOD-05-memory-PRD.md)（v1.2）。
> 本文件只记录**落地口径与偏离说明**，不重复 PRD 的需求描述。

## 1. 模块布局

```
dice_ratchet/memory/
  schemas.py    MemoryEntry / ReflectionEntry / Provenance / MemoryCandidate / AddResult /
                RetrieveResult / StoreStats / VerifyReport / RollbackReport + JSON Schema
                （draft 2020-12）+ round-trip + content_sha256 口径
  retriever.py  纯函数：tokenize / situation_key / obs_tag_weights / extract_candidates /
                similarity / utility_score / bm25_score / score_entry / render_entry(s) /
                select_with_budget（不读墙钟、不 import random）
  store.py      MemoryStore（写入管线、去重/冲突、容量/TTL、检索、快照/回滚、自检）
                + MemoryView（评估局只读视图）
  snapshot.py   memory_v{N}.jsonl 的读写、content_sha256、回滚文件原语（B-35）
  cli.py        memory stats|snapshot|rollback|verify|retrieve（B-08）
  errors.py     MemoryIsolationError / MemoryCorruptError / …（错误码取自 infra.errors.E）
```

数据落盘（Spec §5 / PRD §7.1）：

```
runs/<run_id>/
  reflections/reflections.jsonl        一局一行的 ReflectionEntry（仅 train）
  reflections/leak_quarantine.jsonl    I2 命中者（保留原文供审计）
  memory/memory_active.jsonl           active 库（写入追加，定期 tmp+fsync+replace 压实）
  memory/memory_evicted.jsonl          淘汰归档（capacity / ttl）
  memory/memory_v{N}.jsonl             快照（header + 条目行）
  memory/quarantine.jsonl              坏行 + 扫描命中的条目/反思
  memory/index.json                    版本/血缘/计数器
  memory/memory.lock                   压实与快照的互斥锁
  audit/leak_events.jsonl              I1–I4 命中事件
  metrics/memory.jsonl                 §13 埋点（与 events.jsonl 同源同一行）
```

## 2. 调用契约（给 MOD-04 / MOD-06 / MOD-07）

```python
store = MemoryStore(runs_root / run_id, cfg.memory, config_dir=Path("configs"),
                    run_id=run_id)
store.add_reflection(reflection, provenance=provenance)         # 局末（仅 train）
store.record_usage(episode_key, memory_ids, outcome,            # 局末一次
                   adopted_ids=..., tick=st.tick)
store.retrieve(obs, top_k=..., token_budget=..., tick=st.tick)  # 组装 prompt 前
store.frozen(memory_version)                                    # 评估局只读
store.snapshot(reason="gen-003")                                # 每代
store.rollback(target_version)                                  # 人 / MOD-07
```

- `tick` = 本 run 已完成的训练局序号，**唯一时间源**；评估局不推进 tick。
- `retrieve()` 内部任何异常都降级为空记忆（`memory.retrieve_failed`），不阻断决策。
- `frozen(v)` 的写方法全部抛 `E_IO_CONFIG_INVALID`。
- 记忆库的「当前 harness 代际」取已写入的最高 `harness_version`；MOD-07 回滚 harness
  后应调用 `store.set_harness_version(h')`，`harness_version > h'` 的条目即默认不检索
  （FR-05-12）。

## 3. 落地口径（PRD 未逐字规定、本实现作出的裁决）

| # | 事项 | 落地口径 |
|---|---|---|
| 1 | 返回值命名 | PRD §6.2 的 `AddResult` / `RetrieveResult` 为准（Spec §4.7 的 `str` / `list[MemoryHit]` 为摘要写法）：`AddResult.__str__` 给出可读摘要，`RetrieveResult` 可迭代、可按 `entries[i]` 的字段直接喂给 MOD-04 的 `_memory_items` |
| 2 | 行格式 | active 与快照的条目行**同构**（都带 `_type:"memory_entry"`），因此 `rollback` 可原子替换 active 且逐字节恢复（GWT-05-08）；`MemoryEntry.from_json` 解析时剥掉 `_type`，`to_json` 不输出它（保证 round-trip 字节相同） |
| 3 | `snapshot()` 签名 | PRD §6.2 为 `snapshot(*, reason, memory_version=None)`；兼容 MOD-06 PRD 的 `snapshot(version)`：第一个位置参数为 int 时按显式版本号处理 |
| 4 | §6.3 的 `U(e)` | 取**存储的** `entry.utility`（由 §6.4 在写入与 `record_usage` 时按当时的 tick 重算并落盘）。若用检索时的 tick 现算，分数会随检索时刻漂移，且与 `render_entry` / `memory.evict` 里展示的 utility 不一致 |
| 5 | 合并的计数 | 精确/近似合并**不新增行**，只累加 `n_ref`、`merged_from`、`confidence=max`。`add_reflection` 路径带环境原生 `outcome`，故把这次重复观测记入 `n_not_adopted` / `n_win_not_adopted`（「又见到一次且未被采纳」的保守口径）；`add_memory` 没有局末结果，只累加 `n_ref`。采纳证据只由 `record_usage` 按 §6.4 回填，避免自证式膨胀 |
| 6 | `tags(obs)` | §6.3 的驱动权重映射到 §6.6 的受控 tag 词表（同一 tag 取最大权重）：phase/intent/counter→`combat`/`school_counter`/`status_effect`，virtue→`low_virtue`，slot→`defense`/`combat`/`ability`/`corruption`，node→`elite`/`boss`，corrupt→`corruption`，多敌→`target_choice`。`stage_bucket` 在 tag 词表里没有对应项，只参与「同情境加成」 |
| 7 | `verify(tick)` | PRD §6.2 的签名无 tick，但 GWT-05-06 用 `verify(tick)`；实现为 `verify(tick=None, *, full=False)`，`None` 时用库内最大 tick |
| 8 | `--dry-run` | `memory rollback --dry-run` 需要真实的 `RollbackReport(entries_delta, active_delta, lost_entry_ids)`（§8.3），因此 `infra.cli` 把该命令的 dry-run 转发给实现，而不是打印静态计划 JSON |
| 9 | 错误码/退出码 | 在 `infra/errors.py` 登记：`E_IO_SNAPSHOT_MISSING → 2`（§7.4），`E_IO_MEMORY_{CORRUPT,LOCKED,QUOTA,WRITE_FAILED} → 5` |
| 10 | `memory.lock` | 压实拿不到锁（>5 s）→ 记 `E_IO_MEMORY_LOCKED` 并**跳过本次压实**（写入仍走追加，§10）；`snapshot` / `rollback` 拿不到锁则抛错（不静默产出） |
| 11 | `quarantine.jsonl` | 同时承载「坏行（E_IO_MEMORY_CORRUPT）」与「扫描命中的反思/条目（I2/I3）」；`reason` 区分 `corrupt` / `scan` |
| 12 | I2 的 `pattern` | 文本里出现 `seed[:=#] n` 时记正则 `\bseed\b\s*[:=#]?\s*\d{1,7}`；否则记命中的裸 token / MOD-02 的 token_class（PRD 只要求「记录命中串」） |
| 13 | 墙钟 | 只用于审计展示字段（`created_at`、事件的 `ts`）与**锁的陈旧判定**；**不参与**打分、排序、淘汰，也不计入 `content_sha256`（NFR-05-1） |
| 14 | 索引 | `dict[entry_id]` + BM25 倒排（`token → df`）在内存里全量维护；`entries()` 按 `entry_id` 升序，落盘顺序为写入顺序（保证快照/回滚逐字节一致） |
| 15 | 性能口径（NFR-05-2） | 文档 token 与条目行都按 `entry_id` 做缓存：2000 条 active 下 `retrieve()` P95 ≈ 20 ms、`add_reflection()` P95 ≈ 2 ms、启动加载 ≈ 0.26 s（不缓存时分别是 ~32 ms / ~19 ms / ~1.3 s——行序列化与分词是热点）。锁文件超过 60 s 视为崩溃残留并被接管，避免压实/快照被永久锁死 |
| 16 | 向量检索（可选） | 阶段一 `w_vec=0` 不计算；`MemoryStore(..., on_embed=...)` 注入 MOD-03 `embed()` 且 `memory.embedding.enabled=true` 时才在**写入**（条目向量，维度不符即降级并记 `E_LLM_UNSUPPORTED`）与**检索**（查询向量）两侧启用；`similarity` 与评分公式不变 |
| 17 | MOD-02 审计面的配套修复（F45-2 收口） | `memory` 面原按**原始行**扫 seed，于是 `created_at: "2026-…"` 的年份 `2026` 落在**旧四位** heldout H1（2000–2039）区间，把 `audit leak` 在任何有记忆库的 run 上刷成 critical（实测 7 条假阳性），G4 失效；当时的修法是比照 M1-F3 对 `log` 面的处理，新增人工死名单 `audit.MEMORY_NON_CONTENT_KEYS`（seed 规则只扫内容字段）。F45-2（2026-10-11）把 heldout/test 移到六位端口空间后，该名单与 `LOG_NON_CONTENT_KEYS` **一并删除**：seed 规则扫**整行字段**，仅豁免框架自身的播种计划 `LOG_STRUCTURAL_KEYS = {seed, episode_key, spec}` ⇒ 扫描面比带名单时更严（不再有字段被跳过）；回归用例见 `tests/unit/memory/test_isolation.py::test_g4_*` |
| 18 | 可选 LLM 抽取 | `memory.extract.use_llm=true` 且组合根注入 `on_extract`（MOD-03 `generate_json` 适配器）时先用 LLM 抽取；返回值经受控词表/长度校验，失败或产出为空一律记 `E_LLM_EXTRACT_FAILED` 并降级为规则抽取（§10：反思仍入库）。默认关闭，保证 G3 的逐字节确定性 |

## 4. 验证与门（PRD §11/§12）

```bash
.venv/bin/python -m pytest tests/unit/memory tests/golden/memory -q
.venv/bin/python tests/golden/memory/regen_memory_golden.py   # 仅当有意改变检索/快照产物
.venv/bin/python -m dice_ratchet guards check --root dice_ratchet   # G3：无 random / 无 time.time()
.venv/bin/python -m dice_ratchet memory stats --run-id <run_id>
.venv/bin/python -m dice_ratchet audit leak --run-id <run_id>    # G4：0 告警
```

golden：`tests/golden/memory/retrieve_seed1042_g0.json`（固定反思 + `tick=12` 的检索视图）
与 `memory_v0_sha256.txt`（`memory_v0.jsonl` 条目段 sha256）。
