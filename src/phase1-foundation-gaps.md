# Phase 1 — Foundation Gaps
## Linux Internals, Networking, Distributed Systems, Storage

> Deep technical reference for senior cloud engineers. No fluff, no resume anchoring — just the knowledge itself at the depth that matters in both production and interviews.

<div class="topic-legend">
<span><span class="swatch" style="background:#6aa6ff"></span>Core concept</span>
<span><span class="swatch" style="background:#e8b84e"></span>Interview hot topic</span>
<span><span class="swatch" style="background:#b18cff"></span>Architecture depth</span>
<span><span class="swatch" style="background:#e87a4e"></span>Gap to close</span>
<span><span class="swatch" style="background:#4ee8a0"></span>Hands-on practice</span>
</div>

<div class="topic-grid">
<a class="topic-card" href="#linux-internals">
<h4>Linux internals</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#networking-deep-dive">
<h4>Networking deep dive</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#distributed-systems-theory">
<h4>Distributed systems theory</h4>
<div class="tags"><span class="cat cat-gap">Gap to close</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#storage-internals">
<h4>Storage internals</h4>
<div class="tags"><span class="cat cat-gap">Gap to close</span><span class="cat cat-core">Core concept</span></div>
</a>
</div>

---

## Learning objectives

- Explain precisely what a container is in kernel terms
- Trace a packet from a process through the Linux network stack
- Reason about distributed system behaviour under partition
- Choose storage architectures with confidence, knowing the underlying tradeoffs

**Estimated study time:** 3–4 days

---

## 1. Linux internals

### 1.1 Kernel architecture and the syscall boundary

The Linux kernel is a monolithic kernel — all core services (process scheduling, memory management, VFS, networking, device drivers) run in the same address space with full hardware privileges (ring 0 on x86). User processes run in ring 3 with no direct hardware access.

**The boundary is enforced in hardware.** When a user-space process needs a privileged operation, it issues a system call. On x86-64, this means:

```
1. Arguments go into registers (rdi, rsi, rdx, r10, r8, r9)
2. Syscall number goes into rax
3. The `syscall` instruction triggers a mode switch
4. CPU saves registers, switches to kernel stack, jumps to syscall handler
5. Kernel executes the operation
6. Returns to user space via `sysret`
```

This mode switch costs ~100–300ns. It's cheap individually but adds up — a tight loop calling `read()` on a socket is slower than one using `io_uring` because the former mode-switches per call.

```bash
# Count syscalls by type for a process
strace -c -p <PID>

# Trace all syscalls with arguments
strace -e trace=all -p <PID>

# Filter to just network syscalls
strace -e trace=network nginx -g "daemon off;"

# Watch syscalls system-wide (useful for container debugging)
bpftrace -e 'tracepoint:raw_syscalls:sys_enter { @[comm, args->id] = count(); }'
```

**io_uring** (Linux 5.1+) addresses the syscall overhead problem by letting processes submit and drain I/O operations through shared ring buffers in memory, requiring zero syscalls for the fast path. This is why databases and high-performance runtimes (Tokio in Rust, some JVM projects) are migrating to it.

### 1.2 Process model — fork, exec, clone

Every running entity in Linux is a `task_struct`. Threads and processes are the same object, differentiated only by which resources they share.

```c
// fork() — creates new process, COW copy of parent
pid_t child = fork();

// exec() — replaces current process image
execve("/usr/bin/nginx", argv, envp);

// clone() — fine-grained control over what's shared
// This is what container runtimes actually call
clone(fn, stack, CLONE_NEWPID | CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWUTS, arg);
```

**Copy-on-write (COW):** After `fork()`, the child shares the parent's memory pages. The kernel marks them read-only. The first write by either process triggers a page fault; the kernel copies the page before allowing the write. This is why `fork()` is fast even for large processes — no actual copying until write time.

```bash
# See COW in action: watch RSS vs VSZ
ps -o pid,vsz,rss,comm -p <PID>
# VSZ (virtual size) = same as parent immediately after fork
# RSS (resident set size) = grows only as pages are written

# Process tree
pstree -p

# Detailed task info
cat /proc/<PID>/status
cat /proc/<PID>/maps      # Virtual memory layout
cat /proc/<PID>/smaps     # Per-mapping memory stats (shows COW shared/private)
```

**Zombie processes:** A process that has called `exit()` but whose parent has not called `wait()`. The process is dead (no CPU, no memory except a PID entry), but occupies a slot in the process table. If a parent exits before `wait()`-ing, the children are reparented to PID 1 (init/systemd in the host; your container entrypoint in a container). This is why container entrypoints that don't properly `wait()` for children accumulate zombie processes.

### 1.3 Namespaces — isolation primitives

Namespaces wrap specific global kernel resources and present each process with the illusion of having its own private instance.

