variable "project_id" {
  description = "Dedicated GCP project for this environment (dev, staging and prod are separate projects)."
  type        = string
}

variable "region" {
  description = "Application region. Must be compatible with the approved Vertex AI Gemini Live location (OD04)."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "api_image" {
  description = "Container image for the API. Leave empty to skip the Cloud Run service until an image exists."
  type        = string
  default     = ""
}

variable "content_retention_days" {
  description = "Synthetic-demo default for stored content (doc 07 SEC10). Not a statutory value."
  type        = number
  default     = 7
}
