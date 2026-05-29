"""XGBoost quantile-regression wrapper. Trains P10/P50/P90, predicts a price band.

Output for each row at time T:
    p10, p50, p90      → log-return percentiles for T+H
    p10_price, p50_price, p90_price → reconstructed USD prices
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd
import xgboost as xgb


HYPERPARAMS = dict(
    objective="reg:quantileerror",
    max_depth=5,
    n_estimators=800,
    learning_rate=0.04,
    subsample=0.85,
    colsample_bytree=0.85,
    tree_method="hist",
    early_stopping_rounds=50,
    verbosity=0,
)


@dataclass
class QuantileTriple:
    m10: xgb.XGBRegressor
    m50: xgb.XGBRegressor
    m90: xgb.XGBRegressor
    features: list[str]

    @classmethod
    def train(cls, Xtr: pd.DataFrame, ytr: pd.Series,
              Xva: pd.DataFrame, yva: pd.Series,
              features: list[str]) -> "QuantileTriple":
        def fit_one(alpha):
            m = xgb.XGBRegressor(quantile_alpha=alpha, **HYPERPARAMS)
            m.fit(Xtr[features], ytr, eval_set=[(Xva[features], yva)], verbose=False)
            return m
        return cls(m10=fit_one(0.10), m50=fit_one(0.50), m90=fit_one(0.90), features=features)

    def predict_band(self, d: pd.DataFrame) -> pd.DataFrame:
        """Returns ts / now / y_logret + p10/p50/p90 + reconstructed prices."""
        out = pd.DataFrame({
            "ts":       d["timestamp"].values,
            "now":      d["close"].values,
            "y_logret": d.get("y_logret", pd.Series([np.nan]*len(d))).values,
            "p10":      self.m10.predict(d[self.features]),
            "p50":      self.m50.predict(d[self.features]),
            "p90":      self.m90.predict(d[self.features]),
        })
        out["p10_price"] = out["now"] * np.exp(out["p10"])
        out["p50_price"] = out["now"] * np.exp(out["p50"])
        out["p90_price"] = out["now"] * np.exp(out["p90"])
        if not out["y_logret"].isna().all():
            out["actual_price"] = out["now"] * np.exp(out["y_logret"])
        return out

    def save(self, out_dir: str) -> None:
        import os, json
        os.makedirs(out_dir, exist_ok=True)
        self.m10.save_model(f"{out_dir}/m10.json")
        self.m50.save_model(f"{out_dir}/m50.json")
        self.m90.save_model(f"{out_dir}/m90.json")
        with open(f"{out_dir}/features.json", "w") as f:
            json.dump({"features": self.features, "hyperparams": HYPERPARAMS}, f, indent=2)

    @classmethod
    def load(cls, in_dir: str) -> "QuantileTriple":
        import json
        with open(f"{in_dir}/features.json") as f:
            meta = json.load(f)
        def load_one(path):
            m = xgb.XGBRegressor()
            m.load_model(path)
            return m
        return cls(
            m10=load_one(f"{in_dir}/m10.json"),
            m50=load_one(f"{in_dir}/m50.json"),
            m90=load_one(f"{in_dir}/m90.json"),
            features=meta["features"],
        )
