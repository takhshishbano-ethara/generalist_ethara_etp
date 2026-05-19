(() => {
  "use strict";
  if (window.__earlyHooks) return;

  const hooks = {
    preloader: null,
    routeChanges: [],
    transitionLog: [],
    transitionLibrary: null,
    ioTargets: [],
    viewTransitions: [],
    _startTime: performance.now(),
  };
  window.__earlyHooks = hooks;

  // ---------------------------------------------------------------
  // 1. Splash / preloader lifecycle tracking
  // ---------------------------------------------------------------
  try {
    const PRELOADER_SELS = [
      '[class*="preloader"]', '[class*="Preloader"]',
      '[class*="loader"]', '[class*="Loader"]',
      '[class*="loading"]', '[class*="Loading"]',
      '[class*="splash"]', '[class*="Splash"]',
      '[class*="intro-screen"]', '[class*="page-loader"]',
      '[id*="preloader"]', '[id*="loader"]',
      '[data-preloader]', '[data-loader]',
      '.pace', '.nprogress',
    ];

    function findPreloader() {
      for (const sel of PRELOADER_SELS) {
        try {
          const el = document.querySelector(sel);
          if (el) return { el, sel };
        } catch (e) {}
      }
      return null;
    }

    function capturePreloaderState(el, sel) {
      try {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return {
          found: true,
          selector: sel,
          tag: el.tagName.toLowerCase(),
          classes: Array.from(el.classList).slice(0, 8),
          id: el.id || null,
          visible: cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.01,
          opacity: cs.opacity,
          display: cs.display,
          position: cs.position,
          zIndex: cs.zIndex,
          background: cs.backgroundColor,
          animationName: cs.animationName !== 'none' ? cs.animationName : null,
          transitionDuration: cs.transitionDuration,
          transitionProperty: cs.transitionProperty,
          dimensions: { w: Math.round(r.width), h: Math.round(r.height) },
          appearedAt: performance.now(),
          disappearedAt: null,
          visibleDurationMs: null,
          exitMethod: null,
        };
      } catch (e) {
        return { found: true, selector: sel, error: e.message };
      }
    }

    function watchPreloaderDisappear(el, record) {
      try {
        const observer = new MutationObserver(() => {
          try {
            const cs = getComputedStyle(el);
            const gone = cs.display === 'none' ||
                         cs.visibility === 'hidden' ||
                         parseFloat(cs.opacity) < 0.02 ||
                         !document.contains(el);
            if (gone && !record.disappearedAt) {
              record.disappearedAt = performance.now();
              record.visibleDurationMs = Math.round(record.disappearedAt - record.appearedAt);
              record.exitMethod = !document.contains(el) ? 'removed' :
                                  cs.display === 'none' ? 'display-none' :
                                  parseFloat(cs.opacity) < 0.02 ? 'fade-out' : 'hidden';
              observer.disconnect();
            }
          } catch (e) { observer.disconnect(); }
        });
        if (el.parentElement) {
          observer.observe(el.parentElement, { childList: true, subtree: true });
        }
        observer.observe(el, { attributes: true, attributeFilter: ['class', 'style'] });
        // Fallback: check periodically for 15 seconds
        let checks = 0;
        const interval = setInterval(() => {
          checks++;
          if (checks > 60 || record.disappearedAt) { clearInterval(interval); return; }
          try {
            const cs = getComputedStyle(el);
            const gone = cs.display === 'none' || cs.visibility === 'hidden' ||
                         parseFloat(cs.opacity) < 0.02 || !document.contains(el);
            if (gone && !record.disappearedAt) {
              record.disappearedAt = performance.now();
              record.visibleDurationMs = Math.round(record.disappearedAt - record.appearedAt);
              record.exitMethod = !document.contains(el) ? 'removed' :
                                  cs.display === 'none' ? 'display-none' : 'fade-out';
              observer.disconnect();
              clearInterval(interval);
            }
          } catch (e) { clearInterval(interval); }
        }, 250);
      } catch (e) {}
    }

    // Check immediately
    const found = findPreloader();
    if (found) {
      hooks.preloader = capturePreloaderState(found.el, found.sel);
      if (hooks.preloader.visible) {
        watchPreloaderDisappear(found.el, hooks.preloader);
      }
    } else {
      // Watch for preloader added to DOM later
      try {
        const bodyObserver = new MutationObserver(() => {
          const f = findPreloader();
          if (f && !hooks.preloader) {
            hooks.preloader = capturePreloaderState(f.el, f.sel);
            if (hooks.preloader.visible) {
              watchPreloaderDisappear(f.el, hooks.preloader);
            }
            bodyObserver.disconnect();
          }
        });
        const waitForBody = () => {
          if (document.body) {
            bodyObserver.observe(document.body, { childList: true, subtree: true });
            setTimeout(() => bodyObserver.disconnect(), 15000);
          } else {
            requestAnimationFrame(waitForBody);
          }
        };
        waitForBody();
      } catch (e) {}
    }
  } catch (e) {}

  // ---------------------------------------------------------------
  // 2. SPA route change interception (pushState / replaceState)
  // ---------------------------------------------------------------
  try {
    const origPush = history.pushState?.bind(history);
    const origReplace = history.replaceState?.bind(history);

    if (origPush) {
      history.pushState = function (...args) {
        const from = location.href;
        origPush(...args);
        hooks.routeChanges.push({
          type: 'pushState',
          from,
          to: args[2] ? new URL(args[2], location.origin).href : location.href,
          timestamp: performance.now(),
        });
      };
    }
    if (origReplace) {
      history.replaceState = function (...args) {
        const from = location.href;
        origReplace(...args);
        hooks.routeChanges.push({
          type: 'replaceState',
          from,
          to: args[2] ? new URL(args[2], location.origin).href : location.href,
          timestamp: performance.now(),
        });
      };
    }
    window.addEventListener('popstate', () => {
      hooks.routeChanges.push({
        type: 'popstate',
        to: location.href,
        timestamp: performance.now(),
      });
    });
  } catch (e) {}

  // ---------------------------------------------------------------
  // 3. Navigation API (Chrome 102+)
  // ---------------------------------------------------------------
  try {
    if (window.navigation) {
      navigation.addEventListener('navigate', (event) => {
        hooks.routeChanges.push({
          type: 'navigation-api',
          to: event.destination?.url,
          navigationType: event.navigationType,
          canIntercept: event.canIntercept,
          hashChange: event.hashChange,
          timestamp: performance.now(),
        });
      });
    }
  } catch (e) {}

  // ---------------------------------------------------------------
  // 4. Page transition library hooks (deferred — libs load async)
  // ---------------------------------------------------------------
  function hookTransitionLibraries() {
    try {
      // Barba.js
      if (window.barba && !hooks._barbaHooked) {
        hooks._barbaHooked = true;
        hooks.transitionLibrary = { name: 'barba', version: window.barba.version || null };
        try {
          barba.hooks.before((data) => {
            hooks.transitionLog.push({
              lib: 'barba', hook: 'before',
              from: data?.current?.url?.href || data?.current?.url,
              to: data?.next?.url?.href || data?.next?.url,
              timestamp: performance.now(),
            });
          });
          barba.hooks.afterLeave((data) => {
            hooks.transitionLog.push({ lib: 'barba', hook: 'afterLeave', timestamp: performance.now() });
          });
          barba.hooks.beforeEnter((data) => {
            hooks.transitionLog.push({ lib: 'barba', hook: 'beforeEnter', timestamp: performance.now() });
          });
          barba.hooks.after((data) => {
            hooks.transitionLog.push({
              lib: 'barba', hook: 'after',
              to: data?.next?.url?.href || data?.next?.url,
              timestamp: performance.now(),
            });
          });
        } catch (e) {}
      }

      // Swup
      if (window.swup && !hooks._swupHooked) {
        hooks._swupHooked = true;
        hooks.transitionLibrary = { name: 'swup', version: window.swup.version || null };
        try {
          const swupEvents = ['visit:start', 'animation:out:start', 'animation:out:end',
                              'content:replace', 'animation:in:start', 'animation:in:end', 'visit:end'];
          for (const evt of swupEvents) {
            window.swup.hooks?.on(evt, () => {
              hooks.transitionLog.push({ lib: 'swup', hook: evt, timestamp: performance.now() });
            });
          }
        } catch (e) {}
      }

      // Highway.js
      if (window.Highway && !hooks._highwayHooked) {
        hooks._highwayHooked = true;
        hooks.transitionLibrary = { name: 'highway', version: null };
      }
    } catch (e) {}
  }

  // Check immediately and re-check periodically for async-loaded libs
  hookTransitionLibraries();
  let libCheckCount = 0;
  const libCheckInterval = setInterval(() => {
    hookTransitionLibraries();
    libCheckCount++;
    if (libCheckCount > 40 || (hooks._barbaHooked || hooks._swupHooked || hooks._highwayHooked)) {
      clearInterval(libCheckInterval);
    }
  }, 500);

  // ---------------------------------------------------------------
  // 5. View Transitions API interception
  // ---------------------------------------------------------------
  try {
    if (document.startViewTransition) {
      const origVT = document.startViewTransition.bind(document);
      document.startViewTransition = function (callback) {
        const start = performance.now();
        hooks.viewTransitions.push({ start, end: null });
        const vt = origVT(callback);
        if (vt && vt.finished) {
          vt.finished.then(() => {
            const entry = hooks.viewTransitions[hooks.viewTransitions.length - 1];
            if (entry) entry.end = performance.now();
          }).catch(() => {});
        }
        return vt;
      };
      if (!hooks.transitionLibrary) {
        hooks.transitionLibrary = { name: 'view-transitions-api', version: null };
      }
    }
  } catch (e) {}

  // ---------------------------------------------------------------
  // 6. IntersectionObserver interception
  // ---------------------------------------------------------------
  try {
    const OrigIO = window.IntersectionObserver;
    if (OrigIO) {
      window.IntersectionObserver = function (callback, options) {
        const io = new OrigIO(callback, options);
        const origObserve = io.observe.bind(io);
        io.observe = function (el) {
          try {
            if (el instanceof Element && hooks.ioTargets.length < 200) {
              hooks.ioTargets.push({
                tag: el.tagName?.toLowerCase(),
                id: el.id || null,
                classes: Array.from(el.classList || []).slice(0, 4),
                threshold: options?.threshold,
                rootMargin: options?.rootMargin,
              });
            }
          } catch (e) {}
          return origObserve(el);
        };
        return io;
      };
      // Preserve prototype chain
      window.IntersectionObserver.prototype = OrigIO.prototype;
    }
  } catch (e) {}

})();
