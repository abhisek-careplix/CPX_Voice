#!/usr/bin/env bash
# Deploy CarePlix Voice to PRODUCTION: real clinics, real PHI. NOT IN USE YET: careplix-labs stays
# sandbox and demo only, and this script refuses to run until PROJECT below names the production
# project the owner has created. Separate from careplix-labs in every way: its own Google Cloud project, Cloud SQL, Secret Manager, service accounts, data key, LiveKit
# project and settings file. No payer sandbox, no OpenEMR, no trial line, no demo data.
#
#   ./infra/gcp/deploy-prod.sh preflight  # read-only: check .env.production against the rules
#   ./infra/gcp/deploy-prod.sh setup      # APIs, registry, service accounts, Cloud SQL, secrets, audit logs
#   ./infra/gcp/deploy-prod.sh build      # build images from a clean commit that is on origin/main
#   ./infra/gcp/deploy-prod.sh deploy     # deploy the tag `build` recorded; asks you to type the project id
#   ./infra/gcp/deploy-prod.sh status     # read-only: the build and environment each service runs
#
# Settings come from .env.production (git-ignored), never the labs .env, and secrets go straight
# into Secret Manager; they are never printed or passed on a command line. The services also refuse
# to start with synthetic-only settings (careplix_api.environment). Checklist: docs/solution/G4_PHI_GATE.md.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=""  # the production project id, once the owner creates it: the one place it is named
LABS_PROJECT=careplix-labs
REGION=us-central1
REPO=careplix-voice
SQL_INSTANCE=careplix-voice-prod
DB_NAME=careplix
DB_USER=careplix
MODEL=gemini-live-2.5-flash-native-audio
ENV_FILE=.env.production
LOG_RETENTION_DAYS=400  # Cloud Logging _Default bucket (scrubbed app logs, admin and data access)
TAG=${TAG:-}
TAG_FILE=.deploy-prod-tag  # the tag `build` last pushed from this checkout (git-ignored)
G="gcloud --project $PROJECT --quiet"

die() { echo "✗ $*" >&2; exit 1; }
env_value() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }
head_tag() { echo "$(git rev-parse --short HEAD)$(git diff --quiet HEAD || echo -dirty)"; }

