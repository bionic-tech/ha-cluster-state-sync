# Settled decisions — do not re-raise these as blockers

Short file, deliberately. Everything here has been decided by the owner. Each one has been
rediscovered at least once and reported back as a newly-found obstacle, which costs a round
trip and reads as the work being blocked when it is not.

**If you are an agent reading this: these are answered. Reporting them again as findings is
noise. If you believe one is genuinely wrong, say so directly and give the reason — do not
re-file it as a discovery.**

---

## D1 — node-b's Home Assistant is to be upgraded to match tiger1 and cleared down

**Decided. Not open. Not a blocker.**

tiger2 currently runs container `home-assistant-2`, image **2025.4**, on host networking, with
its own config and its own `remote_homeassistant` entry pointed back at tiger1 — i.e. a
**mirror**, not a standby.

**It is to be brought to tiger1's version (2026.6.4) and cleared down**, becoming a clean
standby that receives tiger1's `.storage`. This has been the intent since F3 was written.

Things that keep being reported as new obstacles, all of which are **the content of that task**:

- the 14-month version gap between 2025.4 and 2026.4/2026.6.4
- Home Assistant migrating `.storage` forward only, never backward
- tiger2 holding its own refresh tokens and config
- tiger2 answering 200 on `:8123` and being indistinguishable from tiger1 to a health check

None of these is a reason to stop. They are descriptions of why the rebuild is needed.

**What follows from it, and is the only part worth restating:** because `.storage` migrates
forward only, tiger2 has to be rebuilt *before* it can receive tiger1's storage. So the order
is **F3 → I9 → Traefik failover**. That is sequencing, not blocking, and it needs no further
discussion.

Doing F3 also dissolves several problems at no extra cost — once tiger2 is a real standby there
is no divergent instance to fail over to, and in the cold model its container is stopped, so
`:8123` refuses and reachability means leadership again.

---

## D2 — Sentinel is not being used

Decided 2026-08-26. The code path stays and is flagged untested in `backend.py`; H9 is paused.
No Sentinel runs anywhere on the fleet. Do not propose it, and do not report the untested
branch as a gap — it is marked, deliberately, in the code.

---

## D3 — The Valkey is dedicated, on node-ops, Tier-1

Decided 2026-08-26, spec in ADD 24 §4. Not a Tier-2 sidecar,
not shared with an existing instance.

---

## D4 — Cold standby is the recommended default

Warm remains selectable. This is a documented recommendation, not a constraint, and it will be
revisited **only** if the measured cold-boot RTO (H1) misses the 2.5-minute budget. Until that
measurement exists, proposing warm-by-default is re-litigating a decision without the evidence
that would change it.

---

## D5 — Secrets stay in `.storage`

Decided. Home Assistant has no encrypted config-entry store to adopt, and moving them out would
break the config flow's reconfigure path and put this integration out of step with every other
one. The mitigations are the Valkey ACL user (I4) and keeping backups and `.storage` in
different places — see ADD 03 §3.1. Do not propose
OpenBao or file-reference secrets for this integration.

---

## How to challenge something here

State the decision, state what you think is wrong with it, and give the evidence. That is a
short conversation. Re-filing it as a discovery is a long one.
