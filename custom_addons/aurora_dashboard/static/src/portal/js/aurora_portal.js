(() => {
  "use strict";

  // ============================================================
  // §1 — THEME TOGGLE
  // Persists to localStorage (`aurora:theme`). Reacts to OS
  // preference changes only when user hasn't made explicit choice.
  // The template inline script already sets `data-theme` on :root
  // before this runs, so no flash occurs.
  // ============================================================
  const root = document.documentElement;
  const toggleBtn = document.getElementById('au-theme-toggle');
  const THEME_KEY = 'aurora:theme';
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)');

  const currentTheme = () => {
    const explicit = root.getAttribute('data-theme');
    if (explicit === 'light' || explicit === 'dark') return explicit;
    return prefersDark.matches ? 'dark' : 'light';
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
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* noop */ }
      syncButtonLabel();
    });
  }

  // If user has NOT made an explicit choice, follow OS changes live.
  const osChangeHandler = (e) => {
    try {
      if (localStorage.getItem(THEME_KEY)) return; // user overrode — ignore OS
    } catch (err) { /* noop */ }
    const sysDark = e && typeof e.matches === 'boolean' ? e.matches : prefersDark.matches;
    root.setAttribute('data-theme', sysDark ? 'dark' : 'light');
    syncButtonLabel();
  };
  if (prefersDark.addEventListener) {
    prefersDark.addEventListener('change', osChangeHandler);
  } else if (prefersDark.addListener) {
    prefersDark.addListener(osChangeHandler); // Safari < 14
  }

  // ============================================================
  // §2 — SCROLL PROGRESS BAR
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
  // §3 — THESIS WORD SPLIT
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
  // §4 — GSAP ANIMATIONS (progressive enhancement)
  // Checks for window.gsap + window.ScrollTrigger + motion pref.
  // If any condition fails, page still reads perfectly — just no
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

    // Thesis word reveal — staggered yPercent rise
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

    // ---------- 3. Funnel final step glow (one-shot) ----------------
    const css = getComputedStyle(document.documentElement);
    const accent = css.getPropertyValue('--au-accent').trim() || '#E8A838';
    const accentSurface = css.getPropertyValue('--au-accent-surface').trim() || '#FFF3E0';
    gsap.fromTo('.funnel-step--final',
      { boxShadow: `inset 0 0 0 1px color-mix(in oklab, ${accent} 30%, transparent)` },
      {
        boxShadow: `inset 0 0 0 1px ${accent}, 0 0 30px ${accentSurface}`,
        duration: 0.8,
        delay: 0.4,
        ease: EASE_OUT,
        scrollTrigger: { trigger: '.funnel', start: 'top 75%', once: true },
      }
    );

    // Re-measure on resize
    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

  // ============================================================
  // §5 — GSAP BOOT
  // GSAP loads via CDN with defer — may not be ready at script
  // execution time. Wait for `load` if needed.
  // ============================================================
  if (typeof window.gsap === 'undefined') {
    window.addEventListener('load', initAnimations, { once: true });
  } else {
    initAnimations();
  }

  // ============================================================
  // §6 — NON-GSAP SCROLL ANIMATIONS (IntersectionObserver)
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

  // ============================================================
  // §7 — (Removed: funnel counters now use static HTML values)
  // ============================================================

  // ============================================================
  // §8 — (Removed: count-up now uses static HTML values)
  // ============================================================

  // ============================================================
  // §9 — LIGHTBOX
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
      const img = trigger.querySelector('img');
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

  /* ═══════════════════════════════════════════════════════════════
     §10 — DELIVERY EVALUATION VIEWER
     ═══════════════════════════════════════════════════════════════ */

  var EVAL_PER_PAGE = 10;
  var TIER_ORDER = ["trivial", "easy", "medium", "hard", "expert"];
  // Column order for the expand cards; overridden by data.models (by sequence).
  var FALLBACK_MODELS = [
    { key: "claude-opus-4-8", display_name: "Claude Opus 4.8" },
    { key: "gemini-3.1-pro-preview", display_name: "Gemini 3.1 Pro" },
    { key: "gpt-5.5", display_name: "GPT-5.5" },
  ];
  var evalAllData = [];          // task rows
  var evalFilteredData = [];
  var evalRunsByTask = {};       // task uuid -> [run, ...]
  var evalModels = [];           // [{key, display_name}] ordered by sequence
  var evalCurrentPage = 1;
  var evalCurrentSort = "task_slug";
  var evalCurrentSortDir = 1;
  var evalExpandedId = null;

  function langLabel(lang) {
    var map = { python: "Python", javascript: "JavaScript", typescript: "TypeScript", go: "Go", rust: "Rust", java: "Java", cpp: "C++", c: "C" };
    return map[lang] || lang;
  }

  /* DEAD (dataset-viewer rewrite, milobench parity): no call sites — the row
     renderer now uses a neutral .au-lang-badge. Commented for rollback. */
  /*
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
  */

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

  /* DEAD (dataset-viewer rewrite): only called by evalPassCell (also dead).
     Commented for rollback. */
  /*
  function evalParsePass(str) {
    if (!str) return 0;
    return parseFloat(str.replace("%", "")) || 0;
  }
  */

  // Imported from milobench pass-bar-fill: fixed vivid hex, theme-independent,
  // milobench thresholds (<20 red, <60 yellow, else green).
  function evalBarColor(pct) {
    if (pct >= 60) return "#22C55E";
    if (pct >= 20) return "#EAB308";
    return "#EF4444";
  }

  /* DEAD (dataset-viewer rewrite): superseded by evalPassBar(d.pass_rate) /
     evalPassBar(d.mean_score) in evalRenderRow. Commented for rollback. */
  /*
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
  */

  // Numeric aggregate bar for the Pass rate / Mean score columns (0–100).
  function evalPassBar(pct) {
    var p = Math.max(0, Math.min(100, pct || 0));
    var color = evalBarColor(p);
    // milobench: neutral % text, vivid coloured bar only.
    return (
      '<div class="au-eval-pass-cell">' +
        '<span class="au-eval-pass-pct">' + p.toFixed(1) + '%</span>' +
        '<div class="au-eval-pass-bar">' +
          '<div class="au-eval-pass-fill" style="width:' + p + '%;background:' + color + '"></div>' +
        '</div>' +
      '</div>'
    );
  }

  // Difficulty pill (trivial → expert), coloured via [data-tier] in CSS.
  function evalTierBadge(tier) {
    var t = (tier || "").toLowerCase();
    return '<span class="au-tier-badge" data-tier="' + esc(t) + '">' + esc(t || "—") + '</span>';
  }

  // Per-run value inside an expand card: PASS (green) when the run scored a
  // binary pass, otherwise the numeric score in the fail colour.
  function evalRunValue(run) {
    if (!run) return '<span class="au-detail-run au-detail-run-na">—</span>';
    var cls = run.score_binary ? "au-detail-run-pass" : "au-detail-run-fail";
    var label = run.score_binary ? "PASS" : (run.score * 100).toFixed(1) + "%";
    return '<span class="au-detail-run ' + cls + '">' + label + '</span>';
  }

  function evalRenderRow(d, isExpanded) {
    var slug = d.task_slug || d.uuid || "—";
    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded au-row-expanded' : '') + '" data-eval-id="' + esc(d.uuid) + '">' +
        '<td class="au-etd-task">' +
          '<span class="au-eval-instance-name">' + esc(slug) + '</span>' +
          '<span class="au-eval-task-uuid">' + esc(d.uuid || "") + '</span>' +
        '</td>' +
        '<td class="au-etd-codebase"><span class="au-eval-codebase">' + esc(d.codebase || "—") + '</span></td>' +
        '<td class="au-etd-lang"><span class="au-lang-badge">' + esc(langLabel(d.language) || "N/A") + '</span></td>' +   /* neutral pill; langClass() dropped for milobench parity */
        '<td class="au-etd-tier">' + evalTierBadge(d.difficulty) + '</td>' +
        '<td class="au-etd-hunks au-num">' + (d.src_hunks || 0) + '</td>' +
        '<td class="au-etd-passrate">' + evalPassBar(d.pass_rate) + '</td>' +
        '<td class="au-etd-meanscore">' + evalPassBar(d.mean_score) + '</td>' +
        '<td class="au-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function evalRenderDetailRow(d) {
    var runs = evalRunsByTask[d.uuid] || [];
    var models = evalModels.length ? evalModels : FALLBACK_MODELS;

    var cards = models.map(function (m) {
      var mruns = runs
        .filter(function (r) { return r.model_key === m.key; })
        .sort(function (a, b) { return a.run_number - b.run_number; });
      var passCount = mruns.filter(function (r) { return r.score_binary; }).length;
      var meanScore = mruns.length ? (mruns.reduce(function (a, r) { return a + r.score; }, 0) / mruns.length) * 100 : 0;
      var meanCost = mruns.length ? (mruns.reduce(function (a, r) { return a + (r.cost_usd || 0); }, 0) / mruns.length) : 0;
      var runRows = mruns.map(function (r) {
        return '<div class="au-detail-row-item"><span class="au-detail-key">run_' + r.run_number + '</span>' +
               '<span class="au-detail-val">' + evalRunValue(r) + '</span></div>';
      }).join("");
      return (
        '<div class="au-detail-block">' +
          '<div class="au-detail-block-title">' + esc(m.display_name) + '</div>' +
          runRows +
          '<div class="au-detail-row-item"><span class="au-detail-key">Pass@3</span><span class="au-detail-val">' + passCount + '/' + (mruns.length || 0) + '</span></div>' +
          '<div class="au-detail-row-item"><span class="au-detail-key">Mean score</span><span class="au-detail-val">' + meanScore.toFixed(1) + '%</span></div>' +
          '<div class="au-detail-row-item"><span class="au-detail-key">Mean cost</span><span class="au-detail-val">$' + meanCost.toFixed(2) + '</span></div>' +
        '</div>'
      );
    }).join("");

    var links = [];
    if (d.dataset_url) links.push('<a class="au-detail-link" href="' + esc(d.dataset_url) + '" target="_blank" rel="noopener">Dataset</a>');
    if (d.trajectories_url) links.push('<a class="au-detail-link" href="' + esc(d.trajectories_url) + '" target="_blank" rel="noopener">Trajectories</a>');
    if (d.instruction_url) links.push('<a class="au-detail-link" href="' + esc(d.instruction_url) + '" target="_blank" rel="noopener">instruction.md</a>');

    return (
      '<tr class="au-detail-row" data-eval-detail-for="' + esc(d.uuid) + '">' +
        '<td colspan="8">' +
          '<div class="au-detail-content">' +
            '<div class="au-eval-detail-grid">' + cards + '</div>' +
            (links.length ? '<div class="au-detail-links">' + links.join("") + '</div>' : '') +
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
      var d = pageData[i];
      var isExpanded = d.uuid === evalExpandedId;
      html += evalRenderRow(d, isExpanded);
      if (isExpanded) html += evalRenderDetailRow(d);
    }

    if (pageData.length === 0) {
      html = '<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--au-text-muted)">No tasks match your filters.</td></tr>';
    }

    tbody.innerHTML = html;

    var countEl = document.getElementById("au-eval-count");
    if (countEl) {
      var n = evalFilteredData.length;
      countEl.textContent = n + (n === 1 ? " task" : " tasks");
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
    var tierEl = document.getElementById("au-eval-tier");
    var tier = tierEl ? tierEl.value : "";
    var lang = document.getElementById("au-eval-language").value;

    evalFilteredData = evalAllData.filter(function (d) {
      if (tier && d.difficulty !== tier) return false;
      if (lang && d.language !== lang) return false;
      if (search) {
        var hay = [d.task_slug, d.codebase, d.uuid, d.keywords].filter(Boolean).join(" ").toLowerCase();
        if (hay.indexOf(search) === -1) return false;
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
      var av = a[key];
      var bv = b[key];

      if (key === "difficulty") {
        av = TIER_ORDER.indexOf((av || "").toLowerCase());
        bv = TIER_ORDER.indexOf((bv || "").toLowerCase());
      }
      if (av === null || av === undefined) av = "";
      if (bv === null || bv === undefined) bv = "";
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;

      av = av.toString().toLowerCase();
      bv = bv.toString().toLowerCase();
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
        var tasks = (data && data.tasks) || [];
        var runs = (data && data.runs) || [];
        evalModels = ((data && data.models) || FALLBACK_MODELS).slice().sort(function (a, b) {
          return (a.sequence || 0) - (b.sequence || 0);
        });
        evalRunsByTask = {};
        runs.forEach(function (r) {
          if (!evalRunsByTask[r.task_uuid]) evalRunsByTask[r.task_uuid] = [];
          evalRunsByTask[r.task_uuid].push(r);
        });
        evalAllData = tasks;
        evalFilteredData = tasks.slice();
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

    var tierSel = document.getElementById("au-eval-tier");
    if (tierSel) tierSel.addEventListener("change", evalApplyFilters);

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
        if (e.target.closest("a")) return;
        var row = e.target.closest("tr[data-eval-id]");
        if (!row) return;
        var id = row.getAttribute("data-eval-id");
        evalExpandedId = (evalExpandedId === id) ? null : id;
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
          evalRenderTable();
          var viewer = document.getElementById("au-eval-viewer");
          if (viewer) {
            viewer.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }
      });
    }
  }

  // ============================================================
  // §11 — INIT + DOMContentLoaded
  // ============================================================
  function init() {
    initScrollAnimations();
    initLightbox();
    initEvalViewer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

/* ============================================================
   §11 — DATA CHARTS (Chart.js), ported from milobench_dashboard.
   Combined Pass@3 by PR horizon (3 models) + Pass@3 by difficulty
   tier. Static PNG remains commented in the template as backup.
   ============================================================ */
(function auCharts() {
  "use strict";
  if (typeof window.Chart === "undefined") return; // Chart.js CDN not loaded

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function palette() {
    return {
      opus:    cssVar("--chart-opus",   "#EA9A3C"),
      gemini:  cssVar("--chart-gemini", "#4A9AE0"),
      gpt:     cssVar("--chart-gpt",    "#57B85C"),
      ink:     cssVar("--ink",    "#0B0E14"),
      muted:   cssVar("--muted",  "#8a8fa5"),
      grid:    cssVar("--border", "rgba(0,0,0,0.10)"),
      surface: cssVar("--bg-2",   "#151a28"),
    };
  }
  function lineDataset(label, color, data) {
    return {
      label: label, data: data, borderColor: color, backgroundColor: color,
      borderDash: [8, 6], borderWidth: 2.5, fill: false, spanGaps: true, tension: 0,
      pointRadius: 6, pointHoverRadius: 8, pointBackgroundColor: color,
      pointBorderColor: color, pointBorderWidth: 2,
    };
  }
  function tooltip(p) {
    return {
      enabled: true, backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink,
      borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
      callbacks: {
        label: function (ctx) {
          var v = ctx.parsed && (ctx.parsed.y !== undefined ? ctx.parsed.y : ctx.parsed);
          if (typeof v !== "number") return ctx.dataset.label + ": —";
          return ctx.dataset.label + ": " + v.toFixed(1) + "%";
        },
      },
    };
  }

  // Per-point value labels: within each x-column the highest value sits above
  // its point, the lowest below, middle to the right (fans out ties cleanly).
  var valueLabelPlugin = {
    id: "auValueLabels",
    afterDatasetsDraw: function (chart, args, pluginOpts) {
      var opts = pluginOpts || {};
      var format = opts.format || function (v) { return String(v); };
      var ctx = chart.ctx;
      var groups = {};
      chart.data.datasets.forEach(function (ds, di) {
        var meta = chart.getDatasetMeta(di);
        if (!meta || meta.hidden || !meta.data) return;
        meta.data.forEach(function (elem, i) {
          var raw = ds.data[i];
          var val = (raw && typeof raw === "object") ? raw.y : raw;
          if (val === null || val === undefined || typeof val !== "number" || isNaN(val)) return;
          (groups[i] || (groups[i] = [])).push({ x: elem.x, y: elem.y, val: val, color: ds.borderColor || "#000" });
        });
      });
      ctx.save();
      ctx.font = "700 11px 'DM Sans', system-ui, sans-serif";
      ctx.textBaseline = "middle";
      ctx.lineJoin = "round";
      var haloColor = cssVar("--bg-2", "#151a28");
      var DX = 18, DY_UP = 18, DY_DN = 20, MIN_GAP = 18;
      var lastCol = (chart.data.labels || []).length - 1;
      var placement = opts.placement || "fan";
      Object.keys(groups).forEach(function (key) {
        var colIdx = +key;
        var pts = groups[key];
        pts.sort(function (a, b) { return a.y - b.y; });
        var labels;
        if (placement === "trail") {
          var toRight = (colIdx === lastCol);
          labels = pts.map(function (pt) {
            return { text: format(pt.val), x: pt.x + (toRight ? DX : -DX), y: pt.y, align: toRight ? "left" : "right", color: pt.color };
          });
        } else {
          labels = pts.map(function (pt, idx) {
            var lx, ly, align;
            if (idx === 0) { lx = pt.x; ly = pt.y - DY_UP; align = "center"; }
            else if (idx === pts.length - 1) { lx = pt.x; ly = pt.y + DY_DN; align = "center"; }
            else { lx = pt.x + DX; ly = pt.y; align = "left"; }
            return { text: format(pt.val), x: lx, y: ly, align: align, color: pt.color };
          });
        }
        labels.sort(function (a, b) { return a.y - b.y; });
        var before = labels.reduce(function (s, L) { return s + L.y; }, 0) / labels.length;
        for (var i = 1; i < labels.length; i++) {
          if (labels[i].y - labels[i - 1].y < MIN_GAP) labels[i].y = labels[i - 1].y + MIN_GAP;
        }
        var after = labels.reduce(function (s, L) { return s + L.y; }, 0) / labels.length;
        var shift = before - after;
        if (shift) labels.forEach(function (L) { L.y += shift; });
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

  function options(p, title, xTitle, yTitle, placement) {
    return {
      responsive: true, maintainAspectRatio: false,
      layout: { padding: { top: 30, right: 52, left: 10, bottom: 6 } },
      plugins: {
        legend: { display: true, position: "top", align: "end", labels: { color: p.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "circle", font: { family: "'DM Sans', sans-serif", size: 12 }, padding: 14 } },
        tooltip: tooltip(p),
        title: { display: true, text: title, color: p.ink, font: { family: "'DM Sans', sans-serif", size: 15, weight: "600" }, padding: { bottom: 12 } },
        auValueLabels: { placement: placement, format: function (v) { return Math.round(v) + "%"; } },
      },
      scales: {
        x: { offset: true, grid: { display: false, drawBorder: false }, ticks: { color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 } }, title: { display: true, text: xTitle, color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
        y: { min: 0, max: 100, grid: { color: p.grid, drawBorder: false }, ticks: { stepSize: 20, color: p.ink, font: { family: "'DM Sans', sans-serif", size: 11 } }, title: { display: true, text: yTitle, color: p.muted, font: { family: "'DM Sans', sans-serif", size: 11 } } },
      },
    };
  }

  var charts = {};
  function build() {
    var p = palette();
    var elPR = document.getElementById("au-chart-pass-rate");
    var elTier = document.getElementById("au-chart-pass-tier");
    if (elPR && !charts.pr) {
      charts.pr = new window.Chart(elPR, {
        type: "line",
        data: {
          labels: ["2-5", "6-10", "11-20", "21-40", "41-100", "100+"],
          datasets: [
            lineDataset("Claude Opus 4.8", p.opus, [58, 44, 27, 21, 6, 2]),
            lineDataset("GPT-5.5", p.gpt, [51.1, 28.2, 16.7, 16.7, 0, 0]),
            lineDataset("Gemini 3.1 Pro", p.gemini, [40, 38.5, 22.2, 12.5, 0, 0]),
          ],
        },
        options: options(p, "Combined Pass@3 by PR Horizon", "Number of PRs", "Success Rate (%)", "trail"),
        plugins: [valueLabelPlugin],
      });
    }
    if (elTier && !charts.tier) {
      charts.tier = new window.Chart(elTier, {
        type: "line",
        data: {
          labels: ["Trivial", "Easy", "Medium", "Hard", "Expert"],
          datasets: [
            lineDataset("Claude Opus 4.8", p.opus, [100, 50, 42, 22, 0]),
            lineDataset("GPT-5.5", p.gpt, [100, 89, 25, 15, 0]),
            lineDataset("Gemini 3.1 Pro", p.gemini, [100, 72, 33, 4, 0]),
          ],
        },
        options: options(p, "Pass@3 by Difficulty Tier", "Difficulty tier", "Pass@3 (%)", "fan"),
        plugins: [valueLabelPlugin],
      });
    }
  }

  // Rebuild on theme change so axis/label colours follow the active palette.
  function rebuild() {
    Object.keys(charts).forEach(function (k) { if (charts[k]) { charts[k].destroy(); charts[k] = null; } });
    build();
  }
  try {
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) { if (muts[i].attributeName === "data-theme") { rebuild(); break; } }
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  } catch (e) { /* noop */ }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
