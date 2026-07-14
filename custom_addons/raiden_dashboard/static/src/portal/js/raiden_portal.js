(() => {
  "use strict";

  // ============================================================
  // §1 - THEME TOGGLE
  // Persists to localStorage (`raiden:theme`). Reacts to OS
  // preference changes only when user hasn't made explicit choice.
  // The template inline script already sets `data-theme` on :root
  // before this runs, so no flash occurs.
  // ============================================================
  const root = document.documentElement;
  const toggleBtn = document.getElementById('rd-theme-toggle');
  const THEME_KEY = 'raiden:theme';
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
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* noop */ }
      syncButtonLabel();
    });
  }

  // If user has NOT made an explicit choice, follow OS changes live.
  const osChangeHandler = () => {
    try {
      if (localStorage.getItem(THEME_KEY)) return; // user overrode - ignore OS
    } catch (e) { /* noop */ }
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
    var color = evalBarColor(pct);
    return (
      '<div class="rd-eval-pass-cell">' +
        '<span class="rd-eval-pass-pct" style="color:' + color + '">' + (passStr || "0.00%") + '</span>' +
        '<div class="rd-eval-pass-bar">' +
          '<div class="rd-eval-pass-fill" style="width:' + pct + '%;background:' + color + '"></div>' +
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
      btn.title = evalCurrentSortDir === 1 ? "Ascending \u2013 click to reverse" : "Descending \u2013 click to reverse";
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
  // §11 - INIT + DOMContentLoaded
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
