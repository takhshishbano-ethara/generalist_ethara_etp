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

  // Scroll progress bar
  const scrollProgress = document.querySelector('.scroll-progress');
  if (scrollProgress) {
    const updateProgress = () => {
      const scrollTop = window.scrollY;
      const docHeight = document.documentElement.scrollHeight - window.innerHeight;
      const progress = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
      scrollProgress.style.width = progress + '%';
    };
    window.addEventListener('scroll', updateProgress, { passive: true });
    updateProgress();
  }

  const dataEl = document.getElementById('instances-data');
  if (!dataEl) return;
  const rows = JSON.parse(dataEl.textContent || '[]');

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Mouse-following glow on cards
  const glowCards = document.querySelectorAll('.kpi-card, .chart, .resource');
  if (glowCards.length && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    glowCards.forEach(card => {
      card.addEventListener('mousemove', (e) => {
        const rect = card.getBoundingClientRect();
        const x = ((e.clientX - rect.left) / rect.width) * 100;
        const y = ((e.clientY - rect.top) / rect.height) * 100;
        card.style.setProperty('--mouse-x', x + '%');
        card.style.setProperty('--mouse-y', y + '%');
      });
    });
  }

  // ============================================================
  // GSAP + ScrollTrigger — loaded via CDN in <head>. Wait for it.
  // Everything here is progressive: if GSAP fails to load or user
  // opts out of motion, the page still reads correctly.
  // ============================================================
  const initAnimations = () => {
    if (prefersReduced) return;
    if (typeof window.gsap === 'undefined' || typeof window.ScrollTrigger === 'undefined') return;

    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    gsap.defaults({ ease: 'power3.out', duration: 1.0 });

    // ---------- 1. Cinematic masthead reveal -------------------------
    const mastTl = gsap.timeline({ defaults: { ease: 'power3.out' } });
    mastTl
      .from('.wordmark', { y: 30, opacity: 0, scale: 1.05, filter: 'blur(8px)', duration: 1.1 })
      .from('.badge', { y: 20, opacity: 0, scale: 0.9, duration: 0.7, ease: 'back.out(1.7)' }, '-=0.6')
      .from('hr.rule', { scaleX: 0, opacity: 0, duration: 0.8, transformOrigin: 'left center' }, '-=0.4')
      .from('.thesis', { y: 40, opacity: 0, filter: 'blur(4px)', duration: 1.0 }, '-=0.5')
      .from('.byline', { y: 16, opacity: 0, duration: 0.7 }, '-=0.4');

    // ---------- 2. Differentiated section animations ----------------
    const sections = document.querySelectorAll('main > .section');
    sections.forEach((section) => {
      const id = section.id;

      if (id === 'scale') {
        gsap.from(section.querySelectorAll('.funnel-step'), {
          x: -40, opacity: 0, stagger: 0.15, duration: 0.8,
          ease: 'power2.out',
          scrollTrigger: { trigger: section, start: 'top 80%' }
        });
      } else if (id === 'pipeline') {
        gsap.from(section.querySelectorAll('.phase'), {
          y: 30, opacity: 0, scale: 0.96, stagger: 0.12, duration: 0.7,
          ease: 'back.out(1.2)',
          scrollTrigger: { trigger: section, start: 'top 78%' }
        });
      } else if (id === 'results') {
        gsap.from('.reveal', {
          x: -20, opacity: 0, duration: 0.9, ease: 'power3.out',
          scrollTrigger: { trigger: '.reveal', start: 'top 80%' }
        });
      } else if (id === 'instances') {
        gsap.from(section.querySelectorAll('.kpi-card'), {
          y: 30, opacity: 0, scale: 0.95, stagger: 0.08, duration: 0.6,
          ease: 'back.out(1.4)',
          scrollTrigger: { trigger: '.kpi-row', start: 'top 82%' }
        });
      } else {
        gsap.from(section.querySelectorAll(':scope > *'), {
          y: 28, opacity: 0, stagger: 0.08, duration: 0.7,
          scrollTrigger: { trigger: section, start: 'top 82%' }
        });
      }
    });

    // Smooth parallax on section labels
    document.querySelectorAll('.section-label').forEach((label) => {
      gsap.from(label, {
        x: -20, opacity: 0, duration: 0.8,
        scrollTrigger: { trigger: label, start: 'top 88%' }
      });
    });

    // Pipeline frame reveal
    const pipelineFrame = document.querySelector('.pipeline-frame');
    if (pipelineFrame) {
      gsap.from(pipelineFrame, {
        y: 40, opacity: 0, scale: 0.98, duration: 1.0,
        scrollTrigger: { trigger: pipelineFrame, start: 'top 80%' }
      });
    }

    // Glance sidebar stagger
    gsap.from('.glance-row', {
      x: 20, opacity: 0, stagger: 0.06, duration: 0.5,
      scrollTrigger: { trigger: '.glance', start: 'top 80%' }
    });

    // View toggle + matrix reveal
    gsap.from('.view-toggle', {
      y: 20, opacity: 0, duration: 0.6,
      scrollTrigger: { trigger: '.view-toggle', start: 'top 88%' }
    });
    gsap.from('.matrix-wrap', {
      y: 30, opacity: 0, duration: 0.8,
      scrollTrigger: { trigger: '.matrix-wrap', start: 'top 85%' }
    });

    // Reveal lines stagger
    gsap.from('.reveal-line', {
      y: 20, opacity: 0, stagger: 0.15, duration: 0.8,
      ease: 'power3.out',
      scrollTrigger: { trigger: '.reveal', start: 'top 78%' }
    });

    // Footer fade
    gsap.from('.foot-inner', {
      y: 20, opacity: 0, duration: 0.8,
      scrollTrigger: { trigger: '.foot', start: 'top 92%' }
    });

    // Repo groups stagger
    gsap.from('.repo-group', {
      y: 20, opacity: 0, stagger: 0.08, duration: 0.6,
      scrollTrigger: { trigger: '.repo-list', start: 'top 85%' }
    });

    // ---------- 3. Funnel final KPI accent glow (one-shot) ---------
    // Colors pull from CSS variables so they adapt to light/dark palettes.
    const css = getComputedStyle(document.documentElement);
    const accent2 = css.getPropertyValue('--accent-2').trim() || '#4A5515';
    const accentSurface = css.getPropertyValue('--accent-surface').trim() || '#EDF3B8';
    gsap.fromTo('.funnel-step--final',
      { boxShadow: `inset 0 0 0 1px color-mix(in oklab, ${accent2} 30%, transparent)` },
      {
        boxShadow: `inset 0 0 0 1px ${accent2}, 0 0 30px ${accentSurface}`,
        duration: 1.0,
        delay: 0.4,
        scrollTrigger: { trigger: '.funnel', start: 'top 75%', once: true },
      }
    );

    // ---------- 4. Charts fade-in under reveal ---------------------
    gsap.from('.charts .chart', {
      y: 40, opacity: 0, scale: 0.96, stagger: 0.12, duration: 0.9,
      ease: 'power2.out',
      scrollTrigger: { trigger: '.charts', start: 'top 85%' },
    });

    // ---------- 5. (KPI cards handled in differentiated section animations above)

    // ---------- 6. Matrix pass-tile pulse on enter -----------------
    ScrollTrigger.create({
      trigger: '#view-matrix',
      start: 'top 75%',
      once: true,
      onEnter: () => {
        const tiles = document.querySelectorAll('.tile-btn--pass');
        tiles.forEach((el, i) => {
          const tl = gsap.timeline({ delay: 0.25 + i * 0.1 });
          const pulseOn = `0 0 0 6px color-mix(in oklab, ${accent2} 40%, transparent)`;
          const pulseOff = `0 0 0 0 color-mix(in oklab, ${accent2} 0%, transparent)`;
          tl.to(el, { boxShadow: pulseOn, duration: 0.25, ease: 'power2.out' })
            .to(el, { boxShadow: pulseOff, duration: 0.45, ease: 'power2.in' });
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

  // ---------- FUNNEL counters (IntersectionObserver) ----------------------
  const funnelNums = document.querySelectorAll('.funnel-num');
  const animateCount = (el) => {
    const target = Number(el.dataset.target || '0');
    const suffix = el.dataset.suffix || '';
    if (prefersReduced) {
      el.textContent = target.toLocaleString() + suffix;
      return;
    }
    const duration = 1100;
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      // Spring overshoot: goes to 105% then settles
      const eased = t < 0.7
        ? 1.05 * (1 - Math.pow(1 - t / 0.7, 3))
        : 1.05 - 0.05 * ((t - 0.7) / 0.3);
      const val = Math.round(target * Math.min(eased, 1.05));
      el.textContent = (t === 1 ? target : Math.min(val, Math.round(target * 1.05))).toLocaleString() + (t === 1 ? suffix : '');
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
  const renderMatrix = () => {
    const tb = document.getElementById('matrix-tbody');
    if (!tb) return;
    tb.innerHTML = instances.map((inst, i) => {
      const isP5 = inst.repo === 'processing/p5.js';
      return `
        <tr class="matrix-row ${isP5 ? 'row-p5' : ''}">
          <td class="matrix-id">
            <span class="num mono">#${String(i + 1).padStart(2, '0')}</span>
            <a href="${esc(inst.issue_url)}" target="_blank" rel="noopener">${esc(inst.repo)}</a>
          </td>
          <td class="matrix-meta">${esc(inst.language)}</td>
          <td class="matrix-meta">${esc(inst.difficulty)}</td>
          ${tileHTML(inst, 'Kimi K2.5')}
          ${tileHTML(inst, 'Nova 2 Lite')}
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

  // ---------- DRAWER (slide panel on tile click) --------------------------
  let lastFocus = null;

  const getDrawer = () => document.getElementById('drawer');
  const getDrawerBody = () => document.getElementById('drawer-body');
  const getDrawerClose = () => document.getElementById('drawer-close');

  const ensureBackdrop = () => {
    let bd = document.getElementById('drawer-backdrop');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'drawer-backdrop';
      bd.className = 'drawer-backdrop';
      document.body.appendChild(bd);
    }
    return bd;
  };

  const openDrawer = (instanceId, modelKey) => {
    const drawer = getDrawer();
    const drawerBody = getDrawerBody();
    if (!drawer || !drawerBody) return;

    const inst = byId.get(instanceId);
    if (!inst) return;
    const run = inst.runs[modelKey];
    if (!run) return;
    const pass = run.pass_at_1 === 'Pass';
    const hasTests = (inst.f2p_count != null) || (inst.p2p_count != null);
    const testRow = hasTests
      ? `<dt>Tests</dt><dd><span style="color:var(--fail)">F2P ${esc(inst.f2p_count ?? 0)}</span> · <span style="color:var(--pass)">P2P ${esc(inst.p2p_count ?? 0)}</span></dd>`
      : '';
    const dockerRow = inst.docker_uri
      ? `<dt>Docker Image</dt><dd><code class="mono drawer-docker">${esc(inst.docker_uri)}</code>
           <button type="button" class="drawer-copy" data-copy="${esc(inst.docker_uri)}" aria-label="Copy docker image URI">copy</button></dd>`
      : '';
    drawerBody.innerHTML = `
      <dl>
        <dt>Instance</dt><dd><a href="${esc(inst.issue_url)}" target="_blank" rel="noopener">${esc(inst.instance_id)}</a></dd>
        <dt>Model</dt><dd class="${modelKey === 'Kimi K2.5' ? 'tok-kimi' : 'tok-nova'}">${esc(modelKey)}</dd>
        <dt>Result</dt><dd>${pass ? '<b style="color:var(--pass)">PASS</b>' : '<b style="color:var(--fail)">FAIL</b>'}</dd>
        <dt>Repo</dt><dd><a href="${esc(inst.repo_url)}" target="_blank" rel="noopener">${esc(inst.repo)}</a></dd>
        <dt>Language</dt><dd>${esc(inst.language)}</dd>
        <dt>Difficulty</dt><dd>${esc(inst.difficulty)}</dd>
        ${testRow}
        <dt>Files changed</dt><dd>${esc(run.files_modified)}</dd>
        <dt>Tool calls</dt><dd>${esc(run.tool_calls)}</dd>
        <dt>Time</dt><dd>${fmtTime(run.time_secs)}</dd>
        <dt>Trajectory</dt><dd><a href="${esc(inst.trajectory_url)}" target="_blank" rel="noopener">view log →</a></dd>
        <dt>PR</dt><dd><a href="${esc(inst.pr_url)}" target="_blank" rel="noopener">original PR →</a></dd>
        ${dockerRow}
      </dl>
    `;
    drawer.removeAttribute('hidden');
    drawer.style.display = '';
    document.body.classList.add('drawer-open');
    ensureBackdrop().classList.add('is-visible');
    requestAnimationFrame(() => {
      drawer.setAttribute('data-open', 'true');
      drawer.setAttribute('aria-hidden', 'false');
    });
    lastFocus = document.activeElement;
    const closeBtn = getDrawerClose();
    if (closeBtn) closeBtn.focus();
  };

  const closeDrawer = () => {
    const drawer = getDrawer();
    if (!drawer) return;
    drawer.setAttribute('data-open', 'false');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('drawer-open');
    ensureBackdrop().classList.remove('is-visible');
    setTimeout(() => { drawer.setAttribute('hidden', ''); }, prefersReduced ? 0 : 240);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  };

  document.addEventListener('click', (e) => {
    if (e.target.closest('#drawer-close') || e.target.closest('.drawer-backdrop')) {
      closeDrawer();
      return;
    }
    const copyBtn = e.target.closest('#drawer .drawer-copy');
    if (copyBtn) {
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
    const tileBtn = e.target.closest('#matrix .tile-btn');
    if (tileBtn && tileBtn.dataset.instance) {
      openDrawer(tileBtn.dataset.instance, tileBtn.dataset.model);
      return;
    }
  });

  document.addEventListener('keydown', (e) => {
    const drawer = getDrawer();
    if (e.key === 'Escape' && drawer && !drawer.hasAttribute('hidden')) closeDrawer();
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
