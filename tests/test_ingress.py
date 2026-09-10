"""The front-door check: AR-0060's missing half.

The incident these tests are written against: a failover that succeeded on
every measure the cluster owned -- lease moved, radios followed, Home
Assistant healthy -- while the address on the phone still pointed at the dead
node and returned 502. Nothing looked. This is what looks.

The load-bearing tests here are the negative ones, and there are three kinds:

* **Tests that it does not cry wolf.** A 401 is a working front door. A single
  failed probe is a blip. An alarm that fires on a proxy reloading its config
  is one nobody believes at 3am, and this project has already written that
  lesson down twice.
* **Tests that it does not claim to know more than it does.** A follower must
  report unknown, never the leader's last answer left to age.
* **Tests that it cannot take the cluster down.** A diagnostic that can raise
  into the setup path, or end its own timer, is a worse bug than the blind
  spot it was added to close.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import pathlib
from unittest.mock import patch

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.cluster_state_sync.const import (
    CONF_INGRESS_URL,
    CONF_INGRESS_VERIFY_TLS,
    DATA_INGRESS,
    DEFAULT_NOTIFY_CONDITIONS,
    DOMAIN,
    NOTIFY_INGRESS_UNREACHABLE,
)
from custom_components.cluster_state_sync.ingress import (
    INGRESS_FAILURES_BEFORE_ALARM,
    INGRESS_PROBE_TIMEOUT,
    SKIP_NEVER_RUN,
    SKIP_NOT_LEADER,
    IngressProbe,
    InvalidIngressURL,
    is_acceptable_status,
    redact_credentials,
    validate_ingress_url,
)
from tests.fakes import FakeBackend

URL = "https://home.example.invalid/"
SECRET = "s" * 44


def _probe(hass: HomeAssistant, url: str = URL) -> IngressProbe:
    return IngressProbe(hass, url)


# ==========================================================================
# The acceptance rule -- what counts as "somebody could get in"
# ==========================================================================


async def test_a_502_is_not_reachable(hass: HomeAssistant, aioclient_mock) -> None:
    """🚨 AR-0060's exact symptom, and the reason this module exists.

    The proxy was up and answering; it simply had nothing alive behind it. A
    check that treats "the TCP connection succeeded" as reachable would have
    been green throughout the incident, which is how this blind spot survived
    a failover that was otherwise instrumented to the second.
    """
    aioclient_mock.get(URL, status=502)
    probe = _probe(hass)
    result = await probe.async_probe()
    assert result.reachable is False
    assert result.status == 502
    assert result.error == "HTTP 502"


async def test_a_404_is_not_reachable(hass: HomeAssistant, aioclient_mock) -> None:
    """A routing rule pointing at something that no longer exists.

    Reporting this as reachable because "the server answered" would be the
    AR-0060 failure wearing a different number: the house is fine, the proxy
    is fine, and the person typing the address still cannot get in.
    """
    aioclient_mock.get(URL, status=404)
    assert (await _probe(hass).async_probe()).reachable is False


@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_challenge_is_the_front_door_working(
    hass: HomeAssistant, aioclient_mock, status: int
) -> None:
    """🚨 The cry-wolf test.

    A front door behind SSO or basic auth refuses an unauthenticated probe,
    and being refused proves something is there to refuse you. Counting that
    as unreachable would report every properly-protected installation as
    permanently broken -- an alarm the operator switches off, taking the real
    one with it.
    """
    aioclient_mock.get(URL, status=status)
    result = await _probe(hass).async_probe()
    assert result.reachable is True, "an auth challenge was reported as an outage"
    assert result.status == status


@pytest.mark.parametrize("status", [200, 204, 301, 302, 399])
def test_the_statuses_that_mean_the_door_answered(status: int) -> None:
    """Below 400 is the door working, redirects included."""
    assert is_acceptable_status(status)


@pytest.mark.parametrize("status", [400, 404, 500, 502, 503, 504])
def test_the_statuses_that_mean_it_did_not(status: int) -> None:
    """And these are the ones worth waking somebody for."""
    assert not is_acceptable_status(status)


# ==========================================================================
# Redirects -- whose availability are we actually reporting?
# ==========================================================================


class _FakeSession:
    """A session that HONOURS `allow_redirects`, which is the whole point.

    `aioclient_mock` accepts the flag and ignores it, so a test written against
    it would pass whether or not the production code sets it -- the exact
    shape of unfaithful double this project has been bitten by before (an
    omitted field in a fake removes the test rather than failing it). This one
    follows the redirect when told to, so dropping `allow_redirects=False`
    changes the observed result.
    """

    def __init__(self, status: int, followed_status: int = 200) -> None:
        self._status = status
        self._followed_status = followed_status
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        status = self._status
        if kwargs.get("allow_redirects") and 300 <= status < 400:
            status = self._followed_status
        return _FakeResponse(status)


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


async def test_a_redirect_is_reported_as_itself_not_followed(hass: HomeAssistant) -> None:
    """🚨 Whose outage would we be reporting?

    A front door that 302s to an identity provider is answering. Following the
    chain would make the published reading a verdict on *that provider* -- so
    Authelia or Cloudflare Access having a bad afternoon would be reported as
    "the house is unreachable", which is both wrong and unfixable by the
    operator receiving the alert.

    The fake follows redirects when permitted, so this fails if the flag is
    ever dropped.
    """
    session = _FakeSession(status=302, followed_status=200)
    with patch(
        "custom_components.cluster_state_sync.ingress.async_get_clientsession",
        return_value=session,
    ):
        result = await _probe(hass).async_probe()

    assert result.status == 302, "the probe followed the redirect off to somewhere else"
    assert result.reachable is True


async def test_the_request_is_bounded_by_a_timeout(hass: HomeAssistant) -> None:
    """A probe with no deadline can leave one request in flight per minute.

    The interval is 60s and the timeout must stay under it, or a hung front
    door accumulates overlapping requests against the very thing that is
    already struggling.
    """
    session = _FakeSession(status=200)
    with patch(
        "custom_components.cluster_state_sync.ingress.async_get_clientsession",
        return_value=session,
    ):
        await _probe(hass).async_probe()

    timeout = session.calls[0]["timeout"]
    assert timeout.total == INGRESS_PROBE_TIMEOUT
    assert INGRESS_PROBE_TIMEOUT < 60.0, "a probe may not outlive its own interval"


# ==========================================================================
# Failure is a reading, never an exception
# ==========================================================================


async def test_a_connection_failure_is_a_reading(hass: HomeAssistant, aioclient_mock) -> None:
    """DNS still pointing at the dead node is the other half of AR-0060.

    502 was one shape; a name that resolves to a machine which no longer
    answers is the other, and it arrives as an exception rather than a status.
    """
    aioclient_mock.get(URL, exc=aiohttp.ClientError("Cannot connect to host home.example.invalid"))
    result = await _probe(hass).async_probe()
    assert result.reachable is False
    assert result.status is None
    assert "Cannot connect" in (result.error or "")


async def test_a_timeout_says_it_timed_out(hass: HomeAssistant, aioclient_mock) -> None:
    """ "Nothing answered in ten seconds" and "the connection was refused" send
    an operator to two different places -- a hung proxy versus a dead listener.

    A single generic "unreachable" would send them to neither, which is the
    difference between a diagnostic and a shrug.
    """
    aioclient_mock.get(URL, exc=TimeoutError())
    result = await _probe(hass).async_probe()
    assert result.reachable is False
    assert "no answer within" in (result.error or "")


async def test_the_probe_never_raises_whatever_happens(hass: HomeAssistant) -> None:
    """🚨 The rule that matters more than any reading it produces.

    This runs on the node holding the house up, from a timer that also carries
    the alert dispatch after it. An exception here skips that dispatch, and
    escapes into a background task where it is reported anonymously, if at
    all. `backend.py` takes the same never-raise posture towards Valkey for
    the same reason: a diagnostic does not get to interfere with the thing it
    is diagnosing.

    Deliberately raises something no `except aiohttp.ClientError` would catch.
    """
    with patch(
        "custom_components.cluster_state_sync.ingress.async_get_clientsession",
        side_effect=RuntimeError("the event loop is having a day"),
    ):
        result = await _probe(hass).async_probe()
    assert result.reachable is False
    assert "RuntimeError" in (result.error or "")


async def test_an_enormous_error_is_truncated(hass: HomeAssistant, aioclient_mock) -> None:
    """This string lands in an entity attribute, which the recorder writes.

    Some client errors carry a chain of nested causes hundreds of characters
    long, and an unbounded one would be written to the database on every
    change of a value that flaps.
    """
    aioclient_mock.get(URL, exc=aiohttp.ClientError("x" * 5000))
    result = await _probe(hass).async_probe()
    assert len(result.error or "") <= 200


# ==========================================================================
# Credentials -- the part that would leak to a phone
# ==========================================================================

CREDENTIALED = "https://doorman:hunter2@home.example.invalid:8443/lovelace"


def test_credentials_are_stripped_from_anything_displayed() -> None:
    """A front door behind basic auth is commonly written into the URL.

    Everything this module publishes is read by anybody with a Home Assistant
    login -- an entity attribute is visible in the UI, kept by the recorder,
    included in a diagnostics download -- and the alert router forwards the
    same text to a third-party notify service. A password arriving in a
    Discord channel because a proxy returned 502 is a real outcome.
    """
    shown = redact_credentials(CREDENTIALED)
    assert "hunter2" not in shown
    # The username goes too: it is half of the credential, and it is what a
    # proxy log needs to match the password against.
    assert "doorman" not in shown
    assert "@" not in shown
    # ...and the address is still recognisable, or it is no use in an alert.
    assert "home.example.invalid:8443" in shown
    assert shown.endswith("/lovelace")


def test_a_url_without_credentials_is_left_exactly_alone() -> None:
    """Redaction must not quietly rewrite the ordinary case.

    A normaliser that dropped a trailing slash or a query string would make
    the alert name an address the operator cannot match against their own
    configuration.
    """
    for url in ("https://home.example.invalid/", "http://10.0.0.5:8123/lovelace?kiosk=1"):
        assert redact_credentials(url) == url


async def test_a_password_never_reaches_the_error_string(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """🚨 aiohttp puts the request URL into several of its own messages.

    So the scrub cannot live only where we format the URL ourselves: the
    library hands us the credential back inside the text of the exception, and
    that text is published verbatim.
    """
    aioclient_mock.get(CREDENTIALED, exc=aiohttp.ClientError(f"Cannot connect to {CREDENTIALED}"))
    probe = IngressProbe(hass, CREDENTIALED)
    result = await probe.async_probe()
    assert "hunter2" not in (result.error or ""), "the password was published in an attribute"
    assert "***" in (result.error or "")


# ==========================================================================
# Counting failures -- the reading is exact, the interruption waits
# ==========================================================================


async def test_failures_accumulate_and_a_success_resets_them(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The counter is what lets the alarm wait for a pattern.

    If it did not reset on success, a node that had three bad minutes at any
    point in its life would alarm on every single failure afterwards.
    """
    aioclient_mock.get(URL, status=502)
    probe = _probe(hass)
    for expected in (1, 2, 3):
        await probe.async_probe()
        assert probe.consecutive_failures == expected

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, status=200)
    await probe.async_probe()
    assert probe.consecutive_failures == 0


