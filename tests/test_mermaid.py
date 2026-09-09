"""Every mermaid diagram we ship must actually render.

There is no JavaScript runtime in CI (the same constraint that produced
`_parse_like_the_panel` in `test_panel.py`), so this cannot run mermaid itself.
What it can do is catch the mistakes that are actually made by hand — an
unclosed `subgraph`, an unbalanced quote, a diagram with no declared type — all
of which render as a bare error box on GitHub and in Home Assistant's markdown
cards.

A broken diagram in a guide is worse than no diagram: it is the first thing a
new reader sees, and it says the documentation is not maintained.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Diagram types this project uses. A block whose first line is not one of
#: these is either a typo or a kind nobody has reviewed for rendering.
KNOWN_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "classDiagram",
    "gantt",
    "pie",
    "journey",
    "timeline",
    "mindmap",
    "C4Context",
)

#: Only what ships. The design-template library is third-party material and
#: not ours to lint.
SEARCH_ROOTS = ("README.md", "info.md", "docs")
SKIP_PARTS = ("design-templates", "archive", "superpowers", "plans")


def _blocks() -> list[tuple[pathlib.Path, int, str]]:
    """Every fenced mermaid block we ship: (file, line number, body)."""
    found: list[tuple[pathlib.Path, int, str]] = []
    paths: list[pathlib.Path] = []
    for root in SEARCH_ROOTS:
        target = REPO / root
        if target.is_file():
            paths.append(target)
        elif target.is_dir():
            paths.extend(target.rglob("*.md"))
    for path in sorted(paths):
        if any(part in path.parts for part in SKIP_PARTS):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        inside, start, buf = False, 0, []
        for n, line in enumerate(lines, 1):
            if not inside and line.strip() == "```mermaid":
                inside, start, buf = True, n, []
            elif inside and line.strip() == "```":
                found.append((path, start, "\n".join(buf)))
                inside = False
            elif inside:
                buf.append(line)
        assert not inside, f"{path}: unterminated ```mermaid block opened at line {start}"
    return found


def test_there_are_diagrams_to_check() -> None:
    """A validator that silently checks nothing is worse than none."""
    assert len(_blocks()) >= 6, "expected the shipped docs to carry mermaid diagrams"


@pytest.mark.parametrize(
    ("path", "line", "body"),
    [(p, n, b) for p, n, b in _blocks()],
    ids=[f"{p.relative_to(REPO)}:{n}" for p, n, _ in _blocks()],
)
def test_each_diagram_is_structurally_valid(path: pathlib.Path, line: int, body: str) -> None:
    where = f"{path.relative_to(REPO)}:{line}"
    stripped = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
    assert stripped, f"{where}: empty mermaid block"

    first = stripped[0].strip()
    assert any(first.startswith(t) for t in KNOWN_TYPES), (
        f"{where}: first line {first!r} does not declare a known diagram type"
    )

    # `subgraph` must be closed. An unclosed one renders as an error box.
    opens = sum(1 for ln in stripped if re.match(r"^\s*subgraph\b", ln))
    ends = sum(1 for ln in stripped if re.match(r"^\s*end\s*$", ln))
    assert opens == ends, f"{where}: {opens} subgraph(s) but {ends} end(s)"

    for n, ln in enumerate(stripped, 1):
        # Quotes inside node labels must balance on their own line.
        assert ln.count('"') % 2 == 0, f"{where} (+{n}): odd number of quotes: {ln.strip()!r}"
        # Brackets that open a node label must close on the same line.
        for opener, closer in (("[", "]"), ("(", ")"), ("{", "}")):
            assert ln.count(opener) == ln.count(closer), (
                f"{where} (+{n}): unbalanced {opener}{closer}: {ln.strip()!r}"
            )


def test_no_diagram_uses_a_bare_hyphen_arrow_in_a_flowchart() -> None:
    """`-->` is an arrow; `->` is a common typo that renders as an error.

    Sequence diagrams legitimately use `->>` and `-->>`, so this only applies
    to flowcharts and graphs.
    """
    for path, line, body in _blocks():
        first = next(ln.strip() for ln in body.splitlines() if ln.strip())
        if not first.startswith(("flowchart", "graph")):
            continue
        for n, ln in enumerate(body.splitlines(), 1):
            bad = re.search(r"(?<![-.=>])->(?!>)", ln)
            assert not bad, (
                f"{path.relative_to(REPO)}:{line} (+{n}): `->` should be `-->`: {ln.strip()!r}"
            )
