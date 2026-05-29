"""Load the committed CSV dataset into BigQuery; optionally pin an immutable snapshot.

CSV files under notebooks/data/ are the project's canonical data version
(git SHA = data version). The KFP pipeline upserts them into BQ so downstream
components have a queryable store, then pins a snapshot for per-run reproducibility.

Idempotent per (symbol, interval, [start, end]) window via DELETE-then-APPEND.
ALLOW_FIELD_ADDITION lets new columns auto-extend the BQ schema without
Terraform changes.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import bigquery


def load_csv(csv_path: str, symbol: str, interval: str) -> pd.DataFrame:
    """Read the committed CSV, normalize types to match BQ schema."""
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    # event counts are ints; everything else FLOAT (NULL where original source had no data)
    for col in ("events_in_window", "high_impact_events_in_window"):
        if col in df.columns:
            df[col] = df[col].astype("Int64")
    # CSVs were exported with symbol/interval columns; be defensive in case they weren't.
    if "symbol" not in df.columns:
        df["symbol"] = symbol
    if "interval" not in df.columns:
        df["interval"] = interval
    return df


def upsert_bq(df: pd.DataFrame, project: str, dataset: str, table: str) -> None:
    cli = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.{table}"
    symbol   = df["symbol"].iloc[0]
    interval = df["interval"].iloc[0]
    win_start = df["timestamp"].min().isoformat()
    win_end   = df["timestamp"].max().isoformat()
    # Skip DELETE on first run — table doesn't exist yet and load_table_from_dataframe
    # below will create it. After the table exists, DELETE-then-APPEND gives us idempotent
    # upsert by (symbol, interval, timestamp window).
    try:
        cli.get_table(table_ref)
        # `interval` is a reserved word in BQ Standard SQL — backtick it (and the
        # other identifiers for safety) so the parser treats them as column names.
        cli.query(f"""
            DELETE FROM `{table_ref}`
            WHERE `symbol` = @symbol AND `interval` = @interval
              AND `timestamp` BETWEEN TIMESTAMP(@s) AND TIMESTAMP(@e)
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("symbol",   "STRING", symbol),
            bigquery.ScalarQueryParameter("interval", "STRING", interval),
            bigquery.ScalarQueryParameter("s",        "STRING", win_start),
            bigquery.ScalarQueryParameter("e",        "STRING", win_end),
        ])).result()
    except NotFound:
        print(f"table {table_ref} does not exist yet — skipping DELETE, will be created by load job")
    job = cli.load_table_from_dataframe(
        df, table_ref,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
        ),
    )
    job.result()


def snapshot_table(project: str, dataset: str, src_table: str,
                   snapshot_dataset: str, snapshot_name: str | None = None,
                   expiration_days: int = 365) -> str:
    """Create an immutable BigQuery snapshot of the live table.

    Snapshots in BQ are metadata-only — they reference the parent table's storage
    and only diverge as new rows are added. Cost is near-zero for the lifetime
    of the snapshot. Returns the fully-qualified snapshot table id.
    """
    cli = bigquery.Client(project=project)
    if snapshot_name is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_name = f"{src_table}__{ts}"
    snapshot_id = f"{project}.{snapshot_dataset}.{snapshot_name}"
    src_id      = f"{project}.{dataset}.{src_table}"
    expire = datetime.now(timezone.utc) + timedelta(days=expiration_days)
    cli.query(f"""
        CREATE SNAPSHOT TABLE `{snapshot_id}`
        CLONE `{src_id}`
        OPTIONS (expiration_timestamp = TIMESTAMP "{expire.isoformat()}")
    """).result()
    print(f"snapshot created: {snapshot_id}  expires={expire.date()}")
    return snapshot_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-path", required=True,
                    help="path to committed CSV under notebooks/data/")
    ap.add_argument("--project",  required=True)
    ap.add_argument("--dataset",  default="btc_raw")
    ap.add_argument("--table",    default="dataset_unified")
    ap.add_argument("--symbol",   default="BTCUSDT")
    ap.add_argument("--interval", default="1d")
    # Reproducibility: after upsert, optionally pin an immutable BQ snapshot.
    # Pipeline reads from the snapshot so a re-run trains on bit-identical data.
    ap.add_argument("--snapshot",            action="store_true",
                    help="Create an immutable BQ snapshot after upsert completes")
    ap.add_argument("--snapshot-dataset",    default="btc_snapshots")
    ap.add_argument("--snapshot-name",       default=None,
                    help="Snapshot table name; defaults to <table>__<utc_timestamp>")
    ap.add_argument("--snapshot-expiration-days", type=int, default=365)
    a = ap.parse_args()

    df = load_csv(a.csv_path, a.symbol, a.interval)
    if df.empty:
        raise SystemExit(f"CSV empty: {a.csv_path}")
    upsert_bq(df, a.project, a.dataset, a.table)
    print(f"upserted {len(df)} rows from {a.csv_path} "
          f"({df['timestamp'].min().date()}..{df['timestamp'].max().date()})")

    if a.snapshot:
        snap_id = snapshot_table(
            a.project, a.dataset, a.table,
            snapshot_dataset=a.snapshot_dataset,
            snapshot_name=a.snapshot_name,
            expiration_days=a.snapshot_expiration_days,
        )
        # Print as the LAST line of stdout so KFP can capture it as a step output.
        print(f"SNAPSHOT_ID={snap_id}")


if __name__ == "__main__":
    main()
