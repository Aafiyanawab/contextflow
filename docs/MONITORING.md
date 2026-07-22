# ContextFlow Observability (Prometheus + Grafana)

A deliberately **lightweight, internal-only** monitoring stack sized for the
single-node **t3.small (2 GB)** k3s cluster. No Prometheus Operator, no
`kube-prometheus-stack` — those are too heavy for 2 GB alongside the app.

## Architecture

```
 namespace: monitoring  (ClusterIP only — NO ingress)
  ┌──────────────────────────┐   scrapes    ┌───────────────────────────┐
  │ Prometheus (24h retention)│─────────────►│ node/pod/container metrics │
  │  req 128Mi / limit 300Mi  │  via kubelet │  (kubelet resource+cadvisor│
  │  PVC 2Gi (local-path)     │  API proxy   │   through API-server proxy)│
  └──────────┬───────────────┘               └───────────────────────────┘
             │ scrapes /metrics (Bearer)
             ▼
  ┌──────────────────────────┐   datasource  ┌───────────────────────────┐
  │ ContextFlow web pod       │◄─────────────┤ Grafana                    │
  │ contextflow ns :80/metrics│              │  req 96Mi / limit 200Mi    │
  └──────────────────────────┘              │  PVC 1Gi · 3 dashboards     │
                                             └───────────────────────────┘
```

**Why no exporters (yet):** node + pod + container CPU/memory come directly
from the k3s **kubelet** (`/metrics/resource` and `/metrics/cadvisor`,
reached through the API-server proxy), so we need **no node-exporter and no
kube-state-metrics** for the core objectives. Those are added *only* if a
post-deploy memory re-check shows headroom (see "Optional exporters").

## Helm charts used

**None.** The stack is plain Kubernetes manifests (`monitoring/*.yaml`) so
resource use is fully under our control on the 2 GB box. (A future move to
`kube-prometheus-stack` would require a larger instance.)

## Components, ports & services

| Component  | Namespace  | Service (ClusterIP)        | Port | Exposure |
|------------|------------|----------------------------|------|----------|
| Prometheus | monitoring | `prometheus.monitoring`    | 9090 | internal |
| Grafana    | monitoring | `grafana.monitoring`       | 3000 | internal |
| App metrics| contextflow| `web.contextflow` `/metrics`| 80  | Bearer-gated |

Nothing here is on the Traefik ingress. Reach the UIs with port-forward:

```bash
kubectl -n monitoring port-forward svc/grafana 3000:3000     # http://localhost:3000
kubectl -n monitoring port-forward svc/prometheus 9090:9090  # http://localhost:9090
```

## Dashboards (provisioned from `monitoring/dashboards/`)

1. **Kubernetes & Node** — targets up, running pods, node CPU/mem, per-pod CPU/mem.
2. **Application** — requests/sec, error rate, latency p50/p95, status mix, tokens.
3. **AI, Cache & Inventory** — cache hit rate/size, repositories (indexed/total),
   knowledge index, documents/chunks, workspaces/companies/users, AI answers.

## Persistence

Both use `local-path` PVCs on the 30 GB root EBS volume: Prometheus `2Gi`
(`/prometheus` TSDB, 24h retention), Grafana `1Gi` (`/var/lib/grafana`
state). Data survives pod restarts; datasource and dashboards are
re-provisioned from ConfigMaps on every start.

## Secrets (created out-of-band, never committed)

```bash
# 1) Bearer token shared by the app (/metrics) and Prometheus.
TOKEN=$(openssl rand -hex 24)
kubectl -n monitoring create secret generic metrics-token \
  --from-literal=metrics-token="$TOKEN"
kubectl -n contextflow patch secret contextflow-secrets \
  -p "{\"stringData\":{\"METRICS_TOKEN\":\"$TOKEN\"}}"   # app reads it after next deploy

# 2) Grafana admin credentials.
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(openssl rand -hex 16)"
```

The app only enforces the token once the image carrying `/metrics` is
deployed; until then Prometheus' app target reads DOWN (node/pod metrics
still flow).

## Install / upgrade

```bash
kubectl apply -f monitoring/00-namespace.yaml
# create the two secrets (above), then:
kubectl apply -f monitoring/10-prometheus-rbac.yaml
kubectl apply -f monitoring/20-prometheus-config.yaml
kubectl apply -f monitoring/30-prometheus.yaml
kubectl apply -f monitoring/40-grafana.yaml
# dashboards ConfigMap from the JSON files:
kubectl -n monitoring create configmap grafana-dashboards \
  --from-file=monitoring/dashboards/ --dry-run=client -o yaml | kubectl apply -f -

# Reload Prometheus config without a restart:
kubectl -n monitoring exec deploy/prometheus -- \
  wget -qO- --post-data='' http://localhost:9090/-/reload
```

## Rollback

```bash
# Roll back a bad Deployment revision:
kubectl -n monitoring rollout undo deploy/prometheus
kubectl -n monitoring rollout undo deploy/grafana

# Remove the whole stack (PVCs deleted too — metrics history is disposable):
kubectl delete namespace monitoring
kubectl -n contextflow patch secret contextflow-secrets \
  --type=json -p='[{"op":"remove","path":"/data/METRICS_TOKEN"}]'
```

## Memory discipline (the t3.small constraint)

Baseline before install: **687Mi available** of 1910Mi. Budget: Prometheus
≤300Mi + Grafana ≤200Mi ≈ 500Mi, leaving ~180Mi headroom. **Re-check after
install** before adding anything:

```bash
free -m ; kubectl top pods -A --sort-by=memory
```

### Optional exporters (only if headroom remains)

- **node-exporter** (~25Mi, DaemonSet) — host disk/filesystem/network.
- **kube-state-metrics** (~60–100Mi) — pod restart counts, deployment status.

Add these one at a time and re-run `free -m` after each. If `available`
drops below ~120Mi, do **not** add them — the app (limit 1Gi) must keep its
headroom.
