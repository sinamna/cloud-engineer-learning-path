# Phase 2 — Kubernetes Mastery
## Scheduler, Operators, Networking, Security, Autoscaling, etcd

> Authoritative deep-dive into Kubernetes internals. Not a tutorial — a reference at the depth required for senior engineering roles and production debugging.

<div class="topic-legend">
<span><span class="swatch" style="background:#6aa6ff"></span>Core concept</span>
<span><span class="swatch" style="background:#e8b84e"></span>Interview hot topic</span>
<span><span class="swatch" style="background:#b18cff"></span>Architecture depth</span>
<span><span class="swatch" style="background:#e87a4e"></span>Gap to close</span>
<span><span class="swatch" style="background:#4ee8a0"></span>Hands-on practice</span>
</div>

<div class="topic-grid">
<a class="topic-card" href="#the-kubernetes-control-loop-everything-is-reconciliation">
<h4>Control loop &amp; reconciliation</h4>
<div class="tags"><span class="cat cat-core">Core concept</span></div>
</a>
<a class="topic-card" href="#the-kubernetes-scheduler">
<h4>Scheduler internals</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#operators-and-crds">
<h4>Operators &amp; CRDs</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-practice">Hands-on practice</span></div>
</a>
<a class="topic-card" href="#kubernetes-networking-in-depth">
<h4>K8s networking depth</h4>
<div class="tags"><span class="cat cat-gap">Gap to close</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#resource-management-and-autoscaling">
<h4>Resource &amp; autoscaling</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-practice">Hands-on practice</span></div>
</a>
<a class="topic-card" href="#etcd-internals">
<h4>etcd &amp; control plane</h4>
<div class="tags"><span class="cat cat-gap">Gap to close</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
</div>

---

## Learning objectives

- Trace a pod from `kubectl apply` through every control-plane component to a running container
- Write a production-grade Kubernetes controller using `controller-runtime`
- Debug any networking issue by reasoning from first principles
- Operate etcd confidently under load, partition, and recovery scenarios

**Estimated study time:** 4–5 days

---

## 1. The Kubernetes control loop — everything is reconciliation

Before any component: understand the pattern that all of Kubernetes follows.

```
Desired state (stored in etcd)
         ↓ watch
   Controller observes current state
         ↓ compare
   Detect drift (desired ≠ actual)
         ↓ act
   Reconcile (take action to converge)
         ↓ update
   Write new status back to apiserver
         ↑
   (loop repeats)
```

Controllers are **level-triggered**, not edge-triggered. They don't react to a single event and assume it was applied. They continuously compare state and act on the difference. This means:

- If a controller crashes and restarts, it re-reconciles from scratch — no lost events
- The order events arrive doesn't matter for correctness
- Reconcile must be **idempotent**: running it 10 times must have the same effect as running it once

**The watch mechanism:**

```bash
# What kubectl get pods -w actually does:
# 1. GET /api/v1/pods — lists all pods, gets resourceVersion
# 2. GET /api/v1/pods?watch=true&resourceVersion=<rv> — open long-poll
# 3. Server streams events: ADDED, MODIFIED, DELETED

# See the raw watch stream
kubectl get --raw "/api/v1/pods?watch=true&timeoutSeconds=30" | head -5

# Every object has a resourceVersion (monotonically increasing per etcd revision)
kubectl get pod <name> -o jsonpath='{.metadata.resourceVersion}'
```

**The full path from `kubectl apply` to running pod:**

```
kubectl apply -f pod.yaml
    ↓ HTTPS to apiserver
kube-apiserver:
  1. AuthN (x509 cert, bearer token, OIDC, webhook)
  2. AuthZ (RBAC: can this identity CREATE pods in this namespace?)
  3. Admission — Mutating webhooks (inject sidecars, set defaults)
  4. Object schema validation
  5. Admission — Validating webhooks (enforce policies)
  6. Write to etcd
  7. Notify watchers (including scheduler, controllers)
    ↓
kube-scheduler watches for pods with spec.nodeName == ""
  → Runs filter + score pipeline
  → Writes spec.nodeName to pod
    ↓
kubelet on target node watches for pods assigned to its node
  → Calls CRI (containerd): pull image, create sandbox
  → Calls CNI: set up network
  → Starts container
  → Runs liveness/readiness probes
  → Reports status back to apiserver
    ↓
Pod status updated in etcd → available to all watchers
```

---

## 2. The Kubernetes scheduler

