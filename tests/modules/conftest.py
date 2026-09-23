"""
Pytest fixtures for module tests.

Starts mock_server.py in a subprocess on a free port before the test session
and tears it down after. Tests use the `mock_endpoint` fixture.
"""
from __future__ import annotations

import subprocess
import sys
import time
import socket
import os

import pytest


MOCK_PORT = 18765
MOCK_MODEL = "mock"
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}"
# Low latency + short output for fast CI
MOCK_TTFT_MS = 50
MOCK_TPOT_MS = 5
MOCK_OUTPUT_TOKENS = 64


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session", autouse=True)
def mock_server():
    """Start mock_server.py once for the whole test session."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    proc = subprocess.Popen(
        [
            sys.executable, os.path.join(repo_root, "mock_server.py"),
            "--port", str(MOCK_PORT),
            "--ttft-ms", str(MOCK_TTFT_MS),
            "--tpot-ms", str(MOCK_TPOT_MS),
            "--output-tokens", str(MOCK_OUTPUT_TOKENS),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for server to become ready (up to 10s)
    for _ in range(50):
        if _port_open(MOCK_PORT):
            break
        time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail(f"mock_server did not start on port {MOCK_PORT}")

    yield proc

    proc.terminate()
    proc.wait()


@pytest.fixture(scope="session")
def mock_endpoint():
    from bench.modules.base import EndpointConfig
    return EndpointConfig(
        api_url=MOCK_URL,
        model=MOCK_MODEL,
        api_key="",
    )