# --- Rules, checked before any gcloud command -----------------------------------------------------
# The same rules as careplix_api.environment (production), plus what only this script can see.
preflight() {
  local problems=() labs_livekit
  [ "$PROJECT" != "$LABS_PROJECT" ] || die "PROJECT is $LABS_PROJECT; production needs its own project."
  [ -f "$ENV_FILE" ] || die "No $ENV_FILE. Create it from infra/gcp/env.production.example (docs/solution/G4_PHI_GATE.md)."
  case "$(env_value CAREPLIX_ENV)" in "" | production) ;; *) problems+=("CAREPLIX_ENV must be production or unset") ;; esac
  [ "$(env_value NEXHEALTH_ENV)" = production ] || problems+=("NEXHEALTH_ENV must be production")
  [ -n "$(env_value NEXHEALTH_API_KEY)" ] || problems+=("NEXHEALTH_API_KEY (the production key) is empty")
  case "$(env_value STEDI_ENV)" in "" | production) ;; *) problems+=("STEDI_ENV must be production or unset") ;; esac
  [ -n "$(env_value STEDI_PROD_API_KEY)" ] || problems+=("STEDI_PROD_API_KEY is empty")
  for key in STEDI_TEST_API_KEY STEDI_API_KEY OPENEMR_BASE_URL SANDBOX_NUMBER SANDBOX_OUTBOUND_TRUNK_ID \
    PAYER_SANDBOX_URL PLATFORM_ADMINS; do
    [ -z "$(env_value "$key")" ] || problems+=("$key must be empty (synthetic-only or labs-only)")
  done
  for key in LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET; do
    [ -n "$(env_value "$key")" ] || problems+=("$key is empty")
  done
  # A worker registered on the labs LiveKit project would take production calls (and vice versa).
  labs_livekit=$( [ -f .env ] && grep -E '^LIVEKIT_URL=' .env | head -1 | cut -d= -f2- || true)
  if [ -n "$labs_livekit" ] && [ "$labs_livekit" = "$(env_value LIVEKIT_URL)" ]; then
    problems+=("LIVEKIT_URL is the labs LiveKit project; production needs its own")
  fi
  case "$(env_value LOG_LEVEL)" in DEBUG | debug) problems+=("LOG_LEVEL must not be DEBUG") ;; esac
  if [ ${#problems[@]} -gt 0 ]; then
    printf '✗ %s refuses:\n' "$ENV_FILE" >&2
    printf '  - %s\n' "${problems[@]}" >&2
    echo "Nothing was changed." >&2
    exit 1
  fi
  echo "preflight ok: $ENV_FILE passes the production rules for $PROJECT"
}

put_secret() {  # put_secret NAME  (value on stdin)
  if ! $G secrets describe "$1" >/dev/null 2>&1; then
    $G secrets create "$1" --replication-policy=automatic >/dev/null
  fi
  $G secrets versions add "$1" --data-file=- >/dev/null
}
grant_secret() {  # grant_secret SERVICE_ACCOUNT SECRET...
  local sa=$1
  shift
  for secret in "$@"; do
    $G secrets add-iam-policy-binding "$secret" --member "serviceAccount:$sa" --role roles/secretmanager.secretAccessor >/dev/null
  done
}
project_number() { $G projects describe "$PROJECT" --format='value(projectNumber)'; }

SA_STUDIO="careplix-studio@$PROJECT.iam.gserviceaccount.com"
SA_INTERNAL="careplix-internal@$PROJECT.iam.gserviceaccount.com"
SA_WORKER="careplix-worker@$PROJECT.iam.gserviceaccount.com"
SA_WEBHOOKS="careplix-webhooks@$PROJECT.iam.gserviceaccount.com"
SA_BROWSER="careplix-browser@$PROJECT.iam.gserviceaccount.com"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT/$REPO"

setup() {
  local number pw policy
  number=$(project_number)
  $G services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    sqladmin.googleapis.com secretmanager.googleapis.com iap.googleapis.com aiplatform.googleapis.com \
    iamcredentials.googleapis.com cloudresourcemanager.googleapis.com logging.googleapis.com

  $G artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 ||
    $G artifacts repositories create "$REPO" --location "$REGION" --repository-format docker

  # One identity per service, each with only what that service uses. No keys are ever created.
  for sa in careplix-studio careplix-internal careplix-worker careplix-webhooks careplix-browser; do
    $G iam service-accounts describe "$sa@$PROJECT.iam.gserviceaccount.com" >/dev/null 2>&1 ||
      $G iam service-accounts create "$sa" --display-name "CarePlix Voice $sa (production)"
  done
  for binding in "$SA_STUDIO roles/cloudsql.client" "$SA_STUDIO roles/aiplatform.user" \
    "$SA_INTERNAL roles/cloudsql.client" "$SA_WORKER roles/aiplatform.user" "$SA_WEBHOOKS roles/cloudsql.client"; do
    set -- $binding
    $G projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$1" --role "$2" --condition=None >/dev/null
  done
  CB_SA="$number-compute@developer.gserviceaccount.com"
  for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
    $G projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$CB_SA" --role "$role" --condition=None >/dev/null
  done

  # Cloud SQL: high availability, daily backups kept 30 days, point-in-time recovery, encrypted
  # connections only, and deletion protection.
  if ! $G sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
    $G sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
      --tier db-custom-1-3840 --region "$REGION" --availability-type regional \
      --storage-size 20 --storage-auto-increase --backup-start-time 03:00 \
      --enable-point-in-time-recovery --retained-backups-count 30 --retained-transaction-log-days 7 \
      --ssl-mode ENCRYPTED_ONLY --deletion-protection
  fi
  $G sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 ||
    $G sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"
  if ! $G secrets describe cpx-database-url >/dev/null 2>&1; then
    pw=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
    $G sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$pw" >/dev/null 2>&1 ||
      $G sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$pw" >/dev/null
    printf 'postgresql+asyncpg://%s:%s@/%s?host=/cloudsql/%s:%s:%s' "$DB_USER" "$pw" "$DB_NAME" "$PROJECT" "$REGION" "$SQL_INSTANCE" | put_secret cpx-database-url
    unset pw
  fi

  # Generated here, never copied from labs: the data key seals production PHI only.
  for s in cpx-session-secret cpx-worker-token cpx-browser-token; do
    $G secrets describe "$s" >/dev/null 2>&1 || python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | put_secret "$s"
  done
  $G secrets describe cpx-data-key >/dev/null 2>&1 ||
    python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end="")' | put_secret cpx-data-key
  env_value LIVEKIT_API_KEY | tr -d '\n' | put_secret cpx-livekit-api-key
  env_value LIVEKIT_API_SECRET | tr -d '\n' | put_secret cpx-livekit-api-secret
  env_value NEXHEALTH_API_KEY | tr -d '\n' | put_secret cpx-nexhealth-api-key
  # Its own name: a production key can never be mounted where a test key is expected, or back.
  env_value STEDI_PROD_API_KEY | tr -d '\n' | put_secret cpx-stedi-prod-api-key
  for opt in "ANTHROPIC_API_KEY cpx-anthropic-api-key" "RESEND_API_KEY cpx-resend-api-key"; do
    set -- $opt
    if [ -n "$(env_value "$1")" ]; then
      env_value "$1" | tr -d '\n' | put_secret "$2"
      grant_secret "$SA_STUDIO" "$2"
    fi
  done

  grant_secret "$SA_STUDIO" cpx-database-url cpx-session-secret cpx-worker-token cpx-livekit-api-key \
    cpx-livekit-api-secret cpx-nexhealth-api-key cpx-stedi-prod-api-key cpx-data-key cpx-browser-token
  # The worker-facing API: claims, the receptionist's EHR tools, eligibility. No browser, no email.
  grant_secret "$SA_INTERNAL" cpx-database-url cpx-session-secret cpx-worker-token cpx-livekit-api-key \
    cpx-livekit-api-secret cpx-nexhealth-api-key cpx-stedi-prod-api-key cpx-data-key
  grant_secret "$SA_WORKER" cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret
  grant_secret "$SA_WEBHOOKS" cpx-database-url cpx-data-key
  grant_secret "$SA_BROWSER" cpx-browser-token

  # Audit logs: admin activity is always on; add data access (reads and writes) for every service.
  policy=$(mktemp)
  $G projects get-iam-policy "$PROJECT" --format json >"$policy"
  python3 - "$policy" <<'PY'
import json, sys
path = sys.argv[1]
policy = json.load(open(path))
configs = [c for c in policy.get("auditConfigs", []) if c.get("service") != "allServices"]
configs.append({"service": "allServices", "auditLogConfigs": [
    {"logType": "ADMIN_READ"}, {"logType": "DATA_READ"}, {"logType": "DATA_WRITE"}]})
policy["auditConfigs"] = configs
json.dump(policy, open(path, "w"))
PY
  $G projects set-iam-policy "$PROJECT" "$policy" >/dev/null
  rm -f "$policy"
  $G logging buckets update _Default --location global --retention-days "$LOG_RETENTION_DAYS" >/dev/null
  echo "setup complete for $PROJECT"
}

