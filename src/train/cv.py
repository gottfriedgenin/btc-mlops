"""Walk-forward CV for honest metric estimation on time-series.

Expands the training window each fold; tests on the next contiguous block.
Never shuffles. Returns a list of band_metrics dicts, one per fold.
"""
from __future__ import annotations
import pandas as pd
from src.train.model import QuantileTriple
from src.features.build import feature_columns
from src.eval.metrics import band_metrics


def walk_forward_eval(df: pd.DataFrame, n_folds: int = 6, fold_days: int = 60) -> list[dict]:
    df = df.sort_values("timestamp").reset_index(drop=True)
    feats = feature_columns(df)
    out = []
    end = df["timestamp"].max()
    cutoffs = [end - pd.Timedelta(days=fold_days * (i + 1)) for i in range(n_folds)][::-1]
    for cutoff in cutoffs:
        tr = df[df["timestamp"] <= cutoff]
        te = df[(df["timestamp"] > cutoff)
                & (df["timestamp"] <= cutoff + pd.Timedelta(days=fold_days))]
        if len(tr) < 200 or len(te) < 20:
            continue
        # use the last 10% of train as inner-val for early stopping
        cut = int(len(tr) * 0.9)
        inner_tr, inner_va = tr.iloc[:cut], tr.iloc[cut:]
        triple = QuantileTriple.train(
            inner_tr, inner_tr["y_logret"], inner_va, inner_va["y_logret"], feats
        )
        band = triple.predict_band(te)
        m = band_metrics(band)
        m["fold_cutoff"] = str(cutoff.date())
        m["fold_n"]      = len(te)
        out.append(m)
    return out
