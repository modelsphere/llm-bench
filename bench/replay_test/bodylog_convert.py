"""Bodylog capture → replay JSONL conversion core.

The *bodylog* format (one JSON object per line) is what the LLM gateway
emits when it mirrors live `/v1/chat/completions` traffic — it records the full
request AND response plus timing/headers/usage:

    {"req_body": "{...openai chat request...}", "req_body_truncated": false,
     "req_headers": {...}, "resp_body": "...", "resp_meta": {...usage...},
     "request_id": "...", "ts": "2026-06-24T23:54:50.302+08:00", "status": 200,
     "uri": "/v1/chat/completions", "rt": 325.9, "first_chunk_t": 0.33, ...}

The replay test (`log_replay_tool.load_extract_jsonl` / `ReplayModule`)
instead expects the oneapi-style MatchedRequest schema:

    {"source_file","line_no","level","timestamp","request_id","channel_id",
     "token_name","request_body","request_json","raw_payload"}

This module rewrites bodylog → MatchedRequest, sending only the request side to
the live target (the replay measures the *target's* fresh response). It is
faithful by default: the recorded `req_body` becomes `request_body` verbatim, and
all other original fields are preserved under `raw_payload`.

Two callers share this code and MUST keep sharing it — the `--clean` rules below
are a long list of hard-won "a strict backend 4xxs on what the gateway accepted"
fixes, and a second copy would drift:

  - `scripts/convert_bodylog_dataset.py` — the offline CLI (file → file);
  - `app/datasets/builder.py` — the platform's rolling dataset collector, which
    feeds records straight from VictoriaLogs (see `unflatten`).

Note `scripts/` is NOT copied into the backend image; `bench/` is. That is why
the core lives here rather than in the script.

--clean mode
------------
Opt-in sanitation for replaying against a strict OpenAI-compatible backend
(SGLang/vLLM) — production gateways accept request shapes that a bare engine
4xx/5xxs on. Requires a context budget (the TARGET's window, minus headroom).
What it does, in order:

  drop  records whose ORIGINAL capture response was a 4xx — production itself
        rejected the request (over-context, malformed, …), so it is not a
        useful perf sample and will typically fail again;
  drop  records with an image that cannot be sent: external http(s) URL (the
        target would have to fetch it — fails on airgapped servers), a data:
        URI whose MIME is not image/* (e.g. a base64-embedded object-storage
        404 XML observed in real captures), or an image/* data: URI whose
        payload does not decode to a known image format;
  drop  records whose prompt exceeds the target's context window: measured by
        the capture's usage.prompt_tokens when present, else estimated from
        the request-body size (UTF-8 bytes / 3 — slightly conservative for
        both CJK-heavy and English text);
  clamp max_tokens / max_completion_tokens down so prompt + generation fits
        the budget, and SET an explicit max_tokens on requests that carry
        none (the target otherwise applies its own default — SGLang: 131072 —
        and rejects when prompt + default exceeds the window; production
        backends clamp silently instead — the single biggest source of
        replay-only 4xxs);
  fix   tool schemas a strict jsonschema validator rejects: `"parameters"`
        null/non-object → empty object schema, `"properties": null` → {},
        `"required": null` → removed;
  fix   message `content` of a non-string scalar type (e.g. the integer 1,
        seen in real traffic) → its JSON text; null content on
        user/system/tool messages → "".

Multiple / out-of-order system messages are NOT normalized here — the replay
module already handles those at request time via its own `clean=True` option.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Input-length buckets for the per-input-length TTFT service level: (human, lo, hi) in
# prompt tokens, "K" = 1024, hi exclusive. Keep in sync with
# bench/tests/functional/replay.py :: TTFT_INPUT_BUCKETS and
# scripts/analyze_replay_dataset.py. Counted at conversion time from the captured
# usage so the input-length distribution is visible right after a build.
TTFT_INPUT_BUCKETS = [
    ("<6K",        0,           6 * 1024),
    ("6K-16K",     6 * 1024,    16 * 1024),
    ("16K-32K",    16 * 1024,   32 * 1024),
    ("32K-64K",    32 * 1024,   64 * 1024),
    ("64K-128K",   64 * 1024,   128 * 1024),
    ("128K-256K",  128 * 1024,  256 * 1024),
    ("≥256K",      256 * 1024,  float("inf")),
]

# --clean context accounting.
#
# CONTEXT_HEADROOM_TOKENS: the target's tokenizer never counts a prompt exactly
# like the capture's did (observed drift on real traffic: +0.05%..+0.2%, i.e.
# a few hundred tokens at 200K+), and the chat template adds a few tokens of
# its own. Both the "drop if prompt > window" decision and the max_tokens clamp
# leave this much slack so borderline requests don't 400 on the target anyway.
CONTEXT_HEADROOM_TOKENS = 1024
# Prompt-size estimate when the capture recorded no usage (production 4xxs have
# none): UTF-8 bytes of the request body / 3. CJK is ~3 bytes/char at roughly
# ~1 token/char; English is ~4 bytes/token — so /3 slightly over-estimates
# (= errs toward dropping) rather than letting an over-long prompt through.
EST_BYTES_PER_TOKEN = 3

# Magic bytes of image formats the replay targets can decode. An image/* data:
# URI whose payload doesn't start with one of these is not really an image
# (PIL raises UnidentifiedImageError server-side → 500).
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8", b"BM")

# Request headers never worth carrying into a dataset that lives on a shared
# volume. The offline CLI captures were curated by hand; the rolling collector
# writes production traffic continuously, so credentials must not accumulate
# there. Matched case-insensitively against the LAST dotted path segment.
DEFAULT_HEADER_DENYLIST = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
})


@dataclass(frozen=True)
class StripPolicy:
    """What to discard from each capture before it is written to the dataset.

    The defaults are the CLI's historical behaviour (keep everything) so
    `scripts/convert_bodylog_dataset.py` output is unchanged. The rolling
    collector passes a stricter policy — see `StripPolicy.for_feed()`.
    """

    header_denylist: frozenset[str] = frozenset()
    keep_response_body: bool = True

    @classmethod
    def for_feed(cls, header_denylist: "frozenset[str] | None" = None) -> "StripPolicy":
        """Policy for auto-collected datasets: no credentials, no response body.

        Dropping `resp_body` roughly halves dataset size and costs nothing for
        replay — the replay path loads records *lean* (request body only) and
        never reads `raw_payload`. `resp_meta` (incl. usage) is always kept:
        build-time bucketing and `scripts/analyze_replay_dataset.py` read it.
        """
        return cls(
            header_denylist=(
                DEFAULT_HEADER_DENYLIST if header_denylist is None else frozenset(header_denylist)
            ),
            keep_response_body=False,
        )


@dataclass
class ConvertStats:
    """Per-build tallies. `drops` and `fixes` mirror the CLI's counters."""

    total: int = 0
    written: int = 0
    drops: Counter = field(default_factory=Counter)
    fixes: Counter = field(default_factory=Counter)
    buckets: Counter = field(default_factory=Counter)
    bucket_unknown: int = 0

    def note_written(self, prompt_tokens: object) -> None:
        self.written += 1
        b = bucket_for_tokens(prompt_tokens)
        if b is None:
            self.bucket_unknown += 1
        else:
            self.buckets[b] += 1

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "written": self.written,
            "drops": dict(self.drops),
            "fixes": dict(self.fixes),
            "buckets": {h: self.buckets.get(h, 0) for h, _, _ in TTFT_INPUT_BUCKETS},
            "bucket_unknown": self.bucket_unknown,
        }


