"""The cluster lease, defined once (ADR-003, AR-0017).

This is the split-brain guard. It is take-or-renew **keyed on identity**: a
caller presenting the node id that already holds the lease renews it; any other
caller is refused until the TTL lapses.

It lives here, in a module that imports nothing outside the standard library,
because two things need it and only one of them can import Home Assistant. The
integration uses it from `backend.py`; the host-side promoter uses it from a
copy the bundle ships, the same way `crypto.py` is shipped.

**Never reimplement this.** Two subtly different take-or-renew implementations
racing on one key is precisely how a cluster ends up with two leaders, and this
project has already spent a day on an identity collision that did exactly that.
"""

from __future__ import annotations

from typing import Final

LEASE_SCRIPT: Final[str] = """
local current = redis.call('GET', KEYS[1])
if current == false then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
    return 1
elseif current == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
end
return 0
"""

#: The manual override's claim (design decision D2), never the identity check
#: above. `force-master` exists for when an operator has confirmed the peer is
#: genuinely dead, so it must not merely skip consulting the lease -- it must
#: also overwrite it. A promotion that forced its way past the check but left
#: the old key standing leaves a healthy or repaired peer free to legitimately
#: take that key later: two nodes, each holding the lease by a different
#: mechanism. `backend.py`'s `release_leadership` also touches this key --
#: an identity-checked DEL on clean shutdown, a release rather than a claim --
#: so this is not the only other code that writes it, but it is the only other
#: way the key is ever *claimed*. Keep it here, beside LEASE_SCRIPT, so there
#: is exactly one module that defines what claiming this key means.
FORCE_SCRIPT: Final[str] = """
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return 1
"""

#: The identity-checked release (design D3), previously inlined separately in
#: `backend.py`'s `release_leadership` -- moved here for the same reason
#: LEASE_SCRIPT and FORCE_SCRIPT are: a second hand-copied definition of what
#: giving up this key means is exactly how the two drift apart. Deletes only
#: a lease this caller actually holds, so a late release from a node that has
#: since lost the lease -- to a clean shutdown racing a promotion, or to the
#: promoter's own D3 probe releasing first -- can never evict whoever holds
#: it now.
RELEASE_SCRIPT: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def lease_ttl_ms(ttl_seconds: int) -> str:
    """Redis `PX` takes milliseconds.

    Passing seconds would give a 30 millisecond lease, so every node would find
    it free on every pass and every node would believe it leads.
    """
    return str(int(ttl_seconds) * 1000)
