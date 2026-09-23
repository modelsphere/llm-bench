#!/usr/bin/env bash
# Check if the current kubeconfig user has all required permissions
# to deploy the LLM Benchmark Platform Helm chart.
#
# Usage:
#   export KUBECONFIG=/path/to/kubeconfig
#   ./scripts/check-k8s-permissions.sh [namespace]
#
# The script exits with code 0 if all required permissions are granted,
# or code 1 if any are missing.

set -euo pipefail

NAMESPACE="${1:-llm-bench}"
FAILED=0

echo "========================================"
echo "Checking permissions for namespace: ${NAMESPACE}"
echo "User: $(kubectl config view --minify -o jsonpath='{.users[0].name}')"
echo "========================================"
echo ""

check() {
  local verb="$1"
  local resource="$2"
  local group="${3:-}"
  local name="${4:-}"

  local args=()
  if [[ -n "$group" ]]; then
    args+=("--subresource=$group")
  fi

  local result
  result=$(kubectl auth can-i "$verb" "$resource" ${group:+--subresource="$group"} -n "$NAMESPACE" 2>/dev/null || echo "no")

  if [[ "$result" == "yes" ]]; then
    printf "  ✓  %-10s %-30s\n" "$verb" "$resource${group:+ (group: $group)}"
  else
    printf "  ✗  %-10s %-30s  MISSING\n" "$verb" "$resource${group:+ (group: $group)}"
    FAILED=1
  fi
}

echo "=== Core resources ==="
check create  serviceaccounts
check create  deployments           apps
check create  services
check create  configmaps
check create  secrets
check create  persistentvolumeclaims
check create  jobs                  batch
check create  pods

echo ""
echo "=== Policy / Autoscaling (used by chart features) ==="
check create  poddisruptionbudgets  policy
check create  horizontalpodautoscalers  autoscaling

echo ""
echo "=== Optional / disabled by default ==="
check create  networkpolicies       networking.k8s.io
check create  servicemonitors       monitoring.coreos.com

echo ""
echo "========================================"
if [[ "$FAILED" -eq 0 ]]; then
  echo "All required permissions are granted."
  echo "You can proceed with: helm install llm-bench ./helm-chart/llm-bench -f secrets.yaml"
  exit 0
else
  echo "MISSING PERMISSIONS detected."
  echo "Ask your cluster admin to grant these permissions to the service account."
  echo "Or install with --set serviceAccount.create=false if a pre-existing SA is provided."
  exit 1
fi
