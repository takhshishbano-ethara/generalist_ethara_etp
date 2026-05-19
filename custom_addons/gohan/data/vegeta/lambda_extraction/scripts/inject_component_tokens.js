(() => {
  const result = {
    buttons: [],
    inputs: [],
    links: [],
    badges: [],
  };

  const COMPONENT_PATTERNS = {
    buttons: {
      selectors: ['button', '.btn', '[class*="btn-"]', '[class*="Button"]', '[class*="cta"]', '[class*="Cta"]', '[role="button"]'],
      pseudoSelectors: [':hover', ':focus', ':focus-visible', ':active', ':disabled'],
    },
    inputs: {
      selectors: ['input[type="text"]', 'input[type="email"]', 'input[type="password"]', 'input[type="search"]', 'input[type="number"]', 'input[type="tel"]', 'input[type="url"]', 'input:not([type])', 'textarea', 'select', '.input', '[class*="Input"]', '[class*="TextField"]', '[class*="form-control"]'],
      pseudoSelectors: [':hover', ':focus', ':focus-visible', ':disabled', '::placeholder'],
    },
    links: {
      selectors: ['a:not(nav a):not(header a):not(footer a)', 'a[class*="link"]', '[class*="Link"]:not(nav *)', 'a[class*="text"]'],
      pseudoSelectors: [':hover', ':focus', ':active', ':visited'],
    },
    badges: {
      selectors: ['.badge', '[class*="badge"]', '[class*="Badge"]', '.tag', '[class*="tag"]', '[class*="Tag"]', '.chip', '[class*="chip"]', '[class*="Chip"]', '.label:not(label)', '[class*="pill"]', '[class*="Pill"]'],
      pseudoSelectors: [':hover'],
    },
  };

  const EXTRACT_PROPS = [
    'backgroundColor', 'color', 'borderWidth', 'borderStyle', 'borderColor',
    'borderRadius', 'padding', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'fontSize', 'fontWeight', 'fontFamily', 'lineHeight', 'letterSpacing', 'textTransform', 'textDecoration',
    'boxShadow', 'opacity', 'cursor',
    'transitionProperty', 'transitionDuration', 'transitionTimingFunction',
    'outline', 'outlineColor', 'outlineWidth', 'outlineOffset',
  ];

  const DEFAULTS_TO_SKIP = {
    boxShadow: 'none',
    textDecoration: 'none',
    textTransform: 'none',
    opacity: '1',
    outline: '',
    outlineColor: '',
    borderWidth: '0px',
    borderStyle: 'none',
    letterSpacing: 'normal',
  };

  function extractProps(style) {
    const props = {};
    for (const prop of EXTRACT_PROPS) {
      const val = style[prop];
      if (val && val !== '' && val !== 'initial' && val !== 'inherit') {
        if (DEFAULTS_TO_SKIP[prop] && val === DEFAULTS_TO_SKIP[prop]) continue;
        props[prop] = val;
      }
    }
    return props;
  }

  function getSelector(el) {
    const tag = el.tagName.toLowerCase();
    if (el.id) return `${tag}#${el.id}`;
    const classes = Array.from(el.classList).slice(0, 3).join('.');
    if (classes) return `${tag}.${classes}`;
    return tag;
  }

  // --- CSS RULE SCANNING for pseudo-class styles ---
  const pseudoRules = {};

  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        if (!(rule instanceof CSSStyleRule)) continue;
        const sel = rule.selectorText || '';
        const pseudoMatch = sel.match(/^(.+?)(:{1,2}(?:hover|focus|focus-visible|active|disabled|visited|placeholder))$/);
        if (!pseudoMatch) continue;

        const baseSel = pseudoMatch[1].trim();
        const pseudo = pseudoMatch[2];
        const props = {};

        for (const prop of EXTRACT_PROPS) {
          const camelProp = prop;
          const cssVal = rule.style.getPropertyValue(
            camelProp.replace(/([A-Z])/g, '-$1').toLowerCase()
          );
          if (cssVal && cssVal.trim()) {
            props[camelProp] = cssVal.trim();
          }
        }

        if (Object.keys(props).length > 0) {
          if (!pseudoRules[baseSel]) pseudoRules[baseSel] = {};
          const pseudoKey = pseudo.replace(/^:+/, '');
          if (!pseudoRules[baseSel][pseudoKey]) pseudoRules[baseSel][pseudoKey] = {};
          Object.assign(pseudoRules[baseSel][pseudoKey], props);
        }
      }
    } catch (e) { /* cross-origin */ }
  }

  function findPseudoStyles(el) {
    const states = {};
    const tag = el.tagName.toLowerCase();
    const classes = Array.from(el.classList);
    const id = el.id;

    const possibleSelectors = [tag];
    if (id) possibleSelectors.push(`#${id}`, `${tag}#${id}`);
    for (const cls of classes) {
      possibleSelectors.push(`.${cls}`, `${tag}.${cls}`);
    }
    if (classes.length >= 2) {
      possibleSelectors.push(`.${classes.slice(0, 2).join('.')}`);
      possibleSelectors.push(`${tag}.${classes.slice(0, 2).join('.')}`);
    }

    for (const sel of possibleSelectors) {
      if (pseudoRules[sel]) {
        for (const [pseudo, props] of Object.entries(pseudoRules[sel])) {
          if (!states[pseudo]) states[pseudo] = {};
          Object.assign(states[pseudo], props);
        }
      }
    }

    return states;
  }

  // --- EXTRACT COMPONENT TOKENS ---
  const seen = new Set();

  for (const [componentType, config] of Object.entries(COMPONENT_PATTERNS)) {
    const allSelector = config.selectors.join(', ');
    let elements;
    try {
      elements = document.querySelectorAll(allSelector);
    } catch (e) { continue; }

    const variants = new Map();

    for (const el of elements) {
      if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;

      const style = getComputedStyle(el);
      const base = extractProps(style);
      if (Object.keys(base).length < 2) continue;

      const fingerprint = JSON.stringify({
        bg: base.backgroundColor,
        color: base.color,
        fs: base.fontSize,
        br: base.borderRadius,
      });

      if (seen.has(componentType + fingerprint)) continue;
      seen.add(componentType + fingerprint);

      const selector = getSelector(el);
      const states = findPseudoStyles(el);

      const textContent = (el.textContent || '').trim().substring(0, 30);

      const entry = {
        selector: selector,
        sampleText: textContent || null,
        base: base,
        states: states,
      };

      const key = fingerprint;
      if (!variants.has(key)) {
        variants.set(key, entry);
      }

      if (variants.size >= 8) break;
    }

    result[componentType] = Array.from(variants.values());
  }

  return result;
})();
