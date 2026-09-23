#!/usr/bin/env python3
"""Filter oneapi log files and replay matched requests concurrently."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import fnmatch
import http.client
import json
import random
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass
class MatchedRequest:
    source_file: str
    line_no: int
    level: str
    timestamp: str
    request_id: str
    channel_id: str | None
    token_name: str | None
    request_body: str
    request_json: object | None
    raw_payload: dict

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "line_no": self.line_no,
            "level": self.level,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "token_name": self.token_name,
            "request_body": self.request_body,
            "request_json": self.request_json,
            "raw_payload": self.raw_payload,
        }


@dataclass
class ReplayResult:
    index: int
    ok: bool
    status_code: int | None
    elapsed_ms: float
    attempts: int
    retry_wait_ms: float
    request_id: str
    source_file: str
    response_finished: bool = False
    finish_marker: str | None = None
    response_bytes: int = 0
    error: str | None = None
    response_head: str | None = None
    response_tail: str | None = None


@dataclass
class ApproxLatencyStats:
    sample_size: int
    count: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0

    def __post_init__(self) -> None:
        self.sample: list[float] = []

    def add(self, value: float) -> None:
        self.count += 1
        self.sum_ms += value
        self.max_ms = max(self.max_ms, value)

        if self.sample_size <= 0:
            return
        if len(self.sample) < self.sample_size:
            self.sample.append(value)
            return

        replace_at = random.randint(1, self.count)
        if replace_at <= self.sample_size:
            self.sample[replace_at - 1] = value

    def avg(self) -> float:
        if self.count == 0:
            return 0.0
        return self.sum_ms / self.count

    def approx_percentile(self, ratio: float) -> float:
        return percentile(self.sample, ratio)


@dataclass
class ExtractProgress:
    total_files: int = 0
    processed_files: int = 0
    total_bytes: int = 0
    processed_bytes: int = 0
    matched_requests: int = 0
    started_at: float = 0.0
    last_report_at: float = 0.0

    def start(self) -> None:
        now = time.time()
        self.started_at = now
        self.last_report_at = now

    def should_report(self, interval_seconds: float) -> bool:
        return interval_seconds > 0 and (time.time() - self.last_report_at) >= interval_seconds

    def mark_reported(self) -> None:
        self.last_report_at = time.time()


@dataclass
class ReplayProgress:
    submitted_requests: int = 0
    completed_requests: int = 0
    success_requests: int = 0
    failed_requests: int = 0
    started_at: float = 0.0
    last_report_at: float = 0.0

    def start(self) -> None:
        now = time.time()
        self.started_at = now
        self.last_report_at = now

    def should_report(self, interval_seconds: float) -> bool:
        return interval_seconds > 0 and (time.time() - self.last_report_at) >= interval_seconds

    def mark_reported(self) -> None:
        self.last_report_at = time.time()


@dataclass
class ShutdownState:
    signal_count: int = 0
    signal_number: int | None = None
    requested_at: float | None = None

    @property
    def requested(self) -> bool:
        return self.signal_number is not None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Filter oneapi logs by patterns and replay matched requests."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract", help="Extract matched DumpRequest records from log files."
    )
    add_common_filter_args(extract)
    extract.add_argument(
        "--output",
        default="matched_requests.jsonl",
        help="Output JSONL file path. Default: matched_requests.jsonl",
    )
    extract.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON objects instead of JSONL.",
    )
    extract.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="Seconds between extract progress updates. 0 disables progress output. Default: 5",
    )

    replay = subparsers.add_parser(
        "replay",
        help="Replay matched requests concurrently from logs or a JSONL extract file.",
    )
    source_group = replay.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--input-jsonl",
        help="Replay from a JSONL file generated by the extract command.",
    )
    source_group.add_argument(
        "--log-dir",
        help="Replay directly from a log directory using the same filter options.",
    )
    add_common_filter_args(replay)
    replay.add_argument("--url", required=True, help="Replay target URL.")
    replay.add_argument(
        "--method", default="POST", help="HTTP method for replay. Default: POST"
    )
    replay.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header, format: 'Key: Value'. Can be repeated.",
    )
    replay.add_argument(
        "--override-model",
        help="Replace the model field in each replayed JSON request body.",
    )
    replay.add_argument(
        "--concurrency", type=int, default=10, help="Worker count. Default: 10"
    )
    replay.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Replay each matched request N times. Default: 1",
    )
    replay.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Replay only the first N matched requests. 0 means no limit.",
    )
    replay.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP timeout in seconds. Default: 120",
    )
    replay.add_argument(
        "--output",
        default="replay_results.jsonl",
        help="Replay result JSONL file path. Default: replay_results.jsonl",
    )
    replay.add_argument(
        "--flush-every",
        type=int,
        default=1,
        help="Flush replay result output every N emitted records. Default: 1",
    )
    replay.add_argument(
        "--shutdown-drain-timeout",
        type=float,
        default=30.0,
        help="After SIGINT/SIGTERM, wait up to this many seconds for in-flight requests to finish before stopping. Default: 30",
    )
    replay.add_argument(
        "--latency-sample-size",
        type=int,
        default=10000,
        help="Reservoir size for approximate latency percentiles. Default: 10000",
    )
    replay.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="Seconds between replay progress updates. 0 disables progress output. Default: 5",
    )
    replay.add_argument(
        "--response-snippet-bytes",
        type=int,
        default=512,
        help="Bytes to keep from both head and tail of replay responses. Default: 512",
    )
    replay.add_argument(
        "--retry-initial-delay",
        type=float,
        default=5.0,
        help="Initial delay in seconds before retrying a retryable replay failure. Default: 5",
    )
    replay.add_argument(
        "--retry-max-delay",
        type=float,
        default=60.0,
        help="Maximum delay in seconds between retry attempts. Default: 60",
    )
    replay.add_argument(
        "--retry-backoff",
        type=float,
        default=2.0,
        help="Backoff multiplier for retry delay growth. Default: 2",
    )
    replay.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="Maximum retry attempts for retryable failures. 0 means retry forever. Default: 0",
    )

    return parser.parse_args()


def add_common_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        default=".",
        help="Directory containing log files. Default: current directory",
    )
    parser.add_argument(
        "--glob",
        default="oneapi-*",
        help="Filename glob for log files. Default: oneapi-*",
    )
    parser.add_argument(
        "--require-pattern",
        action="append",
        default=[],
        help="Substring that must exist in the line. Can be repeated.",
    )
    parser.add_argument(
        "--token-name",
        help='Shortcut for requiring the substring `"token_name":"<value>"`.',
    )
    parser.add_argument(
        "--start-time",
        help="Inclusive file timestamp lower bound in YYYYMMDDHHMMSS format.",
    )
    parser.add_argument(
        "--end-time",
        help="Inclusive file timestamp upper bound in YYYYMMDDHHMMSS format.",
    )


def resolve_required_patterns(args: argparse.Namespace) -> list[str]:
    patterns = list(args.require_pattern or [])
    if not patterns:
        patterns.append("DumpRequest")
    if args.token_name:
        patterns.append(f'"token_name":"{args.token_name}"')
    return patterns


def validate_time_range(start_time: str | None, end_time: str | None) -> None:
    for value, flag in ((start_time, "--start-time"), (end_time, "--end-time")):
        if value is None:
            continue
        if len(value) != 14 or not value.isdigit():
            raise ValueError(f"{flag} must be in YYYYMMDDHHMMSS format: {value!r}")
    if start_time and end_time and start_time > end_time:
        raise ValueError("--start-time must be <= --end-time")


def extract_file_timestamp(path: Path) -> str | None:
    name = path.name
    prefix = "oneapi-"
    if not name.startswith(prefix):
        return None

    rest = name[len(prefix) :]
    if len(rest) < 14:
        return None
    timestamp = rest[:14]
    if len(timestamp) != 14 or not timestamp.isdigit():
        return None
    return timestamp


def iter_log_files(
    log_dir: str, pattern: str, start_time: str | None = None, end_time: str | None = None
) -> Iterable[Path]:
    base = Path(log_dir)
    if not base.exists():
        raise FileNotFoundError(f"log directory does not exist: {base}")
    if not base.is_dir():
        raise NotADirectoryError(f"not a directory: {base}")

    validate_time_range(start_time, end_time)

    files = []
    for path in base.iterdir():
        if not path.is_file() or not fnmatch.fnmatch(path.name, pattern):
            continue
        file_timestamp = extract_file_timestamp(path)
        if file_timestamp is None:
            continue
        if start_time and file_timestamp < start_time:
            continue
        if end_time and file_timestamp > end_time:
            continue
        files.append(path)
    files.sort()
    return files


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{num_bytes}B"


def print_extract_progress(progress: ExtractProgress, *, final: bool = False) -> None:
    elapsed = max(time.time() - progress.started_at, 0.001)
    rate = progress.processed_bytes / elapsed
    prefix = "[extract]" if not final else "[extract][done]"
    bytes_part = f"{format_bytes(progress.processed_bytes)}/{format_bytes(progress.total_bytes)}"
    if progress.total_files:
        files_part = f"{progress.processed_files}/{progress.total_files}"
    else:
        files_part = str(progress.processed_files)
    percent_part = ""
    if progress.total_bytes > 0:
        percent_part = f" ({progress.processed_bytes * 100.0 / progress.total_bytes:.1f}%)"
    print(
        f"{prefix} files={files_part} bytes={bytes_part}{percent_part} "
        f"matched={progress.matched_requests} rate={format_bytes(int(rate))}/s elapsed={elapsed:.1f}s",
        flush=True,
    )
    progress.mark_reported()


def print_replay_progress(progress: ReplayProgress, in_flight: int, *, final: bool = False) -> None:
    elapsed = max(time.time() - progress.started_at, 0.001)
    throughput = progress.completed_requests / elapsed
    prefix = "[replay]" if not final else "[replay][done]"
    print(
        f"{prefix} submitted={progress.submitted_requests} completed={progress.completed_requests} "
        f"success={progress.success_requests} failure={progress.failed_requests} "
        f"in_flight={in_flight} qps={throughput:.2f} elapsed={elapsed:.1f}s",
        flush=True,
    )
    progress.mark_reported()


def parse_dump_request_line(line: str, source_file: str, line_no: int) -> MatchedRequest | None:
    if "DumpRequest:" not in line:
        return None

    parts = line.rstrip("\n").split(" | ", 2)
    if len(parts) != 3:
        return None

    left, request_id, message = parts
    if not left.startswith("[") or "] " not in left:
        return None

    level_end = left.find("]")
    level = left[1:level_end]
    timestamp = left[level_end + 2 :]

    prefix = "DumpRequest: "
    if prefix not in message:
        return None

    payload_text = message.split(prefix, 1)[1].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None

    request_body = payload.get("requestBody")
    if not isinstance(request_body, str):
        return None

    try:
        request_json = json.loads(request_body)
    except json.JSONDecodeError:
        request_json = None

    return MatchedRequest(
        source_file=source_file,
        line_no=line_no,
        level=level,
        timestamp=timestamp,
        request_id=request_id.strip(),
        channel_id=str(payload.get("channel_id")) if payload.get("channel_id") is not None else None,
        token_name=payload.get("token_name"),
        request_body=request_body,
        request_json=request_json,
        raw_payload=payload,
    )


def line_matches(line: str, required_patterns: list[str]) -> bool:
    return all(pattern in line for pattern in required_patterns)


def scan_logs(
    log_dir: str,
    pattern: str,
    required_patterns: list[str],
    start_time: str | None = None,
    end_time: str | None = None,
    progress: ExtractProgress | None = None,
    progress_interval: float = 0.0,
) -> Iterator[MatchedRequest]:
    files = list(iter_log_files(log_dir, pattern, start_time, end_time))
    if progress is not None:
        progress.total_files = len(files)
        progress.total_bytes = sum(path.stat().st_size for path in files)
        progress.start()
        print_extract_progress(progress)

    for path in files:
        file_size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line_matches(line, required_patterns):
                    if progress is not None and progress.should_report(progress_interval):
                        print_extract_progress(progress)
                    continue
                record = parse_dump_request_line(line, path.name, line_no)
                if record is not None:
                    if progress is not None:
                        progress.matched_requests += 1
                    yield record
                if progress is not None and progress.should_report(progress_interval):
                    print_extract_progress(progress)
        if progress is not None:
            progress.processed_files += 1
            progress.processed_bytes += file_size
            if progress.should_report(progress_interval):
                print_extract_progress(progress)


def write_extract_output(records: Iterable[MatchedRequest], output_path: str, pretty: bool) -> tuple[int, dict[str, int]]:
    path = Path(output_path)
    matched_count = 0
    token_counts: dict[str, int] = {}

    with path.open("w", encoding="utf-8") as handle:
        if pretty:
            handle.write("[\n")
            first = True
            for record in records:
                token = record.token_name or "<missing>"
                token_counts[token] = token_counts.get(token, 0) + 1
                matched_count += 1
                if not first:
                    handle.write(",\n")
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
                first = False
            handle.write("\n]\n")
            return matched_count, token_counts

        for record in records:
            token = record.token_name or "<missing>"
            token_counts[token] = token_counts.get(token, 0) + 1
            matched_count += 1
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    return matched_count, token_counts


def load_extract_jsonl(input_path: str, lean: bool = False) -> Iterator[MatchedRequest]:
    """Yield one MatchedRequest per non-blank JSONL line.

    lean=True drops the two heaviest per-record fields — `request_json` (a parsed
    copy of request_body) and `raw_payload` (the entire original log dict, which
    re-contains the body) — keeping only the raw `request_body` string. On the
    multi-GB replay datasets these parsed structures dominate resident memory
    (parsed JSON is ~3-5x its serialized size, and the whole dataset is held in
    RAM for the run), so the replay runner loads lean. Every consumer that needs
    the parsed body already falls back to json.loads(request_body) when
    request_json is None, so this only trades a little re-parsing CPU for a large
    memory saving. The CLI tool keeps the default (full) load.
    """
    # gzip-aware: a .jsonl and a .jsonl.gz are interchangeable to every caller
    # (see bench/replay_test/jsonl_io.py — collected datasets are stored gzipped,
    # which is ~3.6x smaller on this kind of JSON).
    from bench.replay_test.jsonl_io import open_text

    with open_text(input_path) as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            yield MatchedRequest(
                source_file=data["source_file"],
                line_no=data["line_no"],
                level=data["level"],
                timestamp=data["timestamp"],
                request_id=data["request_id"],
                channel_id=data.get("channel_id"),
                token_name=data.get("token_name"),
                request_body=data["request_body"],
                request_json=None if lean else data.get("request_json"),
                raw_payload={} if lean else data.get("raw_payload", {}),
            )


def build_headers(raw_headers: list[str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for item in raw_headers:
        if ":" not in item:
            raise ValueError(f"invalid header format: {item!r}")
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def _system_text(content) -> str:
    """Flatten a message's content (string or list-of-parts) to plain text."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def normalize_system_messages(messages):
    """Make `messages` acceptable to a strict chat template (Qwen/SGLang).

    Such templates allow at most ONE system message and it must be first, else
    they 400 with "System message must be at the beginning." Captured gateway /
    Claude-Code traffic fans a multi-block system prompt out into several
    consecutive `system` messages (a billing/metadata header, the identity line,
    the instructions) — a lenient backend (Kimi) accepts that, a bare engine does
    not. We:
      - merge consecutive LEADING system messages into a single leading one, and
      - demote any later (non-leading) system message to `user` in place
        (Qwen's template imposes no user/assistant alternation, so a resulting
        run of user turns renders fine).
    Content and token count are preserved; non-system message order is untouched.

    Returns (new_messages, changed). When nothing needs fixing the input list is
    returned unchanged (changed=False) so byte-for-byte replays stay byte-for-byte.
    """
    if not isinstance(messages, list) or not messages:
        return messages, False
    i, leading = 0, []
    while i < len(messages) and isinstance(messages[i], dict) and messages[i].get("role") == "system":
        leading.append(messages[i])
        i += 1
    tail = messages[i:]
    has_stray_system = any(isinstance(m, dict) and m.get("role") == "system" for m in tail)
    if len(leading) <= 1 and not has_stray_system:
        return messages, False
    out: list = []
    if leading:
        merged = "\n\n".join(s for s in (_system_text(m.get("content")) for m in leading) if s)
        out.append({"role": "system", "content": merged})
    for m in tail:
        if isinstance(m, dict) and m.get("role") == "system":
            out.append({**m, "role": "user"})
        else:
            out.append(m)
    return out, True


