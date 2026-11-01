#!/usr/bin/env bash
# Deploy CarePlix Voice to the careplix-labs Google Cloud project.
# Safe to re-run: every step checks for existing resources first.
#
#   ./infra/gcp/deploy-labs.sh setup     # APIs, registry, service accounts, Cloud SQL, secrets
#   ./infra/gcp/deploy-labs.sh build     # build images with Cloud Build
#   ./infra/gcp/deploy-labs.sh deploy    # deploy/refresh the four Cloud Run services
#
# Secrets are read from the local .env and written straight into Secret Manager; they are never
# printed or passed on a command line. Synthetic data only until the G4 PHI gate.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=careplix-labs
REGION=us-central1
REPO=careplix-voice
SQL_INSTANCE=careplix-voice-labs
DB_NAME=careplix
DB_USER=careplix
MODEL=gemini-live-2.5-flash-native-audio
TAG=${TAG:-$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)}
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
REGISTRY="$REGION-docker.pkg.dev/$PROJECT/$REPO"
SA_STUDIO="careplix-studio@$PROJECT.iam.gserviceaccount.com"
SA_WORKER="careplix-worker@$PROJECT.iam.gserviceaccount.com"
SA_SANDBOX="careplix-sandbox@$PROJECT.iam.gserviceaccount.com"
SA_WEBHOOKS="careplix-webhooks@$PROJECT.iam.gserviceaccount.com"
G="gcloud --project $PROJECT --quiet"

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

put_secret() {  # put_secret NAME  (value on stdin)
  if ! $G secrets describe "$1" >/dev/null 2>&1; then
    $G secrets create "$1" --replication-policy=automatic >/dev/null
  fi
  $G secrets versions add "$1" --data-file=- >/dev/null
}

setup() {
  $G services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    sqladmin.googleapis.com secretmanager.googleapis.com iap.googleapis.com aiplatform.googleapis.com \
    iamcredentials.googleapis.com cloudresourcemanager.googleapis.com

  $G artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 ||
    $G artifacts repositories create "$REPO" --location "$REGION" --repository-format docker

  for sa in careplix-studio careplix-worker careplix-sandbox careplix-webhooks; do
    $G iam service-accounts describe "$sa@$PROJECT.iam.gserviceaccount.com" >/dev/null 2>&1 ||
      $G iam service-accounts create "$sa" --display-name "CarePlix Voice $sa"
  done
  for role in roles/cloudsql.client roles/aiplatform.user; do
    $G projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA_STUDIO" --role "$role" --condition=None >/dev/null
  done
  $G projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA_WORKER" --role roles/aiplatform.user --condition=None >/dev/null
  $G projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA_WEBHOOKS" --role roles/cloudsql.client --condition=None >/dev/null

  # Cloud Build pushes images with the default compute identity in newer projects.
  CB_SA="$NUMBER-compute@developer.gserviceaccount.com"
  for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
    $G projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$CB_SA" --role "$role" --condition=None >/dev/null
  done

  if ! $G sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
    $G sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
      --tier db-f1-micro --region "$REGION" --storage-size 10 --backup-start-time 03:00
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

  $G secrets describe cpx-session-secret >/dev/null 2>&1 || python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | put_secret cpx-session-secret
  $G secrets describe cpx-worker-token >/dev/null 2>&1 || python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | put_secret cpx-worker-token
  env_value LIVEKIT_API_KEY | tr -d '\n' | put_secret cpx-livekit-api-key
  env_value LIVEKIT_API_SECRET | tr -d '\n' | put_secret cpx-livekit-api-secret
  # Practice layer: NexHealth and Stedi keys from .env; a Fernet key for the encrypted mirror.
  env_value NEXHEALTH_API_KEY | tr -d '\n' | put_secret cpx-nexhealth-api-key
  env_value STEDI_API_KEY | tr -d '\n' | put_secret cpx-stedi-api-key
  $G secrets describe cpx-data-key >/dev/null 2>&1 ||
    python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end="")' | put_secret cpx-data-key

  for secret in cpx-database-url cpx-session-secret cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret \
    cpx-nexhealth-api-key cpx-stedi-api-key cpx-data-key; do
    $G secrets add-iam-policy-binding "$secret" --member "serviceAccount:$SA_STUDIO" --role roles/secretmanager.secretAccessor >/dev/null
  done
  for secret in cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret; do
    $G secrets add-iam-policy-binding "$secret" --member "serviceAccount:$SA_WORKER" --role roles/secretmanager.secretAccessor >/dev/null
  done
  $G secrets add-iam-policy-binding cpx-database-url --member "serviceAccount:$SA_WEBHOOKS" --role roles/secretmanager.secretAccessor >/dev/null
  echo "setup complete"
}

build() {
  $G builds submit --config infra/gcp/cloudbuild.yaml --substitutions "_TAG=$TAG" .
  echo "built $TAG"
}

url_of() { $G run services describe "$1" --region "$REGION" --format 'value(status.url)' 2>/dev/null || true; }