### 2.1 Scheduling pipeline

The scheduler runs every pending pod through a pipeline of plugins:

```
Pending pod queue (priority queue, sorted by PriorityClass)
    ↓
Pre-filter plugins (compute derived data once, cache for later plugins)
    ↓
Filter plugins (eliminate infeasible nodes — runs in parallel across nodes)
    ↓
Post-filter plugins (handle case where no node passes filter — preemption)
    ↓
Pre-score plugins (prepare data for scoring)
    ↓
Score plugins (rank feasible nodes — runs in parallel)
    ↓
Normalize scores (scale per-plugin scores to 0–100)
    ↓
Reserve (tentatively claim resources — prevent double-booking)
    ↓
Pre-bind (e.g., provision a PVC, create volume attachment)
    ↓
Bind (write nodeName to pod spec — commits the decision)
    ↓
Post-bind (cleanup, metrics, etc.)
```

**Built-in filter plugins:**

| Plugin | Checks |
|--------|--------|
| `NodeUnschedulable` | `spec.unschedulable == true` on node |
| `NodeAffinity` | `requiredDuringScheduling` in pod spec |
| `TaintToleration` | Every node taint has a matching toleration |
| `PodTopologySpread` | `topologySpreadConstraints` hard rules |
| `InterPodAffinity` | `requiredDuringSchedulingIgnoredDuringExecution` anti-affinity |
| `NodeResourcesFit` | cpu+memory requested ≤ node allocatable |
| `VolumeBinding` | All PVCs can be satisfied (IPAM, node affinity of PV) |
| `NodePorts` | No host port conflicts |
| `EBSLimits` | AWS: max EBS volumes per node |

**Built-in score plugins:**

| Plugin | Scoring strategy | Default weight |
|--------|-----------------|---------------|
| `LeastAllocated` | Prefer nodes with lowest allocation ratio | 1 |
| `BalancedAllocation` | Penalise nodes with unbalanced cpu:memory ratio | 1 |
| `NodeAffinity` | Reward matching preferred affinity rules | 2 |
| `InterPodAffinity` | Reward preferred co-location/anti-affinity | 2 |
| `ImageLocality` | Reward nodes that already have the container image | 1 |
| `TaintToleration` | Reward nodes with fewer untolerated taints | 1 |

```bash
# Why is my pod pending?
kubectl describe pod <name>
# Events section is the gold mine
# "0/5 nodes are available: 3 Insufficient cpu, 2 node(s) had taint..."

# Verbose scheduler logging (not for production)
# Edit scheduler config to add --v=10

# Real-time scheduling events
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | grep -i schedule

# Node allocatable vs requested
kubectl describe node <name> | grep -A8 "Allocated resources"
# Shows: cpu requests vs allocatable, memory requests vs allocatable

# Which pods are on which node (for debugging spread)
kubectl get pods -o wide --all-namespaces | sort -k8  # sort by node
```

### 2.2 Preemption

When a high-priority pod can't be scheduled because no node has capacity, the scheduler may evict lower-priority pods to free space.

**Preemption algorithm:**

```
1. No feasible node found for pod P (priority class = 1000)
2. Scheduler finds "potential preemption victims" per node:
   - Only considers pods with lower priority than P
   - Eliminates victims protected by PodDisruptionBudgets
3. Selects the node that requires killing fewest victims
   (tie-break: prefer killing lowest-priority victims, then highest-priority victims in
    the smallest number, then prefer nodes with latest-starting pods)
4. Sets pod.status.nominatedNodeName to signal the intent
5. Deletes victim pods (graceful termination)
6. Waits for node capacity to free up
7. Pod eventually schedules (no guarantee — another pod could grab the space)
```

```yaml
# Priority classes
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: system-critical
value: 2000000   # system-cluster-critical = 2000000000 (highest)
globalDefault: false
preemptionPolicy: PreemptLowerPriority  # default

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-background
value: 100
preemptionPolicy: Never   # This class never preempts others
```

```bash
# Check if a pod was preempted or nominated
kubectl get pod <name> -o jsonpath='{.status.nominatedNodeName}'

# See priority of all pods on a node
kubectl get pods --all-namespaces -o custom-columns=\
'NAME:.metadata.name,PRIORITY:.spec.priority,NODE:.spec.nodeName' \
--field-selector=spec.nodeName=<node>
```

### 2.3 Scheduling framework and custom plugins

