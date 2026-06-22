# Phase 3 — Observability & SRE
## Prometheus TSDB, OpenTelemetry, SLO Engineering, Incident Management

> The difference between monitoring and observability: monitoring tells you when something is broken; observability lets you understand why — even for problems you didn't predict.

<div class="topic-legend">
<span><span class="swatch" style="background:#6aa6ff"></span>Core concept</span>
<span><span class="swatch" style="background:#e8b84e"></span>Interview hot topic</span>
<span><span class="swatch" style="background:#b18cff"></span>Architecture depth</span>
<span><span class="swatch" style="background:#e87a4e"></span>Gap to close</span>
<span><span class="swatch" style="background:#4ee8a0"></span>Hands-on practice</span>
</div>

<div class="topic-grid">
<a class="topic-card" href="#the-three-pillars-and-why-theyre-incomplete">
<h4>The three pillars</h4>
<div class="tags"><span class="cat cat-core">Core concept</span></div>
</a>
<a class="topic-card" href="#prometheus-tsdb-internals">
<h4>Prometheus TSDB internals</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-gap">Gap to close</span></div>
</a>
<a class="topic-card" href="#opentelemetry-and-distributed-tracing">
<h4>OpenTelemetry &amp; tracing</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-practice">Hands-on practice</span></div>
</a>
<a class="topic-card" href="#slo-engineering">
<h4>SLO engineering</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#incident-management">
<h4>Incident management</h4>
<div class="tags"><span class="cat cat-practice">Hands-on practice</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
</div>

---

## Learning objectives

- Diagnose and fix a Prometheus cardinality explosion in production without data loss
- Design and implement a full distributed tracing pipeline from instrumentation to backend
- Define SLOs with proper error budgets and multi-window burn rate alerts
- Run blameless post-mortems that produce systemic improvements, not blame

**Estimated study time:** 3–4 days

---

## 1. The three pillars and why they're incomplete

The "three pillars" framing (metrics, logs, traces) is useful but misleading — it suggests they're alternatives. They're complements that answer different questions:

| Signal | Question answered | Cardinality | Cost |
|--------|-----------------|-------------|------|
| Metrics | What is happening? At what rate? | Low | Low |
| Logs | What happened for this specific event? | High | Medium |
| Traces | Where in the call graph did this request slow down? | Medium | Medium |
| Profiles | Which line of code is consuming this CPU/memory? | Very high | High |

**The observability gap:** A system can have excellent metrics (you know the error rate is 1%) and excellent logs (you have every error message) but still be unable to answer "which microservice in this request path is slow for requests from users in Germany?" without traces.

---

## 2. Prometheus TSDB internals

### 2.1 The data model

Every time series is uniquely identified by a metric name and a set of labels:

```
http_requests_total{method="GET", handler="/api/v1/orders", status="200", instance="10.0.0.1:8080"}
```

This is one time series. Prometheus stores `(timestamp int64, value float64)` pairs per series. The timestamp is milliseconds since epoch. The value is IEEE 754 double-precision float.

**Cardinality** = the number of unique time series. Each unique combination of label values creates a new series.

```bash
# Current series count
prometheus_tsdb_head_series

# Top cardinality contributors
topk(20, count by(__name__)({__name__=~".+"}))

# Inspect label value cardinality for a specific metric
count(count by(pod)(http_requests_total))   # How many unique pods?
count(count by(user_id)(api_calls_total))   # Should be 0 if user_id is a label

# TSDB head statistics
curl -s http://localhost:9090/api/v1/status/tsdb | jq '.data.headStats'
# numSeries, chunkCount, memoryChunksBytes
```

**Memory consumption formula (rough):**

```
memory ≈ active_series × (bytes_per_series)
bytes_per_series ≈ 3000 bytes (chunks + series metadata + index structures)

1M series × 3000B = ~3GB RAM (just for current data, before WAL overhead)
10M series ≈ 30GB RAM
```

High-cardinality anti-patterns to avoid:

```
# NEVER use as label values:
- User IDs, session IDs, request IDs, trace IDs  (unbounded)
- IP addresses (potentially unbounded)
- URL paths with variable segments (/users/12345/orders → /users/<id>/orders)
- Version strings with frequent releases
- Full error messages (use error codes instead)
```

### 2.2 TSDB block structure

**WAL (Write-Ahead Log):**