| Namespace | `clone()` flag | Isolates |
|-----------|---------------|---------|
| `pid` | `CLONE_NEWPID` | Process ID number space |
| `net` | `CLONE_NEWNET` | Network stack (interfaces, routes, netfilter, sockets) |
| `mnt` | `CLONE_NEWNS` | Mount point tree |
| `uts` | `CLONE_NEWUTS` | Hostname and NIS domain name |
| `ipc` | `CLONE_NEWIPC` | System V IPC, POSIX message queues |
| `user` | `CLONE_NEWUSER` | UID/GID mappings |
| `cgroup` | `CLONE_NEWCGROUP` | cgroup root directory view |
| `time` | `CLONE_NEWTIME` | Clock offsets (Linux 5.6+) |

**`pid` namespace mechanics:**

The first process in a new PID namespace gets PID 1. It has all the responsibilities of init — it must `wait()` for children, it receives signals delivered to PID 1, and if it exits, all processes in the namespace are killed with SIGKILL.

On the host, these processes have real PIDs from the host's PID space. The kernel maintains a two-level mapping.

```bash
# Find a container's init PID on the host
docker inspect <container> --format '{{ .State.Pid }}'

# Enter a container's namespaces without Docker
nsenter --target <host-PID> --mount --uts --ipc --net --pid -- /bin/bash

# List all namespaces on the system
lsns

# See which namespaces a process belongs to
ls -la /proc/<PID>/ns/

# Each symlink target is namespace type:[inode]
# Processes sharing the same inode are in the same namespace
```

**`net` namespace mechanics:**

Each network namespace has its own:
- Network interfaces (including `lo`)
- IP routing table
- Netfilter (iptables/nftables) rules
- Connection tracking table
- Socket table
- Ephemeral port range

Two processes in different net namespaces can each bind `0.0.0.0:80` without conflict because they are binding to different socket tables in different kernel network stacks.

```bash
# Create and use a network namespace manually
ip netns add test-ns
ip netns exec test-ns bash

# Inside: only lo interface, isolated routing table
ip addr
ip route

# Connect two namespaces with a veth pair
ip link add veth0 type veth peer name veth1
ip link set veth1 netns test-ns
ip addr add 192.168.100.1/24 dev veth0
ip netns exec test-ns ip addr add 192.168.100.2/24 dev veth1
ip link set veth0 up
ip netns exec test-ns ip link set veth1 up
ping 192.168.100.2  # Reachable across namespace boundary via veth
```

**`user` namespace — rootless containers:**

User namespaces map UIDs inside the namespace to different UIDs outside. UID 0 (root) inside can be mapped to UID 65534 (nobody) outside. This is the basis of rootless containers — your "root" process inside the container has no privileges on the host.

```bash
# See UID mapping for a process
cat /proc/<PID>/uid_map
# Format: inside-uid  outside-uid  count
# 0         1000        1  → UID 0 inside = UID 1000 outside

# Run a container as a non-root user on the host
docker run --user 1000:1000 alpine id
```

### 1.4 cgroups — resource containment

Namespaces control what you can see; cgroups control how much you can use.

**cgroups v2 (unified hierarchy)** — default on modern Linux (Ubuntu 22.04+, RHEL 9):

```
/sys/fs/cgroup/
├── cgroup.controllers          # Available controllers
├── cgroup.subtree_control      # Enabled controllers for children
├── memory.max                  # Memory hard limit
├── cpu.max                     # CPU quota/period
├── io.max                      # I/O BPS/IOPS limits
└── system.slice/
    └── docker-<id>.scope/
        ├── cgroup.procs        # PIDs in this cgroup
        ├── memory.current      # Current memory usage
        ├── memory.max          # Memory limit
        ├── cpu.stat            # CPU usage statistics
        └── cpu.max             # "quota period" e.g. "50000 100000"
```

**CPU throttling in depth:**

`cpu.max = "50000 100000"` means: within any 100ms period, this cgroup may run for at most 50ms. If all processes in the cgroup exhaust their 50ms quota before the period ends, they are frozen until the next period. This is **throttling** — not killing, not deprioritising, but literally stopping execution.

This creates a subtle latency pattern: a service might be throttled at 3:00:00.000 for 50ms, resume at 3:00:00.050, get throttled again at 3:00:00.100, etc. From the outside this looks like periodic latency spikes at ~100ms intervals even when the node's overall CPU utilisation is low.

```bash
# Check CPU throttle metrics
cat /sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.stat
# nr_periods         = total scheduling periods elapsed
# nr_throttled       = periods where cgroup was throttled
# throttled_usec     = total microseconds throttled

# In Kubernetes, via Prometheus:
rate(container_cpu_cfs_throttled_periods_total[5m])
  /
rate(container_cpu_cfs_periods_total[5m])
# > 0.25 (25%) is concerning; > 0.5 is serious
```

**Memory: OOMKiller mechanics:**

When a cgroup hits its memory limit, the kernel invokes the OOM killer. It scores all processes in the cgroup using this formula:

```
oom_score = (process_memory_usage / total_memory) * 1000
oom_score += oom_score_adj  # tunable per-process, range -1000 to 1000
```

The process with the highest `oom_score` is killed. Kubernetes sets `oom_score_adj` based on QoS class:
- BestEffort: `1000` (killed first)
- Burstable: `min(max(2, 1000 - (1000 * memoryRequestBytes) / machineMemoryCapacityBytes), 999)`
- Guaranteed: `-997` (almost never killed)

