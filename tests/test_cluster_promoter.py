"""The host-side promoter (design 2026-08-30).

Keepalived is not installed on either tiger, and nothing else writes
/run/cluster-sync/vrrp-state — so today no promotion ever fires and the only
symptom is a log line. This is what replaces it.

Driven against a fake client: the interesting behaviour is transition
detection and what gets exec'd, none of which needs a socket.
"""

from __future__ import annotations

import ast
from datetime import datetime
import os
import pathlib
import sys
import time

import pytest

from custom_components.cluster_state_sync.lease import (
    FORCE_SCRIPT,
    LEASE_SCRIPT,
    RELEASE_SCRIPT,
    RENEW_ONLY_SCRIPT,
)
from custom_components.cluster_state_sync.scripts import cluster_promoter
from custom_components.cluster_state_sync.scripts.cluster_promoter import (
    adopt,
    decide,
    main,
    read_state,
    run,
)
from custom_components.cluster_state_sync.scripts.resp import RespError

NAMESPACE = "prod"
NODE = "tiger1-abc123"


class FakeClient:
    """Records eval() calls and returns a canned lease answer.

    Heartbeats are recorded SEPARATELY from lease operations. They are an
    observability signal written on every tick regardless of the decision, so
    folding them into `calls` would make every lease assertion in this file
    depend on a diagnostic -- and a test that breaks when a log line is added
    is testing the wrong thing.
    """

    def __init__(self, holds: bool = True) -> None:
        self.holds = holds
        self.calls: list[tuple[str, list[str], list[str]]] = []
        self.heartbeats: list[tuple[str, list[str], list[str]]] = []

    def eval(self, script: str, keys: list[str], args: list[str]) -> int:
        if "promoters:" in (keys[0] if keys else ""):
            self.heartbeats.append((script, keys, args))
            return 1
        self.calls.append((script, keys, args))
        return 1 if self.holds else 0

    def close(self) -> None: ...


def _runner(recorded: list[list[str]], state_path: pathlib.Path | None = None):
    """A fake notify script.

    When `state_path` is given, it also writes the state a real
    `notify_master.sh`/`notify_backup.sh` would have recorded -- needed for
    any test that expects `run()` to report success, now that `run()`
    verifies the transition actually landed. Tests that want to prove `run()`
    itself never writes `state_path` pass no `state_path` here instead.
    """

    def run_it(cmd: list[str]) -> int:
        recorded.append(cmd)
        if state_path is not None:
            new_state = "MASTER" if cmd[0].endswith("notify_master.sh") else "BACKUP"
            state_path.write_text(f"{new_state}\n")
        return 0

    return run_it


# -- decide() -----------------------------------------------------------------


def test_gaining_the_lease_from_backup_is_a_promotion() -> None:
    assert decide(holds_lease=True, previous="BACKUP") == "MASTER"


def test_losing_the_lease_while_master_is_a_demotion() -> None:
    assert decide(holds_lease=False, previous="MASTER") == "BACKUP"


def test_holding_the_lease_while_already_master_is_not_a_transition() -> None:
    """Production change that would make this fail: acting on state rather than
    on change.

    This runs every ten seconds. Re-running notify_master.sh on a timer would
    restart containers and re-apply firewall rules forever. The integration's
    own ServiceGate established this rule — act on transitions, never on a
    schedule — and it is the reason that gate exists.
    """
    assert decide(holds_lease=True, previous="MASTER") is None


def test_not_holding_the_lease_while_already_backup_is_not_a_transition() -> None:
    assert decide(holds_lease=False, previous="BACKUP") is None


def test_an_unknown_previous_state_promotes_when_the_lease_is_held() -> None:
    """First boot: tmpfiles writes BACKUP, but a missing or torn file must not
    strand a node that legitimately holds the lease."""
    assert decide(holds_lease=True, previous="") == "MASTER"


def test_an_unknown_previous_state_demotes_when_the_lease_is_not_held() -> None:
    assert decide(holds_lease=False, previous="") == "BACKUP"


# -- read_state() -------------------------------------------------------------


def test_a_missing_state_file_reads_as_unknown_not_as_master(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: defaulting to MASTER.

    A node that assumes leadership because it cannot read a status file is the
    split-brain this design exists to prevent.
    """
    assert read_state(tmp_path / "absent") == ""


def test_state_is_read_stripped(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "vrrp-state"
    p.write_text("MASTER\n")
    assert read_state(p) == "MASTER"


def test_invalid_utf8_reads_as_a_non_matching_state_rather_than_raising(
    tmp_path: pathlib.Path,
) -> None:
    """A torn write (bundle.py's `_record_vrrp_state` acknowledges this reader
    can catch one in progress) can leave a partial multi-byte sequence.
    `read_text(encoding="utf-8")` alone raises `UnicodeDecodeError` on that --
    a `ValueError`, not an `OSError`, invisible to `run()`'s
    `except (ValkeyError, OSError)`. This must not raise at all: a corrupt
    file has to read as *some* string that decide() can compare against, not
    crash the caller before the lease is ever renewed.
    """
    p = tmp_path / "vrrp-state"
    # \xff is never valid UTF-8 in any position -- a lone byte, not merely an
    # incomplete multi-byte sequence, so this cannot decode by accident.
    p.write_bytes(b"MAS\xffTER")
    result = read_state(p)  # must not raise
    assert result not in ("MASTER", "BACKUP")


# -- run() --------------------------------------------------------------------


def test_a_promotion_execs_notify_master_and_nothing_else(
    tmp_path: pathlib.Path,
) -> None:
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    ran: list[list[str]] = []
    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran, state),
    )
    assert rc == 0
    assert ran == [[str(tmp_path / "notify_master.sh")]]


def test_a_demotion_execs_notify_backup(tmp_path: pathlib.Path) -> None:
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    ran: list[list[str]] = []
    run(
        FakeClient(holds=False),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
    )
    assert ran == [[str(tmp_path / "notify_backup.sh")]]


def test_no_transition_execs_nothing(tmp_path: pathlib.Path) -> None:
    """The anti-thrash property. This is the assertion that would catch a
    regression to acting on state rather than on change."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    ran: list[list[str]] = []
    run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
    )
    assert ran == []