Every incoming sample is written to the WAL immediately. The WAL is a sequence of 128MB segment files, fsync-ed on write. Its purpose: if Prometheus crashes, the WAL provides the data to reconstruct the in-memory state.

```
/prometheus/wal/
├── 00000001   (128MB segment)
├── 00000002
└── checkpoint.00000003/
    └── 00000000   (checkpoint file — condensed WAL state)
```

```bash
# WAL health
ls -lh /prometheus/wal/

# Prometheus exposes WAL corruption detection
prometheus_tsdb_wal_corruptions_total  # Should be 0

# WAL write latency (critical: if high, Prometheus ingestion will lag)
histogram_quantile(0.99, rate(prometheus_tsdb_wal_fsync_duration_seconds_bucket[5m]))
# > 100ms is concerning
```

**Head block (in-memory):**

The head block is the current "hot" data — the last 2 hours (configurable via `--storage.tsdb.min-block-duration`). It lives entirely in memory as compressed chunks. Each chunk holds up to 120 samples per series. The head block is written periodically to disk as an immutable block.

**Persistent blocks:**

```
/prometheus/
├── 01EYFCW7JVBZ2K2R8N3FJZM6R5/   2-hour block
│   ├── chunks/
│   │   └── 000001              compressed chunk data (XOR-encoded floats)
│   ├── index                   inverted index: label → series IDs → chunk offsets
│   ├── meta.json              {minTime, maxTime, stats: {numSamples, numSeries, numChunks}}
│   └── tombstones             deleted series (soft delete, reclaimed at compaction)
├── 01EYFCX7JVBZ2K2R8N3FJZM6R7/   6-hour block (compacted from 3×2h)
└── 01EYFCY7JVBZ2K2R8N3FJZM6R9/   24-hour block
```

**Compaction schedule:**

```
2h blocks → compact to → 6h block (when 3 × 2h blocks exist)
6h blocks → compact to → 24h block
24h blocks → compact to → 48h block (for long retention)
```

```bash
# Inspect blocks
ls -la /prometheus/
for dir in /prometheus/*/; do
  [ -f "$dir/meta.json" ] && cat "$dir/meta.json" | jq '{dir: "'$dir'", min: .minTime, max: .maxTime, series: .stats.numSeries}'
done

# TSDB analyzer (official tool)
./tsdb analyze /prometheus
# Shows: highest cardinality labels, largest series, chunk stats

# Block compaction state
prometheus_tsdb_compactions_total
prometheus_tsdb_compaction_duration_seconds_sum
```

### 2.3 Chunk encoding — XOR compression

Prometheus uses Gorilla-style XOR compression for float64 time series values, and delta-of-delta encoding for timestamps. This achieves ~1.37 bytes per sample (vs 16 bytes raw).

**How XOR works:**

```
Values: 1000.0, 1001.2, 1002.5, 1001.8, ...

First value: stored as full float64 (8 bytes)
Subsequent: XOR with previous float64 → only differing bits stored
  - If value is identical: 1 bit (0)
  - If value changes slightly: few bits for the changed exponent/mantissa bits
  - Significant changes: more bits

Why it's effective: time series values often change slowly.
CPU metrics going from 23.4% to 23.6% — only the last few bits differ.
```

**Timestamp delta-of-delta:**

```
Timestamps: 0, 15000, 30000, 45000, ... (15s scrape interval, ms)
Deltas: 15000, 15000, 15000, ...
Delta-of-deltas: 0, 0, 0, ... → stored as a single bit per sample
```

Regular scrape intervals compress to almost nothing. Irregular intervals (failed scrapes, restarts) cost more bits.

### 2.4 Remote write in depth

```
Prometheus scrapes metrics → head block (in-memory)
                                    ↓
                        Remote Write queue (per shard)
                                    ↓
                        WAL read (reads WAL segments, not head block)
                                    ↓
                        HTTP POST to remote endpoint (protobuf, snappy compressed)
                                    ↓
                        Remote storage (Thanos Receive, Cortex, Mimir, VictoriaMetrics)
```

**Why the WAL, not the head block?** The WAL is append-only and durable. Remote write reads from the WAL, tracking its position (wal_last_read_position). If the remote endpoint is slow, the WAL queue fills. If the WAL is purged before remote write catches up, data is lost.

