"""Tests for scripts/truncate_replay_dataset.py.

The invariants that matter here are structural, not statistical: a truncated
dataset is worthless if the requests in it no longer replay. Specifically —
dropping history must never orphan a `tool` message from the `assistant` whose
tool_calls it answers, and clipping a tool call's `arguments` must leave JSON a
backend can still parse. Both are tested below against a synthetic agent trace
shaped like captured gateway traffic.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    """Import the script by path — scripts/ isn't a package."""
    path = _ROOT / "scripts" / "truncate_replay_dataset.py"
    spec = importlib.util.spec_from_file_location("truncate_replay_dataset", path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trunc = _load_script()

def _tokenizer_available() -> bool:
    try:
        trunc.load_tokenizer(trunc.DEFAULT_PROCESSOR)
        return True
    except SystemExit:
        return False


pytestmark = pytest.mark.skipif(
    not _tokenizer_available(),
    reason="default tokenizer not cached and huggingface.co unreachable",
)


@pytest.fixture(scope="module")
def counter():
    return trunc.Counter(trunc.load_tokenizer(trunc.DEFAULT_PROCESSOR), trunc.DEFAULT_MARKER)


def _agent_body(turns: int = 6, filler: str = "lorem ipsum dolor sit amet ") -> dict:
    """A multi-turn agent trace: system, then user → assistant(tool_calls) → tool."""
    messages = [{"role": "system", "content": filler * 200}]
    for i in range(turns):
        messages.append({"role": "user", "content": f"turn {i} question " + filler * 20})
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "exec", "arguments": json.dumps({"command": filler * 40})},
            }],
        })
        messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": filler * 60})
    return {
        "model": "kimi-k2.5",
        "stream": True,
        "messages": messages,
        "tools": [{
            "type": "function",
            "function": {
                "name": "exec",
                "description": filler * 100,
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": filler * 50}},
                    "required": ["command"],
                },
            },
        }],
    }


def _orphaned_tool_messages(messages: list) -> int:
    """Count `tool` messages with no preceding assistant tool_call to answer."""
    orphans = pending = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            pending = len(msg.get("tool_calls") or [])
        elif role == "tool":
            if pending <= 0:
                orphans += 1
            else:
                pending -= 1
        else:
            pending = 0
    return orphans


# --------------------------------------------------------------------------- #
# water-filling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "lengths, budget, expected",
    [
        ([10, 20, 30], 100, -1),   # already fits — nothing to do
        ([10, 20, 30], 60, -1),    # exactly fits
        ([10, 20, 30], 45, 17),    # 10 + 17 + 17 = 44 <= 45
        ([100, 100], 50, 25),      # even split
        ([5, 1000], 10, 5),        # the small block survives whole
        ([100], 0, 0),             # nothing survives
    ],
)
def test_waterfill_cap(lengths, budget, expected):
    assert trunc.waterfill_cap(lengths, budget) == expected


def test_waterfill_never_exceeds_budget():
    lengths = [3, 900, 17, 240, 1, 65]
    for budget in range(0, sum(lengths) + 5):
        cap = trunc.waterfill_cap(lengths, budget)
        if cap < 0:
            assert sum(lengths) <= budget
        else:
            assert sum(min(n, cap) for n in lengths) <= budget


# --------------------------------------------------------------------------- #
# clipping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["middle", "head", "tail"])
def test_clip_respects_cap_and_keeps_the_right_end(counter, mode):
    text = "START " + ("filler word " * 500) + " END"
    out = counter.clip(text, 50, mode)
    assert counter.count(out) <= 50
    if mode in ("middle", "head"):
        assert out.startswith("START")
    if mode in ("middle", "tail"):
        assert out.endswith("END")