```bash
cat /proc/<PID>/oom_score      # Current score (0-1000)
cat /proc/<PID>/oom_score_adj  # Adjustment

# OOM events in kernel log
dmesg | grep -E "oom_kill|Out of memory"
journalctl -k | grep "Killed process"

# Per-cgroup OOM events (v2)
cat /sys/fs/cgroup/.../memory.events
# oom          = number of OOM events
# oom_kill     = number of processes killed
```

**`blkio` / `io` controller:**

```bash
# v2: set max read BPS for a block device
echo "8:0 rbps=10485760" > /sys/fs/cgroup/.../io.max
# 8:0 = major:minor of /dev/sda, rbps = 10MB/s

# Current I/O stats
cat /sys/fs/cgroup/.../io.stat
```

> **Key mental model:** A container is: `clone()` with namespace flags (what you can see) + a cgroup (how much you can use) + a union filesystem (overlayfs) providing the root filesystem. There is no "container daemon" running inside. The container's init process is a plain Linux process, visible from the host with a real PID.

---

## 2. Networking deep dive

### 2.1 TCP/IP internals

**TCP state machine:**

```
CLOSED → LISTEN (server calls listen())
LISTEN → SYN_RECEIVED (SYN arrives, server sends SYN-ACK)
SYN_RECEIVED → ESTABLISHED (ACK arrives)

CLOSED → SYN_SENT (client calls connect())
SYN_SENT → ESTABLISHED (SYN-ACK arrives, client sends ACK)

ESTABLISHED → FIN_WAIT_1 (active close: FIN sent)
FIN_WAIT_1 → FIN_WAIT_2 (ACK received)
FIN_WAIT_2 → TIME_WAIT (FIN from peer received, ACK sent)
TIME_WAIT → CLOSED (2×MSL timer expires, default 60s)

ESTABLISHED → CLOSE_WAIT (passive close: FIN received, ACK sent)
CLOSE_WAIT → LAST_ACK (application closes, FIN sent)
LAST_ACK → CLOSED (ACK received)
```

**TIME_WAIT:** Exists to handle late-arriving packets from old connections. The 2×MSL (Maximum Segment Lifetime) wait ensures stale packets from a previous connection won't corrupt a new connection reusing the same 4-tuple (src_ip, src_port, dst_ip, dst_port).

At high connection rates (microservices making many short connections), TIME_WAIT can exhaust the ephemeral port range (default 32768–60999 = 28,231 ports).

```bash
# Current TIME_WAIT count
ss -ant state time-wait | wc -l

# Tune TIME_WAIT behaviour
sysctl net.ipv4.tcp_fin_timeout          # Default: 60 (seconds)
sysctl net.ipv4.tcp_tw_reuse            # Allow reuse for outbound connections (safer)
sysctl net.ipv4.ip_local_port_range     # Expand ephemeral port range

# Full socket stats
ss -s
# Shows counts per state: estab, closed, time-wait, etc.
```

**TCP congestion control:**

TCP constantly estimates available bandwidth and adjusts its sending rate. The algorithm determines how aggressively it probes and backs off.

| Algorithm | Strategy | Best for |
|-----------|----------|---------|
| Cubic (default pre-5.x) | Loss-based, cubic growth function | Datacenter, stable networks |
| BBR | Model-based (estimates BDP), doesn't rely on loss | WAN, lossy networks |
| QUIC | UDP-based, solves head-of-line blocking | HTTP/3, variable networks |

```bash
sysctl net.ipv4.tcp_congestion_control      # Current algorithm
sysctl net.ipv4.tcp_available_congestion_control  # Available
sysctl -w net.ipv4.tcp_congestion_control=bbr

# Measure connection RTT
ss -tnp | grep ESTAB  # Shows RTT per connection in extended output
ss -tnpi             # Even more detail including congestion state
```

**Kernel network receive path (simplified):**

```
NIC receives packet
  → DMA to ring buffer in RAM
  → Raise hardware interrupt
  → Kernel IRQ handler runs (maps to a CPU)
  → ksoftirqd processes the ring buffer (NAPI polling)
  → skb (socket buffer) allocated
  → Passes through netfilter hooks (PREROUTING)
  → Routing decision
  → Passes through netfilter hooks (INPUT for local delivery)
  → Delivered to socket receive queue
  → Application wake-up (epoll/select triggers)
  → Application calls recv()
```

```bash
# See receive queue depth (if growing, you're dropping packets)
cat /proc/net/softnet_stat
# Columns: total dropped squeezed ... (per CPU)

# Check for packet drops at NIC level
ethtool -S <interface> | grep -i drop
ip -s link show <interface>

# IRQ affinity (which CPU handles which NIC queue)
cat /proc/interrupts | grep eth0
```

### 2.2 eBPF — extended Berkeley Packet Filter

eBPF is a virtual machine embedded in the Linux kernel that lets you run sandboxed programs at kernel hook points without modifying kernel source or loading modules.

**The eBPF execution model:**

