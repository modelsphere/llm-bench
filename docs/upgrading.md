# Upgrading

## Between releases

`helm upgrade` (or `docker compose up -d --build`) is the whole procedure: the
migrate step applies new schema revisions and re-runs the idempotent seed.
Read the changelog first; a release that needs a quiet window says so, and
[deploying.md](deploying.md#disruptive-upgrades) describes how to take one.

## From a pre-release internal deployment

This repository starts a fresh migration history (`001_initial`). A database
created by an earlier, internal version of LLMBench is **not** on this chain
and cannot be upgraded in place. Differences you would otherwise trip over:

- The contest-forwarding columns and their migrations do not exist.
- The replay module is named `replay` (it was `replay_tencent`), in stored
  benchmark configurations as well as results.
- Identities are created by `seed.py`, not by the migration; there is no
  default admin.

To move data across, export each benchmark (`GET /benchmarks/admin/benchmarks/{id}/export`,
renaming `replay_tencent` to `replay`) and import it into the new deployment.
Submission history does not carry over.
