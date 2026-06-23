# Deep Dive — Networking
## From the OSI Model to VXLAN: a Packet's Full Journey Through the Kernel and Kubernetes

> Networking is the most underrated skill in cloud engineering. Everyone learns `kubectl`; few can say what happens to a byte between two pods. This page builds that understanding from the wire up — every layer, every kernel hook, the history and tradeoffs behind each tool, and how overlays stitch a flat pod network across machines. The goal is a **mental model so solid you can debug a network you've never seen.**

<div class="topic-legend">
<span><span class="swatch" style="background:#6aa6ff"></span>Core concept</span>
<span><span class="swatch" style="background:#e8b84e"></span>Interview hot topic</span>
<span><span class="swatch" style="background:#b18cff"></span>Architecture depth</span>
<span><span class="swatch" style="background:#e87a4e"></span>Gap to close</span>
<span><span class="swatch" style="background:#4ee8a0"></span>Hands-on practice</span>
</div>

<div class="topic-grid">
<a class="topic-card" href="#network-models-osi-and-tcpip">
<h4>OSI &amp; TCP/IP models</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#the-packets-journey-into-the-kernel">
<h4>Packet into the kernel</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-gap">Gap to close</span></div>
</a>
<a class="topic-card" href="#layer-2-ethernet-arp-and-bridges">
<h4>L2: Ethernet, ARP, bridges</h4>
<div class="tags"><span class="cat cat-core">Core concept</span></div>
</a>
<a class="topic-card" href="#layer-3-ip-routing-and-nat">
<h4>L3: IP, routing, NAT</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#layer-4-tcp-udp-and-quic">
<h4>L4: TCP, UDP, QUIC</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
<a class="topic-card" href="#the-tooling-and-what-it-replaced">
<h4>Tooling &amp; its history</h4>
<div class="tags"><span class="cat cat-practice">Hands-on practice</span></div>
</a>
<a class="topic-card" href="#packet-filtering-and-nat-iptables-nftables-ipvs-ebpf">
<h4>Filtering: iptables→eBPF</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-gap">Gap to close</span></div>
</a>
<a class="topic-card" href="#network-overlays-vxlan-ipip-geneve">
<h4>Overlays: VXLAN, IPIP</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-gap">Gap to close</span></div>
</a>
<a class="topic-card" href="#kubernetes-networking-putting-it-together">
<h4>Kubernetes networking</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-interview">Interview hot topic</span></div>
</a>
</div>

---

## 1. Network models — OSI and TCP/IP

### The history that explains everything

Two models competed in the 1970s–80s. **OSI** (Open Systems Interconnection) was the committee-designed, government-and-telecom-backed standard — seven layers, rigorously specified *before* implementation. **TCP/IP** grew out of ARPANET: rough consensus and running code, standardized *after* it already worked.

TCP/IP won the wire. OSI's protocol suite is essentially dead. But OSI **won the vocabulary** — we still say "Layer 3 routing" and "Layer 7 load balancer" because the OSI layer numbers are the lingua franca. This is the first tradeoff lesson in networking: *the elegant design lost to the one that shipped, but its mental model survived because models are for humans, not machines.*

### The layers, and how they map

| OSI | OSI layer | TCP/IP | Real-world unit | Example |
|-----|-----------|--------|------|---------|
| 7 | Application | Application | message | HTTP, gRPC, DNS |
| 6 | Presentation | Application | — | TLS, encoding |
| 5 | Session | Application | — | (folded into app) |
| 4 | Transport | Transport | segment / datagram | TCP, UDP, QUIC |
| 3 | Network | Internet | packet | IP, ICMP |
| 2 | Data Link | Link | frame | Ethernet, ARP |
| 1 | Physical | Link | bits | copper, fiber, radio |

In practice engineers use a **5-layer hybrid**: Physical, Link, Network, Transport, Application. OSI's layers 5–7 collapse into "Application" because no real protocol cleanly separates session/presentation — TLS is "layer 6-ish" but lives wherever the app puts it.

### Encapsulation — the matryoshka doll

Every layer wraps the layer above in its own header (and sometimes trailer). Sending "GET /" over Ethernet:

