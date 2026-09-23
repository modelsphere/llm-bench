#!/usr/bin/env python3
"""
Standalone reproduction for 5 functional-acceptance failures.

Reproduces, with ZERO third-party dependencies (Python 3 stdlib only — no
requests / httpx / yaml), the exact requests + pass/fail criteria of these
checks from bench/tests/functional/functional_acceptance.py:

    d06_cache_hit          prompt-cache hit reported on a repeated request
    t3_max_tokens_mid      max_tokens=mid  → expect HTTP 2xx and finish="stop"
    t3_max_tokens_over     max_tokens=ctx+1 → expect HTTP 4xx (overflow rejected)
    t9_json_schema         response_format=json_schema → {"name":"Alice","age":30}
    t16b_missing_content   message with role but no content → expect HTTP 4xx

These were observed FAILING against a server started with max_model_len=100000.
Run this on the same server to see each failure in isolation, with the raw
HTTP status / finish_reason / usage so you can debug the engine, not the suite.

    python3 scripts/repro_functional_failures.py

Edit BASE_URL / MODEL / API_KEY below (or set REPRO_URL / REPRO_MODEL /
REPRO_API_KEY env vars). MAX_CONTEXT_TOKENS must match the value configured for
the functional module (which should equal the server's --max-model-len); the T3
boundaries are derived from it exactly as the suite does.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# EDIT THESE  (or override via env: REPRO_URL / REPRO_MODEL / REPRO_API_KEY)
# ─────────────────────────────────────────────────────────────────────────────
BASE_URL = os.environ.get("REPRO_URL",   "http://127.0.0.1:8000/v1")
MODEL    = os.environ.get("REPRO_MODEL", "your-model-name")
API_KEY  = os.environ.get("REPRO_API_KEY", "")        # "" = no Authorization header

# Must match the server's --max-model-len (the suite's max_context_tokens).
# The failures were reported at exactly 100000.
MAX_CONTEXT_TOKENS = int(os.environ.get("REPRO_MAX_CONTEXT", "100000"))

# "direct" (bare vLLM/SGLang) | "gateway" (an LLM gateway). Only affects which
# thinking-off field t9 sends; the json_schema verdict is unchanged either way.
BACKEND = os.environ.get("REPRO_BACKEND", "direct")

REQUEST_TIMEOUT = float(os.environ.get("REPRO_TIMEOUT", "120"))
VERIFY_TLS = os.environ.get("REPRO_VERIFY_TLS", "1") != "0"   # set 0 for self-signed
# ─────────────────────────────────────────────────────────────────────────────

PASS, FAIL = "PASS", "FAIL"


def _endpoint() -> str:
    """Mirror utils.api.join_endpoint(BASE_URL, 'chat/completions')."""
    url = BASE_URL.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        url = url[:-3]
    return f"{url}/v1/chat/completions"


_URL = _endpoint()
_SSL_CTX = None if VERIFY_TLS else ssl._create_unverified_context()


def chat(body, stream=False):
    """POST /chat/completions. Returns (status, parsed_or_raw_text, elapsed).

    Mirrors functional_acceptance._Client.chat: for stream=True the second
    element is the raw SSE text; otherwise it's the parsed JSON (or {"_raw":...}).
    status=-1 signals a transport-level failure.
    """
    payload = dict(body)
    payload.setdefault("model", MODEL)
    if stream:
        payload.setdefault("stream", True)
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_URL, data=data, headers=headers, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", "replace")
            elapsed = time.monotonic() - t0
            code = resp.getcode()
    except urllib.error.HTTPError as ex:
        # 4xx/5xx still carry a body — read it so callers see the error envelope.
        raw = ex.read().decode("utf-8", "replace") if ex.fp else ""
        elapsed = time.monotonic() - t0
        code = ex.code
    except Exception as ex:  # transport error (DNS, refused, timeout, TLS, ...)
        return -1, {"_error": str(ex)}, time.monotonic() - t0
    if stream:
        return code, raw, elapsed
    try:
        return code, json.loads(raw), elapsed
    except Exception:
        return code, {"_raw": raw}, elapsed


# -- response accessors (verbatim from the suite) -----------------------------
def _content(body):
    try:
        return body["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""


def _finish(body):
    try:
        return body["choices"][0].get("finish_reason") or ""
    except Exception:
        return ""


def _ok(code):
    return 200 <= code < 300


def _4xx(code):
    return 400 <= code < 500


def _snippet(obj, n=400):
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    s = s.replace("\n", "\\n")
    return s[:n] + ("…" if len(s) > n else "")


def _thinking_off(body):
    """Mirror FunctionalAcceptanceTest._with_thinking(body, enabled=False)."""
    out = dict(body)
    ml = MODEL.lower()
    if "glm" in ml or "qwen" in ml:
        fields = {"chat_template_kwargs": {"enable_thinking": False}}
    elif BACKEND == "gateway":
        fields = {"thinking": {"type": "disabled"}}
    else:  # direct vllm/sglang: send both known spellings (harmless if unused)
        fields = {"chat_template_kwargs": {"thinking": False, "enable_thinking": False}}
    for k, v in fields.items():
        if k == "chat_template_kwargs" and isinstance(out.get(k), dict):
            merged = dict(out[k]); merged.update(v); out[k] = merged
        else:
            out[k] = v
    return out


# -- result table -------------------------------------------------------------
_results = []


def record(key, status, detail):
    _results.append((key, status, detail))
    sym = "✓" if status == PASS else "✗"
    print(f"  {sym} {status}  {key}\n      {detail}\n")


# ── the 5 checks ─────────────────────────────────────────────────────────────
def check_d06_cache_hit():
    print("[d06_cache_hit] same long prompt sent twice; expect cached_tokens>0 on the 2nd")
    long_prompt = ("Explain the concept of quantum superposition in detail with examples "
                   "from physics experiments. " * 20)
    cache_body = {"messages": [{"role": "user", "content": long_prompt}], "max_tokens": 20,
                  "stream": True, "stream_options": {"include_usage": True}}

    def usage_from_stream(raw_text):
        # NOTE: the suite's _usage_from_stream returns on the FIRST usage chunk,
        # which is wrong for servers that emit continuous per-chunk usage stats —
        # cached_tokens only appears in the FINAL usage chunk. Scan all chunks and
        # take the prompt_tokens from the last usage seen and the max cached_tokens.
        if not isinstance(raw_text, str):
            return 0, 0
        pt = cached = 0
        for line in raw_text.splitlines():
            if not (line.startswith("data: ") and "prompt_tokens" in line):
                continue
            try:
                u = (json.loads(line[6:]).get("usage") or {})
            except Exception:
                continue
            pt = u.get("prompt_tokens", 0) or pt
            cached = max(cached, (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        return pt, cached

    code1, raw1, _ = chat(cache_body, stream=True)
    pt1, ch1 = usage_from_stream(raw1)
    code2, raw2, _ = chat(cache_body, stream=True)
    pt2, ch2 = usage_from_stream(raw2)
    if ch2 > 0:
        record("d06_cache_hit", PASS, f"1st(pt={pt1},cached={ch1}) 2nd(pt={pt2},cached={ch2})")
    else:
        record("d06_cache_hit", FAIL,
               f"1st(http={code1} pt={pt1},cached={ch1}) 2nd(http={code2} pt={pt2},cached=0)\n"
               f"      2nd-stream-tail: {_snippet(raw2[-400:] if isinstance(raw2, str) else raw2)}")


def check_t3_max_tokens_mid():
    reserve = min(2048, max(1, MAX_CONTEXT_TOKENS // 4))
    mid = min(65536, MAX_CONTEXT_TOKENS - reserve)
    print(f"[t3_max_tokens_mid] max_tokens={mid} (reserve={reserve}); expect HTTP 2xx and finish=stop")
    code, body, _ = chat({"messages": [{"role": "user", "content": "Write a short story"}],
                          "max_tokens": mid})
    fin = _finish(body)
    ok = _ok(code) and fin == "stop"
    record("t3_max_tokens_mid", PASS if ok else FAIL,
           f"HTTP {code} finish={fin!r}" + ("" if ok else f"\n      body: {_snippet(body)}"))


def check_t3_max_tokens_over():
    over = MAX_CONTEXT_TOKENS + 1
    print(f"[t3_max_tokens_over] max_tokens={over} (ctx+1); expect HTTP 4xx (overflow rejected)")
    code, body, _ = chat({"messages": [{"role": "user", "content": "Write a short story"}],
                          "max_tokens": over})
    ok = _4xx(code)
    record("t3_max_tokens_over", PASS if ok else FAIL,
           f"HTTP {code} (expect 4xx) finish={_finish(body)!r}"
           + ("" if ok else f"\n      body: {_snippet(body)}"))


def check_t9_json_schema():
    print("[t9_json_schema] response_format=json_schema; expect parsed {name:Alice, age:30}")
    body_req = _thinking_off({
        "messages": [{"role": "user", "content": "Output a person: name=Alice age=30"}],
        "max_tokens": 100,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "person", "schema": {"type": "object", "properties": {
                "name": {"type": "string"}, "age": {"type": "integer"}},
                "required": ["name", "age"]}}}})
    code, body, _ = chat(body_req)
    try:
        parsed = json.loads(_content(body))
        if _ok(code) and parsed.get("name") == "Alice" and parsed.get("age") == 30:
            record("t9_json_schema", PASS, f"parsed={parsed}")
        else:
            record("t9_json_schema", FAIL,
                   f"HTTP {code} parsed={parsed}\n      content: {_snippet(_content(body) or body)}")
    except Exception as ex:
        record("t9_json_schema", FAIL,
               f"HTTP {code} parse error: {ex}\n      content: {_snippet(_content(body) or body)}")


def check_t16b_missing_content():
    print("[t16b_missing_content] message {role:user} with no content; expect HTTP 4xx")
    code, body, _ = chat({"messages": [{"role": "user"}], "max_tokens": 5})
    ok = _4xx(code)
    record("t16b_missing_content", PASS if ok else FAIL,
           f"HTTP {code} (expect 4xx)" + ("" if ok else f"\n      body: {_snippet(body)}"))


def main():
    print("=" * 78)
    print("functional-acceptance failure reproduction")
    print(f"  URL   : {_URL}")
    print(f"  MODEL : {MODEL}")
    print(f"  ctx   : {MAX_CONTEXT_TOKENS}   backend={BACKEND}   auth={'yes' if API_KEY else 'no'}")
    print("=" * 78 + "\n")

    check_d06_cache_hit()
    check_t3_max_tokens_mid()
    check_t3_max_tokens_over()
    check_t9_json_schema()
    check_t16b_missing_content()

    passed = sum(1 for _, s, _ in _results if s == PASS)
    failed = sum(1 for _, s, _ in _results if s == FAIL)
    print("=" * 78)
    print(f"summary: {passed} PASS / {failed} FAIL  ({len(_results)} checks)")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
