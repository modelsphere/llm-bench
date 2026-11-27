"""Run the frozen BFCL V4 subset in its isolated, pinned Python environment."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from bench.bfcl.diagnostics import EventSummary, exception_details, read_error, read_runner_state, save_state
from bench.modules.base import (
    BenchmarkCancelled,
    BenchmarkTimeout,
    EndpointConfig,
    MetricConfig,
    MetricDescriptor,
    ModuleResult,
    TestModule,
    _noop_progress,
)

logger = logging.getLogger(__name__)


def _checkpoint(progress_cb):
    callback = getattr(progress_cb, "checkpoint", None)
    if callback is not None:
        try:
            callback()
        except Exception:
            logger.warning("BFCL worker_log_checkpoint_failed")


MANIFEST_DIR = Path(__file__).parents[1] / "bfcl"
MANIFESTS = {
    "bfcl-v4-smoke-v1": MANIFEST_DIR / "smoke-manifest.json",
    "bfcl-v4-tool-use-v1": MANIFEST_DIR / "tool-use-manifest.json",
    "bfcl-v4-multi-turn-long-context-v1": MANIFEST_DIR / "multi-turn-long-context-manifest.json",
}


# In the pinned BFCL revision, the tool-use and long-context categories have
# contiguous zero-based IDs and at least 200 official cases.
MAX_TOOL_USE_CASES_PER_CATEGORY = 200

# The platform actor has a 48-hour limit. Leave an hour for setup, artifact
# persistence and teardown, including when the total budget is implicit.
BFCL_SCAN_MAX_SECONDS = 47 * 3600
BFCL_SCAN_METRIC_KEYS = frozenset({
    "reported_concurrency", "sweep_complete", "sweep_canceled", "sweep_timed_out",
    "sweep_points_attempted", "sweep_points_completed", "sweep_points_planned",
    "sweep_stop_reason",
})


class BFCLV4Params(BaseModel):
    task_set: Literal[
        "bfcl-v4-smoke-v1",
        "bfcl-v4-tool-use-v1",
        "bfcl-v4-multi-turn-long-context-v1",
    ] = "bfcl-v4-smoke-v1"
    cases_per_category: int = Field(
        1,
        ge=1,
        le=MAX_TOOL_USE_CASES_PER_CATEGORY,
        description="Number of cases per category for tool-use and multi-turn-long-context task sets.",
    )
    case_selection: Literal["head", "tail"] = Field(
        "head",
        description="Use the original leading case IDs (head), or take the last cases in pinned dataset order (tail).",
    )
    concurrency: int = Field(2, ge=1, le=128)
    concurrency_grid: list[Annotated[int, Field(strict=True, ge=1, le=128)]] | None = Field(
        default=None, min_length=2, max_length=16)
    max_total_seconds: float | None = Field(
        default=None, ge=1, le=BFCL_SCAN_MAX_SECONDS,
        description="Scan budget in seconds; at most 47 hours including all concurrency points.",
    )
    request_timeout_seconds: float = Field(120, ge=1, le=3600)
    max_seconds: float = Field(1800, ge=1, le=86400)
    request_retries: int = Field(0, ge=0, le=3)
    allow_request_failures: bool = Field(
        False, description="Score cases interrupted by HTTP request failures as incorrect."
    )
    export_replay_trace: bool = False
    probe_endpoint: bool = Field(
        False,
        description="Run one official multi-turn generation before the measured run.",
    )
    required_metrics: list[str] = Field(default_factory=lambda: ["accuracy"])
    min_ttft_coverage: float = Field(0, ge=0, le=1)
    min_usage_coverage: float = Field(0, ge=0, le=1)
    min_cache_usage_coverage: float = Field(0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_case_count(self):
        if self.task_set == "bfcl-v4-smoke-v1" and self.case_selection != "head":
            raise ValueError("bfcl-v4-smoke-v1 has fixed cases; case_selection must be head")
        if self.task_set == "bfcl-v4-smoke-v1" and self.cases_per_category != 1:
            raise ValueError(
                "bfcl-v4-smoke-v1 has a fixed case count; use bfcl-v4-tool-use-v1 to select more"
            )
        if self.concurrency_grid is not None:
            if self.concurrency_grid != sorted(set(self.concurrency_grid)):
                raise ValueError("concurrency_grid must be strictly increasing and unique")
            budget = self.max_total_seconds or self.max_seconds * len(self.concurrency_grid)
            if budget > BFCL_SCAN_MAX_SECONDS:
                raise ValueError(
                    "BFCL scan budget exceeds 47 hours; reduce max_seconds or set max_total_seconds"
                )
        return self


def run_process(command, *, env, deadline, cancel_event):
    """Own the entire process group, including scorer children, until reaped."""

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise BenchmarkCancelled("BFCL canceled")
        if time.monotonic() >= deadline:
            raise BenchmarkTimeout("BFCL exceeded its total wall-clock budget")

    check()
    phase = command[command.index("--phase") + 1] if "--phase" in command else "unknown"
    root = Path(command[command.index("--root") + 1]) if "--root" in command else None
    summary = EventSummary(root) if root else None
    started = time.monotonic()
    next_heartbeat = started + 30
    # Upstream may print prompts and credentials in exceptions. Only our typed
    # failure and sanitized event files are published, never raw child output.
    process = subprocess.Popen(
        command,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=None if env.get("BFCL_LOCAL_DEBUG") == "1" else subprocess.DEVNULL,
    )
    try:
        logger.info("BFCL runner started phase=%s pid=%s", phase, process.pid)
        while process.poll() is None:
            check()
            now = time.monotonic()
            if summary is not None and now >= next_heartbeat:
                logger.info("BFCL runner heartbeat phase=%s pid=%s elapsed_seconds=%.1f requests=%s",
                            phase, process.pid, now - started, json.dumps(summary.poll()))
                next_heartbeat = now + 30
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        check()
        if process.returncode:
            raise RuntimeError(
                f"BFCL runner exited with code {process.returncode}; inspect worker diagnostics"
            )
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if root is not None:
            save_state(root / "process_state.json", {
                "phase": phase, "pid": process.pid, "exit_code": process.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            })
        logger.info("BFCL runner exited phase=%s pid=%s exit_code=%s elapsed_seconds=%.1f",
                    phase, process.pid, process.returncode, time.monotonic() - started)


def _write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


class BFCLV4Module(TestModule):
    name = "bfcl_v4"
    display_name = "BFCL V4 subset"
    description = "Official BFCL frozen subset: tool-use quality and streaming performance. Not the full leaderboard."
    ParamsSchema = BFCLV4Params
    card_norm_baseline = 8
    metrics_descriptors = [
        MetricDescriptor(name, name.replace("_", " "), unit, higher_is_better=higher)
        for name, unit, higher in [
            ("accuracy", "", True),
            ("reported_concurrency", "cases", True),
            ("sweep_complete", "", True),
            ("sweep_canceled", "", False),
            ("sweep_timed_out", "", False),
            ("sweep_points_attempted", "", True),
            ("sweep_points_completed", "", True),
            ("sweep_points_planned", "", True),
            ("accuracy_simple_python", "", True),
            ("accuracy_multi_turn_base", "", True),
            ("accuracy_multi_turn_long_context", "", True),
            ("accuracy_multiple", "", True),
            ("accuracy_parallel", "", True),
            ("accuracy_irrelevance", "", True),
            ("ttft_mean_ms", "ms", False),
            ("ttft_p50_ms", "ms", False),
            ("ttft_p90_ms", "ms", False),
            ("ttft_p99_ms", "ms", False),
            ("request_e2e_p90_ms", "ms", False),
            ("generation_wall_seconds", "s", False),
            ("grading_wall_seconds", "s", False),
            ("request_throughput_rps", "req/s", True),
            ("successful_request_rps", "req/s", True),
            ("input_tokens_per_second", "tok/s", True),
            ("output_tokens_per_second", "tok/s", True),
            ("input_tpm", "tok/min", True),
            ("output_tpm", "tok/min", True),
            ("total_tpm", "tok/min", True),
            ("cache_hit_rate", "", True),
            ("cached_input_tokens", "tok", True),
            ("passed_cases_per_hour", "cases/h", True),
            ("active_cases_peak", "", True),
            ("active_http_peak", "", True),
            ("http_attempt_count", "", False),
            ("request_error_count", "", False),
            ("retry_count", "", False),
            ("infra_error_count", "", False),
            ("case_count", "", True),
            ("case_count_simple_python", "", True),
            ("case_count_multi_turn_base", "", True),
            ("case_count_multi_turn_long_context", "", True),
            ("case_count_multiple", "", True),
            ("case_count_parallel", "", True),
            ("case_count_irrelevance", "", True),
            ("ttft_sample_count", "", True),
            ("cache_hit_rate_first", "", True),
            ("cache_hit_rate_subsequent", "", True),
            ("cache_usage_coverage_first", "", True),
            ("cache_usage_coverage_subsequent", "", True),
            ("ttft_coverage", "", True),
            ("input_usage_coverage", "", True),
            ("output_usage_coverage", "", True),
            ("cache_usage_coverage", "", True),
        ]
    ] + [
        MetricDescriptor(
            "request_output_tps", "Per-request Output TPS", "tok/s",
            "Mean of completion tokens divided by end-to-end latency across successful requests.",
        ),
        MetricDescriptor(
            "uptime", "Uptime", "%", "Fraction of successful generation HTTP attempts.",
        ),
    ]

    default_metric_configs = [
        MetricConfig("accuracy", role="score", formula="passthrough"),
        *[
            MetricConfig(metric.name, role="display")
            for metric in metrics_descriptors
            if metric.name != "accuracy"
        ],
    ]

    @classmethod
    def time_budget_seconds(cls, params):
        if params.concurrency_grid:
            return params.max_total_seconds or params.max_seconds * len(params.concurrency_grid)
        return params.max_seconds

    def run(self, endpoint, params, output_dir, progress_cb=_noop_progress, cancel_event=None):
        if params.concurrency_grid:
            return self._run_grid(endpoint, params, output_dir, progress_cb, cancel_event)
        return self._run_single(endpoint, params, output_dir, progress_cb, cancel_event)

    def _run_grid(self, endpoint, params, output_dir, progress_cb, cancel_event):
        from bench.modules.evaluator import evaluate

        root = Path(output_dir) / uuid4().hex
        root.mkdir(parents=True)
        index = root / "artifacts.json"
        result = ModuleResult(artifacts=[str(index)])
        grid = params.concurrency_grid
        deadline = time.monotonic() + self.time_budget_seconds(params)
        configs = endpoint.run_context.get("metric_configs") or []
        # Aggregate metrics do not exist on a child point. Their rules still
        # participate in the protocol hash and the worker's final evaluation.
        point_configs = [c for c in configs if c.get("key") not in BFCL_SCAN_METRIC_KEYS]
        levels = {}
        canceled = None
        stop_reason = ""
        timed_out = False
        active_concurrency = None
        point_states = {}
        selection = {"selected_cases": {}} if params.case_selection == "tail" else {}

        def checkpoint_scan(status):
            snapshot = {"status": status, "concurrency_grid": grid,
                        "active_concurrency": active_concurrency, "points": point_states}
            save_state(root / "scan_state.json", snapshot)
            compact = {name: {k: point[k] for k in ("status", "valid", "eligible", "error") if k in point}
                       for name, point in point_states.items()}
            logger.info("BFCL scan checkpoint %s", json.dumps({**snapshot, "points": compact}))
            _checkpoint(progress_cb)

        checkpoint_scan("running")
        try:
            for i, concurrency in enumerate(grid):
                if cancel_event is not None and cancel_event.is_set():
                    raise BenchmarkCancelled("BFCL scan canceled")
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    timed_out = True
                    stop_reason = "BFCL scan exceeded its total wall-clock budget"
                    break
                point = params.model_copy(update={
                    "concurrency_grid": None, "max_total_seconds": None,
                    "concurrency": concurrency, "max_seconds": min(params.max_seconds, remaining),
                    "probe_endpoint": params.probe_endpoint and i == 0,
                })
                active_concurrency = concurrency
                point_states[f"c{concurrency}"] = {"status": "running"}
                checkpoint_scan("running")
                def point_progress(fraction, message):
                    progress_cb((i + fraction) / len(grid), f"c{concurrency}: {message}")
                point_progress.checkpoint = lambda: _checkpoint(progress_cb)
                child = self._run_single(
                    endpoint, point, str(root / f"c{concurrency}"), point_progress, cancel_event,
                    **selection,
                )
                required = ["total_tpm", *[c["key"] for c in point_configs
                                         if c.get("role") in ("score", "redline")]]
                valid = not child.error and all(
                    isinstance(child.metrics.get(k), (int, float))
                    and math.isfinite(child.metrics[k]) for k in required)
                _, eligible = evaluate(child.metrics, point_configs)
                levels[f"c{concurrency}"] = {
                    **child.metrics, "valid": int(valid), "eligible": int(valid and eligible),
                    "case_concurrency": concurrency,
                }
                if not valid:
                    error = child.error or "Missing required point metrics"
                    levels[f"c{concurrency}"]["error"] = error
                    save_state(root / f"c{concurrency}" / "failure.json", {
                        "error": error, "concurrency": concurrency,
                        "diagnostic": child.metrics.get("diagnostic"),
                    })
                point_states[f"c{concurrency}"] = {
                    "status": "completed" if valid else child.metrics.get("diagnostic", {}).get("status", "failed"),
                    **levels[f"c{concurrency}"]}
                logger.log(logging.INFO if valid else logging.ERROR,
                           "BFCL concurrency point finished concurrency=%s state=%s",
                           concurrency, json.dumps(point_states[f"c{concurrency}"]))
                result.metrics.update(levels)
                active_concurrency = None
                checkpoint_scan("running")
            if cancel_event is not None and cancel_event.is_set():
                raise BenchmarkCancelled("BFCL scan canceled")
        except BenchmarkCancelled as exc:
            timeout_event = getattr(cancel_event, "bfcl_timeout_event", None)
            if timeout_event is not None and timeout_event.is_set():
                timed_out = True
                stop_reason = "BFCL scan exceeded the worker time budget"
            else:
                canceled = exc
                stop_reason = result.error = "BFCL scan canceled"
            if active_concurrency is not None:
                levels[f"c{active_concurrency}"] = {
                    "valid": 0, "eligible": 0, "case_concurrency": active_concurrency,
                    "error": stop_reason,
                    "diagnostic": getattr(exc, "diagnostic", None),
                }
                result.metrics.update(levels)
                point_states[f"c{active_concurrency}"] = {
                    "status": "timed_out" if timed_out else "canceled",
                    **levels[f"c{active_concurrency}"]}
                save_state(root / f"c{active_concurrency}" / "failure.json",
                           point_states[f"c{active_concurrency}"])
        finally:
            # Keep a representative even on cancellation for diagnostics; the
            # cancellation flag always prevents it from ranking or becoming a best level.
            passing = [(k, v) for k, v in levels.items() if v["eligible"]]
            diagnostic = [(k, v) for k, v in levels.items() if v["valid"]]
            choices = passing or diagnostic
            if choices:
                key, best = max(choices, key=lambda kv: (
                    kv[1]["total_tpm"], -kv[1]["case_concurrency"]))
                result.metrics.update({k: v for k, v in best.items()
                                       if k not in ("valid", "eligible", "error")})
                result.metrics["reported_concurrency"] = int(key[1:])
            elif canceled is None:
                result.error = stop_reason or "BFCL scan produced no valid concurrency point"
            complete = len(levels) == len(grid) and all(v["valid"] for v in levels.values())
            result.metrics.update(
                sweep_complete=int(complete and canceled is None),
                sweep_canceled=int(canceled is not None), sweep_timed_out=int(timed_out),
                sweep_stop_reason=stop_reason, sweep_points_attempted=len(levels),
                sweep_points_completed=sum(v["valid"] for v in levels.values()),
                sweep_points_planned=len(grid),
            )
            result.extra_display_configs = [
                {"key": f"{level}.{metric}", "role": "display"}
                for level, values in levels.items() for metric, value in values.items()
                if isinstance(value, (int, float))
            ]
            checkpoint_scan("canceled" if canceled else "timed_out" if timed_out else
                            "completed" if complete else "partial_failure")
            protocol_params = params.model_dump(exclude_none=True)
            if not params.allow_request_failures:
                protocol_params.pop("allow_request_failures")
            if params.case_selection == "head":
                protocol_params.pop("case_selection")
            protocol = {"params": protocol_params, "redlines": configs}
            _write_json(root / "scan.json", {
                "protocol_hash": hashlib.sha256(json.dumps(
                    protocol, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "version": 1, "concurrency_grid": grid, "order": "ascending",
                "cache_policy": "shared endpoint cache; no reset; no synthetic warmup",
                "metrics": result.metrics, "error": result.error,
            })
            _write_json(index, {"version": 1, "error": result.error,
                               "files": [str(p.relative_to(root)) for p in sorted(root.rglob("*"))
                                         if p.is_file() and p != index]})
        if canceled:
            canceled.artifacts = result.artifacts
            canceled.partial_result = result
            raise canceled
        return result

    def _run_single(
        self,
        endpoint: EndpointConfig,
        params,
        output_dir,
        progress_cb=_noop_progress,
        cancel_event=None,
        *,
        selected_cases: dict[str, list[str]] | None = None,
    ):
        started = time.monotonic()
        deadline = started + params.max_seconds
        attempt = uuid4().hex
        root = Path(output_dir) / attempt
        root.mkdir(parents=True, exist_ok=False)
        index = root / "artifacts.json"
        config_path = root / "config.json"
        selection_path = root / "case-selection.json"
        probe_root = root / "probe"
        probe_config_path = probe_root / "config.json"
        result = ModuleResult(artifacts=[str(index)])
        canceled = None
        phase = "setup"
        phase_root = root
        status = "running"
        runner_active = False
        parent_error = None
        identity = {k: v for k, v in getattr(endpoint, "run_context", {}).items()
                    if k in {"submission_id", "module_run_id"}}
        context = {**identity, "attempt_id": attempt, "concurrency": params.concurrency,
                   "task_set": params.task_set}
        logger.info("BFCL attempt started context=%s request_timeout_seconds=%s max_seconds=%s retries=%s",
                    json.dumps(context), params.request_timeout_seconds, params.max_seconds, params.request_retries)
        save_state(root / "state.json", {**context, "status": status, "phase": phase})
        try:
            url = urlsplit(endpoint.api_url)
            if url.username or url.password or url.query or url.fragment:
                raise ValueError(
                    "BFCL endpoint must not contain URL credentials, query or fragment"
                )
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError("BFCL requires an HTTP(S) endpoint")
            manifest = json.loads(MANIFESTS[params.task_set].read_text())
            python = os.environ.get("BFCL_PYTHON", "/opt/bfcl/bin/python")
            if "/" in python and not os.access(python, os.X_OK):
                raise RuntimeError(
                    "BFCL runner environment unavailable. Use a Linux AMD64 BFCL worker or set BFCL_PYTHON."
                )
            if params.case_selection == "tail":
                if selected_cases is None:
                    selected_cases = {}
                if not selected_cases:
                    phase = "select"
                    _write_json(selection_path, {
                        "revision": manifest["revision"],
                        "categories": list(manifest["cases"]),
                        "count": params.cases_per_category,
                    })
                    save_state(root / "state.json", {**context, "status": status, "phase": phase})
                    runner_active = True
                    run_process(
                        [python, "-m", "bench.bfcl.runner", "--phase", phase,
                         "--root", str(root.resolve())],
                        env=os.environ.copy(), deadline=deadline, cancel_event=cancel_event,
                    )
                    runner_active = False
                    selected_cases.update(json.loads(selection_path.read_text()))
                    phase = "setup"
                manifest["cases"] = selected_cases
            elif params.cases_per_category > 1:
                manifest["cases"] = {
                    category: [
                        f"{category}_{index}"
                        for index in range(params.cases_per_category)
                    ]
                    for category in manifest["cases"]
                }
            logger.info("BFCL cases selected context=%s case_count=%s categories=%s",
                        json.dumps(context), sum(len(ids) for ids in manifest["cases"].values()),
                        ",".join(manifest["cases"]))
            manifest.update(
                concurrency=params.concurrency,
                handler_version="llmbench-stream-v2",
                request_timeout_seconds=params.request_timeout_seconds,
                retry_policy={
                    "max_retries": params.request_retries,
                    "backoff_seconds": 0.5,
                },
                warmup={"requests": 0},
                ttft_policy="text-or-tool-calls; excludes-reasoning",
                official_maximum_step_limit=20,
            )
            if params.probe_endpoint:
                manifest["probe"] = {"case_id": "multi_turn_base_0", "concurrency": 1}
            if params.allow_request_failures:
                manifest["allow_request_failures"] = True
            protocol = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            manifest["protocol_hash"] = hashlib.sha256(protocol).hexdigest()
            _write_json(root / "manifest.json", manifest)
            _write_json(root / "params.json", params.model_dump())
            identity = {**getattr(endpoint, "run_context", {}), "attempt_id": attempt}
            _write_json(root / "identity.json", identity)
            config = {
                "manifest": manifest,
                "endpoint": endpoint.api_url,
                "model": endpoint.model,
                "timeout": params.request_timeout_seconds,
                "concurrency": params.concurrency,
                "request_retries": params.request_retries,
                "export_trace": params.export_replay_trace,
                "identity": identity,
            }
            _write_json(config_path, config)
            config_path.chmod(0o600)
            env = {
                **os.environ,
                "BFCL_ENDPOINT_API_KEY": endpoint.api_key,
                "BFCL_PROJECT_ROOT": str(root.resolve()),
            }
            phases = [("generate", 0.1, root), ("evaluate", 0.75, root)]
            if params.probe_endpoint:
                probe_root.mkdir()
                probe_manifest = json.loads(MANIFESTS["bfcl-v4-smoke-v1"].read_text())
                probe_manifest.update(
                    name="bfcl-v4-endpoint-probe-v1",
                    concurrency=1,
                    cases={"multi_turn_base": ["multi_turn_base_0"]},
                )
                _write_json(
                    probe_config_path,
                    {
                        **config,
                        "manifest": probe_manifest,
                        "concurrency": 1,
                        "export_trace": False,
                    },
                )
                probe_config_path.chmod(0o600)
                phases.insert(0, ("probe", 0.02, probe_root))
            for phase, fraction, phase_root in phases:
                save_state(root / "state.json", {**context, "status": status, "phase": phase})
                logger.info("BFCL phase started context=%s phase=%s", json.dumps(context), phase)
                phase_started = time.monotonic()
                progress_cb(fraction, f"BFCL {phase}")
                runner_active = True
                run_process(
                    [
                        python,
                        "-m",
                        "bench.bfcl.runner",
                        "--phase",
                        phase,
                        "--root",
                        str(phase_root.resolve()),
                    ],
                    env=env,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                runner_active = False
                logger.info("BFCL phase completed context=%s phase=%s elapsed_seconds=%.1f requests=%s",
                            json.dumps(context), phase, time.monotonic() - phase_started,
                            json.dumps(EventSummary(phase_root).poll()))
            phase = "validate_metrics"
            result.metrics = json.loads((root / "summary.json").read_text())["metrics"]
            for key in params.required_metrics:
                value = result.metrics.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"BFCL required metric unavailable: {key}")
            for key, minimum in [
                ("ttft_coverage", params.min_ttft_coverage),
                ("input_usage_coverage", params.min_usage_coverage),
                ("output_usage_coverage", params.min_usage_coverage),
                ("cache_usage_coverage", params.min_cache_usage_coverage),
            ]:
                if minimum and (result.metrics.get(key) or 0) < minimum:
                    raise ValueError(f"BFCL telemetry coverage below threshold: {key}")
            if time.monotonic() >= deadline:
                raise BenchmarkTimeout("BFCL exceeded its total wall-clock budget")
            logger.info("BFCL metrics context=%s metrics=%s", json.dumps(context), json.dumps(result.metrics))
            progress_cb(1, "BFCL complete")
            status = "completed"
        except BenchmarkCancelled as exc:
            canceled = exc
            parent_error = exception_details(exc)
            result.error = "BFCL canceled"
            timeout_event = getattr(cancel_event, "bfcl_timeout_event", None)
            status = "timed_out" if timeout_event is not None and timeout_event.is_set() else "canceled"
        except Exception as exc:
            parent_error = exception_details(exc)
            status = "timed_out" if isinstance(exc, BenchmarkTimeout) else "failed"
            # Do not persist arbitrary upstream messages (may include the key).
            result.error = (
                str(exc)
                if isinstance(exc, (ValueError, BenchmarkTimeout, RuntimeError))
                else f"BFCL {type(exc).__name__}"
            )
        finally:
            diagnostic = None
            if result.error:
                if runner_active:
                    diagnostic = read_error(phase_root)
                    if diagnostic.get("phase", phase) != phase:
                        diagnostic = {"code": "diagnostic_stale"}
                    if "type" in diagnostic:
                        result.error += f" ({diagnostic.get('phase', phase)}: {diagnostic['type']})"
                else:
                    diagnostic = {**(parent_error or {}), "code": "parent_failure", "phase": phase}
                if endpoint.api_key:
                    result.error = result.error.replace(endpoint.api_key, "[REDACTED]")
            snapshot = {**context, "status": status, "phase": phase,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "requests": EventSummary(phase_root).poll(), "error": result.error}
            if result.error:
                snapshot["diagnostic"] = diagnostic
                if runner_active:
                    snapshot["runner_state"] = read_runner_state(phase_root, "runner_state.json", phase)
                    snapshot["process_state"] = read_runner_state(phase_root, "process_state.json", phase)
                result.metrics["diagnostic"] = snapshot
            save_state(root / "state.json", snapshot)
            logger.log(logging.ERROR if result.error else logging.INFO,
                       "BFCL attempt finished %s", json.dumps(snapshot))
            _checkpoint(progress_cb)
            config_path.unlink(missing_ok=True)
            probe_config_path.unlink(missing_ok=True)
            selection_path.unlink(missing_ok=True)
            _write_json(
                index,
                {
                    "version": 1,
                    "attempt_id": attempt,
                    "error": result.error,
                    "files": [
                        str(p.relative_to(root))
                        for p in sorted(root.rglob("*"))
                        if p.is_file() and p != index
                    ],
                },
            )
        if canceled:
            canceled.artifacts = result.artifacts
            canceled.diagnostic = result.metrics.get("diagnostic")
            canceled.partial_result = result
            raise canceled
        return result
