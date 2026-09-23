"""
Unit test for opencompass module.

The opencompass module requires dataset files to actually run benchmarks.
This test only verifies that:
  1. The module loads without error and params validate.
  2. When dataset_dir is missing, the module returns a result (not a crash)
     with an error or skipped metrics.
"""
import pytest
from bench.modules import get_module
from bench.modules.base import ModuleResult


def test_opencompass_descriptor():
    cls = get_module("opencompass")
    desc = cls.descriptor()
    assert desc["name"] == "opencompass"
    assert "selected_benchmarks" in desc["params_schema"]["properties"]


def test_opencompass_default_params_valid():
    cls = get_module("opencompass")
    params = cls.ParamsSchema()
    # Defaults to the full benchmark suite.
    assert len(params.selected_benchmarks) == 8
    assert "simpleqa" in params.selected_benchmarks
    assert "longbench_v2" in params.selected_benchmarks


def test_opencompass_subset_params_valid():
    cls = get_module("opencompass")
    params = cls.ParamsSchema(selected_benchmarks=["aime2025", "mmlu_pro"])
    assert params.selected_benchmarks == ["aime2025", "mmlu_pro"]


@pytest.mark.integration
def test_opencompass_missing_dataset_returns_result(mock_endpoint, tmp_path):
    """When dataset_dir doesn't exist, module should not crash — it returns errors per benchmark."""
    cls = get_module("opencompass")
    params = cls.ParamsSchema(
        dataset_dir=str(tmp_path / "nonexistent"),
        selected_benchmarks=["aime2025"],
        sample_cap_aime=2,
    )
    result = cls().run(mock_endpoint, params, str(tmp_path))

    # Should return a ModuleResult (not raise)
    assert isinstance(result, ModuleResult)
    # Score should be 0-1 even on failure
    assert 0.0 <= result.score <= 1.0
