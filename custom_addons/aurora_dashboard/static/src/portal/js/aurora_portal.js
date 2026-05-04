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
  const osChangeHandler = () => {
    try {
      if (localStorage.getItem(THEME_KEY)) return; // user overrode — ignore OS
    } catch (e) { /* noop */ }
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
  // §7 — FUNNEL COUNTERS (IntersectionObserver)
  // Animates `.funnel-num[data-target]` from 0 → target with
  // easeOutCubic over 900ms. Respects prefers-reduced-motion.
  // ============================================================
  function initFunnelCounters() {
    const funnelNums = document.querySelectorAll('.funnel-num[data-target]');
    if (!funnelNums.length) return;

    const animateCount = (el) => {
      const target = Number(el.dataset.target || '0');
      if (prefersReduced) {
        el.textContent = target.toLocaleString();
        return;
      }
      const duration = 900;
      const start = performance.now();
      const step = (now) => {
        const t = Math.min(1, (now - start) / duration);
        // easeOutCubic
        const eased = 1 - Math.pow(1 - t, 3);
        const val = Math.round(target * eased);
        el.textContent = val.toLocaleString();
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };

    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver((entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            animateCount(e.target);
            io.unobserve(e.target);
          }
        }
      }, { threshold: 0.3 });
      funnelNums.forEach((n) => io.observe(n));
    } else {
      funnelNums.forEach(animateCount);
    }
  }

  // ============================================================
  // §8 — COUNT-UP (quality cards, confidence strip)
  // Distinct from funnel counters — operates on metric values
  // that already have text content (including %, commas).
  // ============================================================
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
  var evalCurrentSort = "claude_pass";
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
    var claude = d.models && d.models["Claude Opus 4.6"] ? d.models["Claude Opus 4.6"] : {};
    var glm = d.models && d.models["GLM 5"] ? d.models["GLM 5"] : {};
    var kimi = d.models && d.models["Kimi K2.5"] ? d.models["Kimi K2.5"] : {};

    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-eval-id="' + esc(d.instance_id) + '">' +
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
        '<td class="au-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
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
      countEl.textContent = evalFilteredData.length + " of 10,000 instances";
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
    initFunnelCounters();
    initCountUp();
    initLightbox();
    initEvalViewer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