async def test_the_reading_moves_on_the_first_failure(hass: HomeAssistant, aioclient_mock) -> None:
    """The entity is exact; only the push waits for three.

    Confusing the two would mean an operator staring at a dashboard that still
    said `Connected` while the probe had already failed twice -- the same
    "green status is not function" trap this project has hit before.
    """
    aioclient_mock.get(URL, status=502)
    probe = _probe(hass)
    result = await probe.async_probe()
    assert result.reachable is False
    assert probe.consecutive_failures < INGRESS_FAILURES_BEFORE_ALARM


# ==========================================================================
# The follower -- what a node that is not looking should say
# ==========================================================================


async def test_a_follower_reports_unknown_not_the_last_leader_reading(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """🚨 The worst available answer would be to leave the old one on screen.

    An entity reading `Connected` about a check that stopped running when the
    lease moved is a green tick for a probe that is not running -- which is
    precisely the class of defect (AR-0040) this whole feature exists to end.
    """
    aioclient_mock.get(URL, status=200)
    probe = _probe(hass)
    assert (await probe.async_probe()).reachable is True

    probe.async_note_not_leader()
    assert probe.last_result.reachable is None
    assert probe.last_result.skipped_reason == SKIP_NOT_LEADER


async def test_a_demotion_resets_the_failure_count(hass: HomeAssistant, aioclient_mock) -> None:
    """A node promoted back mid-outage must earn its alarm from scratch.

    Inheriting two-thirds of an alarm from a shift it did not work would let a
    single probe on the new leader interrupt somebody.
    """
    aioclient_mock.get(URL, status=502)
    probe = _probe(hass)
    await probe.async_probe()
    await probe.async_probe()
    probe.async_note_not_leader()
    assert probe.consecutive_failures == 0


async def test_a_follower_does_not_wake_its_listeners_every_minute(hass: HomeAssistant) -> None:
    """It is already unknown; saying so again is not news.

    Without this a cold follower writes the same entity state once a minute
    for ever, and every one of those writes is a row in the recorder.
    """
    probe = _probe(hass)
    calls: list[int] = []
    probe.add_listener(lambda: calls.append(1))
    probe.async_note_not_leader()
    probe.async_note_not_leader()
    probe.async_note_not_leader()
    assert len(calls) == 1


async def test_unsubscribing_twice_is_harmless(hass: HomeAssistant) -> None:
    """Home Assistant calls `async_on_remove` callbacks on entity removal.

    A reload removes the entity and rebuilds it; a raise from the second call
    would surface as an error unloading the config entry, which is a far worse
    outcome than an idempotent no-op.
    """
    probe = _probe(hass)
    remove = probe.add_listener(lambda: None)
    remove()
    remove()


async def test_nothing_has_been_checked_before_the_first_probe(hass: HomeAssistant) -> None:
    """None, not False. "Nobody asked yet" is not "we asked and failed".

    Setup deliberately does not probe -- Home Assistant has not finished
    starting, so a proxy in front of it would legitimately answer 502 and the
    act of restarting would raise an alarm.
    """
    probe = _probe(hass)
    assert probe.last_result.reachable is None
    assert probe.last_result.skipped_reason == SKIP_NEVER_RUN


async def test_a_listener_can_unsubscribe(hass: HomeAssistant, aioclient_mock) -> None:
    """An entity removed from Home Assistant must stop being written to."""
    aioclient_mock.get(URL, status=200)
    probe = _probe(hass)
    calls: list[int] = []
    remove = probe.add_listener(lambda: calls.append(1))
    await probe.async_probe()
    remove()
    await probe.async_probe()
    assert len(calls) == 1


# ==========================================================================
# The URL an operator typed
# ==========================================================================


def test_an_empty_url_is_off_not_broken() -> None:
    """The feature is off by default, so blank must not read as a mistake."""
    for blank in (None, "", "   "):
        assert validate_ingress_url(blank) is None


@pytest.mark.parametrize(
    "bad",
    [
        "home.example.com",  # no scheme: the commonest typo
        "ftp://home.example.com",  # a scheme this cannot fetch
        "file:///etc/passwd",  # not a front door at all
        "https://",  # parses cleanly, then fails at request time
    ],
)
def test_a_url_that_cannot_be_probed_is_refused(bad: str) -> None:
    """🚨 Refused where it was typed, not swallowed into a log line.

    An accepted-but-unprobeable URL produces no entity and no alert, so the
    operator is left believing they configured a front-door check while the
    cluster keeps the exact blind spot AR-0060 was raised for -- now with a
    settings page implying it had been closed.
    """
    with pytest.raises(InvalidIngressURL):
        validate_ingress_url(bad)


def test_a_usable_url_survives_untouched() -> None:
    """Including the port, path and query, which some deployments need."""
    url = "http://10.0.0.5:8123/lovelace?kiosk=1"
    assert validate_ingress_url(f"  {url}  ") == url


# ==========================================================================
# Wired into the integration: leader gating, the timer, the alert
# ==========================================================================


def _entry(**extra: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": SECRET,
            "snapshot_interval": 30,
            "fileset_enabled": False,
            "notify_services": ["notify.tester"],
            **extra,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return ok


async def _tick(hass: HomeAssistant, *, minutes: int) -> None:
    """Advance past the probe interval so the scheduled job runs."""
    from homeassistant.util import dt as dt_util

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


def _leading(entry: MockConfigEntry, value: bool):
    return patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=value)


@pytest.fixture
def pushes(hass: HomeAssistant):
    """Every call that reached a `notify.` service."""
    return async_mock_service(hass, "notify", "tester")


def _ingress_pushes(pushes) -> list:
    """Only the front-door alerts.

    Matched on the probed address rather than on words in the title: the
    backend-lost alert says "unreachable" too, and a filter that caught it
    would turn an unrelated regression into a confusing failure here -- or,
    worse, make a missing ingress push look like a present one.
    """
    return [c for c in pushes if URL in c.data["message"] or CREDENTIALED in c.data["message"]]


async def test_no_url_means_no_probe_and_no_entity(hass: HomeAssistant) -> None:
    """Off by default, and off means absent.

    An ingress entity for a check nobody switched on would read as a working
    check -- the AR-0060 blind spot with a tick beside it. The slot is still
    set to None rather than left missing, because a missing key and a key
    holding None read the same to `.get()` and very differently to `[...]`.
    """
    entry = _entry()
    assert await _setup(hass, entry)
    assert DATA_INGRESS in entry.runtime_data
    assert entry.runtime_data[DATA_INGRESS] is None
    assert _entity_id(hass, entry) is None


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_ingress_reachable"
    )


