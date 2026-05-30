# `platform/helm/` — Our own Helm charts + values overrides

Charts here are kept tiny on purpose — Argo CD ApplicationSets render them
with the GCP project id injected from the in-cluster Secret annotation
(see [`../README.md`](../README.md)). No project id ever lands in git.

## Layout

| Path | Type | Used by |
|---|---|---|
| `kfp-workload-identity/` | Chart | [`apps/kfp-workload-identity.yaml`](../argocd/apps/kfp-workload-identity.yaml) — single ServiceAccount with `iam.gke.io` annotation |
| `btc-serving/`           | Chart | [`apps/btc-serving.yaml`](../argocd/apps/btc-serving.yaml) — Phase 5 FastAPI predict service (SA + Deployment + Service). See [`btc-serving/README.md`](./btc-serving/README.md). |
| `values/`                | Values overrides | Consumed by upstream Helm charts referenced from `apps/` (currently only `mlflow.yaml`). |

## When to add a chart here vs. point to upstream

Add a chart in `platform/helm/<name>/` if:
- The thing is small / project-specific (a SA + an annotation; a single
  Deployment + Service)
- You need to inject the GCP project id

Use an upstream chart (referenced from `apps/<name>.yaml` with a
`valueFiles` override in `platform/helm/values/`) if:
- A reputable chart already exists (mlflow, kube-prometheus, etc.)
- Your customisation fits inside its `values.yaml`

## Project-id injection

ApplicationSets template the project id at render time:

```yaml
helm:
  parameters:
    - name: gcpProject
      value: '{{ index .metadata.annotations "gcp_project" }}'
```

Helpers then derive bucket names / image repos in the chart's
`templates/` or in the ApplicationSet's `parameters` block — see the
btc-serving ApplicationSet for an example with derived `modelsBucket`
and `image.repository`.