# Production builds come from merged code only: a clean checkout whose commit is on origin/main.
build() {
  if [ -z "$TAG" ]; then
    git diff --quiet HEAD && [ -z "$(git status --porcelain --untracked-files=no)" ] ||
      die "Uncommitted changes. Production builds come from a clean checkout of main."
    git fetch -q origin main
    git merge-base --is-ancestor HEAD origin/main ||
      die "$(git rev-parse --short HEAD) is not on origin/main. Merge first, then: git switch main && git pull."
    TAG=$(head_tag)
  fi
  $G builds submit --config infra/gcp/cloudbuild.yaml --substitutions "_TAG=$TAG" .
  echo "$TAG" >"$TAG_FILE"
  echo "built $TAG"
}

# The tag to deploy: TAG if set, else what `build` last pushed, else this checkout's commit. The
# images must exist before anything changes.
deploy_tag() {
  local head
  if [ -z "$TAG" ] && [ -s "$TAG_FILE" ]; then
    TAG=$(cat "$TAG_FILE")
    head=$(head_tag)
    if [ "$TAG" != "$head" ]; then
      echo "ℹ Deploying $TAG, the images last built here. This checkout is now at $head; run build first to deploy that." >&2
    fi
  fi
  TAG=${TAG:-$(head_tag)}
  case "$TAG" in *-dirty) die "$TAG was built from uncommitted changes; production deploys merged builds only." ;; esac
  if ! $G artifacts docker images describe "$REGISTRY/api:$TAG" >/dev/null 2>&1; then
    die "No images tagged $TAG in $REGISTRY. Run ./infra/gcp/deploy-prod.sh build first; nothing was deployed."
  fi
}

