"""Pydantic schemas for module descriptors (GET /modules)."""
from __future__ import annotations

from pydantic import BaseModel


class ModuleDescriptor(BaseModel):
    name: str
    display_name: str
    description: str
    params_schema: dict
    default_params: dict
    metrics_schema: dict = {}
    # Lifecycle flag, read live from bench.modules.MODULE_REGISTRY (not the DB)
    # so it always reflects deployed code even if the startup sync hasn't run.
    deprecated: bool = False
    deprecation_note: str = ""
    # True if this module has a `concurrency` param the submission-level
    # concurrency override applies to. Also read live from the registry.
    supports_concurrency_override: bool = False
    # True if this module can replay a rolling dataset profile (see
    # TestModule.dataset_feed_fields). Also read live from the registry.
    supports_dataset_feed: bool = False


class ModuleListResponse(BaseModel):
    modules: list[ModuleDescriptor]
