#!/usr/bin/env python3
"""Fail if anything other than nginx publishes a port.

This exists because Docker does not respect ufw. `docker compose` writes its own
iptables rules in the DOCKER chain, which is consulted before the FORWARD chain
ufw manages, so a published port is reachable from the internet even when
`ufw status` swears it is denied. On a VPS that means a single stray `ports:`
entry silently exposes Postgres -- with the default password -- to the world.

Binding to 127.0.0.1 would also work, but it is one missing prefix away from
0.0.0.0 and the mistake is invisible in review. Not publishing at all is the
rule here, and this script is what enforces it.

    make compose-check
"""

from __future__ import annotations

import json
import subprocess
import sys

#: The edge proxy, and nothing else.
ALLOWED = {"nginx"}


def main() -> int:
    try:
        raw = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        print("docker not installed; skipping compose exposure check")
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"could not read compose config:\n{exc.stderr}", file=sys.stderr)
        return 2

    services = json.loads(raw).get("services", {})
    published = {
        name: svc["ports"] for name, svc in sorted(services.items()) if svc.get("ports")
    }

    for name, svc in sorted(services.items()):
        ports = svc.get("ports") or []
        detail = ", ".join(f"{p.get('published')}->{p.get('target')}" for p in ports)
        print(f"  {name:<10} {'PUBLISHED' if ports else 'internal only':<14} {detail}")

    offenders = sorted(set(published) - ALLOWED)
    print()
    if offenders:
        print(f"FAIL: these services publish ports but must not: {', '.join(offenders)}")
        print(
            "Docker bypasses ufw, so a published port here is reachable from the\n"
            "internet whatever the firewall says. Remove the `ports:` entry and\n"
            "reach the service by its compose service name instead."
        )
        return 1

    missing = sorted(ALLOWED - set(published))
    if missing:
        print(f"FAIL: {', '.join(missing)} publishes nothing; the app is unreachable.")
        return 1

    print(f"PASS: {', '.join(sorted(published))} is the only published service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
