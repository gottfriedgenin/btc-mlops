"""Read unified dataset from BigQuery, drop dead columns + warmup, add derived features, write Parquet.

Mirrors notebooks/00_setup.ipynb so notebook results reproduce in the KFP pipeline.
Daily granularity, H=3 day prediction horizon (label applied in src/features/labels.py).
"""
from __future__ import annotations
import argparse
import numpy as np
import pandas as pd
from google.cloud import bigquery


# Stale-flag columns: constant 0 across backfill -> no signal -> drop.
AGE_COLS = [
    "hash_rate_age_hours","difficulty_age_hours","block_time_age_hours",
    "miner_reserves_age_hours","miner_outflows_age_hours","mpi_age_hours",
    "exchange_inflow_age_hours","exchange_outflow_age_hours",
    "exchange_reserves_age_hours","etf_flow_total_age_hours",
]
# ETF launched Jan 2024; mostly empty pre-2024. Drop until series is longer.
ETF_COLS = ["etf_flow_total"]


def drop_dead_cols(df: pd.DataFrame, drop_age: bool = True, drop_etf: bool = True) -> pd.DataFrame:
    if drop_age:
        df = df.drop(columns=[c for c in AGE_COLS if c in df.columns])
    if drop_etf:
        df = df.drop(columns=[c for c in ETF_COLS if c in df.columns])
    return df


def drop_indicator_warmup(df: pd.DataFrame, warmup_days: int = 365) -> pd.DataFrame:
    """Slow indicators (Ichimoku senkou_b ~52w, EMA200) need ~1y warmup."""
    cutoff = df["timestamp"].min() + pd.Timedelta(days=warmup_days)
    return df[df["timestamp"] >= cutoff].reset_index(drop=True)


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Returns, vol, regime, calendar — daily granularity."""
    df = df.sort_values("timestamp").reset_index(drop=True).copy()

    # Log returns over multiple horizons (momentum at different timescales)
    for k in (1, 3, 5, 10, 20):
        df[f"logret_{k}"] = np.log(df["close"] / df["close"].shift(k))

    # Realized volatility (rolling stdev of 1-day returns)
    df["vol_5"]  = df["logret_1"].rolling(5).std()
    df["vol_20"] = df["logret_1"].rolling(20).std()
    df["vol_60"] = df["logret_1"].rolling(60).std()

    # Regime features (price/vol-scale normalized)
    df["atr_pct"]    = df["atr"] / df["close"]
    df["bb_width"]   = (df["bollinger_upper"] - df["bollinger_lower"]) / df["bollinger_middle"]
    df["kelt_width"] = (df["keltner_upper"]   - df["keltner_lower"])   / df["keltner_middle"]

    # Trend strength
    df["px_vs_sma"]   = df["close"] / df["sma"] - 1
    df["sma_vs_ema"]  = df["sma"]   / df["ema"] - 1

    # Volume regime
    df["vol_z_20"] = (df["volume"] - df["volume"].rolling(20).mean()) / df["volume"].rolling(20).std()

    # Cyclical calendar encoding
    dt = df["timestamp"].dt
    df["dow_sin"]   = np.sin(2 * np.pi * dt.dayofweek / 7)
    df["dow_cos"]   = np.cos(2 * np.pi * dt.dayofweek / 7)
    df["month_sin"] = np.sin(2 * np.pi * (dt.month - 1) / 12)
    df["month_cos"] = np.cos(2 * np.pi * (dt.month - 1) / 12)

    return df


def feature_columns(df: pd.DataFrame, exclude=None) -> list[str]:
    drop = {"timestamp", "symbol", "interval", "y", "y_logret", "y_price"}
    if exclude:
        drop |= set(exclude)
    return [c for c in df.columns if c not in drop and pd.api.types.is_numeric_dtype(df[c])]


def build(df: pd.DataFrame, warmup_days: int = 365) -> pd.DataFrame:
    df = drop_dead_cols(df)
    df = drop_indicator_warmup(df, warmup_days=warmup_days)
    df = add_derived_features(df)
    return df.dropna().reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project",      required=True, help="billing project for BQ client")
    ap.add_argument("--source-table", required=True,
                    help="fully-qualified BQ ref, e.g. PROJECT.btc_snapshots.dataset_unified__<ts>")
    ap.add_argument("--symbol",       default="BTCUSDT")
    ap.add_argument("--interval",     default="1d")
    ap.add_argument("--out-bucket",   required=True)
    a = ap.parse_args()
    cli = bigquery.Client(project=a.project)
    # `interval` is a reserved word in BQ Standard SQL — backtick it.
    sql = f"""
        SELECT *
        FROM `{a.source_table}`
        WHERE `symbol` = @symbol AND `interval` = @interval
        ORDER BY `timestamp`
    """
    job = cli.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("symbol",   "STRING", a.symbol),
        bigquery.ScalarQueryParameter("interval", "STRING", a.interval),
    ]))
    df = job.to_dataframe()
    feats = build(df)
    out = f"gs://{a.out_bucket}/btc/{a.interval}/features.parquet"
    feats.to_parquet(out, index=False)
    print(f"wrote {len(feats)} feature rows, {len(feats.columns)} cols → {out}")


if __name__ == "__main__":
    main()