def test_no_transition_still_renews_the_lease(tmp_path: pathlib.Path) -> None:
    """The eval call sits above the `if new_state is None: return 0` gate --
    structurally load-bearing, but nothing previously asserted it stays
    there.

    Production change this catches: moving the lease call below that gate,
    on the reasoning that renewal is only needed when something is about to
    happen. A stable healthy leader would then stop renewing entirely; its
    key would expire 30s later, the standby would legitimately take it, and
    this node would demote itself for no reason -- failover firing on a
    cluster where nothing was wrong. Asserted on the recorded call, not a
    return value, so it fails the moment the call itself disappears.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    client = FakeClient(holds=True)
    ran: list[list[str]] = []
    run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
    )
    assert ran == []
    assert client.calls, "a no-transition tick must still renew the lease"
    _script, _keys, args = client.calls[0]
    assert args[0] == NODE


def test_a_torn_state_file_with_invalid_utf8_still_renews_rather_than_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """AR-0040's shape once more, at the `run()` level rather than
    `read_state()`'s own unit test above.

    Before the fix, `read_text(encoding="utf-8")` alone would raise
    `UnicodeDecodeError` on a torn write -- a `ValueError`, which `run()`'s
    `except (ValkeyError, OSError)` does not catch. That crash happens
    BEFORE the eval call, on every tick: the leader's promoter stops
    renewing the lease without ever discovering it lost it, and the peer
    legitimately takes over while this node never demotes -- exactly the
    ordering hazard the comment above the eval call in `run()` warns
    against, reopened by a corrupt file instead of a bug in the ordering
    itself.
    """
    state = tmp_path / "vrrp-state"
    # \xff is never valid UTF-8 in any position.
    state.write_bytes(b"MAS\xffTER")
    client = FakeClient(holds=True)
    ran: list[list[str]] = []
    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran, state),
    )
    assert rc == 0
    assert client.calls, "the lease must still be renewed despite the corrupt state file"
    # A corrupt "previous" matches neither MASTER nor BACKUP, so it is read as
    # a transition -- the same treatment any other unrecognised value gets.
    assert ran == [[str(tmp_path / "notify_master.sh")]]


def test_the_lease_is_taken_under_this_nodes_own_identity(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: a hardcoded or shared id.

    Two nodes presenting one identity both renew the same lease and both
    believe they lead — the exact defect that cost this project a day.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    client = FakeClient(holds=True)
    run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([]),
    )
    _script, keys, args = client.calls[0]
    assert keys == [f"ha:cluster_state_sync:{NAMESPACE}:leader"]
    assert args[0] == NODE
    assert args[1] == "30000", "PX is milliseconds"


def test_the_promoter_does_not_write_the_state_file(tmp_path: pathlib.Path) -> None:
    """The notify scripts write it, first and atomically, and nothing else does.
    A second writer would race the reader the pull gates on."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([]),
    )
    assert state.read_text() == "BACKUP\n", "the promoter must not have touched it"


def test_a_failing_notify_script_is_reported_not_swallowed(
    tmp_path: pathlib.Path,
) -> None:
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")

    def failing(_cmd: list[str]) -> int:
        return 1

    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=failing,
    )
    assert rc != 0


def test_a_notify_script_that_reports_success_but_never_recorded_the_transition_is_caught(
    tmp_path: pathlib.Path,
) -> None:
    """`notify_master.sh`'s own write of vrrp-state is deliberately non-fatal
    (`bundle.py`'s `_record_vrrp_state`), so a `/run` that exists but is
    unwritable lets it exit 0 having recorded nothing -- this runner is
    exactly that script's behaviour in that condition.

    Production change this catches: deleting the re-read-and-compare block
    that follows a successful exec. Every other test's runner either
    faithfully updates the state file or leaves it stale without ever
    checking `rc`, so nothing else here would notice its removal. This is
    the whole point of that block: the promotion silently did not take, and
    the promoter must say so rather than let every later tick call it a
    promotion again in silence.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    ran: list[list[str]] = []

    def reports_success_but_writes_nothing(cmd: list[str]) -> int:
        ran.append(cmd)
        return 0  # exits clean, exactly like the real script in this failure mode

    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=reports_success_but_writes_nothing,
    )
    assert ran == [[str(tmp_path / "notify_master.sh")]], "the script must still have run"
    assert rc != 0, "must not report success when the transition never actually landed"


# -- the override -------------------------------------------------------------


def test_force_master_promotes_without_consulting_the_lease(
    tmp_path: pathlib.Path,
) -> None:
    """Not consulting is only half of it: it also *claims* the key.

    Skipping the consult but leaving the old key standing is the split-brain
    this override would otherwise reopen: a repaired peer finds the key free
    or still its own, legitimately re-takes it, and now two nodes each
    believe they lead, by two different mechanisms.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    force = tmp_path / "force-master"
    force.touch()
    client = FakeClient(holds=False)  # the lease says no -- irrelevant to force
    ran: list[list[str]] = []
    run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner(ran),
    )
    assert ran == [[str(tmp_path / "notify_master.sh")]]
    script, keys, args = client.calls[0]
    assert script == FORCE_SCRIPT, "must claim the key, not just skip the check"
    assert keys == [f"ha:cluster_state_sync:{NAMESPACE}:leader"]
    assert args[0] == NODE
    assert args[1] == "30000", "PX is milliseconds"


def test_force_master_still_promotes_when_the_lease_write_fails(
    tmp_path: pathlib.Path,
) -> None:
    """The claim is best-effort. force-master exists for when Valkey itself
    may be unreachable, so a failed write must not block the promotion it was
    invoked to force -- only the later convergence (a peer seeing this node's
    id) is lost until Valkey comes back."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    force = tmp_path / "force-master"
    force.touch()

    class DeadClient:
        def eval(self, *_a: object, **_k: object) -> int:
            raise RespError("cannot reach Valkey at valkey.node-ops:6379")

        def close(self) -> None: ...

    ran: list[list[str]] = []
    rc = run(
        DeadClient(),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner(ran, state),
    )
    assert rc == 0
    assert ran == [[str(tmp_path / "notify_master.sh")]]
    assert state.read_text() == "MASTER\n", "notify_master.sh still ran and recorded it"


def test_force_master_records_that_the_guard_was_bypassed(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: overriding silently.

    An override that leaves no trace means a later promotion inherits a bypass
    nobody remembers authorising.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    force = tmp_path / "force-master"
    force.touch()
    run(
        FakeClient(holds=False),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner([]),
    )
    used = force.with_name(force.name + ".used")
    assert used.exists()
    node, _, stamp = used.read_text().strip().partition(" ")
    assert node == NODE
    datetime.fromisoformat(stamp)  # raises ValueError if this is not a real timestamp


def test_the_force_marker_appends_rather_than_replacing_a_suffix(
    tmp_path: pathlib.Path,
) -> None:
    """`Path.with_suffix` *replaces* an existing suffix rather than appending
    to the name, so a force file an operator names with a dot in it would
    otherwise produce `force.used` -- silently dropping `.master` -- instead
    of the `<force-file>.used` an operator would expect to find."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    force = tmp_path / "force.master"
    force.touch()
    run(
        FakeClient(holds=False),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner([]),
    )
    assert (tmp_path / "force.master.used").exists()
    assert not (tmp_path / "force.used").exists()


def test_force_master_left_in_place_neither_reexecs_nor_rewrites_the_marker(
    tmp_path: pathlib.Path,
) -> None:
    """force-master is not a standing order to promote every tick -- only
    decide()'s transition-only rule prevents that, and a plausible future
    change ("force should always force", moving the exec inside the force
    branch) would restart containers every ten seconds with no test failing
    until this one. The marker is the same story: it records when the bypass
    *began*, and rewriting it every tick would silently lose that.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    force = tmp_path / "force-master"
    force.touch()
    client = FakeClient(holds=True)
    ran: list[list[str]] = []

    run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner(ran, state),
    )
    assert ran == [[str(tmp_path / "notify_master.sh")]]
    marker = force.with_name(force.name + ".used")
    first_marker_contents = marker.read_text()

    run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner(ran, state),
    )
    assert ran == [[str(tmp_path / "notify_master.sh")]], "must not exec a second time"
    assert marker.read_text() == first_marker_contents, "must record when the bypass began"


# -- failing closed -----------------------------------------------------------


def test_an_unreachable_valkey_changes_nothing_and_reports_it(
    tmp_path: pathlib.Path,
) -> None:
    """Decision D2. A backend that cannot answer is not a yes — the same
    posture LeadershipMonitor already takes."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")

    class DeadClient:
        """Raises what the REAL client raises.

        `ValkeyClient.eval` can only fail with `RespError` -- it subclasses
        `ValkeyError`, and `resp.py` wraps every socket, TLS, DNS and
        malformed-reply failure into it. A fake raising the promoter's own
        `PromoterError` would prove fail-closed works for an exception that
        cannot occur, and `run` catching only `PromoterError` would still
        pass this test while crashing on the real thing.
        """

        def eval(self, *_a: object, **_k: object) -> int:
            raise RespError("cannot reach Valkey at valkey.node-ops:6379")

        def close(self) -> None: ...

    ran: list[list[str]] = []
    rc = run(
        DeadClient(),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
    )
    assert rc != 0
    assert ran == [], "nothing may be promoted or demoted on a backend failure"
    assert state.read_text() == "BACKUP\n"


