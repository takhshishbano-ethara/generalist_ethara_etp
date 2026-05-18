"""
Leviathon configuration: SOP rules, banned phrases, rubric weights, breakpoints.
All constants derived from High_End_Design_SOP_v2.pdf and PRD_Scoring.xlsx.
"""

# -- SOP Design Categories --
CATEGORIES = {
    "normal_website": "Normal Website",
    "cool_transition": "Cool Transition",
    "representation": "Representation Format",
    "svg_vector": "SVG & Vector Graphics",
    "3d_webgl": "3D & WebGL / Game",
}

# -- PRD Word Count Constraints --
PRD_MIN_WORDS = 800
PRD_MAX_WORDS = 3500

# -- Tier 1 Banned Phrases (each = -0.5 from S11.1; 5+ = R1 auto-reject) --
TIER1_BANNED_PHRASES = [
    "smooth animation",
    "modern ux",
    "clean layout",
    "nice",
    "beautiful",
    "sleek",
    "elegant",
    "dynamic effect",
    "subtle motion",
    "intuitive navigation",
    "seamless experience",
    "premium feel",
    "eye-catching",
    "visually appealing",
    "professional look",
    "user-friendly",
    "cutting-edge",
    "immersive journey",
    "pixel-perfect",
    "next-level",
    "stunning visuals",
    "state-of-the-art",
    "intuitive interface",
    "leverage cutting-edge",
]

# -- Tier 2 Banned Phrases (violation only if no specific values follow) --
TIER2_BANNED_PHRASES = [
    "responsive design",
    "fast loading",
    "animated elements",
    "hover effects",
    "parallax scrolling",
]

# -- Auto-Reject Triggers --
REJECT_TRIGGERS = {
    "R1": "5+ Tier 1 vague phrases without specific values",
    "R2": "Colors mentioned without hex codes",
    "R3": "No font family names",
    "R4": f"Under {PRD_MIN_WORDS} words",
    "R5": f"Over {PRD_MAX_WORDS} words",
}

# -- Responsive Breakpoints (SOP mandated) --
BREAKPOINTS = {
    "desktop_large": {"width": 1440, "height": 900, "label": "Desktop Large"},
    "tablet_landscape": {"width": 1024, "height": 768, "label": "Tablet Landscape"},
    "tablet_portrait": {"width": 768, "height": 1024, "label": "Tablet Portrait"},
    "mobile": {"width": 375, "height": 812, "label": "Mobile"},
}

# -- Screenshot viewport for primary captures --
PRIMARY_VIEWPORT = {"width": 1920, "height": 1080}

# -- Chromium launch args (AWS Lambda) --
# `--single-process` is REQUIRED on Lambda. Without it, Chromium tries to spawn
# child renderer/GPU processes and crashes immediately with TargetClosedError —
# Lambda's PID namespace + fork() restrictions break multi-process Chromium.
# Side-effect: heavy WebGL/3D sites can render black screenshots; the asset
# collector's blank-detect retry compensates by re-capturing on detection.
#
# The other flags reduce CPU/memory overhead and disable features Lambda can't
# satisfy (background networking, sync, extensions, etc.).
LAMBDA_CHROMIUM_ARGS = [
    "--single-process",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--disable-setuid-sandbox",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints,AudioServiceOutOfProcess,IsolateOrigins,site-per-process",
]

# -- Rubric Sections with weights and criticality --
RUBRIC_SECTIONS = {
    "S1": {
        "name": "Word Count & Format",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "GATE",
        "consequence": "AUTO-REJECT if failed",
    },
    "S2": {
        "name": "Visual & Brand Direction",
        "max_points": 14,
        "weight": 0.14,
        "criticality": "CRITICAL",
        "consequence": "Score <7/14 caps max grade at C (79)",
    },
    "S3": {
        "name": "Site Architecture & Page Specifications",
        "max_points": 18,
        "weight": 0.18,
        "criticality": "CRITICAL",
        "consequence": "Score <9/18 caps max grade at C (79)",
    },
    "S4": {
        "name": "Motion Language",
        "max_points": 14,
        "weight": 0.14,
        "criticality": "CRITICAL",
        "consequence": "Score <7/14 caps max grade at C (79)",
    },
    "S5": {
        "name": "Technical Ambition",
        "max_points": 9,
        "weight": 0.09,
        "criticality": "IMPORTANT",
        "consequence": "Score <4.5/9 caps max grade at B (89)",
    },
    "S6": {
        "name": "Backend & Application Logic",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "REQUIRED",
        "consequence": "Score of 0 = notable gap flagged",
    },
    "S7": {
        "name": "Data Model",
        "max_points": 9,
        "weight": 0.09,
        "criticality": "IMPORTANT",
        "consequence": "Score <4.5/9 caps max grade at B (89)",
    },
    "S8": {
        "name": "Accessibility & Quality",
        "max_points": 9,
        "weight": 0.09,
        "criticality": "IMPORTANT",
        "consequence": "Score <4.5/9 caps max grade at B (89)",
    },
    "S9": {
        "name": "Content & SEO",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "REQUIRED",
        "consequence": "Score of 0 = notable gap flagged",
    },
    "S10": {
        "name": "Cool Transition Addendum",
        "max_points": 7,
        "weight": 0.07,
        "criticality": "CATEGORY-SPECIFIC",
        "consequence": "N/A for non-Cool-Transition; points redistributed (93 total / normalized)",
    },
    "S11": {
        "name": "Overall Quality & Coherence",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "HOLISTIC",
        "consequence": "Reflects true specificity of entire submission",
    },
}

