# Rolling replay datasets

The `replay` module replays captured requests against an endpoint. Its dataset
is either a **fixed file** or a **collection profile**: a named dataset rebuilt
from live gateway traffic, so a benchmark keeps measuring against what the
service is actually asked this week. Every run records which build it replayed
(`dataset_id`, `dataset_sha256` in its metrics), so two scores can always be
checked for comparability.

This page is the end-to-end recipe: where the traffic comes from, creating a
profile, rebuilding it on demand, reading back what was collected, pointing a
benchmark at it, and tracing a score to the bytes behind it.

## Where the traffic comes from

A profile names a **source**:

| `source_type` | `source_url` | What it reads |
|---|---|---|
| `bodylog_files` (default) | `file:///data/bodylog` | Hourly JSONL files of captured gateway traffic, one request per line (format below): `<dir>/YYYY-MM-DD/HH.jsonl`, finished days rolled into `<dir>/YYYY-MM-DD.tar.gz`. The modelsphere gateway's bodylog listener writes exactly this, deployed with the `bodylog` chart from [`modelsphere/helm-charts`](https://github.com/modelsphere/helm-charts); anything else that writes the same lines works too. |
| `victorialogs` | `http://victorialogs:9428` | A VictoriaLogs store the same records were shipped to, queried with LogsQL. Allows `extra_logsql` filters. |

Both produce identical records, so moving a profile between them changes
nothing about the dataset it builds.

**One line of a bodylog file.** A line needs `ts` and `req_body`, and — with
a profile's default filters (`statuses: ["200"]`, `uris: ["/v1/chat/completions"]`)
— `status` and `uri` too. The rest feed the other filters and the size repairs.
Fields the collector does not know are ignored.

```json
{"ts": "2026-01-01T13:05:09Z",
 "uri": "/v1/chat/completions",
 "status": 200,
 "peer": "198.51.100.20:8052",
 "forwarded_to": "http://198.51.100.20:8052",
 "req_body": "{\"model\":\"my-model\",\"stream\":true,\"messages\":[...]}",
 "req_body_truncated": false,
 "resp_meta": {"model": "my-model", "usage": {"prompt_tokens": 812}}}
```

| field | used for |
|---|---|
| `ts` | picking the lines inside the collection window (ISO 8601; no zone = UTC) |
| `req_body` | the request, as the JSON **string** the client sent; it must have `messages` |
| `status`, `uri` | the `statuses` / `uris` filters; a 4xx is dropped when cleaning |
| `forwarded_to`, `peer` | the `forwarded_to` filter (either form, with or without scheme) |
| `req_body_truncated` | a truncated capture is never replayed |
| `resp_meta.model`, `resp_meta.usage.prompt_tokens` | the `models` filter (falls back to `req_body`'s model); the size budget |

**Files, in a cluster.** The collector pod mounts the listener's directory
read-only (`datasetBuilder.bodylog` in the chart). The listener's volume is
ReadWriteOnce, so the collector runs on the listener's node — set
`datasetBuilder.bodylog.nodeSelector`, and reuse the listener's `hostPath` or
claim. Set `datasetBuilder.bodylog.fileTimezone` to the listener's own
`timezone` value: it names its hourly files in that zone.

**Model names.** The raw bodylog line carries the model in the response
metadata and the request body, not at the top level; the file source looks in
all of those, so a `models` filter means the same thing for both sources.

Every payload below was checked against the running app's OpenAPI schema. When
in doubt, `GET /api/openapi.json` is the ground truth — this file is a guide,
not the contract.

---

## 0. Auth

Creating, editing, probing and freezing profiles is admin-only. Reading
profiles and builds, and triggering a build, is also open to a **service
account** — how another platform such as LLM AutoTune pins a campaign to a
fresh build without holding admin rights.

```bash
API=http://localhost:8080/api          # through the frontend; see the README

# (a) a personal or service API key — best for scripts; it carries its owner's
#     role and does not expire like a login token
AUTH="Authorization: Bearer $LLMBENCH_API_KEY"

# (b) a login token
TOKEN=$(curl -s -X POST "$API/auth/login" \
          -H 'Content-Type: application/json' \
          -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"
```

---

## 1. Create a manual-only profile

`schedule_interval_hours: 0` is what makes it externally driven: the collector
never builds it on a tick, only when step 3 asks.

> Do **not** use `enabled: false` for this. That turns the profile off
> entirely — manual builds included.

```bash
curl -s -X POST "$API/replay-datasets/profiles" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{
  "name": "glm5-ondemand",
  "display_name": "GLM-5 on demand",
  "source_type": "bodylog_files",
  "source_url": "file:///data/bodylog",

  "models": ["glm-5"],
  "forwarded_to": ["http://198.51.100.20:8052"],
  "statuses": ["200"],

  "window_hours": 24,
  "window_timezone": "UTC",
  "sample_size": 2000,
  "clean": true,
  "max_model_len": 262144,

  "schedule_interval_hours": 0
}'
```

`name` must be a lowercase slug — it names a directory on the datasets volume
and **cannot be changed later**. Response is the full profile; keep `id`.

Everything else has a default (`subwindow_minutes` 60, `oversample_factor` 3.0,
`max_carry_multiple` 4, `min_records` 100, `keep_builds` 7, `compress` true, …).
Only `name`, `display_name` and `source_url` are required; `source_type`
defaults to `bodylog_files`. A mismatched pair (an `http://` URL for the file
source, `extra_logsql` without VictoriaLogs) is refused with a 422 that says
which.

Filter semantics worth knowing:

- Values are matched **exactly** on their field. Several values in one list are
  OR-ed; different fields are AND-ed.
- `forwarded_to` is how you pin the dataset to a single backend. It matches
  the record's `forwarded_to` (set when an upstream router reports the real
  backend) or its `peer`, with or without a scheme.