deploy() {
  local livekit_url conn common_env secrets
  livekit_url=$(env_value LIVEKIT_URL)
  conn="$PROJECT:$REGION:$SQL_INSTANCE"

  # 1. Synthetic payer sandbox: private; only the Studio/API identity may call it.
  $G run deploy careplix-payer-sandbox --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_SANDBOX" --no-allow-unauthenticated --min-instances 0 --max-instances 1 \
    --command sh --args='-c,uvicorn careplix_api.payer_sandbox:app --host 0.0.0.0 --port $PORT'
  $G run services add-iam-policy-binding careplix-payer-sandbox --region "$REGION" \
    --member "serviceAccount:$SA_STUDIO" --role roles/run.invoker >/dev/null
  sandbox_url=$(url_of careplix-payer-sandbox)

  common_env="CAREPLIX_ENV=labs,LIVEKIT_URL=$livekit_url,GCP_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION,GEMINI_LIVE_MODEL=$MODEL,PAYER_SANDBOX_URL=$sandbox_url/fhir,PAYER_SANDBOX_AUTH=google_id_token,CAREPLIX_RUNTIME_BUILD=$TAG"
  secrets="DATABASE_URL=cpx-database-url:latest,SESSION_SECRET=cpx-session-secret:latest,WORKER_SERVICE_TOKEN=cpx-worker-token:latest,LIVEKIT_API_KEY=cpx-livekit-api-key:latest,LIVEKIT_API_SECRET=cpx-livekit-api-secret:latest"

  # 2. Webhook ingress: the only public service. Verifies, dedupes and records IDs; no IAP, no PHI.
  $G run deploy careplix-webhooks --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_WEBHOOKS" --add-cloudsql-instances "$conn" --allow-unauthenticated \
    --set-env-vars "CAREPLIX_ENV=labs" --set-secrets "DATABASE_URL=cpx-database-url:latest" \
    --min-instances 0 --max-instances 2 \
    --command sh --args='-c,uvicorn careplix_api.practice.webhooks:app --host 0.0.0.0 --port $PORT'
  webhooks_url=$(url_of careplix-webhooks)

  # 3. Studio + API + background loops, behind IAP (CarePlix Google accounts only).
  practice_env="NEXHEALTH_ENV=$(env_value NEXHEALTH_ENV),NEXHEALTH_SUBDOMAIN=$(env_value NEXHEALTH_SUBDOMAIN),NEXHEALTH_INSTITUTION_ID=$(env_value NEXHEALTH_INSTITUTION_ID),NEXHEALTH_LOCATION_ID=$(env_value NEXHEALTH_LOCATION_ID),STEDI_ENV=$(env_value STEDI_ENV),WEBHOOK_PUBLIC_URL=$webhooks_url"
  practice_secrets="NEXHEALTH_API_KEY=cpx-nexhealth-api-key:latest,STEDI_API_KEY=cpx-stedi-api-key:latest,DATA_ENCRYPTION_KEY=cpx-data-key:latest"
  $G run deploy careplix-studio --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_STUDIO" --add-cloudsql-instances "$conn" \
    --set-env-vars "$common_env,$practice_env,RUN_BACKGROUND=true,IAP_AUDIENCE=/projects/$NUMBER/locations/$REGION/services/careplix-studio" \
    --set-secrets "$secrets,$practice_secrets" --min-instances 1 --max-instances 1 --no-cpu-throttling \
    --timeout 3600 --no-allow-unauthenticated --iap
  $G run services add-iam-policy-binding careplix-studio --region "$REGION" \
    --member "serviceAccount:service-$NUMBER@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker >/dev/null
  $G iap web add-iam-policy-binding --resource-type=cloud-run --service careplix-studio --region "$REGION" \
    --member "domain:careplix.com" --role roles/iap.httpsResourceAccessor >/dev/null

  # 4. Worker-facing internal API: same image, no background loops, IAM-only for the worker.
  $G run deploy careplix-internal --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_STUDIO" --add-cloudsql-instances "$conn" \
    --set-env-vars "$common_env,RUN_BACKGROUND=false,SEED_DEMO_DATA=false" \
    --set-secrets "$secrets" --min-instances 0 --max-instances 2 --no-allow-unauthenticated
  $G run services add-iam-policy-binding careplix-internal --region "$REGION" \
    --member "serviceAccount:$SA_WORKER" --role roles/run.invoker >/dev/null
  internal_url=$(url_of careplix-internal)

  # 5. Voice worker: always on, one instance, Vertex AI through its own identity (no keys).
  $G run deploy careplix-worker --region "$REGION" --image "$REGISTRY/worker:$TAG" \
    --service-account "$SA_WORKER" --no-allow-unauthenticated --ingress internal \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 2 --memory 2Gi \
    --set-env-vars "CAREPLIX_ENV=labs,LIVEKIT_URL=$livekit_url,CAREPLIX_API_URL=$internal_url,CAREPLIX_API_AUDIENCE=$internal_url,GCP_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION,GEMINI_LIVE_MODEL=$MODEL,CAREPLIX_RUNTIME_BUILD=$TAG" \
    --set-secrets "WORKER_SERVICE_TOKEN=cpx-worker-token:latest,LIVEKIT_API_KEY=cpx-livekit-api-key:latest,LIVEKIT_API_SECRET=cpx-livekit-api-secret:latest"

  echo "Studio: $(url_of careplix-studio)"
  echo "Webhooks: $webhooks_url (register from Studio → Practice once deployed)"
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  deploy) deploy ;;
  *) echo "usage: $0 setup|build|deploy" >&2; exit 2 ;;
esac
