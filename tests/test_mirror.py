"""Tests for the dual-write hook (lodestone_memory/mirror.py).

Covers the pure helpers (frontmatter parser, path matcher, field mapping)
and the DB-touching upsert path. Hook entry-point itself (main()) is
exercised via the upsert tests with a fake stdin payload.
"""
from __future__ import annotations

import json
import io
from pathlib import Path

import pytest

from lodestone_memory import db, memory, mirror


# ---- is_auto_memory_path ----

def test_is_auto_memory_path_matches_canonical_layout():
    p = Path("/home/u/.claude/projects/-home-u-myproj/memory/feedback_x.md")
    assert mirror.is_auto_memory_path(p)


def test_is_auto_memory_path_rejects_index_file():
    p = Path("/home/u/.claude/projects/-home-u-myproj/memory/MEMORY.md")
    assert not mirror.is_auto_memory_path(p)


def test_is_auto_memory_path_rejects_arbitrary_md():
    assert not mirror.is_auto_memory_path(Path("/tmp/note.md"))
    assert not mirror.is_auto_memory_path(Path("/home/u/project/README.md"))


def test_is_auto_memory_path_rejects_files_outside_memory_dir():
    p = Path("/home/u/.claude/projects/-home-u-proj/somewhere-else/x.md")
    assert not mirror.is_auto_memory_path(p)


# ---- parse_frontmatter ----

def test_parse_frontmatter_extracts_kv_pairs():
    text = (
        "---\n"
        "name: My memory\n"
        "description: a one-liner\n"
        "type: feedback\n"
        "---\n"
        "Body content here.\n"
        "Multiple lines.\n"
    )
    fields, body = mirror.parse_frontmatter(text)
    assert fields["name"] == "My memory"
    assert fields["description"] == "a one-liner"
    assert fields["type"] == "feedback"
    assert body == "Body content here.\nMultiple lines."


def test_parse_frontmatter_handles_no_frontmatter():
    text = "Just a body, no frontmatter\n"
    fields, body = mirror.parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_parse_frontmatter_handles_unterminated_block():
    """Malformed: opening --- with no closing → treat all as body."""
    text = "---\nname: orphan\nno closer here\n"
    fields, body = mirror.parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_parse_frontmatter_handles_colons_in_values():
    text = "---\ntype: feedback: nested\n---\nbody\n"
    fields, _ = mirror.parse_frontmatter(text)
    # Partition on first colon — value retains the rest
    assert fields["type"] == "feedback: nested"


# ---- map_to_lodestone_fields ----

def test_map_to_lodestone_fields_prefers_body_over_description():
    fm = {"name": "T", "description": "short desc", "type": "feedback"}
    body = "Long substantive body explaining the insight."
    fields = mirror.map_to_lodestone_fields(fm, body)
    assert fields["title"] == "T"
    assert fields["content"] == body
    assert fields["kind"] == "preference"  # feedback → preference


def test_map_to_lodestone_fields_falls_back_to_description_when_body_empty():
    fm = {"name": "T", "description": "the only content", "type": "project"}
    fields = mirror.map_to_lodestone_fields(fm, "")
    assert fields["content"] == "the only content"
    assert fields["kind"] == "fact"  # project → fact


def test_map_to_lodestone_fields_returns_none_when_no_name():
    fm = {"description": "x", "type": "feedback"}
    assert mirror.map_to_lodestone_fields(fm, "body") is None


def test_map_to_lodestone_fields_returns_none_when_no_content():
    fm = {"name": "T", "type": "feedback"}
    assert mirror.map_to_lodestone_fields(fm, "   \n") is None


def test_map_to_lodestone_fields_unknown_type_falls_back_to_fact():
    fm = {"name": "T", "type": "weirdo"}
    fields = mirror.map_to_lodestone_fields(fm, "body")
    assert fields["kind"] == "fact"


def test_map_to_lodestone_fields_recognizes_native_lodestone_kinds():
    """If a user writes type=gotcha directly, pass it through."""
    fm = {"name": "T", "type": "gotcha"}
    fields = mirror.map_to_lodestone_fields(fm, "body")
    assert fields["kind"] == "gotcha"


# ---- upsert_memory (DB-touching) ----

@pytest.fixture
def temp_conn(tmp_path: Path, fake_embed):
    conn = db.open_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_upsert_memory_creates_when_no_existing(temp_conn):
    action, uuid = mirror.upsert_memory(
        temp_conn,
        source_file="/x/y/z.md",
        project_id="p1",
        project_label="p1-label",
        fields={"title": "T", "content": "C", "kind": "fact"},
    )
    assert action == "created"
    fetched = memory.get(temp_conn, uuid=uuid)
    assert fetched["title"] == "T"


def test_upsert_memory_updates_when_source_file_already_indexed(temp_conn):
    first_action, first_uuid = mirror.upsert_memory(
        temp_conn,
        source_file="/x/y/z.md",
        project_id="p1",
        project_label="p1",
        fields={"title": "Original", "content": "first", "kind": "fact"},
    )
    second_action, second_uuid = mirror.upsert_memory(
        temp_conn,
        source_file="/x/y/z.md",
        project_id="p1",
        project_label="p1",
        fields={"title": "Updated", "content": "rewritten", "kind": "preference"},
    )
    assert first_action == "created"
    assert second_action == "updated"
    assert first_uuid == second_uuid  # same row, no duplicate
    fetched = memory.get(temp_conn, uuid=second_uuid)
    assert fetched["title"] == "Updated"
    assert fetched["content"] == "rewritten"
    assert fetched["kind"] == "preference"


def test_upsert_memory_isolates_by_project(temp_conn):
    """Same source_file in two projects → two separate rows."""
    a_action, a_uuid = mirror.upsert_memory(
        temp_conn,
        source_file="/x/y/z.md",
        project_id="alpha",
        project_label="alpha",
        fields={"title": "T", "content": "C", "kind": "fact"},
    )
    b_action, b_uuid = mirror.upsert_memory(
        temp_conn,
        source_file="/x/y/z.md",
        project_id="beta",
        project_label="beta",
        fields={"title": "T", "content": "C", "kind": "fact"},
    )
    assert a_action == "created"
    assert b_action == "created"
    assert a_uuid != b_uuid


# ---- main() entry-point smoke test ----

def test_main_handles_non_auto_memory_path_as_no_op(monkeypatch, tmp_path, fake_embed):
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "random.txt"),
                       "content": "x"},
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mirror.main()
    assert rc == 0


def test_main_skips_when_tool_is_not_write(monkeypatch):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "/x.md"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert mirror.main() == 0


def test_main_handles_invalid_json_gracefully(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert mirror.main() == 1  # soft error; doesn't raise
