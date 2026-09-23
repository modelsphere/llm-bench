"""Unit tests for the endpoint pre-flight probe.

Offline and DB-free — these run even when the integration Postgres/Redis aren't
up (see conftest for those). Most tests monkeypatch `httpx`; the header-encoding
tests instead drive a throwaway loopback server, because the failure they cover
only surfaces once a real connection exists (see the note above them).

The probe deliberately uses httpx (not requests) because it must fail on exactly
the inputs the benchmark fails on — see the module docstring in
``app.core.preflight`` and ``test_whitespace_api_key_fails_like_the_run_would``.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.core import preflight


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", lines=None):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")
        self._lines = lines or []

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_lines(self):
        # httpx yields decoded str lines (requests yielded bytes).
        yield from self._lines

    # support `with httpx.stream("POST", ...) as resp:`
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_chat():
    return FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})


def _sse(*lines):
    return FakeResponse(200, lines=list(lines) or ["data: {}"])


def _install(monkeypatch, *, post, get, stream=None):
    """Patch the three httpx entry points the probe uses.

    Streaming moved from `post(..., stream=True)` to `httpx.stream("POST", ...)`,
    so it is a separate seam now rather than a kwarg on post.
    """
    monkeypatch.setattr(preflight.httpx, "post", post)
    monkeypatch.setattr(preflight.httpx, "get", get)
    monkeypatch.setattr(
        preflight.httpx, "stream",
        stream if stream is not None else (lambda method, url, **kw: _sse()),
    )


def _checks_by_name(result):
    return {c["name"]: c for c in result["checks"]}


def test_all_green(monkeypatch):
    """Happy path: chat works, /models lists the model, streaming yields chunks."""
    def post(url, **kw):
        return _ok_chat()

    def get(url, **kw):
        return FakeResponse(200, {"data": [{"id": "my-model"}, {"id": "other"}]})

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://api.example.com", "my-model", "sk-key")

    assert r["ok"] is True
    assert r["latency_ms"] is not None
    assert r["endpoint_tested"] == "https://api.example.com/v1/chat/completions"
    by = _checks_by_name(r)
    assert by["Connectivity"]["status"] == "pass"
    assert by["Authentication"]["status"] == "pass"
    assert by["Chat completion"]["status"] == "pass"
    assert by["Model available"]["status"] == "pass"
    assert by["Streaming (SSE)"]["status"] == "pass"


def test_bad_api_key_fails(monkeypatch):
    def post(url, **kw):
        return FakeResponse(401, text="invalid api key")

    def get(url, **kw):
        return FakeResponse(401, text="invalid api key")

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://api.example.com", "my-model", "wrong")

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Connectivity"]["status"] == "pass"
    assert by["Authentication"]["status"] == "fail"
    assert by["Chat completion"]["status"] == "skip"
    # streaming probe is skipped because chat never succeeded
    assert "Streaming (SSE)" not in by


def test_wrong_url_404_fails(monkeypatch):
    def post(url, **kw):
        return FakeResponse(404, text="not found")

    def get(url, **kw):
        return FakeResponse(404, text="not found")

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://api.example.com/wrong", "my-model", "sk")

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Chat completion"]["status"] == "fail"


def test_unreachable_host_fails_fast(monkeypatch):
    def post(url, **kw):
        raise httpx.ConnectError("name resolution failed")

    def get(url, **kw):  # pragma: no cover - not reached (conn_ok is False)
        raise AssertionError("should not probe /models when chat couldn't connect")

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://nope.invalid", "m", "")

    assert r["ok"] is False
    assert len(r["checks"]) == 1
    assert r["checks"][0]["name"] == "Connectivity"
    assert r["checks"][0]["status"] == "fail"


def test_model_not_listed_is_warning_not_failure(monkeypatch):
    """An unknown model name (per /models) warns but doesn't block — the chat call
    still answered, and many gateways alias model names."""
    def post(url, **kw):
        return _ok_chat()

    def get(url, **kw):
        return FakeResponse(200, {"data": [{"id": "served-a"}, {"id": "served-b"}]})

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://api.example.com", "typo-model", "sk")

    assert r["ok"] is True  # warning only
    by = _checks_by_name(r)
    assert by["Model available"]["status"] == "warn"
    assert "served-a" in by["Model available"]["detail"]


def test_models_endpoint_absent_is_warning(monkeypatch):
    """Gateways that don't expose /models shouldn't fail the check."""
    def post(url, **kw):
        return _ok_chat()

    def get(url, **kw):
        return FakeResponse(404, text="no such route")

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://gw.example.com/v4", "m", "sk")

    assert r["ok"] is True
    by = _checks_by_name(r)
    assert by["Model available"]["status"] == "warn"


def test_200_without_choices_fails(monkeypatch):
    """A 200 that isn't an OpenAI-compatible body is a real problem."""
    def post(url, **kw):
        return FakeResponse(200, {"unexpected": "shape"})

    def get(url, **kw):
        return FakeResponse(200, {"data": []})

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://api.example.com", "m", "sk")

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Chat completion"]["status"] == "fail"


