/*
 * PQCharge live dashboard (Track C, C-F6; redesigned 2026-10-14 for runbook v1.2).
 *
 * IN PLAIN WORDS
 *   Polls the server it was loaded from (GET only):
 *     /api/fleet      every 1 s   every charger + the migration status
 *     /api/events     every 0.5 s the live ticker, alerts, key-check times
 *     /api/health     every 5 s   mode, TLS, identity check, algorithm
 *   and draws the fleet as an animation:
 *
 *     - no charger yet          -> an empty "No chargers connected" radar
 *     - a charger connects      -> its tile pops in (any fleet size: one tile
 *                                  per charger the server reports, nothing hard-coded)
 *     - charging (classical)    -> green, with moving light
 *     - being migrated          -> blue, pulsing
 *     - post-quantum hybrid     -> purple + shield (glows while charging)
 *     - rolled back             -> burgundy, shakes once
 *     - legacy, skipped         -> grey, hatched ("L" before the migration)
 *     - offline                 -> dimmed, dashed
 *     - refused (attack)        -> red, shakes, banner + ticker line
 *     - canary charger          -> gold "C" badge; other waves "W1", "W2", ...
 *
 *   After a migration ends, a summary card appears (numbers from /api/migration
 *   and the events this page saw). The "Migration, live" chart counts the tile
 *   colours every second.
 *
 * ONE DISPLAY-ONLY RULE (said on the page too): the real orchestrator migrates a
 * wave in well under a second, faster than a 1 s poll can show. So the page
 * replays the waves in order, each shown blue for at least HOLD_MS. The summary
 * shows the server's own start/end times; nothing measured is changed.
 *
 * Every other number is the server's own; the page only formats it. Derived here,
 * and labelled so: "recovered" (Track A's is_recovered rule), the key-check
 * histogram and median (from events seen), and the tile colours.
 */