The modern extension mechanism. You implement Go interfaces and register them as plugins.

**Extension point interfaces (abbreviated):**

```go
type FilterPlugin interface {
    Plugin
    Filter(ctx context.Context, state *CycleState, p *v1.Pod, nodeInfo *NodeInfo) *Status
}

type ScorePlugin interface {
    Plugin
    Score(ctx context.Context, state *CycleState, p *v1.Pod, nodeName string) (int64, *Status)
    ScoreExtensions() ScoreExtensions  // for normalisation
}

type PreFilterPlugin interface {
    Plugin
    PreFilter(ctx context.Context, state *CycleState, p *v1.Pod) (*PreFilterResult, *Status)
}

// CycleState is a per-scheduling-cycle key-value store for plugins to communicate
```

```go
// Example: Filter plugin that requires pods to have a specific node label
type RequireLabelPlugin struct {
    requiredLabel string
}

func (p *RequireLabelPlugin) Name() string { return "RequireLabel" }

func (p *RequireLabelPlugin) Filter(ctx context.Context, state *framework.CycleState,
    pod *v1.Pod, nodeInfo *framework.NodeInfo) *framework.Status {

    if _, ok := nodeInfo.Node().Labels[p.requiredLabel]; !ok {
        return framework.NewStatus(
            framework.Unschedulable,
            fmt.Sprintf("node missing required label: %s", p.requiredLabel),
        )
    }
    return nil
}

// Register the plugin
func New(obj runtime.Object, h framework.Handle) (framework.Plugin, error) {
    args := obj.(*config.RequireLabelArgs)
    return &RequireLabelPlugin{requiredLabel: args.Label}, nil
}
```

```yaml
# Scheduler configuration referencing custom plugin
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
- schedulerName: custom-scheduler
  plugins:
    filter:
      enabled:
      - name: RequireLabel
    score:
      disabled:
      - name: LeastAllocated  # disable built-in if you're replacing it
  pluginConfig:
  - name: RequireLabel
    args:
      label: "scheduling.io/approved"
```

---

## 3. Operators and CRDs

### 3.1 The controller pattern in depth

The reconcile loop is simple. Making it correct and production-grade is the challenge.

**Idempotency requirements:**

```go
// WRONG: assumes we're creating from scratch
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    dep := &appsv1.Deployment{...}
    return ctrl.Result{}, r.Create(ctx, dep)  // Fails on second call!
}

// CORRECT: create or update
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
        Name: req.Name, Namespace: req.Namespace,
    }}
    _, err := ctrl.CreateOrUpdate(ctx, r.Client, dep, func() error {
        dep.Spec = buildDesiredSpec(...)
        return nil
    })
    return ctrl.Result{}, err
}
```

**Owner references — garbage collection:**

```go
// Set owner reference so child is garbage collected when parent is deleted
ctrl.SetControllerReference(parent, child, r.Scheme)
// Adds: child.OwnerReferences = [{apiVersion, kind, name, uid, controller: true}]
// When parent is deleted, GC controller deletes the child
```

**Requeueing — handling transient failures:**

```go
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    // Transient error: retry after 30 seconds
    if err := r.someOperation(); isTransient(err) {
        return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
    }

    // Permanent error: don't retry
    if err := r.validate(); err != nil {
        return ctrl.Result{}, nil  // No requeue; log the error
    }

    // Re-reconcile periodically (e.g., for drift detection)
    return ctrl.Result{RequeueAfter: 5 * time.Minute}, nil
}
```

**Status conditions — the standard pattern:**

```go
// Update status with conditions (standard Kubernetes pattern)
meta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
    Type:               "Ready",
    Status:             metav1.ConditionTrue,
    ObservedGeneration: obj.Generation,
    LastTransitionTime: metav1.Now(),
    Reason:             "ReconcileSucceeded",
    Message:            "All resources are healthy",
})
```

**`ObservedGeneration`:** Critical for correct status reporting. `metadata.generation` increments on spec changes. Setting `status.observedGeneration` tells consumers whether the status reflects the current spec or a previous one.

