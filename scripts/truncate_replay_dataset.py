#!/usr/bin/env python3
"""Cap the input (prompt) length of every request in a replay JSONL dataset.

Captured production traffic has a long input-length tail — the same dataset
carries 800-token requests and 250K-token ones. When you want to benchmark a
service whose context window (or your patience for a long run) is smaller than
that tail, you need a copy of the dataset whose requests all fit under a ceiling
*without* changing the shape of everything below it.

    python scripts/truncate_replay_dataset.py \
        dataset/replay/capture.replay.jsonl \
        dataset/replay/capture.20k.replay.jsonl \
        --max-input-tokens 20000 --max-output-tokens 12000 --drop-multimodal

Requests already under the limit are copied through untouched. Input and output
may each be `.jsonl` or `.jsonl.gz` — codec is picked from the extension (see
bench/replay_test/jsonl_io.py).

How a request is shrunk (`--strategy`, default `drop-then-clip`)
---------------------------------------------------------------
**Stage 1 — drop the oldest conversation turns.** Agent traffic is mostly
history: in one captured agent dataset the largest request is 414 messages,
203 of them assistant turns, and 89K of its 240K tokens are `tool_calls`
arguments. Nothing is oversized there; there is just a lot of it. Shedding old
turns is what an agent harness itself does when it runs out of context, and it
leaves every surviving message's text *verbatim* — much truer to real traffic
than shredding 400 messages into stubs.

History is cut at `user` message boundaries, oldest first, and only whole
segments are dropped. That is what keeps the request well-formed: an `assistant`
message carrying `tool_calls` and the `tool` messages answering it always live
inside one segment, so they are dropped together or not at all — a strict
backend never sees an orphaned `tool` message. Leading `system` messages and the
final user segment are always kept, and only as many segments are dropped as the
budget requires.

**Stage 2 — clip what is still too long.** When the survivors still don't fit
(a 13K-token system prompt, a single 8K-token tool result, a request with one
user turn and nothing droppable), the largest text blocks are clipped by
water-filling: find the largest per-block cap C such that the request fits, then
cut every block longer than C down to C. Short blocks — the final user turn,
small tool results — come through untouched; the big ones absorb the cut. Every
message, role and tool_call_id survives, so the request stays valid.

`--strategy clip` skips stage 1 and only clips, for when the message list must
be preserved exactly.

What counts as a clippable block (`--scope`)
--------------------------------------------
  messages    message contents — plain strings, and the `text` field of each
              part when content is a list.
  tools       `description` fields inside the `tools` schema, at any depth. On
              agent traffic that schema is routinely 8K-32K tokens (one request
              here carries 32,726 tokens of tools alone), so a 20K cap is
              unreachable unless tools are in scope. Tool *names* and parameter
              structure are never touched — those are what the model needs to
              emit a valid call.
  tool_args   `tool_calls[].function.arguments`. Clipped JSON-aware: the string
              is parsed and its string *values* are shortened, then re-
              serialized, so the arguments a backend receives are still parseable
              JSON. Only arguments that were never valid JSON fall back to a
              plain text clip. When even emptying every string value doesn't
              reach the cap, the arguments are left as they are and the record
              is reported over budget — the JSON skeleton is never cut into.

Fitting the *output* too (`--max-output-tokens`)
------------------------------------------------
Capping the prompt is only half of moving a capture onto a smaller server. The
requests still carry the generation limits production asked for (65,536 tokens
on half of one agent capture; 262,144 on one record), and an OpenAI-compatible engine
rejects a request whose `prompt + max_tokens` exceeds its window instead of
clamping it the way a gateway does — so a 6K-input dataset still 400s on a 32K
server. `--max-output-tokens` lowers that limit in the dataset, and sets one on
requests that carry none (without it the server applies its own default —
SGLang: 131072 — and rejects on the same arithmetic). It only ever lowers, so
the output-length distribution below the cap is untouched. For a context window
W, pass roughly W minus `--max-input-tokens`.

Requests truncation cannot fix (`--drop-multimodal`, `--drop-over-budget`)
--------------------------------------------------------------------------
A message content part that isn't text — `image_url`, audio — has no length to
cut and no text-only target that will take it: a deployment started with
`--limit-mm-per-prompt image=0` answers 501 "This server does not accept image
input" for the whole request. Those records are always counted in the summary;
`--drop-multimodal` leaves them out of the output.

Some requests also stay above the limit no matter how hard they are clipped.
Their bulk is the part that is deliberately never cut: tool names and parameter
schemas. One request in an agent capture carries 87 tool definitions worth 20,669
tokens with every description already emptied — only 388 of those are the names
themselves; the rest is JSON Schema structure the model needs to emit a valid
call. Truncating to 6K leaves 189 such records (22%), the worst 3.4x over.
`--drop-over-budget` leaves them out, for when the target's context window is a
hard wall and an oversized request would only 400. Without it they ship and the
summary warns.

All three are in scope by default.

Token counting is an estimate
-----------------------------
Prompt tokens are counted as every message text plus the serialized `tools`
schema, using the tokenizer in `--processor` (default
Qwen/Qwen3-0.6B, matching the rest of this repo). No chat
template is applied — the vendored processor configs don't ship one for MiniMax,
and the model that produced a captured dataset is usually not the model you are
about to benchmark anyway.

Measured against the capture's own accounting on an agent capture (Kimi-K2.5 traffic),
this estimate runs ~6% HIGH — it errs toward truncating slightly more than
strictly necessary. The summary prints the observed ratio for your dataset and
tokenizer, so you can adjust the limit if you need the server-side count to land
on a specific number.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.replay_test.jsonl_io import open_text  # noqa: E402

DEFAULT_PROCESSOR = "Qwen/Qwen3-0.6B"
DEFAULT_MARKER = "\n…[truncated]…\n"
DEFAULT_SCOPE = "messages,tools,tool_args"
SCOPE_KINDS = ("messages", "tools", "tool_args")
# Re-measure and re-clip this many times. One pass is nearly always enough;
# extra passes only matter when JSON escaping makes a serialized structure
# shrink by slightly less than the text we cut out of it.
MAX_PASSES = 3


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #
def load_tokenizer(processor_path: str):
    """Load the fast tokenizer from a local directory or a Hugging Face repo id.

    Uses `tokenizers.Tokenizer` directly rather than transformers' AutoTokenizer:
    it loads in ~0.2s, needs no `trust_remote_code`, and — the part this script
    depends on — exposes per-token character offsets, which let us cut a string
    at an exact token boundary by slicing the ORIGINAL text. Decoding token ids
    back to text would round-trip byte-level BPE through a lossy path and mangle
    CJK; slicing by offset cannot.
    """
    from tokenizers import Tokenizer

    root = Path(__file__).resolve().parent.parent
    base = Path(processor_path)
    if not base.is_absolute():
        base = root / base
    if (base / "tokenizer.json").exists():
        return Tokenizer.from_file(str(base / "tokenizer.json"))
    # Not a local directory: treat it as a repo id (downloaded once into HF_HOME).
    try:
        return Tokenizer.from_pretrained(processor_path)
    except Exception as exc:
        raise SystemExit(
            f"cannot load a tokenizer from {processor_path!r}: not a directory with "
            f"a tokenizer.json, and not a reachable Hugging Face repo id ({exc})"
        )


class Counter:
    """Token counting and exact-boundary clipping against one tokenizer."""

    def __init__(self, tokenizer, marker: str) -> None:
        self.tok = tokenizer
        self.marker = marker
        self.marker_n = len(tokenizer.encode(marker, add_special_tokens=False).ids) if marker else 0

    def count(self, text: str) -> int:
        return len(self.tok.encode(text, add_special_tokens=False).ids) if text else 0

    def count_many(self, texts: list[str]) -> list[int]:
        """Batch encode — `tokenizers` parallelises this in Rust."""
        if not texts:
            return []
        return [len(e.ids) for e in self.tok.encode_batch(texts, add_special_tokens=False)]

    def clip(self, text: str, cap: int, mode: str) -> str:
        """Return `text` shortened to at most `cap` tokens, marker included.

        `mode` decides which end survives: keep the head, keep the tail, or keep
        both ends and drop the middle.
        """
        if cap <= 0:
            return ""
        enc = self.tok.encode(text, add_special_tokens=False)
        n = len(enc.ids)
        if n <= cap:
            return text
        offs = enc.offsets
        # The marker costs tokens too. If the budget is so small the marker
        # would leave no room for real content, drop the marker instead.
        marker = self.marker if self.marker_n < cap else ""
        room = cap - (self.marker_n if marker else 0)
        if room <= 0:
            return ""
        if mode == "head":
            return text[: offs[room - 1][1]] + marker
        if mode == "tail":
            return marker + text[offs[n - room][0] :]
        head, tail = room // 2, room - room // 2
        if head == 0:
            return marker + text[offs[n - tail][0] :]
        return text[: offs[head - 1][1]] + marker + text[offs[n - tail][0] :]

    def clip_json(self, raw: str, cap: int, mode: str) -> str:
        """Clip a JSON *string* to `cap` tokens while keeping it parseable.

        Tool-call arguments are JSON that a backend (or a chat template doing
        `arguments | fromjson`) may parse. Cutting the raw string at a token
        boundary would leave a dangling object; instead we parse it, water-fill
        the string values inside it, and re-serialize. When the JSON skeleton
        alone exceeds `cap` we emit it with every string emptied — still valid,
        still the smallest this argument can become — and the record is reported
        as over budget rather than silently corrupted.
        """
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Not JSON to begin with — a few captured arguments really are
            # malformed. A text clip cannot make them less parseable than they
            # already are, and there is nothing here to preserve.
            return self.clip(raw, cap, mode)
        leaves: list[tuple[Any, Any]] = []
        _collect_str_leaves(obj, leaves)
        if not leaves:
            # Nothing left to give: either the arguments never held a string, or
            # an earlier pass already emptied every one of them. What remains is
            # the skeleton — braces, keys, numbers — and cutting into THAT is
            # what corrupts the request. Ship it over budget instead; the record
            # is reported as over_budget and the caller can see it.
            #
            # This is the line that produced `{"\n…[truncated]…\n}` in the 6k
            # dataset: clip_to_budget re-clips across passes, so a first pass
            # that emptied all the strings handed the second pass an argument
            # with no leaves, which then fell through to a raw text clip.
            return raw
        originals = [container[key] for container, key in leaves]
        lens = self.count_many(originals)
        inner_cap = waterfill_cap(lens, cap - (self.count(raw) - sum(lens)))
        if inner_cap < 0:
            return raw
        # Re-serializing costs more than the strings we measured: `"` and `\n`
        # inside a value are one character but two in the JSON text. So clip,
        # measure the actual dump, and tighten until it fits — re-clipping from
        # `originals` each pass rather than compounding cuts on cut text.
        out = raw
        for _ in range(MAX_PASSES):
            for (container, key), original, n in zip(leaves, originals, lens):
                container[key] = self.clip(original, inner_cap, mode) if n > inner_cap else original
            out = json.dumps(obj, ensure_ascii=False)
            overshoot = self.count(out) - cap
            if overshoot <= 0 or inner_cap == 0:
                break
            n_clipped = sum(1 for n in lens if n > inner_cap) or 1
            inner_cap = max(0, inner_cap - max(1, -(-overshoot // n_clipped)))
        return out


def _collect_str_leaves(node: Any, out: list[tuple[Any, Any]]) -> None:
    """Collect (container, key) pairs for every non-empty string in a JSON tree."""
    if isinstance(node, dict):
        items: Any = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return
    for key, value in items:
        if isinstance(value, str):
            if value:
                out.append((node, key))
        else:
            _collect_str_leaves(value, out)


# --------------------------------------------------------------------------- #
# measuring a request
# --------------------------------------------------------------------------- #
def message_texts(msg: dict) -> list[str]:
    """Every piece of a message that lands in the prompt as text."""
    texts: list[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            for key in ("name", "arguments"):
                if isinstance(fn.get(key), str):
                    texts.append(fn[key])
    return texts


def message_token_counts(messages: list, counter: Counter, overhead_per_message: int) -> list[int]:
    """Per-message token cost, in one batched encode across the whole list."""
    flat: list[str] = []
    spans: list[int] = []
    for msg in messages:
        texts = message_texts(msg) if isinstance(msg, dict) else []
        texts = [t for t in texts if t]
        spans.append(len(texts))
        flat.extend(texts)
    counts = counter.count_many(flat)
    out: list[int] = []
    pos = 0
    for span in spans:
        out.append(sum(counts[pos : pos + span]) + overhead_per_message)
        pos += span
    return out


def tools_tokens(body: dict, counter: Counter) -> int:
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return 0
    return counter.count(json.dumps(tools, ensure_ascii=False))


def measure(body: dict, counter: Counter, overhead_per_message: int) -> int:
    """Estimated prompt tokens: message text + serialized tools schema."""
    messages = body.get("messages")
    total = tools_tokens(body, counter)
    if isinstance(messages, list):
        total += sum(message_token_counts(messages, counter, overhead_per_message))
    return total


# --------------------------------------------------------------------------- #
# stage 1 — drop the oldest conversation turns
# --------------------------------------------------------------------------- #
def user_segment_starts(messages: list) -> tuple[int, list[int]]:
    """Return (end of the leading system block, indices of `user` messages after it).

    Those indices are the only safe cut points: every `tool` message follows the
    `assistant` whose tool_calls it answers, and both sit between two user turns,
    so slicing the list at a user message can never orphan a tool response.
    """
    head_end = 0
    while (
        head_end < len(messages)
        and isinstance(messages[head_end], dict)
        and messages[head_end].get("role") == "system"
    ):
        head_end += 1
    starts = [
        i
        for i in range(head_end, len(messages))
        if isinstance(messages[i], dict) and messages[i].get("role") == "user"
    ]
    return head_end, starts


def drop_history(body: dict, counter: Counter, budget: int, overhead_per_message: int) -> int:
    """Drop the oldest user-bounded segments until the request fits.

    Drops the FEWEST segments that get under budget, so the result lands as
    close to the limit as segment granularity allows rather than collapsing to
    the final turn. If no amount of dropping is enough, everything but the final
    segment goes and stage 2 finishes the job. Returns the number of messages
    removed.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return 0
    head_end, starts = user_segment_starts(messages)
    if len(starts) < 2:  # nothing droppable: no history before the final user turn
        return 0

    per_message = message_token_counts(messages, counter, overhead_per_message)
    fixed = tools_tokens(body, counter) + sum(per_message[:head_end])
    # suffix[i] = cost of messages[i:]
    suffix = [0] * (len(messages) + 1)
    for i in range(len(messages) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + per_message[i]

    keep_from = None
    for start in starts[1:]:
        if fixed + suffix[start] <= budget:
            keep_from = start
            break
    if keep_from is None:
        keep_from = starts[-1]

    body["messages"] = messages[:head_end] + messages[keep_from:]
    return keep_from - head_end


# --------------------------------------------------------------------------- #
# stage 2 — clip the largest remaining blocks
# --------------------------------------------------------------------------- #
@dataclass
class Slot:
    """One clippable text block, plus the closure that writes it back."""

    text: str
    kind: str
    apply: Callable[[str], None]
    json_aware: bool = False
    tokens: int = 0


def _set_key(container: dict, key: str) -> Callable[[str], None]:
    def apply(value: str) -> None:
        container[key] = value

    return apply


def collect_slots(body: dict, scope: set[str]) -> list[Slot]:
    slots: list[Slot] = []
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if "messages" in scope:
                content = msg.get("content")
                if isinstance(content, str) and content:
                    slots.append(Slot(content, "messages", _set_key(msg, "content")))
                elif isinstance(content, list):
                    # Multimodal content: clip the text parts and leave image /
                    # audio parts alone — they aren't text, and their token cost
                    # isn't ours to estimate.
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                            slots.append(Slot(part["text"], "messages", _set_key(part, "text")))
            if "tool_args" in scope:
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") if isinstance(call, dict) else None
                    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str) and fn["arguments"]:
                        slots.append(
                            Slot(fn["arguments"], "tool_args", _set_key(fn, "arguments"), json_aware=True)
                        )
    if "tools" in scope and isinstance(body.get("tools"), list):
        for tool in body["tools"]:
            _collect_descriptions(tool, slots)
    return slots


