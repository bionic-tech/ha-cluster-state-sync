"""Sidebar panel registration.

The integration exposes a dozen diagnostic entities and, until now, nothing
that presented them: whether a failover was healthy could only be answered by
reading logs on two hosts. A Lovelace YAML view would have meant pasting a file
and hand-editing the node id out of eleven entity names, on every install.

So the panel registers itself. `panel_custom.async_register_panel` and
`hass.http.async_register_static_paths` are both public Home Assistant APIs --
which matters here, because this integration's whole design premise is that it
survives minor HA bumps (see CLAUDE.md). Reaching into Lovelace's dashboards
collection would have worked too, and would have broken that promise.

Registration is best-effort by design. A sidebar entry is a convenience; a
failure to create one must never stop the thing that actually keeps the house
running from starting.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

#: Sidebar path, and the URL the module is served from. Both are namespaced to
#: this integration so nothing else on the instance can collide with them.
PANEL_URL_PATH = "cluster-status"

#: The file on disk, and the URL derived from it -- derived rather than typed
#: twice, because a drift between them is a blank panel with nothing logged.
PANEL_FILE = "cluster_status_panel.js"
PANEL_STATIC_PATH = "/cluster_state_sync_panel"
PANEL_MODULE_URL = f"{PANEL_STATIC_PATH}/{PANEL_FILE}"


def _module_url(panel_dir: str) -> str:
    """The module URL, with a content hash so a browser cannot serve a stale one.

    Browsers cache ES modules hard, and a hard refresh frequently does not
    clear them. That cost a real debugging session: a corrected panel was
    deployed, served, and verified over HTTP, while the browser kept running
    the previous build -- and the symptom (headings render, every row missing)
    looked exactly like a fresh bug in the new code rather than a stale copy of
    the old one.

    `cache_headers=False` on the static path is not enough on its own. A URL
    that changes when the file changes is, because the browser has never seen
    it before.
    """
    try:
        digest = hashlib.sha256(pathlib.Path(panel_dir, PANEL_FILE).read_bytes()).hexdigest()[:10]
    except OSError:
        # Unreadable is the static-path registration's problem, not ours; fall
        # back to the bare URL rather than failing the whole panel over a hash.
        return PANEL_MODULE_URL
    return f"{PANEL_MODULE_URL}?v={digest}"


#: Must match `customElements.define(...)` in the JS. A mismatch is a blank
#: panel with nothing in the log, so it is asserted by a test rather than
#: trusted to stay in step.
PANEL_COMPONENT_NAME = "cluster-status-panel"


async def async_register_panel(hass: HomeAssistant) -> bool:
    """Serve the panel module and put it in the sidebar. Never raises.

    Returns True when the panel was registered, False when it was already
    there or could not be registered -- the caller logs, it does not fail.
    """
    try:
        # Imported HERE, not at module scope, and that is deliberate. This
        # module is imported by `__init__.py` as the integration loads, so an
        # ImportError at module scope would escape the guard below and take the
        # whole integration down -- to fail at putting an icon in a sidebar.
        # `panel_custom` is not declared as a manifest dependency for the same
        # reason: a hard dependency that fails means HA refuses to set us up at
        # all, which is a worse outcome than no panel.
        from homeassistant.components import panel_custom
        from homeassistant.components.http import StaticPathConfig

        # Registering the same panel twice raises; HA has no "exists?" helper
        # that is public, so the sentinel lives in our own data bucket.
        if hass.data.get(_REGISTERED_KEY):
            return False

        source = f"{hass.config.path()}/custom_components/cluster_state_sync/panel"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    PANEL_STATIC_PATH,
                    source,
                    # Cached aggressively by the browser otherwise, which makes
                    # an updated panel look like a broken one after an upgrade.
                    cache_headers=False,
                )
            ]
        )
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_COMPONENT_NAME,
            module_url=await hass.async_add_executor_job(_module_url, source),
            sidebar_title="Cluster",
            sidebar_icon="mdi:server-network",
            # Cluster health names hosts and leadership; it is operator
            # information, not household information.
            require_admin=True,
        )
        hass.data[_REGISTERED_KEY] = True
    except Exception:  # noqa: BLE001 -- a sidebar entry must never break setup
        _LOGGER.warning(
            "Could not register the Cluster panel; the integration is unaffected "
            "and its entities are still available. Use dashboards/cluster-status.yaml "
            "if you want the view.",
            exc_info=True,
        )
        return False
    return True


_REGISTERED_KEY = "cluster_state_sync_panel_registered"
