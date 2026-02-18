#!/usr/bin/env bash

set -euo pipefail

if ! command -v minikube >/dev/null 2>&1 || ! command -v kubectl >/dev/null 2>&1 || ! command -v helm >/dev/null 2>&1; then
  echo "minikube, kubectl, and helm must be installed and available in this bash shell." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

minikube start
minikube addons enable ingress
minikube addons enable metrics-server
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

minikube image build -t order:latest -f order/Dockerfile .
minikube image build -t stock:latest -f stock/Dockerfile .
minikube image build -t user:latest -f payment/Dockerfile .

bash deploy-charts-minikube.sh
kubectl apply -f k8s/

deployments=(
  order-deployment
  stock-deployment
  user-deployment
  order-worker-deployment
  order-reconciliation-worker-deployment
  dlq-replay-worker-deployment
  stock-worker-deployment
  payment-worker-deployment
)

for deployment in "${deployments[@]}"; do
  kubectl rollout restart "deployment/${deployment}"
done

for deployment in "${deployments[@]}"; do
  kubectl rollout status "deployment/${deployment}" --timeout=180s
done

kubectl get pods
