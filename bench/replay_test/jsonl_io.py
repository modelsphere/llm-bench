"""Transparent gzip support for replay dataset files.

Replay datasets are JSON text — request bodies full of natural language — which
compresses about 3.6x (measured on a real collected build: 9.60MB → 2.64MB at
gzip level 6). Once datasets are collected continuously rather than curated by
hand, that ratio is the difference between a rolling feed fitting comfortably on
the shared datasets volume and crowding out the curated datasets already there.

Every reader goes through here, so a `.jsonl` and a `.jsonl.gz` are
interchangeable everywhere: the replay runner, the offline converter, the
analyzer, and the CLI all just open the path they were given.

**gzip, not zstd**, deliberately. gzip is in the standard library, so this works
in the bench image, the backend image, a dev box, and any standalone checkout
with nothing to install — this codebase's dependency additions have historically
been painful (pinned mirrors, offline builds). zstd would compress somewhat
better and faster, and the extension dispatch below is where it would slot in if
that ever becomes worth a new dependency.

Decompression is on the hot path twice per replay run (the sizing pass and the
streaming dispatch pass) but costs ~3.5s per pass on a 500MB dataset, against a
run measured in hours. It is not the bottleneck; the LLM is.
"""
from __future__ import annotations

import gzip
import os
from pathlib import Path
from typing import BinaryIO, TextIO

PLAIN_SUFFIX = ".jsonl"
GZIP_SUFFIX = ".jsonl.gz"
# Longest first: `.jsonl.gz` must win over `.jsonl` when stripping.
DATASET_SUFFIXES = (GZIP_SUFFIX, PLAIN_SUFFIX)

# Level 6 (the gzip default) measured 3.64x at 0.4s per 10MB; level 1 gives only
# 3.16x for half the time. Compression happens once, in a background collector
# pod; decompression — the part on every run's hot path — is level-independent.
DEFAULT_COMPRESSLEVEL = 6


def is_compressed(path: "str | os.PathLike[str]") -> bool:
    """True if this path should be read/written through gzip.

    A trailing `.part` is stripped first: builds are staged as
    `<id>.jsonl.gz.part` and published by rename, so the staging name must
    resolve to the SAME codec as its final name. Without this the builder would
    write plain text into a file that gets published as `.gz`, and every reader
    would then fail on it — which is exactly what happens if you only check the
    last suffix.
    """
    name = str(path)
    if name.endswith(".part"):
        name = name[: -len(".part")]
    return name.endswith(".gz")


def dataset_suffix(compress: bool) -> str:
    return GZIP_SUFFIX if compress else PLAIN_SUFFIX


def strip_dataset_suffix(name: str) -> str:
    """`b1.jsonl.gz` → `b1`. Note `Path.stem` is wrong here: it strips one
    suffix, leaving `b1.jsonl`, which would corrupt every build id."""
    for suffix in DATASET_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def open_text(path: "str | os.PathLike[str]", mode: str = "r", **kwargs) -> TextIO:
    """Open a dataset for line-oriented text I/O, gzip-aware by extension."""
    if is_compressed(path):
        return gzip.open(
            path, mode + "t" if "t" not in mode and "b" not in mode else mode,
            encoding=kwargs.pop("encoding", "utf-8"),
            compresslevel=kwargs.pop("compresslevel", DEFAULT_COMPRESSLEVEL),
            **kwargs,
        )
    return Path(path).open(mode, encoding=kwargs.pop("encoding", "utf-8"), **kwargs)


def open_binary(path: "str | os.PathLike[str]") -> BinaryIO:
    if is_compressed(path):
        return gzip.open(path, "rb")
    return Path(path).open("rb", buffering=1 << 20)


def count_lines(path: "str | os.PathLike[str]") -> int:
    """Count records without decoding UTF-8 or parsing JSON.

    This is the replay runner's sizing pass, which must know the record count
    before dispatching but must NOT hold the dataset in memory. Reading in
    binary keeps it a large sequential read — the access pattern a network
    filesystem handles best — and for a compressed file adds only the inflate
    cost.

    Counts NON-BLANK lines, exactly matching what `load_extract_jsonl` yields.
    That equality is load-bearing, not cosmetic: the runner pre-allocates one
    result slot per counted record and treats un-filled slots as "never
    dispatched", so a count that ran ahead of the iterator would silently
    report phantom not-started requests.
    """
    total = 0
    with open_binary(path) as handle:
        for line in handle:
            if line.strip():
                total += 1
    return total
