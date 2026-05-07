/**
 * Akatsuki Portal — akatsuki_dashboard
 * Vanilla JS for the public portal page at /akatsuki
 */
(function () {
    "use strict";

    /* ── Theme Toggle ── */
    const STORAGE_KEY = "akatsuki:theme";

    function getTheme() {
        return document.documentElement.getAttribute("data-theme") || "light";
    }

    function setTheme(t) {
        document.documentElement.setAttribute("data-theme", t);
        try { localStorage.setItem(STORAGE_KEY, t); } catch (e) {}
    }

    document.addEventListener("DOMContentLoaded", function () {
        var toggle = document.getElementById("theme-toggle");
        if (toggle) {
            toggle.addEventListener("click", function () {
                setTheme(getTheme() === "dark" ? "light" : "dark");
            });
        }

        /* ── Scroll Progress ── */
        var bar = document.querySelector(".scroll-progress");
        if (bar) {
            window.addEventListener("scroll", function () {
                var scrollTop = window.scrollY;
                var docHeight = document.documentElement.scrollHeight - window.innerHeight;
                bar.style.width = docHeight > 0 ? (scrollTop / docHeight * 100) + "%" : "0%";
            });
        }

        /* ── Lightbox ── */
        var lightbox = document.getElementById("lightbox");
        var lbImg = document.getElementById("lightbox-img");
        var lbCaption = document.getElementById("lightbox-caption");
        var lbClose = document.getElementById("lightbox-close");

        document.querySelectorAll(".chart-trigger").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var theme = getTheme();
                var img = btn.querySelector(theme === "dark" ? ".chart-dark" : ".chart-light");
                if (!img) img = btn.querySelector("img");
                if (!img) return;
                lbImg.src = img.src;
                lbImg.alt = img.alt;
                var cap = btn.closest("figure");
                lbCaption.textContent = cap ? cap.querySelector("figcaption").textContent : "";
                lightbox.hidden = false;
                lightbox.setAttribute("aria-hidden", "false");
            });
        });

        if (lbClose) {
            lbClose.addEventListener("click", closeLightbox);
        }
        if (lightbox) {
            lightbox.addEventListener("click", function (e) {
                if (e.target === lightbox) closeLightbox();
            });
        }

        function closeLightbox() {
            lightbox.hidden = true;
            lightbox.setAttribute("aria-hidden", "true");
            lbImg.src = "";
        }

        /* ── KPI Counter Animation (GSAP) ── */
        if (typeof gsap !== "undefined" && typeof ScrollTrigger !== "undefined") {
            gsap.registerPlugin(ScrollTrigger);

            document.querySelectorAll(".kpi-num").forEach(function (el) {
                var target = parseFloat(el.dataset.target) || 0;
                var suffix = el.dataset.suffix || "";
                var obj = { val: 0 };

                ScrollTrigger.create({
                    trigger: el,
                    start: "top 85%",
                    once: true,
                    onEnter: function () {
                        gsap.to(obj, {
                            val: target,
                            duration: 1.6,
                            ease: "power2.out",
                            onUpdate: function () {
                                el.textContent = (target % 1 === 0)
                                    ? Math.round(obj.val).toLocaleString() + suffix
                                    : obj.val.toFixed(1) + suffix;
                            }
                        });
                    }
                });
            });
        }

        /* ── Dataset Viewer ── */
        var API_URL = "/akatsuki/api/instances";
        var tbody = document.getElementById("viewer-tbody");
        var countEl = document.getElementById("viewer-count");

        if (tbody) {
            fetch(API_URL)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!Array.isArray(data) || data.length === 0) {
                        countEl.textContent = "0 instances";
                        tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:24px;color:var(--muted);">No instances loaded yet.</td></tr>';
                        return;
                    }
                    countEl.textContent = data.length + " instances";
                    renderTable(data);
                })
                .catch(function () {
                    countEl.textContent = "Error loading data";
                });
        }

        function renderTable(data) {
            tbody.innerHTML = "";
            data.slice(0, 50).forEach(function (item) {
                var tr = document.createElement("tr");
                tr.innerHTML =
                    '<td>' + (item.instance_id || item.name || "—") + '</td>' +
                    '<td>' + (item.difficulty || "—") + '</td>' +
                    '<td></td>';
                tbody.appendChild(tr);
            });
        }
    });
})();
