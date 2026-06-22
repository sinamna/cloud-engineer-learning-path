# Phase 5 — Interview Prep
## System Design, Behavioral Questions, Knowledge Quiz, Common Gaps

> Interview preparation as a discipline: not memorising answers, but building the frameworks that generate correct answers under pressure.

---

## Interview strategy overview

Senior cloud engineering interviews test four things, in this priority order:

1. **Systems thinking** — can you reason about tradeoffs, failure modes, and scale?
2. **Technical depth** — do you have genuine understanding below the surface?
3. **Communication** — can you explain complex ideas clearly under time pressure?
4. **Experience** — have you operated real systems and learned from them?

The mistake most engineers make: they prepare answers to questions. Prepare frameworks instead — systematic ways of thinking through any problem in the domain. An interviewer who asks "design a message queue" is really asking "show me how you think about distributed systems."

---

## 1. System design — methodology and scenarios

### 1.1 The universal system design framework

Use this for every system design question, regardless of the specific system:

```
Phase 1: Clarify (5 minutes)
  - Functional requirements: what does the system do?
  - Non-functional requirements: scale, latency, availability, consistency?
  - Constraints: existing stack, team size, budget, timeline?
  - Scope: what's in and out of scope for this design session?

Phase 2: Estimate (3 minutes)
  - Order of magnitude: QPS, storage, bandwidth
  - Identify if this is read-heavy, write-heavy, or balanced
  - Identify the bottleneck components early

Phase 3: High-level design (10 minutes)
  - Draw the major components and their relationships
  - Define the data model (what's stored where)
  - Define the API contracts (what does each component expose?)
  - Don't go deep yet — validate the overall shape

Phase 4: Deep dive (20 minutes)
  - Interviewer will pick 1-2 components to go deep on
  - OR you say "the most interesting/challenging part is X, let me go deep"
  - This is where you show your actual depth

Phase 5: Tradeoffs and evolution (5 minutes)
  - What would you change if scale increased 10×?
  - What are the failure modes and mitigations?
  - What would you simplify for an MVP vs the full design?
```

**What interviewers are evaluating at each phase:**

| Phase | What they watch for |
|-------|-------------------|
| Clarify | Do you ask the right questions? Do you drive toward concrete requirements? |
| Estimate | Back-of-envelope comfort. Can you size systems? |
| High-level | Do you know the building blocks? Are the components sensible? |
| Deep dive | Do you have genuine depth or just buzzword recognition? |
| Tradeoffs | Senior engineers say "it depends" and mean it with specifics |

### 1.2 Scenario: high-throughput event streaming platform

**Prompt:** "Design a messaging system that can handle 1 million messages per second, with guaranteed delivery and the ability to replay messages."

**Clarifying questions to ask:**
- What's the message size distribution? (small ≤1KB vs large ≤1MB changes storage architecture)
- What delivery semantics? (at-least-once, exactly-once, at-most-once)
- What's the required replay window? (1 day, 7 days, forever)
- What's the consumer model? (push vs pull, fan-out factor, consumer group behaviour)
- Latency requirements? (< 10ms publish-to-consume? < 1s?)
- Ordering requirements? (global order? per-partition order? per-key order?)

**High-level design:**

```
Producers → Load balancer → Broker cluster (partitioned log) → Consumer groups
                                    ↓
                             Object storage (long-term retention)
                                    ↑
                             Replay consumer (reads from offset X)
```

**Partitioned log — the core data structure:**

The fundamental abstraction: an append-only, ordered, immutable log divided into partitions. Each partition is an independent ordered sequence. Consumers track their offset within each partition.

```
Partition 0: [msg1][msg3][msg7][msg12]...  offset: 0,1,2,3...
Partition 1: [msg2][msg5][msg8][msg13]...
Partition 2: [msg4][msg6][msg9][msg14]...
```

**Estimating storage:**

```
1M messages/sec × 1KB average size = 1GB/sec ingestion
7-day retention: 1GB/sec × 86400 × 7 = ~605TB
With 3× replication: ~1.8PB

For 1M msg/sec throughput:
  Each broker handles ~200MB/sec write (sequential, which NVMe can do at 3GB/sec)
  5-6 brokers sufficient for write throughput
  Consumer fan-out (100 consumers) requires 100GB/sec read throughput
  → 50+ brokers, or tiered storage to object store for consumers
```

