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
     §10 - DATASET VIEWER (Rinzler · long-horizon business-sim tasks)
     Renders the 30 Harbor tasks with per-task DATASET metadata only
     (declared tier, seed, world config, verifier expectations). No
     model rollout / trajectory reward is shown. Data comes from
     /rinzler/api/instances.
     ═══════════════════════════════════════════════════════════════ */

  var EVAL_PER_PAGE = 10;
  var evalAllData = [];
  var evalFilteredData = [];
  var evalCurrentPage = 1;
  var evalCurrentSort = "task_completion_floor";
  var evalCurrentSortDir = -1;
  var evalExpandedId = null;
  var evalMaxFunds = 1;
  var evalMaxFloor = 1;

  var TIER_ORDER = { Trivial: 1, Easy: 2, Medium: 3, Hard: 4, Expert: 5 };

  function tierClass(tier) {
    var t = (tier || "").toLowerCase();
    if (t === "trivial" || t === "easy") return "rd-tier-easy";
    if (t === "medium") return "rd-tier-medium";
    if (t === "hard" || t === "expert") return "rd-tier-hard";
    return "";
  }

  function esc(str) {
    var d = document.createElement("div");
    d.textContent = str == null ? "" : str;
    return d.innerHTML;
  }

  function fmtUsd(usd) {
    return "$" + Math.round(usd || 0).toLocaleString("en-US");
  }

  function fmtFundsCompact(usd) {
    usd = usd || 0;
    if (usd >= 1e6) return "$" + (usd / 1e6).toFixed(2) + "M";
    if (usd >= 1e3) return "$" + (usd / 1e3).toFixed(1) + "k";
    return "$" + Math.round(usd);
  }

  function fmtCents(cents) {
    var usd = (cents || 0) / 100;
    var neg = usd < 0;
    return (neg ? "-$" : "$") + Math.abs(Math.round(usd)).toLocaleString("en-US");
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

  function evalBarCell(valueText, pct, colorVar) {
    pct = Math.max(0, Math.min(100, pct));
    return (
      '<div class="rd-eval-pass-cell">' +
        '<span class="rd-eval-pass-pct">' + valueText + '</span>' +
        '<div class="rd-eval-pass-bar">' +
          '<div class="rd-eval-pass-fill" style="width:' + pct + '%;background:' + colorVar + '"></div>' +
        '</div>' +
      '</div>'
    );
  }

  function expected(d) {
    return d.expected || {};
  }

  function evalRenderRow(d, isExpanded) {
    var e = expected(d);
    var funds = d.initial_funds_usd || 0;
    var floor = e.task_completion_floor || 0;
    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-eval-id="' + esc(d.instance_id) + '">' +
        '<td class="rd-etd-instance">' +
          '<span class="rd-eval-instance-name">' + esc(d.instance_id) + '</span>' +
          '<span class="rd-tier-badge ' + tierClass(d.tier) + '">' + esc(d.tier || '') + '</span>' +
        '</td>' +
        '<td class="rd-etd-prrange"><span class="rd-eval-prrange-badge">' + esc(String(d.seed != null ? d.seed : "N/A")) + '</span></td>' +
        '<td class="rd-etd-lang">' + evalBarCell(fmtFundsCompact(funds), funds / evalMaxFunds * 100, "var(--accent)") + '</td>' +
        '<td class="rd-etd-opus">' + evalBarCell(String(floor), floor / evalMaxFloor * 100, "var(--accent-secondary)") + '</td>' +
        '<td class="rd-etd-haiku"><span class="rd-eval-tests-count">' + esc(String(e.rat_detection_turn_window != null ? e.rat_detection_turn_window : 0)) + ' turns</span></td>' +
        '<td class="rd-etd-repo"><span class="rd-eval-tests-count">' + esc(String(d.checkers || 0)) + '</span></td>' +
        '<td class="rd-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function detailItem(key, val) {
    return '<div class="rd-detail-row-item"><span class="rd-detail-key">' + esc(key) + '</span><span class="rd-detail-val">' + val + '</span></div>';
  }

  function evalRenderDetailRow(d) {
    var e = expected(d);
    var r = d.resources || {};

    var taskInfo =
      '<div class="rd-detail-block">' +
        '<div class="rd-detail-block-title">Task Info</div>' +
        detailItem("Task UUID", esc(d.task_id || d.instance_id)) +
        detailItem("Tier", '<span class="rd-tier-badge ' + tierClass(d.tier) + '">' + esc(d.tier || "") + '</span>') +
        detailItem("Seed", esc(String(d.seed != null ? d.seed : ""))) +
        detailItem("Family", esc(d.family || "")) +
        detailItem("Maturity", esc(d.maturity || "")) +
        detailItem("Disposition", esc(d.disposition || "")) +
        detailItem("Content hash", '<code class="rd-cmd-chip">' + esc(d.content_hash || "") + '</code>') +
      '</div>';

    var world =
      '<div class="rd-detail-block">' +
        '<div class="rd-detail-block-title">World config</div>' +
        detailItem("Company", esc(d.company_name || "")) +
        detailItem("Horizon", esc(String(d.horizon_years || "")) + " year") +
        detailItem("Start date", esc(d.start_date || "")) +
        detailItem("Starting funds", '<span style="font-weight:700">' + esc(fmtUsd(d.initial_funds_usd)) + '</span>') +
        detailItem("Clients", esc(String(d.num_clients != null ? d.num_clients : ""))) +
        detailItem("Employees", esc(String(d.num_employees != null ? d.num_employees : ""))) +
        detailItem("Market tasks", esc(String(d.num_market_tasks != null ? d.num_market_tasks : ""))) +
        detailItem("RAT fraction", esc(d.rat_fraction != null ? (d.rat_fraction * 100).toFixed(1) + "%" : "")) +
      '</div>';

    var verifier =
      '<div class="rd-detail-block">' +
        '<div class="rd-detail-block-title">Verifier expectations</div>' +
        detailItem("Prestige floor", esc(String(e.prestige_floor != null ? e.prestige_floor : ""))) +
        detailItem("Task-completion floor", '<span style="font-weight:700">' + esc(String(e.task_completion_floor != null ? e.task_completion_floor : "")) + '</span>') +
        detailItem("Deadline hit-rate floor", esc(e.deadline_hit_rate_floor != null ? (e.deadline_hit_rate_floor * 100).toFixed(0) + "%" : "")) +
        detailItem("RAT-detection window", esc(String(e.rat_detection_turn_window != null ? e.rat_detection_turn_window : "")) + " turns") +
        detailItem("Min domains @ prestige", esc(String(e.min_domains_at_prestige_floor != null ? e.min_domains_at_prestige_floor : ""))) +
        detailItem("Funds band", esc(fmtCents(e.final_funds_cents_lo)) + " &#x2013; " + esc(fmtCents(e.final_funds_cents_hi))) +
        detailItem("Intra-year floor", esc(fmtCents(e.intra_year_floor_cents))) +
        detailItem("Expected RAT F1", esc(String(e.expected_rat_f1 != null ? e.expected_rat_f1 : ""))) +
      '</div>';

    var grading =
      '<div class="rd-detail-block" style="margin-top:14px">' +
        '<div class="rd-detail-block-title">Resources &amp; grading</div>' +
        '<div class="rd-eval-detail-grid">' +
          detailItem("CPUs", esc(String(r.cpus != null ? r.cpus : ""))) +
          detailItem("Memory", esc(String(r.memory_mb != null ? r.memory_mb : "")) + " MB") +
          detailItem("Storage", esc(String(r.storage_mb != null ? r.storage_mb : "")) + " MB") +
          detailItem("Agent timeout", esc(String(r.agent_timeout_sec != null ? r.agent_timeout_sec : "")) + " s") +
          detailItem("Verifier timeout", esc(String(r.verifier_timeout_sec != null ? r.verifier_timeout_sec : "")) + " s") +
          detailItem("Build timeout", esc(String(r.build_timeout_sec != null ? r.build_timeout_sec : "")) + " s") +
          detailItem("Reward checkers", esc(String(d.checkers || 0))) +
          detailItem("Canary tokens", esc(String(d.canaries || 0))) +
        '</div>' +
      '</div>';

    return (
      '<tr class="rd-detail-row" data-eval-detail-for="' + esc(d.instance_id) + '">' +
        '<td colspan="7">' +
          '<div class="rd-detail-content">' +
            '<div class="rd-eval-detail-grid">' +
              taskInfo +
              world +
              verifier +
            '</div>' +
            grading +
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

  function fundsBand(usd) {
    if (usd < 100000) return "low";
    if (usd <= 500000) return "mid";
    return "high";
  }

  function evalApplyFilters() {
    var search = (document.getElementById("rd-eval-search").value || "").toLowerCase();
    var tierSel = document.getElementById("rd-eval-scope").value;
    var bandSel = document.getElementById("rd-eval-difficulty").value;

    evalFilteredData = evalAllData.filter(function (d) {
      if (tierSel && d.tier !== tierSel) return false;
      if (bandSel && fundsBand(d.initial_funds_usd || 0) !== bandSel) return false;
      if (search) {
        var idMatch = (d.instance_id || "").toLowerCase().indexOf(search) !== -1;
        var taskMatch = (d.task_id || "").toLowerCase().indexOf(search) !== -1;
        var tierMatch = (d.tier || "").toLowerCase().indexOf(search) !== -1;
        var seedMatch = String(d.seed || "").toLowerCase().indexOf(search) !== -1;
        var nameMatch = (d.name || "").toLowerCase().indexOf(search) !== -1;
        if (!idMatch && !taskMatch && !tierMatch && !seedMatch && !nameMatch) return false;
      }
      return true;
    });

    evalSortData();
    evalCurrentPage = 1;
    evalRenderTable();
  }

  function evalSortVal(d, key) {
    if (key === "task_completion_floor") return expected(d).task_completion_floor || 0;
    if (key === "rat_detection_turn_window") return expected(d).rat_detection_turn_window || 0;
    if (key === "initial_funds_usd") return d.initial_funds_usd || 0;
    if (key === "seed") return d.seed || 0;
    if (key === "tier") return TIER_ORDER[d.tier] || 99;
    return null;
  }

  function evalSortData() {
    var key = evalCurrentSort;
    var dir = evalCurrentSortDir;

    evalFilteredData.sort(function (a, b) {
      var av = evalSortVal(a, key);
      var bv = evalSortVal(b, key);

      if (av !== null && bv !== null) {
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

    fetch("/rinzler/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        evalAllData = data;
        evalFilteredData = data.slice();
        evalMaxFunds = Math.max.apply(null, data.map(function (d) { return d.initial_funds_usd || 0; }).concat([1]));
        evalMaxFloor = Math.max.apply(null, data.map(function (d) { return expected(d).task_completion_floor || 0; }).concat([1]));
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
  // §12 - CHARTS (Chart.js, dataset-driven, theme-aware)
  // Three per-tier aggregates from /rinzler/api/instances: declared
  // task-completion floor, starting funds, and RAT-detection window.
  // Every series is a DATASET fact - no model rollout / reward.
  // Re-renders on data-theme change.
  // ============================================================
  var rdCharts = {};
  var rdChartData = null;
  var TIER_SEQ = ["Trivial", "Easy", "Medium", "Hard", "Expert"];

  function rdCssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function rdPalette() {
    return {
      accent: rdCssVar("--accent", "#EE00EE"),
      accent2: rdCssVar("--accent-secondary", "#7A99D1"),
      ink: rdCssVar("--ink", "#0B0E14"),
      muted: rdCssVar("--muted", "#8a8fa5"),
      grid: rdCssVar("--border", "rgba(0,0,0,0.10)"),
      surface: rdCssVar("--bg-2", "#ffffff")
    };
  }

  function rdTierColors() {
    return {
      Trivial: rdCssVar("--accent-secondary", "#7A99D1"),
      Easy: rdCssVar("--tier-clean", "#0E8F4B"),
      Medium: rdCssVar("--tier-fixable", "#B8860B"),
      Hard: rdCssVar("--tier-degraded", "#B5171F"),
      Expert: rdCssVar("--accent", "#EE00EE")
    };
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

  function rdTierAgg(data, valueFn) {
    var buckets = {};
    TIER_SEQ.forEach(function (t) { buckets[t] = []; });
    data.forEach(function (d) { if (buckets[d.tier]) buckets[d.tier].push(valueFn(d)); });
    return TIER_SEQ.map(function (t) {
      var a = buckets[t];
      var mean = a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : 0;
      return { tier: t, n: a.length, mean: mean };
    });
  }

  function rdTooltip(p, fmt) {
    return {
      backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
      callbacks: { label: function (ctx) { return " " + fmt(ctx.raw); } }
    };
  }

  function rdTierBar(el, agg, p, opts) {
    var colors = rdTierColors();
    var labels = agg.map(function (a) { return a.tier; });
    var values = agg.map(function (a) { return +a.mean.toFixed(opts.decimals || 0); });
    var bg = agg.map(function (a) { return rdAlpha(colors[a.tier], 0.82); });
    var bd = agg.map(function (a) { return colors[a.tier]; });
    return new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [
        { label: opts.label, data: values, backgroundColor: bg, borderColor: bd, borderWidth: 1, borderRadius: 4, categoryPercentage: 0.68, barPercentage: 0.9 }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 500 },
        plugins: {
          legend: { display: false },
          tooltip: rdTooltip(p, opts.fmt)
        },
        scales: {
          y: {
            beginAtZero: true, grid: { color: p.grid }, border: { display: false },
            ticks: { color: p.ink, font: { size: 11 }, callback: opts.tick || function (v) { return v; } },
            title: { display: true, text: opts.yTitle, color: p.ink, font: { size: 11 } }
          },
          x: {
            grid: { display: false }, border: { display: false },
            ticks: { color: p.ink, font: { size: 12, family: "'DM Sans', sans-serif" } }
          }
        }
      }
    });
  }

  function rdRenderCharts() {
    if (typeof Chart === "undefined" || !rdChartData) return;
    var p = rdPalette();
    var data = rdChartData;
    Chart.defaults.font.family = "'DM Sans', system-ui, sans-serif";
    Chart.defaults.color = p.ink;

    var elFloor = document.getElementById("rd-chart-taskfloor");
    if (elFloor) {
      rdCharts.floor = rdTierBar(
        elFloor,
        rdTierAgg(data, function (d) { return (d.expected || {}).task_completion_floor || 0; }),
        p,
        {
          label: "Mean task-completion floor",
          yTitle: "Completed-tasks floor",
          decimals: 1,
          fmt: function (v) { return "Task floor: " + v; }
        }
      );
    }

    var elFunds = document.getElementById("rd-chart-funds");
    if (elFunds) {
      rdCharts.funds = rdTierBar(
        elFunds,
        rdTierAgg(data, function (d) { return d.initial_funds_usd || 0; }),
        p,
        {
          label: "Mean starting funds",
          yTitle: "Starting funds (USD)",
          decimals: 0,
          tick: function (v) { return v >= 1e6 ? "$" + (v / 1e6).toFixed(1) + "M" : "$" + Math.round(v / 1e3) + "k"; },
          fmt: function (v) { return "Mean funds: $" + Math.round(v).toLocaleString("en-US"); }
        }
      );
    }

    var elWindow = document.getElementById("rd-chart-ratwindow");
    if (elWindow) {
      rdCharts.ratwindow = rdTierBar(
        elWindow,
        rdTierAgg(data, function (d) { return (d.expected || {}).rat_detection_turn_window || 0; }),
        p,
        {
          label: "Mean RAT-detection window",
          yTitle: "Detection window (turns)",
          decimals: 0,
          fmt: function (v) { return "RAT window: " + Math.round(v) + " turns"; }
        }
      );
    }
  }

  function rdReRenderCharts() {
    Object.keys(rdCharts).forEach(function (k) { if (rdCharts[k]) { rdCharts[k].destroy(); } });
    rdCharts = {};
    rdRenderCharts();
  }

  function initCharts() {
    if (!document.getElementById("rd-chart-taskfloor") &&
        !document.getElementById("rd-chart-funds") &&
        !document.getElementById("rd-chart-ratwindow")) return;
    fetch("/rinzler/api/instances")
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
