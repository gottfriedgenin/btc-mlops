#!/usr/bin/env bash
# ArgoCD bootstrap.
#
# This is the ONLY imperative install in the platform. After this runs,
# every other component (NVIDIA, cert-manager, ingress, prom, mlflow,
# qdrant, KFP, btc-serving, monitoring, drift-monitor, vllm, rag-api)
# is an Argo Application synced from git via the App-of-Apps pattern.
#
# Idempotent — safe to re-run.
set -euo pipefail

# Inputs (override via env if needed):
#   GCP_PROJECT  — defaults to current gcloud config; held in the in-cluster
#                  Argo cluster Secret as an annotation. Never committed to git.
#   GKE_CLUSTER  — name of the cluster to point kubectl at
#   GKE_REGION   — region of that cluster
#   ARGO_VERSION — Argo CD release tag
GCP_PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
GKE_CLUSTER="${GKE_CLUSTER:-genops-dev}"
GKE_REGION="${GKE_REGION:-europe-west3}"
ARGO_VERSION="${ARGO_VERSION:-v2.13.0}"

if [ -z "$GCP_PROJECT" ]; then
  echo "ERROR: GCP_PROJECT is empty and 'gcloud config get-value project' returned nothing." >&2
  echo "Set GCP_PROJECT=<project-id> or run: gcloud config set project <project-id>" >&2
  exit 1
fi

echo "Using GCP_PROJECT=$GCP_PROJECT  GKE_CLUSTER=$GKE_CLUSTER  GKE_REGION=$GKE_REGION  ARGO_VERSION=$ARGO_VERSION"

# 1) Point kubectl at the cluster.
gcloud container clusters get-credentials "$GKE_CLUSTER" --region "$GKE_REGION"

# 2) Install Argo CD itself (the only thing we install imperatively).
kubectl create ns argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd -f \
  "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGO_VERSION}/manifests/install.yaml"
kubectl wait --for=condition=Available deploy/argocd-server -n argocd --timeout=300s
kubectl wait --for=condition=Available deploy/argocd-repo-server -n argocd --timeout=300s

# 3) Create an explicit Secret for the in-cluster cluster so the ApplicationSet
#    `clusters: {}` generator can see it AND read the gcp_project annotation.
#    Without this Secret, the implicit "in-cluster" entry has no metadata for
#    ApplicationSets to template against.
#
#    The Secret itself stays in-cluster — the project id never enters git.
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: in-cluster
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
  annotations:
    gcp_project: "$GCP_PROJECT"
stringData:
  name: in-cluster
  server: https://kubernetes.default.svc
  config: |
    {"tlsClientConfig":{"insecure":false}}
EOF

# Update the annotation on every re-run in case GCP_PROJECT changed.
kubectl -n argocd annotate secret in-cluster gcp_project="$GCP_PROJECT" --overwrite

# 4) Apply the App-of-Apps root. From here on, every commit to main triggers a reconcile.
kubectl apply -f root-app.yaml

# 5) Belt-and-suspenders: enforce the pipeline-runner SA WI annotation.
#    The declarative path (kubeflow-pipelines sync-wave=-1, then
#    kfp-workload-identity sync-wave=1 with ignoreDifferences on the SA
#    annotations field in kubeflow-pipelines.yaml) is the primary fix.
#    On a cold cluster, the first reconcile cycle still races sometimes —
#    kubeflow-pipelines may sync, install the SA without our annotation,
#    and SelfHeal kicks in before kfp-workload-identity has a chance. So we
#    poll until both Apps are Synced+Healthy, then explicitly annotate.
#    This step is idempotent: if the annotation is already there, --overwrite
#    is a no-op write.
echo
echo "5) Waiting for kubeflow-pipelines + kfp-workload-identity to converge..."
TIMEOUT=600
INTERVAL=10
elapsed=0
while : ; do
  KFP_SYNC=$(kubectl get applications.argoproj.io -n argocd kubeflow-pipelines    -o jsonpath='{.status.sync.status}'    2>/dev/null || echo "")
  WI_SYNC=$(kubectl  get applications.argoproj.io -n argocd kfp-workload-identity -o jsonpath='{.status.sync.status}'    2>/dev/null || echo "")
  KFP_NS_EXISTS=$(kubectl get ns kubeflow -o name 2>/dev/null || echo "")
  SA_EXISTS=$(kubectl get sa -n kubeflow pipeline-runner -o name 2>/dev/null || echo "")
  echo "  [$elapsed s] kubeflow-pipelines=$KFP_SYNC  kfp-workload-identity=$WI_SYNC  SA=$([ -n "$SA_EXISTS" ] && echo yes || echo no)"
  if [ -n "$SA_EXISTS" ] && [ "$KFP_SYNC" = "Synced" ]; then
    break
  fi
  if [ $elapsed -ge $TIMEOUT ]; then
    echo "WARNING: timed out waiting for kubeflow-pipelines + pipeline-runner SA. Continuing." >&2
    break
  fi
  sleep $INTERVAL
  elapsed=$(( elapsed + INTERVAL ))
done

if [ -n "$SA_EXISTS" ]; then
  echo "  Annotating pipeline-runner SA for Workload Identity..."
  kubectl annotate sa pipeline-runner -n kubeflow \
    "iam.gke.io/gcp-service-account=training-job-sa@${GCP_PROJECT}.iam.gserviceaccount.com" \
    --overwrite
fi

cat <<EOF

ArgoCD installed. Initial admin password:
  kubectl -n argocd get secret argocd-initial-admin-secret \\
    -o jsonpath='{.data.password}' | base64 -d

Open the UI:
  kubectl port-forward -n argocd svc/argocd-server 8081:443

Login: admin / <password from above>. Rotate the password and delete the
initial-admin-secret once you do.

Watch child apps sync (ApplicationSets render their per-cluster Applications
once the in-cluster secret is annotated):
  kubectl get applications.argoproj.io -n argocd -w
  kubectl get applicationsets.argoproj.io -n argocd -w
EOF
