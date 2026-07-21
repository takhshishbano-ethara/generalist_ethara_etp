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
  var evalAllData = [];
  var evalFilteredData = [];
  var evalCurrentPage = 1;
  var evalCurrentSort = "opus_pass";
  var evalCurrentSortDir = -1;
  var evalExpandedId = null;

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

  function evalParsePass(str) {
    if (!str) return 0;
    return parseFloat(str.replace("%", "")) || 0;
  }

  function evalBarColor(pct) {
    if (pct >= 67) return "var(--au-success)";
    if (pct >= 34) return "var(--au-warning)";
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

  function evalRenderRow(d, isExpanded) {
    var opus = d.models && d.models["Claude Opus 4.8"] ? d.models["Claude Opus 4.8"] : {};
    var gpt = d.models && d.models["GPT-5.5"] ? d.models["GPT-5.5"] : {};
    var gemini = d.models && d.models["Gemini 3.1 Pro"] ? d.models["Gemini 3.1 Pro"] : {};

    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-eval-id="' + esc(d.instance_id) + '">' +
        '<td class="au-etd-instance">' +
          '<span class="au-eval-instance-name">' + esc(d.instance_id) + '</span>' +
        '</td>' +
        '<td class="au-etd-prrange"><span class="au-eval-prrange-badge">' + esc(d.pr_range || "N/A") + '</span></td>' +
        '<td class="au-etd-lang"><span class="au-lang-badge ' + langClass(d.language) + '">' + esc(d.language || "N/A") + '</span></td>' +
        '<td class="au-etd-opus">' + evalPassCell(opus.pass_at_3) + '</td>' +
        '<td class="au-etd-gpt">' + evalPassCell(gpt.pass_at_3) + '</td>' +
        '<td class="au-etd-gemini">' + evalPassCell(gemini.pass_at_3) + '</td>' +
        '<td class="au-etd-repo">' +
          (d.repo_url ? '<a class="au-pr-link" href="' + esc(d.repo_url) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">View</a>' : '<span class="au-text-muted">—</span>') +
        '</td>' +
        '<td class="au-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function evalRenderDetailRow(d) {
    var opus = d.models && d.models["Claude Opus 4.8"] ? d.models["Claude Opus 4.8"] : {};
    var gpt = d.models && d.models["GPT-5.5"] ? d.models["GPT-5.5"] : {};
    var gemini = d.models && d.models["Gemini 3.1 Pro"] ? d.models["Gemini 3.1 Pro"] : {};

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
              modelBlock("Claude Opus 4.8", opus) +
              modelBlock("GPT-5.5", gpt) +
              modelBlock("Gemini 3.1 Pro", gemini) +
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
      var d = pageData[i];
      var isExpanded = d.instance_id === evalExpandedId;
      html += evalRenderRow(d, isExpanded);
      if (isExpanded) html += evalRenderDetailRow(d);
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
    evalRenderTable();
  }

  function evalSortData() {
    var key = evalCurrentSort;
    var dir = evalCurrentSortDir;

    evalFilteredData.sort(function (a, b) {
      var av, bv;

      if (key === "opus_pass") {
        av = evalParsePass(a.models && a.models["Claude Opus 4.8"] ? a.models["Claude Opus 4.8"].pass_at_3 : "0");
        bv = evalParsePass(b.models && b.models["Claude Opus 4.8"] ? b.models["Claude Opus 4.8"].pass_at_3 : "0");
        return (av - bv) * dir;
      }
      if (key === "gpt_pass") {
        av = evalParsePass(a.models && a.models["GPT-5.5"] ? a.models["GPT-5.5"].pass_at_3 : "0");
        bv = evalParsePass(b.models && b.models["GPT-5.5"] ? b.models["GPT-5.5"].pass_at_3 : "0");
        return (av - bv) * dir;
      }
      if (key === "gemini_pass") {
        av = evalParsePass(a.models && a.models["Gemini 3.1 Pro"] ? a.models["Gemini 3.1 Pro"].pass_at_3 : "0");
        bv = evalParsePass(b.models && b.models["Gemini 3.1 Pro"] ? b.models["Gemini 3.1 Pro"].pass_at_3 : "0");
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