def test_clip_preserves_multibyte_text_exactly(counter):
    """Offsets slice the original string, so CJK and emoji survive a cut intact.

    A `middle` clip is head + marker + tail, so each surviving piece — not the
    whole result — must be an exact substring of the input. Anything else means
    a token boundary fell inside a code point.
    """
    text = "你好世界，这是一个很长的中文测试。" * 50 + "😀 tail"
    out = counter.clip(text, 40, "middle")
    assert counter.count(out) <= 40
    head, _, tail = out.partition(counter.marker)
    assert head and text.startswith(head)
    assert tail and text.endswith(tail)
    assert "\ufffd" not in out  # no replacement chars from a split code point
    assert out.encode("utf-8").decode("utf-8") == out


def test_clip_is_a_noop_under_the_cap(counter):
    assert counter.clip("short text", 1000, "middle") == "short text"


def test_clip_json_stays_parseable(counter):
    raw = json.dumps({"command": "echo " + "x " * 2000, "cwd": "/tmp", "timeout": 30})
    out = counter.clip_json(raw, 60, "middle")
    parsed = json.loads(out)  # the whole point: still valid JSON
    assert counter.count(out) <= 60
    assert parsed["cwd"] == "/tmp"      # small values survive water-filling
    assert parsed["timeout"] == 30      # non-strings are never touched
    assert len(parsed["command"]) < len("echo " + "x " * 2000)


def test_clip_json_falls_back_to_text_for_non_json(counter):
    raw = "not json at all " * 500
    out = counter.clip_json(raw, 30, "middle")
    assert counter.count(out) <= 30


def test_clip_json_never_cuts_into_the_skeleton(counter):
    """Regression: a cap too small for the JSON structure itself must leave the
    arguments valid and over budget, not shredded.

    The 6k build of gpu-41-42 shipped 535 tool calls whose arguments read
    `{"…[truncated]…}` — clip_to_budget re-clips across passes, so a pass that
    emptied every string value handed the next one an argument with no string
    leaves left, which fell through to a raw text clip of the braces."""
    raw = json.dumps({"command": "echo " + "x " * 2000})
    once = counter.clip_json(raw, 3, "middle")
    json.loads(once)  # valid, even though it cannot reach the cap
    twice = counter.clip_json(once, 3, "middle")
    assert json.loads(twice) == json.loads(once)  # and stable under re-clipping


def test_tool_call_arguments_survive_an_unreachable_budget(counter):
    """End to end: every tool call in a body squeezed far below its floor still
    carries arguments a backend can parse."""
    body = _agent_body(turns=12)
    trunc.truncate_body(body, counter, 40, set(trunc.SCOPE_KINDS), "middle", 0, "drop-then-clip")
    seen = 0
    for msg in body["messages"]:
        for call in msg.get("tool_calls") or []:
            arguments = call["function"]["arguments"]
            json.loads(arguments)  # raises if the skeleton was cut into
            seen += 1
    assert seen  # the fixture really does carry tool calls


# --------------------------------------------------------------------------- #
# generation cap and multimodal records
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body, expected, changed",
    [
        ({"max_tokens": 65536}, {"max_tokens": 8000}, True),
        ({"max_completion_tokens": 65536}, {"max_completion_tokens": 8000}, True),
        ({"max_tokens": 512}, {"max_tokens": 512}, False),            # only ever lowers
        ({"max_tokens": None}, {"max_tokens": 8000}, True),           # null = no limit
        ({"max_tokens": True}, {"max_tokens": 8000}, True),           # bool is not a limit
        ({}, {"max_tokens": 8000}, True),                             # absent = server default
        ({"max_tokens": 65536, "max_completion_tokens": 100},
         {"max_tokens": 8000, "max_completion_tokens": 100}, True),   # each judged alone
    ],
)
def test_clamp_output_tokens(body, expected, changed):
    assert trunc.clamp_output_tokens(body, 8000) is changed
    assert body == expected


