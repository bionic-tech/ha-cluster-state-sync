"""The CI definitions themselves.

A workflow file is code that only ever runs somewhere else, so a mistake in one
is invisible locally and silent remotely — it does not fail, it just quietly
does less than it says.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))


def test_there_are_workflows_to_check() -> None:
    assert WORKFLOWS, "no workflows found — this test would pass vacuously"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_is_defined_twice(workflow: pathlib.Path) -> None:
    """🚨 YAML keeps the last duplicate key and says nothing.

    `ci.yml` defined `canary` twice. The second won, and it was the poorer of
    the two: no shellcheck, no version summary, and — the part that mattered —
    no `scripts/ha_compat_check.py`, which is the check that reads the Home
    Assistant API symbols and recorder schema this integration depends on.

    So the canary ran, went green, and had never once performed the check it
    was built for. Nothing failed. The job list looked complete.
    """
    text = workflow.read_text(encoding="utf-8")
    # Job keys are the two-space-indented mapping keys under `jobs:`.
    keys = re.findall(r"^  ([A-Za-z_][\w-]*):\s*$", text, re.M)
    parsed = list(yaml.safe_load(text).get("jobs", {}))

    duplicates = sorted({k for k in keys if keys.count(k) > 1 and k in parsed})
    assert not duplicates, (
        f"{workflow.name} defines these jobs more than once: {duplicates}. "
        "YAML silently keeps the last one, so the others never run."
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_referenced_repository_script_exists(workflow: pathlib.Path) -> None:
    """A workflow that calls a script that has been moved fails only on a runner."""
    text = workflow.read_text(encoding="utf-8")
    repo = workflow.parent.parent.parent
    missing = [
        ref
        for ref in set(re.findall(r"python (scripts/[\w./-]+\.py)", text))
        if not (repo / ref).exists()
    ]
    assert not missing, f"{workflow.name} runs scripts that do not exist: {missing}"


# -- Home Assistant's own validator, checked locally ------------------------
#
# `hassfest` runs only on a GitHub runner, so its findings are invisible here.
# It found three real errors the day CI started working again, after months in
# which the workflow file was rejected outright and nothing ran. These are the
# rules of its that can be checked without the validator.


TRANSLATION_FILES = (
    REPO / "custom_components" / "cluster_state_sync" / "strings.json",
    REPO / "custom_components" / "cluster_state_sync" / "translations" / "en.json",
)


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_no_translated_string_contains_a_url(path: pathlib.Path) -> None:
    """hassfest: "the string should not contain URLs, use description placeholders".

    It is right to refuse them. A literal URL cannot be localised, and a
    translator has no way to tell an example address from one to click. Pass the
    value through `description_placeholders` and reference it as `{name}`.

    Four strings broke this — the ingress step's field description and its
    error, in both files.
    """
    import json

    found: list[str] = []

    def walk(node: object, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{trail}.{key}" if trail else key)
        elif isinstance(node, str) and re.search(r"https?://", node):
            found.append(trail)

    walk(json.loads(path.read_text(encoding="utf-8")), "")
    assert not found, (
        f"{path.name} contains URLs at: {found}. hassfest rejects these — put the "
        "value in `description_placeholders` and reference it as a `{placeholder}`."
    )


def test_components_used_are_declared_in_the_manifest() -> None:
    """hassfest: "Using component X but it's not in dependencies or after_dependencies".

    `panel.py` imports `homeassistant.components.http` and calls `hass.http`.
    It was declared nowhere, so Home Assistant had no reason to set `http` up
    first — and the panel's own try/except made that look like "no panel today"
    rather than a manifest error.

    `after_dependencies` rather than `dependencies` on purpose: a hard
    dependency that fails means Home Assistant refuses to set this integration
    up at all, which is a far worse outcome than a missing sidebar icon. That
    reasoning is written out in `panel.py`.
    """
    import json

    integration = REPO / "custom_components" / "cluster_state_sync"
    manifest = json.loads((integration / "manifest.json").read_text(encoding="utf-8"))
    declared = set(manifest.get("dependencies", [])) | set(manifest.get("after_dependencies", []))

    used = {
        match
        for source in integration.rglob("*.py")
        for match in re.findall(
            r"from homeassistant\.components(?:\.(\w+)| import (\w+))",
            source.read_text(encoding="utf-8"),
        )
        for match in match
        if match
    }
    # Provided by Home Assistant itself; hassfest does not ask for these.
    used -= {"diagnostics", "sensor", "binary_sensor", "switch", "button"}

    #: Used, and deliberately NOT declared. Each is a lazy import inside a
    #: `try`, so its absence costs a feature rather than the integration.
    #:
    #: 🚨 `automation` is the one that matters. Declaring it would make Home
    #: Assistant set the automation engine up BEFORE this integration — and the
    #: entire design rests on the restore running before automations can act on
    #: what it writes. It is read only to check whether an automation's state is
    #: pinned by `initial_state`, which is not worth reordering setup for.
    #:
    #: `panel_custom` is argued in `panel.py`: a hard dependency that fails means
    #: Home Assistant refuses to set us up at all, which is a worse outcome than
    #: no sidebar icon. `persistent_notification` is the same shape, in
    #: `alerts.py`.
    deliberately_undeclared = {"automation", "panel_custom", "persistent_notification"}
    undeclared = used - declared - deliberately_undeclared

    assert not undeclared, (
        f"these Home Assistant components are used but undeclared: {sorted(undeclared)}. "
        "Add them to `after_dependencies` in manifest.json — or, if the import is a "
        "lazy one whose failure is survivable, add it to `deliberately_undeclared` "
        "above WITH the reason."
    )
    assert "http" in declared, (
        "`http` must stay declared: panel.py calls `hass.http`, and hassfest fails "
        "the build without it."
    )


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_every_field_description_belongs_to_a_field_on_that_step(path: pathlib.Path) -> None:
    """hassfest: "data_description key X is not in data at 'config'".

    🚨 The validator's complaint is the smaller half. A `data_description` under
    a step whose `data` has no such field is help text Home Assistant never
    renders — so the explanation is not merely misfiled, it is invisible.

    `leadership_source` carried its explanation under the `direct` and
    `sentinel` steps. The field is on `topology`, which had no description at
    all. Every operator who picked a leadership source did it with no guidance,
    while the guidance sat two steps away being rejected by the validator.
    """
    import json

    document = json.loads(path.read_text(encoding="utf-8"))
    orphans: list[str] = []
    for section in ("config", "options"):
        for step, body in document.get(section, {}).get("step", {}).items():
            stray = set(body.get("data_description", {})) - set(body.get("data", {}))
            if stray:
                orphans.append(f"{section}.step.{step}: {sorted(stray)}")

    assert not orphans, (
        f"{path.name} describes fields that are not on the step: {orphans}. "
        "Home Assistant will not render these, and hassfest fails the build. Move "
        "each one to the step whose schema actually has the field."
    )


def test_hacs_offers_only_released_versions() -> None:
    """🚨 Without this, HACS offers `main` alongside the releases.

    The moment anything is committed after a release, `main` becomes an
    untested, unreleased option sitting in the same dropdown as the version that
    has a release note, an upgrade section and a rehearsal behind it. Somebody
    picks it because it is at the top.

    This integration stops and starts Home Assistant and swaps `.storage` on
    promotion. "Whatever happened to be on main that afternoon" is not a thing
    to offer for that.
    """
    import json

    hacs = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))
    assert hacs.get("hide_default_branch") is True, (
        "hacs.json must set `hide_default_branch: true`, or HACS lists the default "
        "branch as an installable version alongside the releases."
    )
