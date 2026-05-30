# `src/serving/` — Phase 5 prediction HTTP service

FastAPI app that serves the Production-stage `btc-quantile` model.
Caller hits `GET /predict` → service pulls latest BQ features, runs the
P10/P50/P90 quantile triple, returns a forecast band.

Image: `ci/docker/serving.Dockerfile` → `…/btc-mlops/serving:latest`
Helm chart: [`platform/helm/btc-serving/`](../../platform/helm/btc-serving/README.md)
Argo app:  [`platform/argocd/apps/btc-serving.yaml`](../../platform/argocd/apps/btc-serving.yaml)

## Files

| File | Role |
|---|---|
| `predict.py` | `Predictor` class. Thread-safe model swap. Loads Production model from MLflow registry, pulls features from BQ, returns forecast dict. |
| `app.py`     | FastAPI app. `/health`, `/predict?horizon=N`, `/metrics`. Background thread polls MLflow every `MODEL_REFRESH_SECONDS` and swaps atomically when version changes. |
| `__init__.py`| package marker |

## Endpoints

```
GET /health
  → {"ok": true, "model_version": "1", "model_run_id": "..."}

GET /predict?horizon=3
  → {
      "horizon_days":   3,
      "as_of":         "2025-12-31",
      "predict_for":   "2026-01-03",
      "close":          93840.12,
      "p10_price":      88234.51,
      "p50_price":      94120.07,
      "p90_price":     100456.89,
      "p10_logret":    -0.0612,
      "p50_logret":     0.0030,
      "p90_logret":     0.0682,
      "model_version": "btc-quantile/1",
      "model_run_id":  "...",
      "data_snapshot": "live"
    }

GET /metrics  → Prometheus exposition
```

## Required env

| Var | Default | Purpose |
|---|---|---|
| `GCP_PROJECT`           | *(required)* | BQ + GCS client billing project |
| `MODELS_BUCKET`         | *(required)* | `gs://${MODELS_BUCKET}/btc/${run_id}/` is where train.py uploads model files |
| `MLFLOW_URI`            | `http://mlflow.mlflow.svc.cluster.local:5000` | Tracking + registry |
| `MODEL_REFRESH_SECONDS` | `600` | How often the background thread polls MLflow for a new Production version |

## Workload Identity

KSA `serving/btc-serving` → GCP SA `serving-sa@<project>` (binding declared in
`infra/modules/iam/main.tf`). The SA has:

| Role | Scope | Why |
|---|---|---|
| `roles/storage.objectViewer` | `<project>-models` bucket | download model artifacts |
| `roles/bigquery.dataViewer`  | `btc_raw` dataset          | read live unified table |
| `roles/bigquery.jobUser`     | project                    | run the SELECT |

## Local dev

```bash
export GCP_PROJECT=<your project>
export MODELS_BUCKET=${GCP_PROJECT}-models
export MLFLOW_URI=http://localhost:5000   # via `make mlflow-port-forward`
poetry run uvicorn src.serving.app:app --reload --port 8000
curl -s http://localhost:8000/predict | jq
```

## Inside the cluster

```bash
make serving-port-forward   # one shell  → http://localhost:8000
make serving-predict        # another    → curl /predict | json.tool
```

## Model lifecycle

`Predictor.refresh()` calls `MlflowClient.get_latest_versions(name,
stages=["Production"])`. The KFP `register-op` is what promotes a new
version to Production (see `pipelines/training/pipeline.py`). So:

1. KFP pipeline trains + promotes → MLflow registry has new Production version
2. Within `MODEL_REFRESH_SECONDS`, serving's background thread spots the new
   version, downloads the artifact from `gs://${MODELS_BUCKET}/btc/${run_id}/`,
   and swaps the in-memory `QuantileTriple` under a lock
3. Next `/predict` uses the new model. No pod restart needed.

## Forecast math

`predict_band()` returns log-returns at three quantiles. The HTTP response
reconstructs prices as `close * exp(p{10,50,90})` — same formula the
notebooks and `src/eval/metrics.py` use.