def test_output_clamp_applies_to_untruncated_records(counter):
    """A request whose prompt already fits still carries production's 65K
    generation limit, and a small-window server rejects it on prompt +
    max_tokens. So the clamp cannot be gated on the input being truncated."""
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 65536}
    rec = _record(body)
    out, outcome = trunc.rewrite_record(
        rec, counter, 100000, set(trunc.SCOPE_KINDS), "middle", 0, "drop-then-clip",
        rewrite_usage=True, max_output_tokens=4096,
    )
    assert outcome.dropped == 0 and outcome.clipped == 0
    assert outcome.output_clamped
    assert json.loads(out["request_body"])["max_tokens"] == 4096
    assert out["truncation"]["max_output_tokens"] == 4096


def test_has_non_text_content():
    text_only = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    with_image = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
            ]},
        ]
    }
    assert not trunc.has_non_text_content(text_only)
    assert not trunc.has_non_text_content({"messages": [{"role": "user", "content": "hi"}]})
    assert trunc.has_non_text_content(with_image)


def test_cli_drops_over_budget_records(tmp_path):
    """A body whose tool schema alone busts the limit cannot be clipped into range,
    so --drop-over-budget is the only way to keep the file within the cap."""
    irreducible = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": f"tool_number_{i}_with_a_long_name",
                    "description": "d" * 200,
                    "parameters": {
                        "type": "object",
                        "properties": {f"parameter_{j}": {"type": "string"} for j in range(20)},
                    },
                },
            }
            for i in range(60)
        ],
    }
    src = tmp_path / "in.jsonl"
    with src.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(_agent_body(turns=4)), ensure_ascii=False) + "\n")
        handle.write(json.dumps(_record(irreducible), ensure_ascii=False) + "\n")

    kept = tmp_path / "kept.jsonl"
    assert trunc.main([str(src), str(kept), "-n", "500", "--quiet"]) == 0
    rows = [json.loads(line) for line in kept.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 2
    assert any((row.get("truncation") or {}).get("over_budget") for row in rows)

    dropped = tmp_path / "dropped.jsonl"
    assert trunc.main([str(src), str(dropped), "-n", "500", "--drop-over-budget", "--quiet"]) == 0
    rows = [json.loads(line) for line in dropped.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    assert not (rows[0].get("truncation") or {}).get("over_budget")

    counter = trunc.Counter(trunc.load_tokenizer(trunc.DEFAULT_PROCESSOR), trunc.DEFAULT_MARKER)
    assert trunc.measure(json.loads(rows[0]["request_body"]), counter, 0) <= 500


def test_cli_drops_multimodal_records(tmp_path):
    src = tmp_path / "in.jsonl"
    dst = tmp_path / "out.jsonl"
    image_body = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
        ]}],
        "max_tokens": 65536,
    }
    with src.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(_agent_body(turns=4)), ensure_ascii=False) + "\n")
        handle.write(json.dumps(_record(image_body), ensure_ascii=False) + "\n")

    assert trunc.main([str(src), str(dst), "-n", "2000", "-m", "1024",
                       "--drop-multimodal", "--quiet"]) == 0
    rows = [json.loads(line) for line in dst.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    assert json.loads(rows[0]["request_body"])["max_tokens"] == 1024

    # Without the flag the record is kept — the summary counts it instead.
    keep = tmp_path / "keep.jsonl"
    assert trunc.main([str(src), str(keep), "-n", "2000", "--quiet"]) == 0
    assert len([line for line in keep.read_text(encoding="utf-8").splitlines() if line]) == 2


# --------------------------------------------------------------------------- #
# dropping history
# --------------------------------------------------------------------------- #
def test_drop_history_keeps_system_and_final_turn(counter):
    body = _agent_body(turns=8)
    budget = 2000
    dropped = trunc.drop_history(body, counter, budget, 0)
    assert dropped > 0
    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert any(m["role"] == "user" for m in messages)
    # the last user turn of the original is still the last user turn here
    assert "turn 7" in next(m["content"] for m in reversed(messages) if m["role"] == "user")


def test_drop_history_never_orphans_a_tool_message(counter):
    for budget in (500, 1500, 3000, 6000, 12000):
        body = _agent_body(turns=10)
        trunc.drop_history(body, counter, budget, 0)
        assert _orphaned_tool_messages(body["messages"]) == 0


def test_drop_history_drops_the_minimum_needed(counter):
    """Cutting to the final turn when one fewer segment would have fit wastes
    length — the resulting dataset would sit far below the requested ceiling."""
    body = _agent_body(turns=8)
    full = trunc.measure(body, counter, 0)
    budget = int(full * 0.75)
    trunc.drop_history(body, counter, budget, 0)
    after = trunc.measure(body, counter, 0)
    assert after <= budget
    assert after > budget * 0.5  # didn't collapse all the way to the last turn


def test_drop_history_is_a_noop_without_droppable_history(counter):
    body = {
        "messages": [
            {"role": "system", "content": "sys " * 500},
            {"role": "user", "content": "the only turn " * 500},
        ]
    }
    before = list(body["messages"])
    assert trunc.drop_history(body, counter, 10, 0) == 0
    assert body["messages"] == before


# --------------------------------------------------------------------------- #
# end-to-end truncation of one request
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("budget", [400, 1200, 5000])
@pytest.mark.parametrize("strategy", ["drop-then-clip", "clip"])
def test_truncate_body_reaches_the_budget(counter, budget, strategy):
    body = _agent_body(turns=10)
    scope = set(trunc.SCOPE_KINDS)
    outcome = trunc.truncate_body(body, counter, budget, scope, "middle", 0, strategy)
    assert outcome.before > budget
    assert outcome.after <= budget, "synthetic body has a tiny tool schema — the floor is reachable"
    assert not outcome.over_budget
    assert trunc.measure(body, counter, 0) == outcome.after
    assert _orphaned_tool_messages(body["messages"]) == 0
    for msg in body["messages"]:
        for call in msg.get("tool_calls") or []:
            json.loads(call["function"]["arguments"])  # still parseable


def test_clip_strategy_keeps_every_message(counter):
    body = _agent_body(turns=10)
    n_before = len(body["messages"])
    trunc.truncate_body(body, counter, 800, set(trunc.SCOPE_KINDS), "middle", 0, "clip")
    assert len(body["messages"]) == n_before


def test_under_budget_bodies_are_untouched(counter):
    body = _agent_body(turns=2)
    original = json.dumps(body, sort_keys=True)
    outcome = trunc.truncate_body(
        body, counter, 10_000_000, set(trunc.SCOPE_KINDS), "middle", 0, "drop-then-clip"
    )
    assert not outcome.changed
    assert outcome.before == outcome.after
    assert json.dumps(body, sort_keys=True) == original


def test_truncation_is_idempotent(counter):
    body = _agent_body(turns=10)
    scope = set(trunc.SCOPE_KINDS)
    trunc.truncate_body(body, counter, 1500, scope, "middle", 0, "drop-then-clip")
    settled = json.dumps(body, sort_keys=True)
    second = trunc.truncate_body(body, counter, 1500, scope, "middle", 0, "drop-then-clip")
    assert not second.changed
    assert json.dumps(body, sort_keys=True) == settled


def test_over_budget_is_reported_not_hidden(counter):
    """A tool schema whose bare structure exceeds the limit can't be clipped into
    range. That has to surface, not silently ship an oversized request."""
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": f"tool_number_{i}_with_a_long_name",
                    "description": "d" * 200,
                    "parameters": {
                        "type": "object",
                        "properties": {f"parameter_{j}": {"type": "string"} for j in range(20)},
                    },
                },
            }
            for i in range(60)
        ],
    }
    outcome = trunc.truncate_body(body, counter, 100, set(trunc.SCOPE_KINDS), "middle", 0, "clip")
    assert outcome.over_budget
    assert outcome.after > 100


