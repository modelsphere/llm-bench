# Deploying LLMBench

Two supported ways to run it:

| | For |
|---|---|
| `docker compose` at the repository root | One machine: trying it, a small team, CI. See the [README](../README.md). |
| The Helm chart, `deploy/helm/llm-bench` | A Kubernetes cluster. This page. |

## Install

```bash
scripts/gen-prod-secrets.sh --out secrets.prod.yaml --service-key   # prints the admin password once
helm upgrade --install llm-bench deploy/helm/llm-bench \
  -n llm-bench --create-namespace -f secrets.prod.yaml
kubectl -n llm-bench port-forward svc/llm-bench-frontend 8080:80
```

Open <http://localhost:8080> and sign in with the admin the script printed.
`--service-key` is only needed if LLM AutoTune (or another platform) will
submit here; see [Connecting LLM AutoTune](#connecting-llm-autotune).

What a release contains: the API (`backend`), the benchmark `worker`, the
`frontend` (nginx serving the SPA and proxying `/api`), Postgres, Redis, and a
`migrate` Job that runs after every install and upgrade. That Job applies the
schema, then `seed.py` creates the first admin, the `perf-suite-v1` benchmark
and — when `secrets.serviceApiKey` is set — the service account. It never
rewrites anything that already exists, so it is safe on every upgrade.

The images are `ghcr.io/modelsphere/llm-bench-{backend,frontend}`, tagged with
the chart's `appVersion` unless you set `image.*.tag`. If the cluster cannot
reach ghcr.io or Docker Hub, mirror them and set `image.*.repository` plus
`postgres.image`, `redis.image`, `initContainer.image`.

One more thing the worker fetches at first use: the throughput modules size
their synthetic prompts with a public tokenizer (`Qwen/Qwen3-0.6B`, from the
Hugging Face hub). On a cluster that cannot reach huggingface.co, either name a
mirror or mount a tokenizer directory, through `app.extraEnv`:

```yaml
app:
  extraEnv:
    - name: HF_ENDPOINT              # a hub mirror
      value: https://hf-mirror.com
    # or, with a tokenizer directory on the datasets volume:
    # - name: PROCESSOR_PATH
    #   value: /app/dataset/tokenizers/qwen3-0.6b
```

Without one of these, every `perf_guidellm*` run fails at start with a
tokenizer download error, and the rest of the platform is unaffected.

## Sizing

Concurrent benchmark runs = `replicaCount.worker` × `app.worker.processes`.
The defaults (1 × 2) fit a small cluster. Each process peaks around 3 GiB — a
replay keeps its record set resident — so scale `resources.worker` with the
process count; a busy deployment runs 4 pods × 8 processes with
`requests: 8Gi` / `limits: 24Gi` per pod. Runs are I/O-bound (they wait on the
endpoint under test): memory is the constraint, not CPU.

Postgres connections: budget worker processes × 2 + backend replicas × 15, and
keep it under `postgres.maxConnections` (150).

## Storage

| Volume | Access | Why |
|---|---|---|
| Postgres data | RWO | `helm.sh/resource-policy: keep` — survives `helm uninstall` |
| Redis data | RWO | Dramatiq's queue; `maxmemory-policy noeviction` so a full Redis refuses writes rather than dropping queued jobs |
| Datasets (`datasets.enabled`) | ROX/RWX | Academic data and replay captures the workers read; the rolling collector writes here |
| Backups (`backup.enabled`) | RWO | Daily `pg_dump`, also `keep` |

**ReadWriteOnce across nodes.** A RollingUpdate of an RWO-backed workload
deadlocks when the new pod lands on another node — it cannot mount the volume
the old pod still holds. Postgres, Redis and the dataset collector therefore
roll with `Recreate`. Check this before adding any RWO-backed component.

**Network storage.** Postgres crash recovery replays WAL, and on network
storage the final checkpoint has been seen to take ~50 s; the startup probe
allows 10 minutes so liveness cannot kill it mid-recovery and loop.

**Datasets.** The examples every module defaults to are inside the image. For
real measurements, put data on the datasets volume (mounted at `/app/dataset`):
the academic suites with `scripts/fetch_academic_datasets.py` (run it anywhere,
then copy `dataset/opencompass/data` over), and replay captures as JSONL.

## Rolling replay datasets

`datasetBuilder.enabled` adds the collector: it samples live gateway traffic
into replay datasets on a schedule (see [replay-datasets.md](replay-datasets.md)).
It needs `datasets.enabled`, and a source:

```yaml
datasets:
  enabled: true
  storageClass: nfs-client
datasetBuilder:
  enabled: true
  bodylog:                      # the gateway's bodylog listener files
    hostPath: /data/bodylog     # or existingClaim: <the listener's PVC>
    fileTimezone: UTC           # the listener's own `timezone` value
    nodeSelector:
      kubernetes.io/hostname: <the listener's node>
```

For a VictoriaLogs source instead, leave `bodylog` empty and put the store's
credentials under `datasetBuilder.auth` (in the secrets file).

## Connecting LLM AutoTune

LLM AutoTune benchmarks every configuration it tries through LLMBench. It
authenticates as a **service account**: a `service`-role user that can create
and lock the benchmarks it runs (and only those), read and trigger rolling
dataset builds, and preflight endpoints — never manage users or other people's
benchmarks.

1. Generate a key and give it to LLMBench: `secrets.serviceApiKey`
   (`gen-prod-secrets.sh --service-key` does this). Upgrade the release; the
   migrate Job creates the account.
2. Give AutoTune the same value: `llmbench.apiKey` in its chart, and
   `llmbench.url` pointing at LLMBench's backend service —
   `http://llm-bench-backend.llm-bench:8000` from inside the cluster.

The contract AutoTune relies on — routes, metric names, run identity — is in
[api/for-autotune.md](api/for-autotune.md).

To rotate the key: set the new value and upgrade (the old key keeps working —
keys are added, never swapped), move AutoTune to the new value, then delete the
old key from the service account on the API Keys page.

## Upgrades

A `helm upgrade` rolls the worker. Workers **drain**: on SIGTERM a worker stops
taking new jobs and finishes the one it has, for up to
`app.worker.drainBudgetSeconds` (12 h), while new pods take new work. There is
no mid-run resume, so this is what keeps a deploy from killing runs.

Two consequences:

- While a long run drains, its old pod stays `Terminating`, so two worker
  generations coexist. Size for it.
- The migrate Job runs after the new pods start. A migration that removes or
  renames something the old code still reads breaks the draining workers.

### Disruptive upgrades

For a destructive migration, or to avoid holding two generations through a
very long run, **cordon** first: a Redis flag that makes the API refuse new
submissions (HTTP 503) while running ones finish.

```bash
kubectl -n llm-bench exec deploy/llm-bench-redis -- redis-cli SET platform:cordon 1
# wait until nothing is running (admin → All Submissions, or GET /submissions/admin/all)
helm upgrade llm-bench deploy/helm/llm-bench -n llm-bench -f secrets.prod.yaml
kubectl -n llm-bench exec deploy/llm-bench-redis -- redis-cli DEL platform:cordon
```

| Change | Plain upgrade, or cordon? |
|---|---|
| Frontend only; API handlers; an additive column or table | Plain |
| A new benchmark module | Plain |
| Dropping or renaming a column; a new enum value that new code writes | Cordon |
| Anything that changes how scores are computed | Cordon — mixing old and new scoring fails silently |
| A dramatiq major version, a renamed actor, a changed Redis key prefix | Cordon |

**If `helm upgrade --wait` times out**, look at pod readiness before the
migrate Job: the Job is a post-upgrade hook and only runs once the release is
up, so a timeout usually means a component failed to start, not a migration.

## Data safety

- The Postgres volume and the chart's Secret carry
  `helm.sh/resource-policy: keep`: `helm uninstall` leaves them.
- **Reinstalling over them** fails with `invalid ownership metadata`, because
  the kept objects no longer carry the new release's labels. Re-adopt them:

  ```bash
  NS=llm-bench REL=llm-bench
  for obj in pvc/${REL}-postgres pvc/${REL}-backups secret/${REL}; do
    kubectl -n $NS annotate $obj meta.helm.sh/release-name=$REL meta.helm.sh/release-namespace=$NS --overwrite
    kubectl -n $NS label $obj app.kubernetes.io/managed-by=Helm --overwrite
  done
  ```

- **Postgres reads `POSTGRES_PASSWORD` only when it initializes an empty data
  directory.** If the Secret's password ever diverges from the one inside the
  volume (a regenerated secrets file over a kept volume), the backend fails
  with `InvalidPasswordError`, and dropping the database does not help: the
  role's password lives in the cluster catalog. Restore the old password, or
  start over with a new volume.
- The Secret also holds `PLATFORM_SECRET_KEY`, which encrypts every stored
  endpoint API key. Rotating it makes those unreadable. To rotate deliberately,
  delete the Secret before reinstalling.

**Restore from a dump** (backups on, or any `pg_dump -Fc`):

```bash
kubectl -n llm-bench scale deploy -l 'app.kubernetes.io/component in (backend,worker)' --replicas=0
kubectl -n llm-bench exec deploy/llm-bench-postgres -- psql -U postgres -c 'DROP DATABASE llmbench; CREATE DATABASE llmbench;'
kubectl -n llm-bench exec -i deploy/llm-bench-postgres -- pg_restore -U postgres -d llmbench --no-owner --no-acl < backup.dump
kubectl -n llm-bench scale deploy -l 'app.kubernetes.io/component in (backend,worker)' --replicas=1
```

## Accounts

`user` submits and reads; `admin` manages benchmarks, users and card types;
`super_admin` additionally grants and revokes admin. A super admin is never
granted through the API — set it in the database:

```bash
kubectl -n llm-bench exec deploy/llm-bench-postgres -- \
  psql -U postgres -d llmbench -c "UPDATE users SET role='super_admin' WHERE email='you@example.com';"
```

Roles are re-read on every request; the web UI picks the change up on the
user's next login. A forgotten password is reset by a super admin from the
Users page (reset requests), or, with no super admin, by
`scripts/reset-password.sh`.

## Diagnosing a stuck worker

The worker image ships `py-spy`, and `app.worker.enablePtrace` (on by default)
grants the capability it needs, so a wedged run can be inspected without
restarting the pod that holds the evidence:

```bash
kubectl -n llm-bench exec deploy/llm-bench-worker -- sh -c 'for p in $(pgrep -f dramatiq); do py-spy dump --pid $p; done'
```

A run whose worker dies is not lost silently: workers heartbeat each running
submission to Redis, and the backend's reaper marks one FAILED once its
heartbeat stops.
