"""
§6.1 OpenAI-Format Endpoint Tests

Verifies that all required API endpoints are reachable and respond correctly.
Each endpoint is one scenario; all must pass for the test to pass.
"""
from __future__ import annotations

from typing import Tuple

import requests

from bench.tests.functional.base import BaseFunctionalTest
from utils.api import join_endpoint, to_base_url
from utils.logger import logger


class EndpointTest(BaseFunctionalTest):
    """
    §6.1 — checks that the four required endpoints exist and return the
    expected HTTP status codes.
    """

    name = "endpoints"
    PASS_THRESHOLD = 1.0    # All endpoints must work

    def _run_scenarios(self) -> list[Tuple[str, bool]]:
        return [
            self._check_chat_completions(),
            self._check_completions(),
            self._check_models(),
            self._check_health(),
        ]

    # ------------------------------------------------------------------

    def _check_chat_completions(self) -> Tuple[str, bool]:
        """POST /v1/chat/completions with a minimal message."""
        name = "chat_completions"
        resp = self._chat(
            messages=[{"role": "user", "content": "Say 'ok' in one word."}],
            max_tokens=10,
        )
        if resp is None:
            return name, False
        # Must have choices array with at least one message
        passed = bool(resp.get("choices") and resp["choices"][0].get("message"))
        logger.debug("[endpoints] %s: passed=%s", name, passed)
        return name, passed

    def _check_completions(self) -> Tuple[str, bool]:
        """POST /v1/completions (legacy text completions)."""
        name = "completions"
        url = join_endpoint(self.api_url, "completions")
        payload = {
            "model": self.model,
            "prompt": "Hello",
            "max_tokens": 10,
        }
        try:
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            passed = bool(data.get("choices"))
            logger.debug("[endpoints] %s: passed=%s", name, passed)
            return name, passed
        except Exception as exc:
            logger.debug("[endpoints] %s error: %s", name, exc)
            return name, False

    def _check_models(self) -> Tuple[str, bool]:
        """GET /v1/models — must return a list of models."""
        name = "models"
        url = join_endpoint(self.api_url, "models")
        try:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            passed = bool(data.get("data"))
            logger.debug("[endpoints] %s: passed=%s", name, passed)
            return name, passed
        except Exception as exc:
            logger.debug("[endpoints] %s error: %s", name, exc)
            return name, False

    def _check_health(self) -> Tuple[str, bool]:
        """GET /health — must return HTTP 200."""
        name = "health"
        url = to_base_url(self.api_url) + "/health"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            passed = resp.status_code == 200
            logger.debug("[endpoints] %s: status=%d passed=%s", name, resp.status_code, passed)
            return name, passed
        except Exception as exc:
            logger.debug("[endpoints] %s error: %s", name, exc)
            return name, False