**Leader/follower replication for durability:**

```
Producer sends to partition leader
  → Leader appends to log segment (local disk)
  → Leader sends to ISR (In-Sync Replicas) — followers with lag < threshold
  → Once majority of ISR acknowledge → mark as committed → return ACK to producer
  → If leader fails → elect new leader from ISR → no data loss for committed messages
```

**Deep dive: consumer groups and offset management:**

```
Consumer group: set of consumers sharing work on a topic
  - Each partition is assigned to exactly one consumer in the group
  - Adding consumers to a group → rebalance → partitions redistributed
  - Removing consumers → rebalance → surviving consumers take over partitions

Offset commits:
  - Consumer reads message → processes it → commits offset
  - If consumer crashes before commit: message replayed (at-least-once)
  - For exactly-once: transactional offset commits (write output + commit offset atomically)

Rebalance protocols:
  - Eager (stop-the-world): all consumers release all partitions, then re-assign
  - Cooperative (incremental): only affected partitions are moved — reduces downtime
```

**Failure modes:**

```
Partition leader failure:
  → Controller detects via heartbeat timeout
  → New leader elected from ISR
  → Clients reconnect to new leader
  → Duration: typically 1-5 seconds

Broker failure (non-leader):
  → Partitions where this broker was replica: replica count drops
  → Other ISR members catch up from leader
  → If replica count drops below min.insync.replicas: writes rejected (prevent data loss)

Consumer failure:
  → Group rebalance triggered
  → Partitions redistributed
  → New consumer starts from last committed offset

Full cluster outage:
  → Messages accumulated in producer retry buffer (configurable)
  → After timeout: producers return errors
  → Consumers: no new data until cluster restores
```

---

### 1.3 Scenario: multi-tenant Kubernetes platform

**Prompt:** "Design a Kubernetes platform for 100 engineering teams. Each team has different compliance requirements: some handle PCI-DSS data, some handle GDPR-sensitive data, most handle neither."

**Clarifying questions:**
- How many teams? What's the average number of services per team?
- What does "different compliance" mean operationally? Network isolation? Audit logs? Encryption at rest?
- Single cluster or multi-cluster?
- What's the team's Kubernetes experience level?
- What's the approval process for new services?
- What cloud provider? Managed Kubernetes or self-managed?

**Isolation model decision:**

```
Option A: Single cluster, namespace-per-team
  Pros: Simple, cheap, easy management
  Cons: Noisy neighbour (CPU/memory), etcd size limits (~500MB practical),
        shared kernel (security risk for different trust levels)

Option B: Namespace-per-team in shared cluster + dedicated cluster for PCI/GDPR
  Pros: Balanced; most teams share infrastructure, sensitive workloads isolated
  Cons: Two clusters to manage, application teams must know which cluster

Option C: Cluster-per-team
  Pros: Maximum isolation
  Cons: 100 clusters to maintain, cost, operational overhead
  → Only appropriate if teams are large (10+ engineers) or have strict isolation needs

Recommended: B — tiered isolation based on compliance needs
```

**Namespace-per-team configuration (automated via Operator):**

```yaml
# Team operator creates these resources per TeamNamespace CRD
ResourceQuota:
  requests.cpu: "20"          # Team gets 20 CPU cores
  requests.memory: "40Gi"
  limits.cpu: "40"
  limits.memory: "80Gi"
  count/pods: "100"
  count/services: "20"
  count/persistentvolumeclaims: "10"

LimitRange:
  default:
    cpu: "100m"
    memory: "256Mi"
  defaultRequest:
    cpu: "50m"
    memory: "128Mi"
  max:
    cpu: "4"
    memory: "8Gi"

NetworkPolicy (default-deny + allow ingress):
  - deny all ingress by default
  - allow from ingress controller namespace
  - allow within same namespace
  - allow egress to kube-dns (UDP 53)
  - allow egress to same namespace

RBAC:
  - Team members: edit role (no RBAC changes, no NetworkPolicy changes)
  - Team leads: custom role (can create ServiceAccounts for CI/CD)
```

**PCI-DSS workloads (dedicated cluster):**

