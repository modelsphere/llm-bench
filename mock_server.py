#!/usr/bin/env python3
"""
mock_server.py — OpenAI-compatible mock API server for benchmark testing.

Simulates an LLM serving endpoint (sglang / vLLM shaped) with configurable
latency and throughput. Nothing it reports measures anything; it exists so the
benchmark pipeline can be exercised end to end without a GPU.
Supports: streaming SSE chat completions, tool calls, JSON mode,
          /v1/completions, /v1/models, /health.

Usage:
    python mock_server.py
    python mock_server.py --port 8000 --ttft-ms 500 --tpot-ms 20 --output-tokens 128
    # Fast smoke test (low latency, few tokens):
    python mock_server.py --ttft-ms 50 --tpot-ms 5 --output-tokens 32
"""

import argparse
import asyncio
import json
import random
import time
import uuid

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The model name served at /v1/models and echoed in responses (--model).
MODEL_ID = "mock"

# Token-like words used to fill streaming output
_WORDS = (
    "Hello world this is a mock response from the test server for benchmarking "
    "purposes the quick brown fox jumps over the lazy dog testing performance "
    "metrics latency throughput tokens per second time to first token output "
    "generation language model inference serving evaluation benchmark results "
    "concurrency streaming async request response pipeline processing queue "
).split()


# ---------------------------------------------------------------------------
# Server state (single event-loop thread — no lock needed)
# ---------------------------------------------------------------------------

class ServerState:
    def __init__(self) -> None:
        self.active_requests: int = 0


_state = ServerState()


# ---------------------------------------------------------------------------
# Latency helpers
# ---------------------------------------------------------------------------

def _jitter(base_ms: float) -> float:
    """Return base_ms ± 20% uniform jitter, converted to seconds."""
    offset = base_ms * 0.2 * (2 * random.random() - 1)
    return max(0.0, (base_ms + offset) / 1000.0)


def _scaled_tpot(cfg: argparse.Namespace) -> float:
    """TPOT (ms) scaled linearly by concurrency beyond max_concurrency."""
    load_factor = max(1.0, _state.active_requests / cfg.max_concurrency)
    return _jitter(cfg.tpot_ms * load_factor)


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------

def _prompt_tokens(messages: list) -> int:
    """Rough token estimate: 1 token ≈ 4 chars."""
    total = sum(
        max(1, len(m.get("content") or "" if isinstance(m.get("content"), str)
                   else str(m.get("content") or "")) // 4)
        for m in messages
    )
    return max(10, total)


def _last_user_content(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return (m.get("content") or "").lower()
    return ""


def _resolve_n_tokens(body: dict, cfg: argparse.Namespace) -> tuple[int, str, str]:
    """Return (n_tokens, finish_reason, source) honouring max_tokens / min_tokens / ignore_eos.

    Priority:
      1. truncate_at (server-side truncation simulation) always wins.
      2. ignore_eos=True → produce exactly min(min_tokens or max_tokens, cfg.output_tokens).
      3. max_tokens from request is honored fully; min_tokens sets a floor (clamped to max_tokens).
      4. Default: cfg.output_tokens (only used when request has no max_tokens).
    """
    # Request-level caps — max_completion_tokens is the newer OpenAI name for max_tokens
    _has_max_tokens = body.get("max_tokens") is not None
    _has_max_completion_tokens = body.get("max_completion_tokens") is not None
    if _has_max_tokens and _has_max_completion_tokens:
        req_max = int(body["max_tokens"])
        req_max_field = "max_tokens (BOTH max_tokens and max_completion_tokens present — using max_tokens)"
    elif _has_max_tokens:
        req_max = int(body["max_tokens"])
        req_max_field = "max_tokens"
    elif _has_max_completion_tokens:
        req_max = int(body["max_completion_tokens"])
        req_max_field = "max_completion_tokens"
    else:
        req_max = None
        req_max_field = None
    req_min = body.get("min_tokens")
    ignore_eos = bool(body.get("ignore_eos", False))

    if req_min is not None:
        req_min = int(req_min)

    if ignore_eos and (req_min is not None or req_max is not None):
        # Caller wants a fixed-length output — honour min_tokens first, then max_tokens
        raw = req_min if req_min is not None else req_max
        assert raw is not None  # branch condition guarantees this
        n_tokens = int(raw)
        target = n_tokens
        finish_reason = "stop"
        cap_field = "min_tokens" if req_min is not None else req_max_field
        source = f"ignore_eos+{cap_field}={target}"
    elif req_max is not None:
        # Respect the caller's ceiling fully
        n_tokens = req_max
        if req_min is not None:
            n_tokens = max(n_tokens, min(req_min, n_tokens))
        finish_reason = "stop"
        source = f"req.{req_max_field}={req_max}" + (f"+req.min_tokens={req_min}" if req_min is not None else "")
    else:
        # No request-level cap — use server default
        n_tokens = cfg.output_tokens
        finish_reason = "stop"
        source = f"--output-tokens default={cfg.output_tokens}"

    if cfg.max_output_tokens > 0 and n_tokens > cfg.max_output_tokens:
        source += f" → clamped by --max-output-tokens={cfg.max_output_tokens}"
        n_tokens = cfg.max_output_tokens
        finish_reason = "stop"
    if cfg.truncate_at > 0 and n_tokens > cfg.truncate_at:
        return cfg.truncate_at, "length", source + f" → truncated by --truncate-at={cfg.truncate_at}"
    return n_tokens, finish_reason, source


# ---------------------------------------------------------------------------
# Non-streaming response builders
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:16]}"


