(() => {
  "use strict";
  if (window.__gsapAuthoring) return;

  // GSAP authoring-time capture hook.
  // Injected via page.addInitScript() BEFORE any page JS loads.
  // Uses Object.defineProperty setter traps on window.gsap / window.ScrollTrigger /
  // window.SplitType so the instant the site assigns these globals, we wrap their
  // methods and record original call arguments (selectors, keyframe vars, duration,
  // ease, scrollTrigger configs) BEFORE GSAP transforms them into internal tweens.
  //
  // Data stored on window.__gsapAuthoring (matching window.__earlyHooks pattern).
  // The late-running inject_animation_capture.js reader drains this log into
  // animation_data.json via window.__gsapAuthoringSnapshot().

  const log = {
    calls: [],                  // gsap.to/from/fromTo/set/timeline/delayedCall
    scrollTriggerCalls: [],     // ScrollTrigger.create(...)
    splitTypeInstances: [],     // new SplitType(...) constructor args + result
    registeredPlugins: [],      // registerPlugin names
    _installed: performance.now(),
    _truncated: { calls: 0, st: 0, split: 0 },
  };
  window.__gsapAuthoring = log;

  const MAX_CALLS = 2000;
  const MAX_ST = 800;
  const MAX_SPLIT = 500;

  // -- Serialization helpers --

  function serializeTarget(t) {
    if (t == null) return null;
    if (typeof t === 'string') return { selector: t.slice(0, 200) };
    if (Array.isArray(t)) return { array_len: t.length, sample: t.slice(0, 3).map(serializeTarget) };
    if (typeof Element !== 'undefined' && t instanceof Element) {
      return {
        tag: t.tagName ? t.tagName.toLowerCase() : null,
        id: t.id || null,
        classes: Array.from(t.classList || []).slice(0, 5),
      };
    }
    if (t && typeof t.length === 'number') {
      return { nodelist_len: t.length };
    }
    if (t && typeof t === 'object') return { type: t.constructor?.name || 'object' };
    return { type: typeof t };
  }

  function cloneValue(v, depth) {
    if (depth > 4) return '[depth-cap]';
    if (v === null || v === undefined) return v;
    const t = typeof v;
    if (t === 'function') return '[function]';
    if (t === 'number' || t === 'boolean' || t === 'string') {
      return t === 'string' && v.length > 240 ? v.slice(0, 240) + '…' : v;
    }
    if (typeof Element !== 'undefined' && v instanceof Element) {
      return serializeTarget(v);
    }
    if (Array.isArray(v)) {
      return v.slice(0, 20).map(x => cloneValue(x, depth + 1));
    }
    if (t === 'object') {
      const out = {};
      let count = 0;
      for (const k in v) {
        if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
        if (count++ > 40) { out._truncated = true; break; }
        try { out[k] = cloneValue(v[k], depth + 1); } catch (e) { out[k] = '[err]'; }
      }
      return out;
    }
    return String(v);
  }

  function serializeVars(vars) {
    if (vars == null || typeof vars !== 'object') return vars;
    const out = {};
    for (const k in vars) {
      if (!Object.prototype.hasOwnProperty.call(vars, k)) continue;
      try {
        const v = vars[k];
        if (typeof v === 'function') {
          out[k] = '[callback]';
        } else {
          out[k] = cloneValue(v, 0);
        }
      } catch (e) { out[k] = '[err]'; }
    }
    return out;
  }

  function captureStack() {
    try {
      const lines = (new Error()).stack?.split('\n') || [];
      return lines.slice(2, 6).map(s => s.trim()).join(' | ').slice(0, 400) || null;
    } catch (e) { return null; }
  }

  function pushCall(entry) {
    if (log.calls.length >= MAX_CALLS) {
      log._truncated.calls++;
      return;
    }
    log.calls.push(entry);
  }

  // -- GSAP method wrapping --

  function wrapGsap(gsap) {
    if (!gsap || gsap.__leviathanWrapped) return gsap;
    try { gsap.__leviathanWrapped = true; } catch (e) {}

    const wrap = (name) => {
      const orig = gsap[name];
      if (typeof orig !== 'function') return;
      gsap[name] = function (...args) {
        try {
          const entry = {
            method: name,
            t: +(performance.now() - log._installed).toFixed(1),
            stack: captureStack(),
          };
          if (name === 'fromTo') {
            entry.target = serializeTarget(args[0]);
            entry.fromVars = serializeVars(args[1]);
            entry.toVars = serializeVars(args[2]);
            entry.position = args[3] != null ? cloneValue(args[3], 0) : null;
          } else if (name === 'timeline') {
            entry.vars = serializeVars(args[0]);
          } else if (name === 'set' || name === 'to' || name === 'from') {
            entry.target = serializeTarget(args[0]);
            entry.vars = serializeVars(args[1]);
          } else {
            entry.args = args.slice(0, 4).map(a => cloneValue(a, 0));
          }
          pushCall(entry);
        } catch (e) {}
        return orig.apply(this, args);
      };
    };

    ['to', 'from', 'fromTo', 'set', 'timeline', 'delayedCall'].forEach(wrap);

    // registerPlugin — track which plugins the site uses
    try {
      const origReg = gsap.registerPlugin;
      if (typeof origReg === 'function') {
        gsap.registerPlugin = function (...plugins) {
          try {
            for (const p of plugins) {
              const n = p && (p.name || p.displayName || p.constructor?.name);
              if (n) log.registeredPlugins.push(String(n));
            }
          } catch (e) {}
          return origReg.apply(this, plugins);
        };
      }
    } catch (e) {}

    // Timeline prototype wrapping — captures tl.to(), tl.from(), tl.fromTo(), etc.
    try {
      const TlCtor = gsap.core && gsap.core.Timeline;
      if (TlCtor && TlCtor.prototype && !TlCtor.prototype.__leviathanWrapped) {
        TlCtor.prototype.__leviathanWrapped = true;
        ['to', 'from', 'fromTo', 'set', 'add'].forEach(m => {
          const orig = TlCtor.prototype[m];
          if (typeof orig !== 'function') return;
          TlCtor.prototype[m] = function (...args) {
            try {
              const entry = { method: 'timeline.' + m, t: +(performance.now() - log._installed).toFixed(1) };
              if (m === 'fromTo') {
                entry.target = serializeTarget(args[0]);
                entry.fromVars = serializeVars(args[1]);
                entry.toVars = serializeVars(args[2]);
                entry.position = args[3] != null ? cloneValue(args[3], 0) : null;
              } else if (m === 'add') {
                entry.args = args.slice(0, 3).map(a => cloneValue(a, 0));
              } else {
                entry.target = serializeTarget(args[0]);
                entry.vars = serializeVars(args[1]);
                entry.position = args[2] != null ? cloneValue(args[2], 0) : null;
              }
              pushCall(entry);
            } catch (e) {}
            return orig.apply(this, args);
          };
        });
      }
    } catch (e) {}

    return gsap;
  }

  // -- ScrollTrigger wrapping --

  function wrapScrollTrigger(ST) {
    if (!ST || ST.__leviathanWrapped) return ST;
    try { ST.__leviathanWrapped = true; } catch (e) {}
    try {
      const origCreate = ST.create;
      if (typeof origCreate === 'function') {
        ST.create = function (vars, ...rest) {
          try {
            if (log.scrollTriggerCalls.length < MAX_ST) {
              log.scrollTriggerCalls.push({
                t: +(performance.now() - log._installed).toFixed(1),
                vars: serializeVars(vars),
                stack: captureStack(),
              });
            } else {
              log._truncated.st++;
            }
          } catch (e) {}
          return origCreate.call(this, vars, ...rest);
        };
      }
    } catch (e) {}
    return ST;
  }

  // -- SplitType wrapping --

  function wrapSplitType(ST) {
    if (!ST || ST.__leviathanWrapped) return ST;
    try {
      const Wrapped = new Proxy(ST, {
        construct(target, args) {
          const instance = Reflect.construct(target, args);
          try {
            if (log.splitTypeInstances.length < MAX_SPLIT) {
              log.splitTypeInstances.push({
                t: +(performance.now() - log._installed).toFixed(1),
                target: serializeTarget(args[0]),
                opts: args[1] ? cloneValue(args[1], 0) : null,
                chars: (instance.chars && instance.chars.length) || 0,
                words: (instance.words && instance.words.length) || 0,
                lines: (instance.lines && instance.lines.length) || 0,
              });
            } else {
              log._truncated.split++;
            }
          } catch (e) {}
          return instance;
        },
      });
      Wrapped.__leviathanWrapped = true;
      return Wrapped;
    } catch (e) {
      return ST;
    }
  }

  // -- Property setter traps for late-loaded libraries --

  function defineTrap(propName, wrapFn) {
    let _val = window[propName];
    if (_val) _val = wrapFn(_val);
    try {
      Object.defineProperty(window, propName, {
        configurable: true,
        enumerable: true,
        get() { return _val; },
        set(v) { _val = v ? wrapFn(v) : v; },
      });
    } catch (e) {
      // Fallback: poll if defineProperty fails (e.g., already non-configurable)
      let attempts = 0;
      const iv = setInterval(() => {
        if (window[propName]) {
          try { window[propName] = wrapFn(window[propName]); } catch (e2) {}
          clearInterval(iv);
        }
        if (++attempts > 200) clearInterval(iv);
      }, 50);
    }
  }

  defineTrap('gsap', wrapGsap);
  defineTrap('ScrollTrigger', wrapScrollTrigger);
  defineTrap('SplitType', wrapSplitType);

  // If already present (unlikely with early addInitScript install), wrap now
  if (window.gsap && !window.gsap.__leviathanWrapped) wrapGsap(window.gsap);
  if (window.ScrollTrigger && !window.ScrollTrigger.__leviathanWrapped) wrapScrollTrigger(window.ScrollTrigger);

  // -- Snapshot helper for the late extractor to drain captured data --

  window.__gsapAuthoringSnapshot = () => {
    return {
      callCount: log.calls.length,
      scrollTriggerCount: log.scrollTriggerCalls.length,
      splitTypeCount: log.splitTypeInstances.length,
      registeredPlugins: log.registeredPlugins.slice(),
      truncated: { ...log._truncated },
      calls: log.calls.slice(0, 400),
      scrollTriggerCalls: log.scrollTriggerCalls.slice(),
      splitTypeInstances: log.splitTypeInstances.slice(),
    };
  };
})();
