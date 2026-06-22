# Security Concepts
## Cryptography, TLS, and How Kubernetes Stays Secure

> The goal of this page is dual literacy: a **basic** picture you can hold in your head, and the **deep** mechanics underneath it. Each topic ends with a *mental model* — the one-sentence intuition to fall back on when the details blur. Security is not a feature you add; it is a set of guarantees you can reason about. This page is about building that reasoning.

---

## Learning objectives

- Explain what a digital signature actually proves — and what it does **not**
- Describe a TLS handshake packet by packet, for both TLS 1.2 and 1.3
- Understand why short-lived certificates beat long-lived secrets, and the machinery that makes them practical
- Map the entire Kubernetes security model: identity, authentication, authorization, admission, and encryption
- Reason about "how do I *guarantee* this is secure" using threat models, not vibes

**Estimated study time:** 2–3 days

---

## Topic map

<div class="topic-legend">
<span><span class="swatch" style="background:#6aa6ff"></span>Core concept</span>
<span><span class="swatch" style="background:#e8b84e"></span>Interview hot topic</span>
<span><span class="swatch" style="background:#b18cff"></span>Architecture depth</span>
<span><span class="swatch" style="background:#e87a4e"></span>Gap to close</span>
<span><span class="swatch" style="background:#4ee8a0"></span>Hands-on practice</span>
</div>

<div class="topic-grid">
<div class="topic-card">
<h4>Cryptographic primitives</h4>
<div class="tags"><span class="cat cat-core">Core concept</span></div>
</div>
<div class="topic-card">
<h4>Digital signatures</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</div>
<div class="topic-card">
<h4>Certificates &amp; PKI</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-gap">Gap to close</span></div>
</div>
<div class="topic-card">
<h4>Short-lived certificates</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-gap">Gap to close</span></div>
</div>
<div class="topic-card">
<h4>TLS handshake</h4>
<div class="tags"><span class="cat cat-core">Core concept</span><span class="cat cat-interview">Interview hot topic</span></div>
</div>
<div class="topic-card">
<h4>Kubernetes PKI &amp; mTLS</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-interview">Interview hot topic</span></div>
</div>
<div class="topic-card">
<h4>Identity &amp; authentication</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-gap">Gap to close</span></div>
</div>
<div class="topic-card">
<h4>Authorization &amp; admission</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span></div>
</div>
<div class="topic-card">
<h4>Guaranteeing security</h4>
<div class="tags"><span class="cat cat-arch">Architecture depth</span><span class="cat cat-practice">Hands-on practice</span></div>
</div>
</div>

---

## 1. Cryptographic primitives — the three tools

Everything in this page is built from exactly three primitives. If you understand what each *guarantees*, the rest is composition.

**1. Cryptographic hash (e.g. SHA-256).** A one-way function: easy to compute `H(message)`, infeasible to reverse, and infeasible to find two messages with the same hash (collision resistance). It compresses any input to a fixed-size fingerprint.

```bash
echo -n "hello" | sha256sum
# 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
# Change one bit → completely different output (avalanche effect)
```

Hashes give you **integrity**: if the hash matches, the bytes were not altered.

**2. Symmetric encryption (e.g. AES-256-GCM).** One shared secret key both encrypts and decrypts. Fast (hardware-accelerated, ~GB/s) but requires both parties to already share the key. GCM is *authenticated* encryption — it gives confidentiality **and** integrity in one operation.

**3. Asymmetric / public-key crypto (e.g. RSA, ECDSA, Ed25519).** A *key pair*: a public key you publish freely and a private key you guard. The mathematical relationship means:

- Encrypt with the **public** key → only the **private** key can decrypt → *confidentiality* to the key holder.
- Sign with the **private** key → anyone with the **public** key can verify → *authenticity* and *non-repudiation*.

Asymmetric crypto is slow (~1000x slower than symmetric), so it is almost never used to encrypt bulk data. Its job is to **bootstrap trust** and **exchange a symmetric key**. That single sentence explains the entire shape of TLS.