async def test_a_configured_url_creates_the_probe_and_the_entity(hass: HomeAssistant) -> None:
    """The opposite branch: an operator who asked for it gets it."""
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)
    assert entry.runtime_data[DATA_INGRESS] is not None
    assert _entity_id(hass, entry) is not None


async def test_an_unprobeable_url_does_not_stop_the_cluster_starting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """🚨 A typo in a diagnostic's address may not take down the house.

    The options page refuses this at the point of typing, so reaching here
    means the value arrived some other way -- a restored config entry, a hand
    edit, an older version. Refusing setup over it would stop state
    replication and failover entirely to protect a status check.
    """
    entry = _entry(**{CONF_INGRESS_URL: "home.example.invalid"})
    assert await _setup(hass, entry), "a bad ingress URL refused the whole setup"
    assert entry.runtime_data[DATA_INGRESS] is None
    assert "no ingress probe" in caplog.text


async def test_the_leader_probes_on_the_interval(
    hass: HomeAssistant, aioclient_mock, pushes
) -> None:
    """Scheduling a job is not evidence the job runs."""
    aioclient_mock.get(URL, status=200)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        await _tick(hass, minutes=2)

    assert entry.runtime_data[DATA_INGRESS].last_result.reachable is True
    assert aioclient_mock.call_count == 1


