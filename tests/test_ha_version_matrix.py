"""The list of Home Assistant releases this integration promises to support.

`hacs.json` declares a minimum, which is a promise about every release above it.
Home Assistant ships monthly, so that promise grows by one untested version a
month unless something derives the list rather than hardcoding it.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import ha_version_matrix as m  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent


def _releases(*versions: str) -> str:
    """A PyPI payload with the given versions, all installable."""
    return json.dumps({"releases": {v: [{"yanked": False}] for v in versions}})


def test_it_takes_the_latest_patch_of_every_minor() -> None:
    """One row per minor. Testing 2026.7.0 when 2026.7.4 exists tests nobody."""
    got = m.matrix(
        (2026, 6, 4),
        m.released(lambda: _releases("2026.6.4", "2026.7.0", "2026.7.4", "2026.8.3")),
    )
    assert got == ["2026.6.4", "2026.7.4", "2026.8.3"]


def test_the_floors_own_patch_level_is_respected() -> None:
    """🚨 A floor of 2026.6.4 does not promise 2026.6.3.

    Taking the newest patch of the floor's minor without this check would put
    2026.6.3 in the matrix and then fail on it — a supported-version failure
    for a version that was never supported.
    """
    got = m.matrix(
        (2026, 6, 4),
        m.released(lambda: _releases("2026.6.0", "2026.6.3", "2026.6.4", "2026.7.1")),
    )
    assert "2026.6.3" not in got
    assert got == ["2026.6.4", "2026.7.1"]


def test_older_majors_are_excluded() -> None:
    got = m.matrix((2026, 6, 4), m.released(lambda: _releases("2025.12.5", "2026.5.4", "2026.6.4")))
    assert got == ["2026.6.4"]


def test_a_yanked_release_is_not_a_supported_release() -> None:
    """Nobody can be running a version pip refuses to install."""
    payload = json.dumps(
        {
            "releases": {
                "2026.6.4": [{"yanked": False}],
                "2026.7.0": [{"yanked": True}],
                "2026.7.1": [{"yanked": False}],
            }
        }
    )
    got = m.matrix((2026, 6, 4), m.released(lambda: payload))
    assert got == ["2026.6.4", "2026.7.1"]


def test_a_release_with_no_files_is_skipped() -> None:
    payload = json.dumps({"releases": {"2026.6.4": [{"yanked": False}], "2026.7.0": []}})
    assert m.matrix((2026, 6, 4), m.released(lambda: payload)) == ["2026.6.4"]


def test_the_floor_comes_from_hacs_json() -> None:
    """Not hardcoded here — the promise lives in one place."""
    declared = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]
    assert ".".join(str(p) for p in m.floor_version()) == declared


def test_the_declared_floor_is_the_version_the_suite_pins() -> None:
    """🚨 If these drift, the matrix tests a range the pin does not anchor.

    `hacs.json` says what users may run; `requirements-dev.txt` says what CI
    proves. The floor has to be a version the suite actually runs against, or
    the lowest supported release is the one nobody ever tests.
    """
    import homeassistant.const as const

    assert (
        const.__version__
        == json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]
    ), (
        "the installed Home Assistant is not the version hacs.json declares as the "
        "minimum — either the pin moved without the floor, or the reverse"
    )
