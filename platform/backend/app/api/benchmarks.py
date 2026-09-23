"""Benchmarks router: admin CRUD + public list/detail."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import yaml
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import (
    ensure_can_manage,
    get_current_user,
    require_admin,
    require_service_or_admin,
)
from app.db.models import (
    Benchmark,
    BenchmarkModule,
    BenchmarkStatus,
    Submission,
    SubmissionRun,
    TestModule,
    User,
    get_async_session,
    is_admin_or_above,
)
from app.schemas.benchmarks import (
    BenchmarkCreate,
    BenchmarkGroupTagsBulkResponse,
    BenchmarkGroupTagsBulkUpdate,
    BenchmarkListResponse,
    BenchmarkResponse,
    BenchmarkUpdate,
    normalize_group_tags,
)

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _compute_config_hash(benchmark: Benchmark) -> str:
    """SHA-256 of benchmark config (slug + version + sorted modules). Returns 8-char hex."""
    payload = {
        "slug": benchmark.slug,
        "version": benchmark.version,
        "modules": [
            {
                "module_name": m.module_name,
                "weight": str(m.weight),
                "order_index": m.order_index,
                "params_json": m.params_json,
                "metric_configs_json": m.metric_configs_json or [],
                # Only fold the skip flag into the hash when it's set, so
                # benchmarks that don't use the feature keep their existing hash
                # (and existing leaderboard groupings stay intact).
                **(
                    {"skip_if_prev_failed": True}
                    if getattr(m, "skip_if_prev_failed", False)
                    else {}
                ),
            }
            for m in sorted(benchmark.modules, key=lambda x: x.order_index)
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _validate_module_weights(modules: list) -> None:
    """A module that has no scoring metrics must carry weight 0 — otherwise
    its slot in the benchmark's weighted average is reserved for a contribution
    that will always be 0, silently dragging the overall score down. Reject
    these payloads loudly rather than fixing them up server-side, so the UI
    can't desync from what the API persisted."""
    for bm in modules:
        configs = bm.metric_configs or []
        has_score = any(c.get("role") == "score" for c in configs)
        if not has_score and float(bm.weight) != 0.0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Module {bm.module_name!r} at order {bm.order_index} has no "
                    f"scoring metrics but weight={bm.weight}. Set weight to 0 "
                    f"(redline/display-only modules cannot contribute to the score)."
                ),
            )


async def _validate_dataset_profiles(session, modules) -> None:
    """Reject a module configured to replay a rolling dataset profile that does
    not exist.

    Same motivation as `_validate_module_params`: without this the mistake sits
    in the DB until a worker picks the submission up, and the user's run fails
    at the top of what may be a multi-hour benchmark. A typo'd slug is by far
    the likeliest way to get this wrong, so it is worth one query at edit time.
    """
    from app.db.models import ReplayDatasetProfile
    from bench.modules import get_module

    wanted: dict[str, int] = {}
    for bm in modules:
        try:
            module_cls = get_module(bm.module_name)
        except KeyError:
            continue
        fields = module_cls.dataset_feed_fields()
        if fields is None:
            continue
        params = bm.params_json or {}
        if str(params.get(fields.source, "") or "") != fields.auto_value:
            continue
        name = str(params.get(fields.profile, "") or "").strip()
        if name:
            wanted.setdefault(name, bm.order_index)
    if not wanted:
        return

    result = await session.execute(
        select(ReplayDatasetProfile.name).where(ReplayDatasetProfile.name.in_(wanted.keys()))
    )
    missing = set(wanted) - {r[0] for r in result.all()}
    if missing:
        names = ", ".join(sorted(repr(n) for n in missing))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown rolling dataset profile(s): {names}. Create the "
                f"collection profile on the admin Replay Datasets page first, "
                f"or set the module's dataset_source back to 'fixed'."
            ),
        )


