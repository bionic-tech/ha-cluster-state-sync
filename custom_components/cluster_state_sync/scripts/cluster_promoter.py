#!/usr/bin/env python3
"""Take or renew the cluster lease, and promote or demote on a change of it.

Keepalived is not installed on either tiger, and never will be — the
promotion scripts (`notify_master.sh`, `notify_backup.sh`, `notify_fault.sh`)
were generated as its hooks, but nothing runs on the wire to invoke them.
Nothing writes `/run/cluster-sync/vrrp-state`; the fileset-pull timer reads
it, finds no `BACKUP`, and skips — once a minute, forever. This program is
what replaces Keepalived: the Valkey lease, taken or renewed here every tick,
*is* the leader election. Keepalived's VRRP heartbeat and this program's
lease-renewal timer both answer the same question — "am I still the one
node holding this?" — and this one runs on hardware that already has to
reach Valkey for the fileset pull anyway.

It reads `vrrp-state` to learn what the previous tick decided, but never
writes it: `notify_master.sh` and `notify_backup.sh` already write that file,
first and atomically, before doing anything else (`bundle.py`'s
`_record_vrrp_state`), and a second writer would race the once-a-minute
reader those scripts exist to feed. On a change of the lease outcome — and
only on a change, never on a schedule, the same rule the integration's own
`ServiceGate` follows — this execs the matching notify script and lets it do
that writing.

**The leader renews only while Home Assistant answers HTTP (design D3).**
§7 of the design doc used to call health-checking beyond the lease out of
scope; that was withdrawn 2026-09-01 as false. Before this program existed,
`acquire_leadership` ran only from inside Home Assistant, so a dead Home
Assistant stopped renewing the lease and it lapsed on its own. This host-side
timer renews regardless of Home Assistant's health, so the lease drifted from
meaning "this node is serving Home Assistant" to meaning "this host is
powered on" — and a wedged-but-running Home Assistant on a live host is
exactly the outage this design exists for. See `_probe_ha` and D3's asymmetry
in `run()`: the probe gates renewal, never taking, so a standby whose Home
Assistant is deliberately stopped can still take a free lease and start it.

Standard library only. This runs on the host, where neither Home Assistant
nor any of its dependencies exist. It imports `resp.py` (the RESP/Valkey
client) and `lease.py` (the shared take-or-renew script) — **never
`fileset_pull.py`**, even though that module used to be where `resp.py`'s
contents lived: `fileset_pull.py` also imports `crypto.py`, which imports the
third-party `cryptography` package, and this promoter needs none of it. It
used to import `ValkeyClient` from `fileset_pull` anyway, and dragged that
whole decryption stack in transitively — on a host without
`python3-cryptography` installed, the systemd timer failed on every tick with
nothing but a log line to show for it. See `resp.py`'s own docstring for the
incident that found it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
import os
import pathlib
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

try:  # imported as part of the integration package, e.g. by the tests
    from ..lease import (
        FORCE_SCRIPT,
        LEASE_SCRIPT,
        RELEASE_SCRIPT,
        RENEW_ONLY_SCRIPT,
        lease_ttl_ms,
    )
    from .resp import DEFAULT_DB, PASSWORD_ENV, ValkeyClient, ValkeyError
except ImportError:  # run as a standalone script on the host, with its siblings beside it
    from lease import (  # type: ignore[no-redef]
        FORCE_SCRIPT,
        LEASE_SCRIPT,
        RELEASE_SCRIPT,
        RENEW_ONLY_SCRIPT,
        lease_ttl_ms,
    )
    from resp import DEFAULT_DB, PASSWORD_ENV, ValkeyClient, ValkeyError  # type: ignore[no-redef]

#: Default lease TTL, in seconds. Mirrors `const.LEASE_TTL_SECONDS`, repeated
#: rather than imported for the same reason `resp.DEFAULT_DB` repeats
#: `const.DEFAULT_REDIS_DB`: `const.py` pulls in `voluptuous`, which this
#: stdlib-only host script may not import.
DEFAULT_TTL_SECONDS = 30

#: The promoter's own connect/socket timeout -- deliberately shorter than
#: `resp.SOCKET_TIMEOUT` (30s), which is a legitimate default for the
#: fileset pull's one-minute batch job but far too long here: that timeout
#: covers the connection AND every subsequent `recv`, so AUTH, SELECT and
#: EVAL against a Valkey that accepts the TCP handshake but never answers
#: could each block for up to 30s -- roughly 90s total, the design's "three
#: attempts per TTL" gone, and a healthy leader losing the lease it never
#: got the chance to renew, with nothing to log because nothing had failed
#: yet. 5s keeps a single stalled call well under the default 30s TTL and
#: under the 10-second tick interval (`bundle.py`'s `OnUnitActiveSec`), so a
#: hung connection fails in time for the NEXT tick to actually run.
CONNECT_TIMEOUT_SECONDS = 5.0

#: How long a notify script gets before this process gives up on it.
#: `subprocess.run` with no `timeout=` can block forever -- a stuck Docker
#: daemon on `docker start`, say. Nothing bounds a systemd oneshot's own
#: runtime by default, so a hung tick never exits, systemd will not start
#: the NEXT tick of a still-running oneshot, and the lease laps at its TTL
#: while this node sits mid-promotion, unable to demote. The peer then
#: legitimately takes over -- two masters, indefinitely, with this one stuck
#: rather than failed. 120s is comfortably longer than a real promotion
#: (rehearsed at ~31s end to end) and well under how many ticks fit in a
#: lease TTL's worth of renewals, so a promotion this slow is genuinely
#: exceptional rather than borderline.
NOTIFY_TIMEOUT_SECONDS = 120

#: Where Home Assistant's own HTTP API answers, from the host (design D3).
#: `/api/` needs no credentials -- see `_probe_ha` -- so this is a plain URL,
#: never a token.
DEFAULT_HA_URL = "http://127.0.0.1:8123/"

#: How long the HTTP probe waits before counting as silent. Short on purpose:
#: this runs once a tick, well inside the 10-second interval, and a probe
#: that itself hangs must not be what keeps a dead leader's lease alive.
PROBE_TIMEOUT_SECONDS = 5.0

#: How long after this node records MASTER it renews without probing
#: (design D3). `docker start` returns long before Home Assistant serves
#: HTTP, and a cold boot routinely outlasts the lease's own TTL -- without
#: this, a freshly-promoted node would fail its own probe while still
#: booting, release the lease it just took, and flap. Comfortably longer
#: than a cold Home Assistant boot; overridable via `--probe-grace`.
#: **600, not 300.** 300 was shorter than a real Home Assistant takes to boot,
#: which made every promotion of node-b self-terminating. Measured twice on
#: 2026-09-04: `Home Assistant initialized in 328.71s` and `325.11s`, against a
#: 300s grace. The sequence was deterministic, not flaky --
#:
#:     18:39:26  container starts
#:     18:44:26  grace expires; Home Assistant still ~27s from ready
#:     18:44:53  Home Assistant initialized in 325.11s
#:     18:45:14  Demoting to BACKUP - stopping home-assistant-2
#:
#: -- and it was read as "the standby keeps crashing" for a day. The node
#: promoted, released the lease it had just taken, stopped its own Home
#: Assistant, and did it again on the next promotion.
#:
#: The grace must exceed the SLOWEST cold boot on the slowest node, not a
#: typical one, because being wrong in this direction costs a full failover
#: rather than a slow one.
DEFAULT_PROBE_GRACE_SECONDS = 600.0

#: The BASE grace, used when nothing says Home Assistant is on its way back.
#:
#: 600s was chosen to exceed the slowest cold boot on the slowest node, and it
#: does -- but it applies that worst case to every failure, so a genuinely
#: wedged instance costs ten minutes before the peer may promote. Measured cold
#: boots on the fleet this was built for: 24.0s and 62.9s to
#: `Home Assistant initialized`. 120s is roughly twice the slowest, which is the
#: margin the old flat value was really buying.
#:
#: The full `DEFAULT_PROBE_GRACE_SECONDS` is still granted, but only while the
#: container is *demonstrably* restarting -- see `container_is_returning`.
DEFAULT_BASE_GRACE_SECONDS = 120.0

#: How recently a container must have started for "it is still booting" to be a
#: fair reading. Matched to the base grace deliberately: a start older than the
#: window we would have waited anyway is not evidence of a boot in progress.
RETURNING_STARTED_WITHIN_SECONDS = 120.0

#: Home Assistant's own "restart me" exit code. A container that exited 100 was
#: asked to restart -- by a config reload or an upgrade -- and is not a crash.
HA_RESTART_EXIT_CODE = 100

#: How long after releasing a lease for a silent probe this node refuses to
#: take a free one back (design M3). Without this: release writes BACKUP
#: (`docker stop`), the very next tick sees `previous == "BACKUP"`, takes
#: the lease it just freed, promotes (`docker start`), and earns a fresh
#: `DEFAULT_PROBE_GRACE_SECONDS` -- a perpetual cycle with the peer down,
#: worse than a restart loop because each promotion also re-runs the
#: fileset swap. Only gates TAKING a lease this node released for its own
#: unhealthy probe; a lease free for any other reason (first boot, the peer
#: crashed) is unaffected -- see the hold-down marker's own docstring.
#: Overridable via `--release-holddown`.
#:
#: **900, and 180 was measured to be wrong.** This value has now failed in both
#: directions on real hardware, so both are recorded here.
#:
#: Too long (900) looked like the fault first: node-b sat BACKUP for 901
#: seconds with its peer down before promoting unaided. The reasoning that
#: followed -- that 900 was a 6x overshoot of the failover budget -- was wrong,
#: because it treated the window as a delay to be minimised rather than as the
#: thing keeping a broken leader out.
#:
#: Too short (180) took the house down. 2026-09-04, with both nodes live:
#:
#:     21:02:11  node-a demotes (its Home Assistant was stopped)
#:     21:02:14  node-b promotes
#:     21:05:48  node-a RECLAIMS -- hold-down expired at 180s
#:     21:05:52  node-b demotes
#:     21:06:20  node-b promotes again
#:     21:06:21  node-a demotes again
#:
#: A released leader whose Home Assistant is still stopped takes the lease back
#: the moment this expires, because D3's probe gates renewal and never taking.
#: So the window must outlast the repair of whatever caused the release -- an
#: operator's attention span, not a boot time.
#:
#: What makes 900 affordable now, and did not before, is that `--adopt` unlinks
#: the marker (see `adopt`). The window is no longer a sentence with no appeal:
#: an operator who has fixed the node says so, and promotes immediately.
DEFAULT_RELEASE_HOLDDOWN_SECONDS = 900.0

#: How far back `--adopt` dates the state file. Comfortably beyond any sane
#: `--probe-grace`, so an adopted node is probed on its very next tick rather
#: than trusted for a window it never earned.
_ADOPT_BACKDATE_SECONDS = 86400.0


class PromoterError(Exception):
    """The promoter refuses to run, for a reason of its own.

    For the promoter's own configuration problems — an unusable `--ttl`, say
    — never for wrapping a failure from the Valkey client. Those are
    `ValkeyError` (or a subclass), and `run()` catches that boundary
    separately so a backend failure and a promoter misconfiguration are never
    confused with each other.
    """


def _leader_key(namespace: str) -> str:
    """`const.leader_key`, repeated rather than imported.

    Same reasoning as `fileset_pull._manifest_key`: `const.py` imports
    `voluptuous`, which is not on a bare host, so the format is duplicated
    here instead of shared.
    """
    return f"ha:cluster_state_sync:{namespace}:leader"


def _promoter_key(namespace: str, node_id: str) -> str:
    """Mirrors `const.promoter_key`. Repeated rather than imported for the same
    reason every other constant here is: this file is emitted as standalone
    text onto a host that has no `custom_components` on its path (ADR-005)."""
    return f"ha:cluster_state_sync:{namespace}:promoters:{node_id}"


def read_state(path: pathlib.Path) -> str:
    """The previous tick's decision, or `""` when it cannot be known.

    Never raises, and never defaults to `MASTER`: a node that assumes
    leadership because it could not read a status file is the split-brain
    this whole design exists to prevent. A missing file, a missing directory,
    and a permission error are all "unknown" — decide() already treats an
    unknown previous state safely on both branches.

    `errors="replace"`, deliberately: a torn write (`_record_vrrp_state` in
    bundle.py acknowledges this reader can catch one in progress) can leave a
    partial multi-byte sequence, and plain `read_text` raises
    `UnicodeDecodeError` on that -- a `ValueError`, not an `OSError`, so it
    would escape `run()`'s `except (ValkeyError, OSError)` as a bare
    traceback. That would crash the leader's promoter BEFORE the lease is
    renewed, on every tick: it stops renewing without ever discovering it
    lost the lease, and the peer legitimately takes over while this node
    never demotes -- the exact ordering hazard `run()`'s own comment above
    the eval call warns against, reopened one layer down. `errors="replace"`
    turns undecodable bytes into a placeholder instead, so a corrupt file
    reads as a value that matches neither `MASTER` nor `BACKUP` -- handled by
    `decide()` as a transition, the same as any other unrecognised state.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def decide(*, holds_lease: bool, previous: str) -> str | None:
    """`MASTER`, `BACKUP`, or `None` when nothing changed.

    Acts on the *change* of the lease outcome, never on the outcome itself.
    Re-running `notify_master.sh` every tick because this node still holds
    the lease would restart containers and reapply firewall rules forever —
    the anti-thrash rule the integration's own `ServiceGate` already
    establishes.
    """
    current = "MASTER" if holds_lease else "BACKUP"
    return None if current == previous else current


