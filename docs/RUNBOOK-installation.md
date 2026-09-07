# Installation runbook

Two scenarios — new to Home Assistant, or already running it — and the
requirements that decide whether this will work for you at all. **Read section 1
before anything else.** It rules some setups out.

---

## 1. Will this work for me?

This ships in two halves, and they have very different requirements.

| Half | What it is | What it needs |
|---|---|---|
| **The integration** | The custom component. Mirrors entity state to a shared store and restores it at startup. | Any Home Assistant that accepts custom components. |
| **The host bundle** | Generated shell, systemd units and the promoter. Does the actual failover. | Root on the host, `systemd`, `python3` — and, *as generated today*, the `docker` CLI. |

### By installation type

| Your Home Assistant | Integration | Host bundle | Notes |
|---|---|---|---|
| **Container / Docker** | ✅ | ✅ | What this is built and tested against. |
| **Supervised** | ✅ | ⚠️ | You have host root, so the bundle *can* install — but the Supervisor also manages the Home Assistant container and will fight anything that stops it behind its back. Untested. |
| **Home Assistant OS** | ✅ | ❌ | No host shell, no `systemd` access, no `docker` CLI. You get state mirroring and restore-on-boot; **automatic failover is not possible.** |
| **Core (venv / bare metal)** | ✅ | ⚠️ | Not supported *today*, but nothing fundamental is in the way — see below. |

### Core on bare metal — why it is ⚠️ and not ❌

Bare metal has everything the host bundle actually needs: root, `systemd`,
`python3`. It is better placed than Supervised and far better placed than HA OS.
The blocker is narrower than "it will not work" — the generator only knows how to
emit `docker` commands, and there are exactly two places it does so:

| Where | What it emits | The bare-metal equivalent |
|---|---|---|
| `notify_master/backup/fault.sh` | `docker stop \| start homeassistant` | `systemctl stop \| start home-assistant@homeassistant` |
| `cluster-fileset-pull.sh` | `docker run --entrypoint python3 <ha-image>` | the Core venv's own `python3` |

The second is *easier* on bare metal, not harder. That container exists only to
borrow a Python that has `cryptography` — and a Core venv already has it, because
Home Assistant depends on it.

So this is a missing option in the generator (a "how do I restart Home Assistant"
choice, alongside the existing `NETWORK_MODE`), not a property of bare metal. If
you are on Core and want Level 4, that gap is the thing to raise — and it is
small.

**If you are on Home Assistant OS, stop at Level 2 below.** Everything up to
that point works and is useful. Nothing beyond it will.

### You also need

- **A Redis-compatible store.** Valkey 8.x or Redis 7+. See section 6 for where
  to put it — the answer is *not* "on one of the Home Assistant machines" if you
  want failover.
- **Two machines**, if you want failover. One machine still buys you something
  (Level 1), just not high availability.
- **The same clock**, roughly. Snapshot ages and lease TTLs are wall-clock. NTP
  on both is enough.

---

## 2. Decide how far you are going

You do not have to do all of this, and each level is useful on its own.

| Level | What you get | What it costs |
|---|---|---|
| **1 — one node** | Entity state survives restarts, restored before automations run. | Integration + a Redis/Valkey. Ten minutes. |
| **2 — two nodes** | Both mirror to a shared store. A standby can be promoted **by hand** and comes up with current state. | Second machine, same secret and namespace. |
| **3 — + go-bag** | The standby also inherits *identity* — users, tokens, integrations — so nobody has to log in again after a promotion. | Fileset replication on. More storage. |
| **4 — + host bundle** | **Unattended failover.** A promoter takes a lease and promotes the standby on its own. | Root and `systemd`. Not possible on HA OS; not yet wired up for Core (§1). |

Most people should do 1, live with it for a week, then decide.

---

## 3. Before you start — have you run Home Assistant before?

This is about **you**, not about any particular machine. A fresh machine at the
onboarding wizard is normal and expected later on; never having run Home
Assistant is a different problem.

**Do not try to install this alongside your first setup.** Get a working Home
Assistant first — one you have used, broken, fixed, and taken a backup of. This
integration replicates whatever you have; replicating an empty instance you do
not yet understand teaches you nothing and hides its own mistakes.