### 3.2 CRD schema and validation

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: circuitbreakers.resilience.io
spec:
  group: resilience.io
  versions:
  - name: v1
    served: true
    storage: true
    schema:
      openAPIV3Schema:
        type: object
        required: ["spec"]
        properties:
          spec:
            type: object
            required: ["targetService", "threshold"]
            properties:
              targetService:
                type: string
                description: "Service to protect"
              threshold:
                type: integer
                minimum: 1
                maximum: 100
                description: "Error percentage to trigger open state"
              windowSeconds:
                type: integer
                minimum: 10
                default: 60
              halfOpenRequests:
                type: integer
                minimum: 1
                default: 5
          status:
            type: object
            properties:
              state:
                type: string
                enum: ["closed", "open", "half-open"]
              observedGeneration:
                type: integer
                format: int64
              conditions:
                type: array
                items:
                  type: object
                  required: ["type", "status"]
                  properties:
                    type: {type: string}
                    status: {type: string, enum: ["True", "False", "Unknown"]}
                    reason: {type: string}
                    message: {type: string}
                    lastTransitionTime: {type: string, format: date-time}
                    observedGeneration: {type: integer, format: int64}
    subresources:
      status: {}   # Separate status updates (avoids race conditions)
    additionalPrinterColumns:
    - name: Target
      type: string
      jsonPath: .spec.targetService
    - name: State
      type: string
      jsonPath: .status.state
    - name: Age
      type: date
      jsonPath: .metadata.creationTimestamp
  scope: Namespaced
  names:
    plural: circuitbreakers
    singular: circuitbreaker
    kind: CircuitBreaker
    shortNames: ["cb"]
```

```bash
# Check CRD establishment
kubectl get crd circuitbreakers.resilience.io -o jsonpath='{.status.conditions}'

# Validate a resource against its schema
kubectl apply --dry-run=server -f my-circuit-breaker.yaml

# Watch CRD objects
kubectl get cb -w

# See schema violations in apiserver logs
kubectl logs -n kube-system kube-apiserver-<node> | grep -i "validation\|invalid"
```

### 3.3 Admission webhooks

**Mutating webhook — inject sidecar:**

```go
type SidecarInjector struct{}

func (si *SidecarInjector) Handle(ctx context.Context, req admission.Request) admission.Response {
    pod := &corev1.Pod{}
    if err := json.Unmarshal(req.Object.Raw, pod); err != nil {
        return admission.Errored(http.StatusBadRequest, err)
    }

    // Only inject if annotation present
    if _, ok := pod.Annotations["sidecar.io/inject"]; !ok {
        return admission.Allowed("no injection requested")
    }

    // Build patch
    sidecar := corev1.Container{
        Name:  "metrics-proxy",
        Image: "internal/metrics-proxy:v1.2.0",
        Ports: []corev1.ContainerPort{{Name: "metrics", ContainerPort: 9090}},
        Resources: corev1.ResourceRequirements{
            Requests: corev1.ResourceList{
                corev1.ResourceCPU:    resource.MustParse("10m"),
                corev1.ResourceMemory: resource.MustParse("32Mi"),
            },
            Limits: corev1.ResourceList{
                corev1.ResourceMemory: resource.MustParse("64Mi"),
            },
        },
    }
    pod.Spec.Containers = append(pod.Spec.Containers, sidecar)

    marshalled, _ := json.Marshal(pod)
    return admission.PatchResponseFromRaw(req.Object.Raw, marshalled)
}
```

**Webhook registration:**

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: sidecar-injector
webhooks:
- name: inject.sidecar.io
  admissionReviewVersions: ["v1"]
  sideEffects: None
  failurePolicy: Ignore   # If webhook is down, allow pod creation
  namespaceSelector:      # Only inject in labelled namespaces
    matchLabels:
      sidecar-injection: enabled
  rules:
  - apiGroups: [""]
    apiVersions: ["v1"]
    resources: ["pods"]
    operations: ["CREATE"]
  clientConfig:
    service:
      name: sidecar-injector
      namespace: kube-system
      path: /mutate
    caBundle: <base64-encoded-CA-cert>
  timeoutSeconds: 5
```

**Critical `failurePolicy` decision:**

