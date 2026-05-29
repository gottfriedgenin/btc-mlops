variable "project_id"   { type = string }
variable "region"       { type = string }
variable "cluster_name" { type = string }
variable "network"      { type = string }
variable "subnetwork"   { type = string }

resource "google_container_cluster" "primary" {
  name                     = var.cluster_name
  location                 = var.region
  network                  = var.network
  subnetwork               = var.subnetwork
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
  release_channel { channel = "REGULAR" }

  # Org policy compute.vmExternalIpAccess blocks public IPs on VMs.
  # Private nodes = no external IP. Egress to internet (Binance, Helm charts,
  # container registries) goes via Cloud NAT (created in network module).
  # Public endpoint on the control plane stays on so `kubectl` from laptop
  # still works without a bastion / VPN.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  # Allow your laptop's public IP to reach the control plane API.
  # Tighten this in prod; wide-open for a pet project is acceptable but loud.
  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = "0.0.0.0/0"
      display_name = "all"
    }
  }
}

resource "google_container_node_pool" "system" {
  name     = "system-pool"
  cluster  = google_container_cluster.primary.name
  location = var.region
  autoscaling {
    min_node_count = 1
    max_node_count = 3
  }
  node_config {
    machine_type = "e2-standard-4"
    disk_size_gb = 50

    # COST: spot ~60-91% cheaper than on-demand.
    # NOT FOR PROD: GCP may preempt with 30s notice. When ALL system nodes
    # evict simultaneously, cluster control-plane add-ons (ingress, prom,
    # mlflow, kube-dns) restart together → brief cluster-wide outage.
    # Acceptable for a pet/portfolio project;
    spot = true

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config { mode = "GKE_METADATA" }
  }
}

resource "google_container_node_pool" "cpu_burst" {
  name     = "cpu-burst-pool"
  cluster  = google_container_cluster.primary.name
  location = var.region
  autoscaling {
    min_node_count = 0
    max_node_count = 4
  }
  node_config {
    machine_type = "e2-standard-8"

    # COST: spot. Used by KFP pipeline steps + batch jobs — preemption
    # restarts the step, not the cluster. Cheap and safe.
    # NOT FOR PROD: long-running training jobs may need on-demand fallback.
    spot = true

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config { mode = "GKE_METADATA" }
  }
}

resource "google_container_node_pool" "gpu" {
  name     = "gpu-pool"
  cluster  = google_container_cluster.primary.name
  location = var.region

  # T4 isn't sold in europe-west3 (Frankfurt). L4 is, in zone -b.
  # Pin the pool to the L4-bearing zone so regional cluster doesn't try
  # to schedule GPU nodes in -a or -c and fail with "accelerator type does not exist".
  node_locations = ["europe-west3-b"]

  autoscaling {
    min_node_count = 0
    max_node_count = 1
  }
  node_config {
    # L4 attaches only to the g2- machine family.
    machine_type = "g2-standard-4"
    labels       = { workload = "gpu" }

    # COST: spot GPU = up to 70% cheaper than on-demand L4.
    # NOT FOR PROD: GPU spot has tighter capacity; preempt rate higher.
    # Mid-training preemption = restart from last MLflow checkpoint.
    # Mitigate: short epochs + frequent checkpoint upload to GCS.
    # For prod inference, use on-demand or reservations.
    spot = true

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    guest_accelerator {
      type  = "nvidia-l4"
      count = 1
    }

    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config { mode = "GKE_METADATA" }
  }
}

output "cluster_name" { value = google_container_cluster.primary.name }