#: Publish this promoter's heartbeat. `SET key ts PX ttl` -- unconditional,
#: because the question it answers is "is this promoter running at all", not
#: "does it lead". A follower's heartbeat is the interesting one: on a cold
#: standby the promoter is the ONLY thing running, so it is the only thing that
#: can report the machine is still able to promote (ADR-009).
HEARTBEAT_SCRIPT = "return redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])"


def _beat(client: Any, key: str, ttl_ms: int) -> None:
    """Best effort, always. A failed heartbeat must never change a decision.

    Deliberately swallows everything: this is an observability signal, and a
    promoter that skipped a promotion because it could not write a diagnostic
    would be a far worse bug than a missing diagnostic.
    """
    try:
        client.eval(HEARTBEAT_SCRIPT, [key], [str(int(time.time())), str(ttl_ms)])
    except Exception:  # noqa: BLE001 - see docstring
        pass


def container_is_returning(
    inspect: Callable[[], dict[str, Any] | None],
    *,
    now: float | None = None,
    started_within: float = RETURNING_STARTED_WITHIN_SECONDS,
) -> tuple[bool, str]:
    """Is Home Assistant *coming back*, as opposed to simply not answering?

    Returns `(returning, reason)`. The reason is carried into the log line and
    the operator surface, because "no failover yet" and "no failover ever" look
    identical from outside and the difference is the whole explanation
    (ADR-008 §3: a message may not assert a fact the code did not establish).

    Three signals, and deliberately **not** a fourth. An increasing restart
    count means a crash loop, and a crash loop is precisely when the peer
    *should* take over -- treating it as "coming back" would build the mode
    this must never have, where failover silently never happens.

    This does not weaken D3. A wedged-but-running Home Assistant reports
    `Restarting == false` with an old `StartedAt`, so it still demotes on the
    base grace. ADR-006 rejected `docker inspect` as a *replacement* for the
    HTTP probe, which was right; this uses it only to tell "down and coming
    back" from "down".

    `inspect` returning `None` -- no Docker, no such container, a CLI that is
    not there -- means **no extension**, which is the pre-existing behaviour on
    every platform that cannot answer the question.
    """
    state = inspect()
    if not state:
        return False, ""
    if state.get("Restarting") is True:
        return True, "container is restarting"
    if state.get("ExitCode") == HA_RESTART_EXIT_CODE and state.get("Running") is not True:
        return True, f"container exited {HA_RESTART_EXIT_CODE} (Home Assistant asked to restart)"
    started = state.get("StartedAtEpoch")
    if isinstance(started, (int, float)) and started > 0:
        age = (time.time() if now is None else now) - started
        if 0 <= age < started_within:
            return True, f"container started {age:.0f}s ago and is still booting"
    return False, ""


