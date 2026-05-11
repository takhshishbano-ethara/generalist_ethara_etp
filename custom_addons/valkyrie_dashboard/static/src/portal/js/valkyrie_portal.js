(function () {
    "use strict";

    var ROWS_PER_PAGE = 20;
    var allData = [];
    var filteredData = [];
    var currentPage = 1;
    var currentSort = "instance_id";
    var currentSortDir = 1;
    var expandedId = null;

    // ========================================================
    // Theme — data-theme attribute on <html>, persisted to
    // localStorage (key: vk:theme). Matches the FOUC script in
    // the template head (which sets the attribute before CSS
    // resolves so there's no flash on repeat visits).
    // ========================================================
    function initTheme() {
        var root = document.documentElement;
        var toggle = document.getElementById("vk-theme-toggle");
        if (!toggle) return;

        var KEY = "vk:theme";
        var prefersDark = window.matchMedia("(prefers-color-scheme: dark)");

        function currentTheme() {
            var explicit = root.getAttribute("data-theme");
            if (explicit === "light" || explicit === "dark") return explicit;
            return "dark";
        }

        function syncLabel() {
            var theme = currentTheme();
            toggle.setAttribute("aria-pressed", String(theme === "dark"));
            toggle.setAttribute(
                "aria-label",
                theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
            );
        }

        syncLabel();

        toggle.addEventListener("click", function () {
            var next = currentTheme() === "dark" ? "light" : "dark";
            root.setAttribute("data-theme", next);
            try { localStorage.setItem(KEY, next); } catch (e) {}
            syncLabel();
        });

        // Follow OS changes unless the user overrode.
        function osChange() {
            try { if (localStorage.getItem(KEY)) return; } catch (e) {}
            syncLabel();
        }
        if (prefersDark.addEventListener) {
            prefersDark.addEventListener("change", osChange);
        } else if (prefersDark.addListener) {
            prefersDark.addListener(osChange);
        }
    }

    // ========================================================
    // Scroll progress rail — rAF-throttled width update, passive
    // listeners so we never block scroll.
    // ========================================================
    function initScrollProgress() {
        var bar = document.querySelector(".vk-scroll-progress");
        if (!bar) return;
        var ticking = false;
        function update() {
            var doc = document.documentElement;
            var max = (doc.scrollHeight - window.innerHeight) || 1;
            var progress = Math.max(0, Math.min(1, window.scrollY / max));
            bar.style.width = (progress * 100).toFixed(2) + "%";
            ticking = false;
        }
        function onScroll() {
            if (ticking) return;
            ticking = true;
            requestAnimationFrame(update);
        }
        window.addEventListener("scroll", onScroll, { passive: true });
        window.addEventListener("resize", onScroll, { passive: true });
        update();
    }

    // ========================================================
    // Thesis mask reveal — split sentence into per-word clipped
    // spans, yPercent 110 → 0 on load. Degrades to flat text if
    // reduced-motion is on.
    // ========================================================
    function initThesisReveal() {
        var el = document.querySelector(".vk-thesis");
        if (!el) return;
        var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (reduced) return;

        // Preserve italic on the em word ("shipped") by detecting
        // it during the split. Trailing punctuation (?) stays roman.
        var raw = el.textContent.trim();
        var words = raw.split(/\s+/);
        el.innerHTML = words.map(function (w) {
            var m = w.match(/^(.*?)([.,!?;:]+)$/);
            var core = m ? m[1] : w;
            var punct = m ? m[2] : "";
            var isEm = core.toLowerCase() === "exploit";
            var innerMarkup = isEm
                ? '<span class="vk-thesis-em">' + core + '</span>' + punct
                : core + punct;
            return (
                '<span class="vk-thesis-word" style="display:inline-block;overflow:hidden;vertical-align:baseline;padding:0.1em 0 0.25em;margin:-0.1em 0 -0.25em">' +
                    '<span class="vk-thesis-word-inner" style="display:inline-block;will-change:transform,opacity;transform:translateY(110%);opacity:0">' +
                        innerMarkup +
                    '</span>' +
                '</span>'
            );
        }).join(" ");

        // Stagger the reveal on the next frame (so layout settles first).
        var innerEls = el.querySelectorAll(".vk-thesis-word-inner");
        requestAnimationFrame(function () {
            for (var i = 0; i < innerEls.length; i++) {
                (function (inner, i) {
                    inner.style.transition =
                        "transform 640ms cubic-bezier(0.28, 0.11, 0.32, 1) " + (i * 40) + "ms, " +
                        "opacity 640ms cubic-bezier(0.28, 0.11, 0.32, 1) " + (i * 40) + "ms";
                    inner.style.transform = "translateY(0)";
                    inner.style.opacity = "1";
                })(innerEls[i], i);
            }
        });
    }

    // ========================================================
    // Section fade-up on scroll — IntersectionObserver, one-shot.
    // ========================================================
    function initSectionReveal() {
        var sections = document.querySelectorAll(".vk-section");
        if (!sections.length || !window.IntersectionObserver) return;
        var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (reduced) return;

        sections.forEach(function (s) {
            s.style.opacity = "0";
            s.style.transform = "translateY(16px)";
            s.style.transition =
                "opacity 640ms cubic-bezier(0.28, 0.11, 0.32, 1), " +
                "transform 640ms cubic-bezier(0.28, 0.11, 0.32, 1)";
        });

        var obs = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (!e.isIntersecting) return;
                e.target.style.opacity = "1";
                e.target.style.transform = "translateY(0)";
                obs.unobserve(e.target);
            });
        }, { threshold: 0.08, rootMargin: "0px 0px -40px 0px" });

        sections.forEach(function (s) { obs.observe(s); });
    }

    // ========================================================
    // Chart lightbox — uses the static #vk-lightbox overlay
    // baked into the template. Delegates clicks on
    // .vk-chart-trigger buttons.
    // ========================================================
    function initLightbox() {
        var overlay = document.getElementById("vk-lightbox");
        var closeBtn = document.getElementById("vk-lightbox-close");
        var imgEl = document.getElementById("vk-lightbox-img");
        var captionEl = document.getElementById("vk-lightbox-caption");
        if (!overlay || !closeBtn || !imgEl) return;

        function open(src, alt, caption) {
            imgEl.src = src;
            imgEl.alt = alt || "";
            if (captionEl) captionEl.textContent = caption || "";
            overlay.hidden = false;
            overlay.setAttribute("aria-hidden", "false");
            // force reflow then set data-open for opacity transition
            void overlay.offsetWidth;
            overlay.setAttribute("data-open", "true");
            document.body.style.overflow = "hidden";
        }
        function close() {
            overlay.removeAttribute("data-open");
            overlay.setAttribute("aria-hidden", "true");
            document.body.style.overflow = "";
            setTimeout(function () {
                if (overlay.getAttribute("data-open") !== "true") {
                    overlay.hidden = true;
                    imgEl.src = "";
                }
            }, 400);
        }

        document.addEventListener("click", function (e) {
            var trigger = e.target.closest(".vk-chart-trigger");
            if (!trigger) return;
            var imgs = trigger.querySelectorAll("img");
            var img = null;
            for (var i = 0; i < imgs.length; i++) {
                if (window.getComputedStyle(imgs[i]).display !== "none") { img = imgs[i]; break; }
            }
            if (!img) return;
            var fig = trigger.closest("figure");
            var caption = fig ? (fig.querySelector("figcaption") || {}).textContent : "";
            open(img.src, img.alt, caption || "");
        });

        closeBtn.addEventListener("click", close);
        overlay.addEventListener("click", function (e) {
            if (e.target === overlay) close();
        });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && overlay.getAttribute("data-open") === "true") close();
        });
    }

    // ========================================================
    // Dataset viewer — row-expand table with search, filters,
    // sort, pagination. Data shape matches valkyrie_instances.json.
    // ========================================================
    function diffClass(d) {
        if (d === "Easy")   return "vk-diff-easy";
        if (d === "Medium") return "vk-diff-medium";
        if (d === "Hard")   return "vk-diff-hard";
        return "";
    }

    function esc(str) {
        var d = document.createElement("div");
        d.textContent = str == null ? "" : String(str);
        return d.innerHTML;
    }

    function passBadge(val) {
        if (val === "Pass") {
            return '<span class="vk-pass-badge vk-pass-badge-pass">Pass</span>';
        }
        return '<span class="vk-pass-badge vk-pass-badge-fail">Fail</span>';
    }

    function cweDisplay(vulnTypes) {
        if (!vulnTypes || !vulnTypes.length) return "";
        return vulnTypes.join(", ");
    }

    function renderRow(d) {
        var isExpanded = expandedId === d.instance_id;
        return (
            '<tr class="vk-matrix-row ' + (isExpanded ? "vk-row-expanded" : "") + '" data-id="' + esc(d.instance_id) + '">' +
                '<td class="vk-td-instance">' +
                    '<span class="vk-viewer-instance-name">' + esc(d.instance_id) + '</span>' +
                '</td>' +
                '<td class="vk-td-diff"><span class="vk-diff-badge ' + diffClass(d.difficulty) + '">' + esc(d.difficulty) + '</span></td>' +
                '<td class="vk-td-cwe">' + esc(cweDisplay(d.vulnerability_type)) + '</td>' +
                '<td class="vk-td-tests">' + esc(d.fail_to_pass_count) + '</td>' +
                '<td class="vk-td-kimi">' + passBadge(d.kimi_pass_at_1) + '</td>' +
                '<td class="vk-td-nova">' + passBadge(d.nova_pass_at_1) + '</td>' +
                '<td class="vk-td-expand"><span class="vk-expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
            '</tr>'
        );
    }

    function renderDetailRow(d) {
        var problem = (d.problem_statement || "").substring(0, 220);
        if ((d.problem_statement || "").length > 220) problem += "...";
        var repoUrl = d.repo ? "https://github.com/" + d.repo : "";

        return (
            '<tr class="vk-detail-row" data-detail-for="' + esc(d.instance_id) + '">' +
                '<td colspan="7">' +
                    '<div class="vk-detail-content">' +
                        '<div class="vk-detail-grid">' +
                            '<div class="vk-detail-block">' +
                                '<div class="vk-detail-block-title">Instance</div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Vulnerability</span><span class="vk-detail-val">' + esc(cweDisplay(d.vulnerability_type)) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Category</span><span class="vk-detail-val">' + esc(d.category || "") + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Files Affected</span><span class="vk-detail-val">' + esc(d.num_files_affected || "") + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">F2P Tests</span><span class="vk-detail-val">' + esc(d.fail_to_pass_count) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">P2P Tests</span><span class="vk-detail-val">' + esc(d.pass_to_pass_count) + '</span></div>' +
                                '<div class="vk-detail-row-item" style="flex-direction:column;gap:4px"><span class="vk-detail-key">Problem statement</span><span class="vk-detail-val" style="font-weight:400;font-size:12px;line-height:1.5;font-family:var(--vk-font-sans);text-align:left">' + esc(problem) + '</span></div>' +
                            '</div>' +
                            '<div class="vk-detail-block">' +
                                '<div class="vk-detail-block-title">Kimi K2.5</div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Result</span><span class="vk-detail-val">' + passBadge(d.kimi_pass_at_1) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Time (s)</span><span class="vk-detail-val">' + esc(parseFloat(d.kimi_time || 0).toFixed(1)) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Cost ($)</span><span class="vk-detail-val">' + esc(d.kimi_cost || "0") + '</span></div>' +
                            '</div>' +
                            '<div class="vk-detail-block">' +
                                '<div class="vk-detail-block-title">Nova 2 Lite</div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Result</span><span class="vk-detail-val">' + passBadge(d.nova_pass_at_1) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Time (s)</span><span class="vk-detail-val">' + esc(parseFloat(d.nova_time || 0).toFixed(1)) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Cost ($)</span><span class="vk-detail-val">' + esc(d.nova_cost || "0") + '</span></div>' +
                            '</div>' +
                        '</div>' +
                        (repoUrl ? '<div class="vk-detail-links"><a class="vk-detail-link" href="' + esc(repoUrl) + '" target="_blank" rel="noopener">Repository</a></div>' : '') +
                    '</div>' +
                '</td>' +
            '</tr>'
        );
    }

    function renderTable() {
        var tbody = document.getElementById("vk-viewer-tbody");
        if (!tbody) return;

        var start = (currentPage - 1) * ROWS_PER_PAGE;
        var pageData = filteredData.slice(start, start + ROWS_PER_PAGE);
        var html = "";

        for (var i = 0; i < pageData.length; i++) {
            html += renderRow(pageData[i]);
            if (expandedId === pageData[i].instance_id) {
                html += renderDetailRow(pageData[i]);
            }
        }

        if (pageData.length === 0) {
            html = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--vk-ink-3);font-family:var(--vk-font-sans)">No instances found.</td></tr>';
        }

        tbody.innerHTML = html;

        var countEl = document.getElementById("vk-viewer-count");
        if (countEl) {
            countEl.textContent =
                filteredData.length + " of " + allData.length + " instances";
        }

        renderPagination();
    }

    function renderPagination() {
        var container = document.getElementById("vk-viewer-pagination");
        if (!container) return;

        var totalPages = Math.max(1, Math.ceil(filteredData.length / ROWS_PER_PAGE));
        if (totalPages <= 1) {
            container.innerHTML = "";
            return;
        }

        var html = "";
        html += '<button class="vk-page-btn" data-page="' + (currentPage - 1) + '"' + (currentPage <= 1 ? " disabled" : "") + '>&lsaquo; Prev</button>';

        var pages = paginationRange(currentPage, totalPages);
        for (var i = 0; i < pages.length; i++) {
            var p = pages[i];
            if (p === "...") {
                html += '<span class="vk-page-ellipsis">&hellip;</span>';
            } else {
                html += '<button class="vk-page-btn' + (p === currentPage ? " vk-page-active" : "") + '" data-page="' + p + '">' + p + '</button>';
            }
        }

        html += '<button class="vk-page-btn" data-page="' + (currentPage + 1) + '"' + (currentPage >= totalPages ? " disabled" : "") + '>Next &rsaquo;</button>';

        container.innerHTML = html;
    }

    function paginationRange(current, total) {
        if (total <= 7) {
            var arr = [];
            for (var i = 1; i <= total; i++) arr.push(i);
            return arr;
        }
        var pages = [1];
        if (current > 3) pages.push("...");
        var rangeStart = Math.max(2, current - 1);
        var rangeEnd = Math.min(total - 1, current + 1);
        for (var j = rangeStart; j <= rangeEnd; j++) pages.push(j);
        if (current < total - 2) pages.push("...");
        pages.push(total);
        return pages;
    }

    function applyFilters() {
        var search = (document.getElementById("vk-viewer-search").value || "").toLowerCase();
        var diff = document.getElementById("vk-viewer-difficulty").value;

        filteredData = allData.filter(function (d) {
            if (diff && d.difficulty !== diff) return false;
            if (search) {
                var idMatch   = (d.instance_id || "").toLowerCase().indexOf(search) !== -1;
                var cweMatch  = (d.vulnerability_type || []).join(" ").toLowerCase().indexOf(search) !== -1;
                var repoMatch = (d.repo || "").toLowerCase().indexOf(search) !== -1;
                if (!idMatch && !cweMatch && !repoMatch) return false;
            }
            return true;
        });

        sortData();
        currentPage = 1;
        expandedId = null;
        renderTable();
    }

    function sortData() {
        var key = currentSort;
        var dir = currentSortDir;

        var numericKeys = {
            fail_to_pass_count: true,
            pass_to_pass_count: true,
        };

        filteredData.sort(function (a, b) {
            var av = a[key];
            var bv = b[key];

            if (numericKeys[key]) {
                av = parseFloat(av) || 0;
                bv = parseFloat(bv) || 0;
                return (av - bv) * dir;
            }

            if (key === "kimi_pass_at_1" || key === "nova_pass_at_1") {
                av = av === "Pass" ? 1 : 0;
                bv = bv === "Pass" ? 1 : 0;
                return (bv - av) * dir;  // passes first
            }

            if (key === "difficulty") {
                var order = { Easy: 1, Medium: 2, Hard: 3 };
                av = order[av] || 0;
                bv = order[bv] || 0;
                return (av - bv) * dir;
            }

            av = (av || "").toString().toLowerCase();
            bv = (bv || "").toString().toLowerCase();
            if (av < bv) return -1 * dir;
            if (av > bv) return 1 * dir;
            return 0;
        });
    }

    function initDatasetViewer() {
        var tbody = document.getElementById("vk-viewer-tbody");
        if (!tbody) return;

        fetch("/valkyrie/api/instances")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                allData = Array.isArray(data) ? data : [];
                filteredData = allData.slice();
                sortData();
                renderTable();
            })
            .catch(function () {
                tbody.innerHTML =
                    '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--vk-ink-3);font-family:var(--vk-font-sans)">Failed to load dataset.</td></tr>';
            });

        var search = document.getElementById("vk-viewer-search");
        if (search) {
            search.addEventListener("input", debounce(applyFilters, 250));
        }

        var diffSel = document.getElementById("vk-viewer-difficulty");
        if (diffSel) diffSel.addEventListener("change", applyFilters);

        var sortSel = document.getElementById("vk-viewer-sort");
        if (sortSel) {
            sortSel.addEventListener("change", function () {
                currentSort = this.value;
                currentSortDir = 1;
                applyFilters();
            });
        }

        var wrap = document.getElementById("vk-viewer-table-wrap");
        if (wrap) {
            wrap.addEventListener("click", function (e) {
                var row = e.target.closest("tr[data-id]");
                if (!row) return;
                var id = row.getAttribute("data-id");
                expandedId = (expandedId === id) ? null : id;
                renderTable();
            });
        }

        var pagination = document.getElementById("vk-viewer-pagination");
        if (pagination) {
            pagination.addEventListener("click", function (e) {
                var btn = e.target.closest("[data-page]");
                if (!btn || btn.disabled) return;
                var page = parseInt(btn.getAttribute("data-page"), 10);
                var totalPages = Math.ceil(filteredData.length / ROWS_PER_PAGE);
                if (page >= 1 && page <= totalPages) {
                    currentPage = page;
                    expandedId = null;
                    renderTable();
                    var section = document.getElementById("instances");
                    if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
                }
            });
        }
    }

    function debounce(fn, delay) {
        var timer;
        return function () {
            var ctx = this;
            var args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function () { fn.apply(ctx, args); }, delay);
        };
    }

    function init() {
        initTheme();
        initScrollProgress();
        initThesisReveal();
        initSectionReveal();
        initLightbox();
        initDatasetViewer();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