async def test_a_follower_never_probes(hass: HomeAssistant, aioclient_mock, pushes) -> None:
    """🚨 Leader-only, and this is the test that proves it.

    A follower probing the shared address proves nothing about the node that
    is supposed to be answering -- it could be green purely because the peer
    is healthy -- and it doubles the traffic against a tunnel that may be
    metered. Worse, it would let the standby raise an ingress alarm about an
    outage it is in no position to judge.
    """
    aioclient_mock.get(URL, status=200)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, False):
        await _tick(hass, minutes=2)

    assert aioclient_mock.call_count == 0, "a follower probed the cluster's front door"
    probe = entry.runtime_data[DATA_INGRESS]
    assert probe.last_result.skipped_reason == SKIP_NOT_LEADER


async def test_one_failure_does_not_wake_anybody(
    hass: HomeAssistant, aioclient_mock, pushes
) -> None:
    """🚨 The cry-wolf guard, and the reason the threshold exists.

    A proxy reloading its configuration, a tunnel reconnecting, a DHCP lease
    renewing: all of these produce exactly one failed probe. Pushing on the
    first one teaches the operator to swipe the notification away unread, and
    the one they swipe away unread will be the real failover.
    """
    aioclient_mock.get(URL, status=502)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        await _tick(hass, minutes=2)

    assert _ingress_pushes(pushes) == []
    # ...and the reading is nonetheless exact.
    assert entry.runtime_data[DATA_INGRESS].last_result.reachable is False