```
C source → Clang (LLVM backend) → eBPF bytecode
                                        ↓
                               Kernel verifier
                               (safety checks: no loops that don't terminate,
                                no bad memory access, no unbounded execution)
                                        ↓
                               JIT compiler → native CPU instructions
                                        ↓
                               Attached to hook point → executes in-kernel
```

**Hook points:**

| Hook | Timing | Use case |
|------|--------|---------|
| XDP (eXpress Data Path) | Before sk_buff allocation (earliest possible) | DDoS mitigation, load balancing |
| TC ingress/egress | After sk_buff, before/after routing | Packet modification, observability |
| kprobe/kretprobe | On kernel function entry/return | Tracing any kernel function |
| uprobe/uretprobe | On user-space function entry/return | Tracing application code |
| tracepoints | Stable kernel instrumentation points | Preferred over kprobes |
| LSM hooks | Linux Security Module hooks | Security policy enforcement |
| cgroup | Per-cgroup network hooks | Per-container policy |

**eBPF maps — communication between kernel and user space:**

```c
// Define a hash map in eBPF program
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, u32);           // key = PID
    __type(value, u64);         // value = byte count
    __uint(max_entries, 1024);
} bytes_by_pid SEC(".maps");

// In eBPF program: increment counter
u32 pid = bpf_get_current_pid_tgid() >> 32;
u64 *count = bpf_map_lookup_elem(&bytes_by_pid, &pid);
if (count) (*count)++;
```

```bash
# List loaded eBPF programs
bpftool prog list

# Show eBPF maps
bpftool map list
bpftool map dump id <id>

# Real-time observability with bpftrace
# Trace all execve() calls system-wide
bpftrace -e 'tracepoint:syscalls:sys_enter_execve { printf("%d %s %s\n", pid, comm, str(args->filename)); }'

# Latency histogram for read() syscalls
bpftrace -e '
tracepoint:syscalls:sys_enter_read { @start[tid] = nsecs; }
tracepoint:syscalls:sys_exit_read  /@start[tid]/ {
  @lat = hist(nsecs - @start[tid]);
  delete(@start[tid]);
}'

# TCP connection tracing
bpftrace -e 'kprobe:tcp_connect { printf("connect: %s pid=%d\n", comm, pid); }'

# Count syscalls by process
bpftrace -e 'tracepoint:raw_syscalls:sys_enter { @[comm] = count(); } interval:s:5 { print(@); clear(@); }'
```

**Why Cilium replaced kube-proxy with eBPF:**

iptables rules are evaluated sequentially. With 500 Services in a Kubernetes cluster, you might have 15,000 iptables rules. Every packet must traverse the relevant chain, which is O(n) in rule count.

eBPF uses hash maps for Service lookup — O(1) regardless of the number of Services. At scale (thousands of services), eBPF dataplane provides 5–10× lower latency for service forwarding compared to iptables.

```bash
# Measure iptables rule count vs service count
kubectl get svc --all-namespaces | wc -l
iptables -t nat -L | wc -l   # Should be roughly 20× service count with kube-proxy

# With Cilium eBPF:
cilium service list | wc -l
iptables -t nat -L | wc -l  # Near zero for pod-to-service traffic
```

### 2.3 CNI plugin architecture

The Container Network Interface (CNI) is a spec defining how container runtimes call network plugins. It has exactly two operations: ADD and DEL.

**CNI ADD — what happens when a pod starts:**

```
kubelet creates pod sandbox (pause container with shared net namespace)
    ↓
kubelet calls CRI (containerd) to set up networking
    ↓
containerd executes CNI binary: /opt/cni/bin/<plugin>
    with environment:
      CNI_COMMAND=ADD
      CNI_CONTAINERID=<id>
      CNI_NETNS=/proc/<PID>/ns/net
      CNI_IFNAME=eth0
    and config via stdin (from /etc/cni/net.d/)
    ↓
CNI plugin:
  1. Allocates IP from IPAM backend (host-local, DHCP, Calico IPAM, etc.)
  2. Creates veth pair: one end in pod netns (eth0), one end on host
  3. Configures IP/routes on both ends
  4. May configure additional host routes for pod reachability
  5. Returns JSON with assigned IP to containerd
    ↓
kubelet reports pod IP to apiserver
```

```bash
# CNI configuration files
ls /etc/cni/net.d/
cat /etc/cni/net.d/10-calico.conflist

# CNI binaries
ls /opt/cni/bin/

# Debug: what IPs are allocated?
cat /var/lib/cni/networks/cbr0/*  # For host-local IPAM

# Manually invoke CNI ADD for debugging
export CNI_COMMAND=ADD CNI_CONTAINERID=testcid CNI_NETNS=/var/run/netns/test \
       CNI_IFNAME=eth0 CNI_PATH=/opt/cni/bin
echo '{"cniVersion":"0.4.0","name":"test","type":"bridge","bridge":"cni0","ipam":{"type":"host-local","subnet":"10.244.0.0/24"}}' \
  | /opt/cni/bin/bridge

# Watch CNI calls in kubelet log
journalctl -u kubelet | grep -i cni
```

**Common CNI plugin comparison:**