> **Mental model** — Asymmetric crypto is the expensive armored truck you use once to deliver a shared house key; symmetric crypto is the cheap, fast lock you use for everything after. Hashing is the tamper-evident seal on the envelope.

---

## 2. Digital signatures

### 2.1 The basic idea

A signature answers one question: **"Did the holder of *this specific* private key vouch for *these exact* bytes?"** If verification passes, you know two things at once:

- **Authenticity** — it came from whoever controls the private key.
- **Integrity** — not a single byte changed since they signed it.

It does **not** give confidentiality. A signed message is still plaintext; anyone can read it. Signing is about *trust*, not *secrecy*.

### 2.2 The deep mechanics

You never sign the whole message — you sign its hash. This is both a performance trick (asymmetric ops are slow, hashes are small and fixed-size) and a security requirement.

```
SIGNING (private key holder):
  digest    = SHA256(message)
  signature = Sign_privkey(digest)
  send: (message, signature)

VERIFYING (anyone with the public key):
  digest'   = SHA256(received_message)      # recompute from the bytes you got
  ok        = Verify_pubkey(signature, digest')
  # ok == true  ⟺  signature was produced by the matching private key
  #                over a message whose hash equals digest'
```

The verifier recomputes the hash *independently* from the bytes in hand and checks it against what the signature attests to. Tamper with one byte → `digest'` changes → verification fails. Forge without the private key → infeasible, because producing a valid signature requires solving the hard math problem (integer factorization for RSA, discrete log on an elliptic curve for ECDSA/Ed25519).

```bash
# Generate an Ed25519 key pair (modern, fast, small)
openssl genpkey -algorithm ed25519 -out priv.pem
openssl pkey -in priv.pem -pubout -out pub.pem

# Sign a file and verify it
openssl pkeyutl -sign   -inkey priv.pem -rawin -in message.txt -out sig.bin
openssl pkeyutl -verify -pubin -inkey pub.pem -rawin -in message.txt -sigfile sig.bin
# "Signature Verified Successfully" — flip one byte of message.txt and it fails
```

**Where you already rely on signatures:** every TLS certificate (the CA signs it), every signed container image (`cosign`), every JWT / ServiceAccount token, every `git commit -S`, every OS package and software update. The entire trust fabric of the internet is signatures all the way down.

> **Mental model** — A signature is a **wax seal made from a stamp only you possess**, pressed over a tamper-evident summary of the document. Anyone can recognize the seal (public key); only you can produce it (private key); and the seal is bound to *this* document's fingerprint, so it cannot be peeled off and reused on another.

---

## 3. Certificates and PKI — making public keys trustworthy

### 3.1 The problem signatures don't solve

Signatures prove a message came from *a* private key. But how do you know that public key belongs to `api.bank.com` and not an attacker? A raw public key is anonymous. You need to bind an **identity** to a **public key**, and have someone you trust vouch for that binding.

That binding, signed by a trusted third party, **is a certificate.**

### 3.2 What a certificate actually is

An X.509 certificate is a structured document containing, at minimum:

| Field | Meaning |
|-------|---------|
| Subject | Who this identifies (`CN=api.bank.com`, SANs) |
| Subject Public Key | The public key being vouched for |
| Issuer | Which CA signed this |
| Validity | `notBefore` / `notAfter` window |
| Extensions | Key usage, SANs, EKU (server/client auth), etc. |
| **Signature** | The **CA's signature** over all the fields above |

```bash
# Inspect a live certificate
openssl s_client -connect example.com:443 -servername example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -text | head -40

# Decode a local cert
openssl x509 -in tls.crt -noout -subject -issuer -dates -ext subjectAltName
```

The certificate is just a signed claim: *"I, the CA, attest that this public key belongs to this subject, valid for this window."* Verifying it is exactly the signature verification from §2 — you check the CA's signature over the cert body using the CA's public key.

### 3.3 The chain of trust

You don't trust every CA directly — you trust a small set of **root CAs** whose certificates ship in your OS / browser trust store. Roots sign **intermediates**, intermediates sign **leaf** (end-entity) certs. Verification walks the chain:

