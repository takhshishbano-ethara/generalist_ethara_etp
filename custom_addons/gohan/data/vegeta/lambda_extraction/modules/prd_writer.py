"""
Phase 6B: PRD Writer.

Builds the Vegeta PRD prompt user-message containing the scraped-site bundle.
The system prompt (vegeta-prd-gen.md) is provided separately by Odoo.

Output strictly avoids markdown tables, follows ASCII-only / -> flow marker
conventions, and exposes the enriched bundle fields the Vegeta PRD generator
requires (vegeta_category, scrape_coverage, schema_org_entities,
openapi_specs, graphql_schemas, pricing_tiers, signup_form_fields,
observed_pages with page_type tags).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def build_prd_prompt(
    site_data: dict,
    style_data: dict,
    animation_data: dict,
    responsive_data: dict,
    asset_data: dict,
    webgl_data: dict | None = None,
    network_data: dict | None = None,
    performance_data: dict | None = None,
    auth_data: dict | None = None,
    brand_data: dict | None = None,
    component_tokens: dict | None = None,
    dark_mode_data: dict | None = None,
    output_dir: str | None = None,
    api_doc_data: dict | None = None,
    form_data: dict | None = None,
    business_signals: dict | None = None,
    vegeta_category: dict | None = None,
    scrape_coverage: str | None = None,
    observed_pages: list[dict] | None = None,
    schema_org_types: list[str] | None = None,
) -> str:
    """Build the Vegeta PRD prompt user-message.

    All extra dicts beyond the original 12 are optional; this function tolerates
    missing enrichments so the lambda can degrade gracefully when a probe times
    out or returns nothing. The first 13 positional/keyword args are the legacy
    pipeline inputs; the remaining kwargs carry the new Vegeta enrichment
    bundles produced by api_doc_prober, form_harvester, business_signals_extractor,
    vegeta_classifier, and page_typer.
    """
    parts = ["## SCRAPED SITE BUNDLE\n"]

    parts.append(_format_metadata(site_data, vegeta_category, scrape_coverage))

    parts.append(_format_visual_identity(style_data))

    if brand_data:
        parts.append(_format_brand_data(brand_data))

    if component_tokens:
        parts.append(_format_component_tokens(component_tokens))

    if dark_mode_data and dark_mode_data.get("has_dark_mode"):
        parts.append(_format_dark_mode(dark_mode_data))

    parts.append(_format_animations(animation_data))

    if webgl_data and webgl_data.get("detected"):
        parts.append(_format_webgl(webgl_data, site_data.get("category", "")))

    parts.append(_format_tech_stack(site_data, webgl_data))

    parts.append(_format_auth(auth_data))

    if form_data:
        parts.append(_format_form_data(form_data))

    if business_signals:
        parts.append(_format_business_signals(business_signals))

    if schema_org_types or (style_data.get("seo", {}) or {}).get("json_ld"):
        parts.append(_format_schema_org_entities(style_data, schema_org_types))

    if network_data:
        parts.append(_format_network(network_data))

    if api_doc_data:
        parts.append(_format_api_doc_data(api_doc_data))

    parts.append(_format_responsive(responsive_data))

    if performance_data:
        parts.append(_format_performance(performance_data))

    parts.append(_format_assets(asset_data))

    parts.append(_format_observed_pages(observed_pages, site_data))

    parts.append(_format_screenshot_references(output_dir))

    parts.append(_vegeta_instruction_tail(vegeta_category, scrape_coverage))

    return "\n".join(parts)


def _word_target(*_args, **_kwargs) -> str:
    return "3,200-4,800 words (5,000 hard cap, 800 floor)"


def _vegeta_instruction_tail(vegeta_category: dict | None, scrape_coverage: str | None) -> str:
    cat_name = "Unknown"
    if isinstance(vegeta_category, dict):
        cat_name = vegeta_category.get("category", "Unknown")
    coverage = scrape_coverage or "marketing_only"

    return f"""

---

## WRITE THE PRD NOW

Build a Vegeta-format PRD using ONLY the bundle above plus the named-reference
canonical patterns for the assigned category.

- assigned_category: {cat_name}
- scrape_coverage: {coverage}
- target_length: {_word_target()}

The 11 sections, each as an H3, are fixed and must appear in this order:
1. Overview
2. Goals & Non-Goals
3. User Roles & Permissions
4. Authentication & Onboarding
5. Core Features & User Flows  (5.1-5.x sub-sections, the longest section)
6. Data Model
7. API Design
8. UI/UX Requirements
9. Error Handling & Edge Cases
10. Non-Functional Requirements
11. Category-Specific Guidelines

Hard format rules:
- ASCII only. The flow marker is the literal two characters '->'. No Unicode arrows, em dashes, smart quotes, or emoji.
- NO markdown tables. Use bullets and nested bullets only. The pipe-and-dash table syntax is banned.
- Every color: hex code. Every entity field: name + type. Every endpoint: method + path. Every role: enumerated capabilities.
- Use the fictional product name in the body. The real brand and 2-3 peers belong only in the 'Reference Style' header.
- Definite voice. No 'probably', 'might', 'could', 'should be able to'. Commit to inference silently.
- No meta-commentary, no inference summary, no word-count trailer. The document ends at the last line of Section 11.

