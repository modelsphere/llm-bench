"""Importing a dataset file as a frozen dataset, and downloading a frozen one.

Together they move a dataset between installations: download a build (or a
frozen dataset) from one, import it into another, and benchmarks there replay
exactly the same requests. Pure-logic like the download tests: the store
functions directly, and the endpoints with a stub request that streams chunks.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bench.replay_test import dataset_feed
from app.api import replay_datasets as api


def _records(n=3) -> bytes:
    return b"".join(
        (json.dumps({
            "source_file": "f", "line_no": i, "level": "INFO",
            "timestamp": "2026-08-30T14:57:21Z", "request_id": f"r{i}",
            "request_body": json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
        }) + "\n").encode()
        for i in range(n)
    )


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_FEED_ROOT", str(tmp_path / "auto"))
    monkeypatch.setenv("REPLAY_FROZEN_ROOT", str(tmp_path / "frozen"))
    return tmp_path


def _staged(roots, data: bytes):
    staging = roots / "frozen" / dataset_feed.STAGING_DIRNAME
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / "upload.part"
    path.write_bytes(data)
    return path


# -- the store -------------------------------------------------------------------


@pytest.mark.parametrize("compressed", [True, False])
def test_an_import_is_a_frozen_dataset_the_runner_can_read(roots, compressed):
    content = _records(3)
    staged = _staged(roots, gzip.compress(content) if compressed else content)
    dataset = dataset_feed.import_frozen(
        None, "prod-q36", staged, compressed=compressed,
        source_profile="qwen-triggered", source_build_id="20260830T145721Z",
        frozen_by="admin",
    )
    assert dataset.path.endswith(".jsonl.gz" if compressed else ".jsonl")
    assert dataset.records == 3
    # The hash of the uncompressed content, as a build records it, so an
    # imported build keeps the hash it had where it was built.
    assert dataset.sha256 == hashlib.sha256(content).hexdigest()
    assert not staged.exists()

    from bench.replay_test.log_replay_tool import load_extract_jsonl
    assert len(list(load_extract_jsonl(dataset.path))) == 3
    [listed] = dataset_feed.list_frozen(None)
    assert (listed.name, listed.origin, listed.source_build_id) == (
        "prod-q36", "upload", "20260830T145721Z")


def test_a_file_that_is_not_a_replay_dataset_is_refused_and_left_nowhere(roots):
    bad = _records(2) + b'{"request_id": "r9"}\n'
    staged = _staged(roots, bad)
    with pytest.raises(ValueError, match="line 3 has no source_file"):
        dataset_feed.import_frozen(None, "bad", staged, compressed=False)
    assert not staged.exists()
    assert dataset_feed.list_frozen(None) == []

    staged = _staged(roots, b"not gzip at all")
    with pytest.raises(ValueError, match="not a readable gzip file"):
        dataset_feed.import_frozen(None, "bad", staged, compressed=True)


def test_an_import_never_replaces_a_frozen_dataset(roots):
    dataset_feed.import_frozen(None, "once", _staged(roots, _records()), compressed=False)
    staged = _staged(roots, _records(5))
    with pytest.raises(FileExistsError):
        dataset_feed.import_frozen(None, "once", staged, compressed=False)
    assert not staged.exists()
    assert dataset_feed.resolve_frozen(None, "once").records == 3


# -- the endpoints -------------------------------------------------------------


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Upload:
    """A request whose body arrives in chunks."""

    def __init__(self, body: bytes, size: int = 7):
        self.body, self.size, self.headers = body, size, {}

    async def stream(self):
        for i in range(0, len(self.body), self.size):
            yield self.body[i:i + self.size]
        yield b""


@pytest.fixture
def as_admin(monkeypatch):
    async def _admin(request, token, session):
        return SimpleNamespace(id=1, username="ops")

    monkeypatch.setattr(api, "_download_admin", _admin)
    monkeypatch.setattr(api, "_async_session_factory", _Session)


@pytest.mark.asyncio
async def test_the_import_endpoint_streams_a_file_into_a_frozen_dataset(roots, as_admin):
    content = _records(4)
    out = await api.import_frozen_dataset(
        "from-prod", _Upload(gzip.compress(content)), source_build_id="20260830T145721Z")
    assert (out.records, out.origin, out.frozen_by) == (4, "upload", "ops")
    assert out.path.endswith("from-prod.jsonl.gz")  # told apart by its first bytes
    assert out.sha256 == hashlib.sha256(content).hexdigest()
    staging = roots / "frozen" / dataset_feed.STAGING_DIRNAME
    assert not any(staging.iterdir())

    # Downloading it gives back the same bytes.
    response = await api.download_frozen("from-prod", _Upload(b""))
    with open(response.path, "rb") as handle:
        assert gzip.decompress(handle.read()) == content

    with pytest.raises(HTTPException) as taken:
        await api.import_frozen_dataset("from-prod", _Upload(content))
    assert taken.value.status_code == 409


@pytest.mark.asyncio
async def test_the_import_endpoint_refuses_what_it_cannot_use(roots, as_admin, monkeypatch):
    for name, body, code in (
        ("Bad Name", _records(), 422),
        ("empty", b"", 422),
        ("not-replay", b'{"hello": 1}\n', 422),
    ):
        with pytest.raises(HTTPException) as refused:
            await api.import_frozen_dataset(name, _Upload(body))
        assert refused.value.status_code == code, name

    monkeypatch.setattr(api, "_IMPORT_MAX_BYTES", 10)
    with pytest.raises(HTTPException) as too_big:
        await api.import_frozen_dataset("big", _Upload(_records()))
    assert too_big.value.status_code == 413
    assert dataset_feed.list_frozen(None) == []
    assert not any((roots / "frozen" / dataset_feed.STAGING_DIRNAME).iterdir())


@pytest.mark.asyncio
async def test_downloading_a_missing_frozen_dataset_is_a_404(roots, as_admin):
    with pytest.raises(HTTPException) as missing:
        await api.download_frozen("nope", _Upload(b""))
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_without_a_collector_the_backend_deletes_a_frozen_dataset(roots, as_admin, monkeypatch):
    # An installation with a datasets volume but no collector imports frozen
    # datasets through the backend, so it must be able to remove them too.
    monkeypatch.setattr(api, "collector_base_url", lambda: "")
    await api.import_frozen_dataset("gone-soon", _Upload(_records()))
    await api.delete_frozen_dataset("gone-soon", force=True, _admin=None, session=None)
    assert dataset_feed.list_frozen(None) == []
    assert not list((roots / "frozen").glob("gone-soon*"))
