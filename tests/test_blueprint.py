"""The shipped alerting blueprint (I13).

AR-0019's finding was that this integration was "built to fail safe, but blind
while doing it". The diagnostic entities closed half of that. This blueprint is
the other half: something has to *watch* them, or a promotion that restored
nothing is still only visible to whoever reads the log.

A blueprint is YAML, so the failure mode is that it looks fine in review and
then refuses to import. These tests run it through Home Assistant's own
schemas, which is the only opinion that counts.
"""

from __future__ import annotations

import pathlib

from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    ValidationStatus,
    async_validate_config_item,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.util.yaml import parse_yaml
import pytest

BLUEPRINT_DIR = (
    pathlib.Path(__file__).parent.parent / "blueprints" / "automation" / "cluster_state_sync"
)
FAILOVER_READINESS = BLUEPRINT_DIR / "failover_readiness.yaml"


def load(path: pathlib.Path) -> Blueprint:
    """Parse and validate through HA's own blueprint model."""
    return Blueprint(
        parse_yaml(path.read_text(encoding="utf-8")),
        expected_domain="automation",
        path=str(path),
        # The exact schema the automation integration applies on import —
        # not the generic one, so the backward-compat trigger/action handling
        # is included and this test fails for the same reasons a user would.
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


def test_the_blueprint_is_shipped() -> None:
    """It has to exist where the README says it does."""
    assert FAILOVER_READINESS.is_file()


def test_home_assistant_accepts_the_blueprint() -> None:
    """Production change that would make this fail: any schema error at all.

    `Blueprint(...)` runs BLUEPRINT_SCHEMA and the input validation. If this
    raises, the operator's import fails with the same error, and they find out
    at the moment they were trying to add monitoring.
    """
    blueprint = load(FAILOVER_READINESS)
    assert blueprint.domain == "automation"
    assert blueprint.name


def test_every_input_the_operator_must_answer_is_named_and_described() -> None:
    """An input with no description is a question with no context.

    Production change that would make this fail: adding an input without a
    `name`, or without a `description` explaining what to put in it.

    These blueprints are imported by people who did not write them, at the
    moment they are trying to set up alerting. A bare `backend_sensor:` field
    tells them nothing about which of three similar entities to pick.
    """
    inputs = load(FAILOVER_READINESS).inputs
    assert inputs, "the blueprint takes no inputs, which cannot be right"
    for key, spec in inputs.items():
        assert spec is not None, f"input {key} has no definition"
        assert spec.get("name"), f"input {key} has no name"
        assert spec.get("description"), f"input {key} has no description"


@pytest.mark.parametrize(
    "trigger_id",
    ["backend_unreachable", "snapshot_stale", "restore_was_empty", "fileset_degraded"],
)
def test_the_four_conditions_worth_paging_for_are_all_covered(trigger_id: str) -> None:
    """Each of these is a way the failover silently does not work.

    Production change that would make this fail: dropping a trigger.

    * `backend_unreachable` — nothing is being written, so a promotion now
      restores stale state or none.
    * `snapshot_stale` — the backend is up and the flush loop stopped anyway,
      which AR-0020 added the log line for and nothing watches.
    * `restore_was_empty` — the AR-0040 signature. A snapshot was there and
      none of it was applied. That bug was live for the whole project and its
      only symptom was an INFO line.
    * `fileset_degraded` — Decision D4's promote-anyway path. A stale or
      missing config fileset was swapped in, and the alarm is the only thing
      that says so; see `test_the_blueprint_watches_the_fileset` for what
      *else* has to be true about this one beyond merely existing.
    """
    raw = parse_yaml(FAILOVER_READINESS.read_text(encoding="utf-8"))
    ids = {t.get("id") for t in raw["triggers"]}
    assert trigger_id in ids


def test_the_source_url_points_at_this_repository() -> None:
    """`source_url` is what lets Home Assistant offer the operator updates.

    Production change that would make this fail: omitting it, or pointing it at
    a path that does not exist in the repository. A blueprint imported without
    one is frozen at whatever version was fetched.
    """
    raw = parse_yaml(FAILOVER_READINESS.read_text(encoding="utf-8"))
    url = raw["blueprint"].get("source_url", "")
    assert url.startswith("https://")
    assert url.endswith("blueprints/automation/cluster_state_sync/failover_readiness.yaml")


# -- does it actually produce a working automation? -------------------------


def _instantiate() -> dict:
    """Substitute realistic inputs, as an operator's import would."""
    blueprint = load(FAILOVER_READINESS)
    inputs = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": FAILOVER_READINESS.name,
                "input": {
                    "backend_sensor": "binary_sensor.cluster_sync_backend",
                    "snapshot_age_sensor": "sensor.cluster_sync_last_snapshot_age",
                    "entities_restored_sensor": "sensor.cluster_sync_entities_restored",
                    "fileset_degraded_sensor": "binary_sensor.cluster_sync_fileset_degraded",
                    "notification_action": [
                        {
                            "action": "persistent_notification.create",
                            "data": {
                                "title": "{{ title }}",
                                "message": "{{ message }}",
                            },
                        }
                    ],
                },
            }
        },
    )
    inputs.validate()
    return inputs.async_substitute()


