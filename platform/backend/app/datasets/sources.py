"""Where a rolling replay dataset's records come from.

A build needs one thing from a source: every gateway bodylog record in a time
window that matches the profile's filters, as the NESTED dict that
`bench.replay_test.bodylog_convert.convert_record` takes. Sampling, conversion,
validation and publishing are the builder's and do not change with the source.

Two sources:

``bodylog_files`` (the default)
    The hourly JSONL files a gateway's bodylog listener writes (line format:
    docs/replay-datasets.md):
    ``<root>/YYYY-MM-DD/HH.jsonl``, with finished days rolled into
    ``<root>/YYYY-MM-DD.tar.gz``. Needs nothing but read access to that
    directory — the collector pod mounts the listener's volume read-only.

``victorialogs``
    A VictoriaLogs store the same records were shipped to, queried with LogsQL.
    Supports ``extra_logsql`` filters the file source cannot express.

Both yield the same records, so a profile moved from one source to the other
builds the same dataset from the same traffic.
"""
from __future__ import annotations

import json
import logging
import os
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bench.replay_test import jsonl_io
from bench.replay_test.bodylog_convert import unflatten

from app.datasets.errors import LogSourceError
from app.datasets.victorialogs import LogFilters, VictoriaLogsClient, build_query

logger = logging.getLogger(__name__)

BODYLOG_FILES = "bodylog_files"
VICTORIALOGS = "victorialogs"
SOURCE_TYPES = (BODYLOG_FILES, VICTORIALOGS)


class LogSource(Protocol):
    def iter_window(
        self, filters: LogFilters, start: datetime, end: datetime, *, limit: int = 0,
    ) -> Iterator[dict]:
        """Every matching record with start <= ts < end, nested. `limit` is a
        hint a source may use to bound network transfer; the builder samples
        whatever it receives, so a source may also ignore it."""

    def describe(self, filters: LogFilters, start: datetime, end: datetime, *, limit: int = 0) -> str:
        """One line for the build log that says exactly what was asked for."""

    def probe(self, filters: LogFilters, start: datetime, end: datetime) -> dict:
        """{matched, query, sample_fields, sample_prompt_tokens} for the admin
        editor's "Test" button — a filter that matches nothing shows up there
        as `matched: 0` rather than hours later as an empty build."""

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# VictoriaLogs
# ---------------------------------------------------------------------------


class VictoriaLogsSource:
    """LogsQL over a VictoriaLogs store. Rows come back flattened with every
    value a string; `unflatten` restores the record the file source yields."""

    def __init__(self, client: VictoriaLogsClient):
        self.client = client

    def iter_window(self, filters, start, end, *, limit=0):
        for row in self.client.iter_rows(build_query(filters, start, end, limit=limit)):
            yield unflatten(row)

    def describe(self, filters, start, end, *, limit=0):
        return build_query(filters, start, end, limit=limit)

    def probe(self, filters, start, end):
        return self.client.probe(filters, start, end)

    def close(self):
        self.client.close()


# ---------------------------------------------------------------------------
# The bodylog listener's files
# ---------------------------------------------------------------------------


def _norm_peer(value: object) -> str:
    """`http://198.51.100.20:8052/` and `198.51.100.20:8052` name the same
    backend. The listener records `forwarded_to` only when an upstream router
    reports the real backend, and `peer` otherwise, in either form."""
    s = str(value or "").strip().rstrip("/")
    if "://" in s:
        s = urlparse(s).netloc or s
    return s.lower()


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _model_of(record: dict) -> str:
    """The raw bodylog line carries the model in the response metadata and the
    request body, not at the top level (VictoriaLogs rows had it there only
    because ingestion added it). Cheapest place first."""
    for candidate in (record.get("model"), (record.get("resp_meta") or {}).get("model")):
        if candidate:
            return str(candidate)
    body = record.get("req_body")
    if isinstance(body, str) and body:
        try:
            return str(json.loads(body).get("model") or "")
        except (ValueError, AttributeError):
            return ""
    return ""


def matches(record: dict, filters: LogFilters) -> bool:
    """The file source's reading of the same filters VictoriaLogs turns into
    LogsQL: values within a field OR-ed, fields AND-ed, empty means no clause."""
    if filters.statuses and str(record.get("status", "")) not in {str(s) for s in filters.statuses}:
        return False
    if filters.uris:
        uri = str(record.get("uri", "")).split("?", 1)[0]
        if uri not in set(filters.uris):
            return False
    if filters.exclude_truncated and str(record.get("req_body_truncated", "")).lower() in ("true", "1"):
        return False
    if filters.forwarded_to:
        wanted = {_norm_peer(v) for v in filters.forwarded_to}
        seen = {_norm_peer(record.get("forwarded_to")), _norm_peer(record.get("peer"))}
        if not wanted & seen:
            return False
    if filters.models and _model_of(record) not in set(filters.models):
        return False
    return True