1. **Install Home Assistant normally.** If failover is your goal, choose the
   **Container/Docker** installation — today it is the only one the whole thing
   works on out of the box. **Avoid Home Assistant OS if failover matters to
   you**: it is the one option that closes Level 4 permanently, because there is
   no host shell to install anything into. Core on bare metal is a reasonable
   second choice — it needs one generator change, not a migration (§1).
2. **Use it for a week.** Add your devices. Build a dashboard. Take a backup and
   restore it once, so you know that works.
3. **Then continue below**, treating that instance as your primary.

> **A fresh machine is not this section.** If you already run Home Assistant and
> are building a *second* node, its brand-new install sitting on the onboarding
> wizard is exactly where it should be — that is Step 4, not this. Do not follow
> the "use it for a week, add your devices" advice above on a standby: the go-bag
> replaces its `.storage` wholesale at Level 3, so every one of those devices is
> work you would do twice.

---

## 4. Building the cluster

The normal case, whether you have one machine or two, and the order matters.
Steps 1–3 are everything a single node needs. Step 4 onwards adds the standby.

### Step 1 — stand up the store

Somewhere that is **not** either Home Assistant machine (section 6). Minimal
Valkey:

```yaml
services:
  valkey:
    image: valkey/valkey:8.1
    command: >-
      valkey-server --requirepass CHANGE-ME
      --maxmemory 1gb --maxmemory-policy noeviction --appendonly yes
    ports: ["6379:6379"]
    volumes: ["./data:/data"]
    restart: unless-stopped
```

`noeviction` is not optional if you go as far as Level 3: an eviction takes out
a blob the manifest still references, and the standby stages a broken go-bag.

### Step 2 — back up the primary, properly

Before the integration touches it.

**Use Home Assistant's own backup.** Settings → System → Backups → Create
backup. It is better than anything you can do with `tar` from outside, for one
specific reason: the recorder **locks the database for writes** for the duration
(`recorder/backup.py`, `async_pre_backup`). A raw `tar` of a live `/config`
copies SQLite while it is being written and can hand you a torn database that
looks fine until you restore it.

You can also **exclude the database**, which is usually right here — you are
protecting your *identity and configuration*, not your history, and it takes the
archive from gigabytes to a few hundred megabytes.

🚨 **Then copy it off the host.** Backups land in `/config/backups` by default —
on the machine you are about to modify. A backup that lives on the box it is
protecting is not a backup:

```bash
scp <primary>:/path/to/config/backups/<backup>.tar ./
```

**Also take a small, byte-exact copy of `.storage`.** It is about 100 MB, it
takes seconds, and it works even when Home Assistant will not start — which is
exactly when you will want it. `.storage` is the identity this integration
replicates, so it is the part worth having twice:

```bash
docker exec <ha-container> tar -C /config -cf - .storage | zstd -o storage-$(date +%F).tar.zst
```

🚨 **Run that `tar` inside the container.** A host-side `tar` as a normal user
silently skips root-owned `0600` files — including `.storage/auth`, which holds
every refresh token — and exits `0` with a plausible-looking archive. That
mistake produced a 415 MB backup containing no credentials at all on this
project once. Verify before trusting it: the archive must contain
`.storage/auth`, and that file must list your tokens.

### Step 2b — optional: prove the store on the standby first

Skip this if your primary is healthy and you are confident in the store.

Do it if either of these is true: **your primary is fragile in any way** — a
degraded array, a disk you do not trust, no recent restore test — or you simply
want to know the store works before touching anything that matters.

The idea is to spend the risk on the machine that has nothing to lose. Configure
the integration on the **standby** first, in a **throwaway namespace**, and check
one reading. It proves four things at once:

- the store's hostname resolves from the node
- TLS verifies against whatever CA you gave it
- the username and password actually work
- the integration loads and its entities appear

If any of those is wrong you find out on the machine you can rebuild, rather
than on the live one.

1. Get through the standby's Home Assistant onboarding (Step 4's note applies —
   do not invest in it).
2. Add the integration with your real store details, but **namespace `smoke`**,
   fileset **off**.
3. Check **Backend** reads `on`. That single reading is the whole test.
4. **Delete the config entry afterwards.** It rejoins properly at Step 4 with the
   real namespace.