async def test_a_sustained_outage_does_wake_somebody(
    hass: HomeAssistant, aioclient_mock, pushes
) -> None:
    """The whole point: the failover worked and nobody could get in.

    Three consecutive failures a minute apart is a front door, not a blip, and
    this is the interruption AR-0060 records that nobody received.
    """
    aioclient_mock.get(URL, status=502)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        for minute in range(2, 2 + INGRESS_FAILURES_BEFORE_ALARM):
            await _tick(hass, minutes=minute)

    sent = _ingress_pushes(pushes)
    assert len(sent) == 1, f"expected exactly one interruption, got {len(sent)}"
    assert "502" in sent[0].data["message"]


async def test_the_alarm_is_pushed_once_not_once_per_probe(
    hass: HomeAssistant, aioclient_mock, pushes
) -> None:
    """An outage lasts hours. The alert router speaks on the edge.

    Without this a front door that stays down pushes a notification every
    minute until somebody either fixes it or blocks the app.
    """
    aioclient_mock.get(URL, status=502)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        for minute in range(2, 12):
            await _tick(hass, minutes=minute)

    assert len(_ingress_pushes(pushes)) == 1


async def test_the_all_clear_is_sent_when_the_door_comes_back(
    hass: HomeAssistant, aioclient_mock, pushes
) -> None:
    """Silence is not evidence -- AR-0040, applied to the front door.

    An alarm that stops without a word is indistinguishable from one nobody
    sent, and the person we woke may be nowhere near the repairs page.
    """
    aioclient_mock.get(URL, status=502)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        for minute in range(2, 2 + INGRESS_FAILURES_BEFORE_ALARM):
            await _tick(hass, minutes=minute)
        assert len(_ingress_pushes(pushes)) == 1

        aioclient_mock.clear_requests()
        aioclient_mock.get(URL, status=200)
        await _tick(hass, minutes=20)

    sent = _ingress_pushes(pushes)
    assert len(sent) == 2, "the front door recovered and nobody was told"
    assert "again" in sent[1].data["title"]


