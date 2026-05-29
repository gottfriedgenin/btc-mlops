FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir numpy pandas pyarrow gcsfs google-cloud-storage google-cloud-bigquery pandas-gbq db-dtypes
COPY src/ ./src/
ENV PYTHONPATH=/app
