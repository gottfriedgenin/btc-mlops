# `platform/` — Cluster + GitOps layer

Everything that runs *in* the GKE cluster but is NOT pipeline business logic.
GitOps via Argo CD; the only imperative step is the bootstrap script.

```
platform/
├── argocd/                     # Argo CD itself + App-of-Apps wiring
│   ├── bootstrap.sh            # The ONE imperative install. After this, everything else is git-synced.
│   ├── root-app.yaml           # Treats every YAML in apps/ as a child Application.
│   └── apps/                   # One YAML per platform component (Application or ApplicationSet).
│       ├── kubeflow-pipelines.yaml      # KFP CRDs + controllers + UI (upstream kustomize, pinned tag)
│       ├── kubeflow-pipelines-cluster.yaml
│       ├── kfp-workload-identity.yaml   # Patches the `pipeline-runner` KSA WI annotation
│       ├── mlflow.yaml                  # community-charts/mlflow with our values override
│       ├── btc-serving.yaml             # Phase 5 — FastAPI predict service (see helm/btc-serving)
│       ├── btc-monitoring.yaml          # Phase 8 — placeholder
│       ├── drift-monitor.yaml           # Phase 8 — placeholder
│       ├── kube-prometheus.yaml         # Stack: Prometheus + Alertmanager + Grafana
│       ├── cert-manager.yaml
│       ├── nvidia.yaml                  # GPU device plugin (for vllm later)
│       ├── qdrant.yaml                  # RAG vector store (future)
│       ├── rag-api.yaml                 # placeholder
│       └── vllm.yaml                    # placeholder
├── helm/                        # Helm charts we own (project-id always injected, never committed)
│   ├── kfp-workload-identity/   # Single SA + annotation
│   ├── btc-serving/             # Phase 5 service deployment + SA + Service
│   └── values/                  # values.yaml files consumed by upstream charts
│       └── mlflow.yaml
└── manifests/                   # Non-Helm static manifests for ApplicationSets
    ├── kfp/                     # (empty — kept for future patches over upstream KFP)
    └── nvidia/                  # GKE device-plugin daemonset + installer
```

## How a commit becomes cluster state

1. Edit a file in `platform/`.
2. Push to `main`.
3. Argo root app polls every ~3 min, re-reads `platform/argocd/apps/`,
   reconciles each child app against its source.
4. Child apps render their manifests/charts and apply.

The full design contract is in
[`platform/argocd/bootstrap.sh`](./argocd/bootstrap.sh) and
[`root-app.yaml`](./argocd/root-app.yaml). Read those first when adding a new
component.

## Project-id-out-of-git pattern

`bootstrap.sh` creates an in-cluster `Secret` named `in-cluster` with an
annotation `gcp_project=<project>`. Every ApplicationSet that needs the
project id reads it from that annotation at template time and passes it
to its Helm chart as a parameter. **No project id ever lands in git.**

```yaml
# excerpt from any of our ApplicationSets
parameters:
  - name: gcpProject
    value: '{{ index .metadata.annotations "gcp_project" }}'
```

## Sync ordering

Two apps both manage the `pipeline-runner` KSA (KFP defines it, the
`kfp-workload-identity` chart patches its annotation). To avoid the race:

1. `kubeflow-pipelines.yaml`     has `argocd.argoproj.io/sync-wave: "-1"`
2. `kfp-workload-identity.yaml`  has `argocd.argoproj.io/sync-wave: "1"`
3. `kubeflow-pipelines.yaml` also has `ignoreDifferences` on `/metadata/annotations`
4. `bootstrap.sh` runs a final `kubectl annotate` as belt-and-suspenders.

If you add a third app that touches the same KSA, extend the
`ignoreDifferences` block on `kubeflow-pipelines.yaml`.

## Cluster recreate flow

```bash
gcloud container clusters get-credentials genops-dev --region europe-west3
cd platform/argocd && GCP_PROJECT=$(gcloud config get-value project) ./bootstrap.sh
kubectl -n argocd get applications.argoproj.io -w     # watch them converge
```

All other infra (buckets, BQ datasets, SAs, AR images) live outside the
cluster and survive a destroy/recreate.