# --------------------------------------------------------------------------- #
# record + file level
# --------------------------------------------------------------------------- #
def _record(body: dict, prompt_tokens: int = 5000) -> dict:
    return {
        "source_file": "synthetic.jsonl",
        "line_no": 1,
        "level": "INFO",
        "timestamp": "2026-06-29T14:00:07.783+08:00",
        "request_id": "abc123",
        "channel_id": None,
        "token_name": None,
        "request_body": json.dumps(body, ensure_ascii=False),
        "request_json": body,
        "raw_payload": {
            "req_headers": {"content-length": "999999"},
            "resp_meta": {
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 100,
                    "total_tokens": prompt_tokens + 100,
                    "prompt_tokens_details": {"cached_tokens": prompt_tokens - 10},
                }
            },
            "status": 200,
        },
    }


def test_rewrite_record_syncs_every_derived_field(counter):
    rec = _record(_agent_body(turns=8))
    rec, outcome = trunc.rewrite_record(
        rec, counter, 1500, set(trunc.SCOPE_KINDS), "middle", 0, "drop-then-clip",
        rewrite_usage=True,
    )
    assert outcome.changed
    # request_json must not describe the pre-truncation body
    assert rec["request_json"] == json.loads(rec["request_body"])
    assert rec["raw_payload"]["req_headers"]["content-length"] == str(
        len(rec["request_body"].encode("utf-8"))
    )
    usage = rec["raw_payload"]["resp_meta"]["usage"]
    assert usage["prompt_tokens"] < 5000
    assert usage["prompt_tokens_details"]["cached_tokens"] <= usage["prompt_tokens"]
    assert usage["total_tokens"] >= usage["prompt_tokens"]
    meta = rec["truncation"]
    assert meta["max_input_tokens"] == 1500
    assert meta["estimated_tokens_after"] <= 1500
    assert meta["recorded_usage_before"]["prompt_tokens"] == 5000


