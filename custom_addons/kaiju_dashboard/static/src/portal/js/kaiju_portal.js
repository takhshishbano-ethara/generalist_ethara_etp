(() => {
  // ============================================================
  // Theme toggle — persists to localStorage, also reacts to OS
  // changes for users who never clicked. Runs before everything
  // else so the button works even if dataset loading fails.
  // ============================================================
  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'kaiju:theme';
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
      try { localStorage.setItem(themeKey, next); } catch (e) {}
      syncButtonLabel();
    });
  }
  // If the user has NOT made an explicit choice, follow OS changes live.
  const osChangeHandler = () => {
    try {
      if (localStorage.getItem(themeKey)) return; // user overrode — ignore OS
    } catch (e) {}
    // No saved choice: let the CSS OS fallback handle colors; we only
    // refresh the aria label/icon state to reflect the new OS value.
    syncButtonLabel();
  };
  if (prefersDark.addEventListener) {
    prefersDark.addEventListener('change', osChangeHandler);
  } else if (prefersDark.addListener) {
    prefersDark.addListener(osChangeHandler); // Safari < 14
  }

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ============================================================
  // Scroll progress rail — writes `width` into the fixed bar on
  // every animation frame while the page is scrolled. rAF-throttled
  // so we never fire more than 60 times/sec, and only while the
  // user is actively scrolling (idle state means zero work).
  // Gracefully no-ops if prefers-reduced-motion is on (the rail
  // still renders at 0→final, just without intra-scroll updates).
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
  // GSAP + ScrollTrigger — loaded via CDN in <head>. Wait for it.
  // Everything here is progressive: if GSAP fails to load or user
  // opts out of motion, the page still reads correctly.
  // ============================================================
  // Apple-style easing curves — same strings as CSS --ease-* tokens.
  // GSAP 3.11+ accepts CSS cubic-bezier() strings directly.
  const EASE_OUT = 'cubic-bezier(0.28, 0.11, 0.32, 1)';   // storytelling/entrances
  const EASE_UI  = 'cubic-bezier(0.25, 0.1, 0.25, 1)';    // micro
  const EASE_IN  = 'cubic-bezier(0.55, 0, 0.75, 0.25)';   // pulse tail

  // ============================================================
  // Thesis mask reveal — split the thesis sentence into per-word
  // spans wrapped in overflow-clip containers, so we can stagger
  // a yPercent 110 → 0 rise on load. Preserves the <em> italic on
  // "Scratch" by detecting word === 'scratch' during the split.
  //
  // Runs OUTSIDE initAnimations so the DOM is ready even if GSAP
  // isn't (fallback: flat text, no motion — page still reads).
  // ============================================================
  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    // Preserve the raw text; strip the single <em> wrapper before split
    // and re-apply as a class during the rebuild. Trailing punctuation
    // (like the "?" in "see?") stays roman — it's not italic in the
    // source markup, so we split it off the em word.
    const raw = thesisEl.textContent.trim();
    const words = raw.split(/\s+/);
    thesisEl.innerHTML = words.map((w) => {
      const punctMatch = w.match(/^(.*?)([.,!?;:]+)$/);
      const core  = punctMatch ? punctMatch[1] : w;
      const punct = punctMatch ? punctMatch[2] : '';
      const isEm  = core.toLowerCase() === 'scratch';
      const innerMarkup = isEm
        ? `<span class="thesis-em">${core}</span>${punct}`
        : w;
      return `<span class="thesis-word"><span class="thesis-word-inner">${innerMarkup}</span></span>`;
    }).join(' ');
  }

  const initAnimations = () => {
    if (prefersReduced) return;
    if (typeof window.gsap === 'undefined' || typeof window.ScrollTrigger === 'undefined') return;

    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    // Lock ticker to 60fps — conference projectors run 60Hz HDMI and
    // over-sampling causes visible jitter on scroll-triggered beats.
    gsap.ticker.fps(60);
    gsap.defaults({ ease: EASE_OUT, duration: 0.64 });

    // ---------- 1. Masthead on load ---------------------------------
    gsap.from('.wordmark', { y: 24, opacity: 0, duration: 0.9,  delay: 0.05, ease: EASE_OUT });
    gsap.from('.badge',    { y: 16, opacity: 0, duration: 0.7,  delay: 0.18, ease: EASE_OUT });
    // Thesis uses the line-by-line mask reveal below, not a single fade.
    // The static .thesis container still animates so the underline/rule
    // settles with the copy.
    gsap.from('.thesis-word-inner', {
      yPercent: 110,
      opacity:  0,
      duration: 0.9,
      stagger:  0.04,
      ease:     EASE_OUT,
      delay:    0.35,
    });

    // ---------- 2. Fade-rise on enter for every major section ------
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

    // ---------- 3. KPI card accent glow (one-shot) -----------------
    const css = getComputedStyle(document.documentElement);
    const accent2 = css.getPropertyValue('--accent-2').trim() || '#4A5515';
    const accentSurface = css.getPropertyValue('--accent-surface').trim() || '#EDF3B8';
    gsap.fromTo('.kpi-card--accent',
      { boxShadow: `inset 0 0 0 1px color-mix(in oklab, ${accent2} 30%, transparent)` },
      {
        boxShadow: `inset 0 0 0 1px ${accent2}, 0 0 30px ${accentSurface}`,
        duration: 0.8,
        delay: 0.4,
        ease: EASE_OUT,
        scrollTrigger: { trigger: '.kpi-card--accent', start: 'top 75%', once: true },
      }
    );

    // Re-measure on resize so triggers adapt to layout shifts
    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

  // ============================================================
  // KPI counter animation (IntersectionObserver) — easeOutCubic
  // progression, supports float targets via decimal detection
  // and data-suffix for units like "%".
  // ============================================================
  const kpiNums = document.querySelectorAll('.kpi-num');

  const animateCount = (el) => {
    const target = Number(el.dataset.target || '0');
    const suffix = el.dataset.suffix || '';
    const isFloat = String(el.dataset.target).indexOf('.') !== -1;

    if (prefersReduced) {
      el.textContent = (isFloat ? target.toFixed(1) : target.toLocaleString()) + suffix;
      return;
    }

    const duration = 900;
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - t, 3);
      const current = target * eased;

      if (t === 1) {
        el.textContent = (isFloat ? target.toFixed(1) : target.toLocaleString()) + suffix;
      } else {
        if (isFloat) {
          el.textContent = current.toFixed(1);
        } else {
          el.textContent = Math.round(current).toLocaleString();
        }
        requestAnimationFrame(step);
      }
    };
    requestAnimationFrame(step);
  };

  if ('IntersectionObserver' in window && kpiNums.length) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          animateCount(e.target);
          io.unobserve(e.target);
        }
      }
    }, { threshold: 0.3 });
    kpiNums.forEach((n) => io.observe(n));
  } else {
    kpiNums.forEach(animateCount);
  }

  // ============================================================
  // Lightbox — click any chart image → full-size preview. Close
  // via button, overlay click, or Escape. Body scroll locked.
  // ============================================================
  const lightbox = document.getElementById('lightbox');
  const lightboxImg = document.getElementById('lightbox-img');
  const lightboxCaption = document.getElementById('lightbox-caption');
  const lightboxClose = document.getElementById('lightbox-close');
  let lastChartFocus = null;

  const openLightbox = (trigger) => {
    if (!lightbox) return;
    const img = trigger.querySelector('img');
    const figcap = trigger.closest('figure')?.querySelector('figcaption');
    if (!img) return;
    lightboxImg.src = img.currentSrc || img.src;
    lightboxImg.alt = img.alt || '';
    lightboxCaption.textContent = figcap ? figcap.textContent.trim() : '';
    lightbox.hidden = false;
    requestAnimationFrame(() => lightbox.setAttribute('data-open', 'true'));
    lightbox.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    lastChartFocus = document.activeElement;
    lightboxClose?.focus();
  };
  const closeLightbox = () => {
    if (!lightbox || lightbox.hidden) return;
    lightbox.setAttribute('data-open', 'false');
    lightbox.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    setTimeout(() => {
      lightbox.hidden = true;
      lightboxImg.src = '';
    }, prefersReduced ? 0 : 180);
    if (lastChartFocus && lastChartFocus.focus) lastChartFocus.focus();
  };

  // Delegate clicks anywhere inside the .charts grid
  document.querySelector('.charts')?.addEventListener('click', (e) => {
    const trigger = e.target.closest('.chart-trigger');
    if (!trigger) return;
    e.preventDefault();
    openLightbox(trigger);
  });
  // Close triggers: X button, click on overlay (but not on the image),
  // Escape key.
  lightboxClose?.addEventListener('click', closeLightbox);
  lightbox?.addEventListener('click', (e) => {
    if (e.target === lightbox) closeLightbox();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lightbox && !lightbox.hidden) closeLightbox();
  });

  // ============================================================
  // Dataset Viewer
  // ============================================================
  var ROWS_PER_PAGE = 20;
  var allData = [];
  var filteredData = [];
  var currentPage = 1;
  var currentSort = 'instance_id';
  var currentSortDir = 1;
  var expandedId = null;

  function passFloat(val) {
    var n = parseFloat(val);
    return isNaN(n) ? 0 : n;
  }

  function passRateClass(rate) {
    if (rate >= 60) return 'kj-pass-rate-fill-high';
    if (rate >= 20) return 'kj-pass-rate-fill-mid';
    if (rate > 0) return 'kj-pass-rate-fill-low';
    return 'kj-pass-rate-fill-zero';
  }

  function diffClass(d) {
    if (d === 'Easy') return 'kj-diff-easy';
    if (d === 'Medium') return 'kj-diff-medium';
    if (d === 'Hard') return 'kj-diff-hard';
    return '';
  }

  function esc(str) {
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  function passRateCell(rate) {
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
      '<tr class="' + (isExpanded ? 'kj-row-expanded' : '') + '" data-id="' + esc(d.instance_id) + '">' +
        '<td class="kj-td-instance">' +
          '<span class="kj-viewer-instance-name">' + esc(d.instance_id) + '</span>' +
          (d.original_repo
            ? '<a class="kj-viewer-repo-link" href="' + esc(d.original_repo) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + esc(d.original_repo.replace('https://github.com/', '')) + '</a>'
            : '') +
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
    var tbody = document.getElementById('viewer-tbody');
    if (!tbody) return;

    var start = (currentPage - 1) * ROWS_PER_PAGE;
    var pageData = filteredData.slice(start, start + ROWS_PER_PAGE);
    var html = '';

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

    var countEl = document.getElementById('viewer-count');
    if (countEl) {
      countEl.textContent = filteredData.length + ' of ' + allData.length + ' instances';
    }

    renderPagination();
  }

  function renderPagination() {
    var container = document.getElementById('viewer-pagination');
    if (!container) return;

    var totalPages = Math.max(1, Math.ceil(filteredData.length / ROWS_PER_PAGE));
    if (totalPages <= 1) {
      container.innerHTML = '';
      return;
    }

    var html = '';
    html += '<button class="kj-page-btn" data-page="' + (currentPage - 1) + '"' + (currentPage <= 1 ? ' disabled' : '') + '>&lsaquo; Prev</button>';

    var pages = paginationRange(currentPage, totalPages);
    for (var i = 0; i < pages.length; i++) {
      var p = pages[i];
      if (p === '...') {
        html += '<span class="kj-page-ellipsis">&hellip;</span>';
      } else {
        html += '<button class="kj-page-btn' + (p === currentPage ? ' kj-page-active' : '') + '" data-page="' + p + '">' + p + '</button>';
      }
    }

    html += '<button class="kj-page-btn" data-page="' + (currentPage + 1) + '"' + (currentPage >= totalPages ? ' disabled' : '') + '>Next &rsaquo;</button>';

    container.innerHTML = html;
  }

  function paginationRange(current, total) {
    if (total <= 7) {
      var arr = [];
      for (var i = 1; i <= total; i++) arr.push(i);
      return arr;
    }
    var pages = [1];
    if (current > 3) pages.push('...');
    var rangeStart = Math.max(2, current - 1);
    var rangeEnd = Math.min(total - 1, current + 1);
    for (var j = rangeStart; j <= rangeEnd; j++) pages.push(j);
    if (current < total - 2) pages.push('...');
    pages.push(total);
    return pages;
  }

  function applyFilters() {
    var search = (document.getElementById('viewer-search').value || '').toLowerCase();
    var diff = document.getElementById('viewer-difficulty').value;

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

      av = (av || '').toString().toLowerCase();
      bv = (bv || '').toString().toLowerCase();
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
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

  function initDatasetViewer() {
    var tbody = document.getElementById('viewer-tbody');
    if (!tbody) return;

    fetch('/kaiju/api/instances')
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

    var searchEl = document.getElementById('viewer-search');
    if (searchEl) {
      searchEl.addEventListener('input', debounce(applyFilters, 250));
    }

    var diffEl = document.getElementById('viewer-difficulty');
    if (diffEl) {
      diffEl.addEventListener('change', applyFilters);
    }

    var sortEl = document.getElementById('viewer-sort');
    if (sortEl) {
      sortEl.addEventListener('change', function () {
        currentSort = this.value;
        currentSortDir = 1;
        applyFilters();
      });
    }

    var tableWrap = document.querySelector('.matrix-wrap');
    if (tableWrap) {
      tableWrap.addEventListener('click', function (e) {
        var row = e.target.closest('tr[data-id]');
        if (!row) return;
        var id = row.getAttribute('data-id');
        if (expandedId === id) {
          expandedId = null;
        } else {
          expandedId = id;
        }
        renderTable();
      });
    }

    var pagination = document.getElementById('viewer-pagination');
    if (pagination) {
      pagination.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-page]');
        if (!btn || btn.disabled) return;
        var page = parseInt(btn.getAttribute('data-page'), 10);
        var totalPages = Math.ceil(filteredData.length / ROWS_PER_PAGE);
        if (page >= 1 && page <= totalPages) {
          currentPage = page;
          expandedId = null;
          renderTable();
          var viewer = document.getElementById('instances');
          if (viewer) {
            viewer.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
        }
      });
    }
  }

  // ============================================================
  // Kick Off
  // ============================================================
  initDatasetViewer();

  // kick off after GSAP CDN loads (defer means it's after DOM ready)
  if (typeof window.gsap === 'undefined') {
    window.addEventListener('load', initAnimations, { once: true });
  } else {
    initAnimations();
  }
})();
