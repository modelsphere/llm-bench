"""
Production traffic replay

Purpose : Replay a sample of real collected requests under concurrent load to detect:
            - Service errors
            - TTFT / total generation time redline violations
            - Abnormal output lengths (truncation, post-finish tokens, repetitive endings)
            - Token throughput

Duration: minutes on the example set; hours on a full production capture,
          which is why a benchmark runs this module last.
Dataset : bench/examples/replay-smoke.jsonl (default — 20 requests that prove
          the pipeline, not a measurement). Point it at captured traffic via
          the module's dataset_path, a rolling profile, or REPLAY_DATASET_PATH.  Each line must be a MatchedRequest
          record as produced by bench/replay_test/log_replay_tool.py extract.
          Use REPLAY_SAMPLE_SIZE to limit the number of requests replayed.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
import zlib
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

from bench.replay_test.jsonl_io import count_lines
from bench.replay_test.log_replay_tool import (
    MatchedRequest,
    build_replay_body,
    is_retryable_http_status,
    load_extract_jsonl,
    percentile,
    request_needs_system_normalization,
)
from bench.result import TestResult
from bench.tests.base import BaseTest
# Judge helpers shared with the hallucination probe: send_chat accumulates
# content/reasoning from a streamed (or plain-JSON) chat response, and
# _disable_thinking_fields turns thinking off via chat_template_kwargs only
# (never a non-standard top-level param a strict server would 400 on).
from bench.tests.functional.fixed_request_probe import (
    send_chat,
    _disable_thinking_fields,
)
from utils.api import join_endpoint
from utils.logger import logger

# ---------------------------------------------------------------------------
# Configuration via env vars
# ---------------------------------------------------------------------------

_DEFAULT_DATASET = str(
    Path(__file__).parent.parent.parent.parent
    / "bench"
    / "examples"
    / "replay-smoke.jsonl"
)
# --- Default values, read from env ONCE at module import ---------------------
#
# These are *fallback defaults* for ReplayTest's constructor arguments. They
# matter when ReplayTest is used directly from a script, where env vars are the
# natural override knob and the process is short-lived.
#
# Inside the platform, do NOT rely on these — pass the real values as
# constructor arguments. Long-lived dramatiq worker processes import this
# module once and reuse the imported state across every submission, so
# changing os.environ between submissions has no effect on these module-level
# names. That's a subtle source of "I changed max_samples but nothing happened"
# bugs; the per-instance self.* attributes set by __init__ are the truth.
REPLAY_DATASET_PATH = os.getenv("REPLAY_DATASET_PATH", _DEFAULT_DATASET)
REPLAY_CONCURRENCY = int(os.getenv("REPLAY_CONCURRENCY", "16"))
REPLAY_SAMPLE_SIZE = int(os.getenv("REPLAY_SAMPLE_SIZE", "0"))
REPLAY_REQUEST_TIMEOUT = float(os.getenv("REPLAY_REQUEST_TIMEOUT", "600.0"))
REPLAY_MAX_RETRIES = int(os.getenv("REPLAY_MAX_RETRIES", "3"))
# Cap on how many tokens the model may generate per replayed request. The
# recorded body is replayed verbatim, so a request whose original max_tokens was
# huge or absent can stream for a very long time (runaway / non-terminating
# generation), pinning a worker thread — note `request_timeout` is a per-read
# socket timeout, NOT a total-duration cap, so it does NOT stop a response that
# keeps emitting tokens. This injects/lowers max_tokens in the request (the
# server then stops at finish_reason="length"); as a backstop for servers that
# overrun it, the client stops reading after ~2x the cap. 0 = no cap (default).
REPLAY_MAX_GENERATION_TOKENS = int(os.getenv("REPLAY_MAX_GENERATION_TOKENS", "0"))
# Absolute backstop on how many streamed SSE events (~1 per token) a single
# response may buffer in memory, INDEPENDENT of max_generation_tokens. The
# per-request read accumulates every event into a list before processing, so a
# non-terminating / runaway generation with no max_tokens cap (the default,
# REPLAY_MAX_GENERATION_TOKENS=0) would otherwise grow that list without bound
# and OOM the worker — there is no server-side stop. When the buffer hits this
# ceiling the client stops reading (closing the stream aborts the request
# server-side) and marks the request generation_capped. Sized well above any
# legitimate response; lower it to tighten the memory ceiling. 0 disables it
# (unbounded — not recommended).
REPLAY_MAX_SSE_EVENTS = int(os.getenv("REPLAY_MAX_SSE_EVENTS", "131072"))  # ~128K tokens
# Streaming dispatch window: how many requests may be in flight (submitted to the
# pool but not yet collected) as a multiple of `concurrency`. Records are pulled
# from disk lazily and only this many are resident at once, so peak memory is
# O(concurrency) instead of O(dataset) — a multi-GB replay file never has to fit
# in RAM. A small multiple keeps the pool saturated without queueing the dataset.
REPLAY_INFLIGHT_MULTIPLIER = int(os.getenv("REPLAY_INFLIGHT_MULTIPLIER", "4"))
# Records the tool-support preflight scans (lazily, from the front). Bounded so
# the preflight doesn't pull the whole dataset into RAM just to count.
REPLAY_PREFLIGHT_SCAN = int(os.getenv("REPLAY_PREFLIGHT_SCAN", "1000"))
# Hard time budget for the whole replay batch. When elapsed exceeds this,
# stop dispatching, aggregate what we have, and return normally — instead of
# either running indefinitely or being killed by the heartbeat reaper.
# 0 means no cap.
REPLAY_MAX_SECONDS = float(os.getenv("REPLAY_MAX_SECONDS", "18000"))

# Redline thresholds
REPLAY_TTFT_P99_REDLINE_MS = float(os.getenv("REPLAY_TTFT_P99_REDLINE_MS", "10000.0"))
REPLAY_TOTAL_TIME_P99_REDLINE_MS = float(os.getenv("REPLAY_TOTAL_TIME_P99_REDLINE_MS", "120000.0"))
REPLAY_UPTIME_FLOOR = float(os.getenv("REPLAY_UPTIME_FLOOR", "0.95"))

# TTFT is bucketed by input length (prompt_tokens) so it can be checked against
# the per-input-length TTFT service level, which sets different TTFT SLAs per input
# size. Each tuple is (metric_label, human_label, lo_inclusive, hi_exclusive) in
# tokens; "K" = 1024. The open-ended ge_256k bucket catches anything past the
# table so oversized prompts aren't silently folded into 128K–256K. Per bucket
# we emit ttft_<label>_{p90,p50,avg}_ms plus a _count (percentiles off a handful
# of requests are noisy — read the count before trusting them).
TTFT_INPUT_BUCKETS = [
    ("lt_6k",     "<6K",       0,            6 * 1024),
    ("6k_16k",    "6K-16K",    6 * 1024,     16 * 1024),
    ("16k_32k",   "16K-32K",   16 * 1024,    32 * 1024),
    ("32k_64k",   "32K-64K",   32 * 1024,    64 * 1024),
    ("64k_128k",  "64K-128K",  64 * 1024,    128 * 1024),
    ("128k_256k", "128K-256K", 128 * 1024,   256 * 1024),
    ("ge_256k",   "≥256K",     256 * 1024,   float("inf")),
]


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Debug-only: dump each request + raw response to a JSONL file under output_dir
# so it can be inspected with `kubectl exec` in the worker pod. OFF by default —
# a full ~10h run can produce a lot of data, so this is meant for targeted
# debugging, not normal operation. Set REPLAY_SAVE_RESPONSES=1 on the worker and
# restart it (these are read once at import, like the rest of the REPLAY_* knobs).
# REPLAY_SAVE_RESPONSES_MAX_BYTES caps the raw response text stored per request
# (0 = unlimited).
REPLAY_SAVE_RESPONSES = _env_flag("REPLAY_SAVE_RESPONSES", False)
REPLAY_SAVE_RESPONSES_MAX_BYTES = int(os.getenv("REPLAY_SAVE_RESPONSES_MAX_BYTES", "65536"))

# ---------------------------------------------------------------------------
# Opt-in LLM answer-quality judge (admin-configured via replay params)
# ---------------------------------------------------------------------------
#
# When judge_enable is on, a random judge_sample_rate fraction of replayed
# requests additionally RETAIN their response text (normally discarded — the
# perf path only counts tokens). As each sampled request completes it is
# mechanically classified into a disposition bucket; "answered" ones are
# immediately handed to a separate small judge thread pool, where a judge LLM
# grades quality (good/acceptable/poor) and hallucination (none/suspected/
# clear) — PIPELINED with the perf batch, so a slow (more powerful) judge
# model overlaps the run instead of serializing behind it. Judge workers are
# I/O-bound and parse only tiny verdict payloads, so the client-side jitter
# they add to TTFT is negligible. After the batch, the drain step waits for
# outstanding verdicts and tallies the metrics; the wall-clock bracket used
# for TPM covers the perf batch only.

# Two-axis rubric (quality + hallucination), ported from the offline bodylog
# judge pipeline. The judge must answer with a single-line JSON verdict.
_JUDGE_RUBRIC = """你是 LLM 回答质量审核员。给你一条真实生产请求的【输入】【回答】【思考】。
只输出**一行 JSON**(不要任何多余文字/解释/markdown):
{"task":"..","quality":"good|acceptable|poor","halluc":"none|suspected|clear","reason":"<=30字中文"}

- task: 用 2~6 个字概括任务类型(如 事实核查 / 分类抽取 / RAG问答 / agentic工具 / 内容生成 / 开放问答 / 其它)
- quality:
    good       = 切题 + 格式规范 + 连贯
    acceptable = 小瑕疵 / 被截断但主体完整
    poor       = 答非所问 / 空 / 自相矛盾 / 格式崩(</think> 泄漏、该输出 JSON 却没输出)
- halluc(重点,针对给定材料的核查/抽取/RAG 类任务):
    none      = grounded / 如实转述材料或工具结果
    suspected = 精确数字无法独立确证但声称来自材料
    clear     = 编造材料里没有的事实 / 引用 / ID(RAG、聚类类任务超出给定材料很常见)
