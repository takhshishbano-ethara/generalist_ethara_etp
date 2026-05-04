(function () {
    'use strict';

    /* ─── Dark Mode ──────────────────────────────────────────── */
    function initDarkMode() {
        const toggle = document.querySelector('.kr-dark-toggle');
        const showcase = document.querySelector('.kr-showcase');
        if (!toggle || !showcase) return;

        const stored = localStorage.getItem('kr-dark');
        if (stored === 'true' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
            showcase.classList.add('kr-dark');
            toggle.querySelector('.kr-dark-toggle__icon').textContent = '☀️';
        }

        toggle.addEventListener('click', function () {
            showcase.classList.toggle('kr-dark');
            const isDark = showcase.classList.contains('kr-dark');
            localStorage.setItem('kr-dark', isDark);
            toggle.querySelector('.kr-dark-toggle__icon').textContent = isDark ? '☀️' : '🌙';
        });
    }

    /* ─── Scroll Animations (per-element data-animate system) ── */
    function initScrollAnimations() {
        var elements = document.querySelectorAll('[data-animate]');
        if (!elements.length) return;

        var observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    entry.target.classList.add('kr-visible');
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.15 });

        elements.forEach(function (el) { observer.observe(el); });
    }

    /* ─── Word-by-Word Fade (subtitle) ───────────────────────── */
    function initWordFade() {
        var el = document.querySelector('[data-word-fade]');
        if (!el) return;

        var text = el.textContent.trim();
        var words = text.split(/\s+/);
        el.textContent = '';
        el.setAttribute('aria-label', text);

        words.forEach(function (word, i) {
            var span = document.createElement('span');
            span.className = 'kr-word';
            span.textContent = word;
            span.style.transitionDelay = (i * 0.08) + 's';
            el.appendChild(span);
            if (i < words.length - 1) {
                el.appendChild(document.createTextNode(' '));
            }
        });

        requestAnimationFrame(function () {
            setTimeout(function () {
                el.querySelectorAll('.kr-word').forEach(function (w) {
                    w.classList.add('kr-word--visible');
                });
            }, 300);
        });
    }

    /* ─── Count Up Animation ─────────────────────────────────── */
    function initCountUp() {
        const els = document.querySelectorAll('.kr-countup');
        if (!els.length) return;

        const observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    animateCount(entry.target);
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.5 });

        els.forEach(function (el) { observer.observe(el); });

        function animateCount(el) {
            var target = parseFloat(el.getAttribute('data-target'));
            var suffix = el.getAttribute('data-suffix') || '';
            var duration = 1500;
            var start = performance.now();

            function tick(now) {
                var progress = Math.min((now - start) / duration, 1);
                var eased = 1 - Math.pow(1 - progress, 3);
                var current = target * eased;
                el.textContent = (target % 1 !== 0 ? current.toFixed(1) : Math.round(current)) + suffix;
                if (progress < 1) {
                    requestAnimationFrame(tick);
                } else {
                    el.textContent = (target % 1 !== 0 ? target.toFixed(1) : target) + suffix;
                    el.classList.add('kr-countup--done');
                }
            }
            requestAnimationFrame(tick);
        }
    }

    /* ─── Lightbox ───────────────────────────────────────────── */
    function initLightbox() {
        var lightbox = document.querySelector('.kr-lightbox');
        var lightboxImg = document.querySelector('.kr-lightbox__img');
        var closeBtn = document.querySelector('.kr-lightbox__close');
        if (!lightbox) return;

        document.querySelectorAll('.kr-chart-card__img').forEach(function (img) {
            img.style.cursor = 'pointer';
            img.addEventListener('click', function () {
                lightboxImg.src = img.src;
                lightboxImg.alt = img.alt;
                requestAnimationFrame(function () {
                    lightbox.classList.add('kr-lightbox--active');
                    lightbox.setAttribute('aria-hidden', 'false');
                    document.body.style.overflow = 'hidden';
                });
            });
        });

        function closeLightbox() {
            lightbox.classList.remove('kr-lightbox--active');
            lightbox.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
            setTimeout(function () { lightboxImg.src = ''; }, 400);
        }

        closeBtn.addEventListener('click', closeLightbox);
        lightbox.addEventListener('click', function (e) {
            if (e.target === lightbox) closeLightbox();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') closeLightbox();
        });
    }

    /* ─── Dataset Viewer ─────────────────────────────────────── */
    function initDatasetViewer() {
        var tbody = document.querySelector('.kr-dataset__body');
        var searchInput = document.querySelector('.kr-dataset__search');
        var filterSelect = document.querySelector('.kr-dataset__filter');
        var modelFilter = document.querySelector('.kr-dataset__model-filter');
        var pagination = document.querySelector('.kr-dataset__pagination');
        if (!tbody) return;

        var instances = [];
        var currentPage = 1;
        var pageSize = 10;
        var sortField = 'instance_id';
        var sortDir = 'asc';

        fetch('/kraken/api/instances')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                instances = data;
                render();
            })
            .catch(function (err) {
                console.error('Failed to load Kraken instances:', err);
                tbody.innerHTML = '<tr><td colspan="8" class="kr-dataset__empty">Failed to load data</td></tr>';
            });

        function getFiltered() {
            var search = (searchInput.value || '').toLowerCase();
            var diff = filterSelect.value;
            var model = modelFilter.value;

            return instances.filter(function (inst) {
                if (diff && inst.difficulty !== diff) return false;
                if (search && inst.instance_id.toLowerCase().indexOf(search) === -1) return false;
                if (model === 'glm5' && inst.glm5.outcome === '') return false;
                if (model === 'nova' && inst.nova.outcome === '') return false;
                return true;
            });
        }

        function getSorted(data) {
            var sorted = data.slice();
            sorted.sort(function (a, b) {
                var va, vb;
                switch (sortField) {
                    case 'instance_id': va = a.instance_id; vb = b.instance_id; break;
                    case 'difficulty':
                        var order = { 'Easy': 1, 'Medium': 2, 'Hard': 3, 'Expert': 4 };
                        va = order[a.difficulty] || 0; vb = order[b.difficulty] || 0; break;
                    case 'gold_speedup': va = a.gold_speedup; vb = b.gold_speedup; break;
                    case 'glm5_hsr': va = a.glm5.hsr; vb = b.glm5.hsr; break;
                    case 'nova_hsr': va = a.nova.hsr; vb = b.nova.hsr; break;
                    case 'glm5_outcome': va = a.glm5.outcome; vb = b.glm5.outcome; break;
                    case 'nova_outcome': va = a.nova.outcome; vb = b.nova.outcome; break;
                    default: va = a.instance_id; vb = b.instance_id;
                }
                if (va < vb) return sortDir === 'asc' ? -1 : 1;
                if (va > vb) return sortDir === 'asc' ? 1 : -1;
                return 0;
            });
            return sorted;
        }

        function outcomeClass(outcome) {
            switch (outcome) {
                case 'pass': return 'kr-outcome--pass';
                case 'correct_but_slow': return 'kr-outcome--slow';
                case 'fail': return 'kr-outcome--fail';
                default: return '';
            }
        }

        function outcomeLabel(outcome) {
            switch (outcome) {
                case 'pass': return 'Pass';
                case 'correct_but_slow': return 'Correct (Slow)';
                case 'fail': return 'Fail';
                default: return '-';
            }
        }

        function render() {
            var filtered = getFiltered();
            var sorted = getSorted(filtered);
            var totalPages = Math.ceil(sorted.length / pageSize) || 1;
            if (currentPage > totalPages) currentPage = totalPages;
            var start = (currentPage - 1) * pageSize;
            var page = sorted.slice(start, start + pageSize);

            var html = '';
            page.forEach(function (inst) {
                html += '<tr class="kr-dataset__row">';
                html += '<td class="kr-dataset__cell-id">' + escHtml(inst.instance_id) + '</td>';
                html += '<td><span class="kr-difficulty kr-difficulty--' + inst.difficulty.toLowerCase() + '">' + inst.difficulty + '</span></td>';
                html += '<td>' + inst.gold_speedup.toFixed(2) + 'x</td>';
                html += '<td>' + inst.glm5.hsr.toFixed(4) + '</td>';
                html += '<td>' + inst.nova.hsr.toFixed(4) + '</td>';
                html += '<td><span class="kr-outcome ' + outcomeClass(inst.glm5.outcome) + '">' + outcomeLabel(inst.glm5.outcome) + '</span></td>';
                html += '<td><span class="kr-outcome ' + outcomeClass(inst.nova.outcome) + '">' + outcomeLabel(inst.nova.outcome) + '</span></td>';
                html += '<td><button class="kr-dataset__expand" data-id="' + escHtml(inst.instance_id) + '">▶</button></td>';
                html += '</tr>';
                html += '<tr class="kr-dataset__detail" data-detail="' + escHtml(inst.instance_id) + '" style="display:none;">';
                html += '<td colspan="8">' + renderDetail(inst) + '</td>';
                html += '</tr>';
            });

            if (!page.length) {
                html = '<tr><td colspan="8" class="kr-dataset__empty">No instances match your filters</td></tr>';
            }

            tbody.innerHTML = html;
            renderPagination(totalPages);
            bindExpand();
        }

        function renderDetail(inst) {
            var d = '<div class="kr-detail">';
            d += '<div class="kr-detail__section">';
            d += '<h4>Instance Info</h4>';
            d += '<p><strong>Repository:</strong> <a href="' + escHtml(inst.repo_url) + '" target="_blank">' + escHtml(inst.repo_url) + '</a></p>';
            if (inst.pr_url) d += '<p><strong>PR:</strong> <a href="' + escHtml(inst.pr_url) + '" target="_blank">' + escHtml(inst.pr_url) + '</a></p>';
            d += '<p><strong>Language:</strong> ' + escHtml(inst.language) + '</p>';
            d += '<p><strong>Gold Speedup:</strong> ' + inst.gold_speedup.toFixed(2) + 'x</p>';
            d += '<p><strong>Covering Tests:</strong> ' + inst.covering_tests + '</p>';
            d += '</div>';

            d += '<div class="kr-detail__models">';
            d += renderModelDetail('GLM-5', inst.glm5);
            d += renderModelDetail('Nova-2-Lite', inst.nova);
            d += '</div>';
            d += '</div>';
            return d;
        }

        function renderModelDetail(name, m) {
            var d = '<div class="kr-detail__model">';
            d += '<h4>' + name + '</h4>';
            d += '<p><strong>Speedup (LM):</strong> ' + m.speedup_lm.toFixed(4) + 'x</p>';
            d += '<p><strong>Speedup (Adjusted):</strong> ' + m.speedup_adjusted.toFixed(4) + 'x</p>';
            d += '<p><strong>HSR:</strong> ' + m.hsr.toFixed(4) + '</p>';
            d += '<p><strong>Tests:</strong> ' + m.tests_passed + ' / ' + m.tests_total + '</p>';
            d += '<p><strong>Correctness:</strong> ' + m.correctness_pct.toFixed(1) + '%</p>';
            d += '<p><strong>Files Modified:</strong> ' + m.files_modified + '</p>';
            d += '<p><strong>Tool Calls:</strong> ' + m.tool_calls + '</p>';
            d += '<p><strong>Cost:</strong> $' + m.cost.toFixed(2) + '</p>';
            d += '<p><strong>Outcome:</strong> <span class="kr-outcome ' + outcomeClass(m.outcome) + '">' + outcomeLabel(m.outcome) + '</span></p>';
            d += '</div>';
            return d;
        }

        function renderPagination(totalPages) {
            if (totalPages <= 1) { pagination.innerHTML = ''; return; }
            var html = '';
            html += '<button class="kr-page-btn" data-page="prev" ' + (currentPage === 1 ? 'disabled' : '') + '>←</button>';
            for (var i = 1; i <= totalPages; i++) {
                html += '<button class="kr-page-btn' + (i === currentPage ? ' kr-page-btn--active' : '') + '" data-page="' + i + '">' + i + '</button>';
            }
            html += '<button class="kr-page-btn" data-page="next" ' + (currentPage === totalPages ? 'disabled' : '') + '>→</button>';
            pagination.innerHTML = html;

            pagination.querySelectorAll('.kr-page-btn').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var p = btn.getAttribute('data-page');
                    if (p === 'prev') currentPage = Math.max(1, currentPage - 1);
                    else if (p === 'next') currentPage = Math.min(totalPages, currentPage + 1);
                    else currentPage = parseInt(p);
                    render();
                });
            });
        }

        function bindExpand() {
            tbody.querySelectorAll('.kr-dataset__expand').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var id = btn.getAttribute('data-id');
                    var row = tbody.querySelector('[data-detail="' + id + '"]');
                    if (row) {
                        var visible = row.style.display !== 'none';
                        row.style.display = visible ? 'none' : 'table-row';
                        btn.textContent = visible ? '▶' : '▼';
                    }
                });
            });
        }

        document.querySelectorAll('.kr-dataset__table th[data-sort]').forEach(function (th) {
            th.style.cursor = 'pointer';
            th.addEventListener('click', function () {
                var field = th.getAttribute('data-sort');
                if (sortField === field) {
                    sortDir = sortDir === 'asc' ? 'desc' : 'asc';
                } else {
                    sortField = field;
                    sortDir = 'asc';
                }
                currentPage = 1;
                render();
            });
        });

        searchInput.addEventListener('input', function () { currentPage = 1; render(); });
        filterSelect.addEventListener('change', function () { currentPage = 1; render(); });
        modelFilter.addEventListener('change', function () { currentPage = 1; render(); });
    }

    function escHtml(str) {
        var div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    /* ─── Init ───────────────────────────────────────────────── */
    function init() {
        initDarkMode();
        initWordFade();
        initScrollAnimations();
        initCountUp();
        initLightbox();
        initDatasetViewer();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
