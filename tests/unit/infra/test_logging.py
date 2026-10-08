"""JsonlWriter / 轨迹 schema / Redactor 测试 —— T-09-6 / T-09-7 / T-09-8 / T-09-14 的落盘侧。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.infra.logging import (JsonlWriter, Redactor, validate_trajectory_line)
from dice_ratchet.infra.io import (read_jsonl, schema_error_code, schema_key, validate,
                                write_json_atomic)

REPO = Path(__file__).resolve().parents[3]


def good_line(**over: object) -> dict[str, object]:
    base = {"episode_key": "e-001000-g000-train", "generation": 0, "harness_version": 0,
            "arm": "train", "seed": 1000, "split": "train", "step": 0,
            "decision_index": 0, "plan_index": 0, "attempt": 1, "source": "heuristic",
            "obs_digest": "sha256:" + "a" * 64, "action": {"kind": "end_turn"},
            "reward": 0.0, "cum_reward": 0.0, "illegal": False, "mode": "train",
            "ts": "2026-10-01T12:00:00Z"}
    base.update(over)
    return base


def end_line(**over: object) -> dict[str, object]:
    base = good_line(type="episode_end", outcome="win", stages_cleared=6,
                     hearts_left=3, battles_won=10, decisions=90, degraded=False,
                     step=227)
    base.update(over)
    return base


# ---- T-09-6 崩溃安全 ------------------------------------------------------------------

def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path) as w:
        for i in range(100):
            w.write(good_line(step=i))
    rows, truncated = read_jsonl(path)
    assert len(rows) == 100
    assert truncated == []
    assert rows[0]["step"] == 0 and rows[99]["step"] == 99


def test_truncated_last_line_detected(tmp_path):
    # T-09-6：写 100 行后截断末行 → 前 100 行可解析，末行被 E_IO_TRAILING_TRUNCATED 识别
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path) as w:
        for i in range(100):
            w.write(good_line(step=i))
    data = path.read_bytes()
    # 在最后一行的中间截断（保留前 100 个完整行）
    last_nl = data.rindex(b"\n")
    path.write_bytes(data + b'{"episode_key":"e-001000')
    assert path.read_bytes()[: last_nl + 1] == data
    rows, truncated = read_jsonl(path)
    assert len(rows) == 100
    assert len(truncated) == 1


def test_sigkill_preserves_all_but_last_line(tmp_path):
    # NFR-09-1：子进程写若干行后被 SIGKILL，已落盘行 100% 可解析
    path = tmp_path / "killed.jsonl"
    script = (
        "import json,sys,time\n"
        "from dice_ratchet.infra.logging import JsonlWriter\n"
        f"path = {str(path)!r}\n"
        "w = JsonlWriter(__import__('pathlib').Path(path))\n"
        "for i in range(50):\n"
        "    w.write({'i': i, 's': 'x'*200})\n"
        "time.sleep(30)\n" # 等待被杀
    )
    proc = subprocess.Popen([sys.executable, "-c", script],
                            cwd=str(REPO), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    # 等写入完成（轮询文件行数）
    for _ in range(200):
        if path.is_file():
            with open(path, "rb") as fh:
                if fh.read().count(b"\n") >= 50:
                    break
        if proc.poll() is not None:
            break
        import time as _t
        _t.sleep(0.05)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    rows, truncated = read_jsonl(path)
    assert len(rows) >= 1
    for row in rows:
        assert "i" in row


def test_non_final_corrupt_line_rejected(tmp_path):
    path = tmp_path / "ep.jsonl"
    path.write_text('{"a":1}\nBROKEN\n{"b":2}\n', encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        read_jsonl(path)
    assert exc.value.code == E.E_IO_WRITE


def test_write_line_atomicity(tmp_path):
    # FR-09-6：「一行要么完整要么不存在」—— 整行单次写入
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path) as w:
        w.write(good_line())
    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert content.count("\n") == 1


def test_fsync_every(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path, fsync_every=2) as w:
        for i in range(5):
            w.write(good_line(step=i))
    assert path.is_file()


def test_rotation(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path, rotate_max_mb=1) as w:
        # 1 MB 上限，写足够多行触发轮转
        for i in range(2000):
            w.write(good_line(step=i, x_pad="x" * 2000))
    assert (path.with_name("ep.jsonl.1")).is_file()
    assert path.is_file()


# ---- T-09-8 轨迹行 schema ---------------------------------------------------------------

def test_valid_line_passes():
    validate_trajectory_line(good_line())
    validate_trajectory_line(end_line())


def test_missing_required_field():
    # T-09-8：缺 obs_digest → E_IO_TRAJECTORY_SCHEMA
    line = good_line()
    del line["obs_digest"]
    with pytest.raises(InfraError) as exc:
        validate_trajectory_line(line)
    assert exc.value.code == E.E_IO_TRAJECTORY_SCHEMA
    assert "obs_digest" in str(exc.value)


def test_bad_episode_key():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(episode_key="bad-key"))


def test_bad_obs_digest():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(obs_digest="md5:abc"))


def test_unknown_field_rejected():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(unknown_field=1))


def test_x_prefix_extension_allowed():
    validate_trajectory_line(good_line(x_note="调试扩展字段"))


def test_end_line_requires_outcome_fields():
    with pytest.raises(InfraError) as exc:
        validate_trajectory_line(good_line(type="episode_end"))
    assert "outcome" in str(exc.value) or "stages_cleared" in str(exc.value)


def test_ts_must_be_utc_z():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(ts="2026-10-01T12:00:00+08:00"))


def test_roundtrip_dict_jsonl_dict(tmp_path):
    # G5：dict → validate → jsonl → parse → dict 等价
    path = tmp_path / "ep.jsonl"
    lines = [good_line(step=i) for i in range(3)] + [end_line()]
    with JsonlWriter(path) as w:
        for line in lines:
            w.write(line)
    parsed, _ = read_jsonl(path)
    assert [json.loads(json.dumps(l, ensure_ascii=False)) for l in parsed] == lines


# ---- T-09-7 redaction --------------------------------------------------------------------

def test_redactor_string_and_int(tmp_path):
    # T-09-7 ①：prompt 快照里出现 heldout seed → 替换并计数
    red = Redactor()
    red.register(2042, "seed")
    obj, hits = red.redact({"text": "the seed is 2042 here"})
    assert obj["text"] == "the seed is <REDACTED:seed> here"
    assert hits == 1


def test_redactor_scrubs_int_value():
    # 正向控制：注册的整数值会被精确替换
    red = Redactor()
    red.register(2042, "seed")
    obj, hits = red.redact({"seed": 2042})
    assert obj["seed"] == "<REDACTED:seed>"
    assert hits == 1


def test_trajectory_writer_not_redacted(tmp_path):
    # T-09-7 ② / B-19：轨迹属审计侧——episodes 写入方不传 redactor，seed 原样落盘
    # （FR-09-9 的作用域由调用方接线保证：只有 prompts/memory/reflections 传 redactor）
    path = tmp_path / "episodes" / "train" / "e-002042-g000-train.jsonl"
    line = good_line(seed=2042, episode_key="e-002042-g000-train")
    with JsonlWriter(path) as w: # 注意：无 redactor 参数
        w.write(line)
    rows, _ = read_jsonl(path)
    assert rows[0]["seed"] == 2042
    validate_trajectory_line(rows[0]) # 原值仍过 schema


def test_redactor_no_false_positive_on_substring():
    red = Redactor()
    red.register(2042, "seed")
    obj, hits = red.redact({"v": 20420})
    assert hits == 0
    assert obj["v"] == 20420


def test_redactor_nested_structures():
    red = Redactor()
    red.register("H1", "basket")
    red.register(2042, "seed")
    obj, hits = red.redact({
        "basket": "H1",
        "items": [{"seed_text": "seed 2042", "other": "H1-bundle"}],
        "nested": {"deep": "heldout H1 seeds 2042"},
    })
    assert obj["basket"] == "<REDACTED:basket>"
    assert obj["items"][0]["seed_text"] == "seed <REDACTED:seed>"
    assert hits >= 2


def test_redactor_disabled_when_empty():
    red = Redactor()
    obj, hits = red.redact({"a": 1})
    assert hits == 0 and obj == {"a": 1}


def test_writer_applies_redactor(tmp_path):
    path = tmp_path / "prompt.jsonl"
    red = Redactor()
    red.register(2042, "seed")
    hits_log: list[int] = []
    with JsonlWriter(path, redactor=red,
                     on_redaction_hit=lambda h, _o: hits_log.append(h)) as w:
        w.write({"prompt": "seed 2042"})
    rows, _ = read_jsonl(path)
    assert rows[0]["prompt"] == "seed <REDACTED:seed>"
    assert hits_log == [1]


# ---- FR-09-18 原子写 ----------------------------------------------------------------------

def test_write_json_atomic_is_replace(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"a": 1})
    assert not (tmp_path / "state.json.tmp").exists()
    assert path.read_text(encoding="utf-8").startswith("{")


def test_write_json_atomic_floats_rounded(tmp_path):
    # B-34：浮点 round(x, 4)
    path = tmp_path / "report.json"
    write_json_atomic(path, {"win_rate": 0.47499999})
    assert json.loads(path.read_text(encoding="utf-8"))["win_rate"] == 0.475


# ---- 补足边界路径（G1：logging.py 覆盖率 ≥ 90%）---------------------------------------------

def test_write_after_close_rejected(tmp_path):
    path = tmp_path / "ep.jsonl"
    w = JsonlWriter(path)
    w.close()
    with pytest.raises(InfraError) as exc:
        w.write(good_line())
    assert exc.value.code == E.E_IO_WRITE


def test_double_close_is_safe(tmp_path):
    w = JsonlWriter(tmp_path / "ep.jsonl")
    w.close()
    w.close() # 幂等，不抛


def test_close_fsyncs(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path, fsync_every=1) as w:
        w.write(good_line())
    assert path.is_file()


def test_write_lines_batch(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path) as w:
        w.write_lines([good_line(step=i) for i in range(3)])
    rows, _ = read_jsonl(path)
    assert len(rows) == 3


def test_latency_drain(tmp_path):
    # NFR-09-2 / §13：单行写入耗时记录
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path) as w:
        for i in range(10):
            w.write(good_line(step=i))
    latencies = w.drain_latency_ms()
    assert len(latencies) == 10
    assert all(l >= 0 for l in latencies)
    # NFR-09-2：单次 write() p99 < 1 ms（本地 SSD，不含 fsync）；实测约 0.01 ms
    latencies.sort()
    p99 = latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))]
    assert p99 < 1.0, f"write() p99={p99:.4f} ms 超出 NFR-09-2 预算"


def test_schema_validation_via_writer(tmp_path):
    # JsonlWriter 的 schema 参数：非法行在写入前即被拦截
    path = tmp_path / "ep.jsonl"
    bad = good_line()
    del bad["obs_digest"]
    with JsonlWriter(path, schema="trajectory_line.schema.json") as w:
        with pytest.raises(InfraError) as exc:
            w.write(bad)
        assert exc.value.code == E.E_IO_TRAJECTORY_SCHEMA


def test_schema_validation_episode_end_via_writer(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path, schema="trajectory_line.schema.json") as w:
        w.write(end_line())
    rows, _ = read_jsonl(path)
    assert rows[0]["type"] == "episode_end"


def test_non_dict_line_rejected():
    with pytest.raises(InfraError):
        validate_trajectory_line(["not", "a", "dict"]) # type: ignore[arg-type]


def test_episode_end_wrong_type_value():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(type="episode_middle"))


def test_episode_end_bad_key():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(type="episode_end"))


def test_episode_end_bad_episode_key():
    bad = end_line(episode_key="nope")
    with pytest.raises(InfraError):
        validate_trajectory_line(bad)


def test_episode_end_bad_ts():
    with pytest.raises(InfraError):
        validate_trajectory_line(end_line(ts="2026-10-01T12:00:00+02:00"))


def test_optional_line_keys_allowed():
    validate_trajectory_line(good_line(
        illegal_reason="illegal:dieId", llm_raw="raw text", llm_latency_ms=120,
        prompt_tokens=100, completion_tokens=20, prompt_hash="sha256:" + "b" * 64,
        memory_ids=["m-0001"], tool_hash="sha256:" + "c" * 64))


def test_illegal_reason_type_error():
    with pytest.raises(InfraError):
        validate_trajectory_line(good_line(obs_digest=12345)) # 类型错


def test_read_trajectory_helper(tmp_path):
    path = tmp_path / "ep.jsonl"
    with JsonlWriter(path) as w:
        w.write(good_line())
    from dice_ratchet.infra.logging import read_trajectory
    rows, n_trunc = read_trajectory(path)
    assert len(rows) == 1
    assert n_trunc == 0


def test_redactor_handles_floats_and_tuples():
    red = Redactor()
    red.register("H1", "basket")
    obj, hits = red.redact({"f": 1.5, "t": ("H1", "x"), "nested": {"k": "H1"}})
    assert obj["t"] == ("<REDACTED:basket>", "x")
    assert obj["nested"]["k"] == "<REDACTED:basket>"
    assert hits >= 2


def test_redactor_register_empty_skipped():
    red = Redactor()
    red.register("", "nothing")
    assert red.registered == {}


def test_redactor_register_many():
    red = Redactor()
    red.register_many([2000, 2001], "seed")
    obj, hits = red.redact({"a": "2000 and 2001"})
    assert hits == 2
    assert "<REDACTED:seed>" in obj["a"]


def test_rotation_missing_file_safe(tmp_path):
    # stat 失败时不抛（_maybe_rotate 容错）
    path = tmp_path / "gone.jsonl"
    w = JsonlWriter(path, rotate_max_mb=1)
    w.write(good_line())
    path.unlink()
    w._maybe_rotate() # 不抛
    w.close()


# ---- 回归（2026-10-03 审计 P1-1）：FR-09-9 三出口接线 --------------------------------

def test_make_redacted_writer_emits_event_and_alert(tmp_path):
    """命中 > 0 → 写 infra.redaction_hit 事件 + 触发 MOD-02 告警回调（FR-09-9）。"""
    from dice_ratchet.infra import events

    events.unbind()
    events.set_dev_mode(True)
    alerts: list[int] = []
    events.register_redaction_alert(lambda hits, _obj: alerts.append(hits))
    try:
        events.bind(tmp_path / "events.jsonl", "r-red")
        red = Redactor()
        red.register(2042, "seed")
        with events.make_redacted_writer(tmp_path / "prompts" / "p1.jsonl", red,
                                         surface="prompts") as w:
            w.write({"prompt": "heldout seed 2042 leaked"})
        events.unbind()
        rows, _ = read_jsonl(tmp_path / "events.jsonl")
    finally:
        events.register_redaction_alert(None)
        events.unbind()
        events.set_dev_mode(False)
    assert [r["event"] for r in rows] == ["infra.redaction_hit"]
    assert rows[0]["hits"] == 1
    assert rows[0]["code"] == E.E_IO_REDACTION_HIT
    assert alerts == [1]
    text = (tmp_path / "prompts" / "p1.jsonl").read_text(encoding="utf-8")
    assert "<REDACTED:seed>" in text and "2042" not in text


@pytest.mark.parametrize("surface", ["episodes", "generations", "events", "train"])
def test_make_redacted_writer_rejects_non_redacted_surfaces(tmp_path, surface):
    """B-19：轨迹/事件/generations 属审计侧，作用域在 API 层强制。"""
    from dice_ratchet.infra import events

    with pytest.raises(InfraError) as exc:
        events.make_redacted_writer(tmp_path / f"{surface}.jsonl", Redactor(),
                                    surface=surface)
    assert exc.value.code == E.E_IO_CONFIG_INVALID


def test_redacted_surfaces_constant_matches_spec():
    from dice_ratchet.infra.logging import REDACTED_SURFACES
    assert REDACTED_SURFACES == ("prompts", "memory", "reflections")


# ---- io.validate 的 schema → 错误码登记表（2026-10-03 审计 P2-4）----------------------

def test_every_schema_has_a_registered_error_code():
    """``schemas/*.schema.json`` 与 ``_SCHEMA_ERROR_CODES`` 必须一一对应。

    旧实现用 ``name.startswith("trajectory")`` 二分，新增 schema 会**静默**落到
    ``E_EVAL_SCHEMA``（MOD-08 的码）。本用例把漏登记变成红灯：新增 schema 文件时
    必须同时决定它归哪个模块（IO / ENV / EVAL）。
    """
    from dice_ratchet.infra.io import _SCHEMA_ERROR_CODES

    on_disk = {schema_key(p.name) for p in sorted((REPO / "schemas").glob("*.schema.json"))}
    assert on_disk, "schemas/ 下没有 schema 文件？"
    assert on_disk == set(_SCHEMA_ERROR_CODES), (
        f"未登记的 schema：{sorted(on_disk - set(_SCHEMA_ERROR_CODES))}；"
        f"登记了但文件不存在：{sorted(set(_SCHEMA_ERROR_CODES) - on_disk)}")


@pytest.mark.parametrize("name,expected", [
    ("trajectory_line", E.E_IO_TRAJECTORY_SCHEMA),        # MOD-09
    ("trajectory_line.schema.json", E.E_IO_TRAJECTORY_SCHEMA),
    ("schemas/trajectory_line.schema.json", E.E_IO_TRAJECTORY_SCHEMA),
    ("observation", E.E_ENV_SCHEMA),                      # MOD-02
    ("audit_report", E.E_ENV_SCHEMA),                     # MOD-02（旧实现误报 E_EVAL_SCHEMA）
    ("harness_gate", E.E_ENV_SCHEMA),                     # MOD-02
])
def test_schema_error_code_is_by_owning_module(name, expected):
    assert schema_error_code(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("trajectory_line", E.E_IO_TRAJECTORY_SCHEMA),
    ("observation", E.E_ENV_SCHEMA),
    ("audit_report", E.E_ENV_SCHEMA),
    ("harness_gate", E.E_ENV_SCHEMA),
])
def test_validate_raises_the_registered_code(name, expected):
    """空对象必然违反各 schema：错误码必须按归属模块抛出，而不是一律 ``E_EVAL_SCHEMA``。"""
    with pytest.raises(InfraError) as exc:
        validate(name, {})
    assert exc.value.code == expected, f"{name} 抛了 {exc.value.code}"
    assert name in str(exc.value), "错误信息必须含 schema 名，便于定位"
