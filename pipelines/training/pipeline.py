"""KFP pipeline: ingest → features → labels → train → holdout_eval → register.

Workload Identity wiring: KFP launches every workflow pod with the
`kubeflow/pipeline-runner` KSA. That KSA is annotated
(`iam.gke.io/gcp-service-account=training-job-sa@<project>.iam.gserviceaccount.com`)
by platform/helm/kfp-workload-identity, so all pods impersonate the GCP SA
automatically — no per-task code needed here.
"""
import os
from typing import NamedTuple
from kfp import dsl, compiler

REGISTRY = os.environ.get("REGISTRY", "REGION-docker.pkg.dev/PROJECT/btc-mlops")


@dsl.component(base_image=f"{REGISTRY}/ingest:latest")
def ingest_op(project: str, dataset: str, table: str, csv_path: str,
              symbol: str = "BTCUSDT", interval: str = "1d",
              snapshot_dataset: str = "btc_snapshots",
              snapshot_expiration_days: int = 365) -> str:
    """Run the ingest CLI with --snapshot; return the printed SNAPSHOT_ID line so
       downstream steps can read the immutable snapshot instead of the live table.
       csv_path points at a CSV baked into the ingest image (see ingest.Dockerfile)."""
    import subprocess
    args = ["python", "-m", "src.ingest.dataset",
            "--csv-path", csv_path,
            "--project", project, "--dataset", dataset, "--table", table,
            "--symbol", symbol, "--interval", interval,
            "--snapshot", "--snapshot-dataset", snapshot_dataset,
            "--snapshot-expiration-days", str(snapshot_expiration_days)]
    out = subprocess.check_output(args, text=True)
    print(out)
    last = [ln for ln in out.strip().splitlines() if ln.startswith("SNAPSHOT_ID=")]
    if not last:
        raise RuntimeError("ingest did not emit SNAPSHOT_ID=... on stdout")
    return last[-1].split("=", 1)[1]


@dsl.container_component
def features_op(project: str, source_table: str, out_bucket: str,
                symbol: str = "BTCUSDT", interval: str = "1d"):
    return dsl.ContainerSpec(
        image=f"{REGISTRY}/features:latest",
        command=["python","-m","src.features.build"],
        args=["--project", project, "--source-table", source_table,
              "--symbol", symbol, "--interval", interval,
              "--out-bucket", out_bucket],
    )


@dsl.container_component
def labels_op(in_path: str, out_path: str, horizon: int = 3):
    return dsl.ContainerSpec(
        image=f"{REGISTRY}/features:latest",
        command=["python","-m","src.features.labels"],
        args=["--in-path", in_path, "--out-path", out_path,
              "--task", "regression", "--horizon", str(horizon)],
    )


@dsl.container_component
def train_op(features_uri: str, models_bucket: str, mlflow_uri: str,
             data_snapshot: str,
             train_end: str = "2024-12-31", val_end: str = "2025-09-30"):
    return dsl.ContainerSpec(
        image=f"{REGISTRY}/train:latest",
        command=["python","-m","src.train.train"],
        args=["--features-uri", features_uri,
              "--models-bucket", models_bucket,
              "--mlflow-uri", mlflow_uri,
              "--data-snapshot", data_snapshot,
              "--train-end", train_end, "--val-end", val_end],
    )


@dsl.component(base_image=f"{REGISTRY}/eval:latest")
def holdout_op(mlflow_uri: str, holdout_bq: str, models_bucket: str
               ) -> NamedTuple("HoldoutOut",
                               [("run_id", str),
                                ("band80_cov", float),
                                ("edge_vs_naive", float)]):
    """Score the 2026 holdout once. Each field of the NamedTuple becomes its own
       KFP output channel so the promotion gate can read them individually."""
    import subprocess, json
    out = subprocess.check_output([
        "python", "-m", "src.eval.holdout",
        "--mlflow-uri", mlflow_uri,
        "--holdout-bq", holdout_bq,
        "--models-bucket", models_bucket,
    ]).decode()
    d = json.loads(out.strip().splitlines()[-1])
    return (d["run_id"], float(d["band80_cov"]), float(d["edge_vs_naive"]))


