(() => {
  "use strict";

  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'castiel:theme';
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

  // ---------- scroll progress rail ----------
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

  const EASE_OUT = 'cubic-bezier(0.28, 0.11, 0.32, 1)';

  // ---------- thesis word-mask reveal ----------
  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    const raw = thesisEl.textContent.trim();
    const words = raw.split(/\s+/);
    thesisEl.innerHTML = words.map((w) => {
      // Split trailing punctuation so it stays upright (and unclipped) even
      // when the core word is italicised.
      const m = w.match(/^(.*?)([.,!?;:]+)$/);
      const core = m ? m[1] : w;
      const punct = m ? m[2] : '';
      const isEm = /curriculum/i.test(core);
      const inner = isEm ? `<span class="thesis-em">${core}</span>${punct}` : w;
      return `<span class="thesis-word"><span class="thesis-word-inner">${inner}</span></span>`;
    }).join(' ');
  }

  // ---------- GSAP scroll reveals ----------
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

    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

  if (typeof window.gsap === 'undefined') {
    window.addEventListener('load', initAnimations, { once: true });
  } else {
    initAnimations();
  }

  // ---------- data ----------
  const esc = (s) =>
    String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);

  let DATA = {};
  const dataEl = document.getElementById('castiel-data');
  if (dataEl) {
    try { DATA = JSON.parse(dataEl.textContent || '{}'); } catch (e) { DATA = {}; }
  }

  // ---------- §06 leaderboard table (CyberGym models) ----------
  const models = Array.isArray(DATA.cybergym_models) ? DATA.cybergym_models.slice() : [];
  const maxSuccess = models.reduce((m, r) => Math.max(m, Number(r.success) || 0), 0) || 1;

  const searchInput = document.querySelector('.ct-table__search');
  const tbody = document.getElementById('lb-tbody');
  const paginationEl = document.querySelector('.ct-table__pagination');

  let currentPage = 1;
  const pageSize = 8;
  let sortField = 'success';
  let sortDir = 'desc';

  function getFiltered() {
    const q = (searchInput?.value || '').toLowerCase();
    return models.filter((r) => !q || String(r.model).toLowerCase().indexOf(q) !== -1);
  }

  function getSorted(data) {
    const sorted = data.slice();
    sorted.sort((a, b) => {
      let va, vb;
      if (sortField === 'model') { va = a.model; vb = b.model; }
      else { va = Number(a.success) || 0; vb = Number(b.success) || 0; }
      if (va < vb) return sortDir === 'asc' ? -1 : 1;
      if (va > vb) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return sorted;
  }

  function renderTable() {
    if (!tbody) return;
    const sorted = getSorted(getFiltered());
    const totalPages = Math.ceil(sorted.length / pageSize) || 1;
    if (currentPage > totalPages) currentPage = totalPages;
    const start = (currentPage - 1) * pageSize;
    const page = sorted.slice(start, start + pageSize);

    let html = '';
    page.forEach((r, i) => {
      const pct = ((Number(r.success) || 0) / maxSuccess * 100).toFixed(1);
      html += `
        <tr class="matrix-row">
          <td class="matrix-id"><span class="num mono">#${String(start + i + 1).padStart(2, '0')}</span> ${esc(r.model)}</td>
          <td class="matrix-meta">${Number(r.success).toFixed(1)}%</td>
          <td class="matrix-cell">
            <span class="ct-bar"><span class="ct-bar__fill" style="width:${pct}%"></span></span>
          </td>
        </tr>`;
    });
    if (!page.length) {
      html = '<tr><td colspan="3" style="text-align:center;padding:2rem;color:var(--muted);">No models match your search</td></tr>';
    }
    tbody.innerHTML = html;
    renderPagination(totalPages);
  }

  function renderPagination(totalPages) {
    if (!paginationEl) return;
    if (totalPages <= 1) { paginationEl.innerHTML = ''; return; }
    let html = '';
    html += `<button class="vt-btn" data-page="prev" ${currentPage === 1 ? 'disabled' : ''} style="padding:6px 12px;min-height:auto;">←</button>`;
    for (let i = 1; i <= totalPages; i++) {
      html += `<button class="vt-btn ${i === currentPage ? 'is-active' : ''}" data-page="${i}" style="padding:6px 12px;min-height:auto;">${i}</button>`;
    }
    html += `<button class="vt-btn" data-page="next" ${currentPage === totalPages ? 'disabled' : ''} style="padding:6px 12px;min-height:auto;">→</button>`;
    paginationEl.innerHTML = html;
    paginationEl.querySelectorAll('[data-page]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const p = btn.dataset.page;
        if (p === 'prev') currentPage = Math.max(1, currentPage - 1);
        else if (p === 'next') currentPage = Math.min(totalPages, currentPage + 1);
        else currentPage = parseInt(p, 10);
        renderTable();
      });
    });
  }

  document.querySelectorAll('[data-sort]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const field = th.dataset.sort;
      if (sortField === field) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      else { sortField = field; sortDir = field === 'model' ? 'asc' : 'desc'; }
      currentPage = 1;
      renderTable();
    });
  });
  if (searchInput) searchInput.addEventListener('input', () => { currentPage = 1; renderTable(); });

  renderTable();

  // ---------- chart lightbox ----------
  const lightbox = document.getElementById('lightbox');
  const lightboxImg = document.getElementById('lightbox-img');
  const lightboxCaption = document.getElementById('lightbox-caption');
  const lightboxClose = document.getElementById('lightbox-close');
  let lastChartFocus = null;

  const openLightbox = (trigger) => {
    if (!lightbox) return;
    const img = Array.from(trigger.querySelectorAll('img')).find(i => getComputedStyle(i).display !== 'none') ||
                trigger.querySelector('img');
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
    setTimeout(() => { lightbox.hidden = true; lightboxImg.src = ''; }, prefersReduced ? 0 : 180);
    if (lastChartFocus && lastChartFocus.focus) lastChartFocus.focus();
  };

  document.querySelectorAll('.charts').forEach((grid) => {
    grid.addEventListener('click', (e) => {
      const trigger = e.target.closest('.chart-trigger');
      if (!trigger) return;
      e.preventDefault();
      openLightbox(trigger);
    });
  });
  lightboxClose?.addEventListener('click', closeLightbox);
  lightbox?.addEventListener('click', (e) => { if (e.target === lightbox) closeLightbox(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lightbox && !lightbox.hidden) closeLightbox();
  });
})();
