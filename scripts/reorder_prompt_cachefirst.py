#!/usr/bin/env python3
"""把一份 ``decision.md`` 重排成前缀缓存友好的顺序。

用法::

    python scripts/reorder_prompt_cachefirst.py --source harness/prompts/decision.md
    python scripts/reorder_prompt_cachefirst.py --source <base>/prompts/decision.md \
        --out <candidate>/prompts/decision.md

``--check`` 只报告是否需要重排（退出码 1 = 需要），不写文件。
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dice_ratchet.agent.prompts.cache_layout import (  # noqa: E402
    CacheLayoutError, is_reordered, reorder_for_prefix_cache)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="decision.md 前缀缓存重排")
    parser.add_argument("--source", required=True, help="输入模板（decision.md）")
    parser.add_argument("--out", help="输出路径；缺省打印到 stdout")
    parser.add_argument("--check", action="store_true", help="只检查，不写文件")
    parser.add_argument("--diff", action="store_true", help="打印 unified diff")
    args = parser.parse_args(argv)

    source = Path(args.source)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不到模板：{source}（{exc}）", file=sys.stderr)
        return 2
    try:
        new = reorder_for_prefix_cache(text)
    except CacheLayoutError as exc:
        print(f"模板结构不符合契约：{exc}", file=sys.stderr)
        return 3

    if args.check:
        if is_reordered(text):
            print("already cache-first")
            return 0
        print("needs reorder")
        return 1
    if args.diff:
        sys.stdout.writelines(difflib.unified_diff(
            text.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{source}", tofile=f"b/{source}", n=3))
        return 0
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8")
        print(f"wrote {target}")
    else:
        sys.stdout.write(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