| CNI | Dataplane | Overlay | NetworkPolicy | BGP |
|-----|-----------|---------|--------------|-----|
| Flannel | iptables/VXLAN | VXLAN/host-gw | No (needs separate) | No |
| Calico | iptables/eBPF | VXLAN/IPIP/BGP | Yes | Yes |
| Cilium | eBPF | VXLAN/Geneve/BGP | Yes (L7 too) | Yes |
| Weave | iptables | VXLAN | Yes | No |

---

## 3. Distributed systems theory

### 3.1 CAP theorem — precise understanding

CAP theorem (Brewer, 2000; formalised by Gilbert & Lynch, 2002) states: a distributed system cannot simultaneously guarantee all three of:

- **Consistency (C):** Every read returns the most recent write or an error
- **Availability (A):** Every non-failing node returns a response (not an error) for every request
- **Partition tolerance (P):** The system continues operating when network partitions occur

**The often-misunderstood corollary:** Network partitions are not optional. They will happen. Therefore you must tolerate P. The real choice is: **during a partition, do you sacrifice C or A?**

- **CP systems:** During a partition, reject requests (return errors) rather than serve potentially stale data
- **AP systems:** During a partition, serve the data you have even if it might be stale

**Practical classification:**

| System | Partition choice | Reasoning |
|--------|-----------------|-----------|
| etcd | CP | Minority partition stops accepting writes |
| ZooKeeper | CP | Quorum required for all operations |
| Cassandra | AP (tunable) | Quorum level configurable per-operation |
| DynamoDB | AP (default) | Eventually consistent by default |
| NATS JetStream | CP | Raft-based leader election |
| Ceph RADOS | CP | PG must have quorum of OSDs |
| Consul | CP | Raft-based, quorum required |

```bash
# Observe CP behaviour in etcd during partition
# 3-node cluster, partition one node:
iptables -A INPUT -s <etcd-node-2-ip> -j DROP
iptables -A OUTPUT -d <etcd-node-2-ip> -j DROP

# Cluster still has 2/3 quorum — all operations continue
etcdctl put foo bar  # Succeeds

# Partition second node:
iptables -A INPUT -s <etcd-node-3-ip> -j DROP
iptables -A OUTPUT -d <etcd-node-3-ip> -j DROP

# Now 1/3 — quorum lost, CP kicks in
etcdctl put foo bar  # Fails: "etcdserver: request timed out"
etcdctl get foo      # Fails: no reads either (strict CP)

# Restore
iptables -F
```

### 3.2 Consistency models — the full spectrum

CAP's "C" is actually just one point on a spectrum. Understanding the full spectrum lets you reason about system behaviour.

```
Linearizability (strongest)
    Every operation appears to take effect instantaneously at some point
    between its invocation and completion. All observers see the same order.
    etcd, Zookeeper, single-node Redis

Sequential consistency
    All operations appear in some sequential order consistent with
    the order seen by each individual process.
    Not linearizable: two clients may see operations in different order.

Causal consistency
    Operations that are causally related appear in the same order
    everywhere. Concurrent operations may be reordered.
    MongoDB sessions, some distributed databases

Monotonic read/write
    Once you've read a value, you'll never read an older one.
    Once your write is reflected, subsequent reads reflect it.
    Common in session-based consistency

Eventual consistency (weakest)
    Replicas will converge given no new updates.
    Cassandra (ONE), DynamoDB default, DNS
```

**Read-your-writes:** A middle ground that matters for user-facing systems. After you write, your subsequent reads see your write. Other users may not yet. This is what most applications actually need, and it's achievable without strong consistency.

```python
# Cassandra consistency levels (tunable per operation)
session.execute(
    "INSERT INTO orders (id, status) VALUES (%s, %s)",
    (order_id, 'CREATED'),
    consistency_level=ConsistencyLevel.QUORUM  # Write to majority
)

result = session.execute(
    "SELECT status FROM orders WHERE id=%s",
    (order_id,),
    consistency_level=ConsistencyLevel.QUORUM  # Read from majority → read-your-writes guaranteed
)
# QUORUM + QUORUM = strong consistency in Cassandra
# ONE + ONE = eventual consistency (faster, can read stale)
```

### 3.3 Consensus algorithms — Raft in depth

Raft was designed to be understandable (vs Paxos, which is notoriously difficult). It's used in etcd, CockroachDB, TiKV, Consul, and NATS JetStream.

**Raft guarantees:**
- At most one leader per term
- A leader has all committed log entries from previous terms
- A log entry is committed when stored on a majority of servers
- Committed entries are never overwritten

**Leader election — exact sequence:**

```
All nodes start as followers with randomised election timeout (e.g., 150–300ms)

If a follower receives no heartbeat before timeout:
  1. Convert to Candidate
  2. Increment currentTerm
  3. Vote for self
  4. Reset election timer
  5. Send RequestVote RPC to all other nodes

A node grants a vote if:
  a) candidate's term ≥ voter's currentTerm (not voting for stale candidates)
  b) voter has not already voted in this term
  c) candidate's log is at least as up-to-date:
     - candidate's lastLogTerm > voter's lastLogTerm, OR
     - candidate's lastLogTerm == voter's lastLogTerm AND
       candidate's lastLogIndex >= voter's lastLogIndex

If Candidate receives votes from majority (⌊n/2⌋ + 1):
  → Becomes Leader
  → Immediately sends empty AppendEntries (heartbeats) to all followers
  → This resets their election timers, preventing new elections
```

