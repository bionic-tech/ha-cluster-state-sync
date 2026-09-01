"""Tests for the promotion pre-flight device probe.

A standby that starts with radios missing is useful; a standby that refuses to
start because three config entries cannot find their hardware is not. This
probe runs *before* Home Assistant on the promoted node, works out which
devices are actually present, and disables the config entries that reference
the ones that are not.

The classification comes from sysfs, not from a maintained list. Verified on
node-a on 2026-08-26 — every USB serial device resolves to one of two
shapes:

    /sys/.../pci0000:00/…/usb5/5-1/5-1.3      real PCI USB controller
    /sys/.../platform/vhci_hcd.0/usb9/…       VirtualHere (USB-over-IP)

That distinction is the whole trick. A list of "devices the standby does not
have" is a second copy of the truth and rots the first time a radio moves; the
kernel already knows, and it is never out of date.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.cluster_state_sync.scripts.ha_device_preflight import (
    Attachment,
    classify_attachment,
    entries_to_disable,
    main,
    plan_preflight,
)

# --------------------------------------------------------------------------
# classify_attachment — the sysfs discriminator
# --------------------------------------------------------------------------


def test_a_pci_path_is_physical() -> None:
    """Real hardware in this chassis. It cannot follow a failover."""
    path = "/sys/devices/pci0000:00/0000:00:14.0/usb5/5-1/5-1.3/5-1.3:1.0/ttyUSB0"

    assert classify_attachment(path) is Attachment.PHYSICAL


def test_a_vhci_path_is_network_delivered() -> None:
    """VirtualHere. It can follow, provided the standby claims it."""
    path = "/sys/devices/platform/vhci_hcd.0/usb9/9-1/9-1:1.0/ttyUSB2"

    assert classify_attachment(path) is Attachment.VIRTUALHERE


def test_an_unrecognised_path_is_not_guessed_at() -> None:
    """Unknown is its own answer.

    Calling something physical because we did not recognise it would silently
    disable a device that was merely attached in a way we had not seen.
    """
    assert classify_attachment("/sys/devices/soc0/weird/ttyS0") is Attachment.UNKNOWN


def test_a_missing_path_is_unknown_rather_than_an_error() -> None:
    assert classify_attachment(None) is Attachment.UNKNOWN


# --------------------------------------------------------------------------
# entries_to_disable — matching config entries against what is present
# --------------------------------------------------------------------------


def _entry(entry_id: str, domain: str, device: str, disabled: str | None = None) -> dict:
    return {
        "entry_id": entry_id,
        "domain": domain,
        "title": f"{domain} {entry_id}",
        "data": {"device": device},
        "disabled_by": disabled,
    }


def test_an_entry_whose_device_is_absent_is_selected() -> None:
    """The local firewall RFXtrx on node-a is exactly this case."""
    entries = [_entry("a", "rfxtrx", "/dev/serial/by-id/usb-RFXCOM_RFXtrx433_07VYAMCJ-if00-port0")]

    assert entries_to_disable(entries, present=set()) == ["a"]


def test_an_entry_whose_device_is_present_is_left_alone() -> None:
    dev = "/dev/serial/by-id/usb-RFXCOM_RFXtrx433_A1K33YZ-if00-port0"
    entries = [_entry("a", "rfxtrx", dev)]

    assert entries_to_disable(entries, present={dev}) == []


def test_an_already_disabled_entry_is_not_selected_again() -> None:
    """Idempotence. Promotion may run twice; the second run must be a no-op."""
    entries = [_entry("a", "rfxtrx", "/dev/serial/by-id/gone", disabled="user")]

    assert entries_to_disable(entries, present=set()) == []


def test_entries_with_no_device_reference_are_ignored() -> None:
    """Most of 160 config entries are network or cloud and own no hardware."""
    entries = [
        {"entry_id": "net", "domain": "hue", "data": {"host": "10.0.0.5"}, "disabled_by": None},
        {"entry_id": "cloud", "domain": "met", "data": {}, "disabled_by": None},
    ]

    assert entries_to_disable(entries, present=set()) == []


def test_a_device_reference_under_a_different_key_is_still_found() -> None:
    """Integrations disagree on the key: device, port, usb_path, device_path."""
    entries = [
        {
            "entry_id": "z",
            "domain": "zwave_js",
            "data": {"usb_path": "/dev/serial/by-id/usb-Some_Stick-if00"},
            "disabled_by": None,
        }
    ]

    assert entries_to_disable(entries, present=set()) == ["z"]


def test_a_network_url_is_never_treated_as_a_missing_device() -> None:
    """`socket://` and `http://` are not local hardware and must not be disabled.

    A dlna entry on node-a carries a URL containing `/dev/` in its path,
    which a naive substring match would disable.
    """
    entries = [
        {
            "entry_id": "dlna",
            "domain": "dlna_dmr",
            "data": {"url": "http://192.168.1.245:45136/dev/585e8255-e43b/desc.xml"},
            "disabled_by": None,
        },
        {
            "entry_id": "sock",
            "domain": "zha",
            "data": {"device": {"path": "socket://10.0.0.9:6638"}},
            "disabled_by": None,
        },
    ]

    assert entries_to_disable(entries, present=set()) == []


# --------------------------------------------------------------------------
# plan_preflight — the whole decision, without writing anything
# --------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    (tmp_path / "core.config_entries").write_text(
        json.dumps(
            {
                "data": {
                    "entries": [
                        _entry("local", "rfxtrx", "/dev/serial/by-id/usb-RFXCOM_07VYAMCJ-if00"),
                        _entry("office", "rfxtrx", "/dev/serial/by-id/usb-RFXCOM_A1K33YZ-if00"),
                        {
                            "entry_id": "hue",
                            "domain": "hue",
                            "data": {"host": "10.0.0.5"},
                            "disabled_by": None,
                        },
                    ]
                }
            }
        )
    )
    return tmp_path


def test_the_plan_names_what_it_would_disable_and_why(storage: Path) -> None:
    plan = plan_preflight(storage, present={"/dev/serial/by-id/usb-RFXCOM_A1K33YZ-if00"})

    assert plan.to_disable == ["local"]
    assert plan.present_count == 1
    assert plan.untouched_count == 2


def test_the_plan_writes_nothing_by_itself(storage: Path) -> None:
    """Planning and applying are separate, so a dry run is genuinely dry."""
    before = (storage / "core.config_entries").read_text()

    plan_preflight(storage, present=set())

    assert (storage / "core.config_entries").read_text() == before


def test_everything_present_produces_an_empty_plan(storage: Path) -> None:
    """The blissful case: nothing to do, and it says so."""
    plan = plan_preflight(
        storage,
        present={
            "/dev/serial/by-id/usb-RFXCOM_07VYAMCJ-if00",
            "/dev/serial/by-id/usb-RFXCOM_A1K33YZ-if00",
        },
    )

    assert plan.to_disable == []


# --------------------------------------------------------------------------
# Path equivalence — the same device named two different ways
# --------------------------------------------------------------------------


def test_a_device_matches_however_the_config_names_it() -> None:
    """`/dev/serial/by-id/…` and `/dev/ttyUSB2` can be the same radio.

    Home Assistant stores whichever path the user picked at setup. Comparing
    raw strings would disable a device that is plainly attached, just named the
    other way — the worst possible outcome, since it looks like the probe
    working correctly.
    """
    by_id = "/dev/serial/by-id/usb-RFXCOM_RFXtrx433_A1K33YZ-if00-port0"
    node = "/dev/ttyUSB3"
    present = {by_id, node}

    assert entries_to_disable([_entry("a", "rfxtrx", by_id)], present) == []
    assert entries_to_disable([_entry("b", "rfxtrx", node)], present) == []


def test_a_device_found_under_a_different_directory_still_matches() -> None:
    """The --by-id override must not change the verdict.

    Scanning a copy of the directory is how the standby case gets rehearsed
    before a real promotion; if that changed the answer the rehearsal would be
    worthless.
    """
    present = {"/tmp/probe/usb-RFXCOM_RFXtrx433_A1K33YZ-if00-port0"}
    entry = _entry("a", "rfxtrx", "/dev/serial/by-id/usb-RFXCOM_RFXtrx433_A1K33YZ-if00-port0")

    assert entries_to_disable([entry], present) == []


def test_an_entry_keeps_working_devices_when_only_some_are_missing() -> None:
    """`all`, not `any`.

    An entry naming several devices is disabled only when *every* one is gone.
    Disabling it because one of three is missing would remove working entities
    in order to tidy up broken ones — a worse outcome than the mess.
    """
    here = "/dev/serial/by-id/usb-present-if00"
    gone = "/dev/serial/by-id/usb-absent-if00"
    entry = {
        "entry_id": "multi",
        "domain": "somedomain",
        "data": {"device": here, "port": gone},
        "disabled_by": None,
    }

    assert entries_to_disable([entry], present={here}) == []


def test_an_entry_is_disabled_only_when_every_device_is_gone() -> None:
    entry = {
        "entry_id": "multi",
        "domain": "somedomain",
        "data": {"device": "/dev/serial/by-id/usb-a-if00", "port": "/dev/serial/by-id/usb-b-if00"},
        "disabled_by": None,
    }

    assert entries_to_disable([entry], present=set()) == ["multi"]


# --------------------------------------------------------------------------
# The command line — what actually runs during a promotion
# --------------------------------------------------------------------------


def _by_id_dir(tmp_path: Path, *names: str) -> Path:
    d = tmp_path / "by-id"
    d.mkdir()
    for n in names:
        (d / n).write_text("")  # a plain file stands in for the symlink
    return d


def test_a_dry_run_reports_and_changes_nothing(
    storage: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default must be safe: promotion scripts get run by accident."""
    before = (storage / "core.config_entries").read_text()

    rc = main(["--storage", str(storage), "--by-id", str(_by_id_dir(tmp_path))])

    assert rc == 0
    assert "Dry run" in capsys.readouterr().out
    assert (storage / "core.config_entries").read_text() == before