"""

_JUDGE_QUALITY_VALUES = frozenset({"good", "acceptable", "poor"})
_JUDGE_HALLUC_VALUES = frozenset({"none", "suspected", "clear"})
# Flat JSON object anchored on the "quality" key (the verdict JSON is flat, so
# a flat-brace regex is exact — same keyed pattern as the hallucination
# probe's _JSON_OBJ_RE, which anchors on its own verdict key).
_JUDGE_OBJ_RE = re.compile(r"\{[^{}]*\"quality\"[^{}]*\}", re.DOTALL)
# Loose fallback when the judge answered in prose instead of clean JSON.
_JUDGE_QUALITY_RE = re.compile(r"\"?quality\"?\s*[:=]\s*\"?(good|acceptable|poor)", re.IGNORECASE)
_JUDGE_HALLUC_RE = re.compile(r"\"?halluc\w*\"?\s*[:=]\s*\"?(none|suspected|clear)", re.IGNORECASE)

# Bounds on the text shown to the judge, mirroring the offline pipeline: enough
# to grade, small enough that one verdict stays a single cheap call.
_JUDGE_INPUT_CAP = 6000
_JUDGE_INPUT_PER_MSG_CAP = 3500
_JUDGE_ANSWER_CAP = 5000
_JUDGE_REASONING_CAP = 2000

# Every judge metric key, in emit order. Emitted as None when the judge is off
# so the default judge redlines are skipped (check_redlines skips None — the
# same mechanism that skips empty TTFT buckets).
JUDGE_METRIC_KEYS = (
    "judge_sampled_count",
    "judge_answered_count",
    "judge_toolcall_count",
    "judge_length_count",
    "judge_disconnect_count",
    "judge_error_count",
    "judge_kept_count",
    "judge_good_acc_rate",
    "judge_poor_rate",
    "judge_halluc_clear_rate",
    "judge_halluc_suspected_count",
    "judge_failures",
)

# Dispositions excluded from the quality denominator: the answer never really
# arrived, so grading its text would be meaningless.
JUDGE_EXCLUDED_DISPOSITIONS = frozenset({"error", "length", "disconnect"})


def judge_disposition(rec: Dict[str, Any]) -> str:
    """Mechanically classify one retained replay result for the judge phase.

    Keys off replay's own per-request telemetry (success/status_code/
    finish_reason/stop_reason/response_finished/generation_capped) rather than
    the offline pipeline's bodylog heuristics, which mis-map here:
      - a successful NON-STREAMING reply has real content regardless of
        finish_reason, so "finish_reason is None => disconnect" is wrong; the
        right unfinished signal is replay's response_finished flag;
      - "status in (400, 500)" misses 401/403/404/429/5xx and every transport
        failure; replay's success flag already covers all of those;
      - length-truncation also shows up as stop_reason="max_tokens" (Anthropic)
        or generation_capped (client stopped a runaway server).
    """
    if not rec.get("success"):
        return "error"
    status = rec.get("status_code")
    if isinstance(status, int) and status >= 400:
        return "error"
    if (rec.get("finish_reason") == "length"
            or rec.get("stop_reason") == "max_tokens"
            or rec.get("generation_capped")):
        return "length"
    if (rec.get("finish_reason") in ("tool_calls", "function_call")
            or rec.get("stop_reason") == "tool_use"):
        return "toolcall"
    if not rec.get("response_finished"):
        # Stream ended with no finish marker — truncated/aborted mid-response.
        return "disconnect"
    return "answered"


def _parse_replay_verdict(text: str) -> Optional[Dict[str, str]]:
    """Extract {task, quality, halluc, reason} from a judge reply.

    Tries every flat JSON object containing "quality", newest-last (reasoning
    models sometimes draft a verdict then restate it); falls back to loose
    key:value regexes on the raw text. Returns None when no valid quality
    value is found (counted as a judge failure).
    """
    if not text:
        return None
    for m in reversed(_JUDGE_OBJ_RE.findall(text)):
        try:
            obj = json.loads(m)
        except json.JSONDecodeError:
            continue
        quality = str(obj.get("quality", "")).strip().lower()
        if quality in _JUDGE_QUALITY_VALUES:
            halluc = str(obj.get("halluc", "none")).strip().lower()
            return {
                "task": str(obj.get("task", "?"))[:40],
                "quality": quality,
                "halluc": halluc if halluc in _JUDGE_HALLUC_VALUES else "none",
                "reason": str(obj.get("reason", ""))[:60],
            }
    qm = _JUDGE_QUALITY_RE.search(text)
    if qm:
        hm = _JUDGE_HALLUC_RE.search(text)
        return {
            "task": "?",
            "quality": qm.group(1).lower(),
            "halluc": hm.group(1).lower() if hm else "none",
            "reason": "loose-parse",
        }
    return None


def _judge_input_text(record: MatchedRequest) -> str:
    """Flatten the replayed request's messages into readable judge input."""
    body = record.request_json if isinstance(record.request_json, dict) else None
    if body is None:
        try:
            body = json.loads(record.request_body)
        except Exception:
            return ""
    if not isinstance(body, dict):
        return ""
    parts = []
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            # content may be a string or a multimodal part list; str() of the
            # list keeps the text parts visible, which is enough for grading.
            parts.append("[%s] %s" % (m.get("role"), str(m.get("content", ""))[:_JUDGE_INPUT_PER_MSG_CAP]))
    return "\n".join(parts)[:_JUDGE_INPUT_CAP]


def _record_has_tool_messages(record: MatchedRequest) -> bool:
    """True if the recorded request history contains any tool-role message.

    Used to (a) decide whether to run the tool-support preflight and (b) tag
    each request so 4xx failures on tool-carrying payloads can be counted
    separately (tool_call_4xx_errors). request_json is already parsed in the
    record, so this is cheap; falls back to parsing request_body if needed.
    """
    body = record.request_json if isinstance(record.request_json, dict) else None
    if body is None:
        try:
            body = json.loads(record.request_body)
        except Exception:
            return False
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return False
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs)


# Content-part `type` values that carry a multimodal image the server's model
# must be able to decode. OpenAI chat uses "image_url"; the Responses API and a
# few gateways use "input_image"/"image". A text-only endpoint 4xx's on any of
# these (or, on lax gateways, silently drops them).
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})


def _record_has_image_content(record: MatchedRequest) -> bool:
    """True if any message carries a multimodal image content part.

    Detects genuine vision inputs — a message whose `content` is a list holding
    an image part — NOT base64 blobs pasted into a plain-string message (those
    are just long text a text-only server accepts fine). Used to count 4xx
    failures on image-carrying payloads separately (image_4xx_errors) instead of
    letting them masquerade as tool_call_4xx_errors: in agent datasets almost
    every image request ALSO carries tool messages, so without this split a
    missing-vision-support 4xx looks identical to a missing-tool-support one.
    """
    body = record.request_json if isinstance(record.request_json, dict) else None
    if body is None:
        try:
            body = json.loads(record.request_body)
        except Exception:
            return False
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            if any(isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES
                   for part in content):
                return True
    return False


# ── HTTP-failure cause classification ────────────────────────────────────────
# Maps a failed request's SERVER-REPORTED error message onto a cause class, so
# failure counts say WHY requests died instead of which attributes they happened
# to carry: a tool-carrying request that overflowed the context window is a
# ctx_overflow, not evidence of a tool-support gap (the misread the old
# attribute-only counters invited). Patterns are checked in order against the
# lower-cased message; first hit wins. Wordings covered: SGLang and vLLM
# (self-hosted targets) plus the OpenAI-compatible phrasings gateways proxy.
_HTTP_ERROR_CLASS_PATTERNS = [
    ("ctx_overflow", (
        "longer than the model",                    # SGLang: prompt alone too big ("…model's context length")
        "requested token count exceeds",            # SGLang: prompt + max_tokens
        "maximum context length",                   # vLLM / OpenAI phrasing
        "context length exceeded",
        "context_length_exceeded",
    )),
    ("image_load", (
        "loading image data",                       # SGLang multimodal processor
        "while loading data imagedata",
        "cannot identify image",                    # PIL UnidentifiedImageError
        "failed to fetch image",                    # vLLM URL fetch
        "error in loading image",
    )),
    ("tool_schema", (
        "'parameters' schema",                      # SGLang jsonschema validation
        "invalid tool",
        "tool call validation",
    )),
    ("multi_system_template", (
        "system message must be",                   # strict chat templates
        "multiple system messages",
        "conversation roles must alternate",
    )),
    ("request_validation", (
        "validation error",                         # FastAPI/pydantic rejects
        "input should be",
        "field required",
        "must be one of",
    )),
]


def classify_http_error(status_code: int, body_text: str) -> tuple:
    """Classify a failed HTTP response into (cause_class, server_message).

    `server_message` is the human-readable error the server sent (extracted
    from the JSON error envelope when present, else the raw body) — empty
    string if the body was unreadable. `cause_class` falls back to a
    status-code family when no message pattern matches.
    """
    message = ""
    if body_text:
        try:
            obj = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            message = obj.get("message") or ""
            if not message and isinstance(obj.get("error"), dict):
                message = obj["error"].get("message") or ""
            if not isinstance(message, str):
                message = str(message)
        if not message:
            message = body_text.strip()
    low = message.lower()
    for cls, needles in _HTTP_ERROR_CLASS_PATTERNS:
        if any(n in low for n in needles):
            return cls, message
    if status_code in (401, 403):
        return "auth", message
    if status_code == 404:
        return "not_found", message
    if status_code == 413:
        return "payload_too_large", message
    if status_code == 422:
        return "request_validation", message
    if status_code == 429:
        return "rate_limited", message
    if status_code >= 500:
        return "server_error", message
    return f"http_{status_code}", message


