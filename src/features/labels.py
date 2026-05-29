"""Labels for the BTC trainer.

Primary: regression. Predict H-day forward log-return (`y_logret`). Reconstruct
price downstream via `close[t] * exp(prediction)`. Notebook 03 validated H=3.

Optional: classification. Direction with epsilon dead-band. Kept for ablation
experiments; not the production label.
"""
from __future__ import annotations
import argparse
import numpy as np
import pandas as pd


def label_regression(df: pd.DataFrame, horizon: int = 3) -> pd.DataFrame:
    out = df.copy()
    out["y_logret"] = np.log(out["close"].shift(-horizon) / out["close"])
    out["y_price"]  = out["close"].shift(-horizon)
    return out.dropna(subset=["y_logret"]).reset_index(drop=True)


def label_classification(df: pd.DataFrame, horizon: int = 1, eps: float = 0.005) -> pd.DataFrame:
    out = df.copy()
    future = out["close"].shift(-horizon)
    out["y"] = (future > out["close"] * (1 + eps)).astype("Int64")
    out.loc[future.isna(), "y"] = pd.NA
    return out.dropna(subset=["y"]).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-path",  required=True)
    ap.add_argument("--out-path", required=True)
    ap.add_argument("--task",     choices=["regression", "classification"], default="regression")
    ap.add_argument("--horizon",  type=int, default=3)
    ap.add_argument("--eps",      type=float, default=0.005, help="classification dead-band")
    a = ap.parse_args()
    df = pd.read_parquet(a.in_path)
    if a.task == "regression":
        out = label_regression(df, horizon=a.horizon)
        print(f"wrote {len(out)} rows  y_logret mean={out['y_logret'].mean():+.4f}  std={out['y_logret'].std():.4f}")
    else:
        out = label_classification(df, horizon=a.horizon, eps=a.eps)
        print(f"wrote {len(out)} rows  pos_rate={out['y'].mean():.3f}")
    out.to_parquet(a.out_path, index=False)


if __name__ == "__main__":
    main()
