"""Credential-free request telemetry and coverage-aware aggregates."""

import json
import threading
from statistics import mean

from bench.cache_usage import cache_metrics


class EventWriter:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, event):
        with self.lock, self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")


def peak_concurrency(intervals):
    boundaries = [
        boundary
        for interval in intervals
        for boundary in [
            (interval["started_monotonic"], 1),
            (interval["finished_monotonic"], -1),
        ]
    ]
    active = peak = 0
    for _, delta in sorted(boundaries):
        active += delta
        peak = max(peak, active)
    return peak


def _percentile_ms(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile / 100
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    return 1000 * (ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo))


def aggregate_metrics(events, wall_seconds=None):
    generation = [
        event for event in events if event.get("phase", "generate") == "generate"
    ]
    successes = [event for event in generation if event["status"] == "success"]
    count = len(generation)
    errors = count - len(successes)
    successful_calls = {(e.get("case_id"), e.get("request_index")) for e in successes}
    result = {
        "http_attempt_count": count,
        "request_count": count,
        "request_error_count": errors,
        "uptime": len(successes) / count if count else None,
        "retry_count": sum(e.get("http_attempt", 1) > 1 for e in generation),
        "infra_error_count": len(
            {
                e.get("case_id")
                for e in generation
                if (e.get("case_id"), e.get("request_index")) not in successful_calls
            }
        ),
        "request_throughput_rps": count / wall_seconds if wall_seconds else None,
        "successful_request_rps": len(successes) / wall_seconds
        if wall_seconds
        else None,
    }
    paired = sum(
        e.get("prompt_tokens") is not None and e.get("completion_tokens") is not None
        for e in generation
    )
    result["usage_coverage"] = paired / count if count else 0
    for name, field in [("input", "prompt_tokens"), ("output", "completion_tokens")]:
        values = [e[field] for e in successes if e.get(field) is not None]
        result[f"{name}_usage_coverage"] = (
            len(values) / len(successes) if successes else 0
        )
        result[f"{name}_tokens_per_second"] = (
            sum(values) / wall_seconds if values and wall_seconds else None
        )
        rate = result[f"{name}_tokens_per_second"]
        result[f"{name}_tpm"] = rate * 60 if rate is not None else None
        result[f"{field}_total"] = (
            sum(values) if values and len(values) == count else None
        )
    result["total_tpm"] = (
        result["input_tpm"] + result["output_tpm"]
        if result["input_tpm"] is not None
        and result["output_tpm"] is not None
        and result["input_usage_coverage"] == result["output_usage_coverage"] == 1
        else None
    )
    request_rates = []
    for event in successes:
        tokens = event.get("completion_tokens")
        latency = event.get("latency_seconds")
        if tokens is not None and latency is not None and latency > 0:
            request_rates.append(tokens / latency)
    result["request_output_tps"] = (
        mean(request_rates) if successes and len(request_rates) == len(successes) else None
    )
    ttft = [e["ttft_seconds"] for e in successes if e.get("ttft_seconds") is not None]
    e2e = [
        e["latency_seconds"] for e in successes if e.get("latency_seconds") is not None
    ]
    for percentile in (50, 90, 99):
        result[f"ttft_p{percentile}_ms"] = _percentile_ms(ttft, percentile)
    result["ttft_mean_ms"] = 1000 * mean(ttft) if ttft else None
    result["ttft_sample_count"] = len(ttft)
    result["ttft_coverage"] = len(ttft) / len(successes) if successes else 0
    result["request_e2e_p90_ms"] = _percentile_ms(e2e, 90)
    for suffix, group in [
        ("", successes),
        ("_first", [e for e in successes if e.get("request_index") == 0]),
        ("_subsequent", [e for e in successes if e.get("request_index", 0) > 0]),
    ]:
        paired_usage = cache_metrics(
            [
                {
                    "prompt_tokens": e.get("prompt_tokens"),
                    "cached_tokens": e.get("cached_input_tokens"),
                }
                for e in group
            ]
        )
        result.update({key + suffix: value for key, value in paired_usage.items()})
    return result
