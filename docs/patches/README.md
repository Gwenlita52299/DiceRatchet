# 人工补丁 / 模板候选

本目录放**人工审查用**的 patch 与完整模板快照；版本快照在 `harness/versions/vN/`。
**2026-10-18 起在役 harness 已是 v4**（裸 v0 + 缓存重排；A/B 结果见
[review §3.2/§3.3](../reviews/2026-10-17-API缓存命中修复.md)）。

| 文件 | 基线 | 说明 |
|---|---|---|
| `2026-10-17-cachefirst.patch` | **历史**：v3（`v0+P1+P2`）的 `harness/prompts/decision.md` | 前缀缓存重排（静态段前置、规则 5「上面→下面」、尾部短提醒）；v4 已不再使用该基线 |
| `2026-10-17-cachefirst-decision.md` | 同上 | 历史完整文件，仅供对照 |
| `harness/versions/v4/diff.patch` | 裸 v0 | **v4 实际登记**的 v0→v4 补丁（由 `runs/cachefix/cachefirst-v0.patch` 复制） |

重排**不改变规则语义**，只改段落顺序与一处指代；`play_hash` 会变，所以：

1. **不要**直接把它写进原始 v0（起点守卫会拦；`harness/BASELINE.json` 是 v0 锚点）；
2. 先复制一个候选 harness，再重排，再用 `loop baseline` 测 40 局 heldout：
   ```bash
   python scripts/reorder_prompt_cachefirst.py \
       --source runs/m3r3/harness/prompts/decision.md \
       --out runs/cachefix/harness/prompts/decision.md
   python -m dice_ratchet loop baseline --run-id cachefix-b0 \
       --harness-dir runs/cachefix/harness --basket H1
   ```
3. 通过 A/B/协议裁决后，登记为 `versions/vN`（v4 已按此路径完成；`version.json.parent=0`，
   `accepted_by=human`；提交 `ac1a281`，tag `harness-v4`）。

生成/校验工具：`scripts/reorder_prompt_cachefirst.py`（`--check` 免费、`--diff` 看差异），
缓存复算：`scripts/cache_hit_report.py`。
