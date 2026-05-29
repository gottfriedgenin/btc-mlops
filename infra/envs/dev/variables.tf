variable "bucket" {
  description = "The GCS bucket to store the Terraform state"
  type        = string
  sensitive   = true
}

variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "europe-west3"
}
variable "cluster_name" {
  type = string
}

variable "github_repo" {
  type = string
}