/*
 * PQCharge results page — Track C, Phase C6.
 *
 * Reads the results (window.PQCHARGE_RESULTS from results.js, or results.json
 * when served) and draws every section. It computes NOTHING that matters:
 * every number shown was calculated by analysis/run.py in Python and is
 * only formatted here, so the page and the report can never disagree.
 *
 * Exposed for the dashboard (C7):  window.PQChargeReport.render(data)
 *
 * Runbook v1.3 (2026-10-14): the results file is a BUNDLE of views
 * (analysis/store.py rebuild_bundle): every analysed demo run (demo1, demo2,
 * ...), "All runs combined" and "Experiments" (the step-13 runs). A bar at
 * the top picks the view; the page opens on the run analysed last
 * (bundle.current), or on #view=<name> from the address. A demo run or the
 * experiments are drawn with the usual sections (Runs, E1-E5, nodes); a demo
 * run starts with its V1-V7 summary, the combined view has its own section.
 * The old single-view file (format 3) still opens, as "Experiments".
 * S1 (scale test) is no longer on the page.
 *
 * Colour rules (one meaning per colour, everywhere on the page):
 *   security mode  classical = blue, hybrid = orange, post-quantum = aqua
 *   trust / state  green = good, yellow = warning, red = critical, grey = pending
 *                  -- always with an icon or a text label, never colour alone
 */
