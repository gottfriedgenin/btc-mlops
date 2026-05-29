"""Score the trained model on the untouched 2026 holdout. Once per training run."""
import argparse, json, tempfile
import pandas as pd
import mlflow
from mlflow.tracking import MlflowClient
from google.cloud import storage
from src.train.model import QuantileTriple
from src.features.build import build
from src.features.labels import label_regression
from src.eval.metrics import band_metrics


def download_model(bucket: str, prefix: str, dest: str) -> None:
    bkt = storage.Client().bucket(bucket)
    for blob in bkt.list_blobs(prefix=prefix):
        local = f"{dest}/{blob.name.rsplit('/', 1)[-1]}"
        blob.download_to_filename(local)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow-uri",  required=True)
    ap.add_argument("--experiment",  default="btc-quantile")
    ap.add_argument("--holdout-bq",  required=True, help="`PROJECT.btc_raw.dataset_unified_holdout` or same table filtered to 2026+")
    ap.add_argument("--models-bucket", required=True)
    ap.add_argument("--horizon", type=int, default=3)
    a = ap.parse_args()

    mlflow.set_tracking_uri(a.mlflow_uri)
    cli = MlflowClient()
    exp = cli.get_experiment_by_name(a.experiment)
    run = cli.search_runs([exp.experiment_id], order_by=["start_time DESC"], max_results=1)[0]
    run_id = run.info.run_id

    from google.cloud import bigquery
    bq = bigquery.Client()
    df = bq.query(f"SELECT * FROM `{a.holdout_bq}` WHERE timestamp >= '2026-01-01' ORDER BY timestamp").to_dataframe()
    feats = build(df)
    feats = label_regression(feats, horizon=a.horizon)

    with tempfile.TemporaryDirectory() as td:
        download_model(a.models_bucket, f"btc/{run_id}", td)
        triple = QuantileTriple.load(td)

    band = triple.predict_band(feats)
    m = band_metrics(band)
    print("2026 holdout:", json.dumps(m, indent=2))

    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics({f"holdout_{k}": v for k, v in m.items()})
        mlflow.log_metric("holdout_n_rows", len(band))

    # Persist outputs for downstream gating
    print(json.dumps({"run_id": run_id, **m}))


if __name__ == "__main__":
    main()
