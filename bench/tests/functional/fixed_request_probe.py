"""
Fixed-request probe — replay ONE captured request many times.

Unlike replay (many distinct requests, one per JSONL line), the
hallucination and tool_call_success modules send a *single* fixed request
repeatedly to measure output-consistency properties:

  - Hallucination: at single concurrency, fire the same prompt N times; a
    separate (configurable) judge LLM decides how many replies are
    completely off-topic / incoherent ("hallucinated").
  - Tool-call success: fire the same tool-enabled prompt N times and check
    how often the model emits a valid tool call (known tool name + parseable
    JSON arguments).

A capture file is either a gateway proxy-log record (the request body as a
JSON string under `req_body`) or a plain chat request; the ones shipped in
bench/examples/ are the latter. Proxy-log captures are
captures: the OpenAI chat-completions request lives as a JSON *string* under
the top-level `req_body` key. We parse that directly rather than converting
to the replay JSONL schema — a single request gains nothing from the
one-request-per-line format and the capture also carries useful reference
fields (resp_meta, reasoning) we would otherwise drop.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any, Callable, Dict, List, Optional

import requests

from bench.replay_test.log_replay_tool import normalize_system_messages
from utils.api import join_endpoint
from utils.logger import logger

# Absolute ceiling on how many streamed SSE events (~1 per token) one response
# may buffer in memory. The read below accumulates every event into a list before
# processing, so a non-terminating / runaway generation would otherwise grow it
# without bound and OOM the worker — the same failure that took down replay (see
# REPLAY_MAX_SSE_EVENTS). This path backs the `hallucination` and
# `tool_call_success` modules, so the vector is live. Sized well above any
# legitimate response; 0 disables (unbounded — not recommended).
PROBE_MAX_SSE_EVENTS = int(os.getenv("PROBE_MAX_SSE_EVENTS", "131072"))  # ~128K tokens


ProgressCallback = Callable[[float, str], None]


def _noop_progress(fraction: float, message: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Capture loading
# ---------------------------------------------------------------------------

def load_capture_request(path: str) -> Dict[str, Any]:
    """Parse the OpenAI chat-completions request from a proxy-log capture JSON.

    The capture stores the raw request body as a JSON string under `req_body`.
    Falls back to treating the file itself as the request when it already
    looks like a chat request (has a `messages` array).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("req_body"), str):
        return json.loads(data["req_body"])
    if isinstance(data, dict) and "messages" in data:
        return data
    raise ValueError(f"Cannot extract a chat request from capture file: {path}")