# Nobody deploys production by accident: type the project id (or set CONFIRM_PROJECT to it).
confirm() {
  local answer=${CONFIRM_PROJECT:-}
  if [ -z "$answer" ] && [ -t 0 ]; then
    read -r -p "Deploy $TAG to PRODUCTION ($PROJECT, real PHI)? Type the project id to continue: " answer
  fi
  [ "$answer" = "$PROJECT" ] || die "Not confirmed; nothing was deployed."
}

url_of() { $G run services describe "$1" --region "$REGION" --format 'value(status.url)' 2>/dev/null || true; }

REQUIRED_SECRETS="cpx-database-url cpx-session-secret cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret
  cpx-nexhealth-api-key cpx-stedi-prod-api-key cpx-data-key cpx-browser-token"

deploy() {
  local number livekit_url conn common_env secrets practice_env practice_secrets studio_secrets
  local identity_env studio_access studio_iap_env missing="" iap_members value
  deploy_tag
  for s in $REQUIRED_SECRETS; do
    $G secrets describe "$s" >/dev/null 2>&1 || missing="$missing $s"
  done
  [ -z "$missing" ] || die "Missing in Secret Manager:$missing. Run ./infra/gcp/deploy-prod.sh setup first; nothing was deployed."
  confirm
  echo "deploying $TAG"
  number=$(project_number)
  livekit_url=$(env_value LIVEKIT_URL)
  conn="$PROJECT:$REGION:$SQL_INSTANCE"
  judge_model=$(env_value JUDGE_MODEL)
  # PAYER_SANDBOX_URL is cleared explicitly: its default is the local synthetic sandbox.
  common_env="CAREPLIX_ENV=production,LIVEKIT_URL=$livekit_url,GCP_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION,GEMINI_LIVE_MODEL=$MODEL,JUDGE_MODEL=${judge_model:-gemini-2.5-flash},PAYER_SANDBOX_URL=,SEED_DEMO_DATA=false,LOG_LEVEL=INFO,CAREPLIX_RUNTIME_BUILD=$TAG"
  secrets="DATABASE_URL=cpx-database-url:latest,SESSION_SECRET=cpx-session-secret:latest,WORKER_SERVICE_TOKEN=cpx-worker-token:latest,LIVEKIT_API_KEY=cpx-livekit-api-key:latest,LIVEKIT_API_SECRET=cpx-livekit-api-secret:latest"

  # 1. Webhook ingress: the only public service besides an opted-in Studio. Verifies, dedupes and
  #    records IDs; no IAP, no PHI in the request it answers.
  $G run deploy careplix-webhooks --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_WEBHOOKS" --add-cloudsql-instances "$conn" --allow-unauthenticated \
    --set-env-vars "CAREPLIX_ENV=production,LOG_LEVEL=INFO" --set-secrets "DATABASE_URL=cpx-database-url:latest,DATA_ENCRYPTION_KEY=cpx-data-key:latest" \
    --min-instances 0 --max-instances 2 \
    --command sh --args='-c,uvicorn careplix_api.practice.webhooks:app --host 0.0.0.0 --port $PORT'
  webhooks_url=$(url_of careplix-webhooks)

  # 2. Browser runtime: private Chromium sessions for the supervised browser agent. Studio only.
  $G run deploy careplix-browser --region "$REGION" --image "$REGISTRY/browser:$TAG" \
    --service-account "$SA_BROWSER" --no-allow-unauthenticated \
    --set-secrets "BROWSER_SERVICE_TOKEN=cpx-browser-token:latest" \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 2 --memory 4Gi \
    --concurrency 40 --timeout 3600
  $G run services add-iam-policy-binding careplix-browser --region "$REGION" \
    --member "serviceAccount:$SA_STUDIO" --role roles/run.invoker >/dev/null
  browser_env="BROWSER_RUNTIME_URL=$(url_of careplix-browser),BROWSER_RUNTIME_AUTH=google_id_token"
  for key in BROWSER_DEFAULT_MODEL BROWSER_GEMINI_MODEL BROWSER_GEMINI_LOCATION BROWSER_CLAUDE_MODEL BROWSER_CLAUDE_REGION; do
    value=$(env_value "$key")
    if [ -n "$value" ]; then browser_env="$browser_env,$key=$value"; fi
  done

  # 3. Studio + API + background loops. Production NexHealth and clearinghouse keys only.
  practice_env="NEXHEALTH_ENV=production,NEXHEALTH_SUBDOMAIN=$(env_value NEXHEALTH_SUBDOMAIN),NEXHEALTH_INSTITUTION_ID=$(env_value NEXHEALTH_INSTITUTION_ID),NEXHEALTH_LOCATION_ID=$(env_value NEXHEALTH_LOCATION_ID),STEDI_ENV=production,PRACTICE_NPI=$(env_value PRACTICE_NPI),WEBHOOK_PUBLIC_URL=$webhooks_url"
  practice_secrets="NEXHEALTH_API_KEY=cpx-nexhealth-api-key:latest,STEDI_PROD_API_KEY=cpx-stedi-prod-api-key:latest,DATA_ENCRYPTION_KEY=cpx-data-key:latest"
  studio_secrets="BROWSER_SERVICE_TOKEN=cpx-browser-token:latest"
  if $G secrets describe cpx-anthropic-api-key >/dev/null 2>&1; then
    studio_secrets="$studio_secrets,ANTHROPIC_API_KEY=cpx-anthropic-api-key:latest"
  fi
  if $G secrets describe cpx-resend-api-key >/dev/null 2>&1; then
    studio_secrets="$studio_secrets,RESEND_API_KEY=cpx-resend-api-key:latest"
  fi
  identity_env="STUDIO_URL=$(url_of careplix-studio)"
  for key in GOOGLE_CLIENT_ID MICROSOFT_CLIENT_ID EMAIL_FROM; do
    value=$(env_value "$key")
    if [ -n "$value" ]; then identity_env="$identity_env,$key=$value"; fi
  done
  # Clinic staff sign in to the Studio (Google, Microsoft, email codes): STUDIO_PUBLIC=true. Off,
  # the Studio stays behind IAP for IAP_MEMBERS (default: CarePlix Google accounts).
  if [ "$(env_value STUDIO_PUBLIC)" = "true" ]; then
    [ -n "$(env_value GOOGLE_CLIENT_ID)$(env_value MICROSOFT_CLIENT_ID)$(env_value RESEND_API_KEY)" ] ||
      die "STUDIO_PUBLIC=true needs a sign-in method: GOOGLE_CLIENT_ID, MICROSOFT_CLIENT_ID or RESEND_API_KEY."
    studio_access="--allow-unauthenticated --no-iap"
    studio_iap_env=""
  else
    studio_access="--no-allow-unauthenticated --iap"
    studio_iap_env=",IAP_AUDIENCE=/projects/$number/locations/$REGION/services/careplix-studio"
  fi
  $G run deploy careplix-studio --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_STUDIO" --add-cloudsql-instances "$conn" \
    --set-env-vars "$common_env,$practice_env,$browser_env,$identity_env,RUN_BACKGROUND=true,PUBLISH_REQUIRES_EVAL=true$studio_iap_env" \
    --set-secrets "$secrets,$practice_secrets,$studio_secrets" --min-instances 1 --max-instances 1 --no-cpu-throttling \
    --timeout 3600 $studio_access
  if [ -n "$studio_iap_env" ]; then
    $G run services add-iam-policy-binding careplix-studio --region "$REGION" \
      --member "serviceAccount:service-$number@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker >/dev/null
    iap_members=$(env_value IAP_MEMBERS)
    for member in $(printf '%s' "${iap_members:-domain:careplix.com}" | tr ',;' '  '); do
      $G iap web add-iam-policy-binding --resource-type=cloud-run --service careplix-studio --region "$REGION" \
        --member "$member" --role roles/iap.httpsResourceAccessor >/dev/null
    done
  fi

  # 4. Worker-facing internal API: its own identity, IAM-only for the worker, one warm instance.
  $G run deploy careplix-internal --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_INTERNAL" --add-cloudsql-instances "$conn" \
    --set-env-vars "$common_env,$practice_env,RUN_BACKGROUND=false" \
    --set-secrets "$secrets,$practice_secrets" --min-instances 1 --max-instances 2 --no-allow-unauthenticated
  $G run services add-iam-policy-binding careplix-internal --region "$REGION" \
    --member "serviceAccount:$SA_WORKER" --role roles/run.invoker >/dev/null
  internal_url=$(url_of careplix-internal)

  # 5. Voice worker: always on, one instance, Vertex AI through its own identity (no keys).
  compression=$(env_value GEMINI_CONTEXT_COMPRESSION)
  $G run deploy careplix-worker --region "$REGION" --image "$REGISTRY/worker:$TAG" \
    --service-account "$SA_WORKER" --no-allow-unauthenticated --ingress internal \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 2 --memory 2Gi \
    --set-env-vars "CAREPLIX_ENV=production,LIVEKIT_URL=$livekit_url,CAREPLIX_API_URL=$internal_url,CAREPLIX_API_AUDIENCE=$internal_url,GCP_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION,GEMINI_LIVE_MODEL=$MODEL,GEMINI_CONTEXT_COMPRESSION=${compression:-true},CAREPLIX_RUNTIME_BUILD=$TAG" \
    --set-secrets "WORKER_SERVICE_TOKEN=cpx-worker-token:latest,LIVEKIT_API_KEY=cpx-livekit-api-key:latest,LIVEKIT_API_SECRET=cpx-livekit-api-secret:latest"

  echo "Studio: $(url_of careplix-studio)"
  echo "Webhooks: $webhooks_url (register from Studio → Practice)"
}

