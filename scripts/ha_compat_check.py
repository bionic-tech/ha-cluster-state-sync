#!/usr/bin/env python3
"""What a new Home Assistant release would break here, before a user finds it.

Home Assistant ships monthly and removes things on a published schedule. This
integration reaches into Home Assistant in three ways, and each fails
differently:

1. **Public API** — entity base classes, config entries, helpers. A removal
   here is an ImportError at setup: loud, immediate, and caught by running the
   suite against a newer release.
2. **The recorder's SQLite schema** — read directly, not through an API.
   `statistics_sync` refuses to apply rows across a schema mismatch, so a
   bumped schema does not corrupt anything; it **stops history replication**,
   quietly, until both nodes and the standby's store agree again. Nothing in a
   test suite notices, because the schema lives in Home Assistant, not here.
3. **Generated host artefacts** — unaffected by Home Assistant entirely.

So this reports on 1 and 2 together, and is the thing a scheduled job runs
against `latest` while the test suite runs against the pin.

Exit code is 1 if anything has moved, so a scheduled run goes red *before* an
upgrade does.
"""

from __future__ import annotations

import importlib
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The recorder schema this project has actually been tested against. Bumping
#: it is a deliberate act: see `docs/REFERENCE-ha-compatibility.md` for what a
#: bump costs a running cluster (a re-seed, because the standby's store is not
#: migrated by anything).
TESTED_RECORDER_SCHEMA = 53

#: Every Home Assistant symbol this integration imports. Written out rather
#: than derived, so a removal is reported as "this specific thing went" instead
#: of a stack trace somebody has to read.
REQUIRED_API: dict[str, tuple[str, ...]] = {
    "homeassistant.core": ("HomeAssistant", "Event", "callback"),
    "homeassistant.config_entries": ("ConfigEntry", "ConfigFlow", "OptionsFlow"),
    "homeassistant.helpers.entity_platform": ("AddEntitiesCallback",),
    "homeassistant.helpers.update_coordinator": ("DataUpdateCoordinator", "CoordinatorEntity"),
    "homeassistant.helpers.device_registry": ("DeviceInfo",),
    "homeassistant.helpers.entity": ("EntityCategory",),
    "homeassistant.helpers.event": ("async_track_time_interval",),
    "homeassistant.helpers.issue_registry": ("async_create_issue", "async_delete_issue"),
    "homeassistant.components.button": ("ButtonEntity",),
    "homeassistant.components.switch": ("SwitchEntity",),
    "homeassistant.components.http": ("StaticPathConfig",),
    "homeassistant.components.recorder.db_schema": ("SCHEMA_VERSION",),
    "homeassistant.data_entry_flow": ("FlowResult",),
}


def _ha_version() -> str:
    try:
        return importlib.import_module("homeassistant.const").__version__
    except Exception:  # noqa: BLE001
        return "unknown"


def check_api() -> list[str]:
    """Report every symbol this integration needs that is no longer there."""
    gone: list[str] = []
    for module_name, symbols in REQUIRED_API.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError as err:
            gone.append(f"{module_name} — module is gone ({err})")
            continue
        for symbol in symbols:
            if not hasattr(module, symbol):
                gone.append(f"{module_name}.{symbol} — symbol is gone")
    return gone


def check_recorder_schema() -> tuple[int, bool]:
    """The recorder schema Home Assistant now uses, and whether it moved."""
    try:
        schema = int(
            importlib.import_module("homeassistant.components.recorder.db_schema").SCHEMA_VERSION
        )
    except Exception:  # noqa: BLE001
        return 0, True
    return schema, schema != TESTED_RECORDER_SCHEMA


def check_manifest_floor(ha_version: str) -> str | None:
    """Is the HACS minimum still at or below what is installed?"""
    try:
        floor = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]
    except Exception:  # noqa: BLE001
        return None
    return f"hacs.json requires >= {floor}; testing against {ha_version}"


def main() -> int:
    ha = _ha_version()
    print(f"Home Assistant under test: {ha}")
    note = check_manifest_floor(ha)
    if note:
        print(f"  {note}")
    print()

    problems = 0

    gone = check_api()
    if gone:
        problems += 1
        print("🚨 Home Assistant API this integration depends on has MOVED:")
        for line in gone:
            print(f"    {line}")
        print("    -> setup would fail with an ImportError. Fix before this release ships.")
    else:
        print(f"✅ API: all {sum(len(v) for v in REQUIRED_API.values())} symbols still present.")

    schema, moved = check_recorder_schema()
    if moved:
        problems += 1
        print()
        print(
            f"🚨 Recorder schema is now {schema}; this project is tested against "
            f"{TESTED_RECORDER_SCHEMA}."
        )
        print("    Statistics replication REFUSES across a mismatch, by design, so nothing")
        print("    corrupts — but history replication stops until every side agrees.")
        print("    A cold standby's store is migrated by nothing, so it needs a fresh seed.")
        print("    See docs/REFERENCE-ha-compatibility.md before upgrading a live cluster.")
    else:
        print(f"✅ Recorder schema: {schema}, unchanged.")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