```yaml
remote_write:
- url: "http://thanos-receive:19291/api/v1/receive"
  remote_timeout: 30s
  queue_config:
    capacity: 50000           # Max samples in queue per shard
    max_shards: 200           # Parallel writers (increase for high throughput)
    min_shards: 1
    max_samples_per_send: 10000
    batch_send_deadline: 5s
    min_backoff: 30ms
    max_backoff: 5s
    retry_on_http_429: true
  write_relabel_configs:      # Filter before sending
  - source_labels: [__name__]
    regex: '(up|scrape_duration_seconds|http_requests_total|node_.*|container_.*)'
    action: keep
```

```bash
# Monitor remote write health
prometheus_remote_storage_samples_pending       # Should be low
prometheus_remote_storage_samples_failed_total  # Should be 0
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
  # Compare with time() — if gap > 1min, remote write is lagging

# Shards auto-scaling metrics
prometheus_remote_storage_shards               # Current shard count
prometheus_remote_storage_shards_desired       # What Prometheus wants

# Remote write latency
histogram_quantile(0.99, rate(prometheus_remote_storage_sent_batch_duration_seconds_bucket[5m]))
```

---

## 3. OpenTelemetry and distributed tracing

### 3.1 Why tracing exists — the problems it solves

**Waterfall diagrams:** A trace shows you a timeline of a single request's journey through your system:

```
[──────────────── api-gateway: handle /checkout ─────────────────────]  450ms
   [── auth-service: validate-token ──]  80ms
                 [── inventory-service: check-availability ───────]  310ms (slow!)
                         [── db-query: SELECT FROM inventory ──]  290ms
                 [── payment-service: charge ──────]  120ms
```

Without traces, you'd see from Prometheus that `/checkout` p99 is 450ms. But you wouldn't know inventory-service is the bottleneck, and you wouldn't know it's a specific database query.

### 3.2 The OTel data model

**Span:**

```
Span {
  trace_id: 4bf92f3577b34da6a3ce929d0e0e4736  (128 bits, shared across entire trace)
  span_id:  00f067aa0ba902b7                   (64 bits, unique to this span)
  parent_span_id: <null for root>
  name: "http.server /checkout"
  kind: SERVER | CLIENT | PRODUCER | CONSUMER | INTERNAL
  start_time: 2024-06-15T10:30:00.000Z
  end_time:   2024-06-15T10:30:00.450Z
  status: OK | ERROR | UNSET
  attributes: {
    "http.method": "POST",
    "http.url": "https://api.example.com/checkout",
    "http.status_code": 200,
    "user.id": "u123",    # Be careful: this creates cardinality in trace storage
  }
  events: [
    {time: 10:30:00.100, name: "payment.started", attributes: {"amount": 49.99}}
    {time: 10:30:00.220, name: "payment.completed"}
  ]
  links: []   # References to spans in other traces (for batch processing etc.)
}
```

**Semantic conventions:** OTel defines standard attribute names for common operations:

```
HTTP:     http.method, http.url, http.status_code, http.route
Database: db.system, db.statement, db.operation, db.name
Messaging: messaging.system, messaging.destination, messaging.operation
RPC:      rpc.system, rpc.method, rpc.service
```

Using semantic conventions means your traces are compatible with standard dashboards and analysis tools.

### 3.3 Context propagation — the mechanism

Context propagation is the mechanism that lets spans across service boundaries be linked into a trace tree.

**W3C TraceContext (the standard):**

```http
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             ^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^  ^^
             |   trace-id (32 hex chars)            span-id           flags
             version (always "00")                  (16 hex chars)    01=sampled
```

The injecting service adds this header to outgoing requests. The receiving service extracts it and makes the trace_id/span_id the parent of its new span.

```go
// Instrumentation pattern in Go
import (
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/propagation"
    "go.opentelemetry.io/otel/trace"
)

var tracer = otel.Tracer("inventory-service")

// HTTP Handler: extract incoming context
func handleCheckAvailability(w http.ResponseWriter, r *http.Request) {
    // Extract: creates a context with the incoming trace context
    ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))

    // Start a child span under the extracted context
    ctx, span := tracer.Start(ctx, "check-availability",
        trace.WithAttributes(
            attribute.String("item.id", r.URL.Query().Get("id")),
        ),
    )
    defer span.End()

    // Call downstream: inject context into outgoing request
    req, _ := http.NewRequestWithContext(ctx, "GET", "http://db:5432/...", nil)
    otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
    // req now carries traceparent header linking this span to the tree

    // Record an error
    if err := dbCall(ctx); err != nil {
        span.RecordError(err)
        span.SetStatus(codes.Error, err.Error())
        http.Error(w, "internal error", 500)
        return
    }
}
```

