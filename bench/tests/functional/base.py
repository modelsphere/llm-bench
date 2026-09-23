"""
Base class for functional (non-GuideLLM) tests.

Functional tests make direct HTTP requests to the API and return pass/fail
with a correctness rate.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, Optional, Tuple

import requests

from bench.result import TestResult
from bench.tests.base import BaseTest
from utils.api import join_endpoint
from utils.logger import logger


class BaseFunctionalTest(BaseTest):
    """
    Shared machinery for §6.x functional correctness tests.

    Subclasses implement `_run_scenarios()` which returns a list of
    (scenario_name, passed) tuples.  The base class computes the overall
    pass rate and decides whether the test passes.
    """

    #: Minimum fraction of scenarios that must pass for the test to pass.
    PASS_THRESHOLD: float = 0.95

    @abstractmethod
    def _run_scenarios(self) -> list[Tuple[str, bool]]:
        """
        Run all scenarios for this functional test.
        Returns [(scenario_name, passed), ...].
        """
        raise NotImplementedError

    def run(self) -> TestResult:
        from traceback import format_exc

        try:
            results = self._run_scenarios()
        except Exception:
            err = format_exc()
            logger.error("[%s] functional test crashed: %s", self.name, err)
            return TestResult(name=self.name, passed=False, metrics={}, error=err)

        passed_count = sum(1 for _, p in results if p)
        total = len(results)
        rate = passed_count / total if total > 0 else 0.0

        metrics = {
            "total_scenarios": total,
            "passed_scenarios": passed_count,
            "correctness_rate": rate,
            **{f"scenario_{name}": passed for name, passed in results},
        }
        passed = rate >= self.PASS_THRESHOLD
        if not passed:
            failed_names = [n for n, p in results if not p]
            logger.warning(
                "[%s] correctness rate %.1f%% < threshold %.1f%% — failed scenarios: %s",
                self.name, rate * 100, self.PASS_THRESHOLD * 100, failed_names,
            )
        else:
            logger.info("[%s] all scenarios passed (%.1f%%)", self.name, rate * 100)
        return TestResult(name=self.name, passed=passed, metrics=metrics)

    # ------------------------------------------------------------------
    # Helpers for making OpenAI-compatible API calls
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _chat(
        self,
        messages: list,
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[dict] = None,
        max_tokens: int = 512,
        timeout: float = 300,
    ) -> Optional[Dict[str, Any]]:
        """POST /v1/chat/completions and return the response dict, or None on error."""
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format

        url = join_endpoint(self.api_url, "chat/completions")
        try:
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.warning(
                "[%s] _chat HTTP error: %s — body: %s",
                self.name, exc, resp.text[:500],
            )
            return None
        except Exception as exc:
            logger.warning("[%s] _chat error: %s", self.name, exc)
            return None