(function () {
  "use strict";

  var MAX_W = 7400;
  var TICKER_MAX = 80;
  var WINDOW_S = 600;            // charts keep the last 10 minutes
  var HOLD_MS = 1200;            // each wave is shown blue at least this long (display only)
  var WAVE_GAP_MS = 200;
  var ALERT_MS = 8000;           // a refused charger stays red this long
  var PI_BLINK_MS = 3000;        // agent/gpio_power.py MIGRATION_BLINK_S
  var PI_ID = "CP0000";          // the Raspberry Pi (Track A/B; tests/fixtures/fleet_profile_demo.json)
  var INTRUDER = "__intruder";   // the tile for TLS refusals (no charger id is known then)
  var PQ_ALG = "ML-DSA-44";
  var MLDSA = { pub: 1312, sig: 2420 };          // ML-DSA-44 sizes in bytes (FIPS 204)

  // Tile colours, in the order the legend and the timeline chart stack them.
  var VSTATES = ["charging", "idle", "migrating", "hybrid", "rolled", "legacy", "offline", "alert"];
  var V_LABEL = {
    charging: "charging (classical)", idle: "connected, idle", migrating: "migrating",
    hybrid: "post-quantum hybrid", rolled: "rolled back", legacy: "legacy (skipped)",
    offline: "offline", alert: "refused (attack)"
  };
  var V_ICON = { charging: "⚡", hybrid: "🛡", rolled: "↩", alert: "⛔", offline: "⏻", migrating: "⟳" };
  var STATE_LABEL = { pending: "waiting", in_progress: "upgrading", migrated: "upgraded (hybrid)",
                      rolled_back: "rolled back", incompatible: "incompatible (legacy)" };
  var PHASE_WORDS = {
    idle: "No migration running. Set the canary and wave size, then press Start.",
    canary: "Testing on the canary group (gold C) first.",
    running: "Upgrading the fleet in waves.",
    completed: "Finished: every eligible charger upgraded.",
    rolled_back: "A wave failed its checks and was rolled back. The migration stopped (halt).",
    failed: "The migration controller stopped with an error."
  };
  var ROT_WORDS = {
    canary: "Rotating keys on the canary group first. Old keys stay valid until the new one is proven.",
    running: "Rotating keys in waves. Old keys stay valid until the new one is proven.",
    completed: "Key rotation finished.",
    rolled_back: "A rotation wave failed: those chargers kept their old keys."
  };

  var S = {
    health: null, fleet: null, mig: null,
    reachable: null, lastSeq: 0, eventsApi: null,  // null = unknown, true/false after first try
    prev: {}, firstPoll: true,
    power: [], timeline: [], marks: [], lat: [], checks: [],
    selected: null, tiles: {}, order: [], charts: {},
    seen: {},                  // charger id -> true once this page saw it connected
    alerts: {},                // charger id -> {until, words, at}
    refused: {},               // charger id -> how many times refused
    intruder: { count: 0, until: 0, words: "", detail: "", code: "", at: null },
    arrive: {}, lastShown: {}, shownCounts: {},
    piBlinkUntil: 0,
    play: { mid: null, skip: false, showAt: {}, showEnd: {}, lastEnd: 0, done: {} },
    runs: {},                  // migration id -> {startCharging}
    waveOf: {}, canary: {}, waveRunning: {},
    dense: false, wavesTouched: false
  };

  // ---------------------------------------------------------------- utils
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v, d) { return typeof v === "number" && isFinite(v) ? v.toFixed(d == null ? 0 : d) : "—"; }
  function kw(w) { return typeof w === "number" ? (w / 1000).toFixed(1) + " kW" : "—"; }
  function short(id) { return String(id || "").replace(/^CP0*/, "").replace(/^CP-/, "") || id; }
  function kid(k) { return k ? String(k).slice(0, 8) + "…" : "—"; }
  function now() { return Date.now(); }
  function hhmmss(iso) {
    var d = iso ? new Date(iso) : new Date();
    return d.toTimeString().slice(0, 8);
  }
  function median(xs) {
    if (!xs.length) return null;
    var s = xs.slice().sort(function (a, b) { return a - b; });
    return s[Math.floor((s.length - 1) / 2)];
  }
  function getJSON(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        return { status: r.status, ok: r.ok, body: body };
      });
    });
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function vColor(v) { return css("--v-" + v); }

  function isPi(st) {
    // measured_mw must be a NUMBER: the server sends measured_mw: null for
    // every simulated charger (A-F10, L37), and those must not turn into Pis.
    return st.station_id === PI_ID || typeof st.measured_mw === "number" ||
      st.power_source === "scaled";
  }
  function recovered(st) {
    // Track A's StationView.is_recovered (Contract 7 section 7.7), restated for display only.
    return st.connection_state === "connected" && st.boot_accepted &&
      (st.pq_verified || !st.pq_check_required);
  }
  function legacyCapable(st) {
    var algs = st.supported_algorithms || [];
    return algs.length > 0 && algs.indexOf(PQ_ALG) < 0;
  }
  function stationOf(ev) {
    return ev.station_id || (ev.payload || {}).station || null;
  }
  function alertOn(sid) {
    var a = S.alerts[sid];
    return !!a && a.until > now();
  }
  function terminal(phase) { return phase === "completed" || phase === "rolled_back" || phase === "failed"; }

  // ---------------------------------------------------------------- polling
  function pollFleet() {
    getJSON("/api/fleet").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      S.reachable = true;
      var fleet = r.body;
      var mig = fleet.migration && fleet.migration.phase ? Promise.resolve(fleet.migration)
        : getJSON("/api/migration").then(function (m) { return m.ok ? m.body : null; });
      return mig.then(function (m) {
        noteArrivals(fleet);
        if (S.eventsApi === false) diffTicker(fleet);
        S.fleet = fleet; S.mig = m;
        notePlayback(m, fleet);
        var t = now();
        S.power.push([t, (fleet.aggregate_power_w || 0) / 1000]);
        trim(S.power, t);
        renderAll();
        var row = [t];
        VSTATES.forEach(function (v) { row.push(S.shownCounts[v] || 0); });
        S.timeline.push(row);
        trim(S.timeline, t);
        renderTimeline();
      });
    }).catch(function (e) {
      S.reachable = false;
      $("serverLine").textContent = "server not reachable (" + e.message + ") — retrying…";
      renderHeader();
      if (S.fleet) {                 // server down (V4 storm): every tile goes offline
        renderGrid();
        var t = now(), row = [t];
        VSTATES.forEach(function (v) { row.push(S.shownCounts[v] || 0); });
        S.timeline.push(row); trim(S.timeline, t); renderTimeline();
      }
    });
  }
  function trim(series, t) {
    var cut = t - WINDOW_S * 1000;
    while (series.length && series[0][0] < cut) series.shift();
  }

  function pollEvents() {
    if (S.eventsApi === false) return;
    getJSON("/api/events?after=" + S.lastSeq + "&limit=500").then(function (r) {
      if (r.status === 404) {
        S.eventsApi = false;
        $("tickSrc").textContent = "from fleet changes (/api/events not on this server yet)";
        return;
      }
      if (!r.ok) return;
      if (S.eventsApi !== true) { S.eventsApi = true; $("tickSrc").textContent = "from /api/events"; }
      var evs = r.body.events || [];
      if (!S.lastSeq && evs.length > 20) {
        // First look at a server that has been running: keep every key-check
        // time, but no alerts, and only the latest 20 lines in the ticker.
        evs.slice(0, -20).forEach(function (ev) { onEvent(ev, 2); });
        evs = evs.slice(-20);
      }
      var mode = S.lastSeq ? 0 : 1;   // nothing from before the page opened turns a tile red
      evs.forEach(function (ev) { onEvent(ev, mode); });
      if (typeof r.body.last_seq === "number") S.lastSeq = r.body.last_seq;
      else if (evs.length) S.lastSeq = evs[evs.length - 1].seq || S.lastSeq;
      if (!S.lastSeq) S.lastSeq = -1;   // "looked once" even on an empty server
      renderLatency();
    }).catch(function () { /* the fleet poll reports reachability */ });
  }

  function pollHealth() {
    getJSON("/api/health").then(function (r) {
      if (!r.ok) return;
      S.health = r.body;
      var mock = !!r.body.mock;
      $("mockBadge").hidden = !mock;
      Array.prototype.forEach.call(document.querySelectorAll(".mockOnly"), function (el) { el.hidden = !mock; });
      renderHeader();
    }).catch(function () {});
  }

  // ---------------------------------------------------------------- arrivals
  // Which chargers have connected, and who started charging, since the last poll.
  function noteArrivals(fleet) {
    var stations = fleet.stations || [];
    var joined = [], charging = [];
    stations.forEach(function (st) {
      var sid = st.station_id;
      var on = st.connection_state === "connected";
      // First poll after (re)loading the page: a charger the server knows as
      // "disconnected" did connect earlier, so it gets its (dim) tile too.
      if (S.firstPoll && st.connection_state === "disconnected") S.seen[sid] = true;
      if (on && !S.seen[sid]) { S.seen[sid] = true; if (!S.firstPoll) joined.push(sid); }
      var old = S.prev[sid];
      var isCh = on && st.charging_state === "Charging";
      if (!S.firstPoll && isCh && !(old && old.charging)) charging.push(sid);
      S.prev[sid] = { migration_state: st.migration_state, pq_verified: st.pq_verified,
                      connection_state: st.connection_state, charging: isCh };
    });
    if (joined.length) tick("info", "🔌 " + group(joined) + " connected (TLS + boot accepted)");
    if (charging.length) tick("charge", "⚡ " + group(charging) + " started charging");
    S.firstPoll = false;
  }
  function group(ids) {
    if (ids.length === 1) return "<b>" + esc(ids[0]) + "</b>";
    if (ids.length <= 4) return ids.map(function (i) { return "<b>" + esc(i) + "</b>"; }).join(", ");
    return "<b>" + ids.length + " chargers</b> (" + esc(ids[0]) + " … " + esc(ids[ids.length - 1]) + ")";
  }

  // ---------------------------------------------------------------- playback
  // Which wave each charger is in, and when each wave may be shown blue.
  function notePlayback(m, fleet) {
    m = m || {};
    var mid = m.migration_id || "";
    S.waveOf = {}; S.canary = {}; S.waveRunning = {};
    (m.waves || []).forEach(function (w) {
      if (w.phase === "running") S.waveRunning[w.wave_id] = true;
      (w.station_ids || []).forEach(function (sid) {
        S.waveOf[sid] = w.wave_id;
        if (w.is_canary) S.canary[sid] = true;
      });
    });
    if (mid && mid !== S.play.mid) {
      // A new run: start a fresh replay. If the page first sees it already
      // finished (page reloaded later), skip the replay.
      var phase = String(m.phase || "").toLowerCase();
      // A charger already shown post-quantum (or skipped as legacy) before
      // this run keeps its colour: a second run (a late joiner, V3) only
      // replays the chargers it actually changes.
      var done = {};
      Object.keys(S.lastShown).forEach(function (sid) {
        if (S.lastShown[sid] === "hybrid" || S.lastShown[sid] === "legacy") done[sid] = true;
      });
      S.play = { mid: mid, skip: terminal(phase), showAt: {}, showEnd: {}, lastEnd: 0, done: done };
      if (!S.runs[mid]) {
        S.runs[mid] = { startCharging: terminal(phase) ? null : fleet.charging_count,
                        sawStart: !terminal(phase) };
      }
    }
  }
  function postState(t) { return t === "hybrid" || t === "rolled" || t === "legacy"; }
  function ensureWave(w, t) {
    if (S.play.showAt[w] != null) return;
    var start = Math.max(t, S.play.lastEnd ? S.play.lastEnd + WAVE_GAP_MS : t);
    S.play.showAt[w] = start;
    S.play.showEnd[w] = start + HOLD_MS;
    S.play.lastEnd = S.play.showEnd[w];
  }
  function replaying() { return !!S.play.mid && !S.play.skip && now() < S.play.lastEnd; }
  function playingWave() {
    var t = now(), best = null;
    Object.keys(S.play.showAt).forEach(function (w) {
      if (S.play.showAt[w] <= t && (best === null || +w > best)) best = +w;
    });
    return best;
  }

  // The colour the server's data says (no replay).
  function target(st) {
    var sid = st.station_id;
    if (alertOn(sid)) return "alert";
    if (S.reachable === false) return "offline";            // the server itself is down
    if (st.connection_state !== "connected") return "offline";
    switch (st.migration_state) {
      case "in_progress": return "migrating";
      case "rolled_back": return "rolled";
      case "incompatible": return "legacy";
      case "migrated": return "hybrid";
    }
    return st.charging_state === "Charging" ? "charging" : "idle";
  }
  function preState(st) { return st.charging_state === "Charging" ? "charging" : "idle"; }

  // The colour to draw now (with the wave replay).
  function shown(st, t) {
    var v = target(st), sid = st.station_id, w = S.waveOf[sid];
    if (v === "alert" || v === "offline") return v;
    if (S.play.skip || !S.play.mid || w == null || S.play.done[sid]) return v;
    if (S.mig && S.mig.kind === "rotation") return v;
    if (!(v === "migrating" || postState(v) || S.waveRunning[w])) return v;
    ensureWave(w, t);
    if (t < S.play.showAt[w]) return preState(st);              // an earlier wave is still on screen
    if (v === "migrating") return "migrating";                   // the server says so: real blue
    if (!postState(v)) return "migrating";                       // its wave is running on the server
    if (v === "legacy") { S.play.done[sid] = true; return v; }    // skipped, never blue
    if (t < S.play.showEnd[w]) return "migrating";
    S.play.done[sid] = true;
    return v;
  }
  function preparePlayback(stations) {
    // Claim replay slots wave by wave, in wave order.
    if (S.play.skip || !S.play.mid || (S.mig && S.mig.kind === "rotation")) return;
    var t = now(), need = {};
    stations.forEach(function (st) {
      var w = S.waveOf[st.station_id], v = target(st);
      if (w == null || S.play.done[st.station_id]) return;
      if (v === "migrating" || postState(v) || S.waveRunning[w]) need[w] = true;
    });
    Object.keys(need).map(Number).sort(function (a, b) { return a - b; })
      .forEach(function (w) { ensureWave(w, t); });
  }

  // ---------------------------------------------------------------- ticker + alerts
  function tick(cls, html, at) {
    var li = document.createElement("li");
    li.className = cls;
    li.innerHTML = '<span class="t">' + hhmmss(at) + "</span>" + html;
    var ol = $("ticker");
    ol.insertBefore(li, ol.firstChild);
    while (ol.children.length > TICKER_MAX) ol.removeChild(ol.lastChild);
  }

  function flash(sid, good) {
    var el = S.tiles[sid];
    if (!el) return;
    el.classList.remove("flash-good", "flash-bad");
    void el.offsetWidth;                               // restart the animation
    el.classList.add(good ? "flash-good" : "flash-bad");
  }

  var bannerTimer = null;
  function banner(html) {
    var b = $("alertBar");
    b.hidden = true;
    void b.offsetWidth;
    b.innerHTML = html;
    b.hidden = false;
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(function () { b.hidden = true; }, 6000);
  }

  // count=false: the same refusal seen a second time (the 1008 close that
  // follows a failed boot key check) -- refresh the red tile, do not count twice.
  function raise(sid, words, at, count) {
    S.alerts[sid] = { until: now() + ALERT_MS, words: words, at: at || new Date().toISOString() };
    if (count !== false || !S.refused[sid]) S.refused[sid] = (S.refused[sid] || 0) + 1;
    S.seen[sid] = true;            // a refused charger gets a tile even if it never connected
    banner("⛔ <b>" + esc(sid) + "</b> refused — " + esc(words));
    renderGrid();
  }

  // mode 0: live. 1: from before the page opened (ticker line, no alert).
  // 2: old backlog (only the key-check times are kept).
  function onEvent(ev, mode) {
    var p = ev.payload || {}, sid = stationOf(ev), t = ev.timestamp, type = ev.event_type;
    var quiet = mode === 1;
    function raiseLive(s2, words, count) { if (!quiet) raise(s2, words, t, count); }
    function bannerLive(html) { if (!quiet) banner(html); }
    var why = p.trigger ? " <span class='sub'>(" + esc(p.trigger) + ")</span>" : "";
    var outcome = ev.outcome || p.result;

    if (type === "connection_attempt" && p.transition === "pq_auth") {
      var ok = outcome === "success";
      if (typeof p.duration_ms === "number") {
        if (ok) S.lat.push(p.duration_ms);
        S.checks.push({ at: t ? Date.parse(t) : now(), ms: p.duration_ms, ok: ok, trigger: p.trigger });
      }
      if (mode === 2) return;
      flash(sid, ok);
      tick(ok ? "good" : "bad", ok
        ? "✓ <b>" + esc(sid) + "</b> " + esc(p.algorithm || "ML-DSA") + " signature verified · " +
          num(p.duration_ms, 0) + " ms · key " + esc(kid(p.key_id)) + why
        : "✗ <b>" + esc(sid) + "</b> key check FAILED — " + esc(p.detail || "rejected") + why, t);
      // A failed BOOT check is an attack (stolen certificate without the key).
      // A failed migration/rotation check is a refusing charger: the wave logic handles it.
      if (!ok && p.trigger === "boot" && sid) raiseLive(sid, "failed its post-quantum key check (no " + PQ_ALG + " key) → cut off");
      return;
    }
    if (mode === 2) return;
    if (type === "connection_failed" && p.reason === "tls_rejected") {
      if (quiet) { tick("bad", "⛔ Unknown device refused during TLS — " + esc(p.detail || ""), t); return; }
      // Server change 2026-10-14 (csms/transport.py observe_tls_refusals): the
      // TLS handshake was refused before any charger id was sent, so all we know
      // is WHY. One "unknown device" tile counts these.
      var I = S.intruder;
      I.count += 1; I.until = now() + ALERT_MS; I.words = p.detail || "TLS handshake refused";
      I.detail = p.tls_detail || ""; I.code = p.tls_error || ""; I.at = t;
      tick("bad", "⛔ Unknown device refused during TLS — " + esc(I.words), t);
      banner("⛔ Unknown device refused at the TLS handshake — " + esc(I.words) +
        ". It never reached OCPP, so no charger id is known.");
      renderGrid();
      return;
    }
    if (type === "connection_attempt" && p.transition === "identity_check" &&
        (ev.outcome === "rejected" || p.identity_matches === false)) {
      var cn = p.certificate_common_name || "another charger";
      tick("bad", "✗ <b>" + esc(sid) + "</b> refused: it presented the certificate of " + esc(cn), t);
      raiseLive(sid, "wrong identity: it presented the certificate of " + cn);
      return;
    }
    if (type === "certificate_installed" && p.transition === "pq_enrolled") {
      if (sid === PI_ID) S.piBlinkUntil = now() + PI_BLINK_MS;
      tick("info", "🔑 <b>" + esc(sid) + "</b> made its own " + esc(p.algorithm || "ML-DSA") +
        " key · key " + esc(kid(p.key_id)) + " (private key never left the charger)", t);
      return;
    }
    if (type === "connection_closed" && p.reason === "pq_auth_failed") {
      tick("bad", "⛔ <b>" + esc(sid) + "</b> cut off (code 1008): failed its post-quantum key check", t);
      if (sid) raiseLive(sid, "failed its post-quantum key check → cut off (1008)", false);
      return;
    }
    if (type === "rotation_completed" && sid) {
      flash(sid, true);
      tick("rot", "↻ <b>" + esc(sid) + "</b> key rotated · " + esc(kid(p.old_key_id)) + " → " +
        esc(kid(p.new_key_id)) + " · no disconnect", t);
      return;
    }
    if (type === "rotation_failed" && sid) {
      tick("bad", "↻ <b>" + esc(sid) + "</b> rotation failed — old key " + esc(kid(p.old_key_id)) + " kept", t);
      return;
    }
    if (type === "station_deferred") {
      tick("warn", "… <b>" + esc(sid) + "</b> offline: skipped for now (not a failure)", t);
      return;
    }
    if (sid) return;                                   // other per-charger traffic: too noisy
    var rotating = p.kind === "rotation";              // Track B B-F2: rotation runs reuse the migration lines
    var line = {
      migration_started: rotating
        ? ["rot", "↻ Key rotation started (" + esc(p.total) + " chargers) — old keys stay valid until the new one is proven"]
        : ["info", "▶ Migration started → " + esc(p.target_mode || "") + " (" + esc(p.total) + " chargers)"],
      wave_started: ["info", (p.is_canary ? "Canary" : "Wave " + esc(p.wave_id)) + " started (" + esc(p.size) + ")"],
      wave_completed: ["good", (p.wave_id === 0 ? "Canary" : "Wave " + esc(p.wave_id)) + " done · " + esc(p.migrated) + " ok, " + esc(p.failed || 0) + " failed"],
      wave_rolled_back: ["bad", "Wave " + esc(p.wave_id) + " ROLLED BACK" + (p.trigger === "manual" ? " (by operator)" : " — too many failures, migration halted")],
      migration_completed: rotating ? ["rot", "■ Key rotation completed"] : ["good", "■ Migration completed"],
      migration_failed: ["bad", "Migration controller error: " + esc(p.error || "")],
      server_started: ["info", "Server started"],
      server_stopping: ["warn", "Server stopping"]
    }[type];
    if (line) tick(line[0], line[1], t);
    var mark = { migration_started: rotating ? "rotate" : "start", wave_rolled_back: "rollback",
                 migration_completed: rotating ? "rotated" : "done" }[type];
    if (mark) S.marks.push([t ? Date.parse(t) : now(), mark]);
    if (type === "wave_rolled_back") bannerLive("↩ Wave " + esc(p.wave_id) + " rolled back" +
      (p.trigger === "manual" ? " by the operator" : " — more than 20 % failed; the migration halted"));
  }

  // Before A-F4: what changed between two fleet polls.
  function diffTicker(fleet) {
    var t = new Date().toISOString();
    (fleet.stations || []).forEach(function (st) {
      var old = S.prev[st.station_id];
      if (!old || old.migration_state === st.migration_state) return;
      var good = st.migration_state === "migrated", bad = st.migration_state === "rolled_back";
      if (good || bad || st.migration_state === "incompatible") {
        tick(good ? "good" : bad ? "bad" : "info", (good ? "✓ " : bad ? "✗ " : "") + "<b>" +
          esc(st.station_id) + "</b> " + esc(STATE_LABEL[old.migration_state]) + " → " +
          esc(STATE_LABEL[st.migration_state]), t);
      }
    });
  }

  // ---------------------------------------------------------------- header
  function chip(cls, k, v) { return '<span class="chip ' + cls + '">' + esc(k) + " <b>" + v + "</b></span>"; }

  function renderHeader() {
    var h = S.health || {}, f = S.reachable === false ? {} : (S.fleet || {});   // server down: no stale numbers
    var stations = f.stations || [];
    var mode = h.crypto_mode || f.crypto_mode || "—";
    var tlsOn = typeof h.tls === "boolean" ? h.tls : stations.some(function (s) { return s.tls_version; });
    var tlsVer = (stations.filter(function (s) { return s.tls_version; })[0] || {}).tls_version;
    var idc = h.identity_check;
    $("chips").innerHTML =
      chip("mode-" + mode, "Mode", esc(mode)) +
      chip(tlsOn ? "ok" : "", "TLS", tlsOn ? "🔒 " + esc(tlsVer || "on") : "off") +
      chip(idc === "enforce" ? "ok" : idc === "off" ? "bad" : "", "Identity check", esc(idc || "—")) +
      chip("", "Post-quantum", esc(h.pq_algorithm || "—")) +
      chip(h.boot_check === true ? "ok" : "", "Boot key check",
        h.boot_check === true ? "on" : h.boot_check === false ? "off" : "—");
    var connected = stations.filter(function (s) { return s.connection_state === "connected"; });
    var pq = connected.filter(function (s) { return s.migration_state === "migrated"; }).length;
    var refused = Object.keys(S.refused).reduce(function (a, k) { return a + S.refused[k]; }, 0) + S.intruder.count;
    $("counts").innerHTML = [
      ["", "Connected", connected.length],
      ["c-charging", "Charging", num(f.charging_count)],
      ["c-hybrid", "Post-quantum", pq],
      ["", "Recovered", stations.filter(recovered).length],
      [refused ? "c-alert" : "", "Refused", refused],
      ["", "Fleet power", kw(f.aggregate_power_w)]
    ].map(function (c) {
      return '<div class="count ' + c[0] + '"><div class="v">' + c[2] + '</div><div class="k">' + c[1] + "</div></div>";
    }).join("");
    if (S.reachable) {
      $("serverLine").textContent = "server run " + (f.run_id || h.run_id || "?") +
        (h.migration ? " · " + h.migration : "") + " · updated " + hhmmss();
    }
  }

  // ---------------------------------------------------------------- stepper
  function renderStepper() {
    var m = S.mig || {}, f = S.fleet || {};
    var phase = displayPhase();
    var rotation = m.kind === "rotation";
    var anyOn = (f.stations || []).some(function (s) { return s.connection_state === "connected"; });
    var steps = rotation ? ["Connect", "Charge", "Rotate canary", "Rotate waves", "Rotated"]
                         : ["Connect", "Charge", "Canary", "Waves", "Hybrid"];
    var at = !anyOn ? 0 : phase === "canary" ? 2 : phase === "running" ? 3 : terminal(phase) ? 4
      : (f.charging_count ? 1 : 0);
    var halt = phase === "rolled_back" || phase === "failed";
    if (halt) steps[4] = phase === "failed" ? "Error" : "Halted";
    $("stepper").innerHTML = steps.map(function (s, i) {
      var cls = i < at ? "done" : i === at ? "now" + (i === 4 ? (halt ? " halt" : " hybrid") : "") : "";
      return '<li class="' + cls + '">' + esc(s) + "</li>";
    }).join("");
  }
  function displayPhase() {
    var m = S.mig || {};
    var phase = String(m.phase || "idle").toLowerCase();
    if (terminal(phase) && replaying()) return playingWave() ? "running" : "canary";
    return phase;
  }

  // ---------------------------------------------------------------- fleet grid
  // 6c (500-charger demo): above DENSE_FROM chargers each charger is a small
  // colour square, so the whole fleet -- and a wave sweeping across it -- fits
  // on one projector screen. The Pi keeps its large tile; click still inspects.
  var DENSE_FROM = 100;
  var DENSE_WAVES = { canary: 10, wave: 50 };   // the runbook's 500-charger settings

  function visibleStations() {
    var stations = (S.fleet || {}).stations || [];
    var have = {};
    var vis = stations.filter(function (st) {
      have[st.station_id] = true;
      return S.seen[st.station_id] || alertOn(st.station_id);
    });
    // A refused id the server does not list (not in its fleet profile).
    Object.keys(S.alerts).forEach(function (sid) {
      if (!have[sid]) vis.push({ station_id: sid, connection_state: "never_seen", migration_state: "pending" });
    });
    return vis.sort(function (a, b) {
      return (isPi(b) - isPi(a)) || String(a.station_id).localeCompare(String(b.station_id));
    });
  }

  function makeTile(sid) {
    var b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "listitem");
    b.dataset.sid = sid;
    b.addEventListener("click", function () { openInspector(sid); });
    S.tiles[sid] = b;
    S.arrive[sid] = { cls: "enter", until: now() + 600 };
    return b;
  }

  function syncTiles(ids) {
    // New tiles are inserted in order; existing tiles never move (moving a node
    // would restart its animation). Tiles of chargers that vanished are removed.
    var grid = $("grid");
    var want = {};
    ids.forEach(function (sid) { want[sid] = true; });
    Object.keys(S.tiles).forEach(function (sid) {
      if (!want[sid]) { grid.removeChild(S.tiles[sid]); delete S.tiles[sid]; }
    });
    var next = null;
    for (var i = ids.length - 1; i >= 0; i--) {
      var sid = ids[i];
      if (!S.tiles[sid]) grid.insertBefore(makeTile(sid), next);
      next = S.tiles[sid];
    }
  }

  // 6c: during (and after) a key rotation, which chargers already proved their
  // NEW key in this rotation run -- from last_pq_check (A-F4), so it needs
  // no extra server field. "ok" = rotated, "bad" = rotation failed (old key kept).
  function rotationResult(st, m) {
    if (!m || m.kind !== "rotation" || !m.started_at) return null;
    var c = st.last_pq_check;
    if (!c || c.trigger !== "rotation" || !c.at || new Date(c.at) < new Date(m.started_at)) return null;
    return c.result === "success" ? "ok" : "bad";
  }

  function renderGrid() {
    var stations = visibleStations();
    var ids = stations.map(function (s) { return s.station_id; });
    var hasIntruder = S.intruder.count > 0;
    if (hasIntruder) ids.unshift(INTRUDER);
    var grid = $("grid");
    var dense = stations.length > DENSE_FROM;
    grid.classList.toggle("dense", dense);
    if (dense && !S.dense && !S.wavesTouched) {
      // Large fleet: suggest the runbook's canary and wave size, unless someone already changed them.
      $("inCanary").value = DENSE_WAVES.canary;
      $("inWave").value = DENSE_WAVES.wave;
    }
    S.dense = dense;
    syncTiles(ids);
    $("empty").hidden = ids.length > 0;
    $("gridHint").textContent = !ids.length ? "" : dense
      ? "One square per charger (" + stations.length + "); hover for its id, click for its security details. " +
        "White dot = post-quantum key verified on this connection; gold border = canary."
      : stations.length + " charger" + (stations.length === 1 ? "" : "s") + " seen. Click one for its security details. " +
        "🔒 TLS · ✓ key verified on this connection · C canary · W wave · L legacy (no " + PQ_ALG + ").";

    preparePlayback(stations);
    var t = now(), counts = {};
    stations.forEach(function (st) {
      var sid = st.station_id, el = S.tiles[sid];
      var v = shown(st, t);
      counts[v] = (counts[v] || 0) + 1;
      if (S.lastShown[sid] && S.lastShown[sid] !== v && (v === "hybrid" || v === "rolled")) {
        S.arrive[sid] = { cls: "arrive", until: t + 1000 };
      }
      S.lastShown[sid] = v;
      var on = st.connection_state === "connected" && S.reachable !== false;
      var charging = on && st.charging_state === "Charging";
      var pi = isPi(st);
      // "Verified" only means something for a charger that HAS an enrolled key
      // (pq_check_required); a legacy charger is never checked.
      // ...and only once the tile itself shows the charger as post-quantum (replay).
      var verified = st.pq_verified && st.pq_check_required && v === "hybrid";
      var rot = rotationResult(st, S.mig);
      var arr = S.arrive[sid] && S.arrive[sid].until > t ? " " + S.arrive[sid].cls : "";
      var cls = "tile v-" + v + (charging ? " charging" : "") + (pi ? " pi" : "") +
        (dense && verified ? " ver" : "") + (dense && S.canary[sid] ? " can" : "") +
        (rot === "ok" ? " rot" : rot === "bad" ? " rotbad" : "") + arr;
      ["flash-good", "flash-bad"].forEach(function (f) { if (el.classList.contains(f)) cls += " " + f; });
      if (el.className !== cls) el.className = cls;

      var flag = alertOn(sid) ? "" : S.refused[sid] ? ["alertF", "⛔" + S.refused[sid]]
        : S.canary[sid] ? ["canary", "C"] : S.waveOf[sid] != null ? ["wv", "W" + S.waveOf[sid]]
        : legacyCapable(st) && v !== "legacy" ? ["legacy", "L"] : "";
      var label = sid + ": " + V_LABEL[v] + (charging && v !== "charging" ? ", charging" : "") +
        (S.canary[sid] ? ", canary" : S.waveOf[sid] != null ? ", wave " + S.waveOf[sid] : "") +
        (legacyCapable(st) ? ", legacy (no " + PQ_ALG + ")" : "") +
        (st.tls_version ? ", TLS" : "") + (verified ? ", key verified" : "") +
        (rot === "ok" ? ", key rotated" : rot === "bad" ? ", rotation failed (old key kept)" : "") +
        (alertOn(sid) ? " — " + S.alerts[sid].words : "");
      el.title = label;
      el.setAttribute("aria-label", label);
      var badges = (st.tls_version ? "🔒" : "") + (verified ? "✓" : "");
      var html;
      if (pi) {
        var frac = charging ? Math.max(0, Math.min(1, (st.power_w || 0) / MAX_W)) : 0;
        var blink = v === "migrating" || S.piBlinkUntil > t;
        var mw = typeof st.measured_mw === "number" ? st.measured_mw.toFixed(1) + " mW" : "on the Pi's console";
        html = '<span class="tag">physical charger · Raspberry Pi' + (S.canary[sid] ? " · canary" : "") + "</span>" +
          '<span class="id">' + esc(sid) + " " + badges + " " + (V_ICON[v] || "") + "</span>" +
          '<span class="led"><span class="bulb' + (frac > 0 ? " on" : "") + (blink ? " blink" : "") +
          '" style="opacity:' + (blink ? 1 : (0.35 + 0.65 * frac)).toFixed(2) + '"></span>' +
          "LED " + (blink ? "blinking (being migrated)" : frac > 0 ? Math.round(frac * 100) + "% · " + kw(st.power_w) + " (scaled)" : "off") +
          "</span>" + '<span class="sub">' + esc(V_LABEL[v]) + " · measured " + esc(mw) + "</span>";
      } else if (!dense) {
        html = '<span class="id">' + esc(short(sid)) + "</span>" +
          (flag ? '<span class="flag ' + flag[0] + '">' + esc(flag[1]) + "</span>" : "") +
          '<span class="badges">' + badges + "</span>" +
          '<span class="icon">' + (V_ICON[v] || "") + "</span>";
      } else {
        html = "";                                // dense: colour (and the white dot) say it all
      }
      if (el._html !== html) { el.innerHTML = html; el._html = html; }
    });
    if (hasIntruder) {
      counts.alert = (counts.alert || 0) + (S.intruder.until > t ? 1 : 0);
      renderIntruder(t, dense);
    }
    S.shownCounts = counts;
    renderLegend(counts);
  }

  function renderIntruder(t, dense) {
    var I = S.intruder, el = S.tiles[INTRUDER];
    var hot = I.until > t;
    var cls = "tile intruder" + (hot ? " v-alert" : "") +
      (S.arrive[INTRUDER] && S.arrive[INTRUDER].until > t ? " " + S.arrive[INTRUDER].cls : "");
    if (el.className !== cls) el.className = cls;
    var label = "Unknown device: refused at the TLS handshake " + I.count + "× — " + I.words;
    el.title = label;
    el.setAttribute("aria-label", label);
    var html = dense ? "" : '<span class="id">?</span><span class="flag alertF">×' + I.count +
      '</span><span class="badges">no id</span><span class="icon">⛔</span>';
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
  }

  function renderLegend(counts) {
    var rotated = (S.mig || {}).kind === "rotation"
      ? ((S.fleet || {}).stations || []).filter(function (s) { return rotationResult(s, S.mig) === "ok"; }).length : null;
    $("legend").innerHTML = VSTATES.map(function (k) {
      var n = counts[k] || 0;
      return '<span class="' + (n ? "" : "zero") + '"><i style="background:var(--v-' + k + ')"></i>' +
        esc(V_LABEL[k]) + " <b>" + n + "</b></span>";
    }).join("") +
      '<span><i style="background:var(--canary)"></i>canary</span>' +
      (rotated === null ? "" : '<span><i style="background:var(--rotation)"></i>new key (rotated) <b>' + rotated + "</b></span>");
  }

  // ---------------------------------------------------------------- migration
  function renderMigration() {
    var m = S.mig || {}, stations = (S.fleet || {}).stations || [];
    var byId = {};
    stations.forEach(function (s) { byId[s.station_id] = s; });
    var serverPhase = String(m.phase || "idle").toLowerCase();
    var phase = displayPhase();
    var rotation = m.kind === "rotation";
    var ph = $("phase");
    ph.className = "phase " + phase;
    ph.innerHTML = (rotation ? '<span class="kindRotation">ROTATION</span> · ' : "") + esc(phase.toUpperCase());
    var words = (rotation ? ROT_WORDS[phase] : null) || PHASE_WORDS[phase] || "";
    if (phase !== serverPhase) {
      words += " (Replaying the waves: the server already finished — " + serverPhase.toUpperCase() +
        " — faster than the eye can follow.)";
    }
    $("phaseWords").textContent = words;

    // The progress bar counts what the TILES show, so it moves with the replay.
    var t = now(), shownBy = {};
    stations.forEach(function (st) { if (S.waveOf[st.station_id] != null) {
      var v = S.lastShown[st.station_id] || shown(st, t);
      shownBy[v] = (shownBy[v] || 0) + 1;
    } });
    var segs = rotation
      ? [["migrated", "var(--rotation)", m.migrated], ["rolled_back", "var(--v-rolled)", m.rolled_back],
         ["pending", "var(--v-idle)", m.pending]]
      : [["hybrid", "var(--v-hybrid)", shownBy.hybrid], ["migrating", "var(--v-migrating)", shownBy.migrating],
         ["rolled", "var(--v-rolled)", shownBy.rolled], ["legacy", "var(--v-legacy)", shownBy.legacy],
         ["waiting", "var(--v-charging)", (shownBy.charging || 0) + (shownBy.idle || 0)],
         ["offline", "var(--v-offline)", (shownBy.offline || 0) + (shownBy.alert || 0)]];
    $("progress").innerHTML = segs.map(function (s) {
      var n = s[2] || 0;
      return n ? '<span title="' + esc(s[0] + ": " + n) + '" style="flex:' + n + ";background:" + s[1] + '"></span>' : "";
    }).join("");
    $("progress").setAttribute("aria-label", "progress: " + segs.map(function (s) {
      return (s[2] || 0) + " " + s[0];
    }).join(", "));

    var waves = m.waves || [];
    var live = playingWave();
    $("waves").innerHTML = waves.map(function (w) {
      var ids = w.station_ids || [];
      var cells = ids.map(function (sid) {
        var st = byId[sid] || {};
        // 6c: in a rotation every charger stays "migrated"; colour by its rotation result instead.
        var r = rotation ? rotationResult(st, m) : null;
        var cls = rotation ? (r === "ok" ? "st-rotated" : r === "bad" ? "v-rolled" : "v-hybrid")
                           : "v-" + (S.lastShown[sid] || "idle");
        return '<i class="' + esc(cls) + '" title="' + esc(sid) + '"></i>';
      }).join("");
      var legacy = ids.filter(function (sid) { return (byId[sid] || {}).migration_state === "incompatible"; }).length;
      var shownPhase = S.play.showAt[w.wave_id] != null && t < S.play.showAt[w.wave_id] ? "queued"
        : S.play.showEnd[w.wave_id] != null && t < S.play.showEnd[w.wave_id] ? "running" : w.phase;
      var res = String(shownPhase || "") === "rolled_back" ? "rolled back ↩"
        : shownPhase === "queued" ? (terminal(serverPhase) && w.phase === "queued" ? "not attempted" : "queued") : shownPhase === "running" ? "migrating…"
        : (w.migrated_count || 0) + " ✓" + (w.failed_count ? " " + w.failed_count + " ✗" : "") +
          (legacy ? " · " + legacy + " legacy" : "");
      return '<div class="wave' + (shownPhase === "rolled_back" ? " rolled" : "") +
        (shownPhase === "running" || (live === w.wave_id && replaying()) ? " live" : "") +
        (w.is_canary ? " isCanary" : "") + '"><span class="w">' +
        (w.is_canary ? "Canary" : "Wave " + esc(w.wave_id)) + '</span><span class="cells' + (ids.length > 20 ? " many" : "") + '">' + cells +
        '</span><span class="res">' + esc(res) + "</span></div>";
    }).join("");

    var sel = $("selWave"), cur = sel.value;
    sel.innerHTML = waves.filter(function (w) { return w.phase !== "queued"; }).map(function (w) {
      return '<option value="' + esc(w.wave_id) + '">' + (w.is_canary ? "canary" : "wave " + esc(w.wave_id)) + "</option>";
    }).join("");
    if (cur) sel.value = cur;
    var busy = serverPhase === "canary" || serverPhase === "running";
    $("btnStart").disabled = busy;
    $("btnRotate").disabled = busy;
    $("btnRollback").disabled = !sel.options.length;
    renderSummary(m, stations, serverPhase);
  }

  // ---------------------------------------------------------------- summary card
  function renderSummary(m, stations, phase) {
    var box = $("summary");
    if (!m.migration_id || !terminal(phase) || replaying()) { box.hidden = true; box._key = ""; return; }
    var rotation = m.kind === "rotation";
    var waves = m.waves || [];
    var halted = waves.filter(function (w) { return w.phase === "rolled_back"; })[0];
    var startMs = m.started_at ? Date.parse(m.started_at) : null;
    var endMs = m.completed_at ? Date.parse(m.completed_at) : null;
    var dur = startMs && endMs ? (endMs - startMs) / 1000 : null;
    var checks = S.checks.filter(function (c) {
      return startMs && c.at >= startMs - 1000 && (!endMs || c.at <= endMs + 1000) &&
        c.trigger === (rotation ? "rotation" : "migration");
    });
    var okMs = checks.filter(function (c) { return c.ok; }).map(function (c) { return c.ms; });
    var med = median(okMs);
    var run = S.runs[m.migration_id] || (S.runs[m.migration_id] = {});
    // Freeze "charging at the end" the first time the finished card is drawn,
    // so a later storm or stop does not rewrite the migration's summary.
    if (run.endCharging == null) run.endCharging = (S.fleet || {}).charging_count;
    var chNow = run.endCharging;
    var migrated = m.migrated || 0, rolled = m.rolled_back || 0, legacy = m.incompatible || 0;
    var pending = m.pending || 0;
    var eligible = rotation ? (m.total_stations || 0) : Math.max(0, (m.total_stations || 0) - legacy);
    var pct = eligible ? Math.round(100 * migrated / eligible) : 0;
    var key = [m.migration_id, phase, migrated, rolled, legacy, pending, checks.length, chNow].join("|");
    if (box._key === key) return;               // nothing new: keep the card (and its animation) still
    box._key = key;
    box.className = "summary" + (halted || phase === "failed" ? " halted" : "");

    var title = rotation
      ? (halted ? "↻ Key rotation stopped — wave " + halted.wave_id + " kept its old keys" : "↻ Key rotation complete")
      : phase === "failed" ? "⚠ Migration stopped with an error"
      : halted ? "⛔ Migration halted — " + (halted.is_canary ? "canary" : "wave " + halted.wave_id) + " rolled back"
      : "✅ Migration complete — the fleet is post-quantum hybrid";
    var sw = rotation
      ? "Each charger made a new " + PQ_ALG + " key and proved it before the old one was dropped. Nobody was disconnected."
      : halted ? "More than 20 % of that wave failed its key check, so the server put the old state back and stopped. " +
                 "Waves that passed keep their post-quantum keys; the rest were never touched."
      : "Every charger that can do " + PQ_ALG + " made its own key and proved it. Legacy chargers were left as they were.";
    var ring = rotation
      ? "conic-gradient(var(--rotation) 0 " + pct + "%, var(--v-rolled) " + pct + "% " +
        (eligible ? Math.round(100 * (migrated + rolled) / eligible) : 0) + "%, var(--v-idle) 0)"
      : "conic-gradient(var(--v-hybrid) 0 " + pct + "%, var(--v-rolled) " + pct + "% " +
        (eligible ? Math.round(100 * (migrated + rolled) / eligible) : 0) + "%, var(--v-charging) 0)";
    var big = rotation
      ? [["Rotated", migrated], ["Kept old key", rolled], ["Not attempted", pending], ["Waves", waves.length]]
      : [["Hybrid", migrated + "/" + eligible], ["Legacy skipped", legacy], ["Rolled back", rolled], ["Not attempted", pending]];
    var wmax = Math.max.apply(null, waves.map(function (w) { return (w.station_ids || []).length; }).concat([1]));
    var byId = {};
    stations.forEach(function (s) { byId[s.station_id] = s; });
    var wbars = waves.map(function (w) {
      var n = (w.station_ids || []).length;
      var lg = (w.station_ids || []).filter(function (sid) { return (byId[sid] || {}).migration_state === "incompatible"; }).length;
      var ok = w.phase === "rolled_back" ? 0 : (w.migrated_count || 0);
      var bad = w.phase === "rolled_back" ? n - lg : (w.failed_count || 0);
      var rest = Math.max(0, n - ok - bad - lg);
      function seg(k, c) { return k ? '<span style="flex:' + k + ";background:" + c + '"></span>' : ""; }
      return '<div class="wbar"><span>' + (w.is_canary ? "canary" : "wave " + esc(w.wave_id)) + '</span><span class="b" style="width:' +
        Math.max(8, Math.round(100 * n / wmax)) + '%">' +
        seg(ok, rotation ? "var(--rotation)" : "var(--v-hybrid)") + seg(bad, "var(--v-rolled)") +
        seg(lg, "var(--v-legacy)") + seg(rest, "var(--v-idle)") + "</span></div>";
    }).join("");
    var chargingLine = run.startCharging == null ? num(chNow) + " now"
      : run.startCharging + " → " + num(chNow) + (chNow >= run.startCharging ? " (charging never stopped)" : "");
    box.innerHTML =
      '<p class="sTitle">' + esc(title) + "</p>" +
      '<p class="sWords">' + esc(sw) + "</p>" +
      '<div class="donut"><div class="ring" style="background:' + ring + '" data-pct="' + pct + '%"></div><dl>' +
      "<dt>Waves</dt><dd>" + (waves[0] && waves[0].is_canary ? "canary " + (waves[0].station_ids || []).length + " + " : "") +
        Math.max(0, waves.length - 1) + " wave(s)</dd>" +
      (halted ? "<dt>Halted at</dt><dd>" + (halted.is_canary ? "canary" : "wave " + esc(halted.wave_id)) + " (" +
        esc(halted.failed_count || 0) + " of " + (halted.station_ids || []).length + " failed)</dd>" : "") +
      "<dt>Server time</dt><dd>" + (dur == null ? "—" : dur.toFixed(1) + " s") + "</dd>" +
      "<dt>Key checks</dt><dd>" + okMs.length + " passed" + (checks.length - okMs.length ? ", " + (checks.length - okMs.length) + " failed" : "") +
        (med == null ? "" : " · median " + med.toFixed(0) + " ms") + "</dd>" +
      "<dt>Charging</dt><dd>" + esc(chargingLine) + "</dd>" +
      "</dl></div>" +
      '<div class="big">' + big.map(function (b) {
        return '<div><div class="v">' + esc(b[1]) + '</div><div class="k">' + esc(b[0]) + "</div></div>";
      }).join("") + "</div>" +
      '<div class="wbars">' + wbars + "</div>" +
      '<p class="note">Counts and server time: /api/migration. Key checks: the events this page saw. ' +
      "The tiles replay each wave for at least " + (HOLD_MS / 1000).toFixed(1) + " s so it can be followed; " +
      "the real time is “Server time”.</p>";
    box.hidden = false;
  }

  // ---------------------------------------------------------------- inspector
  function yes(v) { return v === true ? '<span class="yes">yes ✓</span>' : v === false ? '<span class="no">no ✗</span>' : '<span class="na">—</span>'; }
  function val(v) { return v == null || v === "" ? '<span class="na">—</span>' : esc(v); }

  function renderInspector() {
    if (!S.selected) return;
    if (S.selected === INTRUDER) {
      var I = S.intruder;
      $("dTitle").textContent = "Unknown device (refused at TLS)";
      $("drawerBody").innerHTML =
        "<p class='sub'>Something tried to connect but its TLS handshake was refused, before any OCPP " +
        "message or charger id. The server cannot tell who it was — only why it was refused.</p>" +
        "<dl class='kv'><dt>Refused</dt><dd>" + I.count + " time(s)</dd>" +
        "<dt>Last reason</dt><dd>" + val(I.words) + "</dd>" +
        "<dt>TLS error</dt><dd><code>" + val(I.code) + "</code></dd>" +
        "<dt>Detail</dt><dd>" + val(I.detail) + "</dd>" +
        "<dt>Last at</dt><dd>" + (I.at ? hhmmss(I.at) : val(null)) + "</dd></dl>";
      return;
    }
    var st = ((S.fleet || {}).stations || []).filter(function (s) { return s.station_id === S.selected; })[0] ||
      { station_id: S.selected, connection_state: "never_seen" };
    var lc = st.last_pq_check || null;
    var a = S.alerts[st.station_id];
    $("dTitle").textContent = st.station_id + (isPi(st) ? " · Raspberry Pi (physical)" : "");
    $("drawerBody").innerHTML =
      (a ? "<p class='no'>⛔ Refused " + (S.refused[st.station_id] || 1) + "× — last: " + esc(a.words) +
        " (" + hhmmss(a.at) + ")</p>" : "") +
      "<h3>Connection</h3><dl class='kv'>" +
      "<dt>State</dt><dd>" + val(st.connection_state) + (st.boot_accepted ? " · boot accepted" : "") + "</dd>" +
      "<dt>Shown as</dt><dd>" + esc(V_LABEL[S.lastShown[st.station_id]] || "—") + "</dd>" +
      "<dt>TLS version</dt><dd>" + val(st.tls_version || (st.connection_state === "connected" ? "off (plain WebSocket)" : null)) + "</dd>" +
      "<dt>Cipher</dt><dd>" + val(st.tls_cipher) + "</dd>" +
      "<dt>Certificate</dt><dd>" + (st.peer_cert_bytes ? num(st.peer_cert_bytes) + " bytes" : val(null)) + "</dd>" +
      "<dt>Certificate name = charger id</dt><dd>" + yes(st.identity_ok) + "</dd>" +
      "</dl><h3>Post-quantum identity</h3><dl class='kv'>" +
      "<dt>Migration state</dt><dd>" + esc(STATE_LABEL[st.migration_state] || st.migration_state || "—") +
        (S.canary[st.station_id] ? " · canary" : S.waveOf[st.station_id] != null ? " · wave " + esc(S.waveOf[st.station_id]) : "") + "</dd>" +
      "<dt>Algorithm now</dt><dd>" + val(st.current_algorithm) + "</dd>" +
      "<dt>Can do</dt><dd>" + val((st.supported_algorithms || []).join(", ")) + "</dd>" +
      "<dt>Key id</dt><dd><code>" + val(st.pq_key_id) + "</code></dd>" +
      "<dt>Key check needed at boot</dt><dd>" + yes(st.pq_check_required) + "</dd>" +
      "<dt>Verified on this connection</dt><dd>" + yes(st.pq_verified) + "</dd>" +
      "<dt>Last key check</dt><dd>" + (lc ? esc(lc.result) + " · " + esc(lc.trigger) + " · " + num(lc.duration_ms, 1) +
        " ms · " + hhmmss(lc.at) : val(null)) + "</dd>" +
      "<dt>ML-DSA-44 sizes</dt><dd>public key " + MLDSA.pub + " B · signature " + MLDSA.sig + " B</dd>" +
      "</dl><h3>Charging</h3><dl class='kv'>" +
      "<dt>State</dt><dd>" + val(st.charging_state) + "</dd>" +
      "<dt>Power</dt><dd>" + kw(st.power_w) + (isPi(st) ? " (scaled: LED duty × 7.4 kW)" : "") + "</dd>" +
      (isPi(st) ? "<dt>Measured (INA219)</dt><dd>" + (typeof st.measured_mw === "number" ? st.measured_mw.toFixed(1) + " mW"
        : '<span class="na">not sent to the server yet — shown on the Pi\'s console</span>') + "</dd>" : "") +
      "<dt>Energy this session</dt><dd>" + num((st.energy_wh || 0) / 1000, 2) + " kWh</dd>" +
      "</dl>";
  }

  function openInspector(sid) {
    S.selected = sid;
    renderInspector();
    $("drawer").hidden = false;
    $("drawerClose").focus();
  }
  function closeInspector() {
    S.selected = null;
    $("drawer").hidden = true;
  }

  // ---------------------------------------------------------------- charts
  function baseChart(el) {
    if (!window.echarts) { el.innerHTML = '<p class="sub">Charts unavailable (vendor/echarts.min.js missing).</p>'; return null; }
    var c = echarts.init(el, null, { renderer: "canvas" });
    window.addEventListener("resize", function () { c.resize(); });
    return c;
  }
  function axisStyle() {
    return { axisLine: { lineStyle: { color: css("--line") } }, axisLabel: { color: css("--ink-2"), fontSize: 13 },
             splitLine: { lineStyle: { color: "#232c3a" } }, nameTextStyle: { color: css("--ink-2"), fontSize: 13 } };
  }
  function tooltip(extra) {
    return Object.assign({ backgroundColor: css("--surface-2"), borderColor: css("--line"), textStyle: { color: css("--ink") } }, extra);
  }

  function renderTimeline() {
    var c = S.charts.tl || (S.charts.tl = baseChart($("cTimeline")));
    if (!c) return;
    var MARK = { start: "▶ migration", rollback: "↩ rollback", done: "■ done", rotate: "↻ rotation", rotated: "■ rotated" };
    var marks = S.marks.filter(function (m) { return m[0] >= now() - WINDOW_S * 1000; }).map(function (m) {
      var low = m[1] === "done" || m[1] === "rotated";      // end-of-run labels at the bottom: no overlap
      return { xAxis: m[0], label: { formatter: MARK[m[1]] || m[1], color: css("--ink-2"), position: low ? "start" : "end" },
               lineStyle: { color: m[1] === "rollback" ? vColor("rolled") : css("--ink-2"), type: "dashed" } };
    });
    c.setOption({
      animation: false, grid: { left: 44, right: 16, top: 26, bottom: 30 },
      legend: { show: false },
      tooltip: tooltip({ trigger: "axis" }),
      xAxis: Object.assign({ type: "time", min: "dataMin", max: "dataMax" }, axisStyle(), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", name: "chargers", minInterval: 1, min: 0 }, axisStyle()),
      series: VSTATES.map(function (v, i) {
        return {
          type: "line", name: V_LABEL[v], stack: "all", showSymbol: false, smooth: 0.2,
          lineStyle: { width: 0 }, areaStyle: { color: vColor(v), opacity: 0.9 }, itemStyle: { color: vColor(v) },
          data: S.timeline.map(function (r) { return [r[0], r[i + 1]]; }),
          markLine: i === 0 ? { symbol: "none", silent: true, data: marks } : undefined
        };
      })
    });
  }

  function renderPower() {
    var c = S.charts.power || (S.charts.power = baseChart($("cPower")));
    if (!c) return;
    c.setOption({
      animation: false, grid: { left: 56, right: 16, top: 16, bottom: 30 },
      tooltip: tooltip({ trigger: "axis", valueFormatter: function (v) { return v.toFixed(1) + " kW"; } }),
      xAxis: Object.assign({ type: "time" }, axisStyle(), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", name: "kW", min: 0 }, axisStyle()),
      series: [{ type: "line", name: "Fleet power", showSymbol: false, step: "end", data: S.power,
                 lineStyle: { width: 2, color: vColor("charging") },
                 areaStyle: { color: vColor("charging"), opacity: 0.15 } }]
    });
  }
  function renderLatency() {
    var c = S.charts.lat || (S.charts.lat = baseChart($("cLat")));
    if (!c) return;
    var bin = 10, counts = {}, max = 0;
    S.lat.forEach(function (v) { var b = Math.floor(v / bin) * bin; counts[b] = (counts[b] || 0) + 1; max = Math.max(max, b); });
    var cats = [], vals = [];
    for (var b = 0; b <= max; b += bin) { cats.push(b + "–" + (b + bin)); vals.push(counts[b] || 0); }
    var med = median(S.lat);
    $("latSub").textContent = "Round-trip time of each ML-DSA-44 check seen since this page opened" +
      (S.lat.length ? " · " + S.lat.length + " checks, median " + med.toFixed(0) + " ms" : "") + ".";
    c.setOption({
      animation: false, grid: { left: 48, right: 16, top: 16, bottom: 44 },
      tooltip: tooltip({ trigger: "item", formatter: function (it) { return esc(it.name) + " ms: " + it.value + " check(s)"; } }),
      xAxis: Object.assign({ type: "category", data: cats, name: "ms", nameLocation: "middle", nameGap: 28 }, axisStyle()),
      yAxis: Object.assign({ type: "value", minInterval: 1, name: "checks" }, axisStyle()),
      series: [{ type: "bar", data: vals, barMaxWidth: 28, itemStyle: { color: vColor("hybrid"), borderRadius: [4, 4, 0, 0] } }]
    });
  }

  // ---------------------------------------------------------------- controls
  function say(text, kind) {
    var t = $("toast");
    t.textContent = text;
    t.className = "toast " + (kind || "");
  }
  function control(path, okText) {
    say("…");
    getJSON(path).then(function (r) {
      if (r.ok) say(okText(r.body), "good");
      else say("Refused (" + r.status + "): " + (r.body.error || r.body.detail || JSON.stringify(r.body)), "bad");
      pollFleet();
    }).catch(function (e) { say("Not reachable: " + e.message, "bad"); });
  }
  function ints() {
    return "wave_size=" + Math.max(1, +$("inWave").value || 10) + "&canary_count=" + Math.max(1, +$("inCanary").value || 5);
  }
  function wire() {
    $("btnStart").addEventListener("click", function () {
      control("/api/migration/start?" + ints() + "&target_mode=hybrid",
        function (b) { return "Migration " + (b.migration_id || "") + " started."; });
    });
    $("btnRotate").addEventListener("click", function () {
      control("/api/migration/rotate?" + ints(), function (b) { return "Key rotation " + (b.migration_id || "") + " started."; });
    });
    $("btnRollback").addEventListener("click", function () {
      var w = $("selWave").value;
      control("/api/migration/rollback?wave_id=" + encodeURIComponent(w),
        function (b) { return b.reverted ? "Wave " + w + " rolled back." : "Nothing to roll back in wave " + w + "."; });
    });
    ["inCanary", "inWave"].forEach(function (id) {   // 6c: keep what the operator typed
      $(id).addEventListener("input", function () { S.wavesTouched = true; });
    });
    $("inLimit").addEventListener("input", function () { $("limitOut").textContent = kw(+$("inLimit").value); });
    $("btnLimit").addEventListener("click", function () {
      var w = +$("inLimit").value;
      control("/api/fleet/limit?watts=" + w, function (b) {
        return "Limit " + kw(w) + " sent to " + (b.targets != null ? b.targets : "?") + " chargers (" + (b.ok != null ? b.ok : "?") + " accepted).";
      });
    });
    $("btnClear").addEventListener("click", function () {
      $("inLimit").value = MAX_W; $("limitOut").textContent = kw(MAX_W);
      control("/api/fleet/clear-limit", function () { return "Limit cleared."; });
    });
    $("btnStorm").addEventListener("click", function () { control("/api/mock/storm", function () { return "Mock storm: every charger reconnects."; }); });
    $("btnImpostor").addEventListener("click", function () { control("/api/mock/impostor", function () { return "Mock impostor on CP0045 for 15 s."; }); });
    $("btnIntruder").addEventListener("click", function () { control("/api/mock/intruder", function () { return "Mock: a device with a forged certificate tried to connect."; }); });
    $("btnWrongId").addEventListener("click", function () { control("/api/mock/wrong-identity", function () { return "Mock: CP0045 presented CP0004's certificate."; }); });
    $("drawerClose").addEventListener("click", closeInspector);
    $("drawer").addEventListener("click", function (e) { if (e.target === $("drawer")) closeInspector(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeInspector(); });
  }

  // ---------------------------------------------------------------- main
  function renderAll() {
    renderHeader();
    renderStepper();
    renderGrid();
    renderMigration();
    renderInspector();
    renderPower();
  }
  // Between polls: keep the replay, the alerts and the Pi's LED moving.
  function fastTick() {
    if (!S.fleet) return;
    var t = now();
    var busy = replaying() || t < S.play.lastEnd + 600 || S.piBlinkUntil > t - 600 || S.intruder.until > t - 600 ||
      Object.keys(S.alerts).some(function (k) { return S.alerts[k].until > t - 600; });
    if (!busy) return;
    renderHeader();
    renderStepper();
    renderGrid();
    renderMigration();
  }

  wire();
  pollHealth();
  pollFleet();
  pollEvents();
  setInterval(pollFleet, 1000);
  setInterval(pollEvents, 500);
  setInterval(pollHealth, 5000);
  setInterval(fastTick, 250);
  renderLatency();
})();
