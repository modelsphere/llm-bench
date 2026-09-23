"""
bench/modules — Registry of modular test units.

MODULE_REGISTRY maps module name → TestModule subclass.
The FastAPI app syncs this into the test_modules DB table on startup
and exposes GET /modules for the admin benchmark editor.

Usage:
    from bench.modules import MODULE_REGISTRY, get_module

    module_cls = get_module("perf_guidellm")
    params = module_cls.ParamsSchema(concurrency=8)
    result = module_cls().run(endpoint, params, output_dir="/tmp/out")
"""
from __future__ import annotations

from typing import Dict, List, Type

from bench.modules.base import TestModule  # noqa: F401 (re-exported)
from bench.modules.perf_guidellm import PerfGuidellmModule
from bench.modules.perf_guidellm_sweep import PerfGuidellmSweepModule
from bench.modules.case_truncation import CaseTruncationModule
from bench.modules.opencompass import OpenCompassModule
from bench.modules.replay import ReplayModule
from bench.modules.agentic import AgenticModule
from bench.modules.hallucination import HallucinationModule
from bench.modules.tool_call_success import ToolCallSuccessModule
from bench.modules.functional_acceptance import FunctionalAcceptanceModule

MODULE_REGISTRY: Dict[str, Type[TestModule]] = {
    PerfGuidellmModule.name:      PerfGuidellmModule,
    PerfGuidellmSweepModule.name: PerfGuidellmSweepModule,
    CaseTruncationModule.name: CaseTruncationModule,
    OpenCompassModule.name:    OpenCompassModule,
    ReplayModule.name:  ReplayModule,
    AgenticModule.name:        AgenticModule,
    HallucinationModule.name:        HallucinationModule,
    ToolCallSuccessModule.name:      ToolCallSuccessModule,
    FunctionalAcceptanceModule.name: FunctionalAcceptanceModule,
}


def get_module(name: str) -> Type[TestModule]:
    """Return a TestModule class by registry name. Raises KeyError if unknown."""
    if name not in MODULE_REGISTRY:
        raise KeyError(f"Unknown module {name!r}. Available: {list(MODULE_REGISTRY)}")
    return MODULE_REGISTRY[name]


def list_modules() -> List[dict]:
    """Return a list of module descriptor dicts (for GET /modules API)."""
    return [cls.descriptor() for cls in MODULE_REGISTRY.values()]
