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
                        entry.target.classList.add("kj-visible");
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
        var portal = document.getElementById("kj-portal");
        if (!portal) return;

        portal.addEventListener("click", function (e) {
            var img = e.target.closest(".kj-chart-card img");
            if (!img) return;

            var overlay = document.createElement("div");
            overlay.className = "kj-lightbox";

            var inner = document.createElement("div");
            inner.className = "kj-lightbox-inner";

            var closeBtn = document.createElement("button");
            closeBtn.className = "kj-lightbox-close";
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
            ".kj-metric-val, .kj-quality-num, .kj-confidence-big"
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

    function passFloat(val) {
        var n = parseFloat(val);
        return isNaN(n) ? 0 : n;
    }

    function passRateClass(rate) {
        if (rate >= 60) return "kj-pass-rate-fill-high";
        if (rate >= 20) return "kj-pass-rate-fill-mid";
        if (rate > 0) return "kj-pass-rate-fill-low";
        return "kj-pass-rate-fill-zero";
    }

    function diffClass(d) {
        if (d === "Easy") return "kj-diff-easy";
        if (d === "Medium") return "kj-diff-medium";
        if (d === "Hard") return "kj-diff-hard";
        return "";
    }

    function esc(str) {
        var d = document.createElement("div");
        d.textContent = str;
        return d.innerHTML;
    }

    function passRateCell(rate, cssClass) {
        var r = passFloat(rate);
        return (
            '<div class="kj-pass-rate-bar">' +
                '<span class="kj-pass-rate">' + r.toFixed(1) + '%</span>' +
                '<div class="kj-pass-rate-track">' +
                    '<div class="kj-pass-rate-fill ' + passRateClass(r) + '" style="width:' + Math.min(r, 100) + '%"></div>' +
                '</div>' +
            '</div>'
        );
    }

    function renderRow(d) {
        var isExpanded = expandedId === d.instance_id;
        return (
            '<tr class="' + (isExpanded ? "kj-row-expanded" : "") + '" data-id="' + esc(d.instance_id) + '">' +
                '<td class="kj-td-instance">' +
                    '<span class="kj-viewer-instance-name">' + esc(d.instance_id) + '</span>' +
                    (d.original_repo
                        ? '<a class="kj-viewer-repo-link" href="' + esc(d.original_repo) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + esc(d.original_repo.replace("https://github.com/", "")) + '</a>'
                        : "") +
                '</td>' +
                '<td class="kj-td-diff"><span class="kj-diff-badge ' + diffClass(d.difficulty) + '">' + esc(d.difficulty) + '</span></td>' +
                '<td class="kj-td-tests">' + d.test_count + '</td>' +
                '<td class="kj-td-python">' + esc(d.setup_python) + '</td>' +
                '<td class="kj-td-glm">' + passRateCell(d.glm5_stage3) + '</td>' +
                '<td class="kj-td-nova">' + passRateCell(d.nova_stage3) + '</td>' +
                '<td class="kj-td-expand"><span class="kj-expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
            '</tr>'
        );
    }

    function renderDetailRow(d) {
        return (
            '<tr class="kj-detail-row" data-detail-for="' + esc(d.instance_id) + '">' +
                '<td colspan="7">' +
                    '<div class="kj-detail-content">' +
                        '<div class="kj-detail-grid">' +
                            '<div class="kj-detail-block">' +
                                '<div class="kj-detail-block-title">Setup</div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Source Dir</span><span class="kj-detail-val">' + esc(d.setup_src_dir) + '</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Install</span><span class="kj-detail-val">' + esc(d.setup_install) + '</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Python</span><span class="kj-detail-val">' + esc(d.setup_python) + '</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Test Cmd</span><span class="kj-detail-val">' + esc(d.test_cmd) + '</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Test Dir</span><span class="kj-detail-val">' + esc(d.test_dir) + '</span></div>' +
                                (d.specification ? '<div class="kj-detail-links"><a class="kj-detail-link" href="' + esc(d.specification) + '" target="_blank" rel="noopener">Specification</a></div>' : '') +
                            '</div>' +
                            '<div class="kj-detail-block">' +
                                '<div class="kj-detail-block-title">GLM-5</div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Stage 1</span><span class="kj-detail-val">' + passFloat(d.glm5_stage1).toFixed(1) + '%</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Stage 2</span><span class="kj-detail-val">' + passFloat(d.glm5_stage2).toFixed(1) + '%</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Stage 3</span><span class="kj-detail-val">' + passFloat(d.glm5_stage3).toFixed(1) + '%</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Files Changed</span><span class="kj-detail-val">' + esc(d.glm5_files) + '</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Time (s)</span><span class="kj-detail-val">' + esc(d.glm5_time) + '</span></div>' +
                            '</div>' +
                            '<div class="kj-detail-block">' +
                                '<div class="kj-detail-block-title">Nova-2-Lite</div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Stage 1</span><span class="kj-detail-val">' + passFloat(d.nova_stage1).toFixed(1) + '%</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Stage 2</span><span class="kj-detail-val">' + passFloat(d.nova_stage2).toFixed(1) + '%</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Stage 3</span><span class="kj-detail-val">' + passFloat(d.nova_stage3).toFixed(1) + '%</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Files Changed</span><span class="kj-detail-val">' + esc(d.nova_files) + '</span></div>' +
                                '<div class="kj-detail-row-item"><span class="kj-detail-key">Time (s)</span><span class="kj-detail-val">' + esc(d.nova_time) + '</span></div>' +
                            '</div>' +
                        '</div>' +
                        (d.repo_path ? '<div class="kj-detail-links" style="margin-top:16px"><a class="kj-detail-link" href="' + esc(d.repo_path) + '" target="_blank" rel="noopener">Fork Repository</a>' + (d.original_repo ? '<a class="kj-detail-link" href="' + esc(d.original_repo) + '" target="_blank" rel="noopener">Original Repository</a>' : '') + '</div>' : '') +
                    '</div>' +
                '</td>' +
            '</tr>'
        );
    }

    function renderTable() {
        var tbody = document.getElementById("kj-viewer-tbody");
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
            html = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--kj-text-muted)">No instances found.</td></tr>';
        }

        tbody.innerHTML = html;

        var countEl = document.getElementById("kj-viewer-count");
        if (countEl) {
            countEl.textContent =
                filteredData.length + " of " + allData.length + " instances";
        }

        renderPagination();
    }

    function renderPagination() {
        var container = document.getElementById("kj-viewer-pagination");
        if (!container) return;

        var totalPages = Math.max(1, Math.ceil(filteredData.length / ROWS_PER_PAGE));
        if (totalPages <= 1) {
            container.innerHTML = "";
            return;
        }

        var html = "";
        html += '<button class="kj-page-btn" data-page="' + (currentPage - 1) + '"' + (currentPage <= 1 ? " disabled" : "") + '>&lsaquo; Prev</button>';

        var pages = paginationRange(currentPage, totalPages);
        for (var i = 0; i < pages.length; i++) {
            var p = pages[i];
            if (p === "...") {
                html += '<span class="kj-page-ellipsis">&hellip;</span>';
            } else {
                html += '<button class="kj-page-btn' + (p === currentPage ? " kj-page-active" : "") + '" data-page="' + p + '">' + p + '</button>';
            }
        }

        html += '<button class="kj-page-btn" data-page="' + (currentPage + 1) + '"' + (currentPage >= totalPages ? " disabled" : "") + '>Next &rsaquo;</button>';

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
        var search = (document.getElementById("kj-viewer-search").value || "").toLowerCase();
        var diff = document.getElementById("kj-viewer-difficulty").value;

        filteredData = allData.filter(function (d) {
            if (diff && d.difficulty !== diff) return false;
            if (search && d.instance_id.toLowerCase().indexOf(search) === -1 &&
                d.original_repo.toLowerCase().indexOf(search) === -1) {
                return false;
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
            test_count: true,
            glm5_stage3: true,
            nova_stage3: true,
        };

        filteredData.sort(function (a, b) {
            var av = a[key];
            var bv = b[key];

            if (numericKeys[key]) {
                av = parseFloat(av) || 0;
                bv = parseFloat(bv) || 0;
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
        var tbody = document.getElementById("kj-viewer-tbody");
        if (!tbody) return;

        fetch("/kaiju/api/instances")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                allData = data;
                filteredData = data.slice();
                sortData();
                renderTable();
            })
            .catch(function () {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--kj-text-muted)">Failed to load dataset.</td></tr>';
            });

        document.getElementById("kj-viewer-search").addEventListener(
            "input",
            debounce(applyFilters, 250)
        );

        document.getElementById("kj-viewer-difficulty").addEventListener(
            "change",
            applyFilters
        );

        document.getElementById("kj-viewer-sort").addEventListener(
            "change",
            function () {
                currentSort = this.value;
                currentSortDir = 1;
                applyFilters();
            }
        );

        var tableWrap = document.getElementById("kj-viewer-table-wrap");
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

        var pagination = document.getElementById("kj-viewer-pagination");
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
                    var viewer = document.getElementById("kj-dataset-viewer");
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

    function initDarkMode() {
        var portal = document.getElementById("kj-portal");
        var toggle = document.getElementById("kj-theme-toggle");
        if (!portal || !toggle) return;

        var STORAGE_KEY = "kj-dark-mode";
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
            var odooFooters = document.querySelectorAll("footer:not(.kj-footer), .o_footer, #footer, .o_footer_copyright");
            for (var j = 0; j < odooFooters.length; j++) {
                odooFooters[j].style.display = "none";
            }
            var header = document.querySelector("header");
            if (header) header.style.display = "none";
        }

        function applyTheme(isDark) {
            if (isDark) {
                portal.classList.add("kj-dark");
                document.body.classList.add("kj-dark-body");
            } else {
                portal.classList.remove("kj-dark");
                document.body.classList.remove("kj-dark-body");
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
            var isDark = portal.classList.contains("kj-dark");
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