def request_needs_system_normalization(record: MatchedRequest) -> bool:
    """True if the record's messages would be rewritten by normalize_system_messages
    (i.e. it carries >1 system message or a non-leading one). Cheap: request_json
    is already parsed on the record; falls back to parsing request_body."""
    body = record.request_json if isinstance(record.request_json, dict) else None
    if body is None:
        try:
            body = json.loads(record.request_body)
        except Exception:
            return False
    msgs = body.get("messages") if isinstance(body, dict) else None
    _new, changed = normalize_system_messages(msgs)
    return changed


def build_replay_body(
    record: MatchedRequest,
    override_model: str | None,
    force_stream: bool = False,
    max_generation_tokens: int = 0,
    clean: bool = False,
) -> bytes:
    # Parse the body up front. Besides honoring model override / stream forcing,
    # we need to introspect the `stream` flag so we can inject
    # stream_options.include_usage on streamed requests (see below).
    if isinstance(record.request_json, dict):
        request_json = dict(record.request_json)
    else:
        try:
            parsed = json.loads(record.request_body)
        except json.JSONDecodeError as exc:
            if override_model or force_stream:
                raise ValueError(
                    f"cannot override model for non-JSON request body in request_id={record.request_id}"
                ) from exc
            # Nothing to change and we can't introspect — send verbatim.
            return record.request_body.encode("utf-8")
        if not isinstance(parsed, dict):
            if override_model or force_stream:
                raise ValueError(
                    f"cannot override model for non-object JSON body in request_id={record.request_id}"
                )
            return record.request_body.encode("utf-8")
        request_json = parsed

    if override_model:
        request_json["model"] = override_model
    if force_stream:
        request_json["stream"] = True

    # Backend-compat cleaning (opt-in via `clean`, default off so the replay stays
    # byte-for-byte). Strict chat templates (Qwen/SGLang) reject the multiple /
    # out-of-order system messages that captured gateway traffic carries; collapse
    # them so a bare engine accepts the replay instead of 400-ing.
    if clean:
        new_msgs, changed = normalize_system_messages(request_json.get("messages"))
        if changed:
            request_json["messages"] = new_msgs

    # Cap how many tokens the model may generate, to bound runaway / non-
    # terminating responses. The recorded body is replayed verbatim, so a
    # request whose original max_tokens was huge or absent can otherwise stream
    # for hours (`request_timeout` is a per-read socket timeout, not a total cap).
    # We only ever LOWER the limit — a request that already asked for fewer
    # tokens keeps its smaller value. Clamp whichever field the request uses
    # (`max_completion_tokens` is the newer OpenAI spelling; `max_tokens` is what
    # vLLM/SGLang accept); if neither is present, set max_tokens.
    if max_generation_tokens and max_generation_tokens > 0:
        capped_existing = False
        for key in ("max_completion_tokens", "max_tokens"):
            if key in request_json:
                cur = request_json.get(key)
                # A numeric limit (int OR float; bool excluded) is lowered ONLY if
                # it exceeds the cap — a request that already asked for fewer keeps
                # its smaller value (the "only ever lower" contract; previously a
                # float like 200.0 was force-raised to the cap). A non-numeric value
                # — null ("no limit") or a malformed string — is treated as
                # unbounded and clamped down to the cap.
                if isinstance(cur, bool) or not isinstance(cur, (int, float)):
                    request_json[key] = max_generation_tokens
                elif cur > max_generation_tokens:
                    request_json[key] = max_generation_tokens
                capped_existing = True
        if not capped_existing:
            request_json["max_tokens"] = max_generation_tokens

    # When the request streams, OpenAI-compatible servers (vLLM, SGLang, …) only
    # emit a `usage` block — and therefore prompt_tokens / cached_tokens — on the
    # final chunk if stream_options.include_usage is true. Production-collected
    # requests rarely set it, so without this the replay can only rough-count
    # output tokens from deltas and reports input_tpm / cached_tpm as 0. Force it
    # on (preserving any other stream_options the original request carried).
    if request_json.get("stream"):
        opts = request_json.get("stream_options")
        opts = dict(opts) if isinstance(opts, dict) else {}
        opts["include_usage"] = True
        request_json["stream_options"] = opts
    else:
        # Non-streaming requests already return a full usage block; stream_options
        # is invalid there and some servers 400 on it, so make sure it's absent.
        request_json.pop("stream_options", None)

    return json.dumps(request_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_replay_jobs(
    records: Iterable[MatchedRequest], repeat: int, limit: int
) -> Iterator[tuple[int, MatchedRequest]]:
    if repeat < 1:
        raise ValueError("--repeat must be >= 1")
    if limit < 0:
        raise ValueError("--limit must be >= 0")

    if repeat > 1:
        records = list(records)
        if limit:
            records = records[:limit]
    elif limit:
        records = _limit_records(records, limit)

    index = 0
    for _ in range(repeat):
        for record in records:
            index += 1
            yield index, record


def _limit_records(records: Iterable[MatchedRequest], limit: int) -> Iterator[MatchedRequest]:
    emitted = 0
    for record in records:
        if emitted >= limit:
            return
        emitted += 1
        yield record


def detect_finish_marker(response_text: str) -> tuple[bool, str | None]:
    markers = [
        ("event: message_stop", "anthropic_message_stop"),
        ('"type":"message_stop"', "anthropic_message_stop"),
        ('"type": "message_stop"', "anthropic_message_stop"),
        ("data: [DONE]", "openai_done"),
        ('"finish_reason":"stop"', "openai_finish_reason"),
        ('"finish_reason":"length"', "openai_finish_reason"),
        ('"finish_reason":"content_filter"', "openai_finish_reason"),
        ('"finish_reason":"tool_calls"', "openai_finish_reason"),
        ('"finish_reason":"function_call"', "openai_finish_reason"),
        ('"finish_reason": "stop"', "openai_finish_reason"),
        ('"finish_reason": "length"', "openai_finish_reason"),
        ('"finish_reason": "content_filter"', "openai_finish_reason"),
        ('"finish_reason": "tool_calls"', "openai_finish_reason"),
        ('"finish_reason": "function_call"', "openai_finish_reason"),
        ('"stop_reason":"end_turn"', "anthropic_stop_reason"),
        ('"stop_reason": "end_turn"', "anthropic_stop_reason"),
        ('"stop_reason":"tool_use"', "anthropic_stop_reason"),
        ('"stop_reason": "tool_use"', "anthropic_stop_reason"),
        ('"stop_reason":"max_tokens"', "anthropic_stop_reason"),
        ('"stop_reason": "max_tokens"', "anthropic_stop_reason"),
        ('"stop_reason":"stop_sequence"', "anthropic_stop_reason"),
        ('"stop_reason": "stop_sequence"', "anthropic_stop_reason"),
    ]
    for needle, marker in markers:
        if needle in response_text:
            return True, marker
    return False, None


def is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.RemoteDisconnected,
        ),
    ):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(
        reason,
        (
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.RemoteDisconnected,
        ),
    )


