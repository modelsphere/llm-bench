"""
Correctness under stress: Long Output / Truncation Detection

Purpose : Verify the service can generate up to 64k tokens without truncation.
Method  : Send `concurrency` streaming requests concurrently (input=50 tokens,
          max_tokens=OUTPUT_TOKENS).  Each response is checked for:
            1. finish_reason — must be "stop" (not "length")
            2. output token count — must be >= OUTPUT_TOKENS * COMPLETION_RATIO
            3. EOS token presence — checked via the finish_reason and usage fields
          A request is flagged as "truncated" if finish_reason != "stop" OR
          output tokens fall below the threshold.

Pass condition: truncation_rate <= TRUNCATION_RATE_THRESHOLD (default 0.0, i.e. none)
"""
from __future__ import annotations

import json
import os
import threading
import time
from traceback import format_exc
from typing import Dict, List, Optional

import requests

from bench.result import TestResult
from bench.tests.base import BaseTest
from utils.api import join_endpoint
from utils.logger import logger

# Module-level fallback defaults, read from env ONCE at import. Used as the
# kwargs defaults on LongOutputTest.__init__ so CLI / short-lived processes
# behave like before. Long-lived workers MUST pass these as constructor args
# — see ReplayTest's docstring for the env-capture trap these defaults exist
# to dodge.
OUTPUT_TOKENS = int(os.getenv("LONG_OUTPUT_TEST_MAX_TOKENS", "65536"))
CONCURRENCY = int(os.getenv("LONG_OUTPUT_TEST_CONCURRENCY", "4"))
COMPLETION_RATIO = float(os.getenv("LONG_OUTPUT_TEST_COMPLETION_RATIO", "0.95"))
# Fraction of requests allowed to be truncated before the test fails
TRUNCATION_RATE_THRESHOLD = float(os.getenv("LONG_OUTPUT_TRUNCATION_THRESHOLD", "0.0"))
REQUEST_TIMEOUT = float(os.getenv("LONG_OUTPUT_TEST_TIMEOUT", "900.0"))

# Short prompt (~50 tokens) that instructs the model to generate maximum output
_LONG_PROMPT = (
    "Please write an extremely long, detailed, comprehensive essay on any topic you choose. "
    "Do not stop until you have produced the maximum possible output length."
)