```
┌──────────────────────────────────────────────────────────────────┐
│ Ethernet hdr │ IP hdr │ TCP hdr │  HTTP payload "GET /"  │ Eth FCS │
│  (L2, 14B)   │(L3,20B)│(L4,20B) │        (L7)            │ (CRC)   │
└──────────────────────────────────────────────────────────────────┘
   dst/src MAC   dst/src   dst/src     application data
                  IP        port
```

Each layer only reads *its own* header and treats everything inside as opaque payload. A router rewrites L2 (new src/dst MAC each hop) but leaves L3 mostly alone (just decrements TTL). A NAT box rewrites L3/L4. A proxy terminates L4 and reads L7. **Where a device operates tells you what it can see and change.**

> **Mental model** — The OSI model is a **stack of envelopes**. Your letter (L7 data) goes in a transport envelope (TCP: "deliver reliably, to port 443"), inside a routing envelope (IP: "to this address, anywhere on Earth"), inside a local-delivery envelope (Ethernet: "to the machine on this wire"). Each post office opens only the envelope addressed to its layer, acts, and passes the rest along. Debugging is asking: *which envelope is wrong, and which office should have caught it?*

---

## 2. The packet's journey into the kernel

This is the part most engineers never learn, and it's where real performance and drops live. We trace a single received packet from photons on fiber to `recv()` in your process.

### The receive (RX) path, step by step

```
1.  NIC receives frame off the wire, validates Ethernet FCS (CRC).
2.  NIC DMAs the frame directly into a ring buffer in RAM (the "rx ring"),
    a pre-allocated circular array of descriptors the driver set up.
3.  NIC raises a hardware IRQ: "I put something in the ring."
4.  CPU jumps to the driver's IRQ handler (top half) — does the BARE
    minimum: acknowledge, disable further IRQs for this queue, schedule
    a softirq. (You must not do heavy work in hard IRQ context.)
5.  NET_RX_SOFTIRQ runs (bottom half), serviced by the per-CPU
    ksoftirqd thread, using NAPI polling: drain many packets per poll
    instead of one IRQ per packet.
6.  For each frame, the kernel allocates an sk_buff (skb) — the struct
    that represents this packet for its whole life in the stack.
7.  XDP hook fires here (if attached) — earliest possible eBPF hook,
    can DROP/REDIRECT/PASS before the skb is even fully built.
8.  Protocol handler: eth_type_trans() sets the L3 protocol; packet
    enters the IP layer.
9.  netfilter PREROUTING hook (conntrack, DNAT) runs.
10. Routing decision: is this for me (local) or to be forwarded?
11. netfilter INPUT hook (firewall for locally-destined traffic).
12. Matched to a socket by the 4-tuple; skb queued on the socket's
    receive buffer (sk_rcvbuf).
13. Kernel wakes any process blocked in poll/epoll/recv on that socket.
14. Process calls recv(); kernel copies data from skb into user buffer,
    frees the skb.
```

### Why NAPI exists — an interrupt-storm war story

In the 1990s, every packet raised its own hardware interrupt. Under load (think 1 Gbps of small packets) the CPU spent 100% of its time in interrupt context — **receive livelock**: so busy being interrupted it never processed anything. The fix (NAPI, ~2003) is **interrupt mitigation by polling**: on the first packet, disable the IRQ and switch to polling the ring buffer in a softirq, draining a budget of packets per poll, then re-enable IRQs when the ring drains. Low latency at low load (interrupts), high throughput at high load (polling). This hybrid is the same pattern you'll see again in storage (io_uring) and even Kubernetes informers.

```bash
# The single most important RX-health file. Per-CPU columns:
# processed | dropped | time_squeeze | ... 
cat /proc/net/softnet_stat
# Nonzero col2 (dropped) = ring overflowed: NIC filled it faster than
#   softirq drained it. col3 (squeezed) = NAPI hit its budget and yielded.

# NIC-level drops/errors (ring exhaustion, bad CRC, no buffer)
ethtool -S eth0 | grep -Ei 'drop|err|miss|fifo'
ip -s -s link show eth0

# Ring buffer size — bumping it absorbs bursts (at a latency cost)
ethtool -g eth0
ethtool -G eth0 rx 4096

# Which CPU handles which NIC queue (IRQ affinity / RSS)
cat /proc/interrupts | grep eth0
# Spread queues across CPUs with RSS/RPS for multi-core scaling
```

