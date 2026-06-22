# Phase 4 — Architecture & Design
## Multi-Region HA, Platform Engineering, FinOps, Zero-Trust Security

> Architecture is the set of decisions that are hardest to change later. This phase teaches you to make them deliberately, with full awareness of tradeoffs.

---

## Learning objectives

- Design a multi-region platform from first principles, with justified tradeoffs
- Define what an Internal Developer Platform is and design one that reduces cognitive load
- Reduce Kubernetes infrastructure cost by 30–50% using Karpenter, Spot, and rightsizing
- Architect a zero-trust security model from workload identity through to secrets management

**Estimated study time:** 4–5 days

---

## 1. Multi-region high availability

### 1.1 Why multi-region and what you're actually protecting against

Multi-region protects against events that affect an entire cloud region: natural disasters, major power failures, regional network outages, and — more commonly — regional service degradations (AWS us-east-1 has had multiple significant partial outages).

**What multi-region does NOT protect against:**
- Application bugs (a bad deploy hits all regions simultaneously)
- DNS failures (your domain's DNS is a single point of failure)
- Control plane issues (if you use a single global management cluster)
- Human error at scale (terraform apply in the wrong workspace)

**Cost of multi-region:** Roughly 2× infrastructure cost for active-passive, potentially 2× for active-active (depends on traffic distribution). Multi-region also multiplies operational complexity: you need automated failover, data synchronisation, observability across regions, and regular failover drills.

**The key question before going multi-region:** Have you made the most of a single region first? Multi-AZ deployment with proper Pod Disruption Budgets and anti-affinity rules gives you significant resilience at much lower cost and complexity.

### 1.2 RPO and RTO — the requirements that drive architecture

**RPO (Recovery Point Objective):** Maximum acceptable data loss. Answers: "How old can our data be after recovery?"

**RTO (Recovery Time Objective):** Maximum acceptable downtime. Answers: "How long can we be unavailable?"

```
RPO = 0:
  → Synchronous replication required
  → Every write must be confirmed on multiple regions before returning success
  → Adds round-trip latency to every write (typically 20–80ms cross-region)
  → Often not acceptable for interactive applications

RPO = 5 minutes:
  → Asynchronous replication with a 5-minute lag acceptable
  → Writes are fast (no cross-region blocking)
  → Maximum 5 minutes of data loss if primary fails
  → Used for most databases in multi-region setups

RPO = hours/days:
  → Backup-restore model
  → Cheapest option
  → Acceptable for batch workloads, non-critical data
```

```
RTO = seconds:
  → Automatic failover, pre-warmed secondary, DNS TTL already set to low value
  → Active-active or very warm active-passive
  → Highest cost

RTO = minutes (5–15):
  → Semi-automated failover: health checks detect failure, DNS update, application reconfigures
  → Warm standby

RTO = hours:
  → Manual failover runbook
  → Cold standby (scale from zero in secondary)
  → Lowest cost for multi-region
```

### 1.3 Active-passive architecture in detail

```
┌─────────────────────────────────────────────────────────────────┐
│                     Global Load Balancing                        │
│   Route53 health checks + latency routing / Global Accelerator  │
└───────────────────┬─────────────────────────────────────────────┘
                    │ All traffic (normal operation)
          ┌─────────▼──────────┐         ┌────────────────────────┐
          │  Primary Region     │         │  Secondary Region       │
          │  (e.g., eu-west-1)  │         │  (e.g., eu-central-1)  │
          │                     │ async   │                        │
          │  K8s cluster (full) │──repl──▶│  K8s cluster (warm)    │
          │  Database (primary) │         │  Database (replica)     │
          │  Full traffic load  │         │  Scaled to 50% capacity │
          └─────────────────────┘         └────────────────────────┘
```

**Database replication strategies:**

```
For PostgreSQL (RDS):
  - Synchronous streaming replication: secondary gets every WAL segment before commit ACK
  - Asynchronous streaming replication: faster, but lag possible
  - AWS RDS Multi-AZ: synchronous, same region (different AZ)
  - AWS RDS Read Replica in another region: asynchronous (RPO = lag, typically < 1 minute)
  - Promotion to primary: takes 1–5 minutes (RDS), can be automated

For applications that need global write capability:
  - CockroachDB: Raft-based, global writes, configurable consistency
  - PlanetScale (MySQL): Vitess-based, global sharding
  - DynamoDB Global Tables: multi-region active-active, eventual consistency
  - Cassandra: tunable consistency, native multi-region support
```

**Failover DNS strategy:**

```bash
# AWS Route53 health check failover
# Primary record: ALIAS to primary region ALB, failover=PRIMARY
# Secondary record: ALIAS to secondary region ALB, failover=SECONDARY

# Critical: pre-lower DNS TTL BEFORE you need to fail over
# Default TTL might be 300s → change to 30s 24 hours before any planned work
# You can't lower TTL at failover time — old TTL is already cached

aws route53 change-resource-record-sets --hosted-zone-id Z1234 --change-batch '{
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "api.example.com",
      "Type": "A",
      "TTL": 30,
      "ResourceRecords": [{"Value": "<secondary-lb-ip>"}]
    }
  }]
}'

# Verify propagation
dig +short api.example.com @8.8.8.8
```

### 1.4 Active-active architecture

Active-active requires solving conflict resolution: two regions can both write, so what happens when they write to the same record simultaneously?

**Approaches:**

1. **Geo-routing + data partitioning:** Route users to the region that "owns" their data. European users → EU region (their data lives there). APAC users → APAC region. No conflict because data is sharded by user/geography.

2. **Last-write-wins (LWW):** Each write carries a timestamp (logical clock). During convergence, the highest timestamp wins. Simple, but can silently drop writes.

3. **CRDTs (Conflict-free Replicated Data Types):** Data structures that can be merged without conflicts. Counters, sets, registers with specific merge semantics. Limited applicability but correct.

4. **Application-level conflict resolution:** Application knows domain semantics. E.g., for a bank: debits always win; concurrent credits are both applied (additive operations commute, so no conflict).

```yaml
# DynamoDB Global Tables (active-active, eventually consistent)
aws dynamodb create-global-table \
  --global-table-name orders \
  --replication-group '[{"RegionName":"eu-west-1"},{"RegionName":"eu-central-1"}]'

# CockroachDB: true serialisable, multi-region
CREATE DATABASE orders PRIMARY REGION "eu-west" REGIONS "eu-west", "eu-central";
ALTER TABLE orders ADD COLUMN region crdb_internal_region AS (gateway_region()) STORED;
ALTER TABLE orders SET LOCALITY REGIONAL BY ROW;
# Writes to eu-west rows go to eu-west nodes; eu-central rows go to eu-central
# Cross-region writes still work but are slower (consensus across regions)
```

### 1.5 Kubernetes multi-cluster patterns

**Independent clusters + ApplicationSet (GitOps pattern):**

```yaml
# ArgoCD ApplicationSet: deploy to all production clusters
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: myapp
  namespace: argocd
spec:
  generators:
  - clusters:
      selector:
        matchLabels:
          environment: production
  template:
    metadata:
      name: "{{name}}-myapp"
    spec:
      project: production
      source:
        repoURL: https://github.com/org/helm-charts
        targetRevision: HEAD
        path: charts/myapp
        helm:
          valueFiles:
          - "values/{{metadata.labels.region}}.yaml"   # Per-region overrides
      destination:
        server: "{{server}}"
        namespace: production
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

**Cluster API (cluster lifecycle management):**

```yaml
# Define a cluster as a Kubernetes object (infrastructure as code)
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: prod-eu-central-1
spec:
  clusterNetwork:
    pods: {cidrBlocks: ["10.128.0.0/14"]}
    services: {cidrBlocks: ["10.96.0.0/12"]}
  infrastructureRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
    kind: AWSCluster
    name: prod-eu-central-1
  controlPlaneRef:
    apiVersion: controlplane.cluster.x-k8s.io/v1beta1
    kind: KubeadmControlPlane
    name: prod-eu-central-1
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSCluster
metadata:
  name: prod-eu-central-1
spec:
  region: eu-central-1
  sshKeyName: prod-keypair
  network:
    vpc: {cidrBlock: "10.20.0.0/16"}
```

---

## 2. Platform engineering

### 2.1 What platform engineering is — and what it isn't

**It is NOT:** Renaming your DevOps or infrastructure team, building a portal for the sake of it, or forcing a specific toolset on development teams.

**It IS:** Treating your internal customers (development teams) as product users, building self-service capabilities that reduce cognitive load, and measuring success by developer productivity metrics (DORA), not by uptime.

**The cognitive load problem:**

A developer deploying a service in a typical organisation needs to understand:
Kubernetes deployment manifests, Helm charts, ArgoCD sync status, Terraform for cloud resources, Prometheus metric instrumentation, Alertmanager routing, certificate management, secret management, NetworkPolicy, RBAC, cost attribution, and so on.

A platform team's job is to abstract this into: "write a service, define what it needs, push to Git."

### 2.2 Internal Developer Platform (IDP) components

```
┌─────────────────────────────────────────────────────┐
│              Developer Portal (Backstage)             │
│    Service catalog | TechDocs | Scaffolder templates │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│                  Golden Paths                         │
│    Git repo template | CI/CD pipeline | Deployment   │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│              Self-Service Infrastructure             │
│   Namespace provisioning | Secret management        │
│   Database provisioning | Monitoring setup           │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│           Platform Primitives                         │
│    Kubernetes | Vault | Prometheus | Istio | CI/CD   │
└─────────────────────────────────────────────────────┘
```

### 2.3 Golden paths in practice

A golden path is not a mandate — it's a heavily worn trail through the forest. Teams can go off-path but then own the maintenance themselves.

**Anatomy of a golden path for a new service:**

```bash
# Developer runs a scaffolder template in Backstage
# Result: a new Git repo with:

├── src/                      # Application code
├── Dockerfile                # Standard multi-stage build
├── helm/
│   ├── Chart.yaml
│   ├── values.yaml           # Defaults (cpu, memory, replicas)
│   └── values/
│       ├── staging.yaml
│       └── production.yaml
├── .github/workflows/
│   └── ci.yaml               # Test → build → push → ArgoCD sync
├── catalog-info.yaml         # Backstage catalog entity
├── monitoring/
│   └── alerts.yaml           # Pre-built alert rules (SLO burning rate)
└── README.md                 # Auto-generated docs from TechDocs
```

**Platform-provided defaults (zero developer configuration):**

- `PodDisruptionBudget` with `minAvailable: 1` — created automatically per Deployment
- `NetworkPolicy` — default deny-all + allow from ingress controller
- `HorizontalPodAutoscaler` — default scaling on CPU 60%
- `ServiceMonitor` — Prometheus scraping configured automatically from annotations
- `ResourceQuota` per namespace — prevents runaway resource consumption
- `LimitRange` — sets default CPU/memory requests for containers that don't specify

### 2.4 Backstage in depth

```yaml
# Backstage software catalog entity — lives in every service repo
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: payment-service
  description: Processes payment transactions via Stripe and PayPal
  annotations:
    github.com/project-slug: "org/payment-service"
    backstage.io/techdocs-ref: dir:.
    prometheus.io/alert-dashboard: "payment-service-slo"
    argocd/app-name: "payment-service"
  tags: [golang, grpc, payments, pci-dss]
  links:
  - url: https://grafana.internal/d/payment
    title: Grafana dashboard
    icon: dashboard
  - url: https://argocd.internal/applications/payment-service
    title: ArgoCD
    icon: web
spec:
  type: service
  lifecycle: production
  owner: group:payments-team
  system: payment-platform
  dependsOn:
  - component:stripe-integration
  - resource:payments-postgres
  - component:notification-service
  providesApis:
  - payment-api-v2
  consumesApis:
  - fraud-detection-api
```

```yaml
# Backstage scaffolder template for new Go services
apiVersion: scaffolder.backstage.io/v1beta3
kind: Template
metadata:
  name: golang-service
  title: New Go Service
spec:
  owner: platform-team
  type: service
  parameters:
  - title: Service info
    properties:
      name:
        type: string
        title: Service name
        pattern: '^[a-z][a-z0-9-]*$'
      owner:
        type: string
        title: Owner team
      description:
        type: string
  steps:
  - id: fetch
    name: Fetch template
    action: fetch:template
    input:
      url: ./template
      values:
        name: ${{ parameters.name }}
        owner: ${{ parameters.owner }}
  - id: publish
    name: Create GitHub repo
    action: publish:github
    input:
      repoUrl: github.com?repo=${{ parameters.name }}&owner=org
  - id: register
    name: Register in catalog
    action: catalog:register
    input:
      repoContentsUrl: ${{ steps.publish.output.repoContentsUrl }}
      catalogInfoPath: /catalog-info.yaml
```

### 2.5 Measuring platform success

**DORA metrics (DevOps Research and Assessment):**

| Metric | Elite | High | Medium | Low |
|--------|-------|------|--------|-----|
| Deployment frequency | Multiple/day | Weekly | Monthly | < Monthly |
| Lead time for changes | < 1 hour | 1 day | 1 week | > 1 month |
| Change failure rate | < 5% | 10-15% | 16-30% | > 30% |
| Time to restore service | < 1 hour | < 1 day | < 1 week | > 1 week |

**Platform-specific metrics:**

```
Adoption rate: % of services on the golden path vs custom setups
Toil reduction: hours/week saved on manual operational tasks (measure before and after)
Self-service ratio: % of infrastructure requests handled without platform team involvement
Deployment wait time: time from merge to deployed in production
Onboarding time: days for a new developer to deploy their first change
```

---

## 3. FinOps and cost optimisation

### 3.1 Where Kubernetes costs come from

**Compute (typically 60–80% of bill):**
- Node instances (EC2, GCE, Azure VMs)
- Over-provisioned resources (requested but not used)
- Underutilised nodes (node is running but pods don't fill it)
- Wrong instance types (general-purpose when you need memory-optimised)

**Storage (10–20%):**
- Persistent volumes left behind after pod/namespace deletion
- Oversized PVCs
- Unoptimised storage class (io2 EBS where gp3 would suffice)

**Data transfer (5–15%):**
- Cross-AZ traffic (suprisingly expensive: $0.01/GB in AWS)
- Internet egress
- Inter-cluster traffic in multi-region setups

**Other (5–10%):**
- Load balancers (each Service of type LoadBalancer creates one)
- NAT gateways (per-AZ)
- Logging/monitoring storage

```bash
# Install OpenCost
helm repo add opencost https://opencost.github.io/opencost-helm-chart
helm install opencost opencost/opencost -n opencost --create-namespace

# Query costs by namespace
curl "http://opencost:9003/allocation?window=7d&aggregate=namespace" | jq .

# Top cost drivers
curl "http://opencost:9003/allocation?window=30d&aggregate=label:team" | jq \
  '.data[0] | to_entries | sort_by(.value.totalCost) | reverse | .[0:10]'

# Identify idle resources
curl "http://opencost:9003/allocation?window=7d&aggregate=pod&idle=true" | jq .

# Cross-AZ traffic costs (requires network logging)
kubectl get --raw "/api/v1/nodes/<node>/proxy/stats/summary" | jq '.pods[].network'
```

### 3.2 Karpenter — architecture and cost optimisation

**How Karpenter saves money vs Cluster Autoscaler:**

1. **Bin-packing:** Karpenter selects the exact instance size that fits the pending pods. CA provisions from pre-defined groups (which may be over-sized).

2. **Instance type diversity:** Karpenter can pick `c6i.2xlarge`, `c6a.2xlarge`, `c5.2xlarge`, or `m6i.xlarge` — whatever is cheapest at this moment. CA is tied to one instance type per node group.

3. **Spot consolidation:** Karpenter automatically replaces spot nodes with cheaper spot types as prices change.

4. **Consolidation:** Karpenter continuously tries to reschedule pods onto fewer nodes, terminating underutilised ones.

```yaml
# Karpenter NodePool — the full spec
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: default
spec:
  template:
    metadata:
      labels:
        managed-by: karpenter
    spec:
      nodeClassRef:
        apiVersion: karpenter.k8s.aws/v1
        kind: EC2NodeClass
        name: default
      requirements:
      - key: kubernetes.io/arch
        operator: In
        values: ["amd64", "arm64"]       # arm64 = Graviton = 20% cheaper
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["spot", "on-demand"]    # Spot first, on-demand fallback
      - key: karpenter.k8s.aws/instance-category
        operator: In
        values: ["c", "m", "r"]         # Compute, memory, general
      - key: karpenter.k8s.aws/instance-generation
        operator: Gt
        values: ["5"]                    # Only modern instances
      taints:
      - key: node.kubernetes.io/not-ready
        effect: NoSchedule
  limits:
    cpu: "1000"
    memory: 4000Gi
  disruption:
    consolidationPolicy: WhenUnderutilized
    consolidateAfter: 1m
    budgets:
    - nodes: "20%"                       # Max 20% disrupted simultaneously
    - nodes: "0"
      schedule: "0 0 * * 1-5"
      duration: 8h                       # No disruption during business hours Mon-Fri
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023
  role: "KarpenterNodeRole-<cluster>"
  subnetSelectorTerms:
  - tags:
      karpenter.sh/discovery: "<cluster>"
  securityGroupSelectorTerms:
  - tags:
      karpenter.sh/discovery: "<cluster>"
  blockDeviceMappings:
  - deviceName: /dev/xvda
    ebs:
      volumeSize: 50Gi
      volumeType: gp3
      throughput: 125
      iops: 3000
      deleteOnTermination: true
```

**Spot instance interruption handling:**

Spot instances get a 2-minute warning (as a node label and via the EC2 Instance Metadata Service). Karpenter watches for this and cordons + drains the node, allowing pods to reschedule.

Your application must:
1. Handle SIGTERM gracefully (drain connections, finish in-flight requests)
2. Complete shutdown within `terminationGracePeriodSeconds` (default 30s)
3. Have enough replicas that losing one doesn't violate a PodDisruptionBudget

```yaml
# Ensure PDB is set so node drains don't cause outages
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-pdb
spec:
  selector:
    matchLabels:
      app: api
  minAvailable: "50%"   # Always keep 50% of pods running during disruptions
```

### 3.3 Rightsizing — the biggest win

Over-provisioned resource requests are the largest source of waste in most Kubernetes clusters. Developers tend to set requests conservatively (or copy from templates) and never revisit them.

**Goldilocks (automated rightsizing recommendations):**

```bash
# Install Goldilocks (uses VPA in recommendation mode, no auto-apply)
helm repo add fairwinds-stable https://charts.fairwinds.com/stable
helm install goldilocks fairwinds-stable/goldilocks -n goldilocks --create-namespace

# Enable for a namespace
kubectl label namespace production goldilocks.fairwinds.com/enabled=true

# After 24h of data:
kubectl port-forward svc/goldilocks-dashboard -n goldilocks 8080:80
# Browse to localhost:8080 — shows recommended vs current requests per container
```

**Manual rightsizing PromQL:**

```promql
# Containers where CPU request is > 3× CPU usage (over-provisioned)
(
  sum by(namespace, pod, container) (
    kube_pod_container_resource_requests{resource="cpu"}
  )
) / (
  sum by(namespace, pod, container) (
    rate(container_cpu_usage_seconds_total[24h])
  )
) > 3

# Memory request vs actual peak (over 7 days)
(
  sum by(namespace, pod, container) (
    kube_pod_container_resource_requests{resource="memory"}
  )
) / (
  sum by(namespace, pod, container) (
    max_over_time(container_memory_working_set_bytes[7d])
  )
) > 2

# Containers with zero CPU usage (unused workloads)
sum by(namespace, pod, container) (
  rate(container_cpu_usage_seconds_total[24h])
) == 0
```

### 3.4 Reserved Instances and Savings Plans

```
Cost hierarchy (highest to lowest):
  On-demand: full price, maximum flexibility
  Spot: 60-90% discount, can be interrupted with 2-min notice
  Compute Savings Plan: 40-66% discount, 1 or 3 year commitment, any EC2 in any region
  EC2 Instance RI: 40-72% discount, 1 or 3 year, specific instance family/region/OS

Strategy for most Kubernetes workloads:
  Baseline (always-on): Compute Savings Plan (flexible, covers any instance type)
  Variable load above baseline: On-demand (no commitment)
  Batch/stateless burst: Spot instances
  Never: 100% on-demand for production at scale
```

```bash
# Check Savings Plan coverage
aws ce get-savings-plans-coverage \
  --time-period Start=2024-06-01,End=2024-06-30 \
  --query 'SavingsPlansCoverages[*].{Resource:Attributes.instanceType,Coverage:Coverage.CoverageHours.CoverageHoursPercentage}'

# See what's not covered (pure on-demand)
aws ce get-reservation-coverage \
  --time-period Start=2024-06-01,End=2024-06-30 \
  --group-by Type=DIMENSION,Key=INSTANCE_TYPE
```

---

## 4. Zero-trust security

### 4.1 Zero-trust principles

**Traditional perimeter security:** "Trust everything inside the network. Block everything outside." One compromised internal host → attacker moves laterally freely.

**Zero-trust:** "Never trust, always verify — regardless of network location."

The three pillars:
1. **Verify explicitly:** Authenticate and authorise every connection, every time. No implicit trust from network location.
2. **Least privilege:** Each workload/user gets exactly what it needs — no more.
3. **Assume breach:** Design as if attackers are already inside. Limit blast radius.

**Applied to Kubernetes:**

```
Without zero-trust (default Kubernetes):
  - Any pod can call any other pod on any port (no NetworkPolicy)
  - Pods use long-lived static secrets in Kubernetes Secrets (base64 encoded, not encrypted)
  - Service-to-service connections are unencrypted (HTTP, not HTTPS)
  - RBAC is often overly permissive (cluster-admin for convenience)

With zero-trust:
  - Default-deny NetworkPolicy; explicit allow-list
  - Workload identity (SPIFFE/SPIRE): each pod has a cryptographic identity
  - mTLS: all service-to-service connections mutually authenticated + encrypted
  - Dynamic secrets: credentials generated on-demand, short TTL, automatically rotated
```

### 4.2 SPIFFE/SPIRE — workload identity

**SPIFFE (Secure Production Identity Framework for Everyone):** A specification for workload identity. Defines:
- **SPIFFE ID:** A URI identifying a workload: `spiffe://trust-domain/path/to/workload`
- **SVID (SPIFFE Verifiable Identity Document):** An X.509 certificate or JWT that proves a SPIFFE ID

**SPIRE (SPIFFE Runtime Environment):** The reference implementation.

**Architecture:**

```
SPIRE Server (cluster-wide, HA deployment)
  ├── Issues SVIDs (X.509 certs) to workloads
  ├── Maintains entry registry (which workloads get which SPIFFE IDs)
  └── Stores trust bundles (root CAs)

SPIRE Agent (DaemonSet, one per node)
  ├── Attests the node to the server (k8s_psat attestor uses projected service account token)
  ├── Receives SVIDs from server
  └── Exposes SPIFFE Workload API on a unix socket (/run/spire/sockets/agent.sock)

Workload
  └── Calls Workload API → receives X.509 SVID (certificate + private key)
  └── Uses SVID for mTLS with other workloads
```

```bash
# Install SPIRE in Kubernetes
kubectl apply -f https://raw.githubusercontent.com/spiffe/spire/main/examples/k8s/simple_psat/spire-namespace.yaml
kubectl apply -f spire-server-configmap.yaml
kubectl apply -f spire-server-statefulset.yaml
kubectl apply -f spire-agent-configmap.yaml
kubectl apply -f spire-agent-daemonset.yaml

# Register workload entries
kubectl exec -n spire spire-server-0 -- \
  /opt/spire/bin/spire-server entry create \
  -spiffeID spiffe://example.org/ns/production/sa/api-service \
  -parentID spiffe://example.org/k8s-psat/my-cluster/node \
  -selector k8s:ns:production \
  -selector k8s:sa:api-service

# Verify a workload can get an SVID
kubectl exec -n production api-pod -- \
  /opt/spire/bin/spire-agent api fetch x509 \
  -socketPath /run/spire/sockets/agent.sock

# The SVID contains:
# Subject: spiffe://example.org/ns/production/sa/api-service
# Valid for: 1 hour (auto-rotated before expiry)
# Can be used for mTLS (Istio, Linkerd, or custom TLS config)
```

### 4.3 Service mesh options for mTLS

**Istio:**

```
Architecture: sidecar proxy (Envoy) injected into every pod
              Istiod (control plane): manages certs, xDS config

mTLS mode:
  PERMISSIVE: accept both mTLS and plain HTTP (migration mode)
  STRICT: only accept mTLS

Traffic interception:
  iptables rules redirect all pod traffic through Envoy sidecar
  Envoy handles mTLS termination/origination transparently
  Application code doesn't know about TLS
```

```yaml
# Enable strict mTLS cluster-wide
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT

# AuthorizationPolicy: only payment-service can call the orders database
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: orders-db-policy
  namespace: production
spec:
  selector:
    matchLabels:
      app: orders-db
  rules:
  - from:
    - source:
        principals: ["cluster.local/ns/production/sa/payment-service"]
    to:
    - operation:
        ports: ["5432"]
```

**Cilium Service Mesh (eBPF-based, no sidecar):**

```bash
# Enable Cilium mTLS (uses SPIFFE/SPIRE or Cilium's built-in CA)
cilium install --set encryption.enabled=true --set encryption.type=wireguard

# Or with SPIRE integration
cilium install \
  --set authentication.mutual.spire.enabled=true \
  --set authentication.mutual.spire.install.enabled=true

# Verify mTLS is working
cilium monitor --type drop | grep -i auth
cilium connectivity test
```

### 4.4 Vault for secrets management

**Why Kubernetes Secrets are insufficient:**

Kubernetes Secrets are base64-encoded (not encrypted) by default in etcd. Anyone with `kubectl get secret` permissions and access to etcd can read them. They are static — if a secret leaks, you must manually rotate every service that uses it.

**Vault dynamic secrets — the key concept:**

Instead of storing `DB_PASSWORD=mypassword`, Vault generates a new, unique, short-lived credential on demand:

```
Service starts → Requests credential from Vault
Vault → Creates a new PostgreSQL role with a password
       Returns: username=v-role-xyz123, password=A1b-random, TTL=1h
Service uses credential for 1 hour
At 55 min → Vault agent renews the lease (re-issues)
At end of service lifetime → Vault agent revokes the credential
Result: credential that only existed while the service was running
```

```bash
# Set up Vault with Kubernetes auth
vault auth enable kubernetes

vault write auth/kubernetes/config \
  kubernetes_host="https://kubernetes.default.svc" \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token

# Create a policy for the api-service
vault policy write api-service - <<EOF
path "database/creds/api-service" {
  capabilities = ["read"]
}
path "secret/data/production/api/*" {
  capabilities = ["read"]
}
EOF

# Bind the policy to a Kubernetes service account
vault write auth/kubernetes/role/api-service \
  bound_service_account_names=api-service \
  bound_service_account_namespaces=production \
  policies=api-service \
  ttl=1h

# Enable database secrets engine
vault secrets enable database

vault write database/config/production-postgres \
  plugin_name=postgresql-database-plugin \
  allowed_roles="api-service" \
  connection_url="postgresql://{{username}}:{{password}}@postgres.production:5432/apidb?sslmode=require" \
  username="vault-root" \
  password="<root-password>"

vault write database/roles/api-service \
  db_name=production-postgres \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
  default_ttl="1h" \
  max_ttl="24h"

# Test credential generation
vault read database/creds/api-service
```

**External Secrets Operator (simpler integration):**

```yaml
# ClusterSecretStore: configure the Vault backend once
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "https://vault.internal:8200"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "external-secrets"
---
# ExternalSecret: sync specific secrets into Kubernetes
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: api-service-secrets
  namespace: production
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: api-service-secrets
    creationPolicy: Owner
    template:
      data:
        config.yaml: |
          database:
            url: "postgres://{{ .username }}:{{ .password }}@postgres:5432/apidb"
          redis:
            password: "{{ .redis_password }}"
  data:
  - secretKey: username
    remoteRef:
      key: production/api-service/database
      property: username
  - secretKey: password
    remoteRef:
      key: production/api-service/database
      property: password
  - secretKey: redis_password
    remoteRef:
      key: production/api-service/redis
      property: password
```

---

## Design tradeoffs summary

| Decision | Option A | Option B | Choose A when | Choose B when |
|----------|----------|----------|--------------|--------------|
| Multi-region | Active-passive | Active-active | RTO > 5min, lower budget | RTO < 2min, global user base, budget allows |
| Service mesh | Istio (sidecar) | Cilium (eBPF) | Need rich L7 traffic management | Already on Cilium CNI, minimise overhead |
| Secrets | ESO + Vault | Vault Agent | Simple secret sync is enough | Need auto-renewal of dynamic credentials |
| Node scaling | Cluster Autoscaler | Karpenter | Need simple, stable ASG management | Need optimal cost, fast scale-up, consolidation |
| Platform UX | Backstage | Custom portal | Ecosystem integration matters | You have unique requirements backstage doesn't meet |

---

## Hands-on exercises

1. Design (on paper) a multi-region active-passive architecture for a real application. Specify: RPO, RTO, database replication strategy, DNS failover approach, runbook, and estimated cost premium vs single region.
2. Create a Backstage `catalog-info.yaml` for a real service, including dependencies, APIs provided, and links to dashboards. Run Backstage locally and verify the entity appears in the catalog.
3. Install OpenCost in a test cluster. Identify the top 3 cost drivers. Calculate how much would be saved by rightsizing the top 5 over-provisioned deployments.
4. Set up Vault with the Kubernetes auth method. Configure a database secrets engine. Write an ExternalSecret that pulls a dynamic credential into a Kubernetes Secret. Verify the credential rotates on schedule.
5. Write an ApplicationSet that deploys to multiple clusters using a generator. Verify it deploys correctly to 2+ clusters simultaneously.

---

## What to study next → [Phase 5 — Interview Prep](./phase5-interview-prep.md)

Phase 5 converts everything you've learned in Phases 1–4 into interview performance: system design under time pressure, behavioral questions with STAR format, and a Kubernetes knowledge quiz at senior/staff level difficulty.
