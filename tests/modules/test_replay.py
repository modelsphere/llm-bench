"""
Unit test for replay module.

The module requires a JSONL replay dataset. Tests here verify:
  1. Descriptor and params validation pass.
  2. When no dataset_path is set, the module skips gracefully.
  3. With a minimal inline dataset file, the module runs against mock_server.
"""
from __future__ import annotations

import json
import pytest
from bench.modules import get_module
from bench.modules.base import ModuleResult
from bench.replay_test.log_replay_tool import build_replay_body, MatchedRequest


def _record(body: dict) -> MatchedRequest:
    """A MatchedRequest wrapping the given request body (as both json + str)."""
    return MatchedRequest(
        source_file="t", line_no=1, level="info", timestamp="",
        request_id="r1", channel_id=None, token_name=None,
        request_body=json.dumps(body), request_json=body, raw_payload={},
    )


def test_replay_descriptor():
    cls = get_module("replay")
    desc = cls.descriptor()
    assert desc["name"] == "replay"
    assert "concurrency" in desc["params_schema"]["properties"]


# --- build_replay_body: usage reporting on streamed requests ------------------
# Regression for input_tpm / cached_tpm reading 0: OpenAI-compatible servers only
# emit a `usage` block (prompt/cached tokens) on streamed responses when
# stream_options.include_usage is set, which collected production requests rarely
# carry. build_replay_body must force it on whenever the request streams.

def test_streaming_request_gets_include_usage():
    out = json.loads(build_replay_body(_record(
        {"model": "old", "stream": True, "messages": []}), "new"))
    assert out["model"] == "new"
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}


def test_force_stream_also_injects_include_usage():
    out = json.loads(build_replay_body(_record(
        {"model": "old", "messages": []}), "new", force_stream=True))
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}


def test_include_usage_forced_true_and_preserves_other_options():
    out = json.loads(build_replay_body(_record(
        {"model": "old", "stream": True,
         "stream_options": {"include_usage": False, "continuous_usage_stats": True},
         "messages": []}), None))
    assert out["stream_options"] == {"include_usage": True, "continuous_usage_stats": True}


def test_non_streaming_request_has_no_stream_options():
    # Non-streaming responses already carry a full usage block; stream_options is
    # invalid there (some servers 400 on it) so it must be absent / stripped.
    out = json.loads(build_replay_body(_record(
        {"model": "old", "stream": False,
         "stream_options": {"include_usage": True}, "messages": []}), None))
    assert "stream_options" not in out


def test_non_json_body_sent_verbatim_when_no_changes():
    rec = MatchedRequest("t", 1, "i", "", "r", None, None, "not json", None, {})
    assert build_replay_body(rec, None) == b"not json"


def test_replay_default_params_valid():
    cls = get_module("replay")
    params = cls.ParamsSchema(dataset_path="/data/replay.jsonl")
    assert params.concurrency == 5
    assert params.clean is False  # opt-in; default preserves byte-for-byte replay


# --- system-message normalization (the `clean` toggle) ------------------------

def test_normalize_merges_consecutive_leading_system():
    from bench.replay_test.log_replay_tool import normalize_system_messages
    msgs = [
        {"role": "system", "content": "meta"},
        {"role": "system", "content": "ident"},
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hi"},
    ]
    out, changed = normalize_system_messages(msgs)
    assert changed is True
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "meta\n\nident\n\nrules"  # order + content preserved


def test_normalize_demotes_non_leading_system_to_user():
    from bench.replay_test.log_replay_tool import normalize_system_messages
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "mid"},
    ]
    out, changed = normalize_system_messages(msgs)
    assert changed is True
    assert [m["role"] for m in out] == ["system", "user", "user"]
    assert out[2]["content"] == "mid"  # content kept, only role changed


def test_normalize_noop_on_single_system():
    from bench.replay_test.log_replay_tool import normalize_system_messages
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out, changed = normalize_system_messages(msgs)
    assert changed is False and out is msgs


