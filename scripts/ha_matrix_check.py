#!/usr/bin/env python3
"""Run the suite against every Home Assistant release we claim to support.

`hacs.json` declares a minimum, which is a promise about every release above it.
This proves it, locally, because there is no CI credit and a gate that cannot
execute is not a gate.

    python scripts/ha_matrix_check.py                 # every supported release
    python scripts/ha_matrix_check.py 2026.9.2        # just one
    python scripts/ha_matrix_check.py --rebuild       # discard cached venvs

Each version gets its own cached virtualenv under `.ha-matrix/`, so the first
run is slow (a full Home Assistant install per version) and later ones are not.

🚨 **It refuses to run on an interpreter Home Assistant cannot use.**

That is not defensive programming, it is a trap this repository fell into:
building the environment with the system `python3` (3.12) did not fail. pip
walked BACKWARDS to the newest release that interpreter could run — Home
Assistant 2025.1.4, twenty months behind the pin — and the suite went green
against it, proving nothing whatsoever. A wrong answer that looks like a right
one is the failure mode this project exists to catch.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_version_matrix  # noqa: E402

REPO = ha_version_matrix.REPO
CACHE = REPO / ".ha-matrix"

#: Home Assistant 2026.x requires this. Read from the running interpreter rather
#: than hardcoded so it cannot drift from reality silently.
MINIMUM_PYTHON = (3, 14, 2)


def _assert_interpreter_can_run_home_assistant() -> None:
    if sys.version_info[:3] < MINIMUM_PYTHON:
        want = ".".join(str(p) for p in MINIMUM_PYTHON)
        raise SystemExit(
            f"refusing to run: this is Python {sys.version.split()[0]}, and Home "
            f"Assistant 2026.x needs >= {want}.\n\n"
            "pip would NOT fail here. It would resolve backwards to the newest "
            "Home Assistant this interpreter can run — which on one attempt meant "
            "2025.1.4, twenty months behind the pin — and the suite would pass "
            "against it while telling you nothing.\n\n"
            "Run this with the same interpreter the project's .venv uses."
        )


def _venv_for(version: str, rebuild: bool) -> pathlib.Path:
    env = CACHE / version
    python = env / "bin" / "python"
    if rebuild and env.exists():
        shutil.rmtree(env)
    if python.exists():
        return env

    CACHE.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    pip = [str(env / "bin" / "pip"), "install", "-q"]
    subprocess.run([*pip, "--upgrade", "pip"], check=True)

    # The harness pins an exact core, so naming the core lets the resolver pick
    # the harness rather than the other way round.
    subprocess.run(
        [*pip, f"homeassistant=={version}", "pytest-homeassistant-custom-component"],
        check=True,
    )
    without_pin = CACHE / "requirements-nopin.txt"
    without_pin.write_text(
        "\n".join(
            line
            for line in (REPO / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
            if not line.startswith("pytest-homeassistant-custom-component==")
        ),
        encoding="utf-8",
    )
    subprocess.run([*pip, "-r", str(without_pin)], check=True)

    got = subprocess.run(
        [str(python), "-c", "import homeassistant.const as c;print(c.__version__)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if got != version:
        raise SystemExit(
            f"asked for Home Assistant {version} and got {got}. Refusing to report a "
            "result for a version that is not installed."
        )
    return env


def _deselect() -> list[str]:
    """Skip the tests about publishing this repository.

    🚨 This matrix asks one question: does the INTEGRATION work on each Home
    Assistant release we promise to support. The export allowlist, the port-back
    ledger and the history replay do not import Home Assistant at all, so
    running them once per version answers nothing and costs four times over.

    Worse, they need a full git clone — tags, signing config, a working tree.
    Run them from a copy without `.git/` and they fail, which is how the first
    run of this reported all four versions broken when every one of them was
    fine. The set comes from `export_public.EXCLUDED_FROM_PUBLIC`, so it is the
    same list that decides what ships: the tests a contributor gets are exactly
    the tests that matter here.
    """
    import export_public

    return [
        arg for path in sorted(export_public.EXCLUDED_FROM_PUBLIC) for arg in ("--ignore", path)
    ]


def run(version: str, rebuild: bool) -> tuple[bool, str]:
    env = _venv_for(version, rebuild)
    python = str(env / "bin" / "python")

    compat = subprocess.run(
        [python, str(REPO / "scripts" / "ha_compat_check.py")],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    schema = next(
        (ln.strip() for ln in compat.stdout.splitlines() if "schema" in ln.lower()), "schema: ?"
    )
    suite = subprocess.run(
        [python, "-m", "pytest", "-q", "--timeout=300", "-p", "no:cacheprovider", *_deselect()],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    tail = next(
        (ln for ln in reversed(suite.stdout.splitlines()) if "passed" in ln or "failed" in ln),
        "no result",
    )
    ok = suite.returncode == 0 and compat.returncode == 0
    detail = f"{tail.strip()}  |  {schema.lstrip('✅ ').strip()}"
    if not ok:
        # 🚨 A verdict with no evidence is not a result. The first version of
        # this printed "FAIL" and the summary line, and it took a separate run
        # inside the container to discover the failures were all repository
        # machinery rather than anything to do with Home Assistant.
        named = [ln for ln in suite.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))]
        detail += "\n" + "\n".join(f"      {ln}" for ln in named[:15])
        if len(named) > 15:
            detail += f"\n      ... and {len(named) - 15} more"
    return ok, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions", nargs="*", help="limit to these versions")
    parser.add_argument("--rebuild", action="store_true", help="discard cached venvs first")
    args = parser.parse_args(argv)

    _assert_interpreter_can_run_home_assistant()

    wanted = args.versions or ha_version_matrix.matrix(
        ha_version_matrix.floor_version(), ha_version_matrix.released()
    )
    print(f"Supported releases to verify: {', '.join(wanted)}\n")

    failures = []
    for version in wanted:
        print(f"  {version} ... ", end="", flush=True)
        ok, detail = run(version, args.rebuild)
        print(("PASS  " if ok else "FAIL  ") + detail)
        if not ok:
            failures.append(version)

    print()
    if failures:
        print(f"✗ failed against: {', '.join(failures)}")
        print("  Anyone running these installed it because hacs.json said they could.")
        return 1
    print(f"✓ all {len(wanted)} supported releases pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