```python
# Python equivalent
from opentelemetry import trace, propagate
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

tracer = trace.get_tracer("payment-service")

def handle_payment(request):
    # Extract context from incoming request headers
    ctx = propagate.extract(dict(request.headers))

    with tracer.start_as_current_span("process-payment", context=ctx) as span:
        span.set_attribute("payment.amount", request.json["amount"])
        span.set_attribute("payment.currency", request.json["currency"])

        try:
            result = charge_card(request.json)
            span.add_event("payment.charged", {"transaction_id": result.id})
            return result
        except PaymentDeclined as e:
            span.set_status(StatusCode.ERROR, str(e))
            span.record_exception(e)
            raise
```

### 3.4 Sampling strategies — in depth

At 10,000 RPS, storing every trace is: 10,000 traces/s × 86,400 s/day = 864M traces/day. At 1KB/trace = 864GB/day. Sampling is mandatory.

**Head-based sampling — decided at the root span:**

```
Pros: Simple, deterministic (same trace_id always gets same decision),
      no buffering required
Cons: Decision made before you know if the trace is interesting
      (can't guarantee 100% of errors are captured)

Implementations:
  - Always: capture everything (dev/staging only)
  - Never: capture nothing (useful for high-traffic services)
  - Ratio: capture X% of traces (e.g., 1%)
  - ParentBased: respect the sampling decision from the parent span
```

**Tail-based sampling — decided after the trace is complete:**

```
Pros: Can always capture 100% of errors and slow traces
Cons: Requires buffering entire traces (memory intensive),
      must wait for all spans to arrive (typically 10-30s delay)

Requires: OTel Collector with tail_sampling processor,
          or Tempo/Jaeger with dedicated sampling service
```

```yaml
# OTel Collector tail_sampling config
processors:
  tail_sampling:
    decision_wait: 30s        # Wait for all spans before deciding
    num_traces: 100000        # Hold this many traces in memory
    policies:
    - name: always-sample-errors
      type: status_code
      status_code:
        status_codes: [ERROR]
    - name: always-sample-slow
      type: latency
      latency:
        threshold_ms: 1000    # Always keep traces > 1 second
    - name: sample-http-500
      type: string_attribute
      string_attribute:
        key: http.status_code
        values: ["500", "502", "503", "504"]
    - name: probabilistic-baseline
      type: probabilistic
      probabilistic:
        sampling_percentage: 1  # Keep 1% of everything else
```

### 3.5 OTel Collector — the hub

The Collector decouples instrumentation from backends. Your services send to the Collector; the Collector fans out to whatever backends you use.

```yaml
# Complete collector config
receivers:
  otlp:
    protocols:
      grpc: {endpoint: "0.0.0.0:4317"}
      http: {endpoint: "0.0.0.0:4318"}
  prometheus:                          # Scrape existing Prometheus endpoints
    config:
      scrape_configs:
      - job_name: 'existing-service'
        static_configs:
        - targets: ['service:8080']

processors:
  batch:
    send_batch_size: 1024
    timeout: 5s
  memory_limiter:
    check_interval: 1s
    limit_mib: 1024
    spike_limit_mib: 256
  resource:                            # Add cluster-level attributes
    attributes:
    - key: deployment.environment
      value: production
      action: upsert
    - key: cloud.region
      from_attribute: REGION
      action: insert
  attributes:                          # Remove sensitive data
    actions:
    - key: user.password
      action: delete
    - key: http.request.header.authorization
      action: delete

exporters:
  otlp/tempo:
    endpoint: "tempo.monitoring:4317"
    tls: {insecure: true}
  prometheusremotewrite:
    endpoint: "http://prometheus:9090/api/v1/write"
  logging:
    loglevel: warn

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch, resource, attributes]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp, prometheus]
      processors: [batch]
      exporters: [prometheusremotewrite]
```

---

## 4. SLO engineering

### 4.1 SLI → SLO → SLA — precise definitions