class ReplayTest(BaseTest):
    """
    Replays the configured dataset (bench/examples/replay-smoke.jsonl by default)
    against the live service with configurable concurrency.

    Each record's request_body is sent verbatim (model field overridden to
    self.model).  The response is parsed as an SSE stream (or plain JSON
    fallback) to extract precise finish_reason / stop_reason, token counts,
    and post-finish anomalies.
    """

    name = "replay"

    def __init__(
        self,
        api_url: str,
        model: str,
        api_key: str,
        output_dir: str,
        dataset_path: str = REPLAY_DATASET_PATH,
        concurrency: int = REPLAY_CONCURRENCY,
        force_stream: bool = False,
        clean: bool = False,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        cancel_event=None,
        max_seconds: float = REPLAY_MAX_SECONDS,
        max_samples: int = REPLAY_SAMPLE_SIZE,
        request_timeout: float = REPLAY_REQUEST_TIMEOUT,
        max_retries: int = REPLAY_MAX_RETRIES,
        max_generation_tokens: int = REPLAY_MAX_GENERATION_TOKENS,
        max_sse_events: int = REPLAY_MAX_SSE_EVENTS,
        ttft_p99_redline_ms: float = REPLAY_TTFT_P99_REDLINE_MS,
        total_time_p99_redline_ms: float = REPLAY_TOTAL_TIME_P99_REDLINE_MS,
        uptime_floor: float = REPLAY_UPTIME_FLOOR,
        save_responses: bool = REPLAY_SAVE_RESPONSES,
        save_responses_max_bytes: int = REPLAY_SAVE_RESPONSES_MAX_BYTES,
        judge_enable: bool = False,
        judge_sample_rate: float = 0.05,
        judge_max_samples: int = 2000,
        judge_api_url: str = "",
        judge_model: str = "",
        judge_api_key: str = "",
        judge_max_tokens: int = 256,
        judge_concurrency: int = 8,
        judge_prompt: str = "",
        judge_disable_thinking: bool = True,
        judge_seed: int = 0,
        judge_max_retries: int = 5,
    ) -> None:
        super().__init__(api_url, model, api_key, output_dir)
        self.dataset_path = dataset_path
        self.concurrency = concurrency
        self.force_stream = force_stream
        # When True, normalize each replayed request's messages so a strict
        # Qwen/SGLang chat template accepts them (merge/relocate system messages).
        # Default off keeps the replay byte-for-byte. See normalize_system_messages.
        self.clean = clean
        self.progress_cb = progress_cb
        self.cancel_event = cancel_event
        self.max_seconds = max_seconds
        self.max_samples = max_samples
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.max_generation_tokens = max_generation_tokens
        # Absolute in-memory backstop on buffered SSE events per response (guards
        # against runaway generations OOM-ing the worker; see REPLAY_MAX_SSE_EVENTS).
        self.max_sse_events = max_sse_events
        self.ttft_p99_redline_ms = ttft_p99_redline_ms
        self.total_time_p99_redline_ms = total_time_p99_redline_ms
        self.uptime_floor = uptime_floor
        # Debug-only request/response capture (see REPLAY_SAVE_RESPONSES above).
        self.save_responses = save_responses
        self.save_responses_max_bytes = save_responses_max_bytes
        self._save_fh = None
        self._save_path: Optional[str] = None
        self._save_lock = threading.Lock()
        # Opt-in LLM answer-quality judge (see module docstring block above).
        self.judge_enable = judge_enable
        self.judge_sample_rate = judge_sample_rate
        self.judge_max_samples = judge_max_samples
        self.judge_api_url = judge_api_url.strip()
        self.judge_model = judge_model.strip()
        self.judge_api_key = judge_api_key.strip()
        self.judge_max_tokens = judge_max_tokens
        self.judge_concurrency = judge_concurrency
        self.judge_prompt = judge_prompt
        self.judge_disable_thinking = judge_disable_thinking
        self.judge_seed = judge_seed
        self.judge_max_retries = judge_max_retries
        self._judge_lock = threading.Lock()
        self._judge_disp: Counter = Counter()           # disposition tallies of sampled records
        self._judge_live: Counter = Counter()           # live verdict tallies (for log visibility)
        self._judge_futures: List[concurrent.futures.Future] = []
        self._judge_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._judge_sess: Optional[requests.Session] = None
        self._judge_url = ""
        self._judge_rubric = ""
        self._judge_extra: Dict[str, Any] = {}
        self._judge_retained = 0                        # sampling decisions made
        self._judge_cap_logged = False
        # Re-use TCP connections across requests (massive speed-up for localhost tests)
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        if self.api_key:
            self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> TestResult:
        plan = self._prepare_records()
        if plan is None:
            return TestResult(
                name=self.name,
                passed=True,
                metrics={"skipped": True, "reason": "REPLAY_DATASET_PATH not set"},
            )
        total, make_iter = plan
        if not total or make_iter is None:
            return TestResult(
                name=self.name,
                passed=False,
                metrics={},
                error=f"Dataset empty or unreadable: {self.dataset_path}",
            )

        logger.info(
            "[%s] Replaying %d requests, concurrency=%d",
            self.name, total, self.concurrency,
        )
        if self.save_responses:
            self._open_response_log()
        if self.judge_enable:
            # Pipelined judge: the pool must exist before the batch so each
            # sampled answer is judged the moment it completes.
            self._judge_setup()
        try:
            # Wall-clock bracket around the concurrent batch. Used to derive
            # throughput-per-minute metrics (input/output/cached TPM) so the
            # aggregate reflects "tokens processed per real minute of test time"
            # rather than per-request token rates. Loading records is excluded
            # because it's negligible vs the LLM-active window — and with streaming
            # dispatch it now overlaps the batch anyway (records arrive as slots free).
            self._preflight_tool_check(make_iter)
            wall_t0 = time.monotonic()
            results = self._run_concurrent(make_iter, total)
            wall_time_s = time.monotonic() - wall_t0
            logger.info("[%s] wall_time_s=%.3f  records=%d", self.name, wall_time_s, total)
            metrics = self._aggregate(results, wall_time_s)
            # Opt-in judge: verdicts ran pipelined with the batch; this drains
            # the stragglers (outside the wall-clock bracket, so TPM remains a
            # perf-batch measurement). All-None when disabled.
            metrics.update(self._run_judge_phase())
            passed = self._check_redlines(metrics)

            if not passed:
                logger.warning("[%s] FAILED redline check. metrics=%s", self.name, metrics)
            else:
                logger.info("[%s] PASSED. metrics=%s", self.name, metrics)

            return TestResult(name=self.name, passed=passed, metrics=metrics)
        finally:
            self._close_response_log()
            # Normally shut down by the drain; this covers a mid-batch raise.
            if self._judge_pool is not None:
                self._judge_pool.shutdown(wait=False, cancel_futures=True)
                self._judge_pool = None

    # ------------------------------------------------------------------
    # Dataset loading
    # ------------------------------------------------------------------

    def _prepare_records(self) -> Optional[Tuple[int, Optional[Callable[[], Iterator[MatchedRequest]]]]]:
        """Plan the batch WITHOUT loading it: return (total, make_iter).

        Returns None when the dataset path is unset (caller reports "skipped"), and
        (0, None) when the file is missing/empty/unreadable (caller reports an error).

        ``make_iter()`` returns a FRESH lazy iterator over exactly the records to be
        replayed, so the dataset is streamed from disk on demand and the whole file
        is never held in RAM — peak memory is O(in-flight window), not O(dataset).
        Callers needing a second pass (e.g. the bounded preflight scan) simply call
        ``make_iter()`` again; each call re-opens the file.

        Record selection is unchanged from the eager version: no cap replays the
        whole file; max_samples < n even-stride down-samples across the WHOLE
        dataset (same int(i*step) index formula); a dataset SHORTER than
        max_samples wraps around (re-read repeatedly) to reach exactly max_samples.
        """
        if not self.dataset_path:
            logger.warning("[%s] REPLAY_DATASET_PATH is not set — skipping.", self.name)
            return None

        path = Path(self.dataset_path)
        if not path.exists():
            logger.error("[%s] Dataset file not found: %s", self.name, path)
            return 0, None

        # lean=True: the run path never needs the parsed request_json / raw_payload
        # (consumers re-parse request_body on demand), and retaining them for a
        # multi-GB dataset is what OOM-kills the worker.
        def _raw() -> Iterator[MatchedRequest]:
            return load_extract_jsonl(str(path), lean=True)

        try:
            # One sizing pass. load_extract_jsonl yields exactly one record per
            # non-blank line, so the non-blank line count equals the record count —
            # and counting never parses or retains a record.
            #
            # count_lines reads BINARY: this pass only needs to know how many lines
            # there are, so decoding multi-GB of UTF-8 (and materialising a str per
            # line) is pure waste. It's a big sequential read — the access pattern a
            # slow/network FS handles best — and it warms the page cache for the
            # streaming pass that follows. For a gzipped dataset it also inflates
            # (~3.5s per 500MB), which is noise next to an LLM-bound batch.
            n = count_lines(path)
            if n == 0:
                logger.error("[%s] Dataset is empty: %s", self.name, path)
                return 0, None

            if self.max_samples <= 0:
                return n, _raw

            if n > self.max_samples:
                step = n / self.max_samples
                want = {int(i * step) for i in range(self.max_samples)}
                total = len(want)
                logger.info("[%s] Down-sampling %d of %d records.", self.name, total, n)

                def _downsampled() -> Iterator[MatchedRequest]:
                    for idx, rec in enumerate(_raw()):
                        if idx in want:
                            yield rec

                return total, _downsampled

            if n < self.max_samples:
                logger.warning(
                    "[%s] Dataset has only %d records but %d requested — "
                    "wrapping around (dataset replayed ~%.2fx) to reach %d.",
                    self.name, n, self.max_samples, self.max_samples / n, self.max_samples,
                )
                total = self.max_samples

                def _wrapped() -> Iterator[MatchedRequest]:
                    emitted = 0
                    while emitted < total:
                        for rec in _raw():
                            if emitted >= total:
                                return
                            yield rec
                            emitted += 1

                return total, _wrapped

            return n, _raw
        except Exception as exc:
            logger.error("[%s] Failed to read dataset: %s", self.name, exc)
            return 0, None

    # ------------------------------------------------------------------
    # Concurrent execution
    # ------------------------------------------------------------------

    def _preflight_tool_check(self, make_iter: Callable[[], Iterator[MatchedRequest]]) -> None:
        """Warn early if the dataset carries tool-calling conversations but the
        target can't render them.

        Sends ONE tiny synthetic tool request (cheap, max_tokens=1). A 4xx means
        the real tool-carrying records will likely fail the same way (counted as
        tool_call_4xx_errors). We do NOT auto-skip — this is just a heads-up so a
        low uptime on such a target is explainable rather than mysterious.
        Best-effort: network errors here never abort the run.

        Scans only the first REPLAY_PREFLIGHT_SCAN records (streamed, then
        released) rather than the whole dataset: this is a heads-up, not a census,
        and materializing a multi-GB dataset just to count tool messages is what
        the streaming dispatch exists to avoid. The exact per-request counts still
        land in the metrics (tool_requests_total / tool_call_4xx_errors).
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return
        n_tool = 0
        scanned = 0
        for rec in make_iter():
            if scanned >= REPLAY_PREFLIGHT_SCAN:
                break
            scanned += 1
            if _record_has_tool_messages(rec):
                n_tool += 1
        if not n_tool:
            return
        url = join_endpoint(self.api_url, "chat/completions")
        probe = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_0", "type": "function",
                                 "function": {"name": "noop", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
            ],
            "max_tokens": 1,
            "stream": False,
        }
        try:
            resp = self._session.post(url, json=probe, timeout=min(self.request_timeout, 30.0))
        except Exception as exc:
            logger.warning(
                "[%s] tool-support preflight could not complete (%s) — proceeding anyway; "
                "%d/%d scanned records carry tool messages and may fail if unsupported.",
                self.name, exc, n_tool, scanned,
            )
            return
        if 400 <= resp.status_code < 500:
            snippet = (resp.text or "").strip().replace("\n", " ")[:300]
            logger.warning(
                "[%s] TOOL-SUPPORT PREFLIGHT FAILED: HTTP %d on a minimal tool request. The "
                "target likely does NOT render tool_calls/tool-role messages, so %d/%d scanned "
                "records carrying tool messages will probably fail with 4xx (tracked as "
                "tool_call_4xx_errors; not skipped). Response: %s",
                self.name, resp.status_code, n_tool, scanned, snippet,
            )
        else:
            logger.info(
                "[%s] tool-support preflight OK (HTTP %d); %d/%d scanned records carry tool messages.",
                self.name, resp.status_code, n_tool, scanned,
            )

    def _run_concurrent(
        self,
        make_iter: Callable[[], Iterator[MatchedRequest]],
        total: int,
    ) -> List[Dict]:
        """Replay `total` records, STREAMING them from disk as pool slots free up.

        Records are pulled lazily and at most ``concurrency *
        REPLAY_INFLIGHT_MULTIPLIER`` are in flight at once, so each completed
        request's record becomes garbage immediately: peak memory is O(in-flight
        window), not O(dataset). That is what lets a multi-GB replay file run in a
        worker that cannot hold it.

        ``results`` keeps one small dict per record, indexed by position, with an
        empty dict meaning "never dispatched" — the contract ``_aggregate`` relies
        on for attempted_requests / not_started_requests.
        """
        url = join_endpoint(self.api_url, "chat/completions")
        results: List[Dict] = [{} for _ in range(total)]
        # Report progress roughly every 5 % (but at least once)
        report_every = max(1, total // 20)
        last_reported = 0
        # In-flight cap. Keep it a multiple of concurrency so the pool always has
        # queued work (never starves waiting on the next disk read).
        window = max(self.concurrency, self.concurrency * max(1, REPLAY_INFLIGHT_MULTIPLIER))

        logger.info("[%s] BEGIN batch  total=%d  concurrency=%d  window=%d  url=%s",
                    self.name, total, self.concurrency, window, url)
        t0 = time.monotonic()

        def send(idx: int, record: MatchedRequest) -> None:
            results[idx] = self._send_request(record, url)

        record_iter = make_iter()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency)
        pending: set = set()
        dispatched = 0
        completed = 0
        exhausted = False
        try:
            while True:
                # Top up the in-flight window from the streamed iterator. Bounded by
                # `total` as well as the window: if the file grew since it was
                # counted, extra records are ignored rather than overrunning results.
                while not exhausted and len(pending) < window and dispatched < total:
                    try:
                        record = next(record_iter)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.add(pool.submit(send, dispatched, record))
                    dispatched += 1
                    # Drop our own reference so the only one left is the queued work
                    # item — the record is freed the moment its request finishes.
                    del record

                # Drive termination off the pending set, NOT a completed counter.
                # Using the counter risks a spin-forever bug if any increment is
                # lost (older code did this without a lock and hung when many
                # requests failed in lockstep, because their callbacks raced).
                if not pending:
                    break

                # Cooperative cancellation — check every second so we don't block forever
                if self.cancel_event is not None and self.cancel_event.is_set():
                    logger.info("[%s] Cancellation detected — aborting batch", self.name)
                    # Close session to force in-flight requests to error out quickly
                    try:
                        self._session.close()
                    except Exception:
                        pass
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                # Hard time budget — stop dispatching and let the aggregator
                # summarize whatever finished. Distinct from cancellation: the
                # submission stays in DONE status, not CANCELED.
                if self.max_seconds > 0 and (time.monotonic() - t0) >= self.max_seconds:
                    logger.warning(
                        "[%s] Time budget reached (%.0fs) — stopping batch with %d in flight, "
                        "%d/%d dispatched",
                        self.name, self.max_seconds, len(pending), dispatched, total,
                    )
                    try:
                        self._session.close()
                    except Exception:
                        pass
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                done, pending = concurrent.futures.wait(
                    pending, timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    continue
                # Report progress from main thread (not worker threads) so the
                # Redis publish inside the progress callback is thread-safe.
                completed += len(done)
                if completed - last_reported >= report_every or completed == total:
                    last_reported = completed
                    fraction = 0.1 + 0.8 * (completed / total)
                    logger.info("[replay] Progress: %.0f%% (%d/%d) has_cb=%s",
                                fraction * 100, completed, total, self.progress_cb is not None)
                    if self.progress_cb is not None:
                        self.progress_cb(fraction, f"Replayed {completed}/{total} requests")
        except Exception:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            # Release the dataset file handle held by the (possibly unexhausted)
            # generator instead of waiting for GC.
            try:
                record_iter.close()
            except Exception:
                pass
            # Retire the pool's threads deterministically. wait=False so a
            # cancelled/timed-out batch still returns immediately (in-flight
            # requests finish into `results` exactly as before); idempotent when
            # the break paths already shut it down. Without this the threads only
            # go away when the executor is GC'd, which accumulates across the many
            # submissions a long-lived worker process handles.
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass

        elapsed = time.monotonic() - t0
        # `completed` counts futures actually collected; a cancel/time-budget break
        # can leave in-flight work uncounted, so treat it as a floor. The
        # authoritative attempted/not-started split is derived from `results` in
        # _aggregate (a non-empty dict = the request was dispatched).
        throughput = completed / elapsed if elapsed > 0 else 0.0
        logger.info(
            "[%s] END   batch  total=%d  dispatched=%d  completed=%d  elapsed=%.1fs  throughput=%.2f req/s",
            self.name, total, dispatched, completed, elapsed, throughput,
        )

        return results

    def _send_request(
        self,
        record: MatchedRequest,
        url: str,
    ) -> Dict:
        """Send one request, override model, collect TTFT, tokens, and precise finish markers."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return {
                "request_id": record.request_id,
                "ttft_ms": None,
                "total_ms": 0.0,
                "response_finished": False,
                "finish_marker": None,
                "finish_reason": None,
                "stop_reason": None,
                "response_bytes": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "output_tps": 0.0,
                "input_tps": 0.0,
                "tokens_after_finish": 0,
                "repetitive_tokens": False,
                "status_code": None,
                "had_tool_messages": False,
                "tool_call_error": False,
                "had_image_content": False,
                "image_call_error": False,
                "multi_system": False,
                "system_normalized": False,
                "generation_capped": False,
                "error": "Canceled",
                "error_class": "canceled",
                "error_detail": None,
                "success": False,
            }

        # Judge sampling is decided BEFORE the request is sent (deterministic
        # per-request hash draw), so retention is outcome-independent —
        # sampling after seeing the result would bias the judged population.
        sampled = self._judge_sample(record.request_id)

        body_bytes = build_replay_body(
            record, self.model, force_stream=self.force_stream,
            max_generation_tokens=self.max_generation_tokens,
            clean=self.clean,
        )
        had_tool = _record_has_tool_messages(record)
        # Does this record carry a multimodal image content part? A text-only
        # endpoint 4xx's on these; tracking it lets image failures be counted
        # apart from tool failures (image_4xx_errors vs tool_call_4xx_errors)
        # even though most image requests here also carry tool messages.
        had_image = _record_has_image_content(record)
        # Does this record carry the multiple/out-of-order system messages that a
        # strict template rejects? Tracked regardless of `clean` so the metrics
        # surface the real cause of 4xx's (and, when clean=True, what got fixed).
        multi_system = request_needs_system_normalization(record)
        save = self.save_responses
        # Client-side backstop for servers that overrun the injected max_tokens
        # (e.g. reasoning models that don't bound thinking tokens). 0 = off.
        client_hard_cap = self.max_generation_tokens * 2 if self.max_generation_tokens > 0 else 0
        # Effective in-memory ceiling on buffered SSE events: the tighter of the
        # per-request cap (when max_generation_tokens is set) and the absolute
        # backstop (self.max_sse_events, applied even when the per-request cap is
        # off). Without this, a non-terminating generation buffers sse_events
        # without bound and OOM-kills the worker.
        event_ceiling = client_hard_cap
        if self.max_sse_events > 0:
            event_ceiling = min(event_ceiling, self.max_sse_events) if event_ceiling else self.max_sse_events
        # Raw response bytes captured for debug dump (only the final attempt's
        # body is kept — reset at the top of each retry below).
        raw_parts: List[bytes] = []
        raw_captured = 0

        t_start = time.monotonic()
        ttft_ms: Optional[float] = None
        total_ms: float = 0.0
        status_code: Optional[int] = None
        response_finished = False
        finish_marker: Optional[str] = None
        finish_reason: Optional[str] = None
        stop_reason: Optional[str] = None
        error: Optional[str] = None
        error_class: Optional[str] = None
        error_detail: Optional[str] = None
        error_body_text = ""
        response_bytes = 0
        prompt_tokens = 0
        completion_tokens = 0
        cached_tokens = 0
        output_tps = 0.0
        input_tps = 0.0
        tokens_after_finish = 0
        repetitive_tokens = False
        generation_capped = False
        last_tokens: deque[str] = deque(maxlen=10)
        first_token = True
        # Response text retained ONLY for judge-sampled requests; the normal
        # perf path keeps discarding content (these lists stay empty).
        judge_content_parts: List[str] = []
        judge_reasoning_parts: List[str] = []

        attempt = 0
        retry_delay = 5.0
        while True:
            attempt += 1
            logger.info("[%s] START request_id=%s attempt=%d", self.name, record.request_id, attempt)
            # Reset ALL per-attempt state so a retry after a partially-streamed
            # attempt doesn't inherit its TTFT, byte count, or token counts. Only
            # the final attempt's values should reach the result dict.
            raw_parts = []
            raw_captured = 0
            error_body_text = ""
            generation_capped = False
            ttft_ms = None
            first_token = True
            response_bytes = 0
            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            tokens_after_finish = 0
            finish_reason = None
            stop_reason = None
            finish_marker = None
            output_tps = 0.0
            input_tps = 0.0
            repetitive_tokens = False
            last_tokens.clear()
            judge_content_parts = []
            judge_reasoning_parts = []
            try:
                t_post = time.monotonic()
                with self._session.post(
                    url,
                    data=body_bytes,
                    timeout=self.request_timeout,
                    stream=True,
                ) as resp:
                    t_post_done = time.monotonic()
                    status_code = resp.status_code
                    logger.info(
                        "[%s] POST  request_id=%s status=%d post_dur=%.3fs",
                        self.name, record.request_id, resp.status_code, t_post_done - t_post,
                    )
                    # For an error status the body IS the useful debug payload
                    # (the server's error message), but raise_for_status() is
                    # about to abort before we ever read the stream. Grab it now
                    # — always, not just when saving: the failure-cause
                    # classification (error_class) reads it too.
                    if status_code >= 400:
                        try:
                            err_bytes = resp.content
                        except Exception:
                            err_bytes = b""
                        # Server error envelopes are small JSON docs; cap defensively.
                        error_body_text = err_bytes[:65536].decode("utf-8", errors="replace")
                        if save and err_bytes:
                            raw_captured = self._capture_raw(raw_parts, raw_captured, err_bytes)
                    resp.raise_for_status()

                    buffer = b""
                    sse_events: list[dict] = []
                    for chunk in resp.iter_content(chunk_size=8192):
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            break
                        if not chunk:
                            break
                        response_bytes += len(chunk)
                        if save:
                            raw_captured = self._capture_raw(raw_parts, raw_captured, chunk)
                        buffer += chunk
                        # Extract complete SSE lines
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line_str = line.decode("utf-8", errors="replace").strip()
                            if not line_str.startswith("data:"):
                                continue
                            data_str = line_str[5:].strip()
                            if data_str == "[DONE]":
                                continue
                            try:
                                ev = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            sse_events.append(ev)
                            # TTFT MUST be captured here, as each event streams in.
                            # The token loop below runs only AFTER iter_content has
                            # drained the whole response, so setting ttft there would
                            # measure total time, not time-to-first-token. Fire on the
                            # first content/reasoning/tool_call/text delta.
                            if first_token:
                                for _ch in ev.get("choices", []):
                                    _d = _ch.get("delta", {})
                                    if (_d.get("content") or _d.get("reasoning")
                                            or _d.get("reasoning_content")
                                            or _d.get("tool_calls") or _ch.get("text")):
                                        ttft_ms = (time.monotonic() - t_start) * 1000.0
                                        first_token = False
                                        break
                        # Backstop: stop reading if the server blew past the injected
                        # max_tokens (one SSE event ≈ one token, so 2x the cap leaves
                        # ample headroom for a server that honors it) OR the response
                        # hit the absolute in-memory ceiling (runaway generation with
                        # no max_tokens cap). Closing the `with` block aborts the
                        # request server-side too. Marks generation_capped.
                        if event_ceiling and len(sse_events) >= event_ceiling:
                            generation_capped = True
                            break

                    # Fallback: non-streaming JSON response
                    if not sse_events and buffer.strip():
                        try:
                            sse_events.append(json.loads(buffer.decode("utf-8", errors="replace")))
                        except json.JSONDecodeError:
                            pass

                    # Process parsed events
                    has_usage = False
                    for data in sse_events:
                        usage = data.get("usage")
                        if usage:
                            if usage.get("completion_tokens"):
                                completion_tokens = usage["completion_tokens"]
                                has_usage = True
                            if usage.get("prompt_tokens"):
                                prompt_tokens = usage["prompt_tokens"]
                            # Cached prompt tokens. Spelling differs by vendor — check
                            # known shapes in priority order; default 0 if absent.
                            cached_val = None
                            details = usage.get("prompt_tokens_details")
                            if isinstance(details, dict):
                                cached_val = details.get("cached_tokens")
                            if cached_val is None:
                                cached_val = (
                                    usage.get("cached_tokens")
                                    or usage.get("prompt_cache_hit_tokens")
                                )
                            if cached_val:
                                try:
                                    cached_tokens = int(cached_val)
                                except (TypeError, ValueError):
                                    pass

                        for choice in data.get("choices", []):
                            delta = choice.get("delta", {})
                            if sampled:
                                # Retain the answer text for the judge phase.
                                # Streaming carries pieces in delta; a plain
                                # JSON response carries them in message.
                                msg = choice.get("message") or {}
                                piece = delta.get("content") or msg.get("content") or choice.get("text")
                                if piece:
                                    judge_content_parts.append(str(piece))
                                rpiece = (delta.get("reasoning") or delta.get("reasoning_content")
                                          or msg.get("reasoning") or msg.get("reasoning_content"))
                                if rpiece:
                                    judge_reasoning_parts.append(str(rpiece))
                            token = (
                                delta.get("content")
                                or delta.get("reasoning")
                                or delta.get("reasoning_content")
                                or choice.get("text")
                            )
                            if token:
                                last_tokens.append(token)
                                if first_token:
                                    ttft_ms = (time.monotonic() - t_start) * 1000.0
                                    first_token = False
                                if finish_reason is not None or stop_reason is not None:
                                    tokens_after_finish += 1
                                elif not has_usage:
                                    # Rough count when usage block is absent
                                    completion_tokens += 1

                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            if choice.get("stop_reason"):
                                stop_reason = choice["stop_reason"]

                    # Fallback token counting for non-streaming without usage
                    if not has_usage and not completion_tokens:
                        for data in sse_events:
                            for choice in data.get("choices", []):
                                content = (choice.get("message") or {}).get("content") or choice.get("text") or ""
                                if content:
                                    completion_tokens += len(content.split())
                                    if first_token:
                                        ttft_ms = (time.monotonic() - t_start) * 1000.0
                                        first_token = False

                    total_ms = (time.monotonic() - t_start) * 1000.0
                    response_finished = finish_reason is not None or stop_reason is not None
                    elapsed_sec = total_ms / 1000.0 if total_ms > 0 else 0.0
                    # output_tps: completion tokens over the WHOLE request time
                    # (OpenRouter-style throughput — denominator includes prefill/TTFT).
                    output_tps = completion_tokens / elapsed_sec if elapsed_sec > 0 else 0.0
                    # input_tps: prefill rate = prompt tokens over TTFT (the time
                    # the server spent ingesting the prompt before the first token).
                    ttft_sec = (ttft_ms / 1000.0) if ttft_ms else 0.0
                    input_tps = prompt_tokens / ttft_sec if ttft_sec > 0 else 0.0
                    repetitive_tokens = len(last_tokens) == 10 and len(set(last_tokens)) == 1

                    # Derive backward-compatible finish_marker
                    if finish_reason == "stop":
                        finish_marker = "openai_finish_reason_stop"
                    elif finish_reason == "length":
                        finish_marker = "openai_finish_reason_length"
                    elif finish_reason == "content_filter":
                        finish_marker = "openai_finish_reason_content_filter"
                    elif finish_reason == "tool_calls":
                        finish_marker = "openai_finish_reason_tool_calls"
                    elif finish_reason == "function_call":
                        finish_marker = "openai_finish_reason_function_call"
                    elif stop_reason == "end_turn":
                        finish_marker = "anthropic_stop_reason_end_turn"
                    elif stop_reason == "tool_use":
                        finish_marker = "anthropic_stop_reason_tool_use"
                    elif stop_reason == "max_tokens":
                        finish_marker = "anthropic_stop_reason_max_tokens"
                    elif stop_reason == "stop_sequence":
                        finish_marker = "anthropic_stop_reason_stop_sequence"

                    # A client-imposed generation cap is a deliberate, successful
                    # stop — not a hang or a failure. Tag it distinctly so it
                    # neither masquerades as a model stop nor inflates the
                    # unfinished_rate. (When the server honored the injected
                    # max_tokens, finish_reason="length" already won above and
                    # generation_capped stays False.)
                    if generation_capped and not response_finished:
                        finish_marker = "client_generation_cap"
                        response_finished = True

                    logger.info(
                        "[%s] DONE  request_id=%s total=%.1fms bytes=%d finished=%s "
                        "finish=%s stop=%s out_tok=%d prompt_tok=%d tps=%.1f tok_after_finish=%d repetitive=%s capped=%s",
                        self.name, record.request_id, total_ms, response_bytes,
                        response_finished, finish_reason, stop_reason,
                        completion_tokens, prompt_tokens, output_tps,
                        tokens_after_finish, repetitive_tokens, generation_capped,
                    )
                    if generation_capped:
                        logger.warning(
                            "[%s] GENERATION CAPPED request_id=%s — client stopped reading after "
                            "%d streamed events (>= 2x max_generation_tokens=%d); the server overran "
                            "the injected max_tokens (likely runaway / non-terminating output).",
                            self.name, record.request_id, len(sse_events), self.max_generation_tokens,
                        )
                break
            except Exception as exc:
                # Retry only TRANSIENT failures: connection drop / timeout /
                # mid-stream chunk error, or a 429 / 5xx. A 4xx is a deterministic
                # request-shape rejection (bad body, multiple system messages,
                # unsupported tools) — retrying just burns the backoff and the
                # same 4xx returns, so fail it immediately. status_code was
                # captured before raise_for_status(), so it's set for HTTPErrors.
                retryable = (
                    isinstance(exc, (requests.exceptions.ConnectionError,
                                     requests.exceptions.Timeout,
                                     requests.exceptions.ChunkedEncodingError))
                    or (isinstance(exc, requests.exceptions.HTTPError)
                        and status_code is not None
                        and is_retryable_http_status(status_code))
                )
                if retryable and attempt <= self.max_retries:
                    logger.warning(
                        "[%s] RETRY request_id=%s attempt=%d status=%s error=%s — retrying in %.1fs",
                        self.name, record.request_id, attempt, status_code, exc, retry_delay,
                    )
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60.0)
                    continue
                total_ms = (time.monotonic() - t_start) * 1000.0
                error = f"{type(exc).__name__}: {exc}"
                # Classify the CAUSE (see classify_http_error): HTTP failures by
                # the server's own error message, transport failures by kind.
                if isinstance(exc, requests.exceptions.HTTPError) and status_code is not None:
                    error_class, error_detail = classify_http_error(status_code, error_body_text)
                elif isinstance(exc, requests.exceptions.Timeout):
                    error_class = "timeout"
                elif isinstance(exc, requests.exceptions.ConnectionError):
                    error_class = "connection"
                elif isinstance(exc, requests.exceptions.ChunkedEncodingError):
                    error_class = "stream_aborted"
                else:
                    error_class = type(exc).__name__
                logger.warning("[%s] FAIL  request_id=%s status=%s class=%s error=%s server_msg=%s (retryable=%s, attempt=%d)",
                               self.name, record.request_id, status_code, error_class, error,
                               (error_detail or "").replace("\n", " ")[:300] or "-", retryable, attempt)
                break

        out = {
            "request_id": record.request_id,
            "ttft_ms": ttft_ms,
            "total_ms": total_ms,
            "response_finished": response_finished,
            "finish_marker": finish_marker,
            "finish_reason": finish_reason,
            "stop_reason": stop_reason,
            "response_bytes": response_bytes,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "output_tps": output_tps,
            "input_tps": input_tps,
            "tokens_after_finish": tokens_after_finish,
            "repetitive_tokens": repetitive_tokens,
            "generation_capped": generation_capped,
            "status_code": status_code,
            "had_tool_messages": had_tool,
            # Heuristic: a tool-carrying request that the server rejected with a
            # 4xx (excluding rate-limit 429). Historically read as "can't render
            # tool_calls", but a multiple-system-message 4xx looks identical — see
            # multi_system / multi_system_4xx_errors for the more precise signal.
            "tool_call_error": bool(
                had_tool and status_code is not None
                and 400 <= status_code < 500 and status_code != 429
            ),
            "had_image_content": had_image,
            # A multimodal-image request the server rejected with a 4xx (excluding
            # rate-limit 429) — a strong signal the target's model isn't
            # vision-capable. Counted alongside tool_call_error (a request can
            # trip both); compare the two to tell a missing-vision-support 4xx
            # apart from a missing-tool-support one.
            "image_call_error": bool(
                had_image and status_code is not None
                and 400 <= status_code < 500 and status_code != 429
            ),
            # Request carried >1 / out-of-order system messages (strict-template
            # reject shape). `system_normalized` = we actually rewrote it (clean on).
            "multi_system": multi_system,
            "system_normalized": bool(self.clean and multi_system),
            "error": error,
            # Cause class + the server's own error message (see
            # classify_http_error) — the authoritative "why" behind a failure,
            # aggregated into error_class_counts. None on success.
            "error_class": error_class,
            "error_detail": (error_detail or "").replace("\n", " ")[:500] or None,
            "success": error is None,
        }
        if save:
            self._write_response_record(record, body_bytes, b"".join(raw_parts), out)
        if sampled:
            self._judge_submit({
                "request_id": record.request_id,
                "input": _judge_input_text(record),
                "answer": "".join(judge_content_parts)[:_JUDGE_ANSWER_CAP],
                "reasoning": "".join(judge_reasoning_parts)[:_JUDGE_REASONING_CAP],
                "finish_reason": finish_reason,
                "stop_reason": stop_reason,
                "response_finished": response_finished,
                "generation_capped": generation_capped,
                "status_code": status_code,
                "success": error is None,
            })
        return out

    # ------------------------------------------------------------------
    # Debug-only request/response capture (REPLAY_SAVE_RESPONSES)
    # ------------------------------------------------------------------

    def _capture_raw(self, raw_parts: List[bytes], raw_captured: int, chunk: bytes) -> int:
        """Append `chunk` (or the part still under the byte cap) to raw_parts.

        Returns the new running byte count. cap<=0 means unlimited.
        """
        cap = self.save_responses_max_bytes
        if cap <= 0:
            raw_parts.append(chunk)
            return raw_captured + len(chunk)
        remaining = cap - raw_captured
        if remaining <= 0:
            return raw_captured
        take = chunk[:remaining]
        raw_parts.append(take)
        return raw_captured + len(take)

    def _open_response_log(self) -> None:
        """Open the JSONL capture file under output_dir. Best-effort: a failure
        here disables capture but never aborts the run."""
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            self._save_path = os.path.join(self.output_dir, "replay_responses.jsonl")
            self._save_fh = open(self._save_path, "a", encoding="utf-8")
            logger.info(
                "[%s] SAVING per-request request/response capture to %s "
                "(<=%d raw bytes each; REPLAY_SAVE_RESPONSES_MAX_BYTES=0 for full). "
                "Inspect with: kubectl exec <worker-pod> -- cat %s",
                self.name, os.path.abspath(self._save_path),
                self.save_responses_max_bytes, os.path.abspath(self._save_path),
            )
        except Exception as exc:
            logger.warning(
                "[%s] Could not open response capture file (%s) — continuing without saving.",
                self.name, exc,
            )
            self._save_fh = None
            self._save_path = None

    def _close_response_log(self) -> None:
        fh = self._save_fh
        self._save_fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            if self._save_path:
                logger.info("[%s] Response capture written to %s",
                            self.name, os.path.abspath(self._save_path))

    def _write_response_record(
        self,
        record: MatchedRequest,
        body_bytes: bytes,
        raw_bytes: bytes,
        result: Dict,
    ) -> None:
        """Write one request+response as a JSONL line. Thread-safe; called from
        worker threads. Best-effort: a write failure is logged, never raised."""
        fh = self._save_fh
        if fh is None:
            return
        # The body actually sent (model overridden vs the recorded original).
        try:
            request_payload: Any = json.loads(body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            request_payload = body_bytes.decode("utf-8", errors="replace")
        cap = self.save_responses_max_bytes
        truncated = cap > 0 and len(raw_bytes) > cap
        entry = {
            "request_id": record.request_id,
            "status_code": result.get("status_code"),
            "success": result.get("success"),
            "error": result.get("error"),
            "error_class": result.get("error_class"),
            "error_detail": result.get("error_detail"),
            "finish_reason": result.get("finish_reason"),
            "stop_reason": result.get("stop_reason"),
            "response_finished": result.get("response_finished"),
            "ttft_ms": result.get("ttft_ms"),
            "total_ms": result.get("total_ms"),
            "prompt_tokens": result.get("prompt_tokens"),
            "completion_tokens": result.get("completion_tokens"),
            "response_bytes": result.get("response_bytes"),
            "tokens_after_finish": result.get("tokens_after_finish"),
            "repetitive_tokens": result.get("repetitive_tokens"),
            "request": request_payload,
            # Raw response stream (SSE frames or non-streaming JSON), capped.
            "response_raw": raw_bytes.decode("utf-8", errors="replace"),
            "response_truncated": truncated,
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[%s] Could not serialize capture for %s: %s",
                           self.name, record.request_id, exc)
            return
        with self._save_lock:
            try:
                fh.write(line + "\n")
                fh.flush()
            except Exception as exc:
                logger.warning("[%s] Failed to write capture for %s: %s",
                               self.name, record.request_id, exc)

    # ------------------------------------------------------------------
    # Opt-in LLM answer-quality judge phase
    # ------------------------------------------------------------------

    def _judge_sample(self, request_id: str) -> bool:
        """Decide whether the request being sent should retain its response
        text for judging.

        DETERMINISTIC: the draw is a pure function of (judge_seed, request_id)
        — a seed-keyed crc32 hash mapped to [0,1) and compared against
        judge_sample_rate — so the SAME requests are judged on every run of
        the same dataset, regardless of concurrency or thread order. That
        makes A/B runs against different endpoints grade identical prompts on
        both sides (the responses differ, the judged subset doesn't). Change
        judge_seed to rotate the subset. crc32, not Python's per-process
        salted hash(), keeps decisions stable across worker processes.

        Caveat: if judge_max_samples trips, the candidate set is still
        deterministic but which candidates got in first is completion-order
        dependent — size the cap above rate x dataset when strict
        reproducibility matters.
        """
        if not self.judge_enable or self.judge_sample_rate <= 0.0:
            return False
        h = zlib.crc32(("%d:%s" % (self.judge_seed, request_id)).encode("utf-8"))
        if h / 4294967296.0 >= self.judge_sample_rate:   # crc32 is uniform on [0, 2^32)
            return False
        with self._judge_lock:
            if self.judge_max_samples > 0 and self._judge_retained >= self.judge_max_samples:
                if not self._judge_cap_logged:
                    self._judge_cap_logged = True
                    logger.info(
                        "[%s] judge_max_samples=%d reached — no further requests "
                        "retained for judging (raise it or lower judge_sample_rate "
                        "to cover more of the run).",
                        self.name, self.judge_max_samples,
                    )
                return False
            self._judge_retained += 1
            return True

    def _judge_setup(self) -> None:
        """Prepare the judge session/pool. Called once at run start when the
        judge is enabled, BEFORE the perf batch, so answered samples can be
        judged pipelined with the replay instead of serially after it."""
        self._judge_url = join_endpoint(self.judge_api_url, "chat/completions")
        self._judge_sess = requests.Session()
        self._judge_sess.headers.update({"Content-Type": "application/json"})
        if self.judge_api_key:
            self._judge_sess.headers.update({"Authorization": f"Bearer {self.judge_api_key}"})
        self._judge_rubric = self.judge_prompt or _JUDGE_RUBRIC
        self._judge_extra = (_disable_thinking_fields(self.judge_model)
                             if self.judge_disable_thinking else {})
        self._judge_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, self.judge_concurrency), thread_name_prefix="judge")
        logger.info(
            "[%s] JUDGE enabled: model=%s url=%s sample_rate=%.3f max_samples=%d "
            "concurrency=%d (pipelined — verdicts run alongside the replay batch)",
            self.name, self.judge_model, self._judge_url, self.judge_sample_rate,
            self.judge_max_samples, self.judge_concurrency,
        )

    def _judge_submit(self, rec: Dict[str, Any]) -> None:
        """Classify one retained record and, if answered, fire its judge call
        immediately on the judge pool (called from replay worker threads as
        each sampled request completes)."""
        disp = judge_disposition(rec)
        with self._judge_lock:
            self._judge_disp[disp] += 1
            sampled_total = sum(self._judge_disp.values())
            disp_tally = dict(self._judge_disp)
            if disp == "answered" and self._judge_pool is not None:
                self._judge_futures.append(self._judge_pool.submit(self._judge_one, rec))
        # One line per sampled record so the operator can see the judge working
        # (and see WHY nothing is judged when e.g. traffic is all tool_calls).
        logger.info(
            "[%s] JUDGE sampled request_id=%s disposition=%s%s (sampled=%d: %s)",
            self.name, rec.get("request_id"), disp,
            " → queued for judging" if disp == "answered"
            else (" (counted good, not judged)" if disp == "toolcall" else " (excluded)"),
            sampled_total, disp_tally,
        )

    def _judge_one(self, rec: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Grade one answered record with the judge LLM. Returns the parsed
        verdict dict, or None on transport/parse failure (never raises)."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return None
        body = "【输入】\n%s\n\n【回答】\n%s\n\n【思考】\n%s" % (
            rec.get("input", ""), rec.get("answer", ""),
            (rec.get("reasoning") or "")[:1200],
        )
        payload = {
            "model": self.judge_model,
            "messages": [
                {"role": "system", "content": self._judge_rubric},
                {"role": "user", "content": body},
            ],
            "max_tokens": self.judge_max_tokens,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._judge_extra,
        }
        # Retry policy: an external judge (often a bigger, busier model) fails
        # transiently a lot, so transient errors — connection/timeout, 429,
        # 5xx — get judge_max_retries attempts with exponential backoff. A
        # non-retryable 4xx (bad key/model/payload) fails immediately: the
        # same rejection would just come back. An unparseable verdict gets ONE
        # re-ask (temperature=0 makes a repeat likely, but streaming glitches
        # and nondeterministic backends do recover).
        last_err = ""
        transient_retries = 0
        parse_retries = 0
        delay = 2.0
        while True:
            res = send_chat(self._judge_sess, self._judge_url, payload,
                            timeout=min(self.request_timeout, 120.0))
            if res.ok:
                # A thinking judge that ignored the thinking-off switch may put
                # the verdict in reasoning — parse both before failing.
                v = _parse_replay_verdict(res.content) or _parse_replay_verdict(res.reasoning)
                if v is not None:
                    # No per-verdict rows are persisted, so log EVERY verdict
                    # (with a live running tally) — flagged ones at WARNING.
                    with self._judge_lock:
                        self._judge_live[v["quality"]] += 1
                        if v["halluc"] != "none":
                            self._judge_live["halluc_" + v["halluc"]] += 1
                        tally = self._judge_tally_locked()
                    flagged = v["quality"] == "poor" or v["halluc"] == "clear"
                    (logger.warning if flagged else logger.info)(
                        "[%s] JUDGE verdict request_id=%s quality=%s halluc=%s task=%s :: %s (%s)",
                        self.name, rec.get("request_id"), v["quality"], v["halluc"],
                        v.get("task", "?"), v.get("reason", ""), tally,
                    )
                    return v
                last_err = "unparseable: %r" % ((res.content or res.reasoning)[:120],)
                if parse_retries >= 1:
                    break
                parse_retries += 1
            else:
                last_err = "status=%s error=%s" % (res.status, res.error)
                # status None = transport failure (connection/timeout) — retryable.
                retryable = res.status is None or is_retryable_http_status(int(res.status))
                if not retryable or transient_retries >= self.judge_max_retries:
                    break
                transient_retries += 1
            if self.cancel_event is not None and self.cancel_event.is_set():
                break  # don't retry into a canceled run
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        with self._judge_lock:
            self._judge_live["failed"] += 1
            tally = self._judge_tally_locked()
        logger.warning("[%s] JUDGE failed request_id=%s after %d transient / %d parse retries (%s) (%s)",
                       self.name, rec.get("request_id"), transient_retries, parse_retries, last_err, tally)
        return None

    def _judge_tally_locked(self) -> str:
        """Running verdict tally for log lines. Caller holds _judge_lock."""
        lv = self._judge_live
        return ("judged %d: good=%d acc=%d poor=%d halluc_clear=%d halluc_suspected=%d failed=%d"
                % (lv["good"] + lv["acceptable"] + lv["poor"] + lv["failed"],
                   lv["good"], lv["acceptable"], lv["poor"],
                   lv["halluc_clear"], lv["halluc_suspected"], lv["failed"]))

    def _run_judge_phase(self) -> Dict[str, Any]:
        """Drain the pipelined judge verdicts and tally the judge metrics.

        Judge calls were fired as each sampled answer completed (overlapping
        the perf batch — a slow judge model runs alongside the replay instead
        of serializing behind it); this waits for the stragglers, so with a
        judge faster than the run it returns near-instantly. Runs outside the
        wall-clock bracket, so TPM stays a perf-batch measurement. Returns the
        judge metric dict — all None when the judge is disabled, so the default
        judge redlines are skipped by check_redlines.
        """
        if not self.judge_enable:
            return {k: None for k in JUDGE_METRIC_KEYS}

        with self._judge_lock:
            disp = Counter(self._judge_disp)
            futures = list(self._judge_futures)
        sampled = sum(disp.values())

        quality: Counter = Counter()
        halluc: Counter = Counter()
        # Answered samples that never got a judge future (pool unavailable —
        # defensive; shouldn't happen when run() did the setup).
        failures = disp.get("answered", 0) - len(futures)
        if failures:
            logger.warning("[%s] JUDGE: %d answered samples were never submitted for judging.",
                           self.name, failures)
        n = len(futures)
        if n:
            already_done = sum(1 for f in futures if f.done())
            logger.info(
                "[%s] JUDGE drain: %d answered of %d sampled (dispositions=%s, "
                "judge_model=%s); %d verdicts already finished during the batch",
                self.name, n, sampled, dict(disp), self.judge_model, already_done,
            )
            if self.progress_cb is not None:
                self.progress_cb(0.90, f"Waiting for {n - already_done} of {n} judge verdicts "
                                       f"({self.judge_model})")
            done = 0
            step = max(1, n // 20)
            for fut in concurrent.futures.as_completed(futures):
                v = fut.result()  # _judge_one never raises
                done += 1
                if v is None:
                    failures += 1
                else:
                    quality[v["quality"]] += 1
                    halluc[v["halluc"]] += 1
                if self.progress_cb is not None and (
                        done == 1 or done % step == 0 or done == n):
                    self.progress_cb(0.90 + 0.08 * done / n, f"Judged {done}/{n} answers")
        if self._judge_pool is not None:
            self._judge_pool.shutdown(wait=False)
            self._judge_pool = None

        # "Kept" = the genuinely-completed branch: LLM-judged answers plus
        # tool_calls turns (an empty answer with tool_calls is a NORMAL agentic
        # turn — counted good without asking the judge). error/length/disconnect
        # are excluded: the answer never really arrived, so its text can't be
        # graded fairly. Judge failures are excluded from the denominator too.
        toolcall = disp.get("toolcall", 0)
        judged_ok = sum(quality.values())
        kept = judged_ok + toolcall
        good_acc = quality.get("good", 0) + quality.get("acceptable", 0) + toolcall
        metrics = {
            "judge_sampled_count": sampled,
            "judge_answered_count": disp.get("answered", 0),
            "judge_toolcall_count": toolcall,
            "judge_length_count": disp.get("length", 0),
            "judge_disconnect_count": disp.get("disconnect", 0),
            "judge_error_count": disp.get("error", 0),
            "judge_kept_count": kept,
            "judge_good_acc_rate": (good_acc / kept) if kept else None,
            "judge_poor_rate": (quality.get("poor", 0) / kept) if kept else None,
            "judge_halluc_clear_rate": (halluc.get("clear", 0) / kept) if kept else None,
            "judge_halluc_suspected_count": halluc.get("suspected", 0),
            "judge_failures": failures,
        }
        logger.info(
            "[%s] JUDGE done: sampled=%d kept=%d good+acc=%s poor=%s halluc_clear=%s "
            "suspected=%d failures=%d",
            self.name, sampled, kept,
            f"{metrics['judge_good_acc_rate']:.3f}" if metrics["judge_good_acc_rate"] is not None else "-",
            f"{metrics['judge_poor_rate']:.3f}" if metrics["judge_poor_rate"] is not None else "-",
            f"{metrics['judge_halluc_clear_rate']:.3f}" if metrics["judge_halluc_clear_rate"] is not None else "-",
            metrics["judge_halluc_suspected_count"], failures,
        )
        return metrics

    # ------------------------------------------------------------------
    # Aggregation & redline checks
    # ------------------------------------------------------------------

    def _aggregate(self, results: List[Dict], wall_time_s: float = 0.0) -> Dict[str, Any]:
        total = len(results)
        # Slots stay as empty dicts when the future was canceled before send()
        # ever ran (e.g. heartbeat-reaper or user cancellation while the
        # batch was still draining its queue). Counting those as "errors"
        # is misleading — the service never got the request. Split them out.
        attempted_results = [r for r in results if r]
        attempted = len(attempted_results)
        not_started = total - attempted

        successes = [r for r in attempted_results if r.get("success")]
        failures = [r for r in attempted_results if not r.get("success")]
        errors = len(failures)
        # Tool-call diagnostics: how many attempted requests carried tool messages,
        # and how many of those the target rejected with a 4xx (a strong signal the
        # endpoint can't render tool_calls/tool-role messages — not a service fault).
        tool_requests = sum(1 for r in attempted_results if r.get("had_tool_messages"))
        tool_call_4xx = sum(1 for r in attempted_results if r.get("tool_call_error"))
        # Image diagnostics: how many attempted requests carried a multimodal
        # image content part, and how many of THOSE 4xx'd (a strong signal the
        # target's model isn't vision-capable — a distinct cause that
        # tool_call_4xx_errors would otherwise absorb, since most image requests
        # in agent datasets also carry tool messages).
        image_requests = sum(1 for r in attempted_results if r.get("had_image_content"))
        image_4xx = sum(1 for r in attempted_results if r.get("image_call_error"))
        # Sanity counter: how many requests the service answered with a literal 200.
        http_200 = sum(1 for r in attempted_results if r.get("status_code") == 200)
        # System-message diagnostics: how many attempted requests carry the
        # multiple/out-of-order system-message shape, how many of THOSE 4xx'd (the
        # true cause that tool_call_4xx_errors was misattributing to tools), and how
        # many the `clean` toggle rewrote.
        multi_system_requests = sum(1 for r in attempted_results if r.get("multi_system"))
        multi_system_4xx = sum(
            1 for r in attempted_results
            if r.get("multi_system") and isinstance(r.get("status_code"), int)
            and 400 <= r["status_code"] < 500
        )
        system_normalized = sum(1 for r in attempted_results if r.get("system_normalized"))
        unfinished = sum(1 for r in successes if not r.get("response_finished"))
        # Uptime is the service's success rate over requests it actually saw.
        # Requests that never started (time budget ran out, batch canceled
        # before dispatch) aren't the service's fault and would otherwise drag
        # uptime down even on a perfectly healthy endpoint.
        uptime = len(successes) / attempted if attempted > 0 else 0.0

        # Surface what actually went wrong. Without this, a 10-hour run that
        # canceled mid-batch reports "797 errors" with zero clue what failed —
        # the never-started slots and real failures get bucketed together.
        # Cause-based failure classification, from the server's own error
        # message (see classify_http_error). Unlike the attribute counters
        # above (tool/image/multi-system = what the request CARRIED), these
        # say WHY it died — the counters that actually explain a failed run.
        error_class_counts = Counter(
            r.get("error_class") or "unknown" for r in failures
        )
        if failures:
            name = getattr(self, "name", "replay")
            logger.warning(
                "[%s] Failure breakdown (of %d attempted): %s",
                name, attempted, dict(error_class_counts),
            )
            # One line per cause with a real example, so a failed run explains
            # itself without pulling the response dump.
            examples: Dict[str, tuple] = {}
            for r in failures:
                cls = r.get("error_class") or "unknown"
                if cls not in examples:
                    examples[cls] = (
                        r.get("request_id"),
                        r.get("error_detail") or r.get("error") or "-",
                    )
            for cls, n in error_class_counts.most_common():
                rid, msg = examples[cls]
                logger.warning("[%s]   %s ×%d — e.g. request_id=%s: %s",
                               name, cls, n, rid, msg[:220])
            # Cause-specific operator guidance — only for causes that occurred.
            if error_class_counts.get("ctx_overflow"):
                logger.warning(
                    "[%s] %d requests exceeded the target's context window (prompt alone, or prompt "
                    "+ max_tokens / the server's default completion budget). Not a service outage — "
                    "pre-filter the dataset (convert_bodylog_dataset.py --clean --max-model-len "
                    "<target ctx>) or set max_generation_tokens.",
                    name, error_class_counts["ctx_overflow"],
                )
            if error_class_counts.get("image_load"):
                logger.warning(
                    "[%s] %d requests failed while the server loaded their image content — broken / "
                    "non-image data URIs or unfetchable image URLs in the dataset (convert --clean "
                    "drops these). NOT a vision-capability verdict: %d/%d image-carrying requests "
                    "were attempted overall; if the rest succeeded, vision works.",
                    name, error_class_counts["image_load"], image_requests, attempted,
                )
            if error_class_counts.get("tool_schema"):
                logger.warning(
                    "[%s] %d requests were rejected for an invalid tool JSON-schema (strict backends "
                    "validate tools; production gateways often don't). convert --clean repairs the "
                    "known shapes (parameters/properties: null).",
                    name, error_class_counts["tool_schema"],
                )
            if error_class_counts.get("multi_system_template"):
                if self.clean:
                    logger.warning(
                        "[%s] %d requests were still rejected for system-message placement DESPITE "
                        "clean=True — the target's template rejects a shape normalize_system_messages "
                        "doesn't cover; check an example error above.",
                        name, error_class_counts["multi_system_template"],
                    )
                else:
                    logger.warning(
                        "[%s] %d requests were rejected for multiple/out-of-order system messages "
                        "(strict chat template). Re-run with clean=True to normalize them "
                        "(%d/%d attempted requests carry that shape).",
                        name, error_class_counts["multi_system_template"],
                        multi_system_requests, attempted,
                    )
            if error_class_counts.get("request_validation"):
                logger.warning(
                    "[%s] %d requests failed the server's request validation (malformed roles / "
                    "content types the capture's gateway tolerated). convert --clean coerces the "
                    "known shapes; see the example above for what the server objected to.",
                    name, error_class_counts["request_validation"],
                )
        if not_started:
            logger.warning(
                "[%s] %d/%d requests never started (batch was canceled or budget ran out before they were dispatched)",
                getattr(self, "name", "replay"), not_started, total,
            )

        ttft_values = sorted(r["ttft_ms"] for r in successes if r.get("ttft_ms") is not None)
        total_time_values = sorted(r["total_ms"] for r in successes if r.get("total_ms") is not None)

        # Bucket TTFT by input length (prompt_tokens) per TTFT_INPUT_BUCKETS, and
        # emit p90/p50/avg + count for each bucket. Only requests whose input
        # length we actually know (prompt_tokens > 0) are bucketed; a successful
        # request with a TTFT but no usage block (prompt_tokens absent) can't be
        # placed by size, so it's tallied under ttft_input_unknown_count instead
        # of being dumped into the <6K bucket.
        ttft_bucket_values: Dict[str, list] = {label: [] for label, *_ in TTFT_INPUT_BUCKETS}
        ttft_input_unknown = 0
        for r in successes:
            t = r.get("ttft_ms")
            if t is None:
                continue
            pt = r.get("prompt_tokens") or 0
            if pt <= 0:
                ttft_input_unknown += 1
                continue
            for label, _human, lo, hi in TTFT_INPUT_BUCKETS:
                if lo <= pt < hi:
                    ttft_bucket_values[label].append(t)
                    break
        ttft_bucket_metrics: Dict[str, Any] = {"ttft_input_unknown_count": ttft_input_unknown}
        for label, *_ in TTFT_INPUT_BUCKETS:
            vals = sorted(ttft_bucket_values[label])
            ttft_bucket_metrics[f"ttft_{label}_p90_ms"] = percentile(vals, 0.90) if vals else None
            ttft_bucket_metrics[f"ttft_{label}_p50_ms"] = percentile(vals, 0.50) if vals else None
            ttft_bucket_metrics[f"ttft_{label}_avg_ms"] = sum(vals) / len(vals) if vals else None
            ttft_bucket_metrics[f"ttft_{label}_count"] = len(vals)

        finish_markers = [r.get("finish_marker") for r in successes if r.get("finish_marker")]
        finish_reasons = [r.get("finish_reason") for r in successes if r.get("finish_reason")]

        prompt_tokens_values = [r["prompt_tokens"] for r in successes if r.get("prompt_tokens")]
        completion_tokens_values = [r["completion_tokens"] for r in successes if r.get("completion_tokens")]
        output_tps_values = sorted(r["output_tps"] for r in successes if r.get("output_tps") is not None)
        input_tps_values = sorted(r["input_tps"] for r in successes if r.get("input_tps") is not None)
        tokens_after_finish_count = sum(r.get("tokens_after_finish", 0) for r in successes)
        repetitive_token_count = sum(1 for r in successes if r.get("repetitive_tokens"))
        # Requests the client had to stop because the server overran the injected
        # max_tokens (see max_generation_tokens). A non-zero count means the cap
        # is doing its job AND the target tends to run away on some prompts.
        generation_capped_count = sum(1 for r in successes if r.get("generation_capped"))

        # Aggregate token totals across all successful requests. If the service
        # did not report `cached_tokens` in any usage block, total_cached_tokens
        # stays at 0 and total_input_tokens is the raw prompt_tokens sum.
        total_input_tokens = sum(r.get("prompt_tokens", 0) for r in successes)
        total_output_tokens = sum(r.get("completion_tokens", 0) for r in successes)
        total_cached_tokens = sum(r.get("cached_tokens", 0) for r in successes)
        # Cached tokens are a SUBSET of prompt_tokens (the cache-hit portion), so
        # total_input_tokens already includes them. For cost models that price
        # cache reads separately, the fresh/full-price portion is input minus
        # cached — exposed here so callers don't double-count cached tokens at
        # both the full input rate AND the cache-read rate. Clamp at 0 in case a
        # server misreports cached > prompt_tokens.
        total_uncached_input_tokens = max(0, total_input_tokens - total_cached_tokens)

        unfinished_rate = unfinished / attempted if attempted > 0 else 0.0

        # Throughput per minute over the full wall-clock window.
        # If the caller didn't pass a positive wall_time (legacy callers or
        # unexpected clock behaviour), fall back to the slowest successful
        # request's total_ms as a proxy for wall time so TPM is still useful.
        effective_wall_s = wall_time_s
        if effective_wall_s <= 0 and total_time_values:
            effective_wall_s = max(total_time_values) / 1000.0
            logger.warning(
                "[%s] wall_time_s was %.3f; falling back to max(total_ms)=%.3fs for TPM",
                getattr(self, 'name', 'replay'), wall_time_s, effective_wall_s,
            )
        wall_minutes = effective_wall_s / 60.0 if effective_wall_s > 0 else 0.0
        input_tpm = total_input_tokens / wall_minutes if wall_minutes > 0 else None
        output_tpm = total_output_tokens / wall_minutes if wall_minutes > 0 else None
        cached_tpm = total_cached_tokens / wall_minutes if wall_minutes > 0 else None
        # Fresh (full-price) input throughput: input_tpm minus the cache-hit
        # portion. Use this — NOT input_tpm — as the coefficient for the full
        # input price in a cost model, with cached_tpm priced at the cache-read
        # rate. (input_tpm == uncached_input_tpm + cached_tpm.)
        uncached_input_tpm = total_uncached_input_tokens / wall_minutes if wall_minutes > 0 else None

        return {
            "total_requests": total,
            "attempted_requests": attempted,
            "not_started_requests": not_started,
            "successful_requests": len(successes),
            "error_requests": errors,
            # Rate of failures among requests that actually ran. Computing this
            # over `total` would conflate "never dispatched" with "service
            # errored" — the original bug we're fixing.
            "error_rate": errors / attempted if attempted > 0 else 0.0,
            "unfinished_requests": unfinished,
            "unfinished_rate": unfinished_rate,
            "uptime": uptime,
            "http_200_count": http_200,
            "tool_requests_total": tool_requests,
            "tool_call_4xx_errors": tool_call_4xx,
            "image_requests_total": image_requests,
            "image_4xx_errors": image_4xx,
            "multi_system_requests": multi_system_requests,
            "multi_system_4xx_errors": multi_system_4xx,
            "system_normalized_count": system_normalized,
            # WHY failures failed, keyed by cause class from the server's own
            # error message (ctx_overflow / image_load / tool_schema /
            # multi_system_template / request_validation / rate_limited /
            # server_error / timeout / connection / …). The attribute counters
            # above say what failed requests carried; this says what killed them.
            "error_class_counts": dict(error_class_counts),
            "ttft_p50_ms": percentile(ttft_values, 0.50) if ttft_values else None,
            "ttft_p90_ms": percentile(ttft_values, 0.90) if ttft_values else None,
            "ttft_p99_ms": percentile(ttft_values, 0.99) if ttft_values else None,
            "ttft_mean_ms": sum(ttft_values) / len(ttft_values) if ttft_values else None,
            # Per-input-length TTFT buckets (p90/p50/avg/count each) — see
            # TTFT_INPUT_BUCKETS. Spread last so the keys sit alongside the
            # overall ttft_* metrics.
            **ttft_bucket_metrics,
            "total_time_p50_ms": percentile(total_time_values, 0.50) if total_time_values else None,
            "total_time_p99_ms": percentile(total_time_values, 0.99) if total_time_values else None,
            "avg_prompt_tokens": sum(prompt_tokens_values) / len(prompt_tokens_values) if prompt_tokens_values else None,
            "avg_completion_tokens": sum(completion_tokens_values) / len(completion_tokens_values) if completion_tokens_values else None,
            "output_tps_avg": sum(output_tps_values) / len(output_tps_values) if output_tps_values else None,
            "output_tps_mean": sum(output_tps_values) / len(output_tps_values) if output_tps_values else None,
            "output_tps_p10": percentile(output_tps_values, 0.10) if output_tps_values else None,
            "output_tps_p50": percentile(output_tps_values, 0.50) if output_tps_values else None,
            "output_tps_p90": percentile(output_tps_values, 0.90) if output_tps_values else None,
            "input_tps_mean": sum(input_tps_values) / len(input_tps_values) if input_tps_values else None,
            "input_tps_p10": percentile(input_tps_values, 0.10) if input_tps_values else None,
            "input_tps_p50": percentile(input_tps_values, 0.50) if input_tps_values else None,
            "input_tps_p90": percentile(input_tps_values, 0.90) if input_tps_values else None,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cached_tokens": total_cached_tokens,
            "total_uncached_input_tokens": total_uncached_input_tokens,
            # Token-weighted cache hit rate in [0,1] (cached ⊆ input). None when
            # no input tokens were counted (nothing to divide).
            "cache_hit_rate": (total_cached_tokens / total_input_tokens) if total_input_tokens > 0 else None,
            "wall_time_s": effective_wall_s if effective_wall_s > 0 else None,
            "input_tpm": input_tpm,
            "output_tpm": output_tpm,
            "cached_tpm": cached_tpm,
            "uncached_input_tpm": uncached_input_tpm,
            "tokens_after_finish_count": tokens_after_finish_count,
            "repetitive_token_count": repetitive_token_count,
            "generation_capped_count": generation_capped_count,
            "finish_reason_length_count": finish_reasons.count("length"),
            "finish_reason_stop_count": finish_reasons.count("stop"),
            "finish_marker_counts": {
                m: finish_markers.count(m) for m in set(finish_markers)
            },
        }

    def _check_redlines(self, metrics: Dict[str, Any]) -> bool:
        violations = []

        uptime = metrics.get("uptime", 0.0)
        if uptime < self.uptime_floor:
            violations.append(f"uptime={uptime:.2%} < {self.uptime_floor:.2%}")

        ttft_p99 = metrics.get("ttft_p99_ms")
        if ttft_p99 is not None and ttft_p99 > self.ttft_p99_redline_ms:
            violations.append(f"ttft_p99={ttft_p99:.0f}ms > {self.ttft_p99_redline_ms:.0f}ms")

        total_p99 = metrics.get("total_time_p99_ms")
        if total_p99 is not None and total_p99 > self.total_time_p99_redline_ms:
            violations.append(
                f"total_time_p99={total_p99:.0f}ms > {self.total_time_p99_redline_ms:.0f}ms"
            )

        if violations:
            logger.warning("[%s] Redline violations: %s", self.name, violations)
            return False
        return True
