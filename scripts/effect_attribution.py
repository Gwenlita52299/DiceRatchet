"""差异归因：两个 harness 变体在同一批 seed 上的**配对**对比（¥0，只读归档产物）。

回答的问题（2026-10-17 的 P1 负效应复盘）：
1. 胜率差是**哪几个 seed 翻转**造成的？McNemar 精确 p 是多少？
2. 行为通道：每局/每回合的 attack / defense / heal / utility 出牌数怎么变？
3. 推理通道：`reasoning` 里提到心数 / 防御 / 斩杀的比例与**冲突率**怎么变？
4. 翻转 seed 与其余 seed 的差异是否分离（是"普遍变差"还是"少数 seed 骤降"）？

用法：
    .venv/bin/python scripts/effect_attribution.py \
        --a m3-formal:gen000-heldout-H1-parent \
        --b m3r2:gen000-heldout-H1-parent

    # 也可只比较聚合（不读轨迹）：
    .venv/bin/python scripts/effect_attribution.py --a ... --b ... --no-reasoning
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import sys
from pathlib import Path

KIND_SPACE = ("play_face", "discard_die", "purify", "use_ability", "end_turn", "map_choose",
              "shop_buy", "shop_refresh", "shop_remove", "shop_leave", "event_choose",
              "treasure_pick", "forge_pick", "shrine_choose", "rest_choose", "relic_swap")


def load_arm(run_tag: str, runs_root: Path = Path("runs")) -> tuple[dict[int, dict], dict]:
    """``run_id:arm`` → (seed→逐局, meta)。meta 里带轨迹定位需要的 generation/split/arm。"""
    run, arm = run_tag.split(":", 1)
    path = runs_root / run / "metrics" / "arms" / f"{arm}.json"
    data = json.load(open(path, encoding="utf-8"))
    prov = data.get("provenance") or {}
    meta = {"run": run, "arm": str(prov.get("arm") or arm),
            "generation": int(prov.get("generation") or 0),
            "split": str(prov.get("split") or "heldout")}
    return ({int(e["seed"]): e for e in data.get("episodes", [])}, meta)


def _slots(ep: dict, cat: str) -> int:
    return sum(v for k, v in (ep.get("slot_counts") or {}).items() if str(k).startswith(cat + "×"))


def mcnemar(a: dict[int, dict], b: dict[int, dict]) -> dict:
    seeds = sorted(set(a) & set(b))
    flipped_ab = [s for s in seeds if a[s]["outcome"] == "win" and b[s]["outcome"] != "win"]
    flipped_ba = [s for s in seeds if a[s]["outcome"] != "win" and b[s]["outcome"] == "win"]
    n = len(flipped_ab) + len(flipped_ba)
    if n == 0:
        p = 1.0
    else:
        k = min(len(flipped_ab), len(flipped_ba))
        p = min(1.0, 2.0 * sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n))
    return {
        "paired_seeds": len(seeds),
        "a_wins": sum(1 for s in seeds if a[s]["outcome"] == "win"),
        "b_wins": sum(1 for s in seeds if b[s]["outcome"] == "win"),
        "win_to_lose": flipped_ab,
        "lose_to_win": flipped_ba,
        "mcnemar_p": round(p, 4),
    }


def mix(eps: dict[int, dict], seeds: list[int]) -> dict:
    n = len(seeds)
    return {
        "n": n,
        "win_rate": round(sum(1 for s in seeds if eps[s]["outcome"] == "win") / n, 3),
        "stages": round(sum(eps[s]["stages_cleared"] for s in seeds) / n, 3),
        "hearts": round(sum(eps[s]["hearts_left"] for s in seeds) / n, 3),
        "decisions": round(sum(eps[s]["decisions"] for s in seeds) / n, 1),
        "attack": round(sum(_slots(eps[s], "attack") for s in seeds) / n, 1),
        "defense": round(sum(_slots(eps[s], "defense") for s in seeds) / n, 1),
        "heal": round(sum(_slots(eps[s], "heal") for s in seeds) / n, 1),
        "utility": round(sum(_slots(eps[s], "utility") for s in seeds) / n, 1),
        "illegal": round(sum(eps[s]["illegal_attempts"] for s in seeds) / n, 2),
        "truncation": round(sum(1 for s in seeds if eps[s].get("truncation_reason")) / n, 3),
    }


def reasoning_stats(eps: dict[int, dict], seeds: list[int], meta: dict) -> dict:
    """按该臂自己的轨迹路径读 reasoning：``episodes/<split>/e-<seed:06d>-g<gen:03d>-<arm>.jsonl``。"""
    n = heart = defensive = offensive = both = 0
    root = Path("runs") / meta["run"] / "episodes" / meta["split"]
    for seed in seeds:
        path = root / f"e-{int(seed):06d}-g{int(meta['generation']):03d}-{meta['arm']}.jsonl"
        if not path.is_file():
            continue
        seen = set()
        for line in open(path, encoding="utf-8"):
            row = json.loads(line)
            if row.get("llm_raw") is None:
                continue
            di = row.get("decision_index")
            if di in seen:
                continue
            seen.add(di)
            try:
                text = json.loads(row["llm_raw"]).get("reasoning", "")
            except Exception:
                text = ""
            if not text:
                continue
            n += 1
            h = "心" in text
            d = any(w in text for w in ("防御", "回复", "治疗", "光盾", "保命"))
            o = any(w in text for w in ("输出", "斩杀", "最大化", "集中", "优先杀"))
            heart += h
            defensive += d
            offensive += o
            both += (h and d and o)
    if not n:
        return {"n": 0}
    return {"n": n, "heart": round(heart / n, 3), "defensive": round(defensive / n, 3),
            "offensive": round(offensive / n, 3), "conflict": round(both / n, 3)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="run_id:arm，例如 m3-formal:gen000-heldout-H1-parent")
    ap.add_argument("--b", required=True, help="run_id:arm")
    ap.add_argument("--no-reasoning", action="store_true")
    args = ap.parse_args(argv)
    a, meta_a = load_arm(args.a)
    b, meta_b = load_arm(args.b)
    result = {"a": args.a, "b": args.b, "paired": mcnemar(a, b)}
    seeds = sorted(set(a) & set(b))
    result["mix"] = {"a": mix(a, seeds), "b": mix(b, seeds)}
    flips = result["paired"]["win_to_lose"]
    rest = [s for s in seeds if s not in flips]
    result["flip_vs_rest"] = {
        "flipped": {"a": mix(a, flips), "b": mix(b, flips)},
        "rest": {"a": mix(a, rest), "b": mix(b, rest)},
    }
    if not args.no_reasoning:
        result["reasoning"] = {"a": reasoning_stats(a, seeds, meta_a),
                               "b": reasoning_stats(b, seeds, meta_b)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
