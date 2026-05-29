"""Train quantile triple on labeled features, log to MLflow, save artifacts to GCS.

Inputs:
  --features-uri  Parquet from src.features.labels (must contain y_logret)
  --models-bucket GCS bucket for the saved model files
  --mlflow-uri    MLflow tracking server

Outputs:
  - MLflow run with metrics for train/val/test and model artifacts
  - gs://<models-bucket>/btc/<run_id>/{m10,m50,m90}.json + features.json
"""
import argparse, os, tempfile
import pandas as pd
import mlflow
from google.cloud import storage
from src.train.model import QuantileTriple
from src.features.build import feature_columns
from src.features.split import split
from src.eval.metrics import band_metrics


def upload_dir(local: str, bucket: str, prefix: str) -> None:
    bkt = storage.Client().bucket(bucket)
    for fn in os.listdir(local):
        bkt.blob(f"{prefix}/{fn}").upload_from_filename(f"{local}/{fn}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features-uri",  required=True)
    ap.add_argument("--models-bucket", required=True)
    ap.add_argument("--mlflow-uri",    required=True)
    ap.add_argument("--experiment",    default="btc-quantile")
    ap.add_argument("--train-end",     default="2024-12-31")
    ap.add_argument("--val-end",       default="2025-09-30")
    ap.add_argument("--data-snapshot", default="live",
                    help="BQ snapshot id; logged to MLflow as run param")
    a = ap.parse_args()

    mlflow.set_tracking_uri(a.mlflow_uri)
    mlflow.set_experiment(a.experiment)

    df = pd.read_parquet(a.features_uri)
    tr, va, te = split(df, train_end=a.train_end, val_end=a.val_end)
    feats = feature_columns(df)
    print(f"train={len(tr)} val={len(va)} test={len(te)} features={len(feats)}")

    # Reproducibility: log the exact data the run trained on.
    # `data_snapshot` = BQ snapshot table id passed by the KFP ingest step
    # `data_sha256`   = hash of the labeled Parquet read from GCS
    import hashlib, fsspec
    data_snapshot = a.data_snapshot
    with fsspec.open(a.features_uri, "rb") as f:
        data_sha256 = hashlib.sha256(f.read()).hexdigest()

    with mlflow.start_run() as run:
        mlflow.log_params({
            **vars(a),
            "n_features":    len(feats),
            "horizon_days":  3,
            "data_snapshot": data_snapshot,
            "data_sha256":   data_sha256,
        })

        triple = QuantileTriple.train(tr, tr["y_logret"], va, va["y_logret"], feats)

        for name, d in (("train", tr), ("val", va), ("test", te)):
            band = triple.predict_band(d)
            m = band_metrics(band)
            mlflow.log_metrics({f"{name}_{k}": v for k, v in m.items()})
            print(f"{name:5s}  MAE={m['mae_logret']:.4f}  naive={m['naive_zero_mae_logret']:.4f}  "
                  f"cov80={m['band80_cov']:.3f}  dir={m['dir_acc']:.3f}  px_MAE={m['price_mae_pct']:.2f}%")

        with tempfile.TemporaryDirectory() as td:
            triple.save(td)
            upload_dir(td, a.models_bucket, f"btc/{run.info.run_id}")
            mlflow.log_artifacts(td, artifact_path="model")

        mlflow.set_tag("model_uri", f"gs://{a.models_bucket}/btc/{run.info.run_id}")
        print(f"run_id={run.info.run_id}")


if __name__ == "__main__":
    main()
