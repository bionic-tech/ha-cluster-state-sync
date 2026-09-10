"""Every multi-value picker with real options must render tickboxes.

Reported by the owner, 2026-09-10, going to change which domains replicate:

    "I noticed that we don't have the multiselect again ... make sure it's
    multiselect so that it's not like click, add, click, add, click, add"

Home Assistant's `SelectSelector` renders `multiple=True` two ways.
`SelectSelectorMode.LIST` gives tickboxes — see them all, tick several, done.
`DROPDOWN` gives a menu you must reopen once per value, so choosing the twelve
default domains was twelve separate round-trips.

The rule this pins, and why it is a rule rather than a review habit: a picker
with a real options list must use LIST. A picker whose `options` is a literal
`[]` is a free-text field — the operator types values that do not exist yet —
and LIST would render an empty box with no way to type into it, so those must
stay DROPDOWN. Both halves matter; enforcing only the first would break
`radio_watch` and the wizard's `include_entities`.

Checked by parsing the source rather than building schemas, because the schema
builders need a live `hass` and half of them are behind wizard steps. The
failure this guards against is silent: the form still works, it is just tedious
enough that nobody edits their replication scope.
"""

from __future__ import annotations

import ast
import pathlib

CONFIG_FLOW = (
    pathlib.Path(__file__).parent.parent
    / "custom_components"
    / "cluster_state_sync"
    / "config_flow.py"
)


def _select_selector_configs() -> list[tuple[int, dict[str, ast.expr]]]:
    """Every `SelectSelectorConfig(...)` in the flow, with its line number."""
    tree = ast.parse(CONFIG_FLOW.read_text(encoding="utf-8"))
    found: list[tuple[int, dict[str, ast.expr]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "SelectSelectorConfig":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        found.append((node.lineno, kwargs))
    return found


def _is_multiple(kwargs: dict[str, ast.expr]) -> bool:
    node = kwargs.get("multiple")
    return isinstance(node, ast.Constant) and node.value is True


def _options_is_literally_empty(kwargs: dict[str, ast.expr]) -> bool:
    node = kwargs.get("options")
    return isinstance(node, ast.List) and not node.elts


def _mode_name(kwargs: dict[str, ast.expr]) -> str:
    node = kwargs.get("mode")
    if node is None:
        return "DROPDOWN"  # Home Assistant's default
    return getattr(node, "attr", "?")


def test_the_flow_still_has_multi_value_pickers_to_check() -> None:
    """Guards the guard: an AST walk that silently matches nothing proves nothing."""
    multi = [(ln, kw) for ln, kw in _select_selector_configs() if _is_multiple(kw)]
    assert len(multi) >= 6, (
        f"only found {len(multi)} multi-value SelectSelectors — the walk is "
        "probably not matching any more, so this file is asserting nothing"
    )


def _has_custom_value(kwargs: dict[str, ast.expr]) -> bool:
    node = kwargs.get("custom_value")
    return isinstance(node, ast.Constant) and node.value is True


def test_pickers_with_real_options_render_tickboxes() -> None:
    """🚨 The owner's report: picking twelve domains took twelve round-trips.

    Scoped to pickers that do NOT set `custom_value` — one that does is a chip
    box on purpose, and the test below holds it to declaring that honestly.
    """
    offenders = [
        (ln, _mode_name(kw))
        for ln, kw in _select_selector_configs()
        if _is_multiple(kw)
        and not _options_is_literally_empty(kw)
        and not _has_custom_value(kw)
        and _mode_name(kw) != "LIST"
    ]
    assert not offenders, (
        "these multi-value pickers offer real options but render as a dropdown, "
        "so the operator must reopen the menu once per value: "
        + ", ".join(f"config_flow.py:{ln} (mode={mode})" for ln, mode in offenders)
    )


def test_free_text_pickers_stay_dropdowns() -> None:
    """The other half of the rule. LIST with no options is an empty, unusable box."""
    broken = [
        (ln, _mode_name(kw))
        for ln, kw in _select_selector_configs()
        if _is_multiple(kw) and _options_is_literally_empty(kw) and _mode_name(kw) == "LIST"
    ]
    assert not broken, (
        "these pickers have no options and exist for typing free text, but were "
        "switched to LIST — which renders an empty box with nothing to tick and "
        "no way to type: " + ", ".join(f"config_flow.py:{ln}" for ln, _ in broken)
    )


def test_a_list_mode_picker_does_not_also_claim_custom_value() -> None:
    """🚨 `custom_value=True` is what actually causes click-add-click-add.

    This is the mechanism, and it is not the one the field name suggests. From
    this flow's own comment on `fileset_extra_paths`:

        "No `custom_value` here, deliberately. With it, Home Assistant renders
        a multi-select as a type-to-add chip box rather than a tick list."

    So `custom_value` overrides `mode`. A picker declaring LIST while setting
    `custom_value` promises tick boxes and delivers a chip box — the code says
    one thing and the operator sees another, with nothing reporting the gap.

    Either is a legitimate choice. Claiming one while doing the other is not.
    """
    lying = [
        ln
        for ln, kw in _select_selector_configs()
        if _mode_name(kw) == "LIST"
        and isinstance(kw.get("custom_value"), ast.Constant)
        and kw["custom_value"].value is True
    ]
    assert not lying, (
        "these pickers set custom_value=True alongside LIST mode, which Home "
        "Assistant ignores — remove it or use DROPDOWN: "
        + ", ".join(f"config_flow.py:{ln}" for ln in lying)
    )


def test_a_picker_with_no_options_can_still_be_typed_into() -> None:
    """An empty options list with no `custom_value` is a dead control.

    Nothing to tick, nothing to type. It would render as an empty box that
    silently discards whatever the operator meant to put in it.
    """
    dead = [
        ln
        for ln, kw in _select_selector_configs()
        if _is_multiple(kw) and _options_is_literally_empty(kw) and not _has_custom_value(kw)
    ]
    assert not dead, (
        "these pickers offer no options and accept no custom value, so there is "
        "no way to put anything into them: " + ", ".join(f"config_flow.py:{ln}" for ln in dead)
    )
