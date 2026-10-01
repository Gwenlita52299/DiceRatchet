#!/usr/bin/env python3
# 按 papers/README-论文清单.md 重新下载 arXiv 论文 PDF
import json, os, sys, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))

# arxivid -> 文件名短标题（与 README-论文清单一致）
SLUGS = {
    "2607.07663": "Recursive-Self-Improvement-in-AI",
    "2607.13104": "Self-Improvements-in-Modern-Agentic-Systems-Survey",
    "2507.21046": "Survey-of-Self-Evolving-Agents",
    "2607.04277": "Self-Reference-in-LLMs-Introspection-Threshold",
    "2401.01335": "SPIN-Self-Play-Fine-Tuning",
    "2401.10020": "Self-Rewarding-Language-Models",
    "2402.06457": "V-STaR-Training-Verifiers",
    "2408.02666": "Self-Taught-Evaluators",
    "2505.03335": "Absolute-Zero",
    "2407.18219": "RISE-Recursive-Introspection",
    "2504.15228": "SAIL-Self-Improving-Coding-Agent",
    "2506.13131": "AlphaEvolve",
    "2408.06292": "The-AI-Scientist",
    "2607.21461": "AREX-Recursively-Self-Improving-Agent",
    "2609.26457": "AIDE2-Recursive-Self-Improvement-Research-Agents",
    "2605.24539": "DemoEvolve",
    "2609.32093": "GameBoyWorlds",
    "2609.24972": "RRSI-Regularized-RSI-Agent-Harnesses",
    "2609.33180": "REUSE-Which-Self-Improvements-Should-We-Trust",
    "2607.24300": "Self-Authored-Verification-Unreliable",
    "2609.02246": "LLM-as-a-Judge-Is-Not-an-Oracle",
    "2609.24663": "EvoPathBench-Process-Level-Evaluation",
    "2406.10162": "Sycophancy-to-Subterfuge-Reward-Tampering",
    "2609.00069": "Auditing-Harness-Tampering",
    "2609.31186": "Evolutionary-Safety-of-RSI",
    "2609.35596": "SEABench-Endogenous-Misalignment",
}

with open(os.path.join(BASE, "metadata.json"), encoding="utf-8") as f:
    META = {p["id"]: p for p in json.load(f)}

ok, fail, skip = [], [], []
for pid, slug in SLUGS.items():
    meta = META.get(pid, {})
    year = meta.get("published", pid[:4])[:4]
    out = os.path.join(BASE, f"{year}_{pid}_{slug}.pdf")
    url = f"https://arxiv.org/pdf/{pid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research-paper-archiver)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data.startswith(b"%PDF"):
            fail.append((pid, f"not a PDF (got {len(data)} bytes, head={data[:40]!r})"))
            continue
        with open(out, "wb") as fh:
            fh.write(data)
        ok.append((pid, os.path.basename(out), len(data)))
        print(f"OK   {pid} -> {os.path.basename(out)} ({len(data)/1024:.0f} KB)", flush=True)
    except urllib.error.HTTPError as e:
        fail.append((pid, f"HTTP {e.code}"))
        print(f"FAIL {pid} HTTP {e.code}", flush=True)
    except Exception as e:
        fail.append((pid, f"{type(e).__name__}: {e}"))
        print(f"FAIL {pid} {type(e).__name__}: {e}", flush=True)

print("\n=== summary ===")
print(f"downloaded: {len(ok)}")
print(f"failed:     {len(fail)}")
for pid, why in fail:
    print(f"  - {pid}: {why}")
json.dump({"ok": [o[0] for o in ok], "fail": fail},
          open(os.path.join(BASE, "_download_report.json"), "w"), ensure_ascii=False, indent=2)
