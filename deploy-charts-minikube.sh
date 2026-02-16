#!/usr/bin/env bash

set -euo pipefail

if ! command -v helm >/dev/null 2>&1 || ! command -v kubectl >/dev/null 2>&1; then
  echo "helm and kubectl must be installed and available in this bash shell." >&2
  exit 1
fi

helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

kubectl create secret generic rabbitmq-load-definition \
  --from-file=load_definition.json=helm-config/rabbitmq-definitions.json \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install -f helm-config/redis-helm-values-minikube.yaml redis bitnami/redis
helm upgrade --install -f helm-config/rabbitmq-helm-values.yaml rabbitmq bitnami/rabbitmq
