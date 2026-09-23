"""Worker-log retention across re-runs, and the size bound that guards it.

A submission can run more than once (Dramatiq retry, or a broker redelivery
after an OOM/SIGKILL). Every flush REPLACES submissions.worker_log_gz, so
without the carry-forward in log_capture the retry's first flush erased the
crashed attempt's log — the one worth reading — within ~30s of restarting.

Retention alone would be a slow leak: naively appending each attempt's log would
let the blob grow without bound across retries. These tests pin BOTH halves —
history survives, and the total never crosses WORKER_LOG_MAX_BYTES.

Pure unit tests: they drive the buffer/budget arithmetic directly and never
touch Postgres, Redis or a bench module.
"""
from __future__ import annotations

import pytest

from app.queue import log_capture as lc
from app.queue.log_capture import (
    BoundedLogBuffer,
    _build_carry_prefix,
    _carry_budget,
    _nbytes,
    _tail_lines_within,
)


def _lines(tag: str, n: int) -> str:
    return "".join(f"[{tag}] line {i} {'x' * 80}\n" for i in range(n))


# --------------------------------------------------------------------------- #
# The bound
# --------------------------------------------------------------------------- #
def test_carried_history_is_charged_against_the_cap():
    cap = 200_000
    prefix = _lines("old", 5_000)  # far larger than the cap on its own
    buf = BoundedLogBuffer(cap, prefix=prefix, prefix_truncated=False)
    for i in range(5_000):
        buf.append(f"[new] line {i}\n")

    text, truncated = buf.getvalue()
    assert _nbytes(text) <= cap
    assert truncated, "dropping lines on both sides must be reported as truncated"


def test_live_attempt_keeps_its_floor_however_big_the_history():
    cap = 4 * 1024 * 1024
    buf = BoundedLogBuffer(cap, prefix=_lines("old", 200_000))
    assert buf.max_bytes >= lc._MIN_LIVE_BYTES


def test_repeated_reruns_do_not_grow_the_blob():
    """The invariant that makes retention safe: attempt N costs no more than 2."""
    cap = 300_000
    stored = ""
    sizes = []
    for attempt in range(1, 11):
        prefix, _trimmed = _build_carry_prefix(stored, _carry_budget(cap))
        buf = BoundedLogBuffer(cap, prefix=prefix)
        buf.append(f"===== attempt {attempt} =====\n")
        for i in range(4_000):
            buf.append(f"[attempt {attempt}] line {i} {'y' * 60}\n")
        stored, _truncated = buf.getvalue()
        sizes.append(_nbytes(stored))

    assert max(sizes) <= cap
    # Steady state, not a creeping climb: every attempt from the second on lands
    # in the same size band.
    assert max(sizes[1:]) - min(sizes[1:]) < cap * 0.05


# --------------------------------------------------------------------------- #
# The retention
# --------------------------------------------------------------------------- #
def test_previous_attempt_survives_into_the_next_log():
    cap = 500_000
    crash = _lines("attempt-1", 20) + "[attempt-1] MemoryError: killed\n"
    prefix, trimmed = _build_carry_prefix(crash, _carry_budget(cap))
    assert not trimmed, "a small crash log must be carried whole"

    buf = BoundedLogBuffer(cap, prefix=prefix)
    buf.append("===== attempt 2 =====\n")
    text, _ = buf.getvalue()

    assert "MemoryError: killed" in text
    assert lc._CARRY_SEPARATOR in text
    assert text.index("MemoryError") < text.index("attempt 2"), "history comes first"


def test_oversized_history_keeps_the_tail_and_says_so():
    """The end of a crashed run is the part that explains the crash."""
    prior = _lines("early", 2_000) + "[late] the actual crash\n"
    prefix, trimmed = _build_carry_prefix(prior, 20_000)

    assert trimmed
    assert "[late] the actual crash" in prefix
    assert "[early] line 0 " not in prefix
    assert lc._CARRY_ELIDED in prefix
    assert _nbytes(prefix) <= 20_000


def test_truncation_flag_propagates_from_a_prior_truncated_log():
    buf = BoundedLogBuffer(500_000, prefix="old\n", prefix_truncated=True)
    buf.append("new\n")
    _text, truncated = buf.getvalue()
    assert truncated, "a run whose carried history was already clipped is truncated"


# --------------------------------------------------------------------------- #
# Degenerate inputs must not raise on the actor's path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("budget", [0, -1, 10])
def test_tiny_budget_carries_nothing_rather_than_failing(budget):
    prefix, _ = _build_carry_prefix(_lines("old", 50), budget)
    assert prefix == ""


def test_retention_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(lc, "WORKER_LOG_CARRY_FRACTION", 0.0)
    assert _carry_budget(4 * 1024 * 1024) == 0
    assert _build_carry_prefix(_lines("old", 10), _carry_budget(4 * 1024 * 1024)) == ("", False)