def _probe_ha(url: str, timeout: float) -> bool:
    """Is Home Assistant's own HTTP API answering (design D3)?

    No credentials, on purpose: `GET /api/` returns 401 without a token, and
    that 401 IS proof the server is up and answering. So *any* response
    counts as alive, including an error one -- only a connection that never
    got a response at all counts as dead.

    Getting the discrimination backwards here is the one way this function
    can fail dangerously. `HTTPError` (any 4xx/5xx, the 401 included) is a
    subclass of `URLError`, so it MUST be caught first, or the 401 that
    proves Home Assistant is alive would be read as the same "silent" case a
    genuinely dead server produces -- and every renewal would be refused on
    a perfectly healthy leader.

    `except Exception: return False` -- deliberately broad, and deliberately
    NOT narrowed to `(URLError, TimeoutError)`. Verified against a real
    socket: a listener speaking non-HTTP on this port (Home Assistant with
    `http: ssl_certificate:` set, probed with a plain `http://` URL --
    `bundle.py`'s `_promoter_ha_url` builds exactly that) raises
    `http.client.BadStatusLine`, which is neither a `URLError` nor an
    `OSError` -- it would escape this function AND `run()`'s own
    `(ValkeyError, OSError)` boundary as a bare traceback, aborting the
    whole tick before either renewal or demotion. That is worse than a
    stale lease: the old leader never demotes -- its containers and
    firewall rules stay up -- while the standby eventually takes the lease
    on TTL expiry alone. Two masters, and the exact failure D3 exists to
    close, reopened by D3's own code. `ConnectionResetError` (accept-then-
    close) IS an `OSError`, so it would have been swallowed by `run()`'s
    boundary anyway -- but with the same effect, since arriving there means
    the tick aborts before `decide()` ever runs, not that it demotes.
    Every failure of this function means exactly one thing -- "Home
    Assistant answered nothing useful" -- and the whole contract this
    function makes to its caller is a plain bool. Never re-narrow this to a
    specific exception list; the next transport surprise will not be on it
    either.
    """
    try:
        urllib.request.urlopen(url, timeout=timeout)
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False
    return True