# -- Grade Scale --
GRADE_SCALE = [
    (90, 100, "A", "Production-ready"),
    (80, 89, "B", "Solid, minor gaps"),
    (70, 79, "C", "Usable, needs follow-up"),
    (60, 69, "D", "Significant holes"),
    (0, 59, "F", "Major rewrite"),
]

# -- Tech Detection Patterns --
TECH_DETECTION = {
    "three_js": {
        "globals": ["THREE", "__THREE__"],
        "script_patterns": ["three.js", "three.min.js", "three.module.js", "@three"],
        "dom_markers": ["canvas[data-engine]"],
    },
    "gsap": {
        "globals": ["gsap", "GreenSock", "TweenMax", "TweenLite", "TimelineMax"],
        "script_patterns": ["gsap", "greensock", "ScrollTrigger", "ScrollSmoother"],
        "dom_markers": [],
    },
    "lenis": {
        "globals": ["Lenis", "__lenis"],
        "script_patterns": ["lenis", "@studio-freight/lenis"],
        "dom_markers": ["html.lenis", "[data-lenis]"],
    },
    "framer_motion": {
        "globals": [],
        "script_patterns": ["framer-motion", "motion"],
        "dom_markers": ["[data-framer-component-type]", "[data-projection-id]"],
    },
    "lottie": {
        "globals": ["lottie", "bodymovin"],
        "script_patterns": ["lottie", "bodymovin"],
        "dom_markers": ["lottie-player", "dotlottie-player", "[data-anim-type]"],
    },
    "react": {
        "globals": ["__REACT_DEVTOOLS_GLOBAL_HOOK__", "__NEXT_DATA__"],
        "script_patterns": ["react", "next"],
        "dom_markers": ["#__next", "[data-reactroot]"],
    },
    "vue": {
        "globals": ["__VUE__", "__NUXT__"],
        "script_patterns": ["vue", "nuxt"],
        "dom_markers": ["#__nuxt", "[data-v-]"],
    },
    "svelte": {
        "globals": [],
        "script_patterns": ["svelte"],
        "dom_markers": [".svelte-"],
    },
    "bootstrap": {
        "globals": ["bootstrap"],
        "script_patterns": ["bootstrap"],
        "dom_markers": [".navbar-toggler", ".modal-dialog"],
    },
    "material_ui": {
        "globals": [],
        "script_patterns": ["@mui", "@material-ui"],
        "dom_markers": ["[class*='MuiButton']", "[class*='MuiPaper']"],
    },
    "chakra_ui": {
        "globals": [],
        "script_patterns": ["@chakra-ui"],
        "dom_markers": ["[class*='chakra-']"],
    },
    "ant_design": {
        "globals": [],
        "script_patterns": ["antd"],
        "dom_markers": ["[class*='ant-btn']", "[class*='ant-modal']"],
    },
    "vuetify": {
        "globals": [],
        "script_patterns": ["vuetify"],
        "dom_markers": [".v-application", ".v-btn"],
    },
    "quasar": {
        "globals": [],
        "script_patterns": ["quasar"],
        "dom_markers": [".q-page", ".q-btn"],
    },
    "bulma": {
        "globals": [],
        "script_patterns": ["bulma"],
        "dom_markers": [".hero-body", ".is-primary"],
    },
    "mantine": {
        "globals": [],
        "script_patterns": ["@mantine"],
        "dom_markers": ["[class*='mantine-']"],
    },
    "radix_ui": {
        "globals": [],
        "script_patterns": ["@radix-ui"],
        "dom_markers": ["[data-radix-collection-item]"],
    },
    "headless_ui": {
        "globals": [],
        "script_patterns": ["@headlessui"],
        "dom_markers": ["[data-headlessui-state]"],
    },
    "carbon": {
        "globals": [],
        "script_patterns": ["@carbon"],
        "dom_markers": ["[class*='bx--']", "[class*='cds--']"],
    },
    "fluent_ui": {
        "globals": [],
        "script_patterns": ["@fluentui"],
        "dom_markers": ["[class*='fui-']"],
    },
    "foundation": {
        "globals": [],
        "script_patterns": ["foundation"],
        "dom_markers": [".grid-x", ".callout"],
    },
    "barba_js": {
        "globals": ["barba"],
        "script_patterns": ["barba", "barba.js", "@barba/core"],
        "dom_markers": ["[data-barba]", "[data-barba-namespace]"],
    },
    "swup": {
        "globals": ["swup"],
        "script_patterns": ["swup"],
        "dom_markers": [],
    },
    "highway": {
        "globals": ["Highway"],
        "script_patterns": ["highway", "@unseenco/taxi"],
        "dom_markers": ["[data-router-wrapper]", "[data-router-view]"],
    },
    "taxi": {
        "globals": ["Taxi"],
        "script_patterns": ["@unseenco/taxi", "taxi.js"],
        "dom_markers": ["[data-taxi]"],
    },
}