async def test_a_healthy_door_says_nothing_at_all(
    hass: HomeAssistant, aioclient_mock, pushes
) -> None:
    """A clear with no preceding raise is not news.

    The scheduled callback calls `async_clear` on every successful probe, so
    without edge-triggering a working installation would push once a minute
    for ever.
    """
    aioclient_mock.get(URL, status=200)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        for minute in range(2, 8):
            await _tick(hass, minutes=minute)

    assert _ingress_pushes(pushes) == []


async def test_an_unforeseen_failure_is_logged_as_itself_and_checking_continues(
    hass: HomeAssistant, aioclient_mock, caplog: pytest.LogCaptureFixture
) -> None:
    """🚨 Written after measuring what actually happens, not what I assumed.

    My first version of this asserted that an escaping exception would end the
    timer. It does not: `_TrackTimeInterval` re-arms before running the job, so
    the loop survives on its own -- and the test passed with the guard removed,
    which is a test that cannot fail.

    What removing the guard really costs is the diagnosis. The exception
    becomes an unretrieved task error, surfaced by asyncio whenever the task is
    collected, attributed to a `HassJob` with nothing naming this integration
    or this check. So this asserts the thing the guard actually buys -- a log
    line that says what broke -- as well as the continuation.
    """
    aioclient_mock.get(URL, status=200)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)
    probe = entry.runtime_data[DATA_INGRESS]

    with (
        _leading(entry, True),
        patch.object(probe, "async_probe", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.ERROR, logger="custom_components.cluster_state_sync"),
    ):
        await _tick(hass, minutes=2)

    assert "Ingress probe failed unexpectedly" in caplog.text, (
        "the failure escaped as an anonymous task error instead of naming itself"
    )

    # And the second tick proves the first did not stop the checking.
    with _leading(entry, True):
        await _tick(hass, minutes=4)
    assert probe.last_result.reachable is True


# ==========================================================================
# The entity
# ==========================================================================


