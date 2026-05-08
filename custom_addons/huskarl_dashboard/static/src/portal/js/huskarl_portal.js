(() => {
  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'huskarl:theme';
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
  const osChangeHandler = () => {
    try {
      if (localStorage.getItem(themeKey)) return;
    } catch (e) {}
    syncButtonLabel();
  };
  if (prefersDark.addEventListener) {
    prefersDark.addEventListener('change', osChangeHandler);
  } else if (prefersDark.addListener) {
    prefersDark.addListener(osChangeHandler);
  }

  const dataEl = document.getElementById('instances-data');
  if (!dataEl) return;
  const rows = JSON.parse(dataEl.textContent || '[]');

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

  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    const emEl = thesisEl.querySelector('em') || thesisEl.querySelector('.thesis-em');
    const emWords = emEl ? emEl.textContent.trim().toLowerCase().split(/\s+/) : [];
    const raw = thesisEl.textContent.trim();
    const words = raw.split(/\s+/);
    thesisEl.innerHTML = words.map((w) => {
      const punctMatch = w.match(/^(.*?)([.,!?;:]+)$/);
      const core  = punctMatch ? punctMatch[1] : w;
      const punct = punctMatch ? punctMatch[2] : '';
      const isEm  = emWords.includes(core.toLowerCase());
      const innerMarkup = isEm
        ? `<span class="thesis-em" style="font-style:normal;">${core}</span>${punct}`
        : w;
      return `<span class="thesis-word"><span class="thesis-word-inner">${innerMarkup}</span></span>`;
    }).join(' ');
  }

  const initAnimations = () => {
    if (prefersReduced) return;
    if (typeof window.gsap === 'undefined' || typeof window.ScrollTrigger === 'undefined') return;

    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    gsap.ticker.fps(60);
    gsap.defaults({ ease: EASE_OUT, duration: 0.64 });

    gsap.from('.wordmark', { y: 24, opacity: 0, duration: 0.9,  delay: 0.05, ease: EASE_OUT });
    gsap.from('.badge',    { y: 16, opacity: 0, duration: 0.7,  delay: 0.18, ease: EASE_OUT });
    gsap.from('.thesis-word-inner', {
      yPercent: 110,
      opacity:  0,
      duration: 0.9,
      stagger:  0.04,
      ease:     EASE_OUT,
      delay:    0.35,
    });

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

    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

  if (typeof window.gsap === 'undefined') {
    window.addEventListener('load', initAnimations, { once: true });
  } else {
    initAnimations();
  }

  const esc = (s) =>
    String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);

  const fmtTime = (s) => {
    const n = Number(s);
    if (!isFinite(n)) return '\u2014';
    return n >= 60 ? `${(n / 60).toFixed(1)}m` : `${n.toFixed(1)}s`;
  };

  const funnelNums = document.querySelectorAll('.funnel-num');
  const animateCount = (el) => {
    const target = Number(el.dataset.target || '0');
    const suffix = el.dataset.suffix || '';
    if (prefersReduced) {
      el.textContent = target.toLocaleString() + suffix;
      return;
    }
    const duration = 900;
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const val = Math.round(target * eased);
      el.textContent = val.toLocaleString() + (t === 1 ? suffix : '');
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  if ('IntersectionObserver' in window && funnelNums.length) {
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

  const outcomeClass = (passed) => passed ? 'tile-btn--pass' : 'tile-btn--fail';
  const outcomeLabel = (passed) => passed ? 'Pass' : 'Fail';

  const searchInput = document.querySelector('.hk-dataset__search');
  const filterSelect = document.querySelector('.hk-dataset__filter');
  const diffFilter = document.querySelector('.hk-dataset__diff-filter');
  const paginationEl = document.querySelector('.hk-dataset__pagination');
  let currentPage = 1;
  const pageSize = 20;
  let sortField = 'serial';
  let sortDir = 'asc';

  function getFiltered() {
    const search = (searchInput?.value || '').toLowerCase();
    const category = filterSelect?.value || '';
    const diff = diffFilter?.value || '';

    return rows.filter((inst) => {
      if (category && inst.category !== category) return false;
      if (diff && inst.difficulty !== diff) return false;
      if (search && inst.instance_id.toLowerCase().indexOf(search) === -1 &&
          inst.task_description.toLowerCase().indexOf(search) === -1 &&
          inst.skill_domain.toLowerCase().indexOf(search) === -1) return false;
      return true;
    });
  }

  function getSorted(data) {
    const sorted = data.slice();
    sorted.sort((a, b) => {
      let va, vb;
      switch (sortField) {
        case 'serial': va = a.serial; vb = b.serial; break;
        case 'instance_id': va = a.instance_id; vb = b.instance_id; break;
        case 'category': va = a.category; vb = b.category; break;
        case 'difficulty':
          const order = { 'Easy': 1, 'Medium': 2, 'Hard': 3 };
          va = order[a.difficulty] || 0; vb = order[b.difficulty] || 0; break;
        case 'glm5_result':
          va = a.models['GLM-5']?.passed ? 1 : 0;
          vb = b.models['GLM-5']?.passed ? 1 : 0; break;
        case 'glm5_time':
          va = a.models['GLM-5']?.time_seconds || 0;
          vb = b.models['GLM-5']?.time_seconds || 0; break;
        case 'nova_result':
          va = a.models['Nova 2 Lite']?.passed ? 1 : 0;
          vb = b.models['Nova 2 Lite']?.passed ? 1 : 0; break;
        case 'nova_time':
          va = a.models['Nova 2 Lite']?.time_seconds || 0;
          vb = b.models['Nova 2 Lite']?.time_seconds || 0; break;
        default: va = a.serial; vb = b.serial;
      }
      if (va < vb) return sortDir === 'asc' ? -1 : 1;
      if (va > vb) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return sorted;
  }

  let expandedId = null;

  function renderDetailRow(inst) {
    const item = (k, v) => `<div class="detail-row-item"><span class="detail-key">${esc(k)}</span><span class="detail-val">${v}</span></div>`;
    const glm = inst.models['GLM-5'] || {};
    const nova = inst.models['Nova 2 Lite'] || {};

    return `<tr class="detail-row" data-detail-for="${esc(inst.instance_id)}"><td colspan="9"><div class="detail-content"><div class="detail-grid">`
      + `<div class="detail-block"><div class="detail-block-title">Environment</div>`
        + item('Environment ID', esc(inst.instance_id))
        + item('Description', esc(inst.task_description))
        + item('Category', esc(inst.category))
        + item('Skill Domain', esc(inst.skill_domain))
        + item('Language', esc(inst.language))
        + item('Difficulty', esc(inst.difficulty))
      + `</div>`
      + `<div class="detail-block"><div class="detail-block-title">GLM-5</div>`
        + item('Result', `<b style="color:${glm.passed ? 'var(--pass)' : 'var(--fail)'}">${glm.passed ? 'PASS' : 'FAIL'}</b>`)
        + item('Time', fmtTime(glm.time_seconds))
      + `</div>`
      + `<div class="detail-block"><div class="detail-block-title">Nova-2-Lite</div>`
        + item('Result', `<b style="color:${nova.passed ? 'var(--pass)' : 'var(--fail)'}">${nova.passed ? 'PASS' : 'FAIL'}</b>`)
        + item('Time', fmtTime(nova.time_seconds))
      + `</div>`
      + `</div></div></td></tr>`;
  }

  function renderDataset() {
    const tb = document.getElementById('matrix-tbody');
    if (!tb) return;
    const filtered = getFiltered();
    const sorted = getSorted(filtered);
    const totalPages = Math.ceil(sorted.length / pageSize) || 1;
    if (currentPage > totalPages) currentPage = totalPages;
    const start = (currentPage - 1) * pageSize;
    const page = sorted.slice(start, start + pageSize);

    let html = '';
    page.forEach((inst) => {
      const tagStyle = 'display:inline-flex;align-items:center;justify-content:center;max-width:100%;min-height:28px;text-align:center;font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:0.03em;text-transform:uppercase;padding:3px 5px;border:1px solid currentColor;white-space:normal;line-height:1.3;box-sizing:border-box;';
      const diffColors = { easy:'color:var(--pass);', medium:'color:#D4A017;', hard:'color:var(--fail);' };
      const diffStyle = diffColors[(inst.difficulty || '').toLowerCase()] || 'color:var(--ink-3);';
      const glm = inst.models['GLM-5'] || {};
      const nova = inst.models['Nova 2 Lite'] || {};
      const isExpanded = inst.instance_id === expandedId;
      html += `
        <tr class="matrix-row${isExpanded ? ' row-expanded' : ''}" data-id="${esc(inst.instance_id)}">
          <td class="matrix-meta mono" style="font-size:11px;">${inst.serial}</td>
          <td class="matrix-id" style="font-size:12px;" title="${esc(inst.task_description)}">${esc(inst.instance_id)}</td>
          <td class="matrix-meta" style="font-size:11px;">${esc(inst.category)}</td>
          <td class="matrix-meta"><span style="${tagStyle}${diffStyle}">${esc(inst.difficulty)}</span></td>
          <td class="matrix-cell"><span class="tile-btn ${outcomeClass(glm.passed)}" style="${tagStyle}">${outcomeLabel(glm.passed)}</span></td>
          <td class="matrix-cell mono" style="font-size:11px;">${fmtTime(glm.time_seconds)}</td>
          <td class="matrix-cell"><span class="tile-btn ${outcomeClass(nova.passed)}" style="${tagStyle}">${outcomeLabel(nova.passed)}</span></td>
          <td class="matrix-cell mono" style="font-size:11px;">${fmtTime(nova.time_seconds)}</td>
          <td class="matrix-meta" style="font-size:11px;">${esc(inst.language)}</td>
        </tr>
      `;
      if (isExpanded) html += renderDetailRow(inst);
    });

    if (!page.length) {
      html = '<tr><td colspan="9" style="text-align:center;padding:2rem;color:var(--muted);">No instances match your filters</td></tr>';
    }

    tb.innerHTML = html;
    renderPagination(totalPages);
  }

  function renderPagination(totalPages) {
    if (!paginationEl) return;
    if (totalPages <= 1) { paginationEl.innerHTML = ''; return; }
    let html = '';
    html += `<button class="vt-btn" data-page="prev" ${currentPage === 1 ? 'disabled' : ''} style="padding:6px 12px;min-height:auto;">\u2190</button>`;
    for (let i = 1; i <= totalPages; i++) {
      html += `<button class="vt-btn ${i === currentPage ? 'is-active' : ''}" data-page="${i}" style="padding:6px 12px;min-height:auto;">${i}</button>`;
    }
    html += `<button class="vt-btn" data-page="next" ${currentPage === totalPages ? 'disabled' : ''} style="padding:6px 12px;min-height:auto;">\u2192</button>`;
    paginationEl.innerHTML = html;

    paginationEl.querySelectorAll('[data-page]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const p = btn.dataset.page;
        if (p === 'prev') currentPage = Math.max(1, currentPage - 1);
        else if (p === 'next') currentPage = Math.min(totalPages, currentPage + 1);
        else currentPage = parseInt(p);
        renderDataset();
      });
    });
  }

  document.querySelectorAll('[data-sort]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const field = th.dataset.sort;
      if (sortField === field) {
        sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        sortField = field;
        sortDir = 'asc';
      }
      currentPage = 1;
      renderDataset();
    });
  });

  if (searchInput) searchInput.addEventListener('input', () => { currentPage = 1; renderDataset(); });
  if (filterSelect) filterSelect.addEventListener('change', () => { currentPage = 1; renderDataset(); });
  if (diffFilter) diffFilter.addEventListener('change', () => { currentPage = 1; renderDataset(); });

  renderDataset();

  document.addEventListener('click', (e) => {
    const row = e.target.closest('#matrix tr[data-id]');
    if (!row) return;
    if (e.target.closest('a')) return;
    const id = row.dataset.id;
    expandedId = (expandedId === id) ? null : id;
    renderDataset();
  });

  const lightbox = document.getElementById('lightbox');
  const lightboxImg = document.getElementById('lightbox-img');
  const lightboxCaption = document.getElementById('lightbox-caption');
  const lightboxClose = document.getElementById('lightbox-close');
  let lastChartFocus = null;

  const openLightbox = (trigger) => {
    if (!lightbox) return;
    const imgs = trigger.querySelectorAll('img');
    let img = null;
    for (const candidate of imgs) {
      if (window.getComputedStyle(candidate).display !== 'none') {
        img = candidate;
        break;
      }
    }
    if (!img) img = imgs[0];
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

  document.querySelectorAll('.charts').forEach((grid) => {
    grid.addEventListener('click', (e) => {
      const trigger = e.target.closest('.chart-trigger');
      if (!trigger) return;
      e.preventDefault();
      openLightbox(trigger);
    });
  });
  lightboxClose?.addEventListener('click', closeLightbox);
  lightbox?.addEventListener('click', (e) => {
    if (e.target === lightbox) closeLightbox();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lightbox && !lightbox.hidden) closeLightbox();
  });

})();
