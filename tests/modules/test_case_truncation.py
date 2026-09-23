"""Unit test for case_truncation module against mock_server.

Uses a low output_tokens value so the mock responds quickly.
The mock always returns finish_reason='stop', so no truncation is expected.
"""
import pytest
from bench.modules import get_module
from bench.modules.base import ModuleResult


@pytest.mark.integration
def test_case_truncation_passes_on_mock(mock_endpoint, tmp_path):
    cls = get_module("case_truncation")
    params = cls.ParamsSchema.model_construct(
        concurrency=2,
        output_tokens=64,      # small value for fast CI — bypasses ge=1024 validator
        completion_ratio=0.0,  # 0% threshold — any output passes
        request_timeout=30.0,
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    assert isinstance(result, ModuleResult)
    assert result.error is None, f"Module returned error: {result.error}"
    assert 0.0 <= result.score <= 1.0
    assert "truncation_rate" in result.metrics
    assert "total_requests" in result.metrics