class LongOutputTest(BaseTest):
    """
    Sends CONCURRENCY streaming requests in parallel and checks each one
    for truncation.  Uses direct HTTP (not GuideLLM) so we can inspect finish_reason
    and per-token streaming events.
    """

    name = "long_output_truncation"

    def __init__(
        self,
        api_url: str,
        model: str,
        api_key: str,
        output_dir: str,
        concurrency: int = CONCURRENCY,
        output_tokens: int = OUTPUT_TOKENS,
        cancel_event=None,
        completion_ratio: float = COMPLETION_RATIO,
        truncation_rate_threshold: float = TRUNCATION_RATE_THRESHOLD,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        super().__init__(api_url, model, api_key, output_dir)
        self.concurrency = concurrency
        self.output_tokens = output_tokens
        self.cancel_event = cancel_event
        self.completion_ratio = completion_ratio
        self.truncation_rate_threshold = truncation_rate_threshold
        self.request_timeout = request_timeout

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> TestResult:
        # One distinct dict per slot. `[{}] * n` would alias a single dict across
        # all slots; if a worker thread died before its `results[idx] = ...`
        # assignment, the leftover shared dict would silently undercount
        # truncations/errors and let the test pass when it shouldn't.
        results: List[Dict] = [{} for _ in range(self.concurrency)]
        threads = [
            threading.Thread(
                target=self._run_one,
                args=(i, results),
                daemon=True,
            )
            for i in range(self.concurrency)
        ]
        logger.info(
            "[%s] Starting %d concurrent requests (max_tokens=%d)",
            self.name, self.concurrency, self.output_tokens,
        )
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                if self.cancel_event is not None and self.cancel_event.is_set():
                    break
                t.join(timeout=0.5)
            if self.cancel_event is not None and self.cancel_event.is_set():
                for remaining in threads:
                    if remaining.is_alive():
                        remaining.join(timeout=1.0)
                break
        elapsed = time.monotonic() - t0

        # Aggregate
        total = len(results)
        errors = sum(1 for r in results if r.get("error"))
        truncated = sum(1 for r in results if r.get("truncated"))
        passed_count = sum(1 for r in results if r.get("passed"))

        truncation_rate = truncated / total if total > 0 else 1.0
        passed = truncation_rate <= self.truncation_rate_threshold and errors == 0

        output_token_counts = [r["output_tokens"] for r in results if "output_tokens" in r]
        avg_output_tokens = sum(output_token_counts) / len(output_token_counts) if output_token_counts else 0.0

        finish_reasons = [r.get("finish_reason") for r in results if r.get("finish_reason")]

        metrics = {
            "total_requests": total,
            "truncated_requests": truncated,
            "error_requests": errors,
            "passed_requests": passed_count,
            "truncation_rate": truncation_rate,
            "avg_output_tokens": avg_output_tokens,
            "elapsed_s": elapsed,
            "finish_reason_counts": {
                reason: finish_reasons.count(reason) for reason in set(finish_reasons)
            },
        }

        if not passed:
            logger.warning(
                "[%s] FAILED — truncation_rate=%.1f%% (%d/%d), errors=%d",
                self.name, truncation_rate * 100, truncated, total, errors,
            )
        else:
            logger.info(
                "[%s] PASSED — avg_output_tokens=%.0f, elapsed=%.1fs",
                self.name, avg_output_tokens, elapsed,
            )

        return TestResult(name=self.name, passed=passed, metrics=metrics)

    # ------------------------------------------------------------------
    # Per-request worker
    # ------------------------------------------------------------------

    def _run_one(self, idx: int, results: List[Dict]) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            results[idx] = {"idx": idx, "error": "Canceled", "truncated": True, "passed": False}
            return

        result: Dict = {}
        url = join_endpoint(self.api_url, "chat/completions")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # min_tokens + ignore_eos force the backend to generate up to max_tokens
        # without stopping on EOS. These are non-standard but widely supported
        # (vLLM, SGLang, etc.) — testing whether the backend accepts them is part
        # of the point of this test.
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": _LONG_PROMPT}],
            "max_tokens": self.output_tokens,
            "min_tokens": self.output_tokens,
            "ignore_eos": True,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        try:
            finish_reason: Optional[str] = None
            output_tokens_usage: int = 0   # from usage chunk (authoritative if present)
            content_chunks: int = 0        # count of non-empty content chunks (fallback)

            with requests.Session() as session:
                with session.post(
                    url, json=payload, headers=headers,
                    stream=True, timeout=self.request_timeout,
                ) as resp:
                    resp.raise_for_status()
                    for raw_line in resp.iter_lines():
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            break
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            chunk = json.loads(body)
                        except json.JSONDecodeError:
                            continue

                        # Usage chunk (last chunk on many servers — authoritative token count)
                        usage = chunk.get("usage")
                        if usage and usage.get("completion_tokens"):
                            output_tokens_usage = usage["completion_tokens"]

                        for choice in chunk.get("choices", []):
                            fr = choice.get("finish_reason")
                            if fr:
                                finish_reason = fr
                            # Count non-empty content chunks as a token-count proxy
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                content_chunks += 1

            # Use usage count if available, otherwise fall back to chunk count.
            # Chunk count underestimates real tokens (multi-token chunks on some
            # servers) but is always better than 0.
            output_tokens = output_tokens_usage if output_tokens_usage > 0 else content_chunks

            # Truncation = output fell short of target.
            # With min_tokens + ignore_eos the backend must keep generating until
            # max_tokens; any shortfall means the backend truncated or doesn't
            # support these params. finish_reason is NOT used here: "length" is the
            # expected normal outcome (hit max_tokens cap = success), and "stop"
            # with full token count is also fine.
            threshold = int(self.output_tokens * self.completion_ratio)
            truncated = output_tokens < threshold
            if truncated:
                logger.debug(
                    "[%s] request %d: output_tokens=%d < threshold=%d (finish_reason=%r) — truncated",
                    self.name, idx, output_tokens, threshold, finish_reason,
                )

            result = {
                "idx": idx,
                "finish_reason": finish_reason,
                "output_tokens": output_tokens,
                "truncated": truncated,
                "passed": not truncated,
            }

        except Exception:
            err = format_exc()
            logger.warning("[%s] request %d error: %s", self.name, idx, err)
            result = {"idx": idx, "error": err, "truncated": True, "passed": False}

        results[idx] = result
