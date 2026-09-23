"""Streaming LogsQL client for a VictoriaLogs store holding gateway bodylogs.

One of the two places a rolling replay dataset can be collected from (see
`app.datasets.sources`); the other is the bodylog listener's own files. Use
this one when the gateway's records are already shipped to VictoriaLogs.

The store holds one row per proxied `/v1/chat/completions` call — the gateway
*bodylog*, flattened by VictoriaLogs into dotted field names with every value a
string (`bench.replay_test.bodylog_convert.unflatten` reverses that). A row looks
like:

    _msg               "glm-5:8060 --> 198.51.100.20:8052 prompt_tokens:14484 TTFT:0.8s status:200"
    _time              "2026-08-04T05:32:00.084Z"
    model              "glm-5"
    status             "200"
    forwarded_to       "http://198.51.100.20:8052"
    uri                "/v1/chat/completions"
    req_body           "{...the full OpenAI chat request...}"
    req_body_truncated "false"
    req_headers.*      per-header fields
    resp_meta.usage.*  prompt_tokens / completion_tokens / cached_tokens / …

API surface used: `POST {base}/select/logsql/query`, form field `query`,
responding with **NDJSON** — one JSON object per line, streamed. Requests are
always bounded by an explicit `| limit N` because a single hour of production
traffic is gigabytes of request bodies.

Two things about LogsQL that are easy to get wrong and are handled here:

- **Filter on fields, not on `_msg`.** `_msg:glm-5` is a token match against the
  log line, and the line for `glm-5.2` contains the same tokens — on live data
  it matched 511k rows for glm-5.2 against 85k genuine glm-5 rows. Every filter
  this module emits is an exact field filter (`model:="glm-5"`).
- **`| limit N` returns an arbitrary N**, not the newest or oldest N. That is
  exactly what a sampler wants, but nothing downstream may assume ordering.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

import requests

from app.datasets.errors import LogSourceError

logger = logging.getLogger(__name__)

QUERY_PATH = "/select/logsql/query"

# Retryable transport/status conditions. A retry re-runs one sub-window query,
# which is bounded by `limit`, so retrying is cheap and idempotent.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class VictoriaLogsError(LogSourceError):
    """A query could not be completed (after retries)."""


class QueryRejected(VictoriaLogsError):
    """The store rejected the query itself (bad LogsQL, bad auth). Never
    retried — the message carries the store's own parse error, which is the
    most useful thing to show an admin editing a profile's filters."""