def test_a_missing_state_directory_is_created_rather_than_refused(
    tmp_path: pathlib.Path,
) -> None:
    """`notify_master.sh` already does `mkdir -p` on this directory
    (`bundle.py`), so a directory that merely does not exist yet is
    self-healing, not a reason to refuse. Refusing here anyway would turn a
    routine `/run` wipe into a promotion the operator has to notice and
    intervene on by hand."""
    state = tmp_path / "fresh-run" / "vrrp-state"  # parent does not exist yet
    ran: list[list[str]] = []
    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran, state),
    )
    assert rc == 0
    assert ran == [[str(tmp_path / "notify_master.sh")]]
    assert state.parent.is_dir()


def test_a_state_directory_that_cannot_be_created_does_not_run_the_notify_script(
    tmp_path: pathlib.Path,
) -> None:
    """The other half of failing closed, and the half a Valkey fake cannot
    reach. Preparing the state directory and exec'ing the notify script are
    the promoter's own file work, and they raise `OSError`, not `ValkeyError`.

    A directory that merely does not exist yet self-heals (see the sibling
    test above), so this blocks the path with a plain file instead -- a
    parent that can never become a directory, which is the case that
    genuinely cannot succeed.

    Production change this catches: `except ValkeyError` alone. The lease
    would be taken, the `mkdir` would blow up unhandled, and the timer would
    show a crashed unit on a node that now silently holds the lease.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    state = blocker / "vrrp-state"
    ran: list[list[str]] = []
    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
    )
    assert rc != 0
    assert ran == [], "a state directory that cannot be created must not promote"


# -- D3: the leader renews only while Home Assistant answers HTTP ------------
#
# `state.write_text(...)` gives the state file a fresh mtime, so every test
# elsewhere in this module that writes "MASTER\n" and immediately calls
# run() lands inside any realistic grace window and renews without ever
# consulting the probe -- which is exactly why none of those tests needed to
# change for D3 to land. The tests below use `_backdate` to push the file's
# mtime into the past instead, so the grace window has already expired and
# the probe actually gets consulted.


def _backdate(path: pathlib.Path, seconds: float) -> None:
    """Move `path`'s mtime into the past, so `_recently_touched` reads it as
    expired without a test having to sleep for real."""
    import os

    then = pathlib.Path(path).stat().st_mtime - seconds
    os.utime(path, (then, then))


def test_a_probe_that_answers_keeps_the_lease(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: inverting the
    discrimination so a live probe is read as dead (or vice versa) -- this
    is the run()-level companion to the two _probe_ha unit tests below.

    Asserts the actual eval call, not just the absence of an exec: a change
    that stopped renewing on the alive path entirely (skipped the eval, or
    called neither script) would still leave `ran == []` and pass this test
    if that were the only assertion.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    _backdate(state, 1000)
    client = FakeClient(holds=True)
    ran: list[list[str]] = []
    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
        probe_grace=300,
        probe=lambda _url, _timeout: True,
    )
    assert rc == 0
    assert ran == [], "still MASTER: a live probe renews, it does not promote again"
    script, keys, args = client.calls[0]
    assert script == LEASE_SCRIPT, "must actually renew, not merely do nothing"
    assert keys == [f"ha:cluster_state_sync:{NAMESPACE}:leader"]
    assert args[0] == NODE


def test_a_silent_probe_releases_the_lease_and_demotes(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: letting the lease merely
    lapse on a silent probe instead of releasing it -- the standby would
    then have to wait out the remaining TTL rather than promoting on its
    very next tick."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    _backdate(state, 1000)
    client = FakeClient(holds=True)  # irrelevant: release does not consult this
    ran: list[list[str]] = []
    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran, state),
        probe_grace=300,
        probe=lambda _url, _timeout: False,
    )
    assert rc == 0
    assert ran == [[str(tmp_path / "notify_backup.sh")]]
    script, keys, args = client.calls[0]
    assert script == RELEASE_SCRIPT, "must release, not merely let the lease lapse"
    assert keys == [f"ha:cluster_state_sync:{NAMESPACE}:leader"]
    assert args == [NODE]
    # M3: releasing must record the hold-down marker, or this node's very
    # next tick would take the lease straight back and cycle forever.
    assert (tmp_path / "release-holddown").exists()


def test_an_unreachable_valkey_during_release_fails_the_whole_tick(
    tmp_path: pathlib.Path,
) -> None:
    """The release branch deliberately has no try/except, unlike force's
    best-effort claim. Production change this catches: wrapping the release
    the same way force's claim is wrapped (swallow the failure and demote
    anyway) -- that would write BACKUP and stop Home Assistant locally while
    Valkey might still show this node holding the lease, which is worse
    than not demoting at all: this node stops serving while the peer,
    unable to reach the same unreachable Valkey, cannot legitimately take
    over either."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    _backdate(state, 1000)

    class DeadOnRelease:
        def eval(self, script: str, keys: list[str], args: list[str]) -> int:
            raise RespError("cannot reach Valkey at valkey.node-ops:6379")

        def close(self) -> None: ...

    ran: list[list[str]] = []
    rc = run(
        DeadOnRelease(),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
        probe_grace=300,
        probe=lambda _url, _timeout: False,
    )
    assert rc != 0
    assert ran == [], "must not demote on a failed release -- Valkey might still show us holding it"
    assert state.read_text() == "MASTER\n"


def test_a_silent_probe_within_grace_still_renews(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: measuring grace from
    process start (or omitting it entirely) rather than the state file's
    mtime. `docker start` returns long before Home Assistant serves HTTP,
    and a cold boot routinely outlasts the lease's own TTL -- without grace,
    a freshly-promoted node would fail its own probe while still booting,
    release the lease it just took, and flap.

    The probe here raises if called at all: within grace must not consult it,
    not merely happen to renew despite a probe result that was never checked.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")  # fresh mtime -- within any real grace window
    client = FakeClient(holds=True)
    ran: list[list[str]] = []

    def must_not_be_called(_url: str, _timeout: float) -> bool:
        raise AssertionError("must not probe within the grace window")

    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
        probe=must_not_be_called,
    )
    assert rc == 0
    assert ran == []
    assert client.calls, "must still renew"
    script, _keys, _args = client.calls[0]
    assert script == LEASE_SCRIPT


