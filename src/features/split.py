"""Strict time-based split. Never shuffle time-series.

Matches notebooks/03_baseline_vs_transformer.ipynb:
  train:   ≤ 2024-12-31
  val:       2025-01-01 → 2025-09-30
  test:      2025-10-01 → 2025-12-31
  holdout:   2026+, loaded separately (never used for fit/early-stopping)
"""
from __future__ import annotations
import pandas as pd


def split(df: pd.DataFrame,
          train_end: str = "2024-12-31",
          val_end:   str = "2025-09-30") -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("timestamp").reset_index(drop=True)
    tr_end = pd.Timestamp(train_end, tz="UTC")
    va_end = pd.Timestamp(val_end,   tz="UTC")
    train = df[df["timestamp"] <= tr_end]
    val   = df[(df["timestamp"] > tr_end) & (df["timestamp"] <= va_end)]
    test  = df[df["timestamp"] > va_end]
    return train, val, test