def compute_retry_delay(
    attempt: int, initial_delay: float, max_delay: float, backoff: float
) -> float:
    if initial_delay <= 0:
        return 0.0
    if attempt <= 1:
        delay = initial_delay
    else:
        delay = initial_delay * (backoff ** (attempt - 1))
    if max_delay > 0:
        delay = min(delay, max_delay)
    return max(delay, 0.0)


def replay_once(
    index: int,
    record: MatchedRequest,
    url: str,
    method: str,
    headers: dict[str, str],
    override_model: str | None,
    timeout: float,
    response_snippet_bytes: int,
    retry_initial_delay: float,
    retry_max_delay: float,
    retry_backoff: float,
    max_retries: int,
) -> ReplayResult:
    attempts = 0
    total_retry_wait_seconds = 0.0

    while True:
        attempts += 1
        body = build_replay_body(record, override_model)
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                elapsed_ms = (time.perf_counter() - start) * 1000
                head = body[:response_snippet_bytes].decode("utf-8", errors="replace")
                tail = body[-response_snippet_bytes:].decode("utf-8", errors="replace")
                full_text = body.decode("utf-8", errors="replace")
                response_finished, finish_marker = detect_finish_marker(full_text)
                return ReplayResult(
                    index=index,
                    ok=200 <= response.status < 300,
                    status_code=response.status,
                    elapsed_ms=elapsed_ms,
                    attempts=attempts,
                    retry_wait_ms=total_retry_wait_seconds * 1000,
                    request_id=record.request_id,
                    source_file=record.source_file,
                    response_finished=response_finished,
                    finish_marker=finish_marker,
                    response_bytes=len(body),
                    response_head=head,
                    response_tail=tail,
                )
        except urllib.error.HTTPError as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            error_body_raw = exc.read()
            error_head = error_body_raw[:response_snippet_bytes].decode("utf-8", errors="replace")
            error_tail = error_body_raw[-response_snippet_bytes:].decode("utf-8", errors="replace")
            error_text = error_body_raw.decode("utf-8", errors="replace")
            response_finished, finish_marker = detect_finish_marker(error_text)
            if is_retryable_http_status(exc.code) and (max_retries == 0 or attempts <= max_retries):
                delay = compute_retry_delay(
                    attempts, retry_initial_delay, retry_max_delay, retry_backoff
                )
                print(
                    f"[RETRY] index={index} status={exc.code} attempt={attempts} "
                    f"sleep_s={delay:.1f} source={record.source_file} "
                    f"request_id={record.request_id} error=HTTPError: {exc}",
                    flush=True,
                )
                time.sleep(delay)
                total_retry_wait_seconds += delay
                continue
            return ReplayResult(
                index=index,
                ok=False,
                status_code=exc.code,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
                retry_wait_ms=total_retry_wait_seconds * 1000,
                request_id=record.request_id,
                source_file=record.source_file,
                response_finished=response_finished,
                finish_marker=finish_marker,
                response_bytes=len(error_body_raw),
                error=f"HTTPError: {exc}",
                response_head=error_head,
                response_tail=error_tail,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - start) * 1000
            if is_retryable_exception(exc) and (max_retries == 0 or attempts <= max_retries):
                delay = compute_retry_delay(
                    attempts, retry_initial_delay, retry_max_delay, retry_backoff
                )
                print(
                    f"[RETRY] index={index} status=None attempt={attempts} "
                    f"sleep_s={delay:.1f} source={record.source_file} "
                    f"request_id={record.request_id} error={type(exc).__name__}: {exc}",
                    flush=True,
                )
                time.sleep(delay)
                total_retry_wait_seconds += delay
                continue
            return ReplayResult(
                index=index,
                ok=False,
                status_code=None,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
                retry_wait_ms=total_retry_wait_seconds * 1000,
                request_id=record.request_id,
                source_file=record.source_file,
                error=f"{type(exc).__name__}: {exc}",
            )