def test_a_standby_with_a_dead_ha_can_still_take_a_free_lease_and_promote(
    tmp_path: pathlib.Path,
) -> None:
    """D3's asymmetry: the probe gates renewal, never taking. A standby's
    Home Assistant is deliberately stopped, and must still be able to take a
    free lease -- taking it is what starts Home Assistant, so gating that on
    Home Assistant already answering would mean a standby could never
    promote at all.

    Production change this catches: gating the initial take on the probe
    too, not only renewal. The probe here raises if called at all, so this
    fails loudly rather than merely asserting a return value that happened
    not to matter.

    Backdated on purpose, even though `previous` is already `"BACKUP"` here:
    a fresh mtime keeps `_recently_touched` true regardless, which would ALSO
    short-circuit the mutated condition (`previous == "MASTER"` deleted) --
    masking exactly the regression this test exists to catch. Verified: with
    a fresh mtime this test still passed against that mutation; backdated,
    it does not.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    _backdate(state, 1000)

    def must_not_be_called(_url: str, _timeout: float) -> bool:
        raise AssertionError("a standby's take must never consult the probe")

    ran: list[list[str]] = []
    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran, state),
        probe=must_not_be_called,
    )
    assert rc == 0
    assert ran == [[str(tmp_path / "notify_master.sh")]]


# -- M3: the release hold-down ------------------------------------------------


def test_releasing_the_lease_says_why(tmp_path: pathlib.Path, capsys) -> None:
    """The promoter's most consequential act must not be silent.

    Releasing drops the lease and `notify_backup.sh` then stops Home
    Assistant. Until 2026-09-05 this branch printed nothing, and
    reconstructing a live flap meant inferring releases from a container exit
    code hours later. Production change this catches: removing the message, or
    releasing before emitting it.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    _backdate(state, 10_000)  # grace long expired
    client = FakeClient(holds=True)

    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([], state),
        holddown_path=tmp_path / "release-holddown",
        probe=lambda _u, _t: False,  # Home Assistant is silent
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "RELEASING the lease" in err, "the release must announce itself"
    assert "--adopt" in err, "must say how to clear the hold-down it just wrote"


def test_a_held_down_node_says_why_it_is_not_taking(tmp_path: pathlib.Path, capsys) -> None:
    """A node sitting at BACKUP and not taking looks identical to one that has
    not noticed. The hold-down is the whole explanation, so it must say so."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()

    rc = run(
        FakeClient(holds=True),  # the lease IS free and grantable
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([]),
        holddown_path=holddown,
        probe=lambda _u, _t: False,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOT attempting to take the lease" in err
    assert "--adopt" in err
    # It must NOT claim the lease is free: this branch fires on the hold-down
    # marker alone and never looks at the holder.
    assert "free lease" not in err


def test_the_holddown_message_never_claims_the_lease_is_free(
    tmp_path: pathlib.Path, capsys
) -> None:
    """It fires without checking, so it must not assert what it did not check.

    On 2026-09-07 this line printed "NOT taking the free lease" on node-b
    while node-a demonstrably held it -- confirmed by `--adopt`, which
    answered "lease held by 'node-a'". An operator read that as a
    leaderless cluster and went hunting a split-brain that did not exist.

    The branch condition is `previous != "MASTER" and _recently_touched(...)`.
    There is no holder lookup anywhere in it, so no wording derived from one
    is honest.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()

    # The peer holds the lease: this node cannot take it and has not looked.
    rc = run(
        FakeClient(holds=False),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([]),
        holddown_path=holddown,
        probe=lambda _u, _t: False,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "free" not in err, f"asserted an unchecked fact: {err!r}"


def test_a_recent_release_holds_down_the_next_take(tmp_path: pathlib.Path) -> None:
    """Without this, release (above) writes BACKUP, this node's very next
    tick sees `previous == "BACKUP"`, takes the lease it just freed, and
    promotes -- with the peer down, a perpetual cycle that re-runs the
    fileset swap on every lap. Production change this catches: dropping the
    hold-down check, or gating it on the wrong `previous` value so it also
    blocks renewal.

    The tick does NOT consult the probe here, and must not: see
    `test_the_probe_cannot_clear_a_hold_down`. This asserts the plain M3
    behaviour -- a marker inside its window refuses the take, whatever Home
    Assistant is or is not doing.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()  # fresh -- within any real hold-down window
    client = FakeClient(holds=True)  # the lease IS free and grantable
    ran: list[list[str]] = []

    probed: list[bool] = []

    def still_wedged(_url: str, _timeout: float) -> bool:
        probed.append(True)
        return False

    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran),
        holddown_path=holddown,
        release_holddown=900,
        probe=still_wedged,
    )
    assert rc == 0
    assert ran == [], "must not promote while held down"
    assert client.calls == [], "must refuse to even attempt the take"
    assert holddown.exists(), "the marker must survive to block the next tick"


def test_a_hold_down_past_its_window_allows_the_take(tmp_path: pathlib.Path) -> None:
    """The other arm. Must not become a general brake on promotion once the
    window has genuinely passed."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()
    _backdate(holddown, 1000)  # past any real hold-down window
    ran: list[list[str]] = []
    rc = run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(ran, state),
        holddown_path=holddown,
        release_holddown=900,
    )
    assert rc == 0
    assert ran == [[str(tmp_path / "notify_master.sh")]], "must take once the hold-down has expired"


