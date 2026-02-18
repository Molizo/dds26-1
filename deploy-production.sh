#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

require_command kubectl
require_command helm

NAMESPACE="default"
IMAGE_TAG="${IMAGE_TAG:-24h}"
ORDER_IMAGE="${ORDER_IMAGE:-ttl.sh/dds26-order:${IMAGE_TAG}}"
STOCK_IMAGE="${STOCK_IMAGE:-ttl.sh/dds26-stock:${IMAGE_TAG}}"
USER_IMAGE="${USER_IMAGE:-ttl.sh/dds26-user:${IMAGE_TAG}}"

wait_for_crd() {
  local crd_name="$1"
  local timeout="${2:-180s}"
  local poll_interval_sec=3
  local elapsed=0
  local timeout_sec="${timeout%s}"

  while ! kubectl get crd "${crd_name}" >/dev/null 2>&1; do
    if [[ "${elapsed}" -ge "${timeout_sec}" ]]; then
      echo "Timed out waiting for CRD to exist: ${crd_name}" >&2
      exit 1
    fi
    sleep "${poll_interval_sec}"
    elapsed=$((elapsed + poll_interval_sec))
  done

  kubectl wait --for=condition=Established "crd/${crd_name}" --timeout="${timeout}" >/dev/null
}

helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null
helm repo add kedacore https://kedacore.github.io/charts >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server >/dev/null
helm repo update >/dev/null

kubectl create secret generic rabbitmq-load-definition \
  --namespace "${NAMESPACE}" \
  --from-file=load_definition.json="${ROOT_DIR}/helm-config/rabbitmq-definitions.json" \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install -f "${ROOT_DIR}/helm-config/redis-helm-values.yaml" redis bitnami/redis --namespace "${NAMESPACE}"
helm upgrade --install -f "${ROOT_DIR}/helm-config/rabbitmq-helm-values.yaml" rabbitmq bitnami/rabbitmq --namespace "${NAMESPACE}"
helm upgrade --install -f "${ROOT_DIR}/helm-config/nginx-helm-values.yaml" nginx ingress-nginx/ingress-nginx --namespace "${NAMESPACE}"
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace
helm upgrade --install -f "${ROOT_DIR}/helm-config/metrics-server-helm-values.yaml" metrics-server metrics-server/metrics-server --namespace kube-system

# Ensure ingress controller is ready and ingress routes are applied explicitly.
kubectl -n "${NAMESPACE}" rollout status deployment/nginx-ingress-nginx-controller --timeout=300s
kubectl apply -n "${NAMESPACE}" -f "${ROOT_DIR}/k8s/ingress-service.yaml"

# Fresh clusters can take time to register KEDA CRDs; wait before applying ScaledObjects.
wait_for_crd "scaledobjects.keda.sh" "180s"
wait_for_crd "triggerauthentications.keda.sh" "180s"

kubectl kustomize --load-restrictor LoadRestrictionsNone "${ROOT_DIR}/k8s/overlays/linode" | kubectl apply -n "${NAMESPACE}" -f -

kubectl -n "${NAMESPACE}" set image deployment/order-deployment order="${ORDER_IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/stock-deployment stock="${STOCK_IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/user-deployment user="${USER_IMAGE}"

kubectl -n "${NAMESPACE}" set image deployment/order-worker-deployment order-worker="${ORDER_IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/stock-worker-deployment stock-worker="${STOCK_IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/payment-worker-deployment payment-worker="${USER_IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/order-reconciliation-worker-deployment order-reconciliation-worker="${ORDER_IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/dlq-replay-worker-deployment dlq-replay-worker="${ORDER_IMAGE}"

deployments=(
  order-deployment
  stock-deployment
  user-deployment
  order-worker-deployment
  stock-worker-deployment
  payment-worker-deployment
  order-reconciliation-worker-deployment
  dlq-replay-worker-deployment
)

for deployment in "${deployments[@]}"; do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=300s
done

echo "Deployment complete."