# ---------------------------------------------------------------------------
# VictoriaLogs shape adaptation
# ---------------------------------------------------------------------------

# Dotted prefixes whose leaves must stay strings. HTTP header values are strings
# by definition, and coercing e.g. `req_headers.x-request-id: "0012"` to 12
# would corrupt it.
_NO_COERCE_PREFIXES = ("req_headers.",)
# Exact field names that must stay strings even though they look numeric-ish.
_NO_COERCE_FIELDS = frozenset({"req_body", "resp_body", "_msg", "_stream", "_stream_id"})


def _coerce_scalar(value: str) -> Any:
    """VictoriaLogs stores every field as a string. Restore int/float/bool so
    downstream code (which type-checks `status`, `prompt_tokens`, …) behaves the
    same as it does on a raw bodylog file.

    Leading-zero integers stay strings — they are ids, not numbers.
    """
    if value in ("true", "false"):
        return value == "true"
    stripped = value[1:] if value[:1] == "-" else value
    if stripped.isdigit():
        if len(stripped) > 1 and stripped[0] == "0":
            return value
        try:
            return int(value)
        except ValueError:
            return value
    # Floats: only the plain decimal form the gateway emits (rt, first_chunk_t).
    if stripped.count(".") == 1:
        head, _, tail = stripped.partition(".")
        if head.isdigit() and tail.isdigit():
            try:
                return float(value)
            except ValueError:
                return value
    return value


