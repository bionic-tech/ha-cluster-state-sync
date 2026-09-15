# Security

> This page is how to **report** a vulnerability. How the integration protects
> what it replicates — snapshot signing, TLS, sensitive domains, datastore
> credentials — is [docs/REFERENCE-security.md](docs/REFERENCE-security.md).

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private advisory form:
**Security → Report a vulnerability** on this repository.

If that is unavailable to you, open an issue containing only *"security report,
please provide a private channel"* and nothing else — no detail, no reproduction
steps, no configuration.

You should get an acknowledgement within a week. If you do not, assume the
message was lost rather than ignored, and chase it.

## Why this is not a formality

This integration is not a thermostat card. On a normal installation it:

- **runs scripts as root on two machines**, through a systemd timer that starts
  and stops Home Assistant and claims USB devices;
- **holds credentials for a shared datastore** that both nodes read and write;
- **decides which of two machines is in charge of a house** — its alarm, its
  locks, its heating.

A defect that lets somebody influence the lease decides who controls the
building. Please treat it accordingly.

## What is in scope

- Anything that lets an unauthorised party take, hold or influence the cluster
  lease.
- Anything that causes credentials, the cluster secret, or TLS material to be
  written where they should not be — logs, diagnostics, the bundle, the
  replicated fileset.
- Anything that lets snapshot content be forged or replayed such that a
  promoted node restores state it was never sent. Entries are signed for this
  reason; a way around that signature is a vulnerability.
- Anything that causes the promotion scripts to execute content an attacker
  controls.
- Privilege escalation through the generated bundle or `install.sh`.

## What is out of scope

Not because it does not matter, but because it is already written down and is a
property of the design rather than a defect in it:

- **Both nodes run automations during the overlap window.** There is no
  automation leader election, deliberately. See `docs/KNOWN-LIMITATIONS.md`.
- **A promoted standby has no radios attached to the other machine**, and one
  radio never fails over at all.
- **Anyone holding the datastore credential can read any namespace.** The
  namespace is an organisational label, not a security boundary — said plainly
  in `const.py` where it is validated.
- **The restore writes Home Assistant's state machine; it does not command
  devices.** A restored "on" is not a switch being turned on.
- Findings from a scanner with no demonstrated impact on the above.

## If you are reporting something found by automated tooling

Please say which tool and include the finding verbatim. Several dependency
advisories against this project are in packages **pinned by Home Assistant
itself**, which this repository cannot upgrade independently —
`scripts/audit_deps.py` reports those separately for exactly that reason, and a
report that repeats one is still welcome, just already known.

## Diagnostics and issue reports

The **Download diagnostics** button produces a file with secrets, hostnames,
paths and URLs withheld, using an allowlist so that settings added in later
releases are withheld by default rather than published by accident. It is
handed to you as a download rather than uploaded on your behalf so that you can
read it before deciding.

🚨 **Home Assistant's logs are not redacted for you.** They contain entity
names, and occasionally tokens. Read them before pasting them into a public
issue.
