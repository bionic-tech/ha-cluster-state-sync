"""Operator-requested handover: give the lease to the peer, on purpose.

The maintenance hold answers "do not fail over". This answers the opposite
question -- "fail over now" -- and until it existed the only ways to move the
cluster deliberately were to stop Home Assistant on the leader (which is what
caused a real outage on 2026-09-06, because a stopped leader is also a leader
whose container `notify_backup.sh` will not restart) or to write force-master
on the peer by hand at a console.

Same shape as the hold, for the same reason: a flag file inside the config
directory, so the container writes `/config/...` and the host promoter reads
the translated path, and both are looking at one set of bytes.

**Consumed, not persistent.** The promoter deletes the request as it acts on
it. A hold is a state you are in; a handover is an event that happens once. A
request that survived its own execution would hand the cluster over again on
the next tick, and again after that.
"""

from __future__ import annotations

import logging
import os
import pathlib

_LOGGER = logging.getLogger(__name__)

HANDOVER_FILENAME = ".cluster_sync_handover_request"

#: What a handover written from the dashboard records. The promoter logs the
#: reason when it acts, so `journalctl` says who asked and why -- an unexplained
#: leadership change is the thing this whole project keeps having to
#: reconstruct after the fact.
UI_HANDOVER_REASON = "requested from the Home Assistant dashboard"


def handover_path(config_dir: str) -> pathlib.Path:
    return pathlib.Path(config_dir) / HANDOVER_FILENAME


def is_requested(config_dir: str) -> bool:
    """Never raises. An unreadable flag means NO handover.

    Fails toward staying put, exactly as `hold.is_held` fails toward not
    holding: an unreadable file must not move a house between nodes.
    """
    try:
        return handover_path(config_dir).is_file()
    except OSError:
        return False


def request_handover(config_dir: str, reason: str = UI_HANDOVER_REASON) -> None:
    """Ask this node to give up leadership. Raises OSError on failure.

    Does not fail soft, for the same reason `set_hold` does not: an operator
    who believes they have initiated a handover, and has not, will go on to do
    something more drastic.
    """
    target = handover_path(config_dir)
    tmp = target.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(f"{reason}\n", encoding="utf-8")
    tmp.replace(target)


def clear_request(config_dir: str) -> None:
    """Consume the request. Idempotent; an absent file is success."""
    try:
        handover_path(config_dir).unlink()
    except FileNotFoundError:
        return