def _recently_touched(path: pathlib.Path, window_seconds: float) -> bool:
    """True until `window_seconds` after `path`'s mtime.

    Shared by two unrelated timers that both need "has it been less than N
    seconds since this file was last written", measured from the file
    itself rather than this process's own start time so restarting the
    promoter never resets either clock: the D3 boot-grace window
    (`state_path`, `DEFAULT_PROBE_GRACE_SECONDS`) and the M3 release
    hold-down (the holddown marker, `DEFAULT_RELEASE_HOLDDOWN_SECONDS`).
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        # No file, or it vanished underneath us: nothing to measure from, so
        # there is nothing to protect. Treat as expired rather than silently
        # granting an unbounded window -- for grace, that would mean never
        # probing at all; for the hold-down, a take that can never happen.
        return False
    return age < window_seconds


def _mark_force_used(force_path: pathlib.Path, node_id: str) -> None:
    """Record who *first* bypassed the split-brain guard, and when.

    Written once, not on every tick the override stays in place: the marker's
    forensic purpose is "when did this bypass begin", and rewriting it on
    every renewal would leave only the most recent tick's timestamp, which
    answers a different question. `with_name`, not `with_suffix` -- the
    latter *replaces* an existing suffix, so a force file an operator names
    with a dot in it (`force.master`, say) would otherwise produce a marker
    named `force.used` instead of `force.master.used`.
    """
    used = force_path.with_name(force_path.name + ".used")
    if used.exists():
        return
    stamp = datetime.now(tz=UTC).isoformat()
    used.write_text(f"{node_id} {stamp}\n", encoding="utf-8")


def _require_identity(value: str, flag: str) -> str:
    """Guard against an unset generator variable reaching Valkey as `""`.

    A blank `--node-id` or `--namespace` is not a usage error a human is
    likely to make by hand; it is what an interpolated-but-unset template
    variable looks like. Two nodes each presenting `""` would both renew the
    *same* lease and both believe they lead -- the identity collision that
    cost this project a day. `PromoterError`, not a bare return: this is
    exactly the promoter's own configuration refusal the class exists for.
    """
    if not value.strip():
        raise PromoterError(f"{flag} must not be empty")
    return value


def _holddown_path(state_path: pathlib.Path) -> pathlib.Path:
    """Where M3's release hold-down marker lives, by default.

    A sibling of `state_path` -- the same `/run/cluster-sync/` directory
    `vrrp-state` and `force-master` already live in -- rather than a
    brand-new CLI flag: it is this node's own record of its own recent
    release, not something an operator needs to point anywhere else.
    """
    return state_path.with_name("release-holddown")


def _docker_inspector(container: str) -> Callable[[], dict[str, Any] | None]:
    """Ask Docker for the container's state, or `None` if it cannot be asked.

    `None` on *any* failure -- no docker, no such container, malformed output --
    because the caller treats "cannot answer" as "no extension", which is the
    flat pre-adaptive behaviour and the safe direction. A promoter that refuses
    to demote because a CLI is missing would be worse than one that demotes
    early.

    Deliberately shells out rather than importing a Docker SDK: this file is
    emitted as text onto a host with nothing installed but `python3` (ADR-005).
    """

    def inspect() -> dict[str, Any] | None:
        try:
            out = subprocess.run(  # noqa: S603
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Restarting}} {{.State.Running}} "
                    "{{.State.ExitCode}} {{.State.StartedAt}}",
                    container,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        parts = out.stdout.split()
        if len(parts) < 4:
            return None
        started = 0.0
        try:
            started = datetime.fromisoformat(parts[3].replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        return {
            "Restarting": parts[0] == "true",
            "Running": parts[1] == "true",
            "ExitCode": int(parts[2]) if parts[2].lstrip("-").isdigit() else None,
            "StartedAtEpoch": started,
        }

    return inspect


def _grace_extended(
    *,
    state_path: pathlib.Path,
    base_grace: float,
    probe_grace: float,
    inspect_container: Callable[[], dict[str, Any] | None] | None,
    reason_path: pathlib.Path | None,
) -> bool:
    """Should the base grace be extended because Home Assistant is returning?

    Called only once the base grace has expired and the probe has already
    failed, so it can afford a `docker inspect`; on the happy path it never
    runs at all.

    🚨 **The cap is the point.** Extension stops at `probe_grace` no matter what
    the container says, so a crash loop -- which signals "restarting" forever --
    demotes on schedule. Without this the adaptive path would create the one
    mode TODO explicitly forbids: a failover that silently never happens.
    """
    if inspect_container is None:
        return False
    # Past the ceiling: no signal may extend further.
    if not _recently_touched(state_path, probe_grace):
        _write_reason(reason_path, "")
        return False
    try:
        returning, reason = container_is_returning(inspect_container)
    except Exception:  # noqa: BLE001 - a broken inspector must not block demotion
        return False
    if not returning:
        _write_reason(reason_path, "")
        return False
    print(
        f"cluster promoter: probe failed and the {base_grace:.0f}s base grace has "
        f"expired, but NOT demoting yet -- {reason}. Extending to the full "
        f"{probe_grace:.0f}s, after which this node demotes regardless.",
        file=sys.stderr,
    )
    _write_reason(reason_path, reason)
    return True


def _write_reason(path: pathlib.Path | None, reason: str) -> None:
    """Publish why a demotion is being deferred, for the operator surface.

    Best effort by design: this is an explanation, and failing to write an
    explanation must never change what the promoter does.
    """
    if path is None:
        return
    try:
        if reason:
            path.write_text(reason + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def run(
    client: Any,
    *,
    namespace: str,
    node_id: str,
    ttl: int,
    state_path: pathlib.Path,
    install_dir: pathlib.Path,
    force_path: pathlib.Path,
    runner: Callable[[list[str]], int],
    ha_url: str = DEFAULT_HA_URL,
    probe_grace: float = DEFAULT_PROBE_GRACE_SECONDS,
    probe: Callable[[str, float], bool] | None = None,
    holddown_path: pathlib.Path | None = None,
    release_holddown: float = DEFAULT_RELEASE_HOLDDOWN_SECONDS,
    hold_path: pathlib.Path | None = None,
    handover_path: pathlib.Path | None = None,
    base_grace: float = DEFAULT_BASE_GRACE_SECONDS,
    heartbeat_ttl_ms: int = 60_000,
    inspect_container: Callable[[], dict[str, Any] | None] | None = None,
    grace_reason_path: pathlib.Path | None = None,
) -> int:
    """One tick: take or renew the lease, and act only on a change of it.

    `except (ValkeyError, OSError)` — deliberately not `except Exception`.
    `ValkeyError` is the whole Valkey client's failure boundary (`RespError`
    subclasses it, and `resp.py` already wraps every socket, TLS, DNS,
    timeout and malformed-reply failure into one of the two); `OSError`
    covers this function's own file work. A bare `except Exception` would
    let a programming error keep this timer green while nothing is ever
    promoted — a log line the only symptom, which is exactly the incident
    this project exists because of.

    Note this boundary is not as clean as that description makes it sound:
    `urllib.error.URLError` -- the probe's own connection-refused/DNS-failure
    exception -- IS an `OSError` by inheritance (`URLError.__mro__` runs
    straight through it), so a probe failure that reached here would be
    silently folded into "this function's own file work" rather than
    reported as what it actually is. That is exactly why `_probe_ha` must
    never let anything escape it at all (see its own docstring): a probe
    failure has to flow through `decide()`'s ordinary release-then-demote
    path, not abort the tick here before `decide()` ever runs -- aborting
    here would demote nobody, since the old leader never gets the chance to
    write BACKUP, while the lease it stopped renewing quietly expires out
    from under it.

    `probe` defaults to `_probe_ha` (a real HTTP call) when omitted -- safe
    here only because the asymmetry below means it is never reached unless
    `previous` is already `"MASTER"` *and* the grace window has expired, and
    no test drives that combination without supplying a fake. Anything that
    did would hit the test harness's real-socket block, loudly, rather than
    quietly reaching a network.
    """
    try:
        previous = read_state(state_path)

        key = _leader_key(namespace)
        # Before anything can decide to return early, say that this promoter is
        # alive. Placed first deliberately: every branch below can exit, and a
        # heartbeat that only fires on the happy path would go quiet for the
        # very states an operator most needs to see -- a held-down node, a node
        # in maintenance hold, a node whose probe is failing.
        _beat(client, _promoter_key(namespace, node_id), heartbeat_ttl_ms)
        holddown = holddown_path if holddown_path is not None else _holddown_path(state_path)

        held = hold_path is not None and hold_path.is_file()
        if held and not force_path.exists():
            # Maintenance hold (hold.py). Renew what we have, never take what
            # is free, and never demote. Two branches collapse into one here
            # because the same file answers both questions: on the leader this
            # keeps the lease alive across a planned restart, so it never comes
            # free at all; on the standby it declines a lease that came free
            # anyway -- during planned work "free" usually means the peer is
            # mid-restart rather than dead.
            #
            # `force-master` still wins. The hold is the operator saying "not
            # yet"; force-master is the operator saying "now, I have checked".
            # An override that the hold could veto would be no override.
            result = client.eval(RENEW_ONLY_SCRIPT, [key], [node_id, lease_ttl_ms(ttl)])
            holds_lease = bool(result)
            print(
                f"cluster promoter: maintenance hold set ({hold_path}) -- "
                f"{'renewed' if holds_lease else 'not holding'} the lease, "
                f"taking no action. Failover is SUSPENDED until it is removed.",
            )
            if decide(holds_lease=holds_lease, previous=previous) is not None:
                # Deliberately not acted on. Say so, because a hold that
                # silently swallowed a real transition would look identical to
                # a healthy cluster right up until it mattered.
                print(
                    "cluster promoter: a leadership change is pending and is "
                    "being held; remove the hold to let it happen.",
                    file=sys.stderr,
                )
            return 0

        if force_path.exists():
            # D2's override. holds_lease is forced True unconditionally: the
            # whole point of the override is to promote when Valkey itself
            # may be unreachable, so the decision must never depend on
            # consulting the lease.
            #
            # It must still WRITE the lease, best-effort. Skipping the
            # consult but leaving the old key standing opens exactly the
            # split-brain the lease exists to prevent: this node promotes by
            # fiat while the key it never touched keeps counting down, and
            # whoever legitimately takes it next -- the repaired peer,
            # typically -- believes it leads too, by a different mechanism.
            # Claiming it here means a healthy or later-recovered peer's own
            # renewal is refused and it demotes, converging on one master.
            try:
                client.eval(FORCE_SCRIPT, [key], [node_id, lease_ttl_ms(ttl)])
            except ValkeyError as err:
                # Best-effort on purpose: this override exists precisely for
                # when Valkey cannot be reached, so a failed write must not
                # block the promotion it was invoked to force.
                print(
                    f"cluster promoter: could not claim the lease under force-master: {err}",
                    file=sys.stderr,
                )
            holds_lease = True
            _mark_force_used(force_path, node_id)
        elif previous == "MASTER" and handover_path is not None and handover_path.is_file():
            # An operator asked, from the dashboard, for this node to hand the
            # cluster over. Placed BEFORE the D3 probe branch on purpose: a
            # deliberate handover must not depend on Home Assistant being
            # unwell, and it is the one release that happens while everything
            # is working perfectly.
            #
            # The request is consumed here rather than left for the next tick.
            # A hold is a state you are in; a handover is an event that happens
            # once, and a request that survived its own execution would hand
            # the cluster over again on the very next tick.
            reason = "requested"
            try:
                reason = handover_path.read_text(encoding="utf-8").strip() or reason
            except OSError:
                pass
            print(
                f"cluster promoter: HANDING OVER on request ({reason}). Releasing "
                f"the lease so the peer can promote, and stopping Home Assistant "
                f"here. This node will refuse to take the lease back for "
                f"{release_holddown:.0f}s (clear it with --adopt).",
                file=sys.stderr,
            )
            client.eval(RELEASE_SCRIPT, [key], [node_id])
            holds_lease = False
            # Same hold-down as a probe-driven release, and for the same
            # reason: without it this node takes back on its very next tick
            # the lease it was just asked to give away.
            try:
                holddown.touch()
            except OSError:
                pass
            # Consumed last: if anything above raised, the request survives and
            # the operator's intent is not silently lost.
            try:
                handover_path.unlink()
            except OSError:
                pass
        elif (
            previous == "MASTER"
            and not _recently_touched(state_path, base_grace)
            and not (probe if probe is not None else _probe_ha)(ha_url, PROBE_TIMEOUT_SECONDS)
            and not _grace_extended(
                state_path=state_path,
                base_grace=base_grace,
                probe_grace=probe_grace,
                inspect_container=inspect_container,
                reason_path=grace_reason_path,
            )
        ):
            # D3. The probe gates RENEWAL only, never taking -- this whole
            # branch is reachable only when we already believe we hold the
            # lease. A standby's `previous != "MASTER"` never evaluates the
            # probe at all (short-circuited above), because taking a free
            # lease is what starts Home Assistant in the first place; gating
            # that on Home Assistant already answering would mean a standby
            # could never promote.
            #
            # A leader that has stopped answering HTTP releases rather than
            # merely letting the lease lapse, so the standby can promote on
            # ITS very next tick instead of waiting out the remaining TTL.
            # No try/except here, unlike the force branch above: this is the
            # same fail-closed posture as the ordinary renewal below --
            # Valkey being unreachable while trying to release must fail the
            # whole tick loudly, not be treated as "released, so demote"
            # while the key might still be ours.
            # Say so. This is the most consequential thing the promoter does
            # -- it drops the lease and `notify_backup.sh` then stops Home
            # Assistant -- and until 2026-09-05 it did it in total silence.
            # Reconstructing the 2026-09-04 flap meant inferring releases from
            # a container's exit code and one line in notify_backup.sh, hours
            # after the fact. Every other branch says what it did; this one
            # matters more than any of them.
            print(
                f"cluster promoter: RELEASING the lease -- {ha_url} did not "
                f"answer within {PROBE_TIMEOUT_SECONDS}s and the "
                f"{probe_grace:.0f}s post-promotion grace has expired. "
                f"Demoting and stopping Home Assistant; the peer may now "
                f"promote. This node will refuse to take the lease back for "
                f"{release_holddown:.0f}s (clear it with --adopt once repaired).",
                file=sys.stderr,
            )
            client.eval(RELEASE_SCRIPT, [key], [node_id])
            holds_lease = False
            # M3. Recorded so THIS node's own next tick refuses to take the
            # lease straight back -- see the hold-down elif below. Best-
            # effort, deliberately outside any try/except: the release above
            # already happened (or the tick already failed and returned), so
            # a holddown write failing here must not turn a successful
            # release into a failed tick.
            try:
                holddown.touch()
            except OSError:
                pass
        elif previous != "MASTER" and _recently_touched(holddown, release_holddown):
            # NO PROBE HERE, deliberately. An earlier version cleared the
            # hold-down once the probe answered again, reasoning that a healthy
            # node should not be kept out. That was wrong twice over.
            #
            # It is unreachable exactly when it matters. A cold standby's Home
            # Assistant is *deliberately stopped* (`notify_backup.sh` runs
            # `docker stop`), so its probe can never succeed, so the escape
            # hatch never opens for the node that most needs it. Measured on
            # 2026-09-04: node-b sat BACKUP for 901 seconds with its peer
            # down, then promoted on its own the instant the window expired.
            #
            # And it breaks D3's invariant -- *the probe gates renewal, never
            # taking* (see `run()`'s docstring and `_probe_ha`). Letting the
            # probe decide whether the hold-down blocks a take is the probe
            # gating a take, however indirectly.
            #
            # What clears a hold-down is time, or an operator: `--adopt`
            # removes the marker outright (see `adopt`), which is the
            # deliberate act the old probe branch was groping for.
            # M3's hold-down. Without this: release (above) writes BACKUP via
            # notify_backup.sh, this node's very next tick sees
            # `previous == "BACKUP"`, takes the lease it just freed, promotes
            # (notify_master.sh, docker start, the fileset swap), earns a
            # fresh D3 boot grace, fails its probe again once grace expires,
            # and releases again -- a perpetual cycle with the peer down,
            # worse than a plain restart loop because each promotion also
            # re-runs the fileset swap. Gated on `previous != "MASTER"`
            # specifically: this refuses only a TAKE, never a renewal, and
            # only this node's own recent release triggers it -- a lease
            # free for any other reason (first boot, the peer crashed) has
            # no holddown marker and is never blocked by this branch.
            #
            # Announced for the same reason as the release above: a node
            # sitting at BACKUP and not taking looks identical to a node that
            # simply has not noticed, and the difference is the whole
            # explanation.
            #
            # It does NOT say the lease is free, because this branch has not
            # looked. It fires on the hold-down marker and `previous` alone --
            # the peer may well have taken the lease already, and on
            # 2026-09-07 it had: this line printed "NOT taking the free lease"
            # on node-b while node-a demonstrably held it, which sent an
            # operator hunting a split-brain that did not exist. A log line
            # asserting a fact it never checked is the exact failure this
            # project keeps finding elsewhere; it does not get a pass here.
            age = time.time() - holddown.stat().st_mtime if holddown.exists() else 0.0
            print(
                f"cluster promoter: NOT attempting to take the lease -- this "
                f"node released it {age:.0f}s ago and is holding down for "
                f"{release_holddown:.0f}s to avoid a promote/fail/release "
                f"cycle. Whether the peer now holds it is not checked here; "
                f"`--adopt` reports the holder and clears this once Home "
                f"Assistant is healthy on this node.",
                file=sys.stderr,
            )
            holds_lease = False
        else:
            # Ordinary take-or-renew: a standby taking a free lease (with no
            # active hold-down), a freshly-promoted leader still inside its
            # boot grace, or an established leader whose probe just
            # answered. All three renew identically -- LEASE_SCRIPT's own
            # identity check is what makes "take" and "renew" the same call.
            # Reaching here past a hold-down means it EXPIRED (the elif above
            # is the only thing that reads it, and it refuses while the marker
            # is recent). Unlink the spent marker so the next tick does not
            # re-stat a file whose window has already passed.
            if previous != "MASTER" and holddown.exists():
                try:
                    holddown.unlink()
                except OSError:
                    pass
            result = client.eval(LEASE_SCRIPT, [key], [node_id, lease_ttl_ms(ttl)])
            holds_lease = bool(result)

        new_state = decide(holds_lease=holds_lease, previous=previous)
        if new_state is None:
            return 0

        # The lease call above must always happen before this, never after.
        # Checking (or requiring) the state directory first would let a live
        # MASTER whose /run/cluster-sync has gone missing return before ever
        # calling eval: it would stop renewing its lease without ever
        # discovering it lost it, the peer would legitimately take over, and
        # this node would never demote -- two masters, indefinitely, with no
        # way for this one to find out. Ensuring the directory here, only
        # once a transition has actually been decided, also mirrors what
        # notify_master.sh already does (`mkdir -p`, bundle.py): a directory
        # that merely does not exist yet is self-healing, not a permanent
        # refusal -- only a directory that genuinely cannot be created (its
        # path is blocked by a file, say) still fails closed below, and
        # having failed, still refuses to exec.
        state_path.parent.mkdir(parents=True, exist_ok=True)

        script = "notify_master.sh" if new_state == "MASTER" else "notify_backup.sh"
        cmd = [str(install_dir / script)]
        rc = runner(cmd)
        if rc != 0:
            print(f"cluster promoter: {cmd[0]} exited {rc}", file=sys.stderr)
            return rc

        # notify_master.sh's own write of vrrp-state is deliberately
        # non-fatal (bundle.py's _record_vrrp_state), so it can exit 0
        # without having actually recorded the transition -- e.g. a `/run`
        # that exists but is unwritable. Left unchecked, `previous` would
        # read the same stale value forever and every following tick would
        # call it a promotion again: the thrash the transition-only rule
        # exists to prevent, now silent because the script itself reported
        # success. This does not fix that loop, but it stops it being quiet.
        after = read_state(state_path)
        if after != new_state:
            print(
                f"cluster promoter: {cmd[0]} exited 0 but {state_path} reads "
                f"{after!r}, not {new_state!r}",
                file=sys.stderr,
            )
            return 1
        return 0
    except (ValkeyError, OSError) as err:
        print(f"cluster promoter: {err}", file=sys.stderr)
        return 1


def adopt(
    client: ValkeyClient,
    *,
    namespace: str,
    node_id: str,
    state_path: pathlib.Path,
) -> int:
    """Record the role this node already has, without acting on it.

    Arming the timer on a cluster that is already running is otherwise a
    promotion. `decide()` fires on a *change*, and a fresh install has no
    `vrrp-state` at all, so the very first tick reads `previous == ""`, treats
    the status quo as a transition into it, and runs the matching notify
    script. On the node that is already the leader that means
    `notify_master.sh`: a fileset swap and a device pre-flight that rewrites
    `.storage` underneath a Home Assistant which never stopped running. The
    install would break the thing it was installing.

    So `install.sh` seeds the file with this first, and the real first tick
    then sees no change and does nothing.

    Two properties make it safe to run against a live cluster:

    **It observes with `GET`; it never evaluates the lease script.** Taking the
    lease on a standby *is* the promotion this exists to avoid, and even a
    renewal on the leader would extend a TTL on the strength of an install
    step rather than a health check.

    **A lease nobody holds reads as BACKUP**, for the same reason `read_state`
    refuses to default to MASTER: a node that claims leadership because it
    could not establish otherwise is the split brain the design exists to
    prevent. If that leaves a genuinely-leaderless cluster seeded BACKUP
    everywhere, the first ordinary tick takes the lease and promotes properly,
    through the path that is tested.
    """
    try:
        holder = client.get(_leader_key(namespace))
        state = "MASTER" if holder == node_id else "BACKUP"

        # Same write discipline as `_record_vrrp_state` in bundle.py: the pull
        # timer reads this file on a schedule and can catch a write in
        # progress, and a torn value matches none of MASTER, BACKUP or FAULT.
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(f".adopt.{os.getpid()}")
        tmp.write_text(f"{state}\n", encoding="utf-8")
        tmp.replace(state_path)

        # Backdate it, or adopting grants a five-minute no-probe window.
        #
        # D3's grace is measured from this file's mtime: a node that has just
        # promoted renews without probing while Home Assistant boots. Adopt is
        # the opposite -- it records a state the node has been in, possibly for
        # weeks. Leaving a fresh mtime told the promoter this node had just
        # promoted, so it renewed the lease unconditionally for five minutes
        # after Home Assistant stopped. Measured: a failover that took 12
        # seconds when the state file was old sat at two minutes and counting
        # when `install.sh` had run shortly before.
        old = time.time() - _ADOPT_BACKDATE_SECONDS
        os.utime(state_path, (old, old))

        # Clear any hold-down. This is the operator's escape hatch, and the
        # only one: `run()` deliberately will not let the D3 probe open it
        # (see the hold-down branch), because a cold standby's Home Assistant
        # is stopped on purpose and its probe can never answer. Adopting is an
        # explicit human act against a cluster whose real state is being
        # re-read from Valkey, so a marker left by an earlier release is
        # exactly the stale thing adoption exists to clear.
        #
        # Best-effort, like the `touch()` that writes it: the state file above
        # is already committed, and failing to unlink a marker must not turn a
        # successful adopt into a failed install.
        try:
            _holddown_path(state_path).unlink()
        except OSError:
            pass
    except (ValkeyError, OSError) as err:
        # Same boundary as `run()`, and the same reason: install.sh must stop
        # rather than enable a timer whose first tick would then find no
        # seeded state and promote.
        print(f"cluster promoter: adopt failed: {err}", file=sys.stderr)
        return 1

    held_by = f"held by {holder!r}" if holder else "unheld"
    print(f"cluster promoter: adopted {state} ({state_path}); lease {held_by}")
    return 0


def _run_notify_script(cmd: list[str]) -> int:
    """The default `runner` passed to `run()`: exec with a hard ceiling.

    `check=False` -- a non-zero exit is `run()`'s own to interpret and log,
    not this function's to raise on. A timeout is different in kind: nothing
    downstream can interpret a process that never returned at all, so this
    is the one place that turns "never finished" into a return code, rather
    than letting the whole tick hang forever (see `NOTIFY_TIMEOUT_SECONDS`).
    """
    try:
        return subprocess.run(cmd, check=False, timeout=NOTIFY_TIMEOUT_SECONDS).returncode
    except subprocess.TimeoutExpired:
        print(
            f"cluster promoter: {cmd[0]} did not exit within "
            f"{NOTIFY_TIMEOUT_SECONDS}s -- treating as failed",
            file=sys.stderr,
        )
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis", required=True, help="host:port")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument(
        "--db",
        type=int,
        default=DEFAULT_DB,
        help="Valkey database the lease lives in (default: %(default)s)",
    )
    parser.add_argument("--username", default=None, help="Valkey ACL user, if the server has one")
    parser.add_argument("--tls", action="store_true", help="connect over TLS, with verification")
    parser.add_argument(
        "--tls-ca-file",
        default=None,
        help="CA bundle to verify the server against; omit to use the system trust store",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=DEFAULT_TTL_SECONDS,
        help="lease TTL, in seconds (default: %(default)s)",
    )
    parser.add_argument("--state-file", required=True, type=pathlib.Path)
    parser.add_argument("--install-dir", required=True, type=pathlib.Path)
    parser.add_argument("--force-file", required=True, type=pathlib.Path)
    parser.add_argument(
        "--ha-url",
        default=DEFAULT_HA_URL,
        help="Home Assistant's own HTTP API, probed before renewing a held "
        "lease -- design D3 (default: %(default)s)",
    )
    parser.add_argument(
        "--probe-grace",
        type=float,
        default=DEFAULT_PROBE_GRACE_SECONDS,
        help="the CEILING on the grace. Reached only while the container is "
        "demonstrably restarting; extension stops here regardless, so a crash "
        "loop still demotes (default: %(default)s)",
    )
    parser.add_argument(
        "--base-grace",
        type=float,
        default=DEFAULT_BASE_GRACE_SECONDS,
        help="renew without probing for this long after promoting. A wedged "
        "Home Assistant demotes at this point; one that is visibly coming back "
        "is extended toward --probe-grace (default: %(default)s)",
    )
    parser.add_argument(
        "--ha-container",
        default="",
        help="container name to inspect when deciding whether Home Assistant "
        "is coming back. Empty (the default) disables the adaptive grace and "
        "falls back to the flat --probe-grace, which is correct anywhere "
        "docker cannot answer: bare metal, Core, or no CLI",
    )
    parser.add_argument(
        "--grace-reason-file",
        type=pathlib.Path,
        default=None,
        help="where to publish why a demotion is being deferred, for the operator surface",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the D3 liveness probe entirely and renew unconditionally "
        "while holding the lease -- an escape hatch for a host that cannot "
        "reach Home Assistant over HTTP; disables the protection D3 exists "
        "for (see INSTALL.md)",
    )
    parser.add_argument(
        "--release-holddown",
        type=float,
        default=DEFAULT_RELEASE_HOLDDOWN_SECONDS,
        help="after releasing a lease for a silent probe, refuse to take a "
        "free one back for this long -- design M3, so a wedged-but-running "
        "Home Assistant does not cycle release/promote/re-fail-probe every "
        "few minutes (default: %(default)s)",
    )
    parser.add_argument(
        "--hold-file",
        default=None,
        type=pathlib.Path,
        help="maintenance hold: while this file exists, renew a lease already "
        "held, never take a free one, and never promote or demote -- so a "
        "planned restart does not become a failover (see hold.py). "
        "force-master still overrides it",
    )
    parser.add_argument(
        "--handover-file",
        type=pathlib.Path,
        default=None,
        help="operator handover request, written by the dashboard. When present "
        "on the leader, the lease is released so the peer promotes, and the "
        "request is consumed. Lives in the config directory so the container "
        "and the host see one file.",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help="record the role this node already holds into --state-file and "
        "exit, running no notify script -- what install.sh uses so arming "
        "the timer on a live cluster is not itself a promotion",
    )
    # There is deliberately no --password. See resp.PASSWORD_ENV:
    # argv is world readable through `ps`, so the password arrives in the
    # environment instead.
    args = parser.parse_args(argv)

    try:
        node_id = _require_identity(args.node_id, "--node-id")
        namespace = _require_identity(args.namespace, "--namespace")
        if args.ttl <= 0:
            raise PromoterError("--ttl must be a positive number of seconds")
    except PromoterError as err:
        print(f"cluster promoter: {err}", file=sys.stderr)
        return 1

    host, _, port = args.redis.partition(":")
    try:
        client = ValkeyClient.connect(
            host=host,
            port=int(port or 6379),
            username=args.username,
            # Empty is treated as absent: an exported-but-unset variable is
            # how a missing password file shows up, and "" is not a password.
            password=os.environ.get(PASSWORD_ENV) or None,
            db=args.db,
            use_tls=args.tls,
            ca_file=args.tls_ca_file,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except ValkeyError as err:
        print(f"cluster promoter: {err}", file=sys.stderr)
        return 1

    # --no-probe: a probe that always reports "alive" has exactly the effect
    # of never releasing on this branch, without a second code path in run()
    # for "the probe is disabled". The only way this differs from the probe
    # never running at all is that it also never renews-without-probing on a
    # true positive, which is not a distinction that matters -- a lease held
    # under --no-probe renews unconditionally either way.
    probe = (lambda _url, _timeout: True) if args.no_probe else None

    try:
        if args.adopt:
            return adopt(
                client,
                namespace=namespace,
                node_id=node_id,
                state_path=args.state_file,
            )
        return run(
            client,
            namespace=namespace,
            node_id=node_id,
            ttl=args.ttl,
            state_path=args.state_file,
            install_dir=args.install_dir,
            force_path=args.force_file,
            runner=_run_notify_script,
            ha_url=args.ha_url,
            probe_grace=args.probe_grace,
            probe=probe,
            release_holddown=args.release_holddown,
            hold_path=args.hold_file,
            handover_path=args.handover_file,
            base_grace=args.base_grace,
            inspect_container=(_docker_inspector(args.ha_container) if args.ha_container else None),
            grace_reason_path=args.grace_reason_file,
        )
    finally:
        # This runs from a systemd timer, forever. A socket leaked on every
        # tick is a slow-motion outage on the node standing by to take over.
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
