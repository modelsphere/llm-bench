"""
Functional acceptance suite — 56 pass/fail flags.

Two groups of checks in one programmatic suite:
  - 10 "dimension" checks  (keys: d01..d10)
  - 46 extended checks     (keys: t1a..t16c)

Each check yields PASS / FAIL / SKIP. The module surfaces every flag as its
own metric (1.0 = PASS, 0.0 = FAIL; SKIP is omitted so the UI shows "—") plus
summary counts and a `pass_rate`. Full per-check detail is also written to
`functional_results.json` in the run's output dir.

Request-parameter conversion (IMPORTANT)
----------------------------------------
The original scripts assume an LLM gateway, whose *official top-level*
thinking switch `thinking={"type":"disabled"}` is NOT understood by a bare
vLLM / SGLang backend. When the backend resolves to "direct" (we test
vLLM/SGLang directly) every thinking switch is converted to the form the bare
backend honours:
    Kimi : chat_template_kwargs={"thinking": <bool>} — ONLY this spelling; sending
        "enable_thinking" too only half-disables Kimi-K3 and can shadow the real switch
    GLM  : chat_template_kwargs={"enable_thinking": <bool>}
    Qwen : chat_template_kwargs={"enable_thinking": <bool>}
    other/unknown: the switch is PROBED at run start — each candidate spelling is
        tried ALONE (never combined, so a half-working spelling can't shadow the
        real one, the Kimi-K3 trap) with a tiny greedy request, and the one that
        empirically zeroes reasoning wins. If no candidate works (model can't
        disable thinking), both chat_template_kwargs spellings are sent as before
        and the content checks absorb the reasoning via generous budgets.
    The `thinking_off_fields` param (JSON) overrides everything — a brand-new
    model family is a benchmark-config change, not a code change.
When it resolves to "gateway" the official top-level format is used instead
(Kimi: top-level `thinking={"type": "enabled"|"disabled"}`; GLM keeps
`chat_template_kwargs.enable_thinking`).

`backend_style` selects this: "auto" (default) detects bare engine vs gateway
with best-effort probes (engine-native endpoints / engine-specific response
fields); "direct" / "gateway" force it. Detection also gates a few checks whose
correct behaviour differs by backend (top_p=0.0 validity, auth enforcement):
under "gateway" they are asserted strictly (so a misconfigured gateway is
caught); under "direct" they are relaxed/skipped (bare-engine behaviour is
spec-correct, not a failure).

Reasoning-model robustness (IMPORTANT)
--------------------------------------
Two failure modes plague reasoning models and must not FAIL a healthy endpoint:

1. *Budget starvation* — a thinking model spends completion tokens on reasoning
   BEFORE any visible content, so a tight max_tokens intermittently yields
   finish=length with content empty. Checks that assert on the answer text get
   budgets big enough to survive a reasoning preamble (even when we ask for
   thinking off — the switch may be ignored by the model or dropped by a
   router), and checks that are unjudgeable when the budget dies mid-reasoning
   record SKIP (inconclusive), never FAIL.
2. *Reasoning transport variance* — depending on engine flags and any router in
   front, reasoning arrives as `message.reasoning_content` (vLLM/SGLang),
   `message.reasoning` (many routers), `message.thinking`, a
   `message.reasoning_details` part list, or inline `<think>…</think>` tags
   left in `content` when no reasoning parser is configured. The helpers
   normalize all of these: `_reasoning()` finds reasoning by ANY transport,
   `_visible_content()` is the user-facing answer (leading think block
   stripped). Answer-matching checks judge the VISIBLE content only, so
   reasoning text can neither hide the answer nor false-satisfy a match.
   (`_reasoning_field()` deliberately ignores inline tags — D7 uses it to judge
   whether the parser actually split the fields.)
"""
from __future__ import annotations

import base64
import json
import re
import struct
import time
import zlib
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from utils.api import join_endpoint, to_base_url
from utils.logger import logger

ProgressCallback = Callable[[float, str], None]


def _noop_progress(fraction: float, message: str) -> None:
    pass


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

