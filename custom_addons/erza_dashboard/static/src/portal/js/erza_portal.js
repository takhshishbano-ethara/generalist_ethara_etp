(function () {
    "use strict";

    const THEME_KEY = "erza-theme";
    const PAGE_SIZE = 10;
    const LANG_LABELS = {
        go: "Go",
        python: "Python",
        typescript: "TypeScript",
        javascript: "JavaScript",
        rust: "Rust",
        java: "Java",
        c: "C",
        cpp: "C++",
    };

    const TIER_ORDER = ["trivial", "easy", "medium", "hard", "expert"];

    const state = {
        summary: null,
        tasks: [],
        runs: [],
        filtered: [],
        runsByTask: {},
        modelKeys: [],
        modelLabels: {},
        search: "",
        tier: "",
        lang: "",
        sortBy: "task_slug",
        sortDir: 1,
        page: 1,
    };

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function debounce(fn, wait) {
        let timer;
        return function () {
            const args = arguments;
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), wait);
        };
    }

    // Erza Δ helpers: efficacy is reported in percentage points, signed, and
    // colour-coded (positive = Skill helped, negative = Skill hurt, kept + shown).
    function fmtPP(v) {
        const n = Number(v) || 0;
        return (n > 0 ? "+" : n < 0 ? "−" : "±") + Math.abs(n).toFixed(1) + " pp";
    }
    function deltaClass(v) {
        const n = Number(v) || 0;
        return n > 0.05 ? "erza-delta-pos" : n < -0.05 ? "erza-delta-neg" : "erza-delta-zero";
    }
    function deltaSpan(v) {
        return `<span class="erza-delta ${deltaClass(v)}">${fmtPP(v)}</span>`;
    }

    // System preference is authoritative and live-updating (raiden parity).
    // The inline <head> script already set data-theme from prefers-color-scheme
    // before styles resolved (no flash); here we sync the button and follow OS
    // changes live. The click toggle is a session-only override (not persisted).
    function initTheme() {
        const root = document.documentElement;
        const btn = $("#erza-theme-toggle");
        const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
        const currentTheme = () => {
            const explicit = root.getAttribute("data-theme");
            return explicit === "light" || explicit === "dark" ? explicit : "light";
        };
        const syncButton = () => {
            if (!btn) return;
            btn.setAttribute("aria-pressed", currentTheme() === "light" ? "true" : "false");
        };
        syncButton();
        if (btn) {
            btn.addEventListener("click", () => {
                root.setAttribute("data-theme", currentTheme() === "light" ? "dark" : "light");
                syncButton();
            });
        }
        // Follow OS changes live; system preference stays authoritative.
        const osChange = () => {
            root.setAttribute("data-theme", prefersDark.matches ? "dark" : "light");
            syncButton();
        };
        if (prefersDark.addEventListener) {
            prefersDark.addEventListener("change", osChange);
        } else if (prefersDark.addListener) {
            prefersDark.addListener(osChange); // Safari < 14
        }
    }

    // Previous localStorage-first theme selector (kept for rollback):
    // function initTheme() {
    //     const btn = $("#erza-theme-toggle");
    //     if (!btn) return;
    //     const apply = (t) => {
    //         document.documentElement.setAttribute("data-theme", t);
    //         btn.setAttribute("aria-pressed", t === "light" ? "true" : "false");
    //     };
    //     const initial = localStorage.getItem(THEME_KEY) || document.documentElement.getAttribute("data-theme") || "dark";
    //     apply(initial);
    //     btn.addEventListener("click", () => {
    //         const cur = document.documentElement.getAttribute("data-theme");
    //         const next = cur === "light" ? "dark" : "light";
    //         localStorage.setItem(THEME_KEY, next);
    //         apply(next);
    //     });
    // }

    function initReveal() {
        document.documentElement.classList.add("erza-js");
        const targets = $$("[data-animate]");
        if (!targets.length) return;
        const revealAll = () => targets.forEach((el) => el.classList.add("erza-visible"));
        if ("IntersectionObserver" in window) {
            const io = new IntersectionObserver(
                (entries) => {
                    entries.forEach((e) => {
                        if (e.isIntersecting) {
                            e.target.classList.add("erza-visible");
                            io.unobserve(e.target);
                        }
                    });
                },
                { threshold: 0.08, rootMargin: "0px 0px -5% 0px" }
            );
            targets.forEach((el) => io.observe(el));
            setTimeout(revealAll, 1500);
        } else {
            revealAll();
        }
        // Redundant: initAnimations() registers ScrollTrigger itself, and
        // initReveal does not use it. Kept commented for rollback.
        // if (window.gsap && window.ScrollTrigger) {
        //     window.gsap.registerPlugin(window.ScrollTrigger);
        // }
    }

    // Scroll progress rail: writes width into .scroll-progress on each rAF
    // while scrolling. Passive listeners, no layout thrash (raiden parity).
    function initScrollProgress() {
        const bar = document.querySelector(".scroll-progress");
        if (!bar) return;
        let ticking = false;
        const update = () => {
            const h = document.documentElement.scrollHeight - window.innerHeight;
            const pct = h > 0 ? (window.scrollY / h) * 100 : 0;
            bar.style.width = pct + "%";
            ticking = false;
        };
        const onScroll = () => {
            if (!ticking) {
                window.requestAnimationFrame(update);
                ticking = true;
            }
        };
        window.addEventListener("scroll", onScroll, { passive: true });
        window.addEventListener("resize", onScroll, { passive: true });
        update();
    }

    // Masthead on-load animation (raiden parity): wordmark + badge rise,
    // thesis reveals word-by-word. Progressive enhancement - if GSAP is
    // absent or motion is reduced, the masthead simply renders statically.
    function initAnimations() {
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

        // Split the thesis line into per-word wrappers for the staggered rise.
        const thesisEl = document.querySelector(".thesis");
        if (thesisEl && !thesisEl.querySelector(".thesis-word")) {
            const words = thesisEl.textContent.trim().split(/\s+/);
            thesisEl.innerHTML = words
                .map((w) => `<span class="thesis-word"><span class="thesis-word-inner">${w}</span></span>`)
                .join(" ");
        }

        const run = () => {
            if (typeof window.gsap === "undefined") return;
            const gsap = window.gsap;
            const EASE_OUT = "cubic-bezier(0.28, 0.11, 0.32, 1)";
            gsap.defaults({ ease: EASE_OUT, duration: 0.64 });
            gsap.from(".wordmark", { y: 24, opacity: 0, duration: 0.9, delay: 0.05, ease: EASE_OUT });
            gsap.from(".badge", { y: 16, opacity: 0, duration: 0.7, delay: 0.18, ease: EASE_OUT });
            gsap.from(".thesis-word-inner", {
                yPercent: 110, opacity: 0, duration: 0.9, stagger: 0.04, ease: EASE_OUT, delay: 0.3,
            });

            // Section child-stagger on scroll (raiden parity): each section's
            // direct children (label, heading, body, content) rise + fade in
            // sequence as the section enters view. GSAP owns the reveal here,
            // so add .erza-visible first to neutralize the CSS [data-animate]
            // gate (else the container's opacity:0 would double-hide children).
            // Guarded on ScrollTrigger - if it is absent, sections keep the
            // existing initReveal() CSS fade and nothing is hidden.
            if (window.ScrollTrigger) {
                gsap.registerPlugin(window.ScrollTrigger);
                document.querySelectorAll("main > .section").forEach((section) => {
                    section.classList.add("erza-visible");
                    const children = section.querySelectorAll(":scope > *");
                    gsap.from(children, {
                        y: 28,
                        opacity: 0,
                        stagger: 0.08,
                        duration: 0.64,
                        ease: EASE_OUT,
                        scrollTrigger: {
                            trigger: section,
                            start: "top 85%",
                            toggleActions: "play none none none",
                        },
                    });
                });
                window.addEventListener("resize", () => window.ScrollTrigger.refresh(), { passive: true });
            }
        };

        // GSAP is deferred; run now if ready, else once it loads.
        if (typeof window.gsap !== "undefined") {
            run();
        } else {
            window.addEventListener("load", run, { once: true });
        }
    }

    async function loadData() {
        try {
            const res = await fetch("/erza-samples/api/dataset", { headers: { Accept: "application/json" } });
            const data = await res.json();
            state.summary = data.summary || null;
            state.tasks = data.tasks || [];
            state.runs = data.runs || [];
            state.filtered = state.tasks.slice();
            state.runsByTask = {};
            state.runs.forEach((r) => {
                if (!state.runsByTask[r.task_uuid]) state.runsByTask[r.task_uuid] = [];
                state.runsByTask[r.task_uuid].push(r);
            });
            if (state.summary && state.summary.models) {
                state.modelKeys = state.summary.models.map((m) => m.key);
                state.summary.models.forEach((m) => {
                    state.modelLabels[m.key] = m.display_name;
                });
            }
        } catch (e) {
            console.error("Erza: dataset load failed", e);
        }
    }

    function renderKpi() {
        const s = state.summary;
        if (!s) return;
        const setNum = (field, value) => {
            const el = document.querySelector(`[data-field="${field}"]`);
            if (el) el.textContent = value;
        };
        setNum("task_count", s.task_count);
        setNum("run_count", s.run_count);
        setNum("model_count", s.model_count);
        setNum("language_count", s.language_count);
        setNum("skill_count", s.skill_count);
        setNum("codebase_count", s.codebase_count);
        setNum("no_skill_pass_rate_overall", `${(s.no_skill_pass_rate_overall || 0).toFixed(1)}%`);
        setNum("pass_rate_overall", `${(s.pass_rate_overall || 0).toFixed(1)}%`);
        setNum("mean_delta_overall", fmtPP(s.mean_delta_overall));
    }

    function renderTiers() {
        const s = state.summary;
        const host = $("#erza-tier-grid");
        if (!s || !host) return;
        // Only render tiers that actually contain task bundles (keeps the grid
        // clean while the delivery is small; scales up as tasks are added).
        const parts = TIER_ORDER
            .filter((tier) => (((s.tiers || {})[tier] || {}).task_count || 0) > 0)
            .map((tier) => {
                const t = s.tiers[tier];
                return `
                <div class="tier-card" data-tier="${tier}">
                    <h3>${tier}</h3>
                    <div class="tier-count">${t.task_count}</div>
                    <div class="tier-count-label">${t.task_count === 1 ? "task" : "tasks"}</div>
                    <div class="tier-stat"><span class="tier-stat-label">Runs</span><span class="tier-stat-value">${t.run_count}</span></div>
                    <div class="tier-stat"><span class="tier-stat-label">No-Skills</span><span class="tier-stat-value">${(t.no_skill_pass_rate || 0).toFixed(1)}%</span></div>
                    <div class="tier-stat"><span class="tier-stat-label">With-Skills</span><span class="tier-stat-value">${(t.pass_rate || 0).toFixed(1)}%</span></div>
                </div>`;
            });
        host.innerHTML = parts.join("");
    }

    function renderModelTable() {
        const s = state.summary;
        const tbody = $("#erza-model-table tbody");
        if (!s || !tbody) return;
        tbody.innerHTML = (s.models || [])
            .map(
                (m) => `
                <tr>
                    <td><span class="color-dot" style="background:${escapeHtml(m.color)}"></span><strong>${escapeHtml(m.display_name)}</strong></td>
                    <td>${escapeHtml(m.provider || "-")}</td>
                    <td class="num">${m.run_count}</td>
                    <td class="num">${(m.no_skill_pass_rate || 0).toFixed(1)}%</td>
                    <td class="num">${(m.pass_at_3 || 0).toFixed(1)}%</td>
                    <td class="num">${(m.mean_score || 0).toFixed(1)}%</td>
                    <td class="num">${deltaSpan(m.mean_delta)}</td>
                </tr>`
            )
            .join("");
    }

    function renderMatrix(tableId, field, kind = "pct") {
        const s = state.summary;
        const table = $("#" + tableId);
        if (!s || !table) return;
        const thead = table.querySelector("thead tr");
        const tbody = table.querySelector("tbody");
        thead.innerHTML = "<th>Tier (n)</th>" + state.modelKeys.map((k) => {
            const label = state.modelLabels[k] || k;
            const sp = label.indexOf(" ");
            const primary = sp === -1 ? label : label.slice(0, sp);
            const secondary = sp === -1 ? "" : label.slice(sp + 1);
            const sub = secondary ? `<span class="th-model-sub">${escapeHtml(secondary)}</span>` : "";
            return `<th class="num"><span class="th-model"><span class="th-model-main">${escapeHtml(primary)}</span>${sub}</span></th>`;
        }).join("");
        tbody.innerHTML = TIER_ORDER
            .filter((tier) => (((s.tiers || {})[tier] || {}).task_count || 0) > 0)
            .map((tier) => {
            const t = (s.tiers || {})[tier] || {};
            const cells = state.modelKeys
                .map((k) => {
                    const v = ((t.per_model || {})[k] || {})[field] || 0;
                    const display = kind === "delta" ? deltaSpan(v) : `${v.toFixed(1)}%`;
                    return `<td class="num">${display}</td>`;
                })
                .join("");
            return `<tr><td><span class="tier-cell"><span class="tier-badge" data-tier="${tier}">${tier}</span><span class="tier-badge-n">(${t.task_count || 0})</span></span></td>${cells}</tr>`;
        }).join("");
    }

    /* renderLangGrid: dead code — portal_templates.xml has no #erza-lang-grid host
       (the Domains section uses a hardcoded .erza-domain-table), so this never rendered.
       Kept commented for rollback if a live language grid is reintroduced.
    function renderLangGrid() {
        const s = state.summary;
        const host = $("#erza-lang-grid");
        if (!s || !host) return;
        const langs = Object.entries(s.languages || {}).sort((a, b) => b[1].task_count - a[1].task_count);
        host.innerHTML = langs
            .map(
                ([key, entry]) => `
                <div class="lang-card">
                    <div class="lang-card-name">${escapeHtml(entry.label || LANG_LABELS[key] || key)}</div>
                    <div class="lang-card-count">${entry.task_count}</div>
                    <div class="lang-card-label">tasks</div>
                </div>`
            )
            .join("");
    }
    */

    function populateLangFilter() {
        const sel = $("#erza-filter-lang");
        if (!sel) return;
        const langs = Array.from(new Set(state.tasks.map((t) => t.language).filter(Boolean))).sort();
        const opts = langs.map((l) => `<option value="${l}">${escapeHtml(LANG_LABELS[l] || l)}</option>`).join("");
        sel.innerHTML = '<option value="">All languages</option>' + opts;
    }

    function applyFilters() {
        const q = state.search.trim().toLowerCase();
        state.filtered = state.tasks.filter((t) => {
            if (state.tier && t.difficulty !== state.tier) return false;
            if (state.lang && t.language !== state.lang) return false;
            if (q) {
                const hay = [t.task_slug, t.codebase, t.uuid, t.keywords].filter(Boolean).join(" ").toLowerCase();
                if (hay.indexOf(q) === -1) return false;
            }
            return true;
        });
        const dir = state.sortDir;
        const key = state.sortBy;
        state.filtered.sort((a, b) => {
            let av = a[key];
            let bv = b[key];
            if (key === "difficulty") {
                av = TIER_ORDER.indexOf(av);
                bv = TIER_ORDER.indexOf(bv);
            }
            if (av === null || av === undefined) av = "";
            if (bv === null || bv === undefined) bv = "";
            if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
            return String(av).localeCompare(String(bv)) * dir;
        });
        state.page = 1;
        renderViewer();
    }

    function passBarHtml(pct) {
        const p = Math.max(0, Math.min(100, pct || 0));
        let cls = "";
        if (p < 20) cls = 'data-pct-low="1"';
        else if (p < 60) cls = 'data-pct-mid="1"';
        else cls = 'data-pct-high="1"';
        return `<span class="pass-bar"><span class="pass-bar-fill" ${cls} style="width:${p.toFixed(1)}%"></span></span>${p.toFixed(1)}%`;
    }

    function renderViewer() {
        const tbody = $("#erza-viewer-table tbody");
        const count = $("#erza-viewer-count");
        const pagination = $("#erza-pagination");
        if (!tbody || !count || !pagination) return;
        const total = state.filtered.length;
        count.textContent = `${total} task${total === 1 ? "" : "s"}`;
        const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
        if (state.page > totalPages) state.page = totalPages;
        const start = (state.page - 1) * PAGE_SIZE;
        const pageRows = state.filtered.slice(start, start + PAGE_SIZE);
        const rows = pageRows
            .map((t) => {
                const detail = renderTaskDetail(t);
                return `
                <tr data-uuid="${escapeHtml(t.uuid)}">
                    <td><span class="task-slug">${escapeHtml(t.task_slug || t.uuid)}</span><span class="task-uuid">${escapeHtml(t.uuid)}</span></td>
                    <td>${escapeHtml(t.codebase || "-")}</td>
                    <td><span class="lang-badge">${escapeHtml(LANG_LABELS[t.language] || t.language || "-")}</span></td>
                    <td><span class="tier-badge" data-tier="${escapeHtml(t.difficulty)}">${escapeHtml(t.difficulty)}</span></td>
                    <td class="num">${passBarHtml(t.no_skill_pass_rate)}</td>
                    <td class="num">${passBarHtml(t.pass_rate)}</td>
                    <td class="num">${(t.mean_score || 0).toFixed(1)}%</td>
                    <td class="num">${deltaSpan(t.mean_delta)}</td>
                    <td class="task-expand">▸</td>
                </tr>
                <tr class="task-detail-row" data-detail-for="${escapeHtml(t.uuid)}">
                    <td colspan="8"><div class="task-detail">${detail}</div></td>
                </tr>`;
            })
            .join("");
        tbody.innerHTML = rows || '<tr><td colspan="8" style="text-align:center; padding:32px; color:var(--muted);">No tasks match your filters.</td></tr>';
        renderPagination(totalPages);
    }

    function renderTaskDetail(task) {
        const runs = (state.runsByTask[task.uuid] || []).slice();
        const modelBlocks = state.modelKeys.map((mk) => {
            const mruns = runs.filter((r) => r.model_key === mk).sort((a, b) => a.run_number - b.run_number);
            const n = mruns.length;
            const nsPass = mruns.filter((r) => r.no_skill_binary).length;
            const wsPass = mruns.filter((r) => r.score_binary).length;
            const meanDelta = n ? (mruns.reduce((a, r) => a + (r.delta !== undefined ? r.delta : r.score - r.no_skill_score), 0) / n) * 100 : 0;
            const meanCost = n ? mruns.reduce((a, r) => a + r.cost_usd, 0) / n : 0;
            const runRows = mruns
                .map((r) => {
                    const ns = (r.no_skill_score * 100).toFixed(0);
                    const ws = (r.score * 100).toFixed(0);
                    const d = (r.delta !== undefined ? r.delta : r.score - r.no_skill_score) * 100;
                    return `<div class="detail-run"><span class="detail-run-label">run_${r.run_number}</span><span class="detail-run-value">${ns}% <span class="detail-run-arrow">→</span> ${ws}% <span class="erza-delta ${deltaClass(d)}">${fmtPP(d)}</span></span></div>`;
                })
                .join("");
            return `
                <div class="detail-block">
                    <div class="detail-model-name">${escapeHtml(state.modelLabels[mk] || mk)}</div>
                    ${runRows}
                    <div class="detail-summary"><span>No-Skills pass</span><span>${nsPass}/${n || 0}</span></div>
                    <div class="detail-summary"><span>With-Skills pass</span><span>${wsPass}/${n || 0}</span></div>
                    <div class="detail-summary"><span>Mean Δ</span><span class="${deltaClass(meanDelta)}">${fmtPP(meanDelta)}</span></div>
                    <div class="detail-summary"><span>Mean cost</span><span>$${meanCost.toFixed(2)}</span></div>
                </div>`;
        }).join("");
        const links = [
            task.dataset_url ? `<a href="${escapeHtml(task.dataset_url)}" target="_blank">Dataset</a>` : "",
            task.trajectories_url ? `<a href="${escapeHtml(task.trajectories_url)}" target="_blank">Trajectories</a>` : "",
            task.instruction_url ? `<a href="${escapeHtml(task.instruction_url)}" target="_blank">task.md</a>` : "",
        ].filter(Boolean).join("");
        return `
            <div class="detail-grid">${modelBlocks}</div>
            <div class="detail-links">${links}</div>`;
    }

    function paginationRange(current, total) {
        if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
        if (current <= 4) return [1, 2, 3, 4, 5, "…", total];
        if (current >= total - 3) return [1, "…", total - 4, total - 3, total - 2, total - 1, total];
        return [1, "…", current - 1, current, current + 1, "…", total];
    }

    function renderPagination(totalPages) {
        const host = $("#erza-pagination");
        if (!host) return;
        if (totalPages <= 1) { host.innerHTML = ""; return; }
        const parts = [];
        parts.push(`<button type="button" data-page="prev" ${state.page === 1 ? "disabled" : ""}>‹ Prev</button>`);
        paginationRange(state.page, totalPages).forEach((p) => {
            if (p === "…") parts.push('<span class="ellipsis">…</span>');
            else parts.push(`<button type="button" data-page="${p}" class="${p === state.page ? "is-active" : ""}">${p}</button>`);
        });
        parts.push(`<button type="button" data-page="next" ${state.page === totalPages ? "disabled" : ""}>Next ›</button>`);
        host.innerHTML = parts.join("");
    }

    function attachViewerHandlers() {
        const search = $("#erza-search");
        const tier = $("#erza-filter-tier");
        const lang = $("#erza-filter-lang");
        const sort = $("#erza-sort");
        const table = $("#erza-viewer-table");
        const pagination = $("#erza-pagination");

        /* Sort direction toggle deferred; single-select model. */
        const SORT_DIR_MAP = { pass_rate: -1, no_skill_pass_rate: -1, mean_delta: -1 };
        const applySortKey = (key) => {
            state.sortBy = key;
            state.sortDir = SORT_DIR_MAP[key] || 1;
            applyFilters();
        };

        if (search) search.addEventListener("input", debounce((e) => { state.search = e.target.value; applyFilters(); }, 200));
        if (tier) tier.addEventListener("change", (e) => { state.tier = e.target.value; applyFilters(); });
        if (lang) lang.addEventListener("change", (e) => { state.lang = e.target.value; applyFilters(); });
        if (sort) sort.addEventListener("change", (e) => { applySortKey(e.target.value); });

        if (table) {
            table.addEventListener("click", (e) => {
                const row = e.target.closest("tr[data-uuid]");
                if (!row) return;
                row.classList.toggle("expanded");
                const arrow = row.querySelector(".task-expand");
                if (arrow) arrow.textContent = row.classList.contains("expanded") ? "▾" : "▸";
            });
        }
        if (pagination) {
            pagination.addEventListener("click", (e) => {
                const btn = e.target.closest("button[data-page]");
                if (!btn) return;
                const total = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
                if (btn.dataset.page === "prev") state.page = Math.max(1, state.page - 1);
                else if (btn.dataset.page === "next") state.page = Math.min(total, state.page + 1);
                else state.page = parseInt(btn.dataset.page, 10);
                renderViewer();
                document.getElementById("section-viewer").scrollIntoView({ behavior: "smooth", block: "start" });
            });
        }
    }

    function mbCssVar(name, fallback) {
        var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return v || fallback;
    }
    function mbPalette() {
        return {
            opus:      mbCssVar('--chart-opus',   '#EA9A3C'),
            gemini:    mbCssVar('--chart-gemini', '#4A9AE0'),
            gpt:       mbCssVar('--chart-gpt',    '#57B85C'),
            noskill:   mbCssVar('--chart-noskill', '#9aa2c4'),
            withskill: mbCssVar('--chart-withskill', '#EE00EE'),
            ink:       mbCssVar('--ink',              '#0B0E14'),
            muted:     mbCssVar('--muted',            '#8a8fa5'),
            grid:      mbCssVar('--border',           'rgba(0,0,0,0.10)'),
            surface:   mbCssVar('--bg-2',             '#ffffff'),
        };
    }
    function mbTooltip(p, unit) {
        var isMoney = unit === '$';
        return {
            enabled: true,
            backgroundColor: p.surface,
            titleColor: p.ink,
            bodyColor: p.ink,
            borderColor: p.grid,
            borderWidth: 1,
            padding: 10,
            cornerRadius: 8,
            callbacks: {
                label: function (ctx) {
                    var v = ctx.parsed && (ctx.parsed.y !== undefined ? ctx.parsed.y : ctx.parsed);
                    if (typeof v !== 'number') return ctx.dataset.label + ': -';
                    var formatted = isMoney
                        ? '$' + v.toFixed(2)
                        : (unit === '%' ? v.toFixed(1) + '%'
                            : (unit === 'pp' ? (v >= 0 ? '+' : '') + v.toFixed(1) + ' pp'
                                : v.toFixed(2)));
                    return ctx.dataset.label + ': ' + formatted;
                },
            },
        };
    }
    var ERZA_TIER_ORDER = ['trivial', 'easy', 'medium', 'hard', 'expert'];
    var ERZA_TIER_LABELS = ['Trivial', 'Easy', 'Medium', 'Hard', 'Expert'];
    var ERZA_MODEL_ORDER = null;

    function mbDiscoverModelOrder() {
        if (state && state.summary && Array.isArray(state.summary.models) && state.summary.models.length) {
            return state.summary.models.map(function (m) { return m.key; });
        }
        var seen = Object.create(null);
        var order = [];
        (state && state.runs ? state.runs : []).forEach(function (r) {
            if (r && r.model_key && !seen[r.model_key]) { seen[r.model_key] = 1; order.push(r.model_key); }
        });
        return order;
    }
    function mbModelLabel(key) {
        if (state && state.summary && state.summary.models) {
            var hit = state.summary.models.find(function (m) { return m.key === key; });
            if (hit) return hit.display_name || key;
        }
        return key;
    }
    function mbSeriesColor(key, p) {
        if (!ERZA_MODEL_ORDER) ERZA_MODEL_ORDER = mbDiscoverModelOrder();
        var idx = ERZA_MODEL_ORDER.indexOf(key);
        if (idx < 0) idx = 0;
        return [p.opus, p.gemini, p.gpt][idx % 3];
    }
    function mbTierPassSeries(modelKey) {
        var out = [];
        var tiers = (state && state.summary && state.summary.tiers) || {};
        ERZA_TIER_ORDER.forEach(function (t) {
            var block = tiers[t] || {};
            var perModel = block.per_model && block.per_model[modelKey];
            if (perModel && typeof perModel.pass_rate === 'number') {
                out.push(perModel.pass_rate);
                return;
            }
            var runs = (state.runs || []).filter(function (r) {
                return r && r.model_key === modelKey && r.difficulty === t;
            });
            if (!runs.length) { out.push(null); return; }
            var passed = runs.filter(function (r) { return r.score_binary === true || (r.score || 0) >= 1.0; }).length;
            out.push(runs.length ? (passed / runs.length) * 100 : null);
        });
        return out;
    }
    function mbTierCostSeries(modelKey) {
        var out = [];
        var tiers = (state && state.summary && state.summary.tiers) || {};
        ERZA_TIER_ORDER.forEach(function (t) {
            var block = tiers[t] || {};
            var perModel = block.per_model && block.per_model[modelKey];
            if (perModel && typeof perModel.mean_cost_usd === 'number') {
                out.push(perModel.mean_cost_usd);
                return;
            }
            var runs = (state.runs || []).filter(function (r) {
                return r && r.model_key === modelKey && r.difficulty === t;
            });
            if (!runs.length) { out.push(null); return; }
            var sum = runs.reduce(function (a, r) { return a + (r.cost_usd || 0); }, 0);
            out.push(sum / runs.length);
        });
        return out;
    }
    var ERZA_HUNK_BUCKETS = [
        { key: '1-10',  min: 1,  max: 10 },
        { key: '11-40', min: 11, max: 40 },
        { key: '41+',   min: 41, max: Infinity },
    ];

    function mbHunkBucketOf(hunks) {
        for (var i = 0; i < ERZA_HUNK_BUCKETS.length; i += 1) {
            var b = ERZA_HUNK_BUCKETS[i];
            if (hunks >= b.min && hunks <= b.max) return i;
        }
        return -1;
    }

    function mbTaskModelPass(task, modelKey) {
        if (task && task.models && task.models[modelKey] && typeof task.models[modelKey].pass_rate === 'number') {
            return task.models[modelKey].pass_rate;
        }
        var runs = (state.runs || []).filter(function (r) {
            return r && r.model_key === modelKey && r.task_uuid === task.uuid;
        });
        if (!runs.length) return null;
        var passed = runs.filter(function (r) { return r.score_binary === true || (r.score || 0) >= 1.0; }).length;
        return (passed / runs.length) * 100;
    }

    function mbHunkBucketAgg() {
        var order = ERZA_MODEL_ORDER || mbDiscoverModelOrder();
        var counts = ERZA_HUNK_BUCKETS.map(function () { return 0; });
        var byModel = {};
        order.forEach(function (k) { byModel[k] = ERZA_HUNK_BUCKETS.map(function () { return { sum: 0, n: 0 }; }); });
        (state.tasks || []).forEach(function (t) {
            if (!t || typeof t.src_hunks !== 'number') return;
            var bi = mbHunkBucketOf(t.src_hunks);
            if (bi < 0) return;
            counts[bi] += 1;
            order.forEach(function (k) {
                var v = mbTaskModelPass(t, k);
                if (typeof v === 'number') {
                    byModel[k][bi].sum += v;
                    byModel[k][bi].n += 1;
                }
            });
        });
        var labels = ERZA_HUNK_BUCKETS.map(function (b, i) { return b.key + '\n(n=' + counts[i] + ')'; });
        var series = {};
        order.forEach(function (k) {
            series[k] = byModel[k].map(function (agg) { return agg.n ? (agg.sum / agg.n) : null; });
        });
        return { labels: labels, series: series };
    }

    // Per-point value labels, positioned to mirror the source PNGs: within each
    // x-column the HIGHEST value sits above its point, the LOWEST below, and any
    // MIDDLE value to the right. This also fans out overlapping/tied points
    // (e.g. all three models at 100% or 0%) into above / right / below cleanly.
    var mbValueLabelPlugin = {
        id: 'mbValueLabels',
        afterDatasetsDraw: function (chart, args, pluginOpts) {
            var opts = pluginOpts || {};
            var format = opts.format || function (v) { return String(v); };
            var ctx = chart.ctx;

            // Group every visible point by its x-index (category column).
            var groups = {};
            chart.data.datasets.forEach(function (ds, di) {
                var meta = chart.getDatasetMeta(di);
                if (!meta || meta.hidden || !meta.data) return;
                meta.data.forEach(function (elem, i) {
                    var raw = ds.data[i];
                    var val = (raw && typeof raw === 'object') ? raw.y : raw;
                    if (val === null || val === undefined || typeof val !== 'number' || isNaN(val)) return;
                    (groups[i] || (groups[i] = [])).push({ x: elem.x, y: elem.y, val: val, color: ds.borderColor || '#000' });
                });
            });

            ctx.save();
            ctx.font = "700 " + (opts.fontSize || 11) + "px 'DM Sans', system-ui, sans-serif";
            ctx.textBaseline = 'middle';
            ctx.lineJoin = 'round';
            // Halo colour = chart surface, so a dashed line or marker passing behind
            // a label is knocked out and the number stays crisp and readable.
            var haloColor = mbCssVar('--bg-2', '#ffffff');
            var DX = 18, DY_UP = 18, DY_DN = 20, MIN_GAP = 18;
            var lastCol = (chart.data.labels || []).length - 1;
            // 'trail' pushes labels sideways into the empty margin around each point
            // (used where series stay separated horizontally, e.g. the monotonic
            // complexity chart); 'fan' stacks highest-above / lowest-below / middle-
            // right (used where lines cross, e.g. the tier charts).
            var placement = opts.placement || 'fan';

            Object.keys(groups).forEach(function (key) {
                var colIdx = +key;
                var pts = groups[key];
                // Top of chart (smallest y = highest value) first; stable sort keeps
                // dataset order for ties so identical points fan out consistently.
                pts.sort(function (a, b) { return a.y - b.y; });

                var labels;
                if (placement === 'trail') {
                    // All labels sit beside their point in the open margin - left of
                    // the point, or right on the final column - then spread vertically.
                    var toRight = (colIdx === lastCol);
                    labels = pts.map(function (pt) {
                        return { text: format(pt.val), x: pt.x + (toRight ? DX : -DX), y: pt.y, align: toRight ? 'left' : 'right', color: pt.color };
                    });
                } else {
                    // highest -> above, lowest -> below, middle -> right of the point.
                    labels = pts.map(function (pt, idx) {
                        var lx, ly, align;
                        if (idx === 0) { lx = pt.x; ly = pt.y - DY_UP; align = 'center'; }
                        else if (idx === pts.length - 1) { lx = pt.x; ly = pt.y + DY_DN; align = 'center'; }
                        else { lx = pt.x + DX; ly = pt.y; align = 'left'; }
                        return { text: format(pt.val), x: lx, y: ly, align: align, color: pt.color };
                    });
                }

                // De-collide: enforce a minimum vertical gap so labels of near-equal
                // value (e.g. 58% / 55%) never sit on top of one another. Recentre the
                // spread stack on its original midpoint so it stays next to the points.
                labels.sort(function (a, b) { return a.y - b.y; });
                var before = labels.reduce(function (s, L) { return s + L.y; }, 0) / labels.length;
                for (var i = 1; i < labels.length; i++) {
                    if (labels[i].y - labels[i - 1].y < MIN_GAP) {
                        labels[i].y = labels[i - 1].y + MIN_GAP;
                    }
                }
                var after = labels.reduce(function (s, L) { return s + L.y; }, 0) / labels.length;
                var shift = before - after;
                if (shift) { labels.forEach(function (L) { L.y += shift; }); }

                labels.forEach(function (L) {
                    ctx.textAlign = L.align;
                    ctx.lineWidth = 3.5;
                    ctx.strokeStyle = haloColor;
                    ctx.strokeText(L.text, L.x, L.y);
                    ctx.fillStyle = L.color;
                    ctx.fillText(L.text, L.x, L.y);
                });
            });
            ctx.restore();
        },
    };

    function mbLineDataset(label, color, data) {
        return {
            label: label,
            data: data,
            borderColor: color,
            backgroundColor: color,
            borderDash: [8, 6],
            borderWidth: 2.5,
            fill: false,
            spanGaps: true,
            tension: 0,
            pointRadius: 6,
            pointHoverRadius: 8,
            pointBackgroundColor: color,
            pointBorderColor: color,
            pointBorderWidth: 2,
        };
    }

    // Erza domain breakdown - Cybersecurity and
    // Software Engineering excluded. Shared by the hero + Δ charts.
    var ERZA_DOMAIN_LABELS = ['Natural\nScience', 'Media', 'Industrial', 'Finance', 'Office', 'Math & OR'];
    var ERZA_DOMAIN_NOSKILL = [42.0, 23.3, 23.9, 19.1, 40.5, 45.7];
    var ERZA_DOMAIN_WITHSKILL = [70.8, 47.4, 39.6, 33.3, 53.0, 55.4];
    var ERZA_DOMAIN_DELTA = [28.8, 24.1, 15.7, 14.2, 12.6, 9.7];

    function mbBuildPassRateChart(canvas, p) {
        // Erza domain pass rates, no-Skills vs with-Skills. The vertical
        // gap between the two lines is the Skill efficacy (Δ).
        var noSkill = mbLineDataset('No-Skills', p.noskill, ERZA_DOMAIN_NOSKILL);
        var withSkill = mbLineDataset('With-Skills', p.withskill, ERZA_DOMAIN_WITHSKILL);
        withSkill.borderDash = [];      // solid to emphasise the aided arm
        withSkill.borderWidth = 3;
        return new Chart(canvas, {
            type: 'line',
            data: { labels: ERZA_DOMAIN_LABELS, datasets: [noSkill, withSkill] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: { padding: { top: 30, right: 52, left: 10, bottom: 6 } },
                plugins: {
                    legend: { display: true, position: 'top', align: 'end', labels: { color: p.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: 'circle', font: { family: "'DM Sans', sans-serif", size: 12 }, padding: 14 } },
                    tooltip: mbTooltip(p, '%'),
                    title: { display: true, text: 'Pass Rate by Domain - No-Skills vs With-Skills', color: p.ink, font: { family: "'DM Sans', sans-serif", size: 15, weight: '600' }, padding: { bottom: 12 } },
                    mbValueLabels: { format: function (v) { return Math.round(v) + '%'; } },
                },
                scales: {
                    x: { offset: true, grid: { display: false, drawBorder: false }, ticks: { color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 }, callback: function (val) { var l = this.getLabelForValue(val); return String(l).split('\n'); } }, title: { display: true, text: 'Erza task domain', color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
                    y: { min: 0, max: 80, grid: { color: p.grid, drawBorder: false }, ticks: { stepSize: 20, color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 } }, title: { display: true, text: 'Pass rate (%)', color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
                },
            },
            plugins: [mbValueLabelPlugin],
        });
    }

    function mbBuildPassTierChart(canvas, p) {
        // Skill efficacy (Δ, pp) by Erza domain - the quantified gap
        // between the with-Skills and no-Skills arms.
        var ds = mbLineDataset('Δ efficacy', p.withskill, ERZA_DOMAIN_DELTA);
        ds.borderDash = [];
        ds.borderWidth = 3;
        return new Chart(canvas, {
            type: 'line',
            data: { labels: ERZA_DOMAIN_LABELS, datasets: [ds] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: { padding: { top: 30, right: 44, left: 10, bottom: 6 } },
                plugins: {
                    legend: { display: false },
                    tooltip: mbTooltip(p, 'pp'),
                    title: { display: true, text: 'Skill Efficacy (Δ) by Domain', color: p.ink, font: { family: "'DM Sans', sans-serif", size: 15, weight: '600' }, padding: { bottom: 12 } },
                    mbValueLabels: { placement: 'trail', format: function (v) { return '+' + v.toFixed(1); } },
                },
                scales: {
                    x: { offset: true, grid: { display: false, drawBorder: false }, ticks: { color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 }, callback: function (val) { var l = this.getLabelForValue(val); return String(l).split('\n'); } }, title: { display: true, text: 'Erza task domain', color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
                    y: { min: 0, max: 35, grid: { color: p.grid, drawBorder: false }, ticks: { stepSize: 5, color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 } }, title: { display: true, text: 'Δ (percentage points)', color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
                },
            },
            plugins: [mbValueLabelPlugin],
        });
    }

    function mbBuildCostTierChart(canvas, p) {
        // Real agent cost per run for the sample bundle (claude-opus-4-8,
        // erza-rb1-robust-consensus task): the with-Skills arm is both far cheaper AND passes.
        var labels = ['run_1', 'run_2', 'run_3'];
        var noSkill = mbLineDataset('No-Skills', p.noskill, [1.268, 1.590, 0.376]);
        var withSkill = mbLineDataset('With-Skills', p.withskill, [0.145, 0.148, 0.124]);
        withSkill.borderDash = [];
        withSkill.borderWidth = 3;
        return new Chart(canvas, {
            type: 'line',
            data: { labels: labels, datasets: [noSkill, withSkill] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                layout: { padding: { top: 30, right: 52, left: 10, bottom: 6 } },
                plugins: {
                    legend: { display: true, position: 'top', align: 'start', labels: { color: p.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: 'circle', font: { family: "'DM Sans', sans-serif", size: 12 }, padding: 14 } },
                    tooltip: mbTooltip(p, '$'),
                    title: { display: true, text: 'Agent Cost per Run - No-Skills vs With-Skills', color: p.ink, font: { family: "'DM Sans', sans-serif", size: 15, weight: '600' }, padding: { bottom: 12 } },
                    mbValueLabels: { format: function (v) { return '$' + v.toFixed(2); } },
                },
                scales: {
                    x: { offset: true, grid: { display: false, drawBorder: false }, ticks: { color: p.ink, font: { family: "'DM Sans', sans-serif", size: 12 } }, title: { display: true, text: 'Trial', color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
                    y: { min: 0, max: 1.8, grid: { color: p.grid, drawBorder: false }, ticks: { stepSize: 0.3, color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 } }, title: { display: true, text: 'Cost per run (USD)', color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
                },
            },
            plugins: [mbValueLabelPlugin],
        });
    }

    var mbCharts = {};

    function mbRenderCharts() {
        if (typeof Chart === 'undefined') return false;
        // Charts plot fixed published figures (see builders), so they no longer
        // depend on the live API payload - render as soon as Chart.js is ready.
        var p = mbPalette();
        var elPass = document.getElementById('erza-chart-pass-rate');
        var elTier = document.getElementById('erza-chart-pass-tier');
        var elCost = document.getElementById('erza-chart-cost-tier');
        try { if (elPass) mbCharts.pass_rate = mbBuildPassRateChart(elPass, p); }
        catch (e) { console.error('erza: pass-rate chart failed', e); }
        try { if (elTier) mbCharts.pass_tier = mbBuildPassTierChart(elTier, p); }
        catch (e) { console.error('erza: pass-tier chart failed', e); }
        try { if (elCost) mbCharts.cost_tier = mbBuildCostTierChart(elCost, p); }
        catch (e) { console.error('erza: cost-tier chart failed', e); }
        return true;
    }
    function mbDestroyCharts() {
        Object.keys(mbCharts).forEach(function (k) {
            try { if (mbCharts[k]) mbCharts[k].destroy(); } catch (e) {}
            delete mbCharts[k];
        });
    }
    function mbReRenderCharts() {
        mbDestroyCharts();
        mbRenderCharts();
    }
    function mbInitCharts() {
        var tries = 0;
        var poll = setInterval(function () {
            tries += 1;
            if (typeof Chart !== 'undefined') {
                clearInterval(poll);
                try { mbRenderCharts(); }
                catch (e) { console.error('erza: chart render failed', e); }
                // Charts render async (after initAnimations built the triggers)
                // and grow the page. Recompute ScrollTrigger start positions so
                // section reveals fire at correct offsets instead of stale ones
                // (a stale start past max-scroll can leave children opacity:0).
                if (window.ScrollTrigger) { window.ScrollTrigger.refresh(); }
                var mo = new MutationObserver(function (muts) {
                    for (var i = 0; i < muts.length; i += 1) {
                        if (muts[i].attributeName === 'data-theme') { mbReRenderCharts(); break; }
                    }
                });
                mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
            } else if (tries > 60) {
                clearInterval(poll);
                if (window && window.console && console.warn) {
                    console.warn('erza: Chart.js did not load within 3s; charts skipped.');
                }
            }
        }, 50);
    }

    async function init() {
        initTheme();
        initReveal();
        initScrollProgress();
        initAnimations();
        await loadData();
        mbInitCharts();
        renderKpi();
        renderTiers();
        renderModelTable();
        renderMatrix("erza-passrate-table", "pass_rate");
        renderMatrix("erza-meanscore-table", "mean_delta", "delta");
        // renderLangGrid();  // dead: no #erza-lang-grid host in template (see commented def above)
        populateLangFilter();
        attachViewerHandlers();
        applyFilters();
        // The synchronous renders above (KPIs, matrix tables, lang grid) changed
        // page height after initAnimations() created the ScrollTriggers. Refresh
        // once so GSAP's from() section reveals use correct scroll positions.
        // (mbInitCharts refreshes again when the async chart render lands.)
        if (window.ScrollTrigger) { window.ScrollTrigger.refresh(); }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
