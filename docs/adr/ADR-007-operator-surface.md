# ADR-007: The operator surface — a self-registering panel, and controls as flag files

**Status:** Accepted — implemented
**Date:** 2026-09-07
**Deciders:** mmanning (project owner)

## Context

For most of this project's life the integration exposed twelve diagnostic entities and no way to
act on anything. Every control lived in a host script: `cluster-hold.sh` on the box, `force-master`
written by hand under `/run`. Whether a failover was healthy could only be answered by reading
logs on two machines.

That gap was not theoretical. On **2026-09-06** the owner pressed *Restart* in the Home Assistant
UI, looking for a panel. On the leader that is a full failover: the D3 probe fails, the promoter
releases the lease, `notify_backup.sh` stops the container, and `unless-stopped` does not restart a
container that was stopped deliberately. node-a stayed down, node-b promoted, and the kitchen
switch went dead because the radios do not move. **The flag that prevents this had existed for
days, in a file the person pressing Restart was not looking at.**

This ADR records the decisions behind the operator surface built in response.

## Decision

### 1. A self-registering custom panel, not a Lovelace dashboard

The integration registers its own sidebar panel via `panel_custom.async_register_panel` and serves
its JavaScript from inside itself with `hass.http.async_register_static_paths`.

**Rejected: shipping Lovelace YAML.** It works, and it is still shipped as
`dashboards/cluster-status.yaml` for anyone who wants the cards on their own dashboard. But it
requires pasting a file into the raw configuration editor **and hand-editing the node id out of
eleven entity names**, on every install. That is not a dashboard anyone gets by default.

**Rejected: creating a real Lovelace dashboard.** Home Assistant makes its own map dashboard this
way (`dashboards_collection.async_create_item`), so it demonstrably works. But `DashboardsCollection`
is a local inside `lovelace`'s `async_setup` and is not exposed on `hass.data`; reaching it means
another integration's internals. CONTRIBUTING.md is explicit that surviving minor
Home Assistant bumps is the thing this design exists to protect, and a sidebar convenience is not
worth spending that on.

**Rejected: a separate HACS frontend plugin.** Genuinely supported, and the obvious answer if the
integration did not already ship through HACS. It does, so the JavaScript travels with it — one
artefact, one version, no second thing to install.

**Consequence:** entities are **discovered, not named**. A hard-coded entity list would be correct
on exactly one installation. The panel parses node ids out of entity ids and renders one card per
node, which is also what makes it right on a three-node cluster.

### 2. Controls are flag files in the config directory

The maintenance hold and the handover request are both files in the Home Assistant configuration
directory: `.cluster_sync_hold` and `.cluster_sync_handover_request`.

The reason is that two processes must agree. The promoter runs **on the host**; Home Assistant runs
**in a container**. A file in the config directory is `/config/...` to one and the translated host
path to the other — one set of bytes, no IPC, no API, no shared secret. The host scripts and the
dashboard are two doors onto the same flag, and `cluster-hold.sh off` at a console leaves the
dashboard switch showing `off`, because the switch reads the file rather than remembering.

**Writes do not fail soft, though every read does.** `is_held` and `is_requested` return `False` on
any error: an unreadable flag must never suspend failover or move a house. `set_hold` and
`request_handover` raise: an operator who believes failover is suspended, and is wrong, will go on
to restart Home Assistant.

**The handover request is consumed; the hold is not.** A hold is a state you are in. A handover is
an event that happens once — a request that survived its own execution would hand the cluster over
again on the next tick, and again after that.

### 3. The controls are not diagnostic entities

`EntityCategory.DIAGNOSTIC` hides an entity from auto-generated dashboards and files it under a
device's diagnostics section. Every sensor here is diagnostic and should be. **The two switches are
not**, deliberately.

A control nobody can find is how the maintenance hold went unused through two outages. The cost is
that they appear on the default dashboard, which is why the integration also files its device under
a **Cluster** area (§5).

### 4. Colour encodes risk, and the legend states it

Three tiers, named on the page:

| tier | meaning | actions |
|---|---|---|
| safe | repeatable, no effect on leadership | flush snapshot |
| caution | changes how the cluster behaves | maintenance hold |
| moves the house | leadership changes machine | hand over to peer |

Colour that is decoration teaches nothing. A legend turns it into a claim the operator can rely on
under pressure — which is the only time these buttons get pressed.

**Handover is offered only on the leader.** A follower has no lease to give away. Its row says so
rather than showing a live-looking button that is inert, because a button that lies once is a page
that is never trusted again.

### 5. The device is filed under an area, and never re-filed

On setup the integration creates or reuses an area named **Cluster** and assigns its *device* —
entities inherit their device's area, so one assignment covers all fifteen and any added later.

**It never overrides an existing area.** Someone who filed this under "Loft" or "Servers" meant it,
and an integration that quietly moved things back on every restart would be worse than one that
never helped. Verified on this fleet: the device was already under "System", and setup left it
alone.

### 6. Everything in this ADR is best-effort

Panel registration, area assignment, and the cache-busting hash are each wrapped so that any
failure is logged once and setup continues. Imports for the frontend live *inside* the registration
function rather than at module scope, because `__init__.py` imports that module as the integration
loads — an ImportError at module scope would escape the guard entirely and fail the whole
integration to fail at putting an icon in a sidebar.

## Consequences

**Good.** The safeguard that prevents the 2026-09-06 outage is now one click, on the page where the
mistake is made. A deliberate handover no longer requires stopping Home Assistant on the leader —
the thing that *caused* that outage. Cluster health is one screen instead of two log streams.

**Costs.** The integration now ships JavaScript, which is a new class of thing to keep working. It
uses `panel_custom` and `frontend`, so it is coupled to two more public APIs than before. And the
module URL carries a content hash — without it, a corrected panel was deployed, served and verified
over HTTP three times while the browser kept running the previous build.

**Not solved.** There is no "take over" on a follower. Handover is always the leader giving up,
never the standby seizing. Forcing a promotion is `force-master` under `/run`, which the container
cannot write, and making it writable from the dashboard needs its own decision — a mis-click there
moves a house onto a node that, today, still has no radios.
