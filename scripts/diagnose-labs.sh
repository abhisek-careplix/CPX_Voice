#!/usr/bin/env bash
# Read-only health report for careplix-labs: what is deployed, whether each Cloud Run service is
# healthy, and the recent errors from the worker, Studio and browser runtime.
#
#   ./scripts/diagnose-labs.sh            # writes ~/Desktop/careplix-diagnosis.txt and prints it
#
# Changes nothing. Prints no secret values (secrets are shown only as Secret Manager references).
# careplix-labs holds synthetic data only.
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=careplix-labs
REGION=us-central1
HOURS=${HOURS:-3}
OUT=${OUT:-$HOME/Desktop/careplix-diagnosis.txt}
G="gcloud --project $PROJECT --quiet"

section() { printf '\n===== %s =====\n' "$1"; }

{
  section "Local checkout"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)  commit: $(git rev-parse --short HEAD)  origin/main: $(git rev-parse --short origin/main 2>/dev/null)"
  echo "gcloud account: $(gcloud config get-value account 2>/dev/null)"

  section "Cloud Run services"
  for svc in careplix-studio careplix-internal careplix-worker careplix-browser careplix-webhooks careplix-payer-sandbox; do
    echo "--- $svc"
    $G run services describe "$svc" --region "$REGION" --format='value(
      status.latestReadyRevisionName,
      status.latestCreatedRevisionName,
      spec.template.spec.containers[0].image)' 2>&1 | tr '\t' '\n' | sed 's/^/  /'
    $G run services describe "$svc" --region "$REGION" \
      --format='table[no-heading](status.conditions[].type,status.conditions[].status,status.conditions[].message)' 2>/dev/null |
      sed 's/^/  /'
  done

  section "Worker and Studio settings (plain values; secrets shown as references only)"
  for svc in careplix-worker careplix-studio; do
    echo "--- $svc"
    $G run services describe "$svc" --region "$REGION" --format=json 2>/dev/null | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
for e in spec.get("env", []):
    name = e.get("name")
    if "value" in e:
        print("  %s=%s" % (name, e["value"]))
    else:
        ref = e.get("valueFrom", {}).get("secretKeyRef", {})
        print("  %s=<secret %s>" % (name, ref.get("name")))
' || echo "  (not deployed)"
  done

  section "Worker log, last ${HOURS}h (job pick-up, claims, model errors, crashes)"
  $G logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"careplix-worker\"" \
    --freshness="${HOURS}h" --limit 200 --order desc \
    --format='value(timestamp,severity,textPayload,jsonPayload.message)' 2>&1 |
    grep -Ei 'error|warn|traceback|exception|job accepted|claim|registered|model|silent|shutdown|counterpart|failed|refused|denied' | head -150

  section "Studio log, last ${HOURS}h (warnings and errors)"
  $G logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"careplix-studio\" AND severity>=WARNING" \
    --freshness="${HOURS}h" --limit 100 --order desc \
    --format='value(timestamp,severity,textPayload,jsonPayload.message,jsonPayload.error_code)' 2>&1 | head -100

  section "Studio requests that failed, last ${HOURS}h"
  $G logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"careplix-studio\" AND httpRequest.status>=400" \
    --freshness="${HOURS}h" --limit 60 --order desc \
    --format='value(timestamp,httpRequest.status,httpRequest.requestMethod,httpRequest.requestUrl)' 2>&1 |
    sed -E 's#https://[^/]+##' | head -60

  section "Browser runtime log, last ${HOURS}h"
  $G logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"careplix-browser\"" \
    --freshness="${HOURS}h" --limit 60 --order desc \
    --format='value(timestamp,severity,textPayload,jsonPayload.message)' 2>&1 | head -60

  section "Done"
  echo "Send this file back in the chat (synthetic data only; no secret values are included)."
} 2>&1 | tee "$OUT"

echo
echo "Saved to $OUT"
