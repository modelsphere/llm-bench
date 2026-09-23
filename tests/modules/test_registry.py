"""Tests for module registry structure and descriptor contract."""
import pytest
from bench.modules import MODULE_REGISTRY, get_module, list_modules


def test_registry_has_all_modules():
    expected = {
        "perf_guidellm", "perf_guidellm_sweep",
        "case_truncation", "opencompass",
        "replay", "agentic", "hallucination", "tool_call_success",
        "functional_acceptance",
    }
    assert set(MODULE_REGISTRY.keys()) == expected


def test_get_module_known():
    cls = get_module("perf_guidellm")
    assert cls.name == "perf_guidellm"


def test_get_module_unknown():
    with pytest.raises(KeyError, match="Unknown module"):
        get_module("does_not_exist")


def test_descriptors_have_required_fields():
    for desc in list_modules():
        assert "name" in desc
        assert "display_name" in desc
        assert "description" in desc
        assert "params_schema" in desc
        assert "default_params" in desc
        assert "supports_concurrency_override" in desc


def test_concurrency_override_flag_matches_schema():
    """The descriptor's supports_concurrency_override (what the submit-page tip
    reads) must exactly match the resolved concurrency-equivalent param (what the
    worker overrides) — so the tip never claims a module the override won't touch.
    Covers aliased spellings (e.g. opencompass's `max_workers`)."""
    for cls in MODULE_REGISTRY.values():
        pname = cls.concurrency_param_name()
        if pname is not None:
            assert pname in cls.ParamsSchema.model_fields
        assert cls.accepts_concurrency_override() is (pname is not None)
        assert cls.descriptor()["supports_concurrency_override"] is (pname is not None)


def test_opencompass_concurrency_alias_resolves_to_max_workers():
    """Regression for the alias: opencompass's concurrency-equivalent param is
    `max_workers`, so the override must target it (and the tip must list it)."""
    oc = get_module("opencompass")
    assert oc.concurrency_param_name() == "max_workers"
    assert oc.accepts_concurrency_override() is True


def test_default_params_validate_against_schema():
    """Default params should round-trip through the schema without errors."""
    for cls in MODULE_REGISTRY.values():
        defaults = cls.default_params()
        # Should not raise
        instance = cls.ParamsSchema(**defaults)
        assert instance is not None


def test_benchmark_yaml_modules_exist():
    """All modules named in perf-suite-v1.yaml must exist in the registry."""
    import yaml
    import os
    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "benchmarks", "perf-suite-v1.yaml"
    )
    with open(yaml_path) as f:
        bench = yaml.safe_load(f)
    for entry in bench["modules"]:
        assert entry["module_name"] in MODULE_REGISTRY, (
            f"Module {entry['module_name']!r} in YAML not found in registry"
        )


def test_benchmark_yaml_weights_sum_to_one():
    import yaml
    import os
    yaml_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "benchmarks", "perf-suite-v1.yaml"
    )
    with open(yaml_path) as f:
        bench = yaml.safe_load(f)
    total = sum(entry["weight"] for entry in bench["modules"])
    assert abs(total - 1.0) < 1e-6, f"Weights sum to {total}, expected 1.0"
