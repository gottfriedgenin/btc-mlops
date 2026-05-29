variable "project_id"           { type = string }
variable "models_bucket"        { type = string }
variable "data_features_bucket" { type = string }
variable "docs_bucket"          { type = string }
variable "mlflow_bucket"        { type = string }
variable "bq_dataset"           { type = string }
variable "bq_snapshots_dataset" { type = string }

# Project lookup — used to resolve project_number for the compute default SA
# that the GKE node pools run as (image pulls happen with the node identity,
# not the pod's Workload Identity).
data "google_project" "this" {
  project_id = var.project_id
}

# Service accounts soft-delete for 30 days. Recreating with the same account_id
# during that window errors 409 and forces an undelete + import.
# Protect the three identity-layer SAs from destroy. Flip prevent_destroy to false
# manually only when retiring the GCP project.
resource "google_service_account" "training" {
  account_id   = "training-job-sa"
  display_name = "Training job SA"

  lifecycle {
    prevent_destroy = true
  }
}
resource "google_service_account" "serving" {
  account_id   = "serving-sa"
  display_name = "Serving SA"

  lifecycle {
    prevent_destroy = true
  }
}
resource "google_service_account" "github_ci" {
  account_id   = "github-ci-sa"
  display_name = "GitHub Actions CI SA"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket_iam_member" "training_features_rw" {
  bucket = var.data_features_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.training.email}"
}
resource "google_storage_bucket_iam_member" "training_models_write" {
  bucket = var.models_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.training.email}"
}
resource "google_storage_bucket_iam_member" "training_docs_read" {
  bucket = var.docs_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.training.email}"
}
resource "google_storage_bucket_iam_member" "serving_models_read" {
  bucket = var.models_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.serving.email}"
}

# MLflow stores all run artifacts (models, plots, JSONs) under
# gs://<project>-mlflow/. The MLflow tracking server uploads on behalf of the
# *caller* (the KFP train pod's WI identity), not the MLflow pod, so the
# training SA needs object write access to this bucket.
resource "google_storage_bucket_iam_member" "training_mlflow_write" {
  bucket = var.mlflow_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.training.email}"
}

# BigQuery — raw klines
resource "google_bigquery_dataset_iam_member" "training_bq_editor" {
  dataset_id = var.bq_dataset
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.training.email}"
}
# Per-run snapshots dataset (CREATE SNAPSHOT TABLE writes here).
# dataOwner (not dataEditor) because CREATE SNAPSHOT TABLE needs
# `bigquery.tables.deleteSnapshot` which is only in dataOwner/admin.
resource "google_bigquery_dataset_iam_member" "training_bq_snapshots_owner" {
  dataset_id = var.bq_snapshots_dataset
  role       = "roles/bigquery.dataOwner"
  member     = "serviceAccount:${google_service_account.training.email}"
}
resource "google_project_iam_member" "training_bq_jobuser" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.training.email}"
}

# Workload Identity bindings (K8s SA → GCP SA)
resource "google_service_account_iam_member" "training_wi_kubeflow" {
  service_account_id = google_service_account.training.name
  role               = "roles/iam.workloadIdentityUser"
  # KFP installs the `pipeline-runner` KSA and launches every workflow pod
  # with it. Annotating that KSA (see platform/helm/kfp-workload-identity)
  # binds it to this GCP SA via Workload Identity.
  member             = "serviceAccount:${var.project_id}.svc.id.goog[kubeflow/pipeline-runner]"
}
resource "google_service_account_iam_member" "serving_wi" {
  service_account_id = google_service_account.serving.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[serving/btc-serving]"
}

# GKE node SA: image pulls from Artifact Registry happen with the node's
# identity, NOT the pod's Workload Identity binding. Node pools use the
# Compute Engine default SA (<project_number>-compute@developer...), which
# needs read access to AR to pull our private images.
resource "google_project_iam_member" "gke_nodes_ar_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

# GitHub Actions CI permissions
resource "google_project_iam_member" "github_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.github_ci.email}"
}
resource "google_project_iam_member" "github_gke_dev" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.github_ci.email}"
}

output "training_sa_email"  { value = google_service_account.training.email }
output "serving_sa_email"   { value = google_service_account.serving.email }
output "github_ci_sa_email" { value = google_service_account.github_ci.email }