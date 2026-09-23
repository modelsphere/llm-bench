"""Downloading a build's dataset file off the datasets volume.

Pure-logic: the file locator, the "is it still on disk" flag the UI keys off,
and the download endpoint's out-of-band (?token=) authorization. No DB, no
collector, no HTTP server — the parts that decide *which bytes* and *who* are
exactly the parts worth pinning.
"""
from __future__ import annotations

import json

import pytest

from bench.replay_test import dataset_feed, jsonl_io
from app.api import replay_datasets as api
from app.db.models import UserRole


def _make_build(root, profile="daily", build_id="20260804T071349Z", compress=True):
    path = dataset_feed.builds_dir(root / "auto", profile) / \
        f"{build_id}{jsonl_io.dataset_suffix(compress)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_io.open_text(path, "w") as handle:
        handle.write(json.dumps({"request_id": "r0"}) + "\n")
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path / "auto"))
    monkeypatch.setenv("REPLAY_FROZEN_ROOT", str(tmp_path / "frozen"))
    return tmp_path


class _Req:
    """Just enough of a Request for the header fallback."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def test_build_file_locates_and_misses(roots):
    source = _make_build(roots)
    assert api._build_file("daily", "20260804T071349Z") == source
    # A pruned build: the row would still exist, the bytes do not.
    assert api._build_file("daily", "20260101T000000Z") is None
    assert api._build_file("other-profile", "20260804T071349Z") is None


def test_build_file_plain_and_gzipped(roots):
    plain = _make_build(roots, profile="plain", build_id="20260804T080000Z",
                        compress=False)
    assert plain.name.endswith(".jsonl")
    assert api._build_file("plain", "20260804T080000Z") == plain


def test_existing_build_ids_reflects_what_is_on_disk(roots):
    _make_build(roots, build_id="20260804T071349Z")
    _make_build(roots, build_id="20260805T071349Z", compress=False)
    assert api._existing_build_ids("daily") == {
        "20260804T071349Z", "20260805T071349Z",
    }
    # A profile that never published, and one whose name could never be a
    # directory here, both answer "nothing" rather than raising.
    assert api._existing_build_ids("never-built") == set()
    assert api._existing_build_ids("../escape") == set()


@pytest.mark.asyncio
async def test_download_auth_rejects_missing_and_bad_credentials():
    from fastapi import HTTPException

    for token, headers in (
        (None, {}),
        ("not-a-jwt", {}),
        (None, {"authorization": "Bearer not-a-jwt"}),
    ):
        with pytest.raises(HTTPException) as exc:
            await api._download_admin(_Req(headers), token, None)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_download_auth_rejects_non_admin(monkeypatch):
    from fastapi import HTTPException

    class _User:
        role = UserRole.USER

    async def _resolve(token, session):
        return _User()

    monkeypatch.setattr(api, "resolve_user_from_token", _resolve)
    with pytest.raises(HTTPException) as exc:
        await api._download_admin(_Req(), "whatever", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_download_auth_accepts_admin_from_query_or_header(monkeypatch):
    seen = []

    class _User:
        role = UserRole.ADMIN

    async def _resolve(token, session):
        seen.append(token)
        return _User()

    monkeypatch.setattr(api, "resolve_user_from_token", _resolve)
    assert (await api._download_admin(_Req(), "query-token", None)).role is UserRole.ADMIN
    # No query token: the Authorization header is the fallback, minus its scheme.
    await api._download_admin(_Req({"authorization": "Bearer header-token"}), None, None)
    assert seen == ["query-token", "header-token"]
