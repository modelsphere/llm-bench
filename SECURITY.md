# Security policy

## Reporting a vulnerability

Please report security issues privately, not as a public issue.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository. If that is unavailable to you, open an issue saying only
that you have a report and how to reach you — no details — and a maintainer will
arrange a private channel.

We will acknowledge a report within a week and tell you what we intend to do
about it. Please give us a reasonable chance to release a fix before disclosing.

## What this platform holds

Worth knowing when assessing impact:

- **Other people's endpoint credentials.** Every submission stores the API key
  of the endpoint under test, Fernet-encrypted under `PLATFORM_SECRET_KEY` and
  never returned by the API. Module parameters that look like credentials
  (`*api_key`, `*token`, `*secret`, `*password`) are redacted for non-admins.
- **It connects wherever it is told to.** A submission is a URL the worker then
  sends traffic to. That is the product, not a bug — but it means the worker's
  network position matters: run it where reaching an arbitrary submitted URL is
  acceptable. The preflight check requires a website session or a service/admin
  key, so it adds no reach beyond submitting a run.
- **Captured traffic.** Replay datasets and the rolling collector hold real
  request bodies from whatever gateway they were collected from. Credential
  headers are stripped at collection and response bodies are dropped by
  default, but prompts are prompts: treat the datasets volume as sensitive.
- **Accounts.** Passwords are bcrypt-hashed; personal API keys are stored as
  SHA-256 hashes and shown once. Login and registration are rate-limited
  (fail-open if Redis is down). With `DEBUG` off the backend refuses to start on
  the placeholder `SECRET_KEY` / `PLATFORM_SECRET_KEY`, and `seed.py` has no
  default admin password.
