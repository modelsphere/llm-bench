"""
Unit tests for the SimpleQA judge path in bench/tests/functional/opencompass.py.

These cover the three pure-ish pieces that decide a SimpleQA score and that no
integration test exercises: the A/B/C grade parser, the judge retry policy's
transient-vs-hard classification, and the simple-evals metric math.
"""
from __future__ import annotations

import pytest
import requests

from bench.tests.functional.opencompass import (
    _grade_simpleqa,
    _judge_call,
    _parse_simpleqa_grade,
    _simpleqa_breakdown,
)


# ---------------------------------------------------------------------------
# Grade parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply,expected", [
    # The instructed format: a bare letter.
    ("A", "A"),
    ("B", "B"),
    ("C", "C"),
    ("  A  \n", "A"),
    # Light decoration a chat model adds unprompted.
    ("(A)", "A"),
    ("**B**", "B"),
    ("C.", "C"),
    ('"A"', "A"),
    ("Grade: B", "B"),
    ("Final answer: C", "C"),
    ("The verdict is A", "A"),
    # Prose that ends on the verdict line.
    ("The predicted answer names both children.\n\nA", "A"),
])
def test_parse_grade_wellformed(reply, expected):
    assert _parse_simpleqa_grade(reply) == expected


def test_parse_grade_prefers_final_line_over_earlier_letters():
    """Letters in the judge's recap must not outrank the verdict it ends on.

    Upstream simple-evals takes the FIRST A/B/C match, which grades this 'B'
    off the recap of the grading legend.
    """
    reply = (
        "Recall the options: A: CORRECT, B: INCORRECT, C: NOT_ATTEMPTED.\n"
        "The gold target is fully contained in the prediction.\n"
        "A"
    )
    assert _parse_simpleqa_grade(reply) == "A"


def test_parse_grade_ignores_letters_inside_words():
    """'C' in CORRECT / INCORRECT is not a standalone grade."""
    assert _parse_simpleqa_grade("This is INCORRECT because the date differs.\nB") == "B"


def test_parse_grade_falls_back_to_last_standalone_letter():
    """No anchored verdict line — fall back rather than discard the grade."""
    assert _parse_simpleqa_grade("I would grade this B, since the year is wrong.") == "B"


@pytest.mark.parametrize("reply", [
    "",
    "   \n\n  ",
    "The answer looks right to me.",
    "Der Wert ist korrekt.",
])
def test_parse_grade_none_when_absent(reply):
    assert _parse_simpleqa_grade(reply) is None


def test_parse_grade_handles_none_input():
    assert _parse_simpleqa_grade(None) is None


# ---------------------------------------------------------------------------
# _grade_simpleqa fallbacks — every failure must become 'C', never 'A'
# ---------------------------------------------------------------------------

def _stats():
    import threading
    return {"judge_failures": 0, "judge_unparsed": 0, "lock": threading.Lock()}


def test_grade_falls_back_to_c_on_unparseable_reply(monkeypatch):
    import bench.tests.functional.opencompass as oc
    monkeypatch.setattr(oc, "_judge_call", lambda *a, **k: "I cannot decide.")
    stats = _stats()
    grade = _grade_simpleqa("u", "m", "k", 256, "q", "t", "p", 10.0, stats=stats)
    assert grade == "C"
    assert stats["judge_unparsed"] == 1
    assert stats["judge_failures"] == 0


def test_grade_falls_back_to_c_on_judge_exception(monkeypatch):
    import bench.tests.functional.opencompass as oc

    def boom(*a, **k):
        raise requests.ConnectionError("judge unreachable")

    monkeypatch.setattr(oc, "_judge_call", boom)
    stats = _stats()
    grade = _grade_simpleqa("u", "m", "k", 256, "q", "t", "p", 10.0, stats=stats)
    assert grade == "C"
    assert stats["judge_failures"] == 1


def test_grade_passes_question_target_prediction_into_template(monkeypatch):
    import bench.tests.functional.opencompass as oc
    seen = {}

    def capture(url, model, key, max_tokens, prompt, timeout, label="judge"):
        seen["prompt"] = prompt
        return "A"

    monkeypatch.setattr(oc, "_judge_call", capture)
    assert _grade_simpleqa("u", "m", "k", 256,
                           "Who wrote Dune?", "Frank Herbert",
                           "Frank Herbert wrote it.", 10.0) == "A"
    assert "Who wrote Dune?" in seen["prompt"]
    assert "Frank Herbert wrote it." in seen["prompt"]


