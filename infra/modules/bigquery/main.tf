variable "project_id" { type = string }
variable "region"     { type = string }

resource "google_bigquery_dataset" "btc_raw" {
  dataset_id                 = "btc_raw"
  location                   = var.region
  description                = "Unified dataset rows from dataset-service (OHLCV + indicators + network + onchain + ETF + events)"

  # Keep the data layer alive across `tf destroy`. Re-ingesting 6 years of
  # daily history isn't catastrophic, but re-ingesting alt-data is — providers
  # rate-limit and some series are only available going forward.
  delete_contents_on_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

# Immutable per-pipeline-run snapshots (CREATE SNAPSHOT TABLE ... CLONE ...).
# See plan/04-data-features.md §3.8. Snapshots themselves expire individually
# via expiration_timestamp; the dataset holding them lives forever.
resource "google_bigquery_dataset" "btc_snapshots" {
  dataset_id  = "btc_snapshots"
  location    = var.region
  description = "Per-training-run BQ snapshots of btc_raw.dataset_unified for reproducibility"

  delete_contents_on_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_bigquery_table" "dataset_unified" {
  dataset_id = google_bigquery_dataset.btc_raw.dataset_id
  table_id   = "dataset_unified"

  # BQ-side guard: even `bq rm` errors without --force when this is true.
  # Belt-and-braces with the terraform lifecycle below.
  deletion_protection = true

  time_partitioning {
    type  = "MONTH"
    field = "timestamp"
  }
  clustering = ["symbol", "interval"]

  # Columns track composer.columnOrder in dataset-service.
  # New indicator suffix columns (e.g. "macd_histogram_fast") get appended at load
  # time via WriteDisposition=APPEND + schema_update_options=ALLOW_FIELD_ADDITION.
  schema = jsonencode([
    # Base
    { name = "timestamp", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "symbol",    type = "STRING",    mode = "REQUIRED" },
    { name = "interval",  type = "STRING",    mode = "REQUIRED" },

    # OHLCV
    { name = "open",   type = "FLOAT64" },
    { name = "high",   type = "FLOAT64" },
    { name = "low",    type = "FLOAT64" },
    { name = "close",  type = "FLOAT64" },
    { name = "volume", type = "FLOAT64" },

    # Indicators — single value
    { name = "sma",               type = "FLOAT64" },
    { name = "ema",               type = "FLOAT64" },
    { name = "rsi",               type = "FLOAT64" },
    { name = "cci",               type = "FLOAT64" },
    { name = "atr",               type = "FLOAT64" },
    { name = "momentum",          type = "FLOAT64" },
    { name = "williams_r",        type = "FLOAT64" },
    { name = "obv",               type = "FLOAT64" },
    { name = "vwap",              type = "FLOAT64" },
    { name = "ad_line",           type = "FLOAT64" },
    { name = "volume_oscillator", type = "FLOAT64" },

    # Indicators — multi-key (named indicator + "_" + key per composer.indicatorColumn)
    { name = "macd_macd",          type = "FLOAT64" },
    { name = "macd_signal",        type = "FLOAT64" },
    { name = "macd_histogram",     type = "FLOAT64" },
    { name = "adx_adx",            type = "FLOAT64" },
    { name = "adx_plus_di",        type = "FLOAT64" },
    { name = "adx_minus_di",       type = "FLOAT64" },
    { name = "stochastic_k",       type = "FLOAT64" },
    { name = "stochastic_d",       type = "FLOAT64" },
    { name = "bollinger_upper",    type = "FLOAT64" },
    { name = "bollinger_middle",   type = "FLOAT64" },
    { name = "bollinger_lower",    type = "FLOAT64" },
    { name = "keltner_upper",      type = "FLOAT64" },
    { name = "keltner_middle",     type = "FLOAT64" },
    { name = "keltner_lower",      type = "FLOAT64" },
    { name = "ichimoku_tenkan",    type = "FLOAT64" },
    { name = "ichimoku_kijun",     type = "FLOAT64" },
    { name = "ichimoku_senkou_a",  type = "FLOAT64" },
    { name = "ichimoku_senkou_b",  type = "FLOAT64" },
    { name = "ichimoku_chikou",    type = "FLOAT64" },

    # BTC network (composer NetworkMetrics)
    { name = "hash_rate",            type = "FLOAT64" },
    { name = "hash_rate_age_hours",  type = "FLOAT64" },
    { name = "difficulty",           type = "FLOAT64" },
    { name = "difficulty_age_hours", type = "FLOAT64" },
    { name = "block_time",           type = "FLOAT64" },
    { name = "block_time_age_hours", type = "FLOAT64" },

    # Onchain (composer OnchainMetrics)
    { name = "miner_reserves",             type = "FLOAT64" },
    { name = "miner_reserves_age_hours",   type = "FLOAT64" },
    { name = "miner_outflows",             type = "FLOAT64" },
    { name = "miner_outflows_age_hours",   type = "FLOAT64" },
    { name = "mpi",                        type = "FLOAT64" },
    { name = "mpi_age_hours",              type = "FLOAT64" },
    { name = "exchange_inflow",            type = "FLOAT64" },
    { name = "exchange_inflow_age_hours",  type = "FLOAT64" },
    { name = "exchange_outflow",           type = "FLOAT64" },
    { name = "exchange_outflow_age_hours", type = "FLOAT64" },
    { name = "exchange_reserves",          type = "FLOAT64" },
    { name = "exchange_reserves_age_hours",type = "FLOAT64" },

    # ETF
    { name = "etf_flow_total",           type = "FLOAT64" },
    { name = "etf_flow_total_age_hours", type = "FLOAT64" },

    # Events
    { name = "events_in_window",             type = "INT64" },
    { name = "high_impact_events_in_window", type = "INT64" }
  ])

  lifecycle {
    prevent_destroy = true
  }
}

output "dataset_id"           { value = google_bigquery_dataset.btc_raw.dataset_id }
output "dataset_table_id"     { value = google_bigquery_table.dataset_unified.table_id }
output "snapshots_dataset_id" { value = google_bigquery_dataset.btc_snapshots.dataset_id }