**SLI (Service Level Indicator):** A quantitative measurement of a service behaviour that matters to users.

Good SLI criteria:
- Measurable from data you already have (or can cheaply instrument)
- Correlated with actual user experience
- Aggregatable (percentiles, rates, ratios — not raw values)

```
Strong SLIs:
- (successful_requests / total_requests) — availability
- percentile(request_duration, 0.99) — latency
- (valid_data_points / total_data_points) — data correctness
- (requests_within_latency_threshold / total_requests) — latency ratio SLI

Weak SLIs:
- CPU utilisation (not correlated with user experience)
- "System is healthy" (not measurable)
- Mean latency (hides tail; p99 or p999 is better)
```

**SLO (Service Level Objective):** A target value for an SLI, over a time window.

```
Format: [SLI] should be [operator] [value] over [window]

Examples:
- 99.9% of HTTP requests return 2xx or 3xx, measured over a rolling 30-day window
- 95% of API requests complete in < 200ms, measured over 28 days
- 99.99% of messages are delivered at least once within 60 seconds
```

**SLA (Service Level Agreement):** A contractual commitment, usually with financial consequences for breach. The SLA should be lower than your SLO — the gap is your safety margin.

### 4.2 Error budget — the key insight

The error budget converts an abstract SLO into a concrete quantity you can spend.

```
SLO: 99.9% availability over 30 days
Error budget fraction: 1 - 0.999 = 0.001

For a service receiving 1M requests/day (30M/month):
  Error budget in requests: 30M × 0.001 = 30,000 failed requests
  Error budget in time: 30 days × 24h × 60m × 0.001 = 43.2 minutes

If you currently have 10,000 failures: 33% of budget consumed
If you have 30,001 failures: SLO breached
```

**Error budget as a policy tool:**

```
Error budget remaining > 50%:
  → Deploy freely, run experiments, accept more risk
  → Invest in feature velocity

Error budget < 20%:
  → Slow down deployments, increase testing requirements
  → Focus on reliability improvements

Error budget exhausted:
  → Feature freeze
  → All engineering effort on reliability
  → No new deployments until budget recovers
```

### 4.3 Multi-window, multi-burn-rate alerting

This is the Google SRE Workbook method. Simple threshold alerts are inadequate:

- Alert on 1% error rate: fires for a 1-second spike consuming 0.0001% of budget
- Alert on 1% error rate for 5 minutes: misses a 0.5% error rate that drains budget slowly

**Burn rate** = (current error rate) / (error budget fraction)

- Burn rate 1 = consuming budget at exactly the replenishment rate
- Burn rate 14.4 = budget exhausted in 30 days / 14.4 = ~50 hours
- Burn rate 36 = budget exhausted in 30 days / 36 = ~20 hours

**Alert matrix (for 99.9% SLO = 0.1% error budget):**

| Severity | Burn rate | Alert window | Budget consumed | Action |
|----------|-----------|-------------|----------------|--------|
| Page | 14.4× | 1h | 2% in 1h | Immediate response |
| Page | 6× | 6h | 5% in 6h | Urgent response |
| Ticket | 3× | 1d | 10% in 1d | Fix within 1 day |
| Ticket | 1× | 3d | 10% in 3d | Fix within 3 days |

```yaml
# Prometheus alerting rules — 99.9% SLO (error_budget = 0.001)
groups:
- name: slo_alerts
  rules:

  # Recording rules (compute SLI over windows)
  - record: job:http_error_ratio:rate1h
    expr: |
      sum(rate(http_requests_total{status=~"5.."}[1h]))
      / sum(rate(http_requests_total[1h]))

  - record: job:http_error_ratio:rate6h
    expr: |
      sum(rate(http_requests_total{status=~"5.."}[6h]))
      / sum(rate(http_requests_total[6h]))

  # Page alert: fast burn (14.4× in 1h window, confirmed by 5m window)
  - alert: SLOBurnRateFast
    expr: |
      job:http_error_ratio:rate1h > (14.4 * 0.001)
      and
      job:http_error_ratio:rate5m > (14.4 * 0.001)
    for: 2m
    labels:
      severity: page
    annotations:
      summary: "High SLO burn rate — page immediately"
      description: |
        Error rate {{ $value | humanizePercentage }} is burning error budget at 14.4×.
        At this rate, 30-day budget exhausted in ~50 hours.

  # Ticket alert: slow burn (6× in 6h window)
  - alert: SLOBurnRateSlow
    expr: |
      job:http_error_ratio:rate6h > (6 * 0.001)
      and
      job:http_error_ratio:rate30m > (6 * 0.001)
    for: 15m
    labels:
      severity: ticket
    annotations:
      summary: "Elevated SLO burn rate — create ticket"
```

