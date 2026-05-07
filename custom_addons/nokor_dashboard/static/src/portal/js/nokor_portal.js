/* ═══════════════════════════════════════════════════════════════════
   NOKOR PORTAL — Interactive JS
   Theme toggle · Scroll progress · GSAP animations · Dataset viewer
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ─── THEME TOGGLE ─── */
  const THEME_KEY = 'nokor:theme';
  const toggle = document.getElementById('nk-theme-toggle');

  function getTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }
  function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* noop */ }
  }

  if (toggle) {
    toggle.addEventListener('click', function () {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      setTheme(current === 'dark' ? 'light' : 'dark');
    });
  }

  // If no saved theme and user prefers dark, apply it
  if (!getTheme() && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    setTheme('dark');
  }

  /* ─── SCROLL PROGRESS ─── */
  const progressBar = document.querySelector('.scroll-progress');
  if (progressBar) {
    let ticking = false;
    window.addEventListener('scroll', function () {
      if (!ticking) {
        requestAnimationFrame(function () {
          const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
          const docHeight = document.documentElement.scrollHeight - window.innerHeight;
          const pct = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
          progressBar.style.width = pct + '%';
          ticking = false;
        });
        ticking = true;
      }
    });
  }

  /* ─── THESIS WORD-SPLIT ANIMATION ─── */
  const thesis = document.querySelector('.thesis');
  if (thesis) {
    const words = thesis.textContent.trim().split(/\s+/);
    thesis.innerHTML = words.map(function (w) {
      return '<span class="thesis-word">' + w + '</span>';
    }).join(' ');
  }

  /* ─── GSAP SCROLL ANIMATIONS (progressive enhancement) ─── */
  function initGSAP() {
    if (typeof gsap === 'undefined' || typeof ScrollTrigger === 'undefined') return;
    gsap.registerPlugin(ScrollTrigger);

    document.querySelectorAll('[data-animate]').forEach(function (el) {
      var delay = parseFloat(el.dataset.delay || 0) * 0.15;
      gsap.fromTo(el,
        { opacity: 0, y: 24 },
        {
          opacity: 1, y: 0,
          duration: 0.7,
          delay: delay,
          ease: 'power2.out',
          scrollTrigger: {
            trigger: el,
            start: 'top 85%',
            once: true
          }
        }
      );
    });
  }

  // Fallback: IntersectionObserver for [data-animate]
  function initObserver() {
    if (!('IntersectionObserver' in window)) {
      document.querySelectorAll('[data-animate]').forEach(function (el) {
        el.classList.add('in-view');
      });
      return;
    }
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var delay = parseFloat(entry.target.dataset.delay || 0) * 150;
          setTimeout(function () {
            entry.target.classList.add('in-view');
          }, delay);
          obs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.1 });

    document.querySelectorAll('[data-animate]').forEach(function (el) {
      obs.observe(el);
    });
  }

  // Use GSAP if available, otherwise fallback
  if (typeof gsap !== 'undefined' && typeof ScrollTrigger !== 'undefined') {
    initGSAP();
  } else {
    // Wait a bit for deferred scripts to load
    window.addEventListener('load', function () {
      if (typeof gsap !== 'undefined' && typeof ScrollTrigger !== 'undefined') {
        initGSAP();
      } else {
        initObserver();
      }
    });
  }

  /* ─── FUNNEL COUNTER ANIMATION ─── */
  function animateCounter(el, target, duration) {
    duration = duration || 900;
    var start = 0;
    var startTime = null;

    function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

    function step(timestamp) {
      if (!startTime) startTime = timestamp;
      var progress = Math.min((timestamp - startTime) / duration, 1);
      var eased = easeOutCubic(progress);
      var current = Math.round(start + (target - start) * eased);
      el.textContent = current.toLocaleString();
      if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  // Observe funnel numbers
  var funnelNums = document.querySelectorAll('.funnel-num[data-target]');
  if (funnelNums.length) {
    var funnelObs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var target = parseInt(entry.target.dataset.target, 10);
          animateCounter(entry.target, target);
          funnelObs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.3 });
    funnelNums.forEach(function (el) { funnelObs.observe(el); });
  }

  /* ─── COUNT-UP for quality/confidence numbers ─── */
  var countUpEls = document.querySelectorAll('.nk-quality-num[data-target], .nk-confidence-big');
  if (countUpEls.length) {
    var countObs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var target = parseFloat(entry.target.dataset.target || entry.target.textContent);
          if (!isNaN(target)) {
            animateCounter(entry.target, target, 1200);
          }
          countObs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.3 });
    countUpEls.forEach(function (el) { countObs.observe(el); });
  }

  /* ─── LIGHTBOX (supports image, PDF, video) ─── */
  var lightbox = document.getElementById('lightbox');
  var lightboxImg = document.getElementById('lightbox-img');
  var lightboxPdf = document.getElementById('lightbox-pdf');
  var lightboxVideo = document.getElementById('lightbox-video');
  var lightboxCaption = document.getElementById('lightbox-caption');
  var lightboxExternal = document.getElementById('lightbox-external');
  var lightboxClose = document.getElementById('lightbox-close');

  function hideAllLightboxMedia() {
    if (lightboxImg) { lightboxImg.style.display = 'none'; lightboxImg.src = ''; }
    if (lightboxPdf) { lightboxPdf.style.display = 'none'; lightboxPdf.src = ''; }
    if (lightboxVideo) { lightboxVideo.style.display = 'none'; lightboxVideo.pause(); lightboxVideo.src = ''; }
    if (lightboxExternal) { lightboxExternal.style.display = 'none'; lightboxExternal.href = ''; }
  }

  function openLightbox(src, caption, fileType) {
    if (!lightbox) return;
    hideAllLightboxMedia();
    var type = (fileType || '').toLowerCase();
    if (!type) {
      // Infer from extension
      var ext = (src.split('.').pop() || '').toLowerCase();
      if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'].indexOf(ext) !== -1) type = 'image';
      else if (ext === 'pdf') type = 'pdf';
      else if (['mp4', 'webm', 'ogg', 'mov'].indexOf(ext) !== -1) type = 'video';
      else type = 'image'; // fallback
    }
    if (type === 'pdf') {
      lightboxPdf.src = src;
      lightboxPdf.style.display = 'block';
    } else if (type === 'video') {
      lightboxVideo.src = src;
      lightboxVideo.style.display = 'block';
    } else {
      lightboxImg.src = src;
      lightboxImg.alt = caption || '';
      lightboxImg.style.display = 'block';
    }
    lightboxCaption.textContent = caption || '';
    // Always show external link as fallback
    if (lightboxExternal && src) {
      lightboxExternal.href = src;
      lightboxExternal.style.display = 'inline-block';
    }
    lightbox.setAttribute('data-open', 'true');
    lightbox.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeLightbox() {
    if (!lightbox) return;
    lightbox.removeAttribute('data-open');
    lightbox.setAttribute('aria-hidden', 'true');
    hideAllLightboxMedia();
    document.body.style.overflow = '';
  }

  if (lightbox) {
    document.querySelectorAll('.chart-trigger').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var img = btn.querySelector('img:not([style*="display: none"])') || btn.querySelector('img');
        if (img) openLightbox(img.src, img.alt, 'image');
      });
    });
    lightboxClose && lightboxClose.addEventListener('click', closeLightbox);
    lightbox.addEventListener('click', function (e) {
      if (e.target === lightbox) closeLightbox();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeLightbox();
    });
  }

  /* ─── EVAL VIEWER (Dataset Table) ─── */
  function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function evalRunBadge(val) {
    if (!val) return '<span class="outcome-fail">—</span>';
    var v = String(val).toUpperCase();
    if (v === 'PASS' || v === '1') return '<span class="outcome-pass">PASS</span>';
    if (v === 'FAIL' || v === '0') return '<span class="outcome-fail">FAIL</span>';
    return '<span>' + esc(String(val)) + '</span>';
  }

  var evalState = {
    data: [],
    filtered: [],
    page: 1,
    perPage: 10,
    search: '',
    difficulty: '',
    sortKey: 'kimi_pass',
    sortDir: 'desc',
    expanded: {}
  };

  var searchInput = document.getElementById('nk-eval-search');
  var difficultySelect = document.getElementById('nk-eval-difficulty');
  var sortSelect = document.getElementById('nk-eval-sort');
  var sortDirBtn = document.getElementById('nk-eval-sort-dir-btn');
  var evalCount = document.getElementById('nk-eval-count');
  var evalTbody = document.getElementById('nk-eval-tbody');
  var evalPagination = document.getElementById('nk-eval-pagination');

  // Fetch dataset
  function fetchData() {
    fetch('/nokor/api/dataset')
      .then(function (res) { return res.json(); })
      .then(function (data) {
        evalState.data = data;
        applyFilters();
      })
      .catch(function () {
        if (evalCount) evalCount.textContent = 'Failed to load data';
      });
  }

  function applyFilters() {
    var s = evalState.search.toLowerCase();
    var d = evalState.difficulty;
    evalState.filtered = evalState.data.filter(function (row) {
      if (d && row.level !== d) return false;
      if (s) {
        var hay = (row.instance_id + ' ' + row.modality + ' ' + row.level + ' ' + row.tools).toLowerCase();
        if (hay.indexOf(s) === -1) return false;
      }
      return true;
    });
    sortData();
    evalState.page = 1;
    render();
  }

  function sortData() {
    var key = evalState.sortKey;
    var dir = evalState.sortDir === 'asc' ? 1 : -1;

    evalState.filtered.sort(function (a, b) {
      var av, bv;
      switch (key) {
        case 'instance_id':
          av = a.instance_id; bv = b.instance_id;
          return av < bv ? -dir : av > bv ? dir : 0;
        case 'level':
          var levels = { 'Hard': 1, 'Very Hard': 2, 'Expert': 3 };
          av = levels[a.level] || 0; bv = levels[b.level] || 0;
          return (av - bv) * dir;
        case 'modality':
          av = a.modality; bv = b.modality;
          return av < bv ? -dir : av > bv ? dir : 0;
        case 'kimi_pass':
          av = a.kimi_2_5 ? a.kimi_2_5.score : 0;
          bv = b.kimi_2_5 ? b.kimi_2_5.score : 0;
          return (av - bv) * dir;
        case 'nova_pass':
          av = a.nova_2_lite ? a.nova_2_lite.score : 0;
          bv = b.nova_2_lite ? b.nova_2_lite.score : 0;
          return (av - bv) * dir;
        default:
          return 0;
      }
    });
  }

  function render() {
    if (!evalTbody) return;

    var total = evalState.filtered.length;
    var totalPages = Math.ceil(total / evalState.perPage);
    var start = (evalState.page - 1) * evalState.perPage;
    var end = start + evalState.perPage;
    var pageData = evalState.filtered.slice(start, end);

    if (evalCount) {
      evalCount.textContent = total + ' instance' + (total !== 1 ? 's' : '') + ' found';
    }

    // Render rows
    var html = '';
    pageData.forEach(function (row) {
      var id = row.instance_id;
      var shortId = id.replace('nokor__', '');
      var kimiOutcome = row.kimi_2_5 ? row.kimi_2_5.outcome : '—';
      var novaOutcome = row.nova_2_lite ? row.nova_2_lite.outcome : '—';
      var kimiClass = kimiOutcome === 'PASS' ? 'outcome-pass' : 'outcome-fail';
      var novaClass = novaOutcome === 'PASS' ? 'outcome-pass' : 'outcome-fail';
      var diffClass = 'diff-badge diff-badge--' + row.level.toLowerCase().replace(/\s+/g, '-');
      var isExpanded = evalState.expanded[id];

      html += '<tr class="' + (isExpanded ? 'row-expanded' : '') + '" data-id="' + id + '">';
      html += '<td class="eth-instance"><span class="mono" style="font-size:12px;">' + shortId + '</span></td>';
      html += '<td class="eth-level"><span class="' + diffClass + '">' + row.level + '</span></td>';
      html += '<td class="eth-modality">' + row.modality + '</td>';
      html += '<td class="eth-kimi"><span class="' + kimiClass + '">' + kimiOutcome + '</span></td>';
      html += '<td class="eth-nova"><span class="' + novaClass + '">' + novaOutcome + '</span></td>';
      html += '<td class="eth-tools"><span style="font-size:12px;">' + row.num_tools + ' tools</span></td>';
      html += '<td class="eth-expand"><button type="button" class="expand-btn' + (isExpanded ? ' expanded' : '') + '" data-expand="' + id + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M9 5l7 7-7 7"/></svg></button></td>';
      html += '</tr>';

      if (isExpanded) {
        html += '<tr class="detail-row"><td colspan="7">';
        html += '<div class="detail-content">';
        html += '<div class="eval-detail-grid">';

        // Instance Info block
        html += '<div class="detail-block">';
        html += '<div class="detail-block-title">Instance Info</div>';
        html += '<div class="detail-row-item"><span class="detail-key">Task ID</span><span class="detail-val">' + esc(row.task_id) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Level</span><span class="detail-val">' + esc(row.level) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Modality</span><span class="detail-val">' + esc(row.modality) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Num Steps</span><span class="detail-val">' + row.num_steps + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Num Tools</span><span class="detail-val">' + row.num_tools + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Est. Time</span><span class="detail-val">' + esc(row.estimated_time) + '</span></div>';
        if (row.file_name) {
          html += '<div class="detail-row-item"><span class="detail-key">File</span><span class="detail-val">' + esc(row.file_name) + '</span></div>';
        }
        html += '</div>';

        // Kimi 2.5 block
        if (row.kimi_2_5) {
          html += '<div class="detail-block">';
          html += '<div class="detail-block-title">Kimi 2.5</div>';
          html += '<div class="detail-row-item"><span class="detail-key">Outcome</span><span class="detail-val">' + evalRunBadge(row.kimi_2_5.outcome) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Answer</span><span class="detail-val">' + esc(String(row.kimi_2_5.answer)) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Steps</span><span class="detail-val">' + row.kimi_2_5.steps_taken + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Time</span><span class="detail-val">' + row.kimi_2_5.time_seconds + 's</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Cost</span><span class="detail-val">$' + row.kimi_2_5.cost_usd.toFixed(2) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Tools Used</span><span class="detail-val">' + esc((row.kimi_2_5.tools_used || []).join(', ')) + '</span></div>';
          html += '</div>';
        }

        // Nova-2-Lite block
        if (row.nova_2_lite) {
          html += '<div class="detail-block">';
          html += '<div class="detail-block-title">Nova-2-Lite</div>';
          html += '<div class="detail-row-item"><span class="detail-key">Outcome</span><span class="detail-val">' + evalRunBadge(row.nova_2_lite.outcome) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Answer</span><span class="detail-val">' + esc(String(row.nova_2_lite.answer)) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Steps</span><span class="detail-val">' + row.nova_2_lite.steps_taken + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Time</span><span class="detail-val">' + row.nova_2_lite.time_seconds + 's</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Cost</span><span class="detail-val">$' + row.nova_2_lite.cost_usd.toFixed(2) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Tools Used</span><span class="detail-val">' + esc((row.nova_2_lite.tools_used || []).join(', ')) + '</span></div>';
          html += '</div>';
        }

        // Task Meta block
        html += '<div class="detail-block">';
        html += '<div class="detail-block-title">Task Meta</div>';
        html += '<div class="detail-row-item"><span class="detail-key">Tools</span><span class="detail-val">' + esc(row.tools) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">File Type</span><span class="detail-val">' + esc(row.file_type || 'N/A') + '</span></div>';
        if (row.file_view_url) {
          html += '<div class="detail-row-item"><span class="detail-key">View File</span><span class="detail-val"><button type="button" class="detail-link file-preview-btn" data-src="' + esc(row.file_view_url) + '" data-type="' + esc(row.file_type || '') + '" data-name="' + esc(row.file_name || '') + '">Preview ⇢</button></span></div>';
        }
        html += '</div>';

        html += '</div>'; // close eval-detail-grid
        html += '</div>'; // close detail-content
        html += '</td></tr>';
      }
    });
    evalTbody.innerHTML = html;

    // Pagination
    if (evalPagination) {
      var pHtml = '';
      pHtml += '<button type="button" data-page="prev"' + (evalState.page <= 1 ? ' disabled' : '') + '>← Prev</button>';
      for (var p = 1; p <= totalPages; p++) {
        pHtml += '<button type="button" data-page="' + p + '"' + (p === evalState.page ? ' class="active"' : '') + '>' + p + '</button>';
      }
      pHtml += '<button type="button" data-page="next"' + (evalState.page >= totalPages ? ' disabled' : '') + '>Next →</button>';
      evalPagination.innerHTML = pHtml;
    }
  }

  // Event listeners
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      evalState.search = this.value;
      applyFilters();
    });
  }
  if (difficultySelect) {
    difficultySelect.addEventListener('change', function () {
      evalState.difficulty = this.value;
      applyFilters();
    });
  }
  if (sortSelect) {
    sortSelect.addEventListener('change', function () {
      evalState.sortKey = this.value;
      applyFilters();
    });
  }
  if (sortDirBtn) {
    sortDirBtn.addEventListener('click', function () {
      evalState.sortDir = evalState.sortDir === 'desc' ? 'asc' : 'desc';
      this.textContent = evalState.sortDir === 'desc' ? '↓' : '↑';
      this.title = evalState.sortDir === 'desc' ? 'Descending – click to reverse' : 'Ascending – click to reverse';
      applyFilters();
    });
  }

  // Expand/collapse row delegation
  if (evalTbody) {
    evalTbody.addEventListener('click', function (e) {
      if (e.target.closest('.file-preview-btn')) return; // let preview handler deal with it
      var btn = e.target.closest('.expand-btn');
      if (!btn) return;
      var id = btn.dataset.expand;
      evalState.expanded[id] = !evalState.expanded[id];
      render();
    });
  }

  // File preview button delegation
  if (evalTbody) {
    evalTbody.addEventListener('click', function (e) {
      var btn = e.target.closest('.file-preview-btn');
      if (!btn) return;
      e.stopPropagation();
      var src = btn.dataset.src;
      var ftype = btn.dataset.type;
      var fname = btn.dataset.name;
      if (src) openLightbox(src, fname, ftype);
    });
  }

  // Pagination delegation
  if (evalPagination) {
    evalPagination.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-page]');
      if (!btn || btn.disabled) return;
      var page = btn.dataset.page;
      if (page === 'prev') evalState.page--;
      else if (page === 'next') evalState.page++;
      else evalState.page = parseInt(page, 10);
      render();
    });
  }

  // Init
  if (evalTbody) {
    fetchData();
  }

})();
