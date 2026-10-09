"""对比两次真跑（``backend: deepseek``）的审计记录，回答「API 跨时间确定吗」。

用法::

    .venv/bin/python scripts/compare_api_runs.py <run_a> <run_b>   # 名字相对 runs/

判定口径（以 **request hash 为准**，不看决策序号）：

- ``request_hash`` 相同 = 送给服务方的字节完全相同（prompt + 采样 + max_tokens +
  json 模式 + 思考开关）→ 此时 ``response_hash`` 不同就是**跨时间非确定性**的硬证据。
- ``sampling.seed`` 只是审计留痕：deepseek 的 ``api_supports_seed=false``，seed
  **不进 payload**，所以 request hash 天然忽略 seed 差异 —— 这正是我们要的口径。
- ``derive_seed("policy", salt, ctx.run, ...)`` 把 **run_id** 也算进 seed，所以换
  run-id 重跑不会复现采样流（即便 temperature=0）；要让适配器自己裁决，得用**同一个
  run-id** 重跑 —— 旧记录留在 ``runs/<run>/llm_api/``，适配器会写 ``.v<n>.json``
  变体并发 ``llm.nondeterministic``。

读 ``runs/<run>/llm_api/*.json`` 与 ``runs/<run>/episodes/**/*.jsonl``；只读、不触网。
"""
from __future__ import annotations

import glob
import json
import os
import sys

EPISODE_KEYS = ("episode_key", "seed", "outcome", "hearts_left", "decisions", "decisions_llm",
                "fallback_decisions", "illegal_attempts", "dropped", "degraded", "seq_hash",
                "battles_won", "stages_cleared", "env_steps")


def load_records(run: str) -> dict[str, dict]:
    """``request_hash -> 审计记录``；同一 hash 的变体（``.v<n>.json``）取首个（原始证据）。"""
    out: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join("runs", run, "llm_api", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        out.setdefault(rec["request_hash"], rec)
    return out


def load_episodes(run: str) -> list[dict]:
    eps: list[dict] = []
    pattern = os.path.join("runs", run, "episodes", "**", "*.jsonl")
    for path in sorted(glob.glob(pattern, recursive=True)):
        with open(path, encoding="utf-8") as fh:
            eps.extend(json.loads(line) for line in fh if line.strip())
    return eps


def pct(values: list[int], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))])


def _usage_line(label: str, recs: dict[str, dict]) -> str:
    lat = [r["latency_ms"] for r in recs.values()]
    prompt = sum(r["usage"]["prompt_tokens"] for r in recs.values())
    completion = sum(r["usage"]["completion_tokens"] for r in recs.values())
    cost = sum((r.get("cost_estimate") or {}).get("amount", 0.0) for r in recs.values())
    status: dict[int, int] = {}
    attempts: dict[int, int] = {}
    models: dict[str, int] = {}
    for r in recs.values():
        status[r["status"]] = status.get(r["status"], 0) + 1
        attempts[r["attempts"]] = attempts.get(r["attempts"], 0) + 1
        models[str(r.get("model_revision"))] = models.get(str(r.get("model_revision")), 0) + 1
    return (f"  {label}: prompt={prompt} completion={completion} total={prompt + completion} "
            f"cost={cost:.4f} CNY | latency(ms) p50={pct(lat, .5):.0f} p95={pct(lat, .95):.0f} "
            f"max={max(lat, default=0)} | status={status} attempts={attempts} models={models}")


def main(a: str, b: str) -> int:
    ra, rb = load_records(a), load_records(b)
    print(f"run A = {a}: {len(ra)} 条调用记录")
    print(f"run B = {b}: {len(rb)} 条调用记录")

    both = sorted(set(ra) & set(rb))
    print(f"request_hash 交集：{len(both)}（A 独有 {len(set(ra) - set(rb))}，"
          f"B 独有 {len(set(rb) - set(ra))}）")

    same_resp = [h for h in both if ra[h]["response_hash"] == rb[h]["response_hash"]]
    diff_resp = [h for h in both if ra[h]["response_hash"] != rb[h]["response_hash"]]
    seed_diff = [h for h in both if ra[h]["sampling"]["seed"] != rb[h]["sampling"]["seed"]]
    print(f"  逐字节相同的请求里：response 相同 {len(same_resp)} / 不同 {len(diff_resp)}"
          f"   <-- 「不同」即跨时间非确定性")
    print(f"  审计 seed 不同的请求 {len(seed_diff)}（seed 不进 payload，不影响上面的判定）")

    for label, recs in (("A", ra), ("B", rb)):
        print(_usage_line(label, recs))

    if diff_resp:
        print("\n非确定性请求（最多列 5 条）：")
        for h in diff_resp[:5]:
            print(f"  {h}\n    A={ra[h]['response_hash']}\n    B={rb[h]['response_hash']}")

    ea, eb = load_episodes(a), load_episodes(b)
    print(f"\nepisodes：A={len(ea)} B={len(eb)}")
    for x, y in zip(ea, eb):
        diffs = [k for k in EPISODE_KEYS if x.get(k) != y.get(k)]
        verdict = "完全一致" if not diffs else "差异：" + "、".join(diffs)
        print(f"  {x.get('episode_key')}：{verdict}")
        for k in diffs:
            print(f"      {k}: A={x.get(k)!r} B={y.get(k)!r}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
