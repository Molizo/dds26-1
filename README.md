# Distributed Data Systems Project Template

Basic project structure with Python's Flask and Redis. 
**You are free to use any web framework in any language and any database you like for this project.**

### Project structure

* `env`
    Folder containing the Redis env variables for the docker-compose deployment
    
* `helm-config` 
   Helm chart values for Redis and ingress-nginx
        
* `k8s`
    Folder containing the kubernetes deployments, apps and services for the ingress, order, payment and stock services.
    
* `order`
    Folder containing the order application logic and dockerfile. 
    
* `payment`
    Folder containing the payment application logic and dockerfile. 

* `stock`
    Folder containing the stock application logic and dockerfile. 

* `test`
    Folder containing some basic correctness tests for the entire system. (Feel free to enhance them)

### Deployment types:

#### docker-compose (local development)

After coding the REST endpoint logic run `docker-compose up --build` in the base folder to test if your logic is correct
(you can use the provided tests in the `\test` folder and change them as you wish). 

***Requirements:*** You need to have docker and docker-compose installed on your machine. 

K8s is also possible, but we do not require it as part of your submission. 

#### minikube (local k8s cluster)

This setup is for local k8s testing to see if your k8s config works before deploying to the cloud. 
First deploy your database using helm by running the `deploy-charts-minicube.sh` file (in this example the DB is Redis 
but you can find any database you want in https://artifacthub.io/ and adapt the script). Then adapt the k8s configuration files in the
`\k8s` folder to mach your system and then run `kubectl apply -f .` in the k8s folder. 

***Requirements:*** You need to have minikube (with ingress enabled) and helm installed on your machine.

#### kubernetes cluster (managed k8s cluster in the cloud)

Similarly to the `minikube` deployment but run the `deploy-charts-cluster.sh` in the helm step to also install an ingress to the cluster. 

***Requirements:*** You need to have access to kubectl of a k8s cluster.

#### Linode LKE with Terraform (easy create/teardown)

This project now includes a Linode-focused Terraform stack under `terraform/linode` plus helper scripts under `scripts/` for:
- image build/push to `ttl.sh` (`:24h` tags),
- LKE creation,
- Helm dependency installation,
- app deployment rollout,
- full destroy with local `kubectl` context restore.

One-time setup:
1. Copy `terraform/linode/terraform.tfvars.example` to `terraform/linode/terraform.tfvars` and adjust non-secret defaults.

Required environment variables:
- `LINODE_TOKEN`
- Optional: `IMAGE_TAG` (defaults to `24h`)
- Optional image overrides:
  - `ORDER_IMAGE`
  - `STOCK_IMAGE`
  - `USER_IMAGE`

Bash workflow:
```bash
bash scripts/linode-up.sh
bash scripts/linode-down.sh
```

Notes:
- `linode-up` saves your previous `kubectl` context and switches to `dds26-linode` by default.
- `linode-down` destroys cloud resources and restores the previous local context.
- Set `CLEAN_LINODE_CONTEXT=true` if you also want teardown scripts to delete the Linode context alias.
- Linode deploy flow installs `metrics-server` so CPU-based HPA metrics are available (`kubectl top`, `cpu: x%/70%`).

### Phase 3 migration note (2026-02-16)

- Stock and payment persistence migrated from msgpack blobs (`<id>`) to Redis hashes:
  - `stock:<item_id>`
  - `payment:<user_id>`
- During migration from older runtime states, existing stock/payment data is not read-compatible.
- If upgrading an existing environment, clear stock/payment Redis data and re-seed via the existing create/batch-init endpoints before running tests.

### Phase 6 recovery and DLQ replay notes (2026-02-16)

- New workers:
  - `order/workers/reconciliation_worker.py`: leader-locked stale saga recovery scanner.
  - `order/workers/dlq_replay_worker.py`: automatic DLQ replay with bounded attempts.
- `dlq.parking.q` is a quarantine queue for:
  - messages with exhausted replay attempts,
  - messages with invalid/missing source queue metadata.
- New docker-compose services:
  - `order-reconciliation-worker`
  - `dlq-replay-worker`
- New Kubernetes manifests:
  - `k8s/order-reconciliation-worker.yaml`
  - `k8s/dlq-replay-worker.yaml`
- Key environment knobs:
  - Reconciliation: `RECOVERY_SCAN_INTERVAL_MS`, `RECOVERY_STALE_AFTER_MS`, `RECOVERY_BATCH_SIZE`, `RECOVERY_STEP_LOCK_TTL_SEC`, `RECOVERY_MAX_ACTIONS_PER_CYCLE`, `RECOVERY_LEADER_LOCK_TTL_MS`.
  - DLQ replay: `DLQ_REPLAY_QUEUES`, `DLQ_REPLAY_POLL_INTERVAL_MS`, `DLQ_REPLAY_RATE_PER_SEC`, `DLQ_REPLAY_MAX_ATTEMPTS`, `DLQ_REPLAY_ATTEMPT_TTL_SEC`, `DLQ_REPLAY_LEADER_LOCK_TTL_MS`.