@dsl.component(base_image=f"{REGISTRY}/eval:latest")
def register_op(run_id: str, mlflow_uri: str):
    import mlflow
    from mlflow.tracking import MlflowClient
    mlflow.set_tracking_uri(mlflow_uri)
    cli = MlflowClient()
    # Register each quantile sub-model as one logical artifact:
    name = "btc-quantile"
    try:
        cli.create_registered_model(name)
    except Exception:
        pass
    art_uri = f"runs:/{run_id}/model"
    mv = cli.create_model_version(name=name, source=art_uri, run_id=run_id)
    cli.transition_model_version_stage(
        name=name, version=mv.version, stage="Production",
        archive_existing_versions=True,
    )
    print(f"promoted {name} v{mv.version} to Production")


@dsl.pipeline(name="btc-quantile-train")
def btc_pipeline(
    project: str,
    bq_dataset: str,
    bq_table: str,
    features_bucket: str,
    models_bucket: str,
    csv_path:         str = "/app/data/BTCUSDT_1d_merged.csv",
    holdout_csv_path: str = "/app/data/BTCUSDT_1d_2026_holdout.csv",
    holdout_table:    str = "dataset_unified_2026_holdout",
    symbol: str = "BTCUSDT",
    interval: str = "1d",
    horizon: int = 3,
    mlflow_uri: str = "http://mlflow.mlflow.svc.cluster.local:5000",
    train_end: str = "2024-12-31",
    val_end:   str = "2025-09-30",
    cov80_min: float = 0.75,
    cov80_max: float = 0.85,
    edge_min:  float = 0.0,
):
    ing = ingest_op(project=project, dataset=bq_dataset, table=bq_table,
                    csv_path=csv_path, symbol=symbol, interval=interval)
    # ing.output is the fully-qualified snapshot id
    # "PROJECT.btc_snapshots.dataset_unified__<ts>". Pass it straight through —
    # features_op accepts a single --source-table arg so we never need to
    # Python-slice the channel at compile time.

    # Holdout: same CSV → BQ → snapshot path so the 2026 evaluation is also pinned.
    ing_ho = ingest_op(project=project, dataset=bq_dataset, table=holdout_table,
                       csv_path=holdout_csv_path, symbol=symbol, interval=interval)

    fe  = features_op(project=project,
                      source_table=ing.output,
                      symbol=symbol, interval=interval,
                      out_bucket=features_bucket).after(ing)
    lab = labels_op(
        in_path=f"gs://{features_bucket}/btc/{interval}/features.parquet",
        out_path=f"gs://{features_bucket}/btc/{interval}/labelled.parquet",
        horizon=horizon,
    ).after(fe)
    tr = train_op(
        features_uri=f"gs://{features_bucket}/btc/{interval}/labelled.parquet",
        models_bucket=models_bucket,
        mlflow_uri=mlflow_uri,
        # Snapshot id lands in MLflow as `data_snapshot` for run traceability.
        data_snapshot=ing.output,
        train_end=train_end, val_end=val_end,
    ).after(lab)
    # XGBoost runs CPU-only; route to the cpu-burst pool, not the GPU pool.
    tr.set_cpu_limit("4").set_memory_limit("8Gi")
    # ing_ho.output is the holdout snapshot id (fully-qualified BQ ref).
    ho = holdout_op(mlflow_uri=mlflow_uri, holdout_bq=ing_ho.output,
                    models_bucket=models_bucket).after(tr, ing_ho)

    # Promotion gate: holdout band is calibrated AND model beats random walk.
    # KFP 2.x dsl.If takes a single condition — nest for logical AND.
    with dsl.If(ho.outputs["band80_cov"] >= cov80_min):
        with dsl.If(ho.outputs["band80_cov"] <= cov80_max):
            with dsl.If(ho.outputs["edge_vs_naive"] >= edge_min):
                register_op(run_id=ho.outputs["run_id"], mlflow_uri=mlflow_uri).after(ho)


if __name__ == "__main__":
    import os
    os.makedirs("build", exist_ok=True)
    compiler.Compiler().compile(btc_pipeline, "build/btc-quantile-train.yaml")
