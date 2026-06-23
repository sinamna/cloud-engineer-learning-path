import subprocess
import os

ORDER = [
    "phase1-foundation-gaps",
    "phase2-kubernetes-mastery",
    "phase3-observability-sre",
    "phase4-architecture-design",
    "phase5-security-concepts",
    "phase6-interview-prep",
]

TITLES = {
    "phase1-foundation-gaps": "Phase 1 — Foundation Gaps",
    "phase2-kubernetes-mastery": "Phase 2 — Kubernetes Mastery",
    "phase3-observability-sre": "Phase 3 — Observability & SRE",
    "phase4-architecture-design": "Phase 4 — Architecture & Design",
    "phase5-security-concepts": "Phase 5 — Security Concepts",
    "phase6-interview-prep": "Phase 6 — Interview Prep",
}

SUBS = {
    "phase1-foundation-gaps": "Linux internals, networking, distributed systems, storage",
    "phase2-kubernetes-mastery": "Scheduler, operators, networking, security, autoscaling, etcd",
    "phase3-observability-sre": "Prometheus TSDB, OpenTelemetry, SLO engineering, incident management",
    "phase4-architecture-design": "Multi-region HA, platform engineering, FinOps, zero-trust security",
    "phase5-security-concepts": "Digital signatures, certificates, TLS handshakes, and Kubernetes security",
    "phase6-interview-prep": "System design, behavioral questions, knowledge quiz, common gaps",
}

EYEBROWS = {
    "phase1-foundation-gaps": "01 / 06 — foundation",
    "phase2-kubernetes-mastery": "02 / 06 — kubernetes",
    "phase3-observability-sre": "03 / 06 — observability",
    "phase4-architecture-design": "04 / 06 — architecture",
    "phase5-security-concepts": "05 / 06 — security",
    "phase6-interview-prep": "06 / 06 — interview prep",
}

ACTIVE_KEYS = {
    "phase1-foundation-gaps": "p1active",
    "phase2-kubernetes-mastery": "p2active",
    "phase3-observability-sre": "p3active",
    "phase4-architecture-design": "p4active",
    "phase5-security-concepts": "p5active",
    "phase6-interview-prep": "p6active",
}

# Standalone deep-dive companion pages. These are not part of the linear
# phase progression, so they carry their own metadata and prev/next links
# (typically pointing back to their parent phase) rather than being chained.
DEEPDIVES = {
    "deepdive-networking": {
        "title": "Deep Dive — Networking",
        "sub": "OSI to VXLAN: a packet's full journey through the kernel and Kubernetes",
        "eyebrow": "phase 1 · deep dive — networking",
        "active": "ndactive",
        "prev": ("phase1-foundation-gaps.html", "Phase 1 — Foundation Gaps"),
        "next": ("phase2-kubernetes-mastery.html", "Phase 2 — Kubernetes Mastery"),
    },
}

os.chdir(os.path.dirname(os.path.abspath(__file__)))

for i, name in enumerate(ORDER):
    prevlink = prevtitle = nextlink = nexttitle = None
    if i > 0:
        prevname = ORDER[i - 1]
        prevlink = f"{prevname}.html"
        prevtitle = TITLES[prevname]
    if i < len(ORDER) - 1:
        nextname = ORDER[i + 1]
        nextlink = f"{nextname}.html"
        nexttitle = TITLES[nextname]

    cmd = [
        "pandoc", f"src/{name}.md",
        "-f", "markdown",
        "-t", "html5",
        "--template=assets/template.html",
        "-V", f"title={TITLES[name]}",
        "-V", f"subtitle={SUBS[name]}",
        "-V", f"eyebrow={EYEBROWS[name]}",
        "-V", "cssroot=../",
        "-V", "navfoot=1",
    ]

    # Active nav marker
    for key in ACTIVE_KEYS.values():
        if key == ACTIVE_KEYS[name]:
            cmd += ["-V", f'{key}= class="active"']

    if prevlink:
        cmd += ["-V", f"prevlink={prevlink}", "-V", f"prevtitle={prevtitle}"]
    if nextlink:
        cmd += ["-V", f"nextlink={nextlink}", "-V", f"nexttitle={nexttitle}"]

    cmd += ["-o", f"phases/{name}.html"]

    print(f"Converting {name}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ERROR:", result.stderr)
    else:
        print("  OK")

for name, cfg in DEEPDIVES.items():
    cmd = [
        "pandoc", f"src/{name}.md",
        "-f", "markdown",
        "-t", "html5",
        "--template=assets/template.html",
        "-V", f"title={cfg['title']}",
        "-V", f"subtitle={cfg['sub']}",
        "-V", f"eyebrow={cfg['eyebrow']}",
        "-V", "cssroot=../",
        "-V", "navfoot=1",
        "-V", f'{cfg["active"]}= active',
    ]
    prevlink, prevtitle = cfg["prev"]
    nextlink, nexttitle = cfg["next"]
    cmd += ["-V", f"prevlink={prevlink}", "-V", f"prevtitle={prevtitle}"]
    cmd += ["-V", f"nextlink={nextlink}", "-V", f"nexttitle={nexttitle}"]
    cmd += ["-o", f"phases/{name}.html"]

    print(f"Converting {name}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ERROR:", result.stderr)
    else:
        print("  OK")

print("\nDone.")
