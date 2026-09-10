"""Keep this node's own identity across a fileset swap.

The swap replaces `.storage` **wholesale**, which is what makes the companion
app keep working after a promotion: `.storage/auth` carries the refresh tokens
and `.storage/core.config_entries` carries the `mobile_app` registrations, and
both have to be inherited from the leader.

But that same `core.config_entries` also holds *this integration's own config
entry*, which must **not** be inherited. It carries `node_id`, and the lease
that prevents two leaders (ADR-003, AR-0017) guards on pure identity::

    local current = redis.call('GET', KEYS[1])
    if current == false then SET ...; return 1
    elseif current == ARGV[1] then PEXPIRE ...; return 1   -- identity, not lock
    end
    return 0

So a standby promoted carrying the leader's `node_id` takes the lease *as the
leader*, and when the leader comes back it matches `current == ARGV[1]`,
renews, and is told it holds the lease. Both nodes then believe they lead,
both flush and publish, and each prunes the other's blobs computing `keep`
from its own generations. The rehearsal observed exactly this.

AR-0025 is the same failure by a different route (two nodes, one identity),
and its fix -- suffixing `node_id` with Home Assistant's install UUID -- does
not save this one: the UUID lives in `.storage/core.uuid`, which is replicated
too.

`node_id` is not alone in that entry. `ha_config_path` differs between the two
hosts already -- on this fleet by a whole path component (finding F4) -- so an
inherited one sends the swap to a directory that does not exist.
`ha_container` differs too, and `ha_container_ip` would generate firewall rules
for the wrong address.

(`peer_host` used to head this list. It was removed in 2026-09 as genuinely
dead: collected by the wizard, stored, and read by nothing. The argument below
is why removing a field is safe and removing the *rule* would not be.)

**The whole entry is preserved, not a list of fields.** A per-node setting
added in a year cannot then silently reintroduce this bug. The accepted cost
is that a genuinely cluster-wide setting changed on the leader will not
propagate until the operator reconfigures -- config drift rather than a
correctness failure, and in the safe direction.

This is a separate program from `ha_device_preflight.py` on purpose. That
script's job is "which devices may this node claim"; identity preservation is
a different concern, and both stay readable by staying apart.

Usage, from `cluster-fileset-swap.sh`, twice and in this order::

    python3 fileset_identity.py capture --storage /config/.storage --identity FILE
    # ... the swap installs the go-bag over /config/.storage ...
    python3 fileset_identity.py restore --storage /config/.storage --identity FILE

Capture **must** precede the swap: after it, the live file is the leader's and
this node's own entry is gone. Restore must precede the device pre-flight, so
both edits land on the file Home Assistant will actually read.

Standard library only, so it runs on the host with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

#: This integration's domain. Duplicated from `const.py` rather than imported:
#: this file is shipped standalone into `/etc/cluster-sync` and runs on the
#: host, where the package is not importable.
DOMAIN = "cluster_state_sync"

#: Settings that describe THE CLUSTER, not this machine, and are therefore
#: inherited from the go-bag rather than preserved locally.
#:
#: 🚨 AR-0066. "Preserve the whole entry" was a deliberate decision, argued in
#: this module's docstring, and its stated cost was "config drift rather than a
#: correctness failure, and in the safe direction". A live re-drill on
#: 2026-09-09 showed the second half of that is false.
#:
#: The alerting configured on the leader never reached the standby, so the
#: promotion **to** the standby sent nothing while the failback **to** the
#: leader pushed normally. Two moves, one notification, and it was the wrong
#: one: the alert announced the house coming home and stayed silent when it
#: left. The failover worth being told about is precisely the one where the
#: unconfigured node is the one now running.
#:
#: So this is an ALLOWLIST and nothing else changes. A per-node setting added
#: in a year is still preserved by default and still cannot reintroduce the
#: split brain this module exists to prevent — the property the original
#: decision was protecting is kept, and only these named fields cross.
#:
#: Nothing here can affect leadership, identity or any host path. They are the
#: operator's answers to "what should this cluster do", which must be the same
#: on both halves or the pair is not one cluster.
CLUSTER_WIDE_FIELDS: frozenset[str] = frozenset(
    {
        # Who gets told, and about what (v0.4.2).
        "notify_conditions",
        "notify_services",
        # What crosses. A standby that replicates a different set restores a
        # different house -- and v0.4.2's panel card warns about exactly this
        # disagreement, which until now the design guaranteed.
        "include_domains",
        "include_entities",
        "exclude_entities",
        "exclude_devices",
    }
)

#: The Home Assistant store this operates on.
ENTRIES_FILENAME = "core.config_entries"

#: Everything went as intended.
EXIT_OK = 0
#: Could not be done. The caller must roll `.storage` back and mark the
#: promotion degraded: a swap that installed the leader's `.storage` without
#: putting our own entry back is the split-brain this program exists to stop.
EXIT_FAILED = 1
#: This node had no config entry of its own, so the leader's were *removed*
#: rather than inherited. Distinct from both of the above because the operator
#: response is different from either: the go-bag **was** installed and this
#: node comes up unconfigured, so it will never sync until someone configures
#: it -- as against EXIT_FAILED, where the go-bag was not installed and the
#: node is running its own previous config.
EXIT_NO_LOCAL_IDENTITY = 2


class IdentityError(Exception):
    """A refusal, not a crash. Carries the line the operator reads at 3am."""


def _load_store(path: Path) -> dict[str, Any]:
    """Read a Home Assistant store file, or refuse.

    A missing file is *not* handled here: the two callers want opposite things
    from it, so each decides for itself.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise IdentityError(f"cannot read {path}: {err}") from err
    except ValueError as err:
        raise IdentityError(f"{path} is not valid JSON: {err}") from err
    if not isinstance(payload, dict) or not isinstance(
        (payload.get("data") or {}).get("entries"), list
    ):
        raise IdentityError(
            f"{path} is not a config-entries store: no data.entries list. "
            "Refusing rather than treating it as an empty one."
        )
    return payload