### The sk_buff — the packet's body

The `sk_buff` (skb) is the kernel's universal packet container. Crucially, it carries `head/data/tail/end` pointers so layers can **push/pull headers without copying payload** — decapsulation is just moving the `data` pointer. Cloning an skb (e.g. for `tcpdump`) shares the payload and copies only metadata. Understanding that headers are added/removed by pointer arithmetic, not memcpy, is why the stack is fast.

### Offloads — pushing work to the NIC

Modern NICs do segmentation and checksums in hardware:

- **TSO/GSO** (TCP/Generic Segmentation Offload): the stack hands the NIC one giant 64KB "super-packet"; the NIC slices it into MTU-sized frames. Fewer trips through the stack.
- **GRO** (Generic Receive Offload): the inverse on RX — coalesce many small frames into one big skb before pushing it up the stack.
- **Checksum offload**: NIC computes/verifies L3/L4 checksums.

This is why `tcpdump` sometimes shows packets larger than your MTU — you're seeing the pre-segmentation super-packet, because the capture point is above the offload.

```bash
ethtool -k eth0 | grep -Ei 'tso|gso|gro|checksum'   # see offload state
ethtool -K eth0 gro off                              # toggle (debugging)
```

> **Mental model** — A packet arriving is a **delivery truck backing into a loading dock (the ring buffer)**. The hard IRQ is the buzzer — you just note "a truck is here" and walk away fast; you never unload in the doorway. The softirq/NAPI worker is the dock crew that unloads *many* trucks per shift (polling) so the buzzer doesn't drive everyone insane (livelock). The skb is the pallet that moves through the warehouse by re-labeling, never repacking. Drops happen when trucks arrive faster than the crew unloads — and `softnet_stat` is the dock's incident log.

---

## 3. Layer 2 — Ethernet, ARP, and bridges

### Frames and MAC addresses

Layer 2 moves frames between devices on the *same* link/segment. Addressing is by 48-bit **MAC address**, burned into (or faked by) the NIC. L2 has no concept of "the internet" — it only knows "the machines I can reach directly on this wire/switch."

### ARP — gluing L3 to L2

To send an IP packet to `10.0.0.5` on your local network, you need its MAC. **ARP** (Address Resolution Protocol) broadcasts "who has 10.0.0.5?"; the owner replies with its MAC, which you cache.

```bash
ip neigh                 # the ARP/neighbor cache (modern)
# arp -n                 # legacy equivalent
ip neigh flush all       # force re-resolution (debugging stale entries)
```

ARP is also a classic attack surface (ARP spoofing) and a classic outage cause (stale entries after a failover, before the cache expires or a gratuitous ARP is sent).

### The Linux bridge and veth — the heart of container networking

A **Linux bridge** is a software L2 switch inside the kernel. A **veth pair** is a virtual Ethernet cable: two interfaces, whatever enters one exits the other. This duo *is* how containers get networking:

```
   Pod netns                          Host netns
 ┌───────────┐                     ┌──────────────────────┐
 │  eth0     │═══ veth pair ═══════│ vethXXXX             │
 │ 10.244.0.5│                     │   └──► cni0 (bridge) │──► eth0 ──► wire
 └───────────┘                     └──────────────────────┘
```

The container's `eth0` is one end of a veth; the other end plugs into a bridge in the host namespace. The bridge switches frames between all pods on the node; traffic leaving the node goes out the host NIC. **Every container network model is a variation on this picture.**

```bash
ip link add veth0 type veth peer name veth1   # create a pair
ip link add br0 type bridge                    # create a bridge
bridge link                                    # show bridge ports (replaces brctl)
bridge fdb show                                # the bridge's MAC forwarding table
```

