(() => {
  // ============================================================
  // Theme toggle — persists to localStorage, also reacts to OS
  // changes for users who never clicked. Runs before everything
  // else so the button works even if dataset loading fails.
  // ============================================================
  const root = document.documentElement;
  const toggleBtn = document.getElementById('theme-toggle');
  const themeKey = 'tesseract:theme';
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

  const dataEl = document.getElementById('instances-data');
  if (!dataEl) return;
  const rows = JSON.parse(dataEl.textContent || '[]');

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
  // a yPercent 110 → 0 rise on load. Walks child nodes so <em>
  // spans (potentially multi-word) keep their emphasis on every
  // word they contain.
  //
  // Runs OUTSIDE initAnimations so the DOM is ready even if GSAP
  // isn't (fallback: flat text, no motion — page still reads).
  // ============================================================
  const thesisEl = document.querySelector('.thesis');
  if (thesisEl) {
    const tokens = [];
    thesisEl.childNodes.forEach((node) => {
      const isEm = node.nodeType === 1 && node.tagName === 'EM';
      const text = node.textContent || '';
      text.split(/\s+/).filter(Boolean).forEach((w) => tokens.push({ w, isEm }));
    });
    thesisEl.innerHTML = tokens.map(({ w, isEm }) => {
      const punctMatch = w.match(/^(.*?)([.,!?;:]+)$/);
      const core  = punctMatch ? punctMatch[1] : w;
      const punct = punctMatch ? punctMatch[2] : '';
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

    // Blocks 3 (funnel glow), 4 (charts fade) and 5 (KPI fade) REMOVED — all
    // redundant with block 2 (section fade-rise), which already
    // animates every direct child of every <section>.

    // ---------- 6. Matrix pass-tile pulse on enter -----------------
    // Capped to first 8 tiles; at full 40-tile length the cascading
    // shadow repaints overwhelm a 60Hz projector.
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

    // Re-measure on resize so triggers adapt to layout shifts
    window.addEventListener('resize', () => ScrollTrigger.refresh(), { passive: true });
  };

  // kick off after GSAP CDN loads (defer means it's after DOM ready)
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

  // ---------- group rows into one object per instance ---------------------
  const instances = [];
  const byId = new Map();
  for (const r of rows) {
    let inst = byId.get(r.instance_id);
    if (!inst) {
      inst = {
        instance_id: r.instance_id,
        sr_no: r.sr_no,
        repo: r.repo,
        repo_url: r.repo_url,
        issue_url: r.issue_url,
        pr_url: r.pr_url,
        language: r.language,
        difficulty: r.difficulty,
        docker_uri: r.docker_uri,
        trajectory_url: r.trajectory_url,
        f2p_count: r.f2p_count,
        p2p_count: r.p2p_count,
        runs: {},
      };
      byId.set(r.instance_id, inst);
      instances.push(inst);
    }
    inst.runs[r.model] = {
      pass_at_1: r.pass_at_1,
      files_modified: r.files_modified,
      tool_calls: r.tool_calls,
      time_secs: r.time_secs,
    };
  }
  instances.sort((a, b) => (a.sr_no || 0) - (b.sr_no || 0));

  // totals
  const totalKimi = instances.filter(i => i.runs['Kimi K2.5']?.pass_at_1 === 'Pass').length;
  const totalNova = instances.filter(i => i.runs['Nova 2 Lite']?.pass_at_1 === 'Pass').length;
  const total = instances.length;
  const elKimi = document.getElementById('total-kimi');
  const elNova = document.getElementById('total-nova');
  if (elKimi) elKimi.textContent = `${totalKimi} / ${total}`;
  if (elNova) elNova.textContent = `${totalNova} / ${total}`;

  // ---------- VIEW TOGGLE (matrix / repo) ---------------------------------
  const tabs = document.querySelectorAll('.vt-btn');
  const panes = {
    matrix: document.getElementById('view-matrix'),
    repo:   document.getElementById('view-repo'),
  };
  const setView = (view) => {
    tabs.forEach((b) => {
      const active = b.dataset.view === view;
      b.classList.toggle('is-active', active);
      b.setAttribute('aria-selected', String(active));
      b.tabIndex = active ? 0 : -1;
    });
    Object.entries(panes).forEach(([k, el]) => {
      if (!el) return;
      el.hidden = k !== view;
    });
    try { localStorage.setItem('tesseract:view', view); } catch (e) {}
  };
  tabs.forEach((b) => {
    b.addEventListener('click', () => setView(b.dataset.view));
    b.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const list = Array.from(tabs);
      const i = list.indexOf(b);
      const next = list[(i + (e.key === 'ArrowRight' ? 1 : -1) + list.length) % list.length];
      next.focus();
      setView(next.dataset.view);
    });
  });
  try {
    const saved = localStorage.getItem('tesseract:view');
    if (saved && panes[saved]) setView(saved);
  } catch (e) {}

  // ---------- MATRIX render ---------------------------------------------
  const tileHTML = (inst, modelKey) => {
    const run = inst.runs[modelKey];
    if (!run) return `<td class="matrix-cell"><span class="tile-btn" aria-hidden="true">—</span></td>`;
    const pass = run.pass_at_1 === 'Pass';
    const label = `${modelKey} on ${inst.instance_id}: ${pass ? 'resolved' : 'failed'} · ${run.tool_calls} tool calls · ${fmtTime(run.time_secs)}`;
    return `
      <td class="matrix-cell">
        <button class="tile-btn ${pass ? 'tile-btn--pass' : 'tile-btn--fail'}"
                data-instance="${esc(inst.instance_id)}" data-model="${esc(modelKey)}"
                aria-label="${esc(label)}">
          ${pass ? 'PASS' : 'FAIL'}
        </button>
      </td>
    `;
  };
  const detailBlock = (inst, modelKey, cls) => {
    const run = inst.runs[modelKey];
    if (!run) {
      return `
        <div class="matrix-detail-block">
          <p class="matrix-detail-title tok-${cls}">${esc(modelKey)}</p>
          <p class="matrix-detail-item"><span class="matrix-detail-key">—</span><span class="matrix-detail-val">no run</span></p>
        </div>`;
    }
    const pass = run.pass_at_1 === 'Pass';
    const passHTML = pass
      ? '<span style="color:var(--pass);font-weight:600">PASS</span>'
      : '<span style="color:var(--fail);font-weight:600">FAIL</span>';
    return `
      <div class="matrix-detail-block">
        <p class="matrix-detail-title tok-${cls}">${esc(modelKey)}</p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Result</span><span class="matrix-detail-val">${passHTML}</span></p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Tool calls</span><span class="matrix-detail-val">${esc(run.tool_calls)}</span></p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Files changed</span><span class="matrix-detail-val">${esc(run.files_modified)}</span></p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Time</span><span class="matrix-detail-val">${fmtTime(run.time_secs)}</span></p>
      </div>`;
  };

  const instanceBlock = (inst) => {
    const hasTests = (inst.f2p_count != null) || (inst.p2p_count != null);
    const dockerRow = inst.docker_uri
      ? `<p class="matrix-detail-item matrix-detail-item--stack">
           <span class="matrix-detail-key">Docker image</span>
           <span class="matrix-detail-val">
             <code class="matrix-detail-docker">${esc(inst.docker_uri)}</code>
             <button type="button" class="matrix-detail-copy" data-copy="${esc(inst.docker_uri)}" aria-label="Copy docker image URI">copy</button>
           </span>
         </p>`
      : '';
    const testsRow = hasTests
      ? `<p class="matrix-detail-item"><span class="matrix-detail-key">Tests</span><span class="matrix-detail-val"><span style="color:var(--fail)">F2P ${esc(inst.f2p_count ?? 0)}</span> · <span style="color:var(--pass)">P2P ${esc(inst.p2p_count ?? 0)}</span></span></p>`
      : '';
    const trajRow = inst.trajectory_url
      ? `<p class="matrix-detail-item"><span class="matrix-detail-key">Trajectory</span><span class="matrix-detail-val"><a href="${esc(inst.trajectory_url)}" target="_blank" rel="noopener">view log →</a></span></p>`
      : '';
    const prRow = inst.pr_url
      ? `<p class="matrix-detail-item"><span class="matrix-detail-key">PR</span><span class="matrix-detail-val"><a href="${esc(inst.pr_url)}" target="_blank" rel="noopener">original PR →</a></span></p>`
      : '';
    return `
      <div class="matrix-detail-block">
        <p class="matrix-detail-title">Instance</p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">ID</span><span class="matrix-detail-val"><a href="${esc(inst.issue_url)}" target="_blank" rel="noopener">${esc(inst.instance_id)}</a></span></p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Repo</span><span class="matrix-detail-val"><a href="${esc(inst.repo_url)}" target="_blank" rel="noopener">${esc(inst.repo)}</a></span></p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Language</span><span class="matrix-detail-val">${esc(inst.language)}</span></p>
        <p class="matrix-detail-item"><span class="matrix-detail-key">Difficulty</span><span class="matrix-detail-val">${esc(inst.difficulty)}</span></p>
        ${testsRow}
        ${trajRow}
        ${prRow}
        ${dockerRow}
      </div>`;
  };

  const renderMatrix = () => {
    const tb = document.getElementById('matrix-tbody');
    if (!tb) return;
    tb.innerHTML = instances.map((inst, i) => {
      const isP5 = inst.repo === 'processing/p5.js';
      return `
        <tr class="matrix-row ${isP5 ? 'row-p5' : ''}" data-instance="${esc(inst.instance_id)}" aria-expanded="false">
          <td class="matrix-id">
            <span class="num mono">#${String(i + 1).padStart(2, '0')}</span>
            <a href="${esc(inst.issue_url)}" target="_blank" rel="noopener">${esc(inst.repo)}</a>
          </td>
          <td class="matrix-meta">${esc(inst.language)}</td>
          <td class="matrix-meta">${esc(inst.difficulty)}</td>
          ${tileHTML(inst, 'Kimi K2.5')}
          ${tileHTML(inst, 'Nova 2 Lite')}
          <td class="matrix-expand">
            <span class="matrix-expand-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg>
            </span>
          </td>
        </tr>
        <tr class="matrix-detail-row" data-detail-for="${esc(inst.instance_id)}" hidden="hidden">
          <td colspan="6">
            <div class="matrix-detail-content">
              <div class="matrix-detail-grid">
                ${instanceBlock(inst)}
                ${detailBlock(inst, 'Kimi K2.5', 'kimi')}
                ${detailBlock(inst, 'Nova 2 Lite', 'nova')}
              </div>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  };
  renderMatrix();

  // ---------- REPO DRILL-DOWN render ------------------------------------
  const repoGroups = (() => {
    const m = new Map();
    for (const inst of instances) {
      let g = m.get(inst.repo);
      if (!g) { g = { repo: inst.repo, language: inst.language, instances: [] }; m.set(inst.repo, g); }
      g.instances.push(inst);
    }
    const list = Array.from(m.values()).map((g) => {
      const kimiPass = g.instances.filter(i => i.runs['Kimi K2.5']?.pass_at_1 === 'Pass').length;
      const novaPass = g.instances.filter(i => i.runs['Nova 2 Lite']?.pass_at_1 === 'Pass').length;
      return { ...g, kimiPass, novaPass, n: g.instances.length };
    });
    // sort: any-pass first, then by repo name
    list.sort((a, b) => {
      const aScore = a.kimiPass + a.novaPass;
      const bScore = b.kimiPass + b.novaPass;
      if (aScore !== bScore) return bScore - aScore;
      return a.repo.localeCompare(b.repo);
    });
    return list;
  })();

  const runCard = (inst) => {
    const mk = (model, cls) => {
      const r = inst.runs[model];
      if (!r) return '';
      const pass = r.pass_at_1 === 'Pass';
      return `
        <div class="run-model run-model--${cls}">
          <div class="run-model-head">
            <span>${esc(model)}</span>
            <span class="run-pass ${pass ? 'run-pass--pass' : 'run-pass--fail'}">${pass ? 'PASS' : 'FAIL'}</span>
          </div>
          <span class="run-stat">Tools</span><span class="run-val">${esc(r.tool_calls)}</span>
          <span class="run-stat">Time</span><span class="run-val">${fmtTime(r.time_secs)}</span>
          <span class="run-stat">Files</span><span class="run-val">${esc(r.files_modified)}</span>
        </div>
      `;
    };
    const isP5 = inst.repo === 'processing/p5.js';
    return `
      <div class="run-card ${isP5 ? 'run-card--p5' : ''}">
        <div class="run-inst">
          <a class="run-id" href="${esc(inst.issue_url)}" target="_blank" rel="noopener">${esc(inst.instance_id)}</a>
          <span class="run-diff">${esc(inst.difficulty)}</span>
        </div>
        ${mk('Kimi K2.5', 'kimi')}
        ${mk('Nova 2 Lite', 'nova')}
      </div>
    `;
  };

  const renderRepoList = () => {
    const root = document.getElementById('repo-list');
    if (!root) return;
    root.innerHTML = repoGroups.map((g, i) => {
      const kimiCls = g.kimiPass > 0 ? 'has-pass' : '';
      const novaCls = g.novaPass > 0 ? 'has-pass' : '';
      const open = g.repo === 'processing/p5.js';   // auto-expand the "look here" row
      const childrenId = `repo-children-${i}`;
      return `
        <div class="repo-group">
          <button class="repo-head" aria-expanded="${open}" aria-controls="${childrenId}">
            <span class="repo-caret" aria-hidden="true">›</span>
            <span class="repo-name">${esc(g.repo)}</span>
            <span class="repo-lang">${esc(g.language)}</span>
            <span class="repo-score score-kimi ${kimiCls}">K <b>${g.kimiPass}/${g.n}</b></span>
            <span class="repo-score score-nova ${novaCls}">N <b>${g.novaPass}/${g.n}</b></span>
          </button>
          <div class="repo-children" id="${childrenId}" ${open ? '' : 'hidden'}>
            ${g.instances.map(runCard).join('')}
          </div>
        </div>
      `;
    }).join('');

    root.querySelectorAll('.repo-head').forEach((btn) => {
      btn.addEventListener('click', () => {
        const expanded = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', String(!expanded));
        const target = document.getElementById(btn.getAttribute('aria-controls'));
        if (target) target.hidden = expanded;
      });
    });
  };
  renderRepoList();

  // ---------- MATRIX ROW EXPAND ------------------------------------------
  // Click anywhere on a row to expand its detail view (Instance + Kimi +
  // Nova side-by-side). Click again to collapse. Only one row open at a
  // time — opening another closes the previously open one.
  let openInstanceId = null;

  const collapseRow = (id) => {
    const row = document.querySelector(`.matrix-row[data-instance="${CSS.escape(id)}"]`);
    const detail = document.querySelector(`.matrix-detail-row[data-detail-for="${CSS.escape(id)}"]`);
    if (row) {
      row.classList.remove('is-expanded');
      row.setAttribute('aria-expanded', 'false');
    }
    if (detail) detail.setAttribute('hidden', '');
  };

  const expandRow = (id) => {
    const row = document.querySelector(`.matrix-row[data-instance="${CSS.escape(id)}"]`);
    const detail = document.querySelector(`.matrix-detail-row[data-detail-for="${CSS.escape(id)}"]`);
    if (row) {
      row.classList.add('is-expanded');
      row.setAttribute('aria-expanded', 'true');
    }
    if (detail) detail.removeAttribute('hidden');
  };

  const toggleRow = (id) => {
    if (openInstanceId === id) {
      collapseRow(id);
      openInstanceId = null;
      return;
    }
    if (openInstanceId) collapseRow(openInstanceId);
    expandRow(id);
    openInstanceId = id;
  };

  // Matrix click delegate — row click toggles expansion. Links inside the
  // row (repo name, etc.) are allowed to navigate unless user Cmd/Ctrl-
  // clicks. Docker copy button gets its own handler inside the detail row.
  const matrixEl = document.getElementById('matrix');
  if (matrixEl) {
    matrixEl.addEventListener('click', (e) => {
      // Docker copy button lives in the detail row
      const copyBtn = e.target.closest('.matrix-detail-copy');
      if (copyBtn) {
        e.preventDefault();
        const val = copyBtn.dataset.copy || '';
        navigator.clipboard?.writeText(val).then(() => {
          const original = copyBtn.textContent;
          copyBtn.textContent = 'copied';
          copyBtn.classList.add('is-copied');
          setTimeout(() => {
            copyBtn.textContent = original;
            copyBtn.classList.remove('is-copied');
          }, 1400);
        }).catch(() => {});
        return;
      }
      // Link clicks inside a row: let the browser navigate; don't toggle.
      if (e.target.closest('a')) return;
      // Tile buttons still preserve their hover/focus styles for keyboard
      // users, but a pointer click on one is treated as a row click.
      const row = e.target.closest('.matrix-row');
      if (!row || !row.dataset.instance) return;
      toggleRow(row.dataset.instance);
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && openInstanceId) {
      collapseRow(openInstanceId);
      openInstanceId = null;
    }
  });

  // ---------- CHART LIGHTBOX --------------------------------------------
  // Click any chart image → full-size preview. Close via button, overlay
  // click, or Escape. Body scroll is locked while open.
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

})();