def test_clean_toggle_collapses_system_messages():
    body = {"model": "old", "stream": False, "messages": [
        {"role": "system", "content": "a"},
        {"role": "system", "content": "b"},
        {"role": "user", "content": "hi"},
    ]}
    # default (clean=False): byte-for-byte — both system messages survive
    off = json.loads(build_replay_body(_record(body), "new"))
    assert sum(1 for m in off["messages"] if m["role"] == "system") == 2
    # clean=True: collapsed to one leading system message
    on = json.loads(build_replay_body(_record(body), "new", clean=True))
    assert sum(1 for m in on["messages"] if m["role"] == "system") == 1
    assert on["messages"][0] == {"role": "system", "content": "a\n\nb"}


def test_clean_toggle_noop_keeps_request_byte_for_byte():
    body = {"model": "old", "stream": False,
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}
    assert build_replay_body(_record(body), "new", clean=True) == \
           build_replay_body(_record(body), "new", clean=False)


def test_new_replay_metrics_registered():
    cls = get_module("replay")
    names = {d.name for d in cls.metrics_descriptors}
    new = {"http_200_count", "multi_system_requests",
           "multi_system_4xx_errors", "system_normalized_count"}
    assert new <= names
    # every metric must also have a matching config entry
    cfg_keys = {c.key for c in cls.default_metric_configs}
    assert new <= cfg_keys


# --- opt-in LLM answer-quality judge -------------------------------------


def _judge_rec(**kw) -> dict:
    """A retained judge-buffer record with answered-looking defaults."""
    rec = {
        "request_id": "r", "input": "[user] q", "answer": "a", "reasoning": "",
        "finish_reason": "stop", "stop_reason": None, "response_finished": True,
        "generation_capped": False, "status_code": 200, "success": True,
    }
    rec.update(kw)
    return rec


def test_judge_disposition_buckets():
    from bench.tests.functional.replay import judge_disposition
    # Transport failure / HTTP error → error (any 4xx/5xx, not just 400/500)
    assert judge_disposition(_judge_rec(success=False, status_code=None)) == "error"
    assert judge_disposition(_judge_rec(success=False, status_code=429)) == "error"
    assert judge_disposition(_judge_rec(status_code=503, success=False)) == "error"
    # Length truncation: finish_reason, Anthropic stop_reason, or client cap
    assert judge_disposition(_judge_rec(finish_reason="length")) == "length"
    assert judge_disposition(_judge_rec(finish_reason=None, stop_reason="max_tokens")) == "length"
    assert judge_disposition(_judge_rec(generation_capped=True, finish_reason=None,
                                        response_finished=True)) == "length"
    # tool_calls turns are normal agentic rounds (empty text expected)
    assert judge_disposition(_judge_rec(finish_reason="tool_calls")) == "toolcall"
    assert judge_disposition(_judge_rec(finish_reason=None, stop_reason="tool_use")) == "toolcall"
    # Stream ended without any finish marker → disconnect (excluded)
    assert judge_disposition(_judge_rec(finish_reason=None, stop_reason=None,
                                        response_finished=False)) == "disconnect"
    # Normal stop → answered. Crucially, a successful NON-STREAMING reply is
    # answered whenever a finish marker was recorded — the offline pipeline's
    # "finish is None => disconnect" heuristic must not resurface here.
    assert judge_disposition(_judge_rec()) == "answered"
    assert judge_disposition(_judge_rec(finish_reason=None, stop_reason="end_turn")) == "answered"


def test_parse_replay_verdict():
    from bench.tests.functional.replay import _parse_replay_verdict
    # Clean one-line verdict
    v = _parse_replay_verdict('{"task":"RAG问答","quality":"good","halluc":"none","reason":"ok"}')
    assert v == {"task": "RAG问答", "quality": "good", "halluc": "none", "reason": "ok"}
    # Verdict wrapped in prose / drafted twice — the LAST valid object wins
    v = _parse_replay_verdict(
        'thinking… {"quality":"good","halluc":"none"} no wait. '
        'Final: {"task":"x","quality":"poor","halluc":"clear","reason":"编造ID"}')
    assert v["quality"] == "poor" and v["halluc"] == "clear"
    # Loose fallback when the judge answered in prose
    v = _parse_replay_verdict('I would rate quality: acceptable, halluc: suspected overall')
    assert v["quality"] == "acceptable" and v["halluc"] == "suspected"
    # Invalid quality value / garbage → None (counted as judge failure)
    assert _parse_replay_verdict('{"quality":"great"}') is None
    assert _parse_replay_verdict("no verdict here") is None
    assert _parse_replay_verdict("") is None
    # Unknown halluc value degrades to "none" rather than failing the parse
    v = _parse_replay_verdict('{"quality":"good","halluc":"maybe"}')
    assert v["halluc"] == "none"


