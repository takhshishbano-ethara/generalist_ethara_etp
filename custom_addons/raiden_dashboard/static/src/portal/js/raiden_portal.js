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
     §10 - DATASET VIEWER (Raiden · AWS CLI DynamoDB tasks)
     Renders the 30 Harbor tasks with per-model reward (Claude Opus 4.8
     and Claude Haiku 4.5), filterable by difficulty tier and searchable
     by task UUID / command. Data comes from /raiden/api/instances.
     ═══════════════════════════════════════════════════════════════ */

  var EVAL_PER_PAGE = 10;
  var evalAllData = [];
  var evalFilteredData = [];
  var evalCurrentPage = 1;
  var evalCurrentSort = "opus_reward";
  var evalCurrentSortDir = -1;
  var evalExpandedId = null;

  var OPUS = "Claude Opus 4.8";
  var HAIKU = "Claude Haiku 4.5";

  function tierClass(tier) {
    var t = (tier || "").toLowerCase();
    if (t === "easy") return "rd-tier-easy";
    if (t === "medium") return "rd-tier-medium";
    if (t === "hard") return "rd-tier-hard";
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
      timer = setTimeout(function () {
        fn.apply(ctx, args);
      }, delay);
    };
  }

  function evalParsePass(str) {
    if (!str) return 0;
    return parseFloat(String(str).replace("%", "")) || 0;
  }

  function evalBarColor(pct) {
    if (pct >= 67) return "var(--rd-success)";
    if (pct >= 34) return "var(--rd-warning)";
    if (pct > 0) return "var(--rd-danger)";
    return "var(--rd-text-muted)";
  }

  function evalPassCell(passStr) {
    var pct = evalParsePass(passStr);
    return (
      '<div class="rd-eval-pass-cell">' +
        '<span class="rd-eval-pass-pct">' + (passStr || "0.00%") + '</span>' +
        '<div class="rd-eval-pass-bar">' +
          '<div class="rd-eval-pass-fill" style="width:' + pct + '%"></div>' +
        '</div>' +
      '</div>'
    );
  }

  function modelReward(d, name) {
    return d.models && d.models[name] ? d.models[name] : {};
  }

  function evalRenderRow(d, isExpanded) {
    var opus = modelReward(d, OPUS);
    var haiku = modelReward(d, HAIKU);
    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-eval-id="' + esc(d.instance_id) + '">' +
        '<td class="rd-etd-instance">' +
          '<span class="rd-eval-instance-name">' + esc(d.instance_id) + '</span>' +
          '<span class="rd-scope-badge rd-scope-' + (d.scope || '').toLowerCase() + '">' + esc(d.scope || '') + '</span>' +
        '</td>' +
        '<td class="rd-etd-prrange"><span class="rd-eval-prrange-badge">' + esc(d.pr_range || "N/A") + '</span></td>' +
        '<td class="rd-etd-lang"><span class="rd-tier-badge ' + tierClass(d.difficulty) + '">' + esc(d.difficulty || "N/A") + '</span></td>' +
        '<td class="rd-etd-opus">' + evalPassCell(opus.reward_pct) + '</td>' +
        '<td class="rd-etd-haiku">' + evalPassCell(haiku.reward_pct) + '</td>' +
        '<td class="rd-etd-repo"><span class="rd-eval-tests-count">' + esc(String(d.tests_shipped || 0)) + '</span></td>' +
        '<td class="rd-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function evalRenderDetailRow(d) {
    var opus = modelReward(d, OPUS);
    var haiku = modelReward(d, HAIKU);
    var btc = d.behaviour_tag_counts || {};

    function modelBlock(name, m) {
      return (
        '<div class="rd-detail-block">' +
          '<div class="rd-detail-block-title">' + esc(name) + '</div>' +
          '<div class="rd-detail-row-item"><span class="rd-detail-key">Reward</span><span class="rd-detail-val" style="font-weight:700">' + esc(m.reward_pct || "0.00%") + '</span></div>' +
          '<div class="rd-detail-row-item"><span class="rd-detail-key">Tests Passed</span><span class="rd-detail-val">' + esc(String(m.tests_passed != null ? m.tests_passed : 0)) + ' / ' + esc(String(d.tests_shipped || 0)) + '</span></div>' +
          '<div class="rd-detail-row-item"><span class="rd-detail-key">Difficulty</span><span class="rd-detail-val"><span class="rd-tier-badge ' + tierClass(m.tier) + '">' + esc(m.tier || "n/a") + '</span></span></div>' +
        '</div>'
      );
    }

    var cmds = (d.commands || []).map(function (c) {
      return '<code class="rd-cmd-chip">' + esc(c) + '</code>';
    }).join(" ");

    var btcRows = Object.keys(btc).map(function (k) {
      return '<div class="rd-detail-row-item"><span class="rd-detail-key">' + esc(k) + '</span><span class="rd-detail-val">' + esc(String(btc[k])) + '</span></div>';
    }).join("");

    return (
      '<tr class="rd-detail-row" data-eval-detail-for="' + esc(d.instance_id) + '">' +
        '<td colspan="7">' +
          '<div class="rd-detail-content">' +
            '<div class="rd-eval-detail-grid">' +
              '<div class="rd-detail-block">' +
                '<div class="rd-detail-block-title">Task Info</div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Task UUID</span><span class="rd-detail-val">' + esc(d.task_id || d.instance_id) + '</span></div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Scope</span><span class="rd-detail-val">' + esc(d.service || d.scope || "") + '</span></div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Category</span><span class="rd-detail-val">' + esc((d.category || "").replace(/_/g, " ")) + '</span></div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Commands</span><span class="rd-detail-val">' + esc(String(d.command_count || 0)) + '</span></div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Tests Shipped</span><span class="rd-detail-val">' + esc(String(d.tests_shipped || 0)) + '</span></div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Workflow Tests</span><span class="rd-detail-val">' + esc(String(d.workflow_tests || 0)) + '</span></div>' +
                '<div class="rd-detail-row-item"><span class="rd-detail-key">Runtime</span><span class="rd-detail-val">Python ' + esc(d.python_version || "") + ' · ' + esc(d.simulation_backend || "") + '</span></div>' +
              '</div>' +
              modelBlock(OPUS, opus) +
              modelBlock(HAIKU, haiku) +
            '</div>' +
            '<div class="rd-detail-block" style="margin-top:14px">' +
              '<div class="rd-detail-block-title">Behaviour-tag coverage</div>' +
              btcRows +
            '</div>' +
            (cmds ? '<div class="rd-detail-cmds">' + cmds + '</div>' : '') +
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
      var isExpanded = d.instance_id === evalExpandedId;
      html += evalRenderRow(d, isExpanded);
      if (isExpanded) html += evalRenderDetailRow(d);
    }

    if (pageData.length === 0) {
      html = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--rd-text-muted)">No tasks found.</td></tr>';
    }

    tbody.innerHTML = html;

    var countEl = document.getElementById("rd-eval-count");
    if (countEl) {
      countEl.textContent = evalFilteredData.length + " of " + evalAllData.length + " tasks";
    }

    evalRenderPagination();
  }

  function evalRenderPagination() {
    var container = document.getElementById("rd-eval-pagination");
    if (!container) return;

    var totalPages = Math.max(1, Math.ceil(evalFilteredData.length / EVAL_PER_PAGE));
    if (totalPages <= 1) {
      container.innerHTML = "";
      return;
    }

    var html = "";
    html += '<button class="rd-page-btn" data-eval-page="' + (evalCurrentPage - 1) + '"' + (evalCurrentPage <= 1 ? " disabled" : "") + '>&lsaquo; Prev</button>';

    var pages = paginationRange(evalCurrentPage, totalPages);
    for (var i = 0; i < pages.length; i++) {
      var p = pages[i];
      if (p === "...") {
        html += '<span class="rd-page-ellipsis">&hellip;</span>';
      } else {
        html += '<button class="rd-page-btn' + (p === evalCurrentPage ? " rd-page-active" : "") + '" data-eval-page="' + p + '">' + p + '</button>';
      }
    }

    html += '<button class="rd-page-btn" data-eval-page="' + (evalCurrentPage + 1) + '"' + (evalCurrentPage >= totalPages ? " disabled" : "") + '>Next &rsaquo;</button>';

    container.innerHTML = html;
  }

  function evalApplyFilters() {
    var search = (document.getElementById("rd-eval-search").value || "").toLowerCase();
    var tierSel = document.getElementById("rd-eval-difficulty").value;
    var scopeSel = document.getElementById("rd-eval-scope").value;

    evalFilteredData = evalAllData.filter(function (d) {
      if (tierSel && d.difficulty !== tierSel) return false;
      if (scopeSel && d.scope !== scopeSel) return false;
      if (search) {
        var idMatch = (d.instance_id || "").toLowerCase().indexOf(search) !== -1;
        var taskMatch = (d.task_id || "").toLowerCase().indexOf(search) !== -1;
        var cmdMatch = (d.commands || []).join(" ").toLowerCase().indexOf(search) !== -1;
        var scopeMatch = (d.scope || "").toLowerCase().indexOf(search) !== -1 || (d.service || "").toLowerCase().indexOf(search) !== -1;
        if (!idMatch && !taskMatch && !cmdMatch && !scopeMatch) return false;
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

      if (key === "opus_reward") {
        av = evalParsePass(modelReward(a, OPUS).reward_pct);
        bv = evalParsePass(modelReward(b, OPUS).reward_pct);
        return (av - bv) * dir;
      }
      if (key === "haiku_reward") {
        av = evalParsePass(modelReward(a, HAIKU).reward_pct);
        bv = evalParsePass(modelReward(b, HAIKU).reward_pct);
        return (av - bv) * dir;
      }
      if (key === "command_count") {
        av = a.command_count || 0;
        bv = b.command_count || 0;
        return (av - bv) * dir;
      }
      if (key === "difficulty") {
        var order = { easy: 1, medium: 2, hard: 3 };
        av = order[(a.difficulty || "").toLowerCase()] || 99;
        bv = order[(b.difficulty || "").toLowerCase()] || 99;
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
    var tbody = document.getElementById("rd-eval-tbody");
    if (!tbody) return;

    fetch("/raiden/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        evalAllData = data;
        evalFilteredData = data.slice();
        evalSortData();
        evalRenderTable();
      })
      .catch(function () {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--rd-text-muted)">Failed to load task data.</td></tr>';
      });

    document.getElementById("rd-eval-search").addEventListener(
      "input",
      debounce(evalApplyFilters, 250)
    );

    document.getElementById("rd-eval-difficulty").addEventListener(
      "change",
      evalApplyFilters
    );

    document.getElementById("rd-eval-scope").addEventListener(
      "change",
      evalApplyFilters
    );

    document.getElementById("rd-eval-sort").addEventListener(
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

    var sortDirBtn = document.getElementById("rd-eval-sort-dir-btn");
    if (sortDirBtn) {
      sortDirBtn.addEventListener("click", function () {
        evalCurrentSortDir = evalCurrentSortDir * -1;
        evalUpdateSortDirBtn();
        evalApplyFilters();
      });
    }

    function evalUpdateSortDirBtn() {
      var btn = document.getElementById("rd-eval-sort-dir-btn");
      if (!btn) return;
      btn.textContent = evalCurrentSortDir === 1 ? "\u2191" : "\u2193";
      btn.title = evalCurrentSortDir === 1 ? "Ascending, click to reverse" : "Descending, click to reverse";
    }

    var tableWrap = document.getElementById("rd-eval-table-wrap");
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

    var pagination = document.getElementById("rd-eval-pagination");
    if (pagination) {
      pagination.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-eval-page]");
        if (!btn || btn.disabled) return;
        var page = parseInt(btn.getAttribute("data-eval-page"), 10);
        var totalPages = Math.ceil(evalFilteredData.length / EVAL_PER_PAGE);
        if (page >= 1 && page <= totalPages) {
          evalCurrentPage = page;
          evalRenderTable();
          var viewer = document.getElementById("rd-eval-viewer");
          if (viewer) {
            viewer.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }
      });
    }
  }

  // ============================================================
  // §12 - CHARTS (Chart.js, data-driven, theme-aware)
  // 3 charts from /raiden/api/instances. Opus = magenta (--accent),
  // Haiku = blue (--accent-secondary). Re-renders on data-theme change.
  // ============================================================
  var rdCharts = {};
  var rdChartData = null;

  function rdCssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function rdPalette() {
    return {
      opus: rdCssVar("--accent", "#EE00EE"),
      haiku: rdCssVar("--accent-secondary", "#7A99D1"),
      ink: rdCssVar("--ink", "#0B0E14"),
      muted: rdCssVar("--muted", "#8a8fa5"),
      grid: rdCssVar("--border", "rgba(0,0,0,0.10)"),
      surface: rdCssVar("--bg-2", "#ffffff")
    };
  }

  function rdTierAgg(data) {
    var b = { Easy: [], Medium: [], Hard: [] };
    data.forEach(function (d) { if (b[d.difficulty]) b[d.difficulty].push(d); });
    function mean(a, f) { return a.length ? a.reduce(function (s, x) { return s + f(x); }, 0) / a.length : 0; }
    return ["Easy", "Medium", "Hard"].map(function (t) {
      var a = b[t];
      return {
        tier: t, n: a.length,
        opusReward: mean(a, function (d) { return (d.models["Claude Opus 4.8"].reward || 0) * 100; }),
        haikuReward: mean(a, function (d) { return (d.models["Claude Haiku 4.5"].reward || 0) * 100; }),
        opusCost: mean(a, function (d) { return d.models["Claude Opus 4.8"].cost_usd || 0; }),
        haikuCost: mean(a, function (d) { return d.models["Claude Haiku 4.5"].cost_usd || 0; })
      };
    });
  }

  function rdLegend(p) {
    return { position: "top", align: "start", labels: { color: p.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "rectRounded", font: { family: "'DM Sans', sans-serif", size: 12 } } };
  }

  function rdTooltip(p, unit) {
    return {
      backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
      callbacks: { label: function (ctx) {
        var v = ctx.raw;
        return " " + ctx.dataset.label + ": " + (unit === "$" ? "$" + Number(v).toFixed(3) : Number(v).toFixed(1) + "%");
      } }
    };
  }

  function rdAxes(p, valMax, valTitle, horizontal) {
    var val = { grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: valTitle ? { display: true, text: valTitle, color: p.ink, font: { size: 11 } } : { display: false } };
    if (valMax) { val.max = valMax; val.beginAtZero = true; }
    var cat = { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: horizontal ? 10 : 12, family: horizontal ? "'SF Mono', monospace" : "'DM Sans', sans-serif" } } };
    return horizontal ? { x: val, y: cat } : { y: val, x: cat };
  }

  function rdBar(el, labels, opus, haiku, p, opts) {
    return new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [
        { label: "Opus 4.8", data: opus, backgroundColor: rdAlpha(p.opus, 0.18), borderColor: rdAlpha(p.opus, 0.5), borderWidth: 1, borderRadius: 3, categoryPercentage: opts.cat, barPercentage: 0.92 },
        { label: "Haiku 4.5", data: haiku, backgroundColor: p.haiku, borderRadius: 3, categoryPercentage: opts.cat, barPercentage: 0.92 }
      ] },
      options: {
        indexAxis: opts.horizontal ? "y" : "x", responsive: true, maintainAspectRatio: false,
        animation: { duration: 500 },
        plugins: { legend: rdLegend(p), tooltip: rdTooltip(p, opts.unit) },
        scales: rdAxes(p, opts.valMax, opts.valTitle, opts.horizontal)
      }
    });
  }

  function rdCostCombo(el, labels, opus, haiku, splitIdx, p) {
    var plugin = {
      id: "rdCostGroups",
      afterDatasetsDraw: function (chart) {
        var x = chart.scales.x, area = chart.chartArea, ctx = chart.ctx;
        if (!x || !area) { return; }
        var dx = (x.getPixelForValue(splitIdx - 1) + x.getPixelForValue(splitIdx)) / 2;
        ctx.save();
        ctx.strokeStyle = p.ink; ctx.globalAlpha = 0.25; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(dx, area.top); ctx.lineTo(dx, area.bottom); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        ctx.font = "700 10px 'SF Mono', monospace"; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic"; ctx.fillStyle = p.ink;
        ctx.fillText("BY TIER", (x.getPixelForValue(0) + x.getPixelForValue(splitIdx - 1)) / 2, area.top - 6);
        ctx.fillText("BY SURFACE", (x.getPixelForValue(splitIdx) + x.getPixelForValue(labels.length - 1)) / 2, area.top - 6);
        ctx.restore();
      }
    };
    return new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [
        { label: "Opus 4.8", data: opus, backgroundColor: rdAlpha(p.opus, 0.18), borderColor: rdAlpha(p.opus, 0.5), borderWidth: 1, borderRadius: 3, categoryPercentage: 0.72, barPercentage: 0.9 },
        { label: "Haiku 4.5", data: haiku, backgroundColor: p.haiku, borderRadius: 3, categoryPercentage: 0.72, barPercentage: 0.9 }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 500 },
        layout: { padding: { top: 18 } },
        plugins: { legend: rdLegend(p), tooltip: rdTooltip(p, "$") },
        scales: {
          y: { beginAtZero: true, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: "Mean cost / run (USD)", color: p.ink, font: { size: 11 } } },
          x: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 11, family: "'DM Sans', sans-serif" } } }
        }
      },
      plugins: [plugin]
    });
  }

  function rdAlpha(c, a) {
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

  function rdDumbbell(el, tasks, p) {
    var H = "Claude Haiku 4.5", O = "Claude Opus 4.8";
    var labels = tasks.map(function (d) { return d.instance_id; });
    var haiku = tasks.map(function (d) { return +((d.models[H].reward || 0) * 100).toFixed(1); });
    var opus = tasks.map(function (d) { return +((d.models[O].reward || 0) * 100).toFixed(1); });
    var floating = tasks.map(function (d, i) { return [haiku[i], opus[i]]; });
    var BANDS = [{ a: 0, b: 50, c: "rgba(255,143,143,0.13)" }, { a: 50, b: 75, c: "rgba(251,191,36,0.13)" }, { a: 75, b: 100, c: "rgba(94,230,149,0.13)" }];
    var plugin = {
      id: "rdDumb",
      beforeDatasetsDraw: function (chart) {
        var x = chart.scales.x, area = chart.chartArea, ctx = chart.ctx;
        if (!x || !area) { return; }
        BANDS.forEach(function (b) {
          var x1 = x.getPixelForValue(b.a), x2 = x.getPixelForValue(b.b);
          ctx.save(); ctx.fillStyle = b.c; ctx.fillRect(x1, area.top, x2 - x1, area.bottom - area.top); ctx.restore();
        });
        var TL = [{ m: 25, t: "HARD", c: "#D2555A" }, { m: 62.5, t: "MEDIUM", c: "#B8860B" }, { m: 87.5, t: "EASY", c: "#3FB768" }];
        ctx.save();
        ctx.font = "700 10px 'SF Mono', monospace"; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
        TL.forEach(function (z) { ctx.fillStyle = z.c; ctx.fillText(z.t, x.getPixelForValue(z.m), area.top - 7); });
        ctx.restore();
      },
      afterDatasetsDraw: function (chart) {
        var x = chart.scales.x, ctx = chart.ctx, meta = chart.getDatasetMeta(0);
        if (!x || !meta || !meta.data) { return; }
        ctx.save();
        meta.data.forEach(function (bar, i) {
          if (!bar) { return; }
          var yy = bar.y, hx = x.getPixelForValue(haiku[i]), ox = x.getPixelForValue(opus[i]);
          ctx.lineWidth = 1.5; ctx.strokeStyle = p.surface;
          ctx.fillStyle = p.opus;
          ctx.beginPath(); ctx.arc(ox, yy, 5.5, 0, 6.2832); ctx.fill(); ctx.stroke();
          ctx.fillStyle = p.haiku;
          ctx.beginPath(); ctx.arc(hx, yy, 5.5, 0, 6.2832); ctx.fill(); ctx.stroke();
        });
        ctx.restore();
      }
    };
    return new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [
        { label: "gap", data: floating, backgroundColor: rdAlpha(p.ink, 0.28), barThickness: 3, borderRadius: 2 }
      ] },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        animation: { duration: 500 },
        layout: { padding: { right: 12, left: 2, top: 20 } },
        plugins: {
          legend: { position: "top", align: "start", onClick: function () {}, labels: { color: p.ink, usePointStyle: true, boxWidth: 8, font: { family: "'DM Sans', sans-serif", size: 12 }, generateLabels: function () { return [{ text: "Haiku 4.5", fillStyle: p.haiku, strokeStyle: p.haiku, pointStyle: "circle" }, { text: "Opus 4.8", fillStyle: p.opus, strokeStyle: p.opus, pointStyle: "circle" }]; } } },
          tooltip: {
            backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
            callbacks: {
              title: function (items) { var d = tasks[items[0].dataIndex]; return d.instance_id + "  ·  " + d.difficulty; },
              label: function (ctx) { var i = ctx.dataIndex; return ["Haiku 4.5: " + haiku[i] + "%", "Opus 4.8: " + opus[i] + "%", "gap: " + (opus[i] - haiku[i]).toFixed(1) + " pts"]; }
            }
          }
        },
        scales: {
          x: { min: 0, max: 100, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: "Reward (%)", color: p.ink, font: { size: 11 } } },
          y: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 10, family: "'SF Mono', monospace" } } }
        }
      },
      plugins: [plugin]
    });
  }

  function rdRetention(el, data, p) {
    function curve(key) {
      var r = data.map(function (d) { return (d.models[key].reward || 0) * 100; }).sort(function (a, b) { return b - a; });
      var sum = 0, peak = r.length ? r[0] : 1, out = [];
      for (var i = 0; i < r.length; i++) { sum += r[i]; out.push(+((sum / (i + 1)) / (peak || 1) * 100).toFixed(1)); }
      return out;
    }
    var opus = curve("Claude Opus 4.8"), haiku = curve("Claude Haiku 4.5");
    var labels = opus.map(function (v, i) { return i + 1; });
    var endLabels = {
      id: "rdRetEnd",
      afterDatasetsDraw: function (chart) {
        var ctx = chart.ctx;
        [{ i: 0, v: opus[opus.length - 1], c: p.opus }, { i: 1, v: haiku[haiku.length - 1], c: p.haiku }].forEach(function (e) {
          var m = chart.getDatasetMeta(e.i); if (!m || !m.data.length) { return; }
          var last = m.data[m.data.length - 1];
          ctx.save(); ctx.font = "700 13px 'DM Sans', sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillStyle = e.c;
          ctx.fillText(Math.round(e.v) + "%", last.x + 8, last.y);
          ctx.restore();
        });
      }
    };
    return new Chart(el, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "Opus 4.8", data: opus, borderColor: p.opus, backgroundColor: "transparent", borderWidth: 2.5, borderDash: [6, 4], pointRadius: 2.5, pointBackgroundColor: p.opus, tension: 0.2, fill: false, clip: false },
        { label: "Haiku 4.5", data: haiku, borderColor: p.haiku, backgroundColor: rdAlpha(p.haiku, 0.12), borderWidth: 2.5, pointRadius: 2.5, pointBackgroundColor: p.haiku, tension: 0.2, fill: { target: 0 }, clip: false }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 600 },
        layout: { padding: { top: 8, right: 46 } },
        plugins: {
          legend: rdLegend(p),
          tooltip: {
            backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
            callbacks: {
              title: function (items) { return items[0].label + " tasks included"; },
              label: function (ctx) { return " " + ctx.dataset.label + ": " + Number(ctx.raw).toFixed(1) + "% of peak"; }
            }
          }
        },
        scales: {
          y: { min: 40, max: 100, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: "Cumulative mean reward (% of peak)", color: p.ink, font: { size: 11 } } },
          x: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 11, family: "'DM Sans', sans-serif" } }, title: { display: true, text: "Tasks included (easiest to hardest)", color: p.ink, font: { size: 11 } } }
        }
      },
      plugins: [endLabels]
    });
  }

  function rdRelShare(el, data, p) {
    var O = "Claude Opus 4.8", H = "Claude Haiku 4.5", accent = "#8A5CF0";
    var series = data.map(function (d) {
      var o = (d.models[O].reward || 0), h = (d.models[H].reward || 0);
      var r = o > 0 ? (h / o) * 100 : (h > 0 ? 100 : 0);
      return Math.max(0, Math.min(100, +r.toFixed(1)));
    }).sort(function (a, b) { return b - a; });
    var labels = series.map(function (v, i) { return i + 1; });
    var parity = {
      id: "rdShareParity",
      beforeDatasetsDraw: function (chart) {
        var area = chart.chartArea, y = chart.scales.y.getPixelForValue(100), ctx = chart.ctx;
        ctx.save(); ctx.strokeStyle = rdAlpha(p.ink, 0.32); ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(area.left, y); ctx.lineTo(area.right, y); ctx.stroke(); ctx.restore();
      }
    };
    var endLabel = {
      id: "rdShareEnd",
      afterDatasetsDraw: function (chart) {
        var m = chart.getDatasetMeta(0); if (!m || !m.data.length) { return; }
        var last = m.data[m.data.length - 1], ctx = chart.ctx;
        ctx.save(); ctx.font = "700 13px 'DM Sans', sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillStyle = accent;
        ctx.fillText(Math.round(series[series.length - 1]) + "%", last.x + 8, last.y);
        ctx.restore();
      }
    };
    return new Chart(el, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "Haiku reward / Opus reward", data: series, borderColor: accent, backgroundColor: rdAlpha(accent, 0.10), borderWidth: 2.5, pointRadius: 2.6, pointBackgroundColor: accent, tension: 0.2, fill: false, clip: false }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 600 },
        layout: { padding: { top: 8, right: 46 } },
        plugins: {
          legend: rdLegend(p),
          tooltip: {
            backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
            callbacks: {
              title: function (items) { return "Task rank " + items[0].label; },
              label: function (ctx) { return " Haiku captures " + Number(ctx.raw).toFixed(1) + "% of Opus"; }
            }
          }
        },
        scales: {
          y: { min: 0, max: 105, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 }, stepSize: 20, callback: function (v) { return v > 100 ? "" : v; } }, title: { display: true, text: "Haiku reward as % of Opus", color: p.ink, font: { size: 11 } } },
          x: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 11, family: "'DM Sans', sans-serif" } }, title: { display: true, text: "Task rank (highest share to lowest)", color: p.ink, font: { size: 11 } } }
        }
      },
      plugins: [parity, endLabel]
    });
  }

  function rdRenderCharts() {
    if (typeof Chart === "undefined" || !rdChartData) return;
    var p = rdPalette();
    var data = rdChartData;
    Chart.defaults.font.family = "'DM Sans', system-ui, sans-serif";
    Chart.defaults.color = p.ink;

    var elS3 = document.getElementById("rd-chart-pertask-s3");
    var elDDB = document.getElementById("rd-chart-pertask-ddb");
    var byH = function (a, b) { return (b.models["Claude Haiku 4.5"].reward || 0) - (a.models["Claude Haiku 4.5"].reward || 0); };
    if (elS3) { rdCharts.dumbS3 = rdDumbbell(elS3, data.filter(function (d) { return d.scope === "S3"; }).sort(byH), p); }
    if (elDDB) { rdCharts.dumbDDB = rdDumbbell(elDDB, data.filter(function (d) { return d.scope === "DynamoDB"; }).sort(byH), p); }

    var elRet = document.getElementById("rd-chart-retention");
    if (elRet) { rdCharts.retention = rdRetention(elRet, data, p); }

    var elShare = document.getElementById("rd-chart-relshare");
    if (elShare) { rdCharts.relshare = rdRelShare(elShare, data, p); }
  }

  function rdReRenderCharts() {
    Object.keys(rdCharts).forEach(function (k) { if (rdCharts[k]) { rdCharts[k].destroy(); } });
    rdCharts = {};
    rdRenderCharts();
  }

  function initCharts() {
    if (!document.getElementById("rd-chart-pertask-s3") && !document.getElementById("rd-chart-retention") && !document.getElementById("rd-chart-relshare")) return;
    fetch("/raiden/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        rdChartData = data;
        if (typeof Chart === "undefined") {
          window.addEventListener("load", rdRenderCharts, { once: true });
        } else {
          rdRenderCharts();
        }
      })
      .catch(function () {});
    if (window.MutationObserver) {
      new MutationObserver(function () { if (rdChartData) rdReRenderCharts(); })
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