(function () {
  "use strict";

  var MODE_LABEL = { classical: "Classical", hybrid: "Hybrid", pqc: "Pure PQC (not built)" };
  var MODE_SHORT = { classical: "Classical", hybrid: "Hybrid", pqc: "Pure PQC (not built)" };
  var STATE_LABEL = {
    pending: "Pending", in_progress: "Upgrading", migrated: "Upgraded",
    rolled_back: "Rolled back", incompatible: "Incompatible"
  };
  var RENDERER = /[?&]png\b/.test(location.search) ? "canvas" : "svg";
  var charts = [];
  var DATA = null;       // the view being drawn (a demo run or the experiments)
  var BUNDLE = null;     // every view (format 4)
  var VIEW = null;       // its name, "combined" or "experiments"
  var selectedSlot = null;
  var e1Exp = null;

  // ------------------------------------------------------------------ utils

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function palette() {
    return {
      surface: css("--surface"), ink: css("--ink"), ink2: css("--ink-2"),
      muted: css("--muted"), grid: css("--grid"), axis: css("--axis"),
      alt: css("--series-alt"),
      mode: {
        classical: css("--mode-classical"), hybrid: css("--mode-hybrid"),
        pqc: css("--mode-pqc"), other: css("--mode-other")
      },
      status: {
        good: css("--good"), warning: css("--warning"),
        serious: css("--serious"), critical: css("--critical"), pending: css("--muted")
      }
    };
  }
  function modeColor(p, mode) { return p.mode[mode] || p.mode.other; }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function fmt(v, digits) {
    if (!isNum(v)) return "—";
    return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function ms(v) { return isNum(v) ? fmt(v, v < 10 ? 2 : 1) + " ms" : "—"; }
  function sec(v) { return isNum(v) ? fmt(v, v < 10 ? 2 : 1) + " s" : "not reached"; }
  function int(v) { return isNum(v) ? fmt(v, 0) : "—"; }
  function bytes(v) { return isNum(v) ? fmt(v, 0) + " B" : "—"; }
  function modeName(m) { return MODE_LABEL[m] || m || "—"; }
  function slotLabel(s) {
    return s.experiment + " · " + s.n_stations + " chargers · " + modeName(s.crypto_mode) +
      (s.tls ? " · TLS" : " · no TLS");
  }
  function slotShort(s) { return s.experiment + " n" + s.n_stations + (s.tls ? "" : " (no TLS)"); }
  function slotTag(s) { return slotShort(s) + " · " + (MODE_SHORT[s.crypto_mode] || s.crypto_mode); }
  function modeCell(m) {
    return '<span class="dot" style="background:var(--mode-' + esc(MODE_LABEL[m] ? m : "other") +
      ')"></span>' + esc(modeName(m));
  }
  function badge(status) {
    var icon = { pass: "✓", warn: "!", fail: "✕", info: "i" }[status] || "?";
    var text = { pass: "Trusted", warn: "Caveats", fail: "Not usable", info: "Note" }[status] || status;
    return '<span class="badge ' + esc(status) + '"><span class="i" aria-hidden="true">' + icon +
      "</span>" + esc(text) + "</span>";
  }
  function tile(k, v, d) {
    return '<div class="tile"><div class="k">' + esc(k) + '</div><div class="v">' + v +
      "</div>" + (d ? '<div class="d">' + esc(d) + "</div>" : "") + "</div>";
  }
  function table(headers, rows) {
    var h = headers.map(function (x) {
      return '<th class="' + (x.num ? "num" : "") + '">' + esc(x.t) + "</th>";
    }).join("");
    var b = rows.map(function (r) {
      return "<tr>" + r.map(function (c, i) {
        return '<td class="' + (headers[i].num ? "num" : "") + '">' + c + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return '<div class="tablewrap"><table><thead><tr>' + h + "</tr></thead><tbody>" + b +
      "</tbody></table></div>";
  }
  function empty(text) { return '<p class="empty">' + text + "</p>"; }
  function slots() { return (DATA.slot_order || []).map(function (k) { return DATA.slots[k]; }); }

  // --------------------------------------------------------- chart plumbing

  function base(p, fileName) {
    return {
      backgroundColor: "transparent",
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: p.ink2 },
      animationDuration: 400,
      grid: { left: 56, right: 28, top: 40, bottom: 48, containLabel: false },
      tooltip: {
        backgroundColor: p.surface, borderColor: p.axis, borderWidth: 1,
        textStyle: { color: p.ink, fontSize: 12 }, confine: true
      },
      legend: { type: "scroll", top: 4, left: 0, right: 64, icon: "roundRect", itemWidth: 12, itemHeight: 4, textStyle: { color: p.ink2 } },
      toolbox: {
        right: 0, top: 0, itemSize: 13,
        iconStyle: { borderColor: p.muted },
        feature: {
          dataView: { title: "Table view", readOnly: true, lang: ["Data", "Close", "Refresh"],
            backgroundColor: p.surface, textColor: p.ink, textareaColor: p.surface, textareaBorderColor: p.axis },
          saveAsImage: { title: "Save image", name: "pqcharge_" + fileName, backgroundColor: p.surface, pixelRatio: 3 }
        }
      }
    };
  }
  function valueAxis(p, name, extra) {
    return Object.assign({
      type: "value", name: name, nameLocation: "middle", nameGap: 36,
      nameTextStyle: { color: p.ink2, fontSize: 12 },
      axisLine: { show: true, lineStyle: { color: p.axis } },
      axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: p.grid, width: 1 } }
    }, extra || {});
  }
  function catAxis(p, data, extra) {
    return Object.assign({
      type: "category", data: data,
      axisLine: { lineStyle: { color: p.axis } }, axisTick: { show: false },
      axisLabel: { color: p.muted, fontSize: 11 }
    }, extra || {});
  }
  function line(name, color, data, extra) {
    return Object.assign({
      type: "line", name: name, data: data, showSymbol: false, symbolSize: 8,
      lineStyle: { width: 2, color: color }, itemStyle: { color: color },
      emphasis: { focus: "series" }
    }, extra || {});
  }

  /** Mount a chart whose option is built from the current palette. */
  function mount(el, build) {
    if (!el || typeof echarts === "undefined") return;
    var chart = echarts.init(el, null, { renderer: RENDERER });
    var entry = { el: el, build: build, chart: chart };
    chart.setOption(build(palette()));
    charts.push(entry);
  }
  function redrawAll() {
    charts.forEach(function (c) {
      c.chart.dispose();
      c.chart = echarts.init(c.el, null, { renderer: RENDERER });
      c.chart.setOption(c.build(palette()));
    });
  }
  function disposeAll() {
    charts.forEach(function (c) { c.chart.dispose(); });
    charts = [];
  }
  window.addEventListener("resize", function () {
    charts.forEach(function (c) { c.chart.resize(); });
  });

  // ================================================================ sections

  // ------------------------------------------------- headlines (C-F5, for the slides)

  function renderHeadlines(root) {
    var all = slots(), cmp = DATA.comparisons || {}, lines = [];
    function trust(s) { return s && s.trust && s.trust.status !== "pass" ? " <span class='sub'>(trust: " + esc(s.trust.status) + ")</span>" : ""; }
    function biggest(list, f) { return list.slice().sort(function (a, b) { return f(b) - f(a); })[0]; }
    function add(id, text) { lines.push("<li><strong>" + id + "</strong> · " + text + "</li>"); }
    function none(id, what) { lines.push('<li class="sub"><strong>' + id + "</strong> · " + what + " — not in these results yet</li>"); }

    var ro = cmp.ready_overhead_vs_classical || [];
    if (ro.length) {
      var r = biggest(ro, function (x) { return x.n + (x.tls ? 0.5 : 0); });
      add("E1", "Time until a charger may work, " + int(r.n) + " chargers" + (r.tls ? ", TLS" : "") + ": classical " +
        ms(r.classical_median_ms) + " → " + esc(modeName(r.mode).toLowerCase()) + " " + ms(r.median_ms) +
        " (<strong>+" + ms(r.added_median_ms) + "</strong>, ×" + fmt(r.median_ratio, 2) + ") for the post-quantum key check.");
    } else none("E1", "classical vs hybrid time until ready");

    var storms = all.filter(function (s) { return s.e2; });
    if (storms.length) {
      var top = Math.max.apply(null, storms.map(function (s) { return s.n_stations; }));
      add("E2", "Server restarted under " + int(top) + " chargers; 95 % of the fleet recovered in " +
        storms.filter(function (s) { return s.n_stations === top; }).map(function (s) {
          return esc(modeName(s.crypto_mode).toLowerCase()) + " <strong>" + sec(s.e2.t95_s) + "</strong>" +
            (s.e2.recovery_rule === "boot + key check" ? " (incl. key check)" : "") + trust(s);
        }).join(" · ") + ".");
    } else none("E2", "reconnection storm");

    var migs = all.filter(function (s) { return s.e3 && s.e3.kind !== "rotation" && !(s.e3.halt || {}).shown; });
    if (migs.length) {
      var e3s = biggest(migs, function (s) { return s.e3.fleet_size; }), e = e3s.e3, fc = e.final_counts, q = e.pq_checks || {};
      add("E3", int(e.fleet_size) + " chargers migrated while charging: <strong>" + int(fc.migrated) + " upgraded</strong>, " +
        int(fc.incompatible) + " legacy skipped, " + int(fc.rolled_back) + " rolled back; " + int(q.passed) + "/" + int(q.total) +
        " key checks passed; <strong>" + int(e.charging.sessions_disturbed) + "/" + int(e.charging.sessions_running_at_start) +
        " sessions disturbed</strong>." + trust(e3s));
    } else none("E3", "migration under load");
    var halts = all.filter(function (s) { return s.e3 && (s.e3.halt || {}).shown; });
    if (halts.length) {
      var h = halts[0].e3.halt;
      add("E3 halt", "A failing wave (" + esc(h.rolled_back_wave) + ") was rolled back and the migration <strong>stopped</strong>; " +
        int(h.never_attempted) + " chargers were never attempted." + trust(halts[0]));
    } else none("E3 halt", "rollback + halt");
    var rots = all.filter(function (s) { return s.e3 && s.e3.rotation; });
    if (rots.length) {
      var rt = rots[0].e3;
      add("E3 rotation", "<strong>" + int(rt.rotation.completed) + " keys rotated</strong>, " + int(rt.rotation.failed) +
        " failed (old key kept); " + int(rt.charging.sessions_disturbed) + " sessions disturbed." + trust(rots[0]));
    } else none("E3 rotation", "key rotation without disconnection");

    var e4 = (DATA.e4 || { rows: [] }).rows, pqc = e4.filter(isPqCert);
    if (pqc.length) {
      add("E4", pqc.map(function (x) {
        return esc(x.label) + " " + int(x.pem_bytes != null ? x.pem_bytes : x.bytes) + " B vs " + int(x.limit) + " — " +
          (x.within_limit ? "fits" : "<strong>over the limit</strong>");
      }).join(" · ") + ".");
    } else if (e4.length) {
      var ch = e4.filter(function (x) { return x.limit && x.group === "classical"; });
      add("E4", "Classical certificates fit (largest " + int(Math.max.apply(null, ch.map(function (x) { return x.pem_bytes || x.bytes; }))) +
        " B); the ML-DSA-44 signature (2,420 B) travels inside DataTransfer. ML-DSA certificate sizes not measured here.");
    } else none("E4", "size limits");

    var e5 = all.filter(function (s) { return s.e5; });
    var cut = 0, bootRej = 0, idRej = 0;
    e5.forEach(function (s) {
      cut += s.e5.pq_cut_off || 0; idRej += s.e5.identity_rejected || 0;
      bootRej += ((s.e5.pq_by_trigger || {}).boot || {}).rejected || 0;
    });
    if (cut || idRej || bootRej) {
      add("E5", "Impostors refused: <strong>" + int(bootRej) + "</strong> failed the post-quantum key check at boot (" + int(cut) +
        " connections closed, code 1008); " + int(idRej) + " refused by the certificate identity check.");
    } else none("E5", "attack demonstration");

    root.insertAdjacentHTML("beforeend", '<section id="headlines"><h2>Headlines</h2><p class="lede">One line per ' +
      "experiment, taken from the runs below (largest fleet first). Trust status is shown where it is not a clean pass.</p>" +
      '<div class="card"><ul class="headlines">' + lines.join("") + "</ul></div></section>");
  }

  function renderRuns(root) {
    var list = slots();
    var html = '<section id="runs"><h2>Runs</h2><p class="lede">One row per test setup ' +
      "(experiment, number of chargers, security mode, TLS). Re-running a setup replaces its " +
      "row. Every run is checked before its numbers are shown: the server's diary and the " +
      "tester's diary must agree.</p>";

    var dc = DATA.diary_check || { status: "pass", issues: [] };
    if (dc.issues && dc.issues.length) {
      html += '<div class="banner">' + badge(dc.status) + " Server diary: " +
        dc.issues.map(function (i) { return esc(i.message); }).join(" ") + "</div>";
    }

    if (!list.length) {
      html += empty("No runs analysed yet. Run the load generator, for example " +
        "<code>python -m harness.load_generator --n 25 --experiment e1</code>, " +
        "then <code>python -m analysis.run</code>.");
      root.insertAdjacentHTML("beforeend", html + "</section>");
      return;
    }

    html += table(
      [{ t: "Setup" }, { t: "Chargers", num: true }, { t: "Mode" }, { t: "TLS" },
       { t: "Started (UTC)" }, { t: "Duration", num: true }, { t: "Sessions completed", num: true },
       { t: "Energy", num: true }, { t: "Trust" }],
      list.map(function (s) {
        var ov = s.overview || {};
        var issues = (s.trust.issues || []).filter(function (i) { return i.level !== "pass"; });
        var trust = badge(s.trust.status) + (issues.length
          ? '<details class="issues"><summary>' + issues.length + " note(s)</summary><ul>" +
            issues.map(function (i) { return "<li>" + esc(i.message) + "</li>"; }).join("") +
            "</ul></details>" : "");
        return [
          esc(s.experiment) + ((s.joined_from || []).length
            ? ' <span class="sub">(' + s.joined_from.length + " load generators: " +
              s.joined_from.map(function (p) { return int(p.n_stations); }).join(" + ") + ")</span>" : ""),
          int(s.n_stations), modeCell(s.crypto_mode), s.tls ? "On" : "Off",
          esc((s.started_at || "").replace("T", " ").slice(0, 19)), sec(s.wall_s),
          int(ov.sessions && ov.sessions.stations_with_completed_session) + " / " + int(ov.stations && ov.stations.spawned),
          isNum(ov.sessions && ov.sessions.energy_wh_total) ? fmt(ov.sessions.energy_wh_total / 1000, 3) + " kWh" : "—",
          trust
        ];
      })
    );

    // -- one run in detail -------------------------------------------------
    html += '<div style="height:20px"></div><div class="controls"><label for="runPick"><strong>Run in detail</strong></label>' +
      '<select id="runPick">' + list.map(function (s) {
        return '<option value="' + esc(s.key) + '"' + (s.key === selectedSlot ? " selected" : "") + ">" +
          esc(slotLabel(s)) + "</option>";
      }).join("") + "</select></div><div id=\"runDetail\"></div>";
    root.insertAdjacentHTML("beforeend", html + "</section>");

    document.getElementById("runPick").addEventListener("change", function (e) {
      selectedSlot = e.target.value;
      show();
      var el = document.getElementById("runs");
      if (el) el.scrollIntoView();
    });
    renderRunDetail(document.getElementById("runDetail"), DATA.slots[selectedSlot]);
  }

  function renderRunDetail(el, s) {
    var ov = s.overview;
    var lost = ov.meter.missing_events;
    el.innerHTML =
      '<div class="tiles">' +
      tile("Sessions completed", int(ov.sessions.stations_with_completed_session) + " / " + int(ov.stations.spawned), "chargers that finished charging") +
      tile("Energy delivered", fmt(ov.sessions.energy_wh_total / 1000, 3) + " kWh", "sum of final meter readings") +
      tile("Meter readings", int(ov.meter.readings), int(ov.meter.replayed_from_offline_queue) + " replayed after an outage") +
      tile("Readings lost", int(lost), lost ? "see E2 for where they were lost" : "none — every reading arrived") +
      tile("Reconnections", int(ov.connections.reconnections), int(ov.connections.connect_timeouts) + " connection time-outs") +
      "</div>" +
      '<div class="grid two">' +
      '<div class="card"><h3>Chargers connected and charging</h3><p class="sub">Counted from the server diary, second by second</p><div class="chart" id="cConn"></div></div>' +
      '<div class="card"><h3>Fleet power</h3><p class="sub">Sum of the latest accepted meter readings (kW); a disconnected charger stops counting</p><div class="chart" id="cPower"></div></div>' +
      "</div>" + machineCard(s);

    var tl = ov.timeline;
    mount(document.getElementById("cConn"), function (p) {
      var o = base(p, "connected_" + s.key.replace(/\|/g, "_"));
      o.tooltip.trigger = "axis";
      o.tooltip.axisPointer = { type: "line", lineStyle: { color: p.axis } };
      o.xAxis = valueAxis(p, "Seconds since the run started", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "Chargers", { minInterval: 1 });
      o.series = [
        line("Connected", p.muted, tl.connected, { step: "end" }),
        line("Charging", p.alt, tl.charging, { step: "end" })
      ];
      return o;
    });
    mount(document.getElementById("cPower"), function (p) {
      var o = base(p, "power_" + s.key.replace(/\|/g, "_"));
      o.legend.show = false;
      o.tooltip.trigger = "axis";
      o.tooltip.valueFormatter = function (v) { return fmt(v, 1) + " kW"; };
      o.xAxis = valueAxis(p, "Seconds since the run started", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "kW");
      o.series = [line("Fleet power", p.alt, tl.power_w.map(function (d) { return [d[0], d[1] / 1000]; }),
        { step: "end", areaStyle: { color: p.alt, opacity: 0.12 } })];
      return o;
    });
    mountMachine(s);
  }

  // ------------------------------------------------- machine (C-P6): is the laptop overloaded?

  function machineCard(s) {
    var m = s.machine;
    if (!m) return "";
    var sv = m.server || {};
    return '<div class="card" style="margin-top:16px"><h3>Test machine</h3><p class="sub">' +
      "Recorded once a second with <code>--watch-machine</code>. If the laptop is overloaded, every time in this run is " +
      "partly the laptop's cost. Loop lag = how late a 1 s timer fired inside the tester (all chargers share it).</p>" +
      (m.saturated ? '<div class="banner">' + badge("warn") + " Saturated: " + esc(m.saturation_reasons.join("; ")) + "</div>" : "") +
      '<div class="tiles">' +
      tile("Machine CPU (p95)", isNum(m.cpu_pct.p95) ? fmt(m.cpu_pct.p95, 0) + "%" : "—",
        "limit " + fmt(m.limits.cpu_p95_pct, 0) + "% · max " + (isNum(m.cpu_pct.max) ? fmt(m.cpu_pct.max, 0) + "%" : "—")) +
      tile("Loop lag (p95)", ms(m.loop_lag_ms.p95), "limit " + ms(m.limits.loop_lag_p95_ms) + " · max " + ms(m.loop_lag_ms.max)) +
      tile("Memory (max)", isNum(m.mem_pct.max) ? fmt(m.mem_pct.max, 0) + "%" : "—", fmt(m.mem_used_mb_max, 0) + " MB in use") +
      tile("Tester process", isNum(m.tester.cpu_pct.p95) ? fmt(m.tester.cpu_pct.p95, 0) + "% CPU" : "—",
        "p95, % of one core · " + fmt(m.tester.rss_mb_max, 0) + " MB") +
      tile("Server process", sv.found && isNum((sv.cpu_pct || {}).p95) ? fmt(sv.cpu_pct.p95, 0) + "% CPU" : "not found",
        sv.found ? "p95, % of one core · " + fmt(sv.rss_mb_max, 0) + " MB" : "ran elsewhere, or not running") +
      "</div>" +
      '<div class="chart" id="cMachine"></div></div>';
  }

  function mountMachine(s) {
    var m = s.machine, el = document.getElementById("cMachine");
    if (!m || !el) return;
    mount(el, function (p) {
      var o = base(p, "machine_" + s.key.replace(/\|/g, "_"));
      o.tooltip.trigger = "axis";
      o.tooltip.axisPointer = { type: "line", lineStyle: { color: p.axis } };
      o.xAxis = valueAxis(p, "Seconds since the run started", { splitLine: { show: false } });
      o.yAxis = [valueAxis(p, "CPU (%)", { max: 100 }), valueAxis(p, "Loop lag (ms)", { splitLine: { show: false } })];
      o.series = [
        line("Machine CPU %", p.alt, m.timeline.cpu_pct, {}),
        line("Loop lag (ms)", p.status.warning, m.timeline.loop_lag_ms, { yAxisIndex: 1 })
      ];
      return o;
    });
  }

  function renderE1(root) {
    var all = slots().filter(function (s) { return s.e1 && s.e1.station_connect_ms && s.e1.station_connect_ms.n; });
    var exps = [];
    all.forEach(function (s) { if (exps.indexOf(s.experiment) < 0) exps.push(s.experiment); });
    if (!e1Exp || (e1Exp !== "all" && exps.indexOf(e1Exp) < 0)) e1Exp = exps.indexOf("e1") >= 0 ? "e1" : "all";
    var list = e1Exp === "all" ? all : all.filter(function (s) { return s.experiment === e1Exp; });
    var html = '<section id="e1"><h2>E1 · Handshake cost</h2><p class="lede">How long a charger takes ' +
      "to connect securely — network connection, TLS encryption, certificate exchange and protocol " +
      "upgrade — measured by the charger itself from dial to ready. Two modes are compared: <strong>classical</strong> " +
      "and <strong>hybrid</strong> (classical TLS + a post-quantum ML-DSA key check at every boot). Pure post-quantum TLS " +
      "was not built (future work, L17). The box shows where the middle " +
      "half of chargers fell; <strong>p95</strong> is the time 95 of every 100 chargers beat. " +
      "<strong>Ready</strong> (Contract 7) is measured from the same dial: a classical charger is ready when " +
      "the server accepts its boot; a hybrid charger only once it has also passed its post-quantum key check.</p>";

    if (!list.length) {
      root.insertAdjacentHTML("beforeend", html + empty(
        "No run has charger-side connection times yet. They are recorded from the C5 fix onwards " +
        "(<code>connect_ms</code>). Run, for example, <code>python -m harness.load_generator --n 50 " +
        "--experiment e1 --csms-url wss://localhost:9000</code> once per security mode.") + "</section>");
      return;
    }

    html += '<div class="controls"><label for="e1Pick"><strong>Experiment</strong></label><select id="e1Pick">' +
      exps.concat(["all"]).map(function (x) {
        return '<option value="' + esc(x) + '"' + (x === e1Exp ? " selected" : "") + ">" +
          (x === "all" ? "All runs (mixed conditions)" : esc(x)) + "</option>";
      }).join("") + '</select><span class="sub" style="margin:0">Connection times from storm or migration runs are taken under load — compare like with like.</span></div>';
    html += '<div class="grid two">' +
      '<div class="card"><h3>Connection time per run</h3><p class="sub">Box = middle half of chargers, line = median, whiskers = typical range, dots = outliers</p><div class="chart" id="cE1box"></div></div>' +
      '<div class="card"><h3>Share of chargers connected within a time</h3><p class="sub">Read across at 95% to find each run\'s p95</p><div class="chart" id="cE1cdf"></div></div>' +
      "</div>";

    var cmpAll = DATA.comparisons.e1_vs_n || {}, cmp = {};
    Object.keys(cmpAll).forEach(function (k) {
      if (e1Exp === "all" || cmpAll[k][0].experiment === e1Exp) cmp[k] = cmpAll[k];
    });
    var multiN = Object.keys(cmp).some(function (k) { return cmp[k].length > 1; });
    if (multiN) {
      html += '<div class="card" style="margin-top:16px"><h3>Connection time against fleet size</h3>' +
        '<p class="sub">Median (solid) and p95 (dashed) per security mode; like is compared with like (same TLS setting)</p>' +
        '<div class="chart" id="cE1n"></div></div>';
    }

    html += '<div class="card" style="margin-top:16px"><h3>Numbers</h3><p class="sub">95% CI = the range the true median very likely lies in. Server upgrade = the part of the handshake the server itself can time.</p>' +
      table(
        [{ t: "Run" }, { t: "Mode" }, { t: "Chargers", num: true }, { t: "Median", num: true },
         { t: "95% CI", num: true }, { t: "p95", num: true }, { t: "p99", num: true },
         { t: "Server upgrade (median)", num: true }, { t: "Bytes per connection", num: true }, { t: "TLS" }],
        list.map(function (s) {
          var d = s.e1.station_connect_ms, ci = d.median_ci95;
          return [esc(slotShort(s)), modeCell(s.crypto_mode), int(d.n), ms(d.median),
            ci ? ms(ci[0]) + " – " + ms(ci[1]) : "—", ms(d.p95), ms(d.p99),
            ms(s.e1.server_upgrade_ms && s.e1.server_upgrade_ms.median),
            bytes(s.e1.bytes_per_connection && s.e1.bytes_per_connection.median),
            esc((s.e1.tls && s.e1.tls.version) || (s.tls ? "?" : "off"))];
        })) + "</div>";

    var readyRows = list.filter(function (s) { return s.e1.ready_ms && s.e1.ready_ms.n; });
    if (readyRows.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Time until ready</h3><p class="sub">From the same dial. Boot accepted = the server accepted the charger. Secure-ready = boot accepted <em>and</em> the post-quantum key check answered (hybrid, chargers that already held a key). Signing = the charger\'s signing time alone.</p>' +
        table([{ t: "Run" }, { t: "Mode" }, { t: "Ready means" }, { t: "Chargers", num: true },
               { t: "Ready (median)", num: true }, { t: "Ready (p95)", num: true },
               { t: "Boot accepted (median)", num: true }, { t: "Secure-ready (median)", num: true },
               { t: "Signing (median)", num: true }],
          readyRows.map(function (s) {
            var e = s.e1, r = e.ready_ms, b = e.boot_ready_ms || { n: 0 },
              sr = e.secure_ready_ms || { n: 0 }, sg = e.sign_ms || { n: 0 };
            return [esc(slotShort(s)), modeCell(s.crypto_mode), esc(e.ready_basis), int(r.n),
              ms(r.median), ms(r.p95), b.n ? ms(b.median) : "—", sr.n ? ms(sr.median) : "—",
              sg.n ? ms(sg.median) : "—"];
          })) + "</div>";
    }

    var over = (DATA.comparisons.overhead_vs_classical || []).filter(function (r) {
      return e1Exp === "all" || r.experiment === e1Exp;
    });
    if (over.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Overhead against classical</h3><p class="sub">Same experiment, same number of chargers, same TLS setting — how many times slower</p>' +
        table([{ t: "Experiment" }, { t: "Chargers", num: true }, { t: "Mode" }, { t: "Median", num: true },
               { t: "Classical median", num: true }, { t: "× median", num: true }, { t: "× p95", num: true }],
          over.map(function (r) {
            return [esc(r.experiment), int(r.n), modeCell(r.mode), ms(r.median_ms), ms(r.classical_median_ms),
              isNum(r.median_ratio) ? fmt(r.median_ratio, 2) + "×" : "—",
              isNum(r.p95_ratio) ? fmt(r.p95_ratio, 2) + "×" : "—"];
          })) + "</div>";
    }
    var rover = (DATA.comparisons.ready_overhead_vs_classical || []).filter(function (r) {
      return e1Exp === "all" || r.experiment === e1Exp;
    });
    if (rover.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Time until ready, against classical</h3><p class="sub">Same dial start. Classical = boot accepted; hybrid = boot accepted and key check answered. Added = the extra milliseconds the post-quantum check costs.</p>' +
        table([{ t: "Experiment" }, { t: "Chargers", num: true }, { t: "Mode" }, { t: "Ready means" },
               { t: "Median", num: true }, { t: "Classical median", num: true }, { t: "Added", num: true },
               { t: "× median", num: true }, { t: "× p95", num: true }],
          rover.map(function (r) {
            return [esc(r.experiment), int(r.n), modeCell(r.mode), esc(r.basis), ms(r.median_ms), ms(r.classical_median_ms),
              ms(r.added_median_ms),
              isNum(r.median_ratio) ? fmt(r.median_ratio, 2) + "×" : "—",
              isNum(r.p95_ratio) ? fmt(r.p95_ratio, 2) + "×" : "—"];
          })) + "</div>";
    }
    root.insertAdjacentHTML("beforeend", html + "</section>");
    document.getElementById("e1Pick").addEventListener("change", function (ev) {
      e1Exp = ev.target.value;
      show();
      var el = document.getElementById("e1");
      if (el) el.scrollIntoView();
    });

    var labels = list.map(slotTag);
    mount(document.getElementById("cE1box"), function (p) {
      var o = base(p, "e1_boxplot");
      o.legend.show = false;
      o.tooltip.trigger = "item";
      o.tooltip.formatter = function (it) {
        if (it.seriesType === "scatter") return esc(labels[it.value[0]]) + "<br>outlier " + ms(it.value[1]);
        var v = it.value;
        return "<strong>" + esc(slotLabel(list[it.dataIndex])) + "</strong><br>max (whisker) " + ms(v[5]) +
          "<br>upper quarter " + ms(v[4]) + "<br>median " + ms(v[3]) + "<br>lower quarter " + ms(v[2]) +
          "<br>min (whisker) " + ms(v[1]);
      };
      o.xAxis = catAxis(p, labels, { axisLabel: { color: p.muted, fontSize: 11, interval: 0, rotate: labels.length > 4 ? 30 : 0 } });
      o.yAxis = valueAxis(p, "Connection time (ms)");
      o.grid.bottom = labels.length > 4 ? 72 : 48;
      o.series = [
        {
          type: "boxplot", name: "Connection time", boxWidth: [10, 36],
          data: list.map(function (s) {
            var b = s.e1.station_connect_ms.box, c = modeColor(p, s.crypto_mode);
            return {
              value: [b.whisker_low, b.q1, b.median, b.q3, b.whisker_high],
              itemStyle: { color: p.surface, borderColor: c, borderWidth: 2 }
            };
          })
        },
        {
          type: "scatter", name: "Outliers", symbolSize: 8,
          data: [].concat.apply([], list.map(function (s, i) {
            return s.e1.station_connect_ms.box.outliers.map(function (v) {
              return { value: [i, v], itemStyle: { color: modeColor(p, s.crypto_mode), borderColor: p.surface, borderWidth: 2 } };
            });
          }))
        }
      ];
      return o;
    });

    mount(document.getElementById("cE1cdf"), function (p) {
      var o = base(p, "e1_cdf");
      o.tooltip.trigger = "axis";
      o.tooltip.axisPointer = { type: "line", lineStyle: { color: p.axis } };
      o.tooltip.valueFormatter = function (v) { return fmt(v, 1) + "%"; };
      o.xAxis = valueAxis(p, "Connection time (ms)", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "Chargers connected (%)", { max: 100 });
      o.legend.data = list.map(slotTag);
      o.grid.right = 96;
      o.series = list.map(function (s) {
        return line(slotTag(s), modeColor(p, s.crypto_mode), s.e1.station_connect_ms.ecdf,
          { step: "end", endLabel: { show: list.length <= 4, formatter: slotTag(s), color: p.ink2, fontSize: 11 } });
      });
      return o;
    });

    if (multiN) {
      mount(document.getElementById("cE1n"), function (p) {
        var o = base(p, "e1_vs_fleet_size");
        o.tooltip.trigger = "item";
        o.tooltip.formatter = function (it) { return esc(it.seriesName) + "<br>" + int(it.value[0]) + " chargers: " + ms(it.value[1]); };
        o.xAxis = valueAxis(p, "Chargers in the run", { splitLine: { show: false }, minInterval: 1 });
        o.yAxis = valueAxis(p, "Connection time (ms)");
        o.series = [];
        Object.keys(cmp).forEach(function (name) {
          var rows = cmp[name], c = modeColor(p, rows[0].mode);
          o.series.push(line(name + " median", c, rows.map(function (r) { return [r.n, r.median]; }), { showSymbol: true }));
          o.series.push(line(name + " p95", c, rows.map(function (r) { return [r.n, r.p95]; }),
            { showSymbol: true, symbol: "emptyCircle", lineStyle: { width: 2, color: c, type: "dashed" } }));
        });
        return o;
      });
    }
  }

  // ---------------------------------------------------------------- E2

  function renderE2(root) {
    var list = slots().filter(function (s) { return s.e2; });
    var html = '<section id="e2"><h2>E2 · Reconnection storm</h2><p class="lede">The server is killed ' +
      "and restarted mid-run. Every charger reconnects at once, each doing a full secure handshake. " +
      "A charger counts as <strong>recovered</strong> when it is connected <em>and</em> the server has " +
      "accepted it again (Track A's definition, fixed for every run). In hybrid mode an enrolled charger must " +
      "<em>also</em> pass its post-quantum key check after the boot (Contract 7). The clock starts at the restart.</p>";

    if (!list.length) {
      root.insertAdjacentHTML("beforeend", html + empty(
        "No storm run yet. Run, for example: <code>python -m harness.load_generator --n 100 --experiment e2 " +
        "--server-cmd \"python -m csms.server --ws-ping-interval 0\" --storm-at 20 --storm-for 10 " +
        "--watch-fleet http://localhost:9000/api/fleet</code>") + "</section>");
      return;
    }

    html += '<div class="card"><h3>Fleet recovery after the restart</h3><p class="sub">Share of chargers back, against seconds since the server came back</p><div class="chart tall" id="cE2"></div></div>';
    var cmp = DATA.comparisons.e2_vs_n || {};
    var multiN = Object.keys(cmp).some(function (k) { return cmp[k].length > 1; });
    if (multiN) {
      html += '<div class="card" style="margin-top:16px"><h3>Time for 95% of the fleet to recover, against fleet size</h3><p class="sub">One line per security mode</p><div class="chart" id="cE2n"></div></div>';
    }
    html += '<div class="card" style="margin-top:16px"><h3>Numbers</h3><p class="sub">T50 / T95 / T100 = seconds until half / 95% / all chargers were back. Lost readings are split by where they were lost.</p>' +
      table(
        [{ t: "Run" }, { t: "Mode" }, { t: "Outage", num: true }, { t: "Had to recover", num: true },
         { t: "T50", num: true }, { t: "T95", num: true }, { t: "T100", num: true }, { t: "Never recovered", num: true },
         { t: "Sessions kept", num: true }, { t: "Lost in transit", num: true }, { t: "Dropped by charger queue", num: true },
         { t: "Replayed", num: true }, { t: "T95 (fleet polls)", num: true }, { t: "Recovered means" }],
        list.map(function (s) {
          var e = s.e2, g = e.integrity;
          return [esc(slotShort(s)), modeCell(s.crypto_mode), sec(e.outage_s), int(e.population),
            sec(e.t50_s), sec(e.t95_s), sec(e.t100_s), int(e.unrecovered),
            int(g.sessions_resumed_after_restart) + " / " + int(g.sessions_in_flight_at_kill),
            int(g.events_lost_in_transit), int(g.events_dropped_by_agent_queue),
            int(g.readings_replayed_from_offline_queue), sec(e.snapshot_t95_s),
            esc(e.recovery_rule === "boot + key check"
              ? "boot + key check (" + int(e.key_checked_population) + " chargers)" : "boot accepted")];
        })) + "</div>";
    root.insertAdjacentHTML("beforeend", html + "</section>");

    mount(document.getElementById("cE2"), function (p) {
      var o = base(p, "e2_recovery");
      o.tooltip.trigger = "axis";
      o.tooltip.axisPointer = { type: "line", lineStyle: { color: p.axis } };
      o.tooltip.valueFormatter = function (v) { return fmt(v, 1) + "%"; };
      o.xAxis = valueAxis(p, "Seconds since the server restarted", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "Fleet recovered (%)", { max: 100 });
      o.grid.right = 120;
      o.series = list.map(function (s) {
        return line(slotTag(s), modeColor(p, s.crypto_mode), s.e2.curve, {
          step: "end",
          endLabel: { show: list.length <= 6, formatter: slotTag(s), color: p.ink2, fontSize: 11 },
          markLine: list.length === 1 ? {
            symbol: "none", silent: true, z: 30, lineStyle: { color: p.axis, type: "dashed" },
            label: { color: p.ink2, formatter: "{b}" },
            data: [{ yAxis: 95, name: "95%" }]
          } : undefined
        });
      });
      return o;
    });

    if (multiN) {
      mount(document.getElementById("cE2n"), function (p) {
        var o = base(p, "e2_t95_vs_fleet_size");
        o.tooltip.trigger = "item";
        o.tooltip.formatter = function (it) { return esc(it.seriesName) + "<br>" + int(it.value[0]) + " chargers: " + sec(it.value[1]); };
        o.xAxis = valueAxis(p, "Chargers in the run", { splitLine: { show: false }, minInterval: 1 });
        o.yAxis = valueAxis(p, "T95 (s)");
        o.series = Object.keys(cmp).map(function (name) {
          var rows = cmp[name];
          return line(name, modeColor(p, rows[0].mode), rows.filter(function (r) { return isNum(r.t95); })
            .map(function (r) { return [r.n, r.t95]; }), { showSymbol: true });
        });
        return o;
      });
    }
  }

  // ---------------------------------------------------------------- E3

  function renderE3(root) {
    var list = slots().filter(function (s) { return s.e3; });
    var html = '<section id="e3"><h2>E3 · Migration under load</h2><p class="lede">The fleet is switched ' +
      "to post-quantum while chargers are charging: a small <strong>canary</strong> (test) group first, then " +
      "<strong>waves</strong>. If too many chargers in a wave fail, that wave is <strong>rolled back</strong>. " +
      "The question is whether charging carried on undisturbed.</p>";
    if (!list.length) {
      root.insertAdjacentHTML("beforeend", html + empty(
        "No migration run yet. This fills in once the migration controller is wired into the server " +
        "(A+B+C session) and a run is started with <code>--watch-fleet</code> and a migration is " +
        "triggered from the dashboard or <code>GET /api/migration/start?target_mode=hybrid</code>.") + "</section>");
      return;
    }
    var s = list.filter(function (x) { return x.key === selectedSlot; })[0] || list[0];
    var e = s.e3, c = e.charging, q = e.pq_checks || { total: 0, passed: 0, rejected: 0 };
    var d = e.deferred || { count: 0, station_ids: [] };
    var verifiedStatus = { "authenticated": "pass", "key installed (not authenticated)": "warn",
      "partly authenticated": "warn", "nothing migrated": "info" }[e.verification] || "info";
    html += '<p class="sub">Showing: ' + esc(slotLabel(s)) + (list.length > 1 ? " (pick another run under Runs)" : "") +
      " · whole fleet: " + int(e.fleet_size) + " chargers, " + int(e.tester_chargers) + " started by the tester</p>" +
      '<div class="banner">' + badge(verifiedStatus).replace(/Trusted|Caveats|Note|Not usable/, esc(
        e.verification === "authenticated" ? "Authenticated" :
        e.verification === "key installed (not authenticated)" ? "Key installed only" :
        e.verification === "partly authenticated" ? "Partly authenticated" : "Nothing migrated")) +
      " " + esc(e.verification === "authenticated"
        ? "Every upgraded charger proved it holds its new key (signed a fresh challenge that the server verified)."
        : e.verification === "key installed (not authenticated)"
          ? "Chargers received keys, but the server never checked them. Report as 'key installed', not 'authenticated'."
          : e.verification === "partly authenticated"
            ? "Some upgraded chargers have no passed key check."
            : "No charger was upgraded in this run.") + "</div>" +
      (e.kind === "rotation" ? '<p class="note">This run only rotated keys of chargers that were already upgraded: ' +
        "see <strong>Key rotation</strong> below. The migration counts do not apply.</p>" : "") +
      '<div class="tiles"' + (e.kind === "rotation" ? " hidden" : "") + ">" +
      tile("Upgraded", int(e.final_counts.migrated), "chargers on the new security") +
      tile("Key checks passed", int(q.passed) + " / " + int(q.total), int(q.rejected) + " rejected") +
      tile("Rolled back", int(e.final_counts.rolled_back), int(e.rollbacks) + " wave rollback(s)" +
        (e.manual_rollbacks ? ", " + int(e.manual_rollbacks) + " manual" : "")) +
      tile("Incompatible", int(e.final_counts.incompatible), "skipped, left on classical") +
      tile("Skipped (offline)", int(d.count), "not attempted, not failed") +
      tile("Duration", sec(e.duration_s), "migration start to finish") +
      tile("Sessions disturbed", int(c.sessions_disturbed) + " / " + int(c.sessions_running_at_start), "tester's chargers charging when it began") +
      ((e.pq_enrolled || {}).count ? tile("Keys made by chargers", int(e.pq_enrolled.count), "private key never left the charger") : "") +
      ((e.boot_checks || {}).total ? tile("Boot key checks", int(e.boot_checks.passed) + " / " + int(e.boot_checks.total),
        "after each boot; not part of the migration") : "") +
      "</div>" +
      ((e.keys_at_start || {}).count ? '<div class="banner">' + badge("warn") + " " + int(e.keys_at_start.count) +
        " charger(s) started with a key saved by an earlier run, so for them this migration was a key rotation, not a first enrolment.</div>" : "") +
      '<div class="card"><h3>Chargers by migration state (whole fleet)</h3><p class="sub">Stacked: every charger is in exactly one state; vertical lines mark waves</p><div class="chart tall" id="cE3"></div></div>';
    var rt = q.round_trip_ms || { n: 0 };
    if (rt.n) {
      html += '<div class="grid two" style="margin-top:16px">' +
        '<div class="card"><h3>Key check round-trip time</h3><p class="sub">Server sends a challenge, charger signs it, server verifies (' +
        esc(q.algorithm || "ML-DSA") + '). Share of checks finished within a time.</p><div class="chart" id="cE3rt"></div></div>' +
        '<div class="card"><h3>Key check numbers</h3>' + table(
          [{ t: "Checks" }, { t: "n", num: true }, { t: "Median", num: true }, { t: "95% CI", num: true },
           { t: "p95", num: true }, { t: "Max", num: true }],
          [["All", rt], ["First per charger", q.first_per_charger_ms || { n: 0 }], ["Later (re-checks)", q.later_ms || { n: 0 }]]
            .filter(function (r) { return r[1].n; })
            .map(function (r) {
              var x = r[1], ci = x.median_ci95;
              return [esc(r[0]), int(x.n), ms(x.median), ci ? ms(ci[0]) + " – " + ms(ci[1]) : "—", ms(x.p95), ms(x.max)];
            })) +
        '<p class="note">A charger\'s first check may include one-time start-up of its post-quantum library.</p></div></div>';
    }
    if (q.rejections && q.rejections.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Rejected key checks</h3>' + table(
        [{ t: "Charger" }, { t: "Wave", num: true }, { t: "At", num: true }, { t: "Reason" }],
        q.rejections.map(function (r) { return [esc(r.station_id), esc(r.wave_id), sec(r.at_s), esc(r.detail)]; })) + "</div>";
    }
    if (d.count) {
      html += '<p class="note">Skipped because offline: ' + esc(d.station_ids.join(", ")) + "</p>";
    }
    var noKey = (e.agent_view || {}).migrated_but_no_key || [];
    if (noKey.length) {
      html += '<div class="banner">' + badge("warn") + " Server says upgraded, but the charger itself reports no key: " +
        esc(noKey.join(", ")) + "</div>";
    }
    if (e.failures && e.failures.length) {
      html += '<div class="banner">' + badge("fail") + " The migration controller crashed: " +
        esc(e.failures.map(function (f) { return f.error || "no error text"; }).join("; ")) + "</div>";
    }
    // C-F5: the halt (L09) and the key rotation (M5).
    var halt = e.halt || {};
    if (halt.shown) {
      html += '<div class="banner">' + badge("fail").replace("Not usable", "Halted") + " Wave " + esc(halt.rolled_back_wave) +
        " failed its checks and was rolled back, and the migration <strong>stopped</strong>: " + int(halt.never_attempted) +
        " charger(s) were never attempted. This is the safety behaviour working, not a fault.</div>";
    } else if (halt.rolled_back_wave !== null && halt.rolled_back_wave !== undefined) {
      html += '<p class="note">Wave ' + esc(halt.rolled_back_wave) + " was rolled back. It was the last wave, so the halt " +
        "itself (later waves not run) is not visible in this run.</p>";
    }
    var rot = e.rotation;
    if (rot) {
      var rq = rot.checks || { total: 0, passed: 0, round_trip_ms: { n: 0 } };
      html += '<div class="card" style="margin-top:16px"><h3>Key rotation (old key valid until the new one is proven)</h3>' +
        '<div class="tiles">' +
        tile("Rotated", int(rot.completed), "new key proven, old key dropped") +
        tile("Failed", int(rot.failed), "old key kept, still a post-quantum charger") +
        tile("Rotation key checks", int(rq.passed) + " / " + int(rq.total), "signed with the NEW key") +
        tile("Check time (median)", rq.round_trip_ms && rq.round_trip_ms.n ? ms(rq.round_trip_ms.median) : "—", "server sends, charger signs, server verifies") +
        tile("Sessions disturbed", int(c.sessions_disturbed) + " / " + int(c.sessions_running_at_start), "charging carried on") +
        "</div>" + table(
          [{ t: "Charger" }, { t: "Wave", num: true }, { t: "Old key" }, { t: "New key" }, { t: "Result" }, { t: "At", num: true }],
          (rot.keys || []).map(function (k) {
            return [esc(k.station_id), esc(k.wave_id), "<code>" + esc(k.old_key_id || "—") + "</code>",
              "<code>" + esc(k.new_key_id || "—") + "</code>",
              k.ok ? badge("pass").replace("Trusted", "Rotated") : badge("fail").replace("Not usable", "Kept old key") +
                (k.detail ? " " + esc(k.detail) : ""), sec(k.at_s)];
          })) + "</div>";
    }
    if (e.waves && e.waves.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Waves</h3>' + table(
        [{ t: "Wave" }, { t: "Chargers", num: true }, { t: "Upgraded", num: true }, { t: "Failed", num: true },
         { t: "Started", num: true }, { t: "Ended", num: true }, { t: "Result" }],
        e.waves.map(function (w) {
          return [w.is_canary ? "Canary" : "Wave " + esc(w.wave_id), int(w.stations), int(w.migrated), int(w.failed),
            sec(w.start_s), sec(w.end_s), esc(w.phase)];
        })) + "</div>";
    }
    root.insertAdjacentHTML("beforeend", html + "</section>");

    var order = ["pending", "in_progress", "migrated", "rolled_back", "incompatible"];
    var colorOf = { pending: "pending", in_progress: "warning", migrated: "good", rolled_back: "critical", incompatible: "serious" };
    mount(document.getElementById("cE3"), function (p) {
      var o = base(p, "e3_migration_states");
      // Stacked by hand: ECharts stacks the x value when both axes are numeric,
      // so each band's top edge is computed here and bands are drawn largest
      // first (each smaller band paints over the one below it). The raw count
      // rides along as a third number for the tooltip.
      var xs = (e.state_timeline.pending || []).map(function (d) { return d[0]; });
      var cumulative = xs.map(function () { return 0; });
      var bands = order.map(function (st) {
        var raw = e.state_timeline[st] || [];
        var pts = xs.map(function (x, i) {
          var v = raw[i] ? raw[i][1] : 0;
          cumulative[i] += v;
          return [x, cumulative[i], v];
        });
        return { st: st, pts: pts };
      });
      o.tooltip.trigger = "axis";
      o.tooltip.formatter = function (items) {
        if (!items.length) return "";
        var byState = {};
        items.forEach(function (it) { byState[it.seriesName] = it.data[2]; });
        return "<strong>" + sec(items[0].data[0]) + "</strong><br>" + order.map(function (st) {
          return esc(STATE_LABEL[st]) + ": " + int(byState[STATE_LABEL[st]]);
        }).join("<br>");
      };
      o.xAxis = valueAxis(p, "Seconds since the run started", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "Chargers", { minInterval: 1 });
      o.legend.data = order.map(function (st) { return STATE_LABEL[st]; });
      o.series = bands.slice().reverse().map(function (b, i) {
        var col = p.status[colorOf[b.st]];
        var sr = line(STATE_LABEL[b.st], col, b.pts, {
          step: "end", areaStyle: { color: col, opacity: 1 },
          lineStyle: { width: 1, color: p.surface }, z: 2 + i
        });
        return sr;
      });
      if (e.markers && e.markers.length) {
        // Wave markers as their own thin series (not markLine, which ECharts
        // always paints beneath filled areas). Gaps ("-") separate the lines.
        var top = cumulative.length ? Math.max.apply(null, cumulative) : 0;
        // Markers closer together than 2% of the time axis share one line and
        // one label (a fast migration puts canary and every wave within a
        // fraction of a second, and separate labels would print on top of
        // each other).
        var span = xs.length > 1 ? xs[xs.length - 1] - xs[0] : 1;
        var groups = [];
        e.markers.slice().sort(function (a, b) { return a.at_s - b.at_s; }).forEach(function (m) {
          if (m.event === "wave_completed") return;
          var label = { migration_started: "start", wave_started: m.wave_id === 0 ? "canary" : "wave " + m.wave_id,
            wave_rolled_back: (m.trigger === "manual" ? "manual rollback " : "rollback ") + m.wave_id,
            migration_completed: "end", migration_failed: "FAILED" }[m.event] || m.event;
          var g = groups[groups.length - 1];
          if (g && m.at_s - g.x <= span * 0.02) { g.labels.push(label); } else { groups.push({ x: m.at_s, labels: [label] }); }
        });
        var pts = [];
        groups.forEach(function (g) {
          var text = g.labels.length > 3 ? g.labels[0] + " … " + g.labels[g.labels.length - 1] : g.labels.join(" · ");
          pts.push([g.x, 0]);
          pts.push({ value: [g.x, top], label: { show: true, formatter: text, position: "top", color: p.ink, fontSize: 10 } });
          pts.push("-");
        });
        o.series.push({
          type: "line", name: "Waves", data: pts, z: 20, silent: true, showSymbol: true, symbol: "circle", symbolSize: 1,
          connectNulls: false, lineStyle: { color: p.ink, width: 1, type: "dashed" }, itemStyle: { color: p.ink },
          tooltip: { show: false }
        });
        o.grid.top = 56;
      }
      return o;
    });
    var rtq = (e.pq_checks || {}).round_trip_ms || { n: 0 };
    if (rtq.n) {
      mount(document.getElementById("cE3rt"), function (p) {
        var o = base(p, "e3_key_check_round_trip");
        o.legend.show = false;
        o.tooltip.trigger = "axis";
        o.tooltip.valueFormatter = function (v) { return fmt(v, 1) + "%"; };
        o.xAxis = valueAxis(p, "Round-trip time (ms)", { splitLine: { show: false } });
        o.yAxis = valueAxis(p, "Checks finished (%)", { max: 100 });
        o.series = [line("Key checks", modeColor(p, "pqc"), rtq.ecdf, { step: "end" })];
        return o;
      });
    }
  }

  // ---------------------------------------------------------------- E4

  // C-F5: an ML-DSA certificate or chain row (Track B's crypto/pq_x509.py), checked against a limit.
  function isPqCert(r) { return !!r.limit && /ML-DSA/i.test(String(r.algorithm || "") + " " + String(r.label || "")); }

  function renderE4(root) {
    var e = DATA.e4 || { rows: [], notes: [] };
    var html = '<section id="e4"><h2>E4 · Size limits</h2><p class="lede">Post-quantum keys and signatures ' +
      "are far larger than classical ones. OCPP caps a certificate at 5,500 characters and a certificate " +
      "chain at 10,000. Every size below is measured from what our own code produces. The fleet's own " +
      "certificates are classical (Option B); the post-quantum keys and signatures travel inside DataTransfer " +
      "messages (shown for scale)." + ((DATA.e4 || { rows: [] }).rows.some(isPqCert)
        ? " The <strong>ML-DSA certificates</strong> are real X.509 certificates built only to answer: would " +
          "post-quantum certificates fit OCPP's limits?" : "") + "</p>";
    if (!e.rows.length) {
      root.insertAdjacentHTML("beforeend", html + empty("Sizes could not be measured: " + esc((e.notes || []).join(" "))) + "</section>");
      return;
    }
    var rows = e.rows.slice().reverse();
    var groupColor = function (p, g) {
      g = String(g || "");
      return g === "classical" ? p.mode.classical : g === "on the wire" ? p.muted : p.mode.pqc;
    };
    html += '<div class="card"><h3>Size of each security artifact</h3><p class="sub">PEM text size where OCPP carries PEM, raw bytes otherwise. Blue = classical, aqua = post-quantum, grey = on-the-wire message</p><div class="chart tall" id="cE4"></div></div>' +
      '<div class="card" style="margin-top:16px"><h3>Numbers</h3>' + table(
        [{ t: "Group" }, { t: "Artifact" }, { t: "Algorithm" }, { t: "Bytes (binary)", num: true },
         { t: "PEM text", num: true }, { t: "Limit", num: true }, { t: "Fits?" }],
        e.rows.map(function (r) {
          return [esc(r.group), esc(r.label), esc(r.algorithm || ""), int(r.bytes), int(r.pem_bytes),
            r.limit ? int(r.limit) : "—",
            r.within_limit === null || r.within_limit === undefined ? "—" : (r.within_limit ? badge("pass").replace("Trusted", "Fits · " + int(r.headroom) + " spare") : badge("fail").replace("Not usable", "Over the limit"))];
        })) + ((e.notes || []).length ? '<p class="note">' + esc(e.notes.join(" ")) + "</p>" : "") + "</div>";
    root.insertAdjacentHTML("beforeend", html + "</section>");

    mount(document.getElementById("cE4"), function (p) {
      var o = base(p, "e4_sizes");
      o.legend.show = false;
      o.tooltip.trigger = "item";
      o.tooltip.formatter = function (it) {
        var r = rows[it.dataIndex];
        return "<strong>" + esc(r.label) + "</strong> (" + esc(r.group) + ")<br>" + bytes(it.value) + (r.algorithm ? "<br>" + esc(r.algorithm) : "");
      };
      o.grid.left = 260;
      o.grid.right = 40;
      o.yAxis = catAxis(p, rows.map(function (r) { return (r.group === "on the wire" ? "wire · " : r.group === "classical" ? "classical · " : "PQ · ") + r.label; }));
      var biggest = Math.max.apply(null, rows.map(function (r) { return r.pem_bytes != null ? r.pem_bytes : r.bytes; }));
      o.xAxis = valueAxis(p, "Bytes", { max: Math.max(11000, Math.ceil(biggest * 1.08 / 1000) * 1000) });
      o.series = [{
        type: "bar", barMaxWidth: 16,
        data: rows.map(function (r) {
          return { value: r.pem_bytes != null ? r.pem_bytes : r.bytes, itemStyle: { color: groupColor(p, r.group), borderRadius: [0, 4, 4, 0] } };
        }),
        markLine: {
          symbol: "none", silent: true, z: 30, lineStyle: { color: p.status.critical, type: "dashed", width: 1.5 },
          label: { color: p.ink2, fontSize: 11, formatter: "{b}" },
          data: [{ xAxis: e.limits.certificate, name: "5,500 certificate limit" }, { xAxis: e.limits.chain, name: "10,000 chain limit" }]
        }
      }];
      return o;
    });
  }

  // ---------------------------------------------------------------- E5

  function e5Verdicts(e) {
    var v = e.verdicts;
    if (!v) return "";
    var word = { stopped: "STOPPED", done: "DONE", not_seen: "not in this run", check: "CHECK: numbers differ" };
    var cls = { stopped: "ok", done: "ok", not_seen: "", check: "bad" };
    var boot = (e.pq_by_trigger || {}).boot || {};
    var c = e.curtailment || {};
    function n(a) { var o = c[a] || {}; return Object.keys(o).map(function (k) { return int(o[k]) + " " + esc(k); }).join(", ") || "0"; }
    var rows = [
      ["(a) Stolen certificate, no ML-DSA key", v.stolen_certificate,
        int(boot.rejected || 0) + " boot key checks rejected, " + int(e.pq_cut_off) + " connections closed 1008"],
      ["(b) Wrong identity", v.wrong_identity, int(e.identity_rejected) + " refused by the identity check"],
      ["(c) Curtailment", v.curtailment, "SetChargingProfile " + n("SetChargingProfile") + "; ClearChargingProfile " + n("ClearChargingProfile")]
    ];
    var out = (e.outside_ids || []).length
      ? '<p class="sub">Includes chargers started outside the load generator (the victim): ' + esc(e.outside_ids.join(", ")) + "</p>" : "";
    return '<div class="card" style="margin:16px 0"><h3>Attack verdicts</h3>' + out + table(
      [{ t: "Attack" }, { t: "Verdict" }, { t: "Evidence (server log)" }],
      rows.map(function (r) { return [r[0], '<span class="verdict ' + cls[r[1]] + '"><b>' + (word[r[1]] || esc(r[1])) + "</b></span>", r[2]]; })) + "</div>";
  }

  function renderE5(root) {
    var list = slots();
    var html = '<section id="e5"><h2>E5 · Security</h2><p class="lede">Three attacks on a hybrid server over ' +
      "mutual TLS (Track A, experiments/e5_attack.md): (a) a <b>stolen certificate</b> without the charger's " +
      "ML-DSA key gets through TLS but fails the key check after boot and is cut off (code 1008); (b) a " +
      "<b>wrong identity</b> (another charger's certificate) is refused at connection time; (c) <b>curtailment</b>: " +
      "one operator command changes every charger's power. Same rules as <code>python -m experiments.e5_check</code>. " +
      "Also counted, not a verdict: devices <b>refused during the TLS handshake</b> (forged, old or missing certificate; " +
      "runbook V3 a).</p>";
    if (!list.length) {
      root.insertAdjacentHTML("beforeend", html + empty("No runs yet.") + "</section>");
      return;
    }
    var s = DATA.slots[selectedSlot] || list[0], e = s.e5;
    var failures = Object.keys(e.connection_failures || {});
    html += '<p class="sub">Showing: ' + esc(slotLabel(s)) + " (pick another run under Runs)</p>" +
      '<div class="tiles">' +
      tile("Identity checks", int(e.identity_checks), e.identity_checks ? "certificate name vs charger id" : "only made when TLS is on") +
      tile("Rejected", int(e.identity_rejected), "wrong certificate for the id") +
      tile("Mismatches let in", int(e.identity_mismatches),
        e.identity_mode === "warn" ? "server in 'warn' mode: logged, not refused" : "certificate name did not match") +
      tile("Key checks", int(e.pq_passed) + " / " + int(e.pq_checks), int(e.pq_rejected) + " rejected (post-quantum)") +
      (e.pq_by_trigger && e.pq_by_trigger.boot.checks ? tile("Boot key checks", int(e.pq_by_trigger.boot.passed) + " / " +
        int(e.pq_by_trigger.boot.checks), "checked after every boot (hybrid)") : "") +
      tile("Cut off", int(e.pq_cut_off), "failed the key check after boot; connection closed") +
      tile("Refused at TLS", int((e.tls_refusals || {}).total),
        Object.keys((e.tls_refusals || {}).by_reason || {}).join("; ") || "forged, old or missing certificate (no charger id)") +
      tile("Connection failures", int(failures.reduce(function (a, k) { return a + e.connection_failures[k]; }, 0)), failures.join(", ") || "none") +
      tile("Protocol errors", int(e.callerrors), "CALLErrors reported by chargers") +
      tile("Commands received", int(e.commands_received), "e.g. power limits, remote stop") +
      "</div>" + e5Verdicts(e) +
      '<div class="card"><h3>Fleet power, with rejected identities marked</h3><p class="sub">Attack (c): a curtailment shows as a step down across the fleet, and back up when cleared</p><div class="chart" id="cE5"></div></div>';
    if (e.identity_rejections && e.identity_rejections.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Rejected identities</h3>' + table(
        [{ t: "Charger id" }, { t: "Certificate name" }, { t: "At", num: true }],
        e.identity_rejections.map(function (r) { return [esc(r.station_id), esc(r.certificate_name), sec(r.at_s)]; })) + "</div>";
    }
    if (e.pq_rejections && e.pq_rejections.length) {
      html += '<div class="card" style="margin-top:16px"><h3>Rejected post-quantum key checks</h3>' + table(
        [{ t: "Charger id" }, { t: "At", num: true }, { t: "When" }, { t: "Reason" }],
        e.pq_rejections.map(function (r) { return [esc(r.station_id), sec(r.at_s), esc(r.trigger || "migration"), esc(r.detail)]; })) + "</div>";
    }
    root.insertAdjacentHTML("beforeend", html + "</section>");

    mount(document.getElementById("cE5"), function (p) {
      var o = base(p, "e5_power_" + s.key.replace(/\|/g, "_"));
      o.legend.show = false;
      o.tooltip.trigger = "axis";
      o.tooltip.valueFormatter = function (v) { return fmt(v, 1) + " kW"; };
      o.xAxis = valueAxis(p, "Seconds since the run started", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "kW");
      var sr = line("Fleet power", p.alt, s.overview.timeline.power_w.map(function (d) { return [d[0], d[1] / 1000]; }),
        { step: "end", areaStyle: { color: p.alt, opacity: 0.12 } });
      if (e.identity_rejections && e.identity_rejections.length) {
        sr.markLine = {
          symbol: "none", silent: true, z: 30, lineStyle: { color: p.status.critical, type: "dashed" },
          label: { color: p.ink2, fontSize: 10, formatter: "{b}" },
          data: e.identity_rejections.map(function (r) { return { xAxis: r.at_s, name: "rejected " + r.station_id }; })
        };
      }
      o.series = [sr];
      return o;
    });
  }

  // ---------------------------------------------------------------- nodes

  function renderNodes(root) {
    var nodes = DATA.external_nodes || [];
    var html = '<section id="nodes"><h2>Hardware node &amp; E6 interoperability</h2><p class="lede">Chargers the ' +
      "server saw that the load generator did not start: the Raspberry Pi (CP0100) and any third-party OCPP " +
      "client (E6). The server treats them exactly like every other charger; only this report labels them.</p>";
    if (!nodes.length) {
      root.insertAdjacentHTML("beforeend", html + empty("None seen yet. Start the Pi agent or a third-party client against the server, then re-run the analysis.") + "</section>");
      return;
    }
    var yes = function (b) { return b ? badge("pass").replace("Trusted", "Yes") : badge("fail").replace("Not usable", "No"); };
    html += '<div class="card">' + table(
      [{ t: "Charger id" }, { t: "Kind" }, { t: "Connected" }, { t: "Accepted" }, { t: "Completed a session" },
       { t: "Sessions", num: true }, { t: "Energy (charger-reported)", num: true }, { t: "Key check" },
       { t: "Mode" }, { t: "TLS" }, { t: "Last seen (UTC)" }],
      nodes.map(function (n) {
        var pq = n.pq_auth ? (n.pq_auth === "success" ? badge("pass").replace("Trusted", "Passed")
          : badge("fail").replace("Not usable", "Rejected")) : (n.deferred ? "skipped (offline)" : "—");
        return [esc(n.station_id), n.kind === "hardware" ? "<strong>Raspberry Pi</strong>" : "External client",
          yes(n.connected), yes(n.accepted), yes(n.completed_session), int(n.sessions_completed),
          fmt(n.energy_wh, 2) + " Wh", pq, modeCell(n.crypto_mode), esc(n.tls_version || "off"),
          esc((n.last_seen || "").replace("T", " ").slice(0, 19))];
      })) + '<p class="note">Energy here is what each charger reported about itself. A simulator\'s figures are not physical, and these are never added to any fleet total.</p></div>';
    root.insertAdjacentHTML("beforeend", html + "</section>");
  }

  // ================================================================ render

  // ================================================================ views

  var VIEW_LABEL = { combined: "All runs combined", experiments: "Experiments (step 13)" };

  function asBundle(data) {
    if (data && data.views) return data;
    // Format 3 (one view, before runbook v1.3): show it as the experiments.
    return { format_version: data && data.format_version, generated_at: data && data.generated_at,
             current: "experiments", view_order: ["experiments"],
             views: { experiments: Object.assign({ kind: "experiments", name: "experiments" }, data) },
             combined: null };
  }

  function viewFromHash() {
    var m = /(?:^|[#&])view=([^&]+)/.exec(location.hash || "");
    return m ? decodeURIComponent(m[1]) : null;
  }

  function render(data) {
    BUNDLE = asBundle(data);
    var order = BUNDLE.view_order || [];
    var wanted = [viewFromHash(), VIEW, BUNDLE.current, order[0]];
    VIEW = null;
    for (var i = 0; i < wanted.length && !VIEW; i++) {
      if (wanted[i] && order.indexOf(wanted[i]) >= 0) VIEW = wanted[i];
    }
    show();
  }

  function pick(name) {
    VIEW = name;
    selectedSlot = null;
    e1Exp = null;
    try { history.replaceState(null, "", "#view=" + encodeURIComponent(name)); } catch (e) { /* file: */ }
    show();
    window.scrollTo(0, 0);
  }

  function renderViewBar(root) {
    var order = BUNDLE.view_order || [];
    var newest = order.filter(function (n) { return n !== "combined" && n !== "experiments"; })[0];
    var html = '<nav class="views" aria-label="Which results">' + order.map(function (n) {
      var v = BUNDLE.views[n] || {}, sum = v.summary || {};
      var sub = n === "combined" ? int(((BUNDLE.combined || {}).totals || {}).runs) + " runs"
        : n === "experiments" ? "classical vs hybrid evidence"
        : ((sum.started_at || "").replace("T", " ").slice(0, 16) + " UTC" + (n === newest ? " · newest" : ""));
      return '<button type="button" class="view' + (n === VIEW ? " on" : "") + '" data-view="' + esc(n) + '"' +
        (n === VIEW ? ' aria-current="true"' : "") + "><b>" + esc(VIEW_LABEL[n] || n) + "</b><span>" + esc(sub) +
        (n === BUNDLE.current ? " · analysed last" : "") + "</span></button>";
    }).join("") + "</nav>";
    root.insertAdjacentHTML("beforeend", html);
    Array.prototype.forEach.call(root.querySelectorAll("nav.views button"), function (b) {
      b.addEventListener("click", function () { pick(b.getAttribute("data-view")); });
    });
  }

  function show() {
    disposeAll();
    var root = document.getElementById("root");
    root.innerHTML = "";
    var order = BUNDLE.view_order || [];
    if (!order.length) {
      root.innerHTML = empty("Nothing analysed yet. After a demo run: <code>python -m analysis.run --run demo3</code> " +
        "(your run name). For the experiment runs: <code>python -m analysis.run --logs evidence\\&lt;folder&gt;</code>.");
      document.getElementById("generated").textContent = "";
      return;
    }
    renderViewBar(root);
    if (VIEW === "combined") {
      DATA = null;
      renderCombined(root, BUNDLE.combined);
      document.getElementById("generated").textContent =
        "Page built " + (BUNDLE.generated_at || "").replace("T", " ").slice(0, 19) + " UTC";
      document.getElementById("foot").innerHTML = "Every demo run analysed with <code>python -m analysis.run --run &lt;name&gt;</code>; " +
        "pooled numbers use every charger and key check of every run. Results format v" + esc(BUNDLE.format_version) + ".";
      return;
    }
    DATA = BUNDLE.views[VIEW];
    var list = slots();
    if (!selectedSlot || !DATA.slots[selectedSlot]) {
      // Default to the most recently started run.
      selectedSlot = null;
      list.forEach(function (s) {
        if (!selectedSlot || (s.started_at_epoch || 0) > (DATA.slots[selectedSlot].started_at_epoch || 0)) selectedSlot = s.key;
      });
    }
    if (DATA.kind === "demo") renderDemoSummary(root, DATA);
    else renderHeadlines(root);
    renderRuns(root);
    renderE1(root);
    renderE2(root);
    renderE3(root);
    renderE4(root);
    renderE5(root);
    renderNodes(root);

    document.getElementById("generated").textContent =
      "Analysed " + ((DATA.analysed_at || DATA.generated_at || "")).replace("T", " ").slice(0, 19) + " UTC";
    var src = DATA.sources || {};
    document.getElementById("foot").innerHTML =
      "Computed by <code>python -m analysis.run" + (DATA.kind === "demo" ? " --run " + esc(DATA.name) : "") + "</code> from " +
      esc(src.server_diary) + " (" + int(src.server_diary_lines) + " lines) and " + int((src.tester_diaries || []).length) +
      " tester diar" + (((src.tester_diaries || []).length === 1) ? "y" : "ies") + ". " +
      int(src.runs_found) + " run(s) found, newest per setup kept (" + int(src.runs_kept) + "). " +
      "Results format v" + esc(BUNDLE.format_version) + ". Charts: Apache ECharts.";
  }

  // ------------------------------------------------- one demo run: V1-V7 in numbers

  function yesNo(ok, text) {
    return '<span class="verdict ' + (ok ? "ok" : "none") + '">' + (ok ? "✓ " : "— ") + text + "</span>";
  }

  function renderDemoSummary(root, v) {
    var r = v.summary || {}, m = r.migration || {}, a = r.attacks || {}, st = r.storm, rot = r.rotation,
      h = r.halt, b = r.boot_checks;
    var attacks = (a.tls_refused || 0) + (a.identity_rejected || 0) + (a.cut_off || 0);
    var rows = [];
    function add(id, name, ok, text) {
      rows.push("<tr><td><strong>" + id + "</strong></td><td>" + name + "</td><td>" + yesNo(ok, text) + "</td></tr>");
    }
    add("V1", "Normal charging", r.chargers > 0, int(r.chargers) + " chargers (" + int(r.tester_chargers) + " from the load generators" +
      ((r.outside_chargers || []).length ? " + " + esc(r.outside_chargers.join(", ")) : "") + "), connect median " + ms(r.connect_median_ms));
    add("V2", "Migration", (m.hybrid || 0) > 0, int(m.hybrid) + " / " + int(m.eligible) + " hybrid, " + int(m.legacy) + " legacy skipped, " +
      int(m.rolled_back) + " rolled back, " + int(m.pending) + " not attempted · server time " + sec(m.server_s) +
      " · key check median " + ms(m.key_check_median_ms) + " · sessions disturbed " + int(m.sessions_disturbed) + " / " + int(m.sessions_running) +
      ((m.runs || 0) > 1 ? ' <span class="sub">(' + int(m.runs) + " migration runs: every Start enrols every charger again, L46)</span>" : ""));
    add("V3", "Attack charger", attacks > 0, int(a.tls_refused) + " refused at TLS (forged/old/missing certificate) · " +
      int(a.identity_rejected) + " wrong identity refused · " + int(a.cut_off) + " stolen-certificate connections cut off (1008)");
    add("V4", "Reconnect storm", !!st, st ? "95 % recovered in " + sec(st.t95_s) + " (" + int(st.recovered) + " / " + int(st.population) + ")" +
      (b ? " · boot key checks " + int(b.passed) + " / " + int(b.total) + ", median " + ms(b.median_ms) : "") : "not seen in this run");
    add("V5", "Key rotation", !!rot, rot ? int(rot.rotated) + " rotated, " + int(rot.failed) + " failed (old key kept), check median " + ms(rot.key_check_median_ms) : "not seen in this run");
    add("V6", "Halt", !!(h && h.shown), !h ? "no halt fleet in this run" : h.not_started ? "halt fleet connected, but no migration was started on it"
      : (h.shown ? "halted at wave " + esc(h.wave) + ": " : "no halt: ") + int(h.hybrid) + " hybrid / " + int(h.rolled_back) + " rolled back / " +
        int(h.never_attempted || h.pending) + " never attempted");
    add("V7", "Curtailment", a.curtailment === "done", a.curtailment === "done" ? "power limit sent and cleared" : "not seen in this run");

    var html = '<section id="headlines"><h2>Run ' + esc(v.name) + "</h2><p class=\"lede\">One complete demo run, " +
      "started " + esc((r.started_at || "").replace("T", " ").slice(0, 19)) + " UTC. Main and halt servers are analysed " +
      "separately, each against its own fleet. Trust: " + badge(r.trust) + "</p>" +
      '<div class="tiles">' +
      tile("Chargers", int(r.chargers), "connected in this run") +
      tile("Post-quantum", int(m.hybrid) + " / " + int(m.eligible), "hybrid at the end of V2") +
      tile("Key check median", ms(m.key_check_median_ms), "ML-DSA-44, during the migration") +
      tile("Storm, 95 % back", st ? sec(st.t95_s) : "—", "after the server restart (V4)") +
      tile("Attacks refused", int(attacks), "V3: TLS, identity, stolen certificate") +
      "</div>" +
      '<div class="card"><h3>The variants</h3>' +
      '<div class="tablewrap"><table class="variants"><tbody>' + rows.join("") + "</tbody></table></div>" +
      '<details class="issues" style="margin-top:8px"><summary>Files analysed (' + int((v.files || []).length) + "), read from " +
      esc(v.source) + "</summary><ul>" + (v.files || []).map(function (f) { return "<li><code>" + esc(f) + "</code></li>"; }).join("") +
      "</ul><p class=\"note\">Saved in <code>" + esc(v.folder) + "</code>. Analysing this run again replaces this result; nothing is added.</p></details>" +
      "</div></section>";
    root.insertAdjacentHTML("beforeend", html);
  }

  // ------------------------------------------------- every demo run together

  var TRIGGER_LABEL = { migration: "Migration", boot: "Boot (after reconnect)", rotation: "Rotation" };

  function triggerColor(p, t) {
    return { migration: p.mode.hybrid, boot: p.alt, rotation: p.mode.pqc }[t] || p.muted;
  }

  function renderCombined(root, c) {
    if (!c || !(c.runs || []).length) {
      root.insertAdjacentHTML("beforeend", empty("No demo run analysed yet: <code>python -m analysis.run --run demo3</code>."));
      return;
    }
    var t = c.totals || {}, pool = c.pooled || {}, kc = pool.key_check_ms || {};
    var html = '<section id="headlines"><h2>All runs combined</h2><p class="lede">Every analysed demo run side by side ' +
      "(oldest first), then every run's numbers pooled together. Each run is still analysed on its own; nothing here " +
      "is re-measured.</p>" +
      '<div class="tiles">' +
      tile("Runs", int(t.runs), (c.runs || []).map(function (r) { return r.name; }).join(", ")) +
      tile("Chargers migrated", int(t.chargers_migrated), "summed over the runs") +
      tile("Keys rotated", int(t.keys_rotated), "summed over the runs") +
      tile("Key checks (pooled)", int((kc.migration || {}).n), "migration, median " + ms((kc.migration || {}).median)) +
      tile("Connect (pooled)", ms((pool.connect_ms || {}).median), int((pool.connect_ms || {}).n) + " chargers, first connection") +
      tile("Attacks refused", int((t.tls_refused || 0) + (t.identity_rejected || 0) + (t.cut_off || 0)),
        int(t.tls_refused) + " TLS · " + int(t.identity_rejected) + " identity · " + int(t.cut_off) + " cut off") +
      "</div>";
    html += '<div class="card"><h3>Side by side</h3>' + table(
      [{ t: "Run" }, { t: "Started (UTC)" }, { t: "Chargers", num: true }, { t: "Connect median", num: true },
       { t: "Hybrid", num: true }, { t: "Server time", num: true }, { t: "Key check median", num: true },
       { t: "Storm 95 %", num: true }, { t: "Rotated", num: true }, { t: "Halt" }, { t: "TLS refused", num: true },
       { t: "Identity refused", num: true }, { t: "Cut off", num: true }, { t: "Disturbed", num: true }, { t: "Trust" }],
      c.runs.map(function (r) {
        var m = r.migration || {}, a = r.attacks || {}, h = r.halt;
        return ['<a href="#view=' + encodeURIComponent(r.name) + '" data-view="' + esc(r.name) + '">' + esc(r.name) + "</a>",
          esc((r.started_at || "").replace("T", " ").slice(0, 16)), int(r.chargers), ms(r.connect_median_ms),
          int(m.hybrid) + " / " + int(m.eligible), sec(m.server_s), ms(m.key_check_median_ms),
          r.storm ? sec(r.storm.t95_s) : "—", r.rotation ? int(r.rotation.rotated) : "—",
          !h ? "—" : h.not_started ? "not started" : h.shown ? "wave " + esc(h.wave) + " ↩" : "no halt",
          int(a.tls_refused), int(a.identity_rejected), int(a.cut_off), int(m.sessions_disturbed), badge(r.trust)];
      })) + "</div>";
    html += '<div class="grid two" style="margin-top:16px">' +
      '<div class="card"><h3>Key-check time per run</h3><p class="sub">Median ML-DSA-44 round trip, by kind of check</p><div class="chart" id="cCmbRuns"></div></div>' +
      '<div class="card"><h3>Every key check, pooled</h3><p class="sub">All runs together, ' + esc(fmt(pool.hist_bin_ms, 0)) +
      ' ms bins (the last bin holds the slowest 1 %)</p><div class="chart" id="cCmbHist"></div></div>' +
      "</div>" +
      '<div class="card" style="margin-top:16px"><h3>Pooled numbers</h3>' + table(
        [{ t: "What" }, { t: "n", num: true }, { t: "Median", num: true }, { t: "95 % CI of the median", num: true },
         { t: "p95", num: true }, { t: "Max", num: true }],
        [["Charger connect (first connection)", pool.connect_ms]].concat(
          ["migration", "boot", "rotation"].map(function (k) { return ["Key check · " + TRIGGER_LABEL[k], kc[k]]; })
        ).map(function (row) {
          var d = row[1] || {};
          return [esc(row[0]), int(d.n), ms(d.median),
            d.median_ci95 ? ms(d.median_ci95[0]) + " – " + ms(d.median_ci95[1]) : "—", ms(d.p95), ms(d.max)];
        })) + '<p class="note">A run that did not do a step (e.g. no storm) simply adds nothing to that row.</p></div>' +
      '<div class="card" style="margin-top:16px"><h3>Charger connect time, pooled</h3><p class="sub">Share of chargers ' +
      "connected within a given time (first connection, every run)</p><div class=\"chart\" id=\"cCmbConnect\"></div></div>";
    root.insertAdjacentHTML("beforeend", html + "</section>");
    Array.prototype.forEach.call(root.querySelectorAll("a[data-view]"), function (a) {
      a.addEventListener("click", function (e) { e.preventDefault(); pick(a.getAttribute("data-view")); });
    });

    var names = (c.per_run_key_checks || []).map(function (r) { return r.name; });
    mount(document.getElementById("cCmbRuns"), function (p) {
      var o = base(p, "combined_key_checks_per_run");
      o.tooltip.trigger = "axis";
      o.tooltip.valueFormatter = function (v) { return ms(v); };
      o.xAxis = catAxis(p, names);
      o.yAxis = valueAxis(p, "ms");
      o.series = ["migration", "boot", "rotation"].map(function (k) {
        return { type: "bar", name: TRIGGER_LABEL[k], barMaxWidth: 28, itemStyle: { color: triggerColor(p, k), borderRadius: [3, 3, 0, 0] },
                 data: (c.per_run_key_checks || []).map(function (r) { return (r[k] || {}).n ? r[k].median : null; }) };
      });
      return o;
    });
    mount(document.getElementById("cCmbHist"), function (p) {
      var o = base(p, "combined_key_check_histogram");
      var hist = pool.key_check_hist || {}, bins = {};
      ["migration", "boot", "rotation"].forEach(function (k) { (hist[k] || []).forEach(function (b) { bins[b[0]] = true; }); });
      var xs = Object.keys(bins).map(Number).sort(function (x, y) { return x - y; });
      o.tooltip.trigger = "axis";
      o.xAxis = catAxis(p, xs.map(function (x) { return fmt(x, 0); }), { name: "ms", nameLocation: "middle", nameGap: 28 });
      o.yAxis = valueAxis(p, "Key checks", { minInterval: 1 });
      o.series = ["migration", "boot", "rotation"].map(function (k) {
        var m = {};
        (hist[k] || []).forEach(function (b) { m[b[0]] = b[1]; });
        return { type: "bar", name: TRIGGER_LABEL[k], stack: "all", itemStyle: { color: triggerColor(p, k) },
                 data: xs.map(function (x) { return m[x] || 0; }) };
      });
      return o;
    });
    mount(document.getElementById("cCmbConnect"), function (p) {
      var o = base(p, "combined_connect_ecdf");
      o.legend.show = false;
      o.tooltip.trigger = "axis";
      o.xAxis = valueAxis(p, "Connect time (ms)", { splitLine: { show: false } });
      o.yAxis = valueAxis(p, "% of chargers", { max: 100 });
      o.series = [line("Connected within", p.alt, (pool.connect_ms || {}).ecdf || [], { step: "end" })];
      return o;
    });
  }

  function load() {
    if (window.PQCHARGE_RESULTS) return Promise.resolve(window.PQCHARGE_RESULTS);
    return fetch("results.json", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("results.json: HTTP " + r.status);
      return r.json();
    });
  }

  function fail(err) {
    document.getElementById("root").innerHTML = empty(
      "Could not load results (" + esc(err && err.message ? err.message : err) + "). " +
      "Run <code>python -m analysis.run</code> first, then open <code>analysis/output/report.html</code>.");
    document.getElementById("generated").textContent = "";
  }

  // When served over HTTP (the dashboard), pick up a newly analysed run by itself.
  function watch() {
    if (location.protocol === "file:") return;
    setInterval(function () {
      fetch("results.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && BUNDLE && d.generated_at !== BUNDLE.generated_at) render(d); })
        .catch(function () { /* the next poll will try again */ });
    }, 10000);
  }

  // Theme: follow the system; the button flips and remembers (per viewer only).
  function setupTheme() {
    var btn = document.getElementById("themeBtn");
    if (btn) btn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme") ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("pqcharge-theme", next); } catch (e) { /* optional */ }
      redrawAll();
    });
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () { if (!document.documentElement.getAttribute("data-theme")) redrawAll(); };
      if (mq.addEventListener) mq.addEventListener("change", onChange); else if (mq.addListener) mq.addListener(onChange);
    }
  }

  window.PQChargeReport = { render: render, load: load };

  document.addEventListener("DOMContentLoaded", function () {
    setupTheme();
    if (typeof echarts === "undefined") {
      fail("vendor/echarts.min.js is missing");
      return;
    }
    load().then(function (d) { render(d); watch(); }).catch(fail);
    window.addEventListener("hashchange", function () {
      var v = viewFromHash();
      if (BUNDLE && v && v !== VIEW && (BUNDLE.view_order || []).indexOf(v) >= 0) { VIEW = v; selectedSlot = null; e1Exp = null; show(); }
    });
  });
})();