def test_judge_params_validators():
    cls = get_module("replay")
    # judge off: judge fields may all stay blank
    cls.ParamsSchema(dataset_path="x")
    # enabling requires BOTH url and model (replay never self-judges)
    with pytest.raises(ValueError, match="judge_api_url and judge_model"):
        cls.ParamsSchema(dataset_path="x", judge_enable=True)
    with pytest.raises(ValueError, match="judge_api_url and judge_model"):
        cls.ParamsSchema(dataset_path="x", judge_enable=True, judge_api_url="http://j")
    # url without model is inconsistent even when judging is off
    with pytest.raises(ValueError, match="judge_model is required"):
        cls.ParamsSchema(dataset_path="x", judge_api_url="http://j")
    # a key without a url would silently never be used
    with pytest.raises(ValueError, match="judge_api_key"):
        cls.ParamsSchema(dataset_path="x", judge_api_key="k")
    # whitespace is stripped (a padded key breaks the Authorization header)
    p = cls.ParamsSchema(dataset_path="x", judge_enable=True,
                         judge_api_url=" http://j ", judge_model=" m ", judge_api_key=" k\n")
    assert (p.judge_api_url, p.judge_model, p.judge_api_key) == ("http://j", "m", "k")


def test_judge_metrics_registered():
    from bench.tests.functional.replay import JUDGE_METRIC_KEYS
    cls = get_module("replay")
    names = {d.name for d in cls.metrics_descriptors}
    assert set(JUDGE_METRIC_KEYS) <= names
    cfg = cls.default_metric_configs
    assert set(JUDGE_METRIC_KEYS) <= {c.key for c in cfg}
    # quality gates ship as default redlines (skipped while the judge is off)
    redlines = {c.key for c in cfg if c.role == "redline"}
    assert {"judge_poor_rate", "judge_halluc_clear_rate"} <= redlines


def _mk_test(tmp_path, **judge_kw):
    from bench.tests.functional.replay import ReplayTest
    return ReplayTest(
        api_url="http://target", model="m", api_key="", output_dir=str(tmp_path),
        dataset_path="unused.jsonl", **judge_kw,
    )


def test_judge_disabled_emits_none_metrics_and_redlines_skip(tmp_path):
    from bench.tests.functional.replay import JUDGE_METRIC_KEYS
    from bench.modules.evaluator import check_redlines
    t = _mk_test(tmp_path)  # judge_enable defaults False
    m = t._run_judge_phase()
    assert set(m) == set(JUDGE_METRIC_KEYS)
    assert all(v is None for v in m.values())
    # the shipped judge redlines must not fail a run that never judged
    cls = get_module("replay")
    from dataclasses import asdict
    configs = [asdict(c) for c in cls.default_metric_configs
               if c.role == "redline" and c.key.startswith("judge_")]
    assert configs, "expected default judge redlines"
    assert check_redlines(m, configs) is True


def test_judge_sampling_rate_and_cap(tmp_path):
    # rate=1.0 retains everything until the cap, then stops
    t = _mk_test(tmp_path, judge_enable=True, judge_sample_rate=1.0, judge_max_samples=2,
                 judge_api_url="http://j", judge_model="jm")
    assert [t._judge_sample(f"r{i}") for i in range(5)].count(True) == 2
    # rate=0 never samples
    t0 = _mk_test(tmp_path, judge_enable=True, judge_sample_rate=0.0,
                  judge_api_url="http://j", judge_model="jm")
    assert not any(t0._judge_sample(f"r{i}") for i in range(20))
    # disabled never samples even at rate=1
    toff = _mk_test(tmp_path, judge_sample_rate=1.0)
    assert not any(toff._judge_sample(f"r{i}") for i in range(20))


