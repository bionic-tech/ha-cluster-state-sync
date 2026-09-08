"""What kind of disk is the config directory on?

Used to pick a **kind** default for the recorder snapshot interval, and to warn
honestly when someone chooses an interval that will wear their drive.

The snapshot deliberately lives **in the config tree**, beside everything else
this integration touches. Putting it on a different device would spare the
wear and introduce drift: the go-bag, the swap script and every operator's
mental model all assume one location, and "I'll just copy it from A to B" is
how that assumption gets broken at 3am.

So the answer to wear is the *interval*, not the *location* -- which means the
interval has to be an informed choice, which means detecting the disk.

**This runs inside the Home Assistant container and needs nothing from the
host.** Verified on a real deployment 2026-09-08: `/sys/dev/block` is visible,
`os.stat("/config").st_dev` gives `8:16`, and that resolves to `sdb` with
`queue/rotational == 0` and model `SNV2S500G`.

🚨 **It fails toward saying "I do not know".** Every branch that cannot
establish the answer returns `None`, and a `None` must never produce a warning
or an aggressive default -- an integration that nags about drive wear it cannot
actually see is worse than one that says nothing (ADR-008).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import pathlib
from typing import Final

_LOGGER = logging.getLogger(__name__)

#: Snapshot cadence when the config directory is on spinning rust, where write
#: endurance is not a consideration.
SPINNING_DEFAULT_MINUTES: Final = 15

#: Snapshot cadence on flash. Each snapshot rewrites the whole compacted
#: database, so the interval is a write-endurance decision rather than a CPU
#: one -- measured on a 2.21 GB database: 11.7s to produce a 1.58 GB snapshot,
#: which is 1.3% duty at 15 minutes and **152 GB/day of writes**.
FLASH_DEFAULT_MINUTES: Final = 30

#: Below this, on flash, the wizard says what it will cost. It does not refuse:
#: someone who has read the number and still wants 5 minutes may have enterprise
#: storage, or may simply not care.
FLASH_WARN_BELOW_MINUTES: Final = 20


@dataclass(frozen=True)
class StorageFacts:
    """What could be established about the disk under a path.

    `rotational is None` means unknown, and unknown is a first-class answer
    here rather than a failure -- Home Assistant OS, overlay filesystems,
    network mounts and unusual container runtimes all legitimately hide this.
    """

    rotational: bool | None
    model: str | None
    device: str | None

    @property
    def is_flash(self) -> bool:
        """True only when flash was positively established."""
        return self.rotational is False

    @property
    def default_minutes(self) -> int:
        """A kind default. Unknown storage gets the cautious one."""
        return SPINNING_DEFAULT_MINUTES if self.rotational is True else FLASH_DEFAULT_MINUTES

    def describe(self) -> str:
        """One line for the wizard, naming what was actually detected."""
        if self.rotational is None:
            return "storage type could not be detected"
        kind = "spinning disk" if self.rotational else "flash (SSD/NVMe)"
        return f"{kind}{f' — {self.model}' if self.model else ''}"


def inspect_path(path: str) -> StorageFacts:
    """Establish what `path` sits on, or return unknowns."""
    try:
        st = os.stat(path)
    except OSError as err:
        _LOGGER.debug("Cannot stat %s for storage detection: %s", path, err)
        return StorageFacts(None, None, None)

    node = pathlib.Path(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
    try:
        real = node.resolve(strict=True)
    except (OSError, RuntimeError):
        _LOGGER.debug("No /sys entry for the device behind %s", path)
        return StorageFacts(None, None, None)

    rotational: bool | None = None
    model: str | None = None
    # A partition's queue/ lives on its parent, so try both.
    for candidate in (real / "queue" / "rotational", real.parent / "queue" / "rotational"):
        try:
            rotational = candidate.read_text().strip() == "1"
            break
        except OSError:
            continue
    for candidate in (real / "device" / "model", real.parent / "device" / "model"):
        try:
            model = candidate.read_text().strip() or None
            break
        except OSError:
            continue
    return StorageFacts(rotational, model, real.name)


def wear_warning(facts: StorageFacts, minutes: int, snapshot_bytes: int) -> str | None:
    """Say what an interval costs, in bytes per day, or nothing at all.

    Returns `None` unless flash was **positively** detected and the interval is
    below the threshold. The number is written per day rather than as a
    predicted lifetime on purpose: a drive's rated endurance is a datasheet
    figure this cannot read, so claiming "you will wear this out in N years"
    would be asserting something never established.
    """
    if not facts.is_flash or minutes >= FLASH_WARN_BELOW_MINUTES or minutes <= 0:
        return None
    per_day = snapshot_bytes * (1440 / minutes)
    return (
        f"Every snapshot rewrites the whole database — about "
        f"{snapshot_bytes / 1e9:.1f} GB. At {minutes} minutes that is roughly "
        f"{per_day / 1e9:.0f} GB written per day to "
        f"{facts.model or 'this drive'}, which is flash. Compare that against "
        f"your drive's rated endurance (TBW) before choosing it; on a consumer "
        f"SSD it can shorten its life materially. A longer interval only costs "
        f"you history if the node dies in the meantime."
    )