**PromQL for error budget dashboard:**

```promql
# Remaining error budget (as fraction)
1 - (
  sum(increase(http_requests_total{status=~"5.."}[30d]))
  /
  sum(increase(http_requests_total[30d]))
) / 0.001

# Error budget burn rate (current)
(
  sum(rate(http_requests_total{status=~"5.."}[1h]))
  / sum(rate(http_requests_total[1h]))
) / 0.001

# Time until budget exhausted at current burn rate
(
  1 - (
    sum(increase(http_requests_total{status=~"5.."}[30d]))
    / sum(increase(http_requests_total[30d]))
  ) / 0.001
) / (
  (
    sum(rate(http_requests_total{status=~"5.."}[1h]))
    / sum(rate(http_requests_total[1h]))
  ) / 0.001
) * 720  # hours in 30 days
```

---

## 5. Incident management

### 5.1 Incident lifecycle — systematic response

```
Detection
  - Alerting fires (Alertmanager → PagerDuty → phone)
  - User reports (support ticket, social media, status page subscriber)
  - Proactive monitoring (synthetic checks, canaries)
    ↓
Triage (< 5 minutes)
  - Acknowledge alert
  - Assess severity (how many users? which regions? what's broken?)
  - Declare incident severity level
  - Page additional responders if needed
    ↓
Communication
  - Open incident channel (#incident-YYYY-MM-DD-service)
  - Assign incident commander (IC) — owns process, not investigation
  - Assign communication lead (external status updates)
  - Post to status page if customer-visible
    ↓
Mitigation (stop the bleeding)
  - Rollback deployment if recent change
  - Reroute traffic away from affected component
  - Scale up healthy instances
  - Enable feature flag to disable affected feature
  - Do NOT deep-dive root cause yet — mitigate first
    ↓
Resolution
  - Confirm metrics have normalised
  - Update status page
  - Debrief in incident channel
    ↓
Post-mortem
  - Within 5 business days
  - Blameless analysis
  - Action items with owners and due dates
```

### 5.2 Blameless post-mortems

**The key principle:** Systems fail, not people. Individual mistakes are symptoms of systemic conditions. A person made a "mistake" because the system allowed them to, didn't prevent them from doing so, or provided misleading information.

**Five Whys — used correctly:**

```
Symptom: Users couldn't check out for 23 minutes

Why? → Payment service was returning 503
Why? → Payment service pods were in CrashLoopBackOff
Why? → OOMKill: memory limit was 256Mi
Why? → Memory limit set in Helm values was copied from staging without adjustment
Why? → There is no process to validate resource limits before production deployment

Root cause: Missing validation process in deployment workflow
(Not: "engineer made a mistake copying the value")
```

**Effective post-mortem structure:**

```markdown
## [Service] incident [date] — [brief description]

**Severity:** SEV2
**Duration:** 14:32–14:55 UTC (23 minutes)
**Impact:** All users in EU region unable to complete checkout

## Timeline
[All times UTC]
- 14:32 — Alert fires: `PaymentService5xxRate > 5%` (page sent)
- 14:34 — On-call acknowledges. Checks dashboards: 503 rate 100%.
- 14:37 — Finds payment pods in CrashLoopBackOff
- 14:39 — Identifies OOMKill in events: `Killed process 1 (payment-svc) due to OOM`
- 14:41 — Memory limit raised from 256Mi to 1Gi. Pod restarts.
- 14:43 — Pod healthy, error rate drops to 0%
- 14:55 — Monitoring normalised. Incident resolved.

## Root cause
A PR that tuned memory settings for the staging environment was merged without
adjusting the production override. The deployment pipeline doesn't validate that
production resource limits match observed memory usage before deployment.

## Contributing factors
- No Helm diff review step that highlights resource changes
- No memory trending alert to catch gradual growth before the limit was hit
- No runbook for OOMKill diagnosis (added time to identify root cause)

## What went well
- Alert fired within 2 minutes of the first OOMKill
- On-call identified root cause within 5 minutes
- Fix was fast (limit increase, pod restart)

## What went wrong
- No alert for "memory limit < memory usage trend" — silent drift
- Took 7 minutes from alert to identifying OOMKill (runbook gap)

## Action items
| Item | Owner | Priority | Due |
|------|-------|----------|-----|
| Add alert: memory.current > 80% of memory.limit for 5m | @platform | P1 | 2024-07-01 |
| Add Helm diff to deployment pipeline highlighting resource changes | @platform | P2 | 2024-07-15 |
| Write OOMKill diagnosis runbook | @oncall | P2 | 2024-07-08 |
| Add VPA recommendation reports to weekly operations review | @team | P3 | 2024-07-22 |
```