def test_no_rewrite_usage_leaves_the_capture_alone(counter):
    rec = _record(_agent_body(turns=8))
    rec, _ = trunc.rewrite_record(
        rec, counter, 1500, set(trunc.SCOPE_KINDS), "middle", 0, "drop-then-clip",
        rewrite_usage=False,
    )
    assert rec["raw_payload"]["resp_meta"]["usage"]["prompt_tokens"] == 5000
    assert "recorded_usage_before" not in rec["truncation"]


def test_unparsable_request_body_is_copied_verbatim(counter):
    rec = {"request_body": "<html>gateway error</html>", "request_json": None}
    out, outcome = trunc.rewrite_record(
        rec, counter, 10, set(trunc.SCOPE_KINDS), "middle", 0, "drop-then-clip", rewrite_usage=True
    )
    assert outcome is None
    assert out["request_body"] == "<html>gateway error</html>"
    assert "truncation" not in out


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_cli_end_to_end(tmp_path, suffix):
    """Runs main() over a real file, including the gzip codec dispatch."""
    from bench.replay_test.jsonl_io import open_text

    src = tmp_path / f"in{suffix}"
    dst = tmp_path / f"out{suffix}"
    with open_text(src, "w") as handle:
        for turns in (2, 6, 10):
            handle.write(json.dumps(_record(_agent_body(turns=turns)), ensure_ascii=False) + "\n")

    assert trunc.main([str(src), str(dst), "-n", "2000", "--quiet"]) == 0
    assert dst.exists()
    assert not (tmp_path / f"out{suffix}.part").exists()

    counter = trunc.Counter(trunc.load_tokenizer(trunc.DEFAULT_PROCESSOR), trunc.DEFAULT_MARKER)
    with open_text(dst) as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    assert len(rows) == 3
    for row in rows:
        body = json.loads(row["request_body"])
        assert trunc.measure(body, counter, 0) <= 2000
        assert _orphaned_tool_messages(body["messages"]) == 0


def test_cli_rejects_a_bad_scope(tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_text(json.dumps(_record(_agent_body(turns=2))) + "\n")
    with pytest.raises(SystemExit):
        trunc.main([str(src), str(tmp_path / "out.jsonl"), "-n", "1000", "--scope", "nonsense"])
