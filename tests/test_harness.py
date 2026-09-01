"""Harness smoke tests.

These assert that the test environment itself is wired up correctly. They are
deliberately cheap and boring: if these fail, no other test in the suite can be
trusted, so failures here should be read as "the harness is broken", not "the
integration is broken".

The behavioural suite (snapshot round-trip, restore guards, the AR-0001
regression) builds on this in later phases.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.cluster_state_sync.const import (
    DEFAULT_INCLUDE_DOMAINS,
    DOMAIN,
    states_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "custom_components" / "cluster_state_sync" / "manifest.json"


def test_integration_imports() -> None:
    """The integration package imports cleanly under the pinned HA version.

    This is the single most valuable assertion in the file: it catches an HA
    API that moved out from under us the moment we bump the pin, rather than at
    3am on a failover.
    """
    from custom_components.cluster_state_sync import async_setup_entry
    from custom_components.cluster_state_sync.backend import RedisBackend
    from custom_components.cluster_state_sync.config_flow import ConfigFlow

    assert callable(async_setup_entry)
    assert RedisBackend is not None
    assert ConfigFlow is not None


def test_manifest_is_valid_json_and_matches_domain() -> None:
    """manifest.json parses and its domain matches the constant."""
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    # Shape, not value. HACS refuses an integration whose manifest carries no
    # version or a malformed one, so that is the property worth guarding.
    # Pinning the literal only guarantees a red suite on the day someone cuts a
    # release -- a failure that says nothing about whether anything is broken,
    # and trains whoever sees it to edit the test without reading it.
    version = manifest["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"not semver: {version!r}"


def test_redis_requirement_has_an_upper_bound() -> None:
    """AR-0028 — an unpinned major lets a breaking redis release ship silently."""
    manifest = json.loads(MANIFEST.read_text())
    redis_req = next(r for r in manifest["requirements"] if r.startswith("redis"))
    assert "<" in redis_req, f"redis requirement {redis_req!r} has no upper bound"


async def test_home_assistant_loads_the_integration(hass: HomeAssistant) -> None:
    """HA's loader can discover and read the integration through the symlink.

    Proves the `custom_components/` symlink and the `enable_custom_integrations`
    fixture are both doing their job — the two things most likely to be silently
    misconfigured in a custom-component repo.
    """
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.domain == DOMAIN
    assert integration.config_flow is True


def test_key_layout_is_namespaced() -> None:
    """Namespaces must produce distinct keys — the multi-cluster promise."""
    assert states_key("alpha") != states_key("beta")
    assert states_key("alpha").startswith("ha:cluster_state_sync:")


def test_default_include_domains_is_non_empty() -> None:
    """A sanity floor: the default allowlist must actually track something."""
    assert DEFAULT_INCLUDE_DOMAINS
    assert "input_boolean" in DEFAULT_INCLUDE_DOMAINS