def test_judge_sampling_deterministic(tmp_path):
    """The sampled subset is a pure function of (judge_seed, request_id):
    identical across runs and endpoints, regardless of draw order."""
    ids = [f"req_{i}" for i in range(200)]
    kw = dict(judge_enable=True, judge_sample_rate=0.5, judge_max_samples=0,
              judge_api_url="http://j", judge_model="jm")
    a = _mk_test(tmp_path, judge_seed=42, **kw)
    b = _mk_test(tmp_path, judge_seed=42, **kw)
    picked_a = {i for i in ids if a._judge_sample(i)}
    # same seed, REVERSED draw order → same subset (order-independence)
    picked_b = {i for i in reversed(ids) if b._judge_sample(i)}
    assert picked_a == picked_b
    # roughly the requested rate (crc32 is uniform; 200 draws @ 0.5)
    assert 60 <= len(picked_a) <= 140
    # a different seed rotates the subset
    c = _mk_test(tmp_path, judge_seed=43, **kw)
    picked_c = {i for i in ids if c._judge_sample(i)}
    assert picked_c != picked_a


def test_judge_transient_retries_and_hard_4xx(tmp_path, monkeypatch):
    """Transient judge errors (transport/429/5xx) retry up to judge_max_retries
    with backoff; a non-retryable 4xx fails immediately."""
    import bench.tests.functional.replay as replay_mod
    from bench.tests.functional.fixed_request_probe import ChatOutcome

    monkeypatch.setattr(replay_mod.time, "sleep", lambda s: None)
    good = '{"task":"t","quality":"good","halluc":"none","reason":"ok"}'

    t = _mk_test(tmp_path, judge_enable=True, judge_sample_rate=1.0,
                 judge_api_url="http://j", judge_model="jm", judge_max_retries=5)
    t._judge_rubric = "r"  # normally set by _judge_setup

    # fails 4x transiently (conn, conn, 429, 500) then succeeds → verdict
    calls = {"n": 0}
    script = [ChatOutcome(ok=False, status=None, error="ConnectionError"),
              ChatOutcome(ok=False, status=None, error="Timeout"),
              ChatOutcome(ok=False, status=429, error="rate limited"),
              ChatOutcome(ok=False, status=503, error="overloaded"),
              ChatOutcome(ok=True, status=200, content=good)]
    monkeypatch.setattr(replay_mod, "send_chat",
                        lambda *a, **k: (script[min(calls["n"], 4)], calls.__setitem__("n", calls["n"] + 1))[0])
    v = t._judge_one(_judge_rec())
    assert v is not None and v["quality"] == "good"
    assert calls["n"] == 5

    # retry budget exhausted → None
    calls["n"] = 0
    monkeypatch.setattr(replay_mod, "send_chat",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         ChatOutcome(ok=False, status=500, error="boom"))[1])
    t2 = _mk_test(tmp_path, judge_enable=True, judge_sample_rate=1.0,
                  judge_api_url="http://j", judge_model="jm", judge_max_retries=2)
    t2._judge_rubric = "r"
    assert t2._judge_one(_judge_rec()) is None
    assert calls["n"] == 3  # initial try + 2 retries

    # non-retryable 4xx → exactly one call, no retries
    calls["n"] = 0
    monkeypatch.setattr(replay_mod, "send_chat",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         ChatOutcome(ok=False, status=401, error="bad key"))[1])
    assert t._judge_one(_judge_rec()) is None
    assert calls["n"] == 1

    # unparseable verdict → exactly one re-ask
    calls["n"] = 0
    monkeypatch.setattr(replay_mod, "send_chat",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         ChatOutcome(ok=True, status=200, content="prose"))[1])
    assert t._judge_one(_judge_rec()) is None
    assert calls["n"] == 2


