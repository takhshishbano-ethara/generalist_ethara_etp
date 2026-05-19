(() => {
  const MAX_DEPTH = 10;
  const MAX_TEXT_LEN = 500;
  const SECTION_TAGS = new Set(['section', 'header', 'footer', 'main', 'nav', 'aside', 'article']);
  const MEDIA_TAGS = new Set(['img', 'picture', 'video', 'iframe', 'canvas', 'svg']);
  const INTERACTIVE_TAGS = new Set(['a', 'button', 'input', 'textarea', 'select']);
  const HEADING_TAGS = new Set(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']);

  function getComponentType(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    const cls = (el.className?.toString() || '').toLowerCase();
    const id = (el.id || '').toLowerCase();

    if (tag === 'nav' || role === 'navigation' || (cls.includes('nav') && !cls.includes('canvas'))) return 'NavBar';
    if (tag === 'header' || role === 'banner') return 'Header';
    if (tag === 'footer' || role === 'contentinfo') return 'Footer';
    if (tag === 'main' || role === 'main') return 'Main';
    if (cls.includes('hero') || id.includes('hero')) return 'Hero';
    if (tag === 'form' || role === 'form') return 'Form';
    if (cls.includes('card') || cls.includes('tile')) return 'Card';
    if (cls.includes('grid') && el.children.length > 2) return 'CardGrid';
    if (cls.includes('carousel') || cls.includes('slider') || cls.includes('swiper')) return 'Carousel';
    if (cls.includes('modal') || cls.includes('dialog') || role === 'dialog') return 'Modal';
    if (cls.includes('lightbox')) return 'Lightbox';
    if (cls.includes('testimonial') || cls.includes('review')) return 'Testimonial';
    if (cls.includes('faq') || cls.includes('accordion')) return 'FAQ';
    if (cls.includes('pricing')) return 'Pricing';
    if (cls.includes('cta') || cls.includes('banner')) return 'CTA';
    if (cls.includes('sidebar')) return 'Sidebar';
    if (cls.includes('search')) return 'Search';
    if (cls.includes('menu') || role === 'menu') return 'Menu';
    if (cls.includes('dropdown')) return 'Dropdown';
    if (cls.includes('tab') || role === 'tablist') return 'Tabs';
    if (cls.includes('logo')) return 'Logo';
    if (tag === 'section' || role === 'region') return 'Section';
    if (tag === 'article') return 'Article';
    if (tag === 'aside' || role === 'complementary') return 'Sidebar';
    if (HEADING_TAGS.has(tag)) return 'Heading';
    if (tag === 'p') return 'Text';
    if (tag === 'ul' || tag === 'ol') return 'List';
    if (tag === 'table') return 'Table';
    if (tag === 'blockquote') return 'Quote';
    if (tag === 'figure') return 'Figure';
    if (MEDIA_TAGS.has(tag)) return 'Media';
    if (INTERACTIVE_TAGS.has(tag)) return 'Interactive';
    if (tag === 'div' || tag === 'span') return 'Container';
    return 'Element';
  }

  function isVisible(el) {
    if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) < 0.01) return false;
    return true;
  }

  function getDirectText(el) {
    let text = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) {
        text += node.textContent;
      }
    }
    return text.trim();
  }

  function buildSelector(el) {
    const tag = el.tagName.toLowerCase();
    if (el.id) return `${tag}#${el.id}`;
    const classes = Array.from(el.classList || []).filter(c => !c.startsWith('w-') || c === 'w-nav').slice(0, 3);
    if (classes.length) return `${tag}.${classes.join('.')}`;
    return tag;
  }

  function getRelevantDataAttrs(el) {
    const attrs = {};
    for (const attr of el.attributes) {
      if (attr.name === 'data-w-id' || attr.name === 'data-animation-type' ||
          attr.name === 'data-wf-collection' || attr.name === 'data-src' ||
          attr.name === 'data-poster' || attr.name === 'data-video-urls' ||
          attr.name.startsWith('data-ix')) {
        attrs[attr.name] = attr.value.substring(0, 100);
      }
    }
    return Object.keys(attrs).length ? attrs : null;
  }

  function walkTree(el, depth) {
    if (depth > MAX_DEPTH) return null;
    if (depth > 0 && !isVisible(el)) return null;

    const tag = el.tagName.toLowerCase();
    if (['script', 'style', 'noscript', 'link', 'meta', 'br', 'hr'].includes(tag)) return null;

    const rect = el.getBoundingClientRect();
    if (depth > 1 && rect.width < 5 && rect.height < 5) return null;

    const style = getComputedStyle(el);
    const componentType = getComponentType(el);
    const directText = getDirectText(el);

    const node = {
      tag,
      componentType,
      selector: buildSelector(el),
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      children: [],
    };

    if (el.id) node.id = el.id;
    const classes = Array.from(el.classList || []).slice(0, 10).join(' ');
    if (classes) node.className = classes;

    const role = el.getAttribute('role');
    if (role) node.role = role;
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) node.ariaLabel = ariaLabel;

    node.styles = {
      display: style.display,
      position: style.position,
    };
    if (style.display === 'flex' || style.display === 'inline-flex') {
      node.styles.flexDirection = style.flexDirection;
      node.styles.justifyContent = style.justifyContent;
      node.styles.alignItems = style.alignItems;
      if (style.gap && style.gap !== 'normal') node.styles.gap = style.gap;
    }
    if (style.display === 'grid' || style.display === 'inline-grid') {
      node.styles.gridTemplateColumns = style.gridTemplateColumns;
      if (style.gap && style.gap !== 'normal') node.styles.gap = style.gap;
    }
    const zi = parseInt(style.zIndex);
    if (zi && zi !== 0) node.styles.zIndex = zi;
    if (style.overflow !== 'visible') node.styles.overflow = style.overflow;

    if (directText && directText.length > 0) {
      node.text = directText.substring(0, MAX_TEXT_LEN);
    }

    if (tag === 'a') {
      node.href = el.href || null;
    }
    if (tag === 'img' || tag === 'video') {
      node.src = el.src || el.currentSrc || null;
      if (el.srcset) node.srcset = el.srcset.substring(0, 200);
      if (el.alt) node.alt = el.alt;
      if (tag === 'img') {
        node.naturalWidth = el.naturalWidth || null;
        node.naturalHeight = el.naturalHeight || null;
      }
    }
    if (tag === 'iframe') {
      node.src = el.src || null;
    }
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      node.inputType = el.type || null;
      node.name = el.name || null;
      node.placeholder = el.placeholder || null;
    }

    const dataAttrs = getRelevantDataAttrs(el);
    if (dataAttrs) node.dataAttributes = dataAttrs;

    // Collapse repeated similar siblings
    const children = el.children;
    let repeatedCount = 0;
    let lastChildSig = null;

    for (let i = 0; i < children.length; i++) {
      const child = walkTree(children[i], depth + 1);
      if (!child) continue;

      const sig = child.tag + '|' + child.componentType + '|' + (child.className || '');
      if (sig === lastChildSig && repeatedCount > 3) {
        repeatedCount++;
        continue;
      }
      if (sig !== lastChildSig && repeatedCount > 3) {
        node.children.push({ _collapsed: true, count: repeatedCount, representative: sig });
        repeatedCount = 0;
      }
      lastChildSig = sig;
      repeatedCount = 1;
      node.children.push(child);
    }
    if (repeatedCount > 3) {
      node.children.push({ _collapsed: true, count: repeatedCount, representative: lastChildSig });
    }

    return node;
  }

  // Build content map — group content by section
  function buildContentMap() {
    const sectionEls = document.querySelectorAll(
      'section, header, footer, main, nav, aside, article, ' +
      '[class*="section"], [class*="Section"], [role="banner"], [role="main"], ' +
      '[role="contentinfo"], [role="navigation"], [role="region"]'
    );

    // Fallback: direct children of body if no sections found
    const targets = sectionEls.length > 2
      ? Array.from(sectionEls).filter(el => isVisible(el))
      : Array.from(document.body.children).filter(el => {
          if (!isVisible(el)) return false;
          const tag = el.tagName.toLowerCase();
          return tag !== 'script' && tag !== 'style' && tag !== 'link';
        });

    const sections = {};
    targets.forEach((el, i) => {
      const rect = el.getBoundingClientRect();
      if (rect.width < 100 || rect.height < 20) return;

      const componentType = getComponentType(el);
      const key = `section_${i}_${componentType.toLowerCase()}`;

      const headings = [];
      el.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach(h => {
        const text = h.textContent?.trim();
        if (text) {
          const hs = getComputedStyle(h);
          headings.push({
            tag: h.tagName.toLowerCase(),
            text: text.substring(0, 200),
            style: { fontSize: hs.fontSize, fontWeight: hs.fontWeight, color: hs.color },
          });
        }
      });

      const bodyText = [];
      el.querySelectorAll('p, li, blockquote, figcaption, [class*="desc"], [class*="text"]').forEach(p => {
        const text = p.textContent?.trim();
        if (text && text.length > 5 && !p.closest('nav')) {
          bodyText.push({
            tag: p.tagName.toLowerCase(),
            text: text.substring(0, MAX_TEXT_LEN),
          });
        }
      });

      const links = [];
      el.querySelectorAll('a').forEach(a => {
        const text = a.textContent?.trim();
        const href = a.href;
        if (!href || href === 'javascript:void(0)') return;
        const cls = (a.className?.toString() || '').toLowerCase();
        const role = (cls.includes('btn') || cls.includes('button') || cls.includes('cta') || a.getAttribute('role') === 'button') ? 'cta' : 'link';
        links.push({ text: (text || '').substring(0, 80), href, role });
      });

      const media = [];
      el.querySelectorAll('img, video, svg, picture, iframe').forEach(m => {
        const tag = m.tagName.toLowerCase();
        const mr = m.getBoundingClientRect();
        if (mr.width < 10 || mr.height < 10) return;

        const entry = { tag, width: Math.round(mr.width), height: Math.round(mr.height) };
        if (tag === 'img') {
          entry.src = m.src || m.currentSrc || null;
          entry.alt = m.alt || null;
          entry.naturalWidth = m.naturalWidth || null;
          entry.naturalHeight = m.naturalHeight || null;
          const cls = (m.className?.toString() || '').toLowerCase();
          const parentCls = (m.parentElement?.className?.toString() || '').toLowerCase();
          if (cls.includes('logo') || parentCls.includes('logo')) entry.role = 'logo';
          else if (cls.includes('icon') || mr.width < 48) entry.role = 'icon';
          else if (cls.includes('avatar')) entry.role = 'avatar';
          else if (cls.includes('hero') || parentCls.includes('hero') || (mr.width > 600 && mr.height > 300)) entry.role = 'hero-image';
          else if (cls.includes('thumb') || cls.includes('card')) entry.role = 'thumbnail';
          else entry.role = 'content-image';
        }
        if (tag === 'video') {
          entry.src = m.src || m.querySelector('source')?.src || null;
          entry.autoplay = m.autoplay;
          entry.loop = m.loop;
          entry.muted = m.muted;
          entry.role = 'video';
        }
        if (tag === 'svg') {
          entry.role = mr.width < 48 ? 'icon' : 'illustration';
        }
        if (tag === 'iframe') {
          entry.src = m.src || null;
          entry.role = 'embed';
        }
        media.push(entry);
      });

      const forms = [];
      el.querySelectorAll('form').forEach(f => {
        const fields = [];
        f.querySelectorAll('input, textarea, select').forEach(inp => {
          fields.push({
            tag: inp.tagName.toLowerCase(),
            type: inp.type || null,
            name: inp.name || null,
            placeholder: inp.placeholder || null,
            required: inp.required || false,
          });
        });
        forms.push({
          action: f.action || null,
          method: f.method || 'GET',
          fields,
        });
      });

      sections[key] = {
        sectionLabel: componentType,
        sectionSelector: buildSelector(el),
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        headings,
        bodyText: bodyText.slice(0, 20),
        links: links.slice(0, 20),
        media: media.slice(0, 20),
        forms,
      };
    });

    return sections;
  }

  // Build site metadata
  function buildSiteMetadata() {
    const meta = {};

    meta.title = document.title || null;
    meta.lang = document.documentElement.lang || null;
    meta.charset = document.characterSet || null;

    // Meta tags
    const metaTags = {};
    document.querySelectorAll('meta[name], meta[property]').forEach(m => {
      const key = m.getAttribute('property') || m.getAttribute('name');
      const content = m.getAttribute('content');
      if (key && content) metaTags[key] = content;
    });
    meta.metaTags = metaTags;

    // Open Graph
    meta.og = {};
    for (const [key, val] of Object.entries(metaTags)) {
      if (key.startsWith('og:')) meta.og[key.replace('og:', '')] = val;
    }

    // Favicon
    const favicon = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]');
    meta.favicon = favicon ? favicon.href : null;

    // Canonical
    const canonical = document.querySelector('link[rel="canonical"]');
    meta.canonical = canonical ? canonical.href : null;

    // Theme color
    const theme = document.querySelector('meta[name="theme-color"]');
    meta.themeColor = theme ? theme.content : null;

    // Navigation structure
    const navLinks = [];
    const navEls = document.querySelectorAll('nav a, header a, [role="navigation"] a');
    navEls.forEach(a => {
      const text = a.textContent?.trim();
      const href = a.href;
      if (!href || !text) return;
      const isExternal = href && !href.includes(window.location.hostname);
      navLinks.push({ text: text.substring(0, 60), href, isExternal });
    });
    meta.navigation = navLinks;

    // Preloaded resources
    const preloads = [];
    document.querySelectorAll('link[rel="preload"], link[rel="preconnect"], link[rel="dns-prefetch"]').forEach(link => {
      preloads.push({ rel: link.rel, href: link.href, as: link.getAttribute('as') || null });
    });
    meta.preloads = preloads;

    return meta;
  }

  const tree = walkTree(document.body, 0);
  const contentMap = buildContentMap();
  const siteMetadata = buildSiteMetadata();

  return { tree, contentMap, siteMetadata };
})();
