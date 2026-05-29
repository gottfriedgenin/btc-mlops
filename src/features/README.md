# `src/features/` — Feature engineering, labels, split

## Purpose

Turn the 58-column unified BQ row into a clean numerical frame the trainer can
consume, attach a forward-looking label, and split it by date.

Mirrors `notebooks/00_setup.ipynb` + `notebooks/03_baseline_vs_transformer.ipynb`
so notebook results reproduce exactly in the KFP pipeline.

## Files

| File | Role |
|---|---|
| `build.py` | Drop dead columns + warmup, add derived features, write Parquet. |
| `labels.py` | Attach `y_logret` + `y_price` (regression) or `y` (classification). |
| `split.py` | Strict time-based train / val / test split (no shuffling). |
| `__init__.py` | Empty. |

---

## `build.py`

Reads the BQ snapshot (set by `--dataset` / `--table`), drops noise, adds
derived columns, drops NaN rows, writes Parquet to GCS.

### Function map

| Function | Purpose |
|---|---|
| `drop_dead_cols(df, drop_age=True, drop_etf=True)` | Strips noise columns (see tables below). |
| `drop_indicator_warmup(df, warmup_days=365)` | Cuts the first year — slow indicators (Ichimoku senkou_b ~52 w, EMA200) need ~1 y of history before they're meaningful. |
| `add_derived_features(df)` | Builds all returns / vol / regime / trend / calendar features. |
| `feature_columns(df, exclude=None)` | Returns the numeric column names usable as model inputs (drops keys + label columns). |
| `build(df, warmup_days=365)` | `drop_dead_cols → drop_indicator_warmup → add_derived_features → dropna`. |
| `main()` | CLI: read BQ → `build()` → upload Parquet. |

### Dead columns

| Group | Columns | Why dropped |
|---|---|---|
| `AGE_COLS` (10) | `hash_rate_age_hours`, `difficulty_age_hours`, `block_time_age_hours`, `miner_reserves_age_hours`, `miner_outflows_age_hours`, `mpi_age_hours`, `exchange_inflow_age_hours`, `exchange_outflow_age_hours`, `exchange_reserves_age_hours`, `etf_flow_total_age_hours` | All constant 0 across the backfill — no signal. |
| `ETF_COLS` (1) | `etf_flow_total` | Spot ETFs launched Jan 2024; mostly empty pre-2024. Drop until the series is long enough to learn from. |

### Derived features

`add_derived_features` adds the following on top of the 58-col raw row:

| Group | Columns | Formula |
|---|---|---|
| Log returns | `logret_1`, `logret_3`, `logret_5`, `logret_10`, `logret_20` | `ln(close_t / close_{t-k})` for k ∈ {1,3,5,10,20} |
| Realized vol | `vol_5`, `vol_20`, `vol_60` | rolling stdev of `logret_1` over 5 / 20 / 60 days |
| Regime | `atr_pct`, `bb_width`, `kelt_width` | `atr/close`, `(bb_upper - bb_lower)/bb_middle`, `(kelt_upper - kelt_lower)/kelt_middle` |
| Trend | `px_vs_sma`, `sma_vs_ema` | `close/sma - 1`, `sma/ema - 1` |
| Volume | `vol_z_20` | z-score of `volume` over rolling 20 d |
| Cyclical calendar | `dow_sin`, `dow_cos`, `month_sin`, `month_cos` | `sin/cos(2π · dayofweek/7)`, `sin/cos(2π · (month-1)/12)` |

Why log returns, not raw returns: addition-closed (k-day return is the sum of
the daily returns), symmetric around zero, more Gaussian — XGBoost still
splits on them effectively and the quantile loss is well-behaved.

### CLI

```bash
poetry run python -m src.features.build \
  --project PROJECT --dataset btc_snapshots \
  --table dataset_unified__20260529_103401 \
  --symbol BTCUSDT --interval 1d \
  --out-bucket PROJECT-data-features
```

Output: `gs://PROJECT-data-features/btc/1d/features.parquet`.

---

## `labels.py`

Two label modes. **Regression is primary.**

| Function | Adds | Drops | When to use |
|---|---|---|---|
| `label_regression(df, horizon=3)` | `y_logret` = forward H-day log return; `y_price` = close at +H | last `horizon` rows | Default. Quantile XGBoost regresses P10/P50/P90 of `y_logret`. |
| `label_classification(df, horizon=1, eps=0.005)` | `y ∈ {-1, 0, 1}` (down / flat / up with ±0.5 % dead-band) | last `horizon` rows | Optional ablation only — current task is regression. |

The horizon trims the tail because there's no future to look at yet.

### Math

For each row `t`:

```
y_logret[t]  = ln( close[t + H] / close[t] )
y_price[t]   = close[t + H]
```

### CLI

```bash
poetry run python -m src.features.labels \
  --in-path  gs://PROJECT-data-features/btc/1d/features.parquet \
  --out-path gs://PROJECT-data-features/btc/1d/labelled.parquet \
  --task regression --horizon 3
```

| Arg | Default | |
|---|---|---|
| `--task` | `regression` | `regression` or `classification` |
| `--horizon` | `3` | days to look ahead (H) |
| `--eps` | `0.005` | classification dead-band only |

---

## `split.py`

Strict time-based split. Never shuffles — that would leak the future.

```python
def split(df, train_end="2024-12-31", val_end="2025-09-30")
    → (train, val, test)
```

| Slice | Date range | Use |
|---|---|---|
| `train` | `… ≤ 2024-12-31` | Fit the model. |
| `val`   | `2025-01-01 … ≤ 2025-09-30` | Early stopping, hyperparameter pick. |
| `test`  | `2025-10-01 … (end of merged CSV)` | Honest in-sample-period test set. Reported in MLflow, **never** used to pick hyperparameters. |
| 2026 holdout | `2026-01-01 …` | Lives in a **separate file/snapshot** (`BTCUSDT_1d_2026_holdout.csv` → `dataset_unified_2026_holdout` → its own snapshot). Scored exactly once per run by `src/eval/holdout.py`. Never train, tune, or peek. |

Why three plus a holdout: hyperparameters touch val, so val is "spent" the
moment you tune. Test confirms generalization within the training-data
window. The 2026 holdout is the only data the model has never seen at any
stage and is the real promotion signal.

---

## How `features/` fits the bigger picture

```
   snapshot id ─▶ build.py ─▶ features.parquet ─▶ labels.py ─▶ labelled.parquet ─▶ split.py ─▶ train.py
```

Inside `train.py` the split is called as:

```python
from src.features.split import split
train, val, test = split(df, train_end=a.train_end, val_end=a.val_end)
```

## Tests

`tests/test_features.py` covers:

- `test_no_nan_after_build` — `build()` returns a NaN-free frame
- `test_regression_label_drops_last_horizon` — label trims correctly
- `test_classification_label_optional` — classification flow still works
- `test_feature_columns_excludes_labels` — `feature_columns()` doesn't leak `y*`
- `test_deterministic` — `build()` is idempotent

Run: `poetry run pytest tests/ -q`.
