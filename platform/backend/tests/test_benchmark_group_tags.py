"""Benchmark `group_tags`: the display-grouping tag paths.

The backend keeps grouping deliberately flat — a list of "Top/Mid/Low" strings
on the benchmark row — and the tree is built in the frontend. What the backend
does own is the *shape* of each path, and that is what is asserted here:

1. Top-down: a level cannot exist without the ones above it, so an empty
   segment anywhere ("/Mid", "A//C") is rejected rather than repaired. A
   trailing separator ("A/") is the one editor artefact we tolerate.
2. At most three levels.
3. Canonicalisation is idempotent and forgiving of whitespace/blank rows, and
   drops exact duplicates while keeping first-seen order (the frontend shows a
   benchmark once per group, so a duplicate would be a duplicate card).
4. `BenchmarkUpdate` keeps None = "leave as-is" and [] = "clear", mirroring
   the other optional fields on that schema.

Pure/offline: schema-level only. Persistence and YAML round-trip follow the
same pattern as the other presentation-only fields and are exercised by the
API handlers.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.benchmarks import (
    GROUP_TAG_MAX_COUNT,
    GROUP_TAG_MAX_DEPTH,
    GROUP_TAG_SEGMENT_MAX,
    BenchmarkCreate,
    BenchmarkGroupTagsBulkUpdate,
    BenchmarkUpdate,
    normalize_group_tags,
)


def test_canonicalises_whitespace_and_blank_rows():
    assert normalize_group_tags([" LLM / Serving / vLLM ", "", "   ", "A/"]) == [
        "LLM/Serving/vLLM",
        "A",
    ]


def test_dedupes_keeping_first_seen_order_and_case():
    assert normalize_group_tags(["B", "A", "b", " B ", "A/x"]) == ["B", "A", "b", "A/x"]


def test_idempotent():
    once = normalize_group_tags(["a / b", "c"])
    assert normalize_group_tags(once) == once


@pytest.mark.parametrize("bad", ["/Mid", "A//C", " /A"])
def test_rejects_empty_segments_above_a_used_level(bad):
    with pytest.raises(ValueError, match="every level above"):
        normalize_group_tags([bad])


def test_separators_only_is_a_blank_row():
    # Nothing but separators has no used level, so there is nothing to be
    # "above" — it is an empty row, not a top-down violation.
    assert normalize_group_tags(["//", "/"]) == []


def test_rejects_more_than_three_levels():
    with pytest.raises(ValueError, match=f"at most {GROUP_TAG_MAX_DEPTH} levels"):
        normalize_group_tags(["a/b/c/d"])
    assert normalize_group_tags(["a/b/c"]) == ["a/b/c"]


def test_rejects_over_long_segment_and_too_many_tags():
    with pytest.raises(ValueError, match="longer than"):
        normalize_group_tags(["x" * (GROUP_TAG_SEGMENT_MAX + 1)])
    with pytest.raises(ValueError, match="at most"):
        normalize_group_tags([f"t{i}" for i in range(GROUP_TAG_MAX_COUNT + 1)])


def test_rejects_non_string_entries():
    with pytest.raises(ValueError):
        normalize_group_tags([1])
    with pytest.raises(ValueError):
        normalize_group_tags("a/b")  # a bare string is not a list


def test_none_is_empty():
    assert normalize_group_tags(None) == []


def test_update_schema_none_vs_clear():
    assert BenchmarkUpdate().group_tags is None
    assert BenchmarkUpdate(group_tags=[]).group_tags == []
    assert BenchmarkUpdate(group_tags=[" a / b "]).group_tags == ["a/b"]
    with pytest.raises(ValidationError):
        BenchmarkUpdate(group_tags=["/b"])


def test_create_schema_defaults_untagged():
    body = BenchmarkCreate(slug="x", name="X", modules=[])
    assert body.group_tags == []


# --- bulk re-tagging payload (admin Groups tab) ------------------------------
#
# The Groups tab edits the grouping as a whole and saves the benchmarks it
# changed in one call. Each assignment REPLACES that benchmark's tags, so the
# payload has to be unambiguous: same normalisation as a single update, and no
# two assignments for one benchmark.


def test_bulk_assignments_normalise_per_benchmark():
    body = BenchmarkGroupTagsBulkUpdate.model_validate(
        {
            "assignments": [
                {"benchmark_id": 1, "group_tags": [" LLM / Serving ", "", "LLM/Serving"]},
                {"benchmark_id": 2, "group_tags": []},
            ]
        }
    )
    assert [a.group_tags for a in body.assignments] == [["LLM/Serving"], []]


def test_bulk_rejects_duplicate_benchmark_ids():
    # Two assignments for one benchmark would resolve to whichever was applied
    # last — silently dropping half of what the admin did.
    with pytest.raises(ValidationError, match="duplicate benchmark_id"):
        BenchmarkGroupTagsBulkUpdate.model_validate(
            {
                "assignments": [
                    {"benchmark_id": 7, "group_tags": ["A"]},
                    {"benchmark_id": 7, "group_tags": ["B"]},
                ]
            }
        )


def test_bulk_rejects_a_malformed_path_in_any_assignment():
    with pytest.raises(ValidationError, match="every level above"):
        BenchmarkGroupTagsBulkUpdate.model_validate(
            {
                "assignments": [
                    {"benchmark_id": 1, "group_tags": ["A"]},
                    {"benchmark_id": 2, "group_tags": ["/Mid"]},
                ]
            }
        )


def test_bulk_empty_is_a_no_op_not_an_error():
    # The tab disables Save with nothing to save; a client bug that posts an
    # empty list should change nothing rather than 422.
    assert BenchmarkGroupTagsBulkUpdate.model_validate({"assignments": []}).assignments == []
    assert BenchmarkGroupTagsBulkUpdate().assignments == []
