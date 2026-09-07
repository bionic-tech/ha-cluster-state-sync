#!/usr/bin/env python3
"""Dependency audit that distinguishes what we can fix from what we cannot.

`pip-audit -r requirements-dev.txt` walks the whole Home Assistant dependency
tree, and Home Assistant pins its dependencies **exactly** — `cryptography`,
`aiohttp`, `pillow` and `pyjwt` are its choices, not ours. Raising any of them
installs cleanly and then breaks the very version the suite exists to test
against.

So a plain `pip-audit` here is permanently red for reasons nobody in this
repository can act on, and a gate that is always red is a gate everyone learns
to scroll past. This splits the result:

* **Actionable** — a package this repository chooses. Fails the build.
* **Upstream** — pinned by Home Assistant. Reported every run, never hidden,
  but does not fail: the fix is an HA release, and the canary is what tells us
  when one arrives.

Exit code 1 only on the actionable set.
"""

from __future__ import annotations

import json
import subprocess
import sys

# Packages this repository picks a version for. Everything else in the tree
# arrives via `pytest-homeassistant-custom-component`, which pins to match a
# Home Assistant release.
OURS = {
    "redis",
    "hypothesis",
    "ruff",
    "pip-audit",
    "pytest-cov",
    "mutmut",
    "pytest-homeassistant-custom-component",
}


def main() -> int:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            "requirements-dev.txt",
            "--progress-spinner",
            "off",
            "-f",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if not proc.stdout.strip():
        print(f"pip-audit produced no output:\n{proc.stderr[:400]}")
        return 1

    payload = json.loads(proc.stdout)
    deps = payload.get("dependencies", payload) if isinstance(payload, dict) else payload
    vulnerable = [d for d in deps if d.get("vulns")]

    ours = sorted((d for d in vulnerable if d["name"] in OURS), key=lambda d: d["name"])
    upstream = sorted((d for d in vulnerable if d["name"] not in OURS), key=lambda d: d["name"])

    if upstream:
        total = sum(len(d["vulns"]) for d in upstream)
        print(f"Upstream — pinned by Home Assistant, not fixable here ({total} advisories):")
        for d in upstream:
            fixes = sorted({f for v in d["vulns"] for f in (v.get("fix_versions") or [])})
            print(
                f"  {d['name']:<14} {d['version']:<10} "
                f"{len(d['vulns'])} advisor{'y' if len(d['vulns']) == 1 else 'ies'}"
                + (f"  (fixed in {fixes[-1]})" if fixes else "  (no fix published)")
            )
        print("  → resolved by a Home Assistant release; the canary reports when one lands.\n")

    if not ours:
        print("Actionable: none. Every advisory is in a Home Assistant-pinned package.")
        return 0

    print("ACTIONABLE — this repository chooses these versions:")
    for d in ours:
        for v in d["vulns"]:
            fixes = ", ".join(v.get("fix_versions") or ["no fix published"])
            print(f"  {d['name']} {d['version']}  {v['id']}  fix: {fixes}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