def _off_fields_to_on(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the thinking-ON form of an OFF fields dict: False → True and
    "disabled" → "enabled", recursively. Lets one probed/configured OFF form
    serve both directions of _with_thinking()."""
    def flip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: flip(x) for k, x in v.items()}
        if v is False:
            return True
        if isinstance(v, str) and v.lower() == "disabled":
            return "enabled"
        return v
    return flip(fields)


# Thinking-switch candidates for model families the code doesn't recognise,
# probed ONE AT A TIME — never combined, because Kimi-K3 proved a half-working
# spelling can shadow the real one when both are sent. Each maps enabled → the
# request fields to merge. Extend this list when a genuinely new switch style
# appears; existing families never reach it.
_THINKING_SWITCH_CANDIDATES: List[Tuple[str, Callable[[bool], Dict[str, Any]]]] = [
    ("chat_template_kwargs.thinking",
     lambda en: {"chat_template_kwargs": {"thinking": en}}),
    ("chat_template_kwargs.enable_thinking",
     lambda en: {"chat_template_kwargs": {"enable_thinking": en}}),
    # Top-level gateway/official form last: bare vLLM 400s on unknown
    # top-level fields, so it can only ever win behind a gateway (where the
    # probe reorders it first).
    ("thinking.type",
     lambda en: {"thinking": {"type": "enabled" if en else "disabled"}}),
]


# max_tokens for checks that ask thinking OFF but judge the visible answer:
# the budget must survive the off-switch being IGNORED (model can't disable /
# router drops chat_template_kwargs / gateway misdetected), because a reasoning
# preamble spends completion tokens before any visible content. Kimi-K3 thinks
# ~300 tokens even on trivial prompts with heavy run-to-run variance — 512 made
# PASS/FAIL a coin flip against the thinking length. 4096 is order-of-magnitude
# headroom at negligible cost (switch honoured → actual usage stays tiny).
THINKING_HEADROOM_MAX_TOKENS = 4096

# 64x64 solid-red PNG (base64) — minimal image for the multimodal checks.
RED_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAeUlEQVR4nO3PQQkAMAzA"
    "wCqpfymTNRF7HINABFzm7H7dcEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUN"
    "aEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAW"
    "NKAFj13Kp0DxHeM4GQAAAABJRU5ErkJggg=="
)

QUADRANT_COLORS = ("red", "green", "blue", "yellow")
_quadrant_png_b64_cache: Optional[str] = None


def quadrant_png_b64() -> str:
    """4096x4096 PNG split into 4 solid quadrants: red (top-left), green
    (top-right), blue (bottom-left), yellow (bottom-right).

    ~78 KB of base64, so it is generated (stdlib only, ~0.1 s) instead of
    being embedded as a literal, and cached for the process lifetime.
    """
    global _quadrant_png_b64_cache
    if _quadrant_png_b64_cache is not None:
        return _quadrant_png_b64_cache

    size, half = 4096, 2048
    red, green, blue, yellow = b"\xff\x00\x00", b"\x00\xff\x00", b"\x00\x00\xff", b"\xff\xff\x00"
    top_row = b"\x00" + red * half + green * half      # leading 0 = PNG "None" filter
    bottom_row = b"\x00" + blue * half + yellow * half
    raw = top_row * half + bottom_row * half

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    _quadrant_png_b64_cache = base64.b64encode(png).decode("ascii")
    return _quadrant_png_b64_cache

# ---------------------------------------------------------------------------
# Parametrised case tables (shared by the catalog and the runner so keys never
# drift). Each entry: (key, display, extra_body, expect_ok)
# ---------------------------------------------------------------------------

SAMPLING_CASES: List[Tuple[str, str, Dict[str, Any], bool]] = [
    ("t2_temperature_0_0",        "T2 temperature=0.0",        {"temperature": 0.0},        True),
    ("t2_temperature_1_0",        "T2 temperature=1.0",        {"temperature": 1.0},        True),
    ("t2_temperature_1_1",        "T2 temperature=1.1",        {"temperature": 1.1},        True),
    ("t2_temperature_2_0",        "T2 temperature=2.0",        {"temperature": 2.0},        True),
    ("t2_top_p_0_0",              "T2 top_p=0.0",              {"top_p": 0.0},              True),
    ("t2_top_p_0_01",             "T2 top_p=0.01",             {"top_p": 0.01},             True),
    ("t2_top_p_0_95",             "T2 top_p=0.95",             {"top_p": 0.95},             True),
    ("t2_top_p_1_0",              "T2 top_p=1.0",              {"top_p": 1.0},              True),
    ("t2_top_p_1_1",              "T2 top_p=1.1 (expect 4xx)", {"top_p": 1.1},              False),
    ("t2_frequency_penalty_neg2", "T2 frequency_penalty=-2",   {"frequency_penalty": -2},   True),
    ("t2_frequency_penalty_0",    "T2 frequency_penalty=0",    {"frequency_penalty": 0},    True),
    ("t2_frequency_penalty_2",    "T2 frequency_penalty=2",    {"frequency_penalty": 2},    True),
    ("t2_presence_penalty_neg2",  "T2 presence_penalty=-2",    {"presence_penalty": -2},    True),
    ("t2_presence_penalty_0",     "T2 presence_penalty=0",     {"presence_penalty": 0},     True),
    ("t2_presence_penalty_2",     "T2 presence_penalty=2",     {"presence_penalty": 2},     True),
    ("t2_n_1",                    "T2 n=1",                    {"n": 1},                    True),
    ("t2_n_2",                    "T2 n=2",                    {"n": 2},                    True),
]

# T3 keys are static; the actual max_tokens values depend on max_context_tokens
# and are computed in the runner. (key, display, expect_ok, expect_finish)
MAXTOKENS_CASES: List[Tuple[str, str, bool, str]] = [
    ("t3_max_tokens_none",  "T3 max_tokens=None",          True,  "stop"),
    ("t3_max_tokens_1",     "T3 max_tokens=1",             True,  "length"),
    ("t3_max_tokens_64",    "T3 max_tokens=64",            True,  "length"),
    ("t3_max_tokens_mid",   "T3 max_tokens=64K",           True,  "stop"),
    ("t3_max_tokens_max",   "T3 max_tokens=near ctx limit", True,  "stop"),
    ("t3_max_tokens_neg1",  "T3 max_tokens=-1 (expect 4xx)",     False, ""),
    ("t3_max_tokens_over",  "T3 max_tokens>context (expect 4xx)", False, ""),
]

LANG_CASES: List[Tuple[str, str, str, str]] = [
    ("t12_chinese",  "T12 Chinese",  "Repeat exactly: '中文测试：你好世界'", "中文测试：你好世界"),
    ("t12_japanese", "T12 Japanese", "Repeat exactly: '日本語: こんにちは'", "こんにちは"),
    ("t12_emoji",    "T12 Emoji",    "Repeat exactly: 'Emoji: 🎉🚀✨🇨🇳'", "🎉"),
]

# Full ordered catalog of (key, display_name). Single source of truth for the
# module's metric descriptors / configs.
TEST_CATALOG: List[Tuple[str, str]] = (
    [
        ("d01_basic_nostream",        "D1 basic chat (non-stream)"),
        ("d02_stream_usage",          "D2 basic chat (stream) + usage"),
        ("d03_tool_call",             "D3 tool calling"),
        ("d04_reasoning",             "D4 reasoning parse"),
        ("d05_multimodal",            "D5 multimodal image"),
        ("d06_cache_hit",             "D6 prompt cache hit"),
        ("d07_reasoning_plus_content","D7 reasoning + content split"),
        ("d08_image_url_blocked",     "D8 external image URL blocked"),
        ("d09_thinking_disable_top",  "D9 thinking off (official top-level fmt)"),
        ("d10_thinking_disable_ctk",  "D10 thinking off (chat_template_kwargs)"),
        ("t1a_thinking_true",         "T1a thinking=true"),
        ("t1b_thinking_false",        "T1b thinking=false"),
        ("t1c_thinking_default",      "T1c thinking=default"),
    ]
    + [(k, d) for (k, d, _e, _o) in SAMPLING_CASES]
    + [(k, d) for (k, d, _o, _f) in MAXTOKENS_CASES]
    + [
        ("t4a_no_system",     "T4a no system prompt"),
        ("t4b_system_control","T4b system prompt override"),
        ("t5_function_calling","T5 function calling"),
        ("t6_multi_turn",     "T6 multi-turn memory"),
        ("t7_streaming_sse",  "T7 streaming SSE + usage"),
        ("t8_json_object",    "T8 response_format json_object"),
        ("t9_json_schema",    "T9 response_format json_schema"),
        ("t10_stop_word",     "T10 stop word"),
        ("t11a_no_auth",      "T11a missing auth → 401"),
        ("t11b_wrong_auth",   "T11b wrong auth → 401"),
    ]
    + [(k, d) for (k, d, _p, _s) in LANG_CASES]
    + [
        ("t13_multimodal_base64", "T13 multimodal 4K quadrant PNG"),
        ("t14_empty_body",        "T14 empty body → 4xx"),
        ("t15_idempotency_seed",  "T15 idempotency (seed, temp=0)"),
        ("t16a_missing_role",     "T16a missing role → 4xx"),
        ("t16b_missing_content",  "T16b missing content → 4xx"),
        ("t16c_empty_messages",   "T16c empty messages → 4xx"),
    ]
)

CATALOG_KEYS = {k for k, _ in TEST_CATALOG}


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self, api_url: str, model: str, api_key: str, timeout: float):
        self.url = join_endpoint(api_url, "chat/completions")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self, key: str) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def chat(
        self,
        body: Dict[str, Any],
        *,
        use_auth: bool = True,
        auth_key: Optional[str] = None,
        stream: bool = False,
    ) -> Tuple[int, Any, float]:
        """POST /chat/completions. Returns (status, parsed_or_raw, elapsed).

        For stream=True the third tuple element of `parsed_or_raw` is the raw
        SSE text. status=-1 signals a transport-level failure.
        """
        payload = dict(body)
        payload.setdefault("model", self.model)
        if stream:
            payload.setdefault("stream", True)
        if not use_auth:
            headers = {"Content-Type": "application/json"}
        else:
            headers = self._headers(self.api_key if auth_key is None else auth_key)
        data = json.dumps(payload).encode("utf-8")
        t0 = time.monotonic()
        try:
            resp = requests.post(self.url, data=data, headers=headers,
                                 timeout=self.timeout, stream=stream)
            raw = resp.text
            elapsed = time.monotonic() - t0
            if stream:
                return resp.status_code, raw, elapsed
            try:
                return resp.status_code, json.loads(raw), elapsed
            except Exception:
                return resp.status_code, {"_raw": raw}, elapsed
        except Exception as ex:  # noqa: BLE001
            return -1, {"_error": str(ex)}, time.monotonic() - t0


def _msg(body: Any) -> Dict[str, Any]:
    try:
        m = body["choices"][0]["message"]
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _text_of(v: Any) -> str:
    """Coerce a message field to text. Routers differ: content/reasoning may be
    a plain string, null, a typed-part list ([{"type":"text","text":…}]) or a
    wrapper dict — never let a shape difference crash a check."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(_text_of(p.get("text") if isinstance(p, dict) else p) for p in v)
    if isinstance(v, dict):
        return _text_of(v.get("text") or v.get("content") or "")
    return "" if v is None else str(v)


_THINK_RE = re.compile(r"^\s*<think(?:ing)?>(.*?)(?:</think(?:ing)?>|\Z)\s*",
                       re.DOTALL | re.IGNORECASE)


def _split_think(text: str) -> Tuple[str, str]:
    """Split a leading <think>…</think> block out of `content`.

    A deployment without a reasoning parser (bare engine missing
    --reasoning-parser, or a router forwarding raw template output) leaves the
    thinking inline in `content` instead of a dedicated field. Returns
    (reasoning, visible). An unclosed block — generation truncated mid-thought —
    counts entirely as reasoning."""
    if not text:
        return "", ""
    m = _THINK_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _content(body: Any) -> str:
    """Raw `message.content` as text (inline think tags NOT stripped)."""
    return _text_of(_msg(body).get("content"))


def _visible_content(body: Any) -> str:
    """`message.content` minus any leading inline think block — the answer an
    end user would actually see."""
    return _split_think(_content(body))[1]


def _reasoning_field(body: Any) -> str:
    """Reasoning reported in a dedicated message field, across the spellings
    seen on bare engines and routers: vLLM/SGLang `reasoning_content`, router
    `reasoning`, rarer `thinking`, and `reasoning_details` part lists. Inline
    <think> tags are deliberately ignored — use _reasoning() unless the check
    specifically judges whether the parser split the fields (D7)."""
    m = _msg(body)
    for k in ("reasoning_content", "reasoning", "thinking"):
        txt = _text_of(m.get(k))
        if txt:
            return txt
    det = m.get("reasoning_details")
    if isinstance(det, list):
        return "".join(_text_of(d.get("text") or d.get("summary"))
                       for d in det if isinstance(d, dict))
    return ""


def _reasoning(body: Any) -> str:
    """Reasoning by ANY transport: a dedicated field, or a leading inline
    <think> block when no parser split it out of `content`."""
    return _reasoning_field(body) or _split_think(_content(body))[0]


def _finish(body: Any) -> str:
    try:
        return body["choices"][0].get("finish_reason") or ""
    except Exception:
        return ""


def _ok(code: int) -> bool:
    return 200 <= code < 300


def _4xx(code: int) -> bool:
    return 400 <= code < 500


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------

@dataclass
class FunctionalAcceptanceTest:
    api_url: str
    model: str
    api_key: str
    backend_style: str = "auto"            # "auto" (detect) | "direct" (vllm/sglang) | "gateway" (an LLM gateway)
    request_timeout: float = 120.0
    max_context_tokens: int = 131072
    cache_min_prompt_tokens: int = 2048    # D6: prompt must exceed the largest expected cache block (page_size × dcp)
    thinking_off_fields: str = ""          # JSON request fields that disable thinking; "" = auto (family table, else probe)
    multimodal: str = "auto"               # "auto" | "on" | "off"
    tool_choice_mode: str = "auto"         # "auto" (model decides) | "named" (forced function)
    run_dimensions: bool = True            # the 10 shell checks
    run_extended: bool = True              # the 46 eval checks
    skip_tests: Optional[List[str]] = None # catalog keys to force-SKIP (unsupported features)
    progress_cb: ProgressCallback = _noop_progress
    cancel_event: Optional[Event] = None

    def __post_init__(self) -> None:
        self.c = _Client(self.api_url, self.model, self.api_key, self.request_timeout)
        self.results: List[Dict[str, Any]] = []
        self._labels = dict(TEST_CATALOG)
        self._total = len(TEST_CATALOG)
        self._done = 0
        # Checks the operator asked to skip (config `skip_tests`). Each is recorded
        # as SKIP — never PASS/FAIL — so an unsupported feature can't fail the module
        # (its metric is omitted, so its per-check redline is ignored too). Loop-based
        # checks short-circuit before issuing any request; singleton checks may issue
        # their (timeout-bounded) request before _record() coerces the result to SKIP.
        # Unknown keys are warned about and ignored rather than failing the whole suite.
        requested = list(self.skip_tests or [])
        self._skip_keys = {k for k in requested if k in CATALOG_KEYS}
        unknown = [k for k in requested if k not in CATALOG_KEYS]
        if unknown:
            logger.warning("[functional] skip_tests: ignoring %d unknown test key(s): %s",
                           len(unknown), ", ".join(sorted(set(unknown))))
        if self._skip_keys:
            logger.warning("[functional] skip_tests: %d check(s) will be skipped (recorded SKIP): %s",
                           len(self._skip_keys), ", ".join(sorted(self._skip_keys)))
        # Resolved "direct"/"gateway" — set in run() (detected for backend_style="auto",
        # else the explicit override). Defaults to "gateway" (strict) until resolved.
        self._resolved_backend = self.backend_style if self.backend_style in ("direct", "gateway") else "gateway"
        self._detect_reason = "explicit override" if self.backend_style in ("direct", "gateway") else "pending"
        # Resolved multimodal support for multimodal="auto" — probed once (lazily or
        # in run()) and cached. None = not yet probed.
        self._multimodal_resolved: Optional[bool] = None
        self._multimodal_reason = "explicit override" if self.multimodal in ("on", "off") else "pending"
        # Operator override for the thinking-off request fields (JSON object
        # merged into the request body). Beats the family table and the runtime
        # probe, so a brand-new model family is a config change, not a code
        # change. A malformed value is warned about and ignored (auto behavior),
        # never fatal.
        self._thinking_override: Optional[Dict[str, Any]] = None
        if (self.thinking_off_fields or "").strip():
            try:
                fields = json.loads(self.thinking_off_fields)
                if not isinstance(fields, dict) or not fields:
                    raise ValueError("must be a non-empty JSON object")
                self._thinking_override = fields
            except (ValueError, json.JSONDecodeError) as exc:
                logger.warning("[functional] thinking_off_fields ignored (%s): %r",
                               exc, self.thinking_off_fields)
        # Empirically probed switch for unknown model families — set in run().
        self._thinking_probed: Optional[Callable[[bool], Dict[str, Any]]] = None

    # -- thinking-parameter conversion -----------------------------------
    @property
    def _is_glm(self) -> bool:
        return "glm" in self.model.lower()

    @property
    def _is_qwen(self) -> bool:
        return "qwen" in self.model.lower()

    @property
    def _is_kimi(self) -> bool:
        return "kimi" in self.model.lower()

    def _thinking_fields(self, enabled: bool) -> Dict[str, Any]:
        """Request fields that toggle thinking, converted for the target backend.

        Resolution order: operator override (thinking_off_fields) → known family
        table (verified spellings) → probed switch (unknown families, resolved
        empirically in run()) → both-spellings fallback."""
        if self._thinking_override is not None:
            return _off_fields_to_on(self._thinking_override) if enabled else dict(self._thinking_override)
        if self._is_glm or self._is_qwen:
            # GLM and Qwen both toggle via chat_template_kwargs["enable_thinking"]
            # in gateway and direct modes alike.
            return {"chat_template_kwargs": {"enable_thinking": enabled}}
        if self._is_kimi:
            if self._resolved_backend == "gateway":
                # Kimi official top-level switch (the gateway translates it).
                return {"thinking": {"type": "enabled" if enabled else "disabled"}}
            # Kimi honours chat_template_kwargs["thinking"] and ONLY that spelling.
            # Do NOT also send "enable_thinking": on Kimi-K3 it only half-disables
            # (reasoning shrinks to ~40 tokens instead of 0) and can shadow the
            # real switch when both are present. Verified on K3: {"thinking":
            # false} → 0 reasoning tokens, deterministic answer.
            return {"chat_template_kwargs": {"thinking": enabled}}
        # Unknown family: prefer the switch the run-start probe verified.
        if self._thinking_probed is not None:
            return self._thinking_probed(enabled)
        if self._resolved_backend == "gateway":
            return {"thinking": {"type": "enabled" if enabled else "disabled"}}
        # No probe result (probe failed or not yet run): send BOTH
        # chat_template_kwargs spellings — on most templates an unrecognised
        # kwarg is just an unused variable (the probe exists precisely for the
        # exceptions, cf. the Kimi-K3 shadow trap).
        return {"chat_template_kwargs": {"thinking": enabled, "enable_thinking": enabled}}

    # -- backend detection -----------------------------------------------
    def _detect_backend(self) -> Tuple[str, str]:
        """Best-effort: distinguish a bare engine ("direct") from a router/gateway
        ("gateway"). Requires *affirmative* engine evidence to return "direct";
        absent any, defaults to "gateway" so an uncertain or misconfigured gateway
        is still held to the stricter expectations. Returns (resolved, reason).

        There is no fully reliable signal — a gateway can transparently proxy an
        engine — so this is intentionally conservative, with explicit
        backend_style="direct"/"gateway" as the override.
        """
        base = to_base_url(self.api_url).rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        timeout = min(15.0, self.request_timeout)
        # 1) Engine-native endpoints a gateway that only proxies /v1/* won't expose.
        for path, engine in (("/get_model_info", "SGLang"),
                             ("/get_server_info", "SGLang"),
                             ("/version", "vLLM")):
            try:
                r = requests.get(base + path, headers=headers, timeout=timeout)
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and ctype.startswith("application/json"):
                    return "direct", f"native endpoint {path} → 200 ({engine})"
            except requests.RequestException:
                pass
        # 2) Engine-specific response fields in a chat completion (survive even when
        #    native endpoints are firewalled; stripped by gateways that re-serialize).
        try:
            code, body, _ = self.c.chat({"messages": [{"role": "user", "content": "hi"}],
                                         "max_tokens": 1})
            if _ok(code) and isinstance(body, dict):
                choice = (body.get("choices") or [{}])[0]
                if "matched_stop" in choice:
                    return "direct", "response carries SGLang 'matched_stop' field"
                if "stop_reason" in choice:
                    return "direct", "response carries vLLM 'stop_reason' field"
        except Exception:  # noqa: BLE001
            pass
        return "gateway", "no engine-native endpoints or response fields found"

    def _probe_thinking_switch(self) -> None:
        """Unknown model family: find which single switch spelling actually stops
        thinking, instead of guessing from the model name. Costs 1 tiny greedy
        request for a non-thinking model, 2-4 for a thinking one; skipped
        entirely for known families and configured overrides. On success every
        _with_thinking() conversion uses the winner; on failure the
        both-spellings fallback stays and the content checks absorb the
        reasoning via THINKING_HEADROOM_MAX_TOKENS budgets."""
        base = {"messages": [{"role": "user", "content": "2+2=?"}],
                "max_tokens": 512, "temperature": 0}
        code, body, _ = self.c.chat(dict(base))
        if not _ok(code) or not _reasoning(body).strip():
            logger.info("[functional] thinking probe: no default reasoning "
                        "(HTTP %s) — model does not think, no off-switch needed", code)
            return
        candidates = list(_THINKING_SWITCH_CANDIDATES)
        if self._resolved_backend == "gateway":
            # Behind a gateway the official top-level form is the most likely
            # winner (and is harmless to try first — gateways don't 400 on it).
            candidates.sort(key=lambda c: c[0] != "thinking.type")
        for label, fields_fn in candidates:
            if self._cancelled():
                return
            req = dict(base)
            req.update(fields_fn(False))
            code, body, _ = self.c.chat(req)
            if _ok(code) and not _reasoning(body).strip() and _visible_content(body).strip():
                self._thinking_probed = fields_fn
                logger.info("[functional] thinking probe: %s zeroes reasoning on %s — "
                            "using it for all thinking conversions", label, self.model)
                return
        logger.warning("[functional] thinking probe: no candidate switch disabled "
                       "thinking on %s — falling back to both chat_template_kwargs "
                       "spellings. The switch checks (t1b, d09/d10) will report "
                       "this; content checks tolerate the extra reasoning.", self.model)

    def _with_thinking(self, body: Dict[str, Any], enabled: bool) -> Dict[str, Any]:
        out = dict(body)
        for k, v in self._thinking_fields(enabled).items():
            if k == "chat_template_kwargs" and isinstance(out.get(k), dict):
                merged = dict(out[k]); merged.update(v); out[k] = merged
            else:
                out[k] = v
        return out

    # -- result recording -------------------------------------------------
    def _record(self, key: str, status: str, detail: str, elapsed: float = 0.0) -> None:
        if key not in CATALOG_KEYS:
            raise KeyError(f"functional_acceptance: unknown test key {key!r}")
        if key in self._skip_keys:
            # Configured skip wins over whatever the check computed. A singleton
            # check may already have issued its request; we still record SKIP (never
            # PASS/FAIL) so an unsupported feature can't fail the module.
            if status != SKIP:
                logger.warning("[functional] %s skipped by config (skip_tests); "
                               "computed %s discarded", key, status)
            status, detail = SKIP, "skipped by config (skip_tests)"
        self.results.append({"key": key, "name": self._labels[key],
                             "status": status, "detail": detail, "elapsed": elapsed})
        self._done += 1
        symbol = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"}[status]
        logger.info("[functional] %s %s %s — %s (%.2fs)", symbol, status, key, detail, elapsed)
        self.progress_cb(0.05 + 0.9 * (self._done / max(1, self._total)),
                         f"{self._done}/{self._total} {key}: {status}")

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _skip(self, key: str) -> bool:
        """True if `key` is in the configured skip set — records it SKIP and returns
        True so a loop can `continue` before issuing any request. The SKIP result
        (and its warning) are emitted by _record()."""
        if key in self._skip_keys:
            self._record(key, SKIP, "skipped by config (skip_tests)")
            return True
        return False

    # -- public entry -----------------------------------------------------
    def run(self) -> Dict[str, Any]:
        # Resolve direct vs gateway before any check runs — it drives the thinking
        # conversion and the backend-specific expectations (top_p=0.0, auth).
        if self.backend_style == "auto":
            self._resolved_backend, self._detect_reason = self._detect_backend()
        logger.info("[functional] backend_style=%s → resolved=%s (%s)",
                    self.backend_style, self._resolved_backend, self._detect_reason)
        # Unknown model family without an operator override: resolve the
        # thinking-off switch empirically before any check uses _with_thinking.
        if self._thinking_override is None and not (self._is_glm or self._is_qwen or self._is_kimi):
            self._probe_thinking_switch()
        self.progress_cb(0.04, f"Backend: {self._resolved_backend} ({self._detect_reason})")
        # Resolve multimodal support before any vision check so D5/D8/T13 gate on a
        # real probe rather than a model-name guess.
        if self.multimodal == "auto":
            self._multimodal_resolved, self._multimodal_reason = self._detect_multimodal()
        logger.info("[functional] multimodal=%s → resolved=%s (%s)",
                    self.multimodal, "on" if self._multimodal_enabled() else "off", self._multimodal_reason)
        self.progress_cb(0.045, f"Multimodal: {'on' if self._multimodal_enabled() else 'off'} ({self._multimodal_reason})")
        if self.run_dimensions:
            self._run_dimensions()
        if self.run_extended and not self._cancelled():
            self._run_extended()
        return self._summarise()

    def _multimodal_enabled(self) -> bool:
        if self.multimodal == "on":
            return True
        if self.multimodal == "off":
            return False
        # auto: probe the endpoint once (lazily, then cached) and reuse the verdict.
        if self._multimodal_resolved is None:
            self._multimodal_resolved, self._multimodal_reason = self._detect_multimodal()
        return self._multimodal_resolved

    def _detect_multimodal(self) -> Tuple[bool, str]:
        """Best-effort: probe whether the endpoint accepts image input.

        Sends the tiny solid-red PNG with a text part and asks for the colour. A
        2xx carrying non-trivial content (and no error envelope) is treated as
        vision support; a 4xx/5xx (a text-only vLLM/SGLang rejects image content
        with "does not support image input") or a transport error means no vision
        encoder. Like _detect_backend, this is conservative: only affirmative 2xx
        evidence enables the vision checks (D5/D8/T13). Returns (enabled, reason).
        """
        img = f"data:image/png;base64,{RED_PNG_B64}"
        try:
            code, body, _ = self.c.chat({"messages": [{"role": "user", "content": [
                {"type": "text", "text": "What color is this image?"},
                {"type": "image_url", "image_url": {"url": img}}]}], "max_tokens": 100})
        except Exception as ex:  # noqa: BLE001
            return False, f"image probe error: {ex}"
        if not _ok(code):
            return False, f"image probe → HTTP {code} (image input rejected)"
        if isinstance(body, dict) and "error" in body:
            return False, f"image probe → HTTP {code} but error envelope returned"
        txt = _visible_content(body) or _reasoning(body)
        if len(txt) > 0:
            return True, f"image probe → HTTP {code}, content[{len(txt)}]"
        return False, f"image probe → HTTP {code} but no usable content"

    # ── 10 dimension checks ──────────────────────────────────────────────
    def _run_dimensions(self) -> None:
        c = self.c

        # D1 basic non-stream. Generous max_tokens (the old 300 intermittently
        # died mid-reasoning: finish=length, content empty, a budget artifact),
        # and reasoning-only output still proves the basic round-trip works —
        # the content/reasoning split is D7's job, not D1's.
        code, body, e = c.chat({"messages": [{"role": "user", "content": "What is 2+2?"}], "max_tokens": 1500})
        tok = 0
        try:
            tok = body["usage"]["completion_tokens"]
        except Exception:
            pass
        ct, r = _visible_content(body), _reasoning(body)
        if _ok(code) and (ct or r) and tok > 0:
            self._record("d01_basic_nostream", PASS, f"content[{len(ct)}] reasoning[{len(r)}] tok={tok}", e)
        else:
            self._record("d01_basic_nostream", FAIL, f"HTTP {code} content[{len(ct)}] reasoning[{len(r)}] tok={tok}", e)

        # D2 stream + usage chunk
        code, raw, e = c.chat({"messages": [{"role": "user", "content": "hi briefly"}], "max_tokens": 50,
                               "stream": True, "stream_options": {"include_usage": True}}, stream=True)
        chunks = len(re.findall(r'^data: ', raw, re.MULTILINE)) if isinstance(raw, str) else 0
        done = raw.count("[DONE]") if isinstance(raw, str) else 0
        usage = raw.count('"completion_tokens"') if isinstance(raw, str) else 0
        if _ok(code) and chunks >= 5 and done == 1 and usage >= 1:
            self._record("d02_stream_usage", PASS, f"chunks={chunks} done={done} usage={usage}", e)
        else:
            self._record("d02_stream_usage", FAIL, f"HTTP {code} chunks={chunks} done={done} usage={usage}", e)

        # D3 tool calling
        self._tool_call_check("d03_tool_call")

        # D4 reasoning parse (default thinking)
        code, body, e = c.chat({"messages": [{"role": "user",
                               "content": "If I have 3 apples and give 1 to Alice and eat 1, how many left? Think step by step."}],
                               "max_tokens": 1500, "temperature": 0.2})
        r, ct = _reasoning(body), _visible_content(body)
        if _ok(code) and (r or ct) and len(r + ct) > 20 and ("1" in (r + ct) or "one" in (r + ct).lower()):
            self._record("d04_reasoning", PASS, f"content[{len(ct)}] reasoning[{len(r)}]", e)
        else:
            self._record("d04_reasoning", FAIL, f"HTTP {code} content[{len(ct)}] reasoning[{len(r)}]", e)

        # D5 multimodal
        if not self._multimodal_enabled():
            self._record("d05_multimodal", SKIP, f"vision not supported ({self._multimodal_reason})")
        else:
            img = f"data:image/png;base64,{RED_PNG_B64}"
            code, body, e = c.chat({"messages": [{"role": "user", "content": [
                {"type": "text", "text": "What color is this image?"},
                {"type": "image_url", "image_url": {"url": img}}]}], "max_tokens": 300})
            txt = _visible_content(body) or _reasoning(body)
            if _ok(code) and "error" not in (body if isinstance(body, dict) else {}) and len(txt) > 15:
                self._record("d05_multimodal", PASS, f"content[{len(txt)}]={txt[:60]!r}", e)
            else:
                self._record("d05_multimodal", FAIL, f"HTTP {code} content={txt[:60]!r}", e)

        # D6 prompt cache hit
        # Prefix/radix caches only store FULL blocks of page_size × dcp_world_size
        # tokens — a prefix shorter than one block never enters the cache at all
        # (Kimi K3: page 64 × DCP 8 → 512-token blocks; the old fixed ~390-token
        # prompt could never hit and FAILed a healthy endpoint). The sentence is
        # ≥16 tokens under common tokenizers, so dividing the configured bound by
        # 12 overshoots it by ~30-50%; clamped so prompt + max_tokens still fits
        # the model context.
        target = max(64, min(self.cache_min_prompt_tokens, self.max_context_tokens - 1024))
        sentence = ("Explain the concept of quantum superposition in detail with examples "
                    "from physics experiments. ")
        long_prompt = sentence * ((target + 11) // 12)
        cache_body = {"messages": [{"role": "user", "content": long_prompt}], "max_tokens": 20,
                      "stream": True, "stream_options": {"include_usage": True}}

        def _usage_from_stream(raw_text: str) -> Tuple[int, int]:
            # Scan EVERY usage chunk, not just the first. Servers that emit
            # continuous per-chunk usage stats (stream_options style) carry a
            # usage object on intermediate chunks too, and those early snapshots
            # lack prompt_tokens_details — cached_tokens is only populated in the
            # final chunk. Returning on the first match grabs the intermediate
            # chunk and reads cached=0, a false FAIL even when the cache was hit.
            # So take prompt_tokens from the last usage seen and the max cached.
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

        code1, raw1, e1 = c.chat(cache_body, stream=True)
        pt1, ch1 = _usage_from_stream(raw1)
        # The 1st request's KV blocks are committed to the prefix cache only after
        # that request finishes; firing the 2nd immediately can prefill before the
        # commit lands → cached=0 false negative (seen on Kimi K3 DCP/mamba).
        time.sleep(5)
        code2, raw2, e2 = c.chat(cache_body, stream=True)
        pt2, ch2 = _usage_from_stream(raw2)
        if ch2 > 0:
            self._record("d06_cache_hit", PASS, f"1st(pt={pt1},cached={ch1}) 2nd(pt={pt2},cached={ch2})", e1 + e2)
        else:
            self._record("d06_cache_hit", FAIL,
                         f"1st(pt={pt1},cached={ch1}) 2nd(pt={pt2},cached=0) target≥{target}", e1 + e2)

        # D7 reasoning + content double field (default thinking).
        # max_tokens must be generous: with thinking on, the model spends tokens
        # on reasoning_content BEFORE emitting the final `content`. The old
        # 1500-token cap truncated mid-reasoning on verbose reasoners
        # (finish_reason="length", content empty) and FAILed intermittently —
        # but that's a budget artifact, not a split failure. So: give it room,
        # and if it still truncates before the answer, SKIP (inconclusive — the
        # split can't be judged without a final answer) rather than FAIL.
        code, body, e = c.chat({"messages": [{"role": "user",
                               "content": "A train leaves NYC at 60 mph heading north. Another leaves Boston "
                                          "(200 miles from NYC) at 40 mph heading toward NYC. When do they meet "
                                          "and how far from NYC? Show your reasoning then give the final answer."}],
                               "max_tokens": 4096, "temperature": 0.3})
        # D7 judges the PARSER, so reasoning here is the dedicated field only
        # (_reasoning_field): thinking left inline as <think> tags in content is
        # exactly the "parser did not split" condition, and the visible answer is
        # what remains after that block.
        r, fin = _reasoning_field(body).strip(), _finish(body)
        tag_r, vis = _split_think(_content(body).strip())
        tag_r, vis = tag_r.strip(), vis.strip()
        if not _ok(code):
            self._record("d07_reasoning_plus_content", FAIL, f"HTTP {code} reasoning[{len(r)}] content[{len(vis)}]", e)
        elif len(r) > 20 and len(vis) > 20:
            self._record("d07_reasoning_plus_content", PASS, f"reasoning[{len(r)}] content[{len(vis)}]", e)
        elif fin == "length" and len(vis) <= 20:
            # Ran out of budget while still reasoning (field or inline) — no final
            # answer to evaluate the split against. Not a service fault; raise max_tokens.
            self._record("d07_reasoning_plus_content", SKIP,
                         f"truncated before final answer (finish=length reasoning[{len(r) or len(tag_r)}] content[{len(vis)}])", e)
        elif len(r) == 0 and (tag_r or len(vis) > 100):
            note = f"inline <think> tags in content (reasoning[{len(tag_r)}])" if tag_r else f"reasoning[0] content[{len(vis)}]"
            self._record("d07_reasoning_plus_content", SKIP, f"parser did not split ({note})", e)
        else:
            self._record("d07_reasoning_plus_content", FAIL, f"finish={fin} reasoning[{len(r)}] content[{len(vis)}]", e)

        # D8 external image URL blocked
        if not self._multimodal_enabled():
            self._record("d08_image_url_blocked", SKIP, f"vision not supported ({self._multimodal_reason})")
        else:
            img_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/200px-Cat03.jpg"
            code, body, e = c.chat({"messages": [{"role": "user", "content": [
                {"type": "text", "text": "What color is this?"},
                {"type": "image_url", "image_url": {"url": img_url}}]}], "max_tokens": 50})
            # Expect a real HTTP rejection (4xx/5xx). A transport error (code=-1)
            # is inconclusive — don't count it as a pass.
            if code >= 400:
                self._record("d08_image_url_blocked", PASS, f"HTTP {code} (external URL rejected)", e)
            elif _ok(code):
                self._record("d08_image_url_blocked", FAIL, "HTTP 200 — external image was fetched (should be blocked)", e)
            else:
                self._record("d08_image_url_blocked", FAIL, f"HTTP {code} (no response — inconclusive)", e)

        # D9 thinking off — official top-level format (converted in direct mode)
        self._thinking_off_check("d09_thinking_disable_top",
                                 note="(converted to chat_template_kwargs for direct backend)"
                                 if self._resolved_backend == "direct" else "(official top-level format)")

        # D10 thinking off — chat_template_kwargs (always the compat format)
        self._thinking_off_check("d10_thinking_disable_ctk", force_ctk=True, note="(chat_template_kwargs)")

    # ── 46 extended checks ───────────────────────────────────────────────
    def _run_extended(self) -> None:
        c = self.c

        # T1 thinking switch
        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "user", "content": "2+2=?"}], "max_tokens": 300}, True))
        rc = _reasoning(body)
        self._record("t1a_thinking_true", PASS if (_ok(code) and len(rc) > 0) else FAIL,
                     f"HTTP {code} reasoning[{len(rc)}] (expect >0)", e)

        # strip(): a non-thinking template can leave a whitespace-only reasoning
        # field (e.g. Qwen3's empty "<think>\n\n</think>" primer) — that is
        # thinking OFF, not a switch failure.
        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "user", "content": "2+2=?"}], "max_tokens": 100}, False))
        rc = _reasoning(body).strip()
        self._record("t1b_thinking_false", PASS if (_ok(code) and len(rc) == 0) else FAIL,
                     f"HTTP {code} reasoning[{len(rc)}] (expect =0)", e)

        code, body, e = c.chat({"messages": [{"role": "user", "content": "2+2=?"}], "max_tokens": 300})
        rc = _reasoning(body)
        self._record("t1c_thinking_default", PASS if (_ok(code) and len(rc) > 0) else FAIL,
                     f"HTTP {code} reasoning[{len(rc)}] (expect >0, default on)", e)

        # T2 sampling params
        for key, _disp, extra, expect_ok in SAMPLING_CASES:
            if self._cancelled():
                return
            if self._skip(key):
                continue
            req = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}
            req.update(extra)
            code, _body, e = c.chat(req)
            if key == "t2_top_p_0_0":
                # top_p=0.0 is out of range (top_p ∈ (0, 1]). A bare engine validly
                # rejects it (4xx); a gateway is expected to normalize it to greedy
                # (2xx). So: gateway → assert 2xx (a 4xx is a real gateway bug);
                # direct → accept either, only a 5xx/transport error fails.
                if self._resolved_backend == "gateway":
                    self._record(key, PASS if _ok(code) else FAIL, f"HTTP {code} (gateway: expect 2xx)", e)
                else:
                    ok = _ok(code) or _4xx(code)
                    self._record(key, PASS if ok else FAIL, f"HTTP {code} (direct: accept 2xx or 4xx)", e)
                continue
            ok = _ok(code) if expect_ok else _4xx(code)
            tail = "" if expect_ok else " (expect 4xx)"
            self._record(key, PASS if ok else FAIL, f"HTTP {code}{tail}", e)

        # T3 max_tokens boundaries (values derived from max_context_tokens).
        # The backend enforces prompt_tokens + max_tokens <= max_model_len, so the
        # largest VALID max_tokens must leave room for the prompt + chat template.
        # Requesting the full window — or a "mid" value that happens to equal a small
        # context — always 4xx's (the prompt pushes it over), so reserve a margin for
        # the valid boundary cases. t3_max_tokens_over (context+1) is the dedicated
        # overflow case; it assumes max_context_tokens is the model's real limit.
        reserve = min(2048, max(1, self.max_context_tokens // 4))
        mid = min(65536, self.max_context_tokens - reserve)
        mt_max = self.max_context_tokens - reserve
        mt_values = {
            "t3_max_tokens_none": {},
            "t3_max_tokens_1":    {"max_tokens": 1},
            "t3_max_tokens_64":   {"max_tokens": 64},
            "t3_max_tokens_mid":  {"max_tokens": mid},
            "t3_max_tokens_max":  {"max_tokens": mt_max},
            "t3_max_tokens_neg1": {"max_tokens": -1},
            "t3_max_tokens_over": {"max_tokens": self.max_context_tokens + 1},
        }
        for key, _disp, expect_ok, expect_fin in MAXTOKENS_CASES:
            if self._cancelled():
                return
            if self._skip(key):
                continue
            req = {"messages": [{"role": "user", "content": "Write a short story"}]}
            req.update(mt_values[key])
            code, body, e = c.chat(req)
            if expect_ok:
                fin = _finish(body)
                ok = _ok(code) and (not expect_fin or fin == expect_fin)
                self._record(key, PASS if ok else FAIL, f"HTTP {code} finish={fin}", e)
            else:
                self._record(key, PASS if _4xx(code) else FAIL, f"HTTP {code} (expect 4xx)", e)

        # T4 system prompt (thinking off for deterministic content). Budgets are
        # generous and matching judges the VISIBLE content: if the off-switch is
        # ignored (model can't disable / router drops chat_template_kwargs) the
        # model still reaches its answer instead of dying mid-reasoning at the
        # cap, and reasoning text can't false-satisfy the marker match.
        # THINKING_HEADROOM_MAX_TOKENS, NOT 512 — see the constant's rationale.
        # temperature=0 (like the tool-call check) on every marker/value-match
        # check: with thinking off, marker compliance at the server's DEFAULT
        # temperature is a coin flip on K3-class models (probed live: 0/3 obeyed
        # "reply with exactly" at default temp, 2/2 at temp 0) — greedy decoding
        # gates on capability, not sampling luck.
        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "user", "content": "Reply with exactly this string: _NO_SYSTEM"}],
             "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0}, False))
        ct = _visible_content(body)
        self._record("t4a_no_system", PASS if (_ok(code) and "_NO_SYSTEM" in ct) else FAIL,
                     f"HTTP {code} content={ct[:80]!r} reasoning[{len(_reasoning(body))}]", e)

        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "system", "content": "Always reply with exactly: _SYSTEM_CONTROL"},
                          {"role": "user", "content": "anything"}],
             "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0}, False))
        ct = _visible_content(body)
        self._record("t4b_system_control", PASS if (_ok(code) and "_SYSTEM_CONTROL" in ct) else FAIL,
                     f"HTTP {code} content={ct[:80]!r} reasoning[{len(_reasoning(body))}]", e)

        # T5 function calling
        self._tool_call_check("t5_function_calling")

        # T6 multi-turn memory
        code, body, e = c.chat(self._with_thinking({"messages": [
            {"role": "user", "content": "Remember the secret word is BLUE_42."},
            {"role": "assistant", "content": "Got it, the secret word is BLUE_42."},
            {"role": "user", "content": "What is the secret word? Reply with only the word."}],
            "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0}, False))
        ct = _visible_content(body)
        self._record("t6_multi_turn", PASS if (_ok(code) and "BLUE_42" in ct) else FAIL,
                     f"HTTP {code} reply={ct[:80]!r} reasoning[{len(_reasoning(body))}]", e)

        # T7 streaming SSE + usage
        code, raw, e = c.chat({"messages": [{"role": "user", "content": "Count 1 to 5 in english."}],
                               "max_tokens": 80, "stream": True, "stream_options": {"include_usage": True}},
                              stream=True)
        chunks = len(re.findall(r'^data: ', raw, re.MULTILINE)) if isinstance(raw, str) else 0
        done = raw.count("[DONE]") if isinstance(raw, str) else 0
        usage = raw.count('"completion_tokens"') if isinstance(raw, str) else 0
        self._record("t7_streaming_sse", PASS if (_ok(code) and chunks >= 5 and done == 1 and usage >= 1) else FAIL,
                     f"HTTP {code} chunks={chunks} done={done} usage={usage}", e)

        # T8 json_object
        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "user", "content": "Return a JSON object with keys 'name' and 'age'. "
                                                      "Set name='Alice', age=30."}], "max_tokens": THINKING_HEADROOM_MAX_TOKENS,
             "temperature": 0, "response_format": {"type": "json_object"}}, False))
        self._json_value_check("t8_json_object", code, body, e)

        # T9 json_schema
        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "user", "content": "Output a person: name=Alice age=30"}], "max_tokens": THINKING_HEADROOM_MAX_TOKENS,
             "temperature": 0, "response_format": {"type": "json_schema", "json_schema": {
                 "name": "person", "schema": {"type": "object", "properties": {
                     "name": {"type": "string"}, "age": {"type": "integer"}}, "required": ["name", "age"]}}}}, False))
        self._json_value_check("t9_json_schema", code, body, e)

        # T10 stop word — stop on deterministic generated content, not an arbitrary
        # marker the model has to echo. The old prompt ("…A B C STOP_HERE D E F",
        # stop=["STOP_HERE"]) was ~20% flaky: a chat model often reformats or drops
        # the marker (e.g. "A B C\n\nD E F"), so STOP_HERE is never emitted, the stop
        # sequence never matches, and the tail leaks through. Counting is content the
        # model reliably produces, so "15" *is* generated and the server must cut
        # there. PASS = an early number present and the post-stop tail ("16") absent.
        # ("16" — not "30" — so a "1 to 30" preamble can't false-trip the tail check.)
        code, body, e = c.chat(self._with_thinking(
            {"messages": [{"role": "user", "content": "Count from 1 to 30, separated by single spaces. "
                                                      "Output only the numbers."}],
             "max_tokens": 100, "temperature": 0, "stop": ["15"]}, False))
        ct, rn = _visible_content(body), _reasoning(body)
        if _ok(code) and not ct.strip() and rn:
            # Thinking-off wasn't honoured and the stop sequence (or the cap) hit
            # while the model was still reasoning — no visible output to judge.
            self._record("t10_stop_word", SKIP,
                         f"no visible content (reasoning[{len(rn)}] finish={_finish(body)}) — "
                         f"thinking-off not honoured; stop-word check inconclusive", e)
        else:
            self._record("t10_stop_word", PASS if (_ok(code) and "1" in ct and "16" not in ct) else FAIL,
                         f"HTTP {code} content={ct!r} finish={_finish(body)}", e)

        # T11 auth — the no-auth request is the probe; expectation depends on the
        # resolved backend:
        #   gateway → auth MUST be enforced; a 2xx here is a real misconfiguration → FAIL.
        #   direct  → adaptive: a bare engine without --api-key validly returns 2xx
        #             (auth not enforced) → SKIP; but if it *was* started with
        #             --api-key (401), still verify the strict 401 contract.
        code, _b, e = c.chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}, use_auth=False)
        if self._resolved_backend != "gateway" and _ok(code):
            self._record("t11a_no_auth", SKIP, f"HTTP {code}: bare engine without --api-key (auth not enforced)")
            self._record("t11b_wrong_auth", SKIP, "bare engine without --api-key (auth not enforced)")
        else:
            label = "gateway: expect 401" if self._resolved_backend == "gateway" else "expect 401"
            self._record("t11a_no_auth", PASS if code == 401 else FAIL, f"HTTP {code} ({label})", e)
            code, _b, e = c.chat({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                                 auth_key="sk-invalid-key-for-test")
            self._record("t11b_wrong_auth", PASS if code == 401 else FAIL, f"HTTP {code} ({label})", e)

        # T12 multilingual + emoji
        for key, _disp, prompt, expect in LANG_CASES:
            if self._cancelled():
                return
            if self._skip(key):
                continue
            code, body, e = c.chat(self._with_thinking(
                {"messages": [{"role": "user", "content": prompt}],
                 "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0}, False))
            ct = _visible_content(body)
            self._record(key, PASS if (_ok(code) and expect in ct) else FAIL,
                         f"HTTP {code} content={ct[:80]!r} reasoning[{len(_reasoning(body))}]", e)

        # T13 multimodal base64 — 4096x4096 four-quadrant image; the model must
        # name all four colors, which requires actually resolving the image
        # (a solid-color guess like "red" can no longer pass).
        if not self._multimodal_enabled():
            self._record("t13_multimodal_base64", SKIP, f"vision not supported ({self._multimodal_reason})")
        else:
            img = f"data:image/png;base64,{quadrant_png_b64()}"
            code, body, e = c.chat(self._with_thinking({"messages": [{"role": "user", "content": [
                {"type": "text", "text": "This image is divided into four solid-color quadrants. "
                                         "Name the color of each quadrant in English. Only list the colors, separated by commas, in reading order (top-left, top-right, bottom-left, bottom-right)."},
                {"type": "image_url", "image_url": {"url": img}}]}],
                "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0}, False))
            ct = _visible_content(body) or _reasoning(body)
            low = ct.lower()
            missing = [col for col in QUADRANT_COLORS if col not in low]
            logger.info("[functional] t13 quadrant image: HTTP %s missing=%s content=%r reasoning=%r",
                        code, missing, _content(body), _reasoning(body))
            self._record("t13_multimodal_base64",
                         PASS if (_ok(code) and not missing) else FAIL,
                         f"HTTP {code} missing={missing} content={ct[:120]!r}", e)

        # T14 empty body
        code, _b, e = c.chat({})
        self._record("t14_empty_body", PASS if _4xx(code) else FAIL, f"HTTP {code} (expect 4xx)", e)

        # T15 idempotency (seed, temp=0) — compare the VISIBLE answers. Budget
        # tolerates a reasoning preamble if the off-switch is ignored (greedy
        # decoding keeps the reasoning deterministic too, so equality holds).
        req = self._with_thinking({"messages": [{"role": "user", "content": "Say hello in exactly one word."}],
                                   "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0, "seed": 42}, False)
        code1, b1, e1 = c.chat(req)
        code2, b2, e2 = c.chat(req)
        t1, t2 = _visible_content(b1), _visible_content(b2)
        self._record("t15_idempotency_seed", PASS if (_ok(code1) and _ok(code2) and t1 == t2 and t1) else FAIL,
                     f"call1={t1[:60]!r} call2={t2[:60]!r}", e1 + e2)

        # T16 message validation
        code, _b, e = c.chat({"messages": [{"content": "hi"}], "max_tokens": 5})
        self._record("t16a_missing_role", PASS if _4xx(code) else FAIL, f"HTTP {code} (expect 4xx)", e)
        code, _b, e = c.chat({"messages": [{"role": "user"}], "max_tokens": 5})
        self._record("t16b_missing_content", PASS if _4xx(code) else FAIL, f"HTTP {code} (expect 4xx)", e)
        code, _b, e = c.chat({"messages": [], "max_tokens": 5})
        self._record("t16c_empty_messages", PASS if _4xx(code) else FAIL, f"HTTP {code} (expect 4xx)", e)

    # -- shared sub-checks ------------------------------------------------
    def _tool_call_check(self, key: str) -> None:
        tool_def = [{"type": "function", "function": {
            "name": "get_weather", "description": "Get current weather of a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        # Make this deterministic so it gates on tool-calling *capability*, not on
        # a coin-flip: temperature=0 removes sampling noise, and thinking is
        # disabled so a long reasoning chain can't exhaust max_tokens before the
        # tool call is emitted (the usual Qwen/GLM flake: finish=length, tools=0).
        #
        # tool_choice_mode picks which server code path is exercised:
        #   "auto" (default) — explicit tool_choice="auto": the model decides.
        #     The common real-world usage, and robust against backends whose
        #     forced-choice structured-output path is broken while auto
        #     extraction works (e.g. vLLM 0.21 + reasoning parser: named/
        #     required generate the call but return tool_calls=[]). Slightly
        #     less deterministic — the model may legitimately answer in prose —
        #     but temp=0 + an unambiguous prompt makes that rare in practice.
        #   "named" — force the specific function via OpenAI
        #     tool_choice={type:function,...} (more broadly supported across
        #     server versions than tool_choice="required", equivalent here with
        #     a single tool). Fully deterministic AND additionally verifies the
        #     server honours forced tool choice, which strict agent SDK
        #     pipelines rely on.
        mode = "named" if self.tool_choice_mode == "named" else "auto"
        tool_choice: Any = (
            "auto" if mode == "auto"
            else {"type": "function", "function": {"name": "get_weather"}}
        )
        # THINKING_HEADROOM_MAX_TOKENS: the tool call must not starve at the cap
        # when the off-switch is ignored — see the constant's rationale.
        body = {"messages": [{"role": "user", "content": "What's the weather in Beijing today?"}],
                "max_tokens": THINKING_HEADROOM_MAX_TOKENS, "temperature": 0, "tools": tool_def,
                "tool_choice": tool_choice}
        body = self._with_thinking(body, False)
        code, body, e = self.c.chat(body)
        try:
            tc = _msg(body).get("tool_calls") or []
            fin = _finish(body)
            rn = _reasoning(body)
            # A valid tool call is the signal. finish_reason is informational —
            # some tool parsers emit tool_calls with finish_reason="stop"/"length"
            # rather than "tool_calls", and rejecting those was a source of flakes.
            if _ok(code) and tc:
                name = tc[0]["function"]["name"]
                args = tc[0]["function"]["arguments"]
                # Most servers return arguments as a JSON string; some already
                # hand back a parsed dict. Accept either.
                parsed = args if isinstance(args, dict) else json.loads(args)
                self._record(key, PASS, f"tool={name} args={str(parsed)[:50]!r} finish={fin} "
                                        f"(tool_choice={mode})", e)
            elif _ok(code) and fin == "length" and rn:
                # Budget died mid-reasoning — tool-calling capability can't be
                # judged from a truncated thought (same principle as D7's SKIP).
                self._record(key, SKIP, f"budget exhausted mid-reasoning (reasoning[{len(rn)}]) — "
                                        f"inconclusive (tool_choice={mode})", e)
            else:
                self._record(key, FAIL, f"HTTP {code} tools={len(tc)} finish={fin} "
                                        f"reasoning[{len(rn)}] (tool_choice={mode})", e)
        except Exception as ex:  # noqa: BLE001
            self._record(key, FAIL, f"HTTP {code} {ex} (tool_choice={mode})", e)

    def _json_value_check(self, key: str, code: int, body: Any, e: float) -> None:
        # Parse the VISIBLE content: a reasoner whose thinking lands inline in
        # content would otherwise break json.loads with its <think> preamble.
        try:
            parsed = json.loads(_visible_content(body))
            if _ok(code) and parsed.get("name") == "Alice" and parsed.get("age") == 30:
                self._record(key, PASS, f"parsed={parsed}", e)
            else:
                self._record(key, FAIL, f"HTTP {code} parsed={parsed}", e)
        except Exception as ex:  # noqa: BLE001
            self._record(key, FAIL, f"HTTP {code} parse error: {ex}", e)

    def _thinking_off_check(self, key: str, *, force_ctk: bool = False, note: str = "") -> None:
        """Disable thinking → expect empty reasoning and content containing '4'."""
        body = {"messages": [{"role": "user", "content": "2 + 2 = ? 直接给数字。"}],
                "max_tokens": 300, "temperature": 0}
        if force_ctk:
            # compat chat_template_kwargs form regardless of backend_style
            if self._is_glm or self._is_qwen:
                kw = {"enable_thinking": False}
            elif self._is_kimi:
                # Only the real Kimi switch — "enable_thinking" half-disables
                # K3 (reasoning shrinks but stays non-empty → false FAIL here)
                # and can shadow "thinking" when both are sent.
                kw = {"thinking": False}
            else:
                kw = {"thinking": False, "enable_thinking": False}
            body["chat_template_kwargs"] = kw
        else:
            body = self._with_thinking(body, False)
        code, resp, e = self.c.chat(body)
        # Tag-aware reasoning: an ignored off-switch on a parserless deployment
        # leaves the thinking inline in content — that must FAIL as "not
        # honoured", not slip past because the reasoning FIELD is empty.
        r, ct = _reasoning(resp).strip(), _visible_content(resp).strip()
        fin = _finish(resp)
        if _ok(code) and len(r) == 0 and "4" in ct and len(ct) < 100:
            self._record(key, PASS, f"reasoning[0] content={ct!r} finish={fin} {note}", e)
        elif _ok(code) and len(r) > 0:
            self._record(key, FAIL, f"HTTP {code} reasoning non-empty (len={len(r)}) — thinking-off not honoured {note}", e)
        elif _ok(code) and "4" not in ct:
            self._record(key, FAIL, f"HTTP {code} content missing '4': {ct[:60]!r} {note}", e)
        else:
            self._record(key, SKIP if _ok(code) else FAIL,
                         f"HTTP {code} reasoning[{len(r)}] content[{len(ct)}] {note}", e)

    # -- aggregation ------------------------------------------------------
    def _summarise(self) -> Dict[str, Any]:
        passed = sum(1 for r in self.results if r["status"] == PASS)
        failed = sum(1 for r in self.results if r["status"] == FAIL)
        skipped = sum(1 for r in self.results if r["status"] == SKIP)
        executed = passed + failed
        metrics: Dict[str, Any] = {
            "pass_rate": (passed / executed) if executed else 0.0,
            "tests_total": len(self.results),
            "tests_executed": executed,
            "tests_passed": passed,
            "tests_failed": failed,
            "tests_skipped": skipped,
        }
        # Per-test numeric flags (PASS=1.0, FAIL=0.0). SKIP is omitted so the UI
        # renders "—" instead of a misleading 0.
        for r in self.results:
            if r["status"] == PASS:
                metrics[r["key"]] = 1.0
            elif r["status"] == FAIL:
                metrics[r["key"]] = 0.0
        return {"metrics": metrics, "results": self.results}