**Log replication — exact sequence:**

```
Client sends write to Leader
  1. Leader appends entry to its log (not yet committed)
  2. Leader sends AppendEntries RPC to all followers
     - Contains: term, leaderId, prevLogIndex, prevLogTerm, entries[], leaderCommit
  3. Each follower:
     - Verifies consistency (prevLogIndex/prevLogTerm matches its log)
     - Appends the entry to its own log
     - Returns success to leader
  4. Once leader receives success from majority:
     - Marks entry as committed
     - Applies to state machine
     - Returns success to client
  5. Next AppendEntries to followers includes updated commitIndex
     - Followers apply committed entries to their state machines
```

**Log consistency check (AppendEntries consistency):**

Before appending, a follower verifies: "Is the entry immediately before this one what we expect?" (prevLogIndex and prevLogTerm must match). If not, the follower rejects and the leader sends older entries until they find a matching point. This is how Raft handles followers that fall behind.

```bash
# Watch Raft in action in etcd
ETCDCTL_API=3 etcdctl watch --prefix /election --rev=0

# Check cluster state
etcdctl endpoint status --cluster -w table
# Shows: endpoint, id, version, db-size, is-leader, is-learner, raft-term, raft-index

# Current leader
etcdctl endpoint status --cluster -w json | jq '.[] | select(.Status.leader == .Status.header.member_id) | .Endpoint'

# Raft metrics in Prometheus
etcd_server_leader_changes_seen_total        # Should be very low
etcd_server_proposals_committed_total        # Ops committed
etcd_server_proposals_failed_total           # Should be 0
etcd_disk_wal_fsync_duration_seconds_bucket  # WAL write latency
```

**Why 3, 5, or 7 nodes — never even:**

With n nodes, Raft requires ⌊n/2⌋+1 for quorum. Fault tolerance = n - quorum = ⌊n/2⌋.

| Nodes | Quorum | Fault tolerance |
|-------|--------|----------------|
| 1 | 1 | 0 |
| 2 | 2 | 0 |
| 3 | 2 | 1 |
| 4 | 3 | 1 |
| 5 | 3 | 2 |
| 6 | 4 | 2 |
| 7 | 4 | 3 |

4 nodes gives the same fault tolerance as 3 but requires more coordination overhead. 6 gives the same as 5. Even numbers add cost without adding fault tolerance.

### 3.4 Vector clocks and causality

In distributed systems with no central clock, how do you order events? Lamport clocks and vector clocks solve different parts of this problem.

**Lamport clocks:** A logical counter. Each process increments its counter on every event. On message receipt, set counter = max(local, received) + 1. Gives a partial order: if A→B (A causally precedes B), then timestamp(A) < timestamp(B). But the converse is not true — higher timestamp doesn't imply causality.

**Vector clocks:** Each process maintains a vector of counters (one per process). On event: increment own counter. On send: include entire vector. On receive: take component-wise maximum, then increment own counter.

```
Process A: [1,0,0] → [2,0,0] → [3,0,0]
Process B:                [0,1,0] → [0,2,0] receives A[2,0,0] → [2,3,0]
Process C:                                              [0,0,1] receives B[2,3,0] → [2,3,2]
```

If vector clock of A ≤ vector clock of B (component-wise), then A causally precedes B. If neither ≤ the other, they are concurrent. This is how CRDTs (Conflict-free Replicated Data Types) and Cassandra's last-write-wins work.

---

## 4. Storage internals

### 4.1 Storage hardware — what the software sees

**SSD internals — why they're different from HDDs:**

Flash NAND cells can be read and written but must be erased before writing. Erasure happens at block granularity (256KB–1MB). Writing happens at page granularity (4–16KB). If you want to update a 4KB page in a block, you must:
1. Read the entire block (~256KB) into buffer
2. Modify the relevant page
3. Erase the block
4. Write the entire block back

This is write amplification. For sequential writes (append-only), it's close to 1:1. For random writes, it can be 10:1 or worse.

**FTL (Flash Translation Layer):** The SSD's internal firmware that maps logical block addresses (what the OS sees) to physical flash pages. The FTL implements wear levelling (spread writes across cells to extend life) and garbage collection (consolidate partially-valid blocks). The FTL's behaviour significantly impacts write amplification and latency variance.

```bash
# Check SSD health (wear indicator, reallocated sectors)
smartctl -a /dev/sda
# Look for: Wear_Leveling_Count, Media_Wearout_Indicator

# I/O stats from the kernel
cat /proc/diskstats
# Or with iostat:
iostat -x 1  # Extended stats including await (avg wait), %util

# Block device queue depth
cat /sys/block/sda/queue/nr_requests  # How many I/Os can queue
cat /sys/block/sda/queue/scheduler    # I/O scheduler (none/mq-deadline/bfq)
```

