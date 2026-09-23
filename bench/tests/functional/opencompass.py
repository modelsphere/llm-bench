"""
Academic benchmark evaluation (self-implemented)

Evaluates the model against 8 academic benchmarks by sending requests directly
to the API endpoint and scoring the responses:

  IFEval          — instruction-following (rule-based)
  MMLU-Pro        — 10-option multiple-choice knowledge (CoT)
  GPQA-Diamond    — graduate-level 4-option MCQ (science reasoning)
  HLE             — hard reasoning (exact-match for text answers)
  AIME2025        — competition math (integer answer extraction)
  LiveCodeBenchV6 — code generation (execution against public tests)
  SimpleQA        — short-form factuality (LLM-graded: correct/incorrect/not-attempted)
  LongBench-v2    — long-context 4-option MCQ (middle-truncated context)

All datasets are loaded from ACADEMIC_DATA_DIR (defaults to
dataset/opencompass/data relative to the repo root).

Env vars:
  ACADEMIC_DATA_DIR        Path to opencompass data root (default: dataset/opencompass/data)
  ACADEMIC_MAX_WORKERS     Parallel inference workers (default: 16)
  ACADEMIC_REQUEST_TIMEOUT Per-request timeout in seconds (default: 120)
  ACADEMIC_MAX_TOKENS      Max tokens per inference call (default: 4096)
  ACADEMIC_BASELINE_FILE   JSON file with baseline scores for regression check
  ACADEMIC_MAX_REGRESSION  Max allowed score drop vs baseline (default: 0.02)

  Per-benchmark sample caps (0 = use all):
  ACADEMIC_AIME_SAMPLES     (default: 0)
  ACADEMIC_GPQA_SAMPLES     (default: 0)
  ACADEMIC_IFEVAL_SAMPLES   (default: 0)
  ACADEMIC_MMLU_SAMPLES     (default: 0)
  ACADEMIC_HLE_SAMPLES      (default: 0 — 2500 rows; set e.g. 200 for fast run)
  ACADEMIC_LCB_SAMPLES      (default: 0)
  ACADEMIC_SIMPLEQA_SAMPLES (default: 0 — 4326 rows)
  ACADEMIC_LONGBENCH_SAMPLES (default: 0 — 503 rows)

  Per-benchmark skip flags (set to 1 to skip entirely):
  ACADEMIC_SKIP_AIME        (default: 0)
  ACADEMIC_SKIP_GPQA        (default: 0)
  ACADEMIC_SKIP_IFEVAL      (default: 0)
  ACADEMIC_SKIP_MMLU        (default: 0)
  ACADEMIC_SKIP_HLE         (default: 0)
  ACADEMIC_SKIP_LCB         (default: 0)
  ACADEMIC_SKIP_SIMPLEQA    (default: 0)
  ACADEMIC_SKIP_LONGBENCH   (default: 0)

  LLM judge, shared by SimpleQA and HLE. Blank judge => SimpleQA self-judges
  with the model under test; HLE falls back to local exact-match grading:
  ACADEMIC_JUDGE_API_URL    (default: "" — SimpleQA self-judge / HLE exact-match)
  ACADEMIC_JUDGE_MODEL      (default: "")
  ACADEMIC_JUDGE_API_KEY    (default: "")
  ACADEMIC_JUDGE_MAX_TOKENS SimpleQA judgement budget (default: 256)
  ACADEMIC_HLE_USE_JUDGE    Grade HLE with the judge when one is configured,
                            as official HLE does (default: 1; 0 = exact-match)
  ACADEMIC_HLE_JUDGE_MAX_TOKENS  HLE judgement budget — bigger than SimpleQA's
                            because the judge reasons before its verdict (default: 2048)

  LongBench-v2 context truncation:
  ACADEMIC_LONGBENCH_MAX_INPUT_TOKENS  Middle-truncate context to this many
                               tokens (keep head+tail; 0 = no truncation). (default: 120000)
                               Auto-clamped to fit max_model_len - output budget.
  ACADEMIC_LONGBENCH_MAX_OUTPUT_TOKENS Output-token cap for LongBench-v2 only,
                               overriding the suite max_tokens (0 = inherit). (default: 8192)

  Per-benchmark repeat sampling — each item is queried k times and scored as
  avg@k (mean accuracy over k samples) and pass@k (≥1 of k samples correct).
  avg@k is the reported score; both are saved in the per-benchmark details.
  k=1 reproduces the old single-shot accuracy exactly.
  ACADEMIC_AIME_AVG_K       (default: 32)
  ACADEMIC_GPQA_AVG_K       (default: 8)
  ACADEMIC_IFEVAL_AVG_K     (default: 1)
  ACADEMIC_MMLU_AVG_K       (default: 1)
  ACADEMIC_HLE_AVG_K        (default: 1)
  ACADEMIC_LCB_AVG_K        (default: 1)
  ACADEMIC_SIMPLEQA_AVG_K   (default: 1)
  ACADEMIC_LONGBENCH_AVG_K  (default: 1)
  ACADEMIC_AIME_PASS_K  / _GPQA_PASS_K / _IFEVAL_PASS_K /
  ACADEMIC_MMLU_PASS_K  / _HLE_PASS_K  / _LCB_PASS_K /
  ACADEMIC_SIMPLEQA_PASS_K / _LONGBENCH_PASS_K           (all default: 1)
  ACADEMIC_SAMPLE_TEMPERATURE  Sampling temperature applied whenever a
                               benchmark's max(avg_k, pass_k) > 1, so repeated
                               samples differ. k=1 stays greedy (temp 0). (default: 1.0)
  ACADEMIC_SAMPLE_TOP_P        Nucleus top_p applied alongside the sampling
                               temperature when k>1. k=1 stays at 1.0. (default: 0.95)
  ACADEMIC_FORCE_TEMPERATURE   Apply the sampling temperature/top_p to every
                               benchmark regardless of k, so even single-shot (k=1)
                               benchmarks sample instead of greedy decoding.
                               (default: 0 = off; k=1 stays greedy)

Run it as the `opencompass` module of a benchmark; fetch the data first with
scripts/fetch_academic_datasets.py.

Mock server test (fast CI check):
  ACADEMIC_AIME_SAMPLES=5 ACADEMIC_HLE_SAMPLES=5 \\
  bash scripts/test_quality.sh --test b
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import inspect
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from bench.result import TestResult
from bench.tests.base import BaseTest
from utils.api import join_endpoint
from utils.logger import logger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DATA_DIR = str(_REPO_ROOT / "dataset" / "opencompass" / "data")

# Module-level fallback defaults — env is read ONCE here, at module import,
# so these values reflect whatever was set when the worker process started.
# CLI / short-lived processes can rely on these. Long-lived processes (the
# platform's dramatiq workers) MUST pass every value through the constructor,
# because changing os.environ after import has no effect on the names below.
# See bench/tests/functional/replay.py for the same trap and the fix template.
ACADEMIC_DATA_DIR = os.getenv("ACADEMIC_DATA_DIR", _DEFAULT_DATA_DIR)
MAX_WORKERS = int(os.getenv("ACADEMIC_MAX_WORKERS", "16"))
REQUEST_TIMEOUT = float(os.getenv("ACADEMIC_REQUEST_TIMEOUT", "120"))
# Hard wall-clock cap for the whole module (all selected benchmarks). 0 = off.
MAX_SECONDS = float(os.getenv("ACADEMIC_MAX_SECONDS", str(16 * 3600)))  # 16h
MAX_TOKENS = int(os.getenv("ACADEMIC_MAX_TOKENS", "4096"))
BASELINE_FILE = os.getenv("ACADEMIC_BASELINE_FILE", "")
MAX_REGRESSION = float(os.getenv("ACADEMIC_MAX_REGRESSION", "0.02"))

_SAMPLE_CAPS = {
    "aime2025": int(os.getenv("ACADEMIC_AIME_SAMPLES", "0")),
    "gpqa_diamond": int(os.getenv("ACADEMIC_GPQA_SAMPLES", "0")),
    "ifeval": int(os.getenv("ACADEMIC_IFEVAL_SAMPLES", "0")),
    "mmlu_pro": int(os.getenv("ACADEMIC_MMLU_SAMPLES", "0")),
    "hle": int(os.getenv("ACADEMIC_HLE_SAMPLES", "0")),
    "livecodebench_v6": int(os.getenv("ACADEMIC_LCB_SAMPLES", "0")),
    "simpleqa": int(os.getenv("ACADEMIC_SIMPLEQA_SAMPLES", "0")),
    "longbench_v2": int(os.getenv("ACADEMIC_LONGBENCH_SAMPLES", "0")),
}

_SKIP_FLAGS = {
    "aime2025":         bool(int(os.getenv("ACADEMIC_SKIP_AIME", "0"))),
    "gpqa_diamond":     bool(int(os.getenv("ACADEMIC_SKIP_GPQA", "0"))),
    "ifeval":           bool(int(os.getenv("ACADEMIC_SKIP_IFEVAL", "0"))),
    "mmlu_pro":         bool(int(os.getenv("ACADEMIC_SKIP_MMLU", "0"))),
    "hle":              bool(int(os.getenv("ACADEMIC_SKIP_HLE", "0"))),
    "livecodebench_v6": bool(int(os.getenv("ACADEMIC_SKIP_LCB", "0"))),
    "simpleqa":         bool(int(os.getenv("ACADEMIC_SKIP_SIMPLEQA", "0"))),
    "longbench_v2":     bool(int(os.getenv("ACADEMIC_SKIP_LONGBENCH", "0"))),
}

# Repeat-sampling defaults. avg@k averages accuracy over k samples per item;
# pass@k counts an item correct if any of its k samples is. The standard
# rigorous protocol uses many samples for high-variance reasoning benchmarks
# (AIME avg@32 à la MathArena, GPQA avg@8 à la simple-evals) and a single
# greedy shot for the lower-variance ones.
_AVG_K = {
    "aime2025":         int(os.getenv("ACADEMIC_AIME_AVG_K", "32")),
    "gpqa_diamond":     int(os.getenv("ACADEMIC_GPQA_AVG_K", "8")),
    "ifeval":           int(os.getenv("ACADEMIC_IFEVAL_AVG_K", "1")),
    "mmlu_pro":         int(os.getenv("ACADEMIC_MMLU_AVG_K", "1")),
    "hle":              int(os.getenv("ACADEMIC_HLE_AVG_K", "1")),
    "livecodebench_v6": int(os.getenv("ACADEMIC_LCB_AVG_K", "1")),
    "simpleqa":         int(os.getenv("ACADEMIC_SIMPLEQA_AVG_K", "1")),
    "longbench_v2":     int(os.getenv("ACADEMIC_LONGBENCH_AVG_K", "1")),
}

_PASS_K = {
    "aime2025":         int(os.getenv("ACADEMIC_AIME_PASS_K", "1")),
    "gpqa_diamond":     int(os.getenv("ACADEMIC_GPQA_PASS_K", "1")),
    "ifeval":           int(os.getenv("ACADEMIC_IFEVAL_PASS_K", "1")),
    "mmlu_pro":         int(os.getenv("ACADEMIC_MMLU_PASS_K", "1")),
    "hle":              int(os.getenv("ACADEMIC_HLE_PASS_K", "1")),
    "livecodebench_v6": int(os.getenv("ACADEMIC_LCB_PASS_K", "1")),
    "simpleqa":         int(os.getenv("ACADEMIC_SIMPLEQA_PASS_K", "1")),
    "longbench_v2":     int(os.getenv("ACADEMIC_LONGBENCH_PASS_K", "1")),
}

# Sampling nucleus applied only when a benchmark draws >1 sample per item, so
# the repeats actually differ. Default temp 1.0 / top_p 0.95 — the diversity
# setup common to AIME avg@k/pass@k harnesses. Single-shot benchmarks stay
# greedy (temp 0, top_p 1.0).
SAMPLE_TEMPERATURE = float(os.getenv("ACADEMIC_SAMPLE_TEMPERATURE", "1.0"))
SAMPLE_TOP_P = float(os.getenv("ACADEMIC_SAMPLE_TOP_P", "0.95"))
# Override: force the sampling temperature/top_p on EVERY benchmark regardless of
# k, so even single-shot (k=1) benchmarks sample instead of decoding greedily.
# Off by default — k=1 stays greedy (temp 0, top_p 1.0) for reproducibility.
FORCE_TEMPERATURE = bool(int(os.getenv("ACADEMIC_FORCE_TEMPERATURE", "0")))

# SimpleQA grader (LLM judge). SimpleQA answers are free-form, so correctness is
# decided by a grader model that classifies each answer CORRECT/INCORRECT/
# NOT_ATTEMPTED (OpenAI simple-evals protocol). When no judge endpoint is set the
# target model grades itself (self-judge) — convenient but mildly biased; point
# these at a strong external grader (e.g. GPT-4-class) for the faithful setup.
JUDGE_API_URL = os.getenv("ACADEMIC_JUDGE_API_URL", "")
JUDGE_MODEL = os.getenv("ACADEMIC_JUDGE_MODEL", "")
JUDGE_API_KEY = os.getenv("ACADEMIC_JUDGE_API_KEY", "")
# Token budget for one grade. A non-reasoning grader emits a single letter, but a
# reasoning model needs room to think before it concludes — raise this (or use a
# non-reasoning grader) if self-judging with a reasoning model.
JUDGE_MAX_TOKENS = int(os.getenv("ACADEMIC_JUDGE_MAX_TOKENS", "256"))

# HLE grader. Official HLE (centerforaisafety/hle) has NO exact-match path — it
# grades every answer with an LLM judge (default o3-mini) that first extracts the
# final answer, then compares it to the gold answer allowing "a small margin of
# error for numerical problems". ~76-80% of HLE is free-form exact-answer, so
# string comparison systematically under-counts (LaTeX, units, phrasing) and does
# so unevenly across models. When a judge is configured (the same ACADEMIC_JUDGE_*
# endpoint SimpleQA uses) HLE is graded that way; with no judge it falls back to
# the local exact-match grader, which is a floor on the true score, not a
# substitute for it. Set ACADEMIC_HLE_USE_JUDGE=0 to force the fallback even when
# a judge is available (e.g. to save judge spend).
HLE_USE_JUDGE = bool(int(os.getenv("ACADEMIC_HLE_USE_JUDGE", "1")))
# The HLE judge must emit extracted_final_answer + reasoning before its verdict,
# so it needs far more room than SimpleQA's single letter (upstream allows 4096).
# Too small a budget truncates the reply before the `correct:` line and every
# sample silently falls back to exact-match.
HLE_JUDGE_MAX_TOKENS = int(os.getenv("ACADEMIC_HLE_JUDGE_MAX_TOKENS", "2048"))

# LongBench-v2 context truncation. Contexts run to hundreds of thousands of
# tokens; we middle-truncate (keep the head and tail, drop the middle) to this
# token budget before sending, mirroring the official pred.py. 0 = no truncation.
LONGBENCH_MAX_INPUT_TOKENS = int(os.getenv("ACADEMIC_LONGBENCH_MAX_INPUT_TOKENS", "120000"))

# LongBench-v2 max OUTPUT tokens. LongBench-v2 is the only input-heavy benchmark
# in the suite (contexts up to hundreds of thousands of tokens), but its answer is
# a single multiple-choice line ("The correct answer is (X)"), so it needs far
# fewer output tokens than the reasoning benchmarks (AIME/GPQA/HLE) the suite-wide
# max_tokens is usually sized for. Capping output here keeps a large global
# max_tokens from stealing the context window: vLLM rejects a request with 400
# when prompt_tokens + max_tokens > max_model_len. 0 = inherit the suite max_tokens.
LONGBENCH_MAX_OUTPUT_TOKENS = int(os.getenv("ACADEMIC_LONGBENCH_MAX_OUTPUT_TOKENS", "8192"))

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# AIME — MathArena prompt (eth-sri/matharena). A single user message: a fixed
# instruction requiring the final answer inside \boxed{}, then the problem.
# No system prompt, matching MathArena's pure-model solver.
_AIME_INSTRUCTION = (
    "Put your final answer within \\boxed{}.\n"
    "The answer is an integer between 0 and 999 inclusive."
)

# GPQA — OpenAI simple-evals QUERY_TEMPLATE_MULTICHOICE. A single user message
# (no system prompt) that asks for chain-of-thought and a final
# "Answer: $LETTER" line, which the ANSWER_PATTERN_MULTICHOICE regex extracts.
_GPQA_QUERY_TMPL = (
    "Answer the following multiple choice question. The last line of your "
    "response should be of the following format: 'Answer: $LETTER' (without "
    "quotes) where LETTER is one of ABCD. Think step by step before answering."
    "\n\n"
    "{question}\n\n"
    "A) {A}\n"
    "B) {B}\n"
    "C) {C}\n"
    "D) {D}"
)

_MMLU_SYSTEM = (
    "You are a knowledgeable expert. Think step by step. "
    "At the end of your response, write your final answer on its own line "
    "in the format: ANSWER: <letter>  (where letter is A through J)"
)

_MMLU_USER_TMPL = (
    "Question: {question}\n\n"
    "{options}\n\n"
    "Choose the single best answer from A to J."
)

# HLE — the official system prompt, verbatim from centerforaisafety/hle
# (hle_eval/run_model_predictions.py SYSTEM_EXACT_ANSWER). One prompt for both
# question types. The Confidence line is not decoration: the official metric set
# reports calibration error alongside accuracy, and the judge is told to read the
# score out of the response.
_HLE_SYSTEM = (
    "Your response should be in the following format:\n"
    "Explanation: {your explanation for your answer choice}\n"
    "Answer: {your chosen answer}\n"
    "Confidence: {your confidence score between 0% and 100% for your answer}"
)

# HLE judge template — verbatim from centerforaisafety/hle
# (hle_eval/run_judge_results.py JUDGE_PROMPT). Filled via .format(question=,
# correct_answer=, response=). The odd "0|\%|" escaping is upstream's, kept as-is
# rather than cleaned up so the judge sees the same bytes the published numbers
# were produced with.
#
# Upstream binds the reply to a pydantic schema via OpenAI structured outputs
# (response_format=ExtractedAnswer). We can't: the judge here is any
# OpenAI-compatible endpoint, and json-schema response_format is not universally
# supported (and silently ignored where it isn't). The prompt already demands
# labelled fields, so we parse them out of the text instead — see
# _parse_hle_judgement. That is the one deliberate deviation from upstream.
_HLE_JUDGE_TEMPLATE = """Judge whether the following [response] to [question] is correct or not based on the precise and unambiguous [correct_answer] below.