def entries_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every config entry in a `core.config_entries` payload."""
    return list(payload["data"]["entries"])


def own_entries(payload: dict[str, Any], domain: str = DOMAIN) -> list[dict[str, Any]]:
    """This integration's own config entries, wholesale.

    Plural on purpose. One entry is the only shape seen in the field, but the
    integration does not forbid a second, and silently dropping one on
    promotion would be the same class of bug this file exists to fix.
    """
    return [e for e in entries_of(payload) if e.get("domain") == domain]


def graft_entries(
    payload: dict[str, Any], ours: list[dict[str, Any]], domain: str = DOMAIN
) -> dict[str, Any]:
    """Replace `domain`'s entries in `payload` with `ours`, leaving the rest.

    Everything about the entry is this node's own -- `data`, `options`,
    `unique_id`, `title` -- with **one** exception: `entry_id` is taken from
    the go-bag's matching entry.

    That exception is not a hole in the "preserve the whole entry" rule, it is
    what makes the rule coherent. `entry_id` is not a per-node *setting* an
    operator ever chooses; `config_entries.py` mints it as `ulid_now()`, so two
    independently configured nodes never share one. It is the **join key** of
    the registries this swap inherits wholesale, and those are the same file
    set: `core.entity_registry` rows carry `config_entry_id`, and this
    integration's entities key off it twice over --
    `_attr_unique_id = f"{entry.entry_id}_{key}"` and
    `DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})`.

    Keep the local `entry_id` and every one of those unique_ids changes, so
    `async_get_or_create` matches nothing in the inherited registry and creates
    fresh rows. The old rows are not cleaned up -- the entity registry only
    prunes on `ConfigEntries.async_remove`, and a `config_entry_id` that never
    existed at load is never removed -- so `_write_unavailable_states` pins the
    leader's `binary_sensor.…_fileset_degraded`, `sensor.…_snapshot_age`,
    `sensor.…_entities_restored` and `binary_sensor.…_backend` to
    `unavailable` forever, squatting the clean entity ids. The swap also
    installs the leader's `automations.yaml`, and
    `failover_readiness.yaml` addresses those four by `!input` -- so the
    promoted node would run the leader's readiness automation against dead ids,
    and an `unavailable` binary_sensor never matches `state: "on"`. The alarm
    would be silently inert on the one node that just failed over. AR-0040's
    shape exactly.

    A per-node *setting* added in a year still cannot leak: it lives in `data`
    or `options`, and both come from this node wholesale.

    Pairing is positional and unambiguous at one entry per node, which is the
    only shape seen in the field. The two asymmetric remainders are **not**
    symmetric in consequence, so they are handled differently:

    * more of ours than the go-bag's -- our surplus keeps its own `entry_id`.
      Nothing is dropped and no inherited row is orphaned, which is also what
      an unconfigured leader (no go-bag entry at all) produces.
    * more of the go-bag's than ours -- **refused.** There is no local entry to
      re-home the surplus onto, so grafting would drop it, and every
      `core.entity_registry` row joining on its `entry_id` would be orphaned:
      pinned `unavailable` forever on the ids `failover_readiness.yaml`
      addresses. That is exactly the failure this pairing exists to prevent,
      arriving through the back door. Nothing here can make it coherent --
      this program edits one file and does not touch the registries -- so it
      refuses, and the swap rolls back and marks. Unreachable at one entry per
      node; so was the edge case that produced this whole task.

    An empty `ours` is not this case: that is the decided unconfigured-node
    path, which removes the go-bag's entries and marks `no_local_identity`.

    The store envelope (`version`, `minor_version`, `key`) is carried through
    untouched -- Home Assistant refuses a store file without it, and a
    promotion that ends in an instance which cannot read its own config
    entries is worse than the one this fixes.
    """
    incoming = [e for e in entries_of(payload) if e.get("domain") == domain]
    kept = [e for e in entries_of(payload) if e.get("domain") != domain]
    if ours and len(incoming) > len(ours):
        raise IdentityError(
            f"the go-bag carries {len(incoming)} {domain} config entries and this node "
            f"has {len(ours)}. Grafting would silently drop {len(incoming) - len(ours)} "
            "of them and orphan every entity-registry row that joins on their "
            "entry_ids, which Home Assistant then pins to `unavailable` for good. "
            "Refusing: the swap will roll back and this node promotes on its own config."
        )
    rehomed: list[dict[str, Any]] = []
    for index, mine in enumerate(ours):
        entry = dict(mine)
        inherited_id = incoming[index].get("entry_id") if index < len(incoming) else None
        if inherited_id:
            entry["entry_id"] = inherited_id
        if index < len(incoming):
            entry = _inherit_cluster_wide(entry, incoming[index])
        rehomed.append(entry)
    grafted = dict(payload)
    grafted["data"] = {**payload["data"], "entries": kept + rehomed}
    return grafted


def _inherit_cluster_wide(mine: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any]:
    """Take the cluster-wide answers from the go-bag; keep everything else local.

    AR-0066. Default-deny: a field absent from `CLUSTER_WIDE_FIELDS` is not
    considered, so a per-node setting added later is preserved without anyone
    having to remember this function exists. That is the property the original
    "preserve the whole entry" rule was protecting, and it is kept.

    Inherited values are written to `options`, because the integration reads
    `{**entry.data, **entry.options}` and `options` therefore wins however the
    local entry happened to store the field.

    A field the leader does not carry is left alone rather than cleared: an
    unconfigured leader must not wipe a standby's settings, which would turn
    one node's missing configuration into two.
    """
    entry = dict(mine)
    options = dict(entry.get("options") or {})
    for key in CLUSTER_WIDE_FIELDS:
        for source in ("options", "data"):
            block = theirs.get(source) or {}
            if key in block:
                options[key] = block[key]
                break
    entry["options"] = options
    return entry


def _write_private(path: Path, payload: object) -> None:
    """Write JSON to `path`, owner-only, replacing whatever was there.

    The captured entry carries `cluster_secret` and `redis_password` in the
    clear, and it lands in a world-writable directory (`/tmp`) on a host that
    runs other things -- so the mode is the only thing protecting it. The
    chmod is unconditional rather than trusting `mktemp`'s 0600 default,
    because the caller may hand us a path that already exists.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(path, 0o600)


