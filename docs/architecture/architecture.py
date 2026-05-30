"""
BTC-MLOps cloud + cluster architecture map.

Renders a single PNG that shows:
- External actors (GitHub repo, GitHub Actions CI, /predict client).
- GKE cluster contents grouped by namespace (Argo CD, KFP, MLflow, serving, monitoring).
- GCP project resources outside the cluster (Artifact Registry, BigQuery, GCS, IAM SAs, VPC).
- Data, GitOps sync, image pull, and Workload Identity edges between them.

Run from project root:

    python3 docs/architecture/architecture.py

Output: docs/architecture/btc-mlops-architecture.png
"""

from pathlib import Path
import os

from diagrams import Cluster, Diagram, Edge
from diagrams.gcp.analytics import BigQuery
from diagrams.gcp.compute import GKE
from diagrams.gcp.devtools import ContainerRegistry
from diagrams.gcp.network import VPC
from diagrams.gcp.security import Iam
from diagrams.gcp.storage import GCS
from diagrams.k8s.compute import Deployment, Pod
from diagrams.k8s.network import Service
from diagrams.k8s.rbac import ServiceAccount
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import Client
from diagrams.onprem.gitops import ArgoCD
from diagrams.onprem.mlops import Mlflow
from diagrams.onprem.monitoring import Prometheus
from diagrams.onprem.vcs import Github

OUT_DIR = Path(__file__).resolve().parent
OUT_FILE = OUT_DIR / "btc-mlops-architecture"

SYNC     = Edge(color="#1f78b4", style="dashed", label="gitops sync",      constraint="false")
DEPLOY   = Edge(color="#1f78b4", style="dashed",                           constraint="false")
WI       = Edge(color="#33a02c", style="bold",   label="Workload Identity", constraint="false")
WI0      = Edge(color="#33a02c", style="bold",                              constraint="false")
DATA     = Edge(color="#e31a1c", constraint="false")
PULL_IMG = Edge(color="#6a3d9a", style="bold",   label="pulls image",       constraint="false")
PULL0    = Edge(color="#6a3d9a", style="bold",                              constraint="false")
HTTP     = Edge(color="#ff7f00", constraint="false")
SCRAPE   = Edge(style="dashed",  color="#999999", label="scrape",           constraint="false")
INVIS    = Edge(style="invis", weight="100", minlen="0")   # same-rank horizontal tie
SPINE    = Edge(style="invis", weight="30",  minlen="2")   # vertical row separation


