"""Endpoint pre-flight checks.

``run_preflight``: a fast, low-cost probe of a user-supplied OpenAI-compatible
endpoint *before* an expensive benchmark run is enqueued. A typo'd URL, a wrong
model name, or a bad API key otherwise only surfaces after a multi-minute
(sometimes much longer) suite has already run and failed.

``run_judge_preflight``: the same idea for an LLM-JUDGE endpoint (the opencompass
module's shared SimpleQA/HLE grader, the hallucination judge, …). Instead of a
"ping" it sends a real grading-shaped prompt with the configured max_tokens and
verifies a grade can be parsed back — because judge failures at run time degrade
silently (SimpleQA scores the sample NOT_ATTEMPTED; HLE drops to its weaker
exact-match grader), not to errors, this is the moment to catch a bad key, a
wrong model name, or a token budget too small for a reasoning judge.

The probe is SimpleQA-shaped (one letter back, ``judge_max_tokens``). HLE grades
through the same endpoint but with its own, much larger budget, and the worker
runs its own HLE-shaped probe at the start of that benchmark — so a pass here
means "this judge is reachable and grades", not "HLE's budget is sufficient".

The probe mirrors the real worker request path on purpose: same URL
normalisation (``utils.api.join_endpoint``), same HTTP stack (``httpx``, which
is what guidellm drives the benchmark with), and the same header construction
as guidellm's ``OpenAIHTTPBackend._build_headers`` — so a pass here is a strong
signal the real run will at least connect. It is intentionally cheap: one tiny
``chat/completions`` call (``max_tokens=8``), one optional ``GET /models``, and
one optional streaming probe.

The HTTP client choice is load-bearing, not incidental. ``requests``/urllib3 is
lenient about header values that h11 (under httpx) rejects outright: an API key
of " " builds the header ``Bearer  ``, which requests happily sends and h11
refuses with ``LocalProtocolError``. Probing with requests therefore PASSED
endpoints whose every benchmark request then failed at construction, producing a
run with zero successful requests and a score of 0. The probe must be loyal to
the user: send the key they set — or the absence of one — exactly the way the
benchmark will.

This is pure/synchronous so it is trivially unit-testable; the API route runs it
in a thread so the event loop is never blocked.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from utils.api import join_endpoint

# The judge preflight's whole promise is that a pass here means the run-time
# grader will parse, so it uses the worker's PARSER rather than a copy of it —
# a local regex silently drifts out of parity (it did once already).
from bench.tests.functional.opencompass import _parse_simpleqa_grade

# Bounded so a slow or misbehaving endpoint can't tie up a worker thread or
# flood memory. connect is short (a dead host should fail fast); read is longer
# to tolerate a genuinely slow first token.
_CONNECT_TIMEOUT = 8.0
_READ_TIMEOUT = 20.0
_MAX_BODY_CHARS = 600

# HTTP/1.1 — guidellm forces http2=False for the load generator, so the probe
# negotiates the same protocol the benchmark will.
_TIMEOUT = httpx.Timeout(
    connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT,
    write=_READ_TIMEOUT, pool=_CONNECT_TIMEOUT,
)


def _headers(api_key: str) -> dict[str, str]:
    """Mirror guidellm's ``_build_headers``: Authorization only when a key is
    actually set, so the keyless path is probed exactly as it will be run."""
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


# Raised while BUILDING the request, before any bytes are sent. httpx rejects
# illegal header values (a whitespace key -> `Bearer  `) with LocalProtocolError
# and non-ASCII ones (a pasted CJK character) with a bare UnicodeEncodeError,
# which is NOT an httpx.HTTPError and would otherwise escape as a 500. Both are
# failures the benchmark would hit on every request.
_HEADER_BUILD_ERRORS = (httpx.LocalProtocolError, UnicodeEncodeError)


def _bad_header_check(exc: Exception, has_key: bool) -> dict[str, str]:
    """Turn a header-encoding rejection into an actionable check.

    This is the failure the benchmark would hit on every single request, so the
    probe reports it as a hard failure rather than a connectivity blip.
    """
    hint = (
        "The API key contains a character that is illegal in an HTTP header — "
        "most often a stray space, tab, newline, or a non-ASCII character from "
        "a copy-paste. Clear the field if this endpoint needs no key."
        if has_key else
        "The request headers could not be encoded."
    )
    return {
        "name": "API key", "status": "fail",
        "detail": f"The request could not be built: {exc}. {hint} "
                  "Every benchmark request would fail this way before leaving "
                  "the platform, scoring 0 without ever reaching the endpoint.",
    }


def _truncate(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _MAX_BODY_CHARS else text[:_MAX_BODY_CHARS] + "…"


def run_preflight(endpoint_url: str, model: str, api_key: str) -> dict[str, Any]:
    """Probe an endpoint and return a structured result.

    Returns ``{ok, latency_ms, endpoint_tested, checks}`` where ``checks`` is an
    ordered list of ``{name, status, detail}`` with ``status`` one of
    ``pass | fail | warn | skip``. ``ok`` is True iff no check failed (warnings
    are advisory — e.g. a gateway that doesn't expose ``/models``).
    """
    checks: list[dict[str, str]] = []
    latency_ms: float | None = None

    chat_url = join_endpoint(endpoint_url, "chat/completions")
    headers = _headers(api_key)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }

    # 1) Chat completion — the canonical request every module makes. This one
    #    call validates connectivity, auth, the URL path, and that the model
    #    actually answers, so we derive several checks from its outcome.
    conn_ok = False
    chat_ok = False
    try:
        t0 = time.monotonic()
        resp = httpx.post(chat_url, json=payload, headers=headers, timeout=_TIMEOUT)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        conn_ok = True
        checks.append({
            "name": "Connectivity",
            "status": "pass",
            "detail": f"Reached {chat_url} in {latency_ms:.0f} ms.",
        })

        code = resp.status_code
        if code in (401, 403):
            checks.append({
                "name": "Authentication", "status": "fail",
                "detail": f"HTTP {code} — the API key was rejected. {_truncate(resp.text)}",
            })
            checks.append({
                "name": "Chat completion", "status": "skip",
                "detail": "Not checked — authentication failed.",
            })
        elif code == 404:
            checks.append({
                "name": "Authentication", "status": "skip",
                "detail": "Not checked — the endpoint path returned 404.",
            })
            checks.append({
                "name": "Chat completion", "status": "fail",
                "detail": f"HTTP 404 at {chat_url} — wrong endpoint URL? {_truncate(resp.text)}",
            })
        elif code == 200:
            checks.append({
                "name": "Authentication", "status": "pass",
                "detail": "API key accepted.",
            })
            try:
                data = resp.json()
            except ValueError:
                data = None
            message = None
            if isinstance(data, dict) and data.get("choices"):
                first = data["choices"][0] or {}
                message = first.get("message")
            if message is not None:
                chat_ok = True
                checks.append({
                    "name": "Chat completion", "status": "pass",
                    "detail": "Endpoint returned a valid chat completion.",
                })
            else:
                checks.append({
                    "name": "Chat completion", "status": "fail",
                    "detail": "HTTP 200 but no choices[].message in the response — "
                              f"is this an OpenAI-compatible endpoint? {_truncate(resp.text)}",
                })
        else:
            # 400/422/5xx — surface the body so a "model not found" / quota / param
            # error is visible to the user.
            checks.append({
                "name": "Authentication", "status": "skip",
                "detail": "Not checked — see the chat completion error.",
            })
            checks.append({
                "name": "Chat completion", "status": "fail",
                "detail": f"HTTP {code}. {_truncate(resp.text)}",
            })
    except _HEADER_BUILD_ERRORS as exc:
        # The request never left the process — the headers themselves are
        # invalid. Same failure the benchmark would hit on every request.
        checks.append(_bad_header_check(exc, bool(api_key)))
    except httpx.TimeoutException:
        checks.append({
            "name": "Connectivity", "status": "fail",
            "detail": f"Timed out talking to {chat_url} (>{_READ_TIMEOUT:.0f}s). "
                      "The endpoint may be very slow, down, or unreachable from the platform.",
        })
    except httpx.HTTPError as exc:
        checks.append({
            "name": "Connectivity", "status": "fail",
            "detail": f"Could not reach {chat_url}: {exc}",
        })

    # 2) Model availability via GET /models — best-effort. Many gateways don't
    #    expose /models; treat that as a warning, never a failure.
    if conn_ok:
        models_url = join_endpoint(endpoint_url, "models")
        try:
            resp = httpx.get(models_url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code == 200:
                ids: list[str] = []
                try:
                    ids = [
                        str(m.get("id"))
                        for m in (resp.json().get("data") or [])
                        if isinstance(m, dict) and m.get("id") is not None
                    ]
                except (ValueError, AttributeError):
                    ids = []
                if model in ids:
                    checks.append({
                        "name": "Model available", "status": "pass",
                        "detail": f"'{model}' is served by this endpoint.",
                    })
                elif ids:
                    shown = ", ".join(ids[:8])
                    more = f" (+{len(ids) - 8} more)" if len(ids) > 8 else ""
                    checks.append({
                        "name": "Model available", "status": "warn",
                        "detail": f"'{model}' is not listed by /models. Available: {shown}{more}.",
                    })
                else:
                    checks.append({
                        "name": "Model available", "status": "warn",
                        "detail": "/models returned an empty list; could not verify the model name.",
                    })
            else:
                checks.append({
                    "name": "Model available", "status": "warn",
                    "detail": f"/models returned HTTP {resp.status_code}; could not verify the "
                              "model name (many gateways don't expose /models).",
                })
        except httpx.HTTPError:
            checks.append({
                "name": "Model available", "status": "warn",
                "detail": "/models is not reachable; could not verify the model name "
                          "(many gateways don't expose /models).",
            })

    # 3) Streaming — most modules stream their responses. Best-effort; a failure
    #    here is a warning since the non-streaming call already succeeded.
    if chat_ok:
        try:
            t0 = time.monotonic()
            got_chunk = False
            ttft_ms = None
            with httpx.stream(
                "POST", chat_url, json={**payload, "stream": True},
                headers=headers, timeout=_TIMEOUT,
            ) as resp:
                if resp.status_code == 200:
                    # httpx yields decoded str lines (requests yielded bytes).
                    for line in resp.iter_lines():
                        if line and line.lstrip().startswith("data:"):
                            got_chunk = True
                            ttft_ms = round((time.monotonic() - t0) * 1000, 1)
                            break
            if got_chunk:
                checks.append({
                    "name": "Streaming (SSE)", "status": "pass",
                    "detail": f"First streamed chunk arrived in ~{ttft_ms:.0f} ms.",
                })
            else:
                checks.append({
                    "name": "Streaming (SSE)", "status": "warn",
                    "detail": "stream=true did not yield SSE 'data:' chunks; some modules "
                              "stream and may behave differently.",
                })
        except httpx.HTTPError:
            checks.append({
                "name": "Streaming (SSE)", "status": "warn",
                "detail": "The streaming request failed; some modules stream and may "
                          "behave differently.",
            })

    ok = not any(c["status"] == "fail" for c in checks)
    return {
        "ok": ok,
        "latency_ms": latency_ms,
        "endpoint_tested": chat_url,
        "checks": checks,
    }


# A compact grading task in the style the SimpleQA grader actually sends
# (OpenAI simple-evals protocol): question + gold target + predicted answer,
# reply with a single letter. The prediction is trivially CORRECT so a healthy
# grader must answer 'A'.
_JUDGE_PROBE_PROMPT = """\
Your job is to grade a predicted answer to a question as CORRECT, INCORRECT, \
or NOT_ATTEMPTED, by comparing it to the gold target.

Question: What is the capital of France?
Gold target: Paris
Predicted answer: The capital of France is Paris.

Grade the predicted answer. Reply with exactly one letter:
A = CORRECT, B = INCORRECT, C = NOT_ATTEMPTED.
"""



def run_judge_preflight(
    endpoint_url: str, model: str, api_key: str, max_tokens: int = 256,
) -> dict[str, Any]:
    """Probe an LLM-judge endpoint with a real grading call.

    Mirrors the worker's judge request exactly: same URL normalisation, same
    Bearer header, non-streamed, ``max_tokens`` as configured on the module —
    so a pass here means the run-time grader will authenticate, answer, and be
    parseable within its token budget. Returns the same shape as
    ``run_preflight``.
    """
    checks: list[dict[str, str]] = []
    latency_ms: float | None = None

    chat_url = join_endpoint(endpoint_url, "chat/completions")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _JUDGE_PROBE_PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }

    resp = None
    try:
        t0 = time.monotonic()
        resp = httpx.post(
            chat_url, json=payload, headers=_headers(api_key), timeout=_TIMEOUT,
        )
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        checks.append({
            "name": "Connectivity", "status": "pass",
            "detail": f"Reached {chat_url} in {latency_ms:.0f} ms.",
        })
    except _HEADER_BUILD_ERRORS as exc:
        checks.append(_bad_header_check(exc, bool(api_key)))
    except httpx.TimeoutException:
        checks.append({
            "name": "Connectivity", "status": "fail",
            "detail": f"Timed out talking to {chat_url} (>{_READ_TIMEOUT:.0f}s). "
                      "The judge endpoint may be very slow, down, or unreachable "
                      "from the platform.",
        })
    except httpx.HTTPError as exc:
        checks.append({
            "name": "Connectivity", "status": "fail",
            "detail": f"Could not reach {chat_url}: {exc}",
        })

    if resp is not None:
        code = resp.status_code
        if code in (401, 403):
            checks.append({
                "name": "Authentication", "status": "fail",
                "detail": f"HTTP {code} — the judge API key was rejected. {_truncate(resp.text)}",
            })
        elif code != 200:
            checks.append({
                "name": "Grading call", "status": "fail",
                "detail": f"HTTP {code} at {chat_url} — wrong judge URL or model name? "
                          f"{_truncate(resp.text)}",
            })
        else:
            checks.append({
                "name": "Authentication", "status": "pass",
                "detail": "Judge API key accepted.",
            })
            message: dict | None = None
            finish_reason = ""
            try:
                data = resp.json()
                first = (data.get("choices") or [{}])[0] or {}
                message = first.get("message")
                finish_reason = first.get("finish_reason") or ""
            except (ValueError, AttributeError, IndexError):
                pass
            if not isinstance(message, dict):
                checks.append({
                    "name": "Grading call", "status": "fail",
                    "detail": "HTTP 200 but no choices[].message in the response — is "
                              f"this an OpenAI-compatible endpoint? {_truncate(resp.text)}",
                })
            else:
                checks.append({
                    "name": "Grading call", "status": "pass",
                    "detail": "The judge answered the grading prompt.",
                })
                # The worker falls back to reasoning_content when content is
                # empty (reasoning models put the verdict there).
                reply = (message.get("content") or "").strip() \
                    or (message.get("reasoning_content") or "").strip()
                grade = _parse_simpleqa_grade(reply)
                if finish_reason == "length" and grade is None:
                    checks.append({
                        "name": "Token budget", "status": "fail",
                        "detail": f"The reply was cut off at max_tokens={max_tokens} before "
                                  "a grade appeared. Raise judge_max_tokens (a reasoning "
                                  "judge needs room to think) or use a non-reasoning grader.",
                    })
                elif grade is None:
                    checks.append({
                        "name": "Grade parse", "status": "fail",
                        "detail": "No standalone A/B/C grade could be parsed from the "
                                  f"reply: {_truncate(reply)!r}. At run time this would "
                                  "silently grade every sample NOT_ATTEMPTED.",
                    })
                elif grade != "A":
                    checks.append({
                        "name": "Grade parse", "status": "warn",
                        "detail": f"A grade was parsed but it is {grade!r} where a "
                                  "trivially-correct sample should grade 'A' — this judge "
                                  "may grade unreliably.",
                    })
                else:
                    checks.append({
                        "name": "Grade parse", "status": "pass",
                        "detail": "The judge graded a known-correct sample 'A' (CORRECT).",
                    })
                    if finish_reason == "length":
                        checks.append({
                            "name": "Token budget", "status": "warn",
                            "detail": f"A grade was parsed but the reply still hit "
                                      f"max_tokens={max_tokens}; consider raising "
                                      "judge_max_tokens for safety margin.",
                        })

    ok = not any(c["status"] == "fail" for c in checks)
    return {
        "ok": ok,
        "latency_ms": latency_ms,
        "endpoint_tested": chat_url,
        "checks": checks,
    }