```
leaf (api.bank.com)  ──signed by──►  intermediate CA  ──signed by──►  root CA
                                                                        │
                                              in your trust store ◄─────┘ (trust anchor)
```

At each hop you verify the signature with the issuer's public key, check validity dates, check name constraints and key usage, and confirm the leaf's SAN matches the hostname you asked for. The chain terminates at a root you *already* trust out-of-band. **Trust is not infinite recursion; it bottoms out at the trust anchors you pre-installed.**

> **Mental model** — A certificate is a **passport**. The photo+name binding is meaningless on its own, but a passport is trusted because a government you already trust (the CA) signed it with a seal hard to forge. You verify a stranger's passport by recognizing the issuing authority — not by personally knowing the traveler. The root CA is the government whose authority you accepted before any specific traveler showed up.

---

## 4. Short-lived certificates

### 4.1 Why "short-lived" is a security strategy, not an inconvenience

The dangerous thing about any credential is the window during which a leaked copy is still useful. A private key valid for 1 year is a year-long liability: if it leaks on day 1 and you don't notice, the attacker has 364 days. Revocation is supposed to fix this — but revocation is **broken in practice**:

- **CRLs** (Certificate Revocation Lists) grow huge and are fetched lazily.
- **OCSP** adds a latency + availability dependency on every connection, and clients "fail open" (accept the cert) when the OCSP responder is unreachable — so an attacker who blocks OCSP wins.

Short-lived certificates sidestep revocation entirely: **if a cert is only valid for 10 minutes, you don't need to revoke it — you just stop reissuing it.** Validity *is* the kill switch. This is the core idea behind SPIFFE/SPIRE, Istio mTLS, HashiCorp Vault's PKI engine, and cloud workload identity.

### 4.2 The machinery that makes it practical

Short-lived certs are only viable if issuance is **automated and cheap**. The pattern:

```
1. Workload generates a fresh key pair locally (private key never leaves the host).
2. It builds a CSR (Certificate Signing Request) = public key + requested identity,
   self-signed to prove it holds the matching private key.
3. It authenticates to an issuing authority using some *bootstrap* identity
   (a k8s ServiceAccount token, a cloud instance identity doc, a join token).
4. The CA validates the bootstrap identity, then signs a short-TTL cert.
5. An agent/sidecar rotates the cert well before expiry (e.g. at 50–80% of TTL).
```

```bash
# A CSR is itself signed by the requester's private key — proof of possession
openssl req -new -key workload.key -subj "/CN=payments.prod" -out workload.csr
openssl req -in workload.csr -noout -verify -text   # CSR self-signature checks out
```

The chicken-and-egg ("you need an identity to get a credential") is solved by a **bootstrap identity** that the platform can attest natively — the node's cloud identity, or a Kubernetes-projected token bound to the pod. The expensive trust handoff happens once; everything after is automated rotation.

### 4.3 Where Kubernetes does this for you

- **Kubelet TLS bootstrap:** a node joins with a bootstrap token, submits a CSR, and the `csrapprover` controller issues its kubelet client cert. `kubelet` then auto-rotates via `--rotate-certificates`.
- **Projected ServiceAccount tokens:** short-lived (default ~1h), audience-bound JWTs that expire and are refreshed by the kubelet — replacing the old, permanent SA secret tokens.
- **Service meshes (Istio/Linkerd):** issue SPIFFE SVID certs with ~24h or shorter TTL to every sidecar, rotated automatically.

> **Mental model** — A long-lived secret is a **house key**: lose it and you're rekeying locks (revocation) in a panic. A short-lived cert is a **hotel keycard**: it self-expires, so a lost card is worthless tomorrow, and you never run around changing locks — you just don't print a new card for someone who shouldn't have one.

---

## 5. The TLS handshake

