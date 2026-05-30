# `platform/helm/btc-serving/` — Helm chart for the Phase 5 serving app

In-cluster deployment of the FastAPI service in
[`src/serving/`](../../../src/serving/README.md). Pulled in by the
[`btc-serving` ApplicationSet](../../argocd/apps/btc-serving.yaml).

## Why an ApplicationSet, not a plain Application

Same pattern as
[`kfp-workload-identity`](../kfp-workload-identity/) — keeps the
GCP project id out of git. The ApplicationSet's `clusters: {}` generator
reads the `gcp_project` annotation off the in-cluster Argo Secret (set by
`platform/argocd/bootstrap.sh`) and passes it to this chart as a Helm
parameter. The chart then derives `modelsBucket = ${gcpProject}-models`
and the image repository, and writes the Workload Identity annotation on
the KSA.

## Templates

| File | Contents |
|---|---|
| `templates/serviceaccount.yaml` | KSA `serving/btc-serving` with `iam.gke.io/gcp-service-account` → GCP `serving-sa@${gcpProject}` |
| `templates/deployment.yaml`     | Single replica, FastAPI + uvicorn, env wired to values, `/health` readiness + liveness, prom scrape annotations on pod |
| `templates/service.yaml`        | `ClusterIP :8000` named `btc-serving` |

## Values

| Key | Default | Notes |
|---|---|---|
| `namespace`           | `serving`     | created via Argo `CreateNamespace=true` |
| `ksaName`             | `btc-serving` | must match the Workload Identity binding in `infra/modules/iam/main.tf` |
| `gcpProject`          | placeholder   | **required** override; set by ApplicationSet |
| `gcpServiceAccount`   | `serving-sa`  | base part of the GCP SA email |
| `modelsBucket`        | placeholder   | **required** override; set by ApplicationSet as `${gcpProject}-models` |
| `image.repository`    | placeholder   | **required** override; set by ApplicationSet |
| `image.tag`           | `latest`      | swap to a digest for prod |
| `image.pullPolicy`    | `Always`      | so a re-pushed `:latest` is picked up |
| `replicas`            | `1`           | bump once we have HPA / load |
| `resources.*`         | 0.2–1 CPU, 0.5–2 GiB | XGBoost predict + tiny BQ result, comfortable |
| `mlflowUri`           | `http://mlflow.mlflow.svc.cluster.local:5000` | in-cluster |
| `modelRefreshSeconds` | `600`         | background poll of MLflow registry |
| `service.type`        | `ClusterIP`   | swap to `LoadBalancer` / add Ingress when going public |
| `service.port`        | `8000`        | matches container port |

## Render locally

```bash
helm template btc-serving platform/helm/btc-serving \
  --set gcpProject=<project> \
  --set modelsBucket=<project>-models \
  --set image.repository=europe-west3-docker.pkg.dev/<project>/btc-mlops/serving
```

## Smoke test once deployed

```bash
make serving-port-forward      # shell A
make serving-predict           # shell B  → P10/P50/P90 band JSON
```

## Workload Identity & IAM

The `serving-sa` GCP SA is created in `infra/modules/iam/main.tf` with:

* `roles/iam.workloadIdentityUser` for `serviceAccount:<project>.svc.id.goog[serving/btc-serving]`
* `roles/storage.objectViewer` on `<project>-models`
* `roles/bigquery.dataViewer` on `btc_raw`
* `roles/bigquery.jobUser` at project level

Recreate of the cluster doesn't touch any of those — they live on the project.
