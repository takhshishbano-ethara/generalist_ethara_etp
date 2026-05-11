(() => {
  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'kraken:theme';
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
      const isEm  = core.toLowerCase() === 'experts';
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

    const css = getComputedStyle(document.documentElement);
    const accent2 = css.getPropertyValue('--accent-2').trim() || '#4A5515';
    const accentSurface = css.getPropertyValue('--accent-surface').trim() || '#EDF3B8';
    gsap.fromTo('.funnel-step--final',
      { boxShadow: `inset 0 0 0 1px color-mix(in oklab, ${accent2} 30%, transparent)` },
      {
        boxShadow: `inset 0 0 0 1px ${accent2}, 0 0 30px ${accentSurface}`,
        duration: 0.8,
        delay: 0.4,
        ease: EASE_OUT,
        scrollTrigger: { trigger: '.funnel', start: 'top 75%', once: true },
      }
    );

    ScrollTrigger.create({
      trigger: '#view-matrix',
      start: 'top 75%',
      once: true,
      onEnter: () => {
        const tiles = Array.from(document.querySelectorAll('.tile-btn--pass')).slice(0, 8);
        tiles.forEach((el, i) => {
          const tl = gsap.timeline({ delay: 0.25 + i * 0.05 });
          const pulseOn = `0 0 0 6px color-mix(in oklab, ${accent2} 40%, transparent)`;
          const pulseOff = `0 0 0 0 color-mix(in oklab, ${accent2} 0%, transparent)`;
          tl.to(el, { boxShadow: pulseOn,  duration: 0.25, ease: EASE_OUT })
            .to(el, { boxShadow: pulseOff, duration: 0.45, ease: EASE_IN  });
        });
      },
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
    if (!isFinite(n)) return '—';
    return n >= 60 ? `${(n / 60).toFixed(1)}m` : `${n.toFixed(1)}s`;
  };

  const fmtCost = (c) => {
    const n = Number(c);
    if (!isFinite(n)) return '—';
    return `$${n.toFixed(2)}`;
  };

  const instances = [];
  const byId = new Map();
  for (const r of rows) {
    let inst = byId.get(r.instance_id);
    if (!inst) {
      inst = {
        instance_id: r.instance_id,
        repo: r.repo,
        repo_url: r.repo_url,
        issue_url: r.issue_url,
        pr_url: r.pr_url,
        language: r.language,
        difficulty: r.difficulty,
        gold_speedup: r.gold_speedup,
        f2p_count: r.f2p_count,
        p2p_count: r.p2p_count,
        runs: {},
      };
      byId.set(r.instance_id, inst);
      instances.push(inst);
    }
    inst.runs[r.model] = {
      outcome: r.outcome,
      pass_at_1: r.pass_at_1,
      hsr: r.hsr,
      speedup_lm: r.speedup_lm,
      speedup_adjusted: r.speedup_adjusted,
      tests_passed: r.tests_passed,
      tests_total: r.tests_total,
      correctness_pct: r.correctness_pct,
      files_modified: r.files_modified,
      tool_calls: r.tool_calls,
      cost: r.cost,
      time_secs: r.time_secs,
    };
  }

  const totalGlm5 = instances.filter(i => i.runs['GLM-5']?.pass_at_1 === 'Pass').length;
  const totalNova = instances.filter(i => i.runs['Kimi K2.5']?.pass_at_1 === 'Pass').length;
  const total = instances.length;
  const elGlm5 = document.getElementById('total-glm5');
  const elNova = document.getElementById('total-nova');
  if (elGlm5) elGlm5.textContent = `${totalGlm5} / ${total}`;
  if (elNova) elNova.textContent = `${totalNova} / ${total}`;



  const outcomeLabel = (outcome) => {
    if (outcome === 'pass') return 'Pass';
    if (outcome === 'correct_but_slow') return 'Correct (Slow)';
    return 'Fail';
  };
  const outcomeClass = (outcome) => {
    if (outcome === 'pass') return 'tile-btn--pass';
    if (outcome === 'correct_but_slow') return 'tile-btn--slow';
    return 'tile-btn--fail';
  };

  // Dataset Viewer: search, filter, sort, pagination
  const searchInput = document.querySelector('.kr-dataset__search');
  const filterSelect = document.querySelector('.kr-dataset__filter');
  const paginationEl = document.querySelector('.kr-dataset__pagination');
  let currentPage = 1;
  const pageSize = 10;
  let sortField = 'instance_id';
  let sortDir = 'asc';

  function getFiltered() {
    const search = (searchInput?.value || '').toLowerCase();
    const diff = filterSelect?.value || '';

    return instances.filter((inst) => {
      if (diff && inst.difficulty !== diff) return false;
      if (search && inst.instance_id.toLowerCase().indexOf(search) === -1) return false;
      return true;
    });
  }

  function getSorted(data) {
    const sorted = data.slice();
    sorted.sort((a, b) => {
      let va, vb;
      switch (sortField) {
        case 'instance_id': va = a.instance_id; vb = b.instance_id; break;
        case 'difficulty':
          const order = { 'Easy': 1, 'Medium': 2, 'Hard': 3, 'Expert': 4 };
          va = order[a.difficulty] || 0; vb = order[b.difficulty] || 0; break;
        case 'gold_speedup': va = a.gold_speedup; vb = b.gold_speedup; break;
        case 'glm5_hsr': va = a.runs['GLM-5']?.hsr || 0; vb = b.runs['GLM-5']?.hsr || 0; break;
        case 'nova_hsr': va = a.runs['Kimi K2.5']?.hsr || 0; vb = b.runs['Kimi K2.5']?.hsr || 0; break;
        case 'glm5_outcome': va = a.runs['GLM-5']?.outcome || ''; vb = b.runs['GLM-5']?.outcome || ''; break;
        case 'nova_outcome': va = a.runs['Kimi K2.5']?.outcome || ''; vb = b.runs['Kimi K2.5']?.outcome || ''; break;
        default: va = a.instance_id; vb = b.instance_id;
      }
      if (va < vb) return sortDir === 'asc' ? -1 : 1;
      if (va > vb) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return sorted;
  }

  let expandedId = null;

  function renderDetailRow(inst) {
    const glm5 = inst.runs['GLM-5'] || {};
    const nova = inst.runs['Kimi K2.5'] || {};
    const outcomeColor = (o) => o === 'pass' ? 'var(--pass)' : (o === 'correct_but_slow' ? 'var(--accent)' : 'var(--fail)');
    const item = (k, v) => `<div class="detail-row-item"><span class="detail-key">${esc(k)}</span><span class="detail-val">${v}</span></div>`;

    function modelBlock(title, m) {
      const o = m.outcome || 'fail';
      return `<div class="detail-block"><div class="detail-block-title">${esc(title)}</div>`
        + item('Outcome', `<b style="color:${outcomeColor(o)}">${outcomeLabel(o).toUpperCase()}</b>`)
        + item('HSR', Number(m.hsr || 0).toFixed(4))
        + item('Speedup (LM)', Number(m.speedup_lm || 0).toFixed(4) + '\u00d7')
        + item('Speedup (Adj)', Number(m.speedup_adjusted || 0).toFixed(4) + '\u00d7')
        + item('Tests', (m.tests_passed ?? '-') + ' / ' + (m.tests_total ?? '-'))
        + item('Correctness', Number(m.correctness_pct || 0).toFixed(1) + '%')
        + item('Files modified', esc(String(m.files_modified ?? '-')))
        + item('Tool calls', esc(String(m.tool_calls ?? '-')))
        + item('Cost', fmtCost(m.cost))
        + item('Time', fmtTime(m.time_secs))
        + `</div>`;
    }

    return `<tr class="detail-row" data-detail-for="${esc(inst.instance_id)}"><td colspan="8"><div class="detail-content"><div class="detail-grid">`
      + `<div class="detail-block"><div class="detail-block-title">Instance</div>`
        + item('Instance ID', esc(inst.instance_id))
        + item('Repo', inst.repo_url ? `<a href="${esc(inst.repo_url)}" target="_blank" rel="noopener">${esc(inst.repo)}</a>` : esc(inst.repo))
        + item('Difficulty', esc(inst.difficulty))
        + item('Gold Speedup', Number(inst.gold_speedup).toFixed(2) + '\u00d7')
        + item('Language', esc(inst.language))
      + `</div>`
      + modelBlock('GLM-5', glm5)
      + modelBlock('Kimi K2.5', nova)
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
    page.forEach((inst, i) => {
      const glm5 = inst.runs['GLM-5'];
      const nova = inst.runs['Kimi K2.5'];
      const glm5Outcome = glm5?.outcome || 'fail';
      const novaOutcome = nova?.outcome || 'fail';
      const tagStyle = 'display:inline-flex;align-items:center;justify-content:center;max-width:100%;min-height:34px;text-align:center;font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:0.03em;text-transform:uppercase;padding:3px 5px;border:1px solid currentColor;white-space:normal;line-height:1.3;box-sizing:border-box;';
      const diffColors = { easy:'color:var(--pass);', medium:'color:var(--accent);', hard:'color:var(--fail);', expert:'color:#7c3aed;' };
      const diffStyle = diffColors[(inst.difficulty || '').toLowerCase()] || 'color:var(--ink-3);';
      const isExpanded = inst.instance_id === expandedId;
      html += `
        <tr class="matrix-row${isExpanded ? ' row-expanded' : ''}" data-id="${esc(inst.instance_id)}">
          <td class="matrix-id"><span class="num mono">#${String(start + i + 1).padStart(2, '0')}</span> ${esc(inst.instance_id)}</td>
          <td class="matrix-meta"><span style="${tagStyle}${diffStyle}">${esc(inst.difficulty)}</span></td>
          <td class="matrix-meta">${Number(inst.gold_speedup).toFixed(2)}x</td>
          <td class="matrix-meta">${glm5 ? Number(glm5.hsr).toFixed(4) : '-'}</td>
          <td class="matrix-meta">${nova ? Number(nova.hsr).toFixed(4) : '-'}</td>
          <td class="matrix-cell"><span class="tile-btn ${outcomeClass(glm5Outcome)}" style="${tagStyle}">${outcomeLabel(glm5Outcome)}</span></td>
          <td class="matrix-cell"><span class="tile-btn ${outcomeClass(novaOutcome)}" style="${tagStyle}">${outcomeLabel(novaOutcome)}</span></td>
          <td class="matrix-cell"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>
        </tr>
      `;
      if (isExpanded) html += renderDetailRow(inst);
    });

    if (!page.length) {
      html = '<tr><td colspan="8" style="text-align:center;padding:2rem;color:var(--muted);">No instances match your filters</td></tr>';
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

  // Sort on header click
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

  // Filter/search handlers
  if (searchInput) searchInput.addEventListener('input', () => { currentPage = 1; renderDataset(); });
  if (filterSelect) filterSelect.addEventListener('change', () => { currentPage = 1; renderDataset(); });

  renderDataset();

  // Row-expand click handler
  document.addEventListener('click', (e) => {
    const row = e.target.closest('#matrix tr[data-id]');
    if (!row) return;
    // Don't toggle if clicking a link
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
