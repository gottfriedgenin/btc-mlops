# `src/` — Pipeline code

Python code that runs **inside** the KFP pipeline (and is exercised by
`pytest tests/` and notebooks). One responsibility per package:

| Package | Responsibility |
|---|---|
| `src/ingest/`    | Load committed CSV → BigQuery, pin an immutable snapshot. |
| `src/features/`  | Drop dead columns + warmup, add derived features, attach labels, time-split. |
| `src/train/`     | (Coming next — quantile XGBoost trainer + MLflow logging.) |
| `src/eval/`      | (Coming next — holdout scorer + promotion gate.) |

The data version is the **git SHA of `notebooks/data/*.csv`**. No service is
called at run time.

---

## Dataflow

```
                    notebooks/data/BTCUSDT_1d_merged.csv         ←─── git SHA = data version
                    notebooks/data/BTCUSDT_1d_2026_holdout.csv
                                  │
                                  │  (CSV baked into ingest image at /app/data/)
                                  ▼
              ┌──────────────────────────────────────────┐
              │  src/ingest/dataset.py                   │
              │    load_csv() → DataFrame                │
              │    upsert_bq() → btc_raw.dataset_unified │
              │    snapshot_table() → CREATE SNAPSHOT    │
              └────────────────────┬─────────────────────┘
                                   │  SNAPSHOT_ID=PROJECT.btc_snapshots.dataset_unified__<ts>
                                   ▼
              ┌──────────────────────────────────────────┐
              │  src/features/build.py                   │
              │    drop_dead_cols (AGE_COLS, ETF_COLS)   │
              │    drop_indicator_warmup (365 d)         │
              │    add_derived_features (returns/vol/…)  │
              │    → features.parquet                    │
              └────────────────────┬─────────────────────┘
                                   ▼
              ┌──────────────────────────────────────────┐
              │  src/features/labels.py                  │
              │    label_regression  → y_logret, y_price │
              │    label_classification (optional)       │
              │    → labelled.parquet                    │
              └────────────────────┬─────────────────────┘
                                   ▼
              ┌──────────────────────────────────────────┐
              │  src/features/split.py                   │
              │    train ≤ 2024-12-31                    │
              │    val   ≤ 2025-09-30                    │
              │    test  (everything after, ≤2025-12-31) │
              │    2026 holdout = separate file/snapshot │
              └────────────────────┬─────────────────────┘
                                   ▼
                          (src/train/train.py  →  src/eval/holdout.py)
```

---

## `src/ingest/dataset.py`

Reads a committed CSV, upserts it into BigQuery, optionally pins a snapshot.

| Function | Purpose |
|---|---|
| `load_csv(csv_path, symbol, interval)` | Read CSV, coerce `timestamp` to UTC, force int dtypes on event-count columns, backfill `symbol`/`interval` columns if missing. |
| `upsert_bq(df, project, dataset, table)` | `DELETE`-then-`APPEND` over the `(symbol, interval, [min_ts, max_ts])` window. `ALLOW_FIELD_ADDITION` lets new columns auto-extend the schema. |
| `snapshot_table(project, dataset, src_table, snapshot_dataset, …)` | `CREATE SNAPSHOT TABLE … CLONE …`. Metadata-only — no row copy. Returns the fully-qualified snapshot id. |
| `main()` | CLI entrypoint. Prints `SNAPSHOT_ID=…` as the **last** line of stdout so KFP can capture it as a step output. |

**CLI:**
```bash
poetry run python -m src.ingest.dataset \
  --csv-path notebooks/data/BTCUSDT_1d_merged.csv \
  --project PROJECT --dataset btc_raw --table dataset_unified \
  --symbol BTCUSDT --interval 1d \
  --snapshot --snapshot-dataset btc_snapshots --snapshot-expiration-days 365
```

**Why a snapshot.** The live BQ table is overwritten on every ingest. A snapshot
decouples the pipeline run from any concurrent or future ingest — re-running
against the same snapshot id gives bit-identical features.

---

## `src/features/build.py`

Direct port of `notebooks/00_setup.ipynb`. Reads from BQ, drops noise, adds
derived columns, writes Parquet to GCS.

| Function | Drops / adds |
|---|---|
| `drop_dead_cols(df)` | Removes `AGE_COLS` (10 stale `*_age_hours` columns) + `ETF_COLS` (`etf_flow_total`, mostly empty pre-2024). |
| `drop_indicator_warmup(df, 365)` | Cuts the first year — slow indicators (Ichimoku senkou_b, EMA200) need ~1 y warmup. |
| `add_derived_features(df)` | Log returns (k=1,3,5,10,20), realized vol (5/20/60), regime (`atr_pct`, `bb_width`, `kelt_width`), trend (`px_vs_sma`, `sma_vs_ema`), volume z-score, cyclical day-of-week + month. |
| `feature_columns(df, exclude)` | Returns the numeric column names usable as model inputs (drops keys + label columns). |
| `build(df, warmup_days=365)` | Pipeline of the four above + `dropna`. |