def _collect_descriptions(node: Any, slots: list[Slot]) -> None:
    """Walk a tool schema and collect every `description` string.

    Descriptions are where the bulk of an agent tool schema lives (parameter
    prose, usage notes, examples). Names, types and the `required` list are what
    the model needs to emit a *valid* call, so they are never clipped.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str) and value:
                slots.append(Slot(value, "tools", _set_key(node, "description")))
            else:
                _collect_descriptions(value, slots)
    elif isinstance(node, list):
        for item in node:
            _collect_descriptions(item, slots)


def waterfill_cap(lengths: list[int], budget: int) -> int:
    """Largest per-block cap C with sum(min(L_i, C)) <= budget.

    Returns -1 when nothing needs clipping, 0 when even emptying every block
    doesn't reach the budget (the caller reports those as over budget).
    """
    if budget < 0:
        return 0
    if sum(lengths) <= budget:
        return -1
    remaining = budget
    ordered = sorted(lengths)
    for i, length in enumerate(ordered):
        n_left = len(ordered) - i
        if length * n_left <= remaining:
            remaining -= length
        else:
            return remaining // n_left
    return -1


def clip_to_budget(
    body: dict, counter: Counter, budget: int, scope: set[str], clip_mode: str,
    overhead_per_message: int, total: int,
) -> tuple[int, int]:
    """Water-fill the clippable blocks of `body`. Returns (new total, blocks clipped)."""
    cap = -1
    clipped = 0
    for _ in range(MAX_PASSES):
        slots = collect_slots(body, scope)
        if not slots:
            break
        for slot, n in zip(slots, counter.count_many([s.text for s in slots])):
            slot.tokens = n
        # Everything that isn't a clippable block: JSON structure, role names,
        # tool names, parameter schemas. This is the floor we cannot cut below.
        fixed = total - sum(s.tokens for s in slots)
        cap = waterfill_cap([s.tokens for s in slots], budget - fixed)
        if cap < 0:
            break
        for slot in slots:
            if slot.tokens > cap:
                shorter = (
                    counter.clip_json(slot.text, cap, clip_mode)
                    if slot.json_aware
                    else counter.clip(slot.text, cap, clip_mode)
                )
                if shorter != slot.text:
                    slot.apply(shorter)
                    clipped += 1
        total = measure(body, counter, overhead_per_message)
        if total <= budget:
            break
    return total, clipped


@dataclass
class Outcome:
    before: int
    after: int
    dropped: int = 0
    clipped: int = 0
    over_budget: bool = False
    output_clamped: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.clipped or self.output_clamped)


def truncate_body(
    body: dict, counter: Counter, budget: int, scope: set[str], clip_mode: str,
    overhead_per_message: int, strategy: str,
) -> Outcome:
    """Shrink `body` in place until its estimated prompt fits `budget`."""
    before = measure(body, counter, overhead_per_message)
    if before <= budget:
        return Outcome(before=before, after=before)

    total = before
    dropped = 0
    if strategy == "drop-then-clip":
        dropped = drop_history(body, counter, budget, overhead_per_message)
        if dropped:
            total = measure(body, counter, overhead_per_message)

    clipped = 0
    if total > budget:
        total, clipped = clip_to_budget(
            body, counter, budget, scope, clip_mode, overhead_per_message, total
        )
    return Outcome(
        before=before, after=total, dropped=dropped, clipped=clipped, over_budget=total > budget
    )


# --------------------------------------------------------------------------- #
# generation cap
# --------------------------------------------------------------------------- #
OUTPUT_TOKEN_KEYS = ("max_completion_tokens", "max_tokens")


def clamp_output_tokens(body: dict, cap: int) -> bool:
    """Lower the request's generation limit to `cap`. Returns True if it moved.

    Capping the *input* is only half of fitting a capture onto a smaller server.
    The requests still carry the generation limits the original traffic asked
    for — in one agent capture that is 65,536 tokens on half the
    dataset and 262,144 on one record. An OpenAI-compatible engine checks
    `prompt + max_tokens` against its context window and REJECTS the request
    when it doesn't fit, rather than silently clamping the way a production
    gateway does, so a 6K-input dataset still 400s on a 32K server.

    The limit is only ever LOWERED — a request that already asked for less keeps
    its own value, so the output-length distribution below the cap is preserved.
    A null or malformed limit means "no limit" and is pinned to `cap`. A request
    carrying no limit at all gets one: without it the server applies its own
    default (SGLang: 131072) and rejects on the same arithmetic.

    Mirrors the request-time clamp in bench/replay_test/log_replay_tool.py
    (`max_generation_tokens`); doing it here bakes the ceiling into the dataset
    so every consumer of the file inherits it.
    """
    changed = False
    present = False
    for key in OUTPUT_TOKEN_KEYS:
        if key not in body:
            continue
        present = True
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value > cap:
            body[key] = cap
            changed = True
    if not present:
        body["max_tokens"] = cap
        changed = True
    return changed


def has_non_text_content(body: dict) -> bool:
    """True when any message carries a content part that isn't text.

    Multimodal parts (`image_url`, and audio/video in newer captures) are the
    one thing truncation cannot shrink and a text-only target cannot accept: a
    vLLM/SGLang deployment started with `--limit-mm-per-prompt image=0` answers
    501 "This server does not accept image input" for the whole request. The
    12 such records in one agent capture are a rounding error in the dataset but a
    steady drip of failures in a replay run.
    """
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") not in (None, "text"):
                return True
    return False


# --------------------------------------------------------------------------- #
# record rewriting
# --------------------------------------------------------------------------- #
def rewrite_record(
    rec: dict, counter: Counter, budget: int, scope: set[str], clip_mode: str,
    overhead_per_message: int, strategy: str, rewrite_usage: bool,
    max_output_tokens: int = 0,
) -> tuple[dict, Outcome | None]:
    """Return (record, outcome). `outcome` is None when the body isn't usable."""
    raw = rec.get("request_body")
    if not isinstance(raw, str):
        return rec, None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return rec, None
    if not isinstance(body, dict):
        return rec, None

    messages_before = len(body.get("messages") or [])
    outcome = truncate_body(
        body, counter, budget, scope, clip_mode, overhead_per_message, strategy
    )
    # The generation cap is independent of the prompt cap: a request whose input
    # was already under budget can still ask for 65K output tokens and be
    # rejected by a small-window server, so this runs on every record.
    if max_output_tokens > 0:
        outcome.output_clamped = clamp_output_tokens(body, max_output_tokens)
    if not outcome.changed:
        return rec, outcome

    new_body = json.dumps(body, ensure_ascii=False)
    rec["request_body"] = new_body
    # Keep the parsed copy in sync. The replay runner loads lean (request_json
    # dropped, request_body re-parsed), but the analyzer and the CLI tool read
    # request_json when it's there — a stale copy would silently describe the
    # pre-truncation dataset.
    if isinstance(rec.get("request_json"), dict):
        rec["request_json"] = body

    raw_payload = rec.get("raw_payload")
    if isinstance(raw_payload, dict):
        headers = raw_payload.get("req_headers")
        if isinstance(headers, dict) and "content-length" in headers:
            headers["content-length"] = str(len(new_body.encode("utf-8")))
    rec["truncation"] = {
        "max_input_tokens": budget,
        "estimated_tokens_before": outcome.before,
        "estimated_tokens_after": outcome.after,
        "messages_before": messages_before,
        "messages_after": len(body.get("messages") or []),
        "messages_dropped": outcome.dropped,
        "blocks_clipped": outcome.clipped,
        "over_budget": outcome.over_budget,
    }
    if outcome.output_clamped:
        rec["truncation"]["max_output_tokens"] = max_output_tokens
    # After `truncation` exists, so the pre-truncation usage it stashes there
    # isn't overwritten by the assignment above.
    if isinstance(raw_payload, dict) and rewrite_usage:
        _rewrite_usage(raw_payload, outcome, rec)
    return rec, outcome


