# Cloud Engineer Deep Dive

A six-phase study system for senior cloud engineering, published as a static site.

**Live site:** https://sinamna.github.io/cloud-engineer-learning-path/

## Contents

| Phase | Topics |
|-------|--------|
| 01 — Foundation Gaps | Linux internals, namespaces & cgroups, networking/eBPF, distributed systems, storage |
| 02 — Kubernetes Mastery | Scheduler, operators & CRDs, networking, security, autoscaling, etcd |
| 03 — Observability & SRE | Prometheus TSDB, OpenTelemetry, SLO engineering, incident management |
| 04 — Architecture & Design | Multi-region HA, platform engineering, FinOps, zero-trust security |
| 05 — Security Concepts | Digital signatures, certificates & PKI, TLS handshakes, Kubernetes security |
| 06 — Interview Prep | System design, behavioral questions, knowledge quiz, common gaps |

## Structure

```
index.html            — landing page
phases/*.html         — rendered phase documents (what GitHub Pages serves)
src/*.md              — original Markdown source for each phase
assets/style.css      — shared stylesheet
assets/template.html  — pandoc template used to generate phases/*.html
build.py              — regenerates phases/*.html from src/*.md
```

## Rebuilding the site

Requires [pandoc](https://pandoc.org/).

```bash
python3 build.py
```

This regenerates every file in `phases/` from the corresponding Markdown file in `src/`.

## Deployment

Served via GitHub Pages from the repository root on the `main` branch.