def test_an_http_error_reads_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/` returns 401 without a token, and that 401 IS proof Home
    Assistant is up and answering. `HTTPError` subclasses `URLError`, so the
    discrimination is easy to invert -- checking `URLError` first would read
    a healthy, answering, unauthenticated probe as dead, and a healthy
    leader's own lease would be released out from under it.

    Production change this catches: swapping the except order, or catching
    `HTTPError` as dead.
    """
    import urllib.error
    import urllib.request

    def raise_401(*_a: object, **_k: object) -> None:
        raise urllib.error.HTTPError("http://x/api/", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", raise_401)
    assert cluster_promoter._probe_ha("http://127.0.0.1:8123/", 5.0) is True


def test_a_connection_refusal_reads_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other arm: a connection that never got a response at all -- not
    merely an error response -- is the only thing this probe treats as
    dead."""
    import urllib.error
    import urllib.request

    def raise_refused(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request, "urlopen", raise_refused)
    assert cluster_promoter._probe_ha("http://127.0.0.1:8123/", 5.0) is False


def test_a_probe_timeout_reads_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    def raise_timeout(*_a: object, **_k: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", raise_timeout)
    assert cluster_promoter._probe_ha("http://127.0.0.1:8123/", 5.0) is False


# -- main()'s own configuration refusals --------------------------------------


def _argv(tmp_path: pathlib.Path, **overrides: str) -> list[str]:
    args = {
        "--redis": "127.0.0.1:1",
        "--namespace": NAMESPACE,
        "--node-id": NODE,
    }
    args.update(overrides)
    argv: list[str] = []
    for flag, value in args.items():
        argv += [flag, value]
    return [
        *argv,
        "--state-file",
        str(tmp_path / "vrrp-state"),
        "--install-dir",
        str(tmp_path),
        "--force-file",
        str(tmp_path / "force-master"),
    ]


def test_main_refuses_a_blank_node_id(tmp_path: pathlib.Path) -> None:
    """An unset generator variable interpolates as `""`. Two nodes each
    presenting that would both renew the *same* lease and both believe they
    lead -- the identity collision that cost this project a day. Checked
    before any socket is opened, so this never touches the network."""
    assert main(_argv(tmp_path, **{"--node-id": "   "})) != 0


def test_main_refuses_a_blank_namespace(tmp_path: pathlib.Path) -> None:
    assert main(_argv(tmp_path, **{"--namespace": ""})) != 0


def test_main_no_probe_renews_unconditionally_end_to_end(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-probe driven through argv and main(), not handed to run()
    directly: this is the only test that proves main() actually threads the
    flag through rather than parsing it and dropping it.

    The state file is backdated well past any real grace window, so main()
    must actually reach the probe-or-not decision. If the wiring were
    broken (`probe=` never passed through), run() would fall back to the
    real `_probe_ha`, which would attempt a real socket -- blocked by this
    test harness by design -- and _probe_ha's own `except Exception: return
    False` (H1) would read that block as "dead", releasing and demoting:
    the opposite of what --no-probe promises. That is what this test would
    actually observe as its failure, not a hang or a crash.
    """
    import custom_components.cluster_state_sync.scripts.cluster_promoter as promoter_mod

    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    _backdate(state, 1000)

    client = FakeClient(holds=True)
    monkeypatch.setattr(promoter_mod.ValkeyClient, "connect", staticmethod(lambda **_kw: client))

    rc = main(_argv(tmp_path, **{"--node-id": NODE}) + ["--no-probe"])

    assert rc == 0, "a broken --no-probe wiring would hit the socket block and demote instead"
    script, _keys, _args = client.calls[0]
    assert script == LEASE_SCRIPT, "must renew, never release, under --no-probe"


# -- Bounding a tick: neither Valkey nor a notify script may hang forever ----


def test_main_gives_the_valkey_connection_its_own_short_timeout(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production change this catches: dropping the explicit `timeout=` on
    `ValkeyClient.connect`, falling back to `resp.SOCKET_TIMEOUT` (30s) --
    a legitimate default for the pull's one-minute batch job, but one that
    lets AUTH, SELECT and EVAL each block up to 30s against a Valkey that
    accepts the TCP handshake and then never answers: roughly 90s total,
    most of a lease's TTL, burned before anything is even logged as failed.
    """
    import custom_components.cluster_state_sync.scripts.cluster_promoter as promoter_mod

    captured: dict[str, object] = {}

    class _StubClient:
        def eval(self, *_a: object, **_k: object) -> int:
            return 1

        def close(self) -> None: ...

    def _fake_connect(**kwargs: object) -> _StubClient:
        captured.update(kwargs)
        return _StubClient()

    monkeypatch.setattr(promoter_mod.ValkeyClient, "connect", staticmethod(_fake_connect))
    # main() promotes on this stub and tries to exec notify_master.sh -- give
    # it one so the tick completes cleanly; irrelevant to what this test
    # actually checks, but a missing script would print a stray error.
    script = tmp_path / "notify_master.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)

    main(_argv(tmp_path))

    assert captured.get("timeout") == promoter_mod.CONNECT_TIMEOUT_SECONDS
    assert promoter_mod.CONNECT_TIMEOUT_SECONDS < 30, "must stay well under resp.SOCKET_TIMEOUT"


def test_a_hung_notify_script_is_treated_as_failed_rather_than_blocking_forever(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Production change this catches: dropping `timeout=` from the
    `subprocess.run` call inside `_run_notify_script`. Without it, a hung
    notify script (a stuck Docker daemon on `docker start`, say) never
    returns; systemd will not start the NEXT tick of a still-running
    oneshot; the lease laps at its TTL while this node sits mid-promotion,
    unable to demote -- two masters, indefinitely, with this one merely
    stuck rather than failed.
    """
    import custom_components.cluster_state_sync.scripts.cluster_promoter as promoter_mod

    monkeypatch.setattr(promoter_mod, "NOTIFY_TIMEOUT_SECONDS", 0.2)

    rc = promoter_mod._run_notify_script(["sleep", "5"])

    assert rc != 0
    assert "did not exit within" in capsys.readouterr().err


# -- The promoter's dependency chain is stdlib-only, end to end --------------


_PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "custom_components" / "cluster_state_sync"


@pytest.mark.parametrize(
    "relpath, allowed_siblings",
    [
        pytest.param("scripts/cluster_promoter.py", {"lease", "resp"}, id="cluster_promoter"),
        pytest.param("scripts/resp.py", set(), id="resp"),
        pytest.param("lease.py", set(), id="lease"),
    ],
)
def test_the_promoters_dependency_chain_imports_nothing_but_stdlib_and_siblings(
    relpath: str, allowed_siblings: set[str]
) -> None:
    """AR-0040's shape, found writing the fix for its descendant: this module
    used to import `ValkeyClient` from `fileset_pull.py` for the sake of one
    function, and `fileset_pull.py` imports `crypto.py`, which imports the
    third-party `cryptography` package. The promoter needs none of that, but
    dragged it in transitively anyway -- on a host without
    `python3-cryptography` installed, the systemd timer failed on every tick
    with nothing but a log line to show for it.

    `resp.py` exists so this chain is stdlib-only end to end, checked here
    rather than trusted, so a later convenient import (of `fileset_pull`, or
    of any other third-party-touching sibling) cannot reopen it silently.
    Both the package-relative arm (`from ..lease import ...`) and the flat,
    standalone-script arm (`from lease import ...`) are checked identically:
    `ast.ImportFrom.module` carries the same bare name either way, only
    `.level` differs.
    """
    source = (_PACKAGE_DIR / relpath).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    disallowed = imported - set(sys.stdlib_module_names) - allowed_siblings
    assert not disallowed, disallowed


# -- adopt(): arming the timer must not itself be a promotion ------------------


class ObservingClient:
    """Answers GET, and records every eval so a test can prove none happened."""

    def __init__(self, holder: str | None) -> None:
        self.holder = holder
        self.gets: list[str] = []
        self.evals: list[tuple[str, list[str], list[str]]] = []

    def get(self, key: str) -> str | None:
        self.gets.append(key)
        return self.holder

    def eval(self, script: str, keys: list[str], args: list[str]) -> int:
        self.evals.append((script, keys, args))
        return 1

    def close(self) -> None: ...


def test_adopt_records_master_when_this_node_already_holds_the_lease(
    tmp_path: pathlib.Path,
) -> None:
    """The case that makes install.sh safe on a live primary.

    Without a seeded state file the first tick reads previous == "" , calls the
    status quo a transition into MASTER, and runs notify_master.sh -- a fileset
    swap and a device pre-flight rewriting .storage under a Home Assistant that
    never stopped.
    """
    state = tmp_path / "run" / "vrrp-state"
    client = ObservingClient(holder=NODE)

    assert adopt(client, namespace=NAMESPACE, node_id=NODE, state_path=state) == 0
    assert state.read_text().strip() == "MASTER"


def test_adopt_records_backup_when_the_peer_holds_the_lease(
    tmp_path: pathlib.Path,
) -> None:
    state = tmp_path / "vrrp-state"
    client = ObservingClient(holder="tiger2-def456")

    assert adopt(client, namespace=NAMESPACE, node_id=NODE, state_path=state) == 0
    assert state.read_text().strip() == "BACKUP"


def test_adopt_records_backup_when_nobody_holds_the_lease(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: treating an unheld lease as
    "then it must be mine".

    read_state() already refuses to default to MASTER for exactly this reason —
    a node that claims leadership because it could not establish otherwise is
    the split brain. Seeding BACKUP costs one ordinary tick, which then takes
    the lease through the tested path.
    """
    state = tmp_path / "vrrp-state"
    client = ObservingClient(holder=None)

    assert adopt(client, namespace=NAMESPACE, node_id=NODE, state_path=state) == 0
    assert state.read_text().strip() == "BACKUP"


def test_adopt_never_touches_the_lease(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: implementing adopt with
    LEASE_SCRIPT instead of GET.

    Taking the lease on a standby IS the promotion this exists to avoid, and
    even renewing it on the leader would extend a TTL on the strength of an
    install step rather than a health check.
    """
    for holder in (NODE, "someone-else", None):
        client = ObservingClient(holder=holder)
        adopt(
            client,
            namespace=NAMESPACE,
            node_id=NODE,
            state_path=tmp_path / f"state-{holder}",
        )
        assert client.evals == [], f"adopt evaluated a script with holder={holder!r}"
        assert client.gets == [f"ha:cluster_state_sync:{NAMESPACE}:leader"]


def test_adopt_runs_no_notify_script(tmp_path: pathlib.Path) -> None:
    """adopt() has no runner argument at all, so there is nothing to call —
    this asserts the signature stays that way."""
    import inspect

    assert "runner" not in inspect.signature(adopt).parameters
    assert "install_dir" not in inspect.signature(adopt).parameters


def test_adopt_reports_failure_rather_than_leaving_no_state(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: letting a ValkeyError escape.

    install.sh gates enabling the timer on this exiting 0. A traceback that
    left no state file, with the installer carrying on regardless, would arm a
    timer whose first tick promotes — the precise outcome adopt exists to
    prevent.
    """
    state = tmp_path / "vrrp-state"

    class DeadClient:
        def get(self, key: str) -> str | None:
            raise RespError("connection refused")

        def close(self) -> None: ...

    assert adopt(DeadClient(), namespace=NAMESPACE, node_id=NODE, state_path=state) == 1
    assert not state.exists()


# -- the maintenance hold (owner ask, 2026-09-03) ------------------------------


class HoldClient:
    """Records which script was evaluated, so a test can prove which path ran.

    Heartbeats are excluded from `scripts`: they fire on every tick regardless
    of the decision, and these tests exist to prove WHICH lease path ran. A
    diagnostic write is not a lease path.
    """

    def __init__(self, holder: str | None) -> None:
        self.holder = holder
        self.scripts: list[str] = []
        self.heartbeats: list[str] = []

    def eval(self, script: str, keys: list[str], args: list[str]) -> int:
        if "promoters:" in (keys[0] if keys else ""):
            self.heartbeats.append(script)
            return 1
        self.scripts.append(script)
        if script == RENEW_ONLY_SCRIPT:
            return 1 if self.holder == args[0] else 0
        if script == LEASE_SCRIPT:
            if self.holder is None:
                self.holder = args[0]
            return 1 if self.holder == args[0] else 0
        return 1

    def close(self) -> None: ...


def _hold(tmp_path: pathlib.Path, text: str = "") -> pathlib.Path:
    f = tmp_path / ".cluster_sync_hold"
    f.write_text(text)
    return f


def test_a_held_leader_renews_and_never_demotes(tmp_path: pathlib.Path) -> None:
    """The point of the hold: a planned restart must not cost leadership.

    Production change that would make this fail: letting the D3 probe run under
    a hold. The leader's Home Assistant is deliberately down during planned
    work, so the probe would fail, release, and demote -- exactly the failover
    the operator set the hold to prevent.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    recorded: list[list[str]] = []
    client = HoldClient(holder=NODE)

    rc = cluster_promoter.run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded),
        hold_path=_hold(tmp_path),
        probe=lambda _u, _t: False,  # HA is down: without the hold this demotes
        probe_grace=0,
    )
    assert rc == 0
    assert recorded == [], "a held node must run no notify script"
    assert client.scripts == [RENEW_ONLY_SCRIPT]
    assert state.read_text().strip() == "MASTER", "still the leader"


def test_a_held_standby_refuses_a_free_lease(tmp_path: pathlib.Path) -> None:
    """During planned work a free lease usually means the peer is mid-restart,
    not dead. Taking it is the failover the hold exists to stop."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    recorded: list[list[str]] = []
    client = HoldClient(holder=None)  # nobody holds it

    rc = cluster_promoter.run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded),
        hold_path=_hold(tmp_path),
    )
    assert rc == 0
    assert recorded == [], "a held standby must not promote"
    assert client.scripts == [RENEW_ONLY_SCRIPT], "renew-only, never LEASE_SCRIPT"
    assert client.holder is None, "the free lease was left alone"


def test_force_master_still_beats_the_hold(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: checking the hold before
    force-master, or letting the hold veto it.

    The hold is the operator saying "not yet". force-master is the operator
    saying "now, I have checked the peer is gone". An override a flag file
    could countermand would not be an override.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    recorded: list[list[str]] = []
    force = tmp_path / "force-master"
    force.touch()

    rc = cluster_promoter.run(
        HoldClient(holder="someone-else"),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=force,
        runner=_runner(recorded, state),
        hold_path=_hold(tmp_path),
    )
    assert rc == 0
    assert [c[0].rsplit("/", 1)[-1] for c in recorded] == ["notify_master.sh"]


def test_no_hold_file_means_no_hold(tmp_path: pathlib.Path) -> None:
    """Fails safe toward *not* holding: a hold that switched itself on would be
    a cluster that had silently stopped failing over."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    recorded: list[list[str]] = []

    cluster_promoter.run(
        HoldClient(holder=None),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        hold_path=tmp_path / "absent",
    )
    assert [c[0].rsplit("/", 1)[-1] for c in recorded] == ["notify_master.sh"]


def test_both_import_branches_expose_the_same_names() -> None:
    """Production change that would make this fail: adding a name to the
    package-relative import and not to the standalone one.

    Only the standalone branch runs on the host, and it is the branch no test
    exercises by importing -- so a name missing there is a NameError that
    appears for the first time on a real node, mid-promotion. This project has
    already shipped exactly that shape once (`cryptography` via fileset_pull).
    """
    import ast
    import pathlib as _p

    src = (
        _p.Path(__file__).parent.parent
        / "custom_components/cluster_state_sync/scripts/cluster_promoter.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    rel = [
        {a.name for a in node.names}
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("lease")
    ]
    assert len(rel) == 2, f"expected two lease imports, found {len(rel)}"
    assert rel[0] == rel[1], f"import branches differ: {rel[0] ^ rel[1]}"


# -- the two stalls found by the live failover test, 2026-09-04 ---------------


def test_adopt_clears_a_hold_down(tmp_path: pathlib.Path) -> None:
    """Adopt is the operator's escape hatch, and now the only one.

    `run()` deliberately refuses to let the D3 probe open a hold-down, because
    a cold standby's Home Assistant is stopped on purpose and its probe can
    never answer (see `test_the_probe_cannot_clear_a_hold_down`). That leaves
    time, or a human. Adopting re-reads the cluster's real state from Valkey on
    an explicit operator action, so a marker left by an earlier release is
    exactly the stale thing it should clear.

    Production change this catches: dropping the unlink from `adopt`, which
    would strand a standby for the full window with no way to release it short
    of deleting the file by hand -- which is how this was recovered before.
    """
    state = tmp_path / "vrrp-state"
    holddown = cluster_promoter._holddown_path(state)
    holddown.parent.mkdir(parents=True, exist_ok=True)
    holddown.touch()
    assert holddown.exists()

    rc = adopt(
        ObservingClient(holder="tiger2-def456"),
        namespace=NAMESPACE,
        node_id=NODE,
        state_path=state,
    )
    assert rc == 0
    assert state.read_text().strip() == "BACKUP"
    assert not holddown.exists(), "adopt must clear a stale hold-down marker"


def test_adopt_survives_having_no_hold_down_to_clear(
    tmp_path: pathlib.Path,
) -> None:
    """The ordinary case -- a first install has no marker. Unlinking a file
    that was never there must not turn a successful adopt into a failed one,
    which is why the unlink is best-effort."""
    state = tmp_path / "vrrp-state"
    assert not cluster_promoter._holddown_path(state).exists()

    rc = adopt(
        ObservingClient(holder=NODE),
        namespace=NAMESPACE,
        node_id=NODE,
        state_path=state,
    )
    assert rc == 0
    assert state.read_text().strip() == "MASTER"


def test_adopt_does_not_grant_a_probe_grace(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: writing the state file with
    a current mtime.

    D3's grace is measured from that mtime — a freshly promoted node renews
    without probing while Home Assistant boots. Adopt records a state the node
    has been in, possibly for weeks. A fresh mtime told the promoter this node
    had just promoted, so it renewed unconditionally for five minutes after
    Home Assistant stopped: a failover measured at 12 seconds sat at over two
    minutes when `install.sh` had run shortly before.
    """
    import time as _time

    state = tmp_path / "vrrp-state"
    adopt(ObservingClient(holder=NODE), namespace=NAMESPACE, node_id=NODE, state_path=state)

    age = _time.time() - state.stat().st_mtime
    assert age > 3600, f"adopt granted a {age:.0f}s grace window"


def test_the_probe_cannot_clear_a_hold_down(tmp_path: pathlib.Path) -> None:
    """The inverse of a test that used to live here, and the reason it went.

    An earlier version cleared the hold-down as soon as the probe answered,
    so that a healthy node was not kept out. It is unreachable exactly when it
    matters: a cold standby's Home Assistant is *deliberately stopped* by
    `notify_backup.sh`, so its probe can never answer, so the hatch never opens
    for the node that most needs it. Measured on 2026-09-04 -- node-b sat
    BACKUP for 901 seconds with its peer down, then promoted unaided the
    instant the window expired.

    It also broke D3's invariant: *the probe gates renewal, never taking*.

    So even a probe that answers must not open a live hold-down. Production
    change this catches: re-adding a probe call to the hold-down branch.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()  # fresh -- inside the window
    recorded: list[list[str]] = []
    probed: list[bool] = []

    def answering(_url: str, _timeout: float) -> bool:
        probed.append(True)
        return True  # Home Assistant is emphatically alive

    rc = cluster_promoter.run(
        FakeClient(holds=True),  # the lease is free and grantable
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        holddown_path=holddown,
        probe=answering,
    )
    assert rc == 0
    assert recorded == [], "a healthy probe must NOT open a live hold-down"
    assert holddown.exists(), "the marker must survive its full window"


def test_a_cold_standby_promotes_when_the_window_expires(
    tmp_path: pathlib.Path,
) -> None:
    """The scenario the old probe-escape was reaching for, done with time.

    A cold standby has no Home Assistant running -- that is the design, not a
    fault -- so its probe fails on every tick. It must still promote once the
    hold-down window passes, or a standby that once released can never take
    over again.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()
    _backdate(holddown, cluster_promoter.DEFAULT_RELEASE_HOLDDOWN_SECONDS + 30)
    recorded: list[list[str]] = []

    rc = cluster_promoter.run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        holddown_path=holddown,
        probe=lambda _u, _t: False,  # stopped on purpose, never answers
    )
    assert rc == 0
    assert [c[0].rsplit("/", 1)[-1] for c in recorded] == ["notify_master.sh"]
    assert not holddown.exists(), "a spent marker must be cleared"


def test_an_unhealthy_probe_still_honours_the_holddown(
    tmp_path: pathlib.Path,
) -> None:
    """The control. A node whose Home Assistant is still silent must stay out —
    that is the cycling the hold-down was written to stop, and each promotion
    also re-runs the fileset swap."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    holddown = tmp_path / "release-holddown"
    holddown.touch()
    recorded: list[list[str]] = []

    cluster_promoter.run(
        FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        holddown_path=holddown,
        probe=lambda _u, _t: False,  # still wedged
    )
    assert recorded == [], "a wedged node must not promote"
    assert holddown.exists(), "the hold-down must survive to block the next tick"


# -- operator-requested handover -------------------------------------------


def test_a_handover_request_releases_the_lease(tmp_path: pathlib.Path) -> None:
    """ "Fail over now", as an operator action rather than an outage.

    Before this, moving the cluster deliberately meant stopping Home Assistant
    on the leader -- which caused a real outage on 2026-09-06, because a
    stopped leader is a leader whose container `notify_backup.sh` will not
    restart -- or writing force-master on the peer by hand.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    req = tmp_path / ".cluster_sync_handover_request"
    req.write_text("requested from the dashboard\n")
    recorded: list[list[str]] = []
    client = FakeClient(holds=True)

    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        holddown_path=tmp_path / "release-holddown",
        handover_path=req,
        probe=lambda _u, _t: True,  # Home Assistant is perfectly healthy
    )
    assert rc == 0
    assert [c[0].rsplit("/", 1)[-1] for c in recorded] == ["notify_backup.sh"]
    assert not req.exists(), "the request must be consumed, not left to re-fire"


def test_a_handover_does_not_require_an_unhealthy_node(tmp_path: pathlib.Path) -> None:
    """It is the one release that happens while everything is working.

    Production change this catches: putting the handover branch after the D3
    probe check, which would make a deliberate handover depend on Home
    Assistant being unwell.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    req = tmp_path / ".cluster_sync_handover_request"
    req.touch()
    recorded: list[list[str]] = []

    run(
        client=FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        holddown_path=tmp_path / "release-holddown",
        handover_path=req,
        probe=lambda _u, _t: True,
    )
    assert [c[0].rsplit("/", 1)[-1] for c in recorded] == ["notify_backup.sh"]


def test_a_handover_writes_a_holddown(tmp_path: pathlib.Path) -> None:
    """Without it this node takes back on its very next tick the lease it was
    just asked to give away."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    req = tmp_path / ".cluster_sync_handover_request"
    req.touch()
    holddown = tmp_path / "release-holddown"

    run(
        client=FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([], state),
        holddown_path=holddown,
        handover_path=req,
        probe=lambda _u, _t: True,
    )
    assert holddown.exists(), "a handover must hold this node down like any release"


def test_a_handover_request_on_a_follower_is_ignored(tmp_path: pathlib.Path) -> None:
    """A follower has nothing to hand over. Acting on it would be a standby
    releasing a lease it does not hold."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    req = tmp_path / ".cluster_sync_handover_request"
    req.touch()
    recorded: list[list[str]] = []

    run(
        client=FakeClient(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state),
        holddown_path=tmp_path / "release-holddown",
        handover_path=req,
        probe=lambda _u, _t: False,
    )
    assert "notify_backup.sh" not in [c[0].rsplit("/", 1)[-1] for c in recorded]


# --- adaptive probe grace (2026-09-07) -------------------------------------
#
# The flat 600s grace applied the slowest-cold-boot worst case to every
# failure, so a wedged Home Assistant cost ten minutes before the peer could
# promote -- and the same 600s is why an HA-only failure misses the 2.5-minute
# budget and why radios move late (ADR-001, ADR-009). One setting, three
# symptoms. The base drops to 120s; the full 600s is granted only while the
# container is demonstrably coming back.


def _inspector(**state):
    return lambda: dict(state)


def test_a_restarting_container_is_returning() -> None:
    """The strongest single signal, and unambiguous."""
    returning, reason = cluster_promoter.container_is_returning(_inspector(Restarting=True))
    assert returning is True
    assert "restarting" in reason


def test_exit_100_is_a_deliberate_restart_not_a_crash() -> None:
    """Home Assistant asks to be restarted with exit 100 after a config change
    or an upgrade. Demoting on that would fail over on every reconfiguration."""
    returning, reason = cluster_promoter.container_is_returning(
        _inspector(Restarting=False, Running=False, ExitCode=100)
    )
    assert returning is True
    assert "100" in reason


def test_a_container_that_just_started_is_still_booting() -> None:
    """Measured cold boots on this fleet: 24.0s and 62.9s to `initialized`.
    A probe failing 30s after start means booting, not wedged."""
    returning, reason = cluster_promoter.container_is_returning(
        _inspector(Restarting=False, Running=True, StartedAtEpoch=1000.0),
        now=1030.0,
    )
    assert returning is True
    assert "booting" in reason


def test_a_wedged_container_is_NOT_returning() -> None:
    """The case D3 exists for: running, old, and answering nothing.

    This is the whole reason the adaptive path may not weaken D3 -- a Home
    Assistant that is up and useless must still lose the lease.
    """
    returning, _ = cluster_promoter.container_is_returning(
        _inspector(Restarting=False, Running=True, StartedAtEpoch=1000.0),
        now=1000.0 + 3600,
    )
    assert returning is False


def test_no_docker_means_no_extension() -> None:
    """Bare metal, Core, or a missing CLI falls back to the flat behaviour.

    An inspector that cannot answer must never extend, because a node that
    silently refuses to demote is worse than one that demotes early.
    """
    assert cluster_promoter.container_is_returning(lambda: None) == (False, "")


def test_a_crash_loop_does_NOT_extend_forever(tmp_path: pathlib.Path) -> None:
    """🚨 The cap. A crash loop reports `Restarting` on every tick, so without
    a ceiling the adaptive path would build the one mode TODO forbids: a
    failover that silently never happens.

    Past `probe_grace` the container's opinion stops counting.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    # Age the state file well past the ceiling.
    old = time.time() - 10_000
    os.utime(state, (old, old))
    assert (
        cluster_promoter._grace_extended(
            state_path=state,
            base_grace=120.0,
            probe_grace=600.0,
            inspect_container=_inspector(Restarting=True),  # loops forever
            reason_path=None,
        )
        is False
    ), "a crash loop extended past the cap — failover would never happen"


def test_within_the_cap_a_restarting_container_does_extend(
    tmp_path: pathlib.Path,
) -> None:
    """The other half: between base and cap, a returning container is spared."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    recent = time.time() - 200  # past the 120s base, inside the 600s cap
    os.utime(state, (recent, recent))
    reason = tmp_path / "grace-reason"
    assert (
        cluster_promoter._grace_extended(
            state_path=state,
            base_grace=120.0,
            probe_grace=600.0,
            inspect_container=_inspector(Restarting=True),
            reason_path=reason,
        )
        is True
    )
    assert "restarting" in reason.read_text(), "the reason must reach the operator surface"


def test_the_reason_file_is_cleared_when_no_longer_extending(
    tmp_path: pathlib.Path,
) -> None:
    """A stale explanation is worse than none — it would claim the promoter is
    still waiting for a boot that finished."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    recent = time.time() - 200
    os.utime(state, (recent, recent))
    reason = tmp_path / "grace-reason"
    reason.write_text("container is restarting\n")
    cluster_promoter._grace_extended(
        state_path=state,
        base_grace=120.0,
        probe_grace=600.0,
        inspect_container=_inspector(Restarting=False, Running=True),
        reason_path=reason,
    )
    assert not reason.exists(), "a stale grace reason was left behind"


def test_a_broken_inspector_never_blocks_demotion(tmp_path: pathlib.Path) -> None:
    """If asking Docker raises, demote. The failure this branch defends against
    is a node that will not give up the lease."""
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    recent = time.time() - 200
    os.utime(state, (recent, recent))

    def boom():
        raise RuntimeError("docker: command not found")

    assert (
        cluster_promoter._grace_extended(
            state_path=state,
            base_grace=120.0,
            probe_grace=600.0,
            inspect_container=boom,
            reason_path=None,
        )
        is False
    )


# --- the promoter heartbeat (ADR-009 residual gap, 2026-09-08) --------------
#
# The custody gap that survives: the promoter stops while the machine keeps
# running. Nothing releases the USB claims, and no server reaps a live client.
# The integration cannot see it -- on a cold standby Home Assistant is
# deliberately stopped, so `cluster_members` reads 1 on a HEALTHY cluster and
# would read 1 on a broken one too. The promoter is the only thing alive there,
# so it is the only thing that can report.


def test_the_heartbeat_fires_even_when_the_node_is_held(
    tmp_path: pathlib.Path,
) -> None:
    """A maintenance hold is exactly when an operator is watching.

    Every branch in `run()` can return early. A heartbeat that only fired on
    the happy path would go quiet for the very states worth seeing -- held,
    holding down, probe failing -- and a quiet heartbeat is how this signal
    says "that promoter is gone".
    """
    state = tmp_path / "vrrp-state"
    state.write_text("MASTER\n")
    hold = tmp_path / "hold"
    hold.write_text("planned work\n")
    client = HoldClient(holder=NODE)

    rc = run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([]),
        hold_path=hold,
    )
    assert rc == 0
    assert client.heartbeats, "a held node published no heartbeat — it looks dead"


def test_the_heartbeat_fires_on_a_follower(tmp_path: pathlib.Path) -> None:
    """The follower's heartbeat is the whole point.

    A cold standby's Home Assistant is stopped, so nothing else on that machine
    can say it is alive and able to promote.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    client = FakeClient(holds=False)
    run(
        client,
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner([]),
        probe=lambda _u, _t: False,
    )
    assert client.heartbeats, "a follower published no heartbeat"
    key = client.heartbeats[0][1][0]
    assert key == f"ha:cluster_state_sync:{NAMESPACE}:promoters:{NODE}", key


def test_a_failed_heartbeat_never_changes_a_decision(tmp_path: pathlib.Path) -> None:
    """🚨 Observability may not alter behaviour.

    A promoter that skipped a promotion because it could not write a diagnostic
    would be a far worse bug than a missing diagnostic.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")

    class Exploding(FakeClient):
        def eval(self, script, keys, args):
            if "promoters:" in (keys[0] if keys else ""):
                raise RespError("heartbeat exploded")
            return super().eval(script, keys, args)

    recorded: list[list[str]] = []
    rc = run(
        Exploding(holds=True),
        namespace=NAMESPACE,
        node_id=NODE,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=_runner(recorded, state_path=state),
    )
    assert rc == 0, "a broken heartbeat failed the whole tick"
    assert recorded, "the promotion did not happen because a diagnostic failed"


# -- an automatic hold that outlives its deadline ---------------------------


def test_a_hold_with_no_expiry_never_expires(tmp_path) -> None:
    """🚨 An operator's hold is indefinite, and must stay that way.

    Guessing that a hold has lapsed would resume failover during exactly the
    maintenance somebody asked us to sit out.
    """
    hold = tmp_path / "hold"
    hold.write_text("somebody is under the floor\n", encoding="utf-8")
    expired, _ = cluster_promoter._hold_is_expired(hold)
    assert expired is False


def test_an_automatic_hold_lapses_once_its_deadline_passes(tmp_path) -> None:
    """The expiry lives HERE because nothing else is still running.

    If Home Assistant never comes back, the integration that set the hold is
    gone and cannot clear it. The promoter is the only participant left, so it
    is the only place a deadline can be enforced.
    """
    from datetime import UTC, datetime, timedelta

    hold = tmp_path / "hold"
    past = datetime.now(UTC) - timedelta(seconds=1)
    hold.write_text(
        f"cluster_state_sync:auto-hold\nexpires: {past.isoformat()}\n", encoding="utf-8"
    )
    expired, stamp = cluster_promoter._hold_is_expired(hold)
    assert expired is True
    assert stamp


def test_an_automatic_hold_inside_its_deadline_still_holds(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    hold = tmp_path / "hold"
    future = datetime.now(UTC) + timedelta(seconds=300)
    hold.write_text(
        f"cluster_state_sync:auto-hold\nexpires: {future.isoformat()}\n", encoding="utf-8"
    )
    expired, _ = cluster_promoter._hold_is_expired(hold)
    assert expired is False


def test_an_unparseable_deadline_is_treated_as_no_deadline(tmp_path) -> None:
    """Fail towards holding, not towards resuming.

    A malformed deadline means we do not know when it lapses. Resuming failover
    on a guess is the one outcome worse than holding slightly too long.
    """
    hold = tmp_path / "hold"
    hold.write_text("cluster_state_sync:auto-hold\nexpires: banana\n", encoding="utf-8")
    expired, _ = cluster_promoter._hold_is_expired(hold)
    assert expired is False
