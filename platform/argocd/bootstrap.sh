#!/usr/bin/env bash
# ArgoCD bootstrap.
#
# This is the ONLY imperative install in the platform. After this runs,
# every other component (NVIDIA, cert-manager, ingress, prom, mlflow,
# qdrant, KFP, btc-serving, monitoring, drift-monitor, vllm, rag-api)
# is an Argo Application synced from git via the App-of-Apps pattern.
#

# Get GKE credentials. This is required to run kubectl commands below.
gcloud container clusters get-credentials genops-dev --region europe-west3

# Idempotent — safe to re-run.
set -euo pipefail

ARGO_VERSION="${ARGO_VERSION:-v2.13.0}"

kubectl create ns argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd -f \
  "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGO_VERSION}/manifests/install.yaml"
kubectl wait --for=condition=Available deploy/argocd-server -n argocd --timeout=300s
kubectl wait --for=condition=Available deploy/argocd-repo-server -n argocd --timeout=300s

# Apply the App-of-Apps root. From here on, every commit to main triggers a reconcile.
kubectl apply -f platform/argocd/root-app.yaml

cat <<'EOF'

ArgoCD installed. Get the initial admin password:
  kubectl -n argocd get secret argocd-initial-admin-secret \
    -o jsonpath='{.data.password}' | base64 -d

Open the UI:
  kubectl port-forward -n argocd svc/argocd-server 8081:443

Login: admin / <password from above>. Rotate the password and delete the
initial-admin-secret once you do.

Wait for child apps to sync:
  kubectl get applications -n argocd -w
EOF
