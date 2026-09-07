"""The maintenance hold switch.

Restarting Home Assistant on the leader is a full failover, and the flag that
prevents it lived only in a host script the person pressing Restart was not
looking at. It has now cost this fleet a real outage. These tests guard the
properties that make the switch trustworthy rather than decorative.
"""

from __future__ import annotations

import pathlib

from custom_components.cluster_state_sync import hold


def test_set_then_clear_round_trips(tmp_path: pathlib.Path) -> None:
    d = str(tmp_path)
    assert hold.is_held(d) is False
    hold.set_hold(d, "planned upgrade")
    assert hold.is_held(d) is True
    assert "planned upgrade" in hold.hold_reason(d)
    hold.clear_hold(d)
    assert hold.is_held(d) is False


def test_clearing_an_absent_hold_is_success(tmp_path: pathlib.Path) -> None:
    """The host script may have cleared it already.

    Two paths to one flag must not disagree about the outcome: `cluster-hold.sh
    off` at the console followed by the switch being turned off is an ordinary
    sequence, not an error.
    """
    hold.clear_hold(str(tmp_path))  # must not raise


def test_the_hold_is_written_atomically(tmp_path: pathlib.Path) -> None:
    """The promoter reads this file on a ~11s tick, from another process.

    A half-written reason is a hold whose text is garbage; worse, a reader that
    catches the file mid-create could see it absent. Write-then-rename means the
    promoter sees either the old state or the new one.
    """
    d = str(tmp_path)
    hold.set_hold(d, "atomic")
    assert not list(tmp_path.glob("*.tmp*")), "temp file left behind"
    assert hold.hold_path(d).is_file()


def test_set_hold_does_not_fail_soft(tmp_path: pathlib.Path) -> None:
    """The readers fail toward NOT holding; the writer must not.

    A hold that silently fails to apply is worse than none: the operator
    believes failover is suspended, restarts Home Assistant, and the peer takes
    over anyway — the exact accident the flag exists to prevent.
    """
    missing = tmp_path / "does-not-exist"
    try:
        hold.set_hold(str(missing))
    except OSError:
        pass  # correct: the caller must learn about it
    else:  # pragma: no cover
        raise AssertionError("set_hold swallowed a write failure")


def test_the_switch_is_a_control_not_a_diagnostic() -> None:
    """Diagnostics are hidden under the device page. A control nobody can find
    is why the hold went unused through two outages."""
    src = (
        pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync/switch.py"
    ).read_text(encoding="utf-8")
    assert "_attr_entity_category" not in src, (
        "an entity_category would bury the switch in diagnostics"
    )


def test_state_is_read_from_disk_not_remembered() -> None:
    """The host script writes the same file.

    If the switch cached its own state, `cluster-hold.sh off` at the console
    would leave it showing `on` — two controls over one flag, disagreeing.
    """
    src = (
        pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync/switch.py"
    ).read_text(encoding="utf-8")
    assert "async def async_update" in src
    assert "is_held" in src, "state must come from the flag file"


# -- operator-requested handover -------------------------------------------


def test_handover_request_round_trips(tmp_path: pathlib.Path) -> None:
    from custom_components.cluster_state_sync import handover

    d = str(tmp_path)
    assert handover.is_requested(d) is False
    handover.request_handover(d, "planned move")
    assert handover.is_requested(d) is True
    handover.clear_request(d)
    assert handover.is_requested(d) is False
    handover.clear_request(d)  # idempotent


def test_a_handover_request_fails_toward_staying_put(tmp_path: pathlib.Path) -> None:
    """An unreadable flag must not move a house between machines.

    Same posture as `hold.is_held`: the reader fails toward doing nothing.
    """
    from custom_components.cluster_state_sync import handover

    assert handover.is_requested("/nonexistent-directory") is False


def test_the_handover_switch_is_offered_only_on_the_leader() -> None:
    """A follower has no lease to give away.

    A button that looks live and is inert is how an operator learns to
    distrust the page — and this one claims it will move their house.
    """
    js = (
        pathlib.Path(__file__).parent.parent
        / "custom_components/cluster_state_sync/panel/cluster_status_panel.js"
    ).read_text(encoding="utf-8")
    assert "_handoverRow" in js
    assert "nothing to hand over" in js, "the follower case must be stated, not hidden"
    assert "is_leader" in js.split("_handoverRow")[1][:800], "must gate on leadership"


def test_action_colours_encode_risk_and_are_explained() -> None:
    """Colour that is decoration teaches nothing. The legend makes it a claim."""
    js = (
        pathlib.Path(__file__).parent.parent
        / "custom_components/cluster_state_sync/panel/cluster_status_panel.js"
    ).read_text(encoding="utf-8")
    assert ".toggle.danger" in js and "--error-color" in js
    assert "moves the house" in js, "the danger tier must say what it means"
    assert "legend" in js
