"""FR-05-1 / GWT-05-01/02：schema、round-trip、未知字段拒绝、content_sha256 口径（G5）。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dice_ratchet.infra.errors import E, InfraError
from dice_ratchet.memory import (ADD_RESULT_SCHEMA, MEMORY_ENTRY_SCHEMA, MEMORY_TAGS,
                              REFLECTION_ENTRY_SCHEMA, RETRIEVE_RESULT_SCHEMA, AddResult,
                              MemoryEntry, ReflectionEntry, RetrieveResult, SituationKey,
                              validate_memory_entry, validate_reflection_entry)
from dice_ratchet.memory.schemas import content_sha256_of

SCHEMA_FILES = ("memory_entry", "reflection_entry", "retrieve_result", "add_result")


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_file_matches_module_dict(repo_root: Path, name: str) -> None:
    """``schemas/*.schema.json`` 与模块内的权威 dict 必须逐字段一致（防漂移）。"""
    on_disk = json.loads((repo_root / "schemas" / f"{name}.schema.json").read_text("utf-8"))
    module = {"memory_entry": MEMORY_ENTRY_SCHEMA, "reflection_entry": REFLECTION_ENTRY_SCHEMA,
              "retrieve_result": RETRIEVE_RESULT_SCHEMA, "add_result": ADD_RESULT_SCHEMA}[name]
    assert on_disk == module


def test_memory_entry_roundtrip_is_byte_identical(mem_entry) -> None:
    """GWT-05-01：``from_json`` → ``to_json`` 必须逐字节相同。"""
    entry = mem_entry()
    line = entry.to_json()
    again = MemoryEntry.from_json(line).to_json()
    assert again == line
    assert MemoryEntry.from_json(again) == entry


def test_unknown_field_is_rejected(mem_entry) -> None:
    """GWT-05-01：多一个字段 ``"foo": 1`` → 校验失败（additionalProperties=false）。"""
    payload = mem_entry().to_dict()
    payload["foo"] = 1
    with pytest.raises(InfraError) as exc:
        validate_memory_entry(payload)
    assert exc.value.code == E.E_IO_MEMORY_CORRUPT
    assert "foo" in str(exc.value)


def test_missing_required_field_is_rejected(mem_entry) -> None:
    payload = mem_entry().to_dict()
    payload.pop("text_hash")
    with pytest.raises(InfraError):
        validate_memory_entry(payload)


def test_bad_entry_id_pattern_is_rejected(mem_entry) -> None:
    payload = mem_entry().to_dict()
    payload["entry_id"] = "mem-XYZ"
    with pytest.raises(InfraError):
        validate_memory_entry(payload)


def test_reflection_roundtrip_and_unknown_field(make_reflection) -> None:
    line = make_reflection().to_json()
    assert ReflectionEntry.from_json(line).to_json() == line
    payload = json.loads(line)
    payload["extra"] = True
    with pytest.raises(InfraError) as exc:
        validate_reflection_entry(payload)
    assert exc.value.code == E.E_EVAL_SCHEMA


def test_content_sha256_excludes_created_at(mem_entry) -> None:
    """NFR-05-1：``created_at`` 只作审计展示，不计入 ``content_sha256``（G3）。"""
    entry = mem_entry()
    same = entry.with_updates(created_at="2030-01-01T00:00:00Z")
    assert same.content_sha256 == entry.content_sha256
    assert same.compute_content_sha256() == entry.content_sha256
    assert entry.compute_content_sha256() == entry.content_sha256
    changed = entry.with_updates(lesson="换一条完全不同的教训。")
    assert changed.content_sha256 != entry.content_sha256


def test_content_sha256_is_canonical_hash(mem_entry) -> None:
    entry = mem_entry()
    assert entry.content_sha256 == content_sha256_of(entry.content_payload())


def test_situation_key_is_recomputable(mem_entry) -> None:
    entry = mem_entry()
    assert entry.situation.key() == entry.situation_key


def test_unknown_field_rejected_by_from_json(mem_entry) -> None:
    payload = mem_entry().to_dict()
    payload["foo"] = 1
    with pytest.raises(InfraError):
        MemoryEntry.from_json(json.dumps(payload, ensure_ascii=False))


def test_tags_come_from_controlled_vocabulary() -> None:
    assert "low_virtue" in MEMORY_TAGS and "defense" in MEMORY_TAGS
    payload = {"tags": ["not_a_tag"]}
    assert payload["tags"][0] not in MEMORY_TAGS


def test_cross_module_results_validate_against_schema() -> None:
    """G5：``RetrieveResult`` / ``AddResult`` 有 schema 且真实产物可通过校验。"""
    import jsonschema

    result = RetrieveResult(text="", obs_digest="sha256:0", n_candidates=0)
    jsonschema.validate(instance=result.to_dict(), schema=RETRIEVE_RESULT_SCHEMA)
    jsonschema.validate(instance=AddResult(added=1, memory_version=3).to_dict(),
                        schema=ADD_RESULT_SCHEMA)
    jsonschema.validate(instance=SituationKey().to_dict(),
                        schema=MEMORY_ENTRY_SCHEMA["properties"]["situation"])


def test_situation_key_format(mem_entry) -> None:
    key = mem_entry().situation_key
    assert key.startswith("sk-") and len(key) == 11


def test_entry_with_type_prefix_roundtrips(mem_entry) -> None:
    """快照行与 active 行同构（``_type`` 前缀）：解析时剥掉，写回时字节稳定。"""
    line = mem_entry().to_json()
    payload = json.loads(line)
    with_type = json.dumps({"_type": "memory_entry", **payload}, ensure_ascii=False,
                           separators=(",", ":"))
    assert MemoryEntry.from_json(with_type) == MemoryEntry.from_json(line)


def test_wrong_type_prefix_rejected(mem_entry) -> None:
    payload = json.loads(mem_entry().to_json())
    with_type = json.dumps({"_type": "memory_snapshot_header", **payload},
                           ensure_ascii=False)
    with pytest.raises(InfraError):
        MemoryEntry.from_json(with_type)


def test_created_at_is_preserved(mem_entry) -> None:
    entry = replace(mem_entry(), created_at="2026-10-02T09:14:11Z")
    assert MemoryEntry.from_json(entry.to_json()).created_at == "2026-10-02T09:14:11Z"
