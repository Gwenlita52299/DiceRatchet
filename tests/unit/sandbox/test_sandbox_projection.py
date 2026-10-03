"""MOD-02 投影单测（PRD §11；FR-02-1 ~ FR-02-6/3b/24、G5）。

给定-当-则覆盖：确定性、未登记字段丢弃（只记计数）、哨兵替换、CHANGELOG 版本、
``schemaVersion`` 覆盖式注入、assert 模式、类型校验、紧凑渲染、注册表契约。
"""

from __future__ import annotations

import json

import pytest

from gamer_rsi.infra.errors import E, InfraError
from gamer_rsi.sandbox import projection as P
from gamer_rsi.sandbox.honeypot import FAKE_FIELD_NAMES, HONEYPOT_CANARY


def _ctx(**kw):
    base = {"episode_id": "e-001000-g000-train", "decision_index": 7, "stage_count": 6}
    base.update(kw)
    return P.ProjectionCtx(**base)


# ---- FR-02-1 纯函数/确定性 --------------------------------------------------------

def test_project_state_is_deterministic(full_obs):
    a, stat_a = P.project_state(full_obs, ctx=_ctx())
    b, stat_b = P.project_state(full_obs, ctx=_ctx())
    assert P.canonical_json(a) == P.canonical_json(b)
    assert P.obs_digest(a) == P.obs_digest(b)
    assert stat_a == stat_b


def test_project_state_does_not_mutate_source(full_obs):
    before = json.dumps(full_obs, ensure_ascii=False, sort_keys=True)
    P.project_state(full_obs, ctx=_ctx())
    assert json.dumps(full_obs, ensure_ascii=False, sort_keys=True) == before