def unflatten(row: dict) -> dict:
    """Rebuild a nested bodylog record from a VictoriaLogs row.

    VictoriaLogs flattens nested JSON into dotted field names and stores every
    value as a string, so a capture that was logged as

        {"req_headers": {"host": "x"}, "resp_meta": {"usage": {"prompt_tokens": 14484}}}

    comes back as

        {"req_headers.host": "x", "resp_meta.usage.prompt_tokens": "14484"}

    This restores the nesting and the scalar types, which is what makes the
    conversion path below identical for a raw bodylog file and for a row pulled
    from the log store — one code path, one set of `--clean` rules.
    """
    out: dict = {}
    for key, value in row.items():
        if isinstance(value, str) and key not in _NO_COERCE_FIELDS \
                and not key.startswith(_NO_COERCE_PREFIXES):
            value = _coerce_scalar(value)
        if "." not in key:
            # A flat key always wins over a nested one built from dotted
            # siblings: the log store never emits both, and if it somehow did,
            # the explicit value is the more trustworthy of the two.
            if isinstance(out.get(key), dict) and not isinstance(value, dict):
                continue
            out[key] = value
            continue
        head, *rest = key.split(".")
        cursor = out
        for part in [head, *rest[:-1]]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[rest[-1]] = value
    return out


# ---------------------------------------------------------------------------
# --clean repairs
# ---------------------------------------------------------------------------


def bucket_for_tokens(pt: object) -> "str | None":
    if not isinstance(pt, int) or isinstance(pt, bool):
        return None
    for human, lo, hi in TTFT_INPUT_BUCKETS:
        if lo <= pt < hi:
            return human
    return None


def _data_uri_image_ok(url: str) -> bool:
    """True if a data: URI declares image/* AND its payload starts with a known
    image magic. Anything else fails server-side decode."""
    head, sep, payload = url.partition(",")
    if not sep:
        return False
    if not head[5:].startswith("image/"):
        return False
    if not head.endswith(";base64"):
        # URL-encoded (non-base64) image payloads are legal per RFC 2397 but
        # unseen in practice and untested against the targets — reject.
        return False
    try:
        # 24 base64 chars (a multiple of 4, so no padding issues) → 18 bytes,
        # enough for every magic we check.
        magic = base64.b64decode(payload[:24])
    except (binascii.Error, ValueError):
        return False
    return any(magic.startswith(m) for m in _IMAGE_MAGIC)


def _clean_tools(tools: object, fixes: Counter) -> None:
    """Repair tool schemas in place. Strict backends run each function's
    `parameters` through a jsonschema metaschema; gateways don't."""
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(fn, dict) or "parameters" not in fn:
            continue
        params = fn["parameters"]
        if not isinstance(params, dict):
            # null or a non-object — replace with the empty-args schema.
            fn["parameters"] = {"type": "object", "properties": {}}
            fixes["tool_schema_fixed"] += 1
            continue
        if "properties" in params and params["properties"] is None:
            params["properties"] = {}
            fixes["tool_schema_fixed"] += 1
        if "required" in params and params["required"] is None:
            del params["required"]
            fixes["tool_schema_fixed"] += 1


