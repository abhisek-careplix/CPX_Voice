#!/usr/bin/env bash
# Deploy CarePlix Voice to the careplix-labs Google Cloud project.
# Safe to re-run: every step checks for existing resources first.
#
#   ./infra/gcp/deploy-labs.sh setup     # APIs, registry, service accounts, Cloud SQL, secrets
#   ./infra/gcp/deploy-labs.sh build     # build images with Cloud Build
#   ./infra/gcp/deploy-labs.sh openemr   # hosted OpenEMR demo: private VM, installed and seeded
#   ./infra/gcp/deploy-labs.sh deploy    # deploy/refresh the Cloud Run services
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
SA_OE_PROXY="careplix-openemr-proxy@$PROJECT.iam.gserviceaccount.com"
SA_BROWSER="careplix-browser@$PROJECT.iam.gserviceaccount.com"
OE_VM=careplix-openemr
OE_ZONE=$REGION-a
OE_PUBLIC_URL="https://careplix-openemr-$NUMBER.$REGION.run.app"
# Private egress to the OpenEMR VM; public traffic (LiveKit, Vertex, NexHealth, Stedi) is unchanged.
VPC_FLAGS="--network default --subnet default --vpc-egress private-ranges-only"
G="gcloud --project $PROJECT --quiet"

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

# careplix-labs is synthetic-only until the G4 PHI gate: never ship a production NexHealth key there.
if [ "$(env_value NEXHEALTH_ENV)" = "production" ]; then
  echo "✗ .env has a production NexHealth key; careplix-labs takes the sandbox key only (G4 PHI gate)." >&2
  exit 1
fi
# Same for Stedi: a production key reaches real payers with real patients. careplix-labs runs Stedi
# in test mode only (mock members), so refuse STEDI_ENV=production or any STEDI_PROD_API_KEY.
if [ "$(env_value STEDI_ENV || true)" = "production" ] || [ -n "$(env_value STEDI_PROD_API_KEY || true)" ]; then
  echo "✗ .env has a production Stedi key or STEDI_ENV=production; careplix-labs takes a Stedi test key only (G4 PHI gate)." >&2
  exit 1
fi
# The Stedi test key: STEDI_TEST_API_KEY, or the older STEDI_API_KEY (always a test key).
stedi_test_key() { env_value STEDI_TEST_API_KEY | grep . || env_value STEDI_API_KEY || true; }

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

  for sa in careplix-studio careplix-worker careplix-sandbox careplix-webhooks careplix-browser; do
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
  # Shared token between the Studio and the private browser runtime (supervised browser agent).
  $G secrets describe cpx-browser-token >/dev/null 2>&1 || python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | put_secret cpx-browser-token
  env_value LIVEKIT_API_KEY | tr -d '\n' | put_secret cpx-livekit-api-key
  env_value LIVEKIT_API_SECRET | tr -d '\n' | put_secret cpx-livekit-api-secret
  # Practice layer: NexHealth and Stedi keys from .env; a Fernet key for the encrypted mirror.
  env_value NEXHEALTH_API_KEY | tr -d '\n' | put_secret cpx-nexhealth-api-key
  stedi_test_key | tr -d '\n' | put_secret cpx-stedi-api-key
  $G secrets describe cpx-data-key >/dev/null 2>&1 ||
    python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end="")' | put_secret cpx-data-key
  # Optional: an Anthropic API key runs the browser agent's Claude on the Anthropic API, not Vertex.
  if [ -n "$(env_value ANTHROPIC_API_KEY || true)" ]; then
    env_value ANTHROPIC_API_KEY | tr -d '\n' | put_secret cpx-anthropic-api-key
    $G secrets add-iam-policy-binding cpx-anthropic-api-key --member "serviceAccount:$SA_STUDIO" --role roles/secretmanager.secretAccessor >/dev/null
  fi

  for secret in cpx-database-url cpx-session-secret cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret \
    cpx-nexhealth-api-key cpx-stedi-api-key cpx-data-key cpx-browser-token; do
    $G secrets add-iam-policy-binding "$secret" --member "serviceAccount:$SA_STUDIO" --role roles/secretmanager.secretAccessor >/dev/null
  done
  for secret in cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret; do
    $G secrets add-iam-policy-binding "$secret" --member "serviceAccount:$SA_WORKER" --role roles/secretmanager.secretAccessor >/dev/null
  done
  $G secrets add-iam-policy-binding cpx-database-url --member "serviceAccount:$SA_WEBHOOKS" --role roles/secretmanager.secretAccessor >/dev/null
  $G secrets add-iam-policy-binding cpx-browser-token --member "serviceAccount:$SA_BROWSER" --role roles/secretmanager.secretAccessor >/dev/null
  echo "setup complete"
}