@dataclass
class BodylogFilesSource:
    """Reads ``<root>/YYYY-MM-DD/HH.jsonl[.gz]`` and ``<root>/YYYY-MM-DD.tar.gz``.

    ``file_timezone`` is the zone the LISTENER names its files in — its chart's
    ``timezone`` value. It decides which files a window touches, not which
    records match: every record is also checked against the window by its own
    ``ts``, so a mismatched zone costs a missed hour at the edges, never a
    record from outside the window.
    """

    root: Path
    file_timezone: str = "UTC"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        try:
            self._tz = ZoneInfo(self.file_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise LogSourceError(f"unknown file timezone {self.file_timezone!r}") from exc

    @classmethod
    def from_url(cls, url: str, file_timezone: str = "UTC") -> "BodylogFilesSource":
        path = urlparse(url).path if url.startswith("file://") else url
        if not path or not os.path.isabs(path):
            raise LogSourceError(
                f"bodylog_files source needs an absolute directory (file:///data/bodylog), got {url!r}"
            )
        return cls(Path(path), file_timezone)

    # -- which files a window touches ----------------------------------------

    def _hours(self, start: datetime, end: datetime) -> Iterator[datetime]:
        hour = start.astimezone(self._tz).replace(minute=0, second=0, microsecond=0)
        stop = end.astimezone(self._tz)
        while hour < stop:
            yield hour
            hour += timedelta(hours=1)

    def _lines_for_hour(self, hour: datetime) -> Iterator[str]:
        day, hh = hour.strftime("%Y-%m-%d"), hour.strftime("%H")
        for name in (f"{hh}.jsonl", f"{hh}.jsonl.gz"):
            path = self.root / day / name
            if path.is_file():
                with jsonl_io.open_text(path) as handle:
                    yield from handle
                return
        archive = self.root / f"{day}.tar.gz"
        if archive.is_file():
            yield from self._lines_from_archive(archive, hh)

    @staticmethod
    def _lines_from_archive(archive: Path, hh: str) -> Iterator[str]:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = os.path.basename(member.name)
                if name not in (f"{hh}.jsonl", f"{hh}.jsonl.gz"):
                    continue
                raw = tar.extractfile(member)
                if raw is None:
                    continue
                if name.endswith(".gz"):
                    import gzip
                    raw = gzip.GzipFile(fileobj=raw)
                for line in raw:
                    yield line.decode("utf-8", errors="replace")

    # -- the protocol ---------------------------------------------------------

    def iter_window(self, filters, start, end, *, limit=0):
        if filters.extra.strip():
            raise LogSourceError("extra_logsql is a VictoriaLogs filter; the bodylog_files source cannot apply it")
        if not self.root.is_dir():
            raise LogSourceError(f"bodylog directory {self.root} does not exist or is not mounted")
        for hour in self._hours(start, end):
            for line in self._lines_for_hour(hour):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                ts = _parse_ts(record.get("ts"))
                if ts is not None and not (start <= ts < end):
                    continue
                if matches(record, filters):
                    yield record
        # `limit` is deliberately ignored: VictoriaLogs uses it to bound network
        # transfer, but here the whole slice is local and the builder's
        # reservoir samples it uniformly. Stopping early would bias every
        # sample towards the start of its slice.

    def describe(self, filters, start, end, *, limit=0):
        return (
            f"bodylog files under {self.root} ({self.file_timezone}) "
            f"[{start.isoformat()}, {end.isoformat()}) "
            f"models={filters.models or '*'} statuses={filters.statuses or '*'} "
            f"uris={filters.uris or '*'} forwarded_to={filters.forwarded_to or '*'} "
            f"exclude_truncated={filters.exclude_truncated}"
        )

    def probe(self, filters, start, end):
        matched, sample = 0, None
        for record in self.iter_window(filters, start, end):
            matched += 1
            if sample is None:
                sample = record
        prompt_tokens = None
        if sample is not None:
            usage = ((sample.get("resp_meta") or {}).get("usage") or {})
            try:
                prompt_tokens = int(usage.get("prompt_tokens") or 0) or None
            except (TypeError, ValueError):
                prompt_tokens = None
        return {
            "matched": matched,
            "query": self.describe(filters, start, end),
            "sample_fields": sorted(sample.keys()) if sample else [],
            "sample_prompt_tokens": prompt_tokens,
        }

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def as_source(obj) -> LogSource:
    """Accept a bare VictoriaLogsClient (or anything with `iter_rows`) where a
    source is expected, wrapping it — so a caller holding a client need not
    know about sources."""
    if hasattr(obj, "iter_window"):
        return obj
    if hasattr(obj, "iter_rows"):
        return VictoriaLogsSource(obj)
    raise TypeError(f"{type(obj).__name__} is not a log source")


def source_for(source_type: str, source_url: str) -> LogSource:
    """Build the source a profile names. Credentials and the file timezone come
    from the environment, never from the profile row — the database holds
    collection policy, the pod holds secrets and mounts."""
    kind = (source_type or BODYLOG_FILES).strip()
    if kind == BODYLOG_FILES:
        return BodylogFilesSource.from_url(
            source_url, os.getenv("REPLAY_FEED_FILE_TZ", "UTC"),
        )
    if kind == VICTORIALOGS:
        return VictoriaLogsSource(VictoriaLogsClient(
            base_url=source_url,
            username=os.getenv("REPLAY_FEED_VL_USERNAME", ""),
            password=os.getenv("REPLAY_FEED_VL_PASSWORD", ""),
            bearer_token=os.getenv("REPLAY_FEED_VL_TOKEN", ""),
        ))
    raise LogSourceError(f"unknown source type {kind!r} (have: {', '.join(SOURCE_TYPES)})")