def _clean_messages(messages: list, fixes: Counter) -> "str | None":
    """Repair message content in place. Returns a drop reason, or None to keep."""
    for m in messages:
        if not isinstance(m, dict):
            return "malformed_message"
        content = m.get("content")
        if content is None:
            # Strict validators want a string here; assistant messages may
            # legitimately carry null content next to tool_calls.
            if m.get("role") in ("user", "system", "tool"):
                m["content"] = ""
                fixes["content_coerced"] += 1
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    return "malformed_content_part"
                if part.get("type") != "image_url":
                    continue
                url = (part.get("image_url") or {}).get("url")
                if not isinstance(url, str):
                    return "malformed_content_part"
                if url.startswith("data:"):
                    if not _data_uri_image_ok(url):
                        return "bad_image_data_uri"
                else:
                    # http(s)/file/… — the target would have to fetch it.
                    return "external_image_url"
        elif not isinstance(content, str):
            # Scalar of the wrong type (integer 1 seen in real traffic) or a
            # stray object — production coerces, strict backends 400.
            m["content"] = json.dumps(content, ensure_ascii=False)
            fixes["content_coerced"] += 1
    return None


def clean_request_json(
    request_json: dict,
    prompt_tokens: object,
    body_utf8_len: int,
    budget: int,
    fixes: Counter,
) -> "str | None":
    """Apply --clean repairs to `request_json` in place.

    `budget` is max_model_len - CONTEXT_HEADROOM_TOKENS. Returns a drop reason
    (the record cannot be made replayable), or None if it was kept. Mutations
    are tallied into `fixes` (the caller re-serializes when any fix landed).
    """
    reason = _clean_messages(request_json.get("messages") or [], fixes)
    if reason:
        return reason
    _clean_tools(request_json.get("tools"), fixes)

    if isinstance(prompt_tokens, int) and prompt_tokens > 0:
        tokens, estimated = prompt_tokens, False
    else:
        tokens, estimated = body_utf8_len // EST_BYTES_PER_TOKEN, True
    if tokens > budget:
        return "ctx_overflow_estimated" if estimated else "ctx_overflow"

    room = max(1, budget - tokens)
    capped = False
    for key in ("max_completion_tokens", "max_tokens"):
        if key not in request_json:
            continue
        val = request_json[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            # null / malformed = "no limit" — pin it to what actually fits.
            request_json[key] = room
            fixes["max_tokens_set"] += 1
        elif tokens + val > budget:
            request_json[key] = room
            fixes["max_tokens_clamped"] += 1
        capped = True
    if not capped:
        # No generation cap at all: the target applies its own default
        # (SGLang: 131072) and REJECTS the request when prompt + default
        # exceeds the window, instead of clamping like production gateways.
        # An explicit cap that fits removes that failure mode; for short
        # prompts it is far above any real completion, so it changes nothing.
        request_json["max_tokens"] = room
        fixes["max_tokens_set"] += 1
    return None


# ---------------------------------------------------------------------------
# Record conversion
# ---------------------------------------------------------------------------


def _strip_payload(payload: dict, strip: StripPolicy) -> dict:
    """Apply the strip policy to the raw_payload copy of a capture."""
    if not strip.header_denylist and strip.keep_response_body:
        return payload
    out = dict(payload)
    if not strip.keep_response_body:
        out.pop("resp_body", None)
        out.pop("resp_body_truncated", None)
    headers = out.get("req_headers")
    if strip.header_denylist and isinstance(headers, dict):
        out["req_headers"] = {
            k: v for k, v in headers.items() if k.lower() not in strip.header_denylist
        }
    return out


def convert_record(
    rec: dict,
    *,
    source_file: str,
    line_no: int,
    clean: bool = False,
    budget: int = 0,
    strip: "StripPolicy | None" = None,
    fixes: "Counter | None" = None,
) -> "tuple[dict | None, str | None]":
    """Convert ONE bodylog capture (nested — call `unflatten` first for a
    VictoriaLogs row) into a MatchedRequest dict.

    Returns `(record, None)` on success or `(None, drop_reason)` — the reason
    strings are the CLI's counter names, so both callers report identically.
    Already-converted MatchedRequest records pass through (detected per record),
    which is what lets `--clean` re-process a converted dataset whose raw
    capture is gone.
    """
    strip = strip or StripPolicy()
    fixes = fixes if fixes is not None else Counter()

    already_converted = "request_body" in rec and "req_body" not in rec

    if already_converted:
        req_body = rec.get("request_body")
        capture = rec.get("raw_payload") or {}
    else:
        req_body = rec.get("req_body")
        capture = rec
        # A truncated capture is an incomplete request — replaying it would
        # send a malformed body, so drop it rather than corrupt the run.
        if rec.get("req_body_truncated"):
            return None, "truncated"
    if not req_body:
        return None, "no_body"
    if not isinstance(req_body, str):
        return None, "bad_json"

    request_json = rec.get("request_json") if already_converted else None
    if not isinstance(request_json, dict):
        try:
            request_json = json.loads(req_body)
        except json.JSONDecodeError:
            return None, "bad_json"
    if not isinstance(request_json, dict) or "messages" not in request_json:
        return None, "no_messages"

    usage = (capture.get("resp_meta") or {}).get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")

    if clean:
        status = capture.get("status")
        if isinstance(status, int) and 400 <= status < 500:
            return None, "orig_4xx"
        pre_fix = sum(fixes.values())
        reason = clean_request_json(
            request_json, prompt_tokens, len(req_body.encode("utf-8")), budget, fixes,
        )
        if reason:
            return None, reason
        if sum(fixes.values()) > pre_fix:
            # A repair mutated the payload — re-serialize so request_body (what
            # the replay actually sends) matches.
            req_body = json.dumps(request_json, ensure_ascii=False, separators=(",", ":"))

    if already_converted:
        normalized = {**rec, "request_body": req_body, "request_json": request_json}
        if strip.header_denylist or not strip.keep_response_body:
            normalized["raw_payload"] = _strip_payload(rec.get("raw_payload") or {}, strip)
        return normalized, None

    headers = rec.get("req_headers") or {}
    # Prefer the capture's own request_id; fall back to the gateway's
    # x-request-id header, then a synthetic id so every record is traceable.
    request_id = rec.get("request_id") or headers.get("x-request-id") or f"req_{line_no}"

    # channel_id / token_name are informational only (the extract tool groups
    # token_counts by them; the replay metrics ignore them). Map the closest
    # caller-identity headers so a per-caller breakdown is preserved.
    normalized = {
        "source_file": source_file,
        "line_no": line_no,
        "level": "INFO",
        # `ts` is ISO8601 metadata; the replay path never parses it, so keep it
        # verbatim rather than reformatting and losing precision/timezone.
        "timestamp": rec.get("ts", ""),
        "request_id": request_id,
        "channel_id": headers.get("x-appid"),
        "token_name": headers.get("x-uin"),
        "request_body": req_body,
        "request_json": request_json,
        # Preserve every original field except the request body itself, which is
        # carried verbatim in request_body/request_json (no need to duplicate the
        # bulky body). This keeps the original response, usage, reasoning,
        # headers and timing for downstream analysis — minus whatever the strip
        # policy removes.
        "raw_payload": _strip_payload({k: v for k, v in rec.items() if k != "req_body"}, strip),
    }
    return normalized, None


def dumps_record(record: dict) -> str:
    """One dataset line. Compact + ensure_ascii=False, matching what the replay
    loader and every existing dataset file use."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))
