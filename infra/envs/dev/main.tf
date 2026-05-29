provider "google" {
  project = var.project_id
  region  = var.region
}

module "network" {
  source     = "../../modules/network"
  project_id = var.project_id
  region     = var.region
}

module "storage" {
  source     = "../../modules/storage"
  project_id = var.project_id
  region     = var.region
}

module "bigquery" {
  source     = "../../modules/bigquery"
  project_id = var.project_id
  region     = var.region
}

module "registry" {
  source     = "../../modules/registry"
  project_id = var.project_id
  region     = var.region
}

module "gke" {
  source       = "../../modules/gke"
  project_id   = var.project_id
  region       = var.region
  cluster_name = var.cluster_name
  network      = module.network.network_name
  subnetwork   = module.network.subnetwork_name
}

module "iam" {
  source               = "../../modules/iam"
  project_id           = var.project_id
  github_repo          = var.github_repo
  models_bucket        = module.storage.models
  data_features_bucket = module.storage.data_features
  docs_bucket          = module.storage.docs
  bq_dataset           = module.bigquery.dataset_id
}
