"""
Unit tests for the HLE grading path in bench/tests/functional/opencompass.py.

Official HLE (centerforaisafety/hle) grades every answer with an LLM judge and
has no exact-match path. We use the judge when one is configured and fall back
to a local exact-match grader otherwise. These tests cover the judge reply
parsing, the fallback's normalisation rules, the calibration-error port, and the
end-to-end grader selection / degradation behaviour of _run_hle.
"""
from __future__ import annotations

import pytest
import requests

import bench.tests.functional.opencompass as oc
from bench.tests.functional.opencompass import (
    _calibration_error,
    _eval_hle,
    _exact_answer_match,
    _extract_confidence,
    _hle_breakdown,
    _judge_hle,
    _normalize_exact_answer,
    _parse_hle_judgement,
)


# ---------------------------------------------------------------------------
# Judge reply parsing
# ---------------------------------------------------------------------------

def test_parse_judgement_labelled_fields():
    reply = (
        "extracted_final_answer: Paris\n"
        "reasoning: The extracted answer matches the correct answer exactly.\n"
        "correct: yes\n"
        "confidence: 90"
    )
    assert _parse_hle_judgement(reply) == (True, 90)


def test_parse_judgement_no_verdict():
    reply = "extracted_final_answer: Paris\nreasoning: It looks right to me."
    assert _parse_hle_judgement(reply) is None


@pytest.mark.parametrize("reply,expected", [
    ("correct: no\nconfidence: 10", (False, 10)),
    ("correct: YES\nconfidence: 100", (True, 100)),
    ('"correct": "yes", "confidence": 55', (True, 55)),   # judge emitted JSON
    ("correct = yes\nconfidence = 42", (True, 42)),
])
def test_parse_judgement_formats(reply, expected):
    assert _parse_hle_judgement(reply) == expected


def test_parse_judgement_defaults_confidence_to_100():
    """Upstream's template says to put 100 when the response has no score."""
    assert _parse_hle_judgement("correct: yes") == (True, 100)


def test_parse_judgement_verdict_wins_over_reasoning_prose():
    """'correct:' inside the reasoning must not outrank the verdict field."""
    reply = (
        "extracted_final_answer: 42\n"
        "reasoning: One might argue this is correct: no such equivalence exists, "
        "but the values do match.\n"
        "correct: yes\n"
        "confidence: 80"
    )
    assert _parse_hle_judgement(reply) == (True, 80)


def test_parse_judgement_clamps_confidence():
    assert _parse_hle_judgement("correct: yes\nconfidence: 999")[1] == 100


@pytest.mark.parametrize("reply", ["", None, "totally unrelated text"])
def test_parse_judgement_none_inputs(reply):
    assert _parse_hle_judgement(reply) is None


# ---------------------------------------------------------------------------
# Confidence extraction from the model's own response
# ---------------------------------------------------------------------------

def test_extract_confidence_from_official_format():
    resp = "Explanation: because\nAnswer: Paris\nConfidence: 85%"
    assert _extract_confidence(resp) == 85


def test_extract_confidence_defaults_to_100_when_absent():
    assert _extract_confidence("Answer: Paris") == 100


def test_extract_confidence_clamped():
    assert _extract_confidence("Confidence: 250%") == 100


# ---------------------------------------------------------------------------
# Exact-match fallback normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Paris.", "paris"),
    ("  PARIS  ", "paris"),
    ('"Paris"', "paris"),
    (r"$\boxed{\text{Paris}}$.", "paris"),
    (r"\boxed{42}", "42"),
    (r"\(x+1\)", "x+1"),
    (r"\frac{1}{2}", "1/2"),
    ("1,234,567", "1234567"),
    (r"$1{,}234$", "1234"),
    ("−5", "-5"),                      # unicode minus
    (r"\left( a \right)", "( a )"),
])
def test_normalize_exact_answer(raw, expected):
    assert _normalize_exact_answer(raw) == expected


@pytest.mark.parametrize("pred,gold", [
    ("Paris", "paris"),
    ("Paris.", "Paris"),
    (r"$\boxed{\text{Paris}}$", "Paris"),
    ("1,234", "1234"),
    (r"\frac{1}{2}", "1/2"),
    ("2.0", "2"),
    (".5", "0.5"),
    ("5e-1", "0.5"),
])
def test_exact_match_accepts_formatting_differences(pred, gold):
    assert _exact_answer_match(pred, gold)


@pytest.mark.parametrize("pred,gold", [
    ("two", "2"),                    # no semantic reasoning — judge territory
    ("Paris, France", "Paris"),
    ("0.5001", "0.5"),
    ("", "Paris"),
    ("Paris", ""),
    ("42", "43"),
])
def test_exact_match_rejects_non_equivalent(pred, gold):
    assert not _exact_answer_match(pred, gold)


def test_eval_hle_multiple_choice_still_letter_based():
    assert _eval_hle("Explanation: x\nAnswer: C\nConfidence: 50%", "C", "multipleChoice")
    assert not _eval_hle("Explanation: x\nAnswer: D\nConfidence: 50%", "C", "multipleChoice")


