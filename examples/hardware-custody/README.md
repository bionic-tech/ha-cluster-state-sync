# Hardware custody hooks

**Nothing in here is installed.** These are worked examples to copy, read, and
adapt. If your radios are passed straight through to the container — the common
case — you need none of it.

## When they run

The promoter provides two moments; you provide the script.

| directory | runs | for |
|---|---|---|
| `pre-start.d/` | on promotion, **before the device pre-flight and before `docker start`** | acquiring hardware |
| `post-stop.d/` | on demotion, **after the container has stopped** | releasing it |

Drop executables in `/etc/cluster-sync/pre-start.d/` and
`/etc/cluster-sync/post-stop.d/`. They run in `LC_ALL=C` sort order, each with a
60-second timeout.

**Why `pre-start.d/` runs before the pre-flight, not just before `docker start`.**
Two separate reasons, both load-bearing:

* A container's `/dev` is a snapshot taken when the container starts. A device
  attached afterwards never appears inside it, even for a privileged container
  (`docs/GOTCHAS.md` §17).
* The device pre-flight **disables config entries whose hardware is absent**.
  Claim after it and a promoted node cheerfully disables the radios it just
  acquired.

## The contract a hook must honour

* **Idempotent.** It will be run again. Claiming something already held is a
  success, not an error.
* **Wait for the device node.** Returning before `/dev/serial/by-id/...` exists
  defeats the ordering above. Poll; do not `sleep` and hope.
* **Non-zero only on real failure.** The exit status is the only thing the
  promoter can read.
* **Never interactive.** No prompts, no pager, no tty.
* **Bounded.** Finish well inside 60s. `pre-start.d/` delays the container start
  directly, so a slow hook is downtime.

## What happens when a hook fails

It is logged, the node is marked **DEGRADED**, and **promotion continues**.

That is deliberate, and it is the same rule as a stale go-bag (design D4). Your
network-attached devices — Tuya, Meross, Sonos, anything over MQTT — work
regardless of what happened to a radio. Refusing to promote because one radio
did not attach converts a partial outage into a total one.

## What a hook cannot do

**A device node is not a working radio.** Claiming a Zigbee coordinator gets you
`/dev/ttyACM0` and nothing else: you also need the daemon that speaks to it
(deCONZ, ZHA, zigbee2mqtt) running on the promoted node, with a matching
database. That is separate work.

**A dead node runs no hooks.** `post-stop.d/` covers an orderly demotion. The
case failover exists for — a node that is simply gone — releases nothing, and
recovery depends entirely on your provider reaping a dead client's claim. For
USB-over-IP that is the server noticing the TCP connection drop. **Measure it:
that number is your radio RTO**, and no hook can improve it.


## Keeping an adapted copy current

🚨 **Your copy is yours, and an upgrade never touches it.** These hooks live in
`/etc/cluster-sync/pre-start.d/` and `post-stop.d/`, which the bundle
deliberately does not manage — regenerating or reinstalling must never delete
an operator's own emergency hook.

The consequence is that improvements here do not reach you. When a release note
mentions a hook example, diff yours against it:

```bash
diff /etc/cluster-sync/post-stop.d/90-virtualhere-release.sh \
     examples/hardware-custody/virtualhere-release.sh
```

Known changes worth picking up:

| Release | Change |
|---|---|
| v0.4.1 | `virtualhere-release.sh` waits `RELEASE_WAIT` (default 30s, was a fixed 15s) and **exits 0** when devices linger. At 15s a *successful* handover was reported as `DEGRADED` — measured on a real failover, where the devices detached at about 21s |
