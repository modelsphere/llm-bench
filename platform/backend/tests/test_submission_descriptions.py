"""Unit tests for the submission description fields (summary + markdown detail).

Pure/offline: exercises only the Pydantic schema layer — no network and no DB,
so these run even when the integration Postgres/Redis aren't up (see conftest
for those).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.benchmarks import (
    DESCRIPTION_DETAIL_MAX,
    DESCRIPTION_SUMMARY_MAX,
    SubmissionCreate,
)


def _create(**kwargs) -> SubmissionCreate:
    return SubmissionCreate(
        endpoint_url="https://api.example.com",
        model="m",
        api_key="k",
        **kwargs,
    )


def test_descriptions_default_to_none():
    s = _create()
    assert s.description_summary is None
    assert s.description_detail is None


def test_descriptions_are_stripped():
    s = _create(
        description_summary="  vLLM + FP8 KV-cache  ",
        description_detail="\n## Setup\n- stuff\n",
    )
    assert s.description_summary == "vLLM + FP8 KV-cache"
    assert s.description_detail == "## Setup\n- stuff"


def test_whitespace_only_descriptions_become_none():
    # Whitespace-only input must store as NULL so views can key off "is None".
    s = _create(description_summary="   ", description_detail="\n\t ")
    assert s.description_summary is None
    assert s.description_detail is None


def test_summary_at_cap_accepted():
    s = _create(description_summary="x" * DESCRIPTION_SUMMARY_MAX)
    assert s.description_summary == "x" * DESCRIPTION_SUMMARY_MAX


def test_summary_over_cap_rejected():
    with pytest.raises(ValidationError, match="description_summary"):
        _create(description_summary="x" * (DESCRIPTION_SUMMARY_MAX + 1))


def test_detail_over_cap_rejected():
    with pytest.raises(ValidationError, match="description_detail"):
        _create(description_detail="x" * (DESCRIPTION_DETAIL_MAX + 1))


def test_summary_cap_applies_after_strip():
    # Padding whitespace around an at-cap summary must not trip the cap.
    padded = "  " + "x" * DESCRIPTION_SUMMARY_MAX + "  "
    s = _create(description_summary=padded)
    assert s.description_summary == "x" * DESCRIPTION_SUMMARY_MAX
