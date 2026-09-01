"""Promotion pre-flight: start Home Assistant degraded rather than not at all.

Run on the promoted node **before** Home Assistant starts. It works out which
serial devices are actually attached, and disables the config entries that
reference the ones that are not.

Why this exists: a standby that comes up missing three radios is useful. A
standby whose `rfxtrx` integrations fail at setup — and stay failed until
someone restarts it by hand — is not. The container is healthy either way,
which is what makes it a nasty failure.

**Classification comes from sysfs, never from a maintained list.** Verified on
node-a, every USB serial device resolves to one of two shapes::

    /sys/devices/pci0000:00/…/usb5/5-1/5-1.3/…     real PCI USB controller
    /sys/devices/platform/vhci_hcd.0/usb9/…        VirtualHere (USB-over-IP)

A hand-kept list of "devices the standby does not have" is a second copy of the
truth, and it rots the first time a radio moves between hosts. The kernel
already knows, and it is never stale.

Usage::

    python3 ha_device_preflight.py --storage /config/.storage          # dry run
    python3 ha_device_preflight.py --storage /config/.storage --apply

Standard library only, so it runs on the host with nothing installed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path, PurePosixPath
import sys

# Keys different integrations use for the same idea. rfxtrx says `device`,
# zwave_js says `usb_path`, zha nests it under `device.path`.
_DEVICE_KEYS = ("device", "port", "usb_path", "device_path", "path")

# Only a path that *starts* with /dev/ is local hardware. This is deliberately a
# positive test rather than a blocklist of URL schemes: the dlna entry on
# node-a carries `http://…/dev/585e8255-…/desc.xml`, and `zha` can carry
# `socket://host:port`. Both contain device-ish text and neither starts with
# /dev/, so one rule covers them and every scheme nobody has thought of yet.
_LOCAL_PREFIXES = ("/dev/",)

# How many pre-flight backups to keep. Enough to walk back from a bad promotion
# without turning `.storage` into an archive — and when fileset replication is
# enabled, `.storage` is the directory it mirrors wholesale, so every file kept
# here also travels across the network for the life of the cluster.
KEEP_BACKUPS = 5


class Attachment(Enum):
    """How a device is attached, which decides whether it can follow a failover."""

    PHYSICAL = "physical"
    VIRTUALHERE = "virtualhere"
    UNKNOWN = "unknown"


def classify_attachment(sysfs_path: str | None) -> Attachment:
    """Classify a device from its resolved sysfs path.

    `UNKNOWN` is deliberately its own answer rather than folding into
    `PHYSICAL`: treating an unrecognised attachment as local would silently
    disable a device that was simply attached in a way this has not seen.
    """
    if not sysfs_path:
        return Attachment.UNKNOWN
    if "vhci_hcd" in sysfs_path:
        return Attachment.VIRTUALHERE
    if "/pci" in sysfs_path:
        return Attachment.PHYSICAL
    return Attachment.UNKNOWN


def present_devices(by_id_dir: Path = Path("/dev/serial/by-id")) -> set[str]:
    """Every serial device currently attached.

    Returns the by-id path *and* the device node it resolves to, because Home
    Assistant stores whichever one the operator picked at setup and both name
    the same radio.
    """
    if not by_id_dir.is_dir():
        return set()
    found: set[str] = set()
    for link in by_id_dir.iterdir():
        found.add(str(link))
        try:
            found.add(str(link.resolve()))
        except OSError:  # dangling symlink — the by-id name is still useful
            pass
    return found


def attachment_of(by_id_path: str) -> Attachment:
    """Resolve a by-id symlink to its sysfs node and classify it."""
    try:
        tty = Path(by_id_path).resolve().name
        return classify_attachment(str(Path(f"/sys/class/tty/{tty}/device").resolve()))
    except OSError:
        return Attachment.UNKNOWN


def _device_references(data: object) -> list[str]:
    """Every local device path this config-entry payload names."""
    found: list[str] = []

    def walk(node: object, key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and key in _DEVICE_KEYS:
            if node.startswith(_LOCAL_PREFIXES):
                found.append(node)

    walk(data)
    return found


def _is_present(reference: str, present: set[str]) -> bool:
    """Is this device reference satisfied by something currently attached?

    Compares the leaf name as well as the full path. by-id names are unique and
    stable by construction, so this stays correct while surviving a config that
    names a device by a different-but-equivalent path, or a probe pointed at a
    copy of the directory for rehearsal.
    """
    if reference in present:
        return True
    leaf = PurePosixPath(reference).name
    return any(PurePosixPath(p).name == leaf for p in present)


def entries_to_disable(entries: list[dict], present: set[str]) -> list[str]:
    """Entry ids naming a local device that is not attached.

    Already-disabled entries are skipped so a second run is a no-op — promotion
    scripts get re-run, and the second pass must not churn the file.
    """
    doomed: list[str] = []
    for entry in entries:
        if entry.get("disabled_by"):
            continue
        refs = _device_references(entry.get("data")) + _device_references(entry.get("options"))
        # `all`, not `any`: an entry naming several devices is only disabled when
        # *every* one is missing. Partial hardware may still be partially useful,
        # and disabling it would remove working entities to tidy up broken ones.
        if refs and all(not _is_present(r, present) for r in refs):
            doomed.append(entry["entry_id"])
    return doomed


@dataclass
class Plan:
    """What a pre-flight would do. Deciding and doing are kept separate."""

    to_disable: list[str] = field(default_factory=list)
    details: list[tuple[str, str, str]] = field(default_factory=list)
    present_count: int = 0
    untouched_count: int = 0


def plan_preflight(storage_dir: Path, present: set[str]) -> Plan:
    """Decide what to disable. Writes nothing, so a dry run really is dry."""
    entries = json.loads((storage_dir / "core.config_entries").read_text())["data"]["entries"]
    doomed = entries_to_disable(entries, present)
    by_id = {e["entry_id"]: e for e in entries}
    return Plan(
        to_disable=doomed,
        details=[
            (
                eid,
                by_id[eid].get("domain", "?"),
                (_device_references(by_id[eid].get("data")) or ["?"])[0],
            )
            for eid in doomed
        ],
        present_count=len(present),
        untouched_count=len(entries) - len(doomed),
    )


def _stamp() -> str:
    """Local wall-clock stamp for a backup filename. Separated so it can be pinned."""
    from datetime import datetime

    return datetime.now().strftime("%Y%m%dT%H%M%S")  # noqa: DTZ005 — a filename, not a fact


def _prune_backups(storage_dir: Path, keep: int = KEEP_BACKUPS) -> None:
    """Keep the newest `keep` backups; remove the rest.

    Sorted by name, which is safe because the names are fixed-width timestamps.
    Deleting is best-effort: a backup that cannot be removed is untidy, whereas
    a promotion that aborts over it is an outage.
    """
    backups = sorted(storage_dir.glob("core.config_entries.bak-preflight-*"))
    for stale in backups[:-keep] if keep else backups:
        try:
            stale.unlink()
        except OSError:  # noqa: PERF203 — best effort, per file
            pass


def apply_plan(storage_dir: Path, plan: Plan) -> None:
    """Write the disables, after taking a timestamped backup beside the file."""
    target = storage_dir / "core.config_entries"
    payload = json.loads(target.read_text())
    (storage_dir / f"core.config_entries.bak-preflight-{_stamp()}").write_text(target.read_text())
    _prune_backups(storage_dir)

    doomed = set(plan.to_disable)
    for entry in payload["data"]["entries"]:
        if entry["entry_id"] in doomed:
            entry["disabled_by"] = "integration"
    target.write_text(json.dumps(payload, indent=2))


def _container_running(name: str) -> bool:
    """Is this container up right now?

    Separated so the guard below can be exercised without a Docker daemon. If
    docker cannot be asked, the answer is **True** — refusing to apply because
    we could not prove Home Assistant was stopped is the safe way to be wrong.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return result.stdout.strip().lower() == "true"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Disable Home Assistant config entries whose hardware is absent.",
    )
    parser.add_argument("--storage", type=Path, required=True, help="path to config/.storage")
    parser.add_argument(
        "--by-id", type=Path, default=Path("/dev/serial/by-id"), help="serial by-id directory"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the changes (default is a dry run)"
    )
    parser.add_argument(
        "--container",
        default=None,
        help="Home Assistant's container name. When given, --apply refuses while it is running.",
    )
    args = parser.parse_args(argv)

    # Editing core.config_entries under a live Home Assistant is useless at best
    # and corrupting at worst: HA holds it in memory and rewrites it on any
    # config-entry change and on shutdown, so the edit is either never read or
    # silently overwritten, with a window where both are writing. Reporting is
    # always safe; only --apply is refused.
    if args.apply and args.container and _container_running(args.container):
        sys.stderr.write(
            f"Refusing to --apply: container '{args.container}' is running (or its "
            "state could not be determined).\n"
            "Home Assistant rewrites core.config_entries from memory, so an edit "
            "made underneath it is lost or corrupting.\n"
            "Stop it first, or drop --apply to get a report.\n"
        )
        return 1

    present = present_devices(args.by_id)
    out = sys.stdout

    out.write(f"Attached serial devices: {len(present)}\n")
    for dev in sorted(present):
        out.write(f"  {attachment_of(dev).value:12} {Path(dev).name}\n")

    plan = plan_preflight(args.storage, present)
    if not plan.to_disable:
        out.write("\nEvery referenced device is present. Nothing to disable.\n")
        return 0

    out.write(f"\n{len(plan.to_disable)} config entr(ies) reference absent hardware:\n")
    for eid, domain, dev in plan.details:
        out.write(f"  {domain:14} {Path(dev).name[:52]:52} ({eid})\n")

    if not args.apply:
        out.write("\nDry run — nothing written. Re-run with --apply to disable these.\n")
        return 0

    apply_plan(args.storage, plan)
    out.write(f"\nDisabled {len(plan.to_disable)}. Backup written beside core.config_entries.\n")
    out.write("Home Assistant can now start without them failing at setup.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