def _write_store(target: Path, payload: object) -> None:
    """Replace a store file atomically, keeping its mode.

    Through a temporary file and `os.replace`, so a crash mid-write cannot
    leave Home Assistant a truncated `core.config_entries` -- the swap's
    rollback would recover it, but only if the swap is still running. The
    replace swaps the inode, so the mode has to be carried over explicitly or
    an owner-only store silently becomes world-readable.
    """
    scratch = target.with_name(target.name + ".identity-tmp")
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(scratch, mode)
        os.replace(scratch, target)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise


def capture(storage_dir: Path, identity_file: Path) -> int:
    """Save this node's own config entries, before the swap overwrites them."""
    source = storage_dir / ENTRIES_FILENAME
    if not source.exists():
        # A node that has never run Home Assistant has no store at all. That
        # is the unconfigured case, and restore handles it by *removing* the
        # leader's entries -- not a reason to abort a promotion here.
        _write_private(identity_file, {"entries": []})
        sys.stdout.write(f"No {ENTRIES_FILENAME} at {source} — nothing to preserve.\n")
        return EXIT_OK
    mine = own_entries(_load_store(source))
    _write_private(identity_file, {"entries": mine})
    sys.stdout.write(
        f"Captured {len(mine)} {DOMAIN} config entr(ies) from {source}.\n"
        if mine
        else f"This node has no {DOMAIN} config entry of its own.\n"
    )
    return EXIT_OK


