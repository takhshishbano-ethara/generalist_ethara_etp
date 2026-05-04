(function () {
    "use strict";

    var ROWS_PER_PAGE = 20;
    var allData = [];
    var filteredData = [];
    var currentPage = 1;
    var currentSort = "instance_id";
    var currentSortDir = 1;
    var expandedId = null;

    function initScrollAnimations() {
        var elements = document.querySelectorAll("[data-animate]");
        if (!elements.length) return;

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        entry.target.classList.add("vk-visible");
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
        var portal = document.getElementById("vk-portal");
        if (!portal) return;

        portal.addEventListener("click", function (e) {
            var img = e.target.closest(".vk-chart-card img");
            if (!img) return;

            var overlay = document.createElement("div");
            overlay.className = "vk-lightbox";

            var inner = document.createElement("div");
            inner.className = "vk-lightbox-inner";

            var closeBtn = document.createElement("button");
            closeBtn.className = "vk-lightbox-close";
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
            ".vk-metric-val, .vk-quality-num, .vk-confidence-big"
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

    function diffClass(d) {
        if (d === "Easy") return "vk-diff-easy";
        if (d === "Medium") return "vk-diff-medium";
        if (d === "Hard") return "vk-diff-hard";
        return "";
    }

    function esc(str) {
        var d = document.createElement("div");
        d.textContent = str;
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
            '<tr class="' + (isExpanded ? "vk-row-expanded" : "") + '" data-id="' + esc(d.instance_id) + '">' +
                '<td class="vk-td-instance">' +
                    '<span class="vk-viewer-instance-name">' + esc(d.instance_id) + '</span>' +
                '</td>' +
                '<td class="vk-td-diff"><span class="vk-diff-badge ' + diffClass(d.difficulty) + '">' + esc(d.difficulty) + '</span></td>' +
                '<td class="vk-td-cwe">' + esc(cweDisplay(d.vulnerability_type)) + '</td>' +
                '<td class="vk-td-tests">' + d.fail_to_pass_count + '</td>' +
                '<td class="vk-td-kimi">' + passBadge(d.kimi_pass_at_1) + '</td>' +
                '<td class="vk-td-nova">' + passBadge(d.nova_pass_at_1) + '</td>' +
                '<td class="vk-td-expand"><span class="vk-expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
            '</tr>'
        );
    }

    function renderDetailRow(d) {
        var problemSnippet = (d.problem_statement || "").substring(0, 200);
        if ((d.problem_statement || "").length > 200) problemSnippet += "...";
        var repoUrl = d.repo ? "https://github.com/" + d.repo : "";

        return (
            '<tr class="vk-detail-row" data-detail-for="' + esc(d.instance_id) + '">' +
                '<td colspan="7">' +
                    '<div class="vk-detail-content">' +
                        '<div class="vk-detail-grid">' +
                            '<div class="vk-detail-block">' +
                                '<div class="vk-detail-block-title">Instance Info</div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Vulnerability</span><span class="vk-detail-val">' + esc(cweDisplay(d.vulnerability_type)) + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Category</span><span class="vk-detail-val">' + esc(d.category || "") + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">Files Affected</span><span class="vk-detail-val">' + esc(d.num_files_affected || "") + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">F2P Tests</span><span class="vk-detail-val">' + d.fail_to_pass_count + '</span></div>' +
                                '<div class="vk-detail-row-item"><span class="vk-detail-key">P2P Tests</span><span class="vk-detail-val">' + d.pass_to_pass_count + '</span></div>' +
                                '<div class="vk-detail-row-item" style="flex-direction:column;gap:4px"><span class="vk-detail-key">Problem Statement</span><span class="vk-detail-val" style="font-weight:400;font-size:12px;line-height:1.5">' + esc(problemSnippet) + '</span></div>' +
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
                        (repoUrl ? '<div class="vk-detail-links" style="margin-top:16px"><a class="vk-detail-link" href="' + esc(repoUrl) + '" target="_blank" rel="noopener">Repository</a></div>' : '') +
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
            html = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--vk-text-muted)">No instances found.</td></tr>';
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
                var idMatch = d.instance_id.toLowerCase().indexOf(search) !== -1;
                var cweMatch = (d.vulnerability_type || []).join(" ").toLowerCase().indexOf(search) !== -1;
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
                allData = data;
                filteredData = data.slice();
                sortData();
                renderTable();
            })
            .catch(function () {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--vk-text-muted)">Failed to load dataset.</td></tr>';
            });

        document.getElementById("vk-viewer-search").addEventListener(
            "input",
            debounce(applyFilters, 250)
        );

        document.getElementById("vk-viewer-difficulty").addEventListener(
            "change",
            applyFilters
        );

        document.getElementById("vk-viewer-sort").addEventListener(
            "change",
            function () {
                currentSort = this.value;
                currentSortDir = 1;
                applyFilters();
            }
        );

        var tableWrap = document.getElementById("vk-viewer-table-wrap");
        if (tableWrap) {
            tableWrap.addEventListener("click", function (e) {
                var row = e.target.closest("tr[data-id]");
                if (!row) return;
                var id = row.getAttribute("data-id");
                if (expandedId === id) {
                    expandedId = null;
                } else {
                    expandedId = id;
                }
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
                    var viewer = document.getElementById("vk-dataset-viewer");
                    if (viewer) {
                        viewer.scrollIntoView({ behavior: "smooth", block: "start" });
                    }
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
            timer = setTimeout(function () {
                fn.apply(ctx, args);
            }, delay);
        };
    }

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
        var odooFooters = document.querySelectorAll("footer:not(.vk-footer), .o_footer, #footer, .o_footer_copyright");
        for (var j = 0; j < odooFooters.length; j++) {
            odooFooters[j].style.display = "none";
        }
        var header = document.querySelector("header");
        if (header) header.style.display = "none";
    }

    function initDarkMode() {
        var portal = document.getElementById("vk-portal");
        var toggle = document.getElementById("vk-theme-toggle");
        if (!portal || !toggle) return;

        var STORAGE_KEY = "vk-dark-mode";
        var DARK_BG = "#0f172a";
        var LIGHT_BG = "#ffffff";

        function applyTheme(isDark) {
            if (isDark) {
                portal.classList.add("vk-dark");
                document.body.classList.add("vk-dark-body");
            } else {
                portal.classList.remove("vk-dark");
                document.body.classList.remove("vk-dark-body");
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
            var isDark = portal.classList.contains("vk-dark");
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
        initDatasetViewer();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