```
Additional requirements:
  - Network: all traffic through WAF + DPI, egress whitelist only
  - Encryption: etcd encrypted at rest (kms-plugin), all secrets encrypted
  - Audit: full Kubernetes audit log, API access logged to SIEM
  - Images: signed images only (Cosign + ImagePolicyWebhook)
  - Nodes: dedicated hardware (no multi-tenancy), IMDSv2 only on AWS
  - Secrets: Vault with HSM backend, no Kubernetes Secrets for credentials
  - Scanning: runtime security (Falco), continuous vulnerability scanning
```

---

### 1.4 Scenario: observability platform

**Prompt:** "Design an observability platform for 500 microservices generating 50 million metric data points per second."

**Estimating storage:**

```
50M samples/sec
  Prometheus compressed: ~1.5 bytes/sample (XOR encoding)
  50M × 1.5B = 75MB/sec ingestion rate
  90-day retention: 75MB × 86400 × 90 = ~583TB

In practice, use tiered storage:
  Hot tier (< 2h): Prometheus memory (in-head block)
  Warm tier (2h to 30d): Remote storage (Thanos/Cortex/Mimir) on fast SSD
  Cold tier (30d to 90d): Object storage (S3/GCS) — 10-100× cheaper than SSD
```

**Architecture options:**

```
Option A: Prometheus + Thanos (your existing knowledge area)
  Federated Prometheus (one per cluster) + Thanos for global aggregation
  Pros: mature, well-understood, battle-tested
  Cons: Prometheus memory limits (~2M series per instance), complex at this scale

Option B: Grafana Mimir (horizontally scalable, drop-in Prometheus-compatible)
  Single binary or microservices mode
  Pros: unlimited horizontal scale, object storage native, multi-tenancy built-in
  Cons: more complex operational model than vanilla Prometheus

Option C: VictoriaMetrics
  Pros: very high ingestion rate (5× Prometheus on same hardware), low resource usage
  Cons: different query dialect (MetricsQL, mostly compatible)
```

**For 50M samples/sec, choose Mimir or VictoriaMetrics.** Prometheus has practical limits around 1-2M active series per instance and would require 25-50 instances for this scale, with complex federation.

**Mimir architecture (microservices mode):**

```
Producers (Prometheus instances, OTel collectors)
    ↓ remote_write
Distributor (fan-out to ingesters based on hash ring, validates, deduplicates)
    ↓
Ingester (writes to WAL, holds last 2h in memory, streams to object store)
    ↓
Object store (S3/GCS — long-term storage as Parquet blocks)
    ↓ (at query time)
Store Gateway (queries object store, caches index)
Querier (merges results from ingesters + store gateway)
Query Frontend (caches queries, splits large range queries, deduplicates)
    ↓
Grafana
```

---

## 2. Behavioral questions — frameworks

### 2.1 The STAR format and why it works

**S**ituation, **T**ask, **A**ction, **R**esult.

STAR works because interviewers need concrete evidence, not assertions. "I'm good at debugging" is an assertion. "I identified a 300ms p99 spike every 100ms in a NATS consumer and traced it to CPU throttling from a misconfigured cgroup quota" is evidence.

**Common failure modes:**
- Too much Situation (3 minutes explaining context, 30 seconds on Action)
- Vague Actions ("we investigated and fixed it") — go specific
- No Result ("it was better after") — always have a number
- No reflection ("the lesson was...") — senior engineers learn from experiences

**Timing target:** 2-3 minutes per answer, 90 seconds maximum on Situation + Task.

### 2.2 Key behavioral themes and what interviewers are really testing

**"Tell me about the most complex infrastructure problem you've solved."**

*What they're testing:* Technical depth, systematic thinking, ability to navigate ambiguity

*Strong answer elements:*
- Problem had multiple possible causes (you had to narrow it down systematically)
- Required understanding multiple system layers (not just "I bumped the config")
- Had business impact (why it mattered)
- You learned something that changed how you design systems
- Result was measurable

**"Describe a time you disagreed with a technical decision."**

*What they're testing:* Collaboration, communication, maturity

*Strong answer elements:*
- You had a valid technical reason (not preference)
- You raised it constructively with data/evidence, not just opinion
- You listened to the counter-argument genuinely
- You committed to the decision once made, even if you disagreed
- What happened (did your concern materialise? Were you wrong?)

*Weak answer:* "I raised it and they ignored me." → Shows you can't influence without authority.

