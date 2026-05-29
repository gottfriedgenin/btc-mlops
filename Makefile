# btc-mlops developer Makefile.
#
# Override at the CLI:  make compile PROJECT=my-gcp-project REGION=europe-west3
# Discover targets:     make help

SHELL        := /bin/bash
PROJECT      ?= $(shell gcloud config get-value project 2>/dev/null)
REGION       ?= europe-west3
REGISTRY     ?= $(REGION)-docker.pkg.dev/$(PROJECT)/btc-mlops
TAG          ?= latest
MLFLOW_URI   ?= http://mlflow.mlflow.svc.cluster.local:5000

BUILD_DIR    := build
PIPELINE_YML := $(BUILD_DIR)/btc-quantile-train.yaml

CSV_TRAIN    := notebooks/data/BTCUSDT_1d_merged.csv
CSV_HOLDOUT  := notebooks/data/BTCUSDT_1d_2026_holdout.csv

POETRY       := poetry
PY           := $(POETRY) run python

.DEFAULT_GOAL := help

## ─── meta ────────────────────────────────────────────────────────────────
.PHONY: help
help: ## show this help
	@awk 'BEGIN{FS=":.*## "; printf "\nTargets:\n"} \
	      /^[a-zA-Z0-9_.-]+:.*## / {printf "  \033[1m%-22s\033[0m %s\n", $$1, $$2} \
	      /^## / {printf "\n\033[33m%s\033[0m\n", substr($$0,4)}' $(MAKEFILE_LIST)
	@echo
	@echo "Override variables: PROJECT=$(PROJECT)  REGION=$(REGION)  REGISTRY=$(REGISTRY)"

## ─── deps ────────────────────────────────────────────────────────────────
.PHONY: install
install: ## install runtime + dev deps via poetry
	$(POETRY) install

.PHONY: lock
lock: ## refresh poetry.lock
	$(POETRY) lock --no-update

## ─── lint / typecheck / test ─────────────────────────────────────────────
.PHONY: lint
lint: ## ruff check
	$(POETRY) run ruff check src/ tests/ pipelines/

.PHONY: fmt
fmt: ## ruff format (writes)
	$(POETRY) run ruff format src/ tests/ pipelines/

.PHONY: typecheck
typecheck: ## mypy on src/
	$(POETRY) run mypy src/

.PHONY: test
test: ## pytest (uses synthetic frame; no BQ creds needed)
	$(POETRY) run pytest tests/ -q

## ─── KFP pipeline ────────────────────────────────────────────────────────
.PHONY: compile
compile: ## compile KFP DAG → $(PIPELINE_YML)
	REGISTRY=$(REGISTRY) $(PY) pipelines/training/pipeline.py
	@echo "→ $(PIPELINE_YML) ($(shell wc -l < $(PIPELINE_YML) 2>/dev/null) lines)"

.PHONY: smoke
smoke: test compile ## pytest + KFP compile in one shot (use before pushing)

## ─── local CLI dry-runs (need GCP creds + BQ tables) ─────────────────────
.PHONY: ingest-local
ingest-local: ## CSV → BQ + snapshot (train CSV)
	$(PY) -m src.ingest.dataset \
	  --csv-path $(CSV_TRAIN) \
	  --project $(PROJECT) --dataset btc_raw --table dataset_unified \
	  --symbol BTCUSDT --interval 1d \
	  --snapshot --snapshot-dataset btc_snapshots

.PHONY: ingest-holdout-local
ingest-holdout-local: ## CSV → BQ + snapshot (2026 holdout)
	$(PY) -m src.ingest.dataset \
	  --csv-path $(CSV_HOLDOUT) \
	  --project $(PROJECT) --dataset btc_raw --table dataset_unified_2026_holdout \
	  --symbol BTCUSDT --interval 1d \
	  --snapshot --snapshot-dataset btc_snapshots

