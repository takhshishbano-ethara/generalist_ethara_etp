(() => {
  const result = {
    css_animations: [],
    css_transitions: [],
    gsap_tweens: [],
    gsap_scroll_triggers: [],
    gsap_config: {},
    lenis_config: {},
    web_animations: [],
    lottie_animations: [],
  };

  // --- WEB ANIMATIONS API ---
  try {
    const animations = document.getAnimations();
    for (const anim of animations) {
      const entry = {
        id: anim.id || null,
        playState: anim.playState,
        currentTime: anim.currentTime,
      };

      if (anim.effect) {
        const timing = anim.effect.getTiming();
        entry.duration = timing.duration;
        entry.delay = timing.delay;
        entry.easing = timing.easing;
        entry.iterations = timing.iterations;
        entry.direction = timing.direction;
        entry.fill = timing.fill;

        try {
          entry.keyframes = anim.effect.getKeyframes().map(kf => {
            const clean = {};
            for (const [k, v] of Object.entries(kf)) {
              if (v !== undefined && v !== '' && k !== 'composite' && k !== 'computedOffset') {
                clean[k] = v;
              }
            }
            return clean;
          });
        } catch (e) {}

        // Target element info
        if (anim.effect.target) {
          const target = anim.effect.target;
          entry.target = {
            tag: target.tagName?.toLowerCase(),
            id: target.id || null,
            classes: Array.from(target.classList || []).slice(0, 5),
            text: target.textContent?.substring(0, 50)?.trim() || null,
          };
        }
      }

      if (anim instanceof CSSAnimation) {
        entry.type = 'css_animation';
        entry.animationName = anim.animationName;
        result.css_animations.push(entry);
      } else if (anim instanceof CSSTransition) {
        entry.type = 'css_transition';
        entry.transitionProperty = anim.transitionProperty;
        result.css_transitions.push(entry);
      } else {
        entry.type = 'web_animation';
        result.web_animations.push(entry);
      }
    }
  } catch (e) {
    result._animation_error = e.message;
  }

  // --- GSAP EXTRACTION ---
  if (window.gsap) {
    result.gsap_config.version = window.gsap.version || 'unknown';
    result.gsap_config.defaults = {};

    try {
      const defaults = window.gsap.defaults();
      if (defaults) {
        result.gsap_config.defaults = {
          duration: defaults.duration,
          ease: defaults.ease,
        };
      }
    } catch (e) {}

    // Extract global timeline children
    try {
      const timeline = window.gsap.globalTimeline;
      if (timeline) {
        const children = timeline.getChildren(true, true, true);
        for (const child of children.slice(0, 100)) {
          try {
            const tween = {
              duration: child.duration(),
              delay: child.delay(),
              startTime: child.startTime(),
            };

            // Extract ease
            if (child.vars) {
              tween.ease = child.vars.ease || null;
              // Extract animated properties
              const animProps = {};
              for (const [k, v] of Object.entries(child.vars)) {
                if (['opacity', 'x', 'y', 'scale', 'scaleX', 'scaleY', 'rotation',
                     'rotationX', 'rotationY', 'rotationZ', 'skewX', 'skewY',
                     'translateX', 'translateY', 'translateZ', 'width', 'height',
                     'clipPath', 'filter', 'backgroundPosition', 'borderRadius',
                     'color', 'backgroundColor', 'borderColor', 'boxShadow',
                     'letterSpacing', 'fontSize', 'lineHeight', 'padding',
                     'margin', 'top', 'left', 'right', 'bottom',
                     'autoAlpha', 'visibility', 'display',
                     'stagger', 'yPercent', 'xPercent'].includes(k)) {
                  animProps[k] = v;
                }
              }
              tween.properties = animProps;
              tween.stagger = child.vars.stagger || null;
            }

            // Target info
            if (child._targets && child._targets.length > 0) {
              const target = child._targets[0];
              if (target instanceof Element) {
                tween.target = {
                  tag: target.tagName?.toLowerCase(),
                  id: target.id || null,
                  classes: Array.from(target.classList || []).slice(0, 3),
                };
              }
              tween.target_count = child._targets.length;
            }

            result.gsap_tweens.push(tween);
          } catch (e) {}
        }
      }
    } catch (e) {
      result._gsap_timeline_error = e.message;
    }

    // Extract ScrollTrigger instances
    if (window.ScrollTrigger) {
      try {
        const triggers = window.ScrollTrigger.getAll();
        for (const st of triggers) {
          try {
            const trigger = {
              start: st.start,
              end: st.end,
              scrub: st.vars?.scrub,
              pin: st.vars?.pin ? true : false,
              markers: st.vars?.markers || false,
            };

            if (st.trigger instanceof Element) {
              trigger.trigger_element = {
                tag: st.trigger.tagName?.toLowerCase(),
                id: st.trigger.id || null,
                classes: Array.from(st.trigger.classList || []).slice(0, 3),
              };
            }

            // Calculate scroll percentage
            const docHeight = document.documentElement.scrollHeight - window.innerHeight;
            if (docHeight > 0) {
              trigger.start_percent = Math.round((st.start / docHeight) * 100);
              trigger.end_percent = Math.round((st.end / docHeight) * 100);
            }

            result.gsap_scroll_triggers.push(trigger);
          } catch (e) {}
        }
      } catch (e) {
        result._scrolltrigger_error = e.message;
      }
    }
  }

  // --- LENIS EXTRACTION ---
  const lenisDetected = window.Lenis || window.__lenis ||
    document.documentElement.classList.contains('lenis') ||
    document.documentElement.classList.contains('lenis-smooth') ||
    document.querySelector('[data-lenis]');

  if (lenisDetected) {
    try {
      // Try direct global access first
      const lenisObj = window.__lenis || (typeof window.Lenis === 'object' ? window.Lenis : null);
      if (lenisObj && lenisObj.options) {
        result.lenis_config = {
          lerp: lenisObj.options.lerp,
          duration: lenisObj.options.duration,
          smooth: lenisObj.options.smooth,
          smoothTouch: lenisObj.options.smoothTouch,
          wheelMultiplier: lenisObj.options.wheelMultiplier,
          touchMultiplier: lenisObj.options.touchMultiplier,
          easing: lenisObj.options.easing ? lenisObj.options.easing.toString().slice(0, 80) : null,
        };
      } else {
        // Lenis loaded as ESM — infer config from DOM state and inline scripts
        const htmlEl = document.documentElement;
        const isSmooth = htmlEl.classList.contains('lenis-smooth');
        const isStopped = htmlEl.classList.contains('lenis-stopped');
        // Scan inline scripts for Lenis instantiation params
        let inferredLerp = null, inferredDuration = null;
        document.querySelectorAll('script:not([src])').forEach(s => {
          const txt = s.textContent || '';
          if (txt.includes('Lenis') || txt.includes('lenis')) {
            const lerpMatch = txt.match(/lerp\s*[:=]\s*([0-9.]+)/);
            const durMatch = txt.match(/duration\s*[:=]\s*([0-9.]+)/);
            if (lerpMatch) inferredLerp = parseFloat(lerpMatch[1]);
            if (durMatch) inferredDuration = parseFloat(durMatch[1]);
          }
        });
        result.lenis_config = {
          lerp: inferredLerp || 0.1,
          duration: inferredDuration || 1.2,
          smooth: isSmooth,
          smoothTouch: false,
          wheelMultiplier: 1,
          touchMultiplier: 2,
          _inferred: true,
        };
      }
    } catch (e) {
      result.lenis_config = { lerp: 0.1, duration: 1.2, smooth: true, _inferred: true };
    }
  }

  // --- LOTTIE EXTRACTION ---
  const lottiePlayers = document.querySelectorAll('lottie-player, dotlottie-player');
  lottiePlayers.forEach(player => {
    result.lottie_animations.push({
      src: player.getAttribute('src') || player.src,
      autoplay: player.hasAttribute('autoplay'),
      loop: player.hasAttribute('loop'),
      mode: player.getAttribute('mode'),
      speed: player.getAttribute('speed'),
      width: player.offsetWidth,
      height: player.offsetHeight,
    });
  });

  // --- WEBFLOW IX2 INTERACTION DATA ---
  // Webflow stores interaction/animation configs in window.__wf_ix2 or in
  // Webflow.require('ix2').store, or serialized inside script[type="application/json"]
  // with a data-wf-ix key. This captures durations, easings, and transforms that
  // are not exposed via CSS transitions.
  result.webflow_ix2 = null;
  try {
    // Method 1: global IX2 store
    if (window.Webflow && typeof window.Webflow.require === 'function') {
      try {
        const ix2 = window.Webflow.require('ix2');
        if (ix2 && ix2.store) {
          const state = typeof ix2.store.getState === 'function' ? ix2.store.getState() : ix2.store;
          if (state && state.ixData) {
            const ixData = state.ixData;
            const interactions = [];
            const events = ixData.events || {};
            const actions = ixData.actionLists || {};

            for (const [evtId, evt] of Object.entries(events)) {
              const actionCfg = (evt.action || {}).config || {};
              const entry = {
                id: evtId,
                name: evt.name || evt.eventTypeId || null,
                triggerType: evt.eventTypeId || (evt.source || {}).type || (evt.trigger || {}).type || null,
                actionListId: actionCfg.actionListId || (evt.action || {}).actionListId || null,
              };
              // Resolve action list to get timings
              const alId = entry.actionListId;
              if (alId && actions[alId]) {
                const al = actions[alId];
                const continuousParams = al.continuousParameterGroups || [];
                const keyframes = al.actionItemGroups || [];
                const timings = [];
                for (const group of keyframes) {
                  for (const item of (group.actionItems || [])) {
                    const cfg = item.config || {};
                    // IX2 durations can be seconds (< 50) or ms (>= 50)
                    const rawDur = cfg.duration;
                    const dur = rawDur != null ? (rawDur < 50 ? rawDur * 1000 : rawDur) : null;
                    const rawDelay = cfg.delay;
                    const delay = rawDelay != null ? (rawDelay < 50 ? rawDelay * 1000 : rawDelay) : null;
                    timings.push({
                      actionTypeId: item.actionTypeId || null,
                      duration: dur,
                      delay: delay,
                      easing: cfg.easing || null,
                      target: cfg.target ? {
                        selector: cfg.target.selector || cfg.target.useEventTarget || null,
                        selectorGuids: cfg.target.selectorGuids || null,
                      } : null,
                      value: cfg.value != null ? cfg.value : null,
                      xValue: cfg.xValue, yValue: cfg.yValue,
                      widthValue: cfg.widthValue, heightValue: cfg.heightValue,
                    });
                  }
                }
                entry.timings = timings;
                entry.continuousParams = continuousParams.map(cp => ({
                  parameterLabel: cp.parameterLabel,
                  continuousActionGroups: (cp.continuousActionGroups || []).map(cag => ({
                    keyframe: cag.keyframe,
                    actionItems: (cag.actionItems || []).map(ai => ({
                      actionTypeId: ai.actionTypeId,
                      duration: (ai.config || {}).duration != null ? ((ai.config || {}).duration < 50 ? (ai.config || {}).duration * 1000 : (ai.config || {}).duration) : null,
                      delay: (ai.config || {}).delay != null ? ((ai.config || {}).delay < 50 ? (ai.config || {}).delay * 1000 : (ai.config || {}).delay) : null,
                      easing: (ai.config || {}).easing,
                      target: (ai.config || {}).target ? { selector: ((ai.config || {}).target || {}).selector || ((ai.config || {}).target || {}).useEventTarget } : null,
                      value: (ai.config || {}).value,
                      xValue: (ai.config || {}).xValue, yValue: (ai.config || {}).yValue,
                    })),
                  })),
                }));
              }
              interactions.push(entry);
            }
            result.webflow_ix2 = { interactions, actionCount: Object.keys(actions).length, eventCount: Object.keys(events).length };
          }
        }
      } catch (e) {}
    }

    // Method 2: data attribute on HTML element
    if (!result.webflow_ix2) {
      const siteJson = document.querySelector('html[data-wf-site]');
      if (siteJson) {
        result.webflow_ix2 = { detected: true, method: 'data-wf-site', siteId: siteJson.getAttribute('data-wf-site') };
      }
    }

    // Method 3: inline script with IX2 data
    if (!result.webflow_ix2 || !result.webflow_ix2.interactions) {
      const scripts = document.querySelectorAll('script');
      for (const s of scripts) {
        const text = s.textContent || '';
        if (text.includes('"actionLists"') && text.includes('"events"') && text.length < 500000) {
          try {
            const parsed = JSON.parse(text);
            if (parsed.actionLists || parsed.events) {
              const events = parsed.events || {};
              const actions = parsed.actionLists || {};
              const interactions = [];
              for (const [evtId, evt] of Object.entries(events)) {
                const actionCfg3 = (evt.action || {}).config || {};
                const entry = {
                  id: evtId,
                  name: evt.name || evt.eventTypeId || null,
                  triggerType: evt.eventTypeId || (evt.source || {}).type || null,
                  actionListId: actionCfg3.actionListId || (evt.action || {}).actionListId || null,
                };
                const alId = entry.actionListId;
                if (alId && actions[alId]) {
                  const timings = [];
                  for (const group of (actions[alId].actionItemGroups || [])) {
                    for (const item of (group.actionItems || [])) {
                      const cfg = item.config || {};
                      const rawDur = cfg.duration;
                      const dur = rawDur != null ? (rawDur < 50 ? rawDur * 1000 : rawDur) : null;
                      const rawDelay = cfg.delay;
                      const delay = rawDelay != null ? (rawDelay < 50 ? rawDelay * 1000 : rawDelay) : null;
                      timings.push({
                        actionTypeId: item.actionTypeId,
                        duration: dur,
                        delay: delay,
                        easing: cfg.easing || null,
                      });
                    }
                  }
                  entry.timings = timings;
                }
                interactions.push(entry);
              }
              result.webflow_ix2 = { interactions, actionCount: Object.keys(actions).length, eventCount: Object.keys(events).length, method: 'inline-script' };
              break;
            }
          } catch (e) {}
        }
      }
    }
  } catch (e) {
    result._webflow_ix2_error = e.message;
  }

  // --- Computed transition durations from all elements ---
  // Many platforms (Webflow, Framer) set transition via inline styles or
  // CSS classes. Walk the DOM to find any element with a non-zero transition-duration.
  result.computed_transitions = [];
  try {
    const allEls = document.querySelectorAll('*');
    const seen = new Set();
    for (const el of allEls) {
      if (result.computed_transitions.length >= 50) break;
      const cs = getComputedStyle(el);
      const rawDur = cs.transitionDuration;
      if (!rawDur || rawDur === '0s' || rawDur === '0ms') continue;
      // Parse max duration
      let maxMs = 0;
      for (const part of rawDur.split(',')) {
        const trimmed = part.trim().toLowerCase();
        if (trimmed.endsWith('ms')) maxMs = Math.max(maxMs, parseFloat(trimmed));
        else if (trimmed.endsWith('s')) maxMs = Math.max(maxMs, parseFloat(trimmed) * 1000);
      }
      if (maxMs <= 0) continue;
      const tag = el.tagName.toLowerCase();
      const cls = Array.from(el.classList).slice(0, 3).join('.');
      const key = tag + '.' + cls + ':' + rawDur;
      if (seen.has(key)) continue;
      seen.add(key);
      result.computed_transitions.push({
        tag,
        classes: Array.from(el.classList).slice(0, 5),
        id: el.id || null,
        transitionDuration: rawDur,
        transitionDurationMs: maxMs,
        transitionProperty: cs.transitionProperty,
        transitionTimingFunction: cs.transitionTimingFunction,
      });
    }
  } catch (e) {
    result._computed_transitions_error = e.message;
  }

  // --- KEYFRAME DEFINITIONS FROM STYLESHEETS ---
  const keyframeRules = [];
  for (const sheet of document.styleSheets) {
    try {
      for (const rule of sheet.cssRules) {
        if (rule instanceof CSSKeyframesRule) {
          const frames = [];
          for (const kfRule of rule.cssRules) {
            frames.push({
              offset: kfRule.keyText,
              style: kfRule.cssText.substring(kfRule.cssText.indexOf('{') + 1, kfRule.cssText.indexOf('}')).trim(),
            });
          }
          keyframeRules.push({
            name: rule.name,
            frames,
          });
        }
      }
    } catch (e) { /* cross-origin */ }
  }
  result.keyframe_definitions = keyframeRules;

  // --- GSAP / SCROLLTRIGGER / SPLITTYPE AUTHORING HOOK DRAIN ---
  // Plan A.7 — the context-installed `inject_gsap_authoring_hook.js`
  // wraps gsap/ScrollTrigger/SplitType at definition time and records each
  // authoring call (selectors + vars) in window.__gsapAuthoring. Drain it
  // here so the captured authoring intent lands in animation_data.json.
  // Falls back to empty object when the hook didn't install (non-GSAP sites).
  result.gsap_authoring = null;
  try {
    if (typeof window.__gsapAuthoringSnapshot === 'function') {
      result.gsap_authoring = window.__gsapAuthoringSnapshot();
    } else if (window.__gsapAuthoring) {
      // hook installed but snapshot helper missing — emit raw log
      const raw = window.__gsapAuthoring;
      result.gsap_authoring = {
        callCount: (raw.calls || []).length,
        scrollTriggerCount: (raw.scrollTriggerCalls || []).length,
        splitTypeCount: (raw.splitTypeInstances || []).length,
        registeredPlugins: raw.registeredPlugins || [],
        calls: (raw.calls || []).slice(0, 400),
        scrollTriggerCalls: raw.scrollTriggerCalls || [],
        splitTypeInstances: raw.splitTypeInstances || [],
        truncated: raw._truncated || {},
      };
    }
  } catch (e) {
    result._gsap_authoring_error = e.message;
  }

  // --- PAGE TRANSITION LIBRARY DETECTION ---
  result.page_transition_libraries = {};
  try {
    result.page_transition_libraries.barba = window.barba ? { version: window.barba.version || null, detected: true } : null;
    result.page_transition_libraries.swup = window.swup ? { version: window.swup.version || null, detected: true } : null;
    result.page_transition_libraries.highway = window.Highway ? { detected: true } : null;
    result.page_transition_libraries.viewTransitions = !!document.startViewTransition;
    result.page_transition_libraries.nextjs = !!window.__NEXT_DATA__;
    result.page_transition_libraries.nuxt = !!window.__NUXT__;
    result.page_transition_libraries.gatsby = !!window.___gatsby;
    // Vue Router / Nuxt transitions
    result.page_transition_libraries.vueRouter = !!(window.__VUE_ROUTER_SYMBOL__ || (window.__vue_app__ && window.__vue_app__.config?.globalProperties?.$router));
    // Check for Taxi.js (another transition library)
    result.page_transition_libraries.taxi = !!window.Taxi;
    // Detect from early hooks if available
    if (window.__earlyHooks?.transitionLibrary) {
      result.page_transition_libraries._detected = window.__earlyHooks.transitionLibrary;
    }
  } catch (e) {}

  return result;
})();
