"""Dry-run CLI for the rolling replay-dataset collector.

Runs the exact production build path — same source, same conversion, same
sampler, same validation, same atomic publish — with no database and no
deployment, so a profile can be tried against real traffic before it is wired
into the platform:

    python -m app.datasets.dryrun --profile profile.json --source file:///data/bodylog
    python -m app.datasets.dryrun --profile profile.json --source file:///data/bodylog --probe
    python -m app.datasets.dryrun --profile profile.json \
        --source-type victorialogs --source http://victorialogs:9428

`profile.json` is the DB row's fields as a flat object, e.g.

    {"name": "glm5-daily",
     "models": ["glm-5"], "forwarded_to": ["http://198.51.100.20:8052"],
     "window_hours": 24, "subwindow_minutes": 60, "sample_size": 2000,
     "clean": true, "max_model_len": 262144}

The file source reads the zone its files are named in from REPLAY_FEED_FILE_TZ
(the bodylog listener's `timezone`, default UTC). VictoriaLogs credentials come
from the environment, never a file that could be committed:
REPLAY_FEED_VL_USERNAME, REPLAY_FEED_VL_PASSWORD or REPLAY_FEED_VL_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.datasets.builder import (  # noqa: E402
    bucket_summary,
    build_dataset,
    profile_from_dict,
    window_bounds,
)
from app.datasets.errors import LogSourceError  # noqa: E402
from app.datasets.sources import BODYLOG_FILES, SOURCE_TYPES, source_for  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Dry-run a replay dataset collection profile")
    ap.add_argument("--profile", type=Path, required=True, help="Profile JSON file")
    ap.add_argument("--feed-root", type=Path, default=Path("/tmp/replay-feed"))
    ap.add_argument("--source", required=True,
                    help="file:///path/to/bodylog for bodylog_files, or the VictoriaLogs base URL")
    ap.add_argument("--source-type", choices=SOURCE_TYPES, default=BODYLOG_FILES)
    ap.add_argument("--probe", action="store_true",
                    help="Only count what the filters match in the window; build nothing")
    ap.add_argument("--sample-size", type=int, default=0, help="Override the profile's sample_size")
    ap.add_argument("--window-hours", type=int, default=0, help="Override the profile's window_hours")
    ap.add_argument("--seed", type=int, default=1, help="Sampling seed (default 1, reproducible)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    data = json.loads(args.profile.read_text(encoding="utf-8"))
    if args.sample_size:
        data["sample_size"] = args.sample_size
    if args.window_hours:
        data["window_hours"] = args.window_hours
    profile = profile_from_dict(data)
    try:
        client = source_for(args.source_type, args.source)
    except LogSourceError as exc:
        raise SystemExit(str(exc))
    start, end = window_bounds(profile)

    if args.probe:
        print(json.dumps(client.probe(profile.filters, start, end), indent=2, ensure_ascii=False))
        return

    result = build_dataset(
        client, profile, feed_root=args.feed_root, seed=args.seed,
        progress=lambda msg: print(f"  {msg}", flush=True),
    )
    print(f"\nstatus: {result.status}")
    if result.error:
        print(f"error : {result.error}")
    if result.build:
        print(f"path  : {result.build.path}")
        print(f"records: {result.build.records}  bytes: {result.build.bytes / 1e6:.1f}MB")
        print(f"sha256: {result.build.sha256}")
    if result.stats:
        print(f"buckets: {bucket_summary(result.stats)}")
        if result.stats.get("drops"):
            print(f"drops : {result.stats['drops']}")
        if result.stats.get("fixes"):
            print(f"fixes : {result.stats['fixes']}")
    sys.exit(0 if result.status == "ready" else 1)


if __name__ == "__main__":
    main()
