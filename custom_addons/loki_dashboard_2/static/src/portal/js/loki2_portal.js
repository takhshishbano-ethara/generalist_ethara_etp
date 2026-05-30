/* Loki Clinical Dashboard — portal JS. Vanilla IIFE, no framework. */
(function () {
    "use strict";

    /* ---------- state ---------- */
    var STATE = {
        data: null,
        currentPid: null,
        compareMode: false,
        charts: {},
        wsiViewer: null,
    };

    var CAT_VAR = {
        chemo: "--cat-chemo",
        immuno: "--cat-immuno",
        targeted: "--cat-targeted",
        radiation: "--cat-radiation",
        surgery: "--cat-surgery",
        imaging: "--cat-imaging",
        pathology: "--cat-pathology",
        molecular: "--cat-molecular",
        biomarker: "--cat-biomarker",
        stage_change: "--cat-other",
        consultation: "--cat-other",
        other: "--cat-other",
    };

    var CAT_ORDER = [
        "surgery",
        "chemo",
        "immuno",
        "targeted",
        "radiation",
        "imaging",
        "pathology",
        "molecular",
        "biomarker",
        "other",
    ];

    /* ---------- helpers ---------- */
    function esc(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // Strip ingest artefacts so cells like "[null]", "null", "None", "nan" render as empty.
    function cleanText(s) {
        if (s === null || s === undefined) return "";
        var t = String(s).trim();
        if (!t) return "";
        var stripped = t.replace(/^\[+|\]+$/g, "").trim();
        if (/^(null|none|nan|n\/a|-)$/i.test(stripped)) return "";
        return t;
    }

    function parseDate(s) {
        if (!s) return null;
        var d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
    }

    function fmtDate(s) {
        var d = parseDate(s);
        if (!d) return esc(s || "—");
        var m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
        return d.getDate() + " " + m[d.getMonth()] + " " + d.getFullYear();
    }

    function daysBetween(a, b) {
        var da = parseDate(a);
        var db = parseDate(b);
        if (!da || !db) return null;
        return Math.round((db - da) / 86400000);
    }

    function catColor(cat) {
        var v = CAT_VAR[cat] || CAT_VAR.other;
        return "var(" + v + ")";
    }

    function destroyCharts(prefix) {
        Object.keys(STATE.charts).forEach(function (k) {
            if (!prefix || k.indexOf(prefix) === 0) {
                try { STATE.charts[k].destroy(); } catch (e) { /* noop */ }
                delete STATE.charts[k];
            }
        });
    }

    function setHTML(id, html) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = html;
    }

    function getPatient(pid) {
        if (!STATE.data || !STATE.data.patients) return null;
        for (var i = 0; i < STATE.data.patients.length; i++) {
            var p = STATE.data.patients[i];
            if (String(p.id) === String(pid) || p.code === pid) return p;
        }
        return null;
    }

    /* ---------- theme toggle (copied pattern from loki_dashboard) ---------- */
    var THEME_KEY = "loki:theme";
    function currentTheme() {
        return document.documentElement.getAttribute("data-theme") || "dark";
    }
    function syncToggleBtn(btn) {
        if (!btn) return;
        var t = currentTheme();
        btn.setAttribute("aria-pressed", t === "light" ? "true" : "false");
        btn.setAttribute("aria-label", t === "light" ? "Switch to dark mode" : "Switch to light mode");
    }
    function initThemeToggle() {
        var btn = document.querySelector(".theme-toggle");
        syncToggleBtn(btn);
        if (!btn) return;
        btn.addEventListener("click", function () {
            var next = currentTheme() === "light" ? "dark" : "light";
            document.documentElement.setAttribute("data-theme", next);
            try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* noop */ }
            syncToggleBtn(btn);
        });
        if (window.matchMedia) {
            var mq = window.matchMedia("(prefers-color-scheme: dark)");
            if (mq.addEventListener) {
                mq.addEventListener("change", function () { syncToggleBtn(btn); });
            }
        }
    }

    /* ---------- doc modal + lightbox ---------- */
    function initDocModal() {
        var modal = document.getElementById("doc-modal");
        var iframe = document.getElementById("doc-iframe");
        var close = document.getElementById("doc-close");
        if (!modal || !iframe || !close) return;
        function closeModal() {
            modal.classList.remove("active");
            iframe.src = "about:blank";
            document.body.style.overflow = "";
        }
        close.addEventListener("click", closeModal);
        modal.addEventListener("click", function (e) {
            if (e.target === modal || e.target.classList.contains("doc-modal-backdrop")) closeModal();
        });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && modal.classList.contains("active")) closeModal();
        });
    }
    function openDoc(url, title) {
        var modal = document.getElementById("doc-modal");
        var iframe = document.getElementById("doc-iframe");
        var caption = document.querySelector("#doc-modal .doc-modal-caption");
        if (!modal || !iframe) return;
        if (caption) caption.textContent = title || url;
        iframe.src = url;
        modal.classList.add("active");
        document.body.style.overflow = "hidden";
    }

    /* ---------- tooltip helper ---------- */
    function showTooltip(x, y, title, body) {
        var tt = document.getElementById("tt-tooltip");
        if (!tt) return;
        tt.innerHTML =
            '<div class="tt-title">' + esc(title) + "</div>" +
            (body ? '<div class="tt-body">' + esc(body) + "</div>" : "");
        tt.style.left = x + 14 + "px";
        tt.style.top = y + 14 + "px";
        tt.classList.add("is-visible");
    }
    function hideTooltip() {
        var tt = document.getElementById("tt-tooltip");
        if (tt) tt.classList.remove("is-visible");
    }

    /* ---------- 1. HEADER ---------- */
    function renderHeader(p) {
        var stage = p.current_stage || {};
        var perf = p.latest_performance || {};
        var dem = p.demographics || {};
        var cp = p.cancer_profile || {};

        var stagePillCls = stage.stage === "IV" ? "pill pill-stage-iv" : "pill";
        var statusPillCls =
            "pill pill-" + (p.status === "alive" ? "alive" : p.status === "deceased" ? "deceased" : "unknown");

        var recistCls = perf.recist ? "recist-badge recist-" + esc(perf.recist) : "recist-badge";

        var kpiHtml =
            '<div class="kpi-row">' +
                '<div class="kpi-card kpi-card--accent">' +
                    '<div class="kpi-label">Patient</div>' +
                    '<div class="kpi-value"><span class="kpi-big">' + esc(p.code) + "</span></div>" +
                "</div>" +
                '<div class="kpi-card">' +
                    '<div class="kpi-label">Age · Sex</div>' +
                    '<div class="kpi-value"><span class="kpi-big">' + esc(dem.age_years || "—") +
                        '</span><span class="kpi-suffix"> ' + esc(dem.gender || "") + "</span></div>" +
                "</div>" +
                '<div class="kpi-card">' +
                    '<div class="kpi-label">Stage</div>' +
                    '<div class="kpi-value"><span class="' + stagePillCls + '">' +
                        esc(stage.stage || "—") + "</span></div>" +
                    '<div class="kpi-sub">' + esc(stage.t || "") + " · " + esc(stage.n || "") +
                        " · " + esc(stage.m || "") + "</div>" +
                "</div>" +
                '<div class="kpi-card">' +
                    '<div class="kpi-label">Latest RECIST</div>' +
                    '<div class="kpi-value"><span class="' + recistCls + '">' +
                        esc(perf.recist || "—") + "</span></div>" +
                    '<div class="kpi-sub">' + esc(perf.label || "") + "</div>" +
                "</div>" +
            "</div>";

        setHTML("kpi-row", "");
        var kpiContainer = document.getElementById("kpi-row");
        if (kpiContainer) kpiContainer.outerHTML = kpiHtml;

        var prose =
            '<h2 class="thesis-sub">' + esc(cp.site || "—") + "</h2>" +
            '<p class="byline">' + esc(cp.morphology || "—") +
                " · ICD-O-3 " + esc(cp.icd_o_3_topo || "—") + " / " + esc(cp.icd_o_3_morph || "—") + "</p>" +
            (cp.symptoms && cp.symptoms.length
                ? '<div class="prose-block"><div class="prose-label">Symptoms</div><div>' +
                    cp.symptoms.map(esc).join(", ") + "</div></div>"
                : "") +
            (cp.risk_factors && cp.risk_factors.length
                ? '<div class="prose-block"><div class="prose-label">Risk factors</div><div>' +
                    cp.risk_factors.map(esc).join(", ") + "</div></div>"
                : "") +
            (cp.comorbidities && cp.comorbidities.length
                ? '<div class="prose-block"><div class="prose-label">Comorbidities</div><div>' +
                    cp.comorbidities.map(esc).join(", ") + "</div></div>"
                : "");
        setHTML("header-prose", prose);

        var dx = p.days_since_diagnosis;
        var glance =
            '<div class="glance-label">At a glance</div>' +
            '<dl class="glance-list">' +
                glanceRow("Status", '<span class="' + statusPillCls + '">' + esc(p.status || "unknown") + "</span>") +
                glanceRow("Since diagnosis", dx != null ? esc(dx) + " days" : "—") +
                glanceRow("Religion", esc(dem.religion || "—")) +
                glanceRow("Diet", esc(dem.diet || "—")) +
                glanceRow("Marital", esc(dem.marital_status || "—")) +
                glanceRow("Height · Weight",
                    (dem.height_cm ? esc(dem.height_cm) + " cm" : "—") + " · " +
                    (dem.weight_kg ? esc(dem.weight_kg) + " kg" : "—")) +
                glanceRow("BMI", esc(dem.bmi || "—")) +
                glanceRow("Occupation", esc(dem.occupation || "—")) +
            "</dl>";
        setHTML("header-glance", glance);
    }

    function glanceRow(k, v) {
        return '<div class="glance-row"><dt>' + esc(k) + "</dt><dd>" + v + "</dd></div>";
    }

    /* ---------- 2. TIMELINE ---------- */
    function renderTimeline(p) {
        var svg = document.getElementById("timeline-svg");
        if (!svg) return;
        while (svg.firstChild) svg.removeChild(svg.firstChild);

        var SVG_NS = "http://www.w3.org/2000/svg";
        var events = (p.events || []).slice();
        var treatments = [];
        ["chemotherapy", "immunotherapy", "targeted", "radiation", "surgery"].forEach(function (k) {
            (p.treatments && p.treatments[k] ? p.treatments[k] : []).forEach(function (t) {
                treatments.push({
                    type: k === "chemotherapy" ? "chemo" : k === "immunotherapy" ? "immuno" : k,
                    label: t.drug || t.procedure || t.type || "—",
                    start: t.start || t.date,
                    end: t.end || t.date,
                    detail: t.comments || t.findings || "",
                });
            });
        });

        var allDates = [];
        events.forEach(function (e) { if (e.date) allDates.push(parseDate(e.date)); });
        treatments.forEach(function (t) {
            if (t.start) allDates.push(parseDate(t.start));
            if (t.end) allDates.push(parseDate(t.end));
        });
        allDates = allDates.filter(Boolean);
        if (!allDates.length) {
            setHTML("timeline-legend", "");
            svg.outerHTML = '<div class="empty-inline">No timeline events yet.</div>';
            return;
        }
        var minD = new Date(Math.min.apply(null, allDates));
        var maxD = new Date(Math.max.apply(null, allDates));
        minD.setMonth(minD.getMonth() - 2);
        maxD.setMonth(maxD.getMonth() + 2);

        var bandH = 32;
        var topPad = 36;
        var bottomPad = 28;
        var W = Math.max(1200, Math.ceil((maxD - minD) / 86400000 * 1.4));
        var H = topPad + bandH * CAT_ORDER.length + bottomPad;

        svg.setAttribute("viewBox", "0 0 " + W + " " + H);
        svg.setAttribute("width", W);
        svg.setAttribute("height", H);

        function x(d) {
            var dd = parseDate(d);
            if (!dd) return 0;
            return ((dd - minD) / (maxD - minD)) * (W - 80) + 60;
        }
        function bandY(cat) {
            var key = CAT_ORDER.indexOf(cat) >= 0 ? cat : "other";
            return topPad + CAT_ORDER.indexOf(key) * bandH + bandH / 2;
        }

        CAT_ORDER.forEach(function (cat, i) {
            var y = topPad + i * bandH;
            var divider = document.createElementNS(SVG_NS, "line");
            divider.setAttribute("x1", 0);
            divider.setAttribute("x2", W);
            divider.setAttribute("y1", y);
            divider.setAttribute("y2", y);
            divider.setAttribute("class", "timeline-band-divider");
            svg.appendChild(divider);
            var label = document.createElementNS(SVG_NS, "text");
            label.setAttribute("x", 8);
            label.setAttribute("y", y + bandH / 2 + 4);
            label.setAttribute("class", "timeline-band-label");
            label.textContent = cat;
            svg.appendChild(label);
        });

        var yearStart = new Date(minD.getFullYear(), 0, 1);
        for (var yr = yearStart.getFullYear(); yr <= maxD.getFullYear(); yr++) {
            var d = new Date(yr, 0, 1);
            if (d < minD) continue;
            var gx = x(d);
            var grid = document.createElementNS(SVG_NS, "line");
            grid.setAttribute("x1", gx); grid.setAttribute("x2", gx);
            grid.setAttribute("y1", topPad); grid.setAttribute("y2", H - bottomPad);
            grid.setAttribute("class", "timeline-grid");
            svg.appendChild(grid);
            var tick = document.createElementNS(SVG_NS, "text");
            tick.setAttribute("x", gx);
            tick.setAttribute("y", H - bottomPad + 16);
            tick.setAttribute("class", "timeline-axis-tick");
            tick.setAttribute("text-anchor", "middle");
            tick.textContent = String(yr);
            svg.appendChild(tick);
        }

        treatments.forEach(function (t) {
            if (!t.start) return;
            var xs = x(t.start);
            var xe = t.end ? x(t.end) : xs + 8;
            var y = bandY(t.type);
            var rect = document.createElementNS(SVG_NS, "rect");
            rect.setAttribute("x", xs);
            rect.setAttribute("y", y - 10);
            rect.setAttribute("width", Math.max(4, xe - xs));
            rect.setAttribute("height", 20);
            rect.setAttribute("class", "timeline-bar");
            rect.setAttribute("fill", catColor(t.type));
            rect.addEventListener("mousemove", function (ev) {
                showTooltip(ev.clientX, ev.clientY,
                    t.label,
                    fmtDate(t.start) + (t.end ? " → " + fmtDate(t.end) : "") +
                    (t.detail ? " · " + t.detail : ""));
            });
            rect.addEventListener("mouseleave", hideTooltip);
            svg.appendChild(rect);
        });

        events.forEach(function (e) {
            if (!e.date) return;
            var cx = x(e.date);
            var cy = bandY(e.category);
            var c = document.createElementNS(SVG_NS, "circle");
            c.setAttribute("cx", cx);
            c.setAttribute("cy", cy);
            c.setAttribute("r", 6);
            c.setAttribute("class", "timeline-event-dot");
            c.setAttribute("fill", catColor(e.category));
            c.addEventListener("mousemove", function (ev) {
                showTooltip(ev.clientX, ev.clientY,
                    (e.title || "(event)") + " · " + fmtDate(e.date),
                    e.detail || "");
            });
            c.addEventListener("mouseleave", hideTooltip);
            svg.appendChild(c);
        });

        var legendHtml = CAT_ORDER.map(function (cat) {
            return '<span class="legend-item"><span class="legend-swatch" style="background:' +
                catColor(cat) + '"></span>' + esc(cat) + "</span>";
        }).join("");
        setHTML("timeline-legend", legendHtml);
    }

    /* ---------- 3. BIOMARKERS ---------- */
    function renderBiomarkers(p) {
        destroyCharts("bio:");
        var grid = document.getElementById("biomarkers-grid");
        if (!grid) return;
        var rows = (p.biomarkers || []).slice();
        if (!rows.length) {
            grid.innerHTML = '<div class="empty-inline">No biomarker data.</div>';
            return;
        }
        var byTest = {};
        rows.forEach(function (r) {
            var t = r.test || "Unknown";
            (byTest[t] = byTest[t] || []).push(r);
        });
        grid.innerHTML = "";
        Object.keys(byTest).forEach(function (test, idx) {
            var wrap = document.createElement("div");
            wrap.className = "chart-wrap";
            var unit = byTest[test].length ? byTest[test][0].unit || "" : "";
            wrap.innerHTML =
                '<div class="chart-title">' + esc(test) + "</div>" +
                '<div class="chart-meta">' + esc(unit) + " · " + byTest[test].length + " points</div>" +
                '<div class="chart-canvas-wrap"><canvas></canvas></div>';
            grid.appendChild(wrap);
            var canvas = wrap.querySelector("canvas");
            var data = byTest[test]
                .map(function (r) { return { d: parseDate(r.date), v: r.value }; })
                .filter(function (r) { return r.d && r.v != null; })
                .sort(function (a, b) { return a.d - b.d; });
            STATE.charts["bio:" + idx + ":" + test] = makeLineChart(canvas, data, test);
        });
    }

    /* ---------- 4. LABS ---------- */
    function renderLabs(p) {
        destroyCharts("lab:");
        var grid = document.getElementById("labs-grid");
        if (!grid) return;
        var rows = (p.labs || []).slice();
        if (!rows.length) {
            grid.innerHTML = '<div class="empty-inline">No lab data.</div>';
            return;
        }
        var byTest = {};
        rows.forEach(function (r) {
            var key = (r.panel || "") + " · " + (r.test || "");
            (byTest[key] = byTest[key] || []).push(r);
        });
        grid.innerHTML = "";
        Object.keys(byTest).forEach(function (key, idx) {
            var wrap = document.createElement("div");
            wrap.className = "chart-wrap";
            var unit = byTest[key].length ? byTest[key][0].unit || "" : "";
            wrap.innerHTML =
                '<div class="chart-title">' + esc(key) + "</div>" +
                '<div class="chart-meta">' + esc(unit) + " · " + byTest[key].length + " points</div>" +
                '<div class="chart-canvas-wrap"><canvas></canvas></div>';
            grid.appendChild(wrap);
            var canvas = wrap.querySelector("canvas");
            var data = byTest[key]
                .map(function (r) { return { d: parseDate(r.date), v: r.value }; })
                .filter(function (r) { return r.d && r.v != null; })
                .sort(function (a, b) { return a.d - b.d; });
            STATE.charts["lab:" + idx] = makeLineChart(canvas, data, key);
        });
    }

    function makeLineChart(canvas, data, label) {
        if (!window.Chart || !canvas) return null;
        var accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#CC00CC";
        var ink3 = getComputedStyle(document.documentElement).getPropertyValue("--ink-3").trim() || "#525A6E";
        var border = getComputedStyle(document.documentElement).getPropertyValue("--border").trim() || "#D5DAE2";
        return new window.Chart(canvas, {
            type: "line",
            data: {
                labels: data.map(function (r) { return r.d.toISOString().slice(0, 10); }),
                datasets: [{
                    label: label,
                    data: data.map(function (r) { return r.v; }),
                    borderColor: accent,
                    backgroundColor: accent + "22",
                    borderWidth: 2,
                    pointRadius: 3,
                    pointBackgroundColor: accent,
                    tension: 0.25,
                    fill: true,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { ticks: { color: ink3, font: { size: 10 } }, grid: { color: border } },
                    y: { ticks: { color: ink3, font: { size: 10 } }, grid: { color: border } },
                },
            },
        });
    }

    /* ---------- 5. TREATMENTS ---------- */
    function renderTreatments(p) {
        var tbody = document.getElementById("treatments-tbody");
        if (!tbody) return;
        var rows = [];
        var tr = p.treatments || {};
        (tr.chemotherapy || []).forEach(function (t) { rows.push(mkTrt("chemo", t.drug, t.start, t.end, t.cycles, t.comments)); });
        (tr.immunotherapy || []).forEach(function (t) { rows.push(mkTrt("immuno", t.drug, t.start, t.end, t.cycles, t.comments)); });
        (tr.targeted || []).forEach(function (t) { rows.push(mkTrt("targeted", t.drug, t.start, t.end, t.cycles, t.comments)); });
        (tr.radiation || []).forEach(function (t) { rows.push(mkTrt("radiation", t.type, t.date, t.date, t.fractions, (t.dose ? t.dose + " · " : "") + (t.comments || ""))); });
        (tr.surgery || []).forEach(function (t) { rows.push(mkTrt("surgery", t.procedure, t.date, t.date, "—", t.findings)); });
        rows.sort(function (a, b) {
            var da = parseDate(a.start), db = parseDate(b.start);
            if (!da) return 1;
            if (!db) return -1;
            return da - db;
        });
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-inline">No treatments recorded.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(function (r) {
            return "<tr>" +
                '<td><span class="cat-tag cat-' + esc(r.type) + '">' + esc(r.type.toUpperCase()) + "</span></td>" +
                "<td>" + esc(r.label || "—") + "</td>" +
                "<td>" + fmtDate(r.start) + "</td>" +
                "<td>" + (r.end ? fmtDate(r.end) : "—") + "</td>" +
                "<td>" + esc(r.cycles || "—") + "</td>" +
                "<td>" + esc(r.comments || "") + "</td>" +
            "</tr>";
        }).join("");
    }
    function mkTrt(type, label, start, end, cycles, comments) {
        return { type: type, label: label, start: start, end: end, cycles: cycles, comments: comments };
    }

    /* ---------- 6. IMAGING ---------- */
    function renderImaging(p) {
        var list = document.getElementById("imaging-list");
        if (!list) return;
        var rows = (p.imaging || []).slice().sort(function (a, b) {
            return (parseDate(b.date) || 0) - (parseDate(a.date) || 0);
        });
        if (!rows.length) {
            list.innerHTML = '<div class="empty-inline">No imaging studies.</div>';
            return;
        }
        list.innerHTML = rows.map(function (r) {
            var modality = cleanText(r.modality);
            var impression = cleanText(r.impression);
            var open = r.pdf
                ? '<button class="btn-open" data-url="' + esc(r.pdf) + '" data-title="' +
                    esc((modality || "") + " · " + fmtDate(r.date)) + '">Open PDF</button>'
                : "";
            return '<div class="imaging-row">' +
                '<div class="imaging-date">' + fmtDate(r.date) + "</div>" +
                '<div><span class="modality-badge">' + esc(modality || "—") + "</span></div>" +
                '<div class="imaging-impression">' + esc(impression || "—") + "</div>" +
                '<div class="imaging-action">' + open + "</div>" +
            "</div>";
        }).join("");
        list.querySelectorAll(".btn-open").forEach(function (btn) {
            btn.addEventListener("click", function () {
                openDoc(btn.getAttribute("data-url"), btn.getAttribute("data-title"));
            });
        });
    }

    /* ---------- 7. PATHOLOGY + MOLECULAR ---------- */
    function renderPathMol(p) {
        var pathBody = document.getElementById("pathology-tbody");
        if (pathBody) {
            var rows = (p.pathology || []).slice().sort(function (a, b) {
                return (parseDate(a.date) || 0) - (parseDate(b.date) || 0);
            });
            if (!rows.length) {
                pathBody.innerHTML = '<tr><td colspan="7" class="empty-inline">No pathology records.</td></tr>';
            } else {
                pathBody.innerHTML = rows.map(function (r) {
                    return "<tr>" +
                        "<td>" + fmtDate(r.date) + "</td>" +
                        "<td>" + esc(r.type || "—") + "</td>" +
                        "<td>" + esc(r.tissue || "—") + (r.location ? " · " + esc(r.location) : "") + "</td>" +
                        "<td>" + esc(r.pt || "—") + "</td>" +
                        "<td>" + esc(r.pn || "—") + "</td>" +
                        "<td>" + esc(r.pm || "—") + "</td>" +
                        "<td>" + esc(r.grade || "—") + "</td>" +
                    "</tr>";
                }).join("");
            }
        }
        var molEl = document.getElementById("molecular-content");
        if (!molEl) return;
        var mol = (p.molecular || []).slice();
        if (!mol.length) {
            molEl.innerHTML = '<div class="empty-inline">No molecular results.</div>';
            return;
        }
        var byCat = {};
        mol.forEach(function (m) {
            var c = m.category || "Other";
            (byCat[c] = byCat[c] || []).push(m);
        });
        molEl.innerHTML = Object.keys(byCat).map(function (cat) {
            return '<div class="mol-group">' +
                '<div class="mol-group-title">' + esc(cat) + "</div>" +
                '<table class="clinical-table">' +
                "<thead><tr><th>Date</th><th>Gene / Marker</th><th>Variation / Clone</th><th>Result</th><th>Comments</th></tr></thead>" +
                "<tbody>" + byCat[cat].map(function (m) {
                    var key = m.gene || m.marker || "—";
                    var detail = m.variation || m.clone || "";
                    return "<tr>" +
                        "<td>" + fmtDate(m.date) + "</td>" +
                        "<td><strong>" + esc(key) + "</strong></td>" +
                        "<td>" + esc(detail) + "</td>" +
                        "<td>" + esc(m.result || "—") + "</td>" +
                        "<td>" + esc(m.comments || "") + "</td>" +
                    "</tr>";
                }).join("") + "</tbody>" +
                "</table>" +
            "</div>";
        }).join("");
    }

    /* ---------- 8. WSI ---------- */
    function showWsiMessage(wrap, html) {
        wrap.classList.add("is-empty");
        wrap.innerHTML = '<div class="wsi-empty-text">' + html + "</div>";
    }

    function buildDziTileSource(dziUrl, xmlText) {
        var doc = new DOMParser().parseFromString(xmlText, "application/xml");
        if (doc.getElementsByTagName("parsererror").length) {
            throw new Error("Invalid DZI XML at " + dziUrl);
        }
        var image = doc.documentElement;
        var size = image.getElementsByTagName("Size")[0];
        if (!image || !size) throw new Error("DZI missing <Image>/<Size>: " + dziUrl);
        var basePath = dziUrl.replace(/\.dzi(?:\?.*)?$/i, "_files");
        return {
            Image: {
                xmlns: "http://schemas.microsoft.com/deepzoom/2008",
                Url: basePath + "/",
                Format: image.getAttribute("Format") || "jpeg",
                Overlap: image.getAttribute("Overlap") || "1",
                TileSize: image.getAttribute("TileSize") || "254",
                Size: {
                    Width: size.getAttribute("Width"),
                    Height: size.getAttribute("Height"),
                },
            },
        };
    }

    function renderWSI(p) {
        var tabsEl = document.getElementById("wsi-tabs");
        var wrap = document.getElementById("wsi-viewer-wrap");
        if (!tabsEl || !wrap) return;
        if (STATE.wsiViewer) {
            try { STATE.wsiViewer.destroy(); } catch (e) { /* noop */ }
            STATE.wsiViewer = null;
        }
        var slides = (p.wsi_slides || []).slice();
        wrap.classList.remove("is-empty");
        wrap.innerHTML = '<div id="wsi-viewer"></div>';
        if (!slides.length) {
            tabsEl.innerHTML = "";
            showWsiMessage(wrap, "No whole-slide images linked for this patient.");
            return;
        }
        tabsEl.innerHTML = slides.map(function (s, i) {
            return '<button class="wsi-tab' + (i === 0 ? " is-active" : "") +
                '" data-idx="' + i + '">' + esc(s.slide) + "</button>";
        }).join("");

        function load(idx) {
            var s = slides[idx];
            tabsEl.querySelectorAll(".wsi-tab").forEach(function (b, j) {
                b.classList.toggle("is-active", j === idx);
            });
            if (STATE.wsiViewer) {
                try { STATE.wsiViewer.destroy(); } catch (e) { /* noop */ }
                STATE.wsiViewer = null;
            }
            wrap.classList.remove("is-empty");
            wrap.innerHTML =
                '<div id="wsi-viewer"></div>' +
                '<div class="wsi-loader" id="wsi-loader">' +
                  '<div class="wsi-loader__spinner"></div>' +
                  '<div class="wsi-loader__label">Loading ' + esc(s.slide) + ' …</div>' +
                '</div>';
            var loaderEl = document.getElementById("wsi-loader");
            var hideLoader = function () {
                if (loaderEl && !loaderEl.hidden) loaderEl.hidden = true;
            };
            if (!window.OpenSeadragon) {
                hideLoader();
                showWsiMessage(wrap,
                    "OpenSeadragon library not bundled. See " +
                    "<code>static/src/portal/vendor/openseadragon/README.md</code>.");
                return;
            }
            fetch(s.dzi_path, { headers: { "Accept": "application/xml,text/xml,*/*" } })
                .then(function (res) {
                    if (!res.ok) {
                        throw new Error("HTTP " + res.status + " fetching " + s.dzi_path);
                    }
                    return res.text();
                })
                .then(function (xmlText) {
                    var tileSource = buildDziTileSource(s.dzi_path, xmlText);
                    STATE.wsiViewer = window.OpenSeadragon({
                        id: "wsi-viewer",
                        prefixUrl: "/loki_dashboard_2/static/src/portal/vendor/openseadragon/images/",
                        tileSources: tileSource,
                        showNavigationControl: true,
                        showFullPageControl: false,
                        immediateRender: true,
                        crossOriginPolicy: false,
                        loadTilesWithAjax: false,
                    });
                    STATE.wsiViewer.addOnceHandler("tile-drawn", hideLoader);
                    STATE.wsiViewer.addHandler("open-failed", function (ev) {
                        console.error("OpenSeadragon open-failed:", ev);
                        hideLoader();
                        showWsiMessage(wrap,
                            "Could not open <strong>" + esc(s.slide) + "</strong>: " +
                            esc((ev && ev.message) || "unknown error") + ".");
                    });
                    STATE.wsiViewer.addHandler("tile-load-failed", function (ev) {
                        console.warn("OpenSeadragon tile-load-failed:", ev && ev.message, ev && ev.tile && ev.tile.url);
                    });
                })
                .catch(function (err) {
                    console.error("WSI load failed for", s.slide, err);
                    showWsiMessage(wrap,
                        "Tiles unavailable for <strong>" + esc(s.slide) + "</strong> " +
                        "(" + esc(err && err.message ? err.message : String(err)) + "). " +
                        "Re-run <code>python tools/generate_dzi.py</code> after dropping " +
                        "the .svs into <code>data/Clinical_Data/Patient_" + esc(p.id) +
                        "/wsi/</code>.");
                });
        }
        tabsEl.querySelectorAll(".wsi-tab").forEach(function (btn) {
            btn.addEventListener("click", function () {
                load(parseInt(btn.getAttribute("data-idx"), 10));
            });
        });
        load(0);
    }

    /* ---------- 9. DOCUMENTS ---------- */
    function renderDocuments(p) {
        var box = document.getElementById("doc-library");
        if (!box) return;
        var docs = (p.documents || []).slice();
        if (!docs.length) {
            box.innerHTML = '<div class="empty-inline">No documents linked.</div>';
            return;
        }
        var byCat = {};
        docs.forEach(function (d) {
            var c = d.category || "other";
            (byCat[c] = byCat[c] || []).push(d);
        });
        box.innerHTML = Object.keys(byCat).sort().map(function (cat) {
            return '<div class="doc-cat is-open">' +
                '<div class="doc-cat-header">' +
                    '<span class="doc-cat-name">' + esc(cat) + "</span>" +
                    '<span class="doc-cat-count">' + byCat[cat].length + "</span>" +
                "</div>" +
                '<div class="doc-cat-items">' + byCat[cat].map(function (d) {
                    return '<div class="doc-cat-item">' +
                        '<span class="doc-cat-icon">PDF</span>' +
                        '<span class="doc-cat-title">' + esc(d.title || d.filename) + "</span>" +
                        '<button class="doc-cat-link" data-url="' + esc(d.url) +
                            '" data-title="' + esc(d.title || d.filename) + '">Open</button>' +
                    "</div>";
                }).join("") + "</div>" +
            "</div>";
        }).join("");
        box.querySelectorAll(".doc-cat-header").forEach(function (h) {
            h.addEventListener("click", function () {
                h.parentElement.classList.toggle("is-open");
            });
        });
        box.querySelectorAll(".doc-cat-link").forEach(function (btn) {
            btn.addEventListener("click", function (e) {
                e.stopPropagation();
                openDoc(btn.getAttribute("data-url"), btn.getAttribute("data-title"));
            });
        });
    }

    /* ---------- 10. COMPARE ---------- */
    function renderCompare(allPatients) {
        var grid = document.getElementById("compare-grid");
        if (!grid) return;
        if (!allPatients || !allPatients.length) {
            grid.innerHTML = '<div class="empty-inline">No patient data.</div>';
            return;
        }
        destroyCharts("cmp:");
        grid.innerHTML = "";
        allPatients.forEach(function (p, idx) {
            var stage = p.current_stage || {};
            var perf = p.latest_performance || {};
            var dem = p.demographics || {};
            var card = document.createElement("div");
            card.className = "compare-card";
            card.innerHTML =
                "<h3>" + esc(p.code) + "</h3>" +
                '<div class="compare-sub">' + esc((p.cancer_profile || {}).site || "—") + "</div>" +
                '<div class="compare-kpi">' +
                    '<div class="kpi-mini"><span>Age</span><strong>' + esc(dem.age_years || "—") + "</strong></div>" +
                    '<div class="kpi-mini"><span>Stage</span><strong>' + esc(stage.stage || "—") + "</strong></div>" +
                    '<div class="kpi-mini"><span>Status</span><strong>' + esc(p.status || "—") + "</strong></div>" +
                    '<div class="kpi-mini"><span>RECIST</span><strong>' + esc(perf.recist || "—") + "</strong></div>" +
                "</div>" +
                '<div class="compare-chart-wrap"><canvas></canvas></div>';
            grid.appendChild(card);
            var canvas = card.querySelector("canvas");
            var bio = (p.biomarkers || [])
                .filter(function (r) { return r.test === "CEA" || r.test === "CA 19-9"; });
            var series = {};
            bio.forEach(function (r) {
                (series[r.test] = series[r.test] || []).push({ d: parseDate(r.date), v: r.value });
            });
            Object.keys(series).forEach(function (k) {
                series[k] = series[k].filter(function (x) { return x.d && x.v != null; })
                    .sort(function (a, b) { return a.d - b.d; });
            });
            if (window.Chart && canvas && Object.keys(series).length) {
                var accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#CC00CC";
                var ink3 = getComputedStyle(document.documentElement).getPropertyValue("--ink-3").trim() || "#525A6E";
                var palette = [accent, "#2A5CB8", "#0E8F4B"];
                STATE.charts["cmp:" + idx] = new window.Chart(canvas, {
                    type: "line",
                    data: {
                        datasets: Object.keys(series).map(function (k, i) {
                            return {
                                label: k,
                                data: series[k].map(function (r) {
                                    return { x: r.d.toISOString().slice(0, 10), y: r.v };
                                }),
                                borderColor: palette[i % palette.length],
                                backgroundColor: palette[i % palette.length] + "22",
                                borderWidth: 2,
                                pointRadius: 2,
                                tension: 0.25,
                            };
                        }),
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        parsing: false,
                        plugins: { legend: { labels: { color: ink3, font: { size: 10 } } } },
                        scales: {
                            x: { type: "category", ticks: { color: ink3, font: { size: 9 } } },
                            y: { ticks: { color: ink3, font: { size: 9 } } },
                        },
                    },
                });
            }
        });
    }

    /* ---------- patient picker ---------- */
    function initPicker() {
        var picker = document.getElementById("patient-picker");
        var compareBtn = document.getElementById("compare-toggle");
        if (!picker) return;
        picker.querySelectorAll(".patient-chip").forEach(function (chip) {
            chip.addEventListener("click", function () {
                var pid = chip.getAttribute("data-pid");
                setCurrentPid(pid);
            });
        });
        if (compareBtn) {
            compareBtn.addEventListener("click", function () {
                STATE.compareMode = !STATE.compareMode;
                compareBtn.classList.toggle("is-active", STATE.compareMode);
                renderAll();
            });
        }
    }

    function setCurrentPid(pid) {
        STATE.currentPid = pid;
        document.querySelectorAll(".patient-chip").forEach(function (c) {
            c.classList.toggle("is-active", c.getAttribute("data-pid") === pid);
        });
        renderAll();
    }

    /* ---------- master render ---------- */
    function renderAll() {
        var sections = document.querySelectorAll("#dashboard-main .section");
        var compareSec = document.querySelector('[data-view="compare"]');
        if (STATE.compareMode) {
            sections.forEach(function (s) {
                s.style.display = s === compareSec ? "" : "none";
            });
            renderCompare(STATE.data.patients);
            return;
        }
        sections.forEach(function (s) {
            s.style.display = s === compareSec ? "none" : "";
        });
        var p = getPatient(STATE.currentPid);
        if (!p) return;
        try { renderHeader(p); } catch (e) { console.warn("header:", e); }
        try { renderTimeline(p); } catch (e) { console.warn("timeline:", e); }
        try { renderBiomarkers(p); } catch (e) { console.warn("biomarkers:", e); }
        try { renderLabs(p); } catch (e) { console.warn("labs:", e); }
        try { renderTreatments(p); } catch (e) { console.warn("treatments:", e); }
        try { renderImaging(p); } catch (e) { console.warn("imaging:", e); }
        try { renderPathMol(p); } catch (e) { console.warn("pathmol:", e); }
        try { renderWSI(p); } catch (e) { console.warn("wsi:", e); }
        try { renderDocuments(p); } catch (e) { console.warn("documents:", e); }
    }

    /* ---------- boot ---------- */
    function showEmpty(msg) {
        var empty = document.getElementById("empty-state");
        if (empty) {
            empty.style.display = "";
            if (msg) {
                var p = empty.querySelector("p.empty-msg");
                if (p) p.textContent = msg;
            }
        }
        document.querySelectorAll("#dashboard-main .section").forEach(function (s) {
            s.style.display = "none";
        });
    }

    function boot() {
        initThemeToggle();
        initDocModal();
        initPicker();
        fetch("/loki2/api/patients", { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
            .then(function (res) {
                if (!res.ok || !res.body || !res.body.patients) {
                    showEmpty();
                    return;
                }
                STATE.data = res.body;
                if (!STATE.data.patients.length) {
                    showEmpty();
                    return;
                }
                var empty = document.getElementById("empty-state");
                if (empty) empty.style.display = "none";
                setCurrentPid(STATE.data.patients[0].id);
            })
            .catch(function (err) {
                console.error("Failed to load patients:", err);
                showEmpty("Failed to load patient data. Check /loki2/api/health.");
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
