variable "project_id" { type = string }
variable "region"     { type = string }

resource "google_artifact_registry_repository" "btc" {
  location      = var.region
  repository_id = "btc-mlops"
  format        = "DOCKER"
  description   = "BTC MLOps images"

  # Destroying the repo nukes every pushed image. Recreating the name is fine
  # (no soft-delete window on AR), but every service has to be rebuilt by CI
  # before any pod can pull. Protect.
  lifecycle {
    prevent_destroy = true
  }
}

output "registry_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.btc.repository_id}"
}