def build() -> None:
    os.chdir(OUT_DIR)
    with Diagram(
        "BTC-MLOps — Cloud & Cluster Map",
        filename=OUT_FILE.name,
        outformat="png",
        show=False,
        direction="TB",
        graph_attr={
            "fontsize": "22",
            "labelloc": "t",
            "bgcolor": "white",
            "splines": "ortho",
            "pad": "0.8",
            "nodesep": "0.8",
            "ranksep": "1.4",
            "concentrate": "false",
            "compound": "true",
            "newrank": "true",
            "dpi": "120",
        },
    ):
        # =================================================================
        # Row 1 — External actors (top)
        # =================================================================
        with Cluster("External"):
            gh = Github("github.com\n(main)")
            gha = GithubActions("GH Actions CI\n(Phase 7)")
            client = Client("/predict client")

        # =================================================================
        # Row 2 — GCP managed resources (out-of-cluster, terraformed)
        # =================================================================
        with Cluster("GCP managed (out-of-cluster, terraformed)"):
            with Cluster("Artifact Registry"):
                ar = ContainerRegistry("btc-mlops repo")

            with Cluster("IAM SAs"):
                sa_train = Iam("training-job-sa")
                sa_serve = Iam("serving-sa")
                sa_ci = Iam("github-ci-sa")

            with Cluster("BigQuery"):
                bq_raw = BigQuery("btc_raw\ndataset_unified")
                bq_snap = BigQuery("btc_snapshots")

            with Cluster("GCS buckets"):
                gcs_data = GCS("data-features")
                gcs_models = GCS("models")
                gcs_mlflow = GCS("mlflow-artifacts")
                gcs_docs = GCS("docs")

            with Cluster("Network"):
                _vpc = VPC("VPC + subnets")

        # =================================================================
        # Row 3 — GKE cluster
        # =================================================================
        with Cluster("GKE cluster — genops-dev (europe-west3)"):
            with Cluster("ns: argocd"):
                argocd = ArgoCD("argocd-server")

            with Cluster("ns: kubeflow"):
                ksa_kfp = ServiceAccount("ksa: pipeline-runner")
                kfp_pod = Pod("KFP pipeline pod\ningest → features → train\n→ holdout → register")
                ksa_kfp - INVIS - kfp_pod

            with Cluster("ns: mlflow"):
                mlflow = Mlflow("mlflow\ntracking + registry\n(svc:5000)")
                mlflow_svc = mlflow  # alias — collapsed for layout

            with Cluster("ns: serving"):
                ksa_serve = ServiceAccount("ksa: btc-serving")
                serve_dep = Deployment("btc-serving\n(FastAPI)")
                serve_svc = Service("svc:8000")
                ksa_serve - INVIS - serve_dep
                serve_dep - INVIS - serve_svc

            with Cluster("ns: monitoring (Phase 8)"):
                prom = Prometheus("kube-prometheus")

        # =================================================================
        # Skeleton — invisible edges only. Every real edge is constraint=false,
        # so these alone control layout: 3 stacked rows, each row a single
        # horizontal rank of side-by-side boxes.
        # =================================================================
        # Row-internal horizontal alignment (same rank, left -> right order)
        gh - INVIS - gha - INVIS - client
        (
            sa_train - INVIS - sa_serve - INVIS - sa_ci
            - INVIS - ar
            - INVIS - bq_raw - INVIS - bq_snap
            - INVIS - gcs_data - INVIS - gcs_models - INVIS - gcs_mlflow - INVIS - gcs_docs
            - INVIS - _vpc
        )
        (
            argocd
            - INVIS - ksa_kfp - INVIS - kfp_pod
            - INVIS - ksa_serve - INVIS - serve_dep - INVIS - serve_svc
            - INVIS - mlflow
            - INVIS - prom
        )
        # Vertical row separation: External -> GCP -> GKE (central spine)
        gha    >> SPINE >> bq_raw
        bq_raw >> SPINE >> serve_dep

        # =================================================================
        # Edges
        # =================================================================
        gh >> SYNC >> argocd
        argocd >> DEPLOY >> kfp_pod
        argocd >> DEPLOY >> mlflow
        argocd >> DEPLOY >> serve_dep
        argocd >> DEPLOY >> prom

        gh >> Edge(style="dotted", color="#666666", constraint="false") >> gha
        gha >> Edge(color="#6a3d9a", style="bold", label="docker push", constraint="false") >> ar
        gha >> WI >> sa_ci

        ksa_kfp >> WI >> sa_train
        ksa_serve >> WI0 >> sa_serve

        ar >> PULL_IMG >> kfp_pod
        ar >> PULL0 >> serve_dep

        kfp_pod >> DATA >> bq_raw
        kfp_pod >> DATA >> bq_snap
        kfp_pod >> DATA >> gcs_data
        kfp_pod >> DATA >> gcs_models
        kfp_pod >> HTTP >> mlflow_svc
        mlflow >> DATA >> gcs_mlflow

        client >> HTTP >> serve_svc
        serve_dep >> HTTP >> mlflow_svc
        serve_dep >> DATA >> gcs_models
        serve_dep >> DATA >> bq_raw

        prom >> SCRAPE >> serve_dep
        prom >> SCRAPE >> mlflow

        kfp_pod >> Edge(style="dotted", color="#999999", label="eval html", constraint="false") >> gcs_docs


if __name__ == "__main__":
    build()
    print(f"Wrote {OUT_FILE.with_suffix('.png')}")