[question]: {question}

[response]: {response}

Your judgement must be in the format and criteria specified below:

extracted_final_answer: The final exact answer extracted from the [response]. Put the extracted answer as 'None' if there is no exact, final answer to extract from the response.

[correct_answer]: {correct_answer}

reasoning: Explain why the extracted_final_answer is correct or incorrect based on [correct_answer], focusing only on if there are meaningful differences between [correct_answer] and the extracted_final_answer. Do not comment on any background to the problem, do not attempt to solve the problem, do not argue for any answer different than [correct_answer], focus only on whether the answers match.

correct: Answer 'yes' if extracted_final_answer matches the [correct_answer] given above, or is within a small margin of error for numerical problems. Answer 'no' otherwise, i.e. if there if there is any inconsistency, ambiguity, non-equivalency, or if the extracted answer is incorrect.


confidence: The extracted confidence score between 0|\\%| and 100|\\%| from [response]. Put 100 if there is no confidence score available."""

_LCB_SYSTEM = (
    "You are a programming expert. Write a Python solution for the problem. "
    "Your code must read input from stdin and write the answer to stdout. "
    "Return ONLY the code, no explanation, no markdown fences."
)

# SimpleQA — the model under test gets a single user message containing the bare
# question (no system prompt), matching OpenAI simple-evals. Correctness is then
# decided by the grader below, not by string matching.

# SimpleQA grader template — verbatim from OpenAI simple-evals (simpleqa_eval.py).
# Filled via .format(question=, target=, predicted_answer=); the grader replies
# with a single letter A/B/C = CORRECT/INCORRECT/NOT_ATTEMPTED.
_SIMPLEQA_GRADER_TEMPLATE = """
Your job is to look at a question, a gold target, and a predicted answer, and then assign a grade of either ["CORRECT", "INCORRECT", "NOT_ATTEMPTED"].
First, I will give examples of each grade, and then you will grade a new example.


The following are examples of CORRECT predicted answers.
```
Question: What are the names of Barack Obama's children?
Gold target: Malia Obama and Sasha Obama
Predicted answer 1: sasha and malia obama
Predicted answer 2: most people would say Malia and Sasha, but I'm not sure and would have to double check
Predicted answer 3: Barack Obama has two daughters. Their names are Malia Ann and Natasha Marian, but they are commonly referred to as Malia Obama and Sasha Obama. Malia was born on July 4, 1998, and Sasha was born on June 10, 2001.
```
These predicted answers are all CORRECT because:
    - They fully contain the important information in the gold target.
    - They do not contain any information that contradicts the gold target.
    - Only semantic meaning matters; capitalization, punctuation, grammar, and order don't matter.
    - Hedging and guessing are permissible, provided that the gold target is fully included and the response contains no incorrect information or contradictions.


The following are examples of INCORRECT predicted answers.
```
Question: What are the names of Barack Obama's children?
Gold target: Malia and Sasha
Predicted answer 1: Malia.
Predicted answer 2: Malia, Sasha, and Susan.
Predicted answer 3: Barack Obama does not have any children.
Predicted answer 4: I think it's either Malia and Sasha. Or it could be Malia and Jackie. Or it could be Joey and Malia.
Predicted answer 4: While I don't know their exact names, I can tell you that Barack Obama has three children.
Predicted answer 5: It's possible you may mean Betsy and Olivia. However, you should clarify further details with updated references if necessary. Is that the correct answer?
Predicted answer 6: It may be the case that Obama's child is named James. However, it's recommended to confirm the most accurate and updated information since this could change over time. This model may not always reflect the most current information.
```
These predicted answers are all INCORRECT because:
    - A factual statement in the answer contradicts the gold target. Incorrect statements that have some hedging (e.g., "it is possible that", "although i'm not sure, i think") are also considered incorrect.


The following are examples of NOT_ATTEMPTED predicted answers.
```
Question: What are the names of Barack Obama's children?
Gold target: Malia and Sasha
Predicted answer 1: I don't know.
Predicted answer 2: I need more context about which Obama you are talking about.
Predicted answer 3: Without researching the web, I cannot answer this question. However, I can tell you that Barack Obama has two children.
Predicted answer 4: Barack Obama has two children. I know that one of them is Malia, but I'm not sure about the other one.
```
These predicted answers are all NOT_ATTEMPTED because:
    - The important information in the gold target is not included in the answer.
    - No statements in the answer contradict the gold target.


Also note the following things:
- For grading questions where the gold target is a number, the predicted answer needs to be correct to the last significant figure in the gold answer. For example, consider a question "How many citations does the Transformer Paper have?" with gold target "120k".
    - Predicted answers "120k", "124k", and 115k" are all CORRECT.
    - Predicted answers "100k" and "113k" are INCORRECT.
    - Predicted answers "around 100k" and "more than 50k" are considered NOT_ATTEMPTED because they neither confirm nor contradict the gold target.
- The gold target may contain more information than the question. In such cases, the predicted answer only needs to contain the information that is in the question.
    - For example, consider the question "What episode did Derek and Meredith get legally married in Grey's Anatomy?" with gold target "Season 7, Episode 20: White Wedding". Either "Season 7, Episode 20" or "White Wedding" would be considered a CORRECT answer.
- Do not punish predicted answers if they omit information that would be clearly inferred from the question.
    - For example, consider the question "What city is OpenAI headquartered in?" and the gold target "San Francisco, California". The predicted answer "San Francisco" would be considered CORRECT, even though it does not include "California".
    - Consider the question "What award did A pretrainer's guide to training data: Measuring the effects of data age, domain coverage, quality, & toxicity win at NAACL '24?", the gold target is "Outstanding Paper Award". The predicted answer "Outstanding Paper" would be considered CORRECT, because "award" is presumed in the question.
    - For the question "What is the height of Jason Wei in meters?", the gold target is "1.73 m". The predicted answer "1.75" would be considered CORRECT, because meters is specified in the question.
    - For the question "What is the name of Barack Obama's wife?", the gold target is "Michelle Obama". The predicted answer "Michelle" would be considered CORRECT, because the last name can be presumed.
- Do not punish for typos in people's name if it's clearly the same name.
    - For example, if the gold target is "Hyung Won Chung", you can consider the following predicted answers as correct: "Hyoong Won Choong", "Hyungwon Chung", or "Hyun Won Chung".


Here is a new example. Simply reply with either CORRECT, INCORRECT, NOT ATTEMPTED. Don't apologize or correct yourself if there was a mistake; we are just trying to grade the answer.
```
Question: {question}
Gold target: {target}
Predicted answer: {predicted_answer}
```

Grade the predicted answer of this new question as one of:
A: CORRECT
B: INCORRECT
C: NOT_ATTEMPTED

Just return the letters "A", "B", or "C", with no text around it.
""".strip()

# LongBench-v2 — verbatim from THUDM/LongBench prompts/0shot.txt (direct, no-CoT).
# $DOC$/$Q$/$C_A$..$C_D$ are filled by str.replace() at build time.
_LONGBENCH_TEMPLATE = """Please read the following text and answer the question below.

<text>
$DOC$
</text>

What is the correct answer to this question: $Q$
Choices:
(A) $C_A$
(B) $C_B$
(C) $C_C$
(D) $C_D$

