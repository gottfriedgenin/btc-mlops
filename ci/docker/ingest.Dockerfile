FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir pandas pyarrow google-cloud-bigquery pandas-gbq db-dtypes
COPY src/ ./src/
# Bake the committed CSVs into the image. Image SHA = data SHA = reproducibility.
# Rebuilding the image is the only way to change the data the pipeline sees.
COPY notebooks/data/BTCUSDT_1d_merged.csv         /app/data/BTCUSDT_1d_merged.csv
COPY notebooks/data/BTCUSDT_1d_2026_holdout.csv   /app/data/BTCUSDT_1d_2026_holdout.csv
ENV PYTHONPATH=/app