- `failurePolicy: Fail` — if webhook is unreachable or returns error, the API request is rejected. A crashed webhook can break the entire cluster (can't create pods, deployments, etc.)
- `failurePolicy: Ignore` — if webhook is down, the request proceeds without mutation/validation

Use `Fail` for security-critical validating webhooks (you'd rather fail open than allow a policy violation). Use `Ignore` for mutating webhooks that add convenience features (sidecar injection, default labels).

---

## 4. Kubernetes networking in depth

### 4.1 How Services work — three implementations

**ClusterIP — the foundation:**

A ClusterIP Service gets a virtual IP (VIP) from the service CIDR (e.g., `10.96.0.0/12`). This IP is not bound to any interface. It exists only in kube-proxy's iptables rules (or eBPF programs). When a pod sends a packet to the VIP, iptables DNAT rewrites the destination to one of the Endpoint pod IPs before the packet leaves the node.

```bash
# See the VIP
kubectl get svc myservice -o jsonpath='{.spec.clusterIP}'

# See the endpoints
kubectl get endpoints myservice
kubectl get endpointslices -l kubernetes.io/service-name=myservice

# Trace the iptables path (kube-proxy iptables mode)
# KUBE-SERVICES → KUBE-SVC-<hash> → KUBE-SEP-<hash> (per endpoint)
iptables -t nat -L KUBE-SERVICES | grep <service-ip>
iptables -t nat -L KUBE-SVC-<hash>
# Shows probabilistic rules selecting backends

# How probability is computed:
# For 3 endpoints: 1/3 → 1/2 → 1/1 (remaining)
# Rule 1: 33% chance → EP1
# Rule 2 (only reached 67% of time): 50% → EP2
# Rule 3 (only reached 33% of time): 100% → EP3
# Net result: each endpoint gets exactly 33%
```

**IPVS mode:**

```bash
# Switch kube-proxy to IPVS
kubectl edit configmap kube-proxy -n kube-system
# Set mode: "ipvs"

# After switch, see IPVS virtual servers
ipvsadm -L -n
# Proto  LocalAddress:Port Scheduler Flags
#   -> RemoteAddress:Port           Forward Weight ActiveConn InActConn
# TCP  10.96.0.1:443 rr
#   -> 192.168.1.10:6443            Masq    1      5          0

# Available schedulers: rr (round-robin), lc (least-connection),
# dh (destination hash), sh (source hash), sed (shortest expected delay)
kubectl edit configmap kube-proxy -n kube-system
# Set: ipvs.scheduler: "lc"
```

**Session affinity:**

```yaml
# Sticky sessions: route same client to same pod
spec:
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 10800  # 3 hours
```

### 4.2 DNS internals — CoreDNS and the ndots problem

**CoreDNS architecture:**

CoreDNS is the in-cluster DNS server (default since Kubernetes 1.13). It's a modular DNS server written in Go. Each plugin processes the DNS query in order:

```
Request → [errors] → [health] → [ready] → [kubernetes] → [prometheus] → [forward] → Response
```

The `kubernetes` plugin handles in-cluster names (`*.cluster.local`). The `forward` plugin proxies external lookups to upstream resolvers (typically the node's resolvers, which may be a cloud provider's DNS or corporate DNS).

**The ndots problem — in full detail:**

Every pod's `/etc/resolv.conf`:
```
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local
options ndots:5
```

`ndots:5`: if the name has fewer than 5 dots, it's treated as relative and each search domain is appended before trying as absolute.

```
Name: postgres
Dots: 0 (< 5) → relative
Tries:
  1. postgres.default.svc.cluster.local    → hit (found in CoreDNS)
  Returns IP immediately

Name: api.stripe.com
Dots: 2 (< 5) → relative
Tries:
  1. api.stripe.com.default.svc.cluster.local  → NXDOMAIN
  2. api.stripe.com.svc.cluster.local          → NXDOMAIN
  3. api.stripe.com.cluster.local              → NXDOMAIN
  4. api.stripe.com.                           → hit (external DNS)
3 unnecessary queries per external lookup!

Name: api.stripe.com.  (trailing dot = absolute/FQDN)
Dots: 2 but treated as FQDN because of trailing dot
Tries:
  1. api.stripe.com.                           → hit immediately
```

**Fix strategies:**

```yaml
# 1. Set ndots:1 for pods that mostly call external services
spec:
  dnsConfig:
    options:
    - name: ndots
      value: "1"
  # Now external names resolve in 1 lookup; internal names need FQDNs

# 2. Use FQDN for internal services in code
# Instead of: postgresql:5432
# Use: postgresql.production.svc.cluster.local:5432

# 3. NodeLocal DNSCache (reduces CoreDNS pressure, adds caching per node)
# Runs a DNS cache daemonset on each node
# Intercepts DNS queries before they reach CoreDNS
```

```bash
# Measure DNS latency
kubectl run debug --image=busybox --rm -it -- sh
time nslookup api.stripe.com   # Shows lookup time

# CoreDNS metrics (forward to Prometheus)
kubectl port-forward -n kube-system svc/kube-dns 9153
curl localhost:9153/metrics | grep coredns_dns_request_duration

# CoreDNS cache hit rate
coredns_cache_hits_total / (coredns_cache_hits_total + coredns_cache_misses_total)

# Watch DNS queries in real time (tcpdump on CoreDNS pod's interface)
kubectl exec -n kube-system <coredns-pod> -- tcpdump -n udp port 53
```

### 4.3 NetworkPolicy — how enforcement works

NetworkPolicy objects are declarative intent. The CNI plugin enforces them. If your CNI doesn't support NetworkPolicy (Flannel by default), the policies exist but do nothing.

**The default behaviour without any NetworkPolicy:** All traffic allowed. Any pod can reach any other pod on any port.

**As soon as one NetworkPolicy selects a pod:** That pod is now deny-all by default. You must explicitly allow all needed traffic.

```yaml
# Complete example: a database pod that only accepts connections from specific app pods
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: postgres-policy
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: postgres
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    # AND condition: pod must match BOTH the namespace AND pod selector
    - namespaceSelector:
        matchLabels:
          name: production
      podSelector:
        matchLabels:
          db-client: "true"
    ports:
    - protocol: TCP
      port: 5432
  egress:
  # Allow DNS (always needed)
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports:
    - protocol: UDP
      port: 53
```

**Common gotcha — OR vs AND:**

```yaml
# This is OR: pod in production namespace, OR pod with label app=frontend
from:
- namespaceSelector:
    matchLabels: {name: production}
- podSelector:
    matchLabels: {app: frontend}

# This is AND: pod that is BOTH in production namespace AND has label app=frontend
from:
- namespaceSelector:
    matchLabels: {name: production}
  podSelector:
    matchLabels: {app: frontend}
```

```bash
# Test NetworkPolicy enforcement
kubectl exec -n production debug -- nc -zv postgres-svc 5432  # Should work
kubectl exec -n other-ns debug -- nc -zv <postgres-pod-ip> 5432  # Should fail

# Debug with Cilium
cilium monitor --type drop
# Shows dropped packets with reason (policy, invalid, ...)

# Debug with Calico
calicoctl policy trace --src-pod production/app-1 --dst-pod production/postgres-1 --dst-port 5432
```

---

## 5. Resource management and autoscaling

### 5.1 The resource model — requests, limits, QoS

**Requests vs limits — two different mechanisms:**

- `requests`: used by the **scheduler** to find a fitting node. The kubelet guarantees this amount is available. Does not limit actual usage.
- `limits`: enforced by the **kernel** via cgroups. CPU limit → CPU throttling. Memory limit → OOMKill if exceeded.

A container with `cpu: 100m` request and `cpu: 2000m` limit can use up to 2 full CPUs — but can also be throttled if the cgroup quota is hit in a burst. A container with no CPU limit can use all available CPU on the node.

**QoS class determination:**

```
Guaranteed:
  - Every container has both cpu and memory requests AND limits
  - Requests == Limits for each resource in each container

Burstable:
  - At least one container has a request or limit
  - Does not meet Guaranteed criteria

BestEffort:
  - No containers have any requests or limits
```

```bash
# Check QoS class
kubectl get pod <name> -o jsonpath='{.status.qosClass}'

# See all pods grouped by QoS
kubectl get pods --all-namespaces -o custom-columns=\
'NAME:.metadata.name,NS:.metadata.namespace,QOS:.status.qosClass' | sort -k3
```

**Node pressure and eviction:**

kubelet monitors node resources. When they get low, it starts evicting pods:

```
Memory pressure:
  1. BestEffort pods (oom_score_adj = 1000, killed first)
  2. Burstable pods exceeding their request (sorted by oom_score)
  3. Guaranteed pods (oom_score_adj = -997, killed last)

Disk pressure:
  1. Pods with largest local storage usage
  2. Eviction order: BestEffort → Burstable → Guaranteed

Eviction thresholds (configurable in kubelet config):
  memory.available < 100Mi → soft eviction (wait for grace period)
  memory.available < 50Mi  → hard eviction (immediate, no grace period)
```

```bash
# kubelet eviction thresholds
cat /var/lib/kubelet/config.yaml | grep -A20 eviction

# Node pressure conditions
kubectl describe node <name> | grep -A10 Conditions

# Prometheus alert for node memory pressure
node_memory_MemAvailable_bytes < 0.1 * node_memory_MemTotal_bytes
```

### 5.2 HPA internals

**The control loop:**

```
Every 15 seconds (--horizontal-pod-autoscaler-sync-period):

1. Fetch metric from metrics-server (cpu/memory) or custom metrics API
2. Calculate desired replicas:
   desiredReplicas = ceil(currentReplicas × (currentMetricValue / desiredMetricValue))

3. Apply tolerance: if |current/desired - 1| < 0.1 (10%), no scaling
4. Apply scale-down stabilisation window (default 5m):
   Only scale down if the desired count has been lower than current for 5 full minutes
   (prevents thrashing)
5. Apply min/max bounds
6. Update Deployment replicas if changed
```

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  minReplicas: 3
  maxReplicas: 50
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 60   # Target 60% of cpu request
  - type: Resource
    resource:
      name: memory
      target:
        type: AverageValue
        averageValue: 400Mi      # Target 400Mi average per pod
  - type: Pods
    pods:
      metric:
        name: requests_per_second  # Custom metric
      target:
        type: AverageValue
        averageValue: "1000"
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 10            # Scale down max 10% of pods at once
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0  # Scale up immediately
      policies:
      - type: Percent
        value: 100           # Double the pod count if needed
        periodSeconds: 15
      - type: Pods
        value: 5             # Or add 5 pods at once, whichever is smaller
        periodSeconds: 15
      selectPolicy: Min
```

### 5.3 Karpenter — node provisioning architecture

Karpenter watches for unschedulable pods and provisions the ideal EC2 instance directly (bypassing ASGs).

**Decision process:**

```
Unschedulable pod detected
    ↓
Karpenter simulates scheduling: what instance type would allow this pod to schedule?
    ↓
Considers: cpu request, memory request, GPU, topology constraints, pod affinity,
           spot vs on-demand preference, instance family preferences
    ↓
Calls EC2 API directly: RunInstances with ideal instance type
    ↓
Node joins cluster (typically 30-60s for Nitro-based instances)
    ↓
Pod schedules on new node

(Independently)
Consolidation loop runs continuously:
    → Find nodes with low utilisation
    → Simulate: can all pods on this node fit elsewhere?
    → If yes: cordon + drain node, let pods reschedule, terminate instance
```

**Disruption budgets in Karpenter:**

```yaml
spec:
  disruption:
    consolidationPolicy: WhenUnderutilized
    consolidateAfter: 30s     # How long a node must be underutilised before consolidation
    budgets:
    - nodes: "10%"            # Never consolidate more than 10% of nodes simultaneously
    - nodes: "0"              # Block all disruption during specific schedule
      schedule: "0 22 * * *"  # (cron: 10pm UTC daily)
      duration: 8h
```

---

## 6. etcd internals

### 6.1 MVCC — Multi-Version Concurrency Control

etcd doesn't just store current values. It stores a complete history of every key-value pair, indexed by a global revision counter.

**Key concepts:**

- **Revision:** A global, monotonically increasing integer. Every write increments it. Think of it as a transaction ID.
- **Key revision:** Each key also has its own create revision (when it was first written), mod revision (when it was last modified), and version (how many times it's been modified).
- **MVCC backend:** etcd uses bbolt (a B+ tree), storing key+revision as the composite key. This allows efficient range scans by revision.

```bash
# See revision info for a key
etcdctl get /registry/pods/default/mypod -w json | jq '.kvs[0] | {
  key: (.key | @base64d),
  create_revision: .create_revision,
  mod_revision: .mod_revision,
  version: .version
}'

# Get old value at a specific revision (history)
etcdctl get /registry/pods/default/mypod --rev=1042

# Watch all changes from a specific revision (replay)
etcdctl watch --prefix /registry/pods --rev=1000
# This is how controllers catch up after a restart

# Current cluster revision
etcdctl endpoint status --cluster -w json | jq '.[0].Status.header.revision'
```

**How `kubectl watch` works end-to-end:**

```
kubectl get pods -w
  → GET /api/v1/namespaces/default/pods?watch=true&resourceVersion=<rv>
  → kube-apiserver opens a watch on etcd: WATCH /registry/pods/ --rev=<rv>
  → etcd streams events for all key changes under that prefix since that revision
  → apiserver decodes, filters, and streams to kubectl as JSON events
  → kubectl prints the events
```

### 6.2 compaction, defragmentation, and backups

**Why compaction is needed:**

Every write creates a new revision. Without compaction, etcd keeps every historical revision forever. The database grows unboundedly. Auto-compaction is critical.

```bash
# Configure auto-compaction (in etcd flags or config)
--auto-compaction-mode=periodic   # or 'revision'
--auto-compaction-retention=1h    # Keep 1 hour of history

# Manual compaction
REV=$(etcdctl endpoint status -w json | jq '.[0].Status.header.revision')
etcdctl compact $REV

# After compaction: disk space is NOT freed
# Data is marked as reclaimable but the bbolt file doesn't shrink
# You must defragment to reclaim space
etcdctl defrag --cluster

# Check database size
etcdctl endpoint status --cluster -w table
# Shows: DB SIZE (current), DB SIZE IN USE (after compaction)
# If SIZE >> SIZE IN USE, you need to defrag
```

**Backup:**

```bash
# Snapshot (consistent point-in-time backup)
ETCDCTL_API=3 etcdctl snapshot save /backup/etcd-$(date +%Y%m%d-%H%M%S).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt \
  --key=/etc/kubernetes/pki/etcd/healthcheck-client.key

# Verify
etcdctl snapshot status /backup/etcd-20240615.db -w table

# Restore
etcdctl snapshot restore /backup/etcd-20240615.db \
  --name etcd-1 \
  --initial-cluster etcd-1=https://10.0.0.10:2380 \
  --initial-cluster-token etcd-cluster-1 \
  --initial-advertise-peer-urls https://10.0.0.10:2380 \
  --data-dir /var/lib/etcd-new

# Key metrics for etcd health
etcd_disk_wal_fsync_duration_seconds_bucket   # WAL write latency (> 10ms = issue)
etcd_disk_backend_commit_duration_seconds_bucket  # DB commit latency (> 25ms = issue)
etcd_server_proposals_failed_total            # Should be 0
etcd_server_leader_changes_seen_total         # Should be very low (< 5/day)
etcd_server_has_leader                        # Should always be 1
etcd_mvcc_db_total_size_in_bytes             # DB file size
etcd_mvcc_db_total_size_in_use_in_bytes      # Actual data size (if << total, defrag needed)
```

**Disk requirements:**

etcd is extremely sensitive to disk latency. WAL writes must fsync before returning success to clients. On a slow disk (HDD or overloaded cloud volume), WAL fsync latency > 10ms causes timeouts and leader instability.

```
Minimum: SSD with < 1ms write latency
Recommended: dedicated NVMe, io2/io1 EBS with provisioned IOPS
Never: HDD, burstable EBS (gp2/gp3 without provisioned IOPS) for production

# Check if your disk is fast enough
dd if=/dev/zero of=/var/lib/etcd/test-write bs=22 count=1 oflag=direct 2>&1
# Should complete in < 10ms

fio --rw=write --ioengine=sync --fdatasync=1 --directory=/var/lib/etcd \
    --size=22m --bs=2300 --name=etcd-test
# Look for: lat percentile (99th) — should be < 10ms
```

---

## Hands-on exercises

1. Write a complete Kubernetes operator (CRD + controller) using `controller-runtime`. Make it reconcile a custom resource that manages a ConfigMap. Include finalizer, status conditions, and owner references.
2. Write a MutatingAdmissionWebhook that sets a default resource request on every container that lacks one. Test with `--dry-run=server`.
3. Deploy kube-proxy in ipvs mode. Create a Service with 5 endpoints. Watch `ipvsadm -L -n` update as endpoints change.
4. Reproduce the ndots DNS issue: capture with tcpdump, count the extra NXDOMAIN queries for external names, then fix with per-pod dnsConfig and verify.
5. Compact and defragment a test etcd cluster. Measure space reclaimed before and after. Monitor `etcd_mvcc_db_total_size_in_bytes` vs `etcd_mvcc_db_total_size_in_use_in_bytes`.
6. Write a custom scheduler plugin (Filter) and deploy it using the scheduler plugin framework. Verify it's excluding nodes correctly via scheduler logs.

---

## What to study next → [Phase 3 — Observability & SRE](./phase3-observability-sre.md)

The systems you've learned to build in Phase 2 need to be observed and operated at scale. Phase 3 covers Prometheus TSDB internals (including cardinality — the most common Prometheus production problem), distributed tracing with OpenTelemetry, SLO engineering, and incident response discipline.
