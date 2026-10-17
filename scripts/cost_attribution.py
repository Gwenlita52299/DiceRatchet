"""成本归因：把 `runs/<run-id>/llm_api/*.json` 的每一次调用落到「代 × 类型（× seed）」上。

**为什么需要它**（2026-10-17）：`--cost` 只给一个总数（本轮 ¥240.94）。裁决与复盘要回答
「钱花在哪个 cycle、哪种局、哪个角色上、复用数据省了多少」——而 `llm_api` 记录里**没有**
seed/generation/role，必须做连接：

1. **轨迹连接**（主）：`episodes/<split>/<key>.jsonl` 的每行 = 一次决策调用，带
   `(prompt_tokens, completion_tokens, llm_latency_ms)`。用这个三元组对 `llm_api` 的
   `(usage.prompt_tokens, usage.completion_tokens, latency_ms)`。实测唯一命中 92.7%，
   歧义 38 次（¥0.27），其余是**没落轨迹的调用**（L0 重试、反思、提案）。
2. **反思连接**：`reflections/<episode>.json` 的 `entry` 同样带这三个字段。
3. **最近邻兜底**（重试）：`llm_api` 没有时间戳字段，但文件 mtime 就是调用完成时刻。
   重试与它同局的下一次成功调用只差几秒 ⇒ 把未匹配调用归到**时间上最近的已匹配调用**，
   且只在 `|Δt| ≤ 120 s` 时接受；超出的记为 `unattributed`（不硬塞）。
4. **提案**：`prompt_tokens > 6000` 的少量调用（实测 4 次 / ¥0.10）单记 `proposer`。

**归因粒度**：每个已匹配调用带 `(generation, kind, seed)`，`kind ∈
{train, smoke, heldout-parent, heldout-cand, reflection}`。于是「复用数据的实际花费」
= 旧 run 里 `kind ∈ {train, reflection}` 且 seed 落在训练块的调用之和 —— 这是**实测**，
不是按均值估算。

用法：
    .venv/bin/python scripts/cost_attribution.py --run-id m3r2
    .venv/bin/python scripts/cost_attribution.py --run-id m3-formal \
        --reuse-seeds 1000-1029          # 上一轮这 30 局训练+反思的实际花费（= 本轮省下的钱）
"""

from __future__ import annotations

import argparse
import bisect
import collections
import glob
import json
import os
import sys
from pathlib import Path


def _tri(prompt_tokens, completion_tokens, latency_ms):
    return (prompt_tokens, completion_tokens, latency_ms)


def _trajectory_index(root: Path) -> dict[tuple, set[tuple]]:
    index: dict[tuple, set[tuple]] = collections.defaultdict(set)
    for path in glob.glob(str(root / "episodes" / "*" / "*.jsonl")):
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("source") != "llm":
                continue
            key = _tri(row.get("prompt_tokens"), row.get("completion_tokens"),
                       row.get("llm_latency_ms"))
            index[key].add((row.get("generation"), row.get("split"), row.get("arm"),
                            row.get("seed")))
    return index


def _reflection_index(root: Path) -> dict[tuple, set[tuple]]:
    index: dict[tuple, set[tuple]] = collections.defaultdict(set)
    for path in glob.glob(str(root / "reflections" / "*.json")):
        if path.endswith("reflections.jsonl"):
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        entry = data.get("entry") or {}
        key = _tri(entry.get("prompt_tokens"), entry.get("completion_tokens"),
                   entry.get("llm_latency_ms"))
        index[key].add((entry.get("generation"), entry.get("seed")))
    return index


def _kind(split, arm) -> str:
    arm = str(arm or "")
    if split == "train" and arm == "train":
        return "train"
    if "smoke" in arm:
        return "smoke"
    if arm == "parent":
        return "heldout-parent"
    return "heldout-cand"


