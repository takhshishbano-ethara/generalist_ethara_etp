"""
Phase 6: Performance Analysis.

Extracts Core Web Vitals, resource hints, optimization patterns,
and accessibility indicators using Playwright CDP and page evaluation.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_PERF_JS = """
(() => {
  const result = {
    navigation: null,
    resources: [],
    lcp: null,
    cls: null,
    tbt_estimate: null,
    preload_hints: [],
    prefetch_hints: [],
    preconnect_hints: [],
    image_optimization: {},
    reduced_motion: false,
    accessibility: {},
  };

  // Navigation timing
  try {
    const navEntries = performance.getEntriesByType('navigation');
    if (navEntries.length > 0) {
      const n = navEntries[0];
      result.navigation = {
        ttfb: Math.round(n.responseStart - n.requestStart),
        dom_interactive: Math.round(n.domInteractive),
        dom_complete: Math.round(n.domComplete),
        load_event: Math.round(n.loadEventEnd),
        transfer_size: n.transferSize,
        encoded_size: n.encodedBodySize,
        decoded_size: n.decodedBodySize,
        protocol: n.nextHopProtocol,
        redirect_count: n.redirectCount,
      };
    }
  } catch (e) {}

  // Resource summary
  try {
    const resources = performance.getEntriesByType('resource');
    const byType = {};
    for (const r of resources) {
      const type = r.initiatorType || 'other';
      if (!byType[type]) byType[type] = { count: 0, totalSize: 0, totalDuration: 0 };
      byType[type].count++;
      byType[type].totalSize += r.transferSize || 0;
      byType[type].totalDuration += r.duration || 0;
    }
    result.resources = Object.entries(byType).map(([type, data]) => ({
      type,
      count: data.count,
      total_size_kb: Math.round(data.totalSize / 1024),
      avg_duration_ms: Math.round(data.totalDuration / data.count),
    }));
    result.total_resources = resources.length;
    result.total_transfer_kb = Math.round(resources.reduce((sum, r) => sum + (r.transferSize || 0), 0) / 1024);
  } catch (e) {}

  // LCP estimate from PerformanceObserver entries (if available)
  try {
    const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
    if (lcpEntries.length > 0) {
      const last = lcpEntries[lcpEntries.length - 1];
      result.lcp = {
        time_ms: Math.round(last.startTime),
        element: last.element?.tagName || null,
        size: last.size,
        url: last.url?.substring(0, 200) || null,
      };
    }
  } catch (e) {}

  // CLS estimate from layout shift entries
  try {
    const layoutShifts = performance.getEntriesByType('layout-shift');
    let clsValue = 0;
    for (const entry of layoutShifts) {
      if (!entry.hadRecentInput) {
        clsValue += entry.value;
      }
    }
    result.cls = Math.round(clsValue * 1000) / 1000;
  } catch (e) {}

  // TBT estimate: sum of long tasks blocking time
  try {
    const longTasks = performance.getEntriesByType('longtask');
    let tbt = 0;
    for (const task of longTasks) {
      tbt += Math.max(0, task.duration - 50);
    }
    result.tbt_estimate = Math.round(tbt);
  } catch (e) {}

  // Resource hints
  document.querySelectorAll('link[rel="preload"]').forEach(l => {
    result.preload_hints.push({ href: l.href?.substring(0, 150), as: l.getAttribute('as'), type: l.type || null });
  });
  document.querySelectorAll('link[rel="prefetch"]').forEach(l => {
    result.prefetch_hints.push(l.href?.substring(0, 150));
  });
  document.querySelectorAll('link[rel="preconnect"]').forEach(l => {
    result.preconnect_hints.push(l.href?.substring(0, 150));
  });

  // Image optimization analysis
  const images = document.querySelectorAll('img');
  let lazyCount = 0;
  let srcsetCount = 0;
  let webpAvif = 0;
  let totalImages = 0;
  images.forEach(img => {
    if (img.offsetWidth === 0 && img.offsetHeight === 0) return;
    totalImages++;
    if (img.loading === 'lazy') lazyCount++;
    if (img.srcset) srcsetCount++;
    const src = (img.src || '').toLowerCase();
    if (src.includes('.webp') || src.includes('.avif')) webpAvif++;
  });
  // Also check picture/source elements
  document.querySelectorAll('source').forEach(s => {
    const type = (s.type || '').toLowerCase();
    if (type.includes('webp') || type.includes('avif')) webpAvif++;
  });
  result.image_optimization = {
    total_images: totalImages,
    lazy_loaded: lazyCount,
    with_srcset: srcsetCount,
    modern_format: webpAvif,
  };

  // prefers-reduced-motion detection
  try {
    for (const sheet of document.styleSheets) {
      try {
        for (const rule of sheet.cssRules) {
          if (rule instanceof CSSMediaRule) {
            const media = (rule.conditionText || rule.media?.mediaText || '').toLowerCase();
            if (media.includes('prefers-reduced-motion')) {
              result.reduced_motion = true;
              break;
            }
          }
        }
      } catch (e) {}
      if (result.reduced_motion) break;
    }
  } catch (e) {}

  // Accessibility indicators
  const a11y = {
    has_skip_link: false,
    has_aria_labels: 0,
    has_alt_text: 0,
    missing_alt: 0,
    focus_visible: false,
    color_scheme: null,
    lang_attribute: document.documentElement.lang || null,
  };

  // Skip link
  const firstLink = document.querySelector('a[href^="#"]');
  if (firstLink && /skip|jump|content/i.test(firstLink.textContent || '')) {
    a11y.has_skip_link = true;
  }

  // ARIA labels
  a11y.has_aria_labels = document.querySelectorAll('[aria-label], [aria-labelledby], [aria-describedby]').length;

  // Alt text
  images.forEach(img => {
    if (img.alt && img.alt.trim()) a11y.has_alt_text++;
    else if (img.offsetWidth > 50) a11y.missing_alt++;
  });

  // Focus visibility
  const focusStyles = document.querySelectorAll('[tabindex], a[href], button, input, select, textarea');
  a11y.focusable_elements = focusStyles.length;

  // Color scheme
  if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
    a11y.color_scheme = 'dark';
  } else {
    a11y.color_scheme = 'light';
  }

  // Touch target sizes (sample buttons)
  const buttons = document.querySelectorAll('button, a[href], [role="button"]');
  let smallTargets = 0;
  let totalTargets = 0;
  buttons.forEach(btn => {
    const rect = btn.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      totalTargets++;
      if (rect.width < 44 || rect.height < 44) smallTargets++;
    }
  });
  a11y.touch_targets = { total: totalTargets, below_44px: smallTargets };

  result.accessibility = a11y;

  return result;
})();
"""


async def analyze_performance(page: Page) -> dict:
    """Extract performance metrics from the loaded page."""
    try:
        raw = await page.evaluate(_PERF_JS)
    except Exception as exc:
        logger.warning("Performance analysis failed: %s", exc)
        return {"error": str(exc)}

    if not isinstance(raw, dict):
        return {"error": "Unexpected result type"}

    return raw


def generate_performance_targets(raw_data: dict) -> dict:
    """Generate recommended performance targets based on extracted data."""
    nav = raw_data.get("navigation") or {}
    lcp_data = raw_data.get("lcp")
    cls_val = raw_data.get("cls")
    tbt_val = raw_data.get("tbt_estimate")
    img_opt = raw_data.get("image_optimization", {})
    a11y = raw_data.get("accessibility", {})

    measured_lcp = lcp_data.get("time_ms") if lcp_data else None
    measured_cls = cls_val
    measured_tbt = tbt_val

    # Recommend targets slightly better than measured (or good defaults)
    targets = {
        "lighthouse_target": ">85",
        "lcp_target": "<2.5s",
        "cls_target": "<0.10",
        "tbt_target": "<200ms",
    }

    measured = {}
    if measured_lcp:
        measured["lcp_measured"] = f"{measured_lcp}ms"
        if measured_lcp < 2500:
            targets["lcp_target"] = "<2.5s"
        else:
            targets["lcp_target"] = f"<{round(measured_lcp / 1000 + 0.5, 1)}s (currently {round(measured_lcp / 1000, 1)}s)"

    if measured_cls is not None:
        measured["cls_measured"] = measured_cls
    if measured_tbt is not None:
        measured["tbt_measured"] = f"{measured_tbt}ms"

    optimizations = []
    if img_opt.get("total_images", 0) > 0:
        if img_opt.get("lazy_loaded", 0) < img_opt["total_images"] * 0.5:
            optimizations.append("lazy loading for below-fold images")
        if img_opt.get("modern_format", 0) < img_opt["total_images"] * 0.3:
            optimizations.append("WebP/AVIF image format conversion")
        if img_opt.get("with_srcset", 0) == 0:
            optimizations.append("responsive srcset for images")

    has_preload = len(raw_data.get("preload_hints", [])) > 0
    if not has_preload:
        optimizations.append("preload critical resources (fonts, hero image)")

    optimizations.extend([
        "code splitting for route-based chunks",
        "GPU-promoted layers for animated elements (will-change, transform3d)",
    ])

    reduced_motion = raw_data.get("reduced_motion", False)

    a11y_summary = {
        "lang_set": bool(a11y.get("lang_attribute")),
        "skip_link": a11y.get("has_skip_link", False),
        "aria_labels": a11y.get("has_aria_labels", 0),
        "alt_text_coverage": f"{a11y.get('has_alt_text', 0)}/{a11y.get('has_alt_text', 0) + a11y.get('missing_alt', 0)}",
        "touch_targets_below_44px": a11y.get("touch_targets", {}).get("below_44px", 0),
    }

    return {
        "targets": targets,
        "measured": measured,
        "optimizations": optimizations,
        "reduced_motion_handled": reduced_motion,
        "accessibility": a11y_summary,
        "raw": raw_data,
    }
