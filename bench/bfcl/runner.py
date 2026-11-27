"""Isolated entry point for the pinned official BFCL generator and evaluator."""

import argparse
import contextvars
import json
import os
import time
from importlib.util import find_spec
from pathlib import Path
from statistics import StatisticsError

from bench.bfcl.diagnostics import DiagnosticError, exception_details, save_state
from bench.bfcl.smoke import category_output, check_generated_cases, installed_revision
from bench.bfcl.streaming import assemble_stream
from bench.bfcl.telemetry import EventWriter, aggregate_metrics, peak_concurrency
from bench.bfcl.trace import TraceWriter
from utils.api import join_endpoint, to_base_url

REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
REGISTRY_KEY = "llmbench-bfcl-streaming"
_case = contextvars.ContextVar("bfcl_case")


class RequestFailed(RuntimeError):
    """A request with an HTTP, transport, or incomplete-stream failure."""


def score_results(root, cases):
    """The pinned evaluator records only failures after its aggregate header."""
    results = []
    for category, expected in cases.items():
        rows = [
            json.loads(line)
            for line in category_output(root, category, "score")
            .read_text()
            .splitlines()
            if line.strip()
        ]
        header, failures = rows[0], rows[1:]
        ids = [row["id"] for row in failures]
        if (
            header["total_count"] != len(expected)
            or len(ids) != len(set(ids))
            or not set(ids).issubset(expected)
            or any(row.get("valid") is not False for row in failures)
            or header["correct_count"] != len(expected) - len(ids)
        ):
            raise ValueError(f"Unexpected scoring coverage for {category}")
        for case_id in expected:
            results.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "success": case_id not in ids,
                }
            )
    return results


def message_field(message, name):
    return (
        message.get(name) if isinstance(message, dict) else getattr(message, name, None)
    )