def test_full_url_is_not_double_suffixed(monkeypatch):
    """If the user pastes the full .../chat/completions URL we must hit it as-is."""
    seen = {}

    def post(url, **kw):
        seen.setdefault("post", url)
        return _ok_chat()

    def get(url, **kw):
        return FakeResponse(200, {"data": [{"id": "m"}]})

    _install(monkeypatch, post=post, get=get)
    r = preflight.run_preflight("https://api.example.com/v1/chat/completions", "m", "sk")

    assert seen["post"] == "https://api.example.com/v1/chat/completions"
    assert r["endpoint_tested"] == "https://api.example.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# Header-encoding failures. The probe must be LOYAL to the user: whatever key
# they set (or didn't) has to be sent exactly the way the benchmark will send it.
# ---------------------------------------------------------------------------


# NOTE: these tests deliberately do NOT monkeypatch httpx — patching post()
# would bypass exactly the validation under test. They need a REAL reachable
# server because the two failure modes surface at different stages:
#   * non-ASCII  -> UnicodeEncodeError while BUILDING the request (no socket)
#   * whitespace -> h11 LocalProtocolError while WRITING to the wire, which is
#                   only reached once a connection has been established
# Hence the loopback fixture rather than a made-up hostname (which would just
# time out on connect and never exercise the header path at all).


@pytest.fixture
def local_endpoint():
    """A minimal OpenAI-shaped endpoint on loopback. Never actually answers the
    bad-header cases — the client fails before/while sending."""
    body = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}],
    }).encode()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._send(body)

        def do_GET(self):
            self._send(json.dumps({"data": [{"id": "m"}]}).encode())

        def log_message(self, *args):  # silence stderr noise
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_whitespace_api_key_fails_like_the_run_would(local_endpoint):
    """A whitespace-only key builds `Bearer  `, which h11 rejects.

    Regression test for a live incident: `requests` sent this happily so the
    probe passed, then every benchmark request died at construction and the run
    scored 0 with all-zero metrics and no visible cause. The probe must reject
    it here instead.
    """
    r = preflight.run_preflight(local_endpoint, "m", " ")

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["API key"]["status"] == "fail"
    assert "Illegal header value" in by["API key"]["detail"]
    # actionable, not just a stack trace
    assert "Clear the field" in by["API key"]["detail"]


def test_non_ascii_api_key_fails_without_crashing(local_endpoint):
    """A pasted CJK/emoji key raises UnicodeEncodeError, not an httpx error —
    it must still be reported as a check, never escape as a 500."""
    r = preflight.run_preflight(local_endpoint, "m", "密钥")

    assert r["ok"] is False
    assert _checks_by_name(r)["API key"]["status"] == "fail"


def test_keyless_endpoint_sends_no_auth_header(monkeypatch):
    """A raw endpoint with no key is a supported target: no Authorization
    header at all, mirroring guidellm's `if api_key:` guard."""
    seen = {}

    def post(url, **kw):
        seen["headers"] = kw.get("headers") or {}
        return _ok_chat()

    _install(monkeypatch, post=post, get=lambda url, **kw: FakeResponse(200, {"data": []}))
    r = preflight.run_preflight("https://api.example.com", "m", "")

    assert r["ok"] is True
    assert "Authorization" not in seen["headers"]


def test_key_present_sends_bearer_header(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen["headers"] = kw.get("headers") or {}
        return _ok_chat()

    _install(monkeypatch, post=post, get=lambda url, **kw: FakeResponse(200, {"data": []}))
    preflight.run_preflight("https://api.example.com", "m", "sk-abc")

    assert seen["headers"]["Authorization"] == "Bearer sk-abc"


def test_judge_whitespace_key_fails(local_endpoint):
    r = preflight.run_judge_preflight(local_endpoint, "m", "\t", 256)

    assert r["ok"] is False
    assert _checks_by_name(r)["API key"]["status"] == "fail"


# ---------------------------------------------------------------------------
# run_judge_preflight — the grading-shaped probe for LLM-judge endpoints
# ---------------------------------------------------------------------------


def _judge_reply(content, finish_reason="stop"):
    return FakeResponse(200, {
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason}],
    })