def escape_value(value: str) -> str:
    """Quote a LogsQL filter value.

    Values are always double-quoted, even when they look word-like: unquoted
    values break on `:` and `/` (`http://198.51.100.20:8052` parses as several
    tokens), and quoting is never wrong.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _fmt_time(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LogFilters:
    """The admin-configurable half of a profile: which rows to collect.

    Every field maps to one exact-match LogsQL clause; empty means "no clause".
    Multiple values within a field are OR-ed, different fields are AND-ed. The
    defaults encode "successfully served, complete, replayable chat traffic",
    which is what a replay dataset needs — a 429 never reached a backend and has
    no timing worth replaying.
    """

    models: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=lambda: ["200"])
    forwarded_to: list[str] = field(default_factory=list)
    uris: list[str] = field(default_factory=lambda: ["/v1/chat/completions"])
    exclude_truncated: bool = True
    # Raw LogsQL, AND-ed with everything above, for filters this shape doesn't
    # cover (e.g. `req_headers.x-app:="cli"`). Admin-authored: it is a query
    # against a read-only store, not an injection surface into anything else,
    # but it CAN produce a query that returns nothing — hence the dry-run probe.
    extra: str = ""

    def clauses(self) -> list[str]:
        out: list[str] = []
        for fieldname, values in (
            ("model", self.models),
            ("status", self.statuses),
            ("forwarded_to", self.forwarded_to),
            ("uri", self.uris),
        ):
            values = [v for v in values if str(v).strip()]
            if not values:
                continue
            if len(values) == 1:
                out.append(f"{fieldname}:={escape_value(values[0])}")
            else:
                joined = " OR ".join(f"{fieldname}:={escape_value(v)}" for v in values)
                out.append(f"({joined})")
        if self.exclude_truncated:
            # A truncated capture is an incomplete request body — unreplayable,
            # so exclude it at the source rather than downloading and dropping it.
            out.append('req_body_truncated:="false"')
        if self.extra.strip():
            out.append(f"({self.extra.strip()})")
        return out


def build_query(
    filters: LogFilters,
    start: datetime,
    end: datetime,
    *,
    limit: int = 0,
    pipe: str = "",
) -> str:
    """`_time:[start, end)` AND every filter clause, plus an optional pipe.

    The time range is written into the query itself rather than passed as
    `start`/`end` HTTP args (both work — verified against the live store) so the
    recorded query string is self-describing: what gets logged is exactly what
    would reproduce the result.
    """
    parts = [f"_time:[{_fmt_time(start)}, {_fmt_time(end)})", *filters.clauses()]
    query = " AND ".join(parts)
    if pipe:
        query = f"{query} | {pipe.lstrip('| ')}"
    if limit > 0:
        query = f"{query} | limit {int(limit)}"
    return query


@dataclass
class VictoriaLogsClient:
    """Thin, streaming, retrying HTTP client. One instance per build."""

    base_url: str
    username: str = ""
    password: str = ""
    bearer_token: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    max_retries: int = 3
    backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        self._session = requests.Session()
        if self.bearer_token:
            self._session.headers["Authorization"] = f"Bearer {self.bearer_token.strip()}"
        elif self.username:
            self._session.auth = (self.username, self.password)
        # Bodies are large and highly compressible JSON; the store gzips them.
        self._session.headers["Accept-Encoding"] = "gzip"

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover - best effort
            pass

    @property
    def query_url(self) -> str:
        return self.base_url.rstrip("/") + QUERY_PATH

    def _post(self, query: str) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(
                    self.query_url,
                    data={"query": query},
                    stream=True,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if resp.status_code < 400:
                    return resp
                detail = resp.text[:500]
                resp.close()
                if resp.status_code not in _RETRY_STATUSES:
                    # A 4xx other than 429 is a bad query — retrying cannot fix
                    # it, and the store's message names the offending token.
                    raise QueryRejected(f"HTTP {resp.status_code}: {detail}")
                last_error = VictoriaLogsError(f"HTTP {resp.status_code}: {detail}")
            if attempt < self.max_retries:
                delay = self.backoff_seconds * (2 ** attempt)
                logger.warning(
                    "[victorialogs] query failed (%s) — retry %d/%d in %.0fs",
                    last_error, attempt + 1, self.max_retries, delay,
                )
                time.sleep(delay)
        raise VictoriaLogsError(f"query failed after {self.max_retries} retries: {last_error}")

    def iter_rows(self, query: str) -> Iterator[dict]:
        """Stream one dict per NDJSON line.

        Un-parseable lines are counted and skipped, never fatal: a single
        malformed row must not lose the whole window. Rows are yielded as they
        arrive, so the caller's sampler bounds memory — the full response is
        never materialised.
        """
        resp = self._post(query)
        bad = 0
        try:
            for line in resp.iter_lines(chunk_size=1 << 20, decode_unicode=False):
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    bad += 1
                    continue
                if isinstance(row, dict):
                    yield row
                else:
                    bad += 1
        finally:
            resp.close()
            if bad:
                logger.warning("[victorialogs] skipped %d un-parseable rows", bad)

    def count(self, filters: LogFilters, start: datetime, end: datetime) -> int:
        """`| stats count()` — cheap enough to run per window before fetching."""
        query = build_query(filters, start, end, pipe="stats count() as n")
        for row in self.iter_rows(query):
            try:
                return int(row.get("n") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def probe(self, filters: LogFilters, start: datetime, end: datetime) -> dict:
        """Connectivity + filter sanity check for the admin UI's "Test" button.

        Returns the matched row count and the field names of one sample row, so
        a misspelled `forwarded_to` shows up as `matched: 0` in the editor
        instead of as an empty dataset hours later.
        """
        matched = self.count(filters, start, end)
        sample_fields: list[str] = []
        sample_prompt_tokens: int | None = None
        if matched:
            for row in self.iter_rows(build_query(filters, start, end, limit=1)):
                sample_fields = sorted(row.keys())
                try:
                    sample_prompt_tokens = int(row.get("prompt_tokens") or 0) or None
                except (TypeError, ValueError):
                    sample_prompt_tokens = None
                break
        return {
            "matched": matched,
            "query": build_query(filters, start, end),
            "sample_fields": sample_fields,
            "sample_prompt_tokens": sample_prompt_tokens,
        }
