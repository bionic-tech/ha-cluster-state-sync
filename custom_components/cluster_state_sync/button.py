"""The one step this integration cannot do for you.

Years of long-term statistics cannot arrive through the replication window --
it carries about half a megabyte a day, and there are six and a half million
rows already on disk. So the standby needs a one-off seed, and moving a file of
that size between two machines is the one job that has no native Home Assistant
answer: no supported API takes a multi-hundred-megabyte upload, and a
node-to-node copy would need the standby to reach the leader over SSH, which
nothing else in this design does and Home Assistant OS could not do at all.

Rather than invent a transport, this writes the file and tells the operator
exactly what to run. The instruction is a persistent notification and not a log
line, because AR-0040 is this project's founding incident: a step that quietly
did not happen, whose only symptom was a line nobody read.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_HA_CONTAINER,
    DATA_COORDINATOR,
    DATA_LEADERSHIP,
    DATA_STATISTICS,
    DOMAIN,
    STATISTICS_SEED_NAME,
)
from .coordinator import BackendHealthCoordinator
from .entity import ClusterSyncDiagnosticEntity
from .statistics_publisher import RECORDER_DB_NAME
from .statistics_sync import write_seed

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the seed button, and only where it has something to do."""
    runtime = entry.runtime_data
    if runtime.get(DATA_STATISTICS) is None:
        # Statistics replication is off. A button that seeds a mechanism
        # nobody is running would produce a ~500 MB file for no reason and
        # instructions that lead nowhere.
        return
    async_add_entities(
        [StatisticsSeedButton(runtime[DATA_COORDINATOR], entry, runtime.get(DATA_LEADERSHIP))]
    )


class StatisticsSeedButton(ClusterSyncDiagnosticEntity, ButtonEntity):
    """Write a seed for the standby, and say what to do with it."""

    _attr_translation_key = "statistics_seed"

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        leadership: Any = None,
    ) -> None:
        super().__init__(coordinator, entry, "statistics_seed")
        self._leadership = leadership
        # AR-0048. Two presses race on one `.tmp` path, and the loser wins:
        # both write the same file and whichever renames last decides what the
        # standby gets. A press takes seconds on a 6.4-million-row table, which
        # is exactly long enough for someone to press it again.
        self._lock = asyncio.Lock()

    async def async_press(self) -> None:
        if self._lock.locked():
            _notify(
                self.hass,
                "Statistics seed already being written",
                "A seed is being written right now. It takes a few seconds on a large "
                "database — wait for the 'ready to copy' notification rather than "
                "pressing again.",
            )
            return

        # AR-0048. A follower's recorder is the stale one: on a warm standby
        # this would write a seed from the copy that is BEHIND and hand the
        # operator instructions to copy it in the wrong direction, overwriting
        # the good history with the bad.
        if self._leadership is not None and not await self._leadership.async_is_leader():
            _notify(
                self.hass,
                "Statistics seed NOT written — this node is not the leader",
                "A seed must be written on the node whose history is authoritative. "
                "This node is a standby, so its recorder is the copy that is behind: "
                "seeding from it would send the wrong history to the other node. "
                "Press this on the leader instead.",
            )
            return

        async with self._lock:
            await self._write_seed()

    async def _write_seed(self) -> None:
        config_dir = self.hass.config.path()
        out = self.hass.config.path(STATISTICS_SEED_NAME)

        def _write() -> int:
            return write_seed(f"{config_dir}/{RECORDER_DB_NAME}", out)

        try:
            # Blocking, and on a 6.4-million-row table not briefly. Straight to
            # an executor, like every other database call in this integration.
            rows = await self.hass.async_add_executor_job(_write)
        except Exception as err:  # noqa: BLE001 — a button press must not raise into the UI
            _LOGGER.exception("Writing the statistics seed failed")
            _notify(
                self.hass,
                "Statistics seed FAILED",
                f"Could not write the seed: {err}\n\n"
                "If this says the recorder has no readable schema version, this "
                "install is probably not using SQLite — on a shared database both "
                "nodes already share history and no seed is needed.",
            )
            return

        size = await self.hass.async_add_executor_job(_size_of, out)
        container = self._entry.data.get(CONF_HA_CONTAINER, "homeassistant")
        _notify(
            self.hass,
            "Statistics seed ready to copy",
            f"Wrote **{rows:,} statistics rows** ({size}) to:\n\n"
            f"`{out}`\n\n"
            "Copy it to the **same filename** in the standby's Home Assistant "
            "config directory. From this host, that is roughly:\n\n"
            f"```\nscp {out} <standby>:<standby-config-dir>/{STATISTICS_SEED_NAME}\n```\n\n"
            "The path inside this container is not the path on the host — if the "
            f"`{container}` container bind-mounts its config, copy from the host "
            "side of that mount.\n\n"
            "Nothing else is needed. The standby checks the file with SQLite's own "
            "integrity check on its next pull, adopts it if it is whole, and starts "
            "applying updates to it. If the copy was still running it refuses the "
            "file and leaves it alone, so a premature run is safe — it will pick it "
            "up on the following pass.\n\n"
            "You can delete this file from here once the standby has adopted it.",
        )


def _size_of(path: str) -> str:
    """A size an operator can weigh against their patience."""
    try:
        return f"{os.stat(path).st_size / 1e6:,.0f} MB"
    except OSError:
        return "unknown size"


def _notify(hass: HomeAssistant, title: str, message: str) -> None:
    """A persistent notification, not a log line — see this module's docstring."""
    hass.async_create_task(
        hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": f"{DOMAIN}_statistics_seed",
            },
            blocking=False,
        )
    )
