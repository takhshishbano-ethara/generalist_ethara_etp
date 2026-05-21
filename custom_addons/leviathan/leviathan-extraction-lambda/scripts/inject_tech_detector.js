(() => {
  const result = {
    globals: {},
    scripts: [],
    dom_markers: {},
    meta: {},
    libraries: {},
    platforms: {},
    cms: {},
    css_tools: {},
  };

  // --- GLOBAL VARIABLE CHECKS ---
  const globalChecks = {
    three_js: ['THREE', '__THREE__'],
    gsap: ['gsap', 'GreenSock', 'TweenMax', 'TweenLite', 'TimelineMax'],
    lenis: ['Lenis', '__lenis'],
    lottie: ['lottie', 'bodymovin'],
    react: ['__REACT_DEVTOOLS_GLOBAL_HOOK__', '__NEXT_DATA__'],
    vue: ['__VUE__', '__NUXT__'],
    angular: ['ng'],
    alpine: ['Alpine'],
    anime: ['anime'],
    barba: ['barba'],
    locomotive: ['LocomotiveScroll'],
    pixi: ['PIXI'],
    babylon: ['BABYLON'],
    p5: ['p5'],
    rive: ['rive'],
    swup: ['swup'],
    highway: ['Highway'],
    motion_one: ['Motion'],
  };

  for (const [lib, globals] of Object.entries(globalChecks)) {
    for (const g of globals) {
      if (window[g] !== undefined) {
        result.globals[lib] = result.globals[lib] || [];
        result.globals[lib].push(g);
      }
    }
  }

  // --- SCRIPT SOURCES ---
  const scripts = Array.from(document.querySelectorAll('script[src]'));
  result.scripts = scripts.map(s => s.src).filter(Boolean);

  // --- LIBRARY VERSION EXTRACTION ---
  if (window.gsap) {
    result.libraries.gsap = { version: window.gsap.version || 'detected', type: 'animation' };
    if (window.ScrollTrigger) result.libraries.ScrollTrigger = { version: 'detected', type: 'scroll-animation' };
    if (window.Flip) result.libraries.Flip = { version: 'detected', type: 'layout-animation' };
    if (window.SplitText) result.libraries.SplitText = { version: 'detected', type: 'text-animation' };
    if (window.DrawSVGPlugin) result.libraries.DrawSVGPlugin = { version: 'detected', type: 'svg-animation' };
    if (window.MorphSVGPlugin) result.libraries.MorphSVGPlugin = { version: 'detected', type: 'svg-animation' };
    if (window.ScrollSmoother) result.libraries.ScrollSmoother = { version: 'detected', type: 'smooth-scroll' };
  }

  if (window.THREE) {
    result.libraries.three_js = { version: window.THREE.REVISION || 'detected', type: '3d-rendering' };
  }

  if (window.Lenis || window.__lenis) {
    result.libraries.lenis = { version: 'detected', type: 'smooth-scroll' };
  }

  if (window.__NEXT_DATA__) {
    result.libraries.nextjs = {
      version: window.__NEXT_DATA__.buildId ? 'detected' : 'unknown',
      type: 'framework',
      router: window.__NEXT_DATA__.page ? 'pages' : 'app',
    };
  }

  if (window.anime) result.libraries.anime_js = { version: 'detected', type: 'animation' };
  if (window.barba) result.libraries.barba_js = { version: 'detected', type: 'page-transition' };
  if (window.LocomotiveScroll) result.libraries.locomotive_scroll = { version: 'detected', type: 'smooth-scroll' };
  if (window.PIXI) result.libraries.pixi_js = { version: window.PIXI.VERSION || 'detected', type: '2d-rendering' };
  if (window.BABYLON) result.libraries.babylon_js = { version: window.BABYLON.Engine?.Version || 'detected', type: '3d-rendering' };
  if (window.p5) result.libraries.p5_js = { version: 'detected', type: 'creative-coding' };
  if (window.Alpine) result.libraries.alpine_js = { version: window.Alpine.version || 'detected', type: 'framework' };

  // --- DOM MARKER DETECTION ---
  const markerChecks = {
    three_js: ['canvas[data-engine]', 'canvas.webgl'],
    framer_motion: ['[data-framer-component-type]', '[data-projection-id]'],
    lenis: ['html.lenis', 'html.lenis-smooth', '[data-lenis]'],
    react: ['#__next', '[data-reactroot]', '#root[data-reactroot]'],
    vue: ['#__nuxt', '[data-v-]', '#app[data-v-app]'],
    lottie: ['lottie-player', 'dotlottie-player', '[data-anim-type]'],
    angular: ['[ng-version]', '[_nghost]', 'app-root'],
    solid: ['[data-hk]'],
    qwik: ['[q\\:container]'],
    astro: ['[data-astro-cid]', '[data-astro-source-file]'],
    remix: ['[data-remix-run]'],
    gatsby: ['#___gatsby'],
    rive: ['canvas[data-rive]', 'rive-canvas'],
    spline: ['[data-spline]', 'canvas.spline-canvas'],
  };

  for (const [lib, selectors] of Object.entries(markerChecks)) {
    for (const sel of selectors) {
      try {
        if (document.querySelector(sel)) {
          result.dom_markers[lib] = result.dom_markers[lib] || [];
          result.dom_markers[lib].push(sel);
        }
      } catch (e) {}
    }
  }

  // Svelte detection
  const svelteCheck = document.querySelectorAll('[class]');
  for (const el of svelteCheck) {
    if (Array.from(el.classList).some(c => c.startsWith('svelte-'))) {
      result.dom_markers.svelte = ['class^="svelte-"'];
      break;
    }
  }

  // Astro fallback: check for data-astro-* attributes
  if (!result.dom_markers.astro) {
    const allEls = document.querySelectorAll('*');
    for (let i = 0; i < Math.min(allEls.length, 200); i++) {
      for (const attr of allEls[i].attributes) {
        if (attr.name.startsWith('data-astro')) {
          result.dom_markers.astro = [attr.name];
          break;
        }
      }
      if (result.dom_markers.astro) break;
    }
  }

  // --- PLATFORM DETECTION ---
  // Webflow
  if (document.documentElement.getAttribute('data-wf-site') || document.querySelector('[data-wf-page]') || document.querySelector('html.w-mod-js')) {
    result.platforms.webflow = { detected: true, evidence: 'data-wf-site or data-wf-page attribute' };
  }

  // WordPress
  if (document.querySelector('link[href*="wp-content"]') || document.querySelector('script[src*="wp-includes"]') || document.querySelector('meta[name="generator"][content*="WordPress"]')) {
    result.platforms.wordpress = { detected: true, evidence: 'wp-content/wp-includes paths' };
    const gen = document.querySelector('meta[name="generator"]');
    if (gen && gen.content.includes('WordPress')) result.platforms.wordpress.version = gen.content;
  }

  // Shopify
  if (window.Shopify || document.querySelector('script[src*="cdn.shopify.com"]') || document.querySelector('link[href*="cdn.shopify.com"]')) {
    result.platforms.shopify = { detected: true, evidence: 'Shopify global or CDN', theme: window.Shopify?.theme?.name };
  }

  // Squarespace
  if (window.Static?.SQUARESPACE_CONTEXT || document.querySelector('[data-squarespace-cacheversion]')) {
    result.platforms.squarespace = { detected: true, evidence: 'SQUARESPACE_CONTEXT or data attribute' };
  }

  // Wix
  if (document.querySelector('meta[name="generator"][content*="Wix"]') || window.wixBiSession || document.querySelector('script[src*="static.parastorage.com"]')) {
    result.platforms.wix = { detected: true, evidence: 'Wix meta generator or parastorage CDN' };
  }

  // Framer
  if (window.__framer_importedComponents || document.querySelector('[data-framer-component-type]') || document.querySelector('script[src*="framerusercontent.com"]')) {
    result.platforms.framer = { detected: true, evidence: '__framer or framerusercontent.com' };
  }

  // Cargo
  if (document.querySelector('[data-cargo]') || document.querySelector('link[href*="cargo.site"]')) {
    result.platforms.cargo = { detected: true, evidence: 'data-cargo attribute' };
  }

  // --- CMS DETECTION ---
  // Check script/link sources for CMS CDN patterns
  const allSrcElements = document.querySelectorAll('script[src], link[href], img[src]');
  const allSrcs = [];
  allSrcElements.forEach(el => {
    const src = el.src || el.href || '';
    if (src) allSrcs.push(src);
  });
  const srcStr = allSrcs.join(' ');

  if (srcStr.includes('cdn.sanity.io') || srcStr.includes('sanity.io/v')) {
    result.cms.sanity = { detected: true, evidence: 'cdn.sanity.io' };
  }
  if (srcStr.includes('ctfassets.net') || srcStr.includes('contentful.com')) {
    result.cms.contentful = { detected: true, evidence: 'ctfassets.net' };
  }
  if (srcStr.includes('prismic.io') || window.PrismicDOM) {
    result.cms.prismic = { detected: true, evidence: 'prismic.io CDN' };
  }
  if (srcStr.includes('datocms-assets.com') || srcStr.includes('dato-cms')) {
    result.cms.datocms = { detected: true, evidence: 'datocms-assets.com' };
  }
  if (srcStr.includes('storyblok.com') || window.StoryblokBridge) {
    result.cms.storyblok = { detected: true, evidence: 'storyblok.com' };
  }
  if (srcStr.includes('hygraph.com') || srcStr.includes('graphcms.com')) {
    result.cms.hygraph = { detected: true, evidence: 'hygraph or graphcms CDN' };
  }
  if (srcStr.includes('strapi') || srcStr.includes('/api/') && document.querySelector('meta[name="generator"][content*="Strapi"]')) {
    result.cms.strapi = { detected: true, evidence: 'strapi patterns' };
  }

  // --- CSS TOOLS DETECTION ---
  // Tailwind CSS: scan for utility class patterns
  const sampleEls = document.querySelectorAll('[class]');
  let tailwindScore = 0;
  const tailwindPatterns = /\b(flex|grid|p-\d|m-\d|text-\w+|bg-\w+|w-\d|h-\d|rounded|shadow|border|gap-\d|items-|justify-|space-[xy]-|max-w-|min-h-|overflow-)\b/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (tailwindPatterns.test(cls)) tailwindScore++;
  }
  if (tailwindScore >= 10) {
    result.css_tools.tailwind = { detected: true, confidence: tailwindScore >= 30 ? 'high' : 'medium' };
  }

  // styled-components: scan for sc- prefix classes
  let scCount = 0;
  for (let i = 0; i < Math.min(sampleEls.length, 200); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (/\bsc-[a-zA-Z]/.test(cls)) scCount++;
  }
  if (scCount >= 3) {
    result.css_tools.styled_components = { detected: true, evidence: `${scCount} sc- class prefixes` };
  }

  // CSS Modules: random hash-style class names
  let moduleCount = 0;
  for (let i = 0; i < Math.min(sampleEls.length, 200); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (/\w+_\w+__[a-zA-Z0-9]{5}/.test(cls)) moduleCount++;
  }
  if (moduleCount >= 5) {
    result.css_tools.css_modules = { detected: true, evidence: `${moduleCount} module-style classes` };
  }

  // Emotion: css-* prefix
  let emotionCount = 0;
  for (let i = 0; i < Math.min(sampleEls.length, 200); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (/\bcss-[a-zA-Z0-9]+\b/.test(cls)) emotionCount++;
  }
  if (emotionCount >= 5) {
    result.css_tools.emotion = { detected: true, evidence: `${emotionCount} css- class prefixes` };
  }

  // --- Pre-compute cssVars and scriptSrcsLower for framework detection ---
  const cssVars = [];
  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        if (rule.selectorText === ':root' || rule.selectorText === 'html') {
          const text = rule.cssText;
          const varMatches = text.match(/--[\w-]+/g);
          if (varMatches) cssVars.push(...varMatches);
        }
      }
    } catch (e) {}
  }
  const scriptSrcsLower = result.scripts.map(s => s.toLowerCase());

  // Bootstrap: class prefixes like btn btn-, col-md-, container-fluid, navbar
  let bootstrapScore = 0;
  const bootstrapPatterns = /\b(btn-(?:primary|secondary|danger|warning|success|info|outline)|col-(?:sm|md|lg|xl)-\d|container-fluid|navbar-(?:brand|toggler|nav|collapse)|d-(?:flex|none|block|inline)|row|modal-dialog|form-control|card-body|dropdown-menu)\b/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (bootstrapPatterns.test(cls)) bootstrapScore++;
  }
  if (bootstrapScore >= 5 || document.querySelector('link[href*="bootstrap"]') || scriptSrcsLower.some(s => s.includes('bootstrap'))) {
    result.css_tools.bootstrap = { detected: true, confidence: bootstrapScore >= 15 ? 'high' : 'medium', evidence: `${bootstrapScore} class matches` };
  }

  // Material UI (MUI): MuiButton, MuiPaper, etc.
  let muiScore = 0;
  const muiPatterns = /\bMui[A-Z][a-zA-Z]+-/;
  for (let i = 0; i < Math.min(sampleEls.length, 200); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (muiPatterns.test(cls)) muiScore++;
  }
  if (muiScore >= 3 || document.querySelector('style[data-emotion="css"]') && emotionCount >= 3) {
    result.css_tools.material_ui = { detected: true, confidence: muiScore >= 10 ? 'high' : 'medium', evidence: `${muiScore} Mui- class matches` };
  }

  // Chakra UI: chakra-* class prefixes, --chakra-* CSS variables
  let chakraScore = 0;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (/\bchakra-/.test(cls)) chakraScore++;
  }
  const chakraVars = cssVars.filter(v => v.startsWith('--chakra-')).length;
  if (chakraScore >= 3 || chakraVars >= 5) {
    result.css_tools.chakra_ui = { detected: true, confidence: chakraScore >= 10 ? 'high' : 'medium', evidence: `${chakraScore} classes, ${chakraVars} CSS vars` };
  }

  // Ant Design: ant-* class prefixes
  let antdScore = 0;
  const antdPattern = /\bant-[a-z]/;
  for (let i = 0; i < Math.min(sampleEls.length, 200); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (antdPattern.test(cls)) antdScore++;
  }
  if (antdScore >= 5) {
    result.css_tools.ant_design = { detected: true, confidence: antdScore >= 15 ? 'high' : 'medium', evidence: `${antdScore} ant- class matches` };
  }

  // shadcn/ui + Radix: data-radix-* attributes alongside Tailwind
  const radixEls = document.querySelectorAll('[data-radix-collection-item], [data-radix-popper-content-wrapper], [data-state], [data-orientation]');
  if (radixEls.length >= 2) {
    result.css_tools.radix_ui = { detected: true, evidence: `${radixEls.length} data-radix/data-state elements` };
    if (tailwindScore >= 10) {
      result.css_tools.shadcn_ui = { detected: true, confidence: 'medium', evidence: 'Radix primitives + Tailwind detected' };
    }
  }

  // DaisyUI: data-theme attribute + Tailwind + daisy class patterns
  const daisyTheme = document.documentElement.getAttribute('data-theme') || document.body?.getAttribute('data-theme');
  if (daisyTheme && tailwindScore >= 5) {
    let daisyScore = 0;
    const daisyPatterns = /\b(btn|card|hero|navbar|footer|modal|drawer|collapse|tooltip|badge|alert|progress|tab|swap|join)\b/;
    for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
      const cls = sampleEls[i].className?.toString() || '';
      if (daisyPatterns.test(cls)) daisyScore++;
    }
    if (daisyScore >= 3) {
      result.css_tools.daisyui = { detected: true, confidence: daisyScore >= 8 ? 'high' : 'medium', evidence: `data-theme="${daisyTheme}", ${daisyScore} component classes` };
    }
  }

  // Bulma: bulma-specific class patterns
  let bulmaScore = 0;
  const bulmaPatterns = /\b(is-(?:primary|info|success|warning|danger|link|light|dark|size-\d)|columns|column|is-\d|hero-body|section|level-item)\b/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (bulmaPatterns.test(cls)) bulmaScore++;
  }
  if (bulmaScore >= 8 || document.querySelector('link[href*="bulma"]')) {
    result.css_tools.bulma = { detected: true, confidence: bulmaScore >= 15 ? 'high' : 'medium', evidence: `${bulmaScore} class matches` };
  }

  // Mantine: mantine-* class prefixes, --mantine-* CSS vars
  let mantineScore = 0;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (/\bmantine-/.test(cls)) mantineScore++;
  }
  const mantineVars = cssVars.filter(v => v.startsWith('--mantine-')).length;
  if (mantineScore >= 3 || mantineVars >= 5) {
    result.css_tools.mantine = { detected: true, confidence: mantineScore >= 10 ? 'high' : 'medium', evidence: `${mantineScore} classes, ${mantineVars} CSS vars` };
  }

  // Vuetify: v-* class prefixes
  let vuetifyScore = 0;
  const vuetifyPattern = /\bv-(?:btn|card|app-bar|navigation-drawer|dialog|toolbar|list|container|row|col-)/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (vuetifyPattern.test(cls)) vuetifyScore++;
  }
  if (vuetifyScore >= 3 || scriptSrcsLower.some(s => s.includes('vuetify'))) {
    result.css_tools.vuetify = { detected: true, confidence: vuetifyScore >= 10 ? 'high' : 'medium', evidence: `${vuetifyScore} v- component classes` };
  }

  // Quasar: q-* class prefixes
  let quasarScore = 0;
  const quasarPattern = /\bq-(?:btn|card|page|layout|header|footer|drawer|toolbar|table|field|input)/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (quasarPattern.test(cls)) quasarScore++;
  }
  if (quasarScore >= 3 || scriptSrcsLower.some(s => s.includes('quasar'))) {
    result.css_tools.quasar = { detected: true, confidence: quasarScore >= 10 ? 'high' : 'medium', evidence: `${quasarScore} q- component classes` };
  }

  // Foundation: foundation-specific patterns
  let foundationScore = 0;
  const foundationPatterns = /\b(small-\d|medium-\d|large-\d|cell|grid-x|grid-y|callout|reveal|orbit|off-canvas|top-bar)\b/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (foundationPatterns.test(cls)) foundationScore++;
  }
  if (foundationScore >= 5 || document.querySelector('link[href*="foundation"]') || scriptSrcsLower.some(s => s.includes('foundation'))) {
    result.css_tools.foundation = { detected: true, confidence: foundationScore >= 10 ? 'high' : 'medium', evidence: `${foundationScore} class matches` };
  }

  // PrimeReact / PrimeVue / PrimeNG: p-* class prefixes
  let primeScore = 0;
  const primePattern = /\bp-(?:button|dialog|datatable|inputtext|dropdown|panel|card|toolbar|menubar|sidebar|tabview)/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (primePattern.test(cls)) primeScore++;
  }
  if (primeScore >= 3) {
    result.css_tools.primereact = { detected: true, confidence: primeScore >= 8 ? 'high' : 'medium', evidence: `${primeScore} p- component classes` };
  }

  // Carbon Design System (IBM): bx--* or cds--* class prefixes
  let carbonScore = 0;
  const carbonPattern = /\b(?:bx|cds)--[a-z]/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (carbonPattern.test(cls)) carbonScore++;
  }
  if (carbonScore >= 3) {
    result.css_tools.carbon = { detected: true, confidence: carbonScore >= 10 ? 'high' : 'medium', evidence: `${carbonScore} bx--/cds-- class matches` };
  }

  // Fluent UI: fui-* or ms-* class prefixes
  let fluentScore = 0;
  const fluentPattern = /\b(?:fui-|ms-Button|ms-Stack|ms-TextField|FluentProvider)/;
  for (let i = 0; i < Math.min(sampleEls.length, 100); i++) {
    const cls = sampleEls[i].className?.toString() || '';
    if (fluentPattern.test(cls)) fluentScore++;
  }
  if (fluentScore >= 3) {
    result.css_tools.fluent_ui = { detected: true, confidence: fluentScore >= 8 ? 'high' : 'medium', evidence: `${fluentScore} fui-/ms- class matches` };
  }

  // Headless UI: data-headlessui-state attributes
  const headlessEls = document.querySelectorAll('[data-headlessui-state]');
  if (headlessEls.length >= 1) {
    result.css_tools.headless_ui = { detected: true, evidence: `${headlessEls.length} data-headlessui-state elements` };
  }

  // Panda CSS: atomic class patterns with specific hash format
  const pandaVars = cssVars.filter(v => v.startsWith('--panda-')).length;
  if (pandaVars >= 3) {
    result.css_tools.panda_css = { detected: true, evidence: `${pandaVars} --panda-* CSS vars` };
  }

  // --- META TAGS ---
  const generator = document.querySelector('meta[name="generator"]');
  if (generator) result.meta.generator = generator.content;

  const canvases = document.querySelectorAll('canvas');
  result.meta.canvas_count = canvases.length;
  result.meta.webgl_contexts = 0;
  canvases.forEach(c => {
    try {
      if (c.getContext('webgl2') || c.getContext('webgl') || c.getContext('experimental-webgl')) {
        result.meta.webgl_contexts++;
      }
    } catch (e) {}
  });

  // Horizontal scroll (inline styles)
  const horizontalScrollSections = [];
  document.querySelectorAll('[style*="overflow-x"], [style*="scroll-snap"]').forEach(el => {
    horizontalScrollSections.push(el.tagName + '.' + el.className?.toString().split(' ')[0]);
  });
  result.meta.horizontal_scroll_sections = horizontalScrollSections.length;

  // Horizontal scroll via computed styles — only count elements with actual horizontal overflow content
  result.meta.horizontal_scroll_computed = 0;
  document.querySelectorAll('section, [class*="horizontal"], [class*="slider"], [class*="scroll"], [data-scroll-section], [data-scroll-container]').forEach(el => {
    try {
      const style = getComputedStyle(el);
      const hasOverflow = style.overflowX === 'scroll' || style.overflowX === 'auto' || (style.scrollSnapType && style.scrollSnapType.includes('x'));
      const hasHorizontalContent = el.scrollWidth > el.clientWidth + 50;
      const isSubstantial = el.scrollWidth > window.innerWidth * 1.5;
      if (hasOverflow && hasHorizontalContent && isSubstantial) {
        result.meta.horizontal_scroll_computed++;
      }
    } catch (e) {}
  });
  // translateX containers that are wider than viewport (GSAP horizontal pin pattern)
  let hasWideTranslateX = false;
  document.querySelectorAll('[style*="translateX"], [style*="translate3d"]').forEach(el => {
    try {
      if (el.scrollWidth > window.innerWidth * 2) hasWideTranslateX = true;
    } catch (e) {}
  });
  result.meta.has_translate_x_container = hasWideTranslateX;

  // GSAP ScrollTrigger deep detection
  result.meta.has_scroll_trigger = false;
  result.meta.has_pin = false;
  result.meta.has_scrub = false;
  result.meta.has_horizontal_pin = false;
  result.meta.scroll_trigger_count = 0;
  result.meta.gsap_timeline_count = 0;

  if (window.gsap) {
    try { result.meta.gsap_timeline_count = gsap.globalTimeline?.getChildren()?.length || 0; } catch (e) {}
    result.meta.has_scroll_trigger = !!window.ScrollTrigger;
    if (window.ScrollTrigger) {
      try {
        const triggers = ScrollTrigger.getAll();
        result.meta.scroll_trigger_count = triggers.length;
        result.meta.has_pin = triggers.some(t => t.pin);
        result.meta.has_scrub = triggers.some(t => t.vars && t.vars.scrub);
        result.meta.has_horizontal_pin = triggers.some(t => t.pin && t.vars && t.vars.horizontal);
      } catch (e) {}
    }
  }

  // SVG animation detection
  result.meta.svg_animated_count = 0;
  result.meta.has_svg_paths = false;
  result.meta.has_lottie_players = document.querySelectorAll('lottie-player, dotlottie-player').length;
  result.meta.total_svg_paths = 0;
  result.meta.svg_element_count = 0;
  result.meta.svg_area_total = 0;
  result.meta.max_svg_path_count = 0;
  result.meta.large_svg_count = 0;
  document.querySelectorAll('svg').forEach(svg => {
    result.meta.svg_element_count++;
    if (svg.querySelectorAll('animate, animateTransform, animateMotion, set').length > 0) {
      result.meta.svg_animated_count++;
    }
    const pathCount = svg.querySelectorAll('path').length;
    result.meta.total_svg_paths += pathCount;
    if (pathCount > result.meta.max_svg_path_count) result.meta.max_svg_path_count = pathCount;
    if (pathCount > 5) {
      result.meta.has_svg_paths = true;
    }
    try {
      const rect = svg.getBoundingClientRect();
      // Filter out decorative icons (< 64px either dimension)
      if (rect.width > 64 && rect.height > 64) {
        result.meta.svg_area_total += rect.width * rect.height;
        // Count SVGs that are meaningfully large (> 10% viewport area)
        if (rect.width * rect.height > window.innerWidth * window.innerHeight * 0.1) {
          result.meta.large_svg_count++;
        }
      }
    } catch (e) {}
  });
  const viewportArea = window.innerWidth * window.innerHeight;
  result.meta.svg_area_ratio = viewportArea > 0 ? +(result.meta.svg_area_total / viewportArea).toFixed(3) : 0;
  result.meta.d3_svg_count = document.querySelectorAll('svg .tick, svg .domain, svg .axis, svg .line, svg .area').length;

  // --- SCROLLYTELLING / STICKY / SCROLL-SNAP SIGNALS ---
  // scrollama.js pattern + common scrollytelling conventions
  result.meta.scrollytelling_step_count = document.querySelectorAll(
    '[data-step], [data-scrollama], .scrolly-step, .step[data-step], .scrollama-step, [data-scroll-step]'
  ).length;

  // position: sticky sections (anchored scrollytelling backdrop)
  let stickyCount = 0;
  document.querySelectorAll('section, div, article, main, figure').forEach(el => {
    try {
      const style = getComputedStyle(el);
      if (style.position === 'sticky' || style.position === '-webkit-sticky') {
        const rect = el.getBoundingClientRect();
        // Only count substantial sticky elements (scrollytelling backdrops, not tiny UI bits)
        if (rect.width >= window.innerWidth * 0.4 && rect.height >= 100) stickyCount++;
      }
    } catch (e) {}
  });
  result.meta.sticky_section_count = stickyCount;

  // scroll-snap-type in CSS (computed)
  let snapCount = 0;
  document.querySelectorAll('section, div, main, ul, ol').forEach(el => {
    try {
      const style = getComputedStyle(el);
      if (style.scrollSnapType && style.scrollSnapType !== 'none') snapCount++;
    } catch (e) {}
  });
  result.meta.scroll_snap_count = snapCount;

  // IntersectionObserver target count (from early hooks, if available)
  try {
    const hooks = window.__earlyHooks;
    result.meta.io_target_count = Array.isArray(hooks?.ioTargets) ? hooks.ioTargets.length : 0;
  } catch (e) { result.meta.io_target_count = 0; }

  // Canvas size heuristic (large = primary experience, small = decorative)
  result.meta.largest_canvas_area = 0;
  result.meta.canvas_is_fullscreen = false;
  canvases.forEach(c => {
    try {
      const area = (c.width || 0) * (c.height || 0);
      if (area > result.meta.largest_canvas_area) result.meta.largest_canvas_area = area;
      const rect = c.getBoundingClientRect();
      if (rect.width >= window.innerWidth * 0.8 && rect.height >= window.innerHeight * 0.6) {
        result.meta.canvas_is_fullscreen = true;
      }
    } catch (e) {}
  });

  // CSS custom properties count (cssVars already populated above)
  result.meta.css_custom_properties_count = cssVars.length;

  // --- SCRIPT URL PATTERN MATCHING (for libraries not exposing globals) ---
  // scriptSrcsLower already computed above
  const scriptPatterns = {
    barba_js: ['barba', 'barba.js'],
    swup: ['swup'],
    highway: ['highway'],
    motion_one: ['motion', '@motionone'],
    popmotion: ['popmotion'],
    split_type: ['split-type', 'splittype'],
    scroll_magic: ['scrollmagic'],
    matter_js: ['matter.js', 'matter-js'],
    tone_js: ['tone.js', 'tone-js'],
  };

  for (const [lib, patterns] of Object.entries(scriptPatterns)) {
    for (const pattern of patterns) {
      if (scriptSrcsLower.some(s => s.includes(pattern))) {
        if (!result.libraries[lib]) {
          result.libraries[lib] = { version: 'detected', type: 'library' };
        }
        break;
      }
    }
  }

  // --- PERFORMANCE RESOURCE HINTS ---
  result.meta.resource_hints = {};
  document.querySelectorAll('link[rel="preload"]').forEach(l => {
    result.meta.resource_hints.preload = result.meta.resource_hints.preload || [];
    result.meta.resource_hints.preload.push({ href: l.href, as: l.getAttribute('as') });
  });
  document.querySelectorAll('link[rel="prefetch"]').forEach(l => {
    result.meta.resource_hints.prefetch = result.meta.resource_hints.prefetch || [];
    result.meta.resource_hints.prefetch.push(l.href);
  });
  document.querySelectorAll('link[rel="preconnect"]').forEach(l => {
    result.meta.resource_hints.preconnect = result.meta.resource_hints.preconnect || [];
    result.meta.resource_hints.preconnect.push(l.href);
  });

  return result;
})();