Format your response as follows: "The correct answer is (insert answer here)"."""


# ---------------------------------------------------------------------------
# Low-level API call (synchronous, used in thread pool)
# ---------------------------------------------------------------------------

def _mean(xs: List[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _derive_perf(m: Dict[str, Any]) -> Dict[str, Any]:
    """Compute TPOT / output-TPS from raw timings + token counts (in place).

    TPOT = decode time (first→last token) spread over the output tokens after
    the first. output_tps = completion tokens / end-to-end latency.
    """
    ct = m.get("completion_tokens")
    total_s = (m["total_ms"] / 1000.0) if m.get("total_ms") else None
    decode_s = (m["decode_ms"] / 1000.0) if m.get("decode_ms") is not None else None
    if ct and ct > 1 and decode_s is not None:
        m["tpot_ms"] = decode_s / (ct - 1) * 1000.0
    else:
        m["tpot_ms"] = None
    m["output_tps"] = (ct / total_s) if (ct and total_s) else None
    return m


def _warn_if_truncated(model: str, finish: Optional[str], content: str,
                       completion_tokens: Any, max_tokens: int) -> None:
    if finish == "length" and not content.strip():
        logger.warning(
            "[chat_complete] model=%s hit token limit (finish_reason=length) with empty content "
            "(completion_tokens=%s, max_tokens=%s). Reasoning likely consumed the full budget "
            "before producing an answer — raise max_tokens for this benchmark.",
            model, completion_tokens, max_tokens,
        )


def _read_stream(line_iter, t0: float, model: str, max_tokens: int,
                 timed: bool = True) -> Tuple[str, Dict[str, Any]]:
    """Parse an SSE chat-completions stream from a line iterator.

    ``timed=True`` (live response iteration) records first/last token wall-clock
    for TTFT/TPOT. ``timed=False`` (an already-buffered body, e.g. a server that
    streamed SSE under a non-standard content-type) recovers text + token usage
    but leaves TTFT/decode as None, since per-token timing can't be reconstructed
    after the fact.
    """
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    t_first: Optional[float] = None
    t_last = t0
    finish = None
    usage = None
    n_chunks = 0
    for raw in line_iter:
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices", []):
            delta = ch.get("delta") or {}
            piece = delta.get("content") or ""
            rpiece = delta.get("reasoning_content") or ""
            if piece or rpiece:
                if timed:
                    now = time.monotonic()
                    if t_first is None:
                        t_first = now
                    t_last = now
                n_chunks += 1
                if piece:
                    content_parts.append(piece)
                if rpiece:
                    reasoning_parts.append(rpiece)
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]

    total_ms = (time.monotonic() - t0) * 1000.0
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    if usage and usage.get("completion_tokens") is not None:
        ct = usage.get("completion_tokens")
        pt = usage.get("prompt_tokens")
        estimated = False
    else:
        # Server omitted the usage chunk: approximate output tokens by counting
        # streamed deltas (one delta ≈ one token for most servers).
        ct = n_chunks or None
        pt = None
        estimated = True
    _warn_if_truncated(model, finish, content, ct, max_tokens)
    perf = _derive_perf({
        "ttft_ms": ((t_first - t0) * 1000.0) if t_first is not None else None,
        "total_ms": total_ms,
        "decode_ms": ((t_last - t_first) * 1000.0) if t_first is not None else None,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "finish_reason": finish,
        "tokens_estimated": estimated,
    })
    # Reasoning models may put the answer in reasoning_content with empty content.
    return (content if content.strip() else reasoning), perf


def _chat_complete(
    api_url: str,
    model: str,
    api_key: str,
    messages: List[Dict],
    max_tokens: int = MAX_TOKENS,
    temperature: float = 0.0,
    top_p: float = 1.0,
    timeout: float = REQUEST_TIMEOUT,
    stream: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """Return (assistant_text, perf_metrics) from one chat-completions call.

    Streaming (default) measures TTFT/TPOT via SSE; non-stream measures only
    total latency + token usage. If ``stream`` is requested but the server
    responds with a plain JSON body (some endpoints ignore ``stream``), we fall
    back to parsing it as a non-streamed response so scores are unaffected.

    perf keys: ttft_ms, total_ms, decode_ms, prompt_tokens, completion_tokens,
    tpot_ms, output_tps, finish_reason, tokens_estimated.
    """
    import requests  # local import to keep module load fast

    url = join_endpoint(api_url, "chat/completions")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    if stream:
        payload["stream"] = True
        # Ask the server for a final usage chunk (OpenAI / vLLM / sglang support).
        payload["stream_options"] = {"include_usage": True}

    t0 = time.monotonic()
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout, stream=stream)
    # Surface the server's error body. requests' raise_for_status() reports only
    # "400 Client Error: Bad Request for url: ..." and drops the response body —
    # but that body is where vLLM/sglang put the real reason (e.g. "maximum
    # context length is N tokens, however you requested M"). Read it here (safe:
    # an error response is small and not the SSE stream we'd otherwise consume)
    # and fold a snippet into the exception so it reaches the failure log.
    if resp.status_code >= 400:
        body = ""
        try:
            body = (resp.text or "").strip().replace("\n", " ")
        except Exception:
            pass
        if len(body) > 500:
            body = body[:500] + "…"
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for {url}"
            + (f": {body}" if body else ""),
            response=resp,
        )
    try:
        resp.raise_for_status()

        if stream:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" in ctype:
                # Standard SSE: iterate the live response so we can time tokens.
                return _read_stream(resp.iter_lines(decode_unicode=True), t0, model, max_tokens)
            # We requested a stream but the body isn't labelled SSE. It's either
            # plain JSON (server ignored `stream`) or SSE under a non-standard
            # content-type. Buffer the body once and detect by shape — NEVER feed
            # an SSE body to resp.json(), which would raise and fail an otherwise
            # healthy request (zeroing the whole benchmark on such servers).
            raw = resp.text
            if raw.lstrip()[:5] == "data:":
                return _read_stream(iter(raw.split("\n")), t0, model, max_tokens, timed=False)
            data = json.loads(raw)
        else:
            # Non-streamed path requested explicitly.
            data = resp.json()
        total_ms = (time.monotonic() - t0) * 1000.0
        choice = data["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        finish = choice.get("finish_reason", "")
        usage = data.get("usage") or {}

        logger.debug(
            "[chat_complete] model=%s time=%.2fs prompt=%s completion=%s finish=%s content_len=%d reasoning_len=%d",
            model, total_ms / 1000.0, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            finish, len(content), len(reasoning),
        )
        _warn_if_truncated(model, finish, content, usage.get("completion_tokens"), max_tokens)
        perf = _derive_perf({
            "ttft_ms": None,
            "total_ms": total_ms,
            "decode_ms": None,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "finish_reason": finish,
            "tokens_estimated": False,
        })
        return (content if content.strip() else reasoning), perf
    finally:
        # Release the pooled connection even if streaming aborts mid-response.
        resp.close()


# ---------------------------------------------------------------------------
# Per-benchmark dataset loaders
# ---------------------------------------------------------------------------

def _read_jsonl_rows(path: Path, cap: int) -> List[Dict]:
    """Parse non-empty lines of a JSONL file into dicts, stopping after ``cap``
    rows (``cap`` falsy = read all).

    Behaviour-identical to the previous
        [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for valid JSONL — same lines, same order — but reads the file lazily with a
    large sequential buffer and stops once ``cap`` rows are collected, so a huge
    file (e.g. the ~680MB livecodebench test set) is not fully read and parsed
    when a benchmark only uses ``cap`` samples. Callers keep their own ``[:cap]``
    slice, so the early stop here is purely an I/O + memory optimisation.

    The only semantic difference from ``str.splitlines()`` is exotic line
    separators (U+2028/U+2029/\\x0b/\\x0c/...): file iteration splits on newlines
    only. Valid JSON escapes all control chars, and a literal U+2028/U+2029
    inside a string would already have crashed the old splitlines() path (it
    would split mid-object), so for every dataset that currently loads the two
    are identical.
    """
    rows: List[Dict] = []
    with path.open(buffering=1 << 20) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
                if cap and len(rows) >= cap:
                    break
    return rows


def _load_aime2025(data_dir: str, cap: int) -> List[Dict]:
    path = Path(data_dir) / "aime2025" / "aime2025.jsonl"
    rows = _read_jsonl_rows(path, cap)
    return rows[:cap] if cap else rows


def _load_gpqa_diamond(data_dir: str, cap: int) -> List[Dict]:
    import csv
    path = Path(data_dir) / "gpqa" / "gpqa_diamond.csv"
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows[:cap] if cap else rows


def _load_ifeval(data_dir: str, cap: int) -> List[Dict]:
    path = Path(data_dir) / "ifeval" / "input_data.jsonl"
    rows = _read_jsonl_rows(path, cap)
    return rows[:cap] if cap else rows


def _load_mmlu_pro(data_dir: str, cap: int) -> List[Dict]:
    import pandas as pd
    path = Path(data_dir) / "mmlu_pro" / "test-00000-of-00001.parquet"
    df = pd.read_parquet(str(path))
    rows = df.to_dict("records")
    return rows[:cap] if cap else rows


def _load_hle(data_dir: str, cap: int) -> List[Dict]:
    import pandas as pd
    path = Path(data_dir) / "cais" / "hle" / "data" / "test-00000-of-00001.parquet"
    df = pd.read_parquet(str(path))
    # Skip image-only questions (we can't handle images)
    df = df[df["image"].isna() | (df["image"] == "")]
    rows = df[["id", "question", "answer", "answer_type"]].to_dict("records")
    return rows[:cap] if cap else rows


def _load_livecodebench_v6(data_dir: str, cap: int) -> List[Dict]:
    """Load LiveCodeBench V6 from test2.jsonl (latest window)."""
    path = Path(data_dir) / "code_generation_lite" / "test2.jsonl"
    rows = _read_jsonl_rows(path, cap)
    return rows[:cap] if cap else rows


def _load_simpleqa(data_dir: str, cap: int) -> List[Dict]:
    """Load SimpleQA (cols: metadata, problem, answer).

    Prefers the HF basicv8vc/SimpleQA CSV (simple_qa_test_set.csv); falls back to
    a ModelScope-style parquet snapshot (data/test-*.parquet) when the CSV is
    absent, so the benchmark works regardless of how the data was acquired.
    """
    base = Path(data_dir) / "simpleqa"
    csv_path = base / "simple_qa_test_set.csv"
    if csv_path.exists():
        import csv
        rows: List[Dict] = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
                if cap and len(rows) >= cap:
                    break
        return rows[:cap] if cap else rows
    import pandas as pd
    matches = sorted(base.glob("data/test-*.parquet")) or sorted(base.glob("*.parquet"))
    if not matches:
        raise FileNotFoundError(f"SimpleQA data not found under {base} (no CSV or parquet)")
    rows = pd.read_parquet(str(matches[0])).to_dict("records")
    return rows[:cap] if cap else rows


def _load_longbench_v2(data_dir: str, cap: int) -> List[Dict]:
    """Load LongBench-v2 from longbench_v2.jsonl (converted from the HF data.json).

    Fields: _id, domain, sub_domain, difficulty, length, question,
    choice_A..choice_D, answer (A/B/C/D), context.
    """
    path = Path(data_dir) / "longbench_v2" / "longbench_v2.jsonl"
    rows = _read_jsonl_rows(path, cap)
    return rows[:cap] if cap else rows


# ---------------------------------------------------------------------------
# Answer extraction helpers
# ---------------------------------------------------------------------------

def _extract_tagged_answer(text: str) -> str:
    """Extract the value after the last 'ANSWER: ' tag."""
    matches = re.findall(r"ANSWER\s*:\s*(.+)", text, re.IGNORECASE)
    if matches:
        return matches[-1].strip()
    return ""


def _extract_integer(text: str) -> Optional[int]:
    """Extract the last integer in a string."""
    nums = re.findall(r"\b(\d+)\b", text)
    if nums:
        return int(nums[-1])
    return None


# OpenAI simple-evals ANSWER_PATTERN_MULTICHOICE — matches the required
# "Answer: $LETTER" final line (case-insensitive, tolerant of $...$ wrapping).
_MC_ANSWER_RE = re.compile(r"(?i)Answer[ \t]*:[ \t]*\$?([A-D])\$?")


def _extract_mc_answer(text: str) -> Optional[str]:
    """Extract A–D from the simple-evals 'Answer: $LETTER' line, last match wins."""
    matches = _MC_ANSWER_RE.findall(text)
    return matches[-1].upper() if matches else None


def _last_boxed(text: str) -> Optional[str]:
    r"""Return the content of the last \boxed{...} / \fbox{...}, brace-balanced.

    A plain regex can't match the balanced braces of nested LaTeX (e.g.
    \boxed{\frac{1}{2}}), so we scan from the last marker and track brace depth.
    """
    pos = max(text.rfind("\\boxed"), text.rfind("\\fbox"))
    if pos < 0:
        return None
    start = text.find("{", pos)
    if start < 0:
        return None
    depth = 0
    for j in range(start, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:j]
    return None  # unbalanced — no closing brace


def _extract_letter(text: str, choices: str = "ABCD") -> Optional[str]:
    """Extract single letter from answer tag, explicit phrases, or last occurrence."""
    tagged = _extract_tagged_answer(text)
    m = re.search(rf"\b([{choices}])\b", tagged, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # Explicit answer phrases (English / Chinese / common variants)
    patterns = [
        rf"(?:answer|correct|答案是|答案为|正确选项|选择)\s*[:：是为]?\s*\b([{choices}])\b",
        rf"\b([{choices}])\b\s*(?:is correct|是正确的|正确)",
        rf"(?:option|choice|选项)\s*[:：]?\s*\b([{choices}])\b",
    ]
    for pat in patterns:
        mm = re.findall(pat, text, re.IGNORECASE)
        if mm:
            return mm[-1].upper()

    # Fallback: last standalone letter in the full text
    m2 = re.findall(rf"\b([{choices}])\b", text, re.IGNORECASE)
    if m2:
        return m2[-1].upper()
    return None


def _extract_longbench_answer(response: str) -> Optional[str]:
    """LongBench-v2 extract_answer (verbatim from THUDM/LongBench pred.py).

    Strip '*' (markdown bold), then look for the required closing line
    'The correct answer is (X)' — with or without the parentheses — X in A-D.
    """
    response = response.replace("*", "")
    match = re.search(r"The correct answer is \(([A-D])\)", response)
    if match:
        return match.group(1)
    match = re.search(r"The correct answer is ([A-D])", response)
    if match:
        return match.group(1)
    return None


_TIKTOKEN_ENC = None  # lazily-initialised cl100k_base encoder (None = unavailable)
_TIKTOKEN_TRIED = False


def _get_tiktoken_encoder():
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if not _TIKTOKEN_TRIED:
        _TIKTOKEN_TRIED = True
        try:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:  # pragma: no cover - optional dependency
            logger.debug("[academic] tiktoken unavailable, using char-based truncation: %s", exc)
            _TIKTOKEN_ENC = None
    return _TIKTOKEN_ENC


def _truncate_middle(text: str, max_tokens: int) -> str:
    """Keep the head and tail of ``text``, drop the middle, to fit ``max_tokens``.

    Mirrors the official LongBench-v2 pred.py truncation (input_ids[:max/2] +
    input_ids[-max/2:]). Uses tiktoken when available, else a ~4-chars/token
    character approximation. ``max_tokens<=0`` disables truncation.
    """
    if not text or max_tokens <= 0:
        return text
    enc = _get_tiktoken_encoder()
    if enc is not None:
        ids = enc.encode(text, disallowed_special=())
        if len(ids) <= max_tokens:
            return text
        half = max_tokens // 2
        return enc.decode(ids[:half] + ids[-half:])
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + text[-half:]


def _get_model_max_len(api_url: str, model: str, api_key: str, timeout: float = 30.0) -> Optional[int]:
    """Best-effort lookup of the server's context window via GET /v1/models.

    vLLM (and some sglang builds) include ``max_model_len`` in each model card.
    Returns the matching model's value, else the first card's, else None when the
    endpoint doesn't expose it or the call fails — callers must treat None as
    "unknown" and fall back to the configured budget without auto-clamping.
    """
    import requests
    try:
        url = join_endpoint(api_url, "models")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        cards = resp.json().get("data") or []
        if not cards:
            return None
        # Prefer the card whose id matches the model under test; else first card.
        card = next((c for c in cards if c.get("id") == model), cards[0])
        mml = card.get("max_model_len")
        return int(mml) if isinstance(mml, (int, float)) and mml > 0 else None
    except Exception as exc:
        logger.debug("[academic] could not read max_model_len from /v1/models: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Per-benchmark evaluators
# ---------------------------------------------------------------------------

def _eval_aime(response: str, answer: str) -> bool:
    # MathArena protocol: read the integer inside the last \boxed{}. Fall back to
    # the old ANSWER:/last-integer heuristics so models that ignore the \boxed{}
    # instruction still get scored on the value they did emit.
    boxed = _last_boxed(response)
    pred = _extract_integer(boxed) if boxed else None
    if pred is None:
        tagged = _extract_tagged_answer(response)
        pred = _extract_integer(tagged) if tagged else _extract_integer(response)
    gt = _extract_integer(answer)
    result = pred is not None and gt is not None and pred == gt
    logger.debug("[eval aime] pred=%s gt=%s boxed=%r result=%s", pred, gt, boxed, result)
    return result


def _eval_gpqa(response: str, correct_answer: str) -> bool:
    # simple-evals protocol: the canonical "Answer: $LETTER" line. Fall back to
    # the heuristic letter extractor only when that line is absent, so a model
    # that phrased its choice differently isn't auto-marked wrong.
    pred = _extract_mc_answer(response)
    if pred is None:
        pred = _extract_letter(response, "ABCD")
    gt = correct_answer.strip().upper()
    result = pred == gt
    logger.debug("[eval gpqa] pred=%s gt=%s result=%s", pred, gt, result)
    return result


def _eval_mmlu(response: str, answer_letter: str) -> bool:
    pred = _extract_letter(response, "ABCDEFGHIJ")
    gt = answer_letter.strip().upper()
    result = pred == gt
    logger.debug("[eval mmlu] pred=%s gt=%s result=%s", pred, gt, result)
    return result


# --- HLE exact-match fallback -------------------------------------------------
# Used only when no LLM judge is configured. Official HLE has no such path, so
# this can only under-count: it accepts a superset of literal string equality,
# never a superset of semantic equivalence. Every rule below is a formatting
# difference the official judge would wave through, and nothing here reasons
# about meaning ("two" != "2", "Paris, France" != "Paris").

# Spacing/sizing macros that carry no meaning, dropped before comparison.
_LATEX_NOISE_RE = re.compile(r"\\(?:left|right|big|Big|bigg|Bigg|,|;|:|!|quad|qquad)\b|\\[,;:!]")
# \boxed{x} / \text{x} / \mathrm{x} / \textbf{x} -> x  (one level; applied twice)
_LATEX_WRAPPER_RE = re.compile(r"\\(?:boxed|text|textbf|textit|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")
# \frac{a}{b} -> a/b, \dfrac/\tfrac likewise.
_LATEX_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
# Thousands separators inside numbers: 1,234,567 / 1{,}234 / 1 234 -> 1234.
# ({,} and a thin space are both LaTeX idioms for the same thing.)
_THOUSANDS_RE = re.compile(r"(?<=\d)[,\s](?=\d{3}\b)")
_PURE_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _normalize_exact_answer(s: str) -> str:
    """Canonicalise a free-form answer for the fallback comparison."""
    import unicodedata

    s = unicodedata.normalize("NFKC", (s or "").strip())
    # Unicode punctuation the model and the dataset spell differently.
    for src, dst in (("\u2212", "-"), ("\u2013", "-"), ("\u2014", "-"),
                     ("\u00d7", "*"), ("\u2018", "'"), ("\u2019", "'"),
                     ("\u201c", '"'), ("\u201d", '"')):
        s = s.replace(src, dst)
    # LaTeX thousands idiom, resolved before the peel loop below: its braces
    # would otherwise defeat the (brace-free) \boxed{...} unwrap.
    s = s.replace("{,}", ",").replace("{\\,}", ",")
    # Peel wrappers until stable: each layer can expose another, and they nest in
    # any order — "$\boxed{\text{Paris}}$." needs the trailing period gone before
    # the closing "$" is at the end, and \boxed{\text{x}} needs two unwraps.
    for _ in range(4):
        before = s
        s = re.sub(r"[.,;:]+$", "", s).strip()          # sentence punctuation
        s = s.strip("\"'").strip()                       # enclosing quotes
        s = re.sub(r"^\$+|\$+$", "", s).strip()          # $x$
        s = re.sub(r"^\\[\(\[]|\\[\)\]]$", "", s).strip()  # \(x\), \[x\]
        s = _LATEX_WRAPPER_RE.sub(r"\1", s).strip()      # \boxed{x}, \text{x}
        if s == before:
            break
    s = _LATEX_FRAC_RE.sub(r"\1/\2", s)
    s = _LATEX_NOISE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _THOUSANDS_RE.sub("", s)
    return s.lower()


def _exact_answer_match(pred: str, gold: str) -> bool:
    """Fallback equivalence: normalised string equality, or numeric closeness."""
    p, g = _normalize_exact_answer(pred), _normalize_exact_answer(gold)
    if not g:
        return False
    if p == g:
        return True
    # Numbers only: 0.5 == .5 == 5e-1, and 2 == 2.0. Tight tolerance — this is
    # float-representation noise, NOT the official judge's "small margin of
    # error for numerical problems", which we cannot reproduce without it.
    if _PURE_NUMBER_RE.match(p) and _PURE_NUMBER_RE.match(g):
        try:
            import math
            return math.isclose(float(p), float(g), rel_tol=1e-9, abs_tol=1e-12)
        except ValueError:
            return False
    return False


def _eval_hle(response: str, answer: str, answer_type: str) -> bool:
    tagged = _extract_tagged_answer(response)
    pred = (tagged or response).strip()
    gt = answer.strip()
    if answer_type in ("multiple_choice", "multipleChoice"):
        result = _extract_letter(pred, "ABCDEFGHIJ") == _extract_letter(gt, "ABCDEFGHIJ")
    else:
        result = _exact_answer_match(pred, gt)
    logger.debug("[eval hle] pred=%r gt=%r type=%s result=%s", pred, gt, answer_type, result)
    return result


# "Confidence: 85%" from the model's own response (the official system prompt
# asks for it). Read directly in fallback mode; in judge mode the judge extracts
# it. Upstream defaults to 100 when the model omits it.
_CONFIDENCE_RE = re.compile(r"confidence\s*:?\s*(\d{1,3})\s*%?", re.IGNORECASE)


def _extract_confidence(text: str, default: int = 100) -> int:
    """Extract a 0-100 confidence score; ``default`` when absent (upstream: 100)."""
    matches = _CONFIDENCE_RE.findall(text or "")
    if not matches:
        return default
    return max(0, min(100, int(matches[-1])))


def _eval_aime2025(response: str, answer: str) -> bool:
    return _eval_aime(response, answer)


def _eval_longbench_v2(response: str, answer: str) -> bool:
    # Extract the 'The correct answer is (X)' choice and exact-match the gold A-D.
    pred = _extract_longbench_answer(response)
    gt = (answer or "").strip().upper()
    result = pred is not None and pred == gt
    logger.debug("[eval longbench_v2] pred=%s gt=%s result=%s", pred, gt, result)
    return result


# ---------------------------------------------------------------------------
# IFEval instruction verifiers
# ---------------------------------------------------------------------------

def _ifeval_relation(count: int, relation: str, target: int) -> bool:
    """IFEval numeric comparison. The official dataset uses relation values
    'at least' and 'less than' (NOT 'at most') — a 'less than' constraint that
    isn't matched here would silently pass and inflate the score, so handle it
    explicitly and fail loudly on an unrecognised relation."""
    if relation in ("at least", ">="):
        return count >= target
    if relation in ("less than", "<"):
        return count < target
    if relation in ("at most", "<="):  # not used by the dataset; kept for robustness
        return count <= target
    if relation in ("exactly", "=="):
        return count == target
    logger.warning("[eval ifeval] unknown relation=%r — counting as NOT satisfied", relation)
    return False


def _verify_ifeval_instruction(instruction_id: str, response: str, kwargs: Dict) -> bool:
    """Return True if response satisfies the instruction constraint."""
    iid = instruction_id.lower()

    if iid == "punctuation:no_comma":
        return "," not in response

    if iid == "punctuation:no_period":
        return "." not in response

    if iid == "length_constraints:number_words":
        relation = kwargs.get("relation", "at least")
        num_words = kwargs.get("num_words", 0)
        word_count = len(response.split())
        return _ifeval_relation(word_count, relation, num_words)

    if iid == "length_constraints:number_sentences":
        relation = kwargs.get("relation", "at least")
        num_sentences = kwargs.get("num_sentences", 0)
        # Split on ., !, ? and drop the trailing empty fragment a string ending
        # in punctuation otherwise leaves (which over-counts by one).
        count = len([s for s in re.split(r"[.!?]+", response.strip()) if s.strip()])
        return _ifeval_relation(count, relation, num_sentences)

    if iid == "length_constraints:number_paragraphs":
        relation = kwargs.get("relation", "at least")
        num_paragraphs = kwargs.get("num_paragraphs", 0)
        count = len([p for p in re.split(r"\n\s*\n", response.strip()) if p.strip()])
        return _ifeval_relation(count, relation, num_paragraphs)

    if iid == "detectable_format:number_highlighted_sections":
        num = kwargs.get("num_highlights", 0)
        # Highlighted sections: *text* or **text**
        found = re.findall(r"\*+[^*\n]+\*+", response)
        return len(found) >= num

    if iid == "detectable_format:number_bullet_lists":
        num = kwargs.get("num_bullets", 0)
        found = re.findall(r"^\s*[-*•]\s+", response, re.MULTILINE)
        return len(found) >= num

    if iid == "detectable_format:title":
        return bool(re.search(r"^<<[^>]+>>", response, re.MULTILINE))

    if iid == "detectable_format:constrained_response":
        choices = kwargs.get("constrained_responses", [])
        resp = response.strip()
        return any(resp == c for c in choices)

    if iid == "detectable_format:json_format":
        try:
            json.loads(response.strip())
            return True
        except Exception:
            return False

    # Dataset uses "keywords:*" (not "detectable_keywords:*")
    if iid in ("detectable_keywords:existence", "keywords:existence"):
        keywords = kwargs.get("keywords", [])
        return all(kw.lower() in response.lower() for kw in keywords)

    if iid in ("detectable_keywords:frequency", "keywords:frequency"):
        keyword = kwargs.get("keyword", "")
        relation = kwargs.get("relation", "at least")
        freq = kwargs.get("frequency", 0)
        count = response.lower().count(keyword.lower())
        return _ifeval_relation(count, relation, freq)

    if iid in ("detectable_keywords:forbidden_words", "keywords:forbidden_words"):
        forbidden = kwargs.get("forbidden_words", [])
        return not any(w.lower() in response.lower() for w in forbidden)

    if iid == "keywords:letter_frequency":
        letter = kwargs.get("letter", "")
        relation = kwargs.get("relation", "at least")
        freq = kwargs.get("frequency", 0)
        count = response.lower().count(letter.lower())
        return _ifeval_relation(count, relation, freq)

    if iid == "detectable_format:multiple_sections":
        num = kwargs.get("num_sections", 0)
        # Markdown sections: lines starting with one or more '#'
        found = re.findall(r"^#{1,6}\s+\S", response, re.MULTILINE)
        return len(found) >= num

    if iid == "detectable_content:number_placeholders":
        num = kwargs.get("num_placeholders", 0)
        found = re.findall(r"\[[^\[\]]+\]", response)
        return len(found) >= num

    if iid == "detectable_content:postscript":
        marker = kwargs.get("postscript_marker", "P.S.")
        return marker in response

    if iid == "length_constraints:nth_paragraph_first_word":
        nth = kwargs.get("nth_paragraph", 1)
        first_word = kwargs.get("first_word", "")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", response.strip()) if p.strip()]
        if len(paragraphs) < nth:
            return False
        para = paragraphs[nth - 1]
        words = para.split()
        return bool(words) and words[0].lower() == first_word.lower()

    if iid == "combination:repeat_prompt":
        # Response must contain the original prompt — we don't have access to it here, pass
        return True

    if iid == "startend:start_checker":
        starter = kwargs.get("starter", "")
        return response.strip().startswith(starter)

    if iid == "startend:end_checker":
        ender = kwargs.get("end_phrase", "")
        return response.strip().endswith(ender)

    if iid == "startend:quotation":
        stripped = response.strip()
        return stripped.startswith('"') and stripped.endswith('"')

    if iid == "change_case:english_capital":
        # All English letters must be uppercase
        letters = re.findall(r"[a-zA-Z]", response)
        return all(c.isupper() for c in letters)

    if iid == "change_case:english_lowercase":
        letters = re.findall(r"[a-zA-Z]", response)
        return all(c.islower() for c in letters)

    if iid == "change_case:capital_word_frequency":
        relation = kwargs.get("relation", "at least")
        capital_frequency = kwargs.get("capital_frequency", 0)
        words = response.split()
        # An all-CAPS word, per official IFEval — not merely capitalised
        # (w[0].isupper() counted every sentence-initial / proper-noun word).
        cap_words = sum(1 for w in words if w.isupper())
        return _ifeval_relation(cap_words, relation, capital_frequency)

    if iid == "language:response_language":
        # UNVERIFIED: proper language detection needs an extra dependency
        # (langdetect). Counts as satisfied — a known inflation for this
        # instruction; see the audit notes. TODO: implement detection.
        return True

    if iid == "combination:two_responses":
        return "******" in response

    # Unknown / unimplemented instruction: fail LOUDLY rather than silently
    # passing — a free pass here inflates the strict-AND prompt accuracy. The
    # warning surfaces any instruction_id we don't handle so it gets implemented.
    logger.warning("[eval ifeval] unhandled instruction_id=%s — counting as NOT satisfied", instruction_id)
    return False


def _eval_ifeval(response: str, instruction_id_list: List[str], kwargs_list: List[Dict]) -> bool:
    """Return True if response satisfies ALL instructions (prompt-level accuracy)."""
    for iid, kw in zip(instruction_id_list, kwargs_list):
        ok = _verify_ifeval_instruction(iid, response, kw)
        logger.debug("[eval ifeval] iid=%s ok=%s response_len=%d", iid, ok, len(response))
        if not ok:
            logger.debug("[eval ifeval] FAILED iid=%s response_preview=%r", iid, response[:200])
            return False
    return True


# ---------------------------------------------------------------------------
# LiveCodeBench evaluation
# ---------------------------------------------------------------------------

def _parse_public_tests(raw: Any) -> List[Tuple[str, str]]:
    """Parse public_test_cases field into (input, output) pairs."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, list):
        pairs = []
        for t in raw:
            if isinstance(t, dict):
                pairs.append((str(t.get("input", "")), str(t.get("output", ""))))
            elif isinstance(t, (list, tuple)) and len(t) >= 2:
                pairs.append((str(t[0]), str(t[1])))
        return pairs
    return []


