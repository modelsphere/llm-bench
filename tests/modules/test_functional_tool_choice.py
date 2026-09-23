"""Unit tests for the functional-acceptance tool_choice_mode toggle.

No network: the client's chat() is monkeypatched to capture the request body
and return a canned tool-call response, so we can assert exactly which
tool_choice shape each mode sends and that both modes accept a valid call.
"""
from __future__ import annotations

import pytest

from bench.modules.functional_acceptance import FunctionalAcceptanceParams
from bench.tests.functional.functional_acceptance import FunctionalAcceptanceTest


def _make_test(**kwargs) -> FunctionalAcceptanceTest:
    return FunctionalAcceptanceTest(
        api_url="http://unit.test:1", model="unit-model", api_key="",
        backend_style="direct", **kwargs,
    )


def _tool_call_response():
    return 200, {
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Beijing"}'},
            }]},
            "finish_reason": "tool_calls",
        }],
    }, 0.01


def _run_check(test: FunctionalAcceptanceTest, response):
    captured = {}

    def fake_chat(body, stream=False):
        captured.update(body)
        return response

    test.c.chat = fake_chat  # type: ignore[method-assign]
    test._tool_call_check("d03_tool_call")
    (result,) = test.results
    return captured, result


def test_auto_mode_is_default_and_sends_auto():
    test = _make_test()  # default tool_choice_mode="auto"
    captured, result = _run_check(test, _tool_call_response())
    assert captured["tool_choice"] == "auto"
    assert result["status"] == "PASS"
    assert "tool_choice=auto" in result["detail"]


def test_named_mode_forces_function():
    test = _make_test(tool_choice_mode="named")
    captured, result = _run_check(test, _tool_call_response())
    assert captured["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    assert result["status"] == "PASS"
    assert "tool_choice=named" in result["detail"]


def test_empty_tool_calls_fails_in_both_modes():
    # The vLLM 0.21 bug shape: 200, finish=tool_calls, but tool_calls=[].
    swallowed = 200, {
        "choices": [{
            "message": {"role": "assistant", "content": "", "tool_calls": []},
            "finish_reason": "tool_calls",
        }],
    }, 0.01
    for mode in ("named", "auto"):
        test = _make_test(tool_choice_mode=mode)
        _, result = _run_check(test, swallowed)
        assert result["status"] == "FAIL", mode
        assert "tools=0" in result["detail"]


def test_module_param_default_and_values():
    assert FunctionalAcceptanceParams().tool_choice_mode == "auto"
    assert FunctionalAcceptanceParams(tool_choice_mode="named").tool_choice_mode == "named"
    with pytest.raises(Exception):
        FunctionalAcceptanceParams(tool_choice_mode="required")
