#!/usr/bin/env bash
# Reset a user's password directly against the running deployment.
#
# There is no admin/API path to reset another user's password by design
# (POST /auth/change-password requires the *current* password). Passwords are
# bcrypt hashes in users.password_hash, so this hashes the new password INSIDE
# the backend pod (reusing app.core.auth.get_password_hash — the exact same
# CryptContext the app uses) and writes it via the pod's own DB connection.
# The plaintext never lands in a SQL string or in postgres query logs.
#
# The target user can log in with the new password on their next login; role is
# re-read from the DB each request, so no backend restart is needed.
#
# Because this is a rare, high-trust, account-takeover-grade operation, the
# script confirms the target BEFORE touching anything: it looks the user up,
# shows you id/email/role, and makes you retype the email. Resetting a
# super_admin requires an additional explicit confirmation.
#
# Usage:
#   scripts/reset-password.sh --email alice@example.com [--password NEWPASS]
#     --email      Target user's email (required)
#     --password   New password (omit to be prompted, no echo — preferred)
#     --namespace  Target namespace (default: llm-bench)
#     --yes        Skip the interactive target confirmation (for automation).
#                  Does NOT skip the super_admin gate.
#
# Requires: KUBECONFIG pointing at the cluster; permission to exec into the
# backend pod. This MUTATES one row in the users table.

set -euo pipefail

NAMESPACE="llm-bench"
EMAIL=""
PASSWORD=""
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)     EMAIL="$2"; shift 2 ;;
    --password)  PASSWORD="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --yes)       ASSUME_YES=1; shift ;;
    -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$EMAIL" ]]; then
  echo "Error: --email is required" >&2
  exit 2
fi

BACKEND=$(kubectl -n "$NAMESPACE" get pod \
  -l app.kubernetes.io/component=backend \
  --field-selector=status.phase=Running -o name | head -1)

if [[ -z "$BACKEND" ]]; then
  echo "Error: no running backend pod found in namespace '$NAMESPACE'" >&2
  exit 1
fi

# How both exec steps below pass values into the pod:
#
#   * `kubectl exec` has NO --env flag (that is `kubectl run` / `kubectl debug`),
#     so env vars cannot be set on the far side from here.
#   * Anything after `--` is part of the exec request: it is recorded in the
#     kube-apiserver audit log and visible in `ps` inside the pod. A password
#     must never go there.
#
# So we send the *code* as an argument (it holds no secrets) and the *values* on
# stdin, which is neither audited nor visible in the process table. `-i` is
# required for stdin to reach the pod at all.

# --- Step 1: read-only lookup so we can confirm the exact target BEFORE any
# password is entered or any write happens. Emits one machine-parseable line.
LOOKUP_PY=$(cat <<'PY'
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.db.models import User

email = sys.stdin.readline().rstrip("\n")
session = sessionmaker(create_engine(settings.DATABASE_URL_SYNC))()
user = session.query(User).filter(User.email == email).first()
if user is None:
    print("RESETLOOKUP\tNOTFOUND")
else:
    print(f"RESETLOOKUP\tFOUND\t{user.id}\t{user.username}\t{user.role.value}")
PY
)

LOOKUP=$(printf '%s\n' "$EMAIL" \
  | kubectl -n "$NAMESPACE" exec -i "$BACKEND" -- python -c "$LOOKUP_PY")

LINE=$(printf '%s\n' "$LOOKUP" | grep '^RESETLOOKUP' || true)
if [[ -z "$LINE" ]]; then
  echo "Error: lookup failed. Raw output:" >&2
  printf '%s\n' "$LOOKUP" >&2
  exit 1
fi

STATUS=$(printf '%s' "$LINE" | cut -f2)
if [[ "$STATUS" != "FOUND" ]]; then
  echo "Error: no user with email '$EMAIL' in namespace '$NAMESPACE'. Nothing changed." >&2
  exit 1
fi

USER_ID=$(printf '%s' "$LINE" | cut -f3)
USERNAME=$(printf '%s' "$LINE" | cut -f4)
ROLE=$(printf '%s' "$LINE" | cut -f5)

echo
echo "About to reset the password for:"
echo "    id:       $USER_ID"
echo "    email:    $EMAIL"
echo "    username: $USERNAME"
echo "    role:     $ROLE"
echo "    cluster:  namespace=$NAMESPACE"
echo

# --- Step 2: confirm the target by retyping the email (defeats typos & the
# wrong-person-selected mistake). Skippable only via --yes for automation.
if [[ "$ASSUME_YES" -eq 1 ]]; then
  echo "(--yes given; skipping target confirmation)"
else
  read -r -p "Retype the target email to confirm: " CONFIRM_EMAIL
  if [[ "$CONFIRM_EMAIL" != "$EMAIL" ]]; then
    echo "Error: confirmation '$CONFIRM_EMAIL' does not match '$EMAIL'. Nothing changed." >&2
    exit 1
  fi
fi

# --- Step 3: extra hard gate for super_admin targets. This one is NOT skipped
# by --yes — resetting a super_admin is peer/self account-takeover.
if [[ "$ROLE" == "super_admin" ]]; then
  echo
  echo "!! WARNING: '$EMAIL' is a super_admin. Resetting it is account-takeover"
  echo "!! of the most privileged role. Consider doing this straight in the DB"
  echo "!! with a second operator watching."
  read -r -p "Type 'RESET SUPER_ADMIN' to proceed: " SA_CONFIRM
  if [[ "$SA_CONFIRM" != "RESET SUPER_ADMIN" ]]; then
    echo "Error: super_admin confirmation failed. Nothing changed." >&2
    exit 1
  fi
fi

# --- Step 4: now (and only now) collect the new password.
if [[ -z "$PASSWORD" ]]; then
  read -rs -p "New password for ${EMAIL}: " PASSWORD; echo
  read -rs -p "Confirm password: " CONFIRM; echo
  if [[ "$PASSWORD" != "$CONFIRM" ]]; then
    echo "Error: passwords do not match. Nothing changed." >&2
    exit 1
  fi
fi

if [[ -z "$PASSWORD" ]]; then
  echo "Error: empty password. Nothing changed." >&2
  exit 1
fi

# The password is handed to the pod as one line on stdin, so an embedded newline
# would silently truncate it (and desync the reads). Prompted input can't contain
# one; --password on the command line could.
if [[ "$PASSWORD" == *$'\n'* ]]; then
  echo "Error: password contains a newline, which this script cannot pass safely. Nothing changed." >&2
  exit 1
fi

# --- Step 5: write. Match on id (resolved above) so we can't hit a different
# row than the one you confirmed. The password travels on stdin (see the note
# above step 1), never in argv, never in a SQL string.
WRITE_PY=$(cat <<'PY'
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.auth import get_password_hash
from app.core.config import settings
from app.db.models import User

user_id = int(sys.stdin.readline().rstrip("\n"))
new_password = sys.stdin.readline().rstrip("\n")

session = sessionmaker(create_engine(settings.DATABASE_URL_SYNC))()
user = session.query(User).filter(User.id == user_id).first()
if user is None:
    raise SystemExit(f"User id={user_id} vanished between confirm and write; aborted")

user.password_hash = get_password_hash(new_password)
session.commit()
print(f"Reset password for {user.email} (id={user.id}, role={user.role.value})")
PY
)

printf '%s\n%s\n' "$USER_ID" "$PASSWORD" \
  | kubectl -n "$NAMESPACE" exec -i "$BACKEND" -- python -c "$WRITE_PY"