**Dropped columns reference:**

| Group | Names | Why |
|---|---|---|
| `AGE_COLS` | `hash_rate_age_hours`, `difficulty_age_hours`, `block_time_age_hours`, `miner_reserves_age_hours`, `miner_outflows_age_hours`, `mpi_age_hours`, `exchange_inflow_age_hours`, `exchange_outflow_age_hours`, `exchange_reserves_age_hours`, `etf_flow_total_age_hours` | Constant 0 across the backfill — no signal. |
| `ETF_COLS` | `etf_flow_total` | Launched Jan 2024 — mostly empty pre-2024. Drop until the series is longer. |

---

## `src/features/labels.py`

Two label modes, regression is primary.

| Function | Adds columns | When to use |
|---|---|---|
| `label_regression(df, horizon=3)` | `y_logret` (forward H-day log return), `y_price` (reconstructed price) | Primary task. Quantile XGBoost predicts a P10 / P50 / P90 band over `y_logret`. |
| `label_classification(df, horizon=1, eps=0.005)` | `y` ∈ {-1, 0, 1} ("down / flat / up" with ±0.5 % deadband) | Optional ablation. |

Both drop the last `horizon` rows (no future to look at).

**CLI:**
```bash
poetry run python -m src.features.labels \
  --in-path  gs://PROJECT-data-features/btc/1d/features.parquet \
  --out-path gs://PROJECT-data-features/btc/1d/labelled.parquet \
  --task regression --horizon 3
```

---

## `src/features/split.py`

Strict time-based split. **Never** shuffles — that would leak the future.

| Slice | Date range | Use |
|---|---|---|
| `train` | `… ≤ 2024-12-31` | Fit the model. |
| `val`   | `2025-01-01 … ≤ 2025-09-30` | Early stopping, hyperparameter pick. |
| `test`  | `2025-10-01 … (end of merged CSV)` | Honest in-sample-period test set. |
| 2026 holdout | `2026-01-01 …` | Lives in a **separate file/snapshot**. Scored exactly once per training run via `src/eval/holdout.py` and logged to MLflow as `holdout_*`. Never train or tune against it. |

---

## Local quickstart

```bash
poetry install
poetry run pytest tests/                       # uses synthetic frame, no BQ needed

# Full local pipeline (needs GCP creds + project, BQ tables created by Terraform):
poetry run python -m src.ingest.dataset \
  --csv-path notebooks/data/BTCUSDT_1d_merged.csv \
  --project PROJECT --dataset btc_raw --table dataset_unified \
  --symbol BTCUSDT --interval 1d --snapshot
# → SNAPSHOT_ID=PROJECT.btc_snapshots.dataset_unified__20260529_103401

poetry run python -m src.features.build \
  --project PROJECT --dataset btc_snapshots \
  --table dataset_unified__20260529_103401 \
  --symbol BTCUSDT --interval 1d \
  --out-bucket PROJECT-data-features

poetry run python -m src.features.labels \
  --in-path  gs://PROJECT-data-features/btc/1d/features.parquet \
  --out-path gs://PROJECT-data-features/btc/1d/labelled.parquet \
  --task regression --horizon 3
```

---

## How this wires into KFP

`pipelines/training/pipeline.py` (Phase 4 §4.9) calls these as components:

```
ingest_op(merged.csv)    → snap_train_id ─┐
ingest_op(holdout.csv)   → snap_holdout_id│
                           │              │
                           ▼              │
                  features_op → labels_op → train_op
                                            │
                                            └──→ holdout_op(snap_holdout_id) → register_op?
```

`ingest_op` returns the printed `SNAPSHOT_ID=…` line as a KFP output. `features_op`
takes that id apart (`<project>.<dataset>.<table>`) and reads the snapshot
directly. The trainer logs `data_snapshot` to MLflow so every run is traceable
back to a specific (CSV git SHA, BQ snapshot id) pair.

---

## Reproducibility chain

```
git SHA of notebooks/data/*.csv
        │
        ▼
ingest image SHA (CSVs baked in at /app/data/)
        │
        ▼
BQ snapshot id (CREATE SNAPSHOT TABLE … CLONE …, metadata-only)
        │
        ▼
MLflow run params: data_snapshot=<snapshot_id>, data_sha256=<features parquet hash>
        │
        ▼
Model artifact (3 quantile XGB models packaged together)
```

Every step is content-addressed. Re-running with the same image + snapshot id
gives byte-identical features.
