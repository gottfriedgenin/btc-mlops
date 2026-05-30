"""Predictor: load Production model from MLflow registry, pull latest features from BQ, return P10/P50/P90 band.

Thread-safe atomic model swap so a background refresh can replace the model
mid-flight without breaking in-flight `/predict` calls.
"""
from __future__ import annotations
import os
import tempfile
import threading
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import mlflow
from mlflow.tracking import MlflowClient
from google.cloud import bigquery, storage

from src.features.build import build
from src.train.model import QuantileTriple

log = logging.getLogger(__name__)

MODEL_NAME       = "btc-quantile"
SYMBOL           = "BTCUSDT"
INTERVAL         = "1d"
HORIZON_DAYS     = 3
# 400 days gives the 60-day rolling features ~340 days of usable history after
# warmup; plenty even after the dropna() inside build().
LOOKBACK_DAYS    = 400


class Predictor:
    def __init__(self, project: str, mlflow_uri: str, models_bucket: str,
                 source_table: str = "btc_raw.dataset_unified"):
        self.project       = project
        self.mlflow_uri    = mlflow_uri
        self.models_bucket = models_bucket
        self.source_table  = source_table
        mlflow.set_tracking_uri(mlflow_uri)
        self._lock    = threading.Lock()
        self._triple: QuantileTriple | None = None
        self._version: str | None = None
        self._run_id:  str | None = None
        self.refresh()  # block startup until a model is loaded

    # ------------------------------------------------------------------ public
    @property
    def model_version(self) -> str | None:
        return self._version

    @property
    def model_run_id(self) -> str | None:
        return self._run_id

    @property
    def ready(self) -> bool:
        return self._triple is not None

    def refresh(self) -> bool:
        """Pull latest Production model from MLflow registry. No-op if version unchanged.

        Returns True if a new model was loaded.
        """
        cli = MlflowClient()
        versions = cli.get_latest_versions(MODEL_NAME, stages=["Production"])
        if not versions:
            raise RuntimeError(f"no Production version of {MODEL_NAME} in MLflow")
        v = versions[0]
        if self._version == v.version:
            return False

        run_id = v.run_id
        log.info(f"loading {MODEL_NAME} v{v.version} (run={run_id}) from gs://{self.models_bucket}/btc/{run_id}/")
        bkt = storage.Client(project=self.project).bucket(self.models_bucket)
        with tempfile.TemporaryDirectory() as td:
            for blob in bkt.list_blobs(prefix=f"btc/{run_id}"):
                local = f"{td}/{blob.name.rsplit('/', 1)[-1]}"
                blob.download_to_filename(local)
            triple = QuantileTriple.load(td)

        with self._lock:
            self._triple, self._version, self._run_id = triple, v.version, run_id
        return True

    def predict(self, horizon_days: int = HORIZON_DAYS,
                lookback_days: int = LOOKBACK_DAYS) -> dict:
        bq = bigquery.Client(project=self.project)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
        sql = f"""
            SELECT *
            FROM `{self.project}.{self.source_table}`
            WHERE `symbol` = @symbol AND `interval` = @itv
              AND `timestamp` >= TIMESTAMP("{cutoff}")
            ORDER BY `timestamp`
        """
        df = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("symbol", "STRING", SYMBOL),
            bigquery.ScalarQueryParameter("itv",    "STRING", INTERVAL),
        ])).to_dataframe()
        if df.empty:
            raise RuntimeError(f"source table {self.source_table} has no rows for {SYMBOL}/{INTERVAL} since {cutoff}")
        feats = build(df, warmup_days=0)
        if feats.empty:
            raise RuntimeError("features empty after build() — check source data")
        last = feats.iloc[[-1]].copy()

        with self._lock:
            triple   = self._triple
            version  = self._version
            run_id   = self._run_id

        # Reindex live features to the model's training schema. Columns that
        # ended up 100%-NaN in the lookback window (and were auto-dropped by
        # build()) get re-added as NaN — XGBoost handles missing values natively.
        missing = [c for c in triple.features if c not in last.columns]
        if missing:
            log.warning(f"live features missing {len(missing)} cols vs model schema: {missing}")
            for c in missing:
                last[c] = np.nan

        band = triple.predict_band(last)

        last_ts    = last["timestamp"].iloc[0]
        last_close = float(last["close"].iloc[0])
        p10_lr = float(band["p10"].iloc[0])
        p50_lr = float(band["p50"].iloc[0])
        p90_lr = float(band["p90"].iloc[0])
        return {
            "horizon_days":  horizon_days,
            "as_of":         last_ts.date().isoformat(),
            "predict_for":   (last_ts + pd.Timedelta(days=horizon_days)).date().isoformat(),
            "close":         last_close,
            "p10_price":     last_close * float(np.exp(p10_lr)),
            "p50_price":     last_close * float(np.exp(p50_lr)),
            "p90_price":     last_close * float(np.exp(p90_lr)),
            "p10_logret":    p10_lr,
            "p50_logret":    p50_lr,
            "p90_logret":    p90_lr,
            "model_version": f"{MODEL_NAME}/{version}",
            "model_run_id":  run_id,
            "data_snapshot": "live",
        }
