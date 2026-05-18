(() => {
  const VIEWPORT_W = window.innerWidth;
  const VIEWPORT_H = window.innerHeight;

  function getSemanticRole(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    const cls = (el.className?.toString() || '').toLowerCase();

    if (tag === 'nav' || role === 'navigation' || cls.includes('nav')) return 'nav';
    if (tag === 'header' || role === 'banner') return 'header';
    if (tag === 'footer' || role === 'contentinfo') return 'footer';
    if (tag === 'main' || role === 'main') return 'main';
    if (tag === 'aside' || role === 'complementary') return 'sidebar';
    if (tag === 'section' || role === 'region') return 'section';
    if (tag === 'article') return 'article';
    if (['h1', 'h2', 'h3', 'h4', 'h5', 'h6'].includes(tag)) return 'heading';
    if (tag === 'p') return 'text';
    if (tag === 'img' || tag === 'picture' || tag === 'svg' || tag === 'video') return 'media';
    if (tag === 'canvas') return 'canvas';
    if (tag === 'button' || tag === 'a' || role === 'button') return 'interactive';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return 'form-field';
    if (tag === 'form') return 'form';
    if (tag === 'ul' || tag === 'ol') return 'list';
    if (tag === 'table') return 'table';
    if (tag === 'figure') return 'figure';
    if (tag === 'blockquote') return 'quote';

    if (cls.includes('card') || cls.includes('tile')) return 'card';
    if (cls.includes('hero')) return 'hero';
    if (cls.includes('grid')) return 'grid';
    if (cls.includes('carousel') || cls.includes('slider') || cls.includes('swiper')) return 'carousel';
    if (cls.includes('modal') || cls.includes('dialog') || role === 'dialog') return 'modal';
    if (cls.includes('menu') || role === 'menu') return 'menu';
    if (cls.includes('dropdown')) return 'dropdown';
    if (cls.includes('sidebar')) return 'sidebar';
    if (cls.includes('banner') || cls.includes('cta')) return 'banner';
    if (cls.includes('search')) return 'search';
    if (cls.includes('logo')) return 'logo';
    if (cls.includes('icon')) return 'icon';
    if (cls.includes('btn') || cls.includes('button')) return 'interactive';

    return 'container';
  }

  function isVisible(el) {
    if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) < 0.01) return false;
    return true;
  }

  function shouldCapture(el, depth) {
    const tag = el.tagName.toLowerCase();
    // Always capture semantic elements
    if (['nav', 'header', 'footer', 'main', 'section', 'article', 'aside', 'figure'].includes(tag)) return true;
    if (['h1', 'h2', 'h3', 'h4', 'h5', 'h6'].includes(tag)) return true;
    if (['img', 'picture', 'video', 'canvas', 'svg'].includes(tag)) return true;
    if (['button', 'input', 'textarea', 'select', 'form'].includes(tag)) return true;
    if (el.getAttribute('role')) return true;

    const cls = (el.className?.toString() || '').toLowerCase();
    if (cls.includes('card') || cls.includes('hero') || cls.includes('grid') ||
        cls.includes('carousel') || cls.includes('slider') || cls.includes('modal') ||
        cls.includes('sidebar') || cls.includes('banner') || cls.includes('cta') ||
        cls.includes('search') || cls.includes('logo') || cls.includes('icon') ||
        cls.includes('menu') || cls.includes('btn') || cls.includes('button') ||
        cls.includes('section') || cls.includes('container') || cls.includes('wrapper')) return true;

    // For deeper elements, only capture if they have significant size
    if (depth > 2) {
      const rect = el.getBoundingClientRect();
      if (rect.width < 50 || rect.height < 20) return false;
    }

    // Capture divs at depth 1-3 that are direct children of body or main
    if (depth <= 3 && tag === 'div') return true;

    return false;
  }

  function walkDOM(el, depth, maxDepth) {
    if (depth > maxDepth) return null;
    // Always process body (depth 0) — it might have zero computed size on canvas sites
    if (depth > 0 && !isVisible(el)) return null;
    if (depth > 0 && !shouldCapture(el, depth)) return null;

    const rect = el.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 5) return null;

    const role = getSemanticRole(el);
    const style = getComputedStyle(el);

    const node = {
      tag: el.tagName.toLowerCase(),
      role: role,
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
      display: style.display,
      position: style.position,
      text: null,
      children: [],
    };

    // Capture text for headings, buttons, links
    if (['heading', 'interactive', 'text'].includes(role)) {
      const text = el.textContent?.trim();
      if (text && text.length < 100) node.text = text.substring(0, 60);
    }

    // Capture image src for media
    if (role === 'media' && el.tagName.toLowerCase() === 'img') {
      node.src = el.src?.substring(0, 100);
    }

    // Walk children
    const children = el.children;
    for (let i = 0; i < children.length; i++) {
      const child = walkDOM(children[i], depth + 1, maxDepth);
      if (child) {
        node.children.push(child);
      }
    }

    return node;
  }

  // Build the layout tree from body
  const tree = walkDOM(document.body, 0, 6);

  // Also capture section boundaries for the section map
  const sections = [];
  document.querySelectorAll('section, [class*="section"], [class*="Section"], header, footer, main, nav, canvas, [role="banner"], [role="main"], [role="contentinfo"], [role="navigation"]').forEach(el => {
    if (!isVisible(el)) return;
    const rect = el.getBoundingClientRect();
    if (rect.width < 100 || rect.height < 30) return;
    const heading = el.querySelector('h1, h2, h3');
    sections.push({
      tag: el.tagName.toLowerCase(),
      role: getSemanticRole(el),
      className: (el.className?.toString() || '').split(' ')[0] || null,
      id: el.id || null,
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
      label: heading?.textContent?.trim().substring(0, 50) || el.id || (el.className?.toString() || '').split(' ')[0] || el.tagName.toLowerCase(),
    });
  });

  // Total page height
  const pageHeight = Math.max(
    document.body.scrollHeight,
    document.documentElement.scrollHeight,
    document.body.offsetHeight,
    document.documentElement.offsetHeight,
  );

  return {
    viewport: { width: VIEWPORT_W, height: VIEWPORT_H },
    pageHeight: pageHeight,
    tree: tree,
    sections: sections,
  };
})();
