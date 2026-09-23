#!/usr/bin/env python3
"""Download the public academic benchmarks the `opencompass` module scores.

Writes the exact files, under the exact paths, that
bench/tests/functional/opencompass.py opens — into ACADEMIC_DATA_DIR
(default: dataset/opencompass/data) — and checks each one's row count.

    uv run --project platform/backend python scripts/fetch_academic_datasets.py
    uv run --project platform/backend python scripts/fetch_academic_datasets.py --only mmlu_pro simpleqa

Two of the sets are gated on Hugging Face: GPQA (Idavidrein/gpqa) and HLE
(cais/hle). Accept their terms on the dataset page while signed in, then pass
a token with HF_TOKEN (or `huggingface-cli login`). Without one, those two are
skipped with a message and the rest still download. At run time a suite whose
data is missing is recorded as an error in that run's details and the other
suites still run; turn it off in the benchmark's module params to silence it.

Everything lands in the Hugging Face cache first (HF_HOME) and is then copied
into place, so re-running is cheap and an interrupted download resumes.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
ATTEMPTS = 3


@dataclass(frozen=True)
class Dataset:
    name: str
    repo: str
    files: tuple[str, ...]
    target: str                 # path under the data dir the loader opens
    rows: int | None            # expected row count, None = report only
    gated: bool = False
    build: Callable[[list[Path], Path], None] | None = None   # None = copy the single file


def _concat_jsonl(sources: list[Path], dest: Path) -> None:
    with dest.open("w", encoding="utf-8") as out:
        for src in sources:
            for line in src.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.write(line.strip() + "\n")


def _json_array_to_jsonl(sources: list[Path], dest: Path) -> None:
    rows = json.loads(sources[0].read_text(encoding="utf-8"))
    with dest.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


DATASETS = (
    Dataset("aime2025", "opencompass/AIME2025", ("aime2025-I.jsonl", "aime2025-II.jsonl"),
            "aime2025/aime2025.jsonl", 30, build=_concat_jsonl),
    Dataset("gpqa", "Idavidrein/gpqa", ("gpqa_diamond.csv",),
            "gpqa/gpqa_diamond.csv", 198, gated=True),
    Dataset("ifeval", "google/IFEval", ("ifeval_input_data.jsonl",),
            "ifeval/input_data.jsonl", 541),
    Dataset("mmlu_pro", "TIGER-Lab/MMLU-Pro", ("data/test-00000-of-00001.parquet",),
            "mmlu_pro/test-00000-of-00001.parquet", 12032),
    Dataset("hle", "cais/hle", ("data/test-00000-of-00001.parquet",),
            "cais/hle/data/test-00000-of-00001.parquet", None, gated=True),
    Dataset("livecodebench", "livecodebench/code_generation_lite", ("test2.jsonl",),
            "code_generation_lite/test2.jsonl", None),
    Dataset("simpleqa", "basicv8vc/SimpleQA", ("simple_qa_test_set.csv",),
            "simpleqa/simple_qa_test_set.csv", 4326),
    Dataset("longbench_v2", "THUDM/LongBench-v2", ("data.json",),
            "longbench_v2/longbench_v2.jsonl", 503, build=_json_array_to_jsonl),
)


def count_rows(path: Path) -> int:
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return pq.ParquetFile(path).metadata.num_rows
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def fetch(ds: Dataset, data_dir: Path, token: str | None) -> str:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import GatedRepoError, HfHubHTTPError

    sources: list[Path] = []
    for attempt in range(1, ATTEMPTS + 1):
        try:
            sources = [Path(hf_hub_download(ds.repo, f, repo_type="dataset", token=token))
                       for f in ds.files]
            break
        except GatedRepoError:
            return (f"SKIP  {ds.name}: gated — accept the terms at "
                    f"https://huggingface.co/datasets/{ds.repo} and set HF_TOKEN")
        except (HfHubHTTPError, OSError, RuntimeError) as exc:
            # Transient network trouble surfaces as any of these (the hub
            # client can even report itself closed after a dropped connection).
            # Downloads resume from the cache, so a retry costs only what failed.
            if attempt == ATTEMPTS:
                return f"FAIL  {ds.name}: {type(exc).__name__}: {exc}"
            time.sleep(2 * attempt)

    dest = data_dir / ds.target
    dest.parent.mkdir(parents=True, exist_ok=True)
    if ds.build is None:
        shutil.copyfile(sources[0], dest)
    else:
        ds.build(sources, dest)

    n = count_rows(dest)
    if ds.rows is not None and n != ds.rows:
        return (f"WARN  {ds.name}: {n} rows, expected {ds.rows} — the upstream set "
                f"changed; scores are not comparable with runs on the old one")
    return f"OK    {ds.name}: {n} rows -> {dest.relative_to(data_dir)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.getenv("ACADEMIC_DATA_DIR") or REPO_ROOT / "dataset/opencompass/data"))
    ap.add_argument("--only", nargs="*", choices=[d.name for d in DATASETS],
                    help="fetch just these (default: all)")
    args = ap.parse_args()

    token = os.getenv("HF_TOKEN") or None
    wanted = [d for d in DATASETS if not args.only or d.name in args.only]
    print(f"into {args.data_dir}  (HF token: {'yes' if token else 'no — gated sets will be skipped'})")
    results = [fetch(d, args.data_dir, token) for d in wanted]
    for line in results:
        print(line)
    return 1 if any(r.startswith("FAIL") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