def test_judge_phase_math(tmp_path, monkeypatch):
    """Canned verdicts through the real pipelined path (setup → submit-per-
    record → drain): bucket tallies, kept denominator (judged + toolcall),
    rate math, and failure counting."""
    import bench.tests.functional.replay as replay_mod
    from bench.tests.functional.fixed_request_probe import ChatOutcome

    verdicts = {
        "v:good": '{"task":"t","quality":"good","halluc":"none","reason":"ok"}',
        "v:acceptable": '{"task":"t","quality":"acceptable","halluc":"suspected","reason":"截断"}',
        "v:poor": '{"task":"t","quality":"poor","halluc":"clear","reason":"编造"}',
        "v:junk": "not a verdict",
    }

    def fake_send_chat(sess, url, payload, timeout):
        body = payload["messages"][1]["content"]
        for marker, reply in verdicts.items():
            if marker in body:
                return ChatOutcome(ok=True, status=200, content=reply)
        return ChatOutcome(ok=False, status=500, error="unexpected judge call")

    # Patch BEFORE submitting: judge calls fire the moment a record is
    # submitted (pipelined), not at drain time.
    monkeypatch.setattr(replay_mod, "send_chat", fake_send_chat)
    monkeypatch.setattr(replay_mod.time, "sleep", lambda s: None)  # skip retry backoff

    t = _mk_test(tmp_path, judge_enable=True, judge_sample_rate=1.0,
                 judge_api_url="http://j", judge_model="jm", judge_concurrency=2)
    t._judge_setup()
    # 4 answered (graded good/acceptable/poor+clear/garbage) + 1 of each other bucket
    for rec in [
        _judge_rec(request_id="good", answer="v:good"),
        _judge_rec(request_id="acc", answer="v:acceptable"),
        _judge_rec(request_id="poor", answer="v:poor"),
        _judge_rec(request_id="junk", answer="v:junk"),
        _judge_rec(request_id="tool", finish_reason="tool_calls", answer=""),
        _judge_rec(request_id="len", finish_reason="length"),
        _judge_rec(request_id="disc", finish_reason=None, response_finished=False),
        _judge_rec(request_id="err", success=False, status_code=None),
    ]:
        t._judge_submit(rec)
    # only the 4 answered records got a judge future
    assert len(t._judge_futures) == 4
    m = t._run_judge_phase()

    assert m["judge_sampled_count"] == 8
    assert m["judge_answered_count"] == 4
    assert m["judge_toolcall_count"] == 1
    assert m["judge_length_count"] == 1
    assert m["judge_disconnect_count"] == 1
    assert m["judge_error_count"] == 1
    assert m["judge_failures"] == 1               # the unparseable one
    # kept = 3 judged + 1 toolcall; toolcall counts good
    assert m["judge_kept_count"] == 4
    assert m["judge_good_acc_rate"] == pytest.approx(3 / 4)
    assert m["judge_poor_rate"] == pytest.approx(1 / 4)
    assert m["judge_halluc_clear_rate"] == pytest.approx(1 / 4)
    assert m["judge_halluc_suspected_count"] == 1


def test_judge_phase_nothing_answered_rates_none(tmp_path):
    """Only excluded buckets sampled → counts populate but rates stay None so
    the default redlines are skipped (a run of pure errors shouldn't 'fail
    quality' — it already fails uptime)."""
    t = _mk_test(tmp_path, judge_enable=True, judge_sample_rate=1.0,
                 judge_api_url="http://j", judge_model="jm")
    # excluded buckets never create judge futures, so no pool/HTTP is needed
    t._judge_submit(_judge_rec(request_id="e", success=False, status_code=None))
    t._judge_submit(_judge_rec(request_id="l", finish_reason="length"))
    m = t._run_judge_phase()
    assert m["judge_sampled_count"] == 2
    assert m["judge_answered_count"] == 0 and m["judge_kept_count"] == 0
    assert m["judge_good_acc_rate"] is None
    assert m["judge_poor_rate"] is None
    assert m["judge_halluc_clear_rate"] is None