> **Mental model** — A bridge is a **power strip for the network**: dumb, fast, learns by listening (it records which MAC it saw on which port). A veth pair is a **garden hose between two rooms (namespaces)** — pour in one end, it comes out the other. Containers don't have magic networking; they have a hose into a power strip.

---

## 4. Layer 3 — IP, routing, and NAT

### The routing decision

For every packet, L3 answers one question: **"out which interface, and to which next-hop, does this go?"** The kernel consults the routing table, longest-prefix-match wins.

```bash
ip route                       # main routing table (replaces `route -n`)
ip route get 8.8.8.8           # ask the kernel: how would YOU route this? (gold)
# 8.8.8.8 via 192.168.1.1 dev eth0 src 192.168.1.50 ...
```

`ip route get` is the single most useful routing command — it shows the *actual* decision the kernel makes, including source-address selection, rather than making you simulate it in your head.

### Policy routing — more than one table

Linux supports **multiple routing tables** plus rules choosing between them based on source, fwmark, etc. This is how a node can route pod traffic differently from host traffic — Calico, Cilium, and cloud CNIs lean on it heavily.

```bash
ip rule                        # the rules that pick a table
ip route show table local      # the kernel's auto-managed local table
ip route show table 100        # a CNI-managed table
```

### NAT and conntrack — the stateful rewrite

**NAT** rewrites source/destination IP:port. **SNAT/MASQUERADE** (many private hosts → one public IP) is how your laptop and how pod egress reaches the internet. The magic that makes replies come back is **conntrack** (connection tracking): the kernel remembers each flow's original and translated tuples so the reverse packet is un-rewritten correctly.

```bash
# Conntrack is a finite table — exhausting it drops new connections,
# a real and nasty production failure on busy NAT gateways / nodes.
sysctl net.netfilter.nf_conntrack_max
cat /proc/sys/net/netfilter/nf_conntrack_count    # current usage
conntrack -L                                       # list live flows
conntrack -S                                       # per-cpu stats incl. drops
```

> **Mental model** — Routing is the **postal sorting facility**: it doesn't know the whole route, only "the next bin toward the destination" (next-hop), and it re-decides at every hop. NAT is the **front desk of an office building**: outgoing mail gets the building's return address, and the desk keeps a ledger (conntrack) so replies addressed to the building get routed back to the right person inside. Lose the ledger and every reply is undeliverable.

---

## 5. Layer 4 — TCP, UDP, and QUIC

### TCP: reliability built on an unreliable network

TCP turns lossy, reordering IP into an ordered, reliable byte stream via sequence numbers, ACKs, retransmission, flow control (receiver window) and congestion control (network estimate). The **3-way handshake** (SYN, SYN-ACK, ACK) syncs sequence numbers and costs one round-trip before any data.

### Congestion control — a history of guessing the network's capacity

| Era | Algorithm | How it estimates capacity | Weakness |
|-----|-----------|---------------------------|----------|
| 1988 | Reno / NewReno | Loss = congestion; halve on loss (AIMD) | Collapses on random (non-congestion) loss |
| 2006 | **Cubic** (Linux default) | Cubic growth after loss; faster recovery on fat pipes | Still loss-based; bufferbloat |
| 2016 | **BBR** (Google) | Models bottleneck bandwidth × RTT directly; ignores loss | Can be unfair to Cubic flows |

The arc: loss-based assumes *any* packet loss means congestion — false on Wi-Fi and WANs where loss is often random. **BBR** instead builds a model of the path's bandwidth-delay product and paces to it, which is why it dominates on lossy long-haul links (and powers much of Google/YouTube).

```bash
sysctl net.ipv4.tcp_congestion_control                 # current
sysctl net.ipv4.tcp_available_congestion_control       # what's loaded
sysctl -w net.ipv4.tcp_congestion_control=bbr          # switch
ss -tni                                                # per-socket rtt, cwnd, retrans
```

### UDP — when you want the network to get out of the way

UDP is IP plus ports plus a checksum — no handshake, ordering, or retransmission. You use it when *you* want to control reliability (game state, VoIP, DNS, QUIC) or when there's nothing to be reliable about (metrics, video frames where a late frame is useless anyway).