**"Tell me about a production incident you led."**

*What they're testing:* Incident management, leadership under pressure, learning culture

*Strong answer elements:*
- Clear incident detection story (how did you know something was wrong?)
- Systematic diagnosis (what you checked and why, in order)
- Decisive mitigation (how you stopped the bleeding before fully understanding the cause)
- Post-mortem that found a systemic issue (not a person to blame)
- Concrete changes that were implemented

**"How have you influenced engineers more senior than you?"**

*What they're testing:* Leadership beyond authority, communication up

*Strong answer elements:*
- You built a case with data (prototype, benchmark, cost analysis)
- You understood their concerns and addressed them
- You found champions, not just opposition
- The outcome — what happened

**"Describe a situation where you had to make a decision with incomplete information."**

*What they're testing:* Decision-making under uncertainty

*Strong answer elements:*
- You were explicit about what you didn't know
- You identified what could be learned quickly vs required long investigation
- You made a reversible decision where possible
- You set a tripwire: "if X happens, we revisit"
- What happened (and whether you needed to revisit)

---

## 3. Kubernetes knowledge quiz

### ⭐ Medium

**Q1: What is the full sequence from `kubectl apply` to a pod running?**

1. `kubectl` serialises the manifest and sends HTTP PUT/PATCH to apiserver
2. apiserver: AuthN (bearer token / x509 / OIDC) → AuthZ (RBAC) → Mutating admission webhooks → Schema validation → Validating admission webhooks → Write to etcd
3. Scheduler watch fires (new pod with no `spec.nodeName`)
4. Scheduler runs filter → score → bind (writes `spec.nodeName`)
5. kubelet on target node watches for pods assigned to it
6. kubelet calls CRI (containerd) → image pull → create sandbox (pause container, shared net/IPC/UTS namespaces)
7. CNI called (ADD): allocates IP, creates veth, configures routes
8. kubelet calls CRI: create and start containers
9. kubelet runs startup probe (if configured), then readiness probe
10. kubelet reports `Running` + pod IP to apiserver → etcd

---

**Q2: What determines a pod's QoS class?**

- **Guaranteed:** Every container (including initContainers) has both CPU and memory request AND limit set, AND request == limit for each.
- **Burstable:** At least one container has a CPU or memory request or limit set, but doesn't meet Guaranteed.
- **BestEffort:** No containers have any resource requests or limits.

QoS affects eviction order (BestEffort evicted first) and OOM kill score (Guaranteed: -997, BestEffort: 1000).

---

**Q3: Explain the difference between a Deployment, StatefulSet, and DaemonSet.**

**Deployment:** For stateless workloads. Pods are fungible — any pod can serve any request. Rolling updates replace pods in any order. No stable network identity, no stable storage.

**StatefulSet:** For stateful workloads. Pods have stable names (`pod-0`, `pod-1`), stable DNS hostnames (`pod-0.service.ns.svc.cluster.local`), and stable PVCs (the PVC follows the pod through restarts). Pods are created sequentially (0, 1, 2...) and deleted in reverse. Used for: databases, Kafka, etcd, anything where instance identity matters.

**DaemonSet:** Ensures exactly one pod runs on each node (or subset of nodes). New nodes automatically get the pod; when a node is removed, the pod is GC'd. Used for: log collectors, node monitoring agents, CNI plugins, storage drivers.

---

**Q4: What is a PodDisruptionBudget and when does it NOT apply?**

A PDB defines the minimum number (or maximum number disrupted) of pods that must be available during **voluntary disruptions** — node drains (`kubectl drain`), rolling updates, cluster autoscaler scale-down.

A PDB is checked by the eviction API. If a pod eviction would violate the PDB, the eviction is blocked until the PDB can be satisfied.

**PDBs do NOT apply to:**
- Node crashes (involuntary disruption)
- OOMKills
- Direct pod deletes (bypassing eviction API)
- `kubectl delete pod --grace-period=0 --force`

---

**Q5: How does the Kubernetes scheduler handle a pod that can't schedule?**

If no node passes the filter phase: check if preemption is possible. If yes: mark pod with `nominatedNodeName`, evict lower-priority pods, wait for capacity to free. If preemption isn't possible or sufficient: pod stays Pending. The scheduler retries on cluster changes (new node added, resources freed).