def write_probe_report(root, case_id):
    path = root / "request_events.jsonl"
    rows = (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if path.exists()
        else []
    )
    successful = [row for row in rows if row["status"] == "success"]
    usage_count = sum(
        row["prompt_tokens"] is not None and row["completion_tokens"] is not None
        for row in successful
    )
    report = {
        "case_id": case_id,
        "tool_call_observed": any(row.get("tool_call_observed") for row in successful),
        "tool_result_roundtrip_observed": any(
            row.get("tool_result_roundtrip_observed") for row in successful
        ),
        "stream_usage_observed": usage_count > 0,
        "usage_sample_count": usage_count,
        "successful_requests": len(successful),
        "http_attempt_count": len(rows),
    }
    (root / "probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def handler_class(base, config, root, *, phase="generate"):
    """Override only the official inference boundary and transport call."""
    import httpx
    from openai import APIError
    from openai.types.chat import ChatCompletion

    events = EventWriter(root / "request_events.jsonl")
    case_events = EventWriter(root / "case_events.jsonl")
    trace = (
        TraceWriter(
            root,
            config["manifest"].get("protocol_hash"),
            os.environ.get("BFCL_ENDPOINT_API_KEY", ""),
        )
        if config.get("export_trace")
        else None
    )
    endpoint = to_base_url(join_endpoint(config["endpoint"], "chat/completions"))

    class StreamingHandler(base):
        def _build_client_kwargs(self):
            key = os.environ.get("BFCL_ENDPOINT_API_KEY")

            def remove_auth(request):
                if not key:
                    request.headers.pop("Authorization", None)

            return {
                "base_url": endpoint,
                "api_key": key or "unused",
                "max_retries": 0,
                "timeout": config["timeout"],
                "http_client": httpx.Client(
                    timeout=config["timeout"], event_hooks={"request": [remove_auth]}
                ),
            }

        def inference(self, test_entry, include_input_log, exclude_state_log):
            context = {"case_id": test_entry["id"], "request_index": 0}
            if phase == "probe":
                context["observed_tool_ids"] = set()
            token = _case.set(context)
            started = time.monotonic()
            status = "inference_error"
            diagnostic = {}
            try:
                response = super().inference(
                    test_entry, include_input_log, exclude_state_log
                )
                status = "generated"
                return response
            except Exception as exc:
                if isinstance(exc, RequestFailed):
                    status = "request_failed"
                diagnostic = exception_details(exc)
                raise
            finally:
                _case.reset(token)
                case_events.write(
                    {
                        **config.get("identity", {}),
                        "case_id": context["case_id"],
                        "status": status,
                        **diagnostic,
                        "request_count": context["request_index"],
                        "started_monotonic": started,
                        "finished_monotonic": time.monotonic(),
                    }
                )

        def generate_with_backoff(self, **kwargs):
            # SDK retries stay disabled. A partial stream is never retried.
            context = _case.get()
            identity = {
                **config.get("identity", {}),
                "case_id": context["case_id"],
                "request_index": context["request_index"],
                "phase": phase,
            }
            context["request_index"] += 1
            body = {**kwargs, "stream": True, "stream_options": {"include_usage": True}}
            if trace:
                trace.write(body, identity)
            deadline = time.monotonic() + config["timeout"]
            max_attempts = 1 + config.get("request_retries", 0)
            for attempt in range(1, max_attempts + 1):
                started = time.monotonic()
                event = {
                    **identity,
                    "http_attempt": attempt,
                    "started_monotonic": started,
                    "status": "error",
                    "ttft_seconds": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "cached_input_tokens": None,
                }
                stream = None
                try:
                    stream = self.client.chat.completions.create(
                        **body, timeout=max(0.001, deadline - started)
                    )
                    response, metrics = assemble_stream(
                        stream, started=started, deadline=deadline
                    )
                    event.update(metrics)
                    completion = ChatCompletion.model_validate(response)
                    if phase == "probe":
                        observed_ids = context["observed_tool_ids"]
                        event["tool_result_roundtrip_observed"] = any(
                            message_field(message, "role") == "tool"
                            and message_field(message, "tool_call_id") in observed_ids
                            for message in body["messages"]
                        )
                        calls = completion.choices[0].message.tool_calls or []
                        tool_ids = {
                            call.id
                            for call in calls
                            if call.type == "function" and call.id
                        }
                        event["tool_call_observed"] = bool(tool_ids)
                        observed_ids.update(tool_ids)
                    event.update(status="success", http_status=200)
                    return completion, metrics["latency_seconds"]
                except Exception as exc:
                    status = getattr(exc, "status_code", None)
                    event["error_type"] = type(exc).__name__
                    event.update(exception_details(exc))
                    event["http_status"] = (
                        status if isinstance(status, int) else (200 if stream else None)
                    )
                    delay = 0.5 * 2 ** (attempt - 1)
                    retryable = status == 429 or (
                        isinstance(status, int) and 500 <= status <= 599
                    )
                    if (
                        stream is not None
                        or not retryable
                        or attempt == max_attempts
                        or time.monotonic() + delay >= deadline
                    ):
                        request_failure = (
                            isinstance(exc, (APIError, httpx.HTTPError, TimeoutError))
                            or (isinstance(exc, DiagnosticError)
                                and exc.code == "completion_stream_incomplete")
                        )
                        error_type = RequestFailed if request_failure else RuntimeError
                        raise error_type(
                            f"BFCL request failed: {type(exc).__name__}"
                        ) from None
                finally:
                    event["finished_monotonic"] = time.monotonic()
                    event["latency_seconds"] = event["finished_monotonic"] - started
                    events.write(event)
                    if stream is not None:
                        stream.close()
                time.sleep(delay)

    return StreamingHandler


def read_generation_events(root, cases, *, allow_request_failures):
    events = [
        json.loads(line)
        for line in (root / "request_events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    case_events = [
        json.loads(line)
        for line in (root / "case_events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    expected = {case_id for ids in cases.values() for case_id in ids}
    timing = {event["case_id"]: event for event in case_events}
    if set(timing) != expected or len(timing) != len(case_events):
        raise ValueError("BFCL case telemetry coverage mismatch")
    request_keys = {(event["case_id"], event["request_index"]) for event in events}
    successful = {
        (event["case_id"], event["request_index"])
        for event in events if event["status"] == "success"
    }
    failed_cases = {case_id for case_id, _ in request_keys - successful}
    if allow_request_failures:
        recorded_failures = {
            case_id for case_id, event in timing.items() if event["status"] == "request_failed"
        }
        expected_requests = {
            (case_id, index)
            for case_id, event in timing.items() for index in range(event["request_count"])
        }
        if request_keys != expected_requests or failed_cases != recorded_failures:
            raise ValueError("BFCL request telemetry disagrees with case outcomes")
    elif failed_cases:
        raise ValueError("BFCL generation contains unsuccessful model calls")
    return events, timing, failed_cases


def select_tail_cases(root):
    path = root / "case-selection.json"
    selection = json.loads(path.read_text())
    if selection["revision"] != REVISION or installed_revision() != REVISION:
        raise DiagnosticError("Install the pinned BFCL Git revision", "installed_revision_mismatch")

    spec = find_spec("bfcl_eval")
    if spec is None or spec.origin is None:
        raise DiagnosticError("Pinned BFCL dataset is unavailable", "dataset_unavailable")
    data = Path(spec.origin).parent / "data"
    count = selection["count"]
    cases = {}
    for category in selection["categories"]:
        with (data / f"BFCL_v4_{category}.json").open(encoding="utf-8") as dataset:
            ids = [json.loads(line)["id"] for line in dataset if line.strip()]
        if len(ids) < count:
            raise DiagnosticError(
                f"BFCL category {category} has {len(ids)} cases, requested {count}",
                "insufficient_cases", category=category,
            )
        cases[category] = ids[-count:]
    path.write_text(json.dumps(cases), encoding="utf-8")


def run_phase(phase, root):
    def stage(name):
        save_state(root / "runner_state.json", {"phase": phase, "stage": name})

    stage("setup")
    if phase == "select":
        select_tail_cases(root)
        return
    config = json.loads((root / "config.json").read_text())
    manifest = config["manifest"]
    allow_request_failures = manifest.get("allow_request_failures", False)
    if manifest["revision"] != REVISION:
        raise DiagnosticError("BFCL manifest revision does not match pinned runner", "manifest_revision_mismatch")
    if installed_revision() != REVISION:
        raise DiagnosticError("Install the pinned BFCL Git revision", "installed_revision_mismatch")
    if not 0 < config["timeout"] <= 3600:
        raise DiagnosticError("Request timeout must be within (0, 3600] seconds", "invalid_request_timeout")
    # Must precede every import from bfcl_eval; CLI invokes each phase separately.
    os.environ["BFCL_PROJECT_ROOT"] = str(root)
    from bfcl_eval.__main__ import cli
    from bfcl_eval.constants.model_config import MODEL_CONFIG_MAPPING, ModelConfig
    from bfcl_eval.model_handler.api_inference.openai_completion import (
        OpenAICompletionsHandler,
    )

    MODEL_CONFIG_MAPPING[REGISTRY_KEY] = ModelConfig(
        model_name=config["model"],
        display_name=REGISTRY_KEY,
        url="",
        org="",
        license="",
        model_handler=handler_class(
            OpenAICompletionsHandler, config, root, phase=phase
        ),
        is_fc_model=True,
        # Match the dot-to-underscore conversion in OpenAI tool schemas.
        underscore_to_dot=True,
    )
    if phase in {"probe", "generate"}:
        (root / "test_case_ids_to_generate.json").write_text(
            json.dumps(manifest["cases"])
        )
        stage("generate")
        started = time.monotonic()
        try:
            cli(
                [
                    "generate",
                    "--model",
                    REGISTRY_KEY,
                    "--run-ids",
                    "--num-threads",
                    str(config["concurrency"]),
                    "--temperature",
                    str(manifest["temperature"]),
                ],
                standalone_mode=False,
            )
        finally:
            if phase == "probe":
                write_probe_report(root, manifest["cases"]["multi_turn_base"][0])
        wall_seconds = time.monotonic() - started
        stage("validate_generated_cases")
        failed_cases = set()
        if phase == "generate" and allow_request_failures:
            _, _, failed_cases = read_generation_events(
                root, manifest["cases"], allow_request_failures=True
            )
        check_generated_cases(
            root / "result" / REGISTRY_KEY, manifest["cases"], request_failed_cases=failed_cases
        )
        if phase == "probe":
            return
        (root / "generation.json").write_text(
            json.dumps({"wall_seconds": wall_seconds})
        )
    else:
        stage("validate_generated_cases")
        events, case_timing, failed_cases = read_generation_events(
            root, manifest["cases"], allow_request_failures=allow_request_failures
        )
        check_generated_cases(
            root / "result" / REGISTRY_KEY, manifest["cases"], request_failed_cases=failed_cases
        )
        stage("evaluate")
        started = time.monotonic()
        try:
            cli(
                [
                    "evaluate",
                    "--model",
                    REGISTRY_KEY,
                    "--partial-eval",
                    "--test-category",
                    ",".join(manifest["cases"]),
                ],
                standalone_mode=False,
            )
        except StatisticsError as exc:
            # The pinned CLI writes case scores before its leaderboard CSV,
            # whose latency standard deviation fails with only one sample.
            frames = [
                (frame["file"], frame["function"]) for frame in exception_details(exc)["frames"]
            ]
            if not failed_cases or frames[-2:] != [
                ("eval_runner_helper.py", "get_cost_latency_info"), ("statistics.py", "stdev")
            ]:
                raise
        stage("validate_scores")
        cases = score_results(root / "score" / REGISTRY_KEY, manifest["cases"])
        grading_seconds = time.monotonic() - started
        generation_seconds = json.loads((root / "generation.json").read_text())[
            "wall_seconds"
        ]
        with (root / "case_results.jsonl").open("w") as output:
            for case in cases:
                timing = case_timing[case["case_id"]]
                if case["case_id"] in failed_cases:
                    # BFCL can accept an inference error as no tool call in irrelevance cases.
                    case["success"] = False
                    case["request_failed"] = True
                case["request_count"] = timing["request_count"]
                case["wall_seconds"] = (
                    timing["finished_monotonic"] - timing["started_monotonic"]
                )
                output.write(json.dumps(case) + "\n")
        passed = sum(case["success"] for case in cases)
        metrics = aggregate_metrics(events, generation_seconds)
        metrics.update(
            active_cases_peak=peak_concurrency(case_timing.values()),
            active_http_peak=peak_concurrency(events),
            case_count=len(cases),
            case_success_count=passed,
            accuracy=passed / len(cases),
            generation_wall_seconds=generation_seconds,
            grading_wall_seconds=grading_seconds,
            passed_cases_per_hour=passed * 3600 / generation_seconds,
        )
        for category, expected in manifest["cases"].items():
            metrics[f"case_count_{category}"] = len(expected)
            metrics[f"accuracy_{category}"] = sum(
                case["success"] for case in cases if case["category"] == category
            ) / len(expected)
        if config.get("export_trace"):
            import hashlib

            trace_path = root / "trace.jsonl"
            metrics["trace_sha256"] = hashlib.sha256(
                trace_path.read_bytes()
            ).hexdigest()
        (root / "summary.json").write_text(
            json.dumps(
                {
                    "metrics": metrics,
                    "identity": config.get("identity", {}),
                    "manifest": manifest,
                },
                indent=2,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=["select", "probe", "generate", "evaluate"], required=True
    )
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        run_phase(args.phase, root)
    except Exception as exc:
        stage = "unknown"
        try:
            stage = json.loads((root / "runner_state.json").read_text()).get("stage", "unknown")
        except (OSError, ValueError, AttributeError):
            pass
        save_state(root / "runner_error.json", {
            "phase": args.phase, "stage": stage, **exception_details(exc)})
        raise


if __name__ == "__main__":
    main()
