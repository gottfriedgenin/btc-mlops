# `docs/architecture/` — Cloud + cluster resource map

A single PNG that maps every GCP resource we provision and every GKE workload
that touches it, with the actual data + identity + sync edges between them.

Regenerate after any infra / platform change:

```bash
brew install graphviz         # one-off, MacOS
pip install diagrams          # one-off
python3 docs/architecture/architecture.py
```

Output: [`btc-mlops-architecture.png`](./btc-mlops-architecture.png)

## Edge legend

| Color / style                | Meaning                                                |
|------------------------------|--------------------------------------------------------|
| **blue dashed**              | GitOps sync (GitHub → Argo) and deploy (Argo → workload) |
| **purple bold**              | Container image push / pull (Artifact Registry)        |
| **green bold**                | Workload Identity binding (KSA ↔ GCP SA)               |
| **red solid**                | Data read/write (BigQuery / GCS)                       |
| **orange solid**             | HTTP (predict client, MLflow tracking / registry)      |
| **grey dashed**              | Prometheus scrape                                      |
| **grey dotted**              | Cold storage / occasional writes (e.g. eval html exports) |

## When to update it

- A new namespace, KSA, or Workload Identity binding lands in `infra/modules/iam/main.tf` or `platform/helm/*/templates/`.
- A new GCS bucket / BigQuery dataset / Artifact Registry repo is added in `infra/modules/storage`, `infra/modules/bigquery`, `infra/modules/registry`.
- A new Argo CD `Application` / `ApplicationSet` is added under `platform/argocd/apps/`.

If the change is purely Helm values (replicas, refresh interval, etc.) — no diagram update needed.

## What it deliberately omits

- Pod-level details inside each namespace (replicas, sidecars, CRDs).
- Backing storage of Argo CD / KFP / MLflow themselves (`PVC`, in-cluster Postgres).
- VPC subnets, firewall rules, secondary ranges (all in `infra/modules/network`).
- IAM role-level grants — see [`infra/README.md`](../../infra/README.md) for the SA → role matrix.

The goal is **runtime data flow**, not a 1:1 mirror of Terraform state.
