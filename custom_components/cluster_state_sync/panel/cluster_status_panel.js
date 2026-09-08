/*
 * Cluster status panel.
 *
 * Registered by the integration via panel_custom.async_register_panel, so it
 * appears in the sidebar on first setup with nothing for the operator to paste
 * and no node id to hand-edit. Entities are DISCOVERED, not named: every one
 * carries the id of the node that created it, so a hard-coded list would be
 * wrong on every installation but the one it was written on.
 *
 * Deliberately dependency-free: no build step, no framework, no imports. This
 * ships inside a HACS integration and has to keep working across frontend
 * versions, so it uses only custom elements and Home Assistant's own CSS
 * variables (which is what makes it theme correctly, light and dark).
 */

const FRESH = 900; // seconds; above this an age is worth a second look

/* Metric names, longest first.
 *
 * The node id is NOT separable by pattern. A node id like `node1-a1b2c3`
 * becomes `node1_a1b2c3` in an entity id, so it is indistinguishable from a
 * metric name once both are underscore-separated. A regex that split on the
 * first underscore produced node="node1", metric="a1b2c3_is_leader" and every
 * card rendered empty -- with no error anywhere.
 *
 * So the METRICS are the known quantity and the node is whatever precedes one.
 * Longest first, because `cluster_leader` must win before `is_leader` can be
 * tried against the same string.
 */
const METRICS = [
  "unreplicated_config_references",
  "shared_snapshot_age",
  "last_snapshot_age",
  "entities_restored",
  "fileset_degraded",
  "handover_request",
  "maintenance_hold",
  "entities_tracked",
  "cluster_members",
  "cluster_leader",
  "fileset_age",
  "clock_skew",
  "is_leader",
  "backend",
].sort((a, b) => b.length - a.length);

class ClusterStatusPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  /* Every entity this integration creates, grouped by the node that owns it.
   * Switch entities are included: the maintenance hold is a control, and it
   * belongs on the same page as the state it changes. */
  _byNode() {
    const nodes = {};
    for (const [id, st] of Object.entries(this._hass.states)) {
      const m = id.match(/^(?:binary_sensor|sensor|switch)\.(?:system_)?cluster_sync_(.+)$/);
      if (!m) continue;
      const rest = m[1];
      const metric = METRICS.find((k) => rest === k || rest.endsWith("_" + k));
      if (!metric) continue;
      const node = rest.slice(0, rest.length - metric.length).replace(/_+$/, "");
      const bucket = (nodes[node] = nodes[node] || {});
      // A switch and a binary_sensor can both be "maintenance_hold". Keep the
      // switch under its own key so the toggle is wired to the writable one.
      bucket[id.startsWith("switch.") ? `${metric}__switch` : metric] = st;
    }
    return nodes;
  }

  _toggleHold(entityId, turnOn) {
    this._hass.callService("switch", turnOn ? "turn_on" : "turn_off", {
      entity_id: entityId,
    });
  }

  connectedCallback() {
    // One delegated listener rather than one per render, so re-rendering on
    // every hass update cannot leak handlers.
    if (this._wired) return;
    this._wired = true;
    this.addEventListener("click", (ev) => {
      const hold = ev.target.closest("[data-hold-entity]");
      if (hold) {
        this._toggleHold(hold.dataset.holdEntity, hold.dataset.holdOn !== "true");
        return;
      }
      const act = ev.target.closest("[data-service]");
      if (act) this._callAction(act, act.dataset.service);
    });
  }

  /* Services run on the instance SERVING this page, not on whichever node's
   * card you happen to be looking at -- which is why the buttons live in their
   * own card rather than per node, where they would quietly mislead.
   *
   * Feedback is written onto the button itself. A control that gives no sign
   * it did anything invites a second click, and "flush again" is a wasted
   * round trip at best. */
  async _callAction(el, service) {
    const original = el.textContent.trim();
    el.disabled = true;
    el.textContent = "working…";
    try {
      await this._hass.callService("cluster_state_sync", service, {});
      el.textContent = "done";
    } catch (err) {
      el.textContent = `failed: ${err && err.message ? err.message : "see the log"}`;
    }
    setTimeout(() => {
      el.textContent = original;
      el.disabled = false;
    }, 2500);
  }

  _actionsCard(nodes) {
    // Only offer the acknowledgement when there is something to acknowledge.
    const anyDegraded = Object.values(nodes).some(
      (e) => e.fileset_degraded && e.fileset_degraded.state === "on"
    );
    return `
      <div class="card">
        <h2>Actions</h2>
        <p class="note" style="margin:0 0 10px">
          These run on the Home Assistant instance serving this page, whichever
          node that is — not on the node whose card you clicked from.
        </p>
        <div class="acts">
          <button class="toggle" data-service="flush_snapshot"
                  title="Write the shared snapshot now. Leader-gated: a follower is refused, because a follower writing is the split-brain this exists to prevent.">
            Flush snapshot now
          </button>
          ${
            anyDegraded
              ? `<button class="toggle on" data-service="clear_degraded"
                    title="Acknowledge the degraded go-bag marker. This does not repair anything — the next promotion is what proves the go-bag healthy — and it returns if the condition persists.">
                  Acknowledge degraded go-bag
                </button>`
              : ""
          }
        </div>
        <div class="legend">
          <i>safe</i> repeatable, no effect on leadership
          <i class="c">caution</i> changes how the cluster behaves
          <i class="d">moves the house</i> leadership changes machine
        </div>
      </div>`;
  }

  /* AR-0044. Everything below builds HTML as a template string and assigns it
   * to `innerHTML`, so any value interpolated into it is markup unless it is
   * escaped. That was fine while every value came from this node's own
   * entities. It stopped being fine when the cluster view started rendering
   * data the PEER writes: `sensor.<node>_cluster_leader` is
   * `coordinator.data.leader`, read straight out of the shared Valkey store,
   * and the node registry is not the sealed-blob channel. Valkey write access
   * was therefore enough to put markup into a page running with an
   * administrator's session.
   *
   * Escaping on the way in rather than switching to `textContent`: the card
   * bodies are assembled as strings by half a dozen helpers, and a partial
   * conversion is how you end up with one that was missed. */
  _esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  _fmtAge(st) {
    if (!st) return "—";
    const n = Number(st.state);
    if (Number.isNaN(n)) return st.state;
    if (n < 90) return `${Math.round(n)}s`;
    if (n < 5400) return `${Math.round(n / 60)}m`;
    return `${(n / 3600).toFixed(1)}h`;
  }

  _row(label, st, opts = {}) {
    if (!st) return "";
    const val = opts.age ? this._fmtAge(st) : st.state;
    let cls = "";
    if (opts.goodWhen) cls = st.state === opts.goodWhen ? "good" : "bad";
    if (opts.warnWhen && opts.warnWhen(st)) cls = "warn";
    return `<div class="row"><span>${this._esc(label)}</span><b class="${cls}">${this._esc(val)}</b></div>`;
  }

  /* The hold, as a control when the switch entity exists and as a readout when
   * it does not -- an older node that has not picked up the switch platform
   * yet must still show its state rather than a dead button. */
  _holdRow(e) {
    const sw = e.maintenance_hold__switch;
    const ro = e.maintenance_hold;
    if (!sw) return this._row("Maintenance hold", ro, { goodWhen: "off" });
    const on = sw.state === "on";
    return `<div class="row"><span>Maintenance hold</span>
      <button class="toggle ${on ? "on" : ""}"
              data-hold-entity="${this._esc(sw.entity_id)}" data-hold-on="${on}">
        ${on ? "SUSPENDED — click to resume" : "failover live — click to suspend"}
      </button></div>`;
  }

  /* Handing over is the one action here that moves the house between machines,
   * so it is coloured as such and only offered where it can actually do
   * something: a follower has no lease to give away, and a button that looks
   * live but is inert is how an operator learns to distrust the page. */
  _handoverRow(e) {
    const sw = e.handover_request__switch;
    if (!sw) return "";
    const leader = e.is_leader && e.is_leader.state === "on";
    const pending = sw.state === "on";
    if (!leader) {
      return `<div class="row"><span>Hand over to peer</span>
        <b class="bad">not the leader — nothing to hand over</b></div>`;
    }
    return `<div class="row"><span>Hand over to peer</span>
      <button class="toggle ${pending ? "pending" : "danger"}"
              data-hold-entity="${this._esc(sw.entity_id)}" data-hold-on="${pending}"
              title="Releases the lease so the peer promotes, and stops Home Assistant on this node. The peer has no radios unless hardware custody hooks are installed.">
        ${pending ? "REQUESTED — click to withdraw" : "hand over now"}
      </button></div>`;
  }

  _render() {
    if (!this._hass) return;
    const nodes = this._byNode();
    const names = Object.keys(nodes).sort();

    const body = names.length
      ? names
          .map((node) => {
            const e = nodes[node];
            const leader = e.is_leader && e.is_leader.state === "on";
            const held = e.maintenance_hold && e.maintenance_hold.state === "on";
            return `
      <div class="card ${leader ? "leader" : ""}">
        <h2>${this._esc(node)}${leader ? ' <span class="tag">LEADER</span>' : ""}${
              held ? ' <span class="tag hold">HOLD</span>' : ""
            }</h2>
        <div class="grid">
          <section>
            <h3>Leadership</h3>
            ${this._row("Is leader", e.is_leader, { goodWhen: "on" })}
            ${this._row("Cluster leader", e.cluster_leader)}
            ${this._row("Members seen", e.cluster_members)}
            ${this._holdRow(e)}
            ${this._handoverRow(e)}
          </section>
          <section>
            <h3>Freshness</h3>
            ${this._row("Our snapshot", e.last_snapshot_age, {
              age: true,
              warnWhen: (s) => Number(s.state) > FRESH,
            })}
            ${this._row("Shared snapshot", e.shared_snapshot_age, {
              age: true,
              warnWhen: (s) => Number(s.state) > FRESH,
            })}
            ${this._row("Config fileset", e.fileset_age, {
              age: true,
              warnWhen: (s) => Number(s.state) > FRESH,
            })}
            ${this._row("Fileset degraded", e.fileset_degraded, { goodWhen: "off" })}
          </section>
          <section>
            <h3>Coverage</h3>
            ${this._row("Entities mirrored", e.entities_tracked)}
            ${this._row("Restored at last start", e.entities_restored)}
            ${this._row("Config refs NOT replicated", e.unreplicated_config_references, {
              warnWhen: (s) => Number(s.state) > 0,
            })}
            ${this._row("Clock skew", e.clock_skew)}
          </section>
        </div>
      </div>`;
          })
          .join("")
      : `<div class="card"><h2>No cluster entities yet</h2>
         <p>This panel discovers entities named <code>cluster_sync_&lt;node&gt;_…</code>.
         If the integration has only just been added, give it one refresh cycle.</p></div>`;

    this.innerHTML = `
      <style>
        :host { display:block; }
        .wrap { padding:16px; max-width:1100px; margin:0 auto;
                font-family:var(--paper-font-body1_-_font-family, sans-serif);
                color:var(--primary-text-color); }
        .card { background:var(--card-background-color, #fff);
                border-radius:var(--ha-card-border-radius, 12px);
                box-shadow:var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
                padding:16px; margin-bottom:16px; }
        .card.leader { border-left:4px solid var(--success-color, #0b0); }
        h2 { margin:0 0 12px; font-size:1.15rem; display:flex; gap:8px; align-items:center; }
        h3 { margin:0 0 6px; font-size:.8rem; text-transform:uppercase;
             letter-spacing:.06em; color:var(--secondary-text-color); }
        .tag { font-size:.65rem; padding:2px 6px; border-radius:4px;
               background:var(--success-color, #0b0); color:#fff; letter-spacing:.05em; }
        .tag.hold { background:var(--warning-color, #e8a); color:#000; }
        .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(240px,1fr)); gap:20px; }
        .row { display:flex; justify-content:space-between; gap:12px;
               padding:4px 0; border-bottom:1px solid var(--divider-color, #eee); }
        .row b { font-variant-numeric:tabular-nums; }
        .good { color:var(--success-color, #0b0); }
        .bad  { color:var(--secondary-text-color); }
        .warn { color:var(--warning-color, #d80); }
        .note { font-size:.85rem; color:var(--secondary-text-color); line-height:1.5; }
        code { background:var(--secondary-background-color, #eee); padding:1px 4px; border-radius:3px; }
        .toggle { cursor:pointer; border:1px solid var(--divider-color,#ccc); border-radius:6px;
                  padding:3px 10px; font-size:.75rem; background:var(--card-background-color,#fff);
                  color:var(--primary-text-color); }
        .toggle.on { background:var(--warning-color,#e8a); color:#000; border-color:transparent; }
        .toggle[disabled] { opacity:.6; cursor:default; }
        .acts { display:flex; flex-wrap:wrap; gap:10px; }
        /* Colour encodes RISK, not decoration: neutral is safe and repeatable,
           caution changes cluster behaviour, danger moves the house. */
        .toggle.danger  { border-color:var(--error-color,#d33); color:var(--error-color,#d33); }
        .toggle.pending { background:var(--error-color,#d33); color:#fff; border-color:transparent; }
        .legend { display:flex; gap:14px; flex-wrap:wrap; font-size:.72rem;
                  color:var(--secondary-text-color); margin-top:10px; }
        .legend i { font-style:normal; padding:1px 7px; border-radius:4px;
                    border:1px solid var(--divider-color,#ccc); }
        .legend i.c { border-color:var(--warning-color,#e8a); color:var(--warning-color,#e8a); }
        .legend i.d { border-color:var(--error-color,#d33); color:var(--error-color,#d33); }
      </style>
      <div class="wrap">
        ${body}
        ${this._actionsCard(nodes)}
        <div class="card note">
          <b>Reading this page.</b>
          <b>Restored at last start</b> is what matters after a promotion — zero against a
          non-empty snapshot means every entry was skipped, and the log says why.
          A <b>snapshot age</b> climbing on a quiet cluster is normal: the flush skips when
          nothing has changed. <b>Maintenance hold</b> on means failover is suspended for
          that node — correct during planned work, wrong at any other time.
          <b>Config refs not replicated</b> above zero means a promoted node may not parse
          its own configuration.
        </div>
      </div>`;
  }
}

/* Guarded, and this is not defensiveness for its own sake.
 *
 * Home Assistant is a single-page app: navigating to a panel imports its
 * module into a page that may already have imported an earlier build of it --
 * which happens every time the module URL changes, i.e. every upgrade. A bare
 * `define` then throws NotSupportedError ("the name has already been used"),
 * the frontend catches it, and the user is told "Unable to load custom panel"
 * with no hint that the panel itself is fine and merely already loaded.
 *
 * Observed on this fleet after the cache-busting hash changed the URL.
 */
if (!customElements.get("cluster-status-panel")) {
  customElements.define("cluster-status-panel", ClusterStatusPanel);
}