- On VictoriaLogs, filter on fields, never on `_msg`: `_msg:glm-5` is a token
  match that also hits `glm-5.2`. The profile's filters always compile to exact
  field matches; `extra_logsql` is where that mistake could come back.

Sizing the sample (why a build can come back smaller than you expect):

The collector time-stratifies the window into `subwindow_minutes` slices and
keeps a **base quota** of `ceil(sample_size / n_slices)` per slice. A quiet
slice's shortfall carries forward, but a single slice keeps at most
`base_quota × max_carry_multiple`. **For traffic concentrated in a few slices
(a daily burst), that product — not `sample_size` — is the real ceiling.**
Example: a model whose traffic sits in 3 hours of a 48h window at
`sample_size=2000` has `base_quota = ⌈2000/48⌉ = 42`, so 3 busy slices cap the
build at `3 × (42 × 4) ≈ 513` records however high `sample_size` is set. To
capture a bursty model fully, raise `max_carry_multiple`, or widen
`subwindow_minutes` (fewer, larger slices → bigger `base_quota`). The admin
editor shows the derived per-slice cap live so this is no longer implicit.

## 2. Check the filters match something (optional, recommended)

```bash
curl -s -X POST "$API/replay-datasets/profiles/$ID/probe" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"hours": 1}'
```

```json
{ "matched": 951,
  "query": "bodylog files under /data/bodylog (UTC) [...] models=['glm-5'] ...",
  "sample_fields": ["req_body", "resp_meta", "status", "ts", "..."],
  "sample_prompt_tokens": 34632 }
```

`matched: 0` means the filters are wrong — a typo'd `forwarded_to` is the usual
cause, or, for files, a `fileTimezone` that does not match the listener's.
Cheaper to find here than as an empty build later. `error` is set instead when
the source cannot be read (directory not mounted, store rejected the query).
The file source scans every line in the window, so probe a short one.

## 3. Trigger a build

```bash
BUILD=$(curl -s -X POST "$API/replay-datasets/profiles/$ID/build" -H "$AUTH" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["build_row_id"])')
```

```json
{ "build_row_id": 42, "queue_depth": 0 }
```

- **409** — a build for this profile is already queued or running. One at a
  time per profile, by design.
- **503** — no collector is configured (`DATASET_BUILDER_URL` unset).
- **502** — the collector is unreachable or refused.

## 4. Poll until it finishes

