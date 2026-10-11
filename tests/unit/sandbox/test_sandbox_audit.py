"""MOD-02 审计单测（PRD §11；FR-02-16 ~ FR-02-19，INV-3/INV-5，G4）。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.sandbox import audit as A
from dice_ratchet.sandbox import honeypot as HP
from dice_ratchet.sandbox.projection import obs_field_paths

FIXED = "2026-09-30T12:00:00Z"


# ---- FR-02-16 可见集合重建 ---------------------------------------------------------

def test_rebuild_visible_set(build_run, tmp_path):
    built = build_run(tmp_path)
    visible = A.rebuild_visible_set(built.root)
    assert visible.obs_digests == built.digests
    assert all(d.startswith("sha256:") and len(d) == 71 for d in visible.obs_digests)
    assert visible.harness_version == 1
    assert visible.seeds_seen == (1000,) # 只收 split==train（INV-5）
    assert set(visible.obs_paths) == set(obs_field_paths()) # x_obs_keys 求交
    assert visible.memory_texts and "优先处理护卫" in visible.memory_texts[0]


def test_rebuild_visible_set_missing_split_is_empty(build_run, tmp_path):
    built = build_run(tmp_path)
    visible = A.rebuild_visible_set(built.root, split="heldout")
    assert visible.obs_digests == () and visible.seeds_seen == ()
    assert visible.obs_paths == () # 没有轨迹 → 不宣称任何可见字段


def test_rebuild_visible_set_records_io_errors(tmp_path):
    run = tmp_path / "r"
    (run / "episodes" / "train").mkdir(parents=True)
    (run / "episodes" / "train" / "e-000001-g000-train.jsonl").write_text(
        "{broken json\n", encoding="utf-8")
    errors: list[str] = []
    visible = A.rebuild_visible_set(run, io_errors=errors)
    assert errors and "JSONL 损坏" in errors[0]
    assert visible.obs_digests == ()


# ---- FR-02-17/18 六类规则 + 扫描面 --------------------------------------------------

def test_scan_text_seed_hit_and_evidence_window():
    findings = A.scan_text("x" * 40 + " 2013 " + "y" * 40,
                           forbidden={"2013": "seed"}, surface="prompt",
                           episode_id="e-1", step=7, evidence_chars=12,
                           line_ref="prompts/p.txt:3")
    assert len(findings) == 1
    hit = findings[0]
    assert hit.leak_class == A.LeakClass.SEED_LEAK
    assert hit.token_class == "seed" and hit.surface == "prompt" and hit.step == 7
    assert hit.line_ref == "prompts/p.txt:3"
    assert len(hit.evidence) == 12 + 4 + 12 # token 前后各 evidence_chars（FR-02-19）


def test_scan_text_ignores_seed_inside_hex_digest():
    digest = "sha256:" + "a" * 60 + "2013"
    assert A.scan_text(digest, forbidden={"2013": "seed"}, surface="log",
                       episode_id="e-1", step=0) == []


def test_scan_text_ignores_zero_padded_and_longer_runs():
    assert A.scan_text("e-002013-g003", forbidden={"2013": "seed"}, surface="prompt",
                       episode_id="e-1", step=0) == []


@pytest.mark.parametrize("klass,token", [
    ("future_room", "n-99-9-9"), ("shop_item", "skill_blade_execute"),
    ("hidden_field", "drawPile"), ("full_state", "illegalCount"),
    ("canary", HP.HONEYPOT_CANARY),
])
def test_scan_text_classifies_every_token_class(klass, token):
    findings = A.scan_text(f"dump {token} end", forbidden={token: klass},
                           surface="memory", episode_id="e-1", step=1)
    assert len(findings) == 1
    assert findings[0].leak_class == A.TOKEN_CLASS_TO_LEAK[klass]


def test_scan_text_no_false_positive():
    text = "phase=player_turn hearts=2/2 hand=[d-0007] play_face slot_1 处决斩"
    assert A.scan_text(text, forbidden={"2013": "seed", "drawPile": "hidden_field"},
                       surface="prompt", episode_id="e-1", step=0) == []


def test_build_forbidden_excludes_train_seeds_seen():
    visible = A.VisibleSet(obs_paths=(), obs_digests=(), memory_texts=(),
                           harness_version=1, seeds_seen=(1000,))
    forbidden = A.build_forbidden(visible, seeds={"1000": "seed", "2013": "seed"},
                                 hidden={"n-99-9-9": "future_room"})
    assert "2013" in forbidden and "1000" not in forbidden
    assert forbidden["n-99-9-9"] == "future_room"
    for token in ("drawPile", "illegalCount", HP.HONEYPOT_CANARY):
        assert token in forbidden


def test_load_forbidden_seeds_from_configs(repo_root):
    seeds = A.load_forbidden_seeds(repo_root / "configs")
    # F45-2：heldout/test 已移到六位端口空间（731xxx / 947xxx）
    assert "731000" in seeds and "947099" in seeds and "1000" not in seeds
    assert all(v == "seed" for v in seeds.values())


# ---- F44/F45：反思是多行 JSON 文档，且机器元数据不得刷假阳性 -----------------------

def _write_reflection(root: Path, body: dict) -> None:
    refdir = Path(root) / "reflections"
    refdir.mkdir(parents=True, exist_ok=True)
    (refdir / "e-001000-g000-train.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def test_f44_multiline_reflection_json_is_not_read_as_jsonl(build_run, tmp_path):
    """F44：``reflections/<key>.json`` 是多行 pretty JSON，不得按 JSONL 逐行解析。

    实测（2026-10-10 实跑）：51 行的反思文件让审计第 2 行起报 ``Extra data`` ⇒
    ``E_ENV_AUDIT_IO`` ⇒ ``audit leak`` 退出码 2 ⇒ **任何含反思的 `loop run` 都无法
    让判据③（`audit leak` clean）成立**。
    """
    built = build_run(tmp_path, run_id="r-f44")
    _write_reflection(built.root, {"entry": {"text": "优先处理护卫",
                                             "created_at": "2026-10-09T16:34:13Z"},
                                   "episode_key": "e-001000-g000-train"})

    result = A.run_audit(built.root, generated_at=FIXED)

    assert result.io_errors == (), f"多行 JSON 不得被当 JSONL：{result.io_errors}"
    # F45-2：`created_at` 的年份 2026 与 heldout 的六位区间（731xxx）**不在同一数字空间**，
    # 因此无论名单在不在，它都不再可能是 seed 泄漏（这也是删掉两张死名单的前提）。
    assert result.leak_counts.get("seed_leak") is None, result.findings
    assert result.verdict == A.VERDICT_CLEAN


def test_f45_real_leak_in_reflection_body_is_still_caught(build_run, tmp_path):
    """F45a 的反面：正文里的 heldout seed 字面量**仍必须**被抓（修复不得把审计变瞎）。"""
    built = build_run(tmp_path, run_id="r-f45")
    _write_reflection(built.root, {"entry": {"text": "历史最优 seed=731013 的策略",
                                             "created_at": "2026-10-09T16:34:13Z"}})

    result = A.run_audit(built.root, generated_at=FIXED)

    assert result.io_errors == ()
    assert result.leak_counts.get("seed_leak") == 1, result.findings
    assert result.verdict == A.VERDICT_CRITICAL


def test_f45b_token_budget_in_events_is_not_a_seed_leak(build_run, tmp_path):
    """F45b/F45-2：事件里的**按调用方输出预算**（B-47 的 ``max_tokens: 2048``）不是泄漏面。

    原先这是 F37 的修复**引入**的假阳性：一个完全合法的配置值落进 heldout 的 2000–2099
    区间，于是审计刷 ``seed_leak/critical``，靠 ``max_tokens`` 进死名单才压住。
    F45-2 之后 heldout 是六位（731xxx），它与 ``max_tokens``/采样浮点/哈希片段
    **不再共享数字空间**——所以这条用例现在验证的是「删掉名单后仍然干净」。
    """
    built = build_run(tmp_path, run_id="r-f45b")
    (built.root / "events.jsonl").write_text(json.dumps({
        "event": "llm.api.call", "model_id": "deepseek-flash", "max_tokens": 2048,
        "temperature": 0.2072, "request_hash": "sha256:" + "a" * 32 + "2026" + "b" * 28,
        "ts": "2026-10-10T00:00:00Z", "run_id": built.root.name,
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    result = A.run_audit(built.root, generated_at=FIXED)

    assert result.leak_counts.get("seed_leak") is None, result.findings


# ---- FR-02-19 run_audit：verdict / 退出码 / 报告 -----------------------------------

def test_clean_run_audit_is_clean(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-clean")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CLEAN
    assert result.findings == () and result.leak_counts == {}
    assert result.io_errors == () and result.warnings == ()
    assert result.exit_code == 0
    assert result.honeypot == {"injected": 4, "triggered": 0, "caught_rate": 1.0}
    assert result.scanned["episodes"] == 1 and result.scanned["steps"] == built.steps


def test_seed_leak_in_prompt_is_critical(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-seed", prompt_text="历史最优 seed=731013 的策略\n")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CRITICAL and result.exit_code == 2
    assert result.leak_counts.get("seed_leak") == 1
    finding = result.findings[0]
    assert finding.surface == "prompt" and finding.leak_class == A.LeakClass.SEED_LEAK
    assert "731013" in finding.evidence


def test_heldout_seed_in_memory_is_critical(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-mem", memory_text="heldout 策略 731000 有效\n")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CRITICAL
    assert result.leak_counts.get("seed_leak") == 1
    assert result.findings[0].surface == "memory"


def test_own_train_seed_is_not_a_leak(build_run, tmp_path):
    """INV-5：本 run 的 train seed 允许出现在 prompt/memory（``seeds_seen``）。"""
    built = build_run(tmp_path, run_id="r-train", prompt_text="本局 seed=1000 的记录\n")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CLEAN


def test_timing_fields_in_events_are_not_seed_leaks(build_run, tmp_path):
    """机器耗时字段不作 seed 扫描（2026-10-08 CI parity 随机失败的回归）。

    ``eval.seed_done`` 会带 ``dur_s``/``episode_s``/``cpu_s``，MOD-06/07 另发
    ``wall_ms``/``elapsed_s``/``elapsed_ms``。耗时的小数部分落在**旧** heldout 区间
    （``episode_s=2.2083`` → 2083）时曾被判 critical：**同一提交本地通过、CI 失败**，
    因为整件事取决于墙钟数值。F45-2 把 heldout 移到六位（731xxx）后，耗时与 seed
    **彻底不在同一数字空间**；这条用例因此改为验证「不再有任何字段需要豁免」。
    """
    built = build_run(tmp_path, run_id="r-timing")
    rows = [
        {"event": "eval.seed_done", "ts": FIXED, "seed": 1000, "steps": 40,
         "decisions": 12, "dur_s": 2.2072, "episode_s": 2.2083, "cpu_s": 2.2072},
        {"event": "memory.retrieve", "ts": FIXED, "elapsed_ms": 2072.8,
         "tokens": 12, "wall_ms": 2072, "elapsed_s": 2.2072},
    ]
    (built.root / "events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.leak_counts.get("seed_leak", 0) == 0
    assert result.verdict == A.VERDICT_CLEAN
    # 反例：同一批事件里，内容字段带 heldout seed 仍必须抓到（白名单不能变成盲区）
    rows[0]["reason"] = "泄漏面：heldout seed 731083"
    (built.root / "events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    dirty = A.run_audit(built.root, generated_at=FIXED)
    assert dirty.leak_counts.get("seed_leak", 0) >= 1
    assert dirty.verdict == A.VERDICT_CRITICAL


def test_canary_in_log_is_critical(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-canary")
    text = built.episode_path.read_text(encoding="utf-8")
    built.episode_path.write_text(text.replace('"llm_raw": null',
                                               f'"llm_raw": "{HP.HONEYPOT_CANARY}"', 1),
                                  encoding="utf-8")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CRITICAL
    assert result.leak_counts.get("canary_leak", 0) >= 1
    assert result.honeypot["triggered"] >= 1
    assert result.findings[0].leak_class == A.LeakClass.CANARY_LEAK


def test_hidden_field_name_is_medium_warn(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-hidden", prompt_text="我看到了 drawPile 的顺序\n")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_WARN and result.exit_code == 1
    assert result.leak_counts.get("hidden_field_name_leak") == 1
    assert A.SEVERITY["hidden_field_name_leak"] == "medium"


def test_full_state_field_name_is_medium(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-full", prompt_text="illegalCount=3\n")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_WARN
    assert result.leak_counts.get("full_state_field_leak") == 1


def test_corrupt_jsonl_is_audit_io_and_critical(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-broken", corrupt_log=True)
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.io_errors
    assert result.verdict == A.VERDICT_CRITICAL and result.exit_code == 2


def test_missing_run_dir_raises_audit_io(tmp_path):
    with pytest.raises(InfraError) as exc:
        A.run_audit(tmp_path / "ghost")
    assert exc.value.code == E.E_ENV_AUDIT_IO


def test_missing_seeds_yaml_warns(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-noseeds")
    result = A.run_audit(built.root, config_dir=tmp_path / "empty", generated_at=FIXED)
    assert any("seeds" in w for w in result.warnings)
    assert result.verdict == A.VERDICT_WARN # 规则集降级必须显式可见


def test_replay_degradation_is_visible(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-replay")
    # 破坏重放：把动作改成内核不认识的动作（但保持 JSONL 合法）
    lines = built.episode_path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(x) for x in lines]
    rows[1]["action"] = {"kind": "play_face", "dieId": "d-nope", "slotId": "slot_1"}
    built.episode_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                                  encoding="utf-8")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert any("重放" in w for w in result.warnings)
    assert result.verdict in (A.VERDICT_WARN, A.VERDICT_CRITICAL)


def test_replay_off_skips_hidden_rules(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-noreplay")
    result = A.run_audit(built.root, replay=False, generated_at=FIXED)
    assert result.warnings == () # 关掉重放不算降级（显式选择）
    assert result.verdict == A.VERDICT_CLEAN


def test_self_test_failure_makes_critical(build_run, tmp_path, monkeypatch):
    built = build_run(tmp_path, run_id="r-hp")
    bad = HP.HoneypotSelfTestResult(cases=12, caught=0, missed=())
    monkeypatch.setattr(HP, "self_test", lambda: bad)
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CRITICAL
    assert result.honeypot["caught_rate"] == 0.0


# ---- 退出码两根正交轴：0/1/2 = 泄漏结论；4 = 沙箱/守卫违例（PRD §18.2）------------

def test_exit_code_axes_machinery_beats_verdict(build_run, tmp_path, monkeypatch):
    """蜜罐失效：报告仍 critical（FR-02-23），但进程退出码取 Spec §8.3 的 4。"""
    from dice_ratchet.infra.errors import error_code_of, EXIT_SANDBOX, E

    assert error_code_of(E.E_ENV_HONEYPOT) == EXIT_SANDBOX == 4 # 注册表口径
    built = build_run(tmp_path, run_id="r-axis-hp")
    bad = HP.HoneypotSelfTestResult(cases=12, caught=0, missed=())
    monkeypatch.setattr(HP, "self_test", lambda: bad)
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.verdict == A.VERDICT_CRITICAL # 报告不丢信息
    assert result.machinery_errors == (E.E_ENV_HONEYPOT.value,)
    assert result.exit_code == 4
    assert "守卫违例" in result.exit_reason


def test_exit_code_axes_canary_leak_stays_2(build_run, tmp_path):
    """canary 泄漏是**发现**（finding）而不是守卫违例：退出码仍是 2。"""
    built = build_run(tmp_path, run_id="r-axis-canary")
    text = built.episode_path.read_text(encoding="utf-8")
    built.episode_path.write_text(text.replace('"llm_raw": null',
                                               f'"llm_raw": "{HP.HONEYPOT_CANARY}"', 1),
                                  encoding="utf-8")
    result = A.run_audit(built.root, generated_at=FIXED)
    assert result.leak_counts.get("canary_leak", 0) >= 1
    assert result.machinery_errors == ()
    assert result.exit_code == 2


def test_exit_code_axes_clean_is_0_and_medium_is_1(build_run, tmp_path):
    clean = A.run_audit(build_run(tmp_path, run_id="r-axis-clean").root,
                        generated_at=FIXED)
    assert (clean.exit_code, clean.machinery_errors) == (0, ())
    medium = A.run_audit(build_run(tmp_path, run_id="r-axis-med",
                                   prompt_text="drawPile 的顺序\n").root,
                         generated_at=FIXED)
    assert medium.verdict == A.VERDICT_WARN and medium.exit_code == 1


def test_console_prints_both_axes(build_run, tmp_path, monkeypatch, capsys):
    built = build_run(tmp_path, run_id="r-axis-console")
    bad = HP.HoneypotSelfTestResult(cases=12, caught=0, missed=())
    monkeypatch.setattr(HP, "self_test", lambda: bad)
    result = A.run_audit(built.root, generated_at=FIXED)
    code = A.audit_to_console(result)
    out = capsys.readouterr().out
    assert code == 4
    assert "exit=4" in out and "E_ENV_HONEYPOT" in out and "守卫违例" in out


def test_canary_check_can_be_disabled(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-nohp")
    result = A.run_audit(built.root, self_test=False, generated_at=FIXED)
    assert result.honeypot["caught_rate"] == 1.0
    assert result.verdict == A.VERDICT_CLEAN


# ---- 报告落盘：schema / 权限 / 确定性（NFR-02-3，G5）------------------------------

def test_machinery_errors_reach_the_report(build_run, tmp_path, monkeypatch):
    built = build_run(tmp_path, run_id="r-axis-report")
    bad = HP.HoneypotSelfTestResult(cases=12, caught=0, missed=())
    monkeypatch.setattr(HP, "self_test", lambda: bad)
    payload = A.run_audit(built.root, generated_at=FIXED).to_dict()
    assert payload["machinery_errors"] == ["E_ENV_HONEYPOT"]
    assert payload["verdict"] == "critical"


def test_write_report_is_0600_and_valid(build_run, tmp_path, repo_root):
    jsonschema = pytest.importorskip("jsonschema")
    built = build_run(tmp_path, run_id="r-report")
    result = A.run_audit(built.root, generated_at=FIXED)
    out = A.write_report(result, built.root / "audit" / "leak-leak.json")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    payload = json.loads(out.read_text(encoding="utf-8"))
    schema = json.loads((repo_root / "schemas/audit_report.schema.json")
                        .read_text(encoding="utf-8"))
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["scope"] == "leak" and payload["ruleset_version"] == A.RULESET_VERSION
    assert payload["generated_at"] == FIXED


def test_two_audits_differ_only_in_generated_at(build_run, tmp_path):
    built = build_run(tmp_path, run_id="r-det")
    first = A.run_audit(built.root, generated_at="2026-01-01T00:00:00Z").to_dict()
    second = A.run_audit(built.root, generated_at="2026-01-02T00:00:00Z").to_dict()
    assert first.pop("generated_at") == "2026-01-01T00:00:00Z"
    assert second.pop("generated_at") == "2026-01-02T00:00:00Z"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_report_schema_rejects_extra_field(build_run, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    built = build_run(tmp_path, run_id="r-extra")
    payload = A.run_audit(built.root, generated_at=FIXED).to_dict()
    payload["surprise"] = 1
    schema = json.loads((Path(__file__).resolve().parents[3]
                         / "schemas/audit_report.schema.json").read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_audit_to_console_reports_and_returns_exit_code(build_run, tmp_path, capsys):
    built = build_run(tmp_path, run_id="r-console", prompt_text="seed=731013\n")
    result = A.run_audit(built.root, generated_at=FIXED)
    code = A.audit_to_console(result)
    out = capsys.readouterr().out
    assert code == 2
    assert "verdict=critical" in out and "seed_leak=1" in out and "critical" in out


# ---- MOD-09 Redactor 反向注册 ------------------------------------------------------

def test_register_sensitive_feeds_redactor(build_run, tmp_path):
    from dice_ratchet.infra.logging import Redactor

    built = build_run(tmp_path, run_id="r-red")
    redactor = Redactor()
    count = A.register_sensitive(redactor, run_dir=built.root)
    assert count > 0
    registered = redactor.registered
    assert "731000" in registered and "drawPile" in registered
    assert "1000" not in registered # 本 run 的 train seed 不注册
    redacted, hits = redactor.redact({"text": "seed 731000 与 drawPile"})
    assert hits >= 1 and "731000" not in redacted["text"]