def test_apply_disables_the_absent_entries_and_backs_up_first(
    storage: Path, tmp_path: Path
) -> None:
    """A promotion edits config entries. It does not get to do that unbacked."""
    main(["--storage", str(storage), "--by-id", str(_by_id_dir(tmp_path)), "--apply"])

    payload = json.loads((storage / "core.config_entries").read_text())
    disabled = [e for e in payload["data"]["entries"] if e.get("disabled_by")]
    assert {e["entry_id"] for e in disabled} == {"local", "office"}
    assert list(storage.glob("core.config_entries.bak-preflight-*"))


def test_applying_twice_is_a_no_op_the_second_time(
    storage: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keepalived can fire notify_master more than once."""
    argv = ["--storage", str(storage), "--by-id", str(_by_id_dir(tmp_path)), "--apply"]
    main(argv)
    capsys.readouterr()

    rc = main(argv)

    assert rc == 0
    assert "Nothing to disable" in capsys.readouterr().out


def test_it_says_so_when_every_device_is_present(
    storage: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The happy path has to be legible in a log read after the fact."""
    present = _by_id_dir(tmp_path, "usb-RFXCOM_07VYAMCJ-if00", "usb-RFXCOM_A1K33YZ-if00")

    rc = main(["--storage", str(storage), "--by-id", str(present)])

    assert rc == 0
    assert "Nothing to disable" in capsys.readouterr().out


def test_a_missing_by_id_directory_is_not_an_error(storage: Path, tmp_path: Path) -> None:
    """A host with no serial devices at all still has to promote."""
    rc = main(["--storage", str(storage), "--by-id", str(tmp_path / "nope")])

    assert rc == 0


# --------------------------------------------------------------------------
# Applying under a live Home Assistant — adversarial review finding, 2026-08-27
# --------------------------------------------------------------------------


def test_apply_refuses_when_home_assistant_is_running(
    storage: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """Editing .storage under a live HA is useless at best and corrupting at worst.

    Home Assistant holds core.config_entries in memory and rewrites it on any
    config-entry change and on shutdown. An edit made underneath it is either
    never read or silently overwritten — and there is a window where both are
    writing. The warm promotion path had exactly this shape: no `docker start`,
    because the container is already up.

    The script refuses rather than trusting the caller, because the caller that
    got it wrong was the one this repository generates.
    """
    import custom_components.cluster_state_sync.scripts.ha_device_preflight as m

    monkeypatch.setattr(m, "_container_running", lambda name: True)
    before = (storage / "core.config_entries").read_text()

    rc = main(
        [
            "--storage",
            str(storage),
            "--by-id",
            str(_by_id_dir(tmp_path)),
            "--container",
            "homeassistant",
            "--apply",
        ]
    )

    assert rc == 1
    assert "running" in capsys.readouterr().err.lower()
    assert (storage / "core.config_entries").read_text() == before


def test_apply_proceeds_when_the_container_is_stopped(
    storage: Path, tmp_path: Path, monkeypatch
) -> None:
    import custom_components.cluster_state_sync.scripts.ha_device_preflight as m

    monkeypatch.setattr(m, "_container_running", lambda name: False)

    rc = main(
        [
            "--storage",
            str(storage),
            "--by-id",
            str(_by_id_dir(tmp_path)),
            "--container",
            "homeassistant",
            "--apply",
        ]
    )

    assert rc == 0
    payload = json.loads((storage / "core.config_entries").read_text())
    assert any(e.get("disabled_by") for e in payload["data"]["entries"])


def test_a_dry_run_is_allowed_even_while_home_assistant_runs(
    storage: Path, tmp_path: Path, monkeypatch
) -> None:
    """Reporting is always safe, and is what the warm path should do."""
    import custom_components.cluster_state_sync.scripts.ha_device_preflight as m

    monkeypatch.setattr(m, "_container_running", lambda name: True)

    assert (
        main(
            [
                "--storage",
                str(storage),
                "--by-id",
                str(_by_id_dir(tmp_path)),
                "--container",
                "homeassistant",
            ]
        )
        == 0
    )


def test_backups_do_not_accumulate_without_bound(
    storage: Path, tmp_path: Path, monkeypatch
) -> None:
    """Adversarial review 2026-08-27, finding 6.

    Every `--apply` writes a timestamped backup into `.storage` — which, when
    fileset replication is enabled, is the directory it mirrors wholesale to
    the standby. Left unbounded, every promotion adds a file to something that
    gets copied across the network for the life of the cluster.

    Keep enough to recover from a bad promotion; do not keep a museum.
    """
    import custom_components.cluster_state_sync.scripts.ha_device_preflight as m

    monkeypatch.setattr(m, "_container_running", lambda name: False)
    argv = [
        "--storage",
        str(storage),
        "--by-id",
        str(_by_id_dir(tmp_path)),
        "--container",
        "ha",
        "--apply",
    ]

    for i in range(8):
        # Re-arm: undo the disables so each pass has something to write.
        payload = json.loads((storage / "core.config_entries").read_text())
        for e in payload["data"]["entries"]:
            e["disabled_by"] = None
        (storage / "core.config_entries").write_text(json.dumps(payload))
        monkeypatch.setattr(m, "_stamp", lambda i=i: f"2026010{i}T000000")
        main(argv)

    backups = sorted(storage.glob("core.config_entries.bak-preflight-*"))
    assert len(backups) <= m.KEEP_BACKUPS, f"{len(backups)} backups kept"
    assert backups, "pruning must not remove every backup"