# ---------------------------------------------------------------------------
# Judge retry policy
# ---------------------------------------------------------------------------

def _http_error(status: int) -> Exception:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retry backoff must not slow the suite."""
    import bench.tests.functional.opencompass as oc
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)


@pytest.mark.parametrize("exc", [
    requests.Timeout("timed out"),
    requests.ConnectionError("reset by peer"),
    _http_error(429),
    _http_error(500),
    _http_error(503),
])
def test_judge_retries_transient_errors(monkeypatch, exc):
    import bench.tests.functional.opencompass as oc
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise exc
        return ("A", {})

    monkeypatch.setattr(oc, "_chat_complete", flaky)
    assert _judge_call("u", "m", "k", 256, "prompt", 10.0) == "A"
    assert calls["n"] == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_judge_does_not_retry_hard_errors(monkeypatch, status):
    """A bad key/URL/model never heals — fail on the first attempt."""
    import bench.tests.functional.opencompass as oc
    calls = {"n": 0}

    def hard(*a, **k):
        calls["n"] += 1
        raise _http_error(status)

    monkeypatch.setattr(oc, "_chat_complete", hard)
    with pytest.raises(requests.HTTPError):
        _judge_call("u", "m", "k", 256, "prompt", 10.0)
    assert calls["n"] == 1


def test_judge_gives_up_after_attempt_limit(monkeypatch):
    import bench.tests.functional.opencompass as oc
    calls = {"n": 0}

    def always_timeout(*a, **k):
        calls["n"] += 1
        raise requests.Timeout("timed out")

    monkeypatch.setattr(oc, "_chat_complete", always_timeout)
    with pytest.raises(requests.Timeout):
        _judge_call("u", "m", "k", 256, "prompt", 10.0)
    assert calls["n"] == oc._JUDGE_ATTEMPTS


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------

def _single_shot(grades):
    """Grades for n items, one sample each — the avg_k=1 default."""
    return {(i, 0): g for i, g in enumerate(grades)}


def test_breakdown_empty_inputs():
    assert _simpleqa_breakdown({}, 0) == {}
    assert _simpleqa_breakdown({}, 10) == {}
    assert _simpleqa_breakdown(_single_shot(["A"]), 0) == {}


def test_breakdown_rates_sum_to_one():
    m = _simpleqa_breakdown(_single_shot(["A", "A", "B", "C"]), 5)  # 1 never graded
    total = (m["correct_rate"] + m["incorrect_rate"]
             + m["not_attempted_rate"] + m["ungraded_rate"])
    assert total == pytest.approx(1.0)


def test_breakdown_correct_rate_matches_score_denominator():
    """correct_rate is over SCORED requests, so it equals the headline score."""
    m = _simpleqa_breakdown(_single_shot(["A", "A", "B", "C"]), 8)
    assert m["correct_rate"] == pytest.approx(2 / 8)
    assert m["ungraded_rate"] == pytest.approx(4 / 8)
    assert m["graded"] == 4
    assert m["scored"] == 8


def test_breakdown_accuracy_given_attempted_excludes_not_attempted():
    m = _simpleqa_breakdown(_single_shot(["A", "A", "B", "C", "C"]), 5)
    # attempted = 2 CORRECT + 1 INCORRECT
    assert m["accuracy_given_attempted"] == pytest.approx(2 / 3)
    assert m["correct_rate"] == pytest.approx(2 / 5)


def test_breakdown_f1_is_harmonic_mean():
    m = _simpleqa_breakdown(_single_shot(["A", "A", "B", "C", "C"]), 5)
    cr, ga = m["correct_rate"], m["accuracy_given_attempted"]
    assert m["f1"] == pytest.approx(2 * cr * ga / (cr + ga))


def test_breakdown_f1_zero_when_nothing_correct():
    m = _simpleqa_breakdown(_single_shot(["B", "C"]), 2)
    assert m["correct_rate"] == 0.0
    assert m["accuracy_given_attempted"] == 0.0
    assert m["f1"] == 0.0


def test_breakdown_all_not_attempted_does_not_divide_by_zero():
    m = _simpleqa_breakdown(_single_shot(["C", "C", "C"]), 3)
    assert m["not_attempted_rate"] == 1.0
    assert m["accuracy_given_attempted"] == 0.0
    assert m["f1"] == 0.0


def test_breakdown_perfect_score():
    m = _simpleqa_breakdown(_single_shot(["A", "A", "A"]), 3)
    assert m["correct_rate"] == 1.0
    assert m["accuracy_given_attempted"] == 1.0
    assert m["f1"] == pytest.approx(1.0)
    assert m["ungraded_rate"] == 0.0


# --- avg_k slicing: the breakdown must cover exactly what `score` averages ---

def test_breakdown_excludes_samples_beyond_avg_k():
    """pass_k > avg_k draws extra samples; they must not enter the breakdown.

    2 items x 3 drawn samples, but avg_k=1: only sample 0 of each item counts,
    so this is 1 CORRECT out of 2 scored — not 4 out of 6.
    """
    grades = {
        (0, 0): "A", (0, 1): "A", (0, 2): "A",
        (1, 0): "B", (1, 1): "A", (1, 2): "B",
    }
    m = _simpleqa_breakdown(grades, n_items=2, avg_k=1)
    assert m["scored"] == 2
    assert m["graded"] == 2
    assert m["correct_rate"] == pytest.approx(1 / 2)


def test_breakdown_counts_all_samples_when_avg_k_matches():
    grades = {
        (0, 0): "A", (0, 1): "B",
        (1, 0): "A", (1, 1): "A",
    }
    m = _simpleqa_breakdown(grades, n_items=2, avg_k=2)
    assert m["scored"] == 4
    assert m["graded"] == 4
    assert m["correct_rate"] == pytest.approx(3 / 4)


def test_breakdown_avg_k_clamped_to_at_least_one():
    m = _simpleqa_breakdown({(0, 0): "A"}, n_items=1, avg_k=0)
    assert m["scored"] == 1
    assert m["correct_rate"] == 1.0


def test_breakdown_ungraded_from_failed_inference_with_repeats():
    """avg_k=2 over 2 items = 4 scored; one request failed, so 3 grades."""
    grades = {(0, 0): "A", (0, 1): "B", (1, 0): "A"}
    m = _simpleqa_breakdown(grades, n_items=2, avg_k=2)
    assert m["scored"] == 4
    assert m["graded"] == 3
    assert m["ungraded_rate"] == pytest.approx(1 / 4)
    assert m["correct_rate"] == pytest.approx(2 / 4)


# ---------------------------------------------------------------------------
# _run_benchmark dispatch — evaluators that want sample coordinates get them
# ---------------------------------------------------------------------------

def test_run_benchmark_passes_coords_to_four_arg_evaluator(monkeypatch):
    import bench.tests.functional.opencompass as oc
    monkeypatch.setattr(oc, "_chat_complete", lambda *a, **k: ("resp", {}))
    seen = []

    def evaluate(response, item, idx, sample):
        seen.append((item["id"], idx, sample))
        return True

    oc._run_benchmark("t", [{"id": 10}, {"id": 11}], lambda item: [], evaluate,
                      "u", "m", "k", 2, 10.0, avg_k=1, pass_k=2)
    assert sorted(seen) == [(10, 0, 0), (10, 0, 1), (11, 1, 0), (11, 1, 1)]


def test_run_benchmark_still_calls_two_arg_evaluators(monkeypatch):
    """Every other benchmark's evaluator keeps the 2-arg signature."""
    import bench.tests.functional.opencompass as oc
    monkeypatch.setattr(oc, "_chat_complete", lambda *a, **k: ("resp", {}))
    seen = []

    def evaluate(response, item):
        seen.append(item["id"])
        return item["id"] == 10

    res = oc._run_benchmark("t", [{"id": 10}, {"id": 11}], lambda item: [], evaluate,
                            "u", "m", "k", 2, 10.0)
    assert sorted(seen) == [10, 11]
    assert res["score"] == pytest.approx(0.5)
