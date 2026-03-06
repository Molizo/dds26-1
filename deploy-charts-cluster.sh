#!/usr/bin/env bash
set -euo pipefail

helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/

helm repo update

helm upgrade --install -f helm-config/rabbitmq-helm-values.yaml rabbitmq bitnami/rabbitmq
helm upgrade --install -f helm-config/nginx-helm-values.yaml nginx ingress-nginx/ingress-nginx
helm upgrade --install metrics-server metrics-server/metrics-server