### QUIC — rebuilding TCP in user space over UDP

TCP has a fatal structural flaw for multiplexed protocols: **head-of-line blocking**. HTTP/2 multiplexes many streams over one TCP connection, but if *one* TCP segment is lost, *all* streams stall waiting for the retransmit — because TCP is a single ordered byte stream. **QUIC** (the basis of HTTP/3) runs over UDP and implements streams, reliability, and congestion control itself, so a lost packet only blocks its own stream. It also folds the TLS handshake into the transport handshake (0–1 RTT setup) and survives IP changes (connection migration — your phone switching Wi-Fi→LTE keeps the connection).

The tradeoff: QUIC lives in user space, so it costs more CPU per byte than kernel TCP, and middleboxes/firewalls that only understand TCP can mishandle it.

> **Mental model** — TCP is a **single conveyor belt with a strict one-at-a-time rule**: drop one box and everything behind it waits. HTTP/2 put many orders on that one belt — fast until a jam blocks them all (head-of-line blocking). QUIC gives each order **its own belt** (independent streams over UDP), so one jam doesn't stop the others, and it remembers your order even if you walk to a different counter (connection migration).

---

## 6. The tooling — and what it replaced

Every networking tool you know replaced an older one for a reason. Knowing the *why* tells you which to trust.

| Task | Legacy (`net-tools`) | Modern (`iproute2`/other) | Why the switch |
|------|----------------------|---------------------------|----------------|
| Interfaces/addresses | `ifconfig` | `ip addr` / `ip link` | net-tools couldn't show multiple IPs, IPv6, or namespaces well; unmaintained |
| Routing table | `route -n` | `ip route` | policy routing, multiple tables |
| Socket stats | `netstat -tulpn` | `ss -tulpn` | `ss` reads netlink directly — far faster on busy hosts |
| ARP cache | `arp -n` | `ip neigh` | unified netlink interface |
| Bridges | `brctl` | `bridge` / `ip link` | folded into iproute2 |
| Packet filter | `iptables` | `nftables` / eBPF | O(n) rule scaling, atomic updates |
| Capture | `tcpdump` | `tcpdump` + `tshark`/Wireshark | tcpdump still king for CLI; tshark adds dissectors |

**The lesson:** `net-tools` (`ifconfig`, `netstat`, `route`) parses `/proc` text and predates namespaces, multiple addresses, and modern netlink; `iproute2` talks the kernel's native **netlink** API. On a host with 100k sockets, `netstat` can take seconds; `ss` returns instantly. Reach for `ip`, `ss`, and `bridge` first.

```bash
# Capture and actually read it
tcpdump -ni eth0 'tcp port 443 and host 10.0.0.5' -w cap.pcap
tcpdump -nr cap.pcap -A                 # ASCII payload
tshark -r cap.pcap -Y 'http.request'    # filter with dissectors

# Live socket truth
ss -tanp                                # all TCP, numeric, with PIDs
ss -s                                   # summary by state (TIME_WAIT etc.)
```

> **Mental model** — Old tools *read the kernel's diary* (`/proc` text); new tools *call the kernel directly* (netlink). The diary is human-readable but slow and lossy; the direct line is fast and complete. When two tools disagree, trust the one closer to the kernel.

---

## 7. Packet filtering and NAT — iptables, nftables, IPVS, eBPF

### netfilter — the hook points

The Linux firewall isn't `iptables`; it's **netfilter**, a set of hook points in the packet path (PREROUTING, INPUT, FORWARD, OUTPUT, POSTROUTING). `iptables`, `nftables`, and `conntrack` are all *clients* of netfilter. This is why you saw netfilter hooks in the RX path (§2).

```
        ┌─────────────┐     routing    ┌─────────┐
  ─────►│ PREROUTING  │──────decision──►│ FORWARD │─────► POSTROUTING ─────►
        │ (DNAT,      │       │         └─────────┘            │
        │  conntrack) │       ▼                                ▼
        └─────────────┘    ┌───────┐                     (SNAT/MASQ)
                           │ INPUT │  local process  ┌────────┐
                           └───────┘ ───────────────►│ OUTPUT │
```