`smoke` is isolated from your real namespace in the store, so nothing this writes
can reach production data. If you like, clear it up afterwards:

```bash
redis-cli ... --scan --pattern 'ha:cluster_state_sync:smoke:*' | xargs -r redis-cli ... DEL
```

> This is a pre-flight, not a scenario. It does not replace Step 3 — the primary
> still joins first, and for the reason given there: the go-bag flows leader →
> standby, so the node with real data must lead before anything can flow.

### Step 3 — the primary joins first

**Always the node with your real configuration, before the empty one.** The
go-bag flows leader → standby, so you want the node that has something worth
copying established as leader before anything can flow the other way.

Copy `custom_components/cluster_state_sync/` into your config directory, restart,
then **Settings → Devices & Services → + ADD INTEGRATION → "Cluster State Sync"**.

- Accept the no-warranty notice.
- **direct**, not sentinel.
- Host, port, username, password, TLS as your store requires.
- **Namespace**: anything; `prod` is fine. Write it down.
- **Cluster secret**: generate one and **store it somewhere you will find it
  again.** The second node needs the identical string.
- **Node ID**: leave the suggested default. It is `hostname-<6 chars of the
  install UUID>` and is unique per instance by construction. Two machines from
  one compose file share a hostname, and identical node IDs mean both halves of
  the cluster claim one identity.
- **Fileset replication: off** for now.

> **On the database number.** It is any of 0-15 and the integration defaults to
> **2**, not 0, on purpose: someone reusing an existing central Redis almost
> certainly has data in db 0 already, and the db number is how you keep this
> integration out of their way — the same isolation a separate database gives
> you in one PostgreSQL server. On a dedicated instance the number is arbitrary.
>
> What matters is that **both nodes use the same one**. Leaving the default is
> the least error-prone choice, because the second node's form pre-fills with it;
> pick anything else and you have to remember to change it twice. Get it wrong
> and both nodes report `Backend: on`, write to different databases, and never
> see each other.


Check **Backend** reads `on`. That one reading proves the host resolves, TLS
verifies, and the credentials work.

**Stop here for at least a day.** This is Level 1, it is doing something useful,
and if anything is wrong you want to find out now rather than with two nodes in
play.

### Step 4 — the second node

**This is where a fresh install belongs.** Build the machine, install Home
Assistant on it, and let it sit at its onboarding wizard — then work through the
wizard quickly and add the integration the same way as Step 3.

> **What you create during that onboarding is temporary.** At Level 3 the go-bag
> replicates `.storage`, which includes `auth`, `auth_provider.homeassistant` and
> `onboarding` — so the first promotion replaces this node's users, passwords and
> refresh tokens with the primary's. Afterwards you log in here with your
> **primary's** credentials. That is the feature working; it is not obvious from
> the wizard in front of you. Do not spend time on this instance's setup, and do
> **not** "restore from backup" during onboarding — that gives you two instances
> claiming one identity before anything exists to arbitrate.

Same namespace. **Same secret.** Different node ID (the default already is).
Fileset off **for now** — it goes on at Step 6, on this node too. See the note
there for why "primary only" is not the rule it looks like.

Optionally, prove the plumbing in a throwaway namespace like `smoke` first, then
switch to the real one — the second node starts publishing its own empty state
the moment the wizard finishes, and that does not belong in your real namespace.

### Step 5 — leadership

Both nodes default to `leadership_source: always`, meaning both write. Once both
are healthy, switch both to **`lease`**. From here only the lease holder writes,
and the split-brain guard is actually doing something.

### Step 6 — the go-bag (Level 3)

Enable fileset replication **on both nodes, primary first** — and only after
Step 5 has put both of them on `lease`.

> **This used to say "primary only", and that was wrong.** The setting has two
> jobs. It gates **publishing**, which is leader-only at runtime — so a standby
> holding no lease publishes nothing, and the split-brain the old wording feared
> cannot happen once Step 5 is done. It also gates **what the bundle emits**:
> `fileset_pull.py`, `cluster-fileset-swap.sh`, `cluster-fileset.key` and
> `cluster_promoter.py` all sit behind it. Leave it off on the standby and
> Step 7 installs a bundle that can neither receive the go-bag nor promote onto
> it — and the only symptom is a timer skipping quietly, once a minute, forever.
>
> Order still matters: turn it on at the primary, confirm it is publishing, then
> turn it on at the standby.

