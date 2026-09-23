#!/usr/bin/env python3
"""Render token-distribution histograms for a replay JSONL dataset as PNGs.

Reads the original response usage recorded at capture time
(`raw_payload.resp_meta.usage` or `resp_meta.usage`, same as
analyze_replay_dataset.py) and produces three charts:

  input-length-cache-hit.png  request count by input-length bucket + per-bucket
                              token-weighted cache hit rate
  output-tokens.png           completion_tokens per request
  cache-hit-ratio.png         cached_tokens / prompt_tokens per request

Usage:
  python scripts/plot_dataset_histograms.py dataset/replay/xxx.replay.jsonl \
      --outdir docs/images [--prefix agentic-]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

INF = float("inf")

TOKEN_BUCKETS = [
    (0, 2_000, "<2K"),
    (2_000, 4_000, "2K–4K"),
    (4_000, 8_000, "4K–8K"),
    (8_000, 16_000, "8K–16K"),
    (16_000, 32_000, "16K–32K"),
    (32_000, 64_000, "32K–64K"),
    (64_000, 128_000, "64K–128K"),
    (128_000, INF, "≥128K"),
]

OUTPUT_BUCKETS = [
    (0, 64, "<64"),
    (64, 128, "64–128"),
    (128, 256, "128–256"),
    (256, 512, "256–512"),
    (512, 1_024, "512–1K"),
    (1_024, 2_048, "1K–2K"),
    (2_048, 4_096, "2K–4K"),
    (4_096, INF, "≥4K"),
]

RATIO_BUCKETS = [
    (0, 1e-9, "0%"),
    (1e-9, 0.2, "0–20%"),
    (0.2, 0.4, "20–40%"),
    (0.4, 0.6, "40–60%"),
    (0.6, 0.8, "60–80%"),
    (0.8, 0.9, "80–90%"),
    (0.9, 0.99, "90–99%"),
    (0.99, 1.01, "99–100%"),
]


def _resp_meta(rec: dict) -> dict:
    rp = rec.get("raw_payload")
    if isinstance(rp, dict) and isinstance(rp.get("resp_meta"), dict):
        return rp["resp_meta"]
    if isinstance(rec.get("resp_meta"), dict):
        return rec["resp_meta"]
    return {}


def load_usage(path: Path) -> dict[str, list[float]]:
    vals: dict = {"input": [], "output": [], "cached": [], "uncached": [], "ratio": [], "pairs": []}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = _resp_meta(rec).get("usage") or {}
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            ptd = usage.get("prompt_tokens_details") or {}
            cached = ptd.get("cached_tokens")
            if prompt is not None:
                vals["input"].append(prompt)
            if completion is not None:
                vals["output"].append(completion)
            if prompt:  # ratio needs a non-zero denominator; absent cached ⇒ full miss
                cv = cached if cached is not None else 0
                vals["cached"].append(cv)
                vals["uncached"].append(prompt - cv)
                vals["ratio"].append(cv / prompt)
                vals["pairs"].append((prompt, cv))
    return vals


def bucketize(values: list[float], buckets: list[tuple[float, float, str]]) -> tuple[list[str], list[int]]:
    labels = [b[2] for b in buckets]
    counts = [0] * len(buckets)
    for v in values:
        for i, (lo, hi, _) in enumerate(buckets):
            if lo <= v < hi:
                counts[i] += 1
                break
    return labels, counts


def _style_axes(ax, labels: list[str], title: str, xlabel: str, ymax: float,
                ylabel: str = "requests") -> None:
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=9.5)
    ax.set_title(title, fontsize=12, fontweight="bold", pad=12)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_ylim(0, ymax * 1.22 if ymax else 1)
    ax.spines[["top", "right"]].set_visible(False)
    ax.yaxis.grid(True, linestyle="--", alpha=0.35)
    ax.set_axisbelow(True)


def _annotate_bars(ax, bars, counts: list[int], total: int, fontsize: float = 8.5) -> None:
    for bar, cnt in zip(bars, counts):
        ax.annotate(
            f"{cnt}\n{cnt / total * 100:.1f}%",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center",
            va="bottom",
            fontsize=fontsize,
            color="#333333",
            linespacing=1.3,
        )


def plot_hist(
    labels: list[str],
    counts: list[int],
    title: str,
    xlabel: str,
    out: Path,
    color: str,
    mean_text: str,
) -> None:
    total = sum(counts) or 1
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=160)
    bars = ax.bar(range(len(labels)), counts, color=color, edgecolor="white", width=0.72)
    _annotate_bars(ax, bars, counts, total)
    _style_axes(ax, labels, title, xlabel, max(counts) if counts else 0)
    ax.text(
        0.99, 0.97, mean_text,
        transform=ax.transAxes, ha="right", va="top", fontsize=10, color="#333333",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="#f5f5f5", edgecolor="#cccccc"),
    )
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")


def plot_hist_with_rate(
    labels: list[str],
    counts: list[int],
    rates: list[float | None],  # per-bucket token-weighted cache hit rate (0–1), None if empty
    title: str,
    xlabel: str,
    out: Path,
    mean_text: str,
) -> None:
    total = sum(counts) or 1
    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(9, 6.6), dpi=160, sharex=True,
        gridspec_kw={"height_ratios": [3, 2], "hspace": 0.12},
    )
    bars = ax.bar(range(len(labels)), counts, color="#4C72B0", edgecolor="white", width=0.72)
    _annotate_bars(ax, bars, counts, total)
    _style_axes(ax, labels, title, "", max(counts) if counts else 0)
    ax.tick_params(axis="x", labelbottom=False)
    ax.text(
        0.99, 0.95, mean_text,
        transform=ax.transAxes, ha="right", va="top", fontsize=9.5, color="#333333",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="#f5f5f5", edgecolor="#cccccc"),
    )

    xs = [i for i, r in enumerate(rates) if r is not None]
    ys = [rates[i] * 100 for i in xs]
    rate_bars = ax2.bar(xs, ys, color="#55A868", edgecolor="white", width=0.72)
    for bar, y in zip(rate_bars, ys):
        ax2.annotate(f"{y:.0f}%", (bar.get_x() + bar.get_width() / 2, y),
                     ha="center", va="bottom", fontsize=8.5, color="#2e7d4f",
                     fontweight="bold")
    _style_axes(ax2, labels, "", xlabel, 100, ylabel="cache hit rate (%)\n(token-weighted)")
    ax2.set_ylim(0, 112)
    ax2.set_yticks(range(0, 101, 25))

    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Plot token histograms for a replay JSONL dataset")
    ap.add_argument("input", type=Path, help="Replay JSONL (converted bodylog or raw bodylog)")
    ap.add_argument("--outdir", type=Path, default=Path("docs/images"), help="Output directory (default docs/images)")
    ap.add_argument("--prefix", default="", help="Filename prefix for the generated PNGs")
    args = ap.parse_args()

    vals = load_usage(args.input)
    if not vals["input"]:
        raise SystemExit("no usage records found — is this a replay JSONL with resp_meta.usage?")
    args.outdir.mkdir(parents=True, exist_ok=True)

    n = len(vals["input"])
    print(f"{args.input.name}: {n} requests with usage")

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    labels, in_counts = bucketize(vals["input"], TOKEN_BUCKETS)
    # per-bucket token-weighted hit rate: Σcached / Σprompt over the bucket's requests
    bucket_prompt = [0] * len(TOKEN_BUCKETS)
    bucket_cached = [0] * len(TOKEN_BUCKETS)
    for prompt, cached in vals["pairs"]:
        for i, (lo, hi, _) in enumerate(TOKEN_BUCKETS):
            if lo <= prompt < hi:
                bucket_prompt[i] += prompt
                bucket_cached[i] += cached
                break
    rates = [c / p if p else None for c, p in zip(bucket_cached, bucket_prompt)]
    total_prompt, total_cached = sum(bucket_prompt), sum(bucket_cached)
    plot_hist_with_rate(
        labels, in_counts, rates,
        "Input length distribution + cache hit rate per bucket",
        "input (prompt) tokens per request",
        args.outdir / f"{args.prefix}input-length-cache-hit.png",
        f"mean input = {mean(vals['input']):,.0f} tok\n"
        f"overall hit rate = {total_cached / total_prompt * 100:.1f}% (token-weighted)",
    )

    labels, counts = bucketize(vals["output"], OUTPUT_BUCKETS)
    plot_hist(
        labels, counts, "Output length distribution", "completion tokens per request",
        args.outdir / f"{args.prefix}output-tokens.png", "#DD8452",
        f"mean = {mean(vals['output']):,.0f} tok",
    )

    labels, counts = bucketize(vals["ratio"], RATIO_BUCKETS)
    plot_hist(
        labels, counts, "Per-request cache hit ratio", "cached / prompt tokens",
        args.outdir / f"{args.prefix}cache-hit-ratio.png", "#8172B3",
        f"mean = {mean(vals['ratio']) * 100:.1f}%",
    )


if __name__ == "__main__":
    main()
