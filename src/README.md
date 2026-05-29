# `src/` — Pipeline code

Python code that runs inside the KFP training pipeline (and is exercised by
`pytest tests/` + the notebooks). One responsibility per package.

| Package | Detail | Responsibility |
|---|---|---|
| [`src/ingest/`](./ingest/README.md)    | [README](./ingest/README.md)   | Load committed CSV → BigQuery, pin an immutable snapshot per pipeline run. |
| [`src/features/`](./features/README.md)| [README](./features/README.md) | Drop dead columns + warmup, add derived features (returns, vol, regime, calendar), attach labels, strict time-split. |
| [`src/train/`](./train/README.md)      | [README](./train/README.md)    | Train three XGBoost quantile regressors (P10/P50/P90), log to MLflow, ship artifacts to GCS. |
| [`src/eval/`](./eval/README.md)        | [README](./eval/README.md)     | Band/edge metrics + 2026 holdout scorer that drives the promotion gate. |

## Dataflow

```
        notebooks/data/BTCUSDT_1d_merged.csv         ←── git SHA = data version
        notebooks/data/BTCUSDT_1d_2026_holdout.csv       (CSVs baked into ingest image)
                       │
                       ▼
              ┌──────────────────────┐
              │  src/ingest/         │   CSV → BQ + CREATE SNAPSHOT
              └────────┬─────────────┘
                       │ SNAPSHOT_ID
                       ▼
              ┌──────────────────────┐
              │  src/features/build  │   drop dead cols + warmup + derived
              └────────┬─────────────┘
                       │ features.parquet
                       ▼
              ┌──────────────────────┐
              │  src/features/labels │   y_logret + y_price (H=3 d)
              └────────┬─────────────┘
                       │ labelled.parquet
                       ▼
              ┌──────────────────────┐
              │  src/features/split  │   train ≤ 2024-12-31, val ≤ 2025-09-30, test rest
              └────────┬─────────────┘
                       ▼
              ┌──────────────────────┐
              │  src/train/          │   QuantileTriple → MLflow + GCS
              └────────┬─────────────┘
                       ▼
              ┌──────────────────────┐
              │  src/eval/holdout    │   score 2026 holdout once → promotion gate
              └──────────────────────┘
```

The KFP DAG that wires these together lives at
[`pipelines/training/pipeline.py`](../pipelines/training/pipeline.py).

## Conventions

- **No external services at run time.** The CSVs are the source of truth.
- **Time-series safe.** `src/features/split.py` is strict-by-date; nothing
  shuffles. Walk-forward CV lives in `src/train/cv.py`.
- **Column conventions:**
  - Keys: `timestamp` (UTC), `symbol`, `interval`
  - Labels: `y_logret` (regression), `y_price`, `y` (classification, optional)
  - Helper rows in band output: `now` (price at prediction time), `ts`
- **Reproducibility chain:** git SHA (CSV) → image SHA → BQ snapshot id →
  MLflow params (`data_snapshot`, `data_sha256`) → model artifact.

## Local quickstart

```bash
poetry install
poetry run pytest tests/             # synthetic frame; no BQ creds needed
poetry run python pipelines/training/pipeline.py   # compiles KFP YAML
```

For the full CSV→BQ→snapshot→features→labels→train→holdout chain see each
sub-README.
