# `src/eval/` — Metrics + holdout scorer

## Purpose

1. Define the **band metrics** that summarize how well the quantile triple did
   on any slice of data (train / val / test / holdout / drift window).
2. Run the **2026 holdout** exactly once per training run and emit the values
   the promotion gate checks (`band80_cov`, `edge_vs_naive`).

## Files

| File | Role |
|---|---|
| `metrics.py` | `band_metrics(band)` — turns a band DataFrame into a dict of scalar metrics. |
| `holdout.py` | CLI that loads the latest model from MLflow, scores the 2026 holdout BQ snapshot, logs `holdout_*` metrics, and returns the gate values. |
| `__init__.py` | Empty. |

---

## `metrics.py`

`band_metrics(band)` consumes the output of
`QuantileTriple.predict_band(...)` (a DataFrame with `y_logret`, `p10`, `p50`,
`p90`, `now`, `actual_price`, `p50_price`) and returns:

| Metric | Formula | What it tells you |
|---|---|---|
| `mae_logret` | `mean(|y_logret − p50|)` | Median accuracy in log-return space. |
| `naive_zero_mae_logret` | `mean(|y_logret − 0|)` | The "predict no change" baseline. |
| `edge_vs_naive` | `naive_zero_mae_logret − mae_logret` | **> 0 means the model actually adds signal.** Negative = a random-walk baseline beats you. Primary promotion signal. |
| `rmse_logret` | `√mean((y_logret − p50)²)` | RMSE in log-return space (penalizes large misses). |
| `band80_cov` | `mean((p10 ≤ y_logret ≤ p90))` | **Calibration.** A well-calibrated 80 % band covers ~80 % of actuals. Below 75 % → too narrow. Above 85 % → too wide. |
| `dir_acc` | `mean(sign(p50) == sign(y_logret))` | Direction-only accuracy. Useful as a sanity check; not a gate. |
| `price_mae_pct` | `mean(|actual_price − p50_price| / now) · 100` | Median absolute price error, in percent. Human-readable. |

### Why these and not Sharpe / PnL

Sharpe needs a position-sizing rule. The model only outputs a forecast band;
turning that into PnL is a separate decision (Kelly fraction, leverage cap,
slippage model). Keep the model metrics decoupled from any specific trading
policy.

### How to interpret an MLflow run quickly

```
val_band80_cov   = 0.81   ← in [0.75, 0.85] ✓
val_edge_vs_naive= 0.0011 ← positive ✓ (model beats naive zero by 0.11 pp)
val_dir_acc      = 0.56   ← > 0.5 ✓
```

If `edge_vs_naive` is < 0, the model is **worse than predicting zero** and
should not be promoted regardless of how nice the bands look.

---

## `holdout.py`

The "score it once, never again" step that drives the promotion gate.

### What it does

1. Fetch the run id of the latest training run (`--experiment`).
2. Download the saved `QuantileTriple` from `gs://PROJECT-models/.../<run_id>/`.
3. Read the 2026 holdout snapshot from BQ (`--holdout-bq`).
4. Run `build()` + `label_regression(horizon=3)` on the holdout frame —
   **exact same code** the trainer used.
5. Call `predict_band()` + `band_metrics()`.
6. `mlflow.log_metric("holdout_*", ...)` on the same run id.
7. Print `band80_cov` + `edge_vs_naive` + `run_id` as JSON on the last line of
   stdout (parsed by `holdout_op` in the KFP pipeline).

### CLI

```bash
poetry run python -m src.eval.holdout \
  --mlflow-uri    http://mlflow.mlflow.svc.cluster.local:5000 \
  --experiment    btc-quantile \
  --holdout-bq    PROJECT.btc_snapshots.dataset_unified_2026_holdout__20260529_103401 \
  --models-bucket PROJECT-models \
  --horizon       3
```

| Arg | Default | |
|---|---|---|
| `--mlflow-uri` | (required) | Same tracking URI used by the trainer. |
| `--experiment` | `btc-quantile` | Resolves to the latest run. |
| `--holdout-bq` | (required) | Fully-qualified BQ ref of the holdout snapshot. Provided by the second `ingest_op` in the KFP DAG. |
| `--models-bucket` | (required) | Where to fetch the model artifacts from. |
| `--horizon` | 3 | Must match the value the trainer used. |

### Stdout contract

Last line is JSON, parsed by `holdout_op` in
[`pipelines/training/pipeline.py`](../../pipelines/training/pipeline.py):

```
…
{"run_id": "abc123…", "band80_cov": 0.81, "edge_vs_naive": 0.0011}
```

### Promotion gate

`pipelines/training/pipeline.py` uses a `dsl.If(...)` block that fires
`register_op` only when **all** of:

| Condition | Default | Source |
|---|---|---|
| `band80_cov ≥ cov80_min` | 0.75 | `holdout_op` output |
| `band80_cov ≤ cov80_max` | 0.85 | `holdout_op` output |
| `edge_vs_naive ≥ edge_min` | 0.0 | `holdout_op` output |

Tighten / loosen by passing different defaults to `btc_pipeline(...)`.

If the gate fails, the run still exists in MLflow (so you can inspect it) but
the model **is not** promoted to `Production`. The previous Production model
stays live.

---

## How `eval/` fits the bigger picture

```
                              ┌──────────────────────────┐
   train.py logs metrics ───▶ │ MLflow run (params,      │
                              │ metrics, model artifact) │
                              └────────┬─────────────────┘
                                       │
   holdout snapshot ──────────────────▶┤
                                       ▼
                              ┌──────────────────────────┐
                              │ holdout.py:              │
                              │   download model         │
                              │   build() + label_…(3)   │
                              │   predict_band()         │
                              │   band_metrics()         │
                              │   log holdout_* to run   │
                              └────────┬─────────────────┘
                                       │ JSON {run_id, band80_cov, edge_vs_naive}
                                       ▼
                              ┌──────────────────────────┐
                              │ pipeline.py dsl.If(...)  │
                              │   gate passes? ─▶ register│
                              │   else stop              │
                              └──────────────────────────┘
```

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `band80_cov` < 0.5 or > 0.95 | Quantile models miscalibrated | Train longer or check feature set; don't loosen the gate. |
| `edge_vs_naive` < 0 | Model worse than predicting zero | Iterate features in notebooks before re-running pipeline. |
| `holdout_*` metrics missing | `holdout_op` crashed before `mlflow.log_metric` | Inspect the holdout pod logs; usually the BQ snapshot id is wrong. |
| Promotion gate never fires | `dsl.If` evaluated with stale outputs | Make sure `holdout_op` ran (not skipped) and re-check the run id in MLflow. |
