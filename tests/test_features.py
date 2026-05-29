import numpy as np
import pandas as pd
from src.features.build import build, add_derived_features, feature_columns
from src.features.labels import label_regression, label_classification


def synth(n=800):
    """Synthetic daily frame with all columns the notebook pipeline expects."""
    rng = np.random.default_rng(0)
    ts = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    close = 30000 + np.cumsum(rng.normal(0, 200, n))
    high, low, open_ = close * 1.01, close * 0.99, close
    vol = rng.uniform(1e3, 1e4, n)
    return pd.DataFrame({
        "timestamp": ts, "symbol": "BTCUSDT", "interval": "1d",
        "open": open_, "high": high, "low": low, "close": close, "volume": vol,
        "rsi": rng.uniform(20, 80, n), "atr": rng.uniform(100, 1000, n),
        "sma": close + rng.normal(0, 100, n), "ema": close + rng.normal(0, 100, n),
        "bollinger_upper": close * 1.02, "bollinger_middle": close,
        "bollinger_lower": close * 0.98,
        "keltner_upper":   close * 1.015, "keltner_middle": close,
        "keltner_lower":   close * 0.985,
    })


def test_no_nan_after_build():
    feats = build(synth(800), warmup_days=60)
    derived = ["logret_1","logret_3","logret_5","logret_10","logret_20",
               "vol_5","vol_20","vol_60",
               "atr_pct","bb_width","kelt_width","px_vs_sma","sma_vs_ema","vol_z_20",
               "dow_sin","dow_cos","month_sin","month_cos"]
    assert feats[derived].isna().sum().sum() == 0


def test_regression_label_drops_last_horizon():
    feats = build(synth(800), warmup_days=60)
    H = 3
    labelled = label_regression(feats, horizon=H)
    assert len(labelled) == len(feats) - H
    assert "y_logret" in labelled.columns
    assert "y_price"  in labelled.columns


def test_classification_label_optional():
    feats = build(synth(800), warmup_days=60)
    labelled = label_classification(feats, horizon=1, eps=0.005)
    assert labelled["y"].isin([0, 1]).all()


def test_feature_columns_excludes_labels():
    feats = build(synth(800), warmup_days=60)
    labelled = label_regression(feats, horizon=3)
    cols = feature_columns(labelled)
    for label_col in ("y", "y_logret", "y_price", "timestamp", "symbol", "interval"):
        assert label_col not in cols


def test_deterministic():
    raw = synth(500)
    pd.testing.assert_frame_equal(build(raw, warmup_days=60), build(raw.copy(), warmup_days=60))