def test_tail_never_splits_a_line():
    text = "aaaa\nbbbb\ncccc\n"
    assert _tail_lines_within(text, 10) == "bbbb\ncccc\n"
    assert _tail_lines_within(text, 3) == "", "a line that cannot fit is dropped whole"


# --------------------------------------------------------------------------- #
# End-to-end through the real persist/reload path
#
# The regression lived in the round trip, not in either half: _persist REPLACES
# the column, so only reloading it on entry keeps a crashed attempt readable.
# A fake session stands in for Postgres so this runs without infrastructure —
# it is the column read/write that matters here, not the engine.
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self) -> None:
        self.gz: bytes | None = None
        self.truncated = False


class _FakeUpdate:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def filter_by(self, **_kw):
        return self

    def update(self, values, synchronize_session=False):
        for col, val in values.items():
            setattr(self.store, col.key.replace("worker_log_", ""), val)


class _FakeSession:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def execute(self, _stmt):
        row = (self.store.gz, self.store.truncated)
        return type("R", (), {"one_or_none": lambda _s: row})()

    def query(self, _entity):
        return _FakeUpdate(self.store)

    def commit(self): ...
    def rollback(self): ...
    def close(self): ...


@pytest.fixture()
def stored_log(monkeypatch):
    import app.db.models as models

    store = _FakeStore()
    monkeypatch.setattr(models, "get_sync_session", lambda: _FakeSession(store))
    return store


def _read(store: _FakeStore) -> str:
    import gzip

    assert store.gz is not None, "nothing was persisted"
    return gzip.decompress(store.gz).decode("utf-8")


def _run_attempt(sid: int, marker: str, message: str, cap: int) -> None:
    import logging

    # flush_interval is parked so only the final flush writes — the timer would
    # just repeat the same assertion mid-test.
    with lc.capture_submission_logs(sid, marker, max_bytes=cap, flush_interval=3600):
        logging.getLogger("bench.fake").error(message)


def test_a_retry_no_longer_erases_the_crashed_attempts_log(stored_log):
    _run_attempt(1, "===== attempt 1/3 =====", "MemoryError: worker killed", 500_000)
    assert "MemoryError: worker killed" in _read(stored_log)

    _run_attempt(1, "===== attempt 2/3 =====", "second attempt running", 500_000)

    text = _read(stored_log)
    assert "MemoryError: worker killed" in text, "the crash log must survive the re-run"
    assert "second attempt running" in text
    assert text.index("attempt 1/3") < text.index("attempt 2/3"), "oldest first"


def test_the_blob_stays_bounded_across_three_attempts(stored_log):
    cap = 150_000
    for attempt in range(1, 4):
        import logging

        with lc.capture_submission_logs(
            1, f"===== attempt {attempt}/3 =====", max_bytes=cap, flush_interval=3600
        ):
            for i in range(3_000):
                logging.getLogger("bench.fake").error(f"attempt {attempt} line {i}")
        assert _nbytes(_read(stored_log)) <= cap

    text = _read(stored_log)
    assert "attempt 3/3" in text
    assert "attempt 3 line 2999" in text, "the newest attempt is never the one dropped"


def test_an_unreadable_prior_log_degrades_instead_of_failing(monkeypatch):
    import app.db.models as models

    store = _FakeStore()

    def _boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr(models, "get_sync_session", _boom)
    monkeypatch.setattr(lc, "PERSIST_RETRY_BACKOFF", 0)

    # The run itself must not notice: no exception escapes the context manager.
    _run_attempt(1, "===== attempt 2/3 =====", "still running", 500_000)
    assert store.gz is None  # persist failed too, and was swallowed


def test_a_tiny_cap_still_keeps_the_attempt_header(stored_log):
    """The header is unevictable, but it must also never be squeezed out."""
    _run_attempt(1, "===== attempt 1/3 =====", "boom", 4_000)
    _run_attempt(1, "===== attempt 2/3 =====", "retrying", 4_000)

    text = _read(stored_log)
    assert _nbytes(text) <= 4_000
    assert "attempt 2/3" in text, "you must always be able to tell which attempt this is"


def test_the_attempt_header_survives_a_chatty_run(stored_log):
    """Regression: as an ordinary append it was the first line evicted."""
    import logging

    with lc.capture_submission_logs(
        1, "===== attempt 2/3 =====", max_bytes=120_000, flush_interval=3600
    ):
        for i in range(3_000):
            logging.getLogger("bench.fake").error(f"noisy line {i} {'z' * 60}")

    text = _read(stored_log)
    assert "attempt 2/3" in text
    assert "noisy line 2999" in text
