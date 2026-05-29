"""Submit the compiled KFP pipeline to a port-forwarded ml-pipeline-ui.

Invoked by `make kfp-submit`. Reads PROJECT / MLFLOW_URI / PIPELINE_YML /
KFP_HOST / EXPERIMENT / CSV_TRAIN / CSV_HOLDOUT / HOLDOUT_TABLE / SYMBOL /
INTERVAL / HORIZON from env, falling back to sensible defaults.
"""
from __future__ import annotations
import os
import sys

import kfp


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: env {name} is empty — pass it via the Makefile")
    return v


def main() -> None:
    project        = _require("PROJECT")
    pipeline_yml   = os.environ.get("PIPELINE_YML", "build/btc-quantile-train.yaml")
    kfp_host       = os.environ.get("KFP_HOST", "http://localhost:8080")
    experiment     = os.environ.get("EXPERIMENT", "btc-quantile")
    mlflow_uri     = os.environ.get("MLFLOW_URI", "http://mlflow.mlflow.svc.cluster.local:5000")
    csv_train      = os.environ.get("CSV_TRAIN",      "/app/data/BTCUSDT_1d_merged.csv")
    csv_holdout    = os.environ.get("CSV_HOLDOUT",    "/app/data/BTCUSDT_1d_2026_holdout.csv")
    holdout_table  = os.environ.get("HOLDOUT_TABLE",  "dataset_unified_2026_holdout")
    symbol         = os.environ.get("SYMBOL",   "BTCUSDT")
    interval       = os.environ.get("INTERVAL", "1d")
    horizon        = int(os.environ.get("HORIZON", "3"))

    cli = kfp.Client(host=kfp_host)
    run = cli.create_run_from_pipeline_package(
        pipeline_file=pipeline_yml,
        arguments={
            "project":          project,
            "bq_dataset":       "btc_raw",
            "bq_table":         "dataset_unified",
            "features_bucket":  f"{project}-data-features",
            "models_bucket":    f"{project}-models",
            "csv_path":         csv_train,
            "holdout_csv_path": csv_holdout,
            "holdout_table":    holdout_table,
            "symbol":           symbol,
            "interval":         interval,
            "horizon":          horizon,
            "mlflow_uri":       mlflow_uri,
        },
        experiment_name=experiment,
    )
    print(f"submitted: run_id={run.run_id}")
    print(f"open: {kfp_host}/#/runs/details/{run.run_id}")


if __name__ == "__main__":
    main()