A build takes minutes (it streams the window from the log store), so poll on
the order of 10–30s, not continuously.

```bash
curl -s "$API/replay-datasets/builds/$BUILD" -H "$AUTH"
```

```json
{ "id": 42, "build_id": "20260805T094230Z", "status": "ready",
  "trigger": "manual",
  "records": 2000, "size_bytes": 141_000_000,
  "sha256": "62c2021e7a6f9f948c674103cb15041606f6dffaae9cdca63345bb6545f9610a",
  "path": "/app/dataset/replay/auto/glm5-ondemand/builds/20260805T094230Z.jsonl.gz",
  "window_start": "2026-08-04T09:00:00+00:00",
  "window_end":   "2026-08-05T09:00:00+00:00",
  "progress": "published 2000 records (141.0MB) as 20260805T094230Z",
  "error": null,
  "stats_json": { "buckets": {...}, "drops": {...}, "summary": {...} },
  "started_at": "...", "finished_at": "..." }
```

`status` moves `pending → running → ready | failed`. `progress` carries the
collector's latest line while it runs. On `failed`, `error` says why — the
common ones are deliberate refusals: fewer than `min_records` collected, too
few input-length buckets, or not enough free space on the datasets volume. A
failed build leaves the previous one serving.

## 5. Read what the benchmark will now replay

```bash
curl -s "$API/replay-datasets/profiles/$ID" -H "$AUTH"
```

The `current` block is read from the pointer file on the datasets volume — i.e.
what a run would actually resolve, not merely the newest row in the table:

```json
"current": {
  "build_id": "20260805T094230Z",
  "sha256": "62c2021e...5f9610a",
  "records": 2000,
  "bytes": 141000000,
  "path": "/app/dataset/replay/auto/glm5-ondemand/builds/20260805T094230Z.jsonl.gz",
  "built_at": "2026-08-05T09:43:12+00:00",
  "age_hours": 0.1,
  "stale": false,
  "window_start": "2026-08-04T09:00:00+00:00",
  "window_end": "2026-08-05T09:00:00+00:00",
  "buckets": {"<6K": 61, "6K-16K": 240, "16K-32K": 902, "32K-64K": 780, "...": 0},
  "summary": {
    "models": {"glm-5": 2000},
    "forwarded_to": {"http://198.51.100.20:8052": 2000},
    "prompt_tokens":     {"count": 2000, "min": 2994, "p50": 29334, "p90": 58062, "max": 63945, "mean": 31531},
    "completion_tokens": {"count": 2000, "min": 19, "p50": 144, "p90": 644, "max": 2602, "mean": 316},
    "total_prompt_tokens": 63062000, "total_cached_tokens": 57206000,
    "cache_hit_rate": 0.9071
  }
}
```

`current` is `null` when nothing has been published yet.

**About the hash.** It is SHA-256 over the dataset's **uncompressed** bytes,
accumulated as records are written. Builds are gzipped, so verifying by hand
needs the content, not the container:

```bash
gunzip -c <file>.jsonl.gz | sha256sum     # matches
sha256sum <file>.jsonl.gz                 # does NOT match
```

It identifies the dataset; it is not an enforced integrity check. Resolution
verifies file *size*, because re-hashing hundreds of MB before every run is not
worth it.

## 6. Point a benchmark at the profile

Set the replay module's params in the benchmark (admin API or the editor):

```json
{ "module_name": "replay",
  "params_json": {
    "dataset_source": "auto",
    "dataset_profile": "glm5-ondemand",
    "dataset_max_age_hours": 0,
    "concurrency": 5,
    "max_samples": 1000
  } }
```

- `dataset_path` is ignored under `auto` — leave it out.
- `dataset_max_age_hours: 0` disables the staleness warning, which is usually
  what you want for a manual-only profile: it is *meant* to sit until you
  rebuild it. Leave it at 48 if you would rather be told the data is old.
- Saving is rejected with a 400 if `dataset_profile` names a profile that does
  not exist.
- The profile must have published **at least one build** before a submission
  runs, or the run fails with "has no published build to replay".

## 7. Submit and trace the result

