#!/usr/bin/env python3
"""Every Home Assistant release this integration claims to support.

`hacs.json` declares a **minimum** Home Assistant version, which is a promise
about every release from there upwards — not about the one release the test
suite happens to pin. Home Assistant ships monthly, so that promise grows by one
untested version every month unless something goes and checks.

This prints the latest patch of each minor from the declared floor to the newest
on PyPI, so CI can run the suite against all of them. It derives the list rather
than hardcoding it, because a hardcoded matrix is a matrix that stops including
the release that broke you.

    python scripts/ha_version_matrix.py            # human readable
    python scripts/ha_version_matrix.py --json     # {"ha":["2026.6.4",...]} for CI

🚨 Testing only the pin and the newest is not enough, and that is not
hypothetical: the 2026.9 series replaced `voluptuous` with `probatio`, and
"works on the floor, works on the newest" says nothing about whether the two
releases in between behave like either.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
PYPI = "https://pypi.org/pypi/homeassistant/json"

_VERSION = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")


def floor_version() -> tuple[int, int, int]:
    """The minimum this integration promises, from hacs.json."""
    declared = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]
    m = _VERSION.match(declared)
    if not m:
        raise SystemExit(f"hacs.json declares an unparseable version: {declared!r}")
    return tuple(int(p) for p in m.groups())  # type: ignore[return-value]


def released(fetch=None) -> list[tuple[int, int, int]]:
    """Every released Home Assistant version, parsed. `fetch` is for tests."""
    if fetch is None:

        def fetch() -> str:
            with urllib.request.urlopen(PYPI, timeout=30) as r:  # noqa: S310
                return r.read().decode()

    data = json.loads(fetch())
    out = []
    for raw, files in data["releases"].items():
        # A yanked or fileless release cannot be installed, so it is not a
        # version anyone can be running.
        if not files or all(f.get("yanked") for f in files):
            continue
        if m := _VERSION.match(raw):
            out.append(tuple(int(p) for p in m.groups()))
    return sorted(out)


def matrix(floor: tuple[int, int, int], versions: list[tuple[int, int, int]]) -> list[str]:
    """Latest patch of every minor at or above the floor.

    The floor's own patch level is respected: a floor of 2026.6.4 means 2026.6.4
    and up, so 2026.6.3 is not in the supported range even though it is a patch
    of the same minor.
    """
    newest: dict[tuple[int, int], tuple[int, int, int]] = {}
    for v in versions:
        if v < (floor[0], floor[1], 0):
            continue
        if v[:2] == floor[:2] and v < floor:
            continue
        key = v[:2]
        if key not in newest or v > newest[key]:
            newest[key] = v
    return [".".join(str(p) for p in v) for v in sorted(newest.values())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a GitHub Actions matrix")
    args = parser.parse_args(argv)

    floor = floor_version()
    supported = matrix(floor, released())

    if args.json:
        print(json.dumps({"ha": supported}))
        return 0

    print(f"hacs.json floor: {'.'.join(str(p) for p in floor)}")
    print(f"Supported releases to test ({len(supported)}):")
    for v in supported:
        print(f"  {v}")
    if len(supported) < 2:
        print("\nOnly one release in range — the floor is the newest.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
