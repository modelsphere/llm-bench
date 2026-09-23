"""Tests for replay's rolling-dataset selection.

The load-bearing guarantee here is the *first* one: a benchmark saved before
this feature existed has none of the new keys in its params_json, so it must
validate to exactly the old behaviour. Everything else in this feature is
opt-in behind `dataset_source='auto'`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bench.modules import get_module
from bench.replay_test import dataset_feed

CLS = get_module("replay")


def publish(root, profile="daily", build_id="20260804T000000Z", records=2, built_at=None):
    staged = dataset_feed.prepare_staging(root, profile, build_id)
    lines = [json.dumps({
        "source_file": "feed", "line_no": i, "level": "INFO", "timestamp": "",
        "request_id": f"r{i}", "channel_id": None, "token_name": None,
        "request_body": '{"messages":[{"role":"user","content":"hi"}]}',
        "request_json": None, "raw_payload": {},
    }) for i in range(records)]
    staged.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dataset_feed.publish(
        root, profile, staged, build_id=build_id, records=records, sha256="x",
        built_at=built_at or datetime.now(timezone.utc),
    )


# --- backwards compatibility ------------------------------------------------------

def test_legacy_params_json_still_validates_to_fixed():
    """A params_json snapshot from before the feature."""
    legacy = {
        "dataset_path": "/app/dataset/replay/captured.jsonl",
        "concurrency": 5, "max_samples": 100,
    }
    params = CLS.ParamsSchema(**legacy)
    assert params.dataset_source == "fixed"
    assert params.dataset_profile == ""
    assert params.dataset_path == legacy["dataset_path"]


def test_empty_dataset_path_still_allowed_for_fixed():
    """Some benchmarks were saved with an empty path and rely on the worker's
    REPLAY_DATASET_PATH fallback. Rejecting that would break them."""
    params = CLS.ParamsSchema(dataset_path="")
    assert params.dataset_source == "fixed"


def test_auto_does_not_require_a_dataset_path():
    """Under 'auto' the path comes from the profile's current build, so asking
    the admin to type one is a question with no right answer — and while
    dataset_path was schema-required, saving such a benchmark 400'd."""
    assert "dataset_path" not in (CLS.params_schema_json().get("required") or [])
    params = CLS.ParamsSchema(dataset_source="auto", dataset_profile="glm5-daily")
    assert params.dataset_path == ""


def test_dataset_path_is_hidden_under_auto():
    """Hiding is lossless in ModuleParamForm (the stored value survives), so a
    benchmark flipped auto -> fixed gets its old path back."""
    schema = CLS.params_schema_json()["properties"]["dataset_path"]
    assert schema["x-visible-when"] == {"dataset_source": ["fixed"]}


def test_default_params_unchanged_shape():
    defaults = CLS.default_params()
    assert defaults["dataset_source"] == "fixed"
    assert defaults["dataset_path"] == ""


# --- validation --------------------------------------------------------------------

def test_auto_requires_a_profile():
    with pytest.raises(ValueError, match="dataset_profile"):
        CLS.ParamsSchema(dataset_path="", dataset_source="auto")
    with pytest.raises(ValueError, match="dataset_profile"):
        CLS.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="   ")
    CLS.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="daily")


def test_unknown_dataset_source_rejected():
    with pytest.raises(ValueError, match="dataset_source"):
        CLS.ParamsSchema(dataset_path="x", dataset_source="rolling")


def test_dataset_source_is_normalized():
    assert CLS.ParamsSchema(dataset_path="x", dataset_source=" FIXED ").dataset_source == "fixed"


# --- schema surface the admin form renders -------------------------------------------

def test_feed_fields_are_conditionally_visible():
    """dataset_profile / dataset_max_age_hours only make sense for auto, and the
    param form hides them via x-visible-when."""
    schema = CLS.params_schema_json()["properties"]
    assert schema["dataset_source"]["enum"] == ["fixed", "auto"]
    for key in ("dataset_profile", "dataset_max_age_hours"):
        assert schema[key]["x-visible-when"] == {"dataset_source": ["auto"]}


def test_module_declares_dataset_feed_capability():
    fields = CLS.dataset_feed_fields()
    assert fields is not None
    assert (fields.source, fields.profile, fields.path) == (
        "dataset_source", "dataset_profile", "dataset_path"
    )
    assert CLS.accepts_dataset_feed() is True
    assert CLS.descriptor()["supports_dataset_feed"] is True


def test_other_modules_opt_out():
    """The worker's resolution step must be a no-op for everything else."""
    other = get_module("perf_guidellm")
    assert other.dataset_feed_fields() is None
    assert other.accepts_dataset_feed() is False


# --- standalone resolution (the CLI path; the platform resolves in the worker) --------

def test_run_errors_when_profile_has_no_build(tmp_path, monkeypatch):
    """Must NOT silently fall back to REPLAY_DATASET_PATH — replaying an
    unrelated dataset would produce a plausible score that means nothing."""
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    from bench.modules.base import EndpointConfig

    params = CLS.ParamsSchema(dataset_path="", dataset_source="auto", dataset_profile="daily")
    result = CLS().run(
        endpoint=EndpointConfig(api_url="http://127.0.0.1:1", model="m", api_key=""),
        params=params, output_dir=str(tmp_path / "out"),
    )
    assert result.error and "daily" in result.error
    assert "no published build" in result.error


def test_resolve_feed_reads_the_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    build = publish(tmp_path)
    resolved = CLS._resolve_feed("daily")
    assert resolved is not None and resolved.path == build.path


def test_resolve_feed_unknown_profile_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    assert CLS._resolve_feed("nope") is None
    assert CLS._resolve_feed("") is None


def test_stale_build_is_still_resolvable(tmp_path, monkeypatch):
    """Staleness warns, it does not block — availability is the chosen tradeoff,
    and the warning plus the pinned provenance are the mitigation."""
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path))
    publish(tmp_path, built_at=datetime.now(timezone.utc) - timedelta(hours=100))
    resolved = CLS._resolve_feed("daily")
    assert resolved is not None and resolved.age_hours() > 99


def test_a_fixed_path_that_does_not_exist_fails_instead_of_replaying_the_default(tmp_path):
    """Same rule as a rolling feed: a benchmark that names a dataset and cannot
    find it must not quietly score a different one."""
    from bench.modules import get_module
    from bench.modules.base import EndpointConfig

    cls = get_module("replay")
    params = cls.ParamsSchema.model_construct(
        **{**cls.default_params(), "dataset_source": "fixed",
           "dataset_path": "/nonexistent/capture.jsonl"})
    result = cls().run(EndpointConfig(api_url="http://127.0.0.1:9", model="m", api_key=""),
                       params, str(tmp_path))
    assert result.error == "replay dataset not found: /nonexistent/capture.jsonl"


def test_a_relative_fixed_path_resolves_against_the_repository():
    """So one benchmark file works from a checkout and inside the image."""
    import os

    from bench.modules import replay

    assert os.path.isfile(os.path.join(replay._PROJECT_ROOT, "bench/examples/replay-smoke.jsonl"))