def attribute(run_id: str, *, runs_root: Path = Path("runs")) -> dict:
    root = runs_root / run_id
    traj = _trajectory_index(root)
    refl = _reflection_index(root)
    files = glob.glob(str(root / "llm_api" / "*.json"))

    recs = []          # (mtime, cost, key, prompt_tokens)
    for path in files:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        usage = data.get("usage") or {}
        recs.append((os.path.getmtime(path),
                     float((data.get("cost_estimate") or {}).get("amount") or 0.0),
                     _tri(usage.get("prompt_tokens"), usage.get("completion_tokens"),
                          data.get("latency_ms")),
                     int(usage.get("prompt_tokens") or 0)))
    recs.sort(key=lambda r: r[0])

    matched = []       # (mtime, gen, kind, seed)
    pending = []       # (mtime, cost) 未匹配、待最近邻
    totals = collections.Counter()
    calls = collections.Counter()
    by_seed = collections.Counter()
    ambiguous = ambiguous_cost = 0.0
    ambiguous_n = proposer_n = 0
    proposer_cost = 0.0

    for mtime, cost, key, prompt_tokens in recs:
        te, re = traj.get(key), refl.get(key)
        if te and len(te) == 1:
            gen, split, arm, seed = next(iter(te))
            kind = _kind(split, arm)
        elif re and len(re) == 1:
            gen, seed = next(iter(re))
            kind = "reflection"
        elif prompt_tokens > 6000:
            totals[("?", "proposer")] += cost
            calls[("?", "proposer")] += 1
            proposer_n += 1
            proposer_cost += cost
            continue
        elif te or re:
            totals[("?", "ambiguous")] += cost
            calls[("?", "ambiguous")] += 1
            ambiguous_n += 1
            ambiguous_cost += cost
            continue
        else:
            pending.append((mtime, cost))
            continue
        totals[(gen, kind)] += cost
        calls[(gen, kind)] += 1
        by_seed[(seed, kind)] += cost
        matched.append((mtime, gen, kind, seed))

    # 最近邻：未匹配（重试）归到时间上最近的已匹配调用；|Δt| > 120 s 不硬塞
    mt = [m[0] for m in matched]
    near_n = 0
    unattributed = unattributed_cost = 0.0
    for mtime, cost in pending:
        if not mt:
            totals[("?", "unattributed")] += cost
            calls[("?", "unattributed")] += 1
            unattributed_cost += cost
            continue
        pos = bisect.bisect_left(mt, mtime)
        cand = [p for p in (pos - 1, pos) if 0 <= p < len(matched)]
        best = min(cand, key=lambda p: abs(mt[p] - mtime))
        if abs(mt[best] - mtime) > 120:
            totals[("?", "unattributed")] += cost
            calls[("?", "unattributed")] += 1
            unattributed_cost += cost
            continue
        _, gen, kind, seed = matched[best]
        totals[(gen, kind)] += cost
        calls[(gen, kind)] += 1
        by_seed[(seed, kind)] += cost
        near_n += 1

    total = sum(totals.values())
    return {
        "run_id": run_id,
        "api_calls": len(files),
        "total_cost": round(total, 4),
        "by_gen_kind": [
            {"generation": g, "kind": k, "cost": round(v, 4), "calls": int(calls[(g, k)])}
            for (g, k), v in sorted(totals.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        ],
        "by_generation": [
            {"generation": g, "cost": round(sum(v for (gg, _k), v in totals.items() if gg == g), 4)}
            for g in sorted({gg for (gg, _k) in totals}, key=str)
        ],
        "by_seed_kind": [
            {"seed": s, "kind": k, "cost": round(v, 4)}
            for (s, k), v in sorted(by_seed.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        ],
        "ambiguous": {"calls": int(ambiguous_n), "cost": round(ambiguous_cost, 4)},
        "proposer": {"calls": int(proposer_n), "cost": round(proposer_cost, 4)},
        "retry_nearest_neighbour": {"calls": near_n},
        "unattributed": {"calls": int(calls[("?", "unattributed")]),
                         "cost": round(unattributed_cost, 4)},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--reuse-seeds", default="", help="如 1000-1029：只报这些 seed 的 train+reflection 花费")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)
    result = attribute(args.run_id)
    if args.reuse_seeds:
        lo, hi = (int(x) for x in args.reuse_seeds.split("-"))
        selected = [row for row in result["by_seed_kind"]
                    if row["kind"] in ("train", "reflection") and lo <= int(row["seed"]) <= hi]
        result["reuse_seeds"] = {
            "range": [lo, hi],
            "cost": round(sum(r["cost"] for r in selected), 4),
            "rows": selected,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
