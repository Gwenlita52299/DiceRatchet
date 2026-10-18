"""API 前缀缓存命中报告（离线、只读）。

用途：不改任何 run 产物，直接从 ``runs/<run_id>/llm_api/*.json`` 复算
``prompt_cache_hit_tokens`` / ``prompt_tokens``，回答两个问题：

1. 当前命中率是多少（整体 / 唯一 prompt / 重复 prompt / 中位数）；
2. 命中值卡在多少 token（用来验证「模板静态头是否真的就是可缓存上限」）。

数据来源优先级：

* 新记录（2026-10-17 之后）：``record["usage"]`` 里已有 ``cache_hit_tokens``；
* 旧记录：只能从 ``record["response_text"]``（服务方原始 JSON）里的
  ``usage.prompt_cache_hit_tokens`` 或 ``usage.prompt_tokens_details.cached_tokens`` 取。

刻意**不**改 ``usage`` / ``cost_estimate`` 的落盘格式；这是一个独立的只读诊断工具。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["extract_cache_usage", "summarize_records", "summarize_dir",
           "format_report", "main"]


def _int(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


def _hit_from_usage(usage: Mapping[str, Any]) -> int:
    """从一份 usage 里取缓存命中 token（兼容两个官方字段）。"""
    raw = usage.get("prompt_cache_hit_tokens")
    if not isinstance(raw, int) or isinstance(raw, bool):
        details = usage.get("prompt_tokens_details")
        raw = details.get("cached_tokens") if isinstance(details, Mapping) else None
    return max(0, _int(raw, 0))


def _raw_usage(record: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """从旧记录的 ``response_text``（服务方原始 JSON）里取 usage。"""
    raw = record.get("response_text")
    if not isinstance(raw, str):
        return None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, Mapping):
        return None
    usage = body.get("usage")
    if not isinstance(usage, Mapping) or not isinstance(usage.get("prompt_tokens"), int):
        return None
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    return prompt, completion, min(_hit_from_usage(usage), prompt)


def extract_cache_usage(record: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """返回 ``(prompt_tokens, completion_tokens, cache_hit_tokens)``；不可解析时 None。"""
    usage = record.get("usage")
    raw = _raw_usage(record)
    if isinstance(usage, Mapping) and isinstance(usage.get("prompt_tokens"), int):
        prompt = _int(usage.get("prompt_tokens"))
        completion = _int(usage.get("completion_tokens"))
        if isinstance(usage.get("cache_hit_tokens"), int):
            hit = _int(usage.get("cache_hit_tokens"))
        elif raw is not None:
            # 旧记录：``usage`` 只有 prompt/completion，缓存字段在 response_text 里
            hit = raw[2]
        else:
            hit = _hit_from_usage(usage)
        return prompt, completion, max(0, min(hit, prompt))
    return raw


def _ratio(hit: int, prompt: int) -> float:
    return hit / prompt if prompt > 0 else 0.0


def _created(record: Mapping[str, Any]) -> int:
    """服务方 ``created``（Unix 秒）；缺失时回落到 0，仅用于稳定排序。"""
    raw = record.get("response_text")
    if not isinstance(raw, str):
        return 0
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    if not isinstance(body, Mapping):
        return 0
    value = body.get("created")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """对一组 ``llm_api`` 记录做聚合（唯一/重复 prompt 分开看）。

    **唯一**以服务方 ``created`` 的**时间顺序**为准（不是文件名字典序）：同一个 prompt
    的第一次调用通常只命中静态头，后续重复调用才可能命中整段；若按文件名分组，会把
    “后来的高命中调用”误当成首次，得到偏高的唯一命中率。
    """
    rows: list[tuple[str, int, int]] = []
    ordered = sorted(enumerate(records), key=lambda pair: (_created(pair[1]), pair[0]))
    for index, record in ordered:
        parsed = extract_cache_usage(record)
        if parsed is None:
            continue
        prompt, _completion, hit = parsed
        key = str(record.get("prompt_hash") or record.get("request_hash")
                  or f"__row{index}")
        rows.append((key, hit, prompt))

    first: dict[str, tuple[int, int]] = {}
    repeats: list[tuple[int, int]] = []
    for key, hit, prompt in rows:
        if key in first:
            repeats.append((hit, prompt))
        else:
            first[key] = (hit, prompt)

    def _totals(items: Iterable[tuple[int, int]]) -> tuple[int, int]:
        hit = 0
        prompt = 0
        for item_hit, item_prompt in items:
            hit += item_hit
            prompt += item_prompt
        return hit, prompt

    hit_all, prompt_all = _totals((h, p) for _, h, p in rows)
    hit_unique, prompt_unique = _totals(first.values())
    hit_repeat, prompt_repeat = _totals(repeats)
    per_call = [_ratio(h, p) for _, h, p in rows]
    hit_values = Counter(h for _, h, _ in rows)
    return {
        "calls": len(rows),
        "unique_prompts": len(first),
        "repeat_calls": len(repeats),
        "prompt_tokens": prompt_all,
        "cache_hit_tokens": hit_all,
        "cache_miss_tokens": max(0, prompt_all - hit_all),
        "hit_rate": round(_ratio(hit_all, prompt_all), 6),
        "unique_hit_rate": round(_ratio(hit_unique, prompt_unique), 6),
        "repeat_hit_rate": round(_ratio(hit_repeat, prompt_repeat), 6),
        "median_call_hit_rate": round(statistics.median(per_call), 6) if per_call else 0.0,
        "top_hit_values": [{"tokens": value, "calls": count}
                           for value, count in hit_values.most_common(8)],
    }


def summarize_dir(directory: Path | str) -> dict[str, Any]:
    """读取一个 ``llm_api/`` 目录并聚合。"""
    path = Path(directory)
    records: list[dict[str, Any]] = []
    for file in sorted(path.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping):
            records.append(dict(data))
    stats = summarize_records(records)
    stats["dir"] = str(path)
    return stats


def format_report(stats: Mapping[str, Any]) -> str:
    """人类可读的一页报告。"""
    def pct(value: Any) -> str:
        return f"{100.0 * float(value):6.2f}%"

    top = "  ".join(f"{item['tokens']}×{item['calls']}"
                    for item in stats.get("top_hit_values") or [])
    return "\n".join([
        f"dir: {stats.get('dir', '<memory>')}",
        f"calls={stats.get('calls', 0)} unique={stats.get('unique_prompts', 0)} "
        f"repeat={stats.get('repeat_calls', 0)}",
        f"prompt_tokens={stats.get('prompt_tokens', 0)} "
        f"hit={stats.get('cache_hit_tokens', 0)} "
        f"miss={stats.get('cache_miss_tokens', 0)}",
        f"hit_rate overall={pct(stats.get('hit_rate', 0))} "
        f"unique={pct(stats.get('unique_hit_rate', 0))} "
        f"repeat={pct(stats.get('repeat_hit_rate', 0))} "
        f"median={pct(stats.get('median_call_hit_rate', 0))}",
        f"top hit values: {top or '(none)'}",
    ])


def _default_run(runs_root: Path) -> str | None:
    candidates = [p for p in runs_root.iterdir()
                  if p.is_dir() and (p / "llm_api").is_dir()] if runs_root.is_dir() else []
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / "llm_api").stat().st_mtime).name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读复算 runs/<id>/llm_api 的 prompt cache 命中率")
    parser.add_argument("run_id", nargs="?", help="run id；缺省取 runs/ 下最新的含 llm_api 的 run")
    parser.add_argument("--runs-root", default="runs", help="runs 根目录（默认 runs/）")
    parser.add_argument("--dir", dest="api_dir", help="直接指定 llm_api 目录（优先于 run_id）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    args = parser.parse_args(argv)

    if args.api_dir:
        directory = Path(args.api_dir)
    else:
        runs_root = Path(args.runs_root)
        run_id = args.run_id or _default_run(runs_root)
        if not run_id:
            print(f"找不到含 llm_api 的 run（runs_root={runs_root}）", file=sys.stderr)
            return 2
        directory = runs_root / run_id / "llm_api"
    if not directory.is_dir():
        print(f"目录不存在：{directory}", file=sys.stderr)
        return 2
    stats = summarize_dir(directory)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_report(stats))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