Submit as normal (`POST /benchmarks/{slug}/submit`). Then
`GET /submissions/{id}` shows which dataset that run actually replayed, in two
places:

```json
"runs": [{
  "module_name": "replay",
  "params_json": {
    "dataset_path": "/app/dataset/replay/auto/glm5-ondemand/pins/s241-r720__20260805T094230Z__1785838024.jsonl.gz",
    "dataset_resolved": {
      "profile": "glm5-ondemand",
      "build_id": "20260805T094230Z",
      "sha256": "62c2021e...5f9610a",
      "path": "/app/.../builds/20260805T094230Z.jsonl.gz",
      "pinned_path": "/app/.../pins/s241-r720__...jsonl.gz",
      "records": 2000,
      "built_at": "...", "window_start": "...", "window_end": "...",
      "age_hours": 0.1, "stale": false
    }
  },
  "metrics_json": { "dataset_id": "20260805T094230Z",
                    "dataset_sha256": "62c2021e7a6f9f94", "...": "..." }
}]
```

`dataset_id` / `dataset_sha256` are also display metrics, so they show on the
submission page next to the numbers.

**Why this matters for a rolling dataset:** two submissions started minutes
apart can legitimately replay different builds. Scores are only directly
comparable between runs whose `dataset_id` matches. To freeze a benchmark on
one dataset, set `dataset_source: "fixed"` and paste `dataset_resolved.path`
(the `builds/` path, not the pin).

`pinned_path` differs from `path` on purpose: the run reads a hard link held
for its lifetime, so retention cannot delete the dataset mid-replay.

## 8. Freeze a build into a static dataset (make a comparison reproducible)

A rolling build is **not permanent**: once `keep_builds` newer builds exist and
it is past `min_retain_hours`, retention deletes it. So pasting a build's
`builds/…` path into a `dataset_source=fixed` benchmark works today and 404s
next week — at which point the replay module silently falls back to the worker's
default dataset. To pin a comparison to one exact dataset that never changes,
**freeze** the build instead:

```bash
# Freeze build row {build_row_id} of profile {id} under a slug you choose.
curl -sS -X POST "$API/replay-datasets/profiles/$PROFILE_ID/builds/$BUILD_ROW_ID/freeze" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"acceptance-2026-q3"}'
# → {"name":"acceptance-2026-q3",
#    "path":"/app/dataset/replay/frozen/acceptance-2026-q3.jsonl.gz",
#    "records":2000,"sha256":"…","source_profile":"glm5-ondemand", …}
```

What freezing does, and why it is safe:

- It **hard-links** (no copy, no data movement — instant even for a multi-GB
  build) the build's bytes into `<datasets>/replay/frozen/<name>.jsonl[.gz]`,
  a directory **outside** the rolling `builds/` tree. Retention (`prune`) never
  looks there, the profile can be deleted, the whole `auto/` tree can be swept —
  the frozen file survives all of it. When the filesystem refuses to link, it
  falls back to a byte-faithful copy.
- Freezing is **create-once**: a second freeze onto the same name is a 409, so a
  frozen dataset is genuinely immutable.
- The returned `path` is what you paste into a **`dataset_source=fixed`**
  benchmark's `dataset_path`. Gzip works identically on `fixed` and `auto` —
  both go through the same extension-dispatched loader — so a `.jsonl.gz` frozen
  file needs no conversion.
- A provenance sidecar (`<name>.meta.json`) records the source profile, build id,
  sha256 and traffic window next to the file, so a frozen dataset is always
  traceable even without the database.

List and delete them:

```bash
curl -sS "$API/replay-datasets/frozen" -H "$AUTH"
# → {"frozen":[{"name":"acceptance-2026-q3","path":"…","records":2000,
#              "used_by":["Q3 GLM-5 acceptance"], …}], "collector_configured":true}

# Delete refuses (409) while a fixed benchmark still points at it — deleting it
# would make that benchmark fall back to the default dataset. Force past it only
# once you have repointed those benchmarks.
curl -sS -X DELETE "$API/replay-datasets/frozen/acceptance-2026-q3?force=true" -H "$AUTH"
```