async def test_the_entity_publishes_what_an_operator_needs_to_act(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """A 502, a 404 and a DNS failure are three different repairs.

    `is_on` alone cannot tell them apart, so somebody woken at 3am would have
    to go and read a log to find out which -- on a node they may not be able
    to reach, for the reason this alarm just fired.
    """
    aioclient_mock.get(URL, status=502)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        await _tick(hass, minutes=2)

    state = hass.states.get(_entity_id(hass, entry))
    assert state is not None
    assert state.state == "off"
    assert state.attributes["last_status"] == 502
    assert state.attributes["last_error"] == "HTTP 502"
    assert state.attributes["last_checked"] is not None
    assert state.attributes["latency_ms"] is not None
    assert state.attributes["url"] == URL


async def test_the_entity_never_publishes_a_credential(hass: HomeAssistant, aioclient_mock) -> None:
    """🚨 Attributes are visible to every user and kept by the recorder.

    A URL carrying basic-auth credentials is an ordinary way to describe a
    protected front door, and the attribute is the wrong place for it to
    resurface.
    """
    aioclient_mock.get(CREDENTIALED, status=200)
    entry = _entry(**{CONF_INGRESS_URL: CREDENTIALED})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        await _tick(hass, minutes=2)

    state = hass.states.get(_entity_id(hass, entry))
    assert "hunter2" not in json.dumps(dict(state.attributes))


async def test_the_entity_stays_available_when_the_door_is_down(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """ "Unreachable" is the reading, not a reason to hide the entity.

    Marking it unavailable would blank the one entity whose whole job is to
    still be readable when something has already gone wrong.
    """
    aioclient_mock.get(URL, exc=aiohttp.ClientError("no route to host"))
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, True):
        await _tick(hass, minutes=2)

    state = hass.states.get(_entity_id(hass, entry))
    assert state.state == "off", "the entity went unavailable instead of reporting the outage"


async def test_a_follower_entity_reads_unknown(hass: HomeAssistant, aioclient_mock) -> None:
    """And says why, so nobody mistakes it for a broken check."""
    aioclient_mock.get(URL, status=200)
    entry = _entry(**{CONF_INGRESS_URL: URL})
    assert await _setup(hass, entry)

    with _leading(entry, False):
        await _tick(hass, minutes=2)

    state = hass.states.get(_entity_id(hass, entry))
    assert state.state == "unknown"
    assert state.attributes["not_checked_because"] == SKIP_NOT_LEADER


# ==========================================================================
# Configuring it
# ==========================================================================


def _options_entry(options: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"redis_host": "valkey.lan", "redis_port": 6379, "cluster_namespace": "ns"},
        options=options,
    )


async def test_a_bad_url_is_refused_on_the_page_that_asked_for_it(hass: HomeAssistant) -> None:
    """🚨 Accepting it would produce a settings page that quietly does nothing.

    The operator would leave believing the front-door check was configured,
    and the only evidence otherwise would be an absent entity and a log line
    -- which is how AR-0060 stayed invisible in the first place.
    """
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow

    flow = ClusterStateSyncOptionsFlow(_options_entry({}))
    flow.hass = hass
    result = await flow.async_step_ingress({CONF_INGRESS_URL: "home.example.com"})
    assert result["type"] == "form"
    assert result["errors"] == {CONF_INGRESS_URL: "invalid_ingress_url"}


async def test_the_refused_form_keeps_what_was_typed(hass: HomeAssistant) -> None:
    """Blanking the field on rejection makes the operator retype the typo.

    Which is the one thing guaranteed not to help them see it.
    """
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow

    flow = ClusterStateSyncOptionsFlow(_options_entry({}))
    flow.hass = hass
    result = await flow.async_step_ingress({CONF_INGRESS_URL: "home.example.com"})
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if hasattr(key, "default")
    }
    assert defaults[CONF_INGRESS_URL] == "home.example.com"


async def test_clearing_the_url_is_accepted_as_switching_it_off(hass: HomeAssistant) -> None:
    """Turning a feature off must not be reported as a mistake."""
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow

    flow = ClusterStateSyncOptionsFlow(_options_entry({CONF_INGRESS_URL: URL}))
    flow.hass = hass
    result = await flow.async_step_ingress({CONF_INGRESS_URL: ""})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INGRESS_URL] == ""


async def test_saving_ingress_does_not_discard_the_other_sections(hass: HomeAssistant) -> None:
    """`async_create_entry` replaces the options wholesale.

    The same data-loss bug the alerts and connection pages already guard
    against, which waits for a restart to become visible.
    """
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow

    flow = ClusterStateSyncOptionsFlow(_options_entry({"redis_host": "valkey-2.lan"}))
    flow.hass = hass
    result = await flow.async_step_ingress({CONF_INGRESS_URL: URL, CONF_INGRESS_VERIFY_TLS: False})
    assert result["data"]["redis_host"] == "valkey-2.lan", "connection override was discarded"
    assert result["data"][CONF_INGRESS_URL] == URL
    assert result["data"][CONF_INGRESS_VERIFY_TLS] is False


