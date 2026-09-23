"""What guidellm measures, asserted against a server whose timing we control.

The rest of the suite checks that the modules run and that metrics come out
under the right names. This file checks that the NUMBERS are right, which is
the thing a guidellm upgrade can break without failing anything else.

The mock is configured with a known time to first token, a known per-token
delay, and a known number of reasoning tokens streamed before the answer.
Every assertion below compares a measurement with what the mock was told to do.

The case that matters most is reasoning. A reasoning model streams its chain of
thought in a separate field (`reasoning_content`, or `reasoning` in vLLM's GLM
parser) before any answer text. The first token is the first REASONING chunk:
that is when the user sees the model respond. A client that only watches the
answer field reports TTFT = real TTFT + the whole reasoning phase, which on a
long chain of thought is off by seconds. guidellm got this wrong for a while;
LLMBench carried a fork to fix it, and upstream has since fixed it too. These
tests are how the next upgrade proves it has not come back.

Tolerances are loose on purpose. Every request pays scheduling overhead on top
of what the mock sleeps, so the measurement always runs a little high; what the
tests guard is the difference between ~TTFT and ~TTFT + reasoning, which here is
a factor of two and cannot be mistaken for noise.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from bench.modules import get_module
from bench.modules.base import EndpointConfig

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TTFT_MS = 150
TPOT_MS = 5
REASONING_TOKENS = 40
OUTPUT_TOKENS = 64
# What a client that ignored reasoning chunks would report as TTFT.
NAIVE_TTFT_MS = TTFT_MS + REASONING_TOKENS * TPOT_MS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_mock(*extra: str) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_REPO_ROOT, "mock_server.py"),
         "--port", str(port), "--ttft-ms", str(TTFT_MS), "--tpot-ms", str(TPOT_MS),
         "--output-tokens", str(OUTPUT_TOKENS), *extra],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, port
        time.sleep(0.2)
    proc.terminate()
    pytest.fail("mock_server did not start")


def _run(port: int, tmp_path) -> dict:
    cls = get_module("perf_guidellm")
    params = cls.ParamsSchema.model_construct(
        backend="guidellm", concurrency=2, input_tokens=64, output_tokens=OUTPUT_TOKENS,
        max_seconds=10.0, request_timeout=30.0, warmup_seconds=0.0,
        dataset_path=None, processor_path="",
    )
    endpoint = EndpointConfig(api_url=f"http://127.0.0.1:{port}", model="mock", api_key="")
    result = cls().run(endpoint, params, str(tmp_path))
    assert result.error is None, result.error
    return result.metrics


@pytest.fixture(params=[
    pytest.param(("--reasoning-tokens", str(REASONING_TOKENS),
                  "--reasoning-field", "reasoning_content"), id="reasoning_content"),
    pytest.param(("--reasoning-tokens", str(REASONING_TOKENS),
                  "--reasoning-field", "reasoning"), id="reasoning"),
])
def reasoning_mock(request):
    proc, port = _start_mock(*request.param)
    yield port
    proc.terminate()
    proc.wait()


@pytest.fixture
def plain_mock():
    proc, port = _start_mock()
    yield port
    proc.terminate()
    proc.wait()


@pytest.mark.integration
def test_ttft_is_the_first_reasoning_token_not_the_first_answer_token(reasoning_mock, tmp_path):
    m = _run(reasoning_mock, tmp_path)
    assert TTFT_MS * 0.9 <= m["ttft_p50_ms"] < (TTFT_MS + NAIVE_TTFT_MS) / 2, (
        f"ttft_p50={m['ttft_p50_ms']:.0f}ms: expected ~{TTFT_MS}ms. Near "
        f"{NAIVE_TTFT_MS}ms means reasoning chunks are not being counted as tokens."
    )


@pytest.mark.integration
def test_ttft_matches_the_server_without_reasoning(plain_mock, tmp_path):
    m = _run(plain_mock, tmp_path)
    assert TTFT_MS * 0.9 <= m["ttft_p50_ms"] < TTFT_MS * 1.6


@pytest.mark.integration
def test_inter_token_latency_matches_the_server(reasoning_mock, tmp_path):
    m = _run(reasoning_mock, tmp_path)
    assert TPOT_MS * 0.8 <= m["itl_p50_ms"] < TPOT_MS * 3


@pytest.mark.integration
def test_every_request_is_counted_and_succeeds(plain_mock, tmp_path):
    m = _run(plain_mock, tmp_path)
    assert m["uptime"] == 1.0
    assert m["measured_requests"] > 0


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "darwin", reason=(
    "guidellm spawns its workers on macOS, where the httpx status counter is "
    "not inherited; every deployment runs on Linux, which forks"))
def test_http_status_counts_cover_every_measured_request(plain_mock, tmp_path):
    m = _run(plain_mock, tmp_path)
    # Requests still in flight at the edges of the window get an HTTP 200 but are
    # not measured, so the status count may exceed the measured count — never
    # fall short of it.
    assert m["http_status_200"] >= m["measured_requests"]