@pytest.mark.integration
def test_replay_with_minimal_dataset(mock_endpoint, tmp_path):
    """Run replay against mock_server using a tiny synthetic dataset file."""
    # Create a minimal JSONL dataset matching the MatchedRequest schema
    # (fields required by load_extract_jsonl in log_replay_tool.py)
    dataset_file = tmp_path / "test_replay.jsonl"
    records = []
    for i in range(3):
        record = {
            "source_file": "test",
            "line_no": i + 1,
            "level": "info",
            "timestamp": "2024-01-01T00:00:00Z",
            "request_id": f"req_{i}",
            "channel_id": None,
            "token_name": None,
            "request_body": json.dumps({
                "model": "Kimi-K2.5",
                "messages": [{"role": "user", "content": "Hello, say hi back briefly."}],
                "max_tokens": 32,
                "stream": True,
            }),
            "request_json": None,
            "raw_payload": {},
        }
        records.append(record)

    dataset_file.write_text("\n".join(json.dumps(r) for r in records))

    cls = get_module("replay")
    # The module force-disables the underlying ReplayTest's redline checks
    # internally (passing permissive thresholds via constructor args), so the
    # admin-facing params here only need to cover the actual knobs.
    params = cls.ParamsSchema(
        dataset_path=str(dataset_file),
        concurrency=2,
        max_samples=3,
        request_timeout=30.0,
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    assert isinstance(result, ModuleResult)
    assert 0.0 <= result.score <= 1.0
    assert "total_requests" in result.metrics
    # Streamed requests must still surface input-token usage (the server reports
    # it on the final chunk thanks to stream_options.include_usage). Guards the
    # input_tpm / total_input_tokens pipeline against regressing back to 0.
    assert result.metrics.get("total_input_tokens", 0) > 0
    assert result.metrics.get("input_tpm") not in (None, 0)
    assert result.metrics.get("avg_prompt_tokens") not in (None, 0)
    # Judge is opt-in: with judge_enable unset every judge metric is None.
    assert result.metrics.get("judge_sampled_count") is None
    assert result.metrics.get("judge_poor_rate") is None


@pytest.mark.integration
def test_replay_judge_end_to_end(mock_endpoint, tmp_path):
    """judge_enable + sample_rate=1.0 against mock_server: every request's
    answer is retained and classified, and the judge loop runs. The mock
    server can't emit verdict JSON, so answered samples land in
    judge_failures — which exercises retention, disposition, the judge HTTP
    round-trip, and the failure path in one go."""
    dataset_file = tmp_path / "judge_replay.jsonl"
    records = [{
        "source_file": "test", "line_no": i + 1, "level": "info",
        "timestamp": "2024-01-01T00:00:00Z", "request_id": f"req_{i}",
        "channel_id": None, "token_name": None,
        "request_body": json.dumps({
            "model": "Kimi-K2.5",
            "messages": [{"role": "user", "content": "Hello, say hi back briefly."}],
            "max_tokens": 32,
            "stream": True,
        }),
        "request_json": None, "raw_payload": {},
    } for i in range(3)]
    dataset_file.write_text("\n".join(json.dumps(r) for r in records))

    cls = get_module("replay")
    params = cls.ParamsSchema(
        dataset_path=str(dataset_file),
        concurrency=2,
        max_samples=3,
        request_timeout=30.0,
        judge_enable=True,
        judge_sample_rate=1.0,
        judge_api_url=mock_endpoint.api_url,   # mock server doubles as "judge"
        judge_model=mock_endpoint.model,
        judge_max_tokens=32,
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    assert isinstance(result, ModuleResult)
    m = result.metrics
    assert m["judge_sampled_count"] == 3          # rate=1.0 retains everything
    disp_total = (m["judge_answered_count"] + m["judge_toolcall_count"]
                  + m["judge_length_count"] + m["judge_disconnect_count"]
                  + m["judge_error_count"])
    assert disp_total == m["judge_sampled_count"]
    # mock replies are graded but unparseable as verdicts → every answered
    # sample becomes a judge failure and the rates stay None (kept=0)
    assert m["judge_answered_count"] + m["judge_length_count"] >= 1
    assert m["judge_failures"] == m["judge_answered_count"]
    assert m["judge_kept_count"] == 0
    assert m["judge_good_acc_rate"] is None


# --- HTTP-failure cause classification ------------------------------------


def test_classify_http_error_causes():
    """Server error bodies (real wordings from SGLang/vLLM) map onto cause
    classes, so failure counts explain WHY requests died — not just which
    attributes (tools/images/system shape) the failed request carried."""
    from bench.tests.functional.replay import classify_http_error

    def cls_of(status, body):
        return classify_http_error(status, body)[0]

    # SGLang: prompt alone exceeds the window
    assert cls_of(400, json.dumps({"object": "error", "message":
        "The input (291793 tokens) is longer than the model's context length (262144 tokens)."})) \
        == "ctx_overflow"
    # SGLang: prompt + max_tokens (or server default) exceeds the window
    assert cls_of(400, json.dumps({"object": "error", "message":
        "Requested token count exceeds the model's maximum context length of 262144 tokens."})) \
        == "ctx_overflow"
    # SGLang multimodal processor: broken data URI / unfetchable URL (a 500!)
    assert cls_of(500, json.dumps({"object": "error", "message":
        "Internal server error: An exception occurred while loading IMAGE data at index 0: "
        "Error while loading data ImageData(url='data:application/xml;base64,..'): "
        "cannot identify image file"})) == "image_load"
    # SGLang strict jsonschema validation of tool definitions
    assert cls_of(400, json.dumps({"object": "error", "message":
        "Tool 52 function has invalid 'parameters' schema: None is not of type 'object'"})) \
        == "tool_schema"
    # FastAPI/pydantic request-shape rejects (e.g. integer message content)
    assert cls_of(400, json.dumps({"object": "error", "message":
        "5 validation errors:\n  {'type': 'value_error', 'loc': (...), "
        "'msg': \"Value error, 'role' must be one of 'system', ...\"}"})) \
        == "request_validation"
    # Strict chat template rejecting system-message placement
    assert cls_of(400, "System message must be at the beginning of the conversation") \
        == "multi_system_template"


def test_classify_http_error_status_fallbacks():
    from bench.tests.functional.replay import classify_http_error
    assert classify_http_error(401, "")[0] == "auth"
    assert classify_http_error(404, "")[0] == "not_found"
    assert classify_http_error(413, "")[0] == "payload_too_large"
    assert classify_http_error(422, "")[0] == "request_validation"
    assert classify_http_error(429, '{"message": "slow down"}')[0] == "rate_limited"
    assert classify_http_error(503, "upstream unavailable")[0] == "server_error"
    # Unrecognized 4xx keeps the status visible instead of guessing a cause
    assert classify_http_error(400, "no known pattern here")[0] == "http_400"
    assert classify_http_error(400, None)[0] == "http_400"


def test_classify_http_error_extracts_server_message():
    from bench.tests.functional.replay import classify_http_error
    # OpenAI-style nested error envelope
    _, msg = classify_http_error(400, json.dumps(
        {"error": {"message": "maximum context length exceeded", "type": "invalid_request_error"}}))
    assert msg == "maximum context length exceeded"
    # Non-JSON body: passed through as-is
    _, msg = classify_http_error(400, "plain text error")
    assert msg == "plain text error"


# ---------------------------------------------------------------------------
# Streaming dispatch (_prepare_records / _run_concurrent)
#
# These cover the refactor that made peak memory O(in-flight window) instead of
# O(dataset): records are pulled from disk lazily and released as requests
# finish, so a multi-GB replay file no longer has to fit in RAM.
# ---------------------------------------------------------------------------

def _write_dataset(tmp_path, n, name="ds.jsonl"):
    """A minimal but schema-valid replay JSONL with `n` records (ids r0..r{n-1})."""
    body = json.dumps({
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
        "stream": True,
    })
    lines = [
        json.dumps({
            "source_file": "t", "line_no": i + 1, "level": "info",
            "timestamp": "", "request_id": f"r{i}", "channel_id": None,
            "token_name": None, "request_body": body,
            "request_json": {"parsed": True}, "raw_payload": {"heavy": "x" * 64},
        })
        for i in range(n)
    ]
    p = tmp_path / name
    p.write_text("\n".join(lines))
    return p


def _mk_stream_test(dataset_path, tmp_path, **kw):
    from bench.tests.functional.replay import ReplayTest
    return ReplayTest(
        api_url="http://127.0.0.1:1/v1", model="m", api_key="",
        output_dir=str(tmp_path / "out"), dataset_path=str(dataset_path), **kw,
    )


def test_prepare_records_no_cap_streams_whole_dataset(tmp_path):
    ds = _write_dataset(tmp_path, 12)
    total, make_iter = _mk_stream_test(ds, tmp_path, max_samples=0)._prepare_records()
    assert total == 12
    assert [r.request_id for r in make_iter()] == [f"r{i}" for i in range(12)]


def test_prepare_records_downsamples_even_stride(tmp_path):
    ds = _write_dataset(tmp_path, 100)
    total, make_iter = _mk_stream_test(ds, tmp_path, max_samples=10)._prepare_records()
    assert total == 10
    # Same int(i*step) formula as the eager version: step = 100/10 = 10.
    assert [r.request_id for r in make_iter()] == [f"r{i * 10}" for i in range(10)]


def test_prepare_records_wraps_short_dataset(tmp_path):
    ds = _write_dataset(tmp_path, 3)
    total, make_iter = _mk_stream_test(ds, tmp_path, max_samples=7)._prepare_records()
    assert total == 7
    # Dataset shorter than requested → replayed repeatedly to reach max_samples.
    assert [r.request_id for r in make_iter()] == ["r0", "r1", "r2", "r0", "r1", "r2", "r0"]


def test_prepare_records_missing_or_empty_dataset(tmp_path):
    missing = _mk_stream_test(tmp_path / "nope.jsonl", tmp_path)._prepare_records()
    assert missing == (0, None)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n  \n")
    total, make_iter = _mk_stream_test(empty, tmp_path)._prepare_records()
    assert total == 0 and make_iter is None


def test_prepare_records_unset_path_returns_none(tmp_path):
    assert _mk_stream_test("", tmp_path)._prepare_records() is None


def test_prepare_records_loads_lean(tmp_path):
    """The run path must drop request_json/raw_payload (the OOM amplifiers)."""
    ds = _write_dataset(tmp_path, 4)
    _total, make_iter = _mk_stream_test(ds, tmp_path, max_samples=0)._prepare_records()
    for rec in make_iter():
        assert rec.request_json is None
        assert rec.raw_payload == {}
        assert rec.request_body  # the one field the replay path actually needs


def test_prepare_records_streams_lazily(tmp_path, monkeypatch):
    """Consuming a few records must not parse the whole file (the OOM fix)."""
    import bench.tests.functional.replay as R
    ds = _write_dataset(tmp_path, 200)
    real = R.load_extract_jsonl
    parsed = {"n": 0}

    def counting(path, lean=False):
        for rec in real(path, lean=lean):
            parsed["n"] += 1
            yield rec

    monkeypatch.setattr(R, "load_extract_jsonl", counting)
    total, make_iter = _mk_stream_test(ds, tmp_path, max_samples=0)._prepare_records()
    assert total == 200
    it = make_iter()
    taken = [next(it) for _ in range(3)]
    assert [r.request_id for r in taken] == ["r0", "r1", "r2"]
    # Eager list() loading would have parsed all 200 before returning.
    assert parsed["n"] <= 5, f"not streaming: parsed {parsed['n']} records for 3 taken"
    it.close()


def test_run_concurrent_covers_every_record_in_order(tmp_path, monkeypatch):
    """Streaming dispatch must still fill results[] by position, for all records."""
    ds = _write_dataset(tmp_path, 25)
    test = _mk_stream_test(ds, tmp_path, concurrency=4, max_samples=0)
    total, make_iter = test._prepare_records()

    def fake_send(record, url):
        return {"success": True, "request_id": record.request_id}

    monkeypatch.setattr(test, "_send_request", fake_send)
    results = test._run_concurrent(make_iter, total)

    assert len(results) == total == 25
    assert all(r.get("success") for r in results), "every record should be dispatched"
    # results is indexed by position — the contract _aggregate relies on.
    assert [r["request_id"] for r in results] == [f"r{i}" for i in range(25)]