### The scaling story: why Kubernetes left iptables

`iptables` evaluates rules as an **ordered list, top to bottom — O(n)**. A cluster with thousands of Services generates tens of thousands of rules; every packet may traverse them linearly, and every Service change rewrites the whole table non-atomically. That's the bottleneck that drove three successive answers:

| Mechanism | Data structure | Service routing cost | Used by |
|-----------|----------------|---------------------|---------|
| **iptables** | ordered rule chains | O(n) per packet | legacy kube-proxy |
| **IPVS** | in-kernel hash tables | O(1) lookup, real LB algos (rr, lc, …) | kube-proxy `--proxy-mode=ipvs` |
| **nftables** | maps/sets, single VM | O(1) via maps, atomic updates | modern kube-proxy nftables mode, firewalls |
| **eBPF** | programmable, hash maps | O(1), no netfilter traversal at all | Cilium |

**eBPF (Cilium)** is the current frontier: instead of *configuring* netfilter, it *replaces* the data path with custom kernel programs attached at XDP/tc, doing service load-balancing and policy with hash-map lookups and even bypassing iptables/conntrack entirely. The tradeoff is operational complexity and a higher floor of kernel/observability sophistication.

> **Mental model** — `iptables` is a **paper checklist read top to bottom at every door** — fine for 10 rules, agony for 10,000. IPVS/nftables swap the checklist for an **index (a hash map): jump straight to the answer.** eBPF goes further and **rewrites the door itself** so there's barely a check at all. Kubernetes' networking evolution is the story of escaping an O(n) checklist.

---

## 8. Network overlays — VXLAN, IPIP, GENEVE

### The problem overlays solve

Kubernetes promises a **flat network**: every pod gets a routable IP and any pod can reach any other pod without NAT. But the physical network (the **underlay**) knows nothing about pod IPs like `10.244.3.7` — they're not in anyone's routing tables, and the switches would drop them. An **overlay** solves this by **encapsulating** pod packets inside packets the underlay *does* understand, addressed node-to-node.

```
Pod A (10.244.1.5, node1)  ──►  Pod B (10.244.2.9, node2)

Original (inner) packet:   [ IP src 10.244.1.5 → dst 10.244.2.9 | payload ]
                                          │ encapsulate
                                          ▼
On the wire (outer packet):[ IP src node1 → dst node2 | VXLAN/IPIP | inner packet ]
                                          │ the underlay routes THIS (node IPs)
                                          ▼
                            node2 decapsulates, delivers inner packet to Pod B
```

### The encapsulation options

| Overlay | Encap | Overhead | What it carries | Tradeoff |
|---------|-------|----------|-----------------|----------|
| **IPIP** | IP-in-IP | **20 bytes** | IP inside IP | Smallest overhead, but IPv4-only payload, no L2, less metadata |
| **VXLAN** | MAC-in-UDP (port 4789) | **50 bytes** | full L2 frame + 24-bit VNI | Ubiquitous, hardware-offloadable, carries L2 — but biggest overhead |
| **GRE** | IP-in-GRE | 24 bytes | flexible | General-purpose, older, less offload |
| **GENEVE** | UDP, extensible TLV options | ~50+ bytes | VXLAN + extensible metadata | Future-proof (carries policy/identity), used by Cilium/OVN |

