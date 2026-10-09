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
 * Colour rules (one meaning per colour, everywhere on the page):
 *   security mode  classical = blue, hybrid = orange, post-quantum = aqua
 *   trust / state  green = good, yellow = warning, red = critical, grey = pending
 *                  -- always with an icon or a text label, never colour alone
 */
(function () {
  "use strict";

  var MODE_LABEL = { classical: "Classical", hybrid: "Hybrid", pqc: "Post-quantum" };
  var MODE_SHORT = { classical: "Classical", hybrid: "Hybrid", pqc: "PQC" };
  var STATE_LABEL = {
    pending: "Pending", in_progress: "Upgrading", migrated: "Upgraded",
    rolled_back: "Rolled back", incompatible: "Incompatible"
  };
  var RENDERER = /[?&]png\b/.test(location.search) ? "canvas" : "svg";
  var charts = [];
  var DATA = null;
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
          esc(s.experiment), int(s.n_stations), modeCell(s.crypto_mode), s.tls ? "On" : "Off",
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
      render(DATA);
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
      "</div>";

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
  }

  // ---------------------------------------------------------------- E1

  function renderE1(root) {
    var all = slots().filter(function (s) { return s.e1 && s.e1.station_connect_ms && s.e1.station_connect_ms.n; });
    var exps = [];
    all.forEach(function (s) { if (exps.indexOf(s.experiment) < 0) exps.push(s.experiment); });
    if (!e1Exp || (e1Exp !== "all" && exps.indexOf(e1Exp) < 0)) e1Exp = exps.indexOf("e1") >= 0 ? "e1" : "all";
    var list = e1Exp === "all" ? all : all.filter(function (s) { return s.experiment === e1Exp; });
    var html = '<section id="e1"><h2>E1 · Handshake cost</h2><p class="lede">How long a charger takes ' +
      "to connect securely — network connection, TLS encryption, certificate exchange and protocol " +
      "upgrade — measured by the charger itself from dial to ready. The box shows where the middle " +
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
      render(DATA);
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
        "triggered from the dashboard or <code>POST /api/migration/start</code>.") + "</section>");
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
      '<div class="tiles">' +
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

  function renderE4(root) {
    var e = DATA.e4 || { rows: [], notes: [] };
    var html = '<section id="e4"><h2>E4 · Size limits</h2><p class="lede">Post-quantum keys and signatures ' +
      "are far larger than classical ones. OCPP caps a certificate at 5,500 characters and a certificate " +
      "chain at 10,000. Every size below is measured from what our own code produces. Under Option B no " +
      "post-quantum certificate exists, so the limits apply to the classical certificates; the post-quantum " +
      "artifacts travel inside DataTransfer messages (shown for scale).</p>";
    if (!e.rows.length) {
      root.insertAdjacentHTML("beforeend", html + empty("Sizes could not be measured: " + esc((e.notes || []).join(" "))) + "</section>");
      return;
    }
    var rows = e.rows.slice().reverse();
    var groupColor = function (p, g) { return g === "classical" ? p.mode.classical : g === "post-quantum" ? p.mode.pqc : p.muted; };
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
      o.grid.left = 190;
      o.grid.right = 40;
      o.yAxis = catAxis(p, rows.map(function (r) { return (r.group === "on the wire" ? "wire · " : r.group === "classical" ? "classical · " : "PQ · ") + r.label; }));
      o.xAxis = valueAxis(p, "Bytes", { max: 11000 });
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

  function renderE5(root) {
    var list = slots();
    var html = '<section id="e5"><h2>E5 · Security</h2><p class="lede">Were impostors kept out? A charger ' +
      "whose certificate names a different charger is rejected at connection time. In the attack demonstration " +
      "a forged command on a classical fleet shows up as a spike in fleet power; on a migrated fleet the fake " +
      "identity fails the post-quantum check and nothing moves.</p>";
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
      tile("Connection failures", int(failures.reduce(function (a, k) { return a + e.connection_failures[k]; }, 0)), failures.join(", ") || "none") +
      tile("Protocol errors", int(e.callerrors), "CALLErrors reported by chargers") +
      tile("Commands received", int(e.commands_received), "e.g. power limits, remote stop") +
      "</div>" +
      '<div class="card"><h3>Fleet power, with rejected identities marked</h3><p class="sub">A forged "full power" command appears as a step up across the fleet</p><div class="chart" id="cE5"></div></div>';
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

  function render(data) {
    DATA = data;
    var list = slots();
    if (!selectedSlot || !DATA.slots[selectedSlot]) {
      // Default to the most recently started run.
      selectedSlot = null;
      list.forEach(function (s) {
        if (!selectedSlot || (s.started_at_epoch || 0) > (DATA.slots[selectedSlot].started_at_epoch || 0)) selectedSlot = s.key;
      });
    }

    disposeAll();
    var root = document.getElementById("root");
    root.innerHTML = "";
    renderRuns(root);
    renderE1(root);
    renderE2(root);
    renderE3(root);
    renderE4(root);
    renderE5(root);
    renderNodes(root);

    document.getElementById("generated").textContent =
      "Analysed " + (data.generated_at || "").replace("T", " ").slice(0, 19) + " UTC";
    var src = data.sources || {};
    document.getElementById("foot").innerHTML =
      "Computed by <code>python -m analysis.run</code> from " + esc(src.server_diary) + " (" +
      int(src.server_diary_lines) + " lines) and " + int((src.tester_diaries || []).length) +
      " tester diar" + (((src.tester_diaries || []).length === 1) ? "y" : "ies") + ". " +
      int(src.runs_found) + " run(s) found, newest per setup kept (" + int(src.runs_kept) + "). " +
      "Results format v" + esc(data.format_version) + ". Charts: Apache ECharts.";
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
        .then(function (d) { if (d && DATA && d.generated_at !== DATA.generated_at) render(d); })
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
  });
})();
