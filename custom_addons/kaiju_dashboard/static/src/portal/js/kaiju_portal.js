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
     §10 - DATASET VIEWER (Kaiju · library-generation tasks)
     Renders the dataset tasks with the model reward (auto-detected *_stage3), filterable
     by language and searchable by task / repo. Data: /kaiju/api/instances.
     =================================================================== */

  var EVAL_PER_PAGE = 10;
  var evalAllData = [];
  var evalFilteredData = [];
  var evalCurrentPage = 1;
  var evalCurrentSort = "opus_reward";
  var evalCurrentSortDir = -1;
  var evalExpandedId = null;

  // Prefix-agnostic model-field access: the dataset's reward fields may be
  // named glm5_* / opus48_* / claude48_* across builds; detect the "*_stage3"
  // key once and read every model value through it.
  var MODEL_PREFIX = null;
  function modelPrefix(d) {
    if (MODEL_PREFIX) return MODEL_PREFIX;
    if (d) { for (var k in d) { if (k.slice(-7) === "_stage3") { MODEL_PREFIX = k.slice(0, -7); return MODEL_PREFIX; } } }
    return MODEL_PREFIX || "claude48";
  }
  function mv(d, suf) { return d ? d[modelPrefix(d) + "_" + suf] : undefined; }
  function setupVer(d) {
    var L = (d.language || "").toLowerCase();
    if (d["setup_" + L]) return d["setup_" + L];
    for (var k in d) { if (k.indexOf("setup_") === 0 && k !== "setup_install" && k !== "setup_src_dir" && d[k]) return d[k]; }
    return "";
  }
  function kjNum(v) { return Number(v || 0).toLocaleString("en-US"); }
  function kjSetAll(cls, v) {
    var els = document.getElementsByClassName(cls);
    for (var i = 0; i < els.length; i++) els[i].textContent = v;
  }

  function langClass(lang) {
    var t = (lang || "").toLowerCase();
    if (t === "go") return "rd-tier-easy";
    if (t === "python") return "rd-tier-medium";
    if (t === "rust") return "rd-tier-hard";
    return "";
  }

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
      timer = setTimeout(function () { fn.apply(ctx, args); }, delay);
    };
  }

  function evalParsePass(str) {
    if (str === "" || str == null) return null;
    var n = parseFloat(String(str).replace("%", ""));
    return isNaN(n) ? null : n;
  }

  function evalRewardSort(d) {
    var v = evalParsePass(mv(d, "stage3"));
    return v === null ? -1 : v;
  }

  function fmtPct(v) {
    var n = evalParsePass(v);
    return n === null ? "N/A" : n.toFixed(2) + "%";
  }

  function fmtTime(sec) {
    var n = parseFloat(sec);
    if (isNaN(n) || !n) return "n/a";
    return (n / 60).toFixed(1) + " min";
  }

  function evalPassCell(val) {
    var pct = evalParsePass(val);
    if (pct === null) {
      return '<div class="rd-eval-pass-cell"><span class="rd-eval-pass-pct" style="color:var(--rd-text-muted)">N/A</span></div>';
    }
    return (
      '<div class="rd-eval-pass-cell">' +
        '<span class="rd-eval-pass-pct">' + pct.toFixed(2) + '%</span>' +
        '<div class="rd-eval-pass-bar">' +
          '<div class="rd-eval-pass-fill" style="width:' + pct + '%"></div>' +
        '</div>' +
      '</div>'
    );
  }

  function evalRenderRow(d, isExpanded) {
    return (
      '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-eval-id="' + esc(d.instance_id) + '">' +
        '<td class="rd-etd-instance"><span class="rd-eval-instance-name">' + esc(d.uuid || d.instance_id) + '</span></td>' +
        '<td class="rd-etd-prrange"><span class="rd-tier-badge ' + langClass(d.language) + '">' + esc(d.language || "") + '</span></td>' +
        '<td class="rd-etd-lang"><span class="rd-tier-badge ' + tierClass(d.difficulty) + '">' + esc(d.difficulty || "N/A") + '</span></td>' +
        '<td class="rd-etd-opus">' + evalPassCell(mv(d, "stage3")) + '</td>' +
        '<td class="rd-etd-repo"><span class="rd-eval-tests-count">' + esc(String(d.test_count || 0)) + '</span></td>' +
        '<td class="rd-etd-expand"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function evalRenderDetailRow(d) {
    function item(k, v) {
      return '<div class="rd-detail-row-item"><span class="rd-detail-key">' + esc(k) + '</span><span class="rd-detail-val">' + v + '</span></div>';
    }
    var links = "";
    if (d.original_repo) links += '<a class="rd-detail-link" href="' + esc(d.original_repo) + '" target="_blank" rel="noopener">Repository</a>';
    if (d.specification) links += '<a class="rd-detail-link" href="' + esc(d.specification) + '" target="_blank" rel="noopener">Specification</a>';
    return (
      '<tr class="rd-detail-row" data-eval-detail-for="' + esc(d.instance_id) + '">' +
        '<td colspan="6">' +
          '<div class="rd-detail-content">' +
            '<div class="rd-eval-detail-grid">' +
              '<div class="rd-detail-block">' +
                '<div class="rd-detail-block-title">Task Info</div>' +
                item("Language", esc(d.language || "")) +
                item("Difficulty", '<span class="rd-tier-badge ' + tierClass(d.difficulty) + '">' + esc(d.difficulty || "n/a") + '</span>') +
                item("Held-out Tests", esc(String(d.test_count || 0))) +
                item("Version", esc(setupVer(d) || "")) +
                item("Test Cmd", esc(d.test_cmd || "")) +
                item("Source Dir", esc(d.setup_src_dir || "")) +
              '</div>' +
              '<div class="rd-detail-block">' +
                '<div class="rd-detail-block-title">Opus 4.8</div>' +
                item("Reward", '<span style="font-weight:700">' + fmtPct(mv(d, "stage3")) + '</span>') +
                item("Stage 1", fmtPct(mv(d, "stage1"))) +
                item("Stage 2", fmtPct(mv(d, "stage2"))) +
                item("Stage 3", fmtPct(mv(d, "stage3"))) +
                item("Gen Time", fmtTime(mv(d, "time"))) +
              '</div>' +
            '</div>' +
            (links ? '<div class="rd-detail-cmds" style="margin-top:14px">' + links + '</div>' : '') +
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
      html = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--rd-text-muted)">No tasks found.</td></tr>';
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
    if (totalPages <= 1) { container.innerHTML = ""; return; }
    var html = "";
    html += '<button class="rd-page-btn" data-eval-page="' + (evalCurrentPage - 1) + '"' + (evalCurrentPage <= 1 ? " disabled" : "") + '>&lsaquo; Prev</button>';
    var pages = paginationRange(evalCurrentPage, totalPages);
    for (var i = 0; i < pages.length; i++) {
      var pg = pages[i];
      if (pg === "...") {
        html += '<span class="rd-page-ellipsis">&hellip;</span>';
      } else {
        html += '<button class="rd-page-btn' + (pg === evalCurrentPage ? " rd-page-active" : "") + '" data-eval-page="' + pg + '">' + pg + '</button>';
      }
    }
    html += '<button class="rd-page-btn" data-eval-page="' + (evalCurrentPage + 1) + '"' + (evalCurrentPage >= totalPages ? " disabled" : "") + '>Next &rsaquo;</button>';
    container.innerHTML = html;
  }

  function evalApplyFilters() {
    var search = (document.getElementById("rd-eval-search").value || "").toLowerCase();
    var tierSel = document.getElementById("rd-eval-difficulty").value;
    var langSel = document.getElementById("rd-eval-scope").value;
    evalFilteredData = evalAllData.filter(function (d) {
      if (tierSel && d.difficulty !== tierSel) return false;
      if (langSel && d.language !== langSel) return false;
      if (search) {
        var idMatch = (d.instance_id || "").toLowerCase().indexOf(search) !== -1;
        var uuidMatch = (d.uuid || "").toLowerCase().indexOf(search) !== -1;
        var repoMatch = (d.original_repo || "").toLowerCase().indexOf(search) !== -1;
        var langMatch = (d.language || "").toLowerCase().indexOf(search) !== -1;
        if (!idMatch && !uuidMatch && !repoMatch && !langMatch) return false;
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
        av = evalRewardSort(a); bv = evalRewardSort(b);
        return (av - bv) * dir;
      }
      if (key === "tests") {
        av = parseFloat(a.test_count) || 0; bv = parseFloat(b.test_count) || 0;
        return (av - bv) * dir;
      }
      av = (a[key] || "").toString().toLowerCase();
      bv = (b[key] || "").toString().toLowerCase();
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }

  function injectStats(data) {
    var n = data.length;
    var langs = {};
    data.forEach(function (d) { langs[d.language] = (langs[d.language] || 0) + 1; });
    var order = ["Go", "Python", "Rust"].filter(function (L) { return langs[L]; });
    Object.keys(langs).forEach(function (L) { if (order.indexOf(L) < 0) order.push(L); });
    var tests = data.reduce(function (a, d) { return a + (parseInt(d.test_count, 10) || 0); }, 0);
    var rew = [];
    data.forEach(function (d) { var r = rdReward(d); if (r !== null) rew.push(r); });
    var graded = rew.length;
    var mean = graded ? rew.reduce(function (a, x) { return a + x; }, 0) / graded : 0;
    var solved = 0, partial = 0, unsolved = 0;
    data.forEach(function (d) { var r = rdReward(d); if (r === null || r === 0) unsolved++; else if (r >= 50) solved++; else partial++; });
    function pc(x) { return n ? (x / n * 100).toFixed(1) + "%" : "0%"; }
    kjSetAll("js-tasks", n);
    kjSetAll("js-tests", kjNum(tests));
    kjSetAll("js-langs", order.length);
    kjSetAll("js-langbreak", order.map(function (L) { return langs[L]; }).join(" \u00b7 "));
    kjSetAll("js-langsub", order.map(function (L) { return langs[L] + " " + L; }).join(" \u00b7 "));
    kjSetAll("js-tests-sub", "across " + n + " real libraries");
    kjSetAll("js-mean", mean.toFixed(1) + "%");
    kjSetAll("js-meanlabel", "Claude Opus 4.8 Mean Reward (" + graded + " graded)");
    kjSetAll("js-solved", solved); kjSetAll("js-solved-pct", pc(solved));
    kjSetAll("js-partial", partial); kjSetAll("js-partial-pct", pc(partial));
    kjSetAll("js-unsolved", unsolved); kjSetAll("js-unsolved-pct", pc(unsolved));
    kjSetAll("js-reveal", "Reward spans the full range across the " + n + " tasks, with a graded mean of " + mean.toFixed(1) + "% over the " + graded + " scored instances (" + (n - graded) + " ungraded).");
    renderLangCards(data, order, langs);
  }

  function renderLangCards(data, order, langs) {
    var wrap = document.getElementById("kj-lang-cards");
    if (!wrap) return;
    function mean(arr) { return arr.length ? arr.reduce(function (a, x) { return a + x; }, 0) / arr.length : 0; }
    var html = "";
    order.forEach(function (L) {
      var a = data.filter(function (d) { return d.language === L; });
      var r = [], t = [];
      a.forEach(function (d) { var rr = rdReward(d); if (rr !== null) r.push(rr); var tt = rdTimeMin(d); if (tt > 0) t.push(tt); });
      var tests = a.reduce(function (x, d) { return x + (parseInt(d.test_count, 10) || 0); }, 0);
      var mr = r.length ? mean(r).toFixed(1) + "%" : "n/a";
      var mt = t.length ? "~" + Math.round(mean(t)) + " min" : "n/a";
      var rng = r.length ? Math.round(Math.min.apply(null, r)) + "% \u2013 " + Math.round(Math.max.apply(null, r)) + "%" : "ungraded";
      html +=
        '<div class="kpi-card" style="min-height: auto;">' +
          '<p class="kpi-label">' + esc(L) + ' \u00b7 ' + a.length + ' tasks</p>' +
          '<dl class="glance-list" style="margin-top: 12px;">' +
            '<div class="glance-row"><dt>Opus 4.8 mean</dt><dd><b>' + mr + '</b></dd></div>' +
            '<div class="glance-row"><dt>Reward range</dt><dd>' + rng + '</dd></div>' +
            '<div class="glance-row"><dt>Held-out tests</dt><dd><b>' + kjNum(tests) + '</b></dd></div>' +
            '<div class="glance-row"><dt>Mean gen time</dt><dd><b>' + mt + '</b></dd></div>' +
          '</dl>' +
        '</div>';
    });
    wrap.innerHTML = html;
    wrap.style.gridTemplateColumns = "repeat(" + order.length + ", 1fr)";
  }

  function initEvalViewer() {
    var tbody = document.getElementById("rd-eval-tbody");
    if (!tbody) return;

    fetch("/kaiju/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        evalAllData = data;
        evalFilteredData = data.slice();
        injectStats(data);
        evalSortData();
        evalRenderTable();
      })
      .catch(function () {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--rd-text-muted)">Failed to load task data.</td></tr>';
      });

    document.getElementById("rd-eval-search").addEventListener("input", debounce(evalApplyFilters, 250));
    document.getElementById("rd-eval-difficulty").addEventListener("change", evalApplyFilters);
    document.getElementById("rd-eval-scope").addEventListener("change", evalApplyFilters);

    document.getElementById("rd-eval-sort").addEventListener("change", function () {
      if (evalCurrentSort === this.value) {
        evalCurrentSortDir = evalCurrentSortDir * -1;
      } else {
        evalCurrentSort = this.value;
        evalCurrentSortDir = 1;
      }
      evalUpdateSortDirBtn();
      evalApplyFilters();
    });

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
          if (viewer) { viewer.scrollIntoView({ behavior: "smooth", block: "start" }); }
        }
      });
    }
  }

  // ============================================================
  // §12 - CHARTS (Chart.js, data-driven, theme-aware)
  // 3 charts from /kaiju/api/instances. Opus 4.8 = magenta (--accent).
  // Grouped by language. Re-renders on data-theme change.
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
      ink: rdCssVar("--ink", "#0B0E14"),
      muted: rdCssVar("--muted", "#8a8fa5"),
      grid: rdCssVar("--border", "rgba(0,0,0,0.10)"),
      surface: rdCssVar("--bg-2", "#ffffff")
    };
  }

  function rdReward(d) {
    var v = mv(d, "stage3");
    if (v === "" || v == null) return null;
    var n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  function rdTimeMin(d) {
    var n = parseFloat(mv(d, "time"));
    return isNaN(n) ? 0 : n / 60;
  }

  var LANGS = ["Go", "Python", "Rust"];

  function rdLangAgg(data) {
    return LANGS.map(function (L) {
      var a = data.filter(function (d) { return d.language === L; });
      var r = [], t = [];
      a.forEach(function (d) {
        var rr = rdReward(d); if (rr !== null) r.push(rr);
        var tt = rdTimeMin(d); if (tt > 0) t.push(tt);
      });
      function mean(arr) { return arr.length ? arr.reduce(function (s, x) { return s + x; }, 0) / arr.length : 0; }
      return { lang: L, n: a.length, reward: mean(r), graded: r.length, time: mean(t) };
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

  function rdLegend(p) {
    return { position: "top", align: "start", labels: { color: p.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "rectRounded", font: { family: "'DM Sans', sans-serif", size: 12 } } };
  }

  function rdTooltip(p, unit) {
    return {
      backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
      callbacks: { label: function (ctx) {
        var v = ctx.raw;
        var val = unit === "min" ? Number(v).toFixed(1) + " min" : Number(v).toFixed(1) + "%";
        return " " + ctx.dataset.label + ": " + val;
      } }
    };
  }

  function rdAxes(p, valMax, valTitle, horizontal) {
    var val = { grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: valTitle ? { display: true, text: valTitle, color: p.ink, font: { size: 11 } } : { display: false } };
    if (valMax) { val.max = valMax; val.beginAtZero = true; }
    var cat = { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: horizontal ? 10 : 12, family: horizontal ? "'SF Mono', monospace" : "'DM Sans', sans-serif" } } };
    return horizontal ? { x: val, y: cat } : { y: val, x: cat };
  }

  function rdBar(el, labels, vals, p, opts) {
    return new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [
        { label: "Opus 4.8", data: vals, backgroundColor: rdAlpha(p.opus, 0.5), borderColor: p.opus, borderWidth: 1, borderRadius: 3, categoryPercentage: opts.cat, barPercentage: 0.82 }
      ] },
      options: {
        indexAxis: opts.horizontal ? "y" : "x", responsive: true, maintainAspectRatio: false,
        animation: { duration: 500 },
        plugins: { legend: rdLegend(p), tooltip: rdTooltip(p, opts.unit) },
        scales: rdAxes(p, opts.valMax, opts.valTitle, opts.horizontal)
      }
    });
  }

  function rdLineChart(el, labels, vals, p, opts) {
    return new Chart(el, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "Opus 4.8", data: vals, borderColor: p.opus, backgroundColor: rdAlpha(p.opus, 0.1), borderWidth: 2.5, pointRadius: 4, pointHoverRadius: 6, pointBackgroundColor: p.opus, pointBorderColor: p.opus, tension: 0.3, fill: false }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 500 },
        plugins: { legend: rdLegend(p), tooltip: rdTooltip(p, opts.unit) },
        scales: {
          y: { beginAtZero: true, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: opts.valTitle, color: p.ink, font: { size: 11 } } },
          x: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 12, family: "'DM Sans', sans-serif" } } }
        }
      }
    });
  }

  function rdRuns(arr, keyFn) {
    var runs = [], cur = null;
    arr.forEach(function (d, i) {
      var k = keyFn(d);
      if (!cur || cur.key !== k) { cur = { key: k, start: i, end: i }; runs.push(cur); }
      else { cur.end = i; }
    });
    return runs;
  }

  function rdHalfBand(y) {
    var band = Math.abs(y.getPixelForTick(1) - y.getPixelForTick(0));
    if (!band) { band = (y.bottom - y.top) / Math.max(1, (y.ticks || []).length || 1); }
    return band / 2;
  }

  function rdPerTaskChart(el, s, p) {
    var labels = s.map(function (d) { return d.instance_id; });
    var vals = s.map(function (d) { var r = rdReward(d); return r === null ? 0 : +r.toFixed(1); });
    var langRuns = rdRuns(s, function (d) { return d.language; });
    var BAND = { Go: "rgba(94,230,149,0.10)", Python: "rgba(251,191,36,0.10)", Rust: "rgba(255,143,143,0.12)" };
    var TXT = { Go: "#3FB768", Python: "#B8860B", Rust: "#D2555A" };
    var groups = {
      id: "rdGroups",
      beforeDatasetsDraw: function (chart) {
        var y = chart.scales.y, area = chart.chartArea, ctx = chart.ctx;
        if (!y || !area) { return; }
        var half = rdHalfBand(y);
        langRuns.forEach(function (g) {
          var top = y.getPixelForTick(g.start) - half, bot = y.getPixelForTick(g.end) + half;
          ctx.save(); ctx.fillStyle = BAND[g.key] || "transparent";
          ctx.fillRect(area.left, top, area.right - area.left, bot - top); ctx.restore();
        });
      },
      afterDatasetsDraw: function (chart) {
        var y = chart.scales.y, area = chart.chartArea, ctx = chart.ctx;
        if (!y || !area) { return; }
        var half = rdHalfBand(y);
        ctx.save();
        ctx.textBaseline = "middle";
        langRuns.forEach(function (g, i) {
          var topPix = y.getPixelForTick(g.start) - half;
          if (i > 0) {
            ctx.strokeStyle = p.ink; ctx.globalAlpha = 0.5; ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(area.left, topPix); ctx.lineTo(area.right, topPix); ctx.stroke(); ctx.globalAlpha = 1;
          }
          var midY = (y.getPixelForTick(g.start) + y.getPixelForTick(g.end)) / 2;
          ctx.font = "700 11px 'SF Mono', monospace"; ctx.textAlign = "left";
          ctx.fillStyle = TXT[g.key] || p.ink;
          ctx.fillText(g.key.toUpperCase(), area.right + 12, midY);
        });
        ctx.restore();
      }
    };
    return new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [
        { label: "Opus 4.8", data: vals, backgroundColor: rdAlpha(p.opus, 0.5), borderColor: p.opus, borderWidth: 1, borderRadius: 3, minBarLength: 2, categoryPercentage: 0.82, barPercentage: 0.96 }
      ] },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        layout: { padding: { right: 84, top: 4 } },
        animation: { duration: 500 },
        plugins: {
          legend: rdLegend(p),
          tooltip: {
            backgroundColor: p.surface, titleColor: p.ink, bodyColor: p.ink, borderColor: p.grid, borderWidth: 1, padding: 10, cornerRadius: 8,
            callbacks: {
              title: function (items) { var d = s[items[0].dataIndex]; return d.instance_id + "  ·  " + d.language + "  ·  " + d.difficulty; },
              label: function (ctx) { var d = s[ctx.dataIndex]; var r = rdReward(d); return " Opus 4.8: " + (r === null ? "ungraded" : Number(ctx.parsed.x).toFixed(1) + "%"); }
            }
          }
        },
        scales: {
          x: { max: 100, beginAtZero: true, grid: { color: p.grid }, border: { display: false }, ticks: { color: p.ink, font: { size: 11 } }, title: { display: true, text: "Reward (%)", color: p.ink, font: { size: 11 } } },
          y: { grid: { display: false }, border: { display: false }, ticks: { color: p.ink, font: { size: 10, family: "'SF Mono', monospace" } } }
        }
      },
      plugins: [groups]
    });
  }

  function rdRenderCharts() {
    if (typeof Chart === "undefined" || !rdChartData) return;
    var p = rdPalette();
    var data = rdChartData;
    Chart.defaults.font.family = "'DM Sans', system-ui, sans-serif";
    Chart.defaults.color = p.ink;

    var elPT = document.getElementById("rd-chart-pertask");
    if (elPT) {
      var langOrd = { Go: 0, Python: 1, Rust: 2 };
      var s = data.slice().sort(function (a, b) {
        var lo = (langOrd[a.language] || 0) - (langOrd[b.language] || 0); if (lo) { return lo; }
        var ra = rdReward(a), rb = rdReward(b);
        return (rb === null ? -1 : rb) - (ra === null ? -1 : ra);
      });
      rdCharts.pertask = rdPerTaskChart(elPT, s, p);
    }

    var agg = rdLangAgg(data);
    var ll = agg.map(function (t) { return t.lang + " (" + t.n + ")"; });
    var elR = document.getElementById("rd-chart-reward-tier");
    if (elR) {
      rdCharts.rewardTier = rdBar(elR, ll,
        agg.map(function (t) { return +t.reward.toFixed(1); }),
        p, { horizontal: false, valMax: 100, valTitle: "Mean reward (%)", unit: "%", cat: 0.62 });
    }
    var elC = document.getElementById("rd-chart-cost-tier");
    if (elC) {
      rdCharts.costTier = rdLineChart(elC, ll,
        agg.map(function (t) { return +t.time.toFixed(1); }),
        p, { valTitle: "Mean gen time / run (min)", unit: "min" });
    }
  }

  function rdReRenderCharts() {
    Object.keys(rdCharts).forEach(function (k) { if (rdCharts[k]) { rdCharts[k].destroy(); } });
    rdCharts = {};
    rdRenderCharts();
  }

  function initCharts() {
    if (!document.getElementById("rd-chart-pertask")) return;
    fetch("/kaiju/api/instances")
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
