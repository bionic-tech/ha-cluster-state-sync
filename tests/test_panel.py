"""The sidebar panel.

The integration exposes a dozen diagnostic entities and shipped nothing that
showed them, so whether a failover was healthy could only be answered from logs
on two hosts. These tests guard the two things that make the panel worth having:
that it registers itself (no paste, no hand-edited node id), and that failing to
register can never take the integration down with it.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from custom_components.cluster_state_sync import panel

PANEL_JS = (
    Path(__file__).parent.parent / "custom_components/cluster_state_sync/panel" / panel.PANEL_FILE
)


def test_the_module_url_matches_the_file_that_is_shipped() -> None:
    """A drift here is a blank panel with nothing in the log.

    The URL is derived from PANEL_FILE rather than typed twice, and the file
    has to actually exist — renaming one without the other is exactly the kind
    of silent breakage this repo keeps finding.
    """
    assert PANEL_JS.is_file(), f"{panel.PANEL_FILE} is referenced but not shipped"
    assert panel.PANEL_MODULE_URL.endswith(panel.PANEL_FILE)
    assert panel.PANEL_MODULE_URL.startswith(panel.PANEL_STATIC_PATH)


def test_the_web_component_name_matches_the_javascript() -> None:
    """Home Assistant loads the module and then instantiates this exact tag.

    If they disagree the panel is blank, the sidebar entry still appears, and
    nothing is logged — so it is asserted rather than trusted.
    """
    js = PANEL_JS.read_text(encoding="utf-8")
    assert f'customElements.define("{panel.PANEL_COMPONENT_NAME}"' in js


def test_the_panel_discovers_entities_rather_than_naming_them() -> None:
    """Every entity carries the id of the node that created it.

    A hard-coded list would be correct on exactly one installation. The YAML
    dashboard needed eleven hand-edits; the panel must not reintroduce that.
    """
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "cluster_sync_" in js and "match(" in js, "must derive node ids from entity ids"
    # No node id may be baked into a shipped file. Asserting on the shape
    # rather than on this fleet's own hostname keeps the test meaningful in the
    # published copy, where that hostname does not appear to begin with.
    baked = re.findall(r"[\"'][a-z][a-z0-9-]*-[0-9a-f]{6}[\"']", js)
    assert not baked, f"a node id is hardcoded in the panel: {baked}"


def test_registration_failure_never_breaks_setup() -> None:
    """A convenience view must not be able to stop the cluster starting.

    Production change this catches: letting the exception out of
    `async_register_panel`, or making setup depend on its return value.
    """

    class _Boom:
        def path(self, *_a: object) -> str:
            raise RuntimeError("no config dir")

    class _Hass:
        data: dict[str, object] = {}
        config = _Boom()

    import asyncio

    assert asyncio.run(panel.async_register_panel(_Hass())) is False


def test_registering_twice_is_a_no_op() -> None:
    """HA raises on a duplicate panel, and setup can run again after a reload."""

    class _Hass:
        data: dict[str, object] = {panel._REGISTERED_KEY: True}

    import asyncio

    assert asyncio.run(panel.async_register_panel(_Hass())) is False


@pytest.mark.parametrize("needle", ["--primary-text-color", "--card-background-color"])
def test_panel_uses_home_assistant_theme_variables(needle: str) -> None:
    """Hard-coded colours look wrong in half of all installations.

    Using HA's own CSS variables is what makes the panel legible in dark mode
    without shipping a second stylesheet.
    """
    assert needle in PANEL_JS.read_text(encoding="utf-8")


def test_the_panel_module_imports_nothing_at_module_scope_that_can_fail() -> None:
    """This module is imported as the integration loads.

    An ImportError at module scope escapes `async_register_panel`'s guard
    entirely and takes the whole integration down -- to fail at putting an icon
    in a sidebar. So the frontend imports live inside the function, and
    `panel_custom` is deliberately NOT a manifest dependency: a hard dependency
    that fails means Home Assistant refuses to set us up at all.
    """
    import json
    from pathlib import Path

    src = Path(panel.__file__).read_text(encoding="utf-8")
    head = src.split("async def async_register_panel")[0]
    for forbidden in (
        "from homeassistant.components import panel_custom",
        "from homeassistant.components.http import",
    ):
        assert forbidden not in head, f"{forbidden!r} at module scope can break integration load"

    manifest = json.loads(
        (Path(panel.__file__).parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert "panel_custom" not in (manifest.get("dependencies") or []), (
        "a hard dependency on panel_custom means no panel becomes no integration"
    )


# The entity ids this fleet actually produces. Captured from a live registry,
# because the first parser was written against what the names looked like they
# ought to be and shipped a panel with every card empty.
REAL_ENTITY_IDS = [
    "binary_sensor.cluster_sync_node-a_f5d04f_backend",
    "binary_sensor.cluster_sync_node-a_f5d04f_fileset_degraded",
    "binary_sensor.cluster_sync_node-a_f5d04f_is_leader",
    "sensor.cluster_sync_node-a_f5d04f_entities_tracked",
    "sensor.cluster_sync_node-a_f5d04f_last_snapshot_age",
    "sensor.cluster_sync_node-a_f5d04f_entities_restored",
    "sensor.cluster_sync_node-a_f5d04f_fileset_age",
    "sensor.cluster_sync_node-a_f5d04f_cluster_leader",
    "sensor.cluster_sync_node-a_f5d04f_shared_snapshot_age",
    "binary_sensor.system_cluster_sync_node-a_f5d04f_maintenance_hold",
    "sensor.system_cluster_sync_node-a_f5d04f_cluster_members",
    "sensor.system_cluster_sync_node-a_f5d04f_clock_skew",
    "sensor.system_cluster_sync_node-a_f5d04f_unreplicated_config_references",
    "switch.cluster_sync_node-a_f5d04f_maintenance_hold",
]


def _parse_like_the_panel(entity_ids: list[str]) -> dict[str, dict[str, str]]:
    """Mirror the panel's grouping, using the panel's own METRICS list.

    There is no JS runtime here, so the list is read out of the shipped file
    rather than duplicated -- a metric added to the panel and not to this test
    would otherwise pass while the card stayed blank.
    """
    import re

    src = PANEL_JS.read_text(encoding="utf-8")
    block = re.search(r"const METRICS = \[(.*?)\]", src, re.S)
    assert block, "METRICS list not found in the panel"
    metrics = sorted(re.findall(r'"([a-z_]+)"', block.group(1)), key=len, reverse=True)
    pattern = re.compile(r"^(?:binary_sensor|sensor|switch)\.(?:system_)?cluster_sync_(.+)$")

    nodes: dict[str, dict[str, str]] = {}
    for eid in entity_ids:
        m = pattern.match(eid)
        if not m:
            continue
        rest = m.group(1)
        metric = next((k for k in metrics if rest == k or rest.endswith("_" + k)), None)
        if not metric:
            continue
        node = rest[: len(rest) - len(metric)].rstrip("_")
        key = f"{metric}__switch" if eid.startswith("switch.") else metric
        nodes.setdefault(node, {})[key] = eid
    return nodes


def test_the_panel_parses_the_entity_ids_this_fleet_really_produces() -> None:
    """The shipped bug: a node id is NOT separable by pattern.

    `node-a` becomes `node-a_f5d04f` in an entity id, so a node id
    is indistinguishable from a metric name once both are underscore-separated.
    A non-greedy split on the first underscore gave node="node-a" and
    metric="f5d04f_is_leader"; nothing matched and every card rendered empty,
    with no error anywhere.
    """
    nodes = _parse_like_the_panel(REAL_ENTITY_IDS)
    assert list(nodes) == ["node-a_f5d04f"], f"node parsed wrong: {list(nodes)}"
    found = nodes["node-a_f5d04f"]
    assert len(found) == len(REAL_ENTITY_IDS), "every entity must land somewhere"
    for required in (
        "is_leader",
        "cluster_leader",  # must win over is_leader: longest match first
        "maintenance_hold",
        "maintenance_hold__switch",  # the control, kept apart from the readout
        "unreplicated_config_references",
        "clock_skew",
    ):
        assert required in found, f"{required} did not parse"


def test_a_hyphenated_node_id_survives_and_so_does_a_second_node() -> None:
    """One card per node is the whole point on a two-node cluster."""
    nodes = _parse_like_the_panel(
        [
            "binary_sensor.cluster_sync_node-a_f5d04f_is_leader",
            "binary_sensor.cluster_sync_node-b_4554b2_is_leader",
            "sensor.system_cluster_sync_node-b_4554b2_clock_skew",
        ]
    )
    assert sorted(nodes) == ["node-a_f5d04f", "node-b_4554b2"]


def test_the_hold_toggle_is_wired_to_the_switch_not_the_sensor() -> None:
    """A button bound to the read-only binary_sensor would do nothing at all."""
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "maintenance_hold__switch" in js
    assert "callService" in js and '"switch"' in js


def test_the_module_url_carries_a_content_hash() -> None:
    """Browsers cache ES modules hard, and a hard refresh often will not clear
    them.

    This cost a real session: a corrected panel was deployed, served, and
    verified over HTTP while the browser kept running the previous build. The
    symptom — headings render, every row missing — looked like a fresh bug in
    the new code rather than a stale copy of the old one. The give-away was the
    node heading showing the value only the OLD parser could produce.

    `cache_headers=False` is not enough on its own; a URL the browser has never
    seen is.
    """
    url = panel._module_url(str(PANEL_JS.parent))
    assert "?v=" in url, "no cache-busting token"
    assert url.split("?v=")[0].endswith(panel.PANEL_FILE)


def test_the_hash_changes_when_the_panel_changes() -> None:
    """A constant token is decoration; it has to track the file."""
    real = panel._module_url(str(PANEL_JS.parent))
    missing = panel._module_url("/nonexistent-directory")
    assert real != missing


def test_an_unreadable_panel_still_yields_a_usable_url() -> None:
    """Hashing is an optimisation. Failing to hash must not cost the panel."""
    assert panel._module_url("/nonexistent-directory") == panel.PANEL_MODULE_URL


def test_action_buttons_call_services_that_actually_exist() -> None:
    """A button naming a service the integration does not register is a button
    that fails only when someone finally presses it, in the moment they needed
    it. The names are checked against const.py rather than trusted."""
    from custom_components.cluster_state_sync import const

    js = PANEL_JS.read_text(encoding="utf-8")
    registered = {const.SERVICE_FLUSH_SNAPSHOT, const.SERVICE_CLEAR_DEGRADED}
    import re

    referenced = set(re.findall(r'data-service="([a-z_]+)"', js))
    assert referenced, "no action buttons found"
    assert referenced <= registered, (
        f"panel calls services that are not registered: {referenced - registered}"
    )
    assert 'callService("cluster_state_sync"' in js


def test_the_actions_card_says_which_instance_it_acts_on() -> None:
    """The panel shows a card per node, but a service call runs on the instance
    serving the page. A button sitting inside a node's card would read as
    'flush THAT node', which it is not."""
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "_actionsCard" in js
    assert "serving this page" in js, "the scope of the buttons must be stated"


def test_a_pressed_button_reports_what_happened() -> None:
    """A control that gives no sign it did anything invites a second press."""
    js = PANEL_JS.read_text(encoding="utf-8")
    for token in ("working", "done", "failed"):
        assert token in js, f"no {token!r} feedback on the action buttons"


def test_the_shipped_javascript_actually_parses() -> None:
    """There is no JS runtime in CI, and the failure mode is silent.

    A panel that does not parse is reported by Home Assistant only as
    "Unable to load custom panel" -- with no hint whether the cause is the file
    or the delivery. This project shipped a broken panel twice before this
    check existed, and each time the debugging went after the network path.
    """
    esprima = pytest.importorskip("esprima")
    esprima.parseModule(PANEL_JS.read_text(encoding="utf-8"))


def test_the_custom_element_definition_is_guarded() -> None:
    """Home Assistant is a single-page app.

    Navigating to a panel imports its module into a page that may already have
    imported an earlier build -- which happens on every upgrade, because the
    cache-busting hash changes the URL. A bare `customElements.define` then
    throws NotSupportedError, the frontend catches it, and the user is told
    "Unable to load custom panel" while the panel itself is perfectly fine and
    merely already loaded. Observed on this fleet.
    """
    js = PANEL_JS.read_text(encoding="utf-8")
    assert 'customElements.get("cluster-status-panel")' in js, (
        "define must be guarded, or a second import breaks the panel"
    )
    define_line = next(line for line in js.splitlines() if "customElements.define(" in line)
    assert define_line.startswith("  "), "the define should sit inside the guard, not beside it"


# --- AR-0044: peer-supplied data must not become markup ------------------


def _esc_like_the_panel(value: object) -> str:
    """A port of the panel's `_esc`, so its contract is pinned in the suite.

    There is no JavaScript runtime in CI (see `_parse_like_the_panel`, which
    exists for the same reason), so the escaping rule is asserted here and the
    structural test below proves the shipped file actually applies it.
    """
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@pytest.mark.parametrize(
    ("payload", "must_not_contain"),
    [
        ("<img src=x onerror=alert(1)>", "<img"),
        ("</b><script>alert(1)</script>", "<script"),
        ('" onmouseover="alert(1)', '"'),
        ("'><svg onload=alert(1)>", "<svg"),
    ],
)
def test_the_escaper_neutralises_markup(payload: str, must_not_contain: str) -> None:
    """🚨 AR-0044. `sensor.<node>_cluster_leader` is peer-supplied.

    Its value is `coordinator.data.leader`, read straight out of the shared
    Valkey store and written by the OTHER node — and the cluster registry is
    not the sealed-blob channel. Valkey write access alone was therefore
    enough to put markup into a page running with an administrator's session.
    """
    assert must_not_contain not in _esc_like_the_panel(payload)


def test_the_escaper_leaves_ordinary_values_readable() -> None:
    """An escaper that mangles `node-a_f5d04f` would be swapped out."""
    assert _esc_like_the_panel("node-a_f5d04f") == "node-a_f5d04f"
    assert _esc_like_the_panel("42.0") == "42.0"
    assert _esc_like_the_panel(None) == ""


def test_every_value_interpolated_into_the_panel_html_is_escaped() -> None:
    """The port above proves the rule; this proves the file applies it.

    Structural rather than behavioural because there is no JS runtime here —
    but it is the half that catches the real regression, which is someone
    adding a seventh interpolation and not knowing about the sixth.

    The rule: a `${...}` holding a **bare value** — an identifier or a dotted
    path, no call — is raw data and must be escaped. An interpolation that
    calls one of the panel's own helpers (`this._row(...)`, `this._holdRow(...)`)
    is already-escaped HTML by construction and is left alone; escaping it
    again would render the markup as text.
    """
    #: Bare interpolations that are NOT data: locals computed from literals in
    #: this file. Each is listed deliberately, so a new one has to be argued
    #: for rather than assumed.
    SAFE_LOCALS = {
        "cls",  # "" | "good" | "bad" | "warn", chosen from literals above
        "on",  # boolean
        "pending",  # boolean
        "leader",  # boolean
        "held",  # boolean
        "FRESH",  # a module constant
        # HTML this file assembled itself, from the escaping helpers above.
        # Escaping it again would render the markup as text.
        "body",
        # Same: the replication card, and the device list inside it. Both are
        # built by `_replicationCard`, which escapes every value it reads from
        # an entity attribute before it becomes markup.
        "scopeCard",
        "devices",
        # NOT markup: an object KEY in `bucket[`${metric}__switch`]`.
        # Escaping it would corrupt the lookup. If `metric` is ever
        # interpolated into HTML, it must be escaped there and removed
        # from this list.
        "metric",
    }
    js = PANEL_JS.read_text(encoding="utf-8")
    bare = re.findall(r"\$\{\s*([A-Za-z_$][\w.$]*)\s*\}", js)
    unescaped = sorted({expr for expr in bare if expr not in SAFE_LOCALS})
    assert not unescaped, (
        f"raw values interpolated into innerHTML: {unescaped}. Every value that reaches "
        "the panel's HTML must go through `this._esc(...)` — AR-0044."
    )


def test_the_panel_has_exactly_one_innerhtml_assignment() -> None:
    """`_esc` must exist, and a second innerHTML sink must not appear unnoticed."""
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "_esc(value)" in js, "the panel's escape helper is gone"
    assignments = re.findall(r"\.innerHTML\s*=", js)
    assert len(assignments) == 1, (
        f"{len(assignments)} innerHTML assignments; a new one needs the same "
        "escaping review as the first"
    )


def test_the_card_heading_names_the_node_that_is_actually_running() -> None:
    """🚨 AR-0055. The heading must not be parsed out of the entity id.

    Entity ids live in `core.entity_registry`, which the go-bag replicates
    wholesale, so a node promoted from its peer inherits the peer's ids: every
    entity on node-b reads `cluster_sync_node-a_…`. Deriving the heading
    from the id then names the machine that is **not** running, beside a LEADER
    tag that is correct — at exactly the moment somebody is trying to work out
    where their house went.

    `binary_sensor.…is_leader` carries `node_id` as an attribute
    (`binary_sensor.py`'s `extra_state_attributes`) and computes it locally, so
    it is right on both nodes.

    Structural, because there is no JS runtime here — but it is the half that
    catches the regression, which is someone "simplifying" the heading back to
    the parsed name.
    """
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "attributes.node_id" in js, (
        "the panel no longer reads the locally-computed node_id attribute — a "
        "promoted node will label its card with the peer's name (AR-0055)"
    )
    assert "this._esc(realNode)" in js, "the heading is not rendered from the attribute"


def test_the_heading_falls_back_when_the_attribute_is_missing() -> None:
    """A follower's is_leader sensor exists but may not have been read yet.

    Falling back to the parsed id keeps the card labelled rather than blank,
    which is the right failure for a cosmetic field.
    """
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "|| node;" in js, "no fallback — a missing attribute would blank the heading"