TLS gives you three guarantees at once: **confidentiality** (eavesdroppers see ciphertext), **integrity** (tampering is detected), and **authentication** (you're talking to who you think). The handshake is how two strangers go from nothing to a shared symmetric key over a hostile network — without ever transmitting that key.

### 5.1 The basic shape

1. Client says hello, offering supported cipher suites and a fresh random.
2. Server presents its **certificate** (the public-key-to-identity binding from §3) and its own random + key-exchange material.
3. Both sides run a **Diffie–Hellman** key exchange to derive a shared secret **that never crosses the wire**.
4. They switch to fast symmetric encryption (AES-GCM / ChaCha20) for the actual data.

The certificate authenticates the server; Diffie–Hellman establishes the secret; symmetric crypto carries the payload. Asymmetric crypto bootstraps, symmetric crypto does the work — exactly as §1 predicted.

### 5.2 Diffie–Hellman, the part that feels like magic

Both parties derive the same secret while an eavesdropper who saw every byte cannot. With (elliptic-curve) DH:

```
Client picks secret a, sends public  A = a·G   (G = curve base point)
Server picks secret b, sends public  B = b·G
Client computes  a·B = a·(b·G)
Server computes  b·A = b·(a·G)
Both get  a·b·G  ← the shared secret. Eavesdropper has A, B, G
                   but recovering a or b is the elliptic-curve
                   discrete-log problem — infeasible.
```

Using a **fresh** `a`/`b` per connection (*ephemeral* DH, "ECDHE") gives **forward secrecy**: even if the server's long-term private key leaks later, past sessions stay safe because the session secret was never derived from that key — it was thrown away when the connection closed.

### 5.3 TLS 1.3 handshake (the modern default), packet by packet

```
Client                                                   Server
  │  ClientHello                                              │
  │   • supported cipher suites, TLS versions                 │
  │   • key_share: client's ephemeral DH public (A)           │
  │   • client_random                                         │
  │ ────────────────────────────────────────────────────────►│
  │                                                           │
  │                                            ServerHello    │
  │              • chosen cipher suite                        │
  │              • key_share: server ephemeral DH public (B)  │
  │              • server_random                              │
  │   ── from here on, ENCRYPTED with handshake keys ──       │
  │              • EncryptedExtensions                        │
  │              • Certificate           (server's cert chain)│
  │              • CertificateVerify     (server SIGNS the    │
  │                 transcript with its cert's private key —  │
  │                 proves it owns the cert, ties §2 to TLS)  │
  │              • Finished              (MAC over transcript)│
  │ ◄────────────────────────────────────────────────────────│
  │  Finished                                                 │
  │ ────────────────────────────────────────────────────────►│
  │            Application data (AES-GCM / ChaCha20-Poly1305) │
  │ ◄═══════════════════════════════════════════════════════►│
```

Key TLS 1.3 wins over 1.2:

- **1 round-trip (1-RTT)** instead of 2 — both sides send key_share in the first flight, so keys are derived immediately. (0-RTT resumption exists but has replay caveats.)
- **Everything after ServerHello is encrypted**, including the certificate.
- **Only forward-secret (EC)DHE suites remain** — static-RSA key transport is removed, so forward secrecy is mandatory, not optional.
- The `CertificateVerify` step is exactly §2's digital signature: the server signs the handshake transcript, proving it holds the private key for the cert it just presented. **This is the moment "a certificate" becomes "an authenticated peer."**

```bash
# Watch a full handshake, see version, cipher, cert chain, and ALPN
openssl s_client -connect example.com:443 -servername example.com -tls1_3 -msg </dev/null

# Confirm forward secrecy: cipher should be ECDHE-based
openssl s_client -connect example.com:443 </dev/null 2>/dev/null | grep -i 'cipher\|protocol'
```

### 5.4 mTLS — mutual TLS

In ordinary TLS, only the **server** proves identity. In **mutual TLS**, the server also sends a `CertificateRequest`, and the client presents *its own* cert + `CertificateVerify`. Now **both** ends are cryptographically authenticated. This is the backbone of zero-trust service-to-service auth — and, as we'll see, of Kubernetes' own control plane.

> **Mental model** — A TLS handshake is **two spies meeting in the open to agree on a one-time codebook**. They publicly mix numbers (Diffie–Hellman) so that only the two of them end up with the same secret codebook, even though everyone watched the exchange. The certificate is the badge one spy shows to prove they're the real contact and not an impostor; in mTLS, both show badges.

---

## 6. How Kubernetes handles security

Kubernetes security is best understood as **four sequential gates** every request passes through, plus the **PKI** that underpins identity and the **encryption** that protects data at rest. Get the request lifecycle and you've got 80% of it.

```
kubectl / pod ──► [ TLS + AuthN ] ──► [ AuthZ (RBAC) ] ──► [ Admission ] ──► etcd
                  who are you?         are you allowed?     is it valid +     (encrypted
                  (cert / token)       (verb on resource)   policy-compliant?  at rest)
```

### 6.1 The cluster PKI — mTLS everywhere

Every control-plane component talks over **mutual TLS**, anchored by a cluster CA created at bootstrap (`/etc/kubernetes/pki/ca.crt`).

```bash
# The whole cluster's trust fabric lives here
ls /etc/kubernetes/pki/
# ca.crt apiserver.crt apiserver-kubelet-client.crt etcd/ front-proxy-* sa.key sa.pub ...

# Check expiry of every control-plane cert (a real on-call task)
kubeadm certs check-expiration
```

- The **API server** presents a serving cert to clients and uses a client cert to authenticate **to** kubelets and etcd.
- **etcd** runs its own mTLS; only the API server holds a valid client cert, so nothing else can read cluster state directly.
- **kubelets** authenticate to the API server with client certs (bootstrapped and auto-rotated, §4.3).
- The `sa.key`/`sa.pub` pair signs and verifies **ServiceAccount tokens** — those JWTs are signatures (§2) the API server can validate offline.

### 6.2 Gate 1 — Authentication (who are you?)

The API server authenticates every request via one of:

- **Client certificates** — the `CN` is the username, `O` (organization) fields are groups. Used by humans, kubelets, controllers.
- **ServiceAccount tokens** — JWTs signed by `sa.key`. Modern clusters use **projected, audience-bound, short-lived** tokens (§4.3) instead of permanent secrets.
- **OIDC / webhook** — delegate to an external IdP (Okta, Google, Dex) or cloud IAM.

Kubernetes has **no user database.** There is no "create user" API. Identity is entirely external: a cert your CA signed, or a token your IdP issued. This is deliberate — auth is delegated to systems built for it.

```bash
# What identity does the API server see for me?
kubectl auth whoami
# See it the hard way: decode your client cert's subject
kubectl config view --raw -o jsonpath='{.users[0].user.client-certificate-data}' \
  | base64 -d | openssl x509 -noout -subject
```

### 6.3 Gate 2 — Authorization (are you allowed?)

Once authenticated, **RBAC** decides if *this identity* may perform *this verb* (`get`, `list`, `create`, `delete`…) on *this resource* in *this namespace*. Rules are **purely additive** — there are no deny rules; you are denied by default and grants accumulate.

```bash
# The single most useful security command in Kubernetes
kubectl auth can-i create deployments --namespace prod
kubectl auth can-i '*' '*' --as system:serviceaccount:prod:payments   # impersonate to test
```

`Role`/`RoleBinding` are namespaced; `ClusterRole`/`ClusterRoleBinding` are cluster-wide. The discipline is **least privilege**: grant the narrowest verb/resource set that works, scope to a namespace, and never bind to `cluster-admin` for convenience.

### 6.4 Gate 3 — Admission control (is it valid and compliant?)

After authz, **admission controllers** inspect and can mutate or reject the object before it persists:

- **Mutating** webhooks rewrite objects (inject sidecars, set defaults).
- **Validating** webhooks enforce policy (no `:latest` tags, must set resource limits, no privileged pods).
- **Pod Security Admission** enforces the Pod Security Standards (`privileged` / `baseline` / `restricted`) per namespace.
- Policy engines (**OPA/Gatekeeper**, **Kyverno**) express org rules as code.

This is where "no container may run as root" or "every image must be signed" is *enforced*, not merely recommended.

### 6.5 Data at rest, secrets, and workload identity

- **Secrets** are base64-*encoded*, not encrypted, by default — anyone with `get secret` RBAC or etcd access reads them. Enable **encryption at rest** (`EncryptionConfiguration`) with a KMS provider so etcd stores ciphertext.
- **Workload identity** lets a pod's ServiceAccount map to a cloud IAM role (IRSA on EKS, Workload Identity on GKE) — pods get short-lived cloud credentials with **no static keys** in the cluster.
- **NetworkPolicies** (enforced by the CNI) restrict pod-to-pod traffic; a service mesh adds mTLS so identity is cryptographic, not just IP-based.

```bash
# Verify secrets are actually encrypted in etcd (should NOT be human-readable)
ETCDCTL_API=3 etcdctl get /registry/secrets/default/mysecret \
  --cacert=ca.crt --cert=etcd.crt --key=etcd.key | hexdump -C | head
# Look for the "k8s:enc:kms:" prefix → encryption is on
```

> **Mental model** — A Kubernetes request runs an **airport gauntlet**: TLS is the sealed jet bridge, authentication is the **passport check** (who are you), authorization is the **boarding pass** (are you ticketed for *this* flight), and admission control is the **security screening** (is what you're carrying allowed). etcd encryption is the **locked vault** the manifests are stored in afterward. Every gate is independent; failing any one stops you.

---

## 7. How to *guarantee* security

"Is it secure?" is the wrong question — it has no answer. The right question is **"secure against whom, doing what?"** Security is always relative to a **threat model**. Guaranteeing it is a discipline of stating assumptions, enforcing controls, and *verifying* them.

### 7.1 Start with a threat model

Name the adversary and what they can do, then walk each asset. A lightweight frame is **STRIDE**: Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege. For each component ask: *who could do this, and what control stops them?*

| Threat | The control that addresses it |
|--------|-------------------------------|
| Spoofing identity | mTLS / signed tokens (§2, §5.4, §6.2) |
| Tampering with data | Signatures + AEAD integrity (§1, §2) |
| Eavesdropping | TLS confidentiality (§5) |
| Stolen long-lived key | Short-lived certs + forward secrecy (§4, §5.2) |
| Over-broad access | Least-privilege RBAC (§6.3) |
| Malicious workload | Admission + Pod Security + image signing (§6.4) |

### 7.2 Defense in depth

No single control is trusted to be perfect. Layer them so a breach of one doesn't cascade: network policy *and* mTLS *and* RBAC *and* admission *and* encryption at rest. The attacker must defeat **every** layer; you must hold **any** one. That asymmetry is the whole point.

### 7.3 Verify, don't assume

A guarantee you haven't tested is a hope. Make verification routine:

```bash
# Identity & access
kubectl auth can-i --list --as system:serviceaccount:prod:payments
# Cert hygiene
kubeadm certs check-expiration
# Encryption in transit
testssl.sh https://api.example.com         # cipher suites, TLS versions, forward secrecy
# Supply chain: verify an image signature before trusting it
cosign verify --key cosign.pub myregistry/payments:1.4.2
# Cluster posture against known benchmarks
kube-bench run --targets master,node       # CIS Kubernetes Benchmark
```

### 7.4 The principles that generalize

- **Least privilege** — every identity gets the minimum it needs, nothing more.
- **Zero trust** — authenticate and authorize *every* request; never trust the network. The packet from inside the cluster gets the same scrutiny as one from the internet.
- **Defense in depth** — independent layers, so one failure isn't fatal.
- **Fail closed** — when a control can't make a decision, deny. (The opposite of OCSP fail-open.)
- **Reduce the blast radius** — short TTLs, scoped tokens, namespace isolation, so a compromise is small and self-healing.
- **Verifiable, not assumed** — if you can't test the guarantee, you don't have it.

> **Mental model** — You cannot prove a system is *secure* — only that it resists *a specified attacker*. So security work is **threat model → layered controls → continuous verification**, on repeat. Think like a castle: you don't build one infinitely tall wall, you build a moat, an outer wall, an inner keep, and a watch that actually checks — because you assume each layer will eventually be tested.