def _make_usage(prompt_tokens: int, completion_tokens: int,
                reasoning_tokens: int = 0) -> dict:
    """Build a usage dict. ``completion_tokens`` is the TOTAL output (reasoning +
    visible content), matching reasoning servers like GLM/DeepSeek/vLLM; the
    reasoning portion is also broken out under ``completion_tokens_details`` the
    way the OpenAI reasoning API reports it."""
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if reasoning_tokens > 0:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return usage


def _build_tool_response(body: dict) -> dict:
    tools: list = body.get("tools", [])
    messages: list = body.get("messages", [])

    if not tools:
        return _build_plain_response(messages, n_tokens=20)

    content = _last_user_content(messages)
    tool = _select_tool(tools, content)
    args = _build_tool_args(tool)
    pt = _prompt_tokens(messages)

    return {
        "id": _new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": tool["function"]["name"],
                        "arguments": json.dumps(args),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": _make_usage(pt, 25),
    }


def _select_tool(tools: list, content: str) -> dict:
    for t in tools:
        fname = t["function"]["name"].lower()
        if "weather" in fname and any(w in content for w in ("weather", "temperature", "climate", "beijing", "shanghai")):
            return t
        if "calculat" in fname and any(w in content for w in ("calculat", "compute", "1337", "multiply", "fahrenheit", "convert", "+")):
            return t
        if "search" in fname and any(w in content for w in ("search", "eiffel", "tower", "tell me about", "who is", "what is")):
            return t
    return tools[0]


def _build_tool_args(tool: dict) -> dict:
    props = tool["function"].get("parameters", {}).get("properties", {})
    args: dict = {}
    for prop, schema in props.items():
        t_str = schema.get("type", "string")
        if t_str == "string":
            enum = schema.get("enum")
            if enum:
                args[prop] = enum[0]
            elif prop == "city":
                args[prop] = "Shanghai"
            elif prop == "expression":
                args[prop] = "1337 * 42"
            elif prop == "query":
                args[prop] = "Eiffel Tower"
            else:
                args[prop] = "test"
        elif t_str in ("number", "integer"):
            args[prop] = 42
    return args


def _build_json_response(messages: list, max_tokens: int) -> dict:
    content = _last_user_content(messages)
    if "alice" in content or "person" in content:
        data = {"name": "Alice", "age": 30, "occupation": "software engineer"}
    elif "cable" in content or "product" in content:
        data = {"title": "Red USB-C Cable", "price": 9.99, "in_stock": True,
                "tags": ["cable", "usb-c", "charging"]}
    elif "summit" in content or "event" in content:
        data = {"event_name": "AI Summit 2025", "date": "March 15, 2025",
                "location": "San Francisco", "attendees_count": 500}
    elif "sentiment" in content or "love" in content:
        data = {"label": "positive", "confidence": 0.95}
    elif "bob" in content or "permission" in content or "user" in content:
        data = {"user": {"id": 1, "name": "Bob"}, "permissions": ["read", "write", "delete"]}
    else:
        data = {"result": "ok", "status": "success"}

    body = json.dumps(data, ensure_ascii=False)
    pt = _prompt_tokens(messages)
    ct = max(1, len(body) // 4)
    return {
        "id": _new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": body},
                     "finish_reason": "stop"}],
        "usage": _make_usage(pt, ct),
    }


def _build_plain_response(messages: list, n_tokens: int,
                           finish_reason: str = "stop") -> dict:
    words = [_WORDS[i % len(_WORDS)] for i in range(n_tokens)]
    body = " ".join(words)
    pt = _prompt_tokens(messages)
    return {
        "id": _new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": body},
                     "finish_reason": finish_reason}],
        "usage": _make_usage(pt, n_tokens),
    }


# ---------------------------------------------------------------------------
# Streaming SSE helpers
# ---------------------------------------------------------------------------

