output "api_service_account" {
  value = google_service_account.api.email
}

output "worker_vertex_service_account" {
  value = google_service_account.worker_vertex.email
}

output "sql_connection_name" {
  value = google_sql_database_instance.main.connection_name
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "commands_queue" {
  value = google_cloud_tasks_queue.commands.id
}

output "api_url" {
  value = try(google_cloud_run_v2_service.api[0].uri, null)
}