### 5.3 Chaos engineering

**Principles:**

1. Define a steady-state hypothesis ("p99 latency will be < 200ms with error rate < 0.1%")
2. Vary one thing at a time
3. Start with small blast radius (staging → small prod canary → full prod)
4. Have an abort condition (rollback plan before you start)
5. Run during business hours with full team available

```yaml
# chaos-mesh: pod failure experiment
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: api-pod-failure
  namespace: chaos-testing
spec:
  action: pod-kill
  mode: random-max-percent
  value: "20"          # Kill up to 20% of matching pods
  selector:
    namespaces: [production]
    labelSelectors:
      app: api
  scheduler:
    cron: "@every 10m"

---
# Network partition: introduce 200ms latency to DB traffic
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: db-latency
spec:
  action: delay
  mode: all
  selector:
    namespaces: [production]
    labelSelectors:
      app: api
  delay:
    latency: "200ms"
    correlation: "50"
    jitter: "100ms"
  direction: to
  target:
    selector:
      namespaces: [production]
      labelSelectors:
        app: postgres
    mode: all
  duration: "5m"

---
# Memory pressure: fill up container memory
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: memory-stress
spec:
  mode: one
  selector:
    namespaces: [staging]
    labelSelectors:
      app: api
  stressors:
    memory:
      workers: 2
      size: "256MB"
  duration: "10m"
```

```bash
# Observe during chaos experiment
watch -n1 'kubectl get pods -n production | grep -v Running | grep -v Completed'
# Watch SLO burn rate on dashboard
# Watch error rate in Grafana

# After experiment:
kubectl delete podchaos api-pod-failure -n chaos-testing

# Export results for post-experiment review
chaos-mesh-dashboard export-experiment api-pod-failure
```

---

## Common misconceptions

| Misconception | Reality |
|---------------|---------|
| "We need 100% uptime" | 100% SLO means no deployments ever. 99.9% = 43 min/month of budget. |
| "Average latency is the right SLI" | p99 or p99.9 matters. Averages hide the long tail that drives user complaints. |
| "More labels = better metrics" | More labels = more cardinality = more memory = slower queries. Label sparingly. |
| "Traces replace logs" | No. Traces show the flow between services. Logs show what happened within one service. You need both. |
| "Post-mortems find the person who caused the incident" | Blameless post-mortems find system conditions that allowed the incident to happen. |
| "Chaos engineering breaks things" | Chaos engineering reveals failures that already exist in the system before users find them. |

---

## Hands-on exercises

1. Find the highest-cardinality metric in a Prometheus instance. Trace it back to the label causing the explosion. Write a `metric_relabel_config` to fix it without dropping the metric.
2. Instrument a multi-service application with OTel (two services, one calling the other). Verify trace continuity (same trace_id) in Jaeger or Tempo. Add a custom span event for a business event.
3. Calculate the error budget for a 99.95% SLO over 28 days, with 500k requests/day. Write the PromQL burn rate query. Set up the fast-burn page alert.
4. Write a complete blameless post-mortem for a real or hypothetical incident. Include a 5-Whys chain that leads to a systemic root cause.
5. Run a chaos-mesh pod failure experiment. Monitor your SLO burn rate during the experiment. Verify that your service degrades gracefully (circuit breakers, retries work).

---

## What to study next → [Phase 4 — Architecture & Design](./phase4-architecture-design.md)

Phase 4 moves from building and operating systems to designing them — multi-region high availability, platform engineering, FinOps, and zero-trust security architecture.
