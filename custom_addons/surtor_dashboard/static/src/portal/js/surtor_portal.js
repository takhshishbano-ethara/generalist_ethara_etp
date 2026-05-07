(() => {
  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'surtor:theme';
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
  const instances = JSON.parse(dataEl.textContent || '[]');

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
  const EASE_UI  = 'cubic-bezier(0.25, 0.1, 0.25, 1)';
  const EASE_IN  = 'cubic-bezier(0.55, 0, 0.75, 0.25)';

  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    const raw = thesisEl.textContent.trim();
    const words = raw.split(/\s+/);
    thesisEl.innerHTML = words.map((w) => {
      const punctMatch = w.match(/^(.*?)([.,!?;:]+)$/);
      const core  = punctMatch ? punctMatch[1] : w;
      const punct = punctMatch ? punctMatch[2] : '';
      const isEm  = core.toLowerCase() === 'specification';
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

  const searchInput = document.querySelector('.kr-dataset__search');
  const filterSelect = document.querySelector('.kr-dataset__filter');
  const paginationEl = document.querySelector('.kr-dataset__pagination');
  let currentPage = 1;
  const pageSize = 10;
  let sortField = 'case_id';
  let sortDir = 'asc';

  function getTypeLabel(inst) {
    if (inst.type === 'ipi') return 'IPI';
    if (inst.type === 'memory_credential') return 'MEM';
    if (inst.type === 'trd') return 'TRD';
    return inst.type || '—';
  }

  function scoreColor(score) {
    if (score === 3) return 'var(--fail)';
    if (score === 0) return 'var(--pass)';
    return 'var(--accent)';
  }

  function scoreLabel(score) {
    if (score === 3) return 'FAIL';
    if (score === 0) return 'PASS';
    return 'PARTIAL';
  }

  function getFiltered() {
    const search = (searchInput?.value || '').toLowerCase();
    const typeFilter = filterSelect?.value || '';

    return instances.filter((inst) => {
      if (typeFilter && inst.type !== typeFilter) return false;
      if (search) {
        const matchId = (inst.case_id || '').toLowerCase().indexOf(search) !== -1;
        const matchTechnique = (inst.technique || '').toLowerCase().indexOf(search) !== -1;
        if (!matchId && !matchTechnique) return false;
      }
      return true;
    });
  }

  function getSorted(data) {
    const sorted = data.slice();
    sorted.sort((a, b) => {
      let va, vb;
      switch (sortField) {
        case 'case_id': va = a.case_id || ''; vb = b.case_id || ''; break;
        case 'type': va = getTypeLabel(a); vb = getTypeLabel(b); break;
        case 'technique': va = a.technique || ''; vb = b.technique || ''; break;
        case 'glm_score': va = a.glm_score || 0; vb = b.glm_score || 0; break;
        case 'nova_score': va = a.nova_score || 0; vb = b.nova_score || 0; break;
        default: va = a.case_id || ''; vb = b.case_id || '';
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

    let html = `<tr class="detail-row" data-detail-for="${esc(inst.case_id)}"><td colspan="7"><div class="detail-content"><div class="detail-grid">`;

    if (inst.type === 'ipi') {
      html += `<div class="detail-block"><div class="detail-block-title">IPI Attack Details</div>`
        + item('Technique', esc(inst.technique))
        + item('Target Tool', esc(inst.target_tool))
        + item('Carrier File', esc(inst.carrier_file))
        + item('Multi-Turn', inst.multi_turn ? 'Yes' : 'No')
      + `</div>`;
    } else if (inst.type === 'memory_credential') {
      html += `<div class="detail-block"><div class="detail-block-title">Memory Attack Details</div>`
        + item('Technique', esc(inst.technique))
        + item('Credential Type', esc(inst.credential_type))
        + item('Credential Category', esc(inst.credential_category))
        + item('Difficulty', esc(inst.difficulty))
      + `</div>`;
    } else if (inst.type === 'trd') {
      html += `<div class="detail-block"><div class="detail-block-title">TRD Attack Details</div>`
        + item('Technique', esc(inst.technique))
        + item('Target Tool', esc(inst.target_tool))
        + item('Target Domain', esc(inst.target_domain || ''))
        + item('Poisoned Tool', esc(inst.poisoned_tool || ''))
        + item('Difficulty', esc(inst.difficulty))
        + item('Multi-Turn', inst.multi_turn ? 'Yes' : 'No')
      + `</div>`;
    }

    html += `<div class="detail-block"><div class="detail-block-title">Model Results</div>`
      + item('GLM-5', '<span style="color:' + scoreColor(inst.glm_score) + ';font-weight:600;">' + scoreLabel(inst.glm_score) + ' (' + inst.glm_score + '/3)</span>')
      + item('Nova-2-Lite', '<span style="color:' + scoreColor(inst.nova_score) + ';font-weight:600;">' + scoreLabel(inst.nova_score) + ' (' + inst.nova_score + '/3)</span>')
      + item('GLM-5 Cost', '$' + (inst.glm_cost || 0).toFixed(4))
      + item('Nova Cost', '$' + (inst.nova_cost || 0).toFixed(4))
      + (inst.type === 'memory_credential' ? item('GLM-5 Leaked', inst.glm_leaked ? 'Yes' : 'No') + item('Nova Leaked', inst.nova_leaked ? 'Yes' : 'No') : '')
    + `</div>`;

    html += `</div></div></td></tr>`;
    return html;
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
    page.forEach((inst, i) => {
      const typeLabel = getTypeLabel(inst);
      const isExpanded = inst.case_id === expandedId;

      const tagStyle = 'display:inline-flex;align-items:center;justify-content:center;min-height:28px;text-align:center;font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:0.03em;text-transform:uppercase;padding:3px 8px;border:1px solid currentColor;white-space:nowrap;line-height:1.3;box-sizing:border-box;';
      const typeColor = typeLabel === 'IPI' ? 'color:var(--fail);' : 'color:#7c3aed;';

      const glmFlag = `<span style="${tagStyle}color:${scoreColor(inst.glm_score)};">${scoreLabel(inst.glm_score)}</span>`;
      const novaFlag = `<span style="${tagStyle}color:${scoreColor(inst.nova_score)};">${scoreLabel(inst.nova_score)}</span>`;

      html += `
        <tr class="matrix-row${isExpanded ? ' row-expanded' : ''}" data-id="${esc(inst.case_id)}">
          <td class="matrix-id"><span class="num mono">${String(start + i + 1).padStart(2, '0')}</span></td>
          <td class="matrix-id">${esc((inst.case_id || '').toUpperCase())}</td>
          <td class="matrix-cell"><span style="${tagStyle}${typeColor}">${esc(typeLabel)}</span></td>
          <td class="matrix-id">${esc(inst.technique)}</td>
          <td class="matrix-cell">${glmFlag}</td>
          <td class="matrix-cell">${novaFlag}</td>
          <td class="matrix-cell"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>
        </tr>
      `;
      if (isExpanded) html += renderDetailRow(inst);
    });

    if (!page.length) {
      html = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--muted);">No scenarios match your filters</td></tr>';
    }

    tb.innerHTML = html;
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
        else currentPage = parseInt(p);
        renderDataset();
      });
    });
  }

  let activeSortTh = null;

  document.querySelectorAll('[data-sort]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.style.userSelect = 'none';
    th.dataset.label = th.textContent.trim();
    th.addEventListener('click', () => {
      const field = th.dataset.sort;
      if (activeSortTh === th) {
        sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        sortField = field;
        sortDir = 'asc';
        activeSortTh = th;
      }
      currentPage = 1;
      updateSortIndicators();
      renderDataset();
    });
  });

  function updateSortIndicators() {
    document.querySelectorAll('[data-sort]').forEach((th) => {
      const label = th.dataset.label;
      if (th === activeSortTh) {
        const arrow = sortDir === 'asc' ? ' \u2191' : ' \u2193';
        th.innerHTML = label + '<span style="color:var(--fail);margin-left:3px;">' + arrow + '</span>';
      } else {
        th.textContent = label;
      }
    });
  }
  activeSortTh = document.querySelector('[data-sort="case_id"]');
  updateSortIndicators();

  if (searchInput) searchInput.addEventListener('input', () => { currentPage = 1; renderDataset(); });
  if (filterSelect) filterSelect.addEventListener('change', () => { currentPage = 1; renderDataset(); });

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
    const img = trigger.querySelector('img:not([style*="display: none"]):not(.chart-dark):not(.chart-light)') ||
                trigger.querySelector('img.chart-dark:not([style*="display: none"])') ||
                trigger.querySelector('img.chart-light:not([style*="display: none"])') ||
                Array.from(trigger.querySelectorAll('img')).find(i => getComputedStyle(i).display !== 'none') ||
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
