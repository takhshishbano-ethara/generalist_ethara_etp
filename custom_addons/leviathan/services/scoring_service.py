"""PRD Self-Scoring & Validation (v2 rubric).

Ported from leviathon_v2_pipeline/modules/prd_scorer.py.
Constants inlined for Odoo module standalone use.
"""

import re
import json

# Constants inlined from leviathon_v2_pipeline/config.py
PRD_MIN_WORDS = 800
PRD_MAX_WORDS = 3500

TIER1_BANNED_PHRASES = [
    "smooth animation", "modern ux", "clean layout", "nice", "beautiful",
    "sleek", "elegant", "dynamic effect", "subtle motion",
    "intuitive navigation", "seamless experience", "premium feel",
    "eye-catching", "visually appealing", "professional look",
    "user-friendly", "cutting-edge", "immersive journey",
    "pixel-perfect", "next-level", "stunning visuals",
    "state-of-the-art", "intuitive interface", "leverage cutting-edge",
]

TIER2_BANNED_PHRASES = [
    "responsive design", "fast loading", "animated elements",
    "hover effects", "parallax scrolling",
]

RUBRIC_SECTIONS = {
    "S1": {"name": "Word Count & Format", "max_points": 5, "criticality": "GATE", "consequence": "AUTO-REJECT if failed"},
    "S2": {"name": "Visual & Brand Direction", "max_points": 14, "criticality": "CRITICAL", "consequence": "Score <7/14 caps max grade at C"},
    "S3": {"name": "Site Architecture & Page Specifications", "max_points": 18, "criticality": "CRITICAL", "consequence": "Score <9/18 caps max grade at C"},
    "S4": {"name": "Motion Language", "max_points": 14, "criticality": "CRITICAL", "consequence": "Score <7/14 caps max grade at C"},
    "S5": {"name": "Technical Ambition", "max_points": 9, "criticality": "IMPORTANT", "consequence": "Score <4.5/9 caps max grade at B"},
    "S6": {"name": "Backend & Application Logic", "max_points": 5, "criticality": "REQUIRED", "consequence": "Score of 0 = notable gap"},
    "S7": {"name": "Data Model", "max_points": 9, "criticality": "IMPORTANT", "consequence": "Score <4.5/9 caps max grade at B"},
    "S8": {"name": "Accessibility & Quality", "max_points": 9, "criticality": "IMPORTANT", "consequence": "Score <4.5/9 caps max grade at B"},
    "S9": {"name": "Content & SEO", "max_points": 5, "criticality": "REQUIRED", "consequence": "Score of 0 = notable gap"},
    "S10": {"name": "Cool Transition Addendum", "max_points": 7, "criticality": "CATEGORY-SPECIFIC", "consequence": "N/A for non-CT"},
    "S11": {"name": "Overall Quality & Coherence", "max_points": 5, "criticality": "HOLISTIC", "consequence": "Reflects true specificity"},
}

SECTION_MAX_POINTS = {k: v["max_points"] for k, v in RUBRIC_SECTIONS.items()}

GRADE_SCALE = [
    (90, 100, "A", "Production-ready"),
    (80, 89, "B", "Solid, minor gaps"),
    (70, 79, "C", "Usable, needs follow-up"),
    (60, 69, "D", "Significant holes"),
    (0, 59, "F", "Major rewrite"),
]

PRD_SECTIONS = [
    "Product Overview", "Visual & Brand Direction", "Technical Ambition",
    "Site Architecture & Page Specifications", "Motion Language",
    "Backend & Application Logic", "Accessibility & Quality", "Content & SEO",
]

COOL_TRANSITION_EXTRAS = [
    "A. Page-to-Page Transition Timing", "B. Scroll-Triggered Animation Map",
    "C. Staggered Reveal Sequences", "D. Micro-Interaction Specs",
]


