(() => {
  const result = {
    site_name: '',
    logo_candidates: [],
    favicons: [],
    theme_color: null,
  };

  const pageTitle = document.title || '';
  const domain = location.hostname.replace(/^www\./, '');
  const domainBase = domain.split('.')[0];

  // --- SITE NAME ---
  const ogSiteName = document.querySelector('meta[property="og:site_name"]');
  if (ogSiteName && ogSiteName.content) {
    result.site_name = ogSiteName.content.trim();
  }

  if (!result.site_name) {
    const jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of jsonLdScripts) {
      try {
        const data = JSON.parse(s.textContent);
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
          if (item['@type'] === 'Organization' && item.name) {
            result.site_name = item.name;
            break;
          }
          if (item['@type'] === 'WebSite' && item.name) {
            result.site_name = item.name;
            break;
          }
        }
      } catch (e) {}
      if (result.site_name) break;
    }
  }

  if (!result.site_name && pageTitle) {
    const separators = [' | ', ' - ', ' — ', ' – ', ' :: ', ' // '];
    for (const sep of separators) {
      if (pageTitle.includes(sep)) {
        const parts = pageTitle.split(sep);
        result.site_name = parts[0].trim().length > 1 ? parts[0].trim() : parts[parts.length - 1].trim();
        break;
      }
    }
    if (!result.site_name) {
      result.site_name = pageTitle.trim();
    }
  }

  if (!result.site_name) {
    result.site_name = domainBase.charAt(0).toUpperCase() + domainBase.slice(1);
  }

  // --- LOGO CANDIDATES ---
  const candidateSelectors = [
    'header img', 'nav img', '[class*="logo"] img', '[id*="logo"] img',
    'a[href="/"] img', 'a[href="' + location.origin + '"] img',
    'header svg', 'nav svg', '[class*="logo"] svg', '[id*="logo"] svg',
    'header a img', 'nav a img',
    'img[class*="logo"]', 'img[id*="logo"]', 'img[alt*="logo"]',
    'svg[class*="logo"]', 'svg[id*="logo"]',
    '[class*="brand"] img', '[class*="Brand"] img',
    '[class*="site-logo"]', '[class*="siteLogo"]',
  ];

  const seen = new Set();
  const candidates = [];

  for (const selector of candidateSelectors) {
    try {
      const els = document.querySelectorAll(selector);
      for (const el of els) {
        const isSvg = el.tagName.toLowerCase() === 'svg';
        const src = isSvg ? 'inline-svg' : (el.src || el.currentSrc || '');
        if (!src || seen.has(src)) continue;
        seen.add(src);

        let score = 0;
        const rect = el.getBoundingClientRect();
        const elStr = (el.className?.toString() || '') + ' ' + (el.id || '') + ' ' + (el.alt || '');
        const parentStr = (el.parentElement?.className?.toString() || '') + ' ' + (el.parentElement?.id || '');
        const allStr = (elStr + ' ' + parentStr).toLowerCase();

        // Position signal: in header/nav or within first 200px
        const inHeader = !!el.closest('header, nav, [role="navigation"], [role="banner"]');
        if (inHeader) score += 3;
        else if (rect.top < 200) score += 1;

        // Class/ID signal
        if (/logo|brand|site-logo|siteLogo/.test(allStr)) score += 3;

        // Alt text matches site title or domain
        const alt = (el.alt || '').toLowerCase();
        if (alt && (alt.includes(domainBase) || alt.includes(result.site_name.toLowerCase()))) score += 2;

        // Dimension signal: logo-typical
        const w = rect.width;
        const h = rect.height;
        if (w >= 40 && w <= 400 && h >= 15 && h <= 150) score += 2;
        else if (w > 0 && h > 0) score += 0;
        else score -= 1;

        // Link signal: wrapped in homepage link
        const parentLink = el.closest('a');
        if (parentLink) {
          const href = parentLink.getAttribute('href') || '';
          if (href === '/' || href === location.origin || href === location.origin + '/') score += 1;
        }

        // Domain match in filename
        if (!isSvg && src.toLowerCase().includes(domainBase)) score += 2;

        candidates.push({
          src: src,
          type: isSvg ? 'svg' : 'img',
          alt: el.alt || null,
          width: Math.round(w),
          height: Math.round(h),
          score: score,
          inHeader: inHeader,
        });
      }
    } catch (e) {}
  }

  candidates.sort((a, b) => b.score - a.score);
  result.logo_candidates = candidates.slice(0, 5);

  // --- FAVICONS ---
  const faviconSelectors = [
    'link[rel="icon"]',
    'link[rel="shortcut icon"]',
    'link[rel="apple-touch-icon"]',
    'link[rel="apple-touch-icon-precomposed"]',
    'link[rel="mask-icon"]',
  ];

  for (const sel of faviconSelectors) {
    try {
      const els = document.querySelectorAll(sel);
      for (const el of els) {
        result.favicons.push({
          href: el.href || '',
          rel: el.rel || '',
          type: el.type || null,
          sizes: el.getAttribute('sizes') || null,
        });
      }
    } catch (e) {}
  }

  // Fallback: /favicon.ico
  if (result.favicons.length === 0) {
    result.favicons.push({
      href: location.origin + '/favicon.ico',
      rel: 'icon',
      type: null,
      sizes: null,
    });
  }

  // --- THEME COLOR ---
  const themeColor = document.querySelector('meta[name="theme-color"]');
  if (themeColor) {
    result.theme_color = themeColor.content;
  }

  return result;
})();
