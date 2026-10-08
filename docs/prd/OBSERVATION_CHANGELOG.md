# OBSERVATION_CHANGELOG —— agent 侧观测协议版本（MOD-02 唯一权威历史）

> 规则（FR-02-4/FR-02-5）：`OBSERVATION_SCHEMA_VERSION` 与 `OBSERVATION_SCHEMA`
> （`dice_ratchet/sandbox/projection.py`）一一对应。字段增删 / 类型变更**必须主版本 +1**，
> 并在本文件**追加一行**（版本 / 日期 / 字段 / digest 影响 / PR 号）。
> CI 校验：末行版本 == `OBSERVATION_SCHEMA_VERSION`（`projection.check_changelog()`），
> 不一致即 `E_ENV_PROJECTION_VERSION`。运行时不读文档目录（v1.0 的 import 期读取已删除）。

| 版本 | 日期 | 字段变更 | digest 影响 | PR |
|---|---|---|---|---|
| v1.0.0 | 2026-09-30 | 首版：GAME `observe(state,"full")` 键子集 + agent 专属键 `episodeId/decisionIndex/stageCount/lastResult`；`legalActionKinds` 去掉 `abandon_run` | 初版基线 | MOD-02 M0 |