def restore(storage_dir: Path, identity_file: Path) -> int:
    """Put this node's own config entries back into the swapped-in store."""
    target = storage_dir / ENTRIES_FILENAME
    try:
        captured = json.loads(identity_file.read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise IdentityError(
            f"cannot read the captured identity at {identity_file}: {err}. "
            "Refusing: writing the go-bag's file unchanged would leave this "
            "node running as its peer."
        ) from err
    if not isinstance(captured, list):
        raise IdentityError(f"{identity_file} does not contain an entry list")
    if not target.exists():
        raise IdentityError(f"no {ENTRIES_FILENAME} at {target} after the swap")

    _write_store(target, graft_entries(_load_store(target), captured))

    if not captured:
        # An unconfigured node must not *acquire* an identity from the go-bag.
        # Nearly unreachable -- no config means no pull timer means no go-bag
        # -- but AR-0040 lived its entire life in exactly that kind of gap.
        sys.stdout.write(
            f"This node has no {DOMAIN} config entry, so the peer's were removed "
            "rather than inherited. It will come up unconfigured.\n"
        )
        return EXIT_NO_LOCAL_IDENTITY
    sys.stdout.write(f"Restored {len(captured)} {DOMAIN} config entr(ies) into {target}.\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preserve this node's own cluster_state_sync config entry "
        "across a fileset swap.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("capture", "read the LIVE store and save this node's own entries"),
        ("restore", "put the saved entries back into the swapped-in store"),
    ):
        step = sub.add_parser(name, help=help_text)
        step.add_argument("--storage", type=Path, required=True, help="path to config/.storage")
        step.add_argument(
            "--identity", type=Path, required=True, help="file the entries are held in"
        )
    args = parser.parse_args(argv)

    try:
        if args.command == "capture":
            return capture(args.storage, args.identity)
        return restore(args.storage, args.identity)
    except IdentityError as err:
        sys.stderr.write(f"{err}\n")
        return EXIT_FAILED
    except OSError as err:
        # A full disk, a read-only `.storage`, a permission the promotion did
        # not have. Caught deliberately rather than left to become a
        # traceback: an unhandled exception exits 1, so the swap rolls back
        # either way, but what the operator finds in the log at 3am should be
        # the one line this module promises everywhere else.
        sys.stderr.write(f"{args.command} failed writing to disk: {err}\n")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
