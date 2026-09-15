# Contributing

Thank you for looking. Two things about this project are unusual, and knowing
them first will save you effort.

---

## 1. A green test suite is not evidence

This is the most important thing on this page.

The rehearsal rig — two real Home Assistant containers, a real Valkey, this
integration installed the way HACS installs it — exists because **305 passing
tests and 93% coverage did not catch a defect that made every promotion restore
nothing at all.**

On its first run the promotion worked perfectly. The standby took the lease in
31 seconds. And then:

```
Snapshot age at restore: 18s (max 1800s)
Restored 0 entities from snapshot (skipped: 2 local-newer, ...)
```

The suite was green throughout. Every health check agreed. The house was empty.

So: if your change touches the restore, the snapshot, the lease, the promotion
sequence or the fileset swap, **say so in the PR.** A test that exercises your
code proves your code does what you wrote. It does not prove the two machines
agree, and the suite you can run here cannot tell you that either.

**The rehearsal rig is not in this repository**, and not to keep it from you:
it drives two real Home Assistant containers and keeps their live `/config`
directories, credentials included. Publishing it would publish those. The
maintainer runs it against changes that touch the failover path, and reports
what it showed on the PR.

What you *can* do, and what helps most: describe what you expect the two nodes
to do differently after your change. That is the thing the rig is pointed at.

## 2. The comments are the argument, not decoration

Where a function looks over-explained, the explanation is usually the record of
something that went wrong, and the constraint that stops it going wrong again.
Before simplifying something that looks redundant, read why it is there.

If the reasoning is absent or wrong, that is a bug worth reporting on its own.

---

## Getting set up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
./check
```

`./check` runs lint, the suite with its coverage floor, and a dependency audit.
It is the same gate CI runs. Please have it green before opening a PR.

🚨 **`pytest-homeassistant-custom-component` blocks real sockets, and that is
intended.** A test that quietly reaches a real Redis or a real Home Assistant
must fail loudly rather than pass on somebody's workstation. If a test fails on
a blocked socket, fix the test to use `tests/fakes.py` — **never unblock
sockets.**

That plugin also **pins an exact `homeassistant` version**, and that pin *is*
the tested target. Bumping it is how the target moves; it is not a formality.

## What a good change looks like

- **A test that fails before your fix and passes after.** If you cannot write
  one, say so in the PR and explain why — some of this is genuinely hard to test
  and an honest "I could not" is more useful than a test that asserts nothing.
- **Say what you actually ran.** "Tests pass" is less useful than "`./check`
  green, 1281 passed". If a gate failed and you could not work out why, say that
  — it is more useful than silence, and it is not a mark against the PR.
- **One concern per PR.** A refactor bundled with a fix means neither can be
  reverted independently.
- **Prose that says why, not what.** `# increment the counter` above `i += 1`
  is noise. `# ...because the peer counts from its own boot, not ours` is the
  reason somebody will need in a year.

## Constraints the code works under

These are not style preferences. Each one is load-bearing.

* **Single-threaded asyncio.** Everything runs on the HA event loop. Any
  blocking call (filesystem, sync DB, sync HTTP) must be wrapped in
  `hass.async_add_executor_job`.
* **Backend failures must never raise.** HA being briefly without state
  sync is a degradation; HA crashing because Redis is down is unacceptable.
  Every backend call is in a try/except that logs and returns a failure
  value.
* **State is immutable in HA.** `hass.states.get()` returns a frozen
  snapshot; mutations happen via `async_set`. Don't try to be clever.
* **Don't listen to events that don't matter.** `EVENT_STATE_CHANGED` is
  the only firehose worth tapping, and it is the only bus subscription the
  integration makes. What gets mirrored is decided by `_should_track`, not by
  any event filter. (Earlier versions of this note described a
  `NOISY_EVENTS_TO_IGNORE` set as the active filter; no such filtering ever
  existed — only `EVENT_STATE_CHANGED` is subscribed, so those events never
  reached a callback in the first place. The constant has been deleted.)

## Things that will be declined, and why

- **Monkey-patching Home Assistant, or requiring a forked core.** This
  integration works entirely through public APIs on purpose. Fragility against a
  minor Home Assistant upgrade is precisely the outcome the design exists to
  avoid.
- **Moving work onto the host when it can be done in Python inside Home
  Assistant.** There is one host-side component and it is one too many already.
- **Making the promoter clever.** It reads a key and runs a script. Every
  proposal to give it its own opinion about health has, so far, been a proposal
  to add a second thing that can disagree with the first.

None of these are "no" to the underlying problem — raise an issue and let us
find another route.

## How pull requests actually get merged

**Please read this, because it is not the usual arrangement.**

This public repository is generated from a private one that also holds design
records and review material. Your PR is reviewed and merged here, and then
**ported back by hand** so the next release carries it.

That means two things for you:

- **Your change will appear in a later release**, attributed to you in the
  release notes — but the commit in this repository's history may be the port,
  not your original.
- **A release cannot ship while a merged PR is un-ported.** The release process
  refuses, by PR number. Your work cannot be silently overwritten by the next
  export, which is the failure this arrangement would otherwise have.

If that is not acceptable to you, please say so in the PR rather than
discovering it later. It is a real cost and you are entitled to weigh it.

## Reporting rather than fixing

Entirely welcome, and often more useful. [`SECURITY.md`](SECURITY.md) for
anything security-related — **not** a public issue. Otherwise the issue
templates ask for a diagnostics file, which Home Assistant hands you as a
download so you can read it before deciding to attach it.

## Code of conduct

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to every space this project
occupies.
