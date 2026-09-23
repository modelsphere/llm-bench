"""
§6.4 Chat Generation Quality Tests

Soft quality check — ensures generation quality has not degraded noticeably.
No strict threshold or LLM-as-judge for now; checks are keyword/length based.

Evaluation: responses must be non-empty and contain expected keywords or
            satisfy basic quality heuristics (min length, no refusal phrases).
"""
from __future__ import annotations

from typing import List, Tuple

from bench.tests.functional.base import BaseFunctionalTest
from utils.logger import logger

# Phrases that indicate a refusal or generation failure
_REFUSAL_PHRASES = [
    "i cannot", "i can't", "i am unable", "i'm unable",
    "as an ai", "i don't have", "i do not have",
]


def _extract_content(resp: dict | None) -> str:
    """Extract message content from a chat completion response.

    Falls back to reasoning_content for thinking models (e.g. Kimi-K2.5)
    that return content=null with the answer in reasoning_content.
    """
    msg = (resp or {}).get("choices", [{}])[0].get("message", {})
    content = msg.get("content")
    if content:
        return content
    reasoning = msg.get("reasoning_content")
    if reasoning:
        logger.warning(
            "[chat_quality] content is empty — falling back to reasoning_content. "
            "The model may be a thinking model that does not populate the content field."
        )
        return reasoning
    return ""


def _basic_quality_check(
    content: str,
    min_words: int = 10,
    expected_keywords: List[str] = [],
) -> bool:
    """Return True if the response passes basic quality heuristics."""
    if not content or not content.strip():
        return False
    words = content.strip().split()
    if len(words) < min_words:
        return False
    lowered = content.lower()
    if any(phrase in lowered for phrase in _REFUSAL_PHRASES):
        return False
    if expected_keywords:
        return any(kw.lower() in lowered for kw in expected_keywords)
    return True


class ChatQualityTest(BaseFunctionalTest):
    """
    §6.4 — spot-check chat generation quality across diverse prompts.
    No strict pass threshold yet; PASS_THRESHOLD is set loosely.
    """

    name = "chat_quality"
    PASS_THRESHOLD = 0.80   # lenient — spec says "no strict check for now"

    def _run_scenarios(self) -> list[Tuple[str, bool]]:
        return [
            self._scenario_factual_question(),
            self._scenario_creative_writing(),
            self._scenario_code_generation(),
            self._scenario_summarization(),
            self._scenario_multilingual(),
            self._scenario_reasoning(),
        ]

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def _log_io(self, name: str, messages: list, resp: dict | None, content: str, passed: bool) -> None:
        prompt = messages[-1]["content"]
        if resp is None:
            out_repr = "<no response / HTTP error>"
        elif not content:
            # Show full raw response to diagnose thinking-model / unexpected structure
            import json as _json
            out_repr = "<empty content> raw=" + _json.dumps(resp)[:500]
        else:
            out_repr = repr(content[:300])
        logger.info(
            "[chat_quality] %s | passed=%s\n  IN : %s\n  OUT: %s",
            name, passed, prompt[:200], out_repr,
        )

    def _scenario_factual_question(self) -> Tuple[str, bool]:
        name = "factual_question"
        messages = [{"role": "user", "content": "What is the capital of France?"}]
        resp = self._chat(messages=messages, max_tokens=256)
        content = _extract_content(resp)
        passed = _basic_quality_check(content, min_words=2, expected_keywords=["Paris"])
        self._log_io(name, messages, resp, content, passed)
        return name, passed

    def _scenario_creative_writing(self) -> Tuple[str, bool]:
        name = "creative_writing"
        messages = [{
            "role": "user",
            "content": "Write a two-sentence story about a robot learning to paint.",
        }]
        resp = self._chat(messages=messages, max_tokens=256)
        content = _extract_content(resp)
        passed = _basic_quality_check(content, min_words=15)
        self._log_io(name, messages, resp, content, passed)
        return name, passed

    def _scenario_code_generation(self) -> Tuple[str, bool]:
        name = "code_generation"
        messages = [{
            "role": "user",
            "content": "Write a Python function that returns the nth Fibonacci number.",
        }]
        resp = self._chat(messages=messages, max_tokens=256)
        content = _extract_content(resp)
        passed = _basic_quality_check(
            content, min_words=10, expected_keywords=["def", "fibonacci", "fib", "return"]
        )
        self._log_io(name, messages, resp, content, passed)
        return name, passed

    def _scenario_summarization(self) -> Tuple[str, bool]:
        name = "summarization"
        article = (
            "The James Webb Space Telescope (JWST) was launched on December 25, 2021. "
            "It is the largest and most powerful space telescope ever built. "
            "Scientists use it to observe distant galaxies, exoplanets, and the early universe. "
            "It operates at infrared wavelengths, allowing it to see through cosmic dust."
        )
        messages = [{"role": "user", "content": f"Summarize in one sentence:\n{article}"}]
        resp = self._chat(messages=messages, max_tokens=256)
        content = _extract_content(resp)
        passed = _basic_quality_check(
            content, min_words=8, expected_keywords=["Webb", "telescope", "JWST", "space"]
        )
        self._log_io(name, messages, resp, content, passed)
        return name, passed

    def _scenario_multilingual(self) -> Tuple[str, bool]:
        """Model should respond in Chinese when asked in Chinese."""
        name = "multilingual_chinese"
        messages = [{"role": "user", "content": "用中文说：你好世界"}]
        resp = self._chat(messages=messages, max_tokens=256)
        content = _extract_content(resp)
        has_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in content)
        passed = bool(content.strip()) and has_chinese
        self._log_io(name, messages, resp, content, passed)
        return name, passed

    def _scenario_reasoning(self) -> Tuple[str, bool]:
        name = "reasoning"
        messages = [{
            "role": "user",
            "content": (
                "If Alice is taller than Bob, and Bob is taller than Carol, "
                "who is the shortest? Answer in one word."
            ),
        }]
        resp = self._chat(messages=messages, max_tokens=256)
        content = _extract_content(resp)
        passed = _basic_quality_check(content, min_words=1, expected_keywords=["Carol"])
        self._log_io(name, messages, resp, content, passed)
        return name, passed
