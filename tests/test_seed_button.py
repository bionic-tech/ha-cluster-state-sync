"""The seed button — the one manual step in the whole integration.

Nothing covered it. That matters more than the coverage number says, because
this button is the only place where an operator's press writes a file that
another node will *adopt as its history*. Two of its guards exist because
adversarial review found ways to lose the good copy:

* **AR-0048, wrong direction.** A follower's recorder is the one that is
  behind. Seeding from it hands the operator instructions to copy the stale
  history over the authoritative history.
* **AR-0048, the race.** Two presses write the same `.tmp` path and whichever
  renames last decides what the standby gets. A press takes seconds on 6.4
  million rows, which is exactly long enough to press it again.

Both were fixed in code and asserted nowhere.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.cluster_state_sync.button import StatisticsSeedButton
from custom_components.cluster_state_sync.const import DOMAIN

SEED = "custom_components.cluster_state_sync.button.write_seed"


def _button(hass: HomeAssistant, *, is_leader: bool | None = True) -> StatisticsSeedButton:
    entry = MockConfigEntry(domain=DOMAIN, data={"ha_container": "homeassistant"})
    entry.add_to_hass(hass)
    leadership = None
    if is_leader is not None:
        leadership = MagicMock()

        async def _resolve() -> bool:
            return is_leader

        leadership.async_is_leader = _resolve
    button = StatisticsSeedButton(MagicMock(), entry, leadership)
    button.hass = hass
    return button


def _notifications(hass: HomeAssistant) -> list:
    return async_mock_service(hass, "persistent_notification", "create")


async def test_a_follower_refuses_to_seed(hass: HomeAssistant) -> None:
    """🚨 AR-0048. Seeding from the stale copy sends history the wrong way.

    The refusal has to be visible, not silent: an operator who presses a button
    and sees nothing presses it again, or concludes the feature is broken.
    """
    notes = _notifications(hass)
    button = _button(hass, is_leader=False)

    with patch(SEED) as write:
        await button.async_press()
        await hass.async_block_till_done()

    assert not write.called, "a standby wrote a seed from its own stale recorder"
    assert len(notes) == 1
    assert "NOT written" in notes[0].data["title"]
    assert "leader" in notes[0].data["message"]


async def test_the_leader_writes_the_seed_and_says_what_to_do_with_it(
    hass: HomeAssistant,
) -> None:
    """The seed is useless without the copy instruction, so both are one step."""
    notes = _notifications(hass)
    button = _button(hass, is_leader=True)

    with patch(SEED, return_value=6_409_818):
        await button.async_press()
        await hass.async_block_till_done()

    assert len(notes) == 1
    assert "ready to copy" in notes[0].data["title"]
    assert "6,409,818" in notes[0].data["message"], "the row count is how you know it worked"
    assert "scp" in notes[0].data["message"]


async def test_a_second_press_while_one_is_running_is_refused(hass: HomeAssistant) -> None:
    """🚨 AR-0048's race. Two writers, one `.tmp` path, last rename wins."""
    notes = _notifications(hass)
    button = _button(hass, is_leader=True)
    started = asyncio.Event()
    release = asyncio.Event()

    def _slow(*_args: object) -> int:
        # 🚨 `started.set()` directly here is a thread-safety bug, and an
        # intermittent one — which is the worst kind.
        #
        # This runs in an executor thread. `asyncio.Event.set()` calls
        # `loop.call_soon`, which raises `RuntimeError: Non-thread-safe
        # operation invoked on an event loop other than the current one` — but
        # only SOMETIMES, because CPython's check returns early while the loop
        # is not marked running. When it does raise, `button.py` catches it and
        # logs "Writing the statistics seed failed", so `started` is never set
        # and the `await started.wait()` below blocks forever.
        #
        # That hung a full suite run for twenty minutes on 2026-09-11 and did
        # not reproduce on the next one. `--timeout` in pyproject.toml is what
        # eventually named it.
        hass.loop.call_soon_threadsafe(started.set)
        # Block in the executor, as the real write does on a large database.
        asyncio.run_coroutine_threadsafe(release.wait(), hass.loop).result(timeout=5)
        return 1

    with patch(SEED, side_effect=_slow):
        first = hass.async_create_task(button.async_press())
        await started.wait()
        await button.async_press()  # the impatient second press
        release.set()
        await first
        await hass.async_block_till_done()

    titles = [n.data["title"] for n in notes]
    assert "Statistics seed already being written" in titles
    assert sum("ready to copy" in t for t in titles) == 1, "two presses wrote two seeds"


async def test_a_failed_write_notifies_rather_than_raising_into_the_ui(
    hass: HomeAssistant,
) -> None:
    """A button press that raises shows a red toast and no explanation.

    The failure worth explaining is the common one: a shared database has no
    SQLite file to seed from, and needs no seed at all.
    """
    notes = _notifications(hass)
    button = _button(hass, is_leader=True)

    with patch(SEED, side_effect=RuntimeError("no readable schema version")):
        await button.async_press()  # must not raise
        await hass.async_block_till_done()

    assert len(notes) == 1
    assert "FAILED" in notes[0].data["title"]
    assert "shared database" in notes[0].data["message"]


async def test_no_leadership_monitor_means_no_refusal(hass: HomeAssistant) -> None:
    """A single-node install has no leadership source and must still seed."""
    notes = _notifications(hass)
    button = _button(hass, is_leader=None)

    with patch(SEED, return_value=12):
        await button.async_press()
        await hass.async_block_till_done()

    assert "ready to copy" in notes[0].data["title"]
