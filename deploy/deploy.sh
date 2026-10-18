#!/usr/bin/env bash
# Build and deploy Decision Ledger to Cloud Run behind Firebase Hosting (DL-006).
# Idempotent: existing service account, roles and secrets are kept. Secret values are generated
# straight into Secret Manager and never printed or written to disk.
set -euo pipefail

PROJECT="${PROJECT:-decision-ledger-cup}"
REGION="${REGION:-asia-east1}"
SERVICE="${SERVICE:-decision-ledger}"
SA_NAME="${SA_NAME:-dl-run}"
# One instance during judging (commander decision 2026-10-17). Since DL-032 the ledger is in Firestore and a
# concurrent unit of work from another instance is rejected with 409 instead of being lost. "Reset my workspace"
# is not covered by that check (it deletes in batches), so keep 1 until reset bumps the version first; Ci decides.
MAX_INSTANCES="${MAX_INSTANCES:-1}"
# Firebase Authentication (DL-032): the web API key is public but is not committed; pass it at deploy time
# (Firebase console > Project settings > Your apps). The project id is the Firebase project id.
FIREBASE_API_KEY="${FIREBASE_API_KEY:-}"
FIREBASE_AUTH_DOMAIN="${FIREBASE_AUTH_DOMAIN:-${PROJECT}.firebaseapp.com}"
# CPU_ALWAYS=0 deploys with request-based CPU (cheaper) while the webhook is still synchronous.
CPU_ALWAYS="${CPU_ALWAYS:-1}"
PUBLIC_URL="${PUBLIC_URL:-https://decision-ledger-cup.web.app}"
DEMO_REPO="${DEMO_REPO:-ci-yang/decision-ledger-demo-repo}"
# Cloud Run URL without the project number (DL-030): the browser sends long LLM requests here to avoid
# Firebase Hosting's 60 s limit. Find it with: gcloud run services describe decision-ledger --format='value(metadata.annotations)'
API_BASE="${API_BASE:-https://decision-ledger-cpgrpijo3a-de.a.run.app}"
FIREBASE="${FIREBASE:-firebase}"
TAG="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- src pyproject.toml uv.lock Dockerfile || echo -dirty)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/app/${SERVICE}:${TAG}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
STEP="${1:-all}"   # all | infra | build | firestore | run | hosting

cd "$(dirname "$0")/.."

infra() {
  gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "Decision Ledger Cloud Run"
  for role in roles/aiplatform.user roles/datastore.user roles/secretmanager.secretAccessor; do
    gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${SA}" --role "$role" \
      --condition=None --quiet >/dev/null
  done
  for secret in dl-webhook-secret dl-sandbox-salt; do
    if ! gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
      openssl rand -hex 32 | tr -d '\n' \
        | gcloud secrets create "$secret" --project "$PROJECT" --replication-policy automatic --data-file=- >/dev/null
    fi
  done
}

build() {
  gcloud builds submit --project "$PROJECT" --region "$REGION" --tag "$IMAGE" .
}

run() {
  local env_vars
  # 180 s: google-genai forwards the timeout as the Vertex server deadline; 60 s produced 504s on slow calls.
  env_vars="DL_LLM_TIMEOUT=180,DL_LLM_BACKEND=gemini,DL_GEMINI_VERTEXAI=1,DL_GEMINI_MODEL=gemini-3.5-flash,DL_GEMINI_LOCATION=global"
  env_vars+=",GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global"
  env_vars+=",DL_PUBLIC_URL=${PUBLIC_URL},DL_CORS_ORIGINS=${PUBLIC_URL},DL_API_BASE=${API_BASE}"
  env_vars+=",DL_PUBLIC_MODE=1,DL_FIRESTORE=1,DL_SHOWCASE_REBUILD=1,DL_STORE=firestore"
  if [[ -z "$FIREBASE_API_KEY" ]]; then
    echo "FIREBASE_API_KEY is not set: the web UI cannot offer Google sign-in (see docs/gcp-setup.md)" >&2
    exit 1
  fi
  env_vars+=",DL_AUTH_MODE=firebase,DL_FIREBASE_PROJECT_ID=${PROJECT},DL_FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN}"
  env_vars+=",DL_FIREBASE_API_KEY=${FIREBASE_API_KEY}"
  env_vars+=",DL_PROJECT_NAME=Showcase (Synthetic),DL_PROJECT_REPO=${DEMO_REPO}"
  # --no-cpu-throttling is for DL-003 (Lane A): once the webhook returns 202 and runs deviation detection in
  # FastAPI BackgroundTasks, request-based CPU would stall that work after the response. Until DL-003 is
  # deployed the webhook runs synchronously and this flag only adds cost (one instance billed all day).
  local cpu_flag="--no-cpu-throttling"
  [[ "$CPU_ALWAYS" == 1 ]] || cpu_flag="--cpu-throttling"
  gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" "$cpu_flag" \
    --service-account "$SA" --allow-unauthenticated --port 8080 --memory 1Gi \
    --min-instances 1 --max-instances "$MAX_INSTANCES" --concurrency 20 --timeout 300 \
    --set-env-vars "^@^${env_vars//,/@}" \
    --set-secrets "DL_WEBHOOK_SECRET=dl-webhook-secret:latest,DL_SANDBOX_SALT=dl-sandbox-salt:latest"
}

firestore() {
  # DL-032: deny-all client rules and the (empty) index list; the backend uses single-field queries only.
  "$FIREBASE" deploy --only firestore:rules,firestore:indexes --project "$PROJECT"
}

hosting() {
  "$FIREBASE" deploy --only hosting --project "$PROJECT"
}

case "$STEP" in
  all) infra; build; firestore; run; hosting ;;
  infra) infra ;;
  build) build ;;
  firestore) firestore ;;
  run) run ;;
  hosting) hosting ;;
  *) echo "usage: $0 [all|infra|build|firestore|run|hosting]" >&2; exit 64 ;;
esac
