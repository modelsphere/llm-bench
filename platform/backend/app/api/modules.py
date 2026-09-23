"""Modules router: GET /modules (public), GET /modules/{name}."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_caps import supports_concurrency_override, supports_dataset_feed
from app.db.models import TestModule, get_async_session
from app.schemas.modules import ModuleDescriptor, ModuleListResponse

router = APIRouter(prefix="/modules", tags=["modules"])


def _to_descriptor(m: TestModule) -> ModuleDescriptor:
    """Build a module descriptor, preferring LIVE module code over the DB JSONB
    snapshot for the schema/metrics/deprecation.

    Two reasons to read live: (1) JSONB normalises object key order, scrambling
    the params-form field order (it stores keys by length then bytewise, not
    definition order), and (2) live code always reflects what's deployed even if
    the startup module-sync lagged. Order-insensitive identity fields
    (name/display_name/description) come from the DB row. Falls back to the
    stored columns for orphan rows whose module was removed from code.

    The import is deferred (matching app/core/metric_configs.py) because the
    bench layer is only on sys.path after main.py inserts BENCH_MODULES_PATH.
    """
    from bench.modules import MODULE_REGISTRY

    cls = MODULE_REGISTRY.get(m.name)
    if cls is not None:
        d = cls.descriptor()
        params_schema = d["params_schema"]
        default_params = d["default_params"]
        metrics_schema = {
            "metrics_descriptors": d.get("metrics_descriptors", []),
            "default_metric_configs": d.get("default_metric_configs", []),
        }
        deprecated = bool(getattr(cls, "deprecated", False))
        note = str(getattr(cls, "deprecation_note", "") or "")
    else:
        params_schema = m.params_schema_json
        default_params = m.default_params_json
        metrics_schema = m.metrics_schema_json or {}
        deprecated, note = False, ""

    return ModuleDescriptor(
        name=m.name,
        display_name=m.display_name,
        description=m.description,
        params_schema=params_schema,
        default_params=default_params,
        metrics_schema=metrics_schema,
        deprecated=deprecated,
        deprecation_note=note,
        supports_concurrency_override=supports_concurrency_override(m.name),
        supports_dataset_feed=supports_dataset_feed(m.name),
    )


@router.get("", response_model=ModuleListResponse)
async def list_modules(session: AsyncSession = Depends(get_async_session)):
    """Return all registered module descriptors (schema from live code; see _to_descriptor)."""
    result = await session.execute(select(TestModule))
    modules = result.scalars().all()
    return ModuleListResponse(modules=[_to_descriptor(m) for m in modules])


@router.get("/{name}", response_model=ModuleDescriptor)
async def get_module(name: str, session: AsyncSession = Depends(get_async_session)):
    """Return a single module descriptor by name."""
    result = await session.execute(select(TestModule).where(TestModule.name == name))
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module {name!r} not found")
    return _to_descriptor(m)
