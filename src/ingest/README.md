# `src/ingest/` — CSV → BigQuery → Snapshot

## Purpose

Take a committed CSV (the project's canonical data version) and:

1. Load it into a working BigQuery table (`btc_raw.dataset_unified`).
2. **Pin an immutable snapshot** (`btc_snapshots.dataset_unified__<ts>`) so
   downstream pipeline steps read a fixed view that can't be changed by a
   concurrent re-ingest or a future CSV refresh.

## Files

| File | Role |
|---|---|
| `dataset.py` | All of the above. Single module, runs as `python -m src.ingest.dataset`. |
| `__init__.py` | Package marker. Empty. |

## `dataset.py` — function map

| Function | Inputs | Output | Notes |
|---|---|---|---|
| `load_csv(csv_path, symbol, interval)` | CSV path + symbol/interval defaults | `pd.DataFrame` | Coerces `timestamp` → UTC. Forces `Int64` on `events_in_window` / `high_impact_events_in_window`. Backfills `symbol`/`interval` columns if missing. |
| `upsert_bq(df, project, dataset, table)` | DataFrame + BQ coordinates | None | Idempotent: `DELETE` over `(symbol, interval, [min_ts, max_ts])` then `WRITE_APPEND`. `ALLOW_FIELD_ADDITION` lets new columns auto-extend the schema. |
| `snapshot_table(project, dataset, src_table, snapshot_dataset, snapshot_name=None, expiration_days=365)` | source table + snapshot dataset | snapshot id (`PROJECT.DS.TBL__YYYYMMDD_HHMMSS`) | Runs `CREATE SNAPSHOT TABLE … CLONE …`. Metadata-only — no row copy. |
| `main()` | argparse | stdout | CLI entrypoint. Prints `SNAPSHOT_ID=…` as the **last line** of stdout so KFP can capture it as a step output. |

## CLI

```bash
poetry run python -m src.ingest.dataset \
  --csv-path notebooks/data/BTCUSDT_1d_merged.csv \
  --project PROJECT --dataset btc_raw --table dataset_unified \
  --symbol BTCUSDT --interval 1d \
  --snapshot --snapshot-dataset btc_snapshots --snapshot-expiration-days 365
```

| Arg | Default | Purpose |
|---|---|---|
| `--csv-path` | (required) | Committed CSV under `notebooks/data/`. In the KFP image it's at `/app/data/BTCUSDT_1d_merged.csv`. |
| `--project` | (required) | GCP project id. |
| `--dataset` | `btc_raw` | BQ dataset that holds the live working table. Created by Terraform. |
| `--table` | `dataset_unified` | Live table name. For the holdout CSV use `dataset_unified_2026_holdout`. |
| `--symbol` / `--interval` | `BTCUSDT` / `1d` | Used for the `DELETE` window scoping. |
| `--snapshot` | off | Toggle the snapshot step. KFP always sets this. |
| `--snapshot-dataset` | `btc_snapshots` | Where snapshots live. Created by Terraform. |
| `--snapshot-name` | `<table>__<utc_timestamp>` | Override only if you need a deterministic name (rare). |
| `--snapshot-expiration-days` | 365 | After expiry the metadata-only row is auto-deleted. Set `NULL` via `ALTER SNAPSHOT TABLE` for promoted-to-Production runs. |

## Stdout contract

The last line of stdout is parsed by `ingest_op` in
[`pipelines/training/pipeline.py`](../../pipelines/training/pipeline.py) to
pass the snapshot id downstream. **Do not log after it.**

```
upserted 2437 rows from /app/data/BTCUSDT_1d_merged.csv (2019-05-01..2025-12-31)
snapshot created: PROJECT.btc_snapshots.dataset_unified__20260529_103401  expires=2027-05-29
SNAPSHOT_ID=PROJECT.btc_snapshots.dataset_unified__20260529_103401    ← last line
```

## Why a snapshot

The live BQ table is mutable. Without snapshotting:

- A re-ingest mid-pipeline-run would change the rows the trainer sees.
- Two concurrent runs would race on the same table.
- A CSV refresh in git would silently change historical training behavior.

`CREATE SNAPSHOT TABLE` decouples the pipeline run from the live table. The
snapshot is **metadata-only**: it references the parent table's storage and
only diverges as new rows are added. Cost is near-zero.

## How this fits the bigger picture

```
                       ┌──────────┐
   committed CSV  ───▶ │ load_csv │──▶ DataFrame
                       └──────────┘
                            │
                       ┌──────────┐
                       │ upsert_bq│──▶ PROJECT.btc_raw.dataset_unified  ◀── mutable working table
                       └──────────┘
                            │
                       ┌──────────────┐
                       │ snapshot_table│──▶ PROJECT.btc_snapshots.dataset_unified__<ts>  ◀── immutable
                       └──────────────┘
                            │
                            ▼  (consumed by src/features/build.py)
```

## Failure modes + behavior

| Symptom | Cause | Fix |
|---|---|---|
| `CSV empty: …` | File missing or truly empty | Verify the path; in the KFP image check the `COPY` step of `ingest.Dockerfile`. |
| `Table NOT_FOUND` on first run | Terraform hasn't run yet | `cd infra && terraform apply` (creates `btc_raw.dataset_unified` + `btc_snapshots`). |
| `403 PERMISSION_DENIED` on snapshot | Wrong WI binding | Pod must run as `training-job-sa@…`; that SA needs `roles/bigquery.dataEditor` on both datasets. |
| Snapshot already exists with same name | You passed `--snapshot-name` twice in the same UTC second | Drop the `--snapshot-name` flag (auto-stamped names include H:M:S). |

## Cost note

- **Storage:** `CREATE SNAPSHOT TABLE` is metadata-only at creation. You pay
  only for rows that change in the live table after the snapshot was made.
  For an append-mostly daily table the lifetime cost is cents per year.
- **Compute:** One DML `DELETE` + one load job per ingest. <$0.01.