def _validate_module_params(module_name: str, params_json: dict, order_index: int) -> None:
    """Run params_json through the module's ParamsSchema.

    Without this, bad values (e.g. request_timeout above the module's le cap)
    persist in the DB until a worker tries to run them, at which point pydantic
    raises ValidationError mid-job and the whole submission goes to ERROR. We'd
    rather reject at edit time so the admin sees the error in the UI.
    """
    from pydantic import ValidationError

    from bench.modules import get_module

    try:
        module_cls = get_module(module_name)
    except KeyError:
        # Unknown module names are caught separately (with a clearer "Unknown
        # modules: ..." message); skip schema validation here.
        return
    try:
        module_cls.ParamsSchema.model_validate(params_json)
    except ValidationError as exc:
        # Surface every field error so the admin can fix them all at once
        # instead of one round-trip per field.
        lines = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(root)"
            lines.append(f"{loc}: {err['msg']}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Module {module_name!r} (order {order_index}) has invalid params:\n"
                + "\n".join(lines)
            ),
        )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@router.post("/admin/benchmarks", response_model=BenchmarkResponse, status_code=status.HTTP_201_CREATED)
async def create_benchmark(
    body: BenchmarkCreate,
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    # Check slug uniqueness
    existing = await session.execute(select(Benchmark).where(Benchmark.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Slug {body.slug!r} already exists")

    # Validate module names exist
    module_names = [m.module_name for m in body.modules]
    result = await session.execute(select(TestModule.name).where(TestModule.name.in_(module_names)))
    found = {r[0] for r in result.all()}
    missing = set(module_names) - found
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown modules: {missing}")

    _validate_module_weights(body.modules)
    for bm in body.modules:
        _validate_module_params(bm.module_name, bm.params_json, bm.order_index)
    await _validate_dataset_profiles(session, body.modules)

    benchmark = Benchmark(
        slug=body.slug,
        name=body.name,
        description=body.description,
        version=body.version,
        status=BenchmarkStatus(body.status),
        group_tags=body.group_tags,
        created_by_user_id=admin.id,
    )
    session.add(benchmark)
    await session.flush()

    for bm in body.modules:
        session.add(BenchmarkModule(
            benchmark_id=benchmark.id,
            module_name=bm.module_name,
            params_json=bm.params_json,
            metric_configs_json=bm.metric_configs,
            weight=Decimal(str(bm.weight)),
            order_index=bm.order_index,
            skip_if_prev_failed=bm.skip_if_prev_failed,
        ))

    benchmark_id = benchmark.id
    await session.flush()

    # Compute hash now that modules are in the session
    refreshed_pre = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules))
        .where(Benchmark.id == benchmark_id)
    )
    benchmark = refreshed_pre.scalar_one()
    benchmark.config_hash = _compute_config_hash(benchmark)
    await session.commit()

    refreshed = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.id == benchmark_id)
    )
    return _build_benchmark_response(refreshed.scalar_one(), redact_secrets=False)


