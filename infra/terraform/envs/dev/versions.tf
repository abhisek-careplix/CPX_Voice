terraform {
  required_version = ">= 1.9"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Configure remote state before first apply (bucket created out of band, versioned, CMEK optional):
  # backend "gcs" {
  #   bucket = "<org>-careplix-voice-tfstate"
  #   prefix = "envs/dev"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