def _run_code_against_tests(code: str, test_cases: List[Tuple[str, str]], timeout: float = 10.0) -> bool:
    """Execute generated code against each (input, expected_output) pair. Returns True if all pass."""
    if not test_cases:
        return False

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmpfile = f.name

    try:
        for stdin_data, expected_out in test_cases:
            try:
                result = subprocess.run(
                    [sys.executable, tmpfile],
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                actual = result.stdout.strip()
                if actual != expected_out.strip():
                    return False
            except subprocess.TimeoutExpired:
                return False
            except Exception:
                return False
        return True
    finally:
        try:
            os.unlink(tmpfile)
        except OSError:
            pass


def _extract_code(response: str) -> str:
    """Extract Python code from the response (strip markdown fences if present)."""
    # Take the LAST fenced block: reasoning/explanatory responses often emit an
    # illustrative or partial snippet first and the final solution last (matches
    # the official LiveCodeBench extraction). re.search would grab the first.
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", response, re.DOTALL)
    if blocks:
        return blocks[-1].strip()
    return response.strip()


def _eval_livecodebench(response: str, row: Dict) -> bool:
    code = _extract_code(response)
    test_cases = _parse_public_tests(row.get("public_test_cases", []))
    logger.debug("[eval livecodebench] tests=%d code_len=%d", len(test_cases), len(code))
    if not test_cases:
        logger.debug("[eval livecodebench] no test cases")
        return False
    ok = _run_code_against_tests(code, test_cases)
    logger.debug("[eval livecodebench] result=%s", ok)
    return ok


# ---------------------------------------------------------------------------
# Benchmark runner helpers
# ---------------------------------------------------------------------------

def _run_benchmark(
    name: str,
    items: List[Dict],
    build_messages: Any,
    evaluate: Any,
    api_url: str,
    model: str,
    api_key: str,
    max_workers: int,
    request_timeout: float,
    max_tokens: int = MAX_TOKENS,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    overall_fraction: float = 0.0,
    overall_step: float = 0.0,
    cancel_event=None,
    stream: bool = True,
    avg_k: int = 1,
    pass_k: int = 1,
    temperature: float = 0.0,
    top_p: float = 1.0,
) -> Dict[str, Any]:
    """Generic concurrent inference + evaluation loop. Returns metrics dict.

    Each item is queried ``n = max(avg_k, pass_k)`` times. The reported ``score``
    is avg@k — the mean per-item accuracy over the item's first ``avg_k`` samples,
    averaged across items — and ``pass_at_k`` is the fraction of items with ≥1
    correct sample among the first ``pass_k``. With avg_k=pass_k=1 this collapses
    to the original single-shot accuracy and one request per item.
    """
    total = len(items)
    if total == 0:
        logger.warning("[academic] %s: no items loaded — skipping", name)
        return {"score": None, "total": 0, "correct": 0, "skipped": True}

    avg_k = max(int(avg_k), 1)
    pass_k = max(int(pass_k), 1)
    n_samples = max(avg_k, pass_k)
    total_tasks = total * n_samples

    logger.info(
        "[academic] %s: BEGIN  items=%d  n_samples=%d (avg@%d/pass@%d)  temp=%.2f  top_p=%.2f  "
        "requests=%d  workers=%d  max_tokens=%d  timeout=%.1fs  stream=%s",
        name, total, n_samples, avg_k, pass_k, temperature, top_p,
        total_tasks, max_workers, max_tokens, request_timeout, stream,
    )
    t0 = time.monotonic()

    # results[idx] is the per-sample correctness list for item idx (length n_samples).
    results: List[List[bool]] = [[False] * n_samples for _ in range(total)]
    completed = 0
    correct_so_far = 0   # correct *samples* (not items)
    failed_so_far = 0

    # Per-request generation-performance accumulators (updated single-threaded in
    # the as_completed loop below, so no extra locking needed).
    ttfts: List[float] = []
    tpots: List[float] = []
    out_tpss: List[float] = []
    sum_completion_tokens = 0

    # Evaluators that need to know WHICH sample they are grading (e.g. SimpleQA,
    # which keeps a per-sample grade breakdown that must be sliced to the first
    # avg_k samples to match `score`) declare a 4-arg signature. Detect it once,
    # here, rather than making every other evaluator carry unused parameters.
    eval_wants_coords = len(inspect.signature(evaluate).parameters) >= 4

    def _infer_and_eval(idx: int, s: int) -> Tuple[int, int, bool, str, Optional[str], Dict[str, Any]]:
        item = items[idx]
        msgs = build_messages(item)
        try:
            response, perf = _chat_complete(
                api_url, model, api_key, msgs,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                timeout=request_timeout,
                stream=stream,
            )
            correct = evaluate(response, item, idx, s) if eval_wants_coords else evaluate(response, item)
            return idx, s, bool(correct), response, None, perf
        except Exception as exc:
            logger.debug("[academic] %s item %d sample %d failed: %s", name, idx, s, exc)
            return idx, s, False, "", f"{type(exc).__name__}: {exc}", {}

    # Collect a few sample responses for debugging
    sample_responses: List[Tuple[int, bool, str]] = []
    # Log progress on percentage milestones AND wall-clock heartbeat so slow
    # benchmarks (long max_tokens, low throughput) still show life between %s.
    log_every = max(1, total_tasks // 100)   # ~1% granularity
    heartbeat_s = 30.0
    last_log_t = t0
    error_samples: List[str] = []

    canceled = False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        # One future per (item, sample); the k repeats parallelise alongside items.
        futures = {
            pool.submit(_infer_and_eval, i, s): (i, s)
            for i in range(total) for s in range(n_samples)
        }
        for fut in concurrent.futures.as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                logger.info(
                    "[academic] %s: cancel requested — aborting at %d/%d "
                    "(in-flight requests will drain up to request_timeout)",
                    name, completed, total_tasks,
                )
                for pending in futures:
                    pending.cancel()
                canceled = True
                break
            idx, s, correct, response, err, perf = fut.result()
            results[idx][s] = correct
            if len(sample_responses) < 3:
                sample_responses.append((idx, correct, response))
            completed += 1
            if correct:
                correct_so_far += 1
            if err is not None:
                failed_so_far += 1
                if len(error_samples) < 3:
                    error_samples.append(err)
            else:
                if perf.get("ttft_ms") is not None:
                    ttfts.append(perf["ttft_ms"])
                if perf.get("tpot_ms") is not None:
                    tpots.append(perf["tpot_ms"])
                if perf.get("output_tps") is not None:
                    out_tpss.append(perf["output_tps"])
                if perf.get("completion_tokens"):
                    sum_completion_tokens += perf["completion_tokens"]

            now = time.monotonic()
            milestone = (completed % log_every == 0) or completed == total_tasks
            heartbeat = (now - last_log_t) >= heartbeat_s
            if milestone or heartbeat:
                elapsed = now - t0
                throughput = completed / elapsed if elapsed > 0 else 0.0
                eta = (total_tasks - completed) / throughput if throughput > 0 else float("inf")
                running_acc = correct_so_far / completed if completed else 0.0
                # Aggregate service throughput: all output tokens / wall time so far.
                svc_tps = sum_completion_tokens / elapsed if elapsed > 0 else 0.0
                mt, mp = _mean(ttfts), _mean(tpots)
                perf_str = (
                    (f"  ttft={mt:.0f}ms" if mt is not None else "")
                    + (f"  tpot={mp:.1f}ms" if mp is not None else "")
                    + (f"  svc_tps={svc_tps:.0f}" if sum_completion_tokens else "")
                )
                logger.info(
                    "[academic] %s: %d/%d reqs (%.1f%%)  sample_acc=%.3f  fails=%d  "
                    "elapsed=%.0fs  rate=%.2f/s  eta=%.0fs%s",
                    name, completed, total_tasks, 100.0 * completed / total_tasks,
                    running_acc, failed_so_far, elapsed, throughput, eta, perf_str,
                )
                last_log_t = now

            if progress_cb is not None and completed % max(1, total_tasks // 20) == 0:
                inner_frac = completed / total_tasks
                fraction = overall_fraction + inner_frac * overall_step
                progress_cb(fraction, f"{name}: {completed}/{total_tasks} requests")
    finally:
        # On cancel, don't block the __exit__ waiting for the full pool to drain;
        # cancel pending futures and let in-flight ones finish in the background.
        pool.shutdown(wait=not canceled, cancel_futures=canceled)

    if error_samples:
        logger.warning(
            "[academic] %s: %d requests failed during inference (sample: %s)",
            name, failed_so_far, error_samples,
        )

    # avg@k: mean accuracy over each item's first avg_k samples, then over items.
    # pass@k: item counts if ≥1 of its first pass_k samples is correct.
    avg_score = _mean([_mean([1.0 if c else 0.0 for c in row[:avg_k]]) or 0.0 for row in results]) or 0.0
    pass_score = _mean([1.0 if any(row[:pass_k]) else 0.0 for row in results]) or 0.0
    correct_samples = sum(1 for row in results for c in row if c)
    elapsed = time.monotonic() - t0

    # Generation-performance summary. Means carry their sample counts so the
    # suite can re-aggregate a correct cross-benchmark mean (see OpenCompassTest.run).
    perf_summary = {
        "ttft_ms_mean": _mean(ttfts),
        "tpot_ms_mean": _mean(tpots),
        "output_tps_mean": _mean(out_tpss),
        "service_tps": (sum_completion_tokens / elapsed) if elapsed > 0 else None,
        "completion_tokens_total": sum_completion_tokens,
        "n_ttft": len(ttfts),
        "n_tpot": len(tpots),
        "n_out_tps": len(out_tpss),
        "elapsed_s": elapsed,
    }

    if canceled:
        logger.info(
            "[academic] %s: CANCELED partial=%d/%d reqs correct_samples=%d in %.1fs",
            name, completed, total_tasks, correct_samples, elapsed,
        )
        return {
            "score": None,
            "total": total,
            "n_samples": n_samples,
            "completed": completed,
            "correct_samples": correct_samples,
            "elapsed_s": elapsed,
            "canceled": True,
            "perf": perf_summary,
        }
    logger.info(
        "[academic] %s: avg@%d=%.4f  pass@%d=%.4f  (items=%d, samples=%d, "
        "correct_samples=%d) in %.1fs",
        name, avg_k, avg_score, pass_k, pass_score, total, total_tasks,
        correct_samples, elapsed,
    )

    # Log sampled responses for debugging low scores
    for idx, correct, response in sample_responses:
        preview = (response[:400] + "...") if len(response) > 400 else response
        preview = preview.replace("\n", " ")
        logger.info(
            "[academic] %s sample item=%d correct=%s response=%r",
            name, idx, correct, preview,
        )

    return {
        "score": avg_score,          # avg@k — the reported per-benchmark score
        "avg_k": avg_k,
        "pass_k": pass_k,
        "pass_at_k": pass_score,
        "n_samples": n_samples,
        "total": total,
        "total_samples": total_tasks,
        "correct_samples": correct_samples,
        "elapsed_s": elapsed,
        "perf": perf_summary,
    }


def _fmt_ms(v: Optional[float]) -> str:
    return ("%.0fms" % v) if v is not None else "n/a"


def _aggregate_perf(details: Dict[str, Dict]) -> Dict[str, Any]:
    """Roll per-benchmark perf summaries into one suite-level perf dict.

    Cross-benchmark means are count-weighted (each benchmark's mean carries its
    sample count), so a 5-item dataset doesn't sway the average like a 500-item
    one. service_tps is total output tokens / total inference wall time.
    """
    ttft_sum = ttft_n = 0.0
    tpot_sum = tpot_n = 0.0
    otps_sum = otps_n = 0.0
    gen_tokens = 0
    wall = 0.0
    for res in details.values():
        p = res.get("perf") if isinstance(res, dict) else None
        if not p:
            continue
        if p.get("ttft_ms_mean") is not None and p.get("n_ttft"):
            ttft_sum += p["ttft_ms_mean"] * p["n_ttft"]; ttft_n += p["n_ttft"]
        if p.get("tpot_ms_mean") is not None and p.get("n_tpot"):
            tpot_sum += p["tpot_ms_mean"] * p["n_tpot"]; tpot_n += p["n_tpot"]
        if p.get("output_tps_mean") is not None and p.get("n_out_tps"):
            otps_sum += p["output_tps_mean"] * p["n_out_tps"]; otps_n += p["n_out_tps"]
        gen_tokens += p.get("completion_tokens_total", 0) or 0
        wall += p.get("elapsed_s", 0.0) or 0.0

    return {
        "ttft_mean_ms": (ttft_sum / ttft_n) if ttft_n else None,
        "tpot_mean_ms": (tpot_sum / tpot_n) if tpot_n else None,
        "output_tps": (otps_sum / otps_n) if otps_n else None,
        "service_tps": (gen_tokens / wall) if wall > 0 and gen_tokens else None,
        "gen_tokens": gen_tokens,
    }


# ---------------------------------------------------------------------------
# Per-benchmark wrappers
# ---------------------------------------------------------------------------

def _run_aime2025(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0):
    items = _load_aime2025(data_dir, sample_cap)

    def build(item):
        # MathArena: single user message — instruction then problem, no system role.
        return [
            {"role": "user", "content": f"{_AIME_INSTRUCTION}\n\n{item['question']}"},
        ]

    def evaluate(response, item):
        return _eval_aime2025(response, item["answer"])

    return _run_benchmark("aime2025", items, build, evaluate, api_url, model, api_key, max_workers, timeout, max_tokens, progress_cb, overall_fraction, overall_step, cancel_event=cancel_event, stream=stream, avg_k=avg_k, pass_k=pass_k, temperature=temperature, top_p=top_p)


def _run_gpqa_diamond(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0):
    raw = _load_gpqa_diamond(data_dir, sample_cap)
    items = []
    for row in raw:
        q = row.get("Question", "").strip()
        correct = row.get("Correct Answer", "").strip()
        wrongs = [
            row.get("Incorrect Answer 1", "").strip(),
            row.get("Incorrect Answer 2", "").strip(),
            row.get("Incorrect Answer 3", "").strip(),
        ]
        if not q or not correct:
            continue
        # Deterministic shuffle based on question hash to avoid position bias.
        all_opts = [correct] + wrongs
        seed = int(hashlib.md5(q.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        rng.shuffle(all_opts)
        letters = "ABCD"
        options = {letters[i]: all_opts[i] for i in range(4)}
        correct_letter = letters[all_opts.index(correct)]
        items.append({"question": q, "options": options, "correct": correct_letter})

    def build(item):
        # simple-evals: single user message, no system role.
        user = _GPQA_QUERY_TMPL.format(
            question=item["question"],
            A=item["options"]["A"],
            B=item["options"]["B"],
            C=item["options"]["C"],
            D=item["options"]["D"],
        )
        return [
            {"role": "user", "content": user},
        ]

    def evaluate(response, item):
        return _eval_gpqa(response, item["correct"])

    return _run_benchmark("gpqa_diamond", items, build, evaluate, api_url, model, api_key, max_workers, timeout, max_tokens, progress_cb, overall_fraction, overall_step, cancel_event=cancel_event, stream=stream, avg_k=avg_k, pass_k=pass_k, temperature=temperature, top_p=top_p)


def _run_ifeval(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0):
    items = _load_ifeval(data_dir, sample_cap)

    def build(item):
        return [{"role": "user", "content": item["prompt"]}]

    def evaluate(response, item):
        return _eval_ifeval(
            response,
            item["instruction_id_list"],
            item.get("kwargs", [{}] * len(item["instruction_id_list"])),
        )

    return _run_benchmark("ifeval", items, build, evaluate, api_url, model, api_key, max_workers, timeout, max_tokens, progress_cb, overall_fraction, overall_step, cancel_event=cancel_event, stream=stream, avg_k=avg_k, pass_k=pass_k, temperature=temperature, top_p=top_p)


def _run_mmlu_pro(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0):
    items = _load_mmlu_pro(data_dir, sample_cap)
    letters = "ABCDEFGHIJ"

    def build(item):
        opts_text = "\n".join(
            f"{letters[i]}) {opt}" for i, opt in enumerate(item["options"])
        )
        user = _MMLU_USER_TMPL.format(question=item["question"], options=opts_text)
        return [
            {"role": "system", "content": _MMLU_SYSTEM},
            {"role": "user", "content": user},
        ]

    def evaluate(response, item):
        ans_idx = item.get("answer_index")
        if ans_idx is None:
            ans_idx = item.get("options", []).index(item["answer"]) if item.get("answer") in item.get("options", []) else -1
        if ans_idx < 0 or ans_idx >= len(letters):
            return False
        return _eval_mmlu(response, letters[ans_idx])

    return _run_benchmark("mmlu_pro", items, build, evaluate, api_url, model, api_key, max_workers, timeout, max_tokens, progress_cb, overall_fraction, overall_step, cancel_event=cancel_event, stream=stream, avg_k=avg_k, pass_k=pass_k, temperature=temperature, top_p=top_p)


def _judge_hle(judge_url: str, judge_model: str, judge_key: str,
               judge_max_tokens: int, question: str, correct_answer: str,
               response: str, timeout: float,
               stats: Optional[Dict[str, Any]] = None) -> Optional[Tuple[bool, int]]:
    """Grade one HLE answer with the LLM judge. None => caller should fall back.

    Unlike SimpleQA (where a judge problem means the sample is written off as
    NOT_ATTEMPTED), HLE has a real local grader to fall back on, so a failure
    here degrades to exact-match rather than to a wrong answer.
    """
    prompt = _HLE_JUDGE_TEMPLATE.format(
        question=question, correct_answer=correct_answer, response=response,
    )
    try:
        text = _judge_call(judge_url, judge_model, judge_key,
                           judge_max_tokens, prompt, timeout, label="hle")
    except Exception as exc:
        _note_judge_problem(stats, "judge_failures", f"judge call failed: {exc}",
                            label="hle", consequence="graded by exact-match instead")
        return None
    verdict = _parse_hle_judgement(text)
    if verdict is None:
        snippet = text.strip().replace("\n", " ")[:200]
        _note_judge_problem(
            stats, "judge_unparsed",
            f"no 'correct: yes|no' verdict in judge reply {snippet!r} — the reply "
            f"may have been cut off before the verdict (hle_judge_max_tokens is "
            f"{judge_max_tokens})",
            label="hle", consequence="graded by exact-match instead",
        )
    return verdict


def _run_hle(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0, judge_api_url="", judge_model="", judge_api_key="", use_judge=HLE_USE_JUDGE, judge_max_tokens=HLE_JUDGE_MAX_TOKENS):
    items = _load_hle(data_dir, sample_cap)

    # Official HLE grades with an LLM judge and has no exact-match path. Use one
    # when configured; otherwise fall back to the local grader, which can only
    # under-count. Unlike SimpleQA there is no self-judge default: a model
    # deciding whether its own free-form answer matches the gold answer is a
    # much weaker check than the same model answering a factoid, and it would
    # silently double the inference bill of the largest benchmark in the suite.
    judge_configured = bool(judge_api_url or judge_model)
    use_llm_judge = bool(use_judge and judge_configured)
    j_url = judge_api_url or api_url
    j_model = judge_model or model
    j_key = judge_api_key or (api_key if not judge_api_url else "")
    if use_llm_judge:
        detail = f"model={j_model} endpoint={j_url} max_tokens={judge_max_tokens}"
    elif judge_configured:
        detail = "judge configured but disabled via hle_use_judge"
    else:
        detail = ("no judge configured; official HLE grades every answer with an "
                  "LLM judge, so this score is a FLOOR on the true accuracy")
    logger.info("[hle] grader=%s (%s)",
                "llm_judge" if use_llm_judge else "exact_match", detail)

    # Fail-fast probe before any inference is spent. A broken judge would not
    # zero the run (exact-match catches it), but it would silently produce a
    # materially different, lower number than the one that was asked for.
    if use_llm_judge:
        probe_prompt = _HLE_JUDGE_TEMPLATE.format(
            question="What is the capital of France?",
            correct_answer="Paris",
            response="Explanation: It is Paris.\nAnswer: Paris\nConfidence: 95%",
        )
        try:
            probe_reply = _judge_call(j_url, j_model, j_key, judge_max_tokens,
                                      probe_prompt, timeout, label="hle")
        except Exception as exc:
            raise RuntimeError(
                f"HLE judge preflight failed against {j_url} (model {j_model!r}): "
                f"{exc} — fix the judge_api_url/judge_model/judge_api_key params, "
                f"or set hle_use_judge=false to grade HLE by exact-match on "
                f"purpose. Refusing to silently grade the whole benchmark with "
                f"the weaker fallback."
            ) from exc
        probe = _parse_hle_judgement(probe_reply)
        if probe is None:
            snippet = probe_reply.strip().replace("\n", " ")[:200]
            raise RuntimeError(
                f"HLE judge preflight: {j_model!r} at {j_url} answered but no "
                f"'correct: yes|no' verdict could be parsed from {snippet!r}. The "
                f"reply was likely cut off before the verdict — raise "
                f"hle_judge_max_tokens (currently {judge_max_tokens})."
            )
        if not probe[0]:
            logger.warning(
                "[hle] judge preflight: %s graded a trivially-correct sample "
                "'no' — this judge may grade unreliably", j_model,
            )
        else:
            logger.info("[hle] judge preflight OK (%s at %s)", j_model, j_url)

    # (item, sample) -> (correct, confidence 0-100). Keyed like SimpleQA so the
    # breakdown covers exactly the samples `score` averages over.
    judged: Dict[Tuple[int, int], Tuple[bool, int]] = {}
    judged_lock = threading.Lock()
    judge_stats: Dict[str, Any] = {
        "judge_failures": 0, "judge_unparsed": 0, "fallbacks": 0,
        "lock": threading.Lock(),
    }

    def build(item):
        return [
            {"role": "system", "content": _HLE_SYSTEM},
            {"role": "user", "content": item["question"]},
        ]

    def evaluate(response, item, idx, sample):
        # NOTE: like SimpleQA, the judge call below runs on a _run_benchmark
        # worker thread and holds an inference slot for its whole duration. See
        # the comment in _run_simpleqa — the same pool coupling applies here.
        verdict = None
        if use_llm_judge:
            verdict = _judge_hle(
                j_url, j_model, j_key, judge_max_tokens,
                item.get("question", ""), item.get("answer", ""), response,
                timeout, stats=judge_stats,
            )
        if verdict is None:
            if use_llm_judge:
                with judge_stats["lock"]:
                    judge_stats["fallbacks"] += 1
            # Confidence comes from the model's own "Confidence:" line, which the
            # official system prompt asks for; the judge would have extracted the
            # same number.
            verdict = (
                _eval_hle(response, item.get("answer", ""), item.get("answer_type", "")),
                _extract_confidence(response),
            )
        with judged_lock:
            judged[(idx, sample)] = verdict
        return verdict[0]

    result = _run_benchmark(
        "hle", items, build, evaluate, api_url, model, api_key, max_workers,
        timeout, max_tokens, progress_cb, overall_fraction, overall_step,
        cancel_event=cancel_event, stream=stream, avg_k=avg_k, pass_k=pass_k,
        temperature=temperature, top_p=top_p,
    )

    result["grader"] = "llm_judge" if use_llm_judge else "exact_match"
    if use_llm_judge:
        fallbacks = judge_stats["fallbacks"]
        result["judge_failures"] = judge_stats["judge_failures"]
        result["judge_unparsed"] = judge_stats["judge_unparsed"]
        result["judge_fallbacks"] = fallbacks
        if fallbacks:
            logger.warning(
                "[hle] judge degraded: %d call failures, %d unparseable replies "
                "— %d of %d graded samples fell back to exact-match, which "
                "under-counts",
                judge_stats["judge_failures"], judge_stats["judge_unparsed"],
                fallbacks, len(judged),
            )

    if not result.get("canceled"):
        breakdown = _hle_breakdown(judged, int(result.get("total") or 0), avg_k)
        if breakdown:
            result.update(breakdown)
            logger.info(
                "[hle] accuracy=%.4f +/- %.4f  calibration_error=%s  "
                "mean_confidence=%.3f  (graded=%d/%d scored, grader=%s)",
                breakdown["accuracy"], breakdown["accuracy_ci95_half_width"],
                ("%.1f" % breakdown["calibration_error"])
                if "calibration_error" in breakdown else "n/a",
                breakdown["confidence_mean"], breakdown["graded"],
                breakdown["scored"], result["grader"],
            )
    return result


def _run_livecodebench_v6(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0):
    items = _load_livecodebench_v6(data_dir, sample_cap)

    def build(item):
        problem = f"{item.get('question_title', '')}\n\n{item.get('question_content', '')}"
        starter = item.get("starter_code", "")
        if starter:
            problem += f"\n\nStarter code:\n{starter}"
        return [
            {"role": "system", "content": _LCB_SYSTEM},
            {"role": "user", "content": problem},
        ]

    def evaluate(response, item):
        return _eval_livecodebench(response, item)

    return _run_benchmark(
        "livecodebench_v6", items, build, evaluate, api_url, model, api_key,
        max_workers=min(max_workers, 4),  # code execution is CPU-heavy
        request_timeout=timeout,
        max_tokens=max_tokens,
        progress_cb=progress_cb,
        overall_fraction=overall_fraction,
        overall_step=overall_step,
        cancel_event=cancel_event,
        stream=stream,
        avg_k=avg_k,
        pass_k=pass_k,
        temperature=temperature,
        top_p=top_p,
    )


# Judge-call retry policy. Transient failures (timeouts, connection resets,
# 429 rate limiting, 5xx) get retried with backoff before the sample is written
# off — an external judge running at max_workers concurrency WILL see 429s from
# commercial endpoints, and without retries each one silently became a
# NOT_ATTEMPTED grade. Hard errors (401 bad key, 404 wrong model/URL) never
# heal, so they fail immediately.
_JUDGE_ATTEMPTS = 3
_JUDGE_BACKOFF_S = (1.0, 4.0)

# First N judge problems of each kind are logged at WARNING so a misconfigured
# judge is visible in the worker log (default level INFO — debug lines vanish);
# the rest drop to debug to avoid flooding a 4k-sample run.
_JUDGE_WARN_LIMIT = 3


def _judge_call(judge_url: str, judge_model: str, judge_key: str,
                judge_max_tokens: int, prompt: str, timeout: float,
                label: str = "judge") -> str:
    """One judge chat call with retry on transient failures.

    Returns the grader's reply text; raises the last error when the call is
    hopeless (auth/URL/model errors) or retries are exhausted. ``label`` only
    names the benchmark in log lines (SimpleQA and HLE both grade this way).
    """
    import requests

    for attempt in range(_JUDGE_ATTEMPTS):
        try:
            text, _ = _chat_complete(
                judge_url, judge_model, judge_key,
                [{"role": "user", "content": prompt}],
                max_tokens=judge_max_tokens, temperature=0.0, top_p=1.0,
                timeout=timeout, stream=False,
            )
            return text
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            transient = (
                isinstance(exc, (requests.Timeout, requests.ConnectionError))
                or status == 429
                or (status is not None and status >= 500)
            )
            if not transient or attempt == _JUDGE_ATTEMPTS - 1:
                raise
            delay = _JUDGE_BACKOFF_S[min(attempt, len(_JUDGE_BACKOFF_S) - 1)]
            logger.debug(
                "[%s] transient judge error (attempt %d/%d, retrying in %.0fs): %s",
                label, attempt + 1, _JUDGE_ATTEMPTS, delay, exc,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # loop always returns or raises


def _note_judge_problem(stats: Optional[Dict[str, Any]], kind: str, detail: str,
                        label: str = "simpleqa",
                        consequence: str = "sample graded NOT_ATTEMPTED") -> None:
    """Count a judge failure/unparsed-reply and log the first few loudly.

    ``consequence`` states what the degradation actually cost, which differs by
    benchmark: SimpleQA scores the sample NOT_ATTEMPTED, HLE falls back to its
    local exact-match grader.
    """
    n = 1
    if stats is not None:
        with stats["lock"]:
            stats[kind] += 1
            n = stats[kind]
    if n <= _JUDGE_WARN_LIMIT:
        suffix = " (further occurrences logged at debug)" if n == _JUDGE_WARN_LIMIT else ""
        logger.warning("[%s] %s — %s%s", label, detail, consequence, suffix)
    else:
        logger.debug("[%s] %s", label, detail)


# Grade extraction. The grader is asked for a bare letter, but two things break
# that: a reasoning judge emits prose, and when it spends its whole budget
# thinking, _chat_complete returns the (possibly truncated) reasoning trace
# instead of an empty content string. So:
#   1. Prefer the reply's LAST non-empty line when that line IS the grade,
#      allowing light decoration ("A", "(A)", "**B**", "Grade: C").
#   2. Only if that fails, fall back to the last standalone A/B/C anywhere.
# The fallback is a heuristic and can misfire on a trace cut off mid-thought
# ("...it isn't C, and not A either" ends on A), which is why the anchored
# match is tried first. Upstream simple-evals takes the FIRST match, which
# has the mirror problem: it grabs the letter from the judge's opening recap.
_GRADE_LABEL_RE = re.compile(
    r"^(?:the\s+)?(?:final\s+)?(?:grade|answer|verdict|result)\s*(?:is)?\s*[:\-–]?\s*",
    re.IGNORECASE,
)
_GRADE_LINE_RE = re.compile(r"^[\*_`\"'\(\[\s]*([ABC])[\*_`\"'\)\]\.\,\:\s]*$")


def _parse_simpleqa_grade(text: str) -> Optional[str]:
    """Extract the A/B/C grade from a judge reply. None when no grade is found."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if lines:
        last = _GRADE_LABEL_RE.sub("", lines[-1]).strip()
        m = _GRADE_LINE_RE.match(last)
        if m:
            return m.group(1)
    letters = re.findall(r"\b([ABC])\b", text or "")
    return letters[-1] if letters else None


# HLE judgement extraction. Upstream gets these fields as validated JSON via
# structured outputs; we read them out of the labelled text the same prompt asks
# for (see _HLE_JUDGE_TEMPLATE). Both take the LAST match: the template orders
# the fields extracted_final_answer -> reasoning -> correct -> confidence, so the
# verdict comes after any "...is correct:" phrasing inside the reasoning.
_HLE_CORRECT_RE = re.compile(r"correct\s*[\"']?\s*[:=]\s*[\"'\s]*(yes|no)\b", re.IGNORECASE)
_HLE_CONFIDENCE_RE = re.compile(r"confidence\s*[\"']?\s*[:=]\s*[\"'\s]*(\d{1,3})", re.IGNORECASE)


def _parse_hle_judgement(text: str) -> Optional[Tuple[bool, int]]:
    """Extract (correct, confidence) from an HLE judge reply. None if no verdict.

    Upstream reads ``"yes" in correct``; confidence defaults to 100 when the
    judge omits it, matching the template's own instruction.
    """
    verdicts = _HLE_CORRECT_RE.findall(text or "")
    if not verdicts:
        return None
    confidences = _HLE_CONFIDENCE_RE.findall(text or "")
    confidence = max(0, min(100, int(confidences[-1]))) if confidences else 100
    return verdicts[-1].lower() == "yes", confidence


def _calibration_error(confidences: List[float], corrects: List[bool],
                       beta: int = 100) -> Optional[float]:
    """RMS calibration error, ported from the HLE harness (calib_err, p='2').

    ``confidences`` are 0-1. Bins of ``beta`` samples by ascending confidence;
    each bin contributes |mean(confidence) - mean(correct)|^2 weighted by its
    share of the samples; the result is the square root of that sum.

    Two deliberate departures from upstream, both about small runs — upstream
    only ever runs the full 2,500-item set:
      * upstream iterates ``range(len(bins) - 1)``, i.e. it drops its final bin.
        We keep that (the weights therefore sum to <1) so the number stays
        comparable to published ones, EXCEPT when there is only one bin, where
        dropping it would return a meaningless 0.
      * upstream indexes ``bins[-1]`` unconditionally and so raises IndexError
        below ``beta`` samples. We fall back to a single bin instead, which is
        what a sample-capped run needs.
    """
    n = len(confidences)
    if n == 0 or n != len(corrects):
        return None
    order = sorted(range(n), key=lambda i: confidences[i])
    conf = [confidences[i] for i in order]
    corr = [1.0 if corrects[i] else 0.0 for i in order]

    bins = [[i * beta, (i + 1) * beta] for i in range(n // beta)]
    if not bins:
        bins = [[0, n]]
    else:
        bins[-1] = [bins[-1][0], n]
    usable = bins[:-1] if len(bins) > 1 else bins

    cerr = 0.0
    for lo, hi in usable:
        size = hi - lo
        if size <= 0:
            continue
        diff = abs(sum(conf[lo:hi]) / size - sum(corr[lo:hi]) / size)
        cerr += size / n * (diff ** 2)
    return cerr ** 0.5


def _hle_breakdown(judged: Dict[Tuple[int, int], Tuple[bool, int]], n_items: int,
                   avg_k: int = 1) -> Dict[str, Any]:
    """HLE's official metric set: accuracy, Wald 95% half-width, calibration error.

    ``judged`` maps (item, sample) to (correct, confidence 0-100). Like
    _simpleqa_breakdown, only samples below ``avg_k`` count, and the denominator
    is the number of SCORED requests — upstream divides by the full question
    count too, so a request that never produced an answer counts against the
    score rather than shrinking the denominator.
    """
    avg_k = max(int(avg_k), 1)
    rows = [v for (_idx, s), v in judged.items() if s < avg_k]
    n_scored = max(int(n_items), 0) * avg_k
    if not n_scored or not rows:
        return {}
    corrects = [c for c, _conf in rows]
    confidences = [conf / 100.0 for _c, conf in rows]
    accuracy = sum(1 for c in corrects if c) / n_scored
    # Wald 95% interval, as printed by the official dump_metrics.
    half_width = 1.96 * ((accuracy * (1.0 - accuracy) / n_scored) ** 0.5)
    cerr = _calibration_error(confidences, corrects)
    out: Dict[str, Any] = {
        "accuracy": accuracy,
        "accuracy_ci95_half_width": half_width,
        "graded": len(rows),
        "scored": n_scored,
        "ungraded_rate": max(0, n_scored - len(rows)) / n_scored,
        "confidence_mean": sum(confidences) / len(confidences),
    }
    if cerr is not None:
        # Reported on the 0-100 scale the HLE leaderboard uses.
        out["calibration_error"] = 100.0 * cerr
    return out


def _simpleqa_breakdown(grades: Dict[Tuple[int, int], str], n_items: int,
                        avg_k: int = 1) -> Dict[str, Any]:
    """simple-evals metric set for SimpleQA. Empty dict when there is nothing to report.

    ``grades`` maps (item index, sample index) to the A/B/C grade the judge
    returned. Only samples below ``avg_k`` are counted, so the breakdown covers
    exactly the requests the headline ``score`` (avg@k) averages over — the
    extra samples drawn when pass_k > avg_k are excluded rather than silently
    inflating the denominator.

    Every rate is over SCORED requests (``n_items * avg_k``). That differs from
    the number of grades when inference failed: such a request never reaches the
    grader, but ``score`` still counts it wrong. Rather than quietly shrinking
    the denominator (which would make ``correct_rate`` disagree with ``score``),
    that mass is reported as ``ungraded_rate``, so correct_rate == score and the
    four rates sum to 1. Upstream simple-evals grades every sample and so has no
    such bucket: ungraded_rate > 0 means the number is not comparable to a
    published SimpleQA score.

    ``accuracy_given_attempted`` is conditioned on the model having attempted an
    answer, so it is graded-only by definition — a failed request is not a
    refusal. ``f1`` is the harmonic mean of it and ``correct_rate``, which is
    the F-score headlined in the SimpleQA paper.
    """
    avg_k = max(int(avg_k), 1)
    graded = [g for (_idx, s), g in grades.items() if s < avg_k]
    n_scored = max(int(n_items), 0) * avg_k
    n = len(graded)
    if not n_scored or not n:
        return {}
    c = graded.count("A")
    i = graded.count("B")
    na = graded.count("C")
    ungraded = max(0, n_scored - n)
    correct_rate = c / n_scored
    attempted = c + i
    acc_given_attempted = (c / attempted) if attempted else 0.0
    denom = acc_given_attempted + correct_rate
    return {
        "correct_rate": correct_rate,
        "incorrect_rate": i / n_scored,
        "not_attempted_rate": na / n_scored,
        "ungraded_rate": ungraded / n_scored,
        "accuracy_given_attempted": acc_given_attempted,
        "f1": (2 * acc_given_attempted * correct_rate / denom) if denom > 0 else 0.0,
        "graded": n,
        "scored": n_scored,
    }


def _grade_simpleqa(judge_url: str, judge_model: str, judge_key: str,
                    judge_max_tokens: int, question: str, target: str,
                    predicted: str, timeout: float,
                    stats: Optional[Dict[str, Any]] = None) -> str:
    """Grade one SimpleQA answer with the LLM judge. Returns 'A'/'B'/'C'.

    'A'=CORRECT, 'B'=INCORRECT, 'C'=NOT_ATTEMPTED. Defaults to 'C' (the
    simple-evals default) when the grader reply has no A/B/C or the call fails;
    each such fallback is counted in ``stats`` and surfaced in the run summary.
    """
    prompt = _SIMPLEQA_GRADER_TEMPLATE.format(
        question=question, target=target, predicted_answer=predicted,
    )
    try:
        text = _judge_call(judge_url, judge_model, judge_key,
                           judge_max_tokens, prompt, timeout, label="simpleqa")
    except Exception as exc:
        _note_judge_problem(stats, "judge_failures", f"judge call failed: {exc}")
        return "C"
    # Fall back to 'C' (not-attempted) when no grade can be read, so a garbled or
    # truncated judge reply is never scored correct.
    grade = _parse_simpleqa_grade(text)
    if grade is None:
        snippet = text.strip().replace("\n", " ")[:200]
        _note_judge_problem(
            stats, "judge_unparsed",
            f"no standalone A/B/C in judge reply {snippet!r} — a reasoning judge "
            f"may need a larger judge_max_tokens (current {judge_max_tokens})",
        )
        return "C"
    return grade


def _run_simpleqa(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0, judge_api_url="", judge_model="", judge_api_key="", judge_max_tokens=JUDGE_MAX_TOKENS):
    items = _load_simpleqa(data_dir, sample_cap)

    # Judge falls back to the target endpoint/model (self-judge). When a separate
    # judge endpoint IS set, only an explicit judge key authenticates it (the
    # target api_key generally won't work against a different endpoint).
    external_judge = bool(judge_api_url or judge_model)
    j_url = judge_api_url or api_url
    j_model = judge_model or model
    j_key = judge_api_key or (api_key if not judge_api_url else "")
    logger.info(
        "[simpleqa] grader model=%s endpoint=%s (%s)  judge_max_tokens=%d",
        j_model, j_url,
        "external" if external_judge else "self-judge", judge_max_tokens,
    )

    # Fail-fast probe of an EXTERNAL judge before any inference is spent. Every
    # judge problem downstream degrades to a NOT_ATTEMPTED grade (never an
    # error), so a bad judge URL/key/model would otherwise burn the full
    # benchmark's inference cost and report a silent 0. Self-judge is exempt:
    # the target endpoint is exercised by the inference calls themselves, and a
    # transient blip here shouldn't kill an otherwise-healthy run.
    if external_judge:
        probe_prompt = _SIMPLEQA_GRADER_TEMPLATE.format(
            question="What is the capital of France?",
            target="Paris",
            predicted_answer="The capital of France is Paris.",
        )
        try:
            probe_reply = _judge_call(j_url, j_model, j_key, judge_max_tokens,
                                      probe_prompt, timeout, label="simpleqa")
        except Exception as exc:
            raise RuntimeError(
                f"SimpleQA judge preflight failed against {j_url} (model {j_model!r}): "
                f"{exc} — fix the judge_api_url/judge_model/judge_api_key params; "
                f"refusing to run the benchmark when every grade would silently "
                f"become NOT_ATTEMPTED."
            ) from exc
        if _parse_simpleqa_grade(probe_reply) is None:
            snippet = probe_reply.strip().replace("\n", " ")[:200]
            raise RuntimeError(
                f"SimpleQA judge preflight: {j_model!r} at {j_url} answered but no "
                f"standalone A/B/C grade could be parsed from {snippet!r}. A "
                f"reasoning judge may need a larger judge_max_tokens "
                f"(current {judge_max_tokens}), or use a non-reasoning grader."
            )
        logger.info("[simpleqa] judge preflight OK (%s at %s)", j_model, j_url)

    # Grades keyed by (item index, sample index) so the breakdown below can be
    # sliced to the same first-avg_k samples that `score` averages over. A flat
    # list would silently mix in the extra samples drawn when pass_k > avg_k.
    grades: Dict[Tuple[int, int], str] = {}
    grades_lock = threading.Lock()
    judge_stats: Dict[str, Any] = {
        "judge_failures": 0, "judge_unparsed": 0, "lock": threading.Lock(),
    }

    def build(item):
        # Single user message = bare question, no system prompt (simple-evals).
        return [{"role": "user", "content": item.get("problem", "")}]

    def evaluate(response, item, idx, sample):
        # NOTE: this runs on a _run_benchmark worker thread, so the judge call
        # below occupies an INFERENCE slot for its whole duration — judge
        # concurrency is pinned to max_workers with no separate cap, and each
        # item costs inference latency + judge latency serially. That coupling
        # is what generates the 429 bursts the retry policy above absorbs. If it
        # becomes a bottleneck, the fix is a separate (smaller) judge pool or a
        # semaphore around _judge_call, not more retries.
        grade = _grade_simpleqa(
            j_url, j_model, j_key, judge_max_tokens,
            item.get("problem", ""), item.get("answer", ""), response, timeout,
            stats=judge_stats,
        )
        with grades_lock:
            grades[(idx, sample)] = grade
        return grade == "A"   # headline correctness = CORRECT (grade A) only

    result = _run_benchmark(
        "simpleqa", items, build, evaluate, api_url, model, api_key,
        max_workers, timeout, max_tokens, progress_cb, overall_fraction,
        overall_step, cancel_event=cancel_event, stream=stream,
        avg_k=avg_k, pass_k=pass_k, temperature=temperature, top_p=top_p,
    )

    # Surface judge health in the result + log so degraded grading is never
    # silent: every counted problem below was scored NOT_ATTEMPTED, deflating
    # the benchmark. (The preflight above catches total judge outages; these
    # counters catch partial ones — rate limiting, mid-run endpoint trouble.)
    judge_failures = judge_stats["judge_failures"]
    judge_unparsed = judge_stats["judge_unparsed"]
    if judge_failures or judge_unparsed:
        logger.warning(
            "[simpleqa] judge degraded: %d call failures, %d unparseable replies "
            "out of %d graded samples — each was scored NOT_ATTEMPTED, deflating "
            "the SimpleQA score",
            judge_failures, judge_unparsed, len(grades),
        )
    result["judge_failures"] = judge_failures
    result["judge_unparsed"] = judge_unparsed

    # A canceled run has grades for only the samples that finished, so every rate
    # would be deflated by whatever share never ran. `score` is None there for
    # the same reason; don't publish a breakdown either.
    if not result.get("canceled"):
        breakdown = _simpleqa_breakdown(grades, int(result.get("total") or 0), avg_k)
        if breakdown:
            result.update(breakdown)
            logger.info(
                "[simpleqa] correct=%.3f incorrect=%.3f not_attempted=%.3f "
                "acc_given_attempted=%.3f f1=%.3f (graded=%d/%d scored)%s",
                breakdown["correct_rate"], breakdown["incorrect_rate"],
                breakdown["not_attempted_rate"], breakdown["accuracy_given_attempted"],
                breakdown["f1"], breakdown["graded"], breakdown["scored"],
                f"  WARNING: {breakdown['scored'] - breakdown['graded']} request(s) "
                f"failed inference and were scored wrong without a grade — not "
                f"comparable to published SimpleQA scores"
                if breakdown["ungraded_rate"] else "",
            )
    return result


def _run_longbench_v2(api_url, model, api_key, data_dir, max_workers, timeout, sample_cap, max_tokens, progress_cb=None, overall_fraction=0.0, overall_step=0.0, cancel_event=None, stream=True, avg_k=1, pass_k=1, temperature=0.0, top_p=1.0, max_input_tokens=LONGBENCH_MAX_INPUT_TOKENS):
    items = _load_longbench_v2(data_dir, sample_cap)

    # Clamp the input budget so prompt + output fits the server's context window.
    # vLLM rejects a request with 400 when prompt_tokens + max_tokens >
    # max_model_len, and LongBench-v2 contexts are easily large enough to trip
    # that. Reserve the output budget (max_tokens) plus a margin for the chat
    # template, the question/choices, and tokenizer skew (we count input with
    # tiktoken cl100k, but the server enforces the limit with the model's own
    # tokenizer). max_model_len is read from /v1/models; if it's unavailable we
    # leave the configured budget untouched.
    effective_input_tokens = max_input_tokens
    model_max = _get_model_max_len(api_url, model, api_key)
    if model_max:
        margin = max(2048, model_max // 20)   # >=2048, or 5% of the window
        safe_input = model_max - max_tokens - margin
        if safe_input < 1024:
            logger.warning(
                "[longbench_v2] max_model_len=%d minus output=%d leaves almost no "
                "room for input (safe=%d) — lower the LongBench output cap",
                model_max, max_tokens, safe_input,
            )
        if safe_input > 0 and (effective_input_tokens <= 0 or effective_input_tokens > safe_input):
            logger.warning(
                "[longbench_v2] clamping input budget %s -> %d tokens "
                "(max_model_len=%d, output=%d, margin=%d)",
                effective_input_tokens or "untruncated", safe_input,
                model_max, max_tokens, margin,
            )
            effective_input_tokens = safe_input
    else:
        logger.warning(
            "[longbench_v2] max_model_len unknown (not exposed by /v1/models) — "
            "using configured input budget %s without auto-clamp; long contexts "
            "may be rejected (400) if prompt+output exceeds the window",
            max_input_tokens or "untruncated",
        )

    records: List[Tuple[str, str, bool]] = []   # (difficulty, length, correct)
    rec_lock = threading.Lock()

    def build(item):
        context = _truncate_middle(item.get("context", "") or "", effective_input_tokens)
        user = (
            _LONGBENCH_TEMPLATE
            .replace("$DOC$", context.strip())
            .replace("$Q$", (item.get("question", "") or "").strip())
            .replace("$C_A$", (item.get("choice_A", "") or "").strip())
            .replace("$C_B$", (item.get("choice_B", "") or "").strip())
            .replace("$C_C$", (item.get("choice_C", "") or "").strip())
            .replace("$C_D$", (item.get("choice_D", "") or "").strip())
        )
        return [{"role": "user", "content": user}]

    def evaluate(response, item):
        ok = _eval_longbench_v2(response, item.get("answer", ""))
        with rec_lock:
            records.append((item.get("difficulty", ""), item.get("length", ""), ok))
        return ok

    result = _run_benchmark(
        "longbench_v2", items, build, evaluate, api_url, model, api_key,
        max_workers, timeout, max_tokens, progress_cb, overall_fraction,
        overall_step, cancel_event=cancel_event, stream=stream,
        avg_k=avg_k, pass_k=pass_k, temperature=temperature, top_p=top_p,
    )

    # Official LongBench-v2 also reports accuracy by difficulty (easy/hard) and
    # length (short/medium/long). Compute those subset accuracies from per-sample
    # records (sample-level; equals item-level at avg_k=1).
    def _subset(by_length: bool) -> Dict[str, float]:
        groups: Dict[str, List[bool]] = {}
        for diff, length, ok in records:
            key = (length if by_length else diff) or "unknown"
            groups.setdefault(key, []).append(ok)
        return {k: (sum(v) / len(v)) for k, v in groups.items() if v}

    if records:
        result["by_difficulty"] = _subset(by_length=False)
        result["by_length"] = _subset(by_length=True)
        logger.info(
            "[longbench_v2] by_difficulty=%s by_length=%s",
            result["by_difficulty"], result["by_length"],
        )
    return result


# ---------------------------------------------------------------------------
# Main test class
# ---------------------------------------------------------------------------

class _DeadlineEvent:
    """cancel_event wrapper that also fires when a wall-clock deadline passes.

    The suite enforces its module-wide time cap by wrapping the incoming cancel
    event in this proxy: every existing ``cancel_event.is_set()`` checkpoint —
    between benchmarks and inside each benchmark's result-wait loop — then doubles
    as a timeout check, with no deadline threaded through the runner functions.

    ``.is_set()`` is True when the real cancel fired OR the deadline elapsed;
    ``.timed_out`` distinguishes a timeout from a genuine user cancel. set/clear/
    wait delegate to the wrapped event so nothing else changes behaviour. Note the
    real cancel event (not this proxy) still backs the progress callback, so a
    timeout never flips the submission to CANCELED — the suite just stops and
    returns partial results, and the run completes normally.
    """

    def __init__(self, event, deadline: Optional[float]) -> None:
        self._event = event
        self._deadline = deadline  # time.monotonic() seconds, or None

    def is_set(self) -> bool:
        if self._event is not None and self._event.is_set():
            return True
        return self._deadline is not None and time.monotonic() >= self._deadline

    @property
    def timed_out(self) -> bool:
        if self._event is not None and self._event.is_set():
            return False
        return self._deadline is not None and time.monotonic() >= self._deadline

    def set(self) -> None:
        if self._event is not None:
            self._event.set()

    def clear(self) -> None:
        if self._event is not None:
            self._event.clear()

    def wait(self, timeout=None) -> bool:
        if self._event is not None:
            return self._event.wait(timeout)
        return False


class OpenCompassTest(BaseTest):
    """
    Self-implemented 6-benchmark academic leaderboard evaluation.

    Driven by the `opencompass` module (bench/modules/opencompass.py).
    """

    name = "opencompass_benchmark"

    def __init__(
        self,
        api_url: str,
        model: str,
        api_key: str,
        output_dir: str,
        dataset_dir: Optional[str] = None,
        cancel_event=None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        max_workers: int = MAX_WORKERS,
        request_timeout: float = REQUEST_TIMEOUT,
        max_seconds: float = MAX_SECONDS,
        max_tokens: int = MAX_TOKENS,
        sample_caps: Optional[Dict[str, int]] = None,
        skip_flags: Optional[Dict[str, bool]] = None,
        avg_k: Optional[Dict[str, int]] = None,
        pass_k: Optional[Dict[str, int]] = None,
        sample_temperature: float = SAMPLE_TEMPERATURE,
        sample_top_p: float = SAMPLE_TOP_P,
        force_temperature: bool = FORCE_TEMPERATURE,
        baseline_file: str = BASELINE_FILE,
        max_regression: float = MAX_REGRESSION,
        stream: bool = True,
        judge_api_url: str = JUDGE_API_URL,
        judge_model: str = JUDGE_MODEL,
        judge_api_key: str = JUDGE_API_KEY,
        judge_max_tokens: int = JUDGE_MAX_TOKENS,
        hle_use_judge: bool = HLE_USE_JUDGE,
        hle_judge_max_tokens: int = HLE_JUDGE_MAX_TOKENS,
        longbench_max_input_tokens: int = LONGBENCH_MAX_INPUT_TOKENS,
        longbench_max_output_tokens: int = LONGBENCH_MAX_OUTPUT_TOKENS,
    ) -> None:
        super().__init__(api_url, model, api_key, output_dir)
        self.data_dir = dataset_dir or ACADEMIC_DATA_DIR
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        # Hard wall-clock cap for the whole suite (seconds); 0 disables. Enforced
        # via a _DeadlineEvent wrapping cancel_event — see run().
        self.max_seconds = max_seconds
        self.max_tokens = max_tokens
        # Stream responses (SSE) so per-request TTFT/TPOT can be measured. Falls
        # back to a plain JSON parse automatically if the server ignores stream.
        self.stream = stream
        # LLM judge, shared by SimpleQA and HLE. Empty url/model means SimpleQA
        # self-judges (target model) and HLE falls back to exact-match.
        # Stripped defensively (platform params are already stripped by the
        # schema, but env/CLI callers bypass it): a pasted key with a trailing
        # newline is an invalid Authorization header, and every judge call
        # would silently grade NOT_ATTEMPTED.
        self.judge_api_url = (judge_api_url or "").strip()
        self.judge_model = (judge_model or "").strip()
        self.judge_api_key = (judge_api_key or "").strip()
        self.judge_max_tokens = judge_max_tokens
        # HLE grades with the same judge but needs its own token budget: its
        # judge writes an extraction + reasoning before the verdict, where
        # SimpleQA's emits one letter.
        self.hle_use_judge = hle_use_judge
        self.hle_judge_max_tokens = hle_judge_max_tokens
        # LongBench-v2 context middle-truncation budget (tokens; 0 = no truncation).
        self.longbench_max_input_tokens = longbench_max_input_tokens
        # LongBench-v2 output cap, overriding the suite max_tokens for this one
        # input-heavy benchmark (tokens; 0 = inherit self.max_tokens).
        self.longbench_max_output_tokens = longbench_max_output_tokens
        # Copy module defaults so callers can override individual benchmarks
        # without affecting other instances.
        self.sample_caps: Dict[str, int] = dict(_SAMPLE_CAPS)
        if sample_caps:
            self.sample_caps.update(sample_caps)
        self.skip_flags: Dict[str, bool] = dict(_SKIP_FLAGS)
        if skip_flags:
            self.skip_flags.update(skip_flags)
        # Repeat-sampling knobs: avg@k / pass@k samples per item, and the
        # temperature used when a benchmark draws >1 sample (so repeats differ).
        self.avg_k: Dict[str, int] = dict(_AVG_K)
        if avg_k:
            self.avg_k.update(avg_k)
        self.pass_k: Dict[str, int] = dict(_PASS_K)
        if pass_k:
            self.pass_k.update(pass_k)
        self.sample_temperature = sample_temperature
        self.sample_top_p = sample_top_p
        # Override: apply temperature/top_p even on single-shot (k=1) benchmarks.
        self.force_temperature = force_temperature
        self.baseline_file = baseline_file
        self.max_regression = max_regression
        self.baseline: Dict[str, float] = self._load_baseline()
        self.cancel_event = cancel_event
        self.progress_cb = progress_cb

    def run(self) -> TestResult:
        active_names = [n for n in self.skip_flags if not self.skip_flags[n]]
        logger.info(
            "[%s] Starting academic evaluation\n"
            "  endpoint        : api_url=%s  model=%s  api_key=%s\n"
            "  data_dir        : %s\n"
            "  max_workers     : %d\n"
            "  request_timeout : %.1fs\n"
            "  max_tokens      : %d\n"
            "  sample_caps     : %s\n"
            "  skip_flags      : %s\n"
            "  avg_k           : %s\n"
            "  pass_k          : %s  sample_temperature=%.2f  sample_top_p=%.2f  force_temperature=%s\n"
            "  active runners  : %s\n"
            "  baseline_file   : %s  max_regression=%.4f",
            self.name,
            self.api_url, self.model, "***" if self.api_key else "(none)",
            self.data_dir,
            self.max_workers,
            self.request_timeout,
            self.max_tokens,
            self.sample_caps,
            self.skip_flags,
            self.avg_k,
            self.pass_k, self.sample_temperature, self.sample_top_p, self.force_temperature,
            active_names,
            self.baseline_file or "(none)", self.max_regression,
        )
        t0 = time.monotonic()

        # Enforce the module-wide wall-clock cap by wrapping cancel_event: every
        # existing cancel_event.is_set() checkpoint (between benchmarks and inside
        # each benchmark's wait loop) now also trips when the deadline passes.
        # 0 = disabled. The real cancel event still backs the progress callback,
        # so a timeout stops the suite with partial results without marking the
        # submission CANCELED.
        if self.max_seconds and self.max_seconds > 0:
            self.cancel_event = _DeadlineEvent(self.cancel_event, t0 + self.max_seconds)
            logger.info("[%s] module time cap: %.0fs", self.name, self.max_seconds)

        runners = [
            ("aime2025",        _run_aime2025),
            ("gpqa_diamond",    _run_gpqa_diamond),
            ("ifeval",          _run_ifeval),
            ("mmlu_pro",        _run_mmlu_pro),
            ("hle",             _run_hle),
            ("livecodebench_v6", _run_livecodebench_v6),
            ("simpleqa",        _run_simpleqa),
            ("longbench_v2",    _run_longbench_v2),
        ]

        # Determine which benchmarks will actually run for progress allocation
        active = [
            (n, fn) for n, fn in runners
            if not self.skip_flags.get(n)
        ]
        step = 1.0 / len(active) if active else 1.0

        scores: Dict[str, float] = {}
        details: Dict[str, Dict] = {}
        timed_out = False

        for idx, (bench_name, runner_fn) in enumerate(active):
            if self.cancel_event is not None and self.cancel_event.is_set():
                if getattr(self.cancel_event, "timed_out", False):
                    timed_out = True
                    logger.warning(
                        "[%s] module time cap (%.0fs) reached after %d/%d benchmarks — "
                        "stopping with partial results",
                        self.name, self.max_seconds, idx, len(active),
                    )
                else:
                    logger.info("[%s] %s canceled — stopping", self.name, bench_name)
                break
            sample_cap = self.sample_caps.get(bench_name, 0)
            avg_k = max(int(self.avg_k.get(bench_name, 1)), 1)
            pass_k = max(int(self.pass_k.get(bench_name, 1)), 1)
            # Greedy single shot stays at temp 0 / top_p 1.0; only apply the
            # sampling nucleus when we actually draw repeats, so the k samples
            # diverge. force_temperature overrides this and applies the configured
            # temperature/top_p to every benchmark, even single-shot (k=1).
            sampling = self.force_temperature or max(avg_k, pass_k) > 1
            temperature = self.sample_temperature if sampling else 0.0
            top_p = self.sample_top_p if sampling else 1.0
            # LongBench-v2 is input-heavy and output-light, so let it cap output
            # below the suite-wide max_tokens (which may be sized for long-reasoning
            # benchmarks). This frees context-window budget for the long input —
            # see LONGBENCH_MAX_OUTPUT_TOKENS. 0 = inherit the suite max_tokens.
            effective_max_tokens = self.max_tokens
            if bench_name == "longbench_v2" and self.longbench_max_output_tokens > 0:
                effective_max_tokens = self.longbench_max_output_tokens
            suite_elapsed = time.monotonic() - t0
            logger.info(
                "[%s] >>> benchmark %d/%d: %s  (sample_cap=%s, avg@%d, pass@%d, temp=%.2f, top_p=%.2f, "
                "max_tokens=%d, suite_elapsed=%.0fs)",
                self.name, idx + 1, len(active), bench_name,
                sample_cap or "all", avg_k, pass_k, temperature, top_p,
                effective_max_tokens, suite_elapsed,
            )
            # Benchmark-specific extra kwargs: judge config for SimpleQA and HLE
            # (same endpoint, separate token budgets), context truncation budget
            # for LongBench-v2. Other runners don't accept these.
            extra: Dict[str, Any] = {}
            if bench_name == "simpleqa":
                extra = dict(
                    judge_api_url=self.judge_api_url,
                    judge_model=self.judge_model,
                    judge_api_key=self.judge_api_key,
                    judge_max_tokens=self.judge_max_tokens,
                )
            elif bench_name == "hle":
                extra = dict(
                    judge_api_url=self.judge_api_url,
                    judge_model=self.judge_model,
                    judge_api_key=self.judge_api_key,
                    use_judge=self.hle_use_judge,
                    judge_max_tokens=self.hle_judge_max_tokens,
                )
            elif bench_name == "longbench_v2":
                extra = dict(max_input_tokens=self.longbench_max_input_tokens)
            try:
                bench_t0 = time.monotonic()
                result = runner_fn(
                    self.api_url, self.model, self.api_key,
                    self.data_dir, self.max_workers, self.request_timeout,
                    sample_cap, effective_max_tokens,
                    progress_cb=self.progress_cb,
                    overall_fraction=idx * step,
                    overall_step=step,
                    cancel_event=self.cancel_event,
                    stream=self.stream,
                    avg_k=avg_k,
                    pass_k=pass_k,
                    temperature=temperature,
                    top_p=top_p,
                    **extra,
                )
                if result.get("canceled"):
                    details[bench_name] = result
                    if getattr(self.cancel_event, "timed_out", False):
                        timed_out = True
                        logger.warning(
                            "[%s] %s stopped by module time cap (%.0fs) at %d/%d items — "
                            "stopping suite", self.name, bench_name, self.max_seconds,
                            result.get("completed", 0), result.get("total", 0),
                        )
                    else:
                        logger.info(
                            "[%s] %s canceled mid-run (%d/%d items) — stopping suite",
                            self.name, bench_name,
                            result.get("completed", 0), result.get("total", 0),
                        )
                    break
                if result.get("skipped"):
                    logger.warning("[%s] %s skipped (no data)", self.name, bench_name)
                    continue
                scores[bench_name] = result["score"]
                details[bench_name] = result
                logger.info(
                    "[%s] <<< benchmark %d/%d: %s done  score=%.4f  took=%.0fs",
                    self.name, idx + 1, len(active), bench_name,
                    result["score"], time.monotonic() - bench_t0,
                )
            except Exception as exc:
                logger.error("[%s] %s raised: %s", self.name, bench_name, exc)
                details[bench_name] = {"error": str(exc)}

        regressions = self._check_regressions(scores)
        passed = len(regressions) == 0

        perf = _aggregate_perf(details)

        # Surface pass@k alongside the avg@k scores. `scores` holds avg@k (the
        # reported per-benchmark score); `pass_at_k` mirrors it for the pass@k
        # view. Both also live in details[bench] with their k values.
        pass_at_k = {
            n: d["pass_at_k"]
            for n, d in details.items()
            if isinstance(d, dict) and isinstance(d.get("pass_at_k"), (int, float))
        }

        metrics: Dict[str, Any] = {
            "scores": scores,
            "pass_at_k": pass_at_k,
            "details": details,
            "regressions": regressions,
            "elapsed_s": time.monotonic() - t0,
            "timed_out": timed_out,
            "perf": perf,
        }
        if perf:
            logger.info(
                "[%s] generation perf — ttft_mean=%s tpot_mean=%s service_tps=%s gen_tokens=%s",
                self.name,
                _fmt_ms(perf.get("ttft_mean_ms")), _fmt_ms(perf.get("tpot_mean_ms")),
                ("%.0f" % perf["service_tps"]) if perf.get("service_tps") is not None else "n/a",
                perf.get("gen_tokens", 0),
            )

        if not passed:
            logger.warning("[%s] FAILED — regressions: %s", self.name, regressions)
        else:
            logger.info("[%s] PASSED — scores: %s", self.name, scores)

        # Save per-benchmark results to output dir
        out_path = Path(self.output_dir) / "academic_benchmark_results.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(metrics, indent=2))

        return TestResult(name=self.name, passed=passed, metrics=metrics)

    def _load_baseline(self) -> Dict[str, float]:
        if not self.baseline_file:
            return {}
        try:
            return json.loads(Path(self.baseline_file).read_text())
        except Exception as exc:
            logger.warning("[%s] Could not load baseline %s: %s", self.name, self.baseline_file, exc)
            return {}

    def _check_regressions(self, scores: Dict[str, float]) -> Dict[str, Dict]:
        regressions: Dict[str, Dict] = {}
        for bench, baseline_score in self.baseline.items():
            current = scores.get(bench)
            if current is None:
                logger.warning("[%s] Benchmark %r missing from results", self.name, bench)
                continue
            drop = baseline_score - current
            if drop > self.max_regression:
                regressions[bench] = {
                    "baseline": baseline_score,
                    "current": current,
                    "drop": drop,
                }
        return regressions
