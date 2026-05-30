FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir \
      pandas pyarrow numpy scikit-learn xgboost mlflow \
      fastapi 'uvicorn[standard]' prometheus-client \
      gcsfs google-cloud-storage google-cloud-bigquery pandas-gbq db-dtypes
COPY src/ ./src/
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "src.serving.app:app", "--host", "0.0.0.0", "--port", "8000"]
