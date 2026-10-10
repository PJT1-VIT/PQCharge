/*
 * PQCharge live dashboard (Track C, C-F6, FINAL 2-DAY PLAN).
 *
 * IN PLAIN WORDS
 *   Polls the server it was loaded from (GET only):
 *     /api/fleet      every 1 s   every charger + the migration status
 *     /api/events     every 0.5 s the live ticker (Track A, A-F4)
 *     /api/health     every 5 s   mode, TLS, identity check, algorithm
 *   and draws: header chips + counts, the fleet grid (the Pi bigger), the
 *   migration panel, the controls, the ticker, a security inspector per
 *   charger, and two charts (fleet power, key-check times).
 *
 *   Every number is the server's own; the page only formats it. Two things
 *   are derived here, and say so: "recovered" (Track A's is_recovered rule:
 *   connected + boot accepted + key verified when one is required) and the
 *   key-check time histogram (from the events this page has seen).
 *
 *   Works before A-F4: without /api/events the ticker is built from what
 *   changed between two /api/fleet polls, and the A-F4 fields (cipher,
 *   identity_ok, pq_key_id, last_pq_check) show "—".
 */
(function () {
  "use strict";

  var MAX_W = 7400;
  var TICKER_MAX = 80;
  var POWER_WINDOW_S = 600;
  var STATES = ["pending", "in_progress", "migrated", "rolled_back", "incompatible"];
  var STATE_LABEL = { pending: "waiting", in_progress: "upgrading", migrated: "upgraded (hybrid)",
                      rolled_back: "rolled back", incompatible: "incompatible (legacy)" };
  var PHASE_WORDS = {
    idle: "No migration running.",
    canary: "Testing on the canary group first.",
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
  var MLDSA = { pub: 1312, sig: 2420 };          // ML-DSA-44 sizes in bytes (FIPS 204)

  var S = {
    health: null, fleet: null, mig: null,
    reachable: null, lastSeq: 0, eventsApi: null,  // null = unknown, true/false after first try
    prev: {}, power: [], lat: [], selected: null, tiles: {}, gridKey: "",
    charts: {}
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
  function hhmmss(iso) {
    var d = iso ? new Date(iso) : new Date();
    return d.toTimeString().slice(0, 8);
  }
  function getJSON(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        return { status: r.status, ok: r.ok, body: body };
      });
    });
  }
  function isPi(st) {
    return /PI/i.test(st.station_id) || st.measured_mw !== undefined || st.power_source === "scaled";
  }
  function recovered(st) {
    // Track A's StationView.is_recovered (Contract 7 section 7.7), restated for display only.
    return st.connection_state === "connected" && st.boot_accepted &&
      (st.pq_verified || !st.pq_check_required);
  }
  function stationOf(ev) {
    return ev.station_id || (ev.payload || {}).station || null;
  }

  // ---------------------------------------------------------------- polling
  function pollFleet() {
    getJSON("/api/fleet").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      S.reachable = true;
      var fleet = r.body;
      var mig = fleet.migration && fleet.migration.phase ? Promise.resolve(fleet.migration)
        : getJSON("/api/migration").then(function (m) { return m.ok ? m.body : null; });
      return mig.then(function (m) {
        if (S.eventsApi === false) diffTicker(fleet);
        S.fleet = fleet; S.mig = m;
        S.power.push([Date.now(), (fleet.aggregate_power_w || 0) / 1000]);
        var cut = Date.now() - POWER_WINDOW_S * 1000;
        while (S.power.length && S.power[0][0] < cut) S.power.shift();
        renderAll();
      });
    }).catch(function (e) {
      S.reachable = false;
      $("serverLine").textContent = "server not reachable (" + e.message + ") — retrying…";
      renderHeader();
    });
  }

  function pollEvents() {
    if (S.eventsApi === false) return;
    getJSON("/api/events?after=" + S.lastSeq + "&limit=300").then(function (r) {
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
        // time for the histogram, but put only the latest 20 lines in the ticker.
        evs.slice(0, -20).forEach(function (ev) {
          var p = ev.payload || {};
          if (ev.event_type === "connection_attempt" && p.transition === "pq_auth" &&
              (ev.outcome || p.result) === "success" && typeof p.duration_ms === "number") S.lat.push(p.duration_ms);
        });
        evs = evs.slice(-20);
        renderLatency();
      }
      evs.forEach(onEvent);
      if (typeof r.body.last_seq === "number") S.lastSeq = r.body.last_seq;
      else if (evs.length) S.lastSeq = evs[evs.length - 1].seq || S.lastSeq;
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

  // ---------------------------------------------------------------- ticker
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

  function onEvent(ev) {
    var p = ev.payload || {}, sid = stationOf(ev), t = ev.timestamp, type = ev.event_type;
    var why = p.trigger ? " <span class='sub'>(" + esc(p.trigger) + ")</span>" : "";
    if (type === "connection_attempt" && p.transition === "pq_auth") {
      var ok = (ev.outcome || p.result) === "success";
      if (ok && typeof p.duration_ms === "number") S.lat.push(p.duration_ms);
      flash(sid, ok);
      tick(ok ? "good" : "bad", ok
        ? "✓ <b>" + esc(sid) + "</b> " + esc(p.algorithm || "ML-DSA") + " signature verified · " +
          num(p.duration_ms, 0) + " ms · key " + esc(kid(p.key_id)) + why
        : "✗ <b>" + esc(sid) + "</b> key check FAILED — " + esc(p.detail || "rejected") + why, t);
      if (ok) renderLatency();
      return;
    }
    if (type === "connection_attempt" && p.transition === "identity_check" &&
        (ev.outcome === "rejected" || p.identity_matches === false)) {
      flash(sid, false);
      tick("bad", "✗ <b>" + esc(sid) + "</b> refused: its certificate is for " +
        esc(p.certificate_common_name || "another charger"), t);
      return;
    }
    if (type === "certificate_installed" && p.transition === "pq_enrolled") {
      tick("info", "🔑 <b>" + esc(sid) + "</b> made its own " + esc(p.algorithm || "ML-DSA") +
        " key · key " + esc(kid(p.key_id)) + " (private key never left the charger)", t);
      return;
    }
    if (type === "connection_closed" && p.reason === "pq_auth_failed") {
      flash(sid, false);
      tick("bad", "⛔ <b>" + esc(sid) + "</b> cut off (code 1008): failed its post-quantum key check", t);
      return;
    }
    if (type === "rotation_completed" && sid) {
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
    if (sid) return;                                   // per-charger traffic: too noisy for the ticker
    var rotating = p.kind === "rotation";       // Track B B-F2: rotation runs reuse the migration lines
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
  }

  // Before A-F4: what changed between two fleet polls.
  function diffTicker(fleet) {
    var now = new Date().toISOString();
    (fleet.stations || []).forEach(function (st) {
      var old = S.prev[st.station_id];
      if (old) {
        if (old.migration_state !== st.migration_state) {
          var good = st.migration_state === "migrated", bad = st.migration_state === "rolled_back";
          if (good || bad || st.migration_state === "incompatible") {
            tick(good ? "good" : bad ? "bad" : "info", (good ? "✓ " : bad ? "✗ " : "") + "<b>" +
              esc(st.station_id) + "</b> " + esc(STATE_LABEL[old.migration_state]) + " → " +
              esc(STATE_LABEL[st.migration_state]) + (good ? " (key check passed)" : ""), now);
            flash(st.station_id, !bad);
          }
        }
        if (!old.pq_verified && st.pq_verified && st.migration_state === old.migration_state) {
          tick("good", "✓ <b>" + esc(st.station_id) + "</b> key verified on this connection", now);
          flash(st.station_id, true);
        }
        if (old.connection_state === "connected" && st.connection_state !== "connected") {
          tick("warn", "<b>" + esc(st.station_id) + "</b> disconnected", now);
        }
      }
      S.prev[st.station_id] = { migration_state: st.migration_state, pq_verified: st.pq_verified,
                                connection_state: st.connection_state };
    });
  }

  // ---------------------------------------------------------------- header
  function chip(cls, k, v) { return '<span class="chip ' + cls + '">' + esc(k) + " <b>" + v + "</b></span>"; }

  function renderHeader() {
    var h = S.health || {}, f = S.fleet || {};
    var stations = f.stations || [];
    var mode = h.crypto_mode || f.crypto_mode || "—";
    var tlsOn = typeof h.tls === "boolean" ? h.tls : stations.some(function (s) { return s.tls_version; });
    var tlsVer = (stations.filter(function (s) { return s.tls_version; })[0] || {}).tls_version;
    var idc = h.identity_check;
    $("chips").innerHTML =
      chip("mode-" + mode, "Mode", esc(mode)) +
      chip(tlsOn ? "ok" : "", "TLS", tlsOn ? "🔒 " + esc(tlsVer || "on") : "off") +
      chip(idc === "enforce" ? "ok" : idc === "off" ? "bad" : "", "Identity check", esc(idc || "—")) +
      chip("", "Post-quantum", esc(h.pq_algorithm || "—"));
    var connected = stations.filter(function (s) { return s.connection_state === "connected"; }).length;
    var rec = stations.filter(recovered).length;
    $("counts").innerHTML = [
      ["Connected", connected + "/" + stations.length],
      ["Charging", num(f.charging_count)],
      ["Recovered", rec],
      ["Fleet power", kw(f.aggregate_power_w)]
    ].map(function (c) { return '<div class="count"><div class="v">' + c[1] + '</div><div class="k">' + c[0] + "</div></div>"; }).join("");
    if (S.reachable) {
      $("serverLine").textContent = "server run " + (f.run_id || h.run_id || "?") +
        (h.migration ? " · " + h.migration : "") + " · updated " + hhmmss();
    }
  }

  // ---------------------------------------------------------------- fleet grid
  function renderLegend(stations) {
    var counts = {};
    stations.forEach(function (s) { counts[s.migration_state] = (counts[s.migration_state] || 0) + 1; });
    var col = { pending: "var(--st-pending)", in_progress: "var(--st-progress)", migrated: "var(--st-migrated)",
                rolled_back: "var(--st-rolled)", incompatible: "var(--st-incompat)" };
    $("legend").innerHTML = STATES.map(function (k) {
      return '<span><i style="background:' + col[k] + '"></i>' + esc(STATE_LABEL[k]) + " " + (counts[k] || 0) + "</span>";
    }).join("");
  }

  function buildGrid(stations) {
    var grid = $("grid");
    grid.innerHTML = "";
    S.tiles = {};
    stations.forEach(function (st) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "listitem");
      b.dataset.sid = st.station_id;
      b.addEventListener("click", function () { openInspector(st.station_id); });
      grid.appendChild(b);
      S.tiles[st.station_id] = b;
    });
  }

  function renderGrid() {
    var stations = ((S.fleet || {}).stations || []).slice().sort(function (a, b) {
      return (isPi(b) - isPi(a)) || String(a.station_id).localeCompare(String(b.station_id));
    });
    var key = stations.map(function (s) { return s.station_id; }).join(",");
    if (key !== S.gridKey) { buildGrid(stations); S.gridKey = key; }
    renderLegend(stations);
    stations.forEach(function (st) {
      var el = S.tiles[st.station_id];
      var on = st.connection_state === "connected";
      var pi = isPi(st);
      var cls = "tile st-" + st.migration_state + (on ? "" : " off") + (pi ? " pi" : "");
      ["flash-good", "flash-bad"].forEach(function (f) { if (el.classList.contains(f)) cls += " " + f; });
      el.className = cls;
      var badges = (st.tls_version ? "🔒" : "") + (st.pq_verified ? "✓" : "");
      var label = st.station_id + ": " + STATE_LABEL[st.migration_state] + (on ? "" : ", disconnected") +
        (st.tls_version ? ", TLS" : "") + (st.pq_verified ? ", key verified" : "");
      el.title = label;
      el.setAttribute("aria-label", label);
      if (!pi) {
        el.innerHTML = '<span class="id">' + esc(short(st.station_id)) + '</span><span class="badges">' + badges + "</span>";
        return;
      }
      var frac = on && st.charging_state === "Charging" ? Math.max(0, Math.min(1, (st.power_w || 0) / MAX_W)) : 0;
      var mw = typeof st.measured_mw === "number" ? st.measured_mw.toFixed(1) + " mW" : "on the Pi's console";
      el.innerHTML = '<span class="tag">physical charger · Raspberry Pi</span>' +
        '<span class="id">' + esc(st.station_id) + " " + badges + "</span>" +
        '<span class="led"><span class="bulb" style="opacity:' + (0.15 + 0.85 * frac).toFixed(2) +
        ";box-shadow:0 0 " + Math.round(18 * frac) + "px " + Math.round(6 * frac) + 'px rgba(250,178,25,.7)"></span>' +
        "LED " + Math.round(frac * 100) + "% · " + kw(st.power_w) + " (scaled)</span>" +
        '<span class="sub">measured: ' + esc(mw) + "</span>";
    });
  }

  // ---------------------------------------------------------------- migration
  function renderMigration() {
    var m = S.mig || {}, stations = (S.fleet || {}).stations || [];
    var byId = {};
    stations.forEach(function (s) { byId[s.station_id] = s; });
    var phase = String(m.phase || "idle").toLowerCase();
    var rotation = m.kind === "rotation";
    var ph = $("phase");
    ph.className = "phase " + phase;
    ph.innerHTML = (rotation ? '<span class="kindRotation">ROTATION</span> · ' : "") + esc(phase.toUpperCase());
    $("phaseWords").textContent = (rotation ? ROT_WORDS[phase] : null) || PHASE_WORDS[phase] || "";

    // In a rotation run (Track B B-F2) "migrated" counts rotated chargers and "rolled_back"
    // counts failed rotations: those chargers keep their OLD key and stay upgraded.
    function rotLabel(k) {
      if (!rotation) return STATE_LABEL[k];
      return { migrated: "rotated", rolled_back: "failed, old key kept", pending: "waiting" }[k] || STATE_LABEL[k];
    }
    var total = m.total_stations || stations.length || 1;
    var segs = rotation
      ? [["migrated", "var(--rotation)"], ["rolled_back", "var(--st-rolled)"], ["pending", "var(--st-pending)"]]
      : [["migrated", "var(--st-migrated)"], ["in_progress", "var(--st-progress)"], ["rolled_back", "var(--st-rolled)"],
         ["incompatible", "var(--st-incompat)"], ["pending", "var(--st-pending)"]];
    $("progress").innerHTML = segs.map(function (s) {
      var n = m[s[0]] || 0;
      return n ? '<span title="' + esc(rotLabel(s[0]) + ": " + n) +
        '" style="flex:' + n + ";background:" + s[1] + '"></span>' : "";
    }).join("");
    $("progress").setAttribute("aria-label", "progress: " + segs.map(function (s) {
      return (m[s[0]] || 0) + " " + rotLabel(s[0]);
    }).join(", ") + " of " + total);

    var waves = m.waves || [];
    $("waves").innerHTML = waves.map(function (w) {
      var ids = w.station_ids || [];
      var cells = ids.map(function (sid) {
        var st = byId[sid] || {};
        return '<i class="st-' + esc(st.migration_state || "pending") + '" title="' + esc(sid) + '"></i>';
      }).join("");
      var legacy = ids.filter(function (sid) { return (byId[sid] || {}).migration_state === "incompatible"; }).length;
      var res = String(w.phase || "") === "rolled_back" ? "rolled back"
        : (w.migrated_count || 0) + " ✓" + (w.failed_count ? " " + w.failed_count + " ✗" : "") +
          (legacy ? " · " + legacy + " legacy" : "");
      return '<div class="wave' + (w.phase === "rolled_back" ? " rolled" : "") + '"><span class="w">' +
        (w.is_canary ? "Canary" : "Wave " + esc(w.wave_id)) + '</span><span class="cells">' + cells +
        '</span><span class="res">' + esc(w.phase === "queued" ? "queued" : res) + "</span></div>";
    }).join("");

    var sel = $("selWave"), cur = sel.value;
    sel.innerHTML = waves.filter(function (w) { return w.phase !== "queued"; }).map(function (w) {
      return '<option value="' + esc(w.wave_id) + '">' + (w.is_canary ? "canary" : "wave " + esc(w.wave_id)) + "</option>";
    }).join("");
    if (cur) sel.value = cur;
    var busy = phase === "canary" || phase === "running";
    $("btnStart").disabled = busy;
    $("btnRotate").disabled = busy;
    $("btnRollback").disabled = !sel.options.length;
  }

  // ---------------------------------------------------------------- inspector
  function yes(v) { return v === true ? '<span class="yes">yes ✓</span>' : v === false ? '<span class="no">no ✗</span>' : '<span class="na">—</span>'; }
  function val(v) { return v == null || v === "" ? '<span class="na">—</span>' : esc(v); }

  function renderInspector() {
    if (!S.selected) return;
    var st = ((S.fleet || {}).stations || []).filter(function (s) { return s.station_id === S.selected; })[0];
    if (!st) return;
    var lc = st.last_pq_check || null;
    $("dTitle").textContent = st.station_id + (isPi(st) ? " · Raspberry Pi (physical)" : "");
    $("drawerBody").innerHTML =
      "<h3>Connection</h3><dl class='kv'>" +
      "<dt>State</dt><dd>" + val(st.connection_state) + (st.boot_accepted ? " · boot accepted" : "") + "</dd>" +
      "<dt>TLS version</dt><dd>" + val(st.tls_version || "off (plain WebSocket)") + "</dd>" +
      "<dt>Cipher</dt><dd>" + val(st.tls_cipher) + "</dd>" +
      "<dt>Certificate</dt><dd>" + (st.peer_cert_bytes ? num(st.peer_cert_bytes) + " bytes" : val(null)) + "</dd>" +
      "<dt>Certificate name = charger id</dt><dd>" + yes(st.identity_ok) + "</dd>" +
      "</dl><h3>Post-quantum identity</h3><dl class='kv'>" +
      "<dt>Migration state</dt><dd>" + esc(STATE_LABEL[st.migration_state] || st.migration_state) +
        (st.migration_wave != null ? " · wave " + esc(st.migration_wave) : "") + "</dd>" +
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
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function baseChart(el) {
    if (!window.echarts) { el.innerHTML = '<p class="sub">Charts unavailable (vendor/echarts.min.js missing).</p>'; return null; }
    var c = echarts.init(el, null, { renderer: "canvas" });
    window.addEventListener("resize", function () { c.resize(); });
    return c;
  }
  function axisStyle() {
    return { axisLine: { lineStyle: { color: css("--line") } }, axisLabel: { color: css("--ink-2"), fontSize: 13 },
             splitLine: { lineStyle: { color: "#2a2a28" } }, nameTextStyle: { color: css("--ink-2"), fontSize: 13 } };
  }
  function renderPower() {
    var c = S.charts.power || (S.charts.power = baseChart($("cPower")));
    if (!c) return;
    c.setOption({
      animation: false, grid: { left: 56, right: 16, top: 16, bottom: 30 },
      tooltip: { trigger: "axis", valueFormatter: function (v) { return v.toFixed(1) + " kW"; },
                 backgroundColor: css("--surface-2"), borderColor: css("--line"), textStyle: { color: css("--ink") } },
      xAxis: Object.assign({ type: "time" }, axisStyle(), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", name: "kW", min: 0 }, axisStyle()),
      series: [{ type: "line", name: "Fleet power", showSymbol: false, step: "end", data: S.power,
                 lineStyle: { width: 2, color: css("--mode-classical") },
                 areaStyle: { color: css("--mode-classical"), opacity: 0.12 } }]
    });
  }
  function renderLatency() {
    var c = S.charts.lat || (S.charts.lat = baseChart($("cLat")));
    if (!c) return;
    var bin = 10, counts = {}, max = 0;
    S.lat.forEach(function (v) { var b = Math.floor(v / bin) * bin; counts[b] = (counts[b] || 0) + 1; max = Math.max(max, b); });
    var cats = [], vals = [];
    for (var b = 0; b <= max; b += bin) { cats.push(b + "–" + (b + bin)); vals.push(counts[b] || 0); }
    var sorted = S.lat.slice().sort(function (a, b2) { return a - b2; });
    var med = sorted.length ? sorted[Math.floor((sorted.length - 1) / 2)] : null;
    $("latSub").textContent = "Round-trip time of each ML-DSA-44 check seen since this page opened" +
      (sorted.length ? " · " + sorted.length + " checks, median " + med.toFixed(0) + " ms" : "") + ".";
    c.setOption({
      animation: false, grid: { left: 48, right: 16, top: 16, bottom: 44 },
      tooltip: { trigger: "item", formatter: function (it) { return esc(it.name) + " ms: " + it.value + " check(s)"; },
                 backgroundColor: css("--surface-2"), borderColor: css("--line"), textStyle: { color: css("--ink") } },
      xAxis: Object.assign({ type: "category", data: cats, name: "ms", nameLocation: "middle", nameGap: 28 }, axisStyle()),
      yAxis: Object.assign({ type: "value", minInterval: 1, name: "checks" }, axisStyle()),
      series: [{ type: "bar", data: vals, barMaxWidth: 28, itemStyle: { color: css("--good"), borderRadius: [4, 4, 0, 0] } }]
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
    $("btnImpostor").addEventListener("click", function () { control("/api/mock/impostor", function () { return "Mock impostor on CP0003 for 15 s."; }); });
    $("drawerClose").addEventListener("click", closeInspector);
    $("drawer").addEventListener("click", function (e) { if (e.target === $("drawer")) closeInspector(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeInspector(); });
  }

  // ---------------------------------------------------------------- main
  function renderAll() {
    renderHeader();
    renderGrid();
    renderMigration();
    renderInspector();
    renderPower();
  }

  wire();
  pollHealth();
  pollFleet();
  pollEvents();
  setInterval(pollFleet, 1000);
  setInterval(pollEvents, 500);
  setInterval(pollHealth, 5000);
  renderLatency();
})();
