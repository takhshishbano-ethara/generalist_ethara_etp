(function () {
    "use strict";

    function initScrollAnimations() {
        var elements = document.querySelectorAll("[data-animate]");
        if (!elements.length) return;

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        entry.target.classList.add("au-visible");
                        observer.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.1, rootMargin: "0px 0px -40px 0px" }
        );

        elements.forEach(function (el) {
            observer.observe(el);
        });
    }

    function initLightbox() {
        var portal = document.getElementById("au-portal");
        if (!portal) return;

        portal.addEventListener("click", function (e) {
            var img = e.target.closest(".au-chart-card img");
            if (!img) return;

            var overlay = document.createElement("div");
            overlay.className = "au-lightbox";

            var inner = document.createElement("div");
            inner.className = "au-lightbox-inner";

            var closeBtn = document.createElement("button");
            closeBtn.className = "au-lightbox-close";
            closeBtn.textContent = "\u00D7";

            var bigImg = document.createElement("img");
            bigImg.src = img.src;
            bigImg.alt = img.alt || "";

            inner.appendChild(closeBtn);
            inner.appendChild(bigImg);
            overlay.appendChild(inner);
            document.body.appendChild(overlay);

            function closeLightbox() {
                overlay.remove();
            }

            closeBtn.addEventListener("click", closeLightbox);
            overlay.addEventListener("click", function (ev) {
                if (ev.target === overlay) closeLightbox();
            });
            document.addEventListener(
                "keydown",
                function onEsc(ev) {
                    if (ev.key === "Escape") {
                        closeLightbox();
                        document.removeEventListener("keydown", onEsc);
                    }
                }
            );
        });
    }

    function initCountUp() {
        var nums = document.querySelectorAll(
            ".au-metric-val, .au-quality-num, .au-confidence-big"
        );
        if (!nums.length) return;

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (!entry.isIntersecting) return;

                    var el = entry.target;
                    observer.unobserve(el);

                    var raw = el.textContent.trim();
                    var hasPercent = raw.indexOf("%") !== -1;
                    var hasComma = raw.indexOf(",") !== -1;
                    var cleaned = raw.replace(/[,%]/g, "");
                    var target = parseFloat(cleaned);

                    if (isNaN(target) || target === 0) return;

                    var isFloat = cleaned.indexOf(".") !== -1;
                    var duration = 800;
                    var start = performance.now();

                    function step(now) {
                        var elapsed = now - start;
                        var progress = Math.min(elapsed / duration, 1);
                        var eased = 1 - Math.pow(1 - progress, 3);
                        var current = target * eased;

                        var display;
                        if (isFloat) {
                            display = current.toFixed(1);
                        } else {
                            display = Math.round(current).toString();
                        }

                        if (hasComma) {
                            display = Number(display).toLocaleString("en-US");
                        }
                        if (hasPercent) {
                            display += "%";
                        }

                        el.textContent = display;

                        if (progress < 1) {
                            requestAnimationFrame(step);
                        }
                    }

                    requestAnimationFrame(step);
                });
            },
            { threshold: 0.3 }
        );

        nums.forEach(function (el) {
            observer.observe(el);
        });
    }

    function langLabel(lang) {
        var map = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", go: "Go", rust: "Rust", java: "Java", cpp: "C++", c: "C" };
        return map[lang] || lang;
    }

    function langClass(lang) {
        var l = (lang || "").toLowerCase().replace(/[^a-z]/g, "");
        if (l === "python") return "au-lang-python";
        if (l === "javascript") return "au-lang-javascript";
        if (l === "typescript") return "au-lang-typescript";
        if (l === "go") return "au-lang-go";
        if (l === "rust") return "au-lang-rust";
        if (l === "java") return "au-lang-java";
        if (l === "c" || l === "cpp") return "au-lang-cpp";
        return "";
    }

    function esc(str) {
        var d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
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

    function debounce(fn, delay) {
        var timer;
        return function () {
            var ctx = this;
            var args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function () {
                fn.apply(ctx, args);
            }, delay);
        };
    }

    function initDarkMode() {
        var portal = document.getElementById("au-portal");
        var toggle = document.getElementById("au-theme-toggle");
        if (!portal || !toggle) return;

        var STORAGE_KEY = "au-dark-mode";
        var DARK_BG = "#0f172a";
        var LIGHT_BG = "#ffffff";

        function nukePortalChrome() {
            var selectors = [
                "#wrapwrap", ".o_portal", ".o_portal_wrap", "main",
                ".oe_website_login_container"
            ];
            for (var s = 0; s < selectors.length; s++) {
                var el = document.querySelector(selectors[s]);
                if (el) {
                    el.style.cssText += "max-width:100%!important;width:100%!important;padding:0!important;margin:0!important;background:transparent!important;";
                }
            }
            var containers = document.querySelectorAll(
                "#wrapwrap .container, #wrapwrap .container-fluid, " +
                ".o_portal .container, .o_portal_wrap .container, " +
                "main > .container, main > .container-fluid"
            );
            for (var i = 0; i < containers.length; i++) {
                containers[i].style.cssText += "max-width:100%!important;width:100%!important;padding:0!important;margin:0!important;background:transparent!important;";
            }
            var odooFooters = document.querySelectorAll("footer:not(.au-footer), .o_footer, #footer, .o_footer_copyright");
            for (var j = 0; j < odooFooters.length; j++) {
                odooFooters[j].style.display = "none";
            }
            var header = document.querySelector("header");
            if (header) header.style.display = "none";
        }

        function applyTheme(isDark) {
            if (isDark) {
                portal.classList.add("au-dark");
                document.body.classList.add("au-dark-body");
            } else {
                portal.classList.remove("au-dark");
                document.body.classList.remove("au-dark-body");
            }
            document.body.style.backgroundColor = isDark ? DARK_BG : LIGHT_BG;
            nukePortalChrome();
        }

        function getPreference() {
            var stored = localStorage.getItem(STORAGE_KEY);
            if (stored !== null) return stored === "true";
            return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
        }

        applyTheme(getPreference());

        toggle.addEventListener("click", function () {
            var isDark = portal.classList.contains("au-dark");
            var newState = !isDark;
            applyTheme(newState);
            localStorage.setItem(STORAGE_KEY, String(newState));
        });

        if (window.matchMedia) {
            window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (e) {
                if (localStorage.getItem(STORAGE_KEY) === null) {
                    applyTheme(e.matches);
                }
            });
        }
    }

    function init() {
        initDarkMode();
        initScrollAnimations();
        initLightbox();
        initCountUp();
        initEvalViewer();
    }

    /* ═══════════════════════════════════════════════════════════════
       DELIVERY EVALUATION VIEWER
       ═══════════════════════════════════════════════════════════════ */

    var EVAL_PER_PAGE = 10;
    var evalAllData = [];
    var evalFilteredData = [];
    var evalCurrentPage = 1;
    var evalCurrentSort = "claude_pass";
    var evalCurrentSortDir = -1;
    var evalExpandedId = null;

    function evalParsePass(str) {
        if (!str) return 0;
        return parseFloat(str.replace("%", "")) || 0;
    }

    function evalBarColor(pct) {
        if (pct >= 60) return "var(--au-success)";
        if (pct >= 30) return "var(--au-warning)";
        if (pct > 0) return "var(--au-danger)";
        return "var(--au-text-muted)";
    }

    function evalPassCell(passStr) {
        var pct = evalParsePass(passStr);
        var color = evalBarColor(pct);
        return (
            '<div class="au-eval-pass-cell">' +
                '<span class="au-eval-pass-pct" style="color:' + color + '">' + (passStr || "0.00%") + '</span>' +
                '<div class="au-eval-pass-bar">' +
                    '<div class="au-eval-pass-fill" style="width:' + pct + '%;background:' + color + '"></div>' +
                '</div>' +
            '</div>'
        );
    }

    function evalRunBadge(result) {
        if (!result) return '<span class="au-eval-run au-eval-run-na">—</span>';
        var cls = result === "Pass" ? "au-eval-run-pass" : "au-eval-run-fail";
        return '<span class="au-eval-run ' + cls + '">' + esc(result) + '</span>';
    }

    function evalRenderRow(d) {
        var isExpanded = evalExpandedId === d.instance_id;
        var claude = d.models && d.models["Claude Opus 4.6"] ? d.models["Claude Opus 4.6"] : {};
        var glm = d.models && d.models["GLM 5"] ? d.models["GLM 5"] : {};
        var kimi = d.models && d.models["Kimi K2.5"] ? d.models["Kimi K2.5"] : {};

        return (
            '<tr class="' + (isExpanded ? "au-row-expanded" : "") + '" data-eval-id="' + esc(d.instance_id) + '">' +
                '<td class="au-etd-instance">' +
                    '<span class="au-eval-instance-name">' + esc(d.instance_id) + '</span>' +
                '</td>' +
                '<td class="au-etd-prrange"><span class="au-eval-prrange-badge">' + esc(d.pr_range || "N/A") + '</span></td>' +
                '<td class="au-etd-lang"><span class="au-lang-badge ' + langClass(d.language) + '">' + esc(d.language || "N/A") + '</span></td>' +
                '<td class="au-etd-claude">' + evalPassCell(claude.pass_at_3) + '</td>' +
                '<td class="au-etd-glm">' + evalPassCell(glm.pass_at_3) + '</td>' +
                '<td class="au-etd-kimi">' + evalPassCell(kimi.pass_at_3) + '</td>' +
                '<td class="au-etd-repo">' +
                    (d.repo_url ? '<a class="au-pr-link" href="' + esc(d.repo_url) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">View</a>' : '<span class="au-text-muted">—</span>') +
                '</td>' +
                '<td class="au-etd-expand"><span class="au-expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
            '</tr>'
        );
    }

    function evalRenderDetailRow(d) {
        var claude = d.models && d.models["Claude Opus 4.6"] ? d.models["Claude Opus 4.6"] : {};
        var glm = d.models && d.models["GLM 5"] ? d.models["GLM 5"] : {};
        var kimi = d.models && d.models["Kimi K2.5"] ? d.models["Kimi K2.5"] : {};

        function modelBlock(name, m) {
            return (
                '<div class="au-detail-block">' +
                    '<div class="au-detail-block-title">' + esc(name) + '</div>' +
                    '<div class="au-detail-row-item"><span class="au-detail-key">Run 1</span><span class="au-detail-val">' + evalRunBadge(m.run_1) + '</span></div>' +
                    '<div class="au-detail-row-item"><span class="au-detail-key">Run 2</span><span class="au-detail-val">' + evalRunBadge(m.run_2) + '</span></div>' +
                    '<div class="au-detail-row-item"><span class="au-detail-key">Run 3</span><span class="au-detail-val">' + evalRunBadge(m.run_3) + '</span></div>' +
                    '<div class="au-detail-row-item"><span class="au-detail-key">Pass@3</span><span class="au-detail-val" style="font-weight:700">' + esc(m.pass_at_3 || "0.00%") + '</span></div>' +
                    (m.trajectory ? '<div class="au-detail-row-item"><span class="au-detail-key">Trajectory</span><span class="au-detail-val"><a class="au-detail-link" href="' + esc(m.trajectory) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">View</a></span></div>' : '') +
                '</div>'
            );
        }

        return (
            '<tr class="au-detail-row" data-eval-detail-for="' + esc(d.instance_id) + '">' +
                '<td colspan="8">' +
                    '<div class="au-detail-content">' +
                        '<div class="au-eval-detail-grid">' +
                            '<div class="au-detail-block">' +
                                '<div class="au-detail-block-title">Instance Info</div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">Task ID</span><span class="au-detail-val">' + esc(d.task_id || d.instance_id) + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">Category</span><span class="au-detail-val">' + esc((d.category || "").replace(/_/g, " ")) + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">PR Range</span><span class="au-detail-val">' + esc(d.pr_range || "N/A") + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">Avg Files Modified</span><span class="au-detail-val">' + (d.avg_files_modified || 0).toFixed(1) + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">Avg Tool Calls</span><span class="au-detail-val">' + (d.avg_tool_calls || 0).toFixed(1) + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">Avg Turns</span><span class="au-detail-val">' + (d.avg_turns || 0).toFixed(1) + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">Est. Time (min)</span><span class="au-detail-val">' + (d.estimated_time || 0).toFixed(1) + '</span></div>' +
                                '<div class="au-detail-row-item"><span class="au-detail-key">PRs</span><span class="au-detail-val">' + ((d.pr_urls || []).length) + '</span></div>' +
                            '</div>' +
                            modelBlock("Claude Opus 4.6", claude) +
                            modelBlock("GLM 5", glm) +
                            modelBlock("Kimi K2.5", kimi) +
                        '</div>' +
                        (d.repo_url ? '<div class="au-detail-links"><a class="au-detail-link" href="' + esc(d.repo_url) + '" target="_blank" rel="noopener"><svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>View on GitHub</a></div>' : '') +
                    '</div>' +
                '</td>' +
            '</tr>'
        );
    }

    function evalRenderTable() {
        var tbody = document.getElementById("au-eval-tbody");
        if (!tbody) return;

        var start = (evalCurrentPage - 1) * EVAL_PER_PAGE;
        var pageData = evalFilteredData.slice(start, start + EVAL_PER_PAGE);
        var html = "";

        for (var i = 0; i < pageData.length; i++) {
            html += evalRenderRow(pageData[i]);
            if (evalExpandedId === pageData[i].instance_id) {
                html += evalRenderDetailRow(pageData[i]);
            }
        }

        if (pageData.length === 0) {
            html = '<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--au-text-muted)">No instances found.</td></tr>';
        }

        tbody.innerHTML = html;

        var countEl = document.getElementById("au-eval-count");
        if (countEl) {
            countEl.textContent = evalFilteredData.length + " of " + evalAllData.length + " instances";
        }

        evalRenderPagination();
    }

    function evalRenderPagination() {
        var container = document.getElementById("au-eval-pagination");
        if (!container) return;

        var totalPages = Math.max(1, Math.ceil(evalFilteredData.length / EVAL_PER_PAGE));
        if (totalPages <= 1) {
            container.innerHTML = "";
            return;
        }

        var html = "";
        html += '<button class="au-page-btn" data-eval-page="' + (evalCurrentPage - 1) + '"' + (evalCurrentPage <= 1 ? " disabled" : "") + '>&lsaquo; Prev</button>';

        var pages = paginationRange(evalCurrentPage, totalPages);
        for (var i = 0; i < pages.length; i++) {
            var p = pages[i];
            if (p === "...") {
                html += '<span class="au-page-ellipsis">&hellip;</span>';
            } else {
                html += '<button class="au-page-btn' + (p === evalCurrentPage ? " au-page-active" : "") + '" data-eval-page="' + p + '">' + p + '</button>';
            }
        }

        html += '<button class="au-page-btn" data-eval-page="' + (evalCurrentPage + 1) + '"' + (evalCurrentPage >= totalPages ? " disabled" : "") + '>Next &rsaquo;</button>';

        container.innerHTML = html;
    }

    function evalApplyFilters() {
        var search = (document.getElementById("au-eval-search").value || "").toLowerCase();
        var lang = document.getElementById("au-eval-language").value;

        evalFilteredData = evalAllData.filter(function (d) {
            if (lang && d.language !== lang) return false;
            if (search) {
                var idMatch = (d.instance_id || "").toLowerCase().indexOf(search) !== -1;
                var taskMatch = (d.task_id || "").toLowerCase().indexOf(search) !== -1;
                var repoMatch = (d.repo_url || "").toLowerCase().indexOf(search) !== -1;
                if (!idMatch && !taskMatch && !repoMatch) return false;
            }
            return true;
        });

        evalSortData();
        evalCurrentPage = 1;
        evalExpandedId = null;
        evalRenderTable();
    }

    function evalSortData() {
        var key = evalCurrentSort;
        var dir = evalCurrentSortDir;

        evalFilteredData.sort(function (a, b) {
            var av, bv;

            if (key === "claude_pass") {
                av = evalParsePass(a.models && a.models["Claude Opus 4.6"] ? a.models["Claude Opus 4.6"].pass_at_3 : "0");
                bv = evalParsePass(b.models && b.models["Claude Opus 4.6"] ? b.models["Claude Opus 4.6"].pass_at_3 : "0");
                return (av - bv) * dir;
            }
            if (key === "glm_pass") {
                av = evalParsePass(a.models && a.models["GLM 5"] ? a.models["GLM 5"].pass_at_3 : "0");
                bv = evalParsePass(b.models && b.models["GLM 5"] ? b.models["GLM 5"].pass_at_3 : "0");
                return (av - bv) * dir;
            }
            if (key === "kimi_pass") {
                av = evalParsePass(a.models && a.models["Kimi K2.5"] ? a.models["Kimi K2.5"].pass_at_3 : "0");
                bv = evalParsePass(b.models && b.models["Kimi K2.5"] ? b.models["Kimi K2.5"].pass_at_3 : "0");
                return (av - bv) * dir;
            }
            if (key === "pr_range") {
                var prOrder = {"1-5": 1, "6-10": 2, "11-20": 3, "21-40": 4, "41-100": 5, "100+": 6};
                av = prOrder[a.pr_range] || 99;
                bv = prOrder[b.pr_range] || 99;
                return (av - bv) * dir;
            }

            av = (a[key] || "").toString().toLowerCase();
            bv = (b[key] || "").toString().toLowerCase();
            if (av < bv) return -1 * dir;
            if (av > bv) return 1 * dir;
            return 0;
        });
    }

    function initEvalViewer() {
        var tbody = document.getElementById("au-eval-tbody");
        if (!tbody) return;

        fetch("/aurora/api/delivery-evaluation")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                evalAllData = data;
                evalFilteredData = data.slice();
                evalSortData();
                evalRenderTable();
            })
            .catch(function () {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--au-text-muted)">Failed to load evaluation data.</td></tr>';
            });

        document.getElementById("au-eval-search").addEventListener(
            "input",
            debounce(evalApplyFilters, 250)
        );

        document.getElementById("au-eval-language").addEventListener(
            "change",
            evalApplyFilters
        );

        document.getElementById("au-eval-sort").addEventListener(
            "change",
            function () {
                if (evalCurrentSort === this.value) {
                    evalCurrentSortDir = evalCurrentSortDir * -1;
                } else {
                    evalCurrentSort = this.value;
                    evalCurrentSortDir = 1;
                }
                evalUpdateSortDirBtn();
                evalApplyFilters();
            }
        );

        var sortDirBtn = document.getElementById("au-eval-sort-dir-btn");
        if (sortDirBtn) {
            sortDirBtn.addEventListener("click", function () {
                evalCurrentSortDir = evalCurrentSortDir * -1;
                evalUpdateSortDirBtn();
                evalApplyFilters();
            });
        }

        function evalUpdateSortDirBtn() {
            var btn = document.getElementById("au-eval-sort-dir-btn");
            if (!btn) return;
            btn.textContent = evalCurrentSortDir === 1 ? "\u2191" : "\u2193";
            btn.title = evalCurrentSortDir === 1 ? "Ascending \u2013 click to reverse" : "Descending \u2013 click to reverse";
        }

        var tableWrap = document.getElementById("au-eval-table-wrap");
        if (tableWrap) {
            tableWrap.addEventListener("click", function (e) {
                var row = e.target.closest("tr[data-eval-id]");
                if (!row) return;
                var id = row.getAttribute("data-eval-id");
                if (evalExpandedId === id) {
                    evalExpandedId = null;
                } else {
                    evalExpandedId = id;
                }
                evalRenderTable();
            });
        }

        var pagination = document.getElementById("au-eval-pagination");
        if (pagination) {
            pagination.addEventListener("click", function (e) {
                var btn = e.target.closest("[data-eval-page]");
                if (!btn || btn.disabled) return;
                var page = parseInt(btn.getAttribute("data-eval-page"), 10);
                var totalPages = Math.ceil(evalFilteredData.length / EVAL_PER_PAGE);
                if (page >= 1 && page <= totalPages) {
                    evalCurrentPage = page;
                    evalExpandedId = null;
                    evalRenderTable();
                    var viewer = document.getElementById("au-eval-viewer");
                    if (viewer) {
                        viewer.scrollIntoView({ behavior: "smooth", block: "start" });
                    }
                }
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