🚨 **This is the destructive step.** The go-bag replaces the standby's
`.storage` with the primary's. Enable it while leadership points the wrong way
and you overwrite your real node with your empty one. Everything before this is
reversible; this is not.

### Step 7 — the host bundle (Level 4)

Generate it from the wizard, copy to `/etc/cluster-sync/` on **both** hosts, read
the generated `INSTALL.md`, set `NETWORK_MODE`, enable the timers.

The first tick takes the lease, calls that a promotion, and runs
`notify_master.sh` — which stops Home Assistant, swaps `.storage` and starts it
again. Do this when you are in front of the machines, not on a Friday.

> The promoter ships **inside** the integration, at `scripts/cluster_promoter.py`.
> It is inert there: the integration reads it as text and emits it for you to
> install deliberately. Copying the integration does not install the promoter.

---

## 5. Why that order

| Rule | Because |
|---|---|
| Store before either node | Neither node completes setup without a reachable backend. It tests the connection before saving. |
| **Primary before secondary** | The go-bag flows leader → standby. The node with real data must be the established leader before anything can flow. |
| Backup before the primary | You are modifying a live system. |
| Smoke test on the standby *(optional, 2b)* | Spends the risk on the machine that has nothing to lose. Worth it when the primary is fragile. |
| `lease` after both are up | With one node configured, a lease just means "I lead", which proves nothing. |
| Go-bag after leadership | It is the destructive step, and it follows leadership. |
| Host bundle last | It is the only part that stops and starts Home Assistant on its own. |

---

## 6. Where does Valkey go?

**Not on either Home Assistant machine, if you want failover.**

The reason is simple and worth sitting with: if the store lives on node A and
node A dies — which is the exact case failover exists for — the survivor cannot
reach it. This integration **fails closed**: a backend it cannot reach is not a
yes, so no promotion happens. You have built a failover system that stops working
in precisely the situation it was built for.

| Placement | Verdict |
|---|---|
| Third machine (NAS, ops box, Pi) | ✅ Correct. It only needs to be reachable and small. |
| On one of the two nodes | ❌ Defeats the purpose, as above. |
| On both, replicated | ⚠️ Sentinel support exists in the code but is **deferred and untested**. Do not rely on it. |
| Same machine, single node (Level 1) | ✅ Fine. There is no failover to defeat. |

It does not need to be powerful. Budget roughly 1 GB of memory if you go to
Level 3, and rather less if you stop at Level 2.

---

## 7. Two failures that look like success

Both present as entirely green on both nodes, which is why they are worth
knowing in advance.

**Different cluster secrets.** Both nodes report `Backend: on` and mirror
happily, and neither can read the other's entries. The tell is `Entities
restored: 0` after a promotion, and a log line about entries skipped as
unreadable.

**Different namespaces.** Two perfectly healthy clusters of one node each. The
tell is `Cluster leader` naming *itself* on both nodes at once, with `Backend`
on.

If you take one thing from this document: **check the secret and the namespace
match before you debug anything else.**

---

## 8. How to find the values the wizard asks for

Most of the form is obvious. These are the ones people get stuck on, with the
command that answers each. Run them on the **host**, not inside Home Assistant.

### Home Assistant container name

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i home-assistant
```

Whatever is in the first column. It is often `homeassistant`, but not always —
one node in this project's own fleet calls it `home-assistant-2`, which is
exactly the sort of thing that makes a copied command fail confusingly.

### Home Assistant config path ON THE HOST

Not `/config` — that is the path *inside* the container. The wizard wants the
host directory behind it:

```bash
docker inspect <container> --format '{{range .Mounts}}{{.Type}} {{.Name}} {{.Source}} -> {{.Destination}}{{println}}{{end}}' | grep -i config
```

**Read the `Type` first, because `.Source` is only trustworthy for one of them.**

