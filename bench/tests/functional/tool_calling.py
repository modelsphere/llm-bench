"""
§6.2 Tool Calling Tests

Verifies that the service returns correctly-formatted `tool_calls` in responses.
Does NOT invoke external services — only validates the response JSON structure.

5 scenarios (min required by spec):
  1. weather_query      — city parameter
  2. calculator         — math expression
  3. knowledge_query    — entity information
  4. multi_turn         — sequential tool use across turns
  5. tool_selection     — model must choose the right tool from several options

Pass threshold: ≥95% of scenarios.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from bench.tests.functional.base import BaseFunctionalTest
from utils.logger import logger


# ---------------------------------------------------------------------------
# Tool definitions reused across scenarios
# ---------------------------------------------------------------------------

_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
}

_CALCULATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "Evaluate a mathematical expression",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Math expression to evaluate"},
            },
            "required": ["expression"],
        },
    },
}

_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "Search for information about an entity or topic",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
}


def _describe_failure(response: Optional[Dict[str, Any]], expected_function: Optional[str]) -> str:
    """Return a human-readable reason why _has_tool_call would return False."""
    if response is None:
        return "response is None (API call failed or returned no data)"
    choices = response.get("choices", [])
    if not choices:
        return f"no 'choices' in response: {list(response.keys())}"
    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls", [])
    if not tool_calls:
        content = message.get("content", "")
        finish_reason = choices[0].get("finish_reason", "?")
        return (
            f"no tool_calls in message (finish_reason={finish_reason!r}); "
            f"content preview: {str(content)[:200]!r}"
        )
    # tool_calls present — check structure
    for i, tc in enumerate(tool_calls):
        if not (tc.get("id") and tc.get("type") == "function"):
            return f"tool_calls[{i}] missing id or type!=function: {tc}"
        fn = tc.get("function", {})
        if not fn.get("name") or fn.get("arguments") is None:
            return f"tool_calls[{i}].function missing name or arguments: {fn}"
        try:
            json.loads(fn["arguments"])
        except (json.JSONDecodeError, TypeError) as exc:
            return f"tool_calls[{i}].function.arguments not valid JSON: {exc} — raw: {fn['arguments']!r}"
        if expected_function and fn["name"] != expected_function:
            return f"called {fn['name']!r} but expected {expected_function!r}"
    return "unknown failure"


def _has_tool_call(response: Optional[Dict[str, Any]], expected_function: Optional[str] = None) -> bool:
    """Return True if the response contains a well-formed tool_calls list."""
    if response is None:
        return False
    choices = response.get("choices", [])
    if not choices:
        return False
    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls", [])
    if not tool_calls:
        return False
    # Each tool call must have id, type, and function with name + arguments
    for tc in tool_calls:
        if not (tc.get("id") and tc.get("type") == "function"):
            return False
        fn = tc.get("function", {})
        if not fn.get("name") or fn.get("arguments") is None:
            return False
        # arguments must be valid JSON string
        try:
            json.loads(fn["arguments"])
        except (json.JSONDecodeError, TypeError):
            return False
        # Optionally check the function name
        if expected_function and fn["name"] != expected_function:
            return False
    return True


class ToolCallingTest(BaseFunctionalTest):
    """§6.2 — validates tool_calls format across 5 scenarios."""

    name = "tool_calling"
    PASS_THRESHOLD = 0.95

    def _run_scenarios(self) -> list[Tuple[str, bool]]:
        logger.info("Starting ToolCallingTest...")
        return [
            self._scenario_weather_query(),
            self._scenario_calculator(),
            self._scenario_knowledge_query(),
            self._scenario_multi_turn(),
            self._scenario_tool_selection(),
        ]

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def _scenario_weather_query(self) -> Tuple[str, bool]:
        """天气查询 — model should call get_weather with a city argument."""
        name = "weather_query"
        resp = self._chat(
            messages=[{"role": "user", "content": "What's the weather like in Shanghai today?"}],
            tools=[_WEATHER_TOOL],
        )
        passed = _has_tool_call(resp, expected_function="get_weather")
        if not passed:
            logger.warning("[tool_calling] %s FAILED: %s", name, _describe_failure(resp, "get_weather"))
        return name, passed

    def _scenario_calculator(self) -> Tuple[str, bool]:
        """计算器 — model should call calculate with an expression."""
        name = "calculator"
        resp = self._chat(
            messages=[{"role": "user", "content": "What is 1337 * 42?"}],
            tools=[_CALCULATOR_TOOL],
        )
        passed = _has_tool_call(resp, expected_function="calculate")
        if not passed:
            logger.warning("[tool_calling] %s FAILED: %s", name, _describe_failure(resp, "calculate"))
        return name, passed

    def _scenario_knowledge_query(self) -> Tuple[str, bool]:
        """知识查询 — model should call search_knowledge.
        Uses a system prompt to prevent the model from answering directly from memory.
        """
        name = "knowledge_query"
        resp = self._chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an assistant that retrieves information exclusively through tools. "
                        "Never answer from your own knowledge — always call search_knowledge."
                    ),
                },
                {"role": "user", "content": "Tell me about the Eiffel Tower."},
            ],
            tools=[_SEARCH_TOOL],
        )
        passed = _has_tool_call(resp, expected_function="search_knowledge")
        if not passed:
            logger.warning("[tool_calling] %s FAILED: %s", name, _describe_failure(resp, "search_knowledge"))
        return name, passed

    def _scenario_multi_turn(self) -> Tuple[str, bool]:
        """多轮对话 — model calls a tool, receives result, then calls another."""
        name = "multi_turn"
        # Turn 1: ask for weather
        resp1 = self._chat(
            messages=[{"role": "user", "content": "What's the weather in Beijing?"}],
            tools=[_WEATHER_TOOL, _CALCULATOR_TOOL],
        )
        if not _has_tool_call(resp1):
            logger.warning("[tool_calling] %s FAILED (turn 1): %s", name, _describe_failure(resp1, None))
            return name, False

        # Simulate tool response and ask a follow-up that triggers another call
        tool_call_id = resp1["choices"][0]["message"]["tool_calls"][0]["id"]
        messages = [
            {"role": "user", "content": "What's the weather in Beijing?"},
            resp1["choices"][0]["message"],
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": '{"temperature": 15, "condition": "sunny"}',
            },
            {"role": "user", "content": "Now calculate 15 * 1.8 + 32 to convert to Fahrenheit."},
        ]
        resp2 = self._chat(messages=messages, tools=[_WEATHER_TOOL, _CALCULATOR_TOOL])
        passed = _has_tool_call(resp2)
        if not passed:
            logger.warning("[tool_calling] %s FAILED (turn 2): %s", name, _describe_failure(resp2, None))
        return name, passed

    def _scenario_tool_selection(self) -> Tuple[str, bool]:
        """工具选择 — model must choose the right tool among multiple options."""
        name = "tool_selection"
        # Provide both tools; the question is clearly about calculation (not weather or search).
        # Use a non-trivial expression so the model doesn't answer directly without a tool call.
        resp = self._chat(
            messages=[{"role": "user", "content": "Calculate 1337 * 42 + 99 / 3."}],
            tools=[_WEATHER_TOOL, _CALCULATOR_TOOL, _SEARCH_TOOL],
        )
        # Must call calculate, not weather or search
        passed = _has_tool_call(resp, expected_function="calculate")
        if not passed:
            logger.warning("[tool_calling] %s FAILED: %s", name, _describe_failure(resp, "calculate"))
        return name, passed
