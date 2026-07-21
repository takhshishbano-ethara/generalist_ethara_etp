(() => {
  "use strict";

  // ============================================================
  // §1 - THEME
  // Follows the OS color-scheme (prefers-color-scheme), live-updating on
  // system theme change. The toggle is a temporary session-only override.
  // The template inline script already sets `data-theme` on :root
  // before this runs, so no flash occurs.
  // ============================================================
  const root = document.documentElement;
  const toggleBtn = document.getElementById('rd-theme-toggle');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)');

  const currentTheme = () => {
    const explicit = root.getAttribute('data-theme');
    if (explicit === 'light' || explicit === 'dark') return explicit;
    return 'dark';
  };

  const syncButtonLabel = () => {
    if (!toggleBtn) return;
    const theme = currentTheme();
    toggleBtn.setAttribute('aria-pressed', String(theme === 'dark'));
    toggleBtn.setAttribute(
      'aria-label',
      theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
    );
  };
  syncButtonLabel();

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      syncButtonLabel();
    });
  }

  // System preference is authoritative: follow OS changes live.
  const osChangeHandler = () => {
    root.setAttribute('data-theme', prefersDark.matches ? 'dark' : 'light');
    syncButtonLabel();
  };
  if (prefersDark.addEventListener) {
    prefersDark.addEventListener('change', osChangeHandler);
  } else if (prefersDark.addListener) {
    prefersDark.addListener(osChangeHandler); // Safari < 14
  }

  // ============================================================
  // §2 - SCROLL PROGRESS BAR
  // Writes `width` into .scroll-progress on every rAF while the
  // user scrolls. Passive listeners, zero layout thrash.
  // ============================================================
  (() => {
    const bar = document.querySelector('.scroll-progress');
    if (!bar) return;
    let ticking = false;
    const update = () => {
      const doc = document.documentElement;
      const max = (doc.scrollHeight - window.innerHeight) || 1;
      const progress = Math.max(0, Math.min(1, window.scrollY / max));
      bar.style.width = (progress * 100).toFixed(2) + '%';
      ticking = false;
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(update);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    update();
  })();

  // ============================================================
  // §3 - THESIS WORD SPLIT
  // Splits the `.thesis` text into per-word `<span>` wrappers so
  // GSAP (or CSS) can clip-reveal each word. Runs OUTSIDE the
  // GSAP init so the DOM is ready even if GSAP fails to load.
  // ============================================================
  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    const raw = thesisEl.textContent.trim();
    const words = raw.split(/\s+/);
    thesisEl.innerHTML = words.map((w) => {
      return `<span class="thesis-word"><span class="thesis-word-inner">${w}</span></span>`;
    }).join(' ');
  }

  // ============================================================
  // §4 - GSAP ANIMATIONS (progressive enhancement)
  // Checks for window.gsap + window.ScrollTrigger + motion pref.
  // If any condition fails, page still reads perfectly - just no
  // entrance animations.
  // ============================================================
  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const EASE_OUT = 'cubic-bezier(0.28, 0.11, 0.32, 1)';
  const EASE_UI  = 'cubic-bezier(0.25, 0.1, 0.25, 1)';

  const initAnimations = () => {
    if (prefersReduced) return;
    if (typeof window.gsap === 'undefined' || typeof window.ScrollTrigger === 'undefined') return;

    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    gsap.ticker.fps(60);
    gsap.defaults({ ease: EASE_OUT, duration: 0.64 });

    // ---------- 1. Masthead on load ---------------------------------
    gsap.from('.wordmark', { y: 24, opacity: 0, duration: 0.9, delay: 0.05, ease: EASE_OUT });
    gsap.from('.badge',    { y: 16, opacity: 0, duration: 0.7, delay: 0.18, ease: EASE_OUT });

    // Thesis word reveal - staggered yPercent rise
    gsap.from('.thesis-word-inner', {
      yPercent: 110,
      opacity:  0,
      duration: 0.9,
      stagger:  0.04,
      ease:     EASE_OUT,
      delay:    0.35,
    });

    // ---------- 2. Section fade-rise on scroll ----------------------
    document.querySelectorAll('main > .section').forEach((section) => {
      const children = section.querySelectorAll(':scope > *');
      gsap.from(children, {
        y: 28,
        opacity: 0,
        stagger: 0.08,
        duration: 0.64,
        ease: EASE_OUT,
        scrollTrigger: {
          trigger: section,
          start: 'top 85%',
          toggleActions: 'play none none none',
        },
        onStart() {
          children.forEach((el) => { el.style.willChange = 'transform, opacity'; });
        },
        onComplete() {
          children.forEach((el) => { el.style.willChange = ''; });
        },
      });
    });

    // Re-measure on resize
    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

  // ============================================================
  // §5 - GSAP BOOT
  // GSAP loads via CDN with defer - may not be ready at script
  // execution time. Wait for `load` if needed.
  // ============================================================
  if (typeof window.gsap === 'undefined') {
    window.addEventListener('load', initAnimations, { once: true });
  } else {
    initAnimations();
  }

  // ============================================================
  // §6 - NON-GSAP SCROLL ANIMATIONS (IntersectionObserver)
  // Basic fade-up fallback for `[data-animate]` elements.
  // Works even when GSAP CDN fails.
  // ============================================================
  function initScrollAnimations() {
    var elements = document.querySelectorAll("[data-animate]");
    if (!elements.length) return;

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("rd-visible");
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

  // ============================================================
  // §7 - (Removed: funnel counters now use static HTML values)
  // ============================================================

  // ============================================================
  // §8 - (Removed: count-up now uses static HTML values)
  // ============================================================

  // ============================================================
  // §9 - LIGHTBOX
  // Uses the `#lightbox` element from the template (not dynamic
  // DOM creation). Triggered by `.chart-trigger` button clicks.
  // ============================================================
  function initLightbox() {
    const lightbox = document.getElementById('lightbox');
    const lightboxImg = document.getElementById('lightbox-img');
    const lightboxCaption = document.getElementById('lightbox-caption');
    const lightboxClose = document.getElementById('lightbox-close');
    if (!lightbox || !lightboxImg) return;

    let lastChartFocus = null;

    const openLightbox = (trigger) => {
      const imgs = trigger.querySelectorAll('img');
      const img = Array.prototype.find.call(imgs, (i) => i.offsetParent !== null) || imgs[0];
      if (!img) return;
      const figcap = trigger.closest('figure') ? trigger.closest('figure').querySelector('figcaption') : null;
      lightboxImg.src = img.currentSrc || img.src;
      lightboxImg.alt = img.alt || '';
      if (lightboxCaption) {
        lightboxCaption.textContent = figcap ? figcap.textContent.trim() : '';
      }
      lightbox.removeAttribute('hidden');
      requestAnimationFrame(() => lightbox.setAttribute('data-open', 'true'));
      lightbox.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      lastChartFocus = document.activeElement;
      if (lightboxClose) lightboxClose.focus();
    };

    const closeLightbox = () => {
      if (!lightbox || lightbox.hidden) return;
      lightbox.setAttribute('data-open', 'false');
      lightbox.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
      setTimeout(() => {
        lightbox.setAttribute('hidden', '');
        lightboxImg.src = '';
      }, prefersReduced ? 0 : 180);
      if (lastChartFocus && lastChartFocus.focus) lastChartFocus.focus();
    };

    // Delegate clicks on chart triggers
    const chartsContainer = document.querySelector('.charts');
    if (chartsContainer) {
      chartsContainer.addEventListener('click', (e) => {
        const trigger = e.target.closest('.chart-trigger');
        if (!trigger) return;
        e.preventDefault();
        openLightbox(trigger);
      });
    }

    // Close triggers
    if (lightboxClose) {
      lightboxClose.addEventListener('click', closeLightbox);
    }
    lightbox.addEventListener('click', (e) => {
      if (e.target === lightbox) closeLightbox();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && lightbox && !lightbox.hidden) closeLightbox();
    });
  }

  /* ===================================================================
     §10 - MODEL LEADERBOARD (Kang · Hedge-Bench 1.0)
     Renders the 8-model leaderboard (dense 0-4, theme + move coverage),
     filterable by provider and searchable. Data: /kang/api/instances.
     =================================================================== */

  var EVAL_PER_PAGE = 10;
  var evalAllData = [];
  var evalFilteredData = [];
  var evalCurrentPage = 1;
  var evalCurrentSort = "dense";
  var evalCurrentSortDir = -1;
  var evalExpandedId = null;

  function providerClass(prov) {
    var t = (prov || "").toLowerCase();
    if (t === "anthropic") return "rd-tier-easy";
    if (t === "openai") return "rd-tier-medium";
    if (t === "google") return "rd-tier-hard";
    return "";
  }

  function esc(str) {
    var d = document.createElement("div");
    d.textContent = str == null ? "" : str;
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
      timer = setTimeout(function () { fn.apply(ctx, args); }, delay);
    };
  }

  function num(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }
  function fmtDense(v) { return num(v).toFixed(2); }
  function fmtPctV(v) { return num(v).toFixed(1) + "%"; }

  function kgCell(display, pct) {
    return (
      '<div class="rd-eval-pass-cell">' +
        '<span class="rd-eval-pass-pct">' + display + '</span>' +
        '<div class="rd-eval-pass-bar">' +
          '<div class="rd-eval-pass-fill" style="width:' + Math.max(0, Math.min(100, pct)) + '%"></div>' +
        '</div>' +
      '</div>'
    );
  }

  function evalRenderRow(d, isExpanded) {
    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-eval-id="' + esc(d.model) + '">' +
        '<td class="rd-etd-instance"><span class="rd-eval-instance-name">' + esc(d.model) + '</span></td>' +
        '<td class="rd-etd-prrange"><span class="rd-tier-badge ' + providerClass(d.provider) + '">' + esc(d.provider || "") + '</span></td>' +
        '<td class="rd-etd-lang">' + kgCell(fmtDense(d.dense), num(d.dense) / 4 * 100) + '</td>' +
        '<td class="rd-etd-opus">' + kgCell(fmtPctV(d.themes), num(d.themes)) + '</td>' +
        '<td class="rd-etd-repo">' + kgCell(fmtPctV(d.moves), num(d.moves)) + '</td>' +
        '<td class="rd-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function evalRenderDetailRow(d) {
    function item(k, v) { return '<div class="rd-detail-row-item"><span class="rd-detail-key">' + esc(k) + '</span><span class="rd-detail-val">' + v + '</span></div>'; }
    return (
      '<tr class="rd-detail-row" data-eval-detail-for="' + esc(d.model) + '">' +
        '<td colspan="6">' +
          '<div class="rd-detail-content">' +
            '<div class="rd-eval-detail-grid">' +
              '<div class="rd-detail-block">' +
                '<div class="rd-detail-block-title">Model</div>' +
                item("Provider", esc(d.provider || "")) +
                item("Rank", "#" + esc(String(d.rank || ""))) +
                item("Environments", "102") +
                item("Trials", "8 per environment") +
              '</div>' +
              '<div class="rd-detail-block">' +
                '<div class="rd-detail-block-title">Scores</div>' +
                item("Dense (0-4)", '<span style="font-weight:700">' + fmtDense(d.dense) + '</span>') +
                item("Themes covered", fmtPctV(d.themes)) +
                item("Moves covered", fmtPctV(d.moves)) +
              '</div>' +
            '</div>' +
          '</div>' +
        '</td>' +
      '</tr>'
    );
  }

  function evalRenderTable() {
    var tbody = document.getElementById("rd-eval-tbody");
    if (!tbody) return;
    var start = (evalCurrentPage - 1) * EVAL_PER_PAGE;
    var pageData = evalFilteredData.slice(start, start + EVAL_PER_PAGE);
    var html = "";
    for (var i = 0; i < pageData.length; i++) {
      var d = pageData[i];
      var isExpanded = d.model === evalExpandedId;
      html += evalRenderRow(d, isExpanded);
      if (isExpanded) html += evalRenderDetailRow(d);
    }
    if (pageData.length === 0) {
      html = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--rd-text-muted)">No models found.</td></tr>';
    }
    tbody.innerHTML = html;
    var countEl = document.getElementById("rd-eval-count");
    if (countEl) countEl.textContent = evalFilteredData.length + " of " + evalAllData.length + " models";
    evalRenderPagination();
  }

  function evalRenderPagination() {
    var container = document.getElementById("rd-eval-pagination");
    if (!container) return;
    var totalPages = Math.max(1, Math.ceil(evalFilteredData.length / EVAL_PER_PAGE));
    if (totalPages <= 1) { container.innerHTML = ""; return; }
    var html = "";
    html += '<button class="rd-page-btn" data-eval-page="' + (evalCurrentPage - 1) + '"' + (evalCurrentPage <= 1 ? " disabled" : "") + '>&lsaquo; Prev</button>';
    var pages = paginationRange(evalCurrentPage, totalPages);
    for (var i = 0; i < pages.length; i++) {
      var pg = pages[i];
      if (pg === "...") { html += '<span class="rd-page-ellipsis">&hellip;</span>'; }
      else { html += '<button class="rd-page-btn' + (pg === evalCurrentPage ? " rd-page-active" : "") + '" data-eval-page="' + pg + '">' + pg + '</button>'; }
    }
    html += '<button class="rd-page-btn" data-eval-page="' + (evalCurrentPage + 1) + '"' + (evalCurrentPage >= totalPages ? " disabled" : "") + '>Next &rsaquo;</button>';
    container.innerHTML = html;
  }

  function evalApplyFilters() {
    var searchEl = document.getElementById("rd-eval-search");
    var search = (searchEl ? searchEl.value : "").toLowerCase();
    var provEl = document.getElementById("rd-eval-scope");
    var provSel = provEl ? provEl.value : "";
    evalFilteredData = evalAllData.filter(function (d) {
      if (provSel && d.provider !== provSel) return false;
      if (search) {
        var m = (d.model || "").toLowerCase().indexOf(search) !== -1;
        var pr = (d.provider || "").toLowerCase().indexOf(search) !== -1;
        if (!m && !pr) return false;
      }
      return true;
    });
    evalSortData();
    evalCurrentPage = 1;
    evalRenderTable();
  }

  function evalSortData() {
    var key = evalCurrentSort;
    var dir = evalCurrentSortDir;
    evalFilteredData.sort(function (a, b) {
      if (key === "dense" || key === "themes" || key === "moves") {
        return (num(a[key]) - num(b[key])) * dir;
      }
      var av = (a[key] || "").toString().toLowerCase();
      var bv = (b[key] || "").toString().toLowerCase();
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }

  function initEvalViewer() {
    var tbody = document.getElementById("rd-eval-tbody");
    if (!tbody) return;

    fetch("/kang/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        evalAllData = data;
        evalFilteredData = data.slice();
        evalSortData();
        evalRenderTable();
      })
      .catch(function () {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--rd-text-muted)">Failed to load leaderboard.</td></tr>';
      });

    var searchEl = document.getElementById("rd-eval-search");
    if (searchEl) searchEl.addEventListener("input", debounce(evalApplyFilters, 250));
    var scopeEl = document.getElementById("rd-eval-scope");
    if (scopeEl) scopeEl.addEventListener("change", evalApplyFilters);

    var sortEl = document.getElementById("rd-eval-sort");
    if (sortEl) sortEl.addEventListener("change", function () {
      if (evalCurrentSort === this.value) { evalCurrentSortDir = evalCurrentSortDir * -1; }
      else { evalCurrentSort = this.value; evalCurrentSortDir = (this.value === "model") ? 1 : -1; }
      evalUpdateSortDirBtn();
      evalApplyFilters();
    });

    var sortDirBtn = document.getElementById("rd-eval-sort-dir-btn");
    if (sortDirBtn) sortDirBtn.addEventListener("click", function () {
      evalCurrentSortDir = evalCurrentSortDir * -1;
      evalUpdateSortDirBtn();
      evalApplyFilters();
    });

    function evalUpdateSortDirBtn() {
      var btn = document.getElementById("rd-eval-sort-dir-btn");
      if (!btn) return;
      btn.textContent = evalCurrentSortDir === 1 ? "\u2191" : "\u2193";
      btn.title = evalCurrentSortDir === 1 ? "Ascending, click to reverse" : "Descending, click to reverse";
    }

    var tableWrap = document.getElementById("rd-eval-table-wrap");
    if (tableWrap) tableWrap.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      var row = e.target.closest("tr[data-eval-id]");
      if (!row) return;
      var id = row.getAttribute("data-eval-id");
      evalExpandedId = (evalExpandedId === id) ? null : id;
      evalRenderTable();
    });

    var pagination = document.getElementById("rd-eval-pagination");
    if (pagination) pagination.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-eval-page]");
      if (!btn || btn.disabled) return;
      var page = parseInt(btn.getAttribute("data-eval-page"), 10);
      var totalPages = Math.ceil(evalFilteredData.length / EVAL_PER_PAGE);
      if (page >= 1 && page <= totalPages) {
        evalCurrentPage = page;
        evalRenderTable();
        var viewer = document.getElementById("rd-eval-viewer");
        if (viewer) viewer.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }

  // ============================================================
  // §12 - CHARTS (Chart.js, interactive, theme-aware)
  // Model leaderboard, difficulty by category, theme vs move coverage.
  // Magenta = --accent, blue = --accent-secondary.
  // ============================================================
  var kgCharts = {};

  function kgCssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function kgAlpha(c, a) {
    c = (c || "").trim();
    if (c.charAt(0) === "#") {
      var h = c.slice(1);
      if (h.length === 3) { h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2); }
      var n = parseInt(h, 16);
      return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
    }
    return c.replace(/rgba?\(([^)]+)\)/, function (m, inner) {
      var q = inner.split(",").slice(0, 3).map(function (x) { return x.trim(); });
      return "rgba(" + q.join(",") + "," + a + ")";
    });
  }

  function kgPalette() {
    return {
      accent: kgCssVar("--accent", "#EE00EE"),
      accent2: kgCssVar("--accent-secondary", "#7A99D1"),
      ink: kgCssVar("--ink", "#0B0E14"),
      grid: kgCssVar("--border", "rgba(0,0,0,0.10)"),
      surface: kgCssVar("--bg-2", "#ffffff")
    };
  }

  // Snapshot data (Hedge-Bench 1.0, 8 frontier models, 102 environments).
  var KG_MODELS = ["Claude-Sonnet-4.6", "Claude-Opus-4.7", "GPT-5.5", "Gemini-3.5-Flash", "Claude-Opus-4.8", "Claude-Haiku-4.5", "Gemini-3.1-Pro", "GPT-5.4-Mini"];
  var KG_DENSE  = [1.92, 1.84, 1.68, 1.68, 1.62, 1.14, 1.12, 0.75];
  var KG_THEMES = [56.4, 53.6, 48.1, 48.0, 47.3, 32.5, 30.5, 19.8];
  var KG_MOVES  = [54.9, 53.9, 49.7, 49.8, 49.1, 36.9, 37.6, 26.1];
  var KG_CATS   = ["Valuation", "Growth", "M&A", "Competitive Pos.", "Risk"];
  var KG_CAT_D  = [1.61, 1.48, 1.40, 1.39, 1.23];

  function kgTip(p, cb) {
    return { backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8, callbacks: cb || {} };
  }

  function kgModelsChart(el, p) {
    return new Chart(el, {
      type: "bar",
      data: { labels: KG_MODELS, datasets: [{ label: "Dense 0-4", data: KG_DENSE, backgroundColor: kgAlpha(p.accent, 0.55), borderColor: p.accent, borderWidth: 1, borderRadius: 4, maxBarThickness: 26 }] },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
        plugins: { legend: { display: false }, tooltip: kgTip(p, { label: function (ctx) { return " " + Number(ctx.parsed.x).toFixed(2) + " / 4.0"; } }) },
        scales: {
          x: { beginAtZero: true, max: 4, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: "Mean dense score (0-4)", color: p.ink, font: { size: 11 } } },
          y: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 11, family: "'SF Mono', monospace" } } }
        }
      }
    });
  }

  function kgCatChart(el, p) {
    return new Chart(el, {
      type: "bar",
      data: { labels: KG_CATS, datasets: [{ label: "Dense 0-4", data: KG_CAT_D, backgroundColor: kgAlpha(p.accent, 0.55), borderColor: p.accent, borderWidth: 1, borderRadius: 4, maxBarThickness: 64 }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
        plugins: { legend: { display: false }, tooltip: kgTip(p, { label: function (ctx) { return " " + Number(ctx.parsed.y).toFixed(2) + " / 4.0"; } }) },
        scales: {
          y: { beginAtZero: true, suggestedMax: 2, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: "Mean dense score (0-4)", color: p.ink, font: { size: 11 } } },
          x: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 11, family: "'DM Sans', sans-serif" } } }
        }
      }
    });
  }

  function kgThemesChart(el, p) {
    return new Chart(el, {
      type: "bar",
      data: { labels: KG_MODELS, datasets: [
        { label: "Themes %", data: KG_THEMES, backgroundColor: kgAlpha(p.accent, 0.55), borderColor: p.accent, borderWidth: 1, borderRadius: 3 },
        { label: "Moves %", data: KG_MOVES, backgroundColor: kgAlpha(p.accent2, 0.7), borderColor: p.accent2, borderWidth: 1, borderRadius: 3 }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
        plugins: {
          legend: { position: "top", align: "start", labels: { color: p.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "rectRounded", font: { family: "'DM Sans', sans-serif", size: 12 } } },
          tooltip: kgTip(p, { label: function (ctx) { return " " + ctx.dataset.label + ": " + Number(ctx.parsed.y).toFixed(1) + "%"; } })
        },
        scales: {
          y: { beginAtZero: true, suggestedMax: 60, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 }, callback: function (v) { return v + "%"; } } },
          x: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 9, family: "'SF Mono', monospace" }, maxRotation: 60, minRotation: 45 } }
        }
      }
    });
  }

  function kgRenderCharts() {
    if (typeof Chart === "undefined") return;
    var p = kgPalette();
    Chart.defaults.font.family = "'DM Sans', system-ui, sans-serif";
    Chart.defaults.color = p.ink;
    var e1 = document.getElementById("kg-chart-models"); if (e1) kgCharts.models = kgModelsChart(e1, p);
    var e2 = document.getElementById("kg-chart-categories"); if (e2) kgCharts.cats = kgCatChart(e2, p);
    var e3 = document.getElementById("kg-chart-themes"); if (e3) kgCharts.themes = kgThemesChart(e3, p);
  }

  function kgReRenderCharts() {
    Object.keys(kgCharts).forEach(function (k) { if (kgCharts[k]) { kgCharts[k].destroy(); } });
    kgCharts = {};
    kgRenderCharts();
  }

  function initCharts() {
    if (!document.getElementById("kg-chart-models") && !document.getElementById("kg-chart-categories") && !document.getElementById("kg-chart-themes")) return;
    if (typeof Chart === "undefined") {
      window.addEventListener("load", kgRenderCharts, { once: true });
    } else {
      kgRenderCharts();
    }
    if (window.MutationObserver) {
      new MutationObserver(function () { kgReRenderCharts(); })
        .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }
  }

  // ============================================================
  // §11 - INIT + DOMContentLoaded
  // ============================================================
  function init() {
    initScrollAnimations();
    initLightbox();
    initEvalViewer();
    initCharts();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