PAGE_TRANSITION_LIBS = ["barba_js", "swup", "highway", "taxi"]

# -- Category Classification Rules --
CATEGORY_RULES = [
    {
        "category": "3d_webgl",
        "required_any": ["three_js", "babylon", "pixi"],
        "condition": "canvas must be fullscreen or >800k px area",
        "description": "Three.js / WebGL canvas detected as primary element",
    },
    {
        "category": "representation",
        "signals": ["horizontal_scroll", "parallax_heavy", "gsap_pin_scrub"],
        "description": "Horizontal scroll or pinned scroll-driven narrative",
    },
    {
        "category": "svg_vector",
        "required_any": ["lottie", "rive", "DrawSVGPlugin", "MorphSVGPlugin"],
        "signals": ["svg_animated_count>=3", "d3_svg_count>=5", "anime+svg_paths"],
        "description": "SVG/vector animation as primary visual language",
    },
    {
        "category": "cool_transition",
        "required_all": ["gsap"],
        "condition": "gsap + (ScrollTrigger OR Lenis) + enough triggers, OR gsap + page transition lib",
        "bonus": ["lenis", "barba", "swup"],
        "description": "GSAP with scroll choreography and/or page transitions",
    },
    {
        "category": "normal_website",
        "signals": [],
        "description": "Default — typography and layout craft, no heavy animation framework detected",
    },
]

# -- Required PRD Sections --
PRD_SECTIONS = [
    "Product Overview",
    "Visual & Brand Direction",
    "Technical Ambition",
    "Site Architecture & Page Specifications",
    "Motion Language",
    "Backend & Application Logic",
    "Accessibility & Quality",
    "Content & SEO",
    "Responsive Behavior",
    "Reference Prototypes",
]

# -- Cool Transition Extra Sections --
COOL_TRANSITION_EXTRAS = [
    "A. Page-to-Page Transition Timing",
    "B. Scroll-Triggered Animation Map",
    "C. Staggered Reveal Sequences",
    "D. Micro-Interaction Specs",
]

# -- Screenshot Strategy (SOP image ordering) --
SCREENSHOT_STRATEGY = {
    "style_targets": {
        "count": 3,
        "description": "Primary style target - full viewport of key pages",
        "positions": ["hero_top", "secondary_page", "tertiary_page"],
    },
    "component_closeups": {
        "count": 3,
        "description": "Component references - close-ups of UI elements",
        "positions": ["navbar", "card_grid", "footer"],
    },
    "structural": {
        "count": 3,
        "description": "Structural/wireframe - layout grid and content hierarchy",
        "positions": ["mid_scroll", "transition_frame", "mobile_view"],
    },
}

# -- Scroll Capture Resolution --
SCROLL_STEP_PERCENT = 5

# -- Interactive Element Selectors --
INTERACTIVE_SELECTORS = [
    "a",
    "button",
    "[role='button']",
    "input",
    "select",
    "textarea",
    ".card",
    "[class*='card']",
    "[class*='Card']",
    "[data-hover]",
    "[class*='btn']",
    "[class*='Btn']",
    "nav a",
    "nav button",
]

# -- Asset MIME Types to Capture --
ASSET_MIME_TYPES = {
    "images": ["image/png", "image/jpeg", "image/webp", "image/avif", "image/gif"],
    "svgs": ["image/svg+xml"],
    "videos": ["video/mp4", "video/webm"],
    "fonts": [
        "font/woff2",
        "font/woff",
        "font/ttf",
        "font/otf",
        "application/font-woff2",
        "application/font-woff",
        "application/x-font-ttf",
    ],
    "json": ["application/json"],
}
