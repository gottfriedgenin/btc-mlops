terraform {
  backend "gcs" {
    bucket = "gg-genops-tf-state"
    prefix = "dev"
  }
  required_version = ">= 1.15.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.33.0" }
  }
}