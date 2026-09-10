"""The advice shown next to each domain in the picker.

Asked for after the owner selected 38 domains and pulled in 2,158 entities, of
which 1,970 could not benefit from crossing — 1,436 of them `device_tracker`,
which rebuilds itself within seconds. Nothing on the form said so, because the
form was a bare alphabetical list of domain names.

These tests hold the advice to being present, honest, and readable. They cannot
check that it is *correct* — that is what `docs/GUIDE-choosing-domains.md` and
its evidence are for — but they can stop it drifting into silence.
"""

from __future__ import annotations

from custom_components.cluster_state_sync.const import DEFAULT_INCLUDE_DOMAINS
from custom_components.cluster_state_sync.domain_advice import (
    DOMAIN_VERDICTS,
    LEGACY_DEFAULTS_NOT_RECOMMENDED,
    UNKNOWN,
    label_for,
    verdict_for,
)


def test_every_shipped_default_has_been_assessed() -> None:
    """Shipping a domain by default without an opinion on it is not a position."""
    unassessed = [d for d in DEFAULT_INCLUDE_DOMAINS if verdict_for(d) is UNKNOWN]
    assert not unassessed, (
        f"these domains ship in DEFAULT_INCLUDE_DOMAINS but nobody has said why: {unassessed}"
    )


def test_the_disagreement_with_our_own_defaults_stays_deliberate() -> None:
    """🚨 We ship three domains this module does not recommend. On purpose.

    `vacuum`, `humidifier` and `water_heater` are device-backed on most estates
    — zero core integrations use `RestoreEntity` for the first two, one for the
    third. They stay because they are tiny, change rarely, and removing a
    shipped default would break replication for anyone whose custom integration
    *does* restore them.

    If that set changes, it must change because someone decided to, not because
    a verdict was edited without noticing what else it governed.
    """
    disagrees = {d for d in DEFAULT_INCLUDE_DOMAINS if not verdict_for(d).worth_it}
    assert disagrees == set(LEGACY_DEFAULTS_NOT_RECOMMENDED), (
        "the defaults we do not recommend no longer match the acknowledged set — "
        f"found {sorted(disagrees)}, recorded {sorted(LEGACY_DEFAULTS_NOT_RECOMMENDED)}. "
        "Either update LEGACY_DEFAULTS_NOT_RECOMMENDED and say why, or change "
        "the verdict back."
    )


def test_the_guard_domain_is_recommended() -> None:
    """`input_boolean` is the one that changes what the house does.

    A toggle whose only job is to stop an automation acting — the owner's
    `dishwasher_washing_machine_ignore_shutdown` — is worthless to replicate
    right up until the moment it is not, and then it shuts down a running
    washing machine.
    """
    v = verdict_for("input_boolean")
    assert v.worth_it
    assert "guard" in v.why.lower() or "automation" in v.why.lower(), (
        "the reason should say what losing it does, not just that it matters"
    )


def test_the_expensive_domain_is_not_recommended() -> None:
    """`device_tracker` was two thirds of the owner's 11x increase."""
    assert not verdict_for("device_tracker").worth_it


def test_every_verdict_says_something() -> None:
    empty = [d for d, v in DOMAIN_VERDICTS.items() if not v.tag.strip() or not v.why.strip()]
    assert not empty, f"these verdicts are blank: {empty}"


def test_labels_survive_being_read_on_a_phone() -> None:
    """A checkbox list wraps, but a label that needs three lines is not read.

    The count makes labels longer, so it is included in the measurement.
    """
    too_long = [
        (d, len(label_for(d, count=9999)))
        for d in DOMAIN_VERDICTS
        if len(label_for(d, count=9999)) > 95
    ]
    assert not too_long, f"these labels are too long to scan: {too_long}"


def test_an_unclassified_domain_says_so_rather_than_guessing() -> None:
    """A custom integration's domain must not be given a confident wrong answer."""
    v = verdict_for("some_custom_domain_nobody_has_seen")
    assert v is UNKNOWN
    assert not v.worth_it, "the safe default for an unknown domain is 'do not'"
    assert "not assessed" in v.why


def test_the_label_carries_the_operators_own_count() -> None:
    """'device_tracker' and 'device_tracker (1436)' are different propositions."""
    assert "(1436)" in label_for("device_tracker", count=1436)
    assert "(" not in label_for("device_tracker").split(" — ")[0], (
        "a domain with no entities should not show an empty count"
    )
