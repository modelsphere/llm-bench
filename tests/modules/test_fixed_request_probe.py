"""Unit tests for the fixed-request probe `clean` toggle (system-message
normalization shared with the replay path). Network-free."""
from __future__ import annotations


from bench.modules import get_module
from bench.tests.functional.fixed_request_probe import build_payload


def _req():
    return {
        "model": "kimi",
        "stream": True,
        "messages": [
            {"role": "system", "content": "meta"},
            {"role": "system", "content": "ident"},
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hi"},
        ],
    }


def _nsys(payload):
    return sum(1 for m in payload["messages"] if m.get("role") == "system")


def test_clean_off_is_byte_for_byte():
    req = _req()
    out = build_payload(req, "qwen", 64)              # clean defaults off
    assert _nsys(out) == 3                            # all system blocks preserved
    assert out["model"] == "qwen" and out["max_tokens"] == 64


def test_clean_on_collapses_system_messages():
    out = build_payload(_req(), "qwen", 64, clean=True)
    assert _nsys(out) == 1
    assert out["messages"][0] == {"role": "system", "content": "meta\n\nident\n\nrules"}


def test_clean_does_not_mutate_input_request():
    req = _req()
    build_payload(req, "qwen", 64, clean=True)
    assert sum(1 for m in req["messages"] if m["role"] == "system") == 3


def test_clean_noop_keeps_single_system_request():
    req = {"model": "k", "stream": False,
           "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}
    assert build_payload(req, "q", 8, clean=True) == build_payload(req, "q", 8, clean=False)


def test_tool_call_and_hallucination_expose_clean_default_off():
    for name in ("tool_call_success", "hallucination"):
        cls = get_module(name)
        params = cls.ParamsSchema(dataset_path="")
        assert params.clean is False, name
        assert "clean" in cls.descriptor()["params_schema"]["properties"], name