- `bind` — the answer is `.Source`. Done.
- `volume` — **`.Source` is a lie for this purpose.** It reports Docker's own
  bookkeeping mountpoint under the Docker root
  (`/var/lib/docker/volumes/<name>/_data`, or wherever `Docker Root Dir` points).
  Ask the volume where the data really is:

  ```bash
  docker volume inspect <name> --format '{{.Options}} {{.Mountpoint}}'
  ```

  A `local` volume declared with `o=bind` — which is what `docker compose`
  produces from a `driver_opts: {type: none, o: bind, device: /some/path}`
  block — has the real path in `Options.device`. **That** is the answer, not the
  mountpoint.

🚨 **Why this is not pedantry.** A volume's bookkeeping mountpoint exists only
while a container is using it. Docker mounts it on container start and unmounts
it on stop:

```
WHILE RUNNING   mount | grep _data  ->  1 line
docker stop <container>
WHILE STOPPED   mount | grep _data  ->  0 lines   # unmounted
```

The go-bag swap runs **while Home Assistant is stopped** — that is the whole
point of it. Give the wizard the bookkeeping path and `cluster-fileset-swap.sh`
will stop Home Assistant, write the promoted `.storage` into an empty directory
on whatever filesystem the Docker root lives on, and start Home Assistant again
— which re-binds the real volume and hides the write completely. The promotion
reports success, the logs are clean, and nothing was restored. Verify by
checking the answer holds when the container is *down*:

```bash
docker stop <container>
ls -d <your answer>/.storage && echo "OK — real path"
docker start <container>
```

**Also check for a `config` level, and do not assume it from the other node.**
Two hosts built from different compose files routinely differ by exactly one
path component — `/mnt/data/homeassistant` on one, `/mnt/data/homeassistant/config`
on the other. `.storage` is the marker: whichever directory directly contains
it is the answer.

### The user Home Assistant runs as (`ha_uid`)

```bash
docker exec <container> id
```

`uid=0(root)` means the wizard's default of 1000 is wrong for you. Getting this
wrong makes the generated bundle create files Home Assistant cannot read, and
the failure looks like the integration simply not appearing.

### The container's IP and docker network (warm standby only)

```bash
docker inspect <container> --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{$v.IPAddress}}{{println}}{{end}}'
docker inspect <container> --format '{{.HostConfig.NetworkMode}}'
```

The second one is also the answer to `NETWORK_MODE` in the generated scripts.

### Your Valkey or Redis details

Is it reachable, and does it want a password?

```bash
# plaintext instance
redis-cli -h <host> -p <port> PING          # PONG, or NOAUTH if it needs auth

# TLS-only instance — no redis-cli needed
openssl s_client -connect <host>:<port> -servername <host> </dev/null 2>/dev/null \
  | grep -E 'Verify return code|subject='
```

`NOAUTH Authentication required` means you need a username and password.
`Verify return code: 0 (ok)` means your CA trusts it; anything else means you
need to point the wizard at the right CA file.

### The TLS CA path

The wizard wants the path **inside the Home Assistant container**. The simplest
reliable answer is to put the CA under `/config`, because that directory is
already bind-mounted and needs no container recreate:

```bash
cat /path/to/your-ca.crt | docker exec -i <container> sh -c 'cat > /config/your-ca.crt'
```

then give the wizard `/config/your-ca.crt`. Verify it landed and parses:

```bash
docker exec <container> python3 -c \
  "from cryptography import x509; \
   print(x509.load_pem_x509_certificate(open('/config/your-ca.crt','rb').read()).subject.rfc4514_string())"
```

(The Home Assistant image has no `openssl` binary, which is why that check uses
Python.)

### Cluster secret

There is nothing to look up — you generate it, and it must be **identical** on
both nodes:

```bash
openssl rand -base64 32
```

Save it somewhere you will find it again before you type it in. A mismatch is
one of the two failures that look entirely green on both nodes (§7).

### Node identifier

Leave the suggested value. It is `hostname-<6 chars of the install UUID>` and is
unique per instance by construction. Two machines built from one compose file
share a hostname, and identical node ids mean both halves of the cluster claim
one identity.

### Peer host

Required by the form and currently **read by nothing** — a leftover from the
tier-1 rsync that ADR-006 retired. Put the other node's hostname; the form will
not submit while it is blank.