@router.get("/admin/benchmarks", response_model=BenchmarkListResponse)
async def admin_list_benchmarks(
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    query = (
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .order_by(Benchmark.created_at.desc())
    )
    if not is_admin_or_above(admin.role):
        query = query.where(Benchmark.created_by_user_id == admin.id)
    result = await session.execute(query)
    benchmarks = result.scalars().unique().all()
    return BenchmarkListResponse(
        benchmarks=[_build_benchmark_response(b, redact_secrets=False) for b in benchmarks]
    )


# Declared before the "/{benchmark_id}" routes on purpose: FastAPI matches in
# declaration order, and "group-tags" would otherwise be parsed as a benchmark
# id and rejected with a 422 before this handler was ever considered.
@router.put("/admin/benchmarks/group-tags", response_model=BenchmarkGroupTagsBulkResponse)
async def bulk_update_group_tags(
    body: BenchmarkGroupTagsBulkUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Replace `group_tags` on many benchmarks in one transaction.

    Grouping is presentation only (see normalize_group_tags), so — exactly as
    in update_benchmark — this stays out of the config hash and is allowed on a
    locked benchmark. The atomicity is the point: the admin Groups tab renames
    a group by rewriting that path prefix on every benchmark carrying it, and a
    half-applied rename would split one group into two.
    """
    ids = [a.benchmark_id for a in body.assignments]
    if not ids:
        return BenchmarkGroupTagsBulkResponse(updated=0)

    result = await session.execute(select(Benchmark).where(Benchmark.id.in_(ids)))
    by_id = {b.id: b for b in result.scalars().all()}
    missing = sorted(set(ids) - set(by_id))
    if missing:
        # All-or-nothing: a benchmark deleted since the tab loaded means the
        # admin is editing a stale tree, so reject rather than apply the rest.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown benchmark id(s): {missing}. Reload the page and try again.",
        )

    for assignment in body.assignments:
        # Already normalised by the schema validator.
        by_id[assignment.benchmark_id].group_tags = assignment.group_tags
    await session.commit()
    return BenchmarkGroupTagsBulkResponse(updated=len(body.assignments))


@router.put("/admin/benchmarks/{benchmark_id}", response_model=BenchmarkResponse)
async def update_benchmark(
    benchmark_id: int,
    body: BenchmarkUpdate,
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.id == benchmark_id)
    )
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    ensure_can_manage(admin, benchmark)

    # Lock check: structural changes (modules, version) are blocked when locked
    config_changed = body.modules is not None or (body.version is not None and body.version != benchmark.version)
    if benchmark.is_locked and config_changed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Benchmark is locked. Unlock it before changing modules or version.",
        )

    if body.status:
        benchmark.status = BenchmarkStatus(body.status)
    if body.name is not None:
        benchmark.name = body.name
    if body.description is not None:
        benchmark.description = body.description
    if body.version is not None:
        benchmark.version = body.version
    if body.group_tags is not None:
        # Presentation only: settable while locked, not part of the config
        # hash. Already normalised by the schema validator.
        benchmark.group_tags = body.group_tags

    if body.modules is not None:
        _validate_module_weights(body.modules)
        for bm in body.modules:
            _validate_module_params(bm.module_name, bm.params_json, bm.order_index)
        await _validate_dataset_profiles(session, body.modules)
        # Sync modules in-place so existing submission_runs FKs stay valid
        existing_by_order = {m.order_index: m for m in benchmark.modules}
        incoming_orders = set()

        for bm in body.modules:
            incoming_orders.add(bm.order_index)
            if bm.order_index in existing_by_order:
                mod = existing_by_order[bm.order_index]
                mod.module_name = bm.module_name
                mod.params_json = bm.params_json
                mod.metric_configs_json = bm.metric_configs
                mod.weight = Decimal(str(bm.weight))
                mod.skip_if_prev_failed = bm.skip_if_prev_failed
            else:
                session.add(BenchmarkModule(
                    benchmark_id=benchmark.id,
                    module_name=bm.module_name,
                    params_json=bm.params_json,
                    metric_configs_json=bm.metric_configs,
                    weight=Decimal(str(bm.weight)),
                    order_index=bm.order_index,
                    skip_if_prev_failed=bm.skip_if_prev_failed,
                ))

        # Only remove modules that are no longer present and have no runs
        for order_index, mod in existing_by_order.items():
            if order_index in incoming_orders:
                continue
            run_result = await session.execute(
                select(SubmissionRun).where(SubmissionRun.benchmark_module_id == mod.id).limit(1)
            )
            if run_result.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot remove module at order {order_index}: it has existing submission runs.",
                )
            await session.delete(mod)

        await session.flush()

    # Recompute hash if structural fields changed
    if config_changed:
        refreshed_modules = await session.execute(
            select(Benchmark).options(selectinload(Benchmark.modules)).where(Benchmark.id == benchmark_id)
        )
        benchmark = refreshed_modules.scalar_one()
        benchmark.config_hash = _compute_config_hash(benchmark)

    await session.commit()
    refreshed = await session.execute(
        select(Benchmark).options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module)).where(Benchmark.id == benchmark_id)
    )
    return _build_benchmark_response(refreshed.scalar_one(), redact_secrets=False)


@router.delete("/admin/benchmarks/{benchmark_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_benchmark(
    benchmark_id: int,
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(select(Benchmark).where(Benchmark.id == benchmark_id))
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    ensure_can_manage(admin, benchmark)

    # Block delete if any submissions exist (integrity protection)
    count_result = await session.execute(
        select(Submission).where(Submission.benchmark_id == benchmark_id).limit(1)
    )
    if count_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete: benchmark has submissions. Archive it instead.",
        )

    await session.delete(benchmark)
    await session.commit()


@router.put("/admin/benchmarks/{benchmark_id}/lock", response_model=BenchmarkResponse)
async def toggle_benchmark_lock(
    benchmark_id: int,
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Toggle the locked state of a benchmark."""
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.id == benchmark_id)
    )
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    ensure_can_manage(admin, benchmark)

    benchmark.is_locked = not benchmark.is_locked
    await session.commit()
    await session.refresh(benchmark)

    refreshed = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.id == benchmark_id)
    )
    return _build_benchmark_response(refreshed.scalar_one(), redact_secrets=False)