def _rewrite_usage(raw_payload: dict, outcome: Outcome, rec: dict) -> None:
    """Scale the captured prompt-token accounting to the truncated request.

    The recorded usage is the *original* model's own count, and downstream tools
    (scripts/analyze_replay_dataset.py, the replay test's TTFT input buckets)
    read it as the request's input length. Left alone it would describe requests
    this dataset no longer contains. We scale it by the same ratio we actually
    cut — which preserves its relationship to the server's own counting — and
    keep the originals on the record under `truncation`.
    """
    meta = raw_payload.get("resp_meta")
    if not isinstance(meta, dict):
        return
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_tokens")
    if not isinstance(prompt, int) or prompt <= 0 or outcome.before <= 0:
        return
    new_prompt = max(1, int(round(prompt * (outcome.after / outcome.before))))
    delta = prompt - new_prompt
    original = {"prompt_tokens": prompt}
    usage["prompt_tokens"] = new_prompt
    if isinstance(usage.get("total_tokens"), int):
        original["total_tokens"] = usage["total_tokens"]
        usage["total_tokens"] = max(new_prompt, usage["total_tokens"] - delta)
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        original["cached_tokens"] = details["cached_tokens"]
        # Cached tokens are a prefix of the prompt; they cannot exceed it.
        details["cached_tokens"] = min(details["cached_tokens"], new_prompt)
    rec.setdefault("truncation", {})["recorded_usage_before"] = original


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def percentile(sorted_vals: list[float], ratio: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = ratio * (len(sorted_vals) - 1)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def describe(vals: list[int]) -> str:
    if not vals:
        return "n=0"
    s = sorted(vals)
    return (
        f"n={len(s):<6,} mean={sum(s) / len(s):>9,.0f}  p50={percentile(s, 0.50):>9,.0f}  "
        f"p90={percentile(s, 0.90):>9,.0f}  p99={percentile(s, 0.99):>9,.0f}  max={s[-1]:>9,}"
    )


@dataclass
class Stats:
    read: int = 0
    written: int = 0
    unparsable: int = 0
    truncated: int = 0
    dropped_history: int = 0
    output_clamped: int = 0
    multimodal: int = 0
    dropped_multimodal: int = 0
    over_budget: int = 0
    dropped_over_budget: int = 0
    overshoot: list[int] = field(default_factory=list)
    before: list[int] = field(default_factory=list)
    after: list[int] = field(default_factory=list)
    est_vs_recorded: list[float] = field(default_factory=list)
    bytes_in: int = 0
    bytes_out: int = 0


def _record_has_non_text_content(rec: dict) -> bool:
    raw = rec.get("request_body")
    if not isinstance(raw, str):
        return False
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(body, dict) and has_non_text_content(body)


def _recorded_prompt_tokens(rec: dict) -> int | None:
    raw_payload = rec.get("raw_payload")
    meta = raw_payload.get("resp_meta") if isinstance(raw_payload, dict) else None
    usage = meta.get("usage") if isinstance(meta, dict) else None
    value = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return value if isinstance(value, int) and value > 0 else None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def iter_records(path: Path) -> Iterator[str]:
    with open_text(path) as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                yield stripped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cap the input length of every request in a replay JSONL dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", type=Path, help="source .jsonl or .jsonl.gz")
    parser.add_argument("output", type=Path, help="destination .jsonl or .jsonl.gz")
    parser.add_argument(
        "--max-input-tokens", "-n", type=int, required=True,
        help="upper bound on estimated prompt tokens per request (e.g. 20000)",
    )
    parser.add_argument(
        "--max-output-tokens", "-m", type=int, default=0,
        help="also cap each request's generation limit (max_tokens / "
             "max_completion_tokens) at this many tokens, setting one on requests "
             "that carry none. Captures keep the limits production asked for — up "
             "to 262,144 here — and a server rejects the request when prompt + "
             "max_tokens exceeds its window, however short the prompt is. For a "
             "context window W, pass roughly W minus --max-input-tokens. "
             "Default 0: leave the captured limits alone",
    )
    parser.add_argument(
        "--drop-multimodal", action="store_true",
        help="drop records whose messages carry a non-text content part (image_url, "
             "audio, …). A text-only deployment answers 501 for those regardless of "
             "length. Without this flag they are kept and merely counted",
    )
    parser.add_argument(
        "--drop-over-budget", action="store_true",
        help="drop records that are still above --max-input-tokens after truncation. "
             "Those are requests whose irreducible structure — tool names and parameter "
             "schemas, which are never clipped — already exceeds the limit on its own, "
             "so no setting brings them under it. Use this when the target's context "
             "window is the hard constraint and an oversized request would just 400",
    )
    parser.add_argument(
        "--processor", default=DEFAULT_PROCESSOR,
        help=f"tokenizer directory or Hugging Face repo id (default: {DEFAULT_PROCESSOR})",
    )
    parser.add_argument(
        "--strategy", choices=("drop-then-clip", "clip"), default="drop-then-clip",
        help="drop the oldest conversation turns first and clip what's left "
             "(default), or only clip and keep every message",
    )
    parser.add_argument(
        "--clip", choices=("middle", "head", "tail"), default="middle",
        help="which part of an oversized block survives: keep both ends and drop "
             "the middle (middle, default), keep the start (head), keep the end (tail)",
    )
    parser.add_argument(
        "--scope", default=DEFAULT_SCOPE,
        help=f"comma-separated clippable block kinds: {', '.join(SCOPE_KINDS)} "
             f"(default: {DEFAULT_SCOPE})",
    )
    parser.add_argument(
        "--marker", default=DEFAULT_MARKER,
        help="text inserted where content was removed; pass '' for none",
    )
    parser.add_argument(
        "--overhead-per-message", type=int, default=0,
        help="extra tokens charged per message for chat-template delimiters (default 0)",
    )
    parser.add_argument("--limit", type=int, default=0, help="process only the first N records")
    parser.add_argument(
        "--no-rewrite-usage", action="store_true",
        help="leave the captured raw_payload.resp_meta.usage token counts alone; by "
             "default they are scaled so downstream analysis reports the truncated "
             "input lengths, with the originals kept under `truncation`",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the progress line")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_input_tokens <= 0:
        parser.error("--max-input-tokens must be positive")
    if args.max_output_tokens < 0:
        parser.error("--max-output-tokens must not be negative")
    scope = {s.strip() for s in args.scope.split(",") if s.strip()}
    unknown = scope - set(SCOPE_KINDS)
    if unknown:
        parser.error(f"unknown --scope value(s): {', '.join(sorted(unknown))}")
    if not scope:
        parser.error("--scope must name at least one block kind")
    if not args.input.exists():
        parser.error(f"input not found: {args.input}")

    counter = Counter(load_tokenizer(args.processor), args.marker)
    stats = Stats()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Stage to `.part` and rename on success, so an interrupted run never leaves
    # a half-written dataset sitting at the published name.
    staging = args.output.with_name(args.output.name + ".part")
    try:
        with open_text(staging, "w") as out:
            for line in iter_records(args.input):
                stats.read += 1
                stats.bytes_in += len(line) + 1
                rec = json.loads(line)
                recorded = _recorded_prompt_tokens(rec)
                keep = True
                if _record_has_non_text_content(rec):
                    stats.multimodal += 1
                    keep = not args.drop_multimodal
                    if not keep:
                        stats.dropped_multimodal += 1
                if keep:
                    rec, outcome = rewrite_record(
                        rec, counter, args.max_input_tokens, scope, args.clip,
                        args.overhead_per_message, args.strategy,
                        rewrite_usage=not args.no_rewrite_usage,
                        max_output_tokens=args.max_output_tokens,
                    )
                    if outcome is None:
                        stats.unparsable += 1
                    else:
                        if outcome.over_budget:
                            stats.over_budget += 1
                            stats.overshoot.append(outcome.after - args.max_input_tokens)
                            keep = not args.drop_over_budget
                            if not keep:
                                stats.dropped_over_budget += 1
                        # Only records that survive are described by the summary's
                        # distributions — otherwise `after` would report a tail the
                        # output file doesn't contain.
                        if keep:
                            stats.before.append(outcome.before)
                            stats.after.append(outcome.after)
                            if outcome.changed:
                                stats.truncated += 1
                            if outcome.dropped:
                                stats.dropped_history += 1
                            if outcome.output_clamped:
                                stats.output_clamped += 1
                            if recorded and not outcome.changed:
                                stats.est_vs_recorded.append(outcome.before / recorded)
                if keep:
                    serialized = json.dumps(rec, ensure_ascii=False)
                    out.write(serialized + "\n")
                    stats.written += 1
                    stats.bytes_out += len(serialized) + 1
                if not args.quiet and stats.read % 100 == 0:
                    print(
                        f"  … {stats.read:,} records, {stats.truncated:,} truncated",
                        end="\r", file=sys.stderr, flush=True,
                    )
                if args.limit and stats.read >= args.limit:
                    break
        staging.replace(args.output)
    finally:
        if staging.exists():
            staging.unlink()

    if not args.quiet:
        print(" " * 60, end="\r", file=sys.stderr)
    report(args, stats)
    return 0


def report(args: argparse.Namespace, stats: Stats) -> None:
    print(f"\n{args.input}  →  {args.output}")
    print(
        f"  input limit    : {args.max_input_tokens:,} tokens/request  "
        f"(strategy={args.strategy}, clip={args.clip}, scope={args.scope})"
    )
    print(
        "  output limit   : "
        + (f"{args.max_output_tokens:,} tokens/request" if args.max_output_tokens
           else "unchanged (captured max_tokens replayed as-is — pass --max-output-tokens "
                "if the target's context window is smaller than prompt + max_tokens)")
    )
    print(f"  tokenizer      : {args.processor}")
    unusable = (
        f"  ({stats.unparsable:,} with unusable request_body, copied verbatim)"
        if stats.unparsable else ""
    )
    print(f"  records        : {stats.written:,} written of {stats.read:,} read{unusable}")
    share = f" ({stats.truncated / stats.read * 100:.1f}%)" if stats.read else ""
    print(f"  shrunk         : {stats.truncated:,}{share}"
          f"  — {stats.dropped_history:,} of them by dropping old turns")
    if args.max_output_tokens:
        print(f"  output clamped : {stats.output_clamped:,} record(s) had their generation "
              f"limit lowered or set")
    if stats.multimodal:
        if args.drop_multimodal:
            print(f"  multimodal     : {stats.dropped_multimodal:,} record(s) dropped "
                  f"(non-text content parts)")
        else:
            print(f"  ⚠ multimodal   : {stats.multimodal:,} record(s) carry a non-text content "
                  f"part (image_url, …).\n"
                  f"                   A text-only deployment answers 501 for every one of them "
                  f"— pass --drop-multimodal\n"
                  f"                   to leave them out.")
    if stats.over_budget:
        worst = max(stats.overshoot)
        if args.drop_over_budget:
            print(
                f"  over budget    : {stats.dropped_over_budget:,} record(s) dropped — still above "
                f"the limit after truncation,\n                   by up to {worst:,} tokens "
                f"({worst / args.max_input_tokens * 100:.1f}%)."
            )
        else:
            print(
                f"  ⚠ over budget  : {stats.over_budget:,} record(s) still exceed the limit, by up "
                f"to {worst:,} tokens ({worst / args.max_input_tokens * 100:.1f}%).\n"
                f"                   Their irreducible structure — tool names, parameter schemas, "
                f"JSON skeletons — is\n                   already bigger than the limit, so no "
                f"amount of clipping text reaches it. Raise\n                   the limit, drop "
                f"tool definitions from those requests by hand, or pass\n"
                f"                   --drop-over-budget to leave them out."
            )
    print(f"  size           : {stats.bytes_in / 1e6:,.1f} MB → {stats.bytes_out / 1e6:,.1f} MB")
    print("\n--- estimated prompt tokens ---")
    print(f"  before : {describe(stats.before)}")
    print(f"  after  : {describe(stats.after)}")
    if stats.est_vs_recorded:
        ratios = sorted(stats.est_vs_recorded)
        median = percentile(ratios, 0.50)
        print(
            f"\n  calibration: across the {len(ratios):,} untouched records that carry the capture's "
            f"own usage,\n  this tokenizer estimates {median:.3f}x the recorded prompt_tokens "
            f"(p10={percentile(ratios, 0.10):.3f}, p90={percentile(ratios, 0.90):.3f}).\n"
            f"  Expect the server to count roughly {args.max_input_tokens / median:,.0f} tokens for "
            f"a request capped here at {args.max_input_tokens:,}."
        )


if __name__ == "__main__":
    raise SystemExit(main())
