"""Tests for the shared bodylog → replay conversion core.

Two callers depend on this code being identical: the offline CLI
(`scripts/convert_bodylog_dataset.py`) and the platform's rolling collector.
The collector additionally feeds it rows straight out of VictoriaLogs, which
flattens nested JSON into dotted keys and stringifies every value — so
`unflatten` is the seam where a schema surprise would show up.

The row shapes below mirror the live store (verified against the real gateway
log store, 2026-08-04): `req_headers.*` and `resp_meta.usage.*` arrive as dotted
string fields, `status` as "200", `req_body` as the raw request JSON text.
"""
from __future__ import annotations

import json
from collections import Counter

from bench.replay_test.bodylog_convert import (
    DEFAULT_HEADER_DENYLIST,
    StripPolicy,
    bucket_for_tokens,
    convert_record,
    unflatten,
)

REQ_BODY = json.dumps({
    "model": "glm-5",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 128,
    "stream": True,
})


def vl_row(**overrides) -> dict:
    row = {
        "_msg": "glm-5:8060 --> 198.51.100.20:8052 prompt_tokens:14484 TTFT:0.8s status:200",
        "_time": "2026-08-04T05:32:00.084Z",
        "model": "glm-5",
        "status": "200",
        "forwarded_to": "http://198.51.100.20:8052",
        "uri": "/v1/chat/completions",
        "ts": "2026-08-04T13:32:00.084+08:00",
        "rt": "140.074",
        "request_id": "9947bb96fb9b2dac699dbc4e7d73d3ed",
        "req_body": REQ_BODY,
        "req_body_truncated": "false",
        "req_headers.host": "gateway.example.com",
        "req_headers.authorization": "Bearer sk-secret-value",
        "req_headers.x-request-id": "0012",
        "req_headers.content-length": "60643",
        "resp_body": "…the original streamed answer…",
        "resp_meta.finish_reason": "stop",
        "resp_meta.usage.prompt_tokens": "14484",
        "resp_meta.usage.completion_tokens": "3124",
        "resp_meta.usage.prompt_tokens_details.cached_tokens": "8832",
    }
    row.update(overrides)
    return row


# --- unflatten ------------------------------------------------------------------

def test_unflatten_rebuilds_nesting():
    rec = unflatten(vl_row())
    assert rec["req_headers"]["host"] == "gateway.example.com"
    assert rec["resp_meta"]["usage"]["prompt_tokens"] == 14484
    assert rec["resp_meta"]["usage"]["prompt_tokens_details"]["cached_tokens"] == 8832


def test_unflatten_restores_scalar_types():
    """Everything arrives as a string; the conversion rules type-check `status`
    and `prompt_tokens`, so they must come back as real numbers/bools."""
    rec = unflatten(vl_row())
    assert rec["status"] == 200 and isinstance(rec["status"], int)
    assert rec["req_body_truncated"] is False
    assert rec["rt"] == 140.074


def test_unflatten_leaves_headers_and_bodies_as_strings():
    """Header values are strings by definition — coercing "0012" to 12 would
    corrupt an id — and the request body must stay verbatim text."""
    rec = unflatten(vl_row())
    assert rec["req_headers"]["x-request-id"] == "0012"
    assert rec["req_headers"]["content-length"] == "60643"
    assert rec["req_body"] == REQ_BODY


def test_unflatten_keeps_negative_and_plain_ints():
    rec = unflatten({"a": "-5", "b": "7", "c": "1.5", "d": "notanumber"})
    assert rec == {"a": -5, "b": 7, "c": 1.5, "d": "notanumber"}


# --- convert_record --------------------------------------------------------------

def test_convert_row_produces_matched_request():
    record, reason = convert_record(unflatten(vl_row()), source_file="feed", line_no=1)
    assert reason is None
    assert record["request_body"] == REQ_BODY
    assert record["request_json"]["model"] == "glm-5"
    assert record["source_file"] == "feed"
    assert record["timestamp"] == "2026-08-04T13:32:00.084+08:00"
    assert record["request_id"] == "9947bb96fb9b2dac699dbc4e7d73d3ed"


def test_truncated_request_body_is_dropped():
    """A truncated capture is an incomplete request — replaying it would send a
    malformed body."""
    record, reason = convert_record(
        unflatten(vl_row(req_body_truncated="true")), source_file="f", line_no=1
    )
    assert record is None and reason == "truncated"


def test_missing_or_unparseable_body_is_dropped():
    record, reason = convert_record(unflatten(vl_row(req_body="")), source_file="f", line_no=1)
    assert record is None and reason == "no_body"
    record, reason = convert_record(
        unflatten(vl_row(req_body="{not json")), source_file="f", line_no=1
    )
    assert record is None and reason == "bad_json"