**I/O schedulers:**

- `none` (noop): No reordering. Best for SSDs and NVMe (they do their own scheduling internally)
- `mq-deadline`: Enforces a deadline per request. Prevents request starvation. Good for spinning disks.
- `bfq` (Budget Fair Queueing): Per-process I/O fairness. Good for desktop/interactive workloads.

For cloud VMs with NVMe-backed storage, `none` is usually optimal.

### 4.2 LSM trees — write-optimised storage

Log-Structured Merge (LSM) trees power: LevelDB, RocksDB, Cassandra, etcd (via bbolt for metadata, but conceptually similar), Prometheus TSDB, InfluxDB, TiKV.

**The core insight:** Sequential writes to append-only structures are dramatically faster than random in-place updates on both HDDs and SSDs. LSM trees convert all writes to sequential appends.

**Write path:**

```
Write arrives
    ↓
1. Append to WAL (Write-Ahead Log) — for crash recovery
    ↓
2. Insert into MemTable (in-memory sorted structure, usually skip list or red-black tree)
    ↓
3. When MemTable reaches size threshold (~64MB):
   Flush to disk as an SSTable (Sorted String Table) — immutable, sorted file
    ↓
4. SSTable compaction (background): merge SSTables, eliminate deleted/overwritten keys
```

**Read path:**

```
Read arrives
    ↓
1. Check MemTable (most recent data)
    ↓ (not found)
2. Check L0 SSTables (most recent flushed, may overlap)
    ↓ (not found)
3. Check L1, L2, L3... SSTables (older, non-overlapping within a level)
    Bloom filters skip SSTables that can't possibly contain the key
    ↓
4. Return value (or not found)
```