def _sse(data: dict) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"


def _chunk(cid: str, delta: dict, finish_reason=None) -> bytes:
    return _sse({
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    })


async def _stream_response(
    response: web.StreamResponse,
    messages: list,
    n_tokens: int,
    cid: str,
    cfg: argparse.Namespace,
    finish_reason: str = "stop",
) -> int:
    """
    Write an SSE stream: TTFT delay → role delta → per-token chunks → final chunk.

    Handles client disconnects (ClientConnectionResetError / ConnectionResetError)
    gracefully: stops writing and returns without raising.

    Returns the number of tokens actually written (may be less than n_tokens on disconnect).
    """
    _state.active_requests += 1
    actual = 0
    try:
        pt = _prompt_tokens(messages)

        # ── TTFT ───────────────────────────────────────────────────────────
        await asyncio.sleep(_jitter(cfg.ttft_ms))

        # ── Role delta (first chunk) ───────────────────────────────────────
        await response.write(_chunk(cid, {"role": "assistant", "content": ""}))

        # ── Reasoning stream (mimics GLM/DeepSeek/vLLM `reasoning_content`) ──
        # Emitted BEFORE any visible content, just like a thinking model. The
        # first reasoning chunk should set TTFT; reasoning counts toward output.
        reasoning = 0
        for i in range(cfg.reasoning_tokens):
            await asyncio.sleep(_scaled_tpot(cfg))
            word = _WORDS[i % len(_WORDS)] + " "
            await response.write(_chunk(cid, {cfg.reasoning_field: word}))
            reasoning += 1

        # ── Token stream (visible content) ─────────────────────────────────
        for i in range(n_tokens):
            await asyncio.sleep(_scaled_tpot(cfg))
            word = _WORDS[i % len(_WORDS)] + (" " if i < n_tokens - 1 else "")
            await response.write(_chunk(cid, {"content": word}))
            actual += 1

        # ── Final chunk with usage ────────────────────────────────────────
        # completion_tokens includes reasoning (server convention).
        await response.write(_sse({
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": _make_usage(pt, reasoning + actual, reasoning_tokens=reasoning),
        }))
        await response.write(_SSE_DONE)

    except (ClientConnectionResetError, ConnectionResetError):
        # Client closed the connection mid-stream — not an error on our side.
        pass

    finally:
        _state.active_requests -= 1

    return actual


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_models(request: web.Request) -> web.Response:
    return web.json_response({
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": 1700000000,
                  "owned_by": "mock"}],
    })


async def handle_chat_completions(request: web.Request) -> web.Response:
    cfg: argparse.Namespace = request.app["cfg"]
    t0 = time.time()

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid JSON body")

    # Log the raw request body (truncate message content to keep it readable)
    debug_body = {k: v for k, v in body.items() if k != "messages"}
    def _truncate_content(c: object) -> str:
        s = c if isinstance(c, str) else json.dumps(c)
        return s[:80] + "…" if len(s) > 80 else s

    debug_body["messages"] = [
        {**m, "content": _truncate_content(m.get("content"))}
        for m in body.get("messages", [])
    ]
    print(f"[MOCK] RAW req    {json.dumps(debug_body)}")

    messages: list = body.get("messages", [])
    n_tokens, finish_reason, n_tokens_src = _resolve_n_tokens(body, cfg)
    streaming: bool = bool(body.get("stream", False))
    tools: list = body.get("tools", [])
    response_format: dict = body.get("response_format") or {}
    req_max = body.get("max_tokens") if body.get("max_tokens") is not None else body.get("max_completion_tokens")
    ignore_eos: bool = bool(body.get("ignore_eos", False))

    print(
        f"[MOCK] REQ start  streaming={streaming}  n_tokens={n_tokens}  "
        f"n_tokens_src=({n_tokens_src})  max_tokens={req_max}  ignore_eos={ignore_eos}  "
        f"active={_state.active_requests}  "
        f"tools={bool(tools)}  json_mode={response_format.get('type')=='json_object'}"
    )

    # ── Non-streaming path ─────────────────────────────────────────────────
    if not streaming:
        load_factor = max(1.0, _state.active_requests / cfg.max_concurrency)
        if tools:
            # Tool call responses are short JSON — use a fixed small delay
            await asyncio.sleep(_jitter(cfg.ttft_ms * load_factor))
            return web.json_response(_build_tool_response(body))
        if response_format.get("type") == "json_object":
            # JSON schema responses are also short — use a fixed small delay
            await asyncio.sleep(_jitter(cfg.ttft_ms * load_factor))
            return web.json_response(_build_json_response(messages, n_tokens))
        effective_ttft = 200.0 if n_tokens <= 128 else cfg.ttft_ms
        total_delay = _jitter(effective_ttft) + n_tokens * _jitter(cfg.tpot_ms * load_factor)
        await asyncio.sleep(total_delay)
        resp = web.json_response(_build_plain_response(messages, n_tokens, finish_reason))
        print(f"[MOCK] REQ done   non-stream  generated={n_tokens}  dur={time.time()-t0:.2f}s")
        return resp

    # ── Streaming SSE path ─────────────────────────────────────────────────
    cid = _new_id()
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    actual = await _stream_response(resp, messages, n_tokens, cid, cfg, finish_reason)
    await resp.write_eof()
    print(f"[MOCK] REQ done   stream      generated={actual}/{n_tokens}  dur={time.time()-t0:.2f}s")
    return resp