def test_eval_hle_exact_answer_uses_normalisation():
    resp = "Explanation: reasoning here\nAnswer: $\\boxed{1{,}234}$\nConfidence: 70%"
    assert _eval_hle(resp, "1234", "exactMatch")


def test_eval_hle_reads_last_answer_tag():
    """The Explanation may itself contain the word 'answer:'."""
    resp = ("Explanation: the answer: could be Berlin at first glance\n"
            "Answer: Paris\nConfidence: 60%")
    assert _eval_hle(resp, "Paris", "exactMatch")


# ---------------------------------------------------------------------------
# Calibration error (port of upstream calib_err, p='2', beta=100)
# ---------------------------------------------------------------------------

def test_calibration_error_perfectly_wrong_and_certain():
    assert _calibration_error([1.0] * 4, [False] * 4) == pytest.approx(1.0)


def test_calibration_error_perfectly_calibrated():
    assert _calibration_error([1.0] * 4, [True] * 4) == pytest.approx(0.0)


def test_calibration_error_single_bin_uses_means():
    # mean confidence 0.5, mean correctness 0.5 -> no gap
    assert _calibration_error([0.9] * 5 + [0.1] * 5,
                              [True] * 5 + [False] * 5) == pytest.approx(0.0)


def test_calibration_error_drops_upstream_final_bin():
    """Upstream iterates range(len(bins)-1), skipping its last bin. We keep that.

    250 samples -> bins of 100/100/50; only the first two are scored, and the
    weights therefore sum to 200/250 rather than 1.
    """
    conf = [0.0] * 200 + [1.0] * 50
    corr = [False] * 200 + [False] * 50   # last bin is maximally miscalibrated
    # If the final bin counted, its 50 samples at confidence 1.0 / correct 0
    # would contribute; it is skipped, and the first two bins are perfect.
    assert _calibration_error(conf, corr) == pytest.approx(0.0)


def test_calibration_error_handles_short_runs():
    """Upstream raises IndexError below beta samples; a sample-capped run needs a value."""
    assert _calibration_error([0.8, 0.2], [True, False]) is not None


def test_calibration_error_empty_and_mismatched():
    assert _calibration_error([], []) is None
    assert _calibration_error([0.5], [True, False]) is None


# ---------------------------------------------------------------------------
# HLE metric set
# ---------------------------------------------------------------------------

def _judged(rows):
    return {(i, 0): v for i, v in enumerate(rows)}


def test_hle_breakdown_accuracy_over_scored_requests():
    m = _hle_breakdown(_judged([(True, 90), (False, 50), (True, 70)]), n_items=4)
    assert m["scored"] == 4          # one request never produced a judgement
    assert m["graded"] == 3
    assert m["accuracy"] == pytest.approx(2 / 4)
    assert m["ungraded_rate"] == pytest.approx(1 / 4)


def test_hle_breakdown_excludes_samples_beyond_avg_k():
    judged = {
        (0, 0): (True, 90), (0, 1): (True, 90),
        (1, 0): (False, 20), (1, 1): (True, 90),
    }
    m = _hle_breakdown(judged, n_items=2, avg_k=1)
    assert m["scored"] == 2
    assert m["accuracy"] == pytest.approx(1 / 2)


def test_hle_breakdown_reports_calibration_and_ci():
    m = _hle_breakdown(_judged([(True, 100), (False, 100)]), n_items=2)
    assert m["accuracy"] == pytest.approx(0.5)
    assert m["confidence_mean"] == pytest.approx(1.0)
    assert m["calibration_error"] == pytest.approx(50.0)   # 0-100 scale
    assert m["accuracy_ci95_half_width"] > 0


def test_hle_breakdown_empty():
    assert _hle_breakdown({}, 10) == {}
    assert _hle_breakdown(_judged([(True, 90)]), 0) == {}


# ---------------------------------------------------------------------------
# _judge_hle degradation — a judge problem must fall back, never score wrong
# ---------------------------------------------------------------------------

def _stats():
    import threading
    return {"judge_failures": 0, "judge_unparsed": 0, "fallbacks": 0,
            "lock": threading.Lock()}


def test_judge_hle_returns_none_on_call_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("judge down")

    monkeypatch.setattr(oc, "_judge_call", boom)
    stats = _stats()
    assert _judge_hle("u", "m", "k", 2048, "q", "gold", "resp", 10.0, stats=stats) is None
    assert stats["judge_failures"] == 1


def test_judge_hle_returns_none_on_unparseable_reply(monkeypatch):
    monkeypatch.setattr(oc, "_judge_call", lambda *a, **k: "I am still thinking...")
    stats = _stats()
    assert _judge_hle("u", "m", "k", 2048, "q", "gold", "resp", 10.0, stats=stats) is None
    assert stats["judge_unparsed"] == 1


def test_judge_hle_fills_template(monkeypatch):
    seen = {}

    def capture(url, model, key, max_tokens, prompt, timeout, label="judge"):
        seen["prompt"] = prompt
        return "correct: yes\nconfidence: 88"

    monkeypatch.setattr(oc, "_judge_call", capture)
    assert _judge_hle("u", "m", "k", 2048, "Capital of France?", "Paris",
                      "Answer: Paris", 10.0) == (True, 88)
    assert "Capital of France?" in seen["prompt"]
    assert "[correct_answer]: Paris" in seen["prompt"]
    assert "Answer: Paris" in seen["prompt"]