@router.get("/admin/benchmarks/{benchmark_id}/export")
async def export_benchmark(
    benchmark_id: int,
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.id == benchmark_id)
    )
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    ensure_can_manage(admin, benchmark)

    data = {
        "name": benchmark.name,
        "slug": benchmark.slug,
        "description": benchmark.description,
        "version": benchmark.version,
        "status": benchmark.status.value,
        # Display grouping ("Top/Mid/Low" paths); travels with the YAML so an
        # imported benchmark lands in the same groups. Not in the config hash.
        "group_tags": list(benchmark.group_tags or []),
        "modules": [
            {
                "module_name": bm.module_name,
                "weight": float(bm.weight),
                "order_index": bm.order_index,
                "params": bm.params_json,
                "metric_configs": bm.metric_configs_json or [],
                "skip_if_prev_failed": bm.skip_if_prev_failed,
            }
            for bm in sorted(benchmark.modules, key=lambda m: m.order_index)
        ],
    }
    yaml_text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return Response(
        content=yaml_text,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{benchmark.slug}.yaml"'},
    )


@router.post("/admin/benchmarks/import", response_model=BenchmarkResponse, status_code=status.HTTP_201_CREATED)
async def import_benchmark(
    file: UploadFile = File(...),
    admin: User = Depends(require_service_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    raw = await file.read()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid YAML: {exc}")

    for field in ("name", "slug", "modules"):
        if field not in data:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Missing required field: '{field}'")

    existing = await session.execute(select(Benchmark).where(Benchmark.slug == data["slug"]))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Slug '{data['slug']}' already exists")

    module_names = [m["module_name"] for m in data["modules"]]
    found_result = await session.execute(select(TestModule.name).where(TestModule.name.in_(module_names)))
    found = {r[0] for r in found_result.all()}
    missing = set(module_names) - found
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown modules: {sorted(missing)}")

    for i, mod in enumerate(data["modules"]):
        _validate_module_params(
            mod["module_name"], mod.get("params", {}), mod.get("order_index", i)
        )

    # Optional: YAML written before grouping existed imports untagged.
    try:
        group_tags = normalize_group_tags(data.get("group_tags") or [])
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"group_tags: {exc}")

    benchmark = Benchmark(
        slug=data["slug"],
        name=data["name"],
        description=data.get("description", ""),
        version=str(data.get("version", "1")),
        status=BenchmarkStatus(data.get("status", "draft")),
        group_tags=group_tags,
        created_by_user_id=admin.id,
    )
    session.add(benchmark)
    await session.flush()

    for i, mod in enumerate(data["modules"]):
        session.add(BenchmarkModule(
            benchmark_id=benchmark.id,
            module_name=mod["module_name"],
            params_json=mod.get("params", {}),
            metric_configs_json=mod.get("metric_configs", []),
            weight=Decimal(str(mod.get("weight", 0))),
            order_index=mod.get("order_index", i),
            skip_if_prev_failed=bool(mod.get("skip_if_prev_failed", False)),
        ))

    benchmark_id = benchmark.id
    await session.flush()

    refreshed_pre = await session.execute(
        select(Benchmark).options(selectinload(Benchmark.modules)).where(Benchmark.id == benchmark_id)
    )
    benchmark = refreshed_pre.scalar_one()
    benchmark.config_hash = _compute_config_hash(benchmark)
    await session.commit()

    refreshed = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.id == benchmark_id)
    )
    return _build_benchmark_response(refreshed.scalar_one(), redact_secrets=False)


