variable "github_repo" {
  type        = string
  description = "owner/repo (e.g. yourname/btc-mlops)"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Pool"

  # WIF pools soft-delete for 30 days on destroy. Recreating with the same id
  # then errors with 409 ALREADY_EXISTS and forces undelete + import dance.
  # Keep this resource immortal; tear it down explicitly only when starting
  # a fresh GCP project.
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    "attribute.actor"      = "assertion.actor"
  }

  # Required in google provider 7.x. Restrict pool to your repo only.
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri        = "https://token.actions.githubusercontent.com"
    allowed_audiences = ["https://iam.googleapis.com/projects/-/locations/global/workloadIdentityPools/github-pool/providers/github-provider"]
  }

  # Same 30d soft-delete behavior as the pool.
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account_iam_member" "github_wif" {
  service_account_id = google_service_account.github_ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

output "wif_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}