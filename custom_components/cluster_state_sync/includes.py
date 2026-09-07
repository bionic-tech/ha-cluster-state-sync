"""Work out which files a Home Assistant configuration actually needs.

The go-bag used to replicate a fixed list — `.storage`, `custom_components`,
`www`, `blueprints`, and seven top-level YAML files. That list cannot know how
anyone has chosen to split their configuration up, and on this fleet it did
not: `configuration.yaml` pulls in `configs/*.yaml`, `themes/` and `packages/`,
none of which crossed. The promoted node could not parse its own
`configuration.yaml` and came up in recovery mode — behind a promotion that had
reported success at every step, with the right identity and a working mobile
app. Green everywhere, and the house did nothing.

So this follows the includes instead of guessing. What the configuration
references is what gets replicated, and it stays right when the operator
reorganises.

**Five forms, and they nest.** `!include`, `!include_dir_list`,
`!include_dir_merge_list`, `!include_dir_merge_named`, `!include_dir_named`.
An included file may include others — `packages/` routinely does — so this
recurses, with a visited set because a configuration is a graph and nothing
stops it having a cycle.

**Includes are NOT confined to the config directory.** Home Assistant's loader
resolves them as `os.path.join(os.path.dirname(including_file), value)` with no
containment check; only `!secret` is restricted. `!include ../outside.yaml`
therefore works. Such a target cannot be replicated, so it is **reported**
rather than dropped — silently dropping references is the exact bug this
module exists to end.

**It cannot see everything, and must not pretend to.** `python_scripts/`,
`custom_templates/`, ZHA's `zigbee.db`, `known_devices.yaml`, and anything an
integration opens by path at runtime are invisible to any parser. Those need
the operator's own list. What this removes is the *guessing*, not the need to
look.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

#: Every include tag Home Assistant registers. Taken from `annotatedyaml`'s
#: loader rather than from memory: a form missing here is a file that silently
#: does not replicate, which is the failure being fixed.
INCLUDE_FILE_TAGS = ("!include",)
INCLUDE_DIR_TAGS = (
    "!include_dir_list",
    "!include_dir_merge_list",
    "!include_dir_merge_named",
    "!include_dir_named",
)

#: The entry point Home Assistant loads. Everything else is reached from here.
ROOT_CONFIG = "configuration.yaml"

#: What `!include_dir_*` picks up, matching the loader's own `_find_files`.
_DIR_PATTERN = "*.yaml"


@dataclass
class IncludeScan:
    """What a configuration references, and what could not be followed."""

    #: Paths relative to the config directory, to replicate. Files and dirs.
    paths: set[str] = field(default_factory=set)
    #: (referencing file, resolved path) that escape the config directory.
    #: Cannot be replicated; the operator has to know.
    outside: list[tuple[str, str]] = field(default_factory=list)
    #: (referencing file, resolved path) referenced but not on disk. Home
    #: Assistant would refuse to start on these, so they usually mean the scan
    #: ran against a half-written config rather than a real problem.
    missing: list[tuple[str, str]] = field(default_factory=list)
    #: Files that could not be parsed. Recorded rather than raised: one bad
    #: file must not cost the whole scan, for the same reason one bad snapshot
    #: entry does not abandon a restore (AR-0013).
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return bool(self.outside or self.missing or self.unreadable)


class _IncludeCollector(yaml.SafeLoader):
    """A loader that records include targets instead of following them.

    Home Assistant's own loader resolves includes as it goes, which needs the
    files to exist and the whole tree to be valid. This only needs to know what
    is *referenced*, so it captures the tag's value and returns a placeholder —
    a scan therefore still works on a configuration that is currently broken,
    which is precisely when someone wants to run it.
    """

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self.found: list[tuple[str, str]] = []


def _capture(tag: str):
    def constructor(loader: _IncludeCollector, node: yaml.Node) -> None:
        loader.found.append((tag, str(node.value)))
        return None

    return constructor


for _tag in INCLUDE_FILE_TAGS + INCLUDE_DIR_TAGS:
    _IncludeCollector.add_constructor(_tag, _capture(_tag))

# Every other unknown tag — `!secret`, `!env_var`, and anything a future Home
# Assistant adds — resolves to None rather than raising. A scan must not fail
# because it met a tag it does not care about.
_IncludeCollector.add_constructor(None, lambda loader, node: None)


def _within(config_dir: Path, candidate: Path) -> bool:
    """Is `candidate` inside the config directory?

    `os.path.realpath` on both sides, so a symlink pointing out of the tree is
    caught. A file that resolves outside cannot be replicated, and saying so is
    the whole point.
    """
    try:
        return os.path.commonpath([config_dir, candidate]) == str(config_dir)
    except ValueError:
        # Different drives on Windows; not our platform, but not a crash.
        return False


def scan(config_dir: str, root: str = ROOT_CONFIG) -> IncludeScan:
    """Follow every include from `root`, and report what cannot be followed."""
    base = Path(os.path.realpath(config_dir))
    result = IncludeScan()
    queue: list[Path] = [base / root]
    seen: set[Path] = set()

    while queue:
        current = queue.pop()
        if current in seen:
            # A configuration is a graph, and nothing stops it containing a
            # cycle. Without this, one would hang the publisher.
            continue
        seen.add(current)

        try:
            with current.open("r", encoding="utf-8") as handle:
                loader = _IncludeCollector(handle)
                try:
                    loader.get_single_data()
                finally:
                    loader.dispose()
        except FileNotFoundError:
            result.missing.append((_rel(base, current), str(current)))
            continue
        except (OSError, yaml.YAMLError) as err:
            result.unreadable.append((_rel(base, current), str(err)))
            continue

        for tag, value in loader.found:
            target = Path(os.path.realpath(current.parent / value))
            if not _within(base, target):
                result.outside.append((_rel(base, current), str(target)))
                continue

            result.paths.add(_rel(base, target))

            if tag in INCLUDE_FILE_TAGS:
                queue.append(target)
                continue

            # A directory include. Home Assistant walks it recursively for
            # `*.yaml`, skipping dotfiles (`_is_file_valid`), and each of those
            # files may include further.
            if not target.is_dir():
                result.missing.append((_rel(base, current), str(target)))
                continue
            for found in sorted(target.rglob(_DIR_PATTERN)):
                if any(part.startswith(".") for part in found.relative_to(target).parts):
                    continue
                queue.append(found)

    return result


def _rel(base: Path, path: Path) -> str:
    """Path relative to the config dir, or absolute when it is outside one."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
