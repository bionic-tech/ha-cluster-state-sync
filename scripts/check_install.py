#!/usr/bin/env python3
"""Refuse the installation mistake that took the leader down on 2026-09-09.

Home Assistant scans **every** subdirectory of `custom_components/` and reads
each `manifest.json`. A backup copy of an integration left in there is a
complete integration — manifest included, declaring the same `domain` — so two
directories claim one domain and the resolver produces an empty package path:

    Setup failed for custom integration 'cluster_state_sync':
    Unable to import component: No module named 'custom_components.'

That message names the domain and says nothing about a duplicate, so it reads
like a broken module rather than a directory that should not be there. A
leading dot does not hide a directory from the scan.

🚨 **The integration cannot check this itself.** When the duplicate is present
Home Assistant fails to import us at all, so no runtime guard of ours ever
runs. It has to be checked from outside, which is what this is for.

    python3 scripts/check_install.py /path/to/config
    python3 scripts/check_install.py /mnt/docker_data/homeassistant/config

Exit 0 clean, 1 if anything would stop an integration loading.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys


def _manifest_domain(path: pathlib.Path) -> str | None:
    try:
        return json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("domain")
    except (OSError, ValueError):
        return None


def check(config_dir: str) -> int:
    root = pathlib.Path(config_dir) / "custom_components"
    if not root.is_dir():
        print(f"✗ {root} is not a directory")
        return 1

    by_domain: dict[str, list[pathlib.Path]] = collections.defaultdict(list)
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        domain = _manifest_domain(child)
        if domain:
            by_domain[domain].append(child)

    problems = 0
    for domain, paths in sorted(by_domain.items()):
        if len(paths) > 1:
            problems += 1
            print(f"✗ {len(paths)} directories claim domain {domain!r}:")
            for p in paths:
                note = "the real one" if p.name == domain else "REMOVE or MOVE OUT"
                print(f"    {p.name}   <- {note}")

    # A directory whose name does not match its own manifest is the same
    # hazard waiting to happen: rename it and it starts colliding.
    for domain, paths in sorted(by_domain.items()):
        for p in paths:
            if p.name != domain and len(paths) == 1:
                print(
                    f"⚠ {p.name}/ declares domain {domain!r}. Not currently a conflict, "
                    f"but it is a copy of an integration living where Home Assistant "
                    f"scans — move it outside custom_components/."
                )

    if problems:
        print(
            f"\n{problems} domain(s) claimed twice. Home Assistant will fail to import "
            f"them with 'No module named custom_components.' — move the extra copies "
            f"OUTSIDE custom_components/ entirely, not to a dot-prefixed sibling."
        )
        return 1

    print(f"✓ {len(by_domain)} custom integrations, no domain claimed twice.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(check(sys.argv[1]))
