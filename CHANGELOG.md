# Changelog

Every version, what it was for, and where the detail lives. Newest first.

Each release note carries its own **Upgrading** section — read that one, not
this table, before upgrading. This page exists to answer *"which release was
that in?"* without opening ten files — and the tag in each row links to the
note itself.

| Version | Tag | What it was for |
|---|---|---|
| **0.5.3** | [`v0.5.3`](docs/releases/v0.5.3.md) | Verified against every Home Assistant release it claims to support — 2026.6.4 through 2026.9.2, each a full suite run — and now says so. Found that 2026.9 swapped `voluptuous` for `probatio` under the same import name, which no symbol check and no breaking-changes section would show you. |
| **0.5.2** | [`v0.5.2`](docs/releases/v0.5.2.md) | Two defects in the machinery that reports defects: a reload re-announced every alert that was still true, and the TLS context was built on the event loop. Plus the promoter drowning its own journal, and a diagnostics platform you read before you send. |
| **0.5.1** | [`v0.5.1`](docs/releases/v0.5.1.md) | Written the morning after a real 61-minute outage in which detection worked perfectly and nobody was told. `ingress_unreachable` now reaches existing installs, and the alert says that nothing will act on it. |
| **0.5.0** | [`v0.5.0`](docs/releases/v0.5.0.md) | `automation` replicated by default, with a migration. The restore stopped overriding `initial_state`. The domain picker started saying which domains rebuild themselves. |
| **0.4.3** | [`v0.4.3`](docs/releases/v0.4.3.md) | Two P1s, both found by running the thing rather than testing it: a promotion that restored nothing (AR-0065), and an alert that fired on recovery instead of the emergency (AR-0066). |
| **0.4.2** | [`v0.4.2`](docs/releases/v0.4.2.md) | Alerting. Everything the integration knew was, until then, visible only to somebody already looking at the right screen. |
| **0.4.1** | [`v0.4.1`](docs/releases/v0.4.1.md) | One change, deliberately alone: a P1 security fix, cut as its own release so the upgrade could not be confused with anything else. |
| **0.4.0** | [`v0.4.0`](docs/releases/v0.4.0.md) | The security release, and the first whose tests ask whether *upgrading* breaks a running cluster rather than only whether the code is correct. |
| **0.3.5** | [`v0.3.5`](docs/releases/v0.3.5.md) | History that survives a failover, and a promotion that no longer waits ten minutes to decide. |
| **0.3.1** | [`v0.3.1`](docs/releases/v0.3.1.md) | Home Assistant can be healthy while every radio behind it is dead — one feature, and three things learned switching it on for real. |
| **0.3.0** | [`v0.3.0`](docs/releases/v0.3.0.md) | Radio failover works. Everything in it was found or proven on a live two-node fleet, not in a test rig. |
| 0.2.0 | `v0.2.0` | ⚠️ **No release note.** Predates the practice. |
| 0.1.0 | *(none)* | ⚠️ **No release note and no tag.** Predates both. |

## The two without notes

`0.1.0` and `0.2.0` are from before this project wrote release notes, and
inventing them now would mean reconstructing intent from diffs and presenting
the guess as a record. They are listed so the gap is visible rather than
looking like an oversight.

From the commits, `0.1.0` is where the repository became HACS-installable, and
`0.2.0` closes the original readiness test list and adds gating layers 3 and 4
(recorder writes and automations). `git log v0.2.0` is the honest source.

`0.1.0` has no tag because none was cut at the time; the manifest is the only
evidence it existed.

## Conventions

- **Patch** (`0.5.1` → `0.5.2`) — defects. No migration, no behaviour change an
  operator has to agree to.
- **Minor** (`0.4.3` → `0.5.0`) — a config-entry migration, or a change to what
  a cluster replicates or how it behaves. `0.5.0` earned one by rewriting the
  domain allowlist on upgrade.
- Every release note states whether **host-side artefacts** changed. If they
  did, the integration alone is not enough: the bundle must be regenerated and
  `install.sh` re-run on **both** nodes. `0.4.3` and `0.5.2` both did.
- Tags are annotated and point at the commit whose `manifest.json` declares
  that version. The `0.4.x` and `0.3.5` tags were added retrospectively on
  2026-09-10 and 2026-09-11 — each verified by reading the manifest at that
  commit rather than trusting a search.

## Verifying a release yourself

```bash
git show v0.5.2:custom_components/cluster_state_sync/manifest.json | grep version
git log --oneline v0.5.1..v0.5.2
```