async def handle_completions(request: web.Request) -> web.Response:
    """Legacy /v1/completions endpoint."""
    cfg: argparse.Namespace = request.app["cfg"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid JSON body")

    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens") or cfg.output_tokens)
    n_tokens = min(max_tokens, cfg.output_tokens)
    words = [_WORDS[i % len(_WORDS)] for i in range(n_tokens)]
    text = " ".join(words)
    pt = max(1, len(str(prompt)) // 4)

    return web.json_response({
        "id": _new_id(),
        "object": "text_completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"text": text, "index": 0, "finish_reason": "stop"}],
        "usage": _make_usage(pt, n_tokens),
    })


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(cfg: argparse.Namespace) -> web.Application:
    app = web.Application()
    app["cfg"] = cfg  # pass config via app context instead of global
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/completions", handle_completions)
    return app


# ---------------------------------------------------------------------------
# CLI & entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible server for benchmark testing")
    parser.add_argument("--port", type=int, default=8000,
                        help="TCP port (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--model", type=str, default="mock",
                        help="Model name served at /v1/models (default: mock)")
    parser.add_argument("--ttft-ms", type=float, default=35210,
                        help="Time-to-first-token in ms (default: 35210 — a slow reasoning model)")
    parser.add_argument("--tpot-ms", type=float, default=49.4,
                        help="Per-token delay in ms (default: 49.4)")
    parser.add_argument("--output-tokens", type=int, default=1500,
                        help="Output tokens per response when request has no max_tokens (default: 1500)")
    parser.add_argument("--reasoning-tokens", type=int, default=0,
                        help="Stream this many reasoning tokens BEFORE the visible content, "
                             "mimicking a thinking model (GLM/DeepSeek/vLLM). They are added to "
                             "usage.completion_tokens. (default: 0 = no reasoning)")
    parser.add_argument("--reasoning-field", type=str, default="reasoning_content",
                        help="Delta key the reasoning tokens are streamed under (default: "
                             "reasoning_content, as GLM/vLLM use). Set to an unrecognized name "
                             "to simulate a server guidellm doesn't detect.")
    parser.add_argument("--max-output-tokens", type=int, default=0,
                        help="Hard cap on output tokens regardless of request max_tokens. "
                             "Useful for testing with datasets that contain huge max_tokens values. "
                             "(default: 0 = disabled, honour request max_tokens)")
    parser.add_argument("--truncate-at", type=int, default=0,
                        help="Simulate truncation: cap at N tokens with finish_reason='length' "
                             "(default: 0 = disabled)")
    parser.add_argument("--max-concurrency", type=int, default=1000,
                        help="Requests beyond this threshold cause linear TPOT scaling "
                             "(default: 1000 = effectively unlimited)")
    return parser.parse_args()


def main() -> None:
    global MODEL_ID
    cfg = parse_args()
    MODEL_ID = cfg.model

    print(f"Mock server starting on http://{cfg.host}:{cfg.port}")
    print(f"  TTFT: {cfg.ttft_ms} ms  |  TPOT: {cfg.tpot_ms} ms  |  output_tokens: {cfg.output_tokens}")
    if cfg.reasoning_tokens > 0:
        print(f"  REASONING: {cfg.reasoning_tokens} reasoning_content tokens streamed before content")
    print(f"  max_concurrency: {cfg.max_concurrency}  (tpot scales linearly beyond this)")
    if cfg.max_output_tokens > 0:
        print(f"  MAX OUTPUT TOKENS: {cfg.max_output_tokens}  (hard cap — requests asking for more will be clamped)")
    if cfg.truncate_at > 0:
        print(f"  TRUNCATION: finish_reason=length when request > {cfg.truncate_at} tokens")
    print(f"  Model: {MODEL_ID}")
    print()

    app = build_app(cfg)
    web.run_app(app, host=cfg.host, port=cfg.port, access_log=None)


if __name__ == "__main__":
    main()