def print_extract_summary(matched_count: int, token_counts: dict[str, int], output_path: str) -> None:
    print(f"matched_requests={matched_count}")
    print(f"output={output_path}")
    for token_name in sorted(token_counts):
        print(f"token_name[{token_name}]={token_counts[token_name]}")


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = ratio * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def print_replay_summary(
    total: int,
    success: int,
    failure: int,
    latency_stats: ApproxLatencyStats,
    output_path: str,
) -> None:
    print(f"total_requests={total}")
    print(f"success={success}")
    print(f"failure={failure}")
    print(f"output={output_path}")
    if latency_stats.count:
        print(f"latency_ms_avg={latency_stats.avg():.2f}")
        print(f"latency_ms_p50_approx={latency_stats.approx_percentile(0.50):.2f}")
        print(f"latency_ms_p90_approx={latency_stats.approx_percentile(0.90):.2f}")
        print(f"latency_ms_p95_approx={latency_stats.approx_percentile(0.95):.2f}")
        print(f"latency_ms_max={latency_stats.max_ms:.2f}")
        print(f"latency_sample_size={len(latency_stats.sample)}")


def do_extract(args: argparse.Namespace) -> int:
    required_patterns = resolve_required_patterns(args)
    progress = ExtractProgress()
    records = scan_logs(
        args.dir,
        args.glob,
        required_patterns,
        start_time=args.start_time,
        end_time=args.end_time,
        progress=progress,
        progress_interval=args.progress_interval,
    )
    matched_count, token_counts = write_extract_output(records, args.output, args.pretty)
    print_extract_progress(progress, final=True)
    print_extract_summary(matched_count, token_counts, args.output)
    return 0


