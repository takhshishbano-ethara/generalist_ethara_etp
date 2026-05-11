(() => {
  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'vesta:theme';
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
    toggleBtn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
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
  const osChangeHandler = () => {
    try { if (localStorage.getItem(themeKey)) return; } catch (e) {}
    syncButtonLabel();
  };
  if (prefersDark.addEventListener) {
    prefersDark.addEventListener('change', osChangeHandler);
  } else if (prefersDark.addListener) {
    prefersDark.addListener(osChangeHandler);
  }

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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
    const onScroll = () => { if (ticking) return; ticking = true; requestAnimationFrame(update); };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    update();
  })();

  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    const raw = thesisEl.textContent.trim();
    const words = raw.split(/\s+/);
    thesisEl.innerHTML = words.map((w) => {
      const punctMatch = w.match(/^(.*?)([.,!?;:]+)$/);
      const core = punctMatch ? punctMatch[1] : w;
      const punct = punctMatch ? punctMatch[2] : '';
      const isEm = core.toLowerCase() === 'attack';
      const innerMarkup = isEm
        ? '<span class="thesis-em">' + core + '</span>' + punct
        : w;
      return '<span class="thesis-word"><span class="thesis-word-inner">' + innerMarkup + '</span></span>';
    }).join(' ');
  }

  const EASE_OUT = 'cubic-bezier(0.28, 0.11, 0.32, 1)';

  const initAnimations = () => {
    if (prefersReduced) return;
    if (typeof window.gsap === 'undefined' || typeof window.ScrollTrigger === 'undefined') return;

    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    gsap.ticker.fps(60);
    gsap.defaults({ ease: EASE_OUT, duration: 0.64 });

    gsap.from('.wordmark', { y: 24, opacity: 0, duration: 0.9, delay: 0.05, ease: EASE_OUT });
    gsap.from('.badge', { y: 16, opacity: 0, duration: 0.7, delay: 0.18, ease: EASE_OUT });
    gsap.from('.thesis-word-inner', {
      yPercent: 110, opacity: 0, duration: 0.9, stagger: 0.04, ease: EASE_OUT, delay: 0.35,
    });

    document.querySelectorAll('main > .section').forEach((section) => {
      const children = section.querySelectorAll(':scope > *');
      gsap.from(children, {
        y: 28, opacity: 0, stagger: 0.08, duration: 0.64, ease: EASE_OUT,
        scrollTrigger: { trigger: section, start: 'top 85%', toggleActions: 'play none none none' },
        onStart() { children.forEach((el) => { el.style.willChange = 'transform, opacity'; }); },
        onComplete() { children.forEach((el) => { el.style.willChange = ''; }); },
      });
    });

    const css = getComputedStyle(document.documentElement);
    const accent2 = css.getPropertyValue('--accent-2').trim() || '#AA00AA';
    const accentSurface = css.getPropertyValue('--accent-surface').trim() || 'rgba(238,0,238,0.10)';
    gsap.fromTo('.kpi-card--accent',
      { boxShadow: 'inset 0 0 0 1px color-mix(in oklab, ' + accent2 + ' 30%, transparent)' },
      {
        boxShadow: 'inset 0 0 0 1px ' + accent2 + ', 0 0 30px ' + accentSurface,
        duration: 0.8, delay: 0.4, ease: EASE_OUT,
        scrollTrigger: { trigger: '.kpi-card--accent', start: 'top 75%', once: true },
      }
    );

    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

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
      const eased = 1 - Math.pow(1 - t, 3);
      const current = target * eased;
      if (t === 1) {
        el.textContent = (isFloat ? target.toFixed(1) : target.toLocaleString()) + suffix;
      } else {
        el.textContent = isFloat ? current.toFixed(1) : Math.round(current).toLocaleString();
        requestAnimationFrame(step);
      }
    };
    requestAnimationFrame(step);
  };

  if ('IntersectionObserver' in window && kpiNums.length) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { animateCount(e.target); io.unobserve(e.target); }
      }
    }, { threshold: 0.3 });
    kpiNums.forEach((n) => io.observe(n));
  } else {
    kpiNums.forEach(animateCount);
  }

  var ROWS_PER_PAGE = 20;
  var allData = [];
  var filteredData = [];
  var currentPage = 1;
  var expandedId = null;

  function esc(str) { var d = document.createElement('div'); d.textContent = str; return d.innerHTML; }

  function pressureClass(p) {
    if (p === 'No adversary') return 'type-nonnpc';
    if (p === 'Single adversary') return 'type-singlenpc';
    if (p === 'Coordinated') return 'type-multinpc';
    return '';
  }

  function safetyClass(val) {
    if (val === 'safe') return 'safety-safe';
    if (val === 'Unsafe') return 'safety-unsafe';
    return 'safety-error';
  }

  function renderRow(d) {
    var isExpanded = expandedId === d.instance_id;
    return (
      '<tr class="' + (isExpanded ? 'dr-row-expanded' : '') + '" data-id="' + esc(d.instance_id) + '">' +
        '<td style="font-size:13px;color:var(--ink)">' + esc(d.instance_id) + '</td>' +
        '<td><span class="type-badge ' + pressureClass(d.pressure) + '">' + esc(d.pressure) + '</span></td>' +
        '<td style="font-size:12px;color:var(--ink-3)">' + esc(d.services) + '</td>' +
        '<td><span class="safety-badge ' + safetyClass(d.glm5) + '">' + esc(d.glm5) + '</span></td>' +
        '<td><span class="safety-badge ' + safetyClass(d.nova) + '">' + esc(d.nova) + '</span></td>' +
        '<td class="dr-td-expand"><span class="dr-expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>' +
      '</tr>'
    );
  }

  function renderDetailRow(d) {
    return (
      '<tr class="dr-detail-row" data-detail-for="' + esc(d.instance_id) + '">' +
        '<td colspan="6">' +
          '<div class="dr-detail-content">' +
            '<p style="font-size:14px;color:var(--ink-2);margin-bottom:16px;line-height:1.6">' + esc(d.premise) + '</p>' +
            '<div class="dr-detail-grid">' +
              '<div class="dr-detail-block">' +
                '<div class="dr-detail-block-title">Model Results</div>' +
                '<div class="dr-detail-row-item"><span class="dr-detail-key">GLM-5</span><span class="dr-detail-val"><span class="safety-badge ' + safetyClass(d.glm5) + '">' + esc(d.glm5) + '</span></span></div>' +
                '<div class="dr-detail-row-item"><span class="dr-detail-key">Kimi-K2.5</span><span class="dr-detail-val"><span class="safety-badge ' + safetyClass(d.nova) + '">' + esc(d.nova) + '</span></span></div>' +
              '</div>' +
              '<div class="dr-detail-block">' +
                '<div class="dr-detail-block-title">Environment Info</div>' +
                '<div class="dr-detail-row-item"><span class="dr-detail-key">Pressure</span><span class="dr-detail-val">' + esc(d.pressure) + '</span></div>' +
                '<div class="dr-detail-row-item"><span class="dr-detail-key">Services</span><span class="dr-detail-val">' + esc(d.services) + '</span></div>' +
              '</div>' +
            '</div>' +
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
      if (expandedId === pageData[i].instance_id) { html += renderDetailRow(pageData[i]); }
    }
    if (pageData.length === 0) {
      html = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--muted)">No environments found.</td></tr>';
    }
    tbody.innerHTML = html;
    var countEl = document.getElementById('viewer-count');
    if (countEl) { countEl.textContent = filteredData.length + ' of ' + allData.length + ' environments'; }
    renderPagination();
  }

  function renderPagination() {
    var container = document.getElementById('viewer-pagination');
    if (!container) return;
    var totalPages = Math.max(1, Math.ceil(filteredData.length / ROWS_PER_PAGE));
    if (totalPages <= 1) { container.innerHTML = ''; return; }
    var html = '';
    html += '<button class="dr-page-btn" data-page="' + (currentPage - 1) + '"' + (currentPage <= 1 ? ' disabled' : '') + '>&lsaquo; Prev</button>';
    var pages = paginationRange(currentPage, totalPages);
    for (var i = 0; i < pages.length; i++) {
      var p = pages[i];
      if (p === '...') { html += '<span class="dr-page-ellipsis">&hellip;</span>'; }
      else { html += '<button class="dr-page-btn' + (p === currentPage ? ' dr-page-active' : '') + '" data-page="' + p + '">' + p + '</button>'; }
    }
    html += '<button class="dr-page-btn" data-page="' + (currentPage + 1) + '"' + (currentPage >= totalPages ? ' disabled' : '') + '>Next &rsaquo;</button>';
    container.innerHTML = html;
  }

  function paginationRange(current, total) {
    if (total <= 7) { var arr = []; for (var i = 1; i <= total; i++) arr.push(i); return arr; }
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
    var pressureFilter = document.getElementById('viewer-type').value;
    filteredData = allData.filter(function (d) {
      if (pressureFilter && d.pressure !== pressureFilter) return false;
      if (search && d.instance_id.toLowerCase().indexOf(search) === -1 &&
          d.premise.toLowerCase().indexOf(search) === -1 &&
          d.services.toLowerCase().indexOf(search) === -1) return false;
      return true;
    });
    currentPage = 1;
    expandedId = null;
    renderTable();
  }

  function debounce(fn, delay) {
    var timer;
    return function () { var ctx = this; var args = arguments; clearTimeout(timer); timer = setTimeout(function () { fn.apply(ctx, args); }, delay); };
  }

  function initTaskExplorer() {
    var tbody = document.getElementById('viewer-tbody');
    if (!tbody) return;

    fetch('/vesta/api/tasks')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        allData = data;
        filteredData = data.slice();
        renderTable();
      })
      .catch(function () {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--muted)">Failed to load environments.</td></tr>';
      });

    var searchEl = document.getElementById('viewer-search');
    if (searchEl) { searchEl.addEventListener('input', debounce(applyFilters, 250)); }

    var typeEl = document.getElementById('viewer-type');
    if (typeEl) { typeEl.addEventListener('change', applyFilters); }

    var tableEl = document.getElementById('viewer-table');
    if (tableEl) {
      tableEl.addEventListener('click', function (e) {
        var row = e.target.closest('tr[data-id]');
        if (!row) return;
        var id = row.getAttribute('data-id');
        expandedId = (expandedId === id) ? null : id;
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
          var viewer = document.getElementById('explorer');
          if (viewer) { viewer.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
        }
      });
    }
  }

  initTaskExplorer();

  if (typeof window.gsap === 'undefined') {
    window.addEventListener('load', initAnimations, { once: true });
  } else {
    initAnimations();
  }
})();
