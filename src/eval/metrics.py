"""Quantile band metrics. Mirrors notebooks/03_baseline_vs_transformer.ipynb cell 3.3."""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error


def band_metrics(band: pd.DataFrame) -> dict[str, float]:
    y = band["y_logret"].values
    mae   = mean_absolute_error(y, band["p50"])
    naive = mean_absolute_error(y, np.zeros(len(band)))
    rmse  = mean_squared_error(y, band["p50"]) ** 0.5
    cov80 = float(((y >= band["p10"]) & (y <= band["p90"])).mean())
    dir_  = float(((band["p50"] > 0) == (y > 0)).mean())
    pxe   = float((abs(band["actual_price"] - band["p50_price"]) / band["now"]).mean() * 100)
    return {
        "mae_logret": float(mae),
        "naive_zero_mae_logret": float(naive),
        "rmse_logret": float(rmse),
        "band80_cov": cov80,
        "dir_acc": dir_,
        "price_mae_pct": pxe,
        "edge_vs_naive": float(naive - mae),  # >0 = model beats random walk
    }