# Read-only: the build and environment each service runs.
status() {
  local image env
  for s in careplix-studio careplix-internal careplix-worker careplix-webhooks careplix-browser; do
    image=$($G run services describe "$s" --region "$REGION" --format 'value(spec.template.spec.containers[0].image)' 2>/dev/null || true)
    env=$($G run services describe "$s" --region "$REGION" --format json 2>/dev/null |
      python3 -c 'import json,sys; c=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]; print(next((e.get("value","") for e in c.get("env",[]) if e["name"]=="CAREPLIX_ENV"),"-"))' 2>/dev/null || true)
    printf '%-18s %-12s %s\n' "$s" "${image:+${image##*:}}" "${env:-not deployed}"
  done
}

# Nothing runs until a production project is named (and it is never the labs project).
if [ -n "${1:-}" ] && [ -z "$PROJECT" ]; then
  die "No production project yet. Set PROJECT= at the top of infra/gcp/deploy-prod.sh (in a reviewed PR) once the owner has created it; nothing was run."
fi

case "${1:-}" in
  preflight) preflight ;;
  setup) preflight && setup ;;
  build) preflight >/dev/null && build ;;
  deploy) preflight >/dev/null && deploy ;;
  status) status ;;
  *) echo "usage: $0 preflight|setup|build|deploy|status" >&2; exit 2 ;;
esac