def test_non_chat_body_is_dropped():
    record, reason = convert_record(
        unflatten(vl_row(req_body='{"prompt":"legacy completions"}')),
        source_file="f", line_no=1,
    )
    assert record is None and reason == "no_messages"


# --- strip policy -----------------------------------------------------------------

def test_default_policy_keeps_everything():
    """The offline CLI's historical behaviour must not change."""
    record, _ = convert_record(unflatten(vl_row()), source_file="f", line_no=1)
    assert "resp_body" in record["raw_payload"]
    assert "authorization" in record["raw_payload"]["req_headers"]


def test_feed_policy_strips_credentials_and_response_body():
    record, _ = convert_record(
        unflatten(vl_row()), source_file="f", line_no=1, strip=StripPolicy.for_feed()
    )
    payload = record["raw_payload"]
    assert "resp_body" not in payload
    assert "authorization" not in payload["req_headers"]
    assert payload["req_headers"]["host"] == "gateway.example.com"
    # Usage survives: build-time bucketing and the dataset analyzer read it.
    assert payload["resp_meta"]["usage"]["prompt_tokens"] == 14484


def test_feed_policy_denylist_is_case_insensitive():
    row = vl_row()
    row.pop("req_headers.authorization")
    row["req_headers.Authorization"] = "Bearer x"
    row["req_headers.X-Api-Key"] = "k"
    record, _ = convert_record(
        unflatten(row), source_file="f", line_no=1, strip=StripPolicy.for_feed()
    )
    assert record["raw_payload"]["req_headers"] == {
        k: v for k, v in record["raw_payload"]["req_headers"].items()
        if k.lower() not in DEFAULT_HEADER_DENYLIST
    }
    assert not any(k.lower() == "authorization" for k in record["raw_payload"]["req_headers"])


def test_custom_denylist_replaces_the_default():
    policy = StripPolicy.for_feed(frozenset({"host"}))
    record, _ = convert_record(unflatten(vl_row()), source_file="f", line_no=1, strip=policy)
    headers = record["raw_payload"]["req_headers"]
    assert "host" not in headers
    assert "authorization" in headers  # explicitly not in the custom list


# --- clean mode --------------------------------------------------------------------

def test_clean_drops_original_4xx():
    record, reason = convert_record(
        unflatten(vl_row(status="400")), source_file="f", line_no=1,
        clean=True, budget=261120, fixes=Counter(),
    )
    assert record is None and reason == "orig_4xx"


def test_clean_drops_over_context_prompt():
    fixes = Counter()
    record, reason = convert_record(
        unflatten(vl_row(**{"resp_meta.usage.prompt_tokens": "300000"})),
        source_file="f", line_no=1, clean=True, budget=261120, fixes=fixes,
    )
    assert record is None and reason == "ctx_overflow"


def test_clean_sets_max_tokens_when_absent_and_reserializes():
    """A request with no generation cap is the biggest source of replay-only
    4xxs (the backend applies its own huge default and rejects). The repair must
    also land in request_body, which is what actually gets sent."""
    body = json.dumps({"model": "glm-5", "messages": [{"role": "user", "content": "hi"}]})
    fixes = Counter()
    record, reason = convert_record(
        unflatten(vl_row(req_body=body)), source_file="f", line_no=1,
        clean=True, budget=261120, fixes=fixes,
    )
    assert reason is None
    assert fixes["max_tokens_set"] == 1
    assert record["request_json"]["max_tokens"] > 0
    assert json.loads(record["request_body"])["max_tokens"] == record["request_json"]["max_tokens"]


def test_clean_clamps_oversized_max_tokens():
    body = json.dumps({
        "model": "glm-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 250000,
    })
    fixes = Counter()
    record, _ = convert_record(
        unflatten(vl_row(req_body=body)), source_file="f", line_no=1,
        clean=True, budget=261120, fixes=fixes,
    )
    assert fixes["max_tokens_clamped"] == 1
    assert record["request_json"]["max_tokens"] == 261120 - 14484


def test_already_converted_record_passes_through():
    """Re-processing a converted dataset (e.g. to apply --clean when the raw
    capture is gone) must be supported."""
    first, _ = convert_record(unflatten(vl_row()), source_file="f", line_no=1)
    again, reason = convert_record(first, source_file="ignored", line_no=99)
    assert reason is None
    assert again["request_body"] == first["request_body"]
    assert again["source_file"] == "f"  # not overwritten


# --- buckets --------------------------------------------------------------------

def test_bucket_for_tokens():
    assert bucket_for_tokens(100) == "<6K"
    assert bucket_for_tokens(14484) == "6K-16K"
    assert bucket_for_tokens(40000) == "32K-64K"
    assert bucket_for_tokens(300000) == "≥256K"
    assert bucket_for_tokens(None) is None
    assert bucket_for_tokens("14484") is None
