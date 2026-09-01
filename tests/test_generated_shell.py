"""Every generated shell script, through shellcheck — READINESS T11.

`bash -n` only catches syntax. These scripts run **as root, from Keepalived,
during a promotion** — the moment when nobody is watching and a mistake costs
the failover. Unquoted expansions, silent word-splitting and assigned-but-unused
variables are exactly the class of bug that survives a syntax check and bites
later.

Found on its first run: the leader bridge ruleset assigned `HA_IP` and never
used it, because only the follower re-adds restrictions. Harmless, but a reader
hitting it has to stop and work out whether a rule is missing.

Skips loudly when shellcheck is absent, rather than passing quietly — a green
run that silently stopped checking is the failure this exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from custom_components.cluster_state_sync.bundle import build_bundle
from custom_components.cluster_state_sync.const import TOPOLOGY_COLD, TOPOLOGY_WARM

pytestmark = pytest.mark.skipif(
    shutil.which("shellcheck") is None,
    reason="shellcheck is not installed — the generated scripts are NOT being "
    "checked in this run. CI installs it; see .github/workflows/ci.yml.",
)

COLD = {
    "topology_model": TOPOLOGY_COLD,
    "node_id": "tiger1",
    "peer_host": "tiger2.lan",
    "ha_container": "homeassistant",
    "cluster_namespace": "default",
}
WARM = {
    **COLD,
    "topology_model": TOPOLOGY_WARM,
    "iot_subnets": "192.168.50.0/24, 10.20.0.0/16",
    "block_discovery": True,
    "docker_network": "all",
    "ha_uid": 1000,
    "ha_container_ip": "192.168.1.60",
    "settle_delay": 15,
}
# The fileset pull and swap scripts are gated behind CONF_FILESET_ENABLED and
# do not appear in COLD or WARM above, so without this variant they would
# never actually reach shellcheck -- a green run here would be checking
# everything except the two scripts a promotion depends on most.
FILESET = {
    **COLD,
    "fileset_enabled": True,
    "cluster_secret": "s3cr3t-for-tests",
}
# The authenticated, TLS variant. Without it the pull script's password block
# (a `source`, an `export` and an early exit) and its TLS flags never reach
# shellcheck at all -- and an unquoted expansion there runs as root, from a
# timer, on the node standing by to take over. `source`-ing a variable path is
# exactly the construct shellcheck has an opinion about.
FILESET_AUTH = {
    **FILESET,
    "redis_username": "ha-cluster-sync",
    "redis_password": "valkey-p4ssword",
    "redis_db": 2,
    "redis_use_tls": True,
    "redis_tls_ca_certs": "/ssl/valkey-ca.crt",
}


def _scripts(cfg: dict) -> list[tuple[str, str]]:
    return [(n, b) for n, b in build_bundle(cfg).items() if n.endswith(".sh")]


@pytest.mark.parametrize(
    "model",
    ["cold", "warm", "fileset", "fileset-auth"],
    ids=["cold", "warm", "fileset", "fileset-auth"],
)
def test_every_generated_script_passes_shellcheck(model: str) -> None:
    cfg = {"cold": COLD, "warm": WARM, "fileset": FILESET, "fileset-auth": FILESET_AUTH}[model]
    scripts = _scripts(cfg)
    assert scripts, "the bundle emitted no shell scripts at all"

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in scripts:
            path = Path(tmp) / name
            path.write_text(body, encoding="utf-8")
            result = subprocess.run(
                ["shellcheck", "--severity=warning", "--format=gcc", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                failures.append(f"{name}:\n{result.stdout.strip()}")

    assert not failures, "shellcheck findings:\n\n" + "\n\n".join(failures)


@pytest.mark.parametrize(
    "cfg, artefact",
    [
        pytest.param(COLD, "ha-device-preflight.py", id="preflight"),
        pytest.param(FILESET, "cluster-fileset-identity.py", id="identity"),
        pytest.param(FILESET, "fileset_pull.py", id="pull"),
    ],
)
def test_the_generated_python_is_syntactically_valid(cfg: dict, artefact: str) -> None:
    """These ship as Python, so `bash -n` and shellcheck never see them.

    All three run on the host during a promotion, from a script that logs a
    failure and carries on — so a broken shipped copy would not stop anything,
    it would just quietly not do its job. `cluster-fileset-identity.py` not
    doing its job means the promoted node comes up as its peer.
    """
    import ast

    ast.parse(build_bundle(cfg)[artefact])  # SyntaxError if the shipped copy is broken
