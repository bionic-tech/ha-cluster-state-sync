"""The maintenance hold: planned downtime that must not become a failover.

Restarting Home Assistant on the leader was, until this existed, a permanent
failover. Three separate and individually-correct mechanisms cooperated to
make it one: `_on_stop` hands the lease back so the peer need not wait out the
TTL; the standby's `_from_lease` calls take-or-renew, so its own Home Assistant
claims the freed lease within seconds; and the leader's promoter then fails its
D3 probe and demotes, which is a `docker stop` that `unless-stopped` will not
undo. The service survived on the standby, which is failover working — but the
primary stayed down and the cluster had moved, which is not what Restart means.

The hold suspends all four behaviours at once (the fourth being the standby's
promoter, which must also refuse a free lease). Any one left armed leaves a
hole big enough to lose the cluster through, which is why they are enumerated
in one place rather than each guarding itself.

**Where the flag lives, and why there.** It is a file in Home Assistant's own
config directory. That is the one directory both halves of this system can
see: the integration reads it at `/config/.cluster_sync_hold` from inside the
container, and the host-side promoter reads the very same bytes at
`<ha_config_path>/.cluster_sync_hold` — the translation `bundle._host_path`
already performs for the TLS CA. A flag under `/run` would be invisible to the
integration, and the integration is where the most important of the four
behaviours lives.

**It fails safe toward *not* holding.** An unreadable flag, a permissions
error, a path that does not exist: all read as "no hold". A hold that switches
itself on by accident is a cluster that has quietly stopped failing over,
which is strictly worse than the bug it was written to fix — so nothing but
the file plainly being there turns it on.

**It is deliberately loud.** `binary_sensor.*_maintenance_hold`, a repair
issue while it is set, and a line in every promoter tick. A forgotten hold is
indistinguishable from a healthy cluster right up until the moment it needed
to fail over and did not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import pathlib

#: The filename, relative to Home Assistant's config directory. Dot-prefixed so
#: it sorts out of the way, and matching `.cluster_sync_staged`'s convention.
HOLD_FILENAME = ".cluster_sync_hold"


def hold_path(config_dir: str) -> pathlib.Path:
    """Where the flag lives, given whichever view of the config dir you have.

    The container passes `/config`; the host passes the real path. Both land on
    the same file, which is the property the whole design rests on.
    """
    return pathlib.Path(config_dir) / HOLD_FILENAME


def is_held(config_dir: str) -> bool:
    """Is maintenance mode on?

    Never raises. See the module docstring: every failure mode answers "no",
    because a hold that turns itself on is a cluster that has silently stopped
    failing over.
    """
    try:
        return hold_path(config_dir).is_file()
    except OSError:
        return False


def read_hold(config_dir: str) -> tuple[bool, str]:
    """Both answers in one filesystem trip, for callers on the event loop.

    `is_held` and `hold_reason` separately are two stats and an open. Home
    Assistant's loop protection flags the open -- correctly: this is blocking
    I/O, and every caller inside the integration runs on the loop. They must
    hand this to an executor, and handing over one call rather than two keeps
    the hop cheap enough to do on every poll.
    """
    return is_held(config_dir), hold_reason(config_dir)


def hold_reason(config_dir: str) -> str:
    """Whatever the operator wrote in the flag file, for the log and the sensor.

    Empty is fine and expected — `touch` is the documented way to set this, and
    an empty file is a perfectly good hold. The reason exists so a hold found
    days later can explain itself, not as a requirement.
    """
    try:
        return hold_path(config_dir).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


#: What a hold written from the UI records as its reason. The promoter prints
#: the reason on every tick, so it wants to say where the hold came from --
#: an operator reading `journalctl` should not have to guess whether a hold was
#: set at the console or by someone tapping a switch.
UI_HOLD_REASON = "set from the Home Assistant dashboard"


def set_hold(config_dir: str, reason: str = UI_HOLD_REASON) -> None:
    """Raise the maintenance hold. Raises OSError if it cannot be written.

    Unlike the readers above, this does NOT fail soft. A hold that silently
    fails to apply is worse than no hold at all: the operator believes failover
    is suspended, restarts Home Assistant, and the peer takes over anyway --
    which is the exact accident this flag exists to prevent. The caller
    surfaces the failure.

    Written whole, then renamed, so a promoter tick that reads the file while
    it is being written cannot see a partial reason.
    """
    target = hold_path(config_dir)
    tmp = target.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(f"{reason}\n", encoding="utf-8")
    tmp.replace(target)


def clear_hold(config_dir: str) -> None:
    """Drop the maintenance hold. Idempotent; raises OSError on real failure.

    An absent file is success, not an error: `cluster-hold.sh off` may have run
    on the host, and two paths to the same outcome must not disagree about it.
    """
    try:
        hold_path(config_dir).unlink()
    except FileNotFoundError:
        return


#: Written into a hold this integration set itself, so it can be told apart
#: from one a person set. The promoter looks for the `expires:` line; this
#: marker is what stops the integration ever clearing somebody else's hold.
AUTO_HOLD_MARKER = "cluster_state_sync:auto-hold"

#: How long an automatic hold covers a restart before lapsing on its own.
#:
#: 🚨 The expiry is enforced by the PROMOTER, not here. If Home Assistant never
#: comes back, this integration is gone and nothing inside it could ever clear
#: the hold — a cluster would sit held forever on a dead node, which is the
#: exact failure the hold exists to avoid causing.
#:
#: 300 seconds, and the number is not arbitrary:
#:
#: * the slowest Home Assistant restart measured on the reference pair was 65s,
#:   so this covers it nearly five times over;
#: * the promoter's probe grace is 600s and is anchored to the state file's
#:   mtime, which a hold does NOT touch — so the grace clock runs *underneath*
#:   this hold rather than being reset by it. A crash during an automatic hold
#:   therefore still demotes at ~600s, exactly as it would without one.
#:
#: That second point is the whole design. It was verified by reading the
#: promoter rather than assumed, and the assumption had been the opposite.
AUTO_HOLD_SECONDS = 300


def set_auto_hold(config_dir: str, seconds: int = AUTO_HOLD_SECONDS) -> bool:
    """Hold failover across a restart this integration is about to perform.

    Returns True if a hold was written. Returns False, and writes nothing, when
    a hold already exists — an operator's hold is indefinite by design, and
    stamping an expiry onto it would quietly resume failover during exactly the
    maintenance they asked us to sit out.
    """
    try:
        path = hold_path(config_dir)
        if path.is_file():
            return False
        deadline = datetime.now(UTC) + timedelta(seconds=seconds)
        path.write_text(
            f"{AUTO_HOLD_MARKER}\n"
            f"Home Assistant is restarting; failover is held until it returns.\n"
            f"expires: {deadline.isoformat()}\n",
            encoding="utf-8",
        )
    except OSError:
        # Never fatal, and silent like the rest of this module. Failing to set
        # the hold means a restart behaves as it did before this existed, which
        # is survivable; failing a shutdown because of it is not.
        return False
    return True


def clear_auto_hold(config_dir: str) -> bool:
    """Remove a hold this integration set, and only one it set.

    🚨 The marker check is the safety. On start-up we cannot know whether the
    hold on disk is ours from the last shutdown or one a person set while we
    were down — and clearing theirs would resume failover in the middle of
    their maintenance, which is worse than leaving ours in place for its
    remaining few minutes.
    """
    try:
        path = hold_path(config_dir)
        if not path.is_file():
            return False
        if AUTO_HOLD_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            return False
        path.unlink()
    except OSError:
        return False
    return True