build() {
  $G builds submit --config infra/gcp/cloudbuild.yaml --substitutions "_TAG=$TAG" .
  echo "built $TAG"
}

url_of() { $G run services describe "$1" --region "$REGION" --format 'value(status.url)' 2>/dev/null || true; }

oe_ip() { $G compute addresses describe careplix-openemr-ip --region "$REGION" --format 'value(address)' 2>/dev/null || true; }
oe_ssh() { $G compute ssh "$OE_VM" --zone "$OE_ZONE" --tunnel-through-iap --command "$1"; }
secret_value() { $G secrets versions access latest --secret "$1"; }

# Hosted OpenEMR demo (synthetic clinic): a VM with no public IP running infra/openemr. Staff reach it
# through the IAP-protected careplix-openemr proxy (deploy step); the Studio over the VPC.
openemr() {
  # Outbound-only internet for the private VM (image pulls, OS updates).
  $G compute routers describe careplix-nat-router --region "$REGION" >/dev/null 2>&1 ||
    $G compute routers create careplix-nat-router --network default --region "$REGION"
  $G compute routers nats describe careplix-nat --router careplix-nat-router --region "$REGION" >/dev/null 2>&1 ||
    $G compute routers nats create careplix-nat --router careplix-nat-router --region "$REGION" \
      --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
  [ -n "$(oe_ip)" ] || $G compute addresses create careplix-openemr-ip --region "$REGION" --subnet default

  # Hosted passwords live only in Secret Manager and the VM's infra/openemr/.env (mode 600).
  for s in cpx-openemr-admin-password cpx-openemr-db-root-password cpx-openemr-db-password; do
    $G secrets describe "$s" >/dev/null 2>&1 ||
      python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")' | put_secret "$s"
  done

  if ! $G compute instances describe "$OE_VM" --zone "$OE_ZONE" >/dev/null 2>&1; then
    $G compute instances create "$OE_VM" --zone "$OE_ZONE" --machine-type e2-medium \
      --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB --boot-disk-type pd-balanced \
      --network default --subnet default --private-network-ip "$(oe_ip)" --no-address \
      --no-service-account --no-scopes --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
      --metadata-from-file startup-script=infra/openemr/vm-startup.sh
  fi
  echo "Waiting for Docker on $OE_VM…"
  until oe_ssh 'sudo docker compose version' >/dev/null 2>&1; do sleep 15; done

  tar czf - infra/openemr scripts/openemr_setup.py | oe_ssh 'mkdir -p careplix && tar xzf - -C careplix'
  {
    echo "OPENEMR_ADMIN_PASSWORD=$(secret_value cpx-openemr-admin-password)"
    echo "OPENEMR_DB_ROOT_PASSWORD=$(secret_value cpx-openemr-db-root-password)"
    echo "OPENEMR_DB_PASSWORD=$(secret_value cpx-openemr-db-password)"
    echo "OPENEMR_PUBLIC_URL=$OE_PUBLIC_URL"
  } | oe_ssh 'umask 077 && cat > careplix/infra/openemr/.env'
  oe_ssh 'cd careplix && sudo docker compose -f infra/openemr/docker-compose.yml -f infra/openemr/docker-compose.cloud.yml --env-file infra/openemr/.env up -d'
  echo "Waiting for OpenEMR's first-boot install (≈ 5 min)…"
  until oe_ssh 'curl -sk -o /dev/null -w "%{http_code}" https://localhost:9300/interface/login/login.php' | grep -q 200; do
    sleep 20
  done
  oe_ssh 'cd careplix && set -a && . infra/openemr/.env && set +a &&
    sudo -E OPENEMR_COMPOSE_EXTRA=infra/openemr/docker-compose.cloud.yml python3 scripts/openemr_setup.py'

  # The API client the setup registered, for the Studio (read on the VM, never printed).
  oe_value() { oe_ssh "sudo grep -E '^$1=' careplix/.env | cut -d= -f2-" | tr -d '\r\n'; }
  oe_value OPENEMR_CLIENT_ID | put_secret cpx-openemr-client-id
  oe_value OPENEMR_CLIENT_SECRET | put_secret cpx-openemr-client-secret
  oe_value OPENEMR_FACILITY_ID | put_secret cpx-openemr-facility-id
  for s in cpx-openemr-client-id cpx-openemr-client-secret cpx-openemr-facility-id cpx-openemr-admin-password; do
    $G secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA_STUDIO" --role roles/secretmanager.secretAccessor >/dev/null
  done
  $G iam service-accounts describe "$SA_OE_PROXY" >/dev/null 2>&1 ||
    $G iam service-accounts create careplix-openemr-proxy --display-name "CarePlix Voice OpenEMR proxy"
  echo "OpenEMR is installed on $OE_VM ($(oe_ip)). Run deploy to publish $OE_PUBLIC_URL behind IAP."
}

