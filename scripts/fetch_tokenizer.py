#!/usr/bin/env python3
"""Download a tokenizer from the Hugging Face hub into a plain directory.

The throughput modules size their synthetic prompts with a tokenizer. The
backend image carries one (Qwen3-0.6B's, fetched by this script at build time)
so nothing is downloaded when a benchmark runs. Use it yourself to prepare
another for PROCESSOR_PATH or a module's `processor_path`, e.g. your model's
own, on a machine that can reach the hub:

    python3 scripts/fetch_tokenizer.py --repo Qwen/Qwen3-0.6B --out dataset/tokenizers/qwen3-0.6b

Stdlib only. HF_ENDPOINT names a mirror; HF_TOKEN is sent for gated repos.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

# What a tokenizer directory needs for transformers' AutoTokenizer; files a
# repo does not have are skipped (tokenizer.json alone is enough for most).
FILES = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
         "special_tokens_map.json", "tokenizer.model", "config.json", "LICENSE")


def fetch(url: str, dest: str, attempts: int = 5) -> bool:
    """True when downloaded, False when the repo has no such file."""
    headers = {"User-Agent": "llm-bench-fetch-tokenizer"}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as resp, open(dest + ".part", "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
            os.replace(dest + ".part", dest)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            error: Exception = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = exc
        if attempt == attempts:
            raise SystemExit(f"could not download {url}: {error}")
        time.sleep(2 * attempt)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default="Qwen/Qwen3-0.6B")
    parser.add_argument("--revision", default="main", help="a branch, tag or commit")
    parser.add_argument("--out", required=True, help="directory to write the files into")
    args = parser.parse_args()

    base = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    os.makedirs(args.out, exist_ok=True)
    got = [name for name in FILES
           if fetch(f"{base}/{args.repo}/resolve/{args.revision}/{name}", os.path.join(args.out, name))]
    if "tokenizer.json" not in got and "tokenizer.model" not in got and "vocab.json" not in got:
        sys.exit(f"{args.repo}@{args.revision} has no tokenizer files")
    print(f"{args.repo}@{args.revision} -> {args.out}: {', '.join(got)}")


if __name__ == "__main__":
    main()