# ---------------------------------------------------------------------------
# _run_hle grader selection
# ---------------------------------------------------------------------------

@pytest.fixture
def hle_items(monkeypatch):
    items = [
        {"question": "Capital of France?", "answer": "Paris", "answer_type": "exactMatch"},
        {"question": "2+2?", "answer": "4", "answer_type": "exactMatch"},
    ]
    monkeypatch.setattr(oc, "_load_hle", lambda data_dir, cap: items)
    return items


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)


def _model_answers(answers):
    """Fake _chat_complete replying with the official Explanation/Answer/Confidence."""
    def fake(api_url, model, api_key, msgs, **kw):
        q = msgs[-1]["content"]
        ans = answers.get(q, "unknown")
        return f"Explanation: because\nAnswer: {ans}\nConfidence: 80%", {}
    return fake


def test_run_hle_uses_exact_match_when_no_judge(monkeypatch, hle_items):
    monkeypatch.setattr(oc, "_chat_complete",
                        _model_answers({"Capital of France?": "Paris", "2+2?": "5"}))
    res = oc._run_hle("u", "m", "k", "/nope", 2, 10.0, 0, 512)
    assert res["grader"] == "exact_match"
    assert res["score"] == pytest.approx(0.5)
    assert res["confidence_mean"] == pytest.approx(0.8)
    assert "judge_failures" not in res


def test_run_hle_uses_judge_when_configured(monkeypatch, hle_items):
    monkeypatch.setattr(oc, "_chat_complete",
                        _model_answers({"Capital of France?": "the city of Paris",
                                        "2+2?": "four"}))
    # A judge accepts both answers that exact-match would reject.
    monkeypatch.setattr(oc, "_judge_call",
                        lambda *a, **k: "correct: yes\nconfidence: 80")
    res = oc._run_hle("u", "m", "k", "/nope", 2, 10.0, 0, 512,
                      judge_api_url="http://judge", judge_model="grader")
    assert res["grader"] == "llm_judge"
    assert res["score"] == pytest.approx(1.0)
    assert res["judge_fallbacks"] == 0


def test_run_hle_judge_disabled_falls_back_even_when_configured(monkeypatch, hle_items):
    monkeypatch.setattr(oc, "_chat_complete",
                        _model_answers({"Capital of France?": "Paris", "2+2?": "4"}))

    def should_not_be_called(*a, **k):
        raise AssertionError("judge must not be called when hle_use_judge is off")

    monkeypatch.setattr(oc, "_judge_call", should_not_be_called)
    res = oc._run_hle("u", "m", "k", "/nope", 2, 10.0, 0, 512,
                      judge_api_url="http://judge", judge_model="grader",
                      use_judge=False)
    assert res["grader"] == "exact_match"
    assert res["score"] == pytest.approx(1.0)


def test_run_hle_preflight_raises_on_broken_judge(monkeypatch, hle_items):
    monkeypatch.setattr(oc, "_chat_complete", _model_answers({}))

    def boom(*a, **k):
        raise requests.HTTPError("401 unauthorized")

    monkeypatch.setattr(oc, "_judge_call", boom)
    with pytest.raises(RuntimeError, match="HLE judge preflight failed"):
        oc._run_hle("u", "m", "k", "/nope", 2, 10.0, 0, 512,
                    judge_api_url="http://judge", judge_model="grader")


def test_run_hle_preflight_raises_on_unparseable_judge(monkeypatch, hle_items):
    monkeypatch.setattr(oc, "_chat_complete", _model_answers({}))
    monkeypatch.setattr(oc, "_judge_call", lambda *a, **k: "hmm, let me think")
    with pytest.raises(RuntimeError, match="no 'correct: yes\\|no' verdict"):
        oc._run_hle("u", "m", "k", "/nope", 2, 10.0, 0, 512,
                    judge_api_url="http://judge", judge_model="grader")


def test_run_hle_mid_run_judge_failure_falls_back_to_exact_match(monkeypatch, hle_items):
    """A judge that dies after preflight must degrade to exact-match, not to wrong."""
    monkeypatch.setattr(oc, "_chat_complete",
                        _model_answers({"Capital of France?": "Paris", "2+2?": "4"}))
    calls = {"n": 0}

    def preflight_then_die(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:                     # the preflight probe
            return "correct: yes\nconfidence: 100"
        raise requests.ConnectionError("judge died")

    monkeypatch.setattr(oc, "_judge_call", preflight_then_die)
    res = oc._run_hle("u", "m", "k", "/nope", 2, 10.0, 0, 512,
                      judge_api_url="http://judge", judge_model="grader")
    assert res["grader"] == "llm_judge"
    assert res["judge_fallbacks"] == 2
    assert res["judge_failures"] == 2
    # Both answers are literally correct, so the fallback still scores them right.
    assert res["score"] == pytest.approx(1.0)
