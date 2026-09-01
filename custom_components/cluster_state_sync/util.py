"""Pure helpers with no Home Assistant or Redis dependencies.

AR-0023: `_parse_sentinel_hosts` used to live in `__init__.py`, which meant the
config flow reached it via `from .__init__ import _parse_sentinel_hosts` — a
local import inside a function, specifically to dodge a circular import. That
is the import system telling you the layering is wrong. Parsing a host list
needs neither the integration's setup module nor a backend, so it lives here
and both callers import it normally.
"""
from __future__ import annotations

from .const import DEFAULT_SENTINEL_PORT


def parse_sentinel_hosts(raw: str) -> list[tuple[str, int]]:
    """Parse a comma-separated list of host:port entries from the config flow.

    AR-0024: the port used to go through a bare `int(port)`. A typo raised an
    opaque ValueError from inside setup without saying which host was wrong,
    and an out-of-range number was accepted here only to fail later at connect
    time, where it looks like a network fault rather than a config error.
    """
    if not raw:
        return []
    out: list[tuple[str, int]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue

        # A bracketed IPv6 literal ("[fd00::1]:26379") is full of colons, so
        # rsplit alone would mangle it into host "[fd00:" and port ":1]".
        if piece.startswith("["):
            host, _, remainder = piece.partition("]")
            host = host[1:]
            port_str = remainder.lstrip(":") or str(DEFAULT_SENTINEL_PORT)
        elif ":" in piece:
            host, port_str = piece.rsplit(":", 1)
        else:
            host, port_str = piece, str(DEFAULT_SENTINEL_PORT)

        host = host.strip()
        if not host:
            raise ValueError(f"sentinel host entry {piece!r} has no hostname")

        try:
            port = int(port_str)
        except ValueError as err:
            raise ValueError(
                f"sentinel host {piece!r} has a non-numeric port {port_str!r}"
            ) from err
        if not 1 <= port <= 65535:
            raise ValueError(
                f"sentinel host {piece!r} has port {port}, outside 1-65535"
            )
        out.append((host, port))
    return out
