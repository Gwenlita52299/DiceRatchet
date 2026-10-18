"""``cache_report`` 只读复算（2026-10-17 缓存修复的验收工具）。"""

from __future__ import annotations

import json

import pytest

from dice_ratchet.inference.cache_report import (extract_cache_usage, format_report,
                                                 main, summarize_dir, summarize_records)


def _response(prompt: int, completion: int, hit: int | None, *,
              details: bool = False) -> str:
    usage: dict[str, object] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if hit is not None:
        if details:
            usage["prompt_tokens_details"] = {"cached_tokens": hit}
        else:
            usage["prompt_cache_hit_tokens"] = hit
            usage["prompt_cache_miss_tokens"] = prompt - hit
    return json.dumps({"id": "x", "choices": [], "usage": usage})


def test_extract_prefers_structured_usage_and_falls_back_to_response_text():
    direct = {"usage": {"prompt_tokens": 100, "completion_tokens": 5,
                        "cache_hit_tokens": 64}}
    assert extract_cache_usage(direct) == (100, 5, 64)
    legacy = {"response_text": _response(100, 5, 32)}
    assert extract_cache_usage(legacy) == (100, 5, 32)
    details = {"response_text": _response(100, 5, 16, details=True)}
    assert extract_cache_usage(details) == (100, 5, 16)
    assert extract_cache_usage({"usage": {}}) is None
    assert extract_cache_usage({"response_text": "not json"}) is None


def test_summarize_splits_unique_and_repeat_prompts():
    records = [
        {"prompt_hash": "p1", "response_text": _response(1000, 10, 64)},
        {"prompt_hash": "p1", "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                                        "cache_hit_tokens": 700}},
        {"prompt_hash": "p2", "response_text": _response(500, 10, 0)},
        {"prompt_hash": "p3", "response_text": "not json"},
    ]
    stats = summarize_records(records)
    assert stats["calls"] == 3
    assert stats["unique_prompts"] == 2
    assert stats["repeat_calls"] == 1
    assert stats["prompt_tokens"] == 2500
    assert stats["cache_hit_tokens"] == 764
    assert stats["cache_miss_tokens"] == 1736
    assert stats["hit_rate"] == pytest.approx(0.3056)
    assert stats["unique_hit_rate"] == pytest.approx(64 / 1500, abs=1e-6)
    assert stats["repeat_hit_rate"] == pytest.approx(0.7)
    assert stats["median_call_hit_rate"] == pytest.approx(0.064)
    assert {item["tokens"] for item in stats["top_hit_values"]} == {0, 64, 700}


def test_summary_uses_created_order_not_file_order():
    """同一 prompt 的首次 = ``created`` 最早那次，不是文件名/入参顺序。"""
    low = {"prompt_hash": "p1", "response_text": json.dumps(
        {"created": 100, "usage": {"prompt_tokens": 1000, "completion_tokens": 5,
                                   "prompt_cache_hit_tokens": 0}})}
    high = {"prompt_hash": "p1", "response_text": json.dumps(
        {"created": 200, "usage": {"prompt_tokens": 1000, "completion_tokens": 5,
                                   "prompt_cache_hit_tokens": 900}})}
    for records in ([high, low], [low, high]):
        stats = summarize_records(records)
        assert stats["unique_hit_rate"] == pytest.approx(0.0)
        assert stats["repeat_hit_rate"] == pytest.approx(0.9)


def test_summarize_dir_and_cli_json(tmp_path, capsys):
    api_dir = tmp_path / "runs" / "r-demo" / "llm_api"
    api_dir.mkdir(parents=True)
    (api_dir / "a.json").write_text(json.dumps(
        {"prompt_hash": "p1", "response_text": _response(1000, 10, 256)}), encoding="utf-8")
    (api_dir / "b.json").write_text(json.dumps(
        {"prompt_hash": "p1", "response_text": _response(1000, 10, 640)}), encoding="utf-8")
    stats = summarize_dir(api_dir)
    assert stats["calls"] == 2 and stats["unique_prompts"] == 1
    assert stats["hit_rate"] == pytest.approx(0.448)
    assert "hit_rate overall" in format_report(stats)

    code = main(["--dir", str(api_dir), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cache_hit_tokens"] == 896
