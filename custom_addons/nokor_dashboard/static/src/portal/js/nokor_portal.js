/* ═══════════════════════════════════════════════════════════════════
   TERRA PORTAL — Interactive JS
   Theme toggle · Scroll progress · GSAP animations · Dataset viewer
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ─── THEME TOGGLE ─── */
  const THEME_KEY = 'terra:theme';
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
      const current = document.documentElement.getAttribute('data-theme') || 'dark';
      setTheme(current === 'dark' ? 'light' : 'dark');
    });
  }

  // If no saved theme, default to dark
  if (!getTheme()) {
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

  /* ─── COUNTER ANIMATION UTILITY ─── */
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
    if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'].indexOf(type) !== -1) type = 'image';
    else if (type === 'pdf') type = 'pdf';
    else if (['mp4', 'webm', 'ogg', 'mov', 'video'].indexOf(type) !== -1) type = 'video';
    else if (['csv', 'xlsx', 'xls'].indexOf(type) !== -1) type = 'spreadsheet';
    else if (!type) {
      var ext = (src.split('.').pop() || '').toLowerCase();
      if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'].indexOf(ext) !== -1) type = 'image';
      else if (ext === 'pdf') type = 'pdf';
      else if (['mp4', 'webm', 'ogg', 'mov'].indexOf(ext) !== -1) type = 'video';
      else if (['csv', 'xlsx', 'xls'].indexOf(ext) !== -1) type = 'spreadsheet';
      else type = 'image';
    }
    if (type === 'spreadsheet') {
      lightboxPdf.src = 'https://docs.google.com/gview?url=' + encodeURIComponent(src) + '&embedded=true';
      lightboxPdf.style.display = 'block';
    } else if (type === 'pdf') {
      lightboxPdf.src = src;
      lightboxPdf.style.display = 'block';
    } else if (type === 'video') {
      lightboxVideo.src = src;
      lightboxVideo.style.display = 'block';
      lightboxVideo.play();
    } else {
      lightboxImg.src = src;
      lightboxImg.alt = caption || '';
      lightboxImg.style.display = 'block';
    }
    lightboxCaption.textContent = caption || '';
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
        var img = btn.querySelector('img.nk-chart-light') || btn.querySelector('img');
        var isDark = document.documentElement.getAttribute('data-theme') === 'dark' || (!document.documentElement.hasAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
        if (isDark) { img = btn.querySelector('img.nk-chart-dark') || img; }
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
  function scoreBadge(score) {
    if (score === true) return '<span class="outcome-pass">PASS</span>';
    if (score === false) return '<span class="outcome-fail">FAIL</span>';
    return '<span class="outcome-fail">—</span>';
  }

  function formatCategory(cat) {
    if (!cat) return '';
    return cat.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
  }

  function renderAnswerCell(text, uid) {
    if (!text) return '<span class="answer-cell-empty">—</span>';
    var words = text.split(/\s+/);
    if (words.length <= 15) {
      return '<span class="answer-cell-text">' + esc(text) + '</span>';
    }
    var truncated = words.slice(0, 15).join(' ');
    return '<div class="answer-cell-wrap" id="ans-' + uid + '">' +
      '<span class="answer-cell-text">' + esc(truncated) + '…</span>' +
      '<button type="button" class="answer-see-more" data-target="ans-' + uid + '">See more</button>' +
      '<span class="answer-cell-full" style="display:none;">' + esc(text) + '</span>' +
      '</div>';
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

  function fetchData() {
    fetch('/terra/api/dataset')
      .then(function (res) {
        if (!res.ok) return fetch('/nokor/api/dataset');
        return res;
      })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        evalState.data = data;
        applyFilters();
        updateSummaryStats(data);
      })
      .catch(function () {
        if (evalCount) evalCount.textContent = 'Failed to load data';
      });
  }

  function updateSummaryStats(data) {
    var total = data.length;
    var kimiPass = 0, novaPass = 0;
    var hardCount = 0, vhardCount = 0, expertCount = 0;
    var categoryCounts = {};

    data.forEach(function (row) {
      if (row.kimi_k2_5 && row.kimi_k2_5.score) kimiPass++;
      if (row.nova_2_lite && row.nova_2_lite.score) novaPass++;
      if (row.level === 'Hard') hardCount++;
      else if (row.level === 'Very Hard') vhardCount++;
      else if (row.level === 'Expert') expertCount++;

      var cat = row.category || 'unknown';
      categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
    });

    var kimiAcc = total > 0 ? Math.round((kimiPass / total) * 100) : 0;
    var novaAcc = total > 0 ? Math.round((novaPass / total) * 100) : 0;
    var accGap = Math.abs(kimiAcc - novaAcc);
    var meanAcc = ((kimiAcc + novaAcc) / 2).toFixed(1);

    // §04 Results
    var kimiScoreEl = document.getElementById('nk-kimi-score');
    var novaScoreEl = document.getElementById('nk-nova-score');
    var kimiDetailEl = document.getElementById('nk-kimi-detail');
    var novaDetailEl = document.getElementById('nk-nova-detail');
    if (kimiScoreEl) kimiScoreEl.innerHTML = kimiAcc + '<small>%</small>';
    if (novaScoreEl) novaScoreEl.innerHTML = novaAcc + '<small>%</small>';
    if (kimiDetailEl) kimiDetailEl.textContent = kimiPass + '/' + total + ' pass';
    if (novaDetailEl) novaDetailEl.textContent = novaPass + '/' + total + ' pass';

    // §05 Model comparison
    var kimiAccEl = document.getElementById('nk-kimi-acc');
    var novaAccEl = document.getElementById('nk-nova-acc');
    var kimiPassCountEl = document.getElementById('nk-kimi-pass-count');
    var novaPassCountEl = document.getElementById('nk-nova-pass-count');
    var accGapEl = document.getElementById('nk-acc-gap');
    var meanAccEl = document.getElementById('nk-mean-acc');
    if (kimiAccEl) kimiAccEl.textContent = kimiAcc;
    if (novaAccEl) novaAccEl.textContent = novaAcc;
    if (kimiPassCountEl) kimiPassCountEl.textContent = kimiPass + '/' + total + ' PASS';
    if (novaPassCountEl) novaPassCountEl.textContent = novaPass + '/' + total + ' PASS';
    if (accGapEl) accGapEl.textContent = accGap;
    if (meanAccEl) meanAccEl.textContent = meanAcc + '%';

    // §06 Difficulty distribution
    var distLevels = [
      { key: 'hard', name: 'Hard' },
      { key: 'vhard', name: 'Very Hard' },
      { key: 'expert', name: 'Expert' }
    ];
    distLevels.forEach(function (lvl) {
      var items = data.filter(function (r) { return r.level === lvl.name; });
      var count = items.length;
      var kimiPass = items.filter(function (r) { return r.kimi_k2_5 && r.kimi_k2_5.score; }).length;
      var novaPass = items.filter(function (r) { return r.nova_2_lite && r.nova_2_lite.score; }).length;
      var combinedPass = kimiPass + novaPass;
      var combinedTotal = count * 2;
      var kimiPct = count > 0 ? Math.round((kimiPass / count) * 100) : 0;
      var novaPct = count > 0 ? Math.round((novaPass / count) * 100) : 0;
      var combinedPct = combinedTotal > 0 ? Math.round((combinedPass / combinedTotal) * 100) : 0;

      var countEl = document.getElementById('nk-dist-' + lvl.key + '-count');
      var kimiPctEl = document.getElementById('nk-dist-' + lvl.key + '-kimi-pct');
      var novaPctEl = document.getElementById('nk-dist-' + lvl.key + '-nova-pct');
      var combinedPctEl = document.getElementById('nk-dist-' + lvl.key + '-combined-pct');
      var kimiDetailEl = document.getElementById('nk-dist-' + lvl.key + '-kimi-pass');
      var novaDetailEl = document.getElementById('nk-dist-' + lvl.key + '-nova-pass');
      var combinedDetailEl = document.getElementById('nk-dist-' + lvl.key + '-combined');

      if (countEl) countEl.textContent = count + ' tasks';
      if (kimiPctEl) kimiPctEl.textContent = kimiPct + '%';
      if (novaPctEl) novaPctEl.textContent = novaPct + '%';
      if (combinedPctEl) combinedPctEl.textContent = combinedPct + '%';
      if (kimiDetailEl) kimiDetailEl.textContent = kimiPass + '/' + count + ' pass';
      if (novaDetailEl) novaDetailEl.textContent = novaPass + '/' + count + ' pass';
      if (combinedDetailEl) combinedDetailEl.textContent = combinedPass + '/' + combinedTotal + ' pass';
    });

    // Modality strip — pass score per category
    var modalityStrip = document.getElementById('nk-modality-strip');
    if (modalityStrip) {
      var stripHtml = '';
      Object.keys(categoryCounts).sort().forEach(function (cat) {
        var catItems = data.filter(function (r) { return r.category === cat; });
        var catTotal = catItems.length * 2;
        var catPass = catItems.filter(function (r) { return r.kimi_k2_5 && r.kimi_k2_5.score; }).length +
                      catItems.filter(function (r) { return r.nova_2_lite && r.nova_2_lite.score; }).length;
        var catPct = catTotal > 0 ? Math.round((catPass / catTotal) * 100) : 0;
        stripHtml += '<div class="mod-item"><span class="mod-pct">' + catPct + '%</span><span class="mod-name mono">' + formatCategory(cat) + '</span></div>';
      });
      modalityStrip.innerHTML = stripHtml;
    }

    // Glance instances
    var glanceInstances = document.getElementById('nk-glance-instances');
    if (glanceInstances) glanceInstances.innerHTML = '<b>' + total.toLocaleString() + '</b>';

    // Eval KPI cards per difficulty level
    var levels = ['hard', 'vhard', 'expert'];
    var levelMap = { 'hard': 'Hard', 'vhard': 'Very Hard', 'expert': 'Expert' };
    levels.forEach(function (lvlKey) {
      var lvlName = levelMap[lvlKey];
      var items = data.filter(function (r) { return r.level === lvlName; });
      var count = items.length;
      var kimiCorrect = items.filter(function (r) { return r.kimi_k2_5 && r.kimi_k2_5.score; }).length;
      var novaCorrect = items.filter(function (r) { return r.nova_2_lite && r.nova_2_lite.score; }).length;
      var totalAttempts = count * 2;
      var totalPass = kimiCorrect + novaCorrect;
      var failPct = totalAttempts > 0 ? Math.round(((totalAttempts - totalPass) / totalAttempts) * 100) : 0;

      var countEl = document.getElementById('nk-kpi-' + lvlKey + '-count');
      var kimiEl = document.getElementById('nk-kpi-' + lvlKey + '-kimi');
      var novaEl = document.getElementById('nk-kpi-' + lvlKey + '-nova');
      var failEl = document.getElementById('nk-kpi-' + lvlKey + '-fail');

      if (countEl) countEl.textContent = count + ' tasks';
      if (kimiEl) kimiEl.textContent = kimiCorrect + '/' + count;
      if (novaEl) novaEl.textContent = novaCorrect + '/' + count;
      if (failEl) failEl.textContent = failPct + '%';
    });
  }

  function applyFilters() {
    var s = evalState.search.toLowerCase();
    var d = evalState.difficulty;
    evalState.filtered = evalState.data.filter(function (row) {
      if (d && row.level !== d) return false;
      if (s) {
        var hay = (row.task_id + ' ' + row.category + ' ' + row.level + ' ' + (row.ground_truth || '') + ' ' + (row.file_name || '')).toLowerCase();
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
        case 'task_id':
          av = a.task_id; bv = b.task_id;
          return av < bv ? -dir : av > bv ? dir : 0;
        case 'level':
          var levels = { 'Hard': 1, 'Very Hard': 2, 'Expert': 3 };
          av = levels[a.level] || 0; bv = levels[b.level] || 0;
          return (av - bv) * dir;
        case 'category':
          av = a.category || ''; bv = b.category || '';
          return av < bv ? -dir : av > bv ? dir : 0;
        case 'kimi_pass':
          av = a.kimi_k2_5 ? (a.kimi_k2_5.score ? 1 : 0) : -1;
          bv = b.kimi_k2_5 ? (b.kimi_k2_5.score ? 1 : 0) : -1;
          return (av - bv) * dir;
        case 'nova_pass':
          av = a.nova_2_lite ? (a.nova_2_lite.score ? 1 : 0) : -1;
          bv = b.nova_2_lite ? (b.nova_2_lite.score ? 1 : 0) : -1;
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

    var html = '';
    pageData.forEach(function (row) {
      var id = row.task_id;
      var shortId = id.length > 8 ? id.substring(0, 8) + '…' : id;
      var kimiScore = row.kimi_k2_5 ? row.kimi_k2_5.score : null;
      var novaScore = row.nova_2_lite ? row.nova_2_lite.score : null;
      var diffClass = 'diff-badge diff-badge--' + row.level.toLowerCase().replace(/\s+/g, '-');
      var isExpanded = evalState.expanded[id];

      html += '<tr class="' + (isExpanded ? 'row-expanded' : '') + '" data-id="' + id + '">';
      html += '<td class="eth-instance"><span class="mono" style="font-size:12px;" title="' + esc(id) + '">' + esc(shortId) + '</span></td>';
      html += '<td class="eth-level"><span class="' + diffClass + '">' + esc(row.level) + '</span></td>';
      html += '<td class="eth-modality">' + esc(formatCategory(row.category)) + '</td>';
      html += '<td class="eth-kimi">' + scoreBadge(kimiScore) + '</td>';
      html += '<td class="eth-nova">' + scoreBadge(novaScore) + '</td>';
      html += '<td class="eth-expand"><button type="button" class="expand-btn' + (isExpanded ? ' expanded' : '') + '" data-expand="' + id + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M9 5l7 7-7 7"/></svg></button></td>';
      html += '</tr>';

      if (isExpanded) {
        var hasFile = row.file_view_url && row.file_name;
        var gridClass = hasFile ? 'eval-detail-grid' : 'eval-detail-grid eval-detail-grid--no-file';
        html += '<tr class="detail-row"><td colspan="6">';
        html += '<div class="detail-content">';
        html += '<div class="' + gridClass + '">';

        // Instance Info
        html += '<div class="detail-block">';
        html += '<div class="detail-block-title">Instance Info</div>';
        html += '<div class="detail-row-item"><span class="detail-key">Task ID</span><span class="detail-val mono" style="font-size:11px;">' + esc(row.task_id) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Level</span><span class="detail-val">' + esc(row.level) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Category</span><span class="detail-val">' + esc(formatCategory(row.category)) + '</span></div>';
        html += '<div class="detail-row-item"><span class="detail-key">Ground Truth</span><span class="detail-val">' + esc(row.ground_truth) + '</span></div>';
        if (row.file_name) {
          html += '<div class="detail-row-item"><span class="detail-key">File</span><span class="detail-val">' + esc(row.file_name) + '</span></div>';
        }
        html += '</div>';

        // Kimi K2.5
        if (row.kimi_k2_5) {
          html += '<div class="detail-block">';
          html += '<div class="detail-block-title">Kimi K2.5</div>';
          html += '<div class="detail-row-item"><span class="detail-key">Score</span><span class="detail-val">' + scoreBadge(row.kimi_k2_5.score) + '</span></div>';
          html += '<div class="detail-row-item detail-row-item--answer"><span class="detail-key">Answer</span><span class="detail-val">' + renderAnswerCell(String(row.kimi_k2_5.model_answer || ''), id + '-kimi') + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">LLM Calls</span><span class="detail-val">' + (row.kimi_k2_5.llm_calls || 0) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Avg Latency</span><span class="detail-val">' + (row.kimi_k2_5.avg_latency_seconds || 0).toFixed(2) + 's</span></div>';
          html += '</div>';
        }

        // Nova-2-Lite
        if (row.nova_2_lite) {
          html += '<div class="detail-block">';
          html += '<div class="detail-block-title">Nova-2-Lite</div>';
          html += '<div class="detail-row-item"><span class="detail-key">Score</span><span class="detail-val">' + scoreBadge(row.nova_2_lite.score) + '</span></div>';
          html += '<div class="detail-row-item detail-row-item--answer"><span class="detail-key">Answer</span><span class="detail-val">' + renderAnswerCell(String(row.nova_2_lite.model_answer || ''), id + '-nova') + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">LLM Calls</span><span class="detail-val">' + (row.nova_2_lite.llm_calls || 0) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Avg Latency</span><span class="detail-val">' + (row.nova_2_lite.avg_latency_seconds || 0).toFixed(2) + 's</span></div>';
          html += '</div>';
        }

        // File preview (only if file exists)
        if (hasFile) {
          var fileExt = (row.file_name || '').split('.').pop().toLowerCase();
          html += '<div class="detail-block">';
          html += '<div class="detail-block-title">Attached File</div>';
          html += '<div class="detail-row-item"><span class="detail-key">File</span><span class="detail-val">' + esc(row.file_name) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Type</span><span class="detail-val mono">' + esc(fileExt.toUpperCase()) + '</span></div>';
          html += '<div class="detail-row-item"><span class="detail-key">Preview</span><span class="detail-val"><button type="button" class="detail-link file-preview-btn" data-src="' + esc(row.file_view_url) + '" data-type="' + esc(fileExt) + '" data-name="' + esc(row.file_name) + '">Preview ⇢</button></span></div>';
          html += '</div>';
        }

        html += '</div>';
        html += '</div>';
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
      if (e.target.closest('.file-preview-btn')) return;
      if (e.target.closest('.answer-see-more')) return;
      var btn = e.target.closest('.expand-btn');
      if (!btn) return;
      var id = btn.dataset.expand;
      evalState.expanded[id] = !evalState.expanded[id];
      render();
    });
  }

  // Answer see more/less toggle
  if (evalTbody) {
    evalTbody.addEventListener('click', function (e) {
      var btn = e.target.closest('.answer-see-more');
      if (!btn) return;
      e.stopPropagation();
      var wrap = document.getElementById(btn.dataset.target);
      if (!wrap) return;
      var shortText = wrap.querySelector('.answer-cell-text');
      var fullText = wrap.querySelector('.answer-cell-full');
      if (fullText.style.display === 'none') {
        shortText.style.display = 'none';
        fullText.style.display = 'block';
        btn.textContent = 'See less';
      } else {
        shortText.style.display = '';
        fullText.style.display = 'none';
        btn.textContent = 'See more';
      }
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
