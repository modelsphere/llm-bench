#!/usr/bin/env python3
"""
Standalone vLLM endpoint benchmark — no Docker, no vLLM package required.

Replicates `vllm bench serve` for OpenAI-compatible endpoints:
  - Generates random prompts of fixed token length
  - Closed-loop async concurrency
  - Collects TTFT, total latency, throughput, token counts

Example:
    python scripts/vllm_bench/vllm_bench.py \
        --base-url https://gateway.example.com \
        --model Kimi-K2.5 \
        --api-key sk-... \
        --input-len 50 \
        --output-len 64000 \
        --num-prompts 200 \
        --max-concurrency 100 \
        --endpoint /v1/completions
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import deque
from typing import Any, Dict, List, Optional

import aiohttp

# Approximate tokenizer heuristic: ~4 chars per token for English text
CHARS_PER_TOKEN = 4


def _generate_prompt(num_tokens: int) -> str:
    """Generate a random prompt approximating num_tokens tokens."""
    num_chars = max(1, num_tokens * CHARS_PER_TOKEN)
    words = []
    while sum(len(w) for w in words) < num_chars:
        words.append(str(random.randint(1000, 9999)))
    return " ".join(words)


async def _send_one(
    session: aiohttp.ClientSession,
    url: str,
    payload: Dict[str, Any],
    timeout: aiohttp.ClientTimeout,
) -> Dict[str, Any]:
    """Send one request and return per-request stats."""
    t0 = time.monotonic()
    ttft: Optional[float] = None
    completion_tokens = 0
    prompt_tokens = 0
    chunks = 0

    try:
        async with session.post(url, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            async for raw in resp.content:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    continue

                usage = data.get("usage")
                if usage:
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)

                for choice in data.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        if ttft is None:
                            ttft = time.monotonic() - t0
                        chunks += 1
                        if not usage:
                            completion_tokens += 1

        total_time = time.monotonic() - t0
        return {
            "success": True,
            "ttft": ttft,
            "total_time": total_time,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "chunks": chunks,
        }
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


async def _worker(
    worker_id: int,
    session: aiohttp.ClientSession,
    url: str,
    payload_template: Dict[str, Any],
    prompts: deque[str],
    results: List[Dict[str, Any]],
    timeout: aiohttp.ClientTimeout,
    cancel_event: asyncio.Event,
) -> None:
    """Worker loop: keep pulling prompts until exhausted."""
    while True:
        if cancel_event.is_set():
            break
        try:
            prompt = prompts.popleft()
        except IndexError:
            break
        payload = {**payload_template, "prompt": prompt}
        result = await _send_one(session, url, payload, timeout)
        results.append(result)


async def _progress_reporter(
    results: List[Dict[str, Any]],
    num_prompts: int,
    interval: float = 5.0,
) -> None:
    """Print progress every N seconds."""
    t0 = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - t0
        ok = sum(1 for r in results if r.get("success"))
        err = len(results) - ok
        print(
            f"  t={elapsed:.0f}s  completed={len(results)}/{num_prompts} "
            f"ok={ok} err={err}",
            flush=True,
        )
        if len(results) >= num_prompts:
            break


def _percentile(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def _mean(vals: List[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _aggregate(
    results: List[Dict[str, Any]],
    elapsed_total: float,
) -> Dict[str, Any]:
    """Aggregate per-request results into summary metrics."""
    successful = [r for r in results if r.get("success")]
    total = len(results)
    n = len(successful)

    uptime = n / total if total > 0 else 0.0

    ttfts = [r["ttft"] * 1000 for r in successful if r.get("ttft") is not None]
    total_times = [r["total_time"] * 1000 for r in successful]
    prompt_toks = [r["prompt_tokens"] for r in successful if r.get("prompt_tokens")]
    completion_toks = [r["completion_tokens"] for r in successful if r.get("completion_tokens")]

    # throughput over elapsed wall-clock time
    input_tps = sum(prompt_toks) / elapsed_total if elapsed_total > 0 else 0.0
    output_tps = sum(completion_toks) / elapsed_total if elapsed_total > 0 else 0.0

    # TPOT = total_time / completion_tokens
    tpots = [
        r["total_time"] / r["completion_tokens"] * 1000
        for r in successful
        if r.get("completion_tokens", 0) > 0
    ]

    # ITL = (total_time - ttft) / (completion_tokens - 1)
    itls = [
        (r["total_time"] - r["ttft"]) / (r["completion_tokens"] - 1) * 1000
        for r in successful
        if r.get("completion_tokens", 0) > 1 and r.get("ttft") is not None
    ]

    return {
        "total_requests": total,
        "successful_requests": n,
        "failed_requests": total - n,
        "uptime": uptime,
        "ttft_p50_ms": _percentile(ttfts, 0.50),
        "ttft_p99_ms": _percentile(ttfts, 0.99),
        "ttft_mean_ms": _mean(ttfts),
        "total_time_p50_ms": _percentile(total_times, 0.50),
        "total_time_p99_ms": _percentile(total_times, 0.99),
        "total_time_mean_ms": _mean(total_times),
        "tpot_p50_ms": _percentile(tpots, 0.50),
        "tpot_p99_ms": _percentile(tpots, 0.99),
        "tpot_mean_ms": _mean(tpots),
        "itl_p50_ms": _percentile(itls, 0.50),
        "itl_p99_ms": _percentile(itls, 0.99),
        "itl_mean_ms": _mean(itls),
        "input_tps": input_tps,
        "output_tps": output_tps,
        "total_prompt_tokens": sum(prompt_toks),
        "total_completion_tokens": sum(completion_toks),
        "elapsed_total_s": elapsed_total,
    }


async def _run_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    """Main async benchmark loop."""
    base = args.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = base + args.endpoint

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    # Pre-generate all prompts
    prompts: deque[str] = deque(
        _generate_prompt(args.input_len) for _ in range(args.num_prompts)
    )

    payload_template: Dict[str, Any] = {
        "model": args.model,
        "max_tokens": args.output_len,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if args.ignore_eos:
        payload_template["ignore_eos"] = True

    timeout = aiohttp.ClientTimeout(total=args.request_timeout, connect=10)
    connector = aiohttp.TCPConnector(limit=args.max_concurrency + 4)
    results: List[Dict[str, Any]] = []
    cancel_event = asyncio.Event()

    print(
        f"Benchmarking {args.model} @ {url}\n"
        f"  prompts={args.num_prompts} concurrency={args.max_concurrency}\n"
        f"  input_len={args.input_len} output_len={args.output_len}\n"
        f"  ignore_eos={args.ignore_eos}\n",
        flush=True,
    )

    t_start = time.monotonic()
    async with aiohttp.ClientSession(
        connector=connector, headers=headers, timeout=timeout
    ) as session:
        workers = [
            asyncio.create_task(
                _worker(
                    i, session, url, payload_template, prompts,
                    results, timeout, cancel_event,
                )
            )
            for i in range(args.max_concurrency)
        ]
        reporter = asyncio.create_task(
            _progress_reporter(results, args.num_prompts, args.progress_interval)
        )
        await asyncio.gather(*workers, return_exceptions=True)
        reporter.cancel()

    elapsed = time.monotonic() - t_start
    return _aggregate(results, elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone vLLM endpoint benchmark (no Docker required)",
    )
    parser.add_argument("--base-url", required=True, help="Base URL of the API endpoint")
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument("--api-key", default="", help="API key (Bearer token)")
    parser.add_argument("--endpoint", default="/v1/completions", help="API endpoint path")
    parser.add_argument("--input-len", type=int, default=50, help="Input prompt length in tokens")
    parser.add_argument("--output-len", type=int, default=64000, help="Max output tokens per request")
    parser.add_argument("--num-prompts", type=int, default=200, help="Total number of prompts to send")
    parser.add_argument("--max-concurrency", type=int, default=100, help="Max concurrent requests")
    parser.add_argument("--request-timeout", type=float, default=600.0, help="Per-request HTTP timeout (s)")
    parser.add_argument("--progress-interval", type=float, default=5.0, help="Progress print interval (s)")
    parser.add_argument("--ignore-eos", action="store_true", help="Pass ignore_eos flag in payload")
    parser.add_argument("--output", help="Optional JSON file to write results")
    args = parser.parse_args()

    try:
        metrics = asyncio.run(_run_benchmark(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Benchmark Results")
    print("=" * 60)
    for key, val in metrics.items():
        if isinstance(val, float):
            print(f"  {key}: {val:.3f}")
        else:
            print(f"  {key}: {val}")

    if args.output:
        import pathlib
        pathlib.Path(args.output).write_text(json.dumps(metrics, indent=2))
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
