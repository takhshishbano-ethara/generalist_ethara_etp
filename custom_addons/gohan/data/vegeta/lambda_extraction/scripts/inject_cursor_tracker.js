// Custom Cursor Behavior Tracker
// Injected after page load via page.evaluate(). Detects custom cursors and records their behavior.
(() => {
  const result = {
    hasCustomCursor: false,
    cursorHidden: false,
    cursorElements: [],       // [{selector, tagName, classes, styles, dimensions}]
    hoverStates: [],          // [{target, dataCursor}]
    followerPattern: null,    // {elementCount, elements}
    gsapDriven: false,
    blendMode: null,
    trail: false,
    magneticEffects: false,
    error: null,
  };

  try {
    // Check if default cursor is hidden
    const htmlCursor = getComputedStyle(document.documentElement).cursor;
    const bodyCursor = getComputedStyle(document.body).cursor;
    result.cursorHidden = htmlCursor === 'none' || bodyCursor === 'none';

    // Also check stylesheets for cursor:none rules
    if (!result.cursorHidden) {
      try {
        for (const sheet of document.styleSheets) {
          try {
            for (const rule of sheet.cssRules || []) {
              if (rule.selectorText && (rule.selectorText === 'html' || rule.selectorText === 'body' || rule.selectorText === '*')) {
                if (rule.style.cursor === 'none') {
                  result.cursorHidden = true;
                  break;
                }
              }
            }
          } catch(e) {} // CORS stylesheets
        }
      } catch(e) {}
    }

    result.hasCustomCursor = result.cursorHidden;

    // Find cursor elements by common selectors
    const cursorSelectors = [
      '[class*="cursor"]', '[class*="Cursor"]',
      '[data-cursor]', '.cursor', '#cursor',
      '[class*="pointer"]', '[class*="Pointer"]',
      '[class*="mouse"]', '[class*="Mouse"]',
      '.ball', '.dot', '.circle-cursor',
    ];

    const cursorEls = new Set();
    cursorSelectors.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        // Filter out false positives (elements that are too large or zero-size)
        const rect = el.getBoundingClientRect();
        if (rect.width < 200 && rect.height < 200 && rect.width > 0) {
          cursorEls.add(el);
        }
      });
    });

    cursorEls.forEach(el => {
      const computed = getComputedStyle(el);
      const entry = {
        selector: getCssSelector(el),
        tagName: el.tagName,
        classes: Array.from(el.classList),
        id: el.id || null,
        styles: {
          width: computed.width,
          height: computed.height,
          borderRadius: computed.borderRadius,
          background: computed.background,
          backgroundColor: computed.backgroundColor,
          border: computed.border,
          mixBlendMode: computed.mixBlendMode,
          pointerEvents: computed.pointerEvents,
          zIndex: computed.zIndex,
          position: computed.position,
          transform: computed.transform,
          transition: computed.transition,
          opacity: computed.opacity,
          willChange: computed.willChange,
        },
        dimensions: {
          width: el.offsetWidth,
          height: el.offsetHeight,
        },
        children: el.children.length,
        innerText: (el.textContent || '').trim().substring(0, 50),
      };

      if (computed.mixBlendMode && computed.mixBlendMode !== 'normal') {
        result.blendMode = computed.mixBlendMode;
        result.hasCustomCursor = true;
      }

      result.cursorElements.push(entry);
    });

    // Detect leader/follower pattern (multiple cursor elements with different transforms)
    if (result.cursorElements.length >= 2) {
      result.followerPattern = {
        elementCount: result.cursorElements.length,
        elements: result.cursorElements.map(e => ({
          selector: e.selector,
          size: e.dimensions,
          transition: e.styles.transition,
        })),
      };
      result.hasCustomCursor = true;
    }

    // Detect GSAP cursor driving
    if (window.gsap) {
      result.gsapDriven = true;
      try {
        if (window.gsap.quickTo) {
          result.gsapQuickTo = true;
        }
      } catch(e) {}
    }

    // Detect magnetic effects (elements with mousemove listeners that translate)
    const magneticSelectors = [
      '[data-magnetic]', '[class*="magnetic"]', '[class*="Magnetic"]',
      'a[class*="btn"]', 'button[class*="btn"]',
    ];
    magneticSelectors.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        const attr = el.dataset?.magnetic;
        if (attr !== undefined) {
          result.magneticEffects = true;
          result.hasCustomCursor = true;
        }
      });
    });

    // Also check for magnetic via GSAP event listeners on buttons/links
    if (window.gsap && !result.magneticEffects) {
      const btns = document.querySelectorAll('a, button, [role="button"]');
      for (let i = 0; i < Math.min(btns.length, 20); i++) {
        const el = btns[i];
        // Check if element has transforms applied that suggest magnetic behavior
        const transform = getComputedStyle(el).transform;
        if (transform && transform !== 'none' && transform !== 'matrix(1, 0, 0, 1, 0, 0)') {
          // Likely a magnetic or hover-transform element
          const willChange = getComputedStyle(el).willChange;
          if (willChange && willChange.includes('transform')) {
            result.magneticEffects = true;
            result.hasCustomCursor = true;
            break;
          }
        }
      }
    }

    // Record hover state changes on interactive elements
    const interactiveEls = document.querySelectorAll('a, button, [role="button"], [data-cursor], [class*="link"], [class*="btn"]');
    const sampleLimit = Math.min(interactiveEls.length, 10);

    for (let i = 0; i < sampleLimit; i++) {
      const target = interactiveEls[i];
      const targetInfo = {
        tag: target.tagName,
        classes: Array.from(target.classList).slice(0, 5),
        text: (target.textContent || '').trim().substring(0, 30),
        href: target.href || null,
      };

      // Check for data-cursor attributes (common pattern in custom cursor libraries)
      const cursorAttr = target.dataset?.cursor || target.dataset?.cursorText || target.dataset?.cursorColor;
      if (cursorAttr) {
        result.hoverStates.push({
          target: targetInfo,
          dataCursor: {
            cursor: target.dataset.cursor || null,
            cursorText: target.dataset.cursorText || null,
            cursorColor: target.dataset.cursorColor || null,
            cursorSize: target.dataset.cursorSize || null,
          },
        });
        result.hasCustomCursor = true;
      }
    }

    // Check for cursor trail elements
    const trailEls = document.querySelectorAll('[class*="trail"], [class*="Trail"], [class*="particle"], [class*="Particle"]');
    if (trailEls.length > 2) {
      result.trail = true;
      result.trailElementCount = trailEls.length;
      result.hasCustomCursor = true;
    }

  } catch(e) {
    result.error = e.message;
  }

  function getCssSelector(el) {
    if (el.id) return '#' + el.id;
    let selector = el.tagName.toLowerCase();
    if (el.classList.length) {
      selector += '.' + Array.from(el.classList).slice(0, 3).join('.');
    }
    return selector;
  }

  window.__cursorData = result;
  return result;
})();