async def test_the_form_shows_what_is_already_configured(hass: HomeAssistant) -> None:
    """Reopening the page must not silently offer to reset it."""
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow

    flow = ClusterStateSyncOptionsFlow(
        _options_entry({CONF_INGRESS_URL: URL, CONF_INGRESS_VERIFY_TLS: False})
    )
    flow.hass = hass
    form = await flow.async_step_ingress()
    defaults = {
        str(key): key.default() for key in form["data_schema"].schema if hasattr(key, "default")
    }
    assert defaults[CONF_INGRESS_URL] == URL
    assert defaults[CONF_INGRESS_VERIFY_TLS] is False


async def test_turning_off_certificate_checking_actually_reaches_the_session(
    hass: HomeAssistant,
) -> None:
    """🚨 An option that is stored and never read is a lie with a checkbox.

    This one has a specific failure mode: an operator with an internal CA
    unticks it, still sees a red entity, concludes the check is broken and
    turns the whole feature off -- taking the real alarm with it.
    """
    session = _FakeSession(status=200)
    with patch(
        "custom_components.cluster_state_sync.ingress.async_get_clientsession",
        return_value=session,
    ) as get_session:
        await IngressProbe(hass, URL, verify_tls=False).async_probe()
    assert get_session.call_args.kwargs["verify_ssl"] is False


# ==========================================================================
# The words, which are half of an alert
# ==========================================================================


def test_the_condition_is_on_by_default_but_can_never_fire_uninvited() -> None:
    """Defaulted on, and safe to default on, only because of the empty URL.

    An alert that cries wolf at install time is one nobody believes at 3am.
    This one cannot fire for anybody who has not typed an address, which is
    what makes shipping it enabled honest rather than noisy.
    """
    assert NOTIFY_INGRESS_UNREACHABLE in DEFAULT_NOTIFY_CONDITIONS


def test_both_translation_files_label_the_new_step_and_entity() -> None:
    """Nothing but a test connects the code to these files.

    A form field with no label renders as its raw config key, and
    `ingress_verify_tls` is not a question anybody can answer well.
    """
    root = pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync"
    for name in ("strings.json", "translations/en.json"):
        blob = json.loads((root / name).read_text())
        step = blob["options"]["step"]["ingress"]
        for field in (CONF_INGRESS_URL, CONF_INGRESS_VERIFY_TLS):
            assert step["data"].get(field, "").strip(), f"{name}: {field} has no label"
            assert step["data_description"].get(field, "").strip(), (
                f"{name}: {field} has no explanation, and this one needs one"
            )
        assert blob["options"]["error"]["invalid_ingress_url"].strip()
        assert blob["entity"]["binary_sensor"]["ingress_reachable"]["name"].strip()


def test_the_operator_is_told_plainly_what_a_green_reading_cannot_prove() -> None:
    """🚨 The honesty requirement, pinned so a tidy-up cannot quietly drop it.

    A health check believed further than it can see is worse than none: it is
    AR-0060 with a tick beside it. A probe made from inside the LAN can be
    green while the tunnel every external user depends on is down, and the
    page that asks for the URL is the only place the operator is guaranteed
    to read.
    """
    root = pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync"
    text = json.loads((root / "strings.json").read_text())["options"]["step"]["ingress"][
        "description"
    ].lower()
    assert "split-horizon" in text or "split dns" in text
    assert "outside" in text
    assert "cannot prove" in text


def test_the_module_docstring_carries_the_same_warning(caplog) -> None:
    """The docstring is what the next maintainer reads before extending this.

    Losing the caveat there is how the next feature ends up trusting the probe
    for something it cannot answer.
    """
    from custom_components.cluster_state_sync import ingress

    doc = (ingress.__doc__ or "").lower()
    assert "split-horizon" in doc
    assert "does not prove" in doc
    del caplog


async def test_the_startup_log_line_does_not_overclaim(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The one line an operator sees when they switch it on.

    "Ingress check on" with nothing after it would read as a guarantee that
    people can reach the house, which is precisely what it is not.
    """
    entry = _entry(**{CONF_INGRESS_URL: URL})
    with caplog.at_level(logging.INFO, logger="custom_components.cluster_state_sync"):
        assert await _setup(hass, entry)
    assert "does NOT prove an external user can reach you" in caplog.text
