(() => {
  "use strict";

  const root = document.documentElement;
  const STORAGE_KEY = "janus:theme";
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");

  const currentTheme = () => {
    const explicit = root.getAttribute("data-theme");
    if (explicit === "light" || explicit === "dark") return explicit;
    return "dark";
  };

  const toggleBtn = document.getElementById("theme-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
    });
  }

  (() => {
    const bar = document.querySelector(".scroll-progress");
    if (!bar) return;
    let ticking = false;
    const update = () => {
      const doc = document.documentElement;
      const max = (doc.scrollHeight - window.innerHeight) || 1;
      const progress = Math.max(0, Math.min(1, window.scrollY / max));
      bar.style.width = (progress * 100).toFixed(2) + "%";
      ticking = false;
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(update);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    update();
  })();

  const lightbox = document.getElementById("lightbox");
  const lightboxImg = document.getElementById("lightbox-img");
  const lightboxCaption = document.getElementById("lightbox-caption");
  const lightboxClose = document.getElementById("lightbox-close");

  const closeLightbox = () => {
    if (!lightbox || lightbox.hidden) return;
    lightbox.setAttribute("data-open", "false");
    lightbox.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    setTimeout(() => {
      lightbox.hidden = true;
      lightboxImg.src = "";
    }, prefersReduced ? 0 : 180);
  };

  document.querySelector(".charts")?.addEventListener("click", (e) => {
    const trigger = e.target.closest(".chart-trigger");
    if (!trigger) return;
    e.preventDefault();
    const img = Array.from(trigger.querySelectorAll("img")).find(
      (i) => getComputedStyle(i).display !== "none"
    ) || trigger.querySelector("img");
    if (!img) return;
    const figcap = trigger.closest("figure")?.querySelector("figcaption");
    lightboxImg.src = img.currentSrc || img.src;
    lightboxImg.alt = img.alt || "";
    lightboxCaption.textContent = figcap ? figcap.textContent.trim() : "";
    lightbox.hidden = false;
    requestAnimationFrame(() => lightbox.setAttribute("data-open", "true"));
    lightbox.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  });

  if (lightboxClose) lightboxClose.addEventListener("click", closeLightbox);
  if (lightbox) {
    lightbox.addEventListener("click", (e) => {
      if (e.target === lightbox) closeLightbox();
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && lightbox && !lightbox.hidden) closeLightbox();
  });

  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const initAnimations = () => {
    if (prefersReduced) return;
    if (typeof window.gsap === "undefined" || typeof window.ScrollTrigger === "undefined") return;

    const { gsap, ScrollTrigger } = window;
    gsap.registerPlugin(ScrollTrigger);
    gsap.ticker.fps(60);
    gsap.defaults({ ease: "cubic-bezier(0.28, 0.11, 0.32, 1)", duration: 0.64 });

    gsap.from(".wordmark", { y: 24, opacity: 0, duration: 0.9, delay: 0.05 });
    gsap.from(".badge", { y: 16, opacity: 0, duration: 0.7, delay: 0.18 });
    gsap.from(".thesis", { y: 20, opacity: 0, duration: 0.8, delay: 0.3 });

    document.querySelectorAll("main > .section").forEach((section) => {
      const children = section.querySelectorAll(":scope > *");
      gsap.from(children, {
        y: 28,
        opacity: 0,
        stagger: 0.08,
        duration: 0.64,
        scrollTrigger: {
          trigger: section,
          start: "top 85%",
          toggleActions: "play none none none",
        },
      });
    });

    window.addEventListener("resize", () => ScrollTrigger.refresh(), { passive: true });
  };

  if (typeof window.gsap === "undefined") {
    window.addEventListener("load", initAnimations, { once: true });
  } else {
    initAnimations();
  }

  const dataEl = document.getElementById("instances-data");
  if (!dataEl) return;
  const instances = JSON.parse(dataEl.textContent || "[]");
  if (!instances.length) return;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);

  const searchInput = document.getElementById("ak-search");
  const filterSelect = document.getElementById("ak-filter");
  const paginationEl = document.getElementById("ak-pagination");

  let currentPage = 1;
  const pageSize = 10;
  let sortField = "instance_id";
  let sortDir = "asc";
  let expandedId = null;

  function getFiltered() {
    const search = (searchInput?.value || "").toLowerCase();
    const domain = filterSelect?.value || "";
    return instances.filter((inst) => {
      if (domain && inst.domain !== domain) return false;
      if (search && inst.instance_id.toLowerCase().indexOf(search) === -1 && inst.task.toLowerCase().indexOf(search) === -1) return false;
      return true;
    });
  }

  function getSorted(data) {
    const sorted = data.slice();
    sorted.sort((a, b) => {
      let va, vb;
      switch (sortField) {
        case "instance_id": va = a.instance_id; vb = b.instance_id; break;
        case "domain": va = a.domain; vb = b.domain; break;
        case "task": va = a.task; vb = b.task; break;
        case "nova": va = a.runs["Qwen3 VL"]?.correct ? 1 : 0; vb = b.runs["Qwen3 VL"]?.correct ? 1 : 0; break;
        case "kimi": va = a.runs["Kimi K2.5"]?.correct ? 1 : 0; vb = b.runs["Kimi K2.5"]?.correct ? 1 : 0; break;
        default: va = a.instance_id; vb = b.instance_id;
      }
      if (va < vb) return sortDir === "asc" ? -1 : 1;
      if (va > vb) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return sorted;
  }

  function renderDetailRow(inst) {
    const item = (k, v) => '<div class="detail-row-item"><span class="detail-key">' + esc(k) + '</span><span class="detail-val">' + v + '</span></div>';

    function modelBlock(title, m, cls) {
      if (!m) return '<div class="detail-block"><div class="detail-block-title ' + cls + '">' + esc(title) + '</div>' + item("Status", "No run") + '</div>';
      const passLabel = m.correct ? '<b style="color:var(--pass)">PASS</b>' : '<b style="color:var(--fail)">FAIL</b>';
      return '<div class="detail-block"><div class="detail-block-title ' + cls + '">' + esc(title) + '</div>'
        + item("Result", passLabel)
        + item("Model Answer", esc(m.model_answer))
        + item("Tool Calls", String(m.tool_calls))
        + item("Cost", "$" + Number(m.cost).toFixed(4))
        + item("V-axis", m.v_score != null ? Number(m.v_score).toFixed(3) : "\u2014")
        + item("S-axis", m.s_score != null ? Number(m.s_score).toFixed(3) : "\u2014")
        + item("PQS", Number(m.pqs).toFixed(3))
        + '</div>';
    }

    return '<tr class="detail-row" data-detail-for="' + esc(inst.instance_id) + '"><td colspan="7"><div class="detail-content"><div class="detail-grid">'
      + '<div class="detail-block"><div class="detail-block-title">Instance</div>'
        + item("ID", esc(inst.instance_id))
        + item("Domain", esc(inst.domain))
        + item("Task", esc(inst.task))
        + item("Golden Answer", '<b>' + esc(inst.golden_answer) + '</b>')
        + (inst.image_url ? '<div style="margin-top:12px;"><button type="button" class="view-image-btn" data-img="' + esc(inst.image_url) + '" data-alt="' + esc(inst.task) + '" style="display:inline-flex;align-items:center;gap:6px;padding:8px 14px;font-family:var(--font-mono);font-size:var(--s-ui);letter-spacing:0.04em;border:1px solid var(--accent);color:var(--accent);background:transparent;cursor:pointer;transition:background 0.15s,color 0.15s;">View Image</button></div>' : '')
      + '</div>'
      + modelBlock("Qwen3 VL", inst.runs["Qwen3 VL"], "tok-nova")
      + modelBlock("Kimi K2.5", inst.runs["Kimi K2.5"], "tok-kimi")
      + '</div></div></td></tr>';
  }

  function renderDataset() {
    const tb = document.getElementById("matrix-tbody");
    if (!tb) return;
    const filtered = getFiltered();
    const sorted = getSorted(filtered);
    const totalPages = Math.ceil(sorted.length / pageSize) || 1;
    if (currentPage > totalPages) currentPage = totalPages;
    const start = (currentPage - 1) * pageSize;
    const page = sorted.slice(start, start + pageSize);

    let html = "";
    page.forEach((inst, i) => {
      const kimi = inst.runs["Kimi K2.5"];
      const nova = inst.runs["Qwen3 VL"];
      const isExpanded = inst.instance_id === expandedId;
      const tileStyle = 'display:inline-flex;align-items:center;justify-content:center;min-width:56px;min-height:28px;font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;padding:4px 8px;border:1px solid currentColor;';

      html += '<tr class="matrix-row' + (isExpanded ? ' row-expanded' : '') + '" data-id="' + esc(inst.instance_id) + '">'
        + '<td class="matrix-id"><span class="num mono">#' + String(start + i + 1).padStart(2, "0") + '</span> ' + esc(inst.instance_id) + '</td>'
        + '<td class="matrix-meta">' + esc(inst.domain) + '</td>'
        + '<td>' + esc(inst.task) + '</td>'
        + '<td style="text-align:center;"><b>' + esc(inst.golden_answer) + '</b></td>'
        + '<td class="matrix-cell"><span class="tile-btn ' + (nova?.correct ? 'tile-btn--pass' : 'tile-btn--fail') + '" style="' + tileStyle + '">' + (nova?.correct ? 'PASS' : 'FAIL') + '</span></td>'
        + '<td class="matrix-cell"><span class="tile-btn ' + (kimi?.correct ? 'tile-btn--pass' : 'tile-btn--fail') + '" style="' + tileStyle + '">' + (kimi?.correct ? 'PASS' : 'FAIL') + '</span></td>'
        + '<td class="matrix-cell"><span class="expand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></span></td>'
        + '</tr>';
      if (isExpanded) html += renderDetailRow(inst);
    });

    if (!page.length) {
      html = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--muted);">No instances match your filters</td></tr>';
    }

    tb.innerHTML = html;
    renderPagination(totalPages);
  }

  function renderPagination(totalPages) {
    if (!paginationEl) return;
    if (totalPages <= 1) { paginationEl.innerHTML = ""; return; }
    let html = "";
    html += '<button class="vt-btn" data-page="prev" ' + (currentPage === 1 ? "disabled" : "") + ' style="padding:6px 12px;min-height:auto;">\u2190</button>';
    for (let i = 1; i <= totalPages; i++) {
      html += '<button class="vt-btn ' + (i === currentPage ? "is-active" : "") + '" data-page="' + i + '" style="padding:6px 12px;min-height:auto;">' + i + '</button>';
    }
    html += '<button class="vt-btn" data-page="next" ' + (currentPage === totalPages ? "disabled" : "") + ' style="padding:6px 12px;min-height:auto;">\u2192</button>';
    paginationEl.innerHTML = html;

    paginationEl.querySelectorAll("[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const p = btn.dataset.page;
        if (p === "prev") currentPage = Math.max(1, currentPage - 1);
        else if (p === "next") currentPage = Math.min(totalPages, currentPage + 1);
        else currentPage = parseInt(p);
        renderDataset();
      });
    });
  }

  document.querySelectorAll("[data-sort]").forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const field = th.dataset.sort;
      if (sortField === field) sortDir = sortDir === "asc" ? "desc" : "asc";
      else { sortField = field; sortDir = "asc"; }
      currentPage = 1;
      renderDataset();
    });
  });

  if (searchInput) searchInput.addEventListener("input", () => { currentPage = 1; renderDataset(); });
  if (filterSelect) filterSelect.addEventListener("change", () => { currentPage = 1; renderDataset(); });

  renderDataset();

  document.addEventListener("click", (e) => {
    const imgBtn = e.target.closest(".view-image-btn");
    if (imgBtn) {
      e.stopPropagation();
      const src = imgBtn.dataset.img;
      const alt = imgBtn.dataset.alt || "";
      if (!src || !lightbox) return;
      lightboxImg.src = src;
      lightboxImg.alt = alt;
      lightboxCaption.textContent = alt;
      lightbox.hidden = false;
      requestAnimationFrame(() => lightbox.setAttribute("data-open", "true"));
      lightbox.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      return;
    }
    const row = e.target.closest("#matrix tr[data-id]");
    if (!row) return;
    if (e.target.closest("a")) return;
    const id = row.dataset.id;
    expandedId = (expandedId === id) ? null : id;
    renderDataset();
  });
})();