`kubectl describe pod <name>` → Events section shows exact filter failure reason per node.

---

### ⭐⭐ Hard

**Q6: Why does CPU throttling happen even when node CPU utilisation is low?**

CPU limits are enforced by cgroups CFS (Completely Fair Scheduler) quotas. Each container gets a CPU quota: e.g., `500m` limit = `cpu.cfs_quota_us=50000` per `cpu.cfs_period_us=100000` (100ms period).

If the container uses its 50ms quota in the first 60ms of a 100ms window, it is frozen for the remaining 40ms — even if the node has spare CPU. The OS cannot give it more even though other CPUs are idle, because the cgroup quota is exhausted for this period.

This creates 100ms-periodic latency spikes at seemingly random times, even on nodes with 20% overall CPU utilisation.

Metric: `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])` — values > 0.25 are concerning.

---

**Q7: Explain etcd's MVCC and why compaction is necessary.**

etcd uses Multi-Version Concurrency Control: every write creates a new revision rather than updating in place. A global revision counter increments on every write. Old revisions are kept — this enables `etcdctl watch --rev=N` (watch from a specific point in history) and consistent snapshots.

Without compaction, revisions accumulate forever. The bbolt database file grows unboundedly. Compaction removes revisions older than a specified revision, marking the space as reclaimable. Defragmentation then rewrites the database file to actually reclaim the disk space (compaction doesn't shrink the file).

Production recommendation: `--auto-compaction-mode=periodic --auto-compaction-retention=1h`. This keeps 1 hour of watch history — enough for controllers to catch up after a restart.

---

**Q8: How does NetworkPolicy enforcement work at the kernel level?**

NetworkPolicy is a Kubernetes API object representing desired network policy. It does nothing by itself — the CNI plugin implements it.

With Calico (iptables mode):
1. Calico watches for NetworkPolicy and Endpoint objects
2. Generates iptables rules using cali- chain naming
3. Packet arriving at a pod's veth interface traverses `cali-INPUT` chain
4. Chain has rules matching source pod IP against policy
5. If no matching allow rule: packet dropped

With Cilium (eBPF mode):
1. Cilium watches for NetworkPolicy objects
2. Compiles them into eBPF programs attached to pod's veth TC (Traffic Control) hook
3. Each packet is evaluated against the eBPF program in O(1) via hash maps
4. Drop or allow decision made in-kernel before sk_buff processing

eBPF enforcement is faster (no chain traversal), supports L7 (HTTP method, path) policies, and scales to thousands of policies without degradation.

---

**Q9: What is the difference between a liveness probe, readiness probe, and startup probe? When does each fire?**

**Startup probe:** Fires first, during container startup. While it's running, liveness and readiness probes are disabled. Allows slow-starting containers without false liveness failures. Once it succeeds once, it stops running and liveness takes over.

**Liveness probe:** Fires continuously after startup. If it fails, kubelet kills and restarts the container. Use when the container can get into a broken state where the only recovery is a restart (deadlock, corrupted internal state).

**Readiness probe:** Fires continuously. If it fails, the pod is removed from Service endpoints (stops receiving traffic) but is NOT restarted. Use for: container is still starting, under temporary load, dependency is unavailable. A pod can be live but not ready.

---

**Q10: Explain how kube-proxy implements ClusterIP services in iptables mode.**

When a Service is created with ClusterIP `10.96.1.100` and port 80, kube-proxy creates:

1. In `KUBE-SERVICES` chain: a rule matching dst=10.96.1.100:80 → jump to `KUBE-SVC-<hash>`

2. In `KUBE-SVC-<hash>` chain: probabilistic rules selecting one of N endpoints:
   - Rule 1: `--probability 0.333 -j KUBE-SEP-<pod1-hash>` (33% → pod 1)
   - Rule 2: `--probability 0.5 -j KUBE-SEP-<pod2-hash>` (50% of remaining 67% = 33% → pod 2)
   - Rule 3: `-j KUBE-SEP-<pod3-hash>` (100% of remaining 33% = 33% → pod 3)

3. In `KUBE-SEP-<pod-hash>` chain: DNAT rule replacing dst=10.96.1.100:80 with real pod IP:port

The packet then continues routing with the real pod IP as destination. On the return path: SNAT if the pod is on a different node.

**Problems with this approach at scale:** Each new endpoint requires a rule change across all chains on all nodes. 1000 endpoints × 1000 pods = O(n²) iptables operations. This is why IPVS mode exists, and why Cilium bypasses iptables entirely.

---

### ⭐⭐⭐ Expert

**Q11: A pod is stuck in `Terminating` for 4 hours. What is happening and how do you fix it?**

The pod has a finalizer set in `metadata.finalizers`, and the controller responsible for removing that finalizer is not running (or is broken).

When you delete a pod, the apiserver sets `metadata.deletionTimestamp`. The object is NOT deleted from etcd. Containers continue running normally (the pod is still live). It will remain in etcd — and show as `Terminating` — until all finalizers are removed.

**Diagnosis:**
```bash
kubectl get pod <name> -o jsonpath='{.metadata.finalizers}'
kubectl get pod <name> -o jsonpath='{.metadata.deletionTimestamp}'
# Check which controller should be managing this finalizer
```

**Fix options:**
1. Restart the controller that manages the finalizer (preferred — correct approach)
2. Force-remove the finalizer: `kubectl patch pod <name> -p '{"metadata":{"finalizers":[]}}' --type=merge`
   → This bypasses cleanup — only do this if you understand the consequences
3. `kubectl delete pod <name> --grace-period=0 --force`
   → Does NOT help with finalizers; this only bypasses the graceful termination grace period

---

**Q12: Walk through exactly what happens when a Deployment rolling update is triggered.**

```
1. User updates Deployment spec (e.g., new image tag)
2. Deployment controller detects change (generation incremented)
3. Controller creates a new ReplicaSet with new pod template
4. Controller begins scaling: new RS +1 pod, old RS -1 pod
   (respecting maxSurge and maxUnavailable constraints)

5. For each new pod:
   a. Pod created in new RS (PodPending)
   b. Scheduler assigns node
   c. kubelet starts container
   d. Startup probe (if configured): if fails, pod restarts
   e. Readiness probe: pod added to Endpoints only when ready
   f. Only after pod is Ready does controller proceed with next step
      (respecting minReadySeconds — pod must be Ready for this long)

6. As new pods become Ready: old RS scaled down
   Controller verifies: old_unavailable ≤ maxUnavailable, total ≤ desired + maxSurge

7. When new RS reaches desired replicas and all old RS pods are deleted: rollout complete
   Deployment status: availableReplicas == desiredReplicas

8. Old RS remains (with 0 replicas) for rollback history
   (revisionHistoryLimit controls how many are kept)
```

**Common failure scenarios:**
- New pod never becomes Ready → rollout hangs → readiness probe not configured correctly or app has bug
- Rollout too slow → increase maxSurge, decrease minReadySeconds
- Old pods not terminating → preStop hook taking too long, or terminationGracePeriodSeconds too short

---

**Q13: Your cluster has 50 nodes and 5,000 pods. A developer reports intermittent 5xx errors on their service, 0.1% error rate, no recent deployments. Walk through your diagnosis.**

**Step 1: Correlate with time.**
```bash
# Is this constant or bursty?
rate(http_requests_total{status=~"5.."}[5m]) vs rate(http_requests_total[5m])
# If bursty: correlate with HPA scaling events, cron jobs, garbage collection
```

**Step 2: Isolate the affected pods/nodes.**
```bash
# Are errors concentrated on specific pods?
sum by(pod) (rate(http_requests_total{status=~"5.."}[5m]))
# If yes: that pod has a local issue (resource pressure, disk, network)
```

**Step 3: Check resource pressure.**
```bash
# CPU throttling on affected pods?
rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])
# Memory pressure?
container_memory_working_set_bytes / kube_pod_container_resource_limits{resource="memory"}
```

**Step 4: Check node-level issues.**
```bash
# conntrack table full? (common at scale, causes new connections to fail)
node_nf_conntrack_entries / node_nf_conntrack_entries_limit
# > 0.8 is dangerous; > 0.9 causes packet drops

# DNS pressure (slow DNS = intermittent failures on new connections)?
histogram_quantile(0.99, rate(coredns_dns_request_duration_seconds_bucket[5m]))
# > 1s is very slow

# kube-proxy iptables not synced?
kubeproxy_sync_proxy_rules_duration_seconds_bucket
```

**Step 5: Check readiness probe timing.**
```bash
# Pods cycling in/out of Ready state?
kube_pod_container_status_ready{namespace="<ns>", pod=~"<service>.*"}
# Drops to 0 intermittently = readiness probe timing issue
```

**Step 6: Check Endpoints.**
```bash
kubectl get endpoints <service> -n <ns> -w
# If endpoints flap (pods added/removed rapidly): readiness probe is too sensitive
```

**Step 7: Distributed tracing.**
```
If you have OTel: look for slow spans coinciding with 5xx windows
This would identify if the error is in this service or a downstream dependency
```

---

## 4. Common senior-level gaps

**Gap 1: Tool knowledge without theory**

*Symptom:* "I use Cilium" but can't explain why eBPF is faster than iptables. "I use etcd" but don't know what MVCC means.

*Test:* For every tool you list on your CV, be able to answer: "What problem does this solve? What's the mechanism? What are its failure modes?"

---

**Gap 2: Operating vs designing**

*Symptom:* Can fix a broken system but can't design a new one from first principles. Knows what to do but not why that's the right choice.

*Fix:* For every system you've operated, write a design document as if you were designing it from scratch. What requirements did it actually meet? What tradeoffs were made? What would you change?

---

**Gap 3: Jumping to solutions**

*Symptom:* "Design a message queue" → immediately starts describing Kafka internals. Never asked about scale, latency, team size, or existing stack.

*Fix:* Practice spending the first 5 minutes of every design problem asking clarifying questions. Time yourself. Stop answering until you've asked at least 5 questions.

---

**Gap 4: Not quantifying impact**

*Symptom:* "I improved the system" not "p99 latency dropped from 450ms to 12ms, measured over 30 days."

*Fix:* Every achievement should have: what changed, how it was measured, what the result was in numbers. If you didn't measure it at the time, go back and think about how you would measure it.

---

**Gap 5: Shallow knowledge at the edges of your experience**

*Symptom:* Deep on what you've directly operated, shallow on things adjacent. Can't answer follow-up questions on items listed on CV.

*Fix:* For each bullet on your CV, prepare to go 3 levels deeper: (1) what you did, (2) why you chose that approach vs alternatives, (3) what the mechanism is under the hood.

---

**Gap 6: Not saying "I don't know"**

*Symptom:* Confidently answering questions you don't actually know, with plausible-sounding (but wrong) answers.

*Fix:* "I'm not certain about X, but my reasoning would be..." is a strong senior engineer answer. Confident wrong answers disqualify. Honest uncertainty with sound reasoning impresses.

---

## 4-week preparation schedule

### Week 1 — Foundation
- Day 1-2: Linux internals (Phase 1 §1): namespaces, cgroups, syscalls — do the hands-on exercises
- Day 3-4: Networking (Phase 1 §2): TCP state machine, eBPF, CNI — capture DNS traffic, trace a packet
- Day 5-7: Distributed systems (Phase 1 §3+4): implement Raft pseudocode on paper, explain MVCC

### Week 2 — Kubernetes depth
- Day 1-2: Scheduler + Operators (Phase 2 §2+3): write a controller, understand the admission flow
- Day 3-4: K8s Networking (Phase 2 §4): reproduce ndots issue, trace iptables rules for a Service
- Day 5-7: etcd (Phase 2 §6): compact + defrag a test cluster, measure recovery from leader failure

### Week 3 — Observability + Architecture
- Day 1-2: Prometheus TSDB (Phase 3 §2): diagnose cardinality, write a remote_write config
- Day 3: OTel tracing (Phase 3 §3): instrument a two-service app, verify trace continuity
- Day 4-5: SLO engineering (Phase 3 §4): write multi-window burn rate alerts for a real service
- Day 6-7: Architecture (Phase 4): design a multi-region system on paper, justify each decision

### Week 4 — Interview simulation
- Day 1: Write 5 STAR stories. Time them at < 3 minutes each.
- Day 2-3: Full mock system design (45 minutes): record yourself, review pacing and framework usage
- Day 4: Knowledge quiz (Phase 5 §3): all 13 questions, no notes, then review gaps
- Day 5-6: Second mock interview. Focus on weakest area from Day 4 review.
- Day 7: Light review only. Trust the preparation.