def score_prd(prd_text: str, category: str = "Normal Website") -> dict:
    """
    Score a PRD document against the rubric.

    Args:
        prd_text: The full PRD markdown text
        category: The SOP category (affects Cool Transition scoring)

    Returns:
        dict with: total_score, grade, section_scores, reject_triggers, warnings, details
    """
    result = {
        "total_score": 0,
        "grade": "F",
        "reject_triggers": [],
        "section_scores": {},
        "warnings": [],
        "details": {},
    }

    # Strip instruction blocks and code fences before scoring
    cleaned_text = _strip_instructions(prd_text)

    # --- GATE CHECKS (auto-reject) ---
    word_count = _count_words(cleaned_text)
    result["details"]["word_count"] = word_count

    if word_count < PRD_MIN_WORDS:
        result["reject_triggers"].append(f"R4: Under {PRD_MIN_WORDS} words ({word_count})")
    if word_count > PRD_MAX_WORDS:
        result["reject_triggers"].append(f"R5: Over {PRD_MAX_WORDS} words ({word_count})")

    # Tier 1 banned phrases (with word boundaries)
    tier1_violations = _find_banned_phrases(cleaned_text, TIER1_BANNED_PHRASES)
    result["details"]["tier1_violations"] = tier1_violations
    if len(tier1_violations) >= 5:
        result["reject_triggers"].append(f"R1: {len(tier1_violations)} Tier 1 vague phrases")

    # Hex code check
    hex_check = _check_hex_codes(cleaned_text)
    result["details"]["hex_check"] = hex_check
    if hex_check["colors_without_hex"] > 0 and hex_check["colors_with_hex"] == 0:
        result["reject_triggers"].append("R2: Colors mentioned without hex codes")

    # Font name check
    font_check = _check_font_names(cleaned_text)
    result["details"]["font_check"] = font_check
    if not font_check["has_named_fonts"]:
        result["reject_triggers"].append("R3: No font family names specified")

    is_rejected = len(result["reject_triggers"]) > 0

    # --- SECTION-BY-SECTION SCORING ---
    scores = {}

    scores["S1"] = _score_word_count_format(cleaned_text, word_count)
    scores["S2"] = _score_visual_identity(cleaned_text, hex_check, font_check)
    scores["S3"] = _score_page_breakdown(cleaned_text)
    scores["S4"] = _score_animations(cleaned_text)
    scores["S5"] = _score_tech_stack(cleaned_text)
    scores["S6"] = _score_auth(cleaned_text)
    scores["S7"] = _score_data_model(cleaned_text)
    scores["S8"] = _score_responsive(cleaned_text)
    scores["S9"] = _score_performance(cleaned_text)

    if category == "Cool Transition":
        scores["S10"] = _score_cool_transition(cleaned_text)
    else:
        scores["S10"] = {"score": 0, "max": 0, "details": "N/A (not Cool Transition)"}

    scores["S11"] = _score_overall_quality(cleaned_text, tier1_violations)

    result["section_scores"] = scores

    total_available = 100 if category == "Cool Transition" else 93
    raw_total = sum(s["score"] for s in scores.values())
    result["details"]["raw_total"] = raw_total
    result["details"]["total_available"] = total_available

    if category != "Cool Transition":
        normalized = round((raw_total / 93) * 100)
    else:
        normalized = round(raw_total)

    result["total_score"] = min(normalized, 100)

    # Apply grade caps
    grade_caps = []
    for section_key, section_config in RUBRIC_SECTIONS.items():
        if section_key in scores:
            s = scores[section_key]
            max_pts = section_config["max_points"]
            if max_pts > 0 and s["score"] < max_pts * 0.5:
                crit = section_config["criticality"]
                if crit == "CRITICAL":
                    grade_caps.append(("C", 79, section_key, section_config["name"]))
                elif crit == "IMPORTANT":
                    grade_caps.append(("B", 89, section_key, section_config["name"]))

    if grade_caps:
        lowest_cap = min(grade_caps, key=lambda x: x[1])
        result["details"]["grade_cap"] = f"{lowest_cap[0]} ({lowest_cap[1]}) due to {lowest_cap[3]}"
        result["total_score"] = min(result["total_score"], lowest_cap[1])

    if is_rejected:
        result["grade"] = "REJECT"
        result["total_score"] = 0
    else:
        for low, high, grade, meaning in GRADE_SCALE:
            if low <= result["total_score"] <= high:
                result["grade"] = grade
                result["details"]["grade_meaning"] = meaning
                break

    return result


