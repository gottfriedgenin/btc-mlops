variable "project_id" { type = string }
variable "region"     { type = string }

# GCS bucket names are globally unique. Once you delete one, the name enters a
# soft-reservation window and the same project may not be able to re-create it.
# Combined with `force_destroy = true` this means a `tf destroy` permanently
# nukes all model artifacts, MLflow experiments, feature Parquets, and doc
# corpus — with no undo. Flip force_destroy off and add prevent_destroy.

resource "google_storage_bucket" "data_features" {
  name          = "${var.project_id}-data-features"
  location      = var.region
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket" "models" {
  name          = "${var.project_id}-models"
  location      = var.region
  force_destroy = false
  versioning { enabled = true }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket" "mlflow" {
  name          = "${var.project_id}-mlflow"
  location      = var.region
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

# Doc corpus for RAG (for saving text/PDF files)
resource "google_storage_bucket" "docs" {
  name          = "${var.project_id}-docs"
  location      = var.region
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

output "data_features" { value = google_storage_bucket.data_features.name }
output "models"        { value = google_storage_bucket.models.name }
output "mlflow"        { value = google_storage_bucket.mlflow.name }
output "docs"          { value = google_storage_bucket.docs.name }