def test_obs_digest_shape(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    digest = P.obs_digest(obs)
    assert digest.startswith("sha256:") and len(digest) == 71
    assert digest == P.obs_digest(obs) # 幂等


# ---- FR-02-2 未登记字段：丢弃 + 只记计数 -------------------------------------------

def test_unregistered_field_dropped_and_counted(full_obs):
    src = dict(full_obs)
    src["hidden_shop_preview"] = {"secret": True}
    obs, stat = P.project_state(src, ctx=_ctx())
    assert "hidden_shop_preview" not in obs
    assert stat.dropped_hidden >= 1
    # ProjectionStat 无名称字段：任何序列化都不得出现该字段名（防日志反向泄漏）
    assert "hidden_shop_preview" not in repr(stat)
    assert not hasattr(stat, "names") and not hasattr(stat, "dropped")


def test_unregistered_player_subkey_dropped(full_obs):
    src = dict(full_obs)
    src["player"] = {**full_obs["player"], "hiddenBuff": 3}
    obs, stat = P.project_state(src, ctx=_ctx())
    assert "hiddenBuff" not in json.dumps(obs)
    assert stat.dropped_hidden >= 1


# ---- FR-02-3 严禁出现的值：逐字段哨兵替换 ------------------------------------------

SENTINELS = {
    "seed": "SEED-SENTINEL-9f8e",
    "rng": "RNG-SENTINEL-1a2b",
    "future_room": "FUTURE-ROOM-SENTINEL-3c4d",
    "shop_item": "SHOP-SENTINEL-5e6f",
    "hidden_field": "HIDDEN-FIELD-SENTINEL-7a8b",
}


def test_no_forbidden_values_reach_obs(full_obs):
    """隐藏值出现在**源的非登记位置**时，投影必须丢弃（严格模式只记计数）。

    容器级字段（``hand/slots/enemies/map/shop/...``）是整块透传的：GAME
    ``observe(state,"full")`` 本身已是白名单（FR-02-3a 的第一道防线），而容器**内部**
    的隐藏内容由蜜罐注入 + 审计兜底（见 ``test_sandbox_honeypot.py``）。
    """
    src = dict(full_obs)
    src["seed"] = SENTINELS["seed"]
    src["rng"] = SENTINELS["rng"]
    src["runId"] = SENTINELS["rng"]
    src["player"] = {**full_obs["player"], "drawPile": [SENTINELS["hidden_field"]],
                     "dice": {"d-1": {"faces": SENTINELS["future_room"]}}}
    obs, stat = P.project_state(src, ctx=_ctx())
    blob = P.canonical_json(obs).decode("utf-8")
    for name in ("seed", "rng", "hidden_field", "future_room"):
        assert SENTINELS[name] not in blob, f"{name} 泄漏进 obs"
    assert stat.dropped_hidden >= 4


def test_registered_container_is_verbatim_passthrough(full_obs):
    """登记为整块的容器按原值透传（不改名、不改语义，Spec §4.2）。"""
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    for key in ("hand", "slots", "enemies", "toggles", "relics"):
        if key in full_obs:
            assert json.loads(json.dumps(obs[key], sort_keys=True)) == \
                json.loads(json.dumps(full_obs[key], sort_keys=True))


# ---- FR-02-5/6 版本一致性 + 覆盖式注入 --------------------------------------------

def test_schema_version_override(full_obs):
    src = dict(full_obs, schemaVersion="9.9.9")
    obs, stat = P.project_state(src, ctx=_ctx())
    assert obs["schemaVersion"] == P.OBSERVATION_SCHEMA_VERSION
    # 同名源字段被丢弃并计入未登记（FR-02-6）
    assert stat.dropped_hidden >= 1


def test_changelog_matches_registry():
    assert P.check_changelog() == P.OBSERVATION_SCHEMA_VERSION.split("obs-agent-")[-1]


def test_changelog_version_mismatch_raises(tmp_path):
    bad = tmp_path / "OBSERVATION_CHANGELOG.md"
    bad.write_text("# x\n\n| 版本 | 日期 |\n|---|---|\n| v9.9.9 | 2026-01-01 |\n",
                   encoding="utf-8")
    with pytest.raises(InfraError) as exc:
        P.check_changelog(bad)
    assert exc.value.code == E.E_ENV_PROJECTION_VERSION


def test_changelog_missing_raises(tmp_path):
    with pytest.raises(InfraError) as exc:
        P.changelog_version(tmp_path / "nope.md")
    assert exc.value.code == E.E_ENV_PROJECTION_VERSION


# ---- FR-02-24 / FR-02-3a 必填字段与 assert 模式 ------------------------------------

def test_missing_required_field_raises(full_obs):
    src = {k: v for k, v in full_obs.items() if k != "phase"}
    with pytest.raises(InfraError) as exc:
        P.project_state(src, ctx=_ctx())
    assert exc.value.code == E.E_ENV_PROJECTION_MISSING


def test_missing_optional_recorded(full_obs):
    src = {k: v for k, v in full_obs.items() if k != "shop"}
    obs, stat = P.project_state(src, ctx=_ctx())
    assert obs["shop"] is None
    assert "shop" in stat.missing_optional


def test_type_mismatch_raises(full_obs):
    src = dict(full_obs, turn="not-an-int")
    with pytest.raises(InfraError) as exc:
        P.project_state(src, ctx=_ctx())
    assert exc.value.code == E.E_ENV_SCHEMA


def test_unknown_mode_raises(full_obs):
    with pytest.raises(InfraError) as exc:
        P.project_state(full_obs, ctx=_ctx(), mode="loose")
    assert exc.value.code == E.E_ENV_SCHEMA


def test_assert_mode_passes_on_real_obs(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx(), mode=P.MODE_ASSERT)
    assert P.validate_observation(obs) == []


def test_assert_mode_rejects_sentinel_smuggled_through_ctx(full_obs):
    """ctx 也不能夹带哨兵（assert 模式 → E_ENV_SCHEMA）。"""
    ctx = _ctx(last_result={"illegal": True, "reason": f"see {HONEYPOT_CANARY}"})
    with pytest.raises(InfraError) as exc:
        P.project_state(full_obs, ctx=ctx, mode=P.MODE_ASSERT)
    assert exc.value.code == E.E_ENV_SCHEMA


def test_strict_mode_does_not_raise_on_sentinel_ctx(full_obs):
    ctx = _ctx(last_result={"illegal": True, "reason": f"see {HONEYPOT_CANARY}"})
    obs, _ = P.project_state(full_obs, ctx=ctx, mode=P.MODE_STRICT)
    assert HONEYPOT_CANARY in json.dumps(obs) # strict 只记计数；assert 才拦（FR-02-24）


# ---- Spec §4.2 改写：legalActionKinds 去掉 abandon_run ----------------------------

def test_legal_action_kinds_drops_abandon(full_obs):
    src = dict(full_obs, legalActionKinds=["end_turn", "abandon_run", "play_face"])
    obs, _ = P.project_state(src, ctx=_ctx())
    assert obs["legalActionKinds"] == ["end_turn", "play_face"]


# ---- G5 校验器与 schema 契约 -------------------------------------------------------

def test_validate_observation_catches_unknown_and_forbidden(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    broken = dict(obs)
    broken["naughty"] = 1
    broken["seed"] = 1234
    violations = P.validate_observation(broken)
    assert any("naughty" in v for v in violations)
    assert any("seed" in v for v in violations)


def test_freeze_observation_is_read_only(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    frozen = P.freeze_observation(obs)
    with pytest.raises(TypeError):
        frozen["phase"] = "map" # type: ignore[index]


def test_observation_schema_matches_registry(repo_root):
    schema = json.loads((repo_root / "schemas/observation.schema.json")
                        .read_text(encoding="utf-8"))
    registry = set(P.obs_field_paths())
    top = {name.split(".")[0] for name in registry}
    assert set(schema["properties"]) == top
    required_top = {name.split(".")[0] for name in registry
                    if not P.OBSERVATION_SCHEMA_BY_NAME[name].optional}
    assert set(schema["required"]) == required_top
    player_allowed = {n.split(".", 1)[1] for n in registry if n.startswith("player.")}
    assert set(schema["properties"]["player"]["properties"]) == player_allowed
    assert set(schema["properties"]["player"]["required"]) == player_allowed


def test_observation_validates_against_schema(full_obs, repo_root):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((repo_root / "schemas/observation.schema.json")
                        .read_text(encoding="utf-8"))
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    jsonschema.validate(instance=json.loads(P.canonical_json(obs)), schema=schema)


# ---- FR-02-21 注册表不得含蜜罐哨兵 -------------------------------------------------

def test_registry_has_no_honeypot_tokens():
    blob = "\x00".join(f"{s.name}\x00{s.source_path}" for s in P.OBSERVATION_SCHEMA)
    for token in (HONEYPOT_CANARY, *FAKE_FIELD_NAMES):
        assert token not in blob


# ---- FR-02-3b 紧凑渲染 -------------------------------------------------------------

def test_render_compact_deterministic_and_bounded(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    a = P.render_compact(obs)
    b = P.render_compact(obs)
    assert a == b and a.endswith("\n")
    assert "hand(" in a and "slots(" in a and "enemies(" in a and "legal:" in a
    # 未揭示的行折叠为节点数（只展开下一行）
    assert "未揭示" in a or "row" in a
    assert len(a) < 12000


def test_render_compact_shows_last_result(full_obs):
    ctx = _ctx(last_result={"illegal": True, "reason": "语法错误"})
    obs, _ = P.project_state(full_obs, ctx=ctx)
    assert "语法错误" in P.render_compact(obs)


def test_render_compact_covers_shop_and_node_phases(engine, shop_state, player_turn_state):
    """shop / 节点类 phase 的渲染分支（陈列、可拆卸、宝箱/事件/锻造）。"""
    shop_obs, _ = P.project_state(engine.observe(shop_state, "full"), ctx=_ctx())
    text = P.render_compact(shop_obs)
    assert "shop:" in text and "removable=" in text and "price=" in text

    # 非 shop phase 的 map 折叠与 abilities 行
    obs, _ = P.project_state(engine.observe(player_turn_state, "full"), ctx=_ctx())
    text = P.render_compact(obs)
    assert "abilities=" in text and "map:" in text and "reachable=" in text

    # 节点类 phase（event/treasure/forge/shrine/rest/pendingRelic）逐行渲染
    node_obs = dict(obs, event={"title": "迷雾祭坛", "options": [{"optionId": "opt_0"}]},
                    treasure={"offers": [], "skipGold": 12},
                    forge={"price": 30}, shrine={"options": []}, rest={"options": []},
                    pendingRelic={"relicId": "relic_x"})
    node_text = P.render_compact(node_obs)
    for key in ("event:", "treasure:", "forge:", "shrine:", "rest:", "pendingRelic:"):
        assert key in node_text


def test_render_compact_handles_empty_mapping():
    assert P.render_compact({}).startswith("phase=None")


def test_validate_observation_rejects_non_mapping():
    assert P.validate_observation(["not", "a", "mapping"]) == [
        "$: 观测必须是 Mapping，实际 list"]


def test_validate_observation_flags_nested_violations(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    broken = dict(obs)
    broken["hand"] = [{"dieId": "d-1", "__class__": "escape"}]
    violations = P.validate_observation(broken)
    assert any("__class__" in v for v in violations)


def test_validate_observation_flags_canary_value(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    broken = dict(obs, episodeId=f"e-{HONEYPOT_CANARY}")
    assert any("蜜罐哨兵" in v for v in P.validate_observation(broken))


def test_validate_observation_flags_null_required(full_obs):
    obs, _ = P.project_state(full_obs, ctx=_ctx())
    broken = dict(obs, phase=None)
    assert any("不允许为 null" in v for v in P.validate_observation(broken))


def test_project_state_rejects_non_mapping_source():
    with pytest.raises(InfraError) as exc:
        P.project_state(["nope"], ctx=_ctx())
    assert exc.value.code == E.E_ENV_PROJECTION_MISSING


def test_obs_field_names_alias_and_ctx_keys():
    assert P.obs_field_names() == P.obs_field_paths()
    with pytest.raises(InfraError) as exc:
        P.project_state({}, ctx=_ctx(), mode="strict")
    assert exc.value.code == E.E_ENV_PROJECTION_MISSING


def test_unknown_agent_ctx_key_raises(monkeypatch):
    """注册表里出现未登记的 ``@ctx.*`` 键 → E_ENV_SCHEMA（防止静默的 None 字段）。"""
    bogus = P.ObsFieldSpec(name="mystery", source_path="@ctx.mystery", type="string",
                           doc="x", since=P.OBSERVATION_SCHEMA_VERSION)
    monkeypatch.setattr(P, "OBSERVATION_SCHEMA", (bogus, *P.OBSERVATION_SCHEMA))
    with pytest.raises(InfraError) as exc:
        P.project_state({"phase": "map"}, ctx=_ctx())
    assert exc.value.code == E.E_ENV_SCHEMA
