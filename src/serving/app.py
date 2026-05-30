"""FastAPI serving app: GET /predict returns a P10/P50/P90 forecast band.

Endpoints:
  GET /health   liveness + readiness + model version
  GET /predict  pulls latest features from BQ, runs Production model, returns band
  GET /metrics  Prometheus exposition

Background thread polls MLflow registry every MODEL_REFRESH_SECONDS and
atomically swaps the model when a newer Production version is found.
"""
from __future__ import annotations
import os
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

from src.serving.predict import Predictor

log = logging.getLogger("btc-serving")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Read env lazily inside lifespan() — KeyError at import-time breaks pytest
# and `python -c "import app"` smoke checks.
def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"required env {name} is empty")
    return v

REFRESH_SECONDS: int = 600  # overwritten in lifespan() from MODEL_REFRESH_SECONDS

# Module-level singletons. predictor is None until /lifespan startup completes.
predictor:  Predictor | None = None
_stop_event: threading.Event = threading.Event()

REQ_TOTAL  = Counter(  "btc_serving_requests_total",   "Predict requests", ["endpoint", "status"])
PRED_LAT   = Histogram("btc_serving_predict_seconds",  "Predict latency (s)")
MODEL_VER  = Gauge(    "btc_serving_model_version_info", "1 with model version label", ["version"])


def _refresh_loop():
    while not _stop_event.wait(REFRESH_SECONDS):
        try:
            if predictor.refresh():
                log.info(f"model refreshed → {MODEL_NAME_TAG()}")
                # Replace the version-labelled gauge: clear then set.
                MODEL_VER.clear()
                MODEL_VER.labels(version=predictor.model_version or "unknown").set(1)
        except Exception as e:
            log.warning(f"background refresh failed: {e}")


def MODEL_NAME_TAG() -> str:
    return f"btc-quantile/{predictor.model_version}" if predictor else "uninit"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global predictor, REFRESH_SECONDS
    gcp_project   = _require_env("GCP_PROJECT")
    models_bucket = _require_env("MODELS_BUCKET")
    mlflow_uri    = os.environ.get("MLFLOW_URI", "http://mlflow.mlflow.svc.cluster.local:5000")
    REFRESH_SECONDS = int(os.environ.get("MODEL_REFRESH_SECONDS", "600"))
    predictor = Predictor(project=gcp_project, mlflow_uri=mlflow_uri, models_bucket=models_bucket)
    MODEL_VER.labels(version=predictor.model_version or "unknown").set(1)
    log.info(f"startup complete: model={MODEL_NAME_TAG()}  refresh_every={REFRESH_SECONDS}s")
    t = threading.Thread(target=_refresh_loop, daemon=True, name="model-refresh")
    t.start()
    yield
    _stop_event.set()
    log.info("shutdown")


app = FastAPI(title="btc-serving", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    ok = predictor is not None and predictor.ready
    return {
        "ok":             ok,
        "model_version":  predictor.model_version if predictor else None,
        "model_run_id":   predictor.model_run_id  if predictor else None,
    }


@app.get("/predict")
def predict(
    horizon: int = Query(default=3, ge=1, le=14, description="forecast horizon in days"),
):
    if predictor is None or not predictor.ready:
        REQ_TOTAL.labels("predict", "not_ready").inc()
        raise HTTPException(503, "predictor not ready")
    with PRED_LAT.time():
        try:
            out = predictor.predict(horizon_days=horizon)
            REQ_TOTAL.labels("predict", "ok").inc()
            return out
        except Exception as e:
            REQ_TOTAL.labels("predict", "err").inc()
            log.exception("predict failed")
            raise HTTPException(500, str(e))


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)
