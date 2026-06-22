import subprocess
import os

ORDER = [
    "phase1-foundation-gaps",
    "phase2-kubernetes-mastery",
    "phase3-observability-sre",
    "phase4-architecture-design",
    "phase5-interview-prep",
]

TITLES = {
    "phase1-foundation-gaps": "Phase 1 — Foundation Gaps",
    "phase2-kubernetes-mastery": "Phase 2 — Kubernetes Mastery",
    "phase3-observability-sre": "Phase 3 — Observability & SRE",
    "phase4-architecture-design": "Phase 4 — Architecture & Design",
    "phase5-interview-prep": "Phase 5 — Interview Prep",
}

SUBS = {
    "phase1-foundation-gaps": "Linux internals, networking, distributed systems, storage",
    "phase2-kubernetes-mastery": "Scheduler, operators, networking, security, autoscaling, etcd",
    "phase3-observability-sre": "Prometheus TSDB, OpenTelemetry, SLO engineering, incident management",
    "phase4-architecture-design": "Multi-region HA, platform engineering, FinOps, zero-trust security",
    "phase5-interview-prep": "System design, behavioral questions, knowledge quiz, common gaps",
}

EYEBROWS = {
    "phase1-foundation-gaps": "01 / 05 — foundation",
    "phase2-kubernetes-mastery": "02 / 05 — kubernetes",
    "phase3-observability-sre": "03 / 05 — observability",
    "phase4-architecture-design": "04 / 05 — architecture",
    "phase5-interview-prep": "05 / 05 — interview prep",
}

ACTIVE_KEYS = {
    "phase1-foundation-gaps": "p1active",
    "phase2-kubernetes-mastery": "p2active",
    "phase3-observability-sre": "p3active",
    "phase4-architecture-design": "p4active",
    "phase5-interview-prep": "p5active",
}

os.chdir("/home/claude/site")

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

print("\nDone.")
