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
# GKE nodes are linux/amd64. macOS arm64 must cross-build via buildx.
# `--push` ships straight to AR without a separate `docker push`.
PLATFORM ?= linux/amd64
BUILDX_FLAGS = buildx build --platform $(PLATFORM)

.PHONY: docker-buildx-setup
docker-buildx-setup: ## create a buildx builder once (idempotent)
	-docker buildx create --name btc-mlops --use 2>/dev/null
	docker buildx inspect --bootstrap

.PHONY: docker-build
docker-build: docker-ingest docker-features docker-train docker-eval docker-serving ## build+push all 5 (cross-arch)

.PHONY: docker-ingest docker-features docker-train docker-eval docker-serving
docker-ingest:   ## build+push ingest image
	docker $(BUILDX_FLAGS) -f ci/docker/ingest.Dockerfile   -t $(REGISTRY)/ingest:$(TAG)   --push .
docker-features: ## build+push features image
	docker $(BUILDX_FLAGS) -f ci/docker/features.Dockerfile -t $(REGISTRY)/features:$(TAG) --push .
docker-train:    ## build+push train image
	docker $(BUILDX_FLAGS) -f ci/docker/train.Dockerfile    -t $(REGISTRY)/train:$(TAG)    --push .
docker-eval:     ## build+push eval image
	docker $(BUILDX_FLAGS) -f ci/docker/eval.Dockerfile     -t $(REGISTRY)/eval:$(TAG)     --push .
docker-serving:  ## build+push serving image
	docker $(BUILDX_FLAGS) -f ci/docker/serving.Dockerfile  -t $(REGISTRY)/serving:$(TAG)  --push .

.PHONY: docker-push
docker-push: docker-build ## alias of docker-build (buildx pushes inline)

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
kfp-port-forward: ## forward KFP UI → http://localhost:8080
	kubectl port-forward -n kubeflow svc/ml-pipeline-ui 8080:80

.PHONY: mlflow-port-forward
mlflow-port-forward: ## forward MLflow UI → http://localhost:5000
	kubectl port-forward -n mlflow svc/mlflow 5000:5000

.PHONY: argocd-port-forward
argocd-port-forward: ## forward ArgoCD UI → https://localhost:8081
	kubectl port-forward -n argocd svc/argocd-server 8081:443

.PHONY: serving-port-forward
serving-port-forward: ## forward btc-serving → http://localhost:8000
	kubectl port-forward -n serving svc/btc-serving 8000:8000

.PHONY: serving-predict
serving-predict: ## hit /predict (run `make serving-port-forward` in another shell first)
	curl -s http://localhost:8000/predict | python -m json.tool

.PHONY: argocd-password
argocd-password: ## print ArgoCD initial-admin password
	@kubectl -n argocd get secret argocd-initial-admin-secret \
	  -o jsonpath='{.data.password}' | base64 -d ; echo

.PHONY: kfp-submit
kfp-submit: $(PIPELINE_YML) ## submit a run against http://localhost:8080
	PROJECT=$(PROJECT) MLFLOW_URI=$(MLFLOW_URI) PIPELINE_YML=$(PIPELINE_YML) \
	  $(PY) scripts/kfp_submit.py

## ─── housekeeping ────────────────────────────────────────────────────────
.PHONY: clean
clean: ## remove build/, caches, pyc
	rm -rf $(BUILD_DIR) .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
