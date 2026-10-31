locals {
  name   = "careplix-voice-${var.environment}"
  labels = { app = "careplix-voice", env = var.environment }

  services = [
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtasks.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
  ]

  # Secret values are added out of band (gcloud secrets versions add), never through Terraform state.
  secrets = ["admin-api-token", "database-url", "livekit-api-key", "livekit-api-secret"]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# --- Identities (least privilege, doc 07 SEC03/SEC14) -------------------------

resource "google_service_account" "api" {
  account_id   = "careplix-api"
  display_name = "CarePlix Voice API (${var.environment})"
}

# Identity the LiveKit-hosted worker uses for Vertex AI. Federation is preferred; if LiveKit Cloud
# cannot federate (BUILD_PLAN R2), a key for this account is held as a LiveKit agent secret and rotated.
resource "google_service_account" "worker_vertex" {
  account_id   = "careplix-worker-vertex"
  display_name = "CarePlix agent worker - Vertex AI only (${var.environment})"
}

resource "google_project_iam_member" "worker_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.worker_vertex.email}"
}

resource "google_project_iam_member" "api_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# --- Secrets -----------------------------------------------------------------

resource "google_secret_manager_secret" "app" {
  for_each  = toset(local.secrets)
  secret_id = "${local.name}-${each.value}"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "api_access" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# --- Data --------------------------------------------------------------------

resource "google_sql_database_instance" "main" {
  name                = local.name
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = "db-custom-1-3840"
    edition           = "ENTERPRISE"
    availability_type = "ZONAL" # REGIONAL for pilot (RPO 15 min / RTO 4 h objectives).
    user_labels       = local.labels

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      # No authorised networks: access via the Cloud SQL connector with IAM. Private IP before pilot.
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }
  depends_on = [google_project_service.enabled]
}

resource "google_sql_database" "app" {
  name     = "careplix"
  instance = google_sql_database_instance.main.name
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-${local.name}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  lifecycle_rule {
    condition {
      age            = var.content_retention_days
      matches_prefix = ["content/"]
    }
    action {
      type = "Delete"
    }
  }
}

# --- Async commands (outbox relay target, ADR05 / BUILD_PLAN 5.3) -------------

resource "google_cloud_tasks_queue" "commands" {
  name     = "${local.name}-commands"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 5
  }
  retry_config {
    max_attempts = 5
    min_backoff  = "2s"
    max_backoff  = "60s"
  }
  depends_on = [google_project_service.enabled]
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "careplix-voice"
  location      = var.region
  format        = "DOCKER"
  labels        = local.labels
  depends_on    = [google_project_service.enabled]
}

# --- API service -------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  count    = var.api_image == "" ? 0 : 1
  name     = "careplix-api"
  location = var.region
  labels   = local.labels

  template {
    service_account = google_service_account.api.email
    timeout         = "3600s" # long-lived SSE streams for live call views

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }

    containers {
      image = var.api_image

      env {
        name  = "CAREPLIX_ENV"
        value = var.environment
      }

      dynamic "env" {
        for_each = {
          ADMIN_API_TOKEN    = "admin-api-token"
          DATABASE_URL       = "database-url"
          LIVEKIT_API_KEY    = "livekit-api-key"
          LIVEKIT_API_SECRET = "livekit-api-secret"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
  }
}
