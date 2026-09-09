"""Publish the long-term-statistics window. Leader only.

The counterpart to `scripts/statistics_pull.py`, which applies what this
publishes on a node whose Home Assistant is stopped.

**Why a rolling window rather than a delta log.** A delta stream is only
correct while the follower keeps up. A cold standby is off for weeks by
design, and a follower that missed three deltas has a hole in its history that
nothing later fills -- and, worse, no way to know it. So every pass publishes
the *whole* window: every statistics row from the last `window_days`. Applying
it is idempotent (`INSERT OR IGNORE`), so a follower that has been off for a
fortnight catches up completely from one fetch, and one that has been off
longer is told, because the payload states the floor it covers.

Measured on a 3,595-entity estate against the live 6.4-million-row database:
7 days = 0.99 MB gzipped, 30 days = 4.48 MB, 90 days = 12.51 MB, the 30-day
export taking 1.7 seconds. The window is cheap because the thing being
replicated is genuinely small; it is the raw `states` table that is enormous,
and that is exactly what this does not carry (ADR-010).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import pathlib
import time
from typing import Any

from .crypto import STATISTICS_AAD, derive_fileset_key, seal
from .statistics_sync import SchemaMismatch, SyncPayload, export_since

_LOGGER = logging.getLogger(__name__)

#: Home Assistant's own recorder database, when it is SQLite. A shared-database
#: install (Postgres/MariaDB) has no such file, which is a valid configuration
#: rather than a fault -- there the two nodes already share history and this
#: whole mechanism is unnecessary.
RECORDER_DB_NAME = "home-assistant_v2.db"

SECONDS_PER_DAY = 86400


@dataclass
class StatisticsResult:
    """What one publish pass did — surfaced through diagnostics."""

    rows: int
    metadata: int
    bytes_written: int
    seconds: float
    watermark: float
    skipped_reason: str | None = None


class StatisticsPublisher:
    """Seal and publish the statistics window to the cluster backend."""

    def __init__(
        self,
        backend: Any,
        *,
        config_dir: str,
        secret: str,
        window_days: int,
        max_bytes: int,
    ) -> None:
        self.backend = backend
        self._db = pathlib.Path(config_dir) / RECORDER_DB_NAME
        self._key = derive_fileset_key(secret)
        self._window_days = window_days
        self._max_bytes = max_bytes
        self.last_result: StatisticsResult | None = None
        #: Separate from `last_result`, which carries no timestamp: it answers
        #: "what did the last pass do", not "how long ago". Mirrors
        #: `FilesetPublisher.last_success_at` for the same reason -- a gauge
        #: that keeps climbing if the loop silently stops.
        self.last_success_at: datetime | None = None
        #: When publishing first succeeded, which is a different question from
        #: when it last did. AR-0057: the follower's silence only means
        #: something once we have been publishing long enough for it to have
        #: had something to fetch. Without this, switching the feature on
        #: raises "replication has stalled" on a correct setup that has simply
        #: not had its first pull yet.
        self.first_success_at: datetime | None = None
        #: The standing refusal, so the error logs on the transition into the
        #: state rather than once every interval for as long as it lasts.
        self._skipped_reason: str | None = None

    def _export(self) -> tuple[SyncPayload, bytes]:
        """Blocking. Runs in an executor — the 30-day export took 1.7s."""
        since = time.time() - self._window_days * SECONDS_PER_DAY
        payload = export_since(self._db, since_ts=since)
        return payload, payload.to_bytes()

    def _skip(self, reason: str, message: str, *args: Any) -> StatisticsResult:
        """Record a refusal, saying so exactly once per transition into it."""
        if self._skipped_reason != reason:
            _LOGGER.error(message, *args)
        self._skipped_reason = reason
        result = StatisticsResult(
            rows=0,
            metadata=0,
            bytes_written=0,
            seconds=0.0,
            watermark=0.0,
            skipped_reason=reason,
        )
        self.last_result = result
        # Deliberately does NOT touch `last_success_at`. Nothing was published,
        # so the age gauge has to keep climbing from the last real success --
        # stamping it here would report a fresh window that does not exist.
        return result

    async def async_publish(self) -> StatisticsResult:
        """Export, seal and publish one window."""
        if not self._db.exists():
            # A shared-database install, or a recorder that has not created
            # its file yet. Neither is an error, and neither is going to
            # change on the next pass, so this must not shout every interval.
            return self._skip(
                "no_sqlite_recorder",
                "Statistics replication is on but there is no SQLite recorder at %s. On a "
                "shared database (Postgres/MariaDB) both nodes already share history and "
                "this is unnecessary — turn it off to silence this.",
                self._db,
            )

        started = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            payload, body = await loop.run_in_executor(None, self._export)
        except SchemaMismatch as err:
            return self._skip("unreadable_schema", "Statistics publish REFUSED: %s", err)
        except Exception as err:  # noqa: BLE001 — a locked or busy recorder
            # Transient by nature (SQLITE_BUSY under a heavy purge), so this
            # is not a standing state: clear the latch so a recurrence after
            # a recovery is reported again.
            self._skipped_reason = None
            return self._skip("export_failed", "Statistics export failed: %s", err)

        if len(body) > self._max_bytes:
            # Publishing a truncated window would look exactly like success
            # and leave a hole nobody sees until a promotion.
            return self._skip(
                "too_large",
                "Statistics publish REFUSED and nothing was published: the %d-day window is "
                "%.1f MB, over the %.1f MB cap. The standby's history stops advancing here. "
                "Shorten the window or raise the cap.",
                self._window_days,
                len(body) / 1e6,
                self._max_bytes / 1e6,
            )

        if self._skipped_reason is not None:
            _LOGGER.info("Statistics publish recovered; the window is being published again")
            self._skipped_reason = None

        await self.backend.write_statistics(seal(self._key, body, aad=STATISTICS_AAD))

        result = StatisticsResult(
            rows=len(payload.rows),
            metadata=len(payload.meta),
            bytes_written=len(body),
            seconds=time.monotonic() - started,
            watermark=payload.watermark,
        )
        self.last_result = result
        self.last_success_at = datetime.now(tz=UTC)
        if self.first_success_at is None:
            self.first_success_at = self.last_success_at
        _LOGGER.debug(
            "Statistics window published: %d rows, %d metadata, %.2f MB, %.1fs",
            result.rows,
            result.metadata,
            result.bytes_written / 1e6,
            result.seconds,
        )
        return result
