# Linode overlay

This overlay keeps the existing base manifests and patches:
- image pull policy to `IfNotPresent`,
- deployment images to `ttl.sh` `:24h` tags.

Cloud rollout scripts apply this overlay and then enforce the same image references:
- `order`
- `stock`
- `user`

This keeps base manifests unchanged for local Minikube usage while enabling cloud pulls.
