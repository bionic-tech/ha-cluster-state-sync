"""Asking the one question none of the cluster's other probes ask: can we get in?

AR-0060, and it is worth stating the incident plainly because the shape of it
decides the shape of this module. A failover on the reference pair succeeded
*completely*: the lease moved, the radios followed, Home Assistant came up
healthy on the surviving node and ran the house. And `home.<domain>` still
resolved to the machine that had died, and returned 502. Every check the
cluster performed passed, because every check the cluster performed looked
inwards -- at Valkey, at the go-bag, at the lease, at its own entities. Nobody
could open the app.

**The infrastructure half of that is not ours and this module does not pretend
otherwise.** DNS, reverse proxies, tunnels, floating addresses, VPNs: those are
the operator's network, they are documented in `GUIDE-ingress.md` under an
explicit no-warranty notice, and nothing here will move any of them. What was
missing was smaller and entirely ours: *the cluster never looked*. This makes
it look, once a minute, from the node that is supposed to be answering, and
publish what it saw.

## 🚨 What a green reading does NOT prove

Read this before trusting it, and it is repeated verbatim in the options page
and in `GUIDE-ingress.md` because a health check that is believed further than
it can see is worse than none at all -- it is AR-0060 with a tick next to it.

* **It does not prove an outside user can reach you.** The request leaves the
  Home Assistant container, on the node itself, inside your LAN. If you run
  split-horizon DNS -- and most people who put Home Assistant behind a tunnel
  do -- `home.example.com` resolves to a local address from in here and to a
  tunnel endpoint from your phone on mobile data. The probe then tests a path
  no external user ever takes, and is green while the tunnel is down. There is
  no way to fix this from inside the network being tested; the only honest
  answer is to say so, and to point at an external uptime monitor for the half
  we cannot see.
* **It does not prove that *this node* answered.** A 200 means something at
  that address served a response. In a warm-standby pair it may have been the
  peer; behind a proxy it may have been a cached error page with a cheerful
  status. What it rules out is the AR-0060 failure specifically -- an address
  answering with 502, a refused connection, or nothing at all -- which is worth
  ruling out even though it is not everything.
* **It does not prove you can log in.** Redirects are deliberately not
  followed (see `async_probe`), so an SSO challenge counts as the front door
  working. A door that is answering and an identity provider that will let you
  through are different facts, and this one measures the first.

A red reading is the more trustworthy direction: if the probe cannot reach the
address from a machine on the same network as the service, an external user
almost certainly cannot either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

#: Seconds between probes, on the leader only.
#:
#: Not configurable, deliberately. One request a minute against your own front
#: door is nothing; the reasons to make it a setting are all bad ones -- a
#: shorter interval invites somebody to hammer a tunnel provider that rate-
#: limits them, and a longer one delays the only alarm that would have told
#: them about AR-0060. Sixty seconds sits inside the 2.5-minute failover
#: budget, which is the number this whole project is built around.
INGRESS_PROBE_INTERVAL = 60.0

#: How long to wait for an answer. Comfortably under the interval, so a hung
#: front door can never leave two probes in flight at once, and generous
#: enough that a cold cache or a sleepy tunnel is not reported as an outage.
INGRESS_PROBE_TIMEOUT = 10.0

#: Consecutive failures before anybody is interrupted.
#:
#: The binary sensor moves on the FIRST failure -- the reading stays exact, the
#: way the repair cards do -- and only the push waits. A single missed probe is
#: a blip: a proxy reloading its config, a tunnel reconnecting, a DHCP lease
#: renewing. Three in a row at a minute apart is a front door, and three
#: minutes is still well inside the window in which somebody would otherwise
#: have discovered it by failing to open the app.
INGRESS_FAILURES_BEFORE_ALARM = 3

#: Statuses that mean "the door answered" even though they are errors.
#:
#: An SSO challenge or a basic-auth prompt is the ingress path doing its job:
#: we are unauthenticated, we should be refused, and being refused proves
#: something is there to refuse us. Treating these as unreachable would report
#: every properly-protected front door as broken -- which is exactly the
#: cry-wolf alarm this project refuses to ship.
INGRESS_AUTH_STATUSES = frozenset({401, 403})

#: Named so a proxy's access log identifies us, because AR-0063 was a failover
#: nobody could reconstruct afterwards for want of a log line.
INGRESS_USER_AGENT = "HomeAssistant-ClusterStateSync/ingress-probe"

#: Why the probe did not run, when it did not.
SKIP_NOT_LEADER = "not_leader"
SKIP_NEVER_RUN = "never_run"


class InvalidIngressURL(ValueError):
    """The configured front-door address cannot be probed.

    A subclass rather than a bare `ValueError` so the config flow can catch
    exactly this and turn it into a field error, without also swallowing a
    `ValueError` raised for some unrelated reason by something underneath.
    """


def validate_ingress_url(raw: str | None) -> str | None:
    """Return a probeable URL, `None` for "not configured", or raise.

    Empty is the normal case and is not an error: the feature is off by
    default and an operator who never opens the page must not be told they
    have misconfigured anything.

    The rules are narrow on purpose. Only `http` and `https` -- a scheme this
    integration cannot fetch, or one that reads a local file, is not a front
    door -- and there must be a host, because `https:///` parses cleanly and
    then fails at request time with a message about nothing in particular.
    Catching it here means the operator is told on the page where they typed
    it, rather than discovering it in a log they were never going to read.
    """
    if raw is None:
        return None
    url = raw.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise InvalidIngressURL(
            f"{url!r} is not an http(s) address -- include the scheme, as in "
            "https://home.example.com"
        )
    if not parts.hostname:
        raise InvalidIngressURL(f"{url!r} has no host in it")
    return url


def redact_credentials(url: str) -> str:
    """Strip any `user:password@` from a URL before it is ever displayed.

    🚨 This is not decoration. A front door behind basic auth is commonly
    written `https://me:hunter2@home.example.com`, and everything this module
    publishes is read by anybody with a Home Assistant login: an entity
    attribute is visible in the UI, kept in the recorder database, included in
    a diagnostics download, and -- through the alert router -- pushed to a
    phone through a third-party notify service. A password that arrives in a
    Discord channel because a proxy returned 502 is a real outcome, and the
    only place to prevent it is here, at the single point where the URL turns
    into text somebody reads.

    The probe itself keeps the credentials; only what is shown loses them.
    """
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class IngressResult:
    """One look at the front door, including the looks that did not happen.

    `reachable` is deliberately three-valued. `None` is not a polite `False`:
    "nobody has asked yet" and "we asked and got a 502" are opposite facts, and
    a follower reporting `False` because it never probes would be an alarm
    about the peer's ingress raised by the node least able to judge it.
    """

    reachable: bool | None = None
    status: int | None = None
    error: str | None = None
    latency_ms: float | None = None
    checked_at: datetime | None = None
    #: Set only when `reachable` is None, and says which kind of "unknown".
    skipped_reason: str | None = SKIP_NEVER_RUN


def is_acceptable_status(status: int) -> bool:
    """Did the front door answer like a front door?

    Anything below 400 is the door working, including the 301/302 a proxy
    issues to upgrade to HTTPS or to hand a request to an identity provider.
    401 and 403 are the door working too -- see `INGRESS_AUTH_STATUSES`.

    Everything else is a fault worth reporting, and that includes 404. A 404 at
    the address a person types is a routing rule pointing somewhere that no
    longer exists, which is AR-0060's failure wearing a different number: the
    house is fine and you still cannot get in.
    """
    return status < 400 or status in INGRESS_AUTH_STATUSES


class IngressProbe:
    """Requests the operator's own front door and records what happened.

    Leader-only by construction: it does not consult leadership itself -- the
    scheduler in `__init__.py` does, exactly as it does for the flush, the
    recorder snapshot and the statistics publish -- but it offers
    `async_note_not_leader` so a follower's reading becomes an honest "not
    checked" instead of the leader's last answer going quietly stale.

    **Nothing here raises.** A probe is a reading to publish, never an
    exception that reaches the timer that called it. That is the same posture
    `backend.py` takes towards Valkey and for the same reason: an integration
    whose job is to keep the house running must not be able to stop it over a
    diagnostic.
    """

    def __init__(self, hass: HomeAssistant, url: str, *, verify_tls: bool = True) -> None:
        self._hass = hass
        self._url = url
        #: What anybody is allowed to see. See `redact_credentials`.
        self.display_url = redact_credentials(url)
        self._verify_tls = verify_tls
        self._listeners: list[Callable[[], None]] = []
        self.last_result = IngressResult()
        #: Failures since the last success, so the alarm can wait for a
        #: pattern while the entity reports each individual look.
        self.consecutive_failures = 0

    # -- subscription ------------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to probe results; returns an unsubscribe callable.

        Push rather than poll, matching `StateMirror.add_listener` and the
        integration's `local_push` iot class: the entity writes state when the
        probe has something new to say, not on a timer of its own that could
        drift out of step with this one.
        """
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # -- the look itself ---------------------------------------------------

    async def async_probe(self) -> IngressResult:
        """Fetch the front door once and record the outcome.

        `allow_redirects=False` is a decision, not a default. Following the
        chain would mean that when the front door 302s to an identity
        provider, the reading published under this cluster's name is really a
        verdict on *that provider's* availability -- so an Authelia or a
        Cloudflare Access having a bad afternoon would report the house as
        unreachable. The redirect itself is the evidence we want: something
        at that address received the request and decided what to do with it.

        The body is never read. We want the status line; downloading the Home
        Assistant frontend every sixty seconds to reach the same conclusion
        would be pure waste on a node that is already busy running a house.
        """
        started = time.monotonic()
        try:
            session = async_get_clientsession(self._hass, verify_ssl=self._verify_tls)
            async with session.get(
                self._url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=INGRESS_PROBE_TIMEOUT),
                headers={"User-Agent": INGRESS_USER_AGENT},
            ) as response:
                status = response.status
        except TimeoutError:
            # Its own branch because "nothing answered within ten seconds" and
            # "the connection was refused" send an operator to two different
            # places -- a hung proxy versus a dead listener -- and a generic
            # message would send them to neither.
            return self._record(
                self._failure(f"no answer within {INGRESS_PROBE_TIMEOUT:.0f}s", started)
            )
        except Exception as err:  # noqa: BLE001 -- see the class docstring
            return self._record(self._failure(self._describe(err), started))

        elapsed = self._elapsed_ms(started)
        if is_acceptable_status(status):
            return self._record(
                IngressResult(
                    reachable=True,
                    status=status,
                    latency_ms=elapsed,
                    checked_at=datetime.now(tz=UTC),
                    skipped_reason=None,
                )
            )
        return self._record(
            IngressResult(
                reachable=False,
                status=status,
                error=f"HTTP {status}",
                latency_ms=elapsed,
                checked_at=datetime.now(tz=UTC),
                skipped_reason=None,
            )
        )

    @callback
    def async_note_not_leader(self) -> None:
        """This node is a follower, so it has stopped looking. Say so.

        Leaving the leader's last reading in place would be the worst of both
        worlds: an entity that says `Connected` about a check that stopped
        running when the lease moved. Unknown is the truth.

        The failure count is reset with it. A node that is demoted mid-outage
        and later promoted again must earn its alarm from scratch rather than
        inheriting two-thirds of one from a different shift.

        Deliberately NOT an all-clear: losing the lease is not evidence that
        anybody fixed the front door, and a `recovered` push here would say
        exactly that.
        """
        self.consecutive_failures = 0
        if self.last_result.skipped_reason == SKIP_NOT_LEADER:
            # Already unknown for this reason. Rewriting the same reading every
            # minute would wake every listener for no news.
            return
        self.last_result = IngressResult(skipped_reason=SKIP_NOT_LEADER)
        self._notify()

    # -- internals ---------------------------------------------------------

    def _record(self, result: IngressResult) -> IngressResult:
        if result.reachable:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
        self.last_result = result
        self._notify()
        return result

    def _failure(self, error: str, started: float) -> IngressResult:
        return IngressResult(
            reachable=False,
            error=error,
            latency_ms=self._elapsed_ms(started),
            checked_at=datetime.now(tz=UTC),
            skipped_reason=None,
        )

    def _describe(self, err: Exception) -> str:
        """A one-line cause, with any credentials taken back out of it.

        aiohttp puts the request URL into most of its error messages, so an
        error string is one of the ways a password in the configured URL
        escapes into an entity attribute and a push notification. Both halves
        of the userinfo are scrubbed, not just the password: a username is a
        credential too, and the pair is what a proxy log needs.
        """
        text = f"{type(err).__name__}: {err}".strip()
        parts = urlsplit(self._url)
        for secret in (parts.password, parts.username):
            if secret:
                text = text.replace(secret, "***")
        # Bounded: this lands in an entity attribute, which is written to the
        # recorder on every change, and some client errors carry a chain of
        # nested causes hundreds of characters long.
        return text[:200]

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.monotonic() - started) * 1000, 1)