def test_the_optional_inputs_really_are_optional() -> None:
    """Production change that would make this fail: dropping a `default:`.

    `inputs.validate()` raises MissingInput for any input the operator did not
    supply and that has no default. The instantiation above deliberately omits
    `backend_grace` and `max_snapshot_age`, so this pins that someone importing
    the blueprint only has to answer the questions that genuinely need an
    answer: which entities, and how to be told.
    """
    substituted = _instantiate()
    # Located by id, not by index. The original asserted `triggers[0]` and
    # `triggers[1]`, which broke the moment a trigger was added in the middle —
    # and the failure looked like a defaulting bug rather than a renumbering.
    by_id = {t["id"]: t for t in substituted["triggers"] if "id" in t}
    assert by_id["backend_unreachable"]["for"] == {"minutes": 5}
    assert by_id["snapshot_stale"]["above"] == 900


def test_the_blueprint_watches_the_fileset() -> None:
    """Fourth condition: a promotion that came up with a stale or missing
    go-bag (Decision D4). Without it the operator's only signal is a phone
    that will not log in.

    A substring check on the dumped YAML -- `"fileset_degraded" in
    yaml.safe_dump(blueprint)` -- would pass if that string only ever showed
    up in a comment or a description, including a blueprint where the
    condition itself was never wired in. Ten tests in this plan have already
    looked correct while failing to discriminate exactly that shape of
    regression, so this walks the real structure instead of the dumped text.

    There are three things a correct blueprint must all get right here, each
    catching a different regression:

    1. Two triggers share the `fileset_degraded` id -- a `state` trigger AND
       a `homeassistant: start` trigger. The state trigger alone races the
       automation's own startup: `automation` and `cluster_state_sync` are
       both stage-2 bootstrap domains set up *concurrently*, so there is no
       guarantee this automation is listening yet the instant a degraded
       sensor is created already "on" -- and there is no second chance,
       because an unchanged state on a later poll fires
       `EVENT_STATE_REPORTED`, not `EVENT_STATE_CHANGED`. Losing either
       trigger reopens that window.
    2. Because the `homeassistant`-start trigger fires on *every* boot
       whether or not anything is wrong, the condition for `fileset_degraded`
       must check the sensor's actual current state, not just which trigger
       fired -- unlike `backend_unreachable`/`snapshot_stale`, where the
       trigger firing at all already proves the bad state.
    3. The `actions: choose:` block must have a branch keyed to
       `fileset_degraded` -- an edit that left the trigger and condition
       intact but dropped only the notification content would otherwise pass
       everything else here.
    """
    raw = parse_yaml(FAILOVER_READINESS.read_text(encoding="utf-8"))

    # (1) Both triggers exist and share the id.
    fileset_triggers = [t for t in raw["triggers"] if t.get("id") == "fileset_degraded"]
    trigger_platforms = {t.get("trigger") for t in fileset_triggers}
    assert "state" in trigger_platforms, "no state trigger for fileset_degraded"
    assert "homeassistant" in trigger_platforms, (
        "no homeassistant-start trigger for fileset_degraded -- the state "
        "trigger alone races the automation's own startup"
    )

    top_condition = raw["conditions"][0]
    assert top_condition.get("condition") == "or", "expected the outer or-condition"
    inner_conditions = top_condition["conditions"]

    # backend_unreachable/snapshot_stale are still bare trigger-id checks.
    id_conditions = [c for c in inner_conditions if c.get("condition") == "trigger"]
    watched_ids: set[str] = set()
    for c in id_conditions:
        ids = c["id"]
        watched_ids.update([ids] if isinstance(ids, str) else ids)
    assert watched_ids >= {"backend_unreachable", "snapshot_stale"}

    # (2) fileset_degraded must be wrapped with a live state check, since its
    # homeassistant-start trigger does not by itself mean anything is wrong.
    and_blocks = [c for c in inner_conditions if c.get("condition") == "and"]
    fileset_block = next(
        (
            block
            for block in and_blocks
            if any(
                c.get("condition") == "trigger" and c.get("id") == "fileset_degraded"
                for c in block["conditions"]
            )
        ),
        None,
    )
    assert fileset_block is not None, "fileset_degraded is not evaluated in the or-condition"
    assert any(
        c.get("condition") == "state" and c.get("state") == "on"
        for c in fileset_block["conditions"]
    ), "fileset_degraded's condition does not check the sensor's current state"

    # (3) The action actually tells someone.
    choose_branches = raw["actions"][0]["choose"]
    assert any(
        any(
            c.get("condition") == "trigger" and c.get("id") == "fileset_degraded"
            for c in branch["conditions"]
        )
        for branch in choose_branches
    ), "no actions: choose: branch handles fileset_degraded"


