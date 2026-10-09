"""两个臂/基线的**逐 seed 配对**读数（同 seed 才可比；这是 n 小也能看的唯一口径）。

用法::

    .venv/bin/python scripts/paired_arm_compare.py <run_id> --split heldout \\
        --arm-a play-llm [--arm-b baseline-heuristic ...] [--run-b <另一个 run>]

为什么需要它：``eval report`` 把基线率附在扩展量里（各算各的 CI），而**配对**检验要看
同一 seed 上两个臂的胜负。n=10 时两个 80% / 70% 的 Wilson CI 会大幅重叠，但配对表
（都赢 / 只 A 赢 / 只 B 赢 / 都输）与 McNemar 精确检验才是"有没有差别"的正确问法；
n 小时它同样会诚实地告诉你"证据不足"（本项目口径：不显著 ≠ 无提升）。

统计实现直接复用 ``dice_ratchet.evals.metrics.mcnemar_exact_p``（PRD §8.2 / T-10），
本脚本只做对齐与展示，不自己写检验。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path: # 让 `.venv/bin/python scripts/xxx.py` 直接可用
    sys.path.insert(0, str(_REPO))

from dice_ratchet.evals.harness import load_episode_results  # noqa: E402
from dice_ratchet.evals.metrics import mcnemar_exact_p  # noqa: E402


def _load(run_id: str, split: str, arm: str, generation: int,
          harness_version: int) -> dict[int, object]:
    root = Path("runs") / run_id
    rows = load_episode_results(root, split, generation=generation,
                               harness_version=harness_version, arm=arm)
    return {int(r.seed): r for r in rows}


def _compare(arm_a: str, arm_b: str, a: dict[int, object], b: dict[int, object],
             *, verbose: bool) -> bool:
    seeds = sorted(set(a) & set(b))
    if not seeds:
        print(f"  {arm_a} vs {arm_b}：无共同 seed，无法配对（A={len(a)} B={len(b)}）")
        return False
    aw = [a[s].outcome == "win" for s in seeds]
    bw = [b[s].outcome == "win" for s in seeds]
    both = sum(1 for x, y in zip(aw, bw) if x and y)
    only_a = sum(1 for x, y in zip(aw, bw) if x and not y)
    only_b = sum(1 for x, y in zip(aw, bw) if y and not x)
    neither = sum(1 for x, y in zip(aw, bw) if not x and not y)
    disc = only_a + only_b
    p_two = mcnemar_exact_p(aw, bw, "two-sided")
    p_greater = mcnemar_exact_p(aw, bw, "greater")
    print(f"\n{arm_a}  vs  {arm_b}   配对 n={len(seeds)}")
    print(f"  胜率：A {sum(aw)}/{len(aw)} = {sum(aw)/len(aw):.1%}"
          f" | B {sum(bw)}/{len(bw)} = {sum(bw)/len(bw):.1%}"
          f" | 差 {100.0 * (sum(aw) - sum(bw)) / len(aw):+.1f}pp")
    print(f"  配对表：都赢 {both} | 只 A 赢 {only_a} | 只 B 赢 {only_b} | 都输 {neither}"
          f"（不一致 {disc} 对）")
    print(f"  McNemar 精确检验：双尾 p={p_two:.4f} | 单尾(A 更好) p={p_greater:.4f}"
          f"  → {'显著' if p_two < 0.05 else '不显著（证据不足，不等于无提升）'}")
    print(f"  可判显著门槛：不一致对需 ≥6 对且方向一致（n=40 时双尾 p=0.031）")
    if verbose:
        for s in seeds:
            print(f"    seed {s}: A {'win ' if a[s].outcome == 'win' else 'lose'}"
                  f" (steps {a[s].steps:4d}) | B {'win' if b[s].outcome == 'win' else 'lose'}"
                  f" (steps {b[s].steps:4d})")
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="逐 seed 配对比较两个臂（McNemar 精确检验）")
    p.add_argument("run_id")
    p.add_argument("--split", default="heldout")
    p.add_argument("--arm-a", default="play-llm", help="默认 play-llm（play --agent llm 的臂名）")
    p.add_argument("--arm-b", action="append", default=None,
                   help="可重复；默认 baseline-heuristic 与 baseline-random")
    p.add_argument("--run-b", default=None,
                   help="B 侧取自另一个 run（A/B/C 换臂必然换 run-id，见 F10）；缺省与 A 同 run")
    p.add_argument("--generation", type=int, default=0)
    p.add_argument("--harness-version", type=int, default=0)
    p.add_argument("--verbose", action="store_true", help="逐局打印 seed/胜负/steps")
    args = p.parse_args(argv)

    run_b = args.run_b or args.run_id
    arms_b = args.arm_b or ["baseline-heuristic", "baseline-random"]
    a = _load(args.run_id, args.split, args.arm_a, args.generation, args.harness_version)
    print(f"run={args.run_id} split={args.split} arm={args.arm_a}: {len(a)} 局")
    label_a = f"{args.arm_a}@{args.run_id}"
    ok = False
    for arm_b in arms_b:
        b = _load(run_b, args.split, arm_b, args.generation, args.harness_version)
        print(f"\nrun={run_b} split={args.split} arm={arm_b}: {len(b)} 局")
        ok = _compare(label_a, f"{arm_b}@{run_b}", a, b, verbose=args.verbose) or ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