.PHONY: features-local
features-local: ## features.parquet from SOURCE_TABLE (override at CLI)
	@test -n "$(SOURCE_TABLE)" || { echo "set SOURCE_TABLE=PROJECT.btc_snapshots.dataset_unified__<ts>"; exit 1; }
	$(PY) -m src.features.build \
	  --project $(PROJECT) --source-table $(SOURCE_TABLE) \
	  --symbol BTCUSDT --interval 1d \
	  --out-bucket $(PROJECT)-data-features

.PHONY: labels-local
labels-local: ## labelled.parquet from features.parquet
	$(PY) -m src.features.labels \
	  --in-path  gs://$(PROJECT)-data-features/btc/1d/features.parquet \
	  --out-path gs://$(PROJECT)-data-features/btc/1d/labelled.parquet \
	  --task regression --horizon 3

## ─── docker images ───────────────────────────────────────────────────────
.PHONY: docker-build
docker-build: docker-ingest docker-features docker-train docker-eval ## build all 4 images locally

.PHONY: docker-ingest docker-features docker-train docker-eval
docker-ingest:   ## build ingest image
	docker build -f ci/docker/ingest.Dockerfile   -t $(REGISTRY)/ingest:$(TAG)   .
docker-features: ## build features image
	docker build -f ci/docker/features.Dockerfile -t $(REGISTRY)/features:$(TAG) .
docker-train:    ## build train image
	docker build -f ci/docker/train.Dockerfile    -t $(REGISTRY)/train:$(TAG)    .
docker-eval:     ## build eval image
	docker build -f ci/docker/eval.Dockerfile     -t $(REGISTRY)/eval:$(TAG)     .

.PHONY: docker-push
docker-push: docker-build ## push all 4 to Artifact Registry
	docker push $(REGISTRY)/ingest:$(TAG)
	docker push $(REGISTRY)/features:$(TAG)
	docker push $(REGISTRY)/train:$(TAG)
	docker push $(REGISTRY)/eval:$(TAG)

SA_KEY ?= secrets/gcp_sa.json

.PHONY: ar-auth
ar-auth: ## docker login to AR using SA JSON key ($(SA_KEY))
	@test -f $(SA_KEY) || { echo "missing $(SA_KEY)"; exit 1; }
	cat $(SA_KEY) | docker login -u _json_key --password-stdin https://$(REGION)-docker.pkg.dev

.PHONY: ar-auth-gcloud
ar-auth-gcloud: ## alt: configure docker helper to use active gcloud account
	gcloud auth configure-docker $(REGION)-docker.pkg.dev

## ─── KFP submit ──────────────────────────────────────────────────────────
.PHONY: kfp-port-forward
kfp-port-forward: ## forward KFP UI to localhost:8080 (run in another shell)
	kubectl port-forward -n kubeflow svc/ml-pipeline-ui 8080:80

.PHONY: kfp-submit
kfp-submit: $(PIPELINE_YML) ## submit a run against http://localhost:8080
	$(PY) - <<-PY
	import kfp
	cli = kfp.Client(host="http://localhost:8080")
	cli.create_run_from_pipeline_package(
	    pipeline_file="$(PIPELINE_YML)",
	    arguments={
	        "project":          "$(PROJECT)",
	        "bq_dataset":       "btc_raw",
	        "bq_table":         "dataset_unified",
	        "features_bucket":  "$(PROJECT)-data-features",
	        "models_bucket":    "$(PROJECT)-models",
	        "csv_path":         "/app/data/BTCUSDT_1d_merged.csv",
	        "holdout_csv_path": "/app/data/BTCUSDT_1d_2026_holdout.csv",
	        "holdout_table":    "dataset_unified_2026_holdout",
	        "symbol":           "BTCUSDT",
	        "interval":         "1d",
	        "horizon":          3,
	        "mlflow_uri":       "$(MLFLOW_URI)",
	    },
	    experiment_name="btc-quantile",
	)
	PY

## ─── housekeeping ────────────────────────────────────────────────────────
.PHONY: clean
clean: ## remove build/, caches, pyc
	rm -rf $(BUILD_DIR) .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