Freezing and deleting need the collector (it is the writer of the datasets
volume); **listing does not** — it reads the volume directly, so the frozen
inventory is visible even when the collector is down.

---

## 9. Download a build's dataset file

Every build in the history can be pulled down as the exact file the workers
replay — same gzip, same bytes, same `sha256` the build row reports. Useful for
inspecting what a run actually saw, or for replaying a production sample
somewhere else.

```bash
curl -sS -L -OJ \
  "$API/replay-datasets/profiles/$PROFILE_ID/builds/$BUILD_ROW_ID/download" \
  -H "$AUTH"
# → glm5-ondemand-20260823T093502Z.jsonl.gz   (Content-Disposition names it)
```

- Served by the **backend off the datasets volume**, not proxied through the
  collector — so a download still works while the collector is down, exactly
  like `GET /replay-datasets/frozen`.
- Auth may be passed as `?token=…` instead of the header, because a browser
  download is a plain navigation that cannot set one (same escape hatch the SSE
  log stream uses). The admin UI's **Download** link, next to Freeze in the
  build history, uses that form so the browser streams a multi-hundred-MB file
  straight to disk.
- A build row outlives its file: retention prunes `builds/` while the history
  stays. A pruned build answers **404** and the UI greys the link out — the
  build list reports this per row as `downloadable`. Freeze a build if you need
  it to stay downloadable forever.
- A build that is not `ready` answers **409**.

---

## The whole thing, as a script

```bash
#!/usr/bin/env bash
set -euo pipefail
API=${API:?}; ID=${PROFILE_ID:?}
AUTH="Authorization: Bearer ${LLMBENCH_API_KEY:?}"

BUILD=$(curl -sf -X POST "$API/replay-datasets/profiles/$ID/build" -H "$AUTH" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["build_row_id"])')
echo "build $BUILD queued"

while :; do
  read -r STATUS ERR < <(curl -sf "$API/replay-datasets/builds/$BUILD" -H "$AUTH" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], (d.get("error") or "-").replace(" ","_"))')
  [[ "$STATUS" == "ready"  ]] && break
  [[ "$STATUS" == "failed" ]] && { echo "build failed: $ERR" >&2; exit 1; }
  sleep 20
done

curl -sf "$API/replay-datasets/profiles/$ID" -H "$AUTH" \
  | python3 -c 'import json,sys; c=json.load(sys.stdin)["current"]; print(c["build_id"], c["sha256"], c["records"])'
```

---

## Endpoint reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/replay-datasets/profiles` | list profiles, each with its `current` build |
| POST | `/replay-datasets/profiles` | create (admin) |
| GET | `/replay-datasets/profiles/{id}` | one profile + `current` (hash, records, summary) |
| PUT | `/replay-datasets/profiles/{id}` | update (partial; `name` is immutable) (admin) |
| DELETE | `/replay-datasets/profiles/{id}` | delete policy + history; dataset FILES are left on disk (admin) |
| POST | `/replay-datasets/profiles/{id}/build` | trigger a build → `build_row_id` |
| POST | `/replay-datasets/profiles/{id}/probe` | count what the filters match in the last N hours (admin) |
| GET | `/replay-datasets/profiles/{id}/builds` | build history (`?limit=`, newest first) |
| GET | `/replay-datasets/builds/{id}` | one build by the id the trigger returned |
| GET | `/replay-datasets/profiles/{id}/builds/{build_row_id}/download` | download the build's dataset file as published (`?token=` accepted; 404 once pruned) |
| GET | `/replay-datasets/health` | per-profile freshness vs `max_age_hours` |
| GET | `/replay-datasets/names` | enabled profile names (feeds the editor's suggestions) |
| POST | `/replay-datasets/profiles/{id}/builds/{build_row_id}/freeze` | freeze a ready build into a permanent `frozen/<name>` dataset → its path |
| GET | `/replay-datasets/frozen` | list frozen datasets, each with the benchmarks (`used_by`) that replay it |
| DELETE | `/replay-datasets/frozen/{name}` | delete a frozen dataset (`?force=true` to override the in-use guard) |

Everything not marked admin is also open to a service account. Freezing and
deleting frozen datasets are admin-only.
