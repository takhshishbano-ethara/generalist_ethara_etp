(() => {
  const result = {
    colors: {},
    fonts: {},
    type_scale: [],
    spacing: {},
    grid: {},
    css_variables: {},
    gradients: [],
    shadows: [],
    border_radii: [],
    effects: [],
    font_faces: [],
    loaded_fonts: [],
    media_queries: [],
  };

  // --- COLOR EXTRACTION ---
  const colorMap = {};
  const colorProps = [
    'color', 'backgroundColor', 'borderColor', 'borderTopColor',
    'borderRightColor', 'borderBottomColor', 'borderLeftColor',
    'outlineColor', 'textDecorationColor', 'fill', 'stroke',
    'boxShadow', 'caretColor', 'columnRuleColor',
  ];

  function rgbToHex(r, g, b) {
    return '#' + [r, g, b].map(x => {
      const hex = parseInt(x).toString(16);
      return hex.length === 1 ? '0' + hex : hex;
    }).join('').toUpperCase();
  }

  function parseColor(colorStr) {
    if (!colorStr || colorStr === 'transparent' || colorStr === 'none' || colorStr === 'inherit'
        || colorStr === 'initial' || colorStr === 'currentcolor') return null;
    const match = colorStr.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (match) {
      const [, r, g, b] = match;
      if (r === '0' && g === '0' && b === '0' && colorStr.includes('rgba') && colorStr.includes(', 0)')) return null;
      return rgbToHex(r, g, b);
    }
    if (colorStr.startsWith('#')) return colorStr.toUpperCase();
    return null;
  }

  const allElements = document.querySelectorAll('*');
  const maxElements = Math.min(allElements.length, 5000);

  // Track unique gradients, shadows, border-radii, effects
  const gradientSet = new Map();
  const shadowSet = new Map();
  const radiusSet = new Map();
  const effectSet = {};

  for (let i = 0; i < maxElements; i++) {
    const el = allElements[i];
    const styles = getComputedStyle(el);

    // --- Colors ---
    for (const prop of colorProps) {
      const val = styles.getPropertyValue(prop);
      if (val) {
        if (prop === 'boxShadow' && val !== 'none') {
          const shadowColors = val.match(/rgba?\([^)]+\)/g) || [];
          shadowColors.forEach(sc => {
            const hex = parseColor(sc);
            if (hex) {
              colorMap[hex] = colorMap[hex] || { count: 0, usages: new Set() };
              colorMap[hex].count++;
              colorMap[hex].usages.add('boxShadow');
            }
          });
        } else {
          const hex = parseColor(val);
          if (hex) {
            colorMap[hex] = colorMap[hex] || { count: 0, usages: new Set() };
            colorMap[hex].count++;
            colorMap[hex].usages.add(prop);
          }
        }
      }
    }

    // --- Gradients ---
    const bgImage = styles.backgroundImage;
    if (bgImage && bgImage !== 'none' && (bgImage.includes('gradient') || bgImage.includes('linear-') || bgImage.includes('radial-'))) {
      const key = bgImage.substring(0, 200);
      if (!gradientSet.has(key)) {
        gradientSet.set(key, {
          value: bgImage,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
          count: 1,
        });
      } else {
        gradientSet.get(key).count++;
      }
    }

    // --- Box Shadows ---
    const shadow = styles.boxShadow;
    if (shadow && shadow !== 'none') {
      const key = shadow.substring(0, 150);
      if (!shadowSet.has(key)) {
        shadowSet.set(key, {
          value: shadow,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
          count: 1,
        });
      } else {
        shadowSet.get(key).count++;
      }
    }

    // --- Border Radius ---
    const radius = styles.borderRadius;
    if (radius && radius !== '0px') {
      const key = radius;
      if (!radiusSet.has(key)) {
        radiusSet.set(key, { value: radius, count: 1 });
      } else {
        radiusSet.get(key).count++;
      }
    }

    // --- Effects: clip-path, backdrop-filter, mix-blend-mode, filter ---
    const clipPath = styles.clipPath;
    if (clipPath && clipPath !== 'none') {
      effectSet['clip-path'] = effectSet['clip-path'] || [];
      if (effectSet['clip-path'].length < 5) {
        effectSet['clip-path'].push({
          value: clipPath,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
        });
      }
    }

    const backdropFilter = styles.backdropFilter || styles.webkitBackdropFilter;
    if (backdropFilter && backdropFilter !== 'none') {
      effectSet['backdrop-filter'] = effectSet['backdrop-filter'] || [];
      if (effectSet['backdrop-filter'].length < 5) {
        effectSet['backdrop-filter'].push({
          value: backdropFilter,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
        });
      }
    }

    const mixBlend = styles.mixBlendMode;
    if (mixBlend && mixBlend !== 'normal') {
      effectSet['mix-blend-mode'] = effectSet['mix-blend-mode'] || [];
      if (effectSet['mix-blend-mode'].length < 5) {
        effectSet['mix-blend-mode'].push({
          value: mixBlend,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
        });
      }
    }

    const filter = styles.filter;
    if (filter && filter !== 'none') {
      effectSet['filter'] = effectSet['filter'] || [];
      if (effectSet['filter'].length < 5) {
        effectSet['filter'].push({
          value: filter,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
        });
      }
    }

    const aspectRatio = styles.aspectRatio;
    if (aspectRatio && aspectRatio !== 'auto') {
      effectSet['aspect-ratio'] = effectSet['aspect-ratio'] || [];
      if (effectSet['aspect-ratio'].length < 5) {
        effectSet['aspect-ratio'].push({
          value: aspectRatio,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
        });
      }
    }

    const objectFit = styles.objectFit;
    if (objectFit && objectFit !== 'fill') {
      effectSet['object-fit'] = effectSet['object-fit'] || [];
      if (effectSet['object-fit'].length < 5) {
        effectSet['object-fit'].push({
          value: objectFit,
          element: el.tagName.toLowerCase() + (el.className ? '.' + el.className.toString().split(' ')[0] : ''),
        });
      }
    }
  }

  // Convert sets to arrays and sort by frequency
  result.colors = Object.entries(colorMap)
    .map(([hex, data]) => ({
      hex,
      count: data.count,
      usages: Array.from(data.usages),
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 30);

  result.gradients = Array.from(gradientSet.values()).sort((a, b) => b.count - a.count).slice(0, 10);
  result.shadows = Array.from(shadowSet.values()).sort((a, b) => b.count - a.count).slice(0, 10);
  result.border_radii = Array.from(radiusSet.entries())
    .map(([val, data]) => ({ value: val, count: data.count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 10);
  result.effects = effectSet;

  // --- CSS CUSTOM PROPERTIES (ALL scopes, not just :root) ---
  const varsByScope = {};
  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        const selector = rule.selectorText || '';
        if (rule.cssText && rule.cssText.includes('--')) {
          const text = rule.cssText;
          const varMatches = [...text.matchAll(/(--[\w-]+)\s*:\s*([^;]+)/g)];
          for (const m of varMatches) {
            const name = m[1].trim();
            let value = m[2].trim();
            const hex = parseColor(value);
            if (hex) value = hex;
            const scope = selector || ':root';
            if (!varsByScope[scope]) varsByScope[scope] = {};
            varsByScope[scope][name] = value;
          }
        }
        // Recurse into @media and @supports rules
        if (rule.cssRules) {
          for (const innerRule of rule.cssRules) {
            if (innerRule.cssText && innerRule.cssText.includes('--')) {
              const text = innerRule.cssText;
              const varMatches = [...text.matchAll(/(--[\w-]+)\s*:\s*([^;]+)/g)];
              for (const m of varMatches) {
                const name = m[1].trim();
                let value = m[2].trim();
                const hex = parseColor(value);
                if (hex) value = hex;
                const scope = innerRule.selectorText || ':root';
                if (!varsByScope[scope]) varsByScope[scope] = {};
                varsByScope[scope][name] = value;
              }
            }
          }
        }
      }
    } catch (e) { /* cross-origin */ }
  }
  // Flatten: prioritize :root/html vars, then merge others
  result.css_variables = {};
  for (const [scope, vars] of Object.entries(varsByScope)) {
    for (const [name, value] of Object.entries(vars)) {
      if (!result.css_variables[name]) {
        result.css_variables[name] = value;
      }
    }
  }
  result.css_variables_by_scope = varsByScope;

  // --- FONT EXTRACTION ---
  const fontMap = {};
  const fontSizeMap = {};

  for (let i = 0; i < maxElements; i++) {
    const el = allElements[i];
    if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;
    const styles = getComputedStyle(el);
    const fontFamily = styles.fontFamily;
    const fontSize = parseFloat(styles.fontSize);
    const fontWeight = styles.fontWeight;
    const lineHeight = styles.lineHeight;
    const letterSpacing = styles.letterSpacing;
    const textTransform = styles.textTransform;

    if (fontFamily) {
      const primaryFont = fontFamily.split(',')[0].trim().replace(/['"]/g, '');
      fontMap[primaryFont] = fontMap[primaryFont] || { count: 0, weights: new Set(), usages: [] };
      fontMap[primaryFont].count++;
      fontMap[primaryFont].weights.add(fontWeight);
    }

    const tag = el.tagName.toLowerCase();
    if (['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'span', 'a', 'li', 'label', 'blockquote', 'figcaption', 'caption', 'dt', 'dd'].includes(tag) && fontSize > 0) {
      const key = `${Math.round(fontSize)}px`;
      if (!fontSizeMap[key]) {
        fontSizeMap[key] = {
          size_px: Math.round(fontSize),
          line_height: lineHeight === 'normal' ? 'normal' : Math.round(parseFloat(lineHeight)),
          letter_spacing: letterSpacing === 'normal' ? '0' : letterSpacing,
          font_family: fontFamily.split(',')[0].trim().replace(/['"]/g, ''),
          font_weight: fontWeight,
          text_transform: textTransform,
          tags: new Set(),
          count: 0,
        };
      }
      fontSizeMap[key].tags.add(tag);
      fontSizeMap[key].count++;
    }
  }

  result.fonts = Object.entries(fontMap)
    .map(([family, data]) => ({
      family,
      count: data.count,
      weights: Array.from(data.weights).sort(),
    }))
    .sort((a, b) => b.count - a.count);

  result.type_scale = Object.values(fontSizeMap)
    .map(ts => ({ ...ts, tags: Array.from(ts.tags) }))
    .sort((a, b) => b.size_px - a.size_px)
    .slice(0, 15);

  // --- FONT-FACE DECLARATIONS ---
  const fontFaces = [];
  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        if (rule instanceof CSSFontFaceRule) {
          fontFaces.push({
            family: rule.style.fontFamily?.replace(/['"]/g, ''),
            weight: rule.style.fontWeight || 'normal',
            style: rule.style.fontStyle || 'normal',
            src: rule.style.src?.substring(0, 200),
          });
        }
      }
    } catch (e) { /* cross-origin */ }
  }
  result.font_faces = fontFaces;

  // Loaded fonts via document.fonts API
  const loadedFonts = [];
  if (document.fonts) {
    document.fonts.forEach(font => {
      loadedFonts.push({
        family: font.family.replace(/['"]/g, ''),
        weight: font.weight,
        style: font.style,
        status: font.status,
      });
    });
  }
  result.loaded_fonts = loadedFonts;

  // --- MEDIA QUERIES from stylesheets ---
  const mediaQueries = new Map();
  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        if (rule instanceof CSSMediaRule) {
          const mq = rule.conditionText || rule.media?.mediaText || '';
          if (mq) {
            const widthMatch = mq.match(/(?:max|min)-width\s*:\s*(\d+)/);
            if (widthMatch) {
              const key = mq.trim();
              if (!mediaQueries.has(key)) {
                mediaQueries.set(key, { query: key, width: parseInt(widthMatch[1]), ruleCount: rule.cssRules?.length || 0 });
              }
            }
          }
        }
      }
    } catch (e) { /* cross-origin */ }
  }
  result.media_queries = Array.from(mediaQueries.values()).sort((a, b) => b.width - a.width);

  // --- LAYOUT & SPACING ---
  const containers = document.querySelectorAll('main, [class*="container"], [class*="wrapper"], [class*="content"], .container, .wrapper');
  const containerWidths = [];
  containers.forEach(c => {
    const style = getComputedStyle(c);
    const maxW = style.maxWidth;
    if (maxW && maxW !== 'none' && maxW !== '0px') {
      containerWidths.push(parseFloat(maxW));
    }
  });
  result.grid.max_widths = [...new Set(containerWidths)].sort((a, b) => b - a).slice(0, 5);

  const gridLayouts = [];
  document.querySelectorAll('[style*="grid"], [class*="grid"], [class*="Grid"]').forEach(el => {
    const style = getComputedStyle(el);
    if (style.display.includes('grid')) {
      gridLayouts.push({
        columns: style.gridTemplateColumns,
        rows: style.gridTemplateRows,
        gap: style.gap || style.gridGap,
        element: el.tagName + '.' + (el.className?.toString().split(' ')[0] || ''),
      });
    }
  });
  // Also detect flex layouts
  const flexLayouts = [];
  document.querySelectorAll('[class*="flex"], [class*="Flex"], [class*="row"], [class*="Row"]').forEach(el => {
    const style = getComputedStyle(el);
    if (style.display === 'flex' || style.display === 'inline-flex') {
      const children = el.children.length;
      if (children >= 2) {
        flexLayouts.push({
          direction: style.flexDirection,
          wrap: style.flexWrap,
          gap: style.gap,
          children: children,
          element: el.tagName + '.' + (el.className?.toString().split(' ')[0] || ''),
        });
      }
    }
  });
  result.grid.layouts = gridLayouts.slice(0, 10);
  result.grid.flex_layouts = flexLayouts.slice(0, 10);

  const spacingValues = new Set();
  const sampleElements = document.querySelectorAll('section, header, footer, main > *, [class*="section"], [class*="Section"]');
  sampleElements.forEach(el => {
    const style = getComputedStyle(el);
    ['marginTop', 'marginBottom', 'paddingTop', 'paddingBottom', 'paddingLeft', 'paddingRight', 'gap'].forEach(prop => {
      const val = parseFloat(style[prop]);
      if (val > 0 && val < 500) spacingValues.add(Math.round(val));
    });
  });
  result.spacing.values = Array.from(spacingValues).sort((a, b) => a - b);

  const spacingArr = result.spacing.values.filter(v => v >= 4 && v <= 200);
  if (spacingArr.length > 2) {
    function gcd(a, b) { return b === 0 ? a : gcd(b, a % b); }
    let g = spacingArr[0];
    for (let i = 1; i < Math.min(spacingArr.length, 10); i++) {
      g = gcd(g, spacingArr[i]);
    }
    result.spacing.baseline_unit = g;
  }

  // --- SEO METADATA ---
  result.seo = {};
  const ogImage = document.querySelector('meta[property="og:image"]');
  if (ogImage) result.seo.og_image = ogImage.content;
  const ogTitle = document.querySelector('meta[property="og:title"]');
  if (ogTitle) result.seo.og_title = ogTitle.content;
  const ogDesc = document.querySelector('meta[property="og:description"]');
  if (ogDesc) result.seo.og_description = ogDesc.content;
  const metaDesc = document.querySelector('meta[name="description"]');
  if (metaDesc) result.seo.meta_description = metaDesc.content;
  const canonical = document.querySelector('link[rel="canonical"]');
  if (canonical) result.seo.canonical = canonical.href;
  const favicon = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]');
  if (favicon) result.seo.favicon = favicon.href;
  const appleTouchIcon = document.querySelector('link[rel="apple-touch-icon"]');
  if (appleTouchIcon) result.seo.apple_touch_icon = appleTouchIcon.href;

  // JSON-LD structured data
  const jsonLd = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { jsonLd.push(JSON.parse(s.textContent)); } catch (e) {}
  });
  if (jsonLd.length) result.seo.json_ld = jsonLd;

  return result;
})();
