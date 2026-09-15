"""Holding failover across a restart the integration knows about.

On 2026-09-10 a `docker restart` on the lease holder moved the house. The
promoter probes every 10 seconds, could not reach Home Assistant during a 65s
restart, released the lease and dropped four radios — and the restart command
returned three seconds *after* the demotion had already run.

Every part of that was correct behaviour. The promoter cannot tell "died" from
"being restarted on purpose", and should not guess. So the integration, which
does know, says so on its way out.
"""

from __future__ import annotations

from datetime import UTC, datetime
import pathlib

from custom_components.cluster_state_sync.hold import (
    AUTO_HOLD_MARKER,
    AUTO_HOLD_SECONDS,
    clear_auto_hold,
    hold_path,
    is_held,
    set_auto_hold,
)


def test_an_automatic_hold_is_written_and_carries_a_deadline(tmp_path: pathlib.Path) -> None:
    assert set_auto_hold(str(tmp_path)) is True
    assert is_held(str(tmp_path))

    text = hold_path(str(tmp_path)).read_text(encoding="utf-8")
    assert AUTO_HOLD_MARKER in text
    assert "expires:" in text

    stamp = next(
        line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("expires:")
    )
    deadline = datetime.fromisoformat(stamp)
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= AUTO_HOLD_SECONDS + 5


def test_an_operators_hold_is_never_stamped_with_an_expiry(tmp_path: pathlib.Path) -> None:
    """🚨 The one that would quietly resume failover mid-maintenance.

    A person's hold is indefinite on purpose — they said "not yet", and only a
    person says otherwise. Writing our deadline onto it would restart failover
    during exactly the work they asked us to sit out.
    """
    hold_path(str(tmp_path)).write_text("upgrading the switch\n", encoding="utf-8")

    assert set_auto_hold(str(tmp_path)) is False
    assert hold_path(str(tmp_path)).read_text(encoding="utf-8") == "upgrading the switch\n"


def test_we_only_clear_a_hold_we_set(tmp_path: pathlib.Path) -> None:
    """On start-up we cannot know whose hold is on disk. Only ours is ours."""
    hold_path(str(tmp_path)).write_text("somebody is under the floor\n", encoding="utf-8")

    assert clear_auto_hold(str(tmp_path)) is False
    assert is_held(str(tmp_path)), "an operator's hold was cleared by the integration"


def test_our_own_hold_is_cleared_when_we_come_back(tmp_path: pathlib.Path) -> None:
    set_auto_hold(str(tmp_path))
    assert clear_auto_hold(str(tmp_path)) is True
    assert not is_held(str(tmp_path))


def test_clearing_when_nothing_is_held_is_not_an_error(tmp_path: pathlib.Path) -> None:
    assert clear_auto_hold(str(tmp_path)) is False


def test_the_deadline_is_long_enough_and_short_enough() -> None:
    """🚨 The number, and why it is that number.

    Long enough: the slowest Home Assistant restart measured on the reference
    pair was 65 seconds, so 300 covers it nearly five times.

    Short enough: the promoter's probe grace is 600 seconds and is anchored to
    the state file's mtime, which a hold does NOT touch — so the grace clock
    runs underneath the hold rather than being reset by it, and a crash during
    an automatic hold still demotes at ~600s exactly as it would without one.

    If this is ever raised past the probe grace, that stops being true and an
    automatic hold starts *delaying* real failover.
    """
    assert AUTO_HOLD_SECONDS >= 120, "shorter than a slow restart; the hold would lapse mid-restart"
    assert AUTO_HOLD_SECONDS < 600, (
        "at or past the promoter's probe grace, an automatic hold would start "
        "adding to the time a genuine crash takes to fail over"
    )
