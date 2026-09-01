"""Diagnostic state for Cluster State Sync (AR-0019, AR-0032).

The v1 review's third theme was that this integration was "built to fail safe,
but blind while doing it" — the graceful-degradation design is sound, but with
no tests, no CI and no observability, AR-0001 and a dead backend both looked
exactly like a healthy green failover. Nobody would have been paged.

This module is the "nobody gets paged" half of that. `entities_tracked` in
particular is the number that makes AR-0001 self-evident: a snapshot carrying
three entities on a system tracking two hundred is not subtle once it is on
screen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backend import ClusterBackend
from .const import DOMAIN, HEALTH_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class SyncStats:
    """Counters that outlive any single flush or restore."""

    restored_count: int = 0
    last_restore_at: datetime | None = None

    def record_restore(self, count: int) -> None:
        self.restored_count = count
        self.last_restore_at = datetime.now(tz=UTC)


class BackendHealthCoordinator(DataUpdateCoordinator[bool]):
    """Polls the backend so a dead one is visible rather than merely quiet.

    `health()` existed in v0.1 and was never called from anywhere, which is
    AR-0019: the integration could sit happily failing every write while
    reporting nothing at all.
    """

    def __init__(self, hass: HomeAssistant, backend: ClusterBackend) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_backend_health",
            update_interval=timedelta(seconds=HEALTH_POLL_INTERVAL),
        )
        self._backend = backend

    async def _async_update_data(self) -> bool:
        # Deliberately does not raise UpdateFailed on a down backend: "the
        # backend is down" is the reading we want to publish, not an error that
        # makes the entity unavailable and hides it.
        return await self._backend.health()
