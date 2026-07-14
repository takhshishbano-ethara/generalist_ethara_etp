(function () {
    "use strict";

    const THEME_KEY = "milobench:theme";
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

    function initTheme() {
        const btn = $("#mb-theme-toggle");
        if (!btn) return;
        const apply = (t) => {
            document.documentElement.setAttribute("data-theme", t);
            btn.setAttribute("aria-pressed", t === "light" ? "true" : "false");
        };
        const initial = localStorage.getItem(THEME_KEY) || document.documentElement.getAttribute("data-theme") || "dark";
        apply(initial);
        btn.addEventListener("click", () => {
            const cur = document.documentElement.getAttribute("data-theme");
            const next = cur === "light" ? "dark" : "light";
            localStorage.setItem(THEME_KEY, next);
            apply(next);
        });
    }

    function initScrollProgress() {
        const bar = $(".scroll-progress");
        if (!bar) return;
        let ticking = false;
        const update = () => {
            const h = document.documentElement.scrollHeight - window.innerHeight;
            const pct = h > 0 ? (window.scrollY / h) * 100 : 0;
            bar.style.width = pct + "%";
            ticking = false;
        };
        window.addEventListener(
            "scroll",
            () => {
                if (!ticking) {
                    window.requestAnimationFrame(update);
                    ticking = true;
                }
            },
            { passive: true }
        );
        update();
    }

    function initReveal() {
        document.documentElement.classList.add("mb-js");
        const targets = $$("[data-animate]");
        if (!targets.length) return;
        const revealAll = () => targets.forEach((el) => el.classList.add("mb-visible"));
        if ("IntersectionObserver" in window) {
            const io = new IntersectionObserver(
                (entries) => {
                    entries.forEach((e) => {
                        if (e.isIntersecting) {
                            e.target.classList.add("mb-visible");
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
        if (window.gsap && window.ScrollTrigger) {
            window.gsap.registerPlugin(window.ScrollTrigger);
        }
    }

    async function loadData() {
        try {
            const res = await fetch("/milo-bench-samples/api/dataset", { headers: { Accept: "application/json" } });
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
            console.error("Milobench: dataset load failed", e);
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
        setNum("codebase_count", s.codebase_count);
        setNum("pass_rate_overall", `${(s.pass_rate_overall || 0).toFixed(1)}%`);
    }

    function renderTiers() {
        const s = state.summary;
        const host = $("#mb-tier-grid");
        if (!s || !host) return;
        const parts = TIER_ORDER.map((tier) => {
            const t = (s.tiers || {})[tier] || { task_count: 0, run_count: 0, mean_score: 0, pass_rate: 0 };
            return `
                <div class="tier-card" data-tier="${tier}">
                    <h3>${tier}</h3>
                    <div class="tier-count">${t.task_count}</div>
                    <div class="tier-count-label">tasks</div>
                    <div class="tier-stat"><span class="tier-stat-label">Runs</span><span class="tier-stat-value">${t.run_count}</span></div>
                    <div class="tier-stat"><span class="tier-stat-label">Mean score</span><span class="tier-stat-value">${(t.mean_score || 0).toFixed(1)}%</span></div>
                    <div class="tier-stat"><span class="tier-stat-label">Pass rate</span><span class="tier-stat-value">${(t.pass_rate || 0).toFixed(1)}%</span></div>
                </div>`;
        });
        host.innerHTML = parts.join("");
    }

    function renderModelTable() {
        const s = state.summary;
        const tbody = $("#mb-model-table tbody");
        if (!s || !tbody) return;
        tbody.innerHTML = (s.models || [])
            .map(
                (m) => `
                <tr>
                    <td><span class="color-dot" style="background:${escapeHtml(m.color)}"></span><strong>${escapeHtml(m.display_name)}</strong></td>
                    <td>${escapeHtml(m.provider || "—")}</td>
                    <td class="num">${m.run_count}</td>
                    <td class="num">${(m.pass_at_3 || 0).toFixed(1)}%</td>
                    <td class="num">${(m.mean_score || 0).toFixed(1)}%</td>
                    <td class="num">$${(m.total_cost_usd || 0).toFixed(2)}</td>
                </tr>`
            )
            .join("");
    }

    function renderMatrix(tableId, field, isMoney = false) {
        const s = state.summary;
        const table = $("#" + tableId);
        if (!s || !table) return;
        const thead = table.querySelector("thead tr");
        const tbody = table.querySelector("tbody");
        thead.innerHTML = "<th>Tier</th>" + state.modelKeys.map((k) => `<th class="num">${escapeHtml(state.modelLabels[k])}</th>`).join("");
        tbody.innerHTML = TIER_ORDER.map((tier) => {
            const t = (s.tiers || {})[tier] || {};
            const cells = state.modelKeys
                .map((k) => {
                    const v = ((t.per_model || {})[k] || {})[field] || 0;
                    const display = isMoney ? `$${v.toFixed(2)}` : `${v.toFixed(1)}%`;
                    return `<td class="num">${display}</td>`;
                })
                .join("");
            return `<tr><td><span class="tier-badge" data-tier="${tier}">${tier}</span></td>${cells}</tr>`;
        }).join("");
    }

    function renderLangGrid() {
        const s = state.summary;
        const host = $("#mb-lang-grid");
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

    function populateLangFilter() {
        const sel = $("#mb-filter-lang");
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
        const tbody = $("#mb-viewer-table tbody");
        const count = $("#mb-viewer-count");
        const pagination = $("#mb-pagination");
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
                    <td>${escapeHtml(t.codebase || "—")}</td>
                    <td><span class="lang-badge">${escapeHtml(LANG_LABELS[t.language] || t.language || "—")}</span></td>
                    <td><span class="tier-badge" data-tier="${escapeHtml(t.difficulty)}">${escapeHtml(t.difficulty)}</span></td>
                    <td class="num">${t.src_hunks || 0}</td>
                    <td class="num">${passBarHtml(t.pass_rate)}</td>
                    <td class="num">${passBarHtml(t.mean_score)}</td>
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
            const passCount = mruns.filter((r) => r.score_binary).length;
            const meanScore = mruns.length ? (mruns.reduce((a, r) => a + r.score, 0) / mruns.length) * 100 : 0;
            const meanCost = mruns.length ? mruns.reduce((a, r) => a + r.cost_usd, 0) / mruns.length : 0;
            const runRows = mruns
                .map((r) => {
                    const pct = (r.score * 100).toFixed(1);
                    const cls = r.score_binary ? "detail-run-pass" : "detail-run-fail";
                    const label = r.score_binary ? "PASS" : `${pct}%`;
                    return `<div class="detail-run"><span class="detail-run-label">run_${r.run_number}</span><span class="detail-run-value ${cls}">${label}</span></div>`;
                })
                .join("");
            return `
                <div class="detail-block">
                    <div class="detail-model-name">${escapeHtml(state.modelLabels[mk] || mk)}</div>
                    ${runRows}
                    <div class="detail-summary"><span>Pass@3</span><span>${passCount}/${mruns.length || 0}</span></div>
                    <div class="detail-summary"><span>Mean score</span><span>${meanScore.toFixed(1)}%</span></div>
                    <div class="detail-summary"><span>Mean cost</span><span>$${meanCost.toFixed(2)}</span></div>
                </div>`;
        }).join("");
        const links = [
            task.dataset_url ? `<a href="${escapeHtml(task.dataset_url)}" target="_blank">Dataset</a>` : "",
            task.trajectories_url ? `<a href="${escapeHtml(task.trajectories_url)}" target="_blank">Trajectories</a>` : "",
            task.instruction_url ? `<a href="${escapeHtml(task.instruction_url)}" target="_blank">instruction.md</a>` : "",
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
        const host = $("#mb-pagination");
        if (!host) return;
        if (totalPages <= 1) { host.innerHTML = ""; return; }
        const parts = [];
        parts.push(`<button type="button" data-page="prev" ${state.page === 1 ? "disabled" : ""}>‹</button>`);
        paginationRange(state.page, totalPages).forEach((p) => {
            if (p === "…") parts.push('<span class="ellipsis">…</span>');
            else parts.push(`<button type="button" data-page="${p}" class="${p === state.page ? "is-active" : ""}">${p}</button>`);
        });
        parts.push(`<button type="button" data-page="next" ${state.page === totalPages ? "disabled" : ""}>›</button>`);
        host.innerHTML = parts.join("");
    }

    function attachViewerHandlers() {
        const search = $("#mb-search");
        const tier = $("#mb-filter-tier");
        const lang = $("#mb-filter-lang");
        const sortBy = $("#mb-sort-by");
        const sortDir = $("#mb-sort-dir");
        const table = $("#mb-viewer-table");
        const pagination = $("#mb-pagination");

        if (search) search.addEventListener("input", debounce((e) => { state.search = e.target.value; applyFilters(); }, 200));
        if (tier) tier.addEventListener("change", (e) => { state.tier = e.target.value; applyFilters(); });
        if (lang) lang.addEventListener("change", (e) => { state.lang = e.target.value; applyFilters(); });
        if (sortBy) sortBy.addEventListener("change", (e) => { state.sortBy = e.target.value; applyFilters(); });
        if (sortDir) sortDir.addEventListener("click", () => {
            state.sortDir = state.sortDir * -1;
            sortDir.textContent = state.sortDir === 1 ? "↑" : "↓";
            applyFilters();
        });

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

    async function init() {
        initTheme();
        initScrollProgress();
        initReveal();
        await loadData();
        renderKpi();
        renderTiers();
        renderModelTable();
        renderMatrix("mb-passrate-table", "pass_rate");
        renderMatrix("mb-meanscore-table", "mean_score");
        renderLangGrid();
        populateLangFilter();
        attachViewerHandlers();
        applyFilters();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