**Bloom filters:** A probabilistic data structure that answers "definitely not here" or "probably here." Each SSTable has a bloom filter. A read checks the bloom filter before opening the SSTable file. False positives are possible (opens a file that doesn't have the key) but false negatives are not (never skips a file that has the key).

**Compaction strategies:**

*Levelled (RocksDB default, Cassandra STCS/LTCS):*
- L0: small, newly flushed SSTables (can overlap)
- L1: one sorted run, ~10× larger than L0
- L2: one sorted run, ~10× larger than L1
- Compaction merges L(n) files into L(n+1)
- Read amplification: O(levels) — predictable
- Write amplification: O(levels × level_size_multiplier) — higher writes

*Tiered (size-tiered, used in Cassandra STCS):*
- SSTables grouped by size
- Compaction merges same-size SSTables
- Read amplification: higher (more SSTables to check)
- Write amplification: lower
- Better for write-heavy workloads

### 4.3 Ceph architecture

Ceph is a unified distributed storage system providing object (RADOS Gateway / S3), block (RADOS Block Device / RBD), and file (CephFS) storage — all built on the same underlying RADOS (Reliable Autonomic Distributed Object Store).

**The RADOS stack:**

```
Client (librados)
    ↓
Object → PG mapping via CRUSH
    ↓
OSD (Object Storage Daemon) — one per disk
    ↓
BlueStore (Ceph's own storage backend)
    ├── Block device (raw, no filesystem)
    ├── RocksDB for metadata (checksums, object map)
    └── Direct I/O for data (bypasses page cache, avoids double buffering)
```

**CRUSH algorithm:**

CRUSH (Controlled Replication Under Scalable Hashing) maps objects to OSDs using a deterministic pseudo-random function. Any client can compute the mapping without a metadata server lookup:

```
object_id → hash → PG (Placement Group) → CRUSH map → list of OSDs
```

The CRUSH map encodes the physical hierarchy (datacentre → rack → host → OSD) and the replication rules. This hierarchy is used to ensure replicas land on different failure domains.

```bash
# See CRUSH map
ceph osd getcrushmap -o /tmp/crush.bin
crushtool -d /tmp/crush.bin -o /tmp/crush.txt
cat /tmp/crush.txt

# Map an object to its OSDs
ceph osd map <pool> <object-name>
# Shows: osdmap, pool, object, pg, acting [1,3,2] → primary OSD 1, replicas 3,2

# OSD tree (physical layout)
ceph osd tree

# PG states
ceph pg stat
ceph pg dump | head -30

# Active+clean = healthy
# Active+degraded = one or more replicas missing but still serving I/O
# Active+undersized = fewer replicas than required (but still serving)
# Recovering = background repair in progress
# Incomplete = not enough up-to-date replicas — I/O blocked

# Monitor cluster I/O
ceph -w  # Live event stream

# OSD performance
ceph osd perf  # Latency per OSD — identifies slow OSDs
```

**Why Ceph latency is higher than local storage:**

1. **Network RTT:** Every write goes to the primary OSD via network, then primary replicates to secondary/tertiary OSDs, then waits for their ACK before returning to client. Even at 0.1ms NIC latency, 3 hops = 3× latency.
2. **Journaling:** BlueStore writes to RocksDB WAL for metadata, then to block device for data.
3. **PG overhead:** Objects are routed through PG mapping, OSD threading model, and BlueStore I/O path.

**Bluestore** replaced the older Filestore (which stored objects as files on XFS/ext4) to eliminate double-write (once to journal, once to filesystem). BlueStore writes directly to the block device with its own checksum and space management.

### 4.4 CSI — Container Storage Interface

CSI is the standard API between orchestrators (Kubernetes, Nomad) and storage systems. A CSI driver exposes a gRPC server implementing three services:

```
Identity service:
  GetPluginInfo()       → driver name and version
  GetPluginCapabilities() → what features the driver supports
  Probe()               → health check

Controller service (runs as Deployment — cluster-wide operations):
  CreateVolume()        → provision new storage (e.g., create EBS volume)
  DeleteVolume()        → deprovision storage
  ControllerPublishVolume()  → attach volume to a node (e.g., attach EBS to EC2)
  ControllerUnpublishVolume() → detach volume from node
  ListVolumes()
  CreateSnapshot()
  DeleteSnapshot()

Node service (runs as DaemonSet — per-node operations):
  NodeStageVolume()     → format and mount at a global path on the node
  NodePublishVolume()   → bind-mount from global path into pod's volume path
  NodeUnpublishVolume() → unmount from pod
  NodeUnstageVolume()   → unmount from global path
  NodeGetCapabilities() → what node-level features the driver supports
```

**The lifecycle of a PVC to mounted volume:**

```
1. User creates PVC
2. kube-controller-manager PersistentVolumeClaim controller watches, finds a matching StorageClass
3. CSI external-provisioner sidecar calls CreateVolume() on CSI controller
4. CSI driver provisions storage, returns VolumeId
5. PV is created and bound to PVC

6. Pod is scheduled to a node
7. kube-controller-manager attachdetach controller calls ControllerPublishVolume()
   (for network storage — skip for local)
8. CSI external-attacher sidecar watches VolumeAttachment objects

9. kubelet on the node calls NodeStageVolume()
   → formats filesystem if new
   → mounts at /var/lib/kubelet/plugins/kubernetes.io/csi/pv/<pv-name>/globalmount/
10. kubelet calls NodePublishVolume()
    → bind-mounts into pod: /var/lib/kubelet/pods/<pod-uid>/volumes/kubernetes.io~csi/<pv-name>/mount

11. Container starts with the bind-mounted volume available at its mount path
```

```bash
# Debug a stuck PVC
kubectl describe pvc <name>
# Look for events: ProvisioningFailed, FailedMount, etc.

kubectl describe pod <name>
# Look for: Unable to attach or mount volumes

# Check CSI driver logs
kubectl logs -n kube-system -l app=csi-<driver>-controller -c csi-provisioner
kubectl logs -n kube-system -l app=csi-<driver>-node -c csi-<driver>

# Check volume attachments (controller publish state)
kubectl get volumeattachments

# Check what's mounted on a node
kubectl get --raw "/api/v1/nodes/<node>/proxy/stats/summary" | jq '.node.fs'

# See all mounts on the node (connect to node)
findmnt | grep kubelet
```

---

## Common misconceptions

| Misconception | Reality |
|---------------|---------|
| "Containers are isolated from the host kernel" | They share the host kernel. A container syscall goes to the same kernel. |
| "CPU limits protect performance" | CPU limits cause throttling — periodic execution freezes. Requests are for scheduling; limits are for caps. |
| "CAP means pick 2 of 3" | P is mandatory. The choice is C vs A during a partition only. |
| "Raft is always safe" | Raft requires correct clock assumptions. Clock skew beyond election timeout causes unnecessary elections. |
| "SSD I/O is always fast" | SSDs have garbage collection pauses (latency spikes), write amplification, and thermal throttling under sustained load. |
| "etcd is a key-value store" | It's a distributed consensus system with a key-value interface. The consensus layer is the important part. |
| "eBPF is just for debugging" | eBPF powers production load balancers (Cilium), security enforcement (Falco), and network policy at hyperscaler scale. |

---

## Hands-on exercises

1. Create a network namespace, veth pair, and a bridge. Ping across the namespace boundary. Trace the packet path with `tcpdump` on both interfaces simultaneously.
2. Write a minimal cgroup (v2) that limits a process to 10% CPU. Verify it throttles using `cpu.stat`. Check `nr_throttled` vs `nr_periods`.
3. Use `bpftrace` to write a one-liner that prints a histogram of `read()` syscall latency, grouped by the calling process name.
4. Set up a 3-node etcd cluster. Kill the leader. Measure election time. Kill two followers. Observe write failure. Restore quorum.
5. Manually walk the LSM compaction of a local RocksDB instance using `ldb` tool — write 1M keys, observe L0→L1 compaction, measure read amplification before and after.
6. Run `iostat -x 1` on a system under write load. Interpret `await`, `svctm`, `%util`. Correlate with `iotop`.

---

## What to study next → [Phase 2 — Kubernetes Mastery](./phase2-kubernetes-mastery.md)

Everything in Phase 2 is built on Phase 1 primitives: the scheduler uses cgroups and namespaces; etcd uses Raft; CNI plugins use network namespaces and eBPF; RBAC and PodSecurity use Linux user namespaces and seccomp syscall filtering.