# Secret Manager entries the services read. A missing one fails its service's deploy and stops the
# script halfway (services left on mixed builds), so check them all before changing anything.
REQUIRED_SECRETS="cpx-database-url cpx-session-secret cpx-worker-token cpx-livekit-api-key cpx-livekit-api-secret
  cpx-nexhealth-api-key cpx-stedi-api-key cpx-data-key cpx-browser-token"

deploy() {
  local livekit_url conn common_env secrets missing=""
  for s in $REQUIRED_SECRETS; do
    $G secrets describe "$s" >/dev/null 2>&1 || missing="$missing $s"
  done
  if [ -n "$missing" ]; then
    echo "✗ Missing in Secret Manager:$missing. Run ./infra/gcp/deploy-labs.sh setup first; nothing was deployed." >&2
    exit 1
  fi
  livekit_url=$(env_value LIVEKIT_URL)
  conn="$PROJECT:$REGION:$SQL_INSTANCE"

  # 1. Synthetic payer sandbox: private; only the Studio/API identity may call it.
  $G run deploy careplix-payer-sandbox --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_SANDBOX" --no-allow-unauthenticated --min-instances 0 --max-instances 1 \
    --command sh --args='-c,uvicorn careplix_api.payer_sandbox:app --host 0.0.0.0 --port $PORT'
  $G run services add-iam-policy-binding careplix-payer-sandbox --region "$REGION" \
    --member "serviceAccount:$SA_STUDIO" --role roles/run.invoker >/dev/null
  sandbox_url=$(url_of careplix-payer-sandbox)

  # VS3 AI judge: a Gemini text model; JUDGE_MODEL in .env overrides the default.
  judge_model=$(env_value JUDGE_MODEL || true)
  common_env="CAREPLIX_ENV=labs,LIVEKIT_URL=$livekit_url,GCP_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION,GEMINI_LIVE_MODEL=$MODEL,JUDGE_MODEL=${judge_model:-gemini-2.5-flash},PAYER_SANDBOX_URL=$sandbox_url/fhir,PAYER_SANDBOX_AUTH=google_id_token,CAREPLIX_RUNTIME_BUILD=$TAG"
  # Shared sandbox line for trials (non-secret): the CarePlix Twilio number and its LiveKit trunk.
  for key in SANDBOX_NUMBER SANDBOX_OUTBOUND_TRUNK_ID; do
    value=$(env_value "$key" || true)
    if [ -n "$value" ]; then common_env="$common_env,$key=$value"; fi
  done
  secrets="DATABASE_URL=cpx-database-url:latest,SESSION_SECRET=cpx-session-secret:latest,WORKER_SERVICE_TOKEN=cpx-worker-token:latest,LIVEKIT_API_KEY=cpx-livekit-api-key:latest,LIVEKIT_API_SECRET=cpx-livekit-api-secret:latest"

  # 2. Webhook ingress: the only public service. Verifies, dedupes and records IDs; no IAP, no PHI.
  $G run deploy careplix-webhooks --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_WEBHOOKS" --add-cloudsql-instances "$conn" --allow-unauthenticated \
    --set-env-vars "CAREPLIX_ENV=labs" --set-secrets "DATABASE_URL=cpx-database-url:latest" \
    --min-instances 0 --max-instances 2 \
    --command sh --args='-c,uvicorn careplix_api.practice.webhooks:app --host 0.0.0.0 --port $PORT'
  webhooks_url=$(url_of careplix-webhooks)

  # 2b. Browser runtime: private Chromium sessions for the supervised browser agent. Only the Studio
  # identity may call it (IAM) and it also checks a shared token. One always-on instance keeps
  # sessions alive while a run waits for an approval.
  $G run deploy careplix-browser --region "$REGION" --image "$REGISTRY/browser:$TAG" \
    --service-account "$SA_BROWSER" --no-allow-unauthenticated \
    --set-secrets "BROWSER_SERVICE_TOKEN=cpx-browser-token:latest" \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 2 --memory 4Gi \
    --concurrency 40 --timeout 3600
  $G run services add-iam-policy-binding careplix-browser --region "$REGION" \
    --member "serviceAccount:$SA_STUDIO" --role roles/run.invoker >/dev/null
  browser_url=$(url_of careplix-browser)
  # Optional .env overrides: BROWSER_DEFAULT_MODEL (gemini|claude), BROWSER_GEMINI_MODEL, BROWSER_CLAUDE_MODEL.
  browser_env="BROWSER_RUNTIME_URL=$browser_url,BROWSER_RUNTIME_AUTH=google_id_token"
  for key in BROWSER_DEFAULT_MODEL BROWSER_GEMINI_MODEL BROWSER_GEMINI_LOCATION BROWSER_CLAUDE_MODEL BROWSER_CLAUDE_REGION; do
    value=$(env_value "$key" || true)
    if [ -n "$value" ]; then browser_env="$browser_env,$key=$value"; fi
  done

  # 3. Studio + API + background loops, behind IAP (CarePlix Google accounts only).
  practice_env="NEXHEALTH_ENV=$(env_value NEXHEALTH_ENV),NEXHEALTH_SUBDOMAIN=$(env_value NEXHEALTH_SUBDOMAIN),NEXHEALTH_INSTITUTION_ID=$(env_value NEXHEALTH_INSTITUTION_ID),NEXHEALTH_LOCATION_ID=$(env_value NEXHEALTH_LOCATION_ID),STEDI_ENV=test,WEBHOOK_PUBLIC_URL=$webhooks_url"
  # Labs only ever gets the Stedi test key, under the test key's name (never STEDI_PROD_API_KEY).
  practice_secrets="NEXHEALTH_API_KEY=cpx-nexhealth-api-key:latest,STEDI_TEST_API_KEY=cpx-stedi-api-key:latest,DATA_ENCRYPTION_KEY=cpx-data-key:latest"
  studio_secrets="BROWSER_SERVICE_TOKEN=cpx-browser-token:latest"
  if $G secrets describe cpx-anthropic-api-key >/dev/null 2>&1; then
    studio_secrets="$studio_secrets,ANTHROPIC_API_KEY=cpx-anthropic-api-key:latest"
  fi
  oe_addr=$(oe_ip)
  if [ -n "$oe_addr" ] && $G secrets describe cpx-openemr-client-secret >/dev/null 2>&1; then
    # Hosted OpenEMR demo: API over the VPC (self-signed TLS on the private VM), screens via the proxy.
    practice_env="$practice_env,OPENEMR_BASE_URL=https://$oe_addr,OPENEMR_USERNAME=admin,OPENEMR_TIMEZONE=America/New_York,OPENEMR_VERIFY_TLS=false,OPENEMR_UI_URL=$OE_PUBLIC_URL"
    practice_secrets="$practice_secrets,OPENEMR_CLIENT_ID=cpx-openemr-client-id:latest,OPENEMR_CLIENT_SECRET=cpx-openemr-client-secret:latest,OPENEMR_PASSWORD=cpx-openemr-admin-password:latest,OPENEMR_FACILITY_ID=cpx-openemr-facility-id:latest"
  fi
  $G run deploy careplix-studio --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_STUDIO" --add-cloudsql-instances "$conn" $VPC_FLAGS \
    --set-env-vars "$common_env,$practice_env,$browser_env,RUN_BACKGROUND=true,PUBLISH_REQUIRES_EVAL=true,IAP_AUDIENCE=/projects/$NUMBER/locations/$REGION/services/careplix-studio" \
    --set-secrets "$secrets,$practice_secrets,$studio_secrets" --min-instances 1 --max-instances 1 --no-cpu-throttling \
    --timeout 3600 --no-allow-unauthenticated --iap
  $G run services add-iam-policy-binding careplix-studio --region "$REGION" \
    --member "serviceAccount:service-$NUMBER@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker >/dev/null
  $G iap web add-iam-policy-binding --resource-type=cloud-run --service careplix-studio --region "$REGION" \
    --member "domain:careplix.com" --role roles/iap.httpsResourceAccessor >/dev/null

  # 4. Worker-facing internal API: same image, no background loops, IAM-only for the worker. Call
  #    claims and the receptionist's EHR tools run here, so it needs the practice settings and the
  #    data key (PhiBox) exactly like the Studio, and the VPC route to the hosted OpenEMR. One
  #    instance stays warm: every call starts with a claim here, and a cold start (~7 s) lost them.
  $G run deploy careplix-internal --region "$REGION" --image "$REGISTRY/api:$TAG" \
    --service-account "$SA_STUDIO" --add-cloudsql-instances "$conn" $VPC_FLAGS \
    --set-env-vars "$common_env,$practice_env,RUN_BACKGROUND=false,SEED_DEMO_DATA=false" \
    --set-secrets "$secrets,$practice_secrets" --min-instances 1 --max-instances 2 --no-allow-unauthenticated
  $G run services add-iam-policy-binding careplix-internal --region "$REGION" \
    --member "serviceAccount:$SA_WORKER" --role roles/run.invoker >/dev/null
  internal_url=$(url_of careplix-internal)

  # 5. Voice worker: always on, one instance, Vertex AI through its own identity (no keys).
  # GEMINI_CONTEXT_COMPRESSION=false in .env turns long-session compression off (default on).
  compression=$(env_value GEMINI_CONTEXT_COMPRESSION || true)
  $G run deploy careplix-worker --region "$REGION" --image "$REGISTRY/worker:$TAG" \
    --service-account "$SA_WORKER" --no-allow-unauthenticated --ingress internal \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 2 --memory 2Gi \
    --set-env-vars "CAREPLIX_ENV=labs,LIVEKIT_URL=$livekit_url,CAREPLIX_API_URL=$internal_url,CAREPLIX_API_AUDIENCE=$internal_url,GCP_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION,GEMINI_LIVE_MODEL=$MODEL,GEMINI_CONTEXT_COMPRESSION=${compression:-true},CAREPLIX_RUNTIME_BUILD=$TAG" \
    --set-secrets "WORKER_SERVICE_TOKEN=cpx-worker-token:latest,LIVEKIT_API_KEY=cpx-livekit-api-key:latest,LIVEKIT_API_SECRET=cpx-livekit-api-secret:latest"

  # 6. OpenEMR demo screens: nginx in front of the private VM, behind IAP (CarePlix accounts only).
  if [ -n "$oe_addr" ]; then
    $G run deploy careplix-openemr --region "$REGION" --image "$REGISTRY/openemr-proxy:$TAG" \
      --service-account "$SA_OE_PROXY" $VPC_FLAGS --set-env-vars "OPENEMR_UPSTREAM=$oe_addr:80" \
      --min-instances 0 --max-instances 2 --no-allow-unauthenticated --iap
    $G run services add-iam-policy-binding careplix-openemr --region "$REGION" \
      --member "serviceAccount:service-$NUMBER@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker >/dev/null
    $G iap web add-iam-policy-binding --resource-type=cloud-run --service careplix-openemr --region "$REGION" \
      --member "domain:careplix.com" --role roles/iap.httpsResourceAccessor >/dev/null
    echo "OpenEMR: $OE_PUBLIC_URL (sign in as admin; password in Secret Manager: cpx-openemr-admin-password)"
  fi

  echo "Studio: $(url_of careplix-studio)"
  echo "Webhooks: $webhooks_url (register from Studio → Practice once deployed)"
}

sandbox_line() {
  # LiveKit side of the shared sandbox line: an inbound trunk for SANDBOX_NUMBER (and a dispatch
  # rule for it). The Twilio side is one console step the script prints at the end.
  uv run python scripts/sandbox_line.py
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  openemr) openemr ;;
  deploy) deploy ;;
  sandbox-line) sandbox_line ;;
  *) echo "usage: $0 setup|build|openemr|deploy|sandbox-line" >&2; exit 2 ;;
esac
