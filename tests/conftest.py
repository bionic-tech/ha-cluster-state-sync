"""Shared pytest fixtures for the cluster_state_sync test suite.

Layout note: the integration lives at `custom_components/cluster_state_sync/`,
which is both what Home Assistant's loader expects and what HACS copies on
install.

It used to sit at the top level with a git symlink here pointing back at it.
That kept the shipped directory tidy but made the repository un-installable:
HACS copies `custom_components/<domain>/` out of the repo, and a symlink does
not survive that — it arrives either pointing outside the install root or as a
short text file containing the path. Moved 2026-08-26.
"""

from __future__ import annotations

from collections.abc import Generator
import os
import shutil
import subprocess
import time

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

# The image the fleet actually runs (node-b: penpot-valkey). Testing the
# Lua against a different engine than production would be a false green.
VALKEY_IMAGE = "valkey/valkey:8.1"
VALKEY_ENV_VAR = "CLUSTER_SYNC_TEST_VALKEY"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load custom integrations in every test.

    Home Assistant's test harness refuses to load custom integrations unless
    this fixture is requested. Making it autouse means individual tests don't
    have to remember, which is the usual failure mode for custom-component
    suites.
    """
    yield


def _wait_for_container_valkey(container: str, timeout: float = 30.0) -> None:
    """Block until the containerised server answers PING.

    Deliberately asks over ``docker exec`` rather than a TCP connect: this runs
    in a *session* fixture, and `pytest-socket` — which the suite enables on
    purpose, so a test that quietly reaches a real service fails loudly — has
    already blocked `socket.socket` by the time fixtures run. The
    function-scoped `socket_enabled` fixture cannot help here, and reaching for
    `enable_socket()` would punch a hole in the guard for the whole session.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", container, "valkey-cli", "PING"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0 and "PONG" in probe.stdout:
            return
        last = (probe.stderr or probe.stdout).strip()[:120]
        time.sleep(0.2)
    raise RuntimeError(f"Valkey container {container} never answered PING: {last}")


@pytest.fixture(scope="session")
def valkey_server() -> Generator[tuple[str, int]]:
    """A **real** Valkey for the tests that must execute Lua.

    Every other backend test in this suite asserts against a hand-written
    double that records `eval()` calls and returns a canned answer, so the
    three Lua scripts in `backend.py` have never actually been run by a Redis.
    That is the gap this fixture closes (READINESS T4).

    Resolution order:

    1. ``CLUSTER_SYNC_TEST_VALKEY=host:port`` — an existing server. This is how
       CI supplies one, and how you would point the suite at a real fleet
       instance.
    2. A throwaway ``valkey/valkey:8.1`` container, if docker is available.
    3. **Skip, loudly.** Never silently pass: a suite that quietly stops
       exercising the Lua would report the same green as one that does, which
       is precisely the failure mode this fixture exists to prevent.
    """
    supplied = os.environ.get(VALKEY_ENV_VAR)
    if supplied:
        # No readiness probe: the operator (or CI's service-container health
        # check) supplied this, and probing it here would need sockets the
        # session fixture must not unblock. A server that is not up surfaces as
        # a clear connection error in the first test, which is honest enough.
        host, _, raw_port = supplied.rpartition(":")
        yield host, int(raw_port)
        return

    if not shutil.which("docker"):
        pytest.skip(
            f"No real Valkey available: ${VALKEY_ENV_VAR} is unset and docker is not "
            "on PATH. The Lua scripts in backend.py are NOT being exercised by this "
            "run — the remaining backend tests assert against a double.",
            allow_module_level=True,
        )

    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "127.0.0.1::6379", VALKEY_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(
            f"Could not start {VALKEY_IMAGE}: {started.stderr.strip()[:200]}. "
            "The Lua scripts are NOT being exercised by this run.",
            allow_module_level=True,
        )
    container = started.stdout.strip()

    try:
        mapped = (
            subprocess.run(
                ["docker", "port", container, "6379/tcp"],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .splitlines()[0]
        )
        port = int(mapped.rpartition(":")[2])
        _wait_for_container_valkey(container)
        yield "127.0.0.1", port
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
