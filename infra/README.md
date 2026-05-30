# `infra/` — Terraform for everything *outside* the cluster

Buckets, BigQuery datasets, service accounts, IAM, GKE cluster + node pools,
Artifact Registry, VPC. None of this lives in Argo. The platform-layer
([`../platform/`](../platform/README.md)) installs *into* the cluster Terraform
creates here.

```
infra/
├── envs/
│   └── dev/             # Single env today. Each env has its own state + tfvars.
│       ├── main.tf      # Wires the modules together
│       ├── variables.tf
│       └── backend.tf   # remote state backend (GCS)
└── modules/
    ├── network/         # VPC + subnets + secondary ranges for GKE
    ├── gke/             # cluster + node pools (CPU + cpu-burst + GPU optional)
    ├── storage/         # GCS buckets: data-features, models, mlflow, docs
    ├── bigquery/        # datasets: btc_raw, btc_snapshots
    ├── registry/        # Artifact Registry repo `btc-mlops`
    └── iam/             # GCP SAs + Workload Identity bindings + dataset/bucket IAM
```

## SAs in `iam/`

| SA | Used by | Key roles |
|---|---|---|
| `training-job-sa`   | KFP pipeline pods (via Workload Identity → `kubeflow/pipeline-runner`) | bq dataEditor on btc_raw, dataOwner on btc_snapshots, jobUser at project, objectAdmin on models + features + mlflow buckets, objectViewer on docs |
| `serving-sa`        | Phase 5 serving pods (WI → `serving/btc-serving`)                       | bq dataViewer on btc_raw, jobUser, objectViewer on models |
| `github-ci-sa`      | GitHub Actions CI (future, Phase 7, via WIF — no key in repo)           | artifactregistry.writer, container.developer |

Workload Identity bindings (`<project>.svc.id.goog[<ns>/<ksa>]`) are in
`iam/main.tf`. Add a new SA or KSA there and re-`terraform apply`.

## Apply / destroy

```bash
cd infra/envs/dev
terraform init -backend-config=<your-tfstate-bucket>   # one-off
terraform plan
terraform apply

# Surgical destroy (e.g. tear down GKE but keep buckets + IAM):
terraform destroy -target=module.gke -auto-approve
```

The SAs and bucket IAM survive `module.gke` destroy. Recreating the
cluster + re-running `platform/argocd/bootstrap.sh` is enough — no
re-grants needed.

## Prevent-destroy guardrails

The three identity-layer SAs (`training`, `serving`, `github_ci`) have
`lifecycle { prevent_destroy = true }`. Google soft-deletes SAs for 30 days,
so recreating an SA with the same `account_id` inside that window throws
409 and forces undelete + import. Flip the flag to `false` manually only
when retiring the GCP project.