# ---------------------------------------------------------------------------
# Public / user endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=BenchmarkListResponse)
async def list_active_benchmarks(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.status == BenchmarkStatus.ACTIVE)
        .order_by(Benchmark.created_at.desc())
    )
    benchmarks = result.scalars().unique().all()
    redact = not is_admin_or_above(user.role)
    return BenchmarkListResponse(
        benchmarks=[_build_benchmark_response(b, redact_secrets=redact) for b in benchmarks]
    )


@router.get("/{slug}", response_model=BenchmarkResponse)
async def get_benchmark(
    slug: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.execute(
        select(Benchmark)
        .options(selectinload(Benchmark.modules).selectinload(BenchmarkModule.module))
        .where(Benchmark.slug == slug)
    )
    benchmark = result.scalar_one_or_none()
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    # Admins get params verbatim — the benchmark editor loads through this route
    # and needs stored credentials (e.g. a judge api key) to round-trip on save.
    return _build_benchmark_response(benchmark, redact_secrets=not is_admin_or_above(user.role))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _build_benchmark_response(benchmark: Benchmark, *, redact_secrets: bool) -> dict[str, Any]:
    """Serialize a benchmark. ``redact_secrets`` masks credential values inside
    each module's params_json (e.g. an LLM-judge api key the admin configured)
    and must be True whenever the caller isn't admin-gated — params_json is
    otherwise returned verbatim to every logged-in user. Keyword-only and
    default-less on purpose: every new call site has to make the choice."""
    from app.core.metric_configs import merge_display_defaults
    from app.core.module_caps import supports_concurrency_override
    from app.core.param_secrets import redact_params

    return {
        "id": benchmark.id,
        "slug": benchmark.slug,
        "name": benchmark.name,
        "description": benchmark.description,
        "version": benchmark.version,
        "status": benchmark.status.value,
        "config_hash": benchmark.config_hash,
        "is_locked": benchmark.is_locked,
        "group_tags": list(benchmark.group_tags or []),
        "created_by_user_id": benchmark.created_by_user_id,
        "created_at": benchmark.created_at,
        "modules": [
            {
                "id": bm.id,
                "module_name": bm.module_name,
                "display_name": bm.module.display_name if bm.module else bm.module_name,
                "params_json": redact_params(bm.params_json) if redact_secrets else bm.params_json,
                # Merge module-default display rows in so the admin UI shows
                # every metric the module emits without requiring the YAML or
                # admin to enumerate them. Score/redline rules from the DB
                # are preserved verbatim — only display rows are auto-added.
                "metric_configs": merge_display_defaults(
                    bm.module_name, bm.metric_configs_json or []
                ),
                "weight": float(bm.weight),
                "order_index": bm.order_index,
                "skip_if_prev_failed": bm.skip_if_prev_failed,
                "metrics_schema": bm.module.metrics_schema_json if bm.module else {},
                "params_schema": bm.module.params_schema_json if bm.module else {},
                "supports_concurrency_override": supports_concurrency_override(bm.module_name),
            }
            for bm in sorted(benchmark.modules, key=lambda m: m.order_index)
        ],
    }