def last_user_text(req: Dict[str, Any]) -> str:
    """Return the text of the last user message (handles list-of-parts content)."""
    for m in reversed(req.get("messages", []) or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            parts = [p.get("text", "") for p in c if isinstance(p, dict)]
            return "\n".join(p for p in parts if p)
        return c or ""
    return ""


def allowed_tool_names(req: Dict[str, Any]) -> List[str]:
    """Tool/function names declared in the request (OpenAI tools schema)."""
    names: List[str] = []
    for t in req.get("tools", []) or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(fn["name"])
    return names


def build_payload(
    req: Dict[str, Any],
    model: str,
    max_tokens: int,
    clean: bool = False,
) -> Dict[str, Any]:
    """Replay the captured request as-is, overriding ONLY the two fields that
    must change: `model` (route to the endpoint under test) and `max_tokens`
    (operator-configurable). Every other field — messages, tools, tool_choice,
    thinking, temperature, user, etc. — is preserved byte-for-byte so the
    bug-triggering request stays as faithful as possible.

    The captured requests already carry `stream: true` and
    `stream_options.include_usage: true`, so we don't touch them. We only
    backfill those when a future capture lacks them — purely so the response
    is parseable and token usage is reported; neither alters model behaviour.

    `clean` (default off) is the only behaviour-affecting exception: it
    normalizes the messages so a strict chat template (Qwen/SGLang) accepts
    them — captured gateway / Claude-Code traffic fans a multi-block system
    prompt out into several `system` messages, which a bare engine rejects with
    "System message must be at the beginning." (4xx). Off by default keeps the
    replay byte-for-byte; turn it on when the target rejects multi-system
    requests. See normalize_system_messages.
    """
    payload = dict(req)
    payload["model"] = model
    payload["max_tokens"] = max_tokens
    if clean:
        new_msgs, changed = normalize_system_messages(payload.get("messages"))
        if changed:
            payload["messages"] = new_msgs
    if "stream" not in payload:
        payload["stream"] = True
    if payload.get("stream"):
        opts = dict(payload.get("stream_options") or {})
        opts.setdefault("include_usage", True)
        payload["stream_options"] = opts
    return payload


def _count_system(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "system")


def _log_clean_effect(tag: str, req: Dict[str, Any], payload: Dict[str, Any], clean: bool) -> None:
    """One-line INFO describing whether `clean` rewrote this fixed request. These
    modules replay a single request N times, so it either changed it or it didn't."""
    if not clean:
        return
    before, after = _count_system(req.get("messages")), _count_system(payload.get("messages"))
    if before != after:
        logger.info("[%s] clean=on: normalized system messages (%d -> %d) so a strict "
                    "Qwen/SGLang template accepts the request.", tag, before, after)
    else:
        logger.info("[%s] clean=on: request already template-clean (no system-message "
                    "rewrite needed).", tag)


# ---------------------------------------------------------------------------
# Chat send + streaming parse
# ---------------------------------------------------------------------------

@dataclass
class ChatOutcome:
    ok: bool = False
    status: Optional[int] = None
    content: str = ""
    reasoning: str = ""
    tool_calls: List[Dict[str, str]] = field(default_factory=list)  # [{name, arguments}]
    finish_reason: Optional[str] = None
    completion_tokens: int = 0
    error: Optional[str] = None


def send_chat(
    session: requests.Session,
    url: str,
    payload: Dict[str, Any],
    timeout: float,
) -> ChatOutcome:
    """POST one streaming chat request and accumulate content / reasoning /
    tool_calls. Returns a ChatOutcome (ok=False with .error on failure)."""
    out = ChatOutcome()
    tool_acc: Dict[int, Dict[str, str]] = {}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        with session.post(url, data=body, timeout=timeout, stream=True) as resp:
            out.status = resp.status_code
            resp.raise_for_status()

            buffer = b""
            events: List[dict] = []
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    s = line.decode("utf-8", errors="replace").strip()
                    if not s.startswith("data:"):
                        continue
                    s = s[5:].strip()
                    if s == "[DONE]":
                        continue
                    try:
                        events.append(json.loads(s))
                    except json.JSONDecodeError:
                        continue
                # Backstop: stop reading a runaway generation rather than buffer
                # it without bound. Closing the `with` block aborts the request
                # server-side too.
                if PROBE_MAX_SSE_EVENTS and len(events) >= PROBE_MAX_SSE_EVENTS:
                    break
            # Non-streaming fallback: whole body is one JSON object
            if not events and buffer.strip():
                try:
                    events.append(json.loads(buffer.decode("utf-8", errors="replace")))
                except json.JSONDecodeError:
                    pass

            for data in events:
                usage = data.get("usage")
                if usage and usage.get("completion_tokens"):
                    out.completion_tokens = usage["completion_tokens"]
                for choice in data.get("choices", []):
                    # streaming deltas and non-streaming message share most keys
                    delta = choice.get("delta") or choice.get("message") or {}
                    piece = delta.get("content")
                    if piece:
                        out.content += piece
                    rpiece = delta.get("reasoning") or delta.get("reasoning_content")
                    if rpiece:
                        out.reasoning += rpiece
                    for tc in delta.get("tool_calls", []) or []:
                        idx = tc.get("index", 0) or 0
                        slot = tool_acc.setdefault(idx, {"name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = slot["name"] or fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        out.finish_reason = choice["finish_reason"]

            out.tool_calls = [tool_acc[i] for i in sorted(tool_acc)]
            out.ok = True
    except Exception as exc:  # noqa: BLE001 — record any transport/HTTP error
        out.error = f"{type(exc).__name__}: {exc}"
        out.ok = False
    return out


def _fan_out(
    n: int,
    concurrency: int,
    fn: Callable[[int], Any],
    progress_cb: ProgressCallback,
    cancel_event: Optional[Event],
    lo: float,
    hi: float,
    label: str,
    msg_extra: Optional[Callable[[], str]] = None,
    on_result: Optional[Callable[[int, Any], None]] = None,
    log_label: Optional[str] = None,
) -> List[Any]:
    """Run fn(i) for i in [0, n) across a thread pool, reporting progress in
    the [lo, hi] band. Slots cancelled before start come back as None.

    Progress is emitted on the FIRST completion, then ~every 1%, then on the
    final item — so a long single-concurrency run shows life immediately (the
    first item confirms the service responded) and keeps moving. `msg_extra`,
    if given, returns a suffix appended to each progress message (e.g. a running
    hallucination count). `on_result(i, result)`, if given, is called on the main
    thread as each item completes — used to launch downstream work (e.g.
    judging) while the rest of the batch is still running. `log_label`, if given,
    also emits each progress point to the logger at INFO (operator-visible in
    pod logs, independent of the SSE bar)."""
    results: List[Any] = [None] * n
    done = 0
    step = max(1, n // 100)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = {ex.submit(_guarded, fn, i, cancel_event): i for i in range(n)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()
            if on_result is not None:
                on_result(i, results[i])
            done += 1
            if done == 1 or done % step == 0 or done == n:
                frac = lo + (hi - lo) * (done / n)
                suffix = f" {msg_extra()}" if msg_extra else ""
                msg = f"{label}: {done}/{n}{suffix}"
                progress_cb(frac, msg)
                if log_label:
                    logger.info("%s %s (%.0f%%)", log_label, msg, 100.0 * done / n)
    return results


def _guarded(fn: Callable[[int], Any], i: int, cancel_event: Optional[Event]) -> Any:
    if cancel_event is not None and cancel_event.is_set():
        return None
    return fn(i)


# ---------------------------------------------------------------------------
# Hallucination probe
# ---------------------------------------------------------------------------

_DEFAULT_JUDGE_PROMPT = (
    "You are grading whether an AI assistant's reply is HALLUCINATED. Here, "
    "\"hallucinated\" means ONLY this: the text that is present is internally "
    "incoherent, garbled, nonsensical, or clearly about a DIFFERENT subject "
    "than the task/domain described below. Nothing else counts as a "
    "hallucination.\n\n"
    "IMPORTANT — the assistant was working from a long prior conversation and "
    "codebase that you CANNOT fully see. The task context below summarises it. "
    "References to specific functions, files, variables, code, data, or domain "
    "details ARE EXPECTED and grounded in that hidden context — do NOT treat "
    "specific or unfamiliar subject matter as \"invented\".\n\n"
    "The reply was produced under a SMALL token budget, so read it leniently. "
    "About THIS reply:\n{reply_meta}\n\n"
    "The following are NOT hallucinations — for each of these, answer false:\n"
    "  1. REASONING / THINKING / PLANNING. A reasoning trace or meta-commentary "
    "about how the assistant intends to do the task (\"I should create a "
    "structured report…\", \"let me check the project structure…\"), even if it "
    "never reaches a final answer. Judge only whether the thinking itself is "
    "coherent and on-topic.\n"
    "  2. TRUNCATION by the token limit. Ending abruptly mid-sentence, a "
    "half-finished table or list, or a broken / garbled FINAL character or two "
    "(e.g. a stray \"�\" or a dangling tag at the very end). Judge only the "
    "coherence of the text that IS present — never penalise incompleteness.\n"
    "  3. RE-DOING / RESTATING the task. Planning, repeating, or re-generating "
    "something the context suggests was already done. The assistant is "
    "responding to the final instruction below; doing what that instruction "
    "asks is on-topic by definition, never a hallucination.\n"
    "  4. STYLE / LANGUAGE. Replying in a different language than the user "
    "(e.g. English when the user wrote Chinese), or being terse or low-effort. "
    "Style and effort are not hallucination.\n\n"
    "Task context (summary of the conversation):\n\"\"\"\n{context}\n\"\"\"\n\n"
    "The user's final instruction:\n\"\"\"\n{instruction}\n\"\"\"\n\n"
    "The assistant's reply:\n\"\"\"\n{response}\n\"\"\"\n\n"
    "Reply with a single JSON object: {{\"hallucination\": true|false, \"reason\": \"<short>\"}}.\n"
    "Mark hallucination=true ONLY if the text that is present is genuinely "
    "incoherent / garbled / nonsensical, or clearly about a different subject "
    "than the task context above — after setting aside reasoning, truncation, "
    "re-doing, and style per the rules above. When in doubt, answer false."
)

# Asks a model to summarise the conversation so the judge knows the task/domain.
_SUMMARY_PROMPT = (
    "Below is a transcript of a conversation between a user and an AI assistant "
    "(it may include tool calls/results and may be trimmed). In 3-6 sentences, "
    "summarise what the user is working on — the domain, codebase, and any "
    "specific files or functions involved — and what the MOST RECENT user "
    "message asks the assistant to produce. Describe ONLY the subject matter and "
    "what is being asked. Do NOT state or imply whether any task has already "
    "been completed, nor judge the conversation's progress — a grader will use "
    "this only to know the domain and the request, not to check whether work is "
    "done. This summary will be shown to a grader who cannot see the transcript. "
    "Output only the summary.\n\n"
    "TRANSCRIPT:\n\"\"\"\n{transcript}\n\"\"\""
)

_NO_CONTEXT_NOTE = (
    "(No summary available. The assistant was working from a long prior "
    "conversation and codebase you cannot see, so do not treat specific or "
    "unfamiliar subject matter as invented/hallucinated.)"
)

_JSON_OBJ_RE = re.compile(r"\{[^{}]*\"hallucination\"[^{}]*\}", re.DOTALL)
_HALLUC_RE = re.compile(r"\"?hallucination\"?\s*[:=]\s*\"?(true|false|yes|no|1|0)", re.IGNORECASE)


def _clean_answer(text: str) -> str:
    """Strip a trailing run of U+FFFD replacement chars before judging.

    When max_tokens cuts the response mid-character, the model's final token is
    a partial multibyte UTF-8 sequence that our streaming decode renders as
    � ("�"). That is a decode/truncation artifact, not model output — but a
    judge reads the dangling "�" as "garbled". Drop it (with surrounding
    whitespace) so only real text is graded."""
    return text.rstrip("� \t\r\n")


def _reply_meta(is_reasoning: bool, finish_reason: Optional[str]) -> str:
    """One-line description of what the graded text actually is, so the judge
    applies the reasoning/truncation rules instead of treating an unfinished
    thinking trace as a hallucination."""
    if is_reasoning:
        bits = ["This text is the assistant's INTERNAL REASONING / thinking "
                "trace, not a finished final answer (the final answer was empty "
                "under the small token budget)."]
    else:
        bits = ["This text is the assistant's answer content."]
    if finish_reason == "length":
        bits.append("It was CUT OFF by the max_tokens limit, so it is EXPECTED "
                    "to end abruptly — mid-sentence, mid-table, or mid-character.")
    return " ".join(bits)


def _coerce_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return None


def _parse_judge_verdict(text: str) -> Optional[bool]:
    """Extract a hallucination verdict from a judge reply. None if unparseable.

    Tries, in order: a JSON object containing "hallucination", then a loose
    regex on the raw text. Robust to thinking models that wrap the verdict in
    prose or reasoning."""
    if not text:
        return None
    for m in _JSON_OBJ_RE.finditer(text):
        try:
            verdict = _coerce_bool(json.loads(m.group(0)).get("hallucination"))
        except json.JSONDecodeError:
            verdict = None
        if verdict is not None:
            return verdict
    m = _HALLUC_RE.search(text)
    if m:
        return _coerce_bool(m.group(1))
    return None


def _disable_thinking_fields(model: str) -> Dict[str, Any]:
    """chat_template_kwargs that turn thinking OFF. Used for our own judge
    requests so the verdict lands in `content` rather than being consumed by
    reasoning tokens.

    Passed via `chat_template_kwargs` (NOT a top-level `thinking` param) because
    bare vLLM/SGLang only honour the chat-template kwargs, not the gateway
    top-level `thinking={type:disabled}` switch. Model-aware, because the old
    "send BOTH spellings, an unrecognised one is harmless" assumption is FALSE
    on Kimi-K3: there "enable_thinking" only half-disables (reasoning shrinks
    but stays non-empty) and can shadow the real "thinking" switch when both
    are sent — which matters here, where judge budgets are tight (128-256
    tokens) and any surviving reasoning starves the verdict. Only for unknown
    families are both spellings still sent, where an unrecognised kwarg really
    is just an unused template variable (cf. functional_acceptance
    FunctionalAcceptanceTest._thinking_fields, which encodes the same rule).
    """
    low = (model or "").lower()
    if "kimi" in low:
        return {"chat_template_kwargs": {"thinking": False}}
    if "glm" in low or "qwen" in low:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"chat_template_kwargs": {"thinking": False, "enable_thinking": False}}


@dataclass
class HallucinationProbe:
    api_url: str
    model: str
    api_key: str
    request: Dict[str, Any]
    num_requests: int = 1000
    concurrency: int = 1
    max_tokens: int = 256
    request_timeout: float = 120.0
    clean: bool = False
    judge_api_url: str = ""
    judge_model: str = ""
    judge_api_key: str = ""
    judge_max_tokens: int = 128
    judge_concurrency: int = 8
    judge_prompt: str = ""
    judge_disable_thinking: bool = True
    summarize_context: bool = True
    progress_cb: ProgressCallback = _noop_progress
    cancel_event: Optional[Event] = None

    def _session(self, api_key: str) -> requests.Session:
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        if api_key:
            s.headers.update({"Authorization": f"Bearer {api_key}"})
        return s

    def _build_transcript(self, max_chars: int = 80000) -> str:
        """Flatten the captured conversation into a plain text transcript for
        summarisation. Per-message content is truncated; if the whole thing is
        too long we keep the head (task framing) plus a larger tail (recent
        context, which is closest to the final instruction)."""
        parts: List[str] = []
        for m in self.request.get("messages", []) or []:
            role = (m.get("role") or "?").upper()
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            c = (c or "").strip()
            tcs = m.get("tool_calls")
            if tcs:
                names = ",".join((t.get("function", {}) or {}).get("name", "?") for t in tcs)
                c = (c + f" [tool_calls: {names}]").strip()
            if c:
                parts.append(f"{role}: {c[:2000]}")
        text = "\n".join(parts)
        if len(text) > max_chars:
            head = text[: max_chars // 4]
            tail = text[-(max_chars - len(head)):]
            text = head + "\n…[transcript trimmed]…\n" + tail
        return text

    def _make_context(self, judge_sess, judge_url, j_model, judge_extra) -> str:
        """One-time summary of the request so the judge knows the task/domain.
        Returns '' on failure (judge then falls back to a conservative note)."""
        self.progress_cb(0.05, "Summarising request context for the judge…")
        payload = {
            "model": j_model,
            "messages": [{"role": "user", "content": _SUMMARY_PROMPT.format(transcript=self._build_transcript())}],
            "max_tokens": 512,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
            **judge_extra,
        }
        res = send_chat(judge_sess, judge_url, payload, self.request_timeout)
        summary = (res.content or res.reasoning).strip() if res.ok else ""
        if summary:
            logger.info("[hallucination] request context summary: %s", summary[:600])
        else:
            logger.warning("[hallucination] context summary failed (status=%s error=%s) — "
                           "judging without it", res.status, res.error)
        return summary

    def run(self) -> Dict[str, Any]:
        instruction = last_user_text(self.request)
        payload = build_payload(self.request, self.model, self.max_tokens, clean=self.clean)
        _log_clean_effect("hallucination", self.request, payload, self.clean)
        url = join_endpoint(self.api_url, "chat/completions")
        sess = self._session(self.api_key)

        # Judge setup — fall back to the target endpoint if no judge configured.
        j_url = self.judge_api_url or self.api_url
        j_model = self.judge_model or self.model
        j_key = self.judge_api_key or (self.api_key if not self.judge_api_url else "")
        if not self.judge_api_url:
            logger.warning("[hallucination] no judge endpoint configured — "
                           "judging with the target model itself (%s).", j_model)
        judge_url = join_endpoint(j_url, "chat/completions")
        judge_sess = self._session(j_key)
        prompt_tmpl = self.judge_prompt or _DEFAULT_JUDGE_PROMPT
        # Disable thinking on our own judge requests so the verdict JSON lands
        # in `content` instead of being eaten by reasoning tokens.
        judge_extra = _disable_thinking_fields(j_model) if self.judge_disable_thinking else {}

        # Summarise the request once so the judge knows the task/domain and does
        # not mistake the model's grounded references (functions, files, code it
        # saw in the hidden conversation) for "invented" hallucinated content.
        context = self._make_context(judge_sess, judge_url, j_model, judge_extra) if self.summarize_context else ""
        context = context or _NO_CONTEXT_NOTE

        # The probed text is the model's answer. For a thinking model with a
        # small max_tokens the visible `content` is often empty (the whole
        # budget was spent on reasoning_content) — fall back to the reasoning
        # so those replies are still judged instead of dropped as "empty". A
        # reply only counts as empty when BOTH fields are blank.
        def _answer(o: ChatOutcome) -> str:
            return o.content.strip() or o.reasoning.strip()

        # Hallucinations are rare, so log each one as it is found (thread-safe
        # counter for a stable running index) and surface a live count on the
        # progress bar.
        halluc_lock = Lock()
        halluc_seen = [0]

        def _judge_text(idx: int, answer: str, is_reasoning: bool,
                        finish_reason: Optional[str]) -> Optional[bool]:
            answer = _clean_answer(answer)
            jpayload = {
                "model": j_model,
                "messages": [{
                    "role": "user",
                    "content": prompt_tmpl.format(
                        context=context[:4000],
                        instruction=instruction[:4000],
                        response=answer[:6000],
                        reply_meta=_reply_meta(is_reasoning, finish_reason),
                    ),
                }],
                "max_tokens": self.judge_max_tokens,
                "temperature": 0.0,
                "stream": True,
                "stream_options": {"include_usage": True},
                **judge_extra,
            }
            res = send_chat(judge_sess, judge_url, jpayload, self.request_timeout)
            if not res.ok:
                logger.warning("[hallucination] judge call failed (req #%d): status=%s error=%s",
                               idx, res.status, res.error)
                return None
            verdict = _parse_judge_verdict(res.content)
            if verdict is None:
                verdict = _parse_judge_verdict(res.reasoning)
            if verdict is None:
                logger.warning("[hallucination] judge verdict unparseable (req #%d): status=%s "
                               "content[%d]=%r reasoning[%d]=%r",
                               idx, res.status, len(res.content), res.content[:200],
                               len(res.reasoning), res.reasoning[:200])
            elif verdict is True:
                with halluc_lock:
                    halluc_seen[0] += 1
                    n = halluc_seen[0]
                logger.warning("[hallucination] ⚠ HALLUCINATION #%d (req #%d): answer=%r | judge=%r",
                               n, idx, answer[:400], (res.content or res.reasoning)[:400])
            return verdict

        # Pipeline: fire a judge call the moment each probe reply arrives, so
        # judging (concurrent, fast) overlaps the single-concurrency, slow probe
        # phase instead of waiting for the whole batch to finish first.
        judge_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.judge_concurrency))
        judge_futures: Dict[int, "concurrent.futures.Future"] = {}

        logged_first = [False]

        def _on_probe(i: int, o: Optional[ChatOutcome]) -> None:
            # Liveness check: log the very first response we get back, with its
            # status and field lengths, so the operator can confirm the service
            # is answering sanely before the long batch grinds on.
            if o is not None and not logged_first[0]:
                logged_first[0] = True
                logger.info("[hallucination] first response: ok=%s status=%s content[%d] reasoning[%d]%s",
                            o.ok, o.status, len(o.content), len(o.reasoning),
                            f" error={o.error}" if o.error else "")
            if o is None or not o.ok:
                return
            ans = _answer(o)
            if ans:
                is_reasoning = not o.content.strip()
                judge_futures[i] = judge_pool.submit(
                    _judge_text, i, ans, is_reasoning, o.finish_reason)

        logger.info("[hallucination] probing %d requests @ concurrency=%d, max_tokens=%d "
                    "(judge=%s); progress logged ~every 1%%",
                    self.num_requests, self.concurrency, self.max_tokens, j_model)
        self.progress_cb(0.08, f"Probing {self.num_requests} reqs @ concurrency={self.concurrency}; "
                              f"judging in parallel with {j_model}")
        try:
            outcomes: List[Optional[ChatOutcome]] = _fan_out(
                self.num_requests, self.concurrency,
                lambda i: send_chat(sess, url, payload, self.request_timeout),
                self.progress_cb, self.cancel_event, 0.10, 0.90, "probe",
                msg_extra=lambda: f"({halluc_seen[0]} hallucinated)",
                on_result=_on_probe,
                log_label="[hallucination]",
            )
            # Drain the judge tail (most verdicts already arrived during probing).
            self.progress_cb(0.92, f"Finalising {len(judge_futures)} judge verdicts")
            verdicts = [judge_futures[i].result() for i in sorted(judge_futures)]
        finally:
            judge_pool.shutdown(wait=True)

        ok_outcomes = [o for o in outcomes if o is not None and o.ok]
        completed = len(ok_outcomes)
        error = sum(1 for o in outcomes if o is not None and not o.ok)
        comp_tokens = [o.completion_tokens for o in ok_outcomes]
        empty = completed - len(judge_futures)
        reasoning_only = sum(1 for o in ok_outcomes if not o.content.strip() and o.reasoning.strip())

        halluc = sum(1 for v in verdicts if v is True)
        judged = sum(1 for v in verdicts if v is not None)
        judge_err = sum(1 for v in verdicts if v is None)
        halluc_rate = (halluc / judged) if judged else 0.0
        avg_ct = (sum(comp_tokens) / len(comp_tokens)) if comp_tokens else 0.0

        return {
            "non_hallucination_rate": 1.0 - halluc_rate,
            "hallucination_rate": halluc_rate,
            "hallucination_count": halluc,
            "judged_requests": judged,
            "judge_error_count": judge_err,
            "total_requests": self.num_requests,
            "completed_requests": completed,
            "error_requests": error,
            "empty_response_count": empty,
            "reasoning_only_count": reasoning_only,
            "avg_completion_tokens": avg_ct,
        }


# ---------------------------------------------------------------------------
# Tool-call success probe
# ---------------------------------------------------------------------------

@dataclass
class ToolCallProbe:
    api_url: str
    model: str
    api_key: str
    request: Dict[str, Any]
    num_requests: int = 100
    concurrency: int = 5
    max_tokens: int = 2048
    request_timeout: float = 120.0
    clean: bool = False
    progress_cb: ProgressCallback = _noop_progress
    cancel_event: Optional[Event] = None

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        if self.api_key:
            s.headers.update({"Authorization": f"Bearer {self.api_key}"})
        return s

    @staticmethod
    def _valid_call(tc: Dict[str, str], allowed: List[str]) -> bool:
        name = (tc.get("name") or "").strip()
        if not name:
            return False
        if allowed and name not in allowed:
            return False
        args = tc.get("arguments", "")
        if args and args.strip():
            # strict=False tolerates raw control characters (newlines, tabs)
            # inside JSON string values — very common in shell/commit-message
            # arguments and accepted by real tool-call runtimes. Genuinely
            # broken JSON (e.g. unescaped closing quotes) still fails.
            try:
                json.loads(args, strict=False)
            except json.JSONDecodeError:
                return False
        return True

    def run(self) -> Dict[str, Any]:
        allowed = allowed_tool_names(self.request)
        payload = build_payload(self.request, self.model, self.max_tokens, clean=self.clean)
        _log_clean_effect("tool_call_success", self.request, payload, self.clean)
        url = join_endpoint(self.api_url, "chat/completions")
        sess = self._session()

        self.progress_cb(0.10, f"Sending {self.num_requests} requests @ concurrency={self.concurrency}")
        outcomes: List[Optional[ChatOutcome]] = _fan_out(
            self.num_requests, self.concurrency,
            lambda i: send_chat(sess, url, payload, self.request_timeout),
            self.progress_cb, self.cancel_event, 0.10, 0.98, "tool-probe",
        )

        completed = error = with_tool = valid = invalid = no_tool = truncated = 0
        comp_tokens: List[int] = []
        # Tool-call failures are expected to be rare and each one is valuable for
        # debugging, so log every failure in full: the offending tool name/args
        # for an invalid (wrong) call, or the text/reasoning/finish_reason for a
        # missing one. A finish_reason=="length" means max_tokens truncated the
        # response before the tool call surfaced (common with thinking models that
        # run out of budget mid-reasoning) — flag it so a low score isn't mistaken
        # for the model declining to call the tool. A per-category cap still
        # guards against a wholly broken endpoint (e.g. a model that never calls
        # tools) flooding the log with up to num_requests lines.
        fail_log_cap = 50
        invalid_logged = no_tool_logged = error_logged = 0
        for i, o in enumerate(outcomes):
            if o is None:
                continue
            if not o.ok:
                error += 1
                # Transport/HTTP error — surface the exception + status so a flaky
                # endpoint or auth/4xx failure is visible, not just counted.
                error_logged += 1
                if error_logged <= fail_log_cap:
                    logger.warning(
                        "[tool_call_success] request error (req #%d): status=%s error=%s",
                        i, o.status, o.error,
                    )
                elif error_logged == fail_log_cap + 1:
                    logger.warning("[tool_call_success] …further request errors "
                                   "suppressed (logged first %d).", fail_log_cap)
                continue
            completed += 1
            comp_tokens.append(o.completion_tokens)
            cut = o.finish_reason == "length"
            if cut:
                truncated += 1
            trunc_note = " TRUNCATED(max_tokens?)" if cut else ""
            if o.tool_calls:
                with_tool += 1
                bad = [tc for tc in o.tool_calls if not self._valid_call(tc, allowed)]
                if not bad:
                    valid += 1
                else:
                    invalid += 1
                    # Wrong tool call — unknown/empty name or unparseable JSON
                    # arguments. Log the offending call(s) + the allowed set so
                    # the mismatch is obvious.
                    invalid_logged += 1
                    if invalid_logged <= fail_log_cap:
                        logger.warning(
                            "[tool_call_success] invalid tool call (req #%d):%s finish=%s "
                            "allowed=%s bad_calls=%s",
                            i, trunc_note, o.finish_reason, allowed,
                            [{"name": tc.get("name"),
                              "arguments": (tc.get("arguments") or "")[:200]} for tc in bad],
                        )
                    elif invalid_logged == fail_log_cap + 1:
                        logger.warning("[tool_call_success] …further invalid tool calls "
                                       "suppressed (logged first %d).", fail_log_cap)
            else:
                no_tool += 1
                # Missing tool call — surface what came back instead. The usual
                # misses are plain text (finish=stop) or a truncated reasoning
                # chain (finish=length).
                no_tool_logged += 1
                if no_tool_logged <= fail_log_cap:
                    logger.warning(
                        "[tool_call_success] no tool call (req #%d):%s finish=%s status=%s "
                        "tokens=%d content[%d]=%r reasoning[%d]=%r",
                        i, trunc_note, o.finish_reason, o.status, o.completion_tokens,
                        len(o.content), o.content[:300],
                        len(o.reasoning), o.reasoning[:200],
                    )
                elif no_tool_logged == fail_log_cap + 1:
                    logger.warning("[tool_call_success] …further no-tool-call responses "
                                   "suppressed (logged first %d).", fail_log_cap)

        if truncated:
            logger.warning(
                "[tool_call_success] %d/%d completed responses hit finish_reason=length "
                "at max_tokens=%d — truncation before the tool call counts as a failure; "
                "raise max_tokens if these are reasoning models.",
                truncated, completed, self.max_tokens,
            )

        total = self.num_requests
        success_rate = valid / total if total else 0.0
        avg_ct = (sum(comp_tokens) / len(comp_tokens)) if comp_tokens else 0.0
        return {
            "tool_call_success_rate": success_rate,
            "valid_tool_call_count": valid,
            "invalid_tool_call_count": invalid,
            "no_tool_call_count": no_tool,
            "tool_call_count": with_tool,
            "truncated_requests": truncated,
            "total_requests": total,
            "completed_requests": completed,
            "error_requests": error,
            "error_rate": (error / total) if total else 0.0,
            "avg_completion_tokens": avg_ct,
        }
