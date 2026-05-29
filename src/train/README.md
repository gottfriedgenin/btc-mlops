# `src/train/` — Quantile XGBoost trainer

## Purpose

Train three XGBoost regressors — one each for the **P10 / P50 / P90** quantile
of the H-day forward log return — bundle them as a single artifact, log to
MLflow, push to GCS.

Why a triple, not a single model: a point estimate of "where will price be in
3 days" is misleading. A **band** (10th to 90th percentile) tells you both
direction and uncertainty. The width of the band collapses in calm regimes
and widens in volatile ones — useful both for trading and for monitoring.

## Files

| File | Role |
|---|---|
| `model.py` | `QuantileTriple` dataclass: shared hyperparameters, fit / predict / save / load helpers. |
| `train.py` | CLI that loads labelled Parquet → trains the triple → logs to MLflow → uploads to GCS. |
| `cv.py` | Walk-forward CV for an honest in-sample evaluation. Not used in the production pipeline; for ablations / sanity. |
| `__init__.py` | Empty. |

---

## `model.py`

### `HYPERPARAMS` (shared across all three quantiles)

| Hyperparameter | Value | Why |
|---|---|---|
| `objective` | `reg:quantileerror` | XGBoost's native quantile (pinball) loss. |
| `max_depth` | 5 | Shallow enough to avoid overfitting ~2000 rows. |
| `n_estimators` | 800 | Upper bound — actual count chosen by `early_stopping_rounds`. |
| `learning_rate` | 0.04 | Slow + many trees beats fast + few trees on small frames. |
| `subsample` | 0.85 | Row sampling regularizer. |
| `colsample_bytree` | 0.85 | Feature sampling regularizer. |
| `tree_method` | `"hist"` | Histogram split — fast and CPU-friendly. |
| `early_stopping_rounds` | 50 | Stops when val MAE hasn't improved in 50 rounds. |
| `verbosity` | 0 | Silent. |

Only `quantile_alpha` (∈ {0.10, 0.50, 0.90}) differs between the three.

### `class QuantileTriple`

`@dataclass` wrapping three `XGBRegressor`s plus the feature list.

| Method | Inputs | Output | Notes |
|---|---|---|---|
| `train(Xtr, ytr, Xva, yva, features)` (classmethod) | train/val features + label series + feature name list | `QuantileTriple` | Fits all three regressors with the shared hyperparameters and per-quantile `alpha`. Each uses `Xva/yva` as early-stopping set. |
| `predict_band(d)` | DataFrame with `timestamp`, `close`, `y_logret` (optional) + feature columns | DataFrame with `ts`, `now`, `y_logret`, `p10`, `p50`, `p90`, `p10_price`, `p50_price`, `p90_price`, `actual_price` | Reconstructs price as `now · exp(p)`. `actual_price` filled if `y_logret` exists. |
| `save(out_dir)` | local directory | files: `q10.json`, `q50.json`, `q90.json`, `meta.json` | XGBoost native JSON + a `meta.json` listing feature names. |
| `load(in_dir)` (classmethod) | local directory | `QuantileTriple` | Inverse of `save`. |

### Why the triple is one artifact

Three separate models would mean three separate MLflow runs and three
versions to keep in lockstep at serving time. Bundling them keeps the
versioning atomic — promoting "the model" promotes all three.

---

## `train.py`

CLI that does:

1. Read `labelled.parquet` from GCS (`--features-uri`).
2. Run `split()` (`--train-end`, `--val-end`).
3. Call `QuantileTriple.train(...)`.
4. Compute band metrics on train / val / test via `src.eval.metrics.band_metrics`.
5. `mlflow.log_params(HYPERPARAMS)` + `mlflow.log_metric(...)` per split.
6. Log `data_snapshot` (env `DATA_SNAPSHOT_ID`) + `data_sha256` so the run is
   traceable back to its exact ingest snapshot.
7. `QuantileTriple.save(local_dir)` → `upload_dir` to GCS.
8. `mlflow.log_artifact(local_dir)`.

### CLI

```bash
poetry run python -m src.train.train \
  --features-uri  gs://PROJECT-data-features/btc/1d/labelled.parquet \
  --models-bucket PROJECT-models \
  --mlflow-uri    http://mlflow.mlflow.svc.cluster.local:5000 \
  --experiment    btc-quantile \
  --train-end     2024-12-31 \
  --val-end       2025-09-30
```

| Arg | Default | |
|---|---|---|
| `--features-uri` | (required) | GCS path to `labelled.parquet`. |
| `--models-bucket` | (required) | GCS bucket for saved artifacts. Created by Terraform. |
| `--mlflow-uri` | (required) | MLflow tracking URL (in-cluster service URL). |
| `--experiment` | `btc-quantile` | MLflow experiment name. Drift retraining uses the same name. |
| `--train-end` | `2024-12-31` | Inclusive train cutoff. |
| `--val-end` | `2025-09-30` | Inclusive val cutoff. Test = everything after. |

### MLflow log shape

| Group | Keys |
|---|---|
| Params | `HYPERPARAMS` (10 keys) + `train_end`, `val_end`, `horizon`, `data_snapshot`, `data_sha256` |
| Metrics per split (`train_*`, `val_*`, `test_*`) | `mae_logret`, `naive_zero_mae_logret`, `rmse_logret`, `band80_cov`, `dir_acc`, `price_mae_pct`, `edge_vs_naive` |
| Artifacts | `model/` (three JSONs + meta), GCS object copy at `gs://PROJECT-models/btc-quantile/<run_id>/` |

---

## `cv.py`

Walk-forward cross-validation. **Not** part of the production pipeline — used
in ablations and for sanity-checking that VAL/TEST metrics aren't a lucky
single split.

```python
def walk_forward_eval(df, n_folds=6, fold_days=60) → list[dict]
```

For each fold the train end advances by `fold_days`, the next `fold_days`
become val. Returns one `band_metrics(...)` dict per fold. Plot or aggregate
in a notebook.

---

## How `train/` fits the bigger picture

```
   labelled.parquet ─▶ train.py ─▶ MLflow run (params + metrics + artifact)
                          │             │
                          │             └─▶ promotion gate (src/eval/holdout.py)
                          ▼
                   gs://PROJECT-models/btc-quantile/<run_id>/
                          │
                          └─▶ Phase 6 serving loads QuantileTriple from this dir
```

## Operational notes

- **No GPU.** `tree_method="hist"` on ~2000 rows × 60 features trains in
  seconds. KFP targets the `cpu-burst-pool` node pool.
- **Cost.** A single training run is a few cents (CPU minutes on a Spot VM
  + ~1 MB of GCS).
- **Determinism.** XGBoost is deterministic given the same data + hyperparams +
  thread count. Trainer pins `n_jobs` implicitly via container CPU limits.