def test_judge_happy_path(monkeypatch):
    """A healthy judge grades the known-correct probe sample 'A'."""
    seen = {}

    def post(url, **kw):
        seen["payload"] = kw.get("json")
        return _judge_reply("A")

    monkeypatch.setattr(preflight.httpx, "post", post)
    r = preflight.run_judge_preflight("https://judge.example.com", "gpt-judge", "sk", 256)

    assert r["ok"] is True
    by = _checks_by_name(r)
    assert by["Connectivity"]["status"] == "pass"
    assert by["Authentication"]["status"] == "pass"
    assert by["Grading call"]["status"] == "pass"
    assert by["Grade parse"]["status"] == "pass"
    # the probe must use the module's real token budget, non-streamed
    assert seen["payload"]["max_tokens"] == 256
    assert seen["payload"]["stream"] is False


def test_judge_bad_key_fails(monkeypatch):
    monkeypatch.setattr(
        preflight.httpx, "post",
        lambda url, **kw: FakeResponse(401, text="invalid token"),
    )
    r = preflight.run_judge_preflight("https://judge.example.com", "m", "bad", 256)

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Authentication"]["status"] == "fail"


def test_judge_wrong_model_fails_with_body(monkeypatch):
    """A wrong judge model name (404/400) must fail and surface the server body —
    at run time this exact misconfiguration silently zeroes SimpleQA."""
    monkeypatch.setattr(
        preflight.httpx, "post",
        lambda url, **kw: FakeResponse(404, text="model 'nope' does not exist"),
    )
    r = preflight.run_judge_preflight("https://judge.example.com", "nope", "sk", 256)

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Grading call"]["status"] == "fail"
    assert "does not exist" in by["Grading call"]["detail"]


def test_judge_truncated_reasoning_fails_token_budget(monkeypatch):
    """A reasoning judge cut off by max_tokens before emitting a grade must
    point at judge_max_tokens, not report a vague parse error."""
    monkeypatch.setattr(
        preflight.httpx, "post",
        lambda url, **kw: _judge_reply(
            "Let me think about whether the predicted answer matches the gold",
            finish_reason="length",
        ),
    )
    r = preflight.run_judge_preflight("https://judge.example.com", "r1", "sk", 16)

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Token budget"]["status"] == "fail"
    assert "judge_max_tokens" in by["Token budget"]["detail"]


def test_judge_unparseable_reply_fails(monkeypatch):
    monkeypatch.setattr(
        preflight.httpx, "post",
        lambda url, **kw: _judge_reply("The answer is correct!"),
    )
    r = preflight.run_judge_preflight("https://judge.example.com", "m", "sk", 256)

    assert r["ok"] is False
    by = _checks_by_name(r)
    assert by["Grade parse"]["status"] == "fail"


def test_judge_wrong_grade_warns(monkeypatch):
    """Grading the trivially-correct sample as B parses, but flags reliability."""
    monkeypatch.setattr(preflight.httpx, "post", lambda url, **kw: _judge_reply("B"))
    r = preflight.run_judge_preflight("https://judge.example.com", "m", "sk", 256)

    assert r["ok"] is True  # warn, not fail
    by = _checks_by_name(r)
    assert by["Grade parse"]["status"] == "warn"


def test_judge_grade_in_reasoning_content(monkeypatch):
    """Reasoning models may leave content empty and put the verdict in
    reasoning_content — same fallback the worker's _chat_complete applies."""
    resp = FakeResponse(200, {
        "choices": [{
            "message": {"role": "assistant", "content": "",
                        "reasoning_content": "The prediction matches. Final grade: A"},
            "finish_reason": "stop",
        }],
    })
    monkeypatch.setattr(preflight.httpx, "post", lambda url, **kw: resp)
    r = preflight.run_judge_preflight("https://judge.example.com", "r1", "sk", 256)

    assert r["ok"] is True
    assert _checks_by_name(r)["Grade parse"]["status"] == "pass"


def test_judge_unreachable_fails(monkeypatch):
    def post(url, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(preflight.httpx, "post", post)
    r = preflight.run_judge_preflight("https://nope.invalid", "m", "", 256)

    assert r["ok"] is False
    assert r["checks"][0]["name"] == "Connectivity"
    assert r["checks"][0]["status"] == "fail"