**VXLAN** is the workhorse: it wraps the entire inner Ethernet frame in UDP, with a 24-bit **VNI** (VXLAN Network Identifier) giving 16M virtual networks (vs VLAN's 4,094 — the original motivation). Each node runs a **VTEP** (VXLAN Tunnel Endpoint) that encaps/decaps. Because it's UDP, it's NAT-friendly and hardware-offloadable on modern NICs.

**IPIP** is the minimalist: just an outer IP header (20 bytes) around the inner IP packet. Lower overhead, but it can't carry L2 info and is IPv4-centric. Calico uses it as a lightweight option when pure routing (BGP) isn't available.

**GENEVE** is VXLAN's successor: same UDP encapsulation, but with extensible **TLV options** so the overlay can carry metadata (like security identity) alongside the packet — which is exactly what identity-aware CNIs want.

### The MTU tax — the #1 overlay footgun

Encapsulation eats into the **MTU**. If the underlay MTU is 1500 and VXLAN adds 50 bytes, the pod MTU must drop to **1450** or packets get fragmented (or silently dropped if DF is set and PMTU discovery is blocked). Mismatched overlay MTU causes the classic "small requests work, large responses hang" bug — TLS handshakes complete (small packets) but the first big response stalls.

```bash
ip -d link show flannel.1        # a VXLAN device: shows vxlan id (VNI), port
ip -d link show tunl0            # an IPIP tunnel device
bridge fdb show dev flannel.1    # VTEP forwarding: remote MAC → remote node IP
ip link show eth0 | grep mtu     # verify pod/node MTU accounts for encap overhead
# Reproduce an MTU bug: ping with don't-fragment at the edge size
ping -M do -s 1472 10.244.2.9    # 1472 + 28 = 1500; bump -s to find the cliff
```

> **Mental model** — An overlay is **putting your inter-office mail (pod-to-pod) inside the postal system's standard envelope (node-to-node)** because the post office only understands its own envelopes. VXLAN uses a big, universal envelope that fits a whole letter-with-its-own-envelope (L2-in-UDP); IPIP uses a thin envelope that barely wraps the inner letter (IP-in-IP). The catch: a wrapped letter is heavier (MTU overhead), so if you stuff it as full as an unwrapped one, it won't fit through the slot (fragmentation).

---

## 9. Kubernetes networking — putting it together

### The four rules of the Kubernetes network model

1. Every **pod gets its own IP** (no port-mapping games).
2. Every pod can reach **every other pod** without NAT (flat network).
3. **Nodes can reach all pods** (and vice versa) without NAT.
4. The IP a pod sees itself as **is the IP others use** to reach it.

Everything above (bridges, veth, routing, overlays) exists to make these four rules true across many machines. Kubernetes itself doesn't implement them — it delegates to a **CNI plugin**.

### How CNI is actually invoked

When the kubelet creates a pod, it calls the CNI plugin as an executable: a binary in `/opt/cni/bin`, configured by JSON in `/etc/cni/net.d`, receiving the container's netns on stdin and returning the assigned IP. That's the entire contract — which is why CNI plugins range from 200-line scripts to Cilium.

### The CNI landscape and their tradeoffs

| CNI | Data path | Pod-to-pod across nodes | Network policy | Best when |
|-----|-----------|------------------------|----------------|-----------|
| **Flannel** | Linux bridge + **VXLAN** overlay | encapsulated | none (needs add-on) | simplicity; you just want it to work |
| **Calico** | pure L3 routing + **BGP**, or **IPIP**/VXLAN overlay | routed (no encap) or IPIP | rich (iptables/eBPF) | scale, policy, on-prem with BGP |
| **Cilium** | **eBPF** at tc/XDP; VXLAN or **GENEVE** or routing | eBPF + optional encap | rich, identity-based | performance, observability, zero-trust |
| **AWS VPC CNI** | pods get **real VPC IPs** (ENI) | native VPC routing, no overlay | security groups | EKS; no overlay tax |

The fundamental fork: **overlay vs. native routing.** Overlays (Flannel VXLAN) work anywhere because they hide pod IPs from the underlay — at the cost of the MTU/encap tax. Native routing (Calico BGP, AWS VPC CNI) makes pod IPs *first-class* in the real network — faster and debuggable with normal tools, but it requires control of (or cooperation from) the underlay.

### Services — the second networking system

Pod IPs are ephemeral, so **Services** give a stable virtual IP (ClusterIP) that load-balances to healthy pods. This is implemented by **kube-proxy** (iptables/IPVS/nftables modes from §7) or bypassed entirely by Cilium's eBPF. A request to a ClusterIP is DNAT'd to a real pod IP, then conntrack ensures replies come back — the exact §4 NAT mechanism, applied to service routing.

```bash
# The CNI config and binaries the kubelet uses
ls /etc/cni/net.d/ ; ls /opt/cni/bin/
# See the overlay device and its remote-node forwarding table
ip -d link show | grep -A2 -Ei 'vxlan|ipip|geneve'
# Trace a Service to its backends
kubectl get endpointslices -l kubernetes.io/service-name=my-svc
# On a node, see how the Service VIP is programmed
iptables-save -t nat | grep my-svc        # iptables mode
ipvsadm -Ln                                # IPVS mode
cilium-dbg service list                    # Cilium/eBPF mode
```

> **Mental model** — Kubernetes networking is a **promise** ("every pod can talk to every pod, flatly") that the CNI keeps using the primitives from this whole page: **veth + bridge** to get a pod onto the node (§3), **routing** to leave the node (§4), and an **overlay or BGP** to cross between nodes (§8). Services layer a **stable phone number (VIP) over a rotating cast of pods**, dialed by NAT (§4) and programmed by whichever filtering engine the cluster chose (§7). There is no magic — only these layers, composed.

---

## Review — self-check

If you can answer these from memory, you own this material. If not, the linked section is where to go back.

1. **Why did TCP/IP beat OSI on the wire but lose the vocabulary war?** → [§1](#network-models-osi-and-tcpip)
2. **A packet arrives. Name every stage from the NIC's DMA to your `recv()` — and where drops show up.** → [§2](#the-packets-journey-into-the-kernel)
3. **What problem did NAPI solve, and what's the general pattern (interrupt vs. poll) it represents?** → [§2](#the-packets-journey-into-the-kernel)
4. **Containers have no special networking — what two primitives actually connect a pod to the node?** (veth + bridge) → [§3](#layer-2-ethernet-arp-and-bridges)
5. **What does `ip route get 8.8.8.8` show that `ip route` doesn't?** → [§4](#layer-3-ip-routing-and-nat)
6. **Why can exhausting the conntrack table take down a busy node, and how would you detect it?** → [§4](#layer-3-ip-routing-and-nat)
7. **Why is BBR better than Cubic on a lossy WAN? What assumption does loss-based CC make that's wrong there?** → [§5](#layer-4-tcp-udp-and-quic)
8. **What is head-of-line blocking in HTTP/2, and how does QUIC eliminate it?** → [§5](#layer-4-tcp-udp-and-quic)
9. **Why does `ss` return instantly where `netstat` crawls on a busy host?** (netlink vs `/proc`) → [§6](#the-tooling-and-what-it-replaced)
10. **Why did Kubernetes move kube-proxy from iptables toward IPVS/nftables/eBPF? What's the complexity class problem?** → [§7](#packet-filtering-and-nat-iptables-nftables-ipvs-ebpf)
11. **VXLAN vs IPIP: overhead, what each carries, and when you'd pick each.** → [§8](#network-overlays-vxlan-ipip-geneve)
12. **A TLS handshake succeeds but the first large response hangs. What's your first hypothesis?** (overlay MTU) → [§8](#network-overlays-vxlan-ipip-geneve)
13. **State the four rules of the Kubernetes network model, and name the primitive that makes each true across nodes.** → [§9](#kubernetes-networking-putting-it-together)
14. **Overlay (Flannel VXLAN) vs native routing (Calico BGP / AWS VPC CNI): the core tradeoff in one sentence.** → [§9](#kubernetes-networking-putting-it-together)

**The one-paragraph summary to carry with you:** A byte leaves your app through a socket, gets wrapped layer by layer (L4 reliability, L3 routing, L2 delivery), is DMA'd onto a ring buffer at the far NIC, drained by a polling softirq into an `sk_buff`, run through netfilter hooks and a routing decision, and handed to the destination socket. In Kubernetes, that journey crosses a **veth** into a **bridge**, gets **routed** off the node, is **encapsulated** (VXLAN/IPIP) or **natively routed** (BGP) to the destination node, and — if it was aimed at a Service — was **NAT'd** from a virtual IP to a real pod along the way. Every layer is an envelope; debugging is finding the wrong one.

---

← Back to [Phase 1 — Foundation Gaps](phase1-foundation-gaps.html#networking-deep-dive)
