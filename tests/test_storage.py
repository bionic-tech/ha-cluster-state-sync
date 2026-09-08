"""Storage detection for the recorder snapshot interval.

The snapshot lives in the config tree deliberately -- moving it to a cheaper
disk would spare the wear and introduce drift, because the go-bag, the swap
script and every operator's mental model assume one location. So the answer to
write endurance is the *interval*, which makes it a decision the wizard has to
inform rather than guess.
"""

from __future__ import annotations

import pathlib

from custom_components.cluster_state_sync.storage import (
    FLASH_DEFAULT_MINUTES,
    SPINNING_DEFAULT_MINUTES,
    StorageFacts,
    inspect_path,
    wear_warning,
)

SNAP = 1_580_000_000  # the measured 1.58 GB compacted snapshot


def test_flash_is_detected_and_gets_the_cautious_default() -> None:
    facts = StorageFacts(rotational=False, model="SNV2S500G", device="sdb")
    assert facts.is_flash is True
    assert facts.default_minutes == FLASH_DEFAULT_MINUTES
    assert "flash" in facts.describe()
    assert "SNV2S500G" in facts.describe()


def test_spinning_gets_the_shorter_default() -> None:
    """Write endurance is not a consideration, so lose less history."""
    facts = StorageFacts(rotational=True, model="ST8000DM004", device="sdd")
    assert facts.is_flash is False
    assert facts.default_minutes == SPINNING_DEFAULT_MINUTES


def test_unknown_storage_gets_the_CAUTIOUS_default_not_the_aggressive_one() -> None:
    """🚨 Unknown must never be treated as spinning.

    Home Assistant OS, overlay filesystems and network mounts all legitimately
    hide this. Guessing 'spinning' there would hand a 15-minute interval to a
    machine we know nothing about.
    """
    facts = StorageFacts(rotational=None, model=None, device=None)
    assert facts.is_flash is False, "unknown must not be reported as flash either"
    assert facts.default_minutes == FLASH_DEFAULT_MINUTES


def test_no_warning_when_the_disk_could_not_be_identified() -> None:
    """An integration that nags about wear it cannot see is worse than silent.

    This is ADR-008 applied to a warning rather than a sensor: do not assert
    what was never established.
    """
    facts = StorageFacts(rotational=None, model=None, device=None)
    assert wear_warning(facts, minutes=5, snapshot_bytes=SNAP) is None


def test_no_warning_on_spinning_storage_however_short_the_interval() -> None:
    facts = StorageFacts(rotational=True, model="ST8000DM004", device="sdd")
    assert wear_warning(facts, minutes=1, snapshot_bytes=SNAP) is None


def test_flash_below_the_threshold_is_warned_with_a_real_number() -> None:
    """The number must be bytes per day, not a predicted lifetime.

    Rated endurance (TBW) is a datasheet figure this cannot read, so claiming
    'you will wear this out in N years' would assert something never
    established. Bytes per day is measured; the comparison is the operator's.
    """
    facts = StorageFacts(rotational=False, model="SNV2S500G", device="sdb")
    msg = wear_warning(facts, minutes=15, snapshot_bytes=SNAP)
    assert msg is not None
    assert "152 GB" in msg, msg  # 1.58 GB * (1440/15)
    assert "SNV2S500G" in msg
    assert "year" not in msg, "claimed a lifetime it cannot know"


def test_no_warning_at_or_above_the_threshold() -> None:
    facts = StorageFacts(rotational=False, model="SNV2S500G", device="sdb")
    assert wear_warning(facts, minutes=30, snapshot_bytes=SNAP) is None
    assert wear_warning(facts, minutes=20, snapshot_bytes=SNAP) is None


def test_a_missing_path_yields_unknowns_rather_than_raising(
    tmp_path: pathlib.Path,
) -> None:
    """Detection runs during the config flow. It may not throw."""
    facts = inspect_path(str(tmp_path / "does-not-exist"))
    assert facts == StorageFacts(None, None, None)


def test_detection_against_the_real_filesystem_never_raises() -> None:
    """Whatever this test machine is, asking must be safe.

    It may legitimately answer 'unknown' -- in CI, in a container, on a Mac.
    What it may not do is raise, because that would fail a wizard step.
    """
    facts = inspect_path("/")
    assert isinstance(facts, StorageFacts)
    assert facts.rotational in (True, False, None)
