#!/usr/bin/env python3
"""
Test whether a service can produce 64k tokens of output.

Sends a single streaming request with max_tokens=65536 and a prompt that
instructs the model to generate a very long response.  Reports actual token
count, time-to-first-token, and tokens/sec.

Usage:
    python scripts/test_long_output.py
    python scripts/test_long_output.py --config submit_example_local_2.yaml
    python scripts/test_long_output.py --config submit_example_local_2.yaml --max-tokens 65536
"""
import argparse
import json
import sys
import time
from pathlib import Path

import yaml

try:
    import httpx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "httpx"])
    import httpx

TARGET_TOKENS = 65536

PROMPT = (
    "Please write an extremely long, detailed, and comprehensive essay. "
    "Fill the response with as many tokens as possible — do not stop early. "
    "Keep writing until you have produced at least 64,000 tokens of content. "
    "You may write about any topic: history, science, literature, philosophy, "
    "mathematics, or anything else. The goal is maximum output length."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test 64k token output capability")
    parser.add_argument(
        "--config", default="submit_example_local_2.yaml",
        help="Path to submit config YAML (default: submit_example_local_2.yaml)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=TARGET_TOKENS,
        help=f"max_tokens to request (default: {TARGET_TOKENS})",
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0,
        help="HTTP read timeout in seconds (default: 600)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        # resolve relative to repo root (parent of scripts/)
        config_path = Path(__file__).parent.parent / config_path
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    api_url: str = cfg["api_url"].rstrip("/")
    model: str = cfg.get("model", "llm")
    api_key: str = cfg.get("api_key", "")

    url = api_url + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": args.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    print(f"Endpoint : {url}")
    print(f"Model    : {model}")
    print(f"max_tokens: {args.max_tokens:,}")
    print(f"Timeout  : {args.timeout}s")
    print()
    print("Streaming response... (press Ctrl-C to abort)")
    print("-" * 60)

    t_start = time.monotonic()
    ttft: float | None = None
    chunk_count = 0
    output_tokens = 0
    finish_reason: str | None = None

    try:
        with httpx.Client(timeout=httpx.Timeout(args.timeout, connect=10)) as client:
            with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        continue

                    # usage chunk (final)
                    usage = data.get("usage")
                    if usage and usage.get("completion_tokens"):
                        output_tokens = usage["completion_tokens"]

                    for choice in data.get("choices", []):
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
                        if choice.get("delta", {}).get("content"):
                            if ttft is None:
                                ttft = time.monotonic() - t_start
                            chunk_count += 1
                            # Progress tick every 1000 chunks
                            if chunk_count % 1000 == 0:
                                elapsed = time.monotonic() - t_start
                                est_tokens = output_tokens or chunk_count
                                tps = est_tokens / (elapsed - (ttft or 0)) if elapsed > (ttft or 0) else 0
                                print(
                                    f"  chunks={chunk_count:,}  "
                                    f"tokens≈{est_tokens:,}  "
                                    f"elapsed={elapsed:.1f}s  "
                                    f"decode_tps≈{tps:.1f}"
                                )

    except KeyboardInterrupt:
        print("\n[aborted by user]")
    except httpx.HTTPStatusError as e:
        print(f"\nHTTP error {e.response.status_code}: {e.response.text[:200]}")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)

    t_end = time.monotonic()
    elapsed = t_end - t_start

    # Fall back to chunk_count if usage field wasn't returned
    if output_tokens == 0:
        output_tokens = chunk_count

    decode_time = elapsed - (ttft or 0)
    decode_tps = output_tokens / decode_time if decode_time > 0 else 0.0

    print()
    print("=" * 60)
    print(f"TTFT              : {(ttft or 0)*1000:.0f} ms")
    print(f"Total time        : {elapsed:.1f} s")
    print(f"Output tokens     : {output_tokens:,}")
    print(f"Finish reason     : {finish_reason}")
    print(f"Decode throughput : {decode_tps:.1f} tok/s")
    print()

    target = args.max_tokens
    pct = output_tokens / target * 100 if target > 0 else 0
    if finish_reason == "length":
        print(f"RESULT: hit max_tokens limit ({output_tokens:,}/{target:,} = {pct:.1f}%) — "
              "service can sustain long output but was capped by max_tokens")
    elif output_tokens >= int(target * 0.95):
        print(f"RESULT: PASS — produced {output_tokens:,} tokens ({pct:.1f}% of {target:,})")
    else:
        print(f"RESULT: FAIL — only produced {output_tokens:,} / {target:,} tokens ({pct:.1f}%)")
        if finish_reason:
            print(f"  finish_reason={finish_reason!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