Evidence precedence (use the bundle above in this order):
1. openapi_specs and graphql_schemas (when present) -- entity field names and types come from these DIRECTLY into Section 6 and 7. Do not invent alternatives.
2. schema_org_entities from JSON-LD -- pin the visible entity types (Course, Product, Article, JobPosting, etc.) used to anchor Section 6.
3. signup_user_fields from form_data -- dictate the User entity fields in Section 6 and the signup flow in Section 4.
4. pricing_tiers from business_signals -- dictate role tiering in Section 3 and success metrics in Section 2.
5. observed_pages with page_type tags -- drive Section 5 sub-section selection. Pick CORE flows from typed pages first (landing, listing, detail, dashboard, pricing, auth, checkout, search, settings, docs).
6. api_endpoints + api_patterns from network_data -- real captured calls. Method and path are Tier 1 evidence.

When evidence is sparse (gated SaaS, marketing_only coverage), reconstruct from the named reference brands listed in the category emphasis table inside the system prompt. The reconstruction must be consistent with every observed signal above.
"""


def _format_metadata(site_data: dict, vegeta_category: dict | None = None, scrape_coverage: str | None = None) -> str:
    title = site_data.get("title", "Unknown")
    url = site_data.get("url", "")
    description = site_data.get("description", "")
    design_category = site_data.get("category", "")

    parts = ["### Bundle Metadata"]
    parts.append(f"- product_identity.observed_name: {title}")
    if description:
        parts.append(f"- product_identity.tagline_or_meta: {description[:200]}")
    parts.append(f"- target_url: {url}")
    if design_category:
        parts.append(f"- design_classification (visual aesthetic, not the Vegeta category): {design_category}")

    if isinstance(vegeta_category, dict):
        cat = vegeta_category.get("category", "Unknown")
        confidence = vegeta_category.get("confidence", "low")
        runner_up = vegeta_category.get("runner_up")
        parts.append(f"- assigned_category: {cat} (confidence={confidence})")
        if runner_up:
            parts.append(f"- assigned_category.runner_up: {runner_up}")
        scores = vegeta_category.get("scores", {}) or {}
        if scores:
            ranked = sorted(scores.items(), key=lambda x: -x[1])[:5]
            parts.append("- assigned_category.score_distribution (top 5):")
            for c, s in ranked:
                parts.append(f"  - {c}: {s}")
        evidence = vegeta_category.get("evidence", []) or []
        if evidence:
            parts.append("- assigned_category.evidence (top signals):")
            for e in evidence[:8]:
                parts.append(
                    f"  - source={e.get('source')} signal={e.get('signal')} "
                    f"-> {e.get('category')} weight={e.get('weight')}"
                )

    if scrape_coverage:
        parts.append(f"- scrape_coverage: {scrape_coverage}")

    parts.append("")
    return "\n".join(parts)


def _format_visual_identity(style_data: dict) -> str:
    parts = ["### Visual Identity"]

    colors = style_data.get("colors", []) or []
    if colors:
        parts.append("- Extracted Colors (by frequency, hex / count / css usages):")
        for c in colors[:15]:
            usages = ", ".join(c.get("usages", []) or [])
            parts.append(f"  - {c.get('hex', '?')}: count={c.get('count', '?')}, css=[{usages or 'n/a'}]")

    fonts = style_data.get("fonts", []) or []
    if fonts:
        parts.append("- Detected Fonts:")
        for f in fonts[:10]:
            weights = ", ".join(str(w) for w in (f.get("weights", []) or []))
            parts.append(f"  - {f.get('family', '?')}: weights=[{weights}], count={f.get('count', '?')}")

    font_faces = style_data.get("font_faces", []) or []
    if font_faces:
        parts.append("- @font-face Declarations:")
        for ff in font_faces[:10]:
            parts.append(
                f"  - {ff.get('family', 'Unknown')} "
                f"({ff.get('weight', 'normal')} {ff.get('style', 'normal')})"
            )

    type_scale = style_data.get("type_scale", []) or []
    if type_scale:
        parts.append("- Type Scale:")
        for ts in type_scale[:10]:
            tags = ", ".join(ts.get("tags", []) or [])
            parts.append(
                f"  - {ts.get('size_px', '?')}px / lh={ts.get('line_height', 'normal')} / "
                f"tracking={ts.get('letter_spacing', '0')} / {ts.get('font_family', '')} "
                f"{ts.get('font_weight', '')} -> [{tags or 'n/a'}]"
            )

    gradients = style_data.get("gradients", []) or []
    if gradients:
        parts.append("- Detected Gradients:")
        for g in gradients[:5]:
            parts.append(f"  - {g.get('value', '')[:150]} (used {g.get('count', '?')}x on {g.get('element', '')})")

    shadows = style_data.get("shadows", []) or []
    if shadows:
        parts.append("- Box Shadows:")
        for s in shadows[:5]:
            parts.append(f"  - {s.get('value', '')[:100]} (used {s.get('count', '?')}x)")

    radii = style_data.get("border_radii", []) or []
    if radii:
        parts.append("- Border Radius Tokens:")
        for r in radii[:8]:
            parts.append(f"  - {r.get('value', '?')} (used {r.get('count', '?')}x)")

    effects = style_data.get("effects", {}) or {}
    if effects:
        parts.append("- CSS Effects:")
        for prop, items in effects.items():
            if items:
                parts.append(f"  - {prop}: {items[0].get('value', '')[:80]} on {items[0].get('element', '')}")

    grid = style_data.get("grid", {}) or {}
    if grid.get("max_widths"):
        parts.append(
            "- Container Max-Widths: "
            + ", ".join(str(int(w)) + "px" for w in grid["max_widths"][:5])
        )
    if grid.get("layouts"):
        parts.append("- Grid Layouts:")
        for g in grid["layouts"][:5]:
            parts.append(
                f"  - {g.get('element', '')}: columns={g.get('columns', '')}, gap={g.get('gap', '')}"
            )

    spacing = style_data.get("spacing", {}) or {}
    if spacing.get("values"):
        parts.append(
            "- Spacing Values: "
            + ", ".join(str(v) + "px" for v in spacing["values"][:15])
        )
        if spacing.get("baseline_unit"):
            parts.append(f"- Baseline Grid Unit: {spacing['baseline_unit']}px")

    css_vars = style_data.get("css_variables", {}) or {}
    if css_vars:
        parts.append(f"- CSS Custom Properties ({len(css_vars)} detected, top 20):")
        for name, value in list(css_vars.items())[:20]:
            parts.append(f"  - {name}: {value}")

    seo = style_data.get("seo", {}) or {}
    if seo:
        parts.append("- SEO Metadata (json_ld surfaced separately):")
        for key, val in seo.items():
            if key == "json_ld":
                continue
            if val:
                parts.append(f"  - {key}: {str(val)[:150]}")

    parts.append("")
    return "\n".join(parts)


def _format_schema_org_entities(style_data: dict, schema_org_types: list[str] | None = None) -> str:
    parts = ["### Schema.org Entities (from JSON-LD -- Tier 1 evidence for Section 6)"]

    seo = style_data.get("seo", {}) or {}
    json_ld = seo.get("json_ld", []) or []
    if not isinstance(json_ld, list):
        json_ld = [json_ld]

    types_seen: list[str] = list(schema_org_types or [])
    entities: list[dict] = []

    for ld in json_ld:
        if not isinstance(ld, dict):
            continue
        graph = ld.get("@graph") if isinstance(ld.get("@graph"), list) else [ld]
        for node in graph:
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            primary: str | None = None
            if isinstance(t, list):
                types_seen.extend(str(x) for x in t)
                if t:
                    primary = str(t[0])
            elif t:
                primary = str(t)
                types_seen.append(primary)
            if primary:
                fields = [k for k in node.keys() if not k.startswith("@")][:25]
                entities.append({"type": primary, "fields": fields})

    types_unique = list(dict.fromkeys(types_seen))
    if types_unique:
        parts.append("- @type values observed (anchor Section 6 entities to these):")
        for t in types_unique[:20]:
            parts.append(f"  - {t}")
    else:
        parts.append("- No schema.org @type declarations captured.")

    if entities:
        parts.append("- Entity field exemplars (drawn from JSON-LD nodes):")
        for ent in entities[:12]:
            field_str = ", ".join(ent["fields"]) if ent["fields"] else "n/a"
            parts.append(f"  - {ent['type']} -> fields=[{field_str}]")

    parts.append("")
    return "\n".join(parts)


def _format_animations(animation_data: dict) -> str:
    parts = ["### Animation & Interaction Data"]

    animations = animation_data.get("animations", []) or []
    if animations:
        parts.append(f"- CSS/Web Animations ({len(animations)} detected, top 15):")
        for a in animations[:15]:
            target = a.get("target") or {}
            tag = target.get("tag", "?")
            classes = ".".join((target.get("classes", []) or [])[:2])
            el_name = f"{tag}.{classes}" if classes else tag
            dur = a.get("duration_ms") or a.get("duration", "?")
            dur_str = f"{int(dur)}ms" if isinstance(dur, (int, float)) and dur > 0 else "?ms"
            trigger = a.get("trigger", "load")
            parts.append(
                f"  - {el_name} ({a.get('type', 'animation')}): {dur_str}, "
                f"easing={a.get('easing', '?')}, iterations={a.get('iterations', '?')}, "
                f"trigger={trigger}"
            )
    else:
        parts.append("- CSS/Web Animations: None detected (site may use canvas/WebGL/JS-driven animation)")

    hover_states = animation_data.get("hover_states", []) or []
    if hover_states:
        has_css_changes = any(h.get("hover") for h in hover_states)
        if has_css_changes:
            parts.append("- Hover States with CSS Changes:")
            for h in hover_states[:10]:
                if h.get("hover"):
                    transition = (h.get("default") or {}).get("transitionDuration", "0ms")
                    parts.append(f"  - {h.get('selector', '')}: transition={transition}")
                    for prop, val in (h.get("hover") or {}).items():
                        def_val = (h.get("default") or {}).get(prop, "")
                        if val != def_val:
                            parts.append(f"    - {prop}: {def_val} -> {val}")
        else:
            parts.append(
                f"- Hover States: All {len(hover_states)} interactive elements use JS event "
                "listeners (not CSS transitions) -- likely canvas/WebGL-driven"
            )

    keyframes = animation_data.get("keyframe_definitions", []) or []
    if keyframes:
        parts.append("- CSS Keyframe Definitions:")
        for kf in keyframes[:10]:
            parts.append(f"  - @keyframes {kf.get('name', '')}: {kf.get('frame_count', '?')} frames")

    scroll_map = animation_data.get("scroll_animation_map", {}) or {}
    if scroll_map:
        parts.append("- Scroll-Triggered Animations:")
        for pos, anims in list(scroll_map.items())[:10]:
            parts.append(f"  - At {pos}%: {len(anims)} animation(s)")

    micro = animation_data.get("micro_interactions", []) or []
    if micro:
        parts.append("- Micro-Interactions:")
        for m in micro[:10]:
            parts.append(f"  - {m.get('element', '')}: {m.get('event', '')} -> {m.get('description', '')}")

    parts.append("")
    return "\n".join(parts)


def _format_webgl(webgl_data: dict, category: str) -> str:
    parts = ["### 3D / WebGL"]

    canvases = webgl_data.get("canvases", []) or []
    if canvases:
        parts.append("- WebGL Canvases:")
        for c in canvases:
            parts.append(
                f"  - canvas[{c.get('index')}]: size={c.get('width')}x{c.get('height')}, "
                f"css={c.get('cssWidth')}x{c.get('cssHeight')}, pixelRatio={c.get('pixelRatio', 1)}, "
                f"context={c.get('contextType', '?')}, antialias={c.get('antialias', False)}"
            )

    info = webgl_data.get("webgl_info", {}) or {}
    if info:
        parts.append(f"- gpu_renderer: {info.get('gpuRenderer', 'Unknown')}")
        parts.append(f"- max_texture_size: {info.get('maxTextureSize', 'Unknown')}")

    three = webgl_data.get("three_js")
    if three:
        parts.append(f"- three_js.revision: r{three.get('revision', 'unknown')}")
        if three.get("renderer"):
            r = three["renderer"]
            parts.append(f"  - renderer.type: {r.get('type', 'Unknown')}")
            if r.get("dataEngine"):
                parts.append(f"  - renderer.dataEngine: {r['dataEngine']}")
            if r.get("shadowMapEnabled"):
                parts.append(f"  - renderer.shadowMap: enabled (type={r.get('shadowMapType', '?')})")
            if r.get("toneMapping"):
                parts.append(
                    f"  - renderer.toneMapping: {r['toneMapping']} "
                    f"(exposure={r.get('toneMappingExposure', '?')})"
                )
        if three.get("controls"):
            parts.append(f"  - controls: {three['controls']}")
        if three.get("physics"):
            parts.append(f"  - physics_engine: {three['physics']}")
        if three.get("post_processing"):
            parts.append(f"  - post_processing: {', '.join(three['post_processing'])}")
        if three.get("loaders"):
            parts.append(f"  - loaders: {', '.join(three['loaders'])}")
        if three.get("code_hints"):
            parts.append(f"  - detected_features: {', '.join(three['code_hints'])}")

    assets_3d = webgl_data.get("detected_3d_assets") or webgl_data.get("detected3DAssets", {}) or {}
    if assets_3d:
        parts.append("- 3D Asset Files:")
        for atype, files in assets_3d.items():
            parts.append(f"  - {atype}: {len(files)} file(s)")
            for f in files[:3]:
                parts.append(f"    - {f}")

    if webgl_data.get("r3f_likely"):
        parts.append("- react_three_fiber: likely detected")

    parts.append("")
    return "\n".join(parts)


def _format_tech_stack(site_data: dict, webgl_data: dict | None) -> str:
    parts = ["### Tech Stack"]

    tech = site_data.get("tech_stack", {}) or {}
    if tech:
        parts.append("- Detected Technologies:")
        for name, info in tech.items():
            version = info.get("version", "detected") if isinstance(info, dict) else "detected"
            lib_type = info.get("type", "unknown") if isinstance(info, dict) else "unknown"
            parts.append(f"  - {name}: version={version}, type={lib_type}")

    platforms = site_data.get("platforms", {}) or {}
    if platforms:
        parts.append("- Platforms:")
        for name, info in platforms.items():
            ev = info.get("evidence", "detected") if isinstance(info, dict) else "detected"
            parts.append(f"  - {name}: {ev}")

    cms = site_data.get("cms", {}) or {}
    if cms:
        parts.append("- CMS:")
        for name, info in cms.items():
            ev = info.get("evidence", "detected") if isinstance(info, dict) else "detected"
            parts.append(f"  - {name}: {ev}")

    css_tools = site_data.get("css_tools", {}) or {}
    if css_tools:
        parts.append("- CSS Tools:")
        for name, info in css_tools.items():
            conf = info.get("confidence", "detected") if isinstance(info, dict) else "detected"
            parts.append(f"  - {name}: confidence={conf}")

    cdn = site_data.get("cdn", []) or []
    if cdn:
        if isinstance(cdn, list):
            parts.append(f"- CDN (network-derived): {', '.join(cdn)}")
        else:
            parts.append(f"- CDN (network-derived): {cdn}")

    parts.append("")
    return "\n".join(parts)


def _format_auth(auth_data: dict | None) -> str:
    parts = ["### Authentication Surface (landing page only -- form_data carries multi-route detail)"]

    if not auth_data or not auth_data.get("has_auth"):
        parts.append("- has_auth: False (no authentication surface detected on landing page)")
        parts.append(
            "- guidance: Section 3 must still define at least Visitor + Admin/Staff roles. "
            "If form_data also has no signup/login routes, treat the product as fully public."
        )
        parts.append("")
        return "\n".join(parts)

    parts.append("- has_auth: True")

    if auth_data.get("login_forms"):
        parts.append("- login_forms (landing page):")
        for form in auth_data["login_forms"]:
            parts.append(f"  - action={form.get('action', 'N/A')}, method={form.get('method', 'POST')}")
            for field in form.get("fields", []) or []:
                parts.append(f"    - {field.get('type', '?')}: {field.get('name', '?')}")

    if auth_data.get("oauth_providers"):
        parts.append(f"- oauth_providers (landing page): {', '.join(auth_data['oauth_providers'])}")

    if (auth_data.get("auth_meta", {}) or {}).get("sdk"):
        parts.append(f"- auth_sdk: {auth_data['auth_meta']['sdk']}")

    if auth_data.get("cookie_consent"):
        cc = auth_data["cookie_consent"]
        parts.append(f"- cookie_consent_banner: {cc.get('selector', 'detected')}")

    parts.append("")
    return "\n".join(parts)


def _format_form_data(form_data: dict) -> str:
    parts = ["### Auth & Onboarding Forms (multi-route harvest)"]

    purpose_routes = form_data.get("purpose_routes", {}) or {}
    if purpose_routes:
        parts.append("- purpose_routes (where each form was harvested):")
        for purpose, url in purpose_routes.items():
            parts.append(f"  - {purpose}: {url}")
    else:
        parts.append("- purpose_routes: none discovered")

    parts.append(f"- password_reset_present: {bool(form_data.get('password_reset_present'))}")
    parts.append(f"- email_verification_present: {bool(form_data.get('email_verification_present'))}")
    parts.append(f"- mfa_present: {bool(form_data.get('mfa_present'))}")
    parts.append(f"- onboarding_present: {bool(form_data.get('onboarding_present'))}")

    sso = form_data.get("sso_providers", []) or []
    if sso:
        parts.append(f"- sso_providers (inferred from sign-in-with buttons): {', '.join(sso)}")

    user_fields = form_data.get("signup_user_fields", []) or []
    if user_fields:
        parts.append("- signup_user_fields (use as User entity in Section 6 and signup flow in Section 4):")
        for f in user_fields[:20]:
            req = "required" if f.get("required") else "optional"
            label = f.get("label") or f.get("placeholder") or f.get("name", "?")
            ac = f.get("autocomplete") or "n/a"
            parts.append(
                f"  - name={f.get('name', '?')}, type={f.get('type', '?')}, "
                f"label='{label}', autocomplete={ac} ({req})"
            )

    login = form_data.get("login_form")
    if login:
        parts.append(f"- login_form: action={login.get('action', '?')} method={login.get('method', 'POST')}")
        for field in (login.get("fields", []) or [])[:10]:
            parts.append(f"  - {field.get('type', '?')}: {field.get('name', '?')}")

    parts.append("")
    return "\n".join(parts)


def _format_business_signals(business_signals: dict) -> str:
    parts = ["### Business Signals (pricing / billing model)"]

    pricing_url = business_signals.get("found_pricing_url")
    if pricing_url:
        parts.append(f"- found_pricing_url: {pricing_url}")
    else:
        parts.append("- found_pricing_url: none (no /pricing or /plans route returned tier markup)")

    parts.append(f"- free_tier_present: {bool(business_signals.get('free_tier_present'))}")
    parts.append(f"- enterprise_contact_only: {bool(business_signals.get('enterprise_contact_only'))}")
    if business_signals.get("currency"):
        parts.append(f"- currency: {business_signals['currency']}")

    hints = business_signals.get("billing_model_hints", []) or []
    if hints:
        parts.append(f"- billing_model_hints: {', '.join(hints)}")

    tiers = business_signals.get("tiers", []) or []
    if tiers:
        parts.append("- pricing_tiers (use to derive role tiers in Section 3 and success metrics in Section 2):")
        for t in tiers:
            name = t.get("name", "?")
            price = t.get("price")
            period = t.get("period", "")
            currency = t.get("currency", "")
            contact_only = t.get("contact_only", False)
            if contact_only or price is None:
                price_str = "contact sales"
            else:
                price_str = f"{currency}{price}/{period}".strip("/")
            parts.append(f"  - {name}: {price_str}")
            for feat in (t.get("features", []) or [])[:6]:
                parts.append(f"    - {feat[:120]}")
            cta = t.get("cta")
            if cta:
                parts.append(f"    - cta: {cta}")
    else:
        parts.append("- No pricing tiers extracted.")

    parts.append("")
    return "\n".join(parts)


def _format_api_doc_data(api_doc_data: dict) -> str:
    parts = ["### API Documentation Probe (OpenAPI / GraphQL / robots / sitemap / .well-known)"]

    openapi = api_doc_data.get("openapi_specs", []) or []
    if openapi:
        parts.append(f"- openapi_specs ({len(openapi)} found -- use as Tier 1 entities + endpoints):")
        for spec in openapi:
            parts.append(f"  - path={spec.get('path')}")
            parts.append(f"    - title: {spec.get('title', 'n/a')}")
            parts.append(f"    - version: {spec.get('version', 'n/a')}")
            servers = spec.get("server_urls", []) or []
            if servers:
                parts.append(f"    - server_urls: {', '.join(servers)}")
            parts.append(f"    - endpoint_count: {spec.get('endpoint_count', 0)}")
            entities = spec.get("entities", []) or []
            if entities:
                parts.append(f"    - entities ({len(entities)}, top 25):")
                for ent in entities[:25]:
                    fields = ent.get("fields", []) or []
                    if fields:
                        field_summary = ", ".join(
                            f"{f.get('name', '?')}:{f.get('type', '?')}" for f in fields[:8]
                        )
                        more = "" if len(fields) <= 8 else f", +{len(fields) - 8} more"
                        parts.append(f"      - {ent.get('name', '?')} {{ {field_summary}{more} }}")
                    else:
                        parts.append(f"      - {ent.get('name', '?')} (no fields captured)")
            endpoints = spec.get("endpoints", []) or []
            if endpoints:
                parts.append(f"    - endpoints ({len(endpoints)}, top 40):")
                for ep in endpoints[:40]:
                    summary = ep.get("summary") or ep.get("operation_id") or ""
                    summary_str = f" -- {summary}" if summary else ""
                    parts.append(f"      - {ep.get('method', '?')} {ep.get('path', '?')}{summary_str}")

    graphql = api_doc_data.get("graphql_schemas", []) or []
    if graphql:
        parts.append(f"- graphql_schemas ({len(graphql)} found):")
        for schema in graphql:
            parts.append(f"  - path={schema.get('path')}")
            parts.append(f"    - query_type: {schema.get('query_type', 'Query')}")
            parts.append(f"    - mutation_type: {schema.get('mutation_type', 'n/a')}")
            parts.append(f"    - subscription_type: {schema.get('subscription_type', 'n/a')}")
            parts.append(f"    - type_count: {schema.get('type_count', 0)}")
            types = schema.get("types", []) or []
            if types:
                parts.append(f"    - types ({len(types)}, top 25):")
                for t in types[:25]:
                    fields = t.get("fields", []) or []
                    if fields:
                        field_summary = ", ".join(
                            f"{f.get('name', '?')}:{f.get('type', '?')}" for f in fields[:8]
                        )
                        more = "" if len(fields) <= 8 else f", +{len(fields) - 8} more"
                        parts.append(
                            f"      - {t.get('name', '?')} ({t.get('kind', '?')}) "
                            f"{{ {field_summary}{more} }}"
                        )
                    else:
                        parts.append(f"      - {t.get('name', '?')} ({t.get('kind', '?')})")

    robots = api_doc_data.get("robots_txt")
    if robots and robots.get("found"):
        parts.append("- robots.txt:")
        for ua in (robots.get("user_agents", []) or [])[:5]:
            parts.append(f"  - user-agent: {ua}")
        disallowed = robots.get("disallowed_paths", []) or []
        if disallowed:
            parts.append(f"  - disallowed ({len(disallowed)}, first 10):")
            for d in disallowed[:10]:
                parts.append(f"    - {d}")
        sitemap_directives = robots.get("sitemaps", []) or []
        if sitemap_directives:
            parts.append(f"  - sitemap directives: {', '.join(sitemap_directives)}")

    sitemaps = api_doc_data.get("sitemaps", {}) or {}
    if sitemaps.get("total_urls"):
        parts.append(f"- sitemaps.total_urls: {sitemaps['total_urls']}")
        urls = sitemaps.get("urls_found", []) or []
        if urls:
            parts.append(f"  - sample URLs ({min(8, len(urls))} of {len(urls)}):")
            for u in urls[:8]:
                parts.append(f"    - {u}")

    well_known = api_doc_data.get("well_known", {}) or {}
    paths_found = well_known.get("paths_found", []) or []
    if paths_found:
        parts.append(f"- .well-known files: {', '.join(paths_found)}")

    if not (openapi or graphql or (robots and robots.get("found")) or sitemaps.get("total_urls") or paths_found):
        parts.append("- No API docs, robots.txt, sitemap.xml, or .well-known artifacts discovered.")

    parts.append("")
    return "\n".join(parts)


def _format_network(network_data: dict) -> str:
    parts = ["### Network & API Capture"]

    parts.append(f"- total_requests: {network_data.get('total_requests', '?')}")
    parts.append(f"- total_transfer_size_kb: {network_data.get('total_transfer_size_kb', '?')}")
    parts.append(f"- third_party_requests: {network_data.get('third_party_requests', '?')}")

    third_party = network_data.get("third_party_domains", []) or []
    if third_party:
        parts.append(f"- third_party_domains: {', '.join(third_party[:10])}")

    apis = network_data.get("api_patterns", []) or []
    if apis:
        parts.append(f"- api_patterns ({len(apis)} normalized, top 30 below):")
        for api in apis[:30]:
            methods = ", ".join(api.get("methods", []) or [])
            parts.append(
                f"  - {api.get('pattern', '?')} -> entity={api.get('entity', 'unknown')}, "
                f"methods=[{methods}], calls={api.get('call_count', 0)}"
            )
            sample = api.get("response_sample")
            if sample and sample.get("body_sample"):
                truncated_str = " (truncated)" if sample.get("truncated") else ""
                body_preview = sample["body_sample"][:600]
                parts.append(f"    - example_url: {sample.get('example_url', api.get('example_url', ''))}")
                parts.append(f"    - response_body_sample{truncated_str}: {body_preview}")

    cms = network_data.get("cms_detected", []) or []
    if cms:
        parts.append(f"- cms_detected (via network): {', '.join(cms)}")

    cdn = network_data.get("cdn_detected", []) or []
    if cdn:
        parts.append(f"- cdn_detected: {', '.join(cdn)}")

    if network_data.get("websocket_count", 0) > 0:
        parts.append(f"- websocket_count: {network_data['websocket_count']}")
        for u in (network_data.get("websocket_urls", []) or [])[:5]:
            parts.append(f"  - {u}")

    parts.append("")
    return "\n".join(parts)


def _format_responsive(responsive_data: dict) -> str:
    parts = ["### Responsive Behavior"]

    breakpoints = responsive_data.get("breakpoints", {}) or {}
    if breakpoints:
        parts.append("- Tested Breakpoints:")
        for name, bp in breakpoints.items():
            if isinstance(bp, dict):
                width = bp.get("width", "?")
                label = bp.get("label", name)
            else:
                width = bp
                label = name
            parts.append(f"  - {name}: width={width}px, label={label}")

    sections = responsive_data.get("per_section_behavior", {}) or {}
    if sections:
        parts.append("- Per-Section Responsive Changes:")
        for section_name, changes in sections.items():
            if section_name.startswith("_"):
                continue
            parts.append(f"  - {section_name}:")
            if isinstance(changes, dict):
                for bp_name, change in changes.items():
                    if isinstance(change, str):
                        parts.append(f"    - {bp_name}: {change}")
                    elif isinstance(change, dict):
                        for prop, val in change.items():
                            parts.append(f"    - {bp_name}: {prop} = {val}")

    typography = responsive_data.get("typography_changes", {}) or {}
    if typography:
        parts.append("- Typography Changes:")
        for bp, changes in typography.items():
            if isinstance(changes, list):
                for c in changes:
                    parts.append(f"  - {bp}: {c}")
            elif isinstance(changes, str):
                parts.append(f"  - {bp}: {changes}")

    behaviors = responsive_data.get("behavior_changes", []) or []
    if behaviors:
        parts.append("- Behavior Changes:")
        for b in behaviors[:10]:
            if isinstance(b, dict):
                parts.append(f"  - {b.get('breakpoint', '?')}: {b.get('change', '?')}")
            else:
                parts.append(f"  - {b}")

    parts.append("")
    return "\n".join(parts)


def _format_performance(performance_data: dict) -> str:
    parts = ["### Performance"]

    targets = performance_data.get("targets", {}) or {}
    if targets:
        parts.append("- Recommended Targets:")
        for key, val in targets.items():
            parts.append(f"  - {key}: {val}")

    measured = performance_data.get("measured", {}) or {}
    if measured:
        parts.append("- Measured Values:")
        for key, val in measured.items():
            parts.append(f"  - {key}: {val}")

    optimizations = performance_data.get("optimizations", []) or []
    if optimizations:
        parts.append("- Recommended Optimizations:")
        for opt in optimizations:
            parts.append(f"  - {opt}")

    parts.append(
        f"- prefers_reduced_motion_handled_in_css: {bool(performance_data.get('reduced_motion_handled'))}"
    )

    a11y = performance_data.get("accessibility", {}) or {}
    if a11y:
        parts.append("- Accessibility Indicators:")
        for key, val in a11y.items():
            parts.append(f"  - {key}: {val}")

    parts.append("")
    return "\n".join(parts)


def _format_assets(asset_data: dict) -> str:
    parts = ["### Asset Reference"]

    screenshots = asset_data.get("screenshots", []) or []
    if screenshots:
        parts.append(f"- Screenshots ({len(screenshots)}):")
        for s in screenshots:
            label = s.get("label", s.get("filename", ""))
            purpose = s.get("purpose", "")
            parts.append(f"  - [{purpose}] {label}")

    assets = asset_data.get("assets", {}) or {}
    for category in ["images", "svgs", "fonts", "videos"]:
        items = assets.get(category, []) or []
        if items:
            parts.append(f"- {category.title()} ({len(items)}, top 10):")
            for item in items[:10]:
                if isinstance(item, dict):
                    parts.append(f"  - {item.get('filename', item.get('url', '?'))}")
                else:
                    parts.append(f"  - {item}")

    parts.append("")
    return "\n".join(parts)


def _format_observed_pages(observed_pages: list[dict] | None, site_data: dict) -> str:
    parts = ["### Observed Pages (typed -- drives Section 5 sub-section selection)"]

    if observed_pages:
        parts.append(f"- typed_pages ({len(observed_pages)} total, top 30 below):")
        for p in observed_pages[:30]:
            route = p.get("route") or p.get("url", "?")
            page_type = p.get("page_type", "other")
            entity = p.get("entity_hint")
            signals = ", ".join(p.get("signals", []) or []) or "n/a"
            qparams = ", ".join(p.get("query_params", []) or [])
            line = f"  - {route} -> page_type={page_type}"
            if entity:
                line += f", entity_hint={entity}"
            if qparams:
                line += f", query_params=[{qparams}]"
            line += f", signals=[{signals}]"
            parts.append(line)
        if len(observed_pages) > 30:
            parts.append(f"  - ... and {len(observed_pages) - 30} more typed routes")
        parts.append("")
        return "\n".join(parts)

    pages = site_data.get("pages", []) or []
    if pages:
        parts.append("- (page typing unavailable; raw URL list)")
        for p in pages[:30]:
            if isinstance(p, dict):
                parts.append(f"  - {p.get('url', '?')}")
            else:
                parts.append(f"  - {p}")
    else:
        parts.append("- Single page application (no additional pages discovered)")

    parts.append("")
    return "\n".join(parts)


def _format_brand_data(brand_data: dict) -> str:
    parts = ["### Brand Identity"]

    if brand_data.get("site_name"):
        parts.append(f"- site_name: {brand_data['site_name']}")

    logo = brand_data.get("logo")
    if logo:
        parts.append(
            f"- logo: {logo.get('src', 'N/A')} (score={logo.get('score', '?')}, "
            f"{logo.get('width', '?')}x{logo.get('height', '?')})"
        )

    favicons = brand_data.get("favicons", []) or []
    if favicons:
        parts.append("- favicons:")
        for f in favicons[:5]:
            parts.append(
                f"  - {f.get('href', '?')} (type={f.get('type', '?')}, sizes={f.get('sizes', '?')})"
            )

    manifest = brand_data.get("manifest")
    if manifest:
        parts.append(
            f"- web_manifest: {manifest.get('name', 'N/A')}, "
            f"theme_color={manifest.get('theme_color', 'N/A')}"
        )

    og = brand_data.get("og", {}) or {}
    if og:
        parts.append("- open_graph:")
        for key, val in og.items():
            if val:
                parts.append(f"  - og:{key}: {str(val)[:120]}")

    parts.append("")
    return "\n".join(parts)


def _format_component_tokens(component_tokens: dict) -> str:
    parts = ["### Component Design Tokens"]

    for comp_type in ["buttons", "inputs", "links", "badges"]:
        items = component_tokens.get(comp_type, []) or []
        if not items:
            continue
        parts.append(f"- {comp_type.title()} ({len(items)}):")
        for item in items[:5]:
            selector = item.get("selector", "?")
            parts.append(f"  - selector: {selector}")
            default = item.get("default", {}) or {}
            if default:
                props = [f"{k}={v}" for k, v in list(default.items())[:6]]
                parts.append(f"    - default: {', '.join(props)}")
            for state in ["hover", "focus", "active"]:
                state_data = item.get(state, {}) or {}
                if state_data:
                    changes = [f"{k}={v}" for k, v in list(state_data.items())[:4]]
                    parts.append(f"    - {state}: {', '.join(changes)}")

    parts.append("")
    return "\n".join(parts)


def _format_dark_mode(dark_mode_data: dict) -> str:
    parts = ["### Dark Mode"]

    parts.append(f"- detection_method: {dark_mode_data.get('detection_method', 'N/A')}")

    overrides = dark_mode_data.get("css_variable_overrides", {}) or {}
    if overrides:
        parts.append(f"- css_variable_overrides ({len(overrides)}, top 15):")
        for name, val in list(overrides.items())[:15]:
            parts.append(f"  - {name}: {val}")

    color_changes = dark_mode_data.get("color_changes", 0)
    if color_changes:
        parts.append(f"- total_color_changes: {color_changes}")

    parts.append("")
    return "\n".join(parts)


def _format_screenshot_references(output_dir: str | None) -> str:
    parts = ["### Reference Screenshots (anchor Section 8 UI/UX requirements)"]

    if not output_dir:
        parts.append("- No output directory provided. Describe UI based on extracted color/typography/layout data.")
        parts.append("")
        return "\n".join(parts)

    screenshots_dir = Path(output_dir) / "screenshots"
    if not screenshots_dir.exists():
        parts.append("- No screenshots directory found. Describe UI based on extracted color/typography/layout data.")
        parts.append("")
        return "\n".join(parts)

    screenshot_files = sorted(screenshots_dir.glob("*.png"))
    if not screenshot_files:
        screenshot_files = sorted(screenshots_dir.glob("*.jpg")) + sorted(screenshots_dir.glob("*.webp"))

    if screenshot_files:
        parts.append(f"- Reference Screenshots ({len(screenshot_files)}):")
        for f in screenshot_files:
            parts.append(f"  - References/{f.name}")
        parts.append("- Use these filenames in Section 8 to ground layout, typography, and component evidence.")
    else:
        parts.append("- No screenshot files found. Describe UI based on extracted color/typography/layout data.")

    parts.append("")
    return "\n".join(parts)