async def test_the_substituted_automation_is_valid(hass) -> None:
    """The test that matters: does it work, not merely does it parse.

    Production change that would make this fail: a template that does not
    compile, an `!input` naming something that is not an input, a trigger or
    action shape the automation platform rejects.

    A blueprint can satisfy BLUEPRINT_SCHEMA and still produce an automation
    Home Assistant refuses — the schema checks the blueprint's own envelope,
    not what falls out once the inputs are filled in. This runs the real
    validator over the real substituted config, which is what happens on the
    operator's machine the moment they press save.
    """
    config = {**_instantiate(), "alias": "Cluster sync readiness"}
    validated = await async_validate_config_item(hass, "automation", config)

    assert validated is not None, "Home Assistant rejected the substituted automation"
    assert validated.validation_status is ValidationStatus.OK, validated.validation_error


def test_the_blueprint_can_watch_the_front_door() -> None:
    """AR-0060, for people who use the blueprint rather than the built-in alerts.

    Every other trigger here looks inward — Valkey, the snapshot, the go-bag,
    what was restored. All of them can be perfectly green while the address a
    person actually types answers nothing, which is the failure AR-0060 was
    raised for.

    Checked by wiring rather than by substring: `"ingress" in dumped_yaml`
    would pass on a blueprint that only mentions it in a description.
    """
    blueprint = load(FAILOVER_READINESS)
    declared = blueprint.data["blueprint"]["input"]
    assert "ingress_sensor" in declared, "no input to select it"
    assert "default" in declared["ingress_sensor"], (
        "the ingress sensor must be optional — most installs have no URL configured"
    )
    ids = [t.get("id") for t in blueprint.data["triggers"]]
    assert "ingress_unreachable" in ids, "the input exists but nothing triggers on it"


def test_the_ingress_input_admits_it_cannot_prove_much() -> None:
    """The honesty that has to travel with this reading wherever it appears.

    A probe from inside the network can be green while an external path is
    down. Said in the module, the entity, the options page, the guide and the
    README — and it has to be said here too, because the blueprint is where
    somebody decides what to be woken for.
    """
    text = FAILOVER_READINESS.read_text(encoding="utf-8")
    block = text[text.index("ingress_sensor:") : text.index("snapshot_age_sensor:")]
    assert "not proof" in block or "is not" in block
    assert "split-horizon" in block or "split horizon" in block
