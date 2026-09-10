"""Why a domain is or is not worth replicating, said at the point of choosing.

Asked for by the owner on 2026-09-10, after selecting 38 domains and finding
that 1,970 of the 2,158 entities it pulled in could not benefit from crossing:

    "Can we make it more obvious when people go to select stuff that these
    rebuild and don't actually need replication? ... I don't want to kill it
    with replication for the sake of replicating."

The wizard used to offer a bare alphabetical list of domain names. Every one
looked equally reasonable, so the honest reading of that form was "tick
everything you care about" — which is exactly wrong, and expensive: the flush
writes the **full map** every time anything in scope changes, so one churning
domain turns an idle cluster into a permanent write loop.

Four questions decide it, in order:

1. **Does anything else know this value?** If the device, the integration or a
   calculation can produce it again, replication is at best cosmetic. This is
   the question that eliminates most domains.
2. **Is there a value at all?** A button's state is the timestamp of its last
   press. There is nothing to carry.
3. **Does it change constantly?** A domain that updates every few seconds keeps
   the full-map flush running permanently, whatever it is worth.
4. **Would losing it change what an automation does?** This is the one that
   overrides the rest, and it is why `input_boolean` is not negotiable — see
   `GUARD` below.

`docs/GUIDE-choosing-domains.md` carries the worked scenarios. This module is
the one-line version, shown in the form.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class Verdict(NamedTuple):
    """A recommendation and the reason for it, both shown to the operator."""

    #: Short tag rendered before the reason. Deliberately a verb or a plain
    #: statement rather than a severity: this is advice, not an error.
    tag: str
    #: One line, under about sixty characters, that must survive being read on
    #: a phone in a checkbox list.
    why: str
    #: Whether ticking this is expected to achieve something.
    worth_it: bool


REPLICATE: Final = Verdict("replicate", "Home Assistant owns this — nothing else knows it", True)
GUARD: Final = Verdict("replicate", "may be guarding an automation — losing it acts", True)
REBUILDS: Final = Verdict("not needed", "rebuilds itself within seconds of starting", False)
DEVICE: Final = Verdict("cosmetic", "the device reports the truth on reconnect", False)
STATELESS: Final = Verdict("nothing to carry", "the value is just when it last fired", False)
OWNED: Final = Verdict("not needed", "the integration restores this itself", False)
CHURN: Final = Verdict("expensive", "changes constantly — keeps the flush running", False)


#: Deliberately explicit rather than pattern-matched. A domain nobody has
#: classified gets `UNKNOWN` below, which says so honestly instead of guessing.
DOMAIN_VERDICTS: Final[dict[str, Verdict]] = {
    # -- Home Assistant is the only thing that knows the value ---------------
    "input_boolean": GUARD,
    "input_number": REPLICATE,
    "input_text": REPLICATE,
    "input_select": REPLICATE,
    "input_datetime": REPLICATE,
    "counter": REPLICATE,
    "timer": REPLICATE,
    "automation": Verdict("replicate", "on/off survives, and it is actually applied", True),
    "todo": REPLICATE,
    "schedule": REPLICATE,
    # -- rebuilt from something else, usually within seconds -----------------
    "device_tracker": Verdict("not needed", "your trackers report position again at once", False),
    "person": Verdict("not needed", "recalculated from that person's trackers", False),
    "zone": Verdict("not needed", "defined in configuration, not discovered", False),
    "sun": Verdict("not needed", "calculated from your latitude and the clock", False),
    "weather": REBUILDS,
    "calendar": Verdict("not needed", "re-fetched from the calendar it mirrors", False),
    "geo_location": REBUILDS,
    # -- the device or service is the authority ------------------------------
    "light": DEVICE,
    "switch": DEVICE,
    "fan": DEVICE,
    "cover": DEVICE,
    "valve": DEVICE,
    "lock": DEVICE,
    # Not simply device-backed, and I classified it that way once before being
    # corrected by the evidence. SEVEN Home Assistant core integrations use
    # RestoreEntity for climate -- generic_thermostat among them, the commonest
    # DIY thermostat there is -- so for those users the setpoint is Home
    # Assistant's own and losing it reverts their heating. For a HomeKit or
    # Z-Wave thermostat the device is the authority and this is cosmetic. The
    # label has to cover both, because the form cannot know which they have.
    "climate": Verdict("replicate", "yours with generic_thermostat; the device's otherwise", True),
    "humidifier": DEVICE,
    "water_heater": DEVICE,
    "vacuum": DEVICE,
    "media_player": Verdict("cosmetic", "the player reports what it is doing", False),
    "camera": Verdict("cosmetic", "recording state comes from the camera", False),
    "sensor": Verdict("cosmetic", "overwritten by the next reading, seconds later", False),
    "binary_sensor": DEVICE,
    "number": DEVICE,
    "text": DEVICE,
    "select": DEVICE,
    "siren": DEVICE,
    "remote": DEVICE,
    "lawn_mower": DEVICE,
    "infrared": DEVICE,
    "radio_frequency": DEVICE,
    # -- there is no state worth the name ------------------------------------
    "button": Verdict("nothing to carry", "its value is when it was last pressed", False),
    "event": Verdict("nothing to carry", "its value is when it last fired", False),
    "scene": STATELESS,
    "notify": Verdict("nothing to carry", "a way to send, not a thing with a state", False),
    "stt": STATELESS,
    "tts": STATELESS,
    "wake_word": STATELESS,
    "conversation": STATELESS,
    "assist_satellite": STATELESS,
    "ai_task": STATELESS,
    "image": STATELESS,
    # -- something else already restores it ----------------------------------
    "script": Verdict("not needed", "'on' means running — do not carry that over", False),
    "alarm_control_panel": Verdict("not needed", "your alarm keeps its own state file", False),
    "update": Verdict("not needed", "recalculated by checking versions again", False),
    "device_automation": OWNED,
    "tag": STATELESS,
}

#: Domains this module does NOT recommend that `DEFAULT_INCLUDE_DOMAINS` ships
#: anyway. Recorded explicitly rather than quietly reconciled, because the two
#: lists answer different questions: the default is what a stranger's cluster
#: starts with, and this module is what we would tell them if they asked.
#:
#: Zero core integrations use `RestoreEntity` for `vacuum` or `water_heater`,
#: and one does for `humidifier` -- so on most estates these are pure
#: device-backed cosmetics. They are also tiny and change rarely, so the cost of
#: leaving them is close to nothing, and removing a shipped default breaks the
#: replication of anyone running a custom integration that *does* restore them.
#:
#: The test on this set exists so the disagreement stays deliberate. If a domain
#: joins or leaves it, someone has to say why.
LEGACY_DEFAULTS_NOT_RECOMMENDED: Final[frozenset[str]] = frozenset(
    {"vacuum", "humidifier", "water_heater"}
)

UNKNOWN: Final = Verdict(
    "unclassified",
    "not assessed — replicate only if you know why",
    False,
)


def verdict_for(domain: str) -> Verdict:
    """What we think of replicating `domain`, and why."""
    return DOMAIN_VERDICTS.get(domain, UNKNOWN)


def label_for(domain: str, *, count: int | None = None) -> str:
    """The picker's label: the domain, what we think, and why.

    `count` is the number of entities the operator actually has in that domain.
    It is the difference between "device_tracker is not needed" as an abstract
    claim and "device_tracker (1436) is not needed", which is the number that
    made the owner look twice.
    """
    v = verdict_for(domain)
    head = f"{domain} ({count})" if count else domain
    return f"{head} — {v.tag}: {v.why}"