def do_replay(args: argparse.Namespace) -> int:
    if args.flush_every < 1:
        raise ValueError("--flush-every must be >= 1")
    if args.shutdown_drain_timeout < 0:
        raise ValueError("--shutdown-drain-timeout must be >= 0")
    if args.input_jsonl:
        records = load_extract_jsonl(args.input_jsonl)
    else:
        required_patterns = resolve_required_patterns(args)
        records = scan_logs(
            args.log_dir,
            args.glob,
            required_patterns,
            start_time=args.start_time,
            end_time=args.end_time,
        )
    headers = build_headers(args.header)
    jobs = make_replay_jobs(records, args.repeat, args.limit)
    total = 0
    success = 0
    failure = 0
    latency_stats = ApproxLatencyStats(sample_size=args.latency_sample_size)
    saw_job = False
    replay_progress = ReplayProgress()
    shutdown_state = ShutdownState()
    replay_progress.start()
    print_replay_progress(replay_progress, 0)

    previous_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, _frame) -> None:
        shutdown_state.signal_count += 1
        if shutdown_state.signal_number is None:
            shutdown_state.signal_number = signum
            shutdown_state.requested_at = time.monotonic()
            signal_name = signal.Signals(signum).name
            print(
                f"[replay] received {signal_name}; stopping new submissions and draining in-flight requests",
                flush=True,
            )
            return
        raise KeyboardInterrupt(f"forced shutdown via {signal.Signals(signum).name}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    def emit_result(handle, result: ReplayResult) -> None:
        nonlocal total, success, failure
        total += 1
        success += int(result.ok)
        failure += int(not result.ok)
        replay_progress.completed_requests += 1
        replay_progress.success_requests += int(result.ok)
        replay_progress.failed_requests += int(not result.ok)
        latency_stats.add(result.elapsed_ms)
        completed_at = datetime.datetime.now().isoformat(timespec="milliseconds")
        elapsed_s = result.elapsed_ms / 1000
        handle.write(
            json.dumps(
                {
                    "index": result.index,
                    "ok": result.ok,
                    "status_code": result.status_code,
                    "completed_at": completed_at,
                    "elapsed_s": round(elapsed_s, 3),
                    "attempts": result.attempts,
                    "retry_wait_ms": round(result.retry_wait_ms, 3),
                    "request_id": result.request_id,
                    "source_file": result.source_file,
                    "response_finished": result.response_finished,
                    "finish_marker": result.finish_marker,
                    "response_bytes": result.response_bytes,
                    "error": result.error,
                    "response_head": result.response_head,
                    "response_tail": result.response_tail,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        if total % args.flush_every == 0:
            handle.flush()
        status = "OK" if result.ok else "FAIL"
        line = (
            f"[{status}] [{completed_at}] index={result.index} status={result.status_code} "
            f"elapsed={elapsed_s:.3f}s attempts={result.attempts} "
            f"retry_wait_ms={result.retry_wait_ms:.2f} source={result.source_file} "
            f"request_id={result.request_id} finished={result.response_finished}"
        )
        if result.finish_marker:
            line += f" finish_marker={result.finish_marker}"
        if result.error:
            line += f" error={result.error}"
        print(line)

    try:
        with Path(args.output).open("w", encoding="utf-8") as output_handle:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                pending: set[concurrent.futures.Future[ReplayResult]] = set()
                job_iter = iter(jobs)

                while True:
                    while not shutdown_state.requested and len(pending) < args.concurrency:
                        try:
                            index, record = next(job_iter)
                        except StopIteration:
                            break
                        saw_job = True
                        replay_progress.submitted_requests += 1
                        future = executor.submit(
                            replay_once,
                            index,
                            record,
                            args.url,
                            args.method,
                            headers,
                            args.override_model,
                            args.timeout,
                            args.response_snippet_bytes,
                            args.retry_initial_delay,
                            args.retry_max_delay,
                            args.retry_backoff,
                            args.max_retries,
                        )
                        pending.add(future)
                        if replay_progress.should_report(args.progress_interval):
                            print_replay_progress(replay_progress, len(pending))

                    if not pending:
                        break

                    if shutdown_state.requested and shutdown_state.requested_at is not None:
                        drain_elapsed = time.monotonic() - shutdown_state.requested_at
                        if drain_elapsed >= args.shutdown_drain_timeout:
                            for future in pending:
                                future.cancel()
                            print(
                                f"[replay] shutdown drain timeout reached after {drain_elapsed:.1f}s; stopping with {len(pending)} in-flight requests unfinished",
                                flush=True,
                            )
                            pending.clear()
                            break

                    done, pending = concurrent.futures.wait(
                        pending,
                        timeout=0.5,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        emit_result(output_handle, future.result())
                    if replay_progress.should_report(args.progress_interval):
                        print_replay_progress(replay_progress, len(pending))

            output_handle.flush()
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)

    if not saw_job:
        print("no matched requests found", file=sys.stderr)
        return 1

    print_replay_progress(replay_progress, 0, final=True)
    print_replay_summary(total, success, failure, latency_stats, args.output)
    if shutdown_state.requested:
        return 128 + shutdown_state.signal_number
    return 0 if failure == 0 else 2


def main() -> int:
    args = parse_args()
    if args.command == "extract":
        return do_extract(args)
    if args.command == "replay":
        return do_replay(args)
    raise ValueError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())