def _strip_instructions(text: str) -> str:
    """Remove instruction blocks, code fences, and meta-prompts that aren't PRD content."""
    # Remove STRICT RULES, REQUIRED SECTIONS, etc. instruction blocks
    cleaned = re.sub(r'## STRICT RULES.*?(?=\n## (?!STRICT)|\Z)', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'## (?:TARGET WEBSITE|DOCUMENT METADATA|REQUIRED SECTIONS|OUTPUT FORMAT|CAPTURED ASSETS|SCREENSHOT REFERENCE|3D .* DETAILS).*?(?=\n## (?!TARGET|DOCUMENT|REQUIRED|OUTPUT|CAPTURED|SCREENSHOT|3D)|\Z)', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    # Remove lines that are clearly instructions (NEVER use, MUST be, etc.)
    cleaned = re.sub(r'^.*?(?:NEVER use these phrases|MUST be between|MUST have|auto-reject|VIOLATION MEANS).*$', '', cleaned, flags=re.MULTILINE | re.IGNORECASE)
    # Remove code fences (```...```)
    cleaned = re.sub(r'```[\s\S]*?```', '', cleaned)
    return cleaned


def _count_words(text):
    """Count words excluding markdown syntax."""
    cleaned = re.sub(r'[#*|`\-]', ' ', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return len(cleaned.split())


def _find_banned_phrases(text, phrases):
    """Find banned phrases in text using word boundaries (case-insensitive)."""
    # Filter out code blocks and table cell separators
    cleaned = re.sub(r'```[\s\S]*?```', '', text)
    cleaned = re.sub(r'`[^`]+`', '', cleaned)
    text_lower = cleaned.lower()

    found = []
    for phrase in phrases:
        # Use word boundaries for single words, substring for multi-word
        if ' ' in phrase:
            pattern = re.escape(phrase.lower())
        else:
            pattern = r'\b' + re.escape(phrase.lower()) + r'\b'

        if re.search(pattern, text_lower):
            found.append(phrase)
    return found


def _check_hex_codes(text):
    """Check that colors are specified with hex codes."""
    hex_pattern = r'#[0-9A-Fa-f]{6}\b'
    hex_matches = re.findall(hex_pattern, text)

    color_words = re.findall(
        r'\b(red|blue|green|yellow|orange|purple|pink|black|white|gray|grey|'
        r'navy|teal|cyan|magenta|maroon|olive|lime|aqua|coral|gold|silver|'
        r'ivory|beige|crimson|indigo|violet|turquoise)\b',
        text.lower()
    )

    colors_with_context = 0
    for word in color_words:
        idx = text.lower().find(word)
        if idx >= 0:
            context = text[max(0, idx-30):idx+30]
            if re.search(hex_pattern, context):
                colors_with_context += 1

    return {
        "colors_with_hex": len(hex_matches),
        "unique_hex_codes": list(set(h.upper() for h in hex_matches)),
        "color_words_found": len(color_words),
        "colors_without_hex": max(0, len(color_words) - colors_with_context),
    }


def _check_font_names(text):
    """Check for actual font family names."""
    generic_fonts = {'sans-serif', 'serif', 'monospace', 'cursive', 'fantasy', 'system-ui'}
    font_descriptions = {'clean', 'bold', 'display', 'body', 'heading', 'text'}

    font_family_pattern = r'(?:font[- ]?family|typeface|font)\s*[:=]?\s*["\']?([A-Z][a-zA-Z\s]+?)(?:["\',;|—\n]|\s-\s|$)'
    matches = re.findall(font_family_pattern, text)

    named_fonts = []
    for m in matches:
        m = m.strip()
        if m.lower() not in generic_fonts and m.lower() not in font_descriptions and len(m) > 2:
            named_fonts.append(m)

    common_fonts = [
        'Inter', 'Roboto', 'Neue Haas', 'Helvetica', 'Arial', 'Oswald', 'Canela',
        'GT Walsheim', 'Druk', 'Bebas', 'Montserrat', 'Poppins', 'DM Sans',
        'Space Grotesk', 'IBM Plex', 'Fira', 'Source', 'Lato', 'Open Sans',
        'Playfair', 'Merriweather', 'Noto', 'Archivo', 'Manrope', 'Syne',
        'Outfit', 'Plus Jakarta', 'Geist', 'Satoshi', 'Cabinet Grotesk',
        'General Sans', 'Clash Display', 'Switzer', 'Instrument Sans',
        'Nunito', 'Pally', 'Amatic', 'Work Sans', 'Barlow', 'Jost',
        'Rubik', 'Karla', 'Quicksand', 'Raleway', 'Ubuntu', 'Mukta',
        'Cabin', 'Exo', 'Overpass', 'Lexend', 'Red Hat',
    ]
    for font in common_fonts:
        if font.lower() in text.lower():
            if font not in named_fonts:
                named_fonts.append(font)

    return {
        "has_named_fonts": len(named_fonts) > 0,
        "named_fonts": named_fonts[:10],
        "font_count": len(named_fonts),
    }


def _score_word_count_format(text, word_count):
    """Score S1: Word Count & Format (5 pts)."""
    score = 0

    if PRD_MIN_WORDS <= word_count <= PRD_MAX_WORDS:
        score += 3
    elif PRD_MAX_WORDS < word_count <= 3800:
        score += 2
    elif 600 <= word_count < PRD_MIN_WORDS:
        score += 1

    has_headers = bool(re.search(r'^##\s', text, re.MULTILINE))
    has_tables = bool(re.search(r'\|.*\|.*\|', text))
    has_structured_lists = bool(re.search(r'^- \*\*\w+.*?:\*\*', text, re.MULTILINE))
    has_sections = text.count('##') >= 5
    if has_headers and (has_tables or has_structured_lists) and has_sections:
        score += 1
    elif has_headers or has_sections:
        score += 0.5

    has_project = bool(re.search(r'(?:project|title)\s*\**\s*[:|]', text[:500], re.IGNORECASE))
    has_category = bool(re.search(r'category\s*\**\s*[:|]', text[:500], re.IGNORECASE))
    has_version = bool(re.search(r'(?:version|date)\s*\**\s*[:|]', text[:500], re.IGNORECASE))
    metadata_count = sum([has_project, has_category, has_version])
    if metadata_count >= 3:
        score += 1
    elif metadata_count >= 1:
        score += 0.5

    return {"score": min(score, 5), "max": 5, "details": {
        "word_count": word_count,
        "has_headers": has_headers,
        "has_tables": has_tables,
        "metadata_count": metadata_count,
    }}


def _score_visual_identity(text, hex_check, font_check):
    """Score S2: Visual Identity (14 pts)."""
    score = 0

    hex_count = hex_check["colors_with_hex"]
    if hex_count >= 5:
        score += 2
    elif hex_count >= 3:
        score += 1
    elif hex_count >= 1:
        score += 0.5

    # Pure black/white penalty (QC P4.3)
    unique_hex_upper = [h.upper() for h in re.findall(r'#[0-9A-Fa-f]{6}\b', text)]
    has_pure_black = '#000000' in unique_hex_upper
    has_pure_white = '#FFFFFF' in unique_hex_upper
    if has_pure_black or has_pure_white:
        score -= 0.5

    semantic_patterns = re.findall(r'(?:Primary|Secondary|Accent|Background|Surface|Text|Border|Muted|Dark|Light)\s*(?:[:–—-]|background|color|text)', text, re.IGNORECASE)
    if len(semantic_patterns) >= 4:
        score += 1
    elif len(semantic_patterns) >= 2:
        score += 0.5

    usage_patterns = re.findall(r'(?:used for|applied to|appears in|used on|for)\s+\w+', text, re.IGNORECASE)
    if len(usage_patterns) >= 3:
        score += 1
    elif len(usage_patterns) >= 1:
        score += 0.5

    restriction_patterns = re.findall(r'\b(?:no gradient|no pastel|never|forbidden|avoid|not used|restricted)\b', text, re.IGNORECASE)
    if restriction_patterns:
        score += 1

    if font_check["font_count"] >= 2:
        score += 2
    elif font_check["font_count"] >= 1:
        score += 1

    weight_pattern = re.findall(r'\b(?:400|500|600|700|800|900|Regular|Medium|Bold|SemiBold|Light|ExtraBold)\b', text)
    tracking_pattern = re.findall(r'(?:letter-spacing|tracking|letter spacing)\s*[:=]?\s*[-\d.]+', text, re.IGNORECASE)
    if len(weight_pattern) >= 3 and tracking_pattern:
        score += 1
    elif len(weight_pattern) >= 2:
        score += 0.5

    type_scale_entries = re.findall(r'(?:H[1-6]|Heading\s*\d|Body|Caption|Display|Overline|Subtitle)\s*\w*\s*\**[|:–-]\**\s*\d+', text, re.IGNORECASE)
    if len(type_scale_entries) >= 3:
        score += 1.5
    elif len(type_scale_entries) >= 2:
        score += 0.75

    # Penalty: empty component tokens (bg=, color= with no value)
    empty_tokens = re.findall(r'bg=,|color=,|radius=,|padding=\s*$', text, re.MULTILINE)
    if len(empty_tokens) >= 3:
        score -= 1.0
    elif len(empty_tokens) >= 1:
        score -= 0.5

    grid_patterns = re.findall(r'\b\d+-col(?:umn)?\b|\bmax-width\s*[:=]?\s*\d+px\b|\bgutter\b|\bgap\b', text, re.IGNORECASE)
    if len(grid_patterns) >= 2:
        score += 1.5
    elif len(grid_patterns) >= 1:
        score += 0.75

    spacing_patterns = re.findall(r'\b(?:4px|8px|baseline|spacing scale|grid unit|spacing unit)\b', text, re.IGNORECASE)
    if spacing_patterns:
        score += 1

    philosophy_section = re.search(r'(?:philosophy|aesthetic|design language|visual direction|design intent)\b', text, re.IGNORECASE)
    if philosophy_section:
        score += 1

    # Design tokens / CSS custom properties
    custom_props = re.findall(r'--[a-zA-Z][\w-]+\s*`?\s*[:|,.]', text)
    if len(custom_props) >= 3:
        score += 1
    elif len(custom_props) >= 1:
        score += 0.5

    return {"score": min(score, 14), "max": 14, "details": {
        "hex_count": hex_count,
        "semantic_tokens": len(semantic_patterns),
        "font_count": font_check["font_count"],
        "weight_count": len(weight_pattern),
    }}


def _score_page_breakdown(text):
    """Score S3: Page-by-Page Breakdown (18 pts)."""
    score = 0

    # Expanded page name recognition
    page_pattern = (
        r'(?:^|\n)#+\s*(?:\d+\.?\d*\s*)?'
        r'(?:Home|About|Contact|Blog|Portfolio|Product|Gallery|Detail|Profile|Settings|'
        r'Dashboard|Login|Register|Search|Game|News|Article|Athletes?|Esports?|Events?|'
        r'Shop|Pricing|Team|Features?|Landing|Hero|Global|Main|Index|Archive|Category|'
        r'Services?|Work|Projects?|Case\s*Study|FAQ|Help|Support|Privacy|Terms|Careers?|'
        r'Download|Documentation|Docs|API|Community|Forum|Checkout|Cart|Wishlist|Orders?|'
        r'3D\s*Scene|WebGL|Canvas|Playground|Experience|World|Map|Level|Menu|Lobby|'
        r'Single\s*Page|One\s*Page|Splash|Intro|Onboarding|Tour|Guide)'
    )
    page_headers = re.findall(page_pattern, text, re.IGNORECASE)
    if len(page_headers) >= 5:
        score += 3
    elif len(page_headers) >= 3:
        score += 2
    elif len(page_headers) >= 1:
        score += 1

    globals_found = sum([
        bool(re.search(r'(?:nav(?:igation)?|header)\b.*(?:\d+px|\d+ms|blur|backdrop|opacity)', text, re.IGNORECASE)),
        bool(re.search(r'footer\b.*(?:\d+|col|grid)', text, re.IGNORECASE)),
        bool(re.search(r'preloader|loading\s*(?:screen|state|animation)', text, re.IGNORECASE)),
    ])
    score += min(globals_found, 2)

    layout_specs = re.findall(r'\b(?:\d+-col|\d+vh|\d+vw|full-bleed|sidebar|sticky|split|centered|grid|flex)\b', text, re.IGNORECASE)
    if len(layout_specs) >= 5:
        score += 2.5
    elif len(layout_specs) >= 3:
        score += 1.5
    elif len(layout_specs) >= 1:
        score += 0.5

    content_specs = re.findall(r'\b(?:H[1-6]|headline|subline|CTA|button|image|video|card|form|input|icon|badge|tag|chip|avatar|thumbnail)\b', text, re.IGNORECASE)
    if len(content_specs) >= 10:
        score += 2.5
    elif len(content_specs) >= 5:
        score += 1.5

    component_behavior = re.findall(r'(?:modal|carousel|dropdown|tooltip|accordion|tabs?|overlay|search|drawer|popover|sheet)\b.*?(?:\d+ms|\d+px|scale|opacity|translate)', text, re.IGNORECASE)
    if len(component_behavior) >= 3:
        score += 3
    elif len(component_behavior) >= 1:
        score += 1.5

    entry_patterns = re.findall(r'(?:entry|enter|appear|fade\s*in|slide\s*in|reveal|mount)\b.*?\d+ms', text, re.IGNORECASE)
    if len(entry_patterns) >= 3:
        score += 2
    elif len(entry_patterns) >= 1:
        score += 1

    effects = re.findall(r'\b(?:parallax|Ken Burns|ticker|marquee|displacement|particle|3D|WebGL|video\s*overlay|data\s*viz|physics|ray\s*cast|collision|orbit|camera)\b', text, re.IGNORECASE)
    if len(effects) >= 2:
        score += 1.5
    elif len(effects) >= 1:
        score += 0.75

    card_specs = re.findall(r'(?:card|tile)\b.*?(?:\d+:\d+|\d+px|\d+×\d+|aspect)', text, re.IGNORECASE)
    if len(card_specs) >= 2:
        score += 1.5
    elif len(card_specs) >= 1:
        score += 0.75

    return {"score": min(score, 18), "max": 18, "details": {
        "pages_found": len(page_headers),
        "globals_found": globals_found,
    }}


def _score_animations(text):
    """Score S4: Animation & Transition Specs (14 pts)."""
    score = 0

    # Filter out 0ms values - they're not real animation durations
    ms_values = re.findall(r'\b(\d+)\s*ms\b', text)
    real_ms = [v for v in ms_values if int(v) > 0]
    if len(real_ms) >= 10:
        score += 2
    elif len(real_ms) >= 5:
        score += 1.5
    elif len(real_ms) >= 2:
        score += 1

    easing = re.findall(r'(?:cubic-bezier|ease-(?:in|out|in-out)|power\d\.(?:in|out|inOut)|linear|back\.out|expo\.out|elastic)', text, re.IGNORECASE)
    if len(easing) >= 5:
        score += 2
    elif len(easing) >= 3:
        score += 1.5
    elif len(easing) >= 1:
        score += 0.5

    triggers = re.findall(r'(?:on\s*(?:load|scroll|hover|click|focus)|trigger|viewport|scroll\s*depth|delay\s*[:=]?\s*\d+ms)', text, re.IGNORECASE)
    if len(triggers) >= 5:
        score += 1.5
    elif len(triggers) >= 2:
        score += 0.75

    transforms = re.findall(r'(?:scale\(|translate[XYZ]?\(|opacity\s*\d|rotate[XYZ]?\(|skew[XY]?\(|clip-path)', text, re.IGNORECASE)
    if len(transforms) >= 5:
        score += 1.5
    elif len(transforms) >= 2:
        score += 0.75

    animated_elements = re.findall(r'(?:hero|heading|CTA|button|card|nav|footer|modal|image|text|title|subtitle|mesh|camera|scene|object)\b.*?\d+ms', text, re.IGNORECASE)
    if len(animated_elements) >= 8:
        score += 2
    elif len(animated_elements) >= 4:
        score += 1

    has_duration_range = bool(re.search(r'(?:duration|timing)\s*(?:range|:).*\d+.*\d+', text, re.IGNORECASE))
    has_default_easing = bool(re.search(r'(?:default|primary)\s*(?:easing|ease|curve)', text, re.IGNORECASE))
    has_forbidden = bool(re.search(r'(?:forbidden|never|avoid|banned)\s*(?:motion|animation|easing|linear)', text, re.IGNORECASE))
    motion_rules = sum([has_duration_range, has_default_easing, has_forbidden])
    if motion_rules >= 3:
        score += 2
    elif motion_rules >= 1:
        score += 1

    has_scroll_lib = bool(re.search(r'(?:Lenis|smooth\s*scroll|scrollerProxy|lerp\s*[:=]?\s*[\d.]+)', text, re.IGNORECASE))
    if has_scroll_lib:
        score += 1.5

    has_loading = bool(re.search(r'(?:skeleton|shimmer|loading\s*(?:state|animation)|placeholder|spinner)', text, re.IGNORECASE))
    if has_loading:
        score += 1.5

    return {"score": min(score, 14), "max": 14, "details": {
        "ms_values": len(real_ms),
        "easing_functions": len(easing),
        "transforms": len(transforms),
    }}


def _score_tech_stack(text):
    """Score S5: Technical Stack (9 pts)."""
    score = 0

    # Expanded framework recognition (word boundaries to prevent false positives like "drives" → "Rive")
    framework = re.search(
        r'\b(?:Next\.js|Nuxt|SvelteKit|Remix|Gatsby|React|Vue|Angular|Solid|Qwik|Astro|'
        r'Three\.js|Babylon\.js|Pixi\.js|p5\.js|Rive|Spline|'
        r'WordPress|Shopify|Webflow|Squarespace|Wix|Framer|Cargo)\b\s*\d*',
        text, re.IGNORECASE
    )
    has_version = bool(re.search(
        r'(?:Next\.js|React|Vue|Nuxt|Svelte|Three\.js|Angular|Gatsby|GSAP|Babylon)\s+[\dvr]+[\d.]*',
        text, re.IGNORECASE
    ))
    if framework and has_version:
        score += 1.5
    elif framework:
        score += 0.75

    anim_libs = re.findall(
        r'(?:GSAP|Framer Motion|Lenis|ScrollTrigger|anime\.js|popmotion|'
        r'Barba\.js|Swup|Highway|Motion One|Locomotive Scroll|ScrollSmoother)\s*(?:\d[\d.]*)?',
        text, re.IGNORECASE
    )
    versioned_anim = re.findall(
        r'(?:GSAP|Framer Motion|Lenis|ScrollTrigger)\s+\d[\d.]*',
        text, re.IGNORECASE
    )
    if len(versioned_anim) >= 2:
        score += 1.5
    elif len(anim_libs) >= 2:
        score += 0.75

    cms = re.search(r'(?:Sanity|Contentful|Strapi|WordPress|Prismic|DatoCMS|Hygraph|Keystatic|Payload|Storyblok)', text, re.IGNORECASE)
    if cms:
        score += 1.5

    hosting = re.findall(r'\b(?:Vercel|Netlify|AWS|Cloudflare|Cloudinary|Mux|Imgix|Akamai|Fastly|Firebase|Render|Railway|Fly\.io)\b', text, re.IGNORECASE)
    if len(hosting) >= 2:
        score += 1.5
    elif len(hosting) >= 1:
        score += 0.75

    purpose_patterns = re.findall(r'\b(?:for|used for|handles|manages|provides|enables|powers|renders)\b\s+\w+', text, re.IGNORECASE)
    if len(purpose_patterns) >= 5:
        score += 1.5
    elif len(purpose_patterns) >= 2:
        score += 0.75

    # Architecture decisions — reward genuine technical descriptions, not "instead of" fabrication
    arch_descriptions = re.findall(r'(?:handles|manages|provides|renders|drives|powers)\s+\w+', text, re.IGNORECASE)
    if len(arch_descriptions) >= 3:
        score += 1.5
    elif len(arch_descriptions) >= 1:
        score += 0.75

    # Penalty: fabricated "instead of" comparisons mentioning libraries not in Framework section
    instead_of_patterns = re.findall(r'instead of|rather than|not used because|was considered but', text, re.IGNORECASE)
    if len(instead_of_patterns) >= 3:
        score -= 0.5

    # TypeScript/JavaScript declaration check (QC P5.2)
    has_ts_js = bool(re.search(r'\bTypeScript\b|\bJavaScript\b', text, re.IGNORECASE))
    if has_ts_js:
        score += 0.5

    return {"score": min(score, 9), "max": 9, "details": {
        "framework": bool(framework),
        "anim_libs": len(anim_libs),
        "cms": bool(cms),
    }}


def _score_auth(text):
    """Score S6: User Roles & Auth (5 pts)."""
    score = 0

    roles = re.findall(r'(?:Visitor|Admin|Editor|User|Member|Guest|Subscriber|Manager|Owner|Moderator|Client|Player|Viewer)\b', text, re.IGNORECASE)
    unique_roles = set(r.lower() for r in roles)
    if len(unique_roles) >= 3:
        score += 1.5
    elif len(unique_roles) >= 2:
        score += 0.75

    access = re.findall(r'(?:read-only|can view|can edit|can create|can delete|full access|restricted|permission|public access)', text, re.IGNORECASE)
    if len(access) >= 3:
        score += 1.5
    elif len(access) >= 1:
        score += 0.75

    auth_flow = re.search(r'(?:OAuth|SSO|email|password|magic link|JWT|session|Clerk|Auth0|NextAuth|Firebase Auth|Supabase Auth|Discord)', text, re.IGNORECASE)
    no_auth = re.search(r'(?:no auth|no authentication|public|static|no login|no sign.?in|authentication.{0,10}not required)', text, re.IGNORECASE)
    if auth_flow:
        score += 1
    elif no_auth:
        score += 1

    edge_cases = re.findall(r'(?:session expir|redirect|protected route|middleware|refresh token|rate limit)', text, re.IGNORECASE)
    if len(edge_cases) >= 2:
        score += 1
    elif len(edge_cases) >= 1:
        score += 0.5

    return {"score": min(score, 5), "max": 5, "details": {
        "roles_found": len(unique_roles),
    }}


def _score_data_model(text):
    """Score S7: Data Model (9 pts)."""
    score = 0

    schemas = re.findall(r'(?:schema|entity|model|collection|type|table)\s*[:=]?\s*[`"]?(\w+)', text, re.IGNORECASE)
    if len(schemas) >= 3:
        score += 2
    elif len(schemas) >= 1:
        score += 1

    fields = re.findall(r'(?:title|slug|name|email|description|content|image|date|status|type|url|body|excerpt|id|created|updated|author|category|tags?|price|quantity)\s*[:–|]', text, re.IGNORECASE)
    if len(fields) >= 8:
        score += 2
    elif len(fields) >= 4:
        score += 1

    types = re.findall(r'\b(?:string|boolean|datetime|number|integer|float|reference|array|object|portableText|slug|image|url|enum|text|json|uuid|timestamp)\b', text, re.IGNORECASE)
    if len(types) >= 5:
        score += 1.5
    elif len(types) >= 2:
        score += 0.75

    relationships = re.findall(r'(?:reference|foreign key|relates to|linked to|belongs to|has many|one-to-many|many-to-many)', text, re.IGNORECASE)
    if relationships:
        score += 1

    rules = re.findall(r'(?:ISR|revalidat|rate limit|cache|webhook|CDN|SWR|polling|real.?time|static generation|SSR|SSG)', text, re.IGNORECASE)
    if len(rules) >= 2:
        score += 1.5
    elif len(rules) >= 1:
        score += 0.75

    flow = re.search(r'(?:fetch|query|API route|server component|client fetch|getServerSideProps|getStaticProps|loader|GraphQL|REST)', text, re.IGNORECASE)
    if flow:
        score += 1

    return {"score": min(score, 9), "max": 9, "details": {
        "schemas": len(schemas),
        "fields": len(fields),
    }}


def _score_responsive(text):
    """Score S8: Responsive Behavior (9 pts)."""
    score = 0

    breakpoints = re.findall(r'\b(?:1440|1024|768|375|390|1280|1536|640|480)\s*px\b', text)
    unique_bp = set(breakpoints)
    bp_count = len(unique_bp)
    if bp_count >= 4:
        score += 2
    else:
        score += bp_count * 0.5

    responsive_table = bool(re.search(r'\|.*(?:Desktop|Tablet|Mobile).*\|', text, re.IGNORECASE))
    responsive_list = bool(re.search(r'^- \*\*(?:Desktop|Tablet|Mobile)\b', text, re.IGNORECASE | re.MULTILINE))
    section_responsive = re.findall(r'(?:nav|hero|grid|content|footer|header|sidebar|canvas)\b.*?(?:1024|768|375|mobile|tablet)\b', text, re.IGNORECASE)
    if (responsive_table or responsive_list) and len(section_responsive) >= 3:
        score += 3
    elif len(section_responsive) >= 3:
        score += 2
    elif len(section_responsive) >= 1:
        score += 1

    column_changes = re.findall(r'\d+\s*col\w*\s*(?:→|to)\s*\d+\s*col', text, re.IGNORECASE)
    if column_changes:
        score += 1.5

    size_changes = re.findall(r'\d+px\s*(?:→|to|->)\s*\d+px', text, re.IGNORECASE)
    if len(size_changes) >= 2:
        score += 1
    elif len(size_changes) >= 1:
        score += 0.5

    behavior_changes = re.findall(r'(?:hidden|disabled|removed|replaced|stacked|collapsed|accordion|hamburger|swipe|touch|pinch)\b', text, re.IGNORECASE)
    if len(behavior_changes) >= 3:
        score += 1.5
    elif len(behavior_changes) >= 1:
        score += 0.75

    return {"score": min(score, 9), "max": 9, "details": {
        "breakpoint_count": bp_count,
    }}


def _score_performance(text):
    """Score S9: Performance (5 pts)."""
    score = 0

    lighthouse = re.search(r'(?:Lighthouse|Performance)\s*(?:score)?\s*(?:>|≥|:)?\s*\d+', text, re.IGNORECASE)
    if lighthouse:
        score += 0.5

    vitals = re.findall(r'\b(?:LCP|CLS|TBT|FID|INP|TTFB)\s*(?:<|>|:|≤|≥)?\s*[\d.]+', text, re.IGNORECASE)
    if len(vitals) >= 3:
        score += 1.5
    elif len(vitals) >= 1:
        score += 0.75

    optimizations = re.findall(r'(?:lazy load|code split|GPU|image format|AVIF|WebP|CDN|tree shak|bundle|minif|compress|preload|prefetch|will-change)', text, re.IGNORECASE)
    if len(optimizations) >= 2:
        score += 1
    elif len(optimizations) >= 1:
        score += 0.5

    reduced_motion = re.search(r'prefers-reduced-motion', text, re.IGNORECASE)
    if reduced_motion:
        score += 1

    a11y = re.findall(r'(?:contrast\s*ratio|touch target|focus ring|WCAG|ARIA|alt text|keyboard|screen reader|skip.to|44px|48px)', text, re.IGNORECASE)
    if len(a11y) >= 3:
        score += 1
    elif len(a11y) >= 1:
        score += 0.5

    return {"score": min(score, 5), "max": 5, "details": {
        "has_lighthouse": bool(lighthouse),
        "vitals_count": len(vitals),
        "a11y_count": len(a11y),
    }}


def _score_cool_transition(text):
    """Score S10: Cool Transition Addendum (7 pts)."""
    score = 0

    # Scope S10 scoring to addendum section where possible
    addendum_start = text.lower().find("cool transition addendum")
    s10_text = text[addendum_start:] if addendum_start >= 0 else text

    # Page transitions: match both table format and humanized bullet format
    # Table: "| contact → about | `view-transitions-api` | 11605ms |"
    # Humanized: "- **contact to about:** view-transitions-api - 11605ms - 3 - 0"
    page_transitions = re.findall(r'(?:→|->|to)\s*\w+.*?\d+ms', s10_text)
    if len(page_transitions) >= 3:
        score += 2
    elif len(page_transitions) >= 1:
        score += 1

    # Scroll map: match "5%:" / "5% |" / "5% -" / "**5%:**" patterns
    scroll_map = re.findall(r'\d+%\s*\**(?:→|:|–|-|\|)\**\s*\w+', s10_text)
    if len(scroll_map) >= 5:
        score += 2
    elif len(scroll_map) >= 2:
        score += 1

    # Stagger sequences: match timing patterns in stagger/group context
    staggers = re.findall(r'(?:stagger|inter-element|delay|Group|Element Count)\s*[:=|]?\s*\d+ms', s10_text, re.IGNORECASE)
    # Also count lines that have "Xms" with "`easing`" nearby (stagger table rows)
    stagger_rows = re.findall(r'\d+ms\s*[-|]\s*[`\w]', s10_text)
    total_staggers = len(staggers) + len(stagger_rows)
    if total_staggers >= 3:
        score += 1.5
    elif total_staggers >= 1:
        score += 0.75

    # Micro-interactions: hover/click/focus state descriptions
    micro = re.findall(r'(?:hover|click|focus)\s*\w*\s*\**[:–|]\**\s*\w+', s10_text, re.IGNORECASE)
    # Also match component hover table rows with property names
    micro_rows = re.findall(r'(?:transform|opacity|color|scale|background)\w*', s10_text, re.IGNORECASE)
    if len(micro) >= 5 or len(micro_rows) >= 8:
        score += 1.5
    elif len(micro) >= 2 or len(micro_rows) >= 4:
        score += 0.75

    # Page-transition library/API detection (search full text — lib may be mentioned in tech stack)
    has_page_transition_lib = bool(re.search(
        r'\b(?:Barba\.?js|Barba\s+\d|Swup|view.transitions?.api|View\s*Transitions?\s*API|Highway\.?js|Taxi\.?js)\b',
        text, re.IGNORECASE
    ))
    if has_page_transition_lib:
        score += 0.5
    else:
        score -= 1.0

    # Penalty: placeholder text left in addendum
    has_placeholder = bool(re.search(r'\[Define', s10_text))
    if has_placeholder:
        score -= 1.5

    # Check for actual transition data presence (any format)
    has_transition_data = bool(re.search(
        r'Homepage\s*→|→\s*https?://|to\s+\w+.*\d+ms|transition.*\d+ms|spa-pushstate|view-transitions',
        s10_text, re.IGNORECASE
    ))
    if not has_transition_data and not has_page_transition_lib:
        score -= 0.5

    return {"score": max(min(score, 7), 0), "max": 7, "details": {
        "page_transitions": len(page_transitions),
        "scroll_map_entries": len(scroll_map),
        "stagger_entries": total_staggers,
        "has_placeholder": has_placeholder,
        "has_page_transition_lib": has_page_transition_lib,
    }}


def _score_overall_quality(text, tier1_violations):
    """Score S11: Overall Quality & Coherence (5 pts)."""
    score = 0

    violation_count = len(tier1_violations)
    if violation_count == 0:
        score += 1.5
    elif violation_count <= 2:
        score += 1.5 - (violation_count * 0.5)
    else:
        score += 0

    score += 0.5

    _OLD_SECTIONS = [
        "Visual Identity", "Page-by-Page Breakdown", "Animation & Transition Specs",
        "Technical Requirements", "User Roles & Auth Flows", "Data Model",
        "Responsive Behavior", "Performance Considerations",
    ]
    all_section_names = list(PRD_SECTIONS) + _OLD_SECTIONS
    sections_present = min(8, sum(1 for s in all_section_names if s.lower() in text.lower()))
    if sections_present >= 7:
        score += 1
    elif sections_present >= 5:
        score += 0.5

    hex_count = len(re.findall(r'#[0-9A-Fa-f]{6}', text))
    ms_count = len(re.findall(r'\b[1-9]\d*\s*ms\b', text))
    px_count = len(re.findall(r'\d+px', text))
    has_specifics = hex_count >= 3 and ms_count >= 5 and px_count >= 10
    if has_specifics:
        score += 1

    # High specificity bonus: dense use of concrete values
    if hex_count >= 10 and ms_count >= 20 and px_count >= 50:
        score += 0.5

    # Structured content density bonus: tables or structured bullet lists
    table_rows = len(re.findall(r'^\|[^|]+\|', text, re.MULTILINE))
    structured_bullets = len(re.findall(r'^- \*\*\w+', text, re.MULTILINE))
    if (table_rows + structured_bullets) >= 30:
        score += 0.5

    return {"score": min(score, 5), "max": 5, "details": {
        "tier1_violations": violation_count,
        "sections_present": sections_present,